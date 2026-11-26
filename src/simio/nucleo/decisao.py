from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from simio.agentes.evidencia import SinaisPagina
from simio.nucleo.modelos import (
    ORDEM_CLASSIFICACAO,
    ORDEM_PRIORIDADE,
    AtribuicaoIdentidade,
    AvaliacaoConformidade,
    AvaliacaoMarca,
    CasoCenario,
    ClasseIdentidade,
    ClassificacaoFinal,
    DecisaoFinal,
    DecisaoRevisao,
    Dimensao,
    Evidencia,
    Indicador,
    Nivel,
    Prioridade,
    Revisao,
    SaidaAnaliseMarca,
    StatusAchado,
    StatusColeta,
    SuficienciaEvidencia,
)

C = ClassificacaoFinal


class ConfigPontuacao(BaseModel):
    # Pesos da heurística offline. A pontuação só ordena e encaminha casos; nunca confirma infração.

    p_marca_no_dominio: float = 2.0
    p_marca_no_titulo: float = 1.5
    p_marca_no_cabecalho: float = 1.0
    p_logo_marca_acima_dobra: float = 1.0
    p_oficialidade_ligada_marca: float = 3.0
    p_oficialidade_generica: float = 0.5
    p_sem_declaracao: float = 1.5
    p_whatsapp_com_marca_destacada: float = 0.5
    p_isca_servico_sensivel: float = 2.5
    p_personificacao_cnpj_instituicao: float = 2.5
    p_declaracao_destacada: float = 2.5
    p_declaracao_presente: float = 1.0
    p_contexto_multibanco: float = 2.0
    p_ouvidoria: float = 0.5
    p_responsavel_identificado: float = 0.5
    limiar_b: float = 1.5
    limiar_c: float = 3.5
    limiar_d: float = 5.5
    min_indicadores_forte: int = Field(default=2, ge=1)

    @classmethod
    def carregar(cls, caminho: Path | None) -> ConfigPontuacao:
        if caminho is None or not caminho.is_file():
            return cls()
        with caminho.open("rb") as fh:
            return cls(**tomllib.load(fh))


PADROES_POR_CASO: dict[CasoCenario, tuple[C, Prioridade]] = {
    CasoCenario.NAO_RELEVANTE: (C.SEM_INDICIO, Prioridade.BAIXA),
    CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA: (C.SEM_INDICIO, Prioridade.BAIXA),
    CasoCenario.B_COMUNICACAO_AMBIGUA: (C.REQUER_REVISAO, Prioridade.MEDIA),
    CasoCenario.C_POSSIVEL_CANAL_OFICIAL: (C.REQUER_REVISAO, Prioridade.ALTA),
    CasoCenario.D_PERSONIFICACAO_FORTE: (C.INDICIO_FORTE, Prioridade.ALTA),
    CasoCenario.E_EVIDENCIA_INSUFICIENTE: (C.INCONCLUSIVA, Prioridade.MEDIA),
}


def _filtrar_refs(refs: list[str], valido: set[str], descartadas: list[str]) -> list[str]:
    saida = []
    for r in refs:
        if r in valido:
            saida.append(r)
        elif r not in descartadas:
            descartadas.append(r)
    return saida


