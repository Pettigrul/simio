from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import pytest

from simio.agentes.evidencia import calcular_sinais, montar_catalogo
from simio.agentes.marca import analise_heuristica
from simio.coletores.navegador import ColetorNavegador, sha256_arquivo
from simio.nucleo import caminhos
from simio.nucleo.decisao import ConfigPontuacao, aplicar_salvaguardas_marca
from simio.nucleo.modelos import CasoCenario, ClassificacaoFinal, StatusColeta
from simio.seguranca.politica_url import PoliticaUrl
from tests.auxiliares import MARCA

pytestmark = pytest.mark.browser


def _coletor(configuracoes) -> ColetorNavegador:
    return ColetorNavegador(configuracoes, PoliticaUrl.das_configuracoes(configuracoes.portas_permitidas, configuracoes.hosts_liberados_inseguro))


async def _coletar(configuracoes, url: str, cid: str = "Cx"):
    cdir = caminhos.dir_campanha(configuracoes.dir_saida, "t")
    async with _coletor(configuracoes) as col:
        return await col.coletar(cid, url, 1, cdir), cdir


async def test_coleta_capturas_texto_e_hashes(configuracoes, site) -> None:
    ev, cdir = await _coletar(configuracoes, f"{site}/legit_partner.html")
    assert ev.status == StatusColeta.OK, ev.erros
    assert ev.status_http == 200 and ev.url_final.endswith("/legit_partner.html")
    assert ev.pagina and "Auto Center Exemplo" in ev.pagina.titulo
    assert "correspondente bancário" in ev.pagina.texto_cabecalho
    assert "CNPJ 11.222.333/0001-81" in ev.pagina.texto_rodape
    assert any("Simular" in c.texto for c in ev.pagina.ctas)
    tipos = {a.tipo for a in ev.artefatos}
    assert {"captura_viewport", "captura_completa", "html", "texto", "dados_pagina"} <= tipos
    for a in ev.artefatos:
        p = cdir / a.caminho
        assert p.is_file() and sha256_arquivo(p) == a.sha256 and p.stat().st_size == a.tamanho_bytes
    vp = cdir / ev.artefato("captura_viewport").caminho
    assert vp.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not any(a.caminho.endswith(".html") for a in ev.artefatos)  # HTML bruto fica como .html.txt
    manifesto = json.loads((cdir / ev.caminho_manifesto).read_text(encoding="utf-8"))
    assert manifesto["algoritmo_hash"] == "SHA-256" and len(manifesto["artefatos"]) == len(ev.artefatos)
    assert sha256_arquivo(cdir / ev.caminho_manifesto) == ev.sha256_manifesto
    assert ev.caminho_manifesto == "evidencias/127.0.0.1_x/tentativa-1/manifesto.json"
    assert vp.name == "tela-inicial.png"


async def test_segue_redirecionamento_permitido(configuracoes, site) -> None:
    ev, _ = await _coletar(configuracoes, f"{site}/redirect-ok")
    assert ev.status == StatusColeta.OK
    assert ev.cadeia_redirecionamentos == [f"{site}/redirect-ok"]
    assert ev.url_final.endswith("/legit_partner.html")


@pytest.mark.parametrize("caminho", ["/redirect-private", "/redirect-metadata"])
async def test_redirecionamento_para_destino_interno_eh_bloqueado(configuracoes, site, acessos_servidor, caminho: str) -> None:
    # Regressão: o route() do Playwright não roda nos saltos de redirect; o proxy de saída roda.
    ev, _ = await _coletar(configuracoes, f"{site}{caminho}")
    assert ev.status == StatusColeta.BLOQUEADA
    assert ev.pagina is None
    assert any("localhost" in b or "169.254" in b for b in ev.requisicoes_bloqueadas), ev.requisicoes_bloqueadas
    assert not [h for h in acessos_servidor if h[0].startswith("localhost")], acessos_servidor


async def test_redirecionamento_de_subrecurso_para_interno_eh_bloqueado(configuracoes, site, acessos_servidor) -> None:
    ev, _ = await _coletar(configuracoes, f"{site}/subresource_redirect.html")
    assert ev.status == StatusColeta.OK
    assert any("localhost" in b for b in ev.requisicoes_bloqueadas), ev.requisicoes_bloqueadas
    assert ("127.0.0.1:" + site.rsplit(":", 1)[1], "/redirect-private") in acessos_servidor
    assert not [h for h in acessos_servidor if h[0].startswith("localhost")], acessos_servidor


async def test_websocket_para_interno_eh_bloqueado(configuracoes, site) -> None:
    ev, _ = await _coletar(configuracoes, f"{site}/websocket_probe.html")
    assert any("10.0.0.2" in b for b in ev.requisicoes_bloqueadas), ev.requisicoes_bloqueadas


async def test_subrecursos_internos_sao_bloqueados(configuracoes, site) -> None:
    ev, _ = await _coletar(configuracoes, f"{site}/ssrf_probe.html")
    assert ev.status == StatusColeta.OK
    bloqueadas = " ".join(ev.requisicoes_bloqueadas)
    for alvo in ("169.254.169.254", "10.0.0.1", "localhost", "192.168.0.1", "[::1]"):
        assert alvo in bloqueadas, (alvo, ev.requisicoes_bloqueadas)


