"""Booster reactions: react to messages that mention a registered booster.

Port of the old Ruby "BoosterReact" feature:
- Identifiers (user id, username, display name, nicknames) are stored raw and
  cleaned/normalised when the matchers are built, then matched
  case-insensitively on word boundaries against each message.
- Reactions are skipped on bot messages and on the booster's own messages.
"""

from __future__ import annotations

import re

import discord
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import session_scope
from ..models import BoosterReact

CUSTOM_EMOJI_RE = re.compile(r"^<(?P<animated>a?):(?P<name>[a-zA-Z0-9_]+):(?P<id>[0-9]+)>$")
_NON_IDENTIFIER_CHARS = re.compile(r"[^a-zA-Z0-9:_.\s]+")
_WS_RUN = re.compile(r"\s+")


def clean_identifier(value: str) -> str:
    """Normalize an identifier the way the old bot did.

    Strips anything that isn't a letter, digit or one of ``:_.``, collapses
    runs of whitespace, trims and lowercases.
    """
    value = _NON_IDENTIFIER_CHARS.sub("", value)
    value = _WS_RUN.sub(" ", value).strip().lower()
    return value


def parse_emoji(raw: str) -> discord.PartialEmoji | str:
    """Turn a config/DB emoji string into something ``Message.add_reaction`` accepts.

    Handles unicode emojis and custom emojis in ``<:name:id>`` /
    ``<a:name:id>`` form (including animated ones).
    """
    if not raw:
        raise ValueError("Empty emoji")
    match = CUSTOM_EMOJI_RE.match(raw)
    if match:
        return discord.PartialEmoji(
            name=match.group("name"),
            id=int(match.group("id")),
            animated=bool(match.group("animated")),
        )
    return raw


class BoosterReactService:
    """CRUD + the in-memory trigger registry used by the message listener."""

    def __init__(self) -> None:
        # booster_id -> {record fields, compiled regexes}
        self._records: dict[int, dict] = {}

    # ------------------------------------------------------------------ cache

    @property
    def records(self) -> list[dict]:
        return list(self._records.values())

    async def all(self) -> list[BoosterReact]:
        """Return every registration (raw rows, for admin listings)."""
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(BoosterReact).order_by(BoosterReact.id)
                    )
                )
                .scalars()
                .all()
            )

    async def reload(self) -> None:
        """Re-read every booster from the database and rebuild the matchers."""
        async with session_scope() as session:
            rows = (
                (await session.execute(select(BoosterReact))).scalars().all()
            )
        self._records = {
            row.discord_user_id: self._build_entry(row)
            for row in rows
        }

    @staticmethod
    def _build_entry(row: BoosterReact) -> dict:
        regexes = []
        for identifier in row.identifiers or []:
            cleaned = clean_identifier(identifier)
            if cleaned:
                regexes.append(
                    re.compile(
                        rf"(?<!\w){re.escape(cleaned)}(?!\w)", re.IGNORECASE
                    )
                )
        return {
            "discord_user_id": row.discord_user_id,
            "emoji": row.emoji,
            "regexes": regexes,
        }

    def find_matching(self, content: str) -> dict | None:
        """Return the first booster whose identifier matches the message content."""
        for entry in self._records.values():
            for regex in entry["regexes"]:
                if regex.search(content):
                    return entry
        return None

    # ------------------------------------------------------------------- CRUD

    async def upsert(
        self,
        discord_user_id: int,
        identifiers: list[str],
        emoji: str,
    ) -> BoosterReact:
        async with session_scope() as session:
            row = await self._find_row(session, discord_user_id)
            if row is None:
                row = BoosterReact(discord_user_id=discord_user_id)
                session.add(row)
            row.identifiers = identifiers
            row.emoji = emoji
            await session.commit()
            await session.refresh(row)
        await self.reload()
        return row

    async def delete(self, discord_user_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(BoosterReact).where(
                    BoosterReact.discord_user_id == discord_user_id
                )
            )
            await session.commit()
        await self.reload()
        return result.rowcount > 0

    async def find(self, discord_user_id: int) -> BoosterReact | None:
        async with session_scope() as session:
            return await self._find_row(session, discord_user_id)

    @staticmethod
    async def _find_row(
        session: AsyncSession, discord_user_id: int
    ) -> BoosterReact | None:
        return (
            await session.execute(
                select(BoosterReact).where(
                    BoosterReact.discord_user_id == discord_user_id
                )
            )
        ).scalar_one_or_none()


def build_identifiers(
    discord_user_id: int,
    username: str | None,
    display_name: str | None,
    nicknames: str | None,
) -> list[str]:
    """Assemble the ordered, de-duplicated list of trigger identifiers.

    Identifiers are stored as entered (raw, case preserved) so they can be
    shown back to admins, but are de-duplicated on their cleaned form. The user
    id is always first; it is used to match mentions. Order is: user id,
    server display name (if present), username (if different), then nicknames
    (with duplicates removed).
    """
    raw: list[str] = [str(discord_user_id)]
    if display_name:
        raw.append(display_name)
    if username and username != display_name:
        raw.append(username)
    if nicknames:
        for part in nicknames.split(","):
            part = part.strip()
            if not part:
                continue
            # avoid re-adding if same as display_name or username
            if part == display_name or part == username:
                continue
            raw.append(part)

    identifiers: list[str] = []
    seen: set[str] = set()
    for value in raw:
        cleaned = clean_identifier(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            identifiers.append(value.strip())
    return identifiers