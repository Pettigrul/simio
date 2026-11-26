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
from simio.config import DISCOVERY_SOURCES, Settings, load_settings
from simio.logging_setup import setup_logging

app = typer.Typer(
    name="simio",
    help="SIMIO — investigação de identidade digital e uso potencialmente indevido de marcas.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)

OutputOpt = Annotated[
    Path | None, typer.Option("--output-dir", "-o", help="Diretório local de saída (DB, evidências, relatórios)")
]
CampaignOpt = Annotated[str | None, typer.Option("--campaign", "-c", help="ID da campanha (padrão: a mais recente)")]


def _settings(**overrides: object) -> Settings:
    try:
        return load_settings(**overrides)
    except Exception as exc:  # pydantic ValidationError, bad env values
        typer.secho(f"Configuração inválida: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc


def _storage(settings: Settings):  # type: ignore[no-untyped-def]
    from simio.core.storage import Storage

    return Storage(settings.db_path)


def _resolve_campaign(storage, campaign: str | None) -> str:  # type: ignore[no-untyped-def]
    cid = campaign or storage.latest_campaign_id()
    if not cid or storage.get_campaign(cid) is None:
        typer.secho("Nenhuma campanha encontrada. Execute `simio hunt` primeiro.", fg="red", err=True)
        raise typer.Exit(1)
    return cid


def _enrich(s: Settings, storage, campaign_id: str, refresh: bool = False) -> None:  # type: ignore[no-untyped-def]
    """Domain intelligence (RDAP/DNS/hosting) of the flagged pages; never fails the command."""
    from simio.agents.domain_intel import enrich_campaign

    if not s.domain_intel or s.offline:
        return
    try:
        intel = asyncio.run(enrich_campaign(s, storage, campaign_id, refresh=refresh))
        typer.echo(f"Inteligência de domínio: {len(intel)} host(s) (RDAP, DNS, hospedagem)")
    except Exception as exc:  # network trouble must not cost the report
        typer.secho(f"Inteligência de domínio indisponível: {exc}", fg="yellow", err=True)


def _print_summary(summary, report=None) -> None:  # type: ignore[no-untyped-def]
    typer.secho(f"\nCampanha {summary.campaign_id}: {summary.status}", bold=True)
    typer.echo("Estados: " + json.dumps(summary.counts, ensure_ascii=False))
    typer.echo(f"LLM: {summary.llm_requests} requisições, {summary.llm_tokens} tokens")
    for d in summary.diagnostics:
        typer.echo(f"  · {d}")
    if report is not None:
        typer.secho(f"Relatório: {report.workbook}", fg="green")
        if getattr(report, "html_report", None):
            typer.secho(f"Relatório HTML: {report.html_report}", fg="green")
        if getattr(report, "pdf_report", None):
            typer.secho(f"Relatório PDF: {report.pdf_report}", fg="green")
        if getattr(report, "threat_report", None):
            typer.secho(f"Relatório de ameaças: {report.threat_report}", fg="green")
        typer.echo(f"Dossiês: {len(report.dossiers)} em {report.report_dir / 'dossiers'}")


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", help="Mostra a versão")] = False,
) -> None:
    if version:
        typer.echo(f"simio {__version__}")
        raise typer.Exit()


# --------------------------------------------------------------------------------- hunt
@app.command()
def hunt(
    brand: Annotated[str, typer.Option(help="Perfil de marca (ex.: bv)")] = "bv",
    limit: Annotated[int, typer.Option(min=1, help="Máximo de candidatos analisados")] = 50,
    workers: Annotated[int | None, typer.Option(min=1, max=16, help="Candidatos processados em paralelo")] = None,
    model: Annotated[str | None, typer.Option(help="Modelo de análise (sobrepõe SIMIO_ANALYSIS_MODEL)")] = None,
    review_model: Annotated[str | None, typer.Option(help="Modelo do revisor")] = None,
    max_queries: Annotated[int | None, typer.Option(min=0, help="Orçamento de consultas de descoberta")] = None,
    max_llm_requests: Annotated[int | None, typer.Option(min=0, help="Orçamento de requisições LLM")] = None,
    max_tokens: Annotated[int | None, typer.Option(min=0, help="Orçamento de tokens LLM")] = None,
    sources: Annotated[
        str | None,
        typer.Option(
            help=f"Fontes de descoberta, separadas por vírgula ({','.join(DISCOVERY_SOURCES)}). "
            "Padrão: todas; --focus logo-antiga: serper,lens; --focus ameacas: grounding,serper,typosquat,crtsh"
        ),
    ] = None,
    focus: Annotated[
        str | None,
        typer.Option(
            help="Campanha focada em uma única questão. 'logo-antiga': só a identidade visual descontinuada "
            "(busca própria; só páginas com indício da logo vão aos modelos). 'ameacas': inteligência de ameaças "
            "sem o contexto de COBANs (logins e portais falsos, coleta de senha/cartão, apps, hospedagem gratuita)"
        ),
    ] = None,
    urls_file: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Importar URLs de arquivo (.txt/.csv)")
    ] = None,
    urls: Annotated[str | None, typer.Option(help="Importar URLs de texto livre")] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline", help="Sem chamadas a Vertex AI/APIs de busca; analisa só URLs importadas com heurística"
        ),
    ] = False,
    review_all: Annotated[bool, typer.Option("--review-all", help="Revisor avalia todos os candidatos")] = False,
    no_report: Annotated[bool, typer.Option("--no-report", help="Não gerar relatórios ao final")] = False,
    output_dir: OutputOpt = None,
) -> None:
    """Descobre, coleta, analisa, revisa e reporta candidatos (investigação completa)."""
    from simio.core.orchestrator import HuntOptions, Orchestrator
    from simio.providers.search import import_file, import_text
    from simio.reporting import generate_reports

    s = _settings(
        workers=workers,
        analysis_model=model,
        review_model=review_model,
        max_search_queries=max_queries,
        max_llm_requests=max_llm_requests,
        max_llm_tokens=max_tokens,
        output_dir=output_dir,
        offline=offline or None,
    )
    setup_logging(s.log_level, s.output_dir / "logs")
    if s.unsafe_allow_hosts:
        typer.secho(
            f"ATENÇÃO: SIMIO_UNSAFE_ALLOW_HOSTS={s.unsafe_allow_hosts} desativa a proteção SSRF para esses "
            "hosts (uso exclusivo em testes). Registrado na campanha.",
            fg="red",
            err=True,
        )
    from simio.agents.discovery import FOCUS_DEFAULT_SOURCES, FOCUS_MODES

    if focus is not None and focus not in FOCUS_MODES:
        typer.secho(f"Foco desconhecido: {focus} (opções: {', '.join(FOCUS_MODES)})", fg="red", err=True)
        raise typer.Exit(2)
    if sources is None:
        sources = ",".join(FOCUS_DEFAULT_SOURCES[focus] if focus else DISCOVERY_SOURCES)
    src = [x.strip() for x in sources.split(",") if x.strip()]
    unknown = [x for x in src if x not in DISCOVERY_SOURCES]
    if unknown:
        typer.secho(f"Fontes desconhecidas: {unknown}", fg="red", err=True)
        raise typer.Exit(2)
    imported = []
    if urls_file:
        imported += import_file(urls_file)
    if urls:
        imported += import_text(urls)
    if s.offline and not imported:
        typer.secho("Modo offline requer URLs importadas (--urls-file ou --urls).", fg="red", err=True)
        raise typer.Exit(2)
    storage = _storage(s)
    try:
        orch = Orchestrator(s, storage)
        summary = asyncio.run(
            orch.hunt(
                HuntOptions(
                    brand=brand, limit=limit, sources=src, imported=imported, review_all=review_all, focus=focus
                )
            )
        )
        if not no_report:
            _enrich(s, storage, summary.campaign_id)
        report = None if no_report else generate_reports(s, storage, summary.campaign_id)
        _print_summary(summary, report)
        if summary.counts == {} and not imported:
            raise typer.Exit(3)
    finally:
        storage.close()


