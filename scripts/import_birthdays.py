"""Import the old birthdays and birthday cards into the new tables.

The script reads the three Mongo JSON exports from ``bkp/birthdays`` (or a
folder passed as the first argument): birth dates, content cards and users. The
users export is the bridge from the old Mongo user ids to Discord user ids and
also carries the timezone each birthday used.

Birthdays are stored yearless, so the old ``year`` is ignored. Cards of kind
``text`` become birthday messages; cards of kind ``image`` become birthday
images. A card linked to a member is imported as personal content, otherwise it
is added to the generic pool. Duplicate birthdays (by member) and duplicate
cards (by linked member and content) are skipped.

Usage (from the repository root, with the production config.yaml present):

    .venv/bin/python scripts/import_birthdays.py --dry-run
    .venv/bin/python scripts/import_birthdays.py
    .venv/bin/python scripts/import_birthdays.py some/export/folder
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db  # noqa: E402
from bot.services.birthdays import (  # noqa: E402
    BirthdayError,
    BirthdayService,
    valid_date,
)
from bot.services.timezones import valid_timezone  # noqa: E402

DEFAULT_FOLDER = ROOT / "bkp" / "birthdays"
CARD_TEXT = "text"
CARD_IMAGE = "image"


@dataclass(frozen=True)
class OldUser:
    old_id: str
    discord_user_id: int
    timezone: str | None


@dataclass(frozen=True)
class BirthdayRecord:
    user_id: int
    month: int
    day: int
    timezone: str


@dataclass(frozen=True)
class CardRecord:
    kind: str
    content: str
    user_id: int | None


def _object_id(value) -> str | None:
    """Read a Mongo ``{"$oid": ...}`` value (or a plain id string)."""
    if isinstance(value, dict):
        oid = value.get("$oid")
        return str(oid) if oid else None
    if isinstance(value, str) and value:
        return value
    return None


def _discord_id(value) -> int | None:
    """Read a Mongo ``{"$numberLong": ...}`` value (or a plain integer)."""
    if isinstance(value, dict):
        value = value.get("$numberLong")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_json(path: Path) -> list:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"{path} does not contain a JSON list")
    return data


def parse_users(rows: list) -> tuple[dict[str, OldUser], list[str]]:
    users: dict[str, OldUser] = {}
    warnings: list[str] = []
    for index, row in enumerate(rows, start=1):
        old_id = _object_id(row.get("_id"))
        discord_user_id = _discord_id(row.get("discord_user_id"))
        if old_id is None or discord_user_id is None:
            warnings.append(f"user row {index}: missing ids, skipped")
            continue
        timezone = str(row.get("timezone") or "").strip() or None
        users[old_id] = OldUser(old_id, discord_user_id, timezone)
    return users, warnings


def parse_birthdays(
    rows: list, users: dict[str, OldUser]
) -> tuple[list[BirthdayRecord], list[str]]:
    records: list[BirthdayRecord] = []
    warnings: list[str] = []
    for index, row in enumerate(rows, start=1):
        old_id = _object_id(row.get("user_id"))
        user = users.get(old_id) if old_id else None
        if user is None:
            warnings.append(
                f"birthday row {index}: unknown user {old_id!r}, skipped"
            )
            continue
        try:
            month = int(row["month"])
            day = int(row["day"])
        except (KeyError, TypeError, ValueError):
            warnings.append(
                f"birthday row {index}: invalid month/day, skipped"
            )
            continue
        if not valid_date(month, day):
            warnings.append(
                f"birthday row {index}: {month:02d}-{day:02d} is not a date"
            )
            continue

        tz = user.timezone
        if tz == "CST6CDT":
            tz = "America/Chicago"
        elif tz == "US/Hawaii":
            tz = "Pacific/Honolulu"
        timezone = tz if valid_timezone(tz) else None
        if timezone is None:
            warnings.append(
                f"birthday for {user.discord_user_id}: unknown timezone "
                f"{user.timezone!r}, using UTC"
            )
            timezone = "UTC"
        records.append(
            BirthdayRecord(
                user_id=user.discord_user_id,
                month=month,
                day=day,
                timezone=timezone,
            )
        )
    return records, warnings


def parse_cards(
    rows: list, users: dict[str, OldUser]
) -> tuple[list[CardRecord], list[str]]:
    records: list[CardRecord] = []
    warnings: list[str] = []
    for index, row in enumerate(rows, start=1):
        kind = str(row.get("_kind") or "").strip()
        content = str(row.get("content") or "").strip()
        if kind not in (CARD_TEXT, CARD_IMAGE):
            warnings.append(f"card row {index}: unknown kind {kind!r}, skipped")
            continue
        if not content:
            warnings.append(f"card row {index}: empty content, skipped")
            continue

        raw_user = row.get("user_id")
        old_id = _object_id(raw_user)
        user_id: int | None = None
        if old_id is not None:
            user = users.get(old_id)
            if user is None:
                warnings.append(
                    f"card row {index}: unknown user {old_id!r}, skipped"
                )
                continue
            user_id = user.discord_user_id
        elif raw_user is not None:
            user_id = _discord_id(raw_user)
            if user_id is None:
                warnings.append(
                    f"card row {index}: invalid user reference, skipped"
                )
                continue

        records.append(CardRecord(kind=kind, content=content, user_id=user_id))
    return records, warnings


def find_export(folder: Path, name: str) -> Path:
    matches = sorted(folder.glob(f"*{name}"))
    if not matches:
        raise FileNotFoundError(f"No *{name} file found in {folder}")
    return matches[0]


async def import_birthdays(
    service: BirthdayService,
    records: list[BirthdayRecord],
    *,
    dry_run: bool,
    overwrite: bool,
) -> tuple[int, int, int, int]:
    added = updated = skipped = failed = 0
    for record in records:
        try:
            existing = await service.get_birthday(record.user_id)
            if existing is not None and not overwrite:
                print(f"  skip birthday for {record.user_id}: already present")
                skipped += 1
                continue
            if dry_run:
                action = "would update" if existing is not None else "would add"
            else:
                await service.save_birthday(
                    user_id=record.user_id,
                    month=record.month,
                    day=record.day,
                    timezone=record.timezone,
                )
                action = "updated" if existing is not None else "added"
            print(
                f"  {action} birthday for {record.user_id}: "
                f"{record.month:02d}-{record.day:02d} {record.timezone}"
            )
            if existing is not None:
                updated += 1
            else:
                added += 1
        except BirthdayError as exc:
            print(f"  fail birthday for {record.user_id}: {exc.key}")
            failed += 1
    return added, updated, skipped, failed


async def import_cards(
    service: BirthdayService,
    records: list[CardRecord],
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    messages = await service.list_messages()
    images = await service.list_images()
    seen_messages = {
        (message.user_id, message.content.strip()) for message in messages
    }
    seen_images = {(image.user_id, image.url.strip()) for image in images}

    added = skipped = failed = 0
    for record in records:
        key = (record.user_id, record.content)
        seen = seen_messages if record.kind == CARD_TEXT else seen_images
        label = "message" if record.kind == CARD_TEXT else "image"
        if key in seen:
            print(f"  skip {label} for {record.user_id or 'everyone'}: duplicate")
            skipped += 1
            continue
        try:
            if dry_run:
                print(
                    f"  would add {label} for "
                    f"{record.user_id or 'everyone'}: {record.content[:60]!r}"
                )
            else:
                if record.kind == CARD_TEXT:
                    await service.add_message(
                        content=record.content, user_id=record.user_id
                    )
                else:
                    await service.add_image(
                        url=record.content, user_id=record.user_id
                    )
                print(
                    f"  added {label} for "
                    f"{record.user_id or 'everyone'}: {record.content[:60]!r}"
                )
            seen.add(key)
            added += 1
        except BirthdayError as exc:
            print(f"  fail {label} {record.content[:40]!r}: {exc.key}")
            failed += 1
    return added, skipped, failed


async def run(args: argparse.Namespace) -> None:
    folder = Path(args.folder)
    users_path = Path(args.users) if args.users else find_export(folder, "users.json")
    birthdays_path = (
        Path(args.birthdays)
        if args.birthdays
        else find_export(folder, "birthdays_birthdays.json")
    )
    cards_path = (
        Path(args.cards)
        if args.cards
        else find_export(folder, "birthdays_cards.json")
    )

    users, user_warnings = parse_users(load_json(users_path))
    birthday_records, birthday_warnings = parse_birthdays(
        load_json(birthdays_path), users
    )
    card_records, card_warnings = parse_cards(load_json(cards_path), users)

    print(f"Folder: {folder}")
    print(f"Users: {users_path.name} ({len(users)} usable)")
    print(
        f"Birthdays: {birthdays_path.name} "
        f"({len(birthday_records)} ready)"
    )
    print(f"Cards: {cards_path.name} ({len(card_records)} ready)\n")

    warnings = user_warnings + birthday_warnings + card_warnings
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  {warning}")
        print()

    config = load_config(Path(args.config))
    guild_id = int(args.guild_id or config.guild_id)
    init_db(config.mysql_url)
    await create_tables()
    try:
        service = BirthdayService(guild_id)
        print(f"Guild: {guild_id}")
        print(f"Mode: {'dry-run' if args.dry_run else 'write'}\n")

        print("Birthdays:")
        birthday_result = await import_birthdays(
            service,
            birthday_records,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
        print("\nCards:")
        card_result = await import_cards(
            service, card_records, dry_run=args.dry_run
        )

        added, updated, skipped, failed = birthday_result
        print(
            "\nBirthdays done: "
            f"{added} added, {updated} updated, "
            f"{skipped} skipped, {failed} failed."
        )
        added, skipped, failed = card_result
        print(
            f"Cards done: {added} added, "
            f"{skipped} skipped, {failed} failed."
        )
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folder",
        nargs="?",
        default=str(DEFAULT_FOLDER),
        help="Folder with the Mongo JSON exports (default: bkp/birthdays)",
    )
    parser.add_argument(
        "--config", default=str(ROOT / "config.yaml"), help="Path to config.yaml"
    )
    parser.add_argument(
        "--guild-id", type=int, default=None,
        help="Override the guild id from config.yaml",
    )
    parser.add_argument(
        "--users", default=None, help="Explicit users JSON path"
    )
    parser.add_argument(
        "--birthdays", default=None, help="Explicit birthdays JSON path"
    )
    parser.add_argument(
        "--cards", default=None, help="Explicit cards JSON path"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Update birthdays already registered for a member",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without writing",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
