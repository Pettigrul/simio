"""Consolidated HTML campaign report aligned with Banco BV Design System."""

from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from simio.core.storage import Storage
from simio.reporting.data import CandidateRecord
from simio.reporting.logo_data import BV_LOGO_WHITE_DATA_URI

_env = Environment(
    loader=PackageLoader("simio.reporting", "templates"),
    autoescape=select_autoescape(["html", "j2"], default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)

LABEL_MAP = {
    "STRONG_INDICATION": "Forte Indício",
    "REVIEW_REQUIRED": "Requer Análise",
    "INCONCLUSIVE": "Inconclusivo",
    "NO_INDICATION": "Sem Indício",
}


def render_campaign_report(
    records: list[CandidateRecord],
    storage: Storage,
    campaign_id: str,
    dossiers: dict[str, str],
    report_html_path: Path,
) -> str:
    camp = storage.get_campaign(campaign_id) or {}
    brand_id = camp.get("brand", "bv")
    camp_status = camp.get("status", "COMPLETED")
    
    # Execution date formatted in Brazilian standard
    now = datetime.now(UTC)
    executed_at = now.strftime("%d/%m/%Y %H:%M:%S UTC")

    counts = Counter()
    pending_reviews = 0
    candidate_rows = []

    for rec in records:
        f = rec.final
        c_status = f.classification.value if f else "INCONCLUSIVE"
        counts[c_status] += 1

        if f and f.requires_human_review:
            pending_reviews += 1

        dossier_rel = ""
        if rec.candidate.id in dossiers:
            # relative link from report_html_path to dossier file
            target = report_html_path.parent / dossiers[rec.candidate.id]
            dossier_rel = Path(os.path.relpath(target, report_html_path.parent)).as_posix()

        review_status = f.human_review_status if f else "NOT_REQUIRED"
        if rec.review:
            review_status = f"{rec.review.decision.value} ({review_status})"

        candidate_rows.append({
            "id": rec.candidate.id,
            "domain": rec.candidate.registrable_domain or rec.candidate.hostname,
            "url": (rec.evidence.final_url if rec.evidence else None) or rec.candidate.url,
            "classification": c_status,
            "classification_label": LABEL_MAP.get(c_status, c_status),
            "is_priority": rec.is_priority,
            "entity_type": rec.identity.classification.value if rec.identity else "DESCONHECIDO",
            "entity_name": (rec.identity.display_name or rec.identity.entity_key) if rec.identity else "",
            "brand_scenario": rec.brand.case.value if rec.brand else "",
            "review_status": review_status,
            "summary": f.justification if f else "",
            "legacy_logo": bool(f and f.legacy_visual_identity),
            "sensitive_data": ", ".join(f.sensitive_data_requested) if f else "",
            "dossier_rel": dossier_rel,
        })

    # Sort candidates by priority & classification (STRONG_INDICATION first)
    order = {"STRONG_INDICATION": 0, "REVIEW_REQUIRED": 1, "INCONCLUSIVE": 2, "NO_INDICATION": 3}
    candidate_rows.sort(key=lambda r: (0 if r["is_priority"] else 1, order.get(r["classification"], 9)))

    disc = storage.get_checkpoint(campaign_id, "discovery", {}) or {}
    sources_used = disc.get("sources_used", [])
    llm_reqs, llm_toks = storage.usage_totals(campaign_id)

    return _env.get_template("campaign_report.html.j2").render(
        campaign_id=campaign_id,
        brand_id=brand_id,
        campaign_status=camp_status,
        executed_at=executed_at,
        total_candidates=len(records),
        counts=counts,
        pending_reviews=pending_reviews,
        candidates=candidate_rows,
        sources_used=sources_used,
        llm_requests=llm_reqs,
        llm_tokens=llm_toks,
        bv_logo_white_uri=BV_LOGO_WHITE_DATA_URI,
    )


def write_campaign_html(
    report_file: Path,
    records: list[CandidateRecord],
    storage: Storage,
    campaign_id: str,
    dossiers: dict[str, str],
) -> Path:
    report_file.parent.mkdir(parents=True, exist_ok=True)
    content = render_campaign_report(records, storage, campaign_id, dossiers, report_file)
    report_file.write_text(content, encoding="utf-8")
    return report_file
