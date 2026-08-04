"""Parser tests against real-world alert formats. This is the component
where a mistake costs money — add every new format your group uses here."""
from datetime import date, datetime, timezone

import pytest

from bot.models import Action, OptionType
from bot.parser import SignalParser

# A fixed "now": Tuesday 2026-08-04 14:30 UTC (10:30 ET)
NOW = datetime(2026, 8, 4, 14, 30, tzinfo=timezone.utc)


def parse_all(text, *, require_keyword=True, known=frozenset()):
    p = SignalParser(require_action_keyword=require_keyword)
    return p.parse(text, now=NOW, known_tickers=known)


def parse(text, *, require_keyword=True, known=frozenset()):
    sigs = parse_all(text, require_keyword=require_keyword, known=known)
    return sigs[0] if sigs else None


# ---- entries -------------------------------------------------------------

def test_classic_bto():
    s = parse("BTO SPY 640C 8/15 @ .52")
    assert s.action is Action.OPEN
    assert s.contract.ticker == "SPY"
    assert s.contract.strike == 640.0
    assert s.contract.option_type is OptionType.CALL
    assert s.contract.expiration == date(2026, 8, 15)
    assert s.price == 0.52


def test_date_before_strike():
    s = parse("BTO SPY 8/15 640C @ .52")
    assert s.contract.strike == 640.0
    assert s.contract.expiration == date(2026, 8, 15)


def test_dollar_ticker_puts_with_year():
    s = parse("$TSLA 250p 12/19/25 @ 2.10 lets go, buying here")
    assert s.action is Action.OPEN
    assert s.contract.ticker == "TSLA"
    assert s.contract.option_type is OptionType.PUT
    assert s.contract.expiration == date(2025, 12, 19)
    assert s.price == 2.10


def test_lowercase_0dte():
    s = parse("bought aapl 230c 0dte at 1.25")
    assert s.action is Action.OPEN
    assert s.contract.ticker == "AAPL"
    assert s.contract.expiration == NOW.date()
    assert s.price == 1.25


def test_spelled_out_calls():
    s = parse("adding NVDA 190 calls 8/22 @ 3.40")
    assert s.action is Action.OPEN
    assert s.contract.option_type is OptionType.CALL
    assert s.contract.strike == 190.0
    assert s.contract.expiration == date(2026, 8, 22)


def test_decimal_strike():
    s = parse("BTO IWM 226.5C 8/8 @ .80")
    assert s.contract.strike == 226.5


def test_expiry_rolls_to_next_year():
    s = parse("BTO QQQ 570C 1/16 @ 4.20")
    assert s.contract.expiration == date(2027, 1, 16)


def test_price_without_at_symbol():
    s = parse("grabbed SPX 6400C 8/5 for 2.50")
    assert s.price == 2.50


def test_bare_contract_requires_flag():
    assert parse("SPY 640C 8/15 @ .52") is None
    s = parse("SPY 640C 8/15 @ .52", require_keyword=False)
    assert s is not None and s.action is Action.OPEN
    assert s.notes  # flagged as an assumption


def test_open_without_expiry_is_rejected():
    s = parse("BTO SPY 640C @ .52")
    assert s is None


# ---- exits ---------------------------------------------------------------

def test_classic_stc():
    s = parse("STC SPY 640C 8/15 @ .95 (+82%)")
    assert s.action is Action.CLOSE
    assert s.ticker == "SPY"
    assert s.price == 0.95


def test_sold_wins_over_bought_mention():
    s = parse("sold the SPY 640C 8/15 I bought earlier @ .95")
    assert s.action is Action.CLOSE


def test_loose_close_by_type():
    s = parse("selling my tsla puts here")
    assert s.action is Action.CLOSE
    assert s.ticker == "TSLA"
    assert s.option_type is OptionType.PUT
    assert s.contract is None


def test_trim():
    s = parse("trimmed half NVDA 190 calls")
    assert s.action is Action.TRIM
    assert s.ticker == "NVDA"


def test_stopped_out_with_known_ticker():
    s = parse("stopped out of amd here, tough one", known={"AMD"})
    assert s.action is Action.CLOSE
    assert s.ticker == "AMD"


def test_all_out_dollar_ticker():
    s = parse("all out $COIN")
    assert s.action is Action.CLOSE
    assert s.ticker == "COIN"


def test_close_without_target_is_dropped():
    assert parse("selling everything, market looks terrible") is None


# ---- things that must NOT trade ------------------------------------------

def test_commentary_no_action():
    assert parse("SPY looking weak under 640 today") is None


def test_watching_is_not_entry():
    assert parse("watching TSLA 250p 12/19 for a possible entry") is None


