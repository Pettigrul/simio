"""Structured logging: human-readable console + JSON lines file (local only, no telemetry)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

_PADROES_SEGREDO = [
    # header-style credentials: keep the scheme word, drop the credential (Basic/Bearer + token)
    re.compile(r"(?i)((?:proxy-)?authorization)(\"?\s*[:=]\s*\"?)(?:(?:basic|bearer|negotiate|ntlm)\s+)?[^\s\",]+"),
    # key=value credentials, English and Portuguese names (SIMIO_SERPER_API_KEY / SIMIO_CHAVE_API_SERPER,
    # serper_api_key / chave_api_serper, token, secret/segredo, password/senha)
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
    """Filter out benign Windows Proactor socket-close noise and SDK AFC reminder."""

    def filter(self, registro: logging.LogRecord) -> bool:
        msg = registro.getMessage()
        if "_ProactorBasePipeTransport._call_connection_lost" in msg:
            exc = registro.exc_info[1] if registro.exc_info else None
            if isinstance(exc, ConnectionResetError):
                return False
        return "Direct use of automatic function calling (AFC)" not in msg


def configurar_logging(nivel: str, dir_log: Path | None) -> None:
    raiz = logging.getLogger()
    raiz.handlers.clear()
    raiz.setLevel(nivel.upper())
    filtro_ruido = _FiltroRuidoBenigno()
    console = logging.StreamHandler()
    console.addFilter(filtro_ruido)
    console.setFormatter(FormatadorMascarador("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    raiz.addHandler(console)
    if dir_log is not None:
        dir_log.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(dir_log / "simio.jsonl", encoding="utf-8")
        fh.addFilter(filtro_ruido)
        fh.setFormatter(FormatadorJson())
        raiz.addHandler(fh)
    for ruidoso in ("httpx", "httpcore", "google_genai", "urllib3", "asyncio"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
