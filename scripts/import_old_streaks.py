"""Import the old Ruby bot's streaks into the new streak tables.

Reads the four Mongo JSON exports (``users``, ``streaks_goals``,
``streaks_streaks`` and ``streaks_daily_logs``), converts them to the new
architecture and wipes/reinserts the streak history of every member found in
the goals export. The members' ``user_xp`` pool is never touched: XP awarded by
the old streak coupons already lives in the old level system and is not
imported again.

What the import does:

- the timezone always comes from the ``users`` export (the old app did the same;
  the duplicated ``timezone`` on goals is only a fallback);
- ``current_level`` is discarded and the internal level is derived from
  ``days_logged`` (``days_logged // 5``);
- old normal rescues are carried over and one golden rescue is granted for
  every 50 completed levels;
- old XP coupon records are ignored, so the streak XP counters start at zero;
- duplicate logs on the same local date keep the newest real log (a repair is
  only used when the day has no real log);
- ``start_date`` is pulled back to the first imported log, and closed streaks
  get ``end_date = last_log_date + 1`` as the new model expects;
- logs pointing at streaks absent from the export are reported and skipped.

Usage (from the repository root, with the production config.yaml present):

    .venv/bin/python scripts/import_old_streaks.py bkp/streaks_2026-10-19 --dry-run
    .venv/bin/python scripts/import_old_streaks.py bkp/streaks_2026-10-19

The script is destructive for the imported members: their existing
``streak_goals``, ``streaks`` and ``streak_daily_logs`` rows in the target guild
are deleted before the new rows are inserted. Always run ``--dry-run`` first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db, session_scope  # noqa: E402
from bot.models import Streak, StreakGoal, StreakLog  # noqa: E402
from bot.services import streaks as streak_service  # noqa: E402

SOURCES = {
    "users": "*.users.json",
    "goals": "*.streaks_goals.json",
    "streaks": "*.streaks_streaks.json",
    "logs": "*.streaks_daily_logs.json",
}

TIMEZONE_ALIASES = {
    "Moscow": "Europe/Moscow",
    "CST6CDT": "America/Denver",
}

UNIT_FIXES = {
    "minnute": "minutes",
    "minnutes": "minutes",
}


@dataclass(frozen=True)
class OldUser:
    oid: str
    discord_user_id: int
    timezone: str


@dataclass(frozen=True)
class OldGoal:
    oid: str
    user_oid: str
    discord_user_id: int | None
    amount: int
    unit: str
    timezone: str | None
    reminder: str
    custom_hour: int | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class OldStreak:
    oid: str
    goal_oid: str
    status: str
    start_date: date
    end_date: date | None
    days_logged: int
    rescues_available: int
    rescues_used: int
    xp_cards: int
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class OldLog:
    oid: str
    streak_oid: str
    date: datetime
    amount: int
    percent: int
    status: str
    repair: bool
    created_at: datetime | None
    updated_at: datetime | None


@dataclass
class PreparedLog:
    log_date: date
    amount: int
    percent: int
    outcome: str
    repair: bool
    rescue_type: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass
class PreparedStreak:
    start_date: date
    end_date: date | None
    last_log_date: date | None
    status: str
    level: int
    days_logged: int
    rescues_available: int
    rescues_used: int
    golden_available: int
    logs: list[PreparedLog]
    created_at: datetime | None
    updated_at: datetime | None


@dataclass
class PreparedGoal:
    user_id: int
    amount: int
    unit: str
    timezone: str
    reminder: str
    custom_hour: int | None
    created_at: datetime | None
    updated_at: datetime | None
    streaks: list[PreparedStreak] = field(default_factory=list)


class Findings:
    """Accumulates data-quality findings instead of dumping thousands or rows."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.samples: dict[str, list[str]] = defaultdict(list)

    def add(self, key: str, sample: str | None = None) -> None:
        self.counts[key] += 1
        if sample is not None and len(self.samples[key]) < 5:
            self.samples[key].append(sample)

    def print(self) -> None:
        if not self.counts:
            print("\nData findings: none.")
            return
        print("\nData findings:")
        for key in sorted(self.counts):
            print(f"  {key}: {self.counts[key]}")
            for sample in self.samples[key]:
                print(f"    - {sample}")


