"""Add the cleanup retention columns.

The purge jobs gained configurable retention windows:

- ``release_settings.purge_days`` (default 30)
- ``streak_settings.log_retention_days`` (default 90)

``create_all`` does not alter existing tables, so run this once against any
database that already has these tables:

    .venv/bin/python scripts/migrate_purge_settings.py

It is idempotent: each column is only added when missing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db, session_scope  # noqa: E402

COLUMNS = (
    ("release_settings", "purge_days", "INT NOT NULL DEFAULT 30"),
    ("streak_settings", "log_retention_days", "INT NOT NULL DEFAULT 90"),
)


async def _ensure_column(table: str, column: str, definition: str) -> bool:
    """Add the column when missing; return whether it was added."""
    async with session_scope() as session:
        exists = (
            await session.execute(
                text(f"SHOW COLUMNS FROM {table} LIKE '{column}'")
            )
        ).first()
        if exists:
            return False
        await session.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        )
        await session.commit()
    return True


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    init_db(config.mysql_url)
    await create_tables()

    for table, column, definition in COLUMNS:
        added = await _ensure_column(table, column, definition)
        state = "added" if added else "already present"
        print(f"{table}.{column}: {state}")

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
