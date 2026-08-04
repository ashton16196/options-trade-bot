"""Watches open positions: your own take-profit / stop-loss rules, plus a
forced close for anything expiring today near the bell."""
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
        self._warned_no_quotes = False

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
            if (eod is not None and pos.contract.expiration == now_et.date()
                    and now_et.time() >= eod):
                await self.executor.close_position(
                    pos, reason=f"expires today, auto-close at {eod.strftime('%H:%M')} ET")
                continue
            if pos.entry_price is None:
                continue
            quote = await asyncio.to_thread(self.broker.get_quote, pos.contract)
            if quote is None or quote.mid <= 0:
                if not self._warned_no_quotes:
                    self._warned_no_quotes = True
                    log.warning("no quotes available — TP/SL monitoring is inactive "
                                "(paper mode without Robinhood credentials?)")
                continue
            change_pct = (quote.mid - pos.entry_price) / pos.entry_price * 100
            if change_pct >= t.take_profit_pct:
                await self.executor.close_position(
                    pos, reason=f"take-profit hit ({change_pct:+.0f}%)")
            elif t.stop_loss_pct is not None and change_pct <= -t.stop_loss_pct:
                await self.executor.close_position(
                    pos, reason=f"stop-loss hit ({change_pct:+.0f}%)")
