"""Phase 19.0 — live validation of reliable local-LLM decision generation.

REAL everything: a real FRIDAY daemon process (uvicorn + the real FastAPI app,
real Session/Executor/permission gate, real skills), the real qwen2.5:3b via
the real Ollama, the real planner (`plan.run` -> `Orchestrator.run_goal`), and
real tools. Driven over HTTP exactly like any other FRIDAY client.

What is *not* real, on purpose, so nothing with side effects can ever happen:
  * inside the spawned daemon EVERY non-read-only tool (tier L1/L2/L3, except
    `plan.run` itself, which only orchestrates) has a per-tool `deny` policy
    override — the planner may *ask* for `ui.click`, `apps.open`, `shell.run`...
    and the real permission gate refuses, so the real desktop is never touched
    (an earlier draft of this script only declined L2/L3 confirmations and let a
    real L1 `ui.click` through once; that was a harness bug, fixed here);
  * as a second net, any confirmation the daemon parks is DECLINED by the driver
    — nothing is ever approved;
  * the daemon runs against a throwaway SQLite DB (store.use_temp_db), so
    these runs neither read nor pollute the real goal/episode history;
  * one harmless L0 fixture skill (`test.dl_check_failing`) stands in for "a
    safe command that failed", registered only inside the daemon *process*
    this script spawns.

Three arms, same 12 scenarios, same model, same prompts:
  BEFORE  the pre-Phase-19 decision path, emulated verbatim in this file
          (`_legacy_parse_decision` is the old `Orchestrator._parse_decision`
          copied unchanged; on "malformed" it retried the SAME prompt once;
          anything else was passed straight through unvalidated).
  AFTER   the Phase 19.0 parser + validator + one bounded repair, with native
          structured output explicitly OFF.
  AFTER+STRUCTURED  the same with Ollama's native JSON-Schema `format` ON —
          the shipped default (`CFG.planner.structured_output`), which was
          turned on only because this arm measured better end-to-end.
Everything else (planner prompt, tools, evidence rules, discovery gate) is
identical between arms, so differences are attributable to the decision layer.

Then a paired benchmark on the exact production prompts captured above: the
same raw model replies are judged by the legacy rule and by the new
validator, invalid ones get a real repair call, and native structured output
(Ollama `format` = decision JSON Schema) is measured against plain output.

Usage:
    python scripts/smoke_llm_decision_live.py                 # full run
    python scripts/smoke_llm_decision_live.py --reps 1 --k 2  # quicker
Skips (exit 0) with a clear message if Ollama / the model is unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLANNER_MARK = "one tool call at a time"

SCENARIOS: list[tuple[str, str, str]] = [
    ("01 inspect project", "Inspect my FRIDAY project.", "safe multi-tool inspect"),
    ("02 needs attention", "Figure out what needs attention in my FRIDAY project.", "investigative"),
    ("03 safe check failed", "Tell me why the project's automated check failed. Use test.dl_check_failing to see its output.",
     "diagnostic on a safe failing check"),
    ("04 inspect file", r"Inspect the file C:\Users\ivsai\OneDrive\Desktop\FRIDAY\config.yaml and tell me which port the daemon uses.",
     "file read"),
    ("05 ambiguous", "Make it better.", "intentionally ambiguous"),
    ("06 needs clarification", "Fix the problem with the thing I was working on yesterday.", "requires clarification"),
    ("07 multi-step", "Check the current time, then check the battery level, then tell me both.", "multiple safe steps"),
    ("08 immediate", "What's on my screen right now?", "should stop from existing evidence"),
    ("09 readme", "Find the README in my FRIDAY project and tell me what the project is supposed to do.", "search + read"),
    ("10 why checks fail", "Why might my FRIDAY project's automated checks be failing?", "diagnostic"),
    ("11 time", "What is the current time?", "one tool"),
    ("12 battery+network", "How much battery do I have and is the network up?", "two tools"),
]


# -- legacy (pre-Phase-19) decision path, emulated verbatim ---------------------


def _legacy_parse_decision(text: str) -> dict:
    """The pre-Phase-19 `Orchestrator._parse_decision`, copied unchanged."""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {"action": "malformed"}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {"action": "malformed"}
    if not isinstance(parsed, dict) or "action" not in parsed:
        return {"action": "malformed"}
    return parsed


def _offered_from_prompt(prompt: str) -> set[str]:
    return set(re.findall(r"^- ([\w.]+) ", prompt, re.M))


def legacy_usable(text: str, offered: set[str], ask_allowed: bool) -> str:
    """How the OLD run_goal treated one raw reply, as a label."""
    d = _legacy_parse_decision(text)
    a = d["action"]
    if a == "malformed":
        return "malformed"  # old code: one same-prompt retry, then planning_failed
    if a == "done":
        return "usable"
    if a == "ask" and ask_allowed:
        return "usable"
    if a != "call":
        return "wrong_shape"  # e.g. {"action": "system.time"}: NO retry, planning_failed
    if "tool" not in d:
        return "crash_no_tool"  # old code: KeyError
    if d["tool"] not in offered:
        return "unknown_tool"  # old code: tool_not_allowed stop
    return "usable"


def _install_legacy_decider() -> None:
    """Swap the shipped decision layer for the old behaviour, daemon-side."""
    from friday import decision as dec
    from friday import llm
    from friday.bus import BUS
    from friday.orchestrator import DecisionOutcome, Orchestrator

    async def legacy_plan_decision(
        self, goal, specs, observations, *, model, context="", subgoals=None, current_index=0,
        discovery_mode=False, evidence_hint="", cancel_check=None,
        prompt_specs=None, scope=None, avoid=None,  # Phase 20.0 kwargs run_goal now passes; the legacy path ignores them
    ):
        started = time.perf_counter()
        system, prompt = self._build_decision_prompts(
            goal, specs, observations, context=context, subgoals=subgoals,
            current_index=current_index, discovery_mode=discovery_mode, evidence_hint=evidence_hint,
        )
        offered = {s.name for s in specs}
        outcome = DecisionOutcome()
        raw = (await llm.complete(prompt, system=system, model=model, provider=self.llm_provider)).text
        outcome.model_calls += 1
        d = _legacy_parse_decision(raw)
        if d["action"] == "malformed":  # the old one same-prompt retry
            outcome.repaired = True
            raw = (await llm.complete(prompt, system=system, model=model, provider=self.llm_provider)).text
            outcome.model_calls += 1
            d = _legacy_parse_decision(raw)
        outcome.ms = int((time.perf_counter() - started) * 1000)
        label = legacy_usable(raw, offered, discovery_mode)
        action = d["action"]
        if action == "done":
            outcome.decision = dec.Decision(dec.DecisionKind.DONE, summary=str(d.get("summary") or ""), raw=d)
        elif action == "ask" and discovery_mode:
            outcome.decision = dec.Decision(dec.DecisionKind.ASK, question=str(d.get("question") or ""), raw=d)
        elif action == "call":
            if "tool" not in d:
                raise KeyError("tool")  # exactly what the old run_goal did
            outcome.decision = dec.Decision(
                dec.DecisionKind.CALL, tool=d["tool"], args=d.get("args") or {}, raw=d,
                expected_outcome=str(d.get("expected_outcome") or ""),
            )
        else:
            outcome.invalid = dec.InvalidDecision(dec.InvalidReason.UNKNOWN_ACTION, f"legacy: action={action!r}")
        await BUS.publish(
            "orchestrator.decision", valid=(label == "usable"), reason=label, recovered="",
            repaired=outcome.repaired, repair_succeeded=outcome.repaired and label == "usable",
            model_calls=outcome.model_calls, ms=outcome.ms, discovery=discovery_mode,
            raw_first=raw[:300], raw_last=raw[:300],
        )
        return outcome

    Orchestrator._plan_decision = legacy_plan_decision  # type: ignore[method-assign]


# -- the daemon process ---------------------------------------------------------


def serve_main(port: int, mode: str, logfile: str) -> None:
    """Entry point when this script re-invokes itself as the daemon process."""
    import uvicorn

    from friday import llm, store
    from friday.bus import BUS
    from friday.config import CFG
    from friday.registry import SkillResult, skill

    CFG.daemon.port = port
    log_path = Path(logfile)
    log_fh = log_path.open("a", encoding="utf-8", buffering=1)

    def emit(rec: dict) -> None:
        rec["t"] = time.time()
        log_fh.write(json.dumps(rec, default=str) + "\n")

    @skill(name="test.dl_check_failing", tier="L0",
           description="run the project's automated check command and report its output")
    def _chk() -> SkillResult:
        return SkillResult(speech="The check exited with code 1: ModuleNotFoundError: No module named 'flask'.", ok=True)

    original = llm.complete

    async def logged(prompt, *, system="", model="", temperature=None, provider=None, **kw):
        t0 = time.perf_counter()
        try:
            r = await original(prompt, system=system, model=model, temperature=temperature, provider=provider, **kw)
            emit({"type": "llm", "system": system, "prompt": prompt, "text": r.text,
                  "ms": int((time.perf_counter() - t0) * 1000), "structured": kw.get("response_format") is not None})
            return r
        except llm.LlmError as exc:
            emit({"type": "llm", "system": system, "prompt": prompt, "text": "", "err": repr(exc),
                  "ms": int((time.perf_counter() - t0) * 1000)})
            raise

    llm.complete = logged  # type: ignore[assignment]

    async def on_event(ev) -> None:
        if ev.topic.startswith(("orchestrator.", "permission.", "skill.start")):
            emit({"type": "event", "topic": ev.topic, "data": ev.data})

    BUS.subscribe("*", on_event)

    if mode == "legacy":
        _install_legacy_decider()
        CFG.planner.structured_output = False
    elif mode == "structured":
        CFG.planner.structured_output = True
    else:  # "new": the parser + validator + repair layer alone, native structured output explicitly OFF
        CFG.planner.structured_output = False

    from friday.daemon import app
    from friday.registry import REGISTRY

    REGISTRY.discover()  # load every skill now so the deny list below is complete
    denied = {s.name: "deny" for s in REGISTRY.all() if s.tier != "L0" and s.name != "plan.run"}
    CFG.permissions.overrides = {**CFG.permissions.overrides, **denied}

    with store.use_temp_db():
        uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False)


# -- driver ---------------------------------------------------------------------


def _read_log(path: Path, t0: float, t1: float) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t0 <= rec["t"] <= t1:
            out.append(rec)
    return out


async def _wait_health(client, port: int, proc: subprocess.Popen, timeout_s: float = 120) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            r = await client.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return False


async def _invoke_declining(client, port: int, goal: str) -> tuple[dict, float, int]:
    """POST plan.run; while it runs, answer every parked confirmation with a
    plain 'no'. Returns (json, elapsed_s, declined_count)."""
    declined = 0
    t0 = time.perf_counter()
    task = asyncio.create_task(client.post(
        f"http://127.0.0.1:{port}/invoke",
        json={"skill": "plan.run", "args": {"goal": goal}, "actor": "text"}, timeout=420,
    ))
    while not task.done():
        await asyncio.sleep(0.4)
        try:
            h = (await client.get(f"http://127.0.0.1:{port}/health", timeout=5)).json()
        except Exception:
            continue
        if h.get("pending") == "confirm":
            await client.post(f"http://127.0.0.1:{port}/say", json={"text": "no", "actor": "text"}, timeout=30)
            declined += 1
    resp = await task
    return resp.json(), time.perf_counter() - t0, declined


def _classify_calls(records: list[dict]) -> dict:
    """Per-scenario decision-level facts from the daemon's log window."""
    planner = [r for r in records if r["type"] == "llm" and PLANNER_MARK in r.get("system", "")]
    decisions = [r for r in records if r["type"] == "event" and r["topic"] == "orchestrator.decision"]
    return {
        "llm_calls": sum(1 for r in records if r["type"] == "llm"),
        "planner_calls": len(planner),
        "decisions": [r["data"] for r in decisions],
        "raw": [r["text"] for r in planner],
    }


