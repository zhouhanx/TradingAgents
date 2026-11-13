"""V2-INFO-CRAWL: sources, fetch policy, store, API and UI wiring (AC-1..AC-15)."""

import http.client
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import requests

from tradingagents.dataflows.info_crawl import base, jobs, registry, store
from tradingagents.dataflows.info_crawl.adapters import (
    FedPress,
    GdeltDoc,
    OkxAnnouncements,
    SecEdgarFilings,
)
from tradingagents.dataflows.info_crawl.base import (
    InfoItem,
    InfoQuery,
    RateLimiter,
    SourceAdapter,
    SourceUnavailable,
    TTLCache,
)
from tradingagents.dataflows.info_crawl.rss import parse_feed
from webui import server  # Load before the shared signed-in fixture inspects sys.modules.

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "tradingagents" / "dataflows" / "info_crawl"


class FakeResponse:
    def __init__(self, status=200, body="", headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode("utf-8")

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _limiter():
    clock = [1000.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds
    return RateLimiter(clock=lambda: clock[0], sleep=sleep), slept, clock


# AC-1 ---------------------------------------------------------------------
def test_no_mediacrawler_anywhere_in_product_tree():
    out = subprocess.run(
        ["git", "grep", "-I", "-i", "-l", "-E", "mediacrawler|relakkes|NON-COMMERCIAL LEARNING", "--",
         ".", ":!tests/test_v2_info_crawl.py", ":!docs"],
        cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == ""
    for name in ("pyproject.toml", "uv.lock", "requirements.txt"):
        path = ROOT / name
        if path.exists():
            assert "mediacrawler" not in path.read_text(encoding="utf-8").lower()


# AC-3 ---------------------------------------------------------------------
BANNED = re.compile(r"playwright|selenium|cookie|set-cookie|proxy|captcha|x-s-common|signature|xiaohongshu|douyin|kuaishou|bilibili|weibo|tieba|zhihu|finnhub|polygon|newsapi|benzinga|hkexnews|cninfo", re.I)


def test_adapter_sources_carry_no_banned_keywords():
    for path in PKG.glob("*.py"):
        hits = BANNED.findall(path.read_text(encoding="utf-8"))
        assert hits == [], (path.name, hits)
    for path in (ROOT / "webui" / "info_crawl.py",):
        assert BANNED.findall(path.read_text(encoding="utf-8")) == []


# AC-2 ---------------------------------------------------------------------
def test_registry_metadata_complete_and_wave1_present():
    for sid in ("sec_edgar", "gdelt", "okx_announcements", "fed_press", "ecb_press", "treasury_press"):
        assert sid in registry.SOURCES and not registry.SOURCES[sid].optional
    for sid in ("coinbase_status", "prnewswire"):
        assert registry.SOURCES[sid].optional
    for sid, adapter in registry.SOURCES.items():
        assert isinstance(adapter, SourceAdapter) and adapter.id == sid
        assert adapter.name_zh and adapter.kind and adapter.terms_url.startswith("https://")
        assert adapter.min_interval > 0 and adapter.ttl > 0 and adapter.assets
    assert registry.SOURCES["gdelt"].min_interval >= 5
    assert registry.SOURCES["okx_announcements"].ttl == 15 * 60
    assert registry.SOURCES["gdelt"].ttl == 10 * 60
    assert registry.SOURCES["sec_edgar"].ttl == 6 * 3600
    assert {row["id"] for row in registry.EXISTING_SOURCE_NOTES} == {"yahoo", "reddit", "stocktwits"}
    assert all("灰区" in row["note"] for row in registry.EXISTING_SOURCE_NOTES)
    assert "个人" in registry.SOURCES["prnewswire"].description and registry.SOURCES["prnewswire"].public()["personal_use"] is True


def test_sources_filter_by_asset():
    assert "okx_announcements" not in {a.id for a in registry.sources_for("stock")}
    assert "sec_edgar" not in {a.id for a in registry.sources_for("crypto")}
    assert {a.id for a in registry.sources_for("macro")} >= {"fed_press", "ecb_press", "treasury_press", "gdelt"}


# AC-5 / retry policy ------------------------------------------------------
def test_429_honours_retry_after_and_retries_at_most_twice():
    limiter, slept, _ = _limiter()
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "7"})] * 3)
    with pytest.raises(SourceUnavailable) as err:
        base.http_get("x", "https://a", min_interval=0, session=session, limiter=limiter)
    assert len(session.calls) == 3 and slept.count(7.0) == 2
    assert "429" in str(err.value)


