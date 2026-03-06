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


def _extract_text_blocks(page: fitz.Page, page_num: int) -> list[TextBlock]:
    """Extract every text span on the page with its bounding rectangle."""
    blocks: list[TextBlock] = []
    raw = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
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
                    )
                )
    return blocks


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
        rect = page.rect
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
