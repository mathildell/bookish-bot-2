"""Import the old introduction posts into the new ``intro_posts`` table.

Reads the CSV exported from the old bot (a ``User (ID)`` column followed by one
column per question), matches each column to an existing intro question in the
database, posts each introduction to the configured intro channel as the related
member, and records it so the member can edit or delete their own post.

Questions are never created: a CSV column is matched to the database question
whose text contains it (ignoring case, punctuation and emojis). Every column
must resolve to exactly one question, otherwise the script aborts.

Discord groups consecutive messages from the same author, so posts are spaced
out by 8m5s by default (override with ``--delay``) to keep each introduction
standalone.

Run it with the main bot stopped: a second gateway connection with the same
token would briefly disconnect it, and the bot caches the sticky message id.

Usage (from the repository root, with the production config.yaml present):

    .venv/bin/python scripts/import_old_intros.py bkp/backup_old_intros.csv --dry-run
    .venv/bin/python scripts/import_old_intros.py bkp/backup_old_intros.csv

Then start the bot again.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.commands.intro import IntroRefreshView, repost_sticky  # noqa: E402
from bot.commands.listing import parse_hex_colour  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db  # noqa: E402
from bot.services.intro import IntroService, build_intro_embed  # noqa: E402
from bot.services.level import utcnow  # noqa: E402

# Discord groups consecutive messages from the same author; a group ends 8
# minutes after it starts, so spacing posts out by 8m5s keeps each one separate.
DEFAULT_DELAY = 485.0


@dataclass
class OldIntro:
    user_id: int
    answers: dict[str, str] = field(default_factory=dict)


def parse_csv(path: Path) -> tuple[list[str], list[OldIntro]]:
    """Parse the old export into its question headers and one row per member."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    if not records:
        return [], []

    headers = [cell.strip() for cell in records[0]]
    questions = headers[1:]

    rows: list[OldIntro] = []
    for raw in records[1:]:
        if not raw or not raw[0].strip():
            continue
        answers = {
            question: (raw[index] if index < len(raw) else "")
            for index, question in enumerate(questions, start=1)
        }
        rows.append(OldIntro(user_id=int(raw[0].strip()), answers=answers))
    return questions, rows


def _normalize(text: str) -> str:
    """Lowercase and keep only word characters, dropping emojis/punctuation."""
    return " ".join(re.findall(r"\w+", text.lower()))


def resolve_header_map(
    headers: list[str], existing: list[tuple[int, str]]
) -> tuple[dict[str, int], list[str], list[tuple[str, list[str]]]]:
    """Match CSV headers to database questions by partial, normalized text.

    A header matches every question whose text contains it once emojis and
    punctuation are stripped, so a database question like "Favorite Reads :books:"
    still matches the CSV column "Favorite Reads". Returns the header -> id map,
    the headers with no match, and the headers matching several questions.
    """
    candidates = [
        (question_id, content, _normalize(content))
        for question_id, content in existing
    ]

    mapping: dict[str, int] = {}
    unmatched: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []
    for header in headers:
        needle = _normalize(header)
        matches = [
            (question_id, content)
            for question_id, content, haystack in candidates
            if needle and needle in haystack
        ]
        exact = [
            (question_id, content)
            for question_id, content in matches
            if _normalize(content) == needle
        ]
        chosen = exact or matches
        if len(chosen) == 1:
            mapping[header] = chosen[0][0]
        elif not chosen:
            unmatched.append(header)
        else:
            ambiguous.append((header, [content for _, content in chosen]))
    return mapping, unmatched, ambiguous


async def _resolve_user(
    client: discord.Client, guild: discord.Guild, user_id: int
) -> discord.abc.User | None:
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        pass
    except discord.HTTPException:
        pass
    try:
        return await client.fetch_user(user_id)
    except discord.HTTPException:
        return None


async def _delete_message(channel, message_id: int) -> None:
    try:
        message = await channel.fetch_message(message_id)
        await message.delete()
    except discord.HTTPException:
        pass


def seconds_until_next(
    last_sent_at: float | None, delay: float, now: float
) -> float:
    """Seconds to wait so the next message starts a fresh Discord group."""
    if last_sent_at is None or delay <= 0:
        return 0.0
    return max(0.0, delay - (now - last_sent_at))


async def _pause(last_sent_at: float | None, delay: float) -> None:
    remaining = seconds_until_next(last_sent_at, delay, monotonic())
    if remaining > 0:
        print(f"  waiting {remaining:.0f}s so the next message isn't grouped...")
        await asyncio.sleep(remaining)


async def _import(
    client: discord.Client,
    service: IntroService,
    guild_id: int,
    headers: list[str],
    rows: list[OldIntro],
    mapping: dict[str, int],
    overwrite: bool,
    repost: bool,
    delay: float,
) -> None:
    channel = await client.fetch_channel(service.channel_id)
    guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)

    questions = {question.id: question for question in await service.all_questions()}
    ordered = [questions[mapping[header]] for header in headers if header in mapping]
    now = utcnow()

    posted = skipped = failed = 0
    last_sent: float | None = None
    for row in rows:
        existing = await service.get_post(row.user_id)
        if existing is not None and not overwrite:
            print(f"  skip {row.user_id}: already has an introduction")
            skipped += 1
            continue

        user = await _resolve_user(client, guild, row.user_id)
        if user is None:
            print(f"  skip {row.user_id}: member not found")
            failed += 1
            continue

        answers = {
            str(mapping[header]): row.answers[header].strip()
            for header in headers
            if header in mapping and row.answers.get(header, "").strip()
        }
        embed = build_intro_embed(user, ordered, answers, now, client)
        await _pause(last_sent, delay)
        try:
            sent = await channel.send(embed=embed, view=IntroRefreshView())
        except discord.HTTPException as exc:
            print(f"  fail {row.user_id}: {exc}")
            failed += 1
            continue
        last_sent = monotonic()

        if existing is not None:
            await _delete_message(channel, existing.message_id)
            await service.update_post(
                existing.id, answers, now, message_id=sent.id
            )
        else:
            await service.create_post(
                row.user_id, sent.id, channel.id, answers, now
            )
        print(f"  posted {row.user_id} ({user.display_name})")
        posted += 1

    if repost:
        await _pause(last_sent, delay)
        await repost_sticky(client, channel)
        print("  reposted the sticky message")

    print(f"\nDone: {posted} posted, {skipped} skipped, {failed} failed.")


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    guild_id = int(args.guild_id or config.guild_id)

    init_db(config.mysql_url)
    await create_tables()

    headers, rows = parse_csv(Path(args.path))
    if not rows:
        print(f"No introductions found in {args.path}")
        await close_db()
        return

    service = IntroService(guild_id)
    await service.reload()

    existing = await service.all_questions()
    mapping, unmatched, ambiguous = resolve_header_map(
        headers, [(question.id, question.content) for question in existing]
    )
    content_by_id = {question.id: question.content for question in existing}

    print(f"Guild: {guild_id}")
    print(f"Source: {args.path} ({len(rows)} introduction(s))")
    for header in headers:
        if header in mapping:
            print(f"  {header}  ->  {content_by_id[mapping[header]]}")
    print(f"Mode: {'dry-run' if args.dry_run else 'write'}"
          f"{', overwrite' if args.overwrite else ''}\n")

    if not args.dry_run and args.delay > 0:
        messages = len(rows) + (0 if args.no_sticky else 1)
        minutes = args.delay * max(0, messages - 1) / 60
        print(f"Delay between posts: {args.delay:.0f}s "
              f"(about {minutes:.0f} minutes total)\n")

    if unmatched:
        print("No database question matches these CSV columns:")
        for header in unmatched:
            print(f"  {header}")
    if ambiguous:
        print("These CSV columns match several database questions:")
        for header, contents in ambiguous:
            print(f"  {header}: {', '.join(contents)}")
    if unmatched or ambiguous:
        print("\nAdd or rename the questions so each column matches exactly one,"
              " then run the script again. No changes were written.")
        await close_db()
        return

    if args.dry_run:
        for row in rows:
            print(f"  would post {row.user_id}")
        print("\nDry run: no changes written.")
        await close_db()
        return

    if not service.channel_id:
        print("No intro channel is set; run /intro channel set first.")
        await close_db()
        return

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    default_color = config.get("default_embed_color")
    client.default_embed_color = (
        parse_hex_colour(default_color) if default_color else None
    )
    client.transparent_embed_image = config.get("transparent_embed_image") or None
    client.intros = service

    @client.event
    async def on_ready() -> None:
        try:
            await _import(
                client,
                service,
                guild_id,
                headers,
                rows,
                mapping,
                overwrite=args.overwrite,
                repost=not args.no_sticky,
                delay=args.delay,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Import failed: {exc}")
        finally:
            await client.close()

    await client.start(config.discord_token)
    await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default="bkp/backup_old_intros.csv",
        help="CSV export to import (default: bkp/backup_old_intros.csv)",
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
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Re-import members who already have an introduction",
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY,
        help=(
            "Seconds to wait between posts so Discord doesn't group them "
            f"(default: {DEFAULT_DELAY:.0f} = 8m5s; use 0 to disable)"
        ),
    )
    parser.add_argument(
        "--no-sticky", action="store_true",
        help="Don't repost the sticky message afterwards",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
