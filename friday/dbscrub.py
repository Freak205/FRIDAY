"""Reversible, preview-first scrubbing of HISTORICAL audit and episode content (Phase 36).

Phase 35 stopped FRIDAY from storing message bodies, typed text, memory text, code, command lines and URL
query strings in the audit log. Rows written before that still hold them. This module brings those rows to the
SAME form the live code would have written, and nothing else:

    audit.args      -> friday.auditview.sanitize_args(skill, args)
    audit.result    -> sanitize_result(skill, result)    + an echo of an omitted argument value is replaced
    audit.error     -> sanitize_error(error)               by "[omitted: N chars]"
    episodes.plan   -> every step's args / error through the same functions
    episodes.errors, episodes.context -> echoes of omitted values and URL query/fragment only

It never touches goals, corrections, memory, the knowledge base, jobs, the undo journal, ids, timestamps,
actor, skill, tier, decision, ok, duration, or an episode's goal_text/embedding. Each is a separate policy
decision (see PLAN.md Phase 36).

Safety contract (all pinned by scripts/smoke_db_scrub.py):

  * PREVIEW is the default and opens the database read-only. It returns counts grouped by table / field /
    action and a short confirmation code; it never returns a stored value.
  * APPLY needs an explicit target ("active" or "legacy"), the confirmation code the preview printed for that
    exact target in that exact state, and -- for a database inside a cloud-synced folder -- `allow_synced`.
    Active and legacy are never scrubbed together, and nothing here runs at startup, on migration or in tests.
  * Before one byte changes: the target is resolved to a canonical path (aliases refused); an EXCLUSIVE lock is
    taken (any other open connection makes it refuse; a stale -wal/-shm is left to SQLite and never deleted);
    a new backup is made OUTSIDE the repository, OneDrive and the database's own folder with SQLite's online
    backup API, verified (integrity, schema, row counts, logical digest) and proven restorable on a disposable
    copy. If any step fails nothing is modified.
  * The scrub is ONE transaction with `secure_delete` on. Before COMMIT it verifies row counts, ids, schema, the
    digest of every column it did not mean to change, and that a second pass would change nothing. Afterwards the
    file is checkpointed, vacuumed, re-opened and verified again. An exception or a killed process leaves the
    database either unchanged (SQLite rolls the transaction back) or scrubbed-and-verified; the backup stays.
  * After VACUUM the file and its -wal are searched for the replaced values; one still readable (or a failed
    VACUUM) is an ERROR ("committed-NOT-PURGED") answered by `db compact`, never a quiet success.
  * Idempotent: a scrubbed database yields zero changes. Errors never quote stored data (SQLite messages can),
    names taken from rows are bounded to identifiers, and the manifest holds counts and hashes only.
  * The backup keeps the sensitive history. It is read-only, never deleted by FRIDAY, and holds the only copy
    of what was scrubbed -- retire it deliberately.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from friday import auditview, dbplace, paths
from friday.log import get

log = get(__name__)

SCRUB_VERSION = 1
TARGETS = ("active", "legacy")

# Tests inject a failure here (`_FAULT("after-backup")` ...); production leaves it None.
_FAULT: Callable[[str], None] | None = None

# Columns this module may rewrite. Everything else must be byte-identical afterwards (verified).
SCRUBBED_COLUMNS: dict[str, tuple[str, ...]] = {
    "audit": ("args", "result", "error"),
    "episodes": ("plan", "errors", "context"),
}
# Stores that are separate policy decisions: kept whole, listed in the preview so the choice is visible.
RETAINED_BY_POLICY = ("goals", "corrections", "memory", "kb_documents", "kb_chunks", "phrasings", "jobs",
                      "job_runs", "undo")


class ScrubError(RuntimeError):
    """The scrub (or its backup) could not be done safely. The message says what state the database is in."""


def _fault(point: str) -> None:
    if _FAULT is not None:
        _FAULT(point)


# --- targets ------------------------------------------------------------------------------------------

@dataclass(slots=True)
class Target:
    name: str  # "active" | "legacy"
    path: Path  # canonical (symlinks resolved)
    alias: bool  # the path as given differs from its canonical form
    synced: bool
    same_as_other: bool = False


def resolve_target(name: str, *, active: Path | None = None, legacy: Path | None = None) -> Target:
    """Resolve "active" / "legacy" to ONE canonical file. `active`/`legacy` override for tests."""
    name = (name or "").strip().lower()
    if name not in TARGETS:
        raise ScrubError(f"choose a target: one of {', '.join(TARGETS)} (got {name!r})")
    a = Path(active) if active is not None else dbplace.resolve()
    lg = Path(legacy) if legacy is not None else Path(paths.LEGACY_DB)
    raw = a if name == "active" else lg
    try:
        same = dbplace._norm(a) == dbplace._norm(lg) or (a.exists() and lg.exists() and os.path.samefile(a, lg))
    except OSError:
        same = dbplace._norm(a) == dbplace._norm(lg)
    canon = Path(os.path.realpath(os.fspath(raw)))
    alias = os.path.normcase(os.path.abspath(os.fspath(raw))) != os.path.normcase(os.fspath(canon))
    if name == "legacy" and same:
        raise ScrubError("the legacy database is the same file as the active one; select --target active instead")
    return Target(name, canon, alias, dbplace.in_synced_folder(canon), same)


def _require_file(t: Target) -> None:
    if not t.path.is_file():
        raise ScrubError(f"the {t.name} database {t.path} does not exist")
    dbplace.guard(t.path)  # a test/benchmark entry point can never reach a real database through here


def _open_ro(path: Path) -> sqlite3.Connection:
    c = dbplace._open_ro(path)
    c.row_factory = sqlite3.Row
    return c


# --- digests ------------------------------------------------------------------------------------------

def _tables(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _columns(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f'PRAGMA table_info("{table}")')]


def _row_hash(values: tuple) -> bytes:
    h = hashlib.sha256()
    for v in values:
        h.update(repr(v).encode("utf-8", "surrogatepass") if not isinstance(v, bytes) else b"b" + v)
        h.update(b"\x1f")
    return h.digest()


def table_digest(c: sqlite3.Connection, table: str, *, exclude: tuple[str, ...] = ()) -> str:
    """Order-independent digest of a table's rows (minus `exclude` columns). Never reveals content."""
    cols = [x for x in _columns(c, table) if x not in exclude]
    if not cols:
        return ""
    sql = "SELECT " + ", ".join(f'"{x}"' for x in cols) + f' FROM "{table}"'
    hashes = sorted(_row_hash(tuple(r)) for r in c.execute(sql))
    h = hashlib.sha256()
    for x in hashes:
        h.update(x)
    return h.hexdigest()


