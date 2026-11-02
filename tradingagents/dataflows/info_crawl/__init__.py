"""V2-INFO-CRAWL: public, official information sources gathered on request.

Only official APIs and RSS feeds that need no login are used (see DESIGN). Each
source is one :class:`SourceAdapter`; a failing source is reported 「不可用」,
never as 「无数据」. Items are stored redacted, for 30 days by default.
"""

from tradingagents.dataflows.info_crawl.base import (
    InfoItem,
    InfoQuery,
    SourceAdapter,
    SourceUnavailable,
)
from tradingagents.dataflows.info_crawl.jobs import format_items_for_prompt, get_info_items, run_job
from tradingagents.dataflows.info_crawl.registry import EXISTING_SOURCE_NOTES, SOURCES, sources_for
from tradingagents.dataflows.info_crawl.store import JsonlItemStore, MemoryItemStore

__all__ = [
    "EXISTING_SOURCE_NOTES", "InfoItem", "InfoQuery", "JsonlItemStore", "MemoryItemStore", "SOURCES",
    "SourceAdapter", "SourceUnavailable", "format_items_for_prompt", "get_info_items", "run_job", "sources_for",
]
