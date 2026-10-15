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
from discord.ext import commands, tasks

from .commands.booster import register_commands
from .commands.level import xp_group
from .commands.listing import parse_hex_colour
from .commands.reviews import reviews_group
from .config import load_config
from .db import close_db, create_tables, init_db, session_scope
from .notifications import announce_level_up
from .services import level as level_service
from .services.booster_react import BoosterReactService, parse_emoji
from .services.booster_role import BoosterRoleService, resolve_notify_channel
from .services.level_rewards import LevelRewardService, sync_member_roles
from .services.reviews import ReviewService

logger = logging.getLogger("bh2")

XP_COOLDOWN_SECONDS = 60


async def _award_message_xp(
    bot: commands.Bot, message: discord.Message
) -> None:
    """Roll XP for a message, apply reward roles on level up, and report it."""
    guild_id = message.guild.id
    user_id = message.author.id
    now = level_service.utcnow()

    async with session_scope() as session:
        user_xp = await level_service.get_user_xp(
            session, guild_id=guild_id, user_id=user_id
        )
        if not level_service.can_gain_message_xp(user_xp, now, XP_COOLDOWN_SECONDS):
            return
        result = await level_service.add_xp(
            session,
            guild_id=guild_id,
            user_id=user_id,
            amount=level_service.random_xp_gain(),
        )
        await level_service.mark_xp_gained(
            session, guild_id=guild_id, user_id=user_id, now=now
        )
        await session.commit()

    if result.new_level == result.old_level:
        return

    member = message.author
    if await sync_member_roles(
        message.guild, member, result.new_level, bot.level_rewards
    ):
        logger.info(
            "Level up: %s reached level %d", member.id, result.new_level
        )

    await announce_level_up(message.channel, message.author, result.new_level)


async def run() -> None:
    config = load_config()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Database
    init_db(config.mysql_url)
    await create_tables()

    # Services (shared instances exposed on the bot for commands)
    reactor = BoosterReactService()
    booster_roles = BoosterRoleService()
    level_rewards = LevelRewardService(int(config.guild_id))
    reviews = ReviewService(int(config.guild_id))

    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="", intents=intents)
    bot.booster_reacts = reactor
    bot.booster_role_id = int(config.get("booster_role_id", 0) or 0)
    bot.booster_roles = booster_roles
    bot.level_rewards = level_rewards
    bot.reviews = reviews
    bot.bot_updates_channel_id = int(
        config.get("bot_updates_channel_id", 0) or 0
    )
    bot.transparent_embed_image = config.get("transparent_embed_image") or None
    default_color = config.get("default_embed_color")
    bot.default_embed_color = (
        parse_hex_colour(default_color) if default_color else None
    )

    # ---------------- registered slash commands ----------------
    register_commands(bot, guild_id=int(config.guild_id))
    guild = discord.Object(id=int(config.guild_id))
    bot.tree.add_command(xp_group, guild=guild)
    bot.tree.add_command(reviews_group, guild=guild)

    # ------------------------ message reactor ------------------------
    @bot.event
    async def on_message(message: discord.Message) -> None:
        # Only act inside the configured server, and never on bots.
        if message.author.bot:
            return
        if message.guild is None or message.guild.id != int(config.guild_id):
            return

        # XP gain: one roll per message, 60s cooldown globally per member.
        await _award_message_xp(bot, message)

        if not message.content:
            return

        used_emojis: set[str] = set()
        for entry in reactor.find_matches(message.content):
            # A booster is not reacted to on their own message.
            if message.author.id == entry["discord_user_id"]:
                continue
            # Two boosters may share an emoji; only add it once.
            if entry["emoji"] in used_emojis:
                continue
            used_emojis.add(entry["emoji"])
            try:
                await message.add_reaction(parse_emoji(entry["emoji"]))
            except discord.HTTPException as exc:
                logger.warning(
                    "Could not react to message %s: %s", message.id, exc
                )

    # ---------------------- role deletions ----------------------
    @bot.event
    async def on_guild_role_delete(role: discord.Role) -> None:
        if role.guild.id != int(config.guild_id):
            return
        if await booster_roles.delete_by_role(role.id):
            logger.info("Unlinked deleted booster role %s", role.id)

    # ------------------- booster role upkeep -------------------
    # No Members intent: poll daily to catch boosters who stopped boosting.
    # The single-member fetch used by reconcile() does not need the intent.
    @tasks.loop(hours=24)
    async def reconcile_boosters() -> None:
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        channel = await resolve_notify_channel(
            guild, bot.bot_updates_channel_id
        )
        unlinked = await booster_roles.reconcile(guild, channel)
        logger.info("Booster role sync: unlinked %d", len(unlinked))

    @reconcile_boosters.before_loop
    async def _wait_ready() -> None:
        await bot.wait_until_ready()

    # ------------------- Goodreads review updates -------------------
    @tasks.loop(minutes=5)
    async def poll_reviews() -> None:
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        channel = await resolve_notify_channel(guild, reviews.channel_id)
        if channel is None:
            return
        posted = await reviews.post_new_reviews(guild, channel, bot)
        if posted:
            logger.info("Posted %d review update(s)", posted)

    @poll_reviews.before_loop
    async def _wait_ready_reviews() -> None:
        await bot.wait_until_ready()

    # ------------------------- startup -------------------------
    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (%s)", bot.user, bot.user.id)

        await reactor.reload()
        logger.info("Loaded %d booster reaction(s)", len(reactor.records))

        await level_rewards.reload()
        logger.info("Loaded %d level reward(s)", len(level_rewards.rewards))

        await reviews.reload()
        logger.info("Review channel: %s", reviews.channel_id)

        if not reconcile_boosters.is_running():
            reconcile_boosters.start()
        if not poll_reviews.is_running():
            poll_reviews.start()

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