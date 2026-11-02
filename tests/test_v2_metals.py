"""V2-METALS: precious metals and commodity futures get their own asset type.
No model calls, no network, no .env."""
from __future__ import annotations

from pathlib import Path

import pytest

from cli.main import app  # noqa: F401  (the CLI module imports cleanly)
from cli.models import AnalystType, AssetType
from cli.prompts import detect_asset_type, filter_analysts_for_asset_type, parse_analysts
from tests.test_run003b import home as home, sleepers as sleepers  # noqa: F401  (fixtures)
from tradingagents.agents.context import build_instrument_context
from tradingagents.dataflows.symbols import normalize_symbol

pytestmark = pytest.mark.unit
LLM = {"llm_provider": "chatgpt", "quick_think_llm": "gpt-6-luna", "deep_think_llm": "gpt-6-sol"}

METALS = {"XAUUSD": "GC=F", "XAGUSD": "SI=F", "XAU": "GC=F", "XAG": "SI=F", "GOLD": "GC=F", "SILVER": "SI=F",
          "XPTUSD": "PL=F", "XPT": "PL=F", "PLATINUM": "PL=F", "XPDUSD": "PA=F", "XPD": "PA=F",
          "PALLADIUM": "PA=F", "xauusd+": "GC=F", " xagusd ": "SI=F"}


@pytest.mark.parametrize(("raw", "yahoo"), sorted(METALS.items()))
def test_metal_aliases_resolve_to_futures(raw, yahoo):
    assert normalize_symbol(raw) == yahoo
    assert detect_asset_type(raw) is AssetType.COMMODITY


@pytest.mark.parametrize("ticker", ["GC=F", "SI=F", "PL=F", "PA=F", "HG=F", "CL=F", "NG=F", "USOIL", "BRENT"])
def test_futures_symbols_are_commodities(ticker):
    assert detect_asset_type(ticker) is AssetType.COMMODITY


@pytest.mark.parametrize(("ticker", "kind"), [("NVDA", "stock"), ("AAPL", "stock"), ("600519.SS", "stock"),
                                              ("0700.HK", "stock"), ("BRK.B", "stock"), ("^GSPC", "stock"),
                                              ("EURUSD", "stock"), ("BTC-USD", "crypto"), ("BTCUSDT", "crypto"),
                                              ("ETH-USD", "crypto")])
def test_stock_and_crypto_detection_unchanged(ticker, kind):
    assert detect_asset_type(ticker).value == kind


@pytest.mark.parametrize("ticker", ["XAUUSD", "XAGUSD"])
def test_commodity_drops_the_fundamentals_analyst(ticker):
    kept = filter_analysts_for_asset_type(list(AnalystType), detect_asset_type(ticker))
    assert AnalystType.FUNDAMENTALS not in kept and kept
    assert AnalystType.FUNDAMENTALS in filter_analysts_for_asset_type(list(AnalystType), AssetType.STOCK)
    with pytest.raises(ValueError):
        parse_analysts("market,fundamentals", AssetType.COMMODITY)


@pytest.mark.parametrize("ticker", ["XAUUSD", "XAGUSD"])
def test_instrument_context_says_commodity_not_company(ticker):
    text = build_instrument_context(ticker, "commodity", {"name": "Gold Dec 26", "sector": "X", "industry": "Y"})
    assert "commodity futures contract" in text and "no company fundamentals" in text
    assert "Company:" not in text and "Name: Gold Dec 26" in text
    assert "contango" in text
    stock = build_instrument_context("NVDA", "stock", {"name": "NVIDIA"})
    assert "Company: NVIDIA" in stock and "commodity" not in stock


@pytest.mark.parametrize(("ticker", "yahoo"), [("XAUUSD", "GC=F"), ("XAGUSD", "SI=F")])
def test_webui_asset_options_for_metals(home, ticker, yahoo):
    from webui import service
    out = service.asset_options(ticker)
    assert out["asset_type"] == "commodity" and out["fundamentals_hidden"] is True
    assert "fundamentals" not in out["analysts"] and "market" in out["analysts"]
    assert normalize_symbol(out["ticker"]) == yahoo


def test_webui_asset_options_stock_and_crypto_unchanged(home):
    from webui import service
    assert service.asset_options("NVDA")["fundamentals_hidden"] is False
    assert service.asset_options("BTC-USD")["asset_type"] == "crypto"


def test_webui_page_knows_the_commodity_label():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'commodity: "贵金属/商品期货"' in script
    assert 'asset_type === "commodity"' in script
    assert '{value: "commodity", zh: "贵金属/商品期货"}' in script


def test_cli_accepts_the_commodity_asset_type():
    assert AssetType("commodity") is AssetType.COMMODITY


@pytest.mark.parametrize("ticker", ["XAUUSD", "XAGUSD"])
def test_webui_start_path_records_commodity(sleepers, home, ticker):
    """Real start path with a sleeping stand-in worker (no model, no network)."""
    from webui import service
    out = service._validate_analysis({**LLM, "ticker": ticker, "date": "2026-10-30", "analysts": ["market", "news"]})
    sel = out.get("selections", out)
    assert sel["asset_type"] == "commodity"
    record = service.start_validated_run(sel)
    assert record["selections"]["asset_type"] == "commodity"
    assert "fundamentals" not in record["selections"]["analysts"]


def test_webui_refuses_fundamentals_for_a_metal(home):
    from webui import service
    service._validate_analysis({**LLM, "ticker": "NVDA", "date": "2026-10-30", "analysts": ["fundamentals"]})
    with pytest.raises(Exception, match="fundamentals"):
        service._validate_analysis({**LLM, "ticker": "XAUUSD", "date": "2026-10-30", "analysts": ["fundamentals"]})
