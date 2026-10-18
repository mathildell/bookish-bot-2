"""Put a member's streak into a known state so every edge case can be eyeballed.

This is a development helper, not a production tool. It wipes the target
member's streak history (``streak_goals``, ``streaks`` and ``streak_daily_logs``
for the guild) and seeds a fresh scenario so that ``/streak view`` and
``/streak log`` render the desired outcome right now. Nothing outside the
streak tables is touched: the member's ``user_xp`` pool, reward roles, etc. are
left exactly as they were.

Usage (from the repository root, with config.yaml present):

    .venv/bin/python scripts/streak_state.py 123456789 state-name
    .venv/bin/python scripts/streak_state.py --list

    # Add --guild-id / --config / --timezone / --amount / --unit as needed.
    .venv/bin/python scripts/streak_state.py 123456789 xp-five-success --amount 20

    # Generate a random streak of N logged days with every derived field
    # (level, coupons, XP) computed exactly as the game would. The last log
    # defaults to yesterday; pass --last-log YYYY-MM-DD to move it, and --seed
    # to make the random outcomes repeatable. A last log older than yesterday
    # seeds the streak closed, so the rescue flow can be tested too.
    .venv/bin/python scripts/streak_state.py 123456789 random --days 49
    .venv/bin/python scripts/streak_state.py 123456789 random --days 49 \
        --last-log 2026-10-01 --seed 7

Every state is documented below: what the seeded data makes ``/streak view``
or ``/streak log`` show, and how that data is produced. The state builders all
work in the goal's timezone and are anchored on "today", so the outcome is
stable as long as you run the Discord command before the local day rolls over.

State builders create rows directly (rather than going through
:class:`~bot.services.streaks.StreakService`) because closed/rescue/anniversary
states cannot be produced by the normal logging flow.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db, session_scope  # noqa: E402
from bot.models import Streak, StreakGoal, StreakLog, StreakSetting  # noqa: E402
from bot.services import streaks as streak_service  # noqa: E402

LEVEL_THRESHOLD = streak_service.LEVEL_THRESHOLD
ONGOING = streak_service.STATUS_ONGOING
CLOSED = streak_service.STATUS_CLOSED
SUCCESS = streak_service.OUTCOME_SUCCESS
FAILED = streak_service.OUTCOME_FAILED
ZERO = streak_service.OUTCOME_ZERO
RESCUE_NORMAL = streak_service.RESCUE_NORMAL
RESCUE_GOLDEN = streak_service.RESCUE_GOLDEN


@dataclass
class Context:
    session: object
    guild_id: int
    user_id: int
    today: date
    timezone: str
    amount: int
    unit: str
    days: int | None = None
    rng: random.Random | None = None
    last_log: date | None = None


@dataclass
class State:
    name: str
    comment: str
    build: Callable[[Context], object]
    build_goal: bool
    log_amount: str | None
    expect: str


# --------------------------------------------------------------- primitives


def _amount_for(outcome: str, goal_amount: int) -> int:
    """A representative amount that produces ``outcome`` for ``goal_amount``."""
    if outcome == SUCCESS:
        return goal_amount
    if outcome == ZERO:
        return 0
    return max(1, goal_amount - 1)


def _add_goal(
    ctx: Context, *, reminder: str = "none", custom_hour: int | None = None
) -> StreakGoal:
    goal = StreakGoal(
        guild_id=ctx.guild_id,
        user_id=ctx.user_id,
        amount=ctx.amount,
        unit=ctx.unit,
        timezone=ctx.timezone,
        reminder=reminder,
        custom_hour=custom_hour,
    )
    ctx.session.add(goal)
    return goal


async def _add_streak(
    ctx: Context,
    goal: StreakGoal,
    *,
    outcomes: list[str],
    end_day: date,
    days_logged: int | None = None,
    level: int | None = None,
    start_date: date | None = None,
    status: str = ONGOING,
    end_date: date | None = None,
    rescues_available: int = 0,
    rescues_used: int = 0,
    golden_available: int = 0,
    golden_used: int = 0,
    last_xp_amount: int = 0,
    total_xp_awarded: int = 0,
    last_xp_at: datetime | None = None,
    window_xp_paid: int = 0,
    repair_offset: int | None = None,
    rescue_type: str = RESCUE_NORMAL,
) -> Streak:
    """Create one streak plus its oldest-to-newest logs ending on ``end_day``.

    ``days_logged`` may exceed the number of seeded logs to imitate a streak
    whose older days were purged after :data:`LOG_RETENTION_DAYS`. ``level`` is
    the *internal* streak level (``days_logged // 5`` at the time of writing).
    ``repair_offset`` turns that index into a rescued zero day.
    """
    await ctx.session.flush()
    days = days_logged if days_logged is not None else len(outcomes)
    streak = Streak(
        guild_id=ctx.guild_id,
        user_id=ctx.user_id,
        goal_id=goal.id,
        start_date=start_date
        or (end_day - timedelta(days=len(outcomes) - 1)),
        end_date=end_date,
        status=status,
        level=level if level is not None else days // LEVEL_THRESHOLD,
        days_logged=days,
        rescues_available=rescues_available,
        rescues_used=rescues_used,
        golden_available=golden_available,
        golden_used=golden_used,
        last_xp_amount=last_xp_amount,
        total_xp_awarded=total_xp_awarded,
        last_xp_at=last_xp_at,
        window_xp_paid=window_xp_paid,
        last_log_date=end_day,
    )
    ctx.session.add(streak)
    await ctx.session.flush()

    first_day = end_day - timedelta(days=len(outcomes) - 1)
    for offset, outcome in enumerate(outcomes):
        is_repair = repair_offset is not None and offset == repair_offset
        amount = 0 if is_repair else _amount_for(outcome, goal.amount)
        ctx.session.add(
            StreakLog(
                streak_id=streak.id,
                log_date=first_day + timedelta(days=offset),
                amount=amount,
                percent=0 if is_repair else streak_service.percent_for(
                    amount, goal.amount
                ),
                outcome=ZERO if is_repair else outcome,
                repair=is_repair,
                rescue_type=rescue_type if is_repair else None,
            )
        )
    await ctx.session.flush()
    return streak


# ------------------------------------------------------------- state builders


async def _unregistered(ctx: Context) -> None:
    """Needs no data at all: the goal is deleted and never recreated.

    What you see: ``/streak view`` and ``/streak log`` both answer with
    "No streak goal found. Run ``/streak register``!".
    How: leave the streak tables empty for this member.
    """


async def _no_streak(ctx: Context) -> None:
    """A goal but no streak yet.

    What you see: ``/streak view`` -> "No streak yet" plus the goal fields
    (timezone, reminders, daily goal). ``/streak log 30`` -> starts day 1.
    How: create the goal, add no ``streaks`` rows.
    """
    _add_goal(ctx)


async def _ongoing_day1(ctx: Context) -> None:
    """One successful day, logged today.

    What you see: ``/streak view`` -> one highlighted success slot, "Logged
    today", level 1, an "Next XP" projection. ``/streak log 15`` -> edits
    today's amount (no new day, days_logged stays 1).
    How: a 1-day streak whose only log is today (deadline is still tomorrow).
    """
    goal = _add_goal(ctx)
    await _add_streak(ctx, goal, outcomes=[SUCCESS], end_day=ctx.today, days_logged=1)


async def _ongoing_mixed(ctx: Context) -> None:
    """Four consecutive days ending yesterday with mixed outcomes.

    What you see: ``/streak view`` -> four slots (success, failed, zero,
    success) and one upcoming; "Last achievement" reflects yesterday; the
    "Next XP" projection is partial (2 successes + assumed days).
    How: a 4-day streak ending yesterday with outcomes [S, F, Z, S].
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS, FAILED, ZERO, SUCCESS],
        end_day=ctx.today - timedelta(days=1),
    )


