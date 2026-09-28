"""Phase 12.0 — deterministic agent reliability harness.

Reuses friday.intelligence.evaluator (`evaluate_step`/`evaluate_goal`) for
the pass/fail verdict on every scenario that produces an `Observation`/
`OrchestratorResult` — this file adds only a thin, per-scenario *evidence*
check on top (did the specific tool/arg sequence, returned data, or
context-memory entry the scenario cares about actually show up), never a
second evaluation architecture. `TaskOutcome` below is a small explicit
mapping from evidence the real evaluator/orchestrator/permissions layer
already produces, not a new judgment call.

Never touches the live desktop: no real Playwright navigation, no real
process spawn, no writes outside a harness-owned temp dir. Every scenario
either drives a fully synthetic tool world (`Orchestrator(runner=...)`), the
real skill registry against faked OS/library boundaries (`fake_win32()`,
a fake Playwright page, an isolated temp file root), or the real
conversational fast path (`SESSION.handle`) with the reusable mechanics
(`friday.whatsapp`, `friday.skills.files`) faked at the lowest safe layer.

Conventions below (ScriptedPlanner, scripted_provider, call/done builders,
fake_win32) are copied verbatim from scripts/smoke_goal_decomposition.py,
scripts/smoke_plan.py, and scripts/smoke_desktop_observer.py — not
reinvented, per PLAN.md Phase 12.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import browser, desktop_observer, llm, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.intelligence import context_memory  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.evaluator import Verdict, evaluate_goal, evaluate_step  # noqa: E402
from friday.intelligence.goals import GoalStatus  # noqa: E402
from friday.intelligence.proactive import PROACTIVE  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.log import get  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, OrchestratorResult, PlanStep, ToolSpec  # noqa: E402
from friday.permissions import EXECUTOR, PermissionError_  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

log = get(__name__)

# ============================================================================
# Shared scaffolding — copied from existing smoke test conventions
# ============================================================================


class ScriptedPlanner(LlmProvider):
    """Replays a fixed sequence of JSON decisions, one per call. Records
    every prompt sent so a scenario can inspect what the planner saw."""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.prompts.append(request.messages[-1].content)
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LlmResponse(text=reply, model="scripted", provider=self.name)


class SlowPlanner(LlmProvider):
    """Never resolves in time for the harness's cancellation scenario."""

    name = "slow"

    async def complete(self, request: LlmRequest) -> LlmResponse:
        await asyncio.sleep(5)
        return LlmResponse(text=done("never reached"), model=self.name, provider=self.name)


@contextlib.contextmanager
def scripted_provider(provider: LlmProvider):
    original = llm.get_provider
    llm.get_provider = lambda name=None: provider
    try:
        yield
    finally:
        llm.get_provider = original


def call(tool: str, args: dict | None = None) -> str:
    return json.dumps({"action": "call", "tool": tool, "args": args or {}})


def done(summary: str) -> str:
    return json.dumps({"action": "done", "summary": summary})


# -- fakes for the Win32 layer (copied from scripts/smoke_desktop_observer.py) --


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

    # -- apps.open's _focus_hwnd() also needs these (smoke_desktop_observer's
    # fake_win32 never had to focus a window, only enumerate/read them) --
    def IsIconic(self, hwnd):
        return False

    def ShowWindow(self, hwnd, cmd):
        return None

    def BringWindowToTop(self, hwnd):
        return None

    def SetForegroundWindow(self, hwnd):
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
    """windows: [{"hwnd", "title", "process", "pid", "visible"?}, ...], first = foreground."""
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


# -- fakes for app resolution / launch (friday.skills.apps) ------------------


@contextlib.contextmanager
def fixed_app_resolution(name: str, target: str):
    """Decouples apps.open's dedup-vs-launch decision from whatever is
    actually installed on the machine running this harness."""
    import friday.skills.apps as apps_mod

    orig = apps_mod.resolve_app
    apps_mod.resolve_app = lambda query: (name, target)
    try:
        yield
    finally:
        apps_mod.resolve_app = orig


@contextlib.contextmanager
def stub_process_launch(*, raise_if_called: bool):
    """Stubs the two ways apps.open spawns a fresh process. `raise_if_called`
    turns an unexpected spawn into a loud failure (proving the dedup path was
    actually taken, not just coincidentally reported); pass False for the
    negative control where a spawn IS expected."""
    import friday.skills.apps as apps_mod

    calls: list[str] = []

    def fake_popen(target, *a, **kw):
        calls.append(f"Popen:{target}")
        if raise_if_called:
            raise AssertionError("subprocess.Popen must not run when a window already matched")
        return None

    def fake_startfile(target, *a, **kw):
        calls.append(f"startfile:{target}")
        if raise_if_called:
            raise AssertionError("os.startfile must not run when a window already matched")
        return None

    orig_popen = apps_mod.subprocess.Popen
    orig_startfile = apps_mod.os.startfile
    apps_mod.subprocess.Popen = fake_popen
    apps_mod.os.startfile = fake_startfile
    try:
        yield calls
    finally:
        apps_mod.subprocess.Popen = orig_popen
        apps_mod.os.startfile = orig_startfile


# -- fake Playwright page (friday.browser) ------------------------------------


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.goto_calls = 0

    def is_closed(self) -> bool:
        return False

    async def goto(self, target: str, wait_until: str = "domcontentloaded") -> None:
        self.goto_calls += 1
        self.url = target

    async def title(self) -> str:
        return "Fake Page"


@contextlib.contextmanager
def fake_browser_page(initial_url: str = "about:blank"):
    page = _FakePage(initial_url)
    orig_page = browser._state.page
    browser._state.page = page
    try:
        yield page
    finally:
        browser._state.page = orig_page


# -- isolated file root (friday.skills.files) ---------------------------------


