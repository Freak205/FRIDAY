"""Conversation session: the loop from utterance to spoken result.

Sits between the interfaces (voice, CLI, tray) and the brain/executor. Owns the
short-lived conversational state that makes follow-ups work — a pending
clarification, a missing slot, or a confirmation awaiting your answer.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from friday.brain import BRAIN
from friday.brain.engine import Action, Understanding
from friday.bus import BUS
from friday.log import get
from friday.permissions import EXECUTOR, PermissionError_
from friday.registry import REGISTRY, Skill, SkillResult

log = get(__name__)

# Phase 11.4: best-effort entity-type hint for a correction's contextual
# target ("no, I meant the backend project" -> "project"). Checked in
# order; the first match wins. "project" is also the fallback when a
# correction names no entity noun at all, matching this codebase's own
# existing correction examples/tests ("no, I meant my college project").
_CORRECTION_ENTITY_HINTS: tuple[tuple[str, re.Pattern], ...] = (
    ("file", re.compile(r"\b(file|document|spreadsheet|pdf)\b", re.IGNORECASE)),
    ("app", re.compile(r"\b(app|application|program)\b", re.IGNORECASE)),
    ("contact", re.compile(r"\b(contact|chat)\b", re.IGNORECASE)),
    ("project", re.compile(r"\bproject\b", re.IGNORECASE)),
)

# Phase 14.0 — compound-goal routing. Connectors that plausibly join two
# distinct stages of one utterance, checked longest/most-explicit first.
# Same vocabulary friday.intelligence.goals._DECOMPOSABLE_MARKERS already
# uses to decide whether plan.run's own upfront decomposition is worth
# trying — reused (not duplicated) for the analogous "is a second
# BRAIN.understand call worth paying for" question below.
_COMPOUND_SPLIT = re.compile(
    r"\s+(?:and then|then|after that|once you|and also|and)\s+", re.IGNORECASE,
)

_CORRECTION_LEAD = re.compile(
    r"^\s*(no,?\s+)?(i meant|actually i meant|you misunderstood|"
    r"that's not what i (meant|asked)|thats not what i (meant|asked))\s*[:,]?\s*",
    re.IGNORECASE,
)

_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "do it", "go ahead",
        "confirm", "affirmative", "please do", "y"}
_NO = {"no", "nope", "nah", "cancel", "stop", "don't", "dont", "never mind",
       "nevermind", "abort", "n"}


# Phase 28.0: what, said ON ITS OWN, means "stop the goal that is running right now". Deliberately a
# short exact list, not a pattern: "stop the music" / "cancel my subscription" are ordinary commands
# and must keep routing as such. It only ever applies while a goal is running or a confirmation is
# waiting (see `Session._stop_applies`) -- idle, these words route exactly as they always did.
_STOP_PHRASES = frozenset({
    "stop", "stop it", "stop that", "stop this", "stop now", "stop everything", "stop all",
    "stop the task", "stop the goal", "stop the plan", "stop doing that", "stop doing this",
    "stop what you are doing", "stop what youre doing", "stop whatever you are doing",
    "cancel", "cancel it", "cancel that", "cancel this", "cancel the task", "cancel the goal",
    "cancel the plan", "cancel everything", "abort", "abort it", "abort the task", "abort the goal",
    "halt", "emergency stop",
})
_STOP_FILLER = frozenset({"hey", "friday", "please", "now"})


def is_stop_phrase(text: str) -> bool:
    words = re.sub(r"[^a-z ]", "", text.lower().replace("\u2019", "").replace("'", "")).split()
    while words and words[0] in _STOP_FILLER:
        words.pop(0)
    while words and words[-1] in _STOP_FILLER:
        words.pop()
    return " ".join(words) in _STOP_PHRASES


@dataclass(slots=True)
class Pending:
    """Whatever FRIDAY is waiting on you for."""

    kind: str                     # confirm | slot | clarify | context_clarify | goal_clarify
    skill: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    missing: str | None = None
    options: list[str] = field(default_factory=list)
    future: asyncio.Future[bool] | None = None
    # Which interface asked — lets a voice-only listener (see
    # friday.voice.conversation.ConversationLoop) react to just its own
    # confirmations instead of every actor's. Text/CLI ignore this field.
    actor: str = "text"
    # Phase 11.4: kind="context_clarify" — the literal reference span
    # matched in the original utterance ("it", "him", "the file", ...) and
    # the utterance itself, so whichever candidate the user picks can be
    # substituted back in and re-understood (see Session._resolve_pending).
    context_span: str = ""
    context_template: str = ""
    # Phase 13.0: the resolved entity's type ("file", "contact", ...), so
    # the clarification answer can be routed via the same entity-compatible
    # path as a first-pass resolution — see
    # friday.brain.engine.route_with_resolved_entity.
    context_entity_type: str = ""
    # Phase 17.0: kind="goal_clarify" — an open-ended `plan.run` goal paused
    # at `GoalStatus.BLOCKED` mid-discovery, asking a whole-goal
    # interpretation question the LLM itself raised (never for a pronoun/
    # entity reference — that stays on context_clarify above). `goal_id`
    # lets the answer resume the SAME `Goal` row via `plan.run`'s
    # `resume_goal_id`/`clarification_answer` params, never a second goal.
    goal_id: str | None = None
    question: str = ""


class Session:
    def __init__(self) -> None:
        self.pending: Pending | None = None
        self.last_utterance: str = ""
        self.last_skill: str | None = None
        # Phase 11.4: the args of the last skill actually run — lets "do
        # that again" re-dispatch the exact same call through the normal
        # EXECUTOR path (still fully subject to permission/confirmation;
        # see _maybe_repeat_last_action) without needing BRAIN to re-parse
        # a sentence that has no slot content of its own.
        self.last_args: dict[str, Any] = {}
        # Phase 21.0: the `data` of that same call — lets "read more" advance a page
        # cursor the tool itself reported (`next_offset`), see `_maybe_continue_read`.
        self.last_data: dict[str, Any] = {}
        # Phase 21.0: (goal_id, original goal text) of the report-only goal that
        # finished on the PREVIOUS turn, so an explicit "yes, fix it" can widen that same
        # goal's scope (`_maybe_expand_scope`). Single-use: consumed by the next turn
        # whether or not that turn turns out to be an expansion.
        self._followup: tuple[str, str] | None = None
        self._confirm_actor: str = "text"
        # One id per handle() call, threaded through to context_memory so
        # entities remembered from the same utterance/goal can be told
        # apart from ones remembered in an earlier turn — see
        # ContextEntity.turn_id.
        self._turn_id: str = ""
        EXECUTOR.set_confirm_handler(self._confirm)

        # Phase 10: passive self-state tracking (see friday.intelligence.self_state
        # — a BUS listener, not a second state machine) wired from here so it's
        # active regardless of entry point (daemon, CLI, tests), same pattern as
        # the confirm handler above.
        from friday.intelligence.self_state import SELF_STATE

        SELF_STATE.wire()

        # Phase 11.5: proactive situational intelligence (see
        # friday.intelligence.proactive — a bounded BUS listener, not a new
        # FSM/scheduler) wired here for the same reason as SELF_STATE above.
        from friday.intelligence.proactive import PROACTIVE

        PROACTIVE.wire()

    # -- confirmation ---------------------------------------------------------

    async def _confirm(self, skill: Skill, args: dict[str, Any], preview: str) -> bool:
        """Park a confirmation and wait for the next utterance to resolve it."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self.pending = Pending(
            kind="confirm", skill=skill.name, args=args, future=future,
            actor=self._confirm_actor,
        )
        await BUS.publish(
            "session.awaiting_confirm",
            skill=skill.name, tier=skill.tier, preview=preview,
            speech=f"{preview}. Should I go ahead?",
            actor=self._confirm_actor,
        )
        try:
            return await asyncio.wait_for(future, timeout=60)
        except asyncio.TimeoutError:
            log.info("confirmation timed out for %s", skill.name)
            return False
        finally:
            if self.pending and self.pending.future is future:
                self.pending = None

    # -- main entry point -----------------------------------------------------

    # -- stop (Phase 28.0) ----------------------------------------------------

    def _stop_applies(self) -> bool:
        """A bare "stop" means this only while there is something to stop."""
        try:
            from friday.intelligence.state import INTEL

            if INTEL.goal_running:
                return True
        except Exception:
            log.exception("stop check failed (non-fatal)")
        p = self.pending
        return p is not None and p.kind == "confirm" and p.future is not None and not p.future.done()

    async def stop(self, *, source: str = "user") -> SkillResult:
        """The one stop entry point -- the GUI button, the global hotkey, the daemon's /cancel and a
        typed/spoken "stop" all land here (Phase 28.0). It arms the single `INTEL` cancel flag the
        orchestrator already polls (no second mechanism) and declines a confirmation that is still
        waiting, so a stopped goal can't be revived by a late "yes". It never executes anything
        itself and never touches permission checks. Idempotent: a repeat, or a stop with nothing
        running, is a harmless no-op that says so.

        What it does NOT do: reverse anything already done, or interrupt a step that runs in a
        worker thread -- the goal's final result reports both, truthfully (see
        `friday.orchestrator._cancelled_summary`)."""
        from friday import audit
        from friday.intelligence.state import INTEL

        running = INTEL.request_cancel()  # first and unconditional: the part that must not fail
        declined = False
        pending = self.pending
        if pending is not None and pending.kind == "confirm" and pending.future is not None and not pending.future.done():
            pending.future.set_result(False)
            self.pending = None
            declined = True

        if running or declined:
            speech = "Stopping. No further steps will start."
            if declined:
                speech += " I declined the confirmation that was waiting."
        else:
            speech = "There's nothing running to stop."
        try:
            audit_id = audit.record(
                actor=source, skill="friday.stop", tier="L0",
                args={"goal_running": running, "declined_confirmation": declined}, decision="auto",
            )
            audit.complete(audit_id, ok=True, result=speech)
        except Exception:
            log.exception("stop audit failed (non-fatal)")
        await BUS.publish(
            "session.stop_requested", source=source, goal_running=running, declined_confirmation=declined,
        )
        log.info("stop requested via %s: goal_running=%s declined_confirmation=%s", source, running, declined)
        return SkillResult(
            speech=speech, ok=True,
            data={"stopped": running or declined, "goal_running": running, "declined_confirmation": declined},
        )

    async def handle(self, utterance: str, *, actor: str = "text") -> SkillResult:
        text = (utterance or "").strip()
        if not text:
            return SkillResult(speech="I didn't catch that.", ok=False)

        # Phase 28.0: checked before anything else touches per-turn state (the follow-up slot,
        # the recorded request), so a stop never clobbers the goal it is stopping.
        if is_stop_phrase(text) and self._stop_applies():
            await BUS.publish("session.heard", text=text, actor=actor)
            return await self.stop(source=actor)

        self._turn_id = uuid.uuid4().hex[:12]
        followup, self._followup = self._followup, None
        await BUS.publish("session.heard", text=text, actor=actor)
        self._record_correction_if_any(text)

        try:
            from friday.intelligence.state import INTEL

            INTEL.record_turn(actor, text)
        except Exception:
            log.exception("intelligence state update failed (non-fatal)")

        # A pending question takes priority over fresh intent matching.
        if self.pending is not None:
            resolved = await self._resolve_pending(text, actor=actor)
            if resolved is not None:
                return resolved

        # Phase 21.0: the user's own explicit follow-up to a report-only goal ("yes, fix
        # it") widens THAT goal's scope; "read more" / "next page" advances the last
        # read's cursor. Both still dispatch through the normal EXECUTOR path —
        # permission and confirmation are untouched — and both only ever act on the
        # user's own words in this utterance.
        expanded = await self._maybe_expand_scope(text, followup, actor=actor)
        if expanded is not None:
            return expanded
        more = await self._maybe_continue_read(text, actor=actor)
        if more is not None:
            return more

        # Phase 11.4: "do that again" / "same as before" is dispatched
        # directly — there is no slot for BRAIN to extract, just "run the
        # last thing again" (still through the normal EXECUTOR path, so
        # permission/confirmation apply exactly as they did the first time).
        again = await self._maybe_repeat_last_action(text, actor=actor)
        if again is not None:
            return again

        # Phase 11.4: ordinary understanding runs first, completely
        # unchanged — "what time is it", "crank it up", "click where it
        # says next" all contain a bare "it" that means nothing without
        # this sentence, and must keep matching exactly as they did before
        # this phase (brief §12: simple commands stay fast, never
        # second-guessed). Contextual reference resolution only engages as
        # a *recovery* step, when plain understanding didn't already land
        # on a confident, complete action — see _needs_context_resolution.
        understanding = BRAIN.understand(text)

        # Phase 22.0: an affirmation with nothing to affirm ("yes", "yes, fix it" with no
        # pending question and no finding just reported) must not be handed to whichever
        # skill's example phrasing it happens to sit nearest — measured: meta.undo for
        # "yes, fix it", whatsapp.send for "yes" / "go ahead". Ask instead.
        guarded = await self._guard_dangling_confirmation(text, understanding, followup)
        if guarded is not None:
            return guarded

        # Phase 14.0: a free-form compound goal can confidently ACT- or
        # ASK_SLOT-match a single skill for just its first clause
        # (documented gap, PLAN.md Phase 13.0 §7) — see
        # _maybe_route_to_plan. Checked before context resolution: a
        # rerouted plan.run understanding has nothing for pronoun
        # resolution to do, and only ever replaces a decision for some
        # *other* skill, never plan.run itself.
        if understanding.action in (Action.ACT, Action.ASK_SLOT) and understanding.skill != "plan.run":
            routed = self._maybe_route_to_plan(text, understanding)
            if routed is not None:
                understanding = routed

        # Phase 17.0: a vague/open-ended utterance ("figure out why my
        # project isn't working") typically has no skill example phrasing
        # close enough to clear even BRAIN's clarify_threshold — before this
        # phase that was a dead end (`Action.UNKNOWN`'s canned "I don't know
        # how to do that yet"). `_maybe_route_to_open_ended` is the bridge:
        # only engages for a genuinely UNKNOWN understanding, and only when
        # `friday.intelligence.discovery.classify_mode` recognizes the
        # utterance as diagnostic/investigative/information-seeking/open-
        # ended — an ordinary out-of-domain utterance ("write me a poem")
        # still correctly falls through to UNKNOWN.
        if understanding.action is Action.UNKNOWN:
            routed = self._maybe_route_to_open_ended(text, understanding)
            if routed is not None:
                understanding = routed

        if self._needs_context_resolution(understanding, text):
            substituted, ctx_result = self._resolve_context_reference(text)
            if ctx_result is not None:
                if not ctx_result.resolved:
                    return await self._ask_context_clarification(ctx_result, text, actor=actor)
                rerouted = self._reroute_with_context(
                    text, ctx_result.matched_span, ctx_result.entity_type, ctx_result.referent,
                )
                if rerouted is not None:
                    understanding = rerouted
                    text = substituted
                elif understanding.action is not Action.ACT:
                    # No compatible skill was found even via the resolved
                    # entity's own type — fall back to the substituted
                    # text only when plain understanding hadn't already
                    # landed on something (brief §4: a resolved reference
                    # must never destroy a working ACT decision). This is
                    # strictly the pre-Phase-13.0 fallback, kept only for
                    # entity types with no canonical phrase mapping.
                    text = substituted
                    understanding = BRAIN.understand(text)

        self.last_utterance = text
        await BUS.publish(
            "brain.understood",
            action=understanding.action.value,
            skill=understanding.skill,
            score=round(understanding.score, 3),
            args=understanding.args,
        )
        return await self._act(understanding, actor=actor)

    # -- corrections (Phase 10) ------------------------------------------------

    def _record_correction_if_any(self, text: str) -> None:
        """"No, I meant my college project" style corrections, recorded as
        structured data against the most recent goal — bookkeeping only.

        This never intercepts or short-circuits normal handling: the same
        utterance still goes on to `BRAIN.understand` below exactly as
        before, so a correction that's also actionable ("no, my college
        project") still does something. Recording is best-effort — any
        failure here must never break a normal conversation turn.
        """
        from friday.intelligence import goals as goals_mod

        try:
            if not goals_mod.looks_like_correction(text):
                return
            goal = goals_mod.most_recent_active()
            from friday.intelligence import corrections

            corrections.record(
                text,
                goal_id=goal.id if goal else None,
                original_interpretation=goal.objective if goal else "",
                context=self.last_utterance,
            )
            self._apply_context_correction(text)
        except Exception:
            log.exception("correction recording failed (non-fatal)")

    def _apply_context_correction(self, text: str) -> None:
        """Phase 11.4: steer *future* reference resolution toward what the
        user just corrected to — e.g. "no, I meant the backend project"
        makes "the backend project" the most-recent "project" entity, so
        the next "open it" resolves there instead of whatever was ambiguous
        before. Best-effort and additive only: never rewrites the
        correction/goal rows this method already wrote above, and a failure
        here must never break correction recording itself (see the caller's
        try/except).
        """
        from friday.brain.extract import _strip_to_object
        from friday.intelligence import context_resolver

        entity_type = "project"
        for candidate_type, pattern in _CORRECTION_ENTITY_HINTS:
            if pattern.search(text):
                entity_type = candidate_type
                break

        remainder = _CORRECTION_LEAD.sub("", text).strip()
        target = _strip_to_object(remainder) if remainder else ""
        if target:
            context_resolver.apply_correction(entity_type, target)

    # -- pending resolution ---------------------------------------------------

    async def _resolve_pending(
        self, text: str, *, actor: str
    ) -> SkillResult | None:
        pending = self.pending
        if pending is None:
            return None

        low = text.lower().strip(" .!?")

        if pending.kind == "confirm" and pending.future is not None:
            if low in _YES:
                pending.future.set_result(True)
                # The still-suspended EXECUTOR.run call (parked in
                # Session._confirm) wakes up and produces + speaks the real
                # result; returning None here would fall through to
                # BRAIN.understand("yes") and hand back a second, unrelated
                # "I don't understand" reply for whichever interface asked.
                return SkillResult(speech="", ok=True)
            if low in _NO:
                pending.future.set_result(False)
                self.pending = None
                return SkillResult(speech="Cancelled.", ok=False)
            # Anything else: treat as a new command, drop the confirmation.
            pending.future.set_result(False)
            self.pending = None
            return None

        if pending.kind == "slot" and pending.skill and pending.missing:
            args = dict(pending.args)
            args[pending.missing] = text
            self.pending = None
            return await self._run(pending.skill, args, actor=actor)

        if pending.kind == "clarify":
            # "the first one" / "yes" -> take the top option.
            choice = None
            if low in _YES or "first" in low:
                choice = pending.options[0] if pending.options else None
            else:
                for option in pending.options:
                    skill = REGISTRY.get(option)
                    if skill and any(w in low for w in skill.name.split(".")):
                        choice = option
                        break
            self.pending = None
            if choice:
                return await self._run(choice, {}, actor=actor)
            return None  # fall through and re-parse as a new command

        if pending.kind == "context_clarify":
            from friday.intelligence import context_resolver

            choice = context_resolver.match_choice(text, pending.options)
            template, span = pending.context_template, pending.context_span
            entity_type = pending.context_entity_type
            self.pending = None
            if choice is None:
                return None  # didn't match an offered candidate; re-parse as a new command
            referent = (
                context_resolver.resolve_display_name(entity_type, choice)
                if entity_type else choice
            )
            understanding = self._reroute_with_context(template, span, entity_type, referent)
            resolved_text = context_resolver.substitute(template, span, referent)
            if understanding is None:
                understanding = BRAIN.understand(resolved_text)
            self.last_utterance = resolved_text
            return await self._act(understanding, actor=actor)

        if pending.kind == "goal_clarify":
            # Phase 17.0: resumes the SAME Goal row via plan.run's
            # resume_goal_id/clarification_answer — never a second/unrelated
            # goal. The original goal text is recovered from the Goal row
            # itself (its `objective`), not from this utterance, since the
            # answer alone ("the backend one") isn't a goal on its own.
            from friday.intelligence import goals as goals_mod

            goal_id = pending.goal_id
            self.pending = None
            record = goals_mod.get(goal_id) if goal_id else None
            original_goal = record.objective if record else text
            return await self._run(
                "plan.run",
                {"goal": original_goal, "resume_goal_id": goal_id or "", "clarification_answer": text},
                actor=actor,
            )

        return None

    # -- Phase 21.0: explicit continuations --------------------------------------------

    async def _maybe_expand_scope(
        self, text: str, followup: tuple[str, str] | None, *, actor: str,
    ) -> SkillResult | None:
        """"Yes, fix it" right after a report-only goal: continue the SAME goal with a
        wider scope. Engages only when (a) the previous turn finished a plan.run goal
        whose recorded scope was read-only, and (b) `friday.intent.derive_expansion`
        accepts THIS utterance — a short, anaphoric directive naming a consequential
        verb. Anything else (a bare "yes", a question, an unrelated command) returns
        None and is handled as a normal new utterance. Never widens anything itself:
        `plan.run` re-derives the scope from these same words, and every action it then
        takes still passes alignment, permission and confirmation."""
        from friday.config import CFG

        if followup is None or not CFG.planner.scope_expansion:
            return None
        try:
            from friday import intent
            from friday.intelligence import goals as goals_mod

            goal_id, objective = followup
            record = goals_mod.get(goal_id)
            if record is None:
                return None
            scope = intent.GoalScope.from_dict(record.contract.action_scope) if record.contract.action_scope else None
            if intent.derive_expansion(text, scope) is None:
                return None
        except Exception:
            log.exception("scope expansion check failed (non-fatal)")
            return None
        await BUS.publish("session.scope_expansion", goal_id=goal_id, text=text, actor=actor)
        return await self._run(
            "plan.run", {"goal": objective, "resume_goal_id": goal_id, "scope_expansion": text}, actor=actor,
        )

    async def _guard_dangling_confirmation(
        self, text: str, understanding: Understanding, followup: tuple[str, str] | None,
    ) -> SkillResult | None:
        """A confirmation that refers to nothing gets a clarifying question, never a skill.

        By the time this runs, every legitimate antecedent has had its chance: a pending
        confirmation/slot/clarification (`_resolve_pending`), and the previous turn's
        report-only goal (`_maybe_expand_scope`, which resolves "yes, fix it" against that
        goal). What is left is the BRAIN's guess from embedding similarity, and two guesses
        are never acceptable on their own:

          * an utterance that is NOTHING but an affirmation ("yes", "go ahead", "ok please")
            names no action — whatever skill it scored nearest is coincidence;
          * `meta.undo` reached, by an utterance shaped like an ANSWER ("yes, fix it", "ok fix
            that", "fix them"), without an undo word in the user's own text: undo reverses a
            real state change, so an affirmation may not stand in for saying so.

        "undo", "revert that", "take that back" etc. still reach `meta.undo` unchanged, and
        every other utterance — including "oops" / "scratch that", which are ordinary
        commands — is untouched (measured: 26 natural phrasings the BRAIN sends to meta.undo). Deterministic, from the user's words only."""
        from friday.config import CFG

        if not CFG.planner.confirmation_guard:
            return None
        try:
            from friday import intent

            bare = intent.is_bare_affirmation(text)
            lexical_undo = (
                understanding.skill == "meta.undo" and intent.is_confirmation_like(text) and not intent.asks_for_undo(text)
            )
        except Exception:
            log.exception("confirmation guard failed (non-fatal)")
            return None
        if not (bare or lexical_undo):
            return None

        if followup is not None:
            subject = f"what I found for \"{followup[1][:80]}\""
            speech = (
                f"I'm not sure what you'd like me to do. If you want me to act on {subject}, tell me what — "
                "for example \"fix it\" or \"fix the issues\"."
            )
        else:
            speech = (
                "Nothing is waiting on an answer from you, so I'm not sure what you mean. "
                "Tell me what you'd like me to do."
            )
        if lexical_undo:
            speech += " If you meant to reverse my last action, say \"undo\"."
        await BUS.publish(
            "session.no_antecedent", text=text, matched=understanding.skill or "", bare=bare, actor="session",
        )
        return SkillResult(speech=speech, ok=False, data={"clarification": "no_antecedent", "matched": understanding.skill})

    def _remember_followup(self, result: SkillResult) -> None:
        """After a `plan.run`: remember the goal for the NEXT turn only if it was
        scoped read-only and is not waiting on a clarification answer."""
        from friday.config import CFG

        self._followup = None
        data = result.data or {}
        goal_id = data.get("goal_id")
        if not goal_id or data.get("awaiting_clarification") or not CFG.planner.scope_expansion:
            return
        try:
            from friday.intelligence import goals as goals_mod

            record = goals_mod.get(goal_id)
            if record is not None and record.contract.action_scope.get("read_only"):
                self._followup = (goal_id, record.objective)
        except Exception:
            log.exception("remembering the follow-up goal failed (non-fatal)")

    async def _maybe_continue_read(self, text: str, *, actor: str) -> SkillResult | None:
        """"Read more" / "next page" / "continue": re-issue the last call with its page
        cursor moved to where that call said the next part starts
        (`friday.intent.next_page_args`). Only for a pagination-capable READ whose
        previous result reported a next position; otherwise None and the utterance is
        handled normally. Dispatched through `_run` -> EXECUTOR like any command."""
        from friday.config import CFG

        if not CFG.planner.continuation_reads or self.last_skill is None:
            return None
        try:
            from friday import intent

            if not intent.is_continuation_request(text):
                return None
            args = intent.next_page_args(self.last_skill, self.last_args, self.last_data)
        except Exception:
            log.exception("continuation check failed (non-fatal)")
            return None
        if args is None:
            return None
        return await self._run(self.last_skill, args, actor=actor)

    # -- contextual reference resolution (Phase 11.4) --------------------------

    async def _maybe_repeat_last_action(self, text: str, *, actor: str) -> SkillResult | None:
        """"Do that again" / "same as before" — see
        `friday.intelligence.context_resolver.resolve_temporal_repeat`.
        Returns `None` (meaning: not a repeat-reference, continue normal
        handling) whenever `text` doesn't match a canonical repeat phrasing
        at all — every other command is completely unaffected."""
        from friday.intelligence import context_resolver

        try:
            if not context_resolver.is_temporal_repeat(text):
                return None
            result = context_resolver.resolve_temporal_repeat()
        except Exception:
            log.exception("temporal reference resolution failed (non-fatal)")
            return None

        if not result.resolved or self.last_skill is None:
            speech = result.clarification or "I don't have anything recent to repeat."
            return SkillResult(speech=speech, ok=False)

        # Re-dispatched through the normal _run -> EXECUTOR.run path, so
        # permission/confirmation are evaluated exactly as they were the
        # first time — resolving *what* "again" means never authorizes
        # *whether* it may run (brief §14).
        return await self._run(self.last_skill, dict(self.last_args), actor=actor)

    def _maybe_route_to_plan(self, text: str, understanding: Understanding) -> Understanding | None:
        """A free-form compound goal ("open Chrome and search YouTube for
        cats", "find my report and open it", "open WhatsApp and message
        Rahul") can confidently ACT-match a single skill for just its FIRST
        clause — `BRAIN.understand` only ever asks "which one skill's
        examples does the whole sentence score closest to," with no notion
        that a sentence might name two distinct stages (documented gap,
        PLAN.md Phase 13.0 §7).

        Cheap gate first (`friday.intelligence.goals.looks_decomposable`,
        the same bar `plan.run`'s own upfront decomposition already uses)
        so an ordinary single-clause command pays nothing extra. Only when
        that trips does this pay for one more real `BRAIN.understand` call
        on the text *after* the first connector — genuine verification that
        a second, different action is actually there, rather than trusting
        the word "and" alone (brief §17: "do not simply route every
        sentence containing 'and' to plan.run"). "salt and pepper," "read
        this and understand it," and a long single-clause sentence with no
        second actionable half all fail this check and stay on the direct
        path. Returns `None` — caller keeps the understanding it already
        had — unless the second half also lands on a complete or
        slot-fillable action for a skill genuinely different from the
        first.
        """
        from friday.intelligence import goals as goals_mod

        if not goals_mod.looks_decomposable(text):
            return None
        # A near-exact match against one skill's own taught vocabulary (a
        # verbatim or near-verbatim example phrasing — e.g. routine.create's
        # own "...then shows the desktop" example, which legitimately
        # contains a connector word as part of ONE freeform argument, not a
        # second action) is strong real evidence of a single atomic action.
        # Confirmed empirically against this exact case before picking
        # 0.90: every one of this phase's three required compound examples
        # scores well below it (0.78-0.85), while the routine.create
        # regression this guard exists for scores a full 1.00.
        if understanding.score >= 0.90:
            return None

        match = _COMPOUND_SPLIT.search(text)
        if not match:
            return None
        head, tail = text[: match.start()].strip(), text[match.end():].strip()
        if not head or not tail:
            return None

        # Real verification, not a guess from the connector word alone
        # (brief §17): does EITHER clause, understood on its own, land on a
        # confident action for a skill genuinely different from what the
        # *whole* sentence matched? A bare "and"/"then" inside one skill's
        # own single freeform argument (as above) has already been screened
        # out by the near-exact check; this catches the remaining case
        # where the whole-sentence embedding match picks up mostly one
        # half's vocabulary (e.g. "Open Chrome and search YouTube for
        # cats." matches web.open — the second clause's own skill — while
        # the first clause alone confidently and separately matches
        # apps.open).
        def _confident_and_distinct(u: Understanding) -> bool:
            return (
                u.action in (Action.ACT, Action.ASK_SLOT)
                and bool(u.skill)
                and u.skill not in (understanding.skill, "plan.run")
            )

        if not (_confident_and_distinct(BRAIN.understand(tail)) or _confident_and_distinct(BRAIN.understand(head))):
            return None

        if REGISTRY.get("plan.run") is None:
            return None
        return Understanding(
            action=Action.ACT, utterance=text, normalized=text,
            skill="plan.run", args={"goal": text}, score=understanding.score,
        )

    def _maybe_route_to_open_ended(self, text: str, understanding: Understanding) -> Understanding | None:
        """Phase 17.0: the actual bridge from `Action.UNKNOWN` into
        open-ended goal handling. `BRAIN.understand` only ever scores an
        utterance against skill EXAMPLE phrasings — a genuinely vague,
        problem-oriented request ("figure out why my project isn't
        working") has nothing close enough to match, and previously dead-
        ended here with a canned "I don't know how to do that yet."

        Deliberately narrow: only engages for `Action.UNKNOWN`, and only
        when `friday.intelligence.discovery.classify_mode` recognizes the
        utterance's SHAPE as diagnostic/investigative/information-seeking/
        open-ended (deterministic heuristics, no embedding, no model call —
        see that module). An ordinary out-of-domain utterance ("write me a
        poem", "what's the capital of France") classifies as
        `GoalMode.DIRECT_ACTION` here and is correctly left alone, still
        landing on the original UNKNOWN response. Returns the same
        `Understanding` shape `_maybe_route_to_plan` already builds — reuses
        the existing ACT/plan.run dispatch path, zero new session mechanics.
        """
        from friday.config import CFG
        from friday.intelligence import discovery
        from friday.intelligence.goals import GoalMode

        if not CFG.planner.enabled or REGISTRY.get("plan.run") is None:
            return None
        if discovery.classify_mode(text) is GoalMode.DIRECT_ACTION:
            return None
        return Understanding(
            action=Action.ACT, utterance=text, normalized=text,
            skill="plan.run", args={"goal": text}, score=understanding.score,
        )

    def _needs_context_resolution(self, u: Understanding, text: str) -> bool:
        """Should contextual reference resolution even be tried for this
        turn? A cheap, purely structural check — no ContextMemory lookup
        happens here, just a decision about whether one is worth trying.

        `Action.ACT` (plain understanding already landed on a complete,
        confident skill call) always says no: a working command must never
        be reinterpreted or second-guessed just because it happens to
        contain the word "it" (brief §12; this is exactly the "what time
        is it" / "crank it up" case — neither has anything to resolve, and
        both already worked before this phase). `ASK_SLOT` (a real skill
        matched but a required argument extracted to nothing — "read it"
        being the textbook case) and `CLARIFY`/`UNKNOWN` (plain
        understanding didn't land at all) are exactly the cases a resolved
        reference can help with, so both say yes; if resolution doesn't
        actually help, the caller falls straight back to the original
        `understanding` it already computed — never a worse outcome than
        before this phase.
        """
        from friday.intelligence import context_resolver

        if not context_resolver.contains_reference(text):
            return False
        if u.action is not Action.ACT:
            return True
        # The one narrow exception to "an ACT decision is never
        # second-guessed": a matched argument whose value is just the
        # literal reference span, echoed back unresolved (e.g. ui.read's
        # optional `app` parameter silently accepting "it" as a literal
        # window name instead of resolving to anything). That is not real
        # evidence for the chosen skill, so it's worth letting contextual
        # resolution try to find a more specific, compatible referent —
        # see friday.brain.engine.route_with_resolved_entity. An ACT whose
        # arguments don't echo the span at all ("what time is it", "crank
        # it up") is completely unaffected (brief §12).
        span = context_resolver.first_reference_span(text)
        if not span:
            return False
        span_low = span.lower()
        return any(
            isinstance(v, str) and v.strip().lower() == span_low
            for v in u.args.values()
        )

    def _reroute_with_context(
        self, text: str, matched_span: str, entity_type: str | None, referent: str | None,
    ) -> Understanding | None:
        """Phase 13.0: after a reference resolves, prefer routing through
        `friday.brain.engine.route_with_resolved_entity` (matches on a
        generic canonical phrase for the entity's type, then fills the
        real value in afterward) over blindly re-running `BRAIN.understand`
        on text containing the raw resolved value — see that function's
        docstring for why the latter drifts to unrelated skills. Returns
        `None` when no compatible route was found, so the caller can fall
        back exactly as it did before this phase.
        """
        if not entity_type or not referent:
            return None
        from friday.brain import engine as brain_engine

        try:
            return brain_engine.route_with_resolved_entity(
                text, matched_span, entity_type, referent,
            )
        except Exception:
            log.exception("context-aware routing failed (non-fatal)")
            return None

    def _resolve_context_reference(self, text: str) -> tuple[str, Any]:
        """Cheap fast-path gate first (brief §12/§19): an utterance with no
        pronoun/reference at all costs nothing beyond one regex scan and
        returns `(text, None)` unchanged. Only a genuine reference pays for
        a `ContextMemory` lookup."""
        from friday import risk
        from friday.intelligence import context_resolver

        try:
            if not context_resolver.contains_reference(text):
                return text, None
            consequential = risk.is_consequential(text)
            return context_resolver.resolve_pronoun_in_text(text, consequential=consequential)
        except Exception:
            log.exception("context reference resolution failed (non-fatal)")
            return text, None

    async def _ask_context_clarification(self, result: Any, original_text: str, *, actor: str) -> SkillResult:
        """A reference was found but must not be guessed — ask instead
        (brief §5: "do not guess when multiple candidates are plausible")."""
        if result.candidates:
            self.pending = Pending(
                kind="context_clarify", options=result.candidates, actor=actor,
                context_span=result.matched_span, context_template=original_text,
                context_entity_type=result.entity_type or "",
            )
            await BUS.publish(
                "session.context_ambiguous", candidates=result.candidates, reason=result.reason,
            )
        speech = result.clarification or "I'm not sure what you mean. Could you clarify?"
        return SkillResult(speech=speech, ok=False)

    def _remember_context_entities(self, skill_name: str, args: dict[str, Any], result: SkillResult) -> None:
        """Best-effort: a successful, entity-naming skill call becomes a
        candidate referent for the *next* "it"/"that" — see
        `friday.intelligence.context_memory.record_from_skill`. Never raises
        into the caller; a broken entity extraction must never break the
        skill call it's derived from."""
        if not result.ok:
            return
        try:
            from friday.config import CFG

            if not CFG.intelligence.context_memory_enabled:
                return
            from friday.intelligence import context_memory

            context_memory.record_from_skill(skill_name, args, result.data, turn_id=self._turn_id)
        except Exception:
            log.exception("context memory recording failed (non-fatal)")

    # -- acting ---------------------------------------------------------------

    async def _act(self, u: Understanding, *, actor: str) -> SkillResult:
        if u.action is Action.ACT and u.skill:
            return await self._run(u.skill, u.args, actor=actor)

        if u.action is Action.ASK_SLOT and u.skill:
            self.pending = Pending(
                kind="slot", skill=u.skill, args=u.args, missing=u.missing[0]
            )
            await BUS.publish("session.awaiting_slot", skill=u.skill, slot=u.missing[0])
            return SkillResult(speech=u.speech, ok=False)

        if u.action is Action.CLARIFY:
            self.pending = Pending(
                kind="clarify", options=[c.skill for c in u.candidates]
            )
            await BUS.publish("session.clarifying", options=[c.skill for c in u.candidates])
            return SkillResult(speech=u.speech, ok=False)

        await BUS.publish("session.unknown", text=u.utterance)
        return SkillResult(speech=u.speech, ok=False)

    async def _run(
        self, skill_name: str, args: dict[str, Any], *, actor: str
    ) -> SkillResult:
        self.last_skill = skill_name
        self.last_args = dict(args)
        self.last_data = {}
        self._confirm_actor = actor
        try:
            result = await EXECUTOR.run(skill_name, args, actor=actor)
        except PermissionError_ as exc:
            return SkillResult(speech=f"I'm not allowed to do that. {exc}", ok=False)
        except KeyError:
            return SkillResult(speech="That skill isn't available.", ok=False)
        except Exception as exc:
            log.exception("skill %s failed", skill_name)
            return SkillResult(speech=f"That failed: {exc}", ok=False)
        self._remember_context_entities(skill_name, args, result)
        self.last_data = dict(result.data or {})
        if skill_name == "plan.run":
            self._remember_followup(result)
        # Phase 17.0: `plan.run` paused a goal at GoalStatus.BLOCKED,
        # waiting on a same-goal clarification (see friday.skills.plan's
        # `_await_clarification`) — park it exactly like any other pending
        # question so the next utterance resumes the SAME goal instead of
        # being re-parsed as an unrelated new command.
        if result.data and result.data.get("awaiting_clarification"):
            self.pending = Pending(
                kind="goal_clarify", goal_id=result.data.get("goal_id"),
                question=result.speech, actor=actor,
            )
        return result

    async def teach_last(self, skill_name: str) -> SkillResult:
        """Bind the previous utterance to a skill — the learning loop."""
        if not self.last_utterance:
            return SkillResult(speech="There's nothing to teach me yet.", ok=False)
        try:
            BRAIN.teach(self.last_utterance, skill_name)
        except KeyError:
            return SkillResult(speech=f"I don't have a skill called {skill_name}.", ok=False)
        return SkillResult(
            speech=f"Got it. \"{self.last_utterance}\" now means {skill_name}."
        )


SESSION = Session()