# --------------------------------------------------------------- JSON helpers


def _unwrap(value):
    """Unwrap Mongo's extended JSON scalars (``$oid``/``$numberLong``/...)."""
    if isinstance(value, dict):
        if "$oid" in value:
            return str(value["$oid"])
        if "$numberLong" in value:
            return int(value["$numberLong"])
        if "$numberInt" in value:
            return int(value["$numberInt"])
        if "$numberDouble" in value:
            return float(value["$numberDouble"])
        if "$date" in value:
            return _parse_moment(value["$date"])
    return value


def _parse_moment(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _plain_date(value) -> date | None:
    """Read a Mongo Date field as the calendar date it stores (UTC midnight)."""
    moment = _parse_moment(_unwrap(value))
    if moment is None:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.date()


def _oid(value) -> str | None:
    unwrapped = _unwrap(value)
    return str(unwrapped) if unwrapped is not None else None


def _discover(path: Path) -> dict[str, Path]:
    if path.is_file():
        raise SystemExit(
            f"{path} is a file; pass the directory containing the four exports."
        )
    found: dict[str, Path] = {}
    for key, pattern in SOURCES.items():
        matches = sorted(path.glob(pattern))
        if not matches:
            raise SystemExit(f"No {key} export matching {pattern} in {path}")
        if len(matches) > 1:
            names = ", ".join(match.name for match in matches)
            raise SystemExit(f"Multiple {key} exports in {path}: {names}")
        found[key] = matches[0]
    return found


def _load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise SystemExit(f"Expected a JSON array in {path}")
    return data


def _canonical_timezone(name: str | None) -> str | None:
    if not name:
        return None
    candidate = TIMEZONE_ALIASES.get(name.strip(), name.strip())
    try:
        ZoneInfo(candidate)
    except Exception:
        return None
    return candidate


def _normalize_unit(unit: str, findings: Findings) -> str:
    cleaned = (unit or "").strip()
    fixed = UNIT_FIXES.get(cleaned.lower())
    if fixed is not None:
        findings.add("unit spelling corrected", f"{cleaned!r} -> {fixed!r}")
        return fixed
    return cleaned


# -------------------------------------------------------------------- parsing


def parse_users(rows: list[dict]) -> tuple[dict[str, OldUser], dict[int, OldUser]]:
    by_oid: dict[str, OldUser] = {}
    by_discord: dict[int, OldUser] = {}
    for row in rows:
        oid = _oid(row.get("_id"))
        discord_id = _unwrap(row.get("discord_user_id"))
        timezone_name = row.get("timezone")
        if oid is None or discord_id is None or not timezone_name:
            continue
        user = OldUser(
            oid=oid,
            discord_user_id=int(discord_id),
            timezone=str(timezone_name),
        )
        by_oid[oid] = user
        by_discord[user.discord_user_id] = user
    return by_oid, by_discord


def parse_goals(rows: list[dict]) -> list[OldGoal]:
    goals: list[OldGoal] = []
    for row in rows:
        oid = _oid(row.get("_id"))
        user_oid = _oid(row.get("user_id"))
        discord_id = _unwrap(row.get("discord_user_id"))
        start = _parse_moment(_unwrap(row.get("created_at")))
        updated = _parse_moment(_unwrap(row.get("updated_at")))
        if oid is None or user_oid is None:
            continue
        goals.append(
            OldGoal(
                oid=oid,
                user_oid=user_oid,
                discord_user_id=int(discord_id) if discord_id is not None else None,
                amount=int(row.get("amount") or 0),
                unit=str(row.get("unit") or ""),
                timezone=row.get("timezone"),
                reminder=str(row.get("_alert") or "none"),
                custom_hour=(
                    int(row["custom_alert_hour"])
                    if row.get("custom_alert_hour") is not None
                    else None
                ),
                created_at=start,
                updated_at=updated,
            )
        )
    return goals


def parse_streaks(rows: list[dict]) -> list[OldStreak]:
    streaks: list[OldStreak] = []
    for row in rows:
        oid = _oid(row.get("_id"))
        goal_oid = _oid(row.get("goal_id"))
        start = _plain_date(row.get("start_date"))
        if oid is None or goal_oid is None or start is None:
            continue
        rescues = row.get("rescues") or []
        available = sum(
            1
            for rescue in rescues
            if _unwrap(rescue.get("_status")) == "available"
        )
        used = sum(
            1 for rescue in rescues if _unwrap(rescue.get("_status")) == "used"
        )
        streaks.append(
            OldStreak(
                oid=oid,
                goal_oid=goal_oid,
                status=str(row.get("_status") or "broken"),
                start_date=start,
                end_date=_plain_date(row.get("end_date")),
                days_logged=int(row.get("days_logged") or 0),
                rescues_available=available,
                rescues_used=used,
                xp_cards=len(row.get("xp_cards") or []),
                created_at=_parse_moment(_unwrap(row.get("created_at"))),
                updated_at=_parse_moment(_unwrap(row.get("updated_at"))),
            )
        )
    return streaks


def parse_logs(rows: list[dict]) -> list[OldLog]:
    logs: list[OldLog] = []
    for row in rows:
        oid = _oid(row.get("_id"))
        streak_oid = _oid(row.get("streak_id"))
        moment = _parse_moment(_unwrap(row.get("date")))
        if oid is None or streak_oid is None or moment is None:
            continue
        logs.append(
            OldLog(
                oid=oid,
                streak_oid=streak_oid,
                date=moment,
                amount=int(row.get("amount") or 0),
                percent=int(row.get("percent") or 0),
                status=str(row.get("_status") or ""),
                repair=bool(row.get("repair")),
                created_at=_parse_moment(_unwrap(row.get("created_at"))),
                updated_at=_parse_moment(_unwrap(row.get("updated_at"))),
            )
        )
    return logs


# ------------------------------------------------------------- transformation


def _local_date(moment: datetime, timezone_name: str) -> date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(timezone_name)).date()