@contextlib.contextmanager
def isolated_file_root(files: dict[str, str]):
    """A harness-owned temp dir as the *only* root files.search walks, and a
    tracking stub in place of files.reveal's real `subprocess.Popen(explorer
    ...)` call — no real Explorer window ever opens."""
    import friday.skills.files as files_mod

    tmp = tempfile.mkdtemp(prefix="friday-reliability-")
    for name, content in files.items():
        (Path(tmp) / name).write_text(content, encoding="utf-8")

    orig_roots = files_mod._roots
    files_mod._roots = lambda: [Path(tmp)]
    reveal_calls: list[str] = []
    orig_popen = subprocess.Popen

    def fake_popen(args, *a, **kw):
        target = args[-1] if isinstance(args, (list, tuple)) else str(args)
        reveal_calls.append(target)
        return None

    subprocess.Popen = fake_popen
    try:
        yield tmp, reveal_calls
    finally:
        files_mod._roots = orig_roots
        subprocess.Popen = orig_popen
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================
# TaskOutcome / ScenarioReport
# ============================================================================


class TaskOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    UNCERTAIN = "uncertain"


_BLOCKING_ERRORS = {"PermissionError_", "confirmation_declined", "tool_not_allowed"}


def outcome_for_observation(obs: Observation, *, evidence_ok: bool) -> TaskOutcome:
    """Evidence-based outcome for one skill call, built on
    `evaluator.evaluate_step` — see module docstring."""
    if obs.error == "timeout":
        return TaskOutcome.TIMEOUT
    if obs.error in _BLOCKING_ERRORS:
        return TaskOutcome.BLOCKED
    ev = evaluate_step(obs)
    if ev.verdict == Verdict.UNCERTAIN:
        return TaskOutcome.UNCERTAIN
    if ev.success and evidence_ok:
        return TaskOutcome.SUCCESS
    return TaskOutcome.FAILURE


def outcome_for_result(result: OrchestratorResult, *, evidence_ok: bool) -> TaskOutcome:
    """Evidence-based outcome for a whole plan, built on
    `evaluator.evaluate_goal` — see module docstring."""
    if result.stopped == "cancelled":
        return TaskOutcome.CANCELLED
    if result.stopped == "timeout":
        return TaskOutcome.TIMEOUT
    if result.stopped == "tool_not_allowed":
        return TaskOutcome.BLOCKED
    if any(o.error in _BLOCKING_ERRORS for o in result.observations):
        return TaskOutcome.BLOCKED
    verdict = evaluate_goal(result)
    if verdict.verdict == Verdict.UNCERTAIN:
        return TaskOutcome.UNCERTAIN
    if verdict.goal_complete and evidence_ok:
        return TaskOutcome.SUCCESS
    return TaskOutcome.FAILURE


def trace_from_result(result: OrchestratorResult) -> tuple[list[dict], list[str]]:
    actions = [{"tool": o.step.tool, "args": o.step.args, "ok": o.ok} for o in result.observations]
    observations = [
        f"{o.step.tool}({o.step.args}) -> {'ok' if o.ok else 'FAILED'}: {o.speech}"
        + (f" [{o.error}]" if o.error else "")
        for o in result.observations
    ]
    return actions, observations


@dataclass(slots=True)
class ScenarioReport:
    scenario_id: str
    goal: str
    actions: list[dict]
    observations: list[str]
    adaptations: list[str]
    outcome: TaskOutcome
    expected_outcome: TaskOutcome
    failure_reason: str
    total_steps: int
    total_time_s: float

    @property
    def passed(self) -> bool:
        return self.outcome == self.expected_outcome

    def print_block(self) -> None:
        tag = "OK  " if self.passed else "MISS"
        print(f"\n--- Scenario {self.scenario_id} [{tag}] ---")
        print(f"GOAL: {self.goal}")
        print(f"ACTIONS: {self.actions}")
        print(f"OBSERVATIONS: {self.observations}")
        print(f"ADAPTATIONS: {self.adaptations or ['(none)']}")
        print(f"FINAL STATUS: {self.outcome.value} (expected {self.expected_outcome.value})")
        print(f"FAILURE REASON: {self.failure_reason or '(none)'}")
        print(f"TOTAL STEPS: {self.total_steps}")
        print(f"TOTAL TIME: {self.total_time_s * 1000:.1f} ms")


def reset_between_scenarios() -> None:
    from friday.intelligence.self_state import SELF_STATE, SelfState, SelfStatus

    INTEL.reset()
    context_memory.CONTEXT.reset()
    PROACTIVE._cooldowns.clear()
    PROACTIVE._notify_times.clear()
    PROACTIVE._queue.clear()
    PROACTIVE._last_goal_seen = None
    EXECUTOR.set_confirm_handler(SESSION._confirm)
    SESSION.pending = None
    SELF_STATE.state = SelfState(status=SelfStatus.IDLE, updated_at="reliability-harness")


# ============================================================================
# Scenario A — app chain: "Open Chrome, open YouTube, search for a song."
# ============================================================================


async def scenario_a() -> ScenarioReport:
    goal = "Open Chrome, open YouTube, search for a song."
    calls: list[tuple[str, dict]] = []

    async def mock_runner(tool: str, args: dict, actor: str) -> SkillResult:
        calls.append((tool, dict(args)))
        if tool == "apps.open":
            return SkillResult(speech="Opening chrome.", data={"app": "chrome"})
        if tool == "browser.open":
            return SkillResult(speech="Opened YouTube.", data={"url": "https://youtube.com", "title": "YouTube"})
        if tool == "browser.type":
            return SkillResult(speech="Filled search box.", data={"field": args.get("field", "")})
        if tool == "browser.press":
            return SkillResult(speech=f"Pressed {args.get('key')}.")
        raise KeyError(tool)

    specs = [
        ToolSpec(name="apps.open", description="Launch an application"),
        ToolSpec(name="browser.open", description="Open a URL"),
        ToolSpec(name="browser.type", description="Type into a field"),
        ToolSpec(name="browser.press", description="Press a key"),
    ]
    planner = ScriptedPlanner([
        call("apps.open", {"app": "chrome"}),
        call("browser.open", {"url": "youtube.com"}),
        call("browser.type", {"field": "search box", "text": "lofi hip hop"}),
        call("browser.press", {"key": "Enter"}),
        done("Opened Chrome, opened YouTube, searched for lofi hip hop."),
    ])
    orch = Orchestrator(
        tools=[s.name for s in specs], runner=mock_runner, actor="test",
        llm_provider=planner, tool_specs=specs, max_steps=6,
    )
    started = time.perf_counter()
    result = await orch.run_goal(goal)
    elapsed = time.perf_counter() - started

    expected_trace = [
        ("apps.open", {"app": "chrome"}),
        ("browser.open", {"url": "youtube.com"}),
        ("browser.type", {"field": "search box", "text": "lofi hip hop"}),
        ("browser.press", {"key": "Enter"}),
    ]
    evidence_ok = (
        calls == expected_trace
        and len(result.observations) >= 2
        and result.observations[1].data.get("title") == "YouTube"
    )
    outcome = outcome_for_result(result, evidence_ok=evidence_ok)
    actions, observations = trace_from_result(result)
    return ScenarioReport(
        scenario_id="A", goal=goal, actions=actions, observations=observations, adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if outcome == TaskOutcome.SUCCESS else evaluate_goal(result).reason,
        total_steps=len(result.observations), total_time_s=elapsed,
    )


