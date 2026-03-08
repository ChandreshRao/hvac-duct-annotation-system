# OCR Service (Docker)

Standalone OCR microservice for HVAC Duct Annotation System.

## Run

```bash
docker compose up -d ocr-service
```

## Health

```bash
curl http://localhost:8081/health
```

## OCR endpoint

`POST /ocr` with multipart file field `file` (image).

Returns JSON:

```json
{
  "spans": [
    { "text": "24X18", "bbox": [10, 20, 60, 40], "confidence": 0.93 }
  ]
}
```
