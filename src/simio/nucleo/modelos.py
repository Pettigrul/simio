"""Typed data contracts shared by all agents (validated with Pydantic)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def agora_utc() -> datetime:
    return datetime.now(UTC)


class Estrito(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------
class StatusCandidata(StrEnum):
    DESCOBERTA = "DESCOBERTA"
    NA_FILA = "NA_FILA"
    COLETANDO = "COLETANDO"
    COLETADA = "COLETADA"
    ANALISANDO = "ANALISANDO"
    REVISANDO = "REVISANDO"
    CONCLUIDA = "CONCLUIDA"
    FALHOU = "FALHOU"
    INCONCLUSIVA = "INCONCLUSIVA"


class ClassificacaoFinal(StrEnum):
    SEM_INDICIO = "SEM_INDICIO"
    REQUER_REVISAO = "REQUER_REVISAO"
    INDICIO_FORTE = "INDICIO_FORTE"
    INCONCLUSIVA = "INCONCLUSIVA"


ORDEM_CLASSIFICACAO = {
    ClassificacaoFinal.SEM_INDICIO: 0,
    ClassificacaoFinal.INCONCLUSIVA: 1,
    ClassificacaoFinal.REQUER_REVISAO: 2,
    ClassificacaoFinal.INDICIO_FORTE: 3,
}


class Prioridade(StrEnum):
    BAIXA = "BAIXA"
    MEDIA = "MEDIA"
    ALTA = "ALTA"


ORDEM_PRIORIDADE = {Prioridade.BAIXA: 0, Prioridade.MEDIA: 1, Prioridade.ALTA: 2}


class Nivel(StrEnum):
    """Qualitative confidence level. NOT a probability of irregularity."""

    ALTA = "ALTA"
    MEDIA = "MEDIA"
    BAIXA = "BAIXA"
    NENHUMA = "NENHUMA"


class CasoCenario(StrEnum):
    A_MENCAO_COMERCIAL_LEGITIMA = "A_MENCAO_COMERCIAL_LEGITIMA"
    B_COMUNICACAO_AMBIGUA = "B_COMUNICACAO_AMBIGUA"
    C_POSSIVEL_CANAL_OFICIAL = "C_POSSIVEL_CANAL_OFICIAL"
    D_PERSONIFICACAO_FORTE = "D_PERSONIFICACAO_FORTE"
    E_EVIDENCIA_INSUFICIENTE = "E_EVIDENCIA_INSUFICIENTE"
    NAO_RELEVANTE = "NAO_RELEVANTE"  # brand not observed at all


class SuficienciaEvidencia(StrEnum):
    SUFICIENTE = "SUFICIENTE"
    PARCIAL = "PARCIAL"
    INSUFICIENTE = "INSUFICIENTE"


class ClasseIdentidade(StrEnum):
    COBAN_CONFIRMADO = "COBAN_CONFIRMADO"
    COBAN_POSSIVEL = "COBAN_POSSIVEL"
    OUTRO_TERCEIRO = "OUTRO_TERCEIRO"
    IDENTIDADE_DESCONHECIDA = "IDENTIDADE_DESCONHECIDA"


class Dimensao(StrEnum):
    IDENTIDADE_VISUAL = "IDENTIDADE_VISUAL"
    IDENTIDADE_COMERCIAL_DECLARADA = "IDENTIDADE_COMERCIAL_DECLARADA"
    TRANSPARENCIA_CORRESPONDENTE = "TRANSPARENCIA_CORRESPONDENTE"
    LINGUAGEM_INSTITUCIONAL = "LINGUAGEM_INSTITUCIONAL"
    USO_LOGO = "USO_LOGO"
    JORNADA_ATENDIMENTO = "JORNADA_ATENDIMENTO"
    CANAIS_CONTATO = "CANAIS_CONTATO"
    CONFUSAO_CONSUMIDOR = "CONFUSAO_CONSUMIDOR"


class Sinal(StrEnum):
    CONFUSAO = "CONFUSAO"
    LEGITIMO = "LEGITIMO"
    NEUTRO = "NEUTRO"
    NAO_AVALIADO = "NAO_AVALIADO"


class StatusColeta(StrEnum):
    OK = "OK"
    PARCIAL = "PARCIAL"  # page loaded but something (e.g. full screenshot) failed
    FALHOU = "FALHOU"  # could not load the page (timeout, DNS, HTTP error...)
    BLOQUEADA = "BLOQUEADA"  # refused by the URL security policy
    NAO_COLETADA = "NAO_COLETADA"


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------
class HipoteseBusca(Estrito):
    id: str
    descricao: str
    prompt_grounding: str
    consultas_palavras_chave: list[str] = Field(default_factory=list)


class ResultadoDescoberta(Estrito):
    """One URL observed by one discovery source. Never invented: always from source data."""

    fonte: str
    url_original: str
    consulta: str | None = None
    id_hipotese: str | None = None
    titulo: str | None = None
    trecho: str | None = None
    posicao: int | None = None
    metadados: dict[str, Any] = Field(default_factory=dict)
    descoberto_em: datetime = Field(default_factory=agora_utc)


class RegistroConsulta(Estrito):
    fonte: str
    consulta: str
    id_hipotese: str | None = None
    status: str = "OK"  # OK | ERROR | EMPTY | SKIPPED
    qtd_resultados: int = 0
    erro: str | None = None
    metadados: dict[str, Any] = Field(default_factory=dict)
    executada_em: datetime = Field(default_factory=agora_utc)


class DesfechoDescoberta(Estrito):
    resultados: list[ResultadoDescoberta] = Field(default_factory=list)
    consultas: list[RegistroConsulta] = Field(default_factory=list)
    diagnosticos: list[str] = Field(default_factory=list)
    fontes_usadas: list[str] = Field(default_factory=list)


class Candidata(Estrito):
    id: str
    id_campanha: str
    url: str
    url_normalizada: str
    hostname: str
    dominio_registravel: str
    status: StatusCandidata = StatusCandidata.DESCOBERTA
    pontuacao_prioridade: float = 0.0
    motivos_prioridade: list[str] = Field(default_factory=list)
    tentativas: int = 0
    recoletas: int = 0
    ultimo_erro: str | None = None


# --------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------
class Artefato(Estrito):
    tipo: str  # screenshot_viewport | screenshot_full | html | text | page_data
    caminho: str  # relative to the campaign directory
    sha256: str
    tamanho_bytes: int


class ItemEvidencia(Estrito):
    """An addressable piece of evidence the analysts must cite (e.g. ``E-TXT-003``)."""

    ref: str
    tipo: str  # title | meta | text | heading | nav | cta | link | contact | image | screenshot | domain | network
    local: str  # e.g. "viewport", "header", "footer", "page", "url"
    conteudo: str
    acima_dobra: bool | None = None


class InfoLink(Estrito):
    texto: str
    href: str
    local: str = "page"


class InfoImagem(Estrito):
    alt: str
    src: str
    local: str = "page"
    acima_dobra: bool = False
    largura: int = 0
    altura: int = 0
    pagina_x: int | None = None  # document coordinates (match the full-page screenshot)
    pagina_y: int | None = None


class CorrespondenciaVisual(Estrito):
    """A region of a hashed screenshot that matches a reference logo (deterministic, no model)."""

    tipo: str  # "legacy_logo"
    ref_captura: str  # E-SHOT-VIEWPORT | E-SHOT-FULL
    bbox: list[int]  # x, y, width, height in screenshot pixels
    pontuacao: float
    src_imagem: str | None = None
    acima_dobra: bool = False


class InteligenciaRdap(Estrito):
    registrador: str | None = None
    criado: str | None = None
    expira: str | None = None
    atualizado: str | None = None
    idade_dias: int | None = None
    status: list[str] = Field(default_factory=list)
    servidores_nomes: list[str] = Field(default_factory=list)
    titular: str | None = None  # only for legal entities (LGPD)
    documento_titular: str | None = None  # CNPJ, when published (Registro.br)
    tipo_titular: str = "não publicado"
    emails_abuso: list[str] = Field(default_factory=list)


class InteligenciaIp(Estrito):
    ip: str
    rede: str | None = None
    organizacao: str | None = None
    pais: str | None = None
    emails_abuso: list[str] = Field(default_factory=list)


class InteligenciaDominio(Estrito):
    """Registration, DNS and hosting facts of a flagged page's host (see ``agents/domain_intel.py``)."""

    hostname: str
    dominio_registrado: str
    plataforma: str | None = None
    a: list[str] = Field(default_factory=list)
    aaaa: list[str] = Field(default_factory=list)
    cname: list[str] = Field(default_factory=list)
    ns: list[str] = Field(default_factory=list)
    mx: list[str] = Field(default_factory=list)
    rdap: InteligenciaRdap | None = None
    ips: list[InteligenciaIp] = Field(default_factory=list)
    notas: list[str] = Field(default_factory=list)
    erros: list[str] = Field(default_factory=list)
    coletada_em: str = ""


