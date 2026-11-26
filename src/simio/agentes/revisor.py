from __future__ import annotations

import json
from pathlib import Path

from simio.agentes.evidencia import SinaisPagina
from simio.agentes.marca import bloco_contexto, bloco_nao_confiavel, carregar_imagens
from simio.marcas import PerfilMarca
from simio.nucleo.modelos import (
    AtribuicaoIdentidade,
    AvaliacaoMarca,
    ClasseIdentidade,
    ClassificacaoFinal,
    Contestacao,
    DecisaoRevisao,
    Evidencia,
    ItemEvidencia,
    Nivel,
    Prioridade,
    Revisao,
    SaidaRevisao,
    StatusColeta,
)
from simio.prompts import renderizar
from simio.provedores.llm import GatewayLLM, Parte

C = ClassificacaoFinal
ID_HEURISTICA = "heuristica-revisor-v1"


def contestacoes_deterministicas(
    a: AvaliacaoMarca, ev: Evidencia, sig: SinaisPagina, identidade: AtribuicaoIdentidade
) -> list[Contestacao]:
    ch: list[Contestacao] = []
    if (
        a.classificacao in (C.INDICIO_FORTE, C.REQUER_REVISAO)
        and identidade.classificacao == ClasseIdentidade.IDENTIDADE_DESCONHECIDA
    ):
        ch.append(
            Contestacao(
                topico="atribuicao_dominio",
                severidade=Nivel.MEDIA,
                preocupacao="Responsável pela página não identificado; a conclusão sobre a marca "
                "não pode ser atribuída a um estabelecimento específico.",
                refs_evidencia=["E-URL"],
            )
        )
    # O texto vem do coletor (navegador.py); aceita a mensagem antiga, em inglês, e a nova.
    if ev.status == StatusColeta.PARCIAL or any(
        t in e.lower() for e in ev.erros for t in ("challenge", "captcha", "desafio anti-bot")
    ):
        ch.append(
            Contestacao(
                topico="suficiencia_evidencia",
                severidade=Nivel.ALTA,
                preocupacao="Coleta parcial ou desafio anti-bot: conteúdo pode estar incompleto.",
                refs_evidencia=["E-COLLECT"] if ev.erros else ["E-URL"],
            )
        )
    if sig.declaracoes and a.classificacao == C.INDICIO_FORTE:
        ch.append(
            Contestacao(
                topico="declaracao_legitima",
                severidade=Nivel.ALTA,
                preocupacao="Há declaração de correspondente/independência na página; avaliar se "
                "o destaque é suficiente antes de manter indício forte.",
                refs_evidencia=sorted({h.ref for h in sig.declaracoes})[:5],
            )
        )
    if (
        sig.contexto_multibanco
        and not sig.marca_no_dominio
        and not sig.alegacoes_ligadas_marca
        and a.classificacao == C.INDICIO_FORTE
    ):
        ch.append(
            Contestacao(
                topico="intermediacao_multibanco",
                severidade=Nivel.ALTA,
                preocupacao="A página menciona múltiplas instituições financeiras concorrentes "
                f"({', '.join(sig.bancos_concorrentes[:4])}) sem domínio próprio da marca nem alegação "
                "de canal oficial; provável intermediação multimarcas.",
                refs_evidencia=["E-URL"],
            )
        )
    if sig.contexto_editorial and not sig.marca_no_dominio and a.classificacao == C.INDICIO_FORTE:
        ch.append(
            Contestacao(
                topico="editorial_ou_plataforma_terceiros",
                severidade=Nivel.ALTA,
                preocupacao="A URL/página aparenta ser conteúdo editorial, portal de reclamações, processo "
                "judicial ou documentação de terceiros, não um canal de captação passando-se pelo banco.",
                refs_evidencia=["E-URL"],
            )
        )
    if a.indicadores_confusao_marca and all(i.forca == Nivel.BAIXA for i in a.indicadores_confusao_marca):
        ch.append(
            Contestacao(
                topico="interpretacao_excessiva",
                severidade=Nivel.MEDIA,
                preocupacao="Todos os indícios de confusão têm força baixa.",
                refs_evidencia=[r for i in a.indicadores_confusao_marca for r in i.refs_evidencia][:5],
            )
        )
    # TODO: comparar com marca.ID_HEURISTICA (e o legado "heuristic-v1") em vez do prefixo; hoje
    # funciona só porque "heuristica-v1" começa com "heuristic".
    if a.analisador.startswith("heuristic") and a.classificacao != C.SEM_INDICIO:
        ch.append(
            Contestacao(
                topico="contexto_visual_nao_analisado",
                severidade=Nivel.MEDIA,
                preocupacao="Identidade visual não foi avaliada por modelo multimodal; verificar screenshots.",
                refs_evidencia=["E-SHOT-VIEWPORT"] if ev.artefato("captura_viewport") else ["E-URL"],
            )
        )
    if sig.declaracoes and a.indicadores_confusao_marca:
        ch.append(
            Contestacao(
                topico="evidencias_contraditorias",
                severidade=Nivel.BAIXA,
                preocupacao="Coexistem declaração de correspondente e indícios de confusão.",
                refs_evidencia=sorted({h.ref for h in sig.declaracoes})[:3],
            )
        )
    return ch


