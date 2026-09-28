"""Phase 21.0 — PLANNER CONTEXT BUDGET: scorecard + live measurement.

Phase 20.0 found (by accident, in a live benchmark) that Ollama's loaded window on
this machine is 4096 tokens, that FRIDAY never asked for another, and that a longer
prompt is silently truncated at the head — the model then answers from a fragment
and no validity check notices. Two changes, both centrally configured:

  1. `CFG.llm.num_ctx` -> `options.num_ctx` on every Ollama chat request (one value
     for every caller: Ollama reloads the model when the window changes between
     requests, so a per-caller size would thrash a 4 GB GPU).
  2. `Orchestrator._fit_prompt` — before each planner call the prompt size is
     estimated and, if it will not fit `num_ctx` x `prompt_budget_fraction`, shrunk
     lowest-value-first (ambient context -> old history -> tool descriptions), never
     touching the goal, the tool names/arguments, the scope line, the user's own
     instruction context or the newest steps.

Default run (deterministic, no Ollama):
  A  the value reaches the Ollama payload — every call of a real run, including the
     structured-output retry, the repair call and decomposition
  B  the size estimate and the shrink policy (order, protected content, bounds)
  C  the budget guard inside the real run_goal
  D  the shipped configuration

`--live` (needs Ollama + qwen2.5:3b; read-only, sends prompts only): measures real
prompt sizes, truncation, latency and VRAM for candidate windows and writes
data/context_budget_live_report.json — the data the shipped value was chosen from.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import subprocess
import sys
import time

import phase21_common as H
from phase21_common import ROOT, ScriptedPlanner, call, check, done, scenario

import httpx  # noqa: E402

from friday import intent, llm, store  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG, Config  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec, _tool_specs  # noqa: E402
from friday.registry import REGISTRY, SkillResult  # noqa: E402

REGISTRY.discover()

MODEL = CFG.llm.model or "qwen2.5:3b"
CANDIDATE_CTX = (4096, 6144, 8192)


# -- shared builders -------------------------------------------------------------------


def real_specs() -> list[ToolSpec]:
    return _tool_specs([s.name for s in REGISTRY.all() if s.name != "plan.run"])


def make_obs(n: int, speech_len: int = 150) -> list[Observation]:
    return [
        Observation(PlanStep(f"files.read", {"path": f"C:/proj/file_{i}.py"}), True,
                    ("contents of file %d: " % i) + "x" * max(0, speech_len - 24))
        for i in range(n)
    ]


AMBIENT = (
    "Active window: Visual Studio Code - FRIDAY. Open windows: Chrome, Terminal, Explorer, WhatsApp. "
    + "Working memory: the user is working on the FRIDAY project; recent focus was planner tests. " * 3
    + "\n\nRELEVANT PAST EXPERIENCE:\n"
    + "\n".join(f"- SUCCEEDED before: 'fix startup issue {i}' using files.read, files.write, project.inspect; it took 6 steps." for i in range(30))
)[:5000]
PROTECTED = "User clarified: the backend project, not the college one."
EVIDENCE = "Discovery evidence so far:\n- [observed_fact] project.inspect: python project with 3 modules and tests"


def orch() -> Orchestrator:
    return Orchestrator(llm_provider=None, tool_specs=real_specs(), runner=None)


def build(goal="Fix my Flask startup issue.", *, specs=None, context="", n_obs=0, scope=None, **kw):
    o = orch()
    return o, o._build_decision_prompts(goal, specs or real_specs(), make_obs(n_obs), context=context, scope=scope, **kw)


def reset() -> None:
    INTEL.reset()
    CFG.llm.num_ctx = 4096
    CFG.planner.prompt_budget = True
    CFG.planner.prompt_budget_fraction = 0.85
    CFG.planner.chars_per_token = 3.0
    CFG.planner.structured_output = False
    CFG.planner.decision_repair_attempts = 1
    CFG.planner.intent_guard = True
    CFG.planner.intent_prefilter = True
    CFG.planner.goal_coverage = True
    CFG.desktop_observer.enabled = False


# ================================================================================
# A — the value reaches Ollama
# ================================================================================


class FakeOllama:
    """An httpx MockTransport standing in for the Ollama daemon: records every request body."""

    def __init__(self, replies: list[str], *, reject_format_once: bool = False, prompt_eval: int = 123) -> None:
        self.replies, self.payloads, self.i = list(replies), [], 0
        self.reject_format_once, self.prompt_eval = reject_format_once, prompt_eval
        self._rejected = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.payloads.append(body)
        if self.reject_format_once and "format" in body and not self._rejected:
            self._rejected = True
            return httpx.Response(400, json={"error": "format not supported"})
        text = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return httpx.Response(200, json={"message": {"role": "assistant", "content": text}, "prompt_eval_count": self.prompt_eval})

    def provider(self) -> llm.OllamaProvider:
        prov = llm.OllamaProvider("http://fake-ollama")

        async def _client():
            return httpx.AsyncClient(base_url="http://fake-ollama", transport=httpx.MockTransport(self.handler))

        prov._client = _client  # type: ignore[method-assign]
        return prov


def opts(fake: FakeOllama) -> list[dict]:
    return [p.get("options", {}) for p in fake.payloads]


async def section_a() -> None:
    scenario("A: num_ctx reaches the Ollama request — centrally, on every call")
    reset()
    fake = FakeOllama(["ok"])
    await llm.complete("hi", model="m", provider=fake.provider())
    check("llm.complete sends options.num_ctx = CFG.llm.num_ctx (the one central value)", opts(fake)[0].get("num_ctx") == 4096, str(opts(fake)))
    CFG.llm.num_ctx = 6144
    fake = FakeOllama(["ok"])
    await llm.complete("hi", model="m", provider=fake.provider())
    check("...change the config, the request changes", opts(fake)[0].get("num_ctx") == 6144)
    fake = FakeOllama(["ok"])
    await llm.complete("hi", model="m", provider=fake.provider(), num_ctx=2048)
    check("an explicit per-call value still wins (for measurement)", opts(fake)[0].get("num_ctx") == 2048)
    CFG.llm.num_ctx = 0
    fake = FakeOllama(["ok"])
    await llm.complete("hi", model="m", provider=fake.provider())
    check("CFG.llm.num_ctx = 0: nothing is sent (provider default, the pre-Phase-21 behaviour)", "num_ctx" not in opts(fake)[0])
    reset()
    fake = FakeOllama(["ok"])
    prov = fake.provider()
    await prov.complete(llm.LlmRequest(messages=[llm.LlmMessage("user", "hi")], model="m"))
    check("a caller that builds an LlmRequest directly (no num_ctx) is unchanged", "num_ctx" not in opts(fake)[0])
    fake = FakeOllama(["ok"])
    await fake.provider().complete(llm.LlmRequest(messages=[llm.LlmMessage("user", "hi")], model="m", num_ctx=1234))
    check("a request that sets it is honoured", opts(fake)[0].get("num_ctx") == 1234)
    fake = FakeOllama(["ok"], reject_format_once=True)
    await llm.complete("hi", model="m", provider=fake.provider(), response_format={"type": "object"})
    check("the structured-output retry (400 -> resend without `format`) keeps num_ctx on BOTH requests",
          len(fake.payloads) == 2 and all(o.get("num_ctx") == 4096 for o in opts(fake)) and "format" not in fake.payloads[1])
    check("Config parses llm.num_ctx from yaml-shaped input", Config(**{"llm": {"num_ctx": 6000}}).llm.num_ctx == 6000)

    scenario("A2: a real planner run — every model call carries the same window")
    reset()
    CFG.planner.structured_output = True
    fake = FakeOllama([
        '{"subgoals": [{"description": "read a", "rationale": "r", "success_evidence": "e"}, {"description": "read b", "rationale": "r", "success_evidence": "e"}]}',
        "this is not json at all",                        # invalid -> repair call
        json.dumps({"action": "call", "tool": "system.time", "args": {}}),
        json.dumps({"action": "done", "summary": "3:45"}),
    ])
    original = llm.get_provider
    llm.get_provider = lambda name=None: fake.provider()
    events = []

    async def on_decision(ev) -> None:
        events.append(ev.data)

    BUS.subscribe("orchestrator.decision", on_decision)
    try:
        specs = [s for s in real_specs() if s.name in ("system.time", "system.battery")]
        o = Orchestrator(tools=[s.name for s in specs], tool_specs=specs, runner=_stub_runner, max_steps=6)
        subs = await o.decompose_goal("read a and read b")
        res = await o.run_goal("Check the time.", action_scope=intent.derive_scope("Check the time."))
    finally:
        llm.get_provider = original
        BUS.unsubscribe("orchestrator.decision", on_decision)
    nums = {p.get("options", {}).get("num_ctx") for p in fake.payloads}
    check("decomposition + first decision + REPAIR + follow-up decision: 4 model calls", len(fake.payloads) == 4, str(len(fake.payloads)))
    check("...all four carry num_ctx=4096 and only that value (no reload thrash)", nums == {4096}, str(nums))
    check("...the structured-output constraint was on for the decisions (and did not displace num_ctx)", any("format" in p for p in fake.payloads[1:]))
    check("the run itself worked end to end", res.ok and res.stopped == "completed" and len(subs) == 2)
    check("the provider's own prompt size was captured (prompt_eval_count -> the decision event)", events and events[0]["budget"].get("prompt_eval_count") == 123, str(events[:1]))
    check("the decision event carries the window and the estimate", events[0]["budget"].get("num_ctx") == 4096 and events[0]["budget"].get("est_tokens", 0) > 0)


async def _stub_runner(tool: str, args: dict, actor: str) -> SkillResult:
    return SkillResult(speech="It's 3:45 PM." if tool == "system.time" else f"{tool} ok")


# ================================================================================
# B — estimate and shrink policy
# ================================================================================


def section_b() -> None:
    scenario("B: estimate_tokens and the shrink policy")
    reset()
    check("estimate is chars / chars_per_token + a small template allowance", Orchestrator.estimate_tokens("a" * 300) == 100 + 24)
    check("...monotonic in length", Orchestrator.estimate_tokens("a" * 30) < Orchestrator.estimate_tokens("a" * 3000))
    CFG.planner.chars_per_token = 2.0
    check("...follows CFG.planner.chars_per_token (conservative = smaller)", Orchestrator.estimate_tokens("a" * 300) == 150 + 24)
    reset()

    specs = real_specs()
    o = orch()
    kw = dict(scope=None, avoid=None, coverage_hint="")
    ctx = f"{PROTECTED}\n\n{AMBIENT}\n\n{EVIDENCE}"

    # a window big enough for everything: byte-identical, nothing shrunk
    CFG.llm.num_ctx = 32768
    sysp, prompt, info = o._fit_prompt("Fix my Flask startup issue.", specs, make_obs(12), context=ctx, **kw)
    ref = o._build_decision_prompts("Fix my Flask startup issue.", specs, make_obs(12), context=ctx, **kw)
    check("fits: the prompt is byte-identical to the unshrunk builder's (no behaviour change when it fits)", (sysp, prompt) == ref and info["shrunk"] == [])
    full_est = info["est_tokens"]
    check("...info reports the window, the budget and the estimate", info["num_ctx"] == 32768 and info["budget_tokens"] == int(32768 * 0.85) and full_est > 1000)

    CFG.llm.num_ctx = 4096
    budget = int(4096 * 0.85)
    s2, p2, i2 = o._fit_prompt("Fix my Flask startup issue.", specs, make_obs(12), context=ctx, **kw)
    check("a worst-case prompt (all 93 tools + 5k ambient + 12 steps) is over the 4096 budget before shrinking", i2["est_tokens_full"] > budget, str(i2["est_tokens_full"]))
    check("...after shrinking it fits (estimate <= budget)", i2["est_tokens"] <= budget and not i2["over_budget"], str(i2))
    check("...the steps ran in policy order: context first", i2["shrunk"][0] == "context_halved")
    order = [s.split("_")[0] for s in i2["shrunk"]]
    check("...and in order: context -> history -> tools", order == sorted(order, key={"context": 0, "history": 1, "tools": 2}.get))
    check("...it is strictly smaller than the unshrunk prompt", len(s2) + len(p2) < len(sysp) + len(prompt))

    text = s2 + p2
    check("NEVER dropped: the goal", "Goal: Fix my Flask startup issue." in p2)
    check("NEVER dropped: the user's own instruction context", PROTECTED in p2)
    check("NEVER dropped: this run's evidence block", EVIDENCE.split("\n")[0] in p2)
    check("NEVER dropped: every tool NAME", all(f"- {s.name} " in p2 for s in specs))
    check("NEVER dropped: every tool's argument names", all((s.params.split(" (")[0].replace("REQUIRED ", "").split(";")[0] in p2) for s in specs if s.params))
    check("NEVER dropped: the newest step", "12. called files.read" in p2)
    check("...older steps are summarized exactly when a history step was taken", any(s.startswith("history") for s in i2["shrunk"]) == ("earlier steps omitted" in p2))
    check("ambient experience was the thing cut", "SUCCEEDED before: 'fix startup issue 29'" not in p2)

    real_names = {s.name for s in specs}
    tool_names = lambda t: {ln.split(" ")[1] for ln in t.splitlines() if ln.startswith("- ") and ln.split(" ")[1] in real_names}  # noqa: E731
    check("shrinking never ADDS a tool (its tool set is exactly the unshrunk one)", tool_names(p2) == tool_names(prompt))

    # protected blocks + goal + scope line + avoid + coverage hint survive even the extreme
    CFG.llm.num_ctx = 512
    scope = intent.derive_scope("Inspect my project.")
    s3, p3, i3 = o._fit_prompt("Inspect my project.", specs, make_obs(12), context=ctx, scope=scope,
                               avoid=("files.read", "already_tried"), coverage_hint="battery level")
    check("impossible budget (512 tokens): over_budget is reported, not hidden", i3["over_budget"] and "tools_compact" in i3["shrunk"])
    check("...every shrink step was tried before giving up", i3["shrunk"][0] == "context_halved" and i3["shrunk"][-1] == "tools_compact")
    check("...and it still sends a complete prompt: goal, protected context, evidence", "Goal: Inspect my project." in p3 and PROTECTED in p3 and "Discovery evidence" in p3)
    check("...the scope line and the avoid clause (system prompt) are intact",
          scope.prompt_line() in s3 and "Your last call to 'files.read' was blocked" in s3)
    check("...the coverage hint is intact", "battery level" in s3)
    check("...tool lines went compact (no descriptions) as the LAST resort", "- system.time [L0 observe] | args: none" in p3 and REGISTRY.get("system.time").description not in p3)

    reset()
    CFG.planner.prompt_budget = False
    CFG.llm.num_ctx = 512
    s4, p4, i4 = o._fit_prompt("Fix my Flask startup issue.", specs, make_obs(12), context=ctx, **kw)
    check("prompt_budget=False: nothing is ever shrunk (the pre-Phase-21 behaviour)", (s4, p4) == ref and i4["shrunk"] == [] and not i4["over_budget"])
    reset()
    CFG.llm.num_ctx = 0
    s5, p5, i5 = o._fit_prompt("Fix my Flask startup issue.", specs, make_obs(12), context=ctx, **kw)
    check("num_ctx=0 (window unknown): no budget, nothing shrunk", (s5, p5) == ref and i5["budget_tokens"] == 0)

    reset()
    a = o._fit_prompt("Fix my Flask startup issue.", specs, make_obs(12), context=ctx, **kw)
    b = o._fit_prompt("Fix my Flask startup issue.", specs, make_obs(12), context=ctx, **kw)
    check("deterministic: same inputs, same prompt and info", a == b)

    scenario("B2: individual mechanisms")
    ts = ToolSpec(name="x.y", description="does a long thing", tier="L1", action="modify", params="REQUIRED a (str); optional b (int, default 1)")
    check("ToolSpec.line(compact=True) drops the description, keeps name/tier/args",
          "does a long thing" not in ts.line(True) and "x.y" in ts.line(True) and "REQUIRED a (str)" in ts.line(True))
    check("ToolSpec.line() default is unchanged", ts.line() == "- x.y [L1 modify] : does a long thing. | args: REQUIRED a (str); optional b (int, default 1)")
    o2 = orch()
    _, (s6, p6) = build(n_obs=10, history_window=3)
    check("history_window=3: the last three steps are shown with their TRUE numbers, the rest counted",
          "(7 earlier steps omitted" in p6 and "8. called" in p6 and "10. called" in p6 and "1. called" not in p6.replace("10. called", ""))
    _, (s7, p7) = build(n_obs=2, history_window=5)
    _, (s8, p8) = build(n_obs=2)
    check("a window larger than the history changes nothing", p7 == p8)
    _, (s9, p9) = build(coverage_hint="battery level; network status")
    check("coverage_hint appears in the system prompt, once", s9.count("battery level; network status") == 1 and "Do not reply done yet" in s9)
    _, (s10, p10) = build()
    check("...and is absent otherwise", "Do not reply done yet" not in s10)


# ================================================================================
# C — inside the real run_goal
# ================================================================================


async def section_c() -> None:
    scenario("C: the guard inside the real run_goal")
    reset()
    CFG.llm.num_ctx = 4096
    specs = real_specs()
    huge = f"{PROTECTED}\n\n{AMBIENT}"
    planner = ScriptedPlanner([call("system.time"), done("3:45")])
    events = []

    async def on_decision(ev) -> None:
        events.append(ev.data["budget"])

    BUS.subscribe("orchestrator.decision", on_decision)
    try:
        o = Orchestrator(tools=[s.name for s in specs], tool_specs=specs, runner=_stub_runner, llm_provider=planner, max_steps=6)
        res = await o.run_goal("Check the time.", context=huge, action_scope=None)
    finally:
        BUS.unsubscribe("orchestrator.decision", on_decision)
    est = Orchestrator.estimate_tokens(planner.system_of(0), planner.prompt_of(0))
    check("full registry + 5k-char ambient context at a 4096 window: the model was sent a prompt that fits", est <= int(4096 * 0.85), str(est))
    check("...the run completed normally with a valid decision", res.ok and res.stopped == "completed")
    check("...the event says what was shrunk", events and events[0]["shrunk"] and not events[0]["over_budget"], str(events[:1]))
    check("...the user's instruction context still reached the model", PROTECTED in planner.prompt_of(0))

    reset()
    CFG.llm.num_ctx = 32768
    planner = ScriptedPlanner([call("system.time"), done("3:45")])
    o = Orchestrator(tools=[s.name for s in specs], tool_specs=specs, runner=_stub_runner, llm_provider=planner, max_steps=6)
    await o.run_goal("Check the time.", context=huge, action_scope=None)
    check("a 32k window: the same context is sent whole", "SUCCEEDED before: 'fix startup issue 29'" in planner.prompt_of(0))

    reset()
    scope = intent.derive_scope("Inspect my project.")
    keep = intent.offered_for(scope, [(s.name, s.tier) for s in specs])
    shown = [s for s in specs if s.name in keep]
    o = orch()
    _, p_ro, i_ro = o._fit_prompt("Inspect my project.", shown, [], context="", scope=scope, avoid=None, coverage_hint="")
    check(f"the prefiltered read-only prompt ({len(shown)} of {len(specs)} tools) fits 4096 with NO shrinking", i_ro["shrunk"] == [] and i_ro["est_tokens"] <= int(4096 * 0.85), str(i_ro))
    _, p_full, i_full = o._fit_prompt("Fix my Flask startup issue.", specs, [], context="", scope=None, avoid=None, coverage_hint="")
    check("the UNfiltered 93-tool prompt (the Phase 20 hazard) is over the 4096 budget before any history or context", i_full["est_tokens_full"] > int(4096 * 0.85), str(i_full["est_tokens_full"]))
    reset()


# ================================================================================
# D — shipped configuration
# ================================================================================


def section_d() -> None:
    scenario("D: shipped configuration")
    fresh = Config()
    check("default llm.num_ctx is set explicitly (not left to the provider)", fresh.llm.num_ctx > 0)
    check("...and is inside the bounds this 4 GB machine was measured for (see the report)", 4096 <= fresh.llm.num_ctx <= 8192, str(fresh.llm.num_ctx))
    check("the budget guard is ON by default with a reserve for the reply", fresh.planner.prompt_budget and 0.5 <= fresh.planner.prompt_budget_fraction <= 0.95)
    check("chars_per_token is conservative (<= 3.5)", fresh.planner.chars_per_token <= 3.5)
    y = (ROOT / "config.yaml").read_text(encoding="utf-8")
    check("config.yaml documents num_ctx", "num_ctx" in y)
    check("only ONE place decides the window: the OllamaProvider reads it from the request, the request from CFG.llm",
          "num_ctx" in (ROOT / "friday" / "llm.py").read_text(encoding="utf-8") and "CFG.llm.num_ctx" in (ROOT / "friday" / "llm.py").read_text(encoding="utf-8"))


# ================================================================================
# live measurement (--live)
# ================================================================================


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def _vram_used_mib() -> int | None:
    out = _run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]).strip().splitlines()
    return int(out[0]) if out and out[0].strip().isdigit() else None


async def _ps() -> dict:
    async with httpx.AsyncClient(base_url=CFG.llm.base_url, timeout=10) as c:
        data = (await c.get("/api/ps")).json()
    m = next((m for m in data.get("models", []) if m.get("name", "").startswith(MODEL.split(":")[0])), {})
    return {"context_length": m.get("context_length"), "size_mib": round(m.get("size", 0) / 2**20), "size_vram_mib": round(m.get("size_vram", 0) / 2**20)}


async def _ask(system: str, prompt: str, ctx: int | None, *, num_predict: int | None = None, fmt: dict | None = None):
    req = llm.LlmRequest(
        messages=[llm.LlmMessage("system", system), llm.LlmMessage("user", prompt)], model=MODEL,
        temperature=CFG.llm.temperature, max_tokens=num_predict, response_format=fmt, num_ctx=ctx,
    )
    t0 = time.perf_counter()
    r = await llm.get_provider().complete(req)
    return r, (time.perf_counter() - t0) * 1000


def _shapes() -> dict[str, tuple[str, str]]:
    """Realistic production prompt shapes, built by the production builder."""
    specs = real_specs()
    o = orch()
    shapes: dict[str, tuple[str, str]] = {}

    def shape(name, goal, *, filtered: bool, context="", n_obs=0, hint=""):
        scope = intent.derive_scope(goal) if filtered else None
        shown = specs
        if scope is not None:
            keep = intent.offered_for(scope, [(s.name, s.tier) for s in specs])
            shown = [s for s in specs if s.name in keep] or specs
        shapes[name] = o._build_decision_prompts(goal, shown, make_obs(n_obs), context=context, scope=scope, coverage_hint=hint)

    shape("readonly_prefiltered", "Inspect my project.", filtered=True)
    shape("readonly_prefiltered_ambient", "Inspect my project.", filtered=True, context=AMBIENT, n_obs=4)
    shape("fix_prefiltered_ambient_hist", "Fix my Flask startup issue.", filtered=True, context=AMBIENT, n_obs=8)
    shape("send_prefiltered_ambient", "Send Rahul a message that I'll be late.", filtered=True, context=AMBIENT, n_obs=3)
    shape("unfiltered_full_registry", "Inspect my project.", filtered=False)
    shape("unfiltered_full_ambient_hist", "Fix my Flask startup issue.", filtered=False, context=AMBIENT, n_obs=8)
    return shapes


async def live() -> int:
    report: dict = {"model": MODEL, "candidates": list(CANDIDATE_CTX), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"\n=== LIVE context-budget measurement: {MODEL} on {_run(['nvidia-smi','--query-gpu=name','--format=csv,noheader']).strip() or 'GPU?'} ===\n")
    shapes = _shapes()

    print("-- 1. true prompt sizes (a 32768 window so nothing is truncated; num_predict=1) --")
    truth: dict[str, dict] = {}
    for name, (s, p) in shapes.items():
        r, ms = await _ask(s, p, 32768, num_predict=1)
        t = r.raw.get("prompt_eval_count")
        chars = len(s) + len(p)
        truth[name] = {"chars": chars, "tokens": t, "chars_per_token": round(chars / t, 2) if t else None, "est_tokens_3.0": Orchestrator.estimate_tokens(s, p)}
        print(f"  {name:34} {chars:6d} chars  {t:5d} tokens  {truth[name]['chars_per_token']} chars/token  (estimate at 3.0: {truth[name]['est_tokens_3.0']})")
    report["true_sizes"] = truth
    cpt_min = min(v["chars_per_token"] for v in truth.values())
    report["min_chars_per_token"] = cpt_min
    print(f"  -> the estimate is conservative iff chars_per_token <= {cpt_min}; configured {CFG.planner.chars_per_token}")

    print("\n-- 2. truncation per window (same prompt, num_ctx = candidate; truncated iff the model evaluated fewer tokens than the truth) --")
    trunc: dict[int, dict] = {}
    for ctx in CANDIDATE_CTX:
        row = {}
        for name, (s, p) in shapes.items():
            r, _ = await _ask(s, p, ctx, num_predict=1)
            seen, want = r.raw.get("prompt_eval_count"), truth[name]["tokens"]
            row[name] = {"evaluated": seen, "true": want, "truncated": bool(seen is not None and want and seen < want - 2)}
        trunc[ctx] = row
        n = sum(1 for v in row.values() if v["truncated"])
        print(f"  num_ctx={ctx:5d}: {n}/{len(row)} prompt shapes truncated: " + ", ".join(k for k, v in row.items() if v["truncated"]) if n else f"  num_ctx={ctx:5d}: 0/{len(row)} truncated")
    report["truncation"] = {str(k): v for k, v in trunc.items()}

    print("\n-- 3. latency and memory (the fix_prefiltered_ambient_hist shape, production request incl. JSON-schema `format`, 4 warm runs) --")
    from friday.decision import decision_json_schema

    s, p = shapes["fix_prefiltered_ambient_hist"]
    schema = decision_json_schema([sp.name for sp in real_specs()], allow_ask=False)
    perf: dict[int, dict] = {}
    for ctx in CANDIDATE_CTX:
        first, first_ms = await _ask(s, p, ctx, fmt=schema)  # window change => model reload; record it
        load_ms = round((first.raw.get("load_duration") or 0) / 1e6)
        runs = []
        for _ in range(4):
            r, ms = await _ask(s, p, ctx, fmt=schema)
            runs.append({"total_ms": round(ms), "prompt_eval_ms": round((r.raw.get("prompt_eval_duration") or 0) / 1e6),
                         "eval_ms": round((r.raw.get("eval_duration") or 0) / 1e6), "eval_count": r.raw.get("eval_count"),
                         "prompt_tokens": r.raw.get("prompt_eval_count")})
        ps = await _ps()
        perf[ctx] = {
            "reload_load_ms": load_ms, "first_call_ms": round(first_ms),
            "median_total_ms": round(statistics.median(x["total_ms"] for x in runs)),
            "median_prompt_eval_ms": round(statistics.median(x["prompt_eval_ms"] for x in runs)),
            "median_eval_ms": round(statistics.median(x["eval_ms"] for x in runs)),
            "runs": runs, "ps": ps, "vram_used_mib": _vram_used_mib(),
        }
        d = perf[ctx]
        print(f"  num_ctx={ctx:5d}: reload {load_ms:5d} ms | median total {d['median_total_ms']:5d} ms "
              f"(prompt-eval {d['median_prompt_eval_ms']} ms, generate {d['median_eval_ms']} ms) | "
              f"Ollama says context_length={ps['context_length']}, model {ps['size_mib']} MiB, in VRAM {ps['size_vram_mib']} MiB | GPU used {d['vram_used_mib']} MiB")
    report["perf"] = {str(k): v for k, v in perf.items()}
    report["verified_reaching_ollama"] = {str(k): v["ps"]["context_length"] for k, v in perf.items()}

    out = ROOT / "data" / "context_budget_live_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport written: {out}")
    return 0


async def main() -> int:
    if "--live" in sys.argv:
        return await live()
    t0 = time.perf_counter()
    try:
        with store.use_temp_db():
            await section_a()
            section_b()
            await section_c()
            section_d()
    finally:
        reset()
    return H.finish("CONTEXT BUDGET SCORECARD", time.perf_counter() - t0, min_assertions=60, min_scenarios=6)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