# A saída do modelo é só proposta: as regras daqui garantem coerência entre cenário, classificação,
# referências e status da coleta, e mantêm classificação, prioridade e confianças como eixos separados.
def aplicar_salvaguardas_marca(
    saida: SaidaAnaliseMarca,
    *,
    analisador: str,
    versao_prompt: str | None,
    refs_validas: set[str],
    evidencia: Evidencia,
    sinais: SinaisPagina,
    config_pontuacao: ConfigPontuacao,
) -> AvaliacaoMarca:
    notas: list[str] = []
    descartadas: list[str] = []

    def limpar_indicadores(itens: list[Indicador]) -> list[Indicador]:
        mantidos = []
        for ind in itens:
            ind.refs_evidencia = _filtrar_refs(ind.refs_evidencia, refs_validas, descartadas)
            if ind.refs_evidencia:
                mantidos.append(ind)
            else:
                notas.append(f"indicador sem referência válida descartado: {ind.descricao[:120]}")
        return mantidos

    confusao = limpar_indicadores(list(saida.indicadores_confusao_marca))
    legitimos = limpar_indicadores(list(saida.indicadores_relacao_legitima))
    for d in saida.dimensoes:
        d.refs_evidencia = _filtrar_refs(d.refs_evidencia, refs_validas, descartadas)
    refs = _filtrar_refs(saida.referencias_evidencia, refs_validas, descartadas)
    if descartadas:
        notas.append(f"referências inexistentes no catálogo removidas: {', '.join(descartadas[:10])}")

    caso, cls, prio = saida.caso, saida.classificacao, saida.prioridade
    suficiencia = saida.suficiencia_evidencia
    injecao = saida.possivel_injecao_prompt or bool(sinais.marcadores_injecao)

    if not evidencia.utilizavel and not sinais.tem_evidencia_descoberta:
        if caso != CasoCenario.E_EVIDENCIA_INSUFICIENTE or cls != C.INCONCLUSIVA:
            notas.append("coleta sem conteúdo utilizável: forçado cenário E / INCONCLUSIVA")
        caso, cls, suficiencia = CasoCenario.E_EVIDENCIA_INSUFICIENTE, C.INCONCLUSIVA, SuficienciaEvidencia.INSUFICIENTE
        prio = Prioridade.MEDIA if sinais.marca_no_dominio else Prioridade.BAIXA
    elif not evidencia.utilizavel and sinais.tem_evidencia_descoberta:
        suficiencia = SuficienciaEvidencia.PARCIAL
        notas.append(
            "acesso direto no navegador local bloqueado/indisponível; avaliação baseada nas evidências "
            "indexadas na descoberta (E-DISC-*)"
        )
        if cls == C.SEM_INDICIO and not sinais.canal_oficial:
            if confusao:
                cls = C.REQUER_REVISAO
                prio = max(prio, Prioridade.MEDIA, key=lambda p: ORDEM_PRIORIDADE[p])
            else:
                notas.append("página não inspecionada diretamente não pode receber SEM_INDICIO: marcado INCONCLUSIVA")
                caso, cls, suficiencia = (
                    CasoCenario.E_EVIDENCIA_INSUFICIENTE,
                    C.INCONCLUSIVA,
                    SuficienciaEvidencia.INSUFICIENTE,
                )
    if caso in (CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA, CasoCenario.NAO_RELEVANTE) and cls in (
        C.INDICIO_FORTE,
        C.REQUER_REVISAO,
    ):
        notas.append(f"inconsistência cenário {caso.value} × {cls.value}: encaminhado para revisão")
        cls = C.REQUER_REVISAO
    if caso == CasoCenario.E_EVIDENCIA_INSUFICIENTE and cls != C.INCONCLUSIVA:
        notas.append("cenário E exige INCONCLUSIVA")
        cls = C.INCONCLUSIVA
    if (
        caso in (CasoCenario.C_POSSIVEL_CANAL_OFICIAL, CasoCenario.D_PERSONIFICACAO_FORTE)
        and cls == C.SEM_INDICIO
    ):
        notas.append(f"cenário {caso.value} incompatível com SEM_INDICIO: elevado a REQUER_REVISAO")
        cls = C.REQUER_REVISAO
    if caso == CasoCenario.B_COMUNICACAO_AMBIGUA and cls == C.SEM_INDICIO:
        if confusao:
            notas.append(
                "inconsistência cenário B_COMUNICACAO_AMBIGUA × SEM_INDICIO com indicadores de confusão: "
                "elevado a REQUER_REVISAO"
            )
            cls = C.REQUER_REVISAO
            prio = max(prio, Prioridade.MEDIA, key=lambda p: ORDEM_PRIORIDADE[p])
        else:
            notas.append(
                "inconsistência cenário B_COMUNICACAO_AMBIGUA × SEM_INDICIO sem indicadores de confusão: "
                "cenário ajustado para A_MENCAO_COMERCIAL_LEGITIMA"
            )
            caso = CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA
    if (
        cls == C.REQUER_REVISAO
        and evidencia.utilizavel
        and not sinais.declaracoes
        and not sinais.contexto_editorial
    ):
        dims_conf = {i.dimensao for i in confusao}
        tem_alegacao_ou_visual_alta = any(
            i.forca == Nivel.ALTA
            and i.dimensao
            in (
                Dimensao.USO_LOGO,
                Dimensao.IDENTIDADE_VISUAL,
                Dimensao.LINGUAGEM_INSTITUCIONAL,
                Dimensao.JORNADA_ATENDIMENTO,
            )
            for i in confusao
        )
        tem_lacuna_transparencia = bool(
            dims_conf & {Dimensao.TRANSPARENCIA_CORRESPONDENTE, Dimensao.IDENTIDADE_COMERCIAL_DECLARADA}
        )
        if (
            len(confusao) >= config_pontuacao.min_indicadores_forte
            and tem_alegacao_ou_visual_alta
            and tem_lacuna_transparencia
        ):
            notas.append(
                "uso destacado de logotipo/identidade visual ou linguagem da instituição sem CNPJ/declaração "
                "de correspondente: elevado a INDICIO_FORTE (ALTA)"
            )
            cls = C.INDICIO_FORTE
            prio = Prioridade.ALTA
            if caso == CasoCenario.B_COMUNICACAO_AMBIGUA:
                caso = CasoCenario.C_POSSIVEL_CANAL_OFICIAL
    if cls == C.INDICIO_FORTE:
        forte_ok = (
            len(confusao) >= config_pontuacao.min_indicadores_forte
            and suficiencia != SuficienciaEvidencia.INSUFICIENTE
            and (evidencia.utilizavel or sinais.tem_evidencia_descoberta)
        )
        if not forte_ok:
            notas.append(
                f"INDICIO_FORTE exige ≥{config_pontuacao.min_indicadores_forte} indicadores com evidência "
                "válida e evidência não insuficiente: rebaixado para REQUER_REVISAO"
            )
            cls = C.REQUER_REVISAO
    if cls in (C.INDICIO_FORTE,) and prio != Prioridade.ALTA:
        prio = Prioridade.ALTA
    if injecao:
        notas.append("possível prompt injection no conteúdo coletado: revisão humana obrigatória")
        if cls == C.SEM_INDICIO:
            cls = C.REQUER_REVISAO
            prio = max(prio, Prioridade.MEDIA, key=lambda p: ORDEM_PRIORIDADE[p])

    liberacao_parcial = evidencia.status == StatusColeta.PARCIAL and cls == C.SEM_INDICIO
    if evidencia.status == StatusColeta.PARCIAL and suficiencia == SuficienciaEvidencia.SUFICIENTE:
        suficiencia = SuficienciaEvidencia.PARCIAL
    if liberacao_parcial:
        notas.append("coleta parcial: ausência de indícios não foi verificada em todo o conteúdo; revisão humana")
    exige_humano = (
        cls != C.SEM_INDICIO or injecao or liberacao_parcial or bool(saida.incertezas and cls != C.SEM_INDICIO)
    )
    return AvaliacaoMarca(
        caso=caso,
        classificacao=cls,
        prioridade=prio,
        dimensoes=saida.dimensoes,
        indicadores_confusao_marca=confusao,
        indicadores_relacao_legitima=legitimos,
        fatos_observados=saida.fatos_observados,
        incertezas=saida.incertezas,
        referencias_evidencia=refs,
        resumo_raciocinio=saida.resumo_raciocinio,
        exige_revisao_humana=exige_humano or (saida.exige_revisao_humana and cls != C.SEM_INDICIO),
        confianca_extracao=(
            saida.confianca_extracao if (evidencia.utilizavel or sinais.tem_evidencia_descoberta) else Nivel.NENHUMA
        ),
        suficiencia_evidencia=suficiencia,
        possivel_injecao_prompt=injecao,
        identidade_visual_antiga=saida.identidade_visual_antiga,
        analisador=analisador,
        versao_prompt=versao_prompt,
        referencias_descartadas=descartadas,
        notas_salvaguarda=notas,
    )


