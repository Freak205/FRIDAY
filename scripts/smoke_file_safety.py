"""Phase 32.0 -- filesystem mutation safety and recoverability.

Everything runs against throwaway temp folders, a throwaway undo/audit database
(`store.use_temp_db`) and a throwaway snapshot store (`paths.SNAPSHOTS` is redirected); nothing
touches the user's files, `data/friday.db` or `data/snapshots`. Every mutation goes through
`EXECUTOR.run`, so permission, confirmation, audit and the undo journal are exercised exactly as
in production.

What it pins (the data-loss paths the review found, verified on the Phase 26 code first):
  A  overwrite undo RESTORES the exact original bytes + mtime; it never deletes the path
  B  delete is recoverable (moved to the recovery store, undo restores bytes/mtime/read-only);
     a copy that cannot be kept means the file is NOT deleted
  C  move: collisions keep the destination's original, folders/links/identity/missing parents
     are refused, cross-volume moves roll back, undo of a replacing move restores both files
  D  copy: destination handling, identity, undo
  E  a FAILED call journals no undo (it used to journal "delete <destination>")
  F  the recovery store: unique ids, traversal-proof, corrupt copies refused, never deleted by
     file commands, never overwritten
  G  permissions / audit unchanged: confirmations, unattended denial, overrides, audit rows
  H  files.restore is internal (not offered to the planner / matcher), verifier registered
  I  the undo journal: declined/failed reversals stay pending, multi-step resume, give-up

Then each safety mechanism is deliberately broken (source-level mutation) and the suite must
FAIL -- counted only when the unmutated suite passes.

Not covered: the residual window between the last re-check and os.replace of an approved
overwrite (Windows has no atomic compare-and-replace). The race scenarios below inject a change at
a fixed point BEFORE the re-check or before a no-overwrite publish; they do not (and cannot, here)
prove the window is closed.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import errno
import hashlib
import inspect
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday import audit, paths, permissions, recovery, store, undo, verify  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.permissions import EXECUTOR, PermissionError_  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.skills import files  # noqa: E402

REGISTRY.discover()

OLD_TIME_NS = 1_600_000_000_000_000_000  # 2020-09-13, recognisably not "now"


# -- scaffolding ----------------------------------------------------------------------------------


class Rec:
    """Collects checks. `loud` prints through the shared phase21 counters; mutation runs are silent."""

    def __init__(self, loud: bool) -> None:
        self.loud = loud
        self.failed: list[str] = []

    def check(self, label: str, cond: bool, detail: object = "") -> bool:
        if not cond:
            self.failed.append(label)
        if self.loud:
            check(label, bool(cond), str(detail))
        return bool(cond)

    def scenario(self, title: str) -> None:
        if self.loud:
            scenario(title)


class World:
    """One isolated environment: files, snapshot store, database, confirmation handler."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "work"
        self.root.mkdir()
        self.snaps = tmp / "snaps"
        self.snaps.mkdir()
        self.answer = True
        self.asked: list[tuple[str, str]] = []
        paths.SNAPSHOTS = self.snaps
        paths.LEGACY_SNAPSHOTS = tmp / "legacy_snaps"  # Phase 33: the old in-repo store is also redirected

        async def confirm(skill, args, preview):
            self.asked.append((skill.name, preview))
            return self.answer

        EXECUTOR.set_confirm_handler(confirm)

    def f(self, name: str, data: bytes | str = b"x", mtime_ns: int | None = None) -> Path:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        if mtime_ns is not None:
            os.utime(p, ns=(mtime_ns, mtime_ns))
        return p

    async def run(self, skill: str, actor: str = "text", **args):
        try:
            return await EXECUTOR.run(skill, args, actor=actor)
        except PermissionError_ as exc:
            return exc

    async def undo(self, n: int = 1) -> list[str]:
        return await undo.undo_last(n)

    def snapshot_dirs(self) -> list[str]:
        return sorted(os.listdir(self.snaps))

    def blobs(self) -> list[bytes]:
        out = []
        for d in self.snapshot_dirs():
            b = self.snaps / d / "blob"
            if b.exists():
                out.append(b.read_bytes())
        return out

    def litter(self, folder: Path | None = None) -> list[str]:
        return [n for n in os.listdir(folder or self.root) if ".friday-" in n]

    def pending(self) -> list[dict]:
        return undo.pending(50)


def ok(r) -> bool:
    return getattr(r, "ok", False) is True


def said(r) -> str:
    return getattr(r, "speech", None) or str(r)


# -- the suite (returns nothing; failures are recorded on `rec`) -----------------------------------


async def suite(rec: Rec) -> None:
    with tempfile.TemporaryDirectory(prefix="friday_filesafety_") as t, store.use_temp_db():
        real_snaps, real_legacy = paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS
        saved_cap = CFG.files.max_snapshot_mb
        try:
            await _suite(rec, World(Path(t)))
        finally:
            paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = real_snaps, real_legacy
            CFG.files.max_snapshot_mb = saved_cap


