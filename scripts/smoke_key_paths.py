"""Phase 88 -- where the key file may live (friday.keystore._unsafe / _require_safe_location).

Everything runs inside a throwaway folder: the "repository" and its `data/` are FAKE directories there
(`paths.ROOT` / `paths.DATA` are reassigned), "OneDrive" / "Dropbox" / the user profile are fake too (environment
variables point at the temp folder, the registry and Dropbox config are faked), and DPAPI is mocked. The real
repository's `data/` is never named, and no real user key is created, read or changed. The one case that needs the
real repository root uses a path that is rejected on its text alone (no filesystem access).

  A  the valid default path (%LOCALAPPDATA%\\FRIDAY\\fingerprint.key) is accepted and the key is made
  B  an override inside the repository is refused
  C  an override inside data/ is refused
  D  symlink / junction escapes into the repository or a synced folder are refused; harmless links are not
  E  synced folders: OneDrive (env + registry), Dropbox config, default profile folders
  F  fail closed: a location list that cannot be read means the path is not allowed
  G  ordinary isolated temp paths (even unfamiliarly named ones) still work
  H  a refused path causes no filesystem write: no mkdir, no temp file, no key file, no read, no sweep
  I  an existing key file survives a validation failure untouched
  J  abandoned temp files are swept narrowly: only our exact stale pattern, nothing else
  K  extended-length (\\\\?\\X:), UNC and NUL spellings: unwrapped, refused, or never an escaping ValueError;
     keystore._create validates first
  L  validation runs on every call, so a root that changes under a cached key is honoured at once
  M  the user's real key path was never created or touched
"""

from __future__ import annotations

import builtins
import contextlib
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402
from smoke_keystore import files_in, mock_protect, mock_unprotect, reason_of  # noqa: E402

from friday import dbplace, keystore, paths  # noqa: E402

REAL_REGISTRY_ROOTS = keystore._onedrive_registry_roots  # the genuine function (sandbox() installs a stub)
ENV_KEYS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "FRIDAY_KEY_FILE")
WRITEY = {"mkdir", "makedirs", "unlink", "remove", "rmdir", "link", "rename", "replace"}
SKIPPED: list[str] = []


# -- helpers ------------------------------------------------------------------------------------------

def n(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def under(p, root) -> bool:
    a, b = n(p), n(root)
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


@contextlib.contextmanager
def sandbox():
    """Fake repo, fake profile, fake app-data folders, mock DPAPI, a stub registry seam; all restored on exit."""
    with tempfile.TemporaryDirectory(prefix="friday-keypaths-", ignore_cleanup_errors=True) as tn:
        t = Path(tn).resolve()
        s = types.SimpleNamespace(
            t=t, repo=t / "repo", data=t / "repo" / "data", profile=t / "profile",
            appdata=t / "appdata", local=t / "localapp", keys=t / "keys", links=[],
        )
        for d in (s.repo, s.data, s.profile, s.appdata, s.local):
            d.mkdir(parents=True)
        saved_attrs = (paths.KEY_FILE, paths.DEFAULT_KEY_FILE, paths.ROOT, paths.DATA)
        saved_fns = (keystore._protect, keystore._unprotect, keystore._onedrive_registry_roots)
        saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        saved_winreg = sys.modules.get("winreg")
        for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "FRIDAY_KEY_FILE"):
            os.environ.pop(k, None)
        os.environ.update(USERPROFILE=str(s.profile), APPDATA=str(s.appdata), LOCALAPPDATA=str(s.local))
        paths.ROOT, paths.DATA = s.repo, s.data
        keystore._protect, keystore._unprotect = mock_protect, mock_unprotect
        keystore._onedrive_registry_roots = lambda: []
        keystore._forget()
        try:
            yield s
        finally:
            for link in reversed(s.links):  # remove links first so nothing can follow them during cleanup
                try:
                    os.rmdir(link) if os.path.isdir(link) and not os.path.islink(link) or _is_junction(link) else os.unlink(link)
                except OSError:
                    pass
            paths.KEY_FILE, paths.DEFAULT_KEY_FILE, paths.ROOT, paths.DATA = saved_attrs
            keystore._protect, keystore._unprotect, keystore._onedrive_registry_roots = saved_fns
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if saved_winreg is None:
                sys.modules.pop("winreg", None)
            else:
                sys.modules["winreg"] = saved_winreg
            keystore._forget()


def _is_junction(p) -> bool:
    return bool(getattr(os.path, "isjunction", lambda _: False)(p))


def attempt(p) -> str | None:
    """Point the key file at `p`, start cold, and ask for the key. None = it worked."""
    paths.KEY_FILE = Path(p) if p is not None else None
    keystore._forget()
    return reason_of(keystore._master)


def tree(root: Path) -> list[tuple]:
    """Everything under `root` (links not followed): relative name, kind, size, mtime."""
    out = []
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            out.append((os.path.relpath(full, root), os.path.isdir(full) and not os.path.islink(full), st.st_size, st.st_mtime_ns))
    return sorted(out)


def make_dir_link(link: Path, target: Path) -> str | None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return "junction"
    except Exception:  # noqa: BLE001
        return None


def make_file_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link)
        return True
    except (OSError, NotImplementedError):
        return False


class FsSpy:
    """Record every call to the filesystem functions the key store could use, then pass it through."""

    NAMES = ("mkdir", "makedirs", "unlink", "remove", "rmdir", "link", "rename", "replace", "scandir")

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._orig: list[tuple[object, str, object]] = []

    def _wrap(self, name, fn):
        def spy(*a, **kw):
            first = a[0] if a else kw.get("path", kw.get("file", ""))
            mode = ""
            if name == "open":
                mode = str(a[1] if len(a) > 1 else kw.get("mode", "r"))
            self.calls.append((name, str(first), mode))
            return fn(*a, **kw)

        return spy

    def __enter__(self):
        for mod, name in [(os, x) for x in self.NAMES] + [(builtins, "open")]:
            orig = getattr(mod, name)
            self._orig.append((mod, name, orig))
            setattr(mod, name, self._wrap(name, orig))
        return self

    def __exit__(self, *exc):
        for mod, name, orig in self._orig:
            setattr(mod, name, orig)
        return False

    def violations(self, area: Path, key_parent: Path) -> list[tuple[str, str, str]]:
        """Calls that must not happen for a refused key path."""
        bad = []
        for name, p, mode in self.calls:
            if under(p, key_parent):
                bad.append((name, p, mode))  # ANY touch of the key's folder, read or write
            elif under(p, area) and (name in WRITEY or (name == "open" and any(c in mode for c in "wxa+"))):
                bad.append((name, p, mode))
        return bad


