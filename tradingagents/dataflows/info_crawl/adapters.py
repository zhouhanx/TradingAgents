"""Wave-1 sources (PM ruling): official APIs and press RSS, no login, no keys.

Required: SEC EDGAR, GDELT DOC 2.0, OKX announcements, Fed / ECB / US Treasury press.
Optional: Coinbase status page, PR Newswire release RSS (personal-use terms).
"""

from __future__ import annotations

from tradingagents.dataflows.info_crawl.base import (
    TTL_ANNOUNCEMENT,
    TTL_FILING,
    TTL_NEWS,
    InfoItem,
    InfoQuery,
    SourceAdapter,
    SourceUnavailable,
    iso_utc,
    matches,
)
from tradingagents.dataflows.info_crawl.rss import parse_feed

_FILING_FORMS = {"10-K", "10-Q", "8-K", "6-K", "20-F", "40-F", "S-1", "DEF 14A", "SC 13D", "SC 13G", "4"}


class SecEdgarFilings(SourceAdapter):
    id = "sec_edgar"
    name_zh = "SEC EDGAR 公告"
    kind = "filing"
    assets = ("stock",)
    terms_url = "https://www.sec.gov/os/accessing-edgar-data"
    description = "美国证监会官方披露（10-K/10-Q/8-K 等，固定看近 90 天）。公开数据，按 SEC 要求每秒不超过 10 次并标明联系方式。"
    min_interval = 0.2
    ttl = TTL_FILING
    min_window_hours = 90 * 24

    def fetch(self, query: InfoQuery) -> list[InfoItem]:
        from tradingagents.dataflows.errors import VendorUnavailableError
        from tradingagents.dataflows.vendors.sec_edgar import _user_agent, cik_for
        symbol = query.keyword.strip().split()[0].upper() if query.keyword.strip() else ""
        if not symbol:
            return []
        try:
            cik = cik_for(symbol)
        except VendorUnavailableError as exc:
            raise SourceUnavailable("SEC 代码表暂时读取失败") from exc
        if not cik:
            return []
        data = self.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers={"User-Agent": _user_agent()}).json()
        recent = (data.get("filings") or {}).get("recent") or {}
        name = data.get("name") or symbol
        out = []
        for index, form in enumerate(recent.get("form") or []):
            if form not in _FILING_FORMS:
                continue
            accession = recent["accessionNumber"][index]
            document = (recent.get("primaryDocument") or [""] * (index + 1))[index]
            out.append(InfoItem(
                source=self.id, kind=self.kind,
                title=f"{name} {form}" + (f"：{recent['primaryDocDescription'][index]}" if recent.get("primaryDocDescription") and recent["primaryDocDescription"][index] else ""),
                url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}",
                published_at=iso_utc(recent.get("acceptanceDateTime", [None] * (index + 1))[index] or recent["filingDate"][index]),
                summary=f"{form} 申报，提交日 {recent['filingDate'][index]}。",
            ))
            if len(out) >= 40:
                break
        return out


class GdeltDoc(SourceAdapter):
    id = "gdelt"
    name_zh = "GDELT 全球新闻索引"
    kind = "news"
    assets = ("stock", "crypto", "macro")
    terms_url = "https://www.gdeltproject.org/about.html#termsofuse"
    description = "GDELT DOC 2.0 开放新闻索引，只取标题与链接。官方要求每 5 秒不超过 1 次请求。"
    min_interval = 5.5
    retry_wait = 6.0
    ttl = TTL_NEWS

    def fetch(self, query: InfoQuery) -> list[InfoItem]:
        keyword = query.keyword.strip()
        if not keyword:
            return []
        hours = max(1, min(int(query.hours), 24 * 7))
        response = self.get("https://api.gdeltproject.org/api/v2/doc/doc", params={
            "query": keyword if " " not in keyword else f'"{keyword}"', "mode": "artlist", "format": "json",
            "maxrecords": 50, "timespan": f"{hours}h", "sort": "datedesc",
        })
        text = response.text.strip()
        if not text.startswith("{"):
            raise SourceUnavailable("GDELT 返回了非 JSON 内容（可能是限流提示）")
        return [InfoItem(source=self.id, kind=self.kind, title=row.get("title") or "", url=row.get("url") or "",
                         published_at=iso_utc(row.get("seendate")), summary=row.get("domain") or "")
                for row in response.json().get("articles") or [] if row.get("url")]


