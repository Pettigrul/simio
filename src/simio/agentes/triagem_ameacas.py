from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from simio.agentes.evidencia import TIPOS_CREDENCIAL, descrever_dados_sensiveis
from simio.agentes.inteligencia_dominio import plataforma_hospedagem
from simio.marcas import PerfilMarca
from simio.nucleo.modelos import CasoCenario, ClasseIdentidade, ClassificacaoFinal, InteligenciaDominio
from simio.nucleo.urls import dominio_corresponde, hostname_de
from simio.provedores.typosquat import descrever_semelhanca

if TYPE_CHECKING:  # relatorios importa este módulo (evita import circular)
    from simio.relatorios.dados import RegistroCandidata

NIVEIS = ("CRÍTICO", "ALTO", "MÉDIO", "BAIXO", "SEM AMEAÇA")
_HOSTS_APK = ("aptoide.com", "apkpure.com", "apkcombo.com", "uptodown.com", "softonic.com", "apkmirror.com",
              "appbrain.com", "apk.cafe", "apkmody.io", "happymod.com", "apksfull.com")
_LOJAS_OFICIAIS = ("play.google.com", "apps.apple.com")
_SOCIAIS = ("instagram.com", "facebook.com", "tiktok.com", "youtube.com", "x.com", "twitter.com", "kwai.com",
           "threads.net", "linkedin.com")
_MENSAGEIROS = ("t.me", "telegram.me", "wa.me", "whatsapp.com", "chat.whatsapp.com")
_IDENTIDADES_LEGITIMAS = (
    ClasseIdentidade.COBAN_CONFIRMADO,
    ClasseIdentidade.COBAN_POSSIVEL,
    ClasseIdentidade.OUTRO_TERCEIRO,
)
DIAS_DOMINIO_NOVO = 180


@dataclass
class VisaoAmeaca:
    pontuacao: float
    nivel: str
    categorias: list[str] = field(default_factory=list)
    fatos: list[str] = field(default_factory=list)

    @property
    def eh_ameaca(self) -> bool:
        return self.nivel != "SEM AMEAÇA"


def _nivel(pontuacao: float) -> str:
    if pontuacao >= 8:
        return "CRÍTICO"
    if pontuacao >= 5:
        return "ALTO"
    if pontuacao >= 3:
        return "MÉDIO"
    if pontuacao >= 2:
        return "BAIXO"
    return "SEM AMEAÇA"


