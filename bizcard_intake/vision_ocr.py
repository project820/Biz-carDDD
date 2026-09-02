"""Apple Vision OCR wrapper."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VISION_OCR_SOURCE = PROJECT_ROOT / "scripts" / "vision_ocr.swift"
VISION_OCR_BINARY = PROJECT_ROOT / ".cache" / "vision_ocr"

log = logging.getLogger("bizcard")


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float


@dataclass(frozen=True)
class OcrResult:
    lines: tuple[OcrLine, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def average_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


def extract_text(image_path: Path) -> OcrResult:
    binary = _ensure_vision_ocr()
    if binary is None:
        return OcrResult(())

    try:
        completed = subprocess.run(
            [str(binary), str(image_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except Exception:
        log.exception("vision_ocr subprocess failed")
        return OcrResult(())

    if completed.returncode != 0:
        log.warning("vision_ocr failed: %s", completed.stderr.strip())
        return OcrResult(())

    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError:
        log.warning("vision_ocr returned non-json output")
        return OcrResult(())

    lines = tuple(
        OcrLine(str(row.get("text", "")).strip(), float(row.get("confidence", 0.0)))
        for row in rows
        if str(row.get("text", "")).strip()
    )
    return OcrResult(lines)


def _ensure_vision_ocr() -> Path | None:
    if VISION_OCR_BINARY.exists():
        return VISION_OCR_BINARY
    if not VISION_OCR_SOURCE.exists():
        return None

    VISION_OCR_BINARY.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            ["swiftc", str(VISION_OCR_SOURCE), "-O", "-o", str(VISION_OCR_BINARY)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        log.exception("swiftc vision_ocr build failed")
        return None
    if completed.returncode != 0 or not VISION_OCR_BINARY.exists():
        log.warning("swiftc vision_ocr failed: %s", completed.stderr.strip())
        return None
    return VISION_OCR_BINARY
