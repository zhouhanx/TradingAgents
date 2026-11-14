"""V2-WATCHLIST-POLISH: W261 (corrupt file), W263/W265 (prune), W262 (unseen by sequence), W259/W260, W264 (bad rows), W266 (BOM), W267 (upto_seq)."""

import json
import socket
import time

import pytest

from webui import watchlist

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo


def _write(root, raw: bytes):
    (root / "watchlist.json").write_bytes(raw)


def _backups(root):
    return sorted(path.name for path in root.glob("watchlist.json.corrupt-*"))


@pytest.mark.unit
@pytest.mark.parametrize("raw", [
    b'{"rule_version": "watch-1", "items": [{"symbol": "NVDA"',  # truncated
    b"",                                                          # emptied
    b"not json at all",
    b"[1, 2, 3]",                                                 # not an object
    b'{"items": "NVDA"}',                                         # wrong shape
    b'{"items": [], "triggers": {"a": 1}}',
    b"\xff\xfe\x00broken",                                       # not UTF-8
])
def test_a_broken_file_is_not_read_as_empty_and_is_backed_up_once(tmp_path, raw):
    _write(tmp_path, raw)
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.list_watch(tmp_path)
    exc = caught.value
    assert exc.status == 409 and exc.code == "watchlist_corrupt"
    backups = _backups(tmp_path)
    assert len(backups) == 1 and backups[0].startswith("watchlist.json.corrupt-2")
    assert (tmp_path / backups[0]).read_bytes() == raw
    assert str(exc) == (f"自选数据文件 watchlist.json 已损坏，没有按空列表读取，也不会写入。原文件已备份为 {backups[0]}。"
                        "请修复或移走 watchlist.json 后刷新页面。")
    with pytest.raises(watchlist.WatchlistError):
        watchlist.list_watch(tmp_path)
    assert _backups(tmp_path) == backups  # the same bad bytes are not copied again


@pytest.mark.unit
@pytest.mark.parametrize("write", [
    lambda root: watchlist.add_symbol("AAPL", root),
    lambda root: watchlist.remove_symbol("NVDA", root),
    lambda root: watchlist.refresh({"NVDA": 1}, root, {}),
    lambda root: watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": 1}, root),
    lambda root: watchlist.set_poll(5, root),
    lambda root: watchlist.mark_seen(root),
    lambda root: watchlist.explicit_analyze("NVDA", root),
    lambda root: watchlist.admit("NVDA", "x", root),
])
def test_writes_are_refused_and_never_overwrite_the_broken_file(tmp_path, write):
    raw = b'{"rule_version": "watch-1", "items": [{"symbol": "NVDA", "enabled": true}], "trig'
    _write(tmp_path, raw)
    with pytest.raises(watchlist.WatchlistError) as caught:
        write(tmp_path)
    assert caught.value.code == "watchlist_corrupt"
    assert (tmp_path / "watchlist.json").read_bytes() == raw
    assert not (tmp_path / "watchlist.json.tmp").exists()


@pytest.mark.unit
def test_a_fixed_file_works_again_and_a_new_breakage_gets_its_own_backup(tmp_path):
    _write(tmp_path, b"{broken")
    with pytest.raises(watchlist.WatchlistError):
        watchlist.list_watch(tmp_path)
    (tmp_path / "watchlist.json").unlink()
    assert watchlist.add_symbol("NVDA", tmp_path)["items"][0]["symbol"] == "NVDA"
    _write(tmp_path, b"{broken again")
    time.sleep(1.1)
    with pytest.raises(watchlist.WatchlistError):
        watchlist.list_watch(tmp_path)
    assert len(_backups(tmp_path)) == 2


@pytest.mark.unit
def test_an_unreadable_file_is_refused_without_a_backup(tmp_path, monkeypatch):
    _write(tmp_path, b"{}")
    real = type(tmp_path).read_bytes

    def deny(self):
        if self.name == "watchlist.json":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(type(tmp_path), "read_bytes", deny)
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.list_watch(tmp_path)
    assert caught.value.code == "watchlist_corrupt" and "读不到自选数据文件 watchlist.json" in str(caught.value)
    assert _backups(tmp_path) == []


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

    def call(method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers={
            server.TOKEN_HEADER: httpd.session_token, "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8") or "{}")
        connection.close()
        return response.status, data

    yield call
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.unit
def test_api_answers_409_with_a_code_for_a_broken_file(console, tmp_path):
    _write(tmp_path, b'{"items": [')
    status, body = console("GET", "/api/watchlist")
    assert status == 409 and body["code"] == "watchlist_corrupt"
    assert body["error"].startswith("自选数据文件 watchlist.json 已损坏，没有按空列表读取，也不会写入。")
    status, body = console("POST", "/api/watchlist", {"symbol": "AAPL"})
    assert status == 409 and body["code"] == "watchlist_corrupt"
    assert (tmp_path / "watchlist.json").read_bytes() == b'{"items": ['


# W263 -------------------------------------------------------------------------------------------

def _store_with_triggers(root, total, checked):
    watchlist.add_symbol("NVDA", root)
    path = root / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["triggers"] = [{"id": f"old-{i}", "symbol": "NVDA", "kind": "price", "rule_version": "watch-1",
                         "created_at": "2026-11-01T00:00:00+00:00"} for i in range(total)]
    data["checks"] = [{"trigger_id": f"old-{i}", "result": "refused", "reason": "x",
                       "checked_at": "2026-11-01T00:00:00+00:00"} for i in checked]
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _quote(price, at):
    return {"price": price, "prev_close": None, "trading_day": "2026-11-13", "fetched_at": at, "delayed": True}


