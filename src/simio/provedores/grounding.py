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


# As URLs vêm só de grounding_chunks, nunca do texto livre do modelo, para não aceitar URL inventada.
# O grounding devolve uma amostra guiada pela hipótese, não um índice completo da web.
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
            return False, "grounding: Vertex AI não configurado (SIMIO_PROJETO_GCP / modelo) ou modo offline"
        if not self.modelo:
            return False, "grounding: nenhum modelo de descoberta/análise configurado"
        if self.motivo_desativacao:
            return False, f"grounding: desligado após erro: {self.motivo_desativacao}"
        return True, f"grounding: modelo {self.modelo}"

    async def resolver(self, uri: str) -> tuple[str | None, str | None]:
        # Resolve o redirecionamento do Vertex com um único GET sem seguir; o destino passa pela política.
        host = hostname_de(uri)
        if host in HOSTS_RESULTADO_BUSCA:
            return None, "página de resultados de busca (não é página-alvo)"
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
                destino = r.headers.get("location")
                if r.status_code in (301, 302, 303, 307, 308) and destino:
                    alvo = urljoin(uri, destino)
                    self.politica.verificar_estatico(alvo)
                    return alvo, None
                return None, f"redirecionamento não resolvido (HTTP {r.status_code})"
        except ViolacaoPolitica as exc:
            return None, f"política: {exc}"
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
        for fonte in res.fontes:
            url, erro = await self.resolver(fonte.uri)
            if url is None:
                nao_resolvidos.append({"uri": fonte.uri, "title": fonte.titulo or "", "error": erro or ""})
                continue
            resultados.append(
                ResultadoDescoberta(
                    fonte=self.nome,
                    url_original=url,
                    consulta=consulta,
                    id_hipotese=hipotese.id,
                    titulo=fonte.titulo,
                    trecho=" | ".join(fonte.textos_suporte)[:1000] or None,
                    posicao=fonte.indice_trecho + 1,
                    metadados={
                        "uri_grounding": fonte.uri,
                        "dominio_grounding": fonte.dominio,
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
                # Guardado sem alteração: exigência dos termos de uso do grounding.
                "html_ponto_entrada_busca": res.html_ponto_entrada_busca,
                "trecho_texto_modelo": res.texto[:1500],
            },
        )
        # O grounding do Vertex tira instagram.com e facebook.com dos grounding_chunks. Nas hipóteses de
        # rede social (e só fora dos testes) completa com os mesmos dorks no Yahoo.
        # TODO: esses resultados saem com fonte "grounding"; uma fonte própria evitaria inflar a
        # contagem do grounding e deixaria o complemento testável.
        if self.transporte is None and hipotese.id.startswith("H6-plataforma:") and hipotese.consultas_palavras_chave:
            resultados_sociais = await self._complementar_dorks_sociais(hipotese)
            urls_vistas = {h.url_original for h in resultados}
            for social in resultados_sociais:
                if social.url_original not in urls_vistas:
                    urls_vistas.add(social.url_original)
                    resultados.append(social)
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
            log.debug("complemento de dorks sociais ignorado: %s", exc)
        return saida
