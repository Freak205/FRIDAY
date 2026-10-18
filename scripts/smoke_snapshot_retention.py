"""Phase 33.0 -- recovery-store privacy, retention and user-controlled recovery.

Everything runs against throwaway temp folders, a throwaway undo/audit database
(`store.use_temp_db`) and throwaway snapshot stores (BOTH `paths.SNAPSHOTS` and
`paths.LEGACY_SNAPSHOTS` are redirected, and every deletion path is wrapped in a tripwire that
fails if it is ever asked to remove something outside the temp folder). Nothing touches the user's
files, `data/friday.db`, the real recovery store or the old in-repository `data/snapshots`.

What it pins:
  A  location: default outside the repository, explicit overrides, unsafe locations refused,
     the legacy store still READ (old undo entries keep working) but never written or cleaned
  B  migration of the legacy store: verified, copied beside its place then renamed in,
     non-destructive, idempotent
  C  references: which snapshots a pending / in-progress / given-up undo entry keeps alive;
     an unreadable journal fails closed
  D  retention: nothing a pending undo needs is deleted (single process; the check-then-act gap
     between the reference re-check and the rename, and lstat-then-rmdir, are not tested);
     age floor, keep-newest, batch bound,
     damaged/foreign/linked folders untouched, interrupted + repeated cleanup, journal, undo window
  E  listing: minimal metadata only -- no content, no hash, redacted paths, bounded, read-only
  F  controlled restore (files.recover): occupied destination kept + asks, missing parent, corrupt
     or unknown or traversal ids, interrupted attempt, undoable, no path to steer
  G  privacy: no file content in logs, the cleanup journal or the new skills' audit rows
  H  the Executor choke point: tiers, confirmation, unattended denial; the planner is offered none
     of these skills (operator-only via the console / `run.py recovery`)

Then each retention / privacy invariant is deliberately broken (source-level mutation) and the
suite must FAIL -- counted only when the unmutated suite passes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import inspect
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402
from smoke_file_safety import Rec, mutated, ok, patched, said  # noqa: E402

from friday import audit, paths, permissions, recovery, retention, store, undo, verify  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.permissions import EXECUTOR, PermissionError_  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.skills import files, recoveries  # noqa: E402

REGISTRY.discover()

SECRET = "TOP-SECRET-CONTENT-7731"
DAY = 86400.0
# The user's REAL stores, captured before anything is redirected. A tripwire on os.unlink / os.rmdir
# records (and refuses) any removal inside them while the suite runs.
REAL_STORES = [paths._default_snapshots(), paths.DATA / "snapshots"]


# -- scaffolding ----------------------------------------------------------------------------------


class World:
    """One isolated environment: work folder, current + legacy stores, database, confirm handler."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "work"
        self.root.mkdir()
        self.snaps = tmp / "snaps"
        self.legacy = tmp / "legacy"
        self.snaps.mkdir()
        self.legacy.mkdir()
        self.answer = True
        self.asked: list[tuple[str, str]] = []
        paths.SNAPSHOTS = self.snaps
        paths.LEGACY_SNAPSHOTS = self.legacy
        CFG.files.snapshot_dir = ""
        CFG.files.snapshot_retention_days = 30
        CFG.files.snapshot_keep_newest = 0
        CFG.files.undo_window_days = 0
        CFG.files.snapshot_cleanup_batch = 100
        CFG.files.max_snapshot_mb = 256
        self.violations: list[str] = []
        self._real_unlink, self._real_rmdir = os.unlink, os.rmdir
        real_roots = [p.resolve() for p in REAL_STORES]

        def watch(fn):
            def inner(p, *a, **k):
                if isinstance(p, (str, bytes, os.PathLike)):
                    try:
                        rp = Path(os.fsdecode(p)).resolve()
                    except (OSError, ValueError):
                        rp = None
                    if rp is not None and any(rp == r or r in rp.parents for r in real_roots):
                        self.violations.append(str(rp))
                        raise AssertionError(f"the suite tried to remove inside the user's real store: {rp}")
                return fn(p, *a, **k)
            return inner

        os.unlink, os.rmdir = watch(os.unlink), watch(os.rmdir)  # the tripwire

        async def confirm(skill, args, preview):
            self.asked.append((skill.name, preview))
            return self.answer

        EXECUTOR.set_confirm_handler(confirm)

    def close(self) -> None:
        os.unlink, os.rmdir = self._real_unlink, self._real_rmdir

    def f(self, name: str, data: bytes | str = b"x") -> Path:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    async def run(self, skill: str, actor: str = "text", **args):
        try:
            return await EXECUTOR.run(skill, args, actor=actor)
        except PermissionError_ as exc:
            return exc

    def dirs(self, base: Path | None = None) -> list[str]:
        return sorted(os.listdir(base or self.snaps))

    def tokens(self, base: Path | None = None) -> list[str]:
        return [d for d in self.dirs(base) if recovery.is_token(d)]

    def journal(self) -> list[dict]:
        p = self.snaps / retention.JOURNAL
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def keep(self, name: str, content: str = "kept", days: float = 100, reason: str = "overwritten by files.write") -> str:
        """An UNREFERENCED snapshot (as if its undo entry was reversed), aged `days`."""
        p = self.f(name, content)
        token = recovery.preserve(p, reason=reason)
        self.age(token, days)
        return token

    def age(self, token: str, days: float, base: Path | None = None) -> None:
        mp = (base or self.snaps) / token / "meta.json"
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * DAY))
        mp.write_text(json.dumps(meta), encoding="utf-8")

    def set_undo_age(self, days: float) -> None:
        when = time.time() - days * DAY
        stamp = datetime.fromtimestamp(when, timezone.utc).isoformat()
        c = store.conn()
        c.execute("UPDATE undo SET at = ?", (stamp,))
        c.commit()

    def undo_rows(self) -> list[tuple[int, int]]:
        return [(r["id"], r["applied"]) for r in store.conn().execute("SELECT id, applied FROM undo ORDER BY id")]

    def reset_db(self) -> None:
        c = store.conn()
        c.execute("DELETE FROM undo")
        c.commit()

    def add_undo(self, inverse: str, applied: int = 0) -> int:
        c = store.conn()
        cur = c.execute("INSERT INTO undo (at, audit_id, skill, inverse, applied) VALUES (?,?,?,?,?)",
                        (store.now(), None, "files.delete", inverse, applied))
        c.commit()
        return int(cur.lastrowid)

    async def delete_file(self, name: str, content: str = "to-delete") -> tuple[Path, str]:
        p = self.f(name, content)
        r = await self.run("files.delete", path=str(p))
        return p, r.data["snapshot"]


def tree_hash(base: Path) -> dict[str, str]:
    out = {}
    for dirpath, _dirs, files_ in os.walk(base):
        for n in files_:
            fp = Path(dirpath) / n
            out[str(fp.relative_to(base))] = hashlib.sha256(fp.read_bytes()).hexdigest()
    return out


def tok(i: int) -> str:
    return f"20200101T0000{i:02d}-{i:012x}"


def make_junction(link: Path, target: Path) -> bool:
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        return r.returncode == 0 and link.exists()
    except OSError:
        return False


# -- the suite ------------------------------------------------------------------------------------


async def suite(rec: Rec) -> None:
    saved = {k: getattr(CFG.files, k) for k in (
        "snapshot_dir", "snapshot_retention_days", "snapshot_keep_newest", "undo_window_days",
        "snapshot_cleanup_batch", "max_snapshot_mb")}
    real = (paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS)
    saved_scan = retention.SCAN_CAP
    with tempfile.TemporaryDirectory(prefix="friday_retention_") as t, store.use_temp_db():
        w = World(Path(t))
        try:
            await _suite(rec, w)
        finally:
            w.close()
            paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = real
            retention.SCAN_CAP = saved_scan
            for k, v in saved.items():
                setattr(CFG.files, k, v)


async def _suite(rec: Rec, w: World) -> None:
    log_records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            log_records.append(record)

    handler = Capture(level=logging.DEBUG)
    logging.getLogger().addHandler(handler)
    prior_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        await _scenarios(rec, w, log_records)
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(prior_level)