async def test_url_inicial_bloqueada_nunca_eh_buscada(configuracoes) -> None:
    ev, _ = await _coletar(configuracoes, "http://169.254.169.254/latest/meta-data/")
    assert ev.status == StatusColeta.BLOQUEADA and not ev.artefatos[:-1]
    ev2, _ = await _coletar(configuracoes, "file:///etc/passwd")
    assert ev2.status == StatusColeta.BLOQUEADA


async def test_pagina_indisponivel_eh_falha_nao_ausencia(configuracoes, site) -> None:
    ev, _ = await _coletar(configuracoes, f"{site}/unavailable")
    assert ev.status == StatusColeta.FALHOU and ev.status_http == 503
    assert any("HTTP 503" in e for e in ev.erros)


async def test_host_inalcancavel_eh_falha(configuracoes) -> None:
    ev, _ = await _coletar(configuracoes, "http://127.0.0.1:9/")  # nada escuta na porta 9
    assert ev.status == StatusColeta.FALHOU and ev.pagina is None
    assert any("não inspecionado" in n for n in ev.nao_inspecionado)


async def test_timeout_eh_falha_repetivel(configuracoes, site) -> None:
    configuracoes.timeout_navegacao_ms = 1500
    ev, _ = await _coletar(configuracoes, f"{site}/slow")
    assert ev.status == StatusColeta.FALHOU and ev.repetivel
    assert any("timeout" in e for e in ev.erros)


async def test_download_nao_eh_salvo(configuracoes, site) -> None:
    ev, cdir = await _coletar(configuracoes, f"{site}/download")
    assert ev.status in (StatusColeta.FALHOU, StatusColeta.BLOQUEADA)
    assert not list(cdir.rglob("*.exe"))


@pytest.mark.parametrize(
    ("pagina", "casos_esperados"),
    [
        ("legit_partner.html", {CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA}),
        ("ambiguous.html", {CasoCenario.B_COMUNICACAO_AMBIGUA}),
        ("lookalike.html", {CasoCenario.D_PERSONIFICACAO_FORTE}),
        (
            "third_party_official_contact.html",
            {CasoCenario.C_POSSIVEL_CANAL_OFICIAL, CasoCenario.D_PERSONIFICACAO_FORTE},
        ),
        ("unrelated.html", {CasoCenario.NAO_RELEVANTE}),
    ],
)
async def test_classificacao_heuristica_nas_fixtures_renderizadas(configuracoes, site, pagina, casos_esperados) -> None:
    ev, _ = await _coletar(configuracoes, f"{site}/{pagina}")
    cat = montar_catalogo(ev, MARCA)
    sig = calcular_sinais(ev, MARCA, cat)
    refs = {i.ref for i in cat}
    saida = analise_heuristica(sig, ConfigPontuacao(), ev, None, refs)
    a = aplicar_salvaguardas_marca(
        saida,
        analisador="heuristica-v1",
        versao_prompt=None,
        refs_validas=refs,
        evidencia=ev,
        sinais=sig,
        config_pontuacao=ConfigPontuacao(),
    )
    assert a.caso in casos_esperados, (a.caso, sig.como_observacoes())
    if pagina == "legit_partner.html":
        assert sig.declaracao_acima_dobra and sig.ouvidoria and sig.links_oficiais
        assert a.classificacao == ClassificacaoFinal.SEM_INDICIO


async def test_pagina_com_injecao_de_prompt_sinalizada(configuracoes, site) -> None:
    ev, _ = await _coletar(configuracoes, f"{site}/injection.html")
    cat = montar_catalogo(ev, MARCA)
    sig = calcular_sinais(ev, MARCA, cat)
    assert sig.marcadores_injecao


def test_estrutura_dir_evidencias(configuracoes) -> None:
    assert isinstance(configuracoes.dir_saida, Path)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc inspection is Linux-only")
async def test_chromium_roda_com_sandbox_do_so(configuracoes) -> None:
    # o Playwright desliga o sandbox por padrão; o SIMIO tem que manter ligado

    def descendentes(pid: int) -> list[int]:
        filhos: dict[int, list[int]] = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    ppid = int(Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[1].split()[1])
                except (OSError, IndexError, ValueError):
                    continue
                filhos.setdefault(ppid, []).append(int(d))
        saida, a_fazer = [], [pid]
        while a_fazer:
            for k in filhos.get(a_fazer.pop(), []):
                saida.append(k)
                a_fazer.append(k)
        return saida

    async with ColetorNavegador(configuracoes, PoliticaUrl()):
        linhas_cmd = []
        for p in descendentes(os.getpid()):
            with contextlib.suppress(OSError):
                linhas_cmd.append(Path(f"/proc/{p}/cmdline").read_bytes().replace(b"\0", b" ").decode())
    chrome = [c for c in linhas_cmd if "chrom" in c.lower()]
    assert chrome, "nenhum processo do Chromium encontrado"
    assert not [c for c in chrome if "--no-sandbox" in c]