# ============================================================================
# Scenario B — browser chain: "Open Chrome, search Google for X, open result."
# ============================================================================


async def scenario_b() -> ScenarioReport:
    goal = "Open Chrome, search Google for X, open the result."
    calls: list[tuple[str, dict]] = []

    async def mock_runner(tool: str, args: dict, actor: str) -> SkillResult:
        calls.append((tool, dict(args)))
        if tool == "apps.open":
            return SkillResult(speech="Opening chrome.", data={"app": "chrome"})
        if tool == "browser.open":
            return SkillResult(speech="Opened Google.", data={"url": "https://google.com", "title": "Google"})
        if tool == "browser.type":
            return SkillResult(speech="Filled search box.", data={"field": args.get("field", "")})
        if tool == "browser.press":
            return SkillResult(speech="Pressed Enter.")
        if tool == "browser.click":
            return SkillResult(speech="Clicked Example Result.", data={"clicked": "Example Result — example.com"})
        raise KeyError(tool)

    specs = [ToolSpec(name=n, description=n) for n in ("apps.open", "browser.open", "browser.type", "browser.press", "browser.click")]
    planner = ScriptedPlanner([
        call("apps.open", {"app": "chrome"}),
        call("browser.open", {"url": "google.com"}),
        call("browser.type", {"field": "search box", "text": "X"}),
        call("browser.press", {"key": "Enter"}),
        call("browser.click", {"target": "Example Result"}),
        done("Opened the top result for X."),
    ])
    orch = Orchestrator(
        tools=[s.name for s in specs], runner=mock_runner, actor="test",
        llm_provider=planner, tool_specs=specs, max_steps=7,
    )
    started = time.perf_counter()
    result = await orch.run_goal(goal)
    elapsed = time.perf_counter() - started

    type_args = next((a for t, a in calls if t == "browser.type"), {})
    evidence_ok = (
        [t for t, _ in calls] == ["apps.open", "browser.open", "browser.type", "browser.press", "browser.click"]
        and type_args.get("text") == "X"
        and bool(result.observations) and result.observations[-1].data.get("clicked") == "Example Result — example.com"
    )
    outcome = outcome_for_result(result, evidence_ok=evidence_ok)
    actions, observations = trace_from_result(result)
    return ScenarioReport(
        scenario_id="B", goal=goal, actions=actions, observations=observations, adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if outcome == TaskOutcome.SUCCESS else evaluate_goal(result).reason,
        total_steps=len(result.observations), total_time_s=elapsed,
    )


# ============================================================================
# Scenario C — file workflow: "Find report.pdf and open it."
# ============================================================================


async def scenario_c() -> ScenarioReport:
    goal = "Find report.pdf and open it."
    with isolated_file_root({"report.pdf": "This is a fake report."}) as (tmp, reveal_calls):
        expected_path = str(Path(tmp) / "report.pdf")
        planner = ScriptedPlanner([
            call("files.search", {"query": "report", "extension": "pdf"}),
            call("files.reveal", {"path": expected_path}),
            done("Found report.pdf and opened it in Explorer."),
        ])
        specs = [ToolSpec(name="files.search", description="search"), ToolSpec(name="files.reveal", description="reveal")]
        orch = Orchestrator(tools=[s.name for s in specs], actor="test", llm_provider=planner, tool_specs=specs, max_steps=5)
        started = time.perf_counter()
        result = await orch.run_goal(goal)
        elapsed = time.perf_counter() - started

        found_path = None
        if result.observations and result.observations[0].data.get("results"):
            found_path = result.observations[0].data["results"][0]["path"]
        evidence_ok = found_path == expected_path and reveal_calls == [expected_path]

    outcome = outcome_for_result(result, evidence_ok=evidence_ok)
    actions, observations = trace_from_result(result)
    return ScenarioReport(
        scenario_id="C", goal=goal, actions=actions, observations=observations, adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if outcome == TaskOutcome.SUCCESS else f"resolved={found_path!r}, revealed={reveal_calls!r}",
        total_steps=len(result.observations), total_time_s=elapsed,
    )


# ============================================================================
# Scenario D — contextual follow-up: "Reveal report.pdf." -> "Read it."
# ============================================================================


