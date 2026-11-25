from __future__ import annotations

import ipaddress

import pytest

from simio.security.url_policy import PolicyViolation, UrlPolicy, ip_is_public


@pytest.fixture
def policy() -> UrlPolicy:
    return UrlPolicy()


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
def test_static_rejects(policy: UrlPolicy, url: str) -> None:
    with pytest.raises(PolicyViolation):
        policy.check(url)


def test_public_url_allowed(policy: UrlPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "_resolve_sync", lambda h, p: ["93.184.215.14"])
    assert policy.check("https://www.example.com/path?q=1") == "www.example.com"


def test_dns_resolving_to_private_is_blocked(policy: UrlPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS manipulation: a public-looking name that resolves to an internal address."""
    monkeypatch.setattr(policy, "_resolve_sync", lambda h, p: ["93.184.215.14", "10.1.2.3"])
    with pytest.raises(PolicyViolation, match="non-public"):
        policy.check("https://rebind.example.com/")


def test_dns_failure_is_blocked(policy: UrlPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(h: str, p: int) -> list[str]:
        raise OSError("NXDOMAIN")

    monkeypatch.setattr(policy, "_resolve_sync", boom)
    with pytest.raises(PolicyViolation, match="DNS"):
        policy.check("https://nx.example.com/")


async def test_async_check_uses_resolver(policy: UrlPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def fake_getaddrinfo(host, port, type=0):
        return [(2, 1, 6, "", ("192.168.0.10", port))]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(PolicyViolation):
        await policy.acheck("https://evil.example.org/")


def test_post_connection_check(policy: UrlPolicy) -> None:
    policy.check_server_address("example.com", "93.184.215.14")
    with pytest.raises(PolicyViolation):
        policy.check_server_address("rebind.example.com", "127.0.0.1")
    with pytest.raises(PolicyViolation):
        policy.check_server_address("rebind.example.com", "[::ffff:10.0.0.1]")


def test_unsafe_allow_hosts_is_explicit() -> None:
    p = UrlPolicy(unsafe_allow_hosts=frozenset({"127.0.0.1"}))
    assert p.check("http://127.0.0.1:5555/x") == "127.0.0.1"
    with pytest.raises(PolicyViolation):
        p.check("http://localhost:5555/x")


@pytest.mark.parametrize(
    ("ip", "public"),
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
def test_ip_is_public(ip: str, public: bool) -> None:
    assert ip_is_public(ipaddress.ip_address(ip)) is public