async def _suite(rec: Rec, w: World) -> None:
    # ---------------------------------------------------------------- A: overwrite + undo
    rec.scenario("A: overwrite keeps the original; undo RESTORES it (never deletes the path)")
    orig_bytes = b"line1\r\nline2\xff\x00binary-ish\r\n"
    a = w.f("a.txt", orig_bytes, mtime_ns=OLD_TIME_NS)
    r = await w.run("files.write", path=str(a), content="BRAND NEW")
    rec.check("A1 overwrite succeeds", ok(r), said(r))
    rec.check("A2 the file now has the new content", a.read_text(encoding="utf-8") == "BRAND NEW")
    rec.check("A3 data says overwritten and names a snapshot", r.data.get("overwritten") is True and recovery.is_token(r.data.get("snapshot")), r.data)
    rec.check("A4 the original bytes are in the recovery store", orig_bytes in w.blobs())
    inv = w.pending()[0]["inverse"] if w.pending() else ""
    rec.check("A5 the journaled inverse is files.restore, NOT files.delete", "files.restore" in inv and "files.delete" not in inv, inv)
    rec.check("A6 no temp file left behind", not w.litter(), w.litter())

    lines = await w.undo()
    rec.check("A7 undo reports Reversed", bool(lines) and lines[0].startswith("Reversed"), lines)
    rec.check("A8 the path exists again after undo", a.exists())
    rec.check("A9 content is byte-for-byte the original", a.exists() and a.read_bytes() == orig_bytes)
    rec.check("A10 the modified time is the original's", a.exists() and a.stat().st_mtime_ns == OLD_TIME_NS, a.stat().st_mtime_ns if a.exists() else None)

    # undo declined -> nothing changes and the entry stays pending
    b = w.f("b.txt", "before")
    await w.run("files.write", path=str(b), content="after")
    before_pending = len(w.pending())
    w.answer = False
    lines = await w.undo()
    w.answer = True
    rec.check("A11 a declined undo is not reported as reversed", bool(lines) and not lines[0].startswith("Reversed"), lines)
    rec.check("A12 ...the file keeps the new content", b.read_text() == "after")
    rec.check("A13 ...and the entry is still pending", len(w.pending()) == before_pending)
    lines = await w.undo()
    rec.check("A14 saying undo again (approved) restores it", b.read_text() == "before", lines)

    # the user edited the file after FRIDAY's write: undo must not lose the user's edit either
    c = w.f("c.txt", "v1")
    await w.run("files.write", path=str(c), content="v2")
    c.write_text("v3 typed by the user", encoding="utf-8")
    await w.undo()
    rec.check("A15 undo restored v1", c.read_text() == "v1")
    rec.check("A16 ...and the user's later edit was kept in the store, not destroyed", b"v3 typed by the user" in w.blobs())
    await w.undo()
    rec.check("A17 undoing the undo brings the user's edit back", c.read_text() == "v3 typed by the user", c.read_text())

    # new file: undo removes it, recoverably
    n = w.root / "new.txt"
    r = await w.run("files.write", path=str(n), content="fresh")
    rec.check("A18 creating a new file reports Created, no snapshot", ok(r) and "Created" in said(r) and "snapshot" not in r.data, r.data)
    await w.undo()
    rec.check("A19 undo of a create removes the file", not n.exists())
    rec.check("A20 ...and the removed content is recoverable", b"fresh" in w.blobs())
    await w.undo()
    rec.check("A21 undoing that brings the created file back", n.exists() and n.read_text() == "fresh")

    # atomic: a failure mid-write leaves the original alone
    d = w.f("d.txt", "stable original")
    real_fsync = files.os.fsync

    def boom(fd):
        raise OSError(errno.EIO, "disk exploded")

    files.os.fsync = boom
    try:
        n_pending = len(w.pending())
        r = await w.run("files.write", path=str(d), content="half-written replacement")
    finally:
        files.os.fsync = real_fsync
    rec.check("A22 a failing write is reported as failed", not ok(r), said(r))
    rec.check("A23 ...the original is untouched", d.read_text() == "stable original", d.read_text())
    rec.check("A24 ...no temp file litters the folder", not w.litter(), w.litter())
    rec.check("A25 ...and no undo was journaled for it", len(w.pending()) == n_pending)

    # fail closed: if the original cannot be kept, it is not overwritten
    e = w.f("e.txt", "keep me")
    real_copy2 = recovery.shutil.copy2

    def nope(*a, **k):
        raise OSError(errno.ENOSPC, "no space")

    recovery.shutil.copy2 = nope
    try:
        r = await w.run("files.write", path=str(e), content="clobber")
    finally:
        recovery.shutil.copy2 = real_copy2
    rec.check("A26 no recoverable copy -> the overwrite is refused", not ok(r) and e.read_text() == "keep me", said(r))
    rec.check("A27 ...and the refusal says why", "recoverable" in said(r).lower(), said(r))

    CFG.files.max_snapshot_mb = 1
    big = w.f("big.bin", b"B" * (2 * 1024 * 1024))
    r = await w.run("files.write", path=str(big), content="small")
    rec.check("A28 a file over the size cap is not overwritten", not ok(r) and big.stat().st_size == 2 * 1024 * 1024, said(r))
    r = await w.run("files.delete", path=str(big))
    rec.check("A29 ...nor deleted", not ok(r) and big.exists(), said(r))
    CFG.files.max_snapshot_mb = 256

    # ---------------------------------------------------------------- B: delete
    rec.scenario("B: delete moves the file to the recovery store; undo restores it exactly")
    payload = bytes(range(256)) * 40
    g = w.f("g.bin", payload, mtime_ns=OLD_TIME_NS)
    os.chmod(g, stat.S_IREAD)
    r = await w.run("files.delete", path=str(g))
    rec.check("B1 delete succeeds", ok(r), said(r))
    rec.check("B2 the file is gone from its place", not g.exists())
    rec.check("B3 data reports a recoverable snapshot", r.data.get("recoverable") is True and recovery.is_token(r.data.get("snapshot")), r.data)
    meta = recovery.info(r.data["snapshot"])
    rec.check("B4 the snapshot records origin, size and hash", meta["original_path"] == str(g) and meta["size"] == len(payload) and meta["sha256"] == hashlib.sha256(payload).hexdigest())
    rec.check("B5 the speech does not claim a permanent deletion", "permanent" not in said(r).lower() and "recover" in said(r).lower(), said(r))
    await w.undo()
    rec.check("B6 undo restores the file", g.exists())
    rec.check("B7 ...with identical bytes", g.exists() and g.read_bytes() == payload)
    rec.check("B8 ...the original modified time", g.exists() and g.stat().st_mtime_ns == OLD_TIME_NS)
    rec.check("B9 ...and the read-only attribute", g.exists() and not (g.stat().st_mode & stat.S_IWRITE))
    os.chmod(g, stat.S_IWRITE | stat.S_IREAD)

    h = w.f("h.txt", "declined delete")
    w.answer = False
    n_snaps = len(w.snapshot_dirs())
    r = await w.run("files.delete", path=str(h))
    w.answer = True
    rec.check("B10 a declined delete leaves the file and keeps nothing", h.exists() and len(w.snapshot_dirs()) == n_snaps, said(r))

    r = await w.run("files.delete", actor="scheduler", path=str(h))
    rec.check("B11 an unattended actor cannot delete at all", isinstance(r, PermissionError_) and h.exists(), said(r))

    ed = w.root / "empty_dir"
    ed.mkdir()
    r = await w.run("files.delete", path=str(ed))
    rec.check("B12 an empty folder can be deleted", ok(r) and not ed.exists(), said(r))
    await w.undo()
    rec.check("B13 ...and undo recreates it", ed.is_dir())
    full = w.root / "full_dir"
    full.mkdir()
    (full / "inside.txt").write_text("in")
    r = await w.run("files.delete", path=str(full))
    rec.check("B14 a folder with files is refused, contents intact", not ok(r) and (full / "inside.txt").exists(), said(r))
    r = await w.run("files.delete", path=str(w.root / "nothing_here.txt"))
    rec.check("B15 a missing path is rejected cleanly", not ok(r), said(r))

    # the original path was re-occupied before restore
    k = w.f("k.txt", "deleted content")
    await w.run("files.delete", path=str(k))
    k.write_text("someone made a new k.txt", encoding="utf-8")
    w.asked.clear()
    await w.undo()
    rec.check("B16 restoring onto an occupied path asks first", any(s == "files.restore" for s, _ in w.asked), w.asked)
    rec.check("B17 ...restores the deleted content", k.read_text() == "deleted content", k.read_text())
    rec.check("B18 ...and keeps what it replaced", b"someone made a new k.txt" in w.blobs())

    # restore with the folder gone
    sub = w.root / "gone_parent"
    sub.mkdir()
    m = w.f("gone_parent/m.txt", "m")
    r = await w.run("files.delete", path=str(m))
    sub.rmdir()
    lines = await w.undo()
    rec.check("B19 restore into a vanished folder fails honestly", lines and not lines[0].startswith("Reversed"), lines)
    rec.check("B20 ...and the recovery copy is kept", b"m" in w.blobs())
    sub.mkdir()
    await w.undo()
    rec.check("B21 once the folder is back, undo works", m.exists() and m.read_text() == "m")

    # delete across volumes: the store is "elsewhere", so the rename fails and it falls back to copy
    x = w.f("x.txt", "cross volume delete")
    real_replace = os.replace

    def exdev_for_blob(src, dst, *a, **k):
        if str(dst).endswith("blob") and not str(src).endswith("blob.partial"):
            raise OSError(errno.EXDEV, "cross-device link")
        return real_replace(src, dst, *a, **k)

    os.replace = exdev_for_blob
    try:
        r = await w.run("files.delete", path=str(x))
    finally:
        os.replace = real_replace
    rec.check("B22 delete falls back to copy+verify+remove across volumes", ok(r) and not x.exists(), said(r))
    await w.undo()
    rec.check("B23 ...and is still restorable", x.exists() and x.read_text() == "cross volume delete")
    y = w.f("y.txt", "locked")
    real_unlink = Path.unlink

    def locked_unlink(self, *a, **k):
        if self == y:
            raise PermissionError(errno.EACCES, "in use")
        return real_unlink(self, *a, **k)

    os.replace = exdev_for_blob
    Path.unlink = locked_unlink
    try:
        r = await w.run("files.delete", path=str(y))
    finally:
        Path.unlink = real_unlink
        os.replace = real_replace
    rec.check("B24 if the original can't be removed, delete reports failure and the file remains", not ok(r) and y.exists() and y.read_text() == "locked", said(r))

    # ---------------------------------------------------------------- C: move
    rec.scenario("C: move -- destination rules, collisions, cross-volume rollback, undo")
    s1 = w.f("s1.txt", "SRC1")
    dst = w.root / "moved1.txt"
    r = await w.run("files.move", src=str(s1), dst=str(dst))
    rec.check("C1 a plain move works", ok(r) and dst.read_text() == "SRC1" and not s1.exists(), said(r))
    await w.undo()
    rec.check("C2 undo moves it back", s1.read_text() == "SRC1" and not dst.exists())

    folder = w.root / "dest_folder"
    folder.mkdir()
    r = await w.run("files.move", src=str(s1), dst=str(folder))
    rec.check("C3 moving into an existing folder keeps the name", ok(r) and (folder / "s1.txt").read_text() == "SRC1", said(r))
    await w.undo()

    # collision with an existing FILE: confirmed, original destination kept, both restored by one undo
    s2 = w.f("s2.txt", "SRC2")
    d2 = w.f("d2.txt", "DEST2-ORIGINAL", mtime_ns=OLD_TIME_NS)
    w.asked.clear()
    r = await w.run("files.move", src=str(s2), dst=str(d2))
    rec.check("C4 a collision is escalated to a confirmation", any(s == "files.move" and "consequential" in p for s, p in w.asked), w.asked)
    rec.check("C5 the move happens after approval", ok(r) and d2.read_text() == "SRC2" and not s2.exists(), said(r))
    rec.check("C6 the destination's original is in the recovery store", b"DEST2-ORIGINAL" in w.blobs())
    rec.check("C7 the speech says a file was replaced", "replacing" in said(r), said(r))
    lines = await w.undo()
    rec.check("C8 one undo puts the moved file back", s2.exists() and s2.read_text() == "SRC2", lines)
    rec.check("C9 ...and restores the replaced destination, with its time", d2.exists() and d2.read_text() == "DEST2-ORIGINAL" and d2.stat().st_mtime_ns == OLD_TIME_NS, lines)

    # collision declined
    s3 = w.f("s3.txt", "SRC3")
    d3 = w.f("d3.txt", "DEST3")
    w.answer = False
    r = await w.run("files.move", src=str(s3), dst=str(d3))
    w.answer = True
    rec.check("C10 a declined collision changes nothing", s3.read_text() == "SRC3" and d3.read_text() == "DEST3", said(r))

    # refusals
    s4 = w.f("s4.txt", "SRC4")
    r = await w.run("files.move", src=str(s4), dst=str(w.root / "nofolder" / "s4.txt"))
    rec.check("C11 a missing destination parent is refused, source intact", not ok(r) and s4.exists(), said(r))
    r = await w.run("files.move", src=str(s4), dst=str(w.root / "newdir") + os.sep)
    rec.check("C12 'newdir/' that doesn't exist is an error, not a file named newdir", not ok(r) and s4.exists() and not (w.root / "newdir").exists(), said(r))
    n_pending = len(w.pending())
    r = await w.run("files.move", src=str(s4), dst=str(s4))
    rec.check("C13 moving a file onto itself is a no-op", ok(r) and r.data.get("noop") is True and s4.read_text() == "SRC4", said(r))
    rec.check("C14 ...and journals no undo", len(w.pending()) == n_pending)
    (folder / "s4.txt").mkdir()
    r = await w.run("files.move", src=str(s4), dst=str(folder))
    rec.check("C15 a folder in the way is refused, nothing lost", not ok(r) and s4.exists() and (folder / "s4.txt").is_dir(), said(r))
    r = await w.run("files.move", src=str(w.root / "ghost.txt"), dst=str(folder))
    rec.check("C16 a missing source is rejected", not ok(r), said(r))
    r = await w.run("files.move", src=str(folder), dst=str(w.root / "other"))
    rec.check("C17 folders are still not movable", not ok(r) and folder.exists(), said(r))

    if os.name == "nt":
        cs = w.f("Notes.txt", "case")
        r = await w.run("files.move", src=str(cs), dst=str(w.root / "notes.txt"))
        names = os.listdir(w.root)
        rec.check("C18 a case-only rename is a real rename", ok(r) and "notes.txt" in names and "Notes.txt" not in names, (said(r), names))

    # cross-volume move (simulated): copy, verify, swap, remove the original
    v1 = w.f("v1.txt", "VOLUME ONE")
    vd = w.f("vd.txt", "VOLUME DEST ORIGINAL")

    def exdev_for_move(src, dst, *a, **k):
        if Path(src) == v1:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_replace(src, dst, *a, **k)

    os.replace = exdev_for_move
    try:
        r = await w.run("files.move", src=str(v1), dst=str(vd))
    finally:
        os.replace = real_replace
    rec.check("C19 a cross-volume move replaces the destination and removes the source", ok(r) and vd.read_text() == "VOLUME ONE" and not v1.exists(), said(r))
    rec.check("C20 ...no temp litter", not w.litter(), w.litter())
    await w.undo()
    rec.check("C21 ...and its undo restores both files", v1.read_text() == "VOLUME ONE" and vd.read_text() == "VOLUME DEST ORIGINAL")

    # cross-volume move whose original cannot be removed: everything is put back
    v2 = w.f("v2.txt", "STUCK SOURCE")
    vd2 = w.f("vd2.txt", "DEST2 SURVIVES")

    def exdev_v2(src, dst, *a, **k):
        if Path(src) == v2:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_replace(src, dst, *a, **k)

    def stuck_unlink(self, *a, **k):
        if self == v2:
            raise PermissionError(errno.EACCES, "in use")
        return real_unlink(self, *a, **k)

    os.replace = exdev_v2
    Path.unlink = stuck_unlink
    try:
        r = await w.run("files.move", src=str(v2), dst=str(vd2))
    finally:
        Path.unlink = real_unlink
        os.replace = real_replace
    rec.check("C22 a failed cross-volume move reports failure", not ok(r), said(r))
    rec.check("C23 ...the source is intact", v2.exists() and v2.read_text() == "STUCK SOURCE")
    rec.check("C24 ...the destination has its original content back", vd2.read_text() == "DEST2 SURVIVES", vd2.read_text())
    rec.check("C25 ...and nothing is left over", not w.litter(), w.litter())
    v3 = w.f("v3.txt", "STUCK TOO")
    real_rename = os.rename
    os.replace = lambda src, dst, *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "x")) if Path(src) == v3 else real_replace(src, dst, *a, **k)
    # a NEW destination is published with os.rename (it refuses to overwrite), so a cross-volume
    # failure has to be simulated on that primitive too -- exactly as the OS reports it for both
    os.rename = lambda src, dst, *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "x")) if Path(src) == v3 else real_rename(src, dst, *a, **k)
    Path.unlink = lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(errno.EACCES, "in use")) if self == v3 else real_unlink(self, *a, **k)
    try:
        r = await w.run("files.move", src=str(v3), dst=str(w.root / "v3_dest.txt"))
    finally:
        Path.unlink = real_unlink
        os.replace = real_replace
        os.rename = real_rename
    rec.check("C26 a failed cross-volume move to a NEW path leaves no stray copy", not ok(r) and v3.exists() and not (w.root / "v3_dest.txt").exists(), said(r))

    # if the replaced destination can't be kept, nothing moves
    s5 = w.f("s5.txt", "SRC5")
    d5 = w.f("d5.txt", "DEST5")
    recovery.shutil.copy2 = nope
    try:
        r = await w.run("files.move", src=str(s5), dst=str(d5))
    finally:
        recovery.shutil.copy2 = real_copy2
    rec.check("C27 no recoverable copy of the destination -> the move is refused", not ok(r) and s5.read_text() == "SRC5" and d5.read_text() == "DEST5", said(r))

    # ---------------------------------------------------------------- D: copy
    rec.scenario("D: copy -- destination handling and undo")
    c1 = w.f("c1.txt", "C1")
    c2 = w.f("c2.txt", "C2-ORIGINAL")
    r = await w.run("files.copy", src=str(c1), dst=str(c2))
    rec.check("D1 copy over an existing file succeeds after confirmation", ok(r) and c2.read_text() == "C1", said(r))
    rec.check("D2 ...the replaced original is kept", b"C2-ORIGINAL" in w.blobs())
    await w.undo()
    rec.check("D3 undo restores the original destination", c2.read_text() == "C2-ORIGINAL")
    rec.check("D4 ...and the source is untouched", c1.read_text() == "C1")
    cn = w.root / "copy_new.txt"
    r = await w.run("files.copy", src=str(c1), dst=str(cn))
    rec.check("D5 a copy to a new path works", ok(r) and cn.read_text() == "C1")
    await w.undo()
    rec.check("D6 undo removes the new copy (recoverably)", not cn.exists() and b"C1" in w.blobs())
    me = w.f("me.txt", "ME")
    n_pending = len(w.pending())
    r = await w.run("files.copy", src=str(me), dst=str(me))
    rec.check("D7 a file copied onto itself is refused, content intact", not ok(r) and me.read_text() == "ME", said(r))
    rec.check("D8 ...and journals no undo", len(w.pending()) == n_pending)
    (folder / "c1.txt").mkdir()
    r = await w.run("files.copy", src=str(c1), dst=str(folder))
    rec.check("D9 copying onto a folder of the same name is refused", not ok(r) and (folder / "c1.txt").is_dir(), said(r))
    r = await w.run("files.copy", src=str(c1), dst=str(w.root / "no_such" / "x.txt"))
    rec.check("D10 a missing destination folder is refused", not ok(r), said(r))

    # ---------------------------------------------------------------- E: failed ops journal nothing
    rec.scenario("E: a FAILED call journals no undo (it used to journal 'delete <destination>')")
    keep = w.f("keep.txt", "MUST SURVIVE")
    edir = w.root / "preexisting_empty"
    edir.mkdir()
    wdir = w.root / "a_folder_named_like_a_file"
    wdir.mkdir()
    sentinel = w.f("sentinel.txt", "sentinel")
    await w.run("files.write", path=str(sentinel), content="sentinel v2")  # something legitimate to be on top
    before = len(w.pending())
    r1 = await w.run("files.copy", src=str(w.root / "does_not_exist.txt"), dst=str(keep))
    r2 = await w.run("files.mkdir", path=str(edir))
    r3 = await w.run("files.write", path=str(wdir), content="x")
    r4 = await w.run("files.copy", src=str(keep), dst=str(keep))
    rec.check("E1 all four calls failed", not any(ok(r) for r in (r1, r2, r3, r4)), [said(r) for r in (r1, r2, r3, r4)])
    rec.check("E2 none of them added an undo entry", len(w.pending()) == before, len(w.pending()) - before)
    await w.undo()
    rec.check("E3 'undo' reverses the last REAL action, not a phantom one", sentinel.read_text() == "sentinel")
    rec.check("E4 the pre-existing file and folders all survived", keep.read_text() == "MUST SURVIVE" and edir.is_dir() and wdir.is_dir())

    mk = w.root / "made_folder"
    r = await w.run("files.mkdir", path=str(mk))
    rec.check("E5 mkdir works", ok(r) and mk.is_dir())
    await w.undo()
    rec.check("E6 undo of mkdir removes the folder it made", not mk.exists())

    # ---------------------------------------------------------------- F: the recovery store itself
    rec.scenario("F: the recovery store -- unique ids, traversal-proof, verified, protected")
    toks = {recovery.new_token() for _ in range(200)}
    rec.check("F1 generated ids are unique and well-formed", len(toks) == 200 and all(recovery.is_token(x) for x in toks))
    rec.check("F2 traversal-shaped and malformed ids are rejected", not any(recovery.is_token(b) for b in ("../evil", "..", "", "x" * 28, "20261016T220000-../../ev", 5, None)))
    evil = w.root.parent / "evil"
    evil.mkdir()
    (evil / "blob").write_bytes(b"EVIL")
    (evil / "meta.json").write_text(
        '{"token": "../evil", "kind": "file", "size": 4, "sha256": "%s", "original_path": "x"}' % hashlib.sha256(b"EVIL").hexdigest(),
        encoding="utf-8",
    )
    # the store is <tmp>/snaps, so "../evil" from the store reaches <tmp>/evil
    target = w.root / "from_evil.txt"
    r = await w.run("files.restore", snapshot="../evil", path=str(target))
    rec.check("F3 a path-traversal id cannot be used to restore", not ok(r) and not target.exists(), said(r))

    z = w.f("z.txt", "ZZZ original")
    r = await w.run("files.write", path=str(z), content="ZZZ new")
    tok = r.data["snapshot"]
    (w.snaps / tok / "blob").write_bytes(b"corrupted!!")
    lines = await w.undo()
    rec.check("F4 a corrupted recovery copy is refused, not restored", z.read_text() == "ZZZ new" and lines and not lines[0].startswith("Reversed"), lines)
    rec.check("F5 ...and the failure is explained", lines and "damaged" in lines[0], lines)
    # let that doomed entry go (give-up path) so later checks have a clean journal
    for _ in range(undo.MAX_FAILURES - 1):
        await w.undo()
    rec.check("F6 an entry that can never succeed is given up on, not blocking the journal", all("files.restore" not in p["inverse"] or tok not in p["inverse"] for p in w.pending()))

    r = await w.run("files.delete", path=str(w.snaps / tok / "blob"))
    rec.check("F7 files.delete refuses to touch the recovery store", not ok(r) and (w.snaps / tok / "blob").exists(), said(r))
    r = await w.run("files.write", path=str(w.snaps / tok / "meta.json"), content="{}")
    rec.check("F8 files.write refuses the recovery store", not ok(r) and "{}" != (w.snaps / tok / "meta.json").read_text(), said(r))
    r = await w.run("files.move", src=str(w.snaps / tok / "blob"), dst=str(w.root / "stolen.bin"))
    rec.check("F9 files.move refuses to take a snapshot out", not ok(r) and (w.snaps / tok / "blob").exists())
    r = await w.run("files.copy", src=str(z), dst=str(w.snaps / "planted.txt"))
    rec.check("F10 files.copy refuses to plant a file in the store", not ok(r) and not (w.snaps / "planted.txt").exists(), said(r))
    r = await w.run("files.mkdir", path=str(w.snaps / "made_here"))
    rec.check("F11 files.mkdir refuses the store", not ok(r))
    r = await w.run("files.restore", snapshot=tok, path=str(w.snaps / "x.txt"))
    rec.check("F12 files.restore refuses to write into the store", not ok(r))

    before_dirs = set(w.snapshot_dirs())
    for i in range(3):
        q = w.f(f"q{i}.txt", f"q{i}")
        await w.run("files.write", path=str(q), content="over")
        await w.run("files.delete", path=str(q))
    rec.check("F13 snapshots only ever accumulate (nothing is deleted by FRIDAY)", before_dirs <= set(w.snapshot_dirs()) and len(w.snapshot_dirs()) >= len(before_dirs) + 6)

    # an id collision must fail closed, never overwrite an existing snapshot
    victim = w.f("victim.txt", "first original")
    r = await w.run("files.write", path=str(victim), content="first new")
    first = r.data["snapshot"]
    first_blob = (w.snaps / first / "blob").read_bytes()
    real_new = recovery.new_token
    recovery.new_token = lambda: first
    try:
        victim.write_text("second original", encoding="utf-8")
        r2 = await w.run("files.write", path=str(victim), content="second new")
    finally:
        recovery.new_token = real_new
    rec.check("F14 an id collision refuses the mutation (fail closed)", not ok(r2) and victim.read_text() == "second original", said(r2))
    rec.check("F15 ...and the existing snapshot is untouched", (w.snaps / first / "blob").read_bytes() == first_blob and recovery.info(first)["sha256"] == hashlib.sha256(first_blob).hexdigest())

    # restore is a no-op when the content already matches (a re-run must not pile up copies)
    nz = w.f("nz.txt", "same")
    await w.run("files.write", path=str(nz), content="changed")
    await w.undo()
    snaps_now = len(w.snapshot_dirs())
    rid = []
    for d in w.snapshot_dirs():
        try:
            m = recovery.info(d)
        except recovery.RecoveryError:
            continue
        if m.get("original_path") == str(nz) and m.get("reason", "").startswith("overwritten"):
            rid.append(d)
    rec.check("F16a the overwrite's snapshot can be found", bool(rid), rid)
    r = await w.run("files.restore", snapshot=rid[0], path=str(nz)) if rid else None
    rec.check("F16 restoring content the file already has changes nothing", r is not None and ok(r) and r.data.get("noop") is True and len(w.snapshot_dirs()) == snaps_now, said(r))

    # ---------------------------------------------------------------- G: permissions / audit unchanged
    rec.scenario("G: the Executor choke point -- permissions, confirmation, audit")
    def policy(name, **args):
        return permissions.evaluate(REGISTRY.get(name), "text", args).policy

    new_p = str(w.root / "never_existed.txt")
    ex_p = str(w.f("exists_g.txt", "g"))
    rec.check("G1 writing a NEW file stays automatic", policy("files.write", path=new_p, content="x") == "auto")
    rec.check("G2 overwriting still asks", policy("files.write", path=ex_p, content="x") == "confirm")
    rec.check("G3 copy onto an existing file still asks", policy("files.copy", src=ex_p, dst=ex_p) == "confirm")
    rec.check("G4 move onto an existing file still asks", policy("files.move", src=new_p, dst=ex_p) == "confirm")
    rec.check("G5 delete still asks (L2)", policy("files.delete", path=ex_p) == "confirm")
    rec.check("G6 restoring over an existing file asks", policy("files.restore", snapshot="x", path=ex_p) == "confirm")
    rec.check("G7 restoring to an empty path is automatic", policy("files.restore", snapshot="x", path=new_p) == "auto")
    r = await w.run("files.write", actor="scheduler", path=ex_p, content="x")
    rec.check("G8 an unattended actor cannot overwrite", isinstance(r, PermissionError_), said(r))
    rec.check("G9 ...and the file is untouched", Path(ex_p).read_text() == "g")
    old_over = dict(CFG.permissions.overrides)
    CFG.permissions.overrides["files.delete"] = "deny"
    try:
        r = await w.run("files.delete", path=ex_p)
    finally:
        CFG.permissions.overrides.clear()
        CFG.permissions.overrides.update(old_over)
    rec.check("G10 a hard-deny override still blocks delete", isinstance(r, PermissionError_) and Path(ex_p).exists(), said(r))

    rows = audit.recent(400)
    del_rows = [x for x in rows if x["skill"] == "files.delete" and x["decision"] in ("confirmed", "auto")]
    rec.check("G11 deletes are audited with their args", bool(del_rows) and "path" in del_rows[0]["args"])
    rec.check("G12 declined and denied attempts are audited too", any(x["decision"] == "denied" for x in rows) and any(x["decision"] == "blocked" for x in rows))
    rec.check("G13 undo steps run through the executor as actor 'undo'", any(x["actor"] == "undo" and x["skill"] in ("files.restore", "files.move", "files.delete") for x in rows))
    ur = store.conn().execute("SELECT COUNT(*) AS n FROM undo u JOIN audit a ON a.id = u.audit_id WHERE u.inverse LIKE '%files.restore%'").fetchone()
    rec.check("G14 restore inverses are linked to the audit row that created them", ur["n"] > 0)

    # ---------------------------------------------------------------- H: internal skill
    rec.scenario("H: files.restore is for the undo journal only")
    from friday import orchestrator

    offered = {s.name for s in orchestrator._tool_specs(None)}
    rec.check("H1 the planner is not offered files.restore", "files.restore" not in offered and "files.write" in offered)
    rec.check("H2 it is still a registered, executor-run skill", REGISTRY.get("files.restore") is not None and REGISTRY.get("files.restore").internal)
    from friday.brain import matcher

    src = inspect.getsource(matcher)
    rec.check("H3 the matcher corpus skips internal skills", "skill.internal" in src)
    rec.check("H4 a verifier exists for files.restore", "files.restore" in verify.VERIFIERS)
    mv = verify.VERIFIERS["files.restore"]
    chk = mv.check({"path": str(a)}, {"path": str(a), "kind": "file"}, {})
    rec.check("H5 the restore verifier reads real state", all(c.passed for c in chk))

    # ---------------------------------------------------------------- I: journal behaviour
    rec.scenario("I: the undo journal -- honest status, resumable multi-step reversal")
    from friday.skills import meta

    i1 = w.f("i1.txt", "I-SRC")
    i2 = w.f("i2.txt", "I-DEST-ORIGINAL")
    await w.run("files.move", src=str(i1), dst=str(i2))
    entry = w.pending()[0]
    rec.check("I1 a replacing move journals a two-step inverse", len(undo.steps_of(json.loads(entry["inverse"]))) == 2, entry["inverse"])

    async def confirm_no_restore(skill, args, preview):
        w.asked.append((skill.name, preview))
        return skill.name != "files.restore"

    # the move-back asks too (destination exists? no: i1's path is free) -> auto; restore target i2 is free -> auto.
    # Force the second step to need a human by occupying its target after step one.
    real_step = EXECUTOR.run
    calls = {"n": 0}

    async def run_then_block_second(skill_name, args=None, *, actor="text"):
        calls["n"] += 1
        res = await real_step(skill_name, args, actor=actor)
        if calls["n"] == 1:
            Path(i2).write_text("a file appeared at the restore target", encoding="utf-8")
        return res

    EXECUTOR.run = run_then_block_second
    EXECUTOR.set_confirm_handler(confirm_no_restore)
    try:
        lines = await w.undo()
    finally:
        del EXECUTOR.run  # drop the instance override (re-assigning it would shadow class-level mutations)

        async def confirm(skill, args, preview):
            w.asked.append((skill.name, preview))
            return w.answer

        EXECUTOR.set_confirm_handler(confirm)
    rec.check("I2 a half-finished reversal is not reported as reversed", bool(lines) and not lines[0].startswith("Reversed"), lines)
    rec.check("I3 step one (the move back) really happened", i1.exists() and i1.read_text() == "I-SRC")
    left = w.pending()[0]["inverse"] if w.pending() else ""
    rec.check("I4 the entry stays pending with only the remaining step", "files.restore" in left and "files.move" not in left, left)
    lines = await w.undo()
    rec.check("I5 saying undo again resumes and finishes", i2.read_text() == "I-DEST-ORIGINAL" and i1.read_text() == "I-SRC", lines)
    rec.check("I6 ...the file that appeared meanwhile is kept, not destroyed", b"a file appeared at the restore target" in w.blobs())

    # meta.undo reports failure honestly
    w.answer = False
    await w.run("files.write", path=str(i1), content="overwritten again")
    res = await meta.undo_last(1)
    w.answer = True
    rec.check("I7 meta.undo is not ok when the reversal was declined", not res.ok, res.speech)
    await w.undo()

    await _integrity(rec, w)

    # ---------------------------------------------------------------- J: nothing leaked into data/
    rec.scenario("J: the real data directory is untouched")
    rec.check("J1 no snapshot was written to the real store during this run", paths.SNAPSHOTS == w.snaps)


