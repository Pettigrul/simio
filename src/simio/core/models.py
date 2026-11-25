"""Typed data contracts shared by all agents (validated with Pydantic)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------
class CandidateStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    COLLECTING = "COLLECTING"
    COLLECTED = "COLLECTED"
    ANALYZING = "ANALYZING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class FinalClassification(StrEnum):
    NO_INDICATION = "NO_INDICATION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    STRONG_INDICATION = "STRONG_INDICATION"
    INCONCLUSIVE = "INCONCLUSIVE"


CLASSIFICATION_RANK = {
    FinalClassification.NO_INDICATION: 0,
    FinalClassification.INCONCLUSIVE: 1,
    FinalClassification.REVIEW_REQUIRED: 2,
    FinalClassification.STRONG_INDICATION: 3,
}


class Priority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


PRIORITY_RANK = {Priority.LOW: 0, Priority.MEDIUM: 1, Priority.HIGH: 2}


class Level(StrEnum):
    """Qualitative confidence level. NOT a probability of irregularity."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class ScenarioCase(StrEnum):
    A_LEGITIMATE_COMMERCIAL_MENTION = "A_LEGITIMATE_COMMERCIAL_MENTION"
    B_AMBIGUOUS_COMMUNICATION = "B_AMBIGUOUS_COMMUNICATION"
    C_POSSIBLE_OFFICIAL_CHANNEL = "C_POSSIBLE_OFFICIAL_CHANNEL"
    D_STRONG_IMPERSONATION = "D_STRONG_IMPERSONATION"
    E_INSUFFICIENT_EVIDENCE = "E_INSUFFICIENT_EVIDENCE"
    NOT_RELEVANT = "NOT_RELEVANT"  # brand not observed at all


