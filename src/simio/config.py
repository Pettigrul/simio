"""Centralised configuration.

All environment-specific values (GCP project, region, model names, API keys, budgets) are
read from environment variables or a local ``.env`` file. Nothing corporate is hard-coded.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DISCOVERY_SOURCES = ("grounding", "brave", "serper", "urlscan", "crtsh", "typosquat", "lens")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIMIO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ---- Vertex AI / Gemini -------------------------------------------------------------
    gcp_project: str | None = Field(default=None, description="GCP project for Vertex AI")
    gcp_location: str = Field(default="global", description="Vertex AI location/region")
    gcp_credentials_path: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("SIMIO_GCP_CREDENTIALS_PATH", "GOOGLE_APPLICATION_CREDENTIALS"),
        description="Optional path to ADC credentials file (sets GOOGLE_APPLICATION_CREDENTIALS)",
    )
    analysis_model: str | None = Field(default=None, description="Multimodal analysis model")
    review_model: str | None = Field(default=None, description="Reviewer model")
    discovery_model: str | None = Field(
        default=None, description="Model used for Google Search grounding (defaults to analysis)"
    )
    llm_max_output_tokens: int = Field(default=4096, ge=256, le=65536)
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_timeout_s: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0, le=10)

    # ---- Budgets --------------------------------------------------------------------------
    max_llm_requests: int = Field(default=300, ge=0, description="Per-campaign LLM call budget")
    max_llm_tokens: int = Field(default=2_000_000, ge=0, description="Per-campaign token budget")
    max_search_queries: int = Field(default=40, ge=0, description="Discovery query budget")
    coban_seed_limit: int = Field(
        default=0,
        ge=0,
        description="Registered correspondents searched per campaign (H9; most recent first, rotating across "
        "campaigns). 0 disables. Requires SIMIO_COBAN_REGISTRY_PATH",
    )
    coban_seed_batch: int = Field(default=4, ge=1, le=10, description="Correspondents per H9 search prompt")

    # ---- Concurrency / pipeline ----------------------------------------------------------
    workers: int = Field(default=3, ge=1, le=16)
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_recollects: int = Field(default=1, ge=0, le=3)
    retry_base_delay_s: float = Field(default=2.0, ge=0)

    # ---- Optional discovery connectors (never required) ----------------------------------
    brave_api_key: SecretStr | None = None
    serper_api_key: SecretStr | None = None
    urlscan_api_key: SecretStr | None = None
    serper_pages: int = Field(
        default=1, ge=1, le=10, description="Serper result pages (10 results / 1 credit each) per query"
    )
    serper_plain_variants: int = Field(
        default=3,
        ge=0,
        le=6,
        description="When Google hides most results of a dork behind opaque links, re-run up to N "
        "operator-free versions of it (1 credit each; 0 disables)",
    )
    min_queue_score: float = Field(
        default=-10.0, description="Candidates below this pre-collection score are not analysed (noise floor)"
    )
    http_timeout_s: float = Field(default=20.0, gt=0)
    # ---- Domain intelligence of flagged pages (RDAP + DNS over HTTPS; public, no key) ----
    domain_intel: bool = Field(default=True, description="Collect RDAP/DNS/hosting data of flagged pages")
    rdap_base_url: str = "https://rdap.org"
    doh_url: str = "https://cloudflare-dns.com/dns-query"

    # ---- Browser / evidence --------------------------------------------------------------
    nav_timeout_ms: int = Field(default=30_000, ge=1000)
    page_budget_s: float = Field(default=90.0, gt=0, description="Hard cap per page collection")
    settle_ms: int = Field(default=2500, ge=0)
    viewport_width: int = 1366
    viewport_height: int = 900
    max_fullpage_height: int = Field(default=12_000, ge=900)
    max_html_bytes: int = Field(default=2_000_000, ge=10_000)
    max_text_chars: int = Field(default=200_000, ge=1000)
    max_requests_per_page: int = Field(default=400, ge=10)
    browser_sandbox: bool = Field(
        default=True, description="Chromium OS sandbox; disable only where it cannot start (documented risk)"
    )
    browser_proxy_pin_ip: bool = Field(
        default=True,
        description="Send the validated IP (not the hostname) to the upstream proxy (anti DNS-rebinding)",
    )
    browser_proxy: str | None = Field(default=None, description="e.g. http://proxy:8080")
    browser_user_agent: str | None = None
    ignore_https_errors: bool = False
    allowed_ports: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [80, 443, 8080, 8443])
    # Explicit host allow-list that bypasses the private-network block. TESTING ONLY.
    unsafe_allow_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---- Data locations ------------------------------------------------------------------
    output_dir: Path = Field(default=Path("output"))
    coban_registry_path: Path | None = Field(
        default=None, description="CSV with authorised correspondents (CNPJ,name,...)"
    )
    coban_registry_authoritative: bool = Field(
        default=False,
        description="Set true ONLY when the registry CSV is an official/approved source (enables CONFIRMED_COBAN)",
    )
    coban_registry_reference_date: str | None = Field(
        default=None, description="Reference date of the registry snapshot, shown in reports"
    )
    contract_requirements_path: Path | None = Field(
        default=None, description="Optional TOML with contractual requirements"
    )
    brand_file: Path | None = None

    # ---- Modes ---------------------------------------------------------------------------
    offline: bool = False
    log_level: str = "INFO"

    @field_validator(
        "gcp_project",
        "gcp_credentials_path",
        "analysis_model",
        "review_model",
        "discovery_model",
        "brave_api_key",
        "serper_api_key",
        "urlscan_api_key",
        "browser_proxy",
        "browser_user_agent",
        "coban_registry_path",
        "coban_registry_reference_date",
        "contract_requirements_path",
        "brand_file",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("allowed_ports", "unsafe_allow_hosts", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            items = [x.strip() for x in v.split(",") if x.strip()]
            return items
        return v

    def model_post_init(self, __context: object) -> None:
        if self.gcp_credentials_path is not None and self.gcp_credentials_path.is_file():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(self.gcp_credentials_path)

    # ---- Derived -------------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.output_dir / "simio.db"

    @property
    def effective_discovery_model(self) -> str | None:
        return self.discovery_model or self.analysis_model

    @property
    def effective_review_model(self) -> str | None:
        return self.review_model or self.analysis_model

    def vertex_configured(self) -> bool:
        return bool(self.gcp_project and self.analysis_model)

    def redacted(self) -> dict[str, object]:
        """Settings snapshot safe for logs/reports (no secret values)."""
        data = self.model_dump(mode="json")
        for key in ("brave_api_key", "serper_api_key", "urlscan_api_key"):
            data[key] = "***set***" if getattr(self, key) else None
        if self.browser_proxy:
            from simio.logging_setup import redact

            data["browser_proxy"] = redact(self.browser_proxy)
        return data


_REPO_ENV = Path(__file__).resolve().parents[2] / ".env"


def load_settings(**overrides: object) -> Settings:
    clean: dict[str, object] = {k: v for k, v in overrides.items() if v is not None}
    if not Path(".env").is_file() and _REPO_ENV.is_file():
        clean["_env_file"] = _REPO_ENV
    return Settings(**clean)  # type: ignore[arg-type]
