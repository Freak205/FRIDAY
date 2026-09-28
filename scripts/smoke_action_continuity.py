"""Phase 14.0 — action continuity & reliable task loop.

Deterministic regression coverage for this phase's three fixes:

1. `friday.intelligence.state.INTEL.record_action` is wired into
   `friday.permissions.Executor.run` — the one real dispatch funnel both a
   direct command and every `plan.run` step go through — so a real
   execution's actual outcome (not merely "a skill was selected") lands in
   `INTEL.state.action_log`/`recent_actions`.
2. "Do that again" (`friday.intelligence.context_resolver
   .resolve_temporal_repeat`) now actually works from a genuinely fresh
   session: it used to always report "I don't have anything recent to
   repeat" because nothing populated `INTEL.state.recent_actions` in
   production (PLAN.md Phase 13.0 §7) — no test-only assist needed anymore.
3. Two routing gaps: "focus VS Code" (`friday.brain.engine
   ._prefer_known_focus`) and free-form compound goals not reaching
   `plan.run` (`friday.session.Session._maybe_route_to_plan`).

Same `check`/`OK`/`MISS`/`NOTE` print convention as every other
`scripts/smoke_*.py` — no pytest. `isolated_file_root`/`fake win32` fakes
are deliberately NOT needed here: every real-execution check below uses
either a harness-owned temp file root (file skills) or a test-only skill
registered the same way `scripts/agent_reliability.py`'s scenarios I/J/K
do, so nothing here touches a real window, browser, or WhatsApp session.
Sections A-G run against the real, shared `data/friday.db` (same
tolerance every other `SESSION.handle()`-driven smoke script already has);
section H exercises `friday.store.use_temp_db` directly, the isolation
mechanism `smoke_experience_planning.py`/`smoke_goal_decomposition.py` now
use.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.brain.engine import Action  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.permissions import EXECUTOR, PermissionError_  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

overall = True


def check(label: str, ok: bool, detail: str = "") -> bool:
    global overall
    overall &= ok
    suffix = f" -> {detail}" if detail else ""
    print(f"  {'OK  ' if ok else 'MISS'} {label}{suffix}")
    return ok


def note(label: str, detail: str = "") -> None:
    suffix = f" -> {detail}" if detail else ""
    print(f"  NOTE {label}{suffix}")


def reset_session() -> None:
    SESSION.pending = None
    SESSION.last_skill = None
    SESSION.last_args = {}
    INTEL.reset()


import contextlib  # noqa: E402


@contextlib.contextmanager
def isolated_file_root(files: dict[str, str]):
    """Copied from scripts/agent_reliability.py / scripts/smoke_intent_routing.py
    (this codebase's own "not reinvented" convention) — a harness-owned
    temp dir as the only root files.search/files.reveal touch, and a
    tracking stub in place of the real `subprocess.Popen(explorer ...)`
    call, so no real Explorer window ever opens."""
    import friday.skills.files as files_mod

    tmp = tempfile.mkdtemp(prefix="friday-continuity-")
    for name, content in files.items():
        (Path(tmp) / name).write_text(content, encoding="utf-8")

    orig_roots = files_mod._roots
    files_mod._roots = lambda: [Path(tmp)]
    reveal_calls: list[str] = []
    orig_popen = subprocess.Popen

    def fake_popen(args, *a, **kw):
        reveal_calls.append(str(args[-1]) if isinstance(args, (list, tuple)) else str(args))
        return None

    subprocess.Popen = fake_popen
    try:
        yield tmp, reveal_calls
    finally:
        files_mod._roots = orig_roots
        subprocess.Popen = orig_popen


def _register_test_skills() -> None:
    """Test-only stand-ins, registered once, idempotent — same precedent as
    scripts/agent_reliability.py's scenarios I/J/K and scripts/smoke_goal_
    decomposition.py's test.gd_wa_* skills. No real side effect anywhere:
    each just appends to a local list this file inspects."""

    if REGISTRY.get("test.ac_safe") is None:
        @skill(name="test.ac_safe", tier="L0", description="test-only safe/repeatable action")
        def _safe(label: Annotated[str, "label"] = "x") -> SkillResult:
            SAFE_CALLS.append(label)
            return SkillResult(speech=f"did {label}")

    if REGISTRY.get("test.ac_failing") is None:
        @skill(name="test.ac_failing", tier="L0", description="test-only action that always fails")
        def _failing() -> SkillResult:
            FAILING_CALLS.append(True)
            return SkillResult(speech="that didn't work", ok=False)

    if REGISTRY.get("test.ac_consequential") is None:
        @skill(
            name="test.ac_consequential", tier="L1",
            description="test-only consequential action requiring confirmation",
            risk=lambda **kw: True,
        )
        def _consequential(target: Annotated[str, "target"] = "send") -> SkillResult:
            CONSEQUENTIAL_CALLS.append(target)
            return SkillResult(speech=f"did the consequential thing: {target}")

    if REGISTRY.get("test.ac_step_a") is None:
        @skill(name="test.ac_step_a", tier="L0", description="test-only multi-step goal, step a")
        def _step_a() -> SkillResult:
            return SkillResult(speech="step a done")

    if REGISTRY.get("test.ac_step_b") is None:
        @skill(name="test.ac_step_b", tier="L0", description="test-only multi-step goal, step b")
        def _step_b() -> SkillResult:
            return SkillResult(speech="step b done")


SAFE_CALLS: list[str] = []
FAILING_CALLS: list[bool] = []
CONSEQUENTIAL_CALLS: list[str] = []


# ==============================================================================
# A. RECORDING — real outcomes land in INTEL.state.action_log
# ==============================================================================


async def section_a_recording() -> None:
    print("\n--- A. RECORDING ---\n")

    # A1. A successful direct action is recorded with status="success".
    reset_session()
    with isolated_file_root({"note.txt": "hello"}) as (tmp, _reveal):
        path = str(Path(tmp) / "note.txt")
        result = await SESSION.handle(f"Reveal this file: {path}.", actor="test")
        entry = INTEL.last_action()
        check(
            "successful direct action recorded, status=success",
            result.ok and entry is not None and entry["tool"] == "files.reveal" and entry["status"] == "success",
            f"entry={entry}",
        )
        check(
            "recorded args are a bounded, non-empty summary (not the raw dict)",
            entry is not None and isinstance(entry["args"], str) and "note.txt" in entry["args"],
            f"args={entry['args'] if entry else None}",
        )

    # A2. A failed action (ok=False, no exception) is recorded as "failed".
    reset_session()
    r2 = await EXECUTOR.run("test.ac_failing", {}, actor="test")
    entry2 = INTEL.last_action()
    check(
        "failed action recorded, status=failed",
        not r2.ok and entry2 is not None and entry2["tool"] == "test.ac_failing" and entry2["status"] == "failed",
        f"entry={entry2}",
    )

    # A3. A permission denial is recorded as "permission_denied", and the
    # skill's own side effect never ran.
    reset_session()
    from friday.config import CFG

    CFG.permissions.overrides["test.ac_safe"] = "deny"
    calls_before = len(SAFE_CALLS)
    try:
        await EXECUTOR.run("test.ac_safe", {"label": "should-not-run"}, actor="test")
        denied = False
    except PermissionError_:
        denied = True
    finally:
        del CFG.permissions.overrides["test.ac_safe"]
    entry3 = INTEL.last_action()
    check(
        "permission denial recorded, status=permission_denied, no side effect",
        denied and entry3 is not None and entry3["status"] == "permission_denied"
        and len(SAFE_CALLS) == calls_before,
        f"entry={entry3}",
    )

    # A4. A declined confirmation is recorded as "confirmation_declined".
    reset_session()

    async def decline(skill_obj, args, preview) -> bool:
        return False

    EXECUTOR.set_confirm_handler(decline)
    try:
        r4 = await EXECUTOR.run("test.ac_consequential", {"target": "x"}, actor="test")
    finally:
        EXECUTOR.set_confirm_handler(SESSION._confirm)
    entry4 = INTEL.last_action()
    check(
        "confirmation decline recorded, status=confirmation_declined",
        not r4.ok and entry4 is not None and entry4["status"] == "confirmation_declined",
        f"entry={entry4}",
    )


# ==============================================================================
# B. REPEAT — "do that again" actually works from a fresh session
# ==============================================================================


async def section_b_repeat() -> None:
    print("\n--- B. REPEAT ---\n")

    # B1. A fresh session (INTEL.reset(), no manual record_action call) can
    # repeat a safe action end to end through plain SESSION.handle() calls.
    reset_session()
    with isolated_file_root({"a.txt": "a"}) as (tmp, reveal_calls):
        path = str(Path(tmp) / "a.txt")
        await SESSION.handle(f"Reveal this file: {path}.", actor="test")
        calls_before = len(reveal_calls)
        again = await SESSION.handle("Do that again.", actor="test")
        check(
            "fresh session: 'do that again' repeats the safe action (no manual assist)",
            again.ok and SESSION.last_skill == "files.reveal" and len(reveal_calls) == calls_before + 1,
            f"speech={again.speech!r}",
        )

        # B2. The repeated action itself gets its own new action_log entry.
        log_before = len(INTEL.state.action_log)
        again2 = await SESSION.handle("Do that again.", actor="test")
        check(
            "each repeat produces its own new action_log entry",
            again2.ok and len(INTEL.state.action_log) == log_before + 1,
            f"log_len {log_before} -> {len(INTEL.state.action_log)}",
        )

    # B3. After two DIFFERENT safe actions, "do that again" targets the
    # most recently executed one, not the first.
    reset_session()
    with isolated_file_root({"one.txt": "1", "two.txt": "2"}) as (tmp, reveal_calls):
        p1, p2 = str(Path(tmp) / "one.txt"), str(Path(tmp) / "two.txt")
        await SESSION.handle(f"Reveal this file: {p1}.", actor="test")
        await SESSION.handle(f"Reveal this file: {p2}.", actor="test")
        calls_before = len(reveal_calls)
        again = await SESSION.handle("Do that again.", actor="test")
        # Paths round-trip through friday.brain.normalize (lowercased) —
        # compare case-insensitively, matching this pipeline's real,
        # pre-existing behavior rather than exact string identity.
        last_path = str(SESSION.last_args.get("path", "")).lower()
        check(
            "latest action (two.txt), not the first (one.txt), becomes the repeat target",
            again.ok and last_path == p2.lower() and reveal_calls[-1].lower() == p2.lower()
            and len(reveal_calls) == calls_before + 1,
            f"last_args={SESSION.last_args}",
        )


# ==============================================================================
# C. CONTEXTUAL REPEAT — open -> read it -> repeat targets the read, not the open
# ==============================================================================


async def section_c_contextual_repeat() -> None:
    print("\n--- C. CONTEXTUAL REPEAT ---\n")

    reset_session()
    with isolated_file_root({"report.pdf": "Report contents."}) as (tmp, reveal_calls):
        path = str(Path(tmp) / "report.pdf")
        await SESSION.handle(f"Reveal this file: {path}.", actor="test")
        check("open -> files.reveal", SESSION.last_skill == "files.reveal")

        read1 = await SESSION.handle("Read it.", actor="test")
        check(
            "'Read it.' -> files.read (the file just revealed)",
            SESSION.last_skill == "files.read" and read1.ok
            and "Report contents" in read1.data.get("content", ""),
            f"content={read1.data.get('content')!r}",
        )

        again = await SESSION.handle("Do that again.", actor="test")
        check(
            "'Do that again.' repeats files.read (the LATEST action), not files.reveal",
            again.ok and SESSION.last_skill == "files.read"
            and "Report contents" in again.data.get("content", ""),
            f"last_skill={SESSION.last_skill} content={again.data.get('content')!r}",
        )


# ==============================================================================
# D. CORRECTION — a correction steers the repeat target
# ==============================================================================


async def section_d_correction() -> None:
    print("\n--- D. CORRECTION ---\n")

    reset_session()
    with isolated_file_root({"report.pdf": "Report contents.", "summary.pdf": "Summary contents."}) as (tmp, _):
        p1, p2 = str(Path(tmp) / "report.pdf"), str(Path(tmp) / "summary.pdf")
        await SESSION.handle(f"Reveal this file: {p1}.", actor="test")
        # Paths round-trip through friday.brain.normalize (lowercased) —
        # compare case-insensitively throughout this section.
        check(
            "open report.pdf -> files.reveal(report.pdf)",
            str(SESSION.last_args.get("path", "")).lower() == p1.lower(),
        )

        # A correction phrasing that is ALSO independently actionable (per
        # friday.session's own design: a correction never short-circuits
        # normal understanding of the same text — see
        # Session._record_correction_if_any's docstring) so the corrected
        # reveal genuinely re-runs through _run, updating last_skill/
        # last_args to the NEW target — exactly what "do that again" reads.
        await SESSION.handle(f"No, I meant reveal this file: {p2} instead.", actor="test")
        note("correction turn itself", f"last_skill={SESSION.last_skill} (bookkeeping is what matters)")

        again = await SESSION.handle("Do that again.", actor="test")
        check(
            "open report.pdf -> correct to summary.pdf -> 'do that again' repeats summary.pdf",
            again.ok and str(SESSION.last_args.get("path", "")).lower() == p2.lower(),
            f"last_args={SESSION.last_args}",
        )


# ==============================================================================
# E. SAFETY — repeat never bypasses confirmation or a denial
# ==============================================================================


async def section_e_safety() -> None:
    print("\n--- E. SAFETY ---\n")

    # E1. A consequential action requiring confirmation still asks for
    # confirmation on every repeat, not just the first time.
    reset_session()
    confirm_calls: list[str] = []

    async def recording_confirm(skill_obj, args, preview) -> bool:
        confirm_calls.append(skill_obj.name)
        return True

    EXECUTOR.set_confirm_handler(recording_confirm)
    try:
        r1 = await SESSION._run("test.ac_consequential", {"target": "send-1"}, actor="text")
        check("first consequential call asks for confirmation", r1.ok and confirm_calls == ["test.ac_consequential"])

        r2 = await SESSION._maybe_repeat_last_action("do that again", actor="text")
        check(
            "repeat of a consequential action asks for confirmation AGAIN (not bypassed)",
            r2 is not None and r2.ok and confirm_calls == ["test.ac_consequential", "test.ac_consequential"],
            f"confirm_calls={confirm_calls}",
        )
    finally:
        EXECUTOR.set_confirm_handler(SESSION._confirm)

    # E2. A denied action's repeat is denied again — never bypassed via a
    # cached/stale approval.
    reset_session()

    async def declining_confirm(skill_obj, args, preview) -> bool:
        return False

    EXECUTOR.set_confirm_handler(declining_confirm)
    try:
        r1 = await SESSION._run("test.ac_consequential", {"target": "send-2"}, actor="text")
        check("first attempt declined", not r1.ok and r1.data.get("confirmation_declined") is True)

        calls_before = len(CONSEQUENTIAL_CALLS)
        r2 = await SESSION._maybe_repeat_last_action("do that again", actor="text")
        check(
            "'do that again' on a declined action is declined again, side effect never runs",
            r2 is not None and not r2.ok and len(CONSEQUENTIAL_CALLS) == calls_before,
            f"speech={r2.speech if r2 else None}",
        )
    finally:
        EXECUTOR.set_confirm_handler(SESSION._confirm)

    # E3. A hard permission denial (not a confirmation) is also never
    # bypassed by repeating it.
    reset_session()
    from friday.config import CFG

    CFG.permissions.overrides["test.ac_safe"] = "deny"
    try:
        try:
            await SESSION._run("test.ac_safe", {"label": "denied-once"}, actor="text")
        except Exception:
            pass
        again = await SESSION._maybe_repeat_last_action("do that again", actor="text")
        ok_denied_again = again is not None and not again.ok
        check(
            "'do that again' on a hard-denied action stays denied, never bypasses policy",
            ok_denied_again, f"speech={again.speech if again else None}",
        )
    finally:
        del CFG.permissions.overrides["test.ac_safe"]


# ==============================================================================
# F. MULTI-STEP — individual actions recorded, goal relationship preserved
# ==============================================================================


async def section_f_multi_step() -> None:
    print("\n--- F. MULTI-STEP ---\n")

    reset_session()
    goal_id = "smoke-ac-goal-001"
    INTEL.start_goal("a compound test goal", goal_id)

    await EXECUTOR.run("test.ac_step_a", {}, actor="orchestrator")
    await EXECUTOR.run("test.ac_step_b", {}, actor="orchestrator")

    log = list(INTEL.state.action_log)
    last_two = log[-2:]
    check(
        "a compound goal's two steps are recorded as two SEPARATE entries (not one)",
        len(last_two) == 2 and last_two[0]["tool"] == "test.ac_step_a" and last_two[1]["tool"] == "test.ac_step_b",
        f"tools={[e['tool'] for e in last_two]}",
    )
    check(
        "both step entries retain the active goal's id",
        all(e["goal_id"] == goal_id for e in last_two),
        f"goal_ids={[e['goal_id'] for e in last_two]}",
    )

    INTEL.end_goal(ok=True)
    reset_session()
    await EXECUTOR.run("test.ac_step_a", {}, actor="text")
    solo_entry = INTEL.last_action()
    check(
        "an ordinary single command outside any goal has no goal_id",
        solo_entry is not None and solo_entry["goal_id"] is None,
        f"entry={solo_entry}",
    )


# ==============================================================================
# G. ROUTING — focus-verb tie-break and compound-goal -> plan.run
# ==============================================================================


async def section_g_routing() -> None:
    print("\n--- G. ROUTING ---\n")

    for text in ["focus VS Code", "focus Chrome", "focus Notepad"]:
        u = BRAIN.understand(text)
        check(f"{text!r} -> apps.focus", u.skill == "apps.focus", f"{u.skill} ({u.score:.2f})")

    reset_session()
    await SESSION.handle("Open Chrome and search YouTube for cats.", actor="test")
    check(
        "compound browser goal -> plan.run",
        SESSION.last_skill == "plan.run", f"last_skill={SESSION.last_skill}",
    )

    reset_session()
    await SESSION.handle("Find my report and open it.", actor="test")
    check(
        "compound file goal -> plan.run",
        SESSION.last_skill == "plan.run", f"last_skill={SESSION.last_skill}",
    )

    # Simple commands are never dragged through plan.run.
    reset_session()
    await SESSION.handle("Open Chrome.", actor="test")
    check("simple 'Open Chrome.' stays direct", SESSION.last_skill == "apps.open", f"last_skill={SESSION.last_skill}")


# ==============================================================================
# H. DATABASE ISOLATION — friday.store.use_temp_db
# ==============================================================================


async def section_h_db_isolation() -> None:
    print("\n--- H. DATABASE ISOLATION ---\n")

    from friday.intelligence import episodes

    real_path_before = store.paths.DB_PATH
    real_count_before = len(episodes.all_episodes())

    tag = "smoke_ac_isolation_probe"
    seen_counts: list[int] = []
    for i in range(2):
        with store.use_temp_db():
            store.init()
            count_at_start = len(episodes.all_episodes())
            episodes.record(
                f"{tag} run {i}", goal_id=None, context="", steps=[],
                stopped="completed", ok=True, duration_ms=1,
            )
            seen_counts.append(count_at_start)
            check(
                f"isolated DB run {i}: starts empty regardless of prior real-DB or prior isolated-run state",
                count_at_start == 0, f"count={count_at_start}",
            )

    check(
        "repeated isolated runs never accumulate into each other (each starts fresh)",
        seen_counts == [0, 0], f"seen_counts={seen_counts}",
    )
    check(
        "the real store.paths.DB_PATH is restored after each isolated block",
        store.paths.DB_PATH == real_path_before, f"path={store.paths.DB_PATH}",
    )
    check(
        "the real database's episode count is unaffected by the isolated writes above",
        len(episodes.all_episodes()) == real_count_before,
        f"before={real_count_before} after={len(episodes.all_episodes())}",
    )


async def main() -> None:
    REGISTRY.discover()
    BRAIN.warm()
    _register_test_skills()

    await section_a_recording()
    await section_b_repeat()
    await section_c_contextual_repeat()
    await section_d_correction()
    await section_e_safety()
    await section_f_multi_step()
    await section_g_routing()
    await section_h_db_isolation()

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
