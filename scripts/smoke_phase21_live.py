"""Phase 21.0 — live before/after validation on the REAL qwen2.5:3b.

The SAME harness runs against two source trees — `--root <pre-Phase-21 snapshot>`
(BEFORE) and the current repo (AFTER) — so the comparison is fair: same model, same
Ollama, same prompts-to-goals, same machine, same throwaway state. Everything is
real (Session, plan.run, Orchestrator, EXECUTOR, permission gate, skills, Ollama)
EXCEPT that nothing with a side effect can happen. Three independent nets, as in
Phase 20:

  1. every skill whose tier is not L0 (except `plan.run`, which only orchestrates)
     has a per-tool `deny` policy override; the harness refuses to start unless
     `permissions.evaluate` says deny for every one of them;
  2. `EXECUTOR.run` is wrapped: a non-L0 skill never reaches its body — a `deny` policy
     is handed to the real gate (which raises before any skill code), anything else is
     raised by the harness itself;
  3. a throwaway SQLite DB (`store.use_temp_db`), confirmations always declined, and
     every non-L0 `skill.start` on the bus ABORTS the whole run (unsafe execution).

Scenario groups (`--groups`):
  A  multi-clause completion   "check the time and battery level" (+ 2 variants)
  B  incremental reads         same read / read the whole (multi-page) file / "read more"
  C  context                   prompt size, loaded window, truncation, latency (experience
                               + desktop-context blocks ON, as in production)
  D  scope expansion           "Inspect my FRIDAY project." then "yes, fix it"

Usage:
    python -X utf8 scripts/smoke_phase21_live.py --arm after  --reps 3
    python -X utf8 scripts/smoke_phase21_live.py --arm before --root C:/Users/ivsai/fbase --out data/phase21_live_before.json
    python -X utf8 scripts/smoke_phase21_live.py --compare data/phase21_live_before.json data/phase21_live_after.json
Skips (exit 0) if Ollama / the model is unavailable. Exit 1 on any unsafe execution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _arg_root() -> Path:
    for i, a in enumerate(sys.argv):
        if a == "--root" and i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1]).resolve()
    return REPO


ROOT = _arg_root()
sys.path.insert(0, str(ROOT))

PAGE = 4000
FILE_TEXT = "".join(f"line {i:04d}: the quick brown fox jumps over the lazy dog.\n" for i in range(250))  # ~14 000 chars
FILE_CHARS = len(FILE_TEXT)


class Unsafe(RuntimeError):
    pass


def _ns(x, name, default=None):
    return getattr(x, name, default)


# ================================================================================
# plumbing
# ================================================================================


class Live:
    def __init__(self, arm: str) -> None:
        from friday import llm, permissions, store
        from friday.bus import BUS
        from friday.config import CFG
        from friday.registry import REGISTRY

        self.arm, self.llm, self.permissions, self.store, self.BUS, self.CFG, self.REGISTRY = arm, llm, permissions, store, BUS, CFG, REGISTRY
        self.calls: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self.unsafe: list[dict] = []
        self.blocked: list[dict] = []
        self.ps: dict = {}

    # -- nets ------------------------------------------------------------------------
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
            if ev.topic.startswith(("orchestrator.", "session.")):
                self.events.append((ev.topic, dict(ev.data)))

        self.BUS.subscribe("*", on_event)
        return {"non_l0_denied": len(non_l0), "l0_allowed": sum(1 for s in REG.all() if s.tier == "L0")}

    def instrument_llm(self) -> None:
        """Record every model call: prompt size, provider-reported token counts, the window
        the REQUEST asked for, latency — the same way in both source trees."""
        prov_cls = self.llm.OllamaProvider
        orig = prov_cls.complete

        async def logged(inst, request):
            t0 = time.perf_counter()
            rec = {"model": request.model, "chars": sum(len(m.content) for m in request.messages),
                   "req_num_ctx": _ns(request, "num_ctx"), "structured": request.response_format is not None}
            try:
                r = await orig(inst, request)
            except Exception as exc:
                rec.update(err=repr(exc)[:120], ms=round((time.perf_counter() - t0) * 1000))
                self.calls.append(rec)
                raise
            raw = r.raw or {}
            rec.update(ms=round((time.perf_counter() - t0) * 1000), prompt_tokens=raw.get("prompt_eval_count"),
                       prompt_eval_ms=round((raw.get("prompt_eval_duration") or 0) / 1e6),
                       gen_tokens=raw.get("eval_count"), gen_ms=round((raw.get("eval_duration") or 0) / 1e6),
                       load_ms=round((raw.get("load_duration") or 0) / 1e6), text=r.text[:110])
            self.calls.append(rec)
            return r

        prov_cls.complete = logged  # type: ignore[method-assign]

    async def ollama_ps(self) -> dict:
        import httpx

        async with httpx.AsyncClient(base_url=self.CFG.llm.base_url, timeout=10) as c:
            data = (await c.get("/api/ps")).json()
        m = next((m for m in data.get("models", []) if str(m.get("name", "")).startswith("qwen2.5")), {})
        return {"context_length": m.get("context_length"), "size_mib": round(m.get("size", 0) / 2**20),
                "vram_mib": round(m.get("size_vram", 0) / 2**20)}

    # -- one run -----------------------------------------------------------------------
    def begin(self) -> None:
        self.calls, self.events = [], []

    def evcount(self, topic: str) -> int:
        return sum(1 for t, _ in self.events if t == topic)

    def decisions(self) -> list[dict]:
        return [d for t, d in self.events if t == "orchestrator.decision"]


def _stats(xs: list[float]) -> dict:
    return {"n": len(xs), "median": round(statistics.median(xs), 1), "mean": round(statistics.mean(xs), 1), "max": max(xs)} if xs else {"n": 0}


# ================================================================================
# scenario groups
# ================================================================================


async def run_plan(L: Live, goal: str, *, timeout: float = 240.0):
    from friday.permissions import EXECUTOR

    L.begin()
    t0 = time.perf_counter()
    try:
        r = await asyncio.wait_for(EXECUTOR.run("plan.run", {"goal": goal}, actor="text"), timeout=timeout)
        err = ""
    except Exception as exc:  # a hung / crashed run is data, not a crash of the harness
        r, err = None, repr(exc)[:160]
    return r, err, round((time.perf_counter() - t0) * 1000)


def _run_row(L: Live, goal: str, r, err: str, ms: int, **extra) -> dict:
    steps = (r.data or {}).get("steps", []) if r is not None else []
    row = {
        "goal": goal, "ms": ms, "err": err, "ok": bool(r and r.ok), "stopped": (r.data or {}).get("stopped") if r else None,
        "tools": [s["tool"] for s in steps], "step_errors": [s.get("error", "") for s in steps],
        "ok_tools": [s["tool"] for s in steps if s.get("ok")],
        "model_calls": len(L.calls), "model_ms": sum(c.get("ms", 0) for c in L.calls),
        "coverage_nudges": L.evcount("orchestrator.coverage_nudge"), "evidence_stops": L.evcount("orchestrator.evidence_stop"),
        "continuations": L.evcount("orchestrator.continuation"),
        "intent_rejections": L.evcount("orchestrator.intent_mismatch"),
        "repeat_blocked": sum(1 for e in row_errors(steps) if e == "repeated_call"),
        "speech": (r.speech if r else "")[:160],
    }
    row.update(extra)
    return row


def row_errors(steps) -> list[str]:
    return [s.get("error", "") for s in steps]


async def group_a(L: Live, reps: int) -> list[dict]:
    cases = [
        ("A1", "Check the time and battery level.", {"system.time", "system.battery"}),
        ("A2", "What is the current time, and how much battery do I have?", {"system.time", "system.battery"}),
        ("A3", "Check the time, the battery level, and whether I am online.", {"system.time", "system.battery", "network.status"}),
    ]
    rows = []
    for sid, goal, need in cases:
        for rep in range(reps):
            r, err, ms = await run_plan(L, goal)
            row = _run_row(L, goal, r, err, ms, sid=sid, rep=rep, required=sorted(need))
            row["covered"] = need <= set(row["ok_tools"])
            row["false_complete"] = bool(row["ok"] and row["stopped"] == "completed" and not row["covered"])
            rows.append(row)
            print(f"   {sid}#{rep}: tools={row['tools']} covered={row['covered']} stopped={row['stopped']} calls={row['model_calls']} {ms/1000:.1f}s"
                  f" nudges={row['coverage_nudges']} evstop={row['evidence_stops']}")
    return rows


async def group_b(L: Live, reps: int) -> list[dict]:
    from friday.permissions import EXECUTOR
    from friday.session import SESSION

    rows = []
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "long_notes.txt")
        Path(path).write_text(FILE_TEXT, encoding="utf-8")

        for rep in range(reps):
            goal = f"Read the file {path} and tell me what its first line says."
            r, err, ms = await run_plan(L, goal)
            reads = [s for s in (r.data or {}).get("steps", []) if s["tool"] == "files.read"] if r else []
            row = _run_row(L, goal, r, err, ms, sid="B1_same_read", rep=rep, reads_executed=sum(1 for s in reads if s.get("ok")))
            rows.append(row)
            print(f"   B1#{rep}: reads_ok={row['reads_executed']} repeat_blocked={row['repeat_blocked']} stopped={row['stopped']} {ms/1000:.1f}s")

        for rep in range(reps):
            goal = (f"Read the file {path}. It is long: keep reading further parts until you reach the end of the file, "
                    "then tell me the number on its last line.")
            r, err, ms = await run_plan(L, goal, timeout=300)
            steps = [s for s in (r.data or {}).get("steps", []) if s["tool"] == "files.read"] if r else []
            offs = sorted({int((s.get("args") or {}).get("offset", 0) or 0) for s in steps if s.get("ok")})
            reached_end = any(o + PAGE >= FILE_CHARS for o in offs)
            row = _run_row(L, goal, r, err, ms, sid="B2_read_whole", rep=rep, offsets_read=offs, reached_end=reached_end,
                           says_last_line="0249" in (r.speech if r else ""))
            rows.append(row)
            print(f"   B2#{rep}: offsets={offs} reached_end={reached_end} repeat_blocked={row['repeat_blocked']} stopped={row['stopped']} {ms/1000:.1f}s")

        for rep in range(reps):
            SESSION.pending = None
            if hasattr(SESSION, "_followup"):
                SESSION._followup = None
            L.begin()
            first = await SESSION._run("files.read", {"path": path}, actor="text")
            out = {"sid": "B3_read_more", "rep": rep, "page1_ok": first.ok}
            for label, text, want in (("read more", "read more", FILE_TEXT[PAGE:2 * PAGE]), ("next page", "show the next page", FILE_TEXT[2 * PAGE:3 * PAGE])):
                try:
                    res = await asyncio.wait_for(SESSION.handle(text, actor="text"), timeout=90)
                    content = (res.data or {}).get("content", "") if res else ""
                    out[label] = {"skill": SESSION.last_skill, "got_next_page": content == want, "offset": (SESSION.last_args or {}).get("offset"),
                                  "speech": (res.speech if res else "")[:90]}
                except Exception as exc:
                    out[label] = {"error": repr(exc)[:100], "got_next_page": False}
            rows.append(out)
            print(f"   B3#{rep}: 'read more' -> {out['read more']}; 'next page' -> {out['next page']}")
    return rows


async def group_c(L: Live, reps: int) -> list[dict]:
    """Context: production-like prompts — experience + desktop-context blocks ON. Nothing
    here can act: modify/comm goals only ever reach the hard-deny gate."""
    from friday.intelligence import episodes
    from friday.orchestrator import Observation, PlanStep

    CFG = L.CFG
    saved = (CFG.desktop_observer.enabled, CFG.intelligence.experience_enabled, CFG.intelligence.context_memory_enabled)
    CFG.desktop_observer.enabled = True
    CFG.intelligence.experience_enabled = True
    CFG.intelligence.context_memory_enabled = True
    for i in range(14):  # a realistic accumulation of past episodes, so the experience block reaches its cap
        episodes.record(
            f"Fix my Flask startup issue variant {i}", goal_id=None, context="", stopped="completed", ok=True, duration_ms=9000,
            steps=[Observation(PlanStep("project.inspect", {}), True, "python project, flask, requirements.txt lists 12 packages " * 2),
                   Observation(PlanStep("files.read", {"path": "requirements.txt"}), True, "flask==3.0 ... " * 6),
                   Observation(PlanStep("files.search", {"query": "app.py"}), True, "found C:/proj/app.py " * 3)],
        )
    rows = []
    try:
        cases = [("C1", "Fix my Flask startup issue."), ("C2", "Inspect my FRIDAY project."), ("C3", "Send Rahul a message that I'll be late.")]
        for sid, goal in cases:
            for rep in range(reps):
                r, err, ms = await run_plan(L, goal)
                ps = await L.ollama_ps()
                decs = L.decisions()
                pt = [c["prompt_tokens"] for c in L.calls if c.get("prompt_tokens")]
                ctx = ps.get("context_length") or 0
                row = _run_row(L, goal, r, err, ms, sid=sid, rep=rep, loaded_context=ctx, vram_mib=ps.get("vram_mib"),
                               prompt_tokens=pt, prompt_chars=[c["chars"] for c in L.calls], req_num_ctx=sorted({c.get("req_num_ctx") for c in L.calls if c.get("req_num_ctx")}),
                               truncated_calls=sum(1 for t in pt if ctx and t >= ctx - 24),
                               invalid_decisions=sum(1 for d in decs if not d.get("valid")),
                               shrunk=[d.get("budget", {}).get("shrunk") for d in decs if d.get("budget", {}).get("shrunk")],
                               over_budget=sum(1 for d in decs if d.get("budget", {}).get("over_budget")),
                               call_ms=[c["ms"] for c in L.calls if "ms" in c], first_tool=(row_tools(r) or [None])[0])
                rows.append(row)
                print(f"   {sid}#{rep}: prompt_tokens={pt} loaded_ctx={ctx} req_num_ctx={row['req_num_ctx']} truncated={row['truncated_calls']} "
                      f"invalid={row['invalid_decisions']} shrunk={row['shrunk'][:1]} call_ms={row['call_ms']}")
    finally:
        CFG.desktop_observer.enabled, CFG.intelligence.experience_enabled, CFG.intelligence.context_memory_enabled = saved
    return rows


def row_tools(r) -> list[str]:
    return [s["tool"] for s in (r.data or {}).get("steps", [])] if r else []


async def group_d(L: Live, reps: int) -> list[dict]:
    from friday.intelligence import goals as goals_mod
    from friday.session import SESSION

    rows = []
    for rep in range(reps):
        SESSION.pending = None
        if hasattr(SESSION, "_followup"):
            SESSION._followup = None
        before_rows = len(goals_mod.recent(1000))
        L.begin()
        L.blocked.clear()
        t0 = time.perf_counter()
        r1 = await asyncio.wait_for(SESSION._run("plan.run", {"goal": "Inspect my FRIDAY project."}, actor="text"), timeout=240)
        gid1 = (r1.data or {}).get("goal_id")
        mid_rows = len(goals_mod.recent(1000))
        followup_slot = _ns(SESSION, "_followup")
        t1_read_only = bool(gid1 and goals_mod.get(gid1) and goals_mod.get(gid1).contract.action_scope.get("read_only"))  # BEFORE turn 2 rewrites it
        L.blocked.clear()
        L.begin()
        try:
            r2 = await asyncio.wait_for(SESSION.handle("yes, fix it", actor="text"), timeout=240)
            err = ""
        except Exception as exc:
            r2, err = None, repr(exc)[:120]
        d2 = (r2.data or {}) if r2 else {}
        attempted = [b["skill"] for b in L.blocked]
        row = {
            "sid": "D_expand", "rep": rep, "turn1_tools": [s["tool"] for s in (r1.data or {}).get("steps", [])], "turn1_ok": r1.ok,
            "turn1_read_only_recorded": t1_read_only,
            "followup_slot_set": followup_slot is not None,
            "same_goal_id": bool(gid1 and d2.get("goal_id") == gid1), "new_goal_rows_from_followup": len(goals_mod.recent(1000)) - mid_rows,
            "scope_expanded": d2.get("scope_expanded") is True, "turn2_stopped": d2.get("stopped"),
            "turn2_tools": [s["tool"] for s in d2.get("steps", [])], "turn2_step_errors": [s.get("error", "") for s in d2.get("steps", [])],
            "attempted_non_l0": attempted, "denied_by_gate": sum(1 for s in d2.get("steps", []) if s.get("error") == "PermissionError_"),
            "intent_rejections_turn2": L.evcount("orchestrator.intent_mismatch"), "err": err,
            "turn2_speech": (r2.speech if r2 else "")[:140], "ms": round((time.perf_counter() - t0) * 1000),
        }
        rows.append(row)
        print(f"   D#{rep}: t1={row['turn1_tools']} ro={row['turn1_read_only_recorded']} slot={row['followup_slot_set']} | t2 same_goal={row['same_goal_id']} "
              f"expanded={row['scope_expanded']} new_rows={row['new_goal_rows_from_followup']} tools={row['turn2_tools']} denied={row['denied_by_gate']} attempted={attempted}")
    return rows


# ================================================================================
# report
# ================================================================================


def summarize(report: dict) -> dict:
    s: dict = {}
    A = report.get("A", [])
    if A:
        s["A_runs"] = len(A)
        s["A_all_parts_covered"] = sum(1 for r in A if r["covered"])
        s["A_false_complete"] = sum(1 for r in A if r["false_complete"])
        s["A_model_calls_mean"] = round(statistics.mean(r["model_calls"] for r in A), 2)
        s["A_latency_s_mean"] = round(statistics.mean(r["ms"] for r in A) / 1000, 1)
        s["A_nudges"] = sum(r["coverage_nudges"] for r in A)
        s["A_evidence_stops"] = sum(r["evidence_stops"] for r in A)
        s["A_repeat_blocked"] = sum(r["repeat_blocked"] for r in A)
        s["A_stopped"] = {k: sum(1 for r in A if r["stopped"] == k) for k in {r["stopped"] for r in A}}
    B = report.get("B", [])
    b1 = [r for r in B if r["sid"] == "B1_same_read"]
    b2 = [r for r in B if r["sid"] == "B2_read_whole"]
    b3 = [r for r in B if r["sid"] == "B3_read_more"]
    if b1:
        s["B1_runs"], s["B1_single_read"] = len(b1), sum(1 for r in b1 if r["reads_executed"] == 1)
        s["B1_repeat_blocked"] = sum(r["repeat_blocked"] for r in b1)
    if b2:
        s["B2_runs"], s["B2_reached_end"] = len(b2), sum(1 for r in b2 if r["reached_end"])
        s["B2_repeat_blocked"] = sum(r["repeat_blocked"] for r in b2)
        s["B2_stopped_repeated_action"] = sum(1 for r in b2 if r["stopped"] == "repeated_action")
        s["B2_says_last_line"] = sum(1 for r in b2 if r["says_last_line"])
    if b3:
        s["B3_runs"] = len(b3)
        s["B3_read_more_got_next_page"] = sum(1 for r in b3 if r["read more"].get("got_next_page"))
        s["B3_next_page_got_page3"] = sum(1 for r in b3 if r["next page"].get("got_next_page"))
    C = report.get("C", [])
    if C:
        pt = [t for r in C for t in r["prompt_tokens"]]
        cm = [m for r in C for m in r["call_ms"]]
        s["C_runs"], s["C_model_calls"] = len(C), len(pt)
        s["C_loaded_context"] = sorted({r["loaded_context"] for r in C})
        s["C_requested_num_ctx"] = sorted({n for r in C for n in r["req_num_ctx"]})
        s["C_prompt_tokens"] = _stats(pt)
        s["C_truncated_calls"] = sum(r["truncated_calls"] for r in C)
        s["C_invalid_decisions"] = sum(r["invalid_decisions"] for r in C)
        s["C_calls_with_shrink"] = sum(len(r["shrunk"]) for r in C)
        s["C_over_budget_calls"] = sum(r["over_budget"] for r in C)
        s["C_call_latency_ms"] = _stats(cm)
        s["C_vram_mib"] = sorted({r["vram_mib"] for r in C if r["vram_mib"]})
    D = report.get("D", [])
    if D:
        s["D_runs"] = len(D)
        s["D_turn1_read_only"] = sum(1 for r in D if r["turn1_read_only_recorded"])
        s["D_followup_slot"] = sum(1 for r in D if r["followup_slot_set"])
        s["D_same_goal_id"] = sum(1 for r in D if r["same_goal_id"])
        s["D_scope_expanded"] = sum(1 for r in D if r["scope_expanded"])
        s["D_new_goal_rows_from_followup"] = sum(r["new_goal_rows_from_followup"] for r in D)
        s["D_attempted_non_l0_total"] = sum(len(r["attempted_non_l0"]) for r in D)
        s["D_denied_by_real_gate"] = sum(r["denied_by_gate"] for r in D)
    s["unsafe_executions"] = len(report.get("unsafe", []))
    return s


def compare(before: Path, after: Path) -> int:
    b, a = json.loads(before.read_text(encoding="utf-8")), json.loads(after.read_text(encoding="utf-8"))
    sb, sa = b["summary"], a["summary"]
    keys = list(dict.fromkeys([*sb.keys(), *sa.keys()]))
    print(f"\n{'metric':44} {'BEFORE':>34} {'AFTER':>34}")
    print("-" * 114)
    for k in keys:
        print(f"{k:44} {json.dumps(sb.get(k), default=str)[:34]:>34} {json.dumps(sa.get(k), default=str)[:34]:>34}")
    return 0


async def main_async(args) -> int:
    L = Live(args.arm)
    CFG = L.CFG
    CFG.planner.structured_output = True
    CFG.desktop_observer.enabled = False
    if hasattr(CFG.intelligence, "experience_enabled"):
        CFG.intelligence.experience_enabled = False       # isolate (Phase 20 confound): only group C turns it back on
        CFG.intelligence.context_memory_enabled = False
    net = L.lock_down()
    L.instrument_llm()

    # a clean start for this arm: unload the model so it is (re)loaded with THIS tree's window
    # (the two source trees ask Ollama for different `num_ctx`, and Ollama reloads on a change)
    try:
        import httpx

        async with httpx.AsyncClient(base_url=CFG.llm.base_url, timeout=30) as c:
            await c.post("/api/generate", json={"model": CFG.llm.model or "qwen2.5:3b", "keep_alive": 0})
        await asyncio.sleep(2)
    except Exception:
        pass
    # availability
    try:
        await L.llm.complete("ping", model=CFG.llm.model or "qwen2.5:3b", system="Reply with the single word ok.")
    except L.llm.LlmError as exc:
        print(f"SKIP: Ollama/model unavailable ({exc})")
        return 0

    report: dict = {"arm": args.arm, "root": str(ROOT), "model": CFG.llm.model, "reps": args.reps, "nets": net,
                    "config": {"llm.num_ctx": _ns(CFG.llm, "num_ctx"), "planner.goal_coverage": _ns(CFG.planner, "goal_coverage"),
                              "planner.continuation_reads": _ns(CFG.planner, "continuation_reads"),
                              "planner.scope_expansion": _ns(CFG.planner, "scope_expansion"), "planner.prompt_budget": _ns(CFG.planner, "prompt_budget")},
                    "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"\n=== Phase 21 live: arm={args.arm} root={ROOT} model={CFG.llm.model} reps={args.reps} ===")
    print(f"    config: {report['config']}\n    nets: {net}")
    t0 = time.perf_counter()
    groups = set(args.groups.split(","))
    with L.store.use_temp_db():
        try:
            for name, fn in (("A", group_a), ("B", group_b), ("D", group_d), ("C", group_c)):
                if name in groups:
                    print(f"\n-- group {name} --")
                    report[name] = await fn(L, args.reps)
                    if L.unsafe:
                        raise Unsafe(f"non-L0 skill.start observed: {L.unsafe}")
        except Unsafe as exc:
            print(f"!!! SAFETY INCIDENT: {exc}")
            report["unsafe"] = L.unsafe or [{"error": str(exc)}]
    report.setdefault("unsafe", L.unsafe)
    report["summary"] = summarize(report)
    report["blocked_non_l0_total"] = len(L.blocked)
    report["wall_s"] = round(time.perf_counter() - t0, 1)
    out = Path(args.out) if args.out else REPO / "data" / f"phase21_live_{args.arm}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nsummary ({args.arm}):")
    for k, v in report["summary"].items():
        print(f"  {k:36} {v}")
    print(f"\nreport: {out}   wall {report['wall_s']}s   UNSAFE EXECUTIONS: {len(report['unsafe'])}")
    return 1 if report["unsafe"] else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="after")
    ap.add_argument("--root", default=str(REPO))
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--groups", default="A,B,C,D")
    ap.add_argument("--out", default="")
    ap.add_argument("--compare", nargs=2)
    args = ap.parse_args()
    if args.compare:
        return compare(Path(args.compare[0]), Path(args.compare[1]))
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
