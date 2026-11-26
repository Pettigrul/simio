from __future__ import annotations

import re
from enum import Enum
from typing import Any

# O banco guarda os códigos (INDICIO_FORTE, D_PERSONIFICACAO_FORTE...); HTML, PDF e Excel mostram estes rótulos.
ROTULOS: dict[str, str] = {
    # classificação final
    "INDICIO_FORTE": "Indício forte",
    "REQUER_REVISAO": "Requer revisão",
    "INCONCLUSIVA": "Inconclusiva",
    "SEM_INDICIO": "Sem indício",
    # prioridade, confiança, força, severidade
    "ALTA": "Alta",
    "MEDIA": "Média",
    "BAIXA": "Baixa",
    "NENHUMA": "Nenhuma",
    # cenário (caso) do analisador
    "A_MENCAO_COMERCIAL_LEGITIMA": "A · Menção comercial legítima",
    "B_COMUNICACAO_AMBIGUA": "B · Comunicação ambígua",
    "C_POSSIVEL_CANAL_OFICIAL": "C · Possível canal oficial",
    "D_PERSONIFICACAO_FORTE": "D · Personificação forte",
    "E_EVIDENCIA_INSUFICIENTE": "E · Evidência insuficiente",
    "NAO_RELEVANTE": "Não relevante",
    # suficiência da evidência
    "SUFICIENTE": "Suficiente",
    "PARCIAL": "Parcial",
    "INSUFICIENTE": "Insuficiente",
    # identidade do responsável
    "COBAN_CONFIRMADO": "Correspondente confirmado",
    "COBAN_POSSIVEL": "Possível correspondente",
    "OUTRO_TERCEIRO": "Outra empresa",
    "IDENTIDADE_DESCONHECIDA": "Sem vínculo confirmado",
    # dimensões e sinais da análise de marca
    "IDENTIDADE_VISUAL": "Identidade visual",
    "IDENTIDADE_COMERCIAL_DECLARADA": "Identidade comercial declarada",
    "TRANSPARENCIA_CORRESPONDENTE": "Transparência de correspondente",
    "LINGUAGEM_INSTITUCIONAL": "Linguagem institucional",
    "USO_LOGO": "Uso de logotipo",
    "JORNADA_ATENDIMENTO": "Jornada de atendimento",
    "CANAIS_CONTATO": "Canais de contato",
    "CONFUSAO_CONSUMIDOR": "Confusão do consumidor",
    "CONFUSAO": "Confusão",
    "LEGITIMO": "Legítimo",
    "NEUTRO": "Neutro",
    "NAO_AVALIADO": "Não avaliado",
    # coleta
    "OK": "OK",
    "FALHOU": "Falhou",
    "BLOQUEADA": "Bloqueada",
    "NAO_COLETADA": "Não coletada",
    # andamento da candidata e da campanha
    "DESCOBERTA": "Descoberta",
    "NA_FILA": "Na fila",
    "COLETANDO": "Coletando",
    "COLETADA": "Coletada",
    "ANALISANDO": "Analisando",
    "REVISANDO": "Revisando",
    "CONCLUIDA": "Concluída",
    "EM_EXECUCAO": "Em execução",
    "INTERROMPIDA": "Interrompida",
    "PAUSADA": "Pausada",
    "PAUSADA_ORCAMENTO": "Pausada (limite de uso)",
    # conformidade
    "NORMATIVO": "Normativo",
    "CONTRATUAL": "Contratual",
    "POLITICA_INTERNA": "Política interna",
    "HIPOTESE_NAO_CONFORMIDADE": "Hipótese de não conformidade",
    "HIPOTESE_CONDICIONADA_VINCULO": "Hipótese condicionada ao vínculo",
    "SEM_INDICIO_OBSERVADO": "Sem indício observado",
    "NAO_AVALIAVEL": "Não avaliável",
    "NAO_APLICAVEL_SALVO_COBAN": "Não aplicável (salvo se for correspondente)",
    # revisão crítica e revisão humana
    "CONFIRMAR": "Confirmada",
    "REBAIXAR": "Rebaixada",
    "RECOLETAR": "Pediu nova coleta",
    "REVISAO_HUMANA": "Pediu revisão humana",
    "PENDENTE": "Pendente",
    "NAO_EXIGIDA": "Não exigida",
    # temas das contestações do revisor
    "atribuicao_dominio": "Atribuição do domínio",
    "suficiencia_evidencia": "Suficiência da evidência",
    "interpretacao_excessiva": "Interpretação excessiva",
    "evidencias_contraditorias": "Evidência contraditória",
    "declaracao_legitima": "Divulgação legítima",
    "contexto_visual_nao_analisado": "Contexto visual não analisado",
    "editorial_ou_plataforma_terceiros": "Conteúdo editorial ou plataforma de terceiros",
    "intermediacao_multibanco": "Intermediação de vários bancos",
    # os mesmos temas, como as versões antigas gravaram
    "domain_attribution": "Atribuição do domínio",
    "evidence_sufficiency": "Suficiência da evidência",
    "over_interpretation": "Interpretação excessiva",
    "contradictory_evidence": "Evidência contraditória",
    "legitimate_disclosure": "Divulgação legítima",
    "authorized_communication": "Comunicação autorizada",
    "unanalyzed_visual_context": "Contexto visual não analisado",
    "editorial_or_third_party_platform": "Conteúdo editorial ou plataforma de terceiros",
    "multi_bank_intermediation": "Intermediação de vários bancos",
    # níveis de ameaça (a triagem grava em maiúsculas)
    "CRÍTICO": "Crítico",
    "ALTO": "Alto",
    "MÉDIO": "Médio",
    "BAIXO": "Baixo",
    "SEM AMEAÇA": "Sem ameaça",
    # itens do catálogo de evidências
    "titulo": "Título",
    "meta": "Metadado",
    "cabecalho_secao": "Título de seção",
    "nav": "Menu",
    "cta": "Botão / chamada",
    "link": "Link",
    "contato": "Contato",
    "imagem": "Imagem",
    "captura": "Captura de tela",
    "visual": "Comparação de imagem",
    "dominio": "Domínio",
    "formulario": "Formulário",
    "rede": "Rede",
    "texto": "Texto",
    # onde o item foi visto na página
    "pagina": "página",
    "topo": "topo",
    "rodape": "rodapé",
    "menu": "menu",
    "area_visivel": "área visível",
    "cabecalho_html": "cabeçalho HTML",
    "url": "endereço",
    "coleta": "coleta",
    "indice_busca": "índice de busca",
    # os mesmos locais, como as versões anteriores gravaram
    "head": "cabeçalho HTML",
    "page": "página",
    "header": "topo",
    "footer": "rodapé",
    "viewport": "área visível",
    "collection": "coleta",
    "search_index": "índice de busca",
    # artefatos da coleta
    "captura_viewport": "Captura da área visível",
    "captura_completa": "Captura da página inteira",
    "html": "Código-fonte",
    "dados_pagina": "Dados extraídos da página",
    # fatos de identidade
    "cnpj": "CNPJ",
    "razao_social": "Razão social",
    "nome_fantasia": "Nome fantasia",
    "telefone": "Telefone",
    "whatsapp": "WhatsApp",
    "email": "E-mail",
    "perfil_social": "Perfil em rede social",
    "endereco": "Endereço",
    "declaracao_parceria": "Declaração de parceria",
    "declaracao_correspondente": "Declaração de correspondente",
    "nome_instituicao_exibido": "Nome da instituição exibido",
    # como a página foi casada com o cadastro de correspondentes
    "raiz_cnpj": "raiz do CNPJ",
    "nome": "nome",
    "cnpj_root": "raiz do CNPJ",
    "name": "nome",
    # situação das consultas de descoberta
    "VAZIA": "Vazia",
    "ERRO": "Erro",
}

# Tom de cor de cada valor: crit, alto, medio, baixo, ok. Valores sem tom ficam neutros.
TONS: dict[str, str] = {
    "CRÍTICO": "crit",
    "ALTO": "alto",
    "MÉDIO": "medio",
    "BAIXO": "baixo",
    "SEM AMEAÇA": "ok",
    "INDICIO_FORTE": "alto",
    "REQUER_REVISAO": "medio",
    "INCONCLUSIVA": "baixo",
    "SEM_INDICIO": "ok",
    "ALTA": "alto",
    "MEDIA": "medio",
    "BAIXA": "baixo",
    "CONFUSAO": "alto",
    "LEGITIMO": "ok",
    "HIPOTESE_NAO_CONFORMIDADE": "alto",
    "HIPOTESE_CONDICIONADA_VINCULO": "medio",
    "SEM_INDICIO_OBSERVADO": "ok",
}

# (texto, fundo) de cada tom, sem o "#": a mesma paleta vale para o CSS dos relatórios e para o Excel.
PALETA: dict[str, tuple[str, str]] = {
    "crit": ("912018", "FDE7E4"),
    "alto": ("B42318", "FEF1EF"),
    "medio": ("B54708", "FEF6E7"),
    "baixo": ("475467", "F2F4F7"),
    "ok": ("067647", "ECFDF3"),
}
COR_MARCA = "1C3FAA"

