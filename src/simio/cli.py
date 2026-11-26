"""Command-line interface."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from simio import __version__
from simio.config_logs import configurar_logging
from simio.configuracoes import FONTES_DESCOBERTA, Configuracoes, carregar_configuracoes

app = typer.Typer(
    name="simio",
    help="SIMIO — investigação de identidade digital e uso potencialmente indevido de marcas.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)

OpcaoSaida = Annotated[
    Path | None, typer.Option("--dir-saida", "-o", help="Diretório local de saída (DB, evidências, relatórios)")
]
OpcaoCampanha = Annotated[str | None, typer.Option("--campanha", "-c", help="ID da campanha (padrão: a mais recente)")]


def _configuracoes(**sobrescritas: object) -> Configuracoes:
    try:
        return carregar_configuracoes(**sobrescritas)
    except Exception as exc:  # pydantic ValidationError, bad env values
        typer.secho(f"Configuração inválida: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc


def _armazenamento(configuracoes: Configuracoes):  # type: ignore[no-untyped-def]
    from simio.nucleo.armazenamento import Armazenamento

    return Armazenamento(configuracoes.caminho_db)


def _resolver_campanha(armazenamento, campanha: str | None) -> str:  # type: ignore[no-untyped-def]
    cid = campanha or armazenamento.id_campanha_mais_recente()
    if not cid or armazenamento.obter_campanha(cid) is None:
        typer.secho("Nenhuma campanha encontrada. Execute `simio investigar` primeiro.", fg="red", err=True)
        raise typer.Exit(1)
    return cid


def _enriquecer(s: Configuracoes, armazenamento, id_campanha: str, atualizar: bool = False) -> None:  # type: ignore[no-untyped-def]
    """Domain intelligence (RDAP/DNS/hosting) of the flagged pages; never fails the command."""
    from simio.agentes.inteligencia_dominio import enriquecer_campanha

    if not s.inteligencia_dominio or s.offline:
        return
    try:
        intel = asyncio.run(enriquecer_campanha(s, armazenamento, id_campanha, atualizar=atualizar))
        typer.echo(f"Inteligência de domínio: {len(intel)} host(s) (RDAP, DNS, hospedagem)")
    except Exception as exc:  # network trouble must not cost the report
        typer.secho(f"Inteligência de domínio indisponível: {exc}", fg="yellow", err=True)


def _imprimir_resumo(resumo, relatorio=None) -> None:  # type: ignore[no-untyped-def]
    typer.secho(f"\nCampanha {resumo.id_campanha}: {resumo.status}", bold=True)
    typer.echo("Estados: " + json.dumps(resumo.contagens, ensure_ascii=False))
    typer.echo(f"LLM: {resumo.requisicoes_llm} requisições, {resumo.tokens_llm} tokens")
    for d in resumo.diagnosticos:
        typer.echo(f"  · {d}")
    if relatorio is not None:
        typer.secho(f"Relatório: {relatorio.planilha}", fg="green")
        if getattr(relatorio, "relatorio_html", None):
            typer.secho(f"Relatório HTML: {relatorio.relatorio_html}", fg="green")
        if getattr(relatorio, "relatorio_pdf", None):
            typer.secho(f"Relatório PDF: {relatorio.relatorio_pdf}", fg="green")
        if getattr(relatorio, "relatorio_ameacas", None):
            typer.secho(f"Relatório de ameaças: {relatorio.relatorio_ameacas}", fg="green")
        typer.echo(f"Dossiês: {len(relatorio.dossies)} em {relatorio.dir_relatorio / 'dossiers'}")


@app.callback()
def principal(
    versao: Annotated[bool, typer.Option("--versao", help="Mostra a versão")] = False,
) -> None:
    if versao:
        typer.echo(f"simio {__version__}")
        raise typer.Exit()


# --------------------------------------------------------------------------------- hunt
@app.command()
def investigar(
    marca: Annotated[str, typer.Option(help="Perfil de marca (ex.: bv)")] = "bv",
    limite: Annotated[int, typer.Option(min=1, help="Máximo de candidatos analisados")] = 50,
    paralelismo: Annotated[int | None, typer.Option(min=1, max=16, help="Candidatos processados em paralelo")] = None,
    modelo: Annotated[str | None, typer.Option(help="Modelo de análise (sobrepõe SIMIO_MODELO_ANALISE)")] = None,
    modelo_revisao: Annotated[str | None, typer.Option(help="Modelo do revisor")] = None,
    max_consultas: Annotated[int | None, typer.Option(min=0, help="Orçamento de consultas de descoberta")] = None,
    max_requisicoes_llm: Annotated[int | None, typer.Option(min=0, help="Orçamento de requisições LLM")] = None,
    max_tokens: Annotated[int | None, typer.Option(min=0, help="Orçamento de tokens LLM")] = None,
    fontes: Annotated[
        str | None,
        typer.Option(
            help=f"Fontes de descoberta, separadas por vírgula ({','.join(FONTES_DESCOBERTA)}). "
            "Padrão: todas; --foco logo-antiga: serper,lens; --foco ameacas: grounding,serper,typosquat,crtsh"
        ),
    ] = None,
    foco: Annotated[
        str,
        typer.Option(
            help="Questão da campanha. 'ampla' (padrão): varredura completa. "
            "Campanha focada em uma única questão. 'logo-antiga': só a identidade visual descontinuada "
            "(busca própria; só páginas com indício da logo vão aos modelos). 'ameacas': inteligência de ameaças "
            "sem o contexto de COBANs (logins e portais falsos, coleta de senha/cartão, apps, hospedagem gratuita)"
        ),
    ] = "ampla",
    arquivo_urls: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Importar URLs de arquivo (.txt/.csv)")
    ] = None,
    urls: Annotated[str | None, typer.Option(help="Importar URLs de texto livre")] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline", help="Sem chamadas a Vertex AI/APIs de busca; analisa só URLs importadas com heurística"
        ),
    ] = False,
    revisar_todas: Annotated[bool, typer.Option("--revisar-todas", help="Revisor avalia todos os candidatos")] = False,
    sem_relatorio: Annotated[bool, typer.Option("--sem-relatorio", help="Não gerar relatórios ao final")] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Descobre, coleta, analisa, revisa e reporta candidatos (investigação completa)."""
    from simio.nucleo.orquestrador import OpcoesInvestigacao, Orquestrador
    from simio.provedores.busca import importar_arquivo, importar_texto
    from simio.relatorios import gerar_relatorios

    s = _configuracoes(
        paralelismo=paralelismo,
        modelo_analise=modelo,
        modelo_revisao=modelo_revisao,
        max_consultas_busca=max_consultas,
        max_requisicoes_llm=max_requisicoes_llm,
        max_tokens_llm=max_tokens,
        dir_saida=dir_saida,
        offline=offline or None,
    )
    configurar_logging(s.nivel_log, s.dir_saida / "logs")
    if s.hosts_liberados_inseguro:
        typer.secho(
            f"ATENÇÃO: SIMIO_HOSTS_LIBERADOS_INSEGURO={s.hosts_liberados_inseguro} desativa a proteção SSRF para esses "
            "hosts (uso exclusivo em testes). Registrado na campanha.",
            fg="red",
            err=True,
        )
    from simio.agentes.descoberta import FOCO_AMPLA, FONTES_PADRAO_FOCO, MODOS_FOCO

    if foco not in MODOS_FOCO:
        typer.secho(f"Foco desconhecido: {foco} (opções: {', '.join(MODOS_FOCO)})", fg="red", err=True)
        raise typer.Exit(2)
    foco_efetivo = None if foco == FOCO_AMPLA else foco
    if fontes is None:
        fontes = ",".join(FONTES_PADRAO_FOCO[foco_efetivo] if foco_efetivo else FONTES_DESCOBERTA)
    src = [x.strip() for x in fontes.split(",") if x.strip()]
    desconhecidas = [x for x in src if x not in FONTES_DESCOBERTA]
    if desconhecidas:
        typer.secho(f"Fontes desconhecidas: {desconhecidas}", fg="red", err=True)
        raise typer.Exit(2)
    importadas = []
    if arquivo_urls:
        importadas += importar_arquivo(arquivo_urls)
    if urls:
        importadas += importar_texto(urls)
    if s.offline and not importadas:
        typer.secho("Modo offline requer URLs importadas (--arquivo-urls ou --urls).", fg="red", err=True)
        raise typer.Exit(2)
    armazenamento = _armazenamento(s)
    try:
        orq = Orquestrador(s, armazenamento)
        resumo = asyncio.run(
            orq.investigar(
                OpcoesInvestigacao(
                    marca=marca,
                    limite=limite,
                    fontes=src,
                    importadas=importadas,
                    revisar_todas=revisar_todas,
                    foco=foco_efetivo,
                )
            )
        )
        if not sem_relatorio:
            _enriquecer(s, armazenamento, resumo.id_campanha)
        relatorio = None if sem_relatorio else gerar_relatorios(s, armazenamento, resumo.id_campanha)
        _imprimir_resumo(resumo, relatorio)
        if resumo.contagens == {} and not importadas:
            raise typer.Exit(3)
    finally:
        armazenamento.fechar()