def _log_order(log: OldLog) -> tuple[datetime, str]:
    moment = log.updated_at or log.created_at
    if moment is None:
        return datetime.min, log.oid
    return moment.replace(tzinfo=None), log.oid


def _dedupe_logs(
    old_logs: list[OldLog], timezone_name: str, findings: Findings
) -> list[tuple[date, OldLog]]:
    """One log per local date, preferring a real log over a rescue patch."""
    grouped: dict[date, list[OldLog]] = defaultdict(list)
    for log in old_logs:
        grouped[_local_date(log.date, timezone_name)].append(log)

    chosen: list[tuple[date, OldLog]] = []
    for day in sorted(grouped):
        group = grouped[day]
        if len(group) > 1:
            findings.add(
                "duplicate logs on one local date",
                f"{day} has {len(group)} rows",
            )
        real = [log for log in group if not log.repair]
        pool = real or group
        chosen.append((day, max(pool, key=_log_order)))
    return chosen


def _outcome_for(log: OldLog) -> str:
    if log.amount <= 0:
        return streak_service.OUTCOME_ZERO
    if log.status == "successful":
        return streak_service.OUTCOME_SUCCESS
    if log.status == "failed":
        return streak_service.OUTCOME_FAILED
    return (
        streak_service.OUTCOME_SUCCESS
        if log.percent >= 100
        else streak_service.OUTCOME_FAILED
    )


def _normalize_reminder(goal: OldGoal, findings: Findings) -> tuple[str, int | None]:
    reminder = goal.reminder
    if reminder not in streak_service.REMINDERS:
        findings.add(
            "unknown reminder setting",
            f"goal {goal.oid}: {reminder!r} -> 'last_chance'",
        )
        reminder = "last_chance"
    hour = goal.custom_hour
    if reminder in ("custom", "both"):
        if hour is None or not (
            streak_service.CUSTOM_HOUR_MIN
            <= hour
            <= streak_service.CUSTOM_HOUR_MAX
        ):
            findings.add(
                "invalid custom reminder hour",
                f"goal {goal.oid}: {hour!r}; reminder kept without custom hour",
            )
            hour = None
    else:
        hour = None
    return reminder, hour


def _gap_days(logs: list[PreparedLog]) -> int:
    if len(logs) < 2:
        return 0
    span = (logs[-1].log_date - logs[0].log_date).days + 1
    return span - len(logs)


