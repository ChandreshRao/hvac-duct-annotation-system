from __future__ import annotations

from io import BytesIO
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile

app = FastAPI(title="OCR Service", version="1.0.0")

DIMENSION_FOCUSED_CONFIG = (
    "--oem 3 --psm 11 "
    "-c tessedit_char_whitelist=0123456789xX×@ØøΦ⌀diaDIA.-_"
)


def _load_ocr_modules() -> tuple[object, object]:
    try:
        pytesseract_mod = __import__("pytesseract")
        pil_image_mod = __import__("PIL.Image", fromlist=["Image"])
        try:
            pil_image_mod.MAX_IMAGE_PIXELS = None
        except Exception:
            pass
        return pytesseract_mod, pil_image_mod
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"OCR service dependency missing: {exc}",
        ) from exc


def _spans_from_tesseract_dict(data: dict[str, Any]) -> list[dict[str, object]]:
    count = len(data.get('text', []))
    spans: list[dict[str, object]] = []

    for i in range(count):
        text = str(data['text'][i]).strip()
        if not text:
            continue

        try:
            conf_raw = float(data['conf'][i])
        except (TypeError, ValueError):
            conf_raw = -1.0

        if conf_raw < 0:
            continue

        left = float(data['left'][i])
        top = float(data['top'][i])
        width = float(data['width'][i])
        height = float(data['height'][i])

        spans.append(
            {
                'text': text,
                'bbox': [left, top, left + width, top + height],
                'confidence': max(0.0, min(1.0, conf_raw / 100.0)),
            }
        )

    return spans


def _dedupe_spans(spans: list[dict[str, object]]) -> list[dict[str, object]]:
    unique: list[dict[str, object]] = []
    seen: set[tuple[str, float, float, float, float]] = set()

    for span in spans:
        text = str(span.get('text', '')).strip()
        bbox = span.get('bbox')
        if not text or not isinstance(bbox, list) or len(bbox) != 4:
            continue

        try:
            x0 = float(bbox[0])
            y0 = float(bbox[1])
            x1 = float(bbox[2])
            y1 = float(bbox[3])
            conf = float(span.get('confidence', 0.0))
        except (TypeError, ValueError):
            continue

        key = (text.lower(), round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))
        if key in seen:
            continue

        seen.add(key)
        unique.append(
            {
                'text': text,
                'bbox': [x0, y0, x1, y1],
                'confidence': max(0.0, min(1.0, conf)),
            }
        )

    return unique


@app.get('/health')
def health() -> dict[str, str]:
    return {'status': 'ok'}


@app.post('/ocr')
async def ocr(file: UploadFile = File(...)) -> dict[str, list[dict[str, object]]]:
    if not file.content_type or not file.content_type.startswith('image/'):
        raise HTTPException(status_code=415, detail='Only image uploads are supported')

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail='Empty image payload')

    pytesseract_mod, pil_image_mod = _load_ocr_modules()

    try:
        image = pil_image_mod.open(BytesIO(image_bytes)).convert('RGB')
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f'Invalid image: {exc}') from exc

    general_data = pytesseract_mod.image_to_data(
        image,
        output_type=pytesseract_mod.Output.DICT,
    )
    dimension_data = pytesseract_mod.image_to_data(
        image,
        output_type=pytesseract_mod.Output.DICT,
        config=DIMENSION_FOCUSED_CONFIG,
    )

    merged = _spans_from_tesseract_dict(general_data) + _spans_from_tesseract_dict(dimension_data)
    return {'spans': _dedupe_spans(merged)}
