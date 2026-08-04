"""Paper broker: simulates fills, places no real orders.

If given a quote_source (e.g. a connected Robinhood adapter used read-only),
fills are simulated against real bid/ask. Without one, fills happen at the
requested limit price and take-profit monitoring is inactive (no quotes).
"""
from __future__ import annotations

import itertools
import logging

from ..models import OptionContract
from .base import Broker, OptionQuote, OrderResult, OrderState

log = logging.getLogger(__name__)


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, quote_source: Broker | None = None) -> None:
        self.quote_source = quote_source
        self._ids = itertools.count(1)
        self._orders: dict[str, OrderResult] = {}

    def connect(self) -> None:
        if self.quote_source:
            self.quote_source.connect()

    def get_quote(self, contract: OptionContract) -> OptionQuote | None:
        if self.quote_source:
            return self.quote_source.get_quote(contract)
        return None

    def buy_to_open(self, contract, quantity, limit_price):
        quote = self.get_quote(contract)
        # A marketable limit fills at the ask (or better); cap at our limit.
        price = min(limit_price, quote.ask) if quote and quote.ask > 0 else limit_price
        return self._fill("buy", contract, quantity, price)

    def sell_to_close(self, contract, quantity, limit_price):
        quote = self.get_quote(contract)
        price = max(limit_price, quote.bid) if quote and quote.bid > 0 else limit_price
        return self._fill("sell", contract, quantity, price)

    def order_status(self, order_id: str) -> OrderResult:
        return self._orders.get(order_id, OrderResult(order_id, OrderState.UNKNOWN))

    def cancel_order(self, order_id: str) -> bool:
        return False  # paper orders fill instantly; nothing to cancel

    def _fill(self, side, contract, quantity, price) -> OrderResult:
        order_id = f"paper-{next(self._ids)}"
        result = OrderResult(order_id, OrderState.FILLED, quantity, round(price, 2))
        self._orders[order_id] = result
        log.info("[PAPER] %s %dx %s @ %.2f", side.upper(), quantity, contract, price)
        return result
