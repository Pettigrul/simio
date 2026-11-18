"""Per-candidate HTML dossiers (local files, no scripts, untrusted content always escaped)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from simio.reporting.data import CandidateRecord

_env = Environment(
    loader=PackageLoader("simio.reporting", "templates"),
    autoescape=select_autoescape(["html", "j2"], default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_dossier(rec: CandidateRecord, output_dir: Path, dossier_path: Path) -> str:
    ev = rec.evidence
    shots = []
    if ev:
        for kind, label in (
            ("screenshot_viewport", "Área visível inicial (E-SHOT-VIEWPORT)"),
            ("screenshot_full", "Página completa (E-SHOT-FULL)"),
        ):
            a = ev.artifact(kind)
            if a:
                target = output_dir / "campaigns" / rec.candidate.campaign_id / a.path
                rel = os.path.relpath(target, dossier_path.parent)
                shots.append({"src": Path(rel).as_posix(), "label": label, "sha": a.sha256})
    return _env.get_template("dossier.html.j2").render(
        c=rec.candidate,
        f=rec.final,
        b=rec.brand,
        idn=rec.identity,
        comp=rec.compliance,
        rv=rec.review,
        ev=ev,
        catalog=rec.catalog,
        hits=rec.hits,
        transitions=rec.transitions,
        shots=shots,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def write_dossiers(
    records: list[CandidateRecord], output_dir: Path, report_dir: Path, all_candidates: bool = False
) -> dict[str, str]:
    """Write dossiers for priority candidates (or all). Returns {candidate_id: path relative to
    output_dir}."""
    ddir = report_dir / "dossiers"
    ddir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for rec in records:
        if not (all_candidates or rec.is_priority):
            continue
        path = ddir / f"{rec.candidate.id}.html"
        path.write_text(render_dossier(rec, output_dir, path), encoding="utf-8")
        written[rec.candidate.id] = Path(os.path.relpath(path, output_dir)).as_posix()
    return written
