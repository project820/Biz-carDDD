"""Image preparation for Google Contacts profile photos.

Rectify chain (first hit wins, all failures fall through):
    1. Apple VNDetectDocumentSegmentationRequest (scripts/vision_rectify.swift)
       — Apple's document-specific ML model (WWDC21 #10041). Most accurate
       on real-world phone photos of business cards.
    2. OpenCV detector (bizcard_intake.rectify) — only accepts highly
       card-shaped quads (strict aspect + opposite-side ratio); forces
       ID-1 landscape (856×540) on success. Refuses bad detections so the
       chain falls back to the original rather than warping garbage.
    3. Original image — last resort so the user always sees *something*.

Final contact-photo composition: 1024×1024 white canvas. The card itself keeps
its real business-card ratio and is scaled small enough to survive Google
Contacts' circular crop.
"""

from dataclasses import dataclass
from pathlib import Path
import logging
import subprocess
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VISION_RECTIFY_SOURCE = PROJECT_ROOT / "scripts" / "vision_rectify.swift"
VISION_RECTIFY_BINARY = PROJECT_ROOT / ".cache" / "vision_rectify"

log = logging.getLogger("bizcard")


CANVAS_SIZE = 1024
# Google Contacts displays photos in a circle. For a 1.586:1 card, 82% width
# keeps the whole rectangle inside the inscribed circle with a little breathing
# room, while preserving the card's actual aspect ratio.
CONTENT_FRAC = 0.82
PROFILE_EDGE_CROP_FRAC = 0.035


LLM_MAX_SIDE = 1024  # the parser also gets the OCR text, so a smaller image is enough


@dataclass(frozen=True)
class PreparedImages:
    profile_path: Path
    ocr_path: Path
    llm_path: Path
    rectified_path: Optional[Path]
    source_used: str


def prepare_card_images(source_path: Path, profile_path: Path) -> PreparedImages:
    """Rectify source_path and write both profile and OCR images.

    The profile image is a 1024×1024 white canvas so Google Contacts' circular
    crop keeps the whole card visible. The OCR image is card-only, sharpened,
    and kept larger for text recognition.
    """
    try:
        from PIL import Image, ImageFilter, ImageOps
    except Exception:
        log.warning("PIL not available; returning source image unchanged")
        return PreparedImages(source_path, source_path, source_path, None, "original")

    rectified_path = _try_vision_rectify(
        source_path, profile_path.with_name(f"{profile_path.stem}_rectified.jpg")
    )
    used = "vision"
    if rectified_path is None:
        rectified_path = _try_opencv_rectify(source_path, profile_path)
        used = "opencv" if rectified_path else "original"
    log.info("rectify[%s] %s -> %s", used, source_path.name, rectified_path or source_path)

    image = Image.open(rectified_path or source_path)
    image = ImageOps.exif_transpose(image).convert("RGB")

    # Auto-rotate the source-fallback case to landscape so even when no
    # rectifier succeeded the preview still reads horizontally.
    if used == "original" and image.height > image.width:
        image = image.rotate(-90, expand=True)

    ocr_path = profile_path.parent.parent / "ocr" / f"{source_path.stem}_ocr.jpg"
    ocr_path.parent.mkdir(parents=True, exist_ok=True)
    ocr_image = ImageOps.autocontrast(image, cutoff=1)
    ocr_image = ocr_image.filter(ImageFilter.UnsharpMask(radius=1.2, percent=150, threshold=3))
    if ocr_image.width < 1600:
        scale = 1600 / max(ocr_image.width, 1)
        ocr_image = ocr_image.resize(
            (1600, max(1, int(ocr_image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    ocr_image.save(ocr_path, format="JPEG", quality=95, optimize=True)

    llm_path = profile_path.parent.parent / "llm" / f"{source_path.stem}_llm.jpg"
    llm_path.parent.mkdir(parents=True, exist_ok=True)
    llm_image = ocr_image.copy()
    llm_image.thumbnail((LLM_MAX_SIDE, LLM_MAX_SIDE), Image.Resampling.LANCZOS)
    llm_image.save(llm_path, format="JPEG", quality=80, optimize=True)

    profile_image = _crop_profile_edge_bleed(image) if used != "original" else image.copy()
    profile_image.thumbnail(
        (int(CANVAS_SIZE * CONTENT_FRAC), int(CANVAS_SIZE * CONTENT_FRAC)),
        Image.Resampling.LANCZOS,
    )

    canvas = Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), "white")
    x = (CANVAS_SIZE - profile_image.width) // 2
    y = (CANVAS_SIZE - profile_image.height) // 2
    canvas.paste(profile_image, (x, y))

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(profile_path, format="JPEG", quality=92, optimize=True)
    return PreparedImages(profile_path, ocr_path, llm_path, rectified_path, used)


def make_profile_image(source_path: Path, output_path: Path) -> Path:
    """Compatibility wrapper for tests and one-off scripts."""
    return prepare_card_images(source_path, output_path).profile_path


def _crop_profile_edge_bleed(image):
    """Trim tiny rectification edge artifacts for the human-facing profile photo."""
    inset_x = int(image.width * PROFILE_EDGE_CROP_FRAC)
    inset_y = int(image.height * PROFILE_EDGE_CROP_FRAC)
    if inset_x <= 0 or inset_y <= 0:
        return image.copy()
    return image.crop((inset_x, inset_y, image.width - inset_x, image.height - inset_y))


def _try_opencv_rectify(source_path: Path, output_path: Path) -> Optional[Path]:
    """Run the OpenCV pipeline; return the rectified file or None."""
    try:
        from .rectify import rectify_card
    except Exception:
        log.warning("opencv rectify import failed", exc_info=True)
        return None

    rectified_path = output_path.with_name(f"{output_path.stem}_rectified.jpg")
    try:
        return rectify_card(source_path, rectified_path)
    except Exception:
        log.exception("opencv rectify_card raised")
        return None


def _try_vision_rectify(source_path: Path, rectified_path: Path) -> Optional[Path]:
    if not VISION_RECTIFY_SOURCE.exists():
        return None
    binary = _ensure_vision_rectifier()
    if binary is None:
        return None

    try:
        result = subprocess.run(
            [str(binary), str(source_path), str(rectified_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        log.exception("vision_rectify subprocess failed")
        return None

    if result.returncode != 0 or not rectified_path.exists() or rectified_path.stat().st_size == 0:
        return None
    return rectified_path


def _ensure_vision_rectifier() -> Optional[Path]:
    if VISION_RECTIFY_BINARY.exists():
        return VISION_RECTIFY_BINARY
    VISION_RECTIFY_BINARY.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["swiftc", str(VISION_RECTIFY_SOURCE), "-O", "-o", str(VISION_RECTIFY_BINARY)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        log.exception("swiftc build failed")
        return None
    if result.returncode != 0 or not VISION_RECTIFY_BINARY.exists():
        return None
    return VISION_RECTIFY_BINARY