async def scenario_d() -> ScenarioReport:
    goal = 'Reveal this file: <path>\\report.pdf. -> Read the file.'
    from friday.intelligence import context_resolver

    started = time.perf_counter()
    with isolated_file_root({"report.pdf": "Report body text for scenario D."}) as (tmp, _reveal_calls):
        expected_path = str(Path(tmp) / "report.pdf")
        # friday.brain.extract's path-slot extraction (files.reveal's "path"
        # param) requires something path-shaped (a drive letter, ./, ../, or
        # ~) -- a bare filename like "report.pdf" doesn't match and falls to
        # ASK_SLOT, so a real full path is used here, exactly as a real user
        # naming a specific file would.
        turn1 = await SESSION.handle(f"Reveal this file: {expected_path}.", actor="test")
        turn1_skill, turn1_args = SESSION.last_skill, dict(SESSION.last_args)

        # "Read the file." is the correct routing/reference-detection proof
        # ("the file" is one of context_resolver's own patterns, and
        # files.read's "path" slot can't accept it literally, so it
        # genuinely lands on ASK_SLOT + contains_reference, exactly as
        # Session._needs_context_resolution requires). Past that point, a
        # second, SEPARATE brittleness used to bite: once "the file" was
        # substituted with an actual filesystem path, BRAIN's embedding
        # matcher would drift away from files.read entirely (confirmed
        # empirically on this machine) purely because the literal word
        # "file" was now gone from the sentence. Recorded as a limitation
        # in PLAN.md Phase 12.0 §4/§5 and FIXED in Phase 13.0 via
        # friday.brain.engine.route_with_resolved_entity (matches on a
        # generic "the file" canonical phrase for routing, never the raw
        # path). This scenario keeps verifying the two granular primitives
        # directly (routing decision + resolution), then ALSO proves the
        # full real Session.handle() pipeline — the actual end-to-end path
        # a user's second turn takes — now lands on files.read with the
        # right path, with no separate "known limitation" carve-out needed.
        pre = BRAIN.understand("Read the file.")
        needs_resolution = pre.action.value != "act" and context_resolver.contains_reference("Read the file.")
        substituted, resolution = context_resolver.resolve_pronoun_in_text("Read the file.")
        read_result = None
        if resolution is not None and resolution.resolved:
            read_result = await EXECUTOR.run("files.read", {"path": resolution.referent}, actor="test")

        # Full end-to-end proof (Phase 13.0): the real second SESSION.handle()
        # call, exactly as a live user's follow-up turn would run it — not
        # the two primitives above called directly.
        SESSION.pending = None
        turn2 = await SESSION.handle("Read it.", actor="test")
    elapsed = time.perf_counter() - started

    evidence_ok = (
        turn1_skill == "files.reveal" and turn1.ok
        and pre.skill == "files.read" and needs_resolution
        and resolution is not None and resolution.resolved and resolution.entity_type == "file"
        and (resolution.referent or "").lower() == expected_path.lower()  # Windows paths, brain.extract lowercases
        and read_result is not None and read_result.ok
        and "Report body text for scenario D" in read_result.data.get("content", "")
        and SESSION.last_skill == "files.read" and turn2.ok
        and "Report body text for scenario D" in turn2.data.get("content", "")
    )
    obs = Observation(
        PlanStep(tool="files.read", args={"path": resolution.referent if resolution else None}),
        bool(read_result and read_result.ok), read_result.speech if read_result else "not resolved",
        read_result.data if read_result else {},
    )
    outcome = outcome_for_observation(obs, evidence_ok=evidence_ok)
    return ScenarioReport(
        scenario_id="D", goal=goal,
        actions=[
            {"tool": turn1_skill, "args": turn1_args},
            {"tool": "files.read", "args": {"path": resolution.referent if resolution else None}},
        ],
        observations=[
            f"turn1 {turn1_skill} -> {'ok' if turn1.ok else 'FAILED'}: {turn1.speech}",
            f"'Read the file.' -> BRAIN routes to {pre.skill} (needs resolution: {needs_resolution})",
            f"resolved 'the file' -> {resolution.referent if resolution else None!r}",
            f"files.read({resolution.referent if resolution else None!r}) -> {'ok' if read_result and read_result.ok else 'FAILED'}",
            f"end-to-end SESSION.handle('Read it.') -> {SESSION.last_skill} ok={turn2.ok}",
        ],
        adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if outcome == TaskOutcome.SUCCESS else (
            f"pre.skill={pre.skill!r}, resolution={resolution!r}, "
            f"read_result={read_result!r}, turn2_skill={SESSION.last_skill!r}, turn2={turn2!r}"
        ),
        total_steps=2, total_time_s=elapsed,
    )


# ============================================================================
# Scenario E — multi-turn contextual: "Message Rahul..." -> "Send him..."
# ============================================================================


async def scenario_e() -> ScenarioReport:
    goal = "Message Rahul on WhatsApp: just checking in. -> Text him about the update on WhatsApp."
    import friday.whatsapp as wa_mod
    from friday.intelligence import context_resolver

    compose_calls: list[tuple[str, str]] = []

    async def fake_compose(contact: str, message: str) -> str:
        compose_calls.append((contact, message))
        return contact

    orig_compose = wa_mod.compose
    wa_mod.compose = fake_compose

    async def always_confirm(skill_obj, args, preview) -> bool:
        return True

    EXECUTOR.set_confirm_handler(always_confirm)
    started = time.perf_counter()
    try:
        turn1 = await SESSION.handle("Message Rahul on WhatsApp: just checking in.", actor="test")
        turn1_skill, turn1_args = SESSION.last_skill, dict(SESSION.last_args)
        # "Send him the project update." and its rephrasings all directly
        # ACT-match a skill via BRAIN's embedding matcher before context
        # resolution ever gets a chance to run (Session._needs_context_
        # resolution deliberately never second-guesses an Action.ACT) --
        # confirmed empirically; see PLAN.md Phase 12.0 §4 for the phrasing
        # this scenario settled on instead, one that genuinely lands on
        # ASK_SLOT (missing contact+message) so context resolution engages,
        # exactly as it would for any phrasing extract.py can't fully fill.
        turn2_text = "Text him about the update on WhatsApp."
        substituted, resolution = context_resolver.resolve_pronoun_in_text(turn2_text)
    finally:
        wa_mod.compose = orig_compose
        EXECUTOR.set_confirm_handler(SESSION._confirm)
    elapsed = time.perf_counter() - started

    evidence_ok = (
        turn1_skill == "whatsapp.compose" and turn1.ok
        and turn1_args.get("contact", "").lower() == "rahul"
        and compose_calls == [("rahul", turn1_args.get("message", ""))]
        and resolution is not None and resolution.resolved
        and (resolution.referent or "").lower() == "rahul"
        and substituted.lower() == "text rahul about the update on whatsapp."
    )
    outcome = TaskOutcome.SUCCESS if evidence_ok else TaskOutcome.FAILURE
    return ScenarioReport(
        scenario_id="E", goal=goal,
        actions=[
            {"tool": turn1_skill, "args": turn1_args},
            {"tool": "context_resolver.resolve_pronoun_in_text", "args": {"text": turn2_text}},
        ],
        observations=[
            f"turn1 {turn1_skill} -> {'ok' if turn1.ok else 'FAILED'}: {turn1.speech} (compose_calls={compose_calls})",
            f"turn2 'him' resolved -> referent={resolution.referent if resolution else None!r}, substituted={substituted!r}",
        ],
        adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if evidence_ok else (
            f"turn1_args={turn1_args!r}, compose_calls={compose_calls!r}, "
            f"resolution={resolution!r}, substituted={substituted!r}"
        ),
        total_steps=2, total_time_s=elapsed,
    )


