from __future__ import annotations

import enum
import re
from collections.abc import Callable
from functools import lru_cache

from simio.nucleo import modelos
from simio.nucleo.traducao_legado import ETAPA, STATUS_CAMPANHA, STATUS_REVISAO_HUMANA, VALORES_ENUM
from simio.provedores.typosquat import descrever_semelhanca
from simio.relatorios.rotulos import num_br, plural, rotulo

Grupos = dict[str, str]


def _p(grupos: Grupos, nome: str, um: str, varios: str) -> str:
    return plural(int(grupos[nome]), um, varios)


def _urls_importadas(g: Grupos) -> str:
    return f"{_p(g, 'n', 'URL importada', 'URLs importadas')} manualmente"


def _busca_reversa(g: Grupos) -> str:
    return f"lens: busca reversa de {_p(g, 'n', 'imagem', 'imagens')} da logo antiga"


def _busca_coban(g: Grupos) -> str:
    return (
        f"busca pelos COBANs cadastrados (H9): {_p(g, 'n', 'correspondente', 'correspondentes')} em "
        f"{_p(g, 'p', 'prompt', 'prompts')}; {g['pesquisados']} já pesquisados em campanhas anteriores, "
        f"~{g['restantes']} ainda não"
    )


def _vista_por(g: Grupos) -> str:
    return f"vista por {_p(g, 'n_fontes', 'fonte', 'fontes')} e {_p(g, 'n_hipoteses', 'hipótese', 'hipóteses')}"


def _semelhante(g: Grupos) -> str:
    return f"domínio {descrever_semelhanca(g['semelhante_a'], g['fuzzer'])}"


_IDENTIFICADO_POR_CNPJ = "Responsável identificado pelo CNPJ publicado na página."
_REDE_OCUPADA = "a página continuou carregando; a coleta seguiu assim mesmo"
_CRTSH = "crtsh: ativo (busca em certificados públicos)"

