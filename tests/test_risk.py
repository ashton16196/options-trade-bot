from datetime import date, datetime, timedelta, timezone

import pytest

from bot.brokers.base import OptionQuote
from bot.config import Config
from bot.models import Action, OptionContract, OptionType, Signal
from bot.risk import RiskManager, market_is_open
from bot.state import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


@pytest.fixture
def risk(store, tmp_path):
    cfg = Config()
    cfg.trading.market_hours_only = False  # tests shouldn't depend on wall clock
    return RiskManager(cfg, store, halt_dir=tmp_path)


def signal(age_seconds=0, ticker="SPY", price=0.52):
    now = datetime.now(timezone.utc)
    contract = OptionContract(ticker, 640.0, OptionType.CALL,
                              (now + timedelta(days=10)).date())
    return Signal(action=Action.OPEN, raw_text="test", ticker=ticker,
                  received_at=now - timedelta(seconds=age_seconds),
                  contract=contract, price=price)


QUOTE = OptionQuote(bid=0.50, ask=0.55, mark=0.52)


def test_fresh_signal_passes(risk):
    ok, reason = risk.check_entry(signal(), QUOTE)
    assert ok, reason


def test_stale_signal_rejected(risk):
    ok, reason = risk.check_entry(signal(age_seconds=90), QUOTE)
    assert not ok and "old" in reason


def test_allowlist(risk):
    risk.cfg.trading.ticker_allowlist = ["QQQ"]
    ok, reason = risk.check_entry(signal(), QUOTE)
    assert not ok and "allowlist" in reason


def test_blocklist(risk):
    risk.cfg.trading.ticker_blocklist = ["SPY"]
    ok, _ = risk.check_entry(signal(), QUOTE)
    assert not ok


def test_slippage_guard(risk):
    ran_away = OptionQuote(bid=0.80, ask=0.85, mark=0.82)  # alert said .52
    ok, reason = risk.check_entry(signal(), ran_away)
    assert not ok and "above" in reason


def test_premium_cap(risk):
    ok, reason = risk.check_premium(limit_price=6.00, quantity=1)  # $600 > $500
    assert not ok


def test_daily_loss_halts(risk, store):
    store.record_realized_pnl(-800.0)
    ok, reason = risk.check_entry(signal(), QUOTE)
    assert not ok and "loss cap" in reason


def test_halt_file_kill_switch(risk):
    risk.halt_file.touch()
    ok, reason = risk.check_entry(signal(), QUOTE)
    assert not ok and "HALT" in reason


def test_max_positions(risk, store):
    risk.cfg.trading.max_open_positions = 0
    ok, reason = risk.check_entry(signal(), QUOTE)
    assert not ok and "max_open_positions" in reason


def test_market_hours_helper():
    tue_lunch = datetime(2026, 8, 4, 17, 0, tzinfo=timezone.utc)   # 13:00 ET Tue
    tue_night = datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc)
    saturday = datetime(2026, 8, 8, 17, 0, tzinfo=timezone.utc)
    assert market_is_open(tue_lunch)
    assert not market_is_open(tue_night)
    assert not market_is_open(saturday)
