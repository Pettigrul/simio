"""Structured logging: human-readable console + JSON lines file (local only, no telemetry)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)(\"?\s*[:=]\s*\"?)[^\s\",]+"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}"),
]


def redact(text: str) -> str:
    for rx in _SECRET_PATTERNS:
        text = rx.sub(lambda m: (m.group(1) + m.group(2) + "***") if m.lastindex and m.lastindex >= 2 else "***", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))[-4000:]
        return json.dumps(payload, ensure_ascii=False)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str, log_dir: Path | None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "simio.jsonl", encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