@app.command()
def resume(
    campaign: CampaignOpt = None,
    retry_failed: Annotated[bool, typer.Option("--retry-failed", help="Recoloca candidatos FAILED na fila")] = False,
    max_llm_requests: Annotated[int | None, typer.Option(min=0)] = None,
    max_tokens: Annotated[int | None, typer.Option(min=0)] = None,
    workers: Annotated[int | None, typer.Option(min=1, max=16)] = None,
    offline: Annotated[bool, typer.Option("--offline")] = False,
    no_report: Annotated[bool, typer.Option("--no-report")] = False,
    output_dir: OutputOpt = None,
) -> None:
    """Retoma uma campanha interrompida (falha, Ctrl+C ou orçamento esgotado)."""
    from simio.core.orchestrator import CampaignBusy, Orchestrator
    from simio.reporting import generate_reports

    s = _settings(output_dir=output_dir)
    # The campaign's original parameters (model, offline mode, budgets) are kept; only flags given
    # explicitly here override them.
    overrides = {
        "max_llm_requests": max_llm_requests,
        "max_llm_tokens": max_tokens,
        "workers": workers,
        "offline": True if offline else None,
    }
    setup_logging(s.log_level, s.output_dir / "logs")
    storage = _storage(s)
    try:
        cid = _resolve_campaign(storage, campaign)
        try:
            summary = asyncio.run(Orchestrator(s, storage).resume(cid, retry_failed=retry_failed, overrides=overrides))
        except CampaignBusy as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(4) from exc
        if not no_report:
            _enrich(s, storage, cid)
        report = None if no_report else generate_reports(s, storage, cid)
        _print_summary(summary, report)
    finally:
        storage.close()