class InfoCampoEntrada(Estrito):
    """A form field the page asks for (recorded, never filled)."""

    tipo_campo: str = ""
    nome: str = ""
    id: str = ""
    placeholder: str = ""
    autocomplete: str = ""
    rotulo: str = ""


class DadosPagina(Estrito):
    titulo: str = ""
    meta: dict[str, str] = Field(default_factory=dict)
    canonica: str | None = None
    idioma_pagina: str | None = None
    titulos_secao: list[str] = Field(default_factory=list)
    itens_nav: list[InfoLink] = Field(default_factory=list)
    ctas: list[InfoLink] = Field(default_factory=list)
    links_contato: list[InfoLink] = Field(default_factory=list)
    links: list[InfoLink] = Field(default_factory=list)
    imagens: list[InfoImagem] = Field(default_factory=list)
    texto_cabecalho: str = ""
    texto_rodape: str = ""
    texto_acima_dobra: str = ""
    texto_visivel: str = ""
    formularios_presentes: int = 0
    campos_entrada: list[InfoCampoEntrada] = Field(default_factory=list)
    acoes_formulario: list[str] = Field(default_factory=list)


class Evidencia(Estrito):
    id_candidata: str
    tentativa: int
    status: StatusColeta
    url_original: str
    url_final: str | None = None
    cadeia_redirecionamentos: list[str] = Field(default_factory=list)
    status_http: int | None = None
    ip_servidor: str | None = None
    coletada_em: datetime = Field(default_factory=agora_utc)
    pagina: DadosPagina | None = None
    artefatos: list[Artefato] = Field(default_factory=list)
    requisicoes_bloqueadas: list[str] = Field(default_factory=list)
    erros: list[str] = Field(default_factory=list)
    nao_inspecionado: list[str] = Field(default_factory=list)
    repetivel: bool = False
    caminho_manifesto: str | None = None
    sha256_manifesto: str | None = None

    @property
    def utilizavel(self) -> bool:
        return self.status in (StatusColeta.OK, StatusColeta.PARCIAL) and self.pagina is not None

    def artefato(self, tipo: str) -> Artefato | None:
        return next((a for a in self.artefatos if a.tipo == tipo), None)


