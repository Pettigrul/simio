"""Discovery Agent: formulates investigative hypotheses, runs the available connectors within
the query budget, deduplicates and prioritises candidate pages."""

from __future__ import annotations

import itertools
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field

from simio.brands import BrandProfile
from simio.core.models import DiscoveryHit, DiscoveryOutcome, QueryRecord, SearchHypothesis
from simio.core.urls import domain_matches, hostname_of, normalize_url, registrable_domain
from simio.providers.llm import BudgetExceeded
from simio.providers.search import Connector
from simio.security.url_policy import PolicyViolation, UrlPolicy

log = logging.getLogger(__name__)

# A connector is disabled for the rest of the run after this many consecutive errors.
MAX_CONSECUTIVE_ERRORS = 3

# Shared hosting / social platforms where distinct paths belong to distinct third parties.
SHARED_PLATFORM_DOMAINS = (
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "linkedin.com",
    "wa.me",
    "whatsapp.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "threads.com",
    "lovable.app",
    "vercel.app",
    "netlify.app",
    "pages.dev",
    "web.app",
    "firebaseapp.com",
    "github.io",
    "wixsite.com",
    "canva.site",
)

EPHEMERAL_HOSTING_DOMAINS = (
    "lovable.app",
    "vercel.app",
    "netlify.app",
    "pages.dev",
    "web.app",
    "firebaseapp.com",
    "github.io",
    "wixsite.com",
    "canva.site",
    "blogspot.com",
    "wordpress.com",
)

_ABANDONED_BUILDER_HOSTS = ("weebly.com", "site123.me")

_SOCIAL_INDEX_RE = re.compile(
    r"(?i)^https?://(?:(?:www|m)\.)?(?:"
    r"instagram\.com/(?:popular|explore|tags)/"
    r"|tiktok\.com/(?:discover|tag)/"
    r"|facebook\.com/(?:public|hashtag|watch/explore)/"
    r")"
)

_MARKETPLACE_ITEM_RE = re.compile(
    r"(?i)^https?://(?:[\w-]+\.)?(?:"
    r"olx\.com\.br/.*/(?:autos-e-pecas|veiculos-e-pecas)/"
    r"|mercadolivre\.com\.br/MLB-\d+"
    r"|webmotors\.com\.br/comprar/"
    r"|mobiauto\.com\.br/comprar/"
    r"|usadosbr\.com/carros-e-utilitarios/"
    r")"
)

_ANCILLARY_PATH_RE = re.compile(
    r"(?i)/(?:politica-de-privacidade|privacidade|privacy-policy|privacy|termos-de-uso|"
    r"termos-e-condicoes|lgpd|cookies|trabalhe-conosco|vagas)(?:/|$|\.)"
    r"|(?:^|/)(?:post-)?sitemap[\w-]*\.xml$"
)

_DIRECT_DOCUMENT_RE = re.compile(
    r"(?i)(?:\.(?:pdf|docx?|xlsx?|pptx?|zip)(?:$|\?)|blob\.core\.windows\.net/pdf/)"
)

_INSTITUTION_DIRECTORY_RE = re.compile(
    r"(?i)/(?:empresa|servicos|sobre|nomes|consulta-gratis|consulta-empresa)/"
    r"[^/?#]*(?:bv-financeira-sa|bv-financeira-s-a|banco-bv-s-a|banco-votorantim)"
)

_EDITORIAL_URL_RE = re.compile(
    r"(?i)(?:^https?://(?:ajuda|help|suporte|blog|noticias|conteudos)\."
    r"|/(?:blog|noticias?|artigos?|materias?|imprensa|historias-clientes|customer-story|"
    r"hc/pt-br/articles|jurisprudencia|processos?|publieditorial|post-patrocinado)(?:/|$))"
)

_EDITORIAL_TITLE_RE = re.compile(
    r"(?i)(?:^(?:como\s+(?:ser|falar|adicionar|financiar|quitar|negociar|simular|cancelar|encontrar)\b"
    r"|o\s+que\s+fazer\b|guia\s+(?:completo|definitivo|pr[aá]tico)\b)"
    r"|\b(?:avalia[cç][aã]o\s+completa|manual\s+completo|advogados?\b|entrega\s+amig[aá]vel)\b)"
)

_BANKING_HINT_RE = re.compile(
    r"(?i)\b(banco|financeira|financiamento|financie|financiar|cr[eé]dito|empr[eé]stimo|"
    r"consignado|promotora|correspondente|boleto|quita[cç][aã]o|parcela|sac\b|ouvidoria|"
    r"refinanciamento|ve[ií]culos?|seminovos|autom[oó]veis|motos?|solar|feir[aã]o|0800)\b"
)


BR_REGIONS = {
    "Norte": ["Acre", "Amapá", "Amazonas", "Pará", "Rondônia", "Roraima", "Tocantins"],
    "Nordeste": [
        "Alagoas", "Bahia", "Ceará", "Maranhão", "Paraíba", "Pernambuco", "Piauí", "Rio Grande do Norte", "Sergipe",
    ],
    "Centro-Oeste": ["Distrito Federal", "Goiás", "Mato Grosso", "Mato Grosso do Sul"],
    "Sudeste": ["Espírito Santo", "Minas Gerais", "Rio de Janeiro", "São Paulo"],
    "Sul": ["Paraná", "Rio Grande do Sul", "Santa Catarina"],
}


def _dork_grounding_prompt(description: str, dorks: list[str]) -> str:
    joined = "\n".join(f"  - {q}" for q in dorks)
    return (
        f"Objetivo: {description}.\n"
        f"Execute no Google Search as seguintes consultas (dorks):\n{joined}\n"
        f"Liste de forma abrangente as páginas públicas encontradas (até 12 resultados distintos), "
        f"informando para cada uma o nome do estabelecimento/perfil, a URL e como a marca é "
        f"utilizada (destaque visual, título, feirão, simulador, redes sociais e se há ou não "
        f"identificação clara de CNPJ e condição de correspondente bancário)."
    )