def precisa_revisao(avaliacao: AvaliacaoMarca, revisar_todas: bool = False) -> bool:
    if revisar_todas:
        return True
    if avaliacao.classificacao == C.INCONCLUSIVA and avaliacao.confianca_extracao == Nivel.NENHUMA:
        return False
    return (
        avaliacao.classificacao != C.SEM_INDICIO
        or avaliacao.confianca_extracao in (Nivel.BAIXA, Nivel.NENHUMA)
        or avaliacao.possivel_injecao_prompt
        or (avaliacao.classificacao == C.SEM_INDICIO and avaliacao.exige_revisao_humana)
    )


def aplicar_revisao(avaliacao: AvaliacaoMarca, revisao: Revisao | None) -> tuple[C, Prioridade, list[str]]:
    # O revisor pode confirmar ou rebaixar, mas não sobe sozinho para INDICIO_FORTE: nesse caso fica
    # REQUER_REVISAO e a decisão é humana.
    notas: list[str] = []
    cls, prio = avaliacao.classificacao, avaliacao.prioridade
    if revisao is None:
        return cls, prio, notas
    rc, rp = revisao.classificacao_revisada, revisao.prioridade_revisada
    if revisao.decisao == DecisaoRevisao.INCONCLUSIVA:
        return C.INCONCLUSIVA, max(prio, Prioridade.MEDIA, key=lambda p: ORDEM_PRIORIDADE[p]), notas
    if ORDEM_CLASSIFICACAO[rc] > ORDEM_CLASSIFICACAO[cls]:
        if rc == C.INDICIO_FORTE and cls != C.INDICIO_FORTE:
            notas.append(
                "revisor sugeriu escalar para INDICIO_FORTE; limitado a REQUER_REVISAO para decisão humana"
            )
            rc = C.REQUER_REVISAO
        if revisao.decisao not in (DecisaoRevisao.REVISAO_HUMANA, DecisaoRevisao.CONFIRMAR):
            notas.append(
                f"revisor elevou a classificação com decisão {revisao.decisao.value}; mantido para revisão humana"
            )
            rc = max(rc, C.REQUER_REVISAO, key=lambda c: ORDEM_CLASSIFICACAO[c])
    if revisao.decisao == DecisaoRevisao.CONFIRMAR:
        return cls, prio, notas
    if (
        rc == C.SEM_INDICIO
        and avaliacao.possivel_injecao_prompt
        # texto gravado por marca.verificacao_cruzada; se mudar lá, mude aqui
        and any("divergência modelo × regras" in n for n in avaliacao.notas_salvaguarda)
    ):
        notas.append(
            "revisor sugeriu SEM_INDICIO em candidato com possível prompt injection e divergência "
            "modelo × regras; mantido REQUER_REVISAO para decisão humana"
        )
        return C.REQUER_REVISAO, max(rp, Prioridade.MEDIA, key=lambda p: ORDEM_PRIORIDADE[p]), notas
    return rc, rp, notas


