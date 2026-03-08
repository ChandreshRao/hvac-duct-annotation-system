# HVAC Duct Annotation System

A FastAPI backend that accepts an HVAC mechanical-drawing PDF, detects duct
regions using PyMuPDF vector geometry, analyses each region with rules-based
text extraction first, falls back to GPT-4o when rules cannot resolve a
region, and returns structured annotations.

---

## Architecture

```
POST /api/v1/annotate
         │
         ▼
  PDFParser (PyMuPDF)
  • extract vector line segments
  • extract text blocks with positions
         │
         ▼
  DuctDetector
  • find pairs of parallel lines (horizontal & vertical)
  • apply gap / length / overlap heuristics
  • non-maximum suppression
         │
         ▼
  ImageCropper (PyMuPDF pixmap)
  • render each page at configurable DPI
  • crop bounding-box region → PNG bytes
         │
         ▼
  DuctTextExtractor (rules-first)
  • page-0 text span extraction
  • regex label matching + pressure heuristics
         │
         ▼
  GPT-4o Analyzer (fallback only)
  • base64-encode PNG
  • send with structured prompt
  • parse JSON: dimension, pressure_class, material, confidence
         │
         ▼
  AnnotationResponse → frontend
  [{ id, bbox, label, pressure_class, dimension, material, confidence }]
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env; set ENABLE_GPT_FALLBACK=true to use GPT fallback.
# If fallback is enabled, also set GITHUB_TOKEN or OPENAI_API_KEY.
# Optional: set ENABLE_OCR_EXTRACTION=true when PDF text is not embedded.
# Optional Docker OCR service: set USE_OCR_SERVICE=true and run `docker compose up -d ocr-service`.
```

### 3. Run the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Interactive docs → http://localhost:8000/docs

---

## API Reference

### `POST /api/v1/annotate`

**Request** – `multipart/form-data`

| Field | Type | Description |
|-------|------|-------------|
| `file` | `File` (PDF) | HVAC mechanical drawing |

**Response** – `application/json`

```jsonc
{
  "page_count": 2,
  "duct_count": 14,
  "annotations": [
    {
      "id": 0,
      "bbox": { "x0": 120.5, "y0": 200.1, "x1": 340.0, "y1": 230.4, "page": 0 },
      "label": "24x12  |  1\"wg  |  galvanized steel",
      "pressure_class": "1\"wg",
      "dimension": "24x12",
      "material": "galvanized steel",
      "confidence": 0.93,
      "orientation": "horizontal"
    }
  ]
}
```

### `POST /api/v1/texts`

Upload a PDF and return every extracted text span with coordinates.

Query params:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `include_ocr` | `bool` | `true` | Merge OCR text spans (service/local OCR) with embedded PDF text |
| `include_normalized` | `bool` | `true` | Adds `normalized_text`, inferred `normalized_label`, and `normalized_variants` fields per text span |

**Request** – `multipart/form-data`

| Field | Type | Description |
|-------|------|-------------|
| `file` | `File` (PDF) | HVAC mechanical drawing |

**Response** – `application/json`

```jsonc
{
       "page_count": 1,
       "text_count": 125,
       "page_sizes": [
              { "page": 0, "width": 1728.0, "height": 2592.0 }
       ],
       "texts": [
              {
                     "text": "14\"⌀",
                     "x0": 1296.0,
                     "y0": 1191.0,
                     "x1": 1709.0,
                     "y1": 1214.0,
                     "page": 0,
                     "source": "ocr",
                     "normalized_text": "140.04",
                     "normalized_label": "14⌀",
                     "normalized_variants": ["14⌀", "14\"Ø", "14\"ø", "14\"⌀", "Ø14", "ø14", "⌀14", "14\"@"]
              }
       ]
}
```

### `POST /api/v1/manual-annotations`

Save one user-corrected annotation into SQLite for a specific document.

**Request** – `application/json`

```jsonc
{
       "document_id": "<sha256-or-custom-doc-id>",
       "document_name": "testset2.pdf",
       "annotation": {
              "bbox": { "x0": 1296.0, "y0": 1192.0, "x1": 1709.0, "y1": 1213.0, "page": 0 },
              "label": "14\"⌀",
              "pressure_class": "HIGH",
              "dimension": "14\"⌀",
              "material": null,
              "confidence": 1.0,
              "orientation": "horizontal"
       }
}
```

**Response** – `application/json`

```jsonc
{
       "id": 1,
       "document_id": "<sha256-or-custom-doc-id>",
       "document_name": "testset2.pdf",
       "bbox": { "x0": 1296.0, "y0": 1192.0, "x1": 1709.0, "y1": 1213.0, "page": 0 },
       "label": "14\"⌀",
       "pressure_class": "HIGH",
       "dimension": "14\"⌀",
       "material": null,
       "confidence": 1.0,
       "orientation": "horizontal",
       "source": "manual",
       "created_at": "2026-03-08T10:20:30.000000+00:00",
       "updated_at": "2026-03-08T10:20:30.000000+00:00"
}
```

### `GET /api/v1/manual-annotations/{document_id}`

Retrieve all saved manual corrections for a document.

**Response** – `application/json`

