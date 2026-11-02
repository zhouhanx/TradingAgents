"""Minimal RSS 2.0 / Atom reader for the official feeds."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from tradingagents.dataflows.info_crawl.base import InfoItem, SourceUnavailable, iso_utc

_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_feed(text, source: str, kind: str) -> list[InfoItem]:
    raw = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    head = raw[:2000].decode("utf-8", "ignore").lower()
    if "<!doctype html" in head and "<rss" not in head:
        raise SourceUnavailable("来源返回的不是 RSS 内容")
    try:
        root = ET.fromstring(raw.lstrip(b"\xef\xbb\xbf \r\n\t"))
    except ET.ParseError as exc:
        raise SourceUnavailable("RSS 内容无法解析") from exc
    items = []
    for node in root.iter("item"):
        items.append(InfoItem(
            source=source, kind=kind,
            title=node.findtext("title") or "",
            url=node.findtext("link") or "",
            published_at=iso_utc(node.findtext("pubDate") or node.findtext("{http://purl.org/dc/elements/1.1/}date")),
            summary=node.findtext("description") or "",
        ))
    for node in root.iter(f"{_ATOM}entry"):
        link = node.find(f"{_ATOM}link")
        items.append(InfoItem(
            source=source, kind=kind,
            title=node.findtext(f"{_ATOM}title") or "",
            url=link.get("href", "") if link is not None else "",
            published_at=iso_utc(node.findtext(f"{_ATOM}updated") or node.findtext(f"{_ATOM}published")),
            summary=node.findtext(f"{_ATOM}summary") or "",
        ))
    return [item for item in items if item.title and item.url]
