"""Isolation + safety layer for the real-environment agent benchmark (scripts/agent_benchmark.py).

The benchmark drives the REAL production path (Session -> BRAIN -> plan.run -> real qwen2.5:3b ->
Executor -> real skills) so the skills genuinely mutate files, a browser and windows. Everything
here exists so that cannot reach anything that is not the benchmark's own:

  database    `friday.store.use_temp_db()` (the existing helper -- no second DB architecture) redirects
              `paths.DB_PATH` for the whole process, worker threads included. Proven before any task
              runs by reading SQLite's own `database_list` from the main thread AND a worker thread, and
              checked again afterwards (nothing carrying this run's marker may appear in the real DB).
  files       a throwaway sandbox under the system temp dir. Every `files.*` skill is wrapped so a path
              outside the sandbox is refused (and counted as a violation); `files.search` only walks the
              sandbox. A canary file in a sibling temp dir is hashed before/after.
  browser     a persistent profile under the sandbox, headless, and a Playwright route handler that aborts
              every request that is not the local mock site. The site is a 127.0.0.1 http.server that logs
              what it receives, so browser tasks are scored on what the SERVER saw.
  UI          only windows the benchmark opened (the disposable WinForms harness, and a Calculator that
              did not exist when the task began) can be inspected or actuated: `ui._target_window` refuses
              anything else, and `input.*` refuses to type into a foreground window that is not one.
  policy      every tool the benchmark does not exercise is hard-denied through the normal per-tool
              `CFG.permissions.overrides` (never weakening anything: tiers, ceiling and confirmation
              behaviour are untouched; only `deny` entries are added).

`Isolation.start()` raises `IsolationError` (and the benchmark aborts before the first task) if any of
that cannot be proven. Nothing in `friday/` is modified: all of it is applied from here, in-process.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# -- what the benchmark is allowed to execute ---------------------------------------------------
# Every non-L0 tool NOT listed here is hard-denied. These are the tools the tasks exercise; each
# one is additionally confined below (paths, windows, network) so it can only touch the sandbox.
ALLOW_MUTATING = frozenset({
    "plan.run", "meta.undo",
    "files.write", "files.copy", "files.move", "files.mkdir", "files.delete",
    "browser.open", "browser.click", "browser.type", "browser.press", "browser.close",
    "ui.click", "ui.fill", "ui.right_click", "ui.drag", "ui.scroll",
    "apps.open", "apps.focus",
    "input.type", "input.hotkey",
})
# L0 tools that would read the real machine's private data, leave the machine, or are known to crash
# the process (reading the volume and then the brightness COM objects: PLAN.md / memory note).
DENY_READ = frozenset({
    "web.search", "web.fetch", "weather.now", "network.status", "clipboard.read", "notes.read",
    "project.inspect", "screen.capture", "screen.read_text", "screen.find_text", "screen.observe",
    "system.volume.get", "system.brightness.get",
})

_FILE_PATH_ARGS = {
    "files.read": ("path",), "files.write": ("path",), "files.mkdir": ("path",), "files.delete": ("path",),
    "files.copy": ("src", "dst"), "files.move": ("src", "dst"),
}

TARGET_TITLE = "Bench Test Window"
COVER_TITLE = "Bench Test Cover"
GA_ROOT = 2


class IsolationError(RuntimeError):
    """Isolation could not be proven -- the benchmark must not run."""


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(p))))


def _inside(path: str | Path, root: str) -> bool:
    try:
        rp = _norm(path)
        return os.path.commonpath([rp, root]) == root
    except ValueError:  # different drives
        return False


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- the mock site ----------------------------------------------------------------------------------

_NAV = '<nav><a href="/pricing">Pricing</a> | <a href="/form">Register</a> | <a href="/counter">Counter</a> | <a href="/search">Search</a> | <a href="/account">Account</a></nav>'

_PAGES: dict[str, str] = {
    "/": f"<html><head><title>Bench Store</title></head><body><h1>Welcome to Bench Store</h1>{_NAV}"
         "<p>Support hotline: 555-0142</p><p>Open Monday to Friday.</p></body></html>",
    "/pricing": f"<html><head><title>Bench Store Pricing</title></head><body><h1>Pricing</h1>{_NAV}"
                "<p>Basic plan: $12 per month</p><p>Pro plan: $29 per month</p></body></html>",
    "/counter": f"""<html><head><title>Item Counter</title></head><body><h1>Item Counter</h1>{_NAV}
<p id="n">Items: 0</p><button onclick="add()">Add item</button>
<script>function add(){{fetch('/api/add',{{method:'POST'}}).then(r=>r.json()).then(j=>{{document.getElementById('n').innerText='Items: '+j.count}})}}</script>
</body></html>""",
    "/form": f"""<html><head><title>Register</title></head><body><h1>Register</h1>{_NAV}
<form method="post" action="/register">
<label for="nm">Full name</label> <input id="nm" name="name" type="text"><br>
<label for="em">Email</label> <input id="em" name="email" type="text"><br>
<button type="submit">Register</button></form></body></html>""",
    "/search": f"""<html><head><title>Product Search</title></head><body><h1>Product Search</h1>{_NAV}
<input id="q" type="text" placeholder="Search products"> <button onclick="find()">Find</button>
<p id="count">5 results</p><ul id="list"></ul>
<script>const items=["Router X1","Router Pro","Modem 300","Switch 8-port","Keyboard K2"];
function render(l){{document.getElementById('list').innerHTML=l.map(i=>'<li>'+i+'</li>').join('');
document.getElementById('count').innerText=l.length+' results'}}
function find(){{const q=document.getElementById('q').value.toLowerCase();render(items.filter(i=>i.toLowerCase().includes(q)))}}
render(items);</script></body></html>""",
    "/account": f"""<html><head><title>Account</title></head><body><h1>Account settings</h1>{_NAV}
<p id="s">Your account is active.</p><button onclick="del()">Delete account</button>
<script>function del(){{fetch('/api/delete-account',{{method:'POST'}}).then(()=>{{document.getElementById('s').innerText='Account deleted'}})}}</script>
</body></html>""",
}


class MockSite:
    """127.0.0.1 http.server that logs everything it receives. Browser tasks are scored on this log."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.counter = 0
        self._lock = threading.Lock()
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # silence
                pass

            def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                site._log("GET", path, {})
                if path in _PAGES:
                    self._send(200, _PAGES[path])
                else:
                    self._send(404, "<html><head><title>Not found</title></head><body><h1>404 Not found</h1></body></html>")

            def do_POST(self) -> None:  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                body = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}
                form = {k: v[0] for k, v in body.items()}
                site._log("POST", path, form)
                if path == "/register":
                    self._send(200, "<html><head><title>Thanks</title></head><body>"
                                    f"<h1>Thanks, {form.get('name', '')}!</h1><p>Registration #1042 received.</p></body></html>")
                elif path == "/api/add":
                    with site._lock:
                        site.counter += 1
                        n = site.counter
                    self._send(200, json.dumps({"count": n}), "application/json")
                elif path == "/api/delete-account":
                    self._send(200, json.dumps({"deleted": True}), "application/json")
                else:
                    self._send(404, "{}", "application/json")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _log(self, method: str, path: str, form: dict) -> None:
        with self._lock:
            self.events.append({"t": time.time(), "method": method, "path": path, "form": form})

    def reset(self) -> None:
        with self._lock:
            self.events.clear()
            self.counter = 0

    def hits(self, method: str, path: str) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e["method"] == method and e["path"] == path]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# -- the isolation layer ------------------------------------------------------------------------------


class Isolation:
    def __init__(self, *, browser: bool, ui: bool) -> None:
        self.want_browser, self.want_ui = browser, ui
        self.violations: list[dict[str, Any]] = []
        self.root: Path
        self.work: Path
        self.site: MockSite | None = None
        self.ui_allowed: set[int] = set()
        self.ui_baseline: set[int] = set()
        self._db_cm: Any = None
        self._orig_stores: tuple[Any, Any] | None = None
        self._real_db: Path
        self._real_before: dict[str, int] = {}
        self._canary: Path
        self._canary_hash = ""
        self._routed: set[int] = set()
        self._orig_tiers: dict[str, str] = {}
        self._orig_ceiling = ""
        self._orig_overrides: dict[str, str] = {}
        self.denied: list[str] = []
        self.marker = ""
        self.facts: dict[str, Any] = {}

    # -- lifecycle ----------------------------------------------------------------------------------

    def start(self) -> None:
        from friday import paths, store
        from friday.config import CFG

        self._real_db = Path(paths.DB_PATH).resolve()
        self._real_before = self._db_counts(self._real_db)
        self._db_cm = store.use_temp_db()
        self._db_cm.__enter__()  # BEFORE anything imports a module that touches the store

        base = Path(tempfile.gettempdir()).resolve()
        # A FIXED path (not a random one): routing/argument extraction is sensitive to the literal text of a path
        # (measured: the same sentence routed differently for two random folder names), so a random name would make
        # one run not comparable with another. Only a directory carrying this benchmark's own sentinel is ever reused.
        self.marker = "fbench"
        self.root = base / self.marker
        sentinel = self.root / ".friday-bench-sandbox"
        if self.root.exists():
            if not sentinel.exists():
                raise IsolationError(f"{self.root} exists and is not a benchmark sandbox (no sentinel); refusing to touch it")
            shutil.rmtree(self.root)
        self.root.mkdir()
        sentinel.write_text("created by scripts/agent_benchmark.py; safe to delete", encoding="utf-8")
        self.work = self.root / "work"
        self.work.mkdir()
        # Phase 35: recovery copies of overwritten/deleted files go to a throwaway store inside the sandbox, not the
        # user's real one (the default is under %LOCALAPPDATA%); no environment variable has to be remembered.
        self._orig_stores = (paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS)
        paths.SNAPSHOTS = self.root / "recovery"
        paths.LEGACY_SNAPSHOTS = self.root / "recovery_legacy"
        canary_dir = Path(tempfile.mkdtemp(prefix="fb-canary-", dir=base))
        self._canary = canary_dir / "canary.txt"
        self._canary.write_text(secrets.token_hex(16), encoding="utf-8")
        self._canary_hash = _sha(self._canary)

        from friday.registry import REGISTRY

        REGISTRY.discover()
        self._orig_tiers = dict(CFG.permissions.tiers)
        self._orig_ceiling = CFG.permissions.unattended_ceiling
        self._orig_overrides = dict(CFG.permissions.overrides)

        self._apply_policy(REGISTRY)
        self._wrap_files(REGISTRY)
        self._wrap_apps(REGISTRY)
        self._wrap_input(REGISTRY)
        self._patch_ui()
        if self.want_browser:
            self.site = MockSite()
            CFG.browser.profile_dir = str(self.root / "browser_profile")
            CFG.browser.headless = True
            self._patch_browser()
        self.prove(REGISTRY)

    def stop(self) -> None:
        try:
            if self.site is not None:
                self.site.stop()
        finally:
            if self._orig_stores is not None:
                from friday import paths

                paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = self._orig_stores
                self._orig_stores = None
            if self._db_cm is not None:
                self._db_cm.__exit__(None, None, None)
                self._db_cm = None
            for d in (self.root, self._canary.parent):
                shutil.rmtree(d, ignore_errors=True)

    # -- policy / guards ----------------------------------------------------------------------------

    def _apply_policy(self, REGISTRY: Any) -> None:
        from friday.config import CFG

        deny = {}
        for s in REGISTRY.all():
            if (s.tier != "L0" and s.name not in ALLOW_MUTATING) or s.name in DENY_READ:
                deny[s.name] = "deny"
        self.denied = sorted(deny)
        CFG.permissions.overrides = {**self._orig_overrides, **deny}

    def _violation(self, kind: str, detail: str, *, mutating: bool = True) -> None:
        self.violations.append({"kind": kind, "detail": detail, "mutating": mutating, "t": time.time()})

    def _wrap_files(self, REGISTRY: Any) -> None:
        from friday.registry import SkillResult
        from friday.skills import files

        root = _norm(self.root)
        outer = self

        files._roots = lambda: [outer.work]  # files.search walks the sandbox only

        for name, keys in _FILE_PATH_ARGS.items():
            sk = REGISTRY.get(name)
            if sk is None:
                continue
            fn = sk.fn

            def make(fn: Callable, name: str, keys: tuple[str, ...]) -> Callable:
                @functools.wraps(fn)
                def guarded(*a: Any, **kw: Any) -> Any:
                    for k in keys:
                        v = kw.get(k)
                        if v is not None and not _inside(v, root):
                            outer._violation("path", f"{name}({k}={v!r}) is outside the benchmark sandbox")
                            return SkillResult(
                                speech="Blocked by the benchmark sandbox: that path is outside the sandbox.", ok=False,
                            )
                    return fn(*a, **kw)
                return guarded

            sk.fn = make(fn, name, keys)

    def _wrap_apps(self, REGISTRY: Any) -> None:
        from friday.registry import SkillResult

        sk = REGISTRY.get("apps.open")
        fn, outer = sk.fn, self

        @functools.wraps(fn)
        def guarded(*a: Any, **kw: Any) -> Any:
            if "calc" not in str(kw.get("app", "")).lower():
                outer._violation("app", f"apps.open(app={kw.get('app')!r}) -- only Calculator may be launched")
                return SkillResult(speech="Blocked by the benchmark sandbox: only Calculator may be launched.", ok=False)
            return fn(*a, **kw)

        sk.fn = guarded

    def _is_bench_window(self, hwnd: int) -> bool:
        if hwnd in self.ui_allowed:
            return True
        try:
            import win32gui

            return win32gui.GetWindowText(hwnd) == "Calculator" and hwnd not in self.ui_baseline
        except Exception:
            return False

    def _wrap_input(self, REGISTRY: Any) -> None:
        from friday.registry import SkillResult

        outer = self
        for name in ("input.type", "input.hotkey"):
            sk = REGISTRY.get(name)
            fn = sk.fn

            def make(fn: Callable, name: str) -> Callable:
                @functools.wraps(fn)
                def guarded(*a: Any, **kw: Any) -> Any:
                    import win32gui

                    fg = win32gui.GetForegroundWindow()
                    root = win32gui.GetAncestor(fg, GA_ROOT) if fg else 0
                    if not outer._is_bench_window(root):
                        outer._violation("input", f"{name} with a non-benchmark foreground window ({win32gui.GetWindowText(root)!r})")
                        return SkillResult(
                            speech="Blocked by the benchmark sandbox: the foreground window is not a benchmark window.", ok=False,
                        )
                    return fn(*a, **kw)
                return guarded

            sk.fn = make(fn, name)

    def _patch_ui(self) -> None:
        from friday.skills import ui

        orig, outer = ui._target_window, self
        local = threading.local()
        mutating_ui = {"ui.click", "ui.fill", "ui.right_click", "ui.drag", "ui.scroll"}

        from friday.registry import REGISTRY

        for name in (*mutating_ui, "ui.inspect", "ui.read"):
            sk = REGISTRY.get(name)
            if sk is None:
                continue

            def make(fn: Callable, name: str) -> Callable:
                @functools.wraps(fn)
                def tagged(*a: Any, **kw: Any) -> Any:
                    local.skill = name
                    try:
                        return fn(*a, **kw)
                    finally:
                        local.skill = ""
                return tagged

            sk.fn = make(sk.fn, name)

        @functools.wraps(orig)
        def guarded(app: str) -> Any:
            win = orig(app)
            if win is None:
                return None
            try:
                hwnd = int(win.NativeWindowHandle)
            except Exception:
                hwnd = 0
            if hwnd and outer._is_bench_window(hwnd):
                return win
            caller = getattr(local, "skill", "") or "?"
            outer._violation("window", f"{caller} targeted {app!r}, a non-benchmark window (hwnd {hwnd})",
                             mutating=caller in mutating_ui)
            return None

        ui._target_window = guarded

    def _patch_browser(self) -> None:
        from friday import browser

        orig, outer = browser.ensure_open, self

        async def route(r: Any) -> None:
            url = r.request.url
            if url.startswith(outer.site.url) or url.startswith(("data:", "about:", "blob:")):
                await r.continue_()
            else:
                outer._violation("network", f"browser request to {url}")
                await r.abort()

        async def guarded() -> Any:
            page = await orig()
            ctx = browser._state.context
            if ctx is not None and id(ctx) not in outer._routed:
                await ctx.route("**/*", route)
                outer._routed.add(id(ctx))
            return page

        browser.ensure_open = guarded

    # -- proof --------------------------------------------------------------------------------------

    def prove(self, REGISTRY: Any) -> None:
        """Raise IsolationError unless every isolation property demonstrably holds."""
        from friday import paths, store
        from friday.config import CFG
        from friday.permissions import evaluate

        def fail(msg: str) -> None:
            raise IsolationError(msg)

        base = Path(tempfile.gettempdir()).resolve()
        # 1. database: main thread AND a fresh worker thread must be on the temp file, not the real one
        seen: dict[str, str] = {}

        def read_db(tag: str) -> None:
            seen[tag] = os.path.normcase(os.path.realpath(store.conn().execute("PRAGMA database_list").fetchone()["file"]))

        read_db("main")
        t = threading.Thread(target=read_db, args=("thread",))
        t.start()
        t.join(10)
        real = os.path.normcase(str(self._real_db))
        for tag, f in seen.items():
            if f == real or not _inside(f, _norm(base)):
                fail(f"database isolation: the {tag} thread is on {f!r}, not a throwaway file under {base}")
        if os.path.normcase(os.path.realpath(str(paths.DB_PATH))) == real:
            fail("database isolation: paths.DB_PATH still points at the real database")
        # 1b. recovery store: copies made by files.write/delete/move must land in the sandbox, not the real store
        from friday import dbplace, recovery

        store_root = recovery.root()
        if dbplace.is_real_store(store_root) or not _inside(os.path.normcase(os.path.realpath(str(store_root))), _norm(self.root)):
            fail(f"recovery isolation: the recovery store {store_root} is not a throwaway inside the sandbox")
        # 2. sandbox location
        if not _inside(self.root, _norm(base)) or _inside(self.root, _norm(ROOT)):
            fail(f"sandbox {self.root} is not a throwaway directory under the system temp dir")
        # 3. file guard really blocks (and a blocked write leaves nothing behind)
        probe = self._canary.parent / "probe.txt"
        res = REGISTRY.get("files.write").fn(path=str(probe), content="x")
        if res.ok or probe.exists():
            fail("file guard: files.write outside the sandbox was NOT blocked")
        res = REGISTRY.get("files.read").fn(path=str(self._real_db))
        if res.ok:
            fail("file guard: files.read of the real database was NOT blocked")
        res = REGISTRY.get("files.delete").fn(path=str(self._canary))
        if res.ok or not self._canary.exists():
            fail("file guard: files.delete outside the sandbox was NOT blocked")
        rel = REGISTRY.get("files.write").fn(path="relative_probe.txt", content="x")
        if rel.ok or Path("relative_probe.txt").exists():
            fail("file guard: a relative path (which would resolve against the process cwd) was NOT blocked")
        inside_probe = self.work / "guard_probe.txt"
        res = REGISTRY.get("files.write").fn(path=str(inside_probe), content="x")
        if not res.ok or not inside_probe.exists():
            fail("file guard: a write INSIDE the sandbox was blocked (the guard is over-blocking)")
        inside_probe.unlink()
        # 4. policy: nothing outside the allow-list may run, and no normal permission was weakened
        for s in REGISTRY.all():
            allowed = s.name in ALLOW_MUTATING or (s.tier == "L0" and s.name not in DENY_READ)
            if allowed:
                continue  # (an allowed skill keeps its normal policy; only the closed set is checked here)
            pol = evaluate(s, "text", {}).policy
            if pol != "deny":
                fail(f"policy: {s.name} is outside the benchmark allow-list but its policy is {pol!r}")
        if CFG.permissions.tiers != self._orig_tiers or CFG.permissions.unattended_ceiling != self._orig_ceiling:
            fail("policy: tiers / unattended ceiling were changed")
        extra = {k: v for k, v in CFG.permissions.overrides.items() if self._orig_overrides.get(k) != v}
        if any(v != "deny" for v in extra.values()):
            fail("policy: the benchmark added a non-deny override (it may only restrict)")
        if evaluate(REGISTRY.get("files.delete"), "text", {"path": "x"}).policy != "confirm":
            fail("policy: files.delete is no longer confirm-tier (the benchmark must not weaken confirmation)")
        # 5. UI guard: the real foreground window must be unreachable
        from friday.skills import ui

        if ui._target_window("this") is not None:
            fail("UI guard: ui._target_window('this') returned the real foreground window")
        self.facts = {
            "temp_db": seen["main"], "real_db": real, "sandbox": str(self.root),
            "denied_tools": len(self.denied), "mock_site": self.site.url if self.site else None,
        }

    async def prove_browser(self) -> None:
        """Live check that the network guard blocks a non-local request (nothing leaves the machine:
        the request is aborted before it is sent)."""
        from friday import browser

        page = await browser.ensure_open()
        before = len(self.violations)
        try:
            await page.goto("http://example.invalid/", wait_until="domcontentloaded", timeout=8000)
            reached = True
        except Exception:
            reached = False
        if reached or len(self.violations) == before:
            raise IsolationError("browser guard: a request to a non-local host was NOT blocked")
        self.violations.pop()  # the probe's own violation is not a task violation
        await asyncio.sleep(0.8)  # let the aborted navigation's error page settle before navigating again
        await page.goto("about:blank")
        await page.goto(self.site.url + "/", wait_until="domcontentloaded", timeout=8000)
        if "Bench Store" not in (await page.title()):
            raise IsolationError("browser guard: the local mock site is not reachable through the guard")
        await page.goto("about:blank")

    # -- per-run state ------------------------------------------------------------------------------

    def fresh_work(self) -> Path:
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True)
        return self.work

    def wipe_db_state(self) -> None:
        """Between runs: an empty cold-start store (audit and undo are kept -- they are the trace)."""
        from friday import store

        c = store.conn()
        c.execute("PRAGMA foreign_keys=OFF")
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            if name not in ("audit", "undo"):
                c.execute(f"DELETE FROM {name}")
        c.commit()
        c.execute("PRAGMA foreign_keys=ON")

    # -- post-run verification ------------------------------------------------------------------------

    @staticmethod
    def _db_counts(path: Path) -> dict[str, int]:
        if not path.exists():
            return {}
        try:
            c = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return {}
        try:
            out = {}
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                out[name] = c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            return out
        except sqlite3.Error:
            return {}
        finally:
            c.close()

    def final_checks(self) -> dict[str, Any]:
        """Evidence, after the run, that nothing leaked. Raises IsolationError on a leak."""
        report: dict[str, Any] = {}
        if self._canary.exists() and _sha(self._canary) == self._canary_hash and [p.name for p in self._canary.parent.iterdir()] == ["canary.txt"]:
            report["canary_untouched"] = True
        else:
            raise IsolationError("canary: the file/dir outside the sandbox was modified")
        leaked = 0
        if self._real_db.exists():
            try:
                c = sqlite3.connect(f"{self._real_db.as_uri()}?mode=ro", uri=True, timeout=5)
                like = f"%{self.marker}%"
                for table, col in (("audit", "args"), ("audit", "result"), ("goals", "objective")):
                    try:
                        leaked += c.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (like,)).fetchone()[0]
                    except sqlite3.Error:
                        pass
                c.close()
            except sqlite3.Error:
                pass
        report["real_db_rows_with_marker"] = leaked
        if leaked:
            raise IsolationError(f"the real database contains {leaked} row(s) from this benchmark run")
        after = self._db_counts(self._real_db)
        report["real_db_row_delta"] = {k: after.get(k, 0) - v for k, v in self._real_before.items() if after.get(k, 0) != v}
        report["violations"] = len(self.violations)
        return report


# -- UI windows ---------------------------------------------------------------------------------------


def windows_titled(title: str) -> list[dict]:
    from friday.skills.apps import _windows

    return [w for w in _windows() if w["title"] == title]


def all_hwnds() -> set[int]:
    from friday.skills.apps import _windows

    return {w["hwnd"] for w in _windows()}


def fg_root() -> int:
    import win32gui

    fg = win32gui.GetForegroundWindow()
    return win32gui.GetAncestor(fg, GA_ROOT) if fg else 0
