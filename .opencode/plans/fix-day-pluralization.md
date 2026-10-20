# Plan: Fix day/days singular/pluralization

## Problem
When a count is 1, texts show "1 days" which is grammatically incorrect. Need to show "day" (singular) when count can be 1. Some cases like level-ups every 5 days should remain plural (never 1 in those contexts). User suggested pattern: add `day%{s}` in YAML and in code do `s="" if n==1 else "s"`.

## Analysis

### Settings (can be 1)
- `settings.list.unit_days` in `bot/texts/settings.yml:24` - currently `"days"`. Used in `/settings list` for purge days, streak log retention, review repost cooldown. All can be set to 1.
- Display logic in `bot/commands/settings.py`:
  - `_current_value()` line ~154: `return str(_current_days(interaction, key)), t("settings.list.unit_days")` - needs to pass count for pluralization
  - `current_value_line()` uses unit from above
- Validation message `settings.edit.expected.days` says "days" generically in ranges - instructional text, less awkward; not displaying a specific "1 days" value. Probably fine to leave or can update, but main issue is actual value display.

### Streaks (some can be 1)
From `bot/texts/streaks.yml`:
- Line 100: level-up message "logged your progress every day for **%{days} days!**" - days_logged is multiple of LEVEL_THRESHOLD (5), so never 1. Keep plural.
- Line 116: rescue normal "saved **%{days} days**" - normal rescue patches up to 24h window; but how many days? Could be 1 day rescued. Also have separate `content_one` (line 117) for exactly 1 day. Current code might use different keys - need to check. But safer to handle generally.
- Line 117: `content_one` already says "saved **1 day**" - singular case already handled separately. Good.
- Line 138: `started_value: "%{date}\n-# %{days} days"` - shows total days logged. Can be 1 (just started). **Needs fixing**.
- Line 177: `days_details: "> **%{days}** days (level %{level})   Started: %{date}"` in leaderboard - total days logged, can be 1. **Needs fixing**.
- Line 233: anniversary "kept a reading streak alive for a full year! **%{days} days total**" - 365 days minimum, multiples of 365? Or just total days reaching 365. Always >=365, plural. Keep plural.

## Implementation

### 1. Update settings texts
**File:** `bot/texts/settings.yml`
- Change `unit_days` from `"days"` to `"day%{s}"`

**File:** `bot/commands/settings.py`
- In `_current_value()` for days kind (line ~150-155): get the days count, compute `s = "" if days == 1 else "s"`, pass `s=s` to `t("settings.list.unit_days", s=s)`. Need to restructure to compute days first.
```python
if kind == "days":
    days = _current_days(interaction, key)
    return str(days), t("settings.list.unit_days", s="" if days == 1 else "s")
```

### 2. Update streak texts
**File:** `bot/texts/streaks.yml`
- Change `info.started_value` from `"%{date}\n-# %{days} days"` to `"%{date}\n-# %{days} day%{s}"`
- Change `leaderboard.days_details` from `"> **%{days}** days (level %{level})   Started: %{date}"` to `"> **%{days}** day%{s} (level %{level})   Started: %{date}"`

### 3. Update streak command code
**File:** `bot/commands/streaks.py`
- In `_view_embed()` where `started_value` is used (lines ~523, ~574): pass `s="" if streak.days_logged == 1 else "s"`. Also need to get the days count.
- In `_leaderboard_embed()` where `days_details` is used (line ~735): pass `s="" if streak.days_logged == 1 else "s"`

Also check if there might be other places. The rescue already handles singular separately with `content_one` - leave as is.

## Notes
- Follow the suggested pattern from user exactly
- Only change places where count can actually be 1
- Keep existing behavior for cases where count is never 1
