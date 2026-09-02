"""OpenCV-based business-card rectification.

Strategy (borrowed from DocAligner/DocScanner ideas + PyImageSearch):
1. Glossy-tolerant pre-processing: CLAHE on L channel + bilateral filter.
2. Two-channel edge: Sobel-magnitude(Otsu) OR adaptive-Gaussian threshold.
   This beats Canny alone on glossy/low-contrast Korean business cards.
3. Morphological close to bridge specular-highlight gaps in the border.
4. Largest 4-vertex contour with area > 15% of frame is the card.
5. Order corners by atan2 around centroid (TL, TR, BR, BL).
6. Force output to ISO/IEC 7810 ID-1 ratio 856×540 (landscape).
   If the detected quad is portrait, swap corner ordering so the
   long edge maps to horizontal.

The whole module degrades to returning None on any failure so the
caller can fall back to the original image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import cv2
import numpy as np


# ISO/IEC 7810 ID-1 (85.60 × 53.98 mm) → 1.586:1.  Use 10 px/mm.
CARD_W = 856
CARD_H = 540
MIN_AREA_RATIO = 0.12  # quad must cover at least 12% of frame
DETECT_LONG_SIDE = 1024  # downscale long side for detection


def rectify_card(source_path: Path, output_path: Path) -> Optional[Path]:
    """Detect and rectify a business card to landscape ID-1 dimensions.

    Returns the output path on success, None on any failure (caller falls
    back to Apple Vision or the original image).
    """
    img = cv2.imread(str(source_path))
    if img is None:
        return None

    quad = _find_card_quad(img)
    if quad is None:
        return None

    quad = _order_corners(quad)
    if not _is_card_shaped(quad):
        return None  # bad detection — let Vision fallback try.

    quad = _force_landscape(quad)
    warped = _warp(img, quad)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), warped, [cv2.IMWRITE_JPEG_QUALITY, 92]):
        return None
    return output_path


def make_landscape_canvas(card_path: Path, output_path: Path, margin: int = 80) -> Path:
    """Wrap an already-rectified landscape card in a white canvas with margin."""
    card = cv2.imread(str(card_path))
    if card is None:
        return card_path
    h, w = card.shape[:2]
    canvas = np.full((h + 2 * margin, w + 2 * margin, 3), 255, dtype=np.uint8)
    canvas[margin:margin + h, margin:margin + w] = card
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return output_path


def _find_card_quad(bgr: np.ndarray) -> Optional[np.ndarray]:
    h, w = bgr.shape[:2]
    scale = DETECT_LONG_SIDE / max(h, w)
    if scale < 1.0:
        small = cv2.resize(bgr, None, fx=scale, fy=scale)
    else:
        scale = 1.0
        small = bgr

    # CLAHE on L channel normalises glossy highlights without blowing out edges.
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    l_ch = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l_ch)
    smoothed = cv2.cvtColor(cv2.merge([l_ch, a_ch, b_ch]), cv2.COLOR_LAB2BGR)
    gray = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)

    frame_area = small.shape[0] * small.shape[1]

    # Try edge maps in order of selectivity. Sobel-only is best on glossy
    # cards because adaptive threshold drags in interior text/logos and
    # pollutes the card outline. Adaptive helps low-contrast cards. Canny
    # is the textbook last resort.
    for edges in _edge_candidates(gray):
        quad = _quad_from_edges(edges, frame_area, scale)
        if quad is not None:
            return quad
    return None


def _edge_candidates(gray: np.ndarray):
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))

    # 1. Sobel magnitude + Otsu, alone.
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.convertScaleAbs(cv2.magnitude(gx, gy))
    _, edges_sobel = cv2.threshold(mag, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield cv2.morphologyEx(edges_sobel, cv2.MORPH_CLOSE, close_kernel)

    # 2. Sobel OR adaptive — catches edges Sobel misses on low contrast.
    edges_adapt = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 10
    )
    combined = cv2.bitwise_or(edges_sobel, edges_adapt)
    yield cv2.morphologyEx(combined, cv2.MORPH_CLOSE, close_kernel)

    # 3. Canny textbook fallback.
    edges_canny = cv2.Canny(gray, 50, 150)
    yield cv2.morphologyEx(edges_canny, cv2.MORPH_CLOSE, close_kernel)


def _quad_from_edges(edges: np.ndarray, frame_area: int, scale: float) -> Optional[np.ndarray]:
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:8]

    # Pass 1: exact 4-vertex convex polygon (best fidelity).
    for contour in contours:
        if cv2.contourArea(contour) < MIN_AREA_RATIO * frame_area:
            continue
        peri = cv2.arcLength(contour, True)
        for eps_mult in (0.02, 0.025, 0.03, 0.04, 0.05):
            approx = cv2.approxPolyDP(contour, eps_mult * peri, True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                return approx.reshape(4, 2).astype(np.float32) / scale

    # Pass 2: rotated minAreaRect when contour is too jagged for a clean
    # 4-vertex collapse. Accept when the rect explains >=55% of the contour
    # area (lower than the textbook 0.7 because real card contours have
    # interior holes from the printed logo).
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < MIN_AREA_RATIO * frame_area:
            continue
        rect = cv2.minAreaRect(contour)
        rw, rh = rect[1]
        if rw < 1 or rh < 1:
            continue
        if area / (rw * rh) < 0.55:
            continue
        return cv2.boxPoints(rect).astype(np.float32) / scale
    return None


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Return corners as (top-left, top-right, bottom-right, bottom-left).

    Canonical sum/diff trick from PyImageSearch — robust to any input order
    and to rotation. TL minimises x+y, BR maximises x+y, TR minimises y-x,
    BL maximises y-x (image coordinates, y grows downward).
    """
    s = pts.sum(axis=1)
    d = pts[:, 1] - pts[:, 0]
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


