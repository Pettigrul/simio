from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from simio.agentes.conformidade import AnalistaConformidade, CatalogoRequisitos
from simio.agentes.descoberta import AgenteDescoberta, deduplicar, priorizar
from simio.agentes.evidencia import calcular_sinais, montar_catalogo
from simio.agentes.identidade import AgenteIdentidade, CadastroCoban
from simio.agentes.marca import aplicar_regra_logo_antiga
from simio.agentes.visual import carregar_referencia, detectar_logo_antiga, mascaras_cor, pontuacao_regiao
from simio.nucleo.decisao import compor_final
from simio.nucleo.modelos import (
    Artefato,
    AvaliacaoMarca,
    CasoCenario,
    DadosPagina,
    DecisaoRevisao,
    Evidencia,
    InfoImagem,
    Nivel,
    Prioridade,
    Revisao,
    StatusColeta,
    SuficienciaEvidencia,
)
from simio.nucleo.modelos import ClassificacaoFinal as C
from simio.provedores.busca import BuscaLens
from simio.seguranca.politica_url import PoliticaUrl
from tests.auxiliares import MARCA, montar_evidencia

CAMINHO_REF = MARCA.caminhos_logo_antiga()[0]
REF = carregar_referencia(CAMINHO_REF)
LOGO = Image.open(CAMINHO_REF).convert("RGB")


def _monocromatica(img: Image.Image, rgb: tuple[int, int, int] = (30, 100, 255)) -> Image.Image:
    # logo atual: mesmo monograma, só azul
    vermelho, azul = mascaras_cor(img)
    arr = np.full((img.height, img.width, 3), 255, dtype=np.uint8)
    arr[vermelho | azul] = rgb
    return Image.fromarray(arr)


def _evidencia_captura(tmp_path: Path, tela: Image.Image, imagens: list[InfoImagem] | None = None) -> Evidencia:
    rel = Path("evidence/C1/attempt-1")
    (tmp_path / rel).mkdir(parents=True)
    tela.save(tmp_path / rel / "viewport.png")
    return Evidencia(
        id_candidata="C1",
        tentativa=1,
        status=StatusColeta.OK,
        url_original="https://loja.example/",
        pagina=DadosPagina(titulo="Loja", imagens=imagens or []),
        artefatos=[Artefato(tipo="captura_viewport", caminho=str(rel / "viewport.png"), sha256="x", tamanho_bytes=1)],
    )


def _pagina_com(logo: Image.Image, fundo: str = "white", quando: tuple[int, int] = (180, 140)) -> Image.Image:
    tela = Image.new("RGB", (1366, 900), fundo)
    # distrações: botão vermelho, barra azul no topo e blocos parecidos com texto
    tela.paste(Image.new("RGB", (240, 60), (220, 20, 60)), (900, 600))
    tela.paste(Image.new("RGB", (1366, 50), (0, 90, 170)), (0, 0))
    for i in range(6):
        tela.paste(Image.new("RGB", (300, 10), (20, 110, 190)), (200, 400 + 30 * i))
    tela.paste(logo, quando)
    return tela


def test_referencia_casa_consigo_em_tamanhos_pequenos_e_logo_atual_nunca_casa() -> None:
    for w in (950, 240, 95):
        pequeno = LOGO.resize((w, int(w * LOGO.height / LOGO.width)))
        assert pontuacao_regiao(pequeno, REF.assinatura) > 0.85
        assert pontuacao_regiao(_monocromatica(pequeno), REF.assinatura) == 0.0


def test_logo_antiga_achada_na_captura_sem_coordenadas_do_elemento(tmp_path: Path) -> None:
    logo = LOGO.resize((110, 58))
    ev = _evidencia_captura(tmp_path, _pagina_com(logo))
    [m] = detectar_logo_antiga(ev, tmp_path, REF)
    assert m.ref_captura == "E-SHOT-VIEWPORT" and m.pontuacao > 0.7 and m.acima_dobra
    vermelho, azul = mascaras_cor(logo)  # caixa esperada = tinta colorida da logo colada (ela tem margem branca)
    ys, xs = np.nonzero(vermelho | azul)
    x, y, w, h = m.bbox
    assert abs(x - (180 + xs.min())) <= 3 and abs(y - (140 + ys.min())) <= 3
    assert abs(w - (xs.max() + 1 - xs.min())) <= 4 and abs(h - (ys.max() + 1 - ys.min())) <= 4


