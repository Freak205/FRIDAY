"""Local key management (Phase 87): one random master key per Windows user, protected by DPAPI.

Why it exists: a future persisted fingerprint of an objective (a path, a URL, a name) is only private if it is
a KEYED hash -- an ordinary hash of such low-entropy input can be guessed. This module is only the key; it
does not fingerprint anything.

Design:
  * The master key is 32 bytes from `secrets` (the OS CSPRNG), created on first use.
  * The file holds ONLY the DPAPI-protected blob (current-user scope, UI forbidden, FRIDAY-specific entropy).
    The plaintext key exists only in this process's memory. Its location is `paths.KEY_FILE`
    (`%LOCALAPPDATA%\\FRIDAY\\fingerprint.key`, override FRIDAY_KEY_FILE).
  * Creation is atomic and never overwrites: the blob is written and fsynced to a unique temp file, then
    published with a no-overwrite hard link. A process that loses the race discards its own key and adopts the
    winner's, so concurrent first use (threads or processes) always converges on one key, and an interrupted
    write can only leave a stray `*.tmp-*` file, never a partial key.
  * An existing key file is NEVER replaced. If it is empty, oversized, undecryptable (another user, another
    machine, damaged) or the wrong length, `KeyUnavailable` is raised on every call: fingerprints made under
    the old key must not silently be compared under a new one.
  * Fail closed: no `%LOCALAPPDATA%`, no DPAPI or any I/O error means `KeyUnavailable`. There is no plaintext
    fallback and no fallback to the repository folder (a OneDrive folder).
  * `derive(purpose, version)` gives each use its own subkey (HMAC-SHA256 over a versioned label) so the
    master key itself is never handed out.

Neither key is ever logged or put into an exception message: `KeyUnavailable` carries a fixed reason code and
a fixed sentence, and its cause is suppressed. Tests redirect `paths.KEY_FILE` (scripts/iso_env.py) and
`dbplace.guard_key` refuses a test or benchmark entry point that would reach the real key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from friday import dbplace, paths
from friday.log import get

log = get(__name__)

KEY_BYTES = 32
_MAX_BLOB = 4096  # a DPAPI blob for 32 bytes is ~200; anything far larger is not our file
_ENTROPY = b"friday/keystore/v1"  # DPAPI "optional entropy": a blob made for another purpose won't decrypt here
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_PURPOSE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

_MESSAGES = {
    "no_location": "no per-user application-data folder is available for the key file",
    "no_dpapi": "Windows data protection (DPAPI) is not available",
    "protect_failed": "the key could not be protected",
    "malformed": "the key file is malformed",
    "undecryptable": "the key file could not be decrypted",
    "bad_length": "the key file holds key material of the wrong length",
    "io_error": "the key file could not be read or written",
    "unsafe_location": "the key file location is not allowed (repository, data folder, synced folder, or unclassifiable)",
}


class KeyUnavailable(RuntimeError):
    """No usable key. `reason` is one of a fixed set of codes; the message never carries key material, a blob,
    or the underlying error text."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{_MESSAGES.get(reason, reason)} [{reason}]")


# --- DPAPI (the seam tests replace) --------------------------------------------------------------------

def _protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise KeyUnavailable("no_dpapi")
    try:
        import win32crypt
    except ImportError:
        raise KeyUnavailable("no_dpapi") from None
    try:
        return bytes(win32crypt.CryptProtectData(data, "FRIDAY", _ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN))
    except Exception:  # noqa: BLE001 -- the message is deliberately dropped
        raise KeyUnavailable("protect_failed") from None


def _unprotect(blob: bytes) -> bytes:
    if sys.platform != "win32":
        raise KeyUnavailable("no_dpapi")
    try:
        import win32crypt
    except ImportError:
        raise KeyUnavailable("no_dpapi") from None
    try:
        return bytes(win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN)[1])
    except Exception:  # noqa: BLE001
        raise KeyUnavailable("undecryptable") from None


# --- where the key file may live (Phase 88) ------------------------------------------------------------
#
# A path is allowed only if it is positively classified as outside (a) the repository -- which contains
# `data/` -- and (b) every cloud-synced folder we can enumerate. It is judged both as written (absolute,
# lexical) and fully resolved (symlinks / junctions followed), and BOTH must be clean. Anything that cannot be
# classified (an unreadable registry key, a malformed Dropbox config, an unresolvable path) is refused, never
# assumed safe. This runs before the key file is read and before any directory or file is created.

_PROFILE_SYNC_NAMES = ("dropbox", "google drive", "googledrive", "icloud", "iclouddrive", "box")  # default folders in %USERPROFILE%


class _Unclassified(Exception):
    """A location list could not be read; the caller refuses the path."""


