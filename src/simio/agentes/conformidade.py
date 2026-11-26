from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from simio.nucleo import traducao_legado as legado
from simio.nucleo.modelos import (
    AchadoConformidade,
    AtribuicaoIdentidade,
    AvaliacaoConformidade,
    AvaliacaoMarca,
    ClasseIdentidade,
    ClassificacaoFinal,
    Dimensao,
    FonteRequisito,
    Sinal,
    StatusAchado,
)

log = logging.getLogger(__name__)

DIR_CONHECIMENTO = Path(__file__).resolve().parent.parent / "conhecimento"


@dataclass
class Requisito:
    id: str
    citacao: str
    texto: str
    fonte: FonteRequisito
    dimensoes: list[Dimensao]
    hipotese: str
    destinatario: str = ""


@dataclass
class CatalogoRequisitos:
    requisitos: list[Requisito] = field(default_factory=list)
    controles: list[dict[str, str]] = field(default_factory=list)
    fonte_normativa: str = ""
    nota_versao: str = ""
    contratuais_carregados: bool = False

    @classmethod
    def carregar(cls, caminho_contrato: Path | None = None) -> CatalogoRequisitos:
        with (DIR_CONHECIMENTO / "cmn_4935.toml").open("rb") as fh:
            dados = tomllib.load(fh)
        cat = cls(fonte_normativa=dados["fonte"], nota_versao=dados.get("nota_versao", ""))
        for r in dados.get("requisito", []):
            cat.requisitos.append(
                Requisito(
                    id=r["id"],
                    citacao=r["citacao"],
                    texto=r["texto"].strip(),
                    fonte=FonteRequisito.NORMATIVO,
                    dimensoes=[Dimensao(d) for d in r["dimensoes"]],
                    hipotese=r["hipotese"],
                    destinatario=r.get("destinatario", ""),
                )
            )
        cat.controles = [
            {"id": c["id"], "citacao": c["citacao"], "texto": c["texto"].strip()} for c in dados.get("controle", [])
        ]
        if caminho_contrato is not None:
            contratuais = carregar_requisitos_contratuais(caminho_contrato)
            cat.requisitos.extend(contratuais)
            cat.contratuais_carregados = bool(contratuais)
            if not contratuais:
                log.warning("nenhum requisito em %s; o mapeamento fica só no texto normativo", caminho_contrato)
        return cat


_CHAVES_REQUISITO = {"id", "citacao", "texto", "fonte", "dimensoes", "hipotese", "destinatario"}


def carregar_requisitos_contratuais(caminho: Path) -> list[Requisito]:
    # Aceita também o formato antigo ([[requirement]], citation, text, dimensões em inglês).
    with caminho.open("rb") as fh:
        dados = tomllib.load(fh)
    blocos = [*dados.get("requisito", []), *dados.get("requirement", [])]
    requisitos = []
    for n, bruto in enumerate(blocos, 1):
        r = {legado.REQUISITO_CONTRATUAL.get(k, k): v for k, v in bruto.items()}
        desconhecidas = sorted(set(r) - _CHAVES_REQUISITO)
        faltando = sorted({"id", "citacao", "texto", "dimensoes"} - set(r))
        if desconhecidas or faltando:
            problemas = []
            if desconhecidas:
                problemas.append(f"chaves desconhecidas: {', '.join(desconhecidas)}")
            if faltando:
                problemas.append(f"faltam: {', '.join(faltando)}")
            raise ValueError(f"{caminho}: requisito {bruto.get('id', n)} inválido ({'; '.join(problemas)})")
        try:
            fonte = FonteRequisito(legado.enum(r.get("fonte", "CONTRATUAL")))
            dimensoes = [Dimensao(legado.enum(d)) for d in r["dimensoes"]]
        except ValueError as exc:
            raise ValueError(f"{caminho}: requisito {r['id']}: {exc}") from exc
        requisitos.append(
            Requisito(
                id=r["id"],
                citacao=r["citacao"],
                texto=r["texto"].strip(),
                fonte=fonte,
                dimensoes=dimensoes,
                hipotese=r.get("hipotese", "Possível descumprimento de requisito contratual"),
                destinatario=r.get("destinatario", ""),
            )
        )
    return requisitos


_HIPOTESES = (
    StatusAchado.HIPOTESE_NAO_CONFORMIDADE,
    StatusAchado.HIPOTESE_CONDICIONADA_VINCULO,
    StatusAchado.NAO_APLICAVEL_SALVO_COBAN,
)


