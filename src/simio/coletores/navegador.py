from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from simio.configuracoes import Configuracoes
from simio.nucleo import caminhos
from simio.nucleo.caminhos import ARTEFATOS
from simio.nucleo.modelos import Artefato, DadosPagina, Evidencia, StatusColeta
from simio.nucleo.urls import hostname_de
from simio.seguranca.politica_url import PoliticaUrl, ViolacaoPolitica
from simio.seguranca.proxy_saida import ProxySaida

log = logging.getLogger(__name__)

VERSAO_COLETOR = "browser-collector/1"

ARGS_CHROMIUM = [
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--no-first-run",
    "--no-default-browser-check",
    "--metrics-recording-only",
    "--disable-features=InterestFeedContentSuggestions,MediaRouter,OptimizationHints",
    "--deny-permission-prompts",
    # Todo o tráfego tem que passar pelo proxy de saída: sem QUIC, sem UDP do WebRTC fora do
    # proxy, e loopback não fica isento implicitamente.
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--proxy-bypass-list=<-loopback>",
]
# Proxy global fictício: cada contexto sobrescreve com o seu proxy de saída. Se algum contexto
# ficar sem, o tráfego falha em vez de sair direto.
PROXY_POR_CONTEXTO: Any = {"server": "http://per-context.invalid:1", "bypass": "<-loopback>"}

MARCADORES_DESAFIO = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "verify you are human",
    "verifique se você é humano",
    "just a moment",
    "checking your browser",
    "attention required",
    "cf-challenge",
)

MARCADORES_BLOQUEIO_CORPORATIVO = (
    "site bloqueado para seu perfil",
    "bloqueado devido as políticas de acesso",
    "bloqueado devido às políticas de acesso",
    "regra de bloqueio:",
)

_POST_IG_RE = re.compile(
    r"(?i)^https?://(?:www\.|m\.)?instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"
)
_PERFIL_IG_RE = re.compile(r"(?i)^https?://(?:www\.|m\.)?instagram\.com/([A-Za-z0-9._]+)/?(?:\?.*)?$")
_RESERVADOS_IG = frozenset(
    {"p", "reel", "reels", "tv", "stories", "explore", "popular", "accounts", "direct", "about", "legal", "tags"}
)
_VIDEO_FB_RE = re.compile(r"(?i)^https?://(?:www\.|m\.|web\.)?facebook\.com/(?:[^/?#]+/videos/|reel/|watch/?\?v=)")
_POST_FB_RE = re.compile(
    r"(?i)^https?://(?:www\.|m\.|web\.)?facebook\.com/(?:[^/?#]+/posts/|[^/?#]+/photos/|photo\.php\?|photo/?\?"
    r"|permalink\.php\?|story\.php\?)"
)
# Raiz de página/perfil ("facebook.com/lojax/" ou "profile.php?id=..."): o Page Plugin público mostra
# nome, avatar, bio, contatos e os últimos posts; a página normal cai na tela de login.
_PAGINA_FB_RE = re.compile(
    r"(?i)^https?://(?:www\.|m\.|web\.|pt-br\.)?facebook\.com/(?:(profile\.php)\?(?:[^#]*&)?id=(\d+)|([A-Za-z0-9.\-]+)/?(?:[?#].*)?$)"
)
_RESERVADOS_FB = frozenset(
    {"groups", "watch", "events", "marketplace", "share", "login", "help", "policies", "pages", "gaming", "reel",
     "hashtag", "search", "public", "people", "plugins", "dialog", "sharer", "home.php", "photo.php", "photo"}
)


def _url_incorporavel_social(url: str) -> str:
    # Posts, reels e perfis públicos do Instagram/Facebook viram a URL de embed oficial (sem login),
    # que é o único jeito de renderizar o criativo, o @ e a legenda sem cair no muro de login.
    m = _POST_IG_RE.match(url)
    if m:
        return f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/"
    m = _PERFIL_IG_RE.match(url)
    if m and m.group(1).lower() not in _RESERVADOS_IG:
        return f"https://www.instagram.com/{m.group(1)}/embed/"
    if _VIDEO_FB_RE.match(url):
        return f"https://www.facebook.com/plugins/video.php?href={quote(url, safe='')}&show_text=true&width=500"
    if _POST_FB_RE.match(url):
        return f"https://www.facebook.com/plugins/post.php?href={quote(url, safe='')}&show_text=true&width=500"
    m = _PAGINA_FB_RE.match(url)
    if m and (m.group(2) or (m.group(3) or "").lower() not in _RESERVADOS_FB):
        href = (
            f"https://www.facebook.com/profile.php?id={m.group(2)}"
            if m.group(2)
            else f"https://www.facebook.com/{m.group(3)}/"
        )
        return (
            f"https://www.facebook.com/plugins/page.php?href={quote(href, safe='')}"
            "&tabs=timeline&width=500&height=1400&small_header=false&hide_cover=false"
        )
    return url

