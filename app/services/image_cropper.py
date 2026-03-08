"""
Image Cropper Service
=====================
Renders each PDF page at a configurable DPI and crops the bounding-box
region of each DuctCandidate into a PNG byte string ready for
transmission to GPT-4o.
"""

from __future__ import annotations

import logging
from io import BytesIO

import fitz  # PyMuPDF

from app.models.schemas import BoundingBox, DuctCandidate

logger = logging.getLogger(__name__)

# Default render resolution (points-per-inch × scale factor = DPI)
DEFAULT_DPI: int = 150
# Minimum crop dimension in pixels; too-small crops are skipped
MIN_CROP_PX: int = 20


def _pdf_to_scale_matrix(dpi: int) -> fitz.Matrix:
    """Return a fitz.Matrix that scales from PDF points to pixels at *dpi*."""
    scale = dpi / 72.0
    return fitz.Matrix(scale, scale)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def crop_duct_region(
    pdf_bytes: bytes,
    candidate: DuctCandidate,
    dpi: int = DEFAULT_DPI,
) -> bytes | None:
    """
    Render the page containing *candidate* and return a PNG crop of its
    bounding-box region.

    Returns
    -------
    PNG image bytes, or None if the crop is too small to be useful.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    try:
        page_num = candidate.bbox.page
        if page_num >= len(doc):
            logger.warning("Page %d out of range in PDF", page_num)
            return None

        page = doc[page_num]
        page_rect = page.rect
        mat = _pdf_to_scale_matrix(dpi)

        # Clamp in PDF-space (points)
        x0_pt = _clamp(candidate.bbox.x0, 0, page_rect.width)
        y0_pt = _clamp(candidate.bbox.y0, 0, page_rect.height)
        x1_pt = _clamp(candidate.bbox.x1, 0, page_rect.width)
        y1_pt = _clamp(candidate.bbox.y1, 0, page_rect.height)

        scale = dpi / 72.0
        w_px = (x1_pt - x0_pt) * scale
        h_px = (y1_pt - y0_pt) * scale
        if w_px < MIN_CROP_PX or h_px < MIN_CROP_PX:
            logger.debug(
                "Candidate %d crop too small (%.1f × %.1f px) – skipped",
                candidate.id, w_px, h_px,
            )
            return None

        clip_rect = fitz.Rect(x0_pt, y0_pt, x1_pt, y1_pt)
        sub_pix = page.get_pixmap(matrix=mat, clip=clip_rect, alpha=False)

        buf = BytesIO()
        buf.write(sub_pix.tobytes("png"))
        return buf.getvalue()

    finally:
        doc.close()


def crop_all_candidates(
    pdf_bytes: bytes,
    candidates: list[DuctCandidate],
    dpi: int = DEFAULT_DPI,
) -> dict[int, bytes]:
    """
    Crop every candidate from the PDF.

    Returns
    -------
    Mapping of candidate.id → PNG bytes (candidates that fail are omitted).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    scale = dpi / 72.0
    mat = _pdf_to_scale_matrix(dpi)

    crops: dict[int, bytes] = {}
    try:
        for cand in candidates:
            pn = cand.bbox.page
            if pn < 0 or pn >= len(doc):
                logger.debug("Candidate %d has invalid page %d", cand.id, pn)
                continue

            page = doc[pn]
            page_rect = page.rect

            x0_pt = _clamp(cand.bbox.x0, 0, page_rect.width)
            y0_pt = _clamp(cand.bbox.y0, 0, page_rect.height)
            x1_pt = _clamp(cand.bbox.x1, 0, page_rect.width)
            y1_pt = _clamp(cand.bbox.y1, 0, page_rect.height)

            w_px = (x1_pt - x0_pt) * scale
            h_px = (y1_pt - y0_pt) * scale
            if w_px < MIN_CROP_PX or h_px < MIN_CROP_PX:
                logger.debug("Candidate %d too small – skipped", cand.id)
                continue

            try:
                clip_rect = fitz.Rect(x0_pt, y0_pt, x1_pt, y1_pt)
                sub_pix = page.get_pixmap(matrix=mat, clip=clip_rect, alpha=False)
                crops[cand.id] = sub_pix.tobytes("png")
            except Exception as exc:
                logger.warning("Failed to crop candidate %d: %s", cand.id, exc)
    finally:
        doc.close()

    logger.info("Cropped %d / %d candidates", len(crops), len(candidates))
    return crops