# Frases fixas que as versões antigas gravaram em inglês no banco. O banco e os manifestos não mudam
# (cadeia de custódia): a troca é só na hora de exibir. {x} é um trecho variável, copiado como está.
# Também entram frases em português que mudaram de redação depois de gravadas.
FRASES: tuple[tuple[str, str | Callable[[Grupos], str]], ...] = (
    # núcleo / orquestrador
    ("interrupted {n} times in {status}; marked FALHOU", "interrompida {n} vezes em {status}; marcada como FALHOU"),
    ("recovered after interruption", "recuperada após interrupção"),
    ("{atual} -> {alvo} is not allowed", "transição não permitida: {atual} -> {alvo}"),
    ("SSRF protection bypassed for these hosts", "proteção contra SSRF desligada para estes hosts"),
    ("retry requested by operator", "nova tentativa pedida pelo operador"),
    (
        "coban-seeded search (H9): not used in a focused campaign ({foco})",
        "busca pelos COBANs cadastrados (H9): não usada em campanha focada ({foco})",
    ),
    (
        "offline mode: automatic discovery and AI analysis disabled; only imported URLs are processed",
        "modo offline: sem descoberta automática nem análise por IA; só as URLs importadas entram",
    ),
    ("prioritised (score {pontuacao})", "priorizada (pontuação {pontuacao})"),
    ("priorizada (pontuação {pontuacao})", "priorizada (pontuação {pontuacao})"),
    (
        "coban-seeded search (H9): SIMIO_CAMINHO_CADASTRO_COBAN not set; skipped",
        "busca pelos COBANs cadastrados (H9): SIMIO_CAMINHO_CADASTRO_COBAN não definido; pulada",
    ),
    (
        "coban-seeded search (H9): {n} registered correspondents in {p} prompt(s); {pesquisados} searched in "
        "earlier campaigns, ~{restantes} not yet searched",
        _busca_coban,
    ),
    (
        "busca pelos COBANs cadastrados (H9): {n} correspondentes em {p} prompt(s); {pesquisados} já pesquisados "
        "em campanhas anteriores, ~{restantes} ainda não",
        _busca_coban,
    ),
    ("Vertex AI not configured; heuristic analysis used", "Vertex AI não configurado; análise heurística"),
    ("unexpected error: {tipo}: {erro}", "erro inesperado: {tipo}: {erro}"),
    ("collector error, retry {tentativa}", "erro no coletor, nova tentativa ({tentativa})"),
    ("collector error: {erro}", "erro no coletor: {erro}"),
    ("transient collection failure, retry {tentativa}", "falha temporária na coleta, nova tentativa ({tentativa})"),
    ("no evidence stored for a COLETADA candidate", "candidata COLETADA sem evidência gravada"),
    ("reviewer requested recollection: {motivo}", "revisor pediu nova coleta: {motivo}"),
    (
        "paused: budget exhausted (paid results kept for resume)",
        "pausada: limite de uso atingido (o que já foi pago fica guardado)",
    ),
    (
        "LLM unavailable at {etapa}; retrying with heuristic analysis",
        "LLM indisponível em {etapa}; nova tentativa com análise heurística",
    ),
    ("{etapa} error, retry {tentativa}: {tipo}", "erro em {etapa}, nova tentativa ({tentativa}): {tipo}"),
    ("{etapa} failed: {tipo}: {erro}", "{etapa} falhou: {tipo}: {erro}"),
    ("LLM indisponível nesta execução: {etapa}: ", "LLM indisponível nesta execução: {etapa}: "),
    # Gravada uma vez, na reanálise que endureceu a regra de credenciais.
    (
        "credential rule tightened (brand must be in domain/title/header/logo; platform login walls ignored)",
        "regra de credenciais endurecida (a marca precisa estar no domínio, título, cabeçalho ou logo; "
        "telas de login de plataformas ignoradas)",
    ),
    # descoberta
    ("official institutional domain", "domínio institucional oficial"),
    ("official institutional social profile", "perfil social oficial da instituição"),
    ("social platform tag/search index page", "página de tag/busca de rede social"),
    ("direct binary/PDF document URL", "link direto para PDF/arquivo"),
    (
        "public business directory entry for the institution itself",
        "ficha da própria instituição em diretório de empresas",
    ),
    (
        "generic free-builder boleto/phishing page outside correspondent scope",
        "página de boleto/phishing em construtor gratuito, fora do escopo de correspondentes",
    ),
    ("free website builder (common for phishing kits)", "construtor de sites gratuito (comum em kits de phishing)"),
    (
        "home page of a host recovered from an opaque Google result (matched page unknown)",
        "página inicial recuperada de link opaco do Google (página exata desconhecida)",
    ),
    ("brand pattern in domain ({padrao})", "padrão da marca no domínio ({padrao})"),
    ("ephemeral/free-hosting domain", "hospedagem gratuita/efêmera"),
    ("brand mentioned in title/snippet", "marca citada no título/trecho"),
    ("official-channel language in title/snippet", "linguagem de canal oficial no título/trecho"),
    ("sensitive billing/settlement service in title/snippet", "serviço sensível (boleto/quitação) no título/trecho"),
    ("correspondent disclosure in snippet", "declaração de correspondente no trecho"),
    ("multi-bank partner list in snippet", "lista de vários bancos parceiros no trecho"),
    ("likely non-bank homonym context in title/snippet", "provável homônimo fora do setor bancário"),
    ("editorial/blog/help article (de-prioritised)", "artigo de blog/notícia/ajuda (rebaixado)"),
    ("lookalike of {semelhante_a} ({fuzzer})", _semelhante),
    ("domínio parecido com {semelhante_a} ({fuzzer})", _semelhante),
    (
        "image visually similar to the legacy logo (reverse image search)",
        "imagem parecida com a logo antiga (busca reversa)",
    ),
    ("seen by {n_fontes} source(s) / {n_hipoteses} hypothesis(es)", _vista_por),
    ("vista por {n_fontes} fonte(s) / {n_hipoteses} hipótese(s)", _vista_por),
    ("manually imported by operator", "importada manualmente pelo operador"),
    ("news/review/regulator domain (de-prioritised)", "domínio de notícia/reclamação/regulador (rebaixado)"),
    (
        "individual marketplace vehicle listing (de-prioritised)",
        "anúncio individual de veículo em marketplace (rebaixado)",
    ),
    ("ancillary legal/sitemap page (de-prioritised)", "página acessória (privacidade/termos/sitemap) (rebaixada)"),
    (
        "additional page on same registrable domain (#{contagem}, -{penalidade})",
        "página extra do mesmo domínio (#{contagem}, -{penalidade})",
    ),
    ("invalid/blocked URL: {erro}", "URL inválida/bloqueada: {erro}"),
    ("{conector}: not used in a focused campaign ({foco})", "{conector}: não usado em campanha com foco ({foco})"),
    ("{n} URL(s) imported manually", _urls_importadas),
    ("{n} URL(s) importada(s) manualmente", _urls_importadas),
    (
        "NO automatic discovery mechanism is available. Configure Vertex AI (SIMIO_PROJETO_GCP + SIMIO_MODELO_ANALISE) "
        "for Google Search Grounding, or an optional connector key (SIMIO_CHAVE_API_BRAVE / SIMIO_CHAVE_API_SERPER / "
        "SIMIO_CHAVE_API_URLSCAN), or import URLs with --arquivo-urls / --urls.",
        "Nenhuma fonte de descoberta automática disponível. Configure o Vertex AI (SIMIO_PROJETO_GCP + "
        "SIMIO_MODELO_ANALISE) para o grounding, uma chave de conector (SIMIO_CHAVE_API_BRAVE / SIMIO_CHAVE_API_SERPER "
        "/ SIMIO_CHAVE_API_URLSCAN) ou importe URLs com --arquivo-urls / --urls.",
    ),
    ("{conector}: stopped, {erro}", "{conector}: interrompido, {erro}"),
    (
        "{conector}: disabled for this run -> fallback to remaining sources ({motivo})",
        "{conector}: desligado nesta execução, seguindo com as outras fontes ({motivo})",
    ),
    ("{conector}: API key not configured (optional)", "{conector}: chave de API não configurada (opcional)"),
    ("{conector}: configured", "{conector}: configurado"),
    ("lens: Serper API key not configured (optional)", "lens: chave do Serper não configurada (opcional)"),
    ("lens: no legacy_logo_search_urls in the brand profile", "lens: perfil da marca sem urls_busca_logo_antiga"),
    ("lens: reverse image search of {n} legacy-logo image(s)", _busca_reversa),
    ("lens: busca reversa de {n} imagem(ns) da logo antiga", _busca_reversa),
    ("crtsh: active (public Certificate Transparency log)", _CRTSH),
    ("crtsh: ativo (log público de Certificate Transparency)", _CRTSH),
    ("Issuer: {emissor}", "Emissor: {emissor}"),
    (
        "grounding: Vertex AI not configured (SIMIO_PROJETO_GCP / model) or offline",
        "grounding: Vertex AI não configurado (SIMIO_PROJETO_GCP / modelo) ou modo offline",
    ),
    ("grounding: no discovery/analysis model configured", "grounding: nenhum modelo de descoberta/análise configurado"),
    ("grounding: disabled after error: {motivo}", "grounding: desligado após erro: {motivo}"),
    ("grounding: model={modelo}", "grounding: modelo {modelo}"),
    ("grounding: modelo={modelo}", "grounding: modelo {modelo}"),
    (
        "search-results page reference (not a target page)",
        "página de resultados de busca (não é página-alvo)",
    ),
    ("redirect not resolved (HTTP {status})", "redirecionamento não resolvido (HTTP {status})"),
    ("policy: {erro}", "política: {erro}"),
    (
        "typosquat: no typosquat_seeds / typosquat_combo_labels in the brand profile",
        "typosquat: perfil da marca sem sementes_typosquat / rotulos_combo_typosquat",
    ),
    ("typosquat: disabled: {motivo}", "typosquat: desligado: {motivo}"),
    (
        "typosquat: active (DNS permutations of the official domains)",
        "typosquat: ativo (variações em DNS dos domínios oficiais)",
    ),
    (
        "the DNS resolver answers for non-existent domains (NXDOMAIN hijacking); every permutation would look "
        "registered. Use a resolver without redirection.",
        "o resolvedor DNS responde para domínios inexistentes (sequestro de NXDOMAIN); toda variação pareceria "
        "registrada. Use um resolvedor sem redirecionamento.",
    ),
    # análise (LLM e orçamento)
    (
        "LLM request budget exhausted ({requisicoes}/{max_requisicoes})",
        "orçamento de requisições ao LLM esgotado ({requisicoes}/{max_requisicoes})",
    ),
    (
        "LLM token budget insufficient ({tokens} used + {reservados} reserved + ~{estimados} estimated > {max_tokens})",
        "orçamento de tokens do LLM insuficiente ({tokens} usados + {reservados} reservados + ~{estimados} estimados "
        "> {max_tokens})",
    ),
    ("google-genai is not installed", "google-genai não está instalado"),
    ("SIMIO_PROJETO_GCP is not set", "SIMIO_PROJETO_GCP não definido"),
    ("{codigo}: permission/auth error: {mensagem}", "{codigo}: erro de permissão/autenticação: {mensagem}"),
    ("404: model or endpoint not found: {mensagem}", "404: modelo ou endpoint não encontrado: {mensagem}"),
    ("credentials error: {mensagem}", "erro de credenciais: {mensagem}"),
    ("empty response (finish_reason={motivo})", "resposta vazia (finish_reason={motivo})"),
    # coleta e política de URLs (aparecem dentro de outras mensagens: "{url} -> {motivo}", "policy: ...")
    ("{url} -> request cap reached", "{url} -> limite de requisições atingido"),
    ("blocked by URL policy: {motivo}", "bloqueada pela política de URL: {motivo}"),
    ("page budget of {segundos}s exceeded", "tempo limite da página ({segundos}s) excedido"),
    ("collection error: {tipo}: {mensagem}", "erro na coleta: {tipo}: {mensagem}"),
    ("navigation timeout", "timeout de navegação"),
    ("navigation error: {mensagem}", "erro de navegação: {mensagem}"),
    ("network did not become idle (continuing)", _REDE_OCUPADA),
    ("a rede não ficou ociosa (coleta seguiu)", _REDE_OCUPADA),
    ("request cap reached", "limite de requisições atingido"),
    ("final URL violates policy: {motivo}", "URL final viola a política: {motivo}"),
    ("HTTP {status}: page content is an error page", "HTTP {status}: o conteúdo é uma página de erro"),
    (
        "possible bot challenge/CAPTCHA detected; no bypass attempted",
        "possível desafio anti-bot/CAPTCHA detectado; nenhuma tentativa de contorno",
    ),
    ("full-page screenshot failed: {mensagem}", "falha no screenshot da página completa: {mensagem}"),
    ("HTML truncated to {n} bytes", "HTML truncado em {n} bytes"),
    ("empty or oversized URL", "URL vazia ou longa demais"),
    ("control characters or backslash in URL", "caracteres de controle ou barra invertida na URL"),
    ("malformed URL: {erro}", "URL malformada: {erro}"),
    ("scheme not allowed: (none)", "esquema não permitido: (nenhum)"),
    ("scheme not allowed: {esquema}", "esquema não permitido: {esquema}"),
    ("credentials in URL are not allowed", "credenciais na URL não são permitidas"),
    ("missing host", "URL sem host"),
    ("port {porta} not allowed", "porta {porta} não permitida"),
    ("internal hostname blocked: {host}", "hostname interno bloqueado: {host}"),
    ("single-label hostname blocked: {host}", "hostname sem domínio bloqueado: {host}"),
    ("non-public IP literal blocked: {host}", "IP literal não público bloqueado: {host}"),
    ("{host} did not resolve", "{host} não resolveu"),
    ("{host} resolves to non-public address {ip}", "{host} resolve para endereço não público {ip}"),
    ("DNS resolution failed for {host}: {erro}", "falha na resolução DNS de {host}: {erro}"),
    (
        "browser connected to non-public address {ip} for {host}",
        "navegador conectou em endereço não público {ip} para {host}",
    ),
    ("per-page transfer cap of {n} bytes reached", "limite de transferência por página ({n} bytes) atingido"),
    # análise heurística
    ("Triagem heurística — cenário ", "Triagem heurística, cenário "),
    (
        "(Pontuação de triagem: confusão {confusao}, transparência {transparencia}; ordena casos, não prova.)",
        "(Pontuação de triagem: confusão {confusao}; transparência {transparencia}; ordena casos, não prova.)",
    ),
)

