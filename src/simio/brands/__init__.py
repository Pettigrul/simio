"""Brand profiles (TOML) and text-matching helpers."""

from __future__ import annotations

import re
import tomllib
import unicodedata
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from simio.core.urls import domain_matches

BRANDS_DIR = Path(__file__).parent


def fold(text: str) -> str:
    """Lower-case and strip accents for robust phrase matching."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _phrase_regex(phrases: list[str]) -> re.Pattern[str] | None:
    folded = sorted({fold(p) for p in phrases if p.strip()}, key=len, reverse=True)
    if not folded:
        return None
    body = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in folded)
    return re.compile(rf"(?<![\w]){'(?:' + body + ')'}(?![\w])")


class BrandProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: str
    display_name: str
    language: str = "pt-BR"
    strong_terms: list[str]
    weak_terms: list[str] = Field(default_factory=list)
    official_domains: list[str] = Field(default_factory=list)
    institution_names: list[str] = Field(default_factory=list)
    institution_cnpjs: list[str] = Field(default_factory=list)
    domain_patterns: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    establishment_types: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    official_claim_phrases: list[str] = Field(default_factory=list)
    correspondent_disclosure_phrases: list[str] = Field(default_factory=list)
    partnership_phrases: list[str] = Field(default_factory=list)
    deprioritized_domains: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------ compiled matchers
    @cached_property
    def _strong_re(self) -> re.Pattern[str] | None:
        return _phrase_regex(self.strong_terms)

    @cached_property
    def _weak_re(self) -> re.Pattern[str] | None:
        return _phrase_regex(self.weak_terms)

    @cached_property
    def _claim_re(self) -> re.Pattern[str] | None:
        return _phrase_regex(self.official_claim_phrases)

    @cached_property
    def _disclosure_re(self) -> re.Pattern[str] | None:
        return _phrase_regex(self.correspondent_disclosure_phrases)

    @cached_property
    def _partner_re(self) -> re.Pattern[str] | None:
        return _phrase_regex(self.partnership_phrases)

    @cached_property
    def _domain_res(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.domain_patterns]

    @staticmethod
    def _find(rx: re.Pattern[str] | None, text: str) -> list[str]:
        if rx is None or not text:
            return []
        return [m.group(0) for m in rx.finditer(fold(text))]

    def strong_mentions(self, text: str) -> list[str]:
        return self._find(self._strong_re, text)

    def weak_mentions(self, text: str) -> list[str]:
        return self._find(self._weak_re, text)

    def mentions(self, text: str) -> list[str]:
        return self.strong_mentions(text) + self.weak_mentions(text)

    def official_claims(self, text: str) -> list[str]:
        return self._find(self._claim_re, text)

    def disclosures(self, text: str) -> list[str]:
        return self._find(self._disclosure_re, text)

    def partnership_mentions(self, text: str) -> list[str]:
        return self._find(self._partner_re, text)

    def domain_brand_match(self, hostname: str) -> str | None:
        host = hostname.lower()
        for rx in self._domain_res:
            if rx.search(host):
                return rx.pattern
        return None

    def is_official(self, hostname: str) -> bool:
        return domain_matches(hostname, self.official_domains)

    def is_deprioritized(self, hostname: str) -> bool:
        return domain_matches(hostname, self.deprioritized_domains)

    def all_terms(self) -> list[str]:
        return self.strong_terms + self.weak_terms


def load_brand(brand: str, brand_file: Path | None = None) -> BrandProfile:
    path = brand_file or BRANDS_DIR / f"{brand}.toml"
    if not path.is_file():
        available = sorted(p.stem for p in BRANDS_DIR.glob("*.toml"))
        raise FileNotFoundError(f"Brand profile '{brand}' not found ({path}). Available: {', '.join(available)}")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return BrandProfile(**data)
