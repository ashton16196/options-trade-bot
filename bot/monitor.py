"""Watches open positions: the user's take-profit / stop-loss rules, queued
close requests, and a forced close for anything expiring today near the bell.

Closes are spawned as tasks, not awaited inline — one illiquid close must
not starve TP/SL checks on every other position. try_mark_closing makes
duplicate spawns across ticks harmless.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .brokers.base import Broker
from .config import Config
from .executor import TradeExecutor
from .risk import ET, market_is_open
from .state import Store

log = logging.getLogger(__name__)


class PositionMonitor:
    def __init__(self, cfg: Config, broker: Broker, store: Store,
                 executor: TradeExecutor) -> None:
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.executor = executor
        self._quote_failures: dict[int, int] = {}

    async def run(self) -> None:
        log.info("position monitor running (every %ds, TP %+.0f%%, SL %s)",
                 self.cfg.monitor.poll_seconds, self.cfg.trading.take_profit_pct,
                 f"-{self.cfg.trading.stop_loss_pct:.0f}%" if self.cfg.trading.stop_loss_pct else "off")
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor tick failed")
            await asyncio.sleep(self.cfg.monitor.poll_seconds)

    async def _tick(self) -> None:
        if self.cfg.trading.market_hours_only and not market_is_open():
            return
        t = self.cfg.trading
        eod = self.cfg.monitor.eod_close()
        now_et = datetime.now(ET)

        for pos in self.store.open_positions():
            if pos.close_requested:
                self.executor._spawn(self.executor.close_position(
                    pos, reason="queued close (alert arrived off-hours or mid-order)"))
                continue
            if (eod is not None and pos.contract.expiration == now_et.date()
                    and now_et.time() >= eod):
                self.executor._spawn(self.executor.close_position(
                    pos, reason=f"expires today, auto-close at {eod.strftime('%H:%M')} ET"))
                continue
            if pos.entry_price is None:
                continue
            quote = await asyncio.to_thread(self.broker.get_quote, pos.contract)
            if quote is None or quote.mid <= 0:
                self._note_quote_failure(pos)
                continue
            self._quote_failures.pop(pos.id, None)
            change_pct = (quote.mid - pos.entry_price) / pos.entry_price * 100
            if change_pct >= t.take_profit_pct:
                self.executor._spawn(self.executor.close_position(
                    pos, reason=f"take-profit hit ({change_pct:+.0f}%)"))
            elif t.stop_loss_pct is not None and change_pct <= -t.stop_loss_pct:
                self.executor._spawn(self.executor.close_position(
                    pos, reason=f"stop-loss hit ({change_pct:+.0f}%)"))

    def _note_quote_failure(self, pos) -> None:
        n = self._quote_failures.get(pos.id, 0) + 1
        self._quote_failures[pos.id] = n
        # Warn immediately, then roughly once a minute while it persists —
        # a quote outage means TP/SL protection is OFF for this position.
        if n == 1 or n % max(1, 60 // self.cfg.monitor.poll_seconds) == 0:
            log.warning("no quote for %s from %s broker — TP/SL inactive for "
                        "this position (%d consecutive misses)",
                        pos.contract, self.broker.name, n)
