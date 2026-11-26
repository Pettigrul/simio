#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PADROES_CONTEUDO = {
    "chave-api-google": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "token-oauth-google": re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),
    "chave-privada": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "json-conta-servico": re.compile(r"\"type\"\s*:\s*\"service_account\""),
    "token-renovacao": re.compile(r"\"refresh_token\"\s*:\s*\"[^\"]{10,}\""),
    "token-github": re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}"),
    "chave-acesso-aws": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "token-slack": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"),
    "segredo-atribuido": re.compile(
        r"(?i)\b[\w-]*(api[_-]?key|secret|segredo|token|password|passwd|senha|chave[_-]?api[\w-]*)\b\s*[:=]\s*"
        r"['\"][A-Za-z0-9_\-./+=]{16,}['\"]"
    ),
    "valor-secreto-env": re.compile(r"(?m)^\s*SIMIO_(\w*(API_KEY|TOKEN|SECRET|SEGREDO)|\w*CHAVE_API\w*)\s*=\s*\S+"),
    "url-com-credencial": re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@'\"<>]+:[^/\s@'\"<>]{3,}@[\w.\-]+"),
    "cabecalho-chave-api": re.compile(r"(?i)[\"']x-(?:api-key|subscription-token)[\"']\s*:\s*[\"'][^\"'{}\s]{8,}[\"']"),
    "cabecalho-autenticacao": re.compile(
        r"(?i)authorization[\"']?\s*[:=]\s*[\"']?(?:basic|bearer)\s+[A-Za-z0-9+/=._\-]{12,}"
    ),
}
NOMES_PROIBIDOS = re.compile(
    r"(?i)(^|/)([^/]*\.env(\.(?!example$)[^/]*)?|application_default_credentials\.json|adc\.json|"
    r"[^/]*service[-_]?account[^/]*\.json|[^/]*credentials[^/]*\.json|[^/]*\.(pem|key|p12|pfx)|"
    r"coban_registry[^/]*\.csv|cadastro_coban[^/]*\.csv)$"
)
NOMES_DADOS = re.compile(r"(?i)\.(db|sqlite3?|xlsx|jsonl|log)$")
DIRS_BINARIOS_PERMITIDOS = ("tests/fixtures/", "src/simio/marcas/assets/", "assets/", "design-system/")
# Pastas de resultados versionadas de propósito (git add -f): nelas só valem as regras de segredo.
DIRS_RESULTADOS_VERSIONADOS = ("resultados/", "output/")
PRAGMAS_PERMISSAO = ("pragma: segredo permitido", "pragma: allowlist secret")
DIRS_PRAGMA_PERMISSAO = ("tests/",)  # o pragma só vale em código de teste
DIRS_DADOS = re.compile(
    r"(^|/)(output|outputs|evidence|reports|dossiers|registry|resultados|campanhas|evidencias|dossies)/"
)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


# Marcadores óbvios em código de teste (dígitos em sequência, domínios example da RFC 2606, user:pass).
# Segredo de verdade não tem essa cara; fora de tests/ continuam sendo apontados.
SINTETICO = re.compile(r"(?i)1234567890|user:pass|@(?:[\w-]+\.)*example(?:\.(?:com|org|net))?(?=[:/\s'\"]|$)")


def _permitido(caminho: str, texto_linha: str, rx: re.Pattern[str]) -> bool:
    if not caminho.startswith(DIRS_PRAGMA_PERMISSAO):
        return False
    if any(p in texto_linha for p in PRAGMAS_PERMISSAO):
        return True
    correspondencias = [m.group(0) for m in rx.finditer(texto_linha)]
    return bool(correspondencias) and all(SINTETICO.search(m) for m in correspondencias)


def varrer_texto(nome: str, texto: str) -> list[str]:
    resultados = []
    for rotulo, rx in PADROES_CONTEUDO.items():
        for m in rx.finditer(texto):
            exemplo_env = nome.endswith(".env.example") and rotulo == "valor-secreto-env"
            if nome.endswith("verificar_segredos.py") or exemplo_env:
                continue
            linha_txt = texto.count("\n", 0, m.start()) + 1
            texto_linha = texto.splitlines()[linha_txt - 1] if texto else ""
            if _permitido(nome, texto_linha, rx):
                continue
            resultados.append(f"{nome}:{linha_txt}: possível {rotulo}")
    return resultados


def varrer_nome(nome: str) -> list[str]:
    saida = []
    if NOMES_PROIBIDOS.search(nome):
        saida.append(f"{nome}: tipo ou nome de arquivo proibido")
    if nome.startswith(DIRS_RESULTADOS_VERSIONADOS):
        return saida
    if NOMES_DADOS.search(nome):
        saida.append(f"{nome}: banco, planilha ou log fora da pasta de resultados")
    if DIRS_DADOS.search(nome):
        saida.append(f"{nome}: pasta de dados de investigação não pode ser versionada")
    if re.search(r"(?i)\.(png|jpe?g|webp)$", nome) and not nome.startswith(DIRS_BINARIOS_PERMITIDOS):
        saida.append(f"{nome}: imagem fora de {', '.join(DIRS_BINARIOS_PERMITIDOS)} (pode ser captura de evidência)")
    return saida


def principal() -> int:
    ap = argparse.ArgumentParser(
        description="Procura segredos e arquivos proibidos antes de publicar. Sem opções, confere os "
        "arquivos rastreados e preparados no git. Sai com código 1 se achar algo."
    )
    ap.add_argument("--caminhos", nargs="*", help="confere só estes arquivos")
    ap.add_argument("--historico", action="store_true", help="confere também todos os commits do histórico local")
    args = ap.parse_args()
    achados: list[str] = []
    if args.caminhos:
        for p in args.caminhos:
            caminho = Path(p)
            achados += varrer_nome(caminho.as_posix())
            achados += varrer_texto(caminho.as_posix(), caminho.read_text(encoding="utf-8", errors="replace"))
    else:
        arquivos = sorted(set(git("ls-files").split("\n")) | set(git("diff", "--cached", "--name-only").split("\n")))
        for nome in filter(None, arquivos):
            achados += varrer_nome(nome)
            p = Path(nome)
            if p.is_file() and p.stat().st_size < 5_000_000:
                achados += varrer_texto(nome, p.read_text(encoding="utf-8", errors="replace"))
        if args.historico:
            for nome in sorted(set(git("log", "--all", "--pretty=format:", "--name-only").split("\n"))):
                if nome:
                    achados += [f"[histórico] {f}" for f in varrer_nome(nome)]
            for rev in filter(None, git("rev-list", "--all").split("\n")):
                for rotulo, rx in PADROES_CONTEUDO.items():
                    if rotulo in ("segredo-atribuido", "valor-secreto-env"):
                        continue  # já coberto na varredura da árvore; regex largo demais para o git grep -P
                    saida = git("grep", "-I", "-n", "-P", rx.pattern, rev, "--", ".", ":!scripts/verificar_segredos.py")
                    for linha_txt in saida.splitlines():
                        # <rev>:<caminho>:<linha>:<conteúdo>
                        partes = linha_txt.split(":", 3)
                        if len(partes) == 4 and _permitido(partes[1], partes[3], rx):
                            continue
                        if linha_txt:
                            achados.append(f"[histórico {rev[:8]}] {linha_txt} ({rotulo})")
    achados = sorted(set(achados))
    for f in achados:
        print(f)
    print(f"{len(achados)} achado(s)")
    return 1 if achados else 0


if __name__ == "__main__":
    sys.exit(principal())
