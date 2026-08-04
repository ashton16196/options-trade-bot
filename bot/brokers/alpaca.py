"""Alpaca adapter — the recommended broker for this bot.

Why: official, supported API (unlike Robinhood); options enabled by default
in the paper environment, so the whole pipeline can be tested with realistic
simulated fills before any real money moves; commission-free options; solid
official SDK (alpaca-py). Live options trading needs a quick in-dashboard
application (Account → Configure) before real orders are accepted.

Data note: the free plan's option quotes are a real-time "indicative" feed —
fine for TP monitoring and sanity checks. Full OPRA NBBO is Alpaca's paid
Algo Trader Plus add-on.

Keys: https://app.alpaca.markets → paper keys work instantly.
"""
from __future__ import annotations

import logging

from ..models import OptionContract
from .base import Broker, OptionQuote, OrderResult, OrderState

log = logging.getLogger(__name__)

_STATE_MAP = {
    "new": OrderState.SUBMITTED,
    "accepted": OrderState.SUBMITTED,
    "pending_new": OrderState.SUBMITTED,
    "pending_cancel": OrderState.SUBMITTED,
    "partially_filled": OrderState.SUBMITTED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELLED,
    "expired": OrderState.CANCELLED,
    "done_for_day": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
}


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, paper: bool = True) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self.paper = paper
        self._trading = None
        self._data = None

    def connect(self) -> None:
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.trading.client import TradingClient
        except ImportError as e:
            raise RuntimeError("alpaca-py is not installed — pip install alpaca-py") from e
        self._trading = TradingClient(self._api_key, self._secret_key, paper=self.paper)
        self._data = OptionHistoricalDataClient(self._api_key, self._secret_key)
        account = self._trading.get_account()
        log.info("Alpaca %s account connected (buying power $%s)",
                 "PAPER" if self.paper else "LIVE", account.buying_power)

    def get_quote(self, contract: OptionContract) -> OptionQuote | None:
        from alpaca.data.requests import OptionLatestQuoteRequest

        occ = contract.occ_symbol()
        try:
            quotes = self._data.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=occ))
        except Exception:
            log.exception("quote lookup failed for %s", contract)
            return None
        q = quotes.get(occ) if hasattr(quotes, "get") else None
        if q is None:
            return None
        bid = float(q.bid_price or 0)
        ask = float(q.ask_price or 0)
        mark = (bid + ask) / 2 if bid and ask else max(bid, ask)
        return OptionQuote(bid=bid, ask=ask, mark=mark)

    def buy_to_open(self, contract, quantity, limit_price):
        return self._order("buy", contract, quantity, limit_price)

    def sell_to_close(self, contract, quantity, limit_price):
        return self._order("sell", contract, quantity, limit_price)

    def _order(self, side: str, contract: OptionContract, quantity: int,
               limit_price: float) -> OrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        req = LimitOrderRequest(
            symbol=contract.occ_symbol(),
            qty=quantity,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,  # options only support DAY on Alpaca
            limit_price=round(max(limit_price, 0.01), 2),
        )
        try:
            order = self._trading.submit_order(req)
        except Exception:
            log.exception("%s order failed for %s", side, contract)
            return OrderResult("", OrderState.REJECTED)
        return self._to_result(order)

    def order_status(self, order_id: str) -> OrderResult:
        try:
            order = self._trading.get_order_by_id(order_id)
        except Exception:
            log.exception("order status lookup failed for %s", order_id)
            return OrderResult(order_id, OrderState.UNKNOWN)
        return self._to_result(order)

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._trading.cancel_order_by_id(order_id)
            return True
        except Exception:
            log.exception("cancel failed for %s", order_id)
            return False

    @staticmethod
    def _to_result(order) -> OrderResult:
        raw_status = getattr(order.status, "value", str(order.status)).lower()
        return OrderResult(
            order_id=str(order.id),
            state=_STATE_MAP.get(raw_status, OrderState.UNKNOWN),
            filled_quantity=int(float(order.filled_qty or 0)),
            average_price=float(order.filled_avg_price) if order.filled_avg_price else None,
        )
