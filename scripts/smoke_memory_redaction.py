"""Phase 25 (Workstream 4) — regression coverage for memory secret redaction.

The completion audit found `friday.memory.remember()` had NO redaction at all, unlike
every other subsystem that can see model/tool text (friday.toolview, used by the
orchestrator's evidence and by context_memory/episodes/experience) — despite the skill's
own canonical example being "remember my wifi password".

The fix reuses the existing mechanism: `toolview.sanitize(value, prose=True)` (Phase 25.0
adds the `prose` pass to `friday/toolview.py` itself, so this is the SAME module/pattern
set every other redaction call site already uses, not a second framework) is applied in
`friday.memory.remember()` before a value is embedded and stored, and again in the
`memory.remember`/`memory.recall` SKILLS (`friday/skills/memory.py`) so the spoken
confirmation never echoes back the raw secret it just declined to store.

This suite pins:

  A  a secret-like value is redacted before it reaches SQLite (never plaintext on disk)
  B  ...and the skill's own spoken confirmation does not leak it either (found live: the
     confirmation used to build its speech from the raw pre-storage value)
  C  ordinary (non-secret) memories are stored and recalled byte-for-byte unchanged
  D  semantic recall still finds a redacted memory (it's forgotten as a fact WHAT it is,
     never made unfindable) — asking about it returns the redacted placeholder, not silence
  E  redaction is stable under repeated storage (idempotent — storing an already-redacted
     value again does not add a second marker)

Real friday.memory + friday.store (throwaway DB) + the real friday.skills.memory skills
through Session.handle. No Ollama, no network.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday import memory, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402


async def main_async() -> int:
    t0 = time.perf_counter()
    store.init()
    REGISTRY.discover()
    BRAIN.warm()

    with store.use_temp_db():
        for row in memory.all_memories(limit=500):
            memory.forget(row["id"])

        scenario("A: a secret-like value is redacted before it reaches SQLite")
        memory.remember("my wifi password is upstairs123", kind="fact")
        stored = memory.all_memories()[0]["value"]
        check("the password is not stored in plaintext", "upstairs123" not in stored, stored)
        check("the redaction marker is present", "[redacted]" in stored, stored)

        memory.remember("my github token is ghp_abcdefghijklmnopqrstuvwxyz123456", kind="fact")
        stored2 = memory.all_memories()[0]["value"]
        check("a github token is not stored in plaintext", "ghp_abcdefghijklmnopqrstuvwxyz123456" not in stored2, stored2)

        memory.remember("my api key is sk-abcdefghijklmnop1234", kind="fact")
        stored3 = memory.all_memories()[0]["value"]
        check("an api key (structured, no 'is' needed) is not stored in plaintext",
              "sk-abcdefghijklmnop1234" not in stored3, stored3)

        scenario("B: the spoken confirmation does not leak the secret either")
        result = await SESSION.handle("remember my bank password is hunter2000", actor="text")
        check("confirmation speech does not contain the raw secret", "hunter2000" not in result.speech, result.speech)
        check("confirmation speech shows the redaction happened", "[redacted]" in result.speech, result.speech)
        stored4 = memory.all_memories()[0]["value"]
        check("...and what got stored matches what was said (same value, not a second copy)", stored4 in result.speech, f"{stored4!r} / {result.speech!r}")

        scenario("C: ordinary (non-secret) memories are unaffected — stored and recalled verbatim")
        result_ok = await SESSION.handle("remember that my sister priya lives in chennai", actor="text")
        check("an ordinary fact is stored", result_ok.ok, result_ok.speech)
        check("...unchanged, no redaction marker", "[redacted]" not in result_ok.speech, result_ok.speech)
        recall_ok = await SESSION.handle("where does my sister live", actor="text")
        check("ordinary recall still finds it", "chennai" in recall_ok.speech.lower(), recall_ok.speech)

        scenario("D: semantic recall still finds a redacted memory — it's WHAT is hidden, not THAT it exists")
        recall_secret = await SESSION.handle("what is my bank password", actor="text")
        check("recall finds the memory (doesn't just silently fail)", recall_secret.ok, recall_secret.speech)
        check("...but the redacted placeholder, not the real secret", "hunter2000" not in recall_secret.speech, recall_secret.speech)

        scenario("E: redaction is idempotent under repeated storage")
        once = memory.all_memories()[2]["value"]  # the wifi password row, oldest of the three A memories
        memory.remember(once, kind="fact")  # store the already-redacted text again
        twice = memory.all_memories()[0]["value"]
        check("re-storing an already-redacted value adds no second marker", twice.count("[redacted]") <= 1, twice)
        check("re-storing an already-redacted value is unchanged", twice == once, f"{once!r} -> {twice!r}")

        scenario("F: a weakly-similar memory that shares no content word does not answer an unrelated question")
        from friday.skills import memory as memory_skill

        check("0.57 'blood type' vs 'my sister's name is priya' is refused",
              not memory_skill.grounded_hit("what's my blood type", "my sister's name is priya", 0.57))
        check("0.61 'favorite color' vs 'i prefer dark mode in every app' is refused",
              not memory_skill.grounded_hit("what's my favorite color", "i prefer dark mode in every app", 0.61))
        check("a weak hit that shares a content word is kept",
              memory_skill.grounded_hit("what theme do i prefer", "i prefer dark mode in every app", 0.57))
        check("a solid similarity is believed without a shared word",
              memory_skill.grounded_hit("what is my sibling called", "my sister's name is priya", 0.71))
        blood = await SESSION.handle("what is my blood type", actor="text")
        check("an untold personal fact is declined, not answered with an unrelated memory",
              not blood.ok and "priya" not in blood.speech.lower() and "chennai" not in blood.speech.lower(), blood.speech)

        scenario("G: Phase 37 — a question about the user is answered from memory or declined, never by the clock / weather / a timer")
        bday = await SESSION.handle("when is my mother's birthday", actor="text")
        check("an untold birthday is declined (not the current time)", not bday.ok and "priya" not in bday.speech.lower(), bday.speech)
        allergy = await SESSION.handle("do i have any allergies", actor="text")
        check("an untold allergy is declined (not a weather report)", not allergy.ok, allergy.speech)
        locker = await SESSION.handle("remind me what my locker number is", actor="text")
        check("an untold locker number is declined and sets no timer", not locker.ok and "timer" not in locker.speech.lower(), locker.speech)
        await SESSION.handle("remember my mother's birthday is march 3rd", actor="text")
        told = await SESSION.handle("when is my mother's birthday", actor="text")
        check("once told, the birthday is recalled", "march" in told.speech.lower(), told.speech)

        scenario("H: Phase 38 — 'my laptop's name' / 'I forgot my blood type' are memory questions; system questions are not")
        from friday.registry import SkillResult  # noqa: E402

        # No Ollama: any phrase that falls through to the planner (plan.run) is intercepted here.
        planner_goals: list[str] = []
        real_run = SESSION._run

        async def _no_planner(skill_name, args, *, actor):
            if skill_name == "plan.run":
                planner_goals.append(str(args.get("goal", "")))
                return SkillResult(speech="(planner disabled in this smoke)", ok=False)
            return await real_run(skill_name, args, actor=actor)

        SESSION._run = _no_planner  # type: ignore[method-assign]
        try:
            for row in memory.all_memories(limit=500):
                memory.forget(row["id"])
            memory.remember("my sister priya lives in chennai", kind="fact")
            memory.remember("my favorite color is teal", kind="fact")

            laptop_q = await SESSION.handle("what is my laptop's name?", actor="text")
            check("H1 untold laptop name is declined", not laptop_q.ok, laptop_q.speech)
            check("H1 ...and does not return an unrelated stored fact",
                  not any(w in laptop_q.speech.lower() for w in ("priya", "chennai", "teal")), laptop_q.speech)
            check("H1 ...via memory.recall, not the planner", SESSION.last_skill == "memory.recall" and not planner_goals,
                  f"{SESSION.last_skill!r} planner={planner_goals}")
            blood_q = await SESSION.handle("I forgot my blood type, what was it?", actor="text")
            check("H2 untold blood type ('I forgot' frame) is declined", not blood_q.ok, blood_q.speech)
            check("H2 ...and does not return an unrelated stored fact",
                  not any(w in blood_q.speech.lower() for w in ("priya", "chennai", "teal")), blood_q.speech)
            check("H2 ...via memory.recall, not the planner", SESSION.last_skill == "memory.recall" and not planner_goals,
                  f"{SESSION.last_skill!r} planner={planner_goals}")

            stored_laptop = await SESSION.handle("remember my laptop's name is orion-test", actor="text")
            if not (stored_laptop.ok and any("orion" in m["value"].lower() for m in memory.all_memories(limit=500))):
                print(f"  NOTE: 'remember my laptop's name ...' did not store via SESSION ({stored_laptop.speech!r}); using memory.remember directly")
                memory.remember("my laptop's name is orion-test", kind="fact")
            else:
                print("  NOTE: laptop fact was stored through SESSION.handle")
            stored_blood = await SESSION.handle("remember my blood type is O positive", actor="text")
            if not (stored_blood.ok and any("positive" in m["value"].lower() for m in memory.all_memories(limit=500))):
                print(f"  NOTE: 'remember my blood type ...' did not store via SESSION ({stored_blood.speech!r}); using memory.remember directly")
                memory.remember("my blood type is O positive", kind="fact")
            else:
                print("  NOTE: blood-type fact was stored through SESSION.handle")

            laptop_told = await SESSION.handle("what is my laptop's name?", actor="text")
            check("H3 once told, the laptop name is recalled", "orion" in laptop_told.speech.lower(), laptop_told.speech)
            blood_told = await SESSION.handle("I forgot my blood type, what was it?", actor="text")
            check("H3 once told, the blood type is recalled via the 'I forgot' frame", "positive" in blood_told.speech.lower(), blood_told.speech)

            planner_goals.clear()
            for question in ("what is my laptop's battery level?", "what is my cpu usage"):
                res = await SESSION.handle(question, actor="text")
                low = res.speech.lower()
                check(f"H4 {question!r} does not return a stored memory fact", "orion" not in low and "positive" not in low, res.speech)
                check(f"H4 {question!r} is not routed to memory.recall", SESSION.last_skill != "memory.recall",
                      f"{SESSION.last_skill!r}")
            check("H5 'remember my laptop's name ...' was stored through SESSION (deterministic route, no planner)",
                  stored_laptop.ok and not planner_goals, f"{stored_laptop.speech!r} planner={planner_goals}")
            check("H5 'remember my blood type ...' was stored through SESSION (deterministic route, no planner)",
                  stored_blood.ok and not planner_goals, f"{stored_blood.speech!r} planner={planner_goals}")
        finally:
            SESSION._run = real_run  # type: ignore[method-assign]

        scenario("I: Phase 38 — statements and 'I forgot ...' delete nothing and store nothing; explicit writes keep the value; explicit forgets still work")
        for row in memory.all_memories(limit=500):
            memory.forget(row["id"])
        memory.remember("my umbrella is red", kind="fact")
        memory.remember("my old address is 12 test lane", kind="fact")
        before = sorted(m["value"] for m in memory.all_memories(limit=500))
        for phrase in ("I forgot my umbrella", "my cat's name is Tom", "my blood type is O positive"):
            res = await SESSION.handle(phrase, actor="text")
            check(f"I1 {phrase!r} is declined (not ok) with an explanation", not res.ok and bool(res.speech), res.speech)
            check(f"I1 ...and the stored memories are exactly as they were",
                  sorted(m["value"] for m in memory.all_memories(limit=500)) == before, f"{phrase!r}")
        for query in ("I forgot my umbrella", "i forgot my old address", "don't forget my umbrella", "I've forgotten my umbrella"):
            res = memory_skill.forget(query)
            check(f"I2 memory.forget({query!r}) refuses and deletes nothing", not res.ok and res.data.get("deleted") is False
                  and sorted(m["value"] for m in memory.all_memories(limit=500)) == before, res.speech)
        res = memory_skill.forget("forget my old address is 12 test lane")
        remaining = [m["value"] for m in memory.all_memories(limit=500)]
        check("I3 an explicit forget still removes the matching synthetic memory", res.ok and "my old address is 12 test lane" not in remaining
              and "my umbrella is red" in remaining, f"{res.speech!r} {remaining}")
        res = await SESSION.handle("Remember my blood type is O+.", actor="text")
        check("I4 'Remember my blood type is O+.' is stored, the plus sign intact", res.ok and any("blood type is O+" in m["value"] for m in memory.all_memories(limit=500)),
              f"{res.speech!r} {[m['value'] for m in memory.all_memories(limit=500)]}")

        scenario("J: Phase 38 — an explicit memory forget is deterministic AND confirmed + audited; 'I forgot' / 'don't forget' touch nothing")
        from friday import audit  # noqa: E402
        from friday.permissions import EXECUTOR  # noqa: E402

        for row in memory.all_memories(limit=500):
            memory.forget(row["id"])
        for fact in ("my cat's name is Tom", "my old address is 12 test lane", "my umbrella is red"):
            memory.remember(fact, kind="fact")
        asked: list[str] = []
        answer = {"yes": False}

        async def _confirm(skill, args, preview):
            asked.append(preview)
            return answer["yes"]

        real_confirm = EXECUTOR._confirm
        EXECUTOR.set_confirm_handler(_confirm)
        planner_goals = []
        real_run = SESSION._run

        async def _no_planner(skill_name, args, *, actor):
            if skill_name == "plan.run":
                planner_goals.append(str(args.get("goal", "")))
                return SkillResult(speech="(planner disabled in this smoke)", ok=False)
            return await real_run(skill_name, args, actor=actor)

        SESSION._run = _no_planner  # type: ignore[method-assign]
        try:
            def values() -> list[str]:
                return sorted(m["value"] for m in memory.all_memories(limit=500))

            start = values()
            res = await SESSION.handle("forget that my cat is named Tom", actor="text")
            check("J1 an explicit forget goes straight to memory.forget (no planner) and asks for confirmation",
                  SESSION.last_skill == "memory.forget" and not planner_goals and len(asked) == 1, f"{SESSION.last_skill!r} planner={planner_goals} asked={asked}")
            check("J2 ...declined: nothing is deleted", values() == start, f"{values()} {res.speech!r}")
            answer["yes"] = True
            res = await SESSION.handle("forget that my cat is named Tom", actor="text")
            check("J3 ...approved: only the matching memory is deleted", res.ok and values() == [v for v in start if "cat" not in v], f"{values()} {res.speech!r}")
            check("J4 ...and the audit trail names memory.forget", (audit.recent(1) or [{}])[0].get("skill") == "memory.forget", str(audit.recent(1)[:1]))
            res = await SESSION.handle("forget the fact I told you about my old address", actor="text")
            check("J5 'forget the fact I told you about my old address' is a confirmed memory.forget that deletes only the address",
                  res.ok and SESSION.last_skill == "memory.forget" and values() == ["my umbrella is red"] and not planner_goals, f"{values()} {SESSION.last_skill!r} {res.speech!r}")
            memory.remember("my cat's name is Tom", kind="fact")
            now, asked_before = values(), len(asked)
            for phrase in ("I forgot my umbrella", "don't forget my umbrella"):
                res = await SESSION.handle(phrase, actor="text")
                check(f"J6 {phrase!r} stores nothing, deletes nothing and asks no confirmation",
                      values() == now and len(asked) == asked_before and not res.ok and not planner_goals, f"{values()} {res.speech!r} planner={planner_goals}")
        finally:
            SESSION._run = real_run  # type: ignore[method-assign]
            EXECUTOR.set_confirm_handler(real_confirm)

    return finish("Phase 25 — memory secret redaction", time.perf_counter() - t0, min_assertions=56, min_scenarios=10)


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    import iso_env  # noqa: E402  (Phase 35: throwaway database + recovery store)

    with iso_env.isolated_runtime():
        sys.exit(main())
