"""Consolidated Excel workbook (openpyxl) aligned with Banco BV Design System."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.drawing.image import Image as OpenpyxlImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from simio.agents.discovery import FOCUS_THREAT
from simio.agents.domain_intel import summarize as summarize_intel
from simio.agents.threat_triage import ThreatView, triage
from simio.brands import BrandProfile, load_brand
from simio.core.models import DomainIntel
from simio.core.storage import Storage
from simio.core.urls import hostname_of
from simio.reporting.data import CandidateRecord

# Banco BV Design System Theme Colors
# --bv-blue-dark: #001A6B
# --bv-blue-primary: #0B5FFF
# --bv-bg-light: #F5F5F5 / #EFF4FB
HEADER_FILL = PatternFill("solid", fgColor="001A6B")
HEADER_FONT = Font(name="Inter", size=11, color="FFFFFF", bold=True)
DATA_FONT = Font(name="Inter", size=10, color="272833")

# Status and Classification Fills with high legibility and soft tint
FILLS = {
    "STRONG_INDICATION": PatternFill("solid", fgColor="FCE8E6"),  # Soft red
    "REVIEW_REQUIRED": PatternFill("solid", fgColor="FEF3D6"),    # Soft amber
    "INCONCLUSIVE": PatternFill("solid", fgColor="E7E7ED"),       # Soft gray
    "NO_INDICATION": PatternFill("solid", fgColor="E6F4EA"),      # Soft green
    # Threat levels (threat triage)
    "CRÍTICO": PatternFill("solid", fgColor="F9D2CE"),
    "ALTO": PatternFill("solid", fgColor="FCE8E6"),
    "MÉDIO": PatternFill("solid", fgColor="FEF3D6"),
    "BAIXO": PatternFill("solid", fgColor="E7E7ED"),
}

STATUS_FONTS = {
    "STRONG_INDICATION": Font(name="Inter", size=10, color="B42318", bold=True),
    "REVIEW_REQUIRED": Font(name="Inter", size=10, color="B54708", bold=True),
    "INCONCLUSIVE": Font(name="Inter", size=10, color="475467", bold=True),
    "NO_INDICATION": Font(name="Inter", size=10, color="067647", bold=True),
    "CRÍTICO": Font(name="Inter", size=10, color="912018", bold=True),
    "ALTO": Font(name="Inter", size=10, color="B42318", bold=True),
    "MÉDIO": Font(name="Inter", size=10, color="B54708", bold=True),
    "BAIXO": Font(name="Inter", size=10, color="475467", bold=True),
}

THIN_BORDER = Border(
    left=Side(style="thin", color="E7E7ED"),
    right=Side(style="thin", color="E7E7ED"),
    top=Side(style="thin", color="E7E7ED"),
    bottom=Side(style="thin", color="E7E7ED"),
)

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
    ws.row_dimensions[1].height = 28
    for c in ws[1]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="left")
        c.border = THIN_BORDER
    for row in rows:
        ws.append([_cell(v) for v in row])
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.font = DATA_FONT
            c.border = THIN_BORDER
            c.alignment = Alignment(wrap_text=True, vertical="top")
        if fill_col is not None:
            val = str(r[fill_col].value)
            fill = FILLS.get(val)
            if fill:
                r[fill_col].fill = fill
            s_font = STATUS_FONTS.get(val)
            if s_font:
                r[fill_col].font = s_font
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


GLOSSARY: list[list[str]] = [
    ["NO_INDICATION", "Sem indício de confusão com a instituição no material coletado (não atesta conformidade)"],
    ["REVIEW_REQUIRED", "Requer análise humana: indícios de ambiguidade ou de apresentação como canal da instituição"],
    ["STRONG_INDICATION", "Forte indício de que o consumidor pode crer tratar diretamente com a instituição"],
    ["INCONCLUSIVE", "Evidência insuficiente (coleta falhou, conteúdo bloqueado ou contraditório)"],
    ["A_LEGITIMATE_COMMERCIAL_MENTION", "Cenário A — menção comercial com condição de correspondente clara"],
    ["B_AMBIGUOUS_COMMUNICATION", "Cenário B — uso destacado da marca sem transparência suficiente"],
    ["C_POSSIBLE_OFFICIAL_CHANNEL", "Cenário C — possível apresentação como canal oficial da instituição"],
    ["D_STRONG_IMPERSONATION", "Cenário D — forte indício de impersonação"],
    ["E_INSUFFICIENT_EVIDENCE", "Cenário E — evidência insuficiente para avaliar a comunicação"],
    ["NOT_RELEVANT", "A página não usa a marca de forma relevante"],
    ["CONFIRMED_COBAN", "CNPJ do responsável consta de cadastro autoritativo de correspondentes"],
    ["POSSIBLE_COBAN", "Indícios de vínculo (declaração, nome) sem confirmação em cadastro autoritativo"],
    ["OTHER_THIRD_PARTY", "Empresa identificada sem indício de relação de correspondente"],
    ["UNKNOWN_IDENTITY", "Responsável pela página não identificado"],
    ["HYPOTHESIS_OF_NONCONFORMITY", "Hipótese de não conformidade (correspondente confirmado) — exige revisão"],
    ["HYPOTHESIS_CONDITIONAL_ON_LINK", "Hipótese aplicável somente se confirmado o vínculo de correspondente"],
    ["NOT_APPLICABLE_UNLESS_COBAN", "Responsável não identificado como correspondente; possível tema de marca/fraude"],
    ["NO_INDICATION_OBSERVED", "Nenhum indício observado para o requisito no material coletado"],
    ["CANNOT_ASSESS", "Não avaliável com a evidência disponível"],
    ["PENDING / NOT_REQUIRED", "Status da revisão humana (registrar a decisão da auditoria nos papéis de trabalho)"],
    ["E-…", "Referências ao catálogo de evidências (ver aba Evidence Index e dossiês)"],
    [
        "Confiança",
        "Extração = leitura das evidências; atribuição = vínculo página→responsável. Nenhuma é "
        "probabilidade de irregularidade.",
    ],
]


LEGACY_LOGO_SHEET = "Logo antiga"
_CROP_ROW_HEIGHT = 64


def _legacy_logo_sheet(
    wb: Workbook, records: list[CandidateRecord], dossier_paths: dict[str, str], crops: dict[str, str]
) -> None:
    """One row per page where the discontinued logo was found (model and/or image comparison)."""
    rows: list[list[Any]] = []
    images: list[tuple[int, str]] = []
    for r in records:
        f, b, idn = r.final, r.brand, r.identity
        if f is None or not f.legacy_visual_identity:
            continue
        matches = b.visual_matches if b else []
        best = max(matches, key=lambda m: m.score) if matches else None
        reg = idn.registry_match.record if idn and idn.registry_match else {}
        rows.append(
            [
                "",
                r.candidate.registrable_domain,
                r.candidate.url,
                f.classification.value,
                f.priority.value,
                f.legacy_visual_identity_source,
                "sim" if b and b.legacy_logo_exempt else "não",
                best.score if best else "",
                (f"{best.screenshot_ref} x={best.bbox[0]} y={best.bbox[1]} {best.bbox[2]}x{best.bbox[3]}px")
                if best
                else "",
                ("sim" if best.above_fold else "não") if best else "",
                "\n".join(dict.fromkeys(m.image_src for m in matches if m.image_src)),
                idn.classification.value if idn else "",
                f"{reg.get('nome', '')} ({reg.get('cnpj', '')})" if reg else "",
                crops.get(r.candidate.id, ""),
                dossier_paths.get(r.candidate.id, ""),
            ]
        )
        if r.candidate.id in crops:
            images.append((len(rows) + 1, crops[r.candidate.id]))
    headers = [
        "Recorte",
        "Domínio",
        "URL",
        "Classificação",
        "Prioridade",
        "Origem da detecção",
        "Site de referência (não eleva)",
        "Similaridade (comparação de imagem)",
        "Onde (print e posição)",
        "Acima da dobra",
        "Imagem na página",
        "Identidade atribuída",
        "Cadastro de correspondentes",
        "Arquivo do recorte",
        "Dossiê",
    ]
    _sheet(wb, LEGACY_LOGO_SHEET, headers, rows, {1: 24, 3: 60, 6: 30, 9: 34, 11: 60, 13: 40, 14: 50}, fill_col=3)
    ws = wb[LEGACY_LOGO_SHEET]
    for row, crop in images:
        try:
            img = OpenpyxlImage(crop)
            scale = min(160 / img.width, (_CROP_ROW_HEIGHT * 1.3) / img.height, 1.0)
            img.width, img.height = int(img.width * scale), int(img.height * scale)
            ws.row_dimensions[row].height = _CROP_ROW_HEIGHT
            ws.add_image(img, f"A{row}")
        except Exception:  # noqa: S112 - a missing/broken crop must not break the report
            continue


THREAT_SHEET = "Ameaças"


DOMAIN_SHEET = "Domínios"


def _page_host(r: CandidateRecord) -> str:
    return hostname_of((r.evidence.final_url if r.evidence else None) or r.candidate.url) or r.candidate.hostname


def _domain_sheet(wb: Workbook, records: list[CandidateRecord], intel: dict[str, DomainIntel]) -> None:
    """Registration (RDAP/WHOIS), DNS and hosting of every flagged host, with the takedown contacts."""
    worst: dict[str, tuple[str, str]] = {}
    rank = {"STRONG_INDICATION": 0, "REVIEW_REQUIRED": 1}
    for r in records:
        if r.final is None:
            continue
        host = _page_host(r)
        cur = (r.final.classification.value, r.final.priority.value)
        if host in intel and (host not in worst or rank.get(cur[0], 9) < rank.get(worst[host][0], 9)):
            worst[host] = cur
    rows = []
    for host, item in sorted(intel.items(), key=lambda kv: (rank.get(worst.get(kv[0], ("",))[0], 9), kv[0])):
        s = summarize_intel(item)
        cls, prio = worst.get(host, ("", ""))
        rows.append(
            [host, item.registered_domain, cls, prio, s["platform"], s["created"], s["registrar"], s["holder"],
             s["resolves"], ", ".join(item.a[:4]), s["hosting"], s["ns"], s["mx"], s["abuse"], s["notes"]]
        )
    headers = [
        "Host", "Domínio registrado", "Classificação", "Prioridade", "Plataforma (dono só via denúncia)",
        "Criado em (idade)", "Registrador", "Titular", "Resolve hoje", "IPs", "Hospedagem (rede do IP)",
        "Servidores DNS (NS)", "E-mail (MX)", "Contatos de abuso (takedown)", "Observações",
    ]
    _sheet(wb, DOMAIN_SHEET, headers, rows, {1: 40, 2: 26, 8: 40, 11: 40, 12: 36, 14: 44, 15: 70}, fill_col=2)


def threat_rows(
    records: list[CandidateRecord],
    brand: BrandProfile,
    focus: str | None,
    intel: dict[str, DomainIntel] | None = None,
) -> list[tuple[ThreatView, CandidateRecord, DomainIntel | None]]:
    """Pages in the threat view, most dangerous first. In a threat campaign every level is listed; in
    other campaigns only CRÍTICO/ALTO pages and pages asking for credentials."""
    out = []
    for r in records:
        if r.final is None:
            continue
        item = (intel or {}).get(_page_host(r))
        t = triage(r, brand, item)
        keep = t.is_threat if focus == FOCUS_THREAT else (t.level in ("CRÍTICO", "ALTO") or bool(
            r.final.sensitive_data_requested and t.is_threat))
        if keep:
            out.append((t, r, item))
    out.sort(key=lambda x: (-x[0].score, x[1].candidate.url))
    return out


def _threat_sheet(
    wb: Workbook,
    rows_in: list[tuple[ThreatView, CandidateRecord, DomainIntel | None]],
    dossier_paths: dict[str, str],
) -> bool:
    """Threat-intelligence view: level, category and the facts behind it, with the IOCs and takedown data."""
    rows: list[list[Any]] = []
    for t, r, item in rows_in:
        f, ev = r.final, r.evidence
        assert f is not None
        final_url = (ev.final_url if ev else None) or r.candidate.url
        action = next((i.content.split(": ", 1)[-1] for i in r.catalog if i.ref == "E-FORM-ACTION"), "")
        di = summarize_intel(item) if item else {}
        registration = " · ".join(x for x in (di.get("created"), di.get("registrar"), di.get("holder")) if x)
        rows.append(
            [
                t.level,
                t.score,
                "; ".join(t.categories),
                "\n".join(t.facts),
                final_url,
                r.candidate.registrable_domain,
                di.get("platform", ""),
                registration,
                di.get("resolves", ""),
                di.get("hosting", "") or ((ev.server_ip or "") if ev else ""),
                di.get("abuse", ""),
                ", ".join(f.sensitive_data_requested),
                action,
                "\n".join(ev.redirect_chain[:6]) if ev else "",
                f.classification.value,
                f.case.value,
                (f.justification or "")[:600],
                ", ".join(sorted({h.source for h in r.hits})),
                "\n".join(sorted({h.hypothesis_id for h in r.hits if h.hypothesis_id}))[:500],
                dossier_paths.get(r.candidate.id, ""),
            ]
        )
    if not rows:
        return False
    headers = [
        "Nível de ameaça",
        "Pontos",
        "Categoria",
        "Fatos que levaram ao nível",
        "URL",
        "Domínio",
        "Plataforma (dono só via denúncia)",
        "Registro do domínio (criação · registrador · titular)",
        "Resolve hoje",
        "Hospedagem",
        "Contatos de abuso (takedown)",
        "Dados sensíveis pedidos",
        "Formulário envia para",
        "Redirecionamentos",
        "Classificação SIMIO (conformidade)",
        "Caso (analisador)",
        "Justificativa do analisador",
        "Fontes",
        "Hipóteses",
        "Dossiê",
    ]
    widths = {3: 34, 4: 60, 5: 50, 6: 26, 8: 44, 10: 36, 11: 40, 14: 40, 17: 70, 19: 34, 20: 40}
    _sheet(wb, THREAT_SHEET, headers, rows, widths, fill_col=0)
    return True


def write_workbook(
    path: Path,
    storage: Storage,
    campaign_id: str,
    records: list[CandidateRecord],
    dossier_paths: dict[str, str],
    logo_crops: dict[str, str] | None = None,
    domain_intel: dict[str, DomainIntel] | None = None,
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

    # Add Banco BV logo to Executive Summary if available
    ws_exec = wb["Executive Summary"]
    logo_path = Path("design-system/assets/bv-logo-blue.png")
    if logo_path.exists():
        try:
            img = OpenpyxlImage(str(logo_path))
            img.width = 120
            img.height = 89
            ws_exec.add_image(img, "D2")
        except Exception:
            pass

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
        "Identidade visual antiga (logo descontinuada)",
        "Dados sensíveis pedidos (senha, cartão, token...)",
        "Cenário",
        "Confiança da extração",
        "Suficiência da evidência",
        "Encaminhamento sugerido",
        "Mapeamento normativo (hipóteses — não conclusivo)",
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
        "Score pré-coleta (ordem da fila de descoberta)",
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
                (f"SIM ({f.legacy_visual_identity_source})" if f.legacy_visual_identity else "não") if f else "",
                ", ".join(f.sensitive_data_requested) if f else "",
                f.case.value if f else "",
                f.extraction_confidence.value if f else "",
                f.evidence_sufficiency.value if f else "",
                "\n".join(f.routing) if f else "",
                "\n".join(f.compliance_summary) if f else "",
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
    widths = {1: 13, 2: 24, 3: 40, 4: 40, 6: 40, 16: 34, 17: 40, 18: 60, 19: 60, 20: 50}
    _sheet(wb, "Candidates", cand_headers, rows, widths, fill_col=9)
    strong = [
        row
        for row, r in zip(rows, records, strict=True)
        if r.final and r.final.classification.value == "STRONG_INDICATION"
    ]
    _sheet(wb, "Strong Indications", cand_headers, strong, widths, fill_col=9)
    if any(r.final and r.final.legacy_visual_identity for r in records):
        _legacy_logo_sheet(wb, records, dossier_paths, logo_crops or {})
        wb.move_sheet(LEGACY_LOGO_SHEET, offset=-(len(wb.sheetnames) - 2))
    focus = (camp.get("params") or {}).get("focus")
    brand_profile = load_brand(camp.get("brand") or "bv")
    # In a threat campaign the threat view comes first; in the COBAN sweep it follows the compliance sheets.
    threat_first = focus == FOCUS_THREAT
    if _threat_sheet(wb, threat_rows(records, brand_profile, focus, domain_intel), dossier_paths) and threat_first:
        wb.move_sheet(THREAT_SHEET, offset=-(len(wb.sheetnames) - 2))
    if domain_intel:
        _domain_sheet(wb, records, domain_intel)
        if threat_first:
            wb.move_sheet(DOMAIN_SHEET, offset=-(len(wb.sheetnames) - 3))

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
    _sheet(wb, "Glossário", ["Termo", "Significado"], GLOSSARY, {1: 36, 2: 120})

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
