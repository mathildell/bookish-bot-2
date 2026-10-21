"""Import reading prompts from a CSV file into the ``weekly_reading_cards`` table.

The CSV may carry a header row naming the columns (``title`` and, optionally,
``description``); a file with a single column of prompts works too. Without a
header, the first column is the title and the second the description.

Duplicate titles are skipped, comparing them without case, accents or repeated
whitespace, both against the cards already in the database and against the
earlier rows of the same file.

Usage (from the repository root, with the production config.yaml present):

    .venv/bin/python scripts/import_weekly_reading_cards.py prompts.csv --dry-run
    .venv/bin/python scripts/import_weekly_reading_cards.py prompts.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db  # noqa: E402
from bot.services.weekly_reading import (  # noqa: E402
    WeeklyReadingError,
    WeeklyReadingService,
    normalize_title,
)

TITLE_HEADERS = {"title", "prompt", "card", "text", "question", "name"}
DESCRIPTION_HEADERS = {"description", "details", "body", "notes", "extra"}


@dataclass(frozen=True)
class CardRow:
    title: str
    description: str | None = None


def parse_cards(path: Path) -> list[CardRow]:
    """Read the CSV into card rows, with or without a header row."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    records = [row for row in records if any(cell.strip() for cell in row)]
    if not records:
        return []

    header = [cell.strip().casefold() for cell in records[0]]
    if any(cell in TITLE_HEADERS or cell in DESCRIPTION_HEADERS for cell in header):
        title_index = next(
            (
                index
                for index, cell in enumerate(header)
                if cell in TITLE_HEADERS
            ),
            0,
        )
        description_index = next(
            (
                index
                for index, cell in enumerate(header)
                if cell in DESCRIPTION_HEADERS
            ),
            None,
        )
        rows = records[1:]
    else:
        title_index = 0
        description_index = 1
        rows = records

    cards: list[CardRow] = []
    for row in rows:
        title = row[title_index].strip() if title_index < len(row) else ""
        if not title:
            continue
        description = ""
        if description_index is not None and description_index < len(row):
            description = row[description_index].strip()
        cards.append(CardRow(title=title, description=description or None))
    return cards


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    guild_id = int(args.guild_id or config.guild_id)

    init_db(config.mysql_url)
    await create_tables()

    cards = parse_cards(Path(args.path))
    if not cards:
        print(f"No reading prompts found in {args.path}")
        await close_db()
        return

    service = WeeklyReadingService(guild_id)
    seen = {
        normalize_title(card.title) for card in await service.list_cards()
    }

    print(f"Guild: {guild_id}")
    print(f"Source: {args.path} ({len(cards)} prompt(s))")
    print(f"Mode: {'dry-run' if args.dry_run else 'write'}\n")

    added = skipped = failed = 0
    for card in cards:
        try:
            key = normalize_title(card.title)
            if key in seen:
                print(f"  skip {card.title!r}: already in the deck")
                skipped += 1
                continue
            if args.dry_run:
                print(f"  would add {card.title!r}")
            else:
                await service.add_card(card.title, card.description)
                print(f"  added {card.title!r}")
            seen.add(key)
            added += 1
        except WeeklyReadingError as exc:
            print(f"  fail {card.title!r}: {exc.key}")
            failed += 1

    print(f"\nDone: {added} added, {skipped} skipped, {failed} failed.")
    await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default="prompts.csv",
        help="CSV file with the reading prompts (default: prompts.csv)",
    )
    parser.add_argument(
        "--config", default=str(ROOT / "config.yaml"), help="Path to config.yaml"
    )
    parser.add_argument(
        "--guild-id", type=int, default=None,
        help="Override the guild id from config.yaml",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without writing",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
