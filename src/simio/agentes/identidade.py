"""Identity Agent: who controls / represents the page?

Deterministic and fully traceable: every fact points to an evidence-catalog reference.
Facts (observed) and inferences (association reasoning) are kept separate.

``COBAN_CONFIRMADO`` requires a match by CNPJ in an *authoritative* registry supplied by the
operator (e.g. the contracting institution's open-data list of correspondents published under
Res. CMN 4.935/2021 art. 21, or an internal base). Advertising the brand's products is never
enough to infer a correspondent relationship.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from simio.agentes.evidencia import (
    CNPJ_RE,
    EMAIL_RE,
    NOME_ROTULADO_RE,
    PREFIXO_UI_NOME,
    RAZAO_SOCIAL_RE,
    SinaisPagina,
    cnpj_valido,
    formatar_cnpj,
    normalizar_cnpj,
)
from simio.marcas import PerfilMarca, normalizar_texto
from simio.nucleo.modelos import (
    AtribuicaoIdentidade,
    ClasseIdentidade,
    CorrespondenciaCadastro,
    Evidencia,
    FatoIdentidade,
    ItemEvidencia,
    Nivel,
)
from simio.nucleo.urls import dominio_registravel, hostname_de

SOCIAL_RE = re.compile(r"(?i)https?://(?:www\.)?(instagram|facebook|linkedin|tiktok|youtube)\.com/[^\s<>\"]+")
PERFIL_IG_TITULO_RE = re.compile(r"(?i)^Instagram\s+@([A-Za-z0-9._]+)")
PERFIL_IG_META_RE = re.compile(r"\(@([A-Za-z0-9._]+)\)")


# --------------------------------------------------------------------------------- registry
@dataclass
class CadastroCoban:
    """Operator-supplied list of authorised correspondents (CSV).

    Required column: ``cnpj``. Optional: ``nome``/``name``, ``contratante``, ``fonte``,
    ``data_referencia``. ``oficial`` must only be True for an official/approved source.
    """

    registros: dict[str, dict[str, str]] = field(default_factory=dict)
    fonte: str = "none"
    oficial: bool = False
    data_referencia: str | None = None

    @classmethod
    def carregar(cls, caminho: Path | None, oficial: bool = False, data_referencia: str | None = None) -> CadastroCoban:
        if caminho is None:
            return cls()
        if not caminho.is_file():
            raise FileNotFoundError(f"COBAN registry not found: {caminho}")
        recs: dict[str, dict[str, str]] = {}
        texto = caminho.read_text(encoding="utf-8-sig")
        primeiro = texto.splitlines()[0] if texto else ""
        delimitador = ";" if primeiro.count(";") > primeiro.count(",") else ","
        for linha in csv.DictReader(io.StringIO(texto), delimiter=delimitador):
            norm = {(k or "").strip().lower(): (v or "").strip() for k, v in linha.items()}
            cnpj = normalizar_cnpj(norm.get("cnpj", ""))
            if len(cnpj) == 14:
                recs[cnpj] = norm
        fonte = caminho.name + (f" (ref. {data_referencia})" if data_referencia else "")
        return cls(registros=recs, fonte=fonte, oficial=oficial, data_referencia=data_referencia)

    @property
    def carregado(self) -> bool:
        return bool(self.registros)

    def buscar_cnpj(self, cnpj: str) -> dict[str, str] | None:
        return self.registros.get(normalizar_cnpj(cnpj))

    def buscar_raiz_cnpj(self, cnpj: str) -> dict[str, str] | None:
        """Same legal entity, different establishment (matriz/filial share the 8-digit root)."""
        c = normalizar_cnpj(cnpj)
        if len(c) != 14 or c in self.registros:
            return None
        return self._por_raiz.get(c[:8])

    def buscar_nome(self, nome: str) -> dict[str, str] | None:
        alvo = normalizar_texto(nome).strip()
        if len(alvo) < 6:
            return None
        return self._por_nome.get(alvo)

    @cached_property
    def _por_raiz(self) -> dict[str, dict[str, str]]:
        idx: dict[str, dict[str, str]] = {}
        for cnpj, rec in sorted(self.registros.items()):  # matriz (0001) sorts first within a root
            idx.setdefault(cnpj[:8], rec)
        return idx

    @cached_property
    def _por_nome(self) -> dict[str, dict[str, str]]:
        idx: dict[str, dict[str, str]] = {}
        for rec in self.registros.values():
            for bruto in (rec.get("nome"), rec.get("name"), rec.get("razao_social")):
                n = normalizar_texto(bruto or "").strip()
                if n:
                    idx.setdefault(n, rec)
        return idx


# --------------------------------------------------------------------------------- agent
class AgenteIdentidade:
    def __init__(self, marca: PerfilMarca, cadastro: CadastroCoban) -> None:
        self.marca = marca
        self.cadastro = cadastro
        self._cnpjs_instituicao = {normalizar_cnpj(c) for c in marca.cnpjs_instituicao}

    def _eh_contexto_instituicao(self, contexto: str) -> bool:
        ctx = normalizar_texto(contexto)
        return any(normalizar_texto(n) in ctx for n in self.marca.nomes_instituicao)

    def _nome_mais_proximo_eh_instituicao(self, contexto: str) -> bool:
        """True when the closest company name before a CNPJ is the institution itself
        (e.g. "Banco BV S.A., CNPJ ..."), not another company named in between."""
        f = normalizar_texto(contexto)
        inst = max((f.rfind(normalizar_texto(n)) for n in self.marca.nomes_instituicao), default=-1)
        if inst < 0:
            return False
        outros = [
            m.start() for m in RAZAO_SOCIAL_RE.finditer(contexto) if not self._eh_contexto_instituicao(m.group(1))
        ]
        return inst > max(outros, default=-1)

    def _limpar_nome(self, bruto: str) -> str:
        return PREFIXO_UI_NOME.sub("", bruto.strip(" .,-–—")).strip(" .,-–—")

    def atribuir(
        self, id_candidata: str, ev: Evidencia, catalogo: list[ItemEvidencia], sinais: SinaisPagina
    ) -> AtribuicaoIdentidade:
        url = ev.url_final or ev.url_original
        host = hostname_de(url)
        dominio_reg = dominio_registravel(host)
        fatos: list[FatoIdentidade] = [FatoIdentidade(tipo="dominio", valor=dominio_reg, ref_evidencia="E-URL")]
        inferencias: list[str] = []
        limitacoes: list[str] = []

        if not ev.utilizavel and not sinais.tem_evidencia_descoberta:
            limitacoes.append("coleta sem conteúdo utilizável; identidade não pôde ser examinada")
            return AtribuicaoIdentidade(
                id_candidata=id_candidata,
                classificacao=ClasseIdentidade.IDENTIDADE_DESCONHECIDA,
                chave_entidade=f"domain:{dominio_reg}",
                nome_exibicao=None,
                fatos=fatos,
                inferencias=["sem evidência de identidade; associação limitada ao domínio"],
                confianca_atribuicao=Nivel.NENHUMA,
                limitacoes=limitacoes,
                fundamentacao="Conteúdo não coletado; nenhuma atribuição possível além do domínio.",
            )
        if not ev.utilizavel:
            limitacoes.append(
                "acesso direto pelo navegador indisponível/bloqueado; identidade examinada apenas "
                "a partir das evidências indexadas na descoberta (E-DISC-*)"
            )

        # ---- CNPJs: owner vs. referenced institution ---------------------------------
        # A CNPJ is the institution's only when listed in the brand profile, or when the nearest
        # preceding company name is the institution AND another valid CNPJ exists on the page (so
        # the owner is not lost). Otherwise proximity to the institution's name is only flagged.
        cnpjs_responsavel: list[str] = []
        refs_instituicao: list[str] = []
        invalido: list[str] = []
        encontrados: list[tuple[str, bool]] = []  # (cnpj, institution-context)
        for item in catalogo:
            fim_anterior = 0
            for m in CNPJ_RE.finditer(item.conteudo.upper()):
                bruto = m.group(1)
                if sum(ch.isdigit() for ch in bruto) < 8:
                    continue
                cnpj = formatar_cnpj(bruto)
                valido = cnpj_valido(bruto)
                # Context = text since the previous CNPJ (max 120 chars) so labels don't leak across.
                ctx = item.conteudo[max(fim_anterior, m.start() - 120) : m.end()]
                fim_anterior = m.end()
                fatos.append(
                    FatoIdentidade(tipo="cnpj", valor=cnpj, ref_evidencia=item.ref, contexto=ctx, valido=valido)
                )
                if not valido:
                    invalido.append(cnpj)
                    continue
                if normalizar_cnpj(cnpj) in self._cnpjs_instituicao:
                    if cnpj not in refs_instituicao:
                        refs_instituicao.append(cnpj)
                    continue
                ctx_inst = self._nome_mais_proximo_eh_instituicao(ctx[: len(ctx) - len(m.group(0))])
                encontrados.append((cnpj, ctx_inst and not self.cadastro.buscar_cnpj(cnpj)))
        sinalizadas: list[str] = []
        tem_simples = any(not inst for _, inst in encontrados)
        for cnpj, inst in encontrados:
            if cnpj in cnpjs_responsavel or cnpj in refs_instituicao:
                continue
            if inst and tem_simples:
                refs_instituicao.append(cnpj)
            else:
                cnpjs_responsavel.append(cnpj)
                if inst:
                    sinalizadas.append(cnpj)
        if invalido:
            inferencias.append(f"CNPJ(s) com dígito verificador inválido ignorados: {', '.join(invalido)}")
        if refs_instituicao:
            inferencias.append(
                "CNPJ(s) da instituição (perfil da marca) ou citados logo após o nome da instituição, com outro "
                f"CNPJ presente na página, tratados como referência à contratante: {', '.join(refs_instituicao)}"
            )
        if sinalizadas:
            inferencias.append(
                f"CNPJ {', '.join(sinalizadas)} aparece logo após o nome da instituição: confirmar se pertence ao "
                "titular da página ou à contratante (perfil da marca sem CNPJs da instituição validados)"
            )

        # ---- names, contacts, declarations -------------------------------------------
        # A name that is just the institution's (e.g. og:site_name "Banco BV") is a *claimed* identity
        # observed on the page, never the owner's name.
        razoes_sociais: list[str] = []
        nomes_fantasia: list[str] = []
        alegado: list[str] = []

        def adicionar_nome(tipo: str, val: str, ref: str) -> None:
            val = self._limpar_nome(val)
            if len(val) < 3:
                return
            if self.marca.eh_nome_instituicao(val) or self._eh_contexto_instituicao(val):
                if val not in alegado:
                    alegado.append(val)
                    fatos.append(FatoIdentidade(tipo="nome_instituicao_exibido", valor=val, ref_evidencia=ref))
                return
            # Do not attribute another bank's corporate name (e.g. "Banco Inter S/A", "Banco Omni S/A")
            # or a shared platform's og:site_name ("YouTube", "Instagram", "Facebook") as the third-party owner.
            valor_normalizado = normalizar_texto(val)
            if (
                self.marca.mencoes_concorrentes(val)
                or re.match(r"^banco\s+\w+", valor_normalizado)
                or valor_normalizado in ("youtube", "instagram", "facebook", "tiktok", "linkedin", "my site", "home")
            ):
                return
            bucket = razoes_sociais if tipo == "razao_social" else nomes_fantasia
            if val not in bucket:
                bucket.append(val)
                fatos.append(FatoIdentidade(tipo=tipo, valor=val, ref_evidencia=ref))

        for item in catalogo:
            if item.tipo in ("captura", "rede"):
                continue
            for m in NOME_ROTULADO_RE.finditer(item.conteudo):
                adicionar_nome("razao_social" if "raz" in m.group(1).lower() else "nome_fantasia", m.group(2), item.ref)
            if item.local in ("footer", "page") or item.tipo == "texto":
                for linha_txt in item.conteudo.splitlines():
                    for m in RAZAO_SOCIAL_RE.finditer(linha_txt):
                        adicionar_nome("razao_social", m.group(1), item.ref)
            for m in EMAIL_RE.finditer(item.conteudo):
                fatos.append(FatoIdentidade(tipo="email", valor=m.group(0), ref_evidencia=item.ref))
            for m in SOCIAL_RE.finditer(item.conteudo):
                fatos.append(FatoIdentidade(tipo="perfil_social", valor=m.group(0)[:200], ref_evidencia=item.ref))
        nome_site = ev.pagina.meta.get("og:site_name") if ev.pagina else None
        if nome_site:
            adicionar_nome(
                "nome_fantasia", nome_site, "E-META-2" if any(i.ref == "E-META-2" for i in catalogo) else "E-TITLE"
            )
        if ev.pagina and dominio_reg in ("instagram.com", "tiktok.com", "facebook.com"):
            m_ig = PERFIL_IG_TITULO_RE.search(ev.pagina.titulo or "") or PERFIL_IG_META_RE.search(
                (ev.pagina.meta.get("twitter:title") or "") + " " + (ev.pagina.meta.get("og:title") or "")
            )
            if m_ig:
                tratar = f"@{m_ig.group(1)}"
                adicionar_nome("nome_fantasia", tratar, "E-TITLE")
        if alegado:
            inferencias.append(
                "a página se apresenta com o nome da instituição ("
                + "; ".join(alegado[:3])
                + "); isso é identidade declarada/visual, não a identidade do responsável"
            )
        for h in sinais.telefones[:10]:
            fatos.append(FatoIdentidade(tipo="telefone", valor=h.valor, ref_evidencia=h.ref))
        for h in sinais.contatos_whatsapp[:5]:
            fatos.append(FatoIdentidade(tipo="whatsapp", valor=h.valor, ref_evidencia=h.ref))
        for h in sinais.declaracoes[:5]:
            fatos.append(
                FatoIdentidade(
                    tipo="declaracao_correspondente", valor=h.valor, ref_evidencia=h.ref, contexto=h.contexto
                )
            )
        for h in sinais.parceria_com_marca[:5]:
            fatos.append(
                FatoIdentidade(tipo="declaracao_parceria", valor=h.valor, ref_evidencia=h.ref, contexto=h.contexto)
            )
        fatos = _deduplicar_fatos(fatos)

        # ---- registry lookup -----------------------------------------------------------
        correspondencia_cadastro: CorrespondenciaCadastro | None = None
        for c in cnpjs_responsavel:
            rec = self.cadastro.buscar_cnpj(c)
            if rec:
                correspondencia_cadastro = CorrespondenciaCadastro(
                    fonte=self.cadastro.fonte,
                    casado_por="cnpj",
                    registro=rec,
                    oficial=self.cadastro.oficial,
                )
                break
        if correspondencia_cadastro is None:
            for c in cnpjs_responsavel:
                rec = self.cadastro.buscar_raiz_cnpj(c)
                if rec:
                    correspondencia_cadastro = CorrespondenciaCadastro(
                        fonte=self.cadastro.fonte, casado_por="cnpj_root", registro=rec, oficial=False
                    )
                    break
        if correspondencia_cadastro is None:
            for nome in razoes_sociais:
                rec = self.cadastro.buscar_nome(nome)
                if rec:
                    correspondencia_cadastro = CorrespondenciaCadastro(
                        fonte=self.cadastro.fonte, casado_por="name", registro=rec, oficial=False
                    )
                    break
        if not self.cadastro.carregado:
            limitacoes.append(
                "nenhum cadastro autorizado de correspondentes carregado; COBAN_CONFIRMADO indisponível nesta execução"
            )

        # ---- classification -----------------------------------------------------------
        correspondente_declara = bool(sinais.declaracoes)
        menciona_parceiro = bool(sinais.parceria_com_marca)  # "parceiro" of *this* brand
        identificado = bool(cnpjs_responsavel or razoes_sociais)
        if len(cnpjs_responsavel) > 1:
            inferencias.append(
                f"{len(cnpjs_responsavel)} CNPJs distintos na página: pode ser diretório, marketplace "
                "ou grupo de empresas; associação ao responsável é incerta"
            )

        if (
            correspondencia_cadastro
            and correspondencia_cadastro.casado_por == "cnpj"
            and correspondencia_cadastro.oficial
            and len(cnpjs_responsavel) > 1
        ):
            cls = ClasseIdentidade.COBAN_POSSIVEL
            conf = Nivel.BAIXA
            fundamentacao = (
                "Um dos CNPJs da página consta do cadastro autorizado, mas há outros CNPJs (diretório, "
                "marketplace ou grupo): a titularidade da página não é inequívoca."
            )
        elif (
            correspondencia_cadastro
            and correspondencia_cadastro.casado_por == "cnpj"
            and correspondencia_cadastro.oficial
        ):
            cls = ClasseIdentidade.COBAN_CONFIRMADO
            conf = Nivel.ALTA if len(cnpjs_responsavel) == 1 else Nivel.MEDIA
            fundamentacao = (
                f"CNPJ {cnpjs_responsavel[0] if cnpjs_responsavel else ''} observado na página consta do "
                f"cadastro autorizado '{correspondencia_cadastro.fonte}'."
            )
            inferencias.append("associação página→CNPJ baseada na publicação do CNPJ na própria página")
            inferencias.append(
                "um CNPJ publicado pode ter sido copiado por terceiros: confirmar junto ao correspondente que o "
                "domínio é seu"
            )
        elif correspondencia_cadastro and correspondencia_cadastro.casado_por == "cnpj_root":
            cls = ClasseIdentidade.COBAN_POSSIVEL
            conf = Nivel.MEDIA if self.cadastro.oficial else Nivel.BAIXA
            fundamentacao = (
                f"O CNPJ da página não consta do cadastro, mas outro estabelecimento da mesma empresa (raiz "
                f"{cnpjs_responsavel[0][:8] if cnpjs_responsavel else ''}, matriz/filial) consta de "
                f"'{correspondencia_cadastro.fonte}'; "
                "confirmar se o contrato de correspondente abrange este estabelecimento."
            )
        elif correspondencia_cadastro:
            cls = ClasseIdentidade.COBAN_POSSIVEL
            conf = Nivel.BAIXA
            fundamentacao = (
                "Correspondência apenas por nome (ou fonte não autoritativa) no cadastro; requer confirmação por CNPJ."
            )
        elif correspondente_declara and identificado:
            cls = ClasseIdentidade.COBAN_POSSIVEL
            conf = Nivel.BAIXA if sinalizadas else Nivel.MEDIA
            fundamentacao = (
                "A página declara atuação como correspondente/prestador e identifica a empresa, "
                "mas não houve confirmação em cadastro autorizado."
            )
        elif correspondente_declara or (menciona_parceiro and identificado):
            cls = ClasseIdentidade.COBAN_POSSIVEL
            conf = Nivel.BAIXA
            fundamentacao = (
                "Há declaração de correspondente ou de parceria, sem identificação suficiente "
                "ou sem confirmação cadastral."
            )
        elif identificado:
            cls = ClasseIdentidade.OUTRO_TERCEIRO
            conf = Nivel.MEDIA if len(cnpjs_responsavel) == 1 and not sinalizadas else Nivel.BAIXA
            fundamentacao = (
                "Empresa identificada na página sem declaração de relação de correspondente; "
                "anunciar produtos da marca não comprova vínculo."
            )
        else:
            cls = ClasseIdentidade.IDENTIDADE_DESCONHECIDA
            conf = Nivel.NENHUMA if not fatos[1:] else Nivel.BAIXA
            fundamentacao = "Nenhum CNPJ ou razão social identificáveis no conteúdo coletado."

        if cnpjs_responsavel:
            chave_entidade = f"cnpj:{normalizar_cnpj(cnpjs_responsavel[0])}"
            fundamento = "cnpj publicado na página"
        elif dominio_reg in ("instagram.com", "tiktok.com", "facebook.com") and nomes_fantasia:
            chave_entidade = f"social:{dominio_reg}:{normalizar_texto(nomes_fantasia[0]).lstrip('@')}"
            fundamento = "perfil em rede social"
        else:
            chave_entidade = f"domain:{dominio_reg}"
            fundamento = "domínio (sem CNPJ)"
            inferencias.append(
                "entidade agrupada pelo domínio registrável; um mesmo estabelecimento "
                "pode ter outros domínios não associados"
            )
        exibicao = (razoes_sociais[0] if razoes_sociais else None) or (nomes_fantasia[0] if nomes_fantasia else None)
        if correspondencia_cadastro and not exibicao:
            exibicao = correspondencia_cadastro.registro.get("nome") or correspondencia_cadastro.registro.get("name")
        inferencias.append(f"base da associação: {fundamento}")
        return AtribuicaoIdentidade(
            id_candidata=id_candidata,
            classificacao=cls,
            chave_entidade=chave_entidade,
            nome_exibicao=exibicao,
            cnpjs_responsavel=cnpjs_responsavel,
            cnpjs_instituicao_referenciados=refs_instituicao,
            fatos=fatos,
            inferencias=inferencias,
            correspondencia_cadastro=correspondencia_cadastro,
            confianca_atribuicao=conf,
            limitacoes=limitacoes,
            fundamentacao=fundamentacao,
        )


def _deduplicar_fatos(fatos: list[FatoIdentidade]) -> list[FatoIdentidade]:
    vistos: set[tuple[str, str]] = set()
    saida = []
    for f in fatos:
        chave = (f.tipo, f.valor.lower())
        if chave not in vistos:
            vistos.add(chave)
            saida.append(f)
    return saida