@app.command()
def retomar(
    campanha: OpcaoCampanha = None,
    repetir_falhas: Annotated[
        bool, typer.Option("--repetir-falhas", help="Recoloca candidatos FALHOU na fila")
    ] = False,
    max_requisicoes_llm: Annotated[int | None, typer.Option(min=0)] = None,
    max_tokens: Annotated[int | None, typer.Option(min=0)] = None,
    paralelismo: Annotated[int | None, typer.Option(min=1, max=16)] = None,
    offline: Annotated[bool, typer.Option("--offline")] = False,
    sem_relatorio: Annotated[bool, typer.Option("--sem-relatorio")] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Retoma uma campanha interrompida (falha, Ctrl+C ou orçamento esgotado)."""
    from simio.nucleo.orquestrador import CampanhaOcupada, Orquestrador
    from simio.relatorios import gerar_relatorios

    s = _configuracoes(dir_saida=dir_saida)
    # The campaign's original parameters (model, offline mode, budgets) are kept; only flags given
    # explicitly here override them.
    sobrescritas = {
        "max_requisicoes_llm": max_requisicoes_llm,
        "max_tokens_llm": max_tokens,
        "paralelismo": paralelismo,
        "offline": True if offline else None,
    }
    configurar_logging(s.nivel_log, s.dir_saida / "logs")
    armazenamento = _armazenamento(s)
    try:
        cid = _resolver_campanha(armazenamento, campanha)
        try:
            resumo = asyncio.run(
                Orquestrador(s, armazenamento).retomar(cid, repetir_falhas=repetir_falhas, sobrescritas=sobrescritas)
            )
        except CampanhaOcupada as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(4) from exc
        if not sem_relatorio:
            _enriquecer(s, armazenamento, cid)
        relatorio = None if sem_relatorio else gerar_relatorios(s, armazenamento, cid)
        _imprimir_resumo(resumo, relatorio)
    finally:
        armazenamento.fechar()


@app.command()
def enriquecer(
    campanha: OpcaoCampanha = None,
    atualizar: Annotated[bool, typer.Option("--atualizar", help="Consulta de novo os hosts já levantados")] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Levanta registro (RDAP/WHOIS), DNS e hospedagem dos achados e gera o relatório de novo."""
    from simio.relatorios import gerar_relatorios

    s = _configuracoes(dir_saida=dir_saida)
    armazenamento = _armazenamento(s)
    try:
        cid = _resolver_campanha(armazenamento, campanha)
        _enriquecer(s, armazenamento, cid, atualizar=atualizar)
        res = gerar_relatorios(s, armazenamento, cid)
        typer.secho(f"Relatório: {res.planilha}", fg="green")
    finally:
        armazenamento.fechar()


@app.command()
def relatorio(
    campanha: OpcaoCampanha = None,
    todos_dossies: Annotated[
        bool, typer.Option("--todos-dossies", help="Gerar dossiê para todos os candidatos")
    ] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Gera o Excel consolidado e os dossiês HTML a partir do banco local."""
    from simio.relatorios import gerar_relatorios

    s = _configuracoes(dir_saida=dir_saida)
    armazenamento = _armazenamento(s)
    try:
        cid = _resolver_campanha(armazenamento, campanha)
        res = gerar_relatorios(s, armazenamento, cid, todos_dossies=todos_dossies)
        typer.secho(f"Relatório: {res.planilha}", fg="green")
        if res.relatorio_html:
            typer.secho(f"Relatório HTML: {res.relatorio_html}", fg="green")
        if res.relatorio_pdf:
            typer.secho(f"Relatório PDF: {res.relatorio_pdf}", fg="green")
        if res.relatorio_ameacas:
            typer.secho(f"Relatório de ameaças: {res.relatorio_ameacas}", fg="green")
        typer.echo(f"Dossiês: {len(res.dossies)} em {res.dir_relatorio / 'dossiers'}")
    finally:
        armazenamento.fechar()


@app.command()
def status(
    campanha: OpcaoCampanha = None,
    listar_todas: Annotated[bool, typer.Option("--listar", help="Lista campanhas")] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Mostra o estado de uma campanha (ou lista campanhas)."""
    s = _configuracoes(dir_saida=dir_saida)
    if not s.caminho_db.exists():
        typer.echo("Nenhum banco encontrado em " + str(s.caminho_db))
        raise typer.Exit(1)
    armazenamento = _armazenamento(s)
    try:
        if listar_todas:
            for c in armazenamento.listar_campanhas(50):
                typer.echo(f"{c['id']}  {c['marca']:<6} {c['status']:<14} {c['criado_em']}")
            return
        cid = _resolver_campanha(armazenamento, campanha)
        camp = armazenamento.obter_campanha(cid) or {}
        req, tok = armazenamento.totais_uso(cid)
        typer.secho(f"Campanha {cid} ({camp.get('marca')}) — {camp.get('status')}", bold=True)
        typer.echo("Estados: " + json.dumps(armazenamento.contagem_por_status(cid), ensure_ascii=False))
        typer.echo(
            f"LLM: {req} requisições / {tok} tokens "
            f"(orçamento {camp.get('parametros', {}).get('max_requisicoes_llm')} / "
            f"{camp.get('parametros', {}).get('max_tokens_llm')})"
        )
        disc = armazenamento.obter_checkpoint(cid, "descoberta", {}) or {}
        typer.echo(f"Descoberta: {disc.get('urls_unicas', 0)} URLs únicas, fontes {disc.get('fontes_usadas', [])}")
        typer.echo(f"Erros registrados: {len(armazenamento.erros(cid))}")
    finally:
        armazenamento.fechar()


# --------------------------------------------------------------------------------- doctor
def _verificar(rotulo: str, ok: bool | None, detalhe: str = "") -> bool:
    selo = {
        True: typer.style("OK  ", fg="green"),
        False: typer.style("FAIL", fg="red"),
        None: typer.style("WARN", fg="yellow"),
    }[ok]
    typer.echo(f"[{selo}] {rotulo}" + (f" — {detalhe}" if detalhe else ""))
    return ok is not False


@app.command()
def diagnostico(
    real: Annotated[
        bool, typer.Option("--real", help="Executa chamadas reais mínimas a Vertex AI e Grounding")
    ] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Diagnostica ambiente, dependências, navegador, configuração e integrações."""
    s = _configuracoes(dir_saida=dir_saida)
    ok = True
    ok &= _verificar("Python", sys.version_info >= (3, 11), f"{platform.python_version()} ({sys.executable})")
    for mod in ("google.genai", "playwright", "pydantic", "openpyxl", "jinja2", "httpx", "tldextract"):
        try:
            m = importlib.import_module(mod)
            ok &= _verificar(f"módulo {mod}", True, getattr(m, "__version__", ""))
        except ImportError as exc:
            ok &= _verificar(f"módulo {mod}", False, str(exc))
    try:
        s.dir_saida.mkdir(parents=True, exist_ok=True)
        sondar = s.dir_saida / ".write-test"
        sondar.write_text("ok")
        sondar.unlink()
        _verificar("diretório de saída gravável", True, str(s.dir_saida.resolve()))
    except OSError as exc:
        ok &= _verificar("diretório de saída gravável", False, str(exc))
    try:
        st = _armazenamento(s)
        _verificar("SQLite", True, f"schema v{st.versao_esquema()} em {s.caminho_db}")
        st.fechar()
    except Exception as exc:
        ok &= _verificar("SQLite", False, str(exc))
    ok &= _verificar("navegador Playwright (Chromium)", *_verificar_navegador(s))
    try:
        from simio.marcas import carregar_marca

        b = carregar_marca("bv", s.arquivo_marca)
        _verificar("perfil de marca", True, f"{b.nome_exibicao}; domínios oficiais: {b.dominios_oficiais}")
        if not b.cnpjs_instituicao:
            _verificar("CNPJs da instituição no perfil", None, "não preenchidos (VALIDATE em bv.toml)")
    except Exception as exc:
        ok &= _verificar("perfil de marca", False, str(exc))
    _verificar("modo offline", None if s.offline else True, "ativado" if s.offline else "desativado")
    _verificar("Vertex: projeto", True if s.projeto_gcp else None, s.projeto_gcp or "SIMIO_PROJETO_GCP não definido")
    _verificar("Vertex: localização", True, s.local_gcp)
    _verificar(
        "Vertex: modelo de análise",
        True if s.modelo_analise else None,
        s.modelo_analise or "SIMIO_MODELO_ANALISE não definido (análise heurística será usada)",
    )
    _verificar("Vertex: modelo de revisão", True, s.modelo_revisao_efetivo or "(heurístico)")
    _verificar("Vertex: modelo de grounding", True, s.modelo_descoberta_efetivo or "(indisponível)")
    _verificar(*_verificar_adc())
    for nome in ("brave", "serper", "urlscan"):
        chave = getattr(s, f"chave_api_{nome}")
        _verificar(
            f"conector {nome}", True if chave else None, "configurado" if chave else "não configurado (opcional)"
        )
    try:
        from simio.agentes.visual import carregar_referencia
        from simio.marcas import carregar_marca

        bv = carregar_marca("bv", s.arquivo_marca)
        refs = [carregar_referencia(p) for p in bv.caminhos_logo_antiga()]
        _verificar(
            "referência da logo antiga",
            True if refs else None,
            f"{len(refs)} imagem(ns); busca reversa (lens): {len(bv.urls_busca_logo_antiga)} URL(s)"
            if refs
            else "não configurada (detecção por comparação de imagem desativada)",
        )
    except (OSError, ValueError) as exc:
        _verificar("referência da logo antiga", False, str(exc)[:200])
    if s.caminho_cadastro_coban:
        _verificar("cadastro de correspondentes", s.caminho_cadastro_coban.is_file(), str(s.caminho_cadastro_coban))
    else:
        _verificar("cadastro de correspondentes", None, "não configurado: COBAN_CONFIRMADO indisponível")
    if s.hosts_liberados_inseguro:
        _verificar(
            "política de rede",
            None,
            f"SIMIO_HOSTS_LIBERADOS_INSEGURO ativo ({s.hosts_liberados_inseguro}) — somente para testes!",
        )
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(var):
            _verificar(f"variável {var}", True, "definida")
    if s.proxy_navegador:
        _verificar("proxy do navegador", True, "definido (verificação pós-conexão de IP desativada)")
    if real:
        ok &= _verificacoes_reais(s)
    if not ok:
        raise typer.Exit(1)


def _verificar_navegador(s: Configuracoes) -> tuple[bool, str]:
    """Launch Chromium exactly as the collector does (sandbox setting included)."""

    async def executar() -> str:
        from playwright.async_api import async_playwright

        from simio.coletores.navegador import iniciar_chromium

        async with async_playwright() as p:
            b = await iniciar_chromium(p, sandbox=s.sandbox_navegador)
            v = b.version
            await b.close()
            return v

    sandbox = "sandbox ativo" if s.sandbox_navegador else "SANDBOX DESATIVADO (SIMIO_SANDBOX_NAVEGADOR=false)"
    try:
        return True, f"Chromium {asyncio.run(executar())}; {sandbox}"
    except Exception as exc:
        pista = (
            "se o erro citar sandbox/namespaces, consulte docs/deployment.md (SIMIO_SANDBOX_NAVEGADOR)"
            if s.sandbox_navegador
            else "execute `python -m playwright install chromium`"
        )
        return False, f"{str(exc).splitlines()[0][:200]} — {pista}; ou `python -m playwright install chromium`"


def _verificar_adc() -> tuple[str, bool | None, str]:
    try:
        import google.auth

        creds, projeto = google.auth.default()
        return "credenciais ADC", True, f"{type(creds).__name__} (projeto ADC: {projeto or '-'})"
    except Exception as exc:
        return (
            "credenciais ADC",
            None,
            f"não encontradas ({type(exc).__name__}); use `gcloud auth application-default login` "
            "ou a conta de serviço autorizada",
        )


def _verificacoes_reais(s: Configuracoes) -> bool:
    from simio.provedores.vertex import ProvedorVertex

    if not s.vertex_configurado():
        return _verificar("Vertex live", False, "configure SIMIO_PROJETO_GCP e SIMIO_MODELO_ANALISE")
    prov = ProvedorVertex(s)

    async def executar() -> tuple[str, int, list[str]]:
        texto, _ = await prov.gerar_texto(
            modelo=s.modelo_analise or "",
            sistema="Responda apenas OK.",
            partes=["teste"],
            esquema_json=None,
            esquema_pydantic=None,
        )
        g = await prov.busca_grounding(
            modelo=s.modelo_descoberta_efetivo or "", prompt="site do Banco Central do Brasil", dominios_excluidos=[]
        )
        return texto.strip()[:40], len(g.fontes), g.consultas_busca_web

    try:
        texto, n, consultas = asyncio.run(executar())
        _verificar("Vertex generate_content", True, f"resposta: {texto!r}")
        return _verificar("Google Search Grounding", n > 0 or None, f"{n} fontes; consultas: {consultas}")
    except Exception as exc:
        return _verificar("Vertex live", False, f"{type(exc).__name__}: {str(exc)[:300]}")


# --------------------------------------------------------------------------------- test
@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def testar(
    ctx: typer.Context,
    real: Annotated[
        bool, typer.Option("--real", help="Inclui testes reais (Vertex/Grounding) — só em ambiente autorizado")
    ] = False,
) -> None:
    """Executa a suíte de testes automatizados (pytest). Argumentos extras vão para o pytest."""
    raiz = Path(__file__).resolve().parents[2]
    testes = raiz / "tests"
    if not testes.is_dir():
        typer.secho("Pasta tests/ não encontrada (instale a partir do repositório clonado).", fg="red")
        raise typer.Exit(2)
    env = dict(os.environ)
    if real:
        env["SIMIO_TESTES_REAIS"] = "1"
    cmd = [sys.executable, "-m", "pytest", str(testes), *ctx.args]
    raise typer.Exit(subprocess.call(cmd, cwd=raiz, env=env))  # noqa: S603


if __name__ == "__main__":  # pragma: no cover
    app()
