"""Orchestrator: explicit, checkpointed pipeline over a SQLite-backed state machine.

Flow per candidate::

    DESCOBERTA -> NA_FILA -> COLETANDO -> COLETADA -> ANALISANDO -> [REVISANDO] -> CONCLUIDA
                                                                         |-> INCONCLUSIVA
                                       (bounded recollect: REVISANDO -> NA_FILA)
    any stage -> FALHOU after ``max_tentativas`` internal errors

Agents never talk to each other directly; the orchestrator passes typed outputs along.
Parallelism is limited to independent candidates (``paralelismo``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from simio.agentes.conformidade import AnalistaConformidade, CatalogoRequisitos
from simio.agentes.descoberta import (
    FOCO_AMEACAS,
    FOCO_LOGO_ANTIGA,
    FOCOS_SEM_COBANS,
    AgenteDescoberta,
    deduplicar,
    hipoteses_coban,
    priorizar,
    resumir_consultas,
)
from simio.agentes.evidencia import AgenteEvidencia, calcular_sinais, montar_catalogo
from simio.agentes.identidade import AgenteIdentidade, CadastroCoban
from simio.agentes.marca import AnalistaMarca
from simio.agentes.revisor import Revisor
from simio.agentes.visual import Referencia, carregar_referencia, detectar_logo_antiga, pistas_logo_antiga
from simio.coletores.navegador import ColetorNavegador
from simio.configuracoes import FONTES_DESCOBERTA, Configuracoes
from simio.marcas import PerfilMarca, carregar_marca
from simio.nucleo import caminhos
from simio.nucleo.armazenamento import Armazenamento, ModificacaoConcorrente
from simio.nucleo.decisao import ConfigPontuacao, compor_final, precisa_revisao
from simio.nucleo.estado import TransicaoInvalida
from simio.nucleo.modelos import (
    AtribuicaoIdentidade,
    AvaliacaoConformidade,
    AvaliacaoMarca,
    CasoCenario,
    ClassificacaoFinal,
    CorrespondenciaVisual,
    DecisaoRevisao,
    Evidencia,
    Nivel,
    Prioridade,
    RegistroConsulta,
    ResultadoDescoberta,
    Revisao,
    SuficienciaEvidencia,
)
from simio.nucleo.modelos import (
    StatusCandidata as S,
)
from simio.provedores.busca import BuscaBrave, BuscaCrtSh, BuscaLens, BuscaSerper, BuscaUrlscan, Conector
from simio.provedores.grounding import ConectorGrounding
from simio.provedores.llm import (
    GatewayLLM,
    LLMIndisponivel,
    Orcamento,
    OrcamentoEsgotado,
    ProvedorLLM,
)
from simio.provedores.typosquat import ConectorTyposquat
from simio.provedores.vertex import ProvedorVertex
from simio.seguranca.politica_url import PoliticaUrl

log = logging.getLogger(__name__)


NOTA_FOCO = (
    "campanha focada na identidade visual antiga: a comparação de imagem não encontrou o logotipo antigo "
    "e nenhuma imagem da página tem o nome da marca; a página não foi enviada aos modelos"
)
LIMITACAO_FOCO = "campanha focada (logo antiga): os demais aspectos de uso da marca não foram avaliados"


class Coletor(Protocol):
    async def coletar(self, id_candidata: str, url: str, tentativa: int, dir_campanha: Path) -> Evidencia: ...


@dataclass
class OpcoesInvestigacao:
    marca: str = "bv"
    limite: int = 50
    fontes: list[str] = field(default_factory=lambda: list(FONTES_DESCOBERTA))
    importadas: list[ResultadoDescoberta] = field(default_factory=list)
    revisar_todas: bool = False
    # Focused campaign (discovery.FOCUS_MODES), e.g. "logo-antiga": only that question is investigated.
    foco: str | None = None


@dataclass
class ResumoExecucao:
    id_campanha: str
    status: str
    contagens: dict[str, int]
    diagnosticos: list[str]
    requisicoes_llm: int
    tokens_llm: int


class Orquestrador:
    def __init__(
        self,
        configuracoes: Configuracoes,
        armazenamento: Armazenamento,
        *,
        provedor: ProvedorLLM | None = None,
        fabrica_coletor: Any = None,
        conectores: list[Conector] | None = None,
    ) -> None:
        self.configuracoes = configuracoes
        self.armazenamento = armazenamento
        self._provedor_forcado = provedor
        self._fabrica_coletor = fabrica_coletor
        self._conectores_forcados = conectores
        self.politica = PoliticaUrl.das_configuracoes(
            configuracoes.portas_permitidas, configuracoes.hosts_liberados_inseguro
        )
        self._parada = asyncio.Event()
        self._motivo_parada: str | None = None
        self._motivo_degradacao: str | None = None

    # ------------------------------------------------------------------ wiring
    @staticmethod
    def _referencias_logo_antiga(marca: PerfilMarca) -> list[Referencia]:
        refs: list[Referencia] = []
        for caminho in marca.caminhos_logo_antiga():
            try:
                refs.append(carregar_referencia(caminho))
            except (OSError, ValueError) as exc:
                log.warning("legacy logo reference ignored (%s): %s", caminho, exc)
        return refs

    def dir_campanha(self, id_campanha: str) -> Path:
        return caminhos.dir_campanha(self.configuracoes.dir_saida, id_campanha)

    def _gateway(self, id_campanha: str) -> GatewayLLM | None:
        if self.configuracoes.offline:
            return None
        provedor = self._provedor_forcado
        if provedor is None:
            if not self.configuracoes.vertex_configurado():
                return None
            provedor = ProvedorVertex(self.configuracoes)
        orcamento = Orcamento(
            self.armazenamento, id_campanha, self.configuracoes.max_requisicoes_llm, self.configuracoes.max_tokens_llm
        )
        return GatewayLLM(
            provedor,
            orcamento,
            max_retentativas=self.configuracoes.llm_max_retentativas,
            atraso_base_s=self.configuracoes.atraso_base_retentativa_s,
            concorrencia=self.configuracoes.paralelismo,
            max_tokens_saida=self.configuracoes.llm_max_tokens_saida,
        )

    def _conectores(self, gateway: GatewayLLM | None, marca: PerfilMarca, fontes: list[str]) -> list[Conector]:
        if self._conectores_forcados is not None:
            return self._conectores_forcados
        s = self.configuracoes
        chave = lambda v: v.get_secret_value() if v else None  # noqa: E731
        cadastro: dict[str, Conector] = {
            "grounding": ConectorGrounding(
                gateway, s.modelo_descoberta_efetivo, self.politica, marca.dominios_oficiais, s.timeout_http_s
            ),
            "brave": BuscaBrave(chave(s.chave_api_brave), s.timeout_http_s),
            "serper": BuscaSerper(
                chave(s.chave_api_serper),
                s.timeout_http_s,
                paginas=s.paginas_serper,
                variantes_simples=s.variantes_simples_serper,
            ),
            "urlscan": BuscaUrlscan(chave(s.chave_api_urlscan), s.timeout_http_s),
            "crtsh": BuscaCrtSh(s.timeout_http_s),
            "typosquat": ConectorTyposquat(marca),
            "lens": BuscaLens(chave(s.chave_api_serper), marca.urls_busca_logo_antiga, s.timeout_http_s),
        }
        return [cadastro[nome] for nome in fontes if nome in cadastro]

    def _coletor(self) -> AbstractAsyncContextManager[Any]:
        if self._fabrica_coletor is not None:
            return self._fabrica_coletor()
        return ColetorNavegador(self.configuracoes, self.politica)

    # ------------------------------------------------------------------ public API
    async def investigar(self, opcs: OpcoesInvestigacao) -> ResumoExecucao:
        marca = carregar_marca(opcs.marca, self.configuracoes.arquivo_marca)
        parametros = {
            "marca": opcs.marca,
            "limite": opcs.limite,
            "fontes": opcs.fontes,
            "offline": self.configuracoes.offline,
            "revisar_todas": opcs.revisar_todas,
            "foco": opcs.foco,
            "modelo_analise": self.configuracoes.modelo_analise,
            "modelo_revisao": self.configuracoes.modelo_revisao_efetivo,
            "modelo_descoberta": self.configuracoes.modelo_descoberta_efetivo,
            "paralelismo": self.configuracoes.paralelismo,
            "max_requisicoes_llm": self.configuracoes.max_requisicoes_llm,
            "max_tokens_llm": self.configuracoes.max_tokens_llm,
            "max_consultas_busca": self.configuracoes.max_consultas_busca,
            "urls_importadas": len(opcs.importadas),
        }
        id_campanha = self.armazenamento.criar_campanha(opcs.marca, parametros)
        log.info("campaign %s started", id_campanha)
        self.armazenamento.registrar_decisao(id_campanha, None, "orquestrador", "campanha_iniciada", parametros)
        if self.configuracoes.hosts_liberados_inseguro:
            self.armazenamento.registrar_decisao(
                id_campanha,
                None,
                "orquestrador",
                "HOSTS_LIBERADOS_INSEGURO_ATIVO",
                {
                    "hosts": self.configuracoes.hosts_liberados_inseguro,
                    "aviso": "SSRF protection bypassed for these hosts",
                },
            )
        # Imported URLs are persisted before any network work so a crash cannot lose them.
        self.armazenamento.salvar_checkpoint(
            id_campanha, "resultados_importados", [h.model_dump(mode="json") for h in opcs.importadas]
        )
        async with self._execucao_campanha(id_campanha):
            gateway = self._gateway(id_campanha)
            await self._descobrir(id_campanha, marca, gateway, opcs)
            return await self._processar(id_campanha, marca, gateway, opcs.revisar_todas, opcs.foco)

    PARAMS_RETOMAVEIS = (
        "offline",
        "modelo_analise",
        "modelo_revisao",
        "modelo_descoberta",
        "paralelismo",
        "max_requisicoes_llm",
        "max_tokens_llm",
        "max_consultas_busca",
    )

    async def retomar(
        self, id_campanha: str, repetir_falhas: bool = False, sobrescritas: dict[str, Any] | None = None
    ) -> ResumoExecucao:
        """Resume with the campaign's *original* parameters (model, offline mode, budgets...);
        only values the operator passes explicitly in ``sobrescritas`` change them."""
        camp = self.armazenamento.obter_campanha(id_campanha)
        if camp is None:
            raise KeyError(f"campaign not found: {id_campanha}")
        parametros = camp["parametros"]
        mesclado = {k: parametros[k] for k in self.PARAMS_RETOMAVEIS if parametros.get(k) is not None}
        mesclado.update({k: v for k, v in (sobrescritas or {}).items() if v is not None})
        self.configuracoes = self.configuracoes.model_copy(update=mesclado)
        marca = carregar_marca(parametros["marca"], self.configuracoes.arquivo_marca)
        async with self._execucao_campanha(id_campanha):
            recuperados = self.armazenamento.recuperar_em_andamento(id_campanha, self.configuracoes.max_tentativas)
            self.armazenamento.registrar_decisao(
                id_campanha,
                None,
                "orquestrador",
                "retomada",
                {
                    "recuperadas_em_andamento": recuperados,
                    "repetir_falhas": repetir_falhas,
                    "parametros_efetivos": {k: mesclado.get(k) for k in self.PARAMS_RETOMAVEIS},
                    "sobrescritas": {k: v for k, v in (sobrescritas or {}).items() if v is not None},
                },
            )
            if repetir_falhas:
                for cand in self.armazenamento.listar_candidatas(id_campanha, [S.FALHOU]):
                    self.armazenamento.zerar_tentativas(cand.id)
                    self.armazenamento.transicionar(cand.id, S.NA_FILA, nota="retry requested by operator")
            gateway = self._gateway(id_campanha)
            if not self.armazenamento.obter_checkpoint(id_campanha, "descoberta_concluida", False):
                importadas = [
                    ResultadoDescoberta.model_validate(h)
                    for h in (self.armazenamento.obter_checkpoint(id_campanha, "resultados_importados", []) or [])
                ]
                opcs = OpcoesInvestigacao(
                    marca=parametros["marca"],
                    limite=parametros["limite"],
                    fontes=parametros["fontes"],
                    revisar_todas=parametros.get("revisar_todas", False),
                    importadas=importadas,
                    foco=parametros.get("foco"),
                )
                await self._descobrir(id_campanha, marca, gateway, opcs)
            return await self._processar(
                id_campanha, marca, gateway, parametros.get("revisar_todas", False), parametros.get("foco")
            )

    @asynccontextmanager
    async def _execucao_campanha(self, id_campanha: str) -> AsyncIterator[None]:
        """Single-writer lease (two processes on one campaign would double-process candidates),
        heartbeat while running, and an explicit INTERROMPIDA status on Ctrl+C / crash."""
        dono = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
        titular = self.armazenamento.adquirir_trava(id_campanha, dono)
        if titular is not None:
            raise CampanhaOcupada(f"campaign {id_campanha} is being processed by {titular}")
        self.armazenamento.definir_status_campanha(id_campanha, "EM_EXECUCAO")

        async def pulsar() -> None:
            while True:
                await asyncio.sleep(30)
                self.armazenamento.renovar_trava(id_campanha, dono)

        hb = asyncio.create_task(pulsar())
        try:
            yield
        except BaseException as exc:
            status = "INTERROMPIDA" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "FALHOU"
            with contextlib.suppress(Exception):
                self.armazenamento.definir_status_campanha(id_campanha, status)
                self.armazenamento.registrar_erro(id_campanha, None, "orquestrador", type(exc).__name__, str(exc)[:500])
            raise
        finally:
            hb.cancel()
            with contextlib.suppress(Exception):
                self.armazenamento.liberar_trava(id_campanha, dono)

    # ------------------------------------------------------------------ discovery
    async def _descobrir(
        self, id_campanha: str, marca: PerfilMarca, gateway: GatewayLLM | None, opcs: OpcoesInvestigacao
    ) -> None:
        fontes = [s for s in opcs.fontes if s in FONTES_DESCOBERTA]
        conectores = (
            []
            if self.configuracoes.offline and self._conectores_forcados is None
            else self._conectores(gateway, marca, fontes)
        )
        # Resume-safety: queries already executed (and paid for) are skipped and count against the cap;
        # their hits were checkpointed as they arrived.
        feitas = {
            (q["fonte"], q["texto_consulta"])
            for q in self.armazenamento.listar_consultas(id_campanha)
            if q["fonte"] in fontes
        }
        if opcs.foco in FOCOS_SEM_COBANS:
            hips_coban, nota_coban = [], f"coban-seeded search (H9): not used in a focused campaign ({opcs.foco})"
        else:
            hips_coban, nota_coban = self._hipoteses_coban(id_campanha, marca, conectores)
        agente = AgenteDescoberta(
            marca,
            conectores,
            self.politica,
            max_consultas=self.configuracoes.max_consultas_busca,
            ja_feitas=feitas,
            hipoteses_extras=hips_coban,
            foco=opcs.foco,
        )
        parciais = [
            ResultadoDescoberta.model_validate(h)
            for h in (self.armazenamento.obter_checkpoint(id_campanha, "resultados_parciais_descoberta", []) or [])
        ]

        if opcs.importadas and not self.armazenamento.obter_checkpoint(id_campanha, "importadas_registradas", False):
            self.armazenamento.salvar_checkpoint(id_campanha, "importadas_registradas", True)
            por_fonte: dict[str, list[ResultadoDescoberta]] = {}
            for h in opcs.importadas:
                por_fonte.setdefault(h.fonte, []).append(h)
            for src, resultados in por_fonte.items():
                qid = self.armazenamento.registrar_consulta(
                    id_campanha, RegistroConsulta(fonte=src, consulta=src, status="OK", qtd_resultados=len(resultados))
                )
                for h in resultados:
                    h.metadados["id_consulta_db"] = qid

        def ao_consultar(registro: RegistroConsulta, resultados: list[ResultadoDescoberta]) -> None:
            qid = self.armazenamento.registrar_consulta(id_campanha, registro)
            for h in resultados:
                h.metadados["id_consulta_db"] = qid
            parciais.extend(resultados)
            self.armazenamento.salvar_checkpoint(
                id_campanha, "resultados_parciais_descoberta", [h.model_dump(mode="json") for h in parciais]
            )

        desfecho = await agente.executar([*opcs.importadas, *parciais], ao_consultar=ao_consultar)
        if nota_coban:
            desfecho.diagnosticos.append(nota_coban)
        if self.configuracoes.offline:
            desfecho.diagnosticos.insert(
                0, "offline mode: automatic discovery and AI analysis disabled; only imported URLs are processed"
            )
        grupos, rejeitados = deduplicar(desfecho.resultados, self.politica)
        for resultado, motivo in rejeitados:
            self.armazenamento.registrar_decisao(
                id_campanha,
                None,
                "descoberta",
                "url_rejeitada",
                {"url": resultado.url_original[:500], "fonte": resultado.fonte, "motivo": motivo},
            )
        ordenados = priorizar(grupos, marca, opcs.foco)
        # Candidates already queued/processed (earlier interrupted run) count against --limite.
        na_fila = sum(
            n for st, n in self.armazenamento.contagem_por_status(id_campanha).items() if st != S.DESCOBERTA.value
        )
        for g in ordenados:
            if g.motivo_exclusao:
                self.armazenamento.registrar_decisao(
                    id_campanha,
                    None,
                    "descoberta",
                    "url_excluida",
                    {"url": g.url, "motivo": g.motivo_exclusao, "fontes": sorted({h.fonte for h in g.resultados})},
                )
                continue
            cid, _ = self.armazenamento.upsert_candidata(
                id_campanha,
                url=g.url,
                url_normalizada=g.url_normalizada,
                hostname=g.hostname,
                dominio_registravel=g.dominio_registravel,
            )
            for h in g.resultados:
                self.armazenamento.adicionar_resultado_descoberta(cid, h.metadados.get("id_consulta_db"), h)
            self.armazenamento.definir_prioridade(cid, g.pontuacao, g.motivos)
            cand = self.armazenamento.obter_candidata(cid)
            eh_manual = any(h.fonte.startswith("import") for h in g.resultados)
            if cand.status == S.DESCOBERTA and na_fila < opcs.limite and (
                g.pontuacao >= self.configuracoes.pontuacao_minima_fila or eh_manual
            ):
                self.armazenamento.transicionar(cid, S.NA_FILA, nota=f"prioritised (score {g.pontuacao:.1f})")
                na_fila += 1
        info = {
            "resultados": len(desfecho.resultados),
            "urls_unicas": len(grupos),
            "rejeitados": len(rejeitados),
            "na_fila": na_fila,
            "fontes_usadas": desfecho.fontes_usadas,
            "consultas": resumir_consultas(desfecho.consultas),
            "diagnosticos": desfecho.diagnosticos,
        }
        self.armazenamento.salvar_checkpoint(id_campanha, "descoberta", info)
        self.armazenamento.salvar_checkpoint(id_campanha, "descoberta_concluida", True)
        self.armazenamento.registrar_decisao(id_campanha, None, "descoberta", "concluida", info)
        log.info("discovery: %d hits, %d unique, %d queued", len(desfecho.resultados), len(grupos), na_fila)

    def _hipoteses_coban(
        self, id_campanha: str, marca: PerfilMarca, conectores: list[Conector]
    ) -> tuple[list[Any], str | None]:
        """H9: searches seeded by the registered correspondents (complements the open-web sweep).
        Rotates across campaigns: correspondents searched in earlier campaigns are skipped."""
        s = self.configuracoes
        if s.limite_sementes_coban <= 0 or not conectores:
            return [], None
        if not s.caminho_cadastro_coban or not s.caminho_cadastro_coban.is_file():
            return [], "coban-seeded search (H9): SIMIO_CAMINHO_CADASTRO_COBAN not set; skipped"
        cadastro = CadastroCoban.carregar(s.caminho_cadastro_coban, oficial=s.cadastro_coban_oficial)
        pesquisados = self.armazenamento.cnpjs_coban_pesquisados(excluir_campanha=id_campanha)
        hips, n = hipoteses_coban(
            cadastro.registros, marca, limite=s.limite_sementes_coban, lote=s.lote_sementes_coban, excluir=pesquisados
        )
        restantes = len(cadastro.registros) - len(pesquisados) - n
        return hips, (
            f"coban-seeded search (H9): {n} registered correspondents in {len(hips)} prompt(s); "
            f"{len(pesquisados)} searched in earlier campaigns, ~{max(0, restantes)} not yet searched"
        )

    # ------------------------------------------------------------------ processing
    async def _processar(
        self,
        id_campanha: str,
        marca: PerfilMarca,
        gateway: GatewayLLM | None,
        revisar_todas: bool,
        foco: str | None = None,
    ) -> ResumoExecucao:
        s = self.configuracoes
        cdir = self.dir_campanha(id_campanha)
        cdir.mkdir(parents=True, exist_ok=True)
        cadastro = CadastroCoban.carregar(
            s.caminho_cadastro_coban,
            oficial=s.cadastro_coban_oficial,
            data_referencia=s.data_referencia_cadastro_coban,
        )
        agente_identidade = AgenteIdentidade(marca, cadastro)
        config_pontuacao = ConfigPontuacao.carregar(caminhos.arquivo_pontuacao(s.dir_saida))
        catalogo = CatalogoRequisitos.carregar(s.caminho_requisitos_contratuais)
        # Threat campaigns judge impersonation and risk to customers, not correspondent compliance.
        modo = "modo_ameacas" if foco == FOCO_AMEACAS else None
        analista = AnalistaMarca(marca, config_pontuacao, cdir, gateway, s.modelo_analise, modo=modo)
        revisor = Revisor(marca, cdir, gateway, s.modelo_revisao_efetivo, modo=modo)
        conformidade = AnalistaConformidade(catalogo)
        if gateway is None and not s.offline:
            self.armazenamento.registrar_decisao(
                id_campanha,
                None,
                "orquestrador",
                "llm_indisponivel",
                {"motivo": "Vertex AI not configured; heuristic analysis used"},
            )

        pendentes = self.armazenamento.listar_candidatas(id_campanha, [S.NA_FILA, S.COLETADA])
        if pendentes:
            semaforo = asyncio.Semaphore(s.paralelismo)
            async with self._coletor() as coletor:
                agente_evidencia = AgenteEvidencia(coletor, cdir)
                ctx = _Ctx(
                    id_campanha,
                    marca,
                    agente_evidencia,
                    agente_identidade,
                    analista,
                    revisor,
                    conformidade,
                    revisar_todas,
                    self._referencias_logo_antiga(marca),
                    foco,
                )

                async def trabalhador(cid: str) -> None:
                    async with semaforo:
                        if self._parada.is_set():
                            return
                        try:
                            await self._processar_candidata(ctx, cid)
                        except ModificacaoConcorrente as exc:
                            log.warning("%s", exc)
                        except Exception as exc:
                            # One unexpected error must not abort sibling workers (or close the
                            # shared browser under them): isolate it to this candidate.
                            log.exception("unexpected error processing %s", cid)
                            self.armazenamento.registrar_erro(
                                id_campanha, cid, "orquestrador", type(exc).__name__, str(exc)
                            )
                            self._falhar_com_seguranca(cid, f"unexpected error: {type(exc).__name__}: {exc}")

                retornos = await asyncio.gather(*(trabalhador(c.id) for c in pendentes), return_exceptions=True)
                for r in retornos:
                    if isinstance(r, BaseException) and not isinstance(r, Exception):
                        raise r

        contagens = self.armazenamento.contagem_por_status(id_campanha)
        restantes = contagens.get(S.NA_FILA.value, 0) + contagens.get(S.COLETADA.value, 0)
        if self._motivo_parada:
            status = "PAUSADA_ORCAMENTO"
        elif restantes:
            status = "PAUSADA"
        else:
            status = "CONCLUIDA"
        self.armazenamento.definir_status_campanha(id_campanha, status, finalizada=status == "CONCLUIDA")
        disc = self.armazenamento.obter_checkpoint(id_campanha, "descoberta", {}) or {}
        diagnosticos = list(disc.get("diagnosticos", []))
        if self._motivo_parada:
            diagnosticos.append(f"stopped: {self._motivo_parada}. Use `simio retomar` after adjusting budgets.")
        if self._motivo_degradacao:
            diagnosticos.append(f"LLM degraded to heuristic analysis: {self._motivo_degradacao}")
        req, tok = self.armazenamento.totais_uso(id_campanha)
        self.armazenamento.registrar_decisao(
            id_campanha,
            None,
            "orquestrador",
            "execucao_finalizada",
            {"status": status, "contagens": contagens, "requisicoes_llm": req, "tokens_llm": tok},
        )
        return ResumoExecucao(id_campanha, status, contagens, diagnosticos, req, tok)

    @staticmethod
    def _avaliacao_foco(ev: Evidencia) -> AvaliacaoMarca:
        utilizavel = ev.utilizavel
        return AvaliacaoMarca(
            caso=CasoCenario.NAO_RELEVANTE if utilizavel else CasoCenario.E_EVIDENCIA_INSUFICIENTE,
            classificacao=ClassificacaoFinal.SEM_INDICIO if utilizavel else ClassificacaoFinal.INCONCLUSIVA,
            prioridade=Prioridade.BAIXA,
            confianca_extracao=Nivel.ALTA if utilizavel else Nivel.BAIXA,
            suficiencia_evidencia=SuficienciaEvidencia.SUFICIENTE if utilizavel else SuficienciaEvidencia.INSUFICIENTE,
            analisador=f"foco:{FOCO_LOGO_ANTIGA}",
            resumo_raciocinio=NOTA_FOCO if utilizavel else "coleta sem conteúdo utilizável",
            notas_salvaguarda=[NOTA_FOCO],
        )

    def _falhar_com_seguranca(self, cid: str, nota: str) -> None:
        cand = self.armazenamento.obter_candidata(cid)
        if cand.status in (S.CONCLUIDA, S.FALHOU, S.INCONCLUSIVA):
            return
        try:
            if cand.status in (S.NA_FILA, S.COLETANDO, S.ANALISANDO, S.REVISANDO, S.COLETADA):
                self.armazenamento.transicionar(cid, S.FALHOU, nota=nota[:500])
        except TransicaoInvalida:
            log.error("could not mark %s FALHOU from %s", cid, cand.status.value)

    def _espera_retentativa(self, tentativa: int) -> float:
        base = self.configuracoes.atraso_base_retentativa_s
        return base * (2 ** max(0, tentativa - 1)) + random.uniform(0, base / 2 if base else 0)  # noqa: S311

    async def _processar_candidata(self, ctx: _Ctx, cid: str) -> None:
        st = self.armazenamento
        max_tentativas = self.configuracoes.max_tentativas
        while not self._parada.is_set():
            cand = st.obter_candidata(cid)
            if cand.status == S.NA_FILA:
                st.transicionar(cid, S.COLETANDO, esperado=S.NA_FILA)
                tentativa = st.incrementar(cid, "tentativas")
                indice_ev = len(st.todas_evidencias(cid)) + 1
                try:
                    ev = await ctx.evidencia.coletar(cid, cand.url, indice_ev)
                except Exception as exc:
                    st.registrar_erro(ctx.id_campanha, cid, "coleta", type(exc).__name__, str(exc))
                    if tentativa < max_tentativas:
                        st.transicionar(cid, S.NA_FILA, nota=f"collector error, retry {tentativa}")
                        await asyncio.sleep(self._espera_retentativa(tentativa))
                        continue
                    st.transicionar(cid, S.FALHOU, nota=f"collector error: {exc}"[:500])
                    return
                id_ev = st.salvar_evidencia(ev)
                st.registrar_decisao(
                    ctx.id_campanha,
                    cid,
                    "evidencia",
                    ev.status.value,
                    {
                        "tentativa": indice_ev,
                        "id_evidencia": id_ev,
                        "url_final": ev.url_final,
                        "erros": ev.erros[:5],
                        "requisicoes_bloqueadas": len(ev.requisicoes_bloqueadas),
                        "manifesto": ev.caminho_manifesto,
                    },
                )
                if ev.status.value == "FALHOU" and ev.repetivel and tentativa < max_tentativas:
                    st.transicionar(cid, S.NA_FILA, nota=f"transient collection failure, retry {tentativa}")
                    await asyncio.sleep(self._espera_retentativa(tentativa))
                    continue
                st.transicionar(cid, S.COLETADA)
                st.zerar_tentativas(cid)
            elif cand.status == S.COLETADA:
                feitas = await self._analisar(ctx, cid)
                if feitas:
                    return
            else:
                return

    async def _analisar(self, ctx: _Ctx, cid: str) -> bool:
        """Identity -> brand -> compliance -> review -> final. Returns True when the candidate is settled.

        Every assessment is stored with the id of the evidence it was computed from, so a retry or a
        resume after a crash/budget pause *reuses* paid LLM results for the same evidence instead of
        buying them again. Attempts are counted *before* the stage runs, so a candidate that crashes
        or hangs the process is eventually marked FALHOU (see ``recuperar_em_andamento``)."""
        st = self.armazenamento
        cand = st.obter_candidata(cid)
        st.transicionar(cid, S.ANALISANDO, esperado=S.COLETADA)
        tentativa = st.incrementar(cid, "tentativas")
        etapa = "preparar"
        try:
            mais_recente = st.evidencia_mais_recente_com_id(cid)
            if mais_recente is None:
                raise RuntimeError("no evidence stored for a COLETADA candidate")
            id_ev, ev = mais_recente
            resultados_disc = st.resultados_descoberta(cid)
            visual: list[CorrespondenciaVisual] = []
            if ctx.logos_antigas and ev.utilizavel:
                cdir = self.dir_campanha(ctx.id_campanha)
                for ref in ctx.logos_antigas:
                    visual += await asyncio.to_thread(detectar_logo_antiga, ev, cdir, ref)
                visual = sorted(visual, key=lambda m: -m.pontuacao)[:5]
            catalogo = montar_catalogo(
                ev, ctx.marca, resultados_descoberta=resultados_disc, correspondencias_visuais=visual
            )
            sinais = calcular_sinais(ev, ctx.marca, catalogo)

            etapa = "identidade"
            identidade: AtribuicaoIdentidade = ctx.identidade.atribuir(cid, ev, catalogo, sinais)
            st.salvar_avaliacao(cid, "identidade", identidade, id_ev)
            st.upsert_entidade(
                ctx.id_campanha, identidade, identidade.inferencias[-1] if identidade.inferencias else ""
            )
            st.registrar_decisao(
                ctx.id_campanha,
                cid,
                "identidade",
                identidade.classificacao.value,
                {"chave_entidade": identidade.chave_entidade, "confianca": identidade.confianca_atribuicao.value},
            )

            if ctx.foco == FOCO_LOGO_ANTIGA and not visual and not pistas_logo_antiga(ev):
                # Focused campaign: no sign of the legacy logo, settle without spending on the models.
                etapa = "final"
                avaliacao = self._avaliacao_foco(ev)
                st.salvar_avaliacao(cid, "marca", avaliacao, id_ev)
                comp = ctx.conformidade.avaliar(avaliacao, identidade, bool(sinais.ouvidoria))
                st.salvar_avaliacao(cid, "conformidade", comp, id_ev)
                final = compor_final(cid, avaliacao, None, identidade, comp, ev)
                final.limitacoes.append(LIMITACAO_FOCO)
                st.salvar_avaliacao(cid, "final", final, id_ev)
                st.registrar_decisao(
                    ctx.id_campanha, cid, "orquestrador", f"final:{final.classificacao.value}", {"foco": ctx.foco}
                )
                alvo = S.INCONCLUSIVA if final.classificacao == ClassificacaoFinal.INCONCLUSIVA else S.CONCLUIDA
                st.transicionar(cid, alvo)
                st.zerar_tentativas(cid)
                return True

            etapa = "marca"
            avaliacao = st.obter_avaliacao(cid, "marca", AvaliacaoMarca, id_evidencia=id_ev)
            if avaliacao is None or (avaliacao.analisador.startswith("heuristic") and ctx.analista.usa_llm):
                avaliacao = await ctx.analista.analisar(ev, catalogo, sinais, identidade)
                avaliacao = avaliacao.model_copy(update={"correspondencias_visuais": visual})
                st.salvar_avaliacao(cid, "marca", avaliacao, id_ev)
                st.registrar_decisao(
                    ctx.id_campanha,
                    cid,
                    "analista_marca",
                    avaliacao.classificacao.value,
                    {
                        "caso": avaliacao.caso.value,
                        "prioridade": avaliacao.prioridade.value,
                        "analisador": avaliacao.analisador,
                        "salvaguardas": avaliacao.notas_salvaguarda,
                        "id_evidencia": id_ev,
                    },
                )
            else:
                st.registrar_decisao(ctx.id_campanha, cid, "analista_marca", "reaproveitado", {"id_evidencia": id_ev})

            etapa = "conformidade"
            comp: AvaliacaoConformidade = ctx.conformidade.avaliar(avaliacao, identidade, bool(sinais.ouvidoria))
            st.salvar_avaliacao(cid, "conformidade", comp, id_ev)
            st.registrar_decisao(
                ctx.id_campanha,
                cid,
                "conformidade",
                "mapeado",
                {"achados": {f.id_requisito: f.status.value for f in comp.achados}},
            )

            revisao: Revisao | None = None
            if precisa_revisao(avaliacao, ctx.revisar_todas):
                etapa = "revisao"
                st.transicionar(cid, S.REVISANDO)
                pode_recoletar = cand.recoletas < self.configuracoes.max_recoletas
                revisao = st.obter_avaliacao(cid, "revisao", Revisao, id_evidencia=id_ev)
                if revisao is None:
                    revisao = await ctx.revisor.revisar(avaliacao, ev, catalogo, sinais, identidade, pode_recoletar)
                    st.salvar_avaliacao(cid, "revisao", revisao, id_ev)
                    st.registrar_decisao(
                        ctx.id_campanha,
                        cid,
                        "revisor",
                        revisao.decisao.value,
                        {
                            "revisada": revisao.classificacao_revisada.value,
                            "contestacoes": [c.topico for c in revisao.contestacoes],
                            "revisor": revisao.revisor,
                            "id_evidencia": id_ev,
                        },
                    )
                if revisao.decisao == DecisaoRevisao.RECOLETAR and pode_recoletar:
                    st.incrementar(cid, "recoletas")
                    st.zerar_tentativas(cid)
                    st.transicionar(cid, S.NA_FILA, nota=f"reviewer requested recollection: {revisao.motivo_recoleta}")
                    return False

            etapa = "final"
            final = compor_final(cid, avaliacao, revisao, identidade, comp, ev)
            if self._motivo_degradacao:
                final.limitacoes.append(f"LLM indisponível nesta execução: {self._motivo_degradacao}")
            st.salvar_avaliacao(cid, "final", final, id_ev)
            st.registrar_decisao(
                ctx.id_campanha,
                cid,
                "orquestrador",
                f"final:{final.classificacao.value}",
                {"prioridade": final.prioridade.value, "revisao_humana": final.exige_revisao_humana},
            )
            alvo = S.INCONCLUSIVA if final.classificacao == ClassificacaoFinal.INCONCLUSIVA else S.CONCLUIDA
            st.transicionar(cid, alvo)
            st.zerar_tentativas(cid)
            return True
        except OrcamentoEsgotado as exc:
            self._motivo_parada = str(exc)
            self._parada.set()
            st.registrar_erro(ctx.id_campanha, cid, etapa, "OrcamentoEsgotado", str(exc))
            st.zerar_tentativas(cid)  # not the candidate's fault
            st.transicionar(cid, S.COLETADA, nota="paused: budget exhausted (paid results kept for resume)")
            return True
        except LLMIndisponivel as exc:
            # Degrade gracefully, only the agent whose model failed; record the limitation.
            self._motivo_degradacao = f"{etapa}: {str(exc)[:300]}"
            st.registrar_erro(ctx.id_campanha, cid, etapa, "LLMIndisponivel", str(exc))
            if etapa == "revisao":
                ctx.revisor.gateway = None
            else:
                ctx.analista.gateway = None
                ctx.revisor.gateway = None
            st.zerar_tentativas(cid)
            st.transicionar(cid, S.COLETADA, nota=f"LLM unavailable at {etapa}; retrying with heuristic analysis")
            return False
        except Exception as exc:  # LLMOutputInvalid, TransientLLMError, unexpected bugs
            st.registrar_erro(ctx.id_campanha, cid, etapa, type(exc).__name__, str(exc))
            if tentativa < self.configuracoes.max_tentativas:
                st.transicionar(cid, S.COLETADA, nota=f"{etapa} error, retry {tentativa}: {type(exc).__name__}")
                await asyncio.sleep(self._espera_retentativa(tentativa))
                return False
            st.transicionar(cid, S.FALHOU, nota=f"{etapa} failed: {type(exc).__name__}: {exc}"[:500])
            return True


class CampanhaOcupada(RuntimeError):
    """Another live process holds the campaign lease."""


@dataclass
class _Ctx:
    id_campanha: str
    marca: PerfilMarca
    evidencia: AgenteEvidencia
    identidade: AgenteIdentidade
    analista: AnalistaMarca
    revisor: Revisor
    conformidade: AnalistaConformidade
    revisar_todas: bool
    logos_antigas: list[Referencia] = field(default_factory=list)
    foco: str | None = None