# ============================================================================
# Scenario F — adaptive browser recovery (bounded, not infinite retry)
# ============================================================================


async def scenario_f() -> ScenarioReport:
    goal = "Click Buy Now; if that fails, find another way to add the item to the cart."
    calls: list[tuple[str, dict]] = []

    async def mock_runner(tool: str, args: dict, actor: str) -> SkillResult:
        calls.append((tool, dict(args)))
        if tool == "browser.click":
            if args.get("target") == "Buy Now":
                return SkillResult(speech="I couldn't find that.", ok=False)
            return SkillResult(speech=f"Clicked {args.get('target')}.", data={"clicked": args.get("target")})
        if tool == "browser.inspect":
            return SkillResult(
                speech="2 elements: Add to Cart, Buy Now.",
                data={"elements": [{"role": "button", "text": "Add to Cart"}, {"role": "button", "text": "Buy Now"}]},
            )
        raise KeyError(tool)

    specs = [ToolSpec(name="browser.click", description="click"), ToolSpec(name="browser.inspect", description="inspect")]
    planner = ScriptedPlanner([
        call("browser.click", {"target": "Buy Now"}),
        call("browser.inspect", {}),
        call("browser.click", {"target": "Add to Cart"}),
        done("Added the item to the cart via a different button."),
    ])
    orch = Orchestrator(tools=[s.name for s in specs], runner=mock_runner, actor="test", llm_provider=planner, tool_specs=specs, max_steps=6)
    started = time.perf_counter()
    result = await orch.run_goal(goal, max_replans=1)
    elapsed = time.perf_counter() - started

    adaptations: list[str] = []
    if len(result.observations) >= 3 and not result.observations[0].ok and result.observations[2].ok:
        adaptations.append(
            f"browser.click('Buy Now') failed -> browser.inspect -> retried with "
            f"'{result.observations[2].step.args.get('target')}' (different target)"
        )
    sub1_ok = (
        [t for t, _ in calls] == ["browser.click", "browser.inspect", "browser.click"]
        and calls[0][1].get("target") != calls[2][1].get("target")
        and result.ok
    )

    # Sub-case 2: identical failing call retried -> repeat-guard bounds it.
    calls2: list[tuple[str, dict]] = []

    async def failing_runner(tool: str, args: dict, actor: str) -> SkillResult:
        calls2.append((tool, dict(args)))
        return SkillResult(speech="I couldn't find that.", ok=False)

    planner2 = ScriptedPlanner([call("browser.click", {"target": "Buy Now"})] * 3 + [done("never reached")])
    orch2 = Orchestrator(
        tools=["browser.click"], runner=failing_runner, actor="test", llm_provider=planner2,
        tool_specs=[ToolSpec(name="browser.click", description="click")], max_steps=8,
    )
    result2 = await orch2.run_goal("retry the exact same failing click forever", max_replans=5)
    sub2_ok = len(calls2) == 1 and result2.stopped == "repeated_action"

    evidence_ok = sub1_ok and sub2_ok
    outcome = outcome_for_result(result, evidence_ok=evidence_ok)
    actions, observations = trace_from_result(result)
    observations.append(f"[bounded-retry sub-case] identical repeat invoked the tool {len(calls2)}x, stopped={result2.stopped}")
    return ScenarioReport(
        scenario_id="F", goal=goal, actions=actions, observations=observations, adaptations=adaptations,
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if evidence_ok else f"sub1_ok={sub1_ok}, sub2_ok={sub2_ok} (invoked {len(calls2)}x, stopped={result2.stopped})",
        total_steps=len(result.observations), total_time_s=elapsed,
    )


# ============================================================================
# Scenario G — app already open: no duplicate instance
# ============================================================================


async def scenario_g() -> ScenarioReport:
    goal = "Open Chrome (already running) -- must reuse the window, not spawn a second one."
    started = time.perf_counter()
    windows = [{"hwnd": 1, "title": "New Tab - Google Chrome", "process": "chrome.exe", "pid": 100}]
    with fixed_app_resolution("chrome", "chrome.exe"), fake_win32(windows), stub_process_launch(raise_if_called=True) as spawn_calls:
        result = await EXECUTOR.run("apps.open", {"app": "chrome"}, actor="test")
    already_running_ok = (
        result.ok and result.data.get("already_running") is True
        and result.data.get("title") == "New Tab - Google Chrome"
        and not spawn_calls
    )

    # Negative control: no matching window -> the spawn path must be reachable.
    with fixed_app_resolution("chrome", "chrome.exe"), fake_win32([]), stub_process_launch(raise_if_called=False) as spawn_calls2:
        result_neg = await EXECUTOR.run("apps.open", {"app": "chrome"}, actor="test")
    negative_ok = result_neg.ok and not result_neg.data.get("already_running") and bool(spawn_calls2)
    elapsed = time.perf_counter() - started

    evidence_ok = already_running_ok and negative_ok
    obs = Observation(PlanStep(tool="apps.open", args={"app": "chrome"}), result.ok, result.speech, result.data)
    outcome = outcome_for_observation(obs, evidence_ok=evidence_ok)
    return ScenarioReport(
        scenario_id="G", goal=goal,
        actions=[{"tool": "apps.open", "args": {"app": "chrome"}}],
        observations=[
            f"already-open case -> {result.speech} (data={result.data})",
            f"negative control (no window) -> {result_neg.speech} (spawned={spawn_calls2})",
        ],
        adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if evidence_ok else f"already_running_ok={already_running_ok}, negative_ok={negative_ok}",
        total_steps=1, total_time_s=elapsed,
    )


# ============================================================================
# Scenario H — website already open: skip redundant navigation (exact URL)
# ============================================================================