def revisao_heuristica(
    a: AvaliacaoMarca, ev: Evidencia, sig: SinaisPagina, identidade: AtribuicaoIdentidade, pode_recoletar: bool
) -> SaidaRevisao:
    ch = contestacoes_deterministicas(a, ev, sig, identidade)
    topicos = {c.topico for c in ch}
    cls, prio = a.classificacao, a.prioridade
    if "suficiencia_evidencia" in topicos and pode_recoletar and ev.repetivel:
        return SaidaRevisao(
            decisao=DecisaoRevisao.RECOLETAR,
            classificacao_revisada=cls,
            prioridade_revisada=prio,
            contestacoes=ch,
            motivo_recoleta="coleta parcial com falha transitória",
            resumo_raciocinio="Nova coleta solicitada antes de concluir.",
        )
    if (
        topicos & {"declaracao_legitima", "intermediacao_multibanco", "editorial_ou_plataforma_terceiros"}
        and cls == C.INDICIO_FORTE
    ):
        return SaidaRevisao(
            decisao=DecisaoRevisao.REBAIXAR,
            classificacao_revisada=C.REQUER_REVISAO,
            prioridade_revisada=Prioridade.ALTA,
            contestacoes=ch,
            evidencias_contraditorias=[c.preocupacao for c in ch if c.topico == "evidencias_contraditorias"],
            resumo_raciocinio="Indício forte rebaixado: existem sinais de transparência, intermediação "
            "multimarcas ou contexto editorial a serem ponderados por auditor.",
        )
    if "interpretacao_excessiva" in topicos and prio == Prioridade.ALTA:
        return SaidaRevisao(
            decisao=DecisaoRevisao.REBAIXAR,
            classificacao_revisada=cls,
            prioridade_revisada=Prioridade.MEDIA,
            contestacoes=ch,
            resumo_raciocinio="Prioridade reduzida: indícios de baixa força.",
        )
    if ch:
        return SaidaRevisao(
            decisao=DecisaoRevisao.REVISAO_HUMANA,
            classificacao_revisada=cls,
            prioridade_revisada=prio,
            contestacoes=ch,
            resumo_raciocinio="Questionamentos registrados; encaminhado para revisão humana.",
        )
    return SaidaRevisao(
        decisao=DecisaoRevisao.CONFIRMAR,
        classificacao_revisada=cls,
        prioridade_revisada=prio,
        resumo_raciocinio="Nenhum questionamento determinístico; indícios mantidos.",
        exige_revisao_humana=cls != C.SEM_INDICIO,
    )


class Revisor:
    def __init__(
        self,
        marca: PerfilMarca,
        dir_campanha: Path,
        gateway: GatewayLLM | None = None,
        modelo: str | None = None,
        modo: str | None = None,
    ) -> None:
        self.marca = marca
        self.dir_campanha = dir_campanha
        self.gateway = gateway
        self.modelo = modelo
        self.modo = modo

    async def revisar(
        self,
        a: AvaliacaoMarca,
        ev: Evidencia,
        catalogo: list[ItemEvidencia],
        sig: SinaisPagina,
        identidade: AtribuicaoIdentidade,
        pode_recoletar: bool,
    ) -> Revisao:
        if self.gateway is not None and self.modelo and ev.utilizavel:
            versao, sistema = renderizar("revisor", nome_marca=self.marca.nome_exibicao)
            if self.modo:
                versao_modo, adendo = renderizar(self.modo, nome_marca=self.marca.nome_exibicao)
                versao, sistema = f"{versao}+{versao_modo}", f"{sistema}\n\n{adendo}"
            hipotese = a.model_dump(
                include={
                    "caso",
                    "classificacao",
                    "prioridade",
                    "indicadores_confusao_marca",
                    "indicadores_relacao_legitima",
                    "incertezas",
                    "resumo_raciocinio",
                    "suficiencia_evidencia",
                },
                mode="json",
            )
            partes: list[Parte] = [
                "## Contexto da coleta\n" + bloco_contexto(self.marca, ev, sig, identidade),
                "## Catálogo de evidências (dados não confiáveis)\n" + bloco_nao_confiavel(catalogo),
                *carregar_imagens(ev, self.dir_campanha),
                "## Hipótese do analista (a ser contestada, não é verdade estabelecida)\n"
                + json.dumps(hipotese, ensure_ascii=False, indent=1),
                f"Nova coleta permitida: {'sim' if pode_recoletar else 'não'}.",
            ]
            saida = await self.gateway.estruturada(
                agente="revisor", modelo=self.modelo, sistema=sistema, partes=partes, esquema=SaidaRevisao
            )
            if saida.decisao == DecisaoRevisao.RECOLETAR and not pode_recoletar:
                saida.decisao = DecisaoRevisao.INCONCLUSIVA
                saida.resumo_raciocinio += " (nova coleta não permitida: limite atingido)"
            valido = {i.ref for i in catalogo}
            for c in saida.contestacoes:
                c.refs_evidencia = [r for r in c.refs_evidencia if r in valido]
            conhecidos = {c.topico for c in saida.contestacoes}
            saida.contestacoes.extend(
                c for c in contestacoes_deterministicas(a, ev, sig, identidade) if c.topico not in conhecidos
            )
            return Revisao(**saida.model_dump(), revisor=self.modelo, versao_prompt=versao)
        saida = revisao_heuristica(a, ev, sig, identidade, pode_recoletar)
        return Revisao(**saida.model_dump(), revisor=ID_HEURISTICA, versao_prompt=None)