def test_429_then_success():
    limiter, slept, _ = _limiter()
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(200, "ok")])
    assert base.http_get("x", "https://a", min_interval=0, session=session, limiter=limiter).text == "ok"
    assert slept == [3.0]


def test_4xx_not_retried_and_5xx_network_retried():
    limiter, _, _ = _limiter()
    session = FakeSession([FakeResponse(404)])
    with pytest.raises(SourceUnavailable):
        base.http_get("x", "https://a", min_interval=0, session=session, limiter=limiter)
    assert len(session.calls) == 1
    session = FakeSession([FakeResponse(503), requests.ConnectionError("down"), FakeResponse(200, "ok")])
    assert base.http_get("x", "https://a", min_interval=0, session=session, limiter=limiter).text == "ok"
    assert len(session.calls) == 3


# AC-7 ---------------------------------------------------------------------
def test_rate_limit_spacing_per_source():
    limiter, slept, clock = _limiter()
    session = FakeSession([FakeResponse(200, "a"), FakeResponse(200, "b"), FakeResponse(200, "c")])
    base.http_get("gd", "https://a", min_interval=5, session=session, limiter=limiter)
    clock[0] += 1
    base.http_get("gd", "https://a", min_interval=5, session=session, limiter=limiter)
    assert slept == [4.0]
    base.http_get("other", "https://a", min_interval=5, session=session, limiter=limiter)
    assert slept == [4.0]  # another source is not held back


# AC-6 ---------------------------------------------------------------------
class Counting(SourceAdapter):
    id = "counting"
    name_zh = "计数"
    assets = ("stock",)
    terms_url = "https://example.org"
    ttl = 600

    def __init__(self, fail=None, items=None):
        self.calls = 0
        self.fail = fail
        self.items = items

    def fetch(self, query):
        self.calls += 1
        if self.fail:
            raise self.fail
        return list(self.items) if self.items is not None else [
            InfoItem(source=self.id, title="t", url="https://e.org/1", published_at=base.iso_utc(base.utc_now()))]


def test_cache_ttl_with_injected_clock():
    clock = [0.0]
    cache = TTLCache(clock=lambda: clock[0])
    adapter = Counting()
    query = InfoQuery("NVDA", "stock", 72)
    adapter.collect(query, cache=cache)
    adapter.collect(query, cache=cache)
    assert adapter.calls == 1
    clock[0] += 601
    adapter.collect(query, cache=cache)
    assert adapter.calls == 2
    adapter.collect(InfoQuery("AAPL", "stock", 72), cache=cache)
    assert adapter.calls == 3


# AC-4 ---------------------------------------------------------------------
def test_one_failing_source_is_unavailable_not_empty():
    good, bad, parse = Counting(), Counting(fail=SourceUnavailable("来源服务暂时不可用（HTTP 503）")), Counting(fail=ValueError("x"))
    bad.id, parse.id = "bad", "parse"
    sources = {"counting": good, "bad": bad, "parse": parse}
    job = {"id": "ic-test-0001", "keyword": "NVDA", "asset_type": "stock", "hours": 72, "status": "running",
           "sources": {sid: {"status": "pending"} for sid in sources}}
    mem = store.MemoryItemStore()
    out = jobs.run_job(job, mem, sources=sources, cache=TTLCache())
    assert out["status"] == "partial"
    assert out["sources"]["counting"] == {"status": "ok", "count": 1, "reason": ""}
    assert out["sources"]["bad"]["status"] == "unavailable" and "503" in out["sources"]["bad"]["reason"]
    assert out["sources"]["parse"]["status"] == "unavailable" and out["sources"]["parse"]["reason"] == "来源返回的内容无法解析"
    assert "无数据" not in json.dumps(out, ensure_ascii=False)
    assert len(mem.load_items("ic-test-0001")) == 1


# AC-13 --------------------------------------------------------------------
def test_missing_key_source_is_gray_with_chinese_reason(monkeypatch):
    class Keyed(Counting):
        id = "keyed"
        requires_env = ("DEMO_INFO_KEY",)
    monkeypatch.delenv("DEMO_INFO_KEY", raising=False)
    row = Keyed().public()
    assert row["available"] is False and "未配置 DEMO_INFO_KEY" in row["reason"]
    keyed = Keyed()
    job = {"id": "ic-test-0002", "keyword": "NVDA", "asset_type": "stock", "hours": 72, "sources": {"keyed": {}}}
    out = jobs.run_job(job, store.MemoryItemStore(), sources={"keyed": keyed}, cache=TTLCache())
    assert keyed.calls == 0 and out["sources"]["keyed"]["status"] == "unavailable" and out["status"] == "failed"


