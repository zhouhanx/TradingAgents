"""V2-COST-PRICING: cache-hit prices, currencies (W245, W246), BOM (W247), strict prices (W250).

Fake prices and fake usage only. The DeepSeek adapter is driven through a mock HTTP
transport: no network, no key (the key below is a placeholder string).
"""

import http.client
import json
import socket
import threading
from decimal import Decimal

import httpx
import pytest

from cli.stats_handler import StatsCallbackHandler
from tests import v2_cost_fixtures as fixtures
from webui import cost, server

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo
CURRENT = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
           "prompt_tokens_details": {"cached_tokens": 64}, "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36}
LEGACY = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
          "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36}


def _deepseek_call(usage: dict, stream: bool = False) -> dict:
    """One call through TradingAgents' DeepSeek adapter; returns the stored usage row."""
    from tradingagents.llm_clients.openai_client import DeepSeekChatOpenAI

    def reply(request):
        if not stream:
            body = json.dumps({"id": "resp-1", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                            "finish_reason": "stop"}], "usage": usage})
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})
        chunks = [{"id": "resp-1", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-chat",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}],
                   "usage": None},
                  {"id": "resp-1", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-chat",
                   "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}], "usage": usage}]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    handler = StatsCallbackHandler()
    model = DeepSeekChatOpenAI(model="deepseek-chat", api_key="placeholder-not-a-key",
                               base_url="https://api.deepseek.com", streaming=stream, callbacks=[handler],
                               http_client=httpx.Client(transport=httpx.MockTransport(reply)))
    model.invoke("hello")
    stats = handler.get_stats()
    assert stats["tokens_in"] == 100 and stats["tokens_out"] == 10
    return stats["calls"][0]


@pytest.mark.unit
def test_deepseek_cached_tokens_reach_cache_read_once():
    for stream in (False, True):
        row = _deepseek_call(CURRENT, stream)
        assert row["input_token_details"] == {"cache_read": 64}, stream   # not 128
        assert "cache_read_source" not in row


@pytest.mark.unit
def test_deepseek_hit_tokens_alone_are_kept_from_the_raw_usage():
    row = _deepseek_call(LEGACY)
    assert row["input_token_details"] == {"cache_read": 64}
    assert row["cache_read_source"] == "prompt_cache_hit_tokens"
    assert row["input_tokens"] == 100


@pytest.mark.unit
def test_hit_tokens_above_the_input_are_not_trusted():
    row = _deepseek_call({**LEGACY, "prompt_cache_hit_tokens": 500})
    assert row["input_token_details"] == {}


def _run(model: str, calls: list[dict], selections: dict | None = None) -> dict:
    selections = selections or {"analysts": ["market"], "llm_provider": "deepseek",
                                "quick_think_llm": model, "deep_think_llm": model}
    total_in = sum(call["input_tokens"] for call in calls)
    total_out = sum(call["output_tokens"] for call in calls)
    return {"id": "r", "kind": "analysis", "ticker": "X", "selections": selections,
            "role_models": cost.role_snapshot(selections),
            "usage": {"calls": calls, "reported": {"input_tokens": total_in, "output_tokens": total_out}}}


def _call(role: str, incoming: int, outgoing: int, hits=None) -> dict:
    details = {} if hits is None else {"cache_read": hits}
    return {"role": role, "input_tokens": incoming, "output_tokens": outgoing, "input_token_details": details}


def _row(view, role):
    return next(row for row in view["roles"] if row["role"] == role)


PRICE = {"input_per_million": "1", "output_per_million": "2", "currency": "USD"}


@pytest.mark.unit
def test_cache_hits_are_priced_at_the_cache_price():
    prices = {"m": {**PRICE, "cache_read_per_million": "0.1"}}
    view = cost._view_run(_run("m", [_call("Market Analyst", 1000, 100, hits=600)]), prices)
    row = _row(view, "Market Analyst")
    # 400 x 1 + 600 x 0.1 + 100 x 2 = 660 per million
    assert row["cost_text"] == "0.00066 USD" and row["price_note"] == "" and row["cache_read"] == 600
    assert view["api_text"] == "0.00066 USD"


@pytest.mark.unit
def test_cache_hits_without_a_cache_price_use_the_input_price_and_say_so():
    view = cost._view_run(_run("m", [_call("Market Analyst", 1000, 100, hits=600)]), {"m": PRICE})
    row = _row(view, "Market Analyst")
    assert row["cost_text"] == "0.0012 USD" and row["price_note"] == "缓存命中按输入单价计"
    plain = cost._view_run(_run("m", [_call("Market Analyst", 1000, 100)]), {"m": PRICE})
    assert _row(plain, "Market Analyst")["price_note"] == ""


@pytest.mark.unit
@pytest.mark.parametrize("hits", [-5, 1001, 2.5, "600", True])
def test_a_bad_cache_count_prices_all_input_at_the_input_price(hits):
    prices = {"m": {**PRICE, "cache_read_per_million": "0.1"}}
    view = cost._view_run(_run("m", [_call("Market Analyst", 1000, 100, hits=hits)]), prices)
    assert _row(view, "Market Analyst")["cost_text"] == "0.0012 USD"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["-0.1", "1_000", "abc", 20000, True, {}])
def test_a_stated_but_bad_cache_price_makes_the_model_unknown(value):
    view = cost._view_run(_run("m", [_call("Market Analyst", 1000, 100, hits=10)]),
                          {"m": {**PRICE, "cache_read_per_million": value}})
    assert _row(view, "Market Analyst")["cost_text"] == "价格未知"


@pytest.mark.unit
def test_mixed_currencies_are_never_added_together():
    selections = {"analysts": ["market"], "llm_provider": "deepseek", "quick_think_provider": "openai",
                  "quick_think_llm": "q", "deep_think_llm": "d"}
    calls = [_call("Market Analyst", 1000, 100), _call("Portfolio Manager", 1000, 100)]
    prices = {"q": PRICE, "d": {**PRICE, "currency": "CNY"}}
    view = cost._view_run(_run("q", calls, selections), prices)
    assert view["api_text"] == "币种不一致，不计入合计"
    assert _row(view, "Market Analyst")["cost_text"] == "0.0012 USD"
    assert _row(view, "Portfolio Manager")["cost_text"] == "0.0012 CNY"
    same = cost._view_run(_run("q", calls, selections), {"q": {**PRICE, "currency": "CNY"}, "d": prices["d"]})
    assert same["api_text"] == "0.0024 CNY"


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [("usd", "USD"), (" cny ", "CNY"), ("EUR", "EUR")])
def test_a_currency_code_is_normalised(value, expected):
    assert cost._price("m", {"m": {**PRICE, "currency": value}})["currency"] == expected


