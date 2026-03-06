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

        # Convert PDF-space bbox to pixel space
        scale = dpi / 72.0
        x0 = _clamp(candidate.bbox.x0, 0, page_rect.width) * scale
        y0 = _clamp(candidate.bbox.y0, 0, page_rect.height) * scale
        x1 = _clamp(candidate.bbox.x1, 0, page_rect.width) * scale
        y1 = _clamp(candidate.bbox.y1, 0, page_rect.height) * scale

        if (x1 - x0) < MIN_CROP_PX or (y1 - y0) < MIN_CROP_PX:
            logger.debug(
                "Candidate %d crop too small (%.1f × %.1f px) – skipped",
                candidate.id, x1 - x0, y1 - y0,
            )
            return None

        # Render full page then extract the sub-rectangle
        pixmap = page.get_pixmap(matrix=mat, alpha=False)
        clip = fitz.IRect(int(x0), int(y0), int(x1), int(y1))
        sub = pixmap.set_origin(0, 0)  # ensure origin at 0,0
        # Use a fresh pixmap clipped to the region
        sub_pix = fitz.Pixmap(
            pixmap,
            fitz.IRect(int(x0), int(y0), int(x1), int(y1)),
        )

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
    # Group by page to avoid reopening the doc repeatedly
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    scale = dpi / 72.0
    mat = _pdf_to_scale_matrix(dpi)

    # Pre-render all needed pages
    page_pixmaps: dict[int, fitz.Pixmap] = {}
    needed_pages = {c.bbox.page for c in candidates}
    for pn in needed_pages:
        if pn < len(doc):
            page_pixmaps[pn] = doc[pn].get_pixmap(matrix=mat, alpha=False)
    doc.close()

    crops: dict[int, bytes] = {}
    for cand in candidates:
        pn = cand.bbox.page
        pixmap = page_pixmaps.get(pn)
        if pixmap is None:
            continue

        page_w = pixmap.width
        page_h = pixmap.height

        x0 = _clamp(cand.bbox.x0 * scale, 0, page_w)
        y0 = _clamp(cand.bbox.y0 * scale, 0, page_h)
        x1 = _clamp(cand.bbox.x1 * scale, 0, page_w)
        y1 = _clamp(cand.bbox.y1 * scale, 0, page_h)

        w, h = x1 - x0, y1 - y0
        if w < MIN_CROP_PX or h < MIN_CROP_PX:
            logger.debug("Candidate %d too small – skipped", cand.id)
            continue

        clip = fitz.IRect(int(x0), int(y0), int(x1), int(y1))
        try:
            sub = fitz.Pixmap(pixmap, clip)
            crops[cand.id] = sub.tobytes("png")
        except Exception as exc:
            logger.warning("Failed to crop candidate %d: %s", cand.id, exc)

    logger.info("Cropped %d / %d candidates", len(crops), len(candidates))
    return crops