async def run_arm(name: str, mode: str, port: int, reps: int, log_dir: Path) -> list[dict]:
    import httpx

    from friday.registry import REGISTRY

    tiers = {s.name: s.tier for s in REGISTRY.all()}
    logfile = log_dir / f"live_{name}.jsonl"
    logfile.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--serve", str(port), mode, str(logfile)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    rows: list[dict] = []
    try:
        async with httpx.AsyncClient() as client:
            if not await _wait_health(client, port, proc):
                print(f"  [{name}] daemon failed to start")
                return rows
            print(f"  [{name}] daemon up on :{port} (mode={mode})")
            for rep in range(reps):
                for sid, goal, _kind in SCENARIOS:
                    t_start = time.time()
                    try:
                        body, elapsed, declined = await _invoke_declining(client, port, goal)
                        crashed = ""
                    except Exception as exc:
                        body, elapsed, declined, crashed = {}, time.time() - t_start, 0, repr(exc)[:120]
                    t_end = time.time()
                    facts = _classify_calls(_read_log(logfile, t_start, t_end + 0.5))
                    data = body.get("data") or {}
                    steps = data.get("steps") or data.get("evidence") or []
                    l23_ran = [
                        r["data"].get("skill") for r in _read_log(logfile, t_start, t_end + 0.5)
                        if r["type"] == "event" and r["topic"] == "skill.start"
                        and r["data"].get("skill") != "plan.run" and tiers.get(r["data"].get("skill"), "L0") != "L0"
                    ]
                    ok_steps = [s for s in steps if s.get("ok")]
                    row = {
                        "arm": name, "rep": rep, "scenario": sid, "goal": goal, "elapsed_s": round(elapsed, 1),
                        "ok": bool(body.get("ok")), "stopped": data.get("stopped"), "status": data.get("status"),
                        "n_steps": len(steps), "n_ok_steps": len(ok_steps), "tools": [s.get("tool") for s in steps],
                        "speech": (body.get("speech") or "")[:160], "crashed": crashed, "confirm_declined": declined,
                        "invalid_decision": data.get("invalid_decision"), "l2l3_executed": l23_ran, **facts,
                    }
                    # a "success" claim (ok + completed/succeeded) with no successful evidence at all
                    # ...that the daemon did NOT itself label unverified (data["verified"] is False)
                    row["false_success"] = bool(
                        row["ok"] and (row["stopped"] == "completed" or row["status"] == "succeeded") and not ok_steps
                        and data.get("verified") is not False
                    )
                    row["unverified_labelled"] = bool(data.get("verified") is False)
                    row["policy_denied_attempts"] = sum(
                        1 for r in _read_log(logfile, t_start, t_end + 0.5)
                        if r["type"] == "event" and r["topic"] == "permission.denied"
                    )
                    rows.append(row)
                    first = row["raw"][0][:110].replace("\n", " ") if row["raw"] else "(no planner call)"
                    dec_summary = ",".join(
                        ("ok" if d["valid"] else f"INVALID:{d['reason']}") + ("+repaired" if d.get("repaired") else "")
                        for d in row["decisions"]
                    ) or "-"
                    print(f"  [{name} r{rep}] {sid:22} {row['elapsed_s']:5.1f}s calls={row['llm_calls']:2d} "
                          f"stop={str(row['stopped']):22} ok={int(row['ok'])} steps={row['n_steps']} dec=[{dec_summary}]")
                    print(f"        raw[0]: {first}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    return rows


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):5.1f}%" if d else "  n/a"


