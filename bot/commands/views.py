from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


class ExpiringView(discord.ui.View):
    """A timed view that disables its components when it stops listening."""

    message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                logger.debug("Could not disable an expired view", exc_info=True)