def build_hypotheses(brand: BrandProfile) -> list[SearchHypothesis]:
    """Investigative hypotheses (sec. 4.2) incorporating the validated OSINT dork packages
    (correspondent links, vehicle/solar/credit products, brand/logo in URL/title, feirão campaigns
    and social media profiles)."""
    name = brand.display_name
    alt = next((t for t in brand.strong_terms if t.lower() != name.lower()), name)
    excl = " ".join(f"-site:{d}" for d in brand.official_domains)
    noise_excl = (
        f"{excl} -site:reclameaqui.com.br -site:jusbrasil.com.br -site:jus.br"
    )
    hs: list[SearchHypothesis] = []

    def add_h(hid: str, desc: str, dorks: list[str]) -> None:
        hs.append(
            SearchHypothesis(
                id=hid,
                description=desc,
                grounding_prompt=_dork_grounding_prompt(desc, dorks),
                keyword_queries=dorks,
            )
        )

    # ---- H1: Brand identity in title, URL and logo assets (packages: logo-nomes, nome-marca) ----
    add_h(
        "H1-identity:title-brand",
        "Terceiros e promotoras que usam a marca no título da página",
        [
            f'(intitle:"{name}" OR intitle:"{alt}" OR intitle:"BV Financiamentos" OR intitle:"BV Promotora") '
            f"(financiamento OR crédito OR veículos OR correspondente) {noise_excl}",
            f'(intitle:"BV Veículos" OR intitle:"Agência BV") (financiamento OR seminovos OR veículos) {noise_excl}',
        ],
    )
    add_h(
        "H1-identity:inurl-brand",
        "Sites de terceiros com o nome da marca ou páginas de financiamento na URL",
        [
            f"(inurl:banco-bv OR inurl:bancobv OR inurl:bv-financeira OR inurl:bvfinanceira) "
            f"(financiamento OR veículos OR crédito OR correspondente) {noise_excl}",
            f'(inurl:financie OR inurl:financiar) ("{alt}" OR "{name}") (veículos OR simulador OR loja) {noise_excl}',
        ],
    )
    add_h(
        "H1-identity:logo-assets",
        "Páginas de financiamento que expõem arquivos de logotipo da marca ou logo antiga (B vermelho / V azul)",
        [
            f'(inurl:logo-bv OR "logo-bv" OR "logo-bv-financeira" OR "logo antiga bv" OR "bv logo antiga") (financiamento OR veículos OR crédito) {noise_excl}',
            f'("Logo {name}" OR "Logo {alt}" OR "logo BV" OR "logo BV Financeira") (financiamento OR veículos OR consignado) '
            f"-site:brandsoftheworld.com -site:logospng.org {noise_excl}",
            f'("BV Financeira" OR "{name}") ("logo antiga" OR "logotipo antigo" OR "marca antiga") {noise_excl}',
        ],
    )

    # ---- H2: Products (packages: veiculos, solar, outros-produtos) ----
    add_h(
        f"H2-product:{brand.products[0]}",
        "Páginas de financiamento de veículos de lojas e multimarcas com destaque para a marca",
        [
            f'inurl:financiamento "{name}" veículos {noise_excl}',
            f'inurl:financiamento "{alt}" (veículos OR seminovos OR multimarcas) {noise_excl}',
            f'intitle:financiamento ("{alt}" OR "{name}") veículos {noise_excl}',
        ],
    )
    add_h(
        "H2-product:financiamento de carro e multimarcas",
        "Revendas de seminovos e lojas multimarcas que divulgam financiamento da marca",
        [
            f'"{name}" financiamento (loja OR revenda OR seminovos OR multimarcas) veículos {noise_excl}',
            f'"{alt}" (seminovos OR multimarcas OR revenda) financiamento {noise_excl}',
            f'("financiamos" OR "trabalhamos com" OR "financie") ("{alt}" OR "{name}") veículos {noise_excl}',
        ],
    )
    add_h(
        "H2-product:financiamento de moto",
        "Lojas de motos que oferecem financiamento usando a marca em destaque",
        [
            f'("{alt}" OR "{name}") motos financiamento loja {noise_excl}',
        ],
    )
    add_h(
        "H2-product:financiamento de energia solar",
        "Integradores e correspondentes de energia solar que utilizam a marca",
        [
            f'("{name}" OR "{alt}") "energia solar" (financiamento OR integrador OR parceiro) {noise_excl}',
            f'("financiamento solar" OR "Correspondente BV" OR "parceiro BV") '
            f'("{name}" OR "energia solar") {noise_excl}',
        ],
    )
    add_h(
        "H2-product:refinanciamento e crédito com garantia",
        "Promotoras, correspondentes e corretoras oferecendo refinanciamento, crédito consignado, empréstimo pessoal e garantia com a marca",
        [
            f'"{name}" ("crédito com garantia" OR "home equity" OR refinanciamento) '
            f"(correspondente OR promotora OR financeira) {noise_excl}",
            f'("{alt}" OR "{name}") ("crédito consignado" OR "empréstimo consignado" OR consignado) '
            f'(promotora OR correspondente OR "taxas" OR simulação) {noise_excl}',
            f'("BV Financeira" OR "Banco BV") ("Daycoval" OR "Pan" OR "Safra" OR "BMG") '
            f'(consignado OR refinanciamento OR empréstimo OR promotora) {noise_excl}',
        ],
    )

    # ---- H3: Correspondent relationship & service capture (packages: vinculo-explicito, vinculo-rodada2) ----
    add_h(
        "H3-service:correspondente-vinculo",
        "Correspondentes bancários e promotoras que declaram vínculo com a marca",
        [
            f'("correspondente bancário" OR "Correspondente no País" OR "correspondente do {name}") '
            f'"{name}" {noise_excl}',
            f'("correspondente BV" OR "Somos correspondente" "{name}" OR "correspondente" "{alt}") '
            f"financiamento {noise_excl}",
            f'"correspondente" "Banco Votorantim" financiamento {noise_excl}',
        ],
    )
    add_h(
        "H3-service:simulacao-whatsapp",
        "Atendimento e simulação de financiamento via WhatsApp apresentados em nome da marca",
        [
            f'("simulação" OR "atendimento {name}") ("{name}" OR "{alt}") whatsapp '
            f"(financiamento OR veículos) {noise_excl}",
            f'("parceiro BV" OR "parceiro do {name}" OR "parceria com o {name}") financiamento {noise_excl}',
        ],
    )

    # ---- H4: Official-channel / official-partner claims (package: vinculo-rodada2) ----
    add_h(
        "H4-official-language",
        "Uso de expressões de oficialidade ('parceiro oficial', 'representante', 'correspondente autorizado', 'agência BV')",
        [
            f'("parceiro oficial" OR "parceiros autorizados" OR "correspondente autorizado" '
            f'OR "correspondente bancário autorizado") ("{name}" OR "{alt}") {noise_excl}',
            f'("representante" OR "agência BV" OR "agência {name}" OR "canal oficial" OR "site oficial") '
            f'("{name}" OR "{alt}") financiamento {noise_excl}',
        ],
    )

    # ---- H5: Feirão campaigns and regional establishments (package: feirao) ----
    add_h(
        "H5-establishment:feirao",
        "Lojas e multimarcas que divulgam 'Feirão Banco BV' / 'Feirão BV Financeira'",
        [
            f'("Feirão {name}" OR "Feirão {alt}" OR "Feirão BV") '
            f"(loja OR veículos OR seminovos OR multimarcas) {noise_excl}",
        ],
    )
    # Regional sweep over all 27 UFs (package uf-veiculos), one prompt per macro-region.
    for region, ufs in BR_REGIONS.items():
        add_h(
            f"H5-establishment:regiao-{region}",
            f"Lojas, revendas e correspondentes da região {region} que divulgam financiamento da marca",
            [
                q
                for uf in ufs
                for q in (
                    f'"{alt}" "bancos parceiros" veículos "{uf}" {noise_excl}',
                    f'("{name}" OR "{alt}") financiamento (loja OR revenda OR correspondente OR promotora) "{uf}" '
                    f"{noise_excl}",
                )
            ],
        )

    # ---- H6: Social media profiles & posts (packages: social-marketplace, feirao, nome-marca) ----
    add_h(
        "H6-platform:Instagram",
        "Perfis, Reels e publicações no Instagram de lojas e correspondentes usando a marca ('Parceiro BV', 'Correspondente', 'Agência BV', 'Feirão Banco BV')",
        [
            f'site:instagram.com "Feirão {name}" OR "Feirão BV" {excl}',
            f'site:instagram.com ("correspondente" OR promotora OR "crédito consignado") ("{name}" OR "{alt}") {excl}',
            f'site:instagram.com ("Parceiro BV" OR "BV Financeira") (veículos OR empréstimo OR consignado OR refinanciamento) {excl}',
            f'site:instagram.com/reel ("{name}" OR "{alt}") (financiamento OR consignado OR veículos) {excl}',
        ],
    )
    add_h(
        "H6-platform:Facebook",
        "Páginas e vídeos no Facebook divulgando 'Feirão Banco BV', consignado ou financiamento sob a marca",
        [
            f'site:facebook.com "Feirão {name}" OR "Feirão {alt}" {excl}',
            f'site:facebook.com ("{alt}" OR "{name}") (veículos OR consignado OR empréstimo OR refinanciamento) {excl}',
            f'site:facebook.com ("BV Financeira" OR "Banco BV") (promotora OR correspondente) {excl}',
        ],
    )
    add_h(
        "H6-platform:TikTok-LinkedIn",
        "Perfis no TikTok e páginas de empresas no LinkedIn apresentando-se como correspondentes ou parceiros da marca",
        [
            f'site:tiktok.com ("{name}" OR "{alt}") financiamento -inurl:/@bancobv -inurl:/discover/ {excl}',
            f'site:linkedin.com/company "correspondente" ("{name}" OR "{alt}") -inurl:/company/bancobv {excl}',
        ],
    )
    add_h(
        "H6-platform:WhatsApp",
        "Links e páginas de captação via WhatsApp (wa.me) utilizando a marca",
        [
            f'("wa.me" OR "api.whatsapp.com") ("{name}" OR "{alt}") '
            f"(financiamento OR correspondente OR veículos OR consignado OR empréstimo) {noise_excl}",
            f'("linktr.ee" OR "linkme.bio" OR "heylink.me") ("{name}" OR "{alt}") {noise_excl}',
        ],
    )

    # ---- H7: Threat Intelligence & Combosquatting / Sensitive Services (CTI / Brand Protection) ----
    add_h(
        "H7-threat-intel:sensitive-services",
        "Páginas de terceiros ofertando serviços sensíveis com a marca (2ª via boleto, quitação, negociação, quitação de contrato)",
        [
            f'("2ª via" OR "segunda via" OR "quitação" OR "quitar financiamento" OR "acordo") '
            f'("{name}" OR "{alt}") (boleto OR contrato OR parcelas) {noise_excl}',
            f'("central de atendimento" OR "fale com" OR "SAC" OR "0800") ("{name}" OR "{alt}") '
            f'(boleto OR quitação OR negociação OR whatsapp) {noise_excl}',
        ],
    )
    add_h(
        "H7-threat-intel:combosquatting-portals",
        "Páginas de terceiros com combosquatting ou simuladores se passando por canais de contratação ou login",
        [
            f'(intitle:"{name}" OR intitle:"{alt}") (intitle:"simulador" OR intitle:"login" OR intitle:"portal" OR intitle:"atendimento") {noise_excl}',
            f'(inurl:bancobv OR inurl:bvfinanceira OR inurl:bv-credito OR inurl:bv-online) '
            f'(simulacao OR portal OR login OR acesso OR proposta) {noise_excl}',
        ],
    )

    # ---- Packages from dorks.md not covered above --------------------------------------------
    add_h(
        "H2-product:bancos-parceiros",
        "Lojas e integradores que listam a marca entre 'bancos parceiros' (logotipo em vitrine multibanco)",
        [
            f'"bancos parceiros" "{name}" {noise_excl}',
            f'"bancos parceiros" "{alt}" {noise_excl}',
            f'"{alt}" "Banco Pan" "Santander" financiamento veículos {noise_excl}',
            f'"{name}" "Banco Pan" "Santander" financiamento loja {noise_excl}',
            f'"bancos parceiros" "energia solar" "BV" {noise_excl}',
            f'"BV" "Solfácil" "Santander" financiamento energia solar {noise_excl}',
        ],
    )
    add_h(
        "H6-platform:YouTube-Kwai-Threads",
        "Vídeos e canais (YouTube, Kwai, Threads) de lojas e promotoras usando a marca",
        [
            f'site:youtube.com ("{name}" OR "{alt}") (financiamento OR feirão OR loja OR consignado) {excl}',
            f'site:kwai.com ("{name}" OR "{alt}" OR "Feirão BV") {excl}',
            f'site:threads.net ("{name}" OR "{alt}") (financiamento OR feirão OR crédito) {excl}',
        ],
    )
    add_h(
        "H6-platform:Telegram-grupos",
        "Canais do Telegram e grupos de WhatsApp oferecendo crédito/financiamento em nome da marca",
        [
            f'site:t.me ("{name}" OR "{alt}") (empréstimo OR financiamento OR crédito OR consignado) {excl}',
            f'("chat.whatsapp.com" OR "grupo de whatsapp" OR "grupo do whatsapp") ("{name}" OR "{alt}") '
            f"(empréstimo OR financiamento OR crédito) {noise_excl}",
        ],
    )
    add_h(
        "H6-platform:marketplaces",
        "Anúncios e lojas em marketplaces (OLX, Webmotors, Mercado Livre, iCarros, Mobiauto) usando a marca",
        [
            f'site:olx.com.br ("{alt}" OR "{name}") {excl}',
            f'site:webmotors.com.br ("{alt}" OR "{name}") {excl}',
            f'site:mercadolivre.com.br ("{name}" OR "{alt}") (financiamento OR crédito OR empréstimo) {excl}',
            f'(site:icarros.com.br OR site:mobiauto.com.br) ("{name}" OR "{alt}") (loja OR financiamento) {excl}',
        ],
    )

    # ---- H10: fraud typologies typical of bank / auto-finance brand abuse --------------------
    brand_or = f'("{name}" OR "{alt}")'
    add_h(
        "H10-fraud:leilao-retomados",
        "Leilões falsos e venda de veículos 'retomados/apreendidos' em nome da marca",
        [
            f"(leilão OR leilões OR leiloeiro) {brand_or} (veículos OR carros OR motos) {noise_excl}",
            f'("veículos retomados" OR "carros retomados" OR "veículos apreendidos" OR "busca e apreensão" '
            f'OR "recuperados") {brand_or} (venda OR compre OR lote OR "abaixo da fipe") {noise_excl}',
        ],
    )
    add_h(
        "H10-fraud:agio-repasse",
        "Venda de ágio, repasse e 'assumo/passo financiamento' de veículos financiados pela marca",
        [
            f'("assumo financiamento" OR "passo financiamento" OR "repasse de financiamento" OR "venda de ágio" '
            f'OR "carro com ágio" OR "transfiro financiamento") ("BV" OR "{name}" OR "{alt}") {noise_excl}',
        ],
    )
    add_h(
        "H10-fraud:falso-emprestimo",
        "Falso empréstimo: crédito 'pré-aprovado', liberação imediata ou cobrança antecipada em nome da marca",
        [
            f'("crédito pré-aprovado" OR "empréstimo aprovado" OR "liberação imediata" OR "sem consulta" '
            f'OR "para negativados") {brand_or} {noise_excl}',
            f'("taxa de liberação" OR "depósito antecipado" OR "taxa de cadastro" OR "pagamento antecipado") '
            f"{brand_or} (empréstimo OR crédito OR financiamento) {noise_excl}",
        ],
    )
    add_h(
        "H10-fraud:app-phishing",
        "Aplicativos falsos (APK), páginas de login e pedidos de atualização de dados em nome da marca",
        [
            f'("app {name}" OR "aplicativo BV" OR "BV apk" OR "baixar app BV" OR "{name} apk") '
            f"-site:play.google.com -site:apps.apple.com {excl}",
            f'("acesse sua conta" OR "atualize seus dados" OR "atualização cadastral" OR "token" OR "desbloqueio") '
            f"{brand_or} {noise_excl}",
        ],
    )
    add_h(
        "H10-fraud:central-falsa",
        "Telefones, WhatsApp e 'centrais' de atendimento não oficiais divulgados como sendo da marca",
        [
            f'("whatsapp oficial" OR "número do whatsapp" OR "telefone" OR "0800" OR "central de atendimento") '
            f"{brand_or} (contato OR atendimento OR falar) {noise_excl} -site:telefone.info",
        ],
    )
    add_h(
        "H10-fraud:agencia-listagem",
        "Listagens de 'agência/loja/escritório' da marca em diretórios e mapas de terceiros",
        [
            f'("agência {name}" OR "agência {alt}" OR "loja {name}" OR "escritório {name}") '
            f'(endereço OR "como chegar" OR horário OR telefone) {noise_excl}',
        ],
    )
    add_h(
        "H10-fraud:vagas",
        "Vagas e 'renda extra' falsas usando a marca (digitador, correspondente, home office)",
        [
            f'(vagas OR "trabalhe conosco" OR "processo seletivo" OR "renda extra" OR "home office") {brand_or} '
            f"(correspondente OR promotora OR digitador OR vendedor OR comissão) {noise_excl}",
        ],
    )
    add_h(
        "H10-fraud:investimento-cartao",
        "Investimentos com rendimento garantido e cartões 'aprovados na hora' em nome da marca",
        [
            f'("rendimento garantido" OR "CDB" OR "lucro" OR "renda fixa") ("{name}" OR "BV Investimentos") '
            f"(whatsapp OR telegram OR grupo OR invista) {noise_excl}",
            f'("cartão BV" OR "cartão de crédito BV" OR "cartão {name}") ("aprovado na hora" OR "sem consulta" '
            f'OR "limite alto" OR "solicite já" OR "para negativados") {noise_excl}',
        ],
    )
    add_h(
        "H10-fraud:quitacao-limpa-nome",
        "Ofertas de quitação com desconto, 'limpa nome' e revisão de juros usando a marca",
        [
            f'("limpa nome" OR "quitação com desconto" OR "redução de juros" OR "revisão de contrato" '
            f'OR "juros abusivos" OR "quitar com desconto") {brand_or} {noise_excl}',
        ],
    )

    # ---- H11: NaPista (group marketplace brand) impersonation ---------------------------------
    add_h(
        "H11-napista:sites",
        "Sites e páginas de terceiros usando a marca NaPista como canal próprio ou oficial",
        [
            f'("NaPista" OR "Feirão NaPista" OR "Feirão Na Pista") (financiamento OR parceiro OR loja OR oficial) '
            f"{noise_excl}",
            f'inurl:napista ("NaPista" OR financiamento) {noise_excl}',
        ],
    )
    add_h(
        "H11-napista:social",
        "Perfis e publicações em redes sociais usando a marca NaPista",
        [
            f'site:instagram.com ("NaPista" OR "Feirão NaPista") {excl}',
            f'site:facebook.com ("NaPista" OR "Feirão NaPista") (financiamento OR feirão OR loja) {excl}',
        ],
    )
    return hs


