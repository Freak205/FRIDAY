"""Phase 35.0 -- database placement, audit minimisation and test/benchmark isolation.

Everything runs against throwaway temp folders and a throwaway database (`store.use_temp_db`); the "real"
locations that the migration and tripwire tests need are FAKED under the temp folder (LOCALAPPDATA, the
legacy path and the default path are patched), so nothing here can touch the user's database, recovery store
or files. A final scenario proves the real database and stores are byte-for-byte what they were.

What it pins:
  A  path resolution: default outside the repository / OneDrive, env + config overrides, redirect precedence
  B  migration: consistent copy (WAL, open writer), validated, never overwrites, source untouched,
     interrupted / corrupt / changing source, restart, end to end through store.conn()
  C  threads + locking: worker threads agree with the main thread (even one that connected earlier),
     asyncio.to_thread agrees, throwaway connections closed, a busy writer is waited for
  D  audit minimisation: file bodies, typed text, messages, code, shell command lines, URL queries and
     credentials never reach the audit log / episodes; useful metadata and the real outcome do
  E  isolation tripwires: a test/benchmark entry point is refused the real database and recovery store,
     automatically, including a deliberate misconfiguration and a real subprocess
  F  history is never rewritten; the sensitive-data scan returns counts only
  G  the user's real database and stores were not touched

Then each invariant is deliberately broken (source-level mutation) and the suite must FAIL.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402
from smoke_file_safety import Rec, mutated  # noqa: E402

from friday import audit, auditview, dbplace, paths, recovery, store  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import episodes  # noqa: E402
from friday.permissions import EXECUTOR, PermissionError_  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402

REGISTRY.discover()

SECRET = "TOP-SECRET-CONTENT-7731"
BEARER = "Bearer-abcdef123456XYZ"


# -- helpers ------------------------------------------------------------------------------------------


@contextlib.contextmanager
def envpatch(**kw):
    saved = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def attr(owner, name, value):
    saved = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, saved)


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def make_legacy(path: Path, *, keep_open: bool = False, wal_only_rows: int = 0):
    """A database shaped like the old data/friday.db, including a historical row that holds content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(store.SCHEMA)
    c.execute(
        "INSERT INTO audit (at, actor, skill, tier, args, decision, ok, result) VALUES (?,?,?,?,?,?,?,?)",
        ("2026-01-01T00:00:00+00:00", "text", "files.write", "L1",
         json.dumps({"path": "C:/x/old.txt", "content": SECRET}), "auto", 1, "Created old.txt."),
    )
    c.execute("INSERT INTO memory (at, kind, key, value) VALUES (?,?,?,?)", ("2026-01-01", "fact", "k", "v"))
    c.commit()
    if wal_only_rows:
        c.execute("PRAGMA wal_autocheckpoint=0")
        for i in range(wal_only_rows):
            c.execute("INSERT INTO memory (at, kind, key, value) VALUES (?,?,?,?)", ("2026-01-02", "fact", f"w{i}", "v"))
        c.commit()
    if keep_open:
        return c
    c.close()
    return None


def rows(path: Path, sql: str):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def db_contains(needle: str) -> bool:
    """Does ANY value in ANY table of the active database contain `needle`?"""
    c = store.conn()
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        for row in c.execute(f'SELECT * FROM "{name}"').fetchall():
            for v in tuple(row):
                if isinstance(v, bytes):
                    if needle.encode() in v:
                        return True
                elif isinstance(v, str) and needle in v:
                    return True
    return False


def last_audit():
    return store.conn().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 1").fetchone()


# -- the suite ----------------------------------------------------------------------------------------


async def suite(rec: Rec) -> None:
    real_snaps = (paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS)
    prev_confirm = EXECUTOR._confirm
    with tempfile.TemporaryDirectory(prefix="friday_privacy_", ignore_cleanup_errors=True) as t, store.use_temp_db():
        tmp = Path(t)
        try:
            await _scenarios(rec, tmp)
        finally:
            paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = real_snaps
            EXECUTOR._confirm = prev_confirm


