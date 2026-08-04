"""Turns approved signals into orders and tracks their fills."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .brokers.base import Broker, OrderState
from .config import Config
from .models import Action, Position, PositionStatus, Signal
from .risk import RiskManager
from .state import Store

log = logging.getLogger(__name__)


class TradeExecutor:
    def __init__(self, cfg: Config, broker: Broker, store: Store, risk: RiskManager) -> None:
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.risk = risk
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle_signal(self, sig: Signal) -> None:
        if sig.action is Action.OPEN:
            await self.open_from_signal(sig)
        else:
            await self.close_from_signal(sig)

    # -- entries ----------------------------------------------------------

    async def open_from_signal(self, sig: Signal) -> None:
        contract = sig.contract
        if contract is None:
            return
        quote = await asyncio.to_thread(self.broker.get_quote, contract)
        ok, reason = self.risk.check_entry(sig, quote)
        if not ok:
            log.warning("SKIP entry %s: %s", contract, reason)
            return

        t = self.cfg.trading
        if quote is not None and quote.ask > 0:
            limit = quote.ask * (1 + t.entry_limit_buffer_pct / 100)
        else:
            limit = sig.price  # paper without quotes; risk check ensured it exists
        limit = max(round(limit, 2), 0.01)
        qty = t.contracts_per_trade
        ok, reason = self.risk.check_premium(limit, qty)
        if not ok:
            log.warning("SKIP entry %s: %s", contract, reason)
            return

        log.info("ENTRY %dx %s limit %.2f (alert price %s)", qty, contract, limit, sig.price)
        result = await asyncio.to_thread(self.broker.buy_to_open, contract, qty, limit)
        if not result.order_id or result.state in (OrderState.REJECTED, OrderState.UNKNOWN):
            log.error("entry order failed for %s: %s", contract, result.state.value)
            return

        pos = self.store.insert_position(Position(
            contract=contract, quantity=qty, status=PositionStatus.PENDING,
            open_order_id=result.order_id, source_signal=sig.raw_text[:300],
        ))
        if result.state is OrderState.FILLED:
            self._mark_open(pos, result.average_price or limit, result.filled_quantity or qty)
        else:
            self._spawn(self._watch_entry_fill(pos, limit))

    def _mark_open(self, pos: Position, fill_price: float, filled_qty: int) -> None:
        pos.entry_price = fill_price
        pos.quantity = filled_qty
        pos.status = PositionStatus.OPEN
        pos.opened_at = datetime.now(timezone.utc)
        self.store.update_position(pos)
        log.info("OPENED %dx %s @ %.2f", pos.quantity, pos.contract, pos.entry_price)

    async def _watch_entry_fill(self, pos: Position, limit: float) -> None:
        timeout = self.cfg.trading.fill_timeout_seconds
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(2)
            waited += 2
            result = await asyncio.to_thread(self.broker.order_status, pos.open_order_id)
            if result.state is OrderState.FILLED:
                self._mark_open(pos, result.average_price or limit,
                                result.filled_quantity or pos.quantity)
                return
            if result.state in (OrderState.CANCELLED, OrderState.REJECTED):
                pos.status = PositionStatus.FAILED
                self.store.update_position(pos)
                log.warning("entry order %s for %s: %s", pos.open_order_id,
                            pos.contract, result.state.value)
                return
        log.warning("entry for %s unfilled after %ds — cancelling", pos.contract, timeout)
        await asyncio.to_thread(self.broker.cancel_order, pos.open_order_id)
        result = await asyncio.to_thread(self.broker.order_status, pos.open_order_id)
        if result.filled_quantity > 0:  # raced: filled (maybe partially) during cancel
            self._mark_open(pos, result.average_price or limit, result.filled_quantity)
        else:
            pos.status = PositionStatus.FAILED
            self.store.update_position(pos)
            log.info("entry for %s cancelled, no fill", pos.contract)

    # -- exits ------------------------------------------------------------

    async def close_from_signal(self, sig: Signal) -> None:
        targets = self._match_positions(sig)
        if not targets:
            log.info("close alert for %s but no matching open position", sig.ticker)
            return
        fraction = self.cfg.trading.trim_fraction if sig.action is Action.TRIM else 1.0
        for pos in targets:
            await self.close_position(
                pos, reason=f"group alert: {sig.raw_text[:80]}",
                fraction=fraction, price_hint=sig.price,
            )

    def _match_positions(self, sig: Signal) -> list[Position]:
        open_positions = self.store.open_positions()
        if sig.contract is not None:
            exact = [p for p in open_positions if p.contract == sig.contract]
            if exact:
                return exact
        by_ticker = [p for p in open_positions if p.contract.ticker == sig.ticker]
        if sig.option_type is not None:
            typed = [p for p in by_ticker if p.contract.option_type is sig.option_type]
            if typed:
                return typed
        return by_ticker

    async def close_position(
        self, pos: Position, reason: str, fraction: float = 1.0,
        price_hint: float | None = None,
    ) -> None:
        ok, why = self.risk.check_close()
        if not ok:
            log.warning("cannot close %s now: %s", pos.contract, why)
            return
        if not self.store.try_mark_closing(pos.id):
            return  # another path (TP monitor vs. alert) beat us to it
        qty = pos.quantity if fraction >= 1 else max(1, int(pos.quantity * fraction))
        t = self.cfg.trading

        for attempt, aggression in enumerate((1.0, 2.0), start=1):
            quote = await asyncio.to_thread(self.broker.get_quote, pos.contract)
            if quote is not None and quote.bid > 0:
                limit = quote.bid * (1 - aggression * t.close_limit_buffer_pct / 100)
            else:
                limit = price_hint or pos.entry_price or 0.01
            limit = max(round(limit, 2), 0.01)
            log.info("EXIT %dx %s limit %.2f — %s (attempt %d)",
                     qty, pos.contract, limit, reason, attempt)
            result = await asyncio.to_thread(
                self.broker.sell_to_close, pos.contract, qty, limit)
            if not result.order_id or result.state in (OrderState.REJECTED, OrderState.UNKNOWN):
                log.error("close order failed for %s (%s)", pos.contract, result.state.value)
                pos.status = PositionStatus.OPEN
                self.store.update_position(pos)
                return
            pos.close_order_id = result.order_id
            self.store.update_position(pos)
            if result.state is OrderState.FILLED:
                self._mark_closed(pos, qty, result.average_price or limit, reason)
                return
            filled = await self._await_close_fill(pos, qty, limit, reason)
            if filled:
                return
        # Two attempts failed; put it back so the monitor keeps trying.
        log.error("could not close %s after 2 attempts — will retry via monitor. "
                  "CHECK YOUR BROKER APP.", pos.contract)
        pos.status = PositionStatus.OPEN
        self.store.update_position(pos)

    async def _await_close_fill(self, pos: Position, qty: int, limit: float,
                                reason: str) -> bool:
        timeout = self.cfg.trading.fill_timeout_seconds
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(2)
            waited += 2
            result = await asyncio.to_thread(self.broker.order_status, pos.close_order_id)
            if result.state is OrderState.FILLED:
                self._mark_closed(pos, qty, result.average_price or limit, reason)
                return True
            if result.state in (OrderState.CANCELLED, OrderState.REJECTED):
                return False
        await asyncio.to_thread(self.broker.cancel_order, pos.close_order_id)
        result = await asyncio.to_thread(self.broker.order_status, pos.close_order_id)
        if result.filled_quantity > 0:
            self._mark_closed(pos, result.filled_quantity,
                              result.average_price or limit, reason)
            return result.filled_quantity >= qty
        return False

    def _mark_closed(self, pos: Position, qty_sold: int, price: float, reason: str) -> None:
        entry = pos.entry_price or price
        realized = (price - entry) * 100 * qty_sold
        self.store.record_realized_pnl(realized, note=f"{pos.contract}: {reason}")
        if qty_sold >= pos.quantity:
            pos.status = PositionStatus.CLOSED
            pos.closed_at = datetime.now(timezone.utc)
            pos.close_price = price
            log.info("CLOSED %dx %s @ %.2f | P&L %+.2f | %s",
                     qty_sold, pos.contract, price, realized, reason)
        else:
            pos.quantity -= qty_sold
            pos.status = PositionStatus.OPEN
            log.info("TRIMMED %dx %s @ %.2f (%d left) | P&L %+.2f | %s",
                     qty_sold, pos.contract, price, pos.quantity, realized, reason)
        self.store.update_position(pos)
