"""Recovery store for filesystem mutations (Phase 32).

Before FRIDAY overwrites or deletes a user's file, the original is *preserved* here, and the
undo journal records a `files.restore` that puts it back. Nothing in this module bypasses the
Executor: it only supplies the copy/verify/restore primitives the `files.*` skills call while
they run under it, so every mutation still goes through permission, confirmation, audit and
undo like any other skill.

Layout (one directory per snapshot, under `root()` -- by default %LOCALAPPDATA%/FRIDAY/snapshots,
outside the repository and outside OneDrive; `files.snapshot_dir` / FRIDAY_SNAPSHOTS override it):

    <token>/meta.json   what it is, where it came from, size and sha256
    <token>/blob        the exact bytes of the original file (absent for a folder)

Design rules:
  * A token is generated, never user text, and is validated against a strict pattern before it
    is ever joined onto a path (no traversal).
  * A snapshot directory is created with `exist_ok=False`: an existing snapshot is never reused
    or overwritten. Nothing in the files.* skills ever deletes one; the only deletion path is the
    explicit, confirmed, journaled cleanup in `friday.retention`, which skips a snapshot a pending
    undo entry refers to (re-checked just before each deletion, not atomically with it).
  * Phase 32 kept snapshots in <repo>/data/snapshots. That folder (`legacy_root()`) is still READ,
    so undo entries made before the move keep working, but it is never written to or cleaned.
  * `meta.json` is written BEFORE the blob is moved into place, and a restore trusts a snapshot
    only when the blob's size AND sha256 match `meta.json`. A crash mid-way leaves either no
    usable snapshot or a verified one, never a half-written one that looks good.
  * Every failure raises `RecoveryError` and the caller refuses the mutation (fail closed): if a
    recoverable copy cannot be made, the original is left untouched. The one exception is
    `RecoveryStranded` (a delete whose file was taken out of place and could neither be verified
    nor put back): the file sits in the store, marked unverified, and a person has to resolve it.
  * Restores write to a temporary file in the target's folder, verify it, then rename it into
    place, so an interrupted process leaves the old content or the new content, not a partial
    one (the temporary file is not fsynced, so a power cut is not covered).

Known limitation: Windows has no atomic compare-and-replace. Before replacing a file that exists,
the callers re-check that it still matches its snapshot (`still_matches`), but the re-check and the
replace are two steps. The window is bounded, not closed: a process that changes the file between
them has its newer bytes replaced with no snapshot of them, and a writer that holds the file open
with share-delete can keep writing. A NEW destination is different: `publish_new` uses a
no-overwrite primitive, so a concurrent creator is not clobbered. A stranded blob is not repaired
automatically.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from friday import dbplace, paths
from friday.config import CFG
from friday.log import get

log = get(__name__)

_HEX = set("0123456789abcdef")
_CHUNK = 1024 * 1024


class RecoveryError(Exception):
    """A recoverable copy could not be made or verified. The caller must not mutate."""


class RecoveryStranded(RecoveryError):
    """The file was taken out of its place but the copy could not be verified AND it could not be put
    back. The file as it was moved is not deleted: it is in the store under `token` (marked unverified
    in its meta.json, so its bytes may differ from what was hashed). The caller must not report the
    file as recoverable, and must tell the user where it is; a person has to resolve it."""

    def __init__(self, message: str, *, token: str) -> None:
        super().__init__(message)
        self.token = token


# -- paths and tokens --------------------------------------------------------------------------


def root() -> Path:
    """The directory new snapshots are written to. Read at call time so a test can point
    `paths.SNAPSHOTS` at a temp folder. `files.snapshot_dir` wins when it is set. A location that
    would be unsafe to treat as a private store (a drive root or the home folder itself) raises
    RecoveryError, so the caller refuses to mutate rather than scattering copies there."""
    configured = str(getattr(CFG.files, "snapshot_dir", "") or "").strip()
    base = Path(os.path.expandvars(configured)).expanduser() if configured else Path(paths.SNAPSHOTS)
    if configured and not base.is_absolute():
        base = paths.ROOT / base
    try:
        resolved = base.resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError) as exc:
        raise RecoveryError(f"the recovery folder can't be resolved: {exc}") from exc
    if resolved == resolved.parent or resolved == home:
        raise RecoveryError(f"{base} is not a safe place for recovery copies (a drive root or the home folder)")
    dbplace.guard_store(base)  # Phase 35: a test/benchmark entry point may not use the user's real store
    return base


def legacy_root() -> Path | None:
    """The Phase 32 in-repository store, when it differs from the current one. Read-only."""
    legacy = Path(paths.LEGACY_SNAPSHOTS)
    try:
        if legacy.resolve() == root().resolve():
            return None
    except (OSError, RuntimeError, RecoveryError):
        pass
    return legacy


def locate(token: str) -> Path:
    """The folder holding snapshot `token`: the current store first, then the legacy one. A token
    found in neither resolves to the current store (where `info` will report it missing)."""
    if not is_token(token):
        raise RecoveryError("that isn't a valid recovery id")
    current = root() / token
    if (current / "meta.json").is_file():
        return current
    legacy = legacy_root()
    if legacy is not None and (legacy / token / "meta.json").is_file():
        return legacy / token
    return current


def synced_by_onedrive(path: Path | None = None) -> bool:
    """True when `path` (default: the store) sits inside a OneDrive folder -- worth telling the user,
    since recovery copies are unencrypted copies of their files."""
    try:
        target = (path or root()).resolve()
    except (OSError, RuntimeError, RecoveryError):
        return False
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        try:
            base = Path(raw).resolve()
        except (OSError, RuntimeError):
            continue
        if target == base or base in target.parents:
            return True
    return False


def is_token(value: object) -> bool:
    """`YYYYMMDDTHHMMSS-` + 12 hex digits -- the only shape `new_token` produces."""
    if not isinstance(value, str) or len(value) != 28 or value[8] != "T" or value[15] != "-":
        return False
    date, clock, rand = value[:8], value[9:15], value[16:]
    return date.isdigit() and clock.isdigit() and len(rand) == 12 and set(rand) <= _HEX


def new_token() -> str:
    return time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(6)


def _dir_of(token: str) -> Path:
    return locate(token)


def in_store(path: str | os.PathLike[str]) -> bool:
    """True when `path` is the recovery store or anything inside it. The files.* skills refuse to
    touch such a path so a planner can never delete or rewrite a snapshot through them."""
    try:
        target = Path(path).expanduser().resolve()
        bases = [root().resolve()]
        legacy = legacy_root()
        if legacy is not None:
            bases.append(legacy.resolve())
    except dbplace.IsolationError:
        raise  # a test using the real store must fail loudly, not look like a "protected" path
    except (OSError, RuntimeError, RecoveryError):
        return True  # cannot tell -> treat as protected
    return any(target == base or base in target.parents for base in bases)


def is_link(path: Path) -> bool:
    """Symlinks and junctions: replacing or moving the link itself is not what a caller means by
    'the file', so the mutation skills refuse them rather than guess."""
    isjunction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(isjunction and isjunction(path))


# -- hashing / verification ---------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def same_content(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and sha256_of(a) == sha256_of(b)
    except OSError:
        return False


def _limit_bytes() -> int:
    return int(CFG.files.max_snapshot_mb) * 1024 * 1024


def still_matches(token: str, path: Path) -> bool:
    """True when the file at `path` still has exactly the size and sha256 snapshot `token` recorded
    (used right before replacing it: if it changed since it was kept, the replace must not happen).
    It narrows the window before the replace; it cannot close it (see the module docstring)."""
    try:
        meta = read_meta(token)
        if meta.get("kind") != "file":
            return False
        return path.stat().st_size == meta["size"] and sha256_of(path) == meta["sha256"]
    except (OSError, RecoveryError):
        return False


def publish_new(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Move `src` to `dst`, refusing if anything is at `dst`: raises FileExistsError if something is
    (including a file that appeared a moment ago) and OSError(EXDEV / winerror 17) across volumes.
    On Windows this is one `os.rename`, which refuses to overwrite (atomic). Elsewhere a hard link
    is the no-replace primitive, followed by an unlink of `src` -- two steps, so not atomic."""
    if os.name == "nt":
        os.rename(src, dst)
        return
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise
        if os.path.lexists(dst):  # a filesystem without hard links: best effort
            raise FileExistsError(errno.EEXIST, "File exists", str(dst)) from exc
        os.rename(src, dst)
        return
    try:
        os.unlink(src)
    except OSError:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise


# -- preserve ----------------------------------------------------------------------------------


def preserve(path: Path, *, reason: str, move: bool = False) -> str:
    """Keep a verified copy of `path` and return its token.

    `move=False` copies (the original stays where it is -- for an overwrite). `move=True` takes the
    file out of its place into the store (for a delete) -- by rename when the store is on the same
    volume, otherwise by copy + verify + unlink. A file bigger than `files.max_snapshot_mb`, a link,
    or anything that is not a regular file or an empty folder is refused: a recoverable copy that
    cannot be made means the caller must not proceed.

    A token is returned ONLY when the stored blob has been re-read and its size and sha256 match
    what the original was hashed as. If the original changed while it was being taken (a writer with
    share-delete can keep writing to a renamed file), the move is rolled back with a no-overwrite
    rename; if it cannot be rolled back the bytes stay in the store, marked unverified, and
    `RecoveryStranded` names the token. No file content is deleted on that path.
    """
    try:
        if is_link(path):
            raise RecoveryError(f"{path.name} is a link; I won't replace or remove a link")
        if path.is_dir():
            return _preserve_dir(path, reason=reason, move=move)
        if not path.is_file():
            raise RecoveryError(f"{path.name} is not a regular file")
        before = path.stat()
        if before.st_size > _limit_bytes():
            raise RecoveryError(
                f"{path.name} is {before.st_size // (1024 * 1024)} MB, over the "
                f"{CFG.files.max_snapshot_mb} MB limit for keeping a recoverable copy"
            )
        digest = sha256_of(path)
        settled = path.stat()
        if (settled.st_size, settled.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise RecoveryError(f"{path.name} changed while I was reading it")
        token, folder = _new_folder()
        blob = folder / "blob"
        _write_meta(folder, {
            "version": 1, "token": token, "kind": "file", "original_path": str(path),
            "size": before.st_size, "sha256": digest, "mtime_ns": before.st_mtime_ns,
            "mode": before.st_mode, "reason": reason, "captured_by": "move" if move else "copy",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        if move:
            _move_into_store(path, folder, token, before.st_size, digest)
        else:
            _copy_into(path, blob, digest, before.st_size)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise RecoveryError(f"{path.name} changed while I was copying it")
        return token
    except RecoveryError:
        raise
    except OSError as exc:
        raise RecoveryError(f"couldn't keep a recoverable copy of {path.name}: {exc}") from exc


def _verify_blob(blob: Path, size: int, digest: str) -> None:
    """Re-read `blob` and require the expected size AND sha256 (and that it did not change while it
    was being read). Raises RecoveryError otherwise."""
    try:
        first = blob.stat()
        actual = sha256_of(blob)
        last = blob.stat()
    except OSError as exc:
        raise RecoveryError(f"the stored copy can't be read back: {exc}") from exc
    if first.st_size != size or last.st_size != size:
        raise RecoveryError(f"the stored copy is {last.st_size} bytes, expected {size}")
    if (first.st_size, first.st_mtime_ns) != (last.st_size, last.st_mtime_ns):
        raise RecoveryError("the stored copy changed while it was being verified")
    if actual != digest:
        raise RecoveryError("the stored copy's content does not match the original")


def _move_into_store(path: Path, folder: Path, token: str, size: int, digest: str) -> None:
    blob = folder / "blob"
    try:
        os.replace(path, blob)  # atomic; `blob` can't pre-exist: the folder was created exclusively just now
    except OSError:
        _move_by_copy(path, blob, size, digest)
        return
    try:
        _verify_blob(blob, size, digest)
        return
    except RecoveryError as exc:
        problem = str(exc)
    # The bytes that landed in the store are not the bytes that were hashed (the file changed during
    # the move, or the store damaged it). Do NOT report success. Put the file back where it was.
    try:
        os.rename(blob, path)  # refuses to overwrite: if something took the old name, we keep the blob
    except OSError as back:
        _mark_unverified(folder, token, problem)
        raise RecoveryStranded(
            f"{path.name} changed while I was moving it ({problem}) and I could not put it back "
            f"({back}); the file is kept in FRIDAY's recovery storage as {token}, marked unverified",
            token=token,
        ) from back
    _abort_scratch(folder)
    raise RecoveryError(f"{path.name} changed while I was taking it out of place ({problem}); I left it where it was")


def _move_by_copy(path: Path, blob: Path, size: int, digest: str) -> None:
    """Cross-volume fallback: verified copy, then remove the original only if it is still exactly
    what was copied."""
    _copy_into(path, blob, digest, size)
    try:
        unchanged = path.stat().st_size == size and sha256_of(path) == digest
    except OSError:
        unchanged = False
    if not unchanged:
        raise RecoveryError(
            f"{path.name} changed while I was copying it, so I left it in place "
            f"(a copy of the earlier version is kept in the recovery storage)"
        )
    try:
        path.unlink()
    except OSError as exc:
        raise RecoveryError(f"kept a copy but couldn't remove the original: {exc}") from exc


def _mark_unverified(folder: Path, token: str, problem: str) -> None:
    """Flag a snapshot whose blob did not verify. `sha256` keeps the EXPECTED value, so `info()` keeps
    refusing it (fail closed); the extra fields tell a human what happened."""
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        meta["integrity"] = "unverified-after-move"
        meta["integrity_problem"] = problem
        try:
            meta["actual_size"] = (folder / "blob").stat().st_size
        except OSError:
            pass
        _write_meta(folder, meta)
    except (OSError, ValueError):
        log.warning("couldn't mark %s unverified", token)


def _abort_scratch(folder: Path) -> None:
    """Remove the metadata-only folder of a preserve that never produced a blob (our own scratch)."""
    try:
        if (folder / "blob").exists() or (folder / "blob.partial").exists():
            return
        for name in ("meta.json", "meta.json.partial"):
            (folder / name).unlink(missing_ok=True)
        folder.rmdir()
    except OSError:
        pass


def _preserve_dir(path: Path, *, reason: str, move: bool) -> str:
    if not move:
        raise RecoveryError(f"{path.name} is a folder")
    if any(path.iterdir()):
        raise RecoveryError(f"{path.name} isn't empty")
    token, folder = _new_folder()
    _write_meta(folder, {
        "version": 1, "token": token, "kind": "dir", "original_path": str(path), "size": 0,
        "sha256": "", "reason": reason, "captured_by": "move",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    path.rmdir()
    return token


def _new_folder() -> tuple[str, Path]:
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        token = new_token()
        folder = base / token
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        return token, folder
    raise RecoveryError("couldn't allocate a recovery id")


def _write_meta(folder: Path, meta: dict[str, Any]) -> None:
    tmp = folder / "meta.json.partial"
    tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.replace(tmp, folder / "meta.json")


def _copy_into(src: Path, blob: Path, digest: str, size: int) -> None:
    partial = blob.with_name("blob.partial")
    shutil.copy2(src, partial)
    try:
        _verify_blob(partial, size, digest)
    except RecoveryError as exc:
        partial.unlink(missing_ok=True)
        raise RecoveryError(f"the copy of {src.name} did not match the original ({exc})") from exc
    os.replace(partial, blob)


# -- inspect / restore --------------------------------------------------------------------------


def read_meta(token: str) -> dict[str, Any]:
    """A snapshot's metadata WITHOUT hashing its content (cheap; for listings and risk checks).
    Raises RecoveryError if it is missing or malformed."""
    return read_meta_at(_dir_of(token), token)


def read_meta_at(folder: Path, token: str) -> dict[str, Any]:
    """`read_meta` for one specific snapshot folder (the current or the legacy store)."""
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecoveryError("that recovery copy isn't available") from exc
    if not isinstance(meta, dict) or meta.get("token") != token or meta.get("kind") not in ("file", "dir"):
        raise RecoveryError("that recovery copy is damaged")
    if meta["kind"] == "file" and not (isinstance(meta.get("size"), int) and isinstance(meta.get("sha256"), str)):
        raise RecoveryError("that recovery copy is damaged")
    return meta


def info(token: str) -> dict[str, Any]:
    """The verified metadata of a snapshot. Raises RecoveryError if it is missing or damaged."""
    return verify_at(_dir_of(token), token)


def verify_at(folder: Path, token: str) -> dict[str, Any]:
    """`info` for one specific snapshot folder: metadata read, and for a file the blob's size AND
    sha256 must match it."""
    meta = read_meta_at(folder, token)
    if meta["kind"] == "file":
        blob = folder / "blob"
        try:
            # a blob that is a link (symlink/junction) would restore content from OUTSIDE the store
            ok = (not is_link(blob) and blob.is_file() and blob.stat().st_size == meta["size"]
                  and sha256_of(blob) == meta["sha256"])
        except OSError:
            ok = False
        if not ok:
            raise RecoveryError("that recovery copy is damaged (its content doesn't match what was saved)")
    return meta


def restore_to(token: str, target: Path, *, replace: bool, expect_token: str | None = None) -> dict[str, Any]:
    """Put snapshot `token` at `target`. `replace=False` fails if anything is there already.
    `expect_token` (with `replace=True`): the snapshot the file currently at `target` was kept as; if
    the target no longer matches it just before the swap, nothing is replaced.

    The content lands via a temporary file in the target's folder and a rename, so an interrupted
    process leaves the old target or the finished one, not a partial file (no fsync: a power cut is
    not covered). With `expect_token` the target is re-checked just before the swap; the re-check
    and the swap are two steps (see the module docstring). Returns the snapshot meta.
    """
    meta = info(token)
    if is_link(target):
        raise RecoveryError(f"{target.name} is a link")
    if meta["kind"] == "dir":
        try:
            target.mkdir()
        except FileExistsError as exc:
            raise RecoveryError(f"{target.name} already exists") from exc
        except OSError as exc:
            raise RecoveryError(f"couldn't recreate {target.name}: {exc}") from exc
        return meta
    if target.is_dir():
        raise RecoveryError(f"{target.name} is a folder")
    if target.exists() and not replace:
        raise RecoveryError(f"{target.name} already exists")
    tmp = target.with_name(f".{target.name}.friday-restore-{secrets.token_hex(4)}")
    try:
        shutil.copy2(_dir_of(token) / "blob", tmp)
        if sha256_of(tmp) != meta["sha256"]:
            raise RecoveryError("the restored copy did not verify")
        if replace:
            if expect_token is not None and target.exists() and not still_matches(expect_token, target):
                raise RecoveryError(f"{target.name} changed while I was restoring it, so I left it alone")
            os.replace(tmp, target)
        else:
            publish_new(tmp, target)  # refuses to overwrite
    except RecoveryError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise RecoveryError(f"couldn't restore {target.name}: {exc}") from exc
    return meta
