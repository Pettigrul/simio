from __future__ import annotations

from pathlib import Path

from simio.agentes.evidencia import calcular_sinais, montar_catalogo
from simio.agentes.identidade import AgenteIdentidade, CadastroCoban
from simio.nucleo.modelos import ClasseIdentidade, Nivel, StatusColeta
from tests.auxiliares import MARCA, montar_evidencia

CNPJ_A = "11.222.333/0001-81"  # exemplo clássico de documentação
CNPJ_ALFANUMERICO = "12.ABC.345/01DE-35"  # exemplo alfanumérico da Receita Federal


def _atribuicao(ev, cadastro: CadastroCoban | None = None, marca=MARCA):
    cat = montar_catalogo(ev, marca)
    sig = calcular_sinais(ev, marca, cat)
    return AgenteIdentidade(marca, cadastro or CadastroCoban()).atribuir("C1", ev, cat, sig)


def _cadastro(tmp_path: Path, linhas: str, oficial: bool = True) -> CadastroCoban:
    p = tmp_path / "reg.csv"
    p.write_text(linhas, encoding="utf-8")
    return CadastroCoban.carregar(p, oficial=oficial)


def test_coban_confirmado_exige_cnpj_no_cadastro(tmp_path: Path) -> None:
    ev = montar_evidencia(rodape=f"Loja Exemplo Ltda — CNPJ {CNPJ_A} — correspondente bancário do Banco BV")
    sem = _atribuicao(ev)
    assert sem.classificacao == ClasseIdentidade.COBAN_POSSIVEL
    assert any("COBAN_CONFIRMADO indisponível" in x for x in sem.limitacoes)
    reg = _cadastro(tmp_path, "cnpj;nome\n11222333000181;LOJA EXEMPLO LTDA\n")
    com_reg = _atribuicao(ev, reg)
    assert com_reg.classificacao == ClasseIdentidade.COBAN_CONFIRMADO
    assert com_reg.correspondencia_cadastro and com_reg.correspondencia_cadastro.casado_por == "cnpj"
    assert com_reg.chave_entidade == "cnpj:11222333000181"
    fato_cnpj = next(f for f in com_reg.fatos if f.tipo == "cnpj")
    assert fato_cnpj.ref_evidencia and fato_cnpj.valido is True


def test_produtos_da_marca_sozinhos_nao_implicam_coban() -> None:
    ev = montar_evidencia(
        titulo="Financiamento Banco BV",
        corpo="Financiamento Banco BV com taxas baixas",
        rodape=f"Revenda Exemplo Ltda CNPJ {CNPJ_A}",
    )
    a = _atribuicao(ev)
    assert a.classificacao == ClasseIdentidade.OUTRO_TERCEIRO


def test_identidade_desconhecida_sem_identificadores() -> None:
    a = _atribuicao(montar_evidencia(titulo="Banco BV", corpo="Financiamento Banco BV"))
    assert a.classificacao == ClasseIdentidade.IDENTIDADE_DESCONHECIDA
    assert a.chave_entidade == "domain:loja.example"
    assert any("domínio" in i for i in a.inferencias)


def test_correspondencia_so_por_nome_no_cadastro_nao_confirma(tmp_path: Path) -> None:
    reg = _cadastro(tmp_path, "cnpj,nome\n11222333000181,Loja Exemplo Ltda\n")
    ev = montar_evidencia(rodape="Razão social: Loja Exemplo Ltda")
    a = _atribuicao(ev, reg)
    assert a.classificacao == ClasseIdentidade.COBAN_POSSIVEL
    assert a.confianca_atribuicao == Nivel.BAIXA


def test_cadastro_nao_oficial_nunca_confirma(tmp_path: Path) -> None:
    reg = _cadastro(tmp_path, "cnpj;nome\n11222333000181;X\n", oficial=False)
    a = _atribuicao(montar_evidencia(rodape=f"CNPJ {CNPJ_A}"), reg)
    assert a.classificacao != ClasseIdentidade.COBAN_CONFIRMADO


def test_mesma_empresa_outro_estabelecimento_eh_coban_possivel_nao_confirmado(tmp_path: Path) -> None:
    # o cadastro tem a filial (0002); a página mostra a matriz (0001) da mesma empresa
    reg = _cadastro(tmp_path, "cnpj;nome\n11222333000262;LOJA EXEMPLO LTDA FILIAL\n")
    a = _atribuicao(montar_evidencia(rodape=f"Loja Exemplo Ltda CNPJ {CNPJ_A}"), reg)
    assert a.classificacao == ClasseIdentidade.COBAN_POSSIVEL
    assert a.confianca_atribuicao == Nivel.MEDIA
    assert a.correspondencia_cadastro and a.correspondencia_cadastro.casado_por == "cnpj_root"
    assert not a.correspondencia_cadastro.oficial