JS_EXTRACAO = r"""
(maxChars) => {
  const vh = window.innerHeight;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const isVisible = el => {
    if (!el) return false;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const loc = el => el.closest('header,[role=banner]') ? 'header'
    : el.closest('footer,[role=contentinfo]') ? 'footer'
    : el.closest('nav,[role=navigation]') ? 'nav' : 'page';
  const meta = {};
  document.querySelectorAll('meta[name],meta[property]').forEach(m => {
    const k = (m.getAttribute('name') || m.getAttribute('property') || '').toLowerCase();
    const v = m.getAttribute('content');
    if (k && v && Object.keys(meta).length < 60) meta[k] = v.slice(0, 500);
  });
  const canonical = (document.querySelector('link[rel=canonical]') || {}).href || null;
  const headings = [...document.querySelectorAll('h1,h2,h3')].filter(isVisible)
    .map(h => clean(h.innerText)).filter(Boolean).slice(0, 40);
  const allLinks = [...document.querySelectorAll('a[href]')].slice(0, 1500).map(a => ({
    texto: clean(a.innerText || a.getAttribute('aria-label') || a.title || '').slice(0, 200),
    href: (a.href || '').slice(0, 1000), local: loc(a), visible: isVisible(a)
  }));
  const strip = l => ({texto: l.texto, href: l.href, local: l.local});
  const nav = allLinks.filter(l => l.visible && (l.local === 'nav' || l.local === 'header'))
    .slice(0, 60).map(strip);
  const contactRe = /^(tel:|mailto:|sms:)|wa\.me|whatsapp|api\.whatsapp|t\.me\/|instagram\.com|facebook\.com|m\.me\//i;
  const contacts = allLinks.filter(l => contactRe.test(l.href)).slice(0, 60).map(strip);
  const ctaEls = [...document.querySelectorAll(
    'button, a[role=button], input[type=submit], input[type=button], a[class*=btn], a[class*=button], a[class*=cta]'
  )].filter(isVisible).slice(0, 80);
  const ctas = ctaEls.map(e => ({
    texto: clean(e.innerText || e.value || e.getAttribute('aria-label') || '').slice(0, 200),
    href: (e.href || '').slice(0, 1000), local: loc(e)
  })).filter(c => c.texto);
  const images = [...document.querySelectorAll('img, svg[aria-label], [role=img]')]
    .filter(isVisible).slice(0, 200).map(img => {
      const r = img.getBoundingClientRect();
      return {
        alt: clean(img.alt || img.getAttribute('aria-label') || img.title || '').slice(0, 200),
        src: (img.currentSrc || img.src || '').slice(0, 500), local: loc(img),
        acima_dobra: r.top < vh && r.bottom > 0, largura: Math.round(r.width), altura: Math.round(r.height),
        pagina_x: Math.round(r.left + window.scrollX), pagina_y: Math.round(r.top + window.scrollY)
      };
    });
  const sectionText = sel => [...document.querySelectorAll(sel)].filter(isVisible)
    .map(e => clean(e.innerText)).filter(Boolean).join(' | ').slice(0, 4000);
  const fold = [];
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode()) && fold.length < 1500) {
    const t = clean(n.textContent);
    if (!t) continue;
    const p = n.parentElement;
    if (!p || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(p.tagName) || !isVisible(p)) continue;
    const r = p.getBoundingClientRect();
    if (r.top < vh && r.bottom > 0) fold.push(t);
  }
  let pageTitle = clean(document.title);
  let visText = ((document.body ? document.body.innerText : '') || '');
  if (pageTitle === 'Instagram' || location.hostname.includes('instagram.com')) {
    const userEl = document.querySelector('.UsernameText, .Header .Username, a.Username');
    const capEl = document.querySelector('.Caption');
    const pathMatch = location.pathname.match(/^\/([A-Za-z0-9._]+)\/embed\/?$/i);
    const u = userEl ? clean(userEl.innerText) : (pathMatch ? pathMatch[1] : '');
    const c = capEl ? clean(capEl.innerText) : '';
    if (u || c) pageTitle = `Instagram ${u ? '@' + u : ''}${c ? ' - ' + c : ''}`.slice(0, 500);
    const imgAlts = images.map(i => i.alt).filter(a => a && a.length > 15).join('\n');
    if (imgAlts) visText = visText + '\n' + imgAlts;
  }
  return {
    titulo: pageTitle.slice(0, 500),
    meta, canonica: canonical,
    idioma_pagina: document.documentElement.lang || null,
    titulos_secao: headings, itens_nav: nav, ctas, links_contato: contacts,
    links: allLinks.filter(l => l.visible).slice(0, 400).map(strip),
    imagens: images,
    texto_cabecalho: sectionText('header,[role=banner]'),
    texto_rodape: sectionText('footer,[role=contentinfo]'),
    texto_acima_dobra: fold.join(' ').slice(0, 8000),
    texto_visivel: visText.slice(0, maxChars),
    formularios_presentes: document.forms.length,
    // campos que a página pede (nunca preenchidos): senha/CPF/cartão/token indicam captura de credenciais
    campos_entrada: [...document.querySelectorAll('input, select, textarea')]
      .filter(e => !['hidden', 'submit', 'button', 'reset', 'image'].includes((e.type || '').toLowerCase()))
      .filter(e => isVisible(e) || (e.type || '').toLowerCase() === 'password')
      .slice(0, 60)
      .map(e => ({
        tipo_campo: (e.type || e.tagName || '').toLowerCase().slice(0, 30),
        nome: clean(e.name || '').slice(0, 80), id: clean(e.id || '').slice(0, 80),
        placeholder: clean(e.placeholder || '').slice(0, 120),
        autocomplete: clean(e.getAttribute('autocomplete') || '').slice(0, 60),
        rotulo: clean((e.labels && e.labels[0] ? e.labels[0].innerText : '')
          || e.getAttribute('aria-label') || '').slice(0, 120)
      })),
    acoes_formulario: [...document.forms].map(f => (f.action || '').slice(0, 300)).filter(Boolean).slice(0, 20)
  };
}
"""


