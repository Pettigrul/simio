from __future__ import annotations

import itertools
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field

from simio.marcas import PerfilMarca
from simio.nucleo.modelos import DesfechoDescoberta, HipoteseBusca, RegistroConsulta, ResultadoDescoberta
from simio.nucleo.urls import dominio_corresponde, dominio_registravel, hostname_de, normalizar_url
from simio.provedores.busca import Conector
from simio.provedores.llm import OrcamentoEsgotado
from simio.provedores.typosquat import descrever_semelhanca
from simio.seguranca.politica_url import PoliticaUrl, ViolacaoPolitica

log = logging.getLogger(__name__)

# Erros seguidos que desligam o conector até o fim da execução.
MAX_ERROS_CONSECUTIVOS = 3

# Plataformas em que cada caminho pertence a um terceiro diferente.
DOMINIOS_PLATAFORMA_COMPARTILHADA = (
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

DOMINIOS_HOSPEDAGEM_EFEMERA = (
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

_HOSTS_CONSTRUTORES_ABANDONADOS = ("weebly.com", "site123.me")
DOMINIOS_HOSPEDAGEM_GRATUITA = (
    *DOMINIOS_HOSPEDAGEM_EFEMERA,
    *_HOSTS_CONSTRUTORES_ABANDONADOS,
    "webnode.page",
    "godaddysites.com",
    "hostingersite.com",
)

_INDICE_SOCIAL_RE = re.compile(
    r"(?i)^https?://(?:(?:www|m)\.)?(?:"
    r"instagram\.com/(?:popular|explore|tags)/"
    r"|tiktok\.com/(?:discover|tag)/"
    r"|facebook\.com/(?:public|hashtag|watch/explore)/"
    r")"
)

_ITEM_MARKETPLACE_RE = re.compile(
    r"(?i)^https?://(?:[\w-]+\.)?(?:"
    r"olx\.com\.br/.*/(?:autos-e-pecas|veiculos-e-pecas)/"
    r"|mercadolivre\.com\.br/MLB-\d+"
    r"|webmotors\.com\.br/comprar/"
    r"|mobiauto\.com\.br/comprar/"
    r"|usadosbr\.com/carros-e-utilitarios/"
    r")"
)

_CAMINHO_AUXILIAR_RE = re.compile(
    r"(?i)/(?:politica-de-privacidade|privacidade|privacy-policy|privacy|termos-de-uso|"
    r"termos-e-condicoes|lgpd|cookies|trabalhe-conosco|vagas)(?:/|$|\.)"
    r"|(?:^|/)(?:post-)?sitemap[\w-]*\.xml$"
)

_DOCUMENTO_DIRETO_RE = re.compile(
    r"(?i)(?:\.(?:pdf|docx?|xlsx?|pptx?|zip)(?:$|\?)|blob\.core\.windows\.net/pdf/)"
)

_DIRETORIO_INSTITUICOES_RE = re.compile(
    r"(?i)/(?:empresa|servicos|sobre|nomes|consulta-gratis|consulta-empresa)/"
    r"[^/?#]*(?:bv-financeira-sa|bv-financeira-s-a|banco-bv-s-a|banco-votorantim)"
)

_URL_EDITORIAL_RE = re.compile(
    r"(?i)(?:^https?://(?:ajuda|help|suporte|blog|noticias|conteudos)\."
    r"|/(?:blog|noticias?|artigos?|materias?|imprensa|historias-clientes|customer-story|"
    r"hc/pt-br/articles|jurisprudencia|processos?|publieditorial|post-patrocinado)(?:/|$))"
)

_TITULO_EDITORIAL_RE = re.compile(
    r"(?i)(?:^(?:como\s+(?:ser|falar|adicionar|financiar|quitar|negociar|simular|cancelar|encontrar)\b"
    r"|o\s+que\s+fazer\b|guia\s+(?:completo|definitivo|pr[aá]tico)\b)"
    r"|\b(?:avalia[cç][aã]o\s+completa|manual\s+completo|advogados?\b|entrega\s+amig[aá]vel)\b)"
)

_PISTA_BANCARIA_RE = re.compile(
    r"(?i)\b(banco|financeira|financiamento|financie|financiar|cr[eé]dito|empr[eé]stimo|"
    r"consignado|promotora|correspondente|boleto|quita[cç][aã]o|parcela|sac\b|ouvidoria|"
    r"refinanciamento|ve[ií]culos?|seminovos|autom[oó]veis|motos?|solar|feir[aã]o|0800)\b"
)


REGIOES_BR = {
    "Norte": ["Acre", "Amapá", "Amazonas", "Pará", "Rondônia", "Roraima", "Tocantins"],
    "Nordeste": [
        "Alagoas", "Bahia", "Ceará", "Maranhão", "Paraíba", "Pernambuco", "Piauí", "Rio Grande do Norte", "Sergipe",
    ],
    "Centro-Oeste": ["Distrito Federal", "Goiás", "Mato Grosso", "Mato Grosso do Sul"],
    "Sudeste": ["Espírito Santo", "Minas Gerais", "Rio de Janeiro", "São Paulo"],
    "Sul": ["Paraná", "Rio Grande do Sul", "Santa Catarina"],
}


def _termo_alternativo(marca: PerfilMarca) -> str:
    return next((t for t in marca.termos_fortes if t.lower() != marca.nome_exibicao.lower()), marca.nome_exibicao)


def _prompt_grounding_dork(descricao: str, dorks: list[str]) -> str:
    unido = "\n".join(f"  - {q}" for q in dorks)
    return (
        f"Objetivo: {descricao}.\n"
        f"Execute no Google Search as seguintes consultas (dorks):\n{unido}\n"
        f"Liste de forma abrangente as páginas públicas encontradas (até 12 resultados distintos), "
        f"informando para cada uma o nome do estabelecimento/perfil, a URL e como a marca é "
        f"utilizada (destaque visual, título, feirão, simulador, redes sociais e se há ou não "
        f"identificação clara de CNPJ e condição de correspondente bancário)."
    )


def montar_hipoteses(marca: PerfilMarca) -> list[HipoteseBusca]:
    nome = marca.nome_exibicao
    alt = _termo_alternativo(marca)
    excl = " ".join(f"-site:{d}" for d in marca.dominios_oficiais)
    excl_ruido = f"{excl} -site:reclameaqui.com.br -site:jusbrasil.com.br -site:jus.br"
    hs: list[HipoteseBusca] = []

    # TODO: os três montar_hipoteses* repetem este adicionar_h e as exclusões; dá para virar um helper só.
    def adicionar_h(hid: str, desc: str, dorks: list[str]) -> None:
        hs.append(
            HipoteseBusca(
                id=hid,
                descricao=desc,
                prompt_grounding=_prompt_grounding_dork(desc, dorks),
                consultas_palavras_chave=dorks,
            )
        )

    adicionar_h(
        "H1-identidade:title-brand",
        "Terceiros e promotoras que usam a marca no título da página",
        [
            f'(intitle:"{nome}" OR intitle:"{alt}" OR intitle:"BV Financiamentos" OR intitle:"BV Promotora") '
            f"(financiamento OR crédito OR veículos OR correspondente) {excl_ruido}",
            f'(intitle:"BV Veículos" OR intitle:"Agência BV") (financiamento OR seminovos OR veículos) {excl_ruido}',
        ],
    )
    adicionar_h(
        "H1-identidade:inurl-brand",
        "Sites de terceiros com o nome da marca ou páginas de financiamento na URL",
        [
            f"(inurl:banco-bv OR inurl:bancobv OR inurl:bv-financeira OR inurl:bvfinanceira) "
            f"(financiamento OR veículos OR crédito OR correspondente) {excl_ruido}",
            f'(inurl:financie OR inurl:financiar) ("{alt}" OR "{nome}") (veículos OR simulador OR loja) {excl_ruido}',
        ],
    )
    adicionar_h(
        "H1-identidade:logo-assets",
        "Páginas de financiamento que expõem arquivos de logotipo da marca ou logo antiga (B vermelho / V azul)",
        [
            f'(inurl:logo-bv OR "logo-bv" OR "logo-bv-financeira" OR "logo antiga bv" OR "bv logo antiga") (financiamento OR veículos OR crédito) {excl_ruido}',
            f'("Logo {nome}" OR "Logo {alt}" OR "logo BV" OR "logo BV Financeira") (financiamento OR veículos OR consignado) '
            f"-site:brandsoftheworld.com -site:logospng.org {excl_ruido}",
            f'("BV Financeira" OR "{nome}") ("logo antiga" OR "logotipo antigo" OR "marca antiga") {excl_ruido}',
        ],
    )

    adicionar_h(
        f"H2-produto:{marca.produtos[0]}",
        "Páginas de financiamento de veículos de lojas e multimarcas com destaque para a marca",
        [
            f'inurl:financiamento "{nome}" veículos {excl_ruido}',
            f'inurl:financiamento "{alt}" (veículos OR seminovos OR multimarcas) {excl_ruido}',
            f'intitle:financiamento ("{alt}" OR "{nome}") veículos {excl_ruido}',
        ],
    )
    adicionar_h(
        "H2-produto:financiamento de carro e multimarcas",
        "Revendas de seminovos e lojas multimarcas que divulgam financiamento da marca",
        [
            f'"{nome}" financiamento (loja OR revenda OR seminovos OR multimarcas) veículos {excl_ruido}',
            f'"{alt}" (seminovos OR multimarcas OR revenda) financiamento {excl_ruido}',
            f'("financiamos" OR "trabalhamos com" OR "financie") ("{alt}" OR "{nome}") veículos {excl_ruido}',
        ],
    )
    adicionar_h(
        "H2-produto:financiamento de moto",
        "Lojas de motos que oferecem financiamento usando a marca em destaque",
        [
            f'("{alt}" OR "{nome}") motos financiamento loja {excl_ruido}',
        ],
    )
    adicionar_h(
        "H2-produto:financiamento de energia solar",
        "Integradores e correspondentes de energia solar que utilizam a marca",
        [
            f'("{nome}" OR "{alt}") "energia solar" (financiamento OR integrador OR parceiro) {excl_ruido}',
            f'("financiamento solar" OR "Correspondente BV" OR "parceiro BV") '
            f'("{nome}" OR "energia solar") {excl_ruido}',
        ],
    )
    adicionar_h(
        "H2-produto:refinanciamento e crédito com garantia",
        "Promotoras, correspondentes e corretoras oferecendo refinanciamento, crédito consignado, empréstimo pessoal e garantia com a marca",
        [
            f'"{nome}" ("crédito com garantia" OR "home equity" OR refinanciamento) '
            f"(correspondente OR promotora OR financeira) {excl_ruido}",
            f'("{alt}" OR "{nome}") ("crédito consignado" OR "empréstimo consignado" OR consignado) '
            f'(promotora OR correspondente OR "taxas" OR simulação) {excl_ruido}',
            f'("BV Financeira" OR "Banco BV") ("Daycoval" OR "Pan" OR "Safra" OR "BMG") '
            f"(consignado OR refinanciamento OR empréstimo OR promotora) {excl_ruido}",
        ],
    )

    adicionar_h(
        "H3-servico:correspondente-vinculo",
        "Correspondentes bancários e promotoras que declaram vínculo com a marca",
        [
            f'("correspondente bancário" OR "Correspondente no País" OR "correspondente do {nome}") '
            f'"{nome}" {excl_ruido}',
            f'("correspondente BV" OR "Somos correspondente" "{nome}" OR "correspondente" "{alt}") '
            f"financiamento {excl_ruido}",
            f'"correspondente" "Banco Votorantim" financiamento {excl_ruido}',
        ],
    )
    adicionar_h(
        "H3-servico:simulacao-whatsapp",
        "Atendimento e simulação de financiamento via WhatsApp apresentados em nome da marca",
        [
            f'("simulação" OR "atendimento {nome}") ("{nome}" OR "{alt}") whatsapp '
            f"(financiamento OR veículos) {excl_ruido}",
            f'("parceiro BV" OR "parceiro do {nome}" OR "parceria com o {nome}") financiamento {excl_ruido}',
        ],
    )

    adicionar_h(
        "H4-linguagem-oficial",
        "Uso de expressões de oficialidade ('parceiro oficial', 'representante', 'correspondente autorizado', 'agência BV')",
        [
            f'("parceiro oficial" OR "parceiros autorizados" OR "correspondente autorizado" '
            f'OR "correspondente bancário autorizado") ("{nome}" OR "{alt}") {excl_ruido}',
            f'("representante" OR "agência BV" OR "agência {nome}" OR "canal oficial" OR "site oficial") '
            f'("{nome}" OR "{alt}") financiamento {excl_ruido}',
        ],
    )

    adicionar_h(
        "H5-estabelecimento:feirao",
        "Lojas e multimarcas que divulgam 'Feirão Banco BV' / 'Feirão BV Financeira'",
        [
            f'("Feirão {nome}" OR "Feirão {alt}" OR "Feirão BV") '
            f"(loja OR veículos OR seminovos OR multimarcas) {excl_ruido}",
        ],
    )
    # As 27 UFs, um prompt de grounding por região.
    for regiao, ufs in REGIOES_BR.items():
        adicionar_h(
            f"H5-estabelecimento:regiao-{regiao}",
            f"Lojas, revendas e correspondentes da região {regiao} que divulgam financiamento da marca",
            [
                q
                for uf in ufs
                for q in (
                    f'"{alt}" "bancos parceiros" veículos "{uf}" {excl_ruido}',
                    f'("{nome}" OR "{alt}") financiamento (loja OR revenda OR correspondente OR promotora) "{uf}" '
                    f"{excl_ruido}",
                )
            ],
        )

    adicionar_h(
        "H6-plataforma:Instagram",
        "Perfis, Reels e publicações no Instagram de lojas e correspondentes usando a marca ('Parceiro BV', 'Correspondente', 'Agência BV', 'Feirão Banco BV')",
        [
            f'site:instagram.com "Feirão {nome}" OR "Feirão BV" {excl}',
            f'site:instagram.com ("correspondente" OR promotora OR "crédito consignado") ("{nome}" OR "{alt}") {excl}',
            f'site:instagram.com ("Parceiro BV" OR "BV Financeira") (veículos OR empréstimo OR consignado OR refinanciamento) {excl}',
            f'site:instagram.com/reel ("{nome}" OR "{alt}") (financiamento OR consignado OR veículos) {excl}',
        ],
    )
    adicionar_h(
        "H6-plataforma:Facebook",
        "Páginas e vídeos no Facebook divulgando 'Feirão Banco BV', consignado ou financiamento sob a marca",
        [
            f'site:facebook.com "Feirão {nome}" OR "Feirão {alt}" {excl}',
            f'site:facebook.com ("{alt}" OR "{nome}") (veículos OR consignado OR empréstimo OR refinanciamento) {excl}',
            f'site:facebook.com ("BV Financeira" OR "Banco BV") (promotora OR correspondente) {excl}',
        ],
    )
    adicionar_h(
        "H6-plataforma:TikTok-LinkedIn",
        "Perfis no TikTok e páginas de empresas no LinkedIn apresentando-se como correspondentes ou parceiros da marca",
        [
            f'site:tiktok.com ("{nome}" OR "{alt}") financiamento -inurl:/@bancobv -inurl:/discover/ {excl}',
            f'site:linkedin.com/company "correspondente" ("{nome}" OR "{alt}") -inurl:/company/bancobv {excl}',
        ],
    )
    adicionar_h(
        "H6-plataforma:WhatsApp",
        "Links e páginas de captação via WhatsApp (wa.me) utilizando a marca",
        [
            f'("wa.me" OR "api.whatsapp.com") ("{nome}" OR "{alt}") '
            f"(financiamento OR correspondente OR veículos OR consignado OR empréstimo) {excl_ruido}",
            f'("linktr.ee" OR "linkme.bio" OR "heylink.me") ("{nome}" OR "{alt}") {excl_ruido}',
        ],
    )

    adicionar_h(
        "H7-inteligencia-ameacas:sensitive-services",
        "Páginas de terceiros ofertando serviços sensíveis com a marca (2ª via boleto, quitação, negociação, quitação de contrato)",
        [
            f'("2ª via" OR "segunda via" OR "quitação" OR "quitar financiamento" OR "acordo") '
            f'("{nome}" OR "{alt}") (boleto OR contrato OR parcelas) {excl_ruido}',
            f'("central de atendimento" OR "fale com" OR "SAC" OR "0800") ("{nome}" OR "{alt}") '
            f"(boleto OR quitação OR negociação OR whatsapp) {excl_ruido}",
        ],
    )
    adicionar_h(
        "H7-inteligencia-ameacas:combosquatting-portals",
        "Páginas de terceiros com combosquatting ou simuladores se passando por canais de contratação ou login",
        [
            f'(intitle:"{nome}" OR intitle:"{alt}") (intitle:"simulador" OR intitle:"login" OR intitle:"portal" OR intitle:"atendimento") {excl_ruido}',
            f"(inurl:bancobv OR inurl:bvfinanceira OR inurl:bv-credito OR inurl:bv-online) "
            f"(simulacao OR portal OR login OR acesso OR proposta) {excl_ruido}",
        ],
    )

    # Pacotes do dorks.md que ficaram de fora dos grupos acima.
    adicionar_h(
        "H2-produto:bancos-parceiros",
        "Lojas e integradores que listam a marca entre 'bancos parceiros' (logotipo em vitrine multibanco)",
        [
            f'"bancos parceiros" "{nome}" {excl_ruido}',
            f'"bancos parceiros" "{alt}" {excl_ruido}',
            f'"{alt}" "Banco Pan" "Santander" financiamento veículos {excl_ruido}',
            f'"{nome}" "Banco Pan" "Santander" financiamento loja {excl_ruido}',
            f'"bancos parceiros" "energia solar" "BV" {excl_ruido}',
            f'"BV" "Solfácil" "Santander" financiamento energia solar {excl_ruido}',
        ],
    )
    adicionar_h(
        "H6-plataforma:YouTube-Kwai-Threads",
        "Vídeos e canais (YouTube, Kwai, Threads) de lojas e promotoras usando a marca",
        [
            f'site:youtube.com ("{nome}" OR "{alt}") (financiamento OR feirão OR loja OR consignado) {excl}',
            f'site:kwai.com ("{nome}" OR "{alt}" OR "Feirão BV") {excl}',
            f'site:threads.net ("{nome}" OR "{alt}") (financiamento OR feirão OR crédito) {excl}',
        ],
    )
    adicionar_h(
        "H6-plataforma:Telegram-grupos",
        "Canais do Telegram e grupos de WhatsApp oferecendo crédito/financiamento em nome da marca",
        [
            f'site:t.me ("{nome}" OR "{alt}") (empréstimo OR financiamento OR crédito OR consignado) {excl}',
            f'("chat.whatsapp.com" OR "grupo de whatsapp" OR "grupo do whatsapp") ("{nome}" OR "{alt}") '
            f"(empréstimo OR financiamento OR crédito) {excl_ruido}",
        ],
    )
    adicionar_h(
        "H6-plataforma:marketplaces",
        "Anúncios e lojas em marketplaces (OLX, Webmotors, Mercado Livre, iCarros, Mobiauto) usando a marca",
        [
            f'site:olx.com.br ("{alt}" OR "{nome}") {excl}',
            f'site:webmotors.com.br ("{alt}" OR "{nome}") {excl}',
            f'site:mercadolivre.com.br ("{nome}" OR "{alt}") (financiamento OR crédito OR empréstimo) {excl}',
            f'(site:icarros.com.br OR site:mobiauto.com.br) ("{nome}" OR "{alt}") (loja OR financiamento) {excl}',
        ],
    )

    marcas_ou = f'("{nome}" OR "{alt}")'
    adicionar_h(
        "H10-fraude:leilao-retomados",
        "Leilões falsos e venda de veículos 'retomados/apreendidos' em nome da marca",
        [
            f"(leilão OR leilões OR leiloeiro) {marcas_ou} (veículos OR carros OR motos) {excl_ruido}",
            f'("veículos retomados" OR "carros retomados" OR "veículos apreendidos" OR "busca e apreensão" '
            f'OR "recuperados") {marcas_ou} (venda OR compre OR lote OR "abaixo da fipe") {excl_ruido}',
        ],
    )
    adicionar_h(
        "H10-fraude:agio-repasse",
        "Venda de ágio, repasse e 'assumo/passo financiamento' de veículos financiados pela marca",
        [
            f'("assumo financiamento" OR "passo financiamento" OR "repasse de financiamento" OR "venda de ágio" '
            f'OR "carro com ágio" OR "transfiro financiamento") ("BV" OR "{nome}" OR "{alt}") {excl_ruido}',
        ],
    )
    adicionar_h(
        "H10-fraude:falso-emprestimo",
        "Falso empréstimo: crédito 'pré-aprovado', liberação imediata ou cobrança antecipada em nome da marca",
        [
            f'("crédito pré-aprovado" OR "empréstimo aprovado" OR "liberação imediata" OR "sem consulta" '
            f'OR "para negativados") {marcas_ou} {excl_ruido}',
            f'("taxa de liberação" OR "depósito antecipado" OR "taxa de cadastro" OR "pagamento antecipado") '
            f"{marcas_ou} (empréstimo OR crédito OR financiamento) {excl_ruido}",
        ],
    )
    adicionar_h(
        "H10-fraude:app-phishing",
        "Aplicativos falsos (APK), páginas de login e pedidos de atualização de dados em nome da marca",
        [
            f'("app {nome}" OR "aplicativo BV" OR "BV apk" OR "baixar app BV" OR "{nome} apk") '
            f"-site:play.google.com -site:apps.apple.com {excl}",
            f'("acesse sua conta" OR "atualize seus dados" OR "atualização cadastral" OR "token" OR "desbloqueio") '
            f"{marcas_ou} {excl_ruido}",
        ],
    )
    adicionar_h(
        "H10-fraude:central-falsa",
        "Telefones, WhatsApp e 'centrais' de atendimento não oficiais divulgados como sendo da marca",
        [
            f'("whatsapp oficial" OR "número do whatsapp" OR "telefone" OR "0800" OR "central de atendimento") '
            f"{marcas_ou} (contato OR atendimento OR falar) {excl_ruido} -site:telefone.info",
        ],
    )
    adicionar_h(
        "H10-fraude:agencia-listagem",
        "Listagens de 'agência/loja/escritório' da marca em diretórios e mapas de terceiros",
        [
            f'("agência {nome}" OR "agência {alt}" OR "loja {nome}" OR "escritório {nome}") '
            f'(endereço OR "como chegar" OR horário OR telefone) {excl_ruido}',
        ],
    )
    adicionar_h(
        "H10-fraude:vagas",
        "Vagas e 'renda extra' falsas usando a marca (digitador, correspondente, home office)",
        [
            f'(vagas OR "trabalhe conosco" OR "processo seletivo" OR "renda extra" OR "home office") {marcas_ou} '
            f"(correspondente OR promotora OR digitador OR vendedor OR comissão) {excl_ruido}",
        ],
    )
    adicionar_h(
        "H10-fraude:investimento-cartao",
        "Investimentos com rendimento garantido e cartões 'aprovados na hora' em nome da marca",
        [
            f'("rendimento garantido" OR "CDB" OR "lucro" OR "renda fixa") ("{nome}" OR "BV Investimentos") '
            f"(whatsapp OR telegram OR grupo OR invista) {excl_ruido}",
            f'("cartão BV" OR "cartão de crédito BV" OR "cartão {nome}") ("aprovado na hora" OR "sem consulta" '
            f'OR "limite alto" OR "solicite já" OR "para negativados") {excl_ruido}',
        ],
    )
    adicionar_h(
        "H10-fraude:quitacao-limpa-nome",
        "Ofertas de quitação com desconto, 'limpa nome' e revisão de juros usando a marca",
        [
            f'("limpa nome" OR "quitação com desconto" OR "redução de juros" OR "revisão de contrato" '
            f'OR "juros abusivos" OR "quitar com desconto") {marcas_ou} {excl_ruido}',
        ],
    )

    # NaPista é o marketplace de veículos do grupo.
    adicionar_h(
        "H11-napista:sites",
        "Sites e páginas de terceiros usando a marca NaPista como canal próprio ou oficial",
        [
            f'("NaPista" OR "Feirão NaPista" OR "Feirão Na Pista") (financiamento OR parceiro OR loja OR oficial) '
            f"{excl_ruido}",
            f'inurl:napista ("NaPista" OR financiamento) {excl_ruido}',
        ],
    )
    adicionar_h(
        "H11-napista:social",
        "Perfis e publicações em redes sociais usando a marca NaPista",
        [
            f'site:instagram.com ("NaPista" OR "Feirão NaPista") {excl}',
            f'site:facebook.com ("NaPista" OR "Feirão NaPista") (financiamento OR feirão OR loja) {excl}',
        ],
    )
    return hs


# Campanhas com foco (`simio investigar --foco`).
FOCO_AMPLA = "ampla"  # gravada sem foco (foco=None)
FOCO_LOGO_ANTIGA = "logo-antiga"
FOCO_AMEACAS = "ameacas"
MODOS_FOCO = (FOCO_AMPLA, FOCO_LOGO_ANTIGA, FOCO_AMEACAS)
# Fontes padrão quando o operador não escolhe.
FONTES_PADRAO_FOCO = {
    FOCO_LOGO_ANTIGA: ("serper", "lens"),
    FOCO_AMEACAS: ("grounding", "serper", "typosquat", "crtsh", "urlscan", "brave"),
}
_PULADOS_POR_FOCO = {FOCO_LOGO_ANTIGA: {"typosquat", "crtsh", "urlscan"}, FOCO_AMEACAS: {"lens"}}
# Sem buscas a partir do cadastro de COBANs (H9).
FOCOS_SEM_COBANS = frozenset({FOCO_AMEACAS})


def _prompt_grounding_ameaca(descricao: str, dorks: list[str]) -> str:
    unido = "\n".join(f"  - {q}" for q in dorks)
    return (
        f"Objetivo (inteligência de ameaças e proteção de marca): {descricao}.\n"
        f"Execute no Google Search as seguintes consultas (dorks):\n{unido}\n"
        f"Liste de forma abrangente as páginas públicas encontradas (até 12 resultados distintos) que possam "
        f"se passar pela instituição ou enganar seus clientes. Para cada uma, informe a URL, quem parece operar "
        f"a página e o que ela oferece ou pede (login, senha, CPF, dados de cartão, token, boleto ou Pix, "
        f"download de aplicativo, contato por WhatsApp/Telegram) e por que parece falsa ou perigosa."
    )


def montar_hipoteses_ameacas(marca: PerfilMarca) -> list[HipoteseBusca]:
    """Ameaças sem ligação com correspondentes; reaproveita H7, H10 e o Telegram da varredura ampla."""
    nome = marca.nome_exibicao
    alt = _termo_alternativo(marca)
    b = f'("{nome}" OR "{alt}")'
    excl = " ".join(f"-site:{d}" for d in marca.dominios_oficiais)
    ex = f"{excl} -site:reclameaqui.com.br -site:jusbrasil.com.br -site:jus.br"
    hs: list[HipoteseBusca] = []

    def adicionar_h(hid: str, desc: str, dorks: list[str]) -> None:
        hs.append(
            HipoteseBusca(
                id=hid,
                descricao=desc,
                prompt_grounding=_prompt_grounding_ameaca(desc, dorks),
                consultas_palavras_chave=dorks,
            )
        )

    adicionar_h(
        "H14-ameaca:login-falso",
        "Páginas de login/acesso bancário falsas usando a marca (phishing de credenciais)",
        [
            f'(intitle:"{nome}" OR intitle:"{alt}") (login OR entrar OR "acesse sua conta" OR "internet banking") {ex}',
            f'{b} ("digite sua senha" OR "informe sua senha" OR "senha eletrônica" OR "código de acesso") {ex}',
            f"(inurl:bancobv OR inurl:bvfinanceira OR inurl:bv-online OR inurl:bvonline) "
            f"(inurl:login OR inurl:acesso OR inurl:auth OR inurl:conta OR inurl:seguranca) {ex}",
        ],
    )
    adicionar_h(
        "H14-ameaca:portal-parceiro-falso",
        "Portais falsos de parceiro/correspondente (roubo de acesso de lojistas e promotoras)",
        [
            f'("portal do parceiro" OR "portal parceiro" OR "acesso parceiro" OR "área do parceiro") {b} login {ex}',
            f'(intitle:"parceiro BV" OR intitle:"BV parceiros" OR intitle:"portal BV") (login OR acesso OR senha) {ex}',
        ],
    )
    adicionar_h(
        "H14-ameaca:hospedagem-gratuita",
        "Páginas com a marca em hospedagem gratuita/efêmera (padrão de kits de phishing)",
        [
            f"(site:vercel.app OR site:netlify.app OR site:pages.dev OR site:github.io) {b}",
            f"(site:web.app OR site:firebaseapp.com OR site:lovable.app OR site:canva.site) {b}",
            f"(site:wixsite.com OR site:weebly.com OR site:site123.me OR site:webnode.page) {b} "
            f"(boleto OR acordo OR quitação OR empréstimo OR login)",
            f"(site:blogspot.com OR site:wordpress.com OR site:godaddysites.com OR site:hostingersite.com) {b} "
            f"(boleto OR acordo OR quitação OR empréstimo)",
        ],
    )
    adicionar_h(
        "H14-ameaca:boleto-pix-falso",
        "Geração de boleto/Pix e consulta de débitos fora dos canais oficiais",
        [
            f'("gerar boleto" OR "emitir boleto" OR "boleto atualizado" OR "chave pix") {b} '
            f"(acordo OR quitação OR parcela OR débito) {ex}",
            f'("consultar CPF" OR "consulte seu CPF" OR "digite seu CPF") {b} (acordo OR débito OR desconto OR boleto) {ex}',
        ],
    )
    adicionar_h(
        "H14-ameaca:taxa-antecipada",
        "Empréstimo/financiamento que cobra taxa antecipada em nome da marca",
        [
            f'{b} ("taxa de liberação" OR "taxa de cadastro" OR "seguro prestamista" OR "depósito antecipado") {ex}',
            f'{b} ("empréstimo aprovado" OR "crédito liberado") (pix OR whatsapp) {ex}',
        ],
    )
    adicionar_h(
        "H14-ameaca:paineis-dados",
        "Painéis de consulta, venda de dados, logins ou leads citando a marca",
        [
            f'("painel de consulta" OR "painel consulta" OR "consulta completa") {b} (CPF OR dados) {ex}',
            f"site:t.me {b} (painel OR \"consulta CPF\" OR logins OR \"base de dados\" OR leads)",
            f'(logins OR "contas" OR "acessos" OR "leads") {b} (venda OR vendo OR telegram) {ex}',
        ],
    )
    adicionar_h(
        "H14-ameaca:perfis-falsos",
        "Perfis em redes sociais se passando pelo atendimento da marca",
        [
            f"site:instagram.com {b} (atendimento OR central OR quitação OR acordo OR boleto)",
            f'site:facebook.com {b} ("atendimento oficial" OR quitação OR acordo OR "limpa nome")',
            f'site:tiktok.com {b} (quitação OR acordo OR empréstimo OR "dinheiro rápido")',
        ],
    )
    adicionar_h(
        "H14-ameaca:apps-falsos",
        "Aplicativos/APKs da marca fora das lojas oficiais",
        [
            f'{b} (apk OR "download") (site:apkpure.com OR site:apkcombo.com OR site:uptodown.com OR site:aptoide.com)',
            f'{b} ("baixar aplicativo" OR "instale o app" OR "apk") -site:play.google.com -site:apps.apple.com {ex}',
        ],
    )
    adicionar_h(
        "H14-ameaca:napista-golpe",
        "Golpes usando o marketplace do grupo (NaPista)",
        [f'"NaPista" (leilão OR retomados OR "sinal" OR pix OR "taxa") {ex} -site:napista.com.br'],
    )
    reaproveitado = [
        h
        for h in montar_hipoteses(marca)
        if h.id.startswith(("H7-", "H10-")) or h.id == "H6-plataforma:Telegram-grupos"
    ]
    for h in reaproveitado:
        hs.append(
            h.model_copy(update={"prompt_grounding": _prompt_grounding_ameaca(h.descricao, h.consultas_palavras_chave)})
        )
    return hs


def montar_hipoteses_logo_antiga(marca: PerfilMarca) -> list[HipoteseBusca]:
    # Buscador indexa texto, não logo: as consultas miram o texto em volta das vitrines de bancos
    # parceiros. O lens cobre as páginas que nem escrevem o nome da marca.
    nome = marca.nome_exibicao
    alt = _termo_alternativo(marca)
    excl = " ".join(f"-site:{d}" for d in marca.dominios_oficiais)
    ex = f"{excl} -site:reclameaqui.com.br -site:jusbrasil.com.br -site:jus.br"
    hs: list[HipoteseBusca] = []

    def adicionar_h(hid: str, desc: str, dorks: list[str]) -> None:
        hs.append(
            HipoteseBusca(
                id=hid,
                descricao=desc,
                prompt_grounding=_prompt_grounding_dork(desc, dorks),
                consultas_palavras_chave=dorks,
            )
        )

    adicionar_h(
        "H13-foco-logo-antiga:vitrine-multibanco",
        "Lojas que exibem os logotipos dos bancos/financeiras parceiros (vitrine multibanco)",
        [
            f'"bancos parceiros" "{alt}" {ex}',
            f'"bancos parceiros" "{nome}" {ex}',
            f'"financeiras parceiras" ("{alt}" OR "{nome}") {ex}',
            f'"trabalhamos com" ("{alt}" OR "{nome}") financiamento {ex}',
            f'"nossos parceiros" "{alt}" financiamento {ex}',
            f'"{alt}" "Santander" "Itaú" financiamento seminovos {ex}',
            f'"{alt}" "Banco Pan" "Bradesco" financiamento {ex}',
            f'"{alt}" "Safra" "Santander" financiamento veículos {ex}',
            f'"{nome}" "Banco Pan" "Santander" financiamento loja {ex}',
        ],
    )
    adicionar_h(
        "H13-foco-logo-antiga:paginas-financiamento",
        "Páginas de financiamento de revendas de veículos, motos e caminhões citando a marca",
        [
            f'inurl:financiamento "{alt}" {ex}',
            f'inurl:financie ("{alt}" OR "BV") "Santander" {ex}',
            f'"{alt}" financiamento seminovos "simule" loja {ex}',
            f'"{alt}" motos financiamento revenda {ex}',
            f'"{alt}" caminhões financiamento {ex}',
        ],
    )
    adicionar_h(
        "H13-foco-logo-antiga:energia-solar",
        "Integradores de energia solar com financiamento pela marca",
        [
            f'"{alt}" "energia solar" financiamento parceiros {ex}',
            f'"BV" "Solfácil" "Santander" energia solar {ex}',
            f'"{alt}" financiamento "placas solares" {ex}',
        ],
    )
    adicionar_h(
        "H13-foco-logo-antiga:credito-consignado",
        "Correspondentes e promotoras de crédito/consignado exibindo a marca",
        [
            f'"{alt}" consignado correspondente {ex}',
            f'"{alt}" empréstimo "correspondente bancário" {ex}',
            f'"{nome}" refinanciamento correspondente {ex}',
        ],
    )
    for regiao, estados in REGIOES_BR.items():
        adicionar_h(
            f"H13-foco-logo-antiga:regiao-{regiao}",
            f"Revendas e lojas com financiamento pela marca na região {regiao}",
            [f'"{alt}" financiamento veículos "{uf}" {ex}' for uf in estados],
        )
    return hs


_SUFIXO_JURIDICO_RE = re.compile(
    r"(?i)(?<![\w])(?:ltda|limitada|me|epp|eireli|s\s*/\s*a|s\.?\s*a\.?|sociedade an[oô]nima|"
    r"sociedade simples|ss)(?![\w])\.?|&\s*(?:cia|companhia)\.?|(?<![\w])(?:cia|companhia)\.?\s*$"
)
_ID_PESSOA_RE = re.compile(r"\d{11}")  # nome de MEI costuma trazer o CPF do dono
_NOME_BANCO_RE = re.compile(r"(?i)^\s*(banco|bco|caixa econ|cooperativa de cr[eé]dito|cresol|sicoob|sicredi)\b")


def nome_busca_coban(bruto: str) -> str | None:
    """Nome sem sufixo jurídico nem números; None para pessoa física/MEI, banco ou nome genérico."""
    if not bruto or _ID_PESSOA_RE.search(bruto) or _NOME_BANCO_RE.search(bruto):
        return None
    nome = re.sub(r"\d+", " ", _SUFIXO_JURIDICO_RE.sub(" ", bruto))
    nome = re.sub(r"[\s\-–.,/&]+$", "", re.sub(r"\s+", " ", nome)).strip(" -–.,/&")
    nome = re.sub(r"(?i)\s+(?:e|de|da|do|dos|das)$", "", nome)
    return nome if len(nome) >= 6 and len(nome.split()) <= 8 else None


def hipoteses_coban(
    registros: dict[str, dict[str, str]],
    marca: PerfilMarca,
    *,
    limite: int,
    lote: int = 4,
    excluir: set[str] | None = None,
) -> tuple[list[HipoteseBusca], int]:
    """H9: presença on-line dos correspondentes cadastrados. Pega os cadastros mais recentes ainda
    não pesquisados, para que campanhas seguidas façam rodízio. Retorna (hipóteses, qtd escolhida)."""
    if limite <= 0 or not registros:
        return [], 0
    excluir = excluir or set()
    ordenados = sorted(
        registros.items(),
        key=lambda kv: (kv[1].get("data_cadastro_recente") or kv[1].get("data_cadastro") or "", kv[0]),
        reverse=True,
    )
    escolhidos: list[tuple[str, str]] = []
    nomes_vistos: set[str] = set()
    for cnpj, rec in ordenados:
        if cnpj in excluir:
            continue
        nome = nome_busca_coban(rec.get("nome") or rec.get("name") or "")
        if not nome or nome.lower() in nomes_vistos:
            continue
        nomes_vistos.add(nome.lower())
        escolhidos.append((cnpj, nome))
        if len(escolhidos) >= limite:
            break
    excl = " ".join(f"-site:{d}" for d in marca.dominios_oficiais)
    alt = _termo_alternativo(marca)
    saida: list[HipoteseBusca] = []
    for i in range(0, len(escolhidos), lote):
        grupo = escolhidos[i : i + lote]
        listagem = "\n".join(f"  - {n} (CNPJ {c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]})" for c, n in grupo)
        saida.append(
            HipoteseBusca(
                id="H9-coban:" + ",".join(c for c, _ in grupo),
                descricao="Correspondentes cadastrados: presença on-line e uso da marca ("
                + "; ".join(n for _, n in grupo)
                + ")",
                prompt_grounding=(
                    f"Objetivo: localizar sites, landing pages e perfis em redes sociais (Instagram, Facebook, "
                    f"TikTok, WhatsApp/linktree) das empresas abaixo, que são correspondentes cadastrados, e "
                    f"verificar como usam a marca {marca.nome_exibicao} / {alt}.\n"
                    f"Empresas:\n{listagem}\n"
                    f"Liste as páginas encontradas de cada empresa (até 4 por empresa) e informe se a página usa "
                    f"o nome/logotipo da marca em destaque, se se apresenta como canal oficial, banco ou agência, "
                    f"e se exibe razão social, CNPJ e a condição de correspondente bancário."
                ),
                consultas_palavras_chave=[
                    f'"{n}" ("{marca.nome_exibicao}" OR "{alt}" OR "Feirão BV") {excl}' for _, n in grupo
                ],
            )
        )
    return saida, len(escolhidos)


def consultas_urlscan(marca: PerfilMarca) -> list[str]:
    titulos = " OR ".join(f'page.title:"{t}"' for t in marca.termos_fortes[:3])
    nao_oficial = " AND ".join(f"NOT page.domain:{d}" for d in marca.dominios_oficiais) or "*"
    return [f"({titulos}) AND {nao_oficial}"]


@dataclass
class _EstadoConector:
    conector: Conector
    erros_seguidos: int = 0
    desativado: str | None = None
    executadas: int = 0
    sucessos: int = 0


@dataclass
class ConsultaPlanejada:
    conector: str
    hipotese: HipoteseBusca
    consulta: str


@dataclass
class AgenteDescoberta:
    marca: PerfilMarca
    conectores: list[Conector]
    politica: PoliticaUrl
    max_consultas: int = 40
    diagnosticos: list[str] = field(default_factory=list)
    # (conector, consulta) já feitas numa execução interrompida: puladas ao retomar, mas contam no limite.
    ja_feitas: set[tuple[str, str]] = field(default_factory=set)
    # Entram depois das hipóteses da web aberta (ex.: H9).
    hipoteses_extras: list[HipoteseBusca] = field(default_factory=list)
    foco: str | None = None

    def planejar(self, disponivel: list[Conector]) -> list[ConsultaPlanejada]:
        # Intercala os conectores para que um orçamento pequeno não fique todo numa fonte só.
        construtor = {FOCO_LOGO_ANTIGA: montar_hipoteses_logo_antiga, FOCO_AMEACAS: montar_hipoteses_ameacas}
        base = construtor.get(self.foco or "", montar_hipoteses)(self.marca)
        hips = base + list(self.hipoteses_extras)
        pulados = _PULADOS_POR_FOCO.get(self.foco or "", set())
        # TODO: o plano depende de con.nome e de getattr/hasattr em "consultas"; melhor cada conector
        # declarar as próprias consultas no protocolo Conector.
        por_conector: list[list[ConsultaPlanejada]] = []
        for con in disponivel:
            planos: list[ConsultaPlanejada] = []
            if con.nome in pulados:
                self.diagnosticos.append(f"{con.nome}: não usado em campanha com foco ({self.foco})")
            elif con.nome == "grounding":
                planos = [ConsultaPlanejada(con.nome, h, h.prompt_grounding) for h in hips]
            elif con.nome == "urlscan":
                h = HipoteseBusca(
                    id="H7-urlscan", descricao="Páginas escaneadas com a marca no título", prompt_grounding=""
                )
                planos = [ConsultaPlanejada(con.nome, h, q) for q in consultas_urlscan(self.marca)]
            elif con.nome == "crtsh":
                h = HipoteseBusca(
                    id="H7-crtsh",
                    descricao="Certificados SSL recém-emitidos para domínios da marca",
                    prompt_grounding="",
                )
                consultas_brutas = [
                    d.replace("-?", "")
                    for d in self.marca.padroes_dominio
                    if not any(c in d for c in ("^", "$", "(", ")", "[", "]"))
                ] or [t.replace(" ", "") for t in self.marca.termos_fortes[:2]]
                consultas_crtsh = [f"%{q.strip('.%')}%" for q in consultas_brutas if q.strip('.%')]
                planos = [ConsultaPlanejada(con.nome, h, q) for q in consultas_crtsh]
            elif con.nome == "lens":
                h = HipoteseBusca(
                    id="H12-logo-antiga:lens",
                    descricao="Páginas com imagem semelhante ao logotipo antigo (busca reversa de imagem)",
                    prompt_grounding="",
                )
                planos = [ConsultaPlanejada(con.nome, h, q) for q in getattr(con, "consultas", list)()]
            elif con.nome == "typosquat":
                consultas = con.consultas() if hasattr(con, "consultas") else []
                planos = [
                    ConsultaPlanejada(
                        con.nome,
                        HipoteseBusca(
                            id=f"H8-semelhante:{q}",
                            descricao="Domínios registrados semelhantes aos oficiais (typo/combosquatting)",
                            prompt_grounding="",
                        ),
                        q,
                    )
                    for q in consultas
                ]
            else:
                planos = [ConsultaPlanejada(con.nome, h, q) for h in hips for q in h.consultas_palavras_chave]
            por_conector.append(planos)
        saida: list[ConsultaPlanejada] = []
        for lote in itertools.zip_longest(*por_conector):
            saida.extend(p for p in lote if p is not None and (p.conector, p.consulta) not in self.ja_feitas)
        return saida[: max(0, self.max_consultas - len(self.ja_feitas))]

    async def executar(
        self, importadas: list[ResultadoDescoberta] | None = None, ao_consultar=None
    ) -> DesfechoDescoberta:  # type: ignore[no-untyped-def]
        desfecho = DesfechoDescoberta()
        desfecho.resultados.extend(importadas or [])
        if importadas:
            desfecho.fontes_usadas.append("importacao")
            n = len(importadas)
            desfecho.diagnosticos.append(
                "1 URL importada manualmente" if n == 1 else f"{n} URLs importadas manualmente"
            )

        disponivel: list[Conector] = []
        for con in self.conectores:
            ok, msg = con.disponivel()
            desfecho.diagnosticos.append(msg)
            if ok:
                disponivel.append(con)
        if not disponivel:
            if not importadas:
                desfecho.diagnosticos.append(
                    "Nenhuma fonte de descoberta automática disponível. Configure o Vertex AI "
                    "(SIMIO_PROJETO_GCP + SIMIO_MODELO_ANALISE) para o grounding, uma chave de conector "
                    "(SIMIO_CHAVE_API_BRAVE / SIMIO_CHAVE_API_SERPER / SIMIO_CHAVE_API_URLSCAN) "
                    "ou importe URLs com --arquivo-urls / --urls."
                )
            return desfecho

        estados = {c.nome: _EstadoConector(c) for c in disponivel}
        consultas_planejadas = self.planejar(disponivel)
        desfecho.diagnosticos.extend(self.diagnosticos)
        for planejadas in consultas_planejadas:
            estado = estados[planejadas.conector]
            if estado.desativado:
                continue
            try:
                resultados, registro = await estado.conector.executar(planejadas.hipotese, planejadas.consulta)
            except OrcamentoEsgotado as exc:
                desfecho.diagnosticos.append(f"{planejadas.conector}: interrompido, {exc}")
                estado.desativado = "orçamento esgotado"
                continue
            estado.executadas += 1
            desfecho.consultas.append(registro)
            if ao_consultar is not None:
                ao_consultar(registro, resultados)
            desfecho.resultados.extend(resultados)
            if registro.status == "ERRO":
                estado.erros_seguidos += 1
                ok, msg = estado.conector.disponivel()
                if not ok or estado.erros_seguidos >= MAX_ERROS_CONSECUTIVOS:
                    estado.desativado = registro.erro or msg
                    desfecho.diagnosticos.append(
                        f"{planejadas.conector}: desligado nesta execução, seguindo com as outras fontes "
                        f"({estado.desativado})"
                    )
            else:
                estado.erros_seguidos = 0
                estado.sucessos += 1
        for nome, estado in estados.items():
            if estado.sucessos:
                desfecho.fontes_usadas.append(nome)
        return desfecho


@dataclass
class GrupoCandidatas:
    url_normalizada: str
    url: str  # primeira URL original vista
    hostname: str
    dominio_registravel: str
    resultados: list[ResultadoDescoberta]
    pontuacao: float = 0.0
    motivos: list[str] = field(default_factory=list)
    motivo_exclusao: str | None = None


def deduplicar(
    resultados: list[ResultadoDescoberta], politica: PoliticaUrl
) -> tuple[list[GrupoCandidatas], list[tuple[ResultadoDescoberta, str]]]:
    grupos: OrderedDict[str, GrupoCandidatas] = OrderedDict()
    rejeitados: list[tuple[ResultadoDescoberta, str]] = []
    for resultado in resultados:
        try:
            politica.verificar_estatico(resultado.url_original)
            norm = normalizar_url(resultado.url_original)
        except (ViolacaoPolitica, ValueError) as exc:
            rejeitados.append((resultado, f"URL inválida/bloqueada: {exc}"))
            continue
        host = hostname_de(norm)
        if norm not in grupos:
            grupos[norm] = GrupoCandidatas(norm, resultado.url_original, host, dominio_registravel(host), [])
        grupos[norm].resultados.append(resultado)
    return list(grupos.values()), rejeitados


def priorizar(grupos: list[GrupoCandidatas], marca: PerfilMarca, foco: str | None = None) -> list[GrupoCandidatas]:
    # Só ordena a fila de coleta; a pontuação não é achado.
    ameaca = foco == FOCO_AMEACAS
    for g in grupos:
        texto_titulos = " ".join(filter(None, (h.titulo or "" for h in g.resultados)))
        texto_trechos = " ".join(filter(None, (h.trecho or "" for h in g.resultados)))
        texto = f"{texto_titulos} {texto_trechos}".strip()
        eh_manual = any(h.fonte.startswith("import") for h in g.resultados)

        if marca.eh_oficial(g.hostname):
            g.motivo_exclusao = "domínio institucional oficial"
            continue
        if marca.eh_url_oficial(g.url, titulo=texto_titulos, trecho=texto_trechos):
            g.motivo_exclusao = "perfil social oficial da instituição"
            continue
        if not eh_manual and _INDICE_SOCIAL_RE.search(g.url_normalizada):
            g.motivo_exclusao = "página de tag/busca de rede social"
            continue
        if not eh_manual and _DOCUMENTO_DIRETO_RE.search(g.url_normalizada):
            g.motivo_exclusao = "link direto para PDF/arquivo"
            continue
        if not eh_manual and _DIRETORIO_INSTITUICOES_RE.search(g.url_normalizada):
            g.motivo_exclusao = "ficha da própria instituição em diretório de empresas"
            continue
        if not eh_manual and not ameaca and dominio_corresponde(g.hostname, _HOSTS_CONSTRUTORES_ABANDONADOS):
            g.motivo_exclusao = "página de boleto/phishing em construtor gratuito, fora do escopo de correspondentes"
            continue

        pontuacao, motivos = 0.0, []
        if ameaca and dominio_corresponde(g.hostname, _HOSTS_CONSTRUTORES_ABANDONADOS):
            pontuacao += 2
            motivos.append("construtor de sites gratuito (comum em kits de phishing)")
        if all(h.metadados.get("recuperado_de_redirecionamento_google") for h in g.resultados):
            pontuacao -= 1
            motivos.append("página inicial recuperada de link opaco do Google (página exata desconhecida)")
        if padrao := marca.correspondencia_marca_dominio(g.hostname):
            pontuacao += 3
            motivos.append(f"padrão da marca no domínio ({padrao})")
        if dominio_corresponde(g.hostname, DOMINIOS_HOSPEDAGEM_EFEMERA) and (padrao or marca.mencoes(texto)):
            pontuacao += 2
            motivos.append("hospedagem gratuita/efêmera")
        if marca.mencoes_fortes(texto):
            pontuacao += 2
            motivos.append("marca citada no título/trecho")
        alegacoes = marca.alegacoes_oficiais(texto)
        if alegacoes and (
            marca.mencoes_fortes(texto)
            or any(marca.mencoes_fortes(c) for c in alegacoes)
            or (bool(_PISTA_BANCARIA_RE.search(texto)) and not marca.tem_contexto_homonimo(texto))
        ):
            pontuacao += 3
            motivos.append("linguagem de canal oficial no título/trecho")
        if marca.mencoes_servicos_sensiveis(texto):
            pontuacao += 2.5
            motivos.append("serviço sensível (boleto/quitação) no título/trecho")
        if marca.declaracoes(texto):
            pontuacao -= 1
            motivos.append("declaração de correspondente no trecho")
        if len(marca.mencoes_concorrentes(texto)) >= 2 and not padrao and not marca.alegacoes_oficiais(texto):
            pontuacao -= 1.5
            motivos.append("lista de vários bancos parceiros no trecho")
        if marca.tem_contexto_homonimo(texto) and not marca.mencoes_fortes(texto):
            pontuacao -= 4
            motivos.append("provável homônimo fora do setor bancário")
        if not padrao and not marca.alegacoes_oficiais(texto) and (
            _URL_EDITORIAL_RE.search(g.url_normalizada) or _TITULO_EDITORIAL_RE.search(texto_titulos)
        ):
            pontuacao -= 3
            motivos.append("artigo de blog/notícia/ajuda (rebaixado)")
        semelhante = next((h for h in g.resultados if h.fonte == "typosquat"), None)
        if semelhante is not None:
            pontuacao += 3
            motivos.append(
                "domínio "
                + descrever_semelhanca(semelhante.metadados.get("semelhante_a"), semelhante.metadados.get("fuzzer"))
            )
        if any(h.fonte == "lens" for h in g.resultados):
            pontuacao += 2
            motivos.append("imagem parecida com a logo antiga (busca reversa)")
        fontes = {h.fonte for h in g.resultados}
        hips = {h.id_hipotese for h in g.resultados if h.id_hipotese}
        if len(fontes) > 1 or len(hips) > 1:
            pontuacao += min(2, len(fontes) + len(hips) - 2) + 0.5
            motivos.append(
                f"vista por {len(fontes)} {'fonte' if len(fontes) == 1 else 'fontes'} e "
                f"{len(hips)} {'hipótese' if len(hips) == 1 else 'hipóteses'}"
            )
        if eh_manual:
            pontuacao += 5
            motivos.append("importada manualmente pelo operador")
        if marca.eh_despriorizado(g.hostname):
            pontuacao -= 4
            motivos.append("domínio de notícia/reclamação/regulador (rebaixado)")
        if _ITEM_MARKETPLACE_RE.search(g.url_normalizada) and not any(
            (h.id_hipotese or "").startswith("H10-fraude") for h in g.resultados
        ):
            pontuacao -= 5
            motivos.append("anúncio individual de veículo em marketplace (rebaixado)")
        if _CAMINHO_AUXILIAR_RE.search(g.url_normalizada):
            pontuacao -= 2.5
            motivos.append("página acessória (privacidade/termos/sitemap) (rebaixada)")
        if g.url_normalizada.rstrip("/").count("/") <= 2:
            pontuacao += 0.5
            motivos.append("página inicial")
        g.pontuacao, g.motivos = pontuacao, motivos

    mantidos = [g for g in grupos if g.motivo_exclusao is None]
    mantidos.sort(key=lambda g: (-g.pontuacao, g.url_normalizada))

    # Penaliza páginas extras do mesmo domínio (ex.: páginas regionais de SEO) para não lotar o --limite.
    # A página inicial nunca é penalizada: o art. 14, III, "a" da Res. CMN 4.935 fala dela.
    vistos_por_dominio: dict[str, int] = {}
    for g in mantidos:
        eh_manual = any(h.fonte.startswith("import") for h in g.resultados)
        eh_inicial = g.url_normalizada.rstrip("/").count("/") <= 2
        if eh_manual or dominio_corresponde(g.hostname, DOMINIOS_PLATAFORMA_COMPARTILHADA):
            continue
        contagem = vistos_por_dominio.get(g.dominio_registravel, 0) + 1
        vistos_por_dominio[g.dominio_registravel] = contagem
        if contagem > 2 and not eh_inicial:
            penalidade = 2.5 * (contagem - 2)
            g.pontuacao -= penalidade
            g.motivos.append(f"página extra do mesmo domínio (#{contagem}, -{penalidade:.1f})")

    mantidos.sort(key=lambda g: (-g.pontuacao, g.url_normalizada))
    return mantidos + [g for g in grupos if g.motivo_exclusao is not None]


def resumir_consultas(registros: list[RegistroConsulta]) -> dict[str, int]:
    saida: dict[str, int] = {}
    for r in registros:
        saida[f"{r.fonte}:{r.status}"] = saida.get(f"{r.fonte}:{r.status}", 0) + 1
    return saida