def snapshot(c: sqlite3.Connection) -> dict[str, Any]:
    """Counts, schema and digests of everything: the 'before' and 'after' of a scrub."""
    c.row_factory = None
    tables = _tables(c)
    out: dict[str, Any] = {
        "counts": {t: int(c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables},
        "schema": sorted(f"{r[0]}|{r[1]}|{r[2]}" for r in c.execute(
            "SELECT type, name, COALESCE(sql,'') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")),
        "digest": {}, "kept_digest": {}, "ids": {},
    }
    for t in tables:
        out["digest"][t] = table_digest(c, t)
        out["kept_digest"][t] = table_digest(c, t, exclude=SCRUBBED_COLUMNS.get(t, ()))
    for t in ("audit", "episodes"):
        if t in tables:
            out["ids"][t] = hashlib.sha256(
                ",".join(str(r[0]) for r in c.execute(f'SELECT id FROM "{t}" ORDER BY id')).encode()).hexdigest()
    out["fk_violations"] = len(c.execute("PRAGMA foreign_key_check").fetchall())
    out["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
    return out


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --- the policy: what each row becomes ---------------------------------------------------------------

@dataclass(slots=True)
class Change:
    table: str
    rowid: int
    values: dict[str, Any]  # column -> new value
    labels: list[tuple[str, str, str]] = field(default_factory=list)  # (table, field, action)
    needles: list[str] = field(default_factory=list)  # OLD sensitive strings, in memory only, to prove they are gone


_NAME_OK = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){0,3}$")
_KEY_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")


def _group_skill(skill: str) -> str:
    """A skill / tool name as it may appear in a report or manifest. Names come from stored rows, so anything
    that is not a plain dotted identifier is shown as '(other)' instead of being echoed."""
    if skill.startswith("test.") and _NAME_OK.match(skill):
        return "test.*"
    return skill if len(skill) <= 40 and _NAME_OK.match(skill) else "(other)"


def _key(name: Any) -> str:
    n = str(name)
    return n if _KEY_OK.match(n) else "(other)"


def _same(a: Any, b: Any) -> bool:
    """Equality that treats NaN == NaN (a JSON `NaN` would otherwise look 'changed' forever)."""
    return json.dumps(a, default=str, sort_keys=True) == json.dumps(b, default=str, sort_keys=True)


def _arg_action(skill: str, key: str, old: Any, new: Any) -> str:
    if auditview._CREDENTIAL_KEY.search(key):
        return "redact credential"
    mode = auditview.POLICY.get(skill, {}).get(key)
    return {
        auditview.OMIT: "omit content", auditview.URL: "strip url query/fragment", auditview.INTENT: "scrub+bound text",
        auditview.ARGV0: "keep executable only", auditview.KEEP: "scrub+bound text", auditview.PATH: "bound path",
        None: "omit unclassified",
    }[mode]


def _text_action(skill: str, old: Any, new: Any, echoed: bool) -> str:
    if not isinstance(old, str) or not isinstance(new, str):
        return "omit non-text value"
    if echoed:
        return "redact echoed content"
    if skill in auditview.CONTENT_RESULT_SKILLS and new.startswith("[read-back content not stored"):
        return "drop read-back content"
    if "[query/credentials not stored]" in new and "[query/credentials not stored]" not in old:
        return "strip url query/fragment"
    if "...[+" in new and len(new) < len(old):
        return "truncate"
    return "scrub text"


def _url_secrets(skill: str, obj: Any) -> list[str]:
    """Query / fragment / userinfo text of the URL-mode arguments: removed from the row, so must not remain on disk."""
    out: list[str] = []
    if not isinstance(obj, dict):
        return out
    from urllib.parse import urlsplit

    for key, mode in auditview.POLICY.get(skill, {}).items():
        v = obj.get(key)
        if mode == auditview.URL and isinstance(v, str):
            try:
                parts = urlsplit(v.strip())
            except ValueError:
                continue
            for piece in (parts.query, parts.fragment, parts.password or ""):
                if len(piece) >= 6:
                    out.append(piece)
    return out


def _args_change(skill: str, raw: str | None) -> tuple[str | None, list[str], list[str]]:
    """(new args json or None if unchanged, echo values, action labels like 'skill.key|action')."""
    try:
        obj = json.loads(raw) if raw is not None else None
    except (TypeError, ValueError):
        obj = None
        if raw is not None:
            return (json.dumps({"_unparseable": f"[omitted: {len(raw)} chars]"}), [],
                    [f"{_group_skill(skill)}|(unparseable)|omit content"])
    if not isinstance(obj, dict):
        new_obj = auditview.sanitize_args(skill, obj)
        new = store_dumps(new_obj)
        same = _same(new_obj, obj)
        return (None if same else new), [], ([] if same else [f"{_group_skill(skill)}|(non-object)|omit content"])
    echoes = auditview.content_values(skill, obj)  # URL query/fragment are NOT echoes: strip_query_urls handles them (needles only)
    new_obj = auditview.sanitize_args(skill, obj)
    if _same(new_obj, obj):
        return None, echoes, []
    labels = [f"{_group_skill(skill)}|{_key(k)}|{_arg_action(skill, k, obj.get(k), new_obj.get(k))}"
              for k in obj if not _same(new_obj.get(k), obj.get(k))]
    return store_dumps(new_obj), echoes, labels


def store_dumps(value: Any) -> str:  # the exact encoding friday.store.dumps writes
    return json.dumps(value, default=str, ensure_ascii=False)


def _sample(old: Any, new: Any) -> list[str]:
    """A fingerprint of text that was dropped (not merely truncated): lets the purge check look for it on disk."""
    if isinstance(old, str) and len(old) >= 12 and (not isinstance(new, str) or old[:24] not in new):
        return [old[:24]]
    return []


def changes(c: sqlite3.Connection) -> Iterator[Change]:
    """Every row whose stored form differs from what the live code would write today. Pure read."""
    c.row_factory = sqlite3.Row
    tables = set(_tables(c))
    if "audit" in tables:
        for r in c.execute("SELECT id, skill, args, result, error FROM audit ORDER BY id"):
            skill = r["skill"] or ""
            values: dict[str, Any] = {}
            labels: list[tuple[str, str, str]] = []
            new_args, echoes, arg_labels = _args_change(skill, r["args"])
            try:
                url_secrets = _url_secrets(skill, json.loads(r["args"]))
            except (TypeError, ValueError):
                url_secrets = []
            needles = (list(echoes) + url_secrets) if new_args is not None or echoes else []
            if new_args is not None:
                values["args"] = new_args
                labels += [("audit", "args", lab) for lab in arg_labels]
            for col, fn in (("result", auditview.sanitize_result), ("error", auditview.sanitize_error)):
                old = r[col]
                if old is None:
                    continue
                new = fn(skill, old, echoes) if col == "result" else fn(old, echoes)
                if not _same(new, old):
                    values[col] = new
                    needles += _sample(old, new)
                    hit = isinstance(old, str) and auditview.redact_echoes(old, echoes) != old
                    labels.append(("audit", col, f"{_group_skill(skill)}|{col}|{_text_action(skill, old, new, hit)}"))
            if values:
                yield Change("audit", int(r["id"]), values, labels, needles)
    if "episodes" in tables:
        for r in c.execute("SELECT id, plan, errors, context FROM episodes ORDER BY id"):
            values = {}
            labels = []
            all_echoes: list[str] = []
            needles = []
            plan_raw = r["plan"]
            try:
                steps = json.loads(plan_raw or "[]")
            except (TypeError, ValueError):
                steps = None
            if steps is None:
                values["plan"] = json.dumps([{"tool": "(unparseable)", "args": {"_unparseable": f"[omitted: {len(plan_raw)} chars]"},
                                              "ok": None, "error": None}])
                labels.append(("episodes", "plan", "(unparseable)|plan|omit content"))
            elif isinstance(steps, list):
                new_steps, touched = [], False
                for s in steps:
                    if not isinstance(s, dict):
                        new_steps.append(s)
                        continue
                    ns = dict(s)
                    tool = str(s.get("tool", ""))
                    echoes = auditview.content_values(tool, s.get("args")) if isinstance(s.get("args"), dict) else []
                    all_echoes += echoes
                    needles += echoes + _url_secrets(tool, s.get("args"))
                    if "args" in s and s["args"] is not None:
                        na = auditview.sanitize_args(tool, s["args"])
                        if not _same(na, s["args"]):
                            ns["args"] = na
                            touched = True
                            if isinstance(s["args"], dict):
                                labels += [("episodes", "plan.args", f"{_group_skill(tool)}|{_key(k)}|{_arg_action(tool, k, s['args'].get(k), na.get(k))}")
                                           for k in s["args"] if not _same(na.get(k), s["args"].get(k))]
                    if isinstance(s.get("error"), str) and s["error"]:
                        ne = auditview.sanitize_error(s["error"], echoes)
                        if ne != s["error"]:
                            needles += _sample(s["error"], ne)
                            ns["error"] = ne
                            touched = True
                            hit = auditview.redact_echoes(s["error"], echoes) != s["error"]
                            labels.append(("episodes", "plan.error", f"{_group_skill(tool)}|error|{_text_action(tool, s['error'], ne, hit)}"))
                    new_steps.append(ns)
                if touched:
                    values["plan"] = store_dumps(new_steps)
            for col in ("errors", "context"):
                old = r[col]
                if not old:
                    continue
                new = auditview.scrub_free_text(old, all_echoes)
                if not _same(new, old):
                    values[col] = new
                    needles += _sample(old, new)
                    hit = isinstance(old, str) and auditview.redact_echoes(old, all_echoes) != old
                    labels.append(("episodes", col, f"*|{col}|{_text_action('', old, new, hit)}"))
            if values:
                yield Change("episodes", int(r["id"]), values, labels, needles)


# --- preview ------------------------------------------------------------------------------------------

@dataclass(slots=True)
class Preview:
    target: Target
    exists: bool = True
    bytes: int = 0
    sidecars: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    rows_to_change: Counter = field(default_factory=Counter)  # table -> rows
    actions: Counter = field(default_factory=Counter)  # (table, field, label) -> occurrences
    unchanged: dict[str, int] = field(default_factory=dict)  # table -> rows retained unchanged
    retained_by_policy: dict[str, int] = field(default_factory=dict)
    undo_flagged: int = 0
    code: str = ""
    error: str = ""

    @property
    def total(self) -> int:
        return sum(self.rows_to_change.values())


def _undo_flagged(c: sqlite3.Connection) -> int:
    """Undo-journal rows whose inverse carries a long text value of a kind the audit policy omits. Read only."""
    if "undo" not in _tables(c):
        return 0
    n = 0
    for (inv,) in c.execute("SELECT inverse FROM undo"):
        try:
            d = json.loads(inv)
        except (TypeError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("args"), dict) and auditview.content_values(str(d.get("skill", "")), d["args"]):
            n += 1
    return n


def _code(path: Path, c: sqlite3.Connection, rows: list[Change]) -> str:
    """Binds a confirmation to this target AND this exact state: change it and the code changes."""
    h = hashlib.sha256()
    h.update(f"v{SCRUB_VERSION}|{os.path.normcase(os.fspath(path))}|".encode())
    for ch in rows:
        h.update(f"{ch.table}:{ch.rowid}:".encode())
        h.update(_row_hash(tuple(sorted(ch.values.items()))))
    return h.hexdigest()[:10]


def preview(target: Target, *, conn: sqlite3.Connection | None = None) -> Preview:
    """Read-only. `conn` lets apply() reuse its exclusive connection."""
    p = Preview(target=target)
    if not target.path.is_file():
        p.exists, p.error = False, "no such file"
        return p
    dbplace.guard(target.path)
    p.bytes = target.path.stat().st_size
    p.sidecars = [s for s in ("-wal", "-shm") if Path(str(target.path) + s).exists()]
    own = conn is None
    c = conn if conn is not None else _open_ro(target.path)
    try:
        c.row_factory = sqlite3.Row
        tables = _tables(c)
        p.counts = {t: int(c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables}
        rows = list(changes(c))
        for ch in rows:
            p.rows_to_change[ch.table] += 1
            for lab in ch.labels:
                tbl, col, text = lab
                skill, key, action = text.split("|", 2)
                p.actions[(tbl, f"{col}: {skill}.{key}" if col in ("args", "plan.args") else f"{col}: {skill}", action)] += 1
        for t in SCRUBBED_COLUMNS:
            if t in p.counts:
                p.unchanged[t] = p.counts[t] - p.rows_to_change[t]
        p.retained_by_policy = {t: p.counts[t] for t in RETAINED_BY_POLICY if t in p.counts}
        p.undo_flagged = _undo_flagged(c)
        p.code = _code(target.path, c, rows)
        return p
    except sqlite3.Error as exc:
        p.error = f"{type(exc).__name__} (details withheld: a stored value may be quoted in the message)"
        return p
    finally:
        if own:
            c.close()


def format_preview(p: Preview) -> list[str]:
    """Human text. Contains paths, table/field/skill names and counts -- never a stored value."""
    t = p.target
    lines = [f"[{t.name}] {t.path}"]
    if not p.exists or p.error:
        lines.append(f"    unavailable: {p.error or 'missing'}")
        return lines
    lines.append(f"    size={p.bytes} bytes  sidecars={p.sidecars or 'none'}  synced-folder={t.synced}  alias={t.alias}")
    if t.synced:
        lines.append("    NOTE: inside a cloud-synced folder -- scrubbing the file does not purge the provider's version history, recycle bin or other devices.")
    for tbl in SCRUBBED_COLUMNS:
        if tbl in p.counts:
            lines.append(f"    {tbl}: {p.counts[tbl]} rows, {p.rows_to_change[tbl]} would change, {p.unchanged.get(tbl, 0)} unchanged")
    for (tbl, fld, action), n in sorted(p.actions.items()):
        lines.append(f"      {tbl:9} {fld:55} -> {action:28} x{n}")
    lines.append("    retained unchanged by policy (separate stores): " + ", ".join(f"{k}={v}" for k, v in p.retained_by_policy.items() if v))
    if p.undo_flagged:
        lines.append(f"    undo journal rows carrying content-like arguments (retained, review separately): {p.undo_flagged}")
    lines.append(f"    confirmation code (this target, this exact state): {p.code}")
    return lines


# --- backup -------------------------------------------------------------------------------------------

@dataclass(slots=True)
class Backup:
    directory: Path
    file: Path
    sha256: str
    bytes: int
    restore_verified: bool
    snapshot: dict[str, Any]


def default_backup_root() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local) / "FRIDAY-backups" if local else Path.home() / "FRIDAY-backups"


def check_backup_root(root: Path, target: Target) -> Path:
    """The backup must be outside the repository, outside cloud sync and outside the database's own folder."""
    root = Path(os.path.realpath(os.fspath(root)))
    if dbplace._under(root, paths.ROOT):
        raise ScrubError(f"backup folder {root} is inside the repository; choose another with --backup-dir")
    if dbplace.in_synced_folder(root):
        raise ScrubError(f"backup folder {root} is inside a cloud-synced folder; choose another with --backup-dir")
    for guarded in {target.path.parent, *(p.parent for p in dbplace.real_db_paths())}:
        if dbplace._under(root, guarded):
            raise ScrubError(f"backup folder {root} is inside a database folder ({guarded}); choose another with --backup-dir")
    return root


def verify_backup_file(path: Path, expected: dict[str, Any] | None = None, *, rehearse: bool = True) -> dict[str, Any]:
    """Prove a backup file: integrity, schema, counts, digest -- and (rehearse) that it restores. Read only.
    Returns its snapshot; raises ScrubError on any mismatch with `expected`."""
    dbplace.guard(path)
    c = dbplace._open_ro(path)
    try:
        snap = snapshot(c)
        if snap["integrity"] != "ok":
            raise ScrubError(f"backup {path} fails SQLite's integrity check")
        if expected is not None:
            for key in ("counts", "schema", "digest"):
                if snap[key] != expected[key]:
                    raise ScrubError(f"backup {path} does not match the database it was taken from ({key})")
        if rehearse:
            tmp_dir = Path(tempfile.mkdtemp(prefix="friday-restore-rehearsal-"))
            try:
                disposable = tmp_dir / "rehearsal.db"
                d = sqlite3.connect(str(disposable))
                try:
                    c.backup(d)
                finally:
                    d.close()
                rc = dbplace._open_ro(disposable)
                try:
                    again = snapshot(rc)
                finally:
                    rc.close()
                if again["integrity"] != "ok" or again["digest"] != snap["digest"] or again["counts"] != snap["counts"]:
                    raise ScrubError(f"restoring {path} onto a disposable copy did not reproduce it")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                if tmp_dir.exists():
                    log.warning("could not remove the restore-rehearsal copy %s; delete it yourself", tmp_dir)
        return snap
    except sqlite3.Error as exc:
        raise ScrubError(f"backup {path} cannot be read ({type(exc).__name__}; details withheld)") from None
    finally:
        c.close()


def _make_backup(src: sqlite3.Connection, target: Target, backup_root: Path, before: dict[str, Any]) -> Backup:
    root = check_backup_root(backup_root, target)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / f"scrub-{stamp}-{target.name}-{secrets.token_hex(3)}"
    directory.mkdir(parents=False, exist_ok=False)  # never an existing backup folder
    need = target.path.stat().st_size * 3 + (16 << 20)
    free = shutil.disk_usage(directory).free
    if free < need:
        raise ScrubError(f"not enough free space for a backup in {directory} ({free} < {need} bytes); nothing was changed")
    partial = directory / "friday.db.partial"
    final = directory / "friday.db.bak"
    dst = sqlite3.connect(str(partial))
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()
    finally:
        dst.close()
    _fault("after-backup-copy")
    verify_backup_file(partial, before, rehearse=False)
    if not dbplace._publish_no_overwrite(partial, final):
        raise ScrubError(f"{final} already exists; refusing to overwrite a backup")
    os.chmod(final, stat.S_IREAD)  # read-only on every platform; FRIDAY never deletes it
    verify_backup_file(final, before, rehearse=True)  # includes the restore rehearsal on a disposable copy
    return Backup(directory, final, file_sha256(final), final.stat().st_size, True, before)


def _write_manifest(directory: Path, data: dict[str, Any]) -> None:
    tmp = directory / "manifest.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, directory / "manifest.json")


# --- proving the old content is gone from the file ---------------------------------------------------

def _find_remnants(path: Path, needles: list[str]) -> int:
    """How many distinct old values still appear in the database file or its -wal. A value that is legitimately
    still stored somewhere (a goal that quotes it, a memory) is not a remnant. Returns a COUNT, never a value."""
    keys: dict[bytes, str] = {}
    for n in needles:
        if isinstance(n, str) and len(n) >= 6:
            b = n.encode("utf-8", "ignore")[:24]
            keys[b] = b.decode("utf-8", "ignore")
    if not keys:
        return 0
    blobs = []
    for suffix in ("", "-wal"):
        f = Path(str(path) + suffix)
        if f.is_file():
            blobs.append(f.read_bytes())
    present = [k for k in keys if any(k in b for b in blobs)]
    if not present:
        return 0
    c = dbplace._open_ro(path)
    try:
        cols = [(t, col) for t in _tables(c) for col in _columns(c, t)]
        remnants = 0
        for k in present:
            text = keys[k]
            live = False
            for t, col in cols:
                try:
                    q = f'SELECT 1 FROM "{t}" WHERE typeof("{col}")=\'text\' AND instr("{col}", ?) > 0 LIMIT 1'
                    if c.execute(q, (text,)).fetchone():
                        live = True
                        break
                except sqlite3.Error:
                    continue
            if not live:
                remnants += 1
        return remnants
    finally:
        c.close()


# --- apply --------------------------------------------------------------------------------------------

@dataclass(slots=True)
class Result:
    target: Target
    backup: Backup
    rows_changed: Counter
    before: dict[str, Any]
    after: dict[str, Any]
    vacuumed: bool
    purged: bool = True
    notes: list[str] = field(default_factory=list)


def _lock_exclusive(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), timeout=1.5, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA locking_mode=EXCLUSIVE")
        c.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()  # takes (and keeps) the exclusive lock
    except sqlite3.Error as exc:
        c.close()
        raise ScrubError(
            f"{path} is in use by another process or connection ({exc}). Close FRIDAY and anything else using it, "
            "then retry. Nothing was changed. Leftover -wal/-shm files are never deleted by this command."
        ) from exc
    return c


def apply(
    target: Target,
    *,
    confirm: str,
    backup_root: Path | None = None,
    allow_synced: bool = False,
    vacuum: bool = True,
) -> Result:
    """Scrub ONE database. See the module docstring for the guarantees."""
    if target.name not in TARGETS:
        raise ScrubError("explicit target required")
    _require_file(target)
    if target.alias:
        raise ScrubError(f"{target.path} is reached through an alias (symlink/junction); give the real path via the config and retry")
    if target.synced and not allow_synced:
        raise ScrubError(
            f"{target.path} is inside a cloud-synced folder. Scrubbing it does not purge the provider's version history "
            "or other devices; if you still want it, repeat with --allow-synced. (Deleting the old file yourself is usually better.)")
    if not confirm or not isinstance(confirm, str):
        raise ScrubError("confirmation code required: run the preview and pass its code with --confirm")
    c = _lock_exclusive(target.path)  # refuses when anything else has the database open
    committed = False
    backup: Backup | None = None
    manifest: dict[str, Any] = {}
    try:
        pv = preview(target, conn=c)
        if pv.error:
            raise ScrubError(f"cannot inspect {target.path}: {pv.error}")
        if confirm.strip() != pv.code:
            raise ScrubError("the confirmation code does not match this target in its current state; run the preview again. Nothing was changed.")
        rows = list(changes(c))
        before = snapshot(c)
        if before["integrity"] != "ok":
            raise ScrubError(f"{target.path} fails SQLite's integrity check; refusing to modify it. Nothing was changed.")
        if not rows:
            raise ScrubError("nothing to scrub: every audit and episode row is already in its minimal form")
        backup = _make_backup(c, target, backup_root or default_backup_root(), before)
        manifest = {
            "version": SCRUB_VERSION, "target": target.name, "database": str(target.path),
            "started": datetime.now(timezone.utc).isoformat(), "status": "backup-verified",
            "backup_file": str(backup.file), "backup_sha256": backup.sha256, "backup_bytes": backup.bytes,
            "restore_rehearsal": "passed", "counts_before": before["counts"], "rows_to_change": dict(pv.rows_to_change),
            "actions": {f"{a}|{b}|{c_}": n for (a, b, c_), n in pv.actions.items()},
            "note": "contains counts and hashes only; the backup file itself keeps the sensitive history",
        }
        _write_manifest(backup.directory, manifest)
        _fault("after-backup")

        c.execute("PRAGMA secure_delete=ON")  # overwrite replaced content instead of leaving it in free space
        c.execute("BEGIN IMMEDIATE")
        try:
            for i, ch in enumerate(rows):
                sets = ", ".join(f'"{col}"=?' for col in ch.values)
                c.execute(f'UPDATE "{ch.table}" SET {sets} WHERE id=?', (*ch.values.values(), ch.rowid))
                if i == len(rows) // 2:
                    _fault("mid-scrub")
            # verify BEFORE commit: nothing but the intended columns moved, and a second pass would be a no-op
            now = snapshot(c)
            problems = _compare(before, now)
            if _any_change(c):
                problems.append("a second pass would still change rows (not a fixed point)")
            if problems:
                raise ScrubError("pre-commit verification failed: " + "; ".join(problems))
            _fault("before-commit")
            c.execute("COMMIT")
            committed = True
        except BaseException:
            if not committed:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                _mark_aborted(manifest, backup)
            raise
        manifest.update(status="committed-unverified", rows_changed=dict(pv.rows_to_change))
        _write_manifest(backup.directory, manifest)
        _fault("after-commit")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        vacuumed = False
        vacuum_error = ""
        if vacuum:
            try:
                c.execute("VACUUM")  # rewrites the file so the old content is not left in free pages
                vacuumed = True
            except sqlite3.Error as exc:
                vacuum_error = type(exc).__name__
                log.warning("VACUUM after scrub failed (%s); replaced content may remain in free pages", vacuum_error)
        c.close()
        c = None  # type: ignore[assignment]

        r = _open_ro(target.path)  # a fresh connection: what a restart would see
        try:
            after = snapshot(r)
            problems = _compare(before, after)
            if _any_change(r):
                problems.append("re-opened database still has unscrubbed rows")
        finally:
            r.close()
        if problems:
            manifest["status"] = "committed-verification-FAILED"
            manifest["problems"] = problems
            _write_manifest(backup.directory, manifest)
            raise ScrubError(
                "the scrub was committed but post-verification failed: " + "; ".join(problems) +
                f". The verified backup is at {backup.file}; restore it with `python run.py db restore`.")
        remnants = _find_remnants(target.path, [n for ch in rows for n in ch.needles])
        purged = vacuumed and remnants == 0
        if vacuum and not purged:
            manifest.update(status="committed-NOT-PURGED", remnants=remnants, vacuum_error=vacuum_error)
            _write_manifest(backup.directory, manifest)
            raise ScrubError(
                f"the scrub was committed and verified, but {remnants or 'some'} replaced value(s) may still be readable in "
                f"the database file ({'VACUUM failed: ' + vacuum_error if vacuum_error else 'found after VACUUM'}). "
                "Free disk space, close FRIDAY, and run `python run.py db compact --target " + target.name + "`. "
                f"The verified backup is at {backup.file}.")
        manifest.update(status="done" if purged else "done-not-compacted", finished=datetime.now(timezone.utc).isoformat(),
                        counts_after=after["counts"], vacuumed=vacuumed, purged=purged, integrity_after=after["integrity"])
        _write_manifest(backup.directory, manifest)
        return Result(target, backup, Counter(pv.rows_to_change), before, after, vacuumed, purged)
    except ScrubError:
        if not committed:
            _mark_aborted(manifest, backup)
        raise
    except Exception as exc:  # noqa: BLE001
        if not committed:
            _mark_aborted(manifest, backup)
        where = "the database was NOT changed" if not committed else "the scrub was committed"
        hint = f" The verified backup is at {backup.file}." if backup is not None else ""
        raise ScrubError(f"scrub failed ({type(exc).__name__}); {where}.{hint}") from exc
    finally:
        if c is not None:
            try:
                c.close()
            except sqlite3.Error:
                pass


def _mark_aborted(manifest: dict[str, Any], backup: "Backup | None") -> None:
    if backup is not None and manifest and manifest.get("status") != "aborted-database-unchanged":
        manifest["status"] = "aborted-database-unchanged"
        try:
            _write_manifest(backup.directory, manifest)
        except OSError:
            pass


def compact(target: Target, *, allow_synced: bool = False) -> dict[str, Any]:
    """Checkpoint + VACUUM so replaced content leaves the file (also after an interrupted scrub). Changes no
    logical content: row counts and every table digest are verified equal. Takes the same exclusive lock."""
    _require_file(target)
    if target.alias:
        raise ScrubError(f"{target.path} is reached through an alias; give the real path and retry")
    if target.synced and not allow_synced:
        raise ScrubError(f"{target.path} is inside a cloud-synced folder; repeat with --allow-synced if you really want this")
    c = _lock_exclusive(target.path)
    try:
        before = snapshot(c)
        if before["integrity"] != "ok":
            raise ScrubError(f"{target.path} fails SQLite's integrity check; refusing to modify it")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
    except sqlite3.Error as exc:
        raise ScrubError(f"compact failed ({type(exc).__name__}); the database is unchanged") from None
    finally:
        c.close()
    r = _open_ro(target.path)
    try:
        after = snapshot(r)
    finally:
        r.close()
    problems = _compare(before, after)
    if problems or before["digest"] != after["digest"]:
        raise ScrubError("compact verification failed: " + "; ".join(problems or ["content changed"]))
    return {"path": str(target.path), "counts": after["counts"], "integrity": after["integrity"]}


def _any_change(c: sqlite3.Connection) -> bool:
    gen = changes(c)
    try:
        return next(gen, None) is not None
    finally:
        gen.close()


def _compare(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """What must be true after a scrub. Returns the problems (empty = fine)."""
    out: list[str] = []
    if after["integrity"] != "ok":
        out.append("integrity check failed")
    if after["counts"] != before["counts"]:
        out.append("row counts changed")
    if after["schema"] != before["schema"]:
        out.append("schema changed")
    if after["ids"] != before["ids"]:
        out.append("ids changed")
    if after["fk_violations"] > before["fk_violations"]:
        out.append("new foreign-key violations")
    for t, d in before["kept_digest"].items():
        if after["kept_digest"].get(t) != d:
            out.append(f"{t}: a column that must not change did change")
    for t in before["digest"]:
        if t not in SCRUBBED_COLUMNS and after["digest"].get(t) != before["digest"][t]:
            out.append(f"{t}: content changed")
    return out


# --- restore ------------------------------------------------------------------------------------------

def restore(backup: Path, dest: Path) -> dict[str, Any]:
    """Restore a backup to a NEW file. Never overwrites: `dest` must not exist. The caller (the person) moves the
    restored file into place after stopping FRIDAY and setting the scrubbed one aside."""
    backup, dest = Path(backup), Path(dest)
    dbplace.guard(dest)
    if dest.exists():
        raise ScrubError(f"{dest} already exists; restore never overwrites. Move or rename it first.")
    leftovers = [str(dest) + sfx for sfx in ("-wal", "-shm") if Path(str(dest) + sfx).exists()]
    if leftovers:
        raise ScrubError("stale SQLite sidecar files exist next to the destination and would be applied to the restored "
                         "database: " + ", ".join(leftovers) + ". Set them aside (rename, do not delete) together with the old .db first.")
    if dbplace.in_synced_folder(dest):
        raise ScrubError(f"{dest} is inside a cloud-synced folder; restore into a local folder instead")
    snap = verify_backup_file(backup, rehearse=False)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".restoring-{secrets.token_hex(3)}")
    src = dbplace._open_ro(backup)
    try:
        d = sqlite3.connect(str(tmp))
        try:
            src.backup(d)
        finally:
            d.close()
    finally:
        src.close()
    try:
        r = dbplace._open_ro(tmp)
        try:
            again = snapshot(r)
        finally:
            r.close()
        if again["integrity"] != "ok" or again["digest"] != snap["digest"] or again["counts"] != snap["counts"]:
            raise ScrubError("the restored copy does not match the backup; nothing was published")
        if not dbplace._publish_no_overwrite(tmp, dest):
            raise ScrubError(f"{dest} appeared while restoring; nothing was overwritten")
        tmp = None  # type: ignore[assignment]
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
    return {"restored": str(dest), "counts": snap["counts"]}
