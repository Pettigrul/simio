"""Consolidated Excel workbook (openpyxl)."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from simio.core.storage import Storage
from simio.reporting.data import CandidateRecord

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(color="FFFFFF", bold=True)
FILLS = {
    "STRONG_INDICATION": PatternFill("solid", fgColor="F4C7C3"),
    "REVIEW_REQUIRED": PatternFill("solid", fgColor="FCE8B2"),
    "INCONCLUSIVE": PatternFill("solid", fgColor="E0E0E0"),
    "NO_INDICATION": PatternFill("solid", fgColor="D9EAD3"),
}
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
MAX_CELL = 32000


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (list, tuple, set)):
        v = "\n".join(str(x) for x in v)
    elif isinstance(v, dict):
        v = json.dumps(v, ensure_ascii=False)
    if isinstance(v, str):
        v = _ILLEGAL.sub("", v)[:MAX_CELL]
        # Neutralise spreadsheet formula injection from untrusted page content.
        if v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            v = "'" + v
    return v


def _sheet(
    wb: Workbook,
    title: str,
    headers: list[str],
    rows: list[list[Any]],
    widths: dict[int, int] | None = None,
    fill_col: int | None = None,
) -> None:
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for row in rows:
        ws.append([_cell(v) for v in row])
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        if fill_col is not None:
            fill = FILLS.get(str(r[fill_col].value))
            if fill:
                r[fill_col].fill = fill
    for i in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, 22)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions


def _main_evidence(rec: CandidateRecord, n: int = 6) -> str:
    if not rec.final:
        return ""
    by_ref = {i.ref: i for i in rec.catalog}
    return "\n".join(f"[{r}] {by_ref[r].content[:220]}" for r in rec.final.key_evidence[:n] if r in by_ref)


def write_workbook(
    path: Path, storage: Storage, campaign_id: str, records: list[CandidateRecord], dossier_paths: dict[str, str]
) -> Path:
    camp = storage.get_campaign(campaign_id) or {}
    wb = Workbook()
    if wb.active is not None:
        wb.remove(wb.active)
    finals = [r.final for r in records if r.final]
    cls_counts = Counter(f.classification.value for f in finals)
    status_counts = storage.status_counts(campaign_id)
    disc = storage.get_checkpoint(campaign_id, "discovery", {}) or {}
    req, tok = storage.usage_totals(campaign_id)
    analyzers = Counter(r.brand.analyzer for r in records if r.brand)

    summary: list[list[Any]] = [
        ["Campanha", campaign_id],
        ["Marca", camp.get("brand")],
        ["Status da campanha", camp.get("status")],
        ["Início", camp.get("created_at")],
        ["Relatório gerado em", datetime.now(UTC).isoformat(timespec="seconds")],
        ["Candidatos descobertos (únicos)", len(records)],
        ["Status do pipeline", json.dumps(status_counts, ensure_ascii=False)],
        ["STRONG_INDICATION", cls_counts.get("STRONG_INDICATION", 0)],
        ["REVIEW_REQUIRED", cls_counts.get("REVIEW_REQUIRED", 0)],
        ["INCONCLUSIVE", cls_counts.get("INCONCLUSIVE", 0)],
        ["NO_INDICATION", cls_counts.get("NO_INDICATION", 0)],
        ["Pendentes de revisão humana", sum(1 for f in finals if f.requires_human_review)],
        ["Fontes de descoberta usadas", ", ".join(disc.get("sources_used", []))],
        ["Consultas executadas", json.dumps(disc.get("queries", {}), ensure_ascii=False)],
        ["Diagnóstico da descoberta", "\n".join(disc.get("diagnostics", []))],
        ["Analisadores utilizados", json.dumps(dict(analyzers), ensure_ascii=False)],
        ["Requisições LLM / tokens", f"{req} / {tok}"],
        ["Parâmetros", json.dumps(camp.get("params", {}), ensure_ascii=False)],
        [
            "Aviso",
            "Classificações são indícios para apoio à auditoria; não constituem conclusão "
            "regulatória. Pontuações não representam probabilidade de irregularidade.",
        ],
    ]
    _sheet(wb, "Executive Summary", ["Item", "Valor"], summary, {1: 34, 2: 110})

    cand_headers = [
        "ID",
        "Domínio",
        "URL",
        "URL final",
        "Origem da descoberta",
        "Consulta utilizada",
        "Identidade atribuída",
        "Categoria de relacionamento",
        "Confiança da atribuição",
        "Classificação",
        "Prioridade",
        "Cenário",
        "Confiança da extração",
        "Suficiência da evidência",
        "Justificativa",
        "Principais evidências",
        "Limitações",
        "Revisão humana",
        "Decisão do revisor",
        "Screenshot (viewport)",
        "Screenshot (completo)",
        "Dossiê",
        "Data da coleta",
        "Status pipeline",
        "Score de priorização (fila)",
    ]
    rows = []
    for r in records:
        c, f, ev, idn = r.candidate, r.final, r.evidence, r.identity
        vp = ev.artifact("screenshot_viewport") if ev else None
        fp = ev.artifact("screenshot_full") if ev else None
        rows.append(
            [
                c.id,
                c.registrable_domain,
                c.url,
                ev.final_url if ev else "",
                ", ".join(sorted({h.source for h in r.hits})),
                "\n".join(sorted({h.query for h in r.hits if h.query}))[:2000],
                (idn.display_name or idn.entity_key) if idn else "",
                idn.classification.value if idn else "",
                idn.attribution_confidence.value if idn else "",
                f.classification.value if f else "",
                f.priority.value if f else "",
                f.case.value if f else "",
                f.extraction_confidence.value if f else "",
                f.evidence_sufficiency.value if f else "",
                f.justification if f else (c.last_error or ""),
                _main_evidence(r),
                "\n".join(f.limitations) if f else "",
                f.human_review_status if f else "",
                r.review.decision.value if r.review else "",
                f"campaigns/{c.campaign_id}/{vp.path}" if vp else "",
                f"campaigns/{c.campaign_id}/{fp.path}" if fp else "",
                dossier_paths.get(c.id, ""),
                ev.collected_at.isoformat(timespec="seconds") if ev else "",
                c.status.value,
                round(c.priority_score, 1),
            ]
        )
    widths = {1: 13, 2: 24, 3: 40, 4: 40, 6: 40, 15: 60, 16: 60, 17: 50}
    _sheet(wb, "Candidates", cand_headers, rows, widths, fill_col=9)
    strong = [
        row
        for row, r in zip(rows, records, strict=True)
        if r.final and r.final.classification.value == "STRONG_INDICATION"
    ]
    _sheet(wb, "Strong Indications", cand_headers, strong, widths, fill_col=9)

    id_rows = []
    for r in records:
        idn = r.identity
        if not idn:
            continue
        facts = "\n".join(
            f"{f.kind}: {f.value} [{f.evidence_ref}]" + ("" if f.valid in (None, True) else " (inválido)")
            for f in idn.facts
        )
        id_rows.append(
            [
                r.candidate.id,
                r.candidate.registrable_domain,
                idn.entity_key,
                idn.display_name,
                idn.classification.value,
                idn.attribution_confidence.value,
                ", ".join(idn.owner_cnpjs),
                ", ".join(idn.referenced_institution_cnpjs),
                facts,
                "\n".join(idn.inferences),
                json.dumps(idn.registry_match.model_dump(), ensure_ascii=False) if idn.registry_match else "",
                idn.rationale,
                "\n".join(idn.limitations),
            ]
        )
    _sheet(
        wb,
        "Identity Attribution",
        [
            "ID",
            "Domínio",
            "Chave da entidade",
            "Nome",
            "Classificação",
            "Confiança",
            "CNPJs do responsável",
            "CNPJs da instituição citados",
            "Fatos observados (ref)",
            "Inferências",
            "Correspondência em cadastro",
            "Racional",
            "Limitações",
        ],
        id_rows,
        {9: 60, 10: 50, 12: 50},
    )

    ent_rows = [
        [e["entity_key"], e["display_name"], e["classification"], e["domains"], e["candidate_ids"]]
        for e in storage.entities(campaign_id)
    ]
    if ent_rows:
        _sheet(wb, "Entities", ["Chave", "Nome", "Classificação", "Domínios", "Candidatos"], ent_rows)

    ev_rows = []
    for r in records:
        for ev in r.all_evidence:
            for a in ev.artifacts:
                ev_rows.append(
                    [
                        r.candidate.id,
                        ev.attempt,
                        ev.status.value,
                        a.kind,
                        f"campaigns/{r.candidate.campaign_id}/{a.path}",
                        a.sha256,
                        a.size_bytes,
                        ev.collected_at.isoformat(timespec="seconds"),
                        ev.manifest_path,
                        ev.manifest_sha256,
                    ]
                )
            if not ev.artifacts:
                ev_rows.append(
                    [
                        r.candidate.id,
                        ev.attempt,
                        ev.status.value,
                        "(nenhum artefato)",
                        "",
                        "",
                        0,
                        ev.collected_at.isoformat(timespec="seconds"),
                        ev.manifest_path,
                        ev.manifest_sha256,
                    ]
                )
    _sheet(
        wb,
        "Evidence Index",
        [
            "ID",
            "Tentativa",
            "Status da coleta",
            "Tipo",
            "Caminho",
            "SHA-256",
            "Bytes",
            "Coletado em",
            "Manifesto",
            "SHA-256 do manifesto",
        ],
        ev_rows,
        {5: 60, 6: 66, 9: 50, 10: 66},
    )

    log_rows = [
        [d["at"], d["candidate_id"] or "", d["agent"], d["decision"], d["detail_json"]]
        for d in storage.decisions(campaign_id)
    ]
    for q in storage.list_queries(campaign_id):
        meta = json.loads(q["metadata_json"] or "{}")
        meta.pop("search_entry_point_html", None)
        log_rows.append(
            [
                q["executed_at"],
                "",
                f"query:{q['source']}",
                q["status"],
                json.dumps(
                    {
                        "query": q["query_text"],
                        "hypothesis": q["hypothesis_id"],
                        "results": q["result_count"],
                        "error": q["error"],
                        **meta,
                    },
                    ensure_ascii=False,
                ),
            ]
        )
    log_rows.sort(key=lambda x: str(x[0]))
    _sheet(wb, "Investigation Log", ["Quando", "Candidato", "Agente", "Decisão", "Detalhe"], log_rows, {1: 22, 5: 120})

    err_rows = [
        [e["at"], e["candidate_id"] or "", e["stage"], e["error_type"], e["message"]]
        for e in storage.errors(campaign_id)
    ]
    for r in records:
        if r.evidence and (r.evidence.errors or r.evidence.not_inspected):
            err_rows.append(
                [
                    r.evidence.collected_at.isoformat(timespec="seconds"),
                    r.candidate.id,
                    "collection",
                    r.evidence.status.value,
                    "\n".join([*r.evidence.errors, *r.evidence.not_inspected]),
                ]
            )
        if r.candidate.status.value in ("DISCOVERED",):
            err_rows.append(
                [
                    "",
                    r.candidate.id,
                    "queue",
                    "NOT_PROCESSED",
                    "candidato não processado (fora do limite --limit ou campanha pausada)",
                ]
            )
    for diag in disc.get("diagnostics", []):
        err_rows.append(["", "", "discovery", "DIAGNOSTIC", diag])
    _sheet(wb, "Errors and Limitations", ["Quando", "Candidato", "Etapa", "Tipo", "Mensagem"], err_rows, {5: 120})

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