# Curtas ou genéricas demais para trocar no meio de outro texto: só quando são o texto inteiro.
FRASES_INTEIRAS: tuple[tuple[str, str], ...] = (
    ("home page", "página inicial"),
    ("unversioned", "sem-versao"),
    ("associação página→CNPJ baseada na publicação do CNPJ na própria página", _IDENTIFICADO_POR_CNPJ),
    ("base da associação: cnpj publicado na página", _IDENTIFICADO_POR_CNPJ),
)

# Como casar cada {x}: número, código/palavra, trecho sem espaço (URL, host) ou texto livre (o resto).
_NUMERICOS = {
    "n",
    "p",
    "tentativa",
    "porta",
    "segundos",
    "contagem",
    "penalidade",
    "pontuacao",
    "n_fontes",
    "n_hipoteses",
    "pesquisados",
    "restantes",
    "requisicoes",
    "max_requisicoes",
    "tokens",
    "reservados",
    "estimados",
    "max_tokens",
    "confusao",
    "transparencia",
}
# Números que a exibição passa para o formato brasileiro (5.0 -> 5,0; 52471 -> 52.471).
_NUMEROS_BR = {"pontuacao", "restantes", "confusao", "transparencia"}
_PALAVRAS = {"status", "codigo", "atual", "alvo", "conector", "tipo", "versao"}
_SEM_ESPACO = {"url", "host", "ip", "esquema", "modelo", "semelhante_a", "fuzzer"}
_ETAPAS_ANTIGAS = "|".join(sorted(map(re.escape, ETAPA), key=len, reverse=True))
_MARCADOR = re.compile(r"\{(\w+)\}")


