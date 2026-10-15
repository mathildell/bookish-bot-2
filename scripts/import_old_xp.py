"""Import XP from the old levelling system into the new ``user_xp`` table.

Reads the markdown table produced by the old bot (User ID / Username / Level /
Progress) and reconstructs each member's total XP:

    total_xp = total_xp_for_level(level) + progress_earned_in_level

The old system used the same level thresholds as the new one, so the level and
progress carry over unchanged.

Usage (from the repository root, with the production config.yaml present):

    .venv/bin/python scripts/import_old_xp.py bkp/backup_old_xp.md --dry-run
    .venv/bin/python scripts/import_old_xp.py bkp/backup_old_xp.md

Run ``/xp rewards resync`` afterwards so reward roles are applied.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db, session_scope  # noqa: E402
from bot.models import UserXP  # noqa: E402
from bot.services import level as level_service  # noqa: E402


@dataclass
class OldRow:
    user_id: int
    username: str
    level: int
    progress: int


def parse_amount(text: str) -> int:
    """Parse ``20``, ``481`` or a shorthand like ``3.6k`` into an integer."""
    text = text.strip().lower().replace(",", "").replace(" ", "")
    if text.endswith("k"):
        return int(round(float(text[:-1]) * 1000))
    return int(text)


def _is_separator(cells: list[str]) -> bool:
    return all(c and set(c) <= set("-: ") for c in cells)


def parse_table(path: Path) -> list[OldRow]:
    rows: list[OldRow] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0].lower() == "user id" or _is_separator(cells):
            continue
        rows.append(
            OldRow(
                user_id=int(cells[0]),
                username=cells[1],
                level=int(cells[2]),
                progress=parse_amount(cells[3].split("/")[0]),
            )
        )
    return rows


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    guild_id = int(args.guild_id if args.guild_id else config.guild_id)

    init_db(config.mysql_url)
    await create_tables()

    rows = parse_table(Path(args.path))
    if not rows:
        print(f"No rows found in {args.path}")
        return

    print(f"Guild: {guild_id}")
    print(f"Source: {args.path} ({len(rows)} member(s))")
    print(f"Mode: {'dry-run' if args.dry_run else 'write'}"
          f"{', overwrite' if args.overwrite else ''}\n")
    print(f"{'User ID':<20}{'Username':<34}{'Old':>4}{'Progress':>10}"
          f"{'Total XP':>10}{'New':>4}  Action")
    print("-" * 95)

    created = updated = skipped = 0
    async with session_scope() as session:
        for row in rows:
            total_xp = (
                level_service.total_xp_for_level(row.level) + row.progress
            )
            new_level = level_service.level_from_total_xp(total_xp)
            existing = await level_service.get_user_xp(
                session, guild_id=guild_id, user_id=row.user_id
            )

            flag = "" if new_level == row.level else " (!)"
            if existing is None:
                action = "create"
                created += 1
                if not args.dry_run:
                    session.add(
                        UserXP(
                            guild_id=guild_id,
                            user_id=row.user_id,
                            total_xp=total_xp,
                            level=new_level,
                        )
                    )
            elif args.overwrite or total_xp > existing.total_xp:
                action = f"update ({existing.total_xp} -> {total_xp})"
                updated += 1
                if not args.dry_run:
                    existing.total_xp = total_xp
                    existing.level = new_level
            else:
                action = f"skip (existing {existing.total_xp} >= {total_xp})"
                skipped += 1

            print(
                f"{row.user_id:<20}{row.username:<34}{row.level:>4}"
                f"{row.progress:>10}{total_xp:>10}{new_level:>4}{flag}  {action}"
            )

        if not args.dry_run:
            await session.commit()

    await close_db()

    verb = "Would import" if args.dry_run else "Imported"
    print(
        f"\n{verb}: {created} created, {updated} updated, {skipped} skipped."
    )
    if args.dry_run:
        print("Dry run: no changes written.")
    else:
        print("Next: run /xp rewards resync in Discord to apply reward roles.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", default="bkp/backup_old_xp.md",
        help="Markdown table to import (default: bkp/backup_old_xp.md)",
    )
    parser.add_argument("--config", default=str(ROOT / "config.yaml"),
                        help="Path to config.yaml")
    parser.add_argument("--guild-id", type=int, default=None,
                        help="Override the guild id from config.yaml")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite even if the stored XP is higher")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without writing")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
