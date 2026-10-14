# README
## List of commands (boosters)
-# \[\] = optional arguments
## Booster reacts
```
/booster react list
```
**Permissions:** 🔒 Admin
Paginated list of every registered booster react.

```
/booster react view [user:]
```
**Permissions:** Everyone
Show a booster's react emoji and matching identifiers.

```
/booster react add emoji: [user:] [nicknames:]
```
**Permissions:** Admin (anyone) / Booster (self only)
Paginated list of every registered booster react. 

```
/booster react edit [user:] [emoji:] [nicknames:]
```
**Permissions:** Admin (anyone) / Booster (self only)
Change an existing react's emoji and/or nicknames.

```
/booster react remove [user:]
```
**Permissions:** Admin (anyone) / Booster (self only)
Remove a booster's react registration.
## Booster roles
```
/booster role list
```
**Permissions:** 🔒 Admin
Paginated list of every linked booster role.

```
/booster role link user: role:
```
**Permissions:** 🔒 Admin
Link an admin-created role to a booster. 

```
/booster role sync
```
**Permissions:** 🔒 Admin
Check for boosters who stopped boosting, unlink them, and notify the updates channel.

```
/booster role edit [user:] [name:] [color:] [gradient:] [emoji:] [image:]
```
**Permissions:** Admin (anyone) / Booster (self only)
Change a linked role's name, primary colour, gradient, or emoji/image icon.

```
/booster role remove [user:]
```
**Permissions:** 🔒 Admin
Delete a booster's role and remove the link.

***

## Levels
```
/xp level [user:]
```
**Permissions:** Everyone
Show XP, level, progress to the next level, reward role, and leaderboard rank.
Defaults to you; pass a user to see someone else's card.

```
/xp leaderboard
```
**Permissions:** Everyone
Paginated leaderboard of every member by total XP.

```
/xp add user: amount:
```
**Permissions:** 🔒 Admin
Add XP to a member.

```
/xp remove user: amount:
```
**Permissions:** 🔒 Admin
Remove XP from a member (never below 0).

```
/xp freeze user:
```
**Permissions:** 🔒 Admin
Stop a member from earning XP from messages (admin grants still apply).

```
/xp unfreeze user:
```
**Permissions:** 🔒 Admin
Allow a frozen member to earn XP from messages again.

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

