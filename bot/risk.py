"""Pre-trade risk checks. Every entry must pass ALL of these.

There is also a manual kill switch: create a file named HALT next to the
config file (`touch HALT`) and the bot refuses all new entries instantly.
Delete it to resume.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .brokers.base import OptionQuote
from .config import Config
from .models import Signal
from .state import Store

ET = ZoneInfo("America/New_York")


def market_is_open(now: datetime | None = None) -> bool:
    """Regular trading hours: Mon–Fri 9:30–16:00 ET. US market holidays are
    NOT handled — orders on a holiday just won't fill and time out."""
    et = (now or datetime.now(timezone.utc)).astimezone(ET)
    minutes = et.hour * 60 + et.minute
    return et.weekday() < 5 and (9 * 60 + 30) <= minutes < (16 * 60)


class RiskManager:
    def __init__(self, cfg: Config, store: Store, halt_dir: str | Path = ".") -> None:
        self.cfg = cfg
        self.store = store
        self.halt_file = Path(halt_dir) / "HALT"

    def check_entry(self, signal: Signal, quote: OptionQuote | None) -> tuple[bool, str]:
        t = self.cfg.trading
        contract = signal.contract
        assert contract is not None
        now = datetime.now(timezone.utc)

        if self.halt_file.exists():
            return False, "HALT file present — kill switch engaged"
        if t.market_hours_only and not market_is_open(now):
            return False, "outside regular market hours"

        age = (now - signal.received_at).total_seconds()
        if age > t.max_signal_age_seconds:
            return False, f"signal is {age:.0f}s old (max {t.max_signal_age_seconds}s)"

        if t.ticker_allowlist and contract.ticker not in t.ticker_allowlist:
            return False, f"{contract.ticker} not in allowlist"
        if contract.ticker in t.ticker_blocklist:
            return False, f"{contract.ticker} is blocklisted"

        today_et = now.astimezone(ET).date()
        if contract.expiration < today_et:
            return False, f"expiration {contract.expiration} already passed"
        if contract.expiration == today_et and not t.allow_0dte:
            return False, "0DTE entries disabled (allow_0dte: false)"

        if not t.allow_duplicate_contracts and self.store.holds_contract(contract):
            return False, f"already holding {contract}"
        if self.store.count_active() >= t.max_open_positions:
            return False, f"max_open_positions ({t.max_open_positions}) reached"

        pnl = self.store.realized_pnl_today()
        if pnl <= -t.max_daily_loss:
            return False, f"daily loss cap hit (realized {pnl:+.2f})"

        if quote is not None:
            if quote.ask <= 0:
                return False, "no ask price — market closed or bad contract"
            if signal.price and quote.ask > signal.price * (1 + t.max_slippage_from_alert_pct / 100):
                return False, (
                    f"ask {quote.ask:.2f} is >{t.max_slippage_from_alert_pct:.0f}% above "
                    f"alert price {signal.price:.2f} — too late, skipping"
                )
        elif self.cfg.mode == "live":
            return False, "no quote available in live mode"
        elif signal.price is None:
            return False, "paper mode without quotes needs an alert price to simulate"

        return True, "ok"

    def check_premium(self, limit_price: float, quantity: int) -> tuple[bool, str]:
        cost = limit_price * 100 * quantity
        if cost > self.cfg.trading.max_premium_per_trade:
            return False, (
                f"trade cost ${cost:.0f} exceeds max_premium_per_trade "
                f"${self.cfg.trading.max_premium_per_trade:.0f}"
            )
        return True, "ok"

    def check_close(self) -> tuple[bool, str]:
        # Closing reduces risk, so almost nothing blocks it — but options
        # only trade during RTH, so an off-hours order would just sit.
        # Callers persist the intent (close_requested) and the monitor
        # executes it on the first in-hours tick.
        if self.cfg.trading.market_hours_only and not market_is_open():
            return False, "outside regular market hours"
        return True, "ok"
