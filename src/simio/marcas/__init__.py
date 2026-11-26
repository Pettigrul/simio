from __future__ import annotations

import re
import tomllib
import unicodedata
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from simio.nucleo import traducao_legado as legado
from simio.nucleo.urls import dominio_corresponde, hostname_de, normalizar_url

DIR_MARCAS = Path(__file__).parent


def normalizar_texto(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _normalizar_caractere(c: str) -> str:
    minuscula = c.lower()
    if len(minuscula) != 1:
        minuscula = c
    base = [x for x in unicodedata.normalize("NFKD", minuscula) if not unicodedata.combining(x)]
    return base[0] if base else minuscula


def normalizar_alinhado(texto: str) -> tuple[str, str]:
    # Os dois textos têm o mesmo tamanho: o trecho achado no texto normalizado serve para citar o
    # original exato, com acento e caixa.
    t = unicodedata.normalize("NFC", texto or "")
    return t, "".join(_normalizar_caractere(c) for c in t)


_CONTEXTO_SUFIXO_EMPRESA = re.compile(
    r"\b(holding|group|groep|international|europe|nederland|netherlands|beheer|investments?)\s*$"
)

_DIVISOR_FRASES = re.compile(r"(?<=[.!?;|•])\s+|\n+|\s+[-–—|]\s+")


def sentencas(texto: str) -> list[str]:
    return [x.strip() for x in _DIVISOR_FRASES.split(texto or "") if x.strip()]


def _regex_frases(frases: list[str]) -> re.Pattern[str] | None:
    normalizado = sorted({normalizar_texto(p) for p in frases if p.strip()}, key=len, reverse=True)
    if not normalizado:
        return None
    # Espaço da frase também casa com "", "-", "_" ou ".", para pegar "BancoBV" e "Banco-BV".
    corpo = "|".join(re.escape(p).replace(r"\ ", r"[\s\-_.]*") for p in normalizado)
    return re.compile(rf"(?<![\w]){'(?:' + corpo + ')'}(?![\w])")


class PerfilMarca(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: str
    nome_exibicao: str
    idioma: str = "pt-BR"
    termos_fortes: list[str]
    termos_fracos: list[str] = Field(default_factory=list)
    dominios_oficiais: list[str] = Field(default_factory=list)
    perfis_sociais_oficiais: list[str] = Field(default_factory=list)
    nomes_instituicao: list[str] = Field(default_factory=list)
    cnpjs_instituicao: list[str] = Field(default_factory=list)
    padroes_dominio: list[str] = Field(default_factory=list)
    produtos: list[str] = Field(default_factory=list)
    tipos_estabelecimento: list[str] = Field(default_factory=list)
    regioes: list[str] = Field(default_factory=list)
    plataformas: list[str] = Field(default_factory=list)
    frases_oficialidade: list[str] = Field(default_factory=list)
    frases_declaracao_correspondente: list[str] = Field(default_factory=list)
    frases_parceria: list[str] = Field(default_factory=list)
    instituicoes_concorrentes: list[str] = Field(default_factory=list)
    frases_servicos_sensiveis: list[str] = Field(default_factory=list)
    frases_contexto_homonimo: list[str] = Field(default_factory=list)
    dominios_despriorizados: list[str] = Field(default_factory=list)
    # Organizações sem relação que usam a mesma sigla; a triagem de ameaças nunca as aponta.
    dominios_homonimos: list[str] = Field(default_factory=list)
    # provedores/typosquat.py
    sementes_typosquat: list[str] = Field(default_factory=list)
    rotulos_combo_typosquat: list[str] = Field(default_factory=list)
    palavras_chave_typosquat: list[str] = Field(default_factory=list)
    # Imagens da logo antiga (caminho relativo a marcas/), comparadas com as capturas em agentes/visual.py.
    referencias_logo_antiga: list[str] = Field(default_factory=list)
    # Sites que só catalogam ou noticiam a logo antiga: o achado fica registrado, mas não sobe a classificação.
    dominios_isentos_logo_antiga: list[str] = Field(default_factory=list)
    # Cópias públicas da logo antiga para a busca reversa (lens).
    urls_busca_logo_antiga: list[str] = Field(default_factory=list)

    def caminhos_logo_antiga(self) -> list[Path]:
        return [p if p.is_absolute() else DIR_MARCAS / p for p in map(Path, self.referencias_logo_antiga)]

    @cached_property
    def _re_forte(self) -> re.Pattern[str] | None:
        return _regex_frases(self.termos_fortes)

    @cached_property
    def _re_fraco(self) -> re.Pattern[str] | None:
        return _regex_frases(self.termos_fracos)

    @cached_property
    def _re_oficialidade(self) -> re.Pattern[str] | None:
        return _regex_frases(self.frases_oficialidade)

    @cached_property
    def _re_declaracao(self) -> re.Pattern[str] | None:
        return _regex_frases(self.frases_declaracao_correspondente)

    @cached_property
    def _re_parceria(self) -> re.Pattern[str] | None:
        return _regex_frases(self.frases_parceria)

    @cached_property
    def _re_concorrentes(self) -> re.Pattern[str] | None:
        return _regex_frases(self.instituicoes_concorrentes)

    @cached_property
    def _re_servicos_sensiveis(self) -> re.Pattern[str] | None:
        return _regex_frases(self.frases_servicos_sensiveis)

    @cached_property
    def _re_homonimo(self) -> re.Pattern[str] | None:
        return _regex_frases(self.frases_contexto_homonimo)

    @cached_property
    def _res_dominio(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.padroes_dominio]

    @staticmethod
    def _encontrar(rx: re.Pattern[str] | None, texto: str) -> list[str]:
        if rx is None or not texto:
            return []
        orig, normalizado = normalizar_alinhado(texto)
        return [orig[m.start() : m.end()] for m in rx.finditer(normalizado)]

    def mencoes_fortes(self, texto: str) -> list[str]:
        return self._encontrar(self._re_forte, texto)

    def mencoes_fracas(self, texto: str) -> list[str]:
        # Ignora o termo curto dentro de host/URL ("bv.com.br") e como sufixo de empresa estrangeira
        # ("Example Holding BV").
        rx = self._re_fraco
        if rx is None or not texto:
            return []
        orig, normalizado = normalizar_alinhado(texto)
        saida = []
        for m in rx.finditer(normalizado):
            antes, depois = normalizado[: m.start()], normalizado[m.end() :]
            if re.match(r"\.[a-z0-9]", depois) or re.search(r"[./@:]$", antes):
                continue  # parte de host, URL ou e-mail
            if _CONTEXTO_SUFIXO_EMPRESA.search(antes[-40:]):
                continue
            saida.append(orig[m.start() : m.end()])
        return saida

    def mencoes(self, texto: str) -> list[str]:
        return self.mencoes_fortes(texto) + self.mencoes_fracas(texto)

    def alegacoes_oficiais(self, texto: str) -> list[str]:
        return self._encontrar(self._re_oficialidade, texto)

    def declaracoes(self, texto: str) -> list[str]:
        return self._encontrar(self._re_declaracao, texto)

    def mencoes_parceria(self, texto: str) -> list[str]:
        return self._encontrar(self._re_parceria, texto)

    def mencoes_concorrentes(self, texto: str) -> list[str]:
        encontrados = self._encontrar(self._re_concorrentes, texto)
        saida: list[str] = []
        vistos: set[str] = set()
        for item in encontrados:
            canonico = re.sub(r"^banco\s+", "", normalizar_texto(item)).strip()
            if canonico and canonico not in vistos:
                vistos.add(canonico)
                saida.append(item)
        return saida

    def mencoes_servicos_sensiveis(self, texto: str) -> list[str]:
        return self._encontrar(self._re_servicos_sensiveis, texto)

    def tem_contexto_homonimo(self, texto: str) -> bool:
        return bool(self._encontrar(self._re_homonimo, texto))

    def sem_declaracoes(self, texto: str) -> str:
        # "X Ltda é correspondente bancário do Banco BV" cita a marca por obrigação: é transparência,
        # não destaque da marca. Por isso as frases de declaração saem do texto.
        return " ".join(s for s in sentencas(texto) if not self.declaracoes(s))

    def declaracoes_nomeando_instituicao(self, texto: str) -> list[str]:
        # Art. 14, III: a declaração precisa identificar a contratante pelo nome de mercado.
        return [s for s in sentencas(texto) if self.declaracoes(s) and self.mencoes_fortes(s)]

    def eh_nome_instituicao(self, valor: str) -> bool:
        """True se o valor é só o nome da instituição ou de empresa/produto do grupo (BV Financeira, NaPista)."""
        v = re.sub(r"[^a-z0-9 ]+", " ", normalizar_texto(valor)).strip()
        v = re.sub(r"\s+", " ", v)
        if not v:
            return False
        nomes = {
            re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", normalizar_texto(n))).strip()
            for n in self.nomes_instituicao + self.termos_fortes
        }
        if v in nomes:
            return True
        removido = v
        for n in sorted(nomes, key=len, reverse=True):
            removido = removido.replace(n, " ")
        resto = re.sub(
            r"\b(s a|sa|s\.a|ltda|oficial|site|o|a|do|da|de|e|credito|financiamento|investimento|investimentos)\b",
            " ",
            removido,
        )
        tem_inst = bool(self.mencoes_fortes(valor)) or any(n in v for n in nomes if len(n) >= 6)
        return tem_inst and not resto.strip()

    def correspondencia_marca_dominio(self, hostname: str) -> str | None:
        host = hostname.lower()
        for rx in self._res_dominio:
            if rx.search(host):
                return rx.pattern
        return None

    def eh_oficial(self, hostname: str) -> bool:
        return dominio_corresponde(hostname, self.dominios_oficiais)

    def eh_url_oficial(self, url: str, titulo: str | None = None, trecho: str | None = None) -> bool:
        host = hostname_de(url)
        if self.eh_oficial(host):
            return True
        if not self.perfis_sociais_oficiais:
            return False
        try:
            norm = normalizar_url(url).lower()
        except Exception:
            norm = url.strip().lower()
        # Tira esquema e prefixos de país/celular (br.linkedin.com, m.facebook.com).
        sem_prefixo = re.sub(r"^https?://(?:(?:www|m|br|pt)\.)?", "", norm).rstrip("/")
        perfis = set()
        for perfil in self.perfis_sociais_oficiais:
            perfil_limpo = re.sub(r"^https?://(?:(?:www|m|br|pt)\.)?", "", perfil.strip().lower()).rstrip("/")
            if not perfil_limpo:
                continue
            if (
                sem_prefixo == perfil_limpo
                or sem_prefixo.startswith(perfil_limpo + "/")
                or sem_prefixo.startswith(perfil_limpo + "?")
            ):
                return True
            slug = perfil_limpo.rsplit("/", 1)[-1].lstrip("@")
            if slug:
                perfis.add(slug)
        # Link direto de post/reel (instagram.com/reel/XYZ/) não traz o @ no caminho, mas o título do
        # buscador começa com "banco BV (@bancobv)" ou "banco BV on Instagram:".
        if perfis and dominio_corresponde(
            host, ("instagram.com", "tiktok.com", "facebook.com", "x.com", "twitter.com")
        ):
            texto_meta = normalizar_texto(f"{titulo or ''} {trecho or ''}")
            for h in perfis:
                if f"(@{h})" in texto_meta or f"{normalizar_texto(self.nome_exibicao)} on instagram:" in texto_meta:
                    return True
        return False

    def eh_despriorizado(self, hostname: str) -> bool:
        return dominio_corresponde(hostname, self.dominios_despriorizados)

    def eh_isento_logo_antiga(self, hostname: str) -> bool:
        return dominio_corresponde(hostname, self.dominios_isentos_logo_antiga)

    def todos_termos(self) -> list[str]:
        return self.termos_fortes + self.termos_fracos


def carregar_marca(marca: str, arquivo_marca: Path | None = None) -> PerfilMarca:
    caminho = arquivo_marca or DIR_MARCAS / f"{marca}.toml"
    if not caminho.is_file():
        disponivel = sorted(p.stem for p in DIR_MARCAS.glob("*.toml"))
        raise FileNotFoundError(
            f"Perfil de marca '{marca}' não encontrado ({caminho}). Disponíveis: {', '.join(disponivel)}"
        )
    with caminho.open("rb") as arq:
        dados = tomllib.load(arq)
    # Perfil personalizado escrito antes da tradução (display_name, strong_terms...).
    dados = legado.traduzir_modelo(PerfilMarca, dados)
    dados["referencias_logo_antiga"] = [
        legado.ASSETS_MARCA.get(r, r) for r in dados.get("referencias_logo_antiga", [])
    ]
    return PerfilMarca(**dados)