def _regex_marcador(nome: str, no_fim: bool) -> str:
    if nome == "etapa":
        return f"(?P<etapa>{_ETAPAS_ANTIGAS})"
    if nome in _NUMERICOS:
        return rf"(?P<{nome}>-?\d+(?:[.,]\d+)*)"
    if nome in _PALAVRAS:
        return rf"(?P<{nome}>[\w.-]+)"
    if nome in _SEM_ESPACO:
        return rf"(?P<{nome}>[^\s;,]+)"
    return rf"(?P<{nome}>[^\n]+)" if no_fim else rf"(?P<{nome}>.+?)"


def _compilar(antigo: str, inteiro: bool) -> tuple[str, re.Pattern[str]]:
    partes, pos = [], 0
    for m in _MARCADOR.finditer(antigo):
        partes.append(re.escape(antigo[pos : m.start()]))
        partes.append(_regex_marcador(m.group(1), no_fim=m.end() == len(antigo)))
        pos = m.end()
    partes.append(re.escape(antigo[pos:]))
    corpo = "".join(partes)
    if inteiro:
        corpo = rf"\A{corpo}\Z"
    else:
        if re.match(r"\w", antigo):
            corpo = r"(?<!\w)" + corpo
        elif antigo.startswith("{"):
            corpo = r"(?<![\w.-])" + corpo
        if re.search(r"\w\Z", antigo):
            corpo += r"(?!\w)"
    # Trecho fixo mais longo: um teste "in" barato antes de rodar a regex.
    trecho = max(_MARCADOR.split(antigo)[::2], key=len)
    return trecho, re.compile(corpo, re.DOTALL)