# -- preserve integrity + move/write races (deterministic fault injection, no timing) ---------------


@contextlib.contextmanager
def os_wrap(name: str, wrapper):
    """Run with os.<name> replaced by `wrapper(real, src, dst, *a, **k)` -- a hook that fires at the
    exact moment the code under test calls the primitive, so a 'race' happens at a fixed point."""
    real = getattr(os, name)
    setattr(os, name, lambda src, dst, *a, **k: wrapper(real, src, dst, *a, **k))
    try:
        yield
    finally:
        setattr(os, name, real)


def _append(path, data: bytes) -> None:
    with open(path, "ab") as fh:
        fh.write(data)


def _exdev(real, src, dst, *a, **k):
    raise OSError(errno.EXDEV, "cross-device link")


async def _integrity(rec: Rec, w: World) -> None:
    import shutil

    payload = bytes(range(256)) * 20
    late = b"LATE-WRITE"
    real_copy2 = shutil.copy2

    # ---------------------------------------------------------------- M: preserve() verifies what it stored
    rec.scenario("M: preserve(move) re-verifies the stored blob; a mismatch fails closed")

    def snap_state():
        return list(w.snapshot_dirs()), len(w.pending())

    # M1: the source changes between the hash and the rename (a writer with share-delete)
    g1 = w.f("m1.bin", payload)
    dirs0, undo0 = snap_state()

    def writer_during_move(real, src, dst, *a, **k):
        if str(dst).endswith("blob") and Path(src) == g1:
            _append(src, late)
        return real(src, dst, *a, **k)

    with os_wrap("replace", writer_during_move):
        r = await w.run("files.delete", path=str(g1))
    rec.check("M1 a source that changes during the move makes delete fail", not ok(r), said(r))
    rec.check("M2 ...the file is still at its original path with ALL its bytes (late write included)",
              g1.exists() and g1.read_bytes() == payload + late)
    rec.check("M3 ...nothing is claimed recoverable", not r.data.get("recoverable") and "recoverable copy is kept" not in said(r), (r.data, said(r)))
    rec.check("M4 ...no undo entry was journaled", snap_state()[1] == undo0)
    rec.check("M5 ...and no snapshot folder was left behind", snap_state()[0] == dirs0, set(snap_state()[0]) - set(dirs0))

    # M6/M7: the stored blob is truncated / corrupted by the move itself
    for tag, damage in (
        ("truncated", lambda p: open(p, "r+b").truncate(len(payload) // 2)),
        ("corrupted", lambda p: (lambda fh: (fh.write(b"CORRUPT!"), fh.close()))(open(p, "r+b"))),
    ):
        gx = w.f(f"m_{tag}.bin", payload)
        dirs0, undo0 = snap_state()

        def damage_after_move(real, src, dst, *a, _gx=gx, _damage=damage, **k):
            out = real(src, dst, *a, **k)
            if str(dst).endswith("blob") and Path(src) == _gx:
                _damage(dst)
            return out

        with os_wrap("replace", damage_after_move):
            r = await w.run("files.delete", path=str(gx))
        rec.check(f"M6 delete with a {tag} stored blob fails", not ok(r), said(r))
        rec.check(f"M6 ...({tag}) the file is put back where it was, not left only in the store", gx.exists())
        rec.check(f"M6 ...({tag}) no recoverable claim, no undo, no snapshot left", not r.data.get("recoverable") and snap_state() == (dirs0, undo0), (r.data, snap_state()))
        rec.check(f"M6 ...({tag}) no byte is dropped: the path holds exactly what the store held", gx.stat().st_size == (len(payload) // 2 if tag == "truncated" else len(payload)) and (tag == "truncated" or gx.read_bytes()[:8] == b"CORRUPT!"), gx.stat().st_size)

    # M8: it changed AND the old name is taken -> the bytes stay in the store, marked, error names the id
    g4 = w.f("m4.bin", payload)
    dirs0, undo0 = snap_state()

    def writer_then_squatter(real, src, dst, *a, **k):
        if str(dst).endswith("blob") and Path(src) == g4:
            _append(src, late)
            out = real(src, dst, *a, **k)
            g4.write_bytes(b"A FOREIGN FILE TOOK THE NAME")
            return out
        return real(src, dst, *a, **k)

    with os_wrap("replace", writer_then_squatter):
        r = await w.run("files.delete", path=str(g4))
    new_dirs = sorted(set(w.snapshot_dirs()) - set(dirs0))
    rec.check("M8 changed-and-occupied: delete fails", not ok(r), said(r))
    rec.check("M9 ...the foreign file at the old name is untouched", g4.read_bytes() == b"A FOREIGN FILE TOOK THE NAME")
    rec.check("M10 ...the user's bytes (with the late write) are kept in the store", len(new_dirs) == 1 and (w.snaps / new_dirs[0] / "blob").read_bytes() == payload + late, new_dirs)
    tok4 = new_dirs[0] if new_dirs else ""
    meta4 = json.loads((w.snaps / tok4 / "meta.json").read_text(encoding="utf-8")) if tok4 else {}
    rec.check("M11 ...the snapshot is marked unverified and still refused by info()", meta4.get("integrity") == "unverified-after-move", meta4)
    try:
        recovery.info(tok4)
        refused_info = False
    except recovery.RecoveryError:
        refused_info = True
    rec.check("M12 ...info() refuses it (fail closed)", refused_info)
    rec.check("M13 ...the error names exactly where the bytes are", tok4 in said(r) and r.data.get("stranded_snapshot") == tok4, (said(r), r.data))
    rec.check("M14 ...no recoverable claim and no undo entry", not r.data.get("recoverable") and snap_state()[1] == undo0)
    rf = w.root / "m4_restored.bin"
    r2 = await w.run("files.restore", snapshot=tok4, path=str(rf))
    rec.check("M15 files.restore refuses the unverified copy", not ok(r2) and not rf.exists(), said(r2))

    # M15b: a snapshot whose blob is a link must be refused (it would restore content from outside the store)
    g5 = w.f("m5.bin", payload)
    dirs5 = set(w.snapshot_dirs())
    await w.run("files.delete", path=str(g5))
    tok5 = sorted(set(w.snapshot_dirs()) - dirs5)[0]
    recovery.info(tok5)  # sound before the patch
    real_is_link = recovery.is_link
    recovery.is_link = lambda p: Path(p).name == "blob" or real_is_link(p)
    try:
        recovery.info(tok5)
        link_refused = False
    except recovery.RecoveryError:
        link_refused = True
    finally:
        recovery.is_link = real_is_link
    rec.check("M15b a snapshot whose blob is a link is refused by info()", link_refused)

    # M16: cross-volume delete whose source changes after the copy was verified
    g5 = w.f("m5.txt", "five original")
    dirs0, undo0 = snap_state()

    def exdev_blob(real, src, dst, *a, **k):
        if str(dst).endswith("blob") and Path(src) == g5:
            raise OSError(errno.EXDEV, "cross-device link")
        return real(src, dst, *a, **k)

    def late_after_copy(srcf, dstf, *a, **k):
        out = real_copy2(srcf, dstf, *a, **k)
        if str(dstf).endswith("blob.partial") and Path(srcf) == g5:
            _append(srcf, b" + late edit")
        return out

    with os_wrap("replace", exdev_blob), patched(shutil, "copy2", late_after_copy):
        r = await w.run("files.delete", path=str(g5))
    rec.check("M16 cross-volume delete of a file that changed after the copy fails", not ok(r), said(r))
    rec.check("M17 ...the file stays, late edit included", g5.exists() and g5.read_text() == "five original + late edit")
    rec.check("M18 ...no undo entry and no recoverable claim", snap_state()[1] == undo0 and not r.data.get("recoverable"), r.data)

    # M19: a copy (overwrite path) that does not verify refuses the overwrite
    for tag, damage in (("truncated", lambda p: open(p, "r+b").truncate(3)), ("flipped", lambda p: (lambda fh: (fh.write(b"ZZ"), fh.close()))(open(p, "r+b")))):
        tgt = w.f(f"m_copy_{tag}.txt", "original content that must survive")
        undo0 = len(w.pending())

        def bad_copy(srcf, dstf, *a, _damage=damage, **k):
            out = real_copy2(srcf, dstf, *a, **k)
            if str(dstf).endswith("blob.partial"):
                _damage(dstf)
            return out

        with patched(shutil, "copy2", bad_copy):
            r = await w.run("files.write", path=str(tgt), content="clobber")
        rec.check(f"M19 an overwrite whose recovery copy is {tag} is refused", not ok(r) and tgt.read_text() == "original content that must survive", said(r))
        rec.check(f"M19 ...({tag}) with no undo journaled and no temp litter", len(w.pending()) == undo0 and not w.litter(), w.litter())

    # M20: the target changes between the snapshot and the swap (write / copy / restore)
    real_keep = files._keep_original

    def keep_then_edit(target, why):
        out = real_keep(target, why)
        target.write_text("EDITED BY THE USER MEANWHILE", encoding="utf-8")
        return out

    wt = w.f("m20_write.txt", "before")
    undo0 = len(w.pending())
    with patched(files, "_keep_original", keep_then_edit):
        r = await w.run("files.write", path=str(wt), content="FRIDAY'S NEW CONTENT")
    rec.check("M20 write: a target edited after its copy was kept is not replaced", not ok(r) and wt.read_text() == "EDITED BY THE USER MEANWHILE", said(r))
    rec.check("M21 ...the refusal says it changed, journals no undo, leaves no litter", "changed" in said(r) and len(w.pending()) == undo0 and not w.litter(), said(r))
    csrc = w.f("m20_csrc.txt", "copy source")
    ct = w.f("m20_ctarget.txt", "copy target before")
    with patched(files, "_keep_original", keep_then_edit):
        r = await w.run("files.copy", src=str(csrc), dst=str(ct))
    rec.check("M22 copy: same protection", not ok(r) and ct.read_text() == "EDITED BY THE USER MEANWHILE" and csrc.read_text() == "copy source", said(r))

    rt = w.f("m20_restore.txt", "v1")
    await w.run("files.write", path=str(rt), content="v2")
    rt.write_text("v3 typed by the user", encoding="utf-8")
    real_preserve = recovery.preserve

    def preserve_then_race(path, **kw):
        out = real_preserve(path, **kw)
        if Path(path) == rt:
            rt.write_text("v4 racing edit", encoding="utf-8")
        return out

    with patched(recovery, "preserve", preserve_then_race):
        lines = await w.undo()
    rec.check("M23 restore: a target that changed after its copy was kept is not replaced", rt.read_text() == "v4 racing edit" and lines and not lines[0].startswith("Reversed"), lines)
    lines = await w.undo()
    rec.check("M24 ...and a retry (no race) finishes the undo", lines and lines[0].startswith("Reversed") and rt.read_text() == "v1", lines)

    # ---------------------------------------------------------------- N: move races and partial failures
    rec.scenario("N: files.move refuses to overwrite a NEW destination that appeared after the check; put_back leaves a foreign file it can see alone")

    def clean_state():
        return not w.litter()

    # N1: new destination, a foreign file appears between the existence check and the publish
    s1 = w.f("n1_src.txt", "N1 SRC")
    d1 = w.root / "n1_dst.txt"
    undo0 = len(w.pending())

    def foreign_appears(real, src, dst, *a, **k):
        if Path(src) == s1 and Path(dst) == d1:
            d1.write_text("FOREIGN N1", encoding="utf-8")
        return real(src, dst, *a, **k)

    with os_wrap("rename", foreign_appears):
        r = await w.run("files.move", src=str(s1), dst=str(d1))
    rec.check("N1 a file that appears at a new destination before the publish is not overwritten", not ok(r) and d1.read_text() == "FOREIGN N1", said(r))
    rec.check("N2 ...the source is intact, the speech says why, no undo, no litter",
              s1.read_text() == "N1 SRC" and "appeared" in said(r) and len(w.pending()) == undo0 and clean_state(), said(r))

    # N3: destination existed and was approved, but is a different file by the time of the swap
    s3 = w.f("n3_src.txt", "N3 SRC")
    d3 = w.f("n3_dst.txt", "N3 DEST ORIGINAL")
    undo0 = len(w.pending())
    with patched(files, "_keep_original", keep_then_edit):
        r = await w.run("files.move", src=str(s3), dst=str(d3))
    rec.check("N3 an approved replacement is aborted if the destination no longer matches the kept copy",
              not ok(r) and s3.read_text() == "N3 SRC" and d3.read_text() == "EDITED BY THE USER MEANWHILE", said(r))
    rec.check("N4 ...both files kept, no undo journaled", len(w.pending()) == undo0 and clean_state())

    # N5: cross-volume, new destination, a foreign file appears at the publish
    s5 = w.f("n5_src.txt", "N5 SRC")
    d5 = w.root / "n5_dst.txt"
    undo0 = len(w.pending())

    def rename_x(real, src, dst, *a, **k):
        if Path(src) == s5:
            raise OSError(errno.EXDEV, "cross-device link")
        if Path(dst) == d5 and ".friday-tmp-" in str(src):
            d5.write_text("FOREIGN N5", encoding="utf-8")
        return real(src, dst, *a, **k)

    def replace_x(real, src, dst, *a, **k):
        if Path(src) == s5:
            raise OSError(errno.EXDEV, "cross-device link")
        return real(src, dst, *a, **k)

    with os_wrap("rename", rename_x), os_wrap("replace", replace_x):
        r = await w.run("files.move", src=str(s5), dst=str(d5))
    rec.check("N5 cross-volume: the copy is published without overwriting a file that appeared", not ok(r) and d5.read_text() == "FOREIGN N5", said(r))
    rec.check("N6 ...source intact, temp copy removed, no undo", s5.read_text() == "N5 SRC" and clean_state() and len(w.pending()) == undo0, w.litter())

    # N7: cross-volume copy fails midway -> source intact, no partial destination
    s7 = w.f("n7_src.txt", "N7 SRC")
    d7 = w.root / "n7_dst.txt"
    undo0 = len(w.pending())

    def half_copy(srcf, dstf, *a, **k):
        if Path(srcf) == s7:
            Path(dstf).write_bytes(b"N7 ha")
            raise OSError(errno.EIO, "disk error mid-copy")
        return real_copy2(srcf, dstf, *a, **k)

    def rename_s7(real, src, dst, *a, **k):
        if Path(src) == s7:
            raise OSError(errno.EXDEV, "cross-device link")
        return real(src, dst, *a, **k)

    def replace_s7(real, src, dst, *a, **k):
        if Path(src) == s7:
            raise OSError(errno.EXDEV, "cross-device link")
        return real(src, dst, *a, **k)

    with os_wrap("rename", rename_s7), os_wrap("replace", replace_s7), patched(shutil, "copy2", half_copy):
        r = await w.run("files.move", src=str(s7), dst=str(d7))
    rec.check("N7 a copy that fails midway fails the move", not ok(r), said(r))
    rec.check("N8 ...source intact, no partial destination, no litter, no undo",
              s7.read_text() == "N7 SRC" and not d7.exists() and clean_state() and len(w.pending()) == undo0, w.litter())

    # N9/N10: put_back must not touch a destination it cannot prove is its own copy
    for tag, existed_dest in (("new", False), ("approved-replace", True)):
        sx = w.f(f"n9_{tag}_src.txt", f"N9 SRC {tag}")
        dx = w.f(f"n9_{tag}_dst.txt", f"N9 DEST ORIGINAL {tag}") if existed_dest else w.root / f"n9_{tag}_dst.txt"
        undo0 = len(w.pending())

        def rename_sx(real, src, dst, *a, _sx=sx, **k):
            if Path(src) == _sx:
                raise OSError(errno.EXDEV, "cross-device link")
            return real(src, dst, *a, **k)

        def replace_sx(real, src, dst, *a, _sx=sx, **k):
            if Path(src) == _sx:
                raise OSError(errno.EXDEV, "cross-device link")
            return real(src, dst, *a, **k)

        def stuck_and_swapped(self, *a, _sx=sx, _dx=dx, **k):
            if self == _sx:
                _dx.write_bytes(b"FOREIGN REPLACED OUR COPY")  # somebody replaces the published copy ...
                raise PermissionError(errno.EACCES, "in use")  # ... and the original can't be removed
            return real_unlink(self, *a, **k)

        real_unlink = Path.unlink
        Path.unlink = stuck_and_swapped
        try:
            with os_wrap("rename", rename_sx), os_wrap("replace", replace_sx):
                r = await w.run("files.move", src=str(sx), dst=str(dx))
        finally:
            Path.unlink = real_unlink
        rec.check(f"N9 ({tag}) a failed cross-volume move does not delete or restore over a foreign file at the destination",
                  not ok(r) and dx.read_bytes() == b"FOREIGN REPLACED OUR COPY", said(r))
        rec.check(f"N9 ({tag}) ...the source is intact and the words say a different file is there",
                  sx.read_text() == f"N9 SRC {tag}" and "different file" in said(r), said(r))
        if existed_dest:
            rec.check("N9 (approved-replace) ...the replaced original is still recoverable and named", f"N9 DEST ORIGINAL {tag}".encode() in w.blobs() and "recovery id" in said(r), said(r))
        rec.check(f"N9 ({tag}) ...no undo entry for a move that did not happen", len(w.pending()) == undo0 and clean_state())

    # N11: cross-volume, source changed after the copy was published -> the source is NOT removed
    s11 = w.f("n11_src.txt", "N11 SRC")
    d11 = w.root / "n11_dst.txt"
    undo0 = len(w.pending())

    def rename_late(real, src, dst, *a, **k):
        if Path(src) == s11:
            raise OSError(errno.EXDEV, "cross-device link")
        if Path(dst) == d11 and ".friday-tmp-" in str(src):
            _append(s11, b" + late edit")
        return real(src, dst, *a, **k)

    with os_wrap("rename", rename_late), os_wrap("replace", lambda real, src, dst, *a, **k: _exdev(real, src, dst) if Path(src) == s11 else real(src, dst, *a, **k)):
        r = await w.run("files.move", src=str(s11), dst=str(d11))
    rec.check("N11 a source edited during a cross-volume move is not removed", not ok(r) and s11.exists() and s11.read_text() == "N11 SRC + late edit", said(r))
    rec.check("N12 ...no undo claims the move happened", len(w.pending()) == undo0)

    # N13/N14: write / copy to a NEW path use a no-overwrite publish too
    def squat(target):
        def hook(real, src, dst, *a, **k):
            if Path(dst) == target and ".friday-tmp-" in str(src):
                target.write_text("FOREIGN NEW-PATH", encoding="utf-8")
            return real(src, dst, *a, **k)
        return hook

    nw = w.root / "n13_write.txt"
    undo0 = len(w.pending())
    with os_wrap("rename", squat(nw)):
        r = await w.run("files.write", path=str(nw), content="mine")
    rec.check("N13 files.write to a new path does not overwrite a file that appeared", not ok(r) and nw.read_text() == "FOREIGN NEW-PATH" and clean_state() and len(w.pending()) == undo0, said(r))
    nc = w.root / "n14_copy.txt"
    csrc = w.f("n14_src.txt", "N14 SRC")
    with os_wrap("rename", squat(nc)):
        r = await w.run("files.copy", src=str(csrc), dst=str(nc))
    rec.check("N14 files.copy to a new path does not overwrite a file that appeared", not ok(r) and nc.read_text() == "FOREIGN NEW-PATH" and csrc.read_text() == "N14 SRC" and clean_state() and len(w.pending()) == undo0, said(r))

    # N15/N16: undo of a move keeps both files
    s15 = w.f("n15_src.txt", "N15 SRC")
    d15 = w.root / "n15_dst.txt"
    r = await w.run("files.move", src=str(s15), dst=str(d15))
    s15.write_text("N15 REAPPEARED", encoding="utf-8")  # the source name was reused after the move
    w.answer = False
    lines = await w.undo()
    w.answer = True
    rec.check("N15 undo of a move onto a reappeared source asks, and declining changes nothing",
              lines and not lines[0].startswith("Reversed") and s15.read_text() == "N15 REAPPEARED" and d15.read_text() == "N15 SRC", lines)
    lines = await w.undo()
    rec.check("N16 approved: the moved file goes back, and the file that had reappeared is kept in the store",
              s15.read_text() == "N15 SRC" and not d15.exists() and b"N15 REAPPEARED" in w.blobs(), lines)

    s17 = w.f("n17_src.txt", "N17 SRC")
    d17 = w.f("n17_dst.txt", "N17 DEST ORIGINAL", mtime_ns=OLD_TIME_NS)
    r = await w.run("files.move", src=str(s17), dst=str(d17))
    d17.write_text("N17 EDITED AFTER MOVE", encoding="utf-8")
    lines = await w.undo()
    rec.check("N17 undo of an approved replacing move after the moved file was edited keeps the edit and restores the original",
              s17.read_text() == "N17 EDITED AFTER MOVE" and d17.read_text() == "N17 DEST ORIGINAL" and d17.stat().st_mtime_ns == OLD_TIME_NS, lines)


# -- source-level mutation---------------------------------------------------------------------


@contextlib.contextmanager
def mutated(owner, name: str, old: str, new: str, skill: str | None = None):
    """Replace `old` with `new` in the source of owner.name and run with the broken version."""
    target = getattr(owner, name)
    func = target.fget if isinstance(target, property) else target
    src = textwrap.dedent(inspect.getsource(func))
    assert old in src, f"mutation anchor not found in {name}: {old!r}"
    tree = ast.parse(src.replace(old, new, 1))
    fn = tree.body[0]
    fn.decorator_list = []
    ns = dict(vars(inspect.getmodule(func)))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "<mutant>", "exec"), ns)
    broken = ns[fn.name]
    saved_attr = getattr(owner, name)
    saved_fn = REGISTRY.get(skill).fn if skill else None
    setattr(owner, name, broken)
    if skill:
        REGISTRY.get(skill).fn = broken
    try:
        yield
    finally:
        setattr(owner, name, saved_attr)
        if skill:
            REGISTRY.get(skill).fn = saved_fn


@contextlib.contextmanager
def patched(owner, name: str, value):
    saved = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, saved)


MUTATIONS = [
    ("overwrite undo deletes the path instead of restoring it",
     lambda: mutated(files, "write", "undo=_undo_restore(snapshot, p),", "undo=_undo_create(p),", "files.write")),
    ("overwrite does not keep the original first",
     lambda: mutated(files, "write", 'snapshot, refused = _keep_original(p, "overwritten by files.write")', "snapshot, refused = None, None", "files.write")),
    ("write is not atomic (truncates the target in place)",
     lambda: mutated(files, "write", 'with open(tmp, "w", encoding="utf-8") as fh:', 'with open(p, "w", encoding="utf-8") as fh:', "files.write")),
    ("delete unlinks permanently instead of keeping a copy",
     lambda: mutated(files, "delete", 'token = recovery.preserve(p, reason="deleted by files.delete", move=True)', "p.unlink(); token = recovery.new_token()", "files.delete")),
    ("delete will touch the recovery store",
     lambda: mutated(files, "delete", "if (refused := _in_store(p)) is not None:", "if False:", "files.delete")),
    ("the executor journals an undo for FAILED calls",
     lambda: mutated(permissions.Executor, "run", "if result.ok:", "if True:")),
    ("a declined/failed undo is marked applied anyway",
     lambda: mutated(undo, "undo_last", "if not failure and not declined:", "if True:")),
    ("an interrupted multi-step undo forgets its progress",
     lambda: mutated(undo, "undo_last", '_save(entry["id"], {"steps": remaining, "failures": inverse.get("failures", 0)})', "pass")),
    ("copy onto itself is allowed",
     lambda: mutated(files, "copy", "if existed and os.path.samefile(source, dest):", "if False:", "files.copy")),
    ("copy overwrites without keeping the destination",
     lambda: mutated(files, "copy", 'snapshot, refused = _keep_original(dest, "overwritten by files.copy")', "snapshot, refused = None, None", "files.copy")),
    ("move replaces the destination without keeping it",
     lambda: mutated(files, "move", 'snapshot, refused = _keep_original(dest, "replaced by files.move")', "snapshot, refused = None, None", "files.move")),
    ("move onto the same file is not recognised",
     lambda: mutated(files, "move", "if existed and os.path.samefile(source, dest):", "if False:", "files.move")),
    ("a destination ending in a separator becomes a file name",
     lambda: mutated(files, "_destination", 'elif str(dst).rstrip(" ").endswith(("/", "\\\\")):', "elif False:")),
    ("a cross-volume move that fails is not rolled back",
     lambda: mutated(files, "move", 'return SkillResult(speech=f"I couldn\'t move that file: {inner}. {put_back()}", ok=False)', 'return SkillResult(speech=f"I couldn\'t move that file: {inner}.", ok=False)', "files.move")),
    ("restore overwrites the current file without keeping it",
     lambda: mutated(files, "restore_snapshot", 'current = recovery.preserve(p, reason="replaced by files.restore")', "current = None")),
    ("recovery ids are not validated (path traversal)",
     lambda: patched(recovery, "is_token", lambda v: isinstance(v, str))),
    ("recovery copies are not verified before restoring",
     lambda: mutated(recovery, "verify_at", 'ok = (not is_link(blob) and blob.is_file() and blob.stat().st_size == meta["size"]\n                  and sha256_of(blob) == meta["sha256"])', "ok = True")),
    ("a recovery copy that is a link is trusted",
     lambda: mutated(recovery, "verify_at", "ok = (not is_link(blob) and ", "ok = (")),
    ("an existing snapshot can be overwritten by an id collision",
     lambda: mutated(recovery, "_new_folder", "folder.mkdir()", "folder.mkdir(exist_ok=True)")),
    ("the size cap is ignored",
     lambda: patched(recovery, "_limit_bytes", lambda: 10 ** 15)),
    ("a moved-in blob is trusted without re-verifying it",
     lambda: mutated(recovery, "_move_into_store", "_verify_blob(blob, size, digest)", "pass")),
    ("a failed move-in rollback overwrites whatever took the old name",
     lambda: mutated(recovery, "_move_into_store", "os.rename(blob, path)", "os.replace(blob, path)")),
    ("move publishes a new destination with a clobbering replace",
     lambda: mutated(files, "move", "recovery.publish_new(source, dest)", "os.replace(source, dest)", "files.move")),
    ("put_back restores over / removes a destination it cannot prove is its own",
     lambda: mutated(files, "move", 'elif not _same_as(dest, published["size"], published["sha256"]):', "elif False:", "files.move")),
    ("an overwrite swaps in without re-checking the target against its kept copy",
     lambda: mutated(files, "_replace_checked", "if snapshot is not None and target.exists() and not recovery.still_matches(snapshot, target):", "if False:")),
    ("a cross-volume move removes the source without checking it is still what was copied",
     lambda: mutated(files, "_move_across_volumes", "if not _same_as(source, size, digest):", "if False:")),
    ("write to a new path publishes with a clobbering replace",
     lambda: mutated(files, "write", "recovery.publish_new(tmp, p)", "os.replace(tmp, p)", "files.write")),
]


def main() -> int:
    t0 = time.perf_counter()
    snap_before = sorted(os.listdir(paths.SNAPSHOTS)) if Path(paths.SNAPSHOTS).exists() else []
    legacy_before = sorted(os.listdir(paths.LEGACY_SNAPSHOTS)) if Path(paths.LEGACY_SNAPSHOTS).exists() else []

    loud = Rec(loud=True)
    asyncio.run(suite(loud))

    scenario("K: mutation testing -- break each safety mechanism; the suite must notice")
    clean = not loud.failed
    check("K0 the unmutated suite is clean (mutations only count against a green baseline)", clean, loud.failed[:5])
    killed = 0
    for label, make in MUTATIONS:
        quiet = Rec(loud=False)
        try:
            with make():
                asyncio.run(suite(quiet))
        except AssertionError:
            raise
        except Exception as exc:  # a crash is also a detection: the suite did not pass
            quiet.failed.append(f"crashed: {type(exc).__name__}: {exc}")
        caught = bool(quiet.failed)
        killed += caught
        check(f"K mutation caught: {label}", caught and clean, "survived" if not caught else "")
    check(f"K all {len(MUTATIONS)} mutations killed", killed == len(MUTATIONS) and clean, f"{killed}/{len(MUTATIONS)}")

    scenario("L: the real snapshot store was never touched by the suite")
    snap_after = sorted(os.listdir(paths.SNAPSHOTS)) if Path(paths.SNAPSHOTS).exists() else []
    check("L1 the real recovery store is unchanged", snap_before == snap_after, (snap_before, snap_after))
    legacy_after = sorted(os.listdir(paths.LEGACY_SNAPSHOTS)) if Path(paths.LEGACY_SNAPSHOTS).exists() else []
    check("L2 the old in-repository data/snapshots is unchanged", legacy_before == legacy_after, (legacy_before, legacy_after))

    return finish("Phase 32.0 -- filesystem mutation safety and recoverability", time.perf_counter() - t0, min_assertions=150, min_scenarios=11)


if __name__ == "__main__":
    sys.exit(main())
