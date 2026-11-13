"""W233 / W234: a bad cost-prices.json never breaks the cost page; bad prices are unknown.

Fixture runs from tests/v2_cost_fixtures.py (fake usage, fake prices). No model, no network.
"""

import http.client
import json
import socket
import threading

import pytest

from tests import v2_cost_fixtures
from webui import cost, server

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo
GOOD = {"input_per_million": "0.50", "output_per_million": "1.50", "currency": "USD"}
DEEP = {"input_per_million": "1.00", "output_per_million": "2.00", "currency": "USD"}

# Whole file unreadable (W233): every price unknown, notice on the page.
UNREADABLE = {
    "not-utf8": "\ufeff{\"models\": {}}".encode("utf-16"),
    "invalid-json": b'{"models": {"deepseek-chat": ',
    "infinity-literal": b'{"models": {"deepseek-chat": {"input_per_million": Infinity, "output_per_million": 1}}}',
    "nan-literal": b'{"models": {"deepseek-chat": {"input_per_million": NaN, "output_per_million": 1}}}',
    "top-level-list": b'[{"models": {}}]',
    "models-is-a-list": b'{"models": [["deepseek-chat", 0.5]]}',
    "no-models-key": b'{"deepseek-chat": {"input_per_million": 0.5, "output_per_million": 1.5}}',
}
# One model's price unusable (W234): that model only is unknown.
BAD_VALUES = {
    "overflow-1e999": b"1e999",
    "string-nan": b'"NaN"',
    "string-infinity": b'"Infinity"',
    "negative": b"-0.5",
    "negative-string": b'"-0.5"',
    "huge-string": b'"1e999"',
    "boolean": b"true",
    "object": b'{"amount": 1}',
}


def _home(tmp_path, monkeypatch, raw: bytes | None):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    v2_cost_fixtures.build(tmp_path, prices=False)
    if raw is not None:
        (tmp_path / cost.PRICES_NAME).write_bytes(raw)
    return tmp_path


def _bad_deep(value: bytes) -> bytes:
    good = json.dumps(GOOD).encode()
    return (b'{"models": {"deepseek-chat": ' + good + b', "deepseek-reasoner": {"input_per_million": '
            + value + b', "output_per_million": "2.00"}}}')


def _checked(page):
    return next(run for run in page["runs"] if run["id"] == v2_cost_fixtures.CHECKED)


def _assert_all_unknown(page):
    assert page["prices_unreadable"] is True and page["prices_models"] == 0
    run = _checked(page)
    assert run["match_text"] == "与供应商计数一致，容差 0。"
    assert run["api_text"] == "价格未知"
    priced = [row for row in run["roles"] if row["prompt_count"] is not None]
    assert priced and {row["cost_text"] for row in priced} == {"价格未知"}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(UNREADABLE))
def test_an_unreadable_price_file_makes_every_price_unknown(tmp_path, monkeypatch, name):
    home = _home(tmp_path, monkeypatch, UNREADABLE[name])
    assert cost.read_prices(home) == ({}, True)
    _assert_all_unknown(cost.list_cost(home))


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(BAD_VALUES))
def test_a_bad_price_is_unknown_for_that_model_only(tmp_path, monkeypatch, name):
    home = _home(tmp_path, monkeypatch, _bad_deep(BAD_VALUES[name]))
    models, unreadable = cost.read_prices(home)
    assert unreadable is False
    assert cost._price("deepseek-reasoner", models) is None
    assert cost._price("deepseek-chat", models)["input_per_million"] == cost.Decimal("0.50")
    page = cost.list_cost(home)
    assert page["prices_unreadable"] is False and page["prices_models"] == 1
    roles = {row["role"]: row["cost_text"] for row in _checked(page)["roles"]}
    assert roles["Portfolio Manager"] == roles["Research Manager"] == "价格未知"
    # Priced, but the run total is unknown, so the quick roles' money is not added up.
    assert roles["Market Analyst"] == "不计入合计"
    assert _checked(page)["api_text"] == "价格未知"


@pytest.mark.unit
def test_a_bad_price_on_an_unused_model_leaves_the_others_priced(tmp_path, monkeypatch):
    raw = json.dumps({"models": {"deepseek-chat": GOOD, "deepseek-reasoner": DEEP,
                                 "unused": {"input_per_million": -1, "output_per_million": 1}}}).encode()
    home = _home(tmp_path, monkeypatch, raw.replace(b"-1", b"1e999"))
    page = cost.list_cost(home)
    assert page["prices_unreadable"] is False and page["prices_models"] == 2
    run = _checked(page)
    assert run["api_text"].endswith(" USD")
    assert all(row["cost_text"].endswith(" USD") for row in run["roles"] if row["prompt_count"] is not None)


@pytest.mark.unit
def test_a_valid_or_missing_price_file_still_works(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, None)
    page = cost.list_cost(home)
    assert page["prices_unreadable"] is False and page["prices_models"] == 0
    (home / cost.PRICES_NAME).write_text(json.dumps({"models": {"deepseek-chat": GOOD, "deepseek-reasoner": DEEP}}),
                                         encoding="utf-8")
    page = cost.list_cost(home)
    assert page["prices_unreadable"] is False and page["prices_models"] == 2
    # 900..1020 prompt / 90..102 completion tokens per role (fixture data).
    assert _checked(page)["api_text"].endswith(" USD")


@pytest.mark.unit
def test_a_price_too_large_to_state_is_unknown_not_an_error(tmp_path, monkeypatch):
    big = {"input_per_million": "1e40", "output_per_million": "1"}
    home = _home(tmp_path, monkeypatch, json.dumps({"models": {"deepseek-chat": big, "deepseek-reasoner": DEEP}}).encode())
    roles = {row["role"]: row["cost_text"] for row in _checked(cost.list_cost(home))["roles"]}
    assert roles["Market Analyst"] == "价格未知"


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
    v2_cost_fixtures.build(home, prices=False)
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
def test_api_cost_never_fails_for_any_price_file(console):
    prices = console["home"] / cost.PRICES_NAME
    for name, raw in sorted(UNREADABLE.items()):
        prices.write_bytes(raw)
        status, page = console["get"]("/api/cost")
        assert status == 200, name
        _assert_all_unknown(page)
    for name, value in sorted(BAD_VALUES.items()):
        prices.write_bytes(_bad_deep(value))
        status, page = console["get"]("/api/cost")
        assert status == 200, name
        assert page["prices_unreadable"] is False and page["prices_models"] == 1, name
        roles = {row["role"]: row["cost_text"] for row in _checked(page)["roles"]}
        assert roles["Portfolio Manager"] == "价格未知", name
    prices.write_text(json.dumps({"models": {"deepseek-chat": GOOD, "deepseek-reasoner": DEEP}}), encoding="utf-8")
    status, page = console["get"]("/api/cost")
    assert status == 200 and page["prices_unreadable"] is False and page["prices_models"] == 2
    assert _checked(page)["api_text"].endswith(" USD")