def _substituto(novo: str | Callable[[Grupos], str]) -> Callable[[re.Match[str]], str]:
    def trocar(m: re.Match[str]) -> str:
        grupos = m.groupdict()
        if grupos.get("etapa"):
            grupos["etapa"] = ETAPA[grupos["etapa"]]
        for nome in _NUMEROS_BR & grupos.keys():
            grupos[nome] = num_br(grupos[nome])
        return novo(grupos) if callable(novo) else novo.format(**grupos)

    return trocar


def _tabela_frases() -> list[tuple[str, re.Pattern[str], Callable[[re.Match[str]], str]]]:
    tabela = [(len(_MARCADOR.sub("", a)), *_compilar(a, False), _substituto(n)) for a, n in FRASES]
    tabela += [(0, *_compilar(a, True), _substituto(n)) for a, n in FRASES_INTEIRAS]
    # Mais específica primeiro: "blocked by URL policy: {motivo}" antes de "policy: {erro}".
    tabela.sort(key=lambda t: -t[0])
    return [(trecho, padrao, troca) for _, trecho, padrao, troca in tabela]


_FRASES = _tabela_frases()


def _codigos_antigos() -> dict[str, str]:
    """Código em inglês das versões antigas -> código atual (INDICIO_FORTE, ALTA...)."""
    return {**VALORES_ENUM, **STATUS_CAMPANHA, **STATUS_REVISAO_HUMANA}


def _codigos_atuais() -> set[str]:
    return {
        str(membro.value)
        for cls in vars(modelos).values()
        if isinstance(cls, type) and issubclass(cls, enum.Enum) and cls.__module__ == modelos.__name__
        for membro in cls
    }