# Focused campaigns (``simio hunt --focus``): only one question is investigated.
FOCUS_LEGACY_LOGO = "logo-antiga"
FOCUS_MODES = (FOCUS_LEGACY_LOGO,)
# Connectors used by a focused campaign when the operator does not choose them.
FOCUS_DEFAULT_SOURCES = {FOCUS_LEGACY_LOGO: ("serper", "lens")}
# Connectors that never contribute to a focused campaign (domain-level discovery without page content).
_FOCUS_SKIPPED = {FOCUS_LEGACY_LOGO: {"typosquat", "crtsh", "urlscan"}}


def build_legacy_logo_hypotheses(brand: BrandProfile) -> list[SearchHypothesis]:
    """Focused campaign on the *legacy* logo: pages where third parties show the brand's logo as an
    image. Search engines index text, not logos, so the queries target the text that surrounds a
    partner-bank showcase (dealers, solar integrators, credit brokers) in every state; the reverse
    image search (``lens``) complements them with pages that never write the brand name."""
    name = brand.display_name
    alt = next((t for t in brand.strong_terms if t.lower() != name.lower()), name)
    excl = " ".join(f"-site:{d}" for d in brand.official_domains)
    ex = f"{excl} -site:reclameaqui.com.br -site:jusbrasil.com.br -site:jus.br"
    hs: list[SearchHypothesis] = []

    def add_h(hid: str, desc: str, dorks: list[str]) -> None:
        hs.append(
            SearchHypothesis(
                id=hid, description=desc, grounding_prompt=_dork_grounding_prompt(desc, dorks), keyword_queries=dorks
            )
        )

    add_h(
        "H13-legacy-focus:vitrine-multibanco",
        "Lojas que exibem os logotipos dos bancos/financeiras parceiros (vitrine multibanco)",
        [
            f'"bancos parceiros" "{alt}" {ex}',
            f'"bancos parceiros" "{name}" {ex}',
            f'"financeiras parceiras" ("{alt}" OR "{name}") {ex}',
            f'"trabalhamos com" ("{alt}" OR "{name}") financiamento {ex}',
            f'"nossos parceiros" "{alt}" financiamento {ex}',
            f'"{alt}" "Santander" "Itaú" financiamento seminovos {ex}',
            f'"{alt}" "Banco Pan" "Bradesco" financiamento {ex}',
            f'"{alt}" "Safra" "Santander" financiamento veículos {ex}',
            f'"{name}" "Banco Pan" "Santander" financiamento loja {ex}',
        ],
    )
    add_h(
        "H13-legacy-focus:paginas-financiamento",
        "Páginas de financiamento de revendas de veículos, motos e caminhões citando a marca",
        [
            f'inurl:financiamento "{alt}" {ex}',
            f'inurl:financie ("{alt}" OR "BV") "Santander" {ex}',
            f'"{alt}" financiamento seminovos "simule" loja {ex}',
            f'"{alt}" motos financiamento revenda {ex}',
            f'"{alt}" caminhões financiamento {ex}',
        ],
    )
    add_h(
        "H13-legacy-focus:energia-solar",
        "Integradores de energia solar com financiamento pela marca",
        [
            f'"{alt}" "energia solar" financiamento parceiros {ex}',
            f'"BV" "Solfácil" "Santander" energia solar {ex}',
            f'"{alt}" financiamento "placas solares" {ex}',
        ],
    )
    add_h(
        "H13-legacy-focus:credito-consignado",
        "Correspondentes e promotoras de crédito/consignado exibindo a marca",
        [
            f'"{alt}" consignado correspondente {ex}',
            f'"{alt}" empréstimo "correspondente bancário" {ex}',
            f'"{name}" refinanciamento correspondente {ex}',
        ],
    )
    for region, states in BR_REGIONS.items():
        add_h(
            f"H13-legacy-focus:regiao-{region}",
            f"Revendas e lojas com financiamento pela marca na região {region}",
            [f'"{alt}" financiamento veículos "{uf}" {ex}' for uf in states],
        )
    return hs