def _fire_one(root):
    watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": 1}, root)
    return watchlist.refresh({"NVDA": _quote(5, time.time() + 180)}, root)


@pytest.mark.unit
def test_checked_triggers_are_pruned_too_so_the_store_never_passes_500(tmp_path):
    path = _store_with_triggers(tmp_path, 520, checked=range(520))
    view = _fire_one(tmp_path)
    ids = [row["id"] for row in view["triggers"]]
    assert len(ids) == 500 and view["trigger_total"] == 500
    assert "old-20" not in ids and "old-21" in ids and ids[-1].startswith("rule|NVDA|")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert len(stored["triggers"]) == 500
    assert len(stored["checks"]) == 520  # check records stay as history


def _store_rows(root, rows, seen_seq):
    """rows: kinds in table order ("rule" or "price"), numbered 1.. in that order."""
    watchlist.add_symbol("NVDA", root)
    path = root / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["triggers"] = [{"id": f"old-{i}", "symbol": "NVDA", "kind": kind, "rule_version": "watch-1", "seq": i + 1,
                         "created_at": "2026-11-01T00:00:00+00:00"} for i, kind in enumerate(rows)]
    data["trigger_seq"] = len(rows)
    data["alerts_seen_seq"] = seen_seq
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _gone(view, total):
    return {f"old-{i}" for i in range(total)} - {row["id"] for row in view["triggers"]}


@pytest.mark.unit
def test_w265_seen_triggers_go_before_unseen_ones(tmp_path):
    # Odd rows are unseen rule hits, even rows are price triggers (never unseen).
    _store_rows(tmp_path, ["price" if i % 2 == 0 else "rule" for i in range(505)], seen_seq=0)
    view = _fire_one(tmp_path)
    assert view["trigger_total"] == 500
    assert _gone(view, 505) == {"old-0", "old-2", "old-4", "old-6", "old-8", "old-10"}
    assert view["unseen_count"] == 253  # 252 old unseen hits and the new one, none deleted


def _check(path, indexes, key="checks"):
    data = json.loads(path.read_text(encoding="utf-8"))
    data[key] = data.get(key, []) + [{"trigger_id": f"old-{i}", "result": "refused", "reason": "x",
                                      "checked_at": "2026-11-01T00:00:00+00:00"} for i in indexes]
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.unit
def test_unchecked_triggers_go_before_checked_ones(tmp_path):
    _store_with_triggers(tmp_path, 505, checked=range(0, 505, 2))  # 253 checked, 252 unchecked
    ids = [row["id"] for row in _fire_one(tmp_path)["triggers"]]
    assert len(ids) == 500
    gone = {f"old-{i}" for i in range(505)} - set(ids)
    assert gone == {"old-1", "old-3", "old-5", "old-7", "old-9", "old-11"}


@pytest.mark.unit
def test_w265_tier_1_then_2_seen_is_the_seen_mark_and_referenced_rows_wait(tmp_path):
    path = _store_rows(tmp_path, ["rule"] * 505, seen_seq=3)
    _check(path, [0, 300])
    view = _fire_one(tmp_path)
    # Tier 1: unreferenced seen old-1, old-2 (old-0 is seen but checked). Tier 2: oldest
    # unreferenced unseen old-3..old-6. Referenced old-0 and old-300 stay.
    assert _gone(view, 505) == {"old-1", "old-2", "old-3", "old-4", "old-5", "old-6"}
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert len(stored["triggers"]) == 500 and len(stored["checks"]) == 2


@pytest.mark.unit
def test_w265_tier_3_referenced_rows_go_last_by_time_and_admissions_count_too(tmp_path):
    path = _store_rows(tmp_path, ["rule"] * 505, seen_seq=100)
    referenced = [i for i in range(505) if i not in (10, 200)]
    _check(path, referenced[::2])
    _check(path, referenced[1::2], key="admissions")
    view = _fire_one(tmp_path)
    # Tier 1: old-10 (seen, unreferenced). Tier 2: old-200 (unseen, unreferenced).
    # Tier 3: the oldest referenced, seen or not: old-0..old-3.
    assert _gone(view, 505) == {"old-10", "old-200", "old-0", "old-1", "old-2", "old-3"}
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert len(stored["checks"]) + len(stored["admissions"]) == 503  # records stay as history


@pytest.mark.unit
def test_w265_the_newest_twenty_are_never_deleted_even_when_seen(tmp_path):
    # 485 unseen rule hits, then 20 seen price triggers; the new hit makes the newest 20
    # old-486..old-504 plus itself, so old-485 is the only seen row that may go.
    _store_rows(tmp_path, ["rule"] * 485 + ["price"] * 20, seen_seq=0)
    view = _fire_one(tmp_path)
    assert view["trigger_total"] == 500
    assert _gone(view, 505) == {"old-485", "old-0", "old-1", "old-2", "old-3", "old-4"}
    assert [row["id"] for row in view["triggers"][-20:-1]] == [f"old-{i}" for i in range(486, 505)]


@pytest.mark.unit
def test_w265_prune_keeps_the_newest_twenty_when_every_older_row_is_unseen(tmp_path):
    data = {"alerts_seen_seq": 0, "triggers": [
        {"id": f"t{i}", "kind": "rule", "seq": i + 1} for i in range(530)]}
    watchlist._prune_triggers(data)
    assert [row["id"] for row in data["triggers"]] == [f"t{i}" for i in range(30, 530)]


