#!/usr/bin/env python3
"""account-sync: keep Claude Code Desktop sessions in sync across the Claude accounts on one computer.

Unofficial tool, not affiliated with Anthropic. It works on the Claude app's local files, which are
undocumented and may change. It never deletes anything, never uses links, backs up whatever it changes
and only writes while the Claude app is closed.

Commands (nothing is written to the app unless you use apply, arm or undo):
  accounts             list the Claude accounts found on this computer
  select ID [ID ...]   choose the accounts to keep in sync; NAME=ID gives them a name
  plan                 dry run: what a sync would change
  apply                sync now (the Claude app must be closed)
  arm                  macOS: run as soon as you quit Claude, then reopen it (--undo, --test)
  disarm               cancel an armed run
  status               result of the last sync and of the last armed run
  sidebar              tool calls that make the signed-in account's sidebar match the other accounts
  undo                 undo the last sync (the Claude app must be closed); --show to preview

Only the Python standard library is used (3.9 or newer).
"""
import argparse
import datetime as dt
import glob
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter

VERSION = "0.1.0"
IS_MAC = sys.platform == "darwin"
DEFAULT_APP_DIR = os.path.expanduser("~/Library/Application Support/Claude") if IS_MAC else None
DEFAULT_DATA_DIR = os.path.expanduser("~/.claude-account-sync")
APP_PATH = "/Applications/Claude.app"
APP_BUNDLE_ID = "com.anthropic.claudefordesktop"
APP_PROCESS = "Claude"
LABEL = "local.claude-account-sync"
TESTED_APP_VERSIONS = ("4.23118.0",)
WAIT_SECONDS = 2 * 60 * 60
ERROR_KEYS = ("error", "errorAt", "errorRows", "priorErrorMark")
TASK_DATE_KEYS = ("lastRunAt", "lastScheduledFor", "missedRunScanFloor")
SECTION_NAMES = {"pinned": "Pinned", "routines": "Routines", "sessions": "Sessions"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


class Fail(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


# ---------- files ----------

def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_json(path, default=None):
    try:
        return json.loads(read_bytes(path))
    except FileNotFoundError:
        return default


def compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def indented(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2).encode()


def same_format(path, obj):
    """Serialize obj like the file it replaces (the app writes some files indented, some compact)."""
    try:
        return indented(obj) if read_bytes(path)[:2] == b"{\n" else compact(obj)
    except FileNotFoundError:
        return compact(obj)


def to_ms(value):
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return 0.0
    return 0.0


def _tmp_file(dirpath, data, mode, mtime):
    fd, tmp = tempfile.mkstemp(prefix=".account-sync-", dir=dirpath)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def create_file(path, data, mtime=None, mode=0o600):
    """Create path atomically; fails if it already exists (never overwrites)."""
    tmp = _tmp_file(os.path.dirname(path), data, mode, mtime)
    try:
        os.link(tmp, path)
    finally:
        os.unlink(tmp)  # the final file is left with a single link


def replace_file(path, data, mtime=None):
    """Rewrite path atomically, keeping its permissions."""
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    tmp = _tmp_file(os.path.dirname(path), data, mode, mtime)
    os.replace(tmp, path)


def write_json(path, obj):
    """For account-sync's own files (state, results): write to a temp file, then rename."""
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


def restore_copy(src, dst):
    """Put a backup back in place atomically (single link, same permissions and dates)."""
    fd, tmp = tempfile.mkstemp(prefix=".account-sync-undo-", dir=os.path.dirname(dst))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------- the app ----------

def app_running():
    """True or False on macOS; None where we can't tell."""
    if not IS_MAC:
        return None
    # -a: without it pgrep skips its own ancestors, so it can't see Claude when run from inside Claude
    return subprocess.run(["/usr/bin/pgrep", "-a", "-x", APP_PROCESS], capture_output=True).returncode == 0


def app_version():
    try:
        with open(os.path.join(APP_PATH, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except Exception:
        return None


def is_real_app_dir(app_dir):
    return DEFAULT_APP_DIR is not None and os.path.realpath(app_dir) == os.path.realpath(DEFAULT_APP_DIR)


def signed_in_account(app_dir):
    return (read_json(os.path.join(app_dir, "config.json")) or {}).get("lastKnownAccountUuid")


def dirs(a):
    app_dir = a.app_dir or DEFAULT_APP_DIR
    if not app_dir:
        raise Fail("--app-dir is required on this system (there is only a default on macOS)")
    return os.path.abspath(os.path.expanduser(app_dir)), os.path.abspath(os.path.expanduser(a.data_dir))


# ---------- accounts ----------

class Account:
    def __init__(self, name, account, org, path):
        self.name, self.account, self.org, self.dir = name, account, org, path
        self.scope = account + "/" + org
        self.short = account[:8] + "/" + org[:8]


def looks_like_id(s):
    return len(s) == 36 and s.count("-") == 4


def discover(app_dir):
    root = os.path.join(app_dir, "claude-code-sessions")
    if not os.path.isdir(root):
        raise Fail(f"no Claude Code sessions folder in {app_dir} (is that the Claude app's data folder?)")
    found = []
    for acc in sorted(os.listdir(root)):
        pa = os.path.join(root, acc)
        if not looks_like_id(acc) or os.path.islink(pa) or not os.path.isdir(pa):
            continue
        for org in sorted(os.listdir(pa)):
            d = os.path.join(pa, org)
            if not looks_like_id(org) or os.path.islink(d) or not os.path.isdir(d):
                continue
            files = glob.glob(os.path.join(d, "local_*.json"))
            last = max((os.lstat(p).st_mtime for p in files), default=0)
            found.append({"account": acc, "org": org, "dir": d, "sessions": len(files), "last": last})
    return found


def resolve(found, ident):
    ident = ident.strip().lower()
    if "/" in ident:
        acc, org = ident.split("/", 1)
        matches = [f for f in found if f["account"].startswith(acc) and f["org"].startswith(org)]
    else:
        matches = [f for f in found if f["account"].startswith(ident)]
        if len(matches) > 1:
            with_sessions = [f for f in matches if f["sessions"]]
            if len(with_sessions) == 1:
                matches = with_sessions
    if not matches:
        raise Fail(f'no account folder matches "{ident}" (see `accounts --all`)')
    if len(matches) > 1:
        options = ", ".join(f["account"][:8] + "/" + f["org"][:8] for f in matches)
        raise Fail(f'"{ident}" matches several folders: {options}. Use ACCOUNT/ORG')
    return matches[0]


def load_accounts(app_dir, data_dir):
    cfg = read_json(os.path.join(data_dir, "accounts.json"))
    if not cfg or len(cfg.get("accounts", [])) < 2:
        raise Fail("no accounts selected yet: run `accounts`, then `select` with at least two of them")
    out = []
    for x in cfg["accounts"]:
        d = os.path.join(app_dir, "claude-code-sessions", x["account"], x["org"])
        if os.path.islink(d) or not os.path.isdir(d):
            raise Fail(f'the folder of account "{x["name"]}" is missing ({d}). Sign in once with it in the app')
        out.append(Account(x["name"], x["account"], x["org"], d))
    return out


# ---------- one run ----------

class Run:
    """Keeps track of what is written, the backup and the manifest that `undo` uses."""

    def __init__(self, apply, app_dir, data_dir):
        self.apply, self.app_dir, self.data_dir = apply, app_dir, data_dir
        self.manifest = {"version": 1, "when": dt.datetime.now().isoformat(timespec="seconds"), "app_dir": app_dir,
                         "created": [], "modified": [], "dirs_created": []}
        self.backed_up = set()
        self.counts = {}          # account name -> Counter
        self.details = []
        self.notes = Counter()
        self.dir = None
        if apply:
            self.dir = os.path.join(data_dir, "runs", dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            os.makedirs(os.path.join(self.dir, "backup"))
            state = os.path.join(data_dir, "state.json")
            if os.path.exists(state):
                shutil.copy2(state, os.path.join(self.dir, "previous-state.json"))

    def save_manifest(self):
        write_json(os.path.join(self.dir, "manifest.json"), self.manifest)

    def count(self, c, key, text=None):
        self.counts.setdefault(c.name, Counter())[key] += 1
        if text:
            self.details.append(f"  {c.name}: {text}")

    def backup(self, path):
        if path in self.backed_up:
            return
        dst = os.path.join(self.dir, "backup", os.path.relpath(path, self.app_dir))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst)
        self.backed_up.add(path)
        self.manifest["modified"].append({"path": path, "backup": dst})

    def new_file(self, path, data, mtime=None):
        if self.apply:
            create_file(path, data, mtime=mtime)
            self.manifest["created"].append(path)

    def change_file(self, path, data, mtime=None):
        if self.apply:
            self.backup(path)
            replace_file(path, data, mtime=mtime)

    def new_dir(self, path):
        if self.apply and not os.path.isdir(path):
            os.mkdir(path, 0o700)
            self.manifest["dirs_created"].append(path)


# ---------- sessions ----------

def load_sessions(c):
    """Session files of one account: {id: {"rec", "mtime", "path"}}, plus format problems."""
    out, problems = {}, []
    for p in glob.glob(os.path.join(c.dir, "local_*.json")):
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode):
            problems.append(f"{p}: not a regular file")
            continue
        sid = os.path.basename(p)[:-5]
        try:
            rec = json.loads(read_bytes(p))
        except ValueError:
            problems.append(f"{p}: not valid JSON")
            continue
        if not isinstance(rec, dict) or rec.get("sessionId") != sid:
            problems.append(f"{p}: sessionId does not match the file name")
            continue
        if not isinstance(rec.get("lastActivityAt", 0), (int, float, str, type(None))):
            problems.append(f"{p}: unexpected lastActivityAt")
            continue
        out[sid] = {"rec": rec, "mtime": st.st_mtime, "path": p}
    return out, problems


def check_other_files(c):
    problems = []
    for rel, key in (("scheduled-tasks.json", "scheduledTasks"), ("archived-sessions.idx", "archived"),
                     (os.path.join("backlog", "tasks.json"), "items")):
        p = os.path.join(c.dir, rel)
        if not os.path.lexists(p):
            continue
        try:
            obj = json.loads(read_bytes(p))
        except (ValueError, OSError):
            problems.append(f"{p}: not valid JSON")
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get(key, []), list):
            problems.append(f'{p}: no "{key}" list')
        elif key != "archived" and not all(isinstance(t, dict) and "id" in t for t in obj.get(key, [])):
            problems.append(f'{p}: "{key}" entries without an id')
    return problems


def version(entry):
    return (to_ms(entry["rec"].get("lastActivityAt")), entry["mtime"])


def markers_of(c):
    return {os.path.basename(p)[len("deleted_"):]: p for p in glob.glob(os.path.join(c.dir, "deleted_*"))
            if os.path.isfile(p) and not os.path.islink(p)}


def ids_of(sid, rec):
    """Every id a deleted_ marker may use to refer to this session."""
    ids = {sid[len("local_"):], rec.get("cliSessionId")} | set(rec.get("priorCliSessionIds") or [])
    ids.discard(None)
    return ids


def sync_sessions(run, accounts, targets, base, S):
    markers = {c.name: markers_of(c) for c in accounts}
    deleted = set().union(*markers.values())
    final = {c.name: {sid: e["rec"] for sid, e in S[c.name].items()} for c in accounts}
    archived = {c.name: {} for c in accounts}

    for sid in sorted(set().union(*[set(S[c.name]) for c in accounts])):
        holders = [c for c in accounts if sid in S[c.name]]
        best = max(holders, key=lambda c: version(S[c.name][sid]))
        src = S[best.name][sid]
        title = (src["rec"].get("title") or "")[:60]
        for c in targets:
            if c is best:
                continue
            cur = S[c.name].get(sid)
            if cur is None:
                if ids_of(sid, src["rec"]) & deleted:
                    run.notes[f"sessions with a deletion marker, not copied to {c.name}"] += 1
                    continue
                if sid in base.get(c.scope, {}).get("sessions", ()):
                    run.notes[f"sessions you deleted in {c.name}, not copied back"] += 1
                    continue
            elif version(cur) >= version(src):
                continue
            rec = {k: v for k, v in src["rec"].items() if k not in ERROR_KEYS}   # errors belong to the other account
            data = compact(rec)
            path = os.path.join(c.dir, sid + ".json")
            if cur is None:
                run.new_file(path, data, mtime=src["mtime"])
                run.count(c, "new sessions", f"+ session {sid}  {title}")
            else:
                if read_bytes(cur["path"]) == data:
                    continue
                run.change_file(path, data, mtime=src["mtime"])
                run.count(c, "updated sessions", f"~ session {sid}  {title}")
            final[c.name][sid] = rec
            archived[c.name][sid] = bool(rec.get("isArchived"))

    for c in targets:
        if not archived[c.name]:
            continue
        p = os.path.join(c.dir, "archived-sessions.idx")
        idx = read_json(p)
        ids = list((idx or {}).get("archived", []))
        for sid, is_archived in archived[c.name].items():
            if is_archived and sid not in ids:
                ids.append(sid)
            elif not is_archived and sid in ids:
                ids.remove(sid)
        if idx is None:
            if ids:
                run.new_file(p, compact({"v": 1, "archived": ids}))
        elif ids != idx.get("archived", []):
            run.change_file(p, same_format(p, dict(idx, archived=ids)))

    for c in targets:
        alive = set()
        for sid, rec in final[c.name].items():
            alive |= ids_of(sid, rec)
        have, kept_out = dict(markers[c.name]), set()
        for o in accounts:
            if o is c:
                continue
            for uid, p in markers[o.name].items():
                if uid in have or uid in kept_out:
                    continue
                if uid in alive:
                    kept_out.add(uid)
                    run.notes[f"deletion markers for sessions still alive in {c.name}, not copied"] += 1
                    continue
                run.new_file(os.path.join(c.dir, "deleted_" + uid), read_bytes(p), mtime=os.lstat(p).st_mtime)
                have[uid] = p
                run.count(c, "deletion markers", f"+ marker deleted_{uid}")
    return final


# ---------- backlog ----------

def sync_backlog(run, accounts, targets, base):
    B = {c.name: read_json(os.path.join(c.dir, "backlog", "tasks.json")) for c in accounts}
    every = {}
    for c in accounts:
        for item in (B[c.name] or {}).get("items", []):
            if item.get("id") is not None:
                every.setdefault(item["id"], item)
    present = {c.name: {i.get("id") for i in (B[c.name] or {}).get("items", [])} - {None} for c in accounts}
    file_version = next((b.get("version", 1) for b in B.values() if b), 1)
    for c in targets:
        current, have = B[c.name], present[c.name]
        gone = set(base.get(c.scope, {}).get("backlog", ())) - have
        n_gone = sum(1 for i in every if i not in have and i in gone)
        if n_gone:
            run.notes[f"backlog items you deleted in {c.name}, not copied back"] += n_gone
        missing = [item for i, item in every.items() if i not in have and i not in gone]
        if not missing:
            continue
        for item in missing:
            run.count(c, "backlog items", f"+ backlog {item.get('id')}  {(item.get('title') or '')[:50]}")
        p = os.path.join(c.dir, "backlog", "tasks.json")
        if current is None:
            run.new_dir(os.path.dirname(p))
            run.new_file(p, compact({"version": file_version, "items": missing}))
        else:
            run.change_file(p, same_format(p, dict(current, items=current.get("items", []) + missing)))
        present[c.name] = have | {item["id"] for item in missing}
    return present


# ---------- routines (scheduled tasks) ----------

def sync_tasks(run, accounts, targets, base):
    T, mtime = {}, {}
    for c in accounts:
        p = os.path.join(c.dir, "scheduled-tasks.json")
        T[c.name] = read_json(p)
        mtime[c.name] = os.lstat(p).st_mtime if T[c.name] is not None else 0
    by_id = {}
    for c in accounts:
        for t in (T[c.name] or {}).get("scheduledTasks", []):
            by_id.setdefault(t.get("id"), []).append((c, t))
    merged = {}
    for tid, holders in by_id.items():
        # the definition comes from the account whose routines file changed last (the one in use)
        t = dict(max(holders, key=lambda ct: mtime[ct[0].name])[1])
        for key in TASK_DATE_KEYS:   # the latest run wins, so nothing runs twice after switching
            values = [x.get(key) for _, x in holders if x.get(key)]
            if values:
                t[key] = max(values, key=to_ms)
        merged[tid] = t
    present = {c.name: {t.get("id") for t in (T[c.name] or {}).get("scheduledTasks", [])} for c in accounts}
    for c in targets:
        data = T[c.name]
        if data is None:
            run.notes[f"{c.name} has no scheduled-tasks.json yet (open the app once with that account); "
                      "its routines were left alone"] += 1
            continue
        tasks = list(data.get("scheduledTasks", []))
        pos = {t.get("id"): i for i, t in enumerate(tasks)}
        changed = False
        for tid, t in merged.items():
            if tid in pos:
                if tasks[pos[tid]] != t:
                    tasks[pos[tid]] = t
                    changed = True
                    run.count(c, "updated routines", f"~ routine {tid}")
                continue
            if not (t.get("filePath") and os.path.isfile(t["filePath"])):
                run.notes["routines without their SKILL.md (deleted), not copied"] += 1
                continue
            if tid in base.get(c.scope, {}).get("tasks", ()):
                run.notes[f"routines you deleted in {c.name}, not copied back"] += 1
                continue
            tasks.append(t)
            changed = True
            run.count(c, "new routines", f"+ routine {tid} ({'on' if t.get('enabled') else 'off'})")
        if changed:
            p = os.path.join(c.dir, "scheduled-tasks.json")
            run.change_file(p, same_format(p, dict(data, scheduledTasks=tasks)))
            present[c.name] = {t.get("id") for t in tasks}
    return present


# ---------- sidebar ----------

def sidebar_plan(app_dir, accounts, final, ref_name=None):
    cfg = read_json(os.path.join(app_dir, "claude_desktop_config.json")) or {}
    prefs = (cfg.get("preferences") or {}).get("epitaxyPrefs") or {}
    active_id = signed_in_account(app_dir)
    active = next((c for c in accounts if c.account == active_id), None)
    if active is None:
        return None
    # The real pinned list is starred-local-code-sessions; dframe-local-slice.pinnedOrder is only ordering history.
    pinned = set(prefs.get("starred-local-code-sessions") or [])
    all_sections = prefs.get("dframe-code-sections") or {}
    scopes = prefs.get("dframe-group-scopes") or {}

    def sections(c):
        return sorted((all_sections.get(c.scope) or {}).get("sections") or [], key=lambda s: s.get("order", 0))

    def group_by(c):
        return next((s.get("groupBy") for s in sections(c) if s.get("kind") == "sessions"), None)

    def folded(c):
        return {(s.get("name") or SECTION_NAMES.get(s.get("kind"), s.get("kind"))): bool(s.get("collapsed"))
                for s in sections(c)}

    def groups(c):
        gs = scopes.get(c.scope) or {}
        names = {g.get("id"): g.get("name") for g in gs.get("groups") or []}
        for s in sections(c):
            if s.get("kind") == "manual":
                names.setdefault(s.get("id"), s.get("name"))
        assigned = {k.split(":", 1)[1]: names[g] for k, g in (gs.get("assignments") or {}).items()
                    if k.startswith("code:") and names.get(g)}
        order = [s.get("name") for s in sections(c) if s.get("kind") == "manual"]
        return assigned, set(names.values()), order

    a_assigned, a_names, order = groups(active)
    recs = final[active.name]

    def movable(sid):
        # Archived sessions can't be moved, and nested ones (spawnedFrom) always go with their parent.
        r = recs.get(sid)
        if r is None or r.get("isArchived"):
            return False
        parent = (r.get("spawnedFrom") or {}).get("sessionId")
        return not (parent and not r.get("lineageDetached") and parent in recs)

    others = [c for c in accounts if c is not active]
    if ref_name:
        others = [c for c in others if c.name == ref_name]
        if not others:
            raise Fail(f'"{ref_name}" is not one of the other selected accounts')
    move, repin, planned, conflicts = {}, [], set(), 0
    for c in others:
        assigned, _, o = groups(c)
        order += [n for n in o if n not in order]
        for sid, g in assigned.items():
            if not movable(sid) or a_assigned.get(sid) == g or sid in planned:
                continue
            if sid in a_assigned:
                conflicts += 1
                continue
            move.setdefault(g, []).append(sid)
            planned.add(sid)
            if sid in pinned:
                repin.append(sid)   # moving a pinned session into a group unpins it
    order += [n for n in move if n not in order]
    create = [n for n in order if n in move and n not in a_names]
    ref = next((c for c in others if sections(c)), None)
    ref_view, act_view = (group_by(ref) if ref else None), group_by(active)
    view = ref_view or act_view
    if view == "custom" and sections(active):
        view = None   # refused in the sections layout, where groups are always their own sections
    fa, fr = folded(active), (folded(ref) if ref else {})
    return {"open_account": active.name, "reference": ref.name if ref else None,
            "create_groups": create, "move": {n: move[n] for n in order if n in move}, "repin": repin,
            # moving sessions may switch the grouping, so the view is set again at the end
            "set_group_by": view if view and (move or view != act_view) else None,
            "fold": [n for n, v in fr.items() if v and (n in create or (n in fa and not fa[n]))],
            "unfold": [n for n, v in fr.items() if not v and fa.get(n)],
            "conflicts": conflicts}


def sidebar_summary(plan):
    if plan is None:
        return "sidebar: skipped (the account signed in to the app is not one of the selected ones)"
    n = sum(len(v) for v in plan["move"].values())
    if not (n or plan["create_groups"] or plan["set_group_by"]):
        return f'sidebar of the signed-in account ({plan["open_account"]}): already matches'
    return (f'sidebar of the signed-in account ({plan["open_account"]}): groups to create: {len(plan["create_groups"])}, '
            f'sessions to place: {n}. Run `sidebar` for the steps')


def sidebar_steps(plan):
    lines, n = [], 0
    if not (plan["move"] or plan["create_groups"] or plan["repin"] or plan["set_group_by"]):
        lines.append("Nothing to do: the sidebar already matches.")
    else:
        def step(text):
            nonlocal n
            n += 1
            lines.append(f"{n}. {text}")
        if plan["move"]:
            step("mcp__ccd_sidebar__list_groups  (note the id of each group named below)")
        for g in plan["create_groups"]:
            step("mcp__ccd_sidebar__create_group " + json.dumps({"name": g}, ensure_ascii=False)
                 + "  (its result is the new group's id)")
        for g, ids in plan["move"].items():
            for i in range(0, len(ids), 100):
                step("mcp__ccd_sidebar__move_sessions "
                     + json.dumps({"group_id": f"<id of {g}>", "session_ids": ids[i:i + 100]}, ensure_ascii=False))
        for sid in plan["repin"]:
            step("mcp__ccd_sidebar__set_pinned " + json.dumps({"session_id": sid, "pinned": True}))
        if plan["set_group_by"]:
            step("mcp__ccd_sidebar__set_view " + json.dumps({"group_by": plan["set_group_by"]}))
    if plan["fold"] or plan["unfold"]:
        lines.append(f'By hand, to look like "{plan["reference"]}" (there is no tool for this): '
                     f'fold {", ".join(plan["fold"]) or "nothing"}; unfold {", ".join(plan["unfold"]) or "nothing"}.')
    if plan["conflicts"]:
        lines.append(f'Left alone: {plan["conflicts"]} sessions already in a different group here.')
    return lines


# ---------- sync ----------

def verify(run, targets):
    failures = []
    for c in targets:
        paths = glob.glob(os.path.join(c.dir, "local_*.json")) + [
            os.path.join(c.dir, n) for n in
            ("scheduled-tasks.json", "archived-sessions.idx", os.path.join("backlog", "tasks.json"))]
        for p in paths:
            if not os.path.lexists(p):
                continue
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                failures.append(f"not a plain file (link?): {p}")
                continue
            try:
                json.loads(read_bytes(p))
            except ValueError:
                failures.append(f"broken JSON: {p}")
    for p in run.manifest["created"] if os.name == "posix" else ():
        if stat.S_IMODE(os.lstat(p).st_mode) != 0o600:
            failures.append(f"permissions are not 600: {p}")
    return failures


def remember(base_c, key, present):
    """What is there now plus what was remembered, so things deleted in an account stay deleted."""
    return sorted((set(present) | set(base_c.get(key, ()))) - {None}, key=str)


def pid_alive(pid):
    if os.name != "posix":
        return True   # os.kill(pid, 0) is not a harmless check on Windows
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def take_lock(data_dir):
    path = os.path.join(data_dir, ".lock")
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            try:
                pid = int(read_bytes(path).strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and pid_alive(pid):
                raise Fail(f"another account-sync run is in progress (pid {pid}); if it is not, delete {path}")
            os.unlink(path)   # stale lock from a run that is gone
    raise Fail(f"could not take the lock {path}")


def do_sync(app_dir, data_dir, only=None, details=False, apply=False):
    accounts = load_accounts(app_dir, data_dir)
    targets = [c for c in accounts if not only or c.name == only]
    if not targets:
        raise Fail(f'no selected account is called "{only}"')
    real = is_real_app_dir(app_dir)
    running = app_running()
    if apply and real and running:
        raise Fail("Claude is running: quit it with Cmd+Q and run apply again, or use `arm`", code=3)
    lines = []
    if apply and running is None:
        lines.append("note: can't tell whether Claude is running on this system: make sure it is closed")
    ver = app_version() if real else None
    if ver and ver not in TESTED_APP_VERSIONS:
        lines.append(f"note: Claude {ver} has not been tested with account-sync; the format check still guards writes")

    S, problems = {}, []
    for c in accounts:
        S[c.name], p = load_sessions(c)
        problems += p + check_other_files(c)
    if problems:
        lines.append(f"unexpected format in {len(problems)} file{'s' if len(problems) != 1 else ''} "
                     "(the app may have changed how it stores sessions):")
        lines += [f"  {p}" for p in problems[:10]]
        if apply:
            raise Fail("\n".join(lines + ["refusing to write anything"]), code=4)

    os.makedirs(data_dir, exist_ok=True)
    lock = take_lock(data_dir) if apply else None
    state_path = os.path.join(data_dir, "state.json")
    state = read_json(state_path, {})
    base = state.get("accounts", {}) if state.get("app_dir") == app_dir else {}
    run = None
    try:
        run = Run(apply, app_dir, data_dir)
        final = sync_sessions(run, accounts, targets, base, S)
        backlog = sync_backlog(run, accounts, targets, base)
        tasks = sync_tasks(run, accounts, targets, base)

        lines.insert(0, ("Synced" if apply else "Dry run, nothing written") + (f", only into {only}" if only else "") + ":")
        summary = []
        for c in accounts:
            n = run.counts.get(c.name, Counter())
            parts = [f"{k}: {v}" for k, v in n.items() if v]
            written = c in targets
            lines.append(f"  {c.name} ({c.short}){'' if written else ' (not written)'}: "
                         f"{', '.join(parts) if parts else 'nothing to change'} → {len(final[c.name])} sessions")
            summary.append({"name": c.name, "id": c.short, "written": written, "changes": dict(n),
                            "sessions_after": len(final[c.name])})
        for k, v in sorted(run.notes.items()):
            lines.append(f"  note: {k}: {v}")
        plan = sidebar_plan(app_dir, accounts, final)
        lines.append("  " + sidebar_summary(plan))
        if details and run.details:
            lines += run.details
        data = {"applied": apply, "accounts": summary, "notes": dict(run.notes), "format_problems": problems,
                "sidebar": plan}
        if not apply:
            if any(run.counts.values()):
                lines.append("To write these changes: `arm` and quit Claude (or `apply` with Claude closed)." if IS_MAC
                             else "To write these changes: quit Claude, then `apply`.")
            return 0, lines, data

        failures = verify(run, targets)
        new_state = {"version": 1, "when": run.manifest["when"], "app_dir": app_dir, "accounts": {
            c.scope: {"name": c.name,
                      "sessions": remember(base.get(c.scope, {}), "sessions", final[c.name]),
                      "tasks": remember(base.get(c.scope, {}), "tasks", tasks[c.name]),
                      "backlog": remember(base.get(c.scope, {}), "backlog", backlog[c.name])}
            for c in accounts}}
        write_json(state_path, new_state)
        result = {"when": run.manifest["when"], "ok": not failures, "created": len(run.manifest["created"]),
                  "modified": len(run.manifest["modified"]), "failures": len(failures), "accounts": summary}
        write_json(os.path.join(run.dir, "result.json"), result)
        lines += [f"  {f}" for f in failures[:10]]
        lines.append(f"created {result['created']} files, modified {result['modified']}, failures {result['failures']}. "
                     f"Backup and manifest: {run.dir}")
        data.update(result=result, run_dir=run.dir)
        return (1 if failures else 0), lines, data
    finally:
        if apply:
            if run is not None and run.dir:
                run.save_manifest()   # always, even if something failed halfway: undo needs it
            if lock and os.path.exists(lock):
                os.unlink(lock)


# ---------- undo ----------

def undo_impl(app_dir, data_dir, show=False):
    runs = [r for r in sorted(glob.glob(os.path.join(data_dir, "runs", "*")))
            if os.path.isfile(os.path.join(r, "manifest.json")) and not os.path.exists(os.path.join(r, "UNDONE"))]
    if not runs:
        return 1, ["Nothing to undo."], {}
    run_dir = runs[-1]
    man = read_json(os.path.join(run_dir, "manifest.json"))
    root = man.get("app_dir") or "/"
    paths = man["created"] + [m["path"] for m in man["modified"]]
    real = DEFAULT_APP_DIR and any(
        os.path.realpath(p).startswith(os.path.realpath(DEFAULT_APP_DIR) + os.sep) for p in paths)
    previous = os.path.join(run_dir, "previous-state.json")
    state = os.path.join(data_dir, "state.json")
    undone = os.path.join(run_dir, "moved-aside")   # not "undone": macOS file names ignore case
    lines = [f"Undo the sync of {man.get('when')} ({os.path.basename(run_dir)}):",
             f"  files restored from the backup: {len(man['modified'])}",
             f"  files it created, moved to {undone}: {len(man['created'])}",
             "  state.json goes back to the previous one" if os.path.exists(previous)
             else "  state.json is moved aside (there was none before)"]
    data = {"run_dir": run_dir, "restored": len(man["modified"]), "moved_aside": len(man["created"])}
    if show:
        return 0, lines, data
    if real and app_running():
        raise Fail("Claude is running: quit it with Cmd+Q and run undo again, or use `arm --undo`", code=3)
    lock = take_lock(data_dir)
    try:
        for m in man["modified"]:
            restore_copy(m["backup"], m["path"])
        moved = 0
        for p in man["created"]:
            if os.path.lexists(p):
                dst = os.path.join(undone, os.path.relpath(p, root))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(p, dst)
                moved += 1
        for d in reversed(man["dirs_created"]):
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)   # only if empty: nothing is lost
        if os.path.exists(previous):
            restore_copy(previous, state)
        elif os.path.exists(state):
            os.makedirs(undone, exist_ok=True)
            shutil.move(state, os.path.join(undone, "state.json"))
        with open(os.path.join(run_dir, "UNDONE"), "w") as f:
            f.write(now_str() + "\n")
    finally:
        if os.path.exists(lock):
            os.unlink(lock)
    lines.append(f"Done: {len(man['modified'])} restored, {moved} moved aside.")
    return 0, lines, data


# ---------- commands ----------

def cmd_accounts(a):
    app_dir, data_dir = dirs(a)
    found = discover(app_dir)
    me = signed_in_account(app_dir)
    cfg = read_json(os.path.join(data_dir, "accounts.json")) or {}
    selected = {(x["account"], x["org"]): x["name"] for x in cfg.get("accounts", [])}
    ver = app_version() if is_real_app_dir(app_dir) else None
    lines = ["Claude accounts on this computer" + (f" (Claude {ver})" if ver else "") + ":"]
    hidden, rows = 0, []
    in_use = {f["account"] for f in found if f["sessions"]}
    for f in found:
        key = (f["account"], f["org"])
        # an empty folder of an account that has sessions elsewhere is noise; a new account's empty folder is not
        if not f["sessions"] and f["account"] in in_use and not a.all and key not in selected:
            hidden += 1
            continue
        tags = (["signed in"] if f["account"] == me else []) + \
               ([f'selected as "{selected[key]}"'] if key in selected else [])
        last = dt.datetime.fromtimestamp(f["last"]).strftime("%Y-%m-%d %H:%M") if f["last"] else "-"
        lines.append(f'  {f["account"][:8]}/{f["org"][:8]}  {f["sessions"]:>5} sessions  last change {last}'
                     + (f"  [{', '.join(tags)}]" if tags else ""))
        rows.append({"id": f["account"][:8] + "/" + f["org"][:8], "account": f["account"], "org": f["org"],
                     "sessions": f["sessions"], "signed_in": f["account"] == me, "selected_as": selected.get(key)})
    if hidden:
        lines.append(f"  ({hidden} empty folder{'s' if hidden != 1 else ''} hidden; --all shows them)")
    if not selected:
        lines.append("Nothing selected yet: `select NAME=ID NAME=ID` (the first 8 characters of the id are enough).")
    return 0, lines, {"accounts": rows}


def cmd_select(a):
    app_dir, data_dir = dirs(a)
    found = discover(app_dir)
    chosen = []
    for arg in a.ids:
        name, ident = arg.split("=", 1) if "=" in arg else ("", arg)
        f = resolve(found, ident)
        if any(x["account"] == f["account"] and x["org"] == f["org"] for x in chosen):
            raise Fail(f'"{ident}" is the same account as another one in the list')
        name = name or f["account"][:8]
        if not NAME_RE.match(name):
            raise Fail(f'"{name}" is not a valid name (letters, digits, ".", "_" and "-")')
        if any(x["name"] == name for x in chosen):
            raise Fail(f'two accounts are called "{name}"')
        chosen.append({"name": name, "account": f["account"], "org": f["org"]})
    if len(chosen) < 2:
        raise Fail("select at least two accounts")
    os.makedirs(data_dir, exist_ok=True)
    write_json(os.path.join(data_dir, "accounts.json"), {"version": 1, "accounts": chosen})
    return 0, ["Selected for sync: " + ", ".join(f'{x["name"]} ({x["account"][:8]}/{x["org"][:8]})' for x in chosen),
               "Next: `plan`."], {"accounts": chosen}


def cmd_plan(a):
    app_dir, data_dir = dirs(a)
    return do_sync(app_dir, data_dir, a.only, a.details, apply=False)


def cmd_apply(a):
    app_dir, data_dir = dirs(a)
    return do_sync(app_dir, data_dir, a.only, a.details, apply=True)


def cmd_undo(a):
    app_dir, data_dir = dirs(a)
    return undo_impl(app_dir, data_dir, show=a.show)


def cmd_sidebar(a):
    app_dir, data_dir = dirs(a)
    accounts = load_accounts(app_dir, data_dir)
    final = {}
    for c in accounts:
        S, _ = load_sessions(c)
        final[c.name] = {sid: e["rec"] for sid, e in S.items()}
    plan = sidebar_plan(app_dir, accounts, final, a.ref)
    if plan is None:
        return 1, ["The account signed in to the app is not one of the selected accounts."], {}
    os.makedirs(data_dir, exist_ok=True)
    write_json(os.path.join(data_dir, "sidebar-plan.json"), plan)
    lines = [f'Sidebar of the signed-in account "{plan["open_account"]}", in the running app:']
    return 0, lines + sidebar_steps(plan), {"plan": plan}


def launchd_domain():
    return f"gui/{os.getuid()}"


def cmd_arm(a):
    if not IS_MAC:
        raise Fail("arm needs macOS (launchd). Elsewhere: quit Claude and run `apply`")
    app_dir, data_dir = dirs(a)
    if not a.undo:
        load_accounts(app_dir, data_dir)   # fail now, not after you quit Claude
    mode = "undo" if a.undo else "test" if a.test else "sync"
    label = LABEL + (".test" if mode == "test" else "")
    os.makedirs(data_dir, exist_ok=True)
    plist = os.path.join(data_dir, label + ".plist")
    args = [sys.executable, "-I", os.path.abspath(__file__), "_armed", "--mode", mode,
            "--app-dir", app_dir, "--data-dir", data_dir] + (["--only", a.only] if a.only else [])
    log = os.path.join(data_dir, "sync.log")
    with open(plist, "wb") as f:
        plistlib.dump({"Label": label, "ProgramArguments": args, "RunAtLoad": True,
                       "StandardOutPath": log, "StandardErrorPath": log}, f)
    subprocess.run(["/bin/launchctl", "bootout", f"{launchd_domain()}/{label}"], capture_output=True)
    r = subprocess.run(["/bin/launchctl", "bootstrap", launchd_domain(), plist], capture_output=True, text=True)
    if r.returncode != 0:
        raise Fail(f"launchctl bootstrap failed: {r.stderr.strip()}")
    if mode == "test":
        return 0, ["Dry run started from launchd: `status` shows the result in a few seconds."], {"mode": mode}
    what = "undo the last sync" if mode == "undo" else "sync"
    return 0, [f"Armed: as soon as Claude is quit (Cmd+Q) it will {what} and reopen Claude by itself "
               "(it waits up to 2 hours). Cancel with `disarm`."], {"mode": mode}


def cmd_disarm(a):
    if not IS_MAC:
        return 0, ["Nothing to disarm (arm only exists on macOS)."], {"removed": []}
    removed = [label for label in (LABEL, LABEL + ".test")
               if subprocess.run(["/bin/launchctl", "bootout", f"{launchd_domain()}/{label}"],
                                 capture_output=True).returncode == 0]
    return 0, ["Disarmed." if removed else "Nothing was armed."], {"removed": removed}


def armed_mode(data_dir):
    if not IS_MAC:
        return None
    if subprocess.run(["/bin/launchctl", "print", f"{launchd_domain()}/{LABEL}"], capture_output=True).returncode != 0:
        return None
    try:
        with open(os.path.join(data_dir, LABEL + ".plist"), "rb") as f:
            args = plistlib.load(f)["ProgramArguments"]
        return args[args.index("--mode") + 1]
    except Exception:
        return "unknown"


def cmd_status(a):
    app_dir, data_dir = dirs(a)
    lines, data = [], {}
    cfg = read_json(os.path.join(data_dir, "accounts.json")) or {}
    names = [x["name"] for x in cfg.get("accounts", [])]
    lines.append("accounts: " + (", ".join(names) if names else "none selected yet"))
    mode = armed_mode(data_dir)
    lines.append("armed: " + (f"yes ({mode}), waiting for Claude to be quit" if mode else "no"))
    runs = sorted(glob.glob(os.path.join(data_dir, "runs", "*")))
    if runs:
        last = runs[-1]
        res = read_json(os.path.join(last, "result.json"))
        undone = " [undone]" if os.path.exists(os.path.join(last, "UNDONE")) else ""
        if res:
            lines.append(f"last sync: {res['when']} {'OK' if res['ok'] else 'FAILED'} (created {res['created']}, "
                         f"modified {res['modified']}, failures {res['failures']}){undone}")
        else:
            lines.append(f"last sync: {os.path.basename(last)} did not finish; `undo` reverts what it wrote{undone}")
        data["last_run"] = {"dir": last, "result": res, "undone": bool(undone)}
    else:
        lines.append("last sync: none yet")
    log = os.path.join(data_dir, "sync.log")
    if os.path.exists(log):
        with open(log, errors="replace") as f:
            text = f.read().splitlines()
        heads = [i for i, l in enumerate(text) if l.startswith("=== ")]
        if heads:
            block = text[heads[-1]:]
            result = next((l for l in reversed(block) if l.startswith("RESULT: ")), None)
            lines.append(f"last armed run: {block[0][4:]} → "
                         + (result[8:] if result else "no result yet (still waiting, or it was interrupted)"))
            data["last_armed"] = {"header": block[0][4:], "result": result[8:] if result else None}
    return 0, lines, data


def run_safely(fn):
    try:
        return fn()
    except Fail as e:
        return e.code, [f"error: {e}"], {}
    except Exception:
        return 1, [traceback.format_exc()], {}


def cmd_armed(a):
    """What launchd runs (see arm). Output goes to sync.log."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except AttributeError:
            pass
    app_dir, data_dir = dirs(a)
    label = LABEL + (".test" if a.mode == "test" else "")
    print(f"=== {now_str()} armed {a.mode}")
    try:
        if a.mode == "test":
            code, lines, _ = run_safely(lambda: do_sync(app_dir, data_dir, a.only, False, False))
            print("\n".join(lines))
            print(f"RESULT: test {'OK' if code == 0 else f'FAILED (code {code})'}")
            return code
        print("waiting for Claude to be quit (up to 2 hours)")
        deadline = time.time() + WAIT_SECONDS
        while app_running():
            if time.time() > deadline:
                print("RESULT: nothing done (Claude was not quit within 2 hours)")
                return 1
            time.sleep(2)
        print(f"{now_str()} Claude quit; waiting for its helper processes")
        for _ in range(15):
            helpers = subprocess.run(["/usr/bin/pgrep", "-a", "-f", APP_PATH + "/Contents/Frameworks/Claude Helper"],
                                     capture_output=True)
            if helpers.returncode != 0:
                break
            time.sleep(2)
        time.sleep(3)
        if a.mode == "undo":
            code, lines, _ = run_safely(lambda: undo_impl(app_dir, data_dir))
        else:
            code, lines, _ = run_safely(lambda: do_sync(app_dir, data_dir, a.only, False, True))
        print("\n".join(lines))
        print(f"RESULT: {a.mode} {'OK' if code == 0 else f'FAILED (code {code})'}")
        subprocess.run(["/usr/bin/open", "-b", APP_BUNDLE_ID])
        print(f"{now_str()} Claude reopened")
        return code
    finally:
        sys.stdout.flush()
        subprocess.run(["/bin/launchctl", "bootout", f"{launchd_domain()}/{label}"], capture_output=True)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--app-dir", help="the Claude app's data folder (macOS default: ~/Library/Application Support/Claude)")
    common.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="where account-sync keeps its settings, state, backups and log (default: ~/.claude-account-sync)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    p = argparse.ArgumentParser(prog="account_sync.py", description=__doc__.split("\n\n")[0],
                                epilog="Nothing is written to the app unless you use apply, arm or undo.")
    p.add_argument("--version", action="version", version=f"account-sync {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sp = sub.add_parser("accounts", parents=[common], help="list the Claude accounts found on this computer")
    sp.add_argument("--all", action="store_true", help="also show empty account folders")
    sp = sub.add_parser("select", parents=[common], help="choose the accounts to keep in sync")
    sp.add_argument("ids", nargs="+", metavar="[NAME=]ID", help="account id prefix (8 characters are enough) or ACCOUNT/ORG")
    for name, text in (("plan", "dry run: what a sync would change"), ("apply", "sync now; the Claude app must be closed")):
        sp = sub.add_parser(name, parents=[common], help=text)
        sp.add_argument("--only", metavar="NAME", help="write only into this account")
        sp.add_argument("--details", action="store_true", help="list every change")
    sp = sub.add_parser("arm", parents=[common], help="macOS: run as soon as you quit Claude, then reopen it")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--undo", action="store_true", help="undo the last sync instead of syncing")
    g.add_argument("--test", action="store_true", help="run a dry run from launchd now, to check that it works")
    sp.add_argument("--only", metavar="NAME", help="write only into this account")
    sub.add_parser("disarm", parents=[common], help="cancel an armed run")
    sub.add_parser("status", parents=[common], help="result of the last sync and of the last armed run")
    sp = sub.add_parser("sidebar", parents=[common], help="tool calls that make the signed-in account's sidebar match")
    sp.add_argument("--from", dest="ref", metavar="NAME", help="take the groups from this account only")
    sp = sub.add_parser("undo", parents=[common], help="undo the last sync; the Claude app must be closed")
    sp.add_argument("--show", action="store_true", help="only show what it would do")
    sp = sub.add_parser("_armed", parents=[common], help="(internal) what an armed launchd job runs")
    sp.add_argument("--mode", choices=("sync", "undo", "test"), required=True)
    sp.add_argument("--only")
    return p


COMMANDS = {"accounts": cmd_accounts, "select": cmd_select, "plan": cmd_plan, "apply": cmd_apply,
            "arm": cmd_arm, "disarm": cmd_disarm, "status": cmd_status, "sidebar": cmd_sidebar,
            "undo": cmd_undo, "_armed": cmd_armed}


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 0
    if a.cmd == "_armed":
        return cmd_armed(a)
    try:
        code, lines, data = COMMANDS[a.cmd](a)
    except Fail as e:
        code, lines, data = e.code, [f"error: {e}"], {}
    if a.json:
        print(json.dumps(dict(data, ok=code == 0, code=code, lines=lines), ensure_ascii=False, indent=1))
    elif lines:
        print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
