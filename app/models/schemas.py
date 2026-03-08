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
    source: str = "embedded"
    normalized_text: Optional[str] = None
    normalized_label: Optional[str] = None
    normalized_variants: list[str] = Field(default_factory=list)


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
    """A detected duct candidate from parallel-line analysis or single centerline."""

    id: int
    bbox: BoundingBox
    line_a: LineSegment
    line_b: Optional[LineSegment] = None
    gap_width: float = Field(0.0, description="Perpendicular distance between the two parallel lines (0 for single line)")
    orientation: str = Field(..., description="'horizontal' or 'vertical' or 'diagonal'")
    nearby_text: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Final annotation returned to the frontend
# ---------------------------------------------------------------------------

from typing import Optional, Union, Dict

class DuctBBox(BaseModel):
    """Frontend-friendly bounding box with page-relative pixel coordinates."""

    x0: float
    y0: float
    x1: float
    y1: float
    page: int


class DuctAnnotation(BaseModel):
    """Single duct annotation as returned by the /annotate endpoint."""

    id: Union[int, str]
    bbox: DuctBBox
    label: str = Field(..., description="Human-readable label, e.g. '24x12 – 1\"wg'")
    pressure_class: Optional[str] = None
    dimension: Optional[str] = None
    material: Optional[str] = None
    confidence: float = 0.0
    orientation: str = "unknown"
    source: Optional[str] = None
    line: Optional[Dict[str, float]] = None


class AnnotationResponse(BaseModel):
    """Top-level response envelope for the /annotate endpoint."""

    document_id: Optional[str] = None
    document_name: Optional[str] = None
    page_count: int
    duct_count: int
    annotations: list[DuctAnnotation]


class PDFPageSize(BaseModel):
    """Page dimensions (PDF points) for one page index."""

    page: int
    width: float
    height: float


class PDFTextResponse(BaseModel):
    """Top-level response envelope for the /texts endpoint."""

    page_count: int
    text_count: int
    page_sizes: list[PDFPageSize]
    texts: list[TextBlock]


class ManualAnnotationPayload(BaseModel):
    """Payload for one manually corrected annotation."""

    bbox: DuctBBox
    label: str
    pressure_class: Optional[str] = None
    dimension: Optional[str] = None
    material: Optional[str] = None
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    orientation: str = "manual"
    source: Optional[str] = "manual"
    line: Optional[Dict[str, float]] = None


class ManualAnnotationCreateRequest(BaseModel):
    """Request body for saving one manual annotation."""

    document_id: str = Field(..., min_length=1)
    document_name: Optional[str] = None
    annotation: ManualAnnotationPayload


class ManualAnnotationBulkCreateRequest(BaseModel):
    """Request body for bulk saving all annotations for a document, overwriting existing."""
    
    document_id: str = Field(..., min_length=1)
    document_name: Optional[str] = None
    annotations: list[ManualAnnotationPayload]


class ManualAnnotationUpdateRequest(BaseModel):
    """Request body for updating one saved manual annotation."""

    annotation: ManualAnnotationPayload


class ManualAnnotationRecord(BaseModel):
    """Persisted manual annotation row returned by API."""

    id: int
    document_id: str
    document_name: Optional[str] = None
    bbox: DuctBBox
    label: str
    pressure_class: Optional[str] = None
    dimension: Optional[str] = None
    material: Optional[str] = None
    confidence: float = 1.0
    orientation: str = "manual"
    source: str = "manual"
    line: Optional[Dict[str, float]] = None
    created_at: str
    updated_at: str


class ManualAnnotationListResponse(BaseModel):
    """Top-level response envelope for saved manual annotations of one document."""

    document_id: str
    count: int
    annotations: list[ManualAnnotationRecord]


class ManualAnnotationDeleteResponse(BaseModel):
    """Response envelope for deleting one saved manual annotation."""

    id: int
    deleted: bool
