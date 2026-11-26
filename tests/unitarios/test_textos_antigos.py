from __future__ import annotations

import pytest

from simio.nucleo.modelos import (
    CasoCenario,
    ClasseIdentidade,
    ClassificacaoFinal,
    DecisaoFinal,
    Evidencia,
    Nivel,
    Prioridade,
    StatusColeta,
    SuficienciaEvidencia,
)
from simio.relatorios.dados import _evidencia_traduzida, _traduzido
from simio.relatorios.rotulos import (
    descrever_analisador,
    descrever_chave_entidade,
    num_br,
    resumir_consultas,
    rotulo_fonte,
)
from simio.relatorios.textos_antigos import traduzir

# Pares tirados do banco real (resultados/simio.db), gravados pelas versões antigas.
EXEMPLOS_REAIS = [
    (
        "[Revisor: CONFIRM] Confirma-se a classificação de STRONG_INDICATION com prioridade HIGH.",
        "Revisor confirmou: Confirma-se a classificação de Indício forte com prioridade Alta.",
    ),
    (
        "Triagem heurística — cenário B_AMBIGUOUS_COMMUNICATION: uso destacado da marca sem transparência suficiente.",
        "Triagem heurística, cenário B · Comunicação ambígua: uso destacado da marca sem transparência suficiente.",
    ),
    (
        "página não inspecionada diretamente não pode receber NO_INDICATION: marcado INCONCLUSIVE",
        "página não inspecionada diretamente não pode receber Sem indício: marcado Inconclusiva",
    ),
    (
        "nenhum cadastro autorizado de correspondentes carregado; CONFIRMED_COBAN indisponível nesta execução",
        "nenhum cadastro autorizado de correspondentes carregado; Correspondente confirmado indisponível nesta execução",
    ),
    ("prioritised (score 2.0)", "priorizada (pontuação 2,0)"),
    ("prioritised (score -5.5)", "priorizada (pontuação -5,5)"),
    ("priorizada (pontuação 5.0)", "priorizada (pontuação 5,0)"),
    ("prepare error, retry 1: UnboundLocalError", "erro em preparar, nova tentativa (1): UnboundLocalError"),
    ("transient collection failure, retry 2", "falha temporária na coleta, nova tentativa (2)"),
    ("retry requested by operator", "nova tentativa pedida pelo operador"),
    (
        "reviewer requested recollection: coleta parcial com falha transitória",
        "revisor pediu nova coleta: coleta parcial com falha transitória",
    ),
    (
        "LLM unavailable at brand; retrying with heuristic analysis",
        "LLM indisponível em marca; nova tentativa com análise heurística",
    ),
    ("network did not become idle (continuing)", "a página continuou carregando; a coleta seguiu assim mesmo"),
    ("a rede não ficou ociosa (coleta seguiu)", "a página continuou carregando; a coleta seguiu assim mesmo"),
    ("HTTP 403: page content is an error page", "HTTP 403: o conteúdo é uma página de erro"),
    ("page budget of 90.0s exceeded", "tempo limite da página (90.0s) excedido"),
    ("HTML truncated to 2000000 bytes", "HTML truncado em 2000000 bytes"),
    (
        "possible bot challenge/CAPTCHA detected; no bypass attempted",
        "possível desafio anti-bot/CAPTCHA detectado; nenhuma tentativa de contorno",
    ),
    (
        "blocked by URL policy: DNS resolution failed for www.bvfinanon.com: [Errno -2] Name or service not known",
        "bloqueada pela política de URL: falha na resolução DNS de www.bvfinanon.com: [Errno -2] Name or service not known",
    ),
    (
        "navigation error: Page.goto: net::ERR_CERT_DATE_INVALID at https://mkveiculos.com/financie",
        "erro de navegação: Page.goto: net::ERR_CERT_DATE_INVALID at https://mkveiculos.com/financie",
    ),
    ("brave: API key not configured (optional)", "brave: chave de API não configurada (opcional)"),
    ("serper: configured", "serper: configurado"),
    ("grounding: model=gemini-3.8-flash", "grounding: modelo gemini-3.8-flash"),
    ("grounding: modelo=gemini-3.8-flash", "grounding: modelo gemini-3.8-flash"),
    (
        "coban-seeded search (H9): 40 registered correspondents in 10 prompt(s); 0 searched in earlier campaigns, "
        "~52511 not yet searched",
        "busca pelos COBANs cadastrados (H9): 40 correspondentes em 10 prompts; 0 já pesquisados em campanhas "
        "anteriores, ~52.511 ainda não",
    ),
    (
        "busca pelos COBANs cadastrados (H9): 40 correspondentes em 1 prompt(s); 0 já pesquisados em campanhas "
        "anteriores, ~52471 ainda não",
        "busca pelos COBANs cadastrados (H9): 40 correspondentes em 1 prompt; 0 já pesquisados em campanhas "
        "anteriores, ~52.471 ainda não",
    ),
    (
        "coban-seeded search (H9): not used in a focused campaign (ameacas)",
        "busca pelos COBANs cadastrados (H9): não usada em campanha focada (ameacas)",
    ),
    ("20 URL(s) imported manually", "20 URLs importadas manualmente"),
    ("1 URL(s) importada(s) manualmente", "1 URL importada manualmente"),
    ("lens: reverse image search of 3 legacy-logo image(s)", "lens: busca reversa de 3 imagens da logo antiga"),
    ("lens: busca reversa de 1 imagem(ns) da logo antiga", "lens: busca reversa de 1 imagem da logo antiga"),
    ("crtsh: ativo (log público de Certificate Transparency)", "crtsh: ativo (busca em certificados públicos)"),
    ("vista por 2 fonte(s) / 1 hipótese(s)", "vista por 2 fontes e 1 hipótese"),
    (
        "LLM indisponível nesta execução: brand: 403: permission/auth error: There are issues with your billing account",
        "LLM indisponível nesta execução: marca: 403: erro de permissão/autenticação: There are issues with your "
        "billing account",
    ),
    ("CMN4935-ART14-II: NAO_APLICAVEL_SALVO_COBAN", "CMN4935-ART14-II: Não aplicável (salvo se for correspondente)"),
    ("coleta: BLOCKED", "coleta: Bloqueada"),
    # versões de prompt são fatos de auditoria: ficam com o nome original
    ("brand-analyst/v7", "brand-analyst/v7"),
    ("home page", "página inicial"),
    ("lookalike of bv.com.br (tld-swap)", "domínio parecido com bv.com.br (mesmo nome com outra extensão)"),
    ("domínio parecido com bv (combosquatting)", "domínio que combina a marca bv com palavra do setor"),
    ("base da associação: cnpj publicado na página", "Responsável identificado pelo CNPJ publicado na página."),
    (
        "(Pontuação de triagem: confusão 3.5, transparência 1.0; ordena casos, não prova.)",
        "(Pontuação de triagem: confusão 3,5; transparência 1,0; ordena casos, não prova.)",
    ),
    ("[Revisor: HUMAN_REVIEW] Questionamento.", "Revisor pediu revisão humana: Questionamento."),
    ("[Revisor: RECOLETAR] Coleta parcial.", "Revisor pediu nova coleta: Coleta parcial."),
    ("official institutional social profile", "perfil social oficial da instituição"),
]


