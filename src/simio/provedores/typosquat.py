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

_TECLADO = {  # QWERTY / ABNT2
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
# Erro de digitação em rótulo curto ("bv") só gera domínio de duas letras sem relação; para esses fica
# o combosquatting, que é de longe o mais usado em golpes (Kintis et al., CCS 2017), e a troca de TLD.
MIN_ROTULO_TYPO = 5

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
    """{domínio candidato: (fuzzer, semente)}."""
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
    palavras = [p.lower().strip() for p in palavras_chave if p.strip()]
    for rotulo in rotulos_combo:
        base = rotulo.lower().strip()
        for p in palavras:
            for v in (f"{base}{p}", f"{base}-{p}", f"{p}{base}", f"{p}-{base}"):
                if _rotulo_valido(v):
                    for sufixo in sufixos_combo:
                        adicionar(f"{v}.{sufixo}", "combosquatting", base)
    return saida


async def resolvedor_sistema(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


def _eh_publico(endereco: str) -> bool:
    try:
        ip = ipaddress.ip_address(endereco.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


class ConectorTyposquat:
    # Cada consulta é "typo:<domínio>" ou "combo:<rótulo>". Só vira resultado o nome que resolve para
    # IP público; nada é baixado aqui, a coleta segue o pipeline normal.

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
            return False, "typosquat: perfil da marca sem sementes_typosquat / rotulos_combo_typosquat"
        if self.motivo_desativacao:
            return False, f"typosquat: desligado: {self.motivo_desativacao}"
        return True, "typosquat: ativo (variações em DNS dos domínios oficiais)"

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
        # Resolvedor que responde para nome inexistente (sequestro de NXDOMAIN pelo provedor) faria toda
        # variação parecer registrada.
        if self._sequestro_verificado:
            return
        self._sequestro_verificado = True
        sondar = f"simio-nx-{secrets.token_hex(6)}.com.br"
        if await self._resolver(sondar):
            self.motivo_desativacao = (
                "o resolvedor DNS responde para domínios inexistentes (sequestro de NXDOMAIN); "
                "toda variação pareceria registrada. Use um resolvedor sem redirecionamento."
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
        for dominio, enderecos in retornos:
            if not enderecos:
                continue
            publicos = [e for e in enderecos if _eh_publico(e)]
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
