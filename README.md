# HVAC Duct Annotation System

A FastAPI backend that accepts an HVAC mechanical-drawing PDF, detects duct
regions using PyMuPDF vector geometry, analyses each region with GPT-4o
(via GitHub Models Marketplace or OpenAI), and returns structured annotations.

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
  GPT-4o Analyzer (GitHub Models / OpenAI)
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
# Edit .env and set GITHUB_TOKEN or OPENAI_API_KEY
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
| `RENDER_DPI` | `150` | PDF render resolution for crops |
| `DUCT_MIN_GAP` | `4.0` | Min parallel-line gap (PDF points) |
| `DUCT_MAX_GAP` | `200.0` | Max parallel-line gap (PDF points) |
| `MAX_UPLOAD_SIZE_MB` | `50` | Maximum PDF upload size |

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
│       └── gpt_analyzer.py      # GPT-4o vision analysis
├── requirements.txt
├── .env.example
└── README.md
```