ROTA_COBAN = "Gestão/auditoria de correspondentes (Res. 4.935)"
ROTA_JURIDICO = "Jurídico / proteção de marca"
ROTA_FRAUDE = "Segurança / prevenção a fraudes"
ROTA_RECOLETA = "Verificação manual / nova coleta"
ROTA_NENHUMA = "Sem encaminhamento (amostrar para controle de qualidade)"


def sugerir_encaminhamento(cls: C, identidade: ClasseIdentidade) -> list[str]:
    # A identidade define quem age; a classificação define se alguém precisa agir.
    if cls == C.SEM_INDICIO:
        return [ROTA_NENHUMA]
    if cls == C.INCONCLUSIVA:
        return [ROTA_RECOLETA]
    saida: list[str] = []
    if identidade in (ClasseIdentidade.COBAN_CONFIRMADO, ClasseIdentidade.COBAN_POSSIVEL):
        saida.append(ROTA_COBAN)
    if identidade != ClasseIdentidade.COBAN_CONFIRMADO:
        saida.append(ROTA_JURIDICO)
    if cls == C.INDICIO_FORTE and identidade in (
        ClasseIdentidade.IDENTIDADE_DESCONHECIDA,
        ClasseIdentidade.OUTRO_TERCEIRO,
    ):
        saida.append(ROTA_FRAUDE)
    return saida


