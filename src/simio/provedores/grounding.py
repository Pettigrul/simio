"""Google Search Grounding connector (Gemini on Vertex AI).

The model is asked an investigative question with the ``google_search`` tool enabled.
URLs are taken **exclusively** from ``grounding_metadata.grounding_chunks`` -- never from the
model's free text -- so no URL is invented. Grounding redirect URIs
(``vertexaisearch.cloud.google.com/grounding-api-redirect/...``) are resolved with a single
non-following request and the target is validated by the URL policy.

Grounding is not an exhaustive web index; results are a sample guided by the hypotheses.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import unquote, urljoin

import httpx

from simio.nucleo.modelos import HipoteseBusca, RegistroConsulta, ResultadoDescoberta
from simio.nucleo.urls import hostname_de
from simio.provedores.llm import ErroTransitorioLLM, GatewayLLM, LLMIndisponivel, OrcamentoEsgotado
from simio.seguranca.politica_url import PoliticaUrl, ViolacaoPolitica

log = logging.getLogger(__name__)

HOSTS_REDIRECIONAMENTO = frozenset({"vertexaisearch.cloud.google.com"})
HOSTS_RESULTADO_BUSCA = frozenset({"www.google.com", "google.com"})
_RESULTADO_YAHOO_RE = re.compile(r'/RU=([^/]+)/RK=\d+/RS=[^"\']+["\'][^>]*>(.*?)</a>', re.DOTALL)
_DOMINIOS_SOCIAIS = ("instagram.com/", "facebook.com/", "tiktok.com/", "linkedin.com/")

INSTRUCOES_GROUNDING = (
    "Você está apoiando uma auditoria de conformidade de correspondentes bancários (Resolução CMN "
    "nº 4.935/2021) e proteção de marca. Use a busca do Google para encontrar páginas públicas na "
    "web brasileira e em redes sociais que correspondam especificamente à hipótese abaixo.\n"
    "Priorize encontrar:\n"
    "- sites e landing pages de correspondentes bancários, promotoras de crédito, lojas/revendas de "
    "veículos e empresas de energia solar que utilizem a marca ou o logotipo do banco em destaque, "
    "ofereçam simuladores próprios, promovam feirões ou omitam no site sua razão social, CNPJ e a "
    "declaração obrigatória de correspondente bancário;\n"
    "- perfis comerciais, páginas e publicações de terceiros em redes sociais que usem a marca como "
    "se fossem canais ou representantes oficiais.\n"
    "Exclua das buscas e não retorne:\n"
    "- domínios e perfis oficiais do próprio grupo (bv.com.br, bancobv.com.br, bvfinanceira.com.br, "
    "bvinvestimentos.com.br, napista.com.br, bvopen.com.br, meufinanciamentosolar.com.br);\n"
    "- notícias de imprensa, reclamações (Reclame Aqui), processos judiciais (Jusbrasil/Tribunais), "
    "órgãos públicos (.gov.br), arquivos PDF avulsos de leilão e guias telefônicos/cadastrais que "
    "apenas listam o CNPJ/endereço da própria BV Financeira S.A.\n"
    "Para cada página encontrada, descreva brevemente como ela utiliza a marca e se exibe ou omite "
    "sua razão social, CNPJ e condição de correspondente.\n\n"
    "Hipótese: "
)


class ConectorGrounding:
    nome = "grounding"

    def __init__(
        self,
        gateway: GatewayLLM | None,
        modelo: str | None,
        politica: PoliticaUrl,
        dominios_excluidos: list[str],
        timeout_http: float = 15.0,
        transporte: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.gateway = gateway
        self.modelo = modelo
        self.politica = politica
        self.dominios_excluidos = dominios_excluidos
        self.timeout_http = timeout_http
        self.transporte = transporte
        self.motivo_desativacao: str | None = None

    def disponivel(self) -> tuple[bool, str]:
        if self.gateway is None:
            return False, "grounding: Vertex AI not configured (SIMIO_PROJETO_GCP / model) or offline"
        if not self.modelo:
            return False, "grounding: no discovery/analysis model configured"
        if self.motivo_desativacao:
            return False, f"grounding: disabled after error: {self.motivo_desativacao}"
        return True, f"grounding: model={self.modelo}"

    async def resolver(self, uri: str) -> tuple[str | None, str | None]:
        """Resolve grounding redirect URIs. Returns (url, error)."""
        host = hostname_de(uri)
        if host in HOSTS_RESULTADO_BUSCA:
            return None, "search-results page reference (not a target page)"
        if host not in HOSTS_REDIRECIONAMENTO:
            return uri, None
        try:
            await self.politica.verificar_async(uri)
            kwargs: dict = {"timeout": self.timeout_http, "follow_redirects": False}
            if self.transporte is not None:
                kwargs["transport"] = self.transporte
            else:
                kwargs["trust_env"] = True
            async with httpx.AsyncClient(**kwargs) as c, c.stream("GET", uri) as r:
                loc = r.headers.get("location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    alvo = urljoin(uri, loc)
                    self.politica.verificar_estatico(alvo)
                    return alvo, None
                return None, f"redirect not resolved (HTTP {r.status_code})"
        except ViolacaoPolitica as exc:
            return None, f"policy: {exc}"
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"

    async def executar(
        self, hipotese: HipoteseBusca, consulta: str
    ) -> tuple[list[ResultadoDescoberta], RegistroConsulta]:
        assert self.gateway is not None and self.modelo is not None
        prompt = INSTRUCOES_GROUNDING + consulta
        try:
            res = await self.gateway.busca_grounding(
                agente="descoberta.grounding",
                modelo=self.modelo,
                prompt=prompt,
                dominios_excluidos=self.dominios_excluidos,
            )
        except OrcamentoEsgotado:
            raise
        except LLMIndisponivel as exc:
            self.motivo_desativacao = str(exc)[:300]
            return [], RegistroConsulta(
                fonte=self.nome, consulta=consulta, id_hipotese=hipotese.id, status="ERRO", erro=self.motivo_desativacao
            )
        except (ErroTransitorioLLM, Exception) as exc:
            return [], RegistroConsulta(
                fonte=self.nome,
                consulta=consulta,
                id_hipotese=hipotese.id,
                status="ERRO",
                erro=f"{type(exc).__name__}: {exc}"[:500],
            )

        resultados: list[ResultadoDescoberta] = []
        nao_resolvidos: list[dict[str, str]] = []
        for src in res.fontes:
            url, err = await self.resolver(src.uri)
            if url is None:
                nao_resolvidos.append({"uri": src.uri, "title": src.titulo or "", "error": err or ""})
                continue
            resultados.append(
                ResultadoDescoberta(
                    fonte=self.nome,
                    url_original=url,
                    consulta=consulta,
                    id_hipotese=hipotese.id,
                    titulo=src.titulo,
                    trecho=" | ".join(src.textos_suporte)[:1000] or None,
                    posicao=src.indice_trecho + 1,
                    metadados={
                        "uri_grounding": src.uri,
                        "dominio_grounding": src.dominio,
                        "consultas_busca_web": res.consultas_busca_web,
                        "modelo": res.modelo,
                    },
                )
            )
        registro = RegistroConsulta(
            fonte=self.nome,
            consulta=consulta,
            id_hipotese=hipotese.id,
            status="OK" if resultados else "VAZIA",
            qtd_resultados=len(resultados),
            metadados={
                "consultas_busca_web": res.consultas_busca_web,
                "fragmentos_grounding": len(res.fontes),
                "nao_resolvidos": nao_resolvidos,
                # Kept verbatim (unmodified) as required by the grounding terms of service.
                "html_ponto_entrada_busca": res.html_ponto_entrada_busca,
                "trecho_texto_modelo": res.texto[:1500],
            },
        )
        # Vertex AI Search Grounding strips instagram.com and facebook.com from grounding_chunks.
        # When executing social platform hypotheses in live mode (no mock transport), supplement with
        # direct web dork queries so public Instagram/Facebook reels, posts and profiles are discovered.
        if self.transporte is None and hipotese.id.startswith("H6-plataforma:") and hipotese.consultas_palavras_chave:
            resultados_sociais = await self._complementar_dorks_sociais(hipotese)
            urls_vistas = {h.url_original for h in resultados}
            for sh in resultados_sociais:
                if sh.url_original not in urls_vistas:
                    urls_vistas.add(sh.url_original)
                    resultados.append(sh)
            registro.qtd_resultados = len(resultados)
            if resultados:
                registro.status = "OK"
        return resultados, registro

    async def _complementar_dorks_sociais(self, hipotese: HipoteseBusca) -> list[ResultadoDescoberta]:
        saida: list[ResultadoDescoberta] = []
        cabecalhos = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_http, trust_env=True, follow_redirects=True, headers=cabecalhos
            ) as c:
                for dork in hipotese.consultas_palavras_chave:
                    # Strip excess -site: operators for the social search engine query
                    dork_limpo = re.sub(r"\s+-site:\S+", "", dork).strip()
                    r = await c.get("https://br.search.yahoo.com/search", params={"p": dork_limpo, "n": 20})
                    if r.status_code != 200:
                        continue
                    for idx, m in enumerate(_RESULTADO_YAHOO_RE.finditer(r.text), 1):
                        url_bruta = unquote(m.group(1)).strip()
                        if not url_bruta.startswith(("http://", "https://")):
                            continue
                        if not any(d in url_bruta.lower() for d in _DOMINIOS_SOCIAIS):
                            continue
                        try:
                            self.politica.verificar_estatico(url_bruta)
                        except ViolacaoPolitica:
                            continue
                        titulo = html.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).strip()
                        saida.append(
                            ResultadoDescoberta(
                                fonte=self.nome,
                                url_original=url_bruta,
                                consulta=dork_limpo,
                                id_hipotese=hipotese.id,
                                titulo=titulo[:300] or None,
                                posicao=idx,
                                metadados={"complemento": "social_dork"},
                            )
                        )
        except Exception as exc:
            log.debug("social dork supplement skipped: %s", exc)
        return saida
