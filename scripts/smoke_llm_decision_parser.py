"""Phase 19.0 — deterministic scorecard for reliable local-LLM decision generation.

Entirely deterministic: every model reply below is either a corpus entry
(scripts/llm_decision_corpus.py — verbatim-observed qwen2.5:3b replies plus
clearly-labelled constructed ones) or a scripted reply injected through the
existing `Orchestrator(llm_provider=...)` / `llm.get_provider` seam. No Ollama.
Nothing destructive can run: every consequential tool here is a `test.dp_*`
skill whose body only flips a flag, and every confirmation is declined.

Sections
  A  the corpus through parse -> validate (`friday.decision`)
  B  parse success is not decision success
  C  argument validation against the existing skill registry
  D  the compact repair prompt (short, schema-focused, no leaked context/secrets)
  E  failure injection through the real Orchestrator: exactly-one bounded repair
  F  safety: malformed + consequential, permission denial, declined confirmation
  G  false-success protection ("done" is a suggestion, evidence decides)
  H  experience/memory hygiene (no fake episodes/history)
  I  cancellation (never a late execution)
  J  discovery reliability (malformed output never eats the budget)
  K  native structured-output plumbing (llm.py / config)
  L  direct routing untouched (USER -> BRAIN -> SKILL -> EXECUTOR)

Scorecard classes: PASS (valid accepted) / INVALID (correctly rejected) /
RECOVERED (invalid or non-bare input made valid safely) / BLOCKED (execution
correctly prevented) / FAIL (an assertion did not hold).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from friday import decision as dec  # noqa: E402
from friday import llm, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import context_memory, episodes, evaluator  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec, _parse_subgoals  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402
import llm_decision_corpus as corpus  # noqa: E402

# -- scaffolding ----------------------------------------------------------------

RESULTS: list[tuple[str, str]] = []  # (class, label)
CLASSES = ("PASS", "INVALID", "RECOVERED", "BLOCKED")


def check(label: str, cond: bool, kind: str = "PASS", detail: str = "") -> bool:
    """Record one assertion. On success it is tallied under `kind`
    (PASS/INVALID/RECOVERED/BLOCKED); on failure under FAIL."""
    RESULTS.append((kind if cond else "FAIL", label))
    suffix = f" -- {detail}" if detail and not cond else ""
    print(f"  {'OK  ' if cond else 'MISS'} [{kind if cond else 'FAIL'}] {label}{suffix}")
    return bool(cond)


class ScriptedPlanner(LlmProvider):
    """Deterministic replies, recorded requests. `delays[i]` (seconds) makes the
    i-th call slow, for cancellation tests."""

    name = "scripted"

    def __init__(self, replies: list[str], delays: dict[int, float] | None = None) -> None:
        self.replies = list(replies)
        self.delays = delays or {}
        self.calls = 0
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        i = self.calls
        self.calls += 1
        self.requests.append(request)
        if i in self.delays:
            await asyncio.sleep(self.delays[i])
        return LlmResponse(text=self.replies[min(i, len(self.replies) - 1)], model="scripted", provider=self.name)


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


def done(summary: str = "ok") -> str:
    return json.dumps({"action": "done", "summary": summary})


FLAGS: dict[str, int] = {"destructive": 0, "external": 0, "read": 0, "confirm_prompts": 0}


def _register_skills() -> None:
    @skill(name="test.dp_read", tier="L0", description="harmless read-only probe (decision-parser smoke)")
    def _read() -> SkillResult:
        FLAGS["read"] += 1
        return SkillResult(speech="dp_read reports the demo service is running.")

    @skill(name="test.dp_destructive", tier="L2", description="stand-in destructive action; only flips a flag")
    def _destructive() -> SkillResult:
        FLAGS["destructive"] += 1
        return SkillResult(speech="destroyed")

    # Phase 20.0: a non-L0 fixture declares what KIND of action it stands for
    # (friday.intent) — this one is an external send, and the goal it is driven by
    # is "send the thing externally".
    @skill(name="test.dp_external", tier="L3", action="communicate",
           description="stand-in external/irreversible action; only flips a flag")
    def _external() -> SkillResult:
        FLAGS["external"] += 1
        return SkillResult(speech="sent externally")


async def _decline(skill_, args, preview: str) -> bool:
    FLAGS["confirm_prompts"] += 1
    return False


def reset() -> None:
    INTEL.reset()
    context_memory.CONTEXT.reset()
    EXECUTOR.set_confirm_handler(_decline)
    SESSION.pending = None
    for k in FLAGS:
        FLAGS[k] = 0
    CFG.planner.decision_repair_attempts = 1
    CFG.planner.structured_output = False


# A registry-free tool world for the pure-orchestrator scenarios.
MOCK_CALLS: list[tuple[str, dict]] = []
MOCK_SPECS = [
    ToolSpec(name="mock.echo", description="Echo some text"),
    ToolSpec(name="mock.fail", description="Always fails"),
]


async def mock_runner(tool: str, args: dict, actor: str) -> SkillResult:
    MOCK_CALLS.append((tool, dict(args)))
    if tool == "mock.echo":
        return SkillResult(speech=f"echoed {args.get('text', '')}")
    if tool == "mock.fail":
        return SkillResult(speech="that didn't work", ok=False)
    raise KeyError(tool)


def make_orch(planner: LlmProvider, *, max_steps: int = 6) -> Orchestrator:
    MOCK_CALLS.clear()
    return Orchestrator(
        tools=[s.name for s in MOCK_SPECS], runner=mock_runner, actor="test",
        llm_provider=planner, tool_specs=MOCK_SPECS, max_steps=max_steps,
    )


def offered_l0() -> list[str]:
    return [s.name for s in REGISTRY.all() if s.tier == "L0"]


# -- the sections ---------------------------------------------------------------


def section_a_corpus() -> None:
    print("\n--- A: corpus through parse -> validate ---\n")
    catalog = dec.ToolCatalog(offered=offered_l0())
    cats = set()
    for c in corpus.CASES:
        cats.add(c.category)
        result, ex = dec.parse_and_validate(c.text, catalog=catalog, allow_ask=c.allow_ask)
        kind, _, arg = c.expect.partition(":")
        label = f"[{c.source}] {c.category} / {c.id}"
        if kind == "valid":
            ok = isinstance(result, dec.Decision) and result.kind.value == arg and not result.recovered and not ex.normalized
            check(label, ok, "PASS", f"got {result}")
        elif kind == "normalized":
            ok = isinstance(result, dec.Decision) and result.kind.value == arg and ex.normalized
            check(label, ok, "RECOVERED", f"got {result} normalized={ex.normalized}")
        elif kind == "recovered":
            ok = isinstance(result, dec.Decision) and result.recovered == arg
            check(label, ok, "RECOVERED", f"got {result}")
        else:
            ok = isinstance(result, dec.InvalidDecision) and result.reason.value == arg
            check(label, ok, "INVALID", f"got {result}")
    required = {f"{i}-" for i in range(1, 19)}
    covered = {c.split("-")[0] + "-" for c in cats if c[0].isdigit()}
    check("all 18 required corpus categories are represented", required <= covered, "PASS", f"missing {sorted(required - covered)}")
    check(
        f"corpus provenance is explicit ({corpus.OBSERVED_COUNT} observed verbatim, "
        f"{corpus.CONSTRUCTED_COUNT} constructed)",
        corpus.OBSERVED_COUNT >= 12 and corpus.CONSTRUCTED_COUNT > 0,
    )


def section_b_parse_vs_validate() -> None:
    print("\n--- B: parse success is not decision success ---\n")
    catalog = dec.ToolCatalog(offered=offered_l0())
    text = '{"action":"call","tool":"files.read","args":{}}'
    ex = dec.extract_json_object(text)
    check("well-formed JSON parses (extraction succeeds)", ex.obj is not None and ex.failure is None)
    v = dec.validate_decision(ex.obj, catalog=catalog)
    check("...but the decision is invalid (missing required 'path')",
          isinstance(v, dec.InvalidDecision) and v.reason is dec.InvalidReason.INVALID_ARGS, "INVALID")
    ex = dec.extract_json_object('{"foo":"bar"}')
    check("a parseable non-decision object extracts fine", ex.obj == {"foo": "bar"})
    v = dec.validate_decision(ex.obj, catalog=catalog)
    check("...and is rejected as missing_action, never guessed",
          isinstance(v, dec.InvalidDecision) and v.reason is dec.InvalidReason.MISSING_ACTION, "INVALID")
    ex = dec.extract_json_object("I think you should open VS Code.")
    check("plain English yields NO structured object (no fabricated action)", ex.obj is None and ex.failure is not None, "INVALID")
    check("extraction never raises on odd input types",
          all(dec.extract_json_object(x) is not None for x in (None, "", "{", "}", "[[[", "\x00", "{" * 500, "😀" * 50)))
    # a registered-but-not-offered tool is a POLICY case, not a format failure
    v = dec.validate_decision(
        {"action": "call", "tool": "browser.open", "args": {}}, catalog=dec.ToolCatalog(offered=offered_l0()),
    )
    check("a real tool merely not offered this run stays a valid decision (policy refuses it later, no repair)",
          isinstance(v, dec.Decision) and v.tool == "browser.open", "PASS")


def section_c_args() -> None:
    print("\n--- C: argument validation reuses the existing registry metadata ---\n")
    catalog = dec.ToolCatalog(offered=offered_l0())

    def v(obj):
        return dec.validate_decision(obj, catalog=catalog)

    d = v({"action": "call", "tool": "files.read", "args": {"path": "a.txt", "max_chars": "500"}})
    check("numeric string for an int param is normalized to the int", isinstance(d, dec.Decision) and d.args["max_chars"] == 500, "RECOVERED")
    d = v({"action": "call", "tool": "files.read", "args": {"path": "a.txt", "max_chars": None}})
    check("null for an optional arg is dropped", isinstance(d, dec.Decision) and "max_chars" not in d.args, "RECOVERED")
    d = v({"action": "call", "tool": "files.read", "args": {"path": "a.txt", "max_chars": True}})
    check("a bool is not accepted as an int", isinstance(d, dec.InvalidDecision) and d.reason is dec.InvalidReason.INVALID_ARGS, "INVALID")
    d = v({"action": "call", "tool": "files.read", "args": {"path": "a.txt", "bogus": 1}})
    check("an invented argument name is rejected (names the valid ones)",
          isinstance(d, dec.InvalidDecision) and "valid arguments" in d.detail and "path" in d.detail, "INVALID")
    d = v({"action": "call", "tool": "files.read", "args": {"path": 42}})
    check("a number for a str param is normalized to text", isinstance(d, dec.Decision) and d.args["path"] == "42", "RECOVERED")
    d = v({"action": "call", "tool": "system.time", "args": {"anything": 1}})
    check("a no-arg tool rejects any invented argument", isinstance(d, dec.InvalidDecision), "INVALID")
    d = v({"action": "call", "tool": "files.read", "args": {"path": "a.txt"}, "reason": 5, "expected_outcome": ["x"]})
    check("optional free-text fields with odd types never break a valid call",
          isinstance(d, dec.Decision) and d.expected_outcome == "" and d.reason == "5")
    d = v({"action": "call", "tool": "  system.time  "})
    check("tool names are trimmed but otherwise matched exactly", isinstance(d, dec.Decision) and d.tool == "system.time")
    d = v({"action": "call", "tool": "SYSTEM.TIME"})
    check("tool names are case-sensitive (no fuzzy matching of tools)",
          isinstance(d, dec.InvalidDecision) and d.reason is dec.InvalidReason.UNKNOWN_TOOL, "INVALID")
    d = v({"action": "done", "summary": {"nested": "x"}})
    check("a non-text summary on done never crashes", isinstance(d, dec.Decision) and d.summary == "")
    d = v({"action": "ask", "question": "  "})
    d2 = dec.validate_decision({"action": "ask", "question": "  "}, catalog=catalog, allow_ask=True)
    check("ask needs a real question (and is unknown outside discovery)",
          isinstance(d, dec.InvalidDecision) and isinstance(d2, dec.InvalidDecision), "INVALID")


def section_d_repair_prompt() -> None:
    print("\n--- D: the compact repair prompt ---\n")
    names = offered_l0()
    inv = dec.InvalidDecision(dec.InvalidReason.UNKNOWN_TOOL, "'file.search' is not an available tool",
                              '{"action": "call", "tool": "file.search", "args": {"password": "hunter2"}}', "file.search")
    system, prompt = dec.build_repair_request(
        "find the README", inv, tool_names=names, tool_signature=lambda n: "query (str, required)",
        recent_steps=["1. called a", "2. called b", "3. called c", "4. called d"], allow_ask=False, call_tool="file.search",
    )
    check("repair prompt states the reason", "invalid because" in prompt and "unknown_tool" in prompt)
    check("repair prompt demands ONLY valid JSON", "ONLY" in system and "ONLY a valid decision" in prompt)
    check("repair prompt shows the schema", '"action": "call"' in system and '"action": "done"' in system)
    check("repair prompt is short (system+prompt < 3000 chars)", len(system) + len(prompt) < 3000, detail=str(len(system) + len(prompt)))
    check("repair prompt carries at most the last 3 steps", "4. called d" in prompt and "1. called a" not in prompt)
    check("repair prompt scrubs secrets from the echoed reply", "hunter2" not in prompt and "[redacted]" in prompt)
    check("unknown_tool repair suggests the closest valid names", "Closest valid tool names" in prompt and "files.search" in prompt)
    check("'ask' is only advertised in discovery repairs",
          "ask" not in system.lower().replace("task", "") and
          '"action": "ask"' in dec.build_repair_request("g", inv, tool_names=names, allow_ask=True)[0])
    check("repair prompt never mentions desktop context or retrieved experience",
          "desktop context" not in prompt.lower() and "experience" not in prompt.lower())


async def section_e_failure_injection() -> None:
    print("\n--- E: failure injection through the real Orchestrator (bounded repair) ---\n")

    async def run(replies, *, attempts=1, max_steps=6):
        reset()
        CFG.planner.decision_repair_attempts = attempts
        planner = ScriptedPlanner(replies)
        orch = make_orch(planner, max_steps=max_steps)
        events: list[dict] = []

        async def _h(ev):
            events.append(ev.data)

        BUS.subscribe("orchestrator.decision", _h)
        try:
            result = await orch.run_goal("say hi")
        finally:
            BUS.unsubscribe("orchestrator.decision", _h)
        return result, planner, events

    # 1. malformed JSON -> one repair -> valid
    r, p, ev = await run(['{"action":"call","tool":"mock.echo","args":{"text":"hi"', call("mock.echo", {"text": "hi"}), done("said hi")])
    check("malformed JSON -> exactly one repair -> valid decision executes -> completes",
          r.ok and r.stopped == "completed" and len(MOCK_CALLS) == 1 and p.calls == 3, "RECOVERED", f"calls={p.calls} {r.stopped}")
    check("the repair attempt is observable on the bus (repaired + repair_succeeded)",
          ev[0]["repaired"] and ev[0]["repair_succeeded"] and ev[0]["model_calls"] == 2, "RECOVERED")
    # 2. valid but wrong shape -> repair
    r, p, ev = await run(['{"answer":"hello"}', call("mock.echo", {"text": "x"}), done()])
    check("valid-but-wrong-shape JSON -> repaired", r.ok and len(MOCK_CALLS) == 1, "RECOVERED")
    # 3. unknown tool -> one repair -> still unknown -> never executed
    r, p, ev = await run([call("something_that_does_not_exist")])
    check("unknown tool: exactly one repair (2 model calls), never executed",
          p.calls == 2 and not MOCK_CALLS and not r.ok, "BLOCKED", f"calls={p.calls}")
    check("...the stop reason stays tool_not_allowed and carries the structured reason",
          r.stopped == "tool_not_allowed" and r.invalid_decision is not None
          and r.invalid_decision.reason is dec.InvalidReason.UNKNOWN_TOOL, "BLOCKED")
    # 4. unknown tool -> repaired to a real tool
    r, p, ev = await run([call("mock.ecko", {"text": "x"}), call("mock.echo", {"text": "x"}), done()])
    check("unknown tool repaired to a real one executes only the real one",
          MOCK_CALLS == [("mock.echo", {"text": "x"})] and r.ok, "RECOVERED")
    # 5. missing action, repeated -> bounded
    r, p, ev = await run(['{"tool":"mock.echo"}'])
    check("missing action repeated: exactly 2 model calls then a truthful stop, nothing executed",
          p.calls == 2 and not MOCK_CALLS and r.stopped == "planning_failed" and not r.ok, "INVALID")
    check("...with the structured reason preserved",
          r.invalid_decision is not None and r.invalid_decision.reason is dec.InvalidReason.MISSING_ACTION, "INVALID")
    check("...and an honest summary (nothing run)", "nothing was run" in r.summary and "missing_action" in r.summary, "INVALID")
    # 6. repeated invalid response never loops (huge step budget)
    r, p, ev = await run(["not json at all, sorry"], max_steps=50)
    check("garbage forever with max_steps=50 still stops after exactly 2 model calls", p.calls == 2 and not MOCK_CALLS, "INVALID")
    # 7. repair budget is configurable and hard-capped
    r, p, ev = await run(["garbage"], attempts=0)
    check("decision_repair_attempts=0 -> one call, immediate honest stop", p.calls == 1 and r.stopped == "planning_failed", "INVALID")
    r, p, ev = await run(["garbage"], attempts=2)
    check("decision_repair_attempts=2 -> at most 3 calls", p.calls == 3, "INVALID")
    r, p, ev = await run(["garbage"], attempts=99)
    check("an absurd repair setting is clamped (never more than 3 calls)", p.calls == 3, "INVALID")
    # 8. wrong-shape "action is the tool name" (dominant real failure) recovers with NO extra model call
    r, p, ev = await run([json.dumps({"action": "mock.echo", "args": {"text": "hi"}}), done("ok")])
    check("action-is-tool-name recovers deterministically with zero repair calls",
          r.ok and p.calls == 2 and len(MOCK_CALLS) == 1 and not ev[0]["repaired"] and ev[0]["recovered"] == "action_was_tool_name",
          "RECOVERED", f"calls={p.calls}")
    # 9. prose + JSON, fenced JSON: no repair needed
    r, p, ev = await run(["Sure:\n```json\n" + call("mock.echo", {"text": "hi"}) + "\n```", done()])
    check("fenced + prose-wrapped JSON executes with zero repair calls", r.ok and p.calls == 2 and not ev[0]["repaired"], "RECOVERED")
    # 10. a TOOL failure is not an LLM-format failure: no repair
    r, p, ev = await run([call("mock.fail"), done("gave up")], )
    check("a failed tool call never triggers structured-output repair",
          all(not e["repaired"] for e in ev), "PASS", f"{[e['repaired'] for e in ev]}")
    # 11. plain-English answer is never turned into an action
    r, p, ev = await run(["I think you should open VS Code."])
    check("plain English is never converted into an action", not MOCK_CALLS and r.stopped == "planning_failed", "INVALID")
    # 12. invalid decision creates no observation
    r, p, ev = await run(["garbage"])
    check("an invalid decision never becomes an Observation / step", r.observations == [], "INVALID")

    # 13. a repair may not conjure a completion (found live: prose -> repair -> "done", nothing run)
    r, p, ev = await run(["Let's start by inspecting the project. Here are the steps:", done("Inspection steps initiated")])
    check("a REPAIR reply of 'done' with nothing observed is rejected (unsupported_done), nothing executed",
          r.stopped == "planning_failed" and not MOCK_CALLS and p.calls == 2
          and r.invalid_decision is not None and r.invalid_decision.reason is dec.InvalidReason.UNSUPPORTED_DONE, "BLOCKED",
          f"{r.stopped} calls={p.calls} {r.invalid_decision}")
    r, p, ev = await run([call("mock.echo", {"text": "a"}), "some prose, no json", done("checked it")])
    check("...but a repair 'done' AFTER real evidence is accepted (evidence exists)",
          r.ok and r.stopped == "completed" and len(MOCK_CALLS) == 1, "RECOVERED", f"{r.stopped}")
    r, p, ev = await run([done("nothing to do")])
    check("a FIRST-attempt 'done' is not rejected at the decision layer (the evaluator decides what it's worth)",
          r.ok and r.stopped == "completed" and p.calls == 1, "PASS")

    # subgoal decomposition parser uses the same scanner
    check("_parse_subgoals: fenced JSON", _parse_subgoals('```json\n{"subgoals":[{"description":"a"}]}\n```') == [{"description": "a"}], "RECOVERED")
    check("_parse_subgoals: prose + trailing text",
          _parse_subgoals('Here: {"subgoals":[{"description":"a"}]} hope that helps {x}') == [{"description": "a"}], "RECOVERED")
    check("_parse_subgoals: garbage -> [] (never raises)", _parse_subgoals("nope") == [] and _parse_subgoals("") == [], "INVALID")


async def section_f_safety() -> None:
    print("\n--- F: safety (malformed + consequential, permission denial, declined confirmation) ---\n")

    # 1. malformed model output + a consequential tool available => NO ACTION EXECUTED
    reset()
    planner = ScriptedPlanner(["I will now run test.dp_destructive to clean everything up!", "Executing test.dp_destructive."])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "remove the old files now"}, actor="text")
    check("malformed reply + destructive tool available: the destructive tool NEVER ran",
          FLAGS["destructive"] == 0 and FLAGS["confirm_prompts"] == 0, "BLOCKED")
    check("...and no step was recorded", r.data.get("steps") == [] and not r.ok, "BLOCKED")

    # 2. recovered action-as-tool naming an L2 tool: still goes through confirmation, declined => not executed
    reset()
    planner = ScriptedPlanner([json.dumps({"action": "test.dp_destructive", "args": {}}), done("cleaned")])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "remove the old files now"}, actor="text")
    check("a recovered L2 decision still hits the confirmation gate (prompted exactly once)", FLAGS["confirm_prompts"] == 1, "BLOCKED")
    check("...declined => the destructive tool did not run", FLAGS["destructive"] == 0, "BLOCKED")
    check("...and the run stops instead of being replanned or repaired around the decline",
          planner.calls == 1 and r.data["steps"][0]["error"] == "confirmation_declined", "BLOCKED", f"calls={planner.calls}")

    # 3. invalid reply -> repair -> valid call to an L3 tool, UNATTENDED actor => permission denial, no bypass
    reset()
    planner = ScriptedPlanner(["nonsense", call("test.dp_external"), done("sent")])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "send the thing externally"}, actor="scheduler")
    check("invalid reply + repaired L3 call from an unattended actor: permission denial, tool never ran",
          FLAGS["external"] == 0 and r.data["steps"] and r.data["steps"][0]["error"] == "PermissionError_", "BLOCKED", f"{r.data}")
    check("...repair did not open a way around the denial (exactly the 2 model calls; no third)", planner.calls == 2, "BLOCKED", f"calls={planner.calls}")
    check("...and the run is not ok", not r.ok, "BLOCKED")

    # 4. a real declined confirmation from a valid decision is never retried
    reset()
    planner = ScriptedPlanner([call("test.dp_destructive"), done("done")])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "remove the old files"}, actor="text")
    check("declined confirmation: one planner call, one prompt, no repair, nothing executed",
          planner.calls == 1 and FLAGS["confirm_prompts"] == 1 and FLAGS["destructive"] == 0, "BLOCKED")

    # 5. an L1+ tool requested in the L0-only discovery pass: a POLICY stop, not repaired
    reset()
    planner = ScriptedPlanner([call("apps.open", {"app": "chrome"})])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "why isn't chrome opening"}, actor="text")
    check("disallowed-tier tool in discovery: tool_not_allowed immediately, ONE model call (never repaired)",
          r.data.get("stopped") == "tool_not_allowed" and planner.calls == 1, "BLOCKED", f"calls={planner.calls} {r.data.get('stopped')}")

    # 6. an invalid-args call to a registry-backed tool is never executed
    reset()
    planner = ScriptedPlanner([call("test.dp_read", {"bogus": 1})])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "check dp_read"}, actor="text")
    check("invented argument name on a registered tool: never executed", FLAGS["read"] == 0 and not r.ok, "BLOCKED")
    check("...structured reason surfaces in plan.run's data",
          (r.data.get("invalid_decision") or {}).get("reason") == "invalid_args", "BLOCKED", f"{r.data.get('invalid_decision')}")

    # 7. never dynamically creates a skill from model output
    reset()
    before = len(REGISTRY)
    planner = ScriptedPlanner([call("brand.new_skill", {"code": "import os; os.remove('x')"})])
    with scripted_provider(planner):
        await EXECUTOR.run("plan.run", {"goal": "make a new skill"}, actor="text")
    check("an unknown tool never registers/creates a skill", len(REGISTRY) == before and REGISTRY.get("brand.new_skill") is None, "BLOCKED")

    # 8. positive control: a valid L0 call still runs
    reset()
    planner = ScriptedPlanner([call("test.dp_read"), done("service is running")])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "check dp_read service"}, actor="text")
    check("positive control: a valid registry-backed L0 call executes and completes",
          FLAGS["read"] == 1 and r.ok and r.data["stopped"] == "completed", "PASS")


async def section_g_false_success() -> None:
    print("\n--- G: 'done' is a suggestion; evidence decides ---\n")

    # evaluator unit
    from friday.orchestrator import OrchestratorResult

    ev = evaluator.evaluate_goal(OrchestratorResult("g", [], True, "done", "completed"))
    check("evaluator: done with ZERO observations is not goal_complete", not ev.goal_complete and ev.verdict is evaluator.Verdict.UNCERTAIN)
    ok_obs = Observation(PlanStep("x"), True, "did it")
    ev = evaluator.evaluate_goal(OrchestratorResult("g", [ok_obs], True, "done", "completed"))
    check("evaluator: done after a real successful step IS goal_complete (positive control)", ev.goal_complete and ev.verdict is evaluator.Verdict.SUCCESS)

    # main loop: done after nothing but failures is not a completion
    reset()
    planner = ScriptedPlanner([call("mock.fail"), done("All good!")])
    orch = make_orch(planner)
    r = await orch.run_goal("do the thing", max_replans=2)
    check("main loop: 'done' after only failed steps is NOT a completion (ok=False, stopped=failure)",
          not r.ok and r.stopped == "failure" and "every step I tried failed" in r.summary, "BLOCKED", f"{r.stopped}")
    reset()
    planner = ScriptedPlanner([call("mock.fail"), call("mock.echo", {"text": "b"}), done("recovered")])
    orch = make_orch(planner)
    r = await orch.run_goal("do the thing", max_replans=2)
    check("main loop: fail-then-recover-then-done still completes (existing recovery rule intact)", r.ok and r.stopped == "completed")

    # discovery-only goal + evidence-free done => PARTIAL/unverified, never SUCCEEDED
    reset()
    planner = ScriptedPlanner([done("Everything is fine, nothing to report.")])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "why isn't the printer working"}, actor="text")
    g = goals_mod.get(r.data["goal_id"])
    check("discovery: evidence-free 'done' is NOT recorded as succeeded",
          r.data.get("status") != "succeeded" and g.status is not goals_mod.GoalStatus.SUCCEEDED, "BLOCKED", f"{r.data.get('status')}")
    check("discovery: verdict is not 'success'", g.contract.final_verdict != evaluator.Verdict.SUCCESS.value, "BLOCKED", g.contract.final_verdict)
    check("discovery: the user is told it's unverified", "unverified" in r.speech.lower() or "didn't gather any evidence" in r.speech.lower(), "BLOCKED", r.speech)
    n_before = len(episodes.recent(500))

    # discovery with real evidence + done => succeeds (positive control)
    reset()
    planner = ScriptedPlanner([call("test.dp_read"), done("The demo service is running.")])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "why isn't the demo service working"}, actor="text")
    check("discovery: done backed by a real observation is still SUCCEEDED (positive control)",
          r.data.get("status") == "succeeded", "PASS", f"{r.data.get('status')} {r.data.get('stopped')}")
    check("...and records exactly one real (successful) episode", len(episodes.recent(500)) == n_before + 1, "PASS")

    # main mode, direct action, evidence-free done => goal PARTIAL, never SUCCEEDED
    reset()
    planner = ScriptedPlanner([done("Opened it.")])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "open the quarterly numbers for me"}, actor="text")
    g = goals_mod.get(r.data["goal_id"])
    check("main loop: evidence-free 'done' -> goal PARTIAL, never SUCCEEDED",
          g.status is goals_mod.GoalStatus.PARTIAL, "BLOCKED", f"{g.status}")
    check("...and INTEL reports it as partial, not succeeded", INTEL.state.goal_status == "partial", "BLOCKED", INTEL.state.goal_status)
    check("...the user is told it's unverified, and the result carries verified=False",
          r.data.get("verified") is False and "unverified" in r.speech.lower(), "BLOCKED", f"{r.data.get('verified')} {r.speech}")
    reset()
    planner = ScriptedPlanner([call("test.dp_read"), done("Service is running.")])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "open the quarterly numbers for me"}, actor="text")
    check("positive control: an evidence-backed completion is verified=True with no caveat in the speech",
          r.data.get("verified") is True and "unverified" not in r.speech.lower(), "PASS", f"{r.data.get('verified')} {r.speech}")


async def section_h_experience() -> None:
    print("\n--- H: malformed output creates no fake experience ---\n")
    reset()
    ep_before = len(episodes.recent(500))
    actions_before = len(INTEL.state.recent_actions)
    ctx_before = len(list(getattr(context_memory.CONTEXT, "_entries", []) or []))
    planner = ScriptedPlanner(["garbage", "still garbage"])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "reorganize the quarterly folders somehow"}, actor="text")
    g = goals_mod.get(r.data["goal_id"])
    check("a planner-format failure with no steps records NO episode (no fake experience)",
          len(episodes.recent(500)) == ep_before, "INVALID", f"{len(episodes.recent(500))} vs {ep_before}")
    check("...the Goal row is truthfully FAILED (not succeeded)", g.status is goals_mod.GoalStatus.FAILED, "INVALID", f"{g.status}")
    check("...no action history is fabricated", len(INTEL.state.recent_actions) - actions_before <= 1, "INVALID")  # +1: plan.run itself
    check("...no recent-entity memory is fabricated",
          len(list(getattr(context_memory.CONTEXT, "_entries", []) or [])) == ctx_before, "INVALID")
    check("...and the structured reason is reported in plan.run's data",
          (r.data.get("invalid_decision") or {}).get("reason") == "malformed_json", "INVALID", f"{r.data.get('invalid_decision')}")

    # partial progress + later malformed: real progress still reported and recorded honestly
    reset()
    planner = ScriptedPlanner([call("test.dp_read"), "garbage", "garbage"])
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": "reorganize the quarterly folders somehow"}, actor="text")
    check("real progress before a later invalid decision is still reported (the Phase 15.0 behaviour)",
          len(r.data["steps"]) == 1 and r.data["steps"][0]["ok"] and "partway" in r.speech, "PASS", r.speech[:80])


async def section_i_cancellation() -> None:
    print("\n--- I: cancellation never executes a late model response ---\n")

    # (a) cancel while waiting for the planner
    reset()
    planner = ScriptedPlanner([call("mock.echo", {"text": "late"})], delays={0: 5.0})
    orch = make_orch(planner)
    flag = {"cancel": False}

    async def _flip():
        await asyncio.sleep(0.3)
        flag["cancel"] = True

    t0 = time.perf_counter()
    asyncio.get_running_loop().create_task(_flip())
    r = await orch.run_goal("say hi", cancel_check=lambda: flag["cancel"])
    el = time.perf_counter() - t0
    check("cancel while waiting for the planner returns promptly (<2s, not the 5s model time)", r.stopped == "cancelled" and el < 2.0, "BLOCKED", f"{el:.2f}s {r.stopped}")
    check("...and the (would-be) late decision never executed", MOCK_CALLS == [], "BLOCKED")

    # (b) cancel during the repair attempt
    reset()
    planner = ScriptedPlanner(["garbage", call("mock.echo", {"text": "late"})], delays={1: 5.0})
    orch = make_orch(planner)
    flag = {"cancel": False}

    async def _flip2():
        await asyncio.sleep(0.4)
        flag["cancel"] = True

    asyncio.get_running_loop().create_task(_flip2())
    t0 = time.perf_counter()
    r = await orch.run_goal("say hi", cancel_check=lambda: flag["cancel"])
    check("cancel during the repair attempt: stops immediately, repaired decision never executed",
          r.stopped == "cancelled" and MOCK_CALLS == [] and time.perf_counter() - t0 < 2.0, "BLOCKED")

    # (c) a reply that arrives after cancellation is discarded
    reset()
    planner = ScriptedPlanner([call("mock.echo", {"text": "late"})])
    orch = make_orch(planner)
    polls = {"n": 0}

    def _cancel_after_first_poll() -> bool:
        polls["n"] += 1
        return polls["n"] > 1  # the loop-top check passes; every later poll says cancelled

    r = await orch.run_goal("say hi", cancel_check=_cancel_after_first_poll)
    check("a valid reply that lands after cancellation is discarded, not executed",
          r.stopped == "cancelled" and MOCK_CALLS == [], "BLOCKED", f"{r.stopped} {MOCK_CALLS}")

    # (d) baseline: no cancel => normal
    reset()
    planner = ScriptedPlanner([call("mock.echo", {"text": "ok"}), done()])
    orch = make_orch(planner)
    r = await orch.run_goal("say hi", cancel_check=lambda: False)
    check("positive control: cancel_check that never fires changes nothing", r.ok and len(MOCK_CALLS) == 1)


async def section_j_discovery() -> None:
    print("\n--- J: discovery reliability ---\n")
    reset()
    # max_steps=2: an invalid reply must NOT eat a step of the investigation budget.
    planner = ScriptedPlanner(["garbage", call("mock.echo", {"text": "a"}), call("mock.echo", {"text": "b"}), done()])
    orch = make_orch(planner, max_steps=2)
    r = await orch.run_goal("investigate the demo", discovery_mode=True)
    check("malformed reply repaired inside the same step: both real steps still fit in max_steps=2",
          len([o for o in r.observations if o.ok]) == 2 and r.stopped == "step_limit", "RECOVERED", f"obs={len(r.observations)} {r.stopped}")
    check("a malformed reply is never counted as a discovery action/observation",
          all(o.step.tool == "mock.echo" for o in r.observations))
    reset()
    planner = ScriptedPlanner(["garbage"])
    orch = make_orch(planner, max_steps=6)
    r = await orch.run_goal("investigate the demo", discovery_mode=True)
    check("discovery: invalid + failed repair -> clean bounded failure (no observations, no crash)",
          r.stopped == "planning_failed" and r.observations == [] and planner.calls == 2, "INVALID")
    reset()
    planner = ScriptedPlanner([json.dumps({"action": "ask", "question": "Which service?"})])
    orch = make_orch(planner)
    r = await orch.run_goal("investigate the demo", discovery_mode=True)
    check("discovery: a valid 'ask' still stops for clarification", r.stopped == "clarification_required" and r.summary == "Which service?")
    reset()
    planner = ScriptedPlanner([json.dumps({"action": "ask", "question": "Which service?"}), call("mock.echo", {"text": "x"}), done()])
    orch = make_orch(planner)
    r = await orch.run_goal("say hi", discovery_mode=False)
    check("outside discovery 'ask' is not a valid action: it is repaired, not honoured",
          r.ok and len(MOCK_CALLS) == 1 and planner.calls == 3, "RECOVERED", f"calls={planner.calls}")
    # discovery prompt carries the ask action; the byte-identical non-discovery prompt does not
    reset()
    planner = ScriptedPlanner([done()])
    orch = make_orch(planner)
    await orch.run_goal("say hi", discovery_mode=False)
    check("the non-discovery planner prompt is unchanged (no 'ask' action advertised)", '"action": "ask"' not in planner.requests[0].messages[0].content)


async def section_k_structured_output() -> None:
    print("\n--- K: native structured-output plumbing (off by default; measured in PLAN.md) ---\n")
    reset()
    planner = ScriptedPlanner([done()])
    orch = make_orch(planner)
    await orch.run_goal("say hi")
    check("structured_output=False sends NO response_format to the provider", planner.requests[0].response_format is None)
    from friday.config import PlannerConfig

    check("the shipped default is structured_output=True (measured: PLAN.md Phase 19.0)", PlannerConfig().structured_output is True)
    CFG.planner.structured_output = True
    planner = ScriptedPlanner([call("mock.echo", {"text": "hi"}), done()])
    orch = make_orch(planner)
    await orch.run_goal("say hi")
    fmt = planner.requests[0].response_format
    check("structured_output=True sends the decision JSON Schema", isinstance(fmt, dict) and fmt.get("required") == ["action"])
    check("...whose tool enum is exactly the offered tools", fmt["properties"]["tool"]["enum"] == ["mock.echo", "mock.fail"])
    check("...and whose action enum excludes 'ask' outside discovery", fmt["properties"]["action"]["enum"] == ["call", "done"])
    check("...but includes 'ask' in discovery",
          dec.decision_json_schema(["a"], allow_ask=True)["properties"]["action"]["enum"] == ["call", "done", "ask"])
    CFG.planner.structured_output = False

    # the Ollama provider actually puts it on the wire as `format`
    import httpx

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "{}"}})

    class _P(llm.OllamaProvider):
        async def _client(self):
            return httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))

    p = _P(base_url="http://x")
    await p.complete(LlmRequest(messages=[llm.LlmMessage("user", "hi")], model="m", response_format={"type": "object"}))
    check("OllamaProvider sends response_format as Ollama's `format` field", seen["body"].get("format") == {"type": "object"})
    await p.complete(LlmRequest(messages=[llm.LlmMessage("user", "hi")], model="m"))
    check("...and sends nothing extra when it is unset", "format" not in seen["body"])

    # a server that rejects the schema is retried once WITHOUT it (never assumed to be supported)
    bodies: list[dict] = []

    def handler2(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "format" in body:
            return httpx.Response(400, json={"error": "invalid format"})
        return httpx.Response(200, json={"message": {"content": '{"action":"done"}'}})

    class _P2(llm.OllamaProvider):
        async def _client(self):
            return httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler2))

    resp = await _P2(base_url="http://x").complete(
        LlmRequest(messages=[llm.LlmMessage("user", "hi")], model="m", response_format={"type": "object"}))
    check("a 400 to a schema format is retried once without it (structured output is never a requirement)",
          len(bodies) == 2 and "format" in bodies[0] and "format" not in bodies[1] and resp.text == '{"action":"done"}', "RECOVERED")


async def section_l_direct_routing() -> None:
    print("\n--- L: direct routing untouched ---\n")
    reset()
    planner = ScriptedPlanner(["THE PLANNER MUST NOT BE CALLED FOR A DIRECT COMMAND"])
    with scripted_provider(planner):
        r = await SESSION.handle("what time is it", actor="text")
    check("a direct command (USER -> BRAIN -> SKILL -> EXECUTOR) never touches the planner/decision path",
          planner.calls == 0 and r.ok, "PASS", f"calls={planner.calls} {r.speech}")
    check("the decision module has no import-time dependency on the orchestrator/executor",
          "friday.orchestrator" not in open(dec.__file__, encoding="utf-8").read().split('"""', 2)[2].split("import json")[0])


