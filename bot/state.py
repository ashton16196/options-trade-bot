"""SQLite persistence: processed messages, positions, realized P&L.

Everything runs on the asyncio event-loop thread, so a single connection
without locking is fine. Broker HTTP calls (the slow part) happen in
worker threads and never touch this module.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .models import OptionContract, OptionType, Position, PositionStatus

ET = ZoneInfo("America/New_York")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_signals (
    key TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL,
    expiration TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL,
    entry_price REAL,
    opened_at TEXT,
    closed_at TEXT,
    close_price REAL,
    open_order_id TEXT,
    close_order_id TEXT,
    source_signal TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS realized_pnl (
    day TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT DEFAULT ''
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- signal dedupe ----------------------------------------------------

    def already_processed(self, key: str) -> bool:
        """Record the key; return True if it was seen before."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO processed_signals (key, seen_at) VALUES (?, ?)",
            (key, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return cur.rowcount == 0

    # -- positions --------------------------------------------------------

    def insert_position(self, pos: Position) -> Position:
        cur = self.conn.execute(
            """INSERT INTO positions
               (ticker, strike, option_type, expiration, quantity, status,
                entry_price, opened_at, open_order_id, source_signal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pos.contract.ticker, pos.contract.strike,
                pos.contract.option_type.value, pos.contract.expiration.isoformat(),
                pos.quantity, pos.status.value, pos.entry_price,
                pos.opened_at.isoformat() if pos.opened_at else None,
                pos.open_order_id, pos.source_signal,
            ),
        )
        self.conn.commit()
        pos.id = cur.lastrowid
        return pos

    def update_position(self, pos: Position) -> None:
        self.conn.execute(
            """UPDATE positions SET quantity=?, status=?, entry_price=?, opened_at=?,
               closed_at=?, close_price=?, open_order_id=?, close_order_id=? WHERE id=?""",
            (
                pos.quantity, pos.status.value, pos.entry_price,
                pos.opened_at.isoformat() if pos.opened_at else None,
                pos.closed_at.isoformat() if pos.closed_at else None,
                pos.close_price, pos.open_order_id, pos.close_order_id, pos.id,
            ),
        )
        self.conn.commit()

    def try_mark_closing(self, position_id: int) -> bool:
        """Atomically move OPEN → CLOSING. False if someone else got there
        first (e.g. take-profit monitor vs. a close alert racing)."""
        cur = self.conn.execute(
            "UPDATE positions SET status=? WHERE id=? AND status=?",
            (PositionStatus.CLOSING.value, position_id, PositionStatus.OPEN.value),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def positions_with_status(self, *statuses: PositionStatus) -> list[Position]:
        marks = ",".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT * FROM positions WHERE status IN ({marks})",
            [s.value for s in statuses],
        ).fetchall()
        return [self._row_to_position(r) for r in rows]

    def open_positions(self) -> list[Position]:
        return self.positions_with_status(PositionStatus.OPEN)

    def open_tickers(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT ticker FROM positions WHERE status IN (?, ?)",
            (PositionStatus.OPEN.value, PositionStatus.CLOSING.value),
        ).fetchall()
        return {r["ticker"] for r in rows}

    def count_active(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE status IN (?, ?, ?)",
            (PositionStatus.PENDING.value, PositionStatus.OPEN.value,
             PositionStatus.CLOSING.value),
        ).fetchone()
        return row["n"]

    def holds_contract(self, contract: OptionContract) -> bool:
        row = self.conn.execute(
            """SELECT COUNT(*) AS n FROM positions
               WHERE ticker=? AND strike=? AND option_type=? AND expiration=?
                 AND status IN (?, ?, ?)""",
            (contract.ticker, contract.strike, contract.option_type.value,
             contract.expiration.isoformat(), PositionStatus.PENDING.value,
             PositionStatus.OPEN.value, PositionStatus.CLOSING.value),
        ).fetchone()
        return row["n"] > 0

    # -- P&L --------------------------------------------------------------

    def record_realized_pnl(self, amount: float, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO realized_pnl (day, amount, note) VALUES (?, ?, ?)",
            (datetime.now(ET).date().isoformat(), amount, note),
        )
        self.conn.commit()

    def realized_pnl_today(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM realized_pnl WHERE day=?",
            (datetime.now(ET).date().isoformat(),),
        ).fetchone()
        return row["total"]

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        contract = OptionContract(
            ticker=row["ticker"],
            strike=row["strike"],
            option_type=OptionType(row["option_type"]),
            expiration=date.fromisoformat(row["expiration"]),
        )
        return Position(
            contract=contract,
            quantity=row["quantity"],
            status=PositionStatus(row["status"]),
            id=row["id"],
            entry_price=row["entry_price"],
            opened_at=datetime.fromisoformat(row["opened_at"]) if row["opened_at"] else None,
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
            close_price=row["close_price"],
            open_order_id=row["open_order_id"],
            close_order_id=row["close_order_id"],
            source_signal=row["source_signal"] or "",
        )
