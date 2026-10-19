# Bookish Hideaway

Slash-command reference. `[arg]` marks an optional argument.

**Permissions:** **Admin** = Manage Server, **Booster** = has the configured
booster role (`booster_role_id`).

## Booster reacts

Tie an emoji to a booster's name/nickname; the bot then reacts with that emoji
whenever the name is mentioned.

```
/booster react add emoji: [user:] [nicknames:]
```
**Permissions:** Admin (anyone) / Booster (self only)
Register a booster's react emoji, with optional extra comma-separated nicknames
to match.

```
/booster react edit [user:] [emoji:] [nicknames:]
```
**Permissions:** Admin (anyone) / Booster (self only)
Change a registration's emoji and/or nicknames.

```
/booster react view [user:]
```
**Permissions:** Everyone
Show a booster's react emoji and matching identifiers.

```
/booster react remove [user:]
```
**Permissions:** Admin (anyone) / Booster (self only)
Remove a booster's react registration.

```
/booster react list
```
**Permissions:** 🔒 Admin
Paginated list of every registered booster react.

## Booster roles

An admin links a Discord role to a booster; the booster may then rename and
recolour it.

```
/booster role link user: role:
```
**Permissions:** 🔒 Admin
Link an admin-created role to a booster.

```
/booster role edit [user:] [name:] [color:] [gradient:] [emoji:] [image:]
```
**Permissions:** Admin (anyone) / Booster (self only)
Change a linked role's name, colour, gradient, or emoji/image icon.

```
/booster role remove user:
```
**Permissions:** 🔒 Admin
Delete a booster's role and remove the link.

```
/booster role sync
```
**Permissions:** 🔒 Admin
Check for boosters who stopped boosting, unlink them, and notify the updates
channel.

```
/booster role list
```
**Permissions:** 🔒 Admin
Paginated list of every linked booster role.

## Levels

Members earn XP from messages; each level grants the configured reward role.
When a member levels up, the bot posts a congratulations message in the channel
where it happened (or where the admin ran `/xp add`). Removing XP never posts a
message.

```
/xp level [user:]
```
**Permissions:** Everyone
Show a member's level progress, rank, and reward role.

```
/xp leaderboard
```
**Permissions:** Everyone
Paginated XP leaderboard.

```
/xp add user: amount:
```
**Permissions:** 🔒 Admin
Add XP to a member.

```
/xp remove user: amount:
```
**Permissions:** 🔒 Admin
Remove XP from a member.

```
/xp freeze user:
```
**Permissions:** 🔒 Admin
Stop a member earning XP from messages.

```
/xp unfreeze user:
```
**Permissions:** 🔒 Admin
Let a frozen member earn XP from messages again.

```
/xp rewards set level: role:
```
**Permissions:** 🔒 Admin
Set (or edit) the reward role granted at a level.

```
/xp rewards remove level:
```
**Permissions:** 🔒 Admin
Remove the reward role configured for a level.

```
/xp rewards list
```
**Permissions:** 🔒 Admin
Show every level and its reward role.

```
/xp rewards resync
```
**Permissions:** 🔒 Admin
Reapply reward roles so they match every member's current level.

## Book reviews

Members link their Goodreads reviews RSS feed; the bot polls the feeds every
5 minutes and posts new reviews to the configured channel. Each review is
posted at most once every 24 hours. Reviews already on the feed when it is
linked are never posted.

```
/reviews feed add url: [user:]
```
**Permissions:** Admin (anyone) / Everyone (self only)
Link a member's Goodreads `list_rss` reviews feed.

```
/reviews feed remove [user:]
```
**Permissions:** Admin (anyone) / Everyone (self only)
Unlink a member's Goodreads reviews feed.

```
/reviews feed list
```
**Permissions:** 🔒 Admin
Paginated list of every registered Goodreads reviews feed.

```
/reviews channel set channel:
```
**Permissions:** 🔒 Admin
Set the channel where review updates are posted.

```
/reviews repost entry: [user:]
```
**Permissions:** 🔒 Admin
Force the bot to repost a review immediately, even if the Goodreads feed entry
has not changed.

## Book releases

