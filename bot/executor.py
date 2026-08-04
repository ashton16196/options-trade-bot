"""Turns approved signals into orders and tracks them to terminal state.

Core invariant: once an order is submitted, a coroutine keeps polling it
until the broker reports FILLED / CANCELLED / REJECTED — never a single
post-cancel peek (broker cancels are asynchronous and can race a fill).
All orders use day time-in-force, so a terminal state always arrives.
Partial fills are always booked: an entry that fills 1 of 2 becomes a
1-lot OPEN position; a close that sells 1 of 2 books the P&L and retries
the remainder.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .brokers.base import Broker, OrderResult, OrderState
from .config import Config
from .models import Action, Position, PositionStatus, Signal
from .risk import RiskManager
from .state import Store

log = logging.getLogger(__name__)

_TERMINAL = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}


class TradeExecutor:
    def __init__(self, cfg: Config, broker: Broker, store: Store, risk: RiskManager) -> None:
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.risk = risk
        self._tasks: set[asyncio.Task] = set()
        # Serializes check-and-submit for entries so two near-simultaneous
        # alerts can't both pass the duplicate/max-position/loss-cap checks.
        self._entry_lock = asyncio.Lock()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle_signal(self, sig: Signal) -> None:
        if sig.action is Action.OPEN:
            await self.open_from_signal(sig)
        else:
            await self.close_from_signal(sig)

    # -- startup ----------------------------------------------------------

    async def reconcile(self) -> None:
        """Re-attach to orders left in flight by a previous process, so a
        restart can never strand a PENDING/CLOSING position."""
        for pos in self.store.positions_with_status(PositionStatus.PENDING):
            if not pos.open_order_id:
                self.store.update_status(pos.id, PositionStatus.FAILED)
                continue
            result = await asyncio.to_thread(self.broker.order_status, pos.open_order_id)
            if result.filled_quantity > 0 or result.state is OrderState.FILLED:
                self._mark_open(pos, result.average_price or pos.entry_price or 0.01,
                                result.filled_quantity or pos.quantity)
            elif result.state in (OrderState.CANCELLED, OrderState.REJECTED):
                self.store.update_status(pos.id, PositionStatus.FAILED)
            else:
                log.info("reconcile: resuming entry watch for %s", pos.contract)
                self._spawn(self._watch_entry_fill(pos, pos.entry_price or 0.01, result))
        for pos in self.store.positions_with_status(PositionStatus.CLOSING):
            log.info("reconcile: position %s was mid-close; resuming", pos.contract)
            self._spawn(self._resume_close(pos))

    async def _resume_close(self, pos: Position) -> None:
        if pos.close_order_id:
            final = await self._settle_order(pos.close_order_id, None,
                                             self.cfg.trading.fill_timeout_seconds)
            if final.filled_quantity > 0:
                self._book_close_fill(pos, min(final.filled_quantity, pos.quantity),
                                      final.average_price or pos.entry_price or 0.01,
                                      "reconciled after restart")
        self._finish_close(pos, requeue=pos.quantity > 0)

    # -- entries ----------------------------------------------------------

    async def open_from_signal(self, sig: Signal) -> None:
        contract = sig.contract
        if contract is None:
            return
        quote = await asyncio.to_thread(self.broker.get_quote, contract)
        async with self._entry_lock:
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

            log.info("ENTRY %dx %s limit %.2f (alert price %s)",
                     qty, contract, limit, sig.price)
            result = await asyncio.to_thread(self.broker.buy_to_open, contract, qty, limit)
            if not result.order_id or result.state in (OrderState.REJECTED, OrderState.UNKNOWN):
                log.error("entry order failed for %s: %s", contract, result.state.value)
                return
            # entry_price holds our limit until the real fill price arrives
            pos = self.store.insert_position(Position(
                contract=contract, quantity=qty, status=PositionStatus.PENDING,
                entry_price=limit, open_order_id=result.order_id,
                source_signal=sig.raw_text[:300],
            ))
        if result.state is OrderState.FILLED:
            self._mark_open(pos, result.average_price or limit,
                            result.filled_quantity or qty)
        else:
            self._spawn(self._watch_entry_fill(pos, limit, result))

    def _mark_open(self, pos: Position, fill_price: float, filled_qty: int) -> None:
        pos.entry_price = fill_price
        pos.quantity = filled_qty
        pos.status = PositionStatus.OPEN
        pos.opened_at = datetime.now(timezone.utc)
        self.store.update_position(pos)
        log.info("OPENED %dx %s @ %.2f", pos.quantity, pos.contract, pos.entry_price)
        fresh = self.store.get_position(pos.id)
        if fresh is not None and fresh.close_requested:
            log.info("a close was requested while the entry was pending — closing %s",
                     fresh.contract)
            self._spawn(self.close_position(fresh, reason="queued close alert"))

    async def _watch_entry_fill(self, pos: Position, limit: float,
                                first_result: OrderResult) -> None:
        final = await self._settle_order(pos.open_order_id, first_result,
                                         self.cfg.trading.fill_timeout_seconds)
        if final.filled_quantity > 0 or final.state is OrderState.FILLED:
            filled = final.filled_quantity or pos.quantity
            if filled < pos.quantity:
                log.warning("entry for %s partially filled: %d of %d",
                            pos.contract, filled, pos.quantity)
            self._mark_open(pos, final.average_price or limit, filled)
        else:
            self.store.update_status(pos.id, PositionStatus.FAILED)
            log.info("entry for %s ended %s with no fill",
                     pos.contract, final.state.value)

    # -- order settling ----------------------------------------------------

    async def _settle_order(self, order_id: str, first_result: OrderResult | None,
                            timeout: float) -> OrderResult:
        """Return only once the order is terminal. Waits up to `timeout` for
        a fill, then requests a cancel and KEEPS POLLING until the broker
        says what actually happened — a cancel can race a fill, and acting
        on a guess is how positions get double-sold or orphaned."""
        if first_result is not None and first_result.state in _TERMINAL:
            return first_result
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(2)
            waited += 2
            result = await asyncio.to_thread(self.broker.order_status, order_id)
            if result.state in _TERMINAL:
                return result
        cancelled = await asyncio.to_thread(self.broker.cancel_order, order_id)
        if not cancelled:
            log.warning("cancel request for order %s failed — polling until terminal",
                        order_id)
        while True:
            result = await asyncio.to_thread(self.broker.order_status, order_id)
            if result.state in _TERMINAL:
                return result
            log.warning("order %s not terminal yet (%s) — waiting",
                        order_id, result.state.value)
            await asyncio.sleep(10)

    # -- exits ------------------------------------------------------------

    async def close_from_signal(self, sig: Signal) -> None:
        # Group scalped in and out before our entry even filled → cancel the
        # entry and remember the exit intent for any contracts already bought.
        for pending in self._filter(sig, self.store.positions_with_status(PositionStatus.PENDING)):
            log.info("close alert while entry for %s still pending — cancelling entry",
                     pending.contract)
            self.store.set_close_requested(pending.id)
            await asyncio.to_thread(self.broker.cancel_order, pending.open_order_id)
        if sig.action is not Action.TRIM:
            # A full close while a trim is in flight: close the remainder
            # once the in-flight order settles.
            for closing in self._filter(sig, self.store.positions_with_status(PositionStatus.CLOSING)):
                self.store.set_close_requested(closing.id)

        targets = self._filter(sig, self.store.open_positions())
        if not targets:
            log.info("close alert for %s matched no open position", sig.ticker)
            return
        fraction = self.cfg.trading.trim_fraction if sig.action is Action.TRIM else 1.0
        for pos in targets:
            await self.close_position(
                pos, reason=f"group alert: {sig.raw_text[:80]}",
                fraction=fraction, price_hint=sig.price,
            )

    def _filter(self, sig: Signal, positions: list[Position]) -> list[Position]:
        """Positions a close signal targets. Stated constraints are HARD:
        an alert naming puts must never close calls, and a stated strike
        only closes that strike. Loose alerts ("all out $COIN") match by
        ticker alone."""
        if sig.contract is not None:
            exact = [p for p in positions if p.contract == sig.contract]
            if exact:
                return exact
        candidates = [p for p in positions if p.contract.ticker == sig.ticker]
        if sig.option_type is not None:
            candidates = [p for p in candidates
                          if p.contract.option_type is sig.option_type]
        strike = sig.strike if sig.strike is not None else (
            sig.contract.strike if sig.contract else None)
        if strike is not None:
            candidates = [p for p in candidates if p.contract.strike == strike]
        return candidates

    async def close_position(
        self, pos: Position, reason: str, fraction: float = 1.0,
        price_hint: float | None = None,
    ) -> None:
        ok, why = self.risk.check_close()
        if not ok:
            log.warning("cannot close %s now (%s) — queued for next eligible tick",
                        pos.contract, why)
            self.store.set_close_requested(pos.id)
            return
        if not self.store.try_mark_closing(pos.id):
            return  # another path (TP monitor vs. alert) beat us to it
        # Our snapshot may be stale (a trim may have landed since it was
        # read) — re-read now that we hold the CLOSING lock.
        pos = self.store.get_position(pos.id)
        if pos is None or pos.quantity <= 0:
            return
        qty_target = pos.quantity if fraction >= 1 else max(1, int(pos.quantity * fraction))
        remaining = qty_target
        t = self.cfg.trading

        for attempt, aggression in enumerate((1.0, 2.0), start=1):
            quote = await asyncio.to_thread(self.broker.get_quote, pos.contract)
            if quote is not None and quote.bid > 0:
                limit = quote.bid * (1 - aggression * t.close_limit_buffer_pct / 100)
            else:
                limit = price_hint or pos.entry_price or 0.01
            limit = max(round(limit, 2), 0.01)
            log.info("EXIT %dx %s limit %.2f — %s (attempt %d)",
                     remaining, pos.contract, limit, reason, attempt)
            result = await asyncio.to_thread(
                self.broker.sell_to_close, pos.contract, remaining, limit)
            if not result.order_id or result.state in (OrderState.REJECTED, OrderState.UNKNOWN):
                log.error("close order rejected for %s (%s)",
                          pos.contract, result.state.value)
                break
            pos.close_order_id = result.order_id
            self.store.update_position(pos)
            final = await self._settle_order(result.order_id, result,
                                             t.fill_timeout_seconds)
            if final.filled_quantity > 0:
                sold = min(final.filled_quantity, pos.quantity)
                self._book_close_fill(pos, sold, final.average_price or limit, reason)
                remaining -= sold
            if remaining <= 0:
                self._finish_close(pos)
                return

        log.error("could not fully close %s (%d contract(s) left) — will keep "
                  "retrying via the monitor. CHECK YOUR BROKER APP.",
                  pos.contract, remaining)
        self._finish_close(pos, requeue=True)

    def _book_close_fill(self, pos: Position, qty_sold: int, price: float,
                         reason: str) -> None:
        entry = pos.entry_price or price
        realized = (price - entry) * 100 * qty_sold
        self.store.record_realized_pnl(realized, note=f"{pos.contract}: {reason}")
        pos.quantity -= qty_sold
        pos.close_price = price
        self.store.update_position(pos)
        log.info("SOLD %dx %s @ %.2f (%d left) | P&L %+.2f | %s",
                 qty_sold, pos.contract, price, pos.quantity, realized, reason)

    def _finish_close(self, pos: Position, *, requeue: bool = False) -> None:
        if pos.quantity <= 0:
            pos.quantity = 0
            pos.status = PositionStatus.CLOSED
            pos.closed_at = datetime.now(timezone.utc)
            self.store.update_position(pos)
            log.info("CLOSED %s @ %.2f", pos.contract, pos.close_price or 0.0)
        else:
            pos.status = PositionStatus.OPEN
            self.store.update_position(pos)
            if requeue:
                self.store.set_close_requested(pos.id)
