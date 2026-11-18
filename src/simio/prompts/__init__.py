"""Versioned prompt templates (Markdown with a small front-matter header)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


@lru_cache(maxsize=16)
def load_prompt(name: str) -> tuple[str, str]:
    """Return (version, template) for ``prompts/<name>.md``."""
    raw = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    version = "unversioned"
    body = raw
    if raw.startswith("---"):
        _, header, body = raw.split("---", 2)
        for line in header.strip().splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "version":
                version = value.strip()
    return version, body.strip()


def render(name: str, **values: str) -> tuple[str, str]:
    version, template = load_prompt(name)
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", val)
    return version, out
