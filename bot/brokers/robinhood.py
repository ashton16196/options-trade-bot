"""Robinhood adapter, built on the UNOFFICIAL `robin_stocks` library.

Read this before using it — the situation as of mid-2026:
  * Robinhood has NO official public API for options (crypto only).
    robin_stocks drives the same private endpoints the app uses, and
    automated access is against Robinhood's terms — there is at least one
    documented case (robin_stocks issue #1604, Apr 2025) of an account
    frozen with Robinhood explicitly citing API trading.
  * Robinhood removed authenticator-app (TOTP) 2FA around Feb 2025 in
    favor of in-app device approvals. Practical consequence: the first
    login (and roughly every ~24h, when the cached session token expires)
    may require you to approve the login in the Robinhood mobile app or
    enter an SMS code. Run the bot interactively at least once so
    robin_stocks can cache its session, and expect to re-approve daily.
    A fully unattended Robinhood bot is not reliably possible anymore.
    ROBINHOOD_TOTP_SECRET is still honored for legacy accounts that had
    TOTP enabled before the removal.
  * robin_stocks return shapes drift between versions; everything here
    parses defensively and returns UNKNOWN rather than crashing.

If any of that is a dealbreaker, use the Alpaca adapter (broker: alpaca)
— official API, no login gymnastics, options paper trading included.
"""
from __future__ import annotations

import logging

from ..models import OptionContract, OptionType
from .base import Broker, OptionQuote, OrderResult, OrderState

log = logging.getLogger(__name__)

_STATE_MAP = {
    "queued": OrderState.SUBMITTED,
    "unconfirmed": OrderState.SUBMITTED,
    "confirmed": OrderState.SUBMITTED,
    "partially_filled": OrderState.SUBMITTED,
    "filled": OrderState.FILLED,
    "cancelled": OrderState.CANCELLED,
    "canceled": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
    "failed": OrderState.REJECTED,
}


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class RobinhoodBroker(Broker):
    name = "robinhood"

    def __init__(self, username: str, password: str, totp_secret: str = "") -> None:
        self._username = username
        self._password = password
        self._totp_secret = totp_secret
        self._rh = None

    def connect(self) -> None:
        try:
            import robin_stocks.robinhood as rh
        except ImportError as e:
            raise RuntimeError(
                "robin_stocks is not installed — pip install robin_stocks pyotp"
            ) from e
        mfa_code = None
        if self._totp_secret:
            import pyotp

            mfa_code = pyotp.TOTP(self._totp_secret).now()
        rh.login(self._username, self._password, mfa_code=mfa_code, store_session=True)
        self._rh = rh
        log.info("Robinhood login OK for %s", self._username)

    @property
    def rh(self):
        if self._rh is None:
            raise RuntimeError("RobinhoodBroker.connect() was never called")
        return self._rh

    # -- data -------------------------------------------------------------

    def get_quote(self, contract: OptionContract) -> OptionQuote | None:
        try:
            data = self.rh.get_option_market_data(
                contract.ticker,
                contract.expiration.isoformat(),
                str(contract.strike),
                contract.option_type.value,
            )
        except Exception:
            log.exception("quote lookup failed for %s", contract)
            return None
        entry = _first_dict(data)
        if not entry:
            return None
        bid = _f(entry.get("bid_price"))
        ask = _f(entry.get("ask_price"))
        mark = _f(entry.get("mark_price")) or (bid + ask) / 2
        return OptionQuote(bid=bid, ask=ask, mark=mark)

    # -- orders -----------------------------------------------------------

    def buy_to_open(self, contract, quantity, limit_price):
        return self._order("buy", contract, quantity, limit_price)

    def sell_to_close(self, contract, quantity, limit_price):
        return self._order("sell", contract, quantity, limit_price)

    def _order(self, side: str, contract: OptionContract, quantity: int,
               limit_price: float) -> OrderResult:
        kwargs = dict(
            positionEffect="open" if side == "buy" else "close",
            creditOrDebit="debit" if side == "buy" else "credit",
            price=round(max(limit_price, 0.01), 2),
            symbol=contract.ticker,
            quantity=quantity,
            expirationDate=contract.expiration.isoformat(),
            strike=contract.strike,
            optionType=contract.option_type.value,
            timeInForce="gfd",
        )
        try:
            if side == "buy":
                resp = self.rh.order_buy_option_limit(**kwargs)
            else:
                resp = self.rh.order_sell_option_limit(**kwargs)
        except Exception:
            log.exception("%s order failed for %s", side, contract)
            return OrderResult("", OrderState.REJECTED)
        if not isinstance(resp, dict) or "id" not in resp:
            log.error("unexpected order response for %s: %r", contract, resp)
            return OrderResult("", OrderState.REJECTED)
        return self._to_result(resp)

    def order_status(self, order_id: str) -> OrderResult:
        try:
            resp = self.rh.get_option_order_info(order_id)
        except Exception:
            log.exception("order status lookup failed for %s", order_id)
            return OrderResult(order_id, OrderState.UNKNOWN)
        if not isinstance(resp, dict):
            return OrderResult(order_id, OrderState.UNKNOWN)
        return self._to_result(resp, order_id=order_id)

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.rh.cancel_option_order(order_id)
            return True
        except Exception:
            log.exception("cancel failed for %s", order_id)
            return False

    @staticmethod
    def _to_result(resp: dict, order_id: str | None = None) -> OrderResult:
        state = _STATE_MAP.get(str(resp.get("state", "")).lower(), OrderState.UNKNOWN)
        filled = int(_f(resp.get("processed_quantity")))
        # Prefer actual fill economics; 'price' is just our limit price and
        # only serves as the last-resort fallback.
        avg = _f(resp.get("average_price"))
        if not avg and filled and resp.get("processed_premium"):
            avg = _f(resp["processed_premium"]) / (filled * 100)
        if not avg:
            avg = _f(resp.get("price"))
        return OrderResult(
            order_id=order_id or str(resp.get("id", "")),
            state=state,
            filled_quantity=filled,
            average_price=avg or None,
        )


def _first_dict(data) -> dict | None:
    """robin_stocks sometimes returns [dict], sometimes [[dict]], sometimes []."""
    while isinstance(data, list):
        if not data:
            return None
        data = data[0]
    return data if isinstance(data, dict) else None