@pytest.mark.unit
def test_w265_prune_keeps_the_newest_twenty_even_when_only_they_are_unreferenced():
    data = {"alerts_seen_seq": 0,
            "triggers": [{"id": f"t{i}", "kind": "price", "seq": i + 1} for i in range(505)],
            "checks": [{"trigger_id": f"t{i}"} for i in range(485)]}
    watchlist._prune_triggers(data)
    assert [row["id"] for row in data["triggers"]] == [f"t{i}" for i in range(5, 505)]


# W262 -------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_unseen_is_counted_by_trigger_number_so_two_tabs_in_one_second_agree(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    second = datetime.now(UTC).replace(microsecond=0).isoformat()
    monkeypatch.setattr(watchlist, "_now", lambda: second)  # one second throughout
    at = time.time() + 120
    watchlist.add_symbol("NVDA", tmp_path)
    watchlist.add_symbol("AAPL", tmp_path)
    watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": 1}, tmp_path)
    watchlist.change_rule({"symbol": "AAPL", "op": "add", "kind": "above", "value": 1}, tmp_path)
    tab_a = watchlist.refresh({"NVDA": _quote(5, at)}, tmp_path)           # tab A records hit 1 and shows it
    shown = max(row["seq"] for row in tab_a["triggers"])
    tab_b = watchlist.refresh({"AAPL": _quote(5, at + 1)}, tmp_path)       # tab B records hit 2, same second
    assert [row["seq"] for row in tab_b["triggers"]] == [1, 2] and tab_b["trigger_seq"] == 2
    view = watchlist.mark_seen(tmp_path, upto_seq=shown)                   # tab A says it saw what it showed
    assert view["unseen_count"] == 1 and view["alerts_seen_seq"] == 1
    assert [row["unseen"] for row in view["triggers"]] == [False, True]
    assert watchlist.mark_seen(tmp_path, upto_seq=0)["unseen_count"] == 1  # the mark never goes back
    view = watchlist.mark_seen(tmp_path, upto_seq=99)                      # clamped to what exists
    assert view["unseen_count"] == 0 and view["alerts_seen_seq"] == 2
    tab_a = watchlist.refresh({"NVDA": _quote(0.5, at + 2)}, tmp_path)
    tab_a = watchlist.refresh({"NVDA": _quote(5, at + 3)}, tmp_path)        # re-armed: hit 3
    assert tab_a["unseen_count"] == 1 and tab_a["triggers"][-1]["seq"] == 3


@pytest.mark.unit
def test_seen_without_a_number_still_clears_everything(tmp_path):
    _store_with_triggers(tmp_path, 3, checked=[])
    _fire_one(tmp_path)
    assert watchlist.list_watch(tmp_path)["unseen_count"] == 1
    assert watchlist.mark_seen(tmp_path)["unseen_count"] == 0


@pytest.mark.unit
@pytest.mark.parametrize("bad", [-1, 1.5, "3", True, False, 0.0, 2.0, [1], {}, "0"])
def test_a_bad_seen_number_is_refused(tmp_path, bad):
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.mark_seen(tmp_path, upto_seq=bad)
    assert caught.value.status == 400 and str(caught.value) == "upto_seq 须为不小于 0 的整数。"


@pytest.mark.unit
def test_an_older_file_is_numbered_in_table_order_and_keeps_its_seen_time(tmp_path):
    def rule(i, made):
        return {"id": f"rule|NVDA|r{i}#1", "symbol": "NVDA", "kind": "rule", "rule_version": "watch-1",
                "text": "高于 1.00 · 现价 5.00", "created_at": made}
    (tmp_path / "watchlist.json").write_text(json.dumps({
        "rule_version": "watch-1", "items": [{"symbol": "NVDA"}],
        "triggers": [rule(1, "2026-11-13T10:00:00+00:00"), rule(2, "2026-11-13T10:00:05+00:00"),
                     rule(3, "2026-11-13T10:00:09+00:00")],
        "alerts_seen_at": "2026-11-13T10:00:05+00:00",
    }), encoding="utf-8")
    view = watchlist.list_watch(tmp_path)
    assert [row["seq"] for row in view["triggers"]] == [1, 2, 3]
    assert view["alerts_seen_seq"] == 2 and view["unseen_count"] == 1 and view["trigger_seq"] == 3


@pytest.mark.unit
def test_api_seen_takes_upto_seq(console, tmp_path):
    status, body = console("POST", "/api/watchlist/seen", {"upto_seq": "x"})
    assert status == 400 and body["error"] == "upto_seq 须为不小于 0 的整数。"
    status, body = console("POST", "/api/watchlist/seen", {"upto_seq": 0})
    assert status == 200 and body["unseen_count"] == 0 and body["alerts_seen_seq"] == 0


# W259 / W260 ------------------------------------------------------------------------------------

def _rules_of(view, symbol="NVDA"):
    return next(i for i in view["items"] if i["symbol"] == symbol)["rules"]


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, -1, "0", float("nan"), float("inf"), "inf", "-inf", "nan", True, None, "", "abc", "-5", " + "])
def test_a_threshold_must_be_a_finite_positive_number(tmp_path, value):
    watchlist.add_symbol("NVDA", tmp_path)
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": value}, tmp_path)
    assert str(caught.value) == "请输入大于 0 的价格。" and caught.value.code == "bad_price"