_LEGAL_SUFFIX_RE = re.compile(
    r"(?i)(?<![\w])(?:ltda|limitada|me|epp|eireli|s\s*/\s*a|s\.?\s*a\.?|sociedade an[oô]nima|"
    r"sociedade simples|ss)(?![\w])\.?|&\s*(?:cia|companhia)\.?|(?<![\w])(?:cia|companhia)\.?\s*$"
)
_PERSON_ID_RE = re.compile(r"\d{11}")  # MEI names embed the owner's CPF
_BANK_NAME_RE = re.compile(r"(?i)^\s*(banco|bco|caixa econ|cooperativa de cr[eé]dito|cresol|sicoob|sicredi)\b")


def coban_search_name(raw: str) -> str | None:
    """Searchable company name: legal suffixes and digits removed. None when the record should not
    be searched (natural person / MEI with CPF, financial institution, too generic)."""
    if not raw or _PERSON_ID_RE.search(raw) or _BANK_NAME_RE.search(raw):
        return None
    name = re.sub(r"\d+", " ", _LEGAL_SUFFIX_RE.sub(" ", raw))
    name = re.sub(r"[\s\-–.,/&]+$", "", re.sub(r"\s+", " ", name)).strip(" -–.,/&")
    name = re.sub(r"(?i)\s+(?:e|de|da|do|dos|das)$", "", name)
    return name if len(name) >= 6 and len(name.split()) <= 8 else None


