"""Brand Analyst Agent: multimodal (Gemini) assessment of brand-identity confusion, with a
deterministic heuristic analyst used in offline mode or when the model is unavailable."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from simio.agentes.evidencia import SinaisPagina
from simio.marcas import PerfilMarca
from simio.nucleo.decisao import PADROES_POR_CASO, ConfigPontuacao, aplicar_salvaguardas_marca
from simio.nucleo.modelos import (
    ORDEM_PRIORIDADE,
    AtribuicaoIdentidade,
    AvaliacaoDimensao,
    AvaliacaoMarca,
    CasoCenario,
    ClassificacaoFinal,
    Dimensao,
    Evidencia,
    Indicador,
    ItemEvidencia,
    Nivel,
    Prioridade,
    SaidaAnaliseMarca,
    Sinal,
    StatusColeta,
    SuficienciaEvidencia,
)
from simio.prompts import renderizar
from simio.provedores.llm import GatewayLLM, Parte, ParteImagem

log = logging.getLogger(__name__)

ID_HEURISTICA = "heuristica-v1"
MAX_BYTES_IMAGEM = 6_000_000


_DELIM_RE = re.compile(r"(?i)<(\s*/?\s*)(conteudo_nao_confiavel)")


def _envolver_nao_confiavel(corpo: str) -> str:
    # Neutralise attempts (any case/spacing) to open or close the delimiter from inside the data.
    corpo = _DELIM_RE.sub(lambda m: "‹" + m.group(1) + m.group(2), corpo)
    return f"<conteudo_nao_confiavel>\n{corpo}\n</conteudo_nao_confiavel>"


def bloco_nao_confiavel(catalogo: list[ItemEvidencia]) -> str:
    payload = [item.model_dump(exclude_none=True) for item in catalogo]
    return _envolver_nao_confiavel(json.dumps(payload, ensure_ascii=False, indent=1))


def bloco_contexto(
    marca: PerfilMarca, ev: Evidencia, sinais: SinaisPagina, identidade: AtribuicaoIdentidade | None
) -> str:
    # URLs and collection errors are attacker-influenced: they go inside the untrusted delimiter.
    coleta = {
        "url_original": ev.url_original,
        "url_final": ev.url_final,
        "ocorrencias_de_coleta": ev.erros,
    }
    linhas = [
        f"Marca investigada: {marca.nome_exibicao}",
        f"Domínios oficiais conhecidos da instituição: {', '.join(marca.dominios_oficiais) or '(não informados)'}",
        f"Status da coleta: {ev.status.value} (HTTP {ev.status_http})",
        "Dados da coleta (não confiáveis):",
        _envolver_nao_confiavel(json.dumps(coleta, ensure_ascii=False)),
        f"Conteúdo não inspecionado: {'; '.join(ev.nao_inspecionado) or '-'}",
        "Observações determinísticas (fatos extraídos por regras, não conclusões):",
        *[f"  - {o}" for o in sinais.como_observacoes()],
    ]
    if identidade is not None:
        linhas.append(
            f"Atribuição de identidade (agente de identidade): {identidade.classificacao.value}"
            f" / confiança {identidade.confianca_atribuicao.value} — {identidade.fundamentacao}"
        )
    return "\n".join(linhas)


_CASOS_COM_SINAL = (
    CasoCenario.B_COMUNICACAO_AMBIGUA,
    CasoCenario.C_POSSIVEL_CANAL_OFICIAL,
    CasoCenario.D_PERSONIFICACAO_FORTE,
)


def verificacao_cruzada(avaliacao: AvaliacaoMarca, heuristica: SaidaAnaliseMarca) -> AvaliacaoMarca:
    """Model vs deterministic rules. Page content (or text inside screenshots) may persuade the model
    to clear a page: when the rules see B/C/D signals and the model says SEM_INDICIO, the case is
    forced to human review instead of being silently cleared, preserving the deterministic indicators."""
    if avaliacao.classificacao != ClassificacaoFinal.SEM_INDICIO or heuristica.caso not in _CASOS_COM_SINAL:
        return avaliacao
    nota = (
        f"divergência modelo × regras: o modelo indicou {avaliacao.caso.value}/SEM_INDICIO, mas as regras "
        f"determinísticas indicam {heuristica.caso.value}; encaminhado para revisão (possível erro ou "
        "influência do conteúdo da página)"
    )
    caso_mesclado = (
        heuristica.caso
        if avaliacao.caso in (CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA, CasoCenario.NAO_RELEVANTE)
        else avaliacao.caso
    )
    confusao_mesclada = avaliacao.indicadores_confusao_marca or heuristica.indicadores_confusao_marca
    refs_mescladas = sorted(set(avaliacao.referencias_evidencia) | set(heuristica.referencias_evidencia))
    return avaliacao.model_copy(
        update={
            "caso": caso_mesclado,
            "classificacao": ClassificacaoFinal.REQUER_REVISAO,
            "prioridade": max(avaliacao.prioridade, Prioridade.MEDIA, key=lambda p: ORDEM_PRIORIDADE[p]),
            "indicadores_confusao_marca": confusao_mesclada,
            "referencias_evidencia": refs_mescladas,
            "exige_revisao_humana": True,
            "incertezas": [*avaliacao.incertezas, nota],
            "notas_salvaguarda": [*avaliacao.notas_salvaguarda, nota],
        }
    )


NOTA_LOGO_ANTIGA = (
    "uso da identidade visual antiga da marca (logotipo descontinuado): terceiros, inclusive correspondentes, "
    "não podem utilizá-la; mantido no mínimo REQUER_REVISAO com prioridade ALTA"
)


NOTA_LOGO_ANTIGA_ISENTA = (
    "logotipo antigo registrado em site de referência (repositório de logos, banco de imagens, "
    "enciclopédia ou imprensa); classificação não elevada"
)


def aplicar_regra_logo_antiga(
    avaliacao: AvaliacaoMarca, catalogo: list[ItemEvidencia], *, isento: bool = False
) -> AvaliacaoMarca:
    """Legacy logo seen by the model and/or by the deterministic image comparison (``E-VIS-LEGACY-*``):
    record the source, make sure a USO_LOGO indicator cites it and floor the result at
    REQUER_REVISAO / ALTA. Clearing such a page is a human decision.

    ``isento`` (site that only catalogues or reports the logo): record the finding, keep the result."""
    refs_vis = [i.ref for i in catalogo if i.tipo == "visual"]
    por_modelo = avaliacao.identidade_visual_antiga
    if not por_modelo and not refs_vis:
        return avaliacao
    fonte = " + ".join(s for s, ligado in (("modelo", por_modelo), ("comparação de imagem", bool(refs_vis))) if ligado)
    notas = list(avaliacao.notas_salvaguarda)
    confusao = list(avaliacao.indicadores_confusao_marca)
    if refs_vis and not any(set(refs_vis) & set(ind.refs_evidencia) for ind in confusao):
        confusao.append(
            Indicador(
                dimensao=Dimensao.USO_LOGO,
                forca=Nivel.ALTA,
                descricao="Logotipo antigo da marca (identidade visual descontinuada) confirmado por comparação "
                "de imagem com a referência",
                refs_evidencia=refs_vis[:3],
            )
        )
        if not por_modelo:
            notas.append(
                "a comparação de imagem encontrou o logotipo antigo que a análise do modelo não registrou"
            )
    cls, prio = avaliacao.classificacao, avaliacao.prioridade
    if isento:
        notas.append(NOTA_LOGO_ANTIGA_ISENTA)
        fonte += "; site de referência"
    else:
        if cls == ClassificacaoFinal.SEM_INDICIO:
            cls = ClassificacaoFinal.REQUER_REVISAO
        prio = max(prio, Prioridade.ALTA, key=lambda p: ORDEM_PRIORIDADE[p])
        if (cls, prio) != (avaliacao.classificacao, avaliacao.prioridade):
            notas.append(NOTA_LOGO_ANTIGA)
    return avaliacao.model_copy(
        update={
            "identidade_visual_antiga": True,
            "fonte_identidade_visual_antiga": fonte,
            "logo_antiga_isenta": isento,
            "classificacao": cls,
            "prioridade": prio,
            "indicadores_confusao_marca": confusao,
            "exige_revisao_humana": avaliacao.exige_revisao_humana or not isento,
            "referencias_evidencia": sorted(set(avaliacao.referencias_evidencia) | set(refs_vis)),
            "notas_salvaguarda": notas,
        }
    )


NOTA_DADOS_SENSIVEIS = (
    "página fora dos domínios oficiais que usa a marca e pede {kinds}: possível coleta de credenciais "
    "(phishing/painel falso); mantido no mínimo REQUER_REVISAO com prioridade ALTA"
)


MAX_PALAVRAS_PAGINA_LOGIN = 250


def aplicar_regra_dados_sensiveis(avaliacao: AvaliacaoMarca, sinais: SinaisPagina) -> AvaliacaoMarca:
    """A page that uses the brand and asks for credentials (password, card, CVV, token, bank account)
    is a possible phishing page: record what it asks for and floor at REQUER_REVISAO / ALTA."""
    creds = sinais.campos_credencial
    # The page must *present itself* with the brand, not merely cite it: news portals, multi-bank sites
    # and partner portals cite the brand and have an ordinary login box. Brand in the domain or the legacy
    # logo is enough; brand in the title/header/a brand-named image only counts on a short, login-like page.
    parece_login = sinais.palavras_visiveis <= MAX_PALAVRAS_PAGINA_LOGIN
    usa_marca = bool(
        sinais.marca_no_dominio
        or sinais.logo_antiga_visual
        or (parece_login and (sinais.marca_no_titulo or sinais.marca_no_cabecalho or sinais.imagens_nomeadas_marca))
    )
    if not creds or not usa_marca or sinais.canal_oficial:
        return avaliacao
    tipos = sorted({h.valor for h in sinais.campos_sensiveis})
    refs = list(dict.fromkeys([h.ref for h in creds] + [h.ref for h in sinais.acao_formulario_externa]))[:5]
    confusao = list(avaliacao.indicadores_confusao_marca)
    if not any(set(refs) & set(ind.refs_evidencia) for ind in confusao):
        confusao.append(
            Indicador(
                dimensao=Dimensao.JORNADA_ATENDIMENTO,
                forca=Nivel.ALTA,
                descricao=f"Página usa a marca e pede {', '.join(tipos)}"
                + (" e envia o formulário para outro domínio" if sinais.acao_formulario_externa else ""),
                refs_evidencia=refs,
            )
        )
    notas = list(avaliacao.notas_salvaguarda)
    cls, prio = avaliacao.classificacao, avaliacao.prioridade
    if cls in (ClassificacaoFinal.SEM_INDICIO, ClassificacaoFinal.INCONCLUSIVA):
        cls = ClassificacaoFinal.REQUER_REVISAO
    prio = max(prio, Prioridade.ALTA, key=lambda p: ORDEM_PRIORIDADE[p])
    if (cls, prio) != (avaliacao.classificacao, avaliacao.prioridade):
        notas.append(NOTA_DADOS_SENSIVEIS.format(kinds=", ".join(tipos)))
    return avaliacao.model_copy(
        update={
            "dados_sensiveis_solicitados": tipos,
            "classificacao": cls,
            "prioridade": prio,
            "indicadores_confusao_marca": confusao,
            "exige_revisao_humana": True,
            "referencias_evidencia": sorted(set(avaliacao.referencias_evidencia) | set(refs)),
            "notas_salvaguarda": notas,
        }
    )


def carregar_imagens(ev: Evidencia, dir_campanha: Path) -> list[ParteImagem]:
    partes: list[ParteImagem] = []
    for tipo, rotulo in (
        ("captura_viewport", "E-SHOT-VIEWPORT (área visível inicial)"),
        ("captura_completa", "E-SHOT-FULL (página completa)"),
    ):
        art = ev.artefato(tipo)
        if art is None:
            continue
        p = dir_campanha / art.caminho
        if p.is_file() and p.stat().st_size <= MAX_BYTES_IMAGEM:
            partes.append(ParteImagem(dados=p.read_bytes(), tipo_mime="image/png", rotulo=rotulo))
    return partes


class AnalistaMarca:
    def __init__(
        self,
        marca: PerfilMarca,
        config_pontuacao: ConfigPontuacao,
        dir_campanha: Path,
        gateway: GatewayLLM | None = None,
        modelo: str | None = None,
        modo: str | None = None,
    ) -> None:
        self.marca = marca
        self.config_pontuacao = config_pontuacao
        self.dir_campanha = dir_campanha
        self.modo = modo  # campaign-mode prompt addendum (e.g. "modo_ameacas"), overriding the COBAN scope
        self.gateway = gateway
        self.modelo = modelo

    @property
    def usa_llm(self) -> bool:
        return self.gateway is not None and bool(self.modelo)

    async def analisar(
        self,
        ev: Evidencia,
        catalogo: list[ItemEvidencia],
        sinais: SinaisPagina,
        identidade: AtribuicaoIdentidade | None,
    ) -> AvaliacaoMarca:
        refs_validas = {i.ref for i in catalogo}
        heuristica = analise_heuristica(sinais, self.config_pontuacao, ev, identidade, refs_validas)
        if self.usa_llm and (ev.utilizavel or sinais.tem_evidencia_descoberta):
            assert self.gateway is not None and self.modelo is not None
            versao, sistema = renderizar("analista_marca", nome_marca=self.marca.nome_exibicao)
            if self.modo:
                versao_modo, adendo = renderizar(self.modo, nome_marca=self.marca.nome_exibicao)
                versao, sistema = f"{versao}+{versao_modo}", f"{sistema}\n\n{adendo}"
            partes: list[Parte] = [
                "## Contexto da coleta\n" + bloco_contexto(self.marca, ev, sinais, identidade),
                "## Catálogo de evidências (dados não confiáveis)\n" + bloco_nao_confiavel(catalogo),
                *carregar_imagens(ev, self.dir_campanha),
                "Produza a avaliação estruturada em JSON.",
            ]
            saida = await self.gateway.estruturada(
                agente="analista_marca",
                modelo=self.modelo,
                sistema=sistema,
                partes=partes,
                esquema=SaidaAnaliseMarca,
            )
            analisador, pv = self.modelo, versao
        else:
            saida = heuristica
            analisador, pv = ID_HEURISTICA, None
        avaliacao = aplicar_salvaguardas_marca(
            saida,
            analisador=analisador,
            versao_prompt=pv,
            refs_validas=refs_validas,
            evidencia=ev,
            sinais=sinais,
            config_pontuacao=self.config_pontuacao,
        )
        if analisador != ID_HEURISTICA:
            avaliacao = verificacao_cruzada(avaliacao, heuristica)
        host = urlsplit(ev.url_final or ev.url_original).hostname or ""
        avaliacao = aplicar_regra_logo_antiga(avaliacao, catalogo, isento=self.marca.eh_isento_logo_antiga(host))
        return aplicar_regra_dados_sensiveis(avaliacao, sinais)


# --------------------------------------------------------------------------------------
# Offline heuristic analyst
# --------------------------------------------------------------------------------------
def analise_heuristica(
    sig: SinaisPagina,
    sc: ConfigPontuacao,
    ev: Evidencia,
    identidade: AtribuicaoIdentidade | None,
    refs_validas: set[str] | None = None,
) -> SaidaAnaliseMarca:
    refs_ok = refs_validas or set()

    def escolher(*cands: str) -> list[str]:
        encontrados = [c for c in cands if c in refs_ok]
        return encontrados[:2] or (["E-DISC-001"] if "E-DISC-001" in refs_ok else ["E-URL"])

    if not ev.utilizavel and not sig.tem_evidencia_descoberta:
        return SaidaAnaliseMarca(
            caso=CasoCenario.E_EVIDENCIA_INSUFICIENTE,
            classificacao=PADROES_POR_CASO[CasoCenario.E_EVIDENCIA_INSUFICIENTE][0],
            prioridade=PADROES_POR_CASO[CasoCenario.E_EVIDENCIA_INSUFICIENTE][1],
            fatos_observados=[f"coleta: {ev.status.value}", *ev.erros[:3]],
            incertezas=["conteúdo da página não disponível para análise"],
            referencias_evidencia=["E-URL"] + (["E-COLLECT"] if ev.erros else []),
            resumo_raciocinio="Página não coletada de forma utilizável; não há base para conclusão.",
            exige_revisao_humana=True,
            confianca_extracao=Nivel.NENHUMA,
            suficiencia_evidencia=SuficienciaEvidencia.INSUFICIENTE,
        )

    confusao: list[Indicador] = []
    legitimos: list[Indicador] = []
    dims: list[AvaliacaoDimensao] = []
    pontuacao_c = 0.0
    pontuacao_l = 0.0
    destacado = (
        sig.marca_no_dominio
        or sig.marca_no_titulo
        or sig.marca_no_cabecalho
        or sig.imagens_marca_acima_dobra
        or (not ev.utilizavel and sig.tem_evidencia_descoberta and sig.mencoes_marca > 0)
    )

    if sig.marca_no_dominio:
        pontuacao_c += sc.p_marca_no_dominio
        confusao.append(
            Indicador(
                dimensao=Dimensao.IDENTIDADE_COMERCIAL_DECLARADA,
                forca=Nivel.MEDIA,
                descricao=f"Domínio contém padrão da marca ({sig.marca_no_dominio})",
                refs_evidencia=["E-URL"],
            )
        )
    if sig.marca_no_titulo:
        pontuacao_c += sc.p_marca_no_titulo
        confusao.append(
            Indicador(
                dimensao=Dimensao.IDENTIDADE_COMERCIAL_DECLARADA,
                forca=Nivel.MEDIA,
                descricao="Marca no título/nome do site",
                refs_evidencia=["E-TITLE"],
            )
        )
    if sig.marca_no_cabecalho:
        pontuacao_c += sc.p_marca_no_cabecalho
        confusao.append(
            Indicador(
                dimensao=Dimensao.IDENTIDADE_COMERCIAL_DECLARADA,
                forca=Nivel.BAIXA,
                descricao="Marca no cabeçalho/navegação fora de declaração de correspondente",
                refs_evidencia=escolher("E-HDR", "E-NAV"),
            )
        )
    if sig.imagens_marca_acima_dobra:
        pontuacao_c += sc.p_logo_marca_acima_dobra
        refs = [h.ref for h in sig.imagens_marca][:3]
        confusao.append(
            Indicador(
                dimensao=Dimensao.USO_LOGO,
                forca=Nivel.MEDIA,
                descricao="Imagem possivelmente de logotipo da marca acima da dobra",
                refs_evidencia=refs,
            )
        )
    if sig.logo_antiga_visual:
        pontuacao_c += sc.p_logo_marca_acima_dobra
        confusao.append(
            Indicador(
                dimensao=Dimensao.USO_LOGO,
                forca=Nivel.ALTA,
                descricao="Logotipo antigo da marca (identidade visual descontinuada) confirmado por comparação "
                "de imagem",
                refs_evidencia=[h.ref for h in sig.logo_antiga_visual][:3],
            )
        )
    if sig.alegacoes_ligadas_marca:
        pontuacao_c += sc.p_oficialidade_ligada_marca
        confusao.append(
            Indicador(
                dimensao=Dimensao.LINGUAGEM_INSTITUCIONAL,
                forca=Nivel.ALTA,
                descricao="Expressões de oficialidade associadas à marca: "
                + ", ".join(sorted({h.valor for h in sig.alegacoes_ligadas_marca})),
                refs_evidencia=sorted({h.ref for h in sig.alegacoes_ligadas_marca})[:5],
            )
        )
    elif sig.alegacoes_oficiais:
        pontuacao_c += sc.p_oficialidade_generica
        confusao.append(
            Indicador(
                dimensao=Dimensao.LINGUAGEM_INSTITUCIONAL,
                forca=Nivel.BAIXA,
                descricao="Expressões de oficialidade (não associadas diretamente à marca)",
                refs_evidencia=sorted({h.ref for h in sig.alegacoes_oficiais})[:5],
            )
        )
    # Absence of a disclosure only matters when the brand is *used* (prominence or official claims);
    # a plain mention in body text is not, by itself, a transparency problem.
    if not sig.declaracoes and (destacado or sig.alegacoes_ligadas_marca):
        pontuacao_c += sc.p_sem_declaracao
        confusao.append(
            Indicador(
                dimensao=Dimensao.TRANSPARENCIA_CORRESPONDENTE,
                forca=Nivel.MEDIA,
                descricao="Nenhuma declaração de correspondente/independência observada no conteúdo coletado",
                refs_evidencia=escolher("E-FTR", "E-FOLD", "E-TITLE"),
            )
        )
    if sig.contatos_whatsapp and destacado:
        pontuacao_c += sc.p_whatsapp_com_marca_destacada
        confusao.append(
            Indicador(
                dimensao=Dimensao.CANAIS_CONTATO,
                forca=Nivel.BAIXA,
                descricao="Atendimento por WhatsApp sob identidade destacada da marca",
                refs_evidencia=[h.ref for h in sig.contatos_whatsapp][:3],
            )
        )
    tem_captura_ativo = bool(sig.contatos_whatsapp or sig.formularios_presentes > 0 or sig.telefones)
    if (
        sig.alegacoes_servicos_sensiveis
        and tem_captura_ativo
        and not (sig.declaracao_acima_dobra and sig.declaracao_nomeia_instituicao)
        and not sig.contexto_editorial
    ):
        pontuacao_c += sc.p_isca_servico_sensivel
        confusao.append(
            Indicador(
                dimensao=Dimensao.JORNADA_ATENDIMENTO,
                forca=Nivel.ALTA,
                descricao="Oferta de serviço pós-venda sensível (2ª via de boleto/quitação/acordo) com captação "
                "de contato sem identificação destacada de correspondente/assessoria: "
                + ", ".join(sorted({h.valor for h in sig.alegacoes_servicos_sensiveis})),
                refs_evidencia=sorted({h.ref for h in sig.alegacoes_servicos_sensiveis})[:5],
            )
        )
    if (
        identidade is not None
        and identidade.cnpjs_instituicao_referenciados
        and not identidade.cnpjs_responsavel
        and not sig.declaracoes
        and not sig.contexto_editorial
    ):
        pontuacao_c += sc.p_personificacao_cnpj_instituicao
        refs_inst = [
            f.ref_evidencia
            for f in identidade.fatos
            if f.tipo == "cnpj" and f.valor in identidade.cnpjs_instituicao_referenciados and f.ref_evidencia
        ]
        confusao.append(
            Indicador(
                dimensao=Dimensao.IDENTIDADE_COMERCIAL_DECLARADA,
                forca=Nivel.ALTA,
                descricao="Página de terceiro exibe CNPJ oficial da instituição sem identificar CNPJ próprio "
                "do responsável nem declarar condição de correspondente",
                refs_evidencia=refs_inst[:3] or escolher("E-FTR", "E-URL"),
            )
        )
    if sig.declaracoes:
        refs = sorted({h.ref for h in sig.declaracoes})[:5]
        nomes = "" if sig.declaracao_nomeia_instituicao else " (sem nomear a instituição contratante)"
        if sig.declaracao_acima_dobra and sig.declaracao_nomeia_instituicao:
            pontuacao_l += sc.p_declaracao_destacada
            legitimos.append(
                Indicador(
                    dimensao=Dimensao.TRANSPARENCIA_CORRESPONDENTE,
                    forca=Nivel.ALTA,
                    descricao="Declaração de correspondente nomeando a instituição, no cabeçalho/área visível "
                    "inicial (posição observada; tamanho, contraste e legibilidade a verificar no screenshot)",
                    refs_evidencia=refs,
                )
            )
        else:
            pontuacao_l += sc.p_declaracao_presente
            onde = (
                "na área visível inicial" if sig.declaracao_acima_dobra else "apenas fora da área inicial (ex.: rodapé)"
            )
            legitimos.append(
                Indicador(
                    dimensao=Dimensao.TRANSPARENCIA_CORRESPONDENTE,
                    forca=Nivel.BAIXA,
                    descricao=f"Declaração de correspondente/independência {onde}{nomes}",
                    refs_evidencia=refs,
                )
            )
    responsavel_identificado = bool(identidade is not None and identidade.cnpjs_responsavel) or bool(sig.declaracoes)
    multibanco_transparente = (
        sig.contexto_multibanco
        and not sig.marca_no_dominio
        and not sig.marca_no_titulo
        and not sig.marca_no_cabecalho
        and not sig.alegacoes_ligadas_marca
        and not sig.alegacoes_servicos_sensiveis
        and responsavel_identificado
    )
    if multibanco_transparente:
        pontuacao_l += sc.p_contexto_multibanco
        refs_multibanco = sorted({h.ref for h in sig.parceria})[:3] or escolher("E-FOLD", "E-TITLE", "E-URL")
        legitimos.append(
            Indicador(
                dimensao=Dimensao.TRANSPARENCIA_CORRESPONDENTE,
                forca=Nivel.MEDIA,
                descricao="Apresentação da marca em contexto multimarcas/multibancos junto a outras instituições "
                f"financeiras ({', '.join(sig.bancos_concorrentes[:5])}), "
                "caracterizando oferta intermediada por terceiro",
                refs_evidencia=refs_multibanco,
            )
        )
    if sig.ouvidoria:
        pontuacao_l += sc.p_ouvidoria
        legitimos.append(
            Indicador(
                dimensao=Dimensao.CANAIS_CONTATO,
                forca=Nivel.BAIXA,
                descricao="Menção a ouvidoria (não verificado se é a da instituição contratante)",
                refs_evidencia=[sig.ouvidoria[0].ref],
            )
        )
    if identidade is not None and identidade.cnpjs_responsavel:
        pontuacao_l += sc.p_responsavel_identificado
        refs_cnpj = [f.ref_evidencia for f in identidade.fatos if f.tipo == "cnpj" and f.ref_evidencia]
        legitimos.append(
            Indicador(
                dimensao=Dimensao.IDENTIDADE_COMERCIAL_DECLARADA,
                forca=Nivel.BAIXA,
                descricao="Empresa responsável identificada por CNPJ",
                refs_evidencia=refs_cnpj[:2] or escolher("E-FTR"),
            )
        )
    # A bare "parceiro" mention does not resolve the analysis: noted as uncertainty, not scored.

    rede = pontuacao_c - pontuacao_l
    relevantes = (
        sig.mencoes_marca > 0
        or bool(sig.marca_no_dominio)
        or sig.marca_no_titulo
        or sig.marca_no_cabecalho
        or bool(sig.imagens_marca)
        or bool(sig.alegacoes_ligadas_marca)
    )
    # The page presents *itself* with the brand (vs. a news article or review that talks about it).
    autoapresentado = bool(sig.marca_no_dominio) or sig.marca_no_cabecalho or (
        sig.marca_no_titulo and not sig.pagina_artigo and not sig.contexto_editorial
    )
    # Only a disclosure that names the institution (Art. 14, III) and is not contradicted in the same
    # sentence, or a clear multi-bank intermediary listing without official claims/domain, counts as
    # sufficient context to avoid treating brand mentions as impersonation.
    declaracao_nomeada = sig.declaracao_acima_dobra and sig.declaracao_nomeia_instituicao
    transparente = declaracao_nomeada or multibanco_transparente
    so_editorial = (
        sig.contexto_editorial
        and not sig.marca_no_dominio
        and not sig.marca_no_cabecalho
        and not sig.alegacoes_ligadas_marca
        and not (sig.alegacoes_servicos_sensiveis and tem_captura_ativo)
        and not sig.contatos_whatsapp
        and not sig.imagens_marca_acima_dobra
    )
    tetos: list[str] = []
    if sig.canal_oficial:
        caso = CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA
        confusao.clear()
        legitimos.append(
            Indicador(
                dimensao=Dimensao.IDENTIDADE_COMERCIAL_DECLARADA,
                forca=Nivel.ALTA,
                descricao="URL final pertence a domínio ou perfil institucional oficial da própria marca",
                refs_evidencia=["E-URL"],
            )
        )
    elif not ev.utilizavel and (not relevantes or so_editorial):
        caso = CasoCenario.E_EVIDENCIA_INSUFICIENTE
    elif not relevantes:
        caso = CasoCenario.NAO_RELEVANTE
    elif so_editorial:
        caso = CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA
    elif (
        rede >= sc.limiar_d
        and (sig.alegacoes_ligadas_marca or (sig.alegacoes_servicos_sensiveis and tem_captura_ativo))
        and autoapresentado
        and not declaracao_nomeada
    ):
        caso = CasoCenario.D_PERSONIFICACAO_FORTE
    elif (
        sig.alegacoes_ligadas_marca
        or (
            sig.alegacoes_servicos_sensiveis
            and tem_captura_ativo
            and not declaracao_nomeada
            and not sig.contexto_editorial
        )
        or (rede >= sc.limiar_c and sig.marca_no_dominio and (sig.marca_no_titulo or sig.marca_no_cabecalho))
    ):
        # C needs a signal of *direct service by the institution* (official language, sensitive
        # post-sale capture, or the brand used as the site's own domain + identity); brand prominence
        # alone is case B.
        caso = CasoCenario.C_POSSIVEL_CANAL_OFICIAL
    elif rede >= sc.limiar_b or (destacado and not transparente):
        caso = CasoCenario.B_COMUNICACAO_AMBIGUA
    else:
        caso = CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA
    if (
        (sig.pagina_artigo or sig.contexto_editorial)
        and not sig.marca_no_dominio
        and caso
        in (
            CasoCenario.C_POSSIVEL_CANAL_OFICIAL,
            CasoCenario.D_PERSONIFICACAO_FORTE,
        )
    ):
        tetos.append(
            "página declarada/identificada como artigo/notícia/editorial: cenário limitado a B para revisão humana"
        )
        caso = CasoCenario.B_COMUNICACAO_AMBIGUA
    cls, prio = PADROES_POR_CASO[caso]

    def dim(d: Dimensao, inds_c: list[Indicador], inds_l: list[Indicador]) -> AvaliacaoDimensao:
        c = [i for i in inds_c if i.dimensao == d]
        g = [i for i in inds_l if i.dimensao == d]
        if c and not g:
            s, f, refs = Sinal.CONFUSAO, "; ".join(i.descricao for i in c), [r for i in c for r in i.refs_evidencia]
        elif g and not c:
            s, f, refs = Sinal.LEGITIMO, "; ".join(i.descricao for i in g), [r for i in g for r in i.refs_evidencia]
        elif c and g:
            s, f, refs = Sinal.NEUTRO, "sinais contraditórios", [r for i in c + g for r in i.refs_evidencia]
        else:
            s, f, refs = Sinal.NAO_AVALIADO, "não avaliado pela heurística", []
        return AvaliacaoDimensao(dimensao=d, sinal=s, constatacao=f, refs_evidencia=refs[:5])

    dims = [dim(d, confusao, legitimos) for d in Dimensao]
    incertezas = [
        "análise heurística sem avaliação visual por modelo; identidade visual e logotipo "
        "inferidos apenas por textos alternativos e nomes de arquivos"
    ]
    if sig.parceria and not sig.declaracoes:
        incertezas.append("menção a 'parceiro' sem declaração de correspondente: não resolve a análise")
    if sig.declaracoes and confusao:
        incertezas.append("evidências contraditórias: há declaração de correspondente e indícios de confusão")
    if sig.alegacoes_noticiadas:
        incertezas.append(
            "expressões de oficialidade em contexto de terceiros (notícia, alerta, citação) não foram tratadas "
            "como autoapresentação da página"
        )
    if sig.declaracoes and not sig.declaracao_nomeia_instituicao:
        incertezas.append("declaração de correspondente/independência sem nomear a instituição contratante")
    incertezas.extend(tetos)
    return SaidaAnaliseMarca(
        caso=caso,
        classificacao=cls,
        prioridade=prio,
        dimensoes=dims,
        indicadores_confusao_marca=confusao,
        indicadores_relacao_legitima=legitimos,
        fatos_observados=sig.como_observacoes(),
        incertezas=incertezas,
        referencias_evidencia=sorted({r for i in confusao + legitimos for r in i.refs_evidencia}),
        resumo_raciocinio=_explicar(caso, confusao, legitimos, pontuacao_c, pontuacao_l),
        exige_revisao_humana=caso != CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA
        and caso != CasoCenario.NAO_RELEVANTE,
        confianca_extracao=Nivel.MEDIA if ev.status.value == "OK" else Nivel.BAIXA,
        suficiencia_evidencia=(
            SuficienciaEvidencia.PARCIAL
            if ev.nao_inspecionado or ev.status != StatusColeta.OK
            else SuficienciaEvidencia.SUFICIENTE
        ),
        possivel_injecao_prompt=bool(sig.marcadores_injecao),
    )


_TEXTO_CASO = {
    CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA: "menção comercial com condição de correspondente/terceiro clara",
    CasoCenario.B_COMUNICACAO_AMBIGUA: "uso destacado da marca sem transparência suficiente",
    CasoCenario.C_POSSIVEL_CANAL_OFICIAL: "elementos que sugerem atendimento direto pela instituição",
    CasoCenario.D_PERSONIFICACAO_FORTE: "apresentação consistente como a própria instituição",
    CasoCenario.E_EVIDENCIA_INSUFICIENTE: "evidência insuficiente",
    CasoCenario.NAO_RELEVANTE: "a marca não aparece de forma relevante",
}


def _explicar(caso: CasoCenario, confusao: list[Indicador], legitimos: list[Indicador], sc: float, sl: float) -> str:
    """Plain-language rationale: which elements could lead a consumer to believe they deal directly
    with the institution, and which point the other way."""
    partes = [f"Triagem heurística — cenário {caso.value}: {_TEXTO_CASO[caso]}."]
    if confusao:
        partes.append(
            "Elementos que podem levar o consumidor a crer que trata diretamente com a instituição: "
            + "; ".join(f"{i.descricao} [{', '.join(i.refs_evidencia[:3])}]" for i in confusao[:5])
            + "."
        )
    if legitimos:
        partes.append(
            "Elementos de transparência/identificação do terceiro: "
            + "; ".join(f"{i.descricao} [{', '.join(i.refs_evidencia[:3])}]" for i in legitimos[:4])
            + "."
        )
    partes.append(f"(Pontuação de triagem: confusão {sc:.1f}, transparência {sl:.1f}; ordena casos, não prova.)")
    return " ".join(partes)
