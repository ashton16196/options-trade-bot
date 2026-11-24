"""Wires everything together: listener → parser → risk → executor → monitor."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .brokers.alpaca import AlpacaBroker
from .brokers.paper import PaperBroker
from .brokers.robinhood import RobinhoodBroker
from .config import Config, load_config
from .executor import TradeExecutor
from .monitor import PositionMonitor
from .parser import SignalParser
from .risk import RiskManager
from .state import Store

log = logging.getLogger(__name__)


def build_broker(cfg: Config):
    if cfg.broker == "alpaca":
        if cfg.mode == "live":
            log.warning("=== LIVE MODE: real orders will be placed on Alpaca ===")
        else:
            log.info("Alpaca PAPER account: full order lifecycle, fake money")
        return AlpacaBroker(cfg.alpaca_api_key, cfg.alpaca_secret_key,
                            paper=cfg.mode != "live")

    # broker: robinhood (unofficial API — see bot/brokers/robinhood.py caveats)
    have_rh_creds = bool(cfg.rh_username and cfg.rh_password)
    if cfg.mode == "live":
        log.warning("=== LIVE MODE: real orders will be placed on Robinhood ===")
        return RobinhoodBroker(cfg.rh_username, cfg.rh_password, cfg.rh_totp_secret)
    if have_rh_creds:
        log.info("paper mode with real Robinhood quotes (login is read-only)")
        return PaperBroker(
            quote_source=RobinhoodBroker(cfg.rh_username, cfg.rh_password, cfg.rh_totp_secret))
    log.warning("paper mode WITHOUT quotes: fills simulate at alert prices and "
                "take-profit monitoring is inactive. Add Robinhood credentials "
                "to .env to paper-trade against real bid/ask.")
    return PaperBroker()


async def run_bot(config_path: str) -> None:
    cfg = load_config(config_path)
    cfg.validate()

    broker = build_broker(cfg)
    try:
        await asyncio.to_thread(broker.connect)
    except Exception:
        if cfg.mode == "paper":
            log.exception("quote-source login failed; continuing paper mode without quotes")
            broker = PaperBroker()
        else:
            raise

    store = Store(cfg.db_path)
    risk = RiskManager(cfg, store, halt_dir=Path(config_path).resolve().parent)
    executor = TradeExecutor(cfg, broker, store, risk)
    monitor = PositionMonitor(cfg, broker, store, executor)
    parser = SignalParser(
        require_action_keyword=cfg.trading.require_action_keyword,
        max_message_length=cfg.trading.max_message_length,
    )

    await executor.reconcile()  # re-attach to orders left in flight by a restart

    async def handle(text: str, message_id: str, source: str, created_at) -> None:
        if store.already_processed(message_id):
            return
        # created_at (Discord's timestamp) drives the staleness guard, so a
        # gateway reconnect replaying a 3-minute-old alert doesn't get traded.
        signals = parser.parse(text, now=created_at, known_tickers=store.open_tickers())
        for sig in signals:
            sig.message_id, sig.source = message_id, source
            log.info("SIGNAL %s %s from %s | %r",
                     sig.action.value.upper(), sig.contract or sig.ticker, source, text[:140])
            for note in sig.notes:
                log.info("  parser note: %s", note)
            try:
                await executor.handle_signal(sig)
            except Exception:
                log.exception("failed handling signal %r", text[:140])

    monitor_task = asyncio.create_task(monitor.run())
    log.info("mode=%s | broker=%s | source=%s | TP +%.0f%% | %d contract(s)/trade | max $%.0f/trade",
             cfg.mode, cfg.broker, cfg.source, cfg.trading.take_profit_pct,
             cfg.trading.contracts_per_trade, cfg.trading.max_premium_per_trade)
    try:
        if cfg.source == "browser":
            from .listener_browser import BrowserListener

            watcher = BrowserListener(
                handle, channel_url=cfg.browser.channel_url,
                poll_ms=cfg.browser.poll_ms, profile_dir=cfg.browser.profile_dir)
            await watcher.run()
        else:
            from .listener import DiscordListener

            listener = DiscordListener(cfg.discord.channel_ids, handle)
            await listener.start(cfg.discord_token)
    finally:
        monitor_task.cancel()
