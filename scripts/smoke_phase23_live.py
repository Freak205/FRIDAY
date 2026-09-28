"""Phase 23.0 — live validation on the REAL qwen2.5:3b (evidence-grounded goal completion).

Same spirit as scripts/smoke_phase22_live.py: everything real (Session, BRAIN, plan.run,
Orchestrator, EXECUTOR, permission gate, skills, Ollama) EXCEPT that nothing with a side
effect can happen. Three independent nets, unchanged from Phase 20-22:

  1. every skill whose tier is not L0 (except `plan.run`) has a per-tool `deny` policy
     override; the harness refuses to start unless `permissions.evaluate` says deny for
     every one of them;
  2. `EXECUTOR.run` is wrapped: a non-L0 skill never reaches its body; every ATTEMPT is
     recorded (so a mis-route is observable even though it never runs);
  3. a throwaway SQLite DB, confirmations always declined, and any non-L0 `skill.start`
     on the bus ABORTS the whole run (unsafe execution).

Unlike Phase 20-22 (which diffed a real pre-phase tree copy via `--root`), this repo has
no git history to snapshot, so BEFORE/AFTER here is the SAME tree, SAME process, SAME
Ollama, with the two Phase 23 switches (`CFG.planner.subgoal_evidence_advance`,
`CFG.planner.answer_from_evidence`) toggled off for BEFORE — verified structurally
(scripts/smoke_evidence_grounded_goals.py section G4) and by direct code reading to be a
complete kill switch for every new code path this phase adds; every OTHER line of code is
identical between arms. BEFORE is run in its own subprocess-free pass, then AFTER, on the
same machine in the same sitting.

Measures, per PLAN.md Phase 23 §7:
  - compound-goal completion (answered correctly, stop reason)
  - unnecessary tool calls (real tool-call count per run)
  - repeated subgoal selection (orchestrator.subgoal advance events; a "stuck" run is one
    whose last two tool calls are identical/re-reads of the same acquisition target)
  - answer-from-existing-evidence rate (orchestrator.evidence_stop events / completed runs)
  - unsafe executions (must be 0 in both arms)

Usage:
    python -X utf8 scripts/smoke_phase23_live.py --reps 4
Skips (exit 0) if Ollama / the model is unavailable. Exit 1 on any unsafe execution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

README_CONTENT = (
    "FRIDAY - a local-first Windows desktop assistant.\n\n"
    "Built with Python, FastAPI and SQLite. Speech uses Whisper for recognition and Piper "
    "for text-to-speech. The local reasoning model is served through Ollama.\n"
    "Launch code for the demo build: ZEBRA-7731.\n"
)
NOTES_A_CONTENT = "Meeting notes.\nEverything is on track. No blockers reported this week.\n"
NOTES_B_CONTENT = "Incident log.\nURGENT: the payment webhook has been failing since 09:14.\n"


class Unsafe(RuntimeError):
    pass


def _ns(x, name, default=None):
    return getattr(x, name, default)


class Live:
    def __init__(self) -> None:
        from friday import llm, permissions, store
        from friday.bus import BUS
        from friday.config import CFG
        from friday.registry import REGISTRY

        self.llm, self.permissions, self.store, self.BUS, self.CFG, self.REGISTRY = llm, permissions, store, BUS, CFG, REGISTRY
        self.calls: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self.unsafe: list[dict] = []
        self.blocked: list[dict] = []
        self.attempts: list[str] = []

    def lock_down(self) -> dict:
        REG, CFG, perm = self.REGISTRY, self.CFG, self.permissions
        REG.discover()
        non_l0 = [s for s in REG.all() if s.tier != "L0" and s.name != "plan.run"]
        CFG.permissions.overrides = {**CFG.permissions.overrides, **{s.name: "deny" for s in non_l0}}
        holes = [s.name for s in non_l0 if perm.evaluate(s, "text", {}).policy != "deny"]
        if holes:
            raise Unsafe(f"deny override not effective for {holes}")
        ex, orig = perm.EXECUTOR, perm.EXECUTOR.run

        async def guarded_run(skill_name, args=None, *, actor="text"):
            self.attempts.append(skill_name)
            if skill_name != "plan.run":
                sk = REG.get(skill_name)
                tier = sk.tier if sk is not None else "?"
                if tier != "L0":
                    policy = "unknown"
                    if sk is not None:
                        try:
                            policy = perm.evaluate(sk, actor, args or {}).policy
                        except Exception:
                            policy = "error"
                    self.blocked.append({"skill": skill_name, "args": args or {}, "tier": tier, "policy": policy})
                    if policy == "deny":
                        await orig(skill_name, args, actor=actor)  # the real gate raises before any skill code
                    raise perm.PermissionError_(f"harness: {skill_name} (tier {tier}) not allowed in this live test")
            return await orig(skill_name, args, actor=actor)

        ex.run = guarded_run  # type: ignore[method-assign]

        async def decline(skill_, args, preview) -> bool:
            return False

        ex.set_confirm_handler(decline)

        async def on_event(ev) -> None:
            if ev.topic == "skill.start":
                sk = REG.get(str(ev.data.get("skill")))
                if sk is None or (sk.tier != "L0" and sk.name != "plan.run"):
                    self.unsafe.append(dict(ev.data))
            if ev.topic.startswith("orchestrator."):
                self.events.append((ev.topic, dict(ev.data)))

        self.BUS.subscribe("*", on_event)
        return {"non_l0_denied": len(non_l0), "l0_allowed": sum(1 for s in REG.all() if s.tier == "L0")}

    def instrument_llm(self) -> None:
        prov_cls = self.llm.OllamaProvider
        orig = prov_cls.complete

        async def logged(inst, request):
            t0 = time.perf_counter()
            rec = {"chars": sum(len(m.content) for m in request.messages)}
            try:
                r = await orig(inst, request)
            except Exception as exc:
                rec.update(err=repr(exc)[:120], ms=round((time.perf_counter() - t0) * 1000))
                self.calls.append(rec)
                raise
            raw = r.raw or {}
            rec.update(ms=round((time.perf_counter() - t0) * 1000), prompt_tokens=raw.get("prompt_eval_count"),
                       gen_tokens=raw.get("eval_count"), text=r.text[:140])
            self.calls.append(rec)
            return r

        prov_cls.complete = logged  # type: ignore[method-assign]

    def begin(self) -> None:
        self.calls, self.events = [], []
        self.blocked.clear()
        self.attempts.clear()


async def run_plan(L: Live, goal: str, *, timeout: float = 240.0):
    from friday.permissions import EXECUTOR

    L.begin()
    t0 = time.perf_counter()
    try:
        r = await asyncio.wait_for(EXECUTOR.run("plan.run", {"goal": goal}, actor="text"), timeout=timeout)
        err = ""
    except Exception as exc:
        r, err = None, repr(exc)[:160]
    return r, err, round((time.perf_counter() - t0) * 1000)


def _evcount(L: Live, topic: str) -> int:
    return sum(1 for t, _ in L.events if t == topic)


def _stuck(tools: list[str]) -> bool:
    """A crude, honest signal of Phase 22's reported failure mode: the SAME acquisition
    tool called 2+ times in a row (re-reading instead of answering from what it has)."""
    return any(tools[i] == tools[i + 1] for i in range(len(tools) - 1))


def _run_row(L: Live, sid: str, goal: str, r, err: str, ms: int, needle: str) -> dict:
    steps = (r.data or {}).get("steps", []) if r is not None else []
    tools = [s["tool"] for s in steps]
    pt = [c["prompt_tokens"] for c in L.calls if c.get("prompt_tokens")]
    return {
        "sid": sid, "goal": goal[:140], "ms": ms, "err": err, "ok": bool(r and r.ok),
        "stopped": (r.data or {}).get("stopped") if r else None,
        "tools": tools, "real_tool_calls": len(tools),
        "distinct_tools": len(set(tools)),
        "repeat_blocked": sum(1 for s in steps if s.get("error") == "repeated_call"),
        "model_calls": len(L.calls), "prompt_tokens": pt,
        "subgoal_advances": _evcount(L, "orchestrator.subgoal"),
        "evidence_stop": _evcount(L, "orchestrator.evidence_stop") > 0,
        "stuck_reread": _stuck(tools),
        "answered": bool(r and needle.lower() in (r.speech or "").lower()),
        "speech": (r.speech if r else "")[:220],
    }


# ================================================================================
# groups
# ================================================================================


async def group_a(L: Live, reps: int) -> list[dict]:
    """A: the exact Phase-22-reported failure shape — 'Read X and tell me Y'."""
    rows = []
    with tempfile.TemporaryDirectory() as td:
        readme = Path(td) / "README.md"
        readme.write_text(README_CONTENT, encoding="utf-8")
        cases = [
            ("A1_launch_code", f"Read the file {readme} and tell me the launch code.", "ZEBRA-7731"),
            ("A2_technologies", f"Read the file {readme} and tell me what technologies this project uses.", "python"),
            ("A3_language", f"Read {readme} and tell me what speech recognition library it uses.", "whisper"),
        ]
        for sid, goal, needle in cases:
            for rep in range(reps):
                r, err, ms = await run_plan(L, goal)
                row = _run_row(L, sid, goal, r, err, ms, needle)
                row["rep"] = rep
                rows.append(row)
                print(f"   {sid}#{rep}: answered={row['answered']} tools={row['tools']} stopped={row['stopped']} "
                      f"calls={row['model_calls']} evidence_stop={row['evidence_stop']} stuck={row['stuck_reread']} "
                      f"{ms/1000:.1f}s | {row['speech'][:70]!r}")
    return rows


async def group_b(L: Live, reps: int) -> list[dict]:
    """B: a genuinely multi-part compound goal — two acquisitions, one answer."""
    rows = []
    with tempfile.TemporaryDirectory() as td:
        notes_a = Path(td) / "notes_a.txt"
        notes_b = Path(td) / "notes_b.txt"
        notes_a.write_text(NOTES_A_CONTENT, encoding="utf-8")
        notes_b.write_text(NOTES_B_CONTENT, encoding="utf-8")
        goal = (
            f"Read {notes_a} and {notes_b} and tell me which one is urgent."
        )
        for rep in range(reps):
            r, err, ms = await run_plan(L, goal)
            row = _run_row(L, "B1_two_acquisitions", goal, r, err, ms, "notes_b")
            row["rep"] = rep
            rows.append(row)
            print(f"   B1#{rep}: answered={row['answered']} tools={row['tools']} stopped={row['stopped']} "
                  f"calls={row['model_calls']} evidence_stop={row['evidence_stop']} {ms/1000:.1f}s | {row['speech'][:70]!r}")
    return rows


async def group_c(L: Live, reps: int) -> list[dict]:
    """C: control — a single-clause read (no 'and tell me'), must be unaffected."""
    rows = []
    with tempfile.TemporaryDirectory() as td:
        readme = Path(td) / "README.md"
        readme.write_text(README_CONTENT, encoding="utf-8")
        goal = f"What does the file {readme} say the launch code is?"
        for rep in range(reps):
            r, err, ms = await run_plan(L, goal)
            row = _run_row(L, "C1_single_clause", goal, r, err, ms, "ZEBRA-7731")
            row["rep"] = rep
            rows.append(row)
            print(f"   C1#{rep}: answered={row['answered']} tools={row['tools']} stopped={row['stopped']} "
                  f"calls={row['model_calls']} {ms/1000:.1f}s | {row['speech'][:70]!r}")
    return rows


# ================================================================================
# report
# ================================================================================


def _stats(xs: list[float]) -> dict:
    return {"n": len(xs), "median": round(statistics.median(xs), 1), "mean": round(statistics.mean(xs), 1), "max": max(xs)} if xs else {"n": 0}


def summarize(rows: list[dict]) -> dict:
    s: dict = {}
    if not rows:
        return s
    by_sid: dict[str, list[dict]] = {}
    for r in rows:
        by_sid.setdefault(r["sid"], []).append(r)
    for sid, xs in by_sid.items():
        s[f"{sid}_answered"] = f"{sum(1 for r in xs if r['answered'])}/{len(xs)}"
        s[f"{sid}_completed"] = f"{sum(1 for r in xs if r['stopped'] == 'completed')}/{len(xs)}"
        s[f"{sid}_mean_tool_calls"] = round(statistics.mean(r["real_tool_calls"] for r in xs), 2)
        s[f"{sid}_mean_model_calls"] = round(statistics.mean(r["model_calls"] for r in xs), 2)
        s[f"{sid}_stuck_rereads"] = sum(1 for r in xs if r["stuck_reread"])
        s[f"{sid}_evidence_stops"] = sum(1 for r in xs if r["evidence_stop"])
    s["ALL_answered"] = f"{sum(1 for r in rows if r['answered'])}/{len(rows)}"
    s["ALL_mean_tool_calls"] = round(statistics.mean(r["real_tool_calls"] for r in rows), 2)
    s["ALL_mean_model_calls"] = round(statistics.mean(r["model_calls"] for r in rows), 2)
    s["ALL_stuck_rereads"] = sum(1 for r in rows if r["stuck_reread"])
    s["ALL_evidence_stops"] = sum(1 for r in rows if r["evidence_stop"])
    s["ALL_prompt_tokens"] = _stats([t for r in rows for t in r["prompt_tokens"]])
    return s


async def run_arm(L: Live, reps: int, groups: str) -> list[dict]:
    rows: list[dict] = []
    for name, fn in (("A", group_a), ("B", group_b), ("C", group_c)):
        if name in groups:
            print(f"\n-- group {name} --")
            rows += await fn(L, reps)
            if L.unsafe:
                raise Unsafe(f"non-L0 skill.start observed: {L.unsafe}")
    return rows


async def main_async(args) -> int:
    L = Live()
    CFG = L.CFG
    CFG.planner.structured_output = True
    CFG.desktop_observer.enabled = False
    if hasattr(CFG.intelligence, "experience_enabled"):
        CFG.intelligence.experience_enabled = False
        CFG.intelligence.context_memory_enabled = False
    net = L.lock_down()
    L.instrument_llm()
    try:
        await L.llm.complete("ping", model=CFG.llm.model or "qwen2.5:3b", system="Reply with the single word ok.")
    except L.llm.LlmError as exc:
        print(f"SKIP: Ollama/model unavailable ({exc})")
        return 0

    report: dict = {
        "model": CFG.llm.model or "qwen2.5:3b", "reps": args.reps, "nets": net,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"\n=== Phase 23 live: model={report['model']} reps={args.reps} groups={args.groups} ===\n    nets: {net}")
    t0 = time.perf_counter()
    out = Path(args.out) if args.out else REPO / "data" / "phase23_live.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    with L.store.use_temp_db():
        try:
            print("\n########## ARM: BEFORE (Phase 23 switches OFF) ##########")
            CFG.planner.subgoal_evidence_advance = False
            CFG.planner.answer_from_evidence = False
            report["before"] = await run_arm(L, args.reps, args.groups)
            report["summary_before"] = summarize(report["before"])
            out.write_text(json.dumps({**report, "unsafe": L.unsafe}, indent=2, default=str), encoding="utf-8")

            print("\n########## ARM: AFTER (Phase 23 switches ON, the shipped defaults) ##########")
            CFG.planner.subgoal_evidence_advance = True
            CFG.planner.answer_from_evidence = True
            report["after"] = await run_arm(L, args.reps, args.groups)
            report["summary_after"] = summarize(report["after"])
        except Unsafe as exc:
            print(f"!!! SAFETY INCIDENT: {exc}")
            report["unsafe"] = L.unsafe or [{"error": str(exc)}]

    report.setdefault("unsafe", L.unsafe)
    report["wall_s"] = round(time.perf_counter() - t0, 1)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n=== BEFORE (switches off) ===")
    for k, v in report.get("summary_before", {}).items():
        print(f"  {k:36} {v}")
    print("\n=== AFTER (shipped defaults) ===")
    for k, v in report.get("summary_after", {}).items():
        print(f"  {k:36} {v}")
    print(f"\nreport: {out}   wall {report['wall_s']}s   UNSAFE EXECUTIONS: {len(report['unsafe'])}")
    return 1 if report["unsafe"] else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=4)
    ap.add_argument("--groups", default="ABC")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