# AC-8 / AC-9 / AC-12 ------------------------------------------------------
def test_dedupe_normalise_and_redact():
    long = "x" * 900
    a = InfoItem(source="s", title="<b>Hello</b> sk-abcdefghijklmnopqrstuvwxyz123456", url="https://e.org/a/", published_at="Mon, 02 Nov 2026 10:00:00 -0500", summary=long)
    b = InfoItem(source="s", title="dupe", url="https://E.org/a", published_at="2026-11-02T15:00:00Z")
    assert a.published_at == "2026-11-02T15:00:00Z"
    assert len(a.summary) <= 500 and a.title.startswith("Hello") and "sk-abcdefghij" not in a.title
    assert len(store.dedupe([a, b])) == 1
    assert "author" not in a.to_dict()
    c = InfoItem(source="s", title="t", url="https://e.org/x?api_key=ghp_abcdefghijklmnopqrstuvwxyz0123456789", published_at="1730563200000")
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in c.url
    assert c.published_at.endswith("Z") and InfoItem(source="s", title="t", url="u2", published_at="20261102T120000Z").published_at == "2026-11-02T12:00:00Z"


def test_rss_and_atom_parse():
    rss = '\ufeff<?xml version="1.0"?><rss><channel><item><title>Fed statement</title><link>https://www.federalreserve.gov/a</link><pubDate>Wed, 29 Oct 2026 18:00:00 GMT</pubDate><description>&lt;p&gt;Rates&lt;/p&gt;</description><author>someone@example.com (Person)</author></item></channel></rss>'
    items = parse_feed(rss.encode("utf-8"), "fed_press", "press")
    assert items[0].title == "Fed statement" and items[0].summary == "Rates" and items[0].published_at == "2026-10-29T18:00:00Z"
    assert "someone" not in json.dumps(items[0].to_dict())
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="https://e.org/a"/><updated>2026-11-01T00:00:00Z</updated></entry></feed>'
    assert parse_feed(atom, "x", "press")[0].url == "https://e.org/a"
    with pytest.raises(SourceUnavailable):
        parse_feed("<!DOCTYPE html><html>Just a moment</html>", "x", "press")


def test_adapters_parse_official_payloads(monkeypatch):
    okx = {"code": "0", "data": [{"details": [
        {"title": "OKX to list BTC options", "url": "https://www.okx.com/help/1", "pTime": "1730563200000", "annType": "listing"},
        {"title": "ETH maintenance", "url": "https://www.okx.com/help/2", "pTime": "1730563200000", "annType": "x"}]}]}
    adapter = OkxAnnouncements()
    monkeypatch.setattr(adapter, "get", lambda url, **kw: FakeResponse(200, okx))
    assert [i.url for i in adapter.fetch(InfoQuery("BTC-USDT", "crypto"))] == ["https://www.okx.com/help/1"]
    gd = GdeltDoc()
    monkeypatch.setattr(gd, "get", lambda url, **kw: FakeResponse(200, {"articles": [{"url": "https://n.org/a", "title": "NVIDIA up", "seendate": "20261102T101500Z", "domain": "n.org"}]}))
    assert gd.fetch(InfoQuery("NVIDIA", "stock"))[0].published_at == "2026-11-02T10:15:00Z"
    monkeypatch.setattr(gd, "get", lambda url, **kw: FakeResponse(200, "Please limit requests to one every 5 seconds"))
    with pytest.raises(SourceUnavailable):
        gd.fetch(InfoQuery("NVIDIA", "stock"))
    sec = SecEdgarFilings()
    import tradingagents.dataflows.vendors.sec_edgar as edgar
    monkeypatch.setattr(edgar, "cik_for", lambda t: "0001045810")
    recent = {"form": ["4", "10-Q", "SD"], "accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3"],
              "primaryDocument": ["a.xml", "q.htm", "s.htm"], "primaryDocDescription": ["", "10-Q", ""],
              "filingDate": ["2026-11-01", "2026-10-30", "2026-10-29"], "acceptanceDateTime": ["2026-11-01T20:00:00.000Z", "", ""]}
    monkeypatch.setattr(sec, "get", lambda url, **kw: FakeResponse(200, {"name": "NVIDIA CORP", "filings": {"recent": recent}}))
    out = sec.fetch(InfoQuery("NVDA", "stock"))
    assert [i.title for i in out] == ["NVIDIA CORP 4", "NVIDIA CORP 10-Q：10-Q"]
    assert out[1].url == "https://www.sec.gov/Archives/edgar/data/1045810/0001262/q.htm"
    fed = FedPress()
    monkeypatch.setattr(fed, "get", lambda url, **kw: FakeResponse(200, "<rss><channel></channel></rss>"))
    assert fed.fetch(InfoQuery("x", "macro")) == []