async def _window_complete(ctx: Context) -> None:
    """A window completed earlier today (days_logged = 5, level 1).

    What you see: ``/streak view`` -> the five completed days stay on screen
    until midnight (the just-finished window is not reset to blanks).
    How: a 5-day streak of successes whose last log is today.
    """
    goal = _add_goal(ctx)
    await _add_streak(ctx, goal, outcomes=[SUCCESS] * 5, end_day=ctx.today)


async def _xp_five_success(ctx: Context) -> None:
    """Four successes, one away from clearing level 1.

    What you see: ``/streak log 30`` -> "Level cleared" worth **250 XP**
    (level 1 paid in full) and a rescue coupon granted.
    How: a 4-day all-success streak ending yesterday, internal level 0.
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS] * 4,
        end_day=ctx.today - timedelta(days=1),
    )


async def _xp_five_failure(ctx: Context) -> None:
    """Four failed days, one away from clearing level 1.

    What you see: ``/streak log 5`` -> "Level cleared" for level 1 but worth
    **0 XP** (a failed day pays nothing), plus a rescue coupon. Shows the
    level-up and the level-up-without-XP edge at once.
    How: a 4-day all-failed streak ending yesterday, internal level 0.
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[FAILED] * 4,
        end_day=ctx.today - timedelta(days=1),
    )


