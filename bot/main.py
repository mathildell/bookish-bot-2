"""Bot entrypoint.

Wires everything together:
- loads the single config file
- connects to MySQL (async SQLAlchemy)
- registers the admin slash commands
- listens for messages and reacts to booster aliases
"""

from __future__ import annotations

import logging
from datetime import time as dt_time, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .commands.birthdays import birthday_group
from .commands.booster import register_commands
from .commands.bot import bot_group
from .commands.bot_say import parse_message_link
from .commands.chat import chat_command, handle_mention
from .commands.chatbot import chatbot_group
from .commands.intro import (
    IntroduceView,
    IntroRefreshView,
    intro_group,
    repost_sticky,
    resolve_intro_channel,
)
from .commands.level import (
    XpLeaderboardFallbackView,
    weekly_leaderboard_embed,
    xp_group,
)
from .commands.listing import parse_hex_colour, set_transparent_image
from .commands.ping import ping_command
from .commands.randomize import RandomizeRerollFallbackView, randomize_command
from .commands.reminders import (
    BUTTON_LIFETIME_SECONDS,
    REMINDER_VIEW_KIND,
    ReminderActionsFallbackView,
    ReminderActionsView,
    reminder_embed,
    reminders_group,
    schedule_description,
)
from .commands.releases import ReleaseListFallbackView, releases_group
from .commands.reviews import reviews_group
from .commands.settings import settings_group
from .commands.streaks import (
    STREAK_ACTION_LIFETIME_SECONDS,
    STREAK_LOG_VIEW_KIND,
    StreakEditButtonView,
    StreakLeaderboardFallbackView,
    StreakLogView,
    StreakRescueView,
    streaks_group,
)
from .commands.views import (
    disable_view_message,
    track_view,
)
from .commands.tickets import (
    OpenTicketView,
    TicketCloseView,
    TicketReopenView,
    ensure_sticky as ensure_ticket_sticky,
    resolve_ticket_channel,
    tickets_group,
)
from .commands.biweekly_theme import (
    ThemeSuggestView,
    ThemeSuggestionReviewView,
    biweekly_theme_group,
)
from .config import load_config
from .db import close_db, create_tables, init_db, session_scope
from .notifications import (
    announce_level_up,
    announce_streak_closed,
    announce_theme_failed,
)
from .services import level as level_service
from .services.birthdays import BirthdayService
from .services.booster_react import BoosterReactService, parse_emoji
from .services.booster_role import BoosterRoleService, resolve_notify_channel
from .services.chat import ChatService
from .services.coupons import DEFAULT_FALLBACK_IMAGE, CouponService
from .services.intro import IntroService
from .services.level import WeeklyLeaderboardService
from .services.level_rewards import LevelRewardService, sync_member_roles
from .services.member_profiles import MemberProfileService
from .services.members import resolve_members
from .services.reference import post_reference
from .services.release_sources import BookScrapeError, ReleaseScraper
from .services.releases import ReleaseService
from .services.reminders import ReminderService, is_stale
from .services.reviews import ReviewService
from .services.settings import BotSettingsService
from .services.streaks import Reminder, StreakService
from .services.tickets import TicketService
from .services.view_messages import ViewMessageService
from .services.biweekly_theme import BiweeklyThemeService, ThemeImageError
from .services.image_hosting import ImageHost
from .services.xp_boosts import XpBoostService, apply_boost, boost_percent
from .texts import interpolate, t, t_choice_template

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

    base_amount = level_service.random_xp_gain()
    boosts = getattr(bot, "xp_boosts", None)
    percent = 0
    if boosts is not None:
        channel = message.channel
        channel_ids: list[int] = [channel.id]
        parent_id = getattr(channel, "parent_id", None)
        if parent_id:
            channel_ids.append(parent_id)
        role_ids = [role.id for role in getattr(message.author, "roles", [])]
        percent = boost_percent(
            boosts.role_boosts,
            boosts.channel_boosts,
            role_ids=role_ids,
            channel_ids=channel_ids,
        )
    amount = apply_boost(base_amount, percent)

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
            amount=amount,
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


