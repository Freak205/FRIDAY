"""Phase 22.0 — deterministic scorecard for CONVERSATIONAL CONFIRMATION ROUTING.

The known Phase 21 gap: with no follow-up slot (a second utterance after another command),
the BRAIN's embedding matcher maps a bare "yes, fix it" to the skill whose example phrasing is
nearest. Measured on this tree, BEFORE the fix:

    "yes, fix it" / "fix it" / "ok fix that"  -> meta.undo    (0.70-0.84)   <- reverses a real change
    "yes" / "yes please" / "go ahead"         -> whatsapp.send (0.73-0.87)
    "do it"                                   -> schedule.create (asks for a slot)

Similarity to an example phrase is not what the user asked for. Phase 22:

  * resolve against the active goal / previous turn when there is one — `Session` already does
    that for a report-only goal (Phase 21); it is re-verified here end to end;
  * otherwise ask what the user means — `Session._guard_dangling_confirmation` — and NEVER run a
    skill on the strength of an affirmation alone;
  * `meta.undo` requires the user to actually say undo/revert/take back/put it back...;
  * everything else routes exactly as before.

  A  the two pure predicates (`is_bare_affirmation`, `asks_for_undo`)
  B  "Yes, fix it" with an active goal continues THAT goal (real Session -> plan.run -> executor)
  C  no antecedent: a clarifying question, and no skill is run (meta.undo, whatsapp.send, ...)
  D  an explicit undo still reaches meta.undo
  E  routing that must NOT change: a corpus of ordinary utterances, and every pending state
  F  the switch, and static invariants

Deterministic: the REAL BRAIN and Session; scripted planner replies; every dispatch through the
executor is recorded, and every real non-L0 skill is hard-denied for the whole process, so a
mis-route is observed (as an attempted skill name) and can never act. Throwaway DB. No Ollama.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import time
from collections import Counter

import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario, scripted_provider

from friday import intent, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import context_memory  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION, Pending  # noqa: E402

REGISTRY.discover()

A = intent.ActionClass
FLAGS: Counter = Counter()
ATTEMPTS: list[str] = []  # every skill the Session handed to the executor, in order
_ORIG_RUN = EXECUTOR.run


async def _recording_run(skill_name, args=None, **kw):
    ATTEMPTS.append(skill_name)
    return await _ORIG_RUN(skill_name, args, **kw)


def _register_fixtures() -> None:
    @skill(name="test.tc_read", tier="L0", description="read the project state (confirmation-routing smoke)")
    def _read() -> SkillResult:
        FLAGS["read"] += 1
        return SkillResult(speech="tc_read: the Flask app is not starting because requirements.txt is missing flask.")

    @skill(name="test.tc_fix", tier="L1", action="modify", description="reversible write stand-in (auto-approved L1)")
    def _fix() -> SkillResult:
        FLAGS["fix"] += 1
        return SkillResult(speech="tc_fix: added flask to requirements.txt.")

    from friday import verify

    verify.register("test.tc_fix", verify.Verifier(lambda a, d, b: [verify.Check("fixture effect", FLAGS["fix"] >= 1, "fixed", "fixed")]))


async def _confirm(skill_, args, preview: str) -> bool:
    FLAGS["confirm_prompts"] += 1
    return False


def reset() -> None:
    INTEL.reset()
    context_memory.CONTEXT.reset()
    SESSION.pending = None
    SESSION._followup = None
    SESSION.last_skill, SESSION.last_args, SESSION.last_data = None, {}, {}
    SESSION.last_utterance = ""
    EXECUTOR.set_confirm_handler(_confirm)
    FLAGS.clear()
    ATTEMPTS.clear()
    CFG.planner.structured_output = False
    CFG.planner.decision_repair_attempts = 1
    CFG.planner.intent_guard = True
    CFG.planner.scope_expansion = True
    CFG.planner.goal_coverage = True
    CFG.planner.confirmation_guard = True
    CFG.desktop_observer.enabled = False
    BASE[0] = len(goals_mod.recent(1000))


BASE = [0]


def goal_rows() -> int:
    return len(goals_mod.recent(1000)) - BASE[0]


async def first_turn(goal: str, replies: list[str]):
    planner = ScriptedPlanner(replies)
    with scripted_provider(planner):
        r = await SESSION._run("plan.run", {"goal": goal}, actor="text")
    return r, planner


async def say(text: str, replies: list[str] | None = None):
    """The user's NEXT utterance through the real `Session.handle`."""
    planner = ScriptedPlanner(replies or [done("nothing")])
    with scripted_provider(planner):
        r = await SESSION.handle(text, actor="text")
    return r, planner