async def _xp_partial(ctx: Context) -> None:
    """Four days (two success, two failed), one away from level 1.

    What you see: ``/streak log 30`` -> level 1 cleared worth **150 XP**
    (3 of 5 successes x 250). Shows a partially-reached level window.
    How: a 4-day streak ending yesterday with outcomes [S, F, S, F].
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS, FAILED, SUCCESS, FAILED],
        end_day=ctx.today - timedelta(days=1),
    )


async def _xp_top_up(ctx: Context) -> None:
    """A completed window whose fifth day is still below goal, ready to top up.

    What you see: ``/streak view`` -> five slots, four successes and today's
    failure; level 1 is already banked for a partial **200 XP**.
    ``/streak log 30`` -> tops the day up to 100% and pays the missing
    **50 XP** (250 total) with a "Goal reached" field. A later re-log below the
    goal keeps the XP (it is locked once full).
    How: 5 days ending today, the last one below goal, ``level=1`` and
    ``window_xp_paid=200``.
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS, SUCCESS, SUCCESS, SUCCESS, FAILED],
        end_day=ctx.today,
        window_xp_paid=200,
        last_xp_amount=200,
        total_xp_awarded=200,
        last_xp_at=streak_service.utcnow(),
    )


async def _xp_cap(ctx: Context) -> None:
    """High streak one day from level 31, where the XP payout caps.

    What you see: ``/streak log 30`` -> level 31 cleared worth **3000 XP**
    (the cap; level XP stops growing from here).
    How: 154 days / internal level 30 (only the last 4 logs are seeded; older
    ones are treated as purged history).
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS] * 4,
        end_day=ctx.today - timedelta(days=1),
        days_logged=154,
        level=30,
        start_date=ctx.today - timedelta(days=154),
    )


async def _level_golden_50(ctx: Context) -> None:
    """High streak one day from level 50, a golden-rescue milestone.

    What you see: ``/streak log 30`` -> level 50 cleared and a **golden**
    rescue granted on top of the normal one (a golden is paid every 50 levels).
    How: 249 days / internal level 49 (last 4 logs seeded).
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS] * 4,
        end_day=ctx.today - timedelta(days=1),
        days_logged=249,
        level=49,
        start_date=ctx.today - timedelta(days=249),
    )