# AC-10 --------------------------------------------------------------------
def test_jsonl_store_atomic_roundtrip_and_purge(tmp_path, monkeypatch):
    st = store.JsonlItemStore(tmp_path / "ic")
    item = InfoItem(source="gdelt", title="a", url="https://e.org/a", published_at="2026-11-01T00:00:00Z")
    other = InfoItem(source="fed_press", title="b", url="https://e.org/b", published_at="2026-11-01T00:00:00Z")
    st.save_job({"id": "ic-a-000001", "created_at": "x"})
    st.save_items("ic-a-000001", [item, item, other])
    assert [i.url for i in st.load_items("ic-a-000001")] == ["https://e.org/a", "https://e.org/b"]
    assert [i.source for i in st.load_items("ic-a-000001", "fed_press")] == ["fed_press"]
    assert not list((tmp_path / "ic").glob("*.tmp"))
    calls = []
    import tradingagents.dataflows.info_crawl.store as module
    real = module.replace_file
    monkeypatch.setattr(module, "replace_file", lambda path, write: calls.append(path) or real(path, write))
    st.save_items("ic-a-000001", [item])
    assert calls and calls[0].name == "ic-a-000001.jsonl"
    old = time.time() - 31 * 86400
    for path in (tmp_path / "ic").iterdir():
        os.utime(path, (old, old))
    st.save_job({"id": "ic-b-000002", "created_at": "y"})
    assert st.purge() == 2 and st.load_job("ic-a-000001") is None and st.load_job("ic-b-000002")
    with pytest.raises(ValueError):
        st.save_job({"id": "../evil"})
    mem = store.MemoryItemStore()
    mem.save_items("j", [item, item])
    assert len(mem.load_items("j")) == 1


def test_new_job_validation_is_chinese():
    for payload, word in (({}, "关键词"), ({"keyword": "N", "asset_type": "fx"}, "资产类型"),
                          ({"keyword": "N", "hours": 5}, "时间范围"), ({"keyword": "N", "sources": ["nope"]}, "未知来源"),
                          ({"keyword": "N", "asset_type": "stock", "sources": ["okx_announcements"]}, "至少选择")):
        with pytest.raises(jobs.JobRequestError) as err:
            jobs.new_job(payload)
        assert word in str(err.value)
    job = jobs.new_job({"keyword": "BTC", "asset_type": "crypto"})
    assert set(job["sources"]) == {a.id for a in registry.sources_for("crypto") if not a.optional}


# AC-15: injection default off, router method, prompt citation --------------
def test_inject_default_off_per_row_and_router_citation(monkeypatch, tmp_path):
    from webui import service
    iid = "0123456789abcdef"
    assert service._info_crawl_pick({"info_crawl_job": "ic-a-000001"}) is None
    assert service._info_crawl_pick({"info_crawl_job": "ic-a-000001", "info_inject": []}) is None
    assert service._info_crawl_pick({"info_crawl_job": "ic-a-000001", "info_inject": [iid, iid]}) == {"job": "ic-a-000001", "items": [iid]}
    for bad in (True, "true", ["../x"], [1]):
        with pytest.raises(service.ConsoleError):
            service._info_crawl_pick({"info_crawl_job": "ic-a-000001", "info_inject": bad})
    with pytest.raises(service.ConsoleError):
        service._info_crawl_pick({"info_crawl_job": "../x", "info_inject": [iid]})
    from tradingagents.dataflows import router
    assert router.get_category_for_method("get_info_items") == "info_items"
    assert "info_items" in router.OPTIONAL_CATEGORIES
    mem = store.MemoryItemStore()
    keep = InfoItem(source="fed_press", title="FOMC statement", url="https://www.federalreserve.gov/x", published_at="2026-10-29T18:00:00Z")
    skip = InfoItem(source="fed_press", title="Other", url="https://www.federalreserve.gov/y", published_at="2026-10-29T18:00:00Z")
    mem.save_items("ic-a-000001", [keep, skip])
    assert jobs.get_info_items("ic-a-000001", store=mem) == ""  # nothing ticked, nothing injected
    text = jobs.get_info_items("ic-a-000001", [keep.id], store=mem)
    assert text == "- FOMC statement [美联储新闻稿·2026-10-29·https://www.federalreserve.gov/x]"
    from tradingagents.agents.analysts import news_analyst
    from tradingagents.dataflows import config as dconfig
    seen = []
    monkeypatch.setattr(dconfig, "get_config", lambda: {"info_crawl_job": "ic-a-000001", "info_crawl_items": [keep.id]})
    monkeypatch.setattr(router, "route_to_vendor", lambda method, job, ids, **kw: seen.append(ids) or text)
    block = news_analyst._info_crawl_block()
    assert "FOMC statement" in block and "[source·date·link]" in block and seen == [[keep.id]]
    monkeypatch.setattr(dconfig, "get_config", lambda: {"info_crawl_job": "ic-a-000001"})
    assert news_analyst._info_crawl_block() == ""