def _lex(p: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(p)))


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def _resolved(p: str | os.PathLike[str]) -> str:
    try:
        return os.path.normcase(os.path.realpath(os.fspath(p)))
    except (OSError, ValueError):
        raise _Unclassified from None


def _onedrive_registry_roots() -> list[str]:
    """Every OneDrive account folder and mounted library the OneDrive client has registered (HKCU)."""
    if sys.platform != "win32":
        return []
    import winreg

    out: list[str] = []
    try:
        accounts = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts")
    except FileNotFoundError:
        return out  # OneDrive has never been set up for this user
    except OSError:
        raise _Unclassified from None
    try:
        with accounts:
            for i in range(winreg.QueryInfoKey(accounts)[0]):
                with winreg.OpenKey(accounts, winreg.EnumKey(accounts, i)) as acct:
                    try:
                        out.append(str(winreg.QueryValueEx(acct, "UserFolder")[0]))
                    except FileNotFoundError:
                        pass
                    try:
                        with winreg.OpenKey(acct, "ScopeIdToMountPointPathCache") as mounts:
                            for j in range(winreg.QueryInfoKey(mounts)[1]):
                                out.append(str(winreg.EnumValue(mounts, j)[1]))
                    except FileNotFoundError:
                        pass
    except OSError:
        raise _Unclassified from None
    return out


def _dropbox_roots() -> list[str]:
    """Folders named in Dropbox's own `info.json` (per-user and per-machine locations)."""
    import json

    out: list[str] = []
    for var in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(var, "").strip()
        if not base:
            continue
        f = Path(base) / "Dropbox" / "info.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            raise _Unclassified from None
        if not isinstance(data, dict):
            raise _Unclassified
        for acct in data.values():
            if isinstance(acct, dict) and isinstance(acct.get("path"), str):
                out.append(acct["path"])
    return out


def _synced_roots() -> list[str]:
    """All cloud-sync roots we can name: OneDrive (environment + client registry), Dropbox (its own config),
    and the default sync folders directly under the user profile."""
    roots: list[str] = []
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        v = os.environ.get(var, "").strip()
        if v:
            roots.append(v)
    roots += _onedrive_registry_roots()
    roots += _dropbox_roots()
    profile = os.environ.get("USERPROFILE", "").strip()
    if profile:
        try:
            for entry in os.scandir(profile):
                low = entry.name.lower()
                if low.startswith("onedrive") or low in _PROFILE_SYNC_NAMES:
                    roots.append(entry.path)
        except FileNotFoundError:
            pass
        except OSError:
            raise _Unclassified from None
    return roots


def _prepare(path: str | os.PathLike[str]) -> str:
    """The path as a plain drive-letter path, or `_Unclassified`.

    Windows reaches one file through many spellings (`\\\\?\\C:\\...`, `\\\\localhost\\C$\\...`) that string
    comparison would not match against the repository. Only the `\\\\?\\X:` / `\\\\.\\X:` forms are unwrapped;
    every other extended or UNC (network) form is refused as unclassifiable, as is an embedded NUL."""
    s = os.fspath(path)
    if "\x00" in s:
        raise _Unclassified
    if s.startswith(("\\\\?\\", "\\\\.\\")):
        rest = s[4:]
        if not re.match(r"^[A-Za-z]:[\\/]", rest):
            raise _Unclassified
        s = rest
    if os.path.splitdrive(s)[0].startswith(("\\\\", "//")):
        raise _Unclassified
    return s


def _unsafe(path: Path) -> str | None:
    """None when `path` is allowed, else a short category (for the log; never shown with a key)."""
    try:
        path = Path(_prepare(path))
        repo = [_lex(paths.ROOT)]
        if not _within(_lex(paths.DATA), repo[0]):
            repo.append(_lex(paths.DATA))  # data/ is normally inside the repository; only list it if it is not
        synced = [_lex(r) for r in _synced_roots()]
        lexical = _lex(path)
        # the path as written: decided without touching the filesystem
        if any(_within(lexical, r) for r in repo):
            return "repository"
        if any(_within(lexical, r) for r in synced):
            return "synced"
        # the path as it really resolves (follows symlinks and junctions, even through missing tails)
        real = _resolved(path)
        if os.path.splitdrive(real)[0].startswith(("\\\\", "//")):
            raise _Unclassified  # resolved onto a network / extended-length path
        repo_real = [_resolved(paths.ROOT)] + ([_resolved(paths.DATA)] if len(repo) > 1 else [])
        if any(_within(real, r) for r in repo_real):
            return "repository"
        if any(_within(real, r) for r in synced + [_resolved(r) for r in synced]):
            return "synced"
        # last, a deliberately over-cautious name check; it can only refuse more, never allow
        if dbplace.in_synced_folder(path) or dbplace.in_synced_folder(real):
            return "synced"
    except _Unclassified:
        return "unclassified"
    return None