@pytest.mark.parametrize(("antigo", "esperado"), EXEMPLOS_REAIS)
def test_traduz_textos_reais_do_banco(antigo: str, esperado: str) -> None:
    assert traduzir(antigo) == esperado


def test_mensagem_aninhada_traduz_por_dentro() -> None:
    antigo = "crtsh: disabled for this run -> fallback to remaining sources (crtsh: active (public Certificate Transparency log))"
    assert traduzir(antigo) == (
        "crtsh: desligado nesta execução, seguindo com as outras fontes "
        "(crtsh: ativo (busca em certificados públicos))"
    )
    assert traduzir("https://10.0.0.1/x -> non-public IP literal blocked: 10.0.0.1") == (
        "https://10.0.0.1/x -> IP literal não público bloqueado: 10.0.0.1"
    )


def test_justificativa_com_varios_codigos() -> None:
    antigo = (
        "Classifica-se como B_AMBIGUOUS_COMMUNICATION (REVIEW_REQUIRED / MEDIUM).\n"
        "[Revisor: DOWNGRADE] Reclassificado para NO_INDICATION com prioridade LOW."
    )
    assert traduzir(antigo) == (
        "Classifica-se como B · Comunicação ambígua (Requer revisão / Média).\n"
        "Revisor rebaixou: Reclassificado para Sem indício com prioridade Baixa."
    )