Members track upcoming books from Goodreads or NetGalley. The bot scrapes the
book page for its title, authors, release date, cover, blurb and tags. The
first day of each month it posts an overview of everything releasing that
month, and every day it announces the books releasing that day, pinging the
people who added them and the configured role. Set the channel with
`/releases channel set`.

```
/releases add url:
```
**Permissions:** Everyone
Add an upcoming book from its Goodreads or NetGalley URL. The release date must
be in the future, and the book must not already be tracked. Each book gets a
short unique handle (the title's first two words, then three, then a random
suffix) shown as its id.

```
/releases update url: [book:]
```
**Permissions:** Everyone
Re-scrape a tracked book and show a before/after preview with a confirm button.
`book` accepts the numeric id or the handle, and is only needed when the title
changed and the URL no longer matches the stored book.

```
/releases search keyword:
```
**Permissions:** Everyone
Search tracked books by title or author, or pass a member mention/id to list all
the books that member added. Text matching ignores case and punctuation, so
`KJ Charles` also finds `K. J. Charles`. Matches are listed (paginated when
there are many); if nothing matches, the bot says so and explains how to add it.

```
/releases list
```
**Permissions:** Everyone
Paginated list of upcoming releases, with prev/next buttons and a "jump to
month" dropdown covering the next year.

```
/releases view book:
```
**Permissions:** Everyone
Show one tracked book by its numeric id or handle.

```
/releases remove book:
```
**Permissions:** 🔒 Admin
Remove a tracked book by its numeric id or handle.

```
/releases channel set channel:
```
**Permissions:** 🔒 Admin
Set the channel where release announcements are posted.

```
/releases role set role:
```
**Permissions:** 🔒 Admin
Set the role pinged with release announcements.

```
/releases force daily
```
**Permissions:** 🔒 Admin
Post today's release announcement right now, even for books already announced.

```
/releases force monthly
```
**Permissions:** 🔒 Admin
Post this month's release overview right now, even if it was already posted.

## Introductions

Admins configure an intro channel and the questions for the intro form. A
sticky message in the channel holds a button members use to post (or edit)
their introduction. A member can have a single introduction; editing updates
the existing post. The embed is titled with the member's display name, uses
their avatar as a thumbnail, and shows one field per question; each post has a
Refresh button anyone can use to re-render it, and a Delete button its author
can use (after an ephemeral confirmation) to remove it. Configure the channel to
be read-only for members so only the bot can post there.

```
/intro channel set channel:
```
**Permissions:** 🔒 Admin
Set the channel where introductions are posted and post the sticky message.

```
/intro sticky
```
**Permissions:** 🔒 Admin
Delete and repost the sticky introduction message.

```
/intro questions add content: [placeholder:] [active:]
```
**Permissions:** 🔒 Admin
Add a question to the intro form.

```
/intro questions list
```
**Permissions:** 🔒 Admin
List and manage the questions: select one to edit, toggle it active, reorder it,
or remove it. Only the first 5 active questions are shown in the form.

## Streaks

Members register a daily reading goal (amount, unit and timezone), log their
progress each day, and keep a streak alive. Missing a day closes the streak; a
closed streak can be repaired with a rescue (a normal one within 24 hours, then
a golden one for a few more days). Every five logged days completes a level:
it pays XP into the normal level system and grants a rescue. Every fifty levels
also grants a golden rescue, and every 365 days celebrates with a channel post,
double XP for that window and a golden rescue. XP is binary per day — a day
either reached the goal or it did not — and the payout per level caps at 3000
XP. Streak commands belong to the channel set with `/streak channel set`
(reminders are posted there too). Daily logs are purged after 90 days; the
streak's totals and rank are kept.

```
/streak register amount: unit: timezone:
```
**Permissions:** Everyone
Register a daily goal (first time only). `timezone` is searchable — start
typing a city. Pick a reminder schedule from the buttons: ❌ none, 🕑 a custom
hour (1 AM–10 PM your time), 🌙 a last-chance ping an hour before midnight, or
2️⃣ both. If you already have a goal, use `/streak edit` instead.