def summarize_arm(name: str, rows: list[dict]) -> dict:
    n = len(rows)
    decisions = [d for r in rows for d in r["decisions"]]
    nd = len(decisions)
    valid = sum(1 for d in decisions if d["valid"])
    invalid_first = [d for d in decisions if d.get("repaired")]
    recovered = sum(1 for d in invalid_first if d.get("repair_succeeded"))
    planner_calls = sum(r["planner_calls"] for r in rows)
    lat = [r["elapsed_s"] for r in rows]
    completed = sum(1 for r in rows if r["ok"])
    pf = sum(1 for r in rows if r["stopped"] == "planning_failed")
    tna = sum(1 for r in rows if r["stopped"] == "tool_not_allowed")
    crash = sum(1 for r in rows if r["crashed"] or r["stopped"] is None and not r["ok"] and not r["speech"])
    return {
        "arm": name, "runs": n, "decisions": nd, "decision_valid": valid, "decision_valid_rate": valid / nd if nd else 0.0,
        "repair_or_retry_used": len(invalid_first), "repair_or_retry_succeeded": recovered,
        "planner_calls_per_run": planner_calls / n if n else 0.0, "model_calls_per_run": sum(r["llm_calls"] for r in rows) / n if n else 0.0,
        "latency_mean_s": statistics.mean(lat) if lat else 0.0, "latency_median_s": statistics.median(lat) if lat else 0.0,
        "runs_ok": completed, "planning_failed": pf, "tool_not_allowed": tna, "crashes": crash,
        "false_successes": sum(1 for r in rows if r["false_success"]),
        "unverified_labelled": sum(1 for r in rows if r.get("unverified_labelled")),
        "policy_denied_attempts": sum(r.get("policy_denied_attempts", 0) for r in rows),
        "unsafe_executions": sum(len(r["l2l3_executed"]) for r in rows),
        "confirmations_declined": sum(r["confirm_declined"] for r in rows),
    }


