from __future__ import annotations

import re
from datetime import UTC, datetime

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup, escape

from simio.relatorios.dados_logo import LOGO_BV_BRANCA_DATA_URI
from simio.relatorios.rotulos import (
    COR_MARCA,
    PALETA,
    dados_pedidos,
    descrever_analisador,
    descrever_cadastro,
    num_br,
    rotulo,
    rotulo_fonte,
    tom,
)


def data_br(valor: object, segundos: bool = False) -> str:
    """``2026-11-25T23:35:58+00:00`` -> ``25/11/2026 23:35 UTC``; texto que não é data passa como está."""
    if valor is None or valor == "":
        return ""
    momento = valor
    if isinstance(valor, str):
        try:
            momento = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return valor
    if not isinstance(momento, datetime):
        return str(valor)
    if momento.tzinfo is not None:
        momento = momento.astimezone(UTC)
    return momento.strftime("%d/%m/%Y %H:%M:%S UTC" if segundos else "%d/%m/%Y %H:%M UTC")


_HTTP = re.compile(r"^https?://", re.IGNORECASE)


def link_url(url: object) -> Markup | str:
    """URL clicável, abrindo em outra aba; o que não for http(s) fica só como texto."""
    if not url:
        return ""
    texto = escape(str(url))
    if not _HTTP.match(str(url)):
        return texto
    return Markup(f'<a href="{texto}" target="_blank" rel="noopener noreferrer nofollow">{texto}</a>')  # noqa: S704 (texto já escapado)


def agora_br() -> str:
    return data_br(datetime.now(UTC))


def logo_marca(id_marca: str | None) -> str | None:
    # Só temos a logo do BV embutida; as outras marcas ficam sem logo no cabeçalho.
    return LOGO_BV_BRANCA_DATA_URI if (id_marca or "bv").lower() == "bv" else None


ambiente = Environment(
    loader=PackageLoader("simio.relatorios", "templates"),
    autoescape=select_autoescape(["html", "j2"], default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
ambiente.filters["rotulo"] = rotulo
ambiente.filters["tom"] = tom
ambiente.filters["data_br"] = data_br
ambiente.filters["link_url"] = link_url
ambiente.filters["num_br"] = num_br
ambiente.filters["dados_pedidos"] = dados_pedidos
ambiente.filters["fonte"] = rotulo_fonte
ambiente.filters["cadastro"] = descrever_cadastro
ambiente.globals["descrever_analisador"] = descrever_analisador
ambiente.globals["paleta"] = PALETA
ambiente.globals["cor_marca"] = COR_MARCA
