"""Phase 21.0 — deterministic scorecard for PAGINATED / INCREMENTAL READS.

Phase 20's ALREADY_TRIED guard blocks an identical read when nothing relevant has
changed — right for a loop, wrong for "read more": `files.read` had no cursor, so a
legitimate second page was byte-for-byte the same call as the first and got blocked.

The fix has three deliberately narrow parts, and this suite pins each one AND the
safety property that none of them is a general bypass of repeat protection:

  1. `files.read(offset=)` — a real cursor, reporting `next_offset` (registry-declared,
     so it is a tool property, not a special case in the guard). A changed offset is a
     different call and was never a repeat.
  2. `intent.is_continuation_request` + `next_page_args` — when the USER'S WORDS ask for
     more ("read more", "next page", "continue") and the guard would block an identical
     read, a pagination-capable READ whose previous call reported where the next part
     starts is advanced to that position instead. Nothing else changes.
  3. `Session._maybe_continue_read` — the same for a bare "read more" utterance after a
     direct read, dispatched through the normal EXECUTOR path.

  A  the real `files.read` cursor
  B  intent helpers (pure)
  C  the repeat guard through the real run_goal: same read / read more / next page /
     changed offset / destructive duplicate — and every way pagination must NOT bypass it
  D  Session-level "read more"
  E  static invariants

Deterministic: scripted model replies; reads go through the real (L0, read-only) executor
on a temp file; every other tool is a recording stub. No Ollama.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated

import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario

from friday import intent, store  # noqa: E402
from friday import orchestrator as orch_mod  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import NOT_EXECUTED_ERRORS, Observation, Orchestrator, PlanStep, ToolSpec, _tool_specs, find_prior_attempt  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

REGISTRY.discover()

TEXT = "".join(chr(97 + (i % 26)) for i in range(10000))  # 10 000 distinct-looking chars
SHORT = "a short file"
CALLS: list[tuple[str, dict]] = []
EVENTS: dict[str, int] = {}


def _register_fixtures() -> None:
    @skill(name="test.pg_read", tier="L0", description="paginated read stand-in (has an offset cursor)")
    def _pg_read(offset: Annotated[int, "start"] = 0) -> SkillResult:
        return SkillResult(speech="pg_read", data={"next_offset": offset + 10})

    @skill(name="test.pg_write", tier="L1", action="modify", description="a STATE-CHANGING tool that also has an offset cursor")
    def _pg_write(offset: Annotated[int, "start"] = 0) -> SkillResult:
        return SkillResult(speech="pg_write", data={"next_offset": offset + 10})


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    if tool == "files.read":
        return await EXECUTOR.run("files.read", args, actor=actor)  # the REAL L0 read
    if tool == "system.time":
        return SkillResult(speech="It's 3:45 PM.")
    if tool == "test.pg_write":
        return SkillResult(speech="pg_write ok", data={"next_offset": int(args.get("offset", 0)) + 10})
    if tool == "test.pg_read":
        return SkillResult(speech="pg_read ok", data={"next_offset": int(args.get("offset", 0)) + 10})
    return SkillResult(speech=f"{tool} ok")


FAKE_SPECS = [ToolSpec(name="files.delete", description="delete a file", tier="L2", params="path (str)")]


def real_tool_names() -> list[str]:
    return [s.name for s in REGISTRY.all() if s.name != "plan.run"]


async def _count(topic: str):
    async def handler(ev) -> None:
        EVENTS[topic] = EVENTS.get(topic, 0) + 1

    BUS.subscribe(topic, handler)
    return handler


async def drive(goal: str, replies: list[str], *, scope="auto", max_steps: int = 10):
    CALLS.clear()
    EVENTS.clear()
    specs = _tool_specs(real_tool_names()) + FAKE_SPECS
    planner = ScriptedPlanner(replies)
    orch = Orchestrator(tools=[s.name for s in specs], runner=runner, actor="test", llm_provider=planner,
                        tool_specs=specs, max_steps=max_steps)
    if scope == "auto":
        scope = intent.derive_scope(goal)
    h = await _count("orchestrator.continuation")
    try:
        res = await orch.run_goal(goal, action_scope=scope, max_replans=CFG.planner.max_replans)
    finally:
        BUS.unsubscribe("orchestrator.continuation", h)
    return res, planner


def reads() -> list[dict]:
    return [a for t, a in CALLS if t == "files.read"]


def blocked(res) -> list:
    return [o for o in res.observations if o.error == "repeated_call"]


def reset() -> None:
    CALLS.clear()
    INTEL.reset()
    SESSION.pending = None
    SESSION.last_skill, SESSION.last_args, SESSION.last_data = None, {}, {}
    CFG.planner.structured_output = False
    CFG.planner.intent_guard = True
    CFG.planner.continuation_reads = True
    CFG.planner.goal_coverage = True
    CFG.desktop_observer.enabled = False


def obs(tool: str, speech: str = "x", *, args: dict | None = None, ok: bool = True, data: dict | None = None, error: str = "") -> Observation:
    return Observation(PlanStep(tool, args or {}), ok, speech, data or {}, error)


# ================================================================================
# A — the real files.read cursor
# ================================================================================


async def section_a(td: str) -> None:
    scenario("A: files.read has a real cursor")
    big, small = Path(td) / "big.txt", Path(td) / "small.txt"
    big.write_text(TEXT, encoding="utf-8")
    small.write_text(SHORT, encoding="utf-8")
    run = lambda **a: EXECUTOR.run("files.read", a, actor="test")  # noqa: E731

    p1 = await run(path=str(big))
    check("page 1: 4000 chars, truncated, next_offset=4000", p1.ok and p1.data["content"] == TEXT[:4000] and p1.data["truncated"] and p1.data["next_offset"] == 4000)
    check("page 1 keeps its original wording ('Showing the first part') and adds where to continue",
          "Showing the first part" in p1.speech and "offset=4000" in p1.speech)
    p2 = await run(path=str(big), offset=4000)
    check("page 2 (offset=4000): the NEXT 4000 chars", p2.ok and p2.data["content"] == TEXT[4000:8000] and p2.data["next_offset"] == 8000)
    p3 = await run(path=str(big), offset=8000)
    check("page 3 (offset=8000): the last 2000 chars, not truncated, no next_offset",
          p3.data["content"] == TEXT[8000:] and not p3.data["truncated"] and "next_offset" not in p3.data)
    check("the three pages reassemble the file exactly", p1.data["content"] + p2.data["content"] + p3.data["content"] == TEXT)
    check("page 2's speech says which characters it is showing", "4000-8000 of 10000" in p2.speech)
    check("the last page says it is the end of the file (so a planner stops paging)", "end of the file" in p3.speech and "end of the file" not in p2.speech and "end of the file" not in p1.speech)
    past = await run(path=str(big), offset=10000)
    check("offset at/after the end: a truthful failure, not an empty success", not past.ok and "nothing after" in past.speech)
    neg = await run(path=str(big), offset=-5)
    check("a negative offset is clamped to 0", neg.ok and neg.data["content"] == TEXT[:4000])
    sm = await run(path=str(small))
    check("a short file: whole content, no truncation, no next_offset (old behaviour)", sm.data["content"] == SHORT and not sm.data["truncated"] and "next_offset" not in sm.data)
    custom = await run(path=str(big), max_chars=100, offset=50)
    check("max_chars and offset compose", custom.data["content"] == TEXT[50:150] and custom.data["next_offset"] == 150)
    check("data keeps every pre-Phase-21 key", {"path", "content", "truncated"} <= set(p1.data))

    sk = REGISTRY.get("files.read")
    params = {p.name: p for p in sk.params}
    check("registry: offset is an optional int, default 0", "offset" in params and not params["offset"].required and params["offset"].default == 0)
    check("registry: files.read is still read-only (L0)", sk.tier == "L0")
    line = next(s for s in _tool_specs(["files.read"])).line()
    check("the planner's tool line shows the cursor", "offset (int, default 0)" in line, line)
    check("the tool's own registration is what makes it paginated: pagination_param('files.read') == 'offset'", intent.pagination_param("files.read") == "offset")
    check("a tool with no cursor is not paginated", intent.pagination_param("system.time") is None and intent.pagination_param("nope.tool") is None)


# ================================================================================
# B — intent helpers
# ================================================================================


def section_b() -> None:
    scenario("B: is_continuation_request / next_page_args (pure)")
    yes = ["Read more.", "read more of it", "Continue.", "Show the next page.", "show me more", "keep reading", "Go on", "the rest", "more", "Next", "read the next part"]
    no = ["Read this file.", "What's in the file?", "Read config.yaml", "Open the report", "Check the time and battery level.", "read it again", ""]
    for t in yes:
        check(f"continuation: {t!r}", intent.is_continuation_request(t))
    for t in no:
        check(f"NOT a continuation: {t!r}", not intent.is_continuation_request(t))

    args = {"path": "a.txt"}
    prior = {"next_offset": 4000}
    adv = intent.next_page_args("files.read", args, prior)
    check("advances the cursor to the position the previous call reported", adv == {"path": "a.txt", "offset": 4000})
    check("...without mutating the caller's args", args == {"path": "a.txt"})
    check("...and never changes WHAT is read (same path)", adv["path"] == "a.txt")
    check("no next_offset reported (end of file): None — the block stands", intent.next_page_args("files.read", args, {"truncated": False}) is None)
    check("no prior data at all: None", intent.next_page_args("files.read", args, None) is None and intent.next_page_args("files.read", args, {}) is None)
    check("already AT the reported position: None (would still be a repeat)", intent.next_page_args("files.read", {"path": "a", "offset": 4000}, prior) is None)
    check("a tool with no cursor: None", intent.next_page_args("system.time", {}, {"next_offset": 5}) is None)
    check("a bool is not a position", intent.next_page_args("files.read", args, {"next_offset": True}) is None)
    check("a STATE-CHANGING tool with a cursor: None (only reads ever advance)", intent.next_page_args("test.pg_write", {}, {"next_offset": 10}) is None)
    check("a read tool with a cursor and data: advances", intent.next_page_args("test.pg_read", {}, {"next_offset": 10}) == {"offset": 10})
    check("unregistered tool: None", intent.next_page_args("nope.write", {}, {"next_offset": 10}) is None)


# ================================================================================
# C — the repeat guard through the real loop
# ================================================================================


async def section_c(td: str) -> None:
    big = str(Path(td) / "big.txt")
    small = str(Path(td) / "small.txt")
    r = lambda **kw: call("files.read", {"path": big, **kw})  # noqa: E731

    scenario("C1: the SAME read is still blocked")
    reset()
    res, pl = await drive("Read big.txt.", [r(), r(), done("first page")])
    check("identical read, no continuation wording: ran ONCE", len(reads()) == 1)
    b = blocked(res)
    check("...the second became a structured ALREADY_TRIED", len(b) == 1 and b[0].data.get("status") == "already_tried")
    check("...no continuation event fired", not EVENTS.get("orchestrator.continuation"))

    scenario("C2: 'read more' is allowed — the guard advances the cursor")
    for goal in ("Read more of big.txt.", "Show the next page of big.txt.", "Continue reading big.txt."):
        reset()
        res, pl = await drive(goal, [r(), r(), done("two pages")])
        check(f"{goal!r}: two reads ran", len(reads()) == 2, str(reads()))
        check("...the second is at the position the first reported (offset=4000)", len(reads()) == 2 and reads()[1].get("offset") == 4000)
        check("...never blocked, and the second page is different content", not blocked(res) and res.observations[1].data["content"] == TEXT[4000:8000])
        check("...one continuation event", EVENTS.get("orchestrator.continuation") == 1)
        check("...the path was not changed by the guard", len(reads()) == 2 and reads()[1]["path"] == big)

    scenario("C3: a changed offset is allowed with NO continuation wording")
    reset()
    res, pl = await drive("Read big.txt.", [r(), r(offset=4000), done("p2")])
    check("explicit offset=4000: both ran, nothing blocked", len(reads()) == 2 and not blocked(res))
    check("...and the guard did not need to step in", not EVENTS.get("orchestrator.continuation"))
    reset()
    res, pl = await drive("Read big.txt.", [r(offset=8000), r(offset=4000), r(offset=0), done("d")])
    check("three different offsets in any order: all ran", len(reads()) == 3 and not blocked(res))

    scenario("C4: paging through a whole file, then the end")
    reset()
    res, pl = await drive("Keep reading big.txt.", [r(), r(), r(), r(), done("d")])
    check("pages 1-3 ran with offsets 0, 4000, 8000", [a.get("offset", 0) for a in reads()] == [0, 4000, 8000], str(reads()))
    check("the 4th identical read (end of file, nothing reported) is BLOCKED, not looped", len(reads()) == 3 and len(blocked(res)) == 1)
    check("...pages reassemble the whole file", "".join(o.data["content"] for o in res.observations if o.ok) == TEXT)

    scenario("C5: pagination is NOT a generic bypass of repeat protection")
    reset()
    res, pl = await drive("Continue. Check the time.", [call("system.time"), call("system.time"), done("d")])
    check("continuation wording + a tool with no cursor: the repeat is still blocked", [t for t, _ in CALLS] == ["system.time"] and len(blocked(res)) == 1)
    reset()
    res, pl = await drive("Read more of small.txt.", [call("files.read", {"path": small}), call("files.read", {"path": small}), done("d")])
    check("continuation wording + a read whose previous call reported NO next position: still blocked", len(reads()) == 1 and len(blocked(res)) == 1)
    reset()
    res, pl = await drive("Read more and delete the old report.", [call("files.delete", {"path": "old.txt"}), call("files.delete", {"path": "old.txt"}), done("d")])
    check("a DESTRUCTIVE duplicate under 'read more' wording: blocked", [t for t, _ in CALLS] == ["files.delete"] and len(blocked(res)) == 1)
    reset()
    res, pl = await drive("Delete the old report.", [call("files.delete", {"path": "old.txt"}), call("files.delete", {"path": "old.txt"}), done("d")])
    check("a destructive duplicate with no continuation wording: blocked (unchanged)", [t for t, _ in CALLS] == ["files.delete"] and len(blocked(res)) == 1)
    reset()
    res, pl = await drive("Continue and save the notes.", [call("test.pg_write"), call("test.pg_write"), done("d")])
    check("a STATE-CHANGING tool that has an offset cursor is never advanced: the identical call is blocked",
          [t for t, _ in CALLS] == ["test.pg_write"] and len(blocked(res)) == 1 and not EVENTS.get("orchestrator.continuation"))
    reset()
    res, pl = await drive("Read more.", [call("test.pg_read"), call("test.pg_read"), done("d")])
    check("...but the same shape on a READ tool (test.pg_read) does advance", [a.get("offset") for t, a in CALLS if t == "test.pg_read"] == [None, 10] or
          [a.get("offset", 0) for t, a in CALLS if t == "test.pg_read"] == [0, 10])
    reset()
    res, pl = await drive("Read more.", [call("ui.click", {"label": "Close"}), r(), done("d")])
    check("'read more' cannot launder a mismatched action: ui.click is still an intent_mismatch", not any(t == "ui.click" for t, _ in CALLS) and res.observations[0].error == "intent_mismatch")
    reset()
    CFG.planner.continuation_reads = False
    res, pl = await drive("Read more of big.txt.", [r(), r(), done("d")])
    check("continuation_reads=False: 'read more' is held to the guard exactly as before", len(reads()) == 1 and len(blocked(res)) == 1)
    reset()
    res, pl = await drive("Read more of big.txt.", [r(), r(), r(), done("d")])
    check("bounded: a planner asking for the same page again and again still ends (streak of blocks stops the run)",
          res.stopped in ("repeated_action", "completed", "step_limit") and len(reads()) <= 3)

    scenario("C6: find_prior_attempt itself is unchanged")
    sig = list(inspect.signature(find_prior_attempt).parameters)
    check("signature pinned: (observations, tool, args, tier_of)", sig == ["observations", "tool", "args", "tier_of"])
    o1 = obs("files.read", args={"path": big}, data={"next_offset": 4000})
    o1.fingerprint = orch_mod._state_fingerprint({"path": big})  # as run_goal records it
    check("identical read, nothing changed -> a prior attempt", find_prior_attempt([o1], "files.read", {"path": big}, {"files.read": "L0"}) is not None)
    check("same read with a different offset -> not a repeat", find_prior_attempt([o1], "files.read", {"path": big, "offset": 4000}, {"files.read": "L0"}) is None)
    check("a state-changing duplicate right after itself -> a prior attempt",
          find_prior_attempt([obs("files.delete", args={"path": "x"})], "files.delete", {"path": "x"}, {"files.delete": "L2"}) is not None)
    check("NOT_EXECUTED_ERRORS unchanged", set(NOT_EXECUTED_ERRORS) == {"repeated_call", "intent_mismatch"})


# ================================================================================
# D — Session-level "read more"
# ================================================================================


async def section_d(td: str) -> None:
    scenario("D: a bare 'read more' after a direct read (real Session, real executor)")
    big = str(Path(td) / "big.txt")
    reset()
    first = await SESSION._run("files.read", {"path": big}, actor="text")
    check("the direct read returned page 1 with a cursor", first.ok and SESSION.last_data.get("next_offset") == 4000)
    more = await SESSION.handle("read more")
    check("'read more' -> page 2 (offset=4000), through the normal path", more.ok and more.data["content"] == TEXT[4000:8000] and SESSION.last_args.get("offset") == 4000)
    nxt = await SESSION.handle("show the next page")
    check("'show the next page' -> page 3", nxt.ok and nxt.data["content"] == TEXT[8000:])
    check("at the end: nothing more to continue (returns None, so the utterance is handled normally)", await SESSION._maybe_continue_read("continue", actor="text") is None)
    check("a plain utterance is not a continuation", await SESSION._maybe_continue_read("read the file", actor="text") is None)

    reset()
    await SESSION._run("system.time", {}, actor="text")
    check("after a tool with no cursor, 'read more' does nothing", await SESSION._maybe_continue_read("read more", actor="text") is None)

    reset()
    SESSION.last_skill, SESSION.last_args, SESSION.last_data = "test.pg_write", {}, {"next_offset": 10}
    check("a state-changing last call is never continued", await SESSION._maybe_continue_read("read more", actor="text") is None)

    reset()
    await SESSION._run("files.read", {"path": big}, actor="text")
    saved = dict(CFG.permissions.overrides)
    CFG.permissions.overrides = {**saved, "files.read": "deny"}
    try:
        denied = await SESSION._maybe_continue_read("read more", actor="text")
    finally:
        CFG.permissions.overrides = saved
    check("the continuation still goes through permissions: a policy-denied read is refused", denied is not None and not denied.ok and "not allowed" in denied.speech.lower(), str(denied and denied.speech))
    reset()
    await SESSION._run("files.read", {"path": big}, actor="text")
    CFG.planner.continuation_reads = False
    check("continuation_reads=False: the Session does nothing", await SESSION._maybe_continue_read("read more", actor="text") is None)
    reset()


# ================================================================================
# E — static invariants
# ================================================================================


def section_e() -> None:
    scenario("E: static invariants")
    src = inspect.getsource(intent)
    imports = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and n.module}
    check("intent.py still imports nothing from permissions", not any("permissions" in (m or "") for m in imports))
    check("next_page_args/is_continuation_request take only the user's text / the tool's own data",
          list(inspect.signature(intent.next_page_args).parameters) == ["tool", "args", "prior_data"]
          and list(inspect.signature(intent.is_continuation_request).parameters) == ["text"])
    osrc = inspect.getsource(orch_mod.Orchestrator.run_goal)
    check("run_goal only advances a cursor AFTER alignment and BEFORE the guard's block (structural order)",
          osrc.index("intent.check_alignment(") < osrc.index("intent.next_page_args(") < osrc.index('"ALREADY_TRIED: '))
    check("only the cursor argument is rewritten (the helper copies args and sets one key)", "new_args[param] = nxt" in src and src.count("new_args[") == 1)


async def main() -> int:
    t0 = time.perf_counter()
    _register_fixtures()
    saved = H.lock_down_real_tools(REGISTRY)
    try:
        with store.use_temp_db(), tempfile.TemporaryDirectory() as td:
            await section_a(td)
            section_b()
            await section_c(td)
            await section_d(td)
            section_e()
    finally:
        CFG.permissions.overrides = saved
        CFG.planner.continuation_reads = True
    return H.finish("PAGINATION SCORECARD", time.perf_counter() - t0, min_assertions=80, min_scenarios=8)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