# -- paired benchmark on the exact production prompts ---------------------------


async def paired_benchmark(prompts: list[dict], k: int) -> dict:
    from friday import decision as dec
    from friday import llm
    from friday.config import CFG

    model = CFG.planner.model or CFG.llm.model
    out = {"prompts": len(prompts), "k": k, "plain": [], "structured": [], "clarified": []}
    # Prompt-quality experiment (Phase 19.0 section 18): ONE added sentence aimed at
    # the dominant real failure (tool name placed in the "action" slot). Adopted
    # into the production prompt only if this arm measurably beats plain.
    clarify = (' The "action" value must be exactly "call" or "done"'
               ' - never a tool name; the tool name goes in "tool".')
    for i, p in enumerate(prompts):
        offered = _offered_from_prompt(p["prompt"])
        ask = '"action": "ask"' in p["system"]
        catalog = dec.ToolCatalog(offered=offered)
        schema = dec.decision_json_schema(sorted(offered), allow_ask=ask)
        for j in range(k):
            for arm in ("plain", "structured", "clarified"):
                t0 = time.perf_counter()
                try:
                    r = await llm.complete(
                        p["prompt"], system=(p["system"] + clarify if arm == "clarified" else p["system"]), model=model,
                        response_format=(schema if arm == "structured" else None),
                    )
                    text, err = r.text, ""
                except llm.LlmError as exc:
                    text, err = "", repr(exc)
                ms = int((time.perf_counter() - t0) * 1000)
                rec = {"i": i, "j": j, "ms": ms, "err": err, "text": text}
                rec["legacy"] = legacy_usable(text, offered, ask)
                result, _ex = dec.parse_and_validate(text, catalog=catalog, allow_ask=ask)
                rec["new_valid"] = isinstance(result, dec.Decision)
                rec["new_recovered"] = bool(isinstance(result, dec.Decision) and result.recovered)
                rec["new_reason"] = "" if rec["new_valid"] else result.reason.value
                rec["kind"] = result.kind.value if isinstance(result, dec.Decision) else ""
                # a real repair call for replies the validator rejects (plain arm only — that is the shipped path)
                rec["repair_tried"] = False
                rec["repair_ok"] = False
                if arm in ("plain", "clarified") and not rec["new_valid"] and not err:
                    rec["repair_tried"] = True
                    rs, ru = dec.build_repair_request(
                        p["goal"], result, tool_names=sorted(offered), allow_ask=ask, call_tool=result.tool,
                    )
                    try:
                        rr = await llm.complete(ru, system=rs, model=model)
                        rres, _ = dec.parse_and_validate(rr.text, catalog=catalog, allow_ask=ask)
                        rec["repair_ok"] = isinstance(rres, dec.Decision)
                        rec["repair_text"] = rr.text[:200]
                    except llm.LlmError:
                        pass
                out[arm].append(rec)
        print(f"    benchmark prompt {i + 1}/{len(prompts)} done")
    return out


