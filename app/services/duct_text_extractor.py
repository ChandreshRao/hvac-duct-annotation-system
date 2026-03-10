"""
Rules-based duct text extraction service.

Primary path:
- Extract text spans from page 0 via PyMuPDF
- Match duct labels with prioritized regex rules
- Classify pressure class from nearby context

Fallback trigger:
- Candidate-level analysis returns confidence 0.0 when no nearby regex label
  is found, so callers can fall back to GPT-based crop analysis.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import httpx

from app.core.config import settings
from app.models.schemas import DuctCandidate, GPTDuctAnalysis, TextBlock
from app.services.document_ai_parser import parse_document_ai_json


logger = logging.getLogger(__name__)

_DIA_NUMBER_RE = r'(?<![\d,])\d{1,3}(?:\.\d+)?(?![,\d])'

# Regex priority order (highest first)
ROUND_DUCT_RE = re.compile(rf'{_DIA_NUMBER_RE}\s*["\'°º]?\s*[@ØøΦ⌀∅](?!\w)')
RECT_DUCT_RE = re.compile(r'\d+"\s*[xX×*]\s*\d+')
SHORTHAND_DUCT_RE = re.compile(r'\d+[Ww]\d+')
DIAMETER_ALT_RE = re.compile(rf'{_DIA_NUMBER_RE}\s*"?\s*(?:dia|diameter)\b', flags=re.IGNORECASE)
ROUND_OCR_ZERO_RE = re.compile(rf'{_DIA_NUMBER_RE}\s*["\'°º]\s*0\b')

# Broadened patterns to cover common drawing tokens and OCR variations.
TAGGED_RECT_DUCT_RE = re.compile(
    r'\b[A-Za-z]{1,6}[-_ ]?(\d{1,3}(?:\.\d+)?(?:\s*[xX×*]\s*\d{1,3}(?:\.\d+)?){1,2})[A-Za-z0-9._-]*\b'
)
RECT_DUCT_NO_QUOTE_RE = re.compile(
    r'\b\d{1,3}(?:\.\d+)?\s*[xX×*]\s*\d{1,3}(?:\.\d+)?(?:\s*[xX×*]\s*\d{1,3}(?:\.\d+)?)?\b'
)
ROUND_DUCT_PREFIX_RE = re.compile(rf'[@ØøΦ⌀∅]\s*{_DIA_NUMBER_RE}\s*"?')
DIAMETER_WORD_RE = re.compile(
    rf'\b(?:dia|diameter)\s*[:\-]?\s*{_DIA_NUMBER_RE}\s*"?\b',
    flags=re.IGNORECASE,
)

PATTERN_PRIORITY: tuple[tuple[re.Pattern[str], int | None], ...] = (
    (ROUND_DUCT_RE, None),
    (ROUND_OCR_ZERO_RE, None),
    (RECT_DUCT_RE, None),
    (SHORTHAND_DUCT_RE, None),
    (DIAMETER_ALT_RE, None),
    (TAGGED_RECT_DUCT_RE, 1),
    (RECT_DUCT_NO_QUOTE_RE, None),
    (ROUND_DUCT_PREFIX_RE, None),
    (DIAMETER_WORD_RE, None),
)

HIGH_KEYWORD_RE = re.compile(
    r"\b(grease|stainless|double-wall|double wall)\b",
    flags=re.IGNORECASE,
)
LOW_KEYWORD_RE = re.compile(
    r"\b(supply|return|sa|ra|diffuser)\b",
    flags=re.IGNORECASE,
)
PLAN_NOTES_RE = re.compile(r"\bplan\s+notes?\b", flags=re.IGNORECASE)
EXHAUST_RE = re.compile(r"\bexhaust\b", flags=re.IGNORECASE)
CFM_RE = re.compile(r"(\d[\d,]*)\s*cfm\b", flags=re.IGNORECASE)
ROUND_CONTEXT_TOKEN_RE = re.compile(
    rf'[@ØøΦ⌀∅]|\b(?:dia|diameter)\b|{_DIA_NUMBER_RE}\s*["\'°º]\s*0\b',
    flags=re.IGNORECASE,
)


@dataclass(slots=True)
class _TextSpan:
    text: str
    bbox: tuple[float, float, float, float]
    center: tuple[float, float]
    direction: tuple[float, float] = (1.0, 0.0)
    quad: tuple[float, float, float, float, float, float, float, float] | None = None
    page: int = 0


def _span_center(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float]:
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _coerce_direction(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None


def _is_horizontal_direction(direction: tuple[float, float] | None, tol: float = 0.05) -> bool:
    if direction is None:
        return True
    dx, dy = direction
    return abs(dx - 1.0) <= tol and abs(dy) <= tol


def _is_vertical_direction(direction: tuple[float, float] | None) -> bool:
    if direction is None:
        return False
    dx, dy = direction
    return abs(dy) > abs(dx)


def _point_in_rect(
    point: tuple[float, float],
    rect: tuple[float, float, float, float],
    margin: float = 0.0,
) -> bool:
    x, y = point
    x0, y0, x1, y1 = rect
    return (x0 - margin) <= x <= (x1 + margin) and (y0 - margin) <= y <= (y1 + margin)


def _distance_point_to_rect(
    point: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> float:
    px, py = point
    x0, y0, x1, y1 = rect

    dx = max(x0 - px, 0.0, px - x1)
    dy = max(y0 - py, 0.0, py - y1)
    return math.hypot(dx, dy)


def _coerce_center(center: Any) -> tuple[float, float] | None:
    if not isinstance(center, list) or len(center) != 2:
        return None
    try:
        return float(center[0]), float(center[1])
    except (TypeError, ValueError):
        return None


def _analysis_from_extracted_annotation(item: dict[str, Any]) -> GPTDuctAnalysis:
    label = str(item.get("label", "")).strip()
    if not label:
        return GPTDuctAnalysis(confidence=0.0)

    pressure_class = str(item.get("pressure_class", "LOW")).upper()
    raw_conf = item.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(raw_conf)))
    except (TypeError, ValueError):
        confidence = 0.0

    return GPTDuctAnalysis(
        dimension=label,
        pressure_class=pressure_class,
        material=None,
        confidence=confidence,
    )


def _normalize_text_for_match(text: str) -> str:
    """Normalize common unicode/OCR variants before regex matching."""
    return (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("′", "'")
        .replace("″", '"')
        .replace("’", "'")
        .replace("°", '"')
        .replace("º", '"')
        .replace("*", "x")
        .replace("×", "x")
        .replace("–", "-")
        .replace("—", "-")
        .replace("∅", "⌀")
        .replace("Ø", "⌀")
        .replace("ø", "⌀")
        .replace("Φ", "⌀")
    )


def _canonicalize_label(label: str) -> str:
    """Canonicalize extracted label formatting for consistency."""
    cleaned = re.sub(r"\s+", " ", label.strip())
    cleaned = cleaned.replace("×", "X")
    cleaned = re.sub(r"\s*[xX×*]\s*", "X", cleaned)
    cleaned = cleaned.replace("∅", "⌀")
    cleaned = cleaned.replace("Ø", "⌀")
    cleaned = cleaned.replace("ø", "⌀")
    cleaned = cleaned.replace("Φ", "⌀")
    cleaned = cleaned.replace("@", "⌀")
    cleaned = cleaned.replace("°", '"')
    cleaned = cleaned.replace("º", '"')

    dia_before = re.search(
        r"\b(?:dia|diameter)\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*\"?\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if dia_before:
        return f"{dia_before.group(1)}⌀"

    dia_after = re.search(
        r"\b(\d{1,3}(?:\.\d+)?)\s*\"?\s*(?:dia|diameter)\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if dia_after:
        return f"{dia_after.group(1)}⌀"

    round_suffix = re.search(r"\b(\d{1,3}(?:\.\d+)?)\s*[\"'°º]?\s*⌀(?:\b|$)", cleaned)
    if round_suffix:
        return f"{round_suffix.group(1)}⌀"

    round_prefix = re.search(r"⌀\s*(\d{1,3}(?:\.\d+)?)\s*[\"'°º]?", cleaned)
    if round_prefix:
        return f"{round_prefix.group(1)}⌀"

    round_zero = re.search(r"\b(\d{1,3}(?:\.\d+)?)\s*[\"'°º]\s*0\b", cleaned)
    if round_zero:
        return f"{round_zero.group(1)}⌀"

    return cleaned.strip()


def _label_passes_sanity(label: str) -> bool:
    """Apply light sanity checks to suppress obvious OCR artifacts."""
    rect_match = re.fullmatch(
        r"\s*(\d{1,3}(?:\.\d+)?)\s*[xX]\s*(\d{1,3}(?:\.\d+)?)(?:\s*[xX]\s*(\d{1,3}(?:\.\d+)?))?\s*",
        label,
    )
    if rect_match:
        dims = [float(group) for group in rect_match.groups() if group is not None]
        if any(value < 3.0 or value > 96.0 for value in dims):
            return False
        if len(dims) >= 2 and max(dims) > 0 and (max(dims) / min(dims)) > 12.0:
            return False
        return True

    if "⌀" not in label:
        return True

    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*⌀", label)
    if not m:
        return True

    try:
        size = float(m.group(1))
    except ValueError:
        return False

    return 6.0 <= size <= 96.0


def _same_text_row(a: _TextSpan, b: _TextSpan) -> bool:
    ax0, ay0, ax1, ay1 = a.bbox
    bx0, by0, bx1, by1 = b.bbox
    overlap = min(ay1, by1) - max(ay0, by0)
    if overlap <= 0:
        return False
    min_h = min(max(1.0, ay1 - ay0), max(1.0, by1 - by0))
    return overlap >= 0.45 * min_h


def _looks_round_context_token(text: str) -> bool:
    normalized = _normalize_text_for_match(text)
    return bool(ROUND_CONTEXT_TOKEN_RE.search(normalized))


def _recover_noisy_round_label(anchor: _TextSpan, spans: list[_TextSpan]) -> str | None:
    raw = _normalize_text_for_match(anchor.text).strip()
    if not raw or not any(ch in raw for ch in ('[', ']', '|', '=')):
        return None

    translated = (
        raw.replace('[', '1')
        .replace('|', '1')
        .replace('=', '4')
        .replace(']', '')
        .replace('(', '')
        .replace(')', '')
    )
    digits_only = re.sub(r'[^0-9]', '', translated)
    if len(digits_only) != 2:
        return None

    try:
        value = int(digits_only)
    except ValueError:
        return None

    if value < 8 or value > 48:
        return None

    ax0, _, ax1, _ = anchor.bbox
    has_round_neighbor = False
    for span in spans:
        if span is anchor:
            continue
        if span.page != anchor.page:
            continue
        if not _same_text_row(anchor, span):
            continue
        sx0, _, sx1, _ = span.bbox
        gap = 0.0
        if sx0 > ax1:
            gap = sx0 - ax1
        elif ax0 > sx1:
            gap = ax0 - sx1
        if gap > 80.0:
            continue
        if _looks_round_context_token(span.text):
            has_round_neighbor = True
            break

    if not has_round_neighbor:
        return None

    return f"{value}⌀"


def _extract_page0_text_spans(pdf_path: str) -> list[_TextSpan]:
    def _spans_from_raw_dict(raw: dict[str, Any], page_index: int) -> list[_TextSpan]:
        parsed: list[_TextSpan] = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:  # text block
                continue
            for line in block.get("lines", []):
                line_dir = _coerce_direction(line.get("dir"))
                line_spans: list[tuple[str, tuple[float, float, float, float], tuple[float, float], tuple[float, float, float, float, float, float, float, float] | None]] = []

                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue

                    bbox = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))

                    span_dir = _coerce_direction(span.get("dir")) or line_dir or (1.0, 0.0)
                    quad_raw = span.get("quad")
                    span_quad: tuple[float, float, float, float, float, float, float, float] | None = None
                    if isinstance(quad_raw, (list, tuple)) and len(quad_raw) == 8:
                        try:
                            span_quad = tuple(float(v) for v in quad_raw)  # type: ignore[assignment]
                        except (TypeError, ValueError):
                            span_quad = None

                    line_spans.append((text, (x0, y0, x1, y1), span_dir, span_quad))

                if not line_spans:
                    continue

                has_rotated = any(not _is_horizontal_direction(direction) for _, _, direction, _ in line_spans)
                should_reassemble_vertical = has_rotated and any(
                    _is_vertical_direction(direction) for _, _, direction, _ in line_spans
                )

                if should_reassemble_vertical:
                    dir_y = line_spans[0][2][1] if line_spans else 0.0
                    sorted_vertical = sorted(
                        line_spans,
                        key=lambda item: (
                            (item[1][1] + item[1][3]) / 2.0,
                            (item[1][0] + item[1][2]) / 2.0,
                        ),
                        reverse=(dir_y < -0.5),
                    )
                    merged_text = "".join(item[0] for item in sorted_vertical).strip()
                    if not merged_text:
                        continue

                    vx0 = min(item[1][0] for item in sorted_vertical)
                    vy0 = min(item[1][1] for item in sorted_vertical)
                    vx1 = max(item[1][2] for item in sorted_vertical)
                    vy1 = max(item[1][3] for item in sorted_vertical)
                    parsed.append(
                        _TextSpan(
                            text=merged_text,
                            bbox=(vx0, vy0, vx1, vy1),
                            center=_span_center(vx0, vy0, vx1, vy1),
                            direction=sorted_vertical[0][2],
                            quad=None,
                            page=page_index,
                        )
                    )
                    continue

                for text, (x0, y0, x1, y1), direction, quad in line_spans:
                    parsed.append(
                        _TextSpan(
                            text=text,
                            bbox=(x0, y0, x1, y1),
                            center=_span_center(x0, y0, x1, y1),
                            direction=direction,
                            quad=quad,
                            page=page_index,
                        )
                    )
        return parsed

    def _dedupe_spans(spans: list[_TextSpan]) -> list[_TextSpan]:
        unique: list[_TextSpan] = []
        seen: set[tuple[int, str, float, float, float, float]] = set()
        for span in spans:
            normalized = re.sub(r"\s+", " ", _normalize_text_for_match(span.text)).strip().lower()
            x0, y0, x1, y1 = span.bbox
            key = (span.page, normalized, round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))
            if key in seen:
                continue
            seen.add(key)
            unique.append(span)
        return unique

    def _spans_from_ocr_service(page: fitz.Page, page_index: int) -> list[_TextSpan]:
        ocr_dpi = float(max(300, int(settings.ocr_dpi)))
        mat = fitz.Matrix(ocr_dpi / 72.0, ocr_dpi / 72.0)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png_bytes = pix.tobytes("png")

        with httpx.Client(timeout=settings.ocr_service_timeout_seconds) as client:
            resp = client.post(
                settings.ocr_service_url,
                files={"file": ("page0.png", png_bytes, "image/png")},
            )
            resp.raise_for_status()
            data = resp.json()

        service_spans = data.get("spans", [])
        pdf_spans: list[_TextSpan] = []
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

            pdf_spans.append(
                _TextSpan(
                    text=text,
                    bbox=(x0, y0, x1, y1),
                    center=_span_center(x0, y0, x1, y1),
                    page=page_index,
                )
            )

        return pdf_spans

    doc = fitz.open(pdf_path)
    try:
        if len(doc) == 0:
            return []

        all_spans: list[_TextSpan] = []

        for page in doc:
            raw = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            page_spans = _spans_from_raw_dict(raw, page.number)
            embedded_count = len(page_spans)

            if settings.enable_ocr_extraction:
                try:
                    if settings.use_ocr_service:
                        ocr_spans = _spans_from_ocr_service(page, page.number)
                    else:
                        if not hasattr(page, "get_textpage_ocr"):
                            logger.warning(
                                "OCR requested but current PyMuPDF build has no get_textpage_ocr support"
                            )
                            ocr_spans = []
                        else:
                            ocr_dpi = max(300, int(settings.ocr_dpi))

                            textpage_ocr = page.get_textpage_ocr(
                                language=settings.ocr_language,
                                dpi=ocr_dpi,
                                full=True,
                            )
                            raw_ocr = page.get_text(
                                "dict",
                                flags=fitz.TEXT_PRESERVE_WHITESPACE,
                                textpage=textpage_ocr,
                            )
                            ocr_spans = _spans_from_raw_dict(raw_ocr, page.number)

                    merged_page_spans = _dedupe_spans(page_spans + ocr_spans)
                    all_spans.extend(merged_page_spans)
                    logger.info(
                        "OCR enabled: page=%d embedded spans=%d, ocr spans=%d, merged spans=%d",
                        page.number,
                        embedded_count,
                        len(ocr_spans),
                        len(merged_page_spans),
                    )
                except Exception as exc:
                    all_spans.extend(page_spans)
                    logger.warning(
                        "OCR extraction failed on page %d, using embedded text only: %s",
                        page.number,
                        exc,
                    )
            else:
                all_spans.extend(page_spans)
        
        # Integrate Document AI JSON if present (currently hardcoded as a sidecar to the pdf)
        pdf_path_obj = Path(pdf_path)
        doc_ai_json_path = pdf_path_obj.parent / "document.json"
        if doc_ai_json_path.exists():
            logger.info(f"document.json found at {doc_ai_json_path}, merging Document AI texts.")
            # PDF logical dims (assuming uniform pages for now)
            w = doc[0].rect.width
            h = doc[0].rect.height
            doc_ai_blocks = parse_document_ai_json(doc_ai_json_path, pdf_width=w, pdf_height=h)
            for b in doc_ai_blocks:
                cx = (b.x0 + b.x1) / 2.0
                cy = (b.y0 + b.y1) / 2.0
                
                # Format raw Document AI outputs (like 14" or 12") to include the phi symbol
                formatted_text = b.text.strip()
                if 'x' not in formatted_text.lower() and '*' not in formatted_text:
                    m = re.search(r'^(\d+)(.*)$', formatted_text)
                    if m:
                        formatted_text = f"{m.group(1)}⌀"
                
                span = _TextSpan(
                    text=formatted_text,
                    bbox=(b.x0, b.y0, b.x1, b.y1),
                    center=(cx, cy),
                    direction=(1.0, 0.0), 
                    quad=None,
                    page=b.page
                )
                all_spans.append(span)

        return _dedupe_spans(all_spans)
    finally:
        doc.close()


def _first_pattern_match(text: str) -> str | None:
    for candidate_text in (text, _normalize_text_for_match(text)):
        for pattern, group_idx in PATTERN_PRIORITY:
            m = pattern.search(candidate_text)
            if not m:
                continue
            matched = m.group(group_idx) if group_idx is not None else m.group(0)
            if matched:
                canonical = _canonicalize_label(matched)
                if canonical and _label_passes_sanity(canonical):
                    return canonical
    return None


def _text_match_variants(anchor: _TextSpan, spans: list[_TextSpan]) -> list[str]:
    """Return text variants for matching, including simple adjacent-span joins."""
    variants: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        normalized = value.strip()
        if not normalized:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        variants.append(normalized)

    _add(anchor.text)

    x0, y0, x1, y1 = anchor.bbox
    anchor_h = max(1.0, y1 - y0)
    horizontal_gap_limit = 20.0

    right_candidates: list[tuple[float, _TextSpan]] = []
    left_candidates: list[tuple[float, _TextSpan]] = []

    for span in spans:
        if span is anchor:
            continue
        if span.page != anchor.page:
            continue
        sx0, sy0, sx1, sy1 = span.bbox
        span_h = max(1.0, sy1 - sy0)

        overlap = min(y1, sy1) - max(y0, sy0)
        min_h = min(anchor_h, span_h)
        if overlap <= 0.0 or overlap < 0.5 * min_h:
            continue

        right_gap = sx0 - x1
        if 0.0 <= right_gap <= horizontal_gap_limit:
            right_candidates.append((right_gap, span))

        left_gap = x0 - sx1
        if 0.0 <= left_gap <= horizontal_gap_limit:
            left_candidates.append((left_gap, span))

    right_candidates.sort(key=lambda item: item[0])
    left_candidates.sort(key=lambda item: item[0])

    nearest_right = right_candidates[0][1] if right_candidates else None
    second_right = right_candidates[1][1] if len(right_candidates) > 1 else None
    nearest_left = left_candidates[0][1] if left_candidates else None

    if nearest_right is not None:
        right_text = nearest_right.text
        _add(f"{anchor.text}{right_text}")
        _add(f"{anchor.text} {right_text}")

    if nearest_left is not None:
        left_text = nearest_left.text
        _add(f"{left_text}{anchor.text}")
        _add(f"{left_text} {anchor.text}")

    if nearest_right is not None and second_right is not None:
        r1 = nearest_right.text
        r2 = second_right.text
        _add(f"{anchor.text}{r1}{r2}")
        _add(f"{anchor.text} {r1} {r2}")
        _add(f"{anchor.text}{r1} {r2}")

    if nearest_left is not None and nearest_right is not None:
        left_text = nearest_left.text
        right_text = nearest_right.text
        _add(f"{left_text}{anchor.text}{right_text}")
        _add(f"{left_text} {anchor.text} {right_text}")
        _add(f"{left_text}{anchor.text} {right_text}")
        _add(f"{left_text} {anchor.text}{right_text}")

    return variants


def _nearby_texts(anchor: _TextSpan, spans: list[_TextSpan]) -> list[str]:
    nearby: list[str] = []
    radius = max(0.0, float(settings.text_context_radius_px))
    for span in spans:
        if span is anchor:
            continue
        if span.page != anchor.page:
            continue
        if _distance(anchor.center, span.center) <= radius:
            nearby.append(span.text)
    return nearby


def _extract_cfm_values(text_blob: str) -> list[int]:
    values: list[int] = []
    for match in CFM_RE.finditer(text_blob):
        try:
            values.append(int(match.group(1).replace(",", "")))
        except ValueError:
            continue
    return values


def _classify_pressure_class(
    nearby_blob: str,
    page_blob: str,
) -> tuple[str, bool]:
    """
    Returns
    -------
    pressure_class, explicit_rule_matched
    """
    if HIGH_KEYWORD_RE.search(nearby_blob):
        return "HIGH", True

    if PLAN_NOTES_RE.search(page_blob) and (
        EXHAUST_RE.search(nearby_blob) or EXHAUST_RE.search(page_blob)
    ):
        cfm_values = _extract_cfm_values(nearby_blob)
        if not cfm_values:
            cfm_values = _extract_cfm_values(page_blob)
        if any(v > 1000 for v in cfm_values):
            return "HIGH", True
        return "MEDIUM", True

    if LOW_KEYWORD_RE.search(nearby_blob):
        return "LOW", True

    # Default fallback per requirement
    return "LOW", False


def extract_duct_text_annotations(pdf_path: str) -> list[dict[str, Any]]:
    """
    Extract duct-size labels from page 0 and classify pressure by nearby text.

    Parameters
    ----------
    pdf_path:
        Path to a PDF file.

    Returns
    -------
    List of dict objects:
        {
          "id": uuid string,
          "label": matched text,
          "bbox": [x0, y0, x1, y1],
          "center": [cx, cy],
          "pressure_class": "LOW" | "MEDIUM" | "HIGH",
          "confidence": float,
          "source": "text_extraction"
        }
    """
    if not Path(pdf_path).exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    spans = _extract_page0_text_spans(pdf_path)
    if not spans:
        return []

    page_blob_by_page: dict[int, str] = {}
    for span in spans:
        if span.page not in page_blob_by_page:
            page_blob_by_page[span.page] = span.text
        else:
            page_blob_by_page[span.page] = f"{page_blob_by_page[span.page]} {span.text}"

    annotations: list[dict[str, Any]] = []

    for span in spans:
        label: str | None = None
        label = _recover_noisy_round_label(span, spans)
        if not label:
            for variant in _text_match_variants(span, spans):
                label = _first_pattern_match(variant)
                if label:
                    break
        if not label:
            continue

        nearby = _nearby_texts(span, spans)
        nearby_blob = " ".join(nearby)
        page_blob = page_blob_by_page.get(span.page, "")
        pressure_class, matched_explicit_rule = _classify_pressure_class(
            nearby_blob=nearby_blob,
            page_blob=page_blob,
        )

        confidence = 0.95 if matched_explicit_rule else 0.75
        x0, y0, x1, y1 = span.bbox
        cx, cy = span.center

        annotations.append(
            {
                "id": str(uuid.uuid4()),
                "label": label,
                "bbox": [x0, y0, x1, y1],
                "quad": list(span.quad) if span.quad is not None else None,
                "direction": [span.direction[0], span.direction[1]],
                "center": [cx, cy],
                "page": span.page,
                "pressure_class": pressure_class,
                "confidence": confidence,
                "source": "text_extraction",
            }
        )

    logger.info("Text extraction complete: %d matched labels from '%s'", len(annotations), pdf_path)
    return annotations


def analyze_candidate_via_text_extraction(
    candidate: DuctCandidate,
    extracted_annotations: list[dict[str, Any]],
) -> GPTDuctAnalysis:
    """
    Resolve one detected candidate using extracted text matches.

    If no nearby text match exists (confidence 0.0), caller should fall back to
    the existing GPT crop analyzer for this candidate.
    """
    matched = analyze_candidates_via_text_extraction(
        {candidate.id: candidate},
        extracted_annotations,
    )
    return matched.get(candidate.id, GPTDuctAnalysis(confidence=0.0))


def analyze_candidates_via_text_extraction(
    candidates_by_id: dict[int, DuctCandidate],
    extracted_annotations: list[dict[str, Any]],
) -> dict[int, GPTDuctAnalysis]:
    """
    Resolve detected candidates using extracted text matches with locality rules.

    Matching rules:
    - Text must be inside a candidate bbox (with configurable margin), or
      within a max point-to-bbox distance threshold.
    - Greedy global assignment uses a geometric score so each candidate gets
      the best nearby text item.
    - A single extracted text annotation can resolve only a limited number of
      candidates to avoid one label over-propagating across the whole page.
    """
    if not candidates_by_id or not extracted_annotations:
        return {}

    margin = max(0.0, float(settings.text_match_bbox_margin_px))
    max_distance = max(0.0, float(settings.text_match_max_distance_px))
    max_per_annotation = max(1, int(settings.max_candidates_per_text_annotation))

    assignment_candidates: list[tuple[float, int, int]] = []

    for cand_id, candidate in candidates_by_id.items():
        cand_rect = (
            float(candidate.bbox.x0),
            float(candidate.bbox.y0),
            float(candidate.bbox.x1),
            float(candidate.bbox.y1),
        )
        cand_center = (
            (candidate.bbox.x0 + candidate.bbox.x1) / 2.0,
            (candidate.bbox.y0 + candidate.bbox.y1) / 2.0,
        )

        for ann_idx, annotation in enumerate(extracted_annotations):
            ann_page_raw = annotation.get("page", 0)
            try:
                ann_page = int(ann_page_raw)
            except (TypeError, ValueError):
                ann_page = 0
            if ann_page != candidate.bbox.page:
                continue

            ann_center = _coerce_center(annotation.get("center"))
            if ann_center is None:
                continue

            in_or_near = _point_in_rect(ann_center, cand_rect, margin=margin)
            dist_to_bbox = _distance_point_to_rect(ann_center, cand_rect)
            if not in_or_near and dist_to_bbox > max_distance:
                continue

            center_dist = _distance(cand_center, ann_center)
            score = (dist_to_bbox * 4.0) + center_dist
            if in_or_near:
                score -= max_distance

            assignment_candidates.append((score, cand_id, ann_idx))

    assignment_candidates.sort(key=lambda row: row[0])

    resolved: dict[int, GPTDuctAnalysis] = {}
    usage_by_annotation: dict[int, int] = {}

    for _, cand_id, ann_idx in assignment_candidates:
        if cand_id in resolved:
            continue

        current_usage = usage_by_annotation.get(ann_idx, 0)
        if current_usage >= max_per_annotation:
            continue

        analysis = _analysis_from_extracted_annotation(extracted_annotations[ann_idx])
        if analysis.confidence <= 0.0:
            continue

        resolved[cand_id] = analysis
        usage_by_annotation[ann_idx] = current_usage + 1

    logger.info(
        "Text-to-candidate matching: %d candidates resolved from %d extracted labels",
        len(resolved),
        len(extracted_annotations),
    )
    return resolved
