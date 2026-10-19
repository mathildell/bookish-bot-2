"""SQLAlchemy models (MySQL)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BoosterReact(Base):
    """A booster whose custom emoji is auto-reacted to their name/identifiers."""

    __tablename__ = "booster_reacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The booster's Discord user id (snowflake)
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )

    # Aliases that trigger the reaction, stored raw (case preserved, trimmed):
    # [user_id, display_name, username, *nicknames]. They are cleaned when the
    # matchers are built, not at storage time.
    identifiers: Mapped[list] = mapped_column(JSON, default=list)

    # Emoji to react with; either a unicode emoji or "<:name:id>/<a:name:id>"
    emoji: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class BoosterRole(Base):
    """Links a booster to a custom Discord role they may rename/recolour."""

    __tablename__ = "booster_roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The booster's Discord user id (snowflake)
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )

    # The custom role they own (created by an admin, linked by the bot)
    role_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class UserXP(Base):
    """User XP/level per guild."""

    __tablename__ = "user_xp"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    total_xp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_xp_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    xp_frozen: Mapped[bool] = mapped_column(default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_user_xp_guild_total_xp", "guild_id", "total_xp"),
        Index("ix_user_xp_guild_user_unique", "guild_id", "user_id", unique=True),
    )


class WeeklyXP(Base):
    """XP earned by a member in a Monday-based week."""

    __tablename__ = "weekly_xp"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # Monday the row's XP belongs to; stale rows are ignored and rolled over.
    week_start: Mapped[date | None] = mapped_column(Date, nullable=True)

    xp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_weekly_xp_guild_xp", "guild_id", "xp"),
        Index("ix_weekly_xp_guild_user_unique", "guild_id", "user_id", unique=True),
    )


class LevelReward(Base):
    """Reward role assigned at a specific level."""

    __tablename__ = "level_rewards"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_level_rewards_guild_level_unique", "guild_id", "level", unique=True),
    )


class WeeklySetting(Base):
    """Per-guild settings for the weekly XP leaderboard."""

    __tablename__ = "weekly_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # UTC hour (1-23) the weekly leaderboard is posted on Sundays.
    post_hour: Mapped[int] = mapped_column(
        Integer, nullable=False, default=12, server_default="12"
    )
    # Sunday the board was last posted (idempotency marker).
    last_posted_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ReviewFeed(Base):
    """A member's Goodreads reviews RSS feed."""

    __tablename__ = "review_feeds"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The member's Discord user id (snowflake)
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )

    feed_url: Mapped[str] = mapped_column(Text, nullable=False)

    # When tracking started; entries older than this are treated as backlog and
    # are never posted (only reviews added/edited afterwards are).
    feed_since: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ReviewEntry(Base):
    """Tracks a single review entry so it is posted at most once per cooldown."""

    __tablename__ = "review_entries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    discord_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    # Goodreads review id, normalized from the entry's review URL
    entry_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # The entry's last observed pubDate (bumped by Goodreads on edit)
    last_pub_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # When the bot last posted this entry (null until first post)
    last_posted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ix_review_entries_user_entry_unique",
            "discord_user_id",
            "entry_id",
            unique=True,
        ),
    )


class ReviewSetting(Base):
    """Per-guild settings for the review feature (the posting channel)."""

    __tablename__ = "review_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Days an entry must wait before its review can be reposted.
    repost_cooldown_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class IntroQuestion(Base):
    """A question shown in the introduction form (posted by admins)."""

    __tablename__ = "intro_questions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    placeholder: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class IntroSetting(Base):
    """Per-guild settings for the introduction feature (channel + sticky message)."""

    __tablename__ = "intro_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sticky_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class IntroPost(Base):
    """A member's posted introduction and the answers they gave."""

    __tablename__ = "intro_posts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # The member's Discord user id (snowflake)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # The member's answers keyed by question id (as strings): {question_id: answer}
    answers: Mapped[dict] = mapped_column(JSON, default=dict)

    first_posted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_edited_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_intro_posts_guild_user_unique", "guild_id", "user_id", unique=True),
    )


