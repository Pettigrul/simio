#!/usr/bin/env python3
"""Pre-publication audit: secrets and forbidden files.

Usage:
  python scripts/check_secrets.py              # files tracked/staged in git
  python scripts/check_secrets.py --history    # also every commit reachable in local history
  python scripts/check_secrets.py --paths a b  # specific files

Exit code 1 if anything suspicious is found.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

CONTENT_PATTERNS = {
    "google-api-key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "google-oauth-token": re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "service-account-json": re.compile(r"\"type\"\s*:\s*\"service_account\""),
    "refresh-token": re.compile(r"\"refresh_token\"\s*:\s*\"[^\"]{10,}\""),
    "github-token": re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"),
    "assigned-secret": re.compile(
        r"(?i)\b[\w-]*(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"][A-Za-z0-9_\-./+=]{16,}['\"]"
    ),
    "env-secret-value": re.compile(r"(?m)^\s*SIMIO_\w*(API_KEY|TOKEN|SECRET)\s*=\s*\S+"),
}
FORBIDDEN_NAMES = re.compile(
    r"(?i)(^|/)(\.env(\.(?!example$)[^/]*)?|application_default_credentials\.json|adc\.json|"
    r"[^/]*service[-_]?account[^/]*\.json|[^/]*credentials[^/]*\.json|[^/]*\.(pem|key|p12|pfx|db|sqlite3?|"
    r"xlsx|jsonl|log)|coban_registry[^/]*\.csv)$"
)
ALLOWED_BINARY_DIRS = ("tests/fixtures/",)
DATA_DIRS = re.compile(r"(^|/)(output|outputs|evidence|reports|dossiers|registry)/")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def scan_text(name: str, text: str) -> list[str]:
    hits = []
    for label, rx in CONTENT_PATTERNS.items():
        for m in rx.finditer(text):
            if name.endswith("check_secrets.py") or (name.endswith(".env.example") and label == "env-secret-value"):
                continue
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{name}:{line}: possible {label}")
    return hits


def scan_name(name: str) -> list[str]:
    out = []
    if FORBIDDEN_NAMES.search(name):
        out.append(f"{name}: forbidden file type/name")
    if DATA_DIRS.search(name):
        out.append(f"{name}: investigation data directory must not be versioned")
    if re.search(r"(?i)\.(png|jpe?g|webp)$", name) and not name.startswith(ALLOWED_BINARY_DIRS):
        out.append(f"{name}: image outside tests/fixtures (possible evidence screenshot)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", nargs="*")
    ap.add_argument("--history", action="store_true")
    args = ap.parse_args()
    findings: list[str] = []
    if args.paths:
        for p in args.paths:
            path = Path(p)
            findings += scan_name(path.as_posix())
            findings += scan_text(path.as_posix(), path.read_text(encoding="utf-8", errors="replace"))
    else:
        files = sorted(set(git("ls-files").split("\n")) | set(git("diff", "--cached", "--name-only").split("\n")))
        for name in filter(None, files):
            findings += scan_name(name)
            p = Path(name)
            if p.is_file() and p.stat().st_size < 5_000_000:
                findings += scan_text(name, p.read_text(encoding="utf-8", errors="replace"))
        if args.history:
            for name in sorted(set(git("log", "--all", "--pretty=format:", "--name-only").split("\n"))):
                if name:
                    findings += [f"[history] {f}" for f in scan_name(name)]
            for rev in filter(None, git("rev-list", "--all").split("\n")):
                for label, rx in CONTENT_PATTERNS.items():
                    if label in ("assigned-secret", "env-secret-value"):
                        continue  # covered by working-tree scan; regex too broad for git grep -P portability
                    out = git("grep", "-I", "-n", "-P", rx.pattern, rev, "--", ".", ":!scripts/check_secrets.py")
                    findings += [f"[history {rev[:8]}] {line} ({label})" for line in out.splitlines() if line]
    findings = sorted(set(findings))
    for f in findings:
        print(f)
    print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
