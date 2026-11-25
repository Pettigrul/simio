"""Lookalike-domain discovery: typosquatting, combosquatting and TLD swaps.

Generates permutations of the institution's official domains (fuzzers modelled on dnstwist:
omission, repetition, transposition, keyboard replacement/insertion, homoglyphs, vowel swap,
hyphenation, addition), plus *combosquatting* (brand label + sector keyword, e.g.
``bancobv-financiamento.com.br``) and TLD swaps. Combosquatting is by far the most abused form in
practice (Kintis et al., CCS 2017), so it is generated for the short brand labels where pure typos
would only produce unrelated two-letter domains.

Only names that **resolve in DNS to a public address** become discovery hits; everything else is
counted in the query record. Nothing is fetched here: resolved domains enter the normal pipeline
(URL policy, egress proxy, collection, analysis). A resolver that answers for non-existent names
(ISP NXDOMAIN hijacking) is detected first and disables the connector, since it would turn every
permutation into a false hit.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
from collections.abc import Awaitable, Callable, Iterable

from simio.brands import BrandProfile
from simio.core.models import DiscoveryHit, QueryRecord, SearchHypothesis

Resolver = Callable[[str], Awaitable[list[str]]]

TECHNIQUE_PT = {
    "omission": "omissão de caractere",
    "repetition": "caractere repetido",
    "transposition": "caracteres trocados de posição",
    "replacement": "tecla vizinha substituída",
    "insertion": "tecla vizinha inserida",
    "homoglyph": "caractere visualmente parecido",
    "vowel-swap": "vogal trocada",
    "hyphenation": "hífen inserido",
    "addition": "caractere acrescentado",
    "tld-swap": "mesmo nome com outra extensão",
    "combosquatting": "marca combinada com palavra do setor",
}

_KEYBOARD = {  # QWERTY / ABNT2 letter rows
    "q": "wa", "w": "qeas", "e": "wrsd", "r": "etdf", "t": "ryfg", "y": "tugh", "u": "yihj", "i": "uojk",
    "o": "ipkl", "p": "ol", "a": "qwsz", "s": "awedxz", "d": "serfcx", "f": "drtgvc", "g": "ftyhbv",
    "h": "gyujnb", "j": "huikmn", "k": "jiolm", "l": "kop", "z": "asx", "x": "zsdc", "c": "xdfv",
    "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
    "1": "2q", "2": "13wq", "3": "24ew", "4": "35re", "5": "46tr", "6": "57yt", "7": "68uy", "8": "79iu",
    "9": "80oi", "0": "9po",
}
_HOMOGLYPHS = {
    "o": ["0"], "0": ["o"], "l": ["1", "i"], "i": ["1", "l"], "1": ["l", "i"], "e": ["3"], "a": ["4"],
    "s": ["5"], "m": ["rn", "nn"], "w": ["vv"], "d": ["cl"], "g": ["q"], "q": ["g"], "u": ["v"], "v": ["u"],
    "b": ["d"], "n": ["m"],
}
_MULTI_HOMOGLYPHS = {"rn": "m", "vv": "w", "cl": "d", "nn": "m"}
_VOWELS = "aeiou"
_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"
MIN_TYPO_LABEL = 5  # shorter labels (e.g. "bv") only get combosquatting / TLD swaps

DEFAULT_SWAP_TLDS = (
    "com", "net", "org", "com.br", "net.br", "org.br", "app.br", "tec.br", "app", "online", "site",
    "digital", "info", "store", "shop", "top", "xyz", "link", "click",
)


def _valid_label(label: str) -> bool:
    return (
        0 < len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and all(c in _ALNUM or c == "-" for c in label)
    )


def split_domain(domain: str) -> tuple[str, str]:
    """``bancobv.com.br`` -> (``bancobv``, ``com.br``)."""
    label, _, suffix = domain.lower().strip(".").partition(".")
    return label, suffix


def typo_variants(label: str) -> dict[str, str]:
    """Permutations of a single DNS label -> {variant: fuzzer}."""
    out: dict[str, str] = {}

    def add(v: str, fuzzer: str) -> None:
        if v != label and _valid_label(v) and v not in out:
            out[v] = fuzzer

    n = len(label)
    for i in range(n):
        add(label[:i] + label[i + 1 :], "omission")
        add(label[:i] + label[i] + label[i:], "repetition")
        if i < n - 1:
            add(label[:i] + label[i + 1] + label[i] + label[i + 2 :], "transposition")
        for k in _KEYBOARD.get(label[i], ""):
            add(label[:i] + k + label[i + 1 :], "replacement")
            add(label[:i] + k + label[i:], "insertion")
            add(label[: i + 1] + k + label[i + 1 :], "insertion")
        for g in _HOMOGLYPHS.get(label[i], []):
            add(label[:i] + g + label[i + 1 :], "homoglyph")
        if label[i] in _VOWELS:
            for v in _VOWELS:
                add(label[:i] + v + label[i + 1 :], "vowel-swap")
        if i > 0:
            add(label[:i] + "-" + label[i:], "hyphenation")
    for pair, single in _MULTI_HOMOGLYPHS.items():
        start = label.find(pair)
        while start >= 0:
            add(label[:start] + single + label[start + 2 :], "homoglyph")
            start = label.find(pair, start + 1)
    for c in _ALNUM:
        add(label + c, "addition")
    return out


def lookalike_domains(
    seeds: Iterable[str],
    combo_labels: Iterable[str],
    keywords: Iterable[str],
    *,
    swap_tlds: Iterable[str] = DEFAULT_SWAP_TLDS,
    combo_suffixes: Iterable[str] = ("com.br", "com"),
    exclude: Callable[[str], bool] = lambda _d: False,
) -> dict[str, tuple[str, str]]:
    """{candidate domain: (fuzzer, seed)} for every seed domain and combo label."""
    out: dict[str, tuple[str, str]] = {}

    def add(domain: str, fuzzer: str, seed: str) -> None:
        if domain not in out and not exclude(domain):
            out[domain] = (fuzzer, seed)

    swap = list(dict.fromkeys(swap_tlds))
    for seed in seeds:
        label, suffix = split_domain(seed)
        if not label or not suffix:
            continue
        if len(label) >= MIN_TYPO_LABEL:
            for v, fuzzer in typo_variants(label).items():
                add(f"{v}.{suffix}", fuzzer, seed)
        for tld in swap:
            if tld != suffix:
                add(f"{label}.{tld}", "tld-swap", seed)
    kws = [k.lower().strip() for k in keywords if k.strip()]
    for label in combo_labels:
        lab = label.lower().strip()
        for kw in kws:
            for v in (f"{lab}{kw}", f"{lab}-{kw}", f"{kw}{lab}", f"{kw}-{lab}"):
                if _valid_label(v):
                    for suffix in combo_suffixes:
                        add(f"{v}.{suffix}", "combosquatting", lab)
    return out


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


def _is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


class TyposquatConnector:
    """Discovery connector. Each planned query is one seed domain (``typo:<domain>``) or one combo
    label (``combo:<label>``); the hypothesis id records which."""

    name = "typosquat"

    def __init__(
        self,
        brand: BrandProfile,
        *,
        resolver: Resolver | None = None,
        concurrency: int = 48,
        timeout_s: float = 6.0,
    ) -> None:
        self.brand = brand
        self.resolver = resolver or system_resolver
        self.concurrency = concurrency
        self.timeout_s = timeout_s
        self.disabled_reason: str | None = None
        self._hijack_checked = False

    def available(self) -> tuple[bool, str]:
        if not self.brand.typosquat_seeds and not self.brand.typosquat_combo_labels:
            return False, "typosquat: no typosquat_seeds / typosquat_combo_labels in the brand profile"
        if self.disabled_reason:
            return False, f"typosquat: disabled: {self.disabled_reason}"
        return True, "typosquat: active (DNS permutations of the official domains)"

    def queries(self) -> list[str]:
        return [f"typo:{s}" for s in self.brand.typosquat_seeds] + [
            f"combo:{c}" for c in self.brand.typosquat_combo_labels
        ]

    def _candidates(self, query: str) -> dict[str, tuple[str, str]]:
        kind, _, value = query.partition(":")
        exclude = self.brand.is_official
        if kind == "typo":
            return lookalike_domains([value], [], [], exclude=exclude)
        if kind == "combo":
            return lookalike_domains([], [value], self.brand.typosquat_keywords, exclude=exclude)
        return {}

    async def _resolve(self, host: str) -> list[str] | None:
        try:
            return await asyncio.wait_for(self.resolver(host), self.timeout_s)
        except (OSError, TimeoutError, UnicodeError):
            return None

    async def _check_hijack(self) -> None:
        if self._hijack_checked:
            return
        self._hijack_checked = True
        probe = f"simio-nx-{secrets.token_hex(6)}.com.br"
        if await self._resolve(probe):
            self.disabled_reason = (
                "the DNS resolver answers for non-existent domains (NXDOMAIN hijacking); "
                "every permutation would look registered. Use a resolver without redirection."
            )

    async def run(self, hypothesis: SearchHypothesis, query: str) -> tuple[list[DiscoveryHit], QueryRecord]:
        await self._check_hijack()
        if self.disabled_reason:
            return [], QueryRecord(
                source=self.name, query=query, hypothesis_id=hypothesis.id, status="ERROR", error=self.disabled_reason
            )
        candidates = self._candidates(query)
        sem = asyncio.Semaphore(self.concurrency)

        async def probe(domain: str) -> tuple[str, list[str] | None]:
            async with sem:
                return domain, await self._resolve(domain)

        results = await asyncio.gather(*(probe(d) for d in candidates))
        hits: list[DiscoveryHit] = []
        non_public: list[str] = []
        for domain, addrs in results:
            if not addrs:
                continue
            public = [a for a in addrs if _is_public(a)]
            if not public:
                non_public.append(domain)
                continue
            fuzzer, seed = candidates[domain]
            technique = TECHNIQUE_PT.get(fuzzer, fuzzer)
            hits.append(
                DiscoveryHit(
                    source=self.name,
                    original_url=f"http://{domain}/",
                    query=query,
                    hypothesis_id=hypothesis.id,
                    title=None,
                    snippet=(
                        f"Domínio registrado semelhante ao domínio oficial/marca '{seed}' "
                        f"(técnica: {technique}); resolve em DNS para {', '.join(public[:3])}."
                    ),
                    rank=len(hits) + 1,
                    metadata={"lookalike_of": seed, "fuzzer": fuzzer, "addresses": public[:5]},
                )
            )
        record = QueryRecord(
            source=self.name,
            query=query,
            hypothesis_id=hypothesis.id,
            status="OK" if hits else "EMPTY",
            result_count=len(hits),
            metadata={
                "permutations_checked": len(candidates),
                "resolved_public": len(hits),
                "resolved_non_public": non_public[:50],
            },
        )
        return hits, record
