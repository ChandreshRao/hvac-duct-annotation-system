"""
Annotations Router
==================
POST /api/v1/annotate   – accepts a PDF upload and returns duct annotations
GET  /api/v1/health     – simple health/readiness probe
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
from collections import defaultdict

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.core.config import settings
from app.services.centerline_tracer import is_round_duct_label, trace_from_label
from app.models.schemas import (
    AnnotationResponse,
    DuctAnnotation,
    DuctBBox,
    ManualAnnotationCreateRequest,
    ManualAnnotationDeleteResponse,
    ManualAnnotationListResponse,
    ManualAnnotationRecord,
    ManualAnnotationUpdateRequest,
    ManualAnnotationBulkCreateRequest,
    PDFPageSize,
    PDFTextResponse,
)
from app.services.duct_detector import detect_ducts
from app.services.gpt_analyzer import analyze_all_crops
from app.services.image_cropper import crop_all_candidates
from app.services.manual_annotation_store import (
    delete_manual_annotation as delete_manual_annotation_from_store,
)
from app.services.manual_annotation_store import (
    list_manual_annotations as list_manual_annotations_from_store,
)
from app.services.manual_annotation_store import (
    save_manual_annotation as save_manual_annotation_to_store,
)
from app.services.manual_annotation_store import (
    update_manual_annotation as update_manual_annotation_in_store,
)
from app.services.manual_annotation_store import (
    replace_document_annotations as replace_document_annotations_in_store,
)
from app.services.pdf_parser import extract_ocr_text_blocks, parse_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["annotations"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_label(dim: str | None, pc: str | None, mat: str | None) -> str:
    """Build a human-readable annotation label from GPT-4o fields."""
    if dim:
        return dim
    return "Unknown duct"


def _bbox_center(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float]:
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _is_near_existing_dimension(
    annotations: list[DuctAnnotation],
    dimension: str,
    bbox: tuple[float, float, float, float],
    distance_threshold: float = 90.0,
) -> bool:
    cx, cy = _bbox_center(*bbox)
    normalized_dimension = dimension.strip().upper()

    for ann in annotations:
        ann_dimension = (ann.dimension or "").strip().upper()
        if ann_dimension != normalized_dimension:
            continue
        ax, ay = _bbox_center(ann.bbox.x0, ann.bbox.y0, ann.bbox.x1, ann.bbox.y1)
        if math.hypot(cx - ax, cy - ay) <= distance_threshold:
            return True

    return False


async def _read_pdf_upload(file: UploadFile) -> bytes:
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

    return pdf_bytes


def _parse_pdf_or_422(pdf_bytes: bytes):
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

    return parse_result


def _dedupe_text_blocks(items: list) -> list:
    deduped: list = []
    seen: set[tuple[int, str, float, float, float, float]] = set()

    for item in items:
        normalized_text = " ".join(str(item.text).split()).lower()
        key = (
            int(item.page),
            normalized_text,
            round(float(item.x0), 1),
            round(float(item.y0), 1),
            round(float(item.x1), 1),
            round(float(item.y1), 1),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


def _normalize_text_token(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    normalized = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("′", "'")
        .replace("″", '"')
        .replace("’", "'")
        .replace("°", '"')
        .replace("º", '"')
        .replace("×", "X")
        .replace("*", "X")
        .replace("–", "-")
        .replace("—", "-")
        .replace("∅", "⌀")
        .replace("Ø", "⌀")
        .replace("ø", "⌀")
        .replace("Φ", "⌀")
        .replace("@", "⌀")
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _is_plausible_round_size(value: float) -> bool:
    return 3.0 <= value <= 96.0


def _is_plausible_rect_dims(values: list[float]) -> bool:
    if len(values) < 2:
        return False
    if any(v < 3.0 or v > 96.0 for v in values):
        return False
    ratio = max(values) / max(0.1, min(values))
    return ratio <= 12.0


def _infer_normalized_label(value: str) -> str | None:
    token = _normalize_text_token(value).upper().replace(" ", "")
    if not token:
        return None

    rect = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)[X](\d{1,3}(?:\.\d+)?)(?:[X](\d{1,3}(?:\.\d+)?))?", token)
    if rect:
        dims = [float(group) for group in rect.groups() if group is not None]
        if _is_plausible_rect_dims(dims):
            return "X".join(str(int(v)) if float(v).is_integer() else str(v) for v in dims)

    round_with_dia = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)[\"']?⌀", token)
    if round_with_dia:
        diameter = float(round_with_dia.group(1))
        if _is_plausible_round_size(diameter):
            return f"{int(diameter) if diameter.is_integer() else diameter}⌀"

    round_ocr_zero = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)[\"']0", token)
    if round_ocr_zero:
        diameter = float(round_ocr_zero.group(1))
        if _is_plausible_round_size(diameter):
            return f"{int(diameter) if diameter.is_integer() else diameter}⌀"

    round_prefix = re.fullmatch(r"⌀(\d{1,3}(?:\.\d+)?)[\"']?", token)
    if round_prefix:
        diameter = float(round_prefix.group(1))
        if _is_plausible_round_size(diameter):
            return f"{int(diameter) if diameter.is_integer() else diameter}⌀"

    diameter_word = re.fullmatch(r"(?:DIA|DIAMETER)(\d{1,3}(?:\.\d+)?)", token)
    if diameter_word:
        diameter = float(diameter_word.group(1))
        if _is_plausible_round_size(diameter):
            return f"{int(diameter) if diameter.is_integer() else diameter}⌀"

    # OCR-specific compact artifact seen in this drawing set, e.g. "140.04" -> "14⌀"
    ocr_compact_round = re.fullmatch(r"(\d{2})0[\.,]0?4", token)
    if ocr_compact_round:
        diameter = float(ocr_compact_round.group(1))
        if _is_plausible_round_size(diameter):
            return f"{int(diameter)}⌀"

    return None


def _label_variants(normalized_label: str | None) -> list[str]:
    if not normalized_label:
        return []

    variants: list[str] = [normalized_label]
    round_match = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)⌀", normalized_label)
    if round_match:
        size = round_match.group(1)
        variants.extend(
            [
                f'{size}"Ø',
                f'{size}"ø',
                f'{size}"⌀',
                f'Ø{size}',
                f'ø{size}',
                f'⌀{size}',
                f'{size}"@',
            ]
        )

    deduped: list[str] = []
    seen: set[str] = set()
    for item in variants:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _with_normalized_fields(items: list) -> list:
    enriched: list[dict] = []
    for item in items:
        enriched.append(
            {
                "item": item,
                "normalized_text": _normalize_text_token(item.text),
                "normalized_label": _infer_normalized_label(item.text),
            }
        )

    for entry in enriched:
        if entry["normalized_label"]:
            continue

        token = str(entry["normalized_text"]).upper().replace(" ", "")
        token = re.sub(r"^[^0-9]+|[^0-9]+$", "", token)
        compact_round = re.fullmatch(r"(\d{1,2})0", token)
        if not compact_round:
            continue

        diameter = float(compact_round.group(1))
        if not _is_plausible_round_size(diameter):
            continue

        item = entry["item"]
        cx = (float(item.x0) + float(item.x1)) / 2.0
        cy = (float(item.y0) + float(item.y1)) / 2.0

        has_round_context = False
        for other in enriched:
            if other is entry:
                continue
            other_item = other["item"]
            if int(other_item.page) != int(item.page):
                continue

            other_text = str(other.get("normalized_text", "")).strip()
            if not other_text:
                continue

            round_hint = (
                other_text in {'"', "'", "⌀"}
                or "⌀" in other_text
                or "DIA" in other_text.upper()
            )
            if not round_hint:
                continue

            ocx = (float(other_item.x0) + float(other_item.x1)) / 2.0
            ocy = (float(other_item.y0) + float(other_item.y1)) / 2.0
            if math.hypot(ocx - cx, ocy - cy) <= 20.0:
                has_round_context = True
                break

        if has_round_context:
            entry["normalized_label"] = f"{int(diameter)}⌀"

    normalized_items: list = []
    for entry in enriched:
        item = entry["item"]
        normalized_items.append(
            item.model_copy(
                update={
                    "normalized_text": entry["normalized_text"],
                    "normalized_label": entry["normalized_label"],
                    "normalized_variants": _label_variants(entry["normalized_label"]),
                }
            )
        )

    return normalized_items


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/health", summary="Health check")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post(
    "/manual-annotations",
    response_model=ManualAnnotationRecord,
    summary="Save one manual annotation correction",
)
async def create_manual_annotation(
    request: ManualAnnotationCreateRequest,
) -> ManualAnnotationRecord:
    try:
        record = save_manual_annotation_to_store(
            document_id=request.document_id,
            document_name=request.document_name,
            annotation=request.annotation,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Manual annotation save failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Manual annotation save error: {exc}",
        ) from exc

    logger.info(
        "Saved manual annotation id=%s document_id=%s page=%s label=%s",
        record.id,
        record.document_id,
        record.bbox.page,
        record.label,
    )
    return record


@router.put(
    "/manual-annotations/{annotation_id}",
    response_model=ManualAnnotationRecord,
    summary="Update one saved manual annotation",
)
async def update_manual_annotation(
    annotation_id: int,
    request: ManualAnnotationUpdateRequest,
) -> ManualAnnotationRecord:
    try:
        record = update_manual_annotation_in_store(annotation_id, request.annotation)
    except Exception as exc:
        logger.exception("Manual annotation update failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Manual annotation update error: {exc}",
        ) from exc

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manual annotation id={annotation_id} not found",
        )

    logger.info(
        "Updated manual annotation id=%s document_id=%s page=%s label=%s",
        record.id,
        record.document_id,
        record.bbox.page,
        record.label,
    )
    return record


@router.delete(
    "/manual-annotations/{annotation_id}",
    response_model=ManualAnnotationDeleteResponse,
    summary="Delete one saved manual annotation",
)
async def delete_manual_annotation(annotation_id: int) -> ManualAnnotationDeleteResponse:
    try:
        deleted = delete_manual_annotation_from_store(annotation_id)
    except Exception as exc:
        logger.exception("Manual annotation delete failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Manual annotation delete error: {exc}",
        ) from exc

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manual annotation ID {annotation_id} not found.",
        )
    return ManualAnnotationDeleteResponse(id=annotation_id, deleted=True)


@router.post(
    "/manual-annotations/bulk",
    response_model=ManualAnnotationListResponse,
    summary="Bulk replace manual annotations for a document",
)
def bulk_save_manual_annotations(request: ManualAnnotationBulkCreateRequest) -> ManualAnnotationListResponse:
    records = replace_document_annotations_in_store(
        document_id=request.document_id,
        document_name=request.document_name,
        annotations=request.annotations,
    )
    return ManualAnnotationListResponse(
        document_id=request.document_id,
        count=len(records),
        annotations=records,
    )


@router.get(
    "/manual-annotations/{document_id}",
    response_model=ManualAnnotationListResponse,
    summary="List saved manual annotations for a document",
)
async def get_manual_annotations(document_id: str) -> ManualAnnotationListResponse:
    records = list_manual_annotations_from_store(document_id)
    return ManualAnnotationListResponse(
        document_id=document_id,
        count=len(records),
        annotations=records,
    )


@router.post(
    "/texts",
    response_model=PDFTextResponse,
    summary="Upload a PDF and receive all extracted text with coordinates",
)
async def extract_pdf_texts(
    file: UploadFile = File(..., description="HVAC mechanical drawing in PDF format"),
    include_ocr: bool = True,
    include_normalized: bool = True,
) -> PDFTextResponse:
    pdf_bytes = await _read_pdf_upload(file)
    parse_result = _parse_pdf_or_422(pdf_bytes)

    text_blocks = list(parse_result.text_blocks)
    ocr_count = 0
    if include_ocr:
        ocr_blocks = extract_ocr_text_blocks(pdf_bytes)
        ocr_count = len(ocr_blocks)
        text_blocks.extend(ocr_blocks)

    texts = _dedupe_text_blocks(text_blocks)
    texts.sort(key=lambda item: (item.page, item.y0, item.x0))

    if include_normalized:
        texts = _with_normalized_fields(texts)

    page_sizes = [
        PDFPageSize(page=index, width=size[0], height=size[1])
        for index, size in enumerate(parse_result.page_sizes)
    ]

    logger.info(
        "Text extraction endpoint: %d text spans (%d embedded + %d ocr, include_normalized=%s) across %d pages in '%s'",
        len(texts),
        len(parse_result.text_blocks),
        ocr_count,
        include_normalized,
        parse_result.page_count,
        file.filename,
    )
    return PDFTextResponse(
        page_count=parse_result.page_count,
        text_count=len(texts),
        page_sizes=page_sizes,
        texts=texts,
    )


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
    if os.getenv("USE_HARDCODED_RESPONSE_API", "false").lower() == "true":
        hardcoded_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 
            "sample", 
            "response_hardcoded.json"
        )
        if os.path.exists(hardcoded_path):
            with open(hardcoded_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return AnnotationResponse(**data)
        else:
            logger.warning("Hardcoded response requested but file not found: %s", hardcoded_path)

    # ------------------------------------------------------------------
    # 1. Validate upload & Check Cache
    # ------------------------------------------------------------------
    pdf_bytes = await _read_pdf_upload(file)
    file_hash = hashlib.md5(pdf_bytes).hexdigest()

    skip_cache = os.getenv("DISABLE_ANNOTATION_CACHE", "false").lower() == "true"
    existing_records = [] if skip_cache else list_manual_annotations_from_store(file_hash)
    if existing_records:
        logger.info("Cache hit for document %s (%s)", file.filename, file_hash)
        
        # We need the page count for the AnnotationResponse, so parse the PDF quickly
        parse_result = _parse_pdf_or_422(pdf_bytes)
        
        cached_annotations = []
        for r in existing_records:
            cached_annotations.append(
                DuctAnnotation(
                    id=r.id,
                    bbox=r.bbox,
                    label=r.label,
                    pressure_class=r.pressure_class,
                    dimension=r.dimension,
                    material=r.material,
                    confidence=r.confidence,
                    orientation=r.orientation,
                    source=r.source,
                    line=r.line,
                )
            )
            
        return AnnotationResponse(
            document_id=file_hash,
            document_name=file.filename,
            page_count=parse_result.page_count,
            duct_count=len(cached_annotations),
            annotations=cached_annotations,
        )

    # ------------------------------------------------------------------
    # 2. Parse PDF
    # ------------------------------------------------------------------
    parse_result = _parse_pdf_or_422(pdf_bytes)

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
    text_extracted_annotations: list[dict[str, object]] = []

    try:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".pdf", delete=False) as temp_pdf:
            temp_pdf_path = temp_pdf.name
            temp_pdf.write(pdf_bytes)

        gpt_results = await analyze_all_crops(
            crops,
            candidates_by_id,
            pdf_path=temp_pdf_path,
            extracted_annotations_out=text_extracted_annotations,
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
    filtered_out = 0
    for cand in candidates:
        analysis = gpt_results.get(cand.id)
        dim = analysis.dimension if analysis else None
        pc = analysis.pressure_class if analysis else None
        mat = analysis.material if analysis else None
        conf = analysis.confidence if analysis else 0.0

        if conf <= 0.0:
            filtered_out += 1
            continue

        if dim and _is_near_existing_dimension(
            annotations,
            dim,
            (cand.bbox.x0, cand.bbox.y0, cand.bbox.x1, cand.bbox.y1),
            distance_threshold=140.0,
        ):
            continue

        # Compute a centerline for the frontend to render at the correct orientation
        _cx = (cand.bbox.x0 + cand.bbox.x1) / 2.0
        _cy = (cand.bbox.y0 + cand.bbox.y1) / 2.0
        if cand.orientation == "vertical":
            computed_line = {"x1": _cx, "y1": cand.bbox.y0, "x2": _cx, "y2": cand.bbox.y1}
        else:
            computed_line = {"x1": cand.bbox.x0, "y1": _cy, "x2": cand.bbox.x1, "y2": _cy}

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
                line=computed_line,
            )
        )

    synthetic_text_count = 0
    next_synthetic_id = -1
    for item in text_extracted_annotations:
        label = str(item.get("label", "")).strip()
        if not label:
            continue

        raw_bbox = item.get("bbox")
        if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
            continue

        try:
            x0 = float(raw_bbox[0])
            y0 = float(raw_bbox[1])
            x1 = float(raw_bbox[2])
            y1 = float(raw_bbox[3])
        except (TypeError, ValueError):
            continue

        raw_conf = item.get("confidence", 0.0)
        try:
            conf = max(0.0, min(1.0, float(raw_conf)))
        except (TypeError, ValueError):
            conf = 0.0
        if conf <= 0.0:
            continue

        if _is_near_existing_dimension(annotations, label, (x0, y0, x1, y1)):
            continue

        pressure_class = str(item.get("pressure_class", "LOW")).upper()

        # --- Expand tiny text bboxes into visible duct segments ---
        # Determine direction from text span metadata
        direction = item.get("direction", [1.0, 0.0])
        dx, dy = 1.0, 0.0
        if isinstance(direction, (list, tuple)) and len(direction) == 2:
            try:
                dx, dy = float(direction[0]), float(direction[1])
            except (TypeError, ValueError):
                dx, dy = 1.0, 0.0

        is_vertical = abs(dy) > abs(dx)
        orient = "vertical" if is_vertical else "horizontal"

        # Compute center of original text bbox
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        page_num = int(raw_bbox[4]) if len(raw_bbox) > 4 else 0
        
        best_line = None
        matched_line_dict = None

        # --- Find centerline via trace_from_label (round ducts) ---
        if is_round_duct_label(label):
            page_lines = lines_by_page.get(page_num, [])
            # Try the text-direction-determined orientation first, then the other
            orient_order = ["vertical", "horizontal"] if is_vertical else ["horizontal", "vertical"]
            for try_orient in orient_order:
                traced = trace_from_label(
                    label=label, cx=cx, cy=cy,
                    orientation=try_orient,
                    page_segments=page_lines,
                    snap_px=60.0,
                )
                if traced:
                    orient = try_orient
                    matched_line_dict = traced
                    sx0 = min(traced["x1"], traced["x2"])
                    sy0 = min(traced["y1"], traced["y2"])
                    sx1 = max(traced["x1"], traced["x2"])
                    sy1 = max(traced["y1"], traced["y2"])
                    best_line = True   # sentinel – line was found
                    break

        if not best_line:
            # Fall back to synthetic box generation
            duct_half_length = 60.0
            duct_half_thickness = 10.0
            
            if is_vertical:
                sx0 = cx - duct_half_thickness
                sy0 = cy - duct_half_length
                sx1 = cx + duct_half_thickness
                sy1 = cy + duct_half_length
            else:
                sx0 = cx - duct_half_length
                sy0 = cy - duct_half_thickness
                sx1 = cx + duct_half_length
                sy1 = cy + duct_half_thickness

        annotations.append(
            DuctAnnotation(
                id=next_synthetic_id,
                bbox=DuctBBox(
                    x0=sx0,
                    y0=sy0,
                    x1=sx1,
                    y1=sy1,
                    page=page_num,
                ),
                label=_make_label(label, pressure_class, None),
                pressure_class=pressure_class,
                dimension=label,
                material=None,
                confidence=conf,
                orientation=orient,
                source="centerline_traced" if best_line else "synthetic",
                line=matched_line_dict
            )
        )
        next_synthetic_id -= 1
        synthetic_text_count += 1

    logger.info(
        "Annotation complete: %d ducts returned (%d unresolved filtered, %d synthetic text labels) in '%s'",
        len(annotations),
        filtered_out,
        synthetic_text_count,
        file.filename,
    )
    return AnnotationResponse(
        document_id=file_hash,
        document_name=file.filename,
        page_count=parse_result.page_count,
        duct_count=len(annotations),
        annotations=annotations,
    )