@pytest.mark.unit
def test_a_threshold_is_at_most_1e12(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    view = watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "below", "value": 1e12}, tmp_path)
    assert _rules_of(view)[0]["label"] == "低于 1000000000000.0"
    for kind, value in (("above", 1e12 * 1.0000001), ("below", "2000000000000"),
                        ("above", "1000000000000.000001"), ("below", "01000000000000.1")):
        with pytest.raises(watchlist.WatchlistError) as caught:
            watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": kind, "value": value}, tmp_path)
        assert str(caught.value) == "价格不能超过 1 万亿（1e12）。" and caught.value.code == "bad_price"
    ident = _rules_of(view)[0]["id"]
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": ident, "value": 5e12}, tmp_path)
    assert str(caught.value) == "价格不能超过 1 万亿（1e12）。"


@pytest.mark.unit
@pytest.mark.parametrize("kind,value,label,chip,text", [
    # W259 PM ruling: labels keep the typed digits; prices are padded up to the page format, never rounded.
    ("above", "1452.35", "高于 1452.35", "高于 1452.35", "1452.35"),
    ("above", "1452.30", "高于 1452.30", "高于 1452.30", "1452.30"),
    ("above", "190", "高于 190.00", "高于 190.00", "190"),
    ("above", 190, "高于 190.00", "高于 190.00", "190"),
    ("below", "0.123456", "低于 0.123456", "低于 0.123456", "0.123456"),
    ("below", "0.5", "低于 0.5000", "低于 0.5000", "0.5"),
    ("above", " +190 ", "高于 190.00", "高于 190.00", "190"),
    ("above", "98765.4321", "高于 98765.4321", "高于 98765.4321", "98765.4321"),
    ("above", " 0190.50 ", "高于 190.50", "高于 190.50", "190.50"),
    ("pct", "3.14159", "涨跌 ≥ 3.14159%（较昨收）", "涨跌 ≥ 3.14159%", "3.14159"),
    ("pct", "3.0", "涨跌 ≥ 3.0%（较昨收）", "涨跌 ≥ 3.0%", "3.0"),
    ("pct", 3, "涨跌 ≥ 3%（较昨收）", "涨跌 ≥ 3%", "3"),
    ("win", 1.5, "一个刷新间隔内涨跌 ≥ 1.5%", "间隔内涨跌 ≥ 1.5%", "1.5"),
])
def test_labels_keep_the_typed_digits_and_survive_a_reload(tmp_path, kind, value, label, chip, text):
    watchlist.add_symbol("NVDA", tmp_path)
    watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": kind, "value": value}, tmp_path)
    stored = json.loads((tmp_path / "watchlist.json").read_text(encoding="utf-8"))["items"][0]["rules"][0]
    assert stored["value_text"] == text and stored["value"] == float(str(value).strip())
    rule = _rules_of(watchlist.list_watch(tmp_path))[0]  # a fresh read of the file
    assert (rule["label"], rule["chip"], rule["value_text"]) == (label, chip, text)


@pytest.mark.unit
@pytest.mark.parametrize("kind,value", [("above", "1452.1234567"), ("below", 0.1234567), ("above", "+1.0000001"),
                                        ("pct", "3.1234567"), ("win", "1.0000000")])
def test_more_than_six_decimals_is_refused_and_not_saved(tmp_path, kind, value):
    watchlist.add_symbol("NVDA", tmp_path)
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": kind, "value": value}, tmp_path)
    assert caught.value.status == 400 and caught.value.code == "bad_decimals" and str(caught.value) == "小数最多 6 位。"
    assert _rules_of(watchlist.list_watch(tmp_path)) == []
    view = watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": kind, "value": "2.123456"}, tmp_path)
    ident = _rules_of(view)[0]["id"]
    with pytest.raises(watchlist.WatchlistError):
        watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": ident, "value": value}, tmp_path)
    assert _rules_of(watchlist.list_watch(tmp_path))[0]["value_text"] == "2.123456"


