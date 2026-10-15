"""Import the old introduction posts into the new ``intro_posts`` table.

Reads the CSV exported from the old bot (a ``User (ID)`` column followed by one
column per question), creates any missing questions, posts each introduction to
the configured intro channel as the related member, and records it so the member
can edit or delete their own post.

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
import sys
from dataclasses import dataclass, field
from pathlib import Path

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


def resolve_header_map(
    headers: list[str], existing: list[tuple[int, str]]
) -> tuple[dict[str, int], list[str]]:
    """Match CSV headers to question ids; report headers with no question.

    Matching is case-insensitive on the question content, so an admin who
    already configured the same questions doesn't get duplicates.
    """
    by_content: dict[str, int] = {}
    for question_id, content in existing:
        by_content.setdefault(content.strip().lower(), question_id)

    mapping: dict[str, int] = {}
    missing: list[str] = []
    for header in headers:
        question_id = by_content.get(header.strip().lower())
        if question_id is None:
            missing.append(header)
        else:
            mapping[header] = question_id
    return mapping, missing


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


async def _import(
    client: discord.Client,
    service: IntroService,
    guild_id: int,
    headers: list[str],
    rows: list[OldIntro],
    mapping: dict[str, int],
    overwrite: bool,
    repost: bool,
) -> None:
    channel = await client.fetch_channel(service.channel_id)
    guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)

    questions = {question.id: question for question in await service.all_questions()}
    ordered = [questions[mapping[header]] for header in headers if header in mapping]
    now = utcnow()

    posted = skipped = failed = 0
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
        try:
            sent = await channel.send(embed=embed, view=IntroRefreshView())
        except discord.HTTPException as exc:
            print(f"  fail {row.user_id}: {exc}")
            failed += 1
            continue

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
    mapping, missing = resolve_header_map(
        headers, [(question.id, question.content) for question in existing]
    )

    print(f"Guild: {guild_id}")
    print(f"Source: {args.path} ({len(rows)} introduction(s))")
    print(f"Questions: {', '.join(headers)}")
    if missing:
        action = "will be created" if args.create_questions else "will be skipped"
        print(f"Unmatched questions ({action}): {', '.join(missing)}")
    if not args.create_questions:
        for header in missing:
            mapping.pop(header, None)
    print(f"Mode: {'dry-run' if args.dry_run else 'write'}"
          f"{', overwrite' if args.overwrite else ''}\n")

    if args.dry_run:
        for row in rows:
            print(f"  would post {row.user_id}")
        print("\nDry run: no changes written.")
        await close_db()
        return

    if missing and args.create_questions:
        for header in missing:
            question = await service.add_question(header, None, True)
            mapping[header] = question.id
            print(f"  created question #{question.position}: {header}")

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
        "--no-create-questions", dest="create_questions", action="store_false",
        help="Don't create questions that are missing (skip those columns)",
    )
    parser.add_argument(
        "--no-sticky", action="store_true",
        help="Don't repost the sticky message afterwards",
    )
    parser.set_defaults(create_questions=True)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
