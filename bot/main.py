"""Bot entrypoint.

Wires everything together:
- loads the single config file
- connects to MySQL (async SQLAlchemy)
- registers the admin slash commands
- listens for messages and reacts to booster aliases
"""

from __future__ import annotations

import logging
from datetime import time as dt_time
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .commands.booster import register_commands
from .commands.bot import bot_group
from .commands.intro import (
    IntroduceView,
    IntroRefreshView,
    intro_group,
    repost_sticky,
    resolve_intro_channel,
)
from .commands.level import xp_group
from .commands.listing import parse_hex_colour
from .commands.ping import ping_command
from .commands.releases import releases_group
from .commands.reviews import reviews_group
from .commands.streaks import StreakRescueView, streaks_group
from .config import load_config
from .db import close_db, create_tables, init_db, session_scope
from .notifications import announce_level_up, announce_streak_closed
from .services import level as level_service
from .services.booster_react import BoosterReactService, parse_emoji
from .services.booster_role import BoosterRoleService, resolve_notify_channel
from .services.intro import IntroService
from .services.level_rewards import LevelRewardService, sync_member_roles
from .services.reference import post_reference
from .services.release_sources import BookScrapeError, ReleaseScraper
from .services.releases import ReleaseService
from .services.reviews import ReviewService
from .services.streaks import StreakService
from .texts import t, t_choice

logger = logging.getLogger("bh2")

XP_COOLDOWN_SECONDS = 60

# The periodic jobs fire on fixed clock times (UTC) instead of counting down
# from whenever the bot happened to start, so their ticks never drift. Naive
# times are interpreted as UTC by discord.py.
HOURLY_TIMES = [dt_time(hour=hour) for hour in range(24)]
FIVE_MINUTE_TIMES = [
    dt_time(hour=hour, minute=minute)
    for hour in range(24)
    for minute in range(0, 60, 5)
]
DAILY_TIMES = [dt_time(hour=4)]


def _release_post_time(config) -> dt_time:
    """Scheduled time for the daily release post, from the config or 13:00 UTC."""
    raw = str(config.get("releases_post_time") or "13:00")
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
    except ValueError:
        hour, minute = 13, 0
    tz = dt_timezone.utc
    tz_name = config.get("timezone")
    if tz_name:
        try:
            tz = ZoneInfo(str(tz_name))
        except Exception:
            logger.warning("Unknown timezone %r; using UTC", tz_name)
    return dt_time(hour=hour, minute=minute, tzinfo=tz)


