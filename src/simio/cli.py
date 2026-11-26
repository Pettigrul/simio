from __future__ import annotations

import asyncio
import importlib
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from typer import rich_utils
from typer.core import TyperCommand, TyperGroup

from simio import __version__
from simio.config_logs import configurar_logging
from simio.configuracoes import FONTES_DESCOBERTA, Configuracoes, carregar_configuracoes
from simio.nucleo import caminhos

# O Typer não tem tradução própria: os textos da ajuda em Rich são constantes deste módulo.
# TODO: as mensagens de erro do parser do Click ("No such option", "Missing option") continuam em inglês.
rich_utils.OPTIONS_PANEL_TITLE = "Opções"
rich_utils.COMMANDS_PANEL_TITLE = "Comandos"
rich_utils.ARGUMENTS_PANEL_TITLE = "Argumentos"
rich_utils.ERRORS_PANEL_TITLE = "Erro"
rich_utils.ABORTED_TEXT = "Interrompido."
rich_utils.DEFAULT_STRING = "[padrão: {}]"
rich_utils.REQUIRED_LONG_STRING = "[obrigatório]"
rich_utils.ENVVAR_STRING = "[variável: {}]"
rich_utils.DEPRECATED_STRING = "(obsoleto) "
rich_utils.RICH_HELP = "Veja [blue]'{command_path} {help_option}'[/] para a ajuda."


class _AjudaEmPortugues:
    def get_help_option(self, ctx: Any) -> Any:
        opcao = super().get_help_option(ctx)  # type: ignore[misc]
        if opcao is not None:
            opcao.help = "Mostra esta ajuda e sai"
        return opcao

    def format_usage(self, ctx: Any, formatter: Any) -> None:
        pedacos = self.collect_usage_pieces(ctx)  # type: ignore[attr-defined]
        formatter.write_usage(ctx.command_path, " ".join(pedacos), prefix="Uso: ")


class _Grupo(_AjudaEmPortugues, TyperGroup):
    pass


class _Comando(_AjudaEmPortugues, TyperCommand):
    pass


app = typer.Typer(
    name="simio",
    help="SIMIO: investigação de identidade digital e uso indevido de marcas.",
    cls=_Grupo,
    options_metavar="[OPÇÕES]",
    subcommand_metavar="COMANDO [ARGS]...",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)


def comando(**kw: Any) -> Any:
    return app.command(cls=_Comando, options_metavar="[OPÇÕES]", **kw)


OpcaoSaida = Annotated[
    Path | None, typer.Option("--dir-saida", "-o", help="Pasta de saída (banco, evidências e relatórios)")
]
OpcaoCampanha = Annotated[str | None, typer.Option("--campanha", "-c", help="ID da campanha (padrão: a mais recente)")]