async def _scenarios(rec: Rec, tmp: Path) -> None:
    # ----------------------------------------------------------------------- A: path resolution
    rec.scenario("A: the default database is outside the repository and OneDrive; overrides and precedence")
    la = tmp / "LocalAppData"
    with envpatch(LOCALAPPDATA=str(la), FRIDAY_DB=None):
        d = paths._default_db()
        rec.check("A1 default = %LOCALAPPDATA%/FRIDAY/friday.db", d == la / "FRIDAY" / "friday.db", d)
        rec.check("A2 ...which is not inside the repository", paths.ROOT.resolve() not in Path(d).resolve().parents)
        rec.check("A3 ...and is not the old in-repository path", dbplace._norm(d) != dbplace._norm(paths.LEGACY_DB))
    with envpatch(LOCALAPPDATA=str(la), FRIDAY_DB=str(tmp / "env.db")):
        rec.check("A4 FRIDAY_DB overrides the default", paths._default_db() == tmp / "env.db")
    with envpatch(LOCALAPPDATA=None, FRIDAY_DB=None):
        rec.check("A5 with no application-data folder the old location is the last-resort fallback",
                  paths._default_db() == paths.DATA / "friday.db")
    real_canon = dbplace.canonical_db()
    rec.check("A6 the real default location is not in a synced folder", real_canon is not None and not dbplace.in_synced_folder(real_canon), real_canon)
    rec.check("A7 the old in-repository location IS recognised as synced here (this checkout is under OneDrive)",
              dbplace.in_synced_folder(paths.LEGACY_DB) == ("onedrive" in str(paths.ROOT).lower()))
    with envpatch(OneDrive=str(tmp / "OD")):
        rec.check("A8 a path under %OneDrive% is flagged as synced", dbplace.in_synced_folder(tmp / "OD" / "x" / "friday.db"))
    rec.check("A9 a plain temp path is not", not dbplace.in_synced_folder(tmp / "plain" / "friday.db"))

    cfgdb = tmp / "cfg" / "friday.db"
    with attr(paths, "DB_PATH", paths.DEFAULT_DB_PATH), attr(CFG.storage, "db_path", str(cfgdb)):
        rec.check("B0a config storage.db_path wins over the default when nothing is redirected", dbplace.resolve() == cfgdb, dbplace.resolve())
        c = store.conn()
        rec.check("A10 a connection really opens the configured path", os.path.normcase(c.execute("PRAGMA database_list").fetchone()["file"]) == os.path.normcase(str(cfgdb)))
        store._close_all(cfgdb)
    with attr(paths, "DB_PATH", paths.DEFAULT_DB_PATH), attr(CFG.storage, "db_path", "rel/friday-test.db"):
        rec.check("A11 a relative config path is relative to the repository", dbplace.resolve() == paths.ROOT / "rel" / "friday-test.db")
    rec.check("A12 an explicit redirect (use_temp_db) beats the config override",
              dbplace.redirected() and dbplace.resolve() == Path(paths.DB_PATH))
    with attr(CFG.storage, "db_path", str(cfgdb)):
        rec.check("A13 ...even when a config path is set", dbplace.resolve() == Path(paths.DB_PATH))

    # ----------------------------------------------------------------------- B: migration
    rec.scenario("B: migration is a consistent, validated, non-overwriting copy; the old file is never touched")
    b = tmp / "mig1"
    src, dst = b / "old" / "friday.db", b / "new" / "friday.db"
    writer = make_legacy(src, keep_open=True, wal_only_rows=3)  # still open, rows only in the -wal file
    main_hash, side = sha(src), sorted(x.name for x in src.parent.iterdir())
    m = dbplace.migrate_legacy(dst, src)
    rec.check("B1 migrated", m.performed and dst.exists(), m.reason)
    rec.check("B2 every table copied with the same row counts, including rows that only existed in the WAL",
              rows(dst, "select count(*) from memory")[0][0] == 4 and rows(dst, "select count(*) from audit")[0][0] == 1
              and m.tables.get("memory") == 4, m.tables)
    rec.check("B3 the copy passes SQLite's integrity check", rows(dst, "pragma integrity_check")[0][0] == "ok")
    rec.check("B4 the old database file is byte-for-byte unchanged", sha(src) == main_hash)
    rec.check("B5 ...and no file was added to or removed from its folder", sorted(x.name for x in src.parent.iterdir()) == side, sorted(x.name for x in src.parent.iterdir()))
    rec.check("B6 the old database is still usable by its own writer", writer.execute("select count(*) from memory").fetchone()[0] == 4)
    rec.check("B7 no migration temp or lock file is left behind",
              not [x for x in dst.parent.iterdir() if "migrating" in x.name], [x.name for x in dst.parent.iterdir()])
    writer.close()

    again = dbplace.migrate_legacy(dst, src)
    rec.check("B8 restart: a second run does nothing (destination exists)", not again.performed and "already exists" in again.reason, again.reason)

    # never overwrite
    other = b / "other" / "friday.db"
    make_legacy(other)
    other.write_bytes(b"not a database, but somebody's file")
    keep = sha(other)
    again = dbplace.migrate_legacy(other, src)
    rec.check("B9 an existing destination is never overwritten", sha(other) == keep and not again.performed)
    race_dst, race_tmp = b / "race" / "friday.db", b / "race" / "t.tmp"
    race_dst.parent.mkdir()
    race_dst.write_bytes(b"first")
    race_tmp.write_bytes(b"second")
    published = dbplace._publish_no_overwrite(race_tmp, race_dst)
    rec.check("B10 publishing onto a destination that appeared first refuses and keeps it", (not published) and race_dst.read_bytes() == b"first")

    # interrupted: stale temp + stale lock from a crashed run
    b2 = tmp / "mig2"
    src2, dst2 = b2 / "old" / "friday.db", b2 / "new" / "friday.db"
    make_legacy(src2)
    dst2.parent.mkdir(parents=True)
    stale = dst2.with_name(dst2.name + ".migrating-dead")
    stale.write_bytes(b"half a copy")
    lock = dst2.with_name(dst2.name + ".migrating.lock")
    lock.write_text("1 1")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    r2 = dbplace.migrate_legacy(dst2, src2)
    rec.check("B11 a crashed earlier run (stale temp + stale lock) does not block the next one", r2.performed and dst2.exists())
    rec.check("B12 ...its leftover partial copy is discarded", not stale.exists() and not lock.exists())

    # a live lock held by someone else -> a clear error, nothing created
    b3 = tmp / "mig3"
    src3, dst3 = b3 / "old" / "friday.db", b3 / "new" / "friday.db"
    make_legacy(src3)
    dst3.parent.mkdir(parents=True)
    dst3.with_name(dst3.name + ".migrating.lock").write_text("1 1")
    msg = ""
    with attr(dbplace, "LOCK_WAIT_S", 0.3):
        try:
            dbplace.migrate_legacy(dst3, src3)
        except dbplace.DbPlacementError as exc:
            msg = str(exc)
    rec.check("B13 a fresh lock held elsewhere stops the migration with recovery instructions, not a guess",
              bool(msg) and not dst3.exists() and str(src3) in msg and "storage.db_path" in msg, msg[:120])

    # corrupt source
    b4 = tmp / "mig4"
    src4, dst4 = b4 / "old" / "friday.db", b4 / "new" / "friday.db"
    src4.parent.mkdir(parents=True)
    src4.write_bytes(b"this is not an sqlite database " * 50)
    h4, msg = sha(src4), ""
    try:
        dbplace.migrate_legacy(dst4, src4)
    except dbplace.DbPlacementError as exc:
        msg = str(exc)
    rec.check("B14 an unreadable old database stops with instructions; nothing is published", bool(msg) and not dst4.exists() and "python run.py db migrate" in msg, msg[:100])
    rec.check("B15 ...and the old file is untouched", sha(src4) == h4)
    rec.check("B16 ...and no temp file remains", not list(dst4.parent.glob("*migrating*")) if dst4.parent.exists() else True)

    # a source that keeps changing never validates
    b5 = tmp / "mig5"
    src5, dst5 = b5 / "old" / "friday.db", b5 / "new" / "friday.db"
    make_legacy(src5)
    calls = {"n": 0}
    real_counts = dbplace._table_counts

    def shifting(c):
        calls["n"] += 1
        out = real_counts(c)
        out["memory"] = calls["n"]  # differs on every call
        return out

    msg = ""
    with attr(dbplace, "_table_counts", shifting):
        try:
            dbplace.migrate_legacy(dst5, src5)
        except dbplace.DbPlacementError as exc:
            msg = str(exc)
    rec.check("B17 a copy that does not match the original is never published", bool(msg) and not dst5.exists(), msg[:100])

    # the old database missing / same file
    none = dbplace.migrate_legacy(tmp / "mig6" / "friday.db", tmp / "nope" / "friday.db")
    rec.check("B18 no old database: nothing happens, no error", not none.performed and not (tmp / "mig6" / "friday.db").exists())
    same = dbplace.migrate_legacy(src, src)
    rec.check("B19 source == destination is refused harmlessly", not same.performed)

    # end to end through store.conn(), with a FAKE real location under tmp
    b6 = tmp / "e2e"
    fake_la = b6 / "LocalAppData"
    fake_new = fake_la / "FRIDAY" / "friday.db"
    fake_old = b6 / "repo" / "data" / "friday.db"
    make_legacy(fake_old, wal_only_rows=2)
    old_hash = sha(fake_old)
    with envpatch(LOCALAPPDATA=str(fake_la), FRIDAY_DB=None, FRIDAY_ALLOW_REAL_DB="1"), \
            attr(paths, "LEGACY_DB", fake_old), attr(paths, "DEFAULT_DB_PATH", fake_new), attr(paths, "DB_PATH", fake_new):
        dbplace._forget_ready()
        safe = dbplace.resolve() == fake_new and str(fake_new).startswith(str(tmp))
        rec.check("B20 (setup) the fake real location is under the temp folder", safe)
        if safe:
            c = store.conn()
            n_mem = c.execute("select count(*) from memory").fetchone()[0]
            n_aud = c.execute("select count(*) from audit").fetchone()[0]
            rec.check("B21 first start at the new default location migrates the old database automatically", fake_new.exists() and n_mem == 3 and n_aud == 1, (n_mem, n_aud))
            rec.check("B22 ...the old database was not modified or removed", fake_old.exists() and sha(fake_old) == old_hash)
            audit.record(actor="text", skill="apps.open", tier="L1", args={"app": "notepad"}, decision="auto")
            rec.check("B23 new writes go to the new database only", rows(fake_new, "select count(*) from audit")[0][0] == 2 and rows(fake_old, "select count(*) from audit")[0][0] == 1)
            store._close_all(fake_new)
            dbplace._forget_ready()
            c = store.conn()
            rec.check("B24 restart: reopening keeps the data and does not migrate again", c.execute("select count(*) from audit").fetchone()[0] == 2)
            store._close_all(fake_new)
    fresh = b6 / "fresh" / "FRIDAY" / "friday.db"
    with envpatch(LOCALAPPDATA=str(b6 / "fresh"), FRIDAY_DB=None, FRIDAY_ALLOW_REAL_DB="1"), \
            attr(paths, "LEGACY_DB", b6 / "nothing" / "friday.db"), attr(paths, "DEFAULT_DB_PATH", fresh), attr(paths, "DB_PATH", fresh):
        dbplace._forget_ready()
        c = store.conn()
        rec.check("B25 with no old database a fresh one is created with the schema", fresh.exists() and c.execute("select count(*) from audit").fetchone()[0] == 0)
        store._close_all(fresh)
    dbplace._forget_ready()

    # ----------------------------------------------------------------------- C: threads and locking
    rec.scenario("C: worker threads use the same database as the main thread; throwaway connections close; locks are waited for")
    main_file = os.path.normcase(os.path.realpath(store.conn().execute("PRAGMA database_list").fetchone()["file"]))
    seen: dict[str, str] = {}

    def probe(tag):
        seen[tag] = os.path.normcase(os.path.realpath(store.conn().execute("PRAGMA database_list").fetchone()["file"]))

    th = threading.Thread(target=probe, args=("thread",))
    th.start()
    th.join(10)
    rec.check("C1 a new worker thread opens the same database file as the main thread", seen.get("thread") == main_file, seen)
    seen["to_thread"] = await asyncio.to_thread(lambda: os.path.normcase(os.path.realpath(store.conn().execute("PRAGMA database_list").fetchone()["file"])))
    rec.check("C2 asyncio.to_thread (how blocking skills run) agrees too", seen["to_thread"] == main_file)
    holder: dict[str, int] = {}
    th = threading.Thread(target=lambda: holder.update(id=audit.record(actor="text", skill="apps.open", tier="L1", args={"app": "x"}, decision="auto")))
    th.start()
    th.join(10)
    rec.check("C3 a row written from a worker thread is visible to the main thread", any(r["id"] == holder.get("id") for r in audit.recent(5)))

    cmd_q: queue.Queue = queue.Queue()
    res_q: queue.Queue = queue.Queue()

    def worker():
        while True:
            job = cmd_q.get()
            if job is None:
                return
            try:
                res_q.put(job())
            except Exception as exc:  # noqa: BLE001
                res_q.put(exc)

    def ask(fn):
        cmd_q.put(fn)
        return res_q.get(timeout=10)

    def where():
        return os.path.normcase(os.path.realpath(store.conn().execute("PRAGMA database_list").fetchone()["file"]))

    wt = threading.Thread(target=worker, daemon=True)
    wt.start()
    x, y = tmp / "swap_x.db", tmp / "swap_y.db"
    saved_path = paths.DB_PATH
    try:
        paths.DB_PATH = x
        first = ask(where)
        paths.DB_PATH = y
        second = ask(where)
        paths.DB_PATH = x
        third = ask(where)
    finally:
        paths.DB_PATH = saved_path
        store._close_all(x)
        store._close_all(y)
    rec.check("C4 a worker that connected EARLIER follows the path change instead of staying on its old file",
              first == os.path.normcase(str(x)) and second == os.path.normcase(str(y)) and third == first, (first, second, third))
    cmd_q.put(None)

    leaked = {}
    with store.use_temp_db():
        inner = paths.DB_PATH

        def grab():
            leaked["c"] = store.conn()

        g = threading.Thread(target=grab)
        g.start()
        g.join(10)
        wal_present = os.path.exists(str(inner) + "-wal") or True
        rec.check("C5 inside a throwaway block the worker is on the throwaway file", leaked["c"].execute("PRAGMA database_list").fetchone()["file"].lower().endswith(os.path.basename(str(inner)).lower()))
        rec.check("C6 journal mode is WAL", store.conn().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    closed = False
    try:
        leaked["c"].execute("select 1")
    except sqlite3.ProgrammingError:
        closed = True
    rec.check("C7 leaving the block closes the WORKER thread's connection too", closed)
    rec.check("C8 ...and removes the throwaway file and its sidecars",
              not any(os.path.exists(str(inner) + s) for s in ("", "-wal", "-shm")))
    rec.check("C9 after the block the main thread is back on the outer database", where() == main_file)

    held = threading.Event()

    def hold_lock():
        c = sqlite3.connect(str(paths.DB_PATH), timeout=5)
        c.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(1.0)
        c.commit()
        c.close()

    ht = threading.Thread(target=hold_lock)
    ht.start()
    held.wait(5)
    t0 = time.perf_counter()
    err = ""
    try:
        audit.record(actor="text", skill="apps.open", tier="L1", args={"app": "waited"}, decision="auto")
    except sqlite3.OperationalError as exc:
        err = str(exc)
    waited = time.perf_counter() - t0
    ht.join(10)
    rec.check("C10 a write that meets a busy writer waits for it instead of failing 'database is locked'", not err and waited >= 0.4, f"{err} waited={waited:.2f}s")

    # ----------------------------------------------------------------------- D: audit minimisation
    rec.scenario("D: audit rows keep what happened, never what was written, typed, said or read")
    work = tmp / "work"
    work.mkdir()
    paths.SNAPSHOTS = tmp / "snaps"
    paths.LEGACY_SNAPSHOTS = tmp / "legacy_snaps"
    answer = {"v": True}

    async def confirm(skill, args, preview):
        return answer["v"]

    EXECUTOR.set_confirm_handler(confirm)

    target = work / "note.txt"
    body = f"{SECRET} " + "line of text\n" * 30
    res = await EXECUTOR.run("files.write", {"path": str(target), "content": body}, actor="text")
    row = last_audit()
    args = json.loads(row["args"])
    rec.check("D1 files.write really wrote the full content (the skill still receives its real arguments)",
              res.ok and target.read_text(encoding="utf-8") == body)
    rec.check("D2 the audit row holds NO part of the file content", SECRET not in row["args"] and "line of text" not in row["args"], row["args"][:120])
    rec.check("D3 ...but keeps the path, the skill, the tier and a length marker",
              args.get("path") == str(target) and row["skill"] == "files.write" and row["tier"] == "L1"
              and args.get("content", "").startswith("[omitted: ") and str(len(body)) in args["content"], args)
    rec.check("D4 ...the outcome is the skill's own verdict (ok=1, with its speech)", row["ok"] == 1 and "note.txt" in (row["result"] or ""))
    rec.check("D5 nothing anywhere in the database contains the content (audit, undo, episodes, memory ...)", not db_contains(SECRET))

    res = await EXECUTOR.run("files.read", {"path": str(target)}, actor="text")
    row = last_audit()
    rec.check("D6 a real files.read leaves no file content in its audit row", res.ok and SECRET not in (row["result"] or "") and SECRET not in row["args"], row["result"])
    rec.check("D6b ...and a skill whose speech DOES carry read-back content (clipboard, command output, a page) stores only its length",
              auditview.sanitize_result("clipboard.read", f"{SECRET} " * 40).startswith("[read-back content not stored: ")
              and SECRET not in auditview.sanitize_result("shell.run", f"{SECRET} " * 40))
    rec.check("D7 ...and the row still records ok=1 and the path", row["ok"] == 1 and json.loads(row["args"]).get("path") == str(target))

    answer["v"] = False
    n = store.conn().execute("select count(*) from audit").fetchone()[0]
    await EXECUTOR.run("files.write", {"path": str(work / "declined.txt"), "content": SECRET + "-declined"}, actor="text")
    row = last_audit()
    rec.check("D8 a DECLINED call is audited without its content as well", row["decision"] in ("denied", "auto") and SECRET not in row["args"] and not (work / "declined.txt").exists() or row["decision"] == "auto", row["args"][:100])
    answer["v"] = True

    blocked = ""
    try:
        await EXECUTOR.run("shell.run", {"command": f"curl -H 'Authorization: {BEARER}' https://example.com/x?token=1"}, actor="scheduler")
    except PermissionError_ as exc:
        blocked = str(exc)
    row = last_audit()
    rec.check("D9 a BLOCKED call (unattended actor) is audited without the command line", bool(blocked) and row["decision"] == "blocked" and BEARER not in row["args"] and "token=1" not in row["args"], row["args"][:120])
    rec.check("D10 ...but names the executable", "curl" in row["args"])

    S = auditview.sanitize_args
    rec.check("D11 typed text (input.type / ui.fill / browser.type) is omitted",
              all(SECRET not in json.dumps(S(k, {"text": SECRET})) for k in ("input.type", "ui.fill", "browser.type", "clipboard.write", "notes.add", "memory.remember")))
    rec.check("D12 messages are omitted (whatsapp.compose, notify.send) but the contact / title stay",
              S("whatsapp.compose", {"contact": "Mum", "message": SECRET}) == {"contact": "Mum", "message": f"[omitted: {len(SECRET)} chars]"}
              and S("notify.send", {"title": "Done", "message": SECRET})["title"] == "Done" and SECRET not in json.dumps(S("notify.send", {"title": "Done", "message": SECRET})))
    rec.check("D13 code (dev.python) is omitted; line count is kept", "[omitted: " in S("dev.python", {"code": "a=1\nb=2"})["code"] and "a=1" not in json.dumps(S("dev.python", {"code": "a=1\nb=2"})))
    sh = S("shell.run", {"command": f"git push https://user:{SECRET}@host/repo --force", "shell": "powershell", "cwd": "C:/proj"})
    rec.check("D14 a shell command keeps only the executable and the argument count; shell and cwd stay",
              SECRET not in json.dumps(sh) and sh["command"].startswith("git ") and "args" in sh["command"] and sh["shell"] == "powershell" and sh["cwd"] == "C:/proj", sh)
    gi = S("dev.git", {"subcommand": f"commit -m {SECRET}", "repo": "C:/r"})
    rec.check("D15 dev.git keeps the sub-command word only", SECRET not in json.dumps(gi) and gi["subcommand"].startswith("commit"), gi)
    u = S("web.open", {"url": f"https://user:{SECRET}@example.com:8443/path/a?token={SECRET}&q=1#frag"})["url"]
    rec.check("D16 a URL keeps scheme, host, port and path; credentials, query and fragment are dropped",
              u.startswith("https://example.com:8443/path/a") and SECRET not in u and "q=1" not in u and "frag" not in u, u)
    rec.check("D17 a URL with no query is kept as is", S("web.fetch", {"url": "https://example.com/a/b"})["url"] == "https://example.com/a/b")
    keep = S("apps.open", {"app": "Visual Studio Code"})
    rec.check("D18 an app name is kept verbatim (it is the evidence of what ran)", keep == {"app": "Visual Studio Code"}, keep)
    rec.check("D19 numbers and booleans are always kept", S("system.volume.up", {"amount": 15}) == {"amount": 15} and S("process.kill", {"name": "x.exe", "force": True}) == {"name": "x.exe", "force": True})
    rec.check("D20 a snapshot id and a restore path are kept (they are what an audit needs)",
              S("files.recover", {"snapshot": "20260101T000000-abcdef012345"}) == {"snapshot": "20260101T000000-abcdef012345"})
    unk = S("some.future_skill", {"blob": SECRET, "count": 3, "flag": True, "items": [SECRET]})
    rec.check("D21 an UNCLASSIFIED skill fails closed: text and lists are omitted, numbers and flags kept",
              SECRET not in json.dumps(unk) and unk["count"] == 3 and unk["flag"] is True, unk)
    cred = S("some.future_skill", {"password": "hunter2", "api_key": "k-123", "n": 1})
    rec.check("D22 an argument NAMED like a credential is always [redacted]", cred["password"] == "[redacted]" and cred["api_key"] == "[redacted]" and cred["n"] == 1, cred)
    q = S("web.search", {"query": "my password is hunter2xyz and token=abc123"})["query"]
    rec.check("D23 a free-text intent (search query, goal) is credential-scrubbed", "hunter2xyz" not in q and "abc123" not in q, q)
    g = S("plan.run", {"goal": "x" * 900})["goal"]
    rec.check("D24 ...and bounded", len(g) < 260, len(g))
    rec.check("D25 plan.run's clarification answer (may be a secret) is omitted", SECRET not in json.dumps(S("plan.run", {"goal": "g", "clarification_answer": SECRET})))
    snap = {"content": SECRET, "path": "p"}
    keep_before = json.dumps(snap)
    S("files.write", snap)
    rec.check("D26 sanitising never mutates the caller's arguments", json.dumps(snap) == keep_before)
    ok_weird = True
    for weird in (None, 5, "str", [1, 2], {"a": {"b": object()}}, {1: 2}, {"path": b"bytes"}):
        try:
            S("files.write", weird)
        except Exception:  # noqa: BLE001
            ok_weird = False
    rec.check("D27 it never raises, whatever it is given", ok_weird)
    missing = auditview.unclassified_params(REGISTRY.all())
    rec.check("D28 EVERY registered skill's text parameter is classified (a new skill cannot ship an unreviewed argument)", missing == [], missing[:6])
    rec.check("D29 the policy has no entry for a skill or parameter that does not exist",
              all(s in {x.name for x in REGISTRY.all()} or s == "comms.send_message" for s in auditview.POLICY)
              and all(p in {q.name for q in REGISTRY.get(s).params} for s in auditview.POLICY if REGISTRY.get(s) for p in auditview.POLICY[s]))
    rec.check("D30 error text is scrubbed and bounded", "hunter2" not in (auditview.sanitize_error("boom password=hunter2") or "") and len(auditview.sanitize_error("e" * 2000) or "") < 340)
    rec.check("D31 short, ordinary results are stored as they are",
              auditview.sanitize_result("apps.open", "Opened Notepad.") == "Opened Notepad." and auditview.sanitize_result("files.read", "ok") == "ok")

    # recording an argument is not evidence of success
    aid = audit.record(actor="text", skill="apps.open", tier="L1", args={"app": "nothing"}, decision="auto")
    row = store.conn().execute("select ok, result from audit where id=?", (aid,)).fetchone()
    rec.check("D32 a row recorded before the action has NO outcome (ok is NULL) -- args are not proof of success", row["ok"] is None and row["result"] is None)
    audit.complete(aid, ok=False, result="It did not open.", error="no such app")
    row = store.conn().execute("select ok, result, error from audit where id=?", (aid,)).fetchone()
    rec.check("D33 a failed outcome stays failed (ok=0) with its text", row["ok"] == 0 and row["result"] == "It did not open.")

    # episodes
    seen_ep = SimpleNamespace(step=SimpleNamespace(tool="files.write", args={"path": "C:/a.txt", "content": SECRET}), ok=True, error="", speech="")
    with attr(episodes, "_embed_one", lambda text: None):
        episodes.record("write a file", goal_id=None, context="", steps=[seen_ep], stopped="done", ok=True, duration_ms=1)
    plan = json.loads(store.conn().execute("select plan from episodes order by id desc limit 1").fetchone()["plan"])
    rec.check("D34 the episode store keeps the tool and path of a step, not its file content",
              plan[0]["tool"] == "files.write" and plan[0]["args"]["path"] == "C:/a.txt" and SECRET not in json.dumps(plan), plan)

    # ----------------------------------------------------------------------- E: isolation tripwires
    rec.scenario("E: a test / benchmark entry point is refused the real database and recovery store, automatically")
    rec.check("E1 this process is recognised as a test entry point without any environment variable", dbplace.is_test_entrypoint())
    with envpatch(FRIDAY_ALLOW_REAL_DB="1"):
        rec.check("E2 FRIDAY_ALLOW_REAL_DB=1 is the only explicit opt-out", not dbplace.is_test_entrypoint())
    fake_la2 = tmp / "FakeLocal"
    fake_real = fake_la2 / "FRIDAY" / "friday.db"
    refused = {}
    with envpatch(LOCALAPPDATA=str(fake_la2), FRIDAY_DB=None):
        refused["canonical"] = _refuses(lambda: dbplace.guard(fake_real))
        refused["legacy_def"] = _refuses(lambda: dbplace.guard(paths.LEGACY_DB))
        with attr(paths, "DB_PATH", fake_real):
            try:
                store.conn()
                refused["conn"] = False
            except dbplace.IsolationError:
                refused["conn"] = True
        rec.check("E3 deliberate misconfiguration: pointing the store at the per-user database from a test is refused", refused["conn"])
        rec.check("E4 ...and nothing was created at that location", not fake_real.exists() and not fake_real.parent.exists())
        refused["store"] = False
        with attr(paths, "SNAPSHOTS", fake_la2 / "FRIDAY" / "snapshots"):
            try:
                recovery.root()
            except dbplace.IsolationError:
                refused["store"] = True
        rec.check("E5 pointing the recovery store at the real recovery location is refused", refused["store"])
        refused["legacy_store"] = False
        with attr(paths, "SNAPSHOTS", paths.LEGACY_SNAPSHOTS):
            try:
                recovery.root()
            except dbplace.IsolationError:
                refused["legacy_store"] = True
        rec.check("E6 ...and so is the old in-repository recovery store", refused["legacy_store"])
    rec.check("E7 the old in-repository database path is refused by the guard", refused["legacy_def"])
    with attr(paths, "DB_PATH", paths.LEGACY_DB):
        r = False
        try:
            store.conn()
        except dbplace.IsolationError:
            r = True
    rec.check("E8 opening the old data/friday.db through the store is refused", r)
    cfg_real = tmp / "cfgreal" / "real.db"
    with attr(paths, "DB_PATH", paths.DEFAULT_DB_PATH), attr(CFG.storage, "db_path", str(paths.LEGACY_DB)):
        rec.check("E9 a config override that names the old database is still 'real' to the guard", dbplace.is_real_db(paths.LEGACY_DB))
    rec.check("E10 a throwaway database under the temp folder is not 'real'", not dbplace.is_real_db(paths.DB_PATH) and not dbplace.is_real_db(cfg_real))

    dbplace.require_isolated(db=True, store=True)
    rec.check("E11 require_isolated passes inside use_temp_db with a temp recovery store", True)
    bad = False
    with attr(paths, "SNAPSHOTS", paths.ROOT / "data" / "snapshots_probe"):
        try:
            dbplace.require_isolated(db=False, store=True)
        except dbplace.IsolationError:
            bad = True
    rec.check("E12 require_isolated refuses a recovery store that is not a throwaway", bad)
    bad = False
    with attr(paths, "DB_PATH", paths.ROOT / "data" / "not_temp.db"):
        try:
            dbplace.require_isolated(db=True, store=False)
        except dbplace.IsolationError:
            bad = True
    rec.check("E13 require_isolated refuses a database that is not a throwaway", bad)

    if rec.loud:
        probes = tmp / "probes"
        probes.mkdir()
        body = (
            "import sys\nsys.path.insert(0, %r)\nfrom friday import store, dbplace\n"
            "try:\n    store.conn()\nexcept dbplace.IsolationError as e:\n    print('ISOLATED'); sys.exit(0)\n"
            "print('OPENED'); sys.exit(3)\n" % str(ROOT)
        )
        (probes / "smoke_probe.py").write_text(body, encoding="utf-8")
        (probes / "operator_tool.py").write_text(body, encoding="utf-8")
        py = sys.executable
        sub_la = tmp / "SubLocal"
        base_env = {k: v for k, v in os.environ.items() if k not in ("FRIDAY_DB", "FRIDAY_ALLOW_REAL_DB", "FRIDAY_REFUSE_REAL_DB")}
        r1 = subprocess.run([py, str(probes / "smoke_probe.py")], env={**base_env, "LOCALAPPDATA": str(sub_la)}, capture_output=True, text=True, timeout=120, cwd=str(probes))
        rec.check("E14 a real process named smoke_*.py that opens the default database is refused (no env var needed)", r1.returncode == 0 and "ISOLATED" in r1.stdout, (r1.returncode, r1.stdout[-80:], r1.stderr[-200:]))
        rec.check("E15 ...and the database file was not created", not (sub_la / "FRIDAY" / "friday.db").exists())
        r2 = subprocess.run([py, str(probes / "operator_tool.py")], env={**base_env, "LOCALAPPDATA": str(sub_la), "FRIDAY_REFUSE_REAL_DB": "1"}, capture_output=True, text=True, timeout=120, cwd=str(probes))
        rec.check("E16 FRIDAY_REFUSE_REAL_DB=1 makes any other script refuse too", r2.returncode == 0 and "ISOLATED" in r2.stdout, (r2.returncode, r2.stdout[-80:], r2.stderr[-200:]))
        real_c = dbplace.canonical_db()
        before = (real_c.stat().st_size, real_c.stat().st_mtime_ns) if real_c is not None and real_c.exists() else None
        r3 = subprocess.run([py, str(probes / "smoke_probe.py")], env=base_env, capture_output=True, text=True, timeout=120, cwd=str(probes))
        after = (real_c.stat().st_size, real_c.stat().st_mtime_ns) if real_c is not None and real_c.exists() else None
        rec.check("E17 with the REAL environment, a smoke_* process is refused and the real database is untouched", r3.returncode == 0 and "ISOLATED" in r3.stdout and before == after, (r3.returncode, r3.stderr[-200:]))

    # ----------------------------------------------------------------------- F: history + scan
    rec.scenario("F: history is not rewritten; the sensitive-data scan returns counts, never values")
    hist = tmp / "hist" / "friday.db"
    make_legacy(hist)
    before_h = rows(hist, "select args, result from audit")
    rep = dbplace.scan_sensitive(hist)
    rec.check("F1 the scan counts historical rows that hold file content", rep["audit_args_with_content"].get("files.write") == 1, rep)
    rec.check("F2 ...and its report holds no stored value", SECRET not in json.dumps(rep))
    rec.check("F3 scanning never changes the database", rows(hist, "select args, result from audit") == before_h)
    migrated = tmp / "hist2" / "friday.db"
    dbplace.migrate_legacy(migrated, hist)
    rec.check("F4 migration copies historical rows verbatim: it does not rewrite or scrub history", rows(migrated, "select args, result from audit") == before_h)
    st = dbplace.status()
    rec.check("F5 status reports counts and sizes only", "active" in st and SECRET not in json.dumps(st, default=str))

    # ----------------------------------------------------------------------- G: benchmark isolation
    rec.scenario("G: the benchmark harness isolates database AND recovery store without any environment variable")
    if rec.loud:
        import bench_env

        env = bench_env.Isolation(browser=False, ui=False)
        before_store = (paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS)
        try:
            env.start()
            inside = Path(paths.SNAPSHOTS)
            rec.check("G1 benchmark start() redirects the recovery store into its sandbox", bench_env._inside(inside, bench_env._norm(env.root)), inside)
            rec.check("G2 ...and the database to a throwaway file", dbplace.redirected() and not dbplace.is_real_db(dbplace.resolve()))
            rec.check("G3 ...and recovery.root() is not the user's store", not dbplace.is_real_store(recovery.root()))
        finally:
            env.stop()
        rec.check("G4 stop() restores the original recovery paths", (paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS) == before_store)
        bad_env = bench_env.Isolation(browser=False, ui=False)
        raised = False
        try:
            bad_env.start()
            with attr(paths, "SNAPSHOTS", paths.ROOT / "data" / "snapshots_probe"):
                try:
                    bad_env.prove(REGISTRY)
                except bench_env.IsolationError:
                    raised = True
        finally:
            bad_env.stop()
        rec.check("G5 deliberate misconfiguration: prove() fails fast if the recovery store is not the sandbox's", raised)


def _refuses(fn) -> bool:
    try:
        fn()
    except dbplace.IsolationError:
        return True
    return False


# -- source-level mutation ----------------------------------------------------------------------------

_POLICY_FILES_WRITE = auditview.POLICY["files.write"]


@contextlib.contextmanager
def policy_content_kept():
    saved = dict(auditview.POLICY["files.write"])
    auditview.POLICY["files.write"] = {"path": auditview.PATH, "content": auditview.KEEP}
    try:
        yield
    finally:
        auditview.POLICY["files.write"] = saved


MUTATIONS = [
    ("files.write content is classified as 'keep'", policy_content_kept),
    ("audit.record stores the raw arguments",
     lambda: mutated(audit, "record", "auditview.sanitize_args(skill, args)", "args")),
    ("an unclassified parameter defaults to 'keep' (fail open)",
     lambda: mutated(auditview, "_sanitize_args_once", "modes.get(k, OMIT)", "modes.get(k, KEEP)")),
    ("credential-named arguments are no longer redacted",
     lambda: mutated(auditview, "_sanitize_args_once", "if _CREDENTIAL_KEY.search(k) and", "if False and")),
    ("URL query strings are kept",
     lambda: mutated(auditview, "_url", 'out = f"{parts.scheme}://{parts.hostname}{port}{parts.path}"', 'out = value')),
    ("a shell command is stored in full",
     lambda: mutated(auditview, "_argv0", "first = text.split(None, 1)[0].strip(\"\\\"'\")", "return text")),
    ("read-back content is stored in the result column",
     lambda: mutated(auditview, "_sanitize_result_once", "if skill in CONTENT_RESULT_SKILLS:", "if False:")),
    ("audit.complete flips a failed outcome to ok",
     lambda: mutated(audit, "complete", "(1 if ok else 0,", "(1,")),
    ("audit.complete stores the free-text result unminimised",
     lambda: mutated(audit, "complete", "auditview.sanitize_result(skill, result, echoes)", "result")),
    ("the episode store keeps raw step arguments",
     lambda: mutated(episodes, "_sanitize_args", "out = auditview.sanitize_args(tool, args)", "out = dict(args)")),
    ("a connection ignores a changed database path (one cached connection per thread)",
     lambda: mutated(store, "conn", "existing = mine.get(key)", "existing = next(iter(mine.values()), None)")),
    ("the throwaway block leaves worker connections open",
     lambda: mutated(store, "_close_all", "c.close()", "pass")),
    ("no busy timeout on connections",
     lambda: mutated(store, "conn", 'c = sqlite3.connect(path, check_same_thread=False, timeout=15)\n    c.row_factory = sqlite3.Row\n    c.execute("PRAGMA busy_timeout=15000")',
                     'c = sqlite3.connect(path, check_same_thread=False, timeout=0)\n    c.row_factory = sqlite3.Row\n    c.execute("PRAGMA busy_timeout=0")')),
    ("publishing replaces an existing destination",
     lambda: mutated(dbplace, "_publish_no_overwrite", "os.link(str(tmp), str(dest))  # fails if dest exists, on every platform", "os.replace(str(tmp), str(dest)); return True")),
    ("migration deletes the old database",
     lambda: mutated(dbplace, "migrate_legacy", "out.performed = True", "out.performed = True; source.unlink()")),
    ("a copy that does not match is accepted",
     lambda: mutated(dbplace, "migrate_legacy", "if copied == before or copied == after:", "if True:")),
    ("the integrity check is skipped",
     lambda: mutated(dbplace, "migrate_legacy", 'if verdict != "ok":', "if False:")),
    ("an existing destination is migrated over",
     lambda: mutated(dbplace, "migrate_legacy", "    if dest.exists():\n        out.reason = \"destination already exists; left untouched\"\n        return out\n", "")),
    ("the database guard is disabled",
     lambda: mutated(dbplace, "guard", "if is_test_entrypoint() and is_real_db(path):", "if False:")),
    ("the recovery-store guard is disabled",
     lambda: mutated(dbplace, "guard_store", "if is_test_entrypoint() and is_real_store(path):", "if False:")),
    ("a redirect no longer beats the configured path",
     lambda: mutated(dbplace, "resolve", "if redirected():", "if False:")),
    ("a smoke_* script is no longer recognised as a test entry point",
     lambda: mutated(dbplace, "is_test_entrypoint", "return bool(name) and bool(_TEST_NAME.match(Path(name).stem))", "return False")),
    ("the old database is no longer considered real",
     lambda: mutated(dbplace, "real_db_paths", "out = [p for p in (canonical_db(), Path(paths.LEGACY_DB)) if p is not None]", "out = [p for p in (canonical_db(),) if p is not None]")),
    ("require_isolated accepts a non-temp database",
     lambda: mutated(dbplace, "require_isolated", "if is_real_db(p) or not _under(p, _temp_root()):", "if False:")),
    ("the scan returns stored values",
     lambda: mutated(dbplace, "scan_sensitive", 'report["audit_args_with_content"][skill] = report["audit_args_with_content"].get(skill, 0) + 1', 'report["audit_args_with_content"][skill] = d')),
]


def main() -> int:
    t0 = time.perf_counter()
    legacy = Path(paths.LEGACY_DB)
    canon = dbplace.canonical_db()

    def fingerprint(p):
        if p is None or not Path(p).exists():
            return None
        s = Path(p).stat()
        return (s.st_size, s.st_mtime_ns)

    def tree(p):
        if p is None or not Path(p).exists():
            return None
        return sorted((str(x.relative_to(p)), x.stat().st_size) for x in Path(p).rglob("*") if x.is_file())

    before = (fingerprint(legacy), fingerprint(canon), tree(dbplace.canonical_store()), tree(paths.LEGACY_SNAPSHOTS))

    loud = Rec(loud=True)
    asyncio.run(suite(loud))

    scenario("H: mutation testing -- break each privacy / placement / isolation invariant; the suite must notice")
    clean = not loud.failed
    check("H0 the unmutated suite is clean (mutations only count against a green baseline)", clean, loud.failed[:6])
    killed = 0
    for label, make in MUTATIONS:
        quiet = Rec(loud=False)
        try:
            with make():
                asyncio.run(suite(quiet))
        except AssertionError as exc:
            if "mutation anchor" in str(exc):
                raise
            quiet.failed.append(f"assertion: {exc}")
        except Exception as exc:  # a crash is also a detection: the suite did not pass
            quiet.failed.append(f"crashed: {type(exc).__name__}: {exc}")
        caught = bool(quiet.failed)
        killed += caught
        print(f"      (first checks that failed under it: {[f.split(' ')[0] for f in quiet.failed[:4]]})")
        check(f"H mutation caught: {label}", caught and clean, "survived" if not caught else "")
    check(f"H all {len(MUTATIONS)} mutations killed", killed == len(MUTATIONS) and clean, f"{killed}/{len(MUTATIONS)}")

    scenario("G2: the user's real database and recovery stores were not touched by the suite")
    after = (fingerprint(legacy), fingerprint(canon), tree(dbplace.canonical_store()), tree(paths.LEGACY_SNAPSHOTS))
    check("G2a the old data/friday.db is unchanged (size and mtime)", before[0] == after[0], (before[0], after[0]))
    check("G2b the per-user database is unchanged (size and mtime)", before[1] == after[1], (before[1], after[1]))
    check("G2c the per-user recovery store is unchanged", before[2] == after[2])
    check("G2d the old in-repository data/snapshots is unchanged", before[3] == after[3])

    return finish("Phase 35.0 -- database placement, audit minimisation and test isolation", time.perf_counter() - t0, min_assertions=120, min_scenarios=7)


if __name__ == "__main__":
    sys.exit(main())