# --------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------
class FatoIdentidade(Estrito):
    """Something literally observed in the evidence."""

    tipo: str  # cnpj | legal_name | trade_name | phone | email | address | social_profile |
    # partnership_declaration | correspondent_declaration | domain | whatsapp
    valor: str
    ref_evidencia: str | None = None
    contexto: str | None = None
    valido: bool | None = None  # e.g. CNPJ check digits


class CorrespondenciaCadastro(Estrito):
    fonte: str
    casado_por: str  # cnpj | cnpj_root | name
    registro: dict[str, str]
    oficial: bool


class AtribuicaoIdentidade(Estrito):
    id_candidata: str
    classificacao: ClasseIdentidade
    chave_entidade: str
    nome_exibicao: str | None = None
    cnpjs_responsavel: list[str] = Field(default_factory=list)
    cnpjs_instituicao_referenciados: list[str] = Field(default_factory=list)
    fatos: list[FatoIdentidade] = Field(default_factory=list)
    inferencias: list[str] = Field(default_factory=list)
    correspondencia_cadastro: CorrespondenciaCadastro | None = None
    confianca_atribuicao: Nivel = Nivel.NENHUMA
    limitacoes: list[str] = Field(default_factory=list)
    fundamentacao: str = ""


# --------------------------------------------------------------------------------------
# Brand analysis (LLM contract + metadata)
# --------------------------------------------------------------------------------------
class Indicador(Estrito):
    dimensao: Dimensao
    descricao: str
    refs_evidencia: list[str] = Field(default_factory=list)
    forca: Nivel = Nivel.MEDIA


class AvaliacaoDimensao(Estrito):
    dimensao: Dimensao
    sinal: Sinal
    constatacao: str
    refs_evidencia: list[str] = Field(default_factory=list)


class SaidaAnaliseMarca(Estrito):
    """Schema the multimodal model must return (validated before use)."""

    caso: CasoCenario
    classificacao: ClassificacaoFinal
    prioridade: Prioridade
    dimensoes: list[AvaliacaoDimensao] = Field(default_factory=list)
    indicadores_confusao_marca: list[Indicador] = Field(default_factory=list)
    indicadores_relacao_legitima: list[Indicador] = Field(default_factory=list)
    fatos_observados: list[str] = Field(default_factory=list)
    incertezas: list[str] = Field(default_factory=list)
    referencias_evidencia: list[str] = Field(default_factory=list)
    resumo_raciocinio: str = ""
    exige_revisao_humana: bool = True
    confianca_extracao: Nivel = Nivel.MEDIA
    suficiencia_evidencia: SuficienciaEvidencia = SuficienciaEvidencia.PARCIAL
    possivel_injecao_prompt: bool = False
    # The legacy brand logo (Banco BV: red "B" over a blue "V") is visible in the screenshots.
    identidade_visual_antiga: bool = False


