from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DIR_PROMPTS = Path(__file__).parent


@lru_cache(maxsize=16)
def carregar_prompt(nome: str) -> tuple[str, str]:
    # Devolve (versão, corpo). A versão vem do front-matter e é gravada junto com cada análise.
    bruto = (DIR_PROMPTS / f"{nome}.md").read_text(encoding="utf-8")
    versao = "sem-versao"
    corpo = bruto
    if bruto.startswith("---"):
        _, cabecalho, corpo = bruto.split("---", 2)
        for linha_txt in cabecalho.strip().splitlines():
            chave, _, valor = linha_txt.partition(":")
            if chave.strip() == "version":
                versao = valor.strip()
    return versao, corpo.strip()


def renderizar(nome: str, **valores: str) -> tuple[str, str]:
    versao, template = carregar_prompt(nome)
    saida = template
    for chave, val in valores.items():
        saida = saida.replace("{" + chave + "}", val)
    return versao, saida