def sha256_arquivo(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as fh:
        for fragmento in iter(lambda: fh.read(65536), b""):
            h.update(fragmento)
    return h.hexdigest()


@dataclass
class _GuardaPagina:
    politica: PoliticaUrl
    max_requisicoes: int
    bloqueadas: list[str] = field(default_factory=list)
    contagem: int = 0
    documentos: list[Any] = field(default_factory=list)

    async def ao_rotear(self, rota: Any) -> None:
        req = rota.request
        url = req.url
        if url.startswith(("data:", "blob:", "about:")):
            await rota.continue_()
            return
        self.contagem += 1
        if self.contagem > self.max_requisicoes:
            self.bloqueadas.append(f"{url[:200]} -> limite de requisições atingido")
            await rota.abort("blockedbyclient")
            return
        if req.resource_type == "media":
            await rota.abort("blockedbyclient")
            return
        try:
            await self.politica.verificar_async(url)
        except ViolacaoPolitica as exc:
            self.bloqueadas.append(f"{url[:200]} -> {exc}")
            await rota.abort("blockedbyclient")
            return
        await rota.continue_()

    async def ao_abrir_websocket(self, ws: Any) -> None:
        try:
            await self.politica.verificar_async(ws.url.replace("wss://", "https://", 1).replace("ws://", "http://", 1))
        except ViolacaoPolitica as exc:
            self.bloqueadas.append(f"{ws.url[:200]} -> {exc}")
            await ws.close()
            return
        ws.connect_to_server()

    def ao_responder(self, resposta: Any) -> None:
        if resposta.request.resource_type == "document":
            self.documentos.append(resposta)


async def iniciar_chromium(pw: Any, *, sandbox: bool) -> Any:
    iniciar: dict[str, Any] = {
        "headless": True,
        "args": ARGS_CHROMIUM,
        "proxy": PROXY_POR_CONTEXTO,
        # O Playwright desliga o sandbox do Chromium por padrão. Aqui fica ligado, a não ser que o
        # ambiente não consiga subir com ele (risco documentado em docs/security.md).
        "chromium_sandbox": sandbox,
    }
    try:
        return await pw.chromium.launch(**iniciar)
    except Exception as exc:
        # Sem o headless-shell instalado, cai para o Chromium completo.
        # TODO: detectar pela exceção/caminho do executável em vez da mensagem do Playwright.
        if "Executable doesn't exist" in str(exc):
            return await pw.chromium.launch(**iniciar, channel="chromium")
        raise


class ColetorNavegador:
    # Um Chromium por coletor e um contexto novo (com proxy de saída próprio) por página.

    def __init__(self, configuracoes: Configuracoes, politica: PoliticaUrl) -> None:
        self.configuracoes = configuracoes
        self.politica = politica
        self._pw: Any = None
        self._navegador: Any = None

    async def __aenter__(self) -> ColetorNavegador:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        await self._iniciar()
        return self

    async def _iniciar(self) -> None:
        self._navegador = await iniciar_chromium(self._pw, sandbox=self.configuracoes.sandbox_navegador)

    @property
    def conectado(self) -> bool:
        return self._navegador is not None and self._navegador.is_connected()

    async def garantir_navegador(self) -> None:
        # Se o Chromium caiu (OOM, por exemplo), sobe de novo para não derrubar as páginas seguintes.
        if not self.conectado and self._pw is not None:
            log.warning("navegador desconectado; reiniciando")
            await self._iniciar()

    async def __aexit__(self, *exc: object) -> None:
        if self._navegador is not None:
            await self._navegador.close()
        if self._pw is not None:
            await self._pw.stop()

    async def coletar(self, id_candidata: str, url: str, tentativa: int, dir_campanha: Path) -> Evidencia:
        dir_rel = caminhos.dir_tentativa(hostname_de(url), id_candidata, tentativa)
        dir_destino = dir_campanha / dir_rel
        if dir_destino.exists():
            # Sobra de tentativa interrompida, sem registro de evidência: não pode entrar no hash
            # nem ir para o modelo como se fosse desta tentativa.
            shutil.rmtree(dir_destino)
        dir_destino.mkdir(parents=True, exist_ok=True)
        await self.garantir_navegador()
        ev = Evidencia(
            id_candidata=id_candidata, tentativa=tentativa, status=StatusColeta.NAO_COLETADA, url_original=url
        )
        try:
            await self.politica.verificar_async(url)
        except ViolacaoPolitica as exc:
            ev.status = StatusColeta.BLOQUEADA
            ev.erros.append(f"bloqueada pela política de URL: {exc}")
            ev.nao_inspecionado.append("página não acessada (bloqueada pela política de segurança)")
            return self._finalizar(ev, dir_destino, dir_rel, dir_campanha)
        try:
            await asyncio.wait_for(
                self._coletar_pagina(ev, url, dir_destino, dir_rel), timeout=self.configuracoes.orcamento_pagina_s
            )
        except TimeoutError:
            ev.erros.append(f"tempo limite da página ({self.configuracoes.orcamento_pagina_s}s) excedido")
            ev.nao_inspecionado.append("coleta interrompida por tempo: parte do conteúdo/artefatos pode faltar")
            ev.repetivel = True
            if ev.status == StatusColeta.NAO_COLETADA:
                ev.status = StatusColeta.FALHOU
            elif ev.status == StatusColeta.OK:
                ev.status = StatusColeta.PARCIAL
        except Exception as exc:  # erros do Playwright (alvo caiu, contexto destruído...)
            if not self.conectado:
                raise  # o navegador morreu: falha de infraestrutura, não da página
            ev.erros.append(f"erro na coleta: {type(exc).__name__}: {str(exc)[:300]}")
            ev.repetivel = True
            ev.pagina = None
            ev.status = StatusColeta.FALHOU
        return self._finalizar(ev, dir_destino, dir_rel, dir_campanha)

    async def _coletar_pagina(self, ev: Evidencia, url: str, dir_destino: Path, dir_rel: PurePosixPath) -> None:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        s = self.configuracoes
        kwargs_ctx: dict[str, Any] = {
            "viewport": {"width": s.largura_viewport, "height": s.altura_viewport},
            "accept_downloads": False,
            "service_workers": "block",
            "java_script_enabled": True,
            "ignore_https_errors": s.ignorar_erros_https,
            "locale": "pt-BR",
            "timezone_id": "America/Sao_Paulo",
            "permissions": [],
        }
        if s.user_agent_navegador:
            kwargs_ctx["user_agent"] = s.user_agent_navegador
        proxy = ProxySaida(self.politica, upstream=s.proxy_navegador, fixar_upstream=s.proxy_navegador_fixar_ip)
        await proxy.__aenter__()
        kwargs_ctx["proxy"] = {"server": proxy.url, "bypass": "<-loopback>"}
        guarda = _GuardaPagina(self.politica, s.max_requisicoes_por_pagina)
        try:
            contexto = await self._navegador.new_context(**kwargs_ctx)
        except BaseException:
            await proxy.__aexit__(None, None, None)
            raise
        try:
            await contexto.route("**/*", guarda.ao_rotear)
            await contexto.route_web_socket("**/*", guarda.ao_abrir_websocket)
            pagina = await contexto.new_page()
            contexto.on("page", lambda p: asyncio.ensure_future(p.close()))  # popups
            pagina.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
            pagina.on("response", guarda.ao_responder)
            pagina.set_default_timeout(s.timeout_navegacao_ms)

            url_busca = _url_incorporavel_social(url)
            try:
                resposta = await pagina.goto(url_busca, wait_until="domcontentloaded", timeout=s.timeout_navegacao_ms)
            except PlaywrightTimeout:
                ev.status = StatusColeta.FALHOU
                ev.repetivel = True
                ev.erros.append("timeout de navegação")
                ev.nao_inspecionado.append("conteúdo da página não inspecionado (timeout de navegação)")
                return
            except PlaywrightError as exc:
                msg = str(exc).splitlines()[0][:300]
                ev.status = StatusColeta.BLOQUEADA if "ERR_BLOCKED_BY_CLIENT" in msg else StatusColeta.FALHOU
                ev.repetivel = any(k in msg for k in ("ERR_CONNECTION", "ERR_TIMED_OUT", "ERR_NETWORK"))
                ev.erros.append(f"erro de navegação: {msg}")
                ev.nao_inspecionado.append("conteúdo da página não inspecionado (falha de acesso)")
                return

            cadeia: list[str] = []
            if resposta is not None:
                req = resposta.request
                while req.redirected_from is not None:
                    req = req.redirected_from
                    cadeia.append(req.url)
                ev.cadeia_redirecionamentos = list(reversed(cadeia))
                ev.status_http = resposta.status
            try:
                await pagina.wait_for_load_state("networkidle", timeout=max(1000, s.espera_estabilizar_ms * 2))
            except PlaywrightTimeout:
                ev.erros.append("a rede não ficou ociosa (coleta seguiu)")
            if s.espera_estabilizar_ms:
                await pagina.wait_for_timeout(s.espera_estabilizar_ms)
            ev.url_final = url if url_busca != url and pagina.url == url_busca else pagina.url

            # IP validado (fixado) a que o proxy de saída conectou; vai para a cadeia de custódia.
            ev.ip_servidor = proxy.ip_de(hostname_de(ev.url_final))
            try:
                self.politica.verificar_estatico(ev.url_final)
            except ViolacaoPolitica as exc:
                ev.status = StatusColeta.BLOQUEADA
                ev.erros.append(f"URL final viola a política: {exc}")
                return

            ev.status = StatusColeta.OK
            bruto = await pagina.evaluate(JS_EXTRACAO, s.max_caracteres_texto)  # já trunca na página (memória)
            bruto["texto_visivel"] = (bruto.get("texto_visivel") or "")[: s.max_caracteres_texto]
            ev.pagina = DadosPagina.model_validate(bruto)
            reduzido = (ev.pagina.titulo + " " + ev.pagina.texto_visivel[:5000]).lower()
            if any(m in reduzido for m in MARCADORES_BLOQUEIO_CORPORATIVO):
                ev.status = StatusColeta.BLOQUEADA
                ev.repetivel = False
                ev.erros.append(
                    f"HTTP {ev.status_http or 403}: acesso bloqueado pelo filtro web corporativo da rede"
                )
                ev.nao_inspecionado.append(
                    "conteúdo real do site não inspecionado (acesso bloqueado pela política de rede corporativa)"
                )
            elif ev.status_http and ev.status_http >= 400:
                ev.status = StatusColeta.FALHOU
                ev.erros.append(f"HTTP {ev.status_http}: o conteúdo é uma página de erro")
            if any(m in reduzido for m in MARCADORES_DESAFIO):
                # "CAPTCHA" precisa continuar no texto: o revisor procura por ele em ev.erros.
                ev.erros.append("possível desafio anti-bot/CAPTCHA detectado; nenhuma tentativa de contorno")
                ev.nao_inspecionado.append("conteúdo real possivelmente oculto por desafio anti-bot")
                if ev.status == StatusColeta.OK:
                    ev.status = StatusColeta.PARCIAL

            await self._gravar(
                dir_destino / ARTEFATOS["captura_viewport"], await pagina.screenshot(type="png", animations="disabled")
            )
            try:
                altura = int(
                    await pagina.evaluate(
                        "() => Math.max(document.body ? document.body.scrollHeight : 0,"
                        " document.documentElement.scrollHeight)"
                    )
                )
                # Sempre com clip: página que cresce durante a captura não passa do limite.
                kwargs_captura: dict[str, Any] = {
                    "type": "png",
                    "full_page": True,
                    "animations": "disabled",
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": s.largura_viewport,
                        "height": max(1, min(altura, s.max_altura_pagina_inteira)),
                    },
                }
                if altura > s.max_altura_pagina_inteira:
                    ev.nao_inspecionado.append(
                        f"screenshot completo limitado a {s.max_altura_pagina_inteira}px "
                        f"(página com {altura}px); texto integral preservado em {ARTEFATOS['texto']}"
                    )
                captura_completa = await pagina.screenshot(**kwargs_captura)
                await self._gravar(dir_destino / ARTEFATOS["captura_completa"], captura_completa)
            except PlaywrightError as exc:
                ev.erros.append(f"falha no screenshot da página completa: {str(exc)[:200]}")
                ev.nao_inspecionado.append("screenshot da página completa indisponível")
                if ev.status == StatusColeta.OK:
                    ev.status = StatusColeta.PARCIAL
            total, cabeca = await pagina.evaluate(
                "(n) => { const d = document.documentElement; const h = d ? d.outerHTML : '';"
                " return [h.length, h.slice(0, n)]; }",
                s.max_bytes_html,
            )
            html = str(cabeca).encode("utf-8", errors="replace")
            if int(total) > s.max_bytes_html or len(html) > s.max_bytes_html:
                ev.erros.append(f"HTML truncado em {s.max_bytes_html} bytes")
                html = html[: s.max_bytes_html]
            # O HTML bruto é gravado como .html.txt (ver ARTEFATOS) para nunca ser renderizado sem querer.
            await self._gravar(dir_destino / ARTEFATOS["html"], html)
            await self._gravar(dir_destino / ARTEFATOS["texto"], ev.pagina.texto_visivel.encode("utf-8"))
            await self._gravar(
                dir_destino / ARTEFATOS["dados_pagina"], ev.pagina.model_dump_json(indent=2).encode("utf-8")
            )
            ev.nao_inspecionado.append(
                "apenas a URL descoberta foi inspecionada; páginas internas, "
                "formulários e fluxos após cliques não foram acessados"
            )
        finally:
            ev.requisicoes_bloqueadas = list(dict.fromkeys(guarda.bloqueadas + proxy.bloqueadas))[:200]
            await contexto.close()
            await proxy.__aexit__(None, None, None)

    @staticmethod
    async def _gravar(caminho: Path, dados: bytes) -> None:
        await asyncio.to_thread(caminho.write_bytes, dados)

    def _finalizar(
        self, ev: Evidencia, dir_destino: Path, dir_rel: PurePosixPath, dir_campanha: Path
    ) -> Evidencia:
        for tipo, nome in ARTEFATOS.items():
            p = dir_destino / nome
            if p.exists():
                ev.artefatos.append(
                    Artefato(
                        tipo=tipo, caminho=str(dir_rel / nome), sha256=sha256_arquivo(p), tamanho_bytes=p.stat().st_size
                    )
                )
        manifesto = {
            "coletor": VERSAO_COLETOR,
            "id_candidata": ev.id_candidata,
            "tentativa": ev.tentativa,
            "status": ev.status.value,
            "url_original": ev.url_original,
            "url_final": ev.url_final,
            "cadeia_redirecionamentos": ev.cadeia_redirecionamentos,
            "status_http": ev.status_http,
            "ip_servidor": ev.ip_servidor,
            "coletada_em": ev.coletada_em.isoformat(),
            "manifesto_gravado_em": datetime.now(UTC).isoformat(),
            "artefatos": [a.model_dump() for a in ev.artefatos],
            "requisicoes_bloqueadas": ev.requisicoes_bloqueadas,
            "erros": ev.erros,
            "nao_inspecionado": ev.nao_inspecionado,
            "algoritmo_hash": "SHA-256",
        }
        arq_manifesto = dir_destino / caminhos.ARQUIVO_MANIFESTO
        arq_manifesto.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")
        ev.caminho_manifesto = str(dir_rel / caminhos.ARQUIVO_MANIFESTO)
        ev.sha256_manifesto = sha256_arquivo(arq_manifesto)
        return ev