async def main() -> None:
    db_cm = store.use_temp_db()
    db_cm.__enter__()
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    _register_skills()
    reset()

    section_a_corpus()
    section_b_parse_vs_validate()
    section_c_args()
    section_d_repair_prompt()
    await section_e_failure_injection()
    await section_f_safety()
    await section_g_false_success()
    await section_h_experience()
    await section_i_cancellation()
    await section_j_discovery()
    await section_k_structured_output()
    await section_l_direct_routing()

    counts = Counter(k for k, _ in RESULTS)
    total = len(RESULTS)
    print("\n" + "=" * 68)
    print("PHASE 19.0 DETERMINISTIC SCORECARD (parser / validator / repair / safety)")
    print("=" * 68)
    print(f"  assertions : {total}")
    print(f"  PASS       : {counts['PASS']}   (valid decisions accepted / behaviour preserved)")
    print(f"  INVALID    : {counts['INVALID']}   (bad model output correctly rejected, never executed)")
    print(f"  RECOVERED  : {counts['RECOVERED']}   (invalid or non-bare output made valid safely)")
    print(f"  BLOCKED    : {counts['BLOCKED']}   (an unsafe/late/unsupported execution correctly prevented)")
    print(f"  FAIL       : {counts['FAIL']}")
    print(f"  corpus     : {len(corpus.CASES)} entries ({corpus.OBSERVED_COUNT} observed verbatim from qwen2.5:3b, "
          f"{corpus.CONSTRUCTED_COUNT} constructed)")
    reasons = sorted({c.expect.split(":", 1)[1] for c in corpus.CASES if c.expect.startswith("invalid:")})
    fail_cats = sorted({c.category for c in corpus.CASES if c.expect.startswith("invalid:")})
    print(f"  failure categories (corpus categories with rejected replies): {len(fail_cats)}  |  "
          f"structured reasons exercised: {len(reasons)} ({', '.join(reasons)})")
    print(f"  recovery cases: {counts['RECOVERED']}  |  safety (BLOCKED) cases: {counts['BLOCKED']}")
    failed = [label for k, label in RESULTS if k == "FAIL"]
    if failed:
        print("\n  FAILED:")
        for label in failed:
            print(f"    - {label}")
    print(f"\n{'ALL OK' if not failed else 'FAILURES ABOVE'}")
    db_cm.__exit__(None, None, None)
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    asyncio.run(main())