def cleared(r) -> bool:
    return (r.data or {}).get("clarification") == "no_antecedent"


def fresh() -> None:
    """A conversation with nothing pending and nothing to refer to."""
    reset()


# =============================================================================================
# A — the pure predicates
# =============================================================================================


def section_a() -> None:
    scenario("A1: is_bare_affirmation — nothing but an affirmation")
    yes = ["yes", "Yes", "yes.", "Yes!", "yeah", "yep", "yup", "sure", "ok", "okay", "alright", "absolutely", "please", "go ahead",
           "do it", "yes please", "yeah, go ahead", "ok please", "sure, do it", "yes yes yes", "  yes  "]
    for t in yes:
        check(f"bare affirmation: {t!r}", intent.is_bare_affirmation(t))
    no = ["yes, fix it", "fix it", "yes fix the issues", "yes, delete it", "ok open notepad", "yeah what time is it", "undo", "no", "nope",
          "do it again", "go ahead and send it", "yes, undo that", "what time is it", "", "   ", "yes and no"]
    for t in no:
        check(f"NOT a bare affirmation: {t!r}", not intent.is_bare_affirmation(t))

    scenario("A2: asks_for_undo — the user's own words must say so")
    sk = REGISTRY.get("meta.undo")
    for ex in sk.examples:
        check(f"every meta.undo example the skill itself teaches passes: {ex!r}", intent.asks_for_undo(ex))
    for t in ["undo", "Undo that", "please undo it", "yes, undo that", "revert that", "revert it", "take it back", "take that back",
              "take back what you just did", "put things back", "put it back", "put it back the way it was", "go back",
              "roll back the last change", "rollback", "reverse that",
              "change it back", "set it back", "cancel that", "cancel what you did", "restore it", "as it was", "back to how it was"]:
        check(f"explicit undo language: {t!r}", intent.asks_for_undo(t))
    for t in ["yes, fix it", "fix it", "ok fix that", "fix them", "yes", "go ahead", "do it", "sure", "yes please", "fix the issues", "yes, delete it",
              "what time is it", "open notepad", "remind me later", "read it", "send it", "keep going", "continue", "no thanks"]:
        check(f"NOT an undo request: {t!r}", not intent.asks_for_undo(t))


    scenario("A3: is_confirmation_like — only ANSWER-shaped utterances are held to the explicit-undo rule")
    for t in ["yes, fix it", "Yes fix it", "ok fix that", "sure, do that", "yeah delete it", "yes stop that", "please fix it", "fix it", "fix them", "fix that",
              "just fix it", "go fix the issues", "fix the problems"]:
        check(f"answer-shaped: {t!r}", intent.is_confirmation_like(t))
    for t in ["oops", "scratch that", "that was a mistake", "i didn't want that", "stop that", "wrong one", "abort that", "i changed my mind",
              "what time is it", "open notepad", "undo", "revert that", "put things back", "reset it", "fix the flask startup error in requirements.txt and rerun the whole test suite",
              "", "   "]:
        check(f"NOT answer-shaped: {t!r}", not intent.is_confirmation_like(t))


# =============================================================================================
# B — an active goal: "Yes, fix it" continues it
# =============================================================================================


