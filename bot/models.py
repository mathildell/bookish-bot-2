"""SQLAlchemy models (MySQL)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text, func
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

    # Cleaned lowercase aliases that trigger the reaction:
    # [user_id, username, display_name, *nicknames]
    identifiers: Mapped[list] = mapped_column(JSON, default=list)

    # Emoji to react with; either a unicode emoji or "<:name:id>/<a:name:id>"
    emoji: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )