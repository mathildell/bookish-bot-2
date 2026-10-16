"""Import the old book releases into the new ``book_releases`` table.

Reads the CSV exported from the old bot (one row per tracked release) and
inserts each book, deriving the source and source id from its URL. Google Books
rows (present only in the old data, and unsupported by the bot) are ignored and
reported; import those separately.

Existing rows (same source and source id) are left untouched, so the script can
be re-run safely.

Usage (from the repository root, with the production config.yaml present):

    .venv/bin/python scripts/import_old_releases.py bkp/backup_old_releases.csv --dry-run
    .venv/bin/python scripts/import_old_releases.py bkp/backup_old_releases.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db, session_scope  # noqa: E402
from bot.models import BookRelease  # noqa: E402
from bot.services.html import html_to_markdown  # noqa: E402
from bot.services.release_sources import (  # noqa: E402
    BookScrapeError,
    parse_source_url,
)


SKIPPED_SOURCES = frozenset({"google"})


@dataclass
class OldRelease:
    title: str
    authors: list[str]
    release_date: date
    blurb: str
    image_url: str | None
    source: str
    source_id: str
    url: str
    tags: list[str]
    discord_user_id: int
    announcement_sent: bool
    created_at: datetime | None
    updated_at: datetime | None


def _first(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _collect(row: dict, prefix: str) -> list[str]:
    """Gather the non-empty ``prefix[0]``, ``prefix[1]`` ... values."""
    return [
        value.strip()
        for key, value in row.items()
        if key.startswith(prefix + "[") and (value or "").strip()
    ]


def _unescape_newlines(value: str) -> str:
    """The old export stored the blurbs' line breaks as literal ``\\n``."""
    return value.replace("\\n", "\n")


def _parse_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_timestamp(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _resolve_source(row: dict) -> tuple[str, str]:
    """Return ``(source, source_id)`` for a row, preferring its URL."""
    url = (row.get("url") or "").strip()
    try:
        return parse_source_url(url)
    except BookScrapeError:
        pass
    source = (row.get("_source") or "").strip() or "unknown"
    source_id = (row.get("google_book_id") or "").strip()
    if not source_id:
        source_id = (parse_qs(urlparse(url).query).get("id") or [""])[0].strip()
    if not source_id:
        source_id = (row.get("book_info.id") or "").strip()
    return source, source_id


def parse_release(row: dict) -> OldRelease | None:
    """Turn a CSV row into an ``OldRelease``, or ``None`` if unusable."""
    title = _first(row.get("title"), row.get("book_info.title"))
    release_date = _parse_date(row.get("release_date", ""))
    user_text = _first(row.get("discord_user_id"), row.get("user_id"))
    if not title or release_date is None or not user_text.isdigit():
        return None

    source, source_id = _resolve_source(row)
    raw_blurb = (row.get("blurb") or "").strip()
    blurb = (
        _unescape_newlines(raw_blurb)
        if raw_blurb
        else html_to_markdown(
            _unescape_newlines(row.get("book_info.description") or "")
        )
    )
    return OldRelease(
        title=title,
        authors=_collect(row, "authors") or _collect(row, "book_info.authors"),
        release_date=release_date,
        blurb=blurb,
        image_url=_first(row.get("image_url"), row.get("book_info.image")) or None,
        source=source,
        source_id=source_id,
        url=(row.get("url") or "").strip(),
        tags=_collect(row, "tags") or _collect(row, "book_info.categories"),
        discord_user_id=int(user_text),
        announcement_sent=(row.get("announcement_sent") or "").strip().lower()
        in {"true", "1", "yes"},
        created_at=_parse_timestamp(row.get("created_at", "")),
        updated_at=_parse_timestamp(row.get("updated_at", "")),
    )


def parse_csv(path: Path) -> tuple[list[OldRelease], int, int]:
    """Return the releases, the bad row count and the ignored row count."""
    releases: list[OldRelease] = []
    bad = 0
    ignored = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            release = parse_release(row)
            if release is None:
                bad += 1
            elif release.source in SKIPPED_SOURCES:
                ignored += 1
            else:
                releases.append(release)
    return releases, bad, ignored


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    guild_id = int(args.guild_id if args.guild_id else config.guild_id)

    init_db(config.mysql_url)
    await create_tables()

    releases, bad, ignored = parse_csv(Path(args.path))
    if not releases:
        print(f"No releases found in {args.path}")
        await close_db()
        return

    print(f"Guild: {guild_id}")
    print(f"Source: {args.path} ({len(releases)} release(s))")
    if ignored:
        sources = ", ".join(sorted(SKIPPED_SOURCES))
        print(f"Ignored {ignored} row(s) from unsupported source(s): {sources}")
    print(f"Mode: {'dry-run' if args.dry_run else 'write'}\n")
    print(f"{'Source':<11}{'Date':<12}{'User':<20}Title  Action")
    print("-" * 90)

    created = skipped = 0
    async with session_scope() as session:
        for old in releases:
            existing = (
                await session.execute(
                    select(BookRelease).where(
                        BookRelease.guild_id == guild_id,
                        BookRelease.source == old.source,
                        BookRelease.source_id == old.source_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                action = "skip (already imported)"
            else:
                created += 1
                action = "create"
                if not args.dry_run:
                    book = BookRelease(
                        guild_id=guild_id,
                        title=old.title,
                        authors=old.authors,
                        release_date=old.release_date,
                        blurb=old.blurb or None,
                        image_url=old.image_url,
                        source=old.source,
                        source_id=old.source_id,
                        url=old.url,
                        tags=old.tags,
                        discord_user_id=old.discord_user_id,
                        announcement_sent=old.announcement_sent,
                    )
                    if old.created_at is not None:
                        book.created_at = old.created_at
                    if old.updated_at is not None:
                        book.updated_at = old.updated_at
                    session.add(book)

            title = old.title if len(old.title) <= 40 else old.title[:39] + "…"
            print(
                f"{old.source:<11}{old.release_date.isoformat():<12}"
                f"{old.discord_user_id:<20}{title}  {action}"
            )

        if not args.dry_run:
            await session.commit()

    await close_db()

    verb = "Would import" if args.dry_run else "Imported"
    print(f"\n{verb}: {created} created, {skipped} skipped.")
    if bad:
        print(f"Skipped {bad} unusable row(s).")
    if args.dry_run:
        print("Dry run: no changes written.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", default="bkp/backup_old_releases.csv",
        help="CSV export to import (default: bkp/backup_old_releases.csv)",
    )
    parser.add_argument("--config", default=str(ROOT / "config.yaml"),
                        help="Path to config.yaml")
    parser.add_argument("--guild-id", type=int, default=None,
                        help="Override the guild id from config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without writing")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
