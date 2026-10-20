"""Phase 65 -- which bus events a remote (WebSocket) client may see, and exactly which fields of them.

The bus carries everything FRIDAY does: utterances, planner replies, tool arguments, read-back content, reply speech, window titles. In-process
subscribers (the GUI state hub, self-state, proactive, the CLI) need all of that and keep receiving the ORIGINAL events. A WebSocket client is outside the
process, and with the shared-token model there is no per-client identity: every authenticated client would see every request's content. So the stream is a
projection, not a filter over the whole payload:

  * DENY BY DEFAULT. A topic that is not listed in `POLICY` is never sent. A new internal event does not become remotely observable until someone lists it
    here, field by field, which is a deliberate act.
  * ALLOWLIST OF FIELDS. A listed topic is sent with only its listed fields, each checked by a strict validator (a number, a flag, a short identifier from a
    fixed alphabet, a known word). A field that fails its validator, and every field not listed, is dropped -- never "cleaned up" and passed along.
  * NO CONTENT. No event carries arguments, goals, utterances, previews, planner replies, reply speech, read-back data, exception text, window titles or any
    nested payload. A client learns THAT something happened (which skill, how long, whether it worked), never WHAT was asked or answered.
  * The projected event still goes through `auditview.sanitize_event` (Phase 64: credential check, fail-closed) as a second layer.

The requester of a command is not affected: `/ws` answers its own `{"text": ...}` message with a `result` frame directly, and `/say` / `/invoke` return the
reply to the caller. Nothing here identifies requesters, makes events private to a client, or replaces authentication.

Remote event classes (see `POLICY`):
  daemon status        daemon.ready
  skill activity       skill.start, skill.done, skill.error            -- skill name, duration
  permission activity  permission.confirm_requested / declined / denied -- skill name, tier
  goal progress        orchestrator.start / step / observation / done / cancelling / replan / verification / decision / subgoal / ...
                                                                        -- tool, step counts, durations, status words
  job activity         job.start, job.done                              -- job id, success, duration
  session activity     session.heard (actor only), session.awaiting_confirm, session.stop_requested, ...
Suppressed (never remote): brain.*, session.unknown / no_antecedent / clarifying / context_ambiguous / scope_expansion, proactive.*, trigger.*,
backend.late_result, and every topic not listed.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from friday.log import get

log = get(__name__)

_IDENT = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
Validator = Callable[[Any], bool]


def _ident(v: Any) -> bool:
    return isinstance(v, str) and bool(_IDENT.fullmatch(v))


def _count(v: Any) -> bool:
    return type(v) is int and -1 <= v <= 10_000_000_000


def _flag(v: Any) -> bool:
    return type(v) is bool


def _word(*allowed: str) -> Validator:
    return lambda v: isinstance(v, str) and v in allowed


_TIER = _word("L0", "L1", "L2", "L3")

# topic -> {field: validator}. EVERY entry is a decision that the field is safe to show any authenticated client.
POLICY: dict[str, dict[str, Validator]] = {
    "daemon.ready": {"skills": _count},
    # skill activity
    "skill.start": {"skill": _ident},
    "skill.done": {"skill": _ident, "ms": _count},
    "skill.error": {"skill": _ident},
    # permission activity
    "permission.confirm_requested": {"skill": _ident, "tier": _TIER},
    "permission.declined": {"skill": _ident},
    "permission.denied": {"skill": _ident},
    # goal progress
    "orchestrator.start": {"actor": _ident, "mode": _ident, "steps": _count},
    "orchestrator.step": {"tool": _ident},
    "orchestrator.observation": {"tool": _ident, "ms": _count, "blocked": _flag},
    "orchestrator.done": {"ok": _flag, "stopped": _ident, "actor": _ident, "executed": _count},
    "orchestrator.cancelling": {"tool": _ident, "interruptible": _flag, "actor": _ident},
    "orchestrator.replan": {"attempt": _count, "max_replans": _count},
    "orchestrator.verification": {"tool": _ident, "status": _ident},
    "orchestrator.decision": {"valid": _flag, "repaired": _flag, "repair_succeeded": _flag, "model_calls": _count, "ms": _count, "discovery": _flag},
    "orchestrator.subgoal": {"index": _count},
    "orchestrator.coverage_nudge": {"nudges": _count},
    "orchestrator.action_nudge": {"nudges": _count},
    "orchestrator.action_unfinished": {},
    "orchestrator.evidence_stop": {"steps": _count},
    "orchestrator.intent_mismatch": {"tool": _ident, "rejections": _count, "repeated": _flag},
    "orchestrator.tool_family": {"tool": _ident, "rejections": _count, "repeated": _flag},
    # job activity (the job's name is user text: id only)
    "job.start": {"id": _count},
    "job.done": {"id": _count, "ok": _flag, "ms": _count},
    # session activity (never the text)
    "session.heard": {"actor": _ident},
    "session.awaiting_confirm": {"actor": _ident, "skill": _ident, "tier": _TIER},
    "session.stop_requested": {"source": _ident, "goal_running": _flag, "declined_confirmation": _flag},
    "session.awaiting_slot": {"skill": _ident},
    "session.recovery_route": {"kind": _ident, "skill": _ident, "actor": _ident},
}


def project(topic: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """What a remote client may receive for this event, or None for no event at all. Never raises."""
    rules = POLICY.get(topic)
    if rules is None:
        return None
    try:
        from friday import auditview

        picked = {k: data[k] for k, ok in rules.items() if k in data and ok(data[k])}
        return auditview.sanitize_event(topic, picked)
    except Exception:  # noqa: BLE001 -- anything unexpected: send nothing
        log.warning("remote event %s dropped: projection failed", topic)
        return None
