"""Reward the members who handed in the bi-weekly theme.

An admin marks a submission by reacting to the member's message with the
submission emoji. When the next theme is due, everyone with a marked message
gets XP and one random participant wins a bonus. The XP and the winner are
decided and saved first (:class:`ThemeCycle`), then announced, so a restart
at any point neither pays twice nor loses the announcement.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

import discord

from ..db import session_scope
from ..notifications import announce_level_up
from ..texts import t
from . import level as level_service
from .biweekly_theme import BiweeklyThemeService, ThemeCycle
from .booster_react import parse_emoji
from .embeds import new_embed
from .level_rewards import sync_member_roles
from .members import resolve_members
from .roulette import spin_roulette

SUBMISSION_XP = 1000
WINNER_XP = 1000
NEXT_THEME_DELAY = timedelta(minutes=10)
# A run that is older than this when it resumes is abandoned, not made up.
MAX_CYCLE_AGE = timedelta(hours=12)
EMBED_DESCRIPTION_LIMIT = 4000
EMBEDS_PER_MESSAGE = 10

logger = logging.getLogger("bh2.biweekly_theme")


def unique_labels(names: list[str]) -> list[str]:
    """Make roulette labels distinct so a winner maps back to one member."""
    seen: dict[str, int] = {}
    labels: list[str] = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        labels.append(name if count == 1 else f"{name} ({count})")
    return labels


def progress_text(change: level_service.LevelChangeResult, *, xp: int) -> str:
    """Format one member's XP gain like the weekly leaderboard details."""
    progress_start = change.old_total_xp - level_service.total_xp_for_level(
        change.old_level
    )
    progress_end = change.new_total_xp - level_service.total_xp_for_level(
        change.new_level
    )
    needed = level_service.xp_increment_to_reach_level(change.new_level)
    if change.old_level == change.new_level:
        return t(
            "biweekly_theme.settlement.xp_add",
            xp=xp,
            l=change.old_level,
            n1=progress_start,
            n2=progress_end,
            needed=needed,
        )
    return t(
        "biweekly_theme.settlement.xp_level_up",
        xp=xp,
        l1=change.old_level,
        l2=change.new_level,
        n1=progress_start,
        n2=progress_end,
        needed=needed,
    )


def split_lines(lines: list[str], limit: int = EMBED_DESCRIPTION_LIMIT) -> list[str]:
    """Group lines into blocks that fit one embed description."""
    blocks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            blocks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        blocks.append("\n".join(current))
    return blocks


def reaction_matches(reaction: discord.Reaction, raw_emoji: str) -> bool:
    """Whether a reaction is the configured submission emoji."""
    wanted = parse_emoji(raw_emoji)
    if isinstance(wanted, discord.PartialEmoji) and wanted.id is not None:
        return getattr(reaction.emoji, "id", None) == wanted.id
    return _plain(str(reaction.emoji)) == _plain(str(wanted))


def _plain(emoji: str) -> str:
    """Drop the invisible variation selector some keyboards add."""
    return emoji.replace("\ufe0f", "")


def cycle_is_stale(cycle: ThemeCycle, now: datetime) -> bool:
    return now - cycle.started_at > MAX_CYCLE_AGE


async def _is_admin(
    guild: discord.Guild, user: discord.abc.User, cache: dict[int, bool]
) -> bool:
    """Whether ``user`` has Manage Server, fetching the member if uncached."""
    if user.id in cache:
        return cache[user.id]
    member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
    if member is None:
        try:
            member = await guild.fetch_member(user.id)
        except discord.HTTPException:
            member = None
    cache[user.id] = bool(member and member.guild_permissions.manage_guild)
    return cache[user.id]


async def find_participants(
    channel: discord.abc.Messageable,
    *,
    after_message_id: int,
    emoji: str,
) -> list[discord.abc.User]:
    """Authors of messages an admin marked since the theme post.

    A message counts when the bot itself (``/bot react``) or at least one
    member with Manage Server reacted with the submission emoji; each author is listed once, in order of
    their first marked message.
    """
    guild = channel.guild
    participants: dict[int, discord.abc.User] = {}
    admins: dict[int, bool] = {}
    async for message in channel.history(
        limit=None, after=discord.Object(id=after_message_id), oldest_first=True
    ):
        author = message.author
        if author.bot or author.id in participants:
            continue
        for reaction in message.reactions:
            if not reaction_matches(reaction, emoji):
                continue
            if reaction.me:
                participants[author.id] = author
                break
            async for user in reaction.users():
                if await _is_admin(guild, user, admins):
                    participants[author.id] = author
                    break
            break
    return list(participants.values())


async def start_cycle(
    service: BiweeklyThemeService,
    channel: discord.abc.Messageable,
    now: datetime,
) -> ThemeCycle:
    """Find the participants and winner of the previous card and pay them."""
    previous_id = service.pinned_message_id
    members: list[discord.abc.User] = []
    if (
        previous_id is not None
        and service.settled_message_id != previous_id
        and service.pinned_channel_id == getattr(channel, "id", None)
    ):
        members = await find_participants(
            channel,
            after_message_id=previous_id,
            emoji=service.submission_emoji,
        )
    winner = random.choice(members).id if members else None
    return await service.start_cycle(
        now=now,
        message_id=previous_id,
        participants=[member.id for member in members],
        winner=winner,
        submission_xp=SUBMISSION_XP,
        winner_xp=WINNER_XP,
    )


