from __future__ import annotations

from enum import Enum

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
    "IDENTIDADE_DESCONHECIDA": "Não identificado",
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
    "CONFIRMAR": "Confirmar",
    "REBAIXAR": "Rebaixar",
    "RECOLETAR": "Coletar de novo",
    "REVISAO_HUMANA": "Revisão humana",
    "PENDENTE": "Pendente",
    "NAO_EXIGIDA": "Não exigida",
    # temas das contestações do revisor (gravados em inglês pelas versões antigas)
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
    "head": "cabeçalho HTML",
    "page": "página",
    "header": "topo",
    "footer": "rodapé",
    "viewport": "área visível",
    "url": "endereço",
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
