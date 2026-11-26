"""Domain intelligence of flagged pages: RDAP (registration/holder/abuse), DNS over HTTPS and hosting."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from simio.agents.domain_intel import (
    DomainIntelCollector,
    hosting_platform,
    parse_domain_rdap,
    parse_ip_rdap,
    summarize,
)


def _vcard(*fields: tuple[str, str]) -> list:
    return ["vcard", [["version", {}, "text", "4.0"], *[[k, {}, "text", v] for k, v in fields]]]


GTLD = {
    "events": [
        {"eventAction": "registration", "eventDate": "2026-11-07T10:00:00Z"},
        {"eventAction": "expiration", "eventDate": "2027-11-07T10:00:00Z"},
    ],
    "status": ["client transfer prohibited"],
    "nameservers": [{"ldhName": "CARL.NS.CLOUDFLARE.COM"}],
    "entities": [
        {
            "roles": ["registrar"],
            "vcardArray": _vcard(("fn", "Sav.com LLC")),
            "entities": [{"roles": ["abuse"], "vcardArray": _vcard(("fn", ""), ("email", "abuse@sav.example"))}],
        },
        {"roles": ["registrant"], "vcardArray": _vcard(("fn", "REDACTED FOR PRIVACY"), ("kind", "org"))},
    ],
}


def test_gtld_registration_age_registrar_and_abuse() -> None:
    r = parse_domain_rdap(GTLD, now=datetime(2026, 11, 25, tzinfo=UTC))
    assert (r.registrar, r.age_days, r.created[:10]) == ("Sav.com LLC", 17, "2026-11-07")
    assert r.abuse_emails == ["abuse@sav.example"]
    assert r.holder is None and "privacidade" in r.holder_kind
    assert r.name_servers == ["carl.ns.cloudflare.com"]


def test_br_company_holder_is_kept_and_natural_person_is_omitted() -> None:
    company = {
        "entities": [
            {
                "roles": ["registrant"],
                "vcardArray": _vcard(("kind", "org"), ("fn", "Loja Exemplo Ltda")),
                "publicIds": [{"type": "cnpj", "identifier": "00.000.000/0001-00"}],
            }
        ]
    }
    r = parse_domain_rdap(company)
    assert (r.holder, r.holder_document, r.holder_kind) == ("Loja Exemplo Ltda", "00.000.000/0001-00", "pessoa jurídica")

    person = {
        "entities": [
            {
                "roles": ["registrant"],
                "vcardArray": _vcard(("kind", "org"), ("fn", "Fulana de Tal")),
                "publicIds": [{"type": "cpf", "identifier": "***.617.106-**"}],
                "entities": [
                    {"roles": ["administrative"], "vcardArray": _vcard(("fn", "Fulana"), ("email", "fulana@x.example"))}
                ],
            }
        ]
    }
    r = parse_domain_rdap(person)
    assert r.holder is None and r.holder_document is None and r.holder_kind.startswith("pessoa física")
    assert "Fulana" not in r.model_dump_json() and "fulana@" not in r.model_dump_json()


def test_ip_network_organisation_and_abuse() -> None:
    data = {
        "name": "VERCEL-01",
        "country": "US",
        "entities": [
            {
                "roles": ["registrant"],
                "vcardArray": _vcard(("fn", "Vercel, Inc"), ("kind", "org")),
                "entities": [{"roles": ["abuse"], "vcardArray": _vcard(("email", "abuse@vercel.example"))}],
            }
        ],
    }
    ip = parse_ip_rdap("76.76.21.21", data)
    assert (ip.network, ip.organisation, ip.abuse_emails) == ("VERCEL-01", "Vercel, Inc", ["abuse@vercel.example"])


def test_platform_tenants_are_recognised() -> None:
    assert hosting_platform("xn--assessoriaboletosj-gsb.weebly.com") == "weebly.com"
    assert hosting_platform("voucherreservas.lovable.app") == "lovable.app"
    assert hosting_platform("facilitabr.com") is None
    assert hosting_platform("www.facilitabr.com") is None


async def test_collector_platform_page_reports_platform_abuse_not_its_registration() -> None:
    rdap_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "doh.example":
            name, rtype = request.url.params["name"], request.url.params["type"]
            answers = {("bv.weebly.com", "A"): [{"type": 1, "data": "74.115.51.8"}]}.get((name, rtype), [])
            return httpx.Response(200, json={"Status": 0, "Answer": answers})
        rdap_paths.append(request.url.path)
        if request.url.path.startswith("/ip/"):
            return httpx.Response(
                429 if len(rdap_paths) == 1 else 200,  # first call rate-limited, then retried
                json={
                    "name": "WEEBLY",
                    "entities": [
                        {
                            "roles": ["registrant"],
                            "vcardArray": _vcard(("fn", "Weebly, Inc."), ("kind", "org")),
                            "entities": [{"roles": ["abuse"], "vcardArray": _vcard(("email", "abuse@weebly.example"))}],
                        }
                    ],
                },
            )
        return httpx.Response(404)

    col = DomainIntelCollector(
        rdap_base="https://rdap.example",
        doh_url="https://doh.example/dns-query",
        transport=httpx.MockTransport(handler),
        backoff_s=0,
        rdap_interval_s=0,
    )
    intel = await col.collect("bv.weebly.com")
    assert intel.platform == "weebly.com" and intel.rdap is None and not intel.errors
    assert not any(p.startswith("/domain/") for p in rdap_paths)  # the platform's registration is irrelevant
    s = summarize(intel)
    assert s["abuse"] == "abuse@weebly.example" and s["created"] == "" and "plataforma" in s["holder"]
    assert s["hosting"] == "Weebly, Inc. (74.115.51.8)" and s["resolves"] == "sim"


async def test_collector_records_a_dead_domain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "doh.example":
            return httpx.Response(200, json={"Status": 3})
        return httpx.Response(200, json=GTLD)

    col = DomainIntelCollector(
        rdap_base="https://rdap.example",
        doh_url="https://doh.example/dns-query",
        transport=httpx.MockTransport(handler),
        rdap_interval_s=0,
    )
    intel = await col.collect("www.golpe-exemplo.com.br")
    assert intel.registered_domain == "golpe-exemplo.com.br" and not intel.ips
    assert any("não resolve" in n for n in intel.notes)
    assert summarize(intel)["registrar"] == "Sav.com LLC"