def coban_hypotheses(
    records: dict[str, dict[str, str]],
    brand: BrandProfile,
    *,
    limit: int,
    batch: int = 4,
    exclude: set[str] | None = None,
) -> tuple[list[SearchHypothesis], int]:
    """H9: search the web presence of *registered* correspondents for misuse of the brand.

    Complements (never replaces) the open-web hypotheses. Picks the most recently registered
    correspondents not yet searched in earlier campaigns, so successive runs rotate through the
    registry. Returns (hypotheses, number of correspondents selected)."""
    if limit <= 0 or not records:
        return [], 0
    exclude = exclude or set()
    ordered = sorted(
        records.items(),
        key=lambda kv: (kv[1].get("data_cadastro_recente") or kv[1].get("data_cadastro") or "", kv[0]),
        reverse=True,
    )
    picked: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for cnpj, rec in ordered:
        if cnpj in exclude:
            continue
        name = coban_search_name(rec.get("nome") or rec.get("name") or "")
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())
        picked.append((cnpj, name))
        if len(picked) >= limit:
            break
    excl = " ".join(f"-site:{d}" for d in brand.official_domains)
    alt = next((t for t in brand.strong_terms if t.lower() != brand.display_name.lower()), brand.display_name)
    out: list[SearchHypothesis] = []
    for i in range(0, len(picked), batch):
        group = picked[i : i + batch]
        listing = "\n".join(f"  - {n} (CNPJ {c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]})" for c, n in group)
        out.append(
            SearchHypothesis(
                id="H9-coban:" + ",".join(c for c, _ in group),
                description="Correspondentes cadastrados: presença on-line e uso da marca ("
                + "; ".join(n for _, n in group)
                + ")",
                grounding_prompt=(
                    f"Objetivo: localizar sites, landing pages e perfis em redes sociais (Instagram, Facebook, "
                    f"TikTok, WhatsApp/linktree) das empresas abaixo, que são correspondentes cadastrados, e "
                    f"verificar como usam a marca {brand.display_name} / {alt}.\n"
                    f"Empresas:\n{listing}\n"
                    f"Liste as páginas encontradas de cada empresa (até 4 por empresa) e informe se a página usa "
                    f"o nome/logotipo da marca em destaque, se se apresenta como canal oficial, banco ou agência, "
                    f"e se exibe razão social, CNPJ e a condição de correspondente bancário."
                ),
                keyword_queries=[f'"{n}" ("{brand.display_name}" OR "{alt}" OR "Feirão BV") {excl}' for _, n in group],
            )
        )
    return out, len(picked)


