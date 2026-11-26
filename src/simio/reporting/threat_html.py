"""Threat-intelligence briefing (HTML): what impersonates the bank or endangers its customers, with the
facts behind each level, the domain intelligence and the takedown contacts.

URLs of suspicious pages are *defanged* (``hxxps://exemplo[.]com``) so nobody opens them by accident."""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from simio.agents.domain_intel import summarize as summarize_intel
from simio.agents.threat_triage import LEVELS, ThreatView
from simio.core.models import DomainIntel
from simio.reporting.data import CandidateRecord

_env = Environment(
    loader=PackageLoader("simio.reporting", "templates"),
    autoescape=select_autoescape(["html", "j2"], default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def defang(url: str) -> str:
    out = re.sub(r"^http", "hxxp", url, flags=re.IGNORECASE)
    host_end = out.find("/", out.find("://") + 3) if "://" in out else -1
    host = out[: host_end if host_end > 0 else len(out)]
    return host.replace(".", "[.]") + (out[host_end:] if host_end > 0 else "")


def write_threat_html(
    path: Path,
    rows: list[tuple[ThreatView, CandidateRecord, DomainIntel | None]],
    campaign_id: str,
    output_dir: Path,
    dossier_paths: dict[str, str],
    defensive: list[tuple[str, str]] | None = None,
) -> Path:
    cards = []
    for t, r, item in rows:
        ev, f = r.evidence, r.final
        shot = None
        if ev:
            a = ev.artifact("screenshot_viewport")
            if a:
                target = output_dir / "campaigns" / r.candidate.campaign_id / a.path
                shot = Path(os.path.relpath(target, path.parent)).as_posix()
        dossier = dossier_paths.get(r.candidate.id)
        cards.append(
            {
                "level": t.level,
                "score": t.score,
                "categories": t.categories,
                "facts": t.facts,
                "url": defang((ev.final_url if ev else None) or r.candidate.url),
                "domain": r.candidate.registrable_domain,
                "intel": summarize_intel(item) if item else None,
                "sensitive": ", ".join(f.sensitive_data_requested) if f else "",
                "classification": f.classification.value if f else "",
                "justification": (f.justification or "")[:700] if f else "",
                "shot": shot,
                "dossier": Path(os.path.relpath(output_dir / dossier, path.parent)).as_posix() if dossier else None,
                "cid": r.candidate.id,
            }
        )
    levels = Counter(c["level"] for c in cards)
    categories = Counter(cat for c in cards for cat in c["categories"])
    html = _env.get_template("threat_report.html.j2").render(
        campaign_id=campaign_id,
        generated_at=datetime.now(UTC).strftime("%d/%m/%Y %H:%M UTC"),
        cards=cards,
        levels=[(lv, levels.get(lv, 0)) for lv in LEVELS[:-1]],
        categories=categories.most_common(),
        defensive=defensive or [],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
