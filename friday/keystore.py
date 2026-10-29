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


# --- the key file --------------------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, bytes] = {}  # normalized key-file path -> master key, so DPAPI runs once per path per process
_warned: set[tuple[str, str]] = set()


def _read(path: Path) -> bytes | None:
    """The file's bytes, or None when it does not exist."""
    try:
        with open(path, "rb") as f:
            return f.read(_MAX_BLOB + 1)
    except FileNotFoundError:
        return None
    except OSError:
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
    key = secrets.token_bytes(KEY_BYTES)
    blob = _protect(key)  # before touching the disk: with no DPAPI, nothing is left behind
    tmp = path.with_name(f"{path.name}.tmp-{secrets.token_hex(6)}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
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
    dbplace.guard_key(path)
    cache_key = dbplace._norm(path)
    try:
        with _lock:
            cached = _cache.get(cache_key)
            if cached is not None:
                return cached
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