def _require_safe_location(path: Path) -> None:
    why = _unsafe(path)
    if why is not None:
        _note(f"unsafe_location:{why}", str(path))
        raise KeyUnavailable("unsafe_location")


# --- the key file --------------------------------------------------------------------------------------

_TMP_STALE_S = 3600  # an in-flight temp file lives for milliseconds; one this old was abandoned by a crash
_lock = threading.Lock()
_cache: dict[str, bytes] = {}  # normalized key-file path -> master key, so DPAPI runs once per path per process
_warned: set[tuple[str, str]] = set()


def _sweep_abandoned_tmp(path: Path) -> None:
    """Delete `<name>.tmp-<12 hex>` files in the key's folder that a crashed creator abandoned.

    Narrow on purpose: the exact name pattern we generate, a plain file (not a link), no bigger than a DPAPI
    blob, and untouched for `_TMP_STALE_S` -- far longer than the milliseconds a live creator needs, so another
    process's in-flight temp file is never taken. Nothing else in the folder is looked at."""
    pattern = re.compile(re.escape(path.name) + r"\.tmp-[0-9a-f]{12}")
    now = time.time()
    try:
        with os.scandir(path.parent) as it:
            for entry in it:
                if not pattern.fullmatch(entry.name):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    if st.st_size > _MAX_BLOB or now - st.st_mtime < _TMP_STALE_S:
                        continue
                    os.unlink(entry.path)
                except OSError:
                    continue
    except (OSError, ValueError):
        return


def _read(path: Path) -> bytes | None:
    """The file's bytes, or None when it does not exist."""
    try:
        with open(path, "rb") as f:
            return f.read(_MAX_BLOB + 1)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise KeyUnavailable("io_error") from None


def _decode(blob: bytes) -> bytes:
    if not blob or len(blob) > _MAX_BLOB:
        raise KeyUnavailable("malformed")
    key = _unprotect(blob)
    if len(key) != KEY_BYTES:
        raise KeyUnavailable("bad_length")
    return key


def _create(path: Path) -> bytes:
    """Make the key file if (and only if) it does not exist; return the key that is now on disk."""
    _require_safe_location(path)  # first, so even a direct caller creates no folder in an unsafe place
    key = secrets.token_bytes(KEY_BYTES)
    blob = _protect(key)  # before touching the disk: with no DPAPI, nothing is left behind
    tmp = path.with_name(f"{path.name}.tmp-{secrets.token_hex(6)}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _require_safe_location(path)  # again now that the folders exist: a link swapped in meanwhile is refused
        with open(tmp, "xb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        dbplace._publish_no_overwrite(tmp, path)  # False = another creator won; either way, read what is there
    except OSError:
        raise KeyUnavailable("io_error") from None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    on_disk = _read(path)
    if on_disk is None:
        raise KeyUnavailable("io_error")
    return _decode(on_disk)  # also proves the published blob decrypts


def _master() -> bytes:
    raw = paths.KEY_FILE
    if not raw:
        _note("no_location", "")
        raise KeyUnavailable("no_location")
    path = Path(raw)
    # every call, cached or not (the repository root or the sync roots can change under a live process), and
    # first: before the isolation guard resolves the path, before the key file is read, before anything is made
    _require_safe_location(path)
    dbplace.guard_key(path)
    cache_key = dbplace._norm(path)
    try:
        with _lock:
            cached = _cache.get(cache_key)
            if cached is not None:
                return cached
            _sweep_abandoned_tmp(path)
            blob = _read(path)
            key = _create(path) if blob is None else _decode(blob)
            _cache[cache_key] = key
            return key
    except KeyUnavailable as exc:
        _note(exc.reason, str(path))
        raise


def _note(reason: str, where: str) -> None:
    """Log each distinct failure once per process (reason and path only -- never a key or a blob)."""
    if (reason, where) not in _warned:
        _warned.add((reason, where))
        log.warning("key store unavailable: %s %s", reason, where)


def derive(purpose: str, version: int = 1) -> bytes:
    """A 32-byte subkey for one purpose: HMAC-SHA256(master, b"friday/<purpose>/v<version>").

    Different purposes and versions give unrelated keys; the master key is never returned. Raises
    `KeyUnavailable` when there is no usable key and `ValueError` for a malformed purpose or version."""
    if not isinstance(purpose, str) or not _PURPOSE.match(purpose):
        raise ValueError("purpose must be a lowercase label such as 'objective-fingerprint'")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 9999:
        raise ValueError("version must be an integer from 1 to 9999")
    return hmac.new(_master(), f"friday/{purpose}/v{version}".encode("ascii"), hashlib.sha256).digest()


def _forget() -> None:  # tests only
    with _lock:
        _cache.clear()
        _warned.clear()
