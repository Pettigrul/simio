"""Report generation entry point."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from simio.agents.domain_intel import load_intel
from simio.agents.visual import crop_match
from simio.brands import load_brand
from simio.config import Settings
from simio.core.storage import Storage
from simio.reporting.campaign_html import write_campaign_html
from simio.reporting.data import CandidateRecord, load_records
from simio.reporting.dossier import write_dossiers
from simio.reporting.excel import threat_rows, write_workbook
from simio.reporting.pdf import generate_campaign_pdf
from simio.reporting.threat_html import write_threat_html


@dataclass
class ReportResult:
    workbook: Path
    html_report: Path
    pdf_report: Path | None
    dossiers: dict[str, str]
    report_dir: Path
    threat_report: Path | None = None


def write_legacy_logo_crops(records: list[CandidateRecord], campaign_dir: Path, report_dir: Path) -> dict[str, str]:
    """PNG cut-outs of the best legacy-logo match of each page (``<report>/logo-antiga/``)."""
    out: dict[str, str] = {}
    for r in records:
        matches = r.brand.visual_matches if r.brand else []
        if not matches or r.evidence is None:
            continue
        best = max(matches, key=lambda m: m.score)
        try:
            crop = crop_match(r.evidence, campaign_dir, best)
        except OSError:
            continue
        if crop is None:
            continue
        path = report_dir / "logo-antiga" / f"{r.candidate.id}-{r.candidate.registrable_domain}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        crop.save(path)
        out[r.candidate.id] = str(path)
    return out


def generate_reports(
    settings: Settings, storage: Storage, campaign_id: str, all_dossiers: bool = False
) -> ReportResult:
    camp = storage.get_campaign(campaign_id)
    if camp is None:
        raise KeyError(f"campaign not found: {campaign_id}")
    brand = load_brand(camp["brand"], settings.brand_file)
    records = load_records(storage, campaign_id, brand)
    report_dir = settings.output_dir / "reports" / campaign_id
    intel = load_intel(settings.output_dir, campaign_id)
    dossiers = write_dossiers(
        records, settings.output_dir, report_dir, all_candidates=all_dossiers, domain_intel=intel
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    # 1. Excel Workbook (.xlsx)
    workbook_path = report_dir / f"simio-{campaign_id}-{stamp}.xlsx"
    crops = write_legacy_logo_crops(records, settings.output_dir / "campaigns" / campaign_id, report_dir)
    workbook = write_workbook(workbook_path, storage, campaign_id, records, dossiers, crops, intel)

    # 2. Consolidated Campaign HTML Report (.html)
    html_report_path = report_dir / f"simio-{campaign_id}-{stamp}.html"
    html_report = write_campaign_html(html_report_path, records, storage, campaign_id, dossiers)

    # 3. Consolidated Campaign PDF Report (.pdf)
    pdf_report_path = report_dir / f"simio-{campaign_id}-{stamp}.pdf"
    pdf_report = generate_campaign_pdf(html_report, pdf_report_path)

    # 4. Threat-intelligence briefing (.html), when the campaign has threats
    focus = (camp.get("params") or {}).get("focus")
    rows = threat_rows(records, brand, focus, intel)
    threat_report = None
    if rows:
        defensive = sorted(
            (h, i.rdap.holder or "")
            for h, i in intel.items()
            if i.rdap and not i.platform and (
                (i.rdap.holder_document and i.rdap.holder_document in brand.institution_cnpjs)
                or (i.rdap.holder and brand.is_institution_name(i.rdap.holder))
            )
        )
        threat_report = write_threat_html(
            report_dir / f"simio-{campaign_id}-{stamp}-ameacas.html",
            rows,
            campaign_id,
            settings.output_dir,
            dossiers,
            defensive,
        )

    return ReportResult(
        workbook=workbook,
        html_report=html_report,
        pdf_report=pdf_report,
        dossiers=dossiers,
        report_dir=report_dir,
        threat_report=threat_report,
    )