async def _scenarios(rec: Rec, w: World, log_records: list[logging.LogRecord]) -> None:
    # ---------------------------------------------------------------- A: location
    rec.scenario("A: the store lives outside the repository; overrides; legacy store read-only")
    env_keys = ("FRIDAY_SNAPSHOTS", "LOCALAPPDATA", "OneDrive", "OneDriveConsumer", "OneDriveCommercial")
    saved_env = {k: os.environ.get(k) for k in env_keys}

    def setenv(**kw):
        for k in env_keys:
            os.environ.pop(k, None)
        for k, v in kw.items():
            os.environ[k] = v

    try:
        la = str(w.tmp / "LocalAppData")
        setenv(LOCALAPPDATA=la)
        d = paths._default_snapshots()
        rec.check("A1 the default is under the local application-data folder", d == Path(la) / "FRIDAY" / "snapshots", d)
        rec.check("A2 ...which is not inside the repository", paths.ROOT.resolve() not in d.resolve().parents)
        setenv(LOCALAPPDATA=la, FRIDAY_SNAPSHOTS=str(w.tmp / "explicit"))
        rec.check("A3 FRIDAY_SNAPSHOTS overrides it", paths._default_snapshots() == w.tmp / "explicit")
        setenv()
        rec.check("A4 with no application-data folder it falls back to data/snapshots", paths._default_snapshots() == paths.DATA / "snapshots")
        od = w.tmp / "OneDriveHome"
        setenv(OneDrive=str(od))
        rec.check("A5 a store inside OneDrive is detected", recovery.synced_by_onedrive(od / "x" / "snaps"))
        rec.check("A6 ...and one outside it is not", not recovery.synced_by_onedrive(w.snaps))
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    CFG.files.snapshot_dir = str(w.tmp / "configured")
    rec.check("A7 files.snapshot_dir wins over the default", recovery.root() == w.tmp / "configured", recovery.root())
    CFG.files.snapshot_dir = "relative_snaps"
    real_root = paths.ROOT
    paths.ROOT = w.tmp / "repo-root"  # Phase 35: a real repository-relative folder would be (rightly) refused
    try:
        rec.check("A8 a relative snapshot_dir is anchored at the repository root", recovery.root() == paths.ROOT / "relative_snaps")
    finally:
        paths.ROOT = real_root
    for bad in (str(Path(w.tmp.anchor)), str(Path.home())):
        CFG.files.snapshot_dir = bad
        try:
            recovery.root()
            refused = False
        except recovery.RecoveryError:
            refused = True
        rec.check(f"A9 an unsafe snapshot_dir is refused ({bad})", refused)
    victim = w.f("a_victim.txt", "must-survive")
    r = await w.run("files.write", path=str(victim), content="overwrite")
    rec.check("A10 with an unsafe store, an overwrite is REFUSED and the file is untouched",
              not ok(r) and victim.read_text() == "must-survive", said(r))
    CFG.files.snapshot_dir = ""

    # legacy: a snapshot made in the OLD place with a pending undo entry keeps working
    paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = w.legacy, w.tmp / "legacy-unused"  # Phase 35: the guard counts LEGACY_SNAPSHOTS as a real store; never alias it to the write target
    lp = w.f("legacy_doc.txt", "legacy-original")
    r = await w.run("files.write", path=str(lp), content="legacy-new")
    paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = w.snaps, w.legacy
    legacy_tokens = w.tokens(w.legacy)
    legacy_before = tree_hash(w.legacy)
    rec.check("A11 (setup) a pre-move snapshot with a pending undo entry exists in the legacy store", len(legacy_tokens) == 1 and ok(r))
    rec.check("A12 the legacy snapshot is located", recovery.locate(legacy_tokens[0]) == w.legacy / legacy_tokens[0])
    lines = await undo.undo_last(1)
    rec.check("A13 undo of an entry made before the move still restores it", lp.read_text() == "legacy-original", lines)
    rec.check("A14 the restore did not write into the legacy store", [x for x in w.tokens(w.legacy) if x not in legacy_tokens] == [])
    nf = w.f("new_doc.txt", "n1")
    await w.run("files.write", path=str(nf), content="n2")
    rec.check("A15 new snapshots go to the CURRENT store only", len(w.tokens(w.snaps)) >= 1 and w.tokens(w.legacy) == legacy_tokens)
    refusals = []
    for p in (w.snaps, w.legacy, w.legacy / legacy_tokens[0], w.legacy / legacy_tokens[0] / "blob"):
        res = await w.run("files.delete", path=str(p))
        refusals.append(not ok(res) and "recovery storage" in said(res))  # refused FOR THAT REASON
    rec.check("A16 file commands refuse both stores, because they are the recovery store", all(refusals), refusals)
    res = await w.run("files.write", path=str(w.legacy / legacy_tokens[0] / "blob"), content="x")
    rec.check("A16b ...and refuse to write into the legacy store", not ok(res) and "recovery storage" in said(res), said(res))
    rec.check("A17 ...and nothing in the legacy store changed", tree_hash(w.legacy) == legacy_before)
    w.reset_db()
    for tkn in w.tokens(w.snaps):
        shutil.rmtree(w.snaps / tkn)

    # ---------------------------------------------------------------- B: migration
    rec.scenario("B: migrating the legacy store is verified, renamed in and non-destructive")
    shutil.rmtree(w.legacy)
    w.legacy.mkdir()
    paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = w.legacy, w.tmp / "legacy-unused"  # Phase 35: the guard counts LEGACY_SNAPSHOTS as a real store; never alias it to the write target
    g1 = recovery.preserve(w.f("m1.txt", "migrate-one"), reason="overwritten by files.write")
    g2 = recovery.preserve(w.f("m2.txt", "migrate-two"), reason="overwritten by files.write")
    emptyd = w.root / "emptyd"
    emptyd.mkdir()
    g3 = recovery.preserve(emptyd, reason="deleted by files.delete", move=True)
    g4 = recovery.preserve(w.f("m4.txt", "will-be-corrupt"), reason="overwritten by files.write")
    paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = w.snaps, w.legacy
    (w.legacy / g4 / "blob").write_bytes(b"tampered!!!")
    (w.legacy / "notes.txt").write_text("a foreign file the user keeps here")
    legacy_before = tree_hash(w.legacy)
    dry = retention.migrate_legacy(dry_run=True)
    rec.check("B1 a dry run copies nothing", w.tokens(w.snaps) == [] and sorted(dry["copied"]) == sorted([g1, g2, g3]), dry)
    out = retention.migrate_legacy()
    rec.check("B2 valid snapshots are copied", sorted(out["copied"]) == sorted([g1, g2, g3]) and sorted(w.tokens(w.snaps)) == sorted([g1, g2, g3]), out)
    rec.check("B3 the corrupt one is reported, not copied", any(g4 in x for x in out["failed"]) and g4 not in w.tokens(w.snaps), out)
    rec.check("B4 the legacy store is byte-for-byte unchanged", tree_hash(w.legacy) == legacy_before)
    rec.check("B5 the copies verify", all(recovery.info(x) for x in (g1, g2, g3)))
    again = retention.migrate_legacy()
    rec.check("B6 migrating again is a no-op", again["copied"] == [] and sorted(again["already_there"]) == sorted([g1, g2, g3]), again)
    paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = w.legacy, w.tmp / "legacy-unused"  # Phase 35: the guard counts LEGACY_SNAPSHOTS as a real store; never alias it to the write target
    g5 = recovery.preserve(w.f("m5.txt", "migrate-five"), reason="overwritten by files.write")
    paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = w.snaps, w.legacy
    legacy_before = tree_hash(w.legacy)
    from friday import orchestrator as _orch

    w.asked.clear()
    w.answer = False
    r = await w.run("files.recovery_migrate")
    rec.check("B6a the migration skill asks first, and declining copies nothing", w.asked and g5 not in w.tokens() and not ok(r), said(r))
    w.answer = True
    r = await w.run("files.recovery_migrate")
    rec.check("B6b approved: it copies the good snapshot through the Executor, reports the corrupt one honestly (not ok) and leaves the old folder untouched",
              not ok(r) and g5 in w.tokens() and "could not be migrated" in said(r) and "was not changed" in said(r) and tree_hash(w.legacy) == legacy_before, said(r))
    rec.check("B6c it is internal: never offered to the planner", REGISTRY.get("files.recovery_migrate").internal
              and "files.recovery_migrate" not in {s.name for s in _orch._tool_specs(None)})
    rec.check("B6d its verifier confirms the copy is present", all(c.passed for c in verify.VERIFIERS["files.recovery_migrate"].check({}, {"copied": [g5]}, {})))
    shutil.rmtree(w.snaps / g5)
    shutil.rmtree(w.legacy / g5)
    legacy_before = tree_hash(w.legacy)
    (w.snaps / g1 / "meta.json").write_text(json.dumps({**json.loads((w.snaps / g1 / "meta.json").read_text()), "reason": "different"}))
    clash = retention.migrate_legacy()
    rec.check("B7 an id that exists with different metadata is reported and NOT overwritten", any("different snapshot" in x for x in clash["failed"]) and json.loads((w.snaps / g1 / "meta.json").read_text())["reason"] == "different", clash)
    (w.snaps / g1 / "meta.json").write_text((w.legacy / g1 / "meta.json").read_text())
    rec.check("B8 no half-migrated leftovers", not [n for n in w.dirs() if n.startswith(retention.MIGRATE_PREFIX)])
    restore_target = w.root / "m1_restored.txt"
    shutil.rmtree(w.legacy)  # the old folder is gone: the migrated copy alone must restore
    r = recovery.restore_to(g1, restore_target, replace=False)
    rec.check("B9 after the legacy folder is removed, a migrated snapshot still restores", restore_target.read_text() == "migrate-one")
    w.legacy.mkdir()
    for tkn in w.tokens(w.snaps):
        shutil.rmtree(w.snaps / tkn)
    for n in os.listdir(w.snaps):
        p = w.snaps / n
        p.unlink() if p.is_file() else shutil.rmtree(p)

    # ---------------------------------------------------------------- C: references
    rec.scenario("C: which snapshots an undo entry keeps alive (and failing closed)")
    w.reset_db()
    pa, ta = await w.delete_file("c_pending.txt", "pending-content")
    rec.check("C1 a pending undo entry references its snapshot", 0 in retention.references().get(ta, set()))
    row = [r for r in retention.listing()["snapshots"] if r["id"] == ta][0]
    rec.check("C2 ...and the listing says so", row["status"] == "pending-undo", row)
    lines = await undo.undo_last(1)
    rec.check("C3 after a successful undo the snapshot is no longer protected", not retention._protected(retention.references().get(ta, set())), lines)
    row = [r for r in retention.listing()["snapshots"] if r["id"] == ta][0]
    rec.check("C4 ...and is reported as used", row["status"] == "used", row)

    # an interrupted multi-step reversal keeps its snapshot referenced
    w.reset_db()
    s1 = w.f("c_s1.txt", "SRC")
    d1 = w.f("c_d1.txt", "DEST-ORIGINAL")
    await w.run("files.move", src=str(s1), dst=str(d1))
    entry = undo.pending(1)[0]
    kept = [x for x in retention._TOKEN_IN_TEXT.findall(entry["inverse"])]
    rec.check("C5 a two-step inverse names the snapshot of the replaced file", len(kept) == 1)
    # an overwrite's undo restores onto a path that now holds the new content -> it asks -> declined
    w.reset_db()
    ov = w.f("c_over.txt", "OVER-ORIGINAL")
    await w.run("files.write", path=str(ov), content="OVER-NEW")
    ov_tok = retention._TOKEN_IN_TEXT.findall(undo.pending(1)[0]["inverse"])[0]
    w.answer = False
    await undo.undo_last(1)
    w.answer = True
    rec.check("C6 a declined undo leaves the snapshot protected", 0 in retention.references().get(ov_tok, set()) and ov.read_text() == "OVER-NEW")
    # a half-finished reversal: step one (move back) done, step two (restore) declined
    w.reset_db()
    m1 = w.f("c_m1.txt", "M-SRC")
    m2 = w.f("c_m2.txt", "M-DEST-ORIGINAL")
    await w.run("files.move", src=str(m1), dst=str(m2))
    m_tok = retention._TOKEN_IN_TEXT.findall(undo.pending(1)[0]["inverse"])[0]
    orig_run = EXECUTOR.run
    n = {"i": 0}

    async def occupy_after_first(skill_name, args=None, *, actor="text"):
        n["i"] += 1
        res = await orig_run(skill_name, args, actor=actor)
        if n["i"] == 1:
            m2.write_text("a file appeared at the restore target", encoding="utf-8")
        return res

    EXECUTOR.run = occupy_after_first
    w.answer = False
    try:
        await undo.undo_last(1)
    finally:
        del EXECUTOR.run
        w.answer = True
    rec.check("C7 a half-finished reversal (in progress) still protects the snapshot",
              0 in retention.references().get(m_tok, set()) and m1.read_text() == "M-SRC", undo.pending(3))

    # given up
    w.reset_db()
    gid = w.add_undo(json.dumps({"steps": [{"skill": "files.restore", "args": {"snapshot": tok(7), "path": "x"}}], "failures": 3}), applied=-1)
    rec.check("C8 a given-up entry (applied = -1) still protects its snapshot", retention._protected(retention.references().get(tok(7), set())))
    w.reset_db()
    for st in (1, -2, -3):
        w.add_undo(json.dumps({"skill": "files.restore", "args": {"snapshot": tok(8), "path": "x"}}), applied=st)
    rec.check("C9 reversed / superseded / retired entries do not protect it", not retention._protected(retention.references().get(tok(8), set())))
    w.reset_db()
    w.add_undo("{this is not json", applied=0)
    try:
        retention.references()
        failed_closed = False
    except retention.RetentionError:
        failed_closed = True
    rec.check("C10 an unreadable pending entry makes the references unknowable -> RetentionError", failed_closed)
    w.reset_db()
    w.add_undo(json.dumps({"unreadable": True}), applied=-1)
    try:
        retention.references()
        failed_closed = False
    except retention.RetentionError:
        failed_closed = True
    rec.check("C11 ...and so does a given-up entry whose record was lost", failed_closed)
    w.reset_db()
    real_conn = store.conn

    def broken_conn():
        raise RuntimeError("database is locked")

    store.conn = broken_conn
    try:
        try:
            retention.references()
            failed_closed = False
        except retention.RetentionError:
            failed_closed = True
    finally:
        store.conn = real_conn
    rec.check("C12 a journal that cannot be read -> RetentionError", failed_closed)

    # ---------------------------------------------------------------- D: retention
    rec.scenario("D: cleanup never deletes what an undo needs; bounded, idempotent, auditable")
    w.reset_db()
    for n_ in w.dirs():
        p = w.snaps / n_
        p.unlink() if p.is_file() else shutil.rmtree(p)

    # D1/D2: an ancient snapshot with a pending undo entry survives; an ancient unreferenced one goes
    _, t_pending = await w.delete_file("d_pending.txt", "needed-by-undo")
    w.age(t_pending, 400)
    t_old = w.keep("d_old.txt", "unreferenced-old", days=100)
    t_young = w.keep("d_young.txt", "unreferenced-young", days=2)
    rep = retention.cleanup()
    left = w.tokens()
    rec.check("D1 an old UNREFERENCED snapshot is deleted", t_old not in left and [d["token"] for d in rep.deleted] == [t_old], rep)
    rec.check("D2 an ancient snapshot that a pending undo entry needs is KEPT", t_pending in left)
    rec.check("D3 a young unreferenced snapshot is kept", t_young in left)
    lines = await undo.undo_last(1)
    rec.check("D4 ...and the pending undo still restores it", (w.root / "d_pending.txt").read_text() == "needed-by-undo", lines)

    # D5 the age floor
    w.reset_db()
    for t_ in w.tokens():
        shutil.rmtree(w.snaps / t_)
    CFG.files.snapshot_retention_days = 0
    t_hour = w.keep("d_hour.txt", "two hours old", days=2 / 24)
    rep = retention.cleanup()
    rec.check("D5 retention_days = 0 still never deletes a copy younger than a day", t_hour in w.tokens() and not rep.deleted, rep)
    CFG.files.snapshot_retention_days = 30

    # D6 keep newest
    for t_ in w.tokens():
        shutil.rmtree(w.snaps / t_)
    ks = [w.keep(f"d_k{i}.txt", f"k{i}", days=100 + i) for i in range(5)]
    CFG.files.snapshot_keep_newest = 2
    rep = retention.cleanup()
    rec.check("D6 the newest N unreferenced copies are kept whatever their age", sorted(w.tokens()) == sorted(ks[:2]) and len(rep.deleted) == 3, (w.tokens(), rep))
    CFG.files.snapshot_keep_newest = 0
    for t_ in w.tokens():
        shutil.rmtree(w.snaps / t_)

    # D7 damaged / unreadable / foreign are left alone
    t_bad = w.keep("d_bad.txt", "will be damaged", days=100)
    (w.snaps / t_bad / "blob").write_bytes(b"x")
    t_nometa = w.keep("d_nometa.txt", "meta lost", days=100)
    (w.snaps / t_nometa / "meta.json").write_text("{not json")
    (w.snaps / "scratch-notes").mkdir()
    (w.snaps / "scratch-notes" / "mine.txt").write_text("not a snapshot")
    (w.snaps / "readme.txt").write_text("user file")
    t_ok = w.keep("d_ok.txt", "fine", days=100)
    rep = retention.cleanup()
    rec.check("D7 a damaged copy is never deleted", t_bad in w.tokens(), rep)
    rec.check("D8 a copy with unreadable metadata is never deleted", t_nometa in w.tokens())
    rec.check("D9 folders and files that are not snapshots are never touched", (w.snaps / "scratch-notes" / "mine.txt").exists() and (w.snaps / "readme.txt").exists())
    rec.check("D10 ...while the healthy old copy beside them is deleted", t_ok not in w.tokens() and [d["token"] for d in rep.deleted] == [t_ok], rep)
    lst = retention.listing(50)
    st = {r["id"]: r["status"] for r in lst["snapshots"]}
    rec.check("D11 the listing reports them as damaged", st.get(t_bad) == "damaged" and st.get(t_nometa) == "damaged", st)
    for n_ in w.dirs():
        p = w.snaps / n_
        p.unlink() if p.is_file() else shutil.rmtree(p)

    # D12 batch bound + idempotence
    CFG.files.snapshot_cleanup_batch = 2
    bs = [w.keep(f"d_b{i}.txt", f"b{i}", days=100 + i) for i in range(5)]
    r1 = retention.cleanup()
    rec.check("D12 one run deletes at most the batch size (oldest first)", len(r1.deleted) == 2 and r1.remaining == 3 and sorted(d["token"] for d in r1.deleted) == sorted(bs[-2:]), r1)
    r2 = retention.cleanup()
    r3 = retention.cleanup()
    r4 = retention.cleanup()
    rec.check("D13 repeating it finishes the job", len(r2.deleted) == 2 and len(r3.deleted) == 1 and r3.remaining == 0 and w.tokens() == [], (r2, r3))
    before = len(w.journal())
    rec.check("D14 once nothing qualifies, cleanup is a no-op that writes nothing", not r4.deleted and len(w.journal()) == before and not r4.failed, r4)
    CFG.files.snapshot_cleanup_batch = 100

    # D15 dry run
    d1 = w.keep("d_dry1.txt", "dry", days=100)
    dry = retention.cleanup(dry_run=True)
    rec.check("D15 a dry run reports what would go and changes nothing", d1 in w.tokens() and [x["token"] for x in dry.deleted] == [d1] and dry.dry_run, dry)

    # D16 the journal, written before the deletion; interrupted cleanup
    watched_unlink = os.unlink

    def flaky_unlink(p, *a, **k):
        if os.path.basename(str(p)) == "blob" and retention.CLAIM_PREFIX in str(p):
            raise OSError("disk went away")
        return watched_unlink(p, *a, **k)

    os.unlink = flaky_unlink
    try:
        rep = retention.cleanup()
    finally:
        os.unlink = watched_unlink
    leftover = [n_ for n_ in w.dirs() if n_.startswith(retention.CLAIM_PREFIX)]
    rec.check("D16 an interrupted removal leaves a marker, and the snapshot is already unusable", leftover == [retention.CLAIM_PREFIX + d1] and d1 not in w.tokens(), (leftover, rep))
    try:
        recovery.info(d1)
        usable = True
    except recovery.RecoveryError:
        usable = False
    rec.check("D17 ...so it is not usable as a snapshot any more", not usable)
    jl = [j for j in w.journal() if j.get("event") == "delete" and j.get("token") == d1]
    rec.check("D18 the deletion was journaled (before it happened)", len(jl) == 1 and set(jl[0]) >= {"at", "token", "size", "created_at", "kind"}, jl)
    rep = retention.cleanup()
    rec.check("D19 the next cleanup finishes the interrupted one", rep.finished_leftovers == 1 and not [n_ for n_ in w.dirs() if n_.startswith(retention.CLAIM_PREFIX)], rep)
    rep = retention.cleanup()
    rec.check("D20 ...and then there is nothing left to do", not rep.deleted and rep.finished_leftovers == 0)
    rec.check("D21 the journal holds no paths and no content", SECRET not in json.dumps(w.journal()) and all("original_path" not in j and "path" not in j for j in w.journal()))

    # D22 references that appear between planning and deleting
    late = w.keep("d_late.txt", "late-reference", days=100)
    calls = {"n": 0}
    late_row = json.dumps({"skill": "files.restore", "args": {"snapshot": late, "path": str(w.root / "d_late.txt")}})

    def conn_hook():
        # 1st journal read = planning; the 2nd = the re-check right before the deletion. A new
        # undo entry naming the snapshot lands between the two.
        calls["n"] += 1
        if calls["n"] == 2:
            w.add_undo(late_row, applied=0)
        return real_conn()

    store.conn = conn_hook
    try:
        rep = retention.cleanup()
    finally:
        store.conn = real_conn
    rec.check("D22 a reference that appears after planning still protects the snapshot", late in w.tokens() and not rep.deleted, rep)
    w.reset_db()
    shutil.rmtree(w.snaps / late)

    # D23 unreadable journal -> delete nothing
    victim_tok = w.keep("d_fc.txt", "fail closed", days=100)
    w.add_undo("}{broken", applied=0)
    rep = retention.cleanup()
    rec.check("D23 an unreadable undo journal -> nothing is deleted, and it says why", victim_tok in w.tokens() and not rep.deleted and "deleted nothing" in rep.stopped, rep)
    w.reset_db()
    store.conn = broken_conn
    try:
        rep = retention.cleanup()
    finally:
        store.conn = real_conn
    rec.check("D24 an unavailable database -> nothing is deleted", victim_tok in w.tokens() and not rep.deleted and rep.stopped, rep)
    shutil.rmtree(w.snaps / victim_tok)

    # D25 read-only blob
    ro = w.keep("d_ro.txt", "read-only content", days=100)
    os.chmod(w.snaps / ro / "blob", 0o444)
    rep = retention.cleanup()
    rec.check("D25 a read-only recovery copy is still removed", ro not in w.tokens() and not rep.failed, rep)

    # D26 a junction named like a snapshot is never followed
    victim_dir = w.tmp / "outside_victim"
    victim_dir.mkdir()
    (victim_dir / "precious.txt").write_text("outside the store")
    src_tok = w.keep("d_j.txt", "junction source", days=100)
    for f_ in os.listdir(w.snaps / src_tok):
        shutil.move(str(w.snaps / src_tok / f_), str(victim_dir / f_))
    (w.snaps / src_tok).rmdir()
    link = w.snaps / src_tok
    if make_junction(link, victim_dir):
        rep = retention.cleanup()
        rec.check("D26 a junction named like a snapshot is neither followed nor deleted",
                  (victim_dir / "precious.txt").exists() and (victim_dir / "blob").exists() and not rep.deleted, rep)
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
    else:
        rec.check("D26 (junction unavailable on this machine: skipped, counted as passed)", True)
    # D26a.. reparse points (junctions, symlinks) are never followed: not inside a snapshot, not as a
    # leftover claim, not as the folder handed to _remove_tree, not in a legacy snapshot being migrated.
    # os.walk(followlinks=False) DOES descend a junction on Windows, so this needs its own guard.
    def outside_tree(name: str) -> Path:
        d = w.tmp / name
        (d / "sub").mkdir(parents=True)
        (d / "canary.txt").write_text("canary-" + name)
        (d / "sub" / "deep.bin").write_bytes(b"deep-" + name.encode())
        return d

    def drop_link(path: Path) -> None:  # removes ONLY the link (never traverses it)
        try:
            os.rmdir(path)
        except OSError:
            if os.path.lexists(path):
                os.unlink(path)

    out_in = outside_tree("outside_inner")
    h_in = tree_hash(out_in)
    inner_tok = w.keep("d_jin.txt", "inner junction", days=100)
    link_in = w.snaps / inner_tok / "evil"
    if make_junction(link_in, out_in):
        try:
            rep = retention.cleanup()
            rec.check("D26a a junction INSIDE an old snapshot: the snapshot is cleaned, the junction's target is byte-identical",
                      inner_tok not in w.tokens() and tree_hash(out_in) == h_in and not rep.failed, (rep, tree_hash(out_in)))
            rec.check("D26b ...and no half-deleted claim is left behind",
                      not any(n.startswith(retention.CLAIM_PREFIX) for n in w.dirs()), w.dirs())
        finally:
            drop_link(link_in)
            if (w.snaps / inner_tok).exists():
                shutil.rmtree(w.snaps / inner_tok)
    else:
        rec.check("D26a (junction unavailable on this machine: skipped, counted as passed)", True)
        rec.check("D26b (junction unavailable: skipped, counted as passed)", True)

    out_cl = outside_tree("outside_claim")
    h_cl = tree_hash(out_cl)
    claim_link = w.snaps / (retention.CLAIM_PREFIX + tok(26))
    finishes_before = sum(1 for j_ in w.journal() if j_.get("event") == "finish")
    if make_junction(claim_link, out_cl):
        try:
            rep = retention.cleanup()
            rec.check("D26c a leftover `.deleting-` claim that is a junction is refused: target intact, link left, reported",
                      tree_hash(out_cl) == h_cl and os.path.lexists(claim_link) and rep.finished_leftovers == 0
                      and bool(rep.failed), (rep, tree_hash(out_cl)))
            rec.check("D26d ...and no 'finish' is journaled for it",
                      sum(1 for j_ in w.journal() if j_.get("event") == "finish") == finishes_before)
        finally:
            drop_link(claim_link)
    else:
        rec.check("D26c (junction unavailable on this machine: skipped, counted as passed)", True)
        rec.check("D26d (junction unavailable: skipped, counted as passed)", True)

    out_top = outside_tree("outside_top")
    h_top = tree_hash(out_top)
    top_link = w.tmp / "top_link"
    if make_junction(top_link, out_top):
        try:
            try:
                retention._remove_tree(top_link)
                raised = False
            except OSError:
                raised = True
            rec.check("D26e _remove_tree on a folder that IS a junction refuses it and leaves the target alone",
                      raised and tree_hash(out_top) == h_top and os.path.lexists(top_link), tree_hash(out_top))
        finally:
            drop_link(top_link)
    else:
        rec.check("D26e (junction unavailable on this machine: skipped, counted as passed)", True)

    out_sy = outside_tree("outside_symlink")
    h_sy = tree_hash(out_sy)
    sy_snap = w.tmp / "sy_snap"
    sy_snap.mkdir()
    (sy_snap / "meta.json").write_text("{}")
    try:
        os.symlink(out_sy, sy_snap / "dirlink", target_is_directory=True)
        os.symlink(out_sy / "canary.txt", sy_snap / "filelink")
        have_symlinks = True
    except (OSError, NotImplementedError) as exc:  # needs Developer Mode / privilege on Windows
        have_symlinks = False
        print(f"      (symlinks not creatable here: {exc}; D26f skipped, junction variants above still ran)")
    if have_symlinks:
        retention._remove_tree(sy_snap)
        rec.check("D26f symlinks (directory and file) inside a snapshot are removed as links, targets intact",
                  not sy_snap.exists() and tree_hash(out_sy) == h_sy, tree_hash(out_sy))
    else:
        rec.check("D26f (symlinks not creatable on this machine: skipped, counted as passed)", True)
        shutil.rmtree(sy_snap)

    plain = w.tmp / "plain_tree"
    (plain / "a" / "b").mkdir(parents=True)
    (plain / "a" / "b" / "ro.txt").write_text("read-only, nested")
    os.chmod(plain / "a" / "b" / "ro.txt", 0o444)
    (plain / "meta.json").write_text("{}")
    retention._remove_tree(plain)
    rec.check("D26g an ordinary nested (even read-only) tree is still removed completely", not plain.exists())

    out_mg = outside_tree("outside_migrate")
    h_mg = tree_hash(out_mg)
    mg_tok = w.keep("d_mig_link.txt", "legacy with a junction", days=100)
    shutil.move(str(w.snaps / mg_tok), str(w.legacy / mg_tok))
    mg_link = w.legacy / mg_tok / "evil"
    if make_junction(mg_link, out_mg):
        try:
            res = retention.migrate_legacy()
            rec.check("D26h migrating a legacy snapshot that contains a junction is refused: nothing copied in, target intact",
                      mg_tok not in w.tokens() and any(mg_tok in x for x in res["failed"]) and tree_hash(out_mg) == h_mg, res)
        finally:
            drop_link(mg_link)
    else:
        rec.check("D26h (junction unavailable on this machine: skipped, counted as passed)", True)
    shutil.rmtree(w.legacy / mg_tok)
    for n_ in os.listdir(w.snaps):
        if n_.startswith(retention.MIGRATE_PREFIX):
            shutil.rmtree(w.snaps / n_)

    # D27 the legacy store is never cleaned
    lg = recovery.preserve(w.f("d_legacy.txt", "legacy keep"), reason="overwritten by files.write")
    shutil.move(str(w.snaps / lg), str(w.legacy / lg))
    w.age(lg, 400, base=w.legacy)
    before_l = tree_hash(w.legacy)
    rep = retention.cleanup()
    rec.check("D27 an old legacy snapshot is never cleaned up", tree_hash(w.legacy) == before_l and not rep.deleted, rep)
    shutil.rmtree(w.legacy / lg)

    # D28/29 retention policy comes from configuration
    CFG.files.snapshot_retention_days = 5
    c5 = w.keep("d_c5.txt", "six days", days=6)
    c4 = w.keep("d_c4.txt", "four days", days=4)
    rep = retention.cleanup()
    rec.check("D28 a shorter configured retention applies", c5 not in w.tokens() and c4 in w.tokens(), rep)
    CFG.files.snapshot_retention_days = 30
    shutil.rmtree(w.snaps / c4)

    # D30.. opt-in undo window
    w.reset_db()
    _, tw = await w.delete_file("d_win.txt", "window content")
    w.age(tw, 100)
    w.set_undo_age(60)
    rep = retention.cleanup()
    rec.check("D30 with no undo window, a 60-day-old pending undo entry is NOT retired and keeps its snapshot", tw in w.tokens() and w.undo_rows()[0][1] == 0 and rep.retired_undo == 0, (rep, w.undo_rows()))
    CFG.files.undo_window_days = 30
    dry = retention.cleanup(dry_run=True)
    rec.check("D31 a dry run with a window reports the retirement but changes nothing", dry.retired_undo == 1 and w.undo_rows()[0][1] == 0 and tw in w.tokens() and [x["token"] for x in dry.deleted] == [tw], dry)
    young_p, ty = await w.delete_file("d_young_undo.txt", "recent")
    w.age(ty, 100)  # old copy, but its undo entry is brand new
    rows_before = w.undo_rows()
    rep = retention.cleanup()
    rec.check("D32 with a window, the OLD pending entry is retired and its snapshot goes", rep.retired_undo == 1 and tw not in w.tokens() and w.undo_rows()[0][1] == undo.RETIRED, (rep, w.undo_rows()))
    rec.check("D33 ...but a recent pending entry (even with an old copy) is untouched", ty in w.tokens() and w.undo_rows()[1][1] == 0)
    rec.check("D34 a retired entry is off the undo list", all(e["id"] != rows_before[0][0] for e in undo.pending(10)))
    CFG.files.undo_window_days = 0

    # D35 the cleanup is an Executor call: L3, asks, audited, unattended denied
    w.reset_db()
    for n_ in w.dirs():
        p = w.snaps / n_
        p.unlink() if p.is_file() else shutil.rmtree(p)
    ex = w.keep("d_exec.txt", "exec", days=100)
    w.asked.clear()
    w.answer = False
    r = await w.run("files.recovery_cleanup")
    rec.check("D35 a declined cleanup deletes nothing", ex in w.tokens() and not ok(r), said(r))
    rec.check("D36 ...it asked, and the question says what would be deleted and that it is permanent",
              bool(w.asked) and "PERMANENTLY delete 1 recovery copy" in w.asked[-1][1] and "cannot be brought back" in w.asked[-1][1], w.asked)
    w.answer = True
    r = await w.run("files.recovery_cleanup", actor="scheduler")
    rec.check("D37 an unattended actor cannot clean up", isinstance(r, PermissionError_) and ex in w.tokens(), said(r))
    r = await w.run("files.recovery_cleanup")
    rec.check("D38 an approved cleanup deletes it and says so", ok(r) and ex not in w.tokens() and "Deleted 1 recovery copy" in said(r), said(r))
    rows = [x for x in audit.recent(50) if x["skill"] == "files.recovery_cleanup"]
    rec.check("D39 every attempt is audited (blocked, denied, confirmed)", {x["decision"] for x in rows} >= {"blocked", "denied", "confirmed"}, [x["decision"] for x in rows])
    rec.check("D40 the skill has no arguments, so a planner cannot loosen the policy", REGISTRY.get("files.recovery_cleanup").params == [])
    r = await w.run("files.recovery_cleanup")
    rec.check("D41 running it again says there is nothing to do", ok(r) and "nothing to clean up" in said(r), said(r))
    rec.check("D42 its verifier checks the copies are really gone", "files.recovery_cleanup" in verify.VERIFIERS and all(
        c.passed for c in verify.VERIFIERS["files.recovery_cleanup"].check({}, {"deleted": [ex]}, {})))
    rec.check("D43 ...and notices when one is not", not all(
        c.passed for c in verify.VERIFIERS["files.recovery_cleanup"].check({}, {"deleted": [w.keep("d_v.txt", "v", 1)]}, {})))
    for n_ in w.tokens():
        shutil.rmtree(w.snaps / n_)

    # ---------------------------------------------------------------- E: listing
    rec.scenario("E: the listing is read-only, minimal, bounded and redacted")
    w.reset_db()
    r = await w.run("files.recoveries")
    rec.check("E1 an empty store says so", ok(r) and "no recovery copies" in said(r), said(r))
    home = Path.home()
    secret_dir = w.root / "password=hunter2hunter2"
    secret_dir.mkdir()
    sp = secret_dir / "diary.txt"
    sp.write_text(SECRET)
    await w.run("files.delete", path=str(sp))
    op = w.f("e_over.txt", SECRET + "-original")
    await w.run("files.write", path=str(op), content="new")
    ep, _ = await w.delete_file("e_deleted.txt", SECRET + "-deleted")
    store_before = tree_hash(w.snaps)
    rows_before = w.undo_rows()
    w.asked.clear()
    r = await w.run("files.recoveries")
    blob = json.dumps(r.data, default=str) + said(r)
    rec.check("E2 the listing succeeds and shows every copy", ok(r) and r.data["total"] == 3 and len(r.data["snapshots"]) == 3, said(r))
    rec.check("E3 no file CONTENT appears anywhere in it", SECRET not in blob)
    rec.check("E4 no content hash is exposed", "sha256" not in blob and not any("sha256" in row for row in r.data["snapshots"]))
    rec.check("E5 a credential-shaped folder name is redacted", "hunter2hunter2" not in blob, blob[:300])
    rec.check("E6 each row says what happened, when, how big, and its status", all(
        row["operation"] in ("delete", "overwrite") and row["created_at"] != "unknown" and row["size"] > 0 and row["status"] == "pending-undo"
        for row in r.data["snapshots"]), r.data["snapshots"])
    rec.check("E7 the operation is described truthfully (2 deletes, 1 overwrite)", sorted(row["operation"] for row in r.data["snapshots"]) == ["delete", "delete", "overwrite"])
    rec.check("E8 the speech carries full ids the user can quote", all(row["id"] in said(r) for row in r.data["snapshots"]))
    rec.check("E9 the user's home folder is shortened", retention.display_path(str(home / "Documents" / "x.txt")) == "~\\Documents\\x.txt", retention.display_path(str(home / "Documents" / "x.txt")))
    rec.check("E10 listing changed nothing (store, journal)", tree_hash(w.snaps) == store_before and w.undo_rows() == rows_before)
    rec.check("E11 it is L0 and needs no confirmation", REGISTRY.get("files.recoveries").tier == "L0" and not w.asked)
    r1 = await w.run("files.recoveries", limit=1)
    r0 = await w.run("files.recoveries", limit=0)
    r9 = await w.run("files.recoveries", limit=999)
    rr = await w.run("files.recoveries", limit="many")
    rec.check("E12 the number shown is bounded (1..50) and junk is refused", len(r1.data["snapshots"]) == 1 and len(r0.data["snapshots"]) == 1 and len(r9.data["snapshots"]) == 3 and not ok(rr), (said(rr)))
    rec.check("E13 the newest comes first", r.data["snapshots"][0]["created_at"] >= r.data["snapshots"][-1]["created_at"])
    gone_ids = [row["id"] for row in r.data["snapshots"] if row["operation"] == "overwrite"]
    rec.check("E14 original_exists tells whether a restore would replace something", [row["original_exists"] for row in r.data["snapshots"] if row["operation"] == "overwrite"] == [True] and not any(row["original_exists"] for row in r.data["snapshots"] if row["operation"] == "delete"))
    bad_t = w.keep("e_bad.txt", "damage me", days=1)
    (w.snaps / bad_t / "blob").write_bytes(b"no")
    lst = retention.listing(50)
    row = [x for x in lst["snapshots"] if x["id"] == bad_t][0]
    rec.check("E15 a damaged copy is listed as damaged and not restorable", row["status"] == "damaged" and not row["restorable"], row)
    retention.SCAN_CAP = 2
    lst = retention.listing(50)
    retention.SCAN_CAP = 5000
    rec.check("E16 an oversized store is scanned in bounded fashion and says it was truncated", lst["truncated"] and lst["total"] == 2, lst["total"])
    shutil.rmtree(w.snaps / bad_t)
    rec.check("E17 the store location is shown, and OneDrive sync is flagged when it applies",
              "store" in r.data and r.data["synced_by_onedrive"] is False)

    # ---------------------------------------------------------------- F: controlled restore
    rec.scenario("F: files.recover -- occupied destinations, missing parents, corruption, interruption")
    w.reset_db()
    for n_ in w.dirs():
        p = w.snaps / n_
        p.unlink() if p.is_file() else shutil.rmtree(p)
    rec.check("F0 files.recover takes only a recovery id (no path to steer)", [p.name for p in REGISTRY.get("files.recover").params] == ["snapshot"])
    fp, ft = await w.delete_file("f_gone.txt", "F-ORIGINAL-BYTES")
    w.reset_db()  # the user no longer has this on the undo list: recover by id instead
    w.asked.clear()
    r = await w.run("files.recover", snapshot=ft)
    rec.check("F1 recovering a deleted file restores its bytes", ok(r) and fp.read_text() == "F-ORIGINAL-BYTES", said(r))
    rec.check("F2 it asked first (L2) and the preview names the destination", any(a[0] == "files.recover" for a in w.asked) and "f_gone.txt" in w.asked[-1][1], w.asked)
    rec.check("F3 the restore is itself undoable (undo removes it again, recoverably)", undo.pending(1) and "files.delete" in undo.pending(1)[0]["inverse"])
    lines = await undo.undo_last(1)
    rec.check("F4 undoing a recover removes the file and keeps it recoverable", not fp.exists() and len(w.tokens()) == 2, lines)
    w.reset_db()

    # occupied destination
    fp.write_text("SOMEONE-WROTE-THIS-LATER")
    w.asked.clear()
    w.answer = False
    r = await w.run("files.recover", snapshot=ft)
    rec.check("F5 a declined recover over an occupied path changes nothing", fp.read_text() == "SOMEONE-WROTE-THIS-LATER" and not ok(r), said(r))
    rec.check("F6 ...it asked again because the destination is occupied (risk escalation)", bool(w.asked), w.asked)
    n_before = len(w.tokens())
    w.answer = True
    r = await w.run("files.recover", snapshot=ft)
    rec.check("F7 an approved recover restores the saved content", fp.read_text() == "F-ORIGINAL-BYTES", said(r))
    rec.check("F8 ...after KEEPING what was there, in a new recovery copy", len(w.tokens()) == n_before + 1 and b"SOMEONE-WROTE-THIS-LATER" in [(w.snaps / x / "blob").read_bytes() for x in w.tokens() if (w.snaps / x / "blob").exists()])
    lines = await undo.undo_last(1)
    rec.check("F9 undoing it brings back what was there", fp.read_text() == "SOMEONE-WROTE-THIS-LATER", lines)
    w.reset_db()
    tier_busy = permissions.evaluate(REGISTRY.get("files.recover"), "text", {"snapshot": ft}).effective_tier or "L2"
    r = await w.run("files.recover", snapshot=ft)
    r = await w.run("files.recover", snapshot=ft)
    rec.check("F10 recovering content that is already in place is a harmless no-op", ok(r) and "already has the saved content" in said(r), said(r))
    fp.unlink()
    tier_free = permissions.evaluate(REGISTRY.get("files.recover"), "text", {"snapshot": ft}).effective_tier or "L2"
    rec.check("F10b an occupied destination escalates the recover from L2 to L3; a free one stays L2",
              (tier_busy, tier_free) == ("L3", "L2"), (tier_busy, tier_free))

    # missing parent
    shutil.rmtree(w.root / "nested", ignore_errors=True)
    np_, nt = await w.delete_file("nested/deep.txt", "deep content")
    shutil.rmtree(w.root / "nested")
    before = tree_hash(w.snaps)
    r = await w.run("files.recover", snapshot=nt)
    rec.check("F11 a missing parent folder is refused with the reason, and nothing is created", not ok(r) and not (w.root / "nested").exists() and "no folder at" in said(r), said(r))
    rec.check("F12 ...and the recovery copy is kept", tree_hash(w.snaps) == before)

    # corrupt snapshot
    cp, ct = await w.delete_file("f_corrupt.txt", "intact original")
    (w.snaps / ct / "blob").write_bytes(b"intact original but altered")
    cp.write_text("live file")
    r = await w.run("files.recover", snapshot=ct)
    rec.check("F13 a corrupt copy is refused and the live file is untouched", not ok(r) and cp.read_text() == "live file" and "damaged" in said(r), said(r))
    rec.check("F14 ...and nothing false is claimed", "Restored" not in said(r))

    # bad ids
    outside = w.tmp / "outside.txt"
    outside.write_text("do not read")
    for bad in ("..\\..\\outside", "../x", "20200101T000000-zzzzzzzzzzzz", "", "x" * 40, str(w.snaps)):
        r = await w.run("files.recover", snapshot=bad)
        rec.check(f"F15 an invalid id is refused ({bad!r})", not ok(r), said(r))
    r = await w.run("files.recover", snapshot="20200101T000000-0123456789ab")
    rec.check("F16 a well-formed but unknown id is refused", not ok(r) and "can't find" in said(r), said(r))

    # interrupted restore
    ip, it = await w.delete_file("f_interrupted.txt", "INTERRUPT-ORIGINAL")
    ip.write_text("current-live")
    real_copy2 = shutil.copy2

    def partial_copy(src, dst, *a, **k):
        if ".friday-restore-" in str(dst):
            Path(dst).write_bytes(b"INTERR")  # a partial temp file, then the process 'dies'
            raise OSError("power cut")
        return real_copy2(src, dst, *a, **k)

    shutil.copy2 = partial_copy
    try:
        r = await w.run("files.recover", snapshot=it)
    finally:
        shutil.copy2 = real_copy2
    rec.check("F17 an interrupted restore leaves the destination exactly as it was", not ok(r) and ip.read_text() == "current-live", said(r))
    rec.check("F18 ...and no temporary file is left behind", not [n_ for n_ in os.listdir(w.root) if ".friday-restore-" in n_])
    r = await w.run("files.recover", snapshot=it)
    rec.check("F19 trying again succeeds", ok(r) and ip.read_text() == "INTERRUPT-ORIGINAL", said(r))

    # folder snapshot
    fdir = w.root / "f_emptydir"
    fdir.mkdir()
    r = await w.run("files.delete", path=str(fdir))
    dt = r.data["snapshot"]
    w.reset_db()
    r = await w.run("files.recover", snapshot=dt)
    rec.check("F20 an empty folder can be recovered", ok(r) and fdir.is_dir(), said(r))
    r = await w.run("files.recover", snapshot=dt)
    rec.check("F21 ...and recovering it onto an existing folder is refused, not merged", not ok(r) and fdir.is_dir(), said(r))

    # unattended / hard deny
    w.reset_db()
    r = await w.run("files.recover", actor="scheduler", snapshot=it)
    rec.check("F22 an unattended actor cannot recover", isinstance(r, PermissionError_), said(r))
    saved_over = dict(CFG.permissions.overrides)
    CFG.permissions.overrides["files.recover"] = "deny"
    try:
        r = await w.run("files.recover", snapshot=it)
    finally:
        CFG.permissions.overrides.clear()
        CFG.permissions.overrides.update(saved_over)
    rec.check("F23 a hard-deny override still blocks it", isinstance(r, PermissionError_), said(r))

    # recover from the legacy store
    paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = w.legacy, w.tmp / "legacy-unused"  # Phase 35: the guard counts LEGACY_SNAPSHOTS as a real store; never alias it to the write target
    lp2 = w.f("f_legacy.txt", "from-the-old-store")
    lt = recovery.preserve(lp2, reason="deleted by files.delete", move=True)
    paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = w.snaps, w.legacy
    lst = retention.listing(50)
    rec.check("F24 the listing marks a copy that is still in the legacy store", any(x["id"] == lt and x["location"] == "legacy" for x in lst["snapshots"]), lst["snapshots"])
    r = await w.run("files.recover", snapshot=lt)
    rec.check("F25 ...and it can be recovered from there", ok(r) and lp2.read_text() == "from-the-old-store", said(r))
    shutil.rmtree(w.legacy / lt)

    # ---------------------------------------------------------------- G: privacy
    rec.scenario("G: no file content or path leaks into logs, the cleanup journal or the new skills' audit rows")
    w.reset_db()
    for n_ in w.dirs():
        p = w.snaps / n_
        p.unlink() if p.is_file() else shutil.rmtree(p)
    gp, gt = await w.delete_file("g_private.txt", SECRET + "-g")
    await w.run("files.recoveries")
    w.age(gt, 100)
    w.reset_db()
    await w.run("files.recover", snapshot=gt)
    await w.run("files.delete", path=str(gp))
    w.reset_db()
    w.age(w.tokens()[0], 100)
    await w.run("files.recovery_cleanup")
    ours = [r_ for r_ in log_records if r_.name.startswith(("friday.recovery", "friday.retention", "friday.skills.recoveries"))]
    text = " ".join(r_.getMessage() for r_ in ours)
    rec.check("G1 retention / recovery logging never contains file content", SECRET not in text)
    rec.check("G2 ...or the file's name", "g_private" not in text, text[:200])
    arows = [x for x in audit.recent(200) if x["skill"] in ("files.recoveries", "files.recover", "files.recovery_cleanup")]
    rec.check("G3 audit rows for the new skills hold no content", arows and all(SECRET not in json.dumps(dict(x)) for x in arows))
    rec.check("G4 their audit args hold only a recovery id or a count", all(set(json.loads(x["args"])) <= {"snapshot", "limit"} for x in arows))
    rec.check("G5 the cleanup journal holds no content or path", SECRET not in json.dumps(w.journal()))
    rec.check("G6 the recovery folder name carries no file name", all(recovery.is_token(n_) or n_.startswith(".") or n_ == retention.JOURNAL for n_ in w.dirs()), w.dirs())
    from friday import toolview
    shown = toolview.sanitize(said(await w.run("files.recoveries")))
    rec.check("G7 what reaches the planner via toolview is also free of content", SECRET not in shown)

    # ---------------------------------------------------------------- H: choke point
    rec.scenario("H: the Executor choke point and the planner's view")
    from friday import orchestrator

    offered = {s.name for s in orchestrator._tool_specs(None)}
    mine = {"files.recoveries", "files.recover", "files.recovery_cleanup", "files.recovery_migrate"}
    rec.check("H1 the planner is offered NONE of the recovery skills (operator-only: no prompt growth, no planner-chosen cleanup)",
              not (mine & offered) and "files.write" in offered, sorted(mine & offered))
    rec.check("H2 ...nor the internal files.restore", "files.restore" not in offered)
    rec.check("H2b ...but all of them are registered and run only through the Executor", all(REGISTRY.get(n) is not None and REGISTRY.get(n).internal for n in mine))
    from friday import cli as _cli

    seen = []
    for a, tgt in (("list", ""), ("recover", ""), ("bogus", "")):
        rr = await _cli._recovery(a, tgt, 10)
        seen.append(rr.speech)
    rec.check("H2c the console / CLI entry point runs list and explains misuse", "recovery" in seen[0].lower() or "no recovery copies" in seen[0].lower() or "recovery cop" in seen[0].lower())
    rec.check("H2d ...usage errors are refused, not guessed", all(not x.startswith("Deleted") for x in seen[1:]) and "Use:" in seen[1] and "Use:" in seen[2], seen)
    rr = await _cli._recovery("recover", "20200101T000000-0123456789ab", 10)
    rec.check("H2e ...and recover through it is still the Executor's L2 call (asks, then refuses an unknown id)", any(a[0] == "files.recover" for a in w.asked) and not rr.ok, rr.speech)
    w.asked.clear()
    rec.check("H3 tiers: list L0, recover L2, cleanup L3", (REGISTRY.get("files.recoveries").tier, REGISTRY.get("files.recover").tier, REGISTRY.get("files.recovery_cleanup").tier) == ("L0", "L2", "L3"))
    ex_ = str(w.f("h_occ.txt", "occupied"))
    pol = permissions.evaluate(REGISTRY.get("files.recover"), "text", {"snapshot": tok(1)}).policy
    rec.check("H4 recover always confirms", pol == "confirm", pol)
    rec.check("H5 the internal restore keeps its Phase 32 policy (occupied path asks, free path does not)",
              permissions.evaluate(REGISTRY.get("files.restore"), "text", {"snapshot": tok(1), "path": ex_}).policy == "confirm"
              and permissions.evaluate(REGISTRY.get("files.restore"), "text", {"snapshot": tok(1), "path": str(w.root / "none.txt")}).policy == "auto")
    rec.check("H6 a verifier exists for recover and cleanup", "files.recover" in verify.VERIFIERS and "files.recovery_cleanup" in verify.VERIFIERS)
    from friday.brain import matcher

    rec.check("H7 the matcher corpus skips internal skills (and so never offers files.restore)", "skill.internal" in inspect.getsource(matcher))

    # ---------------------------------------------------------------- J: nothing real touched
    rec.scenario("J: nothing outside the temp folder was touched")
    rec.check("J1 the tripwire on os.unlink / os.rmdir never saw the user's real stores", w.violations == [], w.violations)
    rec.check("J2 both stores were redirected for the whole run", paths.SNAPSHOTS == w.snaps and paths.LEGACY_SNAPSHOTS == w.legacy)


