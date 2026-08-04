"""End-to-end paper flow: alert text in → position opened → close alert →
position closed with P&L recorded. No network, no Discord."""
import asyncio
from datetime import datetime, timezone

import pytest

from bot.brokers.base import Broker, OptionQuote, OrderResult, OrderState
from bot.brokers.paper import PaperBroker
from bot.config import Config
from bot.executor import TradeExecutor
from bot.models import PositionStatus
from bot.parser import SignalParser
from bot.risk import RiskManager
from bot.state import Store


class StubQuotes(Broker):
    """Quote source with a controllable price."""
    name = "stub"

    def __init__(self):
        self.quote = OptionQuote(bid=0.50, ask=0.55, mark=0.52)

    def get_quote(self, contract):
        return self.quote

    def buy_to_open(self, *a):
        raise AssertionError("stub never trades")

    sell_to_close = buy_to_open

    def order_status(self, order_id):
        return OrderResult(order_id, OrderState.UNKNOWN)

    def cancel_order(self, order_id):
        return False


@pytest.fixture
def world(tmp_path):
    cfg = Config()
    cfg.trading.market_hours_only = False
    store = Store(str(tmp_path / "e2e.db"))
    quotes = StubQuotes()
    broker = PaperBroker(quote_source=quotes)
    risk = RiskManager(cfg, store, halt_dir=tmp_path)
    executor = TradeExecutor(cfg, broker, store, risk)
    parser = SignalParser()
    return cfg, store, quotes, executor, parser


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def first(signals):
    assert signals, "expected the text to parse into a signal"
    return signals[0]


def test_open_then_group_close(world):
    cfg, store, quotes, executor, parser = world
    now = datetime.now(timezone.utc)

    sig = first(parser.parse("BTO SPY 640C 12/18 @ .52", now=now))
    run(executor.handle_signal(sig))

    positions = store.open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.status is PositionStatus.OPEN
    assert pos.entry_price == pytest.approx(0.55, abs=0.02)  # filled at the ask

    quotes.quote = OptionQuote(bid=0.90, ask=0.95, mark=0.92)
    close = first(parser.parse("STC SPY 640C 12/18 @ .92", now=now,
                               known_tickers=store.open_tickers()))
    run(executor.handle_signal(close))

    assert store.open_positions() == []
    assert store.realized_pnl_today() > 0


def test_take_profit_via_monitor_logic(world):
    cfg, store, quotes, executor, parser = world
    now = datetime.now(timezone.utc)
    sig = first(parser.parse("BTO QQQ 570C 12/18 @ 1.00", now=now))
    run(executor.handle_signal(sig))
    pos = store.open_positions()[0]

    quotes.quote = OptionQuote(bid=1.55, ask=1.65, mark=1.60)  # +60% vs ~1.00 entry
    change = (quotes.quote.mid - pos.entry_price) / pos.entry_price * 100
    assert change >= cfg.trading.take_profit_pct
    run(executor.close_position(pos, reason="take-profit test"))
    assert store.open_positions() == []


def test_duplicate_contract_rejected(world):
    cfg, store, quotes, executor, parser = world
    now = datetime.now(timezone.utc)
    sig = first(parser.parse("BTO SPY 640C 12/18 @ .52", now=now))
    run(executor.handle_signal(sig))
    run(executor.handle_signal(sig))  # same alert again
    assert len(store.open_positions()) == 1


def test_trim_leaves_partial(world):
    cfg, store, quotes, executor, parser = world
    cfg.trading.contracts_per_trade = 2
    now = datetime.now(timezone.utc)
    run(executor.handle_signal(
        first(parser.parse("BTO TSLA 250C 12/18 @ 1.00", now=now))))

    trim = first(parser.parse("trimming half TSLA 250C", now=now,
                              known_tickers=store.open_tickers()))
    run(executor.handle_signal(trim))
    positions = store.open_positions()
    assert len(positions) == 1
    assert positions[0].quantity == 1


# ---- regression tests from the adversarial review ------------------------

def test_puts_exit_never_closes_calls(world):
    cfg, store, quotes, executor, parser = world
    now = datetime.now(timezone.utc)
    run(executor.handle_signal(
        first(parser.parse("BTO SPY 640C 12/18 @ .52", now=now))))

    close = first(parser.parse("selling my spy puts here", now=now,
                               known_tickers=store.open_tickers()))
    run(executor.handle_signal(close))
    assert len(store.open_positions()) == 1  # calls untouched


def test_specific_contract_exit_never_closes_other_contract(world):
    cfg, store, quotes, executor, parser = world
    now = datetime.now(timezone.utc)
    run(executor.handle_signal(
        first(parser.parse("BTO SPY 640C 12/18 @ .52", now=now))))

    # Group exits a puts play we never took — must not touch our calls.
    close = first(parser.parse("STC SPY 632P 12/18 @ .90", now=now,
                               known_tickers=store.open_tickers()))
    run(executor.handle_signal(close))
    assert len(store.open_positions()) == 1


def test_expiryless_close_targets_only_matching_strike(world):
    cfg, store, quotes, executor, parser = world
    now = datetime.now(timezone.utc)
    run(executor.handle_signal(
        first(parser.parse("BTO SPY 640C 12/18 @ .52", now=now))))
    run(executor.handle_signal(
        first(parser.parse("BTO SPY 650C 12/24 @ .50", now=now))))
    assert len(store.open_positions()) == 2

    close = first(parser.parse("STC SPY 640C @ .95", now=now,
                               known_tickers=store.open_tickers()))
    run(executor.handle_signal(close))
    remaining = store.open_positions()
    assert len(remaining) == 1
    assert remaining[0].contract.strike == 650.0  # the 650C survived


def test_off_hours_close_is_queued_not_dropped(world):
    cfg, store, quotes, executor, parser = world
    cfg.trading.market_hours_only = True  # force check_close to fail off-hours
    now = datetime.now(timezone.utc)
    cfg.trading.market_hours_only = False
    run(executor.handle_signal(
        first(parser.parse("BTO SPY 640C 12/18 @ .52", now=now))))
    cfg.trading.market_hours_only = True

    pos = store.open_positions()[0]
    from bot.risk import market_is_open
    if market_is_open():  # test may run during RTH; simulate via HALT of hours
        pytest.skip("test depends on being outside market hours")
    run(executor.close_position(pos, reason="group close after hours"))
    fresh = store.get_position(pos.id)
    assert fresh.status is PositionStatus.OPEN
    assert fresh.close_requested  # queued for the monitor's next open tick


def test_multi_contract_close_closes_both(world):
    cfg, store, quotes, executor, parser = world
    now = datetime.now(timezone.utc)
    run(executor.handle_signal(
        first(parser.parse("BTO SPY 640C 12/18 @ .52", now=now))))
    run(executor.handle_signal(
        first(parser.parse("BTO QQQ 570C 12/18 @ .60", now=now))))
    assert len(store.open_positions()) == 2

    sigs = parser.parse("Closing out both: SPY 640C 12/18 @ 1.10, QQQ 570C 12/18 @ 1.30",
                        now=now, known_tickers=store.open_tickers())
    assert len(sigs) == 2
    for s in sigs:
        run(executor.handle_signal(s))
    assert store.open_positions() == []