async def scenario_h() -> ScenarioReport:
    goal = "Open YouTube (already open, must skip); then search Google for X (must still navigate)."
    started = time.perf_counter()
    with fake_browser_page("https://example.com") as page:
        info1 = await browser.goto("youtube.com")
        calls_after_1 = page.goto_calls
        info2 = await browser.goto("youtube.com")
        calls_after_2 = page.goto_calls
        sub1_ok = calls_after_1 == 1 and calls_after_2 == 1 and info2.already_loaded is True and not info1.already_loaded

        info3 = await browser.goto("google.com")
        calls_after_3 = page.goto_calls
        info4 = await browser.goto("google.com/search?q=foo")
        calls_after_4 = page.goto_calls
        sub2_ok = calls_after_4 == calls_after_3 + 1 and not info4.already_loaded and not info3.already_loaded
    elapsed = time.perf_counter() - started

    evidence_ok = sub1_ok and sub2_ok
    outcome = TaskOutcome.SUCCESS if evidence_ok else TaskOutcome.FAILURE
    return ScenarioReport(
        scenario_id="H", goal=goal,
        actions=[
            {"tool": "browser.open", "args": {"url": "youtube.com"}},
            {"tool": "browser.open", "args": {"url": "youtube.com"}},
            {"tool": "browser.open", "args": {"url": "google.com"}},
            {"tool": "browser.open", "args": {"url": "google.com/search?q=foo"}},
        ],
        observations=[
            f"revisit same URL twice -> real navigations={calls_after_2} (expect 1), already_loaded={info2.already_loaded}",
            f"same domain, different query -> real navigations delta={calls_after_4 - calls_after_3} (expect 1), already_loaded={info4.already_loaded}",
        ],
        adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if evidence_ok else f"sub1_ok={sub1_ok}, sub2_ok={sub2_ok}",
        total_steps=4, total_time_s=elapsed,
    )


# ============================================================================
# Scenario I — permission boundary: unattended actor auto-denied
# ============================================================================


async def scenario_i() -> ScenarioReport:
    goal = "Attempt a confirm-tier action while unattended (actor=scheduler) -- must be stopped, not asked."
    started = time.perf_counter()
    side_effect_calls: list[bool] = []

    if REGISTRY.get("test.reliability_confirm_tier") is None:
        @skill(name="test.reliability_confirm_tier", tier="L2", description="test-only confirm-tier action for the reliability harness")
        def _confirm_tier_action() -> SkillResult:
            side_effect_calls.append(True)
            return SkillResult(speech="did the confirm-tier thing")

    blocked, err = False, ""
    try:
        await EXECUTOR.run("test.reliability_confirm_tier", {}, actor="scheduler")
    except PermissionError_ as exc:
        blocked, err = True, str(exc)
    elapsed = time.perf_counter() - started

    evidence_ok = blocked and not side_effect_calls
    obs = Observation(
        PlanStep(tool="test.reliability_confirm_tier", args={}),
        False, err or "not blocked", error="PermissionError_" if blocked else "",
    )
    outcome = outcome_for_observation(obs, evidence_ok=evidence_ok)
    return ScenarioReport(
        scenario_id="I", goal=goal,
        actions=[{"tool": "test.reliability_confirm_tier", "args": {}}],
        observations=[f"unattended actor -> {'denied: ' + err if blocked else 'NOT DENIED (bug)'}, side_effect_ran={bool(side_effect_calls)}"],
        adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.BLOCKED,
        failure_reason="" if outcome == TaskOutcome.BLOCKED else "the confirm-tier action was not blocked, or its side effect ran anyway",
        total_steps=1, total_time_s=elapsed,
    )


# ============================================================================
# Scenario J — confirmation boundary: consequential action, granted
# ============================================================================


async def scenario_j() -> ScenarioReport:
    goal = "Click 'Send Message' (consequential) -- requires confirmation, which is granted."
    started = time.perf_counter()
    timeline: list[str] = []

    if REGISTRY.get("test.reliability_consequential") is None:
        @skill(
            name="test.reliability_consequential", tier="L1",
            description="test-only consequential action for the reliability harness",
            risk=lambda **kw: True,
        )
        def _consequential_action(target: Annotated[str, "target"] = "Send Message") -> SkillResult:
            timeline.append(f"executed:{target}")
            return SkillResult(speech=f"Did the consequential thing: {target}.")

    async def recording_confirm(skill_obj, args, preview) -> bool:
        timeline.append(f"confirm_requested:{skill_obj.name}")
        return True

    EXECUTOR.set_confirm_handler(recording_confirm)
    try:
        result = await EXECUTOR.run("test.reliability_consequential", {"target": "Send Message"}, actor="text")
    finally:
        EXECUTOR.set_confirm_handler(SESSION._confirm)
    elapsed = time.perf_counter() - started

    evidence_ok = result.ok and timeline == ["confirm_requested:test.reliability_consequential", "executed:Send Message"]
    obs = Observation(PlanStep(tool="test.reliability_consequential", args={"target": "Send Message"}), result.ok, result.speech, result.data)
    outcome = outcome_for_observation(obs, evidence_ok=evidence_ok)
    return ScenarioReport(
        scenario_id="J", goal=goal,
        actions=[{"tool": "test.reliability_consequential", "args": {"target": "Send Message"}}],
        observations=[f"timeline={timeline} -> {result.speech}"],
        adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if outcome == TaskOutcome.SUCCESS else f"timeline={timeline!r}",
        total_steps=1, total_time_s=elapsed,
    )


# ============================================================================
# Scenario K — confirmation denied: no alternate route bypasses it
# ============================================================================


async def scenario_k() -> ScenarioReport:
    goal = "Do the consequential thing; if declined, try a DIFFERENT consequential action instead (must not happen)."
    started = time.perf_counter()
    timeline: list[str] = []

    if REGISTRY.get("test.reliability_consequential_2") is None:
        @skill(
            name="test.reliability_consequential_2", tier="L1",
            description="test-only alternate consequential action for the reliability harness",
            risk=lambda **kw: True,
        )
        def _consequential_action_2(target: Annotated[str, "target"] = "Confirm Send") -> SkillResult:
            timeline.append(f"executed:{target}")
            return SkillResult(speech=f"Did it: {target}.")

    async def declining_confirm(skill_obj, args, preview) -> bool:
        timeline.append(f"confirm_declined:{skill_obj.name}")
        return False

    EXECUTOR.set_confirm_handler(declining_confirm)
    specs = [
        ToolSpec(name="test.reliability_consequential", description="do the thing"),
        ToolSpec(name="test.reliability_consequential_2", description="do the alternate thing"),
    ]
    planner = ScriptedPlanner([
        call("test.reliability_consequential", {"target": "Send Message"}),
        call("test.reliability_consequential_2", {"target": "Confirm Send"}),  # must never be reached
    ])
    orch = Orchestrator(tools=[s.name for s in specs], actor="text", llm_provider=planner, tool_specs=specs, max_steps=6)
    try:
        result = await orch.run_goal(goal, max_replans=2)
    finally:
        EXECUTOR.set_confirm_handler(SESSION._confirm)
    elapsed = time.perf_counter() - started

    evidence_ok = (
        len(result.observations) == 1
        and result.observations[0].error == "confirmation_declined"
        and planner.calls == 1
        and timeline == ["confirm_declined:test.reliability_consequential"]
        and not result.ok and result.stopped == "failure"
    )
    outcome = outcome_for_result(result, evidence_ok=evidence_ok)
    actions, observations = trace_from_result(result)
    observations.append(f"planner asked for a decision {planner.calls}x (the alternate-action step must never be requested)")
    return ScenarioReport(
        scenario_id="K", goal=goal, actions=actions, observations=observations, adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.BLOCKED,
        failure_reason="" if outcome == TaskOutcome.BLOCKED else f"planner.calls={planner.calls}, timeline={timeline!r}",
        total_steps=len(result.observations), total_time_s=elapsed,
    )


