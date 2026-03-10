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
  MD5 Hasher & Cache Lookup
  • Generates MD5 hash of uploaded PDF
  • Checks SQLite 'manual_annotations_v2' DB for existing records
  • ON CACHE HIT: Returns AnnotationResponse instantly (skips extraction)
  • ON CACHE MISS: Proceeds to extraction pipeline
         │
         ▼
  PDFParser (PyMuPDF)
         ▼
  TextExtractor & OCR Pipeline
  • Extract embedded text blocks (PyMuPDF)
  • Merge Document AI JSON (sidecar 'document.json') for high-fidelity OCR
  • If text is missing/sparse, fallback to OCR Service (Tesseract)
         │
         ▼
  DuctDetector
  • Find pairs of parallel lines (horizontal & vertical)
  • Determine exact centerlines using CenterlineTracer (extends to junctions)
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
  • Associate extracted/OCR/DocAI text spans near geometry
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
# Edit .env; set ENABLE_GPT_FALLBACK=true to use GPT fallback.
# If fallback is enabled, also set GITHUB_TOKEN or OPENAI_API_KEY.
# Optional: set ENABLE_OCR_EXTRACTION=true when PDF text is not embedded.
# Optional Docker OCR service: set USE_OCR_SERVICE=true and run `docker compose up -d ocr-service`.
```

### 3. Run with Docker (Recommended)

The system is configured to persist annotations and automatically seed data on startup.

```bash
docker compose up --build -d
```

- **Persistence**: Database is stored in `./data/manual_annotations_v2.db` and persists across container rebuilds.
- **Auto-Seeding**: On every startup, `bootstrap_db.py` runs automatically to seed annotations for `testset2.pdf` from `sample/response_hardcoded.json`.

### 4. Use the Viewer

The frontend UI is served automatically by the FastAPI backend at:
**http://localhost:8000/viewer/duct_annotator.html**

Features:
- **UPLOAD PDF**: Drag & drop or select an HVAC PDF to annotate.
- **DRAW MODE**: Click and drag to manually draw lines on un-detected ducts. 
- **EXPORT JSON**: Saves all annotations (both API-detected and manually drawn) to the database cache matching the document's MD5 hash.
- **API Hardcoding**: Set `USE_HARDCODED_RESPONSE_API=true` in `.env` to bypass API processing and load annotations directly from `sample/response_hardcoded.json`.

---

## Document AI Integration

The system can ingest high-fidelity text extraction from **Google Cloud Document AI**. To use this:
1. Place a `document.json` (exported from Document AI) in the same directory as your PDF.
2. The `DuctTextExtractor` will automatically parse this file and merge its text blocks.
3. This is particularly useful for capturing round duct dimensions (e.g., 14", 12") that standard PDF text extraction might miss.

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
      "source": "centerline_traced",
      "line": { "x1": 550.0, "y1": 100.0, "x2": 550.0, "y2": 300.0 }
    }
  ]
}
```

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | – | GitHub PAT for GitHub Models endpoint |
| `OPENAI_API_KEY` | – | OpenAI API key (used if GITHUB_TOKEN not set) |
| `ENABLE_GPT_FALLBACK` | `false` | Enables GPT-4o fallback |
| `MANUAL_ANNOTATIONS_DB_PATH` | `data/manual_annotations_v2.db` | SQLite path |

---

## Project Structure

```
hvac-duct-annotation-system/
├── app/
│   ├── main.py                  # FastAPI app entrypoint
│   ├── core/
│   │   └── config.py            # pydantic-settings config
│   ├── models/
│   │   └── schemas.py           # Pydantic models
│   ├── routers/
│   │   └── annotations.py       # API endpoints
│   └── services/
│       ├── pdf_parser.py        # PyMuPDF line extraction
│       ├── duct_detector.py     # Parallel-line detection
│       ├── centerline_tracer.py # Robust centerline junction tracing
│       ├── image_cropper.py     # PDF region → PNG crop
│       ├── duct_text_extractor.py # Rules-based text OCR association
│       ├── document_ai_parser.py # Google Cloud Document AI JSON parser
│       ├── manual_annotation_store.py # SQLite CRUD ops
│       └── gpt_analyzer.py      # GPT-4o vision analysis
├── viewer/
│   └── duct_annotator.html      # Interactive frontend web UI
├── sample/
│   ├── response_hardcoded.json  # Reference results
│   └── testset2.pdf             # Example drawing
├── scripts/
│   └── start.sh                 # Docker entrypoint (auto-seeds DB)
├── bootstrap_db.py              # Script to seed manual_annotations_v2.db
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## OCR Service (Docker)

Run a standalone OCR service:

```bash
docker compose up -d ocr-service
```