def test_texto_novo_em_portugues_fica_igual_ao_antigo() -> None:
    novo = "uso da identidade visual antiga da marca: mantido no mínimo REQUER_REVISAO com prioridade ALTA"
    antigo = "uso da identidade visual antiga da marca: mantido no mínimo REVIEW_REQUIRED com prioridade HIGH"
    assert (
        traduzir(novo)
        == traduzir(antigo)
        == ("uso da identidade visual antiga da marca: mantido no mínimo Requer revisão com prioridade Alta")
    )


@pytest.mark.parametrize(
    "texto",
    [
        # trechos citados da página, em maiúsculas: não são códigos
        "Uso de chamada comercial ('ALTA APROVAÇÃO – Taxa de 1.29% a.m.')",
        "O portal é operado por 404 DIGITAL MEDIA LTDA (CNPJ 36.963.903/0001-15)",
        "selo HIGH QUALITY no topo da página",
        # palavras comuns e referências de evidência
        "o acesso foi bloqueado pela política de rede (E-NET-BLOCKED); high e low em minúsculas",
        "erro de navegação: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x.com.br/financie",
        "a home page do site exibe o logotipo",
        "Texto já em português, sem nada para trocar.",
        # códigos dentro de URL são dado coletado
        "https://golpe.com/PENDING/login",
        "erro de navegação: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://golpe.com/HIGH/ALTA?s=CONFIRM",
        "hxxps://golpe[.]com/REVIEW_REQUIRED/",
        "golpe.com/PENDING/login",
        "busca?status=PENDING&nivel=ALTA",
    ],
)
def test_nao_mexe_em_texto_citado_nem_em_palavras_comuns(texto: str) -> None:
    assert traduzir(texto) == texto


def test_vazio_e_json_bruto() -> None:
    assert traduzir(None) == ""
    assert traduzir("") == ""
    detalhe = (
        '{"motivo": "official institutional domain", "prioridade": "ALTA", "salvaguardas": ["o modelo indicou '
        'NOT_RELEVANT/NO_INDICATION, mas as regras indicam B_AMBIGUOUS_COMMUNICATION"]}'
    )
    assert traduzir(detalhe, rotulos=False) == (
        '{"motivo": "domínio institucional oficial", "prioridade": "ALTA", "salvaguardas": ["o modelo indicou '
        'NAO_RELEVANTE/SEM_INDICIO, mas as regras indicam B_COMUNICACAO_AMBIGUA"]}'
    )


