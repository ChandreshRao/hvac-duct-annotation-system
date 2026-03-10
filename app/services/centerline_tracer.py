"""
Centerline Tracer
=================
Given a duct dimension label's position (cx, cy) and orientation, finds the
vector centerline segment that passes through (or near) that point, then
extends it to T-intersection endpoints.

Coordinate space: derotated PDF points (same space as pdfToSvg() expects),
range roughly x: 0–page_width, y: 0–page_height.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Optional

from app.models.schemas import LineSegment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
SNAP_PX: float = 50.0        # max distance from label centre to candidate line
COLLINEAR_TOL: float = 5.0   # max off-axis deviation for "same" line
PERP_BAND: float = 10.0      # width of search band for perp-crossing detection
MIN_SEG_LEN: float = 15.0    # ignore tiny drawing artifacts
EXTEND_LIMIT: float = 3000.0 # absolute max extension (full page diagonal)

# ---------------------------------------------------------------------------
# Flexible round-duct label pattern
# Matches any 1–3 digit number optionally followed by:
#   inch-mark variants: " ' ° º
#   phi variants:       ⌀ ∅ Ø ø Φ @
#   OCR artifact zero:  "0  '0  (often OCR misread for ⌀)
# ⌀ is allowed to be absent entirely — inch-mark alone is sufficient.
# ---------------------------------------------------------------------------
_ROUND_DUCT_LABEL_RE = re.compile(
    r"""
    (?<![,\d])                        # not preceded by digit/comma
    (?P<size>\d{1,3}(?:\.\d+)?)       # size: 1–3 digits (int or decimal)
    \s*
    (?:
        [\"\'°º]?                     # optional inch mark
        \s*
        [⌀∅ØøΦ@]                     # phi or @ variant
        |
        [\"\'°º]                      # inch mark (no phi needed)
        (?:\s*0)?                     # optional OCR '0' (phi artifact)
    )
    (?!\d)                            # not followed by digit
    """,
    re.VERBOSE | re.UNICODE,
)


def is_round_duct_label(label: str) -> bool:
    """Return True if label looks like a round duct dimension."""
    return bool(_ROUND_DUCT_LABEL_RE.search(label))


def extract_round_duct_size(label: str) -> Optional[float]:
    """Extract numeric diameter from a round duct label; return None if no match."""
    m = _ROUND_DUCT_LABEL_RE.search(label)
    if not m:
        return None
    try:
        return float(m.group("size"))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------

def _seg_length(seg: LineSegment) -> float:
    return math.hypot(seg.x1 - seg.x0, seg.y1 - seg.y0)


def _seg_midpoint(seg: LineSegment) -> tuple[float, float]:
    return (seg.x0 + seg.x1) / 2.0, (seg.y0 + seg.y1) / 2.0


def _is_horizontal(seg: LineSegment, tol: float = COLLINEAR_TOL) -> bool:
    return abs(seg.y1 - seg.y0) <= tol


def _is_vertical(seg: LineSegment, tol: float = COLLINEAR_TOL) -> bool:
    return abs(seg.x1 - seg.x0) <= tol


def _distance_point_to_segment_axis(
    px: float, py: float, seg: LineSegment, axis: str
) -> float:
    """
    Perpendicular distance from point (px, py) to the infinite LINE containing seg,
    measuring only along the perpendicular axis.
      axis="horizontal" → distance measured on y axis
      axis="vertical"   → distance measured on x axis
    """
    if axis == "horizontal":
        seg_y = (seg.y0 + seg.y1) / 2.0
        return abs(py - seg_y)
    else:
        seg_x = (seg.x0 + seg.x1) / 2.0
        return abs(px - seg_x)


def _point_on_segment_range(
    px: float, py: float, seg: LineSegment, axis: str, margin: float = SNAP_PX
) -> bool:
    """Return True if point projects *within* the segment's primary-axis range."""
    if axis == "horizontal":
        lo, hi = min(seg.x0, seg.x1) - margin, max(seg.x0, seg.x1) + margin
        return lo <= px <= hi
    else:
        lo, hi = min(seg.y0, seg.y1) - margin, max(seg.y0, seg.y1) + margin
        return lo <= py <= hi


# ---------------------------------------------------------------------------
# Step 1 – find the nearest collinear segment to the label
# ---------------------------------------------------------------------------

def find_passing_line(
    cx: float,
    cy: float,
    segments: list[LineSegment],
    axis: str,                # "horizontal" | "vertical"
    snap_px: float = SNAP_PX,
) -> Optional[LineSegment]:
    """
    Return the segment whose axis-line passes closest to (cx, cy) AND is
    aligned with `axis`, AND (cx/cy) falls within the segment's primary range.
    Returns None if no candidate within `snap_px`.
    """
    best_seg: Optional[LineSegment] = None
    best_dist = snap_px

    for seg in segments:
        if _seg_length(seg) < MIN_SEG_LEN:
            continue
        if axis == "horizontal" and not _is_horizontal(seg):
            continue
        if axis == "vertical" and not _is_vertical(seg):
            continue

        dist = _distance_point_to_segment_axis(cx, cy, seg, axis)
        if dist > best_dist:
            continue
        if not _point_on_segment_range(cx, cy, seg, axis, margin=snap_px):
            continue

        best_dist = dist
        best_seg = seg

    return best_seg


# ---------------------------------------------------------------------------
# Step 2 – collect all collinear fragments on the same axis-line
# ---------------------------------------------------------------------------

def _segments_on_same_axis_line(
    ref_seg: LineSegment,
    all_segments: list[LineSegment],
    axis: str,
    tol: float = COLLINEAR_TOL,
) -> list[LineSegment]:
    """
    Return all segments (including ref_seg) that lie on the same infinite
    axis-line as ref_seg.
    """
    if axis == "horizontal":
        ref_axis_val = (ref_seg.y0 + ref_seg.y1) / 2.0
    else:
        ref_axis_val = (ref_seg.x0 + ref_seg.x1) / 2.0

    result: list[LineSegment] = []
    for seg in all_segments:
        if _seg_length(seg) < MIN_SEG_LEN:
            continue
        if axis == "horizontal" and not _is_horizontal(seg, tol):
            continue
        if axis == "vertical" and not _is_vertical(seg, tol):
            continue

        if axis == "horizontal":
            seg_axis_val = (seg.y0 + seg.y1) / 2.0
        else:
            seg_axis_val = (seg.x0 + seg.x1) / 2.0

        if abs(seg_axis_val - ref_axis_val) <= tol:
            result.append(seg)

    return result


# ---------------------------------------------------------------------------
# Step 3 – find T-intersection (perpendicular crossing) at a given position
# ---------------------------------------------------------------------------

def _find_nearest_perp_crossing(
    pos: float,          # position along primary axis where we are
    axis_val: float,     # the fixed axis coordinate (y for horiz, x for vert)
    all_segments: list[LineSegment],
    axis: str,
    direction: int,      # +1 or -1 (which direction we're extending)
    band_px: float = PERP_BAND,
    start_pos: float = 0.0,
    limit: float = EXTEND_LIMIT,
) -> float:
    """
    Walk from `pos` in `direction` along the primary axis.
    Return the position of the nearest perpendicular segment crossing the
    current axis-line, or pos ± limit if none found.
    """
    best_cross_pos = pos + direction * limit

    for seg in all_segments:
        if _seg_length(seg) < MIN_SEG_LEN:
            continue

        # Only perpendicular segments
        if axis == "horizontal":
            if not _is_vertical(seg, tol=COLLINEAR_TOL):
                continue
            seg_primary_pos = (seg.x0 + seg.x1) / 2.0   # x pos of vert seg
            seg_axis_range  = (min(seg.y0, seg.y1), max(seg.y0, seg.y1))

            # Must span across our axis line
            if not (seg_axis_range[0] - band_px <= axis_val <= seg_axis_range[1] + band_px):
                continue
        else:  # vertical duct
            if not _is_horizontal(seg, tol=COLLINEAR_TOL):
                continue
            seg_primary_pos = (seg.y0 + seg.y1) / 2.0   # y pos of horiz seg
            seg_axis_range  = (min(seg.x0, seg.x1), max(seg.x0, seg.x1))

            if not (seg_axis_range[0] - band_px <= axis_val <= seg_axis_range[1] + band_px):
                continue

        # Must be in our direction and further than start
        if direction > 0:
            if seg_primary_pos <= pos + 1.0:
                continue
        else:
            if seg_primary_pos >= pos - 1.0:
                continue

        if abs(seg_primary_pos - pos) < abs(best_cross_pos - pos):
            best_cross_pos = seg_primary_pos

    return best_cross_pos


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def trace_from_label(
    label: str,
    cx: float,
    cy: float,
    orientation: str,          # "horizontal" | "vertical"
    page_segments: list[LineSegment],
    snap_px: float = SNAP_PX,
    extend_limit: float = EXTEND_LIMIT,
) -> Optional[dict[str, float]]:
    """
    Given a duct label's centre coordinates and orientation, find the vector
    centerline that passes through (or near) that point and trace it to both
    T-intersection endpoints.

    Returns {"x1", "y1", "x2", "y2"} or None if no centerline found.
    """
    axis = orientation  # "horizontal" or "vertical"

    # 1. Find the segment passing closest to the label centre
    ref_seg = find_passing_line(cx, cy, page_segments, axis, snap_px)
    if ref_seg is None:
        logger.debug("trace_from_label: no passing line for '%s' at (%.1f, %.1f) axis=%s",
                     label, cx, cy, axis)
        return None

    # 2. Gather all collinear fragments
    collinear = _segments_on_same_axis_line(ref_seg, page_segments, axis)

    # Fixed axis coordinate (y for horizontal, x for vertical)
    if axis == "horizontal":
        axis_val = (ref_seg.y0 + ref_seg.y1) / 2.0
        # Primary positions: x values of all segment endpoints
        primary_positions = [seg.x0 for seg in collinear] + [seg.x1 for seg in collinear]
    else:
        axis_val = (ref_seg.x0 + ref_seg.x1) / 2.0
        primary_positions = [seg.y0 for seg in collinear] + [seg.y1 for seg in collinear]

    if not primary_positions:
        return None

    segment_min = min(primary_positions)
    segment_max = max(primary_positions)

    # 3. Find T-intersections in both directions from the label's primary position
    label_primary = cx if axis == "horizontal" else cy

    # Extend leftward / upward from segment_min
    p_lo = _find_nearest_perp_crossing(
        pos=segment_min,
        axis_val=axis_val,
        all_segments=page_segments,
        axis=axis,
        direction=-1,
        band_px=PERP_BAND,
        limit=extend_limit,
    )

    # Extend rightward / downward from segment_max
    p_hi = _find_nearest_perp_crossing(
        pos=segment_max,
        axis_val=axis_val,
        all_segments=page_segments,
        axis=axis,
        direction=+1,
        band_px=PERP_BAND,
        limit=extend_limit,
    )

    # Clamp to within a reasonable distance of the label
    max_extend = extend_limit
    if abs(p_lo - label_primary) > max_extend:
        p_lo = label_primary - max_extend
    if abs(p_hi - label_primary) > max_extend:
        p_hi = label_primary + max_extend

    if axis == "horizontal":
        return {"x1": p_lo, "y1": axis_val, "x2": p_hi, "y2": axis_val}
    else:
        return {"x1": axis_val, "y1": p_lo, "x2": axis_val, "y2": p_hi}