```
/streak edit
```
**Permissions:** Everyone
Change an existing goal: amount & unit, timezone, or reminders. Each choice
opens its own form; your streak and progress are left untouched.

```
/streak log amount:
```
**Permissions:** Everyone
Log today's reading. `amount` takes an absolute value (`30`) or an adjustment
to today's entry (`+10`, `-5`). Logging again the same day overwrites it. If the
streak had already closed, a fresh one starts and a repair button is offered.

```
/streak today
```
**Permissions:** Everyone
Reports whether you have logged today, with what you logged and when. If you
have not logged and it is past 5 PM your time, it also reminds you when the
streak closes.

```
/streak view [user:]
```
**Permissions:** Everyone
Show a streak: the five-day visual, timezone, reminders, goal, start date,
rescues, closing time, whether today is logged, level, coupons and XP. Your own
streak shows an **Edit this streak** button plus a repair button when it can be
rescued.

```
/streak leaderboard
```
**Permissions:** Everyone
Top ten streaks, with buttons to switch between 🏅 ongoing and 🏆 all time.

```
/streak channel set channel:
```
**Permissions:** 🔒 Admin
Set the channel for streak commands and reminders.

```
/streak admin log user: amount: [date:]
```
**Permissions:** 🔒 Admin
Backfill a daily log (defaults to today; `date` is `YYYY-MM-DD`) and adjust or
reopen the streak accordingly.

```
/streak admin dryrun state:
```
**Permissions:** 🔒 Admin
Turn XP gain from streaks `on` or `off`. While off, streaks, levels and rescues
still progress, but members earn no XP — the log and view show what they would
have earned and note that it did not reach their pool.

## Admin tools

Fun tools for admins.

```
/bot say channel: message: [reply_to:]
/bot react link: emoji:
```
**Permissions:** 🔒 Admin
`say` posts a message as the bot in a channel or thread. Emojis (custom and
default) are supported; `reply_to` takes a message id or jump link to reply to,
pinging its author. Slash options are single-line, so type `\n` to add a line
break.

`react` makes the bot add `emoji` (custom or default) as a reaction to the
message at the given jump `link` (right-click a message → Copy Message Link).

## Settings

View and edit the bot's runtime settings.

```
/settings list
/settings edit key: value:
```
**Permissions:** 🔒 Admin
`list` shows the current values. `edit` parses `value` according to the chosen
`key` (days, an hour, a channel, a URL, a message link or a hex colour); pass
`none` to clear the optional ones.

```
/settings edit key: purge_book_releases value: 30
/settings edit key: purge_streak_logs value: 90
/settings edit key: review_repost_cooldown_days value: 1
/settings edit key: releases_post_hour value: 13
/settings edit key: bot_updates_channel_id value: <#123456789012345678>
/settings edit key: transparent_embed_image value: https://example.com/bg.png
/settings edit key: default_embed_color value: "#97A97C"
/settings edit key: buddy_read_channel_id value: <#123456789012345678>
/settings edit key: buddy_read_message_link value: https://discord.com/channels/1/222/333
/settings edit key: admin_test_channel_id value: <#123456789012345678>
```
- `purge_book_releases` — how many days a released book is kept before it is
  purged (default 30).
- `purge_streak_logs` — how many days a streak's daily log is kept before it is
  purged (default 90; streak totals persist).
- `review_repost_cooldown_days` — how many days must pass before an edited review
  can be reposted (default 1).
- `releases_post_hour` — the UTC hour (1–23) the daily release announcements are
  posted (default 13).
- `bot_updates_channel_id` — channel notified about upkeep events (streak
  closures, unlinked boosters).
- `transparent_embed_image` — publicly reachable image used as a transparent
  embed background.
- `default_embed_color` — default colour applied to every embed the bot sends.
- `buddy_read_channel_id` — forum channel where new threads get the reference
  message reposted and pinned.
- `buddy_read_message_link` — jump link to the message reposted into new forum
  threads (right-click the message → Copy Message Link).
- `admin_test_channel_id` — channel where channel-locked commands (like
  `/streak`) work regardless of their configured channel.

## Utility

```
/ping
```
**Permissions:** Everyone
Check that the bot is awake; replies with the round-trip latency.