# AC-11 / API --------------------------------------------------------------
_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo


@pytest.fixture
def console(monkeypatch, tmp_path):
    from webui import info_crawl
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *a, **k))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(home))
    mem = store.MemoryItemStore()
    info_crawl.set_store(mem)
    fake = Counting()
    fake.id, fake.assets = "fed_press", ("macro", "stock", "crypto")
    monkeypatch.setitem(registry.SOURCES, "fed_press", fake)
    monkeypatch.setattr(jobs, "SOURCES", registry.SOURCES)
    httpd = server.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def send(method, path, body=None, token=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        merged = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
        if token:
            merged[server.TOKEN_HEADER] = httpd.session_token
        merged.update(headers or {})
        conn.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=merged)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8") or "{}")
        conn.close()
        return resp.status, data

    yield send
    info_crawl.set_store(None)
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/info-crawl/sources"), ("GET", "/api/info-crawl/jobs"),
    ("GET", "/api/info-crawl/items?job=ic-a-000001"), ("POST", "/api/info-crawl/jobs"),
])
def test_api_requires_token(console, method, path):
    status, data = console(method, path, {"keyword": "NVDA"} if method == "POST" else None, token=False)
    assert status == 403 and "会话令牌" in data["error"]


def test_api_post_refuses_cross_site(console):
    status, _ = console("POST", "/api/info-crawl/jobs", {"keyword": "NVDA"}, headers={"Origin": "https://evil.example"})
    assert status == 403


def test_api_flow(console):
    status, data = console("GET", "/api/info-crawl/sources?asset=crypto")
    assert status == 200 and data["inject_default"] is False
    assert "okx_announcements" in {s["id"] for s in data["sources"]} and "sec_edgar" not in {s["id"] for s in data["sources"]}
    assert len(data["existing"]) == 3
    status, data = console("GET", "/api/info-crawl/sources?asset=fx")
    assert status == 400 and "资产类型" in data["error"]
    status, data = console("POST", "/api/info-crawl/jobs", {"keyword": "", "asset_type": "stock"})
    assert status == 400 and "关键词" in data["error"]
    status, data = console("POST", "/api/info-crawl/jobs", {"keyword": "NVDA", "asset_type": "stock", "hours": 24, "sources": ["fed_press"]})
    assert status == 200
    job_id = data["job"]["id"]
    for _ in range(50):
        status, data = console("GET", f"/api/info-crawl/jobs?id={job_id}")
        if data["job"]["status"] != "running":
            break
        time.sleep(0.05)
    assert data["job"]["status"] == "done" and data["job"]["sources"]["fed_press"]["count"] == 1
    status, data = console("GET", f"/api/info-crawl/items?job={job_id}")
    assert status == 200 and data["items"][0]["url"] == "https://e.org/1" and data["items"][0]["source_name"] == "计数"
    status, data = console("GET", "/api/info-crawl/items?job=ic-zz-999999")
    assert status == 404 and "抓取任务" in data["error"]
    status, data = console("GET", "/api/info-crawl/items?job=../x")
    assert status == 400
    status, data = console("GET", "/api/frameworks")
    assert [t["id"] for t in data["tools"]] == ["info-crawl"] and data["tools"][0]["name"] == "信息抓取"
    assert [f["id"] for f in data["frameworks"]] == ["tradingagents"]


# AC-14 --------------------------------------------------------------------
def test_ui_wiring_inject_default_off():
    app = (ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'const INFO_CRAWL_ID = "info-crawl"' in app and "renderInfoCrawl" in app
    assert "picked: []" in app and 'class="ic-pick"' in app and 'id="ic-inject"' not in app
    assert "info_inject: [...attach.items]" in app and "不可用" in app
    assert "getUTCHours" in app and ":${pad(date.getUTCMinutes())} UTC`" in app and "时间（UTC）" in app
    assert "仅限个人使用" in app and "保留 30 天" in app and "首批必选" in app
    assert 'rel="noopener noreferrer"' in app
    assert not re.search(r"https?://", app.split("// ---- V2-INFO-CRAWL")[1])