# ============================================================================
# Scenario L — proactive interaction complements, never duplicates
# ============================================================================


async def scenario_l() -> ScenarioReport:
    goal = "Run the FRIDAY build (background job) while a direct conversation stays untouched."
    started = time.perf_counter()
    notices: list[dict] = []

    async def capture_notice(event) -> None:
        notices.append(dict(event.data))

    async def mock_runner(tool: str, args: dict, actor: str) -> SkillResult:
        return SkillResult(speech="Build finished.", data={})

    BUS.subscribe("proactive.notice", capture_notice)
    try:
        orch_bg = Orchestrator(tools=["mock.build"], runner=mock_runner, actor="scheduler")
        await orch_bg.run_plan(goal, [PlanStep(tool="mock.build", args={})])
        sub1_ok = len(notices) == 1 and notices[0].get("action") == "inform" and goal[:30] in notices[0].get("text", "")

        orch_fg = Orchestrator(tools=["mock.build"], runner=mock_runner, actor="text")
        await orch_fg.run_plan("A direct user-run goal (should not be re-announced).", [PlanStep(tool="mock.build", args={})])
        sub2_ok = len(notices) == 1  # no new notice from the direct-actor run
    finally:
        BUS.unsubscribe("proactive.notice", capture_notice)
    elapsed = time.perf_counter() - started

    evidence_ok = sub1_ok and sub2_ok
    outcome = TaskOutcome.SUCCESS if evidence_ok else TaskOutcome.FAILURE
    return ScenarioReport(
        scenario_id="L", goal=goal,
        actions=[
            {"tool": "mock.build", "args": {}, "actor": "scheduler"},
            {"tool": "mock.build", "args": {}, "actor": "text"},
        ],
        observations=[
            f"background (scheduler) completion -> {len(notices)} notice(s), last={notices[-1] if notices else None}",
            f"direct (text) completion -> still {len(notices)} notice(s) (no double-announce)",
        ],
        adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.SUCCESS,
        failure_reason="" if evidence_ok else f"sub1_ok={sub1_ok}, sub2_ok={sub2_ok}, notices={notices!r}",
        total_steps=2, total_time_s=elapsed,
    )


# ============================================================================
# M1 (bonus) — state drift: a measured, reported limitation, not a fix
# ============================================================================


async def scenario_m1() -> ScenarioReport:
    goal = "(bonus) Detect that the foreground app changed mid-task."
    started = time.perf_counter()
    windows_a = [{"hwnd": 1, "title": "YouTube - Google Chrome", "process": "chrome.exe", "pid": 100}]
    windows_b = [{"hwnd": 2, "title": "FRIDAY - Visual Studio Code", "process": "Code.exe", "pid": 200}]
    with fake_win32(windows_a):
        obs_a = await desktop_observer.observe(include_screenshot=False, include_ocr=False)
    with fake_win32(windows_b):
        obs_b = await desktop_observer.observe(include_screenshot=False, include_ocr=False)
    elapsed = time.perf_counter() - started

    changed_in_reality = obs_a.active_window_title != obs_b.active_window_title
    return ScenarioReport(
        scenario_id="M1", goal=goal,
        actions=[{"tool": "desktop_observer.observe", "args": {}}, {"tool": "desktop_observer.observe", "args": {}}],
        observations=[
            f"snapshot 1: active_window_title={obs_a.active_window_title!r}",
            f"snapshot 2: active_window_title={obs_b.active_window_title!r}",
            f"real change between snapshots: {changed_in_reality}",
        ],
        adaptations=[],
        outcome=TaskOutcome.UNCERTAIN, expected_outcome=TaskOutcome.UNCERTAIN,
        failure_reason=(
            "KNOWN LIMITATION (not fixed this phase): desktop_observer.observe() is a "
            "stateless, one-shot snapshot with no history/diff of its own -- nothing in "
            "FRIDAY currently detects or reacts to this change mid-task. Building "
            "drift-detection would cross into 'new intelligence subsystem', out of scope "
            "for Phase 12.0 (see PLAN.md §4)."
        ),
        total_steps=2, total_time_s=elapsed,
    )


# ============================================================================
# M2 (bonus) — repeat-guard is tool-agnostic (also covers destructive tiers)
# ============================================================================


async def scenario_m2() -> ScenarioReport:
    goal = "(bonus) Confirm the repeat-guard blocks an identical call to a destructive-tier tool too."
    calls: list[tuple[str, dict]] = []

    async def mock_runner(tool: str, args: dict, actor: str) -> SkillResult:
        calls.append((tool, dict(args)))
        return SkillResult(speech="destructive action attempted", ok=False)

    planner = ScriptedPlanner([call("apps.close", {"app": "notepad"})] * 3 + [done("never reached")])
    orch = Orchestrator(
        tools=["apps.close"], runner=mock_runner, actor="test", llm_provider=planner,
        tool_specs=[ToolSpec(name="apps.close", description="close (L2 in production)")], max_steps=8,
    )
    started = time.perf_counter()
    # max_replans>0 so the identical failing call actually gets *offered*
    # again (otherwise the very first failure halts the plan before the
    # repeat-guard is ever exercised) -- the guard must still block it.
    result = await orch.run_goal(goal, max_replans=5)
    elapsed = time.perf_counter() - started

    evidence_ok = len(calls) == 1 and result.stopped == "repeated_action"
    # "repeated_action" IS the desired stop here -- expressed as BLOCKED
    # (the guard correctly stopped a repeat), not as a plan failure.
    outcome = TaskOutcome.BLOCKED if evidence_ok else TaskOutcome.FAILURE
    return ScenarioReport(
        scenario_id="M2", goal=goal,
        actions=[{"tool": t, "args": a} for t, a in calls],
        observations=[f"tool invoked {len(calls)}x (expect 1), stopped={result.stopped}"],
        adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.BLOCKED,
        failure_reason="" if evidence_ok else f"invoked {len(calls)}x, stopped={result.stopped}",
        total_steps=len(result.observations), total_time_s=elapsed,
    )


