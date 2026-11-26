from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

_PADROES_SEGREDO = [
    # cabeçalho de autenticação: mantém o nome do cabeçalho e tira a credencial (Basic/Bearer + token)
    re.compile(r"(?i)((?:proxy-)?authorization)(\"?\s*[:=]\s*\"?)(?:(?:basic|bearer|negotiate|ntlm)\s+)?[^\s\",]+"),
    # chave=valor com nomes em inglês e em português (SIMIO_SERPER_API_KEY / SIMIO_CHAVE_API_SERPER,
    # token, secret/segredo, password/senha)
    re.compile(
        r"(?i)([\w-]*(?:api[_-]?key|token|secret|segredo|password|passwd|senha)|[\w-]*chave[_-]?api[\w-]*)"
        r"(\"?\s*[:=]\s*\"?)[^\s\",]+"
    ),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}"),
]
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")


def mascarar(texto: str) -> str:
    texto = _USERINFO.sub(lambda m: m.group(1) + "***:***@", texto)
    for rx in _PADROES_SEGREDO:
        texto = rx.sub(
            lambda m: (m.group(1) + m.group(2) + "***") if m.lastindex and m.lastindex >= 2 else "***", texto
        )
    return texto


class FormatadorJson(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(registro.created, UTC).isoformat(timespec="milliseconds"),
            "level": registro.levelname,
            "logger": registro.name,
            "msg": mascarar(registro.getMessage()),
        }
        if registro.exc_info:
            payload["exc"] = mascarar(self.formatException(registro.exc_info))[-4000:]
        return json.dumps(payload, ensure_ascii=False)


class FormatadorMascarador(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        return mascarar(super().format(registro))


class _FiltroRuidoBenigno(logging.Filter):
    # Ruído sem importância: fechamento de socket do Proactor no Windows e o aviso de AFC do SDK.
    def filter(self, registro: logging.LogRecord) -> bool:
        msg = registro.getMessage()
        if "_ProactorBasePipeTransport._call_connection_lost" in msg:
            exc = registro.exc_info[1] if registro.exc_info else None
            if isinstance(exc, ConnectionResetError):
                return False
        return "Direct use of automatic function calling (AFC)" not in msg


def configurar_logging(nivel: str, arquivo_log: Path | None) -> None:
    raiz = logging.getLogger()
    raiz.handlers.clear()
    raiz.setLevel(nivel.upper())
    filtro_ruido = _FiltroRuidoBenigno()
    console = logging.StreamHandler()
    console.addFilter(filtro_ruido)
    console.setFormatter(FormatadorMascarador("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    raiz.addHandler(console)
    if arquivo_log is not None:
        arquivo_log.parent.mkdir(parents=True, exist_ok=True)
        arquivo = logging.FileHandler(arquivo_log, encoding="utf-8")
        arquivo.addFilter(filtro_ruido)
        arquivo.setFormatter(FormatadorJson())
        raiz.addHandler(arquivo)
    for ruidoso in ("httpx", "httpcore", "google_genai", "urllib3", "asyncio"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