def urlscan_queries(brand: BrandProfile) -> list[str]:
    titles = " OR ".join(f'page.title:"{t}"' for t in brand.strong_terms[:3])
    not_official = " AND ".join(f"NOT page.domain:{d}" for d in brand.official_domains) or "*"
    return [f"({titles}) AND {not_official}"]


@dataclass
class _ConnectorState:
    connector: Connector
    errors_in_row: int = 0
    disabled: str | None = None
    executed: int = 0
    succeeded: int = 0


@dataclass
class PlannedQuery:
    connector: str
    hypothesis: SearchHypothesis
    query: str


@dataclass
class DiscoveryAgent:
    brand: BrandProfile
    connectors: list[Connector]
    policy: UrlPolicy
    max_queries: int = 40
    diagnostics: list[str] = field(default_factory=list)
    # (connector, query) pairs already executed in an earlier (interrupted) run of the campaign:
    # skipped on resume and counted against ``max_queries``.
    already_done: set[tuple[str, str]] = field(default_factory=set)
    # Extra hypotheses appended after the open-web ones (e.g. H9 COBAN-seeded searches).
    extra_hypotheses: list[SearchHypothesis] = field(default_factory=list)
    # Focused campaign (FOCUS_MODES): its own hypotheses replace the open-web sweep.
    focus: str | None = None

    def plan(self, available: list[Connector]) -> list[PlannedQuery]:
        """Interleave hypotheses across connectors so a small budget still diversifies."""
        focused = self.focus == FOCUS_LEGACY_LOGO
        base = build_legacy_logo_hypotheses(self.brand) if focused else build_hypotheses(self.brand)
        hyps = base + list(self.extra_hypotheses)
        skipped = _FOCUS_SKIPPED.get(self.focus or "", set())
        per_connector: list[list[PlannedQuery]] = []
        for conn in available:
            plans: list[PlannedQuery] = []
            if conn.name in skipped:
                self.diagnostics.append(f"{conn.name}: not used in a focused campaign ({self.focus})")
            elif conn.name == "grounding":
                plans = [PlannedQuery(conn.name, h, h.grounding_prompt) for h in hyps]
            elif conn.name == "urlscan":
                h = SearchHypothesis(
                    id="H7-urlscan", description="Páginas escaneadas com a marca no título", grounding_prompt=""
                )
                plans = [PlannedQuery(conn.name, h, q) for q in urlscan_queries(self.brand)]
            elif conn.name == "crtsh":
                h = SearchHypothesis(
                    id="H7-crtsh", description="Certificados SSL recém-emitidos para domínios da marca", grounding_prompt=""
                )
                raw_queries = [
                    d.replace("-?", "") for d in self.brand.domain_patterns if not any(c in d for c in ("^", "$", "(", ")", "[", "]"))
                ] or [t.replace(" ", "") for t in self.brand.strong_terms[:2]]
                crtsh_queries = [f"%{q.strip('.%')}%" for q in raw_queries if q.strip('.%')]
                plans = [PlannedQuery(conn.name, h, q) for q in crtsh_queries]
            elif conn.name == "lens":
                h = SearchHypothesis(
                    id="H12-legacy-logo:lens",
                    description="Páginas com imagem semelhante ao logotipo antigo (busca reversa de imagem)",
                    grounding_prompt="",
                )
                plans = [PlannedQuery(conn.name, h, q) for q in getattr(conn, "queries", list)()]
            elif conn.name == "typosquat":
                queries = conn.queries() if hasattr(conn, "queries") else []
                plans = [
                    PlannedQuery(
                        conn.name,
                        SearchHypothesis(
                            id=f"H8-lookalike:{q}",
                            description="Domínios registrados semelhantes aos oficiais (typo/combosquatting)",
                            grounding_prompt="",
                        ),
                        q,
                    )
                    for q in queries
                ]
            else:
                plans = [PlannedQuery(conn.name, h, q) for h in hyps for q in h.keyword_queries]
            per_connector.append(plans)
        out: list[PlannedQuery] = []
        for batch in itertools.zip_longest(*per_connector):
            out.extend(p for p in batch if p is not None and (p.connector, p.query) not in self.already_done)
        return out[: max(0, self.max_queries - len(self.already_done))]

    async def run(self, imported: list[DiscoveryHit] | None = None, on_query=None) -> DiscoveryOutcome:  # type: ignore[no-untyped-def]
        outcome = DiscoveryOutcome()
        outcome.hits.extend(imported or [])
        if imported:
            outcome.sources_used.append("import")
            outcome.diagnostics.append(f"{len(imported)} URL(s) imported manually")

        available: list[Connector] = []
        for conn in self.connectors:
            ok, msg = conn.available()
            outcome.diagnostics.append(msg)
            if ok:
                available.append(conn)
        if not available:
            if not imported:
                outcome.diagnostics.append(
                    "NO automatic discovery mechanism is available. Configure Vertex AI "
                    "(SIMIO_GCP_PROJECT + SIMIO_ANALYSIS_MODEL) for Google Search Grounding, or an "
                    "optional connector key (SIMIO_BRAVE_API_KEY / SIMIO_SERPER_API_KEY / "
                    "SIMIO_URLSCAN_API_KEY), or import URLs with --urls-file / --urls."
                )
            return outcome

        states = {c.name: _ConnectorState(c) for c in available}
        planned_queries = self.plan(available)
        outcome.diagnostics.extend(self.diagnostics)
        for planned in planned_queries:
            st = states[planned.connector]
            if st.disabled:
                continue
            try:
                hits, record = await st.connector.run(planned.hypothesis, planned.query)
            except BudgetExceeded as exc:
                outcome.diagnostics.append(f"{planned.connector}: stopped, {exc}")
                st.disabled = "budget"
                continue
            st.executed += 1
            outcome.queries.append(record)
            if on_query is not None:
                on_query(record, hits)
            outcome.hits.extend(hits)
            if record.status == "ERROR":
                st.errors_in_row += 1
                ok, msg = st.connector.available()
                if not ok or st.errors_in_row >= MAX_CONSECUTIVE_ERRORS:
                    st.disabled = record.error or msg
                    outcome.diagnostics.append(
                        f"{planned.connector}: disabled for this run -> fallback to remaining sources ({st.disabled})"
                    )
            else:
                st.errors_in_row = 0
                st.succeeded += 1
        for name, st in states.items():
            if st.succeeded:
                outcome.sources_used.append(name)
        return outcome


