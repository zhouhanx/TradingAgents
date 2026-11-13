"""V2-REPORT-COMPARE end to end: two finished runs through the signed-in console.

The runs are written by tests/v2_compare_fixtures.py from the real result payload
(``show`` off). Nothing calls a model or leaves 127.0.0.1.
"""

import http.client
import json
import socket
import threading

import pytest

from tests.v2_compare_fixtures import LEFT, RIGHT, build
from webui import server

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo


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
    httpd = server.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    # tests/conftest.py signs the console in: its cookie rides on every request,
    # and the page token is httpd.session_token.

    def get(path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", path, headers={server.TOKEN_HEADER: httpd.session_token})
        response = connection.getresponse()
        data = response.read().decode("utf-8", "replace")
        connection.close()
        return response.status, json.loads(data or "{}")

    yield {"home": home, "get": get}
    httpd.shutdown()
    httpd.server_close()


def test_two_finished_runs_compare_and_replay_across_pages(console):
    seeded = build(console["home"])
    status, listing = console["get"]("/api/compare")
    assert status == 200
    assert {row["id"] for row in listing["runs"]} == {LEFT, RIGHT}

    status, pair = console["get"](f"/api/compare?left={LEFT}&right={RIGHT}")
    assert status == 200
    left, right = pair["left"], pair["right"]
    assert (left["rating_label"], right["rating_label"]) == ("评级 Buy", "评级 Hold")
    assert left["evidence"] == ["ATR 4.2", "成交量放大 35%"] and right["evidence"] == ["PE 位于五年中位"]
    for card, ticker in ((left, "NVDA"), (right, "600519")):
        headings = [section["heading"] for section in card["sections"]]
        assert headings[0] == "I. Analyst Team Reports" and len(headings) >= 3
        assert ticker in card["sections"][0]["text"]
        assert {prompt["id"] for prompt in card["prompts"]} >= {"market_analyst", "trader", "portfolio_manager"}
    assert pair["same_run"] is False and pair["same_ticker"] is False

    status, first = console["get"](f"/api/compare/replay?id={LEFT}")
    assert status == 200 and first["page"] == 1 and first["pages"] >= 2
    assert first["total_steps"] == seeded["left_steps"]
    collected = list(first["steps"])
    for page in range(2, first["pages"] + 1):
        status, found = console["get"](f"/api/compare/replay?id={LEFT}&page={page}")
        assert status == 200 and found["page"] == page
        collected.extend(found["steps"])
    assert len(collected) == seeded["left_steps"]
    assert collected[0]["agent"] == "Market Analyst" and collected[-1]["agent"] == "Portfolio Manager"
    assert first["uses_saved_prompts"] is True
    assert all(prompt["changed"] is False for prompt in first["prompts"])

    status, missing = console["get"](f"/api/compare/replay?id={LEFT}&page={first['pages'] + 1}")
    assert status == 404 and missing.get("error")
