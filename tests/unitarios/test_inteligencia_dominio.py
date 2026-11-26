from __future__ import annotations

from datetime import UTC, datetime

import httpx

from simio.agentes.inteligencia_dominio import (
    ColetorInteligenciaDominio,
    interpretar_rdap_dominio,
    interpretar_rdap_ip,
    plataforma_hospedagem,
    resumir,
)


def _vcard(*campos: tuple[str, str]) -> list:
    return ["vcard", [["version", {}, "text", "4.0"], *[[k, {}, "text", v] for k, v in campos]]]


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


def test_gtld_registro_idade_registrador_e_abuso() -> None:
    r = interpretar_rdap_dominio(GTLD, agora=datetime(2026, 11, 25, tzinfo=UTC))
    assert (r.registrador, r.idade_dias, r.criado[:10]) == ("Sav.com LLC", 17, "2026-11-07")
    assert r.emails_abuso == ["abuse@sav.example"]
    assert r.titular is None and "privacidade" in r.tipo_titular
    assert r.servidores_nomes == ["carl.ns.cloudflare.com"]


def test_titular_empresa_br_eh_mantido_e_pessoa_fisica_omitida() -> None:
    empresa = {
        "entities": [
            {
                "roles": ["registrant"],
                "vcardArray": _vcard(("kind", "org"), ("fn", "Loja Exemplo Ltda")),
                "publicIds": [{"type": "cnpj", "identifier": "00.000.000/0001-00"}],
            }
        ]
    }
    r = interpretar_rdap_dominio(empresa)
    assert (r.titular, r.documento_titular, r.tipo_titular) == ("Loja Exemplo Ltda", "00.000.000/0001-00", "pessoa jurídica")

    pessoa = {
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
    r = interpretar_rdap_dominio(pessoa)
    assert r.titular is None and r.documento_titular is None and r.tipo_titular.startswith("pessoa física")
    assert "Fulana" not in r.model_dump_json() and "fulana@" not in r.model_dump_json()


def test_ip_rede_organizacao_e_abuso() -> None:
    dados = {
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
    ip = interpretar_rdap_ip("76.76.21.21", dados)
    assert (ip.rede, ip.organizacao, ip.emails_abuso) == ("VERCEL-01", "Vercel, Inc", ["abuse@vercel.example"])


def test_inquilinos_de_plataforma_sao_reconhecidos() -> None:
    assert plataforma_hospedagem("xn--assessoriaboletosj-gsb.weebly.com") == "weebly.com"
    assert plataforma_hospedagem("voucherreservas.lovable.app") == "lovable.app"
    assert plataforma_hospedagem("facilitabr.com") is None
    assert plataforma_hospedagem("www.facilitabr.com") is None


async def test_coletor_pagina_de_plataforma_informa_abuso_da_plataforma_nao_o_registro() -> None:
    caminhos_rdap: list[str] = []

    def tratador(requisicao: httpx.Request) -> httpx.Response:
        if requisicao.url.host == "doh.example":
            nome, tipo_registro = requisicao.url.params["name"], requisicao.url.params["type"]
            respostas_dns = {("bv.weebly.com", "A"): [{"type": 1, "data": "74.115.51.8"}]}.get((nome, tipo_registro), [])
            return httpx.Response(200, json={"Status": 0, "Answer": respostas_dns})
        caminhos_rdap.append(requisicao.url.path)
        if requisicao.url.path.startswith("/ip/"):
            return httpx.Response(
                429 if len(caminhos_rdap) == 1 else 200,  # a primeira chamada leva 429 e é repetida
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

    col = ColetorInteligenciaDominio(
        base_rdap="https://rdap.example",
        url_doh="https://doh.example/dns-query",
        transporte=httpx.MockTransport(tratador),
        espera_s=0,
        intervalo_rdap_s=0,
    )
    intel = await col.coletar("bv.weebly.com")
    assert intel.plataforma == "weebly.com" and intel.rdap is None and not intel.erros
    assert not any(p.startswith("/domain/") for p in caminhos_rdap)  # o registro da plataforma não interessa
    s = resumir(intel)
    assert s["abuso"] == "abuse@weebly.example" and s["criado"] == "" and "plataforma" in s["titular"]
    assert s["hospedagem"] == "Weebly, Inc. (74.115.51.8)" and s["resolve"] == "sim"


async def test_coletor_registra_dominio_morto() -> None:
    def tratador(requisicao: httpx.Request) -> httpx.Response:
        if requisicao.url.host == "doh.example":
            return httpx.Response(200, json={"Status": 3})
        return httpx.Response(200, json=GTLD)

    col = ColetorInteligenciaDominio(
        base_rdap="https://rdap.example",
        url_doh="https://doh.example/dns-query",
        transporte=httpx.MockTransport(tratador),
        intervalo_rdap_s=0,
    )
    intel = await col.coletar("www.golpe-exemplo.com.br")
    assert intel.dominio_registrado == "golpe-exemplo.com.br" and not intel.ips
    assert any("não resolve" in n for n in intel.notas)
    assert resumir(intel)["registrador"] == "Sav.com LLC"
