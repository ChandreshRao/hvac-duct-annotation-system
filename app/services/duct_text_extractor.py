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

from app.models.schemas import DuctCandidate, GPTDuctAnalysis

logger = logging.getLogger(__name__)

NEARBY_RADIUS_PX: float = 100.0

# Regex priority order (highest first)
ROUND_DUCT_RE = re.compile(r'\d+"\s*[ØøΦ⌀]')
RECT_DUCT_RE = re.compile(r'\d+"\s*[xX×]\s*\d+')
SHORTHAND_DUCT_RE = re.compile(r'\d+[Ww]\d+')
DIAMETER_ALT_RE = re.compile(r'\d+"\s*dia\b', flags=re.IGNORECASE)

# Broadened patterns to cover common drawing tokens and OCR variations.
TAGGED_RECT_DUCT_RE = re.compile(
    r'\b[A-Za-z]{1,6}[-_ ]?(\d{1,3}(?:\.\d+)?(?:\s*[xX×]\s*\d{1,3}(?:\.\d+)?){1,2})[A-Za-z0-9._-]*\b'
)
RECT_DUCT_NO_QUOTE_RE = re.compile(
    r'\b\d{1,3}(?:\.\d+)?\s*[xX×]\s*\d{1,3}(?:\.\d+)?(?:\s*[xX×]\s*\d{1,3}(?:\.\d+)?)?\b'
)
ROUND_DUCT_PREFIX_RE = re.compile(r'[ØøΦ⌀]\s*\d{1,3}(?:\.\d+)?\s*"?')
DIAMETER_WORD_RE = re.compile(
    r'\b(?:dia|diameter)\s*[:\-]?\s*\d{1,3}(?:\.\d+)?\s*"?\b',
    flags=re.IGNORECASE,
)

PATTERN_PRIORITY: tuple[tuple[re.Pattern[str], int | None], ...] = (
    (ROUND_DUCT_RE, None),
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


@dataclass(slots=True)
class _TextSpan:
    text: str
    bbox: tuple[float, float, float, float]
    center: tuple[float, float]


def _span_center(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float]:
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _normalize_text_for_match(text: str) -> str:
    """Normalize common unicode/OCR variants before regex matching."""
    return (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("′", "'")
        .replace("″", '"')
        .replace("’", "'")
        .replace("×", "x")
        .replace("–", "-")
        .replace("—", "-")
    )


def _canonicalize_label(label: str) -> str:
    """Canonicalize extracted label formatting for consistency."""
    cleaned = re.sub(r"\s+", " ", label.strip())
    cleaned = cleaned.replace("×", "X")
    cleaned = re.sub(r"\s*[xX]\s*", "X", cleaned)
    return cleaned.strip()


def _extract_page0_text_spans(pdf_path: str) -> list[_TextSpan]:
    doc = fitz.open(pdf_path)
    try:
        if len(doc) == 0:
            return []

        page = doc[0]
        raw = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        spans: list[_TextSpan] = []

        for block in raw.get("blocks", []):
            if block.get("type") != 0:  # text block
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue
                    bbox = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
                    spans.append(
                        _TextSpan(
                            text=text,
                            bbox=(x0, y0, x1, y1),
                            center=_span_center(x0, y0, x1, y1),
                        )
                    )

        return spans
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
                return _canonicalize_label(matched)
    return None


def _nearby_texts(anchor: _TextSpan, spans: list[_TextSpan]) -> list[str]:
    nearby: list[str] = []
    for span in spans:
        if span is anchor:
            continue
        if _distance(anchor.center, span.center) <= NEARBY_RADIUS_PX:
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

    page_blob = " ".join(span.text for span in spans)
    annotations: list[dict[str, Any]] = []

    for span in spans:
        label = _first_pattern_match(span.text)
        if not label:
            continue

        nearby = _nearby_texts(span, spans)
        nearby_blob = " ".join(nearby)
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
                "center": [cx, cy],
                "pressure_class": pressure_class,
                "confidence": confidence,
                "source": "text_extraction",
            }
        )

    logger.info(
        "Text extraction complete: %d matched labels on page 0 from '%s'",
        len(annotations),
        pdf_path,
    )
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
    if candidate.bbox.page != 0:
        return GPTDuctAnalysis(confidence=0.0)

    candidate_center = (
        (candidate.bbox.x0 + candidate.bbox.x1) / 2.0,
        (candidate.bbox.y0 + candidate.bbox.y1) / 2.0,
    )

    nearest: dict[str, Any] | None = None
    nearest_dist = float("inf")

    for item in extracted_annotations:
        center = item.get("center")
        if not isinstance(center, list) or len(center) != 2:
            continue

        try:
            cx = float(center[0])
            cy = float(center[1])
        except (TypeError, ValueError):
            continue

        dist = _distance(candidate_center, (cx, cy))
        if dist <= NEARBY_RADIUS_PX and dist < nearest_dist:
            nearest = item
            nearest_dist = dist

    if nearest is None:
        return GPTDuctAnalysis(confidence=0.0)

    label = str(nearest.get("label", "")).strip()
    if not label:
        return GPTDuctAnalysis(confidence=0.0)

    pressure_class = str(nearest.get("pressure_class", "LOW")).upper()
    raw_conf = nearest.get("confidence", 0.0)
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