@app.command()
def enrich(
    campaign: CampaignOpt = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Consulta de novo os hosts já levantados")] = False,
    output_dir: OutputOpt = None,
) -> None:
    """Levanta registro (RDAP/WHOIS), DNS e hospedagem dos achados e gera o relatório de novo."""
    from simio.reporting import generate_reports

    s = _settings(output_dir=output_dir)
    storage = _storage(s)
    try:
        cid = _resolve_campaign(storage, campaign)
        _enrich(s, storage, cid, refresh=refresh)
        res = generate_reports(s, storage, cid)
        typer.secho(f"Relatório: {res.workbook}", fg="green")
    finally:
        storage.close()


@app.command()
def report(
    campaign: CampaignOpt = None,
    all_dossiers: Annotated[bool, typer.Option("--all-dossiers", help="Gerar dossiê para todos os candidatos")] = False,
    output_dir: OutputOpt = None,
) -> None:
    """Gera o Excel consolidado e os dossiês HTML a partir do banco local."""
    from simio.reporting import generate_reports

    s = _settings(output_dir=output_dir)
    storage = _storage(s)
    try:
        cid = _resolve_campaign(storage, campaign)
        res = generate_reports(s, storage, cid, all_dossiers=all_dossiers)
        typer.secho(f"Relatório: {res.workbook}", fg="green")
        if res.html_report:
            typer.secho(f"Relatório HTML: {res.html_report}", fg="green")
        if res.pdf_report:
            typer.secho(f"Relatório PDF: {res.pdf_report}", fg="green")
        if res.threat_report:
            typer.secho(f"Relatório de ameaças: {res.threat_report}", fg="green")
        typer.echo(f"Dossiês: {len(res.dossiers)} em {res.report_dir / 'dossiers'}")
    finally:
        storage.close()


@app.command()
def status(
    campaign: CampaignOpt = None,
    list_all: Annotated[bool, typer.Option("--list", help="Lista campanhas")] = False,
    output_dir: OutputOpt = None,
) -> None:
    """Mostra o estado de uma campanha (ou lista campanhas)."""
    s = _settings(output_dir=output_dir)
    if not s.db_path.exists():
        typer.echo("Nenhum banco encontrado em " + str(s.db_path))
        raise typer.Exit(1)
    storage = _storage(s)
    try:
        if list_all:
            for c in storage.list_campaigns(50):
                typer.echo(f"{c['id']}  {c['brand']:<6} {c['status']:<14} {c['created_at']}")
            return
        cid = _resolve_campaign(storage, campaign)
        camp = storage.get_campaign(cid) or {}
        req, tok = storage.usage_totals(cid)
        typer.secho(f"Campanha {cid} ({camp.get('brand')}) — {camp.get('status')}", bold=True)
        typer.echo("Estados: " + json.dumps(storage.status_counts(cid), ensure_ascii=False))
        typer.echo(
            f"LLM: {req} requisições / {tok} tokens "
            f"(orçamento {camp.get('params', {}).get('max_llm_requests')} / "
            f"{camp.get('params', {}).get('max_llm_tokens')})"
        )
        disc = storage.get_checkpoint(cid, "discovery", {}) or {}
        typer.echo(f"Descoberta: {disc.get('unique_urls', 0)} URLs únicas, fontes {disc.get('sources_used', [])}")
        typer.echo(f"Erros registrados: {len(storage.errors(cid))}")
    finally:
        storage.close()