# --------------------------------------------------------------------------------------
# Deduplication and prioritisation
# --------------------------------------------------------------------------------------
@dataclass
class CandidateGroup:
    normalized_url: str
    url: str  # representative original URL (first seen)
    hostname: str
    registrable_domain: str
    hits: list[DiscoveryHit]
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    excluded_reason: str | None = None


def deduplicate(
    hits: list[DiscoveryHit], policy: UrlPolicy
) -> tuple[list[CandidateGroup], list[tuple[DiscoveryHit, str]]]:
    """Group hits by normalised URL. Returns (groups, rejected[(hit, reason)])."""
    groups: OrderedDict[str, CandidateGroup] = OrderedDict()
    rejected: list[tuple[DiscoveryHit, str]] = []
    for hit in hits:
        try:
            policy.check_static(hit.original_url)
            norm = normalize_url(hit.original_url)
        except (PolicyViolation, ValueError) as exc:
            rejected.append((hit, f"invalid/blocked URL: {exc}"))
            continue
        host = hostname_of(norm)
        if norm not in groups:
            groups[norm] = CandidateGroup(norm, hit.original_url, host, registrable_domain(host), [])
        groups[norm].hits.append(hit)
    return list(groups.values()), rejected


def prioritise(groups: list[CandidateGroup], brand: BrandProfile) -> list[CandidateGroup]:
    """Deterministic pre-collection score. It only orders the queue; it is not a finding."""
    for g in groups:
        titles_text = " ".join(filter(None, (h.title or "" for h in g.hits)))
        snippets_text = " ".join(filter(None, (h.snippet or "" for h in g.hits)))
        text = f"{titles_text} {snippets_text}".strip()
        is_manual = any(h.source.startswith("import") for h in g.hits)

        if brand.is_official(g.hostname):
            g.excluded_reason = "official institutional domain"
            continue
        if brand.is_official_url(g.url, title=titles_text, snippet=snippets_text):
            g.excluded_reason = "official institutional social profile"
            continue
        if not is_manual and _SOCIAL_INDEX_RE.search(g.normalized_url):
            g.excluded_reason = "social platform tag/search index page"
            continue
        if not is_manual and _DIRECT_DOCUMENT_RE.search(g.normalized_url):
            g.excluded_reason = "direct binary/PDF document URL"
            continue
        if not is_manual and _INSTITUTION_DIRECTORY_RE.search(g.normalized_url):
            g.excluded_reason = "public business directory entry for the institution itself"
            continue
        if not is_manual and domain_matches(g.hostname, _ABANDONED_BUILDER_HOSTS):
            g.excluded_reason = "generic free-builder boleto/phishing page outside correspondent scope"
            continue

        score, reasons = 0.0, []
        if pat := brand.domain_brand_match(g.hostname):
            score += 3
            reasons.append(f"brand pattern in domain ({pat})")
        if domain_matches(g.hostname, EPHEMERAL_HOSTING_DOMAINS) and (pat or brand.mentions(text)):
            score += 2
            reasons.append("ephemeral/free-hosting domain")
        if brand.strong_mentions(text):
            score += 2
            reasons.append("brand mentioned in title/snippet")
        claims = brand.official_claims(text)
        if claims and (
            brand.strong_mentions(text)
            or any(brand.strong_mentions(c) for c in claims)
            or (bool(_BANKING_HINT_RE.search(text)) and not brand.has_homonym_context(text))
        ):
            score += 3
            reasons.append("official-channel language in title/snippet")
        if brand.sensitive_service_mentions(text):
            score += 2.5
            reasons.append("sensitive billing/settlement service in title/snippet")
        if brand.disclosures(text):
            score -= 1
            reasons.append("correspondent disclosure in snippet")
        if len(brand.competitor_mentions(text)) >= 2 and not pat and not brand.official_claims(text):
            score -= 1.5
            reasons.append("multi-bank partner list in snippet")
        if brand.has_homonym_context(text) and not brand.strong_mentions(text):
            score -= 4
            reasons.append("likely non-bank homonym context in title/snippet")
        if not pat and not brand.official_claims(text) and (
            _EDITORIAL_URL_RE.search(g.normalized_url) or _EDITORIAL_TITLE_RE.search(titles_text)
        ):
            score -= 3
            reasons.append("editorial/blog/help article (de-prioritised)")
        lookalike = next((h for h in g.hits if h.source == "typosquat"), None)
        if lookalike is not None:
            score += 3
            reasons.append(
                f"lookalike of {lookalike.metadata.get('lookalike_of')} ({lookalike.metadata.get('fuzzer')})"
            )
        if any(h.source == "lens" for h in g.hits):
            score += 2
            reasons.append("image visually similar to the legacy logo (reverse image search)")
        sources = {h.source for h in g.hits}
        hyps = {h.hypothesis_id for h in g.hits if h.hypothesis_id}
        if len(sources) > 1 or len(hyps) > 1:
            score += min(2, len(sources) + len(hyps) - 2) + 0.5
            reasons.append(f"seen by {len(sources)} source(s) / {len(hyps)} hypothesis(es)")
        if is_manual:
            score += 5
            reasons.append("manually imported by operator")
        if brand.is_deprioritized(g.hostname):
            score -= 4
            reasons.append("news/review/regulator domain (de-prioritised)")
        if _MARKETPLACE_ITEM_RE.search(g.normalized_url) and not any(
            (h.hypothesis_id or "").startswith("H10-fraud") for h in g.hits
        ):
            score -= 5
            reasons.append("individual marketplace vehicle listing (de-prioritised)")
        if _ANCILLARY_PATH_RE.search(g.normalized_url):
            score -= 2.5
            reasons.append("ancillary legal/sitemap page (de-prioritised)")
        if g.normalized_url.rstrip("/").count("/") <= 2:
            score += 0.5
            reasons.append("home page")
        g.score, g.reasons = score, reasons

    kept = [g for g in groups if g.excluded_reason is None]
    kept.sort(key=lambda g: (-g.score, g.normalized_url))

    # Domain diversity decay: prevent a single multi-page site (e.g. regional SEO pages from the
    # same domain) from monopolising the --limit queue ahead of uninspected domains. The root/home
    # page of a domain is never penalised because Res. CMN 4.935 art. 14, III, "a" specifically
    # references the correspondent's home page.
    seen_per_domain: dict[str, int] = {}
    for g in kept:
        is_manual = any(h.source.startswith("import") for h in g.hits)
        is_home = g.normalized_url.rstrip("/").count("/") <= 2
        if is_manual or domain_matches(g.hostname, SHARED_PLATFORM_DOMAINS):
            continue
        count = seen_per_domain.get(g.registrable_domain, 0) + 1
        seen_per_domain[g.registrable_domain] = count
        if count > 2 and not is_home:
            penalty = 2.5 * (count - 2)
            g.score -= penalty
            g.reasons.append(f"additional page on same registrable domain (#{count}, -{penalty:.1f})")

    kept.sort(key=lambda g: (-g.score, g.normalized_url))
    return kept + [g for g in groups if g.excluded_reason is not None]


def summarize_queries(records: list[QueryRecord]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in records:
        out[f"{r.source}:{r.status}"] = out.get(f"{r.source}:{r.status}", 0) + 1
    return out