def _configuracoes(**sobrescritas: object) -> Configuracoes:
    try:
        return carregar_configuracoes(**sobrescritas)
    except Exception as exc:  # ValidationError do pydantic, valor ruim no ambiente
        typer.secho(f"Configuração inválida: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc


def _armazenamento(configuracoes: Configuracoes):  # type: ignore[no-untyped-def]
    from simio.nucleo.armazenamento import Armazenamento

    return Armazenamento(configuracoes.caminho_db)


def _resolver_campanha(armazenamento, campanha: str | None) -> str:  # type: ignore[no-untyped-def]
    cid = armazenamento.resolver_id_campanha(campanha) if campanha else armazenamento.id_campanha_mais_recente()
    if not cid or armazenamento.obter_campanha(cid) is None:
        if campanha:
            typer.secho(f"Campanha não encontrada: {campanha}", fg="red", err=True)
        else:
            typer.secho("Nenhuma campanha no banco. Rode `simio investigar` primeiro.", fg="red", err=True)
        raise typer.Exit(1)
    return cid


def _enriquecer(s: Configuracoes, armazenamento, id_campanha: str, atualizar: bool = False) -> None:  # type: ignore[no-untyped-def]
    from simio.agentes.inteligencia_dominio import enriquecer_campanha

    if not s.inteligencia_dominio or s.offline:
        return
    try:
        intel = asyncio.run(enriquecer_campanha(s, armazenamento, id_campanha, atualizar=atualizar))
        typer.echo(f"Inteligência de domínio: {len(intel)} host(s) consultado(s) (RDAP, DNS, hospedagem)")
    except Exception as exc:  # falha de rede aqui não pode custar o relatório
        typer.secho(f"Inteligência de domínio indisponível: {exc}", fg="yellow", err=True)


def _contagens(contagens: dict[str, int]) -> str:
    return ", ".join(f"{status} {n}" for status, n in sorted(contagens.items())) or "nenhuma candidata"


def _imprimir_relatorios(res) -> None:  # type: ignore[no-untyped-def]
    typer.secho(f"Planilha: {res.planilha}", fg="green")
    if getattr(res, "relatorio_html", None):
        typer.secho(f"Relatório HTML: {res.relatorio_html}", fg="green")
    if getattr(res, "relatorio_pdf", None):
        typer.secho(f"Relatório PDF: {res.relatorio_pdf}", fg="green")
    if getattr(res, "relatorio_ameacas", None):
        typer.secho(f"Relatório de ameaças: {res.relatorio_ameacas}", fg="green")
    typer.echo(f"Dossiês: {len(res.dossies)} em {caminhos.relatorios(res.dir_campanha).dossies}")


def _imprimir_resumo(resumo, relatorio=None) -> None:  # type: ignore[no-untyped-def]
    typer.secho(f"\nCampanha {resumo.id_campanha}: {resumo.status}", bold=True)
    typer.echo(f"Candidatas: {_contagens(resumo.contagens)}")
    typer.echo(f"LLM: {resumo.requisicoes_llm} requisições, {resumo.tokens_llm} tokens")
    for d in resumo.diagnosticos:
        typer.echo(f"  - {d}")
    if relatorio is not None:
        _imprimir_relatorios(relatorio)


@app.callback()
def principal(
    versao: Annotated[bool, typer.Option("--versao", help="Mostra a versão e sai")] = False,
) -> None:
    if versao:
        typer.echo(f"simio {__version__}")
        raise typer.Exit()


@comando()
def investigar(
    marca: Annotated[str, typer.Option(help="Perfil de marca (ex.: bv)")] = "bv",
    limite: Annotated[int, typer.Option(min=1, help="Máximo de candidatas analisadas")] = 50,
    paralelismo: Annotated[int | None, typer.Option(min=1, max=16, help="Candidatas processadas em paralelo")] = None,
    modelo: Annotated[str | None, typer.Option(help="Modelo de análise (sobrepõe SIMIO_MODELO_ANALISE)")] = None,
    modelo_revisao: Annotated[str | None, typer.Option(help="Modelo do revisor")] = None,
    max_consultas: Annotated[int | None, typer.Option(min=0, help="Limite de consultas de descoberta")] = None,
    max_requisicoes_llm: Annotated[int | None, typer.Option(min=0, help="Limite de requisições ao LLM")] = None,
    max_tokens: Annotated[int | None, typer.Option(min=0, help="Limite de tokens do LLM")] = None,
    fontes: Annotated[
        str | None,
        typer.Option(
            help=f"Fontes de descoberta separadas por vírgula ({','.join(FONTES_DESCOBERTA)}). "
            "Padrão: todas; com --foco logo-antiga: serper,lens; com --foco ameacas: grounding,serper,typosquat,crtsh"
        ),
    ] = None,
    foco: Annotated[
        str,
        typer.Option(
            help="Tipo de campanha. 'ampla' (padrão): varredura completa. "
            "'logo-antiga': só a identidade visual descontinuada (busca própria; só páginas com indício da "
            "logo vão aos modelos). 'ameacas': ameaças sem o contexto de COBANs (logins e portais falsos, "
            "coleta de senha ou cartão, apps, hospedagem gratuita)"
        ),
    ] = "ampla",
    arquivo_urls: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Importa URLs de um arquivo (.txt/.csv)")
    ] = None,
    urls: Annotated[str | None, typer.Option(help="Importa URLs de um texto livre")] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline", help="Sem Vertex AI nem APIs de busca: analisa só as URLs importadas, por heurística"
        ),
    ] = False,
    revisar_todas: Annotated[
        bool, typer.Option("--revisar-todas", help="Manda todas as candidatas para o revisor")
    ] = False,
    sem_relatorio: Annotated[bool, typer.Option("--sem-relatorio", help="Não gera os relatórios no final")] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Investigação completa: descobre, coleta, analisa, revisa e gera os relatórios."""
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
    configurar_logging(s.nivel_log, caminhos.arquivo_log(s.dir_saida))
    if s.hosts_liberados_inseguro:
        typer.secho(
            f"ATENÇÃO: SIMIO_HOSTS_LIBERADOS_INSEGURO={s.hosts_liberados_inseguro} desliga a proteção contra SSRF "
            "para esses hosts. Use só em teste; fica registrado na campanha.",
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
    escolhidas = [x.strip() for x in fontes.split(",") if x.strip()]
    desconhecidas = [x for x in escolhidas if x not in FONTES_DESCOBERTA]
    if desconhecidas:
        typer.secho(
            f"Fontes desconhecidas: {', '.join(desconhecidas)} (opções: {', '.join(FONTES_DESCOBERTA)})",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)
    importadas = []
    if arquivo_urls:
        importadas += importar_arquivo(arquivo_urls)
    if urls:
        importadas += importar_texto(urls)
    if s.offline and not importadas:
        typer.secho("O modo offline precisa de URLs importadas (--arquivo-urls ou --urls).", fg="red", err=True)
        raise typer.Exit(2)
    armazenamento = _armazenamento(s)
    try:
        orq = Orquestrador(s, armazenamento)
        resumo = asyncio.run(
            orq.investigar(
                OpcoesInvestigacao(
                    marca=marca,
                    limite=limite,
                    fontes=escolhidas,
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


@comando()
def retomar(
    campanha: OpcaoCampanha = None,
    repetir_falhas: Annotated[
        bool, typer.Option("--repetir-falhas", help="Põe de volta na fila as candidatas que falharam")
    ] = False,
    max_requisicoes_llm: Annotated[int | None, typer.Option(min=0, help="Novo limite de requisições ao LLM")] = None,
    max_tokens: Annotated[int | None, typer.Option(min=0, help="Novo limite de tokens do LLM")] = None,
    paralelismo: Annotated[int | None, typer.Option(min=1, max=16, help="Candidatas em paralelo")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="Continua sem Vertex AI nem APIs de busca")] = False,
    sem_relatorio: Annotated[bool, typer.Option("--sem-relatorio", help="Não gera os relatórios no final")] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Retoma uma campanha interrompida (erro, Ctrl+C ou limite de uso atingido)."""
    from simio.nucleo.orquestrador import CampanhaOcupada, Orquestrador
    from simio.relatorios import gerar_relatorios

    s = _configuracoes(dir_saida=dir_saida)
    # A campanha segue com os parâmetros com que foi criada (modelo, offline, limites); só as
    # opções passadas aqui mudam alguma coisa.
    sobrescritas = {
        "max_requisicoes_llm": max_requisicoes_llm,
        "max_tokens_llm": max_tokens,
        "paralelismo": paralelismo,
        "offline": True if offline else None,
    }
    configurar_logging(s.nivel_log, caminhos.arquivo_log(s.dir_saida))
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


