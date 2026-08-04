"""Broker interface. Adapters are synchronous (they do blocking HTTP);
callers run them in a thread via asyncio.to_thread."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from ..models import OptionContract


@dataclass
class OptionQuote:
    bid: float
    ask: float
    mark: float

    @property
    def mid(self) -> float:
        if self.mark > 0:
            return self.mark
        return (self.bid + self.ask) / 2


class OrderState(str, Enum):
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass
class OrderResult:
    order_id: str
    state: OrderState
    filled_quantity: int = 0
    average_price: float | None = None


class Broker(ABC):
    name: str = "broker"

    def connect(self) -> None:
        """Log in / warm up. Called once at startup, in a worker thread."""

    @abstractmethod
    def get_quote(self, contract: OptionContract) -> OptionQuote | None: ...

    @abstractmethod
    def buy_to_open(
        self, contract: OptionContract, quantity: int, limit_price: float
    ) -> OrderResult: ...

    @abstractmethod
    def sell_to_close(
        self, contract: OptionContract, quantity: int, limit_price: float
    ) -> OrderResult: ...

    @abstractmethod
    def order_status(self, order_id: str) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...
