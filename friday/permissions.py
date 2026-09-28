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

import contextvars
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from friday import audit
from friday.bus import BUS
from friday.config import CFG
from friday.log import get
from friday.registry import REGISTRY, Skill, SkillResult

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
        log.exception("action recording failed (non-fatal)")


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
            log.exception("risk classifier failed for %s; failing safe to L3", skill.name)
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

        await BUS.publish("skill.start", skill=skill.name, args=args)
        started = time.perf_counter()
        actor_token = _CURRENT_ACTOR.set(actor)
        try:
            result = await skill(**args)
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            audit.complete(audit_id, ok=False, error=str(exc), duration_ms=elapsed)
            await BUS.publish("skill.error", skill=skill.name, error=str(exc))
            log.exception("skill %s failed", skill.name)
            _record_action(skill.name, args, actor=actor, status="error")
            raise
        finally:
            _CURRENT_ACTOR.reset(actor_token)

        elapsed = int((time.perf_counter() - started) * 1000)
        audit.complete(
            audit_id, ok=result.ok, result=result.speech, duration_ms=elapsed
        )
        _record_action(
            skill.name, args, actor=actor, status="success" if result.ok else "failed",
        )

        if skill.undo is not None:
            try:
                inverse = skill.undo(**args)
                if inverse:
                    audit.register_undo(audit_id, skill.name, inverse)
            except Exception:
                log.exception("undo registration failed for %s", skill.name)

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