def triar(rec: RegistroCandidata, marca: PerfilMarca, intel: InteligenciaDominio | None = None) -> VisaoAmeaca:
    """Nível de ameaça de uma página já analisada, sem busca, coleta nem chamada ao modelo.

    Cada ponto vem de um fato conferível. Empresa identificada falando em nome próprio perde pontos:
    é tema de conformidade, não ameaça."""
    f = rec.final
    if f is None:
        return VisaoAmeaca(0, "SEM AMEAÇA", fatos=["não analisado"])
    ev = rec.evidencia
    url = (ev.url_final if ev else None) or rec.candidata.url
    host = hostname_de(url) or rec.candidata.hostname
    pagina = ev.pagina if ev and ev.pagina else None
    texto = " ".join(
        filter(None, [pagina.titulo if pagina else "", (pagina.texto_visivel if pagina else "")[:20000]]
               + [f"{h.titulo or ''} {h.trecho or ''}" for h in rec.resultados])
    )
    hips = {h.id_hipotese or "" for h in rec.resultados}
    pontuacao, cats, fatos = 0.0, [], []

    def adicionar(pontos: float, fato: str, categoria: str | None = None) -> None:
        nonlocal pontuacao
        pontuacao += pontos
        fatos.append(fato)
        if categoria and categoria not in cats:
            cats.append(categoria)

    host_titulo = f"{pagina.titulo if pagina else ''} {host} " + " ".join(h.titulo or "" for h in rec.resultados)
    apresenta_marca = bool(marca.mencoes_fortes(host_titulo) or marca.correspondencia_marca_dominio(host))
    descartado = f.classificacao == ClassificacaoFinal.SEM_INDICIO  # veredito final, depois da revisão
    creds = [k for k in f.dados_sensiveis_solicitados if k in TIPOS_CREDENCIAL]
    if creds:
        adicionar(
            5,
            f"pede {descrever_dados_sensiveis(creds)} numa página que se apresenta com a marca",
            "Página que pede senha (phishing)" if "senha" in creds else "Página que pede dados bancários (phishing)",
        )
    caso = f.caso
    if descartado:
        pass  # o revisor descartou (homônimo, diretório, canal do próprio banco...): o caso não conta
    elif caso == CasoCenario.D_PERSONIFICACAO_FORTE:
        adicionar(4, "o analisador registrou personificação da instituição", "Personificação da marca")
    elif caso == CasoCenario.C_POSSIVEL_CANAL_OFICIAL:
        adicionar(3, "o analisador registrou que a página se passa por canal oficial", "Falso canal oficial")
    oficial = marca.eh_oficial(host)
    semelhante = next((h for h in rec.resultados if h.fonte == "typosquat"), None)
    if semelhante is not None:
        adicionar(
            2,
            "domínio "
            + descrever_semelhanca(semelhante.metadados.get("semelhante_a"), semelhante.metadados.get("fuzzer")),
            "Domínio parecido com o oficial",
        )
    elif (
        not oficial
        and marca.correspondencia_marca_dominio(host)
        and not dominio_corresponde(host, _SOCIAIS + _MENSAGEIROS)
    ):
        adicionar(2, f"a marca está no nome do domínio ({host})", "Domínio com a marca")
    if (
        (semelhante is not None or marca.correspondencia_marca_dominio(host))
        and not descartado
        and marca.mencoes(texto)
    ):
        adicionar(1, "o domínio com a marca tem conteúdo que cita a instituição")
    # TODO: "apk" in host também pega nomes como napkin.com; checar só rótulos do host (apk., -apk).
    eh_apk = dominio_corresponde(host, _HOSTS_APK) or url.lower().split("?")[0].endswith(".apk") or "apk" in host
    if eh_apk and apresenta_marca and not descartado:
        adicionar(3, "distribui aplicativo (APK) com a marca fora das lojas oficiais", "App falso / APK")
    plataforma = plataforma_hospedagem(host)
    if plataforma and not eh_apk and (apresenta_marca or caso in (
        CasoCenario.C_POSSIVEL_CANAL_OFICIAL, CasoCenario.D_PERSONIFICACAO_FORTE
    )) and not descartado:
        adicionar(2, f"página com a marca em plataforma gratuita/de terceiros ({plataforma})", "Hospedagem gratuita")
    if intel and intel.rdap and intel.rdap.idade_dias is not None and intel.rdap.idade_dias <= DIAS_DOMINIO_NOVO:
        adicionar(2, f"domínio registrado há {intel.rdap.idade_dias} dias", "Domínio recente")
    servicos = marca.mencoes_servicos_sensiveis(texto) if apresenta_marca and not descartado else []
    if servicos:
        adicionar(
            2, f"oferece {', '.join(sorted(set(servicos))[:3])} citando a marca", "Boleto / quitação / atendimento"
        )
    if dominio_corresponde(host, _MENSAGEIROS):
        adicionar(1, "contato direto por mensageiro (WhatsApp/Telegram)", "Canal de mensagens")
        if "H14-ameaca:paineis-dados" in hips or "H6-plataforma:Telegram-grupos" in hips:
            cats.append("Painel / venda de dados")
    if dominio_corresponde(host, _SOCIAIS) and caso in (CasoCenario.C_POSSIVEL_CANAL_OFICIAL,
                                                  CasoCenario.D_PERSONIFICACAO_FORTE):
        cats.append("Perfil/post falso em rede social")
    if any(h.startswith("H10-fraude") for h in hips) and caso in (
        CasoCenario.C_POSSIVEL_CANAL_OFICIAL, CasoCenario.D_PERSONIFICACAO_FORTE
    ):
        cats.append("Golpe com a marca como isca")

    if f.classificacao == ClassificacaoFinal.INDICIO_FORTE:
        adicionar(2, "indício forte na análise SIMIO")
    elif f.classificacao == ClassificacaoFinal.REQUER_REVISAO and pontuacao > 0:
        adicionar(1, "marcado para revisão na análise SIMIO")

    idn = f.classe_identidade
    if idn in _IDENTIDADES_LEGITIMAS and caso in (
        CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA, CasoCenario.B_COMUNICACAO_AMBIGUA
    ) and not creds:
        adicionar(-4, "empresa identificada que se comunica com a própria marca (tema de conformidade, não ameaça)")
    if caso == CasoCenario.NAO_RELEVANTE:
        adicionar(-3, "o analisador considerou a página sem relação com a instituição (ex.: homônimo)")
        pontuacao = min(pontuacao, 3.0)
    elif caso == CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA:
        adicionar(-2, "menção comercial legítima")
    if oficial or dominio_corresponde(host, _LOJAS_OFICIAIS):
        pontuacao = min(pontuacao, 0.0)
        fatos.append("domínio/loja oficial")
    if dominio_corresponde(host, marca.dominios_homonimos):
        pontuacao = min(pontuacao, 0.0)
        fatos.append("homônimo conhecido (outra organização com a mesma sigla)")
    titular = intel.rdap if intel and intel.rdap and not intel.plataforma else None
    if titular and (
        (titular.documento_titular and titular.documento_titular in marca.cnpjs_instituicao)
        or (titular.titular and marca.eh_nome_instituicao(titular.titular))
    ):
        pontuacao = min(pontuacao, 0.0)
        fatos.append(f"domínio registrado pelo próprio grupo ({titular.titular}): registro defensivo")
    if intel and not (intel.a or intel.aaaa or intel.cname) and pontuacao > 0:
        fatos.append("fora do ar hoje (não resolve)")
    return VisaoAmeaca(round(pontuacao, 1), _nivel(pontuacao), cats, fatos)