class OkxAnnouncements(SourceAdapter):
    id = "okx_announcements"
    name_zh = "OKX 官方公告"
    kind = "announcement"
    assets = ("crypto",)
    terms_url = "https://www.okx.com/docs-v5/en/#announcement"
    description = "OKX 公开 API 的官方公告（上新、下架、维护等），无需账户。"
    min_interval = 1.0
    ttl = TTL_ANNOUNCEMENT

    def fetch(self, query: InfoQuery) -> list[InfoItem]:
        payload = self.get("https://www.okx.com/api/v5/support/announcements").json()
        if str(payload.get("code")) != "0":
            raise SourceUnavailable("OKX 公告接口返回错误")
        base = query.keyword.strip().upper().replace("-USDT", "").replace("-USD", "")
        out = []
        for block in payload.get("data") or []:
            for row in block.get("details") or []:
                title = row.get("title") or ""
                if base and not matches(title, base):
                    continue
                out.append(InfoItem(source=self.id, kind=self.kind, title=title, url=row.get("url") or "",
                                    published_at=iso_utc(row.get("pTime")), summary=row.get("annType") or ""))
        return out


class _PressFeed(SourceAdapter):
    feed_url = ""
    kind = "press"
    assets = ("macro", "stock", "crypto")
    min_interval = 2.0
    ttl = TTL_ANNOUNCEMENT
    keyword_filter = False

    def fetch(self, query: InfoQuery) -> list[InfoItem]:
        items = parse_feed(self.get(self.feed_url).content, self.id, self.kind)
        if self.keyword_filter and query.keyword.strip():
            items = [item for item in items if matches(f"{item.title} {item.summary}", query.keyword)]
        return items


class FedPress(_PressFeed):
    id = "fed_press"
    name_zh = "美联储新闻稿"
    feed_url = "https://www.federalreserve.gov/feeds/press_all.xml"
    terms_url = "https://www.federalreserve.gov/disclaimer.htm"
    description = "美联储理事会官方新闻稿 RSS（议息、监管、讲话）。"


class EcbPress(_PressFeed):
    id = "ecb_press"
    name_zh = "欧洲央行新闻稿"
    feed_url = "https://www.ecb.europa.eu/rss/press.html"
    terms_url = "https://www.ecb.europa.eu/services/disclaimer/html/index.en.html"
    description = "欧洲央行官方新闻稿 RSS。"


class TreasuryPress(_PressFeed):
    id = "treasury_press"
    name_zh = "美国财政部新闻稿"
    feed_url = "https://home.treasury.gov/rss/press-releases"
    terms_url = "https://home.treasury.gov/subfooter/site-policies-and-notices"
    description = "美国财政部官方新闻稿 RSS（发债、制裁、讲话）。"
    min_interval = 3.0


class CoinbaseStatus(_PressFeed):
    id = "coinbase_status"
    name_zh = "Coinbase 服务状态"
    kind = "announcement"
    assets = ("crypto",)
    feed_url = "https://status.coinbase.com/history.rss"
    terms_url = "https://www.coinbase.com/legal/user_agreement"
    description = "Coinbase 官方状态页 RSS（故障、维护、充提暂停）。可选来源，仅供个人研究使用。"
    optional = True


class PrNewswire(_PressFeed):
    id = "prnewswire"
    name_zh = "PR Newswire 公司新闻稿"
    kind = "press"
    assets = ("stock", "crypto")
    feed_url = "https://www.prnewswire.com/rss/news-releases-list.rss"
    terms_url = "https://www.prnewswire.com/terms-of-use/"
    description = "公司发布的新闻稿 RSS，按关键词筛选。可选来源；条款限个人非商业使用，请勿转载。"
    optional = True
    personal_use = True
    keyword_filter = True
    ttl = TTL_NEWS
