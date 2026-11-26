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

M = TypeVar("M", bound=BaseModel)


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
        ev = (
            armazenamento.evidencia_por_id(id_ev)
            if id_ev is not None
            else armazenamento.evidencia_mais_recente(cand.id)
        )

        def obter(tipo: str, cls: type[M], id_ev: int | None = id_ev, cid: str = cand.id) -> M | None:
            return armazenamento.obter_avaliacao(cid, tipo, cls, id_evidencia=id_ev)

        resultados = armazenamento.resultados_descoberta(cand.id)
        avaliacao_marca = obter("marca", AvaliacaoMarca)
        visual = avaliacao_marca.correspondencias_visuais if avaliacao_marca else []
        saida.append(
            RegistroCandidata(
                candidata=cand,
                resultados=resultados,
                evidencia=ev,
                todas_evidencias=armazenamento.todas_evidencias(cand.id),
                catalogo=montar_catalogo(ev, marca, resultados_descoberta=resultados, correspondencias_visuais=visual)
                if ev
                else [],
                identidade=obter("identidade", AtribuicaoIdentidade),
                marca=avaliacao_marca,
                conformidade=obter("conformidade", AvaliacaoConformidade),
                revisao=obter("revisao", Revisao),
                final=obter("final", DecisaoFinal),
                transicoes=armazenamento.transicoes(cand.id),
            )
        )
    saida.sort(key=lambda r: r.chave_ordenacao)
    return saida