def prepare(
    users_by_oid: dict[str, OldUser],
    users_by_discord: dict[int, OldUser],
    goals: list[OldGoal],
    streaks: list[OldStreak],
    logs: list[OldLog],
    findings: Findings,
) -> list[PreparedGoal]:
    streak_by_oid = {streak.oid: streak for streak in streaks}
    streaks_by_goal: dict[str, list[OldStreak]] = defaultdict(list)
    for streak in streaks:
        streaks_by_goal[streak.goal_oid].append(streak)

    logs_by_streak: dict[str, list[OldLog]] = defaultdict(list)
    orphan_streaks: set[str] = set()
    for log in logs:
        if log.streak_oid not in streak_by_oid:
            orphan_streaks.add(log.streak_oid)
            findings.add("orphan daily log (no matching streak)")
            continue
        logs_by_streak[log.streak_oid].append(log)
    if orphan_streaks:
        findings.samples["orphan daily log (no matching streak)"] = [
            f"{len(orphan_streaks)} missing streak id(s), "
            f"e.g. {', '.join(sorted(orphan_streaks)[:3])}"
        ]

    prepared_goals: list[PreparedGoal] = []
    seen_users: set[int] = set()
    for goal in goals:
        user = users_by_oid.get(goal.user_oid)
        if user is None and goal.discord_user_id is not None:
            user = users_by_discord.get(goal.discord_user_id)
        if user is None:
            findings.add(
                "goal without a user record",
                f"goal {goal.oid} (user {goal.user_oid})",
            )
            continue

        timezone_name = _canonical_timezone(user.timezone)
        if timezone_name is None:
            timezone_name = _canonical_timezone(goal.timezone)
            if timezone_name is None:
                findings.add(
                    "goal with no valid timezone",
                    f"goal {goal.oid} (user {user.discord_user_id})",
                )
                continue
            findings.add(
                "user timezone invalid; used goal timezone",
                f"user {user.discord_user_id}: {user.timezone!r}",
            )
        elif goal.timezone and goal.timezone != user.timezone:
            findings.add(
                "goal timezone differs from user timezone",
                f"user {user.discord_user_id}: goal {goal.timezone!r}, "
                f"user {user.timezone!r}; user wins",
            )
        elif not goal.timezone:
            findings.add(
                "goal timezone missing; used user timezone",
                f"goal {goal.oid}",
            )

        if user.discord_user_id in seen_users:
            findings.add(
                "duplicate goal for one member",
                f"goal {goal.oid} (user {user.discord_user_id}) skipped",
            )
            continue
        seen_users.add(user.discord_user_id)

        reminder, custom_hour = _normalize_reminder(goal, findings)
        prepared_goal = PreparedGoal(
            user_id=user.discord_user_id,
            amount=goal.amount,
            unit=_normalize_unit(goal.unit, findings),
            timezone=timezone_name,
            reminder=reminder,
            custom_hour=custom_hour,
            created_at=_naive_utc(goal.created_at),
            updated_at=_naive_utc(goal.updated_at),
        )

        for old_streak in streaks_by_goal.get(goal.oid, []):
            chosen = _dedupe_logs(
                logs_by_streak.get(old_streak.oid, []),
                timezone_name,
                findings,
            )
            prepared_logs = [
                PreparedLog(
                    log_date=day,
                    amount=log.amount,
                    percent=log.percent,
                    outcome=_outcome_for(log),
                    repair=log.repair,
                    rescue_type=(
                        streak_service.RESCUE_NORMAL if log.repair else None
                    ),
                    created_at=_naive_utc(log.created_at),
                    updated_at=_naive_utc(log.updated_at),
                )
                for day, log in chosen
            ]

            if prepared_logs:
                first_log = prepared_logs[0].log_date
                last_log = prepared_logs[-1].log_date
            else:
                first_log = last_log = None

            if first_log is not None and first_log < old_streak.start_date:
                findings.add(
                    "first log before streak start; start pulled back",
                    f"streak {old_streak.oid}: {old_streak.start_date} -> "
                    f"{first_log}",
                )
                start_date = first_log
            else:
                start_date = old_streak.start_date

            ongoing = old_streak.status == "ongoing"
            if ongoing:
                status = streak_service.STATUS_ONGOING
                end_date = None
            else:
                status = streak_service.STATUS_CLOSED
                end_date = (
                    last_log + timedelta(days=1)
                    if last_log is not None
                    else old_streak.end_date
                )

            if (
                not ongoing
                and old_streak.end_date is not None
                and end_date is not None
                and old_streak.end_date != end_date
            ):
                findings.add(
                    "closed streak end date normalized",
                    f"streak {old_streak.oid}: {old_streak.end_date} -> "
                    f"{end_date}",
                )

            if prepared_logs and old_streak.days_logged != len(prepared_logs):
                findings.add(
                    "days_logged differs from imported log count",
                    f"streak {old_streak.oid}: days_logged="
                    f"{old_streak.days_logged}, logs={len(prepared_logs)}",
                )

            gaps = _gap_days(prepared_logs)
            if ongoing and gaps:
                findings.add(
                    "ongoing streak contains missing day(s)",
                    f"streak {old_streak.oid}: {gaps} missing day(s) kept as-is",
                )

            if old_streak.status != "ongoing" and old_streak.status != "broken":
                findings.add(
                    "unknown streak status treated as closed",
                    f"streak {old_streak.oid}: {old_streak.status!r}",
                )

            level = max(0, old_streak.days_logged // streak_service.LEVEL_THRESHOLD)
            golden_available = (level // 50) + (level // 73)
            prepared_goal.streaks.append(
                PreparedStreak(
                    start_date=start_date,
                    end_date=end_date,
                    last_log_date=last_log,
                    status=status,
                    level=level,
                    days_logged=old_streak.days_logged,
                    rescues_available=old_streak.rescues_available,
                    rescues_used=old_streak.rescues_used,
                    golden_available=golden_available,
                    logs=prepared_logs,
                    created_at=_naive_utc(old_streak.created_at),
                    updated_at=_naive_utc(old_streak.updated_at),
                )
            )

            if old_streak.xp_cards:
                findings.add(
                    "old XP coupons ignored",
                    f"streak {old_streak.oid}: {old_streak.xp_cards} card(s)",
                )

        prepared_goal.streaks.sort(key=lambda streak: streak.start_date)
        prepared_goals.append(prepared_goal)

    return prepared_goals


# ------------------------------------------------------------------ database


async def _target_rows(
    session, guild_id: int, user_ids: list[int]
) -> tuple[list[int], list[int], int]:
    """Existing goal/streak ids and log count for the members being replaced."""
    goal_ids = list(
        await session.scalars(
            select(StreakGoal.id).where(
                StreakGoal.guild_id == guild_id,
                StreakGoal.user_id.in_(user_ids),
            )
        )
    )
    streak_ids = (
        list(
            await session.scalars(
                select(Streak.id).where(Streak.goal_id.in_(goal_ids))
            )
        )
        if goal_ids
        else []
    )
    log_count = 0
    if streak_ids:
        log_count = int(
            await session.scalar(
                select(func.count())
                .select_from(StreakLog)
                .where(StreakLog.streak_id.in_(streak_ids))
            )
            or 0
        )
    return goal_ids, streak_ids, log_count


async def _wipe(
    session, goal_ids: list[int], streak_ids: list[int]
) -> None:
    if streak_ids:
        await session.execute(
            delete(StreakLog).where(StreakLog.streak_id.in_(streak_ids))
        )
    if goal_ids:
        await session.execute(
            delete(Streak).where(Streak.goal_id.in_(goal_ids))
        )
        await session.execute(
            delete(StreakGoal).where(StreakGoal.id.in_(goal_ids))
        )


def _with_timestamps(row, created_at, updated_at):
    """Set imported timestamps only when present, leaving server defaults."""
    if created_at is not None:
        row.created_at = created_at
    if updated_at is not None:
        row.updated_at = updated_at
    return row


async def _insert(
    session, guild_id: int, prepared_goals: list[PreparedGoal]
) -> tuple[int, int, int]:
    streak_count = 0
    log_count = 0
    for prepared in prepared_goals:
        goal = StreakGoal(
            guild_id=guild_id,
            user_id=prepared.user_id,
            amount=prepared.amount,
            unit=prepared.unit,
            timezone=prepared.timezone,
            reminder=prepared.reminder,
            custom_hour=prepared.custom_hour,
        )
        _with_timestamps(goal, prepared.created_at, prepared.updated_at)
        session.add(goal)
        await session.flush()

        for old_streak in prepared.streaks:
            streak = Streak(
                guild_id=guild_id,
                user_id=prepared.user_id,
                goal_id=goal.id,
                start_date=old_streak.start_date,
                end_date=old_streak.end_date,
                status=old_streak.status,
                level=old_streak.level,
                days_logged=old_streak.days_logged,
                rescues_available=old_streak.rescues_available,
                rescues_used=old_streak.rescues_used,
                golden_available=old_streak.golden_available,
                golden_used=0,
                last_xp_amount=0,
                total_xp_awarded=0,
                last_xp_at=None,
                window_xp_paid=0,
                last_log_date=old_streak.last_log_date,
            )
            _with_timestamps(
                streak, old_streak.created_at, old_streak.updated_at
            )
            session.add(streak)
            await session.flush()
            streak_count += 1

            session.add_all(
                [
                    _with_timestamps(
                        StreakLog(
                            streak_id=streak.id,
                            log_date=log.log_date,
                            amount=log.amount,
                            percent=log.percent,
                            outcome=log.outcome,
                            repair=log.repair,
                            rescue_type=log.rescue_type,
                        ),
                        log.created_at,
                        log.updated_at,
                    )
                    for log in old_streak.logs
                ]
            )
            log_count += len(old_streak.logs)

    return len(prepared_goals), streak_count, log_count


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    guild_id = int(args.guild_id if args.guild_id else config.guild_id)
    paths = _discover(Path(args.path))

    findings = Findings()
    users_by_oid, users_by_discord = parse_users(_load_rows(paths["users"]))
    goals = parse_goals(_load_rows(paths["goals"]))
    streaks = parse_streaks(_load_rows(paths["streaks"]))
    logs = parse_logs(_load_rows(paths["logs"]))
    prepared_goals = prepare(
        users_by_oid,
        users_by_discord,
        goals,
        streaks,
        logs,
        findings,
    )

    imported_log_count = sum(
        len(old_streak.logs)
        for goal in prepared_goals
        for old_streak in goal.streaks
    )
    user_ids = [goal.user_id for goal in prepared_goals]

    print(f"Guild: {guild_id}")
    print(f"Source: {Path(args.path)}")
    print(f"  users: {len(users_by_oid)}")
    print(f"  goals: {len(goals)}")
    print(f"  streaks: {len(streaks)}")
    print(f"  logs: {len(logs)}")
    print(
        f"Prepared: {len(prepared_goals)} goal(s), "
        f"{sum(len(goal.streaks) for goal in prepared_goals)} streak(s), "
        f"{imported_log_count} log(s)"
    )
    if len(prepared_goals) != len(goals):
        print(f"Skipped {len(goals) - len(prepared_goals)} unusable goal(s).")

    findings.print()

    if not prepared_goals:
        print("\nNothing to import.")
        return

    init_db(config.mysql_url)
    created_goals = created_streaks = created_logs = 0
    try:
        await create_tables()
        async with session_scope() as session:
            goal_ids, streak_ids, existing_logs = await _target_rows(
                session, guild_id, user_ids
            )
            print(
                f"\nExisting rows for the {len(user_ids)} target member(s): "
                f"{len(goal_ids)} goal(s), {len(streak_ids)} streak(s), "
                f"{existing_logs} log(s)."
            )
            print(
                f"Mode: "
                f"{'dry-run' if args.dry_run else 'write (wipe + reinsert)'}\n"
            )

            if not args.dry_run:
                await _wipe(session, goal_ids, streak_ids)
                created_goals, created_streaks, created_logs = await _insert(
                    session, guild_id, prepared_goals
                )
                await session.commit()
    finally:
        await close_db()

    if args.dry_run:
        print("Dry run: no changes written.")
        return

    print(
        f"Imported: {created_goals} goal(s), {created_streaks} streak(s), "
        f"{created_logs} log(s)."
    )
    print("User XP was not touched; old streak coupon XP was not imported.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default="bkp/streaks_2026-10-19",
        help="Directory containing the four Mongo JSON exports "
        "(default: bkp/streaks_2026-10-19)",
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.yaml"),
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--guild-id",
        type=int,
        default=None,
        help="Override the guild id from config.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the findings and what would be imported without writing",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
