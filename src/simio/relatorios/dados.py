from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from simio.agentes.evidencia import montar_catalogo
from simio.marcas import PerfilMarca
from simio.nucleo.armazenamento import Armazenamento
from simio.nucleo.modelos import (
    ORDEM_CLASSIFICACAO,
    ORDEM_PRIORIDADE,
    AtribuicaoIdentidade,
    AvaliacaoConformidade,
    AvaliacaoMarca,
    Candidata,
    DecisaoFinal,
    Evidencia,
    ItemEvidencia,
    ResultadoDescoberta,
    Revisao,
)
from simio.relatorios.textos_antigos import traduzir

M = TypeVar("M", bound=BaseModel)


def _traduzido(obj: M | None, *campos: str) -> M | None:
    """Cópia só para exibição, com os textos antigos em inglês já em português; o banco não muda."""
    if obj is None:
        return None
    mudancas: dict[str, Any] = {}
    for campo in campos:
        valor = getattr(obj, campo)
        if isinstance(valor, str):
            novo: Any = traduzir(valor)
        elif isinstance(valor, list):
            novo = [traduzir(v) if isinstance(v, str) else v for v in valor]
        else:
            continue
        if novo != valor:
            mudancas[campo] = novo
    return obj.model_copy(update=mudancas) if mudancas else obj


def _marca_traduzida(m: AvaliacaoMarca | None) -> AvaliacaoMarca | None:
    m = _traduzido(m, "resumo_raciocinio", "fatos_observados", "incertezas", "notas_salvaguarda", "versao_prompt")
    if m is None:
        return None
    return m.model_copy(
        update={
            "dimensoes": [_traduzido(d, "constatacao") for d in m.dimensoes],
            "indicadores_confusao_marca": [_traduzido(i, "descricao") for i in m.indicadores_confusao_marca],
            "indicadores_relacao_legitima": [_traduzido(i, "descricao") for i in m.indicadores_relacao_legitima],
        }
    )


def _conformidade_traduzida(c: AvaliacaoConformidade | None) -> AvaliacaoConformidade | None:
    c = _traduzido(c, "notas_fora_escopo", "limitacoes")
    if c is None:
        return None
    return c.model_copy(update={"achados": [_traduzido(a, "hipotese", "informacoes_faltantes") for a in c.achados]})


def _revisao_traduzida(r: Revisao | None) -> Revisao | None:
    r = _traduzido(r, "resumo_raciocinio", "evidencias_contraditorias", "motivo_recoleta", "versao_prompt")
    if r is None:
        return None
    return r.model_copy(update={"contestacoes": [_traduzido(c, "preocupacao") for c in r.contestacoes]})


@dataclass
class RegistroCandidata:
    candidata: Candidata
    resultados: list[ResultadoDescoberta]
    evidencia: Evidencia | None
    todas_evidencias: list[Evidencia]
    catalogo: list[ItemEvidencia]
    identidade: AtribuicaoIdentidade | None
    marca: AvaliacaoMarca | None
    conformidade: AvaliacaoConformidade | None
    revisao: Revisao | None
    final: DecisaoFinal | None
    transicoes: list[dict[str, Any]]

    @property
    def chave_ordenacao(self) -> tuple[int, int, float]:
        if self.final is None:
            return (1, 0, -self.candidata.pontuacao_prioridade)
        return (
            -ORDEM_CLASSIFICACAO[self.final.classificacao],
            -ORDEM_PRIORIDADE[self.final.prioridade],
            -self.candidata.pontuacao_prioridade,
        )

    @property
    def eh_prioritaria(self) -> bool:
        f = self.final
        return f is not None and (
            f.classificacao.value in ("INDICIO_FORTE", "REQUER_REVISAO") or f.prioridade.value == "ALTA"
        )


def carregar_registros(armazenamento: Armazenamento, id_campanha: str, marca: PerfilMarca) -> list[RegistroCandidata]:
    saida: list[RegistroCandidata] = []
    for cand in armazenamento.listar_candidatas(id_campanha):
        # Uma rodada coerente: a evidência que gerou a decisão final e as avaliações feitas sobre ela
        # (e não, por exemplo, uma revisão RECOLETAR de uma rodada anterior).
        id_ev = armazenamento.id_evidencia_avaliacao(cand.id, "final")
        ev = _traduzido(
            armazenamento.evidencia_por_id(id_ev)
            if id_ev is not None
            else armazenamento.evidencia_mais_recente(cand.id),
            "erros",
            "nao_inspecionado",
            "requisicoes_bloqueadas",
        )

        def obter(tipo: str, cls: type[M], id_ev: int | None = id_ev, cid: str = cand.id) -> M | None:
            return armazenamento.obter_avaliacao(cid, tipo, cls, id_evidencia=id_ev)

        resultados = armazenamento.resultados_descoberta(cand.id)
        avaliacao_marca = _marca_traduzida(obter("marca", AvaliacaoMarca))
        visual = avaliacao_marca.correspondencias_visuais if avaliacao_marca else []
        saida.append(
            RegistroCandidata(
                candidata=_traduzido(cand, "ultimo_erro", "motivos_prioridade") or cand,
                resultados=resultados,
                evidencia=ev,
                todas_evidencias=[
                    _traduzido(e, "erros", "nao_inspecionado", "requisicoes_bloqueadas") or e
                    for e in armazenamento.todas_evidencias(cand.id)
                ],
                catalogo=montar_catalogo(ev, marca, resultados_descoberta=resultados, correspondencias_visuais=visual)
                if ev
                else [],
                identidade=_traduzido(
                    obter("identidade", AtribuicaoIdentidade), "fundamentacao", "inferencias", "limitacoes"
                ),
                marca=avaliacao_marca,
                conformidade=_conformidade_traduzida(obter("conformidade", AvaliacaoConformidade)),
                revisao=_revisao_traduzida(obter("revisao", Revisao)),
                final=_traduzido(
                    obter("final", DecisaoFinal),
                    "justificativa",
                    "limitacoes",
                    "notas_salvaguarda",
                    "resumo_conformidade",
                ),
                transicoes=[{**t, "nota": traduzir(t.get("nota"))} for t in armazenamento.transicoes(cand.id)],
            )
        )
    saida.sort(key=lambda r: r.chave_ordenacao)
    return saida
