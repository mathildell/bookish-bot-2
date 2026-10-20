# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project

`bh2` is the "Bookish Hideaway" Discord bot. We're working on migrating features from the previous project, written in Ruby on Rails. The list of old features are available in `docs/Old project features.md`. 

Implemented features:
- **Booster reacts** — auto-react to messages that mention a registered booster's
  name/nickname.
- **Booster roles** — let boosters rename/recolour a custom role an admin linked
  to them.
- **Levels** — members earn XP from messages; each level grants the configured
  reward role. `/xp leaderboard` switches between all-time and the current
  Monday-to-Sunday week. A top-10 weekly leaderboard is posted on Sundays at
  the configurable hour, and the weekly totals roll over for the next week.
- **Book reviews** — poll members' Goodreads RSS feeds every 5 minutes and post
  new reviews to the configured channel (each entry at most once per 24h).
- **Book releases** — track upcoming Goodreads/NetGalley books (scraped with a
  headless browser) and announce them: an overview on the 1st of each month and
  the day's releases every day. Releases are pruned from the database after a
  configurable retention window (default 30 days). Each release carries a
  guild-unique `reference` handle (title's first two words, then three, then a
  random suffix); commands accept either the numeric id or the handle.
- **Introductions** — admins configure an intro channel and its questions; a
  sticky message's button lets members post (and later edit) a single
  introduction.
- **Pinned forum reference** — when a new thread/post is created in the
  configured forum channel, repost a fixed reference message into it and pin it.
- **Bot actions** — `/bot say` lets admins post a message as the bot into any
  channel or thread, optionally as a reply; `/bot react` adds an emoji reaction
  to a message.
- **Streaks** — members register a daily reading goal (amount, unit, timezone,
  reminder schedule) with `/streak register` (first time only) and change it
  with `/streak edit`. They log each day; missing a day closes the streak; a
  normal rescue repairs it within 24h, a golden one within 5 days. Every 5
  logged days completes a level (XP into the normal level system, plus a
  rescue and a generated coupon image; every 50 levels a golden rescue; every
  365 days a celebration). An
  hourly task sends reminders (picked at random from a list of phrasings) and
  closes due streaks, notifying the admins' updates channel
  (`bot_updates_channel_id`) about each closure; daily logs are purged after a
  configurable retention window (default 90 days) while streak totals persist.
  `/streak admin dryrun` toggles XP gain off while still showing what would
  have been earned.
- **Settings** — `/settings list|edit` let admins view and set the bot's runtime
  settings: the daily cleanup retention windows (old releases, streak logs), the
  review repost cooldown, the UTC hours release announcements and the weekly XP
  leaderboard are posted, the admins' updates channel, the transparent embed
  background, the default embed colour, the coupon fallback image and the buddy
  read forum channel + reference message link. This is the home for values that
  used to live in `config.yaml`.
- **Reminders** — members set one-off or recurring reminders (`/reminder
  add|edit|remove|list`) that fire on the hour in a chosen timezone (defaulting
  to the streak timezone, then UTC) and are posted in the channel they were
  created in, pinging the owner. A reminder is a message, a repeat kind and one
  schedule phrase parsed against a closed grammar (`at 2pm`,
  `on monday at 2pm`, `every 2 days at 2pm`, `on the last day of the month at
  2pm`, …); the schedule option autocompletes the accepted phrasings. Monthly
  reminders take a fixed day (clamped in short months) or days before the
  month's end. Each post carries Mark completed and Snooze 1 hour buttons (2h
  lifetime); snoozes repost an hour later via the database. Reminders missed
  while offline post if at most 24h late.
- **Ping** — `/ping` replies with a friendly note and the bot's latency.

The list of commands from the bot lives in the README.md.