def test_logo_atual_monocromatica_nao_eh_reportada(tmp_path: Path) -> None:
    ev = _evidencia_captura(tmp_path, _pagina_com(_monocromatica(LOGO.resize((110, 58)))))
    assert detectar_logo_antiga(ev, tmp_path, REF) == []


def test_logo_em_caixa_branca_em_pagina_carmim_achada_pelo_elemento_imagem(tmp_path: Path) -> None:
    caixa = Image.new("RGB", (90, 70), "white")
    caixa.paste(LOGO.resize((70, 37)), (10, 16))
    tela = _pagina_com(caixa, fundo=(216, 15, 90), quando=(128, 136))
    img = InfoImagem(
        alt="", src="https://loja.example/img/bv.png", acima_dobra=True, largura=90, altura=70, pagina_x=128, pagina_y=136
    )
    ev = _evidencia_captura(tmp_path, tela, [img])
    correspondencias = detectar_logo_antiga(ev, tmp_path, REF)
    assert correspondencias and any(m.src_imagem == "https://loja.example/img/bv.png" for m in correspondencias)


def _avaliacao(**kw) -> AvaliacaoMarca:
    base = dict(
        caso=CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA,
        classificacao=C.SEM_INDICIO,
        prioridade=Prioridade.BAIXA,
        confianca_extracao=Nivel.ALTA,
        suficiencia_evidencia=SuficienciaEvidencia.SUFICIENTE,
        analisador="model-x",
    )
    base.update(kw)
    return AvaliacaoMarca(**base)


def test_correspondencia_de_imagem_poe_piso_na_decisao_e_sobrevive_ao_rebaixamento(tmp_path: Path) -> None:
    ev = _evidencia_captura(tmp_path, _pagina_com(LOGO.resize((110, 58))))
    correspondencias = detectar_logo_antiga(ev, tmp_path, REF)
    cat = montar_catalogo(ev, MARCA, correspondencias_visuais=correspondencias)
    sig = calcular_sinais(ev, MARCA, cat)
    assert sig.logo_antiga_visual and any(i.ref == "E-VIS-LEGACY-001" for i in cat)

    a = aplicar_regra_logo_antiga(_avaliacao(), cat)
    assert a.identidade_visual_antiga and a.fonte_identidade_visual_antiga == "comparação de imagem"
    assert (a.classificacao, a.prioridade) == (C.REQUER_REVISAO, Prioridade.ALTA)
    assert any("E-VIS-LEGACY-001" in ind.refs_evidencia for ind in a.indicadores_confusao_marca)
    assert any("não registrou" in n for n in a.notas_salvaguarda)

    idn = AgenteIdentidade(MARCA, CadastroCoban()).atribuir("C1", ev, cat, sig)
    comp = AnalistaConformidade(CatalogoRequisitos.carregar()).avaliar(a, idn, False)
    liberada = Revisao(
        decisao=DecisaoRevisao.REBAIXAR,
        classificacao_revisada=C.SEM_INDICIO,
        prioridade_revisada=Prioridade.BAIXA,
        revisor="r",
    )
    f = compor_final("C1", a, liberada, idn, comp, ev)
    assert f.classificacao == C.REQUER_REVISAO and f.prioridade == Prioridade.ALTA
    assert f.identidade_visual_antiga and f.exige_revisao_humana


def test_deteccao_so_do_modelo_eh_registrada_e_ausencia_nao_muda_nada() -> None:
    ev = montar_evidencia(titulo="Loja")
    cat = montar_catalogo(ev, MARCA)
    a = aplicar_regra_logo_antiga(_avaliacao(identidade_visual_antiga=True), cat)
    assert a.fonte_identidade_visual_antiga == "modelo" and a.classificacao == C.REQUER_REVISAO
    intocado = _avaliacao()
    assert aplicar_regra_logo_antiga(intocado, cat) == intocado