async def section_b() -> None:
    scenario("B1: 'Inspect my project.' -> report -> 'Yes, fix it.' continues the SAME goal (never meta.undo)")
    reset()
    r1, _ = await first_turn("Inspect my project.", [call("test.tc_read"), done("requirements.txt is missing flask")])
    gid = r1.data["goal_id"]
    check("turn 1: real findings, read-only, nothing fixed", FLAGS["read"] == 1 and FLAGS["fix"] == 0 and r1.ok)
    check("turn 1: the Session holds the goal as the antecedent for the NEXT turn", SESSION._followup == (gid, "Inspect my project."))
    ATTEMPTS.clear()
    r2, pl = await say("Yes, fix it", [call("test.tc_fix"), done("fixed it")])
    check("turn 2: resolved against the active goal — the fix ran exactly once", FLAGS["fix"] == 1 and r2.ok)
    check("turn 2: the SAME goal continued (id unchanged, still one row)", r2.data["goal_id"] == gid and goal_rows() == 1 and r2.data.get("scope_expanded") is True)
    check("turn 2: the goal is now VERIFIED SUCCEEDED (the fix declared how it reads back)", goals_mod.get(gid).status is goals_mod.GoalStatus.SUCCEEDED, goals_mod.get(gid).status.value)
    check("turn 2: the only skills dispatched were plan.run and the fix — meta.undo was never even routed", "meta.undo" not in ATTEMPTS and ATTEMPTS[0] == "plan.run", str(ATTEMPTS))
    check("turn 2: not treated as a clarification", not cleared(r2))

    for text in ("yes fix it", "Yeah, fix it!", "ok fix that", "fix it", "please fix the issues"):
        reset()
        await first_turn("Inspect my project.", [call("test.tc_read"), done("x")])
        ATTEMPTS.clear()
        r, _ = await say(text, [call("test.tc_fix"), done("fixed")])
        check(f"{text!r} right after a report: continues the goal, fix ran, no undo", FLAGS["fix"] == 1 and "meta.undo" not in ATTEMPTS and not cleared(r), f"{FLAGS['fix']} {ATTEMPTS}")

    scenario("B2: a diagnostic goal ('why isn't my app starting?') is an active goal too")
    reset()
    r1, _ = await first_turn("Why isn't my app starting?", [call("test.tc_read"), done("requirements.txt is missing flask")])
    ATTEMPTS.clear()
    r2, _ = await say("yes, fix it", [call("test.tc_fix"), done("fixed")])
    check("the diagnostic report is resolved as the antecedent, the fix ran, no undo", FLAGS["fix"] == 1 and r2.data["goal_id"] == r1.data["goal_id"] and "meta.undo" not in ATTEMPTS)

    scenario("B3: the expansion is single-use — the goal is spent afterwards")
    reset()
    await first_turn("Inspect my project.", [call("test.tc_read"), done("x")])
    await say("yes, fix it", [call("test.tc_fix"), done("fixed")])
    ATTEMPTS.clear()
    r, _ = await say("yes, fix it", [call("test.tc_fix"), done("again")])
    check("a second 'yes, fix it' has nothing left to refer to -> a question, not a second fix and not an undo", cleared(r) and FLAGS["fix"] == 1 and "meta.undo" not in ATTEMPTS, str(ATTEMPTS))


# =============================================================================================
# C — no antecedent
# =============================================================================================


