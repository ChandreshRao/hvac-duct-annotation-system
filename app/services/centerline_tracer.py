import math
import re
from typing import Any
from app.models.schemas import LineSegment

# Flexible Regex for Round Duct Labels:
# Matches: 18", 18, 18⌀, ⌀18, 18-inch, 18 ⌀, 18"0 (OCR error)
_ROUND_DUCT_LABEL_RE = re.compile(
    r'(?:[⌀∅ΦøØ0]\s*)?(\d{1,3})(?:\s*(?:["\'”\u2033]|inch|in))?(?:\s*[⌀∅ΦøØ0])?', 
    re.IGNORECASE
)

def is_round_duct_label(text: str) -> bool:
    """Checks if text likely represents a round duct size (not a rectangular 20x10)."""
    text = text.strip()
    if 'x' in text.lower() or '*' in text:
        return False
    return bool(_ROUND_DUCT_LABEL_RE.search(text))

def extract_round_duct_size(text: str) -> str | None:
    match = _ROUND_DUCT_LABEL_RE.search(text)
    if match:
        return f"{match.group(1)}⌀"
    return None

def find_passing_line(
    cx: float, cy: float, 
    segments: list[LineSegment], 
    is_horizontal: bool,
    snap_px: float = 40.0
) -> LineSegment | None:
    """Finds the segment that passes nearest to (cx, cy) along the correct axis."""
    best_seg = None
    min_dist = snap_px
    
    for seg in segments:
        if seg.is_horizontal != is_horizontal:
            continue
            
        # Distance from point to infinite line
        if is_horizontal:
            # For horizontal line, distance is vertical offset
            dist = abs(cy - (seg.y0 + seg.y1)/2)
            # Check if point is roughly within the x-span (with some margin)
            if dist < min_dist and (min(seg.x0, seg.x1) - 50 <= cx <= max(seg.x0, seg.x1) + 50):
                min_dist = dist
                best_seg = seg
        else:
            # For vertical line, distance is horizontal offset
            dist = abs(cx - (seg.x0 + seg.x1)/2)
            if dist < min_dist and (min(seg.y0, seg.y1) - 50 <= cy <= max(seg.y0, seg.y1) + 50):
                min_dist = dist
                best_seg = seg
                
    return best_seg

def extend_to_intersections(
    seg: LineSegment, 
    all_segments: list[LineSegment], 
    limit: float = 2000.0
) -> dict[str, float]:
    """Extends a segment in both directions until it hits a perpendicular line."""
    x0, y0, x1, y1 = seg.x0, seg.y0, seg.x1, seg.y1
    is_horiz = seg.is_horizontal
    
    # Sort endpoints
    if is_horiz:
        p_start, p_end = (min(x0, x1), (y0+y1)/2), (max(x0, x1), (y0+y1)/2)
    else:
        p_start, p_end = ((x0+x1)/2, min(y0, y1)), ((x0+x1)/2, max(y0, y1))

    def find_stop(pos: tuple[float, float], direction: int) -> float:
        # direction: -1 (left/up), 1 (right/down)
        curr_val = pos[0] if is_horiz else pos[1]
        const_val = pos[1] if is_horiz else pos[0]
        
        best_stop = curr_val + (direction * limit)
        
        for s in all_segments:
            if s.is_horizontal == is_horiz:
                continue # Only stop at perpendiculars
                
            if is_horiz:
                # Stopping at vertical line s
                sx = (s.x0 + s.x1) / 2
                sy_min, sy_max = min(s.y0, s.y1), max(s.y0, s.y1)
                # Does vertical line s cross our y level?
                if sy_min - 2 <= const_val <= sy_max + 2:
                    if direction == 1 and curr_val < sx < best_stop:
                        best_stop = sx
                    elif direction == -1 and best_stop < sx < curr_val:
                        best_stop = sx
            else:
                # Stopping at horizontal line s
                sy = (s.y0 + s.y1) / 2
                sx_min, sx_max = min(s.x0, s.x1), max(s.x0, s.x1)
                # Does horizontal line s cross our x level?
                if sx_min - 2 <= const_val <= sx_max + 2:
                    if direction == 1 and curr_val < sy < best_stop:
                        best_stop = sy
                    elif direction == -1 and best_stop < sy < curr_val:
                        best_stop = sy
        return best_stop

    if is_horiz:
        x_min = find_stop(p_start, -1)
        x_max = find_stop(p_end, 1)
        return {"x1": x_min, "y1": p_start[1], "x2": x_max, "y2": p_end[1]}
    else:
        y_min = find_stop(p_start, -1)
        y_max = find_stop(p_end, 1)
        return {"x1": p_start[0], "y1": y_min, "x2": p_end[0], "y2": y_max}

def trace_from_label(
    label: str,
    cx: float, cy: float,
    orientation: str,
    page_segments: list[Any], # list of dict or LineSegment
) -> dict[str, float] | None:
    """Main entry point for annotation pipeline."""
    # Convert dicts to LineSegment if needed
    segs = []
    for s in page_segments:
        if isinstance(s, dict):
            # Map dict x1,y1,x2,y2 to schema x0,y0,x1,y1
            segs.append(LineSegment(x0=s['x1'], y0=s['y1'], x1=s['x2'], y1=s['y2']))
        else:
            segs.append(s)
            
    is_horiz = orientation.lower() == "horizontal"
    
    passing = find_passing_line(cx, cy, segs, is_horiz)
    if not passing:
        return None
        
    return extend_to_intersections(passing, segs)