async def test_busca_reversa_lens_planeja_e_prioriza() -> None:
    vistos: list[dict] = []

    def tratador(requisicao: httpx.Request) -> httpx.Response:
        vistos.append(json.loads(requisicao.content))
        return httpx.Response(
            200,
            json={
                "organic": [
                    {"title": "Financiamento", "link": "https://loja-exemplo.com.br/financiamento", "source": "Loja"},
                    {
                        "title": "BV logo vector",
                        "link": "https://www.shutterstock.com/image-vector/bv-1",
                        "source": "x",
                    },
                    {"title": "raw", "link": "x-raw-image:///abc", "source": "y"},
                ]
            },
        )

    semente = "https://logos.example/bv-financeira.png"
    con = BuscaLens("k", [semente], transporte=httpx.MockTransport(tratador))
    agente = AgenteDescoberta(MARCA, [con], PoliticaUrl(), max_consultas=10)
    planejar = agente.planejar([con])
    assert [(p.consulta, p.hipotese.id) for p in planejar] == [(semente, "H12-logo-antiga:lens")]
    desfecho = await agente.executar()
    assert vistos[0]["url"] == semente
    grupos, _ = deduplicar(desfecho.resultados, PoliticaUrl())
    ranqueados = {g.hostname: g for g in priorizar(grupos, MARCA)}
    assert "loja-exemplo.com.br" in ranqueados and ranqueados["loja-exemplo.com.br"].pontuacao > ranqueados["shutterstock.com"].pontuacao
    assert any("legacy logo" in r for r in ranqueados["loja-exemplo.com.br"].motivos)
    assert BuscaLens("k", []).disponivel()[0] is False and BuscaLens(None, [semente]).disponivel()[0] is False


def test_sinal_do_modelo_sobrevive_as_salvaguardas_de_marca() -> None:
    from simio.nucleo.decisao import ConfigPontuacao, aplicar_salvaguardas_marca
    from simio.nucleo.modelos import SaidaAnaliseMarca

    ev = montar_evidencia(titulo="Loja")
    cat = montar_catalogo(ev, MARCA)
    saida = SaidaAnaliseMarca(
        caso=CasoCenario.B_COMUNICACAO_AMBIGUA,
        classificacao=C.REQUER_REVISAO,
        prioridade=Prioridade.MEDIA,
        identidade_visual_antiga=True,
    )
    a = aplicar_salvaguardas_marca(
        saida,
        analisador="model-x",
        versao_prompt="v",
        refs_validas={i.ref for i in cat},
        evidencia=ev,
        sinais=calcular_sinais(ev, MARCA, cat),
        config_pontuacao=ConfigPontuacao(),
    )
    assert a.identidade_visual_antiga


def test_site_de_referencia_registra_a_logo_sem_elevar_o_caso(tmp_path: Path) -> None:
    assert MARCA.eh_isento_logo_antiga("commons.wikimedia.org") and not MARCA.eh_isento_logo_antiga("linkedin.com")
    ev = _evidencia_captura(tmp_path, _pagina_com(LOGO.resize((110, 58))))
    cat = montar_catalogo(ev, MARCA, correspondencias_visuais=detectar_logo_antiga(ev, tmp_path, REF))
    a = aplicar_regra_logo_antiga(_avaliacao(), cat, isento=True)
    assert a.identidade_visual_antiga and a.logo_antiga_isenta and "site de referência" in a.fonte_identidade_visual_antiga
    assert (a.classificacao, a.prioridade) == (C.SEM_INDICIO, Prioridade.BAIXA)
    idn = AgenteIdentidade(MARCA, CadastroCoban()).atribuir("C1", ev, cat, calcular_sinais(ev, MARCA, cat))
    comp = AnalistaConformidade(CatalogoRequisitos.carregar()).avaliar(a, idn, False)
    f = compor_final("C1", a, None, idn, comp, ev)
    assert f.classificacao == C.SEM_INDICIO and f.identidade_visual_antiga


def test_trecho_da_descoberta_lens_nao_eh_fato_da_pagina() -> None:
    from simio.nucleo.modelos import ResultadoDescoberta

    resultado = ResultadoDescoberta(
        fonte="lens",
        url_original="https://loja.example/",
        titulo="Loja",
        trecho="Imagem visualmente semelhante ao logotipo antigo (fonte: Loja)",
        id_hipotese="H12-logo-antiga:lens",
        consulta="https://img.example/bv.png",
    )
    ev = montar_evidencia(titulo="Loja")
    cat = montar_catalogo(ev, MARCA, resultados_descoberta=[resultado])
    sig = calcular_sinais(ev, MARCA, cat)  # dava UnboundLocalError quando o item E-DISC vinha primeiro
    assert not sig.marcadores_logo_antiga
