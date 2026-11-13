"""V2-WATCHLIST W2: per-symbol reminder rules (R2, R11) and the auto refresh choice.

Rules are stored next to the symbol. Changing a rule never calls a model.
"""

import json
import socket

import pytest

from webui import watchlist

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo

def _rules(view, symbol="NVDA"):
    return next(item for item in view["items"] if item["symbol"] == symbol)["rules"]


def _add(root, kind, value, symbol="NVDA"):
    return watchlist.change_rule({"symbol": symbol, "op": "add", "kind": kind, "value": value}, root)


def _error(call):
    with pytest.raises(watchlist.WatchlistError) as caught:
        call()
    return caught.value


@pytest.mark.unit
def test_four_kinds_with_the_mock_labels(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    _add(tmp_path, "above", 190)
    _add(tmp_path, "below", "180.5")
    _add(tmp_path, "pct", 3)
    view = _add(tmp_path, "win", 1.5)
    rules = _rules(view)
    assert [rule["label"] for rule in rules] == [
        "高于 190.00", "低于 180.50", "涨跌 ≥ 3%（较昨收）", "一个刷新间隔内涨跌 ≥ 1.5%"]
    assert [rule["chip"] for rule in rules] == ["高于 190.00", "低于 180.50", "涨跌 ≥ 3%", "间隔内涨跌 ≥ 1.5%"]
    assert all(rule["state"] == "armed" and rule["state_label"] == "生效中。" for rule in rules)
    assert len({rule["id"] for rule in rules}) == 4


@pytest.mark.unit
def test_price_labels_follow_the_page_format_and_keep_typed_digits():
    assert watchlist.price_text(90000) == "90000.0"
    assert watchlist.price_text(0.5) == "0.5000"
    assert watchlist.price_text(1452.35) == "1452.35"


@pytest.mark.unit
@pytest.mark.parametrize("kind,value,text", [
    ("above", -5, "请输入大于 0 的价格。"), ("below", 0, "请输入大于 0 的价格。"), ("above", "abc", "请输入大于 0 的价格。"),
    ("pct", 0.05, "涨跌幅填 0.1 到 50 之间。"), ("pct", 51, "涨跌幅填 0.1 到 50 之间。"), ("win", "", "涨跌幅填 0.1 到 50 之间。"),
])
def test_validation_texts(tmp_path, kind, value, text):
    watchlist.add_symbol("NVDA", tmp_path)
    assert str(_error(lambda: _add(tmp_path, kind, value))) == text


@pytest.mark.unit
def test_a_duplicate_rule_is_refused_with_its_label(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    _add(tmp_path, "pct", 3)
    exc = _error(lambda: _add(tmp_path, "pct", "3.0"))
    assert str(exc) == "已有一条「涨跌 ≥ 3%（较昨收）」，不用重复加。" and exc.code == "duplicate"
    _add(tmp_path, "win", 3)  # same number, another kind: not a duplicate


@pytest.mark.unit
def test_at_most_five_rules_per_symbol(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    watchlist.add_symbol("AAPL", tmp_path)
    for value in (1, 2, 3, 4):
        view = _add(tmp_path, "above", value)
        assert next(i for i in view["items"] if i["symbol"] == "NVDA")["at_cap"] is False
    view = _add(tmp_path, "above", 5)
    assert next(i for i in view["items"] if i["symbol"] == "NVDA")["at_cap"] is True
    exc = _error(lambda: _add(tmp_path, "above", 6))
    assert str(exc) == "每个代码最多 5 条。" and exc.code == "rule_cap"
    _add(tmp_path, "above", 6, symbol="AAPL")  # the cap is per symbol


@pytest.mark.unit
def test_update_toggle_delete(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    view = _add(tmp_path, "above", 190)
    _add(tmp_path, "above", 180)
    ident = _rules(view)[0]["id"]
    exc = _error(lambda: watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": ident, "value": -5}, tmp_path))
    assert str(exc) == "请输入大于 0 的价格。"
    exc = _error(lambda: watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": ident, "value": 180}, tmp_path))
    assert str(exc) == "已有一条「高于 180.00」，不用重复加。"
    view = watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": ident, "value": 195}, tmp_path)
    assert _rules(view)[0]["label"] == "高于 195.00"
    view = watchlist.change_rule({"symbol": "NVDA", "op": "toggle", "rule_id": ident, "enabled": False}, tmp_path)
    assert _rules(view)[0]["enabled"] is False and _rules(view)[0]["state_label"] == "已停用。"
    view = watchlist.change_rule({"symbol": "NVDA", "op": "toggle", "rule_id": ident}, tmp_path)
    assert _rules(view)[0]["enabled"] is True
    view = watchlist.change_rule({"symbol": "NVDA", "op": "delete", "rule_id": ident}, tmp_path)
    assert [rule["label"] for rule in _rules(view)] == ["高于 180.00"]
    exc = _error(lambda: watchlist.change_rule({"symbol": "NVDA", "op": "delete", "rule_id": ident}, tmp_path))
    assert exc.status == 404
    assert _error(lambda: watchlist.change_rule({"symbol": "TSLA", "op": "add", "kind": "above", "value": 1}, tmp_path)).status == 404
    assert str(_error(lambda: watchlist.change_rule({"symbol": "NVDA", "op": "boom"}, tmp_path))) == "不支持的规则操作。"


@pytest.mark.unit
def test_rule_changes_never_record_a_trigger_or_start(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    view = _add(tmp_path, "above", 1)
    assert view["triggers"] == [] and view["mode"] == "observe"


@pytest.mark.unit
def test_old_files_load_without_rules(tmp_path):
    (tmp_path / "watchlist.json").write_text(json.dumps({
        "rule_version": "watch-1", "mode": "observe",
        "items": [{"symbol": "NVDA", "enabled": True, "last_price": 10}, {"symbol": "AAPL", "rules": "bad"}],
        "triggers": [{"id": "watch-1|NVDA|price|9->10", "symbol": "NVDA", "kind": "price", "rule_version": "watch-1",
                      "old_value": 9, "new_value": 10, "created_at": "2026-11-01T00:00:00+00:00"}],
    }), encoding="utf-8")
    view = watchlist.list_watch(tmp_path)
    assert [item["rules"] for item in view["items"]] == [[], []]
    assert view["poll_minutes"] == 0 and view["has_rules"] is False
    assert [row["kind"] for row in view["triggers"]] == ["price"]  # R4: old stored triggers still show


@pytest.mark.unit
def test_auto_refresh_defaults_to_five_minutes_with_rules_and_off_without(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    assert watchlist.list_watch(tmp_path)["poll_minutes"] == 0
    view = _add(tmp_path, "above", 1)
    assert view["poll_minutes"] == 5 and view["poll_stored"] is False
    ident = _rules(view)[0]["id"]
    view = watchlist.change_rule({"symbol": "NVDA", "op": "toggle", "rule_id": ident, "enabled": False}, tmp_path)
    assert view["poll_minutes"] == 0  # a disabled rule is not a reason to poll
    view = watchlist.set_poll(15, tmp_path)
    assert view["poll_minutes"] == 15 and view["poll_stored"] is True
    assert watchlist.set_poll(0, tmp_path)["poll_minutes"] == 0
    for bad in (2, "5", True, None):
        assert "自动刷新只能选" in str(_error(lambda bad=bad: watchlist.set_poll(bad, tmp_path)))


@pytest.fixture
def console(monkeypatch, tmp_path):
    import http.client
    import threading

    from webui import server

    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs))
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    httpd = server.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(path, body):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        connection.request("POST", path, body=json.dumps(body), headers={
            server.TOKEN_HEADER: httpd.session_token, "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8") or "{}")
        connection.close()
        return response.status, data

    yield post
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.unit
def test_rules_and_poll_over_http(console):
    assert console("/api/watchlist", {"symbol": "NVDA"})[0] == 200
    status, view = console("/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "above", "value": 190})
    assert status == 200 and _rules(view)[0]["label"] == "高于 190.00"
    status, body = console("/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "above", "value": 190})
    assert status == 400 and body == {"error": "已有一条「高于 190.00」，不用重复加。", "code": "duplicate"}
    status, body = console("/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "pct", "value": 99})
    assert status == 400 and body["error"] == "涨跌幅填 0.1 到 50 之间。"
    status, view = console("/api/watchlist/poll", {"minutes": 1})
    assert status == 200 and view["poll_minutes"] == 1
