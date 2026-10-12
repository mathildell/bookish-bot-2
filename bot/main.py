"""Bot entrypoint.

Wires everything together:
- loads the single config file
- connects to MySQL (async SQLAlchemy)
- registers the admin slash commands
- listens for messages and reacts to booster aliases
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .commands.admin import register_commands
from .config import load_config
from .db import close_db, create_tables, init_db
from .services.booster_react import BoosterReactService, parse_emoji

logger = logging.getLogger("bh2")


async def run() -> None:
    config = load_config()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Database
    init_db(config.mysql_url)
    await create_tables()

    # Reactor service (shared instance exposed on the bot for commands)
    reactor = BoosterReactService()

    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="", intents=intents)
    bot.booster_reacts = reactor

    # ---------------- registered slash commands ----------------
    register_commands(bot, guild_id=int(config.guild_id))

    # ------------------------ message reactor ------------------------
    @bot.event
    async def on_message(message: discord.Message) -> None:
        # Only act on plain text messages inside the configured server
        if message.author.bot:
            return
        if message.guild is None or message.guild.id != int(config.guild_id):
            return
        if not message.content:
            return

        entry = reactor.find_matching(message.content)
        if entry is None:
            return
        if message.author.id == entry["discord_user_id"]:
            return

        try:
            await message.add_reaction(parse_emoji(entry["emoji"]))
        except discord.HTTPException as exc:
            logger.warning(
                "Could not react to message %s: %s", message.id, exc
            )

    # ------------------------- startup -------------------------
    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (%s)", bot.user, bot.user.id)

        await reactor.reload()
        logger.info("Loaded %d booster reaction(s)", len(reactor.records))

        guild = discord.Object(id=int(config.guild_id))
        try:
            synced = await bot.tree.sync(guild=guild)
            logger.info("Synced %d guild command(s)", len(synced))
        except discord.HTTPException as exc:
            logger.error("Failed to sync commands: %s", exc)

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        logger.error("Slash command error: %s", error, exc_info=error)

    try:
        await bot.start(config.discord_token)
    except KeyboardInterrupt:
        await bot.close()
    finally:
        await close_db()