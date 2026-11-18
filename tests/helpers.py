"""Builders for synthetic evidence (no browser needed)."""

from __future__ import annotations

from simio.brands import load_brand
from simio.core.models import CollectionStatus, Evidence, ImageInfo, LinkInfo, PageData

BRAND = load_brand("bv")


def make_evidence(
    *,
    url: str = "https://loja.example/",
    title: str = "",
    header: str = "",
    above_fold: str = "",
    body: str = "",
    footer: str = "",
    images: list[tuple[str, bool]] | None = None,
    contacts: list[tuple[str, str]] | None = None,
    status: CollectionStatus = CollectionStatus.OK,
    meta: dict[str, str] | None = None,
    headings: list[str] | None = None,
    candidate_id: str = "Ctest",
) -> Evidence:
    text = "\n".join(x for x in (header, above_fold, body, footer) if x)
    page = PageData(
        title=title,
        meta=meta or {},
        headings=headings or [],
        header_text=header,
        footer_text=footer,
        above_fold_text=" ".join(x for x in (header, above_fold) if x),
        visible_text=text,
        images=[
            ImageInfo(alt=a, src=f"/img/{i}.png", above_fold=af, width=100, height=40)
            for i, (a, af) in enumerate(images or [])
        ],
        contact_links=[LinkInfo(text=t, href=h) for t, h in (contacts or [])],
    )
    usable = status in (CollectionStatus.OK, CollectionStatus.PARTIAL)
    return Evidence(
        candidate_id=candidate_id,
        attempt=1,
        status=status,
        original_url=url,
        final_url=url if usable else None,
        http_status=200 if usable else None,
        page=page if usable else None,
        errors=[] if usable else ["navigation error: net::ERR_NAME_NOT_RESOLVED"],
    )
