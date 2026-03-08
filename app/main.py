"""
HVAC Duct Annotation System – FastAPI Application Entry Point
"""

from __future__ import annotations

import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers.annotations import router as annotations_router
from app.services.manual_annotation_store import initialize_manual_annotation_store

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(
        title="HVAC Duct Annotation System",
        description=(
            "Upload an HVAC mechanical drawing PDF. "
            "The API extracts vector geometry, detects duct regions, "
            "analyses each region with GPT-4o, and returns structured annotations."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS – adjust origins as needed for your frontend
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(annotations_router)

    @app.on_event("startup")
    async def _startup() -> None:
        initialize_manual_annotation_store()
        logger.info("HVAC Duct Annotation System started.")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        logger.info("HVAC Duct Annotation System shutting down.")

    return app


app = create_app()