async def section_c() -> None:
    scenario("C1: bare 'Yes, fix it' with NO context — a question, and NOT meta.undo")
    fresh()
    r, pl = await say("Yes, fix it")
    check("the reply is a clarification (not ok, marked no_antecedent)", not r.ok and cleared(r), str(r.data))
    check("meta.undo was NOT called (it was not even dispatched)", "meta.undo" not in ATTEMPTS and ATTEMPTS == [], str(ATTEMPTS))
    check("the model was never asked either (nothing was planned)", pl.calls == 0)
    check("it asks what the user means and offers the explicit undo phrase", "Nothing is waiting on an answer" in r.speech and "say \"undo\"" in r.speech, r.speech)
    check("no goal was created, nothing became 'the last skill'", goal_rows() == 0 and SESSION.last_skill is None)
    check("the matched-and-refused skill is on the record for debugging", (r.data or {}).get("matched") == "meta.undo")

    scenario("C2: the same for every phrasing the BRAIN sent to meta.undo")
    for text in ("Yes, fix it", "yes fix it", "Yes, fix it!", "yeah fix it", "ok fix that", "fix it", "fix them", "yes, fix them all", "fix that"):
        fresh()
        u = BRAIN.understand(text)
        r, _ = await say(text)
        landed_undo = u.skill == "meta.undo"
        check(f"{text!r}: no skill ran (BRAIN said {u.skill}); meta.undo never dispatched", "meta.undo" not in ATTEMPTS and (not landed_undo or cleared(r)), f"{ATTEMPTS} {r.speech[:60]}")

    scenario("C3: a bare affirmation with nothing pending runs NO skill (it used to reach whatsapp.send)")
    for text in ("yes", "Yes.", "yes please", "go ahead", "ok", "sure", "yeah, go ahead", "okay please"):
        fresh()
        r, pl = await say(text)
        check(f"{text!r}: clarified, nothing dispatched, no model call", cleared(r) and ATTEMPTS == [] and pl.calls == 0, f"{ATTEMPTS} {r.speech[:50]}")
    fresh()
    r, pl = await say("do it")
    check("'do it' is answered by the EXISTING repeat resolver ('nothing recent to repeat') before the guard: nothing dispatched, not ok", ATTEMPTS == [] and not r.ok and pl.calls == 0, r.speech)
    fresh()
    r, _ = await say("go ahead")
    check("...and the clarification does not pretend to know what was meant", "Nothing is waiting" in r.speech and "undo" not in r.speech, r.speech)

    scenario("C4: the antecedent expired — an intervening turn consumed the follow-up slot")
    reset()
    await first_turn("Inspect my project.", [call("test.tc_read"), done("x")])
    r_mid, _ = await say("what time is it")  # any ordinary command in between
    check("(setup) the intervening command really ran and consumed the slot", SESSION._followup is None and "system.time" in ATTEMPTS, str(ATTEMPTS))
    ATTEMPTS.clear()
    r, _ = await say("yes, fix it", [call("test.tc_fix"), done("fixed")])
    check("'yes, fix it' two turns later: a question, NOT an undo and NOT a fix of a stale goal", cleared(r) and "meta.undo" not in ATTEMPTS and FLAGS["fix"] == 0, str(ATTEMPTS))

    scenario("C5: a bare 'yes' right after a report names what it could act on")
    reset()
    await first_turn("Inspect my project.", [call("test.tc_read"), done("x")])
    ATTEMPTS.clear()
    r, _ = await say("yes")
    check("bare 'yes' after a report: no skill, and the question names the goal that was just reported", cleared(r) and ATTEMPTS == [] and "Inspect my project." in r.speech and "fix it" in r.speech, r.speech)
    check("...it did NOT count as a fix authorization (nothing fixed, scope untouched)", FLAGS["fix"] == 0)

    scenario("C6: what would have happened without the fix (the switch proves the cause)")
    fresh()
    CFG.planner.confirmation_guard = False
    await say("Yes, fix it")
    check("confirmation_guard=False: 'Yes, fix it' is dispatched to meta.undo again (the Phase 21 behaviour — denied here by the harness)", ATTEMPTS == ["meta.undo"], str(ATTEMPTS))
    fresh()
    CFG.planner.confirmation_guard = False
    await say("yes")
    check("confirmation_guard=False: a bare 'yes' is dispatched to a skill again", len(ATTEMPTS) == 1, str(ATTEMPTS))
    fresh()


# =============================================================================================
# D — explicit undo still works
# =============================================================================================


async def section_d() -> None:
    scenario("D1: explicit undo language still reaches meta.undo")
    for text in ("undo", "Undo", "undo that", "revert that", "take that back", "put it back the way it was", "cancel what you just did",
                 "yes, undo that", "please undo it", "no go back"):
        fresh()
        u = BRAIN.understand(text)
        r, _ = await say(text)
        check(f"{text!r}: routed to meta.undo (BRAIN said {u.skill}) and the guard let it through", "meta.undo" in ATTEMPTS and not cleared(r), f"{ATTEMPTS}")

    scenario("D2: an explicit undo right after a report is an undo, not a scope expansion")
    # Found by this suite: `derive_expansion` used to accept "undo that" (a modify verb + an anaphor)
    # and widen the just-reported read-only goal to MODIFY instead of reversing anything.
    prior = intent.derive_scope("Inspect my project.")
    for t in ("undo that", "yes, undo it", "revert that", "put it back", "revert them", "take that back", "rollback that"):
        check(f"derive_expansion({t!r}) grants nothing", intent.derive_expansion(t, prior) is None)
    check("...while 'yes, fix it' still expands (Phase 21 unchanged)", intent.derive_expansion("yes, fix it", prior) is not None)
    for text in ("undo that", "yes, undo it", "revert that"):
        reset()
        await first_turn("Inspect my project.", [call("test.tc_read"), done("x")])
        ATTEMPTS.clear()
        r, _ = await say(text)
        check(f"{text!r} with a live follow-up slot: routed to meta.undo, no plan.run resumed, no fix, scope untouched",
              "meta.undo" in ATTEMPTS and "plan.run" not in ATTEMPTS and FLAGS["fix"] == 0 and goal_rows() == 1, str(ATTEMPTS))


