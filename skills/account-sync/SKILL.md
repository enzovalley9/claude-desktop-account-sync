---
name: account-sync
description: Keeps Claude Code Desktop sessions in sync across the Claude accounts on this computer, two-way and incremental, so the user can switch accounts without losing sessions, routines, backlog or sidebar groups. Use it whenever the user switches Claude accounts or is about to, wants their Claude Code sessions copied or moved to another account, says sessions are missing in the other account, wants the other account to have the latest, wants the sidebar (groups, pinned sessions) to look like in the other account, or wants to undo or check a sync.
---

# account-sync

One script does all the work and prints a few short lines. Run it and pass its output on. Do not read the app's
files or the script itself: that is what keeps this cheap. Nothing is written until you run `apply`, `arm` or `undo`.

Below, `SYNC` means `python3 "<base directory of this skill>/scripts/account_sync.py"` (on Windows, `python` or `py`
instead of `python3`). The base directory is shown when this skill loads. Write the full command every time: shell
variables do not survive between commands.

## First time

1. `SYNC accounts` lists the account folders, their sessions and which account is signed in.
2. Ask the user which accounts to keep in sync (usually all that have sessions) and a short name for each.
3. `SYNC select work=1a2b3c4d personal=5e6f7a8b`. Eight characters of the id are enough; when it says an id is
   ambiguous, pass `ACCOUNT/ORG` as listed by `SYNC accounts --all`.

## Sync

1. `SYNC plan` is a dry run. Show the user its lines and ask for the go-ahead.
2. On macOS run `SYNC arm`, then ask the user to quit Claude with Cmd+Q. The sync runs as soon as Claude is closed
   and Claude reopens by itself. On Windows and Linux, `plan` ends with the exact command to run: give it to the
   user, who quits Claude completely, runs it in a terminal and opens Claude again.
3. Once Claude is back (this session or a new one), `SYNC status` shows how it went.

Never ask the user to keep Claude open during a sync, and never try `apply` from inside Claude: you run inside
Claude, so on the real data `apply` always refuses while you are running. That is what `arm` and the printed
command are for.

Switching accounts: sync, then sign in to the other account in the app as usual. The sync is two-way, so syncing
again later, from either account, brings each account up to date.

## Sidebar

Groups, pinned sessions and grouping live in the running app and change only through its sidebar tools, for the
account that is signed in now. After a sync that brought sessions in:

1. `SYNC sidebar` prints numbered tool calls. If those tools are deferred, load them with one ToolSearch:
   `select:mcp__ccd_sidebar__list_groups,mcp__ccd_sidebar__create_group,mcp__ccd_sidebar__move_sessions,mcp__ccd_sidebar__set_pinned,mcp__ccd_sidebar__set_view`
2. Make the calls in order. Replace `<id of NAME>` with that group's id from `list_groups` or from `create_group`'s
   result.
3. Pass on the "By hand" line: folding and unfolding sections has no tool.

## Undo

`SYNC undo --show` says what undoing the last sync would do, and how. On macOS `SYNC arm --undo` plus Cmd+Q undoes
it; on Windows and Linux it prints the command to run with Claude closed. Each undo goes back one sync.

## When something goes wrong

- `unexpected format`: the app changed how it stores its data, and nothing was written. Tell the user and suggest
  checking for an account-sync update. Do not work around it.
- `Claude is running`: use `arm`.
- `arm` with no result after a while: `SYNC status`, which also shows the end of the log.
- `SYNC disarm` cancels an armed run. Add `--json` to any command for machine-readable output.

[reference.md](reference.md) explains what is synced and the safety rules. Read it only when the user asks how it
works or something above does not cover the case.