def test_long_dd_post_ignored():
    text = ("Here's my thesis on why I might buy SPY 640C 8/15 next week: " * 10)
    assert parse(text) is None


def test_plain_chatter():
    assert parse("good morning everyone, big day ahead") is None
    assert parse("nice win yesterday team") is None


def test_blocklisted_ticker_words():
    # "ALL" / "OUT" / "EOD" must never be read as tickers
    assert parse("BTO calls EOD maybe") is None


def test_close_only_acts_on_known_or_explicit():
    # No $-prefix, no calls/puts word, not a held ticker → no target
    assert parse("sold some stuff today", known={"SPY"}) is None


def test_time_of_day_is_not_a_price():
    s = parse("BTO SPY 640C 8/15 at 10:15")
    assert s is not None and s.price is None


def test_price_still_parses_after_time_fix():
    assert parse("BTO SPY 640C 8/15 @ .52").price == 0.52
    assert parse("grabbed SPX 6400C 8/5 for 2.50").price == 2.50


# ---- regression tests from the adversarial review ------------------------

def test_markdown_bold_action_words():
    s = parse("**BTO** SPY 640C 8/15 @ .52")
    assert s is not None and s.action is Action.OPEN
    s = parse("🚨 BTO SPY 640C 8/15 @ .52 🚨")
    assert s is not None and s.action is Action.OPEN
    s = parse("**STC** SPY 640C 8/15 @ .95")
    assert s is not None and s.action is Action.CLOSE


def test_chatter_never_liquidates_held_positions():
    # every one of these previously parsed as a CLOSE of the held ticker
    assert parse("SPY selling off hard right now", known={"SPY"}) is None
    assert parse("TSLA breaking out", known={"TSLA"}) is None
    assert parse("NVDA closed above 190 yesterday", known={"NVDA"}) is None
    assert parse("SPY stopped right at 640", known={"SPY"}) is None
    assert parse("second half looking better", known={"SPY"}) is None


def test_entry_with_incidental_close_word_is_still_entry():
    s = parse("Breaking out! BTO SPY 650C 8/15 @ .80", known={"SPY"})
    assert s is not None and s.action is Action.OPEN
    assert s.contract.strike == 650.0


def test_sell_target_in_entry_is_not_a_close():
    s = parse("BTO SPY 640C 8/15 @ .52, will sell at 2.00")
    assert s is not None and s.action is Action.OPEN
    assert s.price == 0.52


def test_exit_plan_does_not_veto_entry():
    s = parse("BTO SPY 640C 8/15 @ .52 exit plan 1.00")
    assert s is not None and s.action is Action.OPEN


def test_scaling_into_is_not_a_trim():
    assert parse("scaling into TSLA 250C 8/15 @ 2.10") is None


def test_dte_between_ticker_and_strike():
    s = parse("BTO SPY 0dte 640C @ .52")
    assert s is not None and s.contract.expiration == NOW.date()


def test_dte_beats_size_fraction():
    s = parse("BTO SPY 640C 0dte 1/2 size @ .52")
    assert s is not None and s.contract.expiration == NOW.date()


def test_month_name_expiry():
    s = parse("BTO SPY 640C Aug 15 @ .52")
    assert s is not None and s.contract.expiration == date(2026, 8, 15)


def test_may_is_a_month_not_a_veto():
    s = parse("BTO SPY 640C May 15 @ .52")
    assert s is not None and s.contract.expiration == date(2027, 5, 15)


def test_recent_past_date_stays_in_past_for_risk_check():
    s = parse("BTO SPY 640C 8/3 @ .52")  # yesterday-ish; risk check must reject
    assert s is not None and s.contract.expiration == date(2026, 8, 3)


def test_close_carries_strike_without_expiry():
    s = parse("STC SPY 640C @ .95")
    assert s is not None and s.action is Action.CLOSE
    assert s.strike == 640.0 and s.contract is None
    assert s.option_type is OptionType.CALL


def test_multi_contract_close_yields_signal_per_contract():
    sigs = parse_all("Closing out both: SPY 640C 8/15 @ 1.10, QQQ 570C 8/15 @ 2.30")
    assert [s.ticker for s in sigs] == ["SPY", "QQQ"]
    assert all(s.action is Action.CLOSE for s in sigs)
    assert sigs[0].price == 1.10 and sigs[1].price == 2.30


def test_profit_target_multiplier_is_not_price():
    s = parse("looking for 2x here, BTO SPY 640C 8/15 @ .52")
    assert s is not None and s.price == 0.52


def test_discord_mention_is_not_price():
    s = parse("<@987654321098765432> BTO SPY 640C 8/15 @ .52")
    assert s is not None and s.price == 0.52