# Sem LLM de propósito: só cita requisitos do catálogo versionado (transcrito do texto oficial) e do
# arquivo contratual informado pelo operador, para nunca inventar artigo.
class AnalistaConformidade:
    def __init__(self, catalogo: CatalogoRequisitos) -> None:
        self.catalogo = catalogo

    @staticmethod
    def _texto_hipotese(req: Requisito, dims_resultado: list[Dimensao], declaracao_vista: bool) -> str:
        if req.id == "CMN4935-ART14-III" and declaracao_vista:
            outras = [d.value for d in dims_resultado if d != Dimensao.TRANSPARENCIA_CORRESPONDENTE]
            return (
                "Há declaração da condição de correspondente/prestador, mas outros elementos da página "
                f"({', '.join(outras) or 'apresentação da marca'}) podem contradizê-la ou reduzir seu efeito "
                "(destaque, legibilidade e identificação da contratante a verificar)"
            )
        return req.hipotese

    def avaliar(
        self, marca: AvaliacaoMarca, identidade: AtribuicaoIdentidade, ouvidoria_observada: bool
    ) -> AvaliacaoConformidade:
        saida = AvaliacaoConformidade()
        if not self.catalogo.contratuais_carregados:
            saida.limitacoes.append(
                "requisitos contratuais/internos não fornecidos: mapeamento limitado ao texto normativo"
            )
        dims = {d.dimensao: d for d in marca.dimensoes}
        refs_confusao: dict[Dimensao, list[str]] = {}
        for ind in marca.indicadores_confusao_marca:
            refs_confusao.setdefault(ind.dimensao, []).extend(ind.refs_evidencia)
        for d in marca.dimensoes:
            if d.sinal == Sinal.CONFUSAO:
                refs_confusao.setdefault(d.dimensao, []).extend(d.refs_evidencia)

        inconclusivos = marca.classificacao == ClassificacaoFinal.INCONCLUSIVA
        confirmados = identidade.classificacao == ClasseIdentidade.COBAN_CONFIRMADO
        possiveis = identidade.classificacao == ClasseIdentidade.COBAN_POSSIVEL
        transparencia = dims.get(Dimensao.TRANSPARENCIA_CORRESPONDENTE)
        declaracao_vista = any(
            i.dimensao == Dimensao.TRANSPARENCIA_CORRESPONDENTE for i in marca.indicadores_relacao_legitima
        ) or (transparencia is not None and transparencia.sinal in (Sinal.LEGITIMO, Sinal.NEUTRO))

        for req in self.catalogo.requisitos:
            dims_resultado = [d for d in req.dimensoes if d in refs_confusao]
            refs = sorted({r for d in dims_resultado for r in refs_confusao[d]})
            faltantes: list[str] = []
            hipotese = self._texto_hipotese(req, dims_resultado, declaracao_vista)
            if inconclusivos:
                status = StatusAchado.NAO_AVALIAVEL
                faltantes.append("evidência insuficiente/coleta incompleta")
                hipotese = ""
            elif not dims_resultado:
                legitimos = [d for d in req.dimensoes if dims.get(d) and dims[d].sinal == Sinal.LEGITIMO]
                status = StatusAchado.SEM_INDICIO_OBSERVADO
                refs = sorted({r for d in legitimos for r in dims[d].refs_evidencia})
                hipotese = ""
            elif req.fonte == FonteRequisito.NORMATIVO and confirmados:
                status = StatusAchado.HIPOTESE_NAO_CONFORMIDADE
            elif req.fonte == FonteRequisito.NORMATIVO and possiveis:
                status = StatusAchado.HIPOTESE_CONDICIONADA_VINCULO
                hipotese = f"{hipotese}. Aplicável somente se confirmado o vínculo de correspondente."
                faltantes.append(
                    "vínculo de correspondente não confirmado em cadastro autorizado (relação pública da "
                    "contratante — art. 21 — ou base interna)"
                )
            elif req.fonte == FonteRequisito.NORMATIVO:
                status = StatusAchado.NAO_APLICAVEL_SALVO_COBAN
                hipotese = (
                    f"{hipotese}. A norma disciplina correspondentes contratados; o "
                    "responsável não foi identificado como correspondente."
                )
                faltantes.append(
                    "confirmar se o responsável é correspondente contratado "
                    "(relação pública da contratante — art. 21 — ou base interna)"
                )
            else:
                status = (
                    StatusAchado.HIPOTESE_NAO_CONFORMIDADE
                    if confirmados
                    else StatusAchado.HIPOTESE_CONDICIONADA_VINCULO
                )
            if req.id == "CMN4935-ART14-III" and not ouvidoria_observada and status in _HIPOTESES:
                faltantes.append(
                    "canais de contato/ouvidoria da contratante não observados no material "
                    "coletado (podem existir em páginas não inspecionadas)"
                )
            if req.fonte == FonteRequisito.NORMATIVO and status in (
                StatusAchado.HIPOTESE_NAO_CONFORMIDADE,
                StatusAchado.HIPOTESE_CONDICIONADA_VINCULO,
            ):
                faltantes.append("contrato de correspondente vigente e suas cláusulas")
            saida.achados.append(
                AchadoConformidade(
                    id_requisito=req.id,
                    fonte_requisito=req.fonte,
                    citacao=req.citacao,
                    texto_requisito=req.texto,
                    status=status,
                    evidencias_observadas=refs,
                    hipotese=hipotese,
                    informacoes_faltantes=faltantes,
                    exige_revisao_humana=status != StatusAchado.SEM_INDICIO_OBSERVADO,
                )
            )

        if any(f.status == StatusAchado.NAO_APLICAVEL_SALVO_COBAN for f in saida.achados):
            saida.notas_fora_escopo.append(
                "Se o responsável não for correspondente contratado, os indícios podem caracterizar uso "
                "indevido de marca ou fraude, fora do escopo da Res. 4.935: avaliar encaminhamento às "
                "áreas jurídica/segurança."
            )
        if any(
            f.status in (StatusAchado.HIPOTESE_NAO_CONFORMIDADE, StatusAchado.HIPOTESE_CONDICIONADA_VINCULO)
            for f in saida.achados
        ):
            saida.controles_relacionados = [f"{c['citacao']}: {c['texto']}" for c in self.catalogo.controles]
        return saida