async def _post_streak_reminders(
    client: discord.Client,
    channel: discord.abc.Messageable,
    reminders: list[Reminder],
    *,
    guild: discord.Guild | None = None,
    colour: discord.Colour | None,
) -> list[Reminder]:
    """Post the due reminders, returning only the ones actually delivered."""
    groups = (
        ("custom", "streaks.reminders.title", "streaks.reminders.content"),
        (
            "last_chance",
            "streaks.reminders.last_chance_title",
            "streaks.reminders.last_chance_content",
        ),
    )
    sent: list[Reminder] = []
    for kind, title_key, content_key in groups:
        due = [
            reminder for reminder in reminders if reminder.kind == kind
        ]
        if not due:
            continue
        members = await resolve_members(
            guild, [reminder.goal.user_id for reminder in due]
        )
        mentions = ", ".join(
            f"<@{reminder.goal.user_id}>" for reminder in due
        )
        names = ", ".join(
            member.display_name
            if member is not None
            else t("streaks.reminders.unknown_member")
            for member in members
        )
        template = t_choice_template(content_key)
        embed = discord.Embed(
            title=t(title_key),
            description=interpolate(template, mentions=names),
            colour=colour,
        )
        embed.set_footer(text=t("streaks.reminders.footer"))
        set_transparent_image(embed, client)
        view = StreakLogView()
        try:
            message = await channel.send(
                content=mentions, embed=embed, view=view
            )
        except discord.HTTPException:
            logger.warning(
                "Could not post %s streak reminder", kind, exc_info=True
            )
            continue
        view.message = message
        await track_view(
            client,
            message,
            kind=STREAK_LOG_VIEW_KIND,
            lifetime=STREAK_ACTION_LIFETIME_SECONDS,
        )
        sent.extend(due)
    return sent


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
    xp_boosts = XpBoostService(int(config.guild_id))
    reviews = ReviewService(int(config.guild_id))
    intros = IntroService(int(config.guild_id))
    releases = ReleaseService(int(config.guild_id))
    streaks = StreakService(int(config.guild_id))
    reminders = ReminderService(int(config.guild_id))
    birthdays = BirthdayService(int(config.guild_id))
    tickets = TicketService(int(config.guild_id))
    weekly = WeeklyLeaderboardService(int(config.guild_id))
    image_host = ImageHost(config.get("imgbb_api_key"))
    biweekly_theme = BiweeklyThemeService(int(config.guild_id), image_host)
    bot_settings = BotSettingsService(int(config.guild_id))
    view_messages = ViewMessageService(int(config.guild_id))
    coupons = CouponService(config.get("imgbb_api_key"))
    release_scraper = ReleaseScraper()
    member_profiles = MemberProfileService(int(config.guild_id))
    chat_service = ChatService(config.get("ai"))

    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="", intents=intents)
    bot.booster_reacts = reactor
    bot.booster_role_id = int(config.get("booster_role_id", 0) or 0)
    bot.booster_roles = booster_roles
    bot.level_rewards = level_rewards
    bot.xp_boosts = xp_boosts
    bot.reviews = reviews
    bot.intros = intros
    bot.releases = releases
    bot.streaks = streaks
    bot.reminders = reminders
    bot.birthdays = birthdays
    bot.tickets = tickets
    bot.weekly = weekly
    bot.biweekly_theme = biweekly_theme
    bot.bot_settings = bot_settings
    bot.view_messages = view_messages
    bot.coupons = coupons
    bot.release_scraper = release_scraper
    bot.member_profiles = member_profiles
    bot.chat = chat_service
    # Persistent button on the intro sticky message (works after restarts).
    bot.add_view(IntroduceView())
    # Persistent refresh button on every intro embed (shares one custom id).
    bot.add_view(IntroRefreshView())
    # Persistent "repair streak" button offered on log/view messages.
    bot.add_view(StreakRescueView(is_fallback=True))
    # Persistent ticket panel and ticket close/reopen buttons.
    bot.add_view(OpenTicketView())
    bot.add_view(TicketCloseView())
    bot.add_view(TicketReopenView())
    # Fallbacks for public timed views whose original message-bound views are
    # lost on restart. Their per-message state lives in view_messages.
    bot.add_view(ReminderActionsFallbackView())
    bot.add_view(StreakLogView(timeout=None))
    bot.add_view(StreakEditButtonView())
    bot.add_view(XpLeaderboardFallbackView())
    bot.add_view(StreakLeaderboardFallbackView())
    bot.add_view(ReleaseListFallbackView())
    bot.add_view(ThemeSuggestView())
    bot.add_view(ThemeSuggestionReviewView())
    bot.add_view(RandomizeRerollFallbackView())
    # Bot-wide settings are loaded from the database in on_ready; start with
    # the disabled/unset defaults so nothing fires before the first reload.
    bot.bot_updates_channel_id = 0
    bot.transparent_embed_image = None
    bot.default_embed_color = None
    bot.reference_forum_channel_id = 0
    bot.reference_channel_id = 0
    bot.reference_message_id = 0
    bot.admin_test_channel_id = 0
    bot.coupon_fallback_image = DEFAULT_FALLBACK_IMAGE

    # ---------------- registered slash commands ----------------
    register_commands(bot, guild_id=int(config.guild_id))
    guild = discord.Object(id=int(config.guild_id))
    bot.tree.add_command(xp_group, guild=guild)
    bot.tree.add_command(reviews_group, guild=guild)
    bot.tree.add_command(intro_group, guild=guild)
    bot.tree.add_command(releases_group, guild=guild)
    bot.tree.add_command(streaks_group, guild=guild)
    bot.tree.add_command(reminders_group, guild=guild)
    bot.tree.add_command(birthday_group, guild=guild)
    bot.tree.add_command(tickets_group, guild=guild)
    bot.tree.add_command(bot_group, guild=guild)
    bot.tree.add_command(settings_group, guild=guild)
    bot.tree.add_command(ping_command, guild=guild)
    bot.tree.add_command(randomize_command, guild=guild)
    bot.tree.add_command(biweekly_theme_group, guild=guild)
    bot.tree.add_command(chat_command, guild=guild)
    bot.tree.add_command(chatbot_group, guild=guild)

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

        # Bot mention chat
        if bot.user is not None and bot.user in message.mentions:
            try:
                await handle_mention(message, bot)
            except Exception:
                logger.exception("Mention handling failed")
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

    # ------------------- member leaves -------------------
    @bot.event
    async def on_member_remove(member: discord.Member) -> None:
        if member.guild.id != int(config.guild_id):
            return
        try:
            service = bot.birthdays
            removed, had_linked = await service.remove_birthday_and_unlink_content(member.id)
            if removed or had_linked:
                channel = await resolve_notify_channel(
                    member.guild, getattr(bot, "bot_updates_channel_id", 0)
                )
                if channel is not None:
                    try:
                        await channel.send(
                            t(
                                "birthday.cleanup.member_left",
                                user=str(member),
                            )
                        )
                    except Exception:
                        logger.exception("Failed to post birthday cleanup notice")
        except Exception:
            logger.exception("Failed to clean up birthday data on member leave")
        try:
            profiles = getattr(bot, "member_profiles", None)
            if profiles is not None:
                await profiles.delete_for_member(member.id)
        except Exception:
            logger.exception("Failed to clean up member profile on leave")



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

    @tasks.loop(time=HOURLY_TIMES)
    async def post_releases() -> None:
        # Runs hourly so the post hour can be changed at runtime; only the
        # configured UTC hour actually posts.
        if discord.utils.utcnow().hour != releases.post_hour:
            return
        await post_releases_now()

    @post_releases.before_loop
    async def _wait_ready_releases() -> None:
        await bot.wait_until_ready()
        # Catch up on anything missed while the bot was down; the work is
        # idempotent thanks to the announcement/monthly markers.
        try:
            await post_releases_now()
            await releases.purge_expired_drafts()
        except Exception:
            logger.exception("Release catch-up failed")

    @tasks.loop(time=DAILY_TIMES)
    async def purge_releases() -> None:
        await purge_old_releases()

    @purge_releases.before_loop
    async def _wait_ready_release_purge() -> None:
        await bot.wait_until_ready()
        try:
            await purge_old_releases()
        except Exception:
            logger.exception("Release purge catch-up failed")

    # ---------------------- reading streak upkeep ----------------------
    async def send_streak_reminders(guild: discord.Guild) -> None:
        reminders = await streaks.due_reminders()
        if not reminders:
            return
        channel = await resolve_notify_channel(guild, streaks.channel_id)
        if channel is None:
            logger.warning("Streak reminders due but no channel is set")
            return
        sent = await _post_streak_reminders(
            bot,
            channel,
            reminders,
            guild=guild,
            colour=getattr(bot, "default_embed_color", None),
        )
        await streaks.mark_reminder(sent)

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
            await announce_streak_closed(
                channel,
                closed=closed,
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

    # ------------------------ member reminders ------------------------
    async def _post_reminder_message(
        channel: discord.abc.Messageable,
        bot: commands.Bot,
        *,
        user_id: int,
        task_text: str,
        description: str | None,
        reminder_id: int | None,
        role_id: int | None = None,
    ) -> discord.Message | None:
        """Ping the owner in ``channel`` with the reminder embed and buttons."""
        view = ReminderActionsView(
            reminder_id, user_id, task_text, role_id=role_id
        )
        content = f"<@{user_id}>"
        if role_id is not None:
            content = f"{content} <@&{role_id}>"
        try:
            message = await channel.send(
                content=content,
                embed=reminder_embed(bot, task_text, description=description),
                view=view,
            )
        except discord.HTTPException:
            logger.warning(
                "Could not post reminder %s", reminder_id, exc_info=True
            )
            return None
        view.message = message
        await track_view(
            bot,
            message,
            kind=REMINDER_VIEW_KIND,
            lifetime=BUTTON_LIFETIME_SECONDS,
            state={"reminder_id": reminder_id},
            author_id=user_id,
        )
        return message

    async def post_member_reminders_now() -> None:
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        now = level_service.utcnow()

        for reminder in await reminders.due(now):
            channel = await resolve_notify_channel(guild, reminder.channel_id)
            if channel is None:
                logger.warning(
                    "Reminder %s has no reachable channel", reminder.id
                )
            elif not is_stale(reminder.next_run_at, now):
                await _post_reminder_message(
                    channel,
                    bot,
                    user_id=reminder.user_id,
                    task_text=reminder.message,
                    description=schedule_description(reminder),
                    reminder_id=reminder.id,
                    role_id=reminder.role_id,
                )
            await reminders.mark_done(reminder, now)

        for snooze in await reminders.due_snoozes(now):
            channel = await resolve_notify_channel(guild, snooze.channel_id)
            if channel is None:
                logger.warning(
                    "Snoozed reminder %s has no reachable channel", snooze.id
                )
            elif not is_stale(snooze.due_at, now):
                await _post_reminder_message(
                    channel,
                    bot,
                    user_id=snooze.user_id,
                    task_text=snooze.message,
                    description=None,
                    reminder_id=snooze.source_reminder_id,
                    role_id=snooze.role_id,
                )
            await reminders.discard_snooze(snooze)

    @tasks.loop(time=FIVE_MINUTE_TIMES)
    async def post_member_reminders() -> None:
        await post_member_reminders_now()

    @post_member_reminders.before_loop
    async def _wait_ready_reminders() -> None:
        await bot.wait_until_ready()
        # Catch up on reminders that came due while the bot was down; the
        # 24h staleness rule keeps old ones from being posted on restart.
        try:
            await post_member_reminders_now()
        except Exception:
            logger.exception("Reminder catch-up failed")

    # ------------------------ birthday upkeep ------------------------
    async def post_birthdays_now() -> None:
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        due = await birthdays.due_birthdays()
        if due:
            channel = await resolve_notify_channel(
                guild, birthdays.channel_id or 0
            )
            if channel is None:
                logger.warning(
                    "Birthdays are due but no announcement channel is set; "
                    "awarding gifts and retrying the posts later"
                )
            posted = await birthdays.celebrate(
                guild=guild,
                channel=channel,
                client=bot,
                birthdays=due,
            )
            if posted:
                logger.info("Posted %d birthday(s)", posted)
        await birthdays.expire_roles(guild)

    @tasks.loop(time=FIVE_MINUTE_TIMES)
    async def birthday_upkeep() -> None:
        await post_birthdays_now()

    @birthday_upkeep.before_loop
    async def _wait_ready_birthdays() -> None:
        await bot.wait_until_ready()
        try:
            await post_birthdays_now()
        except Exception:
            logger.exception("Birthday catch-up failed")

    # ------------------------ timed view upkeep ------------------------
    async def expire_view_buttons_now() -> None:
        rows = await view_messages.expired()
        if not rows:
            return
        cleared = 0
        for row in rows:
            guild = bot.get_guild(row.guild_id)
            if guild is None:
                await view_messages.delete(row.message_id)
                continue
            channel = await resolve_notify_channel(guild, row.channel_id)
            if channel is None:
                await view_messages.delete(row.message_id)
                continue
            try:
                message = await channel.get_partial_message(
                    row.message_id
                ).fetch()
            except discord.NotFound:
                await view_messages.delete(row.message_id)
                continue
            except discord.HTTPException:
                logger.warning(
                    "Could not fetch timed view message %s",
                    row.message_id,
                    exc_info=True,
                )
                continue
            try:
                await disable_view_message(message)
            except discord.NotFound:
                await view_messages.delete(row.message_id)
                continue
            except discord.HTTPException:
                logger.warning(
                    "Could not disable timed view message %s",
                    row.message_id,
                    exc_info=True,
                )
                continue
            await view_messages.delete(row.message_id)
            cleared += 1
        if cleared:
            logger.info("Disabled %d expired view message(s)", cleared)

    @tasks.loop(time=FIVE_MINUTE_TIMES)
    async def expire_view_buttons() -> None:
        await expire_view_buttons_now()

    @expire_view_buttons.before_loop
    async def _wait_ready_view_buttons() -> None:
        await bot.wait_until_ready()
        try:
            await expire_view_buttons_now()
        except Exception:
            logger.exception("Timed view catch-up failed")

    async def purge_view_messages_now() -> None:
        await expire_view_buttons_now()
        cutoff = level_service.utcnow() - timedelta(days=30)
        removed = await view_messages.purge_before(cutoff)
        if removed:
            logger.info("Purged %d stale view message row(s)", removed)

    @tasks.loop(time=DAILY_TIMES)
    async def purge_view_messages() -> None:
        await purge_view_messages_now()

    @purge_view_messages.before_loop
    async def _wait_ready_view_purge() -> None:
        await bot.wait_until_ready()
        try:
            await purge_view_messages_now()
        except Exception:
            logger.exception("Timed view purge catch-up failed")

    # ------------------------ ticket upkeep ------------------------
    async def expire_ticket_buttons_now() -> None:
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        removed = await tickets.expire_reopen_buttons(guild)
        if removed:
            logger.info("Expired %d ticket Reopen button(s)", removed)

    @tasks.loop(time=FIVE_MINUTE_TIMES)
    async def expire_ticket_buttons() -> None:
        await expire_ticket_buttons_now()

    @expire_ticket_buttons.before_loop
    async def _wait_ready_tickets() -> None:
        await bot.wait_until_ready()
        try:
            await expire_ticket_buttons_now()
        except Exception:
            logger.exception("Ticket button expiry catch-up failed")

    # ---------------------- weekly XP leaderboard ----------------------
    async def post_weekly_leaderboard_now() -> None:
        now = level_service.utcnow()
        if not weekly.is_due(now):
            return
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        channel = await resolve_notify_channel(guild, weekly.channel_id or 0)
        if channel is None:
            logger.warning(
                "Weekly leaderboard is due but no channel is set"
            )
            return

        week_start = level_service.week_start_for(now.date())
        rows = await weekly.top(
            week_start=week_start,
            limit=level_service.WEEKLY_LEADERBOARD_SIZE,
        )
        members = await resolve_members(guild, [row.user_id for row in rows])
        ranked = [(row, index + 1) for index, row in enumerate(rows)]
        try:
            await channel.send(
                embed=weekly_leaderboard_embed(
                    bot,
                    ranked,
                    members,
                    week_start=week_start,
                )
            )
        except discord.HTTPException:
            logger.warning(
                "Could not post the weekly XP leaderboard", exc_info=True
            )
            return

        await weekly.mark_posted(now.date())
        logger.info("Posted the weekly XP leaderboard")

    @tasks.loop(time=HOURLY_TIMES)
    async def post_weekly_leaderboard() -> None:
        await post_weekly_leaderboard_now()

    @post_weekly_leaderboard.before_loop
    async def _wait_ready_weekly() -> None:
        await bot.wait_until_ready()
        try:
            await post_weekly_leaderboard_now()
        except Exception:
            logger.exception("Weekly leaderboard catch-up failed")

    # ---------------------- bi-weekly theme posts ----------------------
    async def post_biweekly_theme_now() -> None:
        now = level_service.utcnow()
        if not biweekly_theme.is_due(now):
            return
        guild = bot.get_guild(int(config.guild_id))
        if guild is None:
            return
        channel = await resolve_notify_channel(
            guild, biweekly_theme.channel_id or 0
        )
        if channel is None:
            logger.warning("Theme is due but no channel is set")
            return
        try:
            card = await biweekly_theme.post_card(
                channel, bot, view=ThemeSuggestView()
            )
        except ThemeImageError as exc:
            logger.warning("Could not create the theme image", exc_info=True)
            updates = await resolve_notify_channel(
                guild, bot.bot_updates_channel_id
            )
            await announce_theme_failed(
                updates,
                title=exc.card.title,
                error=exc.cause,
                colour=bot.default_embed_color,
            )
            await biweekly_theme.mark_attempted(now.date())
            return
        except discord.HTTPException:
            logger.warning("Could not post the theme", exc_info=True)
            return
        if card is None:
            logger.warning("Theme is due but the deck is empty")
            return
        await biweekly_theme.mark_posted(now.date())
        logger.info("Posted the bi-weekly theme")

    @tasks.loop(time=HOURLY_TIMES)
    async def post_biweekly_theme() -> None:
        # Runs hourly so the post hour can be changed at runtime; a run
        # missed while the bot was down is skipped, not made up.
        await post_biweekly_theme_now()

    # ------------------------- startup -------------------------
    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (%s)", bot.user, bot.user.id)

        await reactor.reload()
        logger.info("Loaded %d booster reaction(s)", len(reactor.records))

        await level_rewards.reload()
        logger.info("Loaded %d level reward(s)", len(level_rewards.rewards))

        await xp_boosts.reload()
        logger.info(
            "Loaded %d role boost(s) and %d channel boost(s)",
            len(xp_boosts.role_boosts),
            len(xp_boosts.channel_boosts),
        )

        await reviews.reload()
        logger.info("Review channel: %s", reviews.channel_id)

        await bot_settings.reload()
        bot.bot_updates_channel_id = bot_settings.updates_channel_id or 0
        bot.transparent_embed_image = bot_settings.transparent_embed_image
        default_color = bot_settings.default_embed_color
        bot.default_embed_color = (
            parse_hex_colour(default_color) if default_color else None
        )
        bot.reference_forum_channel_id = (
            bot_settings.reference_forum_channel_id or 0
        )
        reference_link = bot_settings.reference_message_link
        reference = parse_message_link(reference_link) if reference_link else None
        bot.reference_channel_id = reference[0] if reference else 0
        bot.reference_message_id = reference[1] if reference else 0
        bot.admin_test_channel_id = bot_settings.admin_test_channel_id or 0
        bot.coupon_fallback_image = (
            bot_settings.coupon_fallback_image or DEFAULT_FALLBACK_IMAGE
        )
        chat_svc = getattr(bot, "chat", None)
        if chat_svc is not None:
            try:
                chat_svc.configure(
                    getattr(bot_settings, "chat_model", None),
                    getattr(bot_settings, "chat_persona", None),
                    getattr(bot_settings, "chat_prompt_name", None),
                    getattr(bot_settings, "chat_prompt_info", None),
                )
            except Exception:
                pass

        await tickets.reload()
        logger.info("Ticket channel: %s", tickets.channel_id)
        ticket_channel = await resolve_ticket_channel(bot, tickets.channel_id)
        if ticket_channel is not None:
            try:
                await ensure_ticket_sticky(bot, ticket_channel)
            except discord.HTTPException:
                logger.warning(
                    "Could not restore the ticket panel", exc_info=True
                )

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

        await birthdays.reload()
        logger.info(
            "Birthday channel: %s (role %s)",
            birthdays.channel_id,
            birthdays.role_id,
        )

        await weekly.reload()
        logger.info(
            "Weekly leaderboard channel: %s (Sunday %02d:00 UTC)",
            weekly.channel_id,
            weekly.post_hour,
        )

        await biweekly_theme.reload()
        logger.info(
            "Bi-weekly theme channel: %s (weekday %s, %s UTC)",
            biweekly_theme.channel_id,
            biweekly_theme.weekday,
            biweekly_theme.hour,
        )

        if not reconcile_boosters.is_running():
            reconcile_boosters.start()
        # Goodreads retired RSS feeds; review polling paused.
        # if not poll_reviews.is_running():
        #     poll_reviews.start()
        if not post_releases.is_running():
            post_releases.start()
        if not purge_releases.is_running():
            purge_releases.start()
        if not streak_hourly.is_running():
            streak_hourly.start()
        if not purge_streak_logs.is_running():
            purge_streak_logs.start()
        if not post_member_reminders.is_running():
            post_member_reminders.start()
        if not birthday_upkeep.is_running():
            birthday_upkeep.start()
        if not expire_ticket_buttons.is_running():
            expire_ticket_buttons.start()
        if not expire_view_buttons.is_running():
            expire_view_buttons.start()
        if not purge_view_messages.is_running():
            purge_view_messages.start()
        if not post_weekly_leaderboard.is_running():
            post_weekly_leaderboard.start()
        if not post_biweekly_theme.is_running():
            post_biweekly_theme.start()

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
        try:
            chat = getattr(bot, "chat", None)
            if chat is not None and hasattr(chat, "close"):
                await chat.close()
        except Exception:
            pass
        await release_scraper.close()
        await close_db()