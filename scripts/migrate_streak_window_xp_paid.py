"""Add the ``streaks.window_xp_paid`` column.

The streak feature now tracks how much XP has already been paid for the current
completed level window, so a fifth day topped up to 100% pays only the
difference and a later lower re-log cannot claw XP back. ``create_all`` does not
alter existing tables, so run this once against any database that already has a
``streaks`` table:

    .venv/bin/python scripts/migrate_streak_window_xp_paid.py

It is idempotent: the column is only added when missing, and streaks whose
current window is already banked are backfilled with the XP they have already
been paid so no retroactive top-up is granted.
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
from bot.models import Streak, StreakLog  # noqa: E402
from bot.services.streaks import (  # noqa: E402
    LEVEL_THRESHOLD,
    OUTCOME_SUCCESS,
    window_xp,
)

TABLE = "streaks"
COLUMN = "window_xp_paid"


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
                f"ADD COLUMN {COLUMN} INT NOT NULL DEFAULT 0"
            )
        )
        await session.commit()
    return True


async def _backfill() -> int:
    """Record XP already paid for banked current windows; return rows changed."""
    changed = 0
    async with session_scope() as session:
        streaks = (
            await session.execute(
                select(Streak).where(Streak.days_logged > 0, Streak.level > 0)
            )
        ).scalars()
        for streak in streaks:
            if streak.days_logged % LEVEL_THRESHOLD != 0:
                continue
            if streak.level != streak.days_logged // LEVEL_THRESHOLD:
                continue
            logs = (
                await session.execute(
                    select(StreakLog)
                    .where(StreakLog.streak_id == streak.id)
                    .order_by(StreakLog.log_date.desc())
                    .limit(LEVEL_THRESHOLD)
                )
            ).scalars()
            successes = sum(1 for entry in logs if entry.outcome == OUTCOME_SUCCESS)
            award = window_xp(streak.level, successes)
            if (streak.level * LEVEL_THRESHOLD) % 365 == 0:
                award *= 2
            if streak.window_xp_paid != award:
                streak.window_xp_paid = award
                changed += 1
        await session.commit()
    return changed


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    init_db(config.mysql_url)
    await create_tables()

    added = await _ensure_column()
    print(f"Column '{COLUMN}': {'added' if added else 'already present'}")
    if added:
        changed = await _backfill()
        print(f"Backfilled {changed} already-paid window(s)")

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