@pytest.mark.unit
def test_a_missing_currency_stays_usd():
    assert cost._price("m", {"m": {"input_per_million": 1, "output_per_million": 2}})["currency"] == "USD"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "  ", "US", "USDT", "$", "RMB元", "U5D", None, 5, ["USD"]])
def test_a_bad_currency_makes_the_model_unknown(value):
    assert cost._price("m", {"m": {**PRICE, "currency": value}}) is None
    view = cost._view_run(_run("m", [_call("Market Analyst", 1000, 100)]), {"m": {**PRICE, "currency": value}})
    assert _row(view, "Market Analyst")["cost_text"] == "价格未知"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["1_000", "1e3", " 1", "1 ", "+1", "-1", "0x10", "1.", ".5", "1,5", "１",
                                   "10000.01", "10001", 10000.5, 1e5, float("inf")])
def test_an_absurd_price_is_unknown(value):
    assert cost._unit_price(value) is None
    assert cost._price("m", {"m": {**PRICE, "input_per_million": value}}) is None


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [("0.50", "0.50"), ("10000", "10000"), (10000, "10000"),
                                            (0, "0"), (0.27, "0.27"), ("0", "0")])
def test_a_plain_price_is_kept(value, expected):
    assert cost._unit_price(value) == Decimal(expected)


@pytest.mark.unit
def test_a_price_file_with_a_bom_is_read(tmp_path):
    (tmp_path / cost.PRICES_NAME).write_bytes(b"\xef\xbb\xbf" + json.dumps({"models": {"m": PRICE}}).encode())
    assert cost.read_prices(tmp_path) == ({"m": PRICE}, False)


@pytest.mark.unit
def test_the_fixture_runs_show_cache_prices_and_mixed_currency(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    fixtures.build_pricing(tmp_path)
    page = cost.list_cost(tmp_path)
    runs = {run["id"]: run for run in page["runs"]}
    cached, mixed = runs[fixtures.CACHE_PRICED], runs[fixtures.MIXED]
    # chat: 500 x 2 + 1500 x 0.5 + 200 x 8 = 3350; reasoner (no cache price): 2000 x 4 + 200 x 16 = 11200
    assert _row(cached, "Market Analyst")["cost_text"] == "0.00335 CNY"
    assert _row(cached, "Portfolio Manager")["cost_text"] == "0.0112 CNY"
    assert _row(cached, "Portfolio Manager")["price_note"] == "缓存命中按输入单价计"
    assert cached["api_text"] == "0.04585 CNY"
    assert mixed["api_text"] == "币种不一致，不计入合计"
    assert page["prices_models"] == 3


@pytest.mark.unit
def test_the_shipped_template_documents_the_cache_price():
    from pathlib import Path

    data = json.loads(Path("webui/cost-prices.example.json").read_text(encoding="utf-8"))
    assert data["models"] == {}
    assert "cache_read_per_million" in data["_format"]
    assert "cache_read_per_million" in cost.__doc__


@pytest.fixture
def console(monkeypatch, tmp_path):
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(home))
    fixtures.build_pricing(home)
    httpd = server.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def get(path):  # tests/conftest.py signs the console in; the page token is httpd.session_token
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", path, headers={server.TOKEN_HEADER: httpd.session_token})
        response = connection.getresponse()
        data = response.read().decode("utf-8", "replace")
        connection.close()
        return response.status, json.loads(data or "{}")

    yield {"home": home, "get": get}
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.unit
def test_api_cost_with_cache_prices_mixed_currency_and_a_bom(console):
    prices = console["home"] / cost.PRICES_NAME
    prices.write_bytes(b"\xef\xbb\xbf" + prices.read_bytes())
    status, page = console["get"]("/api/cost")
    assert status == 200 and page["prices_unreadable"] is False and page["prices_models"] == 3
    runs = {run["id"]: run for run in page["runs"]}
    assert runs[fixtures.CACHE_PRICED]["api_text"] == "0.04585 CNY"
    assert runs[fixtures.MIXED]["api_text"] == "币种不一致，不计入合计"
    data = json.loads(prices.read_bytes().decode("utf-8-sig"))
    data["models"]["deepseek-reasoner"]["currency"] = "RMB元"
    data["models"]["deepseek-chat"]["input_per_million"] = "1_000"
    prices.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    status, page = console["get"]("/api/cost")
    assert status == 200 and page["prices_models"] == 1
    cached = {run["id"]: run for run in page["runs"]}[fixtures.CACHE_PRICED]
    assert {row["cost_text"] for row in cached["roles"] if row["prompt_count"] is not None} == {"价格未知"}