_TOM_POR_ROTULO = {ROTULOS[k]: t for k, t in TONS.items()}


def _codigo(valor: object) -> str:
    if isinstance(valor, Enum):
        return str(valor.value)
    return "" if valor is None else str(valor)


def rotulo(valor: object) -> str:
    """Rótulo legível; valores desconhecidos (texto livre, códigos novos) passam como estão."""
    codigo = _codigo(valor)
    return ROTULOS.get(codigo, codigo)


def tom(valor: object) -> str:
    """Tom de cor do valor, aceitando o código gravado ou o rótulo já traduzido."""
    codigo = _codigo(valor)
    return TONS.get(codigo) or _TOM_POR_ROTULO.get(codigo, "")


def num_br(valor: object, casas: int | None = None) -> str:
    """Número no formato brasileiro: ``52471`` -> ``52.471``, ``0.83`` -> ``0,83``. Texto que não é número passa
    como está."""
    if valor is None or isinstance(valor, bool):
        return "" if valor is None else str(valor)
    if isinstance(valor, str):
        m = re.fullmatch(r"(-?\d+)(?:\.(\d+))?", valor.strip())
        if not m:
            return valor
        if casas is None:
            casas = len(m.group(2) or "")
        numero: float = float(valor) if m.group(2) else int(m.group(1))
    elif isinstance(valor, (int, float)):
        numero = valor
        if casas is None:
            casas = 0 if isinstance(valor, int) else 2
    else:
        return str(valor)
    texto = f"{abs(numero):,.{casas}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"-{texto}" if numero < 0 and texto.strip("0,.") else texto


def plural(n: int, singular: str, varios: str) -> str:
    return f"{num_br(n)} {singular if n == 1 else varios}"


_DADOS_PEDIDOS = {
    "cpf": "CPF",
    "cvv": "CVV",
    "token/código": "token ou código",
    "agência/conta": "agência e conta",
}


def dados_pedidos(tipos: list[str] | None) -> str:
    return ", ".join(_DADOS_PEDIDOS.get(t, t) for t in tipos or [])


def rotulo_fonte(fonte: str | None) -> str:
    """Fonte de descoberta para exibição (``importacao:arquivo:urls.txt`` -> ``Lista importada (urls.txt)``)."""
    fonte = fonte or ""
    for prefixo in ("importacao:arquivo:", "import:file:"):
        if fonte.startswith(prefixo):
            return f"Lista importada ({fonte[len(prefixo) :]})"
    if fonte in ("importacao:texto", "import:text"):
        return "Lista importada (texto)"
    if fonte in ("importacao", "import"):
        return "URLs importadas"
    return fonte