# -- source-level mutation ---------------------------------------------------------------------


@contextlib.contextmanager
def both(first, second):
    with first, second:
        yield


MUTATIONS = [
    ("an undo entry no longer protects its snapshot (everything unreferenced)",
     lambda: patched(retention, "_protected", lambda refs: False)),
    ("a given-up undo entry no longer protects its snapshot",
     lambda: patched(retention, "PROTECTING", (0,))),
    ("there is no minimum age (retention_days = 0 deletes fresh copies)",
     lambda: mutated(retention, "_policy", "max(MIN_AGE_DAYS, int(f.snapshot_retention_days))", "int(f.snapshot_retention_days)")),
    ("keep-newest is ignored",
     lambda: mutated(retention, "make_plan", "pool[keep:]", "pool[0:]")),
    ("the references are not re-checked right before each deletion",
     lambda: mutated(retention, "cleanup", "if _protected(fresh.get(e.token, set())) or recovery.is_link(e.folder) or is_reparse_path(e.folder) or not e.folder.is_dir():", "if recovery.is_link(e.folder) or is_reparse_path(e.folder) or not e.folder.is_dir():")),
    ("an unreadable undo entry is skipped instead of failing closed",
     lambda: mutated(retention, "references", 'raise RetentionError(f"undo entry #{row[\'id\']} is unreadable") from exc', "continue")),
    ("an unreadable journal is treated as 'no references'",
     lambda: mutated(retention, "references", 'raise RetentionError(f"I couldn\'t read the undo journal ({exc})") from exc', "return {}")),
    ("damaged snapshots become deletable",
     lambda: mutated(retention, "make_plan", 'e.state in ("ok", "incomplete")', 'e.state in ("ok", "incomplete", "damaged")')),
    ("the legacy store is cleaned too",
     lambda: mutated(retention, "make_plan", 'e.location == "current" and ', "")),
    ("the deletion is not journaled before it happens",
     lambda: mutated(retention, "cleanup", '_journal(base, {"event": "delete", **record})', "pass")),
    ("links are followed (a junction named like a snapshot is deleted through)",
     lambda: both(patched(recovery, "is_link", lambda p: False), patched(retention, "is_reparse_path", lambda p: False))),
    ("cleanup descends a junction inside a snapshot folder and deletes its target",
     lambda: mutated(retention, "_remove_children", "if _is_reparse(st):", "if False:")),
    ("_remove_tree deletes through a folder that is itself a junction",
     lambda: mutated(retention, "_remove_tree", "if is_reparse_path(path):", "if False:")),
    ("a legacy snapshot containing a junction is migrated (copied through it)",
     lambda: mutated(retention, "migrate_legacy", "if recovery.is_link(src) or _has_reparse(src):", "if recovery.is_link(src):")),
    ("a cleanup run is not bounded by the batch size",
     lambda: mutated(retention, "make_plan", "candidates[:batch]", "candidates")),
    ("a zero undo window retires every pending entry (it must mean 'never')",
     lambda: both(
         mutated(retention, "cleanup", "if not dry_run and int(CFG.files.undo_window_days) > 0:", "if not dry_run:"),
         mutated(undo, "retire_older_than", "if days <= 0:", "if False:"))),
    ("the listing exposes the content hash",
     lambda: mutated(retention, "listing", '"restorable": e.state == "ok",', '"restorable": e.state == "ok", "sha256": meta.get("sha256"),')),
    ("paths in the listing are not redacted",
     lambda: patched(retention, "display_path", lambda raw: str(raw or ""))),
    ("recover over an occupied path no longer asks again",
     lambda: patched(REGISTRY.get("files.recover"), "risk", lambda **kw: False)),
    ("a corrupt recovery copy is restored without verification",
     lambda: mutated(recovery, "verify_at", 'ok = (not is_link(blob) and blob.is_file() and blob.stat().st_size == meta["size"]\n                  and sha256_of(blob) == meta["sha256"])', "ok = True")),
    ("an unsafe snapshot_dir (drive root / home) is accepted",
     lambda: mutated(recovery, "root", "if resolved == resolved.parent or resolved == home:", "if False:")),
    ("the default store goes back inside the repository",
     lambda: mutated(paths, "_default_snapshots", 'return Path(local) / "FRIDAY" / "snapshots"', 'return DATA / "snapshots"')),
    ("migration deletes the legacy source",
     lambda: mutated(retention, "migrate_legacy", '_journal(base, {"event": "migrate", "token": name})', '_journal(base, {"event": "migrate", "token": name}); shutil.rmtree(src)')),
    ("the legacy store is no longer protected from file commands",
     lambda: mutated(recovery, "in_store", "bases.append(legacy.resolve())", "pass")),
    ("a snapshot in the legacy store can no longer be located",
     lambda: mutated(recovery, "locate", "if legacy is not None and (legacy / token / \"meta.json\").is_file():", "if False:")),
]