```jsonc
{
       "document_id": "<sha256-or-custom-doc-id>",
       "count": 1,
       "annotations": [
              {
                     "id": 1,
                     "document_id": "<sha256-or-custom-doc-id>",
                     "document_name": "testset2.pdf",
                     "bbox": { "x0": 1296.0, "y0": 1192.0, "x1": 1709.0, "y1": 1213.0, "page": 0 },
                     "label": "14\"⌀",
                     "pressure_class": "HIGH",
                     "dimension": "14\"⌀",
                     "material": null,
                     "confidence": 1.0,
                     "orientation": "horizontal",
                     "source": "manual",
                     "created_at": "2026-03-08T10:20:30.000000+00:00",
                     "updated_at": "2026-03-08T10:20:30.000000+00:00"
              }
       ]
}
```

### `PUT /api/v1/manual-annotations/{annotation_id}`

Update an existing saved manual correction by annotation id.

**Request** – `application/json`

```jsonc
{
       "annotation": {
              "bbox": { "x0": 1300.0, "y0": 1190.0, "x1": 1715.0, "y1": 1215.0, "page": 0 },
              "label": "14\"⌀",
              "pressure_class": "HIGH",
              "dimension": "14\"⌀",
              "material": null,
              "confidence": 1.0,
              "orientation": "horizontal"
       }
}
```

**Response** – `application/json` (same shape as `POST /manual-annotations`)

### `DELETE /api/v1/manual-annotations/{annotation_id}`

Delete a saved manual correction by annotation id.

**Response** – `application/json`

```jsonc
{
       "id": 1,
       "deleted": true
}
```

### `GET /api/v1/health`

Returns `{"status": "ok"}`.

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | – | GitHub PAT for GitHub Models endpoint |
| `OPENAI_API_KEY` | – | OpenAI API key (used if GITHUB_TOKEN not set) |
| `GPT_MODEL` | `gpt-4o` | Model name |
| `GPT_TIMEOUT_SECONDS` | `60` | HTTP timeout for GPT calls |
| `ENABLE_GPT_FALLBACK` | `false` | Enables GPT-4o fallback when text rules do not match |
| `ENABLE_OCR_EXTRACTION` | `false` | Enables OCR text extraction for page 0 when embedded text is insufficient |
| `OCR_LANGUAGE` | `eng` | OCR language passed to PyMuPDF OCR engine |
| `OCR_DPI` | `300` | OCR render DPI for text recognition quality |
| `USE_OCR_SERVICE` | `false` | Use external OCR HTTP service instead of in-process OCR |
| `OCR_SERVICE_URL` | `http://localhost:8081/ocr` | OCR service endpoint URL |
| `OCR_SERVICE_TIMEOUT_SECONDS` | `30` | Timeout (seconds) for OCR service calls |
| `TEXT_CONTEXT_RADIUS_PX` | `100.0` | Radius for nearby text context used in pressure classification |
| `TEXT_MATCH_MAX_DISTANCE_PX` | `60.0` | Max point-to-candidate distance allowed for text-to-candidate association |
| `TEXT_MATCH_BBOX_MARGIN_PX` | `24.0` | Candidate bbox expansion margin when checking text containment |
| `MAX_CANDIDATES_PER_TEXT_ANNOTATION` | `1` | Caps how many candidates a single extracted text label can resolve |
| `RENDER_DPI` | `150` | PDF render resolution for crops |
| `DUCT_MIN_GAP` | `4.0` | Min parallel-line gap (PDF points) |
| `DUCT_MAX_GAP` | `200.0` | Max parallel-line gap (PDF points) |
| `MAX_UPLOAD_SIZE_MB` | `50` | Maximum PDF upload size |
| `MANUAL_ANNOTATIONS_DB_PATH` | `data/manual_annotations.db` | SQLite file path for saved manual corrections |

---

## Project Structure

```
hvac-duct-annotation-system/
├── app/
│   ├── main.py                  # FastAPI app factory
│   ├── core/
│   │   └── config.py            # pydantic-settings config
│   ├── models/
│   │   └── schemas.py           # Pydantic request/response models
│   ├── routers/
│   │   └── annotations.py       # /api/v1/annotate endpoint
│   └── services/
│       ├── pdf_parser.py        # PyMuPDF line + text extraction
│       ├── duct_detector.py     # Parallel-line duct detection
│       ├── image_cropper.py     # PDF region → PNG crop
│       ├── duct_text_extractor.py # Rules-based text extraction + pressure rules
│       └── gpt_analyzer.py      # GPT-4o vision analysis
├── requirements.txt
├── .env.example
└── README.md
```

---

## OCR Service (Docker)

Run a standalone OCR service:

```bash
docker compose up -d ocr-service
```

Run both API and OCR together:

```bash
docker compose up -d --build
```

Then set in `.env`:

```dotenv
ENABLE_OCR_EXTRACTION=true
USE_OCR_SERVICE=true
OCR_SERVICE_URL=http://ocr-service:8081/ocr   # use this when API runs in compose
```

```
Reading Mechanical Drawing
Medium
Problem Statement
Given is an engineering mechanical drawing for an HVAC system. We need a system that is able to read the ducts in the drawing, 
annotate the ducts with lines, and is able to provide the dimensions of that duct. 
For example, in the given sample, the 14"⌀ duct, which should be annotated 
when the user clicks on the annotated line must show us the dimension of that duct as 14"⌀. 
Additionally, the system should also identify the pressure class of each duct here (low pressure / medium pressure / high pressure )
Input file: https://drive.google.com/file/d/1n-2orOHQC1xLU8UZXB06PvGkPqjjc--_/view?usp=sharing
Sample annotation: https://drive.google.com/file/d/1ntLkSKRTDbCzYrQrI78arNHdIsy2LpTe/view?usp=sharing
```