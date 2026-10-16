"""Add the ``book_releases.reference`` column and backfill unique handles.

The releases feature gained a stored, guild-unique ``reference`` (handle).
``create_all`` does not alter existing tables, so run this once against any
database that already has a ``book_releases`` table:

    .venv/bin/python scripts/migrate_release_references.py

It is idempotent: the column and unique index are only created when missing,
and only rows without a handle are backfilled.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select, text

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db, session_scope  # noqa: E402
from bot.models import BookRelease  # noqa: E402
from bot.services.releases import unique_reference  # noqa: E402

TABLE = "book_releases"
COLUMN = "reference"
INDEX = "ix_book_releases_guild_reference_unique"


async def _ensure_column() -> bool:
    """Add the column when missing; return whether it was added."""
    async with session_scope() as session:
        exists = (
            await session.execute(
                text(f"SHOW COLUMNS FROM {TABLE} LIKE '{COLUMN}'")
            )
        ).first()
        if exists:
            return False
        await session.execute(
            text(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(64) NULL")
        )
        await session.commit()
    return True


async def _backfill() -> int:
    """Assign a unique handle to every row that lacks one."""
    updated = 0
    async with session_scope() as session:
        guild_ids = (
            await session.scalars(select(BookRelease.guild_id).distinct())
        ).all()
        for guild_id in guild_ids:
            books = (
                await session.scalars(
                    select(BookRelease)
                    .where(BookRelease.guild_id == guild_id)
                    .order_by(BookRelease.id)
                )
            ).all()
            taken = {book.reference for book in books if book.reference}
            for book in books:
                if book.reference:
                    continue
                book.reference = unique_reference(book.title, taken)
                taken.add(book.reference)
                updated += 1
        await session.commit()
    return updated


async def _enforce_unique() -> None:
    """Make the column NOT NULL and add its unique index when missing."""
    async with session_scope() as session:
        await session.execute(
            text(f"ALTER TABLE {TABLE} MODIFY {COLUMN} VARCHAR(64) NOT NULL")
        )
        index = (
            await session.execute(
                text(
                    f"SHOW INDEX FROM {TABLE} WHERE Key_name = '{INDEX}'"
                )
            )
        ).first()
        if index is None:
            await session.execute(
                text(
                    f"CREATE UNIQUE INDEX {INDEX} "
                    f"ON {TABLE} (guild_id, {COLUMN})"
                )
            )
        await session.commit()


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    init_db(config.mysql_url)
    await create_tables()

    added = await _ensure_column()
    print(f"Column '{COLUMN}': {'added' if added else 'already present'}")
    backfilled = await _backfill()
    print(f"Backfilled {backfilled} row(s).")
    await _enforce_unique()
    print(f"Ensured unique index '{INDEX}'.")

    await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.yaml"),
        help="Path to config.yaml",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
