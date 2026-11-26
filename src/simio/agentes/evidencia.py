"""Evidence Agent: runs the secure collector and turns raw page data into an addressable
evidence catalog plus deterministic observations ("signals") that other agents cite.

The catalog is a pure function of (Evidence, BrandProfile), so it is reproducible from the
stored evidence at any time (reports and dossiers rebuild it instead of trusting summaries).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from simio.coletores.navegador import ColetorNavegador
from simio.marcas import PerfilMarca, normalizar_texto, sentencas
from simio.nucleo.modelos import (
    CorrespondenciaVisual,
    Evidencia,
    InfoCampoEntrada,
    InfoImagem,
    ItemEvidencia,
    ResultadoDescoberta,
)
from simio.nucleo.urls import dominio_corresponde, hostname_de

# ---------------------------------------------------------------------------------- regexes
CNPJ_RE = re.compile(r"(?<![\w])([0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?\d{2})(?![\w])")
TELEFONE_RE = re.compile(r"(?<!\d)(0800[\s.-]?\d{3}[\s.-]?\d{4}|\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4})(?!\d)")
CEP_RE = re.compile(r"(?<!\d)\d{5}-\d{3}(?!\d)")
# A legal name = 1..8 capitalised words (connectors allowed) ending in a company-type suffix, on a
# single line. Lower-case words and sentence-ending periods break the chain, so surrounding
# sentences are not captured.
RAZAO_SOCIAL_RE = re.compile(
    r"((?:[A-ZÀ-Ý0-9][\wÀ-ÿ&'\-]*(?:\.[\wÀ-ÿ]+)*[ \t]+(?:(?:de|da|do|dos|das|e|&)[ \t]+)?){1,8}"
    r"(?:LTDA|Ltda|EIRELI|Eireli|S\.[ ]?A\.?|S/A|ME|EPP)\b\.?)"
)
# Third-person / reporting / warning context: a claim *about* the brand, not *by* the page.
NOTICIADO_RE = re.compile(
    r"\b(segundo|de acordo com|conforme|informou|anunciou|afirmou|divulgou|comunicado|reportagem|"
    r"noticia|golpe|golpistas|alerta|cuidado com|falso|falsa|fraude|denuncia|procurado pela)\b"
)

# UI words that often precede a company name in flattened text (buttons, menus).
PREFIXO_UI_NOME = re.compile(
    r"^(?:(?:WhatsApp|Whatsapp|Atendimento|Ligar|Fale|Contato|Contatos|Home|Início|Menu|Simule|Simular|"
    r"Oficial|Agora|Sobre|Quem|Somos|Copyright|Todos|Direitos|Reservados|©)[ \t]+)+"
)


NOME_ROTULADO_RE = re.compile(r"(?i)(raz[aã]o social|nome fantasia)\s*[:\-]\s*([^\n|;]{3,100})")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
INJECAO_RE = re.compile(
    r"(?i)(ignore (all |the )?(previous|prior|above) instructions|ignore (as|todas as) instru[cç][oõ]es"
    r"|desconsidere (as|todas as) instru[cç][oõ]es|you are now|system prompt|prompt do sistema"
    r"|as an ai|classif(y|ique) (this|esta|como)|SEM_INDICIO|INDICIO_FORTE|NO_INDICATION|STRONG_INDICATION"
    r"|<\|im_start\|>|<\|system\|>|###\s*instruction|\bassistant\s*:)"
)
PALAVRA_CHAVE_RE = re.compile(
    r"(?i)(cnpj|raz[aã]o social|nome fantasia|ltda|eireli|ouvidoria|sac\b|atendimento|whatsapp|"
    r"correspondente|parceir|credenciad|autorizad|oficial|endere[cç]o|cep\b|telefone|"
    r"institui[cç][aã]o financeira|banco|2[aª]\s*via|segunda\s*via|boleto|quita[cç][aã]o|"
    r"logo(?:tipo)?\s*(?:antig[oa]|velh[oa]|legad[oa])|bv\s*antig[oa])"
)
LOGO_ANTIGA_RE = re.compile(
    r"(?i)(bv-?logo-?antig[oa]|logo-?antiga-?bv|bv-?antig[oa]|bv_antig[oa]|logo-?bv-?antiga|"
    r"bv-?vermelh|b-?vermelho|logotipo\s+antigo|logo\s+antiga|marca\s+antiga)"
)
FRASE_MULTIBANCO_RE = re.compile(
    r"(?i)\b(bancos\s+parceiros|financeiras\s+parceiras|principais\s+bancos|diversos\s+bancos|"
    r"v[aá]rios\s+bancos|m[uú]ltiplos\s+bancos|todas\s+as\s+financeiras|"
    r"trabalhamos\s+com\s+(?:os\s+)?bancos|financiamos\s+(?:por|pelos))\b"
)
CONTEXTO_BANCARIO_RE = re.compile(
    r"(?i)\b(banco|financeira|financiamento|financie|financiar|cr[eé]dito|empr[eé]stimo|"
    r"consignado|promotora|correspondente|boleto|quita[cç][aã]o|parcela|sac\b|ouvidoria|"
    r"refinanciamento|ve[ií]culos?|seminovos|autom[oó]veis|motos?|solar|feir[aã]o)\b"
)
URL_EDITORIAL_RE = re.compile(
    r"(?i)(?:^https?://(?:ajuda|help|meajuda)\.|/hc/pt-br/articles/|"
    r"/(?:jurisprudencia|processos?|reclame|publieditorial|post-patrocinado)/)"
)

_PESOS_CNPJ_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
_PESOS_CNPJ_2 = [6, *_PESOS_CNPJ_1]


def normalizar_cnpj(bruto: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", bruto.upper())


def cnpj_valido(bruto: str) -> bool:
    """Validate numeric and alphanumeric (2026+) CNPJ check digits."""
    c = normalizar_cnpj(bruto)
    if len(c) != 14 or not c[12:].isdigit() or len(set(c)) == 1:
        return False
    vals = [ord(ch) - 48 for ch in c]

    def dv(base: list[int], pesos: list[int]) -> int:
        r = sum(v * w for v, w in zip(base, pesos, strict=True)) % 11
        return 0 if r < 2 else 11 - r

    d1 = dv(vals[:12], _PESOS_CNPJ_1)
    d2 = dv([*vals[:12], d1], _PESOS_CNPJ_2)
    return vals[12] == d1 and vals[13] == d2


def formatar_cnpj(c: str) -> str:
    c = normalizar_cnpj(c)
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else c


# --------------------------------------------------------------------------------- catalog
def _linhas(texto: str) -> list[str]:
    bruto = [re.sub(r"\s+", " ", ln).strip() for ln in (texto or "").splitlines()]
    saida: list[str] = []
    for ln in bruto:
        if not ln:
            continue
        while len(ln) > 400:
            corte = ln.rfind(" ", 0, 400)
            corte = corte if corte > 100 else 400
            saida.append(ln[:corte])
            ln = ln[corte:].strip()
        saida.append(ln)
    return saida


# Form fields that reveal credential harvesting when the page uses the brand. CPF alone is common on
# legitimate simulation pages, so it is recorded but is not a credential by itself.
_CAMPOS_SENSIVEIS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("senha", re.compile(r"(?i)senha|password|passwd|\bpin\b")),
    ("cvv", re.compile(r"(?i)\bcvv\b|\bcvc\b|cc-csc|c[oó]d(igo)?\.? de seguran")),
    ("cartão", re.compile(r"(?i)cart[aã]o|card[-_ ]?number|cc-number|cc-exp|validade do cart")),
    ("token/código", re.compile(r"(?i)token|\botp\b|one-time-code|c[oó]digo (de )?(verifica|sms|acesso|seguran)")),
    ("agência/conta", re.compile(r"(?i)ag[eê]ncia|conta[-_ ]?corrente|n[uú]mero da conta|\bconta\b")),
    ("cpf", re.compile(r"(?i)\bcpf\b")),
)
TIPOS_CREDENCIAL = frozenset({"senha", "cvv", "cartão", "token/código", "agência/conta"})


def classificar_campo_entrada(inp: InfoCampoEntrada) -> str | None:
    """Kind of sensitive data a form field asks for (None for ordinary fields)."""
    t = inp.tipo_campo.lower()
    if t == "password":
        return "senha"
    if t in ("email", "checkbox", "radio", "search", "url", "range", "color", "file"):
        return None
    blob = " ".join((inp.nome, inp.id, inp.placeholder, inp.autocomplete, inp.rotulo))
    return next((tipo for tipo, rx in _CAMPOS_SENSIVEIS if rx.search(blob)), None)


_HOSTS_LOGIN_PLATAFORMA = (
    "facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "x.com", "twitter.com", "threads.net",
    "youtube.com", "google.com", "kwai.com", "pinterest.com", "reddit.com",
)


def montar_catalogo(
    ev: Evidencia,
    marca: PerfilMarca,
    max_itens_texto: int = 60,
    resultados_descoberta: list[ResultadoDescoberta] | None = None,
    correspondencias_visuais: list[CorrespondenciaVisual] | None = None,
) -> list[ItemEvidencia]:
    itens: list[ItemEvidencia] = []
    adicionar = itens.append
    for i, m in enumerate(correspondencias_visuais or [], 1):
        x, y, w, h = m.bbox
        adicionar(
            ItemEvidencia(
                ref=f"E-VIS-LEGACY-{i:03d}",
                tipo="visual",
                local="viewport" if m.acima_dobra else "page",
                acima_dobra=m.acima_dobra,
                conteudo=(
                    f"Comparação de imagem (determinística, sem IA): logotipo ANTIGO de {marca.nome_exibicao} "
                    f"(B vermelho sobre V azul) em {m.ref_captura}, posição x={x} y={y}, {w}x{h}px, "
                    f"similaridade {m.pontuacao:.2f}" + (f"; imagem: {m.src_imagem[:200]}" if m.src_imagem else "")
                ),
            )
        )
    url = ev.url_final or ev.url_original
    adicionar(
        ItemEvidencia(
            ref="E-URL", tipo="dominio", local="url", conteudo=f"URL final: {url} (domínio: {hostname_de(url)})"
        )
    )
    if ev.cadeia_redirecionamentos:
        adicionar(
            ItemEvidencia(
                ref="E-REDIR",
                tipo="rede",
                local="url",
                conteudo="Cadeia de redirecionamento: " + " -> ".join([*ev.cadeia_redirecionamentos, url]),
            )
        )
    if ev.status.value not in ("OK", "PARCIAL") or ev.erros:
        adicionar(
            ItemEvidencia(
                ref="E-COLLECT",
                tipo="rede",
                local="collection",
                conteudo=f"Status da coleta: {ev.status.value}; HTTP {ev.status_http}; "
                f"ocorrências: {'; '.join(ev.erros)[:600] or 'nenhuma'}",
            )
        )
    if resultados_descoberta:
        disc_vistos: set[str] = set()
        idx_disc = 0
        for h in resultados_descoberta:
            partes = [x.strip() for x in (h.titulo or "", h.trecho or "") if x and x.strip()]
            if not partes:
                continue
            combinado = " — ".join(partes)[:1000]
            if combinado in disc_vistos:
                continue
            disc_vistos.add(combinado)
            idx_disc += 1
            adicionar(
                ItemEvidencia(
                    ref=f"E-DISC-{idx_disc:03d}",
                    tipo="texto",
                    local="search_index",
                    conteudo=f"Índice de busca ({h.fonte}): {combinado}",
                )
            )
            if idx_disc >= 5:
                break
    for art, ref in (("captura_viewport", "E-SHOT-VIEWPORT"), ("captura_completa", "E-SHOT-FULL")):
        a = ev.artefato(art)
        if a:
            adicionar(
                ItemEvidencia(
                    ref=ref,
                    tipo="captura",
                    local="viewport" if "VIEW" in ref else "page",
                    conteudo=f"{a.caminho} (sha256 {a.sha256[:16]}…)",
                )
            )
    p = ev.pagina
    if p is None or not ev.utilizavel:
        return itens
    adicionar(ItemEvidencia(ref="E-TITLE", tipo="titulo", local="head", conteudo=p.titulo or "(sem título)"))
    for i, chave in enumerate(
        k
        for k in ("description", "og:site_name", "og:title", "og:description", "author", "application-name")
        if k in p.meta
    ):
        adicionar(ItemEvidencia(ref=f"E-META-{i + 1}", tipo="meta", local="head", conteudo=f"{chave}: {p.meta[chave]}"))
    if p.canonica:
        adicionar(ItemEvidencia(ref="E-CANONICAL", tipo="meta", local="head", conteudo=p.canonica))
    for i, h in enumerate(p.titulos_secao[:15]):
        adicionar(ItemEvidencia(ref=f"E-HEAD-{i + 1}", tipo="cabecalho_secao", local="page", conteudo=h[:300]))
    if p.texto_cabecalho:
        adicionar(ItemEvidencia(ref="E-HDR", tipo="texto", local="header", conteudo=p.texto_cabecalho[:800]))
    if p.texto_acima_dobra:
        adicionar(
            ItemEvidencia(
                ref="E-FOLD", tipo="texto", local="viewport", acima_dobra=True, conteudo=p.texto_acima_dobra[:2000]
            )
        )
    if p.texto_rodape:
        adicionar(ItemEvidencia(ref="E-FTR", tipo="texto", local="footer", conteudo=p.texto_rodape[:2000]))
    if p.itens_nav:
        adicionar(
            ItemEvidencia(
                ref="E-NAV",
                tipo="nav",
                local="header",
                conteudo=" | ".join(f"{n.texto} <{n.href}>" for n in p.itens_nav[:30])[:1500],
            )
        )
    for i, c in enumerate(p.ctas[:20]):
        adicionar(
            ItemEvidencia(
                ref=f"E-CTA-{i + 1}",
                tipo="cta",
                local=c.local,
                conteudo=f"{c.texto}" + (f" <{c.href}>" if c.href else ""),
            )
        )
    for i, c in enumerate(p.links_contato[:20]):
        adicionar(
            ItemEvidencia(ref=f"E-CONTACT-{i + 1}", tipo="contato", local=c.local, conteudo=f"{c.texto} <{c.href}>")
        )
    eh_social = dominio_corresponde(hostname_de(url), ("instagram.com", "facebook.com", "tiktok.com"))
    com_marca = [
        img
        for img in p.imagens
        if _imagem_tem_marca(img, marca) or (eh_social and img.acima_dobra and img.largura >= 200 and img.altura >= 200)
    ]
    for i, img in enumerate(com_marca[:20]):
        adicionar(
            ItemEvidencia(
                ref=f"E-IMG-{i + 1}",
                tipo="imagem",
                local=img.local,
                acima_dobra=img.acima_dobra,
                conteudo=f"imagem alt='{img.alt}' src={img.src[:200]} "
                f"({img.largura}x{img.altura}, {'acima' if img.acima_dobra else 'abaixo'} da dobra)",
            )
        )
    links_oficiais = [ln for ln in p.links if dominio_corresponde(hostname_de(ln.href), marca.dominios_oficiais)]
    if links_oficiais:
        adicionar(
            ItemEvidencia(
                ref="E-OFFICIAL-LINKS",
                tipo="link",
                local="page",
                conteudo=" | ".join(f"{ln.texto} <{ln.href}>" for ln in links_oficiais[:10]),
            )
        )
    n = 0
    for linha_txt in _linhas(p.texto_visivel):
        if n >= max_itens_texto:
            break
        if (
            marca.mencoes(linha_txt)
            or marca.alegacoes_oficiais(linha_txt)
            or marca.declaracoes(linha_txt)
            or PALAVRA_CHAVE_RE.search(linha_txt)
            or CNPJ_RE.search(linha_txt)
            or INJECAO_RE.search(linha_txt)
        ):
            n += 1
            adicionar(ItemEvidencia(ref=f"E-TXT-{n:03d}", tipo="texto", local="page", conteudo=linha_txt))
    n_formularios = 0
    # A social platform's own login wall is not the page asking for credentials.
    campos_formulario = [] if dominio_corresponde(hostname_de(url), _HOSTS_LOGIN_PLATAFORMA) else p.campos_entrada
    for inp in campos_formulario:
        tipo = classificar_campo_entrada(inp)
        if tipo is None or n_formularios >= 12:
            continue
        n_formularios += 1
        desc = ", ".join(
            f"{k}={v}" for k, v in (("type", inp.tipo_campo), ("name", inp.nome), ("placeholder", inp.placeholder),
                                    ("rótulo", inp.rotulo)) if v
        )
        adicionar(
            ItemEvidencia(
                ref=f"E-FORM-{n_formularios:03d}",
                tipo="formulario",
                local="page",
                conteudo=f"Campo de formulário que pede {tipo.upper()} ({desc[:240]})",
            )
        )
    host_pagina = hostname_de(url)
    externo = sorted(
        {hostname_de(a) for a in p.acoes_formulario if a.startswith("http") and hostname_de(a) != host_pagina} - {""}
    )
    if externo:
        adicionar(
            ItemEvidencia(
                ref="E-FORM-ACTION",
                tipo="formulario",
                local="page",
                conteudo="Formulário envia os dados para outro domínio: " + ", ".join(externo[:5]),
            )
        )
    if ev.requisicoes_bloqueadas:
        adicionar(
            ItemEvidencia(
                ref="E-NET-BLOCKED",
                tipo="rede",
                local="collection",
                conteudo=f"{len(ev.requisicoes_bloqueadas)} requisição(ões) bloqueadas pela política: "
                + "; ".join(ev.requisicoes_bloqueadas[:5]),
            )
        )
    return itens


def _imagem_tem_marca(img: InfoImagem, marca: PerfilMarca) -> bool:
    blob = f"{img.alt} {img.src}"
    return bool(
        marca.mencoes(blob)
        or "logo" in normalizar_texto(blob)
        or LOGO_ANTIGA_RE.search(blob)
        or marca.correspondencia_marca_dominio(img.src)
    )


# --------------------------------------------------------------------------------- signals
@dataclass
class Ocorrencia:
    valor: str
    ref: str
    contexto: str = ""


@dataclass
class SinaisPagina:
    """Deterministic observations with catalog references (facts, not conclusions)."""

    utilizavel: bool
    mencoes_marca: int = 0
    marca_no_titulo: bool = False
    marca_no_dominio: str | None = None
    marca_no_cabecalho: bool = False
    marca_acima_dobra: bool = False
    imagens_marca: list[Ocorrencia] = field(default_factory=list)
    imagens_nomeadas_marca: int = 0  # images whose alt/src carries the brand name itself
    palavras_visiveis: int = 0
    imagens_marca_acima_dobra: int = 0
    marcadores_logo_antiga: list[Ocorrencia] = field(default_factory=list)
    logo_antiga_visual: list[Ocorrencia] = field(default_factory=list)  # deterministic image matches
    alegacoes_oficiais: list[Ocorrencia] = field(default_factory=list)
    alegacoes_ligadas_marca: list[Ocorrencia] = field(default_factory=list)
    declaracoes: list[Ocorrencia] = field(default_factory=list)
    declaracao_acima_dobra: bool = False
    declaracao_nomeia_instituicao: bool = False
    locais_declaracao: set[str] = field(default_factory=set)
    parceria: list[Ocorrencia] = field(default_factory=list)
    parceria_com_marca: list[Ocorrencia] = field(default_factory=list)
    alegacoes_noticiadas: list[Ocorrencia] = field(default_factory=list)  # third-person / news / warnings
    mencoes_fracas: int = 0
    tem_evidencia_descoberta: bool = False
    canal_oficial: bool = False
    pagina_artigo: bool = False
    contexto_editorial: bool = False
    contexto_homonimo: bool = False
    bancos_concorrentes: list[str] = field(default_factory=list)
    contexto_multibanco: bool = False
    alegacoes_servicos_sensiveis: list[Ocorrencia] = field(default_factory=list)
    ouvidoria: list[Ocorrencia] = field(default_factory=list)
    links_oficiais: bool = False
    contatos_whatsapp: list[Ocorrencia] = field(default_factory=list)
    telefones: list[Ocorrencia] = field(default_factory=list)
    cnpjs: list[Ocorrencia] = field(default_factory=list)
    marcadores_injecao: list[Ocorrencia] = field(default_factory=list)
    formularios_presentes: int = 0
    campos_sensiveis: list[Ocorrencia] = field(default_factory=list)  # value = kind (senha, cartão, cpf...)
    acao_formulario_externa: list[Ocorrencia] = field(default_factory=list)

    @property
    def campos_credencial(self) -> list[Ocorrencia]:
        return [h for h in self.campos_sensiveis if h.valor in TIPOS_CREDENCIAL]

    def como_observacoes(self) -> list[str]:
        saida = [
            f"menções à marca no texto visível: {self.mencoes_marca}",
            f"marca no título: {'sim' if self.marca_no_titulo else 'não'}",
            f"padrão da marca no domínio: {self.marca_no_dominio or 'não'}",
            f"marca no cabeçalho: {'sim' if self.marca_no_cabecalho else 'não'}",
            f"marca na área visível inicial: {'sim' if self.marca_acima_dobra else 'não'}",
            f"imagens possivelmente de logotipo/marca: {len(self.imagens_marca)} "
            f"({self.imagens_marca_acima_dobra} acima da dobra)",
            "indícios de logotipo antigo/legado (B vermelho / V azul ou termo legado): "
            + (", ".join(f"'{h.valor}' [{h.ref}]" for h in self.marcadores_logo_antiga[:5]) or "nenhum identificado"),
            "logotipo antigo confirmado por comparação de imagem: "
            + (", ".join(f"[{h.ref}] {h.valor}" for h in self.logo_antiga_visual[:3]) or "não"),
            "expressões de oficialidade: "
            + (", ".join(f"'{h.valor}' [{h.ref}]" for h in self.alegacoes_oficiais[:8]) or "nenhuma"),
            "expressões de oficialidade próximas à marca: "
            + (", ".join(f"'{h.valor}' [{h.ref}]" for h in self.alegacoes_ligadas_marca[:8]) or "nenhuma"),
            "serviços sensíveis (2ª via de boleto/quitação/acordo) associados à marca: "
            + (", ".join(f"'{h.valor}' [{h.ref}]" for h in self.alegacoes_servicos_sensiveis[:6]) or "nenhum"),
            "declarações de correspondente/independência: "
            + (", ".join(f"'{h.valor}' [{h.ref}]" for h in self.declaracoes[:8]) or "nenhuma encontrada")
            + f"; locais: {', '.join(sorted(self.locais_declaracao)) or '-'}"
            + f"; na área visível inicial/cabeçalho: {'sim' if self.declaracao_acima_dobra else 'não'}"
            + f"; nomeia a instituição: {'sim' if self.declaracao_nomeia_instituicao else 'não'}"
            + " (tamanho, contraste e legibilidade não medidos automaticamente)",
            "menções a parceria: "
            + (", ".join(f"'{h.valor}' [{h.ref}]" for h in self.parceria[:5]) or "nenhuma")
            + f" (associadas à marca: {len(self.parceria_com_marca)})",
            "contexto multimarcas/multibancos (outras instituições financeiras na página): "
            + (
                f"sim ({', '.join(self.bancos_concorrentes[:6])})"
                if self.contexto_multibanco
                else (f"parcial ({', '.join(self.bancos_concorrentes[:4])})" if self.bancos_concorrentes else "não")
            ),
            "expressões de oficialidade em discurso de terceiros (notícia/alerta): "
            + (", ".join(f"'{h.valor}' [{h.ref}]" for h in self.alegacoes_noticiadas[:5]) or "nenhuma"),
            f"página do tipo artigo/notícia (og:type): {'sim' if self.pagina_artigo else 'não'}; "
            f"contexto editorial/informativo/reclamação: {'sim' if self.contexto_editorial else 'não'}",
            f"ouvidoria mencionada: {'sim' if self.ouvidoria else 'não'}",
            f"links para domínio oficial da instituição: {'sim' if self.links_oficiais else 'não'}",
            f"contatos WhatsApp: {len(self.contatos_whatsapp)}; telefones: {len(self.telefones)}",
            "CNPJs: " + (", ".join(f"{h.valor} [{h.ref}]" for h in self.cnpjs[:5]) or "nenhum"),
            f"formulários na página: {self.formularios_presentes}",
            "campos que pedem dados sensíveis: "
            + (", ".join(f"{h.valor} [{h.ref}]" for h in self.campos_sensiveis[:8]) or "nenhum"),
            "formulário que envia dados para outro domínio: "
            + (", ".join(f"{h.valor} [{h.ref}]" for h in self.acao_formulario_externa[:3]) or "não"),
        ]
        if self.canal_oficial:
            saida.append("canal/domínio oficial da própria instituição identificado na URL final")
        if self.tem_evidencia_descoberta and not self.utilizavel:
            saida.append(
                "acesso direto pelo navegador indisponível/bloqueado; sinais extraídos das evidências "
                "indexadas na descoberta (E-DISC-*)"
            )
        if self.contexto_homonimo and self.mencoes_marca == 0:
            saida.append(
                "observação: indício de homônimo da sigla 'BV' (ex.: Boa Vista/RR ou agência de "
                "publicidade/modelos) sem menção explícita ao nome completo da instituição"
            )
        if self.marcadores_injecao:
            saida.append(
                "ATENÇÃO: conteúdo com padrões de instrução a modelos de IA (possível prompt "
                "injection): " + ", ".join(f"[{h.ref}]" for h in self.marcadores_injecao[:5])
            )
        return saida


def calcular_sinais(ev: Evidencia, marca: PerfilMarca, catalogo: list[ItemEvidencia]) -> SinaisPagina:
    sig = SinaisPagina(utilizavel=ev.utilizavel)
    url = ev.url_final or ev.url_original
    host = hostname_de(url)
    p = ev.pagina if ev.utilizavel else None
    itens_disc = [i for i in catalogo if i.ref.startswith("E-DISC-")]
    texto_disc = "\n".join(i.conteudo for i in itens_disc)
    sig.tem_evidencia_descoberta = any(len(i.conteudo) >= 80 for i in itens_disc)
    sig.canal_oficial = marca.eh_url_oficial(url, titulo=p.titulo if p else texto_disc[:200])
    sig.marca_no_dominio = None if sig.canal_oficial else marca.correspondencia_marca_dominio(host)
    if p is None and not sig.tem_evidencia_descoberta:
        return sig
    if p is not None:
        sig.formularios_presentes = p.formularios_presentes
        texto_completo = "\n".join(
            filter(None, (p.titulo, p.texto_cabecalho, p.texto_acima_dobra, p.texto_visivel, p.texto_rodape))
        )
        sig.mencoes_marca = len(marca.mencoes_fortes(p.texto_visivel))
        sig.mencoes_fracas = len(marca.mencoes_fracas(p.texto_visivel))
        sig.pagina_artigo = (p.meta.get("og:type") or "").strip().lower() in ("article", "news", "noticia")
    else:
        texto_completo = texto_disc
        tem_ctx_banco = bool(CONTEXTO_BANCARIO_RE.search(texto_disc))
        sig.mencoes_marca = len(marca.mencoes_fortes(texto_disc)) + (
            len(marca.mencoes_fracas(texto_disc)) if tem_ctx_banco else 0
        )
        sig.mencoes_fracas = len(marca.mencoes_fracas(texto_disc))
        sig.pagina_artigo = False
    sig.contexto_editorial = sig.pagina_artigo or bool(URL_EDITORIAL_RE.search(url) or marca.eh_despriorizado(host))
    normalizado_completo = normalizar_texto(texto_completo)
    tem_marcador_banco_explicito = (
        sig.mencoes_marca > 0
        or bool(marca.mencoes_fortes(host))
        or bool(marca.mencoes_servicos_sensiveis(texto_completo))
        or bool(marca.declaracoes(texto_completo))
        or bool(re.search(r"\b(banco|financeira|correspondente|sac\s+bv|0800)\b", normalizado_completo))
    )
    sig.contexto_homonimo = marca.tem_contexto_homonimo(texto_completo) and not tem_marcador_banco_explicito
    if sig.contexto_homonimo and sig.mencoes_marca == 0 and not marca.mencoes_fortes(host):
        sig.marca_no_dominio = None
    sig.bancos_concorrentes = marca.mencoes_concorrentes(texto_completo)
    sig.contexto_multibanco = len(sig.bancos_concorrentes) >= 2 or (
        len(sig.bancos_concorrentes) >= 1 and bool(FRASE_MULTIBANCO_RE.search(texto_completo))
    )

    nd = marca.sem_declaracoes
    if p is not None:
        titulo_nd = f"{nd(p.titulo)} {nd(p.meta.get('og:site_name', ''))}".strip()
        cabecalho_nd = " ".join([nd(p.texto_cabecalho), *(nd(n.texto) for n in p.itens_nav)]).strip()
        tem_contexto_bancario = (
            sig.mencoes_marca > 0
            or bool(sig.marca_no_dominio)
            or bool(marca.alegacoes_oficiais(texto_completo))
            or bool(marca.declaracoes(texto_completo))
            or bool(CONTEXTO_BANCARIO_RE.search(texto_completo))
        )
        permitir_identidade_fraca = tem_contexto_bancario and not (sig.contexto_homonimo and sig.mencoes_marca == 0)
        titulo_tem_concorrentes = bool(marca.mencoes_concorrentes(titulo_nd)) and not sig.marca_no_dominio
        cabecalho_tem_concorrentes = bool(marca.mencoes_concorrentes(cabecalho_nd)) and not sig.marca_no_dominio

        sig.marca_no_titulo = bool(
            (marca.mencoes_fortes(titulo_nd) or (permitir_identidade_fraca and marca.mencoes_fracas(titulo_nd)))
            and not titulo_tem_concorrentes
        )
        sig.marca_no_cabecalho = bool(
            (marca.mencoes_fortes(cabecalho_nd) or (permitir_identidade_fraca and marca.mencoes_fracas(cabecalho_nd)))
            and not cabecalho_tem_concorrentes
        )
        sig.marca_acima_dobra = bool(marca.mencoes_fortes(nd(p.texto_acima_dobra)))
        sig.declaracao_nomeia_instituicao = any(
            _declaracoes_reais_nomeando_instituicao(marca, t)
            for t in (p.texto_visivel, p.texto_cabecalho, p.texto_rodape)
        )
    else:
        sig.marca_acima_dobra = sig.mencoes_marca > 0
        sig.declaracao_nomeia_instituicao = bool(_declaracoes_reais_nomeando_instituicao(marca, texto_disc))
    sig.links_oficiais = any(i.ref == "E-OFFICIAL-LINKS" for i in catalogo)
    sig.palavras_visiveis = len(p.texto_visivel.split()) if p else 0
    for item in catalogo:
        texto = item.conteudo
        if INJECAO_RE.search(texto):  # every kind, including image alt text and metadata
            sig.marcadores_injecao.append(Ocorrencia(INJECAO_RE.search(texto).group(0), item.ref))  # type: ignore[union-attr]
        if item.tipo == "imagem":
            # Only treat an image as a brand logo indicator when not a non-bank homonym page.
            if not (sig.contexto_homonimo and sig.mencoes_marca == 0 and not sig.marca_no_dominio):
                sig.imagens_marca.append(Ocorrencia(texto[:120], item.ref))
                if marca.mencoes_fortes(texto):
                    sig.imagens_nomeadas_marca += 1
                if item.acima_dobra:
                    sig.imagens_marca_acima_dobra += 1
                if LOGO_ANTIGA_RE.search(texto):
                    sig.marcadores_logo_antiga.append(Ocorrencia(texto[:120], item.ref))
            continue
        if item.tipo == "formulario":
            if item.ref == "E-FORM-ACTION":
                sig.acao_formulario_externa.append(Ocorrencia(texto.split(": ", 1)[-1][:120], item.ref))
            else:
                tipo = texto.removeprefix("Campo de formulário que pede ").split(" (", 1)[0].lower()
                sig.campos_sensiveis.append(Ocorrencia(tipo, item.ref))
            continue
        if item.tipo == "visual":
            sig.logo_antiga_visual.append(Ocorrencia(texto[texto.find("em E-SHOT") :][:120], item.ref))
            sig.marcadores_logo_antiga.append(Ocorrencia("logotipo antigo (comparação de imagem)", item.ref))
            continue
        if item.tipo in ("captura", "rede", "dominio"):
            continue
        # Do not count synthetic meta-descriptions inside E-DISC snippets ("sem exibir razão social /
        # correspondente bancário", "semelhante ao logotipo antigo") as facts observed on the page.
        eh_disc = item.ref.startswith("E-DISC-")
        if LOGO_ANTIGA_RE.search(texto) and not eh_disc:
            sig.marcadores_logo_antiga.append(Ocorrencia(LOGO_ANTIGA_RE.search(texto).group(0), item.ref))  # type: ignore[union-attr]
        for frase in sentencas(texto):
            alegacoes = marca.alegacoes_oficiais(frase)
            if alegacoes:
                if sig.contexto_homonimo and sig.mencoes_marca == 0 and not marca.mencoes_fortes(frase):
                    alegacoes = [c for c in alegacoes if marca.mencoes_fortes(c)]
            if alegacoes:
                # brand-linked: the claim phrase itself names the brand ("SAC BV") or the sentence does
                vinculado = any(marca.mencoes(c) for c in alegacoes) or bool(marca.mencoes(frase))
                noticiado = bool(NOTICIADO_RE.search(normalizar_texto(frase)))
                for c in alegacoes:
                    sig.alegacoes_oficiais.append(Ocorrencia(c, item.ref, frase[:200]))
                    if vinculado and noticiado:
                        sig.alegacoes_noticiadas.append(Ocorrencia(c, item.ref, frase[:200]))
                    elif vinculado:
                        sig.alegacoes_ligadas_marca.append(Ocorrencia(c, item.ref, frase[:200]))
            sens = marca.mencoes_servicos_sensiveis(frase)
            if sens and not NOTICIADO_RE.search(normalizar_texto(frase)):
                if marca.mencoes(frase) or sig.marca_no_titulo or sig.marca_no_dominio or sig.mencoes_marca > 0:
                    for valor_s in sens:
                        sig.alegacoes_servicos_sensiveis.append(Ocorrencia(valor_s, item.ref, frase[:200]))
            for pm in marca.mencoes_parceria(frase):
                sig.parceria.append(Ocorrencia(pm, item.ref, frase[:200]))
                if marca.mencoes(frase):
                    sig.parceria_com_marca.append(Ocorrencia(pm, item.ref, frase[:200]))
        if not eh_disc:
            for d in marca.declaracoes(texto):
                sig.declaracoes.append(Ocorrencia(d, item.ref, texto[:200]))
                sig.locais_declaracao.add(item.local)
                if item.local in ("viewport", "header") or item.acima_dobra:
                    sig.declaracao_acima_dobra = True
        normalizado = normalizar_texto(texto)
        if "ouvidoria" in normalizado and not eh_disc:
            sig.ouvidoria.append(Ocorrencia("ouvidoria", item.ref, texto[:200]))
        if (item.tipo == "contato" or eh_disc) and ("wa.me" in normalizado or "whatsapp" in normalizado):
            sig.contatos_whatsapp.append(Ocorrencia(texto[:200], item.ref))
        for m in TELEFONE_RE.finditer(texto):
            sig.telefones.append(Ocorrencia(m.group(1), item.ref, texto[:200]))
        for m in CNPJ_RE.finditer(texto.upper()):
            bruto = m.group(1)
            if sum(ch.isdigit() for ch in bruto) >= 8:  # avoid matching random uppercase words
                sig.cnpjs.append(Ocorrencia(formatar_cnpj(bruto), item.ref, texto[:300]))
    # Disclosure in the above-fold text counts even if only the E-TXT copy matched.
    if p is not None and (marca.declaracoes(p.texto_acima_dobra) or marca.declaracoes(p.texto_cabecalho)):
        sig.declaracao_acima_dobra = True
    return sig


def _declaracoes_reais_nomeando_instituicao(marca: PerfilMarca, texto: str) -> list[str]:
    """Disclosure sentences naming the institution that are not negated/contradicted in the same
    sentence (e.g. "Não somos correspondente bancário: somos o Banco BV oficial")."""
    return [s for s in marca.declaracoes_nomeando_instituicao(texto) if not marca.alegacoes_oficiais(s)]


# --------------------------------------------------------------------------------- agent
class AgenteEvidencia:
    def __init__(self, coletor: ColetorNavegador, dir_campanha: Path) -> None:
        self.coletor = coletor
        self.dir_campanha = dir_campanha

    async def coletar(self, id_candidata: str, url: str, tentativa: int) -> Evidencia:
        return await self.coletor.coletar(id_candidata, url, tentativa, self.dir_campanha)
