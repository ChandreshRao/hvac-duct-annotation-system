"""
Annotations Router
==================
POST /api/v1/annotate   – accepts a PDF upload and returns duct annotations
GET  /api/v1/health     – simple health/readiness probe
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections import defaultdict

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.core.config import settings
from app.models.schemas import (
    AnnotationResponse,
    DuctAnnotation,
    DuctBBox,
)
from app.services.duct_detector import detect_ducts
from app.services.gpt_analyzer import analyze_all_crops
from app.services.image_cropper import crop_all_candidates
from app.services.pdf_parser import parse_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["annotations"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_label(dim: str | None, pc: str | None, mat: str | None) -> str:
    """Build a human-readable annotation label from GPT-4o fields."""
    parts: list[str] = []
    if dim:
        parts.append(dim)
    if pc:
        parts.append(pc)
    if mat:
        parts.append(mat)
    return "  |  ".join(parts) if parts else "Unknown duct"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/health", summary="Health check")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post(
    "/annotate",
    response_model=AnnotationResponse,
    summary="Upload a PDF and receive duct annotations",
)
async def annotate_pdf(
    file: UploadFile = File(..., description="HVAC mechanical drawing in PDF format"),
) -> AnnotationResponse:
    """
    Pipeline:
    1. Validate and read the uploaded PDF.
    2. Extract vector lines and text blocks via PyMuPDF.
    3. Detect parallel-line duct candidates.
    4. Crop each candidate region as a PNG.
    5. Run rules-based text extraction; fall back to GPT-4o when needed.
    6. Return all duct annotations.
    """
    # ------------------------------------------------------------------
    # 1. Validate upload
    # ------------------------------------------------------------------
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF files are accepted.",
        )

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    pdf_bytes = await file.read()
    if len(pdf_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum size of {settings.max_upload_size_mb} MB.",
        )
    if not pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    # ------------------------------------------------------------------
    # 2. Parse PDF
    # ------------------------------------------------------------------
    try:
        parse_result = parse_pdf(pdf_bytes)
    except Exception as exc:
        logger.exception("PDF parsing failed")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"PDF parsing error: {exc}",
        ) from exc

    if parse_result.page_count == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PDF appears to contain no pages.",
        )

    # ------------------------------------------------------------------
    # 3. Detect duct candidates
    # ------------------------------------------------------------------
    lines_by_page: dict[int, list] = defaultdict(list)
    for seg in parse_result.lines:
        lines_by_page[seg.page].append(seg)

    text_by_page: dict[int, list] = defaultdict(list)
    for tb in parse_result.text_blocks:
        text_by_page[tb.page].append(tb)

    candidates = detect_ducts(
        dict(lines_by_page),
        dict(text_by_page),
    )

    if not candidates:
        logger.info("No duct candidates found in '%s'", file.filename)
        return AnnotationResponse(
            page_count=parse_result.page_count,
            duct_count=0,
            annotations=[],
        )

    # ------------------------------------------------------------------
    # 4. Crop candidate regions
    # ------------------------------------------------------------------
    crops = crop_all_candidates(pdf_bytes, candidates, dpi=settings.render_dpi)

    # ------------------------------------------------------------------
    # 5. Rules-based analysis with GPT fallback
    # ------------------------------------------------------------------
    candidates_by_id = {c.id: c for c in candidates}
    temp_pdf_path: str | None = None

    try:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".pdf", delete=False) as temp_pdf:
            temp_pdf.write(pdf_bytes)
            temp_pdf_path = temp_pdf.name

        gpt_results = await analyze_all_crops(
            crops,
            candidates_by_id,
            pdf_path=temp_pdf_path,
        )
    except Exception as exc:
        logger.exception("Duct analysis failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Duct analysis error: {exc}",
        ) from exc
    finally:
        if temp_pdf_path:
            try:
                os.remove(temp_pdf_path)
            except OSError as cleanup_exc:
                logger.warning(
                    "Could not remove temporary PDF '%s': %s",
                    temp_pdf_path,
                    cleanup_exc,
                )

    # ------------------------------------------------------------------
    # 6. Assemble annotations
    # ------------------------------------------------------------------
    annotations: list[DuctAnnotation] = []
    for cand in candidates:
        analysis = gpt_results.get(cand.id)
        dim = analysis.dimension if analysis else None
        pc = analysis.pressure_class if analysis else None
        mat = analysis.material if analysis else None
        conf = analysis.confidence if analysis else 0.0

        annotations.append(
            DuctAnnotation(
                id=cand.id,
                bbox=DuctBBox(
                    x0=cand.bbox.x0,
                    y0=cand.bbox.y0,
                    x1=cand.bbox.x1,
                    y1=cand.bbox.y1,
                    page=cand.bbox.page,
                ),
                label=_make_label(dim, pc, mat),
                pressure_class=pc,
                dimension=dim,
                material=mat,
                confidence=conf,
                orientation=cand.orientation,
            )
        )

    logger.info(
        "Annotation complete: %d ducts found in '%s'",
        len(annotations),
        file.filename,
    )
    return AnnotationResponse(
        page_count=parse_result.page_count,
        duct_count=len(annotations),
        annotations=annotations,
    )
