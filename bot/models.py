"""SQLAlchemy models (MySQL)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
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
