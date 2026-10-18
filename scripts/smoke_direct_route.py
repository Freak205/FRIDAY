"""Phase 30.0 - direct-route safety and verification.

Deterministic: no Ollama, no real browser, nothing on the real machine is changed (the executor is
replaced by a recorder wherever a routing decision, not an effect, is what is under test; the
verification tests use real file skills on a private temp folder and a temp database).

Sections
  1  complete direct-route arguments stay direct                     (directroute.assess, synthetic)
  2  missing / unreadable arguments -> planner, a bare request still asks
  3  compound request -> planner
  4  whole sentence (or the skill's own verb) as an argument -> planner
  5  ambiguous skill choice -> planner (and an evidence tie-break is NOT ambiguity)
  6  the same, end to end through the real BRAIN and Session.handle with a stubbed planner
  7  the 28 Phase 29 benchmark prompts: which stay direct, which go to the planner
  8  legitimate single-action requests (the labelled routing suites) still route directly
  9  a direct call is verified like a planned step: success / failure / unverifiable / untouched
 10  a truthful final answer after a direct-route failure (through Session.handle)
 11  kill switches, planner unavailable
 8b  Phase 30.1: an explicit "remember ..." is a STORE (memory.remember, direct and safe, redacted, audited as
     the real write); recall questions never write; a clear knowledge question stays on knowledge.ask
 12  MUTATION TESTING: each check and each safety mechanism is disabled in turn; the suite must fail

Run:  .venv/Scripts/python.exe -X utf8 scripts/smoke_direct_route.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from friday import directroute, store  # noqa: E402
from friday.brain.engine import Action, Understanding  # noqa: E402
from friday.brain.extract import can_fill, extract, parses_own_input  # noqa: E402
from friday.brain.matcher import Match  # noqa: E402

LOOP = asyncio.new_event_loop()


class Rec:
    results: list[tuple[str, bool]] = []
    quiet = False


def check(label: str, ok: bool, detail: str = "") -> bool:
    Rec.results.append((label, bool(ok)))
    if not Rec.quiet:
        suffix = f" -> {detail}" if detail else ""
        print(f"  {'OK  ' if ok else 'MISS'} {label}{suffix}")
    return bool(ok)


def say(text: str) -> None:
    if not Rec.quiet:
        print(text)


def run_suite(fn, *, quiet: bool = False) -> int:
    """Run one suite; return how many of its checks failed."""
    Rec.results, Rec.quiet = [], quiet
    try:
        result = fn()
        if asyncio.iscoroutine(result):
            LOOP.run_until_complete(result)
    finally:
        Rec.quiet = False
    return sum(1 for _l, ok in Rec.results if not ok)


# -- fixtures -------------------------------------------------------------------------------------------

P = r"C:\Users\user\AppData\Local\Temp\fdr30\work"  # a string only; unit sections never touch the disk


def U(skill, args, text, *, score=0.80, action=Action.ACT, missing=(), candidates=()) -> Understanding:
    return Understanding(
        action=action, utterance=text, normalized=text.lower().strip(" ."), skill=skill, args=dict(args),
        score=score, missing=list(missing), candidates=list(candidates),
    )


def verdict(u: Understanding):
    return directroute.assess(u.utterance, u)


def has(a, code: str) -> bool:
    return any(r.startswith(code) for r in a.reasons)


# ==========================================================================================================
# 1-5  the gate, on synthetic understandings (independent of any embedding score)
# ==========================================================================================================


def suite_gate() -> None:
    say("\n--- 1. complete direct-route arguments stay direct ---\n")
    for label, u in [
        ("system.time, no arguments", U("system.time", {}, "What time is it?", score=1.0)),
        ("files.delete with the full path it was given", U("files.delete", {"path": P.lower() + r"\old_draft.txt"}, rf"Delete the file {P}\old_draft.txt.", score=0.86)),
        ("apps.open 'chrome'", U("apps.open", {"app": "chrome"}, "Open Chrome.", score=1.0)),
        ("web.search with a query that is most of the sentence", U("web.search", {"query": "how to cook rice quickly"}, "search for how to cook rice quickly", score=0.8)),
        ("math.calculate takes the phrase on purpose", U("math.calculate", {"expression": "what's 12 plus 30"}, "what is 12 plus 30", score=0.71)),
        ("schedule.create parses its own phrasing", U("schedule.create", {"command": "tell me my battery", "when": "every day at 8am"}, "every day at 8am tell me my battery", score=1.0)),
        ("system.volume.set with its number", U("system.volume.set", {"level": 40}, "set volume to 40", score=0.93)),
        ("a bare 'copy a file' still ASKS (a planner would have nothing to work from)", U("files.copy", {}, "copy a file", score=0.9, action=Action.ASK_SLOT, missing=["src", "dst"])),
        ("an omitted but readable argument is still asked about", U("apps.close", {}, "close", score=0.9, action=Action.ASK_SLOT, missing=["app"])),
        ("a skill's own taught phrasing is not an action mismatch", U("math.calculate", {"expression": "add 340 and 195"}, "add 340 and 195", score=1.0)),
    ]:
        a = verdict(u)
        check(label, a.safe, a.reason)

    say("\n--- 2. missing / unreadable arguments -> planner ---\n")
    a = verdict(U("files.mkdir", {"path": P.lower()}, "In " + P + ", put photo1.jpg and photo2.jpg into a new folder called Pictures.", score=0.81))
    check("an operand the route dropped (photo1.jpg) is caught", has(a, "unconsumed_operand"), a.reason)
    a = verdict(U("screen.observe", {}, rf"What port is set in {P}\config.txt?", score=0.65))
    check("'what port is in <path>' -> screen.observe drops the path", has(a, "unconsumed_operand"), a.reason)
    a = verdict(U("files.copy", {}, rf"Copy {P}\a.txt to {P}\b.txt.", score=0.78, action=Action.ASK_SLOT, missing=["src", "dst"]))
    check("'Which src?' for a request that names both paths", has(a, "unfillable_slot") and has(a, "unconsumed_operand"), a.reason)
    a = verdict(U("ui.click", {}, "Click the Increment Counter button in the Bench Test Window.", score=0.8, action=Action.ASK_SLOT, missing=["label"]))
    check("'Which label?' when the control was named (not bare)", has(a, "unfillable_slot"), a.reason)
    a = verdict(U("files.reveal", {}, "Open http://127.0.0.1:5000/ in the browser and click Download.", score=0.79, action=Action.ASK_SLOT, missing=["path"]))
    check("a URL operand with no argument holding it", has(a, "unconsumed_operand"), a.reason)
    a = verdict(U("files.delete", {"path": r"p:\127.0.0.1:5000\account"}, "Open http://127.0.0.1:5000/account and click Delete account.", score=0.72))
    check("a destructive skill given a garbled path made from a URL is caught", has(a, "unconsumed_operand"), a.reason)
    a = verdict(U("files.search", {"query": "doc"}, rf"find doc in {P}\docs\plan.pdf", score=0.8))
    check("a short argument that merely appears inside a long operand does not 'consume' it", has(a, "unconsumed_operand"), a.reason)
    a = verdict(U("apps.open", {"app": "notepad"}, "open notepad.exe", score=0.8))
    check("an argument that is the stem of the operand (notepad / notepad.exe) does", a.safe, a.reason)
    # the extractor half of that defect
    from friday.registry import REGISTRY
    args, missing = extract(REGISTRY.get("files.delete"), "delete http://127.0.0.1:5000/account")
    check("extractor: a URL is no longer read as a path", "path" not in args and missing == ["path"], f"args={args} missing={missing}")
    args, missing = extract(REGISTRY.get("files.delete"), r"delete c:\temp\old.txt")
    check("extractor: a real path still is", args.get("path", "").lower().endswith(r"temp\old.txt"), str(args))
    check("can_fill: files.copy 'src' has no strategy", not can_fill(REGISTRY.get("files.copy"), REGISTRY.get("files.copy").params[0]))
    check("can_fill: apps.open 'app' does", can_fill(REGISTRY.get("apps.open"), REGISTRY.get("apps.open").params[0]))
    # pre-commit audit: "forget the document <file>" must reach knowledge.forget intact (it used to lose the
    # extension, so the guard refused the route and the planner picked files.delete)
    for phrase, keep in (("forget the document budget.md", "budget.md"), (r"forget the document c:\kb\budget.md", r"c:\kb\budget.md")):
        got, _m = extract(REGISTRY.get("knowledge.forget"), phrase)
        check(f"audit: knowledge.forget keeps the file name in {phrase!r}", keep in str(got.get("query", "")).lower(), str(got))
    got, _m = extract(REGISTRY.get("files.search"), "find all pdf files")
    check("audit: a bare extension word is still dropped from a search query", "pdf" not in str(got.get("query", "")).lower(), str(got))
    check("audit: knowledge.forget parses its own input", parses_own_input("knowledge.forget"))
    check("parses_own_input: schedule/routine/memory yes, files no", parses_own_input("schedule.create") and parses_own_input("routine.create") and not parses_own_input("files.copy"))

    say("\n--- 3. compound request -> planner ---\n")
    a = verdict(U("apps.open", {"app": "notepad"}, "open notepad and then close the calculator", score=0.72))
    check("two action clauses: one skill call cannot be all of it", has(a, "compound_request"), a.reason)
    a = verdict(U("files.search", {"query": "invoice and tell me the total due"}, "Find my invoice file and tell me the total due.", score=0.83))
    check("a second instruction left inside the query", has(a, "second_instruction"), a.reason)
    a = verdict(U("routine.create", {"steps": "mutes the volume then shows the desktop", "name": "focus"}, "make a routine named focus that mutes the volume then shows the desktop", score=1.0))
    check("a routine's own 'then' is NOT a compound request", a.safe, a.reason)
    a = verdict(U("web.search", {"query": "salt and pepper recipes"}, "search for salt and pepper recipes", score=0.8))
    check("'salt and pepper' is not a second instruction", a.safe, a.reason)
    a = verdict(U("files.mkdir", {"path": P.lower()}, "Organize the files in " + P + " into subfolders by type, then tell me what you did.", score=0.7))
    check("organise-then-report goes to the planner", not a.safe, a.reason)

    say("\n--- 4. whole sentence (or the skill's own verb) as an argument ---\n")
    a = verdict(U("ui.scroll", {"app": "scroll down in the bench test"}, "Scroll down in the Bench Test Window.", score=0.8))
    check("ui.scroll(app='scroll down in the bench test')", has(a, "whole_sentence_arg"), a.reason)
    a = verdict(U("apps.focus", {"app": "bring the bench test window to the front"}, "Bring the Bench Test Window to the front.", score=0.79))
    check("apps.focus(app=<the whole sentence>)", has(a, "whole_sentence_arg"), a.reason)
    a = verdict(U("input.type", {"text": "type hello world"}, "type hello world", score=1.0))
    check("input.type(text='type hello world') would type the word 'type'", has(a, "whole_sentence_arg"), a.reason)
    a = verdict(U("input.type", {"text": "in the bench test window type hello bench into the harness input field"}, "In the Bench Test Window, type hello bench into the Harness Input field.", score=0.7))
    check("input.type given the whole instruction as the text to type", has(a, "whole_sentence_arg"), a.reason)
    a = verdict(U("screen.find_text", {"query": "sign in"}, "locate the text sign in on the screen", score=1.0))
    check("a noun in the skill's name ('text') is not treated as its verb", a.safe, a.reason)
    a = verdict(U("files.delete", {"path": (P + r"\old_draft.txt").lower()}, f"Delete the file {P}\\old_draft.txt.", score=0.86))
    check("a long absolute path is an operand, not 'the sentence'", a.safe, a.reason)

    say("\n--- 5. ambiguous skill-word routing -> planner ---\n")
    a = verdict(U("screen.observe", {}, "what port is open", score=0.65, candidates=[Match("screen.observe", 0.65, ""), Match("network.status", 0.65, "")]))
    check("two different skills tied for the word 'port'", has(a, "ambiguous_route"), a.reason)
    a = verdict(U("files.search", {"query": "budget"}, "budget", score=0.72, candidates=[Match("files.search", 0.72, ""), Match("knowledge.ask", 0.715, "")]))
    check("a near-tie below the confidence ceiling", has(a, "ambiguous_route"), a.reason)
    a = verdict(U("apps.open", {"app": "vscode"}, "open vs code", score=0.71, candidates=[Match("apps.open", 0.71, ""), Match("project.open", 0.74, "")]))
    check("a rival that scores HIGHER means an evidence-based tie-break ran: not ambiguous", a.safe, a.reason)
    a = verdict(U("whatsapp.compose", {"contact": "rahul", "message": "checking in"}, "Message Rahul on WhatsApp: just checking in.", score=0.77, candidates=[Match("whatsapp.compose", 0.773, ""), Match("whatsapp.open", 0.773, "")]))
    check("a tie is not ambiguity when the skill's own parser split the phrase (contact + message)", a.safe, a.reason)
    a = verdict(U("memory.recall", {"query": "open the page in the browser and tell me the number"}, "open the page in the browser and tell me the number", score=0.71, candidates=[Match("memory.recall", 0.71, ""), Match("memory.remember", 0.71, "")]))
    check("... but a tie whose only argument echoes the whole sentence still is", has(a, "ambiguous_route"), a.reason)
    a = verdict(U("files.search", {"query": "budget"}, "budget", score=0.83, candidates=[Match("files.search", 0.83, ""), Match("knowledge.ask", 0.82, "")]))
    check("a near-tie at a confident score is not flagged", a.safe, a.reason)
    a = verdict(U("files.search", {"query": "budget"}, "budget", score=0.80, candidates=[Match("files.search", 0.80, ""), Match("knowledge.ask", 0.70, "")]))
    check("a clear margin is not flagged", a.safe, a.reason)
    a = verdict(U("screen.active_window", {}, "In the window titled Bench Test Window, click the Increment Counter button.", score=0.71))
    check("'click ...' routed to a skill that can only look", has(a, "action_mismatch"), a.reason)
    a = verdict(U("memory.recall", {"query": "remember that my car is a blue swift"}, "remember that my car is a blue swift", score=0.71))
    check("assess() alone still flags a store instruction left on recall (look only); refine() is what reroutes it (8b)", has(a, "action_mismatch"), a.reason)
    check("plan.run and non-direct decisions are never second-guessed",
          directroute.assess("x", U("plan.run", {"goal": "x"}, "x")).safe
          and directroute.assess("x", Understanding(Action.UNKNOWN, "x", "x")).safe)


# ==========================================================================================================
# 6  end to end through the real BRAIN and Session.handle (planner stubbed, executor recorded)
# ==========================================================================================================

SANDBOX = Path(tempfile.gettempdir()) / "fbench" / "work"  # the Phase 29 path: routing is sensitive to path text
W = str(SANDBOX)
URL = "http://127.0.0.1:5000"

# (label, prompt, a tool that must NEVER be run directly for it)
UNSAFE_PROMPTS = [
    ("copy with both paths named", f"Copy the file {W}\\notes.txt to {W}\\notes_backup.txt.", "files.search"),
    ("rename with an absolute path", f"Rename {W}\\draft.txt to final.txt.", "files.move"),
    ("question about a file's contents (the word 'port')", f"What port is set in {W}\\config.txt?", "screen.observe"),
    ("compound: two files into a new folder", f"In {W}, put photo1.jpg and photo2.jpg into a new folder called Pictures.", "files.mkdir"),
    ("type into a named field", "In the Bench Test Window, type hello bench into the Harness Input field.", "input.type"),
    ("scroll a named window", "Scroll down in the Bench Test Window.", "ui.scroll"),
    ("click a named control", "In the window titled Bench Test Window, click the Increment Counter button.", "screen.active_window"),
    ("right-click a named control", "Right-click the Context Target button in the Bench Test Window.", "ui.right_click"),
    ("drag one named control onto another", "In the Bench Test Window, drag Drag Source onto Drop Target.", "ui.drag"),
    ("a URL must not become a path", f"Open {URL}/ in the browser and click the Download Report button.", "files.reveal"),
    ("a destructive skill from a URL", f"Open {URL}/account in the browser and click the Delete account button.", "files.delete"),
]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run(self, skill_name, args=None, *, actor="text"):
        from friday.registry import SkillResult

        self.calls.append((skill_name, dict(args or {})))
        return SkillResult(speech=f"(stub) {skill_name}", ok=True)

    @property
    def tools(self) -> list[str]:
        return [c[0] for c in self.calls]


def reset_session() -> None:
    from friday.session import SESSION

    SESSION.pending = None
    SESSION.last_skill = None
    SESSION.last_args = {}
    SESSION.last_data = {}
    SESSION.last_route_gate = None
    SESSION._followup = None


@contextmanager
def recorded_executor():
    from friday.permissions import EXECUTOR

    rec = Recorder()
    EXECUTOR.run = rec.run  # type: ignore[method-assign]
    try:
        yield rec
    finally:
        del EXECUTOR.run


@contextmanager
def cfg(section: str, **kw):
    from friday.config import CFG

    obj = getattr(CFG, section)
    old = {k: getattr(obj, k) for k in kw}
    for k, v in kw.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


async def suite_session() -> None:
    from friday.session import SESSION

    say("\n--- 6. end to end: BRAIN -> Session.handle (planner stubbed, executor recorded) ---\n")
    with cfg("planner", postcondition_verify=False):
        for label, prompt, forbidden in UNSAFE_PROMPTS:
            reset_session()
            with recorded_executor() as rec:
                await SESSION.handle(prompt, actor="test")
            check(
                f"{label}: handed to the planner, {forbidden} never ran",
                rec.tools == ["plan.run"] and rec.calls[0][1].get("goal") == prompt and forbidden not in rec.tools,
                f"tools={rec.tools} gate={SESSION.last_route_gate and SESSION.last_route_gate['reasons']}",
            )

        reset_session()
        with recorded_executor() as rec:
            await SESSION.handle("no, I meant the college project", actor="test")
        check("a correction never starts a planner goal of its own", "plan.run" not in rec.tools, f"{rec.tools}")

        reset_session()
        with recorded_executor() as rec:
            await SESSION.handle("What time is it?", actor="test")
        check("'What time is it?' stays direct", rec.tools == ["system.time"], f"{rec.tools}")
        check("... and the gate did not fire", SESSION.last_route_gate is None)

        reset_session()
        with recorded_executor() as rec:
            await SESSION.handle(f"Delete the file {W}\\old_draft.txt.", actor="test")
        check("'Delete the file <path>' stays direct with its path", rec.tools == ["files.delete"] and rec.calls[0][1].get("path", "").lower().endswith("old_draft.txt"), f"{rec.calls}")

        reset_session()
        with recorded_executor() as rec:
            await SESSION.handle("Copy a file", actor="test")
        check("a bare 'Copy a file' is still ASKED about, not planned", rec.tools == [] and SESSION.pending is not None and SESSION.pending.kind == "slot", f"tools={rec.tools} pending={SESSION.pending}")
        reset_session()


# ==========================================================================================================
# 7  the Phase 29 benchmark prompts
# ==========================================================================================================

STAYS_DIRECT = {"sys_time", "sys_math", "fs_delete_confirmed"}


def suite_benchmark_prompts() -> None:
    import bench_tasks
    from friday.brain import BRAIN

    say("\n--- 7. the 28 Phase 29 benchmark prompts ---\n")
    stayed, planned = set(), set()
    for t in bench_tasks.TASKS:
        text = t.goal.format(work=W, url=URL)
        u = BRAIN.understand(text)
        a = directroute.assess(text, u)
        (stayed if a.safe else planned).add(t.id)
    check("exactly the three legitimate single-action tasks stay direct", stayed == STAYS_DIRECT, f"stayed={sorted(stayed)}")
    check("the other 25 are handed to the planner", len(planned) == len(bench_tasks.TASKS) - 3, f"{len(planned)}")


# ==========================================================================================================
# 8  legitimate single-action requests still route directly
# ==========================================================================================================

# Known, documented: the extractor cannot isolate the argument for these, so the route would have been wrong.
KNOWN_FLAGGED = {
    "snap this to the left",                 # window.snap(app='snap this to the left')
    "click the save button",                 # ui.click: 'Which label?'
    "locate the text sign in on the screen",  # screen.find_text(query='locate ...')
    "click where it says next on the screen",  # screen.click_text(query='click where it says next ...')
}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def suite_legitimate() -> None:
    from friday.brain import BRAIN

    say("\n--- 8. legitimate single-action requests (the labelled routing suites) stay direct ---\n")
    sb = _load(ROOT / "scripts" / "smoke_brain.py", "smoke_brain_dr")
    rg = _load(ROOT / "scripts" / "regression.py", "regression_dr")
    cases = sorted({u for u, s in [*sb.CASES, *rg.MATCH_CASES] if s})
    flagged = set()
    for text in cases:
        u = directroute.refine(text, BRAIN.understand(text))  # the same two steps Session._gate_direct_route takes
        if not directroute.assess(text, u).safe:
            flagged.add(text)
    check(f"{len(cases)} labelled utterances: only the known-wrong routes are diverted",
          flagged <= KNOWN_FLAGGED, f"unexpected={sorted(flagged - KNOWN_FLAGGED)}")
    check("... and that is a small share of them", len(flagged) <= 6, f"{len(flagged)}/{len(cases)}")


# ==========================================================================================================
# 8b  Phase 30.1: store instruction -> memory.remember; clear knowledge question -> knowledge.ask
# ==========================================================================================================

STORE_PHRASES = [
    "remember my bank password is hunter2000",
    "remember that my car is blue",
    "please remember my locker code is 42",
    "Remember that my sister Priya lives in Chennai.",
    "keep in mind I prefer dark mode",
    "remember that my car is a blue swift",
]
# phrases that carry a remember word but are NOT instructions to store
RECALL_PHRASES = [
    "do you remember my wifi password",
    "can you remember my wifi password",
    "what's my sister's name",
    "what did I tell you about the deadline",
    "where did i park",
    "remember what I said about the deadline",
    "what do you remember about me",
    "do you remember my bank password",
]


def _decide(text: str):
    from friday.brain import BRAIN

    u = directroute.refine(text, BRAIN.understand(text))
    return u, directroute.assess(text, u)


def REGISTRY_get(name: str):
    from friday.registry import REGISTRY

    return REGISTRY.get(name)


def suite_store_and_questions() -> None:
    from unittest import mock as _mock

    say("\n--- 8b. an explicit store goes to memory.remember; questions never write ---\n")
    for text in STORE_PHRASES:
        u, a = _decide(text)
        check(f"store: {text!r} -> memory.remember, direct and safe", u.skill == "memory.remember" and a.safe and u.action is Action.ACT, f"{u.skill} {a.reason}")
    for text in RECALL_PHRASES:
        u, a = _decide(text)
        check(f"not a store: {text!r} never becomes memory.remember", u.skill != "memory.remember", f"{u.skill}")
    for text in RECALL_PHRASES[:5]:
        u, a = _decide(text)
        check(f"question {text!r} stays on memory.recall (read-only) and is safe", u.skill == "memory.recall" and a.safe, f"{u.skill} {a.reason}")

    say("")
    for text in ("list my documents", "list my docs", "show my documents", "please list the indexed documents"):
        check(f"explicit_list_documents({text!r})", directroute.explicit_list_documents(text))
        u, a = _decide(text)
        check(f"listing: {text!r} -> knowledge.list, direct and safe", u.skill == "knowledge.list" and a.safe, f"{u.skill} {a.reason}")
    for text in ("list my plans for the budget", "how much is the marketing budget", "show me the budget document", "list"):
        check(f"not a listing: {text!r}", not directroute.explicit_list_documents(text))
    u, a = _decide("how much is the marketing budget")
    check("a knowledge question is still not turned into a listing", u.skill != "knowledge.list", u.skill)

    say("")
    stored = {
        "remember my bank password is hunter2000": "my bank password is hunter2000",
        "please remember that my car is blue": "my car is blue",
        "don't forget my car is parked on level 3": "my car is parked on level 3",
        "keep in mind I prefer dark mode": "I prefer dark mode",
    }
    for text, fact in stored.items():
        check(f"explicit_store({text!r})", directroute.explicit_store(text) == fact, f"{directroute.explicit_store(text)!r}")
    for text in [
        "remember", "remember that", "remember this", "remember it for me", "remember to call mom tomorrow",
        "do you remember my wifi password", "can you remember my wifi password?", "remember what I said about the deadline",
        "remember my car is blue?", "what do you remember about me", "note that the meeting is at 5",
        "save my wifi password is x", "remember my car is blue and open chrome",
        "remember that my password is hunter2 then delete the downloads folder",
    ]:
        check(f"explicit_store({text!r}) is None (not an unmistakable store)", directroute.explicit_store(text) is None, f"{directroute.explicit_store(text)!r}")

    # refine() only ever reroutes recall/list or a decision that already has memory.remember as a candidate
    a = U("web.search", {"query": "remember my car is blue"}, "remember my car is blue", score=0.8)
    check("refine leaves an unrelated skill's route alone (no memory candidate)", directroute.refine(a.utterance, a) is a)
    b = U("ui.fill", {"text": "remember my locker code is 42"}, "remember my locker code is 42", score=0.7,
          candidates=[Match("ui.fill", 0.7, ""), Match("memory.remember", 0.67, "")])
    r = directroute.refine(b.utterance, b)
    check("refine corrects a stray route that has memory.remember among its candidates", r.skill == "memory.remember" and directroute.assess(b.utterance, r).safe, f"{r.skill}")
    c = U("memory.recall", {"query": "do you remember my car"}, "do you remember my car", score=0.9)
    check("refine leaves a recall question as it was", directroute.refine(c.utterance, c) is c)
    d = U("memory.recall", {"query": "remember my car is blue"}, "remember my car is blue", score=0.7, action=Action.UNKNOWN)
    check("refine never touches a non-ACT decision", directroute.refine(d.utterance, d) is d)

    for text in ["do you remember my bank password", "did I tell you my locker code", "remember what I said about the deadline"]:
        a = verdict(U("memory.recall", {"query": text}, text, score=0.80))
        check(f"a recall question that contains 'remember' is not an action mismatch: {text!r}", a.safe, a.reason)
    for text in ["can you remember to lock the door", "remember my car is blue and delete the downloads folder"]:
        a = verdict(U("memory.recall", {"query": text}, text, score=0.80))
        check(f"... but a request that also acts is still refused on recall: {text!r}", has(a, "action_mismatch") or has(a, "compound"), a.reason)

    say("\n--- 8b. Phase 30.3: a plain personal-fact question goes to memory.recall, never to the planner ---\n")
    for text in ("what is my blood type", "what's my blood type", "What is my blood type?", "what is my shoe size"):
        u, a = _decide(text)
        check(f"personal fact: {text!r} -> memory.recall about it, direct and safe",
              u.skill == "memory.recall" and a.safe and u.action is Action.ACT and str(u.args.get("query", "")).lower().endswith(("blood type", "shoe size")),
              f"{u.skill} {u.args} {a.reason}")
    for text in ("what is my wifi password", "what's my favorite color", "what is my sister's name", "what is my address"):
        u, a = _decide(text)
        check(f"personal fact (already clear): {text!r} stays on memory.recall, safe", u.skill == "memory.recall" and a.safe, f"{u.skill} {a.reason}")
    check("recall only reads (tier L0, no modify action)", REGISTRY_get("memory.recall").tier == "L0" and getattr(REGISTRY_get("memory.recall"), "action", "") != "modify")
    for text, want in [
        ("what is my git status", "dev.git"), ("what is my current git branch", "dev.git"), ("show my git log", "dev.git"),
        ("what is my last commit", "dev.git"), ("what is my cpu usage", "system.info"), ("what is my battery level", "system.battery"),
        ("what is my volume", "system.volume.get"), ("what is my wifi name", "network.status"),
        ("remember my blood type is O+", "memory.remember"),
    ]:
        u, a = _decide(text)
        check(f"not a personal fact: {text!r} is left on {want}", u.skill == want, f"{u.skill}")
    u, a = _decide("what is the capital of France")
    check("an unrelated question is not turned into a recall", u.skill != "memory.recall", f"{u.skill}")
    for text in ("what is my git status", "what is my cpu usage", "what is the capital of France", "remember my blood type is O+",
                 "what is my blood type and delete the downloads folder", "what is my 2 favorite colors", "list my documents"):
        check(f"personal_fact_question({text!r}) is False", not directroute.personal_fact_question(text))
    for text in ("what is my blood type", "what's my favourite colour?", "what are my allergies", "please what is my shoe size"):
        check(f"personal_fact_question({text!r}) is True", directroute.personal_fact_question(text))
    tie = U("dev.git", {}, "what is my blood type", score=0.68, candidates=[Match("dev.git", 0.68, ""), Match("memory.recall", 0.678, ""), Match("ui.read", 0.66, "")])
    r = directroute.refine(tie.utterance, tie)
    check("synthetic: a dev.git / memory.recall coin-flip on a personal fact becomes recall", r.skill == "memory.recall" and directroute.assess(tie.utterance, r).safe, f"{r.skill}")
    git_tie = U("dev.git", {}, "what is my git status", score=0.68, candidates=[Match("dev.git", 0.68, ""), Match("memory.recall", 0.678, "")])
    check("synthetic: the same coin-flip on 'what is my git status' is left alone", directroute.refine(git_tie.utterance, git_tie) is git_tie)
    clear = U("dev.git", {}, "what is my blood type", score=0.75, candidates=[Match("dev.git", 0.75, ""), Match("memory.recall", 0.678, "")])
    check("synthetic: a clear lead for another skill is not overridden", directroute.refine(clear.utterance, clear) is clear)
    other = U("weather.now", {}, "what are my allergies", score=0.668, candidates=[Match("weather.now", 0.668, ""), Match("screen.active_window", 0.656, "")])
    check("synthetic: a tie that does not involve memory.recall is left alone", directroute.refine(other.utterance, other) is other)
    nonact = U("dev.git", {}, "what is my blood type", score=0.68, action=Action.UNKNOWN, candidates=[Match("dev.git", 0.68, ""), Match("memory.recall", 0.678, "")])
    check("synthetic: a non-ACT decision is never touched", directroute.refine(nonact.utterance, nonact) is nonact)

    say("\n--- 8b. a clear knowledge question stays on knowledge.ask ---\n")
    ask_cases = ["how much is the marketing budget", "when does the office lease need renewing", "who is sending over the signed contract"]
    with _mock.patch.object(directroute, "_kb_has_documents", lambda: True):
        for text in ask_cases:
            u, a = _decide(text)
            check(f"knowledge base populated: {text!r} -> knowledge.ask, safe", u.skill == "knowledge.ask" and a.safe, f"{u.skill} {a.reason}")
        for text, want in [("what documents have you indexed", "knowledge.list"), ("show my knowledge base", "knowledge.list"), ("list indexed documents", "knowledge.list")]:
            u, a = _decide(text)
            check(f"an explicit listing {text!r} -> {want}", u.skill == want and a.safe, f"{u.skill} {a.reason}")
        u, a = _decide("what is my sister's name")
        check("a personal-fact question still goes to memory.recall", u.skill == "memory.recall" and a.safe, f"{u.skill} {a.reason}")
        # bare phrases stay ambiguous: no interrogative lead, so the tie is still a coin-flip
        t1 = U("files.search", {"query": "budget"}, "budget", score=0.72, candidates=[Match("files.search", 0.72, ""), Match("knowledge.ask", 0.715, "")])
        check("a bare noun phrase 'budget' (tie with knowledge.ask) is still ambiguous", has(verdict(t1), "ambiguous_route"), verdict(t1).reason)
        t2 = U("knowledge.ask", {"query": "budget"}, "budget", score=0.72, candidates=[Match("knowledge.ask", 0.72, ""), Match("notes.add", 0.715, "")])
        check("a bare 'budget' routed to knowledge.ask against notes.add is still ambiguous", has(verdict(t2), "ambiguous_route"), verdict(t2).reason)
        t3 = U("knowledge.ask", {"query": "notes about the meeting"}, "notes about the meeting", score=0.72, candidates=[Match("knowledge.ask", 0.72, ""), Match("memory.recall", 0.715, "")])
        check("'notes about the meeting' (no interrogative, no verb) is still ambiguous", has(verdict(t3), "ambiguous_route"), verdict(t3).reason)
        t4 = U("knowledge.ask", {"query": "how do i delete the budget file"}, "how do i delete the budget file", score=0.72, candidates=[Match("knowledge.ask", 0.72, ""), Match("memory.recall", 0.715, "")])
        check("a question that names an action verb ('delete') is still ambiguous", has(verdict(t4), "ambiguous_route"), verdict(t4).reason)
        t5 = U("knowledge.ask", {"query": "how much is the budget"}, "how much is the budget", score=0.72, candidates=[Match("knowledge.ask", 0.72, ""), Match("notes.add", 0.715, "")])
        check("synthetic: a clear question vs a MUTATING rival is not ambiguity", verdict(t5).safe, verdict(t5).reason)
        t6 = U("files.search", {"query": "how much is the budget"}, "how much is the budget", score=0.72, candidates=[Match("files.search", 0.72, ""), Match("knowledge.ask", 0.715, "")])
        check("synthetic: a question tied between two OTHER read-only skills (neither a mutation, not knowledge.ask) stays ambiguous", has(verdict(t6), "ambiguous_route"), verdict(t6).reason)
        u, a = _decide("delete the budget file")
        check("the mutation-vs-read guard is intact: 'delete the budget file' never becomes a knowledge/memory read", u.skill not in ("knowledge.ask", "memory.recall"), f"{u.skill}")
        a = verdict(U("knowledge.ask", {"query": "delete the budget file"}, "delete the budget file", score=0.72, candidates=[Match("knowledge.ask", 0.72, ""), Match("notes.add", 0.715, "")]))
        check("'delete the budget file' routed to a read-only skill is still refused (action_mismatch)", has(a, "action_mismatch"), a.reason)
    with _mock.patch.object(directroute, "_kb_has_documents", lambda: False):
        u, a = _decide("when does the office lease need renewing")
        check("an EMPTY knowledge base: a read-vs-read tie is NOT waved through", u.skill != "knowledge.ask" or not a.safe, f"{u.skill} {a.reason}")
        u, a = _decide("how much is the marketing budget")
        check("... but a question that only ties with a mutating rival is still waved through (it can only be answered, never stored)", a.safe, f"{u.skill} {a.reason}")


async def suite_store_end_to_end() -> None:
    """Real BRAIN, real Session, real memory skills on the throwaway database; nothing is mocked but the clock."""
    from friday import audit, memory
    from friday.session import SESSION

    say("\n--- 8b. end to end: the write is audited as memory.remember, redacted, and never planned ---\n")
    for row in memory.all_memories(limit=500):
        memory.forget(row["id"])
    reset_session()
    with cfg("planner", postcondition_verify=False):
        r = await SESSION.handle("remember my bank password is hunter2000", actor="text")
        check("the gate did not divert a clear store to the planner", SESSION.last_route_gate is None and SESSION.last_skill == "memory.remember", f"{SESSION.last_skill} {SESSION.last_route_gate}")
        check("the speech is the skill's own deterministic 'Noted - ...'", r.speech.startswith("Noted") and "[redacted]" in r.speech and "hunter2000" not in r.speech, r.speech)
        rows = memory.all_memories()
        check("exactly one memory was written, and the secret is not in it", len(rows) == 1 and "hunter2000" not in rows[0]["value"] and "[redacted]" in rows[0]["value"], str(rows))
        check("what was stored is what was spoken", bool(rows) and rows[0]["value"] in r.speech, f"{rows} / {r.speech!r}")
        last = audit.recent(1)
        check("the audit row names the real write: memory.remember (not memory.recall)", bool(last) and last[0].get("skill") == "memory.remember", str(last[:1]))

        reset_session()
        r = await SESSION.handle("do you remember my bank password", actor="text")
        check("a 'do you remember' question reads and writes nothing", SESSION.last_skill == "memory.recall" and len(memory.all_memories()) == 1, f"{SESSION.last_skill} n={len(memory.all_memories())}")
        reset_session()
        r = await SESSION.handle("what is my bank password", actor="text")
        check("recall of a redacted memory returns the placeholder, not the secret", "hunter2000" not in r.speech and len(memory.all_memories()) == 1, r.speech)
    for row in memory.all_memories(limit=500):
        memory.forget(row["id"])
    reset_session()



# ==========================================================================================================
# 9-10  a direct call is verified like a planned step
# ==========================================================================================================


@contextmanager
def skill_fn(name: str, fn):
    from friday.registry import REGISTRY

    sk = REGISTRY.get(name)
    old = sk.fn
    sk.fn = fn
    try:
        yield
    finally:
        sk.fn = old


def _ok(speech: str, **data):
    from friday.registry import SkillResult

    return SkillResult(speech=speech, ok=True, data=data)


async def suite_verification() -> None:
    from friday.session import SESSION

    say("\n--- 9. a direct call is verified like a planned step ---\n")
    base = Path(tempfile.mkdtemp(prefix="f30v-"))
    try:
        reset_session()
        target = base / "made"
        r = await SESSION._run("files.mkdir", {"path": str(target)}, actor="test")
        v = (r.data or {}).get("verification", {})
        check("success: the real skill really made the folder and it is VERIFIED", r.ok and target.is_dir() and v.get("status") == "verified", f"{r.speech!r} {v.get('status')}")
        check("success: the tool's own wording is kept", "made" in r.speech.lower() or r.speech != "", r.speech)

        target2 = base / "never"
        with skill_fn("files.mkdir", lambda **kw: _ok(f"Created folder {kw['path']}.", path=kw["path"])):
            r = await SESSION._run("files.mkdir", {"path": str(target2)}, actor="test")
        v = (r.data or {}).get("verification", {})
        check("failure: a skill that says 'created' but did nothing is overridden", (not r.ok) and v.get("status") == "failed" and not target2.exists(), f"ok={r.ok} {v.get('status')}")
        check("failure: the reply says it did not take effect", r.speech.startswith("It did not take effect"), r.speech[:90])
        check("failure: the tool's own claim is kept only as context", "The tool itself said" in r.speech, r.speech[:160])

        src = base / "a.txt"
        src.write_text("hello")
        dst = base / "b.txt"
        with skill_fn("files.copy", lambda **kw: _ok("Copied a.txt to b.txt.", src=kw["src"], dst=kw["dst"])):
            r = await SESSION._run("files.copy", {"src": str(src), "dst": str(dst)}, actor="test")
        check("failure: files.copy that copied nothing", (not r.ok) and (r.data or {}).get("verification", {}).get("status") == "failed", r.speech[:90])

        # a read-only direct call is untouched
        r = await SESSION._run("system.time", {}, actor="test")
        check("a read-only direct call carries no verification", r.ok and "verification" not in (r.data or {}), r.speech)

        # a refused call is never verified
        from friday.config import CFG

        CFG.permissions.overrides["files.mkdir"] = "deny"
        try:
            r = await SESSION._run("files.mkdir", {"path": str(base / "denied")}, actor="test")
        finally:
            CFG.permissions.overrides.pop("files.mkdir", None)
        check("a permission-denied call is refused as before, not verified", (not r.ok) and "not allowed" in r.speech.lower() and not (base / "denied").exists(), r.speech)

        # a state-changing skill with no read-back says so
        from friday.registry import REGISTRY, Skill

        async def _noop(**kw):
            return _ok("Did the thing.")

        REGISTRY._skills["zz.opaque"] = Skill("zz.opaque", "L1", "an opaque state-changing test skill", [], _noop, [], True)
        try:
            r = await SESSION._run("zz.opaque", {}, actor="test")
        finally:
            REGISTRY._skills.pop("zz.opaque", None)
        from friday import verify

        check("unverifiable: ok is left alone (nothing is known to have gone wrong)", r.ok)
        check("unverifiable: the reply says it is unconfirmed, in plan.run's own words", r.speech.endswith("so treat it as unconfirmed.") and r.speech.endswith(verify.UNCONFIRMED_NOTE), r.speech)
        check("unverifiable: status recorded as unverified", (r.data or {}).get("verification", {}).get("status") == "unverified")

        # plan.run is not double-wrapped
        with recorded_executor() as rec:
            r = await SESSION._run("plan.run", {"goal": "x"}, actor="test")
        check("plan.run is passed through untouched (its steps are verified individually)", rec.tools == ["plan.run"] and r.speech == "(stub) plan.run", r.speech)
    finally:
        shutil.rmtree(base, ignore_errors=True)
        reset_session()


async def suite_truthful() -> None:
    from friday.brain import BRAIN
    from friday.session import SESSION

    say("\n--- 10. a truthful final answer after a direct-route failure (Session.handle, end to end) ---\n")
    base = Path(tempfile.mkdtemp(prefix="f30t-"))
    try:
        target = base / "reports"
        text = f"Create the folder {target}."
        fake = U("files.mkdir", {"path": str(target).lower()}, text, score=0.85)
        BRAIN.understand = lambda _t: fake  # type: ignore[method-assign]
        try:
            reset_session()
            with skill_fn("files.mkdir", lambda **kw: _ok(f"Created the folder {kw['path']}.", path=kw["path"])):
                r = await SESSION.handle(text, actor="test")
        finally:
            del BRAIN.understand
        check("the route was a legitimate direct call (the gate let it through)", SESSION.last_route_gate is None and SESSION.last_skill == "files.mkdir", f"{SESSION.last_skill} {SESSION.last_route_gate}")
        check("the folder does not exist", not target.exists())
        check("the final answer is NOT a success: ok is False", r.ok is False, f"ok={r.ok}")
        check("the final answer says it did not take effect", "did not take effect" in r.speech, r.speech[:120])
        check("the final answer does not lead with the tool's 'Created ...' claim", not r.speech.lower().startswith("created"), r.speech[:80])
        check("the failed check is attached for callers", (r.data or {}).get("verification", {}).get("status") == "failed")

        # the same call when the effect is real: the answer is the tool's, verified
        reset_session()
        target_ok = base / "real"
        fake_ok = U("files.mkdir", {"path": str(target_ok).lower()}, f"Create the folder {target_ok}.", score=0.85)
        BRAIN.understand = lambda _t: fake_ok  # type: ignore[method-assign]
        try:
            r = await SESSION.handle(f"Create the folder {target_ok}.", actor="test")
        finally:
            del BRAIN.understand
        check("when the effect is real the answer stands and is verified", r.ok and target_ok.is_dir() and (r.data or {}).get("verification", {}).get("status") == "verified", f"{r.speech!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
        reset_session()


# ==========================================================================================================
# 11  kill switches / planner unavailable
# ==========================================================================================================


async def suite_switches() -> None:
    from friday.session import SESSION

    say("\n--- 11. kill switches and planner unavailable ---\n")
    prompt = "Scroll down in the Bench Test Window."
    with cfg("planner", postcondition_verify=False):
        with cfg("brain", direct_route_guard=False):
            reset_session()
            with recorded_executor() as rec:
                await SESSION.handle(prompt, actor="test")
            check("direct_route_guard off: the old direct route runs (Phase 29 behaviour)", rec.tools != ["plan.run"] and rec.tools != [], f"{rec.tools}")
        with cfg("planner", enabled=False):
            reset_session()
            with recorded_executor() as rec:
                r = await SESSION.handle(prompt, actor="test")
            check("no planner: a doubtful ACT is refused, nothing runs", rec.tools == [] and r.ok is False and "not sure" in r.speech.lower(), f"{rec.tools} {r.speech!r}")
            reset_session()
            with recorded_executor() as rec:
                await SESSION.handle(f"Copy {W}\\a.txt to {W}\\b.txt.", actor="test")
            check("no planner: a doubtful ASK_SLOT is asked about, as before", rec.tools == [] and SESSION.pending is not None, f"{rec.tools}")
    base = Path(tempfile.mkdtemp(prefix="f30k-"))
    try:
        reset_session()
        with cfg("planner", postcondition_verify=False):
            with skill_fn("files.mkdir", lambda **kw: _ok("Created.", path=kw["path"])):
                r = await SESSION._run("files.mkdir", {"path": str(base / "x")}, actor="test")
        check("postcondition_verify off: a direct call is not read back (the single master switch)", r.ok and "verification" not in (r.data or {}), r.speech)
    finally:
        shutil.rmtree(base, ignore_errors=True)
        reset_session()


# ==========================================================================================================
# 12  mutation testing
# ==========================================================================================================


def mutation_testing() -> int:
    """Returns how many mutations SURVIVED (were not detected)."""
    from friday import orchestrator, verify
    from friday.session import Session

    outcomes: list[bool] = []

    say("\n--- 12. MUTATION TESTING: disable a safety mechanism -> the suite must fail ---\n")

    def killed(label: str, suite, patches) -> None:
        # the unmutated suite must pass, otherwise "it failed" proves nothing
        clean = run_suite(suite, quiet=True)
        with contextmanager_stack(patches):
            failures = run_suite(suite, quiet=True)
        ok = clean == 0 and failures > 0
        outcomes.append(ok)
        print(f"  {'OK  ' if ok else 'MISS'} mutation killed: {label} -> clean_failures={clean} mutated_failures={failures}")

    none = lambda *a, **k: None  # noqa: E731

    for name in ("_check_unconsumed_operand", "_check_unfillable_slot", "_check_whole_sentence_arg",
                 "_check_second_instruction", "_check_compound_request", "_check_action_mismatch",
                 "_check_ambiguous_route"):
        killed(f"directroute.{name} disabled", suite_gate, [mock.patch.object(directroute, name, none)])

    killed("directroute.refine disabled (a store instruction stays on recall)", suite_store_and_questions,
           [mock.patch.object(directroute, "refine", lambda t, u: u)])
    killed("directroute._is_recall_question says no (a 'do you remember' question is an action mismatch again)", suite_store_and_questions,
           [mock.patch.object(directroute, "_is_recall_question", lambda t: False)])
    killed("directroute.explicit_store accepts nothing", suite_store_and_questions,
           [mock.patch.object(directroute, "explicit_store", lambda t: None)])
    killed("directroute.explicit_store accepts every question too", suite_store_and_questions,
           [mock.patch.object(directroute, "explicit_store", lambda t: t)])
    killed("the personal-fact route removed (a tied 'what is my blood type' goes to the planner again)", suite_store_and_questions,
           [mock.patch.object(directroute, "_recall_for_personal_fact", lambda t, u: u)])
    killed("every 'what is my ...' question counts as a personal fact (git / system questions get stolen)", suite_store_and_questions,
           [mock.patch.object(directroute, "personal_fact_question", lambda t: True)])
    killed("the clear-question waiver removed (knowledge questions go to the planner again)", suite_store_and_questions,
           [mock.patch.object(directroute, "_clear_read_question", lambda *a, **k: False)])
    killed("the clear-question waiver waves EVERYTHING through (bare phrases / verbs no longer ambiguous)", suite_store_and_questions,
           [mock.patch.object(directroute, "_clear_read_question", lambda *a, **k: True)])

    async def passthrough(self, text, understanding):
        return understanding

    killed("Session._gate_direct_route returns the route unchanged", suite_session,
           [mock.patch.object(Session, "_gate_direct_route", passthrough)])
    killed("Session never calls refine (the end-to-end store test must catch it)", suite_store_end_to_end,
           [mock.patch.object(directroute, "refine", lambda t, u: u)])
    killed("directroute.assess approves every route (the end-to-end routing suite must catch it)",
           suite_session, [mock.patch.object(directroute, "assess", lambda t, u: directroute.Assessment(True))])

    async def plain_run(tool, args, *, actor, runner=None):
        return await runner(tool, args)

    import friday.session as session_mod

    killed("direct calls bypass run_verified (no read-back at all)", suite_verification,
           [mock.patch.object(session_mod, "run_verified", plain_run)])
    killed("the post-condition verdict is never attached", suite_verification,
           [mock.patch.object(orchestrator.Orchestrator, "_attach_verification", none_async)])

    async def always_verified(tool, args, data, before, *, timeout_s):
        return verify.Verification(verify.VerifyStatus.VERIFIED, [verify.Check("x", True)], "assumed")

    killed("verify_call rubber-stamps every call as VERIFIED", suite_verification,
           [mock.patch.object(verify, "verify_call", always_verified)])
    killed("a FAILED verdict no longer overrides the tool's success (truthful answer)", suite_truthful,
           [mock.patch.object(verify, "verify_call", always_verified)])
    killed("the unconfirmed note is dropped for unverifiable calls", suite_verification,
           [mock.patch.object(verify, "UNCONFIRMED_NOTE", "")])
    return sum(1 for ok in outcomes if not ok)


async def none_async(*a, **k):
    return None


@contextmanager
def contextmanager_stack(patches):
    started = []
    try:
        for p in patches:
            p.start()
            started.append(p)
        yield
    finally:
        for p in reversed(started):
            p.stop()


# ==========================================================================================================


def main() -> int:
    from friday.brain import BRAIN
    from friday.registry import REGISTRY

    import iso_env  # Phase 35: throwaway database AND recovery store (files.* asks recovery.in_store())

    with iso_env.isolated_runtime():
        REGISTRY.discover()
        BRAIN.warm()
        failed = 0
        for suite in (
            suite_gate, suite_session, suite_benchmark_prompts, suite_legitimate,
            suite_store_and_questions, suite_store_end_to_end, suite_verification, suite_truthful, suite_switches,
        ):
            failed += run_suite(suite)
        failed += mutation_testing()
    print("\nALL OK\n" if failed == 0 else f"\n{failed} CHECK(S) FAILED\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
