"""The caller's book, as the decision agents see it.

Optional input to a run: what is held, at what average price, and how much cash
is free. Without it the agents cannot tell adding to a full position from
opening a new one. Three states are distinct and must stay so: a position, a
flat book, and no context at all, since treating "not provided" as "flat" would
invent a fact about the caller's account.

Broker-neutral by construction: quantities are generic units and the currency is
whatever label the caller passes, so nothing here implies a venue or an
execution path.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.redaction import _INVISIBLE


class Position(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    ticker: str = Field(description="Instrument symbol, e.g. AAPL")
    quantity: float | None = Field(default=None, description="Signed units held; negative is short; None when only the cost is known")
    average_price: float | None = Field(default=None, description="Average entry price per unit, greater than 0; optional in an imported holdings JSON")

    # An imported holdings JSON is read as before WEBUI-006 (W50): a position names
    # its size, its cost, or both. The WebUI step-1 form and the CLI --portfolio
    # file are stricter and need the cost (CostedPosition below, W41/W42).
    # Numbers must be JSON numbers: true/false and numeric strings are refused.

    @field_validator("ticker")
    @classmethod
    def _named(cls, value):
        # W77: an empty or blank ticker names no instrument (the drawer refuses it too).
        # W83: nor does one made only of zero-width / format characters (U+200B, BOM).
        # W114: the same invisible set as the page (U+3164, FE0F, 034F, 115F, 17B4, ...).
        if not _INVISIBLE.sub("", "".join(ch for ch in value if unicodedata.category(ch) != "Cf")).strip():
            raise ValueError("ticker must not be empty")
        return value

    @field_validator("quantity", "average_price", mode="before")
    @classmethod
    def _plain_number(cls, value, info):
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{info.field_name} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be a finite number")
        return value

    @field_validator("average_price")
    @classmethod
    def _positive_price(cls, value):
        if value is not None and value <= 0:
            raise ValueError("average_price must be greater than 0")
        return value

    @field_validator("quantity")
    @classmethod
    def _nonzero_quantity(cls, value):
        if value is not None and value == 0:
            raise ValueError("quantity must not be 0 (leave it out when the size is not known; negative is short)")
        return value

    @model_validator(mode="after")
    def _size_or_cost(self):
        if self.quantity is None and self.average_price is None:
            raise ValueError("a position needs its quantity or its average_price")
        return self


class CostedPosition(Position):
    """A holding from the WebUI step-1 form or the CLI --portfolio file: the cost is required."""

    average_price: float = Field(description="Average entry price per unit; required, greater than 0")


class PortfolioContext(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    cash: float | None = Field(default=None, description="Free cash available")
    currency: str | None = Field(default=None, description="Currency label for cash and prices")
    positions: list[Position] = Field(default_factory=list)

    @field_validator("cash", mode="before")
    @classmethod
    def _plain_cash(cls, value):
        # W51: cash is a JSON number, never true/false or a numeric string, and
        # never negative (free cash; a margin debt is not modelled here).
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("cash must be a number")
        if not math.isfinite(value):
            raise ValueError("cash must be a finite number")
        if value < 0:
            raise ValueError("cash must not be negative")
        return value

    def positions_in(self, ticker: str) -> list[Position]:
        """The holdings of the instrument the run prices for ``ticker``, under any
        broker spelling (BTCUSDT, 700.HK). Holdings keep the spelling they were
        given: a symbol can also name another instrument (WTI the stock)."""
        symbol = normalize_symbol(ticker)
        return [p for p in self.positions if normalize_symbol(p.ticker) == symbol]

    def position_in(self, ticker: str) -> Position | None:
        held = self.positions_in(ticker)
        return held[0] if held else None

    def render(self, ticker: str) -> str:
        """The portfolio block for the decision agents, led by the analyzed instrument."""
        symbol = normalize_symbol(ticker)
        held = self.positions_in(symbol)
        lines = []
        for position in held:
            price = f", average price {position.average_price:,.2f}" if position.average_price is not None else ""
            spelled = position.ticker.strip().upper()
            listed = f" (listed as {position.ticker})" if spelled != symbol else ""
            size = f"{position.quantity:,.4g} units" if position.quantity is not None else "held (size not given)"
            lines.append(f"- Current position in {symbol}: {size}{price}{listed}")
        if not held:
            lines.append(f"- No current position in {symbol}")
        if self.cash is not None:
            lines.append(f"- Cash available: {self.cash:,.2f}{' ' + self.currency if self.currency else ''}")
        others = [p for p in self.positions if all(p is not h for h in held)]
        if others:
            lines.append("- Other positions: " + ", ".join(f"{p.ticker.upper()} {p.quantity:,.4g}" if p.quantity is not None else f"{p.ticker.upper()} (size not given)" for p in others))
        return "Portfolio at the analysis date:\n" + "\n".join(lines)

    def fingerprint(self) -> str:
        """Stable digest of the book, so a changed one cannot resume a stale run."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]



class CostedPortfolioContext(PortfolioContext):
    """The same book, but every position needs its cost (WebUI step-1 form, CLI --portfolio)."""

    positions: list[CostedPosition] = Field(default_factory=list)


def portfolio_unusable_sentence(path, exc) -> str:
    return f"portfolio file {path} is not usable: {exc}"


def load_portfolio(path: str | Path) -> PortfolioContext:
    """Read a CLI --portfolio JSON file, failing before the run rather than mid-graph.

    The CLI matches the WebUI step-1 form: each position needs its cost (W42)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return CostedPortfolioContext.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(portfolio_unusable_sentence(path, exc)) from exc