CODIGOS_ANTIGOS = _codigos_antigos()
CODIGOS = {c: rotulo(c) for c in _codigos_atuais() | set(CODIGOS_ANTIGOS.values()) if c.isupper()}
# Antes do código não pode vir pedaço de URL ou de parâmetro (golpe.com/PENDING/login, ?s=ALTA). A barra
# só passa entre dois códigos ("NAO_RELEVANTE/SEM_INDICIO"), o que _para_rotulo confere.
_CODIGO = re.compile(
    r"(?<![\w+=?&%:.-])("
    + "|".join(sorted(map(re.escape, CODIGOS_ANTIGOS.keys() | CODIGOS.keys()), key=len, reverse=True))
    + r")(?![\w-])"
)
_URL = re.compile(r"(?:https?|hxxps?)://\S+", re.IGNORECASE)
_CODIGO_ANTES_DA_BARRA = re.compile(r"([A-Z][A-Z_]*)/\Z")
_REVISOR = re.compile(r"\[(?:Revisor|Reviewer): ([A-Z_]+)\] ?")
_VERBO_REVISOR = {
    "CONFIRMAR": "Revisor confirmou: ",
    "REBAIXAR": "Revisor rebaixou: ",
    "RECOLETAR": "Revisor pediu nova coleta: ",
    "REVISAO_HUMANA": "Revisor pediu revisão humana: ",
    "INCONCLUSIVA": "Revisor considerou inconclusiva: ",
}
_PALAVRA_ANTES = re.compile(r"(\w+) +\Z")
_PALAVRA_DEPOIS = re.compile(r" +(\w+)")


def _vizinha_em_maiusculas(texto: str, inicio: int, fim: int) -> bool:
    for m in (_PALAVRA_ANTES.search(texto, max(0, inicio - 40), inicio), _PALAVRA_DEPOIS.match(texto, fim)):
        if m and len(m.group(1)) > 1 and m.group(1).isupper():
            vizinha = m.group(1)
            if vizinha not in CODIGOS and vizinha not in CODIGOS_ANTIGOS:
                return True
    return False


def _eh_citacao(m: re.Match[str]) -> bool:
    # Código de uma palavra só (HIGH, ALTA, CONFIRM) no meio de um trecho todo em maiúsculas é texto
    # citado da página ("TAXA BAIXA"), não o código: fica como está.
    if m.start() and m.string[m.start() - 1] == "/":
        antes = _CODIGO_ANTES_DA_BARRA.search(m.string, max(0, m.start() - 60), m.start())
        if not antes or (antes.group(1) not in CODIGOS and antes.group(1) not in CODIGOS_ANTIGOS):
            return True
    return "_" not in m.group(1) and _vizinha_em_maiusculas(m.string, m.start(), m.end())


def _trocar_codigos(texto: str, troca: Callable[[re.Match[str]], str]) -> str:
    # URLs são dado coletado: ficam como estão, mesmo com algo parecido com código no caminho.
    partes = _URL.split(texto)
    urls = _URL.findall(texto)
    saida = [_CODIGO.sub(troca, partes[0])]
    for url, resto in zip(urls, partes[1:], strict=True):
        saida += [url, _CODIGO.sub(troca, resto)]
    return "".join(saida)


def _verbo_revisor(m: re.Match[str]) -> str:
    codigo = CODIGOS_ANTIGOS.get(m.group(1), m.group(1))
    return _VERBO_REVISOR.get(codigo, m.group(0))


def _para_rotulo(m: re.Match[str]) -> str:
    if _eh_citacao(m):
        return m.group(1)
    atual = CODIGOS_ANTIGOS.get(m.group(1), m.group(1))
    return CODIGOS.get(atual, atual)


def _para_codigo_atual(m: re.Match[str]) -> str:
    if _eh_citacao(m):
        return m.group(1)
    return CODIGOS_ANTIGOS.get(m.group(1), m.group(1))


@lru_cache(maxsize=65536)
def _traduzir(texto: str, rotulos: bool) -> str:
    for trecho, padrao, troca in _FRASES:
        if trecho in texto:
            for _ in range(5):
                novo = padrao.sub(troca, texto)
                if novo == texto:
                    break
                texto = novo
    if rotulos:
        texto = _REVISOR.sub(_verbo_revisor, texto)
    return _trocar_codigos(texto, _para_rotulo if rotulos else _para_codigo_atual)


def traduzir(texto: str | None, rotulos: bool = True) -> str:
    """Texto gravado pelas versões antigas, com as frases fixas em inglês trocadas pelo português e os códigos
    (os antigos, em inglês, e os atuais) trocados pelo rótulo legível.

    ``rotulos=False`` é para o JSON bruto do registro, em que os valores estruturados já são os códigos atuais:
    os códigos em inglês viram o código atual (REVIEW_REQUIRED -> REQUER_REVISAO) e os atuais ficam como estão."""
    if not texto:
        return texto or ""
    return _traduzir(texto, rotulos)