async def _anniversary_365(ctx: Context) -> None:
    """One day from a full year (365 days / level 73).

    What you see: ``/streak log 30`` -> level 73 cleared, the "*A whole year*"
    anniversary embed posted, the XP doubled, and a golden rescue granted
    (anniversaries pay a golden too).
    How: 364 days / internal level 72 (last 4 logs seeded). The anniversary
    triggers when the level becomes 73 because 73 x 5 = 365.
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS] * 4,
        end_day=ctx.today - timedelta(days=1),
        days_logged=364,
        level=72,
        start_date=ctx.today - timedelta(days=364),
    )


async def _slots_repaired(ctx: Context) -> None:
    """An ongoing window containing a day saved by a normal rescue.

    What you see: ``/streak view`` -> the repair day renders as the rescue
    (🛟) icon among the five slots; the rescues field shows one used.
    How: a 4-day streak ending yesterday where the third day is a repaired
    zero (``repair=True``, ``rescue_type="normal"``).
    """
    goal = _add_goal(ctx)
    end_day = ctx.today - timedelta(days=1)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS, SUCCESS, SUCCESS, SUCCESS],
        end_day=end_day,
        rescues_available=1,
        rescues_used=1,
        repair_offset=2,
        rescue_type=RESCUE_NORMAL,
    )


async def _slots_golden_repaired(ctx: Context) -> None:
    """An ongoing window containing a day saved by a golden rescue.

    What you see: ``/streak view`` -> the golden repair icon among the slots
    and one golden coupon used.
    How: like :func:`_slots_repaired` but ``rescue_type="golden"``.
    """
    goal = _add_goal(ctx)
    end_day = ctx.today - timedelta(days=1)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS, SUCCESS, SUCCESS, SUCCESS],
        end_day=end_day,
        golden_available=1,
        golden_used=1,
        repair_offset=2,
        rescue_type=RESCUE_GOLDEN,
    )


async def _rescue_normal(ctx: Context) -> None:
    """A streak that broke today, rescuable with a normal coupon.

    What you see: ``/streak view`` -> a "Repair streak" button, "Closed"
    field, one normal rescue available. ``/streak log 30`` -> also offers the
    repair button on the fresh log.
    How: last log two days ago, closed at yesterday's date, ``end_date`` one
    day ago, so the close happened today at 00:00 (inside the 24h window).
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS] * 3,
        end_day=ctx.today - timedelta(days=2),
        status=CLOSED,
        end_date=ctx.today - timedelta(days=1),
        rescues_available=1,
    )


async def _rescue_golden(ctx: Context) -> None:
    """A streak that broke more than 24h ago but less than five days ago.

    What you see: ``/streak view`` -> a "Repair streak" button (identical
    label) backed by a **golden** coupon; the rescues field shows it under the
    golden line. ``/streak log 30`` -> offers the same repair.
    How: last log three days ago, ``end_date`` two days ago, so the close was
    yesterday at 00:00 (past the 24h normal window, inside the 5-day golden
    one); one golden coupon available.
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS] * 3,
        end_day=ctx.today - timedelta(days=3),
        status=CLOSED,
        end_date=ctx.today - timedelta(days=2),
        golden_available=1,
    )


async def _rescue_no_coupon(ctx: Context) -> None:
    """A streak that broke today with no coupon left to save it.

    What you see: ``/streak view`` -> "Closed" but **no** repair button (the
    window is open, the inventory is empty).
    How: same timing as :func:`_rescue_normal` but zero normal and golden
    coupons.
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS] * 3,
        end_day=ctx.today - timedelta(days=2),
        status=CLOSED,
        end_date=ctx.today - timedelta(days=1),
    )


