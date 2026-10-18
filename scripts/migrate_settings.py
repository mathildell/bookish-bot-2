"""Add the configurable bot-setting columns and table.

Settings that used to live in ``config.yaml`` or be fixed constants became
editable from ``/settings``:

- ``release_settings.purge_days`` (default 30)
- ``release_settings.post_hour`` (default 13, UTC)
- ``streak_settings.log_retention_days`` (default 90)
- ``review_settings.repost_cooldown_days`` (default 1)
- ``bot_settings`` table (updates channel, transparent embed image, default
  embed colour, buddy read forum channel and reference message link)

``create_all`` does not alter existing tables. ``create_tables`` now additively
adds missing columns on startup, so this script is no longer required for a
normal deploy; it remains for manual/older setups:

    .venv/bin/python scripts/migrate_settings.py

It is idempotent: each column is only added when missing, and ``create_tables``
creates the new ``bot_settings`` table if absent.
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
    ("release_settings", "post_hour", "INT NOT NULL DEFAULT 13"),
    ("streak_settings", "log_retention_days", "INT NOT NULL DEFAULT 90"),
    ("review_settings", "repost_cooldown_days", "INT NOT NULL DEFAULT 1"),
    ("bot_settings", "reference_forum_channel_id", "BIGINT NULL"),
    ("bot_settings", "reference_message_link", "VARCHAR(512) NULL"),
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

    print("bot_settings: ready")

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