def compor_final(
    id_candidata: str,
    avaliacao: AvaliacaoMarca,
    revisao: Revisao | None,
    identidade: AtribuicaoIdentidade,
    conformidade: AvaliacaoConformidade,
    evidencia: Evidencia,
) -> DecisaoFinal:
    cls, prio, notas = aplicar_revisao(avaliacao, revisao)
    notas = [*avaliacao.notas_salvaguarda, *notas]
    if avaliacao.identidade_visual_antiga and not avaliacao.logo_antiga_isenta:
        # Logo descontinuada nunca é liberada automaticamente, nem pelo revisor.
        if cls == C.SEM_INDICIO:
            cls = C.REQUER_REVISAO
            notas.append(
                "revisor sugeriu SEM_INDICIO, mas a página usa o logotipo antigo; mantido para revisão humana"
            )
        prio = max(prio, Prioridade.ALTA, key=lambda p: ORDEM_PRIORIDADE[p])
    sensiveis = getattr(avaliacao, "dados_sensiveis_solicitados", [])
    if sensiveis:
        # Página com a marca pedindo credencial nunca é liberada automaticamente, nem pelo revisor.
        if cls in (C.SEM_INDICIO, C.INCONCLUSIVA):
            notas.append(
                f"resultado revisado para {cls.value}, mas a página usa a marca e pede "
                f"{', '.join(sensiveis)}; mantido para revisão humana"
            )
            cls = C.REQUER_REVISAO
        prio = max(prio, Prioridade.ALTA, key=lambda p: ORDEM_PRIORIDADE[p])
    limitacoes = [*evidencia.nao_inspecionado, *identidade.limitacoes, *conformidade.limitacoes]
    if avaliacao.analisador.startswith("heuristic"):
        limitacoes.append(
            "análise heurística offline (sem modelo multimodal): indícios visuais não avaliados; resultado é triagem"
        )
    if revisao is None and cls != C.SEM_INDICIO:
        limitacoes.append("revisão crítica automatizada não executada para este candidato")
    justificativa = avaliacao.resumo_raciocinio
    if revisao is not None:
        justificativa += f"\n[Revisor: {revisao.decisao.value}] {revisao.resumo_raciocinio}"
    refs_chave: list[str] = []
    for ind in avaliacao.indicadores_confusao_marca + avaliacao.indicadores_relacao_legitima:
        for r in ind.refs_evidencia:
            if r not in refs_chave:
                refs_chave.append(r)
    if revisao is not None and cls == C.SEM_INDICIO:
        exige_humano = (
            revisao.exige_revisao_humana
            or avaliacao.possivel_injecao_prompt
            or evidencia.status == StatusColeta.PARCIAL
        )
    else:
        exige_humano = (
            cls != C.SEM_INDICIO
            or avaliacao.exige_revisao_humana
            or (revisao is not None and revisao.exige_revisao_humana and cls != C.SEM_INDICIO)
        )
    return DecisaoFinal(
        id_candidata=id_candidata,
        encaminhamento=sugerir_encaminhamento(cls, identidade.classificacao),
        identidade_visual_antiga=avaliacao.identidade_visual_antiga,
        fonte_identidade_visual_antiga=getattr(avaliacao, "fonte_identidade_visual_antiga", ""),
        dados_sensiveis_solicitados=list(sensiveis),
        resumo_conformidade=[
            f"{f.id_requisito}: {f.status.value}"
            for f in conformidade.achados
            if f.status not in (StatusAchado.SEM_INDICIO_OBSERVADO,)
        ],
        classificacao=cls,
        prioridade=prio,
        caso=avaliacao.caso,
        classe_identidade=identidade.classificacao,
        confianca_extracao=avaliacao.confianca_extracao,
        confianca_atribuicao=identidade.confianca_atribuicao,
        suficiencia_evidencia=avaliacao.suficiencia_evidencia,
        exige_revisao_humana=exige_humano,
        status_revisao_humana="PENDENTE" if exige_humano else "NAO_EXIGIDA",
        justificativa=justificativa.strip(),
        evidencias_chave=refs_chave[:20],
        limitacoes=list(dict.fromkeys(limitacoes)),
        notas_salvaguarda=notas,
    )