@comando()
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
        typer.secho(f"Planilha: {res.planilha}", fg="green")
    finally:
        armazenamento.fechar()


@comando()
def relatorio(
    campanha: OpcaoCampanha = None,
    todos_dossies: Annotated[
        bool, typer.Option("--todos-dossies", help="Gera dossiê de todas as candidatas, não só das prioritárias")
    ] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Gera a planilha, o relatório HTML/PDF e os dossiês a partir do banco local."""
    from simio.relatorios import gerar_relatorios

    s = _configuracoes(dir_saida=dir_saida)
    armazenamento = _armazenamento(s)
    try:
        cid = _resolver_campanha(armazenamento, campanha)
        _imprimir_relatorios(gerar_relatorios(s, armazenamento, cid, todos_dossies=todos_dossies))
    finally:
        armazenamento.fechar()


@comando()
def status(
    campanha: OpcaoCampanha = None,
    listar_todas: Annotated[bool, typer.Option("--listar", help="Lista as últimas 50 campanhas")] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Mostra a situação de uma campanha, ou lista as campanhas."""
    s = _configuracoes(dir_saida=dir_saida)
    if not s.caminho_db.exists():
        typer.echo(f"Nenhum banco em {s.caminho_db}")
        raise typer.Exit(1)
    armazenamento = _armazenamento(s)
    try:
        if listar_todas:
            for c in armazenamento.listar_campanhas(50):
                typer.echo(f"{c['id']}  {c['marca']:<6} {c['status']:<17} {c['criado_em']}")
            return
        cid = _resolver_campanha(armazenamento, campanha)
        camp = armazenamento.obter_campanha(cid) or {}
        parametros = camp.get("parametros", {})
        req, tok = armazenamento.totais_uso(cid)
        typer.secho(f"Campanha {cid} ({camp.get('marca')}): {camp.get('status')}", bold=True)
        typer.echo(f"Candidatas: {_contagens(armazenamento.contagem_por_status(cid))}")
        typer.echo(
            f"LLM: {req} requisições, {tok} tokens "
            f"(limites: {parametros.get('max_requisicoes_llm')} requisições, {parametros.get('max_tokens_llm')} tokens)"
        )
        disc = armazenamento.obter_checkpoint(cid, "descoberta", {}) or {}
        fontes_usadas = ", ".join(disc.get("fontes_usadas", [])) or "nenhuma"
        typer.echo(f"Descoberta: {disc.get('urls_unicas', 0)} URLs únicas; fontes: {fontes_usadas}")
        typer.echo(f"Erros registrados: {len(armazenamento.erros(cid))}")
    finally:
        armazenamento.fechar()


def _verificar(rotulo: str, ok: bool | None, detalhe: str = "") -> bool:
    selo = {
        True: typer.style("OK   ", fg="green"),
        False: typer.style("FALHA", fg="red"),
        None: typer.style("AVISO", fg="yellow"),
    }[ok]
    typer.echo(f"[{selo}] {rotulo}" + (f": {detalhe}" if detalhe else ""))
    return ok is not False


@comando()
def diagnostico(
    real: Annotated[
        bool, typer.Option("--real", help="Faz uma chamada real mínima ao Vertex AI e ao Grounding")
    ] = False,
    dir_saida: OpcaoSaida = None,
) -> None:
    """Confere ambiente, dependências, navegador, configuração e integrações."""
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
        sonda = s.dir_saida / ".teste-escrita"
        sonda.write_text("ok")
        sonda.unlink()
        _verificar("pasta de saída gravável", True, str(s.dir_saida.resolve()))
    except OSError as exc:
        ok &= _verificar("pasta de saída gravável", False, str(exc))
    try:
        banco = _armazenamento(s)
        _verificar("SQLite", True, f"esquema v{banco.versao_esquema()} em {s.caminho_db}")
        banco.fechar()
    except Exception as exc:
        ok &= _verificar("SQLite", False, str(exc))
    ok &= _verificar("navegador do Playwright (Chromium)", *_verificar_navegador(s))
    try:
        from simio.marcas import carregar_marca

        b = carregar_marca("bv", s.arquivo_marca)
        _verificar("perfil de marca", True, f"{b.nome_exibicao}; domínios oficiais: {', '.join(b.dominios_oficiais)}")
        if not b.cnpjs_instituicao:
            _verificar("CNPJs da instituição no perfil", None, "não preenchidos (marcados com VALIDAR no bv.toml)")
    except Exception as exc:
        ok &= _verificar("perfil de marca", False, str(exc))
    _verificar("modo offline", None if s.offline else True, "ligado" if s.offline else "desligado")
    _verificar("Vertex: projeto", True if s.projeto_gcp else None, s.projeto_gcp or "SIMIO_PROJETO_GCP não definido")
    _verificar("Vertex: região", True, s.local_gcp)
    _verificar(
        "Vertex: modelo de análise",
        True if s.modelo_analise else None,
        s.modelo_analise or "SIMIO_MODELO_ANALISE não definido (a análise será heurística)",
    )
    _verificar("Vertex: modelo de revisão", True, s.modelo_revisao_efetivo or "(heurístico)")
    _verificar("Vertex: modelo de grounding", True, s.modelo_descoberta_efetivo or "(indisponível)")
    _verificar(*_verificar_adc())
    for nome in ("brave", "serper", "urlscan"):
        chave = getattr(s, f"chave_api_{nome}")
        _verificar(f"conector {nome}", True if chave else None, "configurado" if chave else "sem chave (opcional)")
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
            else "não configurada (sem comparação de imagem)",
        )
    except (OSError, ValueError) as exc:
        _verificar("referência da logo antiga", False, str(exc)[:200])
    if s.caminho_cadastro_coban:
        _verificar("cadastro de correspondentes", s.caminho_cadastro_coban.is_file(), str(s.caminho_cadastro_coban))
    else:
        _verificar("cadastro de correspondentes", None, "não configurado; COBAN_CONFIRMADO fica indisponível")
    if s.hosts_liberados_inseguro:
        _verificar(
            "política de rede",
            None,
            f"SIMIO_HOSTS_LIBERADOS_INSEGURO ligado ({', '.join(s.hosts_liberados_inseguro)}); só para teste",
        )
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(var):
            _verificar(f"variável {var}", True, "definida")
    if s.proxy_navegador:
        _verificar("proxy do navegador", True, "definido (sem conferência do IP depois da conexão)")
    if real:
        ok &= _verificacoes_reais(s)
    if not ok:
        raise typer.Exit(1)


