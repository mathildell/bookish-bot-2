"""SQLAlchemy models (MySQL)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, Text, func
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


class LevelSettings(Base):
    """Per-guild leveling settings (e.g. whether defaults were seeded)."""

    __tablename__ = "level_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    guild_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    rewards_seeded: Mapped[bool] = mapped_column(default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
