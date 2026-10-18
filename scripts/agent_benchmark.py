"""FRIDAY real-environment agent benchmark (Phase 29.0).

Measures how often the CURRENT FRIDAY completes realistic, state-changing tasks, end to end, through
the real production path -- normal routing (BRAIN), `plan.run`, the real local qwen2.5:3b planner,
the normal Executor / permissions / confirmation / intent guard / post-condition verification /
evidence-grounded completion -- in an isolated environment (see scripts/bench_env.py). Tasks are
scored on the FINAL ENVIRONMENT STATE, not on FRIDAY's words (see scripts/bench_tasks.py).

    .venv/Scripts/python.exe -X utf8 scripts/agent_benchmark.py                 # full suite, 3 runs each
    .venv/Scripts/python.exe -X utf8 scripts/agent_benchmark.py --list
    .venv/Scripts/python.exe -X utf8 scripts/agent_benchmark.py --prove-only    # isolation preflight only
    .venv/Scripts/python.exe -X utf8 scripts/agent_benchmark.py --category fs,system --reps 1
    .venv/Scripts/python.exe -X utf8 scripts/agent_benchmark.py --tasks fs_copy,ui_click
    .venv/Scripts/python.exe -X utf8 scripts/agent_benchmark.py --out benchmarks/mine.json [--resume]
    .venv/Scripts/python.exe -X utf8 scripts/agent_benchmark.py --report benchmarks/mine.json [--review benchmarks/review.json]
    .venv/Scripts/python.exe -X utf8 scripts/agent_benchmark.py --report new.json --compare benchmarks/baseline_phase29.json

UI tasks drive the REAL mouse/keyboard against the benchmark's own throwaway window for a few seconds
each (like scripts/smoke_ui_live.py); use `--category fs,browser,system` to avoid that.

Exit codes: 0 ran to completion, 1 a regression versus --compare (with --gate), 2 isolation could not be
proven / the environment is unusable (nothing was run).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

CATEGORIES = ["fs", "browser", "ui", "system"]
ORDER = {"system": 0, "fs": 1, "browser": 2, "ui": 3}
FAILURE_CLASSES = [
    "planner/reasoning", "intent/routing", "tool implementation", "permission/guard",
    "verification/postcondition", "environment/harness", "other",
]
RUN_TIMEOUT_S = 330  # above plan.run's own 300 s total_timeout_s


@dataclass
class Trace:
    utterance: str = ""
    routed_skill: str | None = None
    routed_action: str | None = None
    result_ok: bool = False
    speech: str = ""
    data: dict = field(default_factory=dict)
    stopped: str | None = None
    audit: list[dict] = field(default_factory=list)
    confirms: list[dict] = field(default_factory=list)
    events: list[tuple[str, dict]] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    pending_left: str | None = None
    exception: str | None = None
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)  # blocked read-only probes outside the sandbox (recorded, not failures)

    @property
    def steps(self) -> list[dict]:
        """Tool calls that were attempted (plan.run itself excluded), from the audit trail."""
        return [r for r in self.audit if r["skill"] != "plan.run"]


# -- running one task ------------------------------------------------------------------------------------


def _json_safe(value: Any, limit: int = 160) -> Any:
    s = json.dumps(value, default=str, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


async def run_one(task, rep: int, iso, env_cls, runtime) -> dict[str, Any]:
    from bench_tasks import HarnessError, Skip
    from friday import store
    from friday.bus import BUS
    from friday.intelligence.context_memory import CONTEXT
    from friday.intelligence.state import INTEL
    from friday.session import SESSION

    work = iso.fresh_work()
    iso.wipe_db_state()
    iso.violations.clear()
    iso.ui_allowed, iso.ui_baseline = set(), set()
    if iso.site:
        iso.site.reset()
    if runtime["browser"] and task.category == "browser":
        await reset_browser()
    SESSION.pending = None
    SESSION.last_utterance, SESSION.last_skill, SESSION.last_args, SESSION.last_data = "", None, {}, {}
    SESSION._followup = None
    INTEL.reset()
    CONTEXT.reset()

    env = env_cls(iso=iso, work=work, tag=f"{rep}{os.getpid() % 1000}")
    trace = Trace()
    rec: dict[str, Any] = {"task": task.id, "category": task.category, "run": rep, "status": "RUN"}
    keep = {"brain.understood", "session.awaiting_confirm", "permission.denied", "permission.declined",
            "orchestrator.done", "session.awaiting_slot", "session.clarifying", "session.no_antecedent",
            "session.unknown", "session.context_ambiguous", "session.awaiting_confirm"}
    answer_tasks: list[asyncio.Task] = []

    async def on_event(ev):
        if ev.topic not in keep:
            return
        trace.events.append((ev.topic, {k: v for k, v in ev.data.items() if k in ("action", "skill", "args", "stopped", "ok", "preview", "reason", "slot")}))
        if ev.topic == "brain.understood" and trace.routed_skill is None and trace.routed_action is None:
            trace.routed_skill, trace.routed_action = ev.data.get("skill"), ev.data.get("action")
        if ev.topic == "session.awaiting_confirm":
            ans = "no" if task.confirm == "decline" else "yes"
            trace.confirms.append({"skill": ev.data.get("skill"), "preview": str(ev.data.get("preview"))[:160], "answer": ans})

            async def reply() -> None:
                await asyncio.sleep(0.3)
                await SESSION.handle(ans, actor="text")

            answer_tasks.append(asyncio.create_task(reply()))

    BUS.subscribe("*", on_event)
    start_id = store.conn().execute("SELECT COALESCE(MAX(id),0) FROM audit").fetchone()[0]
    try:
        try:
            if task.setup:
                await asyncio.to_thread(task.setup, env)
        except Skip as exc:
            rec.update(status="SKIP", reason=str(exc))
            return rec
        except HarnessError as exc:
            rec.update(status="FAIL", passed=False, failure_class="environment/harness", auto_failure_class="environment/harness",
                       reason=f"setup: {exc}", elapsed_s=0.0, steps=0, claimed_ok=False, tools=[], confirms=[], violations=[])
            return rec

        trace.utterance = task.render(env)
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(SESSION.handle(trace.utterance, actor="text"), timeout=RUN_TIMEOUT_S)
            trace.result_ok, trace.speech, trace.data = bool(res.ok), res.speech or "", dict(res.data or {})
            trace.stopped = trace.data.get("stopped")
        except asyncio.TimeoutError:
            trace.exception = f"run exceeded {RUN_TIMEOUT_S}s and was cancelled"
        except Exception as exc:  # noqa: BLE001 -- anything here is a harness/crash finding
            trace.exception = f"{type(exc).__name__}: {exc}"
        trace.elapsed_s = time.perf_counter() - t0
        if answer_tasks:
            await asyncio.gather(*answer_tasks, return_exceptions=True)
        await asyncio.sleep(0.3)

        trace.pending_left = SESSION.pending.kind if SESSION.pending is not None else None
        trace.audit = [dict(r) for r in store.conn().execute("SELECT * FROM audit WHERE id>? ORDER BY id", (start_id,)).fetchall()]
        trace.violations = [v for v in iso.violations if v.get("mutating", True)]
        trace.notes = [v["detail"] for v in iso.violations if not v.get("mutating", True)]

        verdict = None
        try:
            from bench_tasks import Verdict

            v = task.check(env, trace)
            verdict = await v if asyncio.iscoroutine(v) else v
        except Exception as exc:  # noqa: BLE001
            verdict = Verdict(False, f"check raised {type(exc).__name__}: {exc}")
            trace.exception = trace.exception or f"check error: {exc}"

        problems: list[str] = []
        if not verdict.ok:
            problems.append(verdict.reason)
        if trace.violations:
            problems.append("sandbox violation attempted: " + "; ".join(v["detail"] for v in trace.violations[:3]))
        if task.confirm in ("approve", "decline") and not trace.confirms:
            problems.append("a confirmation was expected but never requested")
        passed = not problems
        claimed = _claims(trace)
        category, reason = ("", "")
        if not passed:
            category, reason = classify(task, trace, verdict, problems)
        rec.update(
            status="PASS" if passed else "FAIL", passed=passed, elapsed_s=round(trace.elapsed_s, 1),
            steps=len(trace.steps), claimed_ok=claimed, state_ok=bool(verdict.ok),
            false_success=(not passed) and claimed, false_failure=passed and not claimed,
            routed=trace.routed_skill, stopped=trace.stopped, pending_left=trace.pending_left,
            failure_class=category, auto_failure_class=category, reason=reason or "; ".join(problems),
            confirms=trace.confirms, unexpected_confirm=bool(trace.confirms) and task.confirm == "none",
            violations=[v["detail"] for v in trace.violations], blocked_probes=trace.notes,
            tools=[{"tool": r["skill"], "args": _json_safe(json.loads(r["args"]) if r["args"] else {}),
                    "decision": r["decision"], "ok": r["ok"], "result": (r["result"] or r["error"] or "")[:140]}
                   for r in trace.steps],
            speech=trace.speech[:300], exception=trace.exception,
        )
        return rec
    finally:
        BUS.unsubscribe("*", on_event)
        await teardown(env, iso)
        SESSION.pending = None
        INTEL.reset()


def _claims(trace: Trace) -> bool:
    from bench_tasks import claims_success

    return claims_success(trace)


async def reset_browser() -> None:
    from friday import browser

    if browser.is_open():
        try:
            await browser._state.page.goto("about:blank")
            await browser._state.context.clear_cookies()
        except Exception:  # noqa: BLE001
            pass


async def teardown(env, iso) -> None:
    import win32con
    import win32gui

    from bench_env import windows_titled

    for h in (env.ui, env.cover):
        if h is not None:
            await asyncio.to_thread(h.stop)
    for w in windows_titled("Calculator"):
        if w["hwnd"] not in iso.ui_baseline:
            try:
                win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass
    if iso.ui_baseline:
        deadline = time.time() + 10
        while time.time() < deadline and any(w["hwnd"] not in iso.ui_baseline for w in windows_titled("Calculator")):
            await asyncio.sleep(0.3)
    iso.ui_allowed, iso.ui_baseline = set(), set()


# -- failure classification (provisional: every failure is then reviewed by hand) ---------------------------


def classify(task, trace: Trace, verdict, problems: list[str]) -> tuple[str, str]:
    """Rules, in order. The result is a PROVISIONAL label -- a tool failing is not automatically a planner
    failure -- so every failing run is read and the label confirmed or corrected (see --review)."""
    steps = trace.steps
    tools_run = [s["skill"] for s in steps]
    if trace.exception:
        if "timeout" in trace.exception or "exceeded" in trace.exception:
            return "other", f"run timed out: {trace.exception}"
        return "environment/harness", trace.exception
    if trace.violations:
        return "planner/reasoning", "acted outside the sandbox: " + "; ".join(v["detail"] for v in trace.violations[:2])
    if task.confirm in ("approve", "decline") and not trace.confirms:
        risky = [s for s in steps if s["skill"] in task.expected_tools and s["decision"] == "auto"]
        if risky:
            return "permission/guard", f"{risky[0]['skill']} ran WITHOUT the expected confirmation"
    blocked = [s for s in steps if s["decision"] == "blocked"]
    if blocked and trace.routed_skill == blocked[0]["skill"] and trace.routed_skill != "plan.run":
        return "intent/routing", f"BRAIN routed the request straight to {trace.routed_skill!r} (hard-denied by the benchmark; not a tool for this task)"
    if blocked:
        return "planner/reasoning", f"chose a tool outside the safe toolset (hard-denied by the benchmark): {sorted({b['skill'] for b in blocked})}"
    plan_steps = trace.data.get("steps") or []
    mism = [s for s in plan_steps if s.get("error") == "intent_mismatch"]
    if mism and any(s["tool"] in task.expected_tools for s in mism):
        return "permission/guard", f"the intent guard rejected an appropriate tool: {mism[0]['tool']}"
    if any(s.get("error") == "postcondition_failed" for s in plan_steps):
        return "verification/postcondition", "a post-condition check failed a step"
    if trace.stopped == "timeout":
        return "other", "the plan hit its total time limit"
    if trace.stopped in ("step_limit", "repeated_action", "planning_failed", "failure", "evidence_exhausted", "intent_mismatch"):
        return "planner/reasoning", f"the plan ended '{trace.stopped}' without completing the task"
    if trace.pending_left:
        kind = "intent/routing" if trace.routed_skill != "plan.run" else "planner/reasoning"
        return kind, f"stopped to ask the user a '{trace.pending_left}' question"
    if trace.routed_skill != "plan.run":
        if trace.routed_action in (None, "unknown", "clarify", "ask_slot") or trace.routed_skill not in task.expected_tools:
            return "intent/routing", f"direct route to {trace.routed_skill!r} ({trace.routed_action}); expected one of {sorted(task.expected_tools)}"
        return "intent/routing", f"direct route to {trace.routed_skill} (argument extraction), state wrong: {problems[0][:100]}"
    if not steps:
        return "planner/reasoning", "the plan ran no tool at all"
    if any(s["ok"] == 0 for s in steps) and any(s["skill"] in task.expected_tools for s in steps):
        return "other", "an appropriate tool was called but failed -- needs review (tool defect vs bad arguments)"
    return "planner/reasoning", f"tools ran ({sorted(set(tools_run))}) but the end state is wrong: {problems[0][:100]}"


# -- reporting --------------------------------------------------------------------------------------------


def _rate(runs: list[dict]) -> tuple[int, int]:
    scored = [r for r in runs if r["status"] in ("PASS", "FAIL")]
    return sum(1 for r in scored if r["status"] == "PASS"), len(scored)


def apply_review(runs: list[dict], review: dict[str, dict]) -> None:
    for r in runs:
        key = f"{r['task']}#{r['run']}"
        if r["status"] == "FAIL" and key in review:
            r["failure_class"] = review[key]["category"]
            r["reason"] = review[key].get("reason", r.get("reason", ""))
            r["reviewed"] = True


def summarize(data: dict) -> dict[str, Any]:
    runs = data["runs"]
    passed, total = _rate(runs)
    by_cat = {c: _rate([r for r in runs if r["category"] == c]) for c in CATEGORIES}
    by_task: dict[str, tuple[int, int]] = {}
    for tid in dict.fromkeys(r["task"] for r in runs):
        by_task[tid] = _rate([r for r in runs if r["task"] == tid])
    fails = [r for r in runs if r["status"] == "FAIL"]
    classes = Counter(r.get("failure_class") or "other" for r in fails)
    scored = [r for r in runs if r["status"] in ("PASS", "FAIL")]
    from bench_tasks import EXPECTS_FAILURE

    return {
        "passed": passed, "total": total, "by_cat": by_cat, "by_task": by_task, "classes": classes,
        "false_success": sum(1 for r in fails if r.get("false_success")),
        "false_failure": sum(1 for r in scored if r.get("false_failure") and r["task"] not in EXPECTS_FAILURE),
        "unexpected_confirms": sum(1 for r in scored if r.get("unexpected_confirm")),
        "skipped": sum(1 for r in runs if r["status"] == "SKIP"),
        "mean_s": (sum(r.get("elapsed_s", 0) for r in scored) / len(scored)) if scored else 0.0,
        "mean_steps": (sum(r.get("steps", 0) for r in scored) / len(scored)) if scored else 0.0,
        "violations": sum(len(r.get("violations", [])) for r in scored),
    }


def _pct(p: int, t: int) -> str:
    return f"{(100 * p / t):5.1f}%" if t else "  n/a"


def print_report(data: dict, markdown: bool = False) -> None:
    s = summarize(data)
    meta = data.get("meta", {})
    print("\n" + "=" * 78)
    print(f"FRIDAY agent benchmark -- {s['total']} scored runs "
          f"({len(s['by_task'])} tasks x up to {meta.get('reps', '?')} runs), model {meta.get('model', '?')}")
    print("=" * 78)
    print(f"overall pass rate : {s['passed']}/{s['total']} = {_pct(s['passed'], s['total'])}")
    for c in CATEGORIES:
        p, t = s["by_cat"][c]
        if t:
            print(f"  {c:<8}: {p:>3}/{t:<3} = {_pct(p, t)}")
    print(f"mean time/run     : {s['mean_s']:.1f}s    mean steps/run: {s['mean_steps']:.1f}")
    if meta.get("duration_s"):
        print(f"suite duration    : {meta['duration_s'] / 60:.1f} min")
    print(f"false-success runs: {s['false_success']} (FRIDAY claimed success but the state was wrong)")
    print(f"false-failure runs: {s['false_failure']} (state was right but FRIDAY did not claim success)")
    print(f"unexpected confirmations: {s['unexpected_confirms']}   sandbox violations attempted: {s['violations']}   skipped: {s['skipped']}")
    print("\nper task:")
    for tid, (p, t) in s["by_task"].items():
        cat = next(r["category"] for r in data["runs"] if r["task"] == tid)
        print(f"  {tid:<24} {cat:<8} {p}/{t}  {_pct(p, t)}")
    if s["classes"]:
        print("\nfailure classes:")
        for cls, n in s["classes"].most_common():
            print(f"  {cls:<28} {n}")
    if markdown:
        print("\n| task | category | pass | rate |\n|---|---|---|---|")
        for tid, (p, t) in s["by_task"].items():
            cat = next(r["category"] for r in data["runs"] if r["task"] == tid)
            print(f"| {tid} | {cat} | {p}/{t} | {_pct(p, t).strip()} |")


def print_failures(data: dict) -> None:
    for r in data["runs"]:
        if r["status"] != "FAIL":
            continue
        print(f"\n--- {r['task']} run {r['run']}  [{r.get('failure_class')}]  {r.get('elapsed_s', 0)}s  routed={r.get('routed')} stopped={r.get('stopped')}")
        print(f"    reason : {r.get('reason')}")
        for t in r.get("tools", []):
            print(f"    step   : {t['tool']} {t['args']} -> {t['decision']}/{t['ok']} {t['result']!r}")
        for c in r.get("confirms", []):
            print(f"    confirm: {c['skill']} -> {c['answer']}")
        print(f"    speech : {r.get('speech')!r}")


def compare(new: dict, base: dict) -> bool:
    sn, sb = summarize(new), summarize(base)
    print("\n" + "=" * 78 + "\ncomparison against baseline\n" + "=" * 78)
    po, to = sb["passed"], sb["total"]
    pn, tn = sn["passed"], sn["total"]
    print(f"overall  baseline {_pct(po, to)} ({po}/{to})  ->  now {_pct(pn, tn)} ({pn}/{tn})")
    regress = (tn and to and pn / tn < po / to - 1e-9)
    for c in CATEGORIES:
        (bp, bt), (np_, nt) = sb["by_cat"][c], sn["by_cat"][c]
        if bt or nt:
            print(f"  {c:<8} {_pct(bp, bt)} -> {_pct(np_, nt)}")
    print("per task changes:")
    changed = False
    for tid in dict.fromkeys([*sb["by_task"], *sn["by_task"]]):
        bp, bt = sb["by_task"].get(tid, (0, 0))
        np_, nt = sn["by_task"].get(tid, (0, 0))
        if bt and nt and abs(bp / bt - np_ / nt) > 1e-9:
            changed = True
            print(f"  {tid:<24} {bp}/{bt} -> {np_}/{nt}  {'REGRESSION' if np_ / nt < bp / bt else 'improved'}")
    if not changed:
        print("  (no per-task change)")
    return bool(regress)



async def selftest(iso, tasks, env_cls, runtime) -> int:
    """The benchmark's own controls (no planner involved): every task's check must FAIL on an untouched
    environment (unless doing nothing IS correct) and PASS after a known-correct solution performed through
    the real skills. A check that cannot tell those apart would make the baseline meaningless."""
    import bench_oracles
    from bench_tasks import HarnessError, Skip, Verdict
    from friday.permissions import EXECUTOR

    async def approve(skill, args, preview):  # the Executor's normal confirmation channel, answered "yes"
        return True

    EXECUTOR.set_confirm_handler(approve)
    bad: list[str] = []

    async def fresh(task, tag):
        work = iso.fresh_work()
        iso.wipe_db_state()
        iso.violations.clear()
        iso.ui_allowed, iso.ui_baseline = set(), set()
        if iso.site:
            iso.site.reset()
        if runtime["browser"] and task.category == "browser":
            await reset_browser()
        env = env_cls(iso=iso, work=work, tag=tag)
        if task.setup:
            await asyncio.to_thread(task.setup, env)
        return env

    async def judge(task, env, trace):
        v = task.check(env, trace)
        return await v if asyncio.iscoroutine(v) else v

    for task in tasks:
        env = None
        try:
            env = await fresh(task, "n")
            neg = await judge(task, env, Trace(utterance=task.render(env), result_ok=False, speech=""))
            want_fail = task.id not in bench_oracles.UNTOUCHED_OK
            neg_ok = (not neg.ok) if want_fail else neg.ok
            await teardown(env, iso)
            env = await fresh(task, "p")
            speech, ok = await bench_oracles.ORACLES[task.id](env)
            await asyncio.sleep(0.4)
            pos = await judge(task, env, Trace(utterance=task.render(env), result_ok=ok, speech=speech))
            line = f"  {task.id:<24} untouched->{'FAIL' if not neg.ok else 'pass'} ({'ok' if neg_ok else 'WRONG'})   correct->{'PASS' if pos.ok else 'fail'} ({'ok' if pos.ok else 'WRONG'})"
            print(line, flush=True)
            if not neg_ok:
                bad.append(f"{task.id}: check gives {'pass' if neg.ok else 'fail'} on an untouched environment ({neg.reason})")
            if not pos.ok:
                bad.append(f"{task.id}: check REJECTS a correct solution: {pos.reason}")
        except Skip as exc:
            print(f"  {task.id:<24} skipped: {exc}")
        except (HarnessError, Exception) as exc:  # noqa: BLE001
            print(f"  {task.id:<24} SELFTEST ERROR {type(exc).__name__}: {exc}")
            bad.append(f"{task.id}: selftest error {type(exc).__name__}: {exc}")
        finally:
            if env is not None:
                await teardown(env, iso)
    leaked = iso.violations
    print(f"\nselftest: {len(tasks) - len({b.split(':')[0] for b in bad})}/{len(tasks)} tasks clean; sandbox violations during controls: {len(leaked)}")
    for b in bad:
        print("  PROBLEM:", b)
    return 1 if bad else 0


# -- main -------------------------------------------------------------------------------------------------


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


async def amain(args: argparse.Namespace) -> int:
    import bench_tasks
    from bench_env import Isolation, IsolationError

    tasks = list(bench_tasks.TASKS)
    if args.category:
        want = set(args.category.split(","))
        tasks = [t for t in tasks if t.category in want]
    if args.tasks:
        ids = args.tasks.split(",")
        missing = [i for i in ids if i not in bench_tasks.BY_ID]
        if missing:
            print(f"unknown task id(s): {missing}")
            return 2
        tasks = [bench_tasks.BY_ID[i] for i in ids]
    if not tasks:
        print("no tasks selected")
        return 2
    tasks.sort(key=lambda t: ORDER[t.category])

    need_browser = any(t.category == "browser" for t in tasks)
    need_ui = any(t.category == "ui" for t in tasks)
    iso = Isolation(browser=need_browser, ui=need_ui)
    try:
        iso.start()
    except IsolationError as exc:
        print(f"ISOLATION NOT PROVEN -- nothing was run: {exc}")
        try:
            iso.stop()
        except Exception:  # noqa: BLE001
            pass
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ISOLATION SETUP FAILED -- nothing was run: {type(exc).__name__}: {exc}")
        try:
            iso.stop()
        except Exception:  # noqa: BLE001
            pass
        return 2

    code = 0
    try:
        from friday import llm
        from friday.brain import BRAIN
        from friday.config import CFG

        print("isolation proven:")
        for k, v in iso.facts.items():
            print(f"  {k}: {v}")
        if need_browser:
            try:
                await iso.prove_browser()
                print("  browser guard: a non-local request is blocked; the mock site is reachable")
            except IsolationError as exc:
                print(f"ISOLATION NOT PROVEN -- nothing was run: {exc}")
                return 2
            except Exception as exc:  # noqa: BLE001
                print(f"BROWSER UNAVAILABLE -- cannot run browser tasks: {type(exc).__name__}: {exc}")
                return 2
        if args.prove_only:
            print("--prove-only: isolation preflight passed; no task was run.")
            return 0

        if args.selftest:
            print("\nbenchmark self-test (positive and negative controls, no planner):")
            code = await selftest(iso, tasks, bench_tasks.Env, {"browser": need_browser})
            print(f"final isolation checks: {iso.final_checks()}")
            return code

        if not await llm.ping():
            print("Ollama is not reachable -- the benchmark needs the real planner. Nothing was run.")
            return 2
        model = CFG.planner.model or CFG.llm.model
        print(f"warming up {model} ...")
        await llm.complete("Say OK.", model=model)
        BRAIN.understand("what time is it")

        out = Path(args.out) if args.out else REPO / "benchmarks" / f"run_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"meta": {}, "runs": []}
        if args.resume and out.exists():
            data = json.loads(out.read_text(encoding="utf-8"))
        done = {(r["task"], r["run"]) for r in data["runs"]}
        data["meta"] = {
            **data.get("meta", {}), "name": "FRIDAY agent benchmark (Phase 29.0)", "model": model, "reps": args.reps,
            "commit": _git_commit(), "host": platform.platform(), "tasks": [t.id for t in tasks],
            "started": data.get("meta", {}).get("started") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "isolation": iso.facts, "planner": {"max_steps": CFG.planner.max_steps, "total_timeout_s": CFG.planner.total_timeout_s,
                                                  "max_replans": CFG.planner.max_replans, "num_ctx": CFG.llm.num_ctx},
        }
        t_suite = time.perf_counter() + 0.0
        prior = data["meta"].get("duration_s", 0.0)
        runtime = {"browser": need_browser}
        total = len(tasks) * args.reps
        n = 0
        for rep in range(1, args.reps + 1):
            for task in tasks:
                n += 1
                if (task.id, rep) in done:
                    continue
                print(f"\n[{n}/{total}] {task.id} (run {rep}) ...", flush=True)
                rec = await run_one(task, rep, iso, bench_tasks.Env, runtime)
                data["runs"].append(rec)
                tag = rec["status"]
                extra = f"{rec.get('elapsed_s', 0)}s, {rec.get('steps', 0)} steps" if tag != "SKIP" else rec.get("reason", "")
                detail = "" if tag == "PASS" else f" -- [{rec.get('failure_class', '')}] {rec.get('reason', '')[:140]}"
                print(f"    {tag} ({extra}){detail}", flush=True)
                data["meta"]["duration_s"] = prior + (time.perf_counter() - t_suite)
                out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        try:
            data["meta"]["final_isolation_checks"] = iso.final_checks()
            print(f"\nfinal isolation checks: {data['meta']['final_isolation_checks']}")
        except IsolationError as exc:
            data["meta"]["final_isolation_checks"] = {"FAILED": str(exc)}
            print(f"\nISOLATION BREACH DETECTED AFTER THE RUN: {exc}")
            code = 2
        out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        print_report(data)
        print(f"\nresults written to {out}")
    finally:
        try:
            from friday import browser

            await browser.close()
        except Exception:  # noqa: BLE001
            pass
        iso.stop()
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prove-only", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="validate the checks with known-correct solutions + untouched environments")
    ap.add_argument("--report", default="")
    ap.add_argument("--review", default="")
    ap.add_argument("--failures", action="store_true", help="with --report: print every failing run's trace")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--compare", default="")
    ap.add_argument("--gate", action="store_true", help="exit 1 if the overall pass rate regressed vs --compare")
    args = ap.parse_args()

    if args.list:
        import bench_tasks

        for t in bench_tasks.TASKS:
            print(f"{t.id:<24} {t.category:<8} confirm={t.confirm:<8} {t.goal}")
        print(f"\n{len(bench_tasks.TASKS)} tasks")
        return 0

    if args.report:
        data = json.loads(Path(args.report).read_text(encoding="utf-8"))
        if args.review:
            apply_review(data["runs"], json.loads(Path(args.review).read_text(encoding="utf-8")))
        print_report(data, markdown=args.markdown)
        if args.failures:
            print_failures(data)
        if args.compare:
            base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
            if args.review and Path(args.review).exists():
                pass
            regressed = compare(data, base)
            return 1 if (regressed and args.gate) else 0
        return 0

    code = asyncio.run(amain(args))
    sys.stdout.flush()
    os._exit(code)  # the Playwright / COM / daemon threads must not keep the process alive


if __name__ == "__main__":
    raise SystemExit(main())
