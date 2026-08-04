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


def test_open_then_group_close(world):
    cfg, store, quotes, executor, parser = world
    now = datetime.now(timezone.utc)

    sig = parser.parse("BTO SPY 640C 12/18 @ .52", now=now)
    run(executor.handle_signal(sig))

    positions = store.open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.status is PositionStatus.OPEN
    assert pos.entry_price == pytest.approx(0.55, abs=0.02)  # filled at the ask

    quotes.quote = OptionQuote(bid=0.90, ask=0.95, mark=0.92)
    close = parser.parse("STC SPY 640C 12/18 @ .92", now=now,
                         known_tickers=store.open_tickers())
    run(executor.handle_signal(close))

    assert store.open_positions() == []
    assert store.realized_pnl_today() > 0


def test_take_profit_via_monitor_logic(world):
    cfg, store, quotes, executor, parser = world
    now = datetime.now(timezone.utc)
    sig = parser.parse("BTO QQQ 570C 12/18 @ 1.00", now=now)
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
    sig = parser.parse("BTO SPY 640C 12/18 @ .52", now=now)
    run(executor.handle_signal(sig))
    run(executor.handle_signal(sig))  # same alert again
    assert len(store.open_positions()) == 1


def test_trim_leaves_partial(world):
    cfg, store, quotes, executor, parser = world
    cfg.trading.contracts_per_trade = 2
    now = datetime.now(timezone.utc)
    run(executor.handle_signal(parser.parse("BTO TSLA 250C 12/18 @ 1.00", now=now)))

    trim = parser.parse("trimming half TSLA 250C", now=now,
                        known_tickers=store.open_tickers())
    run(executor.handle_signal(trim))
    positions = store.open_positions()
    assert len(positions) == 1
    assert positions[0].quantity == 1
