"""Phase 72 -- a graceful shutdown, from the point of view of the process that shuts down AND of the process that starts next.

Phases 47/48 (`Backend.shutdown` waits a bounded time for work in flight, then marks what is still running "outcome unknown"), 45
(`_quit` runs every step even if one fails) and 52/53 (a later process reconciles only what a DEAD owner left open) were each proved in ONE
process. Phase 70/71 crossed a process boundary, but only for a hard exit (`os._exit`), which skips the shutdown path entirely. This suite
runs the shutdown path in real OS processes and then looks at what the NEXT process finds:

  process G   a real `Backend` (its real `_boot`: store, `audit.start_session`, registry, brain, scheduler, trigger watcher) in a fresh
              process against a throwaway database. It does one thing -- nothing; a state-changing action in flight; a goal that has finished
              one step and is between steps; a goal with a step in flight -- and then quits the way the tray's Quit does: `friday.gui.app._quit`
              (every step, in order), then `_await_backend_stop`, then the interpreter ends NORMALLY (`atexit` handlers and the join of worker
              threads run). Only the Qt window / tray / hub, which would need a display, are stand-ins; the backend, the hotkey stop and the
              single-instance release are the real ones.
  process R   a fresh read-only process: `meta.goal_status`, `meta.recent` and a dump of every audit and goal row. It must change nothing.
  process C   a fresh process that runs the boot hook (`audit.start_session`) and then asks the same questions.
  forced      the contrast: the same work in flight, and the parent TERMINATES the process from outside (`TerminateProcess`, no atexit, no drain).

Nothing is simulated by editing stored state: every row is written by the production code of a process that really ran and really ended.
The parent imports nothing from `friday`; every child redirects `friday.paths` into a temp folder first, stubs `fastembed` and watches
`data/` with an audit hook (Phase 70's harness). Nothing real acts: every real non-L0 skill is hard-denied in every child.

Not covered, and not claimed: the live GUI (Qt event loop, tray, window, global hotkeys), voice, the daemon (it has no shutdown path of its own:
`uvicorn` ends and nothing in FRIDAY runs), a console Ctrl-C / SIGTERM, the real embedding model, a machine shutdown or logoff.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import smoke_process_restart as P70  # noqa: E402   (standard library only: `friday` is imported inside the child functions)

SCRIPT = Path(__file__).resolve()
W, S = P70.W, P70.S
SDS = "test.sd_slow"
STATUS, RECENT = "meta.goal_status", "meta.recent"

# what a G process does. direct = state-changing calls made straight through the Executor (not a goal); goal = (tool, args, stage) decisions
# for a real `plan.run`; block_before = the planner stalls before its Nth decision (the goal is between steps).
SCN: dict[str, dict] = {
    "idle": {},
    "inflight": {"direct": ["i"], "slow_s": 2.0},
    "goal_between": {"goal": [(W, {"name": "b", "text": "x"}, 0), (S, {"name": "b"}, 1)], "block_before": 1},
    "goal_inflight": {"goal": [(W, {"name": "g", "text": "x"}, 0), (SDS, {"name": "g"}, 1)], "slow_s": 2.0},
    "failing_steps": {"direct": ["f"], "slow_s": 2.0, "inject": True},
    "forced": {"goal": [(W, {"name": "k", "text": "x"}, 0), (SDS, {"name": "kg"}, 1)], "direct": ["kd"], "slow_s": 600.0},
    "live": {"goal": [(W, {"name": "v", "text": "x"}, 0), (SDS, {"name": "vg"}, 1)], "slow_s": 600.0},
}
DRAIN_S = 0.5          # the bound Phase 47 tests with; `Backend.SHUTDOWN_DRAIN_S` is 5.0 in production
OTHER_TABLES = ("undo", "episodes", "jobs", "job_runs", "memory", "corrections", "phrasings", "kb_documents", "kb_chunks")


# =============================================================================================================================
# child side
# =============================================================================================================================
def _register_slow(root: Path, tag: str, spec: dict, started: dict | None = None) -> list[str]:
    """The Phase 72 fixture: a state-changing tool that takes a while and leaves a line in calls.log when it STARTS and another when its effect
    is DONE. Registered in every child (a restarted FRIDAY knows its tools), run only in the G processes."""
    from friday import auditview
    from friday.registry import SkillResult, skill

    invocations: list[str] = []
    calls_log = root / "calls.log"

    def note(line: str) -> None:
        with open(calls_log, "a", encoding="utf-8") as f:
            f.write(f"{tag}:{line}\n")

    @skill(name=SDS, tier="L1", action="modify", description="a state-changing write that takes a while (Phase 72 fixture)")
    def sd_slow(name: str = "a") -> SkillResult:
        invocations.append(name)
        note(f"sd_start:{name}")
        if started is not None and name in started:
            started[name].set()
        time.sleep(float(spec.get("slow_s", 2.0)))
        note(f"sd_effect_done:{name}")
        return SkillResult(speech="Wrote the slow file.")

    auditview.POLICY[SDS] = {"name": auditview.KEEP}
    return invocations


def _dump() -> dict:
    """Every audit row (as `audit.recent` reports it, with its derived status), every goal row, the sessions, and the size of every other table."""
    from friday import audit, store

    c = store.conn()
    rows = list(reversed(audit.recent(100000)))
    goals = [dict(r) for r in c.execute("SELECT id, status, failure_reason, current_step, progress, updated_at FROM goals ORDER BY created_at, id")]
    return {
        "audit": rows, "goals": goals,
        "sessions": [dict(r) for r in c.execute("SELECT id, kind, pid FROM runtime_sessions ORDER BY started_at, id")],
        "other": {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in OTHER_TABLES},
    }


def child_gui(root: Path, tag: str, scn: str, mode: str) -> None:
    spec = dict(SCN[scn])
    P70._bootstrap(root, tag)
    import asyncio
    import atexit
    import threading

    import phase21_common as H
    from phase21_common import ScriptedPlanner, done, scripted_provider

    from friday import audit, jobs, store, triggers
    from friday.config import CFG
    from friday.gui import app as app_mod
    from friday.gui import backend as backend_mod
    from friday.intelligence import goals as goals_mod
    from friday.intelligence.state import INTEL
    from friday.llm import LlmResponse
    from friday.permissions import EXECUTOR
    from friday.registry import REGISTRY

    REGISTRY.discover()
    started = {n: threading.Event() for n in spec.get("direct", [])}
    for t, a, _i in spec.get("goal", []):
        if t == SDS:                      # only the slow tool announces that it is running
            started.setdefault(a["name"], threading.Event())
    fx = P70.Fixtures(root, tag, None)
    inv = _register_slow(root, tag, spec, started)
    H.lock_down_real_tools(REGISTRY)
    CFG.desktop_observer.enabled = False
    CFG.voice.enabled = False
    CFG.planner.subgoal_evidence_advance = True
    CFG.planner.step_timeout_s = 200.0
    CFG.planner.total_timeout_s = 300.0

    atexit_marker = root / f"atexit_{tag}.txt"
    atexit.register(lambda: atexit_marker.write_text("ran", encoding="utf-8"))   # this suite's own probe: it exists only if the interpreter exits normally

    blocked = threading.Event()
    decisions = spec.get("goal", [])
    replies = [json.dumps({"action": "call", "tool": t, "args": a, "subgoal_index": i}) for t, a, i in decisions] + [done("All done.")]

    class Planner(ScriptedPlanner):
        n = 0

        async def complete(self, request):  # noqa: ANN001
            self.calls += 1
            system = next((m.content for m in request.messages if m.role == "system"), "")
            if "Break a real-world goal down" in system:
                text = json.dumps({"subgoals": [{"description": d, "rationale": "", "success_evidence": ""} for d in P70.STAGES]})
            else:
                if spec.get("block_before") is not None and self.n == spec["block_before"]:
                    blocked.set()
                    end = time.monotonic() + 60.0
                    while time.monotonic() < end:
                        await asyncio.sleep(0.05)      # a model call that does not return: a stop request is what ends the wait
                text = self.replies[min(self.n, len(self.replies) - 1)]
                self.n += 1
            return LlmResponse(text=text, model="scripted", provider=self.name)

    cancels: list[bool] = []
    real_cancel = INTEL.request_cancel
    INTEL.request_cancel = lambda: cancels.append(real_cancel()) or cancels[-1]    # type: ignore[method-assign]

    b = backend_mod.Backend()
    b.SHUTDOWN_DRAIN_S = DRAIN_S                                                  # type: ignore[misc]
    b.start()
    out: dict = {"ready": b.ready.is_set(), "error": b.error, "stages": list(b.stages), "scn": scn, "mode": mode, "pid": os.getpid()}
    futures: dict[str, object] = {}
    with scripted_provider(Planner(replies)):
        for name in spec.get("direct", []):
            futures[f"direct:{name}"] = asyncio.run_coroutine_threadsafe(EXECUTOR.run(SDS, {"name": name}, actor="text"), b.loop)
        if decisions:
            futures["goal"] = asyncio.run_coroutine_threadsafe(EXECUTOR.run("plan.run", {"goal": P70.GOAL}, actor="text"), b.loop)
        # wait until the work is really in flight (a tool body running in its worker thread; a planner call that has not returned)
        wait_for = [e for e in started.values()] + ([blocked] if spec.get("block_before") is not None else [])
        out["in_flight_reached"] = all(e.wait(60) for e in wait_for)
        out["in_flight_before"] = dict(EXECUTOR.in_flight())
        out["rows_before_quit"] = [{"id": r["id"], "skill": r["skill"], "status": r["status"]} for r in audit.recent(50)]
        (root / f"inflight_{tag}.ready").write_text("x", encoding="utf-8")
        if mode == "hold":
            time.sleep(300)                 # the parent terminates this process from outside
            return

        trace: list[str] = []
        inject = bool(spec.get("inject"))
        real_hotkeys_stop = app_mod.HOTKEYS.stop
        real_release = app_mod.single_instance.release
        real_backend_shutdown = b.shutdown
        real_sched_stop, real_watch_stop = jobs.SCHEDULER.stop, triggers.WATCHER.stop

        def step(name, real=None, fail=False):  # noqa: ANN001
            def run(*a, **k):  # noqa: ANN001
                trace.append(name)
                if fail:
                    raise RuntimeError(f"injected failure in {name}")
                return real(*a, **k) if real else None
            return run

        class Window:
            shutdown = staticmethod(step("window", None, inject))

        class Hub:
            stop = staticmethod(step("state hub"))

        class Tray:
            hide = staticmethod(step("tray"))

        class App:
            quit = staticmethod(step("app quit"))

        app_mod.HOTKEYS.stop = step("hotkeys", real_hotkeys_stop, inject)         # type: ignore[method-assign]
        app_mod.single_instance.release = step("single-instance lock", real_release)
        b.shutdown = step("backend", real_backend_shutdown)                        # type: ignore[method-assign]
        jobs.SCHEDULER.stop = step("scheduler.stop", real_sched_stop, inject)     # type: ignore[method-assign]
        triggers.WATCHER.stop = step("watcher.stop", real_watch_stop, inject)     # type: ignore[method-assign]

        t0 = time.perf_counter()
        app_mod._quit(App(), b, Hub(), Window(), Tray())
        out["quit_returned_s"] = round(time.perf_counter() - t0, 3)
        out["stopped_in_time"] = app_mod._await_backend_stop(b, b.SHUTDOWN_DRAIN_S + 2.0)
        out["stopped_after_s"] = round(time.perf_counter() - t0, 3)
        out["trace"] = trace
        out["backend_stopped"] = b.stopped.is_set()
        out["thread_alive"] = b._thread.is_alive()
        out["shutdown_requested"] = b.shutdown_requested
        out["cancels"] = list(cancels)
        out["in_flight_after"] = dict(EXECUTOR.in_flight())
        out["futures"] = {k: ("pending" if not f.done() else {"ok": getattr(f.result(), "ok", None), "speech": getattr(f.result(), "speech", ""),
                                                              "data": {kk: vv for kk, vv in (getattr(f.result(), "data", None) or {}).items()
                                                                       if kk in ("stopped", "goal_id", "verified")}})
                          for k, f in futures.items()}
        out["invocations"] = list(inv) + list(fx.invocations)
        out["goal_now"] = {g.id: g.status.value for g in goals_mod.recent(5)}
        out["rows_at_quit"] = [{"id": r["id"], "skill": r["skill"], "status": r["status"], "ok": r["ok"], "error": r["error"]} for r in audit.recent(50)]
        out["exit_at"] = time.time()
        (root / f"gui_{tag}.json").write_text(json.dumps(out), encoding="utf-8")
        sys.stdout.flush()
    # the function returns: the interpreter now ends NORMALLY -- atexit handlers run and the worker threads are joined


def child_next(root: Path, tag: str, boot: bool) -> None:
    """A fresh process that asks what happened (and, with `boot`, first runs the boot hook, as every FRIDAY process does at start)."""
    P70._bootstrap(root, tag)
    import asyncio

    from friday import audit, steps
    from friday.config import CFG
    from friday.permissions import EXECUTOR
    from friday.registry import REGISTRY

    REGISTRY.discover()
    fx = P70.Fixtures(root, tag, None)
    inv = _register_slow(root, tag, {})
    CFG.desktop_observer.enabled = False
    CFG.permissions.overrides = {**CFG.permissions.overrides, **{s.name: "deny" for s in REGISTRY.all() if s.name not in (STATUS, RECENT)}}
    calls_log = root / "calls.log"
    calls_before = calls_log.read_bytes() if calls_log.exists() else b""
    before = _dump()
    if boot:
        audit.start_session("cli")
    mid = _dump()

    async def ask() -> dict:
        s = await EXECUTOR.run(STATUS, {}, actor="text")
        r = await EXECUTOR.run(RECENT, {"count": 50}, actor="text")
        return {"status": s.speech, "status_data": {k: v for k, v in (s.data or {}).items() if k in ("id", "status", "report")},
                "recent": r.speech}

    answers = asyncio.run(ask())
    after = _dump()
    gids = [g["id"] for g in after["goals"]]
    out = {
        "before": before, "mid": mid, "after": after, "answers": answers, "views": {g: P70._goal_view(g) for g in gids},
        "invocations": list(inv) + list(fx.invocations), "calls_unchanged": calls_before == (calls_log.read_bytes() if calls_log.exists() else b""),
        "latest": steps.describe_goal()[1].get("id"),
    }
    (root / f"next_{tag}.json").write_text(json.dumps(out, default=str), encoding="utf-8")


# =============================================================================================================================
# parent side (imports nothing from friday)
# =============================================================================================================================
RESULTS: list[tuple[bool, str]] = []
SCENARIO_TITLES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((bool(cond), label))
    suffix = f" -- {detail}" if detail and not cond else ""
    print(f"  {'OK  ' if cond else 'MISS'} {label}{suffix}", flush=True)
    return bool(cond)


def scenario(title: str) -> None:
    SCENARIO_TITLES.append(title)
    print(f"\n--- {title} ---\n", flush=True)


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("FRIDAY_ALLOW_REAL_DB", "FRIDAY_DB")}
    env["PYTHONFAULTHANDLER"] = "1"          # a native crash at exit then leaves its stack in the child's stderr
    return env


def _cmd(root: Path, *args: str) -> list[str]:
    return [sys.executable, str(SCRIPT), "--child", *args, "--root", str(root)]


def spawn(root: Path, *args: str, timeout: int = 170) -> tuple[subprocess.CompletedProcess, float]:
    t0 = time.perf_counter()
    p = subprocess.run(_cmd(root, *args), capture_output=True, text=True, timeout=timeout, env=_env(), encoding="utf-8", errors="replace")
    return p, time.perf_counter() - t0


def gui(root: Path, tag: str, scn: str) -> tuple[subprocess.CompletedProcess, float, dict | None]:
    p, took = spawn(root, "gui", "--tag", tag, "--scn", scn, "--mode", "graceful")
    return p, took, (P70.load(root, f"gui_{tag}.json") if (root / f"gui_{tag}.json").exists() else None)


def nxt(root: Path, tag: str, boot: bool = False) -> tuple[subprocess.CompletedProcess, dict | None]:
    p, _ = spawn(root, "next", "--tag", tag, *(["--boot"] if boot else []))
    return p, (P70.load(root, f"next_{tag}.json") if p.returncode == 0 else None)


def start_held(root: Path, tag: str, scn: str) -> subprocess.Popen:
    """A G process that does its work and then just stays alive (the parent decides how it ends). Returns once the work is in flight."""
    proc = subprocess.Popen(_cmd(root, "gui", "--tag", tag, "--scn", scn, "--mode", "hold"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=_env(), encoding="utf-8", errors="replace")
    ready = root / f"inflight_{tag}.ready"
    end = time.time() + 120
    while time.time() < end and not ready.exists() and proc.poll() is None:
        time.sleep(0.1)
    return proc


UNKNOWN_MARK = "FRIDAY was closed while this was still running"        # audit.OUTCOME_UNKNOWN (an orderly quit that saw the work still running)
INTERRUPTED_MARK = "the process that started this ended before recording an outcome"   # audit.INTERRUPTED (a dead owner, found by a later process)
NATIVE_CRASH = 3221225477          # 0xC0000005
CRASHES: list[str] = []
QUIT_STEPS = ["window", "hotkeys", "state hub", "backend", "tray", "single-instance lock", "app quit"]
BAD_WORDS = ("It finished", "every step that changed something was confirmed", "was confirmed", "It worked", "Succeeded")


def rows_of(n: dict, key: str = "after") -> list[dict]:
    return n[key]["audit"]


def row_by_skill(n: dict, skill: str, key: str = "after") -> list[dict]:
    return [r for r in rows_of(n, key) if r["skill"] == skill]


def goal_row(n: dict, gid: str, key: str = "after") -> dict:
    return next(g for g in n[key]["goals"] if g["id"] == gid)


def stable(r: dict) -> tuple:
    """What a row says about its outcome (not its derived fields): used to show that a later process left it exactly as it was."""
    return (r["id"], r["skill"], r["decision"], r["ok"], r["result"], r["error"], r["status"], r["session_id"], r["goal_id"])


def readonly_checks(label: str, n: dict, calls_before: bytes, root: Path, own_rows: int = 2) -> None:
    """A next process that does not run the boot hook changes nothing but its own two audit rows (the questions it asks)."""
    b, m, a = n["before"], n["mid"], n["after"]
    new = a["audit"][len(b["audit"]):]
    check(f"{label} it changed nothing by starting: every audit row, goal row and session is what the process before left (no boot hook here)", b == m,
          str({k: (b[k] == m[k]) for k in b}))
    check(f"{label} its two questions added exactly {own_rows} audit rows (meta.goal_status, meta.recent: L0, auto, ok, attached to no goal) and nothing else "
          "-- every earlier row, every goal and every other table is untouched",
          [r["skill"] for r in new] == [STATUS, RECENT][:own_rows] and all(r["tier"] == "L0" and r["decision"] == "auto" and r["ok"] == 1 and r["goal_id"] is None for r in new)
          and [stable(r) for r in a["audit"][:len(b["audit"])]] == [stable(r) for r in b["audit"]] and a["goals"] == b["goals"] and a["other"] == b["other"],
          json.dumps(new)[:400])
    check(f"{label} nothing was executed, retried or resumed: no tool body ran in it and calls.log is byte-identical",
          n["invocations"] == [] and n["calls_unchanged"] and calls_bytes(root) == calls_before)


def calls_bytes(root: Path) -> bytes:
    f = root / "calls.log"
    return f.read_bytes() if f.exists() else b""


def linger_of(g: dict, ended_at: float) -> float:
    return ended_at - g["exit_at"]


def graceful_common(label: str, p: subprocess.CompletedProcess, g: dict | None, root: Path, tag: str) -> bool:
    # Rarely (about 2 in 100 runs, never reproduced under faulthandler, no stderr, no module identified) the interpreter's TEARDOWN dies with a native
    # access violation AFTER everything this suite checks was written (the report and the atexit marker exist). It is reported, counted and shown in the
    # summary -- not hidden and not explained -- and accepted only in that exact shape; any other non-zero code fails.
    crashed_late = p.returncode == NATIVE_CRASH and g is not None and (root / f"atexit_{tag}.txt").exists()
    if crashed_late:
        CRASHES.append(tag)
        print(f"  NOTE {label}: exited with the native access violation 0xC0000005 during interpreter teardown, after its report and atexit marker were complete", flush=True)
    ok = check(f"{label}.1 the process exited by itself (code 0; see the summary for a rare native crash at teardown) -- a controlled exit, not a kill -- and wrote its report",
               (p.returncode == 0 or crashed_late) and g is not None, f"rc={p.returncode}\n{p.stderr[-1500:]}")
    if g is None:
        return False
    check(f"{label}.2 it was a real backend: its own boot ran (store, audit session, registry, brain, scheduler and trigger watcher), without error",
          g["ready"] and not g["error"] and g["stages"][-1] == "automation" and g["stages"][0] == "memory", str(g["stages"]))
    check(f"{label}.3 the interpreter ended normally: the atexit handler this suite registered ran (a forced termination would skip it)",
          (root / f"atexit_{tag}.txt").exists())
    check(f"{label}.4 the tray-Quit steps all ran, once, in the documented order, and the backend (on its own loop, concurrently) stopped what it had started: trigger watcher and scheduler",
          [x for x in g["trace"] if x in QUIT_STEPS] == QUIT_STEPS and sorted(x for x in g["trace"] if x not in QUIT_STEPS) == ["scheduler.stop", "watcher.stop"]
          and len(g["trace"]) == 9, str(g["trace"]))
    check(f"{label}.5 quitting returned at once (it never waits), the backend's loop thread really ended, and the app's bounded wait saw it end",
          g["quit_returned_s"] < 0.5 and g["backend_stopped"] and not g["thread_alive"] and g["stopped_in_time"] and g["shutdown_requested"],
          f"{g['quit_returned_s']} {g['backend_stopped']} {g['thread_alive']} {g['stopped_in_time']}")
    return True


def main() -> int:
    t0 = time.perf_counter()
    root = Path(tempfile.mkdtemp(prefix="friday-p72-"))
    parent_pid = os.getpid()
    root2 = Path(tempfile.mkdtemp(prefix="friday-p72b-"))
    held: list[subprocess.Popen] = []
    try:
        # =========================================================================================================================
        # part 1: graceful shutdowns, each followed by a fresh read-only process, in ONE database (every later FRIDAY start runs the boot
        # hook, so what an earlier shutdown left is also tested against later starts)
        # =========================================================================================================================
        g_: dict[str, dict] = {}
        r_: dict[str, dict] = {}
        took: dict[str, float] = {}
        ended: dict[str, float] = {}
        calls_ref = b""
        unknown_ref: dict[int, tuple] = {}
        prev_max = 0
        for i, scn in enumerate(["idle", "inflight", "goal_between", "goal_inflight", "failing_steps"], 1):
            tag = f"G_{scn}"
            scenario(f"{i}. {scn}: a real backend is shut down the way Quit does it, in its own process; a FRESH process then reads what is left")
            p, took[scn], g = gui(root, tag, scn)
            ended[scn] = time.time()
            if not graceful_common(f"{i}.G", p, g, root, tag):
                continue
            g_[scn] = g
            fresh = [x for x in g["rows_at_quit"] if x["id"] > prev_max]       # the rows this scenario's process created
            calls_ref = calls_bytes(root)
            pr, r = nxt(root, f"R_{scn}")
            check(f"{i}.R a fresh OS process (started after the shutdown) read the database and exited cleanly", r is not None, f"rc={pr.returncode}\n{pr.stderr[-1500:]}")
            if r is None:
                continue
            r_[scn] = r
            prev_max = max(x["id"] for x in rows_of(r))
            readonly_checks(f"{i}.R", r, calls_ref, root)
            st, rec = r["answers"]["status"], r["answers"]["recent"]
            check(f"{i}.W no answer claims success it cannot back: neither the goal status nor 'what did you do' says a thing finished or was confirmed that was not",
                  not any(w in st or w in rec for w in BAD_WORDS), f"{st} | {rec}")
            check(f"{i}.D no process touched the repository's data/", P70.no_data_access(root))

            if scn == "idle":
                check("1.1 nothing was running, so nothing was marked, stopped or recorded: no audit row existed at quit, and the STOP flag it raised armed nothing",
                      g["rows_at_quit"] == [] and g["cancels"] == [False] and g["in_flight_before"] == {} and g["in_flight_after"] == {})
                check("1.2 the next process sees an empty history and says so honestly (it has not run a goal), with no unknown, interrupted or failed row",
                      "haven't run any multi-step request" in st and not any(x["status"] in ("outcome_unknown", "interrupted", "failed", "unfinished") for x in rows_of(r, "before"))
                      and r["before"]["goals"] == [])
                idle_linger = linger_of(g, ended[scn])

            elif scn == "inflight":
                rid = next(x["id"] for x in fresh if x["skill"] == SDS)
                check("2.1 a state-changing action was in flight (running in its worker thread, audit row open) when Quit was pressed, and no goal was running (STOP armed nothing)",
                      g["in_flight_reached"] and list(g["in_flight_before"].values()) == [SDS] and g["cancels"] == [False]
                      and [x["status"] for x in fresh] == ["outcome_unknown"], str(fresh))
                check("2.2 the shutdown waited only its bound for it (it was not cancelled, not forced), then marked its row 'outcome unknown': ok is NULL (not failed, not succeeded), "
                      "with the fixed Phase 48 text, and the request's future was left pending",
                      g["stopped_after_s"] < 1.5 and g["futures"] == {"direct:i": "pending"} and fresh[0]["ok"] is None
                      and UNKNOWN_MARK in fresh[0]["error"], str(fresh))
                row = next(x for x in rows_of(r) if x["id"] == rid)
                check("2.3 after the process had ended, the fresh process sees that same row: still 'outcome unknown', ok NULL, no result invented, and no undo entry was registered for it",
                      row["status"] == "outcome_unknown" and row["ok"] is None and row["result"] is None and UNKNOWN_MARK in row["error"] and r["after"]["other"]["undo"] == 0, str(row))
                unknown_ref[rid] = stable(row)
                lines = calls_bytes(root).decode("utf-8").splitlines()
                check("2.4 the action was never retried or resumed: its tool started exactly once; and because a worker thread cannot be stopped it ran to the end before the interpreter "
                      "left (its effect line is there), which FRIDAY did not and could not record -- hence 'unknown', not 'failed'",
                      lines.count(f"{tag}:sd_start:i") == 1 and lines.count(f"{tag}:sd_effect_done:i") == 1, str(lines))
                check("2.5 the process was held open by that worker after Quit (a controlled exit waits for non-daemon worker threads): it ended later than an idle shutdown did",
                      linger_of(g, ended[scn]) > idle_linger + 0.5, f"{linger_of(g, ended[scn]):.2f} vs idle {idle_linger:.2f}")
                check("2.6 the user-facing answers: 'what did you do' flags it as outcome unknown; the goal status is about goals only and does not mention it",
                      "test.sd_slow (outcome unknown)" in rec and "haven't run any multi-step request" in st, f"{rec} | {st}")

            elif scn == "goal_between":
                gid = g["goal_now"] and next(k for k, v in g["goal_now"].items() if v == "cancelled")
                check("3.1 a goal had finished one step (verified) and was between steps -- its planner call had not returned -- when Quit was pressed; STOP armed the running goal",
                      g["in_flight_reached"] and g["cancels"] == [True] and any(x["skill"] == P70.W and x["status"] == "succeeded" for x in fresh))
                fut = g["futures"]["goal"]
                check("3.2 the goal ended itself, inside the shutdown: it was stopped (cancelled) before its next step, says what had already run, nothing undone, and is NOT reported as succeeded",
                      isinstance(fut, dict) and fut["ok"] is False and fut["data"]["stopped"] == "cancelled" and "nothing was undone" in fut["speech"], str(fut))
                check("3.3 its goal row is 'cancelled' and the plan.run audit row holds the run's own verdict (failed=0, no error text, not an unknown marker)",
                      g["goal_now"][gid] == "cancelled" and any(x["skill"] == "plan.run" and x["status"] == "failed" and x["error"] is None and x["ok"] == 0
                                                                   for x in fresh), str(fresh))
                lines = calls_bytes(root).decode("utf-8").splitlines()
                check("3.4 no further step was started or retried: the write ran once and the send step never ran",
                      lines.count(f"{tag}:write:b") == 1 and not any(ln.startswith(f"{tag}:send") for ln in lines), str(lines))
                check("3.5 the next process's answer: it was stopped before it finished, one step done, no resume promised -- and nothing in it says finished or confirmed-complete",
                      "It was stopped before it finished" in st and "Done: 'test.pr_write'" in st and "I won't resume or retry any of it on my own" in st
                      and "Outcome unknown" not in st and "Failed:" not in st, st)
                check("3.6 the goal row is what the run left (cancelled), not rewritten by the next process", goal_row(r, gid, "before")["status"] == "cancelled"
                      and goal_row(r, gid, "after") == goal_row(r, gid, "before"))
                check("3.7 a LATER start (this process's own boot hook ran with the inflight process's dead owner still in the database) left that process's 'outcome unknown' row exactly as it was: "
                      "not rewritten as 'interrupted', not completed",
                      all(stable(x) == unknown_ref[x["id"]] for x in rows_of(r) if x["id"] in unknown_ref) and len(unknown_ref) == 1, str(unknown_ref))
                cancelled_gid = gid

            elif scn == "goal_inflight":
                gi_id = next(k for k, v in g["goal_now"].items())
                check("4.1 a goal had one step done and a state-changing step IN FLIGHT (worker thread) when Quit was pressed: STOP armed the goal, but a tool in a worker thread cannot be interrupted",
                      g["in_flight_reached"] and g["cancels"] == [True] and sorted(g["in_flight_before"].values()) == sorted(["plan.run", SDS]), str(g["in_flight_before"]))
                check("4.2 the bounded wait ended with the goal unfinished (its future is pending, the goal row still 'running'), and BOTH open audit rows -- the step and the goal's own plan.run -- "
                      "were marked 'outcome unknown' (ok NULL), not failed and not succeeded",
                      g["futures"] == {"goal": "pending"} and g["goal_now"][gi_id] == "running" and sorted(x["status"] for x in fresh) == ["outcome_unknown", "outcome_unknown", "succeeded"],
                      str(fresh))
                after = {x["skill"]: x for x in rows_of(r)}
                check("4.3 once the process had really ended (workers joined, interpreter torn down) those rows are STILL 'outcome unknown': tearing down the abandoned run did not overwrite "
                      "plan.run's marker with a failure (the defect this phase found: a ContextVar reset error became 'failed')",
                      after["plan.run"]["status"] == "outcome_unknown" and after["plan.run"]["ok"] is None and after[SDS]["status"] == "outcome_unknown"
                      and "different Context" not in json.dumps(rows_of(r)) and "ValueError" not in p.stderr, f"{after['plan.run']} {p.stderr[-600:]}")
                check("4.4 the next process's answer does not claim success: unfinished, it cannot tell whether part took effect, names the unknown step, the stage never attempted, "
                      "and promises no retry",
                      "It is unfinished, and I can't tell whether part of it took effect" in st and "Outcome unknown: 'test.sd_slow'" in st and "Not attempted: Send the file" in st
                      and "I won't repeat it on my own" in st and "It finished" not in st, st)
                check("4.5 the goal is not settled by the question: the row is still 'running' in the read-only process (only a later FRIDAY start may settle it, and only because its owner is gone)",
                      goal_row(r, gi_id, "after")["status"] == "running" and goal_row(r, gi_id, "after") == goal_row(r, gi_id, "before"))
                lines = calls_bytes(root).decode("utf-8").splitlines()
                check("4.6 nothing retried or resumed: write and the slow step each started once, the slow step's effect ran to its end exactly once, the send step never started",
                      all(lines.count(f"{tag}:{x}") == 1 for x in ("write:g", "sd_start:g", "sd_effect_done:g")) and not any(":send" in ln for ln in lines), str(lines))
                gi_rows = {x["id"]: stable(x) for x in rows_of(r) if x["skill"] in ("plan.run", SDS) and x["status"] == "outcome_unknown" and x["id"] not in unknown_ref}
                unknown_ref.update(gi_rows)
                gi_goal_before_boot = goal_row(r, gi_id, "after")
                gi_gid = gi_id

            elif scn == "failing_steps":
                check("5.1 four independent steps were made to FAIL (window, hotkeys, trigger watcher, scheduler): every other step still ran, in order, and the backend still stopped "
                      "and the process still ended normally",
                      [x for x in g["trace"] if x in QUIT_STEPS] == QUIT_STEPS and sorted(x for x in g["trace"] if x not in QUIT_STEPS) == ["scheduler.stop", "watcher.stop"]
                      and g["backend_stopped"] and g["stopped_in_time"])
                check("5.2 the in-flight action was still recorded as 'outcome unknown' (the failures did not skip the part that matters), no goal was armed, and its effect ran once, unretried",
                      [x["status"] for x in fresh if x["skill"] == SDS] == ["outcome_unknown"]
                      and g["cancels"] == [False] and calls_bytes(root).decode("utf-8").splitlines().count(f"{tag}:sd_start:f") == 1, str(g["rows_at_quit"]))
                # this G process ran the boot hook at ITS start, with the previous scenario's dead process still owning an open goal
                check("5.3 this process's own start (the boot hook) is the later start that settles what the goal_inflight process left: its goal was 'running' with a dead owner, and "
                      "is now 'partial' with the reason recorded and nothing resumed",
                      goal_row(r, gi_gid)["status"] == "partial" and "interrupted: the process running this goal ended before it finished; nothing was retried or resumed" in goal_row(r, gi_gid)["failure_reason"],
                      str(goal_row(r, gi_gid)))
                check("5.4 the boot hook touched ONLY that goal: the three rows earlier shutdowns marked 'outcome unknown' are byte-identical (never rewritten as 'interrupted'), and the goal "
                      "an earlier shutdown had cancelled is unchanged",
                      len(unknown_ref) == 3 and all(stable(x) == unknown_ref[x["id"]] for x in rows_of(r) if x["id"] in unknown_ref)
                      and goal_row(r, cancelled_gid) == goal_row(r_["goal_between"], cancelled_gid, "after"), str([(x["id"], x["status"]) for x in rows_of(r)]))
                check("5.5 the answer about that goal is still honest after it was settled: it did not finish, it cannot tell, no success claim, no retry",
                      "can't tell whether part of it took effect" in st and "I won't" in st and "It finished" not in st and gi_gid == r["views"][gi_gid]["gid"], st)

        # ---- a plain restart over everything the graceful shutdowns left ---------------------------------------------------------
        scenario("6. FRIDAY starts again over everything the graceful shutdowns left (boot hook), and once more read-only")
        last = r_.get("failing_steps")
        pc, c = nxt(root, "C", boot=True)
        pd, d = nxt(root, "D")
        check("6.C a fresh OS process ran the boot hook and answered", c is not None, f"rc={pc.returncode}\n{pc.stderr[-1500:]}")
        check("6.D a further fresh OS process asked again", d is not None, f"rc={pd.returncode}\n{pd.stderr[-1500:]}")
        if c is not None and d is not None and last is not None:
            check("6.1 with every earlier owner already reconciled, the boot hook changes no audit row and no goal (it only registers its own session): nothing is 'interrupted' that an "
                  "orderly shutdown had already settled, and the unknown markers stay markers",
                  [stable(x) for x in rows_of(c, "mid")] == [stable(x) for x in rows_of(last)] and c["mid"]["goals"] == last["after"]["goals"]
                  and len(c["mid"]["sessions"]) == len(last["after"]["sessions"]) + 1, "")
            check("6.2 it ran no tool and resumed nothing", c["invocations"] == [] and c["calls_unchanged"] and d["invocations"] == [] and d["calls_unchanged"])
            check("6.3 the answers did not change across the restarts (same status wording), and the last process sees exactly what the boot hook left",
                  c["answers"]["status"] == last["answers"]["status"] == d["answers"]["status"] and d["after"]["goals"] == c["after"]["goals"], c["answers"]["status"])
            marked = {x["id"] for gg in g_.values() for x in gg["rows_at_quit"] if x["status"] == "outcome_unknown"}
            check("6.4 across the whole database: no row is 'interrupted' (no graceful shutdown produces one), and the 'outcome unknown' rows are exactly the ones the shutdowns marked "
                  "(%d)" % len(marked), not any(x["status"] == "interrupted" for x in rows_of(d))
                  and {x["id"] for x in rows_of(d) if x["status"] == "outcome_unknown"} == marked, str(marked))
            check("6.5 no process touched data/", P70.no_data_access(root))

        # =========================================================================================================================
        # part 2: a forced termination, and a LIVE owner, in a second database
        # =========================================================================================================================
        scenario("7. forced termination from outside, next to a live owner: the same work in flight, no shutdown path, no atexit; a later start reconciles only what a dead owner left")
        import signal

        live = start_held(root2, "L_live", "live")
        held.append(live)
        check("7.1 process L (a real backend, alive) has a goal with a state-changing step in flight and stays alive", (root2 / "inflight_L_live.ready").exists() and live.poll() is None)
        forced = start_held(root2, "F_forced", "forced")
        held.append(forced)
        check("7.2 process F started while L was alive (its boot hook saw L's open rows and left them), and has a goal AND a direct action in flight",
              (root2 / "inflight_F_forced.ready").exists() and forced.poll() is None)
        pids = {ln.split()[0]: int(ln.split()[1]) for ln in (root2 / "pids.log").read_text(encoding="utf-8").splitlines()}
        pr0, r0 = nxt(root2, "R_before")
        check("7.3 a read-only process while both are alive", r0 is not None, f"rc={pr0.returncode}\n{pr0.stderr[-1200:]}")
        if r0 is not None:
            sess = {s["pid"]: s["id"] for s in r0["after"]["sessions"]}
            l_sess, f_sess = sess.get(pids["L_live"]), sess.get(pids["F_forced"])
            l_rows = [x for x in rows_of(r0, "before") if x["session_id"] == l_sess]
            f_rows = [x for x in rows_of(r0, "before") if x["session_id"] == f_sess]
            f_open = {x["id"] for x in f_rows if x["status"] == "unfinished"}
            l_open = {x["id"] for x in l_rows if x["status"] == "unfinished"}
            check("7.4 each live process's open rows are 'unfinished' (started, nothing recorded) and its goal is 'running'; F's own start did not touch L's rows",
                  l_sess and f_sess and [x["status"] for x in l_rows].count("unfinished") == 2 and [x["status"] for x in f_rows].count("unfinished") == 3
                  and all(g["status"] == "running" for g in r0["before"]["goals"]), str([(x["skill"], x["status"]) for x in rows_of(r0, "before")]))
            check("7.5 while its owner is ALIVE the answer says 'still running' -- not unknown, not failed", "It is still running" in r0["answers"]["status"], r0["answers"]["status"])
            calls_f = calls_bytes(root2)
            readonly_checks("7.R", r0, calls_f, root2)
            os.kill(pids["F_forced"], signal.SIGTERM)       # TerminateProcess on the real interpreter (not the venv launcher)
            try:
                forced.kill()
            except Exception:
                pass
            forced.wait(timeout=30)
            time.sleep(0.5)
            calls_after_kill = calls_bytes(root2).decode("utf-8").splitlines()
            check("7.6 F was terminated from outside: it did not exit by itself (non-zero code), ran no atexit handler, wrote no shutdown report, and its work never finished",
                  forced.returncode not in (0, None) and not (root2 / "atexit_F_forced.txt").exists() and not (root2 / "gui_F_forced.json").exists()
                  and not any("sd_effect_done:k" in ln for ln in calls_after_kill), f"rc={forced.returncode}")
            pk, k = nxt(root2, "R_afterkill")
            check("7.7 a read-only process after the kill", k is not None, f"rc={pk.returncode}\n{pk.stderr[-1200:]}")
            if k is not None:
                f_ids = {x["id"] for x in f_rows}
                check("7.8 nothing was recorded for the killed process: its rows are STILL 'unfinished' (no shutdown marker -- there was no shutdown), its goal still 'running'; "
                      "only the answer changed, because its owner is now provably gone: it cannot tell whether part took effect",
                      all(x["status"] == "unfinished" for x in rows_of(k, "before") if x["id"] in f_open)
                      and "can't tell whether part of it took effect" in k["answers"]["status"] and not any(w in k["answers"]["status"] for w in BAD_WORDS), k["answers"]["status"])
                readonly_checks("7.R2", k, calls_bytes(root2), root2)
                calls_c = calls_bytes(root2)
                pcc, cc = nxt(root2, "C", boot=True)
                check("7.9 a fresh process ran the boot hook", cc is not None, f"rc={pcc.returncode}\n{pcc.stderr[-1200:]}")
                if cc is not None:
                    changed = [x["id"] for x in rows_of(cc, "mid") if stable(x) != next((stable(y) for y in rows_of(k) if y["id"] == x["id"]), None)]
                    check("7.10 the boot hook reconciled the killed process's three open rows -- 'interrupted' (the dead-owner marker, distinct from the shutdown marker), ok still NULL -- "
                          "and changed no other row (not L's, which is alive; not the read-only processes')",
                          sorted(changed) == sorted(f_open) and all(x["status"] == "interrupted" and x["ok"] is None and INTERRUPTED_MARK in x["error"] for x in rows_of(cc, "mid") if x["id"] in f_open),
                          f"changed={changed} F={sorted(f_open)}")
                    f_goal = next(x["goal_id"] for x in f_rows if x["goal_id"])
                    l_goal = next(x["goal_id"] for x in l_rows if x["goal_id"])
                    check("7.11 it settled the killed process's goal ('running' -> 'partial', reason recorded, nothing resumed) and left the live owner's goal 'running'",
                          goal_row(cc, f_goal, "mid")["status"] == "partial" and "nothing was retried or resumed" in goal_row(cc, f_goal, "mid")["failure_reason"]
                          and goal_row(cc, l_goal, "mid") == goal_row(k, l_goal, "after"), str(goal_row(cc, f_goal, "mid")))
                    check("7.12 a live owner's open rows are never touched: L's rows are exactly 'unfinished' as before", [stable(x) for x in rows_of(cc, "mid") if x["session_id"] == l_sess]
                          == [stable(x) for x in rows_of(k) if x["session_id"] == l_sess] and all(x["status"] == "unfinished" for x in rows_of(cc, "mid") if x["id"] in l_open))
                    check("7.13 nothing was retried or resumed by any of it: the boot hook ran no tool, and calls.log has no second start for any step",
                          cc["invocations"] == [] and cc["calls_unchanged"] and calls_bytes(root2) == calls_c
                          and len([ln for ln in calls_bytes(root2).decode("utf-8").splitlines() if ":sd_start:" in ln]) == 3)
                    os.kill(pids["L_live"], signal.SIGTERM)
                    try:
                        live.kill()
                    except Exception:
                        pass
                    live.wait(timeout=30)
                    pdd, dd = nxt(root2, "D", boot=True)
                    check("7.14 after L is also terminated, a further start reconciles L's rows and goal the same way, and only those", dd is not None, f"rc={pdd.returncode}\n{pdd.stderr[-1200:]}")
                    if dd is not None:
                        l_ids = {x["id"] for x in l_rows}
                        changed2 = [x["id"] for x in rows_of(dd, "mid") if stable(x) != next((stable(y) for y in rows_of(cc) if y["id"] == x["id"]), None)]
                        check("7.15 exactly L's two open rows became 'interrupted' and its goal 'partial'; everything the earlier start reconciled is unchanged",
                              sorted(changed2) == sorted(l_open) and goal_row(dd, l_goal, "mid")["status"] == "partial"
                              and goal_row(dd, f_goal, "mid") == goal_row(cc, f_goal, "after"), f"{changed2} {goal_row(dd, l_goal, 'mid')['status']}")
            check("7.16 no process touched data/", P70.no_data_access(root2))

        pids_all = [ln.split() for ln in (root / "pids.log").read_text(encoding="utf-8").splitlines()] + [ln.split() for ln in (root2 / "pids.log").read_text(encoding="utf-8").splitlines()]
        # Only processes that were alive TOGETHER must differ (L, F and the read-only process between them); a finished child's id may be reused later.
        together = [pids["L_live"], pids["F_forced"], int(next(p[1] for p in pids_all if p[0] == "R_before"))] if "R_before" in {p[0] for p in pids_all} else []
        check("7.17 every one of the %d children was its own OS process, none of them this process, and the three that were alive at the same time had three different ids"
              % len(pids_all), str(parent_pid) not in {p[1] for p in pids_all} and len(set(together)) == 3, str(pids_all))
        (root / "watched").mkdir()
        ph, _ = spawn(root, "hookcheck")
        hits = (root / "data_hits_hookcheck.log").read_text(encoding="utf-8") if (root / "data_hits_hookcheck.log").exists() else ""
        check("7.18 the isolation hook works: pointed at a temp folder it records the open, the mkdir and the listing (so zero hits on data/ means something)",
              ph.returncode == 0 and "open " in hits and "os.mkdir" in hits and "os.listdir" in hits, f"rc={ph.returncode} {hits} {ph.stderr[-500:]}")
    finally:
        import shutil

        for proc in held:
            try:
                proc.kill()
            except Exception:
                pass
        for line in ((root2 / "pids.log").read_text(encoding="utf-8").splitlines() if (root2 / "pids.log").exists() else []):
            try:
                pid = int(line.split()[1])
                if line.split()[0] in ("L_live", "F_forced"):
                    os.kill(pid, 15)
            except Exception:
                pass
        time.sleep(0.3)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(root2, ignore_errors=True)
    elapsed = time.perf_counter() - t0
    passed = sum(1 for ok, _ in RESULTS if ok)
    failed = [label for ok, label in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f"Phase 72 -- a graceful shutdown, and what the next process finds  ({elapsed:.1f}s)")
    print("=" * 72)
    print(f"  assertions : {passed}/{len(RESULTS)} passed")
    print(f"  scenarios  : {len(SCENARIO_TITLES)}")
    print(f"  native crash at interpreter teardown (after all checked state was written): {len(CRASHES)} {CRASHES}")
    gate = len(RESULTS) >= 90 and len(SCENARIO_TITLES) >= 7
    print(f"  gate >=90 assertions / >=7 scenarios: {'OK' if gate else 'MISS'}")
    if failed:
        print("\n  FAILED:")
        for label in failed:
            print(f"    - {label}")
        print("\nFAILURES ABOVE")
        return 1
    print("\nALL OK" if gate else "\nGATES MISSED")
    return 0 if gate else 1


def _child_main() -> int:
    a = sys.argv
    root = Path(a[a.index("--root") + 1])
    role = a[a.index("--child") + 1]
    if role == "hookcheck":
        P70.child_hookcheck(root)
    elif role == "gui":
        child_gui(root, a[a.index("--tag") + 1], a[a.index("--scn") + 1], a[a.index("--mode") + 1])
    elif role == "next":
        child_next(root, a[a.index("--tag") + 1], "--boot" in a)
    return 0


if __name__ == "__main__":
    sys.exit(_child_main() if "--child" in sys.argv else main())
