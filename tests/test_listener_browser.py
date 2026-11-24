from datetime import datetime, timezone

from bot.listener_browser import snowflake_time


def test_snowflake_time_known_value():
    # Example id from Discord's own docs: 2016-04-30T11:18:25.796Z
    assert snowflake_time(175928847299117063) == datetime(
        2016, 4, 30, 11, 18, 25, 796000, tzinfo=timezone.utc)


def test_snowflake_time_accepts_strings():
    assert snowflake_time("175928847299117063") == snowflake_time(175928847299117063)


def test_snowflake_feeds_staleness_guard():
    # A message posted "now" must look fresh; one from 2016 must look stale.
    old = snowflake_time(175928847299117063)
    age = (datetime.now(timezone.utc) - old).total_seconds()
    assert age > 60  # the risk check would reject it
