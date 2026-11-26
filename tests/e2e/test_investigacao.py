# Ponta a ponta contra sites locais, com Chromium de verdade na coleta. O LLM é a heurística
# offline ou o ProvedorMock; o Vertex AI real nunca é chamado aqui.

from __future__ import annotations

import re

import pytest
from openpyxl import load_workbook
from typer.testing import CliRunner

from simio.cli import app
from simio.nucleo.modelos import (
    CasoCenario,
    DecisaoFinal,
    DecisaoRevisao,
    Prioridade,
    RegistroConsulta,
    ResultadoDescoberta,
    SaidaAnaliseMarca,
    SaidaRevisao,
)
from simio.nucleo.modelos import (
    ClassificacaoFinal as C,
)
from simio.nucleo.modelos import (
    StatusCandidata as S,
)
from simio.nucleo.orquestrador import OpcoesInvestigacao, Orquestrador
from simio.provedores.busca import importar_texto
from simio.provedores.mock import ProvedorMock
from simio.relatorios import gerar_relatorios

pytestmark = pytest.mark.browser

PAGINAS = [
    "legit_partner.html",
    "ambiguous.html",
    "lookalike.html",
    "third_party_official_contact.html",
    "contradictory.html",
    "injection.html",
    "unavailable",
]


def _urls(site: str) -> str:
    return "\n".join(f"{site}/{p}" for p in PAGINAS)


def _finais(armazenamento, cid) -> dict[str, DecisaoFinal]:
    saida = {}
    for c in armazenamento.listar_candidatas(cid):
        f = armazenamento.obter_avaliacao(c.id, "final", DecisaoFinal)
        if f:
            saida[c.url.rsplit("/", 1)[-1]] = f
    return saida


async def test_investigacao_offline_ponta_a_ponta(configuracoes, armazenamento, site) -> None:
    configuracoes.offline = True
    resumo = await Orquestrador(configuracoes, armazenamento).investigar(OpcoesInvestigacao(importadas=importar_texto(_urls(site)), limite=50))
    assert resumo.status == "CONCLUIDA", resumo
    assert resumo.requisicoes_llm == 0
    contagens = resumo.contagens
    assert contagens.get("CONCLUIDA", 0) + contagens.get("INCONCLUSIVA", 0) == len(PAGINAS)
    finais = _finais(armazenamento, resumo.id_campanha)
    assert finais["legit_partner.html"].classificacao == C.SEM_INDICIO
    assert finais["ambiguous.html"].classificacao == C.REQUER_REVISAO
    assert finais["lookalike.html"].classificacao == C.INDICIO_FORTE
    assert finais["unavailable"].classificacao == C.INCONCLUSIVA
    assert finais["injection.html"].classificacao != C.SEM_INDICIO
    # evidência contraditória é contestada pelo revisor e não fica como forte
    assert finais["contradictory.html"].classificacao == C.REQUER_REVISAO
    assert finais["third_party_official_contact.html"].classe_identidade.value == "OUTRO_TERCEIRO"

    relatorio = gerar_relatorios(configuracoes, armazenamento, resumo.id_campanha)
    wb = load_workbook(relatorio.planilha)
    assert wb.sheetnames[:2] == ["Executive Summary", "Candidates"]
    for nome in (
        "Strong Indications",
        "Identity Attribution",
        "Evidence Index",
        "Investigation Log",
        "Errors and Limitations",
    ):
        assert nome in wb.sheetnames
    cand = wb["Candidates"]
    cabecalhos = [c.value for c in cand[1]]
    for h in (
        "ID",
        "Domínio",
        "URL",
        "Origem da descoberta",
        "Consulta utilizada",
        "Identidade atribuída",
        "Categoria de relacionamento",
        "Classificação",
        "Prioridade",
        "Justificativa",
        "Principais evidências",
        "Limitações",
        "Revisão humana",
        "Screenshot (viewport)",
        "Data da coleta",
    ):
        assert h in cabecalhos
    assert cand.max_row == len(PAGINAS) + 1
    assert wb["Strong Indications"].max_row >= 2
    # Dossiês das prioritárias: abrem offline, com escape e sem script.
    assert relatorio.dossies
    html = relatorio.dossies[next(iter(relatorio.dossies))].read_text(encoding="utf-8")
    assert "<script" not in html.lower()
    assert "Content-Security-Policy" in html
    id_semelhante = next(c.id for c in armazenamento.listar_candidatas(resumo.id_campanha) if c.url.endswith("lookalike.html"))
    dossie = relatorio.dossies[id_semelhante].read_text(encoding="utf-8")
    assert "E-SHOT-VIEWPORT" in dossie and "Art. 14" in dossie
    capturas = re.findall(r'<img src="([^"]+)"', dossie)
    assert capturas and all((relatorio.dossies[id_semelhante].parent / s).resolve().is_file() for s in capturas)
    id_inj = next(c.id for c in armazenamento.listar_candidatas(resumo.id_campanha) if c.url.endswith("injection.html"))
    inj = relatorio.dossies[id_inj].read_text(encoding="utf-8")
    assert "Ignore todas as instru" in inj and "font-size:2px" not in inj  # o texto fica, a marcação não é renderizada
    # Todo link local do relatório e da planilha abre um arquivo da pasta da campanha.
    pasta = relatorio.dir_campanha
    assert relatorio.planilha == pasta / "relatorio.xlsx" and relatorio.relatorio_html == pasta / "relatorio.html"
    links = re.findall(r'href="(dossies/[^"]+)"', relatorio.relatorio_html.read_text(encoding="utf-8"))
    assert len(links) == len(relatorio.dossies) and all((pasta / h).is_file() for h in links)
    hiperlinks = [c.hyperlink.target for ws in wb.worksheets for r in ws.iter_rows() for c in r if c.hyperlink]
    assert any(h.startswith("evidencias/") for h in hiperlinks) and any(h.startswith("dossies/") for h in hiperlinks)
    assert all((pasta / h).is_file() for h in hiperlinks)


