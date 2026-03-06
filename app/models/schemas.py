"""
Pydantic models for HVAC duct annotation system.
"""

from typing import Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-models produced by GPT-4o vision analysis
# ---------------------------------------------------------------------------

class GPTDuctAnalysis(BaseModel):
    """Raw response from GPT-4o for a single duct crop."""

    dimension: Optional[str] = Field(
        None,
        description="Duct dimension string, e.g. '24x12' or '18\" dia'",
    )
    pressure_class: Optional[str] = Field(
        None,
        description="Pressure class, e.g. '0.5\"wg', '1\"wg', '2\"wg'",
    )
    material: Optional[str] = Field(
        None,
        description="Duct material, e.g. 'galvanized steel', 'flexible', 'fiberglass'",
    )
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="Model confidence score between 0 and 1",
    )


# ---------------------------------------------------------------------------
# Geometry primitives extracted from PDF
# ---------------------------------------------------------------------------

class LineSegment(BaseModel):
    """A single line segment extracted from PDF vector paths."""

    x0: float
    y0: float
    x1: float
    y1: float
    page: int = 0


class TextBlock(BaseModel):
    """A text block extracted from a PDF page."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int = 0


class BoundingBox(BaseModel):
    """Axis-aligned bounding box in PDF user-space points."""

    x0: float
    y0: float
    x1: float
    y1: float
    page: int = 0

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


# ---------------------------------------------------------------------------
# Duct candidate (detector output)
# ---------------------------------------------------------------------------

class DuctCandidate(BaseModel):
    """A detected duct candidate from parallel-line analysis."""

    id: int
    bbox: BoundingBox
    line_a: LineSegment
    line_b: LineSegment
    gap_width: float = Field(..., description="Perpendicular distance between the two parallel lines")
    orientation: str = Field(..., description="'horizontal' or 'vertical'")
    nearby_text: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Final annotation returned to the frontend
# ---------------------------------------------------------------------------

class DuctBBox(BaseModel):
    """Frontend-friendly bounding box with page-relative pixel coordinates."""

    x0: float
    y0: float
    x1: float
    y1: float
    page: int


class DuctAnnotation(BaseModel):
    """Single duct annotation as returned by the /annotate endpoint."""

    id: int
    bbox: DuctBBox
    label: str = Field(..., description="Human-readable label, e.g. '24x12 – 1\"wg'")
    pressure_class: Optional[str] = None
    dimension: Optional[str] = None
    material: Optional[str] = None
    confidence: float = 0.0
    orientation: str = "unknown"


class AnnotationResponse(BaseModel):
    """Top-level response envelope for the /annotate endpoint."""

    page_count: int
    duct_count: int
    annotations: list[DuctAnnotation]