def test_registro_traduzido_e_copia_e_nao_altera_o_original() -> None:
    final = DecisaoFinal(
        id_candidata="c1",
        classificacao=ClassificacaoFinal.REQUER_REVISAO,
        prioridade=Prioridade.ALTA,
        caso=CasoCenario.B_COMUNICACAO_AMBIGUA,
        classe_identidade=ClasseIdentidade.IDENTIDADE_DESCONHECIDA,
        confianca_atribuicao=Nivel.BAIXA,
        confianca_extracao=Nivel.MEDIA,
        suficiencia_evidencia=SuficienciaEvidencia.PARCIAL,
        exige_revisao_humana=True,
        justificativa="[Revisor: CONFIRM] Confirma-se REVIEW_REQUIRED.",
        notas_salvaguarda=["página não inspecionada diretamente não pode receber NO_INDICATION: marcado INCONCLUSIVE"],
    )
    exibido = _traduzido(final, "justificativa", "notas_salvaguarda")
    assert exibido is not final
    assert exibido.justificativa == "Revisor confirmou: Confirma-se Requer revisão."
    assert exibido.notas_salvaguarda == [
        "página não inspecionada diretamente não pode receber Sem indício: marcado Inconclusiva"
    ]
    assert final.justificativa == "[Revisor: CONFIRM] Confirma-se REVIEW_REQUIRED."
    assert _traduzido(None, "justificativa") is None


def test_codigos_ao_redor_de_url_seguem_traduzidos() -> None:
    assert traduzir("REVIEW_REQUIRED em https://golpe.com/PENDING/login (HIGH)") == (
        "Requer revisão em https://golpe.com/PENDING/login (Alta)"
    )
    assert traduzir("NOT_RELEVANT/NO_INDICATION") == "Não relevante/Sem indício"


def test_requisicoes_bloqueadas_traduzem_so_o_motivo() -> None:
    evidencia = Evidencia(
        id_candidata="c1",
        tentativa=1,
        status=StatusColeta.OK,
        url_original="https://golpe.com/",
        requisicoes_bloqueadas=[
            "https://cdn.golpe.com/PENDING/a.js -> request cap reached",
            "https://golpe.com/HIGH -> blocked by URL policy: port 8080 not allowed",
        ],
    )
    exibida = _evidencia_traduzida(evidencia)
    assert exibida is not None
    assert exibida.requisicoes_bloqueadas == [
        "https://cdn.golpe.com/PENDING/a.js -> limite de requisições atingido",
        "https://golpe.com/HIGH -> bloqueada pela política de URL: porta 8080 não permitida",
    ]
    assert evidencia.requisicoes_bloqueadas[0].endswith("request cap reached")


def test_frases_antigas_iguais_aparecem_uma_vez() -> None:
    final = DecisaoFinal(
        id_candidata="c1",
        classificacao=ClassificacaoFinal.REQUER_REVISAO,
        prioridade=Prioridade.ALTA,
        caso=CasoCenario.B_COMUNICACAO_AMBIGUA,
        classe_identidade=ClasseIdentidade.COBAN_CONFIRMADO,
        confianca_atribuicao=Nivel.ALTA,
        confianca_extracao=Nivel.MEDIA,
        suficiencia_evidencia=SuficienciaEvidencia.SUFICIENTE,
        exige_revisao_humana=True,
        justificativa="x",
        limitacoes=[
            "associação página→CNPJ baseada na publicação do CNPJ na própria página",
            "base da associação: cnpj publicado na página",
        ],
    )
    assert _traduzido(final, "limitacoes").limitacoes == ["Responsável identificado pelo CNPJ publicado na página."]


def test_numeros_e_resumos_para_exibicao() -> None:
    assert num_br(52471) == "52.471" and num_br(0.83) == "0,83" and num_br("5.0") == "5,0" and num_br(3.5, 1) == "3,5"
    assert num_br("abc") == "abc"
    assert resumir_consultas({"grounding:OK": 20, "crtsh:VAZIA": 2, "crtsh:ERRO": 1, "lens:VAZIA": 1}) == (
        "grounding: 20 ok; crtsh: 2 vazias, 1 com erro; lens: 1 vazia"
    )
    assert rotulo_fonte("importacao:texto") == "Lista importada (texto)" and rotulo_fonte("import") == "URLs importadas"
    assert descrever_chave_entidade("cnpj:13554821000111") == "CNPJ 13.554.821/0001-11"
    assert descrever_analisador("gemini-3.8-flash", "brand-analyst/v6") == (
        "modelo gemini-3.8-flash · prompt brand-analyst/v6"
    )