def test_cadastro_indice_por_nome_e_busca_por_raiz(tmp_path: Path) -> None:
    reg = _cadastro(tmp_path, "cnpj;nome\n11222333000181;Loja Exemplo Ltda\n11222333000262;Loja Exemplo Filial\n")
    assert reg.buscar_nome("LOJA EXEMPLO LTDA")["cnpj"] == "11222333000181"
    assert reg.buscar_raiz_cnpj("11222333000181") is None  # casamento exato tem precedência em outro ponto
    assert reg.buscar_raiz_cnpj("11.222.333/0003-43")["cnpj"] == "11222333000181"  # matriz tem preferência


def test_cnpj_invalido_nao_eh_usado() -> None:
    a = _atribuicao(montar_evidencia(rodape="Empresa X — CNPJ 11.222.333/0001-82"))
    assert a.cnpjs_responsavel == []
    assert any("inválido" in i for i in a.inferencias)


def test_cnpj_da_instituicao_nao_eh_responsavel() -> None:
    marca = MARCA.model_copy(update={"cnpjs_instituicao": [CNPJ_A]})
    ev = montar_evidencia(rodape=f"Financiamento pelo Banco BV (CNPJ {CNPJ_A}). Loja Fictícia Ltda CNPJ {CNPJ_ALFANUMERICO}")
    a = _atribuicao(ev, marca=marca)
    assert a.cnpjs_instituicao_referenciados == [CNPJ_A]
    assert a.cnpjs_responsavel == [CNPJ_ALFANUMERICO]


def test_cnpj_logo_apos_nome_da_instituicao_com_outro_cnpj_eh_referencia() -> None:
    ev = montar_evidencia(rodape=f"Loja Exemplo Ltda CNPJ {CNPJ_ALFANUMERICO}. Operação financiada pelo Banco BV, CNPJ {CNPJ_A}.")
    a = _atribuicao(ev)
    assert a.cnpjs_responsavel == [CNPJ_ALFANUMERICO] and a.cnpjs_instituicao_referenciados == [CNPJ_A]


def test_cnpj_unico_perto_do_nome_da_instituicao_eh_mantido_mas_sinalizado() -> None:
    # Ambíguo: pode ser da instituição ou do responsável, então nunca é descartado em silêncio.
    ev = montar_evidencia(rodape=f"Operação financiada pelo Banco BV, CNPJ {CNPJ_A}.")
    a = _atribuicao(ev)
    assert a.cnpjs_responsavel == [CNPJ_A] and a.cnpjs_instituicao_referenciados == []
    assert any("confirmar se pertence" in i for i in a.inferencias)
    assert a.confianca_atribuicao == Nivel.BAIXA


def test_cnpj_responsavel_apos_banner_da_marca_nao_eh_a_instituicao() -> None:
    ev = montar_evidencia(rodape=f"Financiamento Banco BV. Motos Exemplo Ltda, CNPJ {CNPJ_A}")
    a = _atribuicao(ev)
    assert a.cnpjs_responsavel == [CNPJ_A] and not a.cnpjs_instituicao_referenciados
    assert a.nome_exibicao == "Motos Exemplo Ltda"


def test_cnpj_da_instituicao_no_perfil_eh_sempre_referencia() -> None:
    marca = MARCA.model_copy(update={"cnpjs_instituicao": [CNPJ_A]})
    ev = montar_evidencia(rodape=f"CNPJ {CNPJ_A}")
    cat = montar_catalogo(ev, marca)
    a = AgenteIdentidade(marca, CadastroCoban()).atribuir("C1", ev, cat, calcular_sinais(ev, marca, cat))
    assert a.cnpjs_responsavel == [] and a.cnpjs_instituicao_referenciados == [CNPJ_A]


def test_nome_da_instituicao_nunca_eh_nome_do_responsavel() -> None:
    ev = montar_evidencia(meta={"og:site_name": "Banco BV"}, rodape="Nome fantasia: Banco BV S.A.")
    a = _atribuicao(ev)
    assert a.nome_exibicao is None
    assert a.classificacao == ClasseIdentidade.IDENTIDADE_DESCONHECIDA
    assert any(f.tipo == "nome_instituicao_exibido" for f in a.fatos)
    assert any("identidade declarada/visual" in i for i in a.inferencias)


