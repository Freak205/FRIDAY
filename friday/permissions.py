"""Permission tiers and the execution guard.

Nothing in FRIDAY calls a skill directly — everything goes through `Executor.run`,
which decides whether the call is allowed, asks for confirmation when required,
writes the audit trail, and records an undo entry when one exists.

Tiers
  L0  read-only            list, read, search, screenshot
  L1  reversible write     open app, move file, set volume, type text
  L2  destructive          delete, overwrite, kill process, registry write
  L3  external/irreversible  send message, purchase, shutdown, use credentials
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from friday import audit, auditview, toolview
from friday.bus import BUS
from friday.config import CFG
from friday.log import get, log_exception
from friday.registry import DETACH, REGISTRY, Detach, Skill, SkillResult

log = get(__name__)

TIER_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}

# Actors that run without a human present.
UNATTENDED = {"scheduler", "trigger"}

ConfirmFn = Callable[[Skill, dict[str, Any], str], Awaitable[bool]]

# The actor of whichever skill call is currently executing, visible to that
# skill's own body via `current_actor()`. Exists so a skill that itself fans
# out into further calls (e.g. `plan.run` driving an `Orchestrator`) can carry
# the *original* actor through — a scheduled/triggered goal must stay bound by
# the same unattended tier ceiling its inner steps would get if run directly,
# rather than laundering them through a fixed "attended" actor.
_CURRENT_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_CURRENT_ACTOR", default="text"
)


def current_actor() -> str:
    """The actor of the skill call in progress, for callers nested inside one."""
    return _CURRENT_ACTOR.get()


class PermissionError_(Exception):
    """Raised when a call is refused by policy."""


def _check_failed(result: SkillResult) -> SkillResult:
    """The credential check every FAILED result gets before anything reads it (the one `Executor.run` and `_record_late_outcome`
    share, so the two cannot drift apart). Phase 61: a FAILURE reply is an error message, not content the user asked to read back,
    and skills build it from library exceptions ("I couldn't fetch it: HTTP 401 for https://...?api_key=..."); it gets the
    credential check once, here. A successful reply is left alone: "what's in my clipboard" is exactly what the user asked to hear.
    Phase 64: the same for the structured part -- callers return `data` to API clients as is. A copy with the strings
    credential-checked; keys, flags, numbers and ids (a recovery token, `confirmation_declined`) are untouched.
    Phase 97: if the check itself fails, the original is never kept (`safe_speech` already withholds; `data` is dropped)."""
    if result.ok:
        return result
    try:
        result.speech = toolview.safe_speech(result.speech)
    except Exception:  # noqa: BLE001 -- `safe_speech` never raises; if it somehow does, the text is withheld, not kept
        result.speech = toolview.WITHHELD
    if result.data:
        try:
            result.data = auditview.scrub_tree(result.data)
        except Exception:  # noqa: BLE001 -- unchecked data must not be handed on; `scrub_tree` itself never raises
            result.data = {}
    return result


# Phase 97: stands in for the text of an exception that cannot be turned into text at all (a broken `__str__`).
LATE_ERROR_UNREADABLE = "the worker ended with an error whose message could not be read safely"


def _late_error_text(exc: BaseException) -> str:
    """The text of an abandoned worker's exception, credential-checked like the exception text `Executor.run` publishes
    (`safe_speech`); a message that cannot be read, or checked, is replaced, never kept as it was."""
    try:
        return toolview.safe_speech(str(exc))
    except Exception:  # noqa: BLE001
        return LATE_ERROR_UNREADABLE


def _record_late_outcome(audit_id: int, skill_name: str, fut: Any, started: float) -> None:
    """Phase 53: the worker thread of an abandoned step has now ended (`registry.Detach`): write its real outcome over the
    "outcome unknown" marker of its audit row. Runs in that thread, once. Records the outcome only -- no undo entry is
    registered (the run that would have reported the result is long gone, and an inverse nobody was told about is a surprise),
    nothing is retried, and a row that has meanwhile been given any other text is still just overwritten with the truth.

    Phase 97: the audit row is the ONLY place a late result goes (no event, no memory, no log of its content). What reaches it has
    had the same checks as at the end of `Executor.run`: a failed result's speech and data (`_check_failed`), an exception's text
    (`safe_speech`), and then the audit's own storage rules (`audit.complete`: omitted-argument echoes, bounds, the shell's
    read-back rule). The outcome is classified from the result as the worker returned it, never from the checked copy."""
    try:
        if fut.cancelled():
            return
        elapsed = int((time.perf_counter() - started) * 1000)
        exc = fut.exception()
        if exc is not None:
            audit.complete(audit_id, ok=False, error=_late_error_text(exc), duration_ms=elapsed, outcome=audit.FAILURE)
        else:
            result = SkillResult.coerce(fut.result())
            outcome = audit.outcome_of_result(skill_name, result.ok, result.data)
            audit.complete(audit_id, ok=result.ok, result=_check_failed(result).speech, duration_ms=elapsed, outcome=outcome)
        log.info("late outcome recorded for %s (audit %d)", skill_name, audit_id)
    except Exception:
        log_exception(log, "could not record the late outcome of %s (audit %d)", skill_name, audit_id)


def _record_action(skill_name: str, args: dict[str, Any], *, actor: str, status: str) -> None:
    """Phase 14.0: best-effort action-continuity bookkeeping.

    `Executor.run` is the one place every real skill invocation funnels
    through — a direct command and every step of a `plan.run` goal alike
    (see `friday.orchestrator.Orchestrator._run_step`'s default runner) —
    so this is the single correct integration point for
    `friday.intelligence.state.INTEL.record_action`, called only after the
    real outcome is known (deny/decline/error/success), never merely
    because a skill was selected. `goal_id` is read from `INTEL.state`
    (already populated by `friday.skills.plan.py` for a `plan.run` goal,
    left `None` for an ordinary single command) rather than threaded
    through a new parameter, so this needs no orchestrator/session
    signature change. Wrapped so a broken recording call never breaks the
    real skill call it's describing — same "non-fatal" convention as
    `friday.session.Session._remember_context_entities`.
    """
    try:
        from friday.intelligence.state import INTEL

        INTEL.record_action(
            skill_name, args=args, status=status, actor=actor,
            goal_id=INTEL.state.current_goal_id,
        )
    except Exception:
        log_exception(log, "action recording failed (non-fatal)")


@dataclass(slots=True)
class Decision:
    policy: str  # auto | confirm | deny
    reason: str = ""
    # Tier actually used for this decision — equals skill.tier unless a risk
    # classifier escalated this specific call (see friday.risk). Recorded so
    # audit entries and confirm prompts can show *why* an otherwise-auto tool
    # needed a human.
    effective_tier: str = ""


def evaluate(skill: Skill, actor: str, args: dict[str, Any] | None = None) -> Decision:
    """Decide how a call should be handled, before any confirmation prompt."""
    perms = CFG.permissions

    effective_tier = skill.tier
    escalated = False
    if skill.risk is not None:
        try:
            if skill.risk(**(args or {})):
                effective_tier, escalated = "L3", True
        except Exception:
            # A broken classifier must never fail open — treat as the riskiest case.
            log_exception(log, "risk classifier failed for %s; failing safe to L3", skill.name)
            effective_tier, escalated = "L3", True

    # A per-tool override is an explicit user choice — it beats both the tier
    # default and a risk escalation. Without one, escalation swaps in L3's
    # policy default for just this call.
    override = perms.overrides.get(skill.name)
    policy = override or perms.tiers.get(effective_tier, "confirm")
    escalation_note = (
        f" (escalated from {skill.tier}: this call looks consequential)" if escalated else ""
    )

    if actor in UNATTENDED:
        ceiling = TIER_ORDER[perms.unattended_ceiling]
        if TIER_ORDER[effective_tier] > ceiling:
            return Decision(
                "deny",
                f"{effective_tier} exceeds the unattended ceiling "
                f"({perms.unattended_ceiling}) for actor '{actor}'{escalation_note}",
                effective_tier,
            )
        # An unattended job can never stop to ask a human.
        if policy == "confirm":
            return Decision("deny", "confirmation required but no human is present", effective_tier)

    if policy == "deny":
        return Decision("deny", f"policy denies {skill.name}", effective_tier)

    return Decision(policy, escalation_note.strip(), effective_tier)


class Executor:
    """Runs skills under policy. One instance per daemon."""

    def __init__(self, confirm: ConfirmFn | None = None) -> None:
        self._confirm = confirm
        # Phase 48: audit id -> skill name for every call that has started and not yet been recorded. Only read by the
        # backend's shutdown, to mark what is still running when it must stop waiting.
        self._in_flight: dict[int, str] = {}

    def in_flight(self) -> dict[int, str]:
        return dict(self._in_flight)

    def set_confirm_handler(self, fn: ConfirmFn) -> None:
        self._confirm = fn

    async def run(
        self,
        skill_name: str,
        args: dict[str, Any] | None = None,
        *,
        actor: str = "text",
    ) -> SkillResult:
        args = args or {}
        skill = REGISTRY.get(skill_name)
        if skill is None:
            raise KeyError(f"unknown skill: {skill_name}")

        decision = evaluate(skill, actor, args)
        effective_tier = decision.effective_tier or skill.tier

        if decision.policy == "deny":
            audit.record(
                actor=actor, skill=skill.name, tier=effective_tier,
                args=args, decision="blocked",
            )
            await BUS.publish(
                "permission.denied", skill=skill.name, reason=decision.reason
            )
            log.warning("denied %s: %s", skill.name, decision.reason)
            _record_action(skill.name, args, actor=actor, status="permission_denied")
            raise PermissionError_(decision.reason)

        if decision.policy == "confirm":
            preview = skill.preview(**args)
            if decision.reason:
                preview = f"{preview} {decision.reason}"
            await BUS.publish(
                "permission.confirm_requested",
                skill=skill.name, tier=effective_tier, preview=preview,
            )
            approved = await self._ask(skill, args, preview)
            if not approved:
                audit.record(
                    actor=actor, skill=skill.name, tier=effective_tier,
                    args=args, decision="denied",
                )
                await BUS.publish("permission.declined", skill=skill.name)
                # Tagged so a caller driving several calls in sequence (e.g.
                # friday.orchestrator.Orchestrator.run_goal) can tell "the
                # human said no" apart from an ordinary tool failure — see
                # friday.intelligence.evaluator._NON_REPLANNABLE_ERRORS.
                # Adaptive replanning must never look like a way around a
                # declined confirmation (Phase 11.2).
                _record_action(skill.name, args, actor=actor, status="confirmation_declined")
                return SkillResult(speech="Cancelled.", ok=False, data={"confirmation_declined": True})
            audit_decision = "confirmed"
        else:
            audit_decision = "auto"

        audit_id = audit.record(
            actor=actor, skill=skill.name, tier=effective_tier,
            args=args, decision=audit_decision,
        )

        try:
            await BUS.publish("skill.start", skill=skill.name, args=args)
        except (asyncio.CancelledError, Exception) as exc:
            # Phase 96: the row exists but the skill was never called -- nothing may be left open for good in a process that lives on.
            try:
                audit.complete(
                    audit_id, ok=False, outcome=audit.NOT_STARTED,
                    error="not started: cancelled before it began" if isinstance(exc, asyncio.CancelledError) else str(exc),
                )
            except Exception:
                log_exception(log, "could not close the audit row of %s (audit %d)", skill.name, audit_id)  # never replaces the cancel
            raise
        started = time.perf_counter()
        actor_token = _CURRENT_ACTOR.set(actor)
        self._in_flight[audit_id] = skill.name
        refused = bool(skill.unknown_args(args))  # Phase 96: `Skill.__call__` turns such a call away before the skill runs
        detach = Detach(lambda fut: _record_late_outcome(audit_id, skill.name, fut, started))
        detach_token = DETACH.set(detach)
        try:
            result = await skill(**args)
        except asyncio.CancelledError:
            # Phase 28.0: the step was interrupted by a stop request (or the run's own timeout).
            # Close the audit row honestly -- started, not known to have finished -- instead of
            # leaving it open-ended, and let the cancellation keep propagating.
            #
            # Phase 53: a worker-thread skill cannot be interrupted -- it is abandoned, not stopped, and its side effect
            # may still happen. Recording that as a failure (`ok=False`) was a claim nobody could back (found by driving
            # a step timeout through plan.run: the audit said "failed" while the skill went on to finish). Its row is
            # "outcome unknown" instead, and when the thread ends its real outcome replaces the marker.
            elapsed = int((time.perf_counter() - started) * 1000)
            if detach.started:
                audit.mark_outcome_unknown(audit_id, detached=True)
            else:
                # Phase 96: a worker-thread skill whose job the cancel removed from the queue never ran (not_started); any other
                # cancelled call was seen to end by cancellation -- whether it had a partial effect is not established, so it is
                # `cancelled`, never a plain failure. The cancel carries no reason, so a timeout and a stop request read alike.
                audit.complete(
                    audit_id, ok=False, error="interrupted before it finished (cancelled)", duration_ms=elapsed,
                    outcome=audit.NOT_STARTED if detach.never_ran else audit.CANCELLED,
                )
            _record_action(skill.name, args, actor=actor, status="cancelled")
            raise
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            audit.complete(audit_id, ok=False, error=str(exc), duration_ms=elapsed, outcome=audit.FAILURE)
            # Phase 61: the event goes to every bus consumer (GUI, every websocket client); an exception message is library text the
            # user never asked to hear and may carry a key or token (an HTTP error quotes its URL), so it gets the credential check
            await BUS.publish("skill.error", skill=skill.name, error=toolview.safe_speech(str(exc)))
            log_exception(log, "skill %s failed", skill.name)
            _record_action(skill.name, args, actor=actor, status="error")
            raise
        finally:
            self._in_flight.pop(audit_id, None)
            audit.reset_context(DETACH, detach_token)
            audit.reset_context(_CURRENT_ACTOR, actor_token)

        elapsed = int((time.perf_counter() - started) * 1000)
        # Phase 61/64: every direct caller (GUI, voice, /say, /invoke, the event stream, plan steps) receives a failed result from
        # here, so it gets the credential check once, here (see `_check_failed`, which the late-outcome path shares).
        # Phase 94: the explicit outcome (a shell command that was never started / ended unknown / timed out / was cancelled is
        # not just "ok=False"); an unknown outcome keeps `ok` NULL in the row -- see `audit.complete`. Phase 97: read from the
        # result as the skill returned it, before the credential check can touch its data (as the late-outcome path does).
        outcome = audit.NOT_STARTED if refused else audit.outcome_of_result(skill.name, result.ok, result.data)
        _check_failed(result)
        audit.complete(audit_id, ok=result.ok, result=result.speech, duration_ms=elapsed, outcome=outcome)
        _record_action(
            skill.name, args, actor=actor, status="success" if result.ok else "failed",
        )

        # Phase 32.0: an inverse is recorded only for a call that actually SUCCEEDED. Before, a
        # failed call (copy of a missing file, mkdir of an existing folder, write onto a folder)
        # still journaled "delete <destination>", so a later undo deleted something this call never
        # created. A skill whose inverse depends on what happened returns it as `result.undo`
        # (which snapshot of the overwritten file); otherwise its static `undo=` is used.
        if result.ok:
            try:
                inverse = result.undo
                if not inverse and skill.undo is not None:
                    inverse = skill.undo(**args)
                if inverse:
                    audit.register_undo(audit_id, skill.name, inverse)
            except Exception:
                log_exception(log, "undo registration failed for %s", skill.name)

        await BUS.publish(
            "skill.done", skill=skill.name, speech=result.speech, ms=elapsed
        )
        return result

    async def _ask(self, skill: Skill, args: dict[str, Any], preview: str) -> bool:
        if self._confirm is None:
            # No confirmation channel wired up — refuse rather than assume yes.
            log.warning("no confirm handler; refusing %s", skill.name)
            return False
        return await self._confirm(skill, args, preview)


EXECUTOR = Executor()
