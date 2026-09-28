"""Phase 11.4 — contextual memory & personalization.

Entirely deterministic — no Ollama, no browser, no real app launches. Two new
modules under test: `friday.intelligence.context_memory` (the bounded recent-
entity window) and `friday.intelligence.context_resolver` (reference
resolution against it, plus temporal-repeat and personalization resolution).
Sections A-O below correspond 1:1 to PLAN.md Phase 11.4's test list; P-R
exercise the real `friday.session.Session`/`friday.skills.plan` wiring using
the same registered-test-skill pattern `scripts/smoke_conversation.py` and
`scripts/smoke_goal_decomposition.py` already use, so nothing here depends on
BRAIN's fuzzy embedding matcher for a pass/fail signal — only on this
phase's own deterministic regex/lookup logic.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import memory as memory_mod  # noqa: E402
from friday import risk, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import context_memory, context_resolver  # noqa: E402
from friday.intelligence import episodes, experience  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402
from friday.permissions import EXECUTOR, PermissionError_  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402
from friday.skills import plan as plan_mod  # noqa: E402

CONTEXT = context_memory.CONTEXT

ECHO_CALLS: list[str] = []
CONSEQUENTIAL_CALLS: list[str] = []


def _register_test_skills() -> None:
    @skill(
        name="test.ctx_echo", tier="L0",
        description="test-only echo used by scripts/smoke_contextual_memory.py",
    )
    def _echo(text: str = "") -> SkillResult:
        ECHO_CALLS.append(text)
        return SkillResult(speech=f"echoed {text}", data={"text": text})

    @skill(
        name="test.ctx_consequential", tier="L3",
        description="test-only consequential action used by scripts/smoke_contextual_memory.py",
    )
    def _consequential(target: str = "") -> SkillResult:
        CONSEQUENTIAL_CALLS.append(target)
        return SkillResult(speech=f"did the consequential thing to {target}")


async def main() -> None:
    store.init()
    REGISTRY.discover()
    _register_test_skills()
    BRAIN.warm()
    overall = True

    # ========================================================================
    # A — single unambiguous reference resolves
    # ========================================================================
    print("\n--- A: a single recent file resolves an unambiguous 'it' ---\n")
    CONTEXT.reset()
    CONTEXT.remember("file", "report.pdf", source="test", turn_id="t1")
    text, result = context_resolver.resolve_pronoun_in_text("read it")
    ok = result is not None and result.resolved and text == "read report.pdf"
    print(f"  {'OK  ' if ok else 'MISS'} 'read it' -> {text!r} (resolved={result.resolved if result else None})")
    overall &= ok

    # ========================================================================
    # B — reference updates: the most recently established entity wins when
    # it was introduced in a *different* turn than the earlier one
    # ========================================================================
    print("\n--- B: reference updates to whatever was most recently opened ---\n")
    CONTEXT.reset()
    CONTEXT.remember("file", "report.pdf", source="test", turn_id="t1")
    CONTEXT.remember("file", "results.xlsx", source="test", turn_id="t2")
    text, result = context_resolver.resolve_pronoun_in_text("read it")
    ok = result is not None and result.resolved and result.referent == "results.xlsx"
    print(f"  {'OK  ' if ok else 'MISS'} after opening report.pdf then results.xlsx, 'it' -> {result.referent if result else None}")
    overall &= ok

    # ========================================================================
    # C — ambiguous reference: two files introduced in the SAME turn must
    # never be silently resolved by recency alone
    # ========================================================================
    print("\n--- C: two files named in one breath -> ambiguity, no guess ---\n")
    CONTEXT.reset()
    CONTEXT.remember("file", "report.pdf", source="test", turn_id="t3")
    CONTEXT.remember("file", "results.xlsx", source="test", turn_id="t3")
    text, result = context_resolver.resolve_pronoun_in_text("delete it")
    ok = (
        result is not None and not result.resolved
        and set(result.candidates) == {"report.pdf", "results.xlsx"}
        and text == "delete it"  # unchanged: never substituted on ambiguity
        and bool(result.clarification)
    )
    print(f"  {'OK  ' if ok else 'MISS'} 'delete it' -> resolved={result.resolved if result else None}, "
          f"candidates={result.candidates if result else None}, asked={result.clarification if result else None!r}")
    overall &= ok

    # ========================================================================
    # D — pronoun/contact resolution: a uniquely established contact
    # ========================================================================
    print("\n--- D: 'him' resolves to the one contact just established ---\n")
    CONTEXT.reset()
    CONTEXT.remember("contact", "Rahul", source="test", turn_id="t4")
    text, result = context_resolver.resolve_pronoun_in_text("send him hello")
    ok = result is not None and result.resolved and result.referent == "Rahul" and text == "send Rahul hello"
    print(f"  {'OK  ' if ok else 'MISS'} 'send him hello' -> {text!r}")
    overall &= ok

    # ========================================================================
    # E — ambiguous pronoun: two contacts named in the same utterance
    # ========================================================================
    print("\n--- E: 'Search Rahul and Priya' then 'send him hello' -> ambiguous ---\n")
    CONTEXT.reset()
    CONTEXT.remember("contact", "Rahul", source="test", turn_id="t5")
    CONTEXT.remember("contact", "Priya", source="test", turn_id="t5")
    text, result = context_resolver.resolve_pronoun_in_text("send him hello")
    ok = result is not None and not result.resolved and set(result.candidates) == {"Rahul", "Priya"}
    print(f"  {'OK  ' if ok else 'MISS'} 'send him hello' -> resolved={result.resolved if result else None}, "
          f"candidates={result.candidates if result else None}")
    overall &= ok

    # ========================================================================
    # F — temporal reference: "again" resolves only when one recent action
    # is clearly eligible; with no history at all, it must not resolve
    # ========================================================================
    print("\n--- F: 'do that again' resolves only when a recent action exists ---\n")
    INTEL.reset()
    ok = context_resolver.is_temporal_repeat("Do that again.")
    ok &= context_resolver.is_temporal_repeat("same as before")
    ok &= not context_resolver.is_temporal_repeat("search for it again")  # not a bare repeat phrasing
    print(f"  {'OK  ' if ok else 'MISS'} is_temporal_repeat() recognizes canonical phrasings, not just any 'again'")
    overall &= ok

    no_history = context_resolver.resolve_temporal_repeat()
    ok = not no_history.resolved and bool(no_history.clarification)
    print(f"  {'OK  ' if ok else 'MISS'} with no recent action recorded -> resolved=False ({no_history.reason})")
    overall &= ok

    INTEL.record_action("apps.open", "app=notepad")
    with_history = context_resolver.resolve_temporal_repeat()
    ok = with_history.resolved and with_history.referent == "apps.open(app=notepad)"
    print(f"  {'OK  ' if ok else 'MISS'} with one recent action -> resolves to it -> {with_history.referent!r}")
    overall &= ok

    # ========================================================================
    # G — no-context reference: nothing recorded at all
    # ========================================================================
    print("\n--- G: 'Open it' with nothing recent -> cannot resolve ---\n")
    CONTEXT.reset()
    text, result = context_resolver.resolve_pronoun_in_text("open it")
    ok = result is not None and not result.resolved and not result.candidates and bool(result.clarification)
    print(f"  {'OK  ' if ok else 'MISS'} 'open it' with empty context -> {result.clarification if result else None!r}")
    overall &= ok

    # ========================================================================
    # H — explicit preference respected
    # ========================================================================
    print("\n--- H: an explicit stored preference is respected ---\n")
    tag_h = f"zzzctx_{uuid.uuid4().hex[:8]}"
    memory_mod.remember(f"My {tag_h} browser is Chrome.", kind="preference", key=f"{tag_h}_browser")
    pref = context_resolver.resolve_personalization(f"my {tag_h} browser")
    ok = pref.resolved and "Chrome" in (pref.referent or "")
    print(f"  {'OK  ' if ok else 'MISS'} stored preference resolved -> {pref.referent!r}")
    overall &= ok

    # ========================================================================
    # I — repeated behavior is NOT a confirmed preference
    # ========================================================================
    print("\n--- I: repeated app usage never becomes an inferred preference ---\n")
    tag_i = f"zzzctx_{uuid.uuid4().hex[:8]}"
    CONTEXT.reset()
    for _ in range(10):
        CONTEXT.remember("app", f"{tag_i} VS Code", source="test")
    # No stored friday.memory preference exists for this query — recall()
    # is monkeypatched to return nothing so the assertion below is exact
    # rather than at the mercy of the real (shared, ever-growing) memory
    # store's embedding similarity happening to cross the threshold on some
    # unrelated preference, which is a real risk here, not a hypothetical
    # one (this is a persistent SQLite store other sections/runs add to —
    # same caveat scripts/smoke_intelligence.py's own comments call out).
    original_recall = memory_mod.recall
    memory_mod.recall = lambda *a, **kw: []
    try:
        unconfirmed = context_resolver.resolve_personalization(f"my usual {tag_i} editor")
    finally:
        memory_mod.recall = original_recall
    ok = not unconfirmed.resolved
    print(f"  {'OK  ' if ok else 'MISS'} 10x repeated use of the same app, no stored preference -> still "
          f"resolved=False ({unconfirmed.reason})")
    overall &= ok

    # ========================================================================
    # J — correction updates the current contextual interpretation
    # ========================================================================
    print("\n--- J: a correction re-anchors the next reference, without rewriting history ---\n")
    CONTEXT.reset()
    CONTEXT.remember("project", "frontend project", source="test", turn_id="t6")
    CONTEXT.remember("project", "old project", source="test", turn_id="t6")
    before = context_resolver.resolve_reference("", entity_type_hint="project")
    ok = not before.resolved  # ambiguous before correction (same turn_id)
    print(f"  {'OK  ' if ok else 'MISS'} before correction: ambiguous -> {before.candidates}")
    overall &= ok

    context_resolver.apply_correction("project", "backend project")
    after = context_resolver.resolve_reference("", entity_type_hint="project")
    ok = after.resolved and after.referent == "backend project"
    print(f"  {'OK  ' if ok else 'MISS'} after 'no, I meant the backend project' -> resolves to {after.referent!r}")
    overall &= ok
    # The original two entities are still on file — a correction never
    # rewrites history, it only adds a new, more-relevant entity.
    ok = len(CONTEXT.recent("project")) == 3
    print(f"  {'OK  ' if ok else 'MISS'} original ambiguous entities are still present, not deleted "
          f"({len(CONTEXT.recent('project'))} project entities on file)")
    overall &= ok

    # Session-level wiring for the same flow: a correction-shaped utterance
    # through the real Session.handle() re-anchors context too.
    CONTEXT.reset()
    CONTEXT.remember("project", "alpha project", source="test", turn_id="t7")
    CONTEXT.remember("project", "beta project", source="test", turn_id="t7")
    await SESSION.handle("no, I meant the gamma project", actor="test")
    via_session = context_resolver.resolve_reference("", entity_type_hint="project")
    ok = via_session.resolved and "gamma project" in (via_session.referent or "")
    print(f"  {'OK  ' if ok else 'MISS'} Session.handle('no, I meant the gamma project') re-anchors context -> "
          f"{via_session.referent!r}")
    overall &= ok

    # ========================================================================
    # K — expiration / bounds: a fixed-size window, not unlimited history
    # ========================================================================
    print("\n--- K: recent context stays bounded, oldest falls out ---\n")
    original_max = CFG.intelligence.context_max_items
    CFG.intelligence.context_max_items = 3
    CONTEXT.reset()
    for i in range(10):
        CONTEXT.remember("file", f"file_{i}.txt", source="test")
    items = CONTEXT.recent()
    ok = len(items) == 3 and [e.display_name for e in items] == ["file_9.txt", "file_8.txt", "file_7.txt"]
    print(f"  {'OK  ' if ok else 'MISS'} 10 remembered, only the 3 newest remain -> "
          f"{[e.display_name for e in items]}")
    overall &= ok
    CFG.intelligence.context_max_items = original_max
    CONTEXT.reset()

    # ========================================================================
    # L — secret redaction: never store a credential-shaped value
    # ========================================================================
    print("\n--- L: secret-looking context is never persisted ---\n")
    CONTEXT.reset()
    dropped = CONTEXT.remember("preference", "password: hunter2")
    ok = dropped is None and not CONTEXT.recent()
    print(f"  {'OK  ' if ok else 'MISS'} 'password: hunter2' as a display name -> not remembered ({dropped})")
    overall &= ok

    entity = CONTEXT.remember("file", "notes.txt", raw={"api_key": "sk-super-secret", "size": "120"})
    ok = entity is not None and (entity.raw is None or "api_key" not in entity.raw) and "size" in (entity.raw or {})
    print(f"  {'OK  ' if ok else 'MISS'} secret-named raw metadata key dropped, benign key kept -> {entity.raw if entity else None}")
    overall &= ok

    # ========================================================================
    # M — contextual memory and episodic experience stay separate
    # ========================================================================
    print("\n--- M: contextual memory never reaches into episodic experience ---\n")
    CONTEXT.reset()
    CONTEXT.remember("file", "isolated.txt", source="test")

    original_similar = episodes.retrieve_similar
    original_failures = episodes.retrieve_similar_failures
    original_experience = experience.retrieve_relevant_experience

    def boom(*a, **kw):
        raise RuntimeError("episodic experience must never be touched by context_resolver")

    episodes.retrieve_similar = boom
    episodes.retrieve_similar_failures = boom
    experience.retrieve_relevant_experience = boom
    try:
        text, result = context_resolver.resolve_pronoun_in_text("read it")
        ok = result is not None and result.resolved and result.referent == "isolated.txt"
        print(f"  {'OK  ' if ok else 'MISS'} reference resolution works with episodic-experience functions "
              f"forced to raise -> proves it never calls them (referent={result.referent if result else None})")
        overall &= ok
    finally:
        episodes.retrieve_similar = original_similar
        episodes.retrieve_similar_failures = original_failures
        experience.retrieve_relevant_experience = original_experience

    # ========================================================================
    # N — confirmation boundary: contextual resolution never bypasses it
    # ========================================================================
    print("\n--- N: a resolved consequential reference still asks for confirmation ---\n")
    CONTEXT.reset()
    CONTEXT.remember("contact", "Rahul", source="test", turn_id="t8")
    resolved = context_resolver.resolve_reference(
        "send him hello", entity_type_hint="contact", consequential=risk.is_consequential("send him hello"),
    )
    ok = resolved.resolved and resolved.referent == "Rahul"
    print(f"  {'OK  ' if ok else 'MISS'} context resolves the consequential request's target -> {resolved.referent!r}")
    overall &= ok

    confirm_calls: list[str] = []

    async def approve(skill_obj, args, preview: str) -> bool:
        confirm_calls.append(preview)
        return True

    EXECUTOR.set_confirm_handler(approve)
    result = await EXECUTOR.run("test.ctx_consequential", {"target": resolved.referent}, actor="text")
    ok = result.ok and len(confirm_calls) == 1
    print(f"  {'OK  ' if ok else 'MISS'} running the L3 skill with the resolved referent still paused for "
          f"confirmation -> {result.speech!r} (confirm_calls={len(confirm_calls)})")
    overall &= ok

    async def decline(skill_obj, args, preview: str) -> bool:
        return False

    EXECUTOR.set_confirm_handler(decline)
    CONSEQUENTIAL_CALLS.clear()
    result = await EXECUTOR.run("test.ctx_consequential", {"target": resolved.referent}, actor="text")
    ok = (not result.ok) and result.data.get("confirmation_declined") is True and not CONSEQUENTIAL_CALLS
    print(f"  {'OK  ' if ok else 'MISS'} declining still blocks it, resolved referent or not -> {result.speech!r}")
    overall &= ok
    EXECUTOR.set_confirm_handler(SESSION._confirm)  # restore the real handler for later sections

    # ========================================================================
    # O — permission boundary: contextual resolution never bypasses a denial
    # ========================================================================
    print("\n--- O: contextual resolution cannot bypass a permission denial ---\n")
    CONSEQUENTIAL_CALLS.clear()
    denied = False
    try:
        await EXECUTOR.run("test.ctx_consequential", {"target": "Rahul"}, actor="scheduler")
    except PermissionError_:
        denied = True
    ok = denied and not CONSEQUENTIAL_CALLS
    print(f"  {'OK  ' if ok else 'MISS'} an unattended actor calling an L3 skill is still denied "
          f"(the skill body never ran) -> denied={denied}")
    overall &= ok

    # ========================================================================
    # P — Session wiring: pronoun substitution, ambiguity -> Pending ->
    # clarification, and "do that again" re-dispatch, through the real
    # friday.session.Session (not a stand-in)
    # ========================================================================
    print("\n--- P: Session wiring (substitution, clarification, 'again') ---\n")

    CONTEXT.reset()
    CONTEXT.remember("file", "report.pdf", source="test", turn_id="p1")
    text, result = SESSION._resolve_context_reference("read it")
    ok = result is not None and result.resolved and text == "read report.pdf"
    print(f"  {'OK  ' if ok else 'MISS'} Session._resolve_context_reference('read it') -> {text!r}")
    overall &= ok

    CONTEXT.reset()
    CONTEXT.remember("file", "report.pdf", source="test", turn_id="p2")
    CONTEXT.remember("file", "results.xlsx", source="test", turn_id="p2")
    text, result = SESSION._resolve_context_reference("delete it")
    ok = result is not None and not result.resolved
    print(f"  {'OK  ' if ok else 'MISS'} Session._resolve_context_reference('delete it') -> ambiguous, not guessed")
    overall &= ok

    SESSION.pending = None
    clarify_reply = await SESSION._ask_context_clarification(result, "delete it", actor="test")
    ok = (
        not clarify_reply.ok and SESSION.pending is not None
        and SESSION.pending.kind == "context_clarify"
        and set(SESSION.pending.options) == {"report.pdf", "results.xlsx"}
    )
    print(f"  {'OK  ' if ok else 'MISS'} ambiguity sets a context_clarify Pending -> {clarify_reply.speech!r}")
    overall &= ok

    # Whichever real skill "delete results.xlsx" happens to route to next
    # (BRAIN's own routing — outside this phase's scope) might itself need
    # confirmation; a fast auto-decline here keeps this test from blocking
    # on a real 60s confirmation timeout while still exercising the full
    # context_clarify -> substitute -> BRAIN.understand -> _act path.
    async def fast_decline(skill_obj, args, preview: str) -> bool:
        return False

    EXECUTOR.set_confirm_handler(fast_decline)
    follow_up = await SESSION.handle("results.xlsx", actor="test")
    # Phase 13.0: the stale "context_clarify" Pending must always be
    # consumed (never answered twice), but the *next* state legitimately
    # depends on which real skill "delete results.xlsx" now routes to
    # (still outside this phase's scope, per the comment above) — since
    # PLAN.md Phase 13.0's entity-compatible routing (friday.brain.engine
    # .route_with_resolved_entity) can itself land on ASK_SLOT for a
    # *different* missing argument (e.g. a bare filename isn't path-shaped
    # for files.reveal) rather than always completing in one shot. Either
    # outcome is a valid terminal state; only a lingering *context_clarify*
    # pending (the same ambiguity asked twice) would be a real bug.
    ok = (
        (SESSION.pending is None or SESSION.pending.kind != "context_clarify")
        and isinstance(follow_up, SkillResult)
    )
    print(f"  {'OK  ' if ok else 'MISS'} answering the clarification consumes the Pending -> {follow_up.speech!r}")
    overall &= ok
    # Isolate the next sub-scenario from whatever pending state this one
    # left behind (e.g. a genuine ASK_SLOT "which path?") — each
    # independent block in this test should start clean.
    SESSION.pending = None

    # "do that again" — set up a known last action via the real Session._run
    # path (through EXECUTOR, exactly like a normal command would), then
    # confirm a bare "again" re-dispatches the same skill+args.
    EXECUTOR.set_confirm_handler(SESSION._confirm)
    ECHO_CALLS.clear()
    await SESSION._run("test.ctx_echo", {"text": "hello"}, actor="test")
    ok = ECHO_CALLS == ["hello"]
    print(f"  {'OK  ' if ok else 'MISS'} baseline direct run -> ECHO_CALLS={ECHO_CALLS}")
    overall &= ok

    again_result = await SESSION.handle("do that again", actor="test")
    ok = ECHO_CALLS == ["hello", "hello"] and again_result.ok
    print(f"  {'OK  ' if ok else 'MISS'} 'do that again' re-dispatches the exact same call -> ECHO_CALLS={ECHO_CALLS}")
    overall &= ok

    # A brand-new Session-like state (simulated by clearing last_skill) has
    # nothing eligible to repeat -> must explain, not silently no-op or error.
    saved_skill, saved_args = SESSION.last_skill, SESSION.last_args
    SESSION.last_skill = None
    INTEL.reset()
    nothing_result = await SESSION.handle("do that again", actor="test")
    ok = not nothing_result.ok
    print(f"  {'OK  ' if ok else 'MISS'} 'again' with nothing eligible -> {nothing_result.speech!r}")
    overall &= ok
    SESSION.last_skill, SESSION.last_args = saved_skill, saved_args

    # ========================================================================
    # Q — plan.run wiring: executed steps become future context; the
    # planner prompt itself gets a bounded recent-context block
    # ========================================================================
    print("\n--- Q: plan.run records entities from its own steps and reads them back ---\n")
    CONTEXT.reset()
    goal_id = f"zzzctx-goal-{uuid.uuid4().hex[:8]}"
    observations = [
        Observation(PlanStep("files.read", {"path": "C:/tmp/quarterly_report.pdf"}), True, "read it",
                    data={"path": "C:/tmp/quarterly_report.pdf"}),
        Observation(PlanStep("apps.open", {"app": "notepad"}), True, "opened it", data={"app": "notepad"}),
    ]
    plan_mod._remember_plan_entities(goal_id, observations)
    names = {e.display_name for e in CONTEXT.recent()}
    ok = "quarterly_report.pdf" in names and "notepad" in names
    print(f"  {'OK  ' if ok else 'MISS'} both successful steps recorded as context entities -> {names}")
    overall &= ok

    block = plan_mod._append_context_memory("")
    ok = "quarterly_report.pdf" in block or "notepad" in block
    print(f"  {'OK  ' if ok else 'MISS'} bounded context block for the planner prompt -> {block!r}")
    overall &= ok

    bounded = plan_mod._append_context_memory("x" * 50)
    ok = len(bounded) <= 50 + CFG.intelligence.context_block_max_chars + 1
    print(f"  {'OK  ' if ok else 'MISS'} context block stays bounded regardless of prior context length "
          f"(len={len(bounded)})")
    overall &= ok

    # Toggle off: same switch precedent as working memory/experience.
    CFG.intelligence.context_memory_enabled = False
    off_block = plan_mod._append_context_memory("")
    ok = off_block == ""
    print(f"  {'OK  ' if ok else 'MISS'} context_memory_enabled=False -> no context block attached")
    overall &= ok
    CFG.intelligence.context_memory_enabled = True

    # ========================================================================
    # R — realistic narrative (brief §16): open, read, open again, read,
    # open two, then an ambiguous destructive reference must ask, never guess
    # ========================================================================
    print("\n--- R: narrative — open/read/open/read/open-two/ambiguous-delete ---\n")
    CONTEXT.reset()

    CONTEXT.remember("file", "report.pdf", source="narrative:open", turn_id="n1")
    _, r1 = context_resolver.resolve_pronoun_in_text("read it")
    ok = r1 is not None and r1.resolved and r1.referent == "report.pdf"
    print(f"  {'OK  ' if ok else 'MISS'} open report.pdf -> 'read it' resolves to {r1.referent if r1 else None}")
    overall &= ok

    CONTEXT.remember("file", "results.xlsx", source="narrative:open", turn_id="n2")
    _, r2 = context_resolver.resolve_pronoun_in_text("read it")
    ok = r2 is not None and r2.resolved and r2.referent == "results.xlsx"
    print(f"  {'OK  ' if ok else 'MISS'} then open results.xlsx -> 'read it' now resolves to {r2.referent if r2 else None}")
    overall &= ok

    CONTEXT.remember("file", "report.pdf", source="narrative:open_both", turn_id="n3")
    CONTEXT.remember("file", "results.xlsx", source="narrative:open_both", turn_id="n3")
    _, r3 = context_resolver.resolve_pronoun_in_text("delete it")
    ok = r3 is not None and not r3.resolved and set(r3.candidates) == {"report.pdf", "results.xlsx"}
    print(f"  {'OK  ' if ok else 'MISS'} open both together -> 'delete it' asks instead of guessing "
          f"-> {r3.clarification if r3 else None!r}")
    overall &= ok
    print("  (nothing was deleted — this test never calls a destructive skill)")

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