@pytest.mark.unit
def test_retyping_the_same_number_with_other_digits_changes_the_label_only(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    view = watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": "1452.3"}, tmp_path)
    rule = _rules_of(view)[0]
    view = watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": rule["id"], "value": "1452.30"}, tmp_path)
    after = _rules_of(view)[0]
    assert after["label"] == "高于 1452.30" and after["state_label"] == "生效中。"
    stored = json.loads((tmp_path / "watchlist.json").read_text(encoding="utf-8"))["items"][0]["rules"][0]
    assert stored["arm_seq"] == 1  # the same threshold: not re-armed
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": "1452.300"}, tmp_path)
    assert str(caught.value) == "已有一条「高于 1452.30」，不用重复加。"


@pytest.mark.unit
def test_a_rule_saved_before_value_text_reads_from_its_number(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["rules"] = [{"id": "r1", "kind": "above", "value": 1452.35, "enabled": True, "state": "armed"},
                                 {"id": "r2", "kind": "pct", "value": 3.0, "enabled": True, "state": "armed"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert [r["label"] for r in _rules_of(watchlist.list_watch(tmp_path))] == ["高于 1452.35", "涨跌 ≥ 3%（较昨收）"]


@pytest.mark.unit
def test_api_refuses_seven_decimals_with_400(console):
    console("POST", "/api/watchlist", {"symbol": "NVDA"})
    status, body = console("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "above", "value": "1452.1234567"})
    assert status == 400 and body == {"error": "小数最多 6 位。", "code": "bad_decimals"}
    status, view = console("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "above", "value": "1452.30"})
    assert status == 200 and _rules_of(view)[0]["label"] == "高于 1452.30"


@pytest.mark.unit
def test_an_update_with_another_type_is_refused(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    view = watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": 190}, tmp_path)
    ident = _rules_of(view)[0]["id"]
    for other in ("below", "pct", "win", "nonsense", 5):
        with pytest.raises(watchlist.WatchlistError) as caught:
            watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": ident, "kind": other, "value": 3}, tmp_path)
        assert caught.value.status == 400 and caught.value.code == "kind_locked"
        assert str(caught.value) == "规则类型不能修改，请删除后重新添加。"
    assert _rules_of(watchlist.list_watch(tmp_path))[0]["value"] == 190
    view = watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": ident, "kind": "above", "value": 195}, tmp_path)
    assert _rules_of(view)[0]["label"] == "高于 195.00"
    view = watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": ident, "value": 196}, tmp_path)
    assert _rules_of(view)[0]["label"] == "高于 196.00"  # without kind, the rule's own type


@pytest.mark.unit
def test_api_update_with_another_type_answers_400(console):
    status, view = console("POST", "/api/watchlist", {"symbol": "NVDA"})
    status, view = console("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "above", "value": 190})
    ident = _rules_of(view)[0]["id"]
    status, body = console("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "update", "rule_id": ident, "kind": "pct", "value": 3})
    assert status == 400 and body == {"error": "规则类型不能修改，请删除后重新添加。", "code": "kind_locked"}


# W-WL2 ------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_visible_triggers_from_one_second_follow_the_table_order(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    second = datetime.now(UTC).replace(microsecond=0).isoformat()
    monkeypatch.setattr(watchlist, "_now", lambda: second)
    at = time.time() + 120
    for symbol in ("NVDA", "BTC-USDT", "XAUUSD"):
        watchlist.add_symbol(symbol, tmp_path)
        watchlist.change_rule({"symbol": symbol, "op": "add", "kind": "above", "value": 1}, tmp_path)
    # recorded in the order XAUUSD, BTC-USDT, NVDA (one refresh each, same second)
    for symbol in ("XAUUSD", "BTC-USDT", "NVDA"):
        view = watchlist.refresh({symbol: _quote(5, at)}, tmp_path)
    assert [row["symbol"] for row in view["triggers"]] == ["XAUUSD", "BTC-USDT", "NVDA"]
    assert [row["symbol"] for row in view["visible_triggers"]] == ["NVDA", "BTC-USDT", "XAUUSD"]


@pytest.mark.unit
def test_table_order_only_reorders_within_one_second():
    rows = [{"symbol": "B", "seq": 4, "created_at": "t2"}, {"symbol": "A", "seq": 3, "created_at": "t2"},
            {"symbol": "A", "seq": 2, "created_at": "t1"}, {"symbol": "Z", "seq": 1, "created_at": "t1"},
            {"symbol": "B", "seq": 0, "created_at": "t0"}]
    out = watchlist._table_order(rows, ["A", "B"])
    assert [row["seq"] for row in out] == [3, 4, 2, 1, 0]  # Z is not in the table: after the listed ones


# W264 / W266 / W267 (PM ruling, commit 7) -------------------------------------------------------

def _good_file():
    return {
        "rule_version": "watch-1", "mode": "observe",
        "items": [{"symbol": "NVDA", "enabled": True, "last_price": 10,
                   "rules": [{"id": "r1", "kind": "above", "value": 5, "value_text": "5", "enabled": True,
                              "arm_seq": 1}]}],
        "triggers": [{"id": "rule|NVDA|r1#1", "symbol": "NVDA", "kind": "rule", "rule_id": "r1", "seq": 1,
                      "rule_version": "watch-1", "created_at": "2026-11-13T00:00:00+00:00"}],
        "checks": [{"trigger_id": "rule|NVDA|r1#1", "result": "refused", "reason": "x"}],
        "admissions": [],
        "trigger_seq": 1, "alerts_seen_seq": 0,
    }


def _bad_row(change):
    data = _good_file()
    change(data)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


BAD_ROWS = {
    "triggers-string-row": lambda d: d["triggers"].append("rule|NVDA|r1#2"),
    "triggers-number-row": lambda d: d["triggers"].append(7),
    "triggers-null-row": lambda d: d["triggers"].append(None),
    "triggers-id-not-text": lambda d: d["triggers"][0].update(id=["x"]),
    "triggers-symbol-not-text": lambda d: d["triggers"][0].update(symbol={"a": 1}),
    "checks-string-row": lambda d: d["checks"].append("refused"),
    "checks-list-row": lambda d: d["checks"].append([1, 2]),
    "checks-trigger-id-not-text": lambda d: d["checks"][0].update(trigger_id=["x"]),
    "admissions-string-row": lambda d: d["admissions"].append("admitted"),
    "items-string-row": lambda d: d["items"].append("AAPL"),
    "items-null-row": lambda d: d["items"].append(None),
    "items-missing-symbol": lambda d: d["items"].append({"enabled": True}),
    "items-empty-symbol": lambda d: d["items"].append({"symbol": "  "}),
    "items-symbol-not-text": lambda d: d["items"].append({"symbol": 600519}),
    "rules-string-row": lambda d: d["items"][0]["rules"].append("above 5"),
    "rules-missing-id": lambda d: d["items"][0]["rules"][0].pop("id"),
    "rules-arm-not-whole": lambda d: d["items"][0]["rules"][0].update(arm_seq="2"),
    "rules-not-a-list": lambda d: d["items"][0].update(rules="bad"),        # commit 8
    "rules-object": lambda d: d["items"][0].update(rules={"r1": {}}),       # commit 8
    "rules-null": lambda d: d["items"][0].update(rules=None),               # commit 8
}


def _calls():
    quote = {"price": 6, "prev_close": 5, "trading_day": "2026-11-13", "fetched_at": time.time() + 100}
    return [
        ("GET", "/api/watchlist", None),
        ("POST", "/api/watchlist", {"symbol": "AAPL"}),
        ("POST", "/api/watchlist/remove", {"symbol": "NVDA"}),
        ("POST", "/api/watchlist/refresh", {"quotes": {"NVDA": quote}, "decisions": {"NVDA": "BUY"}}),
        ("POST", "/api/watchlist/refresh", {"quotes": {"NVDA": quote}}),  # the page's background refresh
        ("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "below", "value": "1.50"}),
        ("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "update", "rule_id": "r1", "value": "6"}),
        ("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "toggle", "rule_id": "r1", "enabled": False}),
        ("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "delete", "rule_id": "r1"}),
        ("POST", "/api/watchlist/poll", {"minutes": 5}),
        ("POST", "/api/watchlist/seen", {"upto_seq": 1}),
        ("POST", "/api/watchlist/seen", {}),
        ("POST", "/api/watchlist/kline", {"symbol": "NVDA"}),
        ("POST", "/api/watchlist/analyze", {"symbol": "NVDA"}),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("variant", sorted(BAD_ROWS))
def test_w264_a_bad_row_locks_the_file_on_every_endpoint_never_500(console, tmp_path, monkeypatch, variant):
    monkeypatch.setattr(watchlist, "_remind", lambda fired, root: None)
    raw = _bad_row(BAD_ROWS[variant])
    _write(tmp_path, raw)
    for method, path, body in _calls():
        status, answer = console(method, path, body)
        assert (status, answer.get("code")) == (409, "watchlist_corrupt"), (variant, method, path, status, answer)
        assert answer["error"].startswith("自选数据文件 watchlist.json 已损坏，没有按空列表读取，也不会写入。原文件已备份为 ")
        assert (tmp_path / "watchlist.json").read_bytes() == raw
        assert not (tmp_path / "watchlist.json.tmp").exists()
    backups = _backups(tmp_path)
    assert len(backups) == 1 and (tmp_path / backups[0]).read_bytes() == raw  # backed up once


@pytest.mark.unit
@pytest.mark.parametrize("variant", sorted(BAD_ROWS))
def test_w264_a_bad_row_is_refused_by_every_function_too(tmp_path, variant):
    raw = _bad_row(BAD_ROWS[variant])
    _write(tmp_path, raw)
    for call in (
        lambda: watchlist.list_watch(tmp_path),
        lambda: watchlist.add_symbol("AAPL", tmp_path),
        lambda: watchlist.remove_symbol("NVDA", tmp_path),
        lambda: watchlist.refresh({"NVDA": 6}, tmp_path, {}),
        lambda: watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": "7"}, tmp_path),
        lambda: watchlist.set_poll(1, tmp_path),
        lambda: watchlist.mark_seen(tmp_path, upto_seq=0),
        lambda: watchlist.open_kline("NVDA", tmp_path),
        lambda: watchlist.explicit_analyze("NVDA", tmp_path),
        lambda: watchlist.admit("NVDA", "rule|NVDA|r1#1", tmp_path),
    ):
        with pytest.raises(watchlist.WatchlistError) as caught:
            call()
        assert (caught.value.status, caught.value.code) == (409, "watchlist_corrupt")
    assert (tmp_path / "watchlist.json").read_bytes() == raw
    assert len(_backups(tmp_path)) == 1


@pytest.mark.unit
def test_w264_the_good_file_itself_is_accepted(tmp_path):
    _write(tmp_path, _bad_row(lambda d: None))
    view = watchlist.list_watch(tmp_path)
    assert [item["symbol"] for item in view["items"]] == ["NVDA"] and _backups(tmp_path) == []
    # Files from before rules have no rules key and still load with none.
    _write(tmp_path, _bad_row(lambda d: d["items"].append({"symbol": "AAPL"})))
    assert [item["rules"] for item in watchlist.list_watch(tmp_path)["items"]][1] == []


_ODD = [None, "s", 1, 1.5, True, [], {}, [1], {"a": 1}, -1, 0]


def _field_mutations():
    tables = {
        "items": ["symbol", "enabled", "last_price", "last_decision", "rules", "quote_at", "change_pct"],
        "triggers": ["id", "symbol", "kind", "seq", "created_at", "rule_id", "old_value", "new_value",
                     "rule_version", "started", "run_id"],
        "checks": ["trigger_id", "result", "started", "run_id"],
    }
    for table, keys in tables.items():
        for key in keys:
            for value in _ODD:
                yield lambda d, t=table, k=key, v=value: d[t][0].__setitem__(k, v)
    for key in ["id", "kind", "value", "value_text", "enabled", "state", "fired_at", "armed_at", "arm_seq",
                "fired_days"]:
        for value in _ODD:
            yield lambda d, k=key, v=value: d["items"][0]["rules"][0].__setitem__(k, v)
    for key in ["rule_version", "mode", "trigger_seq", "alerts_seen_seq", "alerts_seen_at", "poll_minutes"]:
        for value in _ODD:
            yield lambda d, k=key, v=value: d.__setitem__(k, v)


@pytest.mark.unit
def test_w264_odd_field_values_never_raise_anything_but_a_watchlist_error(tmp_path, monkeypatch):
    """Either the file is usable or it is refused with 409; nothing in between becomes a 500."""
    monkeypatch.setattr(watchlist, "_remind", lambda fired, root: None)
    calls = (
        lambda root: watchlist.list_watch(root),
        lambda root: watchlist.refresh({"NVDA": {"price": 6, "prev_close": 5, "trading_day": "2026-11-13",
                                                 "fetched_at": time.time() + 100}}, root, {"NVDA": "BUY"}),
        lambda root: watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": "r1", "value": "6"}, root),
        lambda root: watchlist.change_rule({"symbol": "NVDA", "op": "toggle", "rule_id": "r1"}, root),
        lambda root: watchlist.mark_seen(root, upto_seq=1),
        lambda root: watchlist.admit("NVDA", "rule|NVDA|r1#1", root),
    )
    for index, change in enumerate(_field_mutations()):
        root = tmp_path / str(index)
        root.mkdir()
        raw = _bad_row(change)
        for call in calls:
            _write(root, raw)
            try:
                call(root)
            except watchlist.WatchlistError as exc:
                if exc.code == "watchlist_corrupt":
                    assert (root / "watchlist.json").read_bytes() == raw


@pytest.mark.unit
def test_w266_a_leading_bom_is_read_and_never_written_back(tmp_path):
    body = json.dumps(_good_file(), ensure_ascii=False).encode("utf-8")
    _write(tmp_path, b"\xef\xbb\xbf" + body)
    view = watchlist.list_watch(tmp_path)
    assert [item["symbol"] for item in view["items"]] == ["NVDA"] and view["trigger_total"] == 1
    assert _backups(tmp_path) == []
    assert (tmp_path / "watchlist.json").read_bytes().startswith(b"\xef\xbb\xbf")  # a read does not rewrite
    watchlist.add_symbol("AAPL", tmp_path)
    saved = (tmp_path / "watchlist.json").read_bytes()
    assert not saved.startswith(b"\xef\xbb\xbf")
    data = json.loads(saved.decode("utf-8"))
    assert [item["symbol"] for item in data["items"]] == ["NVDA", "AAPL"] and len(data["triggers"]) == 1


@pytest.mark.unit
@pytest.mark.parametrize("raw", [b"\xef\xbb\xbf", b"\xef\xbb\xbf{broken", b"\xef\xbb\xbf\xef\xbb\xbf{}",
                                 b'\xef\xbb\xbf{"items": ["NVDA"]}'])
def test_w266_a_bom_does_not_hide_a_broken_file(tmp_path, raw):
    _write(tmp_path, raw)
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.list_watch(tmp_path)
    assert caught.value.code == "watchlist_corrupt" and (tmp_path / "watchlist.json").read_bytes() == raw


@pytest.mark.unit
def test_w266_api_reads_a_bom_file(console, tmp_path):
    _write(tmp_path, b"\xef\xbb\xbf" + json.dumps(_good_file()).encode("utf-8"))
    status, body = console("GET", "/api/watchlist")
    assert status == 200 and [item["symbol"] for item in body["items"]] == ["NVDA"]


@pytest.mark.unit
@pytest.mark.parametrize("good", [0, 1, 3])
def test_w267_whole_numbers_are_accepted(tmp_path, good):
    _write(tmp_path, json.dumps(_good_file()).encode("utf-8"))
    assert watchlist.mark_seen(tmp_path, upto_seq=good)["alerts_seen_seq"] == min(good, 1)


@pytest.mark.unit
def test_w267_api_refuses_booleans_floats_and_strings(console, tmp_path):
    for bad in (False, True, 0.0, 1.0, 1.5, "1", "0", -1):
        status, body = console("POST", "/api/watchlist/seen", {"upto_seq": bad})
        assert status == 400 and body["error"] == "upto_seq 须为不小于 0 的整数。", bad
    assert not (tmp_path / "watchlist.json").exists()  # a refused mark writes nothing
    status, body = console("POST", "/api/watchlist/seen", {"upto_seq": 0})
    assert status == 200


# Commit 8: W269/W270-1/W272 (ASCII decimals only), W268 (exact duplicates), W270-3 (text must match value)

NUMBER_TEXT = "只能填半角数字和小数点，例如 1452.30。"
NOT_ASCII = ["１２３", "１４５２.３０", "1452.３", "٣.٥", "۱۲", "१२३", "1_000", "1e-7", "1E3", "1e309", "2e12",
             ".5", "5.", "1.2.3", "0x10", "1,000", "12 3", "++5", 1e-07, 1e16]


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["above", "pct"])
@pytest.mark.parametrize("value", NOT_ASCII)
def test_c8_only_ascii_decimals_are_numbers(tmp_path, kind, value):
    watchlist.add_symbol("NVDA", tmp_path)
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": kind, "value": value}, tmp_path)
    assert (caught.value.status, caught.value.code, str(caught.value)) == (400, "bad_number", NUMBER_TEXT)
    assert _rules_of(watchlist.list_watch(tmp_path)) == []
    view = watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": kind, "value": "2"}, tmp_path)
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": _rules_of(view)[0]["id"], "value": value},
                              tmp_path)
    assert caught.value.code == "bad_number"
    assert _rules_of(watchlist.list_watch(tmp_path))[0]["value_text"] == "2"


@pytest.mark.unit
@pytest.mark.parametrize("value,text", [("1452.30", "1452.30"), (" +1452.30\t", "1452.30"), ("0190.50", "190.50"),
                                        ("\u30001452.3\u3000", "1452.3"), (190, "190"), (1452.3, "1452.3"),
                                        ("1000000000000", "1000000000000"),
                                        ("1000000000000.000000", "1000000000000.000000"), ("0.000001", "0.000001")])
def test_c8_ascii_decimals_are_accepted_and_value_comes_from_the_text(tmp_path, value, text):
    from decimal import Decimal
    watchlist.add_symbol("NVDA", tmp_path)
    watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": value}, tmp_path)
    stored = json.loads((tmp_path / "watchlist.json").read_text(encoding="utf-8"))["items"][0]["rules"][0]
    assert stored["value_text"] == text and stored["value"] == float(Decimal(text))


@pytest.mark.unit
@pytest.mark.parametrize("value,code", [("1000000000000.000001", "bad_price"), ("0", "bad_price"),
                                        ("0.000000", "bad_price"), ("1452.1234567", "bad_decimals")])
def test_c8_price_limits_are_checked_on_the_decimal(tmp_path, value, code):
    watchlist.add_symbol("NVDA", tmp_path)
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "below", "value": value}, tmp_path)
    assert caught.value.code == code


@pytest.mark.unit
@pytest.mark.parametrize("value,ok", [("0.1", True), ("50", True), ("50.000000", True), ("50.000001", False),
                                      ("0.099999", False)])
def test_c8_percent_limits_are_checked_on_the_decimal(tmp_path, value, ok):
    watchlist.add_symbol("NVDA", tmp_path)
    call = lambda: watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "pct", "value": value}, tmp_path)  # noqa: E731
    if ok:
        assert _rules_of(call())[0]["value_text"] == value
    else:
        with pytest.raises(watchlist.WatchlistError) as caught:
            call()
        assert caught.value.code == "bad_pct" and str(caught.value) == "涨跌幅填 0.1 到 50 之间。"


@pytest.mark.unit
def test_c8_api_refuses_full_width_digits_with_400(console):
    console("POST", "/api/watchlist", {"symbol": "NVDA"})
    status, body = console("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "above", "value": "１４５２.３０"})
    assert (status, body["code"], body["error"]) == (400, "bad_number", NUMBER_TEXT)
    status, body = console("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "above", "value": "1e-7"})
    assert (status, body["code"]) == (400, "bad_number")
    status, body = console("POST", "/api/watchlist/rules", {"symbol": "NVDA", "op": "add", "kind": "above",
                                                            "value": "1000000000000.000001"})
    assert (status, body["code"]) == (400, "bad_price")


@pytest.mark.unit
def test_c8_w268_duplicates_are_exactly_equal_decimals(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    view = watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": "1452.3"}, tmp_path)
    for same in ("1452.30", "1452.300000", "01452.3", 1452.3):
        with pytest.raises(watchlist.WatchlistError) as caught:
            watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": same}, tmp_path)
        assert caught.value.code == "duplicate" and str(caught.value) == "已有一条「高于 1452.3」，不用重复加。"
    view = watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": "1452.300001"}, tmp_path)
    view = watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "below", "value": "1452.3"}, tmp_path)
    assert [r["value_text"] for r in _rules_of(view)] == ["1452.3", "1452.300001", "1452.3"]  # another kind is not a duplicate
    second = _rules_of(view)[1]["id"]
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "update", "rule_id": second, "value": "1452.30"}, tmp_path)
    assert caught.value.code == "duplicate"


@pytest.mark.unit
def test_c8_w268_a_rule_saved_before_value_text_is_compared_by_its_number(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["rules"] = [{"id": "r1", "kind": "above", "value": 1452.3, "enabled": True, "state": "armed"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": "1452.30"}, tmp_path)
    assert caught.value.code == "duplicate"


def _file_with_rule(root, value, value_text):
    watchlist.add_symbol("NVDA", root)
    path = root / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["rules"] = [{"id": "r1", "kind": "above", "value": value, "value_text": value_text,
                                  "enabled": True, "state": "armed", "arm_seq": 1}]
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.unit
def test_c8_w270_3_a_value_text_that_is_not_the_value_is_ignored(tmp_path):
    _file_with_rule(tmp_path, 190.5, "999")
    rule = _rules_of(watchlist.list_watch(tmp_path))[0]
    assert (rule["label"], rule["chip"], rule["value_text"]) == ("高于 190.50", "高于 190.50", "190.5")
    # The duplicate check follows the shown number too.
    view = watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": "999"}, tmp_path)
    assert [r["label"] for r in _rules_of(view)] == ["高于 190.50", "高于 999.00"]
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.change_rule({"symbol": "NVDA", "op": "add", "kind": "above", "value": "190.50"}, tmp_path)
    assert caught.value.code == "duplicate"


@pytest.mark.unit
@pytest.mark.parametrize("value,text,shown", [(1452.3, "1452.30", "高于 1452.30"), (1452.3, "1452.31", "高于 1452.3"),
                                              (5, "5.000000", "高于 5.000000"), (5, "5.000001", "高于 5.00"),
                                              (0.5, "0.500000", "低于 0.500000"), (0.5, "0.51", "低于 0.5000")])
def test_c8_w270_3_matching_text_keeps_its_digits(tmp_path, value, text, shown):
    _file_with_rule(tmp_path, value, text)
    if shown.startswith("低于"):
        path = tmp_path / "watchlist.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["items"][0]["rules"][0]["kind"] = "below"
        path.write_text(json.dumps(data), encoding="utf-8")
    assert _rules_of(watchlist.list_watch(tmp_path))[0]["label"] == shown


@pytest.mark.unit
def test_c8_w270_3_a_trigger_names_the_threshold_that_was_compared(tmp_path, monkeypatch):
    monkeypatch.setattr(watchlist, "_remind", lambda fired, root: None)
    _file_with_rule(tmp_path, 5, "7")
    view = watchlist.refresh({"NVDA": _quote(6, time.time() + 120)}, tmp_path)
    assert view["fired"][0]["label"] == "高于 5.00" and view["triggers"][-1]["rule_value_text"] == "5"
