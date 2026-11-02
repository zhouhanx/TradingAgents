"""All information sources, by id."""

from __future__ import annotations

from tradingagents.dataflows.info_crawl.adapters import (
    CoinbaseStatus,
    EcbPress,
    FedPress,
    GdeltDoc,
    OkxAnnouncements,
    PrNewswire,
    SecEdgarFilings,
    TreasuryPress,
)
from tradingagents.dataflows.info_crawl.base import SourceAdapter

SOURCES: dict[str, SourceAdapter] = {adapter.id: adapter for adapter in (
    SecEdgarFilings(), GdeltDoc(), OkxAnnouncements(), FedPress(), EcbPress(), TreasuryPress(),
    CoinbaseStatus(), PrNewswire(),
)}

ASSET_TYPES = ("stock", "crypto", "macro")

# PM ruling 6: the sources analysis already reads keep their volume; only a short
# gray-risk note is shown with them. They are not part of 信息抓取.
EXISTING_SOURCE_NOTES = (
    {"id": "yahoo", "name": "Yahoo Finance 新闻（分析内置）", "note": "非官方接口，条款灰区，可能随时被限流或停用；仅个人研究。"},
    {"id": "reddit", "name": "Reddit RSS（分析内置）", "note": "公开 RSS，条款灰区，频率受限；内容为网友观点，仅作情绪参考。"},
    {"id": "stocktwits", "name": "StockTwits（分析内置）", "note": "公开接口，条款灰区，可能限流；内容为网友观点，仅作情绪参考。"},
)


def sources_for(asset_type: str | None) -> list[SourceAdapter]:
    if not asset_type:
        return list(SOURCES.values())
    return [adapter for adapter in SOURCES.values() if asset_type in adapter.assets]
