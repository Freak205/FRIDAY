"""Phase 36 -- reversible, preview-first scrubbing of historical audit / episode content (`friday.dbscrub`).

Everything runs on THROWAWAY SQLite files under a temp folder with SYNTHETIC rows (unmistakable `CANARY-...`
strings stand for the sensitive values; `RETAINED-...` strings are in stores the scrub must not touch). The
user's real databases and recovery stores are never opened: section H fingerprints them (size + mtime, via
`stat` only) before and after the whole run. Because the file is named `smoke_*` the automatic real-database
tripwire (`dbplace.guard`) is active for the whole process.

What it pins:
  A  preview: read-only by default (file bytes + logical digest), counts-only output without any stored value,
     independent per target, state-bound confirmation code, target resolution (aliases, synced folders)
  B  gating: no confirm / wrong code / other target's code / stale code / bad target -> nothing changes, no
     backup directory; CLI exit codes; the other database is never touched; nothing at startup imports dbscrub
  C  policy: sensitive text gone from every column AND from the raw file bytes; everything else preserved
     (ids, outcomes, FKs, schema, retained stores); markers equal what the live code writes
  D  idempotence + fixed points of the auditview functions; the live audit path stores no body
  E  backup: location rules, uniqueness, no-overwrite, read-only, manifest, verification, restore
  F  failure handling: a fault at every stage leaves the database unchanged or scrubbed-and-verified; a real
     hard kill (os._exit) mid-scrub and after commit
  G  locking and WAL/SHM: a second connection makes apply refuse; stale sidecars are left to SQLite
  H  the user's real data was not touched
  I  mutation testing: break the safety invariants in the source of dbscrub / the CLI; the suite must notice

Output never prints a canary value, only check names.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import iso_env  # noqa: E402
from friday import audit, auditview, dbplace, dbscrub, paths, store  # noqa: E402

PY = sys.executable


# --------------------------------------------------------------------------------------------- scaffolding

class Rec:
    """Collects checks. `loud` prints; mutation runs are silent and only record what failed."""

    def __init__(self, loud: bool, *, mutation: bool = False) -> None:
        self.loud = loud
        self.mutation = mutation  # True: never touch anything that resembles the user's real data
        self.failed: list[str] = []
        self.n = 0
        self.skipped: list[str] = []

    def check(self, label: str, cond: object, detail: object = "") -> bool:
        self.n += 1
        ok = bool(cond)
        if not ok:
            self.failed.append(label)
        if self.loud:
            extra = ""
            if not ok and detail:
                extra = " -- " + str(detail).replace("CANARY-", "C~")[:200]
            print(f"  {'OK  ' if ok else 'MISS'} {label}{extra}")
        return ok

    def section(self, title: str) -> None:
        if self.loud:
            print(f"\n--- {title} ---\n")

    def skip(self, label: str, why: str) -> None:
        self.skipped.append(label)
        if self.loud:
            print(f"  SKIP {label} ({why})")


def rm_tree(p: Path) -> None:
    """rmtree that clears the read-only bit dbscrub puts on backups."""
    def onerr(func, path, _exc):
        try:
            os.chmod(path, 0o666)
            func(path)
        except OSError:
            pass

    shutil.rmtree(p, onerror=onerr)
    if Path(p).exists():
        shutil.rmtree(p, ignore_errors=True)


@contextlib.contextmanager
def workdir():
    t = Path(tempfile.mkdtemp(prefix="friday_dbscrub_"))
    try:
        yield t
    finally:
        try:
            rm_tree(t)
        except Exception:  # noqa: BLE001
            pass


@contextlib.contextmanager
def attr(owner, name, value):
    saved = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, saved)


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


def fsha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def sidecars(p: Path) -> dict[str, bool]:
    return {s: Path(str(p) + s).exists() for s in ("-wal", "-shm", "-journal")}


def folder_listing(p: Path) -> list[str]:
    return sorted(str(x.relative_to(p)) for x in Path(p).rglob("*"))


def logical(p: Path) -> dict:
    """The dbscrub snapshot (counts, schema, per-table digests, ids, integrity) of a database file, read-only."""
    c = dbplace._open_ro(Path(p))
    try:
        return dbscrub.snapshot(c)
    finally:
        c.close()


def logical_ro_sorted(p: Path) -> str:
    return json.dumps(logical(p), sort_keys=True)


def table_rows(p: Path, sql: str) -> list:
    c = sqlite3.connect(str(p))
    try:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql).fetchall()]
    finally:
        c.close()


def all_values(p: Path, tables: tuple[str, ...] | None = None):
    c = sqlite3.connect(str(p))
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for n in names:
            if tables is not None and n not in tables:
                continue
            for row in c.execute(f'SELECT * FROM "{n}"'):
                for v in row:
                    yield n, v
    finally:
        c.close()


def db_contains(p: Path, needle: str, tables: tuple[str, ...] | None = None) -> bool:
    nb = needle.encode()
    for _t, v in all_values(p, tables):
        if isinstance(v, bytes):
            if nb in v:
                return True
        elif isinstance(v, str) and needle in v:
            return True
    return False


def raw_contains(p: Path, needle: str) -> bool:
    nb = needle.encode()
    for suffix in ("", "-wal", "-shm", "-journal"):
        f = Path(str(p) + suffix)
        if f.exists() and nb in f.read_bytes():
            return True
    return False


# --------------------------------------------------------------------------------------------- fixtures

class Fix:
    def __init__(self, path: Path, tag: str) -> None:
        self.path = path
        self.tag = tag
        self.audit: list[dict] = []  # original rows as inserted (id, skill, args_raw, args_obj, result, error, ok, ...)
        self.removed: list[str] = []  # canary strings that MUST be gone after a scrub
        self.retained: list[str] = []  # strings in stores the scrub must not touch
        self.plain_audit_ids: list[int] = []  # rows that were already in minimal form
        self.undo_audit_id = 0
        self.audit_ids: dict[str, int] = {}
        self.ep_ids: dict[str, int] = {}
        self.changing_audit = 0
        self.changing_episodes = 0


def build_db(path: Path, tag: str = "7f3a", *, small: bool = False) -> Fix:
    """A throwaway database with the REAL schema (store.SCHEMA + MIGRATIONS) and synthetic rows only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fx = Fix(path, tag)
    t = tag
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(store.SCHEMA)
    for table, column, statement in store.MIGRATIONS:  # what store._migrate does, without its log lines
        if column not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(statement)
    c.execute("PRAGMA foreign_keys=ON")

    MSG = f"CANARY-MSG-{t} hello how are you today"
    MEM = f"CANARY-MEM-{t} my locker code is on the fridge"
    CMSG = f"CANARY-CMSG-{t} see you at nine"
    NMSG = f"CANARY-NOTIFY-{t} the invoice is overdue"
    TYPED = f"CANARY-TYPED-{t} hunter2hunter2"
    TYPED2 = f"CANARY-TYPED2-{t} into the search box"
    CODE = f"print('CANARY-CODE-{t}')\nsecret = 1"
    STEPS = [f"open CANARY-RSTEP-{t}-one", f"type CANARY-RSTEP-{t}-two"]
    CMD = f"curl -H 'X-Auth: CANARY-CMD-{t}' https://x.test/a"
    URLQ = f"https://example.com/p/a?token=CANARY-URLTOKEN-{t}&q=1#CANARY-FRAG-{t}"
    URLQ2 = f"https://example.com/q/b?sid=CANARY-EPQ-{t}"
    fx.removed = [
        f"CANARY-MSG-{t}", f"CANARY-MEM-{t}", f"CANARY-CMSG-{t}", f"CANARY-NOTIFY-{t}", f"CANARY-TYPED-{t}",
        f"CANARY-TYPED2-{t}", f"CANARY-CODE-{t}", f"CANARY-RSTEP-{t}", f"CANARY-CMD-{t}", f"CANARY-URLTOKEN-{t}",
        f"CANARY-FRAG-{t}", f"CANARY-EPQ-{t}", f"CANARY-OUT-{t}", f"CANARY-PAGE-{t}", f"CANARY-READ-{t}",
        f"CANARY-GOAL-{t}", f"CANARY-TESTSK-{t}", f"CANARY-KEY-{t}", f"CANARY-RAWARGS-{t}", f"CANARY-LONG-{t}",
        f"CANARY-LIST-{t}", f"CANARY-PW-{t}", f"CANARY-CTXQ-{t}", f"CANARY-PLAN-{t}",
    ]

    specs: list[tuple] = [
        # key, skill, tier, args (dict | raw str), ok, result, error, decision
        ("wa_ok", "whatsapp.compose", "L2", {"contact": "Sam", "message": MSG}, 1, f"Composed '{MSG}' to Sam.", None, "confirmed"),
        ("wa_fail", "whatsapp.compose", "L2", {"contact": "Sam", "message": MSG}, 0, "Could not send.", f"WhatsApp refused: '{MSG}'", "confirmed"),
        ("wa_pending", "whatsapp.compose", "L2", {"contact": "Sam", "message": MSG}, None, None, None, "confirmed"),
        ("mem", "memory.remember", "L1", {"text": MEM}, 1, f"I will remember: {MEM}", None, "auto"),
        ("comms", "comms.send_message", "L2", {"contact": "Mum", "text": CMSG}, 1, f"Sent '{CMSG}' to Mum", None, "confirmed"),
        ("notify", "notify.send", "L1", {"title": "Done", "message": NMSG}, 1, f"Notified: {NMSG}", None, "auto"),
        ("itype", "input.type", "L2", {"text": TYPED}, 1, f"Typed {TYPED}.", None, "confirmed"),
        ("btype", "browser.type", "L2", {"field": "Search", "text": TYPED2}, 1, f"Typed '{TYPED2}' into Search.", None, "confirmed"),
        ("py", "dev.python", "L3", {"code": CODE}, 1, f"stdout: CANARY-OUT-{t} " + "z" * 120, None, "confirmed"),
        ("routine", "routine.create", "L2", {"name": "morning", "steps": STEPS}, 1, f"Routine saved with steps: {STEPS[0]}; {STEPS[1]}", None, "confirmed"),
        ("shell", "shell.run", "L3", {"command": CMD, "shell": "powershell", "cwd": "C:/proj"}, 1, f"CANARY-OUT-{t} " + "o" * 120, None, "confirmed"),
        ("webopen", "web.open", "L1", {"url": URLQ}, 1, f"Opened {URLQ}", None, "auto"),
        ("bopen", "browser.open", "L1", {"url": URLQ}, 0, "Could not open the page.", f"net::ERR_FAILED for {URLQ}", "auto"),
        ("fetch", "web.fetch", "L1", {"url": URLQ}, 1, f"CANARY-PAGE-{t} " + "p" * 200, None, "auto"),
        ("fread", "files.read", "L0", {"path": "C:/x/a.txt"}, 1, f"CANARY-READ-{t} " + "line " * 60, None, "auto"),
        ("plan", "plan.run", "L1", {"goal": "tidy " + "g" * 300 + f" CANARY-GOAL-{t}"}, 1, "Done.", None, "auto"),
        ("harm1", "apps.open", "L1", {"app": "notepad"}, 1, "Opened Notepad.", None, "auto"),
        ("harm2", "system.volume.up", "L1", {"amount": 5}, 1, "Volume up.", None, "auto"),
        ("testsk", "test.fixture", "L0", {"text": f"CANARY-TESTSK-{t} body", "n": 3}, 1, f"CANARY-TESTSK-{t} body done", None, "auto"),
        ("cred", "apps.open", "L1", {"app": "x", "api_key": f"CANARY-KEY-{t}"}, 1, "Opened x.", None, "auto"),
        ("rawargs", "apps.open", "L1", f"not json CANARY-RAWARGS-{t} {{", 1, "Opened.", None, "auto"),
        ("long", "weather.now", "L0", {"location": "Paris"}, 1, "w" * 600 + f" CANARY-LONG-{t}", None, "auto"),
        ("listargs", "files.read", "L0", f'["CANARY-LIST-{t}"]', 1, "ok", None, "auto"),
        ("minimal", "whatsapp.compose", "L2", {"contact": "Sam", "message": "[omitted: 36 chars]"}, 1, "Composed to Sam.", None, "confirmed"),
        ("fdel", "files.delete", "L2", {"path": "C:/x/old.txt"}, 1, "Deleted old.txt.", None, "confirmed"),
    ]
    if small:
        specs = specs[:12] + specs[16:18] + specs[23:]
    base = "2026-01-01T00:00:%02d+00:00"
    for i, (key, skill, tier, args, ok, result, error, decision) in enumerate(specs):
        args_raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        cur = c.execute(
            "INSERT INTO audit (at, actor, skill, tier, args, decision, ok, result, error, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (base % i, "text" if i % 2 else "voice", skill, tier, args_raw, decision, ok, result, error, 10 + i))
        aid = int(cur.lastrowid)
        fx.audit_ids[key] = aid
        obj = None
        try:
            obj = json.loads(args_raw)
        except ValueError:
            pass
        fx.audit.append({"key": key, "id": aid, "skill": skill, "tier": tier, "args_raw": args_raw, "args": obj, "ok": ok,
                         "result": result, "error": error, "decision": decision, "dur": 10 + i, "actor": "text" if i % 2 else "voice"})
        if key in ("harm1", "harm2", "minimal", "fdel"):
            fx.plain_audit_ids.append(aid)
    fx.changing_audit = len(specs) - 4

    # goals (parent / child), corrections, memory, undo, jobs, kb, phrasings -- stores the scrub must not touch
    fx.retained = [f"RETAINED-GOALREQ-{t}", f"RETAINED-CORR-{t}", f"RETAINED-MEMROW-{t}", f"RETAINED-UNDO-{t}",
                   f"RETAINED-JOB-{t}", f"RETAINED-KB-{t}", f"RETAINED-GOALTEXT-{t}", f"RETAINED-PHRASE-{t}"]
    now = "2026-01-01T00:00:00+00:00"
    c.execute("INSERT INTO goals (id, created_at, updated_at, original_request, objective, status) VALUES (?,?,?,?,?,?)",
              (f"g-parent-{t}", now, now, f"RETAINED-GOALREQ-{t}", "parent objective", "done"))
    c.execute("INSERT INTO goals (id, created_at, updated_at, original_request, objective, status, parent_goal_id) VALUES (?,?,?,?,?,?,?)",
              (f"g-child-{t}", now, now, "child request", "child objective", "done", f"g-parent-{t}"))
    c.execute("INSERT INTO corrections (at, goal_id, original_interpretation, user_correction, corrected_objective) VALUES (?,?,?,?,?)",
              (now, f"g-parent-{t}", "x", f"RETAINED-CORR-{t}", "y"))
    c.execute("INSERT INTO memory (at, kind, key, value, embedding, source) VALUES (?,?,?,?,?,?)",
              (now, "semantic", "k", f"RETAINED-MEMROW-{t}", bytes(range(32)), "user"))
    fx.undo_audit_id = fx.audit_ids["fdel"]
    c.execute("INSERT INTO undo (at, audit_id, skill, inverse, applied) VALUES (?,?,?,?,?)",
              (now, fx.undo_audit_id, "files.delete",
               json.dumps({"skill": "files.restore", "args": {"path": "C:/x/old.txt", "content": f"RETAINED-UNDO-{t}"}}), 0))
    c.execute("INSERT INTO phrasings (at, intent, text, source) VALUES (?,?,?,?)", (now, "apps.open", f"RETAINED-PHRASE-{t}", "taught"))
    c.execute("INSERT INTO jobs (name, created_at, trigger_type, trigger_spec, actions) VALUES (?,?,?,?,?)",
              (f"job-{t}", now, "cron", "{}", json.dumps([{"skill": "apps.open", "args": {"app": f"RETAINED-JOB-{t}"}}])))
    c.execute("INSERT INTO job_runs (job_id, at, ok, detail, ms) VALUES (?,?,?,?,?)", (1, now, 1, "ran", 5))
    c.execute("INSERT INTO kb_documents (path, title, indexed_at, mtime, chunk_count) VALUES (?,?,?,?,?)",
              (f"C:/kb/{t}.md", "doc", now, 1.5, 2))
    for seq in range(2):
        c.execute("INSERT INTO kb_chunks (doc_id, seq, text, embedding) VALUES (?,?,?,?)",
                  (1, seq, f"RETAINED-KB-{t} chunk {seq}", bytes([seq + 1]) * 64))

    # episodes
    plan1 = [
        {"tool": "whatsapp.compose", "args": {"contact": "Sam", "message": MSG}, "ok": False, "error": f"could not send '{MSG}'"},
        {"tool": "browser.type", "args": {"field": "Search", "text": TYPED2}, "ok": True, "error": None},
        {"tool": "shell.run", "args": {"command": CMD}, "ok": True, "error": None},
        {"tool": "web.open", "args": {"url": URLQ}, "ok": True, "error": None},
        {"tool": "web.login", "args": {"username": "bob", "password": f"CANARY-PW-{t}"}, "ok": False, "error": "bad login"},
    ]
    emb = bytes(range(16)) * 4
    eps = [
        ("ep1", f"RETAINED-GOALTEXT-{t} send a message", f"g-parent-{t}", json.dumps(plan1),
         f"step 1 failed: could not send '{MSG}'; opened {URLQ2}",
         f"last page https://example.com/ctx?s=CANARY-CTXQ-{t} ; user said '{MSG}'", "error", 0, 111),
        ("ep2", "open notepad", None, json.dumps([{"tool": "apps.open", "args": {"app": "notepad"}, "ok": True, "error": None}]),
         None, "desktop idle", "done", 1, 222),
        ("ep3", "broken plan", None, f"not json CANARY-PLAN-{t}[", "", "", "error", 0, 333),
        ("ep4", "odd plan", None, json.dumps(["just a string", {"tool": "x"}, {"tool": "files.read", "args": None}]), None, None, "done", 1, 444),
    ]
    for key, goal_text, gid, plan, errors, context, stopped, success, dur in eps:
        cur = c.execute(
            "INSERT INTO episodes (at, goal_id, goal_text, context, plan, stopped, success, errors, duration_ms, embedding) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (now, gid, goal_text, context, plan, stopped, success, errors, dur, emb))
        fx.ep_ids[key] = int(cur.lastrowid)
    fx.changing_episodes = 2  # ep1 and ep3
    c.commit()
    ints = c.execute("PRAGMA integrity_check").fetchone()[0]
    assert ints == "ok", ints
    c.close()
    return fx


class World:
    """One section's playground: a temp folder, fixtures in sub-folders, a backup root outside them."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.backups = tmp / "backups"
        self.n = 0

    def fix(self, tag: str = "7f3a", *, small: bool = False, name: str | None = None) -> Fix:
        self.n += 1
        return build_db(self.tmp / (name or f"db{self.n}") / "friday.db", tag, small=small)

    def target(self, fx: Fix, name: str = "active", other: Fix | None = None) -> dbscrub.Target:
        dummy = self.tmp / "no-such-other" / "friday.db"
        if name == "active":
            return dbscrub.resolve_target("active", active=fx.path, legacy=other.path if other else dummy)
        return dbscrub.resolve_target("legacy", active=other.path if other else dummy, legacy=fx.path)

    def code(self, t: dbscrub.Target) -> str:
        return dbscrub.preview(t).code

    def apply(self, t: dbscrub.Target, **kw) -> dbscrub.Result:
        kw.setdefault("backup_root", self.backups)
        kw.setdefault("confirm", self.code(t))
        return dbscrub.apply(t, **kw)

    def backup_dirs(self) -> list[str]:
        return sorted(x.name for x in self.backups.iterdir()) if self.backups.exists() else []

    def state(self, fx: Fix) -> tuple:
        return (fsha(fx.path), tuple(sorted(sidecars(fx.path).items())), tuple(folder_listing(fx.path.parent)))


def raises(fn, exc=dbscrub.ScrubError):
    """Run `fn`; return the exception message if it raised `exc`, None if it did not, re-raise anything else."""
    try:
        fn()
    except exc as e:
        return str(e) or type(e).__name__
    return None


class Boom(Exception):
    pass


def fault_at(point: str, exc: Exception | None = None):
    def f(p: str) -> None:
        if p == point:
            raise (exc or Boom("injected failure"))
    return f


def code_of(lines: list[str]) -> str:
    for ln in lines:
        m = re.search(r"confirmation code[^:]*: ([0-9a-f]{10})\s*$", ln)
        if m:
            return m.group(1)
    return ""


def run_cli(args: list[str], db: Path | None, *, extra_env: dict | None = None, timeout: int = 180):
    env = {k: v for k, v in os.environ.items() if k not in ("FRIDAY_DB", "FRIDAY_ALLOW_REAL_DB", "FRIDAY_REFUSE_REAL_DB")}
    env["FRIDAY_REFUSE_REAL_DB"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["COLUMNS"] = "400"
    if db is not None:
        env["FRIDAY_DB"] = str(db)
    env.update(extra_env or {})
    return subprocess.run([PY, str(ROOT / "run.py"), *args], env=env, capture_output=True, text=True, timeout=timeout,
                          cwd=str(ROOT), encoding="utf-8", errors="replace")


def make_alias(link: Path, target_dir: Path) -> bool:
    """A directory symlink or junction `link` -> `target_dir`; False if the OS refuses both."""
    try:
        os.symlink(str(target_dir), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        try:
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target_dir)], capture_output=True, text=True, timeout=30)
            return r.returncode == 0 and link.exists()
        except (OSError, subprocess.SubprocessError):
            return False
    return False


class SqlSpy:
    """Replaces `dbscrub.sqlite3` so every connection dbscrub opens records the SQL it executes."""

    def __init__(self) -> None:
        self.log: list[str] = []
        outer = self

        class SpyConn(sqlite3.Connection):
            def execute(self, sql, *a, **k):  # noqa: D401
                outer.log.append(" ".join(str(sql).split()))
                return super().execute(sql, *a, **k)

        self._cls = SpyConn

    def module(self):
        spy = self

        class Shim:
            def connect(self, *a, **k):
                k.setdefault("factory", spy._cls)
                return sqlite3.connect(*a, **k)

            def __getattr__(self, name):
                return getattr(sqlite3, name)

        return Shim()




# --------------------------------------------------------------------------------------------- shared checks

def leak_terms(fx: Fix) -> list[str]:
    return fx.removed + fx.retained + ["hello how are you", "locker code", "hunter2", "token=", "q=1", "example.com/p/a",
                                      "Sam", "Mum", "notepad", "C:/x/", "secret = 1", "X-Auth", "RETAINED-", "CANARY-"]


def no_leak(text: str, fx: Fix) -> bool:
    return not any(term in text for term in leak_terms(fx))


def unchanged(w: World, fxs: list[Fix], before: list[tuple]) -> bool:
    now = [(fsha(f.path), logical_ro_sorted(f.path)) for f in fxs]
    return now == before and w.backup_dirs() == []


def capture(fxs: list[Fix]) -> list[tuple]:
    return [(fsha(f.path), logical_ro_sorted(f.path)) for f in fxs]


# --------------------------------------------------------------------------------------------- A: preview

def sec_A(rec: Rec, quick: bool) -> None:
    rec.section("A: preview is read-only, counts-only, independent per target and bound to the exact state")
    with workdir() as tmp:
        w = World(tmp)
        a, b = w.fix(), w.fix("9c1e", small=True)
        ta, tb = w.target(a, "active", other=b), w.target(b, "legacy", other=a)
        before = capture([a, b])
        raw_a = fsha(a.path)
        pa, pb = dbscrub.preview(ta), dbscrub.preview(tb)
        text_a, text_b = "\n".join(dbscrub.format_preview(pa)), "\n".join(dbscrub.format_preview(pb))
        rec.check("A1 preview leaves both .db files byte-identical", capture([a, b]) == before and fsha(a.path) == raw_a)
        rec.check("A2 ...and creates no backup folder", w.backup_dirs() == [] and not sidecars(a.path)["-journal"])
        c = dbscrub._open_ro(a.path)
        blocked = ""
        try:
            c.execute("CREATE TABLE zz_probe (x)")
        except sqlite3.OperationalError as exc:
            blocked = str(exc)
        finally:
            c.close()
        rec.check("A3 the connection preview uses cannot write at all (read-only open)", "readonly" in blocked.replace(" ", "").lower(), blocked)
        rec.check("A4 preview text holds no canary, no message/URL/query text, no stored value of any kind", no_leak(text_a, a) and no_leak(text_b, b))
        rec.check("A5 ...and neither does the preview object's own fields (codes, actions, counts)",
                  no_leak(json.dumps([pa.code, [list(k) for k in pa.actions], pa.counts, pa.retained_by_policy], default=str), a))
        rec.check("A6 counts per table: audit 25 rows / 21 would change / 4 already minimal; episodes 4 / 2 / 2",
                  "audit: 25 rows, 21 would change, 4 unchanged" in text_a and "episodes: 4 rows, 2 would change, 2 unchanged" in text_a
                  and pa.total == 23 and pa.unchanged == {"audit": 4, "episodes": 2}, text_a[:300])
        kinds = {k[2] for k in pa.actions}
        rec.check("A7 changes are grouped by action (omit content, redact echoed content, strip url query/fragment, ...)",
                  {"omit content", "redact echoed content", "strip url query/fragment", "keep executable only", "redact credential",
                   "drop read-back content", "truncate", "scrub+bound text", "omit unclassified"} <= kinds, sorted(kinds))
        rec.check("A8 ...by table and field (e.g. audit args: whatsapp.compose.message x3, episodes plan.args)",
                  any(k[0] == "audit" and k[1] == "args: whatsapp.compose.message" and v == 3 for k, v in pa.actions.items())
                  and any(k[0] == "episodes" and k[1].startswith("plan.args") for k in pa.actions)
                  and "whatsapp.compose.message" in text_a and "plan.args" in text_a)
        rec.check("A9 retained-by-policy stores are listed with their counts",
                  pa.retained_by_policy == {"goals": 2, "corrections": 1, "memory": 1, "kb_documents": 1, "kb_chunks": 2, "phrasings": 1,
                                            "jobs": 1, "job_runs": 1, "undo": 1}
                  and "retained unchanged by policy" in text_a and "kb_chunks=2" in text_a and "goals=2" in text_a)
        rec.check("A10 undo rows that carry content-like arguments are flagged (retained, review separately)",
                  pa.undo_flagged == 1 and "undo journal rows carrying content-like arguments" in text_a)
        rec.check("A11 a confirmation code (10 hex) is shown", re.fullmatch(r"[0-9a-f]{10}", pa.code) is not None
                  and f"confirmation code (this target, this exact state): {pa.code}" in text_a)
        rec.check("A12 the preview is deterministic (same state -> same code)", dbscrub.preview(ta).code == pa.code)
        rec.check("A13 two different databases: independent counts and different codes",
                  pa.counts != pb.counts and pa.code != pb.code and pb.counts["audit"] == len(b.audit)
                  and pb.rows_to_change["audit"] == b.changing_audit and pa.rows_to_change["audit"] == a.changing_audit)
        rec.check("A14 ...each preview names its own target (and its own path)",
                  text_a.startswith("[active] ") and text_b.startswith("[legacy] ") and str(a.path) in text_a and str(b.path) in text_b)
        twin = tmp / "twin" / "friday.db"
        twin.parent.mkdir()
        shutil.copyfile(a.path, twin)
        tt = dbscrub.resolve_target("active", active=twin, legacy=tmp / "no-legacy" / "friday.db")
        ptw = dbscrub.preview(tt)
        rec.check("A15 a byte-identical copy at another path: same counts but a DIFFERENT code (the code binds the path)",
                  ptw.counts == pa.counts and ptw.rows_to_change == pa.rows_to_change and ptw.code != pa.code)

        missing = tmp / "missing" / "friday.db"
        tm = dbscrub.resolve_target("active", active=missing, legacy=tmp / "no-legacy" / "friday.db")
        pm = dbscrub.preview(tm)
        rec.check("A16 a missing database is reported, not created", (not pm.exists) and pm.code == "" and not missing.parent.exists()
                  and "unavailable" in "\n".join(dbscrub.format_preview(pm)))
        junk = tmp / "junk" / "friday.db"
        junk.parent.mkdir()
        junk.write_bytes(b"this is not a database " * 200)
        jh = fsha(junk)
        pj = dbscrub.preview(dbscrub.resolve_target("active", active=junk, legacy=tmp / "no-legacy" / "friday.db"))
        rec.check("A17 a file that is not a database gives an error in the preview (no crash) and is not modified",
                  pj.error != "" and fsha(junk) == jh and "unavailable" in "\n".join(dbscrub.format_preview(pj)), pj.error)
        empty = tmp / "empty" / "friday.db"
        empty.parent.mkdir()
        ec = sqlite3.connect(str(empty))
        ec.execute("CREATE TABLE other (x)")
        ec.commit()
        ec.close()
        pe = dbscrub.preview(dbscrub.resolve_target("active", active=empty, legacy=tmp / "no-legacy" / "friday.db"))
        rec.check("A18 a database without audit/episodes tables previews as 0 changes", pe.total == 0 and pe.error == "" and pe.exists)

        # ------------------------------------------------------------ target resolution
        dummy = tmp / "no-legacy" / "friday.db"
        t1 = dbscrub.resolve_target("active", active=a.path, legacy=dummy)
        rec.check("A19 resolve_target returns a canonical path and the right name",
                  t1.name == "active" and t1.path == Path(os.path.realpath(a.path)) and not t1.alias and not t1.same_as_other)
        rec.check("A20 names are normalised (case, spaces) but only 'active' and 'legacy' exist",
                  dbscrub.resolve_target(" ACTIVE ", active=a.path, legacy=dummy).name == "active" and dbscrub.TARGETS == ("active", "legacy"))
        msgs = [raises(lambda n=n: dbscrub.resolve_target(n, active=a.path, legacy=b.path)) for n in ("both", "", "all", None, "act")]
        rec.check("A21 any other target name raises ScrubError (never guesses, never 'both')", all(m is not None and "choose a target" in m for m in msgs), msgs)
        msg = raises(lambda: dbscrub.resolve_target("legacy", active=a.path, legacy=a.path))
        rec.check("A22 'legacy' that is the same file as 'active' is refused", msg is not None and "same file" in msg, msg)
        same = dbscrub.resolve_target("active", active=a.path, legacy=a.path)
        rec.check("A23 ...while 'active' still resolves and is flagged same_as_other", same.same_as_other and same.name == "active")
        with attr(paths, "DB_PATH", a.path):
            rec.check("A24 default resolution of 'active' follows dbplace.resolve() (the redirect)",
                      dbscrub.resolve_target("active", legacy=dummy).path == Path(os.path.realpath(a.path)))
        with attr(paths, "LEGACY_DB", b.path), attr(paths, "DB_PATH", a.path):
            rec.check("A25 default resolution of 'legacy' follows paths.LEGACY_DB", dbscrub.resolve_target("legacy").path == Path(os.path.realpath(b.path)))
        real = tmp / "aliasreal"
        real.mkdir()
        afx = build_db(real / "friday.db", "aaaa", small=True)
        link = tmp / "aliaslink"
        if make_alias(link, real):
            ta_alias = dbscrub.resolve_target("active", active=link / "friday.db", legacy=dummy)
            rec.check("A26 a path reached through a symlink/junction is flagged as an alias and canonicalised",
                      ta_alias.alias and ta_alias.path == Path(os.path.realpath(real / "friday.db")))
            h = fsha(afx.path)
            msg = raises(lambda: dbscrub.apply(ta_alias, confirm=dbscrub.preview(ta_alias).code, backup_root=w.backups))
            rec.check("A27 apply through an alias is refused, nothing changes, no backup folder",
                      msg is not None and "alias" in msg and fsha(afx.path) == h and w.backup_dirs() == [], msg)
        else:
            rec.skip("A26/A27 alias detection", "the OS refused both symlink and junction creation")
        with envpatch(OneDrive=str(tmp)):
            rec.check("A28 a database under %OneDrive% is flagged as synced",
                      dbscrub.resolve_target("active", active=a.path, legacy=dummy).synced)
        rec.check("A29 a plain temp database is not", not t1.synced)
        with attr(dbplace, "in_synced_folder", lambda p: True):
            ts = dbscrub.resolve_target("active", active=a.path, legacy=dummy)
            ptxt = "\n".join(dbscrub.format_preview(dbscrub.preview(ts)))
        rec.check("A30 the preview of a synced database warns that the provider's history is not purged",
                  ts.synced and "cloud-synced folder" in ptxt and "version history" in ptxt)

        if not quick:
            r = run_cli(["db", "scrub", "--target", "active"], a.path)
            out = r.stdout + r.stderr
            rec.check("A31 CLI preview (python run.py db scrub --target active) exits 0, says nothing was modified",
                      r.returncode == 0 and "Preview only: nothing was modified." in out, (r.returncode, out[-200:]))
            rec.check("A32 ...stdout/stderr contain no canary, no message/URL/query text", no_leak(out, a))
            rec.check("A33 ...the CLI's code equals the in-process preview code and lists counts + retained stores",
                      code_of(out.splitlines()) == pa.code and "audit: 25 rows, 21 would change, 4 unchanged" in out and "retained unchanged by policy" in out)
            rec.check("A34 ...and the database is byte- and logically identical afterwards", capture([a, b]) == before and fsha(a.path) == raw_a)
            r = run_cli(["db", "scrub", "--target", "active"], tmp / "nothing-here" / "friday.db")
            rec.check("A35 CLI preview of a missing database says so and creates nothing",
                      r.returncode == 0 and "does not exist" in r.stdout and not (tmp / "nothing-here").exists(), r.stdout[-200:] + r.stderr[-200:])


# --------------------------------------------------------------------------------------------- B: gating

def _cli_inproc(args: tuple, a: Fix, b: Fix) -> tuple[int | None, str]:
    """Run friday.cli._db_scrub in this process against throwaway 'active'/'legacy' databases."""
    import typer
    from friday import cli

    buf = io.StringIO()
    code: int | None = None
    saved_width = cli.con._width
    cli.con.width = 400
    real_list = list(dbplace.real_db_paths())  # computed BEFORE LEGACY_DB is faked: the guard stays armed for the true paths
    try:
        with attr(dbplace, "resolve", lambda: a.path), attr(paths, "LEGACY_DB", b.path), attr(dbplace, "real_db_paths", lambda: real_list), \
                contextlib.redirect_stdout(buf):
            try:
                cli._db_scrub(*args)
            except typer.Exit as exc:
                code = exc.exit_code
    finally:
        cli.con._width = saved_width
    return code, buf.getvalue()


def sec_B(rec: Rec, quick: bool) -> None:
    rec.section("B: apply is gated -- confirmation, target, state; the other database and startup are never touched")
    with workdir() as tmp:
        w = World(tmp)
        a, b = w.fix(), w.fix("9c1e", small=True)
        ta, tb = w.target(a, "active", other=b), w.target(b, "legacy", other=a)
        before = capture([a, b])
        code_a, code_b = w.code(ta), w.code(tb)

        def attempt(target, **kw):
            return raises(lambda: dbscrub.apply(target, backup_root=w.backups, **kw))

        msg = attempt(ta, confirm="")
        rec.check("B1 apply with no confirmation code -> ScrubError, database unchanged, no backup folder",
                  msg is not None and "confirmation code required" in msg and unchanged(w, [a, b], before), msg)
        msg = attempt(ta, confirm=None)  # type: ignore[arg-type]
        rec.check("B2 ...a non-string confirmation is refused too", msg is not None and unchanged(w, [a, b], before), msg)
        msg = attempt(ta, confirm="0000000000")
        rec.check("B3 a wrong code -> ScrubError, nothing changed, no backup folder",
                  msg is not None and "does not match" in msg and unchanged(w, [a, b], before), msg)
        msg1, msg2 = attempt(ta, confirm=code_b), attempt(tb, confirm=code_a)
        rec.check("B4 the OTHER target's code is refused (both directions); neither database changed",
                  msg1 is not None and msg2 is not None and unchanged(w, [a, b], before), (msg1, msg2))
        msg = attempt(dbscrub.Target("both", a.path, False, False), confirm=code_a)
        rec.check("B5 an invalid target name -> ScrubError ('explicit target required'), nothing changed",
                  msg is not None and "explicit target" in msg and unchanged(w, [a, b], before), msg)
        ghost = dbscrub.resolve_target("active", active=tmp / "ghost" / "friday.db", legacy=b.path)
        msg = attempt(ghost, confirm=code_a)
        rec.check("B6 a target that does not exist is refused without creating anything",
                  msg is not None and "does not exist" in msg and not (tmp / "ghost").exists(), msg)
        rec.check("B7 no error message ever contains a stored value", all(m and "CANARY-" not in m and "RETAINED-" not in m for m in (msg, msg1, msg2)))
        msg = attempt(ta, confirm=code_a + " x")
        rec.check("B8 a code with extra characters is not accepted", msg is not None and unchanged(w, [a, b], before))
        with attr(dbplace, "in_synced_folder", lambda p: True):
            tsy = dbscrub.resolve_target("active", active=a.path, legacy=b.path)
        msg = attempt(tsy, confirm=code_a)
        rec.check("B9 a database in a synced folder is refused without allow_synced (even with the right code)",
                  tsy.synced and msg is not None and "cloud-synced" in msg and "--allow-synced" in msg and unchanged(w, [a, b], before), msg)

        # stale code: same rows, different content -> different code; new row -> different code
        s = w.fix("5b2d", small=True)
        ts_ = w.target(s, "active", other=b)
        old_code = w.code(ts_)
        sc = sqlite3.connect(str(s.path))
        sc.execute("UPDATE audit SET args=? WHERE id=?", (json.dumps({"contact": "Sam", "message": "CANARY-MSG-5b2d changed text of the same row"}), s.audit_ids["wa_ok"]))
        sc.commit()
        sc.close()
        rec.check("B10 changing the CONTENT of an existing row (same ids) changes the code", w.code(ts_) != old_code)
        before_s = capture([s])
        msg = attempt(ts_, confirm=old_code)
        rec.check("B11 a stale code (database changed since the preview) is refused; nothing changed",
                  msg is not None and "does not match" in msg and unchanged(w, [s], before_s), msg)
        sc = sqlite3.connect(str(s.path))
        sc.execute("INSERT INTO audit (at, actor, skill, tier, args, decision) VALUES ('2026-03-01','text','input.type','L2',?, 'auto')",
                   (json.dumps({"text": "CANARY-TYPED-5b2d one more row"}),))
        sc.commit()
        sc.close()
        mid_code = w.code(ts_)
        before_s = capture([s])
        msg = attempt(ts_, confirm=old_code)
        rec.check("B12 a new row added after the preview also invalidates the old code", msg is not None and mid_code != old_code and unchanged(w, [s], before_s), msg)

        # independence: applying to one target never touches the other
        a2, b2 = w.fix(), w.fix("9c1e", small=True)
        t_a2, t_b2 = w.target(a2, "active", other=b2), w.target(b2, "legacy", other=a2)
        b2_before = capture([b2])
        code_b2_before = w.code(t_b2)
        res = w.apply(t_a2)
        rec.check("B13 applying to 'active' leaves the 'legacy' database byte-identical, logically identical and with the same code",
                  capture([b2]) == b2_before and w.code(t_b2) == code_b2_before and res.target.name == "active")
        rec.check("B14 ...and made exactly one backup, named after the active target",
                  len(w.backup_dirs()) == 1 and "-active-" in w.backup_dirs()[0])
        a3, b3 = w.fix(), w.fix("9c1e", small=True)
        t_a3, t_b3 = w.target(a3, "active", other=b3), w.target(b3, "legacy", other=a3)
        a3_before = capture([a3])
        res = w.apply(t_b3)
        rec.check("B15 and the other way round: scrubbing 'legacy' leaves 'active' untouched",
                  capture([a3]) == a3_before and res.target.name == "legacy" and any("-legacy-" in n for n in w.backup_dirs()))
        sig = inspect.signature(dbscrub.apply)
        rec.check("B16 apply takes ONE target (no list, no 'both'); the module defines exactly two targets",
                  list(sig.parameters)[:1] == ["target"] and "targets" not in sig.parameters and dbscrub.TARGETS == ("active", "legacy"))

        # CLI (in process, throwaway databases)
        a4, b4 = w.fix(), w.fix("9c1e", small=True)
        cb = capture([a4, b4])
        code_, out = _cli_inproc(("", True, "whatever", str(w.backups / "cli"), False), a4, b4)
        rec.check("B17 CLI --apply without --target exits 2 and changes nothing (neither database)",
                  code_ == 2 and "explicit --target" in out and capture([a4, b4]) == cb and not (w.backups / "cli").exists(), (code_, out[-160:]))
        code_, out = _cli_inproc(("active", True, "", str(w.backups / "cli"), False), a4, b4)
        rec.check("B18 CLI --apply --target active without --confirm exits 2, prints the code to use, changes nothing",
                  code_ == 2 and "--apply --confirm" in out and capture([a4, b4]) == cb and not (w.backups / "cli").exists(), (code_, out[-160:]))
        code_, out = _cli_inproc(("active", True, "0000000000", str(w.backups / "cli"), False), a4, b4)
        rec.check("B19 CLI with a wrong code exits 1 and changes nothing", code_ == 1 and capture([a4, b4]) == cb, (code_, out[-160:]))
        code_, out = _cli_inproc(("bogus", False, "", "", False), a4, b4)
        rec.check("B20 CLI with an unknown target exits 2", code_ == 2 and capture([a4, b4]) == cb, (code_, out[-120:]))
        code_, out = _cli_inproc(("", False, "", "", False), a4, b4)
        rec.check("B21 CLI preview with no target previews BOTH databases, read-only, and prints no stored value",
                  code_ is None and "[active]" in out and "[legacy]" in out and "Preview only" in out and capture([a4, b4]) == cb
                  and no_leak(out, a4) and no_leak(out, b4), (code_, out[-160:]))
        pcode = dbscrub.preview(w.target(a4, "active", other=b4)).code
        code_, out = _cli_inproc(("active", True, pcode, str(w.backups / "cli"), False), a4, b4)
        rec.check("B22 CLI --apply --target active --confirm CODE scrubs ONLY the active database and prints the backup + 'keeps the sensitive history'",
                  code_ is None and capture([b4]) == [cb[1]] and capture([a4]) != [cb[0]] and "keeps the sensitive history" in out
                  and "sha256=" in out and no_leak(out, a4), (code_, out[-200:]))
        pcode2 = dbscrub.preview(w.target(a4, "active", other=b4)).code
        code_, out = _cli_inproc(("active", True, pcode2, str(w.backups / "cli"), False), a4, b4)
        rec.check("B23 ...and CLI --apply on an already-scrubbed database exits 1 ('nothing to scrub') with no second backup",
                  code_ == 1 and "nothing to scrub" in out and len(list((w.backups / "cli").iterdir())) == 1, (code_, out[-160:]))

        if not quick:
            a5 = w.fix()
            c5 = capture([a5])
            r = run_cli(["db", "scrub", "--apply"], a5.path)
            rec.check("B24 subprocess: `db scrub --apply` without --target exits 2 and changes nothing",
                      r.returncode == 2 and "explicit --target" in (r.stdout + r.stderr) and capture([a5]) == c5, (r.returncode, (r.stdout + r.stderr)[-200:]))
            r = run_cli(["db", "scrub", "--apply", "--target", "active"], a5.path)
            rec.check("B25 subprocess: `--apply --target active` without --confirm exits 2 and changes nothing",
                      r.returncode == 2 and capture([a5]) == c5, (r.returncode, (r.stdout + r.stderr)[-200:]))
            r = run_cli(["db", "scrub", "--apply", "--target", "active", "--confirm", "0000000000", "--backup-dir", str(w.backups / "sub")], a5.path)
            rec.check("B26 subprocess: a wrong --confirm exits 1 and changes nothing",
                      r.returncode == 1 and capture([a5]) == c5 and not (w.backups / "sub").exists(), (r.returncode, (r.stdout + r.stderr)[-200:]))
            r = run_cli(["db", "scrub", "--target", "everything"], a5.path)
            rec.check("B27 subprocess: an unknown --target exits 2", r.returncode == 2, r.returncode)
            r = run_cli(["db", "nonsense"], a5.path)
            rec.check("B28 subprocess: an unknown db action exits 2 and lists the usage", r.returncode == 2 and "verify-backup" in r.stdout)

        # nothing at startup / migration / store init calls or imports dbscrub
        offenders: list[str] = []
        module_level_cli = False
        for f in sorted((ROOT / "friday").rglob("*.py")):
            if f.name == "dbscrub.py":
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    names = [x.name for x in node.names] + [node.module or ""]
                elif isinstance(node, ast.Import):
                    names = [x.name for x in node.names]
                elif isinstance(node, ast.Attribute) and node.attr == "dbscrub":
                    names = ["dbscrub"]
                if any("dbscrub" in n for n in names):
                    offenders.append(f.relative_to(ROOT).as_posix())
            if f.name == "cli.py":
                for node in tree.body:
                    if isinstance(node, (ast.Import, ast.ImportFrom)) and any("dbscrub" in x.name for x in node.names):
                        module_level_cli = True
        rec.check("B29 only friday/cli.py imports dbscrub (not store, dbplace, session, daemon, startup or migration code)",
                  set(offenders) == {"friday/cli.py"}, sorted(set(offenders)))
        rec.check("B30 ...and cli.py imports it lazily, inside the db command only (never at module import)", not module_level_cli)
        if not quick:
            probe = (
                "import sys, os\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from friday import store, dbplace, session, daemon, paths\n"
                "import friday.cli\n"
                "c = store.conn()\n"
                "c.execute('select count(*) from audit').fetchone()\n"
                "dbplace.ensure_ready(dbplace.resolve())\n"
                "print('LOADED' if 'friday.dbscrub' in sys.modules else 'CLEAN')\n"
            )
            env = {k: v for k, v in os.environ.items() if k not in ("FRIDAY_ALLOW_REAL_DB",)}
            env.update(FRIDAY_REFUSE_REAL_DB="1", FRIDAY_DB=str(tmp / "startup" / "friday.db"))
            r = subprocess.run([PY, "-c", probe], capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=180)
            rec.check("B31 a fresh process that imports store/session/daemon/cli, opens the store (schema + migrations) never loads dbscrub",
                      r.returncode == 0 and r.stdout.strip().endswith("CLEAN"), (r.returncode, r.stdout[-100:], r.stderr[-300:]))


# --------------------------------------------------------------------------------------------- C: policy

def _expected_audit(orig: dict) -> tuple[str, str | None, str | None]:
    """What the live code would store for this original row (args json, result, error)."""
    skill = orig["skill"]
    obj = orig["args"]
    try:
        raw = orig["args_raw"]
        json.loads(raw)
        parsed = True
    except ValueError:
        parsed = False
    if not parsed:
        return json.dumps({"_unparseable": f"[omitted: {len(orig['args_raw'])} chars]"}), orig["result"], orig["error"]
    echoes = auditview.content_values(skill, obj)
    return (dbscrub.store_dumps(auditview.sanitize_args(skill, obj)),
            auditview.sanitize_result(skill, orig["result"], echoes), auditview.sanitize_error(orig["error"], echoes))


def sec_C(rec: Rec, quick: bool) -> None:
    rec.section("C: the policy -- sensitive text gone everywhere (columns AND raw bytes), everything else preserved")
    with workdir() as tmp:
        w = World(tmp)
        a, b = w.fix(), w.fix("9c1e", small=True)
        ta = w.target(a, "active", other=b)
        present = [n for n in a.removed if db_contains(a.path, n)]
        pre_audit = {r["id"]: r for r in table_rows(a.path, "select * from audit")}
        pre_ep = {r["id"]: r for r in table_rows(a.path, "select * from episodes")}
        pre_snap = logical(a.path)
        pre_raw_present = [n for n in present if raw_contains(a.path, n)]
        rec.check("C0 (setup) the fixture really contains every canary before the scrub, in the column text and in the raw file",
                  len(present) >= 22 and len(pre_raw_present) == len(present), (len(present), len(pre_raw_present)))
        b_before = capture([b])
        res = w.apply(ta)
        post = logical(a.path)
        rec.check("C1 apply reports 21 audit rows + 2 episode rows changed, integrity ok, vacuumed and purged",
                  dict(res.rows_changed) == {"audit": 21, "episodes": 2} and res.after["integrity"] == "ok" and res.vacuumed and res.purged)
        gone = [n for n in present if db_contains(a.path, n)]
        rec.check("C2 every canary is gone from every column of every table", gone == [], [g.replace("CANARY-", "C~") for g in gone])
        rec.check("C3 ...and no 'CANARY-' text remains anywhere in the database", not db_contains(a.path, "CANARY-"))
        raw_left = [n for n in present if raw_contains(a.path, n)]
        rec.check("C4 ...and none is left in the raw file bytes or its -wal/-shm (secure_delete + checkpoint + VACUUM worked)",
                  raw_left == [] and not raw_contains(a.path, "CANARY-"), [g.replace("CANARY-", "C~") for g in raw_left])
        rec.check("C5 content-like URL pieces are gone from the raw bytes too (query and fragment, message text)",
                  not any(raw_contains(a.path, s) for s in ("token=CANARY", "q=1#", "hello how are you", "locker code on", "hunter2hunter2")))
        rec.check("C6 stores outside the policy still hold their text (goals, corrections, memory, undo, jobs, kb, phrasings, goal_text)",
                  all(db_contains(a.path, n) for n in a.retained))

        post_audit = {r["id"]: r for r in table_rows(a.path, "select * from audit")}
        post_ep = {r["id"]: r for r in table_rows(a.path, "select * from episodes")}
        same_cols = all(
            {k: v for k, v in pre_audit[i].items() if k not in ("args", "result", "error")}
            == {k: v for k, v in post_audit[i].items() if k not in ("args", "result", "error")} for i in pre_audit)
        rec.check("C7 audit: ids, at, actor, skill, tier, decision, ok, duration_ms are identical for every row",
                  same_cols and set(pre_audit) == set(post_audit))
        rec.check("C8 episodes: id, at, goal_id, goal_text, stopped, success, duration_ms and the embedding blob are identical",
                  all({k: v for k, v in pre_ep[i].items() if k not in ("plan", "errors", "context")}
                      == {k: v for k, v in post_ep[i].items() if k not in ("plan", "errors", "context")} for i in pre_ep)
                  and post_ep[a.ep_ids["ep1"]]["goal_text"].startswith("RETAINED-GOALTEXT-"))
        rec.check("C9 ok (the outcome) is untouched: failed rows stay 0, pending rows stay NULL, successes stay 1",
                  post_audit[a.audit_ids["wa_fail"]]["ok"] == 0 and post_audit[a.audit_ids["wa_pending"]]["ok"] is None
                  and post_audit[a.audit_ids["wa_ok"]]["ok"] == 1 and post_audit[a.audit_ids["wa_pending"]]["result"] is None)
        untouched_tables = ["goals", "corrections", "memory", "undo", "jobs", "job_runs", "kb_documents", "kb_chunks", "phrasings"]
        rec.check("C10 goals/corrections/memory/undo/jobs/job_runs/kb_documents/kb_chunks/phrasings: logical digests identical (incl. embedding blobs)",
                  all(post["digest"][t] == pre_snap["digest"][t] for t in untouched_tables) and
                  post["kept_digest"]["audit"] == pre_snap["kept_digest"]["audit"] and post["kept_digest"]["episodes"] == pre_snap["kept_digest"]["episodes"])
        rec.check("C11 row counts, ids and schema are equal; integrity ok", post["counts"] == pre_snap["counts"] and post["ids"] == pre_snap["ids"]
                  and post["schema"] == pre_snap["schema"] and post["integrity"] == "ok")
        cx = sqlite3.connect(str(a.path))
        fk = cx.execute("PRAGMA foreign_key_check").fetchall()
        joined = cx.execute("SELECT COUNT(*) FROM undo u JOIN audit a ON a.id = u.audit_id WHERE u.audit_id = ?", (a.undo_audit_id,)).fetchone()[0]
        parents = cx.execute("SELECT COUNT(*) FROM goals c JOIN goals p ON p.id = c.parent_goal_id").fetchone()[0]
        eps = cx.execute("SELECT COUNT(*) FROM episodes e JOIN goals g ON g.id = e.goal_id").fetchone()[0]
        cx.close()
        rec.check("C12 foreign keys intact: foreign_key_check empty, undo.audit_id still resolves, goal parent/child and episode goal link intact",
                  fk == [] and joined == 1 and parents == 1 and eps == 1 and post["fk_violations"] == 0)

        # literal pins of the documented replacement formats
        wa = post_audit[a.audit_ids["wa_ok"]]
        rec.check("C13 message argument -> '[omitted: N chars]' with the true length; contact kept; echo in the result replaced",
                  json.loads(wa["args"]) == {"contact": "Sam", "message": "[omitted: 39 chars]"} and wa["result"] == "Composed '[omitted: 39 chars]' to Sam.", wa["args"])
        rec.check("C14 an echo inside the ERROR text is replaced the same way",
                  post_audit[a.audit_ids["wa_fail"]]["error"] == "WhatsApp refused: '[omitted: 39 chars]'")
        wo = json.loads(post_audit[a.audit_ids["webopen"]]["args"])["url"]
        rec.check("C15 URL keeps scheme+host+path; query and fragment replaced by ' [query/credentials not stored]'",
                  wo == "https://example.com/p/a [query/credentials not stored]" and "?" not in wo and "#" not in wo, wo)
        rec.check("C16 ...also in a result and an error that quote the URL",
                  post_audit[a.audit_ids["webopen"]]["result"] == "Opened https://example.com/p/a [query/credentials not stored]"
                  and post_audit[a.audit_ids["bopen"]]["error"] == "net::ERR_FAILED for https://example.com/p/a [query/credentials not stored]")
        rec.check("C17 read-back content (files.read, web.fetch, shell.run, dev.python) -> '[read-back content not stored: N chars]'",
                  post_audit[a.audit_ids["fread"]]["result"] == "[read-back content not stored: 317 chars]"
                  and post_audit[a.audit_ids["fetch"]]["result"].startswith("[read-back content not stored: ")
                  and post_audit[a.audit_ids["shell"]]["result"].startswith("[read-back content not stored: ")
                  and post_audit[a.audit_ids["py"]]["result"].startswith("[read-back content not stored: "))
        rec.check("C18 a shell command keeps only the executable and the argument count; shell and cwd stay",
                  json.loads(post_audit[a.audit_ids["shell"]]["args"]) == {"command": "curl [omitted: 4 args, 50 chars]", "shell": "powershell", "cwd": "C:/proj"})
        rec.check("C19 benign arguments are untouched: app name, amount, file paths (the evidence of WHAT was touched)",
                  post_audit[a.audit_ids["harm1"]]["args"] == pre_audit[a.audit_ids["harm1"]]["args"] == '{"app": "notepad"}'
                  and post_audit[a.audit_ids["harm2"]]["args"] == pre_audit[a.audit_ids["harm2"]]["args"]
                  and json.loads(post_audit[a.audit_ids["fread"]]["args"]) == {"path": "C:/x/a.txt"}
                  and post_audit[a.audit_ids["fdel"]]["args"] == pre_audit[a.audit_ids["fdel"]]["args"])
        rec.check("C20 rows that were already minimal are not rewritten (byte-identical rows)",
                  all(post_audit[i] == pre_audit[i] for i in a.plain_audit_ids))
        rec.check("C21 an argument NAMED like a credential -> '[redacted]'", json.loads(post_audit[a.audit_ids["cred"]]["args"]) == {"app": "x", "api_key": "[redacted]"})
        rec.check("C22 args that are not valid JSON -> the documented JSON marker with the original length",
                  json.loads(post_audit[a.audit_ids["rawargs"]]["args"]) == {"_unparseable": f"[omitted: {len(pre_audit[a.audit_ids['rawargs']]['args'])} chars]"})
        rec.check("C23 args that are a JSON list -> a length marker (never kept)", json.loads(post_audit[a.audit_ids["listargs"]]["args"]) == "[omitted: 1 items]")
        rec.check("C24 an over-long free-text result is bounded with the '...[+N chars]' marker; an over-long goal likewise",
                  post_audit[a.audit_ids["long"]]["result"].endswith("...[+117 chars]") and len(post_audit[a.audit_ids["long"]]["result"]) < 520
                  and json.loads(post_audit[a.audit_ids["plan"]]["args"])["goal"].endswith("...[+122 chars]"))
        rec.check("C25 an unknown (test.*) skill fails closed: its text argument becomes a length marker, numbers stay",
                  json.loads(post_audit[a.audit_ids["testsk"]]["args"]) == {"text": "[omitted: 23 chars]", "n": 3})

        # same as the live policy: compare with a fresh sanitisation of the ORIGINAL values
        mism = []
        for o in a.audit:
            exp = _expected_audit(o)
            got = post_audit[o["id"]]
            if (got["args"], got["result"], got["error"]) != exp:
                mism.append(o["key"])
        rec.check("C26 every audit row equals what auditview.sanitize_args / sanitize_result / sanitize_error give the live code for the original values",
                  mism == [], mism)
        ep_mism = []
        ep1 = json.loads(post_ep[a.ep_ids["ep1"]]["plan"])
        orig_plan = json.loads(pre_ep[a.ep_ids["ep1"]]["plan"])
        for o, g in zip(orig_plan, ep1):
            echoes = auditview.content_values(o["tool"], o.get("args"))
            exp_args = auditview.sanitize_args(o["tool"], o.get("args"))
            exp_err = auditview.sanitize_error(o["error"], echoes) if o.get("error") else o.get("error")
            if g["args"] != exp_args or g["error"] != exp_err or g["tool"] != o["tool"] or g["ok"] != o["ok"]:
                ep_mism.append(o["tool"])
        rec.check("C27 every step of the episode plan equals the live sanitisation of the original step (tool and ok kept)", ep_mism == [], ep_mism)
        rec.check("C28 episode plan: password -> '[redacted]'; username (unclassified) -> length marker; web.open URL stripped",
                  ep1[4]["args"] == {"username": "[omitted: 3 chars]", "password": "[redacted]"}
                  and ep1[3]["args"]["url"] == "https://example.com/p/a [query/credentials not stored]")
        e1 = post_ep[a.ep_ids["ep1"]]
        rec.check("C29 episode errors/context: echoes of omitted values -> marker, URL query/fragment stripped, nothing else rewritten",
                  e1["errors"] == "step 1 failed: could not send '[omitted: 39 chars]'; opened https://example.com/q/b [query/credentials not stored]"
                  and e1["context"] == "last page https://example.com/ctx [query/credentials not stored] ; user said '[omitted: 39 chars]'")
        rec.check("C30 an episode whose plan is not JSON -> one '(unparseable)' step with a length marker; clean episodes are unchanged",
                  json.loads(post_ep[a.ep_ids["ep3"]]["plan"]) == [{"tool": "(unparseable)", "args": {"_unparseable": f"[omitted: {len(pre_ep[a.ep_ids['ep3']]['plan'])} chars]"}, "ok": None, "error": None}]
                  and post_ep[a.ep_ids["ep2"]] == pre_ep[a.ep_ids["ep2"]] and post_ep[a.ep_ids["ep4"]] == pre_ep[a.ep_ids["ep4"]])
        rec.check("C31 the second fixture was never touched by scrubbing the first", capture([b]) == b_before and db_contains(b.path, "CANARY-MSG-9c1e"))

        # a fresh preview of the scrubbed database lists nothing, and the manifest/ error text are value-free
        rec.check("C32 a preview of the scrubbed database shows 0 rows to change", dbscrub.preview(ta).total == 0 and dbscrub.preview(ta).rows_to_change == {})

        # vacuum=False: committed and verified, but honestly reported as not compacted; compact() then purges
        c2 = w.fix("1d4e")
        t2 = w.target(c2, "active", other=b)
        res2 = w.apply(t2, vacuum=False)
        man2 = json.loads((res2.backup.directory / "manifest.json").read_text(encoding="utf-8"))
        rec.check("C33 vacuum=False: no raise, vacuumed=False and purged=False are reported, the manifest status is 'done-not-compacted' (not 'done')",
                  not res2.vacuumed and res2.purged is False and man2["status"] == "done-not-compacted" and man2["purged"] is False and not db_contains(c2.path, "CANARY-"))
        d2 = logical(c2.path)["digest"]
        dbscrub.compact(t2)
        rec.check("C33b ...and `compact` then removes every replaced value from the raw file without changing any table digest",
                  not raw_contains(c2.path, "CANARY-") and logical(c2.path)["digest"] == d2)

        # the exact transaction shape: one BEGIN IMMEDIATE ... one COMMIT; secure_delete before it; VACUUM after
        spy = SqlSpy()
        c3 = w.fix("2e5f", small=True)
        t3 = w.target(c3, "active", other=b)
        code3 = w.code(t3)
        with attr(dbscrub, "sqlite3", spy.module()):
            dbscrub.apply(t3, confirm=code3, backup_root=w.backups)
        log_ = spy.log
        idx = {k: [i for i, s in enumerate(log_) if s.startswith(k)] for k in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK", "PRAGMA secure_delete=ON", "VACUUM", "UPDATE")}
        one_tx = len(idx["BEGIN IMMEDIATE"]) == 1 and len(idx["COMMIT"]) == 1 and not idx["ROLLBACK"]
        rec.check("C34 the scrub is ONE transaction: a single BEGIN IMMEDIATE, every UPDATE inside it, a single COMMIT, no ROLLBACK",
                  one_tx and idx["UPDATE"] and idx["BEGIN IMMEDIATE"][0] < min(idx["UPDATE"]) and max(idx["UPDATE"]) < idx["COMMIT"][0], idx)
        rec.check("C35 secure_delete is switched ON before the transaction begins; checkpoint and VACUUM run after COMMIT",
                  idx["PRAGMA secure_delete=ON"] and idx["PRAGMA secure_delete=ON"][0] < idx["BEGIN IMMEDIATE"][0]
                  and idx["VACUUM"] and idx["VACUUM"][0] > idx["COMMIT"][0] and any(s.startswith("PRAGMA wal_checkpoint(TRUNCATE)") for s in log_[idx["COMMIT"][0]:]), idx)
        rec.check("C36 the exclusive lock is taken first (PRAGMA locking_mode=EXCLUSIVE precedes everything else)",
                  log_ and log_[0].startswith("PRAGMA locking_mode=EXCLUSIVE"), log_[:2])


# --------------------------------------------------------------------------------------------- D: idempotence

def _corpus() -> list:
    return ["", "x", "hello world", "x" * 1000, "https://h.test/p?a=b#c", "https://u:p@h.test:8443/p/a?q=1",
            "https://h.test/p [query/credentials not stored]", "http://[bad", "[omitted: 5 chars]", "[omitted: 36 chars, 2 lines]",
            "[omitted: 3 items]", "[redacted]", "[read-back content not stored: 9 chars]", "[read-back content not stored: 317 chars]",
            "curl -H x https://a.test", "curl [omitted: 4 args, 50 chars]", "git", "C:\\Users\\x\\file.txt", "a" * 300 + "...[+5 chars]",
            "b" * 199 + "...[+3 chars]", "c" * 700, "password=hunter2 and token=abc", "line1\nline2 words", 7, 3.5, True, None,
            ["a" * 10, "b" * 10], ["[omitted: 5 chars]"], {"k": "v"}, "see https://h.test/x?y=1 and https://g.test/z#f",
            "Composed '[omitted: 39 chars]' to Sam.", "net::ERR for https://example.com/p/a [query/credentials not stored]"]


def sec_D(rec: Rec, quick: bool) -> None:
    rec.section("D: idempotence -- a scrubbed database yields nothing; the auditview functions are fixed points; the live path stores no body")
    with workdir() as tmp:
        w = World(tmp)
        a, b = w.fix(), w.fix("9c1e", small=True)
        ta = w.target(a, "active", other=b)
        w.apply(ta)
        c = dbscrub._open_ro(a.path)
        try:
            again = list(dbscrub.changes(c))
        finally:
            c.close()
        rec.check("D1 changes() over a scrubbed database yields nothing", again == [])
        p2 = dbscrub.preview(ta)
        rec.check("D2 preview of it: 0 rows, no actions, 'would change' = 0 for both tables",
                  p2.total == 0 and not p2.actions and "21 would change" not in "\n".join(dbscrub.format_preview(p2))
                  and "audit: 25 rows, 0 would change, 25 unchanged" in "\n".join(dbscrub.format_preview(p2)))
        h = fsha(a.path)
        dirs = w.backup_dirs()
        msg = raises(lambda: dbscrub.apply(ta, confirm=p2.code, backup_root=w.backups))
        rec.check("D3 a second apply raises ScrubError('nothing to scrub'), changes nothing and creates no second backup",
                  msg is not None and "nothing to scrub" in msg and fsha(a.path) == h and w.backup_dirs() == dirs and len(dirs) == 1, msg)

        # fixed points of the policy functions
        S = auditview.sanitize_args
        skills = list(auditview.POLICY) + ["unknown.skill", "test.x", ""]
        viol: list[str] = []
        n_cases = 0
        for skill in skills:
            keys = list(auditview.POLICY.get(skill, {})) + ["text", "password", "blob"]
            for key in keys:
                for v in _corpus():
                    n_cases += 1
                    once = S(skill, {key: v})
                    twice = S(skill, json.loads(dbscrub.store_dumps(once)))
                    if once != twice:
                        viol.append(f"{skill}.{key}")
        rec.check("D4 sanitize_args is a fixed point (through the stored JSON form) for every policy skill/key and a corpus of values",
                  viol == [], (n_cases, sorted(set(viol))[:6]))
        viol = []
        for v in _corpus():
            for skill in ("files.read", "unknown.skill"):
                once = S(skill, v)
                twice = S(skill, json.loads(dbscrub.store_dumps(once)))
                if once != twice:
                    viol.append(f"{skill}:{type(v).__name__}")
        rec.check("D5 ...including args that are not an object (list / string / number) -- a scrubbed marker is left as it is", viol == [], viol[:6])
        marker_args = S("files.read", ["x"])
        rec.check("D6 a non-object args value scrubbed once ('[omitted: 1 items]') is not rewritten on the next pass",
                  S("files.read", marker_args) == marker_args == "[omitted: 1 items]")
        viol = []
        for skill in ("files.read", "shell.run", "web.fetch", "apps.open", "whatsapp.compose", "weather.now", "test.x"):
            for v in _corpus():
                if not isinstance(v, str):
                    continue
                for echoes in ((), ("hello world", "line1")):
                    once = auditview.sanitize_result(skill, v, echoes)
                    if auditview.sanitize_result(skill, once, echoes) != once:
                        viol.append(f"result:{skill}")
        rec.check("D7 sanitize_result is a fixed point (read-back skills, ordinary skills, with and without echoes)", viol == [], sorted(set(viol)))
        viol = []
        for v in _corpus():
            if not isinstance(v, str):
                continue
            for echoes in ((), ("hello world",)):
                once = auditview.sanitize_error(v, echoes)
                if auditview.sanitize_error(once, echoes) != once:
                    viol.append("error")
            if auditview.strip_query_urls(auditview.strip_query_urls(v)) != auditview.strip_query_urls(v):
                viol.append("strip_query_urls")
        rec.check("D8 sanitize_error and strip_query_urls are fixed points", viol == [], sorted(set(viol)))
        rec.check("D9 sanitisation never raises on odd input (None, bytes, nested, huge) and never mutates its argument",
                  S("files.write", None) is None and S("files.write", {"path": b"x"}) is not None
                  and (lambda d: (S("files.write", d), d)[1] == {"path": "p", "content": "z" * 50})({"path": "p", "content": "z" * 50}))

        # the live path: audit.record + audit.complete store the same form the scrub produces
        live_bad: list[str] = []
        live_leak = False
        okmatch = True
        post = {r["id"]: r for r in table_rows(a.path, "select * from audit")}
        with iso_env.isolated_runtime():
            for o in a.audit:
                if not isinstance(o["args"], dict):
                    continue
                aid = audit.record(actor=o["actor"], skill=o["skill"], tier=o["tier"], args=o["args"], decision=o["decision"])
                if o["ok"] is not None:
                    audit.complete(aid, ok=bool(o["ok"]), result=o["result"], error=o["error"], duration_ms=o["dur"])
                row = dict(store.conn().execute("SELECT * FROM audit WHERE id=?", (aid,)).fetchone())
                if "CANARY-" in json.dumps(row):
                    live_leak = True
                if row["ok"] != o["ok"]:
                    okmatch = False
                sc = post[o["id"]]
                if (row["args"], row["result"], row["error"]) != (sc["args"], sc["result"], sc["error"]):
                    live_bad.append(o["key"])
        rec.check("D10 live audit.record/complete never store a canary in args, result or error (message bodies, typed text, echoes, URL queries)", not live_leak)
        rec.check("D11 ...and what they store equals what the scrub left in the historical row (same-as-live policy)", live_bad == [], live_bad)
        rec.check("D12 ...with the outcome 'ok' passed through unchanged (1 / 0 / NULL)", okmatch)
        with iso_env.isolated_runtime():
            live_msg = "CANARY-MSG-live1 the live echo"
            aid = audit.record(actor="text", skill="whatsapp.compose", tier="L2", args={"contact": "Sam", "message": live_msg}, decision="confirmed")
            audit.complete(aid, ok=True, result=f"Composed '{live_msg}' to Sam.", error=None)
            row = dict(store.conn().execute("SELECT * FROM audit WHERE id=?", (aid,)).fetchone())
        rec.check("D13 live: the spoken result's echo of the message is replaced by '[omitted: N chars]'",
                  row["result"] == f"Composed '[omitted: {len(live_msg)} chars]' to Sam." and json.loads(row["args"])["message"] == f"[omitted: {len(live_msg)} chars]", row["result"])


# --------------------------------------------------------------------------------------------- E: backup

def sec_E(rec: Rec, quick: bool) -> None:
    rec.section("E: the backup -- location, uniqueness, read-only, manifest, verification, restore")
    with workdir() as tmp:
        w = World(tmp)
        a, b = w.fix(), w.fix("9c1e", small=True)
        ta = w.target(a, "active", other=b)
        pre = logical(a.path)
        present = [n for n in a.removed if db_contains(a.path, n)]
        spy_calls: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def spy_mkdtemp(*args, **kw):
            spy_calls.append(str(kw.get("prefix", args[1] if len(args) > 1 else "")))
            return real_mkdtemp(*args, **kw)

        with attr(dbscrub.tempfile, "mkdtemp", spy_mkdtemp):
            res = w.apply(ta)
        bk = res.backup
        rec.check("E1 the restore rehearsal on a disposable copy really ran during apply", any("restore-rehearsal" in c for c in spy_calls), spy_calls)
        rec.check("E2 the backup folder is under the given root, named scrub-<UTC stamp>-<target>-<random>",
                  dbplace._under(bk.directory, w.backups) and re.fullmatch(r"scrub-\d{8}T\d{6}Z-active-[0-9a-f]{6}", bk.directory.name) is not None, bk.directory.name)
        rec.check("E3 ...and outside the repository, outside any synced folder, outside the database's own folder",
                  not dbplace._under(bk.directory, paths.ROOT) and not dbplace.in_synced_folder(bk.directory) and not dbplace._under(bk.directory, a.path.parent)
                  and not any(dbplace._under(bk.directory, p.parent) for p in dbplace.real_db_paths()))
        rec.check("E4 the folder holds exactly friday.db.bak and manifest.json (no partial or temp file)",
                  sorted(x.name for x in bk.directory.iterdir()) == ["friday.db.bak", "manifest.json"], [x.name for x in bk.directory.iterdir()])
        mode = bk.file.stat().st_mode
        rec.check("E5 the backup file is read-only (no write bit, os.access W_OK false)", not (mode & stat.S_IWRITE) and not os.access(bk.file, os.W_OK))
        man_text = (bk.directory / "manifest.json").read_text(encoding="utf-8")
        man = json.loads(man_text)
        rec.check("E6 manifest.json: status 'done', target, version, sha256 == recomputed hash, size, rehearsal passed",
                  man["status"] == "done" and man["target"] == "active" and man["version"] == dbscrub.SCRUB_VERSION and man["backup_sha256"] == fsha(bk.file)
                  and man["backup_bytes"] == bk.file.stat().st_size and man["restore_rehearsal"] == "passed" and bk.sha256 == fsha(bk.file))
        post = logical(a.path)
        rec.check("E7 manifest has counts_before (== pre-scrub counts) and counts_after (== post-scrub counts), rows_to_change, integrity_after",
                  man["counts_before"] == pre["counts"] and man["counts_after"] == post["counts"] and man["rows_to_change"] == {"audit": 21, "episodes": 2}
                  and man["integrity_after"] == "ok" and man["vacuumed"] is True and "finished" in man and man["database"] == str(a.path))
        rec.check("E8 the manifest contains NO stored value (no canary, no retained text, no message text)", no_leak(man_text, a))
        bsnap = logical(bk.file)
        rec.check("E9 backup integrity ok; schema, counts and logical digest EQUAL the pre-scrub database",
                  bsnap["integrity"] == "ok" and bsnap["schema"] == pre["schema"] and bsnap["counts"] == pre["counts"] and bsnap["digest"] == pre["digest"])
        rec.check("E10 ...and the backup STILL CONTAINS the canaries (it is the sensitive history) while the live database no longer does",
                  all(db_contains(bk.file, n) for n in present) and not db_contains(a.path, "CANARY-"))
        cli_src = inspect.getsource(__import__("friday.cli", fromlist=["db_cmd"]).db_cmd)
        rec.check("E11 the module docstring and the CLI help both say the backup keeps the sensitive history",
                  "keeps the sensitive history" in (dbscrub.__doc__ or "") and "keeps the sensitive history" in cli_src
                  and "never deleted by FRIDAY" in (dbscrub.__doc__ or ""))
        snap_ok = verify_ok = None
        try:
            snap_ok = dbscrub.verify_backup_file(bk.file, pre)
            verify_ok = True
        except dbscrub.ScrubError:
            verify_ok = False
        rec.check("E12 verify_backup_file passes on the good backup (with the pre-scrub snapshot) and returns its snapshot", verify_ok and snap_ok["digest"] == pre["digest"])
        bad_expected = json.loads(json.dumps(pre))
        bad_expected["counts"]["audit"] += 1
        m1 = raises(lambda: dbscrub.verify_backup_file(bk.file, bad_expected))
        bad_expected = json.loads(json.dumps(pre))
        bad_expected["digest"]["audit"] = "0" * 64
        m2 = raises(lambda: dbscrub.verify_backup_file(bk.file, bad_expected))
        bad_expected = json.loads(json.dumps(pre))
        bad_expected["schema"] = bad_expected["schema"][:-1]
        m3 = raises(lambda: dbscrub.verify_backup_file(bk.file, bad_expected))
        rec.check("E13 verify_backup_file raises on a mismatching expected snapshot (counts, digest, schema)",
                  m1 is not None and "counts" in m1 and m2 is not None and "digest" in m2 and m3 is not None and "schema" in m3, (m1, m2, m3))
        flipped = tmp / "flipped" / "friday.db.bak"
        flipped.parent.mkdir()
        data = bytearray(bk.file.read_bytes())
        at = data.find(b"CANARY-MSG-7f3a")
        flipped.write_bytes(bytes(data).replace(b"CANARY-MSG-7f3a", b"XXXXXXXXXXXXXXX"))
        rec.check("E14 a copy with flipped content bytes is rejected against the expected snapshot",
                  at > 0 and raises(lambda: dbscrub.verify_backup_file(flipped, pre)) is not None)
        trunc = tmp / "trunc" / "friday.db.bak"
        trunc.parent.mkdir()
        trunc.write_bytes(bk.file.read_bytes()[: len(data) // 3])
        zero = tmp / "zero" / "friday.db.bak"
        zero.parent.mkdir()
        zd = bytearray(bk.file.read_bytes())
        zd[:200] = b"\x00" * 200
        zero.write_bytes(bytes(zd))
        rec.check("E15 a truncated copy and a copy with a destroyed header are rejected (ScrubError, not a crash)",
                  raises(lambda: dbscrub.verify_backup_file(trunc)) is not None and raises(lambda: dbscrub.verify_backup_file(zero)) is not None)

        # restore
        dest = tmp / "restored" / "friday.db"
        bhash = fsha(bk.file)
        info = dbscrub.restore(bk.file, dest)
        rsnap = logical(dest)
        rec.check("E16 restore(backup, dest) writes a NEW file whose logical digest equals the backup's (and the pre-scrub database)",
                  dest.exists() and rsnap["digest"] == bsnap["digest"] == pre["digest"] and rsnap["integrity"] == "ok" and info["restored"] == str(dest)
                  and fsha(bk.file) == bhash)
        rec.check("E17 ...no temp file left next to it", [x.name for x in dest.parent.iterdir() if x.name != "friday.db" and not x.name.startswith("friday.db-")] == [])
        dh = fsha(dest)
        m = raises(lambda: dbscrub.restore(bk.file, dest))
        rec.check("E18 restore refuses an existing destination and never overwrites it", m is not None and "already exists" in m and fsha(dest) == dh, m)
        stranger = tmp / "stranger" / "friday.db"
        stranger.parent.mkdir()
        stranger.write_bytes(b"somebody else's file")
        sh_ = fsha(stranger)
        m = raises(lambda: dbscrub.restore(bk.file, stranger))
        rec.check("E19 ...also when the destination is not a database", m is not None and fsha(stranger) == sh_)
        m = raises(lambda: dbscrub.restore(zero, tmp / "from-corrupt" / "friday.db"))
        rec.check("E20 restoring from a corrupt backup raises and publishes nothing", m is not None and not (tmp / "from-corrupt" / "friday.db").exists())
        rt = dbscrub.preview(dbscrub.resolve_target("active", active=dest, legacy=tmp / "no-legacy" / "friday.db"))
        rec.check("E21 the restored copy is the sensitive history again: scrubbing it finds the same 21+2 rows (and a different code: new path)",
                  rt.total == 23 and rt.code != dbscrub.preview(ta).code)

        # real-db-path restore from this smoke script: guard fires (a fake 'real' location when mutating)
        if rec.mutation:
            with envpatch(LOCALAPPDATA=str(tmp / "fakelocal")):
                real_dest = dbplace.canonical_db()
                got = raises(lambda: dbscrub.restore(bk.file, real_dest), dbplace.IsolationError)
                rec.check("E22 restore into a 'real' database path from a smoke script raises IsolationError and creates nothing",
                          got is not None and not Path(real_dest).exists())
        else:
            real_dest = dbplace.canonical_db()
            got = raises(lambda: dbscrub.restore(bk.file, real_dest), dbplace.IsolationError)
            rec.check("E22 restore into the real database path from a smoke script raises IsolationError (guard fires before anything is touched)", got is not None)
            with envpatch(LOCALAPPDATA=str(tmp / "fakelocal")):
                real_dest = dbplace.canonical_db()
                got = raises(lambda: dbscrub.restore(bk.file, real_dest), dbplace.IsolationError)
                rec.check("E23 ...and into a configured 'real' location (fake LOCALAPPDATA), where nothing is created",
                          got is not None and not Path(real_dest).exists())

        # backup root rules
        a2 = w.fix("3c7d", small=True)
        t2 = w.target(a2, "active", other=b)
        c2 = w.code(t2)
        h2 = capture([a2])
        probe_root = paths.ROOT / "zz_never_created_backups"
        with attr(dbplace, "in_synced_folder", lambda p: False):  # isolate the repository rule from the OneDrive rule
            m_repo = raises(lambda: dbscrub.apply(t2, confirm=c2, backup_root=probe_root))
        probe_leftover = probe_root.exists()
        rec.check("E24 a backup root inside the repository is refused, nothing is created there, the database is unchanged",
                  m_repo is not None and "inside the repository" in m_repo and capture([a2]) == h2 and not probe_leftover, m_repo)
        m_db = raises(lambda: dbscrub.apply(t2, confirm=c2, backup_root=a2.path.parent / "bk"))
        if probe_root.exists():  # only a mutant can get here: never leave anything behind in the repository
            rm_tree(probe_root)
        rec.check("E25 a backup root inside the database's own folder is refused", m_db is not None and "database folder" in m_db and not (a2.path.parent / "bk").exists(), m_db)
        with envpatch(OneDrive=str(tmp / "OD")):
            m_od = raises(lambda: dbscrub.apply(t2, confirm=c2, backup_root=tmp / "OD" / "bk"))
        rec.check("E26 a backup root inside a cloud-synced folder is refused", m_od is not None and "cloud-synced" in m_od and not (tmp / "OD").exists(), m_od)
        real_local = dbplace.canonical_db()
        m_real = raises(lambda: dbscrub.check_backup_root(real_local.parent / "bk", t2)) if real_local is not None else "n/a"
        rec.check("E27 a backup root inside the real database's folder (LOCALAPPDATA/FRIDAY) is refused (pure check, nothing created)",
                  m_real is not None and (real_local is None or not (real_local.parent / "bk").exists()), m_real)
        m_leg = raises(lambda: dbscrub.check_backup_root(Path(paths.LEGACY_DB).parent / "bk", t2))
        rec.check("E28 ...and inside the old data/ folder", m_leg is not None)
        rec.check("E29 an acceptable root is returned canonicalised and is not created by the check",
                  dbscrub.check_backup_root(tmp / "fine" / "bk", t2) == Path(os.path.realpath(tmp / "fine" / "bk")) and not (tmp / "fine").exists())
        with envpatch(LOCALAPPDATA=str(tmp / "la")):
            d = dbscrub.default_backup_root()
            rec.check("E30 the default root is %LOCALAPPDATA%/FRIDAY-backups, and that passes the root rules",
                      d == tmp / "la" / "FRIDAY-backups" and dbscrub.check_backup_root(d, t2) == Path(os.path.realpath(d)))
        rec.check("E31 after all those refusals the database is still unchanged and no backup folder exists for it", capture([a2]) == h2)

        # uniqueness + no overwrite
        a3, a4 = w.fix("4a1b", small=True), w.fix("4a1c", small=True)
        r3 = w.apply(w.target(a3, "active", other=b))
        r4 = w.apply(w.target(a4, "legacy", other=b))
        rec.check("E32 two applies never share a backup folder", r3.backup.directory != r4.backup.directory and r3.backup.file != r4.backup.file)
        a5 = w.fix("4a1d", small=True)
        t5 = w.target(a5, "active", other=b)
        fixed_dt = SimpleNamespace(now=lambda tz=None: __import__("datetime").datetime(2030, 1, 2, 3, 4, 5, tzinfo=__import__("datetime").timezone.utc))
        fixed_sec = SimpleNamespace(token_hex=lambda n=3: "abcdef")
        pre_made = w.backups / "scrub-20300102T030405Z-active-abcdef"
        pre_made.mkdir(parents=True)
        (pre_made / "sentinel.txt").write_text("keep me")
        h5 = capture([a5])
        code5 = w.code(t5)
        with attr(dbscrub, "datetime", fixed_dt), attr(dbscrub, "secrets", fixed_sec):
            m = raises(lambda: dbscrub.apply(t5, confirm=code5, backup_root=w.backups))
        rec.check("E33 a backup folder that already exists is never reused (mkdir exist_ok=False): apply fails, the sentinel survives, database unchanged",
                  m is not None and [x.name for x in pre_made.iterdir()] == ["sentinel.txt"] and fsha(a5.path) == h5[0][0], m)
        real_du = shutil.disk_usage

        def du_plant(path):
            (Path(path) / "friday.db.bak").write_text("an earlier backup")
            return real_du(path)

        with attr(dbscrub.shutil, "disk_usage", du_plant):
            m = raises(lambda: dbscrub.apply(t5, confirm=code5, backup_root=w.backups))
        planted = [p for p in w.backups.glob("scrub-*-active-*/friday.db.bak") if p.read_text(errors="ignore") == "an earlier backup"]
        rec.check("E34 an existing friday.db.bak is never overwritten: apply refuses, the earlier file is intact, database unchanged",
                  m is not None and "refusing to overwrite" in m and len(planted) == 1 and fsha(a5.path) == h5[0][0], m)
        with attr(dbscrub.shutil, "disk_usage", lambda p: SimpleNamespace(total=10, used=10, free=1000)):
            m = raises(lambda: dbscrub.apply(t5, confirm=code5, backup_root=w.backups))
        rec.check("E35 not enough free disk space -> ScrubError ('nothing was changed'), no backup published, database unchanged",
                  m is not None and "not enough free space" in m and fsha(a5.path) == h5[0][0], m)
        notdir = tmp / "iamafile"
        notdir.write_text("x")
        m = raises(lambda: dbscrub.apply(t5, confirm=code5, backup_root=notdir / "sub"))
        rec.check("E36 an impossible backup location (parent is a file) -> ScrubError and the database is unchanged", m is not None and fsha(a5.path) == h5[0][0], m)
        rec.check("E37 after every failed attempt no friday.db.bak other than the planted one exists, and the database still holds its canaries",
                  len(list(w.backups.glob("**/friday.db.bak"))) == 4 and db_contains(a5.path, "CANARY-MSG-4a1d"))
        # restore rehearsal that cannot reproduce the backup aborts BEFORE any change
        real_snapshot = dbscrub.snapshot

        def snap_bad_on_rehearsal(c):
            s = real_snapshot(c)
            files = [r[2] for r in c.execute("PRAGMA database_list")]
            if any(os.path.basename(f or "") == "rehearsal.db" for f in files):
                s = dict(s, digest=dict(s["digest"], audit="0" * 64))
            return s

        with attr(dbscrub, "snapshot", snap_bad_on_rehearsal):
            m = raises(lambda: dbscrub.apply(t5, confirm=code5, backup_root=tmp / "bk-rehearsal"))
        rec.check("E38 a failing restore rehearsal aborts apply before any change ('did not reproduce'); the database is byte-identical",
                  m is not None and "did not reproduce" in m and fsha(a5.path) == h5[0][0] and db_contains(a5.path, "CANARY-MSG-4a1d"), m)
        if not quick:
            a6 = w.fix("6e6e", small=True)
            c6 = dbscrub.preview(w.target(a6, "active", other=b)).code
            r = run_cli(["db", "scrub", "--apply", "--target", "active", "--confirm", c6, "--backup-dir", str(tmp / "clibk")], a6.path)
            out = r.stdout + r.stderr
            bks = list((tmp / "clibk").glob("scrub-*/friday.db.bak"))
            rec.check("E39 subprocess: the full CLI apply exits 0, prints the backup path, sha256, 'restore rehearsal: passed' and 'keeps the sensitive history'; no stored value",
                      r.returncode == 0 and len(bks) == 1 and "keeps the sensitive history" in out and "sha256=" in out and "restore rehearsal: passed" in out
                      and no_leak(out, a6) and not db_contains(a6.path, "CANARY-"), (r.returncode, out[-300:]))
            r2 = run_cli(["db", "verify-backup", "--backup", str(bks[0])], None)
            rec.check("E40 subprocess: `db verify-backup` passes on the new backup", r2.returncode == 0 and "backup OK" in r2.stdout, (r2.returncode, r2.stdout[-120:], r2.stderr[-200:]))
            to = tmp / "cli-restored" / "friday.db"
            r3 = run_cli(["db", "restore", "--backup", str(bks[0]), "--to", str(to)], None)
            r4 = run_cli(["db", "restore", "--backup", str(bks[0]), "--to", str(to)], None)
            rsh = fsha(to) if to.exists() else ""
            rec.check("E41 subprocess: `db restore` writes the new file (exit 0); a second run refuses (exit 1) and the file is unchanged",
                      r3.returncode == 0 and r4.returncode == 1 and "already exists" in (r4.stdout + r4.stderr) and fsha(to) == rsh and rsh != "", (r3.returncode, r4.returncode, r3.stderr[-200:]))
            r5 = run_cli(["db", "restore", "--backup", str(bks[0])], None)
            r6 = run_cli(["db", "verify-backup"], None)
            rec.check("E42 subprocess: restore without --to and verify-backup without --backup exit 2", r5.returncode == 2 and r6.returncode == 2, (r5.returncode, r6.returncode))
            r7 = run_cli(["db", "verify-backup", "--backup", str(zero)], None)
            rec.check("E43 subprocess: verify-backup of a corrupt copy exits 1", r7.returncode == 1, (r7.returncode, r7.stdout[-120:], r7.stderr[-200:]))


# --------------------------------------------------------------------------------------------- F: failure handling

KILL_SRC = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from friday import dbscrub
db, bk, point = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
t = dbscrub.resolve_target("active", active=db, legacy=db.parent.parent / "no-legacy" / "friday.db")
code = dbscrub.preview(t).code
def fault(p):
    if p == point:
        os._exit(9)
dbscrub._FAULT = fault
dbscrub.apply(t, confirm=code, backup_root=bk)
print("COMPLETED")
'''

WAL_WRITER_SRC = '''
import json, os, sqlite3, sys
db, tag = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db)
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA wal_autocheckpoint=0")
msg = "CANARY-WALROW-" + tag + " typed inside a writer process"
c.execute("INSERT INTO audit (at, actor, skill, tier, args, decision, ok, result) VALUES (?,?,?,?,?,?,?,?)",
          ("2026-02-01T00:00:00+00:00", "text", "whatsapp.compose", "L2", json.dumps({"contact": "Sam", "message": msg}), "confirmed", 1,
           "Composed '" + msg + "' to Sam."))
c.commit()
os._exit(0)
'''

LOCK_HOLDER_SRC = '''
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=1)
c.execute("BEGIN IMMEDIATE")
c.execute("INSERT INTO audit (at, actor, skill, tier, args, decision) VALUES ('2026-02-02','text','apps.open','L1','{}','auto')")
print("READY", flush=True)
sys.stdin.read()
'''


def poisoned_changes(kind: str, fx: Fix):
    real = dbscrub.changes

    def gen(c):
        yield from real(c)
        if kind == "kept-column":
            yield dbscrub.Change("audit", fx.audit_ids["harm1"], {"ok": 0})
        elif kind == "retained-table":
            yield dbscrub.Change("goals", f"g-parent-{fx.tag}", {"objective": "tampered"})
        elif kind == "ids":
            yield dbscrub.Change("audit", fx.audit_ids["harm2"], {"id": 99999})
        elif kind == "toggle":
            row = c.execute("SELECT result FROM audit WHERE id=?", (fx.audit_ids["harm1"],)).fetchone()
            yield dbscrub.Change("audit", fx.audit_ids["harm1"], {"result": "B" if row["result"] == "A" else "A"})
    return gen


def sec_F(rec: Rec, quick: bool) -> None:
    rec.section("F: failure handling -- a fault at every stage leaves the database unchanged or scrubbed-and-verified; the backup stays")
    with workdir() as tmp:
        w = World(tmp)
        b = w.fix("9c1e", small=True)
        n = 0
        for point in ("after-backup-copy", "after-backup", "mid-scrub", "before-commit"):
            n += 1
            a = w.fix(name=f"f{n}")
            t = w.target(a, "active", other=b)
            code = w.code(t)
            pre_hash, pre_logical, pre_snap = fsha(a.path), logical_ro_sorted(a.path), logical(a.path)
            dirs_before = set(w.backup_dirs())
            with attr(dbscrub, "_FAULT", fault_at(point)):
                msg = raises(lambda: dbscrub.apply(t, confirm=code, backup_root=w.backups))
            new_dirs = sorted(set(w.backup_dirs()) - dirs_before)
            bak = (w.backups / new_dirs[0] / "friday.db.bak") if new_dirs else None
            rec.check(f"F1[{point}] the failure surfaces as ScrubError saying the database was NOT changed, with no stored value in the message",
                      msg is not None and "NOT changed" in msg and "CANARY-" not in msg and "Boom" in msg, msg)
            rec.check(f"F2[{point}] database bytes AND logical digest are identical to before; the canaries are still there",
                      fsha(a.path) == pre_hash and logical_ro_sorted(a.path) == pre_logical and db_contains(a.path, "CANARY-MSG-7f3a"))
            if point == "after-backup-copy":
                rec.check(f"F3[{point}] the incomplete backup was never published as friday.db.bak",
                          len(new_dirs) == 1 and bak is not None and not bak.exists() and "backup is at" not in (msg or ""))
            else:
                ok_verify = False
                try:
                    dbscrub.verify_backup_file(bak, pre_snap)
                    ok_verify = True
                except Exception:  # noqa: BLE001
                    pass
                man = json.loads((w.backups / new_dirs[0] / "manifest.json").read_text(encoding="utf-8")) if new_dirs else {}
                rec.check(f"F3[{point}] a verified backup of the pre-scrub database exists, the manifest says 'aborted-database-unchanged' and the error names the file",
                          len(new_dirs) == 1 and ok_verify and man.get("status") == "aborted-database-unchanged" and str(bak) in (msg or ""), (new_dirs, man.get("status")))
            lock_ok = True
            try:
                cx = sqlite3.connect(str(a.path), timeout=0.5)
                cx.execute("BEGIN IMMEDIATE")
                cx.rollback()
                cx.close()
            except sqlite3.Error:
                lock_ok = False
            r = None
            try:
                r = dbscrub.apply(t, confirm=code, backup_root=w.backups)
            except dbscrub.ScrubError:
                pass
            rec.check(f"F4[{point}] the exclusive lock was released and a fresh apply then succeeds (recoverable)",
                      lock_ok and r is not None and not db_contains(a.path, "CANARY-") and logical(a.path)["integrity"] == "ok")

        # committed, then a fault: the error says so and names the backup; restore reproduces the pre-scrub database
        a = w.fix(name="f-commit")
        t = w.target(a, "active", other=b)
        code = w.code(t)
        pre_snap = logical(a.path)
        dirs_before = set(w.backup_dirs())
        with attr(dbscrub, "_FAULT", fault_at("after-commit")):
            msg = raises(lambda: dbscrub.apply(t, confirm=code, backup_root=w.backups))
        new_dirs = sorted(set(w.backup_dirs()) - dirs_before)
        bak = w.backups / new_dirs[0] / "friday.db.bak"
        man = json.loads((w.backups / new_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
        rec.check("F5 a fault AFTER commit: the message says the scrub was committed and names the verified backup",
                  msg is not None and "committed" in msg and str(bak) in msg and "NOT changed" not in msg, msg)
        rec.check("F6 ...the database IS scrubbed (no canary anywhere, integrity ok) and the manifest status is 'committed-unverified'",
                  not db_contains(a.path, "CANARY-") and logical(a.path)["integrity"] == "ok" and man["status"] == "committed-unverified")
        dest = tmp / "after-commit-restore" / "friday.db"
        dbscrub.restore(bak, dest)
        rec.check("F7 restore from that backup reproduces the pre-scrub digest", logical(dest)["digest"] == pre_snap["digest"] and db_contains(dest, "CANARY-MSG-7f3a"))
        rec.check("F8 the scrubbed database is a fixed point: a preview shows 0 rows and a re-apply says nothing to scrub",
                  dbscrub.preview(t).total == 0 and "nothing to scrub" in (raises(lambda: dbscrub.apply(t, confirm=dbscrub.preview(t).code, backup_root=w.backups)) or ""))

        # post-commit verification fails: the scrub IS committed; the backup is named; manifest says FAILED
        a = w.fix(name="f-postverify")
        t = w.target(a, "active", other=b)
        code = w.code(t)
        calls = {"n": 0}
        real_any = dbscrub._any_change

        def any_change_lies(c):
            calls["n"] += 1
            return calls["n"] > 1  # pre-commit: "fixed point"; after re-opening: "still unscrubbed rows"

        dirs_before = set(w.backup_dirs())
        with attr(dbscrub, "_any_change", any_change_lies):
            msg = raises(lambda: dbscrub.apply(t, confirm=code, backup_root=w.backups))
        nd = sorted(set(w.backup_dirs()) - dirs_before)
        man = json.loads((w.backups / nd[0] / "manifest.json").read_text(encoding="utf-8"))
        rec.check("F9 post-commit verification failure: 'committed but post-verification failed', backup + restore command named, manifest status FAILED",
                  msg is not None and "committed" in msg and "post-verification failed" in msg and "db restore" in msg
                  and man["status"] == "committed-verification-FAILED" and man["problems"] and real_any is dbscrub._any_change, msg)

        # pre-commit verification: the scrub refuses to commit anything but the intended columns
        labels = {"kept-column": "must not change", "retained-table": "must not change", "ids": "ids changed", "toggle": "a second pass"}
        for kind, needle in labels.items():
            a = w.fix(name=f"f-poison-{kind}")
            t = w.target(a, "active", other=b)
            with attr(dbscrub, "changes", poisoned_changes(kind, a)):
                code = w.code(t)
                pre_hash, pre_logical = fsha(a.path), logical_ro_sorted(a.path)
                dirs_before = set(w.backup_dirs())
                msg = raises(lambda: dbscrub.apply(t, confirm=code, backup_root=w.backups))
            nd = sorted(set(w.backup_dirs()) - dirs_before)
            rec.check(f"F10[{kind}] pre-commit verification catches it: ScrubError 'pre-commit verification failed' ({needle}); nothing is committed",
                      msg is not None and "pre-commit verification failed" in msg and needle in msg and fsha(a.path) == pre_hash
                      and logical_ro_sorted(a.path) == pre_logical and db_contains(a.path, "CANARY-MSG-7f3a") and len(nd) == 1, msg)

        # rollback really happens (not just a closed connection): SQL spy
        a = w.fix(name="f-rollback")
        t = w.target(a, "active", other=b)
        code = w.code(t)
        spy = SqlSpy()
        with attr(dbscrub, "sqlite3", spy.module()), attr(dbscrub, "_FAULT", fault_at("mid-scrub")):
            msg = raises(lambda: dbscrub.apply(t, confirm=code, backup_root=w.backups))
        rec.check("F11 a failure mid-transaction issues ROLLBACK (and never COMMIT)",
                  msg is not None and "ROLLBACK" in spy.log and "COMMIT" not in spy.log and spy.log.index("ROLLBACK") > spy.log.index("BEGIN IMMEDIATE"), spy.log[-4:])

        if quick:
            return
        # real hard kills
        script = tmp / "killtest_x.py"
        script.write_text(KILL_SRC, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("FRIDAY_ALLOW_REAL_DB", "FRIDAY_DB")}
        env["FRIDAY_REFUSE_REAL_DB"] = "1"
        for point in ("mid-scrub", "before-commit", "after-commit"):
            k = w.fix(name=f"kill-{point}")
            pre = logical(k.path)
            kb = tmp / f"killbk-{point}"
            r = subprocess.run([PY, str(script), str(ROOT), str(k.path), str(kb), point], capture_output=True, text=True, env=env, cwd=str(tmp), timeout=180)
            cx = sqlite3.connect(str(k.path))
            integ = cx.execute("PRAGMA integrity_check").fetchone()[0]
            cx.close()
            post = logical(k.path)
            dirs = sorted(kb.glob("scrub-*")) if kb.exists() else []
            baks = [d / "friday.db.bak" for d in dirs]
            man = json.loads((dirs[0] / "manifest.json").read_text(encoding="utf-8")) if dirs else {}
            ver = False
            if baks:
                try:
                    dbscrub.verify_backup_file(baks[0], pre)
                    ver = True
                except Exception:  # noqa: BLE001
                    pass
            if point != "after-commit":
                rec.check(f"F12[{point}] hard kill (os._exit): process dies with 9, SQLite rolls the transaction back -- integrity ok, logical digest identical",
                          r.returncode == 9 and "COMPLETED" not in r.stdout and integ == "ok" and post["digest"] == pre["digest"] and db_contains(k.path, "CANARY-MSG-7f3a"),
                          (r.returncode, integ, r.stderr[-200:]))
                rec.check(f"F13[{point}] ...the verified backup exists; the manifest still says 'backup-verified' (a killed process cannot update it)", len(dirs) == 1 and ver and man.get("status") == "backup-verified")
                again = w.target(k, "active", other=b)
                try:
                    res = dbscrub.apply(again, confirm=dbscrub.preview(again).code, backup_root=kb)
                except dbscrub.ScrubError as exc:
                    res, why = None, str(exc)
                rec.check(f"F14[{point}] a fresh apply then succeeds on the same database", res is not None and not db_contains(k.path, "CANARY-") and len(list(kb.glob("scrub-*"))) == 2)
            else:
                rec.check("F12[after-commit] hard kill after COMMIT: process dies with 9; the database is scrubbed (no canary in any column), integrity ok",
                          r.returncode == 9 and integ == "ok" and not db_contains(k.path, "CANARY-"), (r.returncode, integ, r.stderr[-200:]))
                rec.check("F13[after-commit] ...manifest says 'committed-unverified'; the backup verifies and restores to the pre-scrub digest",
                          len(dirs) == 1 and ver and man.get("status") == "committed-unverified"
                          and (dbscrub.restore(baks[0], tmp / "kill-restored" / "friday.db") is not None) and logical(tmp / "kill-restored" / "friday.db")["digest"] == pre["digest"])
                kt = w.target(k, "active", other=b)
                rec.check("F14[after-commit] ...and the scrubbed database is verified clean (preview 0 rows)", dbscrub.preview(kt).total == 0)
                d_kill = logical(k.path)["digest"]
                dbscrub.compact(kt)
                rec.check("F15[after-commit] after the kill `compact` purges the file (no canary in the raw bytes) without any logical change",
                          not raw_contains(k.path, "CANARY-") and logical(k.path)["digest"] == d_kill)


# --------------------------------------------------------------------------------------------- G: WAL / locking / guard

def sec_G(rec: Rec, quick: bool) -> None:
    rec.section("G: a second connection stops apply; stale WAL/SHM are left to SQLite; preview works under a write lock; the guard")
    with workdir() as tmp:
        w = World(tmp)
        b = w.fix("9c1e", small=True)

        def locked_case(label: str, hold):
            a = w.fix(name=f"lock-{label}")
            t = w.target(a, "active", other=b)
            code = w.code(t)
            other = sqlite3.connect(str(a.path), timeout=0.2)
            hold(other)
            side_before = sidecars(a.path)
            hash_before = fsha(a.path)
            dirs_before = w.backup_dirs()
            t0 = time.perf_counter()
            msg = raises(lambda: dbscrub.apply(t, confirm=code, backup_root=w.backups))
            waited = time.perf_counter() - t0
            alive = True
            try:
                other.execute("SELECT COUNT(*) FROM audit").fetchone()
            except sqlite3.Error:
                alive = False
            rec.check(f"G1[{label}] apply refuses ('in use', 'Nothing was changed'), the database is byte-identical, no backup folder",
                      msg is not None and "in use" in msg and "Nothing was changed" in msg and fsha(a.path) == hash_before and w.backup_dirs() == dirs_before
                      and "CANARY-" not in msg, (msg, waited))
            rec.check(f"G2[{label}] -wal / -shm files were NOT deleted by the refused attempt (same sidecars before and after), and the other connection still works",
                      sidecars(a.path) == side_before and alive, (side_before, sidecars(a.path)))
            try:
                other.rollback()
            except sqlite3.Error:
                pass
            other.close()
            ok = False
            try:
                dbscrub.apply(t, confirm=code, backup_root=w.backups)
                ok = True
            except dbscrub.ScrubError:
                pass
            rec.check(f"G3[{label}] once the other connection is closed the same apply succeeds", ok and not db_contains(a.path, "CANARY-"))

        locked_case("idle", lambda c: c.execute("SELECT COUNT(*) FROM audit").fetchone())
        locked_case("read-txn", lambda c: (c.execute("BEGIN"), c.execute("SELECT COUNT(*) FROM audit").fetchone()))
        locked_case("write-lock", lambda c: (c.execute("BEGIN IMMEDIATE"), c.execute("INSERT INTO audit (at, actor, skill, tier, args, decision) VALUES ('2026-02-02','text','apps.open','L1','{}','auto')")))

        # preview while another connection holds a write lock: works, read-only, sees only committed rows
        a = w.fix(name="plock")
        t = w.target(a, "active", other=b)
        base = dbscrub.preview(t)
        holder = sqlite3.connect(str(a.path), timeout=0.2)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO audit (at, actor, skill, tier, args, decision) VALUES ('2026-02-02','text','input.type','L2',?, 'auto')",
                       (json.dumps({"text": "CANARY-TYPED-uncommitted writer row"}),))
        hb = fsha(a.path)
        pv = dbscrub.preview(t)
        rec.check("G4 preview while another connection holds a write lock still works, is read-only and sees only committed rows",
                  pv.error == "" and pv.total == base.total and pv.code == base.code and pv.counts["audit"] == base.counts["audit"] and fsha(a.path) == hb)
        holder.rollback()
        holder.close()

        # source claims: nothing here unlinks a -wal/-shm
        src = inspect.getsource(dbscrub)
        risky = [ln.strip() for ln in src.splitlines() if re.search(r"unlink|os\.remove|rmtree|rename|os\.replace", ln) and re.search(r"wal|shm|sidecar|journal", ln, re.I)]
        rec.check("G5 dbscrub's source never unlinks / removes / renames a -wal, -shm or -journal file, and says sidecars are never deleted by it",
                  risky == [] and "never deleted" in src and "left to SQLite" in src, risky)

        if not quick:
            # stale sidecars: a writer process that died without closing leaves committed rows only in the -wal
            sw = w.fix(name="stale-wal")
            script = tmp / "walwriter_x.py"
            script.write_text(WAL_WRITER_SRC, encoding="utf-8")
            r = subprocess.run([PY, str(script), str(sw.path), sw.tag], capture_output=True, text=True, cwd=str(tmp), timeout=120)
            side0 = sidecars(sw.path)
            wal_bytes = Path(str(sw.path) + "-wal").stat().st_size if side0["-wal"] else 0
            t = w.target(sw, "active", other=b)
            pv = dbscrub.preview(t)
            rec.check("G6 (setup) a dead writer left a non-empty -wal and -shm behind", r.returncode == 0 and side0["-wal"] and side0["-shm"] and wal_bytes > 0, (r.returncode, side0, wal_bytes))
            rec.check("G7 preview sees the rows that live only in the -wal (one more audit row to change than the base fixture)",
                      pv.counts["audit"] == len(sw.audit) + 1 and pv.rows_to_change["audit"] == sw.changing_audit + 1, (pv.counts, pv.rows_to_change))
            deleted: list[str] = []
            real_unlink, real_remove, real_rmdir = os.unlink, os.remove, os.rmdir

            def spy(fn):
                def inner(p, *a_, **k_):
                    deleted.append(os.fspath(p))
                    return fn(p, *a_, **k_)
                return inner

            with attr(os, "unlink", spy(real_unlink)), attr(os, "remove", spy(real_remove)), attr(os, "rmdir", spy(real_rmdir)):
                res = dbscrub.apply(t, confirm=pv.code, backup_root=w.backups)
            rec.check("G8 apply scrubs the -wal-only row too: the canary is gone from columns and raw bytes (db + any sidecar left)",
                      not db_contains(sw.path, "CANARY-") and not raw_contains(sw.path, "CANARY-WALROW") and res.rows_changed["audit"] == sw.changing_audit + 1)
            rec.check("G9 dbscrub itself never deleted a -wal / -shm (any change to them was SQLite's own checkpoint)",
                      not [d for d in deleted if d.endswith(("-wal", "-shm", "-journal"))], [Path(d).name for d in deleted][:6])
            wal_row = table_rows(sw.path, "select * from audit order by id desc limit 1")[0]
            rec.check("G10 the -wal-only row keeps its id, skill, ok and result shape", wal_row["skill"] == "whatsapp.compose" and wal_row["ok"] == 1
                      and wal_row["result"].startswith("Composed '[omitted: ") and json.loads(wal_row["args"])["contact"] == "Sam")

            # a lock held by ANOTHER PROCESS
            lk = w.fix(name="proc-lock")
            lscript = tmp / "lockholder_x.py"
            lscript.write_text(LOCK_HOLDER_SRC, encoding="utf-8")
            lt = w.target(lk, "active", other=b)
            base = dbscrub.preview(lt)
            proc = subprocess.Popen([PY, str(lscript), str(lk.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(tmp))
            try:
                line = proc.stdout.readline().strip()
                hb = fsha(lk.path)
                pv = dbscrub.preview(lt)
                m = raises(lambda: dbscrub.apply(lt, confirm=base.code, backup_root=w.backups))
                rec.check("G11 another PROCESS holding a write lock: preview still works read-only; apply refuses with 'in use' and changes nothing",
                          line == "READY" and pv.error == "" and pv.total == base.total and m is not None and "in use" in m and fsha(lk.path) == hb, (line, m))
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                proc.wait(timeout=30)

        # ---------------------------------------------------------------- the real-database guard
        a = w.fix(name="guard-ok")
        t = w.target(a, "active", other=b)
        try:
            dbplace.guard(a.path)
            fine = True
        except dbplace.IsolationError:
            fine = False
        rec.check("G12 dbplace.guard allows a throwaway database from this smoke script (and it is a test entry point)", fine and dbplace.is_test_entrypoint())
        with envpatch(LOCALAPPDATA=str(tmp / "fakela")):
            fake_real = dbplace.canonical_db()
            fx = build_db(fake_real, "fa4e", small=True)
            tt = dbscrub.Target("active", fake_real, False, False)
            h = fsha(fake_real)
            dirs_g = w.backup_dirs()
            code_fake = "0123456789"
            r1 = raises(lambda: dbscrub.preview(tt), dbplace.IsolationError)
            r2 = raises(lambda: dbscrub.apply(tt, confirm=code_fake, backup_root=w.backups), dbplace.IsolationError)
            r3 = raises(lambda: dbscrub.verify_backup_file(fake_real), dbplace.IsolationError)
            r4 = raises(lambda: dbscrub.restore(fake_real, tmp / "never" / "friday.db"), dbplace.IsolationError)
            rec.check("G13 a 'real' location (fake LOCALAPPDATA here): preview, apply, verify_backup_file and restore(source=real) all raise IsolationError",
                      all(x is not None for x in (r1, r2, r3, r4)), (r1 is not None, r2 is not None, r3 is not None, r4 is not None))
            rec.check("G13b ...nothing was opened, written or created (hash identical, no new backup folder, no restore target, no sidecars)",
                      fsha(fake_real) == h and not (tmp / "never").exists() and w.backup_dirs() == dirs_g
                      and sidecars(fake_real) == {"-wal": False, "-shm": False, "-journal": False})
        fake_legacy = tmp / "fake-legacy" / "friday.db"
        build_db(fake_legacy, "fa4f", small=True)
        with attr(paths, "LEGACY_DB", fake_legacy):
            tl = dbscrub.resolve_target("legacy", active=a.path)
            h = fsha(fake_legacy)
            dirs_g = w.backup_dirs()
            r1 = raises(lambda: dbscrub.preview(tl), dbplace.IsolationError)
            r2 = raises(lambda: dbscrub.apply(tl, confirm="0123456789", backup_root=w.backups), dbplace.IsolationError)
        rec.check("G14 the 'legacy' location (paths.LEGACY_DB) is guarded the same way: preview and apply raise IsolationError, file untouched",
                  r1 is not None and r2 is not None and fsha(fake_legacy) == h and w.backup_dirs() == dirs_g)
        if not rec.mutation:
            checks = []
            for name, rp in (("active", dbplace.canonical_db()), ("legacy", Path(paths.LEGACY_DB))):
                if rp is None:
                    continue
                rt = dbscrub.Target(name, Path(rp), False, False)
                present = Path(rp).is_file()
                try:
                    pvr = dbscrub.preview(rt)
                    outcome = "no-file" if not pvr.exists else "opened"
                except dbplace.IsolationError:
                    outcome = "refused"
                msg = ""
                try:
                    dbscrub.apply(rt, confirm="0123456789", backup_root=w.backups)
                    outcome2 = "applied"
                except dbplace.IsolationError:
                    outcome2 = "refused"
                except dbscrub.ScrubError:
                    outcome2 = "no-file"
                try:
                    dbscrub.verify_backup_file(Path(rp))
                    outcome3 = "opened"
                except dbplace.IsolationError:
                    outcome3 = "refused"
                except dbscrub.ScrubError:
                    outcome3 = "other"
                want = ("refused", "refused", "refused") if present else ("no-file", "no-file", "refused")
                checks.append(((outcome, outcome2, outcome3) == want) or (not present and outcome == "no-file" and outcome2 == "no-file"))
            rec.check("G15 the REAL active and legacy database paths: preview / apply / verify_backup_file raise IsolationError before anything is opened", all(checks) and bool(checks))
            rec.check("G16 ...and nothing was created in the default backup root by any of it", not (dbscrub.default_backup_root() / "x").exists()
                      and not any(dbscrub.default_backup_root().glob("scrub-*")) if dbscrub.default_backup_root().exists() else True)


# --------------------------------------------------------------------------------------------- H: real data untouched

def _fp(p: Path | None):
    if p is None or not Path(p).exists():
        return None
    s = Path(p).stat()
    return (s.st_size, s.st_mtime_ns)


def _tree(p: Path | None):
    if p is None or not Path(p).exists():
        return None
    return sorted((str(x.relative_to(p)), x.stat().st_size) for x in Path(p).rglob("*") if x.is_file())


def real_fingerprint() -> dict:
    """stat() only -- nothing is opened."""
    canon = dbplace.canonical_db()
    return {
        "active": _fp(canon), "legacy": _fp(Path(paths.LEGACY_DB)),
        "store": _tree(dbplace.canonical_store()), "legacy_store": _tree(Path(paths.LEGACY_SNAPSHOTS)),
        "backups": _tree(dbscrub.default_backup_root()),
        "repo_probe": (paths.ROOT / "zz_never_created_backups").exists(),
    }


def sec_H(rec: Rec, before: dict) -> None:
    rec.section("H: the user's real databases, recovery stores and default backup root were not touched")
    after = real_fingerprint()
    rec.check("H1 the real per-user database file is unchanged (size and mtime; stat only)", before["active"] == after["active"], (before["active"], after["active"]))
    rec.check("H2 the old data/friday.db is unchanged (size and mtime)", before["legacy"] == after["legacy"], (before["legacy"], after["legacy"]))
    rec.check("H3 the per-user recovery store and the old data/snapshots are unchanged", before["store"] == after["store"] and before["legacy_store"] == after["legacy_store"])
    rec.check("H4 nothing was created in the default backup root (%LOCALAPPDATA%/FRIDAY-backups)", before["backups"] == after["backups"])
    rec.check("H5 nothing was created inside the repository by the backup-root refusals", not after["repo_probe"])


# --------------------------------------------------------------------------------------------- K: review hardening

class FailVacuum(sqlite3.Connection):
    """A connection whose VACUUM fails (disk full), to drive the 'committed but not purged' path."""

    def execute(self, sql, *a, **k):  # noqa: D401
        if str(sql).strip().upper().startswith("VACUUM"):
            raise sqlite3.OperationalError("database or disk is full")
        return super().execute(sql, *a, **k)


def _shim(cls):
    class Shim:
        def connect(self, *a, **k):
            k.setdefault("factory", cls)
            return sqlite3.connect(*a, **k)

        def __getattr__(self, name):
            return getattr(sqlite3, name)

    return Shim()


def _newest_manifest(w: World, before: set) -> tuple[Path, dict]:
    nd = sorted(set(w.backup_dirs()) - before)
    d = w.backups / nd[-1]
    return d, json.loads((d / "manifest.json").read_text(encoding="utf-8"))


def sec_K(rec: Rec, quick: bool) -> None:
    rec.section("K: hardening -- exact markers, any-scheme URLs, withheld error text, bounded names, NOT-PURGED, compact, restore guards, live parity")
    from friday.intelligence import episodes

    with workdir() as tmp:
        w = World(tmp)
        b = w.fix("9c1e", small=True)

        # ------------------------------------------------ names that come from stored rows are never echoed
        odd = w.fix("0dd1", small=True)
        oc = sqlite3.connect(str(odd.path))
        for skill, args in (("evil CANARY-SKILLNAME-0dd1 token", {"CANARY-KEYNAME-0dd1 with space": "CANARY-VAL-0dd1 a long enough value"}),
                            ("test.CANARY-SKILLB-0dd1 x", {"text": "CANARY-VALB-0dd1 another long value"}),
                            ("tool.a.b.c.d.e.f.CANARY-SKILLC-0dd1", {"q": "CANARY-VALC-0dd1 yet another one"})):
            oc.execute("INSERT INTO audit (at, actor, skill, tier, args, decision, ok, result) VALUES ('2026-04-01','text',?,'L1',?,'auto',1,'ok')",
                       (skill, json.dumps(args)))
        oc.commit()
        oc.close()
        to = w.target(odd, "active", other=b)
        pv = dbscrub.preview(to)
        text = "\n".join(dbscrub.format_preview(pv))
        rec.check("K1 skill and argument names taken from stored rows are shown as '(other)' unless they are plain dotted identifiers; none is echoed",
                  "CANARY-" not in text and "(other)" in text and "SKILLNAME" not in text and "KEYNAME" not in text, text[:200])
        before_man = set(w.backup_dirs())
        ro = w.apply(to)
        _d, man = _newest_manifest(w, before_man)
        man_text = json.dumps(man)
        rec.check("K2 ...also in the manifest; the values of those rows are gone from the file (the skill-name column itself is not a scrubbed column)",
                  "CANARY-" not in man_text and not any(raw_contains(odd.path, n) for n in ("CANARY-VAL-0dd1", "CANARY-VALB-0dd1", "CANARY-VALC-0dd1")) and ro.purged)

        # ------------------------------------------------ a stored value that is not valid UTF-8 never reaches any output
        bad = w.fix("0bad", small=True)
        bc = sqlite3.connect(str(bad.path))
        bc.execute("INSERT INTO audit (at, actor, skill, tier, args, decision, ok, result) VALUES ('2026-04-02','text','input.type','L2',CAST(? AS TEXT),'auto',1,'ok')",
                   (b'{"text": "CANARY-NONUTF8-0bad \xff\xfe stored bytes"}',))
        bc.commit()
        bc.close()
        tbad = w.target(bad, "active", other=b)
        hb = fsha(bad.path)
        pvb = dbscrub.preview(tbad)
        textb = "\n".join(dbscrub.format_preview(pvb))
        rec.check("K3 preview of a database holding a non-UTF-8 text value reports 'details withheld' and prints no stored text",
                  pvb.error != "" and "details withheld" in pvb.error and "CANARY-" not in textb + pvb.error and "unavailable" in textb, pvb.error[:120])
        dirs_k4 = w.backup_dirs()
        mb = raises(lambda: dbscrub.apply(tbad, confirm="0123456789", backup_root=w.backups))
        rec.check("K4 apply on it raises ScrubError ('cannot inspect') without the text, changes nothing, makes no backup",
                  mb is not None and "cannot inspect" in mb and "CANARY-" not in mb and fsha(bad.path) == hb and w.backup_dirs() == dirs_k4, mb)

        # ------------------------------------------------ sanitizers on odd inputs: exact markers, fixed points, any-scheme URLs
        S, SR, SE = auditview.sanitize_args, auditview.sanitize_result, auditview.sanitize_error

        nan = float("nan")
        a1 = S("system.volume.up", {"amount": nan, "n": float("inf")})
        a2 = S("system.volume.up", json.loads(dbscrub.store_dumps(a1)))
        rec.check("K5 NaN / inf numbers survive: sanitize_args is a fixed point through the stored JSON and the scrub does not see a change forever",
                  json.dumps(a1, sort_keys=True) == json.dumps(a2, sort_keys=True) and dbscrub._same(float("nan"), float("nan")))
        rec.check("K6 a bytes result is a length marker; a non-string result a plain '[omitted]'; both are fixed points",
                  SR("apps.open", b"abc") == "[omitted: 3 bytes]" and SR("apps.open", 5) == "[omitted]" and SR("apps.open", SR("apps.open", b"abc")) == "[omitted: 3 bytes]"
                  and SE(b"xyz") == "[omitted: 3 bytes]")
        long_first = S("shell.run", {"command": "x" * 100 + " arg1 arg2"})["command"]
        env_first = S("shell.run", {"command": "SECRET=hunter2 run --flag"})["command"]
        rec.check("K7 a shell command whose first token is not a plain executable name (too long, VAR=value, $x) becomes '(command) [omitted: ...]'; fixed point",
                  long_first == "(command) [omitted: 2 args, 110 chars]" and env_first == "(command) [omitted: 2 args, 25 chars]" and "hunter2" not in env_first
                  and S("shell.run", {"command": long_first})["command"] == long_first
                  and S("shell.run", {"command": "curl -x https://h"})["command"] == "curl [omitted: 2 args, 17 chars]")
        fetched = SR("web.fetch", "go https://h.test/x?token=abc")
        fetched2 = SR("web.fetch", "see https://h.test/x?token=abc and https://g.test/y#frag")
        rec.check("K8 a web.fetch result that quotes a URL with a query is stripped; with two such URLs it becomes a read-back marker; both are fixed points and hold no token",
                  fetched == "go https://h.test/x [query/credentials not stored]" and SR("web.fetch", fetched) == fetched
                  and SR("web.fetch", fetched2) == fetched2 and "token" not in fetched2, (fetched, fetched2))
        q = auditview.strip_query_urls
        rec.check("K9 strip_query_urls handles ANY scheme (ftp, wss, https), drops userinfo, and consumes the whole tail up to whitespace",
                  q("ftp://u:p@h.test/x?y=1") == "ftp://h.test/x [query/credentials not stored]"
                  and q("wss://h.test/s?t=1&u=2 next") == "wss://h.test/s [query/credentials not stored] next"
                  and q("https://u:pw@h.test/x") == "https://h.test/x [query/credentials not stored]"
                  and "pw" not in q("https://u:pw@h.test/x") and q("https://h.test/p?a=1 b=2") == "https://h.test/p [query/credentials not stored] b=2")
        rec.check("K10 ...a plain URL without query or userinfo is unchanged, and the function is idempotent",
                  q("see https://h.test/a/b ok") == "see https://h.test/a/b ok" and q(q("ftp://u:p@h.test/x?y=1#z")) == q("ftp://u:p@h.test/x?y=1#z"))
        rec.check("K11 data:/javascript: URLs are never stored (url argument -> '[omitted: non-web url]'); fixed point",
                  S("web.open", {"url": "javascript:alert(document.cookie)"}) == {"url": "[omitted: non-web url]"}
                  and S("web.open", {"url": "data:text/html;base64,AAAA"}) == {"url": "[omitted: non-web url]"}
                  and S("web.open", S("web.open", {"url": "data:text/html,x"})) == {"url": "[omitted: non-web url]"})
        rec.check("K12 the marker grammar is EXACT: a look-alike like '[omitted: abc chars]' is NOT passed through, real markers are",
                  S("files.write", {"path": "p", "content": "[omitted: abc chars]"})["content"] == "[omitted: 20 chars]"
                  and S("files.write", {"path": "p", "content": "[omitted: 5 chars]"})["content"] == "[omitted: 5 chars]"
                  and S("files.write", {"path": "p", "content": "[omitted: 5 chars, 2 lines]"})["content"] == "[omitted: 5 chars, 2 lines]"
                  and S("files.write", {"path": "p", "content": "[omitted: 3 items]"})["content"] == "[omitted: 3 items]"
                  and S("files.write", {"path": "p", "content": "[omitted: 9 bytes]"})["content"] == "[omitted: 9 bytes]"
                  and S("files.write", {"path": "p", "content": "[redacted] but more text"})["content"] == "[omitted: 24 chars]"
                  and S("files.write", {"path": "p", "content": "[redacted]"})["content"] == "[redacted]")
        rec.check("K13 a credential-named key is '[redacted]' even with a NUMERIC value (a PIN); booleans and None are kept",
                  S("some.skill", {"pin": 1234, "password": 5.5, "token": True, "otp": None}) == {"pin": "[redacted]", "password": "[redacted]", "token": True, "otp": None})
        secret = "CANARY-DERIVED-de12 the secret body of the message that was typed"
        partial = auditview.redact_echoes(f"Typed: {secret[20:60]}", [secret])
        rec.check("K14 derived echoes: a 12+ character run of an omitted value inside a result is replaced even when the whole value is not repeated",
                  "secret body" not in partial and "[omitted: " in partial and partial.startswith("Typed:"), partial)

        # the same through the whole pipeline (scrub of history == live write)
        dv = w.fix("de12", small=True)
        dc = sqlite3.connect(str(dv.path))
        dc.execute("INSERT INTO audit (at, actor, skill, tier, args, decision, ok, result, error) VALUES ('2026-04-03','text','input.type','L2',?,'confirmed',1,?,?)",
                   (json.dumps({"text": secret}), f"Typed: {secret[20:60]}", f"warn: {secret[25:55]}"))
        dc.commit()
        dc.close()
        tdv = w.target(dv, "active", other=b)
        w.apply(tdv)
        row = table_rows(dv.path, "select * from audit order by id desc limit 1")[0]
        with iso_env.isolated_runtime():
            aid = audit.record(actor="text", skill="input.type", tier="L2", args={"text": secret}, decision="confirmed")
            audit.complete(aid, ok=True, result=f"Typed: {secret[20:60]}", error=f"warn: {secret[25:55]}")
            live = dict(store.conn().execute("SELECT * FROM audit WHERE id=?", (aid,)).fetchone())
        rec.check("K15 a partial echo in result/error is removed by the scrub from the file (raw bytes too) and equals what the live path stores",
                  "secret body" not in json.dumps(row) and not raw_contains(dv.path, "the secret body of") and (row["args"], row["result"], row["error"]) == (live["args"], live["result"], live["error"]),
                  (row["result"], live["result"]))

        # ------------------------------------------------ live parity: episodes.record minimises like the scrub
        emsg = "CANARY-EPMSG-ep01 hello from the planner step"
        eurl = "https://example.com/ep?sid=CANARY-EPURL-ep01"
        obs = SimpleNamespace(step=SimpleNamespace(tool="whatsapp.compose", args={"contact": "Sam", "message": emsg}), ok=False,
                              error=f"could not send '{emsg}'", speech=f"failed: '{emsg}' at {eurl}")
        ctx = f"user said '{emsg}' while on {eurl}"
        with iso_env.isolated_runtime():
            with attr(episodes, "_embed_one", lambda t: None):
                eid = episodes.record("send it", goal_id=None, context=ctx, steps=[obs], stopped="error", ok=False, duration_ms=5)
            liveep = dict(store.conn().execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone())
        rec.check("K16 episodes.record (live) stores no message text or URL query in plan, step error, errors or context",
                  "CANARY-" not in json.dumps(liveep, default=str), {k: str(v)[:80] for k, v in liveep.items() if k in ("plan", "errors", "context")})
        ep = w.fix("ep01", small=True)
        raw_plan = [{"tool": "whatsapp.compose", "args": {"contact": "Sam", "message": emsg}, "ok": False, "error": f"could not send '{emsg}'"}]
        ec = sqlite3.connect(str(ep.path))
        cur = ec.execute("INSERT INTO episodes (at, goal_id, goal_text, context, plan, stopped, success, errors, duration_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("2026-04-04", None, "send it", ctx[:500], json.dumps(raw_plan, default=str, ensure_ascii=False), "error", 0,
                          f"failed: '{emsg}' at {eurl}"[:500], 5))
        ep_id = int(cur.lastrowid)
        ec.commit()
        ec.close()
        w.apply(w.target(ep, "active", other=b))
        scrubbed = table_rows(ep.path, f"select * from episodes where id={ep_id}")[0]
        rec.check("K17 the live episode row equals the scrub of the same raw episode (plan, errors, context): same-as-live for episodes",
                  json.loads(scrubbed["plan"]) == json.loads(liveep["plan"]) and scrubbed["errors"] == liveep["errors"] and scrubbed["context"] == liveep["context"],
                  (scrubbed["errors"], liveep["errors"]))

        # ------------------------------------------------ committed but NOT PURGED (VACUUM fails) -> loud, recoverable with compact
        nv = w.fix("9f01")
        tnv = w.target(nv, "active", other=b)
        code = w.code(tnv)
        pre_snap = logical(nv.path)
        dirs0 = set(w.backup_dirs())
        with attr(dbscrub, "sqlite3", _shim(FailVacuum)):
            msg = raises(lambda: dbscrub.apply(tnv, confirm=code, backup_root=w.backups))
        d_, man = _newest_manifest(w, dirs0)
        rec.check("K18 VACUUM fails after commit: ScrubError says it was committed and verified but may not be purged, names the backup and `db compact`, and prints no value",
                  msg is not None and "committed and verified" in msg and "db compact --target active" in msg and "VACUUM failed" in msg and str(d_ / "friday.db.bak") in msg
                  and "CANARY-" not in msg, msg)
        rec.check("K19 ...the manifest status is 'committed-NOT-PURGED'; the database IS scrubbed and verified (columns clean, counts and kept columns equal)",
                  man["status"] == "committed-NOT-PURGED" and not db_contains(nv.path, "CANARY-") and logical(nv.path)["counts"] == pre_snap["counts"]
                  and logical(nv.path)["kept_digest"] == pre_snap["kept_digest"] and logical(nv.path)["integrity"] == "ok")
        d1 = logical(nv.path)["digest"]
        info = dbscrub.compact(tnv)
        rec.check("K20 `compact` then purges: no canary in the raw file; every table digest is unchanged (no logical change); idempotent",
                  info["integrity"] == "ok" and not raw_contains(nv.path, "CANARY-") and logical(nv.path)["digest"] == d1 and dbscrub.compact(tnv)["integrity"] == "ok")
        # fault after commit (in process) -> compact
        af = w.fix("9f02")
        taf = w.target(af, "active", other=b)
        codeaf = w.code(taf)
        with attr(dbscrub, "_FAULT", fault_at("after-commit")):
            raises(lambda: dbscrub.apply(taf, confirm=codeaf, backup_root=w.backups))
        digest_af = logical(af.path)["digest"]
        dbscrub.compact(taf)
        rec.check("K21 after a fault at 'after-commit' the old text may still be in the file; `compact` removes it without any logical change",
                  not raw_contains(af.path, "CANARY-") and not db_contains(af.path, "CANARY-") and logical(af.path)["digest"] == digest_af)
        other = sqlite3.connect(str(af.path), timeout=0.2)
        other.execute("SELECT COUNT(*) FROM audit").fetchone()
        mc = raises(lambda: dbscrub.compact(taf))
        other.close()
        rec.check("K22 compact takes the same exclusive lock: with another connection open it refuses ('in use')", mc is not None and "in use" in mc, mc)
        with attr(dbplace, "in_synced_folder", lambda p: True):
            ts = dbscrub.resolve_target("active", active=af.path, legacy=b.path)
        mc = raises(lambda: dbscrub.compact(ts))
        rec.check("K23 compact on a synced-folder database needs allow_synced", mc is not None and "cloud-synced" in mc)

        # ------------------------------------------------ _find_remnants proves old values are gone
        remdb = tmp / "rem" / "friday.db"
        remdb.parent.mkdir()
        rc = sqlite3.connect(str(remdb))
        rc.execute("PRAGMA journal_mode=DELETE")
        rc.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY, args TEXT, result TEXT)")
        rc.execute("INSERT INTO audit (args, result) VALUES (?, 'x')", ("CANARY-REMNANT-aaaa live value that stays",))
        rc.execute("INSERT INTO audit (args, result) VALUES (?, 'x')", ("CANARY-REMNANT-bbbb deleted value gone",))
        rc.commit()
        rc.execute("DELETE FROM audit WHERE args LIKE '%bbbb%'")
        rc.commit()
        rc.close()
        live_v, dead_v = "CANARY-REMNANT-aaaa live value that stays", "CANARY-REMNANT-bbbb deleted value gone"
        r_live, r_dead, r_both, r_short = (dbscrub._find_remnants(remdb, [live_v]), dbscrub._find_remnants(remdb, [dead_v]),
                                           dbscrub._find_remnants(remdb, [live_v, dead_v]), dbscrub._find_remnants(remdb, ["ab"]))
        vc = sqlite3.connect(str(remdb))
        vc.execute("VACUUM")
        vc.close()
        rec.check("K24 _find_remnants: a value still stored somewhere is not a remnant (0); a deleted value still in the file's free space is (1); a short needle is ignored; after VACUUM 0",
                  (r_live, r_dead, r_both, r_short) == (0, 1, 1, 0) and dbscrub._find_remnants(remdb, [dead_v]) == 0, (r_live, r_dead, r_both, r_short))

        # ------------------------------------------------ restore guards, hard links
        src_fx = w.fix("re01", small=True)
        rres = w.apply(w.target(src_fx, "active", other=b))
        bak = rres.backup.file
        rdest = tmp / "rs" / "friday.db"
        rdest.parent.mkdir()
        Path(str(rdest) + "-wal").write_bytes(b"stale wal from an older database")
        m1 = raises(lambda: dbscrub.restore(bak, rdest))
        Path(str(rdest) + "-wal").unlink()
        Path(str(rdest) + "-shm").write_bytes(b"stale shm")
        m2 = raises(lambda: dbscrub.restore(bak, rdest))
        rec.check("K25 restore refuses when a stale -wal or -shm sits next to the destination (it would be applied to the restored database); nothing is published",
                  m1 is not None and "sidecar" in m1 and m2 is not None and "sidecar" in m2 and not rdest.exists()
                  and Path(str(rdest) + "-shm").read_bytes() == b"stale shm", (m1, m2))
        Path(str(rdest) + "-shm").unlink()
        with attr(dbplace, "in_synced_folder", lambda p: True):
            m3 = raises(lambda: dbscrub.restore(bak, rdest))
        rec.check("K26 restore into a cloud-synced folder is refused", m3 is not None and "cloud-synced" in m3 and not rdest.exists(), m3)
        rec.check("K27 with a clean destination the same restore works", dbscrub.restore(bak, rdest)["restored"] == str(rdest) and rdest.exists())
        hl = tmp / "hl" / "friday.db"
        hl.parent.mkdir()
        try:
            os.link(str(b.path), str(hl))
            linked = True
        except OSError:
            linked = False
        if linked:
            m4 = raises(lambda: dbscrub.resolve_target("legacy", active=b.path, legacy=hl))
            t4 = dbscrub.resolve_target("active", active=b.path, legacy=hl)
            rec.check("K28 a HARD LINK to the active database is recognised as the same file ('legacy' refused, 'active' flagged same_as_other)",
                      m4 is not None and "same file" in m4 and t4.same_as_other, m4)
        else:
            rec.skip("K28 hard-link sameness", "the file system refused os.link")

        if not quick:
            # CLI: non-UTF-8 value, compact, no tracebacks
            r = run_cli(["db", "scrub", "--target", "active"], bad.path)
            out = r.stdout + r.stderr
            rec.check("K29 subprocess: previewing the non-UTF-8 database prints no stored text and no traceback", "CANARY-" not in out and "Traceback" not in out and "details withheld" in out, (r.returncode, out[-200:]))
            r = run_cli(["db", "scrub", "--apply", "--target", "active", "--confirm", "0123456789", "--backup-dir", str(tmp / "clibk2")], bad.path)
            out = r.stdout + r.stderr
            rec.check("K30 subprocess: --apply on it fails (exit 1) with no stored text, no traceback and no backup folder",
                      r.returncode == 1 and "CANARY-" not in out and "Traceback" not in out and not (tmp / "clibk2").exists(), (r.returncode, out[-200:]))
            cx = w.fix("c0c0", small=True)
            code_c = dbscrub.preview(w.target(cx, "active", other=b)).code
            r = run_cli(["db", "scrub", "--apply", "--target", "active", "--confirm", code_c, "--backup-dir", str(tmp / "clibk3")], cx.path)
            r2 = run_cli(["db", "compact", "--target", "active"], cx.path)
            r3 = run_cli(["db", "compact"], cx.path)
            rec.check("K31 subprocess: `db scrub --apply` then `db compact --target active` exit 0 (contents unchanged); `db compact` without a target exits 1",
                      r.returncode == 0 and r2.returncode == 0 and "compacted" in r2.stdout and "contents unchanged" in r2.stdout and r3.returncode == 1
                      and not raw_contains(cx.path, "CANARY-"), (r.returncode, r2.returncode, r2.stdout[-120:], r3.returncode))


# --------------------------------------------------------------------------------------------- I: mutation testing

@contextlib.contextmanager
def mutated(owner, name: str, *pairs: str):
    """Replace each `old` (even args) with `new` (odd args) in the SOURCE of owner.name and run with the broken
    function installed in the module itself (so it sees every other patch the tests make)."""
    target = getattr(owner, name)
    src = SRC_CACHE.get((owner.__name__, name)) or textwrap.dedent(inspect.getsource(target))
    for old, new in zip(pairs[0::2], pairs[1::2]):
        assert old in src, f"mutation anchor not found in {name}: {old!r}"
        src = src.replace(old, new, 1)
    tree = ast.parse(src)
    fn = tree.body[0]
    fn.decorator_list = []
    module = inspect.getmodule(target)
    saved = getattr(owner, name)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "<mutant>", "exec"), vars(module))
    try:
        yield
    finally:
        setattr(owner, name, saved)


SRC_CACHE: dict[tuple[str, str], str] = {}


def _cli():
    from friday import cli
    return cli


def cache_sources() -> None:
    cli = _cli()
    for owner, names in ((dbscrub, ("_mark_aborted", "apply", "preview", "_lock_exclusive", "_make_backup", "verify_backup_file", "check_backup_root", "changes",
                                    "_code", "_compare", "restore", "resolve_target", "_require_file", "_open_ro", "compact", "_group_skill",
                                    "_find_remnants")), (cli, ("_db_scrub",))):
        for n in names:
            SRC_CACHE[(owner.__name__, n)] = textwrap.dedent(inspect.getsource(getattr(owner, n)))


EARLY_UPDATE = (
    "for _ch in rows:\n"
    "            c.execute('UPDATE \"' + _ch.table + '\" SET ' + \", \".join('\"%s\"=?' % k for k in _ch.values) + ' WHERE id=?', "
    "(*_ch.values.values(), _ch.rowid))\n"
    "        backup = _make_backup(c, target, backup_root or default_backup_root(), before)"
)

# (label, factory, sections that must notice)
MUTATIONS: list[tuple[str, object, str]] = [
    ("the confirmation code is not checked",
     lambda: mutated(dbscrub, "apply", "if confirm.strip() != pv.code:", "if False:"), "B"),
    ("an empty confirmation code is accepted",
     lambda: mutated(dbscrub, "apply", "if not confirm or not isinstance(confirm, str):", "if False:"), "B"),
    ("the target name is not validated in apply",
     lambda: mutated(dbscrub, "apply", "if target.name not in TARGETS:", "if False:"), "B"),
    ("the confirmation code ignores the target path",
     lambda: mutated(dbscrub, "_code", 'h.update(f"v{SCRUB_VERSION}|{os.path.normcase(os.fspath(path))}|".encode())', 'h.update(b"v1|")'), "A"),
    ("the confirmation code ignores the new values (stale code accepted)",
     lambda: mutated(dbscrub, "_code", "h.update(_row_hash(tuple(sorted(ch.values.items()))))", "pass"), "B"),
    ("the exclusive lock is not taken (a second connection is ignored)",
     lambda: mutated(dbscrub, "_lock_exclusive", 'c.execute("PRAGMA locking_mode=EXCLUSIVE")', "pass"), "G"),
    ("the scrub is applied BEFORE the backup is made",
     lambda: mutated(dbscrub, "apply", "backup = _make_backup(c, target, backup_root or default_backup_root(), before)", EARLY_UPDATE), "FC"),
    ("backup verification is skipped (empty copy accepted)",
     lambda: mutated(dbscrub, "_make_backup", "src.backup(dst)", "pass",
                     "verify_backup_file(partial, before, rehearse=False)", "None",
                     "verify_backup_file(final, before, rehearse=True)", "None"), "E"),
    ("verify_backup_file ignores a mismatching expected snapshot",
     lambda: mutated(dbscrub, "verify_backup_file", "if snap[key] != expected[key]:", "if False:"), "E"),
    ("the restore rehearsal on a disposable copy is skipped",
     lambda: mutated(dbscrub, "_make_backup", "verify_backup_file(final, before, rehearse=True)", "verify_backup_file(final, before, rehearse=False)"), "E"),
    ("the restore rehearsal never compares the digest",
     lambda: mutated(dbscrub, "verify_backup_file", 'again["integrity"] != "ok" or again["digest"] != snap["digest"] or again["counts"] != snap["counts"]', "False"), "E"),
    ("secure_delete is not switched on",
     lambda: mutated(dbscrub, "apply", 'c.execute("PRAGMA secure_delete=ON")', "pass"), "C"),
    ("VACUUM is not run",
     lambda: mutated(dbscrub, "apply", 'c.execute("VACUUM")', "pass"), "C"),
    ("neither secure_delete nor VACUUM (old text stays in free pages)",
     lambda: mutated(dbscrub, "apply", 'c.execute("PRAGMA secure_delete=ON")', "pass", 'c.execute("VACUUM")', "pass"), "C"),
    ("the pre-commit comparison (_compare) is skipped",
     lambda: mutated(dbscrub, "apply", "problems = _compare(before, now)", "problems = []"), "F"),
    ("_compare no longer checks the columns that must not change",
     lambda: mutated(dbscrub, "_compare", 'for t, d in before["kept_digest"].items():', "for t, d in []:"), "F"),
    ("the pre-commit fixed-point check is skipped",
     lambda: mutated(dbscrub, "apply", "if _any_change(c):", "if False:"), "F"),
    ("ROLLBACK is never issued after a failure",
     lambda: mutated(dbscrub, "apply", 'c.execute("ROLLBACK")', "pass"), "F"),
    ("the scrub writes audit.result with the error sanitiser's target (results left as they are)",
     lambda: mutated(dbscrub, "changes", '("result", auditview.sanitize_result)', '("result", lambda s, t, e=(): t)'), "C"),
    ("a backup folder inside the repository is allowed",
     lambda: mutated(dbscrub, "check_backup_root", "if dbplace._under(root, paths.ROOT):", "if False:"), "E"),
    ("a backup folder inside the database's own folder is allowed",
     lambda: mutated(dbscrub, "check_backup_root", "if dbplace._under(root, guarded):", "if False:"), "E"),
    ("a backup folder inside a synced folder is allowed",
     lambda: mutated(dbscrub, "check_backup_root", "if dbplace.in_synced_folder(root):", "if False:"), "E"),
    ("the free-space check is skipped",
     lambda: mutated(dbscrub, "_make_backup", "if free < need:", "if False:"), "E"),
    ("the backup is published over an existing file",
     lambda: mutated(dbscrub, "_make_backup", "if not dbplace._publish_no_overwrite(partial, final):", "os.replace(partial, final)\n    if False:"), "E"),
    ("an existing backup folder is reused (mkdir exist_ok=True)",
     lambda: mutated(dbscrub, "_make_backup", "directory.mkdir(parents=False, exist_ok=False)", "directory.mkdir(parents=False, exist_ok=True)"), "E"),
    ("the backup file is not made read-only",
     lambda: mutated(dbscrub, "_make_backup", "os.chmod(final, stat.S_IREAD)", "pass"), "E"),
    ("restore overwrites an existing destination",
     lambda: mutated(dbscrub, "restore", "if dest.exists():", "if False:",
                     "if not dbplace._publish_no_overwrite(tmp, dest):", "os.replace(tmp, dest)\n        if False:"), "E"),
    ("'nothing to scrub' no longer stops a no-op apply (a second backup is made)",
     lambda: mutated(dbscrub, "apply", "if not rows:", "if False:"), "D"),
    ("a database in a synced folder is scrubbed without allow_synced",
     lambda: mutated(dbscrub, "apply", "if target.synced and not allow_synced:", "if False:"), "B"),
    ("an alias (symlink / junction) path is accepted",
     lambda: mutated(dbscrub, "apply", "if target.alias:", "if False:"), "A"),
    ("legacy == active is not detected",
     lambda: mutated(dbscrub, "resolve_target", 'if name == "legacy" and same:', "if False:"), "A"),
    ("preview writes to the database",
     lambda: mutated(dbscrub, "preview", "p.bytes = target.path.stat().st_size",
                     'p.bytes = target.path.stat().st_size; _w = sqlite3.connect(str(target.path)); _w.execute("CREATE TABLE IF NOT EXISTS zz_probe(x)"); _w.commit(); _w.close()'), "A"),
    ("preview opens the database read-write",
     lambda: mutated(dbscrub, "_open_ro", "c = dbplace._open_ro(path)", "c = sqlite3.connect(str(path))"), "A"),
    ("the real-database guard is removed from apply",
     lambda: mutated(dbscrub, "_require_file", "dbplace.guard(t.path)", "pass"), "G"),
    ("the real-database guard is removed from preview",
     lambda: mutated(dbscrub, "preview", "dbplace.guard(target.path)", "pass"), "G"),
    ("the real-database guard is removed from restore",
     lambda: mutated(dbscrub, "restore", "dbplace.guard(dest)", "pass"), "E"),
    ("a failed VACUUM / remnant after commit is not reported (no NOT-PURGED error)",
     lambda: mutated(dbscrub, "apply", "if vacuum and not purged:", "if False:"), "K"),
    ("purged is claimed without checking for remnants",
     lambda: mutated(dbscrub, "apply", "purged = vacuumed and remnants == 0", "purged = True"), "K"),
    ("_find_remnants never counts a remnant",
     lambda: mutated(dbscrub, "_find_remnants", "remnants += 1", "pass"), "K"),
    ("compact does not VACUUM",
     lambda: mutated(dbscrub, "compact", 'c.execute("VACUUM")', "pass"), "K"),
    ("restore ignores stale -wal/-shm next to the destination",
     lambda: mutated(dbscrub, "restore", "if leftovers:", "if False:"), "K"),
    ("restore into a synced folder is allowed",
     lambda: mutated(dbscrub, "restore", "if dbplace.in_synced_folder(dest):", "if False:"), "K"),
    ("skill names from stored rows are echoed in reports",
     lambda: mutated(dbscrub, "_group_skill", 'return skill if len(skill) <= 40 and _NAME_OK.match(skill) else "(other)"', "return skill"), "K"),
    ("the preview error quotes the SQLite exception text (may contain a stored value)",
     lambda: mutated(dbscrub, "preview", 'p.error = f"{type(exc).__name__} (details withheld: a stored value may be quoted in the message)"',
                     'p.error = f"{type(exc).__name__}: {exc}"'), "K"),
    ("a hard link to the active database is not recognised as the same file",
     lambda: mutated(dbscrub, "resolve_target", " or (a.exists() and lg.exists() and os.path.samefile(a, lg))", ""), "K"),
    ("an aborted run is not recorded in the manifest",
     lambda: mutated(dbscrub, "_mark_aborted", 'manifest["status"] = "aborted-database-unchanged"', "pass"), "F"),
    ("the CLI scrubs without an explicit --target (both databases)",
     lambda: mutated(_cli(), "_db_scrub", "if apply and not target:", "if False:"), "B"),
]


def run_sections(letters: str, rec: Rec, quick: bool, *, real_before: dict | None = None) -> None:
    for letter in letters:
        fn = SECTIONS[letter]
        try:
            fn(rec, quick)
        except Exception as exc:  # noqa: BLE001 -- a crash is also a detection
            rec.failed.append(f"{letter} crashed: {type(exc).__name__}")
            if rec.loud:
                import traceback
                traceback.print_exc()


SECTIONS = {"A": sec_A, "B": sec_B, "C": sec_C, "D": sec_D, "E": sec_E, "F": sec_F, "G": sec_G, "K": sec_K}


def main() -> int:
    t0 = time.perf_counter()
    cache_sources()
    before = real_fingerprint()
    loud = Rec(loud=True)
    run_sections("ABCDEFGK", loud, False)
    sec_H(loud, before)
    if "--fast" in sys.argv:
        print(f"smoke_db_scrub (no mutations): {loud.n - len(loud.failed)}/{loud.n} checks passed; failed={loud.failed}")
        return 0 if not loud.failed else 1

    loud.section("I: mutation testing -- break each safety invariant in the source; the suite must notice")
    clean = not loud.failed
    loud.check("I0 the unmutated suite is clean (mutations only count against a green baseline)", clean, loud.failed[:6])
    killed = 0
    survivors: list[str] = []
    for label, make, letters in MUTATIONS:
        quiet = Rec(loud=False, mutation=True)
        try:
            with make():
                run_sections(letters, quiet, True)
        except AssertionError as exc:
            if "mutation anchor" in str(exc):
                raise
            quiet.failed.append(f"assertion: {exc}")
        except Exception as exc:  # noqa: BLE001
            quiet.failed.append(f"crashed: {type(exc).__name__}: {exc}")
        caught = bool(quiet.failed)
        killed += caught
        if not caught:
            survivors.append(label)
        loud.check(f"I mutation caught: {label}", caught and clean,
                   "SURVIVED" if not caught else "")
        if loud.loud and caught:
            print(f"        (first checks that failed: {[f.split(' ')[0] for f in quiet.failed[:4]]})")
    loud.check(f"I all {len(MUTATIONS)} mutations killed", killed == len(MUTATIONS) and clean, f"{killed}/{len(MUTATIONS)}; survivors={survivors}")

    after_real = real_fingerprint()
    loud.section("H2: the user's real data after the mutation runs too")
    loud.check("H6 real database / stores / default backup root still unchanged after the mutation runs",
               after_real["active"] == before["active"] and after_real["legacy"] == before["legacy"] and after_real["store"] == before["store"]
               and after_real["legacy_store"] == before["legacy_store"] and after_real["backups"] == before["backups"] and not after_real["repo_probe"])

    passed = loud.n - len(loud.failed)
    print("\n" + "=" * 72)
    print(f"smoke_db_scrub  ({time.perf_counter() - t0:.1f}s)   mutations killed {killed}/{len(MUTATIONS)}"
          + (f"   skipped: {', '.join(loud.skipped)}" if loud.skipped else ""))
    if loud.failed:
        print("  FAILED:")
        for label in loud.failed:
            print(f"    - {label}")
    print(f"smoke_db_scrub: {passed}/{loud.n} checks passed")
    return 0 if not loud.failed and loud.n >= 120 else 1


if __name__ == "__main__":
    sys.exit(main())
