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
/releases find title:
```
**Permissions:** Everyone
Check whether a book is already tracked; if it is, its details are shown,
otherwise the bot explains how to add it.

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
/releases role clear
```
**Permissions:** 🔒 Admin
Stop pinging a role with announcements.

```
/releases role view
```
**Permissions:** Everyone
Show the role pinged with announcements.

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

## Admin tools

Fun tools for admins.

```
/say channel: message: [reply_to:]
```
**Permissions:** 🔒 Admin
Post a message as the bot in a channel or thread. Emojis (custom and default)
are supported; `reply_to` takes a message id or jump link to reply to, pinging
its author. Slash options are single-line, so type `\n` to add a line break.

## Utility

```
/ping
```
**Permissions:** Everyone
Check that the bot is awake; replies with the round-trip latency.