class AvaliacaoMarca(SaidaAnaliseMarca):
    analisador: str  # model id or "heuristic-v1"
    correspondencias_visuais: list[CorrespondenciaVisual] = Field(default_factory=list)  # deterministic logo matches
    fonte_identidade_visual_antiga: str = ""  # "modelo", "comparação de imagem" or both
    # Logo repository / image bank / press: finding recorded without the REVIEW_REQUIRED/HIGH floor.
    logo_antiga_isenta: bool = False
    # Credential-type data the page asks for (senha, cartão, token...) while using the brand.
    dados_sensiveis_solicitados: list[str] = Field(default_factory=list)
    versao_prompt: str | None = None
    referencias_descartadas: list[str] = Field(default_factory=list)
    notas_salvaguarda: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Compliance mapping
# --------------------------------------------------------------------------------------
class FonteRequisito(StrEnum):
    NORMATIVO = "NORMATIVO"
    CONTRATUAL = "CONTRATUAL"
    POLITICA_INTERNA = "POLITICA_INTERNA"


class StatusAchado(StrEnum):
    HIPOTESE_NAO_CONFORMIDADE = "HIPOTESE_NAO_CONFORMIDADE"
    # The page *declares* or suggests a correspondent link that is not confirmed in an authoritative
    # registry: the hypothesis only applies if the link is confirmed.
    HIPOTESE_CONDICIONADA_VINCULO = "HIPOTESE_CONDICIONADA_VINCULO"
    SEM_INDICIO_OBSERVADO = "SEM_INDICIO_OBSERVADO"
    NAO_AVALIAVEL = "NAO_AVALIAVEL"
    NAO_APLICAVEL_SALVO_COBAN = "NAO_APLICAVEL_SALVO_COBAN"


class AchadoConformidade(Estrito):
    id_requisito: str
    fonte_requisito: FonteRequisito
    citacao: str
    texto_requisito: str
    status: StatusAchado
    evidencias_observadas: list[str] = Field(default_factory=list)  # evidence refs
    hipotese: str = ""
    informacoes_faltantes: list[str] = Field(default_factory=list)
    exige_revisao_humana: bool = True


class AvaliacaoConformidade(Estrito):
    achados: list[AchadoConformidade] = Field(default_factory=list)
    notas_fora_escopo: list[str] = Field(default_factory=list)
    controles_relacionados: list[str] = Field(default_factory=list)
    limitacoes: list[str] = Field(default_factory=list)
    ressalva: str = (
        "Mapeamento indicativo para apoio à auditoria. Não constitui conclusão regulatória; "
        "a decisão definitiva pertence à auditoria."
    )


# --------------------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------------------
class DecisaoRevisao(StrEnum):
    CONFIRMAR = "CONFIRMAR"
    REBAIXAR = "REBAIXAR"
    RECOLETAR = "RECOLETAR"
    INCONCLUSIVA = "INCONCLUSIVA"
    REVISAO_HUMANA = "REVISAO_HUMANA"


class Contestacao(Estrito):
    topico: str  # domain_attribution | evidence_sufficiency | over_interpretation | ...
    preocupacao: str
    refs_evidencia: list[str] = Field(default_factory=list)
    severidade: Nivel = Nivel.MEDIA


class SaidaRevisao(Estrito):
    decisao: DecisaoRevisao
    classificacao_revisada: ClassificacaoFinal
    prioridade_revisada: Prioridade
    contestacoes: list[Contestacao] = Field(default_factory=list)
    evidencias_contraditorias: list[str] = Field(default_factory=list)
    motivo_recoleta: str | None = None
    resumo_raciocinio: str = ""
    exige_revisao_humana: bool = True


class Revisao(SaidaRevisao):
    revisor: str
    versao_prompt: str | None = None


# --------------------------------------------------------------------------------------
# Final decision
# --------------------------------------------------------------------------------------
class DecisaoFinal(Estrito):
    id_candidata: str
    classificacao: ClassificacaoFinal
    prioridade: Prioridade
    caso: CasoCenario
    classe_identidade: ClasseIdentidade
    confianca_extracao: Nivel
    confianca_atribuicao: Nivel
    suficiencia_evidencia: SuficienciaEvidencia
    exige_revisao_humana: bool
    status_revisao_humana: str = "PENDENTE"
    justificativa: str
    evidencias_chave: list[str] = Field(default_factory=list)
    limitacoes: list[str] = Field(default_factory=list)
    notas_salvaguarda: list[str] = Field(default_factory=list)
    encaminhamento: list[str] = Field(default_factory=list)  # suggested owner(s) of the follow-up
    resumo_conformidade: list[str] = Field(default_factory=list)
    identidade_visual_antiga: bool = False
    fonte_identidade_visual_antiga: str = ""
    dados_sensiveis_solicitados: list[str] = Field(default_factory=list)
    decidida_em: datetime = Field(default_factory=agora_utc)
