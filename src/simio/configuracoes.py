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

from simio.nucleo.caminhos import DIR_SAIDA_PADRAO, arquivo_banco

FONTES_DESCOBERTA = ("grounding", "brave", "serper", "urlscan", "crtsh", "typosquat", "lens")
# Secret settings, by current (PT) and former (EN) names: never logged or reported in clear.
CHAVES_SENSIVEIS = (
    "chave_api_brave",
    "chave_api_serper",
    "chave_api_urlscan",
    "brave_api_key",
    "serper_api_key",
    "urlscan_api_key",
)


class Configuracoes(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIMIO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ---- Vertex AI / Gemini -------------------------------------------------------------
    projeto_gcp: str | None = Field(default=None, description="GCP project for Vertex AI")
    local_gcp: str = Field(default="global", description="Vertex AI location/region")
    caminho_credenciais_gcp: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("SIMIO_CAMINHO_CREDENCIAIS_GCP", "GOOGLE_APPLICATION_CREDENTIALS"),
        description="Optional path to ADC credentials file (sets GOOGLE_APPLICATION_CREDENTIALS)",
    )
    modelo_analise: str | None = Field(default=None, description="Multimodal analysis model")
    modelo_revisao: str | None = Field(default=None, description="Reviewer model")
    modelo_descoberta: str | None = Field(
        default=None, description="Model used for Google Search grounding (defaults to analysis)"
    )
    llm_max_tokens_saida: int = Field(default=4096, ge=256, le=65536)
    llm_temperatura: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_timeout_s: float = Field(default=120.0, gt=0)
    llm_max_retentativas: int = Field(default=3, ge=0, le=10)

    # ---- Budgets --------------------------------------------------------------------------
    max_requisicoes_llm: int = Field(default=300, ge=0, description="Per-campaign LLM call budget")
    max_tokens_llm: int = Field(default=2_000_000, ge=0, description="Per-campaign token budget")
    max_consultas_busca: int = Field(default=40, ge=0, description="Discovery query budget")
    limite_sementes_coban: int = Field(
        default=0,
        ge=0,
        description="Registered correspondents searched per campaign (H9; most recent first, rotating across "
        "campaigns). 0 disables. Requires SIMIO_CAMINHO_CADASTRO_COBAN",
    )
    lote_sementes_coban: int = Field(default=4, ge=1, le=10, description="Correspondents per H9 search prompt")

    # ---- Concurrency / pipeline ----------------------------------------------------------
    paralelismo: int = Field(default=3, ge=1, le=16)
    max_tentativas: int = Field(default=3, ge=1, le=10)
    max_recoletas: int = Field(default=1, ge=0, le=3)
    atraso_base_retentativa_s: float = Field(default=2.0, ge=0)

    # ---- Optional discovery connectors (never required) ----------------------------------
    chave_api_brave: SecretStr | None = None
    chave_api_serper: SecretStr | None = None
    chave_api_urlscan: SecretStr | None = None
    paginas_serper: int = Field(
        default=1, ge=1, le=10, description="Serper result pages (10 results / 1 credit each) per query"
    )
    variantes_simples_serper: int = Field(
        default=3,
        ge=0,
        le=6,
        description="When Google hides most results of a dork behind opaque links, re-run up to N "
        "operator-free versions of it (1 credit each; 0 disables)",
    )
    pontuacao_minima_fila: float = Field(
        default=-10.0, description="Candidates below this pre-collection score are not analysed (noise floor)"
    )
    timeout_http_s: float = Field(default=20.0, gt=0)
    # ---- Domain intelligence of flagged pages (RDAP + DNS over HTTPS; public, no key) ----
    inteligencia_dominio: bool = Field(default=True, description="Collect RDAP/DNS/hosting data of flagged pages")
    url_base_rdap: str = "https://rdap.org"
    url_doh: str = "https://cloudflare-dns.com/dns-query"

    # ---- Browser / evidence --------------------------------------------------------------
    timeout_navegacao_ms: int = Field(default=30_000, ge=1000)
    orcamento_pagina_s: float = Field(default=90.0, gt=0, description="Hard cap per page collection")
    espera_estabilizar_ms: int = Field(default=2500, ge=0)
    largura_viewport: int = 1366
    altura_viewport: int = 900
    max_altura_pagina_inteira: int = Field(default=12_000, ge=900)
    max_bytes_html: int = Field(default=2_000_000, ge=10_000)
    max_caracteres_texto: int = Field(default=200_000, ge=1000)
    max_requisicoes_por_pagina: int = Field(default=400, ge=10)
    sandbox_navegador: bool = Field(
        default=True, description="Chromium OS sandbox; disable only where it cannot start (documented risk)"
    )
    proxy_navegador_fixar_ip: bool = Field(
        default=True,
        description="Send the validated IP (not the hostname) to the upstream proxy (anti DNS-rebinding)",
    )
    proxy_navegador: str | None = Field(default=None, description="e.g. http://proxy:8080")
    user_agent_navegador: str | None = None
    ignorar_erros_https: bool = False
    portas_permitidas: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [80, 443, 8080, 8443])
    # Explicit host allow-list that bypasses the private-network block. TESTING ONLY.
    hosts_liberados_inseguro: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---- Data locations ------------------------------------------------------------------
    dir_saida: Path = Field(default=Path(DIR_SAIDA_PADRAO))
    caminho_cadastro_coban: Path | None = Field(
        default=None, description="CSV with authorised correspondents (CNPJ,name,...)"
    )
    cadastro_coban_oficial: bool = Field(
        default=False,
        description="Set true ONLY when the registry CSV is an official/approved source (enables COBAN_CONFIRMADO)",
    )
    data_referencia_cadastro_coban: str | None = Field(
        default=None, description="Reference date of the registry snapshot, shown in reports"
    )
    caminho_requisitos_contratuais: Path | None = Field(
        default=None, description="Optional TOML with contractual requirements"
    )
    arquivo_marca: Path | None = None

    # ---- Modes ---------------------------------------------------------------------------
    offline: bool = False
    nivel_log: str = "INFO"

    @field_validator(
        "projeto_gcp",
        "caminho_credenciais_gcp",
        "modelo_analise",
        "modelo_revisao",
        "modelo_descoberta",
        "chave_api_brave",
        "chave_api_serper",
        "chave_api_urlscan",
        "proxy_navegador",
        "user_agent_navegador",
        "caminho_cadastro_coban",
        "data_referencia_cadastro_coban",
        "caminho_requisitos_contratuais",
        "arquivo_marca",
        mode="before",
    )
    @classmethod
    def _vazio_para_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("portas_permitidas", "hosts_liberados_inseguro", mode="before")
    @classmethod
    def _separar_csv(cls, v: object) -> object:
        if isinstance(v, str):
            itens = [x.strip() for x in v.split(",") if x.strip()]
            return itens
        return v

    def model_post_init(self, __context: object) -> None:
        if self.caminho_credenciais_gcp is not None and self.caminho_credenciais_gcp.is_file():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(self.caminho_credenciais_gcp)

    # ---- Derived -------------------------------------------------------------------------
    @property
    def caminho_db(self) -> Path:
        return arquivo_banco(self.dir_saida)

    @property
    def modelo_descoberta_efetivo(self) -> str | None:
        return self.modelo_descoberta or self.modelo_analise

    @property
    def modelo_revisao_efetivo(self) -> str | None:
        return self.modelo_revisao or self.modelo_analise

    def vertex_configurado(self) -> bool:
        return bool(self.projeto_gcp and self.modelo_analise)

    def mascarada(self) -> dict[str, object]:
        """Settings snapshot safe for logs/reports (no secret values)."""
        dados = self.model_dump(mode="json")
        for chave in CHAVES_SENSIVEIS:
            if chave in dados:
                dados[chave] = "***set***" if getattr(self, chave, None) else None
        if self.proxy_navegador:
            from simio.config_logs import mascarar

            dados["proxy_navegador"] = mascarar(self.proxy_navegador)
        return dados


_ENV_REPO = Path(__file__).resolve().parents[2] / ".env"


def carregar_configuracoes(**sobrescritas: object) -> Configuracoes:
    limpo: dict[str, object] = {k: v for k, v in sobrescritas.items() if v is not None}
    if not Path(".env").is_file() and _ENV_REPO.is_file():
        limpo["_env_file"] = _ENV_REPO
    return Configuracoes(**limpo)  # type: ignore[arg-type]
