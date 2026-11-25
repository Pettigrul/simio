"""Secure evidence collection with Playwright (Chromium, headless).

Safety properties (see docs/security.md):
* every browser connection goes through a per-page :class:`EgressProxy` that validates the
  target, resolves DNS itself and connects to the validated IP (covers redirect hops, which
  Playwright ``route()`` does not see, and DNS rebinding);
* ``route()`` interception adds early blocking, a request cap and media blocking;
* no form filling, no clicks, no login, downloads disabled, popups closed, dialogs dismissed,
  service workers blocked, no extra permissions, Chromium sandbox enabled (``SIMIO_BROWSER_SANDBOX``);
* hard per-page time budget and request cap;
* raw HTML is stored as ``.html.txt`` so it is never rendered by accident.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from simio.config import Settings
from simio.core.models import Artifact, CollectionStatus, Evidence, PageData
from simio.core.urls import hostname_of
from simio.security.egress_proxy import EgressProxy
from simio.security.url_policy import PolicyViolation, UrlPolicy

log = logging.getLogger(__name__)

COLLECTOR_VERSION = "browser-collector/1"

CHROMIUM_ARGS = [
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
    # All traffic must traverse the egress proxy: no QUIC, no non-proxied WebRTC UDP, and
    # loopback is NOT implicitly bypassed.
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--proxy-bypass-list=<-loopback>",
]
# Placeholder global proxy: every context overrides it with its own egress proxy. If a context
# ever lacked one, traffic would fail closed instead of going direct.
PER_CONTEXT_PROXY: Any = {"server": "http://per-context.invalid:1", "bypass": "<-loopback>"}

CHALLENGE_MARKERS = (
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

CORPORATE_BLOCK_MARKERS = (
    "site bloqueado para seu perfil",
    "bloqueado devido as políticas de acesso",
    "bloqueado devido às políticas de acesso",
    "regra de bloqueio:",
)

_IG_POST_RE = re.compile(
    r"(?i)^https?://(?:www\.|m\.)?instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"
)
_IG_PROFILE_RE = re.compile(r"(?i)^https?://(?:www\.|m\.)?instagram\.com/([A-Za-z0-9._]+)/?(?:\?.*)?$")
_IG_RESERVED = frozenset(
    {"p", "reel", "reels", "tv", "stories", "explore", "popular", "accounts", "direct", "about", "legal", "tags"}
)
_FB_VIDEO_RE = re.compile(r"(?i)^https?://(?:www\.|m\.|web\.)?facebook\.com/(?:[^/?#]+/videos/|reel/|watch/?\?v=)")
_FB_POST_RE = re.compile(r"(?i)^https?://(?:www\.|m\.|web\.)?facebook\.com/[^/?#]+/posts/")


def _social_embed_url(url: str) -> str:
    """Rewrite public Instagram/Facebook post, reel and profile URLs to their official unauthenticated
    embed endpoints so Chromium can render the visual creative, handle and caption without a login wall."""
    m = _IG_POST_RE.match(url)
    if m:
        return f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/"
    m = _IG_PROFILE_RE.match(url)
    if m and m.group(1).lower() not in _IG_RESERVED:
        return f"https://www.instagram.com/{m.group(1)}/embed/"
    if _FB_VIDEO_RE.match(url):
        return f"https://www.facebook.com/plugins/video.php?href={quote(url, safe='')}&show_text=true&width=500"
    if _FB_POST_RE.match(url):
        return f"https://www.facebook.com/plugins/post.php?href={quote(url, safe='')}&show_text=true&width=500"
    return url

EXTRACT_JS = r"""
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
    text: clean(a.innerText || a.getAttribute('aria-label') || a.title || '').slice(0, 200),
    href: (a.href || '').slice(0, 1000), location: loc(a), visible: isVisible(a)
  }));
  const strip = l => ({text: l.text, href: l.href, location: l.location});
  const nav = allLinks.filter(l => l.visible && (l.location === 'nav' || l.location === 'header'))
    .slice(0, 60).map(strip);
  const contactRe = /^(tel:|mailto:|sms:)|wa\.me|whatsapp|api\.whatsapp|t\.me\/|instagram\.com|facebook\.com|m\.me\//i;
  const contacts = allLinks.filter(l => contactRe.test(l.href)).slice(0, 60).map(strip);
  const ctaEls = [...document.querySelectorAll(
    'button, a[role=button], input[type=submit], input[type=button], a[class*=btn], a[class*=button], a[class*=cta]'
  )].filter(isVisible).slice(0, 80);
  const ctas = ctaEls.map(e => ({
    text: clean(e.innerText || e.value || e.getAttribute('aria-label') || '').slice(0, 200),
    href: (e.href || '').slice(0, 1000), location: loc(e)
  })).filter(c => c.text);
  const images = [...document.querySelectorAll('img, svg[aria-label], [role=img]')]
    .filter(isVisible).slice(0, 200).map(img => {
      const r = img.getBoundingClientRect();
      return {
        alt: clean(img.alt || img.getAttribute('aria-label') || img.title || '').slice(0, 200),
        src: (img.currentSrc || img.src || '').slice(0, 500), location: loc(img),
        above_fold: r.top < vh && r.bottom > 0, width: Math.round(r.width), height: Math.round(r.height)
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
    title: pageTitle.slice(0, 500),
    meta, canonical,
    lang: document.documentElement.lang || null,
    headings, nav_items: nav, ctas, contact_links: contacts,
    links: allLinks.filter(l => l.visible).slice(0, 400).map(strip),
    images,
    header_text: sectionText('header,[role=banner]'),
    footer_text: sectionText('footer,[role=contentinfo]'),
    above_fold_text: fold.join(' ').slice(0, 8000),
    visible_text: visText.slice(0, maxChars),
    forms_present: document.forms.length
  };
}
"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class _PageGuard:
    policy: UrlPolicy
    max_requests: int
    blocked: list[str] = field(default_factory=list)
    count: int = 0
    documents: list[Any] = field(default_factory=list)

    async def on_route(self, route: Any) -> None:
        req = route.request
        url = req.url
        if url.startswith(("data:", "blob:", "about:")):
            await route.continue_()
            return
        self.count += 1
        if self.count > self.max_requests:
            self.blocked.append(f"{url[:200]} -> request cap reached")
            await route.abort("blockedbyclient")
            return
        if req.resource_type == "media":
            await route.abort("blockedbyclient")
            return
        try:
            await self.policy.acheck(url)
        except PolicyViolation as exc:
            self.blocked.append(f"{url[:200]} -> {exc}")
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def on_websocket(self, ws: Any) -> None:
        try:
            await self.policy.acheck(ws.url.replace("wss://", "https://", 1).replace("ws://", "http://", 1))
        except PolicyViolation as exc:
            self.blocked.append(f"{ws.url[:200]} -> {exc}")
            await ws.close()
            return
        ws.connect_to_server()

    def on_response(self, response: Any) -> None:
        if response.request.resource_type == "document":
            self.documents.append(response)


async def launch_chromium(pw: Any, *, sandbox: bool) -> Any:
    """Launch headless Chromium, falling back to full Chromium if headless-shell is absent."""
    launch: dict[str, Any] = {
        "headless": True,
        "args": CHROMIUM_ARGS,
        "proxy": PER_CONTEXT_PROXY,
        # Playwright disables the Chromium sandbox by default; keep it ON unless explicitly
        # disabled for an environment where it cannot start (documented risk).
        "chromium_sandbox": sandbox,
    }
    try:
        return await pw.chromium.launch(**launch)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            return await pw.chromium.launch(**launch, channel="chromium")
        raise


class BrowserCollector:
    """Async context manager owning one Chromium instance; one fresh context per page."""

    def __init__(self, settings: Settings, policy: UrlPolicy) -> None:
        self.settings = settings
        self.policy = policy
        self._pw: Any = None
        self._browser: Any = None

    async def __aenter__(self) -> BrowserCollector:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        await self._launch()
        return self

    async def _launch(self) -> None:
        self._browser = await launch_chromium(self._pw, sandbox=self.settings.browser_sandbox)

    @property
    def connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def ensure_browser(self) -> None:
        """Relaunch Chromium if it crashed (e.g. OOM) so one crash does not fail every later page."""
        if not self.connected and self._pw is not None:
            log.warning("browser disconnected; relaunching")
            await self._launch()

    async def __aexit__(self, *exc: object) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    async def collect(self, candidate_id: str, url: str, attempt: int, campaign_dir: Path) -> Evidence:
        rel_dir = Path("evidence") / candidate_id / f"attempt-{attempt}"
        out_dir = campaign_dir / rel_dir
        if out_dir.exists():
            # Leftovers of an interrupted attempt that never produced an evidence record must not be
            # hashed into (or sent to the model as part of) this attempt.
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        await self.ensure_browser()
        ev = Evidence(
            candidate_id=candidate_id, attempt=attempt, status=CollectionStatus.NOT_COLLECTED, original_url=url
        )
        try:
            await self.policy.acheck(url)
        except PolicyViolation as exc:
            ev.status = CollectionStatus.BLOCKED
            ev.errors.append(f"blocked by URL policy: {exc}")
            ev.not_inspected.append("página não acessada (bloqueada pela política de segurança)")
            return self._finalize(ev, out_dir, rel_dir, campaign_dir)
        try:
            await asyncio.wait_for(self._collect_page(ev, url, out_dir, rel_dir), timeout=self.settings.page_budget_s)
        except TimeoutError:
            ev.errors.append(f"page budget of {self.settings.page_budget_s}s exceeded")
            ev.not_inspected.append("coleta interrompida por tempo: parte do conteúdo/artefatos pode faltar")
            ev.retryable = True
            if ev.status == CollectionStatus.NOT_COLLECTED:
                ev.status = CollectionStatus.FAILED
            elif ev.status == CollectionStatus.OK:
                ev.status = CollectionStatus.PARTIAL
        except Exception as exc:  # Playwright errors (crashed target, destroyed context, ...)
            if not self.connected:
                raise  # browser died: infrastructure failure, not a property of this page
            ev.errors.append(f"collection error: {type(exc).__name__}: {str(exc)[:300]}")
            ev.retryable = True
            ev.page = None
            ev.status = CollectionStatus.FAILED
        return self._finalize(ev, out_dir, rel_dir, campaign_dir)

    async def _collect_page(self, ev: Evidence, url: str, out_dir: Path, rel_dir: Path) -> None:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        s = self.settings
        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": s.viewport_width, "height": s.viewport_height},
            "accept_downloads": False,
            "service_workers": "block",
            "java_script_enabled": True,
            "ignore_https_errors": s.ignore_https_errors,
            "locale": "pt-BR",
            "timezone_id": "America/Sao_Paulo",
            "permissions": [],
        }
        if s.browser_user_agent:
            ctx_kwargs["user_agent"] = s.browser_user_agent
        proxy = EgressProxy(self.policy, upstream=s.browser_proxy, pin_upstream=s.browser_proxy_pin_ip)
        await proxy.__aenter__()
        ctx_kwargs["proxy"] = {"server": proxy.url, "bypass": "<-loopback>"}
        guard = _PageGuard(self.policy, s.max_requests_per_page)
        try:
            context = await self._browser.new_context(**ctx_kwargs)
        except BaseException:
            await proxy.__aexit__(None, None, None)
            raise
        try:
            await context.route("**/*", guard.on_route)
            await context.route_web_socket("**/*", guard.on_websocket)
            page = await context.new_page()
            context.on("page", lambda p: asyncio.ensure_future(p.close()))  # popups
            page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
            page.on("response", guard.on_response)
            page.set_default_timeout(s.nav_timeout_ms)

            fetch_url = _social_embed_url(url)
            try:
                response = await page.goto(fetch_url, wait_until="domcontentloaded", timeout=s.nav_timeout_ms)
            except PlaywrightTimeout:
                ev.status = CollectionStatus.FAILED
                ev.retryable = True
                ev.errors.append("navigation timeout")
                ev.not_inspected.append("conteúdo da página não inspecionado (timeout de navegação)")
                return
            except PlaywrightError as exc:
                msg = str(exc).splitlines()[0][:300]
                ev.status = CollectionStatus.BLOCKED if "ERR_BLOCKED_BY_CLIENT" in msg else CollectionStatus.FAILED
                ev.retryable = any(k in msg for k in ("ERR_CONNECTION", "ERR_TIMED_OUT", "ERR_NETWORK"))
                ev.errors.append(f"navigation error: {msg}")
                ev.not_inspected.append("conteúdo da página não inspecionado (falha de acesso)")
                return

            # Redirect chain of the main navigation.
            chain: list[str] = []
            if response is not None:
                req = response.request
                while req.redirected_from is not None:
                    req = req.redirected_from
                    chain.append(req.url)
                ev.redirect_chain = list(reversed(chain))
                ev.http_status = response.status
            try:
                await page.wait_for_load_state("networkidle", timeout=max(1000, s.settle_ms * 2))
            except PlaywrightTimeout:
                ev.errors.append("network did not become idle (continuing)")
            if s.settle_ms:
                await page.wait_for_timeout(s.settle_ms)
            ev.final_url = url if fetch_url != url and page.url == fetch_url else page.url

            # The egress proxy connected to a validated (pinned) IP; record it for custody.
            ev.server_ip = proxy.ip_for(hostname_of(ev.final_url))
            try:
                self.policy.check_static(ev.final_url)
            except PolicyViolation as exc:
                ev.status = CollectionStatus.BLOCKED
                ev.errors.append(f"final URL violates policy: {exc}")
                return

            ev.status = CollectionStatus.OK
            raw = await page.evaluate(EXTRACT_JS, s.max_text_chars)  # truncated in-page (memory bound)
            raw["visible_text"] = (raw.get("visible_text") or "")[: s.max_text_chars]
            ev.page = PageData.model_validate(raw)
            lowered = (ev.page.title + " " + ev.page.visible_text[:5000]).lower()
            if any(m in lowered for m in CORPORATE_BLOCK_MARKERS):
                ev.status = CollectionStatus.BLOCKED
                ev.retryable = False
                ev.errors.append(
                    f"HTTP {ev.http_status or 403}: acesso bloqueado pelo filtro web corporativo da rede"
                )
                ev.not_inspected.append(
                    "conteúdo real do site não inspecionado (acesso bloqueado pela política de rede corporativa)"
                )
            elif ev.http_status and ev.http_status >= 400:
                ev.status = CollectionStatus.FAILED
                ev.errors.append(f"HTTP {ev.http_status}: page content is an error page")
            if any(m in lowered for m in CHALLENGE_MARKERS):
                ev.errors.append("possible bot challenge/CAPTCHA detected; no bypass attempted")
                ev.not_inspected.append("conteúdo real possivelmente oculto por desafio anti-bot")
                if ev.status == CollectionStatus.OK:
                    ev.status = CollectionStatus.PARTIAL

            # --- artifacts --------------------------------------------------------------
            await self._write(out_dir / "viewport.png", await page.screenshot(type="png", animations="disabled"))
            try:
                height = int(
                    await page.evaluate(
                        "() => Math.max(document.body ? document.body.scrollHeight : 0,"
                        " document.documentElement.scrollHeight)"
                    )
                )
                # Always clip: a page that grows while being captured cannot exceed the cap.
                shot_kwargs: dict[str, Any] = {
                    "type": "png",
                    "full_page": True,
                    "animations": "disabled",
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": s.viewport_width,
                        "height": max(1, min(height, s.max_fullpage_height)),
                    },
                }
                if height > s.max_fullpage_height:
                    ev.not_inspected.append(
                        f"screenshot completo limitado a {s.max_fullpage_height}px "
                        f"(página com {height}px); texto integral preservado em text.txt"
                    )
                await self._write(out_dir / "fullpage.png", await page.screenshot(**shot_kwargs))
            except PlaywrightError as exc:
                ev.errors.append(f"full-page screenshot failed: {str(exc)[:200]}")
                ev.not_inspected.append("screenshot da página completa indisponível")
                if ev.status == CollectionStatus.OK:
                    ev.status = CollectionStatus.PARTIAL
            total, head = await page.evaluate(
                "(n) => { const d = document.documentElement; const h = d ? d.outerHTML : '';"
                " return [h.length, h.slice(0, n)]; }",
                s.max_html_bytes,
            )
            html = str(head).encode("utf-8", errors="replace")
            if int(total) > s.max_html_bytes or len(html) > s.max_html_bytes:
                ev.errors.append(f"HTML truncated to {s.max_html_bytes} bytes")
                html = html[: s.max_html_bytes]
            await self._write(out_dir / "page.html.txt", html)
            await self._write(out_dir / "text.txt", ev.page.visible_text.encode("utf-8"))
            await self._write(out_dir / "page.json", ev.page.model_dump_json(indent=2).encode("utf-8"))
            ev.not_inspected.append(
                "apenas a URL descoberta foi inspecionada; páginas internas, "
                "formulários e fluxos após cliques não foram acessados"
            )
        finally:
            ev.blocked_requests = list(dict.fromkeys(guard.blocked + proxy.blocked))[:200]
            await context.close()
            await proxy.__aexit__(None, None, None)

    @staticmethod
    async def _write(path: Path, data: bytes) -> None:
        await asyncio.to_thread(path.write_bytes, data)

    def _finalize(self, ev: Evidence, out_dir: Path, rel_dir: Path, campaign_dir: Path) -> Evidence:
        kinds = {
            "viewport.png": "screenshot_viewport",
            "fullpage.png": "screenshot_full",
            "page.html.txt": "html",
            "text.txt": "text",
            "page.json": "page_data",
        }
        for name, kind in kinds.items():
            p = out_dir / name
            if p.exists():
                ev.artifacts.append(
                    Artifact(kind=kind, path=str(rel_dir / name), sha256=sha256_file(p), size_bytes=p.stat().st_size)
                )
        manifest = {
            "collector": COLLECTOR_VERSION,
            "candidate_id": ev.candidate_id,
            "attempt": ev.attempt,
            "status": ev.status.value,
            "original_url": ev.original_url,
            "final_url": ev.final_url,
            "redirect_chain": ev.redirect_chain,
            "http_status": ev.http_status,
            "server_ip": ev.server_ip,
            "collected_at": ev.collected_at.isoformat(),
            "manifest_written_at": datetime.now(UTC).isoformat(),
            "artifacts": [a.model_dump() for a in ev.artifacts],
            "blocked_requests": ev.blocked_requests,
            "errors": ev.errors,
            "not_inspected": ev.not_inspected,
            "hash_algorithm": "SHA-256",
        }
        mpath = out_dir / "manifest.json"
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        ev.manifest_path = str(rel_dir / "manifest.json")
        ev.manifest_sha256 = sha256_file(mpath)
        return ev