def _verificar_navegador(s: Configuracoes) -> tuple[bool, str]:
    # Abre o Chromium do mesmo jeito que o coletor, com a mesma configuração de sandbox.
    async def executar() -> str:
        from playwright.async_api import async_playwright

        from simio.coletores.navegador import iniciar_chromium

        async with async_playwright() as p:
            b = await iniciar_chromium(p, sandbox=s.sandbox_navegador)
            v = b.version
            await b.close()
            return v

    sandbox = "sandbox ligado" if s.sandbox_navegador else "SANDBOX DESLIGADO (SIMIO_SANDBOX_NAVEGADOR=false)"
    try:
        return True, f"Chromium {asyncio.run(executar())}; {sandbox}"
    except Exception as exc:
        instalar = "`python -m playwright install chromium`"
        pista = (
            f"se o erro falar de sandbox/namespaces, veja docs/deployment.md (SIMIO_SANDBOX_NAVEGADOR); "
            f"senão rode {instalar}"
            if s.sandbox_navegador
            else f"rode {instalar}"
        )
        return False, f"{str(exc).splitlines()[0][:200]} ({pista})"


def _verificar_adc() -> tuple[str, bool | None, str]:
    try:
        import google.auth

        creds, projeto = google.auth.default()
        return "credenciais ADC", True, f"{type(creds).__name__} (projeto do ADC: {projeto or '-'})"
    except Exception as exc:
        return (
            "credenciais ADC",
            None,
            f"não encontradas ({type(exc).__name__}); rode `gcloud auth application-default login` "
            "ou use a conta de serviço autorizada",
        )


