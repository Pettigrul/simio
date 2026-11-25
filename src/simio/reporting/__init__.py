"""Report generation entry point."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from simio.brands import load_brand
from simio.config import Settings
from simio.core.storage import Storage
from simio.reporting.campaign_html import write_campaign_html
from simio.reporting.data import load_records
from simio.reporting.dossier import write_dossiers
from simio.reporting.excel import write_workbook
from simio.reporting.pdf import generate_campaign_pdf


@dataclass
class ReportResult:
    workbook: Path
    html_report: Path
    pdf_report: Path | None
    dossiers: dict[str, str]
    report_dir: Path


def generate_reports(
    settings: Settings, storage: Storage, campaign_id: str, all_dossiers: bool = False
) -> ReportResult:
    camp = storage.get_campaign(campaign_id)
    if camp is None:
        raise KeyError(f"campaign not found: {campaign_id}")
    brand = load_brand(camp["brand"], settings.brand_file)
    records = load_records(storage, campaign_id, brand)
    report_dir = settings.output_dir / "reports" / campaign_id
    dossiers = write_dossiers(records, settings.output_dir, report_dir, all_candidates=all_dossiers)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    # 1. Excel Workbook (.xlsx)
    workbook_path = report_dir / f"simio-{campaign_id}-{stamp}.xlsx"
    workbook = write_workbook(workbook_path, storage, campaign_id, records, dossiers)

    # 2. Consolidated Campaign HTML Report (.html)
    html_report_path = report_dir / f"simio-{campaign_id}-{stamp}.html"
    html_report = write_campaign_html(html_report_path, records, storage, campaign_id, dossiers)

    # 3. Consolidated Campaign PDF Report (.pdf)
    pdf_report_path = report_dir / f"simio-{campaign_id}-{stamp}.pdf"
    pdf_report = generate_campaign_pdf(html_report, pdf_report_path)

    return ReportResult(
        workbook=workbook,
        html_report=html_report,
        pdf_report=pdf_report,
        dossiers=dossiers,
        report_dir=report_dir,
    )
