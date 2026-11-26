from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from simio.nucleo.caminhos import DIR_SAIDA_PADRAO, arquivo_banco

FONTES_DESCOBERTA = ("grounding", "brave", "serper", "urlscan", "crtsh", "typosquat", "lens")
# Nomes atuais e antigos (em inglês) das chaves secretas: nunca aparecem em log ou relatório.
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

    projeto_gcp: str | None = Field(default=None, description="Projeto GCP do Vertex AI")
    local_gcp: str = Field(default="global", description="Região do Vertex AI")
    caminho_credenciais_gcp: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("SIMIO_CAMINHO_CREDENCIAIS_GCP", "GOOGLE_APPLICATION_CREDENTIALS"),
        description="Arquivo de credenciais ADC (vira GOOGLE_APPLICATION_CREDENTIALS)",
    )
    modelo_analise: str | None = Field(default=None, description="Modelo multimodal de análise")
    modelo_revisao: str | None = Field(default=None, description="Modelo do revisor")
    modelo_descoberta: str | None = Field(
        default=None, description="Modelo do Google Search Grounding (padrão: o de análise)"
    )
    llm_max_tokens_saida: int = Field(default=4096, ge=256, le=65536)
    llm_temperatura: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_timeout_s: float = Field(default=120.0, gt=0)
    llm_max_retentativas: int = Field(default=3, ge=0, le=10)

    max_requisicoes_llm: int = Field(default=300, ge=0, description="Limite de chamadas ao LLM por campanha")
    max_tokens_llm: int = Field(default=2_000_000, ge=0, description="Limite de tokens por campanha")
    max_consultas_busca: int = Field(default=40, ge=0, description="Limite de consultas de descoberta")
    limite_sementes_coban: int = Field(
        default=0,
        ge=0,
        description="Correspondentes cadastrados pesquisados por campanha (H9; mais recentes primeiro, em "
        "rodízio entre campanhas). 0 desliga. Precisa de SIMIO_CAMINHO_CADASTRO_COBAN",
    )
    lote_sementes_coban: int = Field(default=4, ge=1, le=10, description="Correspondentes por prompt de busca H9")

    paralelismo: int = Field(default=3, ge=1, le=16)
    max_tentativas: int = Field(default=3, ge=1, le=10)
    max_recoletas: int = Field(default=1, ge=0, le=3)
    atraso_base_retentativa_s: float = Field(default=2.0, ge=0)

    chave_api_brave: SecretStr | None = None
    chave_api_serper: SecretStr | None = None
    chave_api_urlscan: SecretStr | None = None
    paginas_serper: int = Field(
        default=1, ge=1, le=10, description="Páginas do Serper por consulta (10 resultados e 1 crédito cada)"
    )
    variantes_simples_serper: int = Field(
        default=3,
        ge=0,
        le=6,
        description="Quando o Google esconde a maior parte dos resultados de um dork atrás de links opacos, "
        "refaz até N versões sem operadores (1 crédito cada; 0 desliga)",
    )
    pontuacao_minima_fila: float = Field(
        default=-10.0, description="Candidatas abaixo desta pontuação (antes da coleta) não são analisadas"
    )
    timeout_http_s: float = Field(default=20.0, gt=0)
    # RDAP e DNS sobre HTTPS são públicos, sem chave.
    inteligencia_dominio: bool = Field(default=True, description="Levanta RDAP, DNS e hospedagem dos achados")
    url_base_rdap: str = "https://rdap.org"
    url_doh: str = "https://cloudflare-dns.com/dns-query"

    timeout_navegacao_ms: int = Field(default=30_000, ge=1000)
    orcamento_pagina_s: float = Field(default=90.0, gt=0, description="Tempo máximo de coleta por página")
    espera_estabilizar_ms: int = Field(default=2500, ge=0)
    largura_viewport: int = 1366
    altura_viewport: int = 900
    max_altura_pagina_inteira: int = Field(default=12_000, ge=900)
    max_bytes_html: int = Field(default=2_000_000, ge=10_000)
    max_caracteres_texto: int = Field(default=200_000, ge=1000)
    max_requisicoes_por_pagina: int = Field(default=400, ge=10)
    sandbox_navegador: bool = Field(
        default=True, description="Sandbox do Chromium; desligue só onde ele não sobe (risco documentado)"
    )
    proxy_navegador_fixar_ip: bool = Field(
        default=True,
        description="Manda ao proxy o IP já validado, não o hostname (contra DNS rebinding)",
    )
    proxy_navegador: str | None = Field(default=None, description="ex.: http://proxy:8080")
    user_agent_navegador: str | None = None
    ignorar_erros_https: bool = False
    portas_permitidas: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [80, 443, 8080, 8443])
    # Hosts liberados do bloqueio de rede privada. Só para teste.
    hosts_liberados_inseguro: Annotated[list[str], NoDecode] = Field(default_factory=list)

    dir_saida: Path = Field(default=Path(DIR_SAIDA_PADRAO))
    caminho_cadastro_coban: Path | None = Field(
        default=None, description="CSV dos correspondentes autorizados (cnpj;nome;...)"
    )
    cadastro_coban_oficial: bool = Field(
        default=False,
        description="true SÓ se o CSV for fonte oficial ou aprovada (habilita COBAN_CONFIRMADO)",
    )
    data_referencia_cadastro_coban: str | None = Field(
        default=None, description="Data de referência da extração do cadastro, mostrada nos relatórios"
    )
    caminho_requisitos_contratuais: Path | None = Field(
        default=None, description="TOML opcional com requisitos contratuais"
    )
    arquivo_marca: Path | None = None

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
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    def model_post_init(self, __context: object) -> None:
        if self.caminho_credenciais_gcp is not None and self.caminho_credenciais_gcp.is_file():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(self.caminho_credenciais_gcp)

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
        """Cópia das configurações sem os valores secretos, para log e relatório."""
        dados = self.model_dump(mode="json")
        for chave in CHAVES_SENSIVEIS:
            if chave in dados:
                dados[chave] = "***definida***" if getattr(self, chave, None) else None
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