def write_dropbox(base: Path, **accounts) -> None:
    d = base / "Dropbox"
    d.mkdir(parents=True, exist_ok=True)
    (d / "info.json").write_text(json.dumps(accounts), encoding="utf-8")


class Node:
    def __init__(self, subs=None, vals=None) -> None:
        self.subs, self.vals = subs or {}, vals or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_winreg(accounts, *, enum_error: bool = False) -> types.ModuleType:
    """A winreg whose HKCU\\Software\\Microsoft\\OneDrive\\Accounts is `accounts` (a Node, an exception, or None)."""
    m = types.ModuleType("winreg")
    m.HKEY_CURRENT_USER = "HKCU"

    def OpenKey(base, sub):
        if base == "HKCU":
            if isinstance(accounts, BaseException):
                raise accounts
            if accounts is None:
                raise FileNotFoundError(sub)
            return accounts
        if sub not in base.subs:
            raise FileNotFoundError(sub)
        return base.subs[sub]

    def EnumKey(node, i):
        if enum_error:
            raise PermissionError("denied")
        return list(node.subs)[i]

    def QueryValueEx(node, name):
        if name not in node.vals:
            raise FileNotFoundError(name)
        return node.vals[name], 1

    m.OpenKey, m.EnumKey, m.QueryValueEx = OpenKey, EnumKey, QueryValueEx
    m.QueryInfoKey = lambda node: (len(node.subs), len(node.vals), 0)
    m.EnumValue = lambda node, j: (list(node.vals)[j], list(node.vals.values())[j], 1)
    return m


# -- the scenarios ------------------------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    real_key_before = [(p, p.exists()) for p in dbplace.real_key_paths()]
    real_root, real_data, real_key_file = paths.ROOT, paths.DATA, paths.KEY_FILE

    # ------------------------------------------------------------------ A
    scenario("A  the valid default path")
    with sandbox() as s:
        os.environ["LOCALAPPDATA"] = str(s.t / "lad")
        d = paths._default_key()
        check("default is <LOCALAPPDATA>\\FRIDAY\\fingerprint.key", d == s.t / "lad" / "FRIDAY" / "fingerprint.key", str(d))
        # keep the test tripwire away from this stand-in: canonical_key() must differ from the path under test
        os.environ["LOCALAPPDATA"] = str(s.local)
        check("it is not the (fake) canonical real key path", not dbplace.is_real_key(d))
        check("classified as safe", keystore._unsafe(d) is None, str(keystore._unsafe(d)))
        r = attempt(d)
        check("the default-shaped path yields a key", r is None, str(r))
        check("the key file was created there", d.is_file() and mock_unprotect(d.read_bytes()) == keystore._master())
        check("derive works from it", len(keystore.derive("objective-fingerprint")) == 32)
        check("only the key file is in the folder", files_in(d.parent) == ["fingerprint.key"], str(files_in(d.parent)))

        os.environ.pop("LOCALAPPDATA")
        check("without LOCALAPPDATA there is no default", paths._default_key() is None)
        check("...and the key store stays unavailable (no_location)", attempt(None) == "no_location")

    with sandbox() as s:
        def no_dpapi(_):
            raise keystore.KeyUnavailable("no_dpapi")

        keystore._protect = no_dpapi
        p = s.keys / "k.key"
        check("a valid path with no DPAPI still fails closed (no_dpapi)", attempt(p) == "no_dpapi")
        check("...and writes nothing", not p.exists() and files_in(s.keys) == [])

    with sandbox() as s:
        p = s.keys / "k.key"
        s.keys.mkdir()
        p.write_bytes(b"not a real blob")
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        r = attempt(p)
        check("a corrupt key at a valid path is still rejected, not regenerated", r == "undecryptable", str(r))
        check("...and left as it was", (p.read_bytes(), p.stat().st_mtime_ns) == before)

    # ------------------------------------------------------------------ B
    scenario("B  an override inside the repository")
    with sandbox() as s:
        for rel in ("k.key", "keys/k.key", "a/b/c/k.key", "FRIDAY/fingerprint.key"):
            p = s.repo / rel
            check(f"repo/{rel} is refused", attempt(p) == "unsafe_location")
            check(f"repo/{rel}: category is 'repository'", keystore._unsafe(p) == "repository", str(keystore._unsafe(p)))
            check(f"repo/{rel}: nothing was created", not p.exists() and not p.parent.exists() or p.parent == s.repo)
        p = s.repo / "keys" / "k.key"
        os.environ["FRIDAY_KEY_FILE"] = str(p)
        check("FRIDAY_KEY_FILE feeds paths._default_key()", paths._default_key() == p)
        check("the environment override inside the repository is refused", attempt(paths._default_key()) == "unsafe_location")
        os.environ["FRIDAY_KEY_FILE"] = str(s.t / "keys" / ".." / "repo" / "k.key")
        check("a '..' traversal into the repository is refused", attempt(paths._default_key()) == "unsafe_location")
        check("a different letter case is refused (Windows paths are case-insensitive)",
              attempt(Path(str(s.repo / "K.KEY").upper())) == "unsafe_location")
        os.environ.pop("FRIDAY_KEY_FILE")
        cwd = os.getcwd()
        os.chdir(s.repo)
        try:
            check("a relative path resolves against the cwd and is refused", attempt(Path("k.key")) == "unsafe_location")
        finally:
            os.chdir(cwd)
        check("a sibling folder that merely shares the repository's name prefix is allowed",
              attempt(s.t / "repo-other" / "k.key") is None)
        check("the repository itself is refused", attempt(s.repo) == "unsafe_location")

    with sandbox() as s:
        paths.ROOT, paths.DATA = real_root, real_data  # the REAL root, but a path refused on its text alone
        p = real_root / "_nonexistent_keytest" / "k.key"
        check("a path under the real repository root is refused", attempt(p) == "unsafe_location")
        check("...and nothing was created in the real repository", not p.parent.exists())

    # ------------------------------------------------------------------ C
    scenario("C  an override inside data/")
    with sandbox() as s:
        for rel in ("k.key", "cache/k.key", "logs/x/k.key"):
            p = s.data / rel
            check(f"data/{rel} is refused", attempt(p) == "unsafe_location")
            check(f"data/{rel}: nothing was created", not p.exists() and not (s.data / rel).parent.exists() or p.parent == s.data)
        os.environ["FRIDAY_KEY_FILE"] = str(s.data / "k.key")
        check("the environment override inside data/ is refused", attempt(paths._default_key()) == "unsafe_location")
        os.environ.pop("FRIDAY_KEY_FILE")
        check("data/ is listed even though it sits inside the repository, so one rule covers it",
              keystore._unsafe(s.data / "k.key") == "repository")

    with sandbox() as s:
        elsewhere = s.t / "elsewhere-data"
        elsewhere.mkdir()
        paths.DATA = elsewhere  # a data folder that is NOT under the repository root
        check("a data folder outside the repository is refused too", attempt(elsewhere / "k.key") == "unsafe_location")
        check("...categorised as repository", keystore._unsafe(elsewhere / "k.key") == "repository")
        check("the repository root is still refused", attempt(s.repo / "k.key") == "unsafe_location")
        check("an unrelated folder is still allowed", attempt(s.t / "keys" / "k.key") is None)

    # ------------------------------------------------------------------ D
    scenario("D  symlinks and junctions")
    with sandbox() as s:
        link = s.t / "link-to-repo"
        kind = make_dir_link(link, s.repo)
        if kind is None:
            SKIPPED.append("D: neither a symlink nor a junction could be created here")
            print("  SKIP no symlink/junction support on this machine")
        else:
            s.links.append(link)
            print(f"  (using a {kind})")
            check("the link really points at the repository", os.path.realpath(link) == os.path.realpath(s.repo))
            check("the path as written is not under the repository (only its resolution is)", not under(link / "k.key", s.repo))
            for rel in ("k.key", "sub/k.key", "data/k.key", "data/deep/k.key"):
                p = link / rel
                check(f"through the {kind}: {rel} is refused", attempt(p) == "unsafe_location")
                check(f"through the {kind}: {rel} resolves to 'repository'", keystore._unsafe(p) == "repository", str(keystore._unsafe(p)))
            check("nothing was created in the repository through the link",
                  not (s.repo / "k.key").exists() and not (s.repo / "sub").exists() and not (s.data / "deep").exists())

            od = s.t / "od-real"
            od.mkdir()
            os.environ["OneDrive"] = str(od)
            odlink = s.t / "link-to-onedrive"
            if make_dir_link(odlink, od):
                s.links.append(odlink)
                check("a link into a synced folder is refused", attempt(odlink / "FRIDAY" / "k.key") == "unsafe_location")
                check("...categorised as synced", keystore._unsafe(odlink / "FRIDAY" / "k.key") == "synced")
                check("nothing was created in the synced folder", not (od / "FRIDAY").exists())
            os.environ.pop("OneDrive")

            ok_dir = s.t / "ordinary"
            ok_dir.mkdir()
            okl = s.t / "link-to-ordinary"
            if make_dir_link(okl, ok_dir):
                s.links.append(okl)
                r = attempt(okl / "k.key")
                check("a link to an ordinary folder is allowed", r is None, str(r))
                check("...and the key lands in the real target", (ok_dir / "k.key").is_file())

            # a link planted INSIDE the repository, pointing out: caught as written
            outward = s.repo / "out"
            if make_dir_link(outward, s.t / "ordinary"):
                s.links.append(outward)
                check("a link inside the repository is refused as written", attempt(outward / "k2.key") == "unsafe_location")
                check("...so nothing was written through it", not (s.t / "ordinary" / "k2.key").exists())

    with sandbox() as s:
        s.keys.mkdir()
        target = s.repo / "target.key"
        target.write_bytes(mock_protect(b"\x05" * 32))
        keylink = s.keys / "k.key"
        if not make_file_link(keylink, target):
            SKIPPED.append("D: a file symlink could not be created here (needs privilege)")
            print("  SKIP no file-symlink support on this machine")
        else:
            s.links.append(keylink)
            before = (target.read_bytes(), target.stat().st_mtime_ns)
            check("a key file that is a symlink into the repository is refused", attempt(keylink) == "unsafe_location")
            check("...and the repository file is untouched", (target.read_bytes(), target.stat().st_mtime_ns) == before)
            check("...the key it holds was never decrypted or adopted", keystore._cache == {})

    # ------------------------------------------------------------------ E
    scenario("E  known synced folders")
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        with sandbox() as s:
            root = s.t / f"sync-{var}"
            root.mkdir()
            os.environ[var] = str(root)
            for rel in ("FRIDAY/fingerprint.key", "k.key", "a/b/k.key"):
                p = root / rel
                check(f"{var}: {rel} is refused", attempt(p) == "unsafe_location")
                check(f"{var}: {rel} is categorised synced", keystore._unsafe(p) == "synced", str(keystore._unsafe(p)))
            check(f"{var}: nothing was created", files_in(root) == [], str(files_in(root)))
            check(f"{var}: a sibling with the same name prefix is allowed", attempt(s.t / f"sync-{var}-2" / "k.key") is None)
            check(f"{var}: a trailing-slash root still matches", keystore._unsafe(Path(str(root) + os.sep) / "k.key") == "synced")
            os.environ["FRIDAY_KEY_FILE"] = str(root / "k.key")
            check(f"{var}: the FRIDAY_KEY_FILE override is refused", attempt(paths._default_key()) == "unsafe_location")

    with sandbox() as s:
        os.environ["OneDrive"] = str(s.t / "od")
        os.environ["LOCALAPPDATA"] = str(s.t / "od" / "Local")  # LOCALAPPDATA itself redirected into OneDrive
        d = paths._default_key()
        os.environ["LOCALAPPDATA"] = str(s.local)
        check("a DEFAULT path that lands in OneDrive is refused", attempt(d) == "unsafe_location")
        check("...and nothing was created", not (s.t / "od").exists())

    with sandbox() as s:
        reg_root = s.t / "reg-onedrive"
        keystore._onedrive_registry_roots = lambda: [str(reg_root)]
        check("a registry-listed OneDrive folder is refused", attempt(reg_root / "FRIDAY" / "k.key") == "unsafe_location")
        check("a path outside it is allowed", attempt(s.t / "other" / "k.key") is None)

    with sandbox() as s:
        # the genuine registry reader against a fake winreg
        personal, biz, mount = s.t / "reg-personal", s.t / "reg-business", s.t / "reg-library"
        tree_ = Node(subs={
            "Personal": Node(vals={"UserFolder": str(personal), "Other": "x"}),
            "Business1": Node(
                vals={"UserFolder": str(biz)},
                subs={"ScopeIdToMountPointPathCache": Node(vals={"id1": str(mount), "id2": str(mount) + "-2"})},
            ),
            "Empty": Node(),
        })
        sys.modules["winreg"] = fake_winreg(tree_)
        keystore._onedrive_registry_roots = REAL_REGISTRY_ROOTS
        got = set(REAL_REGISTRY_ROOTS())
        check("registry: account folders and mounted libraries are all read",
              got == {str(personal), str(biz), str(mount), str(mount) + "-2"}, str(got))
        for name, root in (("personal", personal), ("business", biz), ("library mount", mount)):
            check(f"registry: the {name} folder is refused", attempt(root / "FRIDAY" / "k.key") == "unsafe_location")
        check("registry: an unrelated folder is allowed", attempt(s.t / "plain" / "k.key") is None)
        sys.modules["winreg"] = fake_winreg(None)
        check("registry: OneDrive never set up -> nothing listed", REAL_REGISTRY_ROOTS() == [])
        check("registry: ...and ordinary paths work", attempt(s.t / "plain2" / "k.key") is None)

    with sandbox() as s:
        real_list = REAL_REGISTRY_ROOTS()
        check("the real registry reader returns a list of strings without error",
              isinstance(real_list, list) and all(isinstance(x, str) for x in real_list))

    with sandbox() as s:
        dbx, biz = s.t / "dbx-personal", s.t / "dbx-business"
        write_dropbox(s.appdata, personal={"path": str(dbx), "host": 1}, other={"nothing": 1})
        check("Dropbox: the personal folder from info.json is refused", attempt(dbx / "FRIDAY" / "k.key") == "unsafe_location")
        check("Dropbox: ...categorised synced", keystore._unsafe(dbx / "k.key") == "synced")
        check("Dropbox: an unrelated folder is allowed", attempt(s.t / "plain" / "k.key") is None)
        write_dropbox(s.local, business={"path": str(biz)})
        check("Dropbox: a folder listed under LOCALAPPDATA is refused", attempt(biz / "k.key") == "unsafe_location")
        write_dropbox(s.appdata)
        write_dropbox(s.local)
        check("Dropbox: an empty config lists nothing", attempt(s.t / "plain3" / "k.key") is None)

    with sandbox() as s:
        for name in ("OneDrive", "OneDrive - Contoso", "Dropbox", "Google Drive", "GoogleDrive", "iCloudDrive", "Box"):
            (s.profile / name).mkdir()
            p = s.profile / name / "FRIDAY" / "k.key"
            check(f"profile folder '{name}' is refused", attempt(p) == "unsafe_location")
            check(f"profile folder '{name}': nothing was created", files_in(s.profile / name) == [], str(files_in(s.profile / name)))
        check("an ordinary profile folder (Documents) is allowed", attempt(s.profile / "Documents" / "k.key") is None)

    # ------------------------------------------------------------------ F
    scenario("F  an unreadable location list fails closed")
    with sandbox() as s:
        p = s.t / "plain" / "k.key"
        check("sanity: the path is allowed when every list is readable", attempt(p) is None)

    with sandbox() as s:
        p = s.t / "plain" / "k.key"

        def broken() -> list[str]:
            raise keystore._Unclassified

        keystore._onedrive_registry_roots = broken
        check("a registry reader that cannot classify refuses the path", attempt(p) == "unsafe_location")
        check("...categorised unclassified", keystore._unsafe(p) == "unclassified", str(keystore._unsafe(p)))
        check("...and creates nothing", not p.parent.exists())
        check("derive is refused too", reason_of(keystore.derive, "objective-fingerprint") == "unsafe_location")

    for label, tree_, enum_err in (
        ("registry key unreadable (OSError)", PermissionError("denied"), False),
        ("registry enumeration fails midway", Node(subs={"A": Node(vals={"UserFolder": "x"})}), True),
    ):
        with sandbox() as s:
            sys.modules["winreg"] = fake_winreg(tree_, enum_error=enum_err)
            keystore._onedrive_registry_roots = REAL_REGISTRY_ROOTS
            p = s.t / "plain" / "k.key"
            check(f"{label}: refused", attempt(p) == "unsafe_location")
            check(f"{label}: categorised unclassified", keystore._unsafe(p) == "unclassified", str(keystore._unsafe(p)))
            check(f"{label}: nothing created", not p.parent.exists())

    for label, content in (("malformed JSON", "{not json"), ("not an object", "[1, 2]"), ("not JSON at all", "\x00\x01")):
        with sandbox() as s:
            (s.appdata / "Dropbox").mkdir()
            (s.appdata / "Dropbox" / "info.json").write_text(content, encoding="utf-8")
            p = s.t / "plain" / "k.key"
            check(f"Dropbox config {label}: refused", attempt(p) == "unsafe_location")
            check(f"Dropbox config {label}: categorised unclassified", keystore._unsafe(p) == "unclassified")
            check(f"Dropbox config {label}: nothing created", not p.parent.exists())

    with sandbox() as s:
        (s.appdata / "Dropbox" / "info.json").mkdir(parents=True)  # unreadable as a file
        check("an unreadable Dropbox config (a directory) is refused", attempt(s.t / "plain" / "k.key") == "unsafe_location")

    with sandbox() as s:
        junk = s.t / "profile-is-a-file"
        junk.write_text("x")
        os.environ["USERPROFILE"] = str(junk)
        check("a user profile that cannot be listed is refused", attempt(s.t / "plain" / "k.key") == "unsafe_location")
        os.environ["USERPROFILE"] = str(s.t / "no-such-profile")
        check("a user profile that does not exist is simply empty", attempt(s.t / "plain2" / "k.key") is None)
        os.environ.pop("USERPROFILE")
        check("an unset USERPROFILE adds no roots", attempt(s.t / "plain3" / "k.key") is None)

    # ------------------------------------------------------------------ G
    scenario("G  ordinary isolated temp paths keep working")
    with sandbox() as s:
        for rel in ("keys/k.key", "zzz-qwerty/k.key", "Sync-Stuff/k.key", "a/b/c/d/fingerprint.key", "my docs/k.key"):
            p = s.t / rel
            check(f"{rel}: classified safe", keystore._unsafe(p) is None, str(keystore._unsafe(p)))
            r = attempt(p)
            check(f"{rel}: a key is created", r is None and p.is_file(), str(r))
            k = keystore._master()
            check(f"{rel}: stored protected, not plaintext", mock_unprotect(p.read_bytes()) == k and k not in p.read_bytes())
            keystore._forget()
            check(f"{rel}: reloads the same key", keystore._master() == k)
        check("a path in the system temp folder itself is allowed",
              keystore._unsafe(Path(tempfile.gettempdir()) / "friday-keypaths-probe" / "k.key") is None)

    # ------------------------------------------------------------------ H
    scenario("H  a refused path causes no filesystem write")
    cases = []
    with sandbox() as s:
        cases.append(("repository override", s.repo / "new" / "deep" / "k.key", None))
        cases.append(("data override", s.data / "new" / "k.key", None))
        od = s.t / "od"
        os.environ["OneDrive"] = str(od)
        cases.append(("OneDrive override", od / "new" / "FRIDAY" / "k.key", None))
        os.environ["LOCALAPPDATA"] = str(od / "Local")
        default = paths._default_key()
        os.environ["LOCALAPPDATA"] = str(s.local)
        cases.append(("OneDrive default", default, None))
        for label, p, _ in cases:
            before = tree(s.t)
            parent_before = p.parent.exists()
            with FsSpy() as spy:
                r = attempt(p)
            bad = spy.violations(s.t, p.parent)
            check(f"{label}: refused as unsafe_location", r == "unsafe_location", str(r))
            check(f"{label}: no filesystem call touched the key's folder or wrote anything", not bad, str(bad))
            check(f"{label}: no folder was created", p.parent.exists() == parent_before)
            check(f"{label}: the whole tree is exactly as before", tree(s.t) == before)
            check(f"{label}: derive is refused with nothing written", reason_of(keystore.derive, "objective-fingerprint") == "unsafe_location")

        # the validator must also run before the key file is READ
        pre = s.repo / "existing.key"
        pre.write_bytes(mock_protect(b"\x09" * 32))
        calls = []
        keystore._unprotect = lambda b: (calls.append(1), mock_unprotect(b))[1]
        with FsSpy() as spy:
            r = attempt(pre)
        check("an existing file in an unsafe place is refused", r == "unsafe_location")
        check("...it was never opened", not [c for c in spy.calls if c[0] == "open" and under(c[1], pre.parent)], str(spy.calls))
        check("...it was never decrypted", calls == [])

    with sandbox() as s:
        # a tmp-like file in an unsafe folder: the sweep must not run there
        s.keys.mkdir()
        od = s.t / "od2"
        (od / "FRIDAY").mkdir(parents=True)
        os.environ["OneDrive"] = str(od)
        stale = od / "FRIDAY" / "k.key.tmp-0123456789ab"
        stale.write_bytes(b"x")
        old = time.time() - keystore._TMP_STALE_S - 600
        os.utime(stale, (old, old))
        with FsSpy() as spy:
            r = attempt(od / "FRIDAY" / "k.key")
        check("a stale temp file in a refused folder is refused, not swept", r == "unsafe_location" and stale.exists())
        check("...the folder was never even listed", not [c for c in spy.calls if c[0] == "scandir" and under(c[1], od)], str(spy.calls))

    # ------------------------------------------------------------------ I
    scenario("I  an existing key survives a validation failure")
    with sandbox() as s:
        variants = {}
        od = s.t / "od"
        (od / "FRIDAY").mkdir(parents=True)
        os.environ["OneDrive"] = str(od)
        variants["valid blob in OneDrive"] = (od / "FRIDAY" / "fingerprint.key", mock_protect(b"\x01" * 32))
        variants["corrupt file in OneDrive"] = (od / "FRIDAY" / "other.key", b"junk junk")
        variants["valid blob in the repository"] = (s.repo / "k.key", mock_protect(b"\x02" * 32))
        variants["valid blob in data/"] = (s.data / "k.key", mock_protect(b"\x03" * 32))
        variants["empty file in data/"] = (s.data / "empty.key", b"")
        for label, (p, content) in variants.items():
            p.write_bytes(content)
        for label, (p, content) in variants.items():
            snap = tree(s.t)
            calls = []
            keystore._unprotect = lambda b: (calls.append(1), mock_unprotect(b))[1]
            with FsSpy() as spy:
                r = attempt(p)
            check(f"{label}: refused as unsafe_location", r == "unsafe_location", str(r))
            check(f"{label}: bytes unchanged", p.read_bytes() == content)
            check(f"{label}: the whole tree (names, sizes, mtimes) unchanged", tree(s.t) == snap)
            check(f"{label}: never opened or decrypted", calls == [] and not [c for c in spy.calls if c[0] == "open" and under(c[1], p)])
        keystore._unprotect = mock_unprotect

    with sandbox() as s:
        # a GOOD key, then the location list becomes unreadable: the key is neither used nor replaced
        p = s.keys / "k.key"
        r = attempt(p)
        good = keystore._master()
        blob, mt = p.read_bytes(), p.stat().st_mtime_ns
        keystore._onedrive_registry_roots = lambda: (_ for _ in ()).throw(keystore._Unclassified())
        keystore._forget()
        check("a good key is not handed out when the location cannot be classified", reason_of(keystore._master) == "unsafe_location")
        check("...and the key file is untouched", (p.read_bytes(), p.stat().st_mtime_ns) == (blob, mt))
        keystore._onedrive_registry_roots = lambda: []
        keystore._forget()
        check("once classification works again the ORIGINAL key is back", keystore._master() == good)

    # ------------------------------------------------------------------ J
    scenario("J  abandoned temp files are swept narrowly")
    old = time.time() - keystore._TMP_STALE_S - 600
    fresh = time.time()
    near = time.time() - keystore._TMP_STALE_S + 120  # still inside the grace period

    def plant(folder: Path) -> dict[str, Path]:
        """One file/dir per situation; returns name -> path. Every one of them is aged unless noted."""
        folder.mkdir(parents=True, exist_ok=True)
        f: dict[str, Path] = {}

        def mk(name, data=b"x", mtime=old, kind="file"):
            p = folder / name
            if kind == "dir":
                p.mkdir()
            else:
                p.write_bytes(data)
            os.utime(p, (mtime, mtime))
            f[name] = p

        mk("k.key.tmp-0123456789ab")  # stale, ours
        mk("k.key.tmp-fedcba987654")  # stale, ours
        mk("k.key.tmp-aaaaaaaaaaaa", mtime=fresh)  # in flight
        mk("k.key.tmp-bbbbbbbbbbbb", mtime=near)  # inside the grace period
        mk("k.key.tmp-xyz")  # wrong suffix
        mk("k.key.tmp-0123456789abc")  # 13 hex
        mk("k.key.tmp-0123456789a")  # 11 hex
        mk("k.key.tmp-ABCDEF012345")  # upper-case hex (a distinct name: NTFS ignores case)
        mk("k.key.tmp-0123456789ab.bak")  # extension
        mk("k.key.tmp-")  # nothing after the dash
        mk("other.key.tmp-0123456789ab")  # another key's temp
        mk("xk.key.tmp-0123456789ab")  # prefix differs
        mk("notes.txt")  # unrelated
        mk("k.key.tmp-cccccccccccc", data=b"A" * (keystore._MAX_BLOB + 1))  # too big to be our blob
        mk("k.key.tmp-dddddddddddd", kind="dir")  # a directory with the right name
        return f

    STALE = ("k.key.tmp-0123456789ab", "k.key.tmp-fedcba987654")

    with sandbox() as s:
        p = s.keys / "k.key"
        planted = plant(s.keys)
        sibling = s.t / "sibling"
        sib = plant(sibling)
        link_ok = make_file_link(s.keys / "k.key.tmp-eeeeeeeeeeee", s.t / "link-target")
        (s.t / "link-target").write_bytes(b"target")
        if link_ok:
            s.links.append(s.keys / "k.key.tmp-eeeeeeeeeeee")
        else:
            SKIPPED.append("J: file symlink case skipped (needs privilege)")
            print("  SKIP no file-symlink support on this machine")
        r = attempt(p)  # no key yet: the creation path
        check("creation still works", r is None and p.is_file(), str(r))
        for name in STALE:
            check(f"stale {name} was removed", not planted[name].exists())
        for name, path in planted.items():
            if name not in STALE:
                check(f"kept: {name}", path.exists())
        check("a mtime inside the grace period is kept", planted["k.key.tmp-bbbbbbbbbbbb"].exists())
        check("a matching DIRECTORY is kept", planted["k.key.tmp-dddddddddddd"].is_dir())
        if link_ok:
            check("a symlink with a matching name is kept", os.path.islink(s.keys / "k.key.tmp-eeeeeeeeeeee"))
            check("...and so is its target", (s.t / "link-target").read_bytes() == b"target")
        check("nothing in the sibling folder was touched", all(path.exists() for path in sib.values()))
        check("unrelated files are untouched", planted["notes.txt"].read_bytes() == b"x")

        # now the LOAD path: key exists, new abandoned files appear
        again = plant(s.t / "again")
        shutil_target = s.t / "again" / "k.key"
        shutil_target.write_bytes(p.read_bytes())
        k = keystore._master()
        r = attempt(shutil_target)
        check("loading an existing key works", r is None and keystore._master() == k)
        for name in STALE:
            check(f"load path: stale {name} was removed", not again[name].exists())
        check("load path: the in-flight file is kept", again["k.key.tmp-aaaaaaaaaaaa"].exists())
        check("load path: the key file is untouched", shutil_target.read_bytes() == p.read_bytes())

    with sandbox() as s:
        # another key name: only ITS temp files match
        s.keys.mkdir()
        a, b = s.keys / "fp.key.tmp-111111111111", s.keys / "k.key.tmp-222222222222"
        for f_ in (a, b):
            f_.write_bytes(b"x")
            os.utime(f_, (old, old))
        attempt(s.keys / "fp.key")
        check("the sweep matches the key file's own name", not a.exists())
        check("...and leaves another name's temp file alone", b.exists())

    with sandbox() as s:
        # a file we cannot delete must not break key loading
        s.keys.mkdir()
        locked = s.keys / "k.key.tmp-333333333333"
        locked.write_bytes(b"x")
        os.utime(locked, (old, old))
        real_unlink = os.unlink

        def failing_unlink(path, *a, **kw):
            if str(path).endswith("tmp-333333333333"):
                raise PermissionError("in use")
            return real_unlink(path, *a, **kw)

        os.unlink = failing_unlink
        try:
            r = attempt(s.keys / "k.key")
        finally:
            os.unlink = real_unlink
        check("a temp file that cannot be deleted does not stop the key", r is None and (s.keys / "k.key").is_file(), str(r))
        check("...and is left in place", locked.exists())

    with sandbox() as s:
        r = attempt(s.t / "no-folder-yet" / "k.key")
        check("a missing folder is not an error for the sweep", r is None)

    # ------------------------------------------------------------------ K
    scenario("K  extended-length, UNC and NUL paths")
    EXT, DEV = "\\\\?\\", "\\\\.\\"  # the prefixes \\?\ and \\.\ (built from escapes, never from a heredoc)

    def outcome(p) -> str:
        try:
            r = attempt(p)
        except Exception as exc:  # noqa: BLE001
            return f"raised {type(exc).__name__}"
        return r or "ok"

    with sandbox() as s:
        repo_s, data_s = str(s.repo), str(s.data)
        for label, raw, want in (
            ("extended-length (\\\\?\\) into the repository", EXT + repo_s + "\\sub\\k", "repository"),
            ("device (\\\\.\\) into the repository", DEV + repo_s + "\\sub\\k", "repository"),
            ("extended-length into data/", EXT + data_s + "\\k.key", "repository"),
            ("extended-length repository with forward slashes after the drive", EXT + repo_s.replace("\\", "/") + "/k", "repository"),
            ("extended-length repository, upper-cased", (EXT + repo_s + "\\K").upper(), "repository"),
        ):
            p = Path(raw)
            check(f"{label}: refused", outcome(p) == "unsafe_location", outcome(p))
            check(f"{label}: categorised {want}", keystore._unsafe(p) == want, str(keystore._unsafe(p)))
            check(f"{label}: nothing was created", not (s.repo / "sub").exists() and not (s.repo / "k").exists() and not (s.data / "k.key").exists())

        od = s.t / "od-ext"
        od.mkdir()
        os.environ["OneDrive"] = str(od)
        p = Path(EXT + str(od) + "\\FRIDAY\\k.key")
        check("extended-length into a synced folder: refused", outcome(p) == "unsafe_location")
        check("...categorised synced", keystore._unsafe(p) == "synced", str(keystore._unsafe(p)))
        check("...nothing was created", files_in(od) == [], str(files_in(od)))
        os.environ.pop("OneDrive")

        for label, raw in (
            ("\\\\?\\UNC\\ form", EXT + "UNC\\localhost\\C$\\k.key"),
            ("\\\\?\\Volume{GUID} form", EXT + "Volume{01234567-89ab-cdef-0123-456789abcdef}\\k.key"),
            ("\\\\?\\GLOBALROOT form", EXT + "GLOBALROOT\\Device\\HarddiskVolume1\\k.key"),
            ("\\\\?\\ with a bare drive and no slash", EXT + "C:k.key"),
            ("\\\\.\\ device that is not a drive", DEV + "PhysicalDrive0"),
            ("\\\\?\\ with nothing after it", EXT),
        ):
            p = Path(raw)
            check(f"{label}: unclassified", keystore._unsafe(p) == "unclassified", str(keystore._unsafe(p)))
        check("\\\\?\\UNC\\localhost\\... through the key store: refused", outcome(Path(EXT + "UNC\\localhost\\C$\\k.key")) == "unsafe_location")

        for label, raw in (
            ("UNC to localhost", "\\\\localhost\\C$\\" + repo_s[3:] + "\\k.key"),
            ("UNC share", "\\\\localhost\\share\\k.key"),
            ("UNC with a loopback address", "\\\\127.0.0.1\\C$\\k.key"),
            ("UNC with forward slashes", "//localhost/share/k.key"),
        ):
            p = Path(raw)
            check(f"{label}: unclassified", keystore._unsafe(p) == "unclassified", str(keystore._unsafe(p)))
        check("plain UNC through the key store: refused", outcome(Path("\\\\localhost\\C$\\" + repo_s[3:] + "\\k.key")) == "unsafe_location")
        check("...and the repository was not written through the share", not (s.repo / "k.key").exists())

        # an extended-length spelling of an ordinary path is still fine
        ordinary = EXT + str(s.t / "keys-ext") + "\\k.key"
        r = outcome(Path(ordinary))
        check("extended-length form of an ordinary temp path is allowed", r == "ok", r)
        check("...and the key is made in that folder", (s.t / "keys-ext" / "k.key").is_file())
        keystore._forget()
        check("...and reloads", outcome(Path(ordinary)) == "ok")
        check("the plain spelling of it is the same key",
              mock_unprotect((s.t / "keys-ext" / "k.key").read_bytes()) == keystore._master())

        # NUL
        for label, p in (
            ("NUL in the file name", Path(str(s.t / "keys") + "\\k\x00.key")),
            ("NUL in a folder name", Path(str(s.t) + "\\bad\x00dir\\k.key")),
            ("NUL at the end", Path(str(s.t / "keys") + "\\k.key\x00")),
        ):
            check(f"{label}: unclassified", keystore._unsafe(p) == "unclassified", str(keystore._unsafe(p)))
            res = outcome(p)
            check(f"{label}: unsafe_location, no ValueError escapes", res == "unsafe_location", res)
            res = reason_of(keystore.derive, "objective-fingerprint")
            check(f"{label}: derive reports it the same way", res == "unsafe_location", str(res))
            check(f"{label}: nothing was created", not (s.t / "keys").exists() and not (s.t / "bad").exists())

        # _create called directly validates first
        for label, p in (
            ("repository", s.repo / "new" / "deep" / "k.key"),
            ("data/", s.data / "new" / "k.key"),
            ("extended-length repository", Path(EXT + repo_s + "\\new2\\k")),
            ("NUL", Path(str(s.t / "nul-dir") + "\\k\x00")),
        ):
            before = tree(s.t)
            with FsSpy() as spy:
                try:
                    keystore._create(p)
                    res = "created"
                except keystore.KeyUnavailable as exc:
                    res = exc.reason
                except Exception as exc:  # noqa: BLE001
                    res = f"raised {type(exc).__name__}"
            bad = [c for c in spy.calls if c[0] in WRITEY or (c[0] == "open" and any(m in c[2] for m in "wxa+"))]
            bad = [c for c in bad if under(c[1], s.t)]
            check(f"_create({label}) directly: refused as unsafe_location", res == "unsafe_location", res)
            check(f"_create({label}) directly: no folder, temp file or write", not bad, str(bad))
            check(f"_create({label}) directly: the tree is unchanged", tree(s.t) == before)
        od2 = s.t / "od-direct"
        od2.mkdir()
        os.environ["OneDrive"] = str(od2)
        try:
            keystore._create(od2 / "FRIDAY" / "k.key")
            res = "created"
        except keystore.KeyUnavailable as exc:
            res = exc.reason
        check("_create(synced) directly: refused and no folder", res == "unsafe_location" and files_in(od2) == [], f"{res} {files_in(od2)}")
        os.environ.pop("OneDrive")
        good = keystore._create(s.t / "direct-ok" / "k.key")
        check("_create(safe path) directly still works", len(good) == keystore.KEY_BYTES and (s.t / "direct-ok" / "k.key").is_file())

    # ------------------------------------------------------------------ L
    scenario("L  validation runs on every call, including cache hits")
    with sandbox() as s:
        p = s.keys / "k.key"
        check("a key is made and cached at a safe path", attempt(p) is None and len(keystore._cache) == 1)
        good = keystore._master()
        blob, mt = p.read_bytes(), p.stat().st_mtime_ns
        check("a repeat call returns it from the cache", keystore._master() == good)

        paths.ROOT = s.t  # the repository now "covers" the cached key's folder
        check("repository root changed to cover the path: next call refused", reason_of(keystore._master) == "unsafe_location")
        check("...derive refused too", reason_of(keystore.derive, "objective-fingerprint") == "unsafe_location")
        check("...the cache did not hand out the key", keystore._cache != {} and reason_of(keystore._master) == "unsafe_location")
        check("...and the key file is untouched", (p.read_bytes(), p.stat().st_mtime_ns) == (blob, mt))
        paths.ROOT = s.repo
        check("restoring the root brings the SAME key back", keystore._master() == good)

        paths.DATA = s.keys  # data/ moved outside the repository, onto the cached key's folder
        check("data/ redirected onto the path: next call refused", reason_of(keystore._master) == "unsafe_location")
        paths.DATA = s.data
        check("...and allowed again after restoring", keystore._master() == good)

        os.environ["OneDrive"] = str(s.keys)
        check("a OneDrive variable now covering the path: next call refused", reason_of(keystore._master) == "unsafe_location")
        check("...categorised synced", keystore._unsafe(p) == "synced")
        os.environ.pop("OneDrive")
        check("...and allowed again after removing it", keystore._master() == good)

        keystore._onedrive_registry_roots = lambda: [str(s.keys)]
        check("a registry-listed root now covering the path: next call refused", reason_of(keystore._master) == "unsafe_location")
        keystore._onedrive_registry_roots = lambda: []
        check("...and allowed again", keystore._master() == good)

        write_dropbox(s.appdata, personal={"path": str(s.keys)})
        check("a Dropbox config now covering the path: next call refused", reason_of(keystore._master) == "unsafe_location")
        write_dropbox(s.appdata, personal={"path": str(s.t / "elsewhere")})
        check("...and allowed again", keystore._master() == good)

        def broken() -> list[str]:
            raise keystore._Unclassified

        keystore._onedrive_registry_roots = broken
        check("a location list that becomes unreadable: next call refused", reason_of(keystore._master) == "unsafe_location")
        keystore._onedrive_registry_roots = lambda: []
        check("...and allowed again", keystore._master() == good)
        check("through all of it the key file never changed", (p.read_bytes(), p.stat().st_mtime_ns) == (blob, mt))

        with FsSpy() as spy:
            paths.ROOT = s.t
            r = reason_of(keystore._master)
            paths.ROOT = s.repo
        check("a refused cache hit touches nothing in the key's folder",
              r == "unsafe_location" and not [c for c in spy.calls if under(c[1], p.parent)], str(spy.calls))

    # ------------------------------------------------------------------ M
    scenario("M  the real key path was never touched")
    real_key_after = [(p, p.exists()) for p in dbplace.real_key_paths()]
    check("the user's real key path was never created", real_key_before == real_key_after, f"{real_key_before} -> {real_key_after}")
    check("paths.ROOT / paths.DATA are restored", paths.ROOT == real_root and paths.DATA == real_data)
    check("the key-file setting is restored", paths.KEY_FILE == real_key_file)
    check("the genuine DPAPI functions and registry reader are restored",
          keystore._unprotect is not mock_unprotect and keystore._onedrive_registry_roots is REAL_REGISTRY_ROOTS)

    if SKIPPED:
        print("\n  SKIPPED (not counted as passes):")
        for line in SKIPPED:
            print(f"    - {line}")

    return finish("Phase 88 key paths", time.time() - t0, min_assertions=280, min_scenarios=13)


if __name__ == "__main__":
    sys.exit(main())