class BookRelease(Base):
    """An upcoming book release tracked from Goodreads or NetGalley."""

    __tablename__ = "book_releases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Short, human-friendly handle shown in place of the numeric id. Unique
    # within a guild; derived from the title (see releases.unique_reference).
    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    authors: Mapped[list] = mapped_column(JSON, default=list)
    release_date: Mapped[date] = mapped_column(Date, nullable=False)
    blurb: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Site the release came from ("goodreads"/"netgalley") and its own id,
    # parsed from the submitted URL. Together they identify the book.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)

    tags: Mapped[list] = mapped_column(JSON, default=list)

    # The member who added (or last refreshed) the release
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )

    # Set once the release-day announcement has been posted
    announcement_sent: Mapped[bool] = mapped_column(default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ix_book_releases_guild_source_unique",
            "guild_id",
            "source",
            "source_id",
            unique=True,
        ),
        Index(
            "ix_book_releases_guild_reference_unique",
            "guild_id",
            "reference",
            unique=True,
        ),
        Index("ix_book_releases_guild_release_date", "guild_id", "release_date"),
    )


class ReleaseSetting(Base):
    """Per-guild settings for the book releases feature."""

    __tablename__ = "release_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ping_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # "YYYY-MM" of the last posted monthly overview (idempotency marker)
    last_monthly_post: Mapped[str | None] = mapped_column(
        String(7), nullable=True
    )
    # Days a released book is kept before the cleanup job purges it.
    purge_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    # UTC hour (1-23) the daily release announcements are posted.
    post_hour: Mapped[int] = mapped_column(
        Integer, nullable=False, default=13, server_default="13"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ReleaseEditDraft(Base):
    """A proposed book edit awaiting confirmation, from ``/releases update``."""

    __tablename__ = "release_edit_drafts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    book_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The member who triggered the edit (only they may confirm it)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # The proposed book attributes, serialised from BookData
    data: Mapped[dict] = mapped_column(JSON, default=dict)

    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class StreakGoal(Base):
    """A member's registered reading streak goal (amount, unit, timezone)."""

    __tablename__ = "streak_goals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    # "none", "custom", "last_chance" or "both"
    reminder: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # Local hour (1-22) for the custom reminder; null unless custom/both
    custom_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_streak_goals_guild_user_unique", "guild_id", "user_id", unique=True),
    )


class Streak(Base):
    """One run of consecutive logged days. Aggregates survive log purging.

    ``days_logged``, ``level`` and the rescue/XP counters are the persistent
    macros the leaderboards and views read; ``streak_daily_logs`` rows are only
    a temporary record of individual days.
    """

    __tablename__ = "streaks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    goal_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # "ongoing" or "closed"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ongoing")

    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_logged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    rescues_available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rescues_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    golden_available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    golden_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_xp_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_xp_awarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_xp_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # XP already paid for the streak's current completed level window. Kept so
    # that topping a just-completed 5th day up to 100% pays only the difference,
    # and so a later lower re-log cannot claw XP back.
    window_xp_paid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_log_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Reminder idempotency across restarts (local date last pinged on)
    last_custom_reminder_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_lastchance_reminder_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_streaks_guild_days", "guild_id", "days_logged"),
        Index("ix_streaks_guild_status_days", "guild_id", "status", "days_logged"),
        Index("ix_streaks_guild_user", "guild_id", "user_id"),
    )


class StreakLog(Base):
    """A single logged day. Temporary storage, purged after a few months."""

    __tablename__ = "streak_daily_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    streak_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    log_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)

    percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # "success" (>= goal), "failed" (0 < amount < goal) or "zero" (amount == 0)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    # A rescued day: patched with 0 and flagged; rescue_type is "normal"/"golden"
    repair: Mapped[bool] = mapped_column(default=False, nullable=False)
    rescue_type: Mapped[str | None] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ix_streak_daily_logs_streak_date_unique",
            "streak_id",
            "log_date",
            unique=True,
        ),
    )


class StreakSetting(Base):
    """Per-guild settings for the streaks feature (the streaks channel)."""

    __tablename__ = "streak_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # When set, streak levels still progress but no XP reaches the members.
    dry_run: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Days a daily log is kept before the cleanup job purges it.
    log_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class BotSetting(Base):
    """Bot-wide, per-guild settings editable from ``/settings``."""

    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    # Channel notified about upkeep events (streak closures, unlinked boosters).
    updates_channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    # Publicly reachable image used as a transparent embed background.
    transparent_embed_image: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    # Default embed colour as "#rrggbb".
    default_embed_color: Mapped[str | None] = mapped_column(
        String(7), nullable=True
    )
    # Forum channel where new threads receive a pinned reference message.
    reference_forum_channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    # Jump link to the message reposted into new forum threads.
    reference_message_link: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    # Channel where channel-locked commands are allowed regardless of their lock.
    admin_test_channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    # Image shown on a streak coupon when rendering/uploading fails.
    coupon_fallback_image: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
