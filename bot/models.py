"""Core data types shared across the bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"

    @classmethod
    def from_text(cls, text: str) -> "OptionType":
        return cls.CALL if text.lower().startswith("c") else cls.PUT


class Action(str, Enum):
    OPEN = "open"    # buy to open
    CLOSE = "close"  # sell to close, full size
    TRIM = "trim"    # sell part of the position


@dataclass(frozen=True)
class OptionContract:
    ticker: str
    strike: float
    option_type: OptionType
    expiration: date

    def __str__(self) -> str:
        letter = "C" if self.option_type is OptionType.CALL else "P"
        return f"{self.ticker} {self.strike:g}{letter} {self.expiration.isoformat()}"

    def occ_symbol(self) -> str:
        """OCC contract symbol, e.g. AAPL240119C00100000 — the format every
        official broker API (Alpaca, Tradier, tastytrade, ...) accepts."""
        letter = "C" if self.option_type is OptionType.CALL else "P"
        return (f"{self.ticker}{self.expiration.strftime('%y%m%d')}"
                f"{letter}{int(round(self.strike * 1000)):08d}")


@dataclass
class Signal:
    """A parsed alert message."""

    action: Action
    raw_text: str
    received_at: datetime
    ticker: str | None = None
    contract: OptionContract | None = None
    price: float | None = None
    # Strike and type survive even when the expiration couldn't be resolved
    # (e.g. "STC SPY 640C @ .95") so closes can target the right position.
    strike: float | None = None
    option_type: OptionType | None = None
    source: str = ""
    message_id: str | None = None
    notes: list[str] = field(default_factory=list)


class PositionStatus(str, Enum):
    PENDING = "pending"  # entry order placed, waiting on fill
    OPEN = "open"
    CLOSING = "closing"  # exit order in flight
    CLOSED = "closed"
    FAILED = "failed"    # entry order never filled


@dataclass
class Position:
    contract: OptionContract
    quantity: int
    status: PositionStatus
    id: int | None = None
    entry_price: float | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    close_price: float | None = None
    open_order_id: str | None = None
    close_order_id: str | None = None
    source_signal: str = ""
    # A close alert arrived while this position couldn't be closed (entry
    # still pending, another close in flight, or market closed). The
    # monitor honors this flag on its next eligible tick.
    close_requested: bool = False
