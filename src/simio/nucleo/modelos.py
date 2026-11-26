from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def agora_utc() -> datetime:
    return datetime.now(UTC)


class Estrito(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


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
    """Nível qualitativo de confiança. NÃO é probabilidade de irregularidade."""

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
    NAO_RELEVANTE = "NAO_RELEVANTE"  # a marca nem aparece


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
    PARCIAL = "PARCIAL"  # a página carregou, mas algo falhou (ex.: captura completa)
    FALHOU = "FALHOU"  # timeout, DNS, erro HTTP...
    BLOQUEADA = "BLOQUEADA"  # recusada pela política de URLs
    NAO_COLETADA = "NAO_COLETADA"


class HipoteseBusca(Estrito):
    id: str
    descricao: str
    prompt_grounding: str
    consultas_palavras_chave: list[str] = Field(default_factory=list)


class ResultadoDescoberta(Estrito):
    # Sempre vem dos dados da fonte; nunca é inventada.
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
    status: str = "OK"  # OK | ERRO | VAZIA | PULADA
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


class Artefato(Estrito):
    tipo: str  # captura_viewport | captura_completa | html | texto | dados_pagina
    caminho: str  # relativo à pasta da campanha
    sha256: str
    tamanho_bytes: int


class ItemEvidencia(Estrito):
    ref: str
    tipo: str  # titulo | meta | texto | cabecalho_secao | nav | cta | link | contato | imagem | captura | ...
    local: str  # ex.: "area_visivel", "topo", "rodape", "pagina", "url"
    conteudo: str
    acima_dobra: bool | None = None


class InfoLink(Estrito):
    texto: str
    href: str
    local: str = "pagina"


class InfoImagem(Estrito):
    alt: str
    src: str
    local: str = "pagina"
    acima_dobra: bool = False
    largura: int = 0
    altura: int = 0
    pagina_x: int | None = None  # coordenadas no documento (batem com a captura completa)
    pagina_y: int | None = None


class CorrespondenciaVisual(Estrito):
    tipo: str  # "logo_antiga"
    ref_captura: str  # E-SHOT-VIEWPORT | E-SHOT-FULL
    bbox: list[int]  # x, y, largura, altura em pixels da captura
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
    titular: str | None = None  # só pessoa jurídica (LGPD)
    documento_titular: str | None = None  # CNPJ, quando publicado (Registro.br)
    tipo_titular: str = "não publicado"
    emails_abuso: list[str] = Field(default_factory=list)


class InteligenciaIp(Estrito):
    ip: str
    rede: str | None = None
    organizacao: str | None = None
    pais: str | None = None
    emails_abuso: list[str] = Field(default_factory=list)


class InteligenciaDominio(Estrito):
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
    # Campo que a página pede; só registrado, nunca preenchido.
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


class FatoIdentidade(Estrito):
    tipo: str  # cnpj | telefone | email | whatsapp | perfil_social | dominio | declaracao_correspondente | ...
    valor: str
    ref_evidencia: str | None = None
    contexto: str | None = None
    valido: bool | None = None  # ex.: dígitos verificadores do CNPJ


class CorrespondenciaCadastro(Estrito):
    fonte: str
    casado_por: str  # cnpj | raiz_cnpj | nome
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


# Contrato com o LLM: as docstrings das classes viram description no JSON schema.
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
    """Resposta que o modelo multimodal deve devolver (validada antes do uso)."""

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
    # Logo antiga visível nas capturas (Banco BV: "B" vermelho sobre "V" azul).
    identidade_visual_antiga: bool = False


class AvaliacaoMarca(SaidaAnaliseMarca):
    analisador: str  # id do modelo ou "heuristica-v1"
    correspondencias_visuais: list[CorrespondenciaVisual] = Field(default_factory=list)
    fonte_identidade_visual_antiga: str = ""  # "modelo", "comparação de imagem" ou os dois
    # Repositório de logos, banco de imagens, imprensa: registra o achado sem o piso REQUER_REVISAO/ALTA.
    logo_antiga_isenta: bool = False
    # Dados de credencial que a página pede usando a marca (senha, cartão, token...).
    dados_sensiveis_solicitados: list[str] = Field(default_factory=list)
    versao_prompt: str | None = None
    referencias_descartadas: list[str] = Field(default_factory=list)
    notas_salvaguarda: list[str] = Field(default_factory=list)


class FonteRequisito(StrEnum):
    NORMATIVO = "NORMATIVO"
    CONTRATUAL = "CONTRATUAL"
    POLITICA_INTERNA = "POLITICA_INTERNA"


class StatusAchado(StrEnum):
    HIPOTESE_NAO_CONFORMIDADE = "HIPOTESE_NAO_CONFORMIDADE"
    # A página declara ou sugere vínculo de correspondente que não consta em cadastro oficial:
    # a hipótese só vale se o vínculo for confirmado.
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
    evidencias_observadas: list[str] = Field(default_factory=list)  # refs de evidência
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


class DecisaoRevisao(StrEnum):
    CONFIRMAR = "CONFIRMAR"
    REBAIXAR = "REBAIXAR"
    RECOLETAR = "RECOLETAR"
    INCONCLUSIVA = "INCONCLUSIVA"
    REVISAO_HUMANA = "REVISAO_HUMANA"


class Contestacao(Estrito):
    topico: str  # atribuicao_dominio | suficiencia_evidencia | interpretacao_excessiva | ...
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
    encaminhamento: list[str] = Field(default_factory=list)  # área(s) sugerida(s) para o tratamento
    resumo_conformidade: list[str] = Field(default_factory=list)
    identidade_visual_antiga: bool = False
    fonte_identidade_visual_antiga: str = ""
    dados_sensiveis_solicitados: list[str] = Field(default_factory=list)
    decidida_em: datetime = Field(default_factory=agora_utc)
