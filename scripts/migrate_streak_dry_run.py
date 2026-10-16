"""Add the ``streak_settings.dry_run`` column.

The streaks feature gained an admin dry-run switch. ``create_all`` does not
alter existing tables, so run this once against any database that already has a
``streak_settings`` table:

    .venv/bin/python scripts/migrate_streak_dry_run.py

It is idempotent: the column is only added when missing.
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

TABLE = "streak_settings"
COLUMN = "dry_run"


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
            text(
                f"ALTER TABLE {TABLE} "
                f"ADD COLUMN {COLUMN} BOOL NOT NULL DEFAULT 0"
            )
        )
        await session.commit()
    return True


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    init_db(config.mysql_url)
    await create_tables()

    added = await _ensure_column()
    print(f"Column '{COLUMN}': {'added' if added else 'already present'}")

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