def _provedor_mock() -> ProvedorMock:
    def analista(sistema: str, partes) -> SaidaAnaliseMarca:
        texto = "\n".join(p for p in partes if isinstance(p, str))
        if "Site Oficial" in texto or "canal oficial" in texto:
            return SaidaAnaliseMarca(
                caso=CasoCenario.D_PERSONIFICACAO_FORTE,
                classificacao=C.INDICIO_FORTE,
                prioridade=Prioridade.ALTA,
                indicadores_confusao_marca=[
                    {
                        "dimensao": "LINGUAGEM_INSTITUCIONAL",
                        "descricao": "afirma ser canal oficial",
                        "refs_evidencia": ["E-TITLE"],
                        "forca": "ALTA",
                    },
                    {
                        "dimensao": "USO_LOGO",
                        "descricao": "logo da marca",
                        "refs_evidencia": ["E-SHOT-VIEWPORT"],
                        "forca": "ALTA",
                    },
                ],
                referencias_evidencia=["E-TITLE"],
                resumo_raciocinio="mock: impersonação",
            )
        return SaidaAnaliseMarca(
            caso=CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA,
            classificacao=C.SEM_INDICIO,
            prioridade=Prioridade.BAIXA,
            resumo_raciocinio="mock: legítimo",
            exige_revisao_humana=False,
        )

    def revisor(sistema: str, partes) -> SaidaRevisao:
        assert any("Hipótese do analista" in p for p in partes if isinstance(p, str))
        return SaidaRevisao(
            decisao=DecisaoRevisao.CONFIRMAR,
            classificacao_revisada=C.INDICIO_FORTE,
            prioridade_revisada=Prioridade.ALTA,
            resumo_raciocinio="mock: confirmado",
        )

    return ProvedorMock({"SaidaAnaliseMarca": analista, "SaidaRevisao": revisor})


class _BuscaFalsa:
    """Conector falso que devolve URLs dos sites locais (no lugar de Brave/Serper/Grounding)."""

    nome = "grounding"

    def __init__(self, urls: list[tuple[str, str]]) -> None:
        self.urls = urls

    def disponivel(self):
        return True, "grounding: fake"

    async def executar(self, hipotese, consulta):
        resultados = [
            ResultadoDescoberta(fonte=self.nome, url_original=u, titulo=t, consulta=consulta, id_hipotese=hipotese.id)
            for u, t in self.urls
        ]
        return resultados, RegistroConsulta(fonte=self.nome, consulta=consulta, id_hipotese=hipotese.id, qtd_resultados=len(resultados))


