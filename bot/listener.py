"""Discord listener. Runs as a proper Discord BOT (not a user account):
the bot must be a member of the server whose channels it watches, and the
MESSAGE CONTENT intent must be enabled in the Discord developer portal.

Embeds are flattened into the text because most alert services post their
signals as embeds, and channel-follow crossposts arrive that way too.
"""
from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


class DiscordListener(discord.Client):
    def __init__(self, channel_ids: list[int], handler) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.watched = set(channel_ids)
        self.handler = handler  # async fn(text=..., message_id=..., source=...)

    async def on_ready(self) -> None:
        log.info("Discord connected as %s, watching %d channel(s)",
                 self.user, len(self.watched))
        visible = {c.id for g in self.guilds for c in getattr(g, "text_channels", [])}
        for cid in self.watched - visible:
            log.warning("watched channel %d is not visible to this bot — "
                        "is the bot in that server with read access?", cid)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is not None and message.author.id == self.user.id:
            return
        if self.watched and message.channel.id not in self.watched:
            return
        text = extract_text(message)
        if not text.strip():
            return
        await self.handler(
            text=text,
            message_id=str(message.id),
            source=f"#{message.channel}/{message.author}",
            # Discord's own timestamp — gateway reconnects can replay old
            # messages, and the staleness guard must see the real post time.
            created_at=message.created_at,
        )


def extract_text(message: discord.Message) -> str:
    parts = [message.content or ""]
    for embed in message.embeds:
        for piece in (embed.title, embed.description):
            if piece:
                parts.append(piece)
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
    return "\n".join(p for p in parts if p)