# =============================================================================================
# E — routing that must not change
# =============================================================================================


async def section_e() -> None:
    scenario("E1: ordinary utterances are untouched by the guard (it returns None for all of them)")
    corpus = [
        "what time is it", "what's the time", "how much battery is left", "crank it up", "turn it down", "set the volume to 40", "mute",
        "open notepad", "open chrome", "launch spotify", "close notepad", "switch to chrome", "list open windows", "what's on my screen",
        "read my notes", "add a note buy milk", "remember that my sister is priya", "what do you remember", "what's my sister's name",
        "search the web for python tutorials", "find my resume", "read that file", "open it", "read it", "delete it", "close it",
        "every day at 8am tell me my battery", "in 10 minutes tell me the time", "what's scheduled", "delete the lock the pc job",
        "set a timer for 10 minutes", "remind me in an hour", "lock the pc", "snap this to the left", "maximise this", "show me the desktop",
        "what can you do", "what did you just do", "do that again", "same as before", "read more", "yes fix the issues", "yes, open notepad",
        "figure out why my project isn't working", "check the time and the battery", "send a message to rahul", "no", "nope",
        "explain how the volume skill works", "undo", "undo that",
    ]
    touched = []
    for text in corpus:
        u = BRAIN.understand(text)
        out = await SESSION._guard_dangling_confirmation(text, u, None)
        if out is not None:
            touched.append((text, u.skill))
    check(f"{len(corpus)} ordinary utterances: the guard touched none of them", touched == [], str(touched))
    lex = [(t, BRAIN.understand(t).skill) for t in corpus if BRAIN.understand(t).skill == "meta.undo"]
    check("...and every corpus utterance that BRAIN sends to meta.undo says undo itself", all(intent.asks_for_undo(t) for t, _ in lex), str(lex))

    scenario("E1b: ambiguous NON-affirmation phrasings the BRAIN sends to meta.undo keep that routing (measured: 26 of 42 natural phrasings)")
    ambiguous = ["oops", "that was a mistake", "wrong one", "abort that", "scratch that", "reset it", "i didn't want that", "i changed my mind", "stop that",
                 "take back what you just did", "put things back"]
    touched = []
    for text in ambiguous:
        u = BRAIN.understand(text)
        if await SESSION._guard_dangling_confirmation(text, u, None) is not None:
            touched.append((text, u.skill))
    check("the guard does not touch any of them (they are commands, not answers) — behaviour preserved", touched == [], str(touched))
    routed = [t for t in ambiguous if BRAIN.understand(t).skill == "meta.undo"]
    check(f"...{len(routed)} of them really are routed to meta.undo by the BRAIN today, and stay so", len(routed) >= 5, str(routed))

    scenario("E2: every PENDING state still takes priority over the guard")
    fresh()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    SESSION.pending = Pending(kind="confirm", skill="test.tc_fix", future=fut)
    r, _ = await say("yes")
    check("a pending confirmation: 'yes' confirms it (future True, no clarification)", fut.result() is True and not cleared(r) and r.speech == "" and ATTEMPTS == [])
    fresh()
    fut = loop.create_future()
    SESSION.pending = Pending(kind="confirm", skill="test.tc_fix", future=fut)
    r, _ = await say("no")
    check("a pending confirmation: 'no' declines it", fut.result() is False and r.speech == "Cancelled.")
    fresh()
    SESSION.pending = Pending(kind="clarify", options=["test.tc_read", "test.tc_fix"])
    r, _ = await say("yes")
    check("a pending clarify: 'yes' takes the first option (dispatched through the executor)", ATTEMPTS == ["test.tc_read"] and not cleared(r), str(ATTEMPTS))
    fresh()
    SESSION.pending = Pending(kind="slot", skill="test.tc_read", args={}, missing="thing")
    r, _ = await say("yes")
    check("a pending slot: the utterance is the slot's value (not a clarification)", ATTEMPTS == ["test.tc_read"] and not cleared(r), str(ATTEMPTS))

    scenario("E3: the guard sits AFTER every legitimate resolver in Session.handle")
    src = inspect.getsource(SESSION.handle)
    order = [src.index(k) for k in ("_resolve_pending(", "_maybe_expand_scope(", "_maybe_continue_read(", "_maybe_repeat_last_action(", "BRAIN.understand(text)", "_guard_dangling_confirmation(")]
    check("order: pending -> scope expansion -> read more -> repeat -> BRAIN -> guard", order == sorted(order), str(order))
    check("...and before plan routing / context resolution act on the BRAIN's guess", src.index("_guard_dangling_confirmation(") < src.index("_maybe_route_to_plan(") < src.index("_needs_context_resolution("))

    scenario("E4: 'do that again' / 'read more' / 'yes, fix it (with a goal)' are resolved BEFORE the guard, so they keep working")
    reset()
    await say("what time is it")
    ATTEMPTS.clear()
    r, _ = await say("do that again")
    check("'do that again' repeats the last skill (not a clarification)", ATTEMPTS == ["system.time"] and not cleared(r), str(ATTEMPTS))


