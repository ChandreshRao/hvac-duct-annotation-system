"""
GPT-4o Analyzer Service
=======================
Sends cropped duct images to GitHub Models / OpenAI GPT-4o with vision
and parses the structured JSON response.

GitHub Models endpoint:
  https://models.inference.ai.azure.com

Standard OpenAI endpoint:
  https://api.openai.com/v1

The choice is controlled by the GITHUB_TOKEN / OPENAI_API_KEY environment
variables.  If GITHUB_TOKEN is set, the GitHub Models endpoint is used.
If OPENAI_API_KEY is set, the OpenAI endpoint is used.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

import httpx

from app.core.config import settings
from app.models.schemas import DuctCandidate, GPTDuctAnalysis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an expert HVAC engineer reviewing mechanical drawings. "
    "You will be shown a cropped region of an HVAC ductwork drawing. "
    "Extract any duct annotations you can find and return ONLY a valid JSON "
    "object with the following fields:\n"
    "  dimension     – duct size as a string e.g. '24x12', '18\" dia', or null\n"
    "  pressure_class – pressure rating e.g. '0.5\"wg', '1\"wg', '2\"wg', or null\n"
    "  material      – duct material e.g. 'galvanized steel', 'flexible duct', "
    "'fiberglass', 'stainless steel', or null\n"
    "  confidence    – a float between 0.0 and 1.0 reflecting how confident you are\n"
    "Return ONLY the raw JSON object, no markdown fences or extra text."
)

USER_PROMPT = (
    "Analyse this duct section from an HVAC mechanical drawing. "
    "Return the JSON object described in the system prompt."
)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_GITHUB_BASE = "https://models.inference.ai.azure.com"
_OPENAI_BASE = "https://api.openai.com/v1"


def _build_headers() -> dict[str, str]:
    if settings.github_token:
        return {
            "Authorization": f"Bearer {settings.github_token}",
            "Content-Type": "application/json",
        }
    return {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }


def _build_url() -> str:
    if settings.github_token:
        return f"{_GITHUB_BASE}/chat/completions"
    return f"{_OPENAI_BASE}/chat/completions"


def _build_payload(image_b64: str, nearby_text: list[str]) -> dict[str, Any]:
    context_hint = ""
    if nearby_text:
        context_hint = (
            f"\n\nNearby drawing text that may contain labels: "
            + ", ".join(f'"{t}"' for t in nearby_text[:10])
        )
    return {
        "model": settings.gpt_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                    {
                        "type": "text",
                        "text": USER_PROMPT + context_hint,
                    },
                ],
            },
        ],
        "max_tokens": 256,
        "temperature": 0.0,
    }


def _parse_gpt_response(content: str) -> GPTDuctAnalysis:
    """Extract JSON from the model response and coerce into GPTDuctAnalysis."""
    # Strip markdown code fences if present
    content = re.sub(r"```(?:json)?", "", content).strip().rstrip("`").strip()
    try:
        data = json.loads(content)
        return GPTDuctAnalysis(
            dimension=data.get("dimension"),
            pressure_class=data.get("pressure_class"),
            material=data.get("material"),
            confidence=float(data.get("confidence", 0.0)),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Could not parse GPT response: %s | raw: %s", exc, content[:200])
        return GPTDuctAnalysis(confidence=0.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def analyze_duct_crop(
    image_bytes: bytes,
    candidate: DuctCandidate,
    client: httpx.AsyncClient,
) -> GPTDuctAnalysis:
    """
    Send a single crop to GPT-4o and return the parsed analysis.
    """
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = _build_payload(image_b64, candidate.nearby_text)
    headers = _build_headers()
    url = _build_url()

    try:
        resp = await client.post(
            url,
            headers=headers,
            json=payload,
            timeout=settings.gpt_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_gpt_response(content)
    except httpx.HTTPStatusError as exc:
        logger.error(
            "GPT-4o HTTP error for candidate %d: %s",
            candidate.id, exc.response.text[:400],
        )
    except Exception as exc:
        logger.error("GPT-4o call failed for candidate %d: %s", candidate.id, exc)

    return GPTDuctAnalysis(confidence=0.0)


async def analyze_all_crops(
    crops: dict[int, bytes],
    candidates_by_id: dict[int, DuctCandidate],
    concurrency: int = 4,
) -> dict[int, GPTDuctAnalysis]:
    """
    Analyze all cropped duct images concurrently.

    Parameters
    ----------
    crops:
        Mapping of candidate_id → PNG bytes.
    candidates_by_id:
        Mapping of candidate_id → DuctCandidate (for nearby_text context).
    concurrency:
        Maximum number of simultaneous GPT-4o requests.

    Returns
    -------
    Mapping of candidate_id → GPTDuctAnalysis.
    """
    results: dict[int, GPTDuctAnalysis] = {}
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:

        async def _analyze_one(cand_id: int, img: bytes) -> None:
            async with semaphore:
                cand = candidates_by_id.get(cand_id)
                if cand is None:
                    return
                analysis = await analyze_duct_crop(img, cand, client)
                results[cand_id] = analysis
                logger.debug(
                    "Candidate %d → dim=%s pc=%s mat=%s conf=%.2f",
                    cand_id,
                    analysis.dimension,
                    analysis.pressure_class,
                    analysis.material,
                    analysis.confidence,
                )

        await asyncio.gather(*[_analyze_one(cid, img) for cid, img in crops.items()])

    logger.info("GPT-4o analysis complete: %d / %d crops processed", len(results), len(crops))
    return results
