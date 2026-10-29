"""Phase 87 -- local key management (friday.keystore): a DPAPI-protected random master key.

Every scenario runs against a throwaway key path under the temp folder (`paths.KEY_FILE` is redirected) and a
MOCK DPAPI (`keystore._protect/_unprotect`), so no real user key is created, read or changed. The "real"
locations the tripwire needs are FAKED under the temp folder (LOCALAPPDATA). One scenario round-trips an
in-memory secret through the real DPAPI to prove the pywin32 call is right; it writes no key file of the user's.

  A  first creation: 32 random bytes, only the protected blob on disk, no stray temp files
  B  repeat loading: same key after a cold start, the file is not rewritten
  C  concurrent initialization: many racers converge on ONE key; a lost publish adopts the winner's key
  D  interruption: a failed write leaves no key file, a later call succeeds, stale temp files are harmless
  E  corruption / decryption failure: never replaced, every call keeps failing closed, the file is untouched
  F  missing environment: no LOCALAPPDATA, no DPAPI, no win32crypt -> unavailable, nothing created
  G  subkey derivation: purpose/version separation, validation, never the master key
  H  secrecy: no key, subkey or blob in logs, exceptions or messages
  I  path isolation: iso_env redirects and restores; the tripwire refuses the real key path
  J  real DPAPI round trip (in memory) and end to end in a temp folder
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

import iso_env  # noqa: E402
from friday import dbplace, keystore, paths  # noqa: E402

PREFIX = b"MOCKDPAPI1:"
REAL_PROTECT, REAL_UNPROTECT = keystore._protect, keystore._unprotect  # the genuine DPAPI functions


def mock_protect(data: bytes) -> bytes:
    pad = hashlib.sha256(b"mock-pad").digest() * 8
    return PREFIX + bytes(a ^ b for a, b in zip(data, pad))


def mock_unprotect(blob: bytes) -> bytes:
    if not blob.startswith(PREFIX):
        raise keystore.KeyUnavailable("undecryptable")
    pad = hashlib.sha256(b"mock-pad").digest() * 8
    body = blob[len(PREFIX):]
    return bytes(a ^ b for a, b in zip(body, pad))


@contextlib.contextmanager
def sandbox(mock: bool = True):
    """A throwaway key path (and a mock DPAPI unless `mock` is False); everything restored on exit."""
    with tempfile.TemporaryDirectory(prefix="friday-keytest-", ignore_cleanup_errors=True) as t:
        saved = (paths.KEY_FILE, keystore._protect, keystore._unprotect)
        paths.KEY_FILE = Path(t) / "FRIDAY" / "fingerprint.key"
        if mock:
            keystore._protect, keystore._unprotect = mock_protect, mock_unprotect
        keystore._forget()
        try:
            yield Path(t)
        finally:
            paths.KEY_FILE, keystore._protect, keystore._unprotect = saved
            keystore._forget()


def reason_of(fn, *a) -> str | None:
    try:
        fn(*a)
    except keystore.KeyUnavailable as exc:
        return exc.reason
    return None


def files_in(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def main() -> int:
    t0 = time.time()
    real_key_before = [(p, p.exists()) for p in dbplace.real_key_paths()]

    # ------------------------------------------------------------------ A
    scenario("A  first creation")
    with sandbox() as t:
        kf = paths.KEY_FILE
        check("no key file before first use", not kf.exists())
        k1 = keystore._master()
        check("master key is 32 bytes", isinstance(k1, bytes) and len(k1) == keystore.KEY_BYTES)
        check("key file created in the configured folder", kf.is_file())
        raw = kf.read_bytes()
        check("file holds the protected blob", raw == mock_protect(k1))
        check("plaintext key is not in the file", k1 not in raw)
        check("no temp files are left behind", files_in(kf.parent) == ["fingerprint.key"], str(files_in(kf.parent)))
        check("the key is not all zeros / constant", len(set(k1)) > 8)
        keystore._forget()
        paths.KEY_FILE = Path(t) / "FRIDAY2" / "fingerprint.key"
        k2 = keystore._master()
        check("a different key file gets a different random key", k1 != k2)

    # ------------------------------------------------------------------ B
    scenario("B  repeat loading")
    with sandbox() as t:
        kf = paths.KEY_FILE
        k1 = keystore._master()
        before = (kf.read_bytes(), kf.stat().st_mtime_ns)
        check("second call in-process returns the same key", keystore._master() == k1)
        keystore._forget()
        calls = []
        orig = keystore._unprotect
        keystore._unprotect = lambda b: (calls.append(1), orig(b))[1]
        try:
            k3 = keystore._master()
        finally:
            keystore._unprotect = orig
        check("a cold start decrypts the stored blob to the same key", k3 == k1)
        check("the cold start decrypted exactly once", len(calls) == 1, str(calls))
        check("the file was not rewritten", (kf.read_bytes(), kf.stat().st_mtime_ns) == before)
        d1 = keystore.derive("objective-fingerprint")
        keystore._forget()
        check("derived key is stable across a cold start", keystore.derive("objective-fingerprint") == d1)

    # ------------------------------------------------------------------ C
    scenario("C  concurrent initialization")
    with sandbox() as t:
        kf = paths.KEY_FILE
        keystore._forget()
        results: list[bytes] = []
        errors: list[str] = []
        gate = threading.Barrier(16)

        def racer_public() -> None:
            try:
                gate.wait()
                results.append(keystore._master())
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)

        threads = [threading.Thread(target=racer_public) for _ in range(16)]
        [x.start() for x in threads]
        [x.join() for x in threads]
        check("16 concurrent first calls: no errors", not errors, str(errors))
        check("all 16 got the same key", len(results) == 16 and len(set(results)) == 1)
        check("one key file, no temp files", files_in(kf.parent) == ["fingerprint.key"], str(files_in(kf.parent)))
        check("the file decrypts to the shared key", mock_unprotect(kf.read_bytes()) == results[0])

    with sandbox() as t:
        kf = paths.KEY_FILE
        # bypass the in-process lock: this is what several PROCESSES racing look like
        out: list[bytes] = []
        errs: list[str] = []
        gate = threading.Barrier(16)

        def racer_raw() -> None:
            try:
                gate.wait()
                out.append(keystore._create(kf))
            except Exception as exc:  # noqa: BLE001
                errs.append(type(exc).__name__)

        threads = [threading.Thread(target=racer_raw) for _ in range(16)]
        [x.start() for x in threads]
        [x.join() for x in threads]
        check("16 unlocked racers: no errors", not errs, str(errs))
        check("every racer returns the key that is on disk", len(out) == 16 and set(out) == {mock_unprotect(kf.read_bytes())})
        check("losers' temp files are gone", files_in(kf.parent) == ["fingerprint.key"], str(files_in(kf.parent)))

    with sandbox() as t:
        kf = paths.KEY_FILE
        kf.parent.mkdir(parents=True)
        winner = b"\x07" * keystore.KEY_BYTES
        orig_pub = dbplace._publish_no_overwrite

        def lose_race(tmp: Path, dest: Path) -> bool:
            dest.write_bytes(mock_protect(winner))  # another process published first
            return orig_pub(tmp, dest)  # -> False: dest exists

        dbplace._publish_no_overwrite = lose_race
        try:
            got = keystore._create(kf)
        finally:
            dbplace._publish_no_overwrite = orig_pub
        check("a lost publish adopts the winner's key", got == winner)
        check("the winner's file is untouched", kf.read_bytes() == mock_protect(winner))
        check("the loser's temp file is removed", files_in(kf.parent) == ["fingerprint.key"], str(files_in(kf.parent)))

    # ------------------------------------------------------------------ D
    scenario("D  interruption")
    with sandbox() as t:
        kf = paths.KEY_FILE
        real_fsync = os.fsync

        def broken_fsync(fd: int) -> None:
            raise OSError("disk went away")

        os.fsync = broken_fsync
        try:
            r = reason_of(keystore._master)
        finally:
            os.fsync = real_fsync
        check("a failed write is reported as io_error", r == "io_error", str(r))
        check("no key file was published", not kf.exists())
        check("the partial temp file was removed", files_in(kf.parent) == [], str(files_in(kf.parent)))
        k = keystore._master()
        check("a later call creates the key normally", len(k) == keystore.KEY_BYTES and kf.is_file())

    with sandbox() as t:
        kf = paths.KEY_FILE
        kf.parent.mkdir(parents=True)
        stale = kf.parent / "fingerprint.key.tmp-deadbeef"
        stale.write_bytes(b"half a key")
        k = keystore._master()
        check("a stale temp file does not stop creation", kf.is_file() and len(k) == keystore.KEY_BYTES)
        check("a stale temp file is not mistaken for the key", mock_unprotect(kf.read_bytes()) == k)

    with sandbox() as t:
        kf = paths.KEY_FILE
        real_mkdir = Path.mkdir

        def bad_mkdir(self, *a, **kw):
            raise PermissionError("denied")

        Path.mkdir = bad_mkdir
        try:
            r = reason_of(keystore._master)
        finally:
            Path.mkdir = real_mkdir
        check("an unwritable folder fails closed", r == "io_error", str(r))
        check("nothing was created", not kf.exists())

    # ------------------------------------------------------------------ E
    scenario("E  corrupt or undecryptable key material is never replaced")
    bad_files = {
        "empty": (b"", "malformed"),
        "garbage": (b"this is not a dpapi blob", "undecryptable"),
        "oversized": (PREFIX + b"A" * (keystore._MAX_BLOB + 10), "malformed"),
        "short key": (mock_protect(b"\x01" * 16), "bad_length"),
        "long key": (mock_protect(b"\x01" * 48), "bad_length"),
        "empty key": (mock_protect(b""), "bad_length"),
    }
    for name, (content, want) in bad_files.items():
        with sandbox() as t:
            kf = paths.KEY_FILE
            kf.parent.mkdir(parents=True)
            kf.write_bytes(content)
            r1 = reason_of(keystore._master)
            r2 = reason_of(keystore._master)
            r3 = reason_of(keystore.derive, "objective-fingerprint")
            check(f"{name}: rejected as {want}", r1 == want, str(r1))
            check(f"{name}: keeps failing closed (not cached, not regenerated)", r2 == want and r3 == want, f"{r2}/{r3}")
            check(f"{name}: the file is byte-for-byte untouched", kf.read_bytes() == content)
            check(f"{name}: no other file appeared", files_in(kf.parent) == ["fingerprint.key"], str(files_in(kf.parent)))

    with sandbox() as t:
        kf = paths.KEY_FILE
        good = keystore._master()
        blob = kf.read_bytes()
        keystore._forget()

        def failing_unprotect(b: bytes) -> bytes:
            raise keystore.KeyUnavailable("undecryptable")

        keystore._unprotect = failing_unprotect
        r = reason_of(keystore._master)
        check("a DPAPI decryption failure is undecryptable", r == "undecryptable", str(r))
        check("a decryption failure does not replace the key file", kf.read_bytes() == blob)
        keystore._unprotect = mock_unprotect
        check("once decryption works again the ORIGINAL key is back", keystore._master() == good)

    with sandbox() as t:
        kf = paths.KEY_FILE
        kf.mkdir(parents=True)  # a directory where the file should be
        r = reason_of(keystore._master)
        check("a directory at the key path is io_error", r == "io_error", str(r))
        check("the directory is not touched", kf.is_dir() and not list(kf.iterdir()))

    # ------------------------------------------------------------------ F
    scenario("F  missing environment fails closed")
    with sandbox() as t:
        paths.KEY_FILE = None
        r = reason_of(keystore._master)
        check("no key location -> no_location", r == "no_location", str(r))
        check("derive reports it too", reason_of(keystore.derive, "objective-fingerprint") == "no_location")
        check("nothing was created under the temp root", files_in(t) == [], str(files_in(t)))

    saved_env = {k: os.environ.get(k) for k in ("LOCALAPPDATA", "FRIDAY_KEY_FILE")}
    try:
        os.environ.pop("LOCALAPPDATA", None)
        os.environ.pop("FRIDAY_KEY_FILE", None)
        check("without LOCALAPPDATA the default key path is None", paths._default_key() is None)
        os.environ["LOCALAPPDATA"] = r"C:\Users\someone\AppData\Local"
        check("with LOCALAPPDATA it is <local>\\FRIDAY\\fingerprint.key",
              paths._default_key() == Path(r"C:\Users\someone\AppData\Local") / "FRIDAY" / "fingerprint.key")
        check("the default is never under the repository's data/ folder",
              not dbplace._under(paths._default_key(), paths.DATA))
        os.environ["FRIDAY_KEY_FILE"] = r"C:\somewhere\k.key"
        check("FRIDAY_KEY_FILE overrides it", paths._default_key() == Path(r"C:\somewhere\k.key"))
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    with sandbox(mock=False) as t:
        kf = paths.KEY_FILE
        real_import = sys.modules.get("win32crypt")
        sys.modules["win32crypt"] = None  # makes `import win32crypt` raise ImportError
        try:
            r = reason_of(keystore._master)
        finally:
            if real_import is not None:
                sys.modules["win32crypt"] = real_import
            else:
                sys.modules.pop("win32crypt", None)
        check("no win32crypt -> no_dpapi", r == "no_dpapi", str(r))
        check("no plaintext fallback: nothing was written", not kf.exists() and not kf.parent.exists(), str(files_in(kf.parent)))

    with sandbox() as t:
        kf = paths.KEY_FILE

        def no_dpapi(_: bytes) -> bytes:
            raise keystore.KeyUnavailable("no_dpapi")

        keystore._protect = no_dpapi
        r = reason_of(keystore._master)
        check("DPAPI unavailable on create -> no_dpapi", r == "no_dpapi", str(r))
        check("no key file and no temp file when protection fails", not kf.exists() and files_in(kf.parent) == [])

    with sandbox() as t:
        kf = paths.KEY_FILE
        real_platform = sys.platform
        sys.platform = "linux"
        try:
            r = reason_of(REAL_PROTECT, b"x" * 32)
            r2 = reason_of(REAL_UNPROTECT, b"x" * 32)
        finally:
            sys.platform = real_platform
        check("a non-Windows platform -> no_dpapi (protect)", r == "no_dpapi", str(r))
        check("a non-Windows platform -> no_dpapi (unprotect)", r2 == "no_dpapi", str(r2))

    # ------------------------------------------------------------------ G
    scenario("G  subkey derivation")
    with sandbox() as t:
        master = keystore._master()
        a = keystore.derive("objective-fingerprint")
        check("subkey is 32 bytes", len(a) == 32)
        check("subkey is the HMAC of the versioned label",
              a == hmac.new(master, b"friday/objective-fingerprint/v1", hashlib.sha256).digest())
        check("subkey is not the master key", a != master and master not in a)
        check("deterministic", keystore.derive("objective-fingerprint") == a)
        check("a different purpose gives an unrelated key", keystore.derive("something-else") != a)
        check("a different version gives an unrelated key", keystore.derive("objective-fingerprint", 2) != a)
        for bad in ("", "Upper", "has space", "x" * 65, "../etc", "a/b", "1abc", None, 5):
            try:
                keystore.derive(bad)  # type: ignore[arg-type]
                ok = False
            except ValueError:
                ok = True
            check(f"bad purpose {bad!r} is rejected", ok)
        for bad in (0, -1, 10000, True, "1", 1.5):
            try:
                keystore.derive("objective-fingerprint", bad)  # type: ignore[arg-type]
                ok = False
            except ValueError:
                ok = True
            check(f"bad version {bad!r} is rejected", ok)
        sig = keystore.derive.__code__.co_varnames[: keystore.derive.__code__.co_argcount]
        check("derive takes only purpose and version (no raw material in)", sig == ("purpose", "version"), str(sig))
        other = tempfile.mkdtemp(prefix="friday-keytest-")
        try:
            keystore._forget()
            paths.KEY_FILE = Path(other) / "k.key"
            check("a different master gives a different subkey", keystore.derive("objective-fingerprint") != a)
        finally:
            import shutil

            shutil.rmtree(other, ignore_errors=True)

    # ------------------------------------------------------------------ H
    scenario("H  nothing secret in logs, exceptions or messages")
    cap = Capture()
    root_logger = logging.getLogger()
    old_level = root_logger.level
    root_logger.addHandler(cap)
    root_logger.setLevel(logging.DEBUG)
    try:
        with sandbox() as t:
            kf = paths.KEY_FILE
            k = keystore._master()
            sub = keystore.derive("objective-fingerprint")
            blob = kf.read_bytes()
            secrets_seen = [k, sub, blob]
            kf.write_bytes(PREFIX + b"junk")
            keystore._forget()
            msgs: list[str] = []
            for _ in range(2):
                try:
                    keystore._master()
                except keystore.KeyUnavailable as exc:
                    msgs.append(str(exc))
                    check("the exception has no chained cause", exc.__cause__ is None and (exc.__context__ is None or exc.__suppress_context__))
                    check("the exception carries a fixed reason code", exc.reason == "undecryptable" or exc.reason == "bad_length", exc.reason)
            paths.KEY_FILE = None
            msgs.append(str(reason_of(keystore._master)))
            paths.KEY_FILE = Path(t) / "x" / "k.key"
            keystore._forget()
            # the genuine DPAPI wrappers, over a win32crypt whose errors CARRY the key: the text must be dropped
            import types

            def leaky(*_a, **_kw):
                raise RuntimeError(f"secret {k.hex()} {blob.hex()}")

            fake = types.SimpleNamespace(CryptProtectData=leaky, CryptUnprotectData=leaky)
            real_w32 = sys.modules.get("win32crypt")
            sys.modules["win32crypt"] = fake
            keystore._protect, keystore._unprotect = REAL_PROTECT, REAL_UNPROTECT
            try:
                for fn, arg in ((REAL_UNPROTECT, blob), (REAL_PROTECT, k)):
                    try:
                        fn(arg)
                        check("a failing DPAPI call raises", False)
                    except keystore.KeyUnavailable as exc:
                        msgs.append(repr(exc) + str(exc))
                        check("a failing DPAPI call is reduced to a reason code", exc.reason in ("undecryptable", "protect_failed"), exc.reason)
                        check("...with its cause dropped", exc.__cause__ is None and exc.__suppress_context__)
                keystore._forget()
                try:
                    keystore._master()
                except keystore.KeyUnavailable as exc:
                    msgs.append(repr(exc) + str(exc))
            finally:
                if real_w32 is not None:
                    sys.modules["win32crypt"] = real_w32
                else:
                    sys.modules.pop("win32crypt", None)
            text = "\n".join(cap.lines + msgs)
            hits = [name for name, s in zip(("master", "subkey", "blob"), secrets_seen)
                    if s.hex() in text or s.hex().upper() in text or repr(s) in text or s.decode("latin-1") in text]
            check("no master key, subkey or blob in log lines or exception text", not hits, str(hits))
            check("failures were logged with a reason", any("key store unavailable" in line for line in cap.lines))
            check("a repeated failure is logged once, not per call",
                  sum("undecryptable" in line for line in cap.lines) <= 1, str(cap.lines))
    finally:
        root_logger.removeHandler(cap)
        root_logger.setLevel(old_level)

    # ------------------------------------------------------------------ I
    scenario("I  path isolation")
    check("this process is a test entry point", dbplace.is_test_entrypoint())
    outer = paths.KEY_FILE
    with iso_env.isolated_runtime():
        inside = paths.KEY_FILE
        check("isolated_runtime redirects the key file", inside != outer and inside is not None)
        check("...to a throwaway under the temp folder", dbplace._under(inside, tempfile.gettempdir()))
        check("...which is not a real key path", not dbplace.is_real_key(inside))
        keystore._protect, keystore._unprotect = mock_protect, mock_unprotect
        try:
            kk = keystore._master()
        finally:
            keystore._protect, keystore._unprotect = REAL_PROTECT, REAL_UNPROTECT
        check("a key can be made inside the isolated runtime", len(kk) == 32 and Path(inside).is_file())
    check("the key file path is restored afterwards", paths.KEY_FILE == outer)
    check("the isolated key file is gone with its folder", not Path(inside).exists())

    with tempfile.TemporaryDirectory(prefix="friday-fakelocal-", ignore_cleanup_errors=True) as fake:
        old_local = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = fake
        saved = (paths.KEY_FILE, paths.DEFAULT_KEY_FILE, keystore._protect, keystore._unprotect)
        try:
            real = Path(fake) / "FRIDAY" / "fingerprint.key"  # what canonical_key() resolves to now
            check("canonical_key() follows LOCALAPPDATA", dbplace.canonical_key() == real)
            check("the canonical key path is a real key path", dbplace.is_real_key(real))
            check("a path in the temp tree that is NOT canonical is not real", not dbplace.is_real_key(Path(fake) / "other.key"))
            paths.KEY_FILE = real
            paths.DEFAULT_KEY_FILE = real
            keystore._protect, keystore._unprotect = mock_protect, mock_unprotect
            keystore._forget()
            opened: list[str] = []
            real_open = open

            def spy_open(file, *a, **kw):
                opened.append(str(file))
                return real_open(file, *a, **kw)

            import builtins

            builtins.open = spy_open
            try:
                try:
                    keystore._master()
                    refused = False
                except dbplace.IsolationError:
                    refused = True
            finally:
                builtins.open = real_open
            check("a test entry point is refused the real key path", refused)
            check("...before any file access", not any("fingerprint.key" in o for o in opened), str(opened))
            check("...and nothing was created", not real.exists() and not real.parent.exists())
            check("derive is refused too", _raises(dbplace.IsolationError, keystore.derive, "objective-fingerprint"))
            os.environ["FRIDAY_ALLOW_REAL_DB"] = "1"
            try:
                check("FRIDAY_ALLOW_REAL_DB=1 is the documented opt-out of the tripwire", not dbplace.is_test_entrypoint())
            finally:
                os.environ.pop("FRIDAY_ALLOW_REAL_DB", None)
        finally:
            paths.KEY_FILE, paths.DEFAULT_KEY_FILE, keystore._protect, keystore._unprotect = saved
            keystore._forget()
            if old_local is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old_local

    # ------------------------------------------------------------------ J
    scenario("J  real DPAPI (in memory, then a throwaway folder)")
    try:
        import win32crypt  # noqa: F401
        have = sys.platform == "win32"
    except ImportError:
        have = False
    check("pywin32 (already a pinned dependency) provides DPAPI here", have)
    if have:
        secret = os.urandom(keystore.KEY_BYTES)
        blob = keystore._protect(secret)
        check("the real DPAPI blob does not contain the secret", secret not in blob and len(blob) > len(secret))
        check("it round-trips", keystore._unprotect(blob) == secret)
        flipped = bytearray(blob)
        flipped[-1] ^= 0xFF
        check("a damaged blob is undecryptable", reason_of(keystore._unprotect, bytes(flipped)) == "undecryptable")
        check("a blob made without FRIDAY's entropy is undecryptable", reason_of(keystore._unprotect, _plain_dpapi(secret)) == "undecryptable")
        with sandbox(mock=False) as t:
            kf = paths.KEY_FILE
            k = keystore._master()
            check("end to end: 32-byte key, protected file, no plaintext", len(k) == 32 and k not in kf.read_bytes())
            keystore._forget()
            check("end to end: reload gives the same key", keystore._master() == k)
            check("end to end: it stayed in the throwaway folder", dbplace._under(kf, tempfile.gettempdir()))

    real_key_after = [(p, p.exists()) for p in dbplace.real_key_paths()]
    check("the user's real key path was never created or touched", real_key_before == real_key_after, f"{real_key_before} -> {real_key_after}")

    return finish("Phase 87 key store", time.time() - t0, min_assertions=120, min_scenarios=10)


def _raises(exc_type, fn, *a) -> bool:
    try:
        fn(*a)
    except exc_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _plain_dpapi(data: bytes) -> bytes:
    import win32crypt

    return bytes(win32crypt.CryptProtectData(data, "x", None, None, None, 1))


if __name__ == "__main__":
    sys.exit(main())