def main() -> int:
    t0 = time.perf_counter()
    real_default = paths._default_snapshots()
    real_before = sorted(os.listdir(real_default)) if real_default.exists() else []
    legacy_before = sorted(os.listdir(paths.LEGACY_SNAPSHOTS)) if Path(paths.LEGACY_SNAPSHOTS).exists() else []

    loud = Rec(loud=True)
    asyncio.run(suite(loud))

    scenario("K: mutation testing -- break each retention / privacy invariant; the suite must notice")
    clean = not loud.failed
    check("K0 the unmutated suite is clean (mutations only count against a green baseline)", clean, loud.failed[:6])
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
        check(f"K mutation caught: {label}", caught and clean, "survived" if not caught else "")
    check(f"K all {len(MUTATIONS)} mutations killed", killed == len(MUTATIONS) and clean, f"{killed}/{len(MUTATIONS)}")

    scenario("L: the user's real stores were never touched by the suite")
    real_after = sorted(os.listdir(real_default)) if real_default.exists() else []
    legacy_after = sorted(os.listdir(paths.LEGACY_SNAPSHOTS)) if Path(paths.LEGACY_SNAPSHOTS).exists() else []
    check("L1 the real recovery store is unchanged", real_before == real_after, (real_before, real_after))
    check("L2 the old in-repository data/snapshots is unchanged", legacy_before == legacy_after, (legacy_before, legacy_after))

    return finish("Phase 33.0 -- recovery-store privacy, retention and user-controlled recovery", time.perf_counter() - t0, min_assertions=150, min_scenarios=9)


if __name__ == "__main__":
    sys.exit(main())
