"""Lookalike-domain discovery: typosquatting, combosquatting and TLD swaps.

Generates permutations of the institution's official domains (fuzzers modelled on dnstwist:
omission, repetition, transposition, keyboard replacement/insertion, homoglyphs, vowel swap,
hyphenation, addition), plus *combosquatting* (brand label + sector keyword, e.g.
``bancobv-financiamento.com.br``) and TLD swaps. Combosquatting is by far the most abused form in
practice (Kintis et al., CCS 2017), so it is generated for the short brand labels where pure typos
would only produce unrelated two-letter domains.

Only names that **resolve in DNS to a public address** become discovery hits; everything else is
counted in the query record. Nothing is fetched here: resolved domains enter the normal pipeline
(URL policy, egress proxy, collection, analysis). A resolver that answers for non-existent names
(ISP NXDOMAIN hijacking) is detected first and disables the connector, since it would turn every
permutation into a false hit.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
from collections.abc import Awaitable, Callable, Iterable

from simio.marcas import PerfilMarca
from simio.nucleo.modelos import HipoteseBusca, RegistroConsulta, ResultadoDescoberta

Resolvedor = Callable[[str], Awaitable[list[str]]]

TECNICA_PT = {
    "omission": "omissão de caractere",
    "repetition": "caractere repetido",
    "transposition": "caracteres trocados de posição",
    "replacement": "tecla vizinha substituída",
    "insertion": "tecla vizinha inserida",
    "homoglyph": "caractere visualmente parecido",
    "vowel-swap": "vogal trocada",
    "hyphenation": "hífen inserido",
    "addition": "caractere acrescentado",
    "tld-swap": "mesmo nome com outra extensão",
    "combosquatting": "marca combinada com palavra do setor",
}

_TECLADO = {  # QWERTY / ABNT2 letter rows
    "q": "wa", "w": "qeas", "e": "wrsd", "r": "etdf", "t": "ryfg", "y": "tugh", "u": "yihj", "i": "uojk",
    "o": "ipkl", "p": "ol", "a": "qwsz", "s": "awedxz", "d": "serfcx", "f": "drtgvc", "g": "ftyhbv",
    "h": "gyujnb", "j": "huikmn", "k": "jiolm", "l": "kop", "z": "asx", "x": "zsdc", "c": "xdfv",
    "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
    "1": "2q", "2": "13wq", "3": "24ew", "4": "35re", "5": "46tr", "6": "57yt", "7": "68uy", "8": "79iu",
    "9": "80oi", "0": "9po",
}
_HOMOGLIFOS = {
    "o": ["0"], "0": ["o"], "l": ["1", "i"], "i": ["1", "l"], "1": ["l", "i"], "e": ["3"], "a": ["4"],
    "s": ["5"], "m": ["rn", "nn"], "w": ["vv"], "d": ["cl"], "g": ["q"], "q": ["g"], "u": ["v"], "v": ["u"],
    "b": ["d"], "n": ["m"],
}
_HOMOGLIFOS_MULTIPLOS = {"rn": "m", "vv": "w", "cl": "d", "nn": "m"}
_VOGAIS = "aeiou"
_ALFANUM = "abcdefghijklmnopqrstuvwxyz0123456789"
MIN_ROTULO_TYPO = 5  # shorter labels (e.g. "bv") only get combosquatting / TLD swaps

TLDS_TROCA_PADRAO = (
    "com", "net", "org", "com.br", "net.br", "org.br", "app.br", "tec.br", "app", "online", "site",
    "digital", "info", "store", "shop", "top", "xyz", "link", "click",
)


def _rotulo_valido(rotulo: str) -> bool:
    return (
        0 < len(rotulo) <= 63
        and not rotulo.startswith("-")
        and not rotulo.endswith("-")
        and all(c in _ALFANUM or c == "-" for c in rotulo)
    )


def separar_dominio(dominio: str) -> tuple[str, str]:
    """``bancobv.com.br`` -> (``bancobv``, ``com.br``)."""
    rotulo, _, sufixo = dominio.lower().strip(".").partition(".")
    return rotulo, sufixo


def variantes_typo(rotulo: str) -> dict[str, str]:
    """Permutations of a single DNS label -> {variant: fuzzer}."""
    saida: dict[str, str] = {}

    def adicionar(v: str, fuzzer: str) -> None:
        if v != rotulo and _rotulo_valido(v) and v not in saida:
            saida[v] = fuzzer

    n = len(rotulo)
    for i in range(n):
        adicionar(rotulo[:i] + rotulo[i + 1 :], "omission")
        adicionar(rotulo[:i] + rotulo[i] + rotulo[i:], "repetition")
        if i < n - 1:
            adicionar(rotulo[:i] + rotulo[i + 1] + rotulo[i] + rotulo[i + 2 :], "transposition")
        for k in _TECLADO.get(rotulo[i], ""):
            adicionar(rotulo[:i] + k + rotulo[i + 1 :], "replacement")
            adicionar(rotulo[:i] + k + rotulo[i:], "insertion")
            adicionar(rotulo[: i + 1] + k + rotulo[i + 1 :], "insertion")
        for g in _HOMOGLIFOS.get(rotulo[i], []):
            adicionar(rotulo[:i] + g + rotulo[i + 1 :], "homoglyph")
        if rotulo[i] in _VOGAIS:
            for v in _VOGAIS:
                adicionar(rotulo[:i] + v + rotulo[i + 1 :], "vowel-swap")
        if i > 0:
            adicionar(rotulo[:i] + "-" + rotulo[i:], "hyphenation")
    for par, unico in _HOMOGLIFOS_MULTIPLOS.items():
        inicio = rotulo.find(par)
        while inicio >= 0:
            adicionar(rotulo[:inicio] + unico + rotulo[inicio + 2 :], "homoglyph")
            inicio = rotulo.find(par, inicio + 1)
    for c in _ALFANUM:
        adicionar(rotulo + c, "addition")
    return saida


def dominios_semelhantes(
    sementes: Iterable[str],
    rotulos_combo: Iterable[str],
    palavras_chave: Iterable[str],
    *,
    tlds_troca: Iterable[str] = TLDS_TROCA_PADRAO,
    sufixos_combo: Iterable[str] = ("com.br", "com"),
    excluir: Callable[[str], bool] = lambda _d: False,
) -> dict[str, tuple[str, str]]:
    """{candidate domain: (fuzzer, seed)} for every seed domain and combo label."""
    saida: dict[str, tuple[str, str]] = {}

    def adicionar(dominio: str, fuzzer: str, semente: str) -> None:
        if dominio not in saida and not excluir(dominio):
            saida[dominio] = (fuzzer, semente)

    troca = list(dict.fromkeys(tlds_troca))
    for semente in sementes:
        rotulo, sufixo = separar_dominio(semente)
        if not rotulo or not sufixo:
            continue
        if len(rotulo) >= MIN_ROTULO_TYPO:
            for v, fuzzer in variantes_typo(rotulo).items():
                adicionar(f"{v}.{sufixo}", fuzzer, semente)
        for tld in troca:
            if tld != sufixo:
                adicionar(f"{rotulo}.{tld}", "tld-swap", semente)
    kws = [k.lower().strip() for k in palavras_chave if k.strip()]
    for rotulo in rotulos_combo:
        lab = rotulo.lower().strip()
        for kw in kws:
            for v in (f"{lab}{kw}", f"{lab}-{kw}", f"{kw}{lab}", f"{kw}-{lab}"):
                if _rotulo_valido(v):
                    for sufixo in sufixos_combo:
                        adicionar(f"{v}.{sufixo}", "combosquatting", lab)
    return saida


async def resolvedor_sistema(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


def _eh_publico(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


class ConectorTyposquat:
    """Discovery connector. Each planned query is one seed domain (``typo:<domain>``) or one combo
    label (``combo:<label>``); the hypothesis id records which."""

    nome = "typosquat"

    def __init__(
        self,
        marca: PerfilMarca,
        *,
        resolvedor: Resolvedor | None = None,
        concorrencia: int = 48,
        timeout_s: float = 6.0,
    ) -> None:
        self.marca = marca
        self.resolvedor = resolvedor or resolvedor_sistema
        self.concorrencia = concorrencia
        self.timeout_s = timeout_s
        self.motivo_desativacao: str | None = None
        self._sequestro_verificado = False

    def disponivel(self) -> tuple[bool, str]:
        if not self.marca.sementes_typosquat and not self.marca.rotulos_combo_typosquat:
            return False, "typosquat: no typosquat_seeds / typosquat_combo_labels in the brand profile"
        if self.motivo_desativacao:
            return False, f"typosquat: disabled: {self.motivo_desativacao}"
        return True, "typosquat: active (DNS permutations of the official domains)"

    def consultas(self) -> list[str]:
        return [f"typo:{s}" for s in self.marca.sementes_typosquat] + [
            f"combo:{c}" for c in self.marca.rotulos_combo_typosquat
        ]

    def _candidatas(self, consulta: str) -> dict[str, tuple[str, str]]:
        tipo, _, valor = consulta.partition(":")
        excluir = self.marca.eh_oficial
        if tipo == "typo":
            return dominios_semelhantes([valor], [], [], excluir=excluir)
        if tipo == "combo":
            return dominios_semelhantes([], [valor], self.marca.palavras_chave_typosquat, excluir=excluir)
        return {}

    async def _resolver(self, host: str) -> list[str] | None:
        try:
            return await asyncio.wait_for(self.resolvedor(host), self.timeout_s)
        except (OSError, TimeoutError, UnicodeError):
            return None

    async def _verificar_sequestro_nxdomain(self) -> None:
        if self._sequestro_verificado:
            return
        self._sequestro_verificado = True
        sondar = f"simio-nx-{secrets.token_hex(6)}.com.br"
        if await self._resolver(sondar):
            self.motivo_desativacao = (
                "the DNS resolver answers for non-existent domains (NXDOMAIN hijacking); "
                "every permutation would look registered. Use a resolver without redirection."
            )

    async def executar(
        self, hipotese: HipoteseBusca, consulta: str
    ) -> tuple[list[ResultadoDescoberta], RegistroConsulta]:
        await self._verificar_sequestro_nxdomain()
        if self.motivo_desativacao:
            return [], RegistroConsulta(
                fonte=self.nome, consulta=consulta, id_hipotese=hipotese.id, status="ERRO", erro=self.motivo_desativacao
            )
        candidatas = self._candidatas(consulta)
        semaforo = asyncio.Semaphore(self.concorrencia)

        async def sondar(dominio: str) -> tuple[str, list[str] | None]:
            async with semaforo:
                return dominio, await self._resolver(dominio)

        retornos = await asyncio.gather(*(sondar(d) for d in candidatas))
        resultados: list[ResultadoDescoberta] = []
        nao_publicos: list[str] = []
        for dominio, addrs in retornos:
            if not addrs:
                continue
            publicos = [a for a in addrs if _eh_publico(a)]
            if not publicos:
                nao_publicos.append(dominio)
                continue
            fuzzer, semente = candidatas[dominio]
            tecnica = TECNICA_PT.get(fuzzer, fuzzer)
            resultados.append(
                ResultadoDescoberta(
                    fonte=self.nome,
                    url_original=f"http://{dominio}/",
                    consulta=consulta,
                    id_hipotese=hipotese.id,
                    titulo=None,
                    trecho=(
                        f"Domínio registrado semelhante ao domínio oficial/marca '{semente}' "
                        f"(técnica: {tecnica}); resolve em DNS para {', '.join(publicos[:3])}."
                    ),
                    posicao=len(resultados) + 1,
                    metadados={"semelhante_a": semente, "fuzzer": fuzzer, "enderecos": publicos[:5]},
                )
            )
        registro = RegistroConsulta(
            fonte=self.nome,
            consulta=consulta,
            id_hipotese=hipotese.id,
            status="OK" if resultados else "VAZIA",
            qtd_resultados=len(resultados),
            metadados={
                "permutacoes_verificadas": len(candidatas),
                "resolvidos_publicos": len(resultados),
                "resolvidos_nao_publicos": nao_publicos[:50],
            },
        )
        return resultados, registro