def _is_card_shaped(quad: np.ndarray) -> bool:
    """Reject detections that aren't plausibly a business-card outline.

    A real card-like quad (even with strong keystone) has:
      - opposite sides of similar length (ratio >= 0.75 each pair),
      - aspect (long edge / short edge) between 1.25 and 2.10
        (ID-1 is 1.585; tolerance covers keystoned views).
    Anything outside is almost always the detector latching onto the user's
    hand, desk, or background — we'd rather fall back to Apple Vision than
    warp those into a fake card.
    """
    tl, tr, br, bl = quad
    top    = float(np.linalg.norm(tr - tl))
    bottom = float(np.linalg.norm(br - bl))
    left   = float(np.linalg.norm(bl - tl))
    right  = float(np.linalg.norm(br - tr))
    if min(top, bottom, left, right) < 1:
        return False
    if min(top, bottom) / max(top, bottom) < 0.75:
        return False
    if min(left, right) / max(left, right) < 0.75:
        return False
    long_side  = max((top + bottom) / 2, (left + right) / 2)
    short_side = min((top + bottom) / 2, (left + right) / 2)
    aspect = long_side / short_side
    return 1.25 <= aspect <= 2.10


def _force_landscape(quad: np.ndarray) -> np.ndarray:
    """Ensure the long edge maps to the horizontal axis of the output card."""
    tl, tr, br, bl = quad
    width_top = np.linalg.norm(tr - tl)
    height_left = np.linalg.norm(bl - tl)
    if height_left > width_top:
        # Detected quad is portrait — rotate corners 90° clockwise so the long
        # edge ends up horizontal once warped.
        return np.array([bl, tl, tr, br], dtype=np.float32)
    return quad


def _warp(bgr: np.ndarray, quad: np.ndarray) -> np.ndarray:
    dst = np.array(
        [[0, 0], [CARD_W - 1, 0], [CARD_W - 1, CARD_H - 1], [0, CARD_H - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(quad, dst)
    return cv2.warpPerspective(bgr, matrix, (CARD_W, CARD_H), flags=cv2.INTER_CUBIC)
