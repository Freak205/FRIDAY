"""Phase 22.0 — deterministic scorecard for the SEMANTIC REPEAT GUARD.

Phase 21 ended with a live finding (B2): asked to read a whole 14k file, the 3B model dodged
the repeat guard by changing an irrelevant argument — `files.read(max_chars=10^6)`, then
`10^7`... — each call being "different" and each returning the same file. The guard compared
arguments literally. Phase 22 compares what a call DOES:

    files.read(path, max_chars=100)  ==  files.read(path, max_chars=5000)     (same read)
    files.read(path)                 !=  files.read(path, offset=4000)        (the next page)
    files.read("C:\\x\\A.txt")        ==  files.read("c:/x/a.txt")             (same file)

  A  `intent.normalize_args(semantic=True)` (pure): what is and is not the same call
  B  the real run_goal over the real `files.read`: max_chars-only blocked, offset allowed, a
     different target allowed, spellings equal, the block explains how to get more
  C  failed / unconfirmed reads stay retryable
  D  state-changing calls stay strongly protected (nothing is ever ignored for them)
  E  "read more" still pages, and the switch restores the old behaviour
  F  registry invariants: who may declare a presentational argument

Deterministic: scripted planner replies; reads go through the real L0 executor on a temp
file; state-changing tools are `test.sr_*` fixtures that flip counters; every real non-L0
skill is hard-denied. No Ollama.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated

import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario

from friday import intent, store  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec, _call_key, _tool_specs, find_prior_attempt  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

REGISTRY.discover()

TMP = Path(tempfile.mkdtemp(prefix="friday_sr_"))
BIG = TMP / "big.txt"
BIG.write_text("".join(chr(97 + (i % 26)) for i in range(10000)), encoding="utf-8")  # 10 000 chars: pages at 4000
OTHER = TMP / "other.txt"
OTHER.write_text("a different file entirely", encoding="utf-8")
CALLS: list[tuple[str, dict]] = []
FLAGS: dict[str, int] = {}
APPROVE = [True]


def bump(n: str) -> None:
    FLAGS[n] = FLAGS.get(n, 0) + 1


def flag(n: str) -> int:
    return FLAGS.get(n, 0)


def _register_fixtures() -> None:
    @skill(name="test.sr_write", tier="L1", action="modify", description="state-changing tool WITH a cursor and a size cap",
           presentational=("max_chars",))  # declared on purpose: the guard must still never ignore it for a write
    def _write(path: Annotated[str, "file"], max_chars: Annotated[int, "cap"] = 100, offset: Annotated[int, "cursor"] = 0) -> SkillResult:
        bump("write")
        return SkillResult(speech="sr_write: wrote it.", data={"next_offset": offset + 10})

    @skill(name="test.sr_delete", tier="L2", action="delete", description="destructive stand-in (needs confirmation)")
    def _delete(path: Annotated[str, "file"]) -> SkillResult:
        bump("delete")
        return SkillResult(speech="sr_delete: removed it.")

    @skill(name="test.sr_uncertain", tier="L0", description="a read that reports its own result as unconfirmed")
    def _unc(target: Annotated[str, "what"] = "x") -> SkillResult:
        bump("uncertain")
        return SkillResult(speech="sr_uncertain: could not confirm.", data={"uncertain": True})

    @skill(name="test.sr_other", tier="L0", description="an unrelated read")
    def _other() -> SkillResult:
        bump("other")
        return SkillResult(speech="sr_other: something else.")


async def _confirm(skill_, args, preview: str) -> bool:
    bump("confirm_prompts")
    return APPROVE[0]


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    return await EXECUTOR.run(tool, args, actor=actor)


def reset() -> None:
    CALLS.clear()
    FLAGS.clear()
    INTEL.reset()
    EXECUTOR.set_confirm_handler(_confirm)
    APPROVE[0] = True
    CFG.planner.structured_output = False
    CFG.planner.intent_guard = True
    CFG.planner.continuation_reads = True
    CFG.planner.goal_coverage = True
    CFG.planner.semantic_repeat_guard = True
    CFG.desktop_observer.enabled = False


def names() -> list[str]:
    return [s.name for s in REGISTRY.all() if s.name != "plan.run"]


async def drive(goal: str, replies: list[str], *, max_steps: int = 10):
    CALLS.clear()
    planner = ScriptedPlanner(replies)
    specs = _tool_specs(names())
    orch = Orchestrator(tools=names(), runner=runner, actor="test", llm_provider=planner, tool_specs=specs, max_steps=max_steps)
    res = await orch.run_goal(goal, action_scope=intent.derive_scope(goal), max_replans=CFG.planner.max_replans)
    return res, planner


def reads() -> list[dict]:
    return [a for t, a in CALLS if t == "files.read"]


def blocked(res) -> list[Observation]:
    return [o for o in res.observations if o.error == "repeated_call"]


def rd(path: Path | str, **kw) -> str:
    return call("files.read", {"path": str(path), **kw})


def o_(tool: str, args: dict, *, ok: bool = True, data: dict | None = None, fp: str = "") -> Observation:
    ob = Observation(PlanStep(tool, args), ok, "x", data or {})
    ob.fingerprint = fp
    return ob


# =============================================================================================
# A — what is "the same call"
# =============================================================================================


def section_a() -> None:
    scenario("A1: files.read — a size cap is not part of WHAT is read")
    n = lambda args, **k: intent.normalize_args("files.read", args, **k)  # noqa: E731
    p = str(BIG)
    same = [
        {"path": p, "max_chars": 100}, {"path": p, "max_chars": 4000}, {"path": p, "max_chars": 10**6},
        {"path": p, "max_chars": 10**7, "offset": 0}, {"path": p, "max_chars": "5000"}, {"path": p},
    ]
    keys = {str(sorted(n(a, semantic=True).items())) for a in same}
    check("six calls differing only in max_chars / a default offset collapse to ONE identity", len(keys) == 1, str(keys))
    check("semantic=False keeps the Phase 20/21 literal identity (max_chars is part of it)", n({"path": p, "max_chars": 100}) != n({"path": p, "max_chars": 5000}))
    check("a different OFFSET is a different read", n({"path": p}, semantic=True) != n({"path": p, "offset": 4000}, semantic=True))
    check("...even when the size cap also changes", n({"path": p, "max_chars": 100, "offset": 4000}, semantic=True) == n({"path": p, "max_chars": 99999, "offset": 4000}, semantic=True)
          and n({"path": p, "max_chars": 100, "offset": 4000}, semantic=True) != n({"path": p, "max_chars": 100}, semantic=True))
    check("a different PATH is a different read", n({"path": p}, semantic=True) != n({"path": str(OTHER)}, semantic=True))
    check("offset '4000' (a string) equals offset 4000", n({"path": p, "offset": "4000"}, semantic=True) == n({"path": p, "offset": 4000}, semantic=True))

    scenario("A2: the same target however it is spelled")
    base = n({"path": p}, semantic=True)
    variants = {
        "forward slashes": p.replace("\\", "/"), "UPPER CASE": p.upper(), "trailing dots segment": str(TMP / "sub" / ".." / "big.txt"),
        "surrounding spaces": f"  {p}  ", "double separators": p.replace("\\", "\\\\", 1) if "\\" in p else p,
    }
    for label, v in variants.items():
        check(f"path spelled with {label}: same read", n({"path": v}, semantic=True) == base, f"{v!r} -> {n({'path': v}, semantic=True)}")
    rel = os.path.relpath(p)
    check("a relative path to the same file: same read", n({"path": rel}, semantic=True) == base, rel)
    check("semantic=False does NOT equate them (only the semantic guard does)", n({"path": p.replace('\\', '/')}) != n({"path": p}) or os.sep == "/")
    u = lambda url: intent.normalize_args("web.fetch", {"url": url}, semantic=True)  # noqa: E731
    check("urls: scheme/host case, a fragment and a trailing slash are the same page", u("HTTPS://Example.com/a/") == u("https://example.com/a") == u("https://example.com/a#top"))
    check("...but a different path or query is a different page", u("https://example.com/a") != u("https://example.com/b") and u("https://example.com/a?x=1") != u("https://example.com/a?x=2"))
    check("an empty / None optional argument is the same as leaving it out",
          intent.normalize_args("files.search", {"query": "x", "extension": ""}, semantic=True) == intent.normalize_args("files.search", {"query": "x"}, semantic=True)
          == intent.normalize_args("files.search", {"query": "x", "extension": None}, semantic=True))
    check("...but a REAL optional value still counts", intent.normalize_args("files.search", {"query": "x", "extension": "pdf"}, semantic=True) != intent.normalize_args("files.search", {"query": "x"}, semantic=True))
    check("a numeric string equals the number for an int parameter", intent.normalize_args("web.search", {"query": "x", "count": "5"}, semantic=True) == intent.normalize_args("web.search", {"query": "x"}, semantic=True))
    check("case and whitespace in a query are ignored (as before)", intent.normalize_args("web.search", {"query": " Hello   World "}, semantic=True) == intent.normalize_args("web.search", {"query": "hello world"}, semantic=True))
    check("a different query is a different call", intent.normalize_args("web.search", {"query": "cats"}, semantic=True) != intent.normalize_args("web.search", {"query": "dogs"}, semantic=True))

    scenario("A3: a STATE-CHANGING call never has anything ignored")
    w = lambda a: intent.normalize_args("test.sr_write", a, semantic=True)  # noqa: E731
    check("test.sr_write DECLARES max_chars presentational and has a cursor — yet different max_chars stay different (it is not a read)", w({"path": p, "max_chars": 1}) != w({"path": p, "max_chars": 2}))
    check("...spelling is still normalised for it (a stronger guard, never a weaker one)", w({"path": p}) == w({"path": p.replace("\\", "/").upper()}))
    check("the same tool as a READ would ignore the cap: files.read does, test.sr_write does not",
          n({"path": p, "max_chars": 1}, semantic=True) == n({"path": p, "max_chars": 2}, semantic=True) and w({"path": p, "max_chars": 1}) != w({"path": p, "max_chars": 2}))

    scenario("A4: through find_prior_attempt (the guard itself)")
    tier = {"files.read": "L0", "test.sr_write": "L1"}
    prior = [o_("files.read", {"path": p, "max_chars": 100}, data={"next_offset": 100}, fp="x")]
    from friday.orchestrator import _state_fingerprint

    fp = _state_fingerprint({"path": p})
    prior = [o_("files.read", {"path": p, "max_chars": 100}, data={"next_offset": 100}, fp=fp)]
    check("guard: max_chars-only re-read of an unchanged file -> blocked", find_prior_attempt(prior, "files.read", {"path": p, "max_chars": 9999}, tier) is not None)
    check("guard: different offset -> not blocked", find_prior_attempt(prior, "files.read", {"path": p, "max_chars": 100, "offset": 100}, tier) is None)
    check("guard: different file -> not blocked", find_prior_attempt(prior, "files.read", {"path": str(OTHER)}, tier) is None)
    check("guard: the same file spelled differently -> blocked", find_prior_attempt(prior, "files.read", {"path": p.replace("\\", "/").upper()}, tier) is not None)
    CFG.planner.semantic_repeat_guard = False
    check("semantic_repeat_guard=False: the max_chars variation gets through (the Phase 21 behaviour)", find_prior_attempt(prior, "files.read", {"path": p, "max_chars": 9999}, tier) is None)
    CFG.planner.semantic_repeat_guard = True
    check("_call_key follows the switch", _call_key("files.read", {"path": p, "max_chars": 1}) == _call_key("files.read", {"path": p, "max_chars": 2}))


# =============================================================================================
# B — the real run_goal over the real files.read
# =============================================================================================


async def section_b() -> None:
    scenario("B1: the exact Phase 21 dodge — only max_chars changes -> BLOCKED")
    reset()
    res, pl = await drive("Read big.txt", [rd(BIG, max_chars=100), rd(BIG, max_chars=5000), done("read")])
    check("exactly ONE real read ran", len(reads()) == 1, str(reads()))
    check("the second call was blocked as ALREADY_TRIED", len(blocked(res)) == 1 and blocked(res)[0].data["status"] == "already_tried")
    check("...and the run then completed normally", res.ok and res.stopped == "completed")
    check("the planner is told which step it repeats", "same as step 1" in pl.prompt_of(2))

    reset()
    res, _ = await drive("Read big.txt", [rd(BIG, max_chars=100), rd(BIG, max_chars=10**6), rd(BIG, max_chars=10**7), done("x")])
    check("the escalating dodge (100, 10^6, 10^7): one real read, then the run stops as repeated_action", len(reads()) == 1 and res.stopped == "repeated_action", f"{len(reads())} {res.stopped}")

    scenario("B2: a different offset is the next page — ALLOWED")
    reset()
    res, _ = await drive("Read big.txt", [rd(BIG), rd(BIG, offset=4000), rd(BIG, offset=8000), done("read it all")])
    check("three pages, three real reads, nothing blocked", len(reads()) == 3 and not blocked(res), f"{len(reads())} reads, {len(blocked(res))} blocked")
    check("...the offsets are the ones the tool asked for (4000, 8000)", [r.get("offset", 0) for r in reads()] == [0, 4000, 8000])
    check("...and the run completed", res.ok and res.stopped == "completed")
    reset()
    res, _ = await drive("Read big.txt", [rd(BIG, max_chars=100), rd(BIG, max_chars=99999, offset=100), done("x")])
    check("offset AND max_chars both change: the offset decides -> allowed", len(reads()) == 2 and not blocked(res))

    scenario("B3: a genuinely different target is allowed")
    reset()
    res, _ = await drive("Read big.txt and other.txt", [rd(BIG), rd(OTHER), done("both")])
    check("two different files: two real reads", len(reads()) == 2 and not blocked(res))
    reset()
    res, _ = await drive("Read big.txt", [rd(BIG, max_chars=100), rd(OTHER, max_chars=100), done("x")])
    check("...and a different file with the SAME size cap is still a different read", len(reads()) == 2 and not blocked(res))

    scenario("B4: the same file spelled another way is the same read")
    reset()
    fwd = str(BIG).replace("\\", "/")
    res, _ = await drive("Read big.txt", [rd(BIG), rd(fwd), done("x")])
    check("backslashes vs forward slashes: blocked", len(reads()) == 1 and len(blocked(res)) == 1)
    reset()
    res, _ = await drive("Read big.txt", [rd(BIG), rd(str(TMP / "sub" / ".." / "big.txt").upper(), max_chars=50), done("x")])
    check("'..' segment + upper case + a different max_chars: blocked", len(reads()) == 1 and len(blocked(res)) == 1)

    scenario("B5: the block says how to actually get more")
    reset()
    res, pl = await drive("Read big.txt", [rd(BIG, max_chars=100), rd(BIG, max_chars=5000), done("x")])
    b = blocked(res)[0]
    check("the ALREADY_TRIED data carries the cursor hint from the previous read (next_offset=100)", b.data.get("next_hint") == "offset=100", str(b.data))
    check("...and the next planner prompt shows it", "More remains: use offset=100" in pl.prompt_of(2), pl.prompt_of(2)[-350:])
    reset()
    res, _ = await drive("Read other.txt", [rd(OTHER), rd(OTHER, max_chars=10), done("x")])
    check("a file that was fully read (no next_offset): no cursor hint invented", "next_hint" not in blocked(res)[0].data)
    check("...instead it says outright that the earlier result was COMPLETE (the model kept re-reading a whole file 'to reach the end')",
          blocked(res)[0].data.get("complete") is True and "complete" in blocked(res)[0].speech, str(blocked(res)[0].data))


# =============================================================================================
# C — failed / unconfirmed reads stay retryable
# =============================================================================================


async def section_c() -> None:
    scenario("C1: a FAILED read can be retried after something else happened")
    reset()
    missing = TMP / "missing.txt"
    if missing.exists():
        missing.unlink()
    res, _ = await drive("Read missing.txt", [rd(missing, max_chars=100), call("test.sr_other"), rd(missing, max_chars=5000), done("x")])
    check("failed read -> another step -> the same read with a different cap runs again (2 real attempts)", len(reads()) == 2 and not blocked(res))
    check("...both attempts genuinely failed (nothing was faked)", [o.ok for o in res.observations if o.step.tool == "files.read"] == [False, False])

    scenario("C2: ...and immediately, when the world changed (the file appeared)")
    reset()
    late = TMP / "late.txt"
    if late.exists():
        late.unlink()

    async def appear_runner(tool, args, actor):
        r = await runner(tool, args, actor)
        if tool == "files.read" and not late.exists():
            late.write_text("now it exists", encoding="utf-8")
        return r

    planner = ScriptedPlanner([rd(late, max_chars=10), rd(late, max_chars=20), done("x")])
    orch = Orchestrator(tools=names(), runner=appear_runner, actor="test", llm_provider=planner, tool_specs=_tool_specs(names()), max_steps=6)
    res = await orch.run_goal("Read late.txt", action_scope=intent.derive_scope("Read late.txt"), max_replans=2)
    check("the file did not exist, then did: the immediate retry is allowed (its state fingerprint changed)", len(reads()) == 2 and not blocked(res))
    check("...and the second read succeeded", [o.ok for o in res.observations if o.step.tool == "files.read"] == [False, True])

    scenario("C3: an UNCONFIRMED read is retryable after something else happened")
    reset()
    res, _ = await drive("Check it", [call("test.sr_uncertain", {"target": "Foo"}), call("test.sr_other"), call("test.sr_uncertain", {"target": "  foo "}), done("x")])
    check("uncertain -> other -> the same call (re-spelled): runs again", flag("uncertain") == 2 and not blocked(res))

    scenario("C4: what is still blocked (documented): re-issuing a failed read at once with nothing changed")
    reset()
    if missing.exists():
        missing.unlink()
    res, _ = await drive("Read missing.txt", [rd(missing, max_chars=100), rd(missing, max_chars=200), done("x")])
    check("failed read, IMMEDIATELY re-issued with only a bigger cap and no change in the world: blocked (as an identical re-issue always was)",
          len(reads()) == 1 and len(blocked(res)) == 1)


# =============================================================================================
# D — state-changing calls stay strongly protected
# =============================================================================================


async def section_d() -> None:
    scenario("D1: an immediate identical state-changing call is blocked — however it is spelled")
    reset()
    p = str(BIG)
    res, _ = await drive("Update big.txt", [call("test.sr_write", {"path": p}), call("test.sr_write", {"path": p.replace("\\", "/").upper()}), done("x")])
    check("write twice in a row (second one re-spelled): the second never ran", flag("write") == 1 and len(blocked(res)) == 1)

    scenario("D2: ...a different size cap on a state-changing call is NOT ignored (it is a different call)")
    reset()
    res, _ = await drive("Update big.txt", [call("test.sr_write", {"path": p, "max_chars": 1}), call("test.sr_write", {"path": p, "max_chars": 2}), done("x")])
    check("write(max_chars=1) then write(max_chars=2): both ran — the presentational declaration is honoured for READS only", flag("write") == 2 and not blocked(res))

    scenario("D3: a real second press is still allowed (state-changing, read in between)")
    reset()
    res, _ = await drive("Update big.txt", [call("test.sr_write", {"path": p}), call("test.sr_other"), call("test.sr_write", {"path": p}), done("x")])
    check("write, read, write: the second write ran (something happened in between)", flag("write") == 2 and not blocked(res))

    scenario("D4: a DESTRUCTIVE duplicate — one confirmation, one execution")
    reset()
    victim = str(TMP / "victim.txt")
    res, _ = await drive("Delete victim.txt", [call("test.sr_delete", {"path": victim}), call("test.sr_delete", {"path": victim.replace("\\", "/").upper()}), done("x")])
    check("delete then the same delete re-spelled: asked ONCE, ran ONCE", flag("confirm_prompts") == 1 and flag("delete") == 1)
    check("...the duplicate was blocked before it could raise another prompt", len(blocked(res)) == 1)
    reset()
    APPROVE[0] = False
    res, _ = await drive("Delete victim.txt", [call("test.sr_delete", {"path": victim}), call("test.sr_delete", {"path": victim}), done("x")])
    check("declined: never ran, and the decline stopped the run (no second prompt)", flag("delete") == 0 and flag("confirm_prompts") == 1)

    scenario("D5: a read cannot mask a state change — after a write, re-reading the same file is allowed")
    reset()
    res, _ = await drive("Update big.txt then read it", [rd(BIG, max_chars=50), call("test.sr_write", {"path": p}), rd(BIG, max_chars=50), done("x")])
    check("read, write, read (same args): the second read runs — the world changed", len(reads()) == 2 and not blocked(res))


# =============================================================================================
# E — "read more" and the switch
# =============================================================================================


async def section_e() -> None:
    scenario("E1: 'read more' still pages, even when the model varies the size cap")
    reset()
    res, _ = await drive("Read big.txt, then keep reading until you reach the end", [rd(BIG, max_chars=100), rd(BIG, max_chars=999), done("x")])
    rs = reads()
    check("the varied-cap repeat under a continuation goal ADVANCED the cursor instead of being blocked", len(rs) == 2 and rs[1].get("offset") == 100, str(rs))
    check("...nothing was blocked, and the second read is the next page (not the same one again)", not blocked(res))

    scenario("E2: the switch — off restores the Phase 21 behaviour exactly")
    reset()
    CFG.planner.semantic_repeat_guard = False
    res, _ = await drive("Read big.txt", [rd(BIG, max_chars=100), rd(BIG, max_chars=5000), done("x")])
    check("semantic_repeat_guard=False: max_chars variations pass again (2 real reads)", len(reads()) == 2 and not blocked(res))
    res, _ = await drive("Read big.txt", [rd(BIG), rd(str(BIG).replace("\\", "/")), done("x")])
    check("...and a re-spelled path is a different call again", len(reads()) == 2 and not blocked(res))
    res, _ = await drive("Read big.txt", [rd(BIG), rd(BIG), done("x")])
    check("...but an IDENTICAL repeat is still blocked (Phase 20 unchanged)", len(reads()) == 1 and len(blocked(res)) == 1)
    reset()


# =============================================================================================
# F — registry invariants
# =============================================================================================


def section_f() -> None:
    scenario("F: only a READ tool with a page cursor may declare a presentational argument")
    declared = {s.name: s.presentational for s in REGISTRY.all() if s.presentational and not s.name.startswith("test.")}
    check("the only real skill that declares one is files.read(max_chars)", declared == {"files.read": ("max_chars",)}, str(declared))
    for name, args in declared.items():
        sk = REGISTRY.get(name)
        check(f"{name}: is L0 (a read)", sk.tier == "L0")
        check(f"{name}: has a page cursor (offset/page) — ignoring the cap can never hide content", intent.pagination_param(name) is not None)
        check(f"{name}: every declared argument is a real parameter", all(a in {p.name for p in sk.params} for a in args))
    check("no real state-changing skill declares one", all(not s.presentational for s in REGISTRY.all() if s.tier != "L0" and not s.name.startswith("test.")))
    check("the size caps of tools WITHOUT a cursor (browser.read, web.fetch, ui.read) are not declared: a bigger cap is their only way to more content",
          all(not REGISTRY.get(n).presentational for n in ("browser.read", "web.fetch", "ui.read", "notes.read", "web.search")))
    check("the fixture that declares one on a write is ignored for it (A3) — enforced in code, not by convention",
          "is_passive_call(tool, args)" in __import__("inspect").getsource(intent.normalize_args))


async def main() -> int:
    t0 = time.perf_counter()
    _register_fixtures()
    saved = H.lock_down_real_tools(REGISTRY)
    try:
        with store.use_temp_db():
            section_a()
            await section_b()
            await section_c()
            await section_d()
            await section_e()
            section_f()
    finally:
        CFG.permissions.overrides = saved
        CFG.planner.semantic_repeat_guard = True
        shutil.rmtree(TMP, ignore_errors=True)
    return H.finish("SEMANTIC REPEAT GUARD SCORECARD", time.perf_counter() - t0, min_assertions=70, min_scenarios=14)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