async def test_llm_investigar_com_descoberta_revisao_e_limite(configuracoes, armazenamento, site) -> None:
    configuracoes.modelo_analise = "mock-analyst"
    configuracoes.max_consultas_busca = 3
    provedor = _provedor_mock()
    busca = _BuscaFalsa(
        [
            (f"{site}/lookalike.html", "Banco BV | Site Oficial"),
            (f"{site}/legit_partner.html?utm_source=x", "Auto Center Exemplo"),
            (f"{site}/legit_partner.html", "Auto Center Exemplo"),
            (f"{site}/ambiguous.html", "Carros Exemplo"),
        ]
    )
    orq = Orquestrador(configuracoes, armazenamento, provedor=provedor, conectores=[busca])
    resumo = await orq.investigar(OpcoesInvestigacao(limite=2))
    cid = resumo.id_campanha
    # dedup: 3 URLs únicas; limite de 2 analisadas; 1 fica em DESCOBERTA (pendência rastreável)
    assert len(armazenamento.listar_candidatas(cid)) == 3
    assert resumo.contagens.get("DESCOBERTA") == 1
    assert len(armazenamento.listar_consultas(cid)) == 3
    finais = _finais(armazenamento, cid)
    olhar = finais["lookalike.html"]
    assert olhar.classificacao == C.INDICIO_FORTE and "[Revisor: CONFIRMAR]" in olhar.justificativa
    tipos = [c.esquema for c in provedor.chamadas]
    assert "SaidaRevisao" in tipos and "SaidaAnaliseMarca" in tipos
    # as imagens foram para o analista multimodal
    assert any(not isinstance(p, str) for c in provedor.chamadas for p in c.partes)
    assert resumo.requisicoes_llm == len(provedor.chamadas)


async def test_orcamento_esgotado_pausa_e_retomada_conclui(configuracoes, armazenamento, site) -> None:
    configuracoes.modelo_analise = "mock-analyst"
    configuracoes.max_requisicoes_llm = 1
    configuracoes.paralelismo = 1
    provedor = _provedor_mock()
    urls = importar_texto(f"{site}/lookalike.html\n{site}/legit_partner.html")
    s1 = await Orquestrador(configuracoes, armazenamento, provedor=provedor).investigar(OpcoesInvestigacao(importadas=urls, fontes=[]))
    assert s1.status == "PAUSADA_ORCAMENTO"
    assert armazenamento.contagem_por_status(s1.id_campanha).get("COLETADA", 0) >= 1
    # A retomada mantém o limite original da campanha, a não ser que seja sobrescrito.
    mesma = await Orquestrador(configuracoes, armazenamento, provedor=provedor).retomar(s1.id_campanha)
    assert mesma.status == "PAUSADA_ORCAMENTO"
    s2 = await Orquestrador(configuracoes, armazenamento, provedor=provedor).retomar(
        s1.id_campanha, sobrescritas={"max_requisicoes_llm": 50}
    )
    assert s2.status == "CONCLUIDA", s2
    assert set(s2.contagens) <= {"CONCLUIDA", "INCONCLUSIVA"}


async def test_retomar_apos_queda_recupera_em_andamento(configuracoes, armazenamento, site) -> None:
    configuracoes.offline = True
    s1 = await Orquestrador(configuracoes, armazenamento).investigar(OpcoesInvestigacao(importadas=importar_texto(f"{site}/ambiguous.html")))
    cid = s1.id_campanha
    # Simula uma queda no meio da coleta de uma candidata nova.
    id_cand, _ = armazenamento.upsert_candidata(
        cid,
        url=f"{site}/lookalike.html",
        url_normalizada="x-lookalike",
        hostname="127.0.0.1",
        dominio_registravel="127.0.0.1",
    )
    armazenamento.transicionar(id_cand, S.NA_FILA)
    armazenamento.transicionar(id_cand, S.COLETANDO)
    armazenamento.definir_status_campanha(cid, "EM_EXECUCAO")
    s2 = await Orquestrador(configuracoes, armazenamento).retomar(cid)
    assert s2.status == "CONCLUIDA"
    assert armazenamento.obter_candidata(id_cand).status == S.CONCLUIDA
    notas = [t["nota"] for t in armazenamento.transicoes(id_cand)]
    assert "recuperada após interrupção" in notas