Stack: Python 3.14, [discord.py](https://discordpy.readthedocs.io/) 2.x,
SQLAlchemy 2 (async) + aiomysql, MySQL, PyYAML.

## Setup

Dependencies live in the repo-local virtualenv (`.venv`):

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium  # browser used by the book release scraper
```

On Linux the browser also needs system libraries; `update.sh` runs
`playwright install-deps chromium` (via sudo) for that. Without them Chromium
fails with "error while loading shared libraries".

Configuration is a single `config.yaml` (gitignored). Copy the example and fill
it in:

```bash
cp config.example.yaml config.yaml
```

MySQL for local development:

```bash
docker compose up -d mysql
```

## Running

```bash
.venv/bin/python run.py
```

The entrypoint is `run.py` → `bot.main.run()`. Slash commands are synced to the
configured guild on startup.

## Testing

There is no test runner dependency; tests use the standard library:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

There is no linter/formatter configured. At minimum, keep the tree importing and
compiling:

```bash
.venv/bin/python -m compileall -q bot tests run.py
```

## Layout

```
run.py                     entrypoint
bot/
  main.py                  bot wiring (events, intents, startup)
  config.py                config.yaml loader
  db.py                    async engine/session helpers
    models.py                SQLAlchemy models (BoosterReact, BoosterRole, UserXP,
                           WeeklyXP, WeeklySetting, LevelReward, ReviewFeed,
                           ReviewEntry, ReviewSetting, IntroQuestion,
                           IntroSetting, IntroPost, BookRelease, ReleaseSetting,
                           ReleaseEditDraft, StreakGoal, Streak, StreakLog,
                           StreakSetting, BotSetting, Reminder, ReminderSnooze)
  notifications.py         level-up + streak-closed announcements
  commands/
    booster.py             /booster root group
    booster_react.py       /booster react ...
    booster_role.py       /booster role ...
    level.py               /xp root group + level/leaderboard/weekly/add/remove/freeze/unfreeze
    level_rewards.py       /xp rewards ...
    reviews.py             /reviews root group + feed add/remove/list, channel, repost
    releases.py            /releases group + add/update/search/list/view/remove, channel, role, force
    intro.py               /intro group + sticky button/modal + question manager
    streaks.py             /streak group + register buttons/hour select, log/today/view/leaderboard, admin log
    reminders.py           /reminder group + add/edit/remove/list + complete/snooze buttons
    bot.py                 /bot root group (say/react)
    bot_say.py             /bot say (post a message as the bot)
    bot_react.py           /bot react (add an emoji reaction to a message)
    settings.py            /settings group + list/edit (typed bot settings)
    ping.py                /ping (is the bot awake?)
    checks.py              shared permission checks
    listing.py             shared pagination + list-embed helpers
    views.py               shared timed-view base that disables buttons on timeout
  services/
    booster_react.py       react matching + CRUD
    booster_role.py        role links + reconcile poll
    coupons.py             streak coupon rendering + image hosting
    level.py               XP curve + per-user XP storage + weekly tracking
    level_rewards.py       level -> reward role links + role sync
    members.py             guild member resolution
    reference.py           pinned reference message for new forum threads
    reviews.py             Goodreads RSS fetch/parse + review poll
    html.py                shared HTML -> Discord markdown converter
    release_sources.py     Playwright scraper + Goodreads/NetGalley parsers
    releases.py            release CRUD + embeds + daily/monthly announcements
    intro.py               intro channel/questions/answers + embed building
    streaks.py             streak rules (XP, slots, rescues) + log/close/revive/purge/leaderboards
    reminders.py           reminder schedules (next run, staleness) + CRUD/snooze delivery
    timezones.py           IANA timezone search/validation shared by streaks and reminders
    settings.py            bot-wide settings (updates channel, embed image/colour)
  texts/                   user-facing strings (YAML, one file per component)
tests/                     unittest suite
scripts/                   one-off utilities (imports + schema migrations)
docs/                      may be outdated — do not treat as source of truth
bookishhideaway/           archived old Ruby project — out of scope, ignore
```

## Conventions

- **Do not overwrite changes humans made in-between prompts.** Be careful to always be working with the latest file version before writing changes. 
- **All user-facing text lives in `bot/texts/`.** Look it up with
  `t("dotted.key", **variables)`. Nothing that a Discord user can read (command
  descriptions, parameters, responses, embed labels, button labels, audit-log
  reasons) may be hardcoded in Python.
- **Group texts by component, not by where they are used.** One YAML file per
  component; e.g. pagination text belongs under `components.pagination`, booster
  text under `booster.*`. Shared strings (used by more than one command) get a
  shared key rather than a duplicate.
- **No dead text.** A key must be referenced from code; remove keys when their
  feature is removed. `tests` and PR review should catch drift.
- **Don't repeat code.** If a helper is needed in several places, define it once
  (e.g. `commands/listing.py` for list/pagination/embed helpers,
  `commands/checks.py` for permissions) and reference it.
- **Match the existing style.** `from __future__ import annotations`, type hints
  on public functions, small focused helpers.
- **Timed views subclass `ExpiringView`** (`commands/views.py`) so their buttons
  are disabled when the bot stops listening to them.
- **Do not add comments unless they explain non-obvious intent.** Keep comments
  accurate — delete comments that no longer match the code.
- **CRUD vocabulary for commands:** `add`, `remove`, `list`, `view`, `edit`. If the user uses the term `delete` instead of `remove`, for example, remind them of the established naming convention.
- No emojis in source files (leave the ones in README.md).

## Database

Tables are created automatically on startup (`create_tables`), which also
additively adds any columns the models declare but existing tables lack, so a
deploy that introduces a column no longer breaks the running schema. Destructive
changes (drops, renames, type changes, data backfills) still require a migration
script in `scripts/`.

## Deployment

Production runs as a systemd service (`bookish-hideaway`). `update.sh` pulls,
installs deps, and restarts the service.
