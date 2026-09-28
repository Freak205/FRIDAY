"""Phase 10 — the intelligence core.

Turns FRIDAY from `user -> intent -> skill -> result` into a loop that
understands, establishes context, plans, acts, observes, evaluates, replans
when needed, and remembers the experience:

    friday.intelligence.state         bounded, in-memory intelligence state
    friday.intelligence.goals         explicit goal representation + status
    friday.intelligence.working_memory  bounded, task-relevant context
    friday.intelligence.episodes      episodic experience memory (SQLite)
    friday.intelligence.evaluator     deterministic, evidence-based evaluation
    friday.intelligence.corrections   structured record of user corrections
    friday.intelligence.self_state    what FRIDAY is doing right now

Nothing here is a second brain, a second permission system, or a second
memory store. `friday.orchestrator.Orchestrator` still runs every tool call
through `friday.permissions.EXECUTOR`; this package only adds the layer of
goal/state/experience bookkeeping around it, wired in primarily from
`friday/skills/plan.py` (multi-step goals) and `friday/session.py`
(correction detection, self-state).
"""