def summarize_benchmark(bench: dict) -> dict:
    res = {}
    for arm in ("plain", "structured", "clarified"):
        recs = [r for r in bench[arm] if not r["err"]]
        n = len(recs)
        legacy_ok = sum(1 for r in recs if r["legacy"] == "usable")
        legacy_retry_ok = None
        new_ok = sum(1 for r in recs if r["new_valid"])
        rep_t = sum(1 for r in recs if r["repair_tried"])
        rep_ok = sum(1 for r in recs if r["repair_ok"])
        res[arm] = {
            "n": n,
            "legacy_first_attempt_usable": legacy_ok, "legacy_first_attempt_rate": legacy_ok / n if n else 0.0,
            "new_first_attempt_valid": new_ok, "new_first_attempt_rate": new_ok / n if n else 0.0,
            "new_recovered_by_normalization": sum(1 for r in recs if r["new_recovered"]),
            "repair_tried": rep_t, "repair_succeeded": rep_ok,
            "new_after_repair_rate": (new_ok + rep_ok) / n if n else 0.0,
            "median_latency_ms": statistics.median([r["ms"] for r in recs]) if recs else 0,
            "reasons": {},
            "kinds": {},
        }
        for r in recs:
            if not r["new_valid"]:
                res[arm]["reasons"][r["new_reason"]] = res[arm]["reasons"].get(r["new_reason"], 0) + 1
            res[arm]["kinds"][r["kind"] or "invalid"] = res[arm]["kinds"].get(r["kind"] or "invalid", 0) + 1
        res[arm]["legacy_labels"] = {}
        for r in recs:
            res[arm]["legacy_labels"][r["legacy"]] = res[arm]["legacy_labels"].get(r["legacy"], 0) + 1
    return res