def _sticky_is_last(
    channel: discord.TextChannel, intros: IntroService
) -> bool:
    """Whether the stored intro sticky is already the channel's last message.

    Only reposting when it has drifted up keeps a restart from deleting and
    recreating a sticky that is already sitting at the bottom of the channel.
    """
    return (
        intros.sticky_message_id is not None
        and channel.last_message_id == intros.sticky_message_id
    )


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
    intros = IntroService(int(config.guild_id))
    releases = ReleaseService(int(config.guild_id))
    streaks = StreakService(int(config.guild_id))
    release_scraper = ReleaseScraper()

    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="", intents=intents)
    bot.booster_reacts = reactor
    bot.booster_role_id = int(config.get("booster_role_id", 0) or 0)
    bot.booster_roles = booster_roles
    bot.level_rewards = level_rewards
    bot.reviews = reviews
    bot.intros = intros
    bot.releases = releases
    bot.streaks = streaks
    bot.release_scraper = release_scraper
    # Persistent button on the intro sticky message (works after restarts).
    bot.add_view(IntroduceView())
    # Persistent refresh button on every intro embed (shares one custom id).
    bot.add_view(IntroRefreshView())
    # Persistent "repair streak" button offered on log/view messages.
    bot.add_view(StreakRescueView())
    bot.bot_updates_channel_id = int(
        config.get("bot_updates_channel_id", 0) or 0
    )
    bot.transparent_embed_image = config.get("transparent_embed_image") or None
    bot.reference_forum_channel_id = int(
        config.get("buddy_read_channel_id", 0) or 0
    )
    bot.reference_channel_id = int(config.get("buddy_read_message_channel_id", 0) or 0)
    bot.reference_message_id = int(config.get("buddy_read_message_id", 0) or 0)
    default_color = config.get("default_embed_color")
    bot.default_embed_color = (
        parse_hex_colour(default_color) if default_color else None
    )

    # ---------------- registered slash commands ----------------
    register_commands(bot, guild_id=int(config.guild_id))
    guild = discord.Object(id=int(config.guild_id))
    bot.tree.add_command(xp_group, guild=guild)
    bot.tree.add_command(reviews_group, guild=guild)
    bot.tree.add_command(intro_group, guild=guild)
    bot.tree.add_command(releases_group, guild=guild)
    bot.tree.add_command(streaks_group, guild=guild)
    bot.tree.add_command(bot_group, guild=guild)
    bot.tree.add_command(ping_command, guild=guild)

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

    # ------------------- pinned forum reference -------------------
    # Repost a fixed reference message and pin it in every new forum thread.
    @bot.event
    async def on_thread_create(thread: discord.Thread) -> None:
        forum_id = getattr(bot, "reference_forum_channel_id", 0)
        if not forum_id or thread.parent_id != forum_id:
            return
        if await post_reference(bot, thread):
            logger.info("Posted reference message in thread %s", thread.id)

    # ------------------- booster role upkeep -------------------
    # No Members intent: poll daily to catch boosters who stopped boosting.
    # The single-member fetch used by reconcile() does not need the intent.
    async def reconcile_boosters_now() -> None:
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        channel = await resolve_notify_channel(
            guild, bot.bot_updates_channel_id
        )
        unlinked = await booster_roles.reconcile(guild, channel)
        logger.info("Booster role sync: unlinked %d", len(unlinked))

    @tasks.loop(time=DAILY_TIMES)
    async def reconcile_boosters() -> None:
        await reconcile_boosters_now()

    @reconcile_boosters.before_loop
    async def _wait_ready() -> None:
        await bot.wait_until_ready()
        try:
            await reconcile_boosters_now()
        except Exception:
            logger.exception("Booster role catch-up failed")

    # ------------------- Goodreads review updates -------------------
    async def poll_reviews_now() -> None:
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        channel = await resolve_notify_channel(guild, reviews.channel_id)
        if channel is None:
            return
        posted = await reviews.post_new_reviews(guild, channel, bot)
        if posted:
            logger.info("Posted %d review update(s)", posted)

    @tasks.loop(time=FIVE_MINUTE_TIMES)
    async def poll_reviews() -> None:
        await poll_reviews_now()

    @poll_reviews.before_loop
    async def _wait_ready_reviews() -> None:
        await bot.wait_until_ready()
        try:
            await poll_reviews_now()
        except Exception:
            logger.exception("Review poll catch-up failed")

    # ------------------- book release announcements -------------------
    async def post_releases_now() -> None:
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        channel = await resolve_notify_channel(guild, releases.channel_id)
        if channel is None:
            return
        posted = await releases.post_due(guild, channel, bot)
        logger.info("Posted %d release announcement(s)", posted)

    async def purge_old_releases() -> None:
        removed = await releases.purge_old_releases()
        if removed:
            logger.info("Removed %d released book(s)", removed)

    @tasks.loop(time=_release_post_time(config))
    async def post_releases() -> None:
        await post_releases_now()
        await purge_old_releases()

    @post_releases.before_loop
    async def _wait_ready_releases() -> None:
        await bot.wait_until_ready()
        # Catch up on anything missed while the bot was down; the work is
        # idempotent thanks to the announcement/montly markers.
        try:
            await post_releases_now()
            await purge_old_releases()
            await releases.purge_expired_drafts()
        except Exception:
            logger.exception("Release catch-up failed")

    # ---------------------- reading streak upkeep ----------------------
    async def send_streak_reminders(guild: discord.Guild) -> None:
        reminders = await streaks.due_reminders()
        if not reminders:
            return
        channel = await resolve_notify_channel(guild, streaks.channel_id)
        if channel is None:
            logger.warning("Streak reminders due but no channel is set")
            return
        groups = (
            ("custom", "streaks.reminders.title", "streaks.reminders.content"),
            (
                "last_chance",
                "streaks.reminders.last_chance_title",
                "streaks.reminders.last_chance_content",
            ),
        )
        for kind, title_key, content_key in groups:
            mentions = [
                f"<@{reminder.goal.user_id}>"
                for reminder in reminders
                if reminder.kind == kind
            ]
            if not mentions:
                continue
            embed = discord.Embed(
                title=t(title_key),
                description=t_choice(content_key, mentions=", ".join(mentions)),
                colour=getattr(bot, "default_embed_color", None),
            )
            embed.set_footer(text=t("streaks.reminders.footer"))
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                logger.warning("Could not post streak reminder", exc_info=True)
        await streaks.mark_reminder(reminders)

    async def streak_hourly_now() -> None:
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        closed = await streaks.close_due()
        if closed:
            logger.info("Closed %d streak(s)", len(closed))
            channel = await resolve_notify_channel(
                guild, bot.bot_updates_channel_id
            )
            for entry in closed:
                await announce_streak_closed(
                    channel,
                    closed=entry,
                    colour=getattr(bot, "default_embed_color", None),
                )
        await send_streak_reminders(guild)

    @tasks.loop(time=HOURLY_TIMES)
    async def streak_hourly() -> None:
        await streak_hourly_now()

    @streak_hourly.before_loop
    async def _wait_ready_streaks() -> None:
        await bot.wait_until_ready()
        # Catch up on streaks that came due while the bot was down instead of
        # waiting for the next hour boundary.
        try:
            await streak_hourly_now()
        except Exception:
            logger.exception("Streak catch-up failed")

    async def purge_streak_logs_now() -> None:
        removed = await streaks.purge_old_logs()
        if removed:
            logger.info("Purged %d stale streak log(s)", removed)

    @tasks.loop(time=DAILY_TIMES)
    async def purge_streak_logs() -> None:
        await purge_streak_logs_now()

    @purge_streak_logs.before_loop
    async def _wait_ready_streak_purge() -> None:
        await bot.wait_until_ready()
        try:
            await purge_streak_logs_now()
        except Exception:
            logger.exception("Streak log purge catch-up failed")

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

        await intros.reload()
        logger.info("Intro channel: %s", intros.channel_id)
        channel = await resolve_intro_channel(bot, intros.channel_id)
        if channel is not None and not _sticky_is_last(channel, intros):
            await repost_sticky(bot, channel)
            logger.info("Reposted intro sticky message")

        await releases.reload()
        logger.info("Release channel: %s", releases.channel_id)
        try:
            await release_scraper.start()
        except BookScrapeError as exc:
            logger.error("Release scraper unavailable: %s", exc.key)
        except Exception:
            logger.exception("Could not start the release scraper")

        await streaks.reload()
        logger.info("Streak channel: %s", streaks.channel_id)

        if not reconcile_boosters.is_running():
            reconcile_boosters.start()
        if not poll_reviews.is_running():
            poll_reviews.start()
        if not post_releases.is_running():
            post_releases.start()
        if not streak_hourly.is_running():
            streak_hourly.start()
        if not purge_streak_logs.is_running():
            purge_streak_logs.start()

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
        await release_scraper.close()
        await close_db()