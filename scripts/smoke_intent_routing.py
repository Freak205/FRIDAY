"""Phase 13.0 — robust intent & goal routing.

Maps and regression-tests the actual routing pipeline
(`friday.brain.engine.Brain.understand` -> `friday.session.Session.handle`'s
contextual-reference recovery -> `friday.brain.engine.route_with_resolved_entity`
-> slot extraction -> `EXECUTOR`), with a focus on the documented Phase 12.0
weakness this phase fixes: once contextual resolution replaces a phrase like
"the file"/"it" with a resolved value, the embedding matcher used to be able
to reinterpret the *substituted* text as an unrelated skill (confirmed
empirically: "Read <path>." landed on `knowledge.index`, and a bare "Read
it." with a file in context landed on `ui.read`, silently reading whatever
window happened to be in the foreground instead of the file just opened).

Same `check`/`OK`/`MISS` print convention as every other `scripts/smoke_*.py`
— no pytest. Never touches a real browser/WhatsApp session; file operations
run against a harness-owned temp directory (`isolated_file_root`, copied
from `scripts/agent_reliability.py`, per that file's own "not reinvented"
convention).

A few required scenarios in PLAN.md Phase 13.0's brief don't correspond to
an actual registered FRIDAY skill (there is no "search a contact by name"
skill) — adapted to the smallest deterministic equivalent this codebase
actually supports, exactly as the brief's own §2 allows.

Phase 14.0 update: two routing gaps this file originally recorded as
NOTE-only known limitations — "focus VS Code" (section 2) and free-form
compound goals like "open Chrome and search YouTube for cats" not reaching
plan.run (section 5) — are fixed in that phase (friday.brain.engine
._prefer_known_focus, friday.session.Session._maybe_route_to_plan) and
their checks upgraded from note() to check() here accordingly. See
PLAN.md Phase 14.0 for the fix; this file's own history of what was and
wasn't fixed at each phase is left in the section comments below.
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.brain import BRAIN  # noqa: E402
from friday.brain import engine as brain_engine  # noqa: E402
from friday.brain.engine import Action, Understanding  # noqa: E402
from friday.intelligence import context_memory, context_resolver  # noqa: E402
from friday.intelligence.context_memory import CONTEXT  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

overall = True


def check(label: str, ok: bool, detail: str = "") -> bool:
    global overall
    overall &= ok
    suffix = f" -> {detail}" if detail else ""
    print(f"  {'OK  ' if ok else 'MISS'} {label}{suffix}")
    return ok


def note(label: str, detail: str = "") -> None:
    """A recorded observation that is NOT part of the pass/fail gate — used
    for known, pre-existing, out-of-scope findings (see §KNOWN LIMITATIONS)
    so this file never silently hides them, but also never fails the suite
    over something this phase was explicitly told not to fix."""
    suffix = f" -> {detail}" if detail else ""
    print(f"  NOTE {label}{suffix}")


def reset_session() -> None:
    SESSION.pending = None
    SESSION.last_skill = None
    SESSION.last_args = {}
    CONTEXT.reset()
    INTEL.reset()


# -- routing trace (brief §15) -------------------------------------------------


def routing_trace(utterance: str, u: Understanding, *, context: str = "") -> None:
    """Bounded, non-secret debug representation of one routing decision —
    disabled/noise-free in normal operation (this script is opt-in, run by
    hand or in CI, never on the hot path), printed only for a handful of
    illustrative cases below rather than every assertion."""
    print("  ROUTING TRACE")
    print(f"    input: {utterance!r}")
    print(f"    normalized: {u.normalized!r}")
    if context:
        print(f"    context: {context}")
    for c in u.candidates[:5]:
        print(f"    candidate: {c.skill} ({c.score:.2f})")
    print(f"    decision: {u.skill or '(none)'}")
    print(f"    action: {u.action.value}")
    print(f"    confidence: {u.score:.2f}" if u.score else "    confidence: n/a")


# -- shared scaffolding (copied from scripts/agent_reliability.py, per that --
# -- file's own "not reinvented" convention) -----------------------------------


@contextlib.contextmanager
def isolated_file_root(files: dict[str, str]):
    """A harness-owned temp dir as the *only* root files.search walks, and a
    tracking stub in place of files.reveal's real `subprocess.Popen(explorer
    ...)` call — no real Explorer window ever opens."""
    import friday.skills.files as files_mod

    tmp = tempfile.mkdtemp(prefix="friday-routing-")
    for name, content in files.items():
        (Path(tmp) / name).write_text(content, encoding="utf-8")

    orig_roots = files_mod._roots
    files_mod._roots = lambda: [Path(tmp)]
    reveal_calls: list[str] = []
    orig_popen = subprocess.Popen

    def fake_popen(args, *a, **kw):
        reveal_calls.append(str(args[-1]) if isinstance(args, (list, tuple)) else str(args))
        return None

    subprocess.Popen = fake_popen
    try:
        yield tmp, reveal_calls
    finally:
        files_mod._roots = orig_roots
        subprocess.Popen = orig_popen


async def always_confirm(skill_obj, args, preview: str) -> bool:
    return True


async def always_decline(skill_obj, args, preview: str) -> bool:
    return False


# -- fakes for the Win32 layer (copied from scripts/agent_reliability.py, --
# -- itself copied from scripts/smoke_desktop_observer.py) -------------------


class FakeWin32Gui:
    def __init__(self, windows: list[dict]) -> None:
        self.windows = windows
        self.foreground_hwnd = windows[0]["hwnd"] if windows else 0

    def GetForegroundWindow(self):
        return self.foreground_hwnd

    def GetWindowText(self, hwnd):
        return next((w["title"] for w in self.windows if w["hwnd"] == hwnd), "")

    def IsWindowVisible(self, hwnd):
        return next((w.get("visible", True) for w in self.windows if w["hwnd"] == hwnd), False)

    def EnumWindows(self, cb, extra):
        for w in self.windows:
            cb(w["hwnd"], extra)

    def IsIconic(self, hwnd):
        return False

    def ShowWindow(self, hwnd, cmd):
        return None

    def BringWindowToTop(self, hwnd):
        return None

    def SetForegroundWindow(self, hwnd):
        return None

    def PostMessage(self, hwnd, msg, wparam, lparam):
        return None


class FakeWin32Con:
    SW_RESTORE = 9
    WM_CLOSE = 0x0010


class FakeWin32Process:
    def __init__(self, pid_of_hwnd: dict[int, int]) -> None:
        self.pid_of_hwnd = pid_of_hwnd

    def GetWindowThreadProcessId(self, hwnd):
        return (0, self.pid_of_hwnd.get(hwnd, 0))

    def AttachThreadInput(self, a, b, c):
        return None


class _FakeProc:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self):
        return self._name


class FakePsutil:
    def __init__(self, name_of_pid: dict[int, str]) -> None:
        self.name_of_pid = name_of_pid

    def Process(self, pid):
        if pid in self.name_of_pid:
            return _FakeProc(self.name_of_pid[pid])
        raise RuntimeError(f"no such process {pid}")


class FakeWin32Api:
    def GetSystemMetrics(self, index):
        return {78: 1920, 79: 1080}.get(index, 0)

    def GetCurrentThreadId(self):
        return 1


@contextlib.contextmanager
def fake_win32(windows: list[dict]):
    """windows: [{"hwnd", "title", "process", "pid", "visible"?}, ...], first = foreground.
    Isolates any apps.*/ui.* skill under test from the real desktop — no
    real window is ever enumerated, focused, or sent WM_CLOSE."""
    pid_of_hwnd = {w["hwnd"]: w["pid"] for w in windows}
    name_of_pid = {w["pid"]: w["process"] for w in windows}
    saved = {name: sys.modules.get(name) for name in ("win32gui", "win32process", "psutil", "win32api", "win32con")}
    sys.modules["win32gui"] = FakeWin32Gui(windows)
    sys.modules["win32process"] = FakeWin32Process(pid_of_hwnd)
    sys.modules["psutil"] = FakePsutil(name_of_pid)
    sys.modules["win32api"] = FakeWin32Api()
    sys.modules["win32con"] = FakeWin32Con()
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


# ==============================================================================
# 1. DIRECT — fast, deterministic single-skill commands (brief §7/§13/§20)
# ==============================================================================


def section_direct() -> None:
    print("\n--- 1. DIRECT commands (must stay fast, no context involvement) ---\n")

    cases = [
        ("open Chrome", "apps.open"),
        ("open VS Code", "apps.open"),
        ("open Notepad", "apps.open"),
        ("what time is it", "system.time"),
        ("set a timer for ten minutes", "timer.set"),
    ]
    for text, expected in cases:
        t0 = time.perf_counter()
        u = BRAIN.understand(text)
        ms = (time.perf_counter() - t0) * 1000
        check(
            f"{text!r} -> {expected}",
            u.action is Action.ACT and u.skill == expected,
            f"{u.skill} ({u.score:.2f}, {ms:.1f}ms)",
        )

    # "read my screen" — routing must land on the screen-reading skill; the
    # skill's own OCR dependency being uninstalled on this machine is an
    # environment issue, not a routing defect, so only the route is checked.
    u = BRAIN.understand("read my screen")
    check("'read my screen' routes to screen.read_text", u.skill == "screen.read_text", u.skill or "")

    # None of the above utterances contain a reference word at all, so
    # Session._needs_context_resolution's cheap contains_reference() gate
    # must short-circuit before any context lookup — the fast path really
    # is still just one embedding match, unaffected by this phase's changes.
    check(
        "no reference word -> contextual gate short-circuits immediately",
        not context_resolver.contains_reference("open Chrome"),
    )


# ==============================================================================
# 2. KNOWN-APP ROUTING — apps.open must win over weak semantic matches
# ==============================================================================


def section_known_app() -> None:
    print("\n--- 2. KNOWN-APP routing (brief §13) ---\n")

    for text in ["open VS Code", "open vscode", "launch vs code", "open Chrome", "open Notepad"]:
        u = BRAIN.understand(text)
        check(f"{text!r} -> apps.open (not project.open)", u.skill == "apps.open", u.skill or "")

    # FIXED in Phase 14.0 (friday.brain.engine._prefer_known_focus): "focus
    # VS Code" used to land on input.type, a real, documented routing gap
    # (PLAN.md Phase 13.0 §7). The same real-app-verification tie-break
    # `_prefer_known_app` already used for "open" is now applied to
    # focus/switch-to/go-to phrasing.
    for text in ["focus VS Code", "focus Chrome", "focus Notepad", "switch to chrome"]:
        u = BRAIN.understand(text)
        check(f"{text!r} -> apps.focus", u.skill == "apps.focus", f"{u.skill} ({u.score:.2f})")


# ==============================================================================
# 3. CONTEXTUAL — the core Phase 13.0 regression class
# ==============================================================================


async def section_contextual() -> None:
    print("\n--- 3. CONTEXTUAL follow-ups (the Phase 13.0 fix) ---\n")

    # 3a. THE headline reproduction: "Open report.pdf." -> "Read it." must
    # land on files.read with the actual file's content, never silently
    # falling through to an unrelated skill (weather.now / knowledge.index /
    # ui.read reading the foreground window instead).
    reset_session()
    with isolated_file_root({"report.pdf": "Report body text for routing smoke."}) as (tmp, _reveal):
        path = str(Path(tmp) / "report.pdf")
        turn1 = await SESSION.handle(f"Reveal this file: {path}.", actor="test")
        check("turn1 'Reveal this file: <path>' -> files.reveal", SESSION.last_skill == "files.reveal" and turn1.ok)

        u_pre = BRAIN.understand("Read it.")
        note(
            "'Read it.' plain BRAIN.understand (no context)",
            f"-> {u_pre.action.value} {u_pre.skill} — a real ACT decision (ui.read, reading the "
            "foreground window) that the bare-argument-echo check in "
            "Session._needs_context_resolution must catch and reconsider",
        )

        turn2 = await SESSION.handle("Read it.", actor="test")
        check(
            "turn2 'Read it.' -> files.read, reads the actual file (not ui.read/weather.now/knowledge.index)",
            SESSION.last_skill == "files.read" and turn2.ok
            and "Report body text for routing smoke" in turn2.data.get("content", ""),
            f"skill={SESSION.last_skill} ok={turn2.ok}",
        )
        routing_trace("Read it.", Understanding(
            action=Action.ACT, utterance="Read it.", normalized="read it.",
            skill=SESSION.last_skill, score=1.0,
        ), context=f"file({Path(path).name})")

    # 3b. Contact reference: "Message Rahul..." -> "Text him saying ..."
    reset_session()
    import friday.whatsapp as wa_mod

    compose_calls: list[tuple[str, str]] = []

    async def fake_compose(contact: str, message: str) -> str:
        compose_calls.append((contact, message))
        return contact

    orig_compose = wa_mod.compose
    wa_mod.compose = fake_compose
    try:
        t1 = await SESSION.handle("Message Rahul on WhatsApp: just checking in.", actor="test")
        check("'Message Rahul...' -> whatsapp.compose", SESSION.last_skill == "whatsapp.compose" and t1.ok)
        t2 = await SESSION.handle("Text him saying the update is ready.", actor="test")
        check(
            "'Text him saying...' -> whatsapp.compose(contact=rahul), not an unrelated skill",
            SESSION.last_skill == "whatsapp.compose" and t2.ok
            and compose_calls[-1][0].lower() == "rahul",
            f"compose_calls={compose_calls}",
        )
    finally:
        wa_mod.compose = orig_compose

    # 3c. "Open report.pdf." -> "Do that again." — the temporal-repeat path
    # (friday.intelligence.context_resolver.resolve_temporal_repeat) is
    # dispatched entirely separately from BRAIN/context-entity resolution
    # and must be completely unaffected by this phase's changes.
    #
    # FIXED in Phase 14.0: this used to need a manual INTEL.record_action()
    # call here, because resolve_temporal_repeat() gates on
    # `friday.intelligence.state.INTEL.state.recent_actions`, but nothing in
    # the real running system called `INTEL.record_action(...)` — see
    # PLAN.md Phase 13.0 §7. Phase 14.0 wired it into
    # `friday.permissions.Executor.run` (the one real dispatch funnel), so a
    # genuinely fresh session's "do that again" now works from a plain
    # `SESSION.handle()` call alone, with no test-only assist.
    reset_session()
    with isolated_file_root({"report.pdf": "x"}) as (tmp, reveal_calls):
        path = str(Path(tmp) / "report.pdf")
        await SESSION.handle(f"Reveal this file: {path}.", actor="test")
        calls_before = len(reveal_calls)
        again = await SESSION.handle("Do that again.", actor="test")
        check(
            "'Open report.pdf.' -> 'Do that again.' re-runs files.reveal unchanged",
            SESSION.last_skill == "files.reveal" and again.ok and len(reveal_calls) == calls_before + 1,
        )

    # 3d. Two files opened in the SAME turn (shared turn_id, the way
    # friday/skills/plan.py's _remember_plan_entities groups a multi-step
    # goal's own steps) -> "Read it." must ask, not guess; the ambiguity
    # answer must still route correctly (this is the gap found and fixed
    # during this phase — see resolve_display_name).
    reset_session()
    with isolated_file_root({"report.pdf": "Report contents.", "summary.pdf": "Summary contents."}) as (tmp, _):
        p1, p2 = str(Path(tmp) / "report.pdf"), str(Path(tmp) / "summary.pdf")
        context_memory.record_from_skill("files.reveal", {"path": p1}, {"path": p1}, turn_id="same-turn")
        context_memory.record_from_skill("files.reveal", {"path": p2}, {"path": p2}, turn_id="same-turn")

        ambiguous = await SESSION.handle("Read it.", actor="test")
        check(
            "two files, same turn, 'Read it.' -> asks instead of guessing",
            not ambiguous.ok and SESSION.pending is not None and SESSION.pending.kind == "context_clarify",
            ambiguous.speech,
        )
        answered = await SESSION.handle("summary.pdf", actor="test")
        check(
            "answering 'summary.pdf' -> files.read reads the right file",
            SESSION.last_skill == "files.read" and answered.ok
            and "Summary contents" in answered.data.get("content", ""),
            f"skill={SESSION.last_skill} content={answered.data.get('content')!r}",
        )

    # 3e. No context at all -> "Read it." must ask/refuse, never guess a
    # random semantic skill (brief §9.E / §12).
    reset_session()
    none_ctx = await SESSION.handle("Read it.", actor="test")
    check(
        "no context, 'Read it.' -> clarification/refusal, not a guessed skill",
        not none_ctx.ok and SESSION.last_skill is None,
        none_ctx.speech,
    )


# ==============================================================================
# 4. CORRECTIONS — must still steer subsequent references (brief §10)
# ==============================================================================


async def section_corrections() -> None:
    print("\n--- 4. CORRECTIONS ---\n")

    reset_session()
    with isolated_file_root({"report.pdf": "Report contents.", "summary.pdf": "Summary contents."}) as (tmp, _):
        p1, p2 = str(Path(tmp) / "report.pdf"), str(Path(tmp) / "summary.pdf")
        await SESSION.handle(f"Reveal this file: {p1}.", actor="test")
        correction = await SESSION.handle(f"No, I meant {p2} instead.", actor="test")
        note("correction turn itself", f"-> {SESSION.last_skill} ok={correction.ok} (bookkeeping is what matters)")

        read = await SESSION.handle("Read it.", actor="test")
        check(
            "'No, I meant summary.pdf instead.' then 'Read it.' -> summary.pdf",
            SESSION.last_skill == "files.read" and read.ok
            and "Summary contents" in read.data.get("content", ""),
            f"skill={SESSION.last_skill} content={read.data.get('content')!r}",
        )


# ==============================================================================
# 5. MULTI-STEP — must still be possible to reach plan.run for genuine
#    multi-stage goal phrasings (brief §8); free-form compound goals now
#    also route correctly as of Phase 14.0 — see check() below.
# ==============================================================================


async def section_multi_step() -> None:
    print("\n--- 5. MULTI-STEP goals ---\n")

    # plan.run itself is still reachable and still wins for phrasing that
    # actually matches its own registered examples — unaffected by this
    # phase (no reference word in any of these, so Session._needs_context_
    # resolution's gate never engages).
    for text in ["handle this task end to end", "plan and carry out this task", "figure out how to do this and do it"]:
        u = BRAIN.understand(text)
        check(f"{text!r} -> plan.run", u.skill == "plan.run", u.skill or "")

    # FIXED in Phase 14.0 (friday.session.Session._maybe_route_to_plan):
    # PLAN.md Phase 13.0 §7 documented that free-form compound-goal
    # phrasing like "open X and search Y" confidently ACT/ASK_SLOT-matches
    # a *specific* single skill for just its first clause instead of
    # reaching plan.run, since friday.intelligence.goals.classify()/
    # looks_multi_step() existed but were never called from
    # friday.session. This phase wires a narrow, verified version of that
    # decision into Session.handle(): the cheap looks_decomposable() gate,
    # a near-exact-match guard (a verbatim/near-verbatim single-skill
    # phrasing — e.g. routine.create's own "...then shows the desktop"
    # example — must never be overridden), then real confirmation that one
    # of the two clauses, understood independently, names a confident,
    # genuinely different skill — never trusting the word "and"/"then"
    # alone. Tested through real SESSION.handle() end-to-end, not just
    # BRAIN.understand(), since the fix lives in Session, not the matcher.
    reset_session()
    for text in [
        "Open Chrome and search YouTube for cats.",
        "Find my report and open it.",
        "Open WhatsApp and message Rahul.",
    ]:
        reset_session()
        await SESSION.handle(text, actor="test")
        check(
            f"{text!r} -> routes to plan.run",
            SESSION.last_skill == "plan.run",
            f"last_skill={SESSION.last_skill}",
        )

    # Simple commands remain direct — never routed through plan.run just
    # because they're a few words long or share vocabulary with a
    # compound-sounding phrase.
    for text in ["Open Chrome.", "Focus VS Code.", "What time is it?", "Crank it up."]:
        reset_session()
        await SESSION.handle(text, actor="test")
        check(
            f"{text!r} -> stays direct (not plan.run)",
            SESSION.last_skill != "plan.run",
            f"last_skill={SESSION.last_skill}",
        )

    # A single skill whose own freeform argument happens to contain a
    # connector word ("...that mutes the volume THEN shows the desktop" is
    # one routine's own registered example, not two FRIDAY actions) must
    # not be split apart — the near-exact-match guard's reason for
    # existing. Checked at the routing-decision level (not a full
    # SESSION.handle(), which would actually create the routine) since only
    # the routing choice, not execution, is what this guards.
    routine_text = "make a routine named focus that mutes the volume then shows the desktop"
    routine_u = BRAIN.understand(routine_text)
    routine_routed = (
        SESSION._maybe_route_to_plan(routine_text, routine_u)
        if routine_u.action in (Action.ACT, Action.ASK_SLOT) else None
    )
    check(
        "a single skill's own registered example containing 'then' stays direct (not plan.run)",
        routine_u.skill == "routine.create" and routine_routed is None,
        f"understood={routine_u.skill} ({routine_u.score:.2f}), routed={routine_routed}",
    )


# ==============================================================================
# 6. ADVERSARIAL / SEMANTIC NEAR-MISS — intent must not be driven by
#    argument-shaped vocabulary alone (brief §6/§11)
# ==============================================================================


def section_adversarial() -> None:
    print("\n--- 6. ADVERSARIAL / semantic near-miss ---\n")

    # File-ish phrasings with no context established: must land somewhere
    # in the document domain (files.*/knowledge.*/screen.read_text — all
    # legitimate given no established referent), never in a wildly
    # unrelated, consequential domain (whatsapp.send, system.shutdown,
    # schedule.*, weather.now, ...).
    plausible_document_domain = {
        "files.read", "files.reveal", "files.search", "knowledge.index",
        "knowledge.ask", "knowledge.list", "screen.read_text", "clipboard.read",
        "web.download", "web.fetch",
    }
    for text in [
        "read the PDF", "read this document", "open the report", "check the file",
        "show me the document", "read what I opened", "open the file I mentioned",
    ]:
        u = BRAIN.understand(text)
        safe = u.action in (Action.UNKNOWN, Action.CLARIFY) or u.skill in plausible_document_domain
        check(f"{text!r} stays in the document domain (or asks)", safe, f"{u.action.value} {u.skill}")

    # Unrelated commands that happen to contain file-like vocabulary
    # ("report", "PDF") must route by their own real intent, not get pulled
    # into the file domain by that vocabulary.
    cases = [
        ("What's the weather for tomorrow?", "weather.now"),
        ("Set a timer for the report review.", "timer.set"),
        ("Search the web for PDF compression.", {"files.search", "web.search"}),
    ]
    for text, expected in cases:
        u = BRAIN.understand(text)
        ok = u.skill == expected if isinstance(expected, str) else u.skill in expected
        check(f"{text!r} routes by its own verb, not by file vocabulary", ok, u.skill or "")


# ==============================================================================
# 7. ENTITY-TYPE COMPATIBILITY — an app-context "close it" must route to
#    apps.close, not a file-shaped skill; a file-context "close it" must
#    not force apps.close either (brief §5).
# ==============================================================================


async def section_entity_compat() -> None:
    print("\n--- 7. ENTITY-TYPE compatibility ---\n")

    EXECUTOR.set_confirm_handler(always_confirm)
    fake_windows = [{"hwnd": 1, "title": "Chrome", "process": "chrome.exe", "pid": 100, "visible": True}]
    try:
        with fake_win32(fake_windows):
            reset_session()
            context_memory.record_from_skill("apps.open", {"app": "chrome"}, {"app": "chrome"}, turn_id="app1")
            r = await SESSION.handle("Close it.", actor="test")
        check(
            "app in context, 'Close it.' -> apps.close (not a file-shaped skill)",
            SESSION.last_skill == "apps.close",
            f"skill={SESSION.last_skill} ok={r.ok}",
        )
    finally:
        EXECUTOR.set_confirm_handler(SESSION._confirm)

    # A resolved entity whose type has no compatible candidate at all must
    # never force a bad route — friday.brain.engine.route_with_resolved_entity
    # returns None and the caller falls back to its prior understanding
    # rather than guessing (brief §5: "do not make the system incapable of
    # handling legitimate commands").
    rerouted = brain_engine.route_with_resolved_entity(
        "play it", "it", "preference", "dark mode",
    )
    check(
        "an entity type with no canonical-phrase mapping -> route_with_resolved_entity returns None",
        rerouted is None,
    )


# ==============================================================================
# 8. SAFETY BOUNDARIES — confirmation/permission must never be bypassed by
#    contextual routing (brief §16)
# ==============================================================================


async def section_safety() -> None:
    print("\n--- 8. SAFETY boundaries ---\n")

    import friday.whatsapp as wa_mod

    async def fake_compose(contact: str, message: str) -> str:
        return contact

    send_calls: list[None] = []

    async def fake_send() -> str:
        # Stubbed so this check never touches a real browser/WhatsApp
        # session (same convention as scripts/agent_reliability.py) — only
        # the confirmation *gate* is under test here.
        send_calls.append(None)
        return "rahul"

    orig_compose, orig_send = wa_mod.compose, wa_mod.send
    wa_mod.compose = fake_compose
    wa_mod.send = fake_send
    confirm_calls: list[str] = []

    async def tracking_confirm(skill_obj, args, preview: str) -> bool:
        confirm_calls.append(skill_obj.name)
        return True

    try:
        reset_session()
        EXECUTOR.set_confirm_handler(tracking_confirm)
        await SESSION.handle("Message Rahul on WhatsApp: hello.", actor="test")
        r = await SESSION.handle("Send it.", actor="test")
        check(
            "'Send it.' (L3 send) still asks for confirmation before running",
            confirm_calls == ["whatsapp.send"] and send_calls == [None] and r.ok,
            f"confirm_calls={confirm_calls} send_calls={len(send_calls)} ok={r.ok} speech={r.speech!r}",
        )

        reset_session()
        confirm_calls.clear()
        send_calls.clear()
        EXECUTOR.set_confirm_handler(always_decline)
        await SESSION.handle("Message Rahul on WhatsApp: hello.", actor="test")
        r2 = await SESSION.handle("Send it.", actor="test")
        check(
            "declining the confirmation actually blocks the send",
            not r2.ok and send_calls == [],
            r2.speech,
        )
    finally:
        wa_mod.compose = orig_compose
        wa_mod.send = orig_send
        EXECUTOR.set_confirm_handler(SESSION._confirm)


# ==============================================================================
# 9. PERFORMANCE (brief §19) — median/p95 across the required categories
# ==============================================================================


def _latency_ms(fn, n: int = 15) -> tuple[float, float]:
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    median = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return median, p95


async def _latency_ms_async(fn, n: int = 8) -> tuple[float, float]:
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        await fn()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    median = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return median, p95


async def section_performance() -> None:
    print("\n--- 9. PERFORMANCE (median / p95, n=15 warm calls) ---\n")

    categories = [
        ("simple direct", lambda: BRAIN.understand("what time is it")),
        ("known app", lambda: BRAIN.understand("open Chrome")),
        ("ambiguous", lambda: BRAIN.understand("do the thing")),
    ]
    for label, fn in categories:
        median, p95 = _latency_ms(fn)
        check(f"{label}: median {median:.2f}ms, p95 {p95:.2f}ms (< 200ms)", p95 < 200)

    # Contextual follow-up: the full second-pass route_with_resolved_entity
    # path (two embedding matches instead of one) — still expected to stay
    # well within interactive latency.
    reset_session()
    with isolated_file_root({"report.pdf": "x"}) as (tmp, _):
        path = str(Path(tmp) / "report.pdf")

        async def _once():
            reset_session()
            await SESSION.handle(f"Reveal this file: {path}.", actor="test")
            await SESSION.handle("Read it.", actor="test")

        median, p95 = await _latency_ms_async(_once, n=8)
        check(f"contextual follow-up (2 embedding matches): median {median:.2f}ms, p95 {p95:.2f}ms (< 500ms)", p95 < 500)


# ==============================================================================


async def main() -> None:
    REGISTRY.discover()
    t0 = time.perf_counter()
    BRAIN.warm()
    print(f"\n{len(REGISTRY.all())} skills | brain warm in {time.perf_counter() - t0:.1f}s")

    section_direct()
    section_known_app()
    await section_contextual()
    await section_corrections()
    await section_multi_step()
    section_adversarial()
    await section_entity_compat()
    await section_safety()
    await section_performance()

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    reset_session()
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
