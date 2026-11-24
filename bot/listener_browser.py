"""Browser watcher: reads alerts from the Discord web app running in a
real Chrome window on YOUR computer — no Discord bot, no bot token, no
access to the group's server needed beyond your own logged-in account.

Why this instead of screenshots + OCR: it is the same "watch what I see"
idea, but it reads the page's actual text, so it cannot misread ".52" as
"52" or "640C" as "540C" — OCR errors spend real money. And nothing ever
clicks anything: orders go through the broker's API, never by driving a
brokerage website's buttons.

Honest status of this approach: the script sends nothing — it never
posts, reacts, or calls Discord's API itself; it only reads what the
official web client has already rendered in your own logged-in browser.
That makes it far lower-risk than a user-token self-bot, and passive
local reading is realistically very hard to detect — but it is still not
the squeaky-clean path. Discord's rules are written against automating
user accounts, and a script-attached browser session lives in a gray
zone. The fully compliant routes remain Channel Following and a Telegram
mirror (see README). Your account, your call.

How to use:
  1. pip install playwright && playwright install chromium
  2. config.yaml: source: browser, and optionally browser.channel_url
     (https://discord.com/channels/<server>/<channel>)
  3. python run.py — a Chrome window opens. Log into Discord the first
     time (the login sticks; it's saved in .browser-profile/). Keep the
     window open with the alert channel visible; minimized is fine, but
     the machine must stay on and awake.

Everything already on screen at startup is treated as history — only
messages that appear AFTER the watcher starts are acted on, and the
message id itself carries Discord's creation timestamp, so the 60-second
staleness guard still works.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DISCORD_EPOCH_MS = 1420070400000

# Collects every rendered message: its snowflake id and its text,
# including embed text (alert services post embeds).
_COLLECT_JS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll("div[id^='message-content-']")) {
    const id = el.id.slice("message-content-".length);
    if (!/^[0-9]+$/.test(id)) continue;
    let text = el.innerText || "";
    const acc = document.getElementById("message-accessories-" + id);
    if (acc && acc.innerText) text += "\\n" + acc.innerText;
    out.push({ id: id, text: text });
  }
  return out;
}
"""


def snowflake_time(snowflake: int | str) -> datetime:
    """A Discord message id encodes its creation time (ms since the
    Discord epoch, shifted left 22 bits)."""
    ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class BrowserListener:
    def __init__(self, handler, *, channel_url: str = "",
                 poll_ms: int = 750, profile_dir: str = ".browser-profile") -> None:
        self.handler = handler  # async fn(text=, message_id=, source=, created_at=)
        self.channel_url = channel_url
        self.poll_ms = poll_ms
        self.profile_dir = profile_dir

    async def run(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "source: browser needs Playwright — run:\n"
                "  pip install playwright && playwright install chromium"
            ) from e
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(Path(self.profile_dir).resolve()),
                headless=False,  # you log in here; it's your own screen
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(self.channel_url or "https://discord.com/channels/@me")
            log.info("Browser watcher is up. If you see a login page, log in and "
                     "open the alert channel — watching starts once messages render.")
            seen: set[str] = set()
            primed = False
            while True:
                try:
                    rows = await page.evaluate(_COLLECT_JS)
                except Exception as e:
                    log.warning("could not read the Discord page (%s) — window "
                                "closed or navigating? retrying", e)
                    await asyncio.sleep(3)
                    continue
                if rows and not primed:
                    seen.update(r["id"] for r in rows)
                    primed = True
                    log.info("watching the open channel (%d message(s) on screen "
                             "treated as history)", len(rows))
                elif primed:
                    for r in rows:
                        if r["id"] in seen or not r["text"].strip():
                            continue
                        seen.add(r["id"])
                        await self.handler(
                            text=r["text"],
                            message_id=r["id"],
                            source="browser",
                            created_at=snowflake_time(r["id"]),
                        )
                if len(seen) > 5000:  # ids sort chronologically; keep the newest
                    seen = set(sorted(seen, key=int)[-2500:])
                await asyncio.sleep(self.poll_ms / 1000)
