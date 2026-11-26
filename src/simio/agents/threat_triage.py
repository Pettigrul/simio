"""Threat-intelligence triage of an analysed campaign, without new searches, collection or model calls.

The brand analyst judges pages as a correspondent (COBAN) auditor: shops and partners are its targets
and much of the rest is "out of scope". A threat analyst asks something else: *does this page impersonate
the bank or put its customers at risk?* This module re-reads what is already stored (the model's
scenario case, the page signals, discovery hypotheses, the domain intelligence) and scores each page
with explicit, auditable facts:

* credential harvesting (password/card/token fields on a page presenting the brand);
* impersonation or fake official channel, as the model itself recorded in the scenario case;
* the brand in a non-official domain or a lookalike (typosquatting);
* APK distribution outside the official stores;
* free/ephemeral hosting, recently registered domains;
* boleto / Pix / quitação / 2ª via services offered with the brand.

Legitimate businesses (an identified correspondent or third party communicating as itself) score down:
they are a compliance matter, not a threat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from simio.agents.domain_intel import hosting_platform
from simio.agents.evidence import CREDENTIAL_KINDS
from simio.brands import BrandProfile
from simio.core.models import DomainIntel, FinalClassification, IdentityClass, ScenarioCase
from simio.core.urls import domain_matches, hostname_of

if TYPE_CHECKING:  # reporting imports this module
    from simio.reporting.data import CandidateRecord

LEVELS = ("CRÍTICO", "ALTO", "MÉDIO", "BAIXO", "SEM AMEAÇA")
_APK_HOSTS = ("aptoide.com", "apkpure.com", "apkcombo.com", "uptodown.com", "softonic.com", "apkmirror.com",
              "appbrain.com", "apk.cafe", "apkmody.io", "happymod.com", "apksfull.com")
_OFFICIAL_STORES = ("play.google.com", "apps.apple.com")
_SOCIAL = ("instagram.com", "facebook.com", "tiktok.com", "youtube.com", "x.com", "twitter.com", "kwai.com",
           "threads.net", "linkedin.com")
_MESSAGING = ("t.me", "telegram.me", "wa.me", "whatsapp.com", "chat.whatsapp.com")
_LEGIT_IDENTITIES = (IdentityClass.CONFIRMED_COBAN, IdentityClass.POSSIBLE_COBAN, IdentityClass.OTHER_THIRD_PARTY)
NEW_DOMAIN_DAYS = 180


@dataclass
class ThreatView:
    score: float
    level: str
    categories: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)

    @property
    def is_threat(self) -> bool:
        return self.level != "SEM AMEAÇA"


def _level(score: float) -> str:
    if score >= 8:
        return "CRÍTICO"
    if score >= 5:
        return "ALTO"
    if score >= 3:
        return "MÉDIO"
    if score >= 2:
        return "BAIXO"
    return "SEM AMEAÇA"


def triage(rec: CandidateRecord, brand: BrandProfile, intel: DomainIntel | None = None) -> ThreatView:
    f = rec.final
    if f is None:
        return ThreatView(0, "SEM AMEAÇA", facts=["não analisado"])
    ev = rec.evidence
    url = (ev.final_url if ev else None) or rec.candidate.url
    host = hostname_of(url) or rec.candidate.hostname
    page = ev.page if ev and ev.page else None
    text = " ".join(
        filter(None, [page.title if page else "", (page.visible_text if page else "")[:20000]]
               + [f"{h.title or ''} {h.snippet or ''}" for h in rec.hits])
    )
    hyps = {h.hypothesis_id or "" for h in rec.hits}
    score, cats, facts = 0.0, [], []

    def add(points: float, fact: str, category: str | None = None) -> None:
        nonlocal score
        score += points
        facts.append(fact)
        if category and category not in cats:
            cats.append(category)

    title_host = f"{page.title if page else ''} {host} " + " ".join(h.title or "" for h in rec.hits)
    presents_brand = bool(brand.strong_mentions(title_host) or brand.domain_brand_match(host))
    dismissed = f.classification == FinalClassification.NO_INDICATION  # the final verdict (after review)
    creds = [k for k in f.sensitive_data_requested if k in CREDENTIAL_KINDS]
    if creds:
        add(5, f"pede {', '.join(creds)} numa página que se apresenta com a marca", "Coleta de credenciais (phishing)")
    case = f.case
    if dismissed:
        pass  # the reviewer discarded it (homonym, directory, the bank's own channel...): the case does not count
    elif case == ScenarioCase.D_STRONG_IMPERSONATION:
        add(4, "o analisador registrou personificação da instituição", "Personificação da marca")
    elif case == ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL:
        add(3, "o analisador registrou que a página se passa por canal oficial", "Falso canal oficial")
    official = brand.is_official(host)
    lookalike = next((h for h in rec.hits if h.source == "typosquat"), None)
    if lookalike is not None:
        add(2, f"domínio semelhante a {lookalike.metadata.get('lookalike_of')} ({lookalike.metadata.get('fuzzer')})",
            "Domínio semelhante (typosquatting)")
    elif not official and brand.domain_brand_match(host) and not domain_matches(host, _SOCIAL + _MESSAGING):
        add(2, f"a marca está no nome do domínio ({host})", "Domínio com a marca")
    if (lookalike is not None or brand.domain_brand_match(host)) and not dismissed and brand.mentions(text):
        add(1, "o domínio com a marca tem conteúdo que cita a instituição")
    is_apk = domain_matches(host, _APK_HOSTS) or url.lower().split("?")[0].endswith(".apk") or "apk" in host
    if is_apk and presents_brand and not dismissed:
        add(3, "distribui aplicativo (APK) com a marca fora das lojas oficiais", "App falso / APK")
    platform = hosting_platform(host)
    if platform and not is_apk and (presents_brand or case in (
        ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL, ScenarioCase.D_STRONG_IMPERSONATION
    )) and not dismissed:
        add(2, f"página com a marca em plataforma gratuita/de terceiros ({platform})", "Hospedagem gratuita")
    if intel and intel.rdap and intel.rdap.age_days is not None and intel.rdap.age_days <= NEW_DOMAIN_DAYS:
        add(2, f"domínio registrado há {intel.rdap.age_days} dias", "Domínio recente")
    services = brand.sensitive_service_mentions(text) if presents_brand and not dismissed else []
    if services:
        add(2, f"oferece {', '.join(sorted(set(services))[:3])} citando a marca", "Boleto / quitação / atendimento")
    if domain_matches(host, _MESSAGING):
        add(1, "contato direto por mensageiro (WhatsApp/Telegram)", "Canal de mensagens")
        if "H14-threat:paineis-dados" in hyps or "H6-platform:Telegram-grupos" in hyps:
            cats.append("Painel / venda de dados")
    if domain_matches(host, _SOCIAL) and case in (ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL,
                                                  ScenarioCase.D_STRONG_IMPERSONATION):
        cats.append("Perfil/post falso em rede social")
    if any(h.startswith("H10-fraud") for h in hyps) and case in (
        ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL, ScenarioCase.D_STRONG_IMPERSONATION
    ):
        cats.append("Golpe com a marca como isca")

    if f.classification == FinalClassification.STRONG_INDICATION:
        add(2, "indício forte na análise SIMIO")
    elif f.classification == FinalClassification.REVIEW_REQUIRED and score > 0:
        add(1, "marcado para revisão na análise SIMIO")

    idn = f.identity_class
    if idn in _LEGIT_IDENTITIES and case in (
        ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION, ScenarioCase.B_AMBIGUOUS_COMMUNICATION
    ) and not creds:
        add(-4, "empresa identificada que se comunica com a própria marca (tema de conformidade, não ameaça)")
    if case == ScenarioCase.NOT_RELEVANT:
        add(-3, "o analisador considerou a página sem relação com a instituição (ex.: homônimo)")
        score = min(score, 3.0)
    elif case == ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION:
        add(-2, "menção comercial legítima")
    if official or domain_matches(host, _OFFICIAL_STORES):
        score = min(score, 0.0)
        facts.append("domínio/loja oficial")
    if domain_matches(host, brand.homonym_domains):
        score = min(score, 0.0)
        facts.append("homônimo conhecido (outra organização com a mesma sigla)")
    holder = intel.rdap if intel and intel.rdap and not intel.platform else None
    if holder and (
        (holder.holder_document and holder.holder_document in brand.institution_cnpjs)
        or (holder.holder and brand.is_institution_name(holder.holder))
    ):
        score = min(score, 0.0)
        facts.append(f"domínio registrado pelo próprio grupo ({holder.holder}): registro defensivo")
    if intel and not (intel.a or intel.aaaa or intel.cname) and score > 0:
        facts.append("fora do ar hoje (não resolve)")
    return ThreatView(round(score, 1), _level(score), cats, facts)
