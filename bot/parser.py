"""Turn free-form Discord alert text into structured Signal objects.

Formats this parser handles (all real-world shapes from signal groups):

    BTO SPY 640C 8/15 @ .52          **BTO** SPY 640C 8/15 @ .52
    BTO SPY 8/15 640C @ .52          BTO SPY 0dte 640C @ .52
    $TSLA 250p 12/19/25 @ 2.10       NVDA 190 calls Aug 22 @ 3.40
    bought aapl 230c 0dte at 1.25    STC SPY 640C @ .95 (+82%)
    selling my tsla puts here        trimmed half NVDA 190C
    stopped out of AMD calls         all out $COIN

parse() returns a LIST of signals: close alerts naming several contracts
("closing out both: SPY 640C ..., QQQ 570C ...") yield one CLOSE per
contract. Entry alerts only ever yield the first contract — opening
several positions off one message is deliberately not done.

Design bias: false entries cost more than missed entries, so anything
hypothetical ("watching", "might"), any long commentary post, and any
ambiguous keyword is dropped or resolved toward not-trading. Close words
that double as chatter ("out", "closed", "stopped", "sell") only count
inside unambiguous exit phrases ("stopped out", "all out", ...), because
a stray match here would liquidate a held position on ordinary talk.
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

# Discord markup that must not reach the regexes: custom emoji and
# user/role/channel mentions ("<@123456>" would parse as "@ <price>").
_MARKUP_RE = re.compile(r"<a?:\w+:\d+>|<[@#][!&]?\d+>|@everyone|@here")

# Chatter idioms containing exit-ish words that must NOT count as exits.
_CHATTER_MASK = re.compile(
    r"\bs(?:elling|old)[\s-]+off\b|\bsell[\s-]?off\b|\bout\s+of\s+the\s+money\b",
    re.IGNORECASE,
)

_TRIM_WORDS = {"trim", "trimmed", "trimming"}
_TRIM_PHRASES = re.compile(
    r"\b(?:sold|selling|sell|take|taking|took)\s+(?:a\s+)?half\b"
    r"|\bscal(?:ed|ing)\s+out\b|\bhalf\s+(?:off|out)\b",
    re.IGNORECASE,
)
# Bare "out"/"closed"/"stopped"/"sell"/"exit" are chatter magnets
# ("breaking out", "closed above 190", "will sell at 2.00", "exit plan") —
# only unambiguous forms count.
_STRONG_CLOSE_WORDS = {"stc", "sold", "selling", "exited", "exiting"}
_CLOSE_PHRASES = re.compile(
    r"\b(?:stopped\s+out|closed?\s+out|closing\s+out|all\s+out"
    r"|i'?m\s+out|we'?re\s+out|taking\s+profits?|took\s+profits?"
    r"|cut(?:ting)?\s+(?:the\s+)?loss(?:es)?)\b",
    re.IGNORECASE,
)
_OPEN_WORDS = {
    "bto", "buy", "buying", "bought", "entry", "enter", "entered", "entering",
    "add", "added", "adding", "grab", "grabbed", "grabbing", "open", "opening",
    "opened", "long", "scooped", "scooping",
}
# Speculation markers: "watching TSLA 250p for a possible entry" must never
# open a position. Only vetoes entries, never exits.
_HYPOTHETICAL_WORDS = {
    "watching", "watch", "watchlist", "eyeing", "considering", "might", "may",
    "could", "thinking", "planning", "possible", "potential", "idea",
    "radar", "waiting", "tomorrow", "soon",
}

# Ticker, optional expiry-or-dte between ticker and strike, strike, C/P.
_CONTRACT_RE = re.compile(
    r"""\$?\b(?P<ticker>[A-Za-z]{1,5})\b
        (?:[\s,]+(?P<mid_date>\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}\s*dte))?
        [\s,]*\$?
        (?P<strike>\d{1,5}(?:\.\d{1,2})?)
        \s*(?P<otype>calls?|puts?|[cp])\b
    """,
    re.VERBOSE | re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b")
_DTE_RE = re.compile(r"\b(?P<n>\d{1,2})\s*dte\b", re.IGNORECASE)
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    # trailing lookahead rejects times ("at 10:15"), dates ("at 8/15") and
    # multipliers ("for 2x")
    r"(?:@|\bat\b|\bfor\b)\s*\$?(?P<price>\d+(?:\.\d{1,4})?|\.\d{1,4})(?![\d:/xX])",
    re.IGNORECASE,
)
# Loose "TSLA puts" / "$AMD calls" mention, for close signals without a strike.
_TICKER_TYPE_RE = re.compile(
    r"\$?\b(?P<ticker>[A-Za-z]{1,5})\b\s+(?P<otype>calls?|puts?)\b", re.IGNORECASE
)
_DOLLAR_TICKER_RE = re.compile(r"\$(?P<ticker>[A-Za-z]{1,5})\b")

# "words after size-fraction" filter: '1/2 size' is a position size, not Jan 2
_FRACTION_TAILS = ("size", "sized", "position", "pos", "risk")


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
    ) -> list[Signal]:
        """Return actionable signals parsed from one message (usually 0 or 1).

        known_tickers: tickers of currently-held positions; used to spot
        close alerts that mention the ticker loosely (or in lowercase).
        """
        now = now or datetime.now(timezone.utc)
        text = _MARKUP_RE.sub(" ", text)
        text = " ".join(text.split())
        if not text or len(text) > self.max_message_length:
            return []
        notes: list[str] = []

        masked = _CHATTER_MASK.sub(" ", text)
        words = {re.sub(r"^\W+|\W+$", "", w).lower() for w in masked.split()}
        is_trim = bool(words & _TRIM_WORDS) or bool(_TRIM_PHRASES.search(masked))
        is_close = is_trim or bool(words & _STRONG_CLOSE_WORDS) or bool(
            _CLOSE_PHRASES.search(masked))
        is_open = bool(words & _OPEN_WORDS)

        found = self._find_contracts(text, now, notes)

        # ---- close / trim ------------------------------------------------
        if is_close:
            action = Action.TRIM if is_trim else Action.CLOSE
            if found:
                return [
                    Signal(
                        action=action, raw_text=text, received_at=now,
                        ticker=f["ticker"], contract=f["contract"],
                        price=f["price"], strike=f["strike"],
                        option_type=f["otype"], notes=list(notes),
                    )
                    for f in found
                ]
            ticker, otype = self._close_target(text, known_tickers)
            if ticker is None:
                return []
            return [Signal(action=action, raw_text=text, received_at=now,
                           ticker=ticker, option_type=otype, notes=notes)]

        # ---- open --------------------------------------------------------
        if not is_open and self.require_action_keyword:
            return []
        if not found:
            return []
        hypothetical = words & _HYPOTHETICAL_WORDS
        # "May 15" is a month, not the modal verb
        if "may" in hypothetical and re.search(r"\bmay\.?\s+\d", masked, re.IGNORECASE):
            hypothetical.discard("may")
        if hypothetical:
            return []
        f = found[0]
        if f["contract"] is None:
            return []  # entries need a resolvable expiration
        if len(found) > 1:
            notes.append("multiple contracts in message; entering only the first")
        if not is_open:
            notes.append("no action keyword; treated bare contract as entry")
        return [Signal(
            action=Action.OPEN, raw_text=text, received_at=now,
            ticker=f["ticker"], contract=f["contract"], price=f["price"],
            strike=f["strike"], option_type=f["otype"], notes=notes,
        )]

    # -- helpers -----------------------------------------------------------

    def _find_contracts(self, text: str, now: datetime, notes: list[str]) -> list[dict]:
        matches = [m for m in _CONTRACT_RE.finditer(text)
                   if m.group("ticker").upper() not in _TICKER_BLOCKLIST]
        found: list[dict] = []
        for i, m in enumerate(matches):
            region_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            expiration = self._resolve_expiration(m, text, now, notes, region_end)
            price_match = _PRICE_RE.search(text, m.end(), region_end)
            if price_match is None and len(matches) == 1:
                price_match = _PRICE_RE.search(text)
            ticker = m.group("ticker").upper()
            strike = float(m.group("strike"))
            otype = OptionType.from_text(m.group("otype"))
            contract = None
            if expiration is not None:
                contract = OptionContract(ticker, strike, otype, expiration)
            else:
                notes.append(f"{ticker} {strike:g}{m.group('otype')[0].upper()}"
                             " has no resolvable expiration")
            found.append({
                "ticker": ticker, "strike": strike, "otype": otype,
                "contract": contract,
                "price": float(price_match.group("price")) if price_match else None,
            })
        return found

    def _close_target(
        self, text: str, known_tickers: frozenset[str] | set[str],
    ) -> tuple[str | None, OptionType | None]:
        m = _TICKER_TYPE_RE.search(text)
        if m and m.group("ticker").upper() not in _TICKER_BLOCKLIST:
            return m.group("ticker").upper(), OptionType.from_text(m.group("otype"))
        m = _DOLLAR_TICKER_RE.search(text)
        if m and m.group("ticker").upper() not in _TICKER_BLOCKLIST:
            return m.group("ticker").upper(), None
        # Last resort: any word matching a ticker we currently hold. Only
        # reachable when the message already carried a strong exit form.
        held = {t.upper() for t in known_tickers}
        for word in text.split():
            w = re.sub(r"^\W+|\W+$", "", word).upper()
            if w in held:
                return w, None
        return None, None

    def _resolve_expiration(
        self, m: re.Match, text: str, now: datetime, notes: list[str], region_end: int,
    ) -> date | None:
        today = now.date()
        mid = m.group("mid_date")
        if mid is not None:
            if "dte" in mid.lower():
                return self._add_trading_days(today, int(re.sub(r"\D", "", mid)))
            return self._parse_slash_date(mid, today, notes)
        # An explicit Ndte anywhere beats a stray m/d token ("0dte 1/2 size")
        dte = _DTE_RE.search(text)
        if dte:
            return self._add_trading_days(today, int(dte.group("n")))
        for region in ((m.end(), region_end), (0, len(text))):
            for dm in _DATE_RE.finditer(text, *region):
                tail = text[dm.end():dm.end() + 12].lstrip().lower()
                if tail.startswith(_FRACTION_TAILS):
                    continue  # "1/2 size" is a fraction, not January 2nd
                return self._parse_slash_date(dm.group(0), today, notes)
        mm = _MONTH_DATE_RE.search(text)
        if mm:
            month = _MONTHS[mm.group(1)[:3].lower()]
            return self._rolled_date(month, int(mm.group(2)), None, today, notes)
        return None

    def _parse_slash_date(self, token: str, today: date, notes: list[str]) -> date | None:
        try:
            parts = [int(p) for p in token.split("/")]
            year = None
            if len(parts) == 3:
                year = parts[2] if parts[2] > 999 else 2000 + parts[2]
            return self._rolled_date(parts[0], parts[1], year, today, notes)
        except ValueError:
            notes.append(f"unparseable expiration {token!r}")
            return None

    @staticmethod
    def _rolled_date(month: int, day: int, year: int | None,
                     today: date, notes: list[str]) -> date:
        if year is not None:
            return date(year, month, day)
        candidate = date(today.year, month, day)
        if candidate < today:
            # A date a few days back is a late repost of an expired play —
            # keep it in the past so the risk check rejects it, instead of
            # silently rolling it a year forward.
            if (today - candidate).days <= 7:
                notes.append(f"expiration {month}/{day} already passed")
                return candidate
            candidate = date(today.year + 1, month, day)
            notes.append(f"assumed {month}/{day} means {candidate.year}")
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