def test_extracao_razao_social_nao_captura_texto_ao_redor() -> None:
    ev = montar_evidencia(
        rodape="Financie sua moto Condições especiais. Motos Exemplo Ltda\n"
        "Ligar agora WhatsApp Cobranças Exemplo Assessoria Ltda"
    )
    nomes = [f.valor for f in _atribuicao(ev).fatos if f.tipo == "razao_social"]
    assert "Motos Exemplo Ltda" in nomes and "Cobranças Exemplo Assessoria Ltda" in nomes


def test_cadastro_nao_eh_oficial_por_padrao(tmp_path: Path) -> None:
    p = tmp_path / "reg.csv"
    p.write_text(f"cnpj;nome\n{CNPJ_A};Loja Exemplo Ltda\n", encoding="utf-8")
    reg = CadastroCoban.carregar(p)
    ev = montar_evidencia(rodape=f"Loja Exemplo Ltda — CNPJ {CNPJ_A}")
    cat = montar_catalogo(ev, MARCA)
    a = AgenteIdentidade(MARCA, reg).atribuir("C1", ev, cat, calcular_sinais(ev, MARCA, cat))
    assert a.classificacao == ClasseIdentidade.COBAN_POSSIVEL


def test_varios_estabelecimentos_reduzem_confianca() -> None:
    ev = montar_evidencia(rodape=f"Loja A Ltda CNPJ {CNPJ_A} | Loja B Ltda CNPJ {CNPJ_ALFANUMERICO}")
    a = _atribuicao(ev)
    assert len(a.cnpjs_responsavel) == 2
    assert a.confianca_atribuicao == Nivel.BAIXA
    assert any("2 CNPJs" in i for i in a.inferencias)


def test_coleta_falha_gera_desconhecida() -> None:
    a = _atribuicao(montar_evidencia(status=StatusColeta.FALHOU))
    assert a.classificacao == ClasseIdentidade.IDENTIDADE_DESCONHECIDA and a.confianca_atribuicao == Nivel.NENHUMA


def test_fatos_e_inferencias_sao_separados() -> None:
    ev = montar_evidencia(
        rodape=f"Loja Exemplo Ltda CNPJ {CNPJ_A}. Contato: contato@loja.example",
        contatos=[("WhatsApp", "https://wa.me/550000000000")],
    )
    a = _atribuicao(ev)
    tipos = {f.tipo for f in a.fatos}
    assert {"dominio", "cnpj", "email", "whatsapp"} <= tipos
    assert all(f.ref_evidencia for f in a.fatos)
    assert a.inferencias  # raciocínio de associação separado dos fatos


def test_empresas_do_grupo_bv_e_outros_bancos_nunca_sao_atribuidos_como_terceiro_responsavel() -> None:
    # Regressão: pedaços de "BV Financeira S.A. - Crédito, Financiamento e Investimento", "BV Investimentos",
    # "NaPista", o CNPJ histórico 01.149.953/0001-89 ou nomes de outros bancos ("Banco Inter S/A") nunca
    # viram o terceiro responsável pela página.
    ev = montar_evidencia(
        rodape="Correspondente bancário de BV Financeira S.A. - Crédito, Financiamento e Investimento "
        "(CNPJ 01.149.953/0001-89), BV Investimentos, NaPista e Banco Inter S/A."
    )
    a = _atribuicao(ev)
    assert a.nome_exibicao is None
    assert a.cnpjs_responsavel == []
    assert "01.149.953/0001-89" in a.cnpjs_instituicao_referenciados


def test_reescrita_url_incorporavel_social_e_atribuicao_por_perfil_instagram() -> None:
    from simio.coletores.navegador import _url_incorporavel_social

    assert (
        _url_incorporavel_social("https://www.instagram.com/reels/DdEP-9cxlFH/")
        == "https://www.instagram.com/p/DdEP-9cxlFH/embed/captioned/"
    )
    assert (
        _url_incorporavel_social("https://www.instagram.com/loja_exemplo/p/AbC123/")
        == "https://www.instagram.com/p/AbC123/embed/captioned/"
    )
    assert (
        _url_incorporavel_social("https://www.instagram.com/agencia_bv_/")
        == "https://www.instagram.com/agencia_bv_/embed/"
    )
    assert "plugins/video.php" in _url_incorporavel_social(
        "https://www.facebook.com/loja.exemplo/videos/feirao-bv/123456/"
    )

    ev = montar_evidencia(
        url="https://www.instagram.com/reels/DdEP-9cxlFH/",
        titulo="Instagram @tdemultimarcas.ce - Não venda seu carro, refinanciamento disponível!",
        corpo="tdemultimarcas.ce Não venda seu carro, refinanciamento disponível!",
    )
    a = _atribuicao(ev)
    assert a.nome_exibicao == "@tdemultimarcas.ce"
    assert a.chave_entidade == "social:instagram.com:tdemultimarcas.ce"
