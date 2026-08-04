"""Configuration: YAML file for behavior, environment variables for secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

import yaml


@dataclass
class DiscordConfig:
    channel_ids: list[int] = field(default_factory=list)


@dataclass
class TradingConfig:
    max_signal_age_seconds: int = 60
    contracts_per_trade: int = 1
    max_premium_per_trade: float = 500.0   # dollars, price * 100 * qty
    max_open_positions: int = 3
    max_daily_loss: float = 750.0          # realized; halts new entries for the day
    take_profit_pct: float = 50.0
    stop_loss_pct: float | None = None     # e.g. 50 → exit at -50%; null disables
    trim_fraction: float = 0.5
    ticker_allowlist: list[str] = field(default_factory=list)  # empty = all
    ticker_blocklist: list[str] = field(default_factory=list)
    allow_0dte: bool = True
    allow_duplicate_contracts: bool = False
    require_action_keyword: bool = True
    max_message_length: int = 280
    entry_limit_buffer_pct: float = 2.0    # limit = ask * (1 + this/100)
    close_limit_buffer_pct: float = 5.0    # limit = bid * (1 - this/100)
    max_slippage_from_alert_pct: float = 25.0
    fill_timeout_seconds: int = 45
    market_hours_only: bool = True


@dataclass
class MonitorConfig:
    poll_seconds: int = 5
    # Positions expiring today get force-closed at this ET time. null disables.
    eod_close_time: str | None = "15:50"

    def eod_close(self) -> time | None:
        v = self.eod_close_time
        if v in (None, "", False):
            return None
        if isinstance(v, int):
            # YAML parses unquoted 15:50 as sexagesimal int 950 — undo that.
            hh, mm = divmod(v, 60)
            return time(hh, mm)
        hh, mm = str(v).split(":")
        return time(int(hh), int(mm))


@dataclass
class Config:
    mode: str = "paper"        # "paper" | "live"
    broker: str = "alpaca"     # "alpaca" (official API) | "robinhood" (unofficial)
    db_path: str = "tradebot.db"
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)

    # Secrets, from environment
    discord_token: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    rh_username: str = ""
    rh_password: str = ""
    rh_totp_secret: str = ""
    confirm_live: bool = False

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {self.mode!r}")
        if self.broker not in ("alpaca", "robinhood"):
            raise ValueError(f"broker must be 'alpaca' or 'robinhood', got {self.broker!r}")
        if not self.discord_token:
            raise ValueError("DISCORD_BOT_TOKEN is not set (see .env.example)")
        if not self.discord.channel_ids:
            raise ValueError("discord.channel_ids is empty — set the channel(s) to watch")
        if self.broker == "alpaca" and not (self.alpaca_api_key and self.alpaca_secret_key):
            raise ValueError("broker 'alpaca' needs ALPACA_API_KEY and ALPACA_SECRET_KEY "
                             "(paper keys are free/instant at app.alpaca.markets)")
        if self.mode == "live":
            if self.broker == "robinhood" and not (self.rh_username and self.rh_password):
                raise ValueError("live robinhood mode needs ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD")
            if not self.confirm_live:
                raise ValueError(
                    "live mode refused: set TRADE_BOT_CONFIRM_LIVE=yes in .env "
                    "to confirm you want real orders placed"
                )
        t = self.trading
        if t.contracts_per_trade < 1:
            raise ValueError("contracts_per_trade must be >= 1")
        if not 0 < t.trim_fraction <= 1:
            raise ValueError("trim_fraction must be in (0, 1]")
        try:
            self.monitor.eod_close()  # fail at startup, not on every tick
        except Exception as e:
            raise ValueError(f"monitor.eod_close_time is invalid "
                             f"({self.monitor.eod_close_time!r}): {e}") from e


def _apply(section: object, data: dict) -> None:
    for key, value in (data or {}).items():
        if not hasattr(section, key):
            raise ValueError(f"unknown config key: {key}")
        setattr(section, key, value)


def load_config(path: str | Path) -> Config:
    cfg = Config()
    raw = yaml.safe_load(Path(path).read_text()) or {}
    for name in ("mode", "broker", "db_path"):
        if name in raw:
            setattr(cfg, name, raw.pop(name))
    _apply(cfg.discord, raw.pop("discord", {}))
    _apply(cfg.trading, raw.pop("trading", {}))
    _apply(cfg.monitor, raw.pop("monitor", {}))
    if raw:
        raise ValueError(f"unknown top-level config keys: {sorted(raw)}")

    cfg.discord.channel_ids = [int(c) for c in cfg.discord.channel_ids]
    cfg.trading.ticker_allowlist = [t.upper() for t in cfg.trading.ticker_allowlist]
    cfg.trading.ticker_blocklist = [t.upper() for t in cfg.trading.ticker_blocklist]

    cfg.discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    cfg.alpaca_api_key = os.environ.get("ALPACA_API_KEY", "")
    cfg.alpaca_secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    cfg.rh_username = os.environ.get("ROBINHOOD_USERNAME", "")
    cfg.rh_password = os.environ.get("ROBINHOOD_PASSWORD", "")
    cfg.rh_totp_secret = os.environ.get("ROBINHOOD_TOTP_SECRET", "")
    cfg.confirm_live = os.environ.get("TRADE_BOT_CONFIRM_LIVE", "").lower() == "yes"
    return cfg
