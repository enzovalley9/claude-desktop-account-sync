#!/usr/bin/env python3
"""Tests for account-sync. They never touch the Claude app's data: everything happens inside --tmp.

  python3 tests/test_scenarios.py --tmp DIR              scenarios on made-up data (any OS)
  python3 tests/test_scenarios.py --tmp DIR --real-copy  macOS: also on a COPY of this computer's Claude data:
                                                         apply, apply again (nothing may change), then undo both
                                                         (the copy must end up identical to the original)
  python3 tests/test_scenarios.py --tmp DIR --launchd    macOS: also `arm --test` through launchd (a dry run)
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "skills", "account-sync", "scripts")
SCRIPT = os.path.join(SCRIPTS, "account_sync.py")
PY = sys.executable
IS_MAC = sys.platform == "darwin"
REAL_APP = os.path.expanduser("~/Library/Application Support/Claude")
T0 = 1_790_000_000_000                  # ms, for lastActivityAt
M0 = int(time.time()) - 30 * 86400      # s, for file dates
FAILURES = []


def made_up_id(letter, n):
    return f"{letter * 8}-0000-4000-8000-{n:012x}"


ACC_A, ORG_A, ORG_A2 = made_up_id("a", 1), made_up_id("a", 2), made_up_id("a", 3)
ACC_B, ORG_B = made_up_id("b", 1), made_up_id("b", 2)
ACC_C, ORG_C1, ORG_C2 = made_up_id("c", 1), made_up_id("c", 2), made_up_id("c", 3)
ACC_D, ORG_D = made_up_id("d", 1), made_up_id("d", 2)


def check(cond, text):
    print(("  ok    " if cond else "  FAIL  ") + text)
    if not cond:
        FAILURES.append(text)


def show(text):
    print("\n".join("      | " + line for line in text.rstrip().splitlines()))


def run_script(*args):
    r = subprocess.run([PY, "-I", SCRIPT, *args], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def fingerprint(d, mtimes=True):
    """relative path -> (sha256, permissions, links[, date]) of every file under d."""
    out = {}
    for base, _, files in os.walk(d):
        for f in files:
            p = os.path.join(base, f)
            st = os.lstat(p)
            with open(p, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            out[os.path.relpath(p, d)] = (digest, st.st_mode & 0o7777, st.st_nlink) + \
                ((int(st.st_mtime),) if mtimes else ())
    return out


def sid(n):
    return f"local_{n:08x}-0000-4000-8000-000000000000"


def cli_id(n):
    return f"{n:08x}-1111-4000-8000-000000000000"


def write(p, obj, mtime=None, indent=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
    os.chmod(p, 0o600)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def load(p):
    with open(p) as f:
        return json.load(f)


def session_ids(d):
    return {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(d, "local_*.json"))}


class Env:
    """A made-up Claude data folder with a few accounts."""

    def __init__(self, base):
        self.app = os.path.join(base, "app")
        self.data = os.path.join(base, "data")
        self.skills = os.path.join(base, "skills")
        self.trash = os.path.join(base, "trash")     # where the "app" puts what it deletes
        root = os.path.join(self.app, "claude-code-sessions")
        self.A = os.path.join(root, ACC_A, ORG_A)
        self.B = os.path.join(root, ACC_B, ORG_B)
        self.D = os.path.join(root, ACC_D, ORG_D)
        for d in (self.A, self.B, os.path.join(root, ACC_A, ORG_A2), os.path.join(root, ACC_C, ORG_C1),
                  os.path.join(root, ACC_C, ORG_C2), self.skills, self.trash):
            os.makedirs(d)

    def run(self, *args):
        return run_script(*args, "--app-dir", self.app, "--data-dir", self.data)

    def session(self, d, n, act, mtime, **extra):
        rec = {"sessionId": sid(n), "cliSessionId": cli_id(n), "title": f"session {n}",
               "lastActivityAt": T0 + act * 1000, "cwd": "/tmp/test", **extra}
        write(os.path.join(d, sid(n) + ".json"), rec, mtime)

    def rec(self, d, n):
        p = os.path.join(d, sid(n) + ".json")
        return load(p) if os.path.exists(p) else None

    def delete_session(self, d, n):
        os.rename(os.path.join(d, sid(n) + ".json"), os.path.join(self.trash, sid(n) + ".json"))

    def marker(self, d, uid):
        with open(os.path.join(d, "deleted_" + uid), "w") as f:
            f.write(str(T0))

    def routine(self, rid, skill=True, **extra):
        p = os.path.join(self.skills, rid, "SKILL.md")
        if skill:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("test\n")
        return {"id": rid, "cronExpression": "0 9 * * *", "enabled": True, "filePath": p,
                "createdAt": T0, "lastRunAt": "2026-10-01T08:00:00.000Z", "cwd": "/tmp/test", **extra}

    def routines(self, d, tasks, mtime):
        write(os.path.join(d, "scheduled-tasks.json"),
              {"scheduledTasks": tasks, "recordedSkips": [], "sundayAliasBoundaryStamped": True,
               "dayFieldsOrBoundaryStamped": True}, mtime, indent=2)

    def routine_list(self, d):
        return {t["id"]: t for t in load(os.path.join(d, "scheduled-tasks.json"))["scheduledTasks"]}

    def backlog(self, d, items=None, mtime=None):
        p = os.path.join(d, "backlog", "tasks.json")
        if items is not None:
            write(p, {"version": 1, "items": [{"id": i, "title": i} for i in items]}, mtime)
        return [i["id"] for i in load(p)["items"]] if os.path.exists(p) else None

    def archived(self, d):
        p = os.path.join(d, "archived-sessions.idx")
        return load(p)["archived"] if os.path.exists(p) else None


def scenarios(tmp):
    E = Env(os.path.join(tmp, "scenarios"))
    A, B = E.A, E.B
    state = os.path.join(E.data, "state.json")

    # an old account with history and a new, empty one
    E.session(A, 1, 10, M0 + 10)
    E.session(A, 2, 20, M0 + 20, error="weekly limit", errorAt=T0, errorRows=[1], priorErrorMark="x")
    E.session(A, 3, 30, M0 + 30)
    E.session(A, 4, 40, M0 + 40, spawnedFrom={"sessionId": sid(1), "title": "parent"})
    E.session(A, 6, 60, M0 + 60)
    E.session(A, 7, 70, M0 + 70, isArchived=True)
    E.session(A, 8, 80, M0 + 80)
    write(os.path.join(A, "archived-sessions.idx"), {"v": 1, "archived": [sid(7)]}, M0)
    E.marker(A, cli_id(5))
    E.routines(A, [E.routine("r1"), E.routine("r2"), E.routine("r-no-skill", skill=False)], M0 + 100)
    E.backlog(A, ["k1", "k2"], M0)
    E.routines(B, [], M0)

    print("\n0. Finding and selecting accounts")
    rc, out = E.run("accounts")
    show(out)
    check(rc == 0 and "aaaaaaaa/aaaaaaaa" in out and "7 sessions" in out, "lists the account with sessions")
    check("bbbbbbbb/bbbbbbbb" in out, "shows a new account even though it is still empty")
    check("(1 empty folder hidden" in out and out.count("cccccccc/") == 2,
          "hides only the empty folder of an account that has sessions in another one")
    rc, out = E.run("accounts", "--all")
    check(rc == 0 and out.count(" sessions  last change") == 5, "--all shows every folder")
    rc, out = E.run("plan")
    check(rc == 2 and "no accounts selected yet" in out, "plan before select says what to do")
    rc, out = E.run("plan", "--json")
    j = json.loads(out)
    check(j["ok"] is False and j["code"] == 2 and j["lines"], "--json reports errors as JSON too")
    rc, out = E.run("select", "x=cccccccc", "new=bbbbbbbb")
    check(rc == 2 and "matches several folders" in out, "an ambiguous id is refused")
    rc, out = E.run("select", "old=aaaaaaaa")
    check(rc == 2 and "at least two" in out, "one account is not enough")
    rc, out = E.run("select", "bad name=aaaaaaaa", "new=bbbbbbbb")
    check(rc == 2 and "not a valid name" in out, "names are checked")
    rc, out = E.run("select", "old=aaaaaaaa", "new=bbbbbbbb")
    picked = load(os.path.join(E.data, "accounts.json"))["accounts"]
    check(rc == 0 and [(x["name"], x["org"]) for x in picked] == [("old", ORG_A), ("new", ORG_B)],
          "selects the accounts (for A, the folder that has sessions)")
    rc, out = E.run("accounts")
    check('selected as "old"' in out and 'selected as "new"' in out, "accounts shows the selection")

    print("\n1. A new, empty account is filled from scratch")
    h0 = fingerprint(E.app)
    rc, out = E.run("plan")
    show(out)
    check(rc == 0 and "Dry run, nothing written" in out, "the dry run ends well")
    check("new sessions: 7" in out, "the dry run announces the 7 sessions")
    check(fingerprint(E.app) == h0, "the dry run writes nothing into the app folder")
    check(not os.path.exists(state) and not os.path.exists(os.path.join(E.data, "runs")),
          "the dry run leaves no state and no backup")

    rc, out = E.run("apply")
    show(out)
    check(rc == 0 and "created 10 files, modified 1, failures 0" in out, "apply ends well: 10 created, 1 modified")
    check(all(E.rec(B, n) for n in (1, 2, 3, 4, 6, 7, 8)), "the new account has the 7 sessions")
    check(not any(k in E.rec(B, 2) for k in ("error", "errorAt", "errorRows", "priorErrorMark")),
          "the copy doesn't carry the other account's error notices")
    check("error" in E.rec(A, 2), "the original keeps its notice")
    check(int(os.stat(os.path.join(B, sid(1) + ".json")).st_mtime) == M0 + 10, "the copy keeps the original's date")
    check(all(os.stat(os.path.join(B, sid(n) + ".json")).st_mode & 0o777 == 0o600 for n in (1, 2, 3)),
          "the copies have permissions 600")
    check(all(os.stat(os.path.join(B, sid(n) + ".json")).st_nlink == 1 for n in (1, 2, 3)),
          "the copies are plain files, not links")
    check(E.archived(B) == [sid(7)], "archive index created with the archived session")
    check(E.backlog(B) == ["k1", "k2"], "backlog created with its 2 items")
    check(set(E.routine_list(B)) == {"r1", "r2"}, "routines copied, except the one without a SKILL.md")
    check(os.path.exists(os.path.join(B, "deleted_" + cli_id(5))), "deletion marker copied")
    check(os.path.exists(state), "state.json is kept for next time")

    print("\n2. Running it again changes nothing")
    h1 = fingerprint(E.app)
    rc, out = E.run("apply")
    check(rc == 0 and out.count("nothing to change") == 2, "both accounts: nothing to change")
    check("created 0 files, modified 0, failures 0" in out, "0 files created or modified")
    check(fingerprint(E.app) == h1, "the app folder is identical")

    print("\n3. Changes in both accounts")
    E.session(B, 10, 100, M0 + 100)                                              # new in the new account
    E.session(B, 1, 110, M0 + 110, title="session 1 (continued in new)")         # continued in the new one
    E.delete_session(A, 3)                                                       # deleted in the old one
    E.session(B, 9, 90, M0 + 90)                                                 # alive in the new one...
    E.marker(A, cli_id(9))                                                       # ...with a marker in the old
    ra, rb = E.routine_list(A), E.routine_list(B)
    ra["r1"]["missedRunScanFloor"] = "2026-11-05T00:00:00.000Z"
    E.routines(A, list(ra.values()), M0 + 150)
    rb["r1"].update(cronExpression="0 10 * * *", lastRunAt="2026-11-01T08:00:00.000Z")
    del rb["r2"]                                                                 # r2 deleted in the new one
    E.routines(B, list(rb.values()), M0 + 200)                                   # the new one, used last
    E.session(A, 6, 60, M0 + 300, isArchived=True)                               # archived in the old one
    write(os.path.join(A, "archived-sessions.idx"), {"v": 1, "archived": [sid(7), sid(6)]}, M0 + 300)
    E.backlog(A, ["k1", "k2", "k3"], M0 + 300)                                   # k3 new in the old one
    E.backlog(B, ["k2"], M0 + 300)                                               # k1 deleted in the new one

    h_before = fingerprint(E.app)
    with open(state) as f:
        state_before = f.read()
    rc, out = E.run("apply")
    show(out)
    check(rc == 0 and "failures 0" in out, "apply ends well")
    check(E.rec(A, 10) is not None, "a session created in the new account reaches the old one")
    check(E.rec(A, 1)["title"] == "session 1 (continued in new)", "a session continued in the new one is updated in the old one")
    check(E.rec(A, 3) is None and E.rec(B, 3) is not None, "deleted in the old one: not brought back (and kept in the new one)")
    check(E.rec(A, 9) is None, "a session with a deletion marker is not copied")
    check(not os.path.exists(os.path.join(B, "deleted_" + cli_id(9))),
          "the marker doesn't go to the account where the session is alive")
    r1a, r1b = E.routine_list(A)["r1"], E.routine_list(B)["r1"]
    check(r1a == r1b, "r1 ends up the same in both")
    check(r1a["cronExpression"] == "0 10 * * *", "the definition comes from the account used last")
    check(r1a["lastRunAt"] == "2026-11-01T08:00:00.000Z", "last run: the latest one (so it doesn't run twice)")
    check(r1a.get("missedRunScanFloor") == "2026-11-05T00:00:00.000Z", "scan floor: the latest one")
    check("r2" not in E.routine_list(B) and "r2" in E.routine_list(A), "the routine deleted in the new one doesn't come back")
    with open(os.path.join(B, "scheduled-tasks.json")) as f:
        check(f.read().startswith("{\n"), "scheduled-tasks.json stays indented, the way the app writes it")
    check(E.rec(B, 6).get("isArchived") is True and sid(6) in E.archived(B),
          "archived in the old one: archived in the new one and listed in its index")
    check(E.backlog(B) == ["k2", "k3"], "backlog: k3 arrives and k1 (deleted in the new one) doesn't come back")
    check(set(E.backlog(A)) == {"k1", "k2", "k3"}, "the old account's backlog is untouched")
    check("error" not in E.rec(B, 2) and "error" in E.rec(A, 2), "error notices stay in their own account")

    print("\n4. Undo")
    h_after = fingerprint(E.app)
    h_after_content = fingerprint(E.app, mtimes=False)
    rc, out = E.run("undo", "--show")
    check(rc == 0 and fingerprint(E.app) == h_after, "--show changes nothing")
    rc, out = E.run("undo")
    show(out)
    check(rc == 0, "undo ends well")
    check(fingerprint(E.app) == h_before, "the app folder is exactly as before the sync (contents, permissions, dates)")
    with open(state) as f:
        check(f.read() == state_before, "state.json is back to the previous one")
    rc, out = E.run("apply")
    check(rc == 0 and E.rec(A, 10) is not None and E.rec(A, 3) is None
          and fingerprint(E.app, mtimes=False) == h_after_content, "syncing again after undo gives the same result")
    rc, out = E.run("apply")
    check(out.count("nothing to change") == 2, "and once more changes nothing")

    print("\n5. Writing into one account only (--only)")
    E.session(B, 11, 120, M0 + 400)
    rc, out = E.run("plan", "--only", "old")
    check("(not written)" in out and "new sessions: 1" in out, "dry run: only the old account would be written")
    rc, out = E.run("apply", "--only", "new")
    check(rc == 0 and E.rec(A, 11) is None, "--only new doesn't write into the old one")
    rc, out = E.run("apply", "--only", "old")
    check(rc == 0 and E.rec(A, 11) is not None, "--only old does (and didn't take it for deleted)")

    print("\n6. Sidebar plan")
    write(os.path.join(E.app, "config.json"), {"lastKnownAccountUuid": ACC_B})
    sa, sb = f"{ACC_A}/{ORG_A}", f"{ACC_B}/{ORG_B}"
    prefs = {
        "starred-local-code-sessions": [sid(8)],
        "dframe-local-slice": {"pinnedOrder": ["code:" + sid(n) for n in (1, 2, 10)]},   # not the pinned list
        "dframe-group-scopes": {
            sa: {"groups": [{"id": "ga1", "name": "Work"}, {"id": "ga2", "name": "Music"}],
                 "assignments": {"code:" + sid(1): "ga1", "code:" + sid(4): "ga1", "code:" + sid(6): "ga2",
                                 "code:" + sid(8): "ga2", "code:" + sid(10): "ga2", "code:" + sid(2): "ga2"}},
            sb: {"groups": [{"id": "gb1", "name": "Work"}],
                 "assignments": {"code:" + sid(1): "gb1", "code:" + sid(2): "gb1"}}},
        "dframe-code-sections": {
            sa: {"sections": [{"kind": "pinned", "order": 0},
                              {"kind": "manual", "id": "ga1", "name": "Work", "order": 1},
                              {"kind": "manual", "id": "ga2", "name": "Music", "order": 2, "collapsed": True},
                              {"kind": "sessions", "groupBy": "none", "order": 3}]},
            sb: {"sections": [{"kind": "pinned", "order": 0},
                              {"kind": "manual", "id": "gb1", "name": "Work", "order": 1},
                              {"kind": "sessions", "groupBy": "folder", "order": 2, "collapsed": True}]}},
    }
    write(os.path.join(E.app, "claude_desktop_config.json"), {"preferences": {"epitaxyPrefs": prefs}})
    h = fingerprint(E.app)
    rc, out = E.run("sidebar")
    show(out)
    plan = load(os.path.join(E.data, "sidebar-plan.json"))
    check(rc == 0 and fingerprint(E.app) == h, "sidebar writes nothing into the app folder")
    check(plan["open_account"] == "new", "the plan is for the signed-in account")
    check(plan["create_groups"] == ["Music"], "creates the missing group")
    check(list(plan["move"]) == ["Music"] and set(plan["move"]["Music"]) == {sid(8), sid(10)},
          "places only what is missing: no nested, no archived, nothing already placed")
    check(plan["repin"] == [sid(8)], "the pinned (starred) session is pinned again; pinnedOrder doesn't count")
    check(plan["conflicts"] == 1, "a session that is in another group here is left alone")
    check(plan["set_group_by"] == "none", "no folders, like in the other account")
    check(plan["fold"] == ["Music"] and plan["unfold"] == ["Sessions"], "says what to fold and unfold by hand")
    check("mcp__ccd_sidebar__create_group" in out and '"<id of Music>"' in out, "prints the tool calls to make")
    rc, out = E.run("plan")
    check("groups to create: 1, sessions to place: 2" in out, "plan sums up the sidebar in one line")
    rc, out = E.run("sidebar", "--from", "nobody")
    check(rc == 2, "--from with an unknown account is refused")

    print("\n7. The format check stops writes when files don't look as expected")
    bad = os.path.join(A, sid(99) + ".json")
    write(bad, {"sessionId": "something-else", "lastActivityAt": T0})
    E.session(B, 12, 130, M0 + 500)     # something to sync
    h = fingerprint(E.app)
    runs_before = len(glob.glob(os.path.join(E.data, "runs", "*")))
    rc, out = E.run("plan")
    check(rc == 0 and "unexpected format in 1 file" in out, "plan reports the odd file")
    rc, out = E.run("apply")
    check(rc == 4 and "refusing to write anything" in out, "apply refuses (exit code 4)")
    check(fingerprint(E.app) == h and len(glob.glob(os.path.join(E.data, "runs", "*"))) == runs_before,
          "and writes nothing")
    os.rename(bad, os.path.join(E.trash, os.path.basename(bad)))

    if os.name == "posix":
        print("\n8. One run at a time")
        lock = os.path.join(E.data, ".lock")
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        rc, out = E.run("apply")
        check(rc == 2 and "in progress" in out and E.rec(A, 12) is None, "a live lock stops a second run")
        p = subprocess.Popen([PY, "-c", "pass"])
        p.wait()
        with open(lock, "w") as f:
            f.write(str(p.pid))
        rc, out = E.run("apply")
        check(rc == 0 and E.rec(A, 12) is not None and not os.path.exists(lock),
              "a lock left by a run that is gone is cleared")

    print("\n9. Status")
    rc, out = E.run("status")
    show(out)
    check(rc == 0 and "accounts: old, new" in out and "OK (created" in out, "shows the accounts and the last sync")

    print("\n10. Three accounts")
    os.makedirs(E.D)
    E.session(E.D, 20, 200, M0 + 600)
    E.session(E.D, 21, 210, M0 + 610)
    E.routines(E.D, [], M0)
    rc, out = E.run("select", "old=aaaaaaaa", "new=bbbbbbbb", "third=dddddddd")
    check(rc == 0, "three accounts selected")
    rc, out = E.run("apply")
    show(out)
    check(rc == 0 and "failures 0" in out, "apply with three accounts ends well")
    check({sid(20), sid(21)} <= session_ids(A) and {sid(20), sid(21)} <= session_ids(B),
          "the third account's sessions reach the other two")
    check(session_ids(E.D) == (session_ids(A) | session_ids(B)) - {sid(9)},
          "the third account gets everything except what was deleted elsewhere")
    rc, out = E.run("apply")
    check(out.count("nothing to change") == 3, "once more: nothing to change in any of the three")
    rc, out = E.run("plan", "--json")
    j = json.loads(out)
    check(j["ok"] is True and len(j["accounts"]) == 3, "plan --json is machine-readable")

    if not IS_MAC:
        rc, out = E.run("arm")
        check(rc == 2 and "macOS" in out, "arm explains that it needs macOS")
    return E


def launchd(E):
    print("\n11. arm --test through launchd (a dry run)")
    log = os.path.join(E.data, "sync.log")
    rc, out = E.run("arm", "--test")
    check(rc == 0, "arm --test is accepted by launchd")
    deadline = time.time() + 30
    text = ""
    while time.time() < deadline:
        if os.path.exists(log):
            with open(log) as f:
                text = f.read()
            if "RESULT:" in text:
                break
        time.sleep(1)
    show(text[-600:])
    check("RESULT: test OK" in text, "the launchd job ran the dry run")
    time.sleep(1)
    gone = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/local.claude-account-sync.test"],
                          capture_output=True).returncode != 0
    check(gone, "the job removed itself when done")
    rc, out = E.run("status")
    check("last armed run:" in out and "test OK" in out, "status shows the armed run")


def real_copy(tmp):
    print("\n12. A copy of this computer's real data (apply, apply again, undo both)")
    root = os.path.join(REAL_APP, "claude-code-sessions")
    if not IS_MAC or not os.path.isdir(root):
        print("  skip  no Claude data on this computer")
        return
    picked = [(acc, org) for acc in sorted(os.listdir(root)) if os.path.isdir(os.path.join(root, acc))
              for org in sorted(os.listdir(os.path.join(root, acc)))
              if glob.glob(os.path.join(root, acc, org, "local_*.json"))]
    if len(picked) < 2:
        print("  skip  fewer than two accounts with sessions")
        return
    base = os.path.join(tmp, "real-copy")
    app, data = os.path.join(base, "app"), os.path.join(base, "data")
    for acc, org in picked:
        rel = os.path.join("claude-code-sessions", acc, org)
        shutil.copytree(os.path.join(REAL_APP, rel), os.path.join(app, rel), symlinks=True)
    for f in ("claude_desktop_config.json", "config.json"):
        if os.path.exists(os.path.join(REAL_APP, f)):
            shutil.copy2(os.path.join(REAL_APP, f), os.path.join(app, f))

    def go(*args):
        return run_script(*args, "--app-dir", app, "--data-dir", data)

    rc, out = go("select", *[f"acct{i + 1}={acc}/{org}" for i, (acc, org) in enumerate(picked)])
    check(rc == 0, f"{len(picked)} accounts with sessions selected")
    h0 = fingerprint(app)
    rc, out = go("plan")
    show(out)
    check(rc == 0 and fingerprint(app) == h0, "the dry run on the copy writes nothing")
    rc, out = go("apply")
    show(out)
    check(rc == 0 and "failures 0" in out, "apply on the copy ends well, with no failures")
    h1 = fingerprint(app)
    rc, out = go("apply")
    check(rc == 0 and out.count("nothing to change") == len(picked) and fingerprint(app) == h1,
          "second run: nothing changes")
    rc1, _ = go("undo")
    rc2, _ = go("undo")
    check(rc1 == 0 and rc2 == 0 and fingerprint(app) == h0, "undoing both leaves the copy identical to the original")
    sys.path.insert(0, SCRIPTS)
    import account_sync
    running = account_sync.app_running()
    print(f"  info  Claude running now, according to account_sync: {'yes' if running else 'no'} "
          "(with the real folder and Claude running, apply refuses)")


def main():
    ap = argparse.ArgumentParser(description="account-sync tests (never touch the real Claude data)")
    ap.add_argument("--tmp", required=True, help="working folder for the tests")
    ap.add_argument("--real-copy", action="store_true", help="macOS: also test on a copy of this computer's data")
    ap.add_argument("--launchd", action="store_true", help="macOS: also run `arm --test` through launchd")
    a = ap.parse_args()
    tmp = os.path.join(os.path.abspath(a.tmp), "account-sync-test-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    if os.path.realpath(tmp).startswith(os.path.realpath(REAL_APP) + os.sep):
        sys.exit("--tmp can't be inside the Claude app's data folder")
    os.makedirs(tmp)
    print("Test folder:", tmp)
    E = scenarios(tmp)
    if a.launchd:
        if IS_MAC:
            launchd(E)
        else:
            print("\n11. skip  --launchd needs macOS")
    if a.real_copy:
        real_copy(tmp)
    print(f"\n{'ALL OK' if not FAILURES else f'{len(FAILURES)} FAILED'}")
    for f in FAILURES:
        print("  FAIL ", f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
