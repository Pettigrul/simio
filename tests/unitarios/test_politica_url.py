from __future__ import annotations

import ipaddress

import pytest

from simio.seguranca.politica_url import PoliticaUrl, ViolacaoPolitica, ip_eh_publico


@pytest.fixture
def politica() -> PoliticaUrl:
    return PoliticaUrl()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "data:text/html,hi",
        "http://localhost/",
        "http://LOCALHOST./",
        "http://foo.localhost/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://printer.local/",
        "http://intranet/",
        "http://127.0.0.1/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://10.0.0.5/",
        "http://172.16.3.4/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://user:pass@example.com/",  # pragma: allowlist secret
        "http://example.com:22/",
        "http://example.com\\@evil/",
        "https://exa\nmple.com/",
    ],
)
def test_verificacao_estatica_rejeita(politica: PoliticaUrl, url: str) -> None:
    with pytest.raises(ViolacaoPolitica):
        politica.verificar(url)


def test_url_publica_permitida(politica: PoliticaUrl, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(politica, "_resolver_sincrono", lambda h, p: ["93.184.215.14"])
    assert politica.verificar("https://www.example.com/path?q=1") == "www.example.com"


def test_dns_resolvendo_para_privado_eh_bloqueado(politica: PoliticaUrl, monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS manipulation: a public-looking name that resolves to an internal address."""
    monkeypatch.setattr(politica, "_resolver_sincrono", lambda h, p: ["93.184.215.14", "10.1.2.3"])
    with pytest.raises(ViolacaoPolitica, match="non-public"):
        politica.verificar("https://rebind.example.com/")


def test_falha_dns_eh_bloqueada(politica: PoliticaUrl, monkeypatch: pytest.MonkeyPatch) -> None:
    def explodir(h: str, p: int) -> list[str]:
        raise OSError("NXDOMAIN")

    monkeypatch.setattr(politica, "_resolver_sincrono", explodir)
    with pytest.raises(ViolacaoPolitica, match="DNS"):
        politica.verificar("https://nx.example.com/")


async def test_verificacao_async_usa_resolvedor(politica: PoliticaUrl, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def getaddrinfo_falso(host, porta, type=0):
        return [(2, 1, 6, "", ("192.168.0.10", porta))]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo_falso)
    with pytest.raises(ViolacaoPolitica):
        await politica.verificar_async("https://evil.example.org/")


def test_verificacao_pos_conexao(politica: PoliticaUrl) -> None:
    politica.verificar_endereco_servidor("example.com", "93.184.215.14")
    with pytest.raises(ViolacaoPolitica):
        politica.verificar_endereco_servidor("rebind.example.com", "127.0.0.1")
    with pytest.raises(ViolacaoPolitica):
        politica.verificar_endereco_servidor("rebind.example.com", "[::ffff:10.0.0.1]")


def test_hosts_liberados_inseguro_eh_explicito() -> None:
    p = PoliticaUrl(hosts_liberados_inseguro=frozenset({"127.0.0.1"}))
    assert p.verificar("http://127.0.0.1:5555/x") == "127.0.0.1"
    with pytest.raises(ViolacaoPolitica):
        p.verificar("http://localhost:5555/x")


@pytest.mark.parametrize(
    ("ip", "publicos"),
    [
        ("8.8.8.8", True),
        ("2001:4860:4860::8888", True),
        ("10.0.0.1", False),
        ("::ffff:192.168.1.1", False),
        ("2002:0a00:0001::1", False),
        ("198.18.0.1", False),
        ("224.0.0.1", False),
    ],
)
def test_ip_eh_publico(ip: str, publicos: bool) -> None:
    assert ip_eh_publico(ipaddress.ip_address(ip)) is publicos