async def _rescue_expired(ctx: Context) -> None:
    """A streak that broke more than five days ago: beyond saving.

    What you see: ``/streak view`` -> "Closed", no repair button, even though
    a coupon is nominally available.
    How: last log seven days ago, ``end_date`` six days ago, so the close was
    five days ago (outside the golden window); one normal coupon still stored.
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS] * 3,
        end_day=ctx.today - timedelta(days=7),
        status=CLOSED,
        end_date=ctx.today - timedelta(days=6),
        rescues_available=1,
    )


async def _reminder_custom(ctx: Context) -> None:
    """A goal with a custom daily reminder hour.

    What you see: ``/streak view`` -> Reminders field reads
    "Reminder at 9:00 AM".
    How: goal with ``reminder="custom"`` and ``custom_hour=9``. Combine with an
    ongoing streak builder for a full picture; here no streak is seeded.
    """
    _add_goal(ctx, reminder="custom", custom_hour=9)


async def _reminder_both(ctx: Context) -> None:
    """A goal with both a custom reminder and the 11PM last-chance one.

    What you see: ``/streak view`` -> Reminders field reads
    "9:00 AM, plus last chance (11PM)".
    How: goal with ``reminder="both"`` and ``custom_hour=9``.
    """
    _add_goal(ctx, reminder="both", custom_hour=9)


async def _coupon_shown(ctx: Context) -> None:
    """An ongoing streak that already earned XP, so the coupon fields render.

    What you see: ``/streak view`` -> "Last coupon" shows **250 XP** with a
    date, and "Last XP" shows 250 XP.
    How: a 3-day streak ending yesterday with ``last_xp_at`` set to now and
    ``last_xp_amount``/``total_xp_awarded`` at 250.
    """
    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=[SUCCESS] * 3,
        end_day=ctx.today - timedelta(days=1),
        last_xp_amount=250,
        total_xp_awarded=250,
        last_xp_at=streak_service.utcnow(),
    )


async def _random_state(ctx: Context) -> str:
    """A random streak of ``--days`` logged days, fully derived.

    What you see: ``/streak view`` -> ``--days`` random days ending on
    ``--last-log`` (yesterday by default), with the level, coupons, last XP and
    next-XP projection seeded exactly as the normal logging flow would have
    produced them. When ``--last-log`` is older than yesterday the streak is
    seeded closed, so the repair flow is offered while the rescue window lasts.
    How: each day is randomly successful (60%), failed (30%) or zero (10%). The
    completed windows pay their successes' share of the level XP (doubled on a
    365-day anniversary), one normal rescue is granted per completed level, and
    a golden one every 50 levels or 365 days. Use ``--seed`` to repeat a run.
    """
    days = ctx.days or 0
    rng = ctx.rng or random.Random()
    last_log = ctx.last_log or (ctx.today - timedelta(days=1))
    is_closed = last_log < ctx.today - timedelta(days=1)
    closed_end = last_log + timedelta(days=1)
    outcomes = rng.choices(
        [SUCCESS, FAILED, ZERO], weights=[60, 30, 10], k=days
    )

    level = days // LEVEL_THRESHOLD
    golden = sum(
        1
        for completed in range(1, level + 1)
        if completed % 50 == 0
        or (completed * LEVEL_THRESHOLD) % 365 == 0
    )
    total_xp = 0
    last_award = 0
    for completed in range(1, level + 1):
        window = outcomes[
            (completed - 1) * LEVEL_THRESHOLD : completed * LEVEL_THRESHOLD
        ]
        successes = sum(1 for outcome in window if outcome == SUCCESS)
        award = streak_service.window_xp(completed, successes)
        if (completed * LEVEL_THRESHOLD) % 365 == 0:
            award *= 2
        total_xp += award
        last_award = award

    remainder = days % LEVEL_THRESHOLD
    current = outcomes[days - remainder :] if remainder else []
    successes_so_far = sum(1 for outcome in current if outcome == SUCCESS)
    projected = streak_service.on_track_xp(level, days, successes_so_far)

    goal = _add_goal(ctx)
    await _add_streak(
        ctx,
        goal,
        outcomes=outcomes,
        end_day=last_log,
        days_logged=days,
        level=level,
        status=CLOSED if is_closed else ONGOING,
        end_date=closed_end if is_closed else None,
        rescues_available=level,
        golden_available=golden,
        last_xp_amount=last_award,
        total_xp_awarded=total_xp,
        last_xp_at=streak_service.utcnow() if level > 0 else None,
        window_xp_paid=last_award if level > 0 else 0,
    )

    successes = sum(1 for outcome in outcomes if outcome == SUCCESS)
    lines = [
        f"Generated {days} logged days (last on {last_log.isoformat()}; "
        f"{successes} successful, {days - successes} not).",
        f"Level: {streak_service.display_level(days)} "
        f"(completed levels: {level}).",
        f"Rescues: {level} normal, {golden} golden available.",
        f"XP earned so far: {total_xp} (last coupon: {last_award}).",
        f"Projected next level: {projected} XP.",
    ]
    if remainder:
        lines.append(
            f"Current window: {successes_so_far} of {remainder} days successful."
        )
    if is_closed:
        rescue_kind = streak_service.rescue_kind_for_closed(
            closed_end, ctx.timezone, streak_service.utcnow()
        )
        lines.append(
            f"Streak seeded closed (end_date {closed_end.isoformat()}); "
            + (
                f"{rescue_kind} rescue available."
                if rescue_kind
                else "past the rescue window."
            )
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ registry


def _registry() -> dict[str, State]:
    def state(
        build,
        *,
        name: str | None = None,
        build_goal: bool = True,
        log_amount: str | None = "30",
        expect: str = "",
    ) -> State:
        return State(
            name=name or build.__name__.lstrip("_").replace("_", "-"),
            comment=build.__doc__ or "",
            build=build,
            build_goal=build_goal,
            log_amount=log_amount,
            expect=expect,
        )

    states = [
        state(_unregistered, build_goal=False, log_amount=None),
        state(_no_streak, log_amount="30"),
        state(_ongoing_day1, log_amount="15"),
        state(_ongoing_mixed, log_amount="30"),
        state(_window_complete, log_amount="30"),
        state(_xp_five_success, log_amount="30"),
        state(_xp_five_failure, log_amount="5"),
        state(_xp_partial, log_amount="30"),
        state(_xp_top_up, log_amount="30"),
        state(_xp_cap, log_amount="30"),
        state(_level_golden_50, log_amount="30"),
        state(_anniversary_365, log_amount="30"),
        state(_slots_repaired, log_amount="30"),
        state(_slots_golden_repaired, log_amount="30"),
        state(_rescue_normal, log_amount="30"),
        state(_rescue_golden, log_amount="30"),
        state(_rescue_no_coupon, log_amount="30"),
        state(_rescue_expired, log_amount="30"),
        state(_reminder_custom, log_amount="30"),
        state(_reminder_both, log_amount="30"),
        state(_coupon_shown, log_amount="30"),
        state(_random_state, name="random", log_amount="30"),
    ]
    return {entry.name: entry for entry in states}


# ------------------------------------------------------------------- helpers


async def _wipe(session, *, guild_id: int, user_id: int) -> None:
    """Delete the member's streak logs, streaks and goal (streak tables only)."""
    streak_ids = list(
        (
            await session.execute(
                select(Streak.id).where(
                    Streak.guild_id == guild_id, Streak.user_id == user_id
                )
            )
        ).scalars()
    )
    if streak_ids:
        await session.execute(
            delete(StreakLog).where(StreakLog.streak_id.in_(streak_ids))
        )
    await session.execute(
        delete(Streak).where(
            Streak.guild_id == guild_id, Streak.user_id == user_id
        )
    )
    await session.execute(
        delete(StreakGoal).where(
            StreakGoal.guild_id == guild_id, StreakGoal.user_id == user_id
        )
    )


