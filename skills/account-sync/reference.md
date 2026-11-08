# account-sync reference

## Where Claude Code Desktop keeps things

On macOS, under `~/Library/Application Support/Claude/`:

| Path | What it is | account-sync |
| --- | --- | --- |
| `claude-code-sessions/<account>/<org>/local_<id>.json` | one record per session: title, folder, model, dates, archived, parent session | synced |
| `…/scheduled-tasks.json` | the account's routines (scheduled tasks) | synced |
| `…/archived-sessions.idx` | index of archived sessions | kept in line with the records |
| `…/backlog/tasks.json` | the backlog | missing items are added |
| `…/deleted_<id>` | marker the app leaves when a session is deleted | copied, see the rules |
| `claude_desktop_config.json` → `preferences.epitaxyPrefs` | the sidebar of each account: groups, sections, pinned sessions | read only; changed with the app's sidebar tools |
| `config.json` → `lastKnownAccountUuid` | the account signed in | read only |
| `~/.claude/projects/` | the conversations themselves | already shared by every account |

Each account folder is `<account id>/<organization id>`. One account can have several organization folders, and
some of them are empty apart from `scheduled-tasks.json`.

The conversations are already shared, so a session works in another account as soon as its record is there. The
records, plus routines, backlog and archive, are all that account-sync copies.

## Rules

- **Newest wins.** For a session in several accounts, the copy with the latest `lastActivityAt` (then the latest
  file date) goes to the others. Error notices (`error`, `errorAt`, `errorRows`, `priorErrorMark`) stay in their
  own account.
- **Nothing comes back.** A session, routine or backlog item that an account had at the last sync and no longer has
  was deleted there, so it is not copied back. A session with a deletion marker in any account is not copied
  anywhere.
- **Nothing is deleted.** Deleting a session in one account doesn't delete it in the others: delete it in each.
  Deletion markers are copied only to the accounts where that session doesn't exist.
- **Routines.** The definition comes from the account whose routines file changed last (the one in use). The run
  dates (`lastRunAt`, `lastScheduledFor`, `missedRunScanFloor`) take the latest of all the accounts, so a routine
  doesn't run twice after a switch. Routines whose SKILL.md is gone are not copied.
- **Sidebar.** The plan places sessions that are in a group in another account and in no group here; a session
  already in a different group here is left alone and counted as a conflict. Archived sessions can't be moved and
  nested sessions go with their parent. Moving a pinned session into a group unpins it, so the plan pins it again.
  Moving can change the grouping, so the plan sets it again at the end. The pinned list is
  `starred-local-code-sessions`; `pinnedOrder` is only ordering history.

## Safety

- Writes only while Claude is closed. On macOS `apply` checks it and `arm` waits for it.
- Dry run first. `apply` refuses to write anything if a file doesn't look as expected (`unexpected format`).
- Files are written atomically (a temporary file, then a rename), with permissions 600 and never as links: the app
  refuses symbolic links in its data folder.
- Each run keeps a backup of every file it changes and a manifest of what it created, in
  `~/.claude-account-sync/runs/<date>/`. `undo` restores the backups and moves what the run created to
  `moved-aside/` in that folder, so even an undo deletes nothing.
- One run at a time: a lock file holds the process id, and a lock whose process is gone is cleared.
- After writing, every touched account is checked: valid JSON, plain files, permissions 600 on new files.
- The app version is compared with the tested ones; an untested version gets a note, and the format check still
  guards every write.

## Its own files

In `~/.claude-account-sync/` (change it with `--data-dir`):

| File | What it is |
| --- | --- |
| `accounts.json` | the selected accounts and their names |
| `state.json` | what each account had at the last sync, so deletions are respected |
| `runs/<date>/` | backup, manifest, previous state and result of each run |
| `sync.log` | output of armed runs |
| `sidebar-plan.json` | the last sidebar plan |
| `local.claude-account-sync.plist` | the launchd job, while armed |

## Exit codes

`0` done, `1` a check failed or there is nothing to undo, `2` usage or setup error, `3` Claude is running,
`4` unexpected format (nothing written).

## Troubleshooting

- **Armed, but nothing happens.** Claude or one of its helper processes is still running. Quit with Cmd+Q, not by
  closing the window. The armed job gives up after two hours; `disarm` cancels it.
- **An account is missing from `accounts`.** Sign in with it once in the app so that the app creates its folder.
  Empty folders of accounts that have sessions in another folder are hidden; `accounts --all` shows them.
- **The other account's sidebar looks different.** Run `sidebar` while signed in to it.
- **"has not been tested with account-sync".** Claude was updated. The format check still protects every write; if
  `plan` reports an unexpected format, wait for an update of account-sync.