# --------------------------------------------------------------------------------- doctor
def _check(label: str, ok: bool | None, detail: str = "") -> bool:
    mark = {
        True: typer.style("OK  ", fg="green"),
        False: typer.style("FAIL", fg="red"),
        None: typer.style("WARN", fg="yellow"),
    }[ok]
    typer.echo(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok is not False


@app.command()
def doctor(
    live: Annotated[
        bool, typer.Option("--live", help="Executa chamadas reais mínimas a Vertex AI e Grounding")
    ] = False,
    output_dir: OutputOpt = None,
) -> None:
    """Diagnostica ambiente, dependências, navegador, configuração e integrações."""
    s = _settings(output_dir=output_dir)
    ok = True
    ok &= _check("Python", sys.version_info >= (3, 11), f"{platform.python_version()} ({sys.executable})")
    for mod in ("google.genai", "playwright", "pydantic", "openpyxl", "jinja2", "httpx", "tldextract"):
        try:
            m = importlib.import_module(mod)
            ok &= _check(f"módulo {mod}", True, getattr(m, "__version__", ""))
        except ImportError as exc:
            ok &= _check(f"módulo {mod}", False, str(exc))
    try:
        s.output_dir.mkdir(parents=True, exist_ok=True)
        probe = s.output_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        _check("diretório de saída gravável", True, str(s.output_dir.resolve()))
    except OSError as exc:
        ok &= _check("diretório de saída gravável", False, str(exc))
    try:
        st = _storage(s)
        _check("SQLite", True, f"schema v{st.schema_version()} em {s.db_path}")
        st.close()
    except Exception as exc:
        ok &= _check("SQLite", False, str(exc))
    ok &= _check("navegador Playwright (Chromium)", *_browser_check(s))
    try:
        from simio.brands import load_brand

        b = load_brand("bv", s.brand_file)
        _check("perfil de marca", True, f"{b.display_name}; domínios oficiais: {b.official_domains}")
        if not b.institution_cnpjs:
            _check("CNPJs da instituição no perfil", None, "não preenchidos (VALIDATE em bv.toml)")
    except Exception as exc:
        ok &= _check("perfil de marca", False, str(exc))
    _check("modo offline", None if s.offline else True, "ativado" if s.offline else "desativado")
    _check("Vertex: projeto", True if s.gcp_project else None, s.gcp_project or "SIMIO_GCP_PROJECT não definido")
    _check("Vertex: localização", True, s.gcp_location)
    _check(
        "Vertex: modelo de análise",
        True if s.analysis_model else None,
        s.analysis_model or "SIMIO_ANALYSIS_MODEL não definido (análise heurística será usada)",
    )
    _check("Vertex: modelo de revisão", True, s.effective_review_model or "(heurístico)")
    _check("Vertex: modelo de grounding", True, s.effective_discovery_model or "(indisponível)")
    _check(*_adc_check())
    for name in ("brave", "serper", "urlscan"):
        key = getattr(s, f"{name}_api_key")
        _check(f"conector {name}", True if key else None, "configurado" if key else "não configurado (opcional)")
    try:
        from simio.agents.visual import load_reference
        from simio.brands import load_brand

        bv = load_brand("bv", s.brand_file)
        refs = [load_reference(p) for p in bv.legacy_logo_paths()]
        _check(
            "referência da logo antiga",
            True if refs else None,
            f"{len(refs)} imagem(ns); busca reversa (lens): {len(bv.legacy_logo_search_urls)} URL(s)"
            if refs
            else "não configurada (detecção por comparação de imagem desativada)",
        )
    except (OSError, ValueError) as exc:
        _check("referência da logo antiga", False, str(exc)[:200])
    if s.coban_registry_path:
        _check("cadastro de correspondentes", s.coban_registry_path.is_file(), str(s.coban_registry_path))
    else:
        _check("cadastro de correspondentes", None, "não configurado: CONFIRMED_COBAN indisponível")
    if s.unsafe_allow_hosts:
        _check(
            "política de rede", None, f"SIMIO_UNSAFE_ALLOW_HOSTS ativo ({s.unsafe_allow_hosts}) — somente para testes!"
        )
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(var):
            _check(f"variável {var}", True, "definida")
    if s.browser_proxy:
        _check("proxy do navegador", True, "definido (verificação pós-conexão de IP desativada)")
    if live:
        ok &= _live_checks(s)
    if not ok:
        raise typer.Exit(1)


def _browser_check(s: Settings) -> tuple[bool, str]:
    """Launch Chromium exactly as the collector does (sandbox setting included)."""

    async def run() -> str:
        from playwright.async_api import async_playwright

        from simio.collectors.browser import launch_chromium

        async with async_playwright() as p:
            b = await launch_chromium(p, sandbox=s.browser_sandbox)
            v = b.version
            await b.close()
            return v

    sandbox = "sandbox ativo" if s.browser_sandbox else "SANDBOX DESATIVADO (SIMIO_BROWSER_SANDBOX=false)"
    try:
        return True, f"Chromium {asyncio.run(run())}; {sandbox}"
    except Exception as exc:
        hint = (
            "se o erro citar sandbox/namespaces, consulte docs/deployment.md (SIMIO_BROWSER_SANDBOX)"
            if s.browser_sandbox
            else "execute `python -m playwright install chromium`"
        )
        return False, f"{str(exc).splitlines()[0][:200]} — {hint}; ou `python -m playwright install chromium`"


def _adc_check() -> tuple[str, bool | None, str]:
    try:
        import google.auth

        creds, project = google.auth.default()
        return "credenciais ADC", True, f"{type(creds).__name__} (projeto ADC: {project or '-'})"
    except Exception as exc:
        return (
            "credenciais ADC",
            None,
            f"não encontradas ({type(exc).__name__}); use `gcloud auth application-default login` "
            "ou a conta de serviço autorizada",
        )


def _live_checks(s: Settings) -> bool:
    from simio.providers.vertex import VertexProvider

    if not s.vertex_configured():
        return _check("Vertex live", False, "configure SIMIO_GCP_PROJECT e SIMIO_ANALYSIS_MODEL")
    prov = VertexProvider(s)

    async def run() -> tuple[str, int, list[str]]:
        text, _ = await prov.generate_text(
            model=s.analysis_model or "",
            system="Responda apenas OK.",
            parts=["teste"],
            json_schema=None,
            pydantic_schema=None,
        )
        g = await prov.grounded_search(
            model=s.effective_discovery_model or "", prompt="site do Banco Central do Brasil", exclude_domains=[]
        )
        return text.strip()[:40], len(g.sources), g.web_search_queries

    try:
        text, n, queries = asyncio.run(run())
        _check("Vertex generate_content", True, f"resposta: {text!r}")
        return _check("Google Search Grounding", n > 0 or None, f"{n} fontes; consultas: {queries}")
    except Exception as exc:
        return _check("Vertex live", False, f"{type(exc).__name__}: {str(exc)[:300]}")


# --------------------------------------------------------------------------------- test
@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def test(
    ctx: typer.Context,
    live: Annotated[
        bool, typer.Option("--live", help="Inclui testes reais (Vertex/Grounding) — só em ambiente autorizado")
    ] = False,
) -> None:
    """Executa a suíte de testes automatizados (pytest). Argumentos extras vão para o pytest."""
    root = Path(__file__).resolve().parents[2]
    tests = root / "tests"
    if not tests.is_dir():
        typer.secho("Pasta tests/ não encontrada (instale a partir do repositório clonado).", fg="red")
        raise typer.Exit(2)
    env = dict(os.environ)
    if live:
        env["SIMIO_LIVE_TESTS"] = "1"
    cmd = [sys.executable, "-m", "pytest", str(tests), *ctx.args]
    raise typer.Exit(subprocess.call(cmd, cwd=root, env=env))  # noqa: S603


if __name__ == "__main__":  # pragma: no cover
    app()
