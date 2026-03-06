"""
Duct Detector Service
=====================
Identifies duct candidates from the extracted line segments by finding
pairs of parallel lines that are:

  1. Nearly axis-aligned (horizontal or vertical within ±ANGLE_TOL degrees)
  2. Approximately the same length            (within LENGTH_TOL_RATIO)
  3. Spatially close together (gap within [MIN_GAP, MAX_GAP] points)
  4. Laterally overlapping                   (overlap > OVERLAP_RATIO)

Each matched pair becomes a DuctCandidate whose bounding box tightly
wraps both lines plus a small padding margin.

Nearby text blocks are attached to each candidate so downstream services
(GPT-4o) and the final annotation have access to dimension labels already
present in the drawing.
"""

from __future__ import annotations

import logging
import math
from typing import Iterable

from app.core.config import settings
from app.models.schemas import (
    BoundingBox,
    DuctCandidate,
    LineSegment,
    TextBlock,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuneable constants
# ---------------------------------------------------------------------------

ANGLE_TOL: float = 8.0       # degrees – how far off-axis a line may be
LENGTH_TOL_RATIO: float = 0.25  # lines must be within ±25 % of each other
MIN_GAP: float = settings.duct_min_gap  # points – minimum duct channel width
MAX_GAP: float = settings.duct_max_gap  # points – maximum duct channel width
OVERLAP_RATIO: float = 0.40    # fraction of shorter segment that must overlap
BBOX_PADDING: float = 6.0      # points – padding added around detected bbox
MIN_LINE_LENGTH: float = 16.0  # points – ignore tiny artefact lines
TEXT_SEARCH_MARGIN: float = 40.0  # points – how far outside bbox to look for text


# ---------------------------------------------------------------------------
# Geometry utilities
# ---------------------------------------------------------------------------

def _length(seg: LineSegment) -> float:
    return math.hypot(seg.x1 - seg.x0, seg.y1 - seg.y0)


def _angle_deg(seg: LineSegment) -> float:
    """Angle in [0, 180) degrees relative to positive-x axis."""
    dx = seg.x1 - seg.x0
    dy = seg.y1 - seg.y0
    angle = math.degrees(math.atan2(dy, dx)) % 180.0
    return angle


def _is_horizontal(seg: LineSegment) -> bool:
    a = _angle_deg(seg)
    return a <= ANGLE_TOL or a >= (180.0 - ANGLE_TOL)


def _is_vertical(seg: LineSegment) -> bool:
    a = _angle_deg(seg)
    return abs(a - 90.0) <= ANGLE_TOL


def _overlap_fraction(a0: float, a1: float, b0: float, b1: float) -> float:
    """Fraction of the shorter interval that overlaps the other."""
    lo = max(min(a0, a1), min(b0, b1))
    hi = min(max(a0, a1), max(b0, b1))
    overlap = max(0.0, hi - lo)
    shorter = min(abs(a1 - a0), abs(b1 - b0))
    if shorter < 1e-6:
        return 0.0
    return overlap / shorter


def _perpendicular_gap_h(la: LineSegment, lb: LineSegment) -> float:
    """Vertical gap between two horizontal segments (centre-to-centre approach)."""
    mid_ya = (la.y0 + la.y1) / 2.0
    mid_yb = (lb.y0 + lb.y1) / 2.0
    return abs(mid_ya - mid_yb)


def _perpendicular_gap_v(la: LineSegment, lb: LineSegment) -> float:
    """Horizontal gap between two vertical segments."""
    mid_xa = (la.x0 + la.x1) / 2.0
    mid_xb = (lb.x0 + lb.x1) / 2.0
    return abs(mid_xa - mid_xb)


def _bbox_from_pair(
    la: LineSegment, lb: LineSegment, page: int
) -> BoundingBox:
    xs = [la.x0, la.x1, lb.x0, lb.x1]
    ys = [la.y0, la.y1, lb.y0, lb.y1]
    return BoundingBox(
        x0=min(xs) - BBOX_PADDING,
        y0=min(ys) - BBOX_PADDING,
        x1=max(xs) + BBOX_PADDING,
        y1=max(ys) + BBOX_PADDING,
        page=page,
    )


def _nearby_text(
    bbox: BoundingBox, text_blocks: list[TextBlock]
) -> list[str]:
    """Collect text whose block intersects or is close to bbox."""
    margin = TEXT_SEARCH_MARGIN
    texts: list[str] = []
    for tb in text_blocks:
        if tb.page != bbox.page:
            continue
        if (
            tb.x1 >= bbox.x0 - margin
            and tb.x0 <= bbox.x1 + margin
            and tb.y1 >= bbox.y0 - margin
            and tb.y0 <= bbox.y1 + margin
        ):
            texts.append(tb.text)
    return texts


# ---------------------------------------------------------------------------
# Non-maximum suppression – remove heavily overlapping candidates
# ---------------------------------------------------------------------------

def _iou(a: BoundingBox, b: BoundingBox) -> float:
    ix0 = max(a.x0, b.x0)
    iy0 = max(a.y0, b.y0)
    ix1 = min(a.x1, b.x1)
    iy1 = min(a.y1, b.y1)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter == 0:
        return 0.0
    area_a = max(0.0, a.x1 - a.x0) * max(0.0, a.y1 - a.y0)
    area_b = max(0.0, b.x1 - b.x0) * max(0.0, b.y1 - b.y0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _nms(candidates: list[DuctCandidate], iou_thresh: float = 0.40) -> list[DuctCandidate]:
    """Simple greedy NMS – keep the wider candidate when two overlap."""
    keep: list[DuctCandidate] = []
    # Sort by gap width desc so the wider duct is preferred
    sorted_c = sorted(candidates, key=lambda c: c.gap_width, reverse=True)
    suppressed = set()
    for i, cand in enumerate(sorted_c):
        if i in suppressed:
            continue
        keep.append(cand)
        for j in range(i + 1, len(sorted_c)):
            if j in suppressed:
                continue
            if _iou(cand.bbox, sorted_c[j].bbox) > iou_thresh:
                suppressed.add(j)
    return keep


# ---------------------------------------------------------------------------
# Core detection logic
# ---------------------------------------------------------------------------

def _detect_on_page(
    lines: list[LineSegment],
    text_blocks: list[TextBlock],
    page_num: int,
    start_id: int,
) -> list[DuctCandidate]:
    """Find duct candidates on a single page."""

    # Partition lines by orientation
    h_lines = [s for s in lines if _is_horizontal(s) and _length(s) >= MIN_LINE_LENGTH]
    v_lines = [s for s in lines if _is_vertical(s) and _length(s) >= MIN_LINE_LENGTH]

    candidates: list[DuctCandidate] = []
    next_id = start_id

    def _process_pair(la: LineSegment, lb: LineSegment, orient: str, gap: float) -> None:
        nonlocal next_id
        bbox = _bbox_from_pair(la, lb, page_num)
        nearby = _nearby_text(bbox, text_blocks)
        candidates.append(
            DuctCandidate(
                id=next_id,
                bbox=bbox,
                line_a=la,
                line_b=lb,
                gap_width=gap,
                orientation=orient,
                nearby_text=nearby,
            )
        )
        next_id += 1

    # --- Horizontal pairs ---
    for i in range(len(h_lines)):
        for j in range(i + 1, len(h_lines)):
            la, lb = h_lines[i], h_lines[j]
            gap = _perpendicular_gap_h(la, lb)
            if not (MIN_GAP <= gap <= MAX_GAP):
                continue
            len_a, len_b = _length(la), _length(lb)
            if abs(len_a - len_b) / max(len_a, len_b) > LENGTH_TOL_RATIO:
                continue
            overlap = _overlap_fraction(
                min(la.x0, la.x1), max(la.x0, la.x1),
                min(lb.x0, lb.x1), max(lb.x0, lb.x1),
            )
            if overlap < OVERLAP_RATIO:
                continue
            _process_pair(la, lb, "horizontal", gap)

    # --- Vertical pairs ---
    for i in range(len(v_lines)):
        for j in range(i + 1, len(v_lines)):
            la, lb = v_lines[i], v_lines[j]
            gap = _perpendicular_gap_v(la, lb)
            if not (MIN_GAP <= gap <= MAX_GAP):
                continue
            len_a, len_b = _length(la), _length(lb)
            if abs(len_a - len_b) / max(len_a, len_b) > LENGTH_TOL_RATIO:
                continue
            overlap = _overlap_fraction(
                min(la.y0, la.y1), max(la.y0, la.y1),
                min(lb.y0, lb.y1), max(lb.y0, lb.y1),
            )
            if overlap < OVERLAP_RATIO:
                continue
            _process_pair(la, lb, "vertical", gap)

    return _nms(candidates)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_ducts(
    lines_by_page: dict[int, list[LineSegment]],
    text_by_page: dict[int, list[TextBlock]],
) -> list[DuctCandidate]:
    """
    Detect duct candidates across all pages.

    Parameters
    ----------
    lines_by_page:
        Mapping of page_number → list[LineSegment].
    text_by_page:
        Mapping of page_number → list[TextBlock].

    Returns
    -------
    Ordered list of DuctCandidate objects across all pages.
    """
    all_candidates: list[DuctCandidate] = []
    next_id = 0

    for page_num in sorted(lines_by_page.keys()):
        page_lines = lines_by_page[page_num]
        page_text = text_by_page.get(page_num, [])
        page_candidates = _detect_on_page(
            page_lines, page_text, page_num, next_id
        )
        next_id += len(page_candidates)
        all_candidates.extend(page_candidates)
        logger.info(
            "Page %d: %d duct candidates detected (%d h-lines, %d v-lines checked)",
            page_num,
            len(page_candidates),
            sum(1 for s in page_lines if _is_horizontal(s) and _length(s) >= MIN_LINE_LENGTH),
            sum(1 for s in page_lines if _is_vertical(s) and _length(s) >= MIN_LINE_LENGTH),
        )

    logger.info("Total duct candidates: %d", len(all_candidates))
    return all_candidates