class EvidenceSufficiency(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class IdentityClass(StrEnum):
    CONFIRMED_COBAN = "CONFIRMED_COBAN"
    POSSIBLE_COBAN = "POSSIBLE_COBAN"
    OTHER_THIRD_PARTY = "OTHER_THIRD_PARTY"
    UNKNOWN_IDENTITY = "UNKNOWN_IDENTITY"


class Dimension(StrEnum):
    VISUAL_IDENTITY = "VISUAL_IDENTITY"
    DECLARED_COMMERCIAL_IDENTITY = "DECLARED_COMMERCIAL_IDENTITY"
    CORRESPONDENT_TRANSPARENCY = "CORRESPONDENT_TRANSPARENCY"
    INSTITUTIONAL_LANGUAGE = "INSTITUTIONAL_LANGUAGE"
    LOGO_USAGE = "LOGO_USAGE"
    SERVICE_JOURNEY = "SERVICE_JOURNEY"
    CONTACT_CHANNELS = "CONTACT_CHANNELS"
    CONSUMER_CONFUSION = "CONSUMER_CONFUSION"


class Signal(StrEnum):
    CONFUSION = "CONFUSION"
    LEGITIMATE = "LEGITIMATE"
    NEUTRAL = "NEUTRAL"
    NOT_ASSESSED = "NOT_ASSESSED"


class CollectionStatus(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"  # page loaded but something (e.g. full screenshot) failed
    FAILED = "FAILED"  # could not load the page (timeout, DNS, HTTP error...)
    BLOCKED = "BLOCKED"  # refused by the URL security policy
    NOT_COLLECTED = "NOT_COLLECTED"


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------
class SearchHypothesis(Strict):
    id: str
    description: str
    grounding_prompt: str
    keyword_queries: list[str] = Field(default_factory=list)


class DiscoveryHit(Strict):
    """One URL observed by one discovery source. Never invented: always from source data."""

    source: str
    original_url: str
    query: str | None = None
    hypothesis_id: str | None = None
    title: str | None = None
    snippet: str | None = None
    rank: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime = Field(default_factory=utcnow)


class QueryRecord(Strict):
    source: str
    query: str
    hypothesis_id: str | None = None
    status: str = "OK"  # OK | ERROR | EMPTY | SKIPPED
    result_count: int = 0
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    executed_at: datetime = Field(default_factory=utcnow)


class DiscoveryOutcome(Strict):
    hits: list[DiscoveryHit] = Field(default_factory=list)
    queries: list[QueryRecord] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    sources_used: list[str] = Field(default_factory=list)


class Candidate(Strict):
    id: str
    campaign_id: str
    url: str
    normalized_url: str
    hostname: str
    registrable_domain: str
    status: CandidateStatus = CandidateStatus.DISCOVERED
    priority_score: float = 0.0
    priority_reasons: list[str] = Field(default_factory=list)
    attempts: int = 0
    recollects: int = 0
    last_error: str | None = None


# --------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------
class Artifact(Strict):
    kind: str  # screenshot_viewport | screenshot_full | html | text | page_data
    path: str  # relative to the campaign directory
    sha256: str
    size_bytes: int


class EvidenceItem(Strict):
    """An addressable piece of evidence the analysts must cite (e.g. ``E-TXT-003``)."""

    ref: str
    kind: str  # title | meta | text | heading | nav | cta | link | contact | image | screenshot | domain | network
    location: str  # e.g. "viewport", "header", "footer", "page", "url"
    content: str
    above_fold: bool | None = None


class LinkInfo(Strict):
    text: str
    href: str
    location: str = "page"


class ImageInfo(Strict):
    alt: str
    src: str
    location: str = "page"
    above_fold: bool = False
    width: int = 0
    height: int = 0


class PageData(Strict):
    title: str = ""
    meta: dict[str, str] = Field(default_factory=dict)
    canonical: str | None = None
    lang: str | None = None
    headings: list[str] = Field(default_factory=list)
    nav_items: list[LinkInfo] = Field(default_factory=list)
    ctas: list[LinkInfo] = Field(default_factory=list)
    contact_links: list[LinkInfo] = Field(default_factory=list)
    links: list[LinkInfo] = Field(default_factory=list)
    images: list[ImageInfo] = Field(default_factory=list)
    header_text: str = ""
    footer_text: str = ""
    above_fold_text: str = ""
    visible_text: str = ""
    forms_present: int = 0


class Evidence(Strict):
    candidate_id: str
    attempt: int
    status: CollectionStatus
    original_url: str
    final_url: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    http_status: int | None = None
    server_ip: str | None = None
    collected_at: datetime = Field(default_factory=utcnow)
    page: PageData | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    blocked_requests: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    not_inspected: list[str] = Field(default_factory=list)
    retryable: bool = False
    manifest_path: str | None = None
    manifest_sha256: str | None = None

    @property
    def usable(self) -> bool:
        return self.status in (CollectionStatus.OK, CollectionStatus.PARTIAL) and self.page is not None

    def artifact(self, kind: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.kind == kind), None)


# --------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------
class IdentityFact(Strict):
    """Something literally observed in the evidence."""

    kind: str  # cnpj | legal_name | trade_name | phone | email | address | social_profile |
    # partnership_declaration | correspondent_declaration | domain | whatsapp
    value: str
    evidence_ref: str | None = None
    context: str | None = None
    valid: bool | None = None  # e.g. CNPJ check digits


class RegistryMatch(Strict):
    source: str
    matched_on: str  # cnpj | cnpj_root | name
    record: dict[str, str]
    authoritative: bool


class IdentityAttribution(Strict):
    candidate_id: str
    classification: IdentityClass
    entity_key: str
    display_name: str | None = None
    owner_cnpjs: list[str] = Field(default_factory=list)
    referenced_institution_cnpjs: list[str] = Field(default_factory=list)
    facts: list[IdentityFact] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    registry_match: RegistryMatch | None = None
    attribution_confidence: Level = Level.NONE
    limitations: list[str] = Field(default_factory=list)
    rationale: str = ""


# --------------------------------------------------------------------------------------
# Brand analysis (LLM contract + metadata)
# --------------------------------------------------------------------------------------
class Indicator(Strict):
    dimension: Dimension
    description: str
    evidence_refs: list[str] = Field(default_factory=list)
    strength: Level = Level.MEDIUM


class DimensionAssessment(Strict):
    dimension: Dimension
    signal: Signal
    finding: str
    evidence_refs: list[str] = Field(default_factory=list)


class BrandAnalysisOutput(Strict):
    """Schema the multimodal model must return (validated before use)."""

    case: ScenarioCase
    classification: FinalClassification
    priority: Priority
    dimensions: list[DimensionAssessment] = Field(default_factory=list)
    brand_confusion_indicators: list[Indicator] = Field(default_factory=list)
    legitimate_relationship_indicators: list[Indicator] = Field(default_factory=list)
    observed_facts: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""
    requires_human_review: bool = True
    extraction_confidence: Level = Level.MEDIUM
    evidence_sufficiency: EvidenceSufficiency = EvidenceSufficiency.PARTIAL
    possible_prompt_injection: bool = False


class BrandAssessment(BrandAnalysisOutput):
    analyzer: str  # model id or "heuristic-v1"
    prompt_version: str | None = None
    dropped_references: list[str] = Field(default_factory=list)
    guardrail_notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Compliance mapping
# --------------------------------------------------------------------------------------
class RequirementSource(StrEnum):
    NORMATIVE = "NORMATIVE"
    CONTRACTUAL = "CONTRACTUAL"
    INTERNAL_POLICY = "INTERNAL_POLICY"


class FindingStatus(StrEnum):
    HYPOTHESIS_OF_NONCONFORMITY = "HYPOTHESIS_OF_NONCONFORMITY"
    # The page *declares* or suggests a correspondent link that is not confirmed in an authoritative
    # registry: the hypothesis only applies if the link is confirmed.
    HYPOTHESIS_CONDITIONAL_ON_LINK = "HYPOTHESIS_CONDITIONAL_ON_LINK"
    NO_INDICATION_OBSERVED = "NO_INDICATION_OBSERVED"
    CANNOT_ASSESS = "CANNOT_ASSESS"
    NOT_APPLICABLE_UNLESS_COBAN = "NOT_APPLICABLE_UNLESS_COBAN"


class ComplianceFinding(Strict):
    requirement_id: str
    requirement_source: RequirementSource
    citation: str
    requirement_text: str
    status: FindingStatus
    observed_evidence: list[str] = Field(default_factory=list)  # evidence refs
    hypothesis: str = ""
    missing_information: list[str] = Field(default_factory=list)
    requires_human_review: bool = True


class ComplianceAssessment(Strict):
    findings: list[ComplianceFinding] = Field(default_factory=list)
    out_of_scope_notes: list[str] = Field(default_factory=list)
    related_controls: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "Mapeamento indicativo para apoio à auditoria. Não constitui conclusão regulatória; "
        "a decisão definitiva pertence à auditoria."
    )