# =============================================================================================
# F — switch and static invariants
# =============================================================================================


def section_f() -> None:
    scenario("F: static invariants")
    check("default: confirmation_guard is on", CFG.planner.confirmation_guard is True)
    g = inspect.getsource(SESSION._guard_dangling_confirmation)
    check("the guard only ever RETURNS a clarification: it never dispatches (no _run / EXECUTOR call)", "_run(" not in g and "EXECUTOR" not in g)
    check("it reads the user's words and the BRAIN's skill name — nothing from context memory, experience or a result",
          "CONTEXT" not in g and "context_memory" not in g and "episodes" not in g and ".speech" not in g.replace("speech +=", "").replace("speech =", "").replace("speech=speech", ""))
    check("signature: (text, understanding, followup)", list(inspect.signature(SESSION._guard_dangling_confirmation).parameters) == ["text", "understanding", "followup"])
    isrc = inspect.getsource(intent.is_bare_affirmation) + inspect.getsource(intent.asks_for_undo)
    check("the predicates are pure functions of the text (no registry, brain, session or context)", "REGISTRY" not in isrc and "BRAIN" not in isrc and "SESSION" not in isrc)
    imports = {n.module for n in ast.walk(ast.parse(inspect.getsource(intent))) if isinstance(n, ast.ImportFrom) and n.module}
    check("intent.py still imports nothing from permissions or the session", not any(("permissions" in m) or ("session" in m) for m in imports))
    check("meta.undo itself is unchanged: still an L1 'modify' skill reached only through the brain / an explicit call",
          REGISTRY.get("meta.undo").tier == "L1" and REGISTRY.get("meta.undo").action == "modify")
    check("the planner can never call meta.undo through a confirmation: scope alone gates it (modify needs an explicit fix/undo verb)",
          A.MODIFY not in intent.derive_scope("yes").allowed)


async def main() -> int:
    t0 = time.perf_counter()
    _register_fixtures()
    saved = H.lock_down_real_tools(REGISTRY)
    EXECUTOR.run = _recording_run  # type: ignore[method-assign]
    saved_cfg = (CFG.desktop_observer.enabled,)
    try:
        with store.use_temp_db():
            section_a()
            await section_b()
            await section_c()
            await section_d()
            await section_e()
            section_f()
    finally:
        EXECUTOR.run = _ORIG_RUN  # type: ignore[method-assign]
        CFG.permissions.overrides = saved
        (CFG.desktop_observer.enabled,) = saved_cfg
        CFG.planner.confirmation_guard = True
        SESSION.pending = None
    return H.finish("CONFIRMATION ROUTING SCORECARD", time.perf_counter() - t0, min_assertions=150, min_scenarios=14)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