async def _existing_timezone(session, *, guild_id: int, user_id: int) -> str | None:
    goal = (
        await session.execute(
            select(StreakGoal).where(
                StreakGoal.guild_id == guild_id, StreakGoal.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return goal.timezone if goal else None


async def _guild_dry_run(session, *, guild_id: int) -> bool:
    row = (
        await session.execute(
            select(StreakSetting).where(StreakSetting.guild_id == guild_id)
        )
    ).scalar_one_or_none()
    return bool(row.dry_run) if row else False


def _print_list(registry: dict[str, State]) -> None:
    print("Available states:\n")
    for name, entry in registry.items():
        summary = " ".join((entry.comment or "").strip().split())
        print(f"  {name}")
        print(f"      {summary}\n")


async def run(args: argparse.Namespace) -> None:
    registry = _registry()

    if args.list:
        _print_list(registry)
        return

    if args.state not in registry:
        print(f"Unknown state {args.state!r}.\n", file=sys.stderr)
        _print_list(registry)
        raise SystemExit(2)

    entry = registry[args.state]
    config = load_config(Path(args.config))
    guild_id = int(args.guild_id if args.guild_id else config.guild_id)

    init_db(config.mysql_url)
    await create_tables()

    now = streak_service.utcnow()
    async with session_scope() as session:
        configured_tz = args.timezone or await _existing_timezone(
            session, guild_id=guild_id, user_id=args.user_id
        ) or str(config.get("timezone") or "UTC")
        timezone = streak_service.valid_timezone(configured_tz) or "UTC"
        today = streak_service.local_date(now, timezone)
        last_log = args.last_log or (today - timedelta(days=1))
        dry_run = await _guild_dry_run(session, guild_id=guild_id)

        await _wipe(session, guild_id=guild_id, user_id=args.user_id)
        generated = None
        if entry.build_goal:
            ctx = Context(
                session=session,
                guild_id=guild_id,
                user_id=args.user_id,
                today=today,
                timezone=timezone,
                amount=args.amount,
                unit=args.unit,
                days=args.days,
                rng=random.Random(args.seed),
                last_log=last_log,
            )
            generated = await entry.build(ctx)
        await session.commit()

    await close_db()

    print(f"State:    {entry.name}")
    print(f"User:     {args.user_id}")
    print(f"Guild:    {guild_id}")
    print(f"Timezone: {timezone} (today is {today.isoformat()})")
    print()
    if entry.build_goal:
        print(f"{'View:':<10}/streak view")
        if entry.log_amount is not None:
            print(f"{'Log:':<10}/streak log {entry.log_amount}")
        else:
            print("Log:      (not applicable)")
    print()
    print("Expected:")
    print(f"  {' '.join((entry.comment or '').strip().split())}")
    if generated:
        print()
        print("Generated:")
        for line in str(generated).splitlines():
            print(f"  {line}")
    if dry_run:
        print()
        print(
            "WARNING: streak dry-run is enabled for this guild; levels progress "
            "but no XP reaches the member. Run /streak admin dryrun off."
        )
    local_hour = streak_service.local_hour(now, timezone)
    if local_hour >= 22:
        print()
        print(
            "WARNING: it is close to local midnight; the seeded 'today' may roll "
            "over before you run the command. Re-run the script if it does."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed a member's streak into a known state for manual testing."
    )
    parser.add_argument("user_id", nargs="?", type=int, help="Discord user id")
    parser.add_argument("state", nargs="?", help="State to seed (see --list)")
    parser.add_argument("--list", action="store_true", help="List all states and exit")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--guild-id", type=int, default=None)
    parser.add_argument("--timezone", default=None,
                        help="Override the goal timezone (default: existing/config)")
    parser.add_argument("--amount", type=int, default=30)
    parser.add_argument("--unit", default="pages")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of logged days for the 'random' state",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed the random outcomes for a repeatable run",
    )
    parser.add_argument(
        "--last-log",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Date of the last daily log for the 'random' state (default: yesterday)",
    )
    args = parser.parse_args()

    if not args.list and (args.user_id is None or args.state is None):
        parser.error("user_id and state are required (or use --list)")
    if args.state == "random":
        if args.days is None:
            parser.error("--days is required for the 'random' state")
        if args.days < 1:
            parser.error("--days must be at least 1")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
