from __future__ import annotations

from simio.marcas import carregar_marca
from simio.nucleo.modelos import DadosPagina, Evidencia, InfoImagem, InfoLink, StatusColeta

MARCA = carregar_marca("bv")


def montar_evidencia(
    *,
    url: str = "https://loja.example/",
    titulo: str = "",
    cabecalho: str = "",
    acima_dobra: str = "",
    corpo: str = "",
    rodape: str = "",
    imagens: list[tuple[str, bool]] | None = None,
    contatos: list[tuple[str, str]] | None = None,
    status: StatusColeta = StatusColeta.OK,
    meta: dict[str, str] | None = None,
    titulos_secao: list[str] | None = None,
    id_candidata: str = "Ctest",
) -> Evidencia:
    texto = "\n".join(x for x in (cabecalho, acima_dobra, corpo, rodape) if x)
    pagina = DadosPagina(
        titulo=titulo,
        meta=meta or {},
        titulos_secao=titulos_secao or [],
        texto_cabecalho=cabecalho,
        texto_rodape=rodape,
        texto_acima_dobra=" ".join(x for x in (cabecalho, acima_dobra) if x),
        texto_visivel=texto,
        imagens=[
            InfoImagem(alt=a, src=f"/img/{i}.png", acima_dobra=af, largura=100, altura=40)
            for i, (a, af) in enumerate(imagens or [])
        ],
        links_contato=[InfoLink(texto=t, href=h) for t, h in (contatos or [])],
    )
    utilizavel = status in (StatusColeta.OK, StatusColeta.PARCIAL)
    return Evidencia(
        id_candidata=id_candidata,
        tentativa=1,
        status=status,
        url_original=url,
        url_final=url if utilizavel else None,
        status_http=200 if utilizavel else None,
        pagina=pagina if utilizavel else None,
        erros=[] if utilizavel else ["navigation error: net::ERR_NAME_NOT_RESOLVED"],
    )