def _verificacoes_reais(s: Configuracoes) -> bool:
    from simio.provedores.vertex import ProvedorVertex

    if not s.vertex_configurado():
        return _verificar("Vertex (chamada real)", False, "defina SIMIO_PROJETO_GCP e SIMIO_MODELO_ANALISE")
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
        _verificar("Vertex: generate_content", True, f"resposta: {texto!r}")
        return _verificar("Google Search Grounding", n > 0 or None, f"{n} fonte(s); consultas: {consultas}")
    except Exception as exc:
        return _verificar("Vertex (chamada real)", False, f"{type(exc).__name__}: {str(exc)[:300]}")


@comando(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def testar(
    ctx: typer.Context,
    real: Annotated[
        bool, typer.Option("--real", help="Inclui os testes reais (Vertex/Grounding); só em ambiente autorizado")
    ] = False,
) -> None:
    """Roda a suíte de testes (pytest). Argumentos extras são repassados ao pytest."""
    raiz = Path(__file__).resolve().parents[2]
    testes = raiz / "tests"
    if not testes.is_dir():
        typer.secho("Pasta tests/ não encontrada (rode a partir do repositório clonado).", fg="red")
        raise typer.Exit(2)
    env = dict(os.environ)
    if real:
        env["SIMIO_TESTES_REAIS"] = "1"
    cmd = [sys.executable, "-m", "pytest", str(testes), *ctx.args]
    raise typer.Exit(subprocess.call(cmd, cwd=raiz, env=env))  # noqa: S603


if __name__ == "__main__":  # pragma: no cover
    app()
