"""Compliance Analyst Agent: maps observed indications to requirements.

Deterministic by design so it cannot invent articles: it only cites requirements present in the
versioned catalog (``knowledge/cmn_4935.toml``, transcribed from the official text) and, when
supplied, an operator-provided contractual/internal requirements file. It separates requirement,
observed evidence, non-conformity *hypothesis*, missing information and need for human review.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from simio.core.models import (
    BrandAssessment,
    ComplianceAssessment,
    ComplianceFinding,
    Dimension,
    FinalClassification,
    FindingStatus,
    IdentityAttribution,
    IdentityClass,
    RequirementSource,
    Signal,
)

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


@dataclass
class Requirement:
    id: str
    citation: str
    text: str
    source: RequirementSource
    dimensions: list[Dimension]
    hypothesis: str
    addressed_to: str = ""


@dataclass
class RequirementCatalog:
    requirements: list[Requirement] = field(default_factory=list)
    controls: list[dict[str, str]] = field(default_factory=list)
    normative_source: str = ""
    version_note: str = ""
    contractual_loaded: bool = False

    @classmethod
    def load(cls, contract_path: Path | None = None) -> RequirementCatalog:
        with (KNOWLEDGE_DIR / "cmn_4935.toml").open("rb") as fh:
            data = tomllib.load(fh)
        cat = cls(normative_source=data["source"], version_note=data.get("version_note", ""))
        for r in data.get("requirement", []):
            cat.requirements.append(
                Requirement(
                    id=r["id"],
                    citation=r["citation"],
                    text=r["text"].strip(),
                    source=RequirementSource.NORMATIVE,
                    dimensions=[Dimension(d) for d in r["dimensions"]],
                    hypothesis=r["hypothesis"],
                    addressed_to=r.get("addressed_to", ""),
                )
            )
        cat.controls = [
            {"id": c["id"], "citation": c["citation"], "text": c["text"].strip()} for c in data.get("control", [])
        ]
        if contract_path is not None:
            with contract_path.open("rb") as fh:
                cdata = tomllib.load(fh)
            for r in cdata.get("requirement", []):
                src = RequirementSource(r.get("source", "CONTRACTUAL"))
                cat.requirements.append(
                    Requirement(
                        id=r["id"],
                        citation=r["citation"],
                        text=r["text"].strip(),
                        source=src,
                        dimensions=[Dimension(d) for d in r["dimensions"]],
                        hypothesis=r.get("hypothesis", "Possível descumprimento de requisito contratual"),
                        addressed_to=r.get("addressed_to", ""),
                    )
                )
            cat.contractual_loaded = True
        return cat


class ComplianceAnalyst:
    def __init__(self, catalog: RequirementCatalog) -> None:
        self.catalog = catalog

    def assess(
        self, brand: BrandAssessment, identity: IdentityAttribution, observed_ouvidoria: bool
    ) -> ComplianceAssessment:
        out = ComplianceAssessment()
        if not self.catalog.contractual_loaded:
            out.limitations.append(
                "requisitos contratuais/internos não fornecidos: mapeamento limitado ao texto normativo"
            )
        dims = {d.dimension: d for d in brand.dimensions}
        confusion_refs: dict[Dimension, list[str]] = {}
        for ind in brand.brand_confusion_indicators:
            confusion_refs.setdefault(ind.dimension, []).extend(ind.evidence_refs)
        for d in brand.dimensions:
            if d.signal == Signal.CONFUSION:
                confusion_refs.setdefault(d.dimension, []).extend(d.evidence_refs)

        inconclusive = brand.classification == FinalClassification.INCONCLUSIVE
        is_coban = identity.classification in (IdentityClass.CONFIRMED_COBAN, IdentityClass.POSSIBLE_COBAN)

        for req in self.catalog.requirements:
            hit_dims = [d for d in req.dimensions if d in confusion_refs]
            refs = sorted({r for d in hit_dims for r in confusion_refs[d]})
            missing: list[str] = []
            if inconclusive:
                status = FindingStatus.CANNOT_ASSESS
                missing.append("evidência insuficiente/coleta incompleta")
                hypothesis = ""
            elif not hit_dims:
                legit = [d for d in req.dimensions if dims.get(d) and dims[d].signal == Signal.LEGITIMATE]
                status = FindingStatus.NO_INDICATION_OBSERVED
                refs = sorted({r for d in legit for r in dims[d].evidence_refs})
                hypothesis = ""
            elif req.source == RequirementSource.NORMATIVE and not is_coban:
                status = FindingStatus.NOT_APPLICABLE_UNLESS_COBAN
                hypothesis = (
                    f"{req.hypothesis}. A norma disciplina correspondentes contratados; o "
                    "responsável não foi identificado como correspondente."
                )
                missing.append(
                    "confirmar se o responsável é correspondente contratado "
                    "(relação pública da contratante — art. 21 — ou base interna)"
                )
            else:
                status = FindingStatus.HYPOTHESIS_OF_NONCONFORMITY
                hypothesis = req.hypothesis
                if identity.classification == IdentityClass.POSSIBLE_COBAN:
                    missing.append("vínculo de correspondente não confirmado em cadastro autorizado")
            if (
                req.id == "CMN4935-ART14-III"
                and not observed_ouvidoria
                and status in (FindingStatus.HYPOTHESIS_OF_NONCONFORMITY, FindingStatus.NOT_APPLICABLE_UNLESS_COBAN)
            ):
                missing.append(
                    "canais de contato/ouvidoria da contratante não observados no material "
                    "coletado (podem existir em páginas não inspecionadas)"
                )
            if req.source == RequirementSource.NORMATIVE and status == FindingStatus.HYPOTHESIS_OF_NONCONFORMITY:
                missing.append("contrato de correspondente vigente e suas cláusulas")
            out.findings.append(
                ComplianceFinding(
                    requirement_id=req.id,
                    requirement_source=req.source,
                    citation=req.citation,
                    requirement_text=req.text,
                    status=status,
                    observed_evidence=refs,
                    hypothesis=hypothesis,
                    missing_information=missing,
                    requires_human_review=status != FindingStatus.NO_INDICATION_OBSERVED,
                )
            )

        if any(f.status == FindingStatus.NOT_APPLICABLE_UNLESS_COBAN for f in out.findings):
            out.out_of_scope_notes.append(
                "Se o responsável não for correspondente contratado, os indícios podem caracterizar uso "
                "indevido de marca ou fraude, fora do escopo da Res. 4.935: avaliar encaminhamento às "
                "áreas jurídica/segurança."
            )
        if any(f.status == FindingStatus.HYPOTHESIS_OF_NONCONFORMITY for f in out.findings):
            out.related_controls = [f"{c['citation']}: {c['text']}" for c in self.catalog.controls]
        return out