# --------------------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------------------
class ReviewDecision(StrEnum):
    CONFIRM = "CONFIRM"
    DOWNGRADE = "DOWNGRADE"
    RECOLLECT = "RECOLLECT"
    INCONCLUSIVE = "INCONCLUSIVE"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class Challenge(Strict):
    topic: str  # domain_attribution | evidence_sufficiency | over_interpretation | ...
    concern: str
    evidence_refs: list[str] = Field(default_factory=list)
    severity: Level = Level.MEDIUM


class ReviewOutput(Strict):
    decision: ReviewDecision
    revised_classification: FinalClassification
    revised_priority: Priority
    challenges: list[Challenge] = Field(default_factory=list)
    contradictory_evidence: list[str] = Field(default_factory=list)
    recollect_reason: str | None = None
    reasoning_summary: str = ""
    requires_human_review: bool = True


class Review(ReviewOutput):
    reviewer: str
    prompt_version: str | None = None


# --------------------------------------------------------------------------------------
# Final decision
# --------------------------------------------------------------------------------------
class FinalDecision(Strict):
    candidate_id: str
    classification: FinalClassification
    priority: Priority
    case: ScenarioCase
    identity_class: IdentityClass
    extraction_confidence: Level
    attribution_confidence: Level
    evidence_sufficiency: EvidenceSufficiency
    requires_human_review: bool
    human_review_status: str = "PENDING"
    justification: str
    key_evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    guardrail_notes: list[str] = Field(default_factory=list)
    routing: list[str] = Field(default_factory=list)  # suggested owner(s) of the follow-up
    compliance_summary: list[str] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=utcnow)
