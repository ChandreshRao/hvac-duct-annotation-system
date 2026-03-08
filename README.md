# HVAC Duct Annotation System

A FastAPI backend that accepts an HVAC mechanical-drawing PDF, detects duct
regions using PyMuPDF vector geometry, analyses each region with rules-based
text extraction first, falls back to an AI provider (GPT-4o / Claude) when rules cannot resolve a
region, and returns structured annotations.

---

## Architecture

```
POST /api/v1/annotate
         │
         ▼
  MD5 Hasher & Cache Lookup
  • Generates MD5 hash of uploaded PDF
  • Checks SQLite 'manual_annotations' DB for existing records
  • ON CACHE HIT: Returns AnnotationResponse instantly (skips extraction)
  • ON CACHE MISS: Proceeds to extraction pipeline
         │
         ▼
  PDFParser (PyMuPDF)
         ▼
  TextExtractor & OCR Pipeline
  • Extract embedded text blocks (PyMuPDF)
  • If text is missing/sparse, fallback to OCR Service (Tesseract)
         │
         ▼
  DuctDetector
  • Find pairs of parallel lines (horizontal & vertical)
  • Find single-line centerlines matching '⌀' dimension patterns
  • Apply gap / length / overlap heuristics
  • Non-maximum suppression
         │
         ▼
  ImageCropper (PyMuPDF pixmap)
  • Render each page at configurable DPI
  • Crop bounding-box region → PNG bytes
         │
         ▼
  DuctTextExtractor (rules-first)
  • Associate extracted/OCR text spans near geometry
  • Regex label matching + pressure heuristics
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
# Edit .env; set ENABLE_GPT_FALLBACK=true to use an AI provider.
# If fallback is enabled, supply exactly ONE of the following API keys:
#    ANTHROPIC_API_KEY (supports Claude series e.g. claude-3-5-sonnet-20241022)
#    GITHUB_TOKEN      (supports GitHub Models e.g. gpt-4o)
#    OPENAI_API_KEY    (supports OpenAI e.g. gpt-4o)
# Optional: set ENABLE_OCR_EXTRACTION=true when PDF text is not embedded.
# Optional Docker OCR service: set USE_OCR_SERVICE=true and run `docker compose up -d ocr-service`.
```

### 3. Run the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Interactive docs → http://localhost:8000/docs

### 4. Use the Viewer

The frontend UI is served automatically by the FastAPI backend at:
**http://localhost:8000/viewer/duct_annotator.html**

Features:
- **UPLOAD PDF**: Drag & drop or select an HVAC PDF to annotate.
- **DRAW MODE**: Click and drag to manually draw lines on un-detected ducts. 
- **EXPORT JSON**: Saves all annotations (both API-detected and manually drawn) to a `duct_annotations.json` file AND automatically POSTs the layout to the database cache matching the document's MD5 hash, preserving the exact layout state for future reloads.
- **API Hardcoding**: Set `USE_HARDCODED_RESPONSE_API=true` in `.env` to bypass API processing and load annotations directly from `sample/response_hardcoded.json` for frontend testing and debugging.

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
  "document_id": "6560d6fde3f89dc408393dca4eef8082",
  "document_name": "testset2.pdf",
  "page_count": 2,
  "duct_count": 14,
  "annotations": [
    {
      "id": 0,
      "bbox": { "x0": 120.5, "y0": 200.1, "x1": 340.0, "y1": 230.4, "page": 0 },
      "label": "24x12",
      "pressure_class": "LOW",
      "dimension": "24x12",
      "material": "galvanized steel",
      "confidence": 0.93,
      "orientation": "horizontal",
      "source": "api",
      "line": null
    },
    {
      "id": -1,
      "bbox": { "x0": 550.0, "y0": 100.0, "x1": 550.0, "y1": 300.0, "page": 0 },
      "label": "18\"⌀",
      "pressure_class": "HIGH",
      "dimension": "18\"⌀",
      "material": null,
      "confidence": 0.95,
      "orientation": "vertical",
      "source": "auto_centerline",
      "line": { "x1": 550.0, "y1": 100.0, "x2": 550.0, "y2": 300.0 }
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

### `POST /api/v1/manual-annotations/bulk`

Bulk clear and replace ALL manual-annotations natively bound to a document MD5 hash at once.

**Request** – `application/json`

```jsonc
{
       "document_id": "6560d6fde3f89dc408393dca4eef8082",
       "document_name": "testset2.pdf",
       "annotations": [
              {
                     "bbox": { "x0": 1296.0, "y0": 1192.0, "x1": 1709.0, "y1": 1213.0, "page": 0 },
                     "label": "14\"⌀",
                     "pressure_class": "HIGH",
                     "dimension": "14\"⌀",
                     "material": null,
                     "confidence": 1.0,
                     "orientation": "horizontal",
                     "source": "manual",
                     "line": { "x1": 1296.0, "y1": 1192.0, "x2": 1709.0, "y2": 1213.0 }
              }
       ]
}
```

**Response** – `application/json` (Same as `GET /api/v1/manual-annotations/{document_id}`)

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
              "orientation": "horizontal",
              "source": "manual",
              "line": { "x1": 1296.0, "y1": 1192.0, "x2": 1709.0, "y2": 1213.0 }
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
       "line": { "x1": 1296.0, "y1": 1192.0, "x2": 1709.0, "y2": 1213.0 },
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
                     "line": { "x1": 1296.0, "y1": 1192.0, "x2": 1709.0, "y2": 1213.0 },
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
              "orientation": "horizontal",
              "source": "manual",
              "line": { "x1": 1300.0, "y1": 1190.0, "x2": 1715.0, "y2": 1215.0 }
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
| `GITHUB_TOKEN` | `None` | Automatically activates GitHub Models API |
| `ANTHROPIC_API_KEY` | `None` | Automatically activates the Anthropic Claude API |
| `OPENAI_API_KEY` | `None` | Automatically activates the OpenAI API |
| `GPT_MODEL` | `gpt-4o` | Inference model to use (`gpt-4o`, `claude-3-5-sonnet-20241022`, etc) |
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
│   ├── main.py                  # FastAPI app factory with StaticFiles mount
│   ├── core/
│   │   └── config.py            # pydantic-settings config
│   ├── models/
│   │   └── schemas.py           # Pydantic request/response models
│   ├── routers/
│   │   └── annotations.py       # API endpoints (/annotate, /manual-annotations)
│   └── services/
│       ├── pdf_parser.py        # PyMuPDF line + text extraction
│       ├── duct_detector.py     # Parallel-line duct detection
│       ├── image_cropper.py     # PDF region → PNG crop
│       ├── duct_text_extractor.py # Rules-based text extraction + pressure rules
│       ├── manual_annotation_store.py # SQLite CRUD ops for manual corrections
│       └── gpt_analyzer.py      # GPT-4o vision analysis
├── viewer/
│   └── duct_annotator.html      # Interactive frontend web UI
├── sample/
│   ├── response_hardcoded.json  # Fallback JSON structure for UI debugging
│   └── testset2.pdf             # Example PDF drawing
├── requirements.txt
├── Dockerfile                   # Docker build instructions
├── docker-compose.yml           # Compose file for API + OCR service
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