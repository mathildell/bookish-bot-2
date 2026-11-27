"""Back up (or restore) the levelling tables for one guild as JSON.

Covers ``user_xp``, ``weekly_xp``, ``level_rewards`` and ``xp_boosts``: every
row of the guild, every column, ids and timestamps included, so a restore puts
the tables back exactly as they were.

Usage (from the repository root, with the production config.yaml present):

    .venv/bin/python scripts/backup_xp.py backup
    .venv/bin/python scripts/backup_xp.py backup --out bkp/before_rewards.json
    .venv/bin/python scripts/backup_xp.py restore bkp/xp_backup_<stamp>.json --dry-run
    .venv/bin/python scripts/backup_xp.py restore bkp/xp_backup_<stamp>.json

A restore replaces the guild's rows in those tables with the backup's. Stop the
bot first (it caches level rewards in memory and keeps writing XP), and run
``/xp rewards resync`` after starting it again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import Date, DateTime, delete, func, select  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db, session_scope  # noqa: E402
from bot.models import LevelReward, UserXP, WeeklyXP, XpBoost  # noqa: E402

MODELS = (UserXP, WeeklyXP, LevelReward, XpBoost)


def _dump_value(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _load_value(column, value: object) -> object:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return datetime.fromisoformat(value)
    if isinstance(column.type, Date):
        return date.fromisoformat(value)
    return value


async def backup(guild_id: int, out: Path) -> None:
    data: dict[str, object] = {
        "guild_id": guild_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "tables": {},
    }
    async with session_scope() as session:
        for model in MODELS:
            rows = (
                await session.execute(
                    select(model)
                    .where(model.guild_id == guild_id)
                    .order_by(model.id)
                )
            ).scalars().all()
            columns = model.__table__.columns
            data["tables"][model.__tablename__] = [
                {c.key: _dump_value(getattr(row, c.key)) for c in columns}
                for row in rows
            ]
            print(f"{model.__tablename__:<16}{len(rows):>6} row(s)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"\nSaved to {out}")


async def restore(guild_id: int, path: Path, dry_run: bool) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data["guild_id"]) != guild_id:
        sys.exit(
            f"Backup is for guild {data['guild_id']}, not {guild_id}; "
            "pass --guild-id to override."
        )
    print(f"Backup taken {data['created_at']}")
    print(f"Mode: {'dry-run' if dry_run else 'write'}\n")
    async with session_scope() as session:
        for model in MODELS:
            rows = data["tables"].get(model.__tablename__, [])
            current = (
                await session.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.guild_id == guild_id)
                )
            ).scalar_one()
            print(
                f"{model.__tablename__:<16}{current:>6} current -> "
                f"{len(rows):>6} from backup"
            )
            if dry_run:
                continue
            await session.execute(delete(model).where(model.guild_id == guild_id))
            columns = model.__table__.columns
            for row in rows:
                session.add(
                    model(
                        **{
                            c.key: _load_value(c, row[c.key])
                            for c in columns
                            if c.key in row
                        }
                    )
                )
        if not dry_run:
            await session.commit()
            print("\nRestored. Start the bot and run /xp rewards resync.")


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    guild_id = int(args.guild_id if args.guild_id else config.guild_id)
    init_db(config.mysql_url)
    await create_tables()
    try:
        if args.command == "backup":
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = Path(args.out or f"bkp/xp_backup_{stamp}.json")
            await backup(guild_id, out)
        else:
            await restore(guild_id, Path(args.path), args.dry_run)
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--guild-id", help="defaults to config.yaml's guild_id")
    sub = parser.add_subparsers(dest="command", required=True)
    backup_parser = sub.add_parser("backup", help="write the tables to JSON")
    backup_parser.add_argument("--out", help="default: bkp/xp_backup_<stamp>.json")
    restore_parser = sub.add_parser("restore", help="replace tables from JSON")
    restore_parser.add_argument("path")
    restore_parser.add_argument("--dry-run", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
