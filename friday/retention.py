"""The recovery store as the user sees and manages it (Phase 33): what is in it, why it is kept,
and how it is cleaned up -- without ever pulling the rug from a pending undo.

How a snapshot is "referenced": the undo journal (`undo` table) stores, for each reversible
action, an inverse such as `files.restore(snapshot=<token>, path=...)`. A snapshot is therefore
NEEDED while an undo entry that names its token is pending (applied = 0, which includes a
reversal that is in progress or was only half done) or was given up on after repeated failures
(applied = -1: it no longer shows on the undo list, but its copy may be the last one). The
references are read straight from the journal, as every token-shaped string in an entry's
inverse, so a future inverse shape that mentions a snapshot is protected without a code change.

Cleanup rules (all must hold for a snapshot to be deleted):
  * no pending / in-progress / given-up undo entry names it, re-checked immediately before each
    deletion;
  * its metadata is readable and gives an age (a damaged or unreadable snapshot is never deleted:
    it may be the user's only chance of salvaging something);
  * it is older than `files.snapshot_retention_days` (never less than MIN_AGE_DAYS, so a copy
    from a call that is still running will not qualify);
  * it is not among the newest `files.snapshot_keep_newest` unreferenced copies.
If the journal cannot be read or understood, NOTHING is deleted (fail closed). The work per run is
bounded by `files.snapshot_cleanup_batch`. A deletion is two steps -- an atomic rename to
`.deleting-<id>`, then removal -- so an interrupted run leaves a marker the next run finishes, and
running it again is harmless. Every deletion is written to `cleanup.jsonl` in the store BEFORE it
happens (no id, size and age -- never a path or any content), and the whole run is an audited
Executor call that asks first. The legacy (Phase 32, in-repository) store is read but never cleaned.

Known limits: the reference check and the rename are separate steps, so an undo entry that starts
naming a snapshot in between is not seen (a second process writing to the store is not guarded
against). Link handling is lstat-then-act: a snapshot folder that is itself a link is refused, and a
link found inside is removed as an entry only (never traversed), but a folder swapped for a junction
between the lstat and the rmdir / scandir is not caught.

Nothing here reads a snapshot's content except `migrate_legacy` (which verifies by hash), and no
listing contains any content.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from friday import recovery, store, toolview, undo
from friday.config import CFG
from friday.log import get
from friday.recovery import RecoveryError

log = get(__name__)

_TOKEN_IN_TEXT = re.compile(r"\d{8}T\d{6}-[0-9a-f]{12}")
PROTECTING = (0, -1)  # undo `applied` values that keep a snapshot alive
MIN_AGE_DAYS = 1
SCAN_CAP = 5000  # most snapshot folders one scan looks at
JOURNAL = "cleanup.jsonl"
CLAIM_PREFIX = ".deleting-"
MIGRATE_PREFIX = ".migrating-"


class RetentionError(Exception):
    """The references of the recovery store could not be determined reliably: delete nothing."""


# -- references ---------------------------------------------------------------------------------


def references(*, retire_before: datetime | None = None) -> dict[str, set[int]]:
    """token -> the `applied` values of every undo entry that names it.

    `retire_before` previews the opt-in undo window: a pending entry older than it is counted as
    retired (-3), exactly as `undo.retire_older_than` would leave it.
    Raises RetentionError when the journal cannot be read, or when an entry that would keep a
    snapshot alive cannot be understood -- the caller must then delete nothing."""
    cutoff = retire_before.isoformat() if retire_before else None
    try:
        rows = store.conn().execute("SELECT id, at, applied, inverse FROM undo").fetchall()
    except Exception as exc:
        raise RetentionError(f"I couldn't read the undo journal ({exc})") from exc
    refs: dict[str, set[int]] = {}
    for row in rows:
        applied = int(row["applied"])
        text = row["inverse"] if isinstance(row["inverse"], str) else ""
        if applied == 0 and cutoff and str(row["at"]) < cutoff:
            applied = undo.RETIRED
        if applied in PROTECTING:
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise RetentionError(f"undo entry #{row['id']} is unreadable") from exc
            if not isinstance(data, dict) or data.get("unreadable"):
                raise RetentionError(f"undo entry #{row['id']} is unreadable")
        for token in _TOKEN_IN_TEXT.findall(text):
            refs.setdefault(token, set()).add(applied)
    return refs


def _protected(refs: set[int]) -> bool:
    return any(r in PROTECTING for r in refs)


# -- scanning -----------------------------------------------------------------------------------


@dataclass
class Entry:
    token: str
    location: str  # "current" | "legacy"
    folder: Path
    state: str  # "ok" | "incomplete" (no content was ever stored) | "damaged"
    meta: dict[str, Any] | None
    created: float | None
    size: int
    refs: set[int]
    problem: str = ""

    @property
    def protected(self) -> bool:
        return _protected(self.refs)

    @property
    def status(self) -> str:
        if self.state == "damaged":
            return "damaged"
        if 0 in self.refs:
            return "pending-undo"
        if -1 in self.refs:
            return "undo-gave-up"
        if self.state == "incomplete":
            return "incomplete"
        return "used" if self.refs else "kept"


def _created_ts(meta: dict[str, Any]) -> float | None:
    try:
        return time.mktime(time.strptime(str(meta.get("created_at")), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError, TypeError):
        return None


def _entry(base: Path, name: str, location: str, refs: dict[str, set[int]]) -> Entry:
    folder = base / name
    mine = set(refs.get(name, ()))
    if recovery.is_link(folder) or is_reparse_path(folder) or not folder.is_dir():
        return Entry(name, location, folder, "damaged", None, None, 0, mine, "not a plain folder")
    try:
        meta = recovery.read_meta_at(folder, name)
    except RecoveryError as exc:
        return Entry(name, location, folder, "damaged", None, None, 0, mine, str(exc))
    created = _created_ts(meta)
    size = int(meta.get("size") or 0)
    state, problem = "ok", ""
    if meta["kind"] == "file":
        blob = folder / "blob"
        try:
            if not blob.is_file():
                state, problem = "incomplete", "no content was stored"
            elif blob.stat().st_size != size:
                state, problem = "damaged", "its content has a different size than was saved"
        except OSError as exc:
            state, problem = "damaged", str(exc)
    return Entry(name, location, folder, state, meta, created, size, mine, problem)


def _scan_dir(base: Path, location: str, refs: dict[str, set[int]]) -> tuple[list[Entry], list[Path], bool]:
    """(snapshots, leftover `.deleting-*` claims, truncated?) in one store folder."""
    entries: list[Entry] = []
    claims: list[Path] = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return entries, claims, False
    truncated = False
    for name in names:
        if recovery.is_token(name):
            if len(entries) >= SCAN_CAP:
                truncated = True
                break
            entries.append(_entry(base, name, location, refs))
        elif name.startswith(CLAIM_PREFIX) and recovery.is_token(name[len(CLAIM_PREFIX):]):
            claims.append(base / name)
    return entries, claims, truncated


def scan(refs: dict[str, set[int]]) -> tuple[list[Entry], list[Path], bool]:
    """Every snapshot in the current store, then those only in the legacy store."""
    entries, claims, truncated = _scan_dir(recovery.root(), "current", refs)
    legacy = recovery.legacy_root()
    if legacy is not None:
        have = {e.token for e in entries}
        more, _, trunc2 = _scan_dir(legacy, "legacy", refs)
        entries += [e for e in more if e.token not in have]
        truncated = truncated or trunc2
    return entries, claims, truncated


# -- the plan -----------------------------------------------------------------------------------


def _policy() -> tuple[int, int, int]:
    f = CFG.files
    return (
        max(MIN_AGE_DAYS, int(f.snapshot_retention_days)),
        max(0, int(f.snapshot_keep_newest)),
        max(1, int(f.snapshot_cleanup_batch)),
    )


@dataclass
class Plan:
    entries: list[Entry]
    delete: list[Entry]  # this run's deletions, oldest first, at most the batch size
    eligible: int  # how many would qualify in total (>= len(delete))
    claims: list[Path]  # leftovers of an interrupted run
    retire_undo: int  # pending undo entries the opt-in window would retire first
    truncated: bool
    days: int
    keep_newest: int
    batch: int

    @property
    def bytes(self) -> int:
        return sum(e.size for e in self.delete)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out


def _count_retirable(days: int, now: datetime) -> int:
    try:
        row = store.conn().execute(
            "SELECT COUNT(*) AS n FROM undo WHERE applied = 0 AND at < ?",
            ((now - timedelta(days=days)).isoformat(),),
        ).fetchone()
        return int(row["n"])
    except Exception:
        return 0


def make_plan(*, now: float | None = None, preview_window: bool = False) -> Plan:
    """Decide what a cleanup would delete, without deleting anything. Raises RetentionError when
    the undo journal can't be relied on. `preview_window` also counts what the opt-in undo window
    would retire first (used for the confirmation text and a dry run)."""
    days, keep, batch = _policy()
    clock = time.time() if now is None else now
    window = max(0, int(CFG.files.undo_window_days))
    when = datetime.fromtimestamp(clock, timezone.utc)
    cutoff = (when - timedelta(days=window)) if (preview_window and window) else None
    refs = references(retire_before=cutoff)
    entries, claims, truncated = scan(refs)

    pool = [
        e for e in entries
        if e.location == "current" and not e.protected and e.state in ("ok", "incomplete") and e.created is not None
    ]
    pool.sort(key=lambda e: e.created or 0.0, reverse=True)
    old_enough = clock - days * 86400
    candidates = [e for e in pool[keep:] if (e.created or clock) <= old_enough]
    candidates.sort(key=lambda e: e.created or 0.0)
    retire = _count_retirable(window, when) if (preview_window and window) else 0
    return Plan(entries, candidates[:batch], len(candidates), claims, retire, truncated, days, keep, batch)


# -- cleanup ------------------------------------------------------------------------------------


@dataclass
class Report:
    dry_run: bool
    deleted: list[dict[str, Any]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    finished_leftovers: int = 0
    retired_undo: int = 0
    freed: int = 0
    remaining: int = 0  # eligible snapshots a further run would handle
    stopped: str = ""  # why the run stopped early, if it did


def _journal(base: Path, record: dict[str, Any]) -> None:
    """Append one line to the store's cleanup journal. If this fails the caller must not delete."""
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    base.mkdir(parents=True, exist_ok=True)
    with open(base / JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
_MAX_DEPTH = 64  # a snapshot is `meta.json` + `blob`; anything deeper is not ours


def _is_reparse(st: os.stat_result) -> bool:
    """A symlink, a junction (mount point) or any other reparse point -- found from the entry's own
    lstat, because `os.walk` / `DirEntry.is_symlink` / `Path.is_symlink` report a junction as a
    plain folder and would descend into it."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def is_reparse_path(path: Path | str) -> bool:
    try:
        return _is_reparse(os.lstat(path))
    except OSError:
        return False


def _has_reparse(path: Path, depth: int = 0) -> bool:
    """True when anything at or under `path` is a link of any kind (never followed)."""
    try:
        if _is_reparse(os.lstat(path)):
            return True
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return False
        if depth > _MAX_DEPTH:
            return True
        with os.scandir(path) as it:
            return any(_has_reparse(Path(e.path), depth + 1) for e in list(it))
    except OSError:
        return True  # cannot tell -> treat as unsafe


def _remove_children(folder: str, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise OSError(f"{os.path.basename(folder)} is nested too deeply to be a recovery copy")
    with os.scandir(folder) as it:
        entries = list(it)
    for entry in entries:
        p = entry.path
        st = os.lstat(p)
        if _is_reparse(st):
            # Remove the LINK ITSELF and never what it points at: rmdir on a junction / directory
            # link only removes the link, unlink on a file link only removes the link.
            if stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & _DIRECTORY:
                os.rmdir(p)
            else:
                os.unlink(p)
        elif stat.S_ISDIR(st.st_mode):
            _remove_children(p, depth + 1)
            os.rmdir(p)
        else:
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            os.unlink(p)


def _remove_tree(path: Path) -> None:
    """Remove a snapshot folder (read-only blobs included) without following a link it can see.

    A folder that is itself a link / junction / reparse point is REFUSED (OSError) and left alone:
    deleting "through" it would delete its target. A link found inside is removed as an entry (the
    link only; its target is not read, listed or changed). Each entry is checked with lstat just before
    it is acted on; a swap between the check and the action is not caught."""
    if is_reparse_path(path):
        raise OSError(f"{os.path.basename(str(path))} is a link, not a recovery folder; I won't delete through it")
    _remove_children(str(path), 0)
    os.rmdir(path)


def cleanup(*, dry_run: bool = False, now: float | None = None) -> Report:
    """Delete the snapshots the retention policy allows (see the module docstring). Idempotent and
    bounded. With `dry_run` nothing is changed and the report says what would be."""
    report = Report(dry_run=dry_run)
    if not dry_run and int(CFG.files.undo_window_days) > 0:
        report.retired_undo = undo.retire_older_than(int(CFG.files.undo_window_days))
    try:
        plan = make_plan(now=now, preview_window=dry_run)
    except (RetentionError, RecoveryError) as exc:
        report.stopped = f"{exc}, so I deleted nothing"
        return report
    report.remaining = max(0, plan.eligible - len(plan.delete))
    if dry_run:
        report.deleted = [_describe_deleted(e) for e in plan.delete]
        report.freed = plan.bytes
        report.retired_undo = plan.retire_undo
        report.finished_leftovers = len(plan.claims)
        return report

    base = recovery.root()
    # 1. finish what an interrupted run already decided (these were claimed, hence unreferenced).
    for claim in plan.claims:
        try:
            if is_reparse_path(claim):
                raise OSError("is a link, not a recovery folder; left alone")
            _journal(base, {"event": "finish", "claim": claim.name})
            _remove_tree(claim)
            report.finished_leftovers += 1
        except OSError as exc:
            report.failed.append(f"{claim.name}: {exc}")
    # 2. the policy deletions, re-checking the references right before each one.
    for e in plan.delete:
        try:
            fresh = references()
        except RetentionError as exc:
            report.stopped = f"{exc}, so I stopped"
            break
        if _protected(fresh.get(e.token, set())) or recovery.is_link(e.folder) or is_reparse_path(e.folder) or not e.folder.is_dir():
            continue
        record = _describe_deleted(e)
        try:
            _journal(base, {"event": "delete", **record})
        except OSError as exc:
            report.stopped = f"I couldn't write the cleanup journal ({exc}), so I stopped"
            break
        claim = base / (CLAIM_PREFIX + e.token)
        try:
            os.replace(e.folder, claim)
        except OSError as exc:
            report.failed.append(f"{e.token}: {exc}")
            continue
        report.deleted.append(record)
        report.freed += e.size
        try:
            _remove_tree(claim)
        except OSError as exc:  # already unreachable as a snapshot; the next run finishes it
            report.failed.append(f"{claim.name}: {exc} (will be finished by the next cleanup)")
    log.info(
        "recovery cleanup: deleted %d (%d bytes), finished %d leftovers, retired %d undo entries, %d failed",
        len(report.deleted), report.freed, report.finished_leftovers, report.retired_undo, len(report.failed),
    )
    return report


def _describe_deleted(e: Entry) -> dict[str, Any]:
    meta = e.meta or {}
    return {
        "token": e.token, "kind": meta.get("kind"), "size": e.size,
        "created_at": meta.get("created_at"), "reason": meta.get("reason"),
        "sha256_prefix": str(meta.get("sha256") or "")[:12],
    }


# -- listing ------------------------------------------------------------------------------------


def display_path(raw: Any) -> str:
    """A path for showing to the user (and the planner): home folder shortened to ~, credential-shaped
    text redacted, control characters stripped, length bounded. Never any content."""
    text = str(raw or "")
    home = str(Path.home())
    if home and text.lower().startswith(home.lower()):
        text = "~" + text[len(home):]
    return toolview.sanitize(text)[:160]


def _operation(reason: Any) -> str:
    r = str(reason or "")
    if r.startswith("deleted"):
        return "delete"
    if "files.restore" in r:
        return "replaced by a restore"
    if r.startswith(("overwritten", "replaced")):
        return "overwrite"
    return "other"


_STATUS_TEXT = {
    "pending-undo": "on the undo list",
    "undo-gave-up": "undo gave up, copy kept",
    "used": "already restored or superseded",
    "kept": "kept, not on the undo list",
    "incomplete": "no content stored",
    "damaged": "damaged",
}


def listing(limit: int = 10, *, now: float | None = None) -> dict[str, Any]:
    """Read-only summary of the recovery store: the newest `limit` snapshots (minimal metadata,
    no content, no hashes) and totals. Raises RetentionError if the journal can't be read."""
    limit = max(1, min(int(limit), 50))
    clock = time.time() if now is None else now
    refs = references()
    entries, _claims, truncated = scan(refs)
    entries.sort(key=lambda e: e.created or 0.0, reverse=True)
    plan = make_plan(now=clock)
    eligible = {e.token for e in plan.delete}

    rows: list[dict[str, Any]] = []
    for e in entries[:limit]:
        meta = e.meta or {}
        original = meta.get("original_path")
        exists = False
        if original:
            try:
                exists = Path(str(original)).exists()
            except OSError:
                exists = False
        rows.append({
            "id": e.token,
            "operation": _operation(meta.get("reason")),
            "kind": meta.get("kind") or "unknown",
            "path": display_path(original),
            "size": e.size,
            "created_at": meta.get("created_at") or "unknown",
            "status": e.status,
            "meaning": _STATUS_TEXT.get(e.status, e.status),
            "location": e.location,
            "original_exists": exists,
            "restorable": e.state == "ok",
            "cleanup_eligible": e.token in eligible,
        })
    return {
        "snapshots": rows,
        "total": len(entries),
        "total_bytes": sum(e.size for e in entries),
        "by_status": plan.counts(),
        "eligible_for_cleanup": plan.eligible,
        "legacy": sum(1 for e in entries if e.location == "legacy"),
        "truncated": truncated,
        "store": display_path(recovery.root()),
        "synced_by_onedrive": recovery.synced_by_onedrive(),
        "retention_days": plan.days,
    }


# -- migration of the Phase 32 in-repository store ------------------------------------------------


def migrate_legacy(*, dry_run: bool = False) -> dict[str, Any]:
    """Copy snapshots from the legacy (in-repository) store into the current one. Each is verified
    by hash before and after, copied beside its final place and renamed in atomically, and an id that
    already exists with different metadata is reported, never overwritten. The legacy folder is
    NEVER modified or deleted -- remove it yourself once you've checked."""
    out: dict[str, Any] = {"copied": [], "already_there": [], "failed": [], "source": None, "dry_run": dry_run}
    legacy = recovery.legacy_root()
    if legacy is None or not legacy.is_dir():
        return out
    out["source"] = str(legacy)
    base = recovery.root()
    for name in sorted(os.listdir(legacy)):
        if not recovery.is_token(name):
            continue
        src, dest = legacy / name, base / name
        try:
            if recovery.is_link(src) or _has_reparse(src):
                raise RecoveryError("is, or contains, a link")
            recovery.verify_at(src, name)
            if dest.exists():
                same = recovery.read_meta_at(dest, name) == recovery.read_meta_at(src, name)
                (out["already_there"] if same else out["failed"]).append(
                    name if same else f"{name}: a different snapshot with this id already exists"
                )
                continue
            if dry_run:
                out["copied"].append(name)
                continue
            base.mkdir(parents=True, exist_ok=True)
            tmp = base / (MIGRATE_PREFIX + name)
            if tmp.exists():
                _remove_tree(tmp)
            shutil.copytree(src, tmp)
            recovery.verify_at(tmp, name)
            os.replace(tmp, dest)
            _journal(base, {"event": "migrate", "token": name})
            out["copied"].append(name)
        except (RecoveryError, OSError, shutil.Error) as exc:
            out["failed"].append(f"{name}: {exc}")
    return out