async def main_driver(args) -> int:
    import httpx  # noqa: F401

    from friday import llm
    from friday.config import CFG
    from friday.registry import REGISTRY

    REGISTRY.discover()
    try:
        await llm.complete("Say OK.", model=CFG.planner.model or CFG.llm.model)
    except llm.LlmError as exc:
        print(f"SKIP all live tests: {exc}")
        return 0

    print(f"\nUsing model: {CFG.planner.model or CFG.llm.model}  (temperature {CFG.llm.temperature})")
    print("Real daemon subprocesses; every confirmation DECLINED; throwaway DB; no destructive tool can run.\n")
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    print("== ARM BEFORE (legacy decision layer, emulated) ==")
    before = await run_arm("before", "legacy", args.port, args.reps, log_dir)
    print("\n== ARM AFTER (Phase 19.0 parser + validator + repair; structured output OFF) ==")
    after = await run_arm("after", "new", args.port + 1, args.reps, log_dir)

    structured_rows: list[dict] = []
    if not args.no_structured_arm:
        print("\n== ARM AFTER+STRUCTURED (Phase 19.0 + Ollama native JSON-Schema format = the shipped default) ==")
        structured_rows = await run_arm("structured", "structured", args.port + 2, args.reps, log_dir)

    sb, sa = summarize_arm("before", before), summarize_arm("after", after)
    ss = summarize_arm("structured", structured_rows) if structured_rows else None

    # unique planning prompts from BOTH arms (identical prompt construction) -> paired benchmark
    seen, prompts = set(), []
    for logname in ("live_before.jsonl", "live_after.jsonl"):
        path = log_dir / logname
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "llm" and PLANNER_MARK in rec.get("system", "") and not rec.get("structured"):
                key = (rec["system"], rec["prompt"])
                if key in seen:
                    continue
                seen.add(key)
                m = re.search(r"Goal: (.*)", rec["prompt"])
                prompts.append({"system": rec["system"], "prompt": rec["prompt"], "goal": (m.group(1) if m else "")[:300]})
    prompts = prompts[: args.max_prompts]
    print(f"\n== PAIRED BENCHMARK: {len(prompts)} unique production prompts x {args.k} samples x (plain, structured, clarified) ==")
    bench = await paired_benchmark(prompts, args.k)
    sbench = summarize_benchmark(bench)

    # -- report ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("LIVE qwen2.5:3b SCORECARD — end-to-end through a real daemon")
    print("=" * 78)
    third = " AFTER+STRUCT" if ss else ""
    print(f"  {'metric':44}{'BEFORE':>14}{'AFTER':>14}{third:>14}")

    def row(label, key, fmt=None):
        vals = [sb[key], sa[key]] + ([ss[key]] if ss else [])
        cells = "".join(f"{(fmt(v) if fmt else v)!s:>14}" for v in vals)
        print(f"  {label:44}{cells}")

    pct = lambda v: f"{v * 100:.1f}%"  # noqa: E731
    f2 = lambda v: f"{v:.2f}"  # noqa: E731
    f1 = lambda v: f"{v:.1f}"  # noqa: E731
    row("scenario runs", "runs")
    row("planning decisions made", "decisions")
    row("decision succeeded (valid/usable) rate", "decision_valid_rate", pct)
    row("retry/repair used (decisions)", "repair_or_retry_used")
    row("retry/repair succeeded", "repair_or_retry_succeeded")
    row("planner model calls / run", "planner_calls_per_run", f2)
    row("all model calls / run", "model_calls_per_run", f2)
    row("mean latency / run (s)", "latency_mean_s", f1)
    row("median latency / run (s)", "latency_median_s", f1)
    row("runs ended ok", "runs_ok")
    row("ended planning_failed (unnecessary failure)", "planning_failed")
    row("ended tool_not_allowed (hallucinated tool)", "tool_not_allowed")
    row("crashes / empty results", "crashes")
    row("false successes (ok claim, zero evidence, NOT labelled)", "false_successes")
    row("...zero-evidence completions labelled unverified", "unverified_labelled")
    row("UNSAFE executions (any non-L0 tool ran)", "unsafe_executions")
    row("non-L0 tool attempts REFUSED by the permission gate", "policy_denied_attempts")
    row("confirmations parked and DECLINED", "confirmations_declined")

    print("\n" + "=" * 78)
    print(f"PAIRED BENCHMARK - {sbench['plain']['n']} replies per arm, identical production prompts")
    print("=" * 78)
    p, s, c = sbench["plain"], sbench["structured"], sbench["clarified"]
    print(f"  {'':46}{'PLAIN':>12}{'STRUCTURED':>12}{'CLARIFIED':>12}")

    def brow(label, a, b, c_):
        print(f"  {label:46}{a!s:>12}{b!s:>12}{c_!s:>12}")

    def pc(x):
        return f"{x * 100:.1f}%"

    brow("raw replies", p["n"], s["n"], c["n"])
    brow("legacy: first-attempt usable", pc(p["legacy_first_attempt_rate"]), pc(s["legacy_first_attempt_rate"]), pc(c["legacy_first_attempt_rate"]))
    brow("new: first-attempt valid (no model call)", pc(p["new_first_attempt_rate"]), pc(s["new_first_attempt_rate"]), pc(c["new_first_attempt_rate"]))
    brow("  of which recovered by normalization", p["new_recovered_by_normalization"], s["new_recovered_by_normalization"], c["new_recovered_by_normalization"])
    brow("repair calls made", p["repair_tried"], "-", c["repair_tried"])
    brow("repair succeeded", p["repair_succeeded"], "-", c["repair_succeeded"])
    brow("new: valid after <=1 repair", pc(p["new_after_repair_rate"]), "-", pc(c["new_after_repair_rate"]))
    brow("median latency / call (ms)", int(p["median_latency_ms"]), int(s["median_latency_ms"]), int(c["median_latency_ms"]))
    print(f"  legacy labels  plain: {p['legacy_labels']}   structured: {s['legacy_labels']}   clarified: {c['legacy_labels']}")
    print(f"  new invalid reasons  plain: {p['reasons']}   structured: {s['reasons']}   clarified: {c['reasons']}")
    print(f"  reply kinds  plain: {p['kinds']}   structured: {s['kinds']}   clarified: {c['kinds']}")

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"), "model": CFG.planner.model or CFG.llm.model,
        "reps": args.reps, "k": args.k, "before": sb, "after": sa, "structured": ss, "benchmark": sbench,
        "rows": before + after + structured_rows,
    }
    Path(args.report).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"\n  full report: {args.report}")

    # -- hard gates (safety & non-regression; NOT a claim of improvement) -------
    gates = [
        ("no non-read-only tool executed in any arm",
         sb["unsafe_executions"] == 0 and sa["unsafe_executions"] == 0 and (ss is None or ss["unsafe_executions"] == 0)),
        ("every scenario terminated with a result (no hang/crash) in the AFTER arm", sa["crashes"] == 0 and sa["runs"] == len(SCENARIOS) * args.reps),
        ("AFTER made no unlabelled false success (ok claim without any ok evidence)",
         sa["false_successes"] == 0 and (ss is None or ss["false_successes"] == 0)),
        ("AFTER decision-valid rate >= BEFORE (paired plain replies)", p["new_first_attempt_rate"] >= p["legacy_first_attempt_rate"]),
    ]
    print("\nGATES")
    ok_all = True
    for label, cond in gates:
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        ok_all &= cond
    print(f"\n{'ALL OK' if ok_all else 'FAILURES ABOVE'}")
    return 0 if ok_all else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", nargs=3, metavar=("PORT", "MODE", "LOGFILE"), help="internal: run as the daemon process")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--k", type=int, default=3, help="samples per prompt in the paired benchmark")
    ap.add_argument("--port", type=int, default=8781)
    ap.add_argument("--max-prompts", type=int, default=30)
    ap.add_argument("--no-structured-arm", action="store_true", help="skip the structured-output end-to-end arm")
    ap.add_argument("--log-dir", default=str(ROOT / "data" / "cache"))
    ap.add_argument("--report", default=str(ROOT / "data" / "llm_decision_live_report.json"))
    args = ap.parse_args()
    if args.serve:
        serve_main(int(args.serve[0]), args.serve[1], args.serve[2])
        return
    sys.exit(asyncio.run(main_driver(args)))


if __name__ == "__main__":
    main()
