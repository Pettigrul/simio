"""Optional keyword-search connectors and URL import adapters.

Every connector is optional: a missing API key makes it *unavailable*, never fatal.
Connectors only return URLs present in the provider response (nothing is synthesised).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from simio.nucleo.modelos import HipoteseBusca, RegistroConsulta, ResultadoDescoberta
from simio.nucleo.urls import extrair_urls

log = logging.getLogger(__name__)
_REDIRECIONAMENTO_GOOGLE_RE = re.compile(r"(?i)^https?://(?:www\.)?google\.[a-z.]+/(?:goto|url)\?")
# Serper's ``displayedLink`` ("https://www.loja.com.br › financiamento"): the destination host of an
# opaque result, without the path.
_HOST_EXIBIDO_RE = re.compile(r"^(https?)://([a-z0-9.-]+\.[a-z]{2,})(?=$|[\s›/])", re.IGNORECASE)
# Hosts whose home page says nothing about the matched post/profile: not recovered.
_HOSTS_IRRECUPERAVEIS = (
    "instagram.com", "facebook.com", "tiktok.com", "youtube.com", "x.com", "twitter.com", "linkedin.com",
    "kwai.com", "threads.net", "pinterest.com", "t.me", "telegram.me", "google.com", "reddit.com",
)


# Search operators that make Google hide results behind opaque links (measured: 6-9 of 10 results).
_OPERADOR_RE = re.compile(r"(?i)(?:^|[\s(])-?(?:site|inurl|intitle|intext|allintitle|allinurl):|\sOR\s|[()]")
_TERMO_CONSULTA_RE = re.compile(r'[()]|-?[A-Za-z]+:"[^"]*"|-?"[^"]*"|[^\s()]+')
# site: targets rewritten as a plain word (Google still ranks the platform's pages first).
_PALAVRAS_SITE = {
    "instagram.com": "instagram", "facebook.com": "facebook", "tiktok.com": "tiktok", "t.me": "telegram",
    "youtube.com": "youtube", "linkedin.com": "linkedin", "kwai.com": "kwai", "x.com": "twitter",
    "threads.net": "threads", "olx.com.br": "olx", "mercadolivre.com.br": "mercado livre",
}


def _termo_simples(token: str) -> str | None:
    """One query token without operators; None drops it (``-site:`` exclusions are applied client-side)."""
    minuscula = token.lower()
    if minuscula.startswith("-") and ":" in minuscula.split('"', 1)[0]:
        return None
    op, sep, valor = token.partition(":")
    if sep and op.lower() in ("site", "inurl", "intitle", "intext", "allintitle", "allinurl") and '"' not in op:
        valor = valor.strip()
        if op.lower() == "site":
            dom = valor.lower().removeprefix("www.").strip("/")
            return _PALAVRAS_SITE.get(dom, dom)
        return valor
    return token


def variantes_simples(consulta: str, limite: int = 3) -> tuple[list[str], list[str]]:
    """Operator-free versions of a dork, plus the domains it excluded with ``-site:``.

    ``OR`` groups are expanded round-robin (variant *i* takes the *i*-th alternative of every group), so
    ``limite`` variants cover the alternatives instead of AND-ing them all. ``site:instagram.com`` becomes
    the word ``instagram``, ``intitle:"X"`` becomes ``"X"``."""
    excluidos = [
        m.group(1).lower().removeprefix("www.")
        for m in re.finditer(r"(?i)(?:^|\s)-site:([^\s()]+)", consulta)
    ]
    tokens = _TERMO_CONSULTA_RE.findall(consulta)
    vagas: list[list[str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "(":
            profundidade, j, grupo = 1, i + 1, []
            while j < len(tokens) and profundidade:
                profundidade += {"(": 1, ")": -1}.get(tokens[j], 0)
                if profundidade:
                    grupo.append(tokens[j])
                j += 1
            alts, cur = [], []
            for t in grupo:
                if t == "OR":
                    alts.append(" ".join(cur))
                    cur = []
                elif t not in ("(", ")"):
                    cur.append(t)
            alts.append(" ".join(cur))
            vagas.append([a for a in alts if a])
            i = j
            continue
        if tok == "OR" and vagas and i + 1 < len(tokens) and tokens[i + 1] not in ("(", ")"):
            vagas[-1].append(tokens[i + 1])
            i += 2
            continue
        if tok not in (")", "OR"):
            vagas.append([tok])
        i += 1
    vagas = [s for s in vagas if s]
    if not vagas:
        return [], excluidos
    n = max(1, min(limite, max(len(s) for s in vagas)))
    saida: list[str] = []
    for v in range(n):
        palavras = []
        for vaga in vagas:
            alt = vaga[v % len(vaga)]
            palavras.extend(w for w in (_termo_simples(t) for t in _TERMO_CONSULTA_RE.findall(alt)) if w)
        q = " ".join(dict.fromkeys(palavras))
        if q and q not in saida and q != consulta:
            saida.append(q)
    return saida, excluidos


class ErroConector(RuntimeError):
    pass


class Conector(Protocol):
    nome: str

    def disponivel(self) -> tuple[bool, str]: ...

    async def executar(
        self, hipotese: HipoteseBusca, consulta: str
    ) -> tuple[list[ResultadoDescoberta], RegistroConsulta]: ...


def _cliente(timeout: float) -> httpx.AsyncClient:
    # trust_env=True honours corporate HTTP(S)_PROXY / SSL_CERT_FILE settings.
    return httpx.AsyncClient(
        timeout=timeout,
        trust_env=True,
        follow_redirects=False,
        headers={"User-Agent": "simio/0.1"},
    )


class _ConectorPalavrasChave:
    nome = "base"
    endpoint = ""

    def __init__(
        self, chave_api: str | None, timeout: float = 20.0, transporte: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.chave_api = chave_api
        self.timeout = timeout
        self.transporte = transporte

    def disponivel(self) -> tuple[bool, str]:
        if not self.chave_api:
            return False, f"{self.nome}: API key not configured (optional)"
        return True, f"{self.nome}: configured"

    def _http(self) -> httpx.AsyncClient:
        if self.transporte is not None:
            return httpx.AsyncClient(timeout=self.timeout, transport=self.transporte)
        return _cliente(self.timeout)

    async def _requisitar(self, consulta: str) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _interpretar(self, dados: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        raise NotImplementedError  # pragma: no cover

    async def executar(
        self, hipotese: HipoteseBusca, consulta: str
    ) -> tuple[list[ResultadoDescoberta], RegistroConsulta]:
        try:
            dados = await self._requisitar(consulta)
            linhas = self._interpretar(dados)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            msg = f"{type(exc).__name__}: {exc}"[:500]
            return [], RegistroConsulta(
                fonte=self.nome, consulta=consulta, id_hipotese=hipotese.id, status="ERRO", erro=msg
            )
        resultados = [
            ResultadoDescoberta(
                fonte=self.nome,
                url_original=url,
                consulta=consulta,
                id_hipotese=hipotese.id,
                titulo=titulo,
                trecho=trecho,
                posicao=i + 1,
                metadados=meta,
            )
            for i, (url, titulo, trecho, meta) in enumerate(linhas)
            if url and url.startswith(("http://", "https://"))
        ]
        return resultados, RegistroConsulta(
            fonte=self.nome,
            consulta=consulta,
            id_hipotese=hipotese.id,
            status="OK" if resultados else "VAZIA",
            qtd_resultados=len(resultados),
        )


class BuscaBrave(_ConectorPalavrasChave):
    nome = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    async def _requisitar(self, consulta: str) -> Any:
        async with self._http() as c:
            r = await c.get(
                self.endpoint,
                params={"q": consulta, "count": 20, "country": "BR", "search_lang": "pt-br"},
                headers={"X-Subscription-Token": self.chave_api or "", "Accept": "application/json"},
            )
            r.raise_for_status()
            return r.json()

    def _interpretar(self, dados: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        retornos = (dados.get("web") or {}).get("results") or []
        return [(r.get("url"), r.get("title"), r.get("description"), {"age": r.get("age")}) for r in retornos]


class BuscaSerper(_ConectorPalavrasChave):
    """Google results via serper.dev, dorks executed literally (site:, inurl:, intitle:, OR, -site:).

    Pages of 10 results (1 credit each; free accounts reject ``num`` > 10). Stops early when a page
    is not full, so short result lists do not spend extra credits.

    Google hides most results of operator queries behind opaque links. When that happens, up to
    ``variantes_simples`` operator-free versions of the dork run as well (see ``variantes_simples``)."""

    nome = "serper"
    endpoint = "https://google.serper.dev/search"

    def __init__(
        self,
        chave_api: str | None,
        timeout: float = 20.0,
        transporte: httpx.AsyncBaseTransport | None = None,
        paginas: int = 1,
        variantes_simples: int = 3,
    ) -> None:
        super().__init__(chave_api, timeout, transporte)
        self.paginas = max(1, paginas)
        self.variantes_simples = max(0, variantes_simples)

    async def _requisitar(self, consulta: str) -> Any:
        organicos: list[Any] = []
        async with self._http() as c:
            for pagina in range(1, self.paginas + 1):
                r = await c.post(
                    self.endpoint,
                    json={"q": consulta, "gl": "br", "hl": "pt-br", "num": 10, "page": pagina},
                    headers={"X-API-KEY": self.chave_api or "", "Content-Type": "application/json"},
                )
                if r.status_code >= 400:
                    try:
                        detalhe = r.json().get("message") or r.text[:200]
                    except ValueError:
                        detalhe = r.text[:200]
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}: {detalhe}", request=r.request, response=r)
                linhas = r.json().get("organic") or []
                for linha in linhas:
                    linha.setdefault("position", len(organicos) + 1)
                    organicos.append(linha)
                if len(linhas) < 10:
                    break
        return {"organic": organicos}

    def _interpretar(self, dados: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        return [
            (
                r.get("link"),
                r.get("title"),
                r.get("snippet"),
                {"position": r.get("position"), "date": r.get("date"), "link_exibido": r.get("displayedLink")},
            )
            for r in dados.get("organic") or []
        ]

    async def executar(
        self, hipotese: HipoteseBusca, consulta: str
    ) -> tuple[list[ResultadoDescoberta], RegistroConsulta]:
        """Google often returns opaque ``google.com/goto?url=...`` links that only resolve through an
        anti-automation interstitial. They are never followed. When the result names its destination
        host (``displayedLink``), the *home page* of that host becomes the candidate instead (marked
        ``recovered_from_google_redirect``; the matched page itself stays unknown). The rest is kept in
        the query record (title/snippet) for manual follow-up."""
        resultados, registro = await super().executar(hipotese, consulta)
        fracao_opacos = sum(bool(_REDIRECIONAMENTO_GOOGLE_RE.match(h.url_original)) for h in resultados) / max(
            1, len(resultados)
        )
        if self.variantes_simples and registro.status != "ERRO" and _OPERADOR_RE.search(consulta) and (
            fracao_opacos >= 0.3 or not resultados
        ):
            variantes, excluidos = variantes_simples(consulta, self.variantes_simples)
            extra: list[ResultadoDescoberta] = []
            for v in variantes:
                mais, _ = await super().executar(hipotese, v)
                extra.extend(
                    h.model_copy(update={"metadados": {**h.metadados, "variante_simples_de": consulta}})
                    for h in mais
                    if not any(_host_em(h.url_original, d) for d in excluidos)
                )
            registro.metadados["variantes_simples"] = variantes
            if registro.status == "VAZIA" and extra:
                registro.status = "OK"
            resultados = resultados + extra
        mantidos: list[ResultadoDescoberta] = []
        opacos: list[dict[str, str]] = []
        recuperados = 0
        vistos: set[str] = set()
        for h in resultados:
            if not _REDIRECIONAMENTO_GOOGLE_RE.match(h.url_original):
                if h.url_original not in vistos:
                    vistos.add(h.url_original)
                    mantidos.append(h)
                continue
            exibido = str(h.metadados.get("link_exibido") or "")
            m = _HOST_EXIBIDO_RE.match(exibido.strip())
            host = m.group(2).lower() if m else ""
            if host and not any(host == d or host.endswith("." + d) for d in _HOSTS_IRRECUPERAVEIS):
                url = f"{m.group(1).lower()}://{host}/"  # type: ignore[union-attr]
                if url not in vistos:
                    vistos.add(url)
                    recuperados += 1
                    mantidos.append(
                        h.model_copy(
                            update={
                                "url_original": url,
                                "metadados": {
                                    **h.metadados,
                                    "recuperado_de_redirecionamento_google": True,
                                    "pagina_correspondente": exibido[:200],
                                },
                            }
                        )
                    )
                continue
            opacos.append({"title": h.titulo or "", "snippet": (h.trecho or "")[:300], "displayed": exibido[:120]})
        if opacos:
            registro.metadados["redirecionamentos_google_nao_resolvidos"] = opacos[:50]
        if recuperados:
            registro.metadados["hosts_redirecionamento_google_recuperados"] = recuperados
        registro.qtd_resultados = len(mantidos)
        if not mantidos and registro.status == "OK":
            registro.status = "VAZIA"
        return mantidos, registro


def _host_em(url: str, dominio: str) -> bool:
    host = (httpx.URL(url).host or "").lower()
    return host == dominio or host.endswith("." + dominio)


class BuscaLens(_ConectorPalavrasChave):
    """Reverse image search (Google Lens via serper.dev): pages showing images visually similar to a
    public copy of the brand's *legacy* logo. Each query is an image URL (3 credits per search).

    It finds pages that never write the brand name in text. Results are only leads: every page goes
    through the normal pipeline, where the deterministic logo comparison confirms (or not) the logo."""

    nome = "lens"
    endpoint = "https://google.serper.dev/lens"

    def __init__(
        self,
        chave_api: str | None,
        urls_imagens: list[str],
        timeout: float = 60.0,
        transporte: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(chave_api, max(timeout, 60.0), transporte)
        self.urls_imagens = list(urls_imagens)

    def disponivel(self) -> tuple[bool, str]:
        if not self.chave_api:
            return False, "lens: Serper API key not configured (optional)"
        if not self.urls_imagens:
            return False, "lens: no legacy_logo_search_urls in the brand profile"
        return True, f"lens: reverse image search of {len(self.urls_imagens)} legacy-logo image(s)"

    def consultas(self) -> list[str]:
        return list(self.urls_imagens)

    async def _requisitar(self, consulta: str) -> Any:
        async with self._http() as c:
            r = await c.post(
                self.endpoint,
                json={"url": consulta, "gl": "br", "hl": "pt-br"},
                headers={"X-API-KEY": self.chave_api or "", "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                try:
                    detalhe = r.json().get("message") or r.text[:200]
                except ValueError:
                    detalhe = r.text[:200]
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}: {detalhe}", request=r.request, response=r)
            return r.json()

    def _interpretar(self, dados: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        return [
            (
                r.get("link"),
                r.get("title"),
                f"Imagem visualmente semelhante ao logotipo antigo (fonte: {r.get('source') or '?'})",
                {
                    "correspondencia_visual_logo_antiga": True,
                    "site_fonte": r.get("source"),
                    "url_imagem": r.get("imageUrl"),
                },
            )
            for r in (dados.get("organic") or dados.get("visual_matches") or [])
        ]


class BuscaUrlscan(_ConectorPalavrasChave):
    """urlscan.io search API (Elasticsearch query-string syntax)."""

    nome = "urlscan"
    endpoint = "https://urlscan.io/api/v1/search/"

    async def _requisitar(self, consulta: str) -> Any:
        async with self._http() as c:
            r = await c.get(
                self.endpoint, params={"q": consulta, "size": 100}, headers={"API-Key": self.chave_api or ""}
            )
            r.raise_for_status()
            return r.json()

    def _interpretar(self, dados: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        saida: list[tuple[str, str | None, str | None, dict[str, Any]]] = []
        for r in dados.get("results") or []:
            pagina = r.get("page") or {}
            tarefa = r.get("task") or {}
            saida.append(
                (
                    str(pagina.get("url") or ""),
                    pagina.get("title"),
                    None,
                    {
                        "momento_varredura": tarefa.get("time"),
                        "domain": pagina.get("domain"),
                        "result": r.get("result"),
                    },
                )
            )
        return saida


class BuscaCrtSh:
    """Certificate Transparency (crt.sh) search connector.
    
    Zero-configuration OSINT connector querying public certificate logs for lookalike / combosquatting domains.
    """

    nome = "crtsh"
    endpoint = "https://crt.sh/"

    def __init__(self, timeout: float = 20.0, transporte: httpx.AsyncBaseTransport | None = None) -> None:
        self.timeout = timeout
        self.transporte = transporte

    def disponivel(self) -> tuple[bool, str]:
        return True, "crtsh: active (public Certificate Transparency log)"

    def _http(self) -> httpx.AsyncClient:
        return _cliente(self.timeout)

    async def executar(
        self, hipotese: HipoteseBusca, consulta: str
    ) -> tuple[list[ResultadoDescoberta], RegistroConsulta]:
        resultados: list[ResultadoDescoberta] = []
        # Query crt.sh expecting JSON array: [{name_value: "...", ...}]
        q_limpa = re.sub(r"[^a-zA-Z0-9.\-_%]", "", consulta.strip()).replace(" ", "%")
        if not q_limpa.startswith("%") and not q_limpa.endswith("%"):
            q_limpa = f"%{q_limpa}%"
        async with self._http() as c:
            try:
                r = await c.get(self.endpoint, params={"q": q_limpa, "output": "json"})
                r.raise_for_status()
                dados = r.json()
            except Exception as e:
                log.warning("crt.sh query '%s' failed: %s", consulta, e)
                return resultados, RegistroConsulta(
                    fonte=self.nome,
                    id_hipotese=hipotese.id,
                    consulta=consulta,
                    status="ERRO",
                    qtd_resultados=0,
                    erro=str(e),
                )

        vistos: set[str] = set()
        for item in dados or []:
            valor_nome = item.get("name_value") or item.get("common_name") or ""
            for dominio in valor_nome.split("\n"):
                dominio = dominio.strip().lower()
                if dominio.startswith("*."):
                    dominio = dominio[2:]
                if not dominio or dominio in vistos or " " in dominio:
                    continue
                vistos.add(dominio)
                url = f"https://{dominio}"
                resultados.append(
                    ResultadoDescoberta(
                        fonte=f"{self.nome}:{hipotese.id}",
                        url_original=url,
                        posicao=len(resultados) + 1,
                        titulo=f"Certificado CT: {dominio}",
                        trecho=f"Issuer: {item.get('issuer_name')}",
                        metadados={"id_entrada": item.get("id"), "registrado_em": item.get("entry_timestamp")},
                    )
                )
                if len(resultados) >= 50:
                    break
            if len(resultados) >= 50:
                break

        return resultados, RegistroConsulta(
            fonte=self.nome,
            id_hipotese=hipotese.id,
            consulta=consulta,
            status="OK" if resultados else "VAZIA",
            qtd_resultados=len(resultados),
        )


# --------------------------------------------------------------------------------------
# Imports
# --------------------------------------------------------------------------------------
def importar_texto(texto: str, fonte: str = "importacao:texto") -> list[ResultadoDescoberta]:
    return [ResultadoDescoberta(fonte=fonte, url_original=u, posicao=i + 1) for i, u in enumerate(extrair_urls(texto))]


def importar_arquivo(caminho: Path) -> list[ResultadoDescoberta]:
    """Import URLs from .txt (any text), .csv (any column containing URLs) or .json."""
    bruto = caminho.read_text(encoding="utf-8", errors="replace")
    fonte = f"importacao:arquivo:{caminho.name}"
    if caminho.suffix.lower() == ".csv":
        urls: list[str] = []
        for linha in csv.reader(io.StringIO(bruto)):
            for celula in linha:
                urls.extend(extrair_urls(celula))
        unicas = list(dict.fromkeys(urls))
        return [ResultadoDescoberta(fonte=fonte, url_original=u, posicao=i + 1) for i, u in enumerate(unicas)]
    return importar_texto(bruto, fonte=fonte)