async def _grant_level_up(
    client: discord.Client,
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    member: discord.Member | None,
    change: level_service.LevelChangeResult,
) -> None:
    if member is None or change.old_level == change.new_level:
        return
    await sync_member_roles(
        guild, member, change.new_level, client.level_rewards
    )
    await announce_level_up(channel, member, change.new_level)


async def _post_summary(
    client: discord.Client,
    channel: discord.abc.Messageable,
    cycle: ThemeCycle,
    *,
    dry_run: bool,
) -> None:
    lines = [
        t(
            "biweekly_theme.settlement.entry",
            user=f"<@{user_id}>",
            progress=progress_text(cycle.changes[user_id], xp=SUBMISSION_XP),
        )
        for user_id in cycle.participants
    ]
    title_key = (
        "biweekly_theme.settlement.dry_run_summary_title"
        if dry_run
        else "biweekly_theme.settlement.summary_title"
    )
    embeds = [
        new_embed(
            client,
            title=t(title_key) if index == 0 else None,
            description=(
                t("biweekly_theme.settlement.summary_intro", xp=SUBMISSION_XP)
                + "\n\n"
                + block
                if index == 0
                else block
            ),
        )
        for index, block in enumerate(split_lines(lines))
    ]
    for start in range(0, len(embeds), EMBEDS_PER_MESSAGE):
        await channel.send(embeds=embeds[start : start + EMBEDS_PER_MESSAGE])


async def present_results(
    client: discord.Client,
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    cycle: ThemeCycle,
    *,
    dry_run: bool = False,
) -> None:
    """Post the summary, the roulette and the winner of a cycle.

    A dry run only shows the messages: nobody is pinged and no level
    reward is applied.
    """
    if not cycle.participants or cycle.winner is None:
        return
    members = dict(
        zip(
            cycle.participants,
            await resolve_members(guild, cycle.participants),
        )
    )
    mentions = discord.AllowedMentions.none() if dry_run else None

    await _post_summary(client, channel, cycle, dry_run=dry_run)
    if not dry_run:
        for user_id in cycle.participants:
            await _grant_level_up(
                client, guild, channel, members[user_id], cycle.changes[user_id]
            )

    if len(cycle.participants) == 1:
        await channel.send(
            t(
                "biweekly_theme.settlement.sole_participant",
                user=f"<@{cycle.winner}>",
            ),
            allowed_mentions=mentions,
        )
    else:
        await channel.send(
            t(
                "biweekly_theme.settlement.draw_intro",
                count=len(cycle.participants),
            )
        )
        labels = unique_labels(
            [
                members[user_id].display_name
                if members[user_id] is not None
                else str(user_id)
                for user_id in cycle.participants
            ]
        )
        await spin_roulette(
            channel,
            labels,
            winner=labels[cycle.participants.index(cycle.winner)],
        )

    winner_key = (
        "biweekly_theme.settlement.dry_run_winner"
        if dry_run
        else "biweekly_theme.settlement.winner"
    )
    await channel.send(
        t(
            winner_key,
            user=f"<@{cycle.winner}>",
            progress=progress_text(cycle.winner_change, xp=WINNER_XP),
        ),
        allowed_mentions=mentions,
    )
    if not dry_run:
        await _grant_level_up(
            client, guild, channel, members[cycle.winner], cycle.winner_change
        )


async def announce_cycle(
    client: discord.Client,
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    service: BiweeklyThemeService,
    cycle: ThemeCycle,
    now: datetime,
) -> None:
    """Announce a paid cycle once, then start the wait for the next card."""
    await present_results(client, guild, channel, cycle)
    cycle.announced = True
    cycle.ready_at = now + NEXT_THEME_DELAY
    await service.save_cycle(cycle)
    logger.info(
        "Rewarded %d theme submission(s); winner %s",
        len(cycle.participants),
        cycle.winner,
    )


async def preview_cycle(
    client: discord.Client,
    guild: discord.Guild,
    source: discord.abc.Messageable,
    output: discord.abc.Messageable,
    service: BiweeklyThemeService,
) -> int:
    """Show what the next settlement would do, without paying anything.

    Scans the ongoing card's submissions in ``source`` and replays the
    announcement in ``output``. Returns the number of participants.
    """
    previous_id = service.pinned_message_id
    if previous_id is None:
        return 0
    members = await find_participants(
        source, after_message_id=previous_id, emoji=service.submission_emoji
    )
    if not members:
        return 0
    changes: dict[int, level_service.LevelChangeResult] = {}
    async with session_scope() as session:
        for member in members:
            row = await level_service.get_user_xp(
                session, guild_id=service.guild_id, user_id=member.id
            )
            changes[member.id] = level_service.preview_level_change(
                row.level if row else 0,
                row.total_xp if row else 0,
                SUBMISSION_XP,
            )
    winner = random.choice(members).id
    after = changes[winner]
    cycle = ThemeCycle(
        started_at=level_service.utcnow(),
        message_id=previous_id,
        participants=[member.id for member in members],
        winner=winner,
        changes=changes,
        winner_change=level_service.preview_level_change(
            after.new_level, after.new_total_xp, WINNER_XP
        ),
    )
    await present_results(client, guild, output, cycle, dry_run=True)
    return len(members)