# ============================================================================
# M3 (bonus) — cancellation cleanly terminates the tracked goal
# ============================================================================


async def scenario_m3() -> ScenarioReport:
    goal = "(bonus) A goal that gets cancelled mid-flight."
    started = time.perf_counter()
    with scripted_provider(SlowPlanner()):
        task = asyncio.ensure_future(EXECUTOR.run("plan.run", {"goal": goal}, actor="test"))
        await asyncio.sleep(0.1)
        goal_id = INTEL.state.current_goal_id
        task.cancel()
        cancelled_cleanly = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled_cleanly = True
    elapsed = time.perf_counter() - started

    recorded = goals_mod.get(goal_id) if goal_id else None
    evidence_ok = cancelled_cleanly and recorded is not None and recorded.status == GoalStatus.CANCELLED
    outcome = TaskOutcome.CANCELLED if evidence_ok else TaskOutcome.FAILURE
    return ScenarioReport(
        scenario_id="M3", goal=goal,
        actions=[{"tool": "plan.run", "args": {"goal": goal}}],
        observations=[f"cancelled_cleanly={cancelled_cleanly}, goal.status={recorded.status.value if recorded else None}"],
        adaptations=[],
        outcome=outcome, expected_outcome=TaskOutcome.CANCELLED,
        failure_reason="" if evidence_ok else "cancellation did not propagate cleanly, or the goal wasn't marked CANCELLED",
        total_steps=1, total_time_s=elapsed,
    )


# ============================================================================
# Scorecard
# ============================================================================


def print_scorecard(reports: dict[str, ScenarioReport]) -> None:
    n = len(reports)

    def r(sid: str) -> ScenarioReport | None:
        return reports.get(sid)

    print("\n" + "=" * 78)
    print(f"RELIABILITY SCORECARD -- deterministic/mocked, N={n} scenarios -- NOT real-machine numbers")
    print("=" * 78)

    success = sum(1 for rep in reports.values() if rep.outcome == TaskOutcome.SUCCESS)
    print(f"Task success rate:            {success}/{n}")

    f_rep = r("F")
    print(f"Recovery success rate:        {1 if f_rep and f_rep.passed else 0}/1  (scenario F's injected failure)")

    unsafe_retry = 0 if (f_rep and f_rep.passed) and (r("M2") and r("M2").passed) else 1
    print(f"Unsafe retry count:           {unsafe_retry}  (repeat-guard proof; expect 0)")

    duplicate_action = 0 if (r("G") and r("G").passed) and (r("H") and r("H").passed) else 1
    print(f"Duplicate action count:       {duplicate_action}  (apps.open/browser.open dedup; expect 0)")

    false_success = sum(1 for rep in reports.values() if rep.outcome == TaskOutcome.SUCCESS and not rep.passed)
    print(f"False success count:          {false_success}  (evaluator said complete but scenario evidence disagreed; expect 0)")

    timeout_count = sum(1 for rep in reports.values() if rep.outcome == TaskOutcome.TIMEOUT)
    print(f"Timeout count:                {timeout_count}")

    permission_bypass = 0 if (r("I") and r("I").passed) else 1
    print(f"Permission bypass count:      {permission_bypass}  (expect 0)")

    confirmation_bypass = 0 if (r("K") and r("K").passed) else 1
    print(f"Confirmation bypass count:    {confirmation_bypass}  (expect 0)")

    ctx_scored = [sid for sid in ("D", "E") if r(sid)]
    ctx_correct = sum(1 for sid in ctx_scored if r(sid).passed)
    print(f"Context resolution accuracy:  {ctx_correct}/{len(ctx_scored)}")

    avg_steps = (sum(rep.total_steps for rep in reports.values()) / n) if n else 0.0
    print(f"Average steps:                {avg_steps:.2f}")

    times = sorted(rep.total_time_s for rep in reports.values())
    p95 = times[min(len(times) - 1, int(0.95 * len(times)))] if times else 0.0
    print(f"P95 task latency:             {p95 * 1000:.1f} ms  (scripted/mocked calls only -- not real network/OS latency)")

    print("\n[!] small deterministic sample (N={}) -- do not extrapolate to real-world reliability\n".format(n))


# ============================================================================
# main
# ============================================================================


async def main() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()

    scenarios = [
        ("A", scenario_a), ("B", scenario_b), ("C", scenario_c), ("D", scenario_d),
        ("E", scenario_e), ("F", scenario_f), ("G", scenario_g), ("H", scenario_h),
        ("I", scenario_i), ("J", scenario_j), ("K", scenario_k), ("L", scenario_l),
        ("M1", scenario_m1), ("M2", scenario_m2), ("M3", scenario_m3),
    ]

    reports: dict[str, ScenarioReport] = {}
    for sid, fn in scenarios:
        reset_between_scenarios()
        try:
            report = await fn()
        except Exception as exc:  # noqa: BLE001 - a scenario crashing is itself a finding
            log.exception("scenario %s crashed", sid)
            report = ScenarioReport(
                scenario_id=sid, goal="(crashed before completion)", actions=[],
                observations=[f"unhandled exception: {exc!r}"], adaptations=[],
                outcome=TaskOutcome.FAILURE, expected_outcome=TaskOutcome.SUCCESS,
                failure_reason=f"scenario harness crashed: {exc!r}", total_steps=0, total_time_s=0.0,
            )
        reports[sid] = report
        report.print_block()

    print_scorecard(reports)
    overall = all(rep.passed for rep in reports.values())
    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
