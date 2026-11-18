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


_HYPOTHESES = (
    FindingStatus.HYPOTHESIS_OF_NONCONFORMITY,
    FindingStatus.HYPOTHESIS_CONDITIONAL_ON_LINK,
    FindingStatus.NOT_APPLICABLE_UNLESS_COBAN,
)


class ComplianceAnalyst:
    def __init__(self, catalog: RequirementCatalog) -> None:
        self.catalog = catalog

    @staticmethod
    def _hypothesis_text(req: Requirement, hit_dims: list[Dimension], disclosure_seen: bool) -> str:
        """Hypothesis wording that matches what was actually observed."""
        if req.id == "CMN4935-ART14-III" and disclosure_seen:
            drivers = [d.value for d in hit_dims if d != Dimension.CORRESPONDENT_TRANSPARENCY]
            return (
                "Há declaração da condição de correspondente/prestador, mas outros elementos da página "
                f"({', '.join(drivers) or 'apresentação da marca'}) podem contradizê-la ou reduzir seu efeito "
                "(destaque, legibilidade e identificação da contratante a verificar)"
            )
        return req.hypothesis

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
        confirmed = identity.classification == IdentityClass.CONFIRMED_COBAN
        possible = identity.classification == IdentityClass.POSSIBLE_COBAN
        transparency = dims.get(Dimension.CORRESPONDENT_TRANSPARENCY)
        disclosure_seen = any(
            i.dimension == Dimension.CORRESPONDENT_TRANSPARENCY for i in brand.legitimate_relationship_indicators
        ) or (transparency is not None and transparency.signal in (Signal.LEGITIMATE, Signal.NEUTRAL))

        for req in self.catalog.requirements:
            hit_dims = [d for d in req.dimensions if d in confusion_refs]
            refs = sorted({r for d in hit_dims for r in confusion_refs[d]})
            missing: list[str] = []
            hypothesis = self._hypothesis_text(req, hit_dims, disclosure_seen)
            if inconclusive:
                status = FindingStatus.CANNOT_ASSESS
                missing.append("evidência insuficiente/coleta incompleta")
                hypothesis = ""
            elif not hit_dims:
                legit = [d for d in req.dimensions if dims.get(d) and dims[d].signal == Signal.LEGITIMATE]
                status = FindingStatus.NO_INDICATION_OBSERVED
                refs = sorted({r for d in legit for r in dims[d].evidence_refs})
                hypothesis = ""
            elif req.source == RequirementSource.NORMATIVE and confirmed:
                status = FindingStatus.HYPOTHESIS_OF_NONCONFORMITY
            elif req.source == RequirementSource.NORMATIVE and possible:
                status = FindingStatus.HYPOTHESIS_CONDITIONAL_ON_LINK
                hypothesis = f"{hypothesis}. Aplicável somente se confirmado o vínculo de correspondente."
                missing.append(
                    "vínculo de correspondente não confirmado em cadastro autorizado (relação pública da "
                    "contratante — art. 21 — ou base interna)"
                )
            elif req.source == RequirementSource.NORMATIVE:
                status = FindingStatus.NOT_APPLICABLE_UNLESS_COBAN
                hypothesis = (
                    f"{hypothesis}. A norma disciplina correspondentes contratados; o "
                    "responsável não foi identificado como correspondente."
                )
                missing.append(
                    "confirmar se o responsável é correspondente contratado "
                    "(relação pública da contratante — art. 21 — ou base interna)"
                )
            else:
                status = (
                    FindingStatus.HYPOTHESIS_OF_NONCONFORMITY
                    if confirmed
                    else FindingStatus.HYPOTHESIS_CONDITIONAL_ON_LINK
                )
            if req.id == "CMN4935-ART14-III" and not observed_ouvidoria and status in _HYPOTHESES:
                missing.append(
                    "canais de contato/ouvidoria da contratante não observados no material "
                    "coletado (podem existir em páginas não inspecionadas)"
                )
            if req.source == RequirementSource.NORMATIVE and status in (
                FindingStatus.HYPOTHESIS_OF_NONCONFORMITY,
                FindingStatus.HYPOTHESIS_CONDITIONAL_ON_LINK,
            ):
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
        if any(
            f.status in (FindingStatus.HYPOTHESIS_OF_NONCONFORMITY, FindingStatus.HYPOTHESIS_CONDITIONAL_ON_LINK)
            for f in out.findings
        ):
            out.related_controls = [f"{c['citation']}: {c['text']}" for c in self.catalog.controls]
        return out
