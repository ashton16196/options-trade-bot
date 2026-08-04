"""Turn free-form Discord alert text into structured Signal objects.

Formats this parser handles (all real-world shapes from signal groups):

    BTO SPY 640C 8/15 @ .52
    BTO SPY 8/15 640C @ .52
    $TSLA 250p 12/19/25 @ 2.10
    bought aapl 230c 0dte at 1.25
    NVDA 190 calls 8/22 @ 3.40
    STC SPY 640C 8/15 @ .95 (+82%)
    selling my tsla puts here
    trimmed half NVDA 190C
    stopped out of AMD calls

Design bias: when a message contains both open- and close-flavored words,
the parser prefers CLOSE. A close signal only ever acts on positions the
bot already holds (worst case: exit early), while a misread OPEN buys new
contracts with real money — the more expensive mistake.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from .models import Action, OptionContract, OptionType, Signal

# Words that would otherwise look like tickers.
_TICKER_BLOCKLIST = {
    "A", "I", "AT", "IN", "ON", "BE", "IS", "IT", "TO", "UP", "MY", "WE", "NO",
    "THE", "AND", "FOR", "ALL", "GOT", "GET", "ADD", "PER", "LOT", "LOW", "TOP",
    "BTO", "STC", "STO", "BTC", "DTE", "OTM", "ITM", "ATM", "EOD", "ATH", "IMO",
    "PT", "TP", "SL", "AVG", "OUT", "NOW", "BUY", "SELL", "SOLD", "HOLD", "EXP",
    "RISK", "SIZE", "JUST", "WITH", "FROM", "HERE", "OVER", "THIS", "THAT",
    "NEXT", "LAST", "WEEK", "PLAY", "OPEN", "HIGH", "STOP", "LOSS", "GAIN",
}

_TRIM_WORDS = {"trim", "trimmed", "trimming", "half", "scaled", "scaling", "partial"}
_CLOSE_WORDS = {
    "stc", "sold", "sell", "selling", "close", "closed", "closing",
    "exit", "exited", "exiting", "out", "stopped",
}
_OPEN_WORDS = {
    "bto", "buy", "buying", "bought", "entry", "enter", "entered", "entering",
    "add", "added", "adding", "grab", "grabbed", "grabbing", "open", "opening",
    "opened", "long", "scooped", "scooping",
}
# Speculation markers: a message like "watching TSLA 250p for a possible
# entry" must never open a position. Only vetoes entries, never exits.
_HYPOTHETICAL_WORDS = {
    "watching", "watch", "watchlist", "eyeing", "considering", "might", "may",
    "could", "thinking", "planning", "plan", "possible", "potential", "idea",
    "radar", "waiting", "tomorrow", "soon",
}

# Ticker, optional expiry between ticker and strike, strike, C/P/call(s)/put(s).
_CONTRACT_RE = re.compile(
    r"""\$?\b(?P<ticker>[A-Za-z]{1,5})\b
        (?:[\s,]+(?P<mid_date>\d{1,2}/\d{1,2}(?:/\d{2,4})?))?
        [\s,]*\$?
        (?P<strike>\d{1,5}(?:\.\d{1,2})?)
        \s*(?P<otype>calls?|puts?|[cp])\b
    """,
    re.VERBOSE | re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b")
_DTE_RE = re.compile(r"\b(?P<n>\d{1,2})\s*dte\b", re.IGNORECASE)
_PRICE_RE = re.compile(
    r"(?:@|\bat\b|\bfor\b)\s*\$?(?P<price>\d+(?:\.\d{1,4})?|\.\d{1,4})", re.IGNORECASE
)
# Loose "TSLA puts" / "$AMD calls" mention, for close signals without a strike.
_TICKER_TYPE_RE = re.compile(
    r"\$?\b(?P<ticker>[A-Za-z]{1,5})\b\s+(?P<otype>calls?|puts?)\b", re.IGNORECASE
)
_DOLLAR_TICKER_RE = re.compile(r"\$(?P<ticker>[A-Za-z]{1,5})\b")


class SignalParser:
    def __init__(
        self,
        *,
        require_action_keyword: bool = True,
        max_message_length: int = 280,
    ) -> None:
        # If False, a bare "SPY 640C 8/15 @ .52" with no BTO/bought/etc.
        # counts as an entry. Riskier; some groups post entries that way.
        self.require_action_keyword = require_action_keyword
        # Alerts are short. Long messages are almost always commentary/DD
        # that merely *mentions* a contract — never trade off those.
        self.max_message_length = max_message_length

    def parse(
        self,
        text: str,
        *,
        now: datetime | None = None,
        known_tickers: frozenset[str] | set[str] = frozenset(),
    ) -> Signal | None:
        """Return a Signal, or None if the message is not an actionable alert.

        known_tickers: tickers of currently-open positions; used to spot
        close alerts that mention the ticker loosely (or in lowercase).
        """
        now = now or datetime.now(timezone.utc)
        text = " ".join(text.split())
        if not text:
            return None
        notes: list[str] = []
        if len(text) > self.max_message_length:
            return None

        words = {w.strip(".,!:;()[]").lower() for w in text.split()}
        is_trim = bool(words & _TRIM_WORDS)
        is_close = is_trim or bool(words & _CLOSE_WORDS)
        is_open = bool(words & _OPEN_WORDS)

        contract, price = None, None
        partial: tuple[str, OptionType] | None = None
        for m in _CONTRACT_RE.finditer(text):
            ticker = m.group("ticker").upper()
            if ticker in _TICKER_BLOCKLIST:
                continue
            expiration = self._resolve_expiration(m, text, now, notes)
            strike = float(m.group("strike"))
            otype = OptionType.from_text(m.group("otype"))
            if expiration is not None:
                contract = OptionContract(ticker, strike, otype, expiration)
            else:
                # Strike/type known but no expiry — enough to match an
                # existing position for a close, not enough to open.
                partial = (ticker, otype)
                notes.append(f"contract {ticker} {strike:g}{m.group('otype')[0].upper()} has no expiration")
            pm = _PRICE_RE.search(text)
            if pm:
                price = float(pm.group("price"))
            if _CONTRACT_RE.search(text, m.end()):
                notes.append("multiple contracts in message; used the first")
            break

        # ---- close / trim ------------------------------------------------
        if is_close:
            action = Action.TRIM if is_trim else Action.CLOSE
            ticker, otype = self._close_target(text, contract, partial, known_tickers)
            if ticker is None:
                return None
            return Signal(
                action=action, raw_text=text, received_at=now, ticker=ticker,
                contract=contract, price=price, option_type=otype, notes=notes,
            )

        # ---- open --------------------------------------------------------
        if not is_open and self.require_action_keyword:
            return None
        if contract is None:
            return None
        if words & _HYPOTHETICAL_WORDS:
            return None
        if not is_open:
            notes.append("no action keyword; treated bare contract as entry")
        return Signal(
            action=Action.OPEN, raw_text=text, received_at=now,
            ticker=contract.ticker, contract=contract, price=price,
            option_type=contract.option_type, notes=notes,
        )

    # -- helpers -----------------------------------------------------------

    def _close_target(
        self,
        text: str,
        contract: OptionContract | None,
        partial: tuple[str, OptionType] | None,
        known_tickers: frozenset[str] | set[str],
    ) -> tuple[str | None, OptionType | None]:
        if contract is not None:
            return contract.ticker, contract.option_type
        if partial is not None:
            return partial
        m = _TICKER_TYPE_RE.search(text)
        if m and m.group("ticker").upper() not in _TICKER_BLOCKLIST:
            return m.group("ticker").upper(), OptionType.from_text(m.group("otype"))
        m = _DOLLAR_TICKER_RE.search(text)
        if m and m.group("ticker").upper() not in _TICKER_BLOCKLIST:
            return m.group("ticker").upper(), None
        # Last resort: any word matching a ticker we currently hold.
        held = {t.upper() for t in known_tickers}
        for word in text.split():
            w = word.strip(".,!:;()[]$").upper()
            if w in held:
                return w, None
        return None, None

    def _resolve_expiration(
        self, m: re.Match, text: str, now: datetime, notes: list[str]
    ) -> date | None:
        today = now.date()
        date_text = m.group("mid_date")
        if date_text is None:
            dm = _DATE_RE.search(text, m.end()) or _DATE_RE.search(text, 0, m.start())
            date_text = dm.group(0) if dm else None
        if date_text is not None:
            try:
                return self._parse_date(date_text, today)
            except ValueError:
                notes.append(f"unparseable expiration {date_text!r}")
                return None
        dte = _DTE_RE.search(text)
        if dte:
            return self._add_trading_days(today, int(dte.group("n")))
        return None

    @staticmethod
    def _parse_date(text: str, today: date) -> date:
        parts = [int(p) for p in text.split("/")]
        month, day = parts[0], parts[1]
        if len(parts) == 3:
            year = parts[2] if parts[2] > 999 else 2000 + parts[2]
            return date(year, month, day)
        candidate = date(today.year, month, day)
        if candidate < today:
            candidate = date(today.year + 1, month, day)
        return candidate

    @staticmethod
    def _add_trading_days(start: date, n: int) -> date:
        d = start
        while d.weekday() >= 5:  # 0dte posted on a weekend → next weekday
            d += timedelta(days=1)
        remaining = n
        while remaining > 0:
            d += timedelta(days=1)
            if d.weekday() < 5:
                remaining -= 1
        return d
