"""
Application configuration loaded from environment variables / .env file.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Authentication – supply ONE of the tokens.
    # GITHUB_TOKEN takes precedence and routes to GitHub Models.
    # ANTHROPIC_API_KEY routes to Anthropic Claude models.
    # OPENAI_API_KEY routes to the standard OpenAI endpoint.
    # ------------------------------------------------------------------
    github_token: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # ------------------------------------------------------------------
    # GPT-4o model identifier
    # GitHub Models: "gpt-4o"
    # OpenAI direct: "gpt-4o" or "gpt-4o-2024-11-20" etc.
    # ------------------------------------------------------------------
    gpt_model: str = "gpt-4o"

    # ------------------------------------------------------------------
    # Request timeout for GPT-4o calls (seconds)
    # ------------------------------------------------------------------
    gpt_timeout_seconds: float = 60.0
    enable_gpt_fallback: bool = False

    # ------------------------------------------------------------------
    # OCR extraction settings (optional, disabled by default)
    # ------------------------------------------------------------------
    enable_ocr_extraction: bool = False
    ocr_language: str = "eng"
    ocr_dpi: int = 300
    use_ocr_service: bool = False
    ocr_service_url: str = "http://localhost:8081/ocr"
    ocr_service_timeout_seconds: float = 30.0

    # ------------------------------------------------------------------
    # Rules-based text matching tuning (candidate ↔ label association)
    # ------------------------------------------------------------------
    text_context_radius_px: float = 100.0
    text_match_max_distance_px: float = 60.0
    text_match_bbox_margin_px: float = 24.0
    max_candidates_per_text_annotation: int = 1

    # ------------------------------------------------------------------
    # Duct detection tuning (can be overridden via env)
    # ------------------------------------------------------------------
    duct_min_gap: float = 4.0
    duct_max_gap: float = 200.0
    render_dpi: int = 150

    # ------------------------------------------------------------------
    # API settings
    # ------------------------------------------------------------------
    max_upload_size_mb: int = 50
    manual_annotations_db_path: str = "data/manual_annotations.db"

    def validate_auth(self) -> None:
        if not self.github_token and not self.openai_api_key:
            raise RuntimeError(
                "No API key configured. "
                "Set GITHUB_TOKEN (GitHub Models) or OPENAI_API_KEY (OpenAI) in your .env file."
            )


settings = Settings()
