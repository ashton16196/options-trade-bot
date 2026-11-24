# Discord Options Alert Copy-Trader

Watches a Discord channel for options trade alerts, copies fresh entries
(≤ 60s old by default) into your brokerage account, then exits when the
group posts a close **or** when your own take-profit % hits — whichever
comes first.

```
Discord channel ──► parser ──► risk checks ──► broker (paper / live)
                                                  │
                    exits ◄── take-profit/stop monitor + group close alerts
```

## ⚠️ Read this first

- **This bot spends real money on the word of strangers.** Signal groups
  misfire, front-run their own alerts, and post ambiguous messages. The
  parser is tested but no parser is perfect. Options can go to zero the
  same day. Never run it with money you can't lose, and start in paper
  mode — it's the default for a reason.
- **Two hard constraints** shaped this design (verified Aug 2026):
  1. **Robinhood has no official API for options** — only crypto. The
     unofficial `robin_stocks` route still works, but Robinhood killed
     authenticator-app 2FA in 2025, so logins need in-app approval
     roughly daily (no fully unattended bot), and automated access is
     against their terms — accounts have been frozen for exactly this.
     That's why the default broker is **Alpaca** (official API, free
     instant paper trading, commission-free options). Robinhood is still
     supported if you accept those risks: `broker: robinhood`.
  2. **Discord doesn't let you programmatically read a server you don't
     manage.** Your bot can only be invited to a server by its admins.
     The legit paths are in [Getting the alerts](#getting-the-alerts-out-of-discord).
- The bot must run on a machine that stays on during market hours — your
  PC, a Raspberry Pi, or a ~$5/mo VPS. It is not a hosted service.

## Getting the alerts out of Discord

Ranked from best to worst:

1. **Ask the group what they offer.** Many paid groups have a webhook
   feed, an API, or a Telegram mirror. One question can delete this whole
   problem.
2. **Channel Following (fully ToS-compliant).** If the group posts alerts
   in an *Announcement* channel: create your own free Discord server,
   right-click their alert channel → **Follow**, target a channel in your
   server, and invite this bot to *your* server. Published alerts arrive
   as crossposts and this bot reads them (embeds included) like any other
   message. Limits: only messages the group explicitly "Publishes"
   propagate, max 10 publishes/hour per source server.
3. **Telegram mirror.** If the group mirrors to Telegram: Telegram's API
   *permits* user-account automation (unlike Discord). The listener here
   is swappable — a Telethon-based listener is a small add-on; ask and
   I'll wire it in.
4. **Watch your own screen (`source: browser`).** The bot opens Discord's
   web app in a real Chrome window on your machine; you log in once, open
   the alert channel, and it reads new messages straight off the page —
   no bot token, no access to their server needed beyond your own
   membership. It reads the page's *text* rather than OCR-ing pixel
   screenshots, because OCR misreads like ".52" → "52" spend real money;
   and it never clicks anything — orders still go through the broker API.
   Honestly: the script sends nothing and makes no Discord API calls of
   its own, so it's far lower-risk than a self-bot and realistically very
   hard to detect, but it's still a gray zone under Discord's
   automated-accounts rule — options 1–3 are the clean ones. Requires the
   machine to stay on and awake with the window open, and
   `pip install playwright && playwright install chromium`.
5. **A user-token "self-bot" on Discord.** This is what most copy-trade
   tools quietly do. It **violates Discord ToS** and risks termination of
   the very account that holds your paid membership. This repo doesn't
   ship one; if you go that route you own that risk.

## Quickstart (paper trading, ~15 minutes)

```bash
git clone <this repo> && cd options-trade-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # then edit: channel_ids at minimum
cp .env.example .env                 # then edit: tokens/keys below
python run.py
```

You need three things in `.env` / `config.yaml`:

1. **A Discord bot token** — [discord.com/developers/applications](https://discord.com/developers/applications)
   → New Application → Bot → copy token into `DISCORD_BOT_TOKEN`. Under
   *Privileged Gateway Intents* enable **Message Content Intent**. Invite
   it to your server: OAuth2 → URL Generator → scope `bot` → permissions
   *View Channels* + *Read Message History* → open the generated URL.
2. **Alpaca paper keys** — [app.alpaca.markets](https://app.alpaca.markets)
   → generate paper API keys (instant, free) → `ALPACA_API_KEY` /
   `ALPACA_SECRET_KEY`. Options are enabled by default in paper.
3. **Channel IDs to watch** — Discord Settings → Advanced → Developer
   Mode, then right-click the alert channel → Copy Channel ID → put it in
   `config.yaml` under `discord.channel_ids`.

The bot logs every message it parses, every trade it takes or skips (and
why), to the console and `bot.log`.

## What it does with an alert

- `BTO SPY 640C 8/15 @ .52` → risk checks → buy limit at ask + 2%,
  cancel if unfilled in 45s. Understands `$TSLA 250p 12/19 @ 2.10`,
  `bought aapl 230c 0dte`, `NVDA 190 calls 8/22`, date-before-strike, etc.
- `STC SPY 640C @ .95` / `selling my tsla puts` / `all out $COIN` →
  sells matching open positions at bid − 5%.
- `trimmed half NVDA 190C` → sells `trim_fraction` of the position.
- Meanwhile the monitor polls every 5s and sells on its own when your
  `take_profit_pct` (or optional `stop_loss_pct`) hits — whichever comes
  first, group exit or your target. Positions expiring today get force-
  closed at 15:50 ET so you're never holding into the bell.
- Close alerts that can't execute right now aren't lost: an exit posted
  after the bell, or while an order on that position is still in flight,
  is queued and executed on the monitor's next eligible tick. A close
  alert that lands before your entry even filled cancels the entry.
- "watching…", "might buy…", long DD posts, market chatter ("SPY selling
  off", "TSLA breaking out"), and stale alerts (> 60s by Discord's own
  timestamp) are deliberately ignored — and an exit alert naming puts
  will never touch your calls, or the wrong strike.

## Safety rails (all in `config.yaml`)

| Rail | Default |
|---|---|
| Mode | `paper` — no real orders |
| Max premium per trade | $500 |
| Contracts per trade | 1 |
| Max open positions | 3 |
| Daily realized-loss halt | $750 |
| Signal freshness | 60s |
| Slippage guard | skip if ask ran >25% past alert price |
| Kill switch | `touch HALT` in the repo dir — instant stop to new entries |

Live mode requires **both** `mode: live` in config and
`TRADE_BOT_CONFIRM_LIVE=yes` in `.env`, so you can't go live by accident.

## Going live checklist

1. Paper trade alongside the group for **at least two weeks**. Check
   `bot.log` daily: did it catch every entry? Skip every non-alert?
2. Grep the log for `parser note:` lines — each is an assumption the
   parser made. Add any misread formats to `tests/test_parser.py`, fix,
   re-run `pytest`.
3. Alpaca live: apply for options (dashboard → Account → Configure),
   fund the account, switch `.env` keys to live keys.
4. Set `mode: live`, `TRADE_BOT_CONFIRM_LIVE=yes`, start small
   (`contracts_per_trade: 1`, tight `max_premium_per_trade`).

## Running it 24/5

Any always-on Linux box. With systemd:

```ini
# /etc/systemd/system/tradebot.service
[Unit]
Description=Discord options copy-trader
After=network-online.target
[Service]
WorkingDirectory=/home/you/options-trade-bot
ExecStart=/home/you/options-trade-bot/.venv/bin/python run.py
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
```

State lives in `tradebot.db` (SQLite), so restarts don't lose open
positions or re-trade already-seen alerts.

## Tests

```bash
pip install pytest && python -m pytest tests/ -q
```

The parser suite is the money-critical one — every alert format your
group uses should have a test there.

## Known limitations

- US market holidays aren't modeled (orders would just sit and time out).
- Multi-leg spreads aren't parsed or traded — single-leg calls/puts only.
- Edited Discord messages are not re-read: the bot acts on the original
  text only, so an alert corrected via edit is not reconciled.
- Entries take only the first contract in a message (multi-contract
  *closes* are handled, one close per contract).
- Robinhood mode: quotes/orders work but see
  `bot/brokers/robinhood.py` for the login-babysitting reality.
