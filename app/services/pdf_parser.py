"""
PDF Parser Service
==================
Uses PyMuPDF (fitz) to extract:
  - Vector line segments from page drawing commands
  - Text blocks with their bounding-box positions
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import BinaryIO

import fitz  # PyMuPDF
import httpx

from app.core.config import settings
from app.models.schemas import BoundingBox, LineSegment, TextBlock

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rect_to_lines(rect: fitz.Rect, page_num: int) -> list[LineSegment]:
    """Convert a PDF rect path item into its four edge line segments."""
    x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    return [
        LineSegment(x0=x0, y0=y0, x1=x1, y1=y0, page=page_num),  # top
        LineSegment(x0=x1, y0=y0, x1=x1, y1=y1, page=page_num),  # right
        LineSegment(x0=x0, y0=y1, x1=x1, y1=y1, page=page_num),  # bottom
        LineSegment(x0=x0, y0=y0, x1=x0, y1=y1, page=page_num),  # left
    ]


def _extract_lines_from_path(path: dict, page_num: int) -> list[LineSegment]:
    """
    Walk a single fitz drawing path and yield every straight line segment.
    path["items"] is a list of tuples:
      ('l', p0, p1)  – straight line
      ('c', p0, p1, p2, p3) – bezier curve (approximate as straight line p0→p3)
      ('re', rect)   – axis-aligned rectangle
      ('qu', quad)   – quadrilateral
    """
    segments: list[LineSegment] = []
    items = path.get("items", [])

    for item in items:
        kind = item[0]
        if kind == "l":  # straight line: (kind, p0, p1)
            p0, p1 = item[1], item[2]
            segments.append(
                LineSegment(x0=p0.x, y0=p0.y, x1=p1.x, y1=p1.y, page=page_num)
            )
        elif kind == "c":  # cubic bezier: approximate as straight line
            p0, p3 = item[1], item[4]
            segments.append(
                LineSegment(x0=p0.x, y0=p0.y, x1=p3.x, y1=p3.y, page=page_num)
            )
        elif kind == "re":  # rectangle
            segments.extend(_rect_to_lines(item[1], page_num))
        elif kind == "qu":  # quadrilateral – four edges
            quad = item[1]  # fitz.Quad
            pts = [quad.ul, quad.ur, quad.lr, quad.ll]
            for i in range(4):
                a, b = pts[i], pts[(i + 1) % 4]
                segments.append(
                    LineSegment(x0=a.x, y0=a.y, x1=b.x, y1=b.y, page=page_num)
                )
    return segments


def _text_blocks_from_raw(
    raw: dict,
    page_num: int,
    source: str = "embedded",
) -> list[TextBlock]:
    """Convert a PyMuPDF text dict into TextBlock records."""
    blocks: list[TextBlock] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:  # 0 = text block
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                bbox = span.get("bbox", (0, 0, 0, 0))
                blocks.append(
                    TextBlock(
                        text=text,
                        x0=bbox[0],
                        y0=bbox[1],
                        x1=bbox[2],
                        y1=bbox[3],
                        page=page_num,
                        source=source,
                    )
                )
    return blocks


def _extract_text_blocks(page: fitz.Page, page_num: int) -> list[TextBlock]:
    """Extract every embedded text span on the page with its bounding rectangle."""
    raw = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    return _text_blocks_from_raw(raw, page_num, source="embedded")


def _extract_ocr_text_blocks_from_service(page: fitz.Page, page_num: int) -> list[TextBlock]:
    """Extract OCR spans from external OCR service and map to page coordinates."""
    ocr_dpi = float(max(300, int(settings.ocr_dpi)))
    mat = fitz.Matrix(ocr_dpi / 72.0, ocr_dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    png_bytes = pix.tobytes("png")

    with httpx.Client(timeout=settings.ocr_service_timeout_seconds) as client:
        resp = client.post(
            settings.ocr_service_url,
            files={"file": (f"page-{page_num}.png", png_bytes, "image/png")},
        )
        resp.raise_for_status()
        data = resp.json()

    service_spans = data.get("spans", [])
    blocks: list[TextBlock] = []
    px_to_pt = 72.0 / ocr_dpi
    needs_derotation = (int(page.rotation) % 360) != 0

    for item in service_spans:
        text = str(item.get("text", "")).strip()
        bbox = item.get("bbox")
        if not text or not isinstance(bbox, list) or len(bbox) != 4:
            continue

        try:
            x0_px = float(bbox[0])
            y0_px = float(bbox[1])
            x1_px = float(bbox[2])
            y1_px = float(bbox[3])
        except (TypeError, ValueError):
            continue

        x0 = x0_px * px_to_pt
        y0 = y0_px * px_to_pt
        x1 = x1_px * px_to_pt
        y1 = y1_px * px_to_pt

        if needs_derotation:
            corners = (
                fitz.Point(x0, y0),
                fitz.Point(x1, y0),
                fitz.Point(x1, y1),
                fitz.Point(x0, y1),
            )
            transformed = [point * page.derotation_matrix for point in corners]
            xs = [point.x for point in transformed]
            ys = [point.y for point in transformed]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)

        blocks.append(
            TextBlock(
                text=text,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                page=page_num,
                source="ocr",
            )
        )

    return blocks


def _extract_ocr_text_blocks_local(page: fitz.Page, page_num: int) -> list[TextBlock]:
    """Extract OCR spans via PyMuPDF local OCR (if available)."""
    if not hasattr(page, "get_textpage_ocr"):
        raise RuntimeError("Current PyMuPDF build has no get_textpage_ocr support")

    textpage_ocr = page.get_textpage_ocr(
        language=settings.ocr_language,
        dpi=max(300, int(settings.ocr_dpi)),
        full=True,
    )
    raw_ocr = page.get_text(
        "dict",
        flags=fitz.TEXT_PRESERVE_WHITESPACE,
        textpage=textpage_ocr,
    )
    return _text_blocks_from_raw(raw_ocr, page_num, source="ocr")


def extract_ocr_text_blocks(pdf_bytes: bytes) -> list[TextBlock]:
    """Extract OCR text spans for all pages and return TextBlock list."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    ocr_blocks: list[TextBlock] = []

    try:
        for page_num, page in enumerate(doc):
            try:
                if settings.use_ocr_service:
                    page_blocks = _extract_ocr_text_blocks_from_service(page, page_num)
                else:
                    page_blocks = _extract_ocr_text_blocks_local(page, page_num)
                ocr_blocks.extend(page_blocks)
            except Exception as exc:
                logger.warning("OCR text extraction failed on page %d: %s", page_num, exc)
    finally:
        doc.close()

    return ocr_blocks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class PDFParseResult:
    """Container for lines and text blocks extracted from one PDF."""

    def __init__(self) -> None:
        self.lines: list[LineSegment] = []
        self.text_blocks: list[TextBlock] = []
        self.page_count: int = 0
        # Per-page MediaBox dimensions as list of (width, height)
        self.page_sizes: list[tuple[float, float]] = []

    def lines_on_page(self, page_num: int) -> list[LineSegment]:
        return [ln for ln in self.lines if ln.page == page_num]

    def text_on_page(self, page_num: int) -> list[TextBlock]:
        return [tb for tb in self.text_blocks if tb.page == page_num]


def parse_pdf(source: bytes | str | Path | BinaryIO) -> PDFParseResult:
    """
    Parse a PDF and return all vector line segments and text blocks.

    Parameters
    ----------
    source:
        Raw PDF bytes, a file path, or an open binary file object.
    """
    if isinstance(source, (str, Path)):
        doc = fitz.open(str(source))
    elif isinstance(source, bytes):
        doc = fitz.open(stream=source, filetype="pdf")
    else:
        data = source.read()
        doc = fitz.open(stream=data, filetype="pdf")

    result = PDFParseResult()
    result.page_count = len(doc)

    for page_num, page in enumerate(doc):
        rect = page.cropbox
        result.page_sizes.append((rect.width, rect.height))

        # --- Vector paths ---
        try:
            paths = page.get_drawings()
        except Exception:
            paths = []

        for path in paths:
            segs = _extract_lines_from_path(path, page_num)
            result.lines.extend(segs)

        # --- Text ---
        result.text_blocks.extend(_extract_text_blocks(page, page_num))

    doc.close()
    logger.info(
        "PDF parsed: %d pages, %d line segments, %d text blocks",
        result.page_count,
        len(result.lines),
        len(result.text_blocks),
    )
    return result
