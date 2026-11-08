# account-sync 🔄

A [Claude Code](https://code.claude.com) skill that keeps your **Claude Code Desktop sessions in sync across the Claude accounts on one Mac**, so you can switch accounts and find everything where you left it.

Claude Desktop keeps each account's Code sessions apart. Sign in with another account and the sidebar is empty, even though the conversations are still on your disk. account-sync copies what is missing between the accounts you choose, both ways, and only what changed since last time. You ask Claude; a small script does the work, so it costs very few tokens.

> **Unofficial.** Not made by or affiliated with Anthropic. It works with the Claude app's local files, which are undocumented and may change with any update. Tested with Claude 4.23118.0 on macOS. It checks their format before writing and keeps a backup of everything it changes.

## ✨ What it does

- 🔁 **Two-way and incremental.** An empty account is filled from scratch; one that already has sessions gets what it is missing and the latest version of the rest.
- 🗂️ **More than sessions.** Routines (scheduled tasks), backlog, archived sessions, and the sidebar: groups, pinned sessions and grouping.
- 🛡️ **Never deletes, never brings back.** A session you deleted in one account doesn't come back there, and isn't deleted in the others.
- ⏸️ **Writes only with Claude closed.** Claude shows you a dry run and arms the sync; you quit Claude, the sync runs and Claude opens again by itself.
- ↩️ **Undo.** Every run keeps a backup and a manifest, and `undo` puts the files back exactly as they were.
- 🪙 **Cheap.** One script with short outputs. Claude never reads the app's files.

## 🧭 How it works

Claude Code keeps the conversations in `~/.claude/projects`, which every account on the computer already shares. What each account has of its own is a small record per session, in its folder of the app's data. That record is what makes a session show up in the sidebar.

account-sync compares those records across your accounts and copies what is missing or older, and does the same with routines, backlog and archive. The sidebar lives in the app's settings and changes only through the app's own sidebar tools, so the script works out the tool calls and Claude makes them.

## 📦 Requirements

- macOS and the Code tab of the Claude desktop app.
- Python 3.9 or later as `python3`. If it's missing, macOS offers to install it (with the Command Line Tools) the first time it runs.
- Each account signed in at least once on this Mac, so the app has created its folder.

## 🚀 Install

```bash
claude plugin marketplace add enzovalley9/claude-desktop-account-sync
claude plugin install account-sync@claude-desktop-account-sync
```

Or as a plain skill:

```bash
git clone https://github.com/enzovalley9/claude-desktop-account-sync
cp -R claude-desktop-account-sync/skills/account-sync ~/.claude/skills/
```

## 💬 Using it

Ask Claude in the Code tab, for example "sync my Claude accounts", "I'm switching to my work account", "my sessions are missing in the other account" or "make the sidebar look like in my other account". The first time, it lists your accounts and asks which ones to keep in sync.

A typical switch:

1. Claude runs a dry run and shows what would change in each account.
2. You say go. Claude arms the sync and asks you to quit Claude with Cmd+Q.
3. The sync runs and Claude opens again by itself.
4. Sign in with the other account: the sessions are there. If you use groups, ask Claude to fix the sidebar.

The script also works on its own, from a terminal:

```bash
python3 skills/account-sync/scripts/account_sync.py plan
```

| Command | What it does |
| --- | --- |
| `accounts` | Lists the accounts on this Mac, their sessions and which one is signed in |
| `select NAME=ID …` | Chooses the accounts to keep in sync (8 characters of each id are enough) |
| `plan` | Dry run: what would change in each account |
| `arm` | Syncs as soon as you quit Claude, then reopens it (`--undo` to undo instead, `--test` for a dry run) |
| `apply` | Syncs now; Claude must be closed |
| `status` | The last sync and the last armed run |
| `sidebar` | The sidebar tool calls that make the signed-in account look like the others |
| `undo` | Undoes the last sync (`--show` to preview it) |
| `disarm` | Cancels an armed run |

`plan` and `apply` take `--only NAME` to write into one account only. Every command takes `--json`. The [reference](skills/account-sync/reference.md) explains what is synced, the rules and the safety measures.

## 🚧 Limits

- Only the desktop app's Claude Code sessions. Chats on claude.ai, Projects, Cowork, cloud sessions and memory belong to each account on Anthropic's side and are not touched.
- Deleting a session doesn't spread to the other accounts: delete it in each.
- Folding and unfolding sidebar sections is by hand, because there is no tool for it.
- `arm` and the check that Claude is closed are macOS only. On other systems `plan` and `apply` may work with `--app-dir`, untested.

## 🔒 Privacy

Everything stays on your computer. The script reads and writes local files only and makes no network requests. Use it only with accounts that are yours.

## 🗑️ Uninstall

```bash
claude plugin uninstall account-sync@claude-desktop-account-sync
```

The synced sessions stay where they are. Backups are kept in `~/.claude-account-sync`; remove that folder when you no longer need them.

## 🧪 Tests

```bash
python3 tests/test_scenarios.py --tmp /tmp
python3 tests/test_scenarios.py --tmp /tmp --launchd --real-copy
```

The first runs the scenarios on made-up data, on any system. The second (macOS) also arms a dry run through launchd and syncs, re-syncs and undoes a copy of your own data. Neither touches the app's folder.

## License

[MIT](LICENSE)
