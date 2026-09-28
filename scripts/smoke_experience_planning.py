"""Phase 11.1 real smoke test: does retrieved experience actually reach
`plan.run`'s planner input?

Walks through exactly the narrative the phase brief asks for:

  A. Run a simple successful task.
  B. Confirm an episode was created for it.
  C. Run a similar task.
  D. Verify the planner's *actual prompt* contains the prior episode.
  E. Create a relevant failed episode.
  F. Run a related task again.
  G. Verify the failure context reaches the planner.

Never asserts on particular LLM wording — every check inspects the real
text `Orchestrator._decide_next` sent to the (scripted) LLM, via the same
`ScriptedPlanner` fake `scripts/smoke_intelligence.py` uses.

Phase 14.0: runs against an isolated, throwaway SQLite database
(`friday.store.use_temp_db`), not the real, shared `data/friday.db` a
prior version of this file used directly. That prior approach made this
script's assertions sensitive to how many times it (and
`smoke_goal_decomposition.py`) had already been run, since retrieval
top-k gets crowded by every earlier run's near-duplicate episode left
behind in the shared store — see PLAN.md Phase 12.0/13.0 for that
documented flakiness. Every goal text is still tagged with a fresh
random/distinctive suffix per run regardless, so this script never
depends on, or leaves behind, any state outside its own temp database.
`CFG.intelligence.experience_max_episodes` is still temporarily widened
below, matching the original intent (deterministic regardless of
episode count) now that isolation makes it unconditionally true rather
than merely likely.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import llm, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import episodes  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402


class ScriptedPlanner(LlmProvider):
    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.prompts.append(request.messages[-1].content)
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LlmResponse(text=reply, model="scripted", provider=self.name)


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


def done(summary: str) -> str:
    return json.dumps({"action": "done", "summary": summary})


async def main() -> None:
    # Phase 14.0: isolated from the real, shared data/friday.db (see
    # friday.store.use_temp_db) — this script's own docstring above
    # documented the resulting top-k-crowding flakiness as a known,
    # unavoidable-without-isolation limitation; this removes that need.
    db_cm = store.use_temp_db()
    db_cm.__enter__()
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    overall = True

    original_max_episodes = CFG.intelligence.experience_max_episodes
    CFG.intelligence.experience_max_episodes = 20

    # Deliberately rare/distinctive vocabulary, not just a unique tag: this
    # runs against the real, shared, ever-growing episode store (no
    # per-run test database — see module docstring), and common words like
    # "project"/"status"/"report" accumulate many near-duplicate rows across
    # repeated smoke-test runs over time, which can out-rank a genuine
    # paraphrase match on generic vocabulary alone.
    tag = uuid.uuid4().hex[:8]
    success_goal = f"smoke_exp_{tag} recalibrate the aurora telemetry uplink antenna"
    similar_goal = f"smoke_exp_{tag} adjust the aurora telemetry uplink antenna"
    fail_goal = f"smoke_exp_{tag} synchronize the obsidian ledger vault"
    similar_fail_goal = f"smoke_exp_{tag} sync the obsidian ledger vault"

    # -- A: run a simple successful task ----------------------------------------
    print("\n--- A: run a simple successful task ---\n")
    planner_a = ScriptedPlanner([call("meta.capabilities"), done("Dashboard is healthy.")])
    with scripted_provider(planner_a):
        result_a = await EXECUTOR.run("plan.run", {"goal": success_goal}, actor="test")
    ok = result_a.ok and result_a.data.get("stopped") == "completed"
    print(f"  {'OK  ' if ok else 'MISS'} task ran successfully -> ok={result_a.ok}, "
          f"stopped={result_a.data.get('stopped')}")
    overall &= ok

    # -- B: confirm an episode was created ---------------------------------------
    print("\n--- B: confirm an episode was created ---\n")
    goal_id_a = result_a.data.get("goal_id")
    recorded = [e for e in episodes.recent(50) if e.goal_id == goal_id_a]
    ok = len(recorded) == 1 and recorded[0].success and recorded[0].goal_text == success_goal
    print(f"  {'OK  ' if ok else 'MISS'} exactly one successful episode recorded -> goal_id={goal_id_a}")
    overall &= ok

    # -- C & D: run a similar task; verify the planner receives the prior episode -
    print("\n--- C & D: a similar task's planner prompt contains the prior success ---\n")
    planner_c = ScriptedPlanner([done("Dashboard status checked.")])
    with scripted_provider(planner_c):
        result_c = await EXECUTOR.run("plan.run", {"goal": similar_goal}, actor="test")
    ok = result_c.ok
    prior_success_lines = [
        line for p in planner_c.prompts for line in p.splitlines()
        if success_goal in line and "SUCCEEDED before" in line
    ]
    ok = ok and bool(prior_success_lines)
    print(f"  {'OK  ' if ok else 'MISS'} planner input for the similar task contains the earlier success "
          f"-> {prior_success_lines[:1]}")
    overall &= ok

    # -- E: create a relevant failed episode -------------------------------------
    print("\n--- E: create a relevant failed episode ---\n")
    # meta.capabilities called with identical (empty) args three times in a
    # row: the orchestrator's repeat guard blocks the 2nd, then stops the
    # plan outright as "repeated_action" on the 3rd — a real, deterministic
    # failure the orchestrator itself classifies, not a fabricated one.
    planner_e = ScriptedPlanner([
        call("meta.capabilities"), call("meta.capabilities"), call("meta.capabilities"),
    ])
    with scripted_provider(planner_e):
        result_e = await EXECUTOR.run("plan.run", {"goal": fail_goal}, actor="test")
    ok = not result_e.ok and result_e.data.get("stopped") == "repeated_action"
    print(f"  {'OK  ' if ok else 'MISS'} the task failed as expected -> ok={result_e.ok}, "
          f"stopped={result_e.data.get('stopped')}")
    overall &= ok

    goal_id_e = result_e.data.get("goal_id")
    recorded_fail = [e for e in episodes.recent(50) if e.goal_id == goal_id_e]
    ok = len(recorded_fail) == 1 and not recorded_fail[0].success
    print(f"  {'OK  ' if ok else 'MISS'} exactly one failed episode recorded -> goal_id={goal_id_e}")
    overall &= ok

    # -- F & G: run a related task again; verify the failure reaches the planner --
    print("\n--- F & G: a related task's planner prompt contains the prior failure ---\n")
    planner_f = ScriptedPlanner([done("Handled carefully given the prior failure.")])
    with scripted_provider(planner_f):
        result_f = await EXECUTOR.run("plan.run", {"goal": similar_fail_goal}, actor="test")
    ok = result_f.ok
    prior_failure_lines = [
        line for p in planner_f.prompts for line in p.splitlines()
        if fail_goal in line and "FAILED before" in line
    ]
    ok = ok and bool(prior_failure_lines)
    print(f"  {'OK  ' if ok else 'MISS'} planner input for the related task contains the earlier failure "
          f"-> {prior_failure_lines[:1]}")
    overall &= ok

    CFG.intelligence.experience_max_episodes = original_max_episodes

    db_cm.__exit__(None, None, None)
    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
