from __future__ import annotations

import pytest
from typer.testing import CliRunner

from simio import __version__
from simio.cli import app, traduzir_erro_click


def test_versao() -> None:
    r = CliRunner().invoke(app, ["--versao"])
    assert r.exit_code == 0
    assert r.output.strip() == f"simio {__version__}"


@pytest.mark.parametrize(
    ("argumentos", "esperado"),
    [
        (["--xpto"], "Opção desconhecida: --xpto"),
        (["investigar", "--xpto"], "Opção desconhecida: --xpto"),
        (["investigr"], "Comando desconhecido: investigr"),
        (["investigar", "--limite", "abc"], "Valor inválido para '--limite'"),
        (["investigar", "--limite"], "A opção --limite precisa de um valor"),
        (["status", "sobra"], "Argumentos que sobraram: sobra"),
    ],
)
def test_erros_de_uso_em_portugues(argumentos: list[str], esperado: str) -> None:
    r = CliRunner().invoke(app, argumentos)
    assert r.exit_code == 2
    texto = " ".join(r.output.replace("│", " ").split())
    assert esperado in texto
    for ingles in ("No such", "Invalid value", "requires an", "unexpected extra"):
        assert ingles not in texto


def test_traducao_das_mensagens_do_click() -> None:
    assert traduzir_erro_click("Missing command.") == "Falta o comando."
    assert (
        traduzir_erro_click("Invalid value for '--limite': 0 is not in the range x>=1.")
        == "Valor inválido para '--limite': 0 está fora do intervalo x>=1."
    )
    assert (
        traduzir_erro_click("Invalid value for '--paralelismo': Input should be less than or equal to 16")
        == "Valor inválido para '--paralelismo': precisa ser menor ou igual a 16"
    )
    assert traduzir_erro_click("Missing option '--marca'.") == "Falta a opção '--marca'."


def test_ajuda_sem_metavar_em_ingles() -> None:
    r = CliRunner().invoke(app, ["investigar", "--help"], env={"COLUMNS": "160"})
    assert r.exit_code == 0
    for metavar in ("<bool>", "<str>", "<int range>", "<path>", "<file>"):
        assert metavar not in r.output
    assert "NÚMERO" in r.output and "PASTA" in r.output
