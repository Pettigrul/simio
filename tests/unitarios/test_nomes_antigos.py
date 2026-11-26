from __future__ import annotations

import os
from pathlib import Path

import pytest

from simio import configuracoes as mod_configuracoes
from simio.agentes.conformidade import CatalogoRequisitos
from simio.configuracoes import NomesAntigos, carregar_configuracoes, variaveis_com_nome_antigo
from simio.marcas import carregar_marca
from simio.nucleo.decisao import ConfigPontuacao
from simio.nucleo.modelos import Dimensao, FonteRequisito


@pytest.fixture
def ambiente_limpo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for chave in list(os.environ):
        if chave.upper().startswith("SIMIO_"):
            monkeypatch.delenv(chave, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod_configuracoes, "_ENV_REPO", tmp_path / "nao-existe" / ".env")
    return tmp_path


def test_nome_antigo_no_env_e_no_ambiente_impede_carregar(
    ambiente_limpo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valor = "http://usuario:segredo-do-proxy@proxy.example:8080"  # pragma: segredo permitido
    (ambiente_limpo / ".env").write_text(
        f"# comentário\nSIMIO_BROWSER_PROXY={valor}\nSIMIO_NIVEL_LOG=DEBUG\n", encoding="utf-8"
    )
    monkeypatch.setenv("SIMIO_GCP_PROJECT", "projeto-antigo")
    with pytest.raises(NomesAntigos) as erro:
        carregar_configuracoes()
    msg = str(erro.value)
    assert "SIMIO_BROWSER_PROXY (agora SIMIO_PROXY_NAVEGADOR; em .env)" in msg
    assert "SIMIO_GCP_PROJECT (agora SIMIO_PROJETO_GCP; em ambiente)" in msg
    assert "segredo-do-proxy" not in msg and "projeto-antigo" not in msg
    assert "SIMIO_NIVEL_LOG" not in msg


def test_diagnostico_ve_os_nomes_antigos_sem_falhar(ambiente_limpo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMIO_SERPER_API_KEY", "x" * 30)
    s = carregar_configuracoes(aceitar_nomes_antigos=True)
    assert s.chave_api_serper is None
    assert variaveis_com_nome_antigo(None) == ["SIMIO_SERPER_API_KEY (agora SIMIO_CHAVE_API_SERPER; em ambiente)"]


def test_nomes_novos_carregam_normalmente(ambiente_limpo: Path) -> None:
    (ambiente_limpo / ".env").write_text("SIMIO_PROXY_NAVEGADOR=http://proxy.example:8080\n", encoding="utf-8")
    assert carregar_configuracoes().proxy_navegador == "http://proxy.example:8080"


def test_cli_recusa_nome_antigo(ambiente_limpo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from simio.cli import app

    monkeypatch.setenv("SIMIO_OUTPUT_DIR", str(ambiente_limpo / "saida"))
    r = CliRunner().invoke(app, ["status"])
    assert r.exit_code == 2
    assert "SIMIO_OUTPUT_DIR (agora SIMIO_DIR_SAIDA" in r.output


def test_scoring_toml_antigo_eh_traduzido(tmp_path: Path) -> None:
    p = tmp_path / "scoring.toml"
    p.write_text("w_multi_bank_context = 9.0\nthreshold_b = 7\nstrong_min_indicators = 3\n", encoding="utf-8")
    sc = ConfigPontuacao.carregar(p)
    assert sc.p_contexto_multibanco == 9.0 and sc.limiar_b == 7 and sc.min_indicadores_forte == 3
    assert sc.p_ouvidoria == ConfigPontuacao().p_ouvidoria


def test_peso_desconhecido_eh_recusado(tmp_path: Path) -> None:
    p = tmp_path / "pontuacao.toml"
    p.write_text("p_contexto_multibanco = 1.0\nw_peso_que_nao_existe = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="w_peso_que_nao_existe"):
        ConfigPontuacao.carregar(p)


REQUISITOS_ANTIGOS = '''
[[requirement]]
id = "CONTRATO-7.2"
citation = "Contrato de correspondente, cláusula 7.2"
text = "O correspondente não pode usar a marca da contratante como se fosse dela."
source = "INTERNAL_POLICY"
dimensions = ["LOGO_USAGE", "VISUAL_IDENTITY"]
hypothesis = "Uso da marca sem autorização"
addressed_to = "Correspondente"
'''


def test_requisitos_contratuais_no_formato_antigo(tmp_path: Path) -> None:
    p = tmp_path / "contrato.toml"
    p.write_text(REQUISITOS_ANTIGOS, encoding="utf-8")
    cat = CatalogoRequisitos.carregar(p)
    assert cat.contratuais_carregados
    req = next(r for r in cat.requisitos if r.id == "CONTRATO-7.2")
    assert req.fonte == FonteRequisito.POLITICA_INTERNA
    assert req.dimensoes == [Dimensao.USO_LOGO, Dimensao.IDENTIDADE_VISUAL]
    assert req.citacao.startswith("Contrato") and req.destinatario == "Correspondente"


def test_requisito_com_chave_desconhecida_falha(tmp_path: Path) -> None:
    p = tmp_path / "contrato.toml"
    p.write_text(REQUISITOS_ANTIGOS.replace("addressed_to", "destinatary"), encoding="utf-8")
    with pytest.raises(ValueError, match="destinatary"):
        CatalogoRequisitos.carregar(p)


def test_arquivo_sem_requisitos_nao_conta_como_carregado(tmp_path: Path) -> None:
    p = tmp_path / "contrato.toml"
    p.write_text('titulo = "vazio"\n', encoding="utf-8")
    assert not CatalogoRequisitos.carregar(p).contratuais_carregados


def test_perfil_de_marca_com_chaves_antigas(tmp_path: Path) -> None:
    p = tmp_path / "minha_marca.toml"
    p.write_text(
        'id = "bv"\ndisplay_name = "Banco BV"\nstrong_terms = ["banco bv"]\n'
        'official_domains = ["bv.com.br"]\nlegacy_logo_references = ["assets/bv_legacy_logo.png"]\n',
        encoding="utf-8",
    )
    marca = carregar_marca("bv", p)
    assert marca.nome_exibicao == "Banco BV" and marca.dominios_oficiais == ["bv.com.br"]
    assert all(c.is_file() for c in marca.caminhos_logo_antiga())