def formatar_cnpj(cnpj: str | None) -> str:
    digitos = re.sub(r"\D", "", cnpj or "")
    if len(digitos) != 14:
        return cnpj or ""
    return f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:]}"


def descrever_chave_entidade(chave: str | None) -> str:
    # A chave (cnpj:..., domain:...) é interna e serve para juntar a mesma empresa entre campanhas.
    tipo, _, valor = (chave or "").partition(":")
    if tipo == "cnpj":
        return f"CNPJ {formatar_cnpj(valor)}"
    if tipo == "domain":
        return f"domínio {valor}"
    if tipo == "social":
        rede, _, perfil = valor.partition(":")
        return f"perfil {perfil} em {rede}" if perfil else f"perfil em {rede}"
    return valor or tipo


def descrever_cadastro(correspondencia: Any) -> str:
    """``Cadastro X (ref. 06/10/2026), por CNPJ: NOME (CNPJ)``."""
    if not correspondencia:
        return ""
    fonte = re.sub(r"\(ref\. (\d{4})-(\d{2})-(\d{2})\)", r"(ref. \3/\2/\1)", correspondencia.fonte or "sem nome")
    registro = correspondencia.registro or {}
    nome = registro.get("nome") or registro.get("name") or ""
    cnpj = formatar_cnpj(registro.get("cnpj"))
    quem = f"{nome} ({cnpj})" if nome and cnpj else (nome or cnpj)
    texto = f"Cadastro {fonte}, por {rotulo(correspondencia.casado_por)}"
    if quem:
        texto += f": {quem}"
    if not correspondencia.oficial:
        texto += " (não conta como confirmação)"
    return texto


def descrever_analisador(analisador: str | None, versao_prompt: str | None = None) -> str:
    """``modelo gemini-3.8-flash · prompt brand-analyst/v6``. A versão do prompt fica com o nome gravado (é fato
    de auditoria)."""
    analisador = analisador or ""
    if analisador.startswith("heuristica"):
        texto = "regras locais, sem modelo"
        if "indice-grounding" in analisador:
            texto += ", sobre o índice do grounding"
    elif analisador.startswith("foco:"):
        texto = f"triagem da campanha {analisador[len('foco:') :]}, sem modelo"
    else:
        texto = f"modelo {analisador}" if analisador else ""
    if versao_prompt:
        texto = f"{texto} · prompt {versao_prompt}" if texto else f"prompt {versao_prompt}"
    return texto


_SITUACOES_CONSULTA = {"OK": ("ok", "ok"), "VAZIA": ("vazia", "vazias"), "ERRO": ("com erro", "com erro")}


def resumir_consultas(consultas: dict[str, int] | None) -> str:
    """``{"grounding:OK": 20, "crtsh:VAZIA": 2}`` -> ``grounding: 20 ok; crtsh: 2 vazias``."""
    por_fonte: dict[str, dict[str, int]] = {}
    for chave, n in (consultas or {}).items():
        fonte, _, situacao = chave.rpartition(":")
        situacao = {"EMPTY": "VAZIA", "ERROR": "ERRO"}.get(situacao, situacao)
        contagem = por_fonte.setdefault(fonte, {})
        contagem[situacao] = contagem.get(situacao, 0) + n
    ordem = list(_SITUACOES_CONSULTA)
    partes = []
    for fonte, contagem in por_fonte.items():
        itens = []
        for situacao in sorted(contagem, key=lambda s: ordem.index(s) if s in ordem else len(ordem)):
            um, varios = _SITUACOES_CONSULTA.get(situacao, (situacao.lower(), situacao.lower()))
            itens.append(plural(contagem[situacao], um, varios))
        partes.append(f"{fonte}: {', '.join(itens)}")
    return "; ".join(partes)