async def test_llm_indisponivel_degrada_para_heuristica(configuracoes, armazenamento, site) -> None:
    from simio.provedores.llm import LLMIndisponivel

    def explodir(sistema, partes):
        raise LLMIndisponivel("403: permission denied")

    configuracoes.modelo_analise = "mock"
    orq = Orquestrador(configuracoes, armazenamento, provedor=ProvedorMock({"SaidaAnaliseMarca": explodir}))
    s = await orq.investigar(OpcoesInvestigacao(importadas=importar_texto(f"{site}/lookalike.html"), fontes=[]))
    assert s.status == "CONCLUIDA"
    f = next(iter(_finais(armazenamento, s.id_campanha).values()))
    assert any("LLM indisponível" in x for x in f.limitacoes)
    assert any("análise heurística no lugar" in d for d in s.diagnosticos)


async def test_saida_invalida_do_modelo_falha_apos_retentativas(configuracoes, armazenamento, site) -> None:
    configuracoes.modelo_analise = "mock"
    orq = Orquestrador(configuracoes, armazenamento, provedor=ProvedorMock({"SaidaAnaliseMarca": lambda s, p: "garbage"}))
    s = await orq.investigar(OpcoesInvestigacao(importadas=importar_texto(f"{site}/ambiguous.html"), fontes=[]))
    assert s.contagens == {"FALHOU": 1}
    assert any(e["tipo_erro"] == "SaidaLLMInvalida" for e in armazenamento.erros(s.id_campanha))


def test_cli_ajuda_e_protecoes(configuracoes, tmp_path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("diagnostico", "investigar", "retomar", "relatorio", "status", "testar"):
        assert cmd in r.output
    r = runner.invoke(app, ["investigar", "--help"])
    for opc in (
        "--limite",
        "--paralelismo",
        "--modelo",
        "--max-consultas",
        "--max-requisicoes-llm",
        "--max-tokens",
        "--fontes",
        "--dir-saida",
        "--offline",
        "--arquivo-urls",
    ):
        assert opc in r.output
    saida = str(tmp_path / "cli-out")
    r = runner.invoke(app, ["investigar", "--offline", "-o", saida])
    assert r.exit_code == 2 and "offline" in r.output.lower()
    r = runner.invoke(app, ["status", "-o", saida])
    assert r.exit_code == 1


def test_cli_offline_investigar_relatorio_status(configuracoes, site, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SIMIO_HOSTS_LIBERADOS_INSEGURO", "127.0.0.1")
    monkeypatch.setenv("SIMIO_ESPERA_ESTABILIZAR_MS", "100")
    monkeypatch.setenv("SIMIO_ATRASO_BASE_RETENTATIVA_S", "0")
    urls = tmp_path / "urls.txt"
    urls.write_text(f"{site}/legit_partner.html\n{site}/lookalike.html\n", encoding="utf-8")
    saida = str(tmp_path / "cli-out")
    runner = CliRunner()
    r = runner.invoke(app, ["investigar", "--offline", "--arquivo-urls", str(urls), "-o", saida])
    assert r.exit_code == 0, r.output
    assert "CONCLUIDA" in r.output and ".xlsx" in r.output
    r = runner.invoke(app, ["status", "-o", saida])
    assert r.exit_code == 0 and "CONCLUIDA" in r.output
    r = runner.invoke(app, ["relatorio", "-o", saida, "--todos-dossies"])
    assert r.exit_code == 0 and "Dossiês: 2" in r.output
    r = runner.invoke(app, ["status", "--listar", "-o", saida])
    assert r.exit_code == 0 and "_bv_ampla" in r.output
