"""Phase 34 -- natural-language routing to the operator-only recovery skills.

Deterministic: no Ollama, no network, no embedding model (BRAIN.understand is replaced by a tripwire
that raises if a sentence is NOT claimed by the recovery router). Nothing on the real machine is
touched: the recovery store, the legacy store and the undo/audit database are all redirected to a
throwaway temp folder (same harness style as smoke_snapshot_retention.py), and os.unlink / os.rmdir
are wrapped in a tripwire that fails the run if anything inside the user's REAL stores is removed.

Sections
  1  listing phrasings -> files.recoveries only (never cleanup / migrate / recover); "all" -> limit 50
  2  explicit ids: ONLY a valid token routes to files.recover, args exactly {"snapshot": id};
     numbers / positions / file names / wrong-case / extra-char / arbitrary strings are never inferred
  3  ambiguous restore -> CLARIFY_RESTORE (listing only); session: real ids shown, nothing else runs
  4  cleanup / migrate wording; EXPLAIN for "delete all"; session: confirmation asked, a decline
     changes nothing, unattended actors are refused, audit rows exist, a listing never reaches them
  5  negative / hijack corpus (ordinary requests are not ours); kill switch
  6  safety architecture: ROUTED_SKILLS, the session refuses a non-recovery skill, every route goes
     through EXECUTOR.run, the planner's tool list / prompt is unchanged
  7  MUTATION TESTING: each guard is deliberately broken (source-level, on a private copy of
     recoveryroute.py or of Session._maybe_route_recovery); the suite must FAIL under every one

Run:  .venv/Scripts/python.exe -X utf8 scripts/smoke_recovery_route.py
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from friday import audit, paths, recovery, retention, store  # noqa: E402
from friday import recoveryroute  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult  # noqa: E402
from friday.skills import recoveries as _recoveries_skills  # noqa: E402,F401

REGISTRY.discover()

ID = "20260101T101010-0123456789ab"
ID2 = "20260102T111111-fedcba987654"
DAY = 86400.0
FOUR = {"files.recoveries", "files.recover", "files.recovery_cleanup", "files.recovery_migrate"}
# what each route kind is allowed to run (string literals on purpose: a mutated constant must not
# be able to redefine the expectation)
KIND_SKILL = {
    "list": "files.recoveries",
    "clarify_restore": "files.recoveries",
    "recover": "files.recover",
    "cleanup": "files.recovery_cleanup",
    "migrate": "files.recovery_migrate",
    "explain": "",
}
# the user's REAL stores, captured before anything is redirected
REAL_STORES = [paths._default_snapshots(), paths.DATA / "snapshots"]


# -- scaffolding ----------------------------------------------------------------------------------


class Rec:
    """Collects checks. `loud` prints; mutation runs are silent."""

    def __init__(self, loud: bool) -> None:
        self.loud = loud
        self.failed: list[str] = []
        self.total = 0

    def check(self, label: str, cond: object, detail: object = "") -> bool:
        self.total += 1
        ok = bool(cond)
        if not ok:
            self.failed.append(label)
        if self.loud:
            tail = "" if ok or not detail else f" -- {str(detail)[:160]}"
            print(f"  {'OK  ' if ok else 'MISS'} {label[:130]}{tail}")
        return ok

    def section(self, title: str) -> None:
        if self.loud:
            print(f"\n--- {title} ---\n")


GLOBAL: list[tuple[str, bool]] = []


def gcheck(label: str, cond: object, detail: object = "") -> bool:
    ok = bool(cond)
    GLOBAL.append((label, ok))
    print(f"  {'OK  ' if ok else 'MISS'} {label[:130]}{'' if ok or not detail else ' -- ' + str(detail)[:160]}")
    return ok


def active_rr():
    return sys.modules["friday.recoveryroute"]


# -- corpora --------------------------------------------------------------------------------------

LIST_PHRASES = [
    "show my recovery snapshots", "list my recovery snapshots", "display my recovery snapshots",
    "show me my recovery copies", "list recovery copies", "display the recovery copies",
    "view my recovery snapshots", "show the saved recovery copies", "list recovery snapshots",
    "show recovery snapshots", "give me my recovery snapshots", "see my recovery copies",
    "list the recovery files",
    "what files can FRIDAY recover", "what files can you restore", "which files can you bring back",
    "what files can you recover?", "what recoverable files do you have", "which recoverable files are there",
    "what recovery snapshots do you have", "do I have any recovery snapshots", "are there any recovery copies",
    "what recovery copies are there", "recovery list",
    "please show my recovery snapshots", "can you show my recovery snapshots",
    "could you please list the recovery copies", "hey friday show my recovery snapshots",
    "hey friday, can you list my recovery copies for me", "Show my recovery snapshots please",
    "show my recovery snapshots for me", "I want you to show my recovery snapshots",
    "I\u2019d like to see my recovery snapshots", "okay friday, show my recovery snapshots",
    "SHOW MY RECOVERY SNAPSHOTS!!!", "  show   my  recovery   snapshots  ", "Friday, please list my recovery copies for me, thanks",
    "tell me which recovery copies you have",
]
LIST_ALL_PHRASES = [
    "show all recovery snapshots", "list all my recovery copies", "show me all the recovery snapshots",
    "please display all recovery copies", "hey friday can you list all the recovery snapshots for me",
]

RECOVER_PHRASES = [
    f"restore snapshot {ID}", f"recover {ID}", f"restore the file from snapshot {ID}",
    f"recovery recover {ID}", f"restore recovery snapshot {ID}", f"please restore snapshot {ID}",
    f"can you recover {ID} for me", f"restore snapshot {ID}.", f"bring back snapshot {ID}",
    f"restore snapshot with id {ID}", f"put back the deleted file from recovery snapshot {ID}",
    f"hey friday, restore the file from the recovery copy {ID}", f"RESTORE SNAPSHOT {ID}",
    f"restore {ID}", f"recover the deleted file from snapshot {ID}",
]

# never a recover route, whatever else they are: listing-only clarification or nothing
NEVER_RECOVER = [
    "restore snapshot 3", "restore snapshot number 3", "restore the first snapshot",
    "restore the latest snapshot", "restore the newest recovery copy", "restore snapshot",
    "restore snapshot one", "restore recovery snapshot 2",
    "restore the second one", "restore 3", "recover the third one",
    f"restore snapshot {ID.replace('T', 't')}",               # lower-case t
    f"restore snapshot {ID}f",                                # an extra character at the end
    f"restore snapshot x{ID}",                                # an extra character at the start
    f"restore snapshot {ID[:-1]}",                            # one character short
    f"restore snapshot {ID.upper()}",                         # upper-case hex
    f"recover {ID.replace('T', 't')}",
    f"restore snapshot {ID}-extra",
    f"restore snapshot {ID[:8]}-{ID[9:15]}-{ID[16:]}",        # wrong separators
    f"restore snapshot {ID[:15]}_{ID[16:]}",
    f"restore snapshot {ID[:-1]}g",                           # not hex
    "restore snapshot abcdefghijklmnopqrstuvwxyz12", "restore snapshot ABCDEFGHIJKLMNOPQRSTUVWXYZ12",
    "recover 2026010110101001234567890abc", "restore snapshot 0123456789012345678901234567",
    "restore snapshot ../../etc/passwd", "restore snapshot C:\\Users\\x\\file.txt",
    "restore snapshot report.docx", "restore report.docx", "recover notes.txt from the recovery copy",
    "restore the report", "restore the file report.docx", "restore my taxes.xlsx",
    f"restore the file report.docx from snapshot {ID}",
]
# compound / extra words around a valid id: not ours at all
COMPOUND_NONE = [
    f"restore report.docx from snapshot {ID}", f"restore snapshot {ID} and delete the file",
    f"restore snapshot {ID} then open it", f"recover {ID} and {ID2}", f"restore snapshot {ID} and {ID2}",
    f"restore snapshot {ID}, then clean up recovery snapshots", f"restore snapshot {ID} to the desktop",
    f"restore snapshot {ID} as report_v2.docx", f"restore {ID} and {ID2}", f"recover {ID} {ID2}",
    f"restore snapshot {ID} and tell me when it is done",
]

AMBIGUOUS_RESTORE = [
    "restore that file", "bring back the file you deleted", "recover my deleted file",
    "restore the snapshot", "restore my deleted file", "recover the file", "restore the file you overwrote",
    "bring back the file", "put back the deleted file", "undelete the file", "recover a recovery copy",
    "restore a snapshot", "restore the deleted file from the recovery copies", "please restore that file for me",
    "can you restore the file you just deleted",
]

CLEANUP_PHRASES = [
    "clean up expired recovery snapshots", "delete old recovery snapshots", "prune recovery copies",
    "clean up recovery copies", "please clean up the old recovery snapshots",
    "can you prune my recovery snapshots for me", "tidy up the recovery copies",
    "remove expired recovery copies", "recovery cleanup", "clear out stale recovery snapshots",
    "clean up recovery snapshots that are old", "purge old recovery copies", "cleanup the recovery snapshots",
    "hey friday, delete the expired recovery copies please", "clean up the recovery folder",
]
EXPLAIN_PHRASES = [
    "delete all recovery snapshots", "delete the recovery copies", "delete my recovery snapshots",
    "remove all recovery copies", "clear all recovery snapshots", "empty the recovery folder",
    "purge every recovery copy", "get rid of all the recovery snapshots", "prune every recovery copy",
    "clean up all recovery snapshots", "delete all old recovery snapshots", "delete every expired recovery copy",
    "please delete all of my recovery snapshots", "remove the recovery files", "empty the recovery store",
]
EXPLAIN_OR_NONE = ["clear everything in recovery", "wipe everything in the recovery folder"]

MIGRATE_PHRASES = [
    "migrate old recovery snapshots", "migrate the recovery snapshots", "migrate my recovery copies",
    "migrate legacy recovery snapshots to the new store", "recovery migrate", "please migrate the old recovery store",
    "migrate recovery snapshots into the new location", "can you migrate the existing recovery files for me",
]
NOT_MIGRATE = [
    "migrate my database", "migrate the snapshots", "migrate my files", "migrate old snapshots",
    "migrate data to the cloud", "migrate to windows 12", "migrate", "migrate recovery",
    "migrate my recovery password", "migrate the old copies", "migrate old recovery snapshots and delete the old folder",
    "migrate recovery snapshots then clean them up", "migrate the legacy store",
]

# ordinary requests, hijack attempts, questions and compounds: NOT recovery routes (classify -> None)
NEGATIVE = [
    "restore my window", "restore", "restore it", "restore that", "recover", "recover my password",
    "recover my account", "recover the data", "restore the tab", "restore defaults", "restore my settings",
    "restore the volume", "restore system", "restore snapshot of my VM", "revert to the previous snapshot",
    "take a snapshot of my screen", "take a snapshot", "take a screenshot", "screenshot",
    "show snapshots", "list snapshots", "show my snapshots", "show copies", "list my copies",
    "list my files", "list files in downloads", "show my files", "show recovery", "list recovery",
    "display recovery", "recovery", "show the recovery",
    "delete the old files", "delete old files", "delete all files", "delete everything", "delete all",
    "delete report.docx", "delete the file report.docx",
    "clean up my downloads", "clean up the temp folder", "clean up", "clean up my desktop", "prune",
    "clean up everything", "clean up old files", "clean up old snapshots", "clean up the snapshots",
    "migrate my database", "migrate to a new laptop",
    "how do recovery snapshots work", "what is a recovery snapshot", "what are recovery snapshots",
    "explain recovery snapshots", "tell me about recovery copies",
    "should I clean up recovery snapshots tomorrow", "tomorrow clean up expired recovery snapshots",
    "clean up recovery snapshots tomorrow", "schedule a cleanup of recovery snapshots every night",
    "remind me to clean up recovery snapshots", "when did you last clean up recovery snapshots",
    "remember that my recovery snapshots are old", "write a file called snapshots.txt",
    "show recovery snapshots and clean them up", "list recovery snapshots and restore the newest",
    "list recovery copies then restore the first one", "show my recovery snapshots and delete them",
    "show my recovery snapshots to my boss", "list the recovery copies every morning",
    "clean up expired recovery snapshots and email me", "delete old recovery snapshots and restart",
    "prune recovery copies then show them", "clean up recovery copies and migrate old recovery snapshots",
    "copy file a.txt to b.txt", "copy the recovery copy to the desktop", "copy report.docx to the recovery folder",
    "undo that", "undo", "undo the last thing", "what time is it", "open notepad", "hello",
    "recover deleted files from the recycle bin", "restore deleted files from the recycle bin",
    "where is the recovery folder", "open the recovery folder", "is a recovery snapshot encrypted",
    "do you have any snapshots", "which files did you delete", "what did you delete today",
    "restore the file from the recycle bin", "I want to restore my computer", "recover from the crash",
    "yes", "no", "go ahead", "cancel", "stop", "",
]


# ==========================================================================================================
# the pure suite: sections 1-5 (classify only)
# ==========================================================================================================


def _is_list(r, limit: int) -> bool:
    return r is not None and r.kind == "list" and r.skill == "files.recoveries" and r.args == {"limit": limit}


def pure_suite(rec: Rec, rr) -> None:
    c = rr.classify

    rec.section("1. listing phrasings -> files.recoveries only")
    for t in LIST_PHRASES:
        r = c(t)
        rec.check(f"list: {t!r}", _is_list(r, 10), r)
    for t in LIST_ALL_PHRASES:
        r = c(t)
        rec.check(f"list 'all': {t!r} -> limit 50", _is_list(r, 50), r)
    routes = [c(t) for t in LIST_PHRASES + LIST_ALL_PHRASES]
    rec.check("1a no listing phrasing ever names cleanup / migrate / recover",
              all(r is not None and r.skill == "files.recoveries" and r.kind == "list" for r in routes))
    rec.check("1b a listing route carries no speech and no id", all(not r.speech and set(r.args) == {"limit"} for r in routes if r))
    rec.check("1c 'all' only widens the limit (never a different skill)",
              getattr(c("show all recovery snapshots"), "skill", 1) == getattr(c("show recovery snapshots"), "skill", 2) == "files.recoveries")

    rec.section("2. explicit ids: only a valid token routes to files.recover")
    for t in RECOVER_PHRASES:
        r = c(t)
        rec.check(f"recover: {t!r}", r is not None and r.kind == "recover" and r.skill == "files.recover"
                  and r.args == {"snapshot": ID}, r)
    r = c(f"restore snapshot {ID2}")
    rec.check("2a the id is passed through verbatim (second id)", r is not None and r.args == {"snapshot": ID2}, r)
    for t in NEVER_RECOVER:
        r = c(t)
        safe = r is None or (r.kind == "clarify_restore" and r.skill == "files.recoveries" and r.args == {"limit": 10})
        rec.check(f"never inferred: {t!r}", safe, r)
    for t in COMPOUND_NONE:
        rec.check(f"compound / extra words around an id -> not ours: {t!r}", c(t) is None, c(t))
    rec.check("2b no sentence without a valid token ever yields kind=recover",
              all(not (c(t) and c(t).kind == "recover") for t in NEVER_RECOVER + COMPOUND_NONE + AMBIGUOUS_RESTORE + NEGATIVE))
    rec.check("2c a wrong-case id is evidence of intent, not an id (clarify, not recover)",
              getattr(c(f"restore snapshot {ID.replace('T', 't')}"), "kind", "") == "clarify_restore")
    rec.check("2e an id with a stray character at either end is shown the list (clarify), never silently dropped or accepted",
              all(getattr(c(t), "kind", "") == "clarify_restore" for t in (f"restore snapshot {ID}f", f"restore snapshot x{ID}", f"restore snapshot {ID}-extra")),
              [c(t) for t in (f"restore snapshot {ID}f", f"restore snapshot x{ID}")])
    rec.check("2d a file name is never matched against snapshots (no recover, no lookup)",
              c("restore report.docx") is None and c("restore the file report.docx") is None)

    rec.section("3. ambiguous restore -> clarify (listing only)")
    for t in AMBIGUOUS_RESTORE:
        r = c(t)
        rec.check(f"clarify: {t!r}", r is not None and r.kind == "clarify_restore" and r.skill == "files.recoveries"
                  and r.args == {"limit": 10} and bool(r.speech), r)

    rec.section("4. cleanup / explain / migrate wording")
    for t in CLEANUP_PHRASES:
        r = c(t)
        rec.check(f"cleanup: {t!r}", r is not None and r.kind == "cleanup" and r.skill == "files.recovery_cleanup" and r.args == {}, r)
    for t in EXPLAIN_PHRASES:
        r = c(t)
        rec.check(f"explain, nothing runs: {t!r}", r is not None and r.kind == "explain" and r.skill == "" and r.args == {} and bool(r.speech), r)
    for t in EXPLAIN_OR_NONE:
        r = c(t)
        rec.check(f"never a cleanup/recover/migrate: {t!r}", r is None or (r.kind == "explain" and r.skill == ""), r)
    for t in MIGRATE_PHRASES:
        r = c(t)
        rec.check(f"migrate: {t!r}", r is not None and r.kind == "migrate" and r.skill == "files.recovery_migrate" and r.args == {}, r)
    for t in NOT_MIGRATE:
        rec.check(f"not a migrate: {t!r}", (c(t) is None) or c(t).kind != "migrate", c(t))
    rec.check("4a 'migrate' needs both words: no migrate route for any non-migrate corpus sentence",
              all(not (c(t) and c(t).kind == "migrate") for t in NOT_MIGRATE + NEGATIVE + EXPLAIN_PHRASES + CLEANUP_PHRASES + LIST_PHRASES))
    rec.check("4b no cleanup route for any 'all' / bare-delete wording",
              all(not (c(t) and c(t).kind == "cleanup") for t in EXPLAIN_PHRASES + EXPLAIN_OR_NONE))
    rec.check("4c cleanup / migrate routes carry no arguments (nothing to steer)",
              all(getattr(c(t), "args", None) == {} for t in CLEANUP_PHRASES + MIGRATE_PHRASES))
    rec.check("4d 'delete the recovery copies' is an explanation (it is NOT what cleanup does)",
              getattr(c("delete the recovery copies"), "kind", "") == "explain")
    rec.check("4e an age word turns a delete verb into cleanup; 'all' turns it back into an explanation",
              getattr(c("delete old recovery snapshots"), "kind", "") == "cleanup"
              and getattr(c("delete all old recovery snapshots"), "kind", "") == "explain")

    rec.section("5. negative / hijack corpus: ordinary requests are not recovery requests")
    for t in NEGATIVE:
        r = c(t)
        rec.check(f"not ours: {t!r}", r is None, r)
    rec.check(f"5a corpus size >= 40 ({len(NEGATIVE)})", len(NEGATIVE) >= 40)
    rec.check("5b classify tolerates non-sentences (None / empty / punctuation) without raising",
              all(c(x) is None for x in ("", "   ", "...", "?!", "\n")) and c(None) is None)


# ==========================================================================================================
# static architecture checks: section 6 (no session needed)
# ==========================================================================================================

PROMPT_PROBE = r'''
import hashlib, json, sys
sys.path.insert(0, {root!r})
if "with" in sys.argv[1:]:
    import friday.recoveryroute  # imported BEFORE anything else
from friday.registry import REGISTRY
REGISTRY.discover()
from friday.orchestrator import Orchestrator, _tool_specs
specs = _tool_specs(None)
o = Orchestrator(llm_provider=None, tool_specs=specs, runner=None)
system, prompt = o._build_decision_prompts("Fix my Flask startup issue.", specs, [], context="")
tool_lines = "\n".join(s.line(False) for s in specs)
print(json.dumps({{
    "n_tools": len(specs), "names": sorted(s.name for s in specs),
    "tool_lines_len": len(tool_lines), "tool_lines_sha": hashlib.sha256(tool_lines.encode()).hexdigest(),
    "prompt_len": len(system) + len(prompt), "prompt_sha": hashlib.sha256((system + "\x00" + prompt).encode()).hexdigest(),
    "rr_loaded": "friday.recoveryroute" in sys.modules,
}}))
'''
PROMPT_STATS: dict = {}


def _probe(mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="friday_rr_probe_") as t:
        script = Path(t) / "probe.py"
        script.write_text(PROMPT_PROBE.format(root=str(ROOT)), encoding="utf-8")
        out = subprocess.run([sys.executable, "-X", "utf8", str(script), mode], capture_output=True, text=True,
                             cwd=str(ROOT), timeout=300)
    line = [x for x in out.stdout.splitlines() if x.startswith("{")]
    if out.returncode != 0 or not line:
        return {"error": (out.stderr or out.stdout)[-400:]}
    return json.loads(line[-1])


def static_suite(rec: Rec, rr, heavy: bool) -> None:
    from friday import orchestrator

    rec.section("6. safety architecture")
    rec.check("6.1 ROUTED_SKILLS is exactly the four recovery skills", set(rr.ROUTED_SKILLS) == FOUR, sorted(rr.ROUTED_SKILLS))
    rec.check("6.2 ROUTED_SKILLS is immutable", isinstance(rr.ROUTED_SKILLS, frozenset))
    forbidden = {"files.restore", "files.delete", "files.write", "files.move", "files.copy", "plan.run", "files.mkdir"}
    rec.check("6.3 no destructive / general skill is a routable skill", not (set(rr.ROUTED_SKILLS) & forbidden))
    rec.check("6.4 all four routed skills exist and are internal (operator-only)",
              all(REGISTRY.get(n) is not None and REGISTRY.get(n).internal for n in FOUR))
    rec.check("6.5 tiers unchanged: list L0, recover L2, cleanup L3, migrate L2",
              [REGISTRY.get(n).tier for n in ("files.recoveries", "files.recover", "files.recovery_cleanup", "files.recovery_migrate")]
              == ["L0", "L2", "L3", "L2"])
    rec.check("6.6 the kill switch exists and defaults to ON", CFG.brain.recovery_routing is True)

    corpus = (LIST_PHRASES + LIST_ALL_PHRASES + RECOVER_PHRASES + NEVER_RECOVER + COMPOUND_NONE + AMBIGUOUS_RESTORE
              + CLEANUP_PHRASES + EXPLAIN_PHRASES + EXPLAIN_OR_NONE + MIGRATE_PHRASES + NOT_MIGRATE + NEGATIVE)
    bad_skill, bad_args, bad_kind = [], [], []
    for t in corpus:
        r = rr.classify(t)
        if r is None:
            continue
        if r.skill not in FOUR and r.skill != "":
            bad_skill.append((t, r.skill))
        if KIND_SKILL.get(r.kind) != r.skill:
            bad_kind.append((t, r.kind, r.skill))
        want = {"list": {"limit"}, "clarify_restore": {"limit"}, "recover": {"snapshot"}, "cleanup": set(),
                "migrate": set(), "explain": set()}.get(r.kind)
        if want is None or set(r.args) != want:
            bad_args.append((t, r.kind, r.args))
    rec.check(f"6.7 across the {len(corpus)}-sentence corpus no route names a skill outside the four", not bad_skill, bad_skill[:3])
    rec.check("6.8 every route kind runs exactly its own skill (list/clarify -> listing only)", not bad_kind, bad_kind[:3])
    rec.check("6.9 route args never carry anything but the declared keys", not bad_args, bad_args[:3])

    specs = orchestrator._tool_specs(None)
    offered = {s.name for s in specs}
    rec.check("6.10 the planner is offered none of the four recovery skills", not (FOUR & offered), sorted(FOUR & offered))
    rec.check("6.11 ...nor files.restore (and it is still offered files.write)", "files.restore" not in offered and "files.write" in offered)
    lines = "\n".join(s.line(False) for s in specs)
    rec.check("6.12 the planner tool-description text mentions none of them", not any(n in lines for n in FOUR | {"files.restore"}))
    src_ok = {}
    for modname in ("friday.orchestrator", "friday.registry", "friday.brain.matcher", "friday.decision", "friday.intent",
                    "friday.directroute", "friday.toolfamily", "friday.toolview"):
        try:
            mod = __import__(modname, fromlist=["_"])
            src_ok[modname] = "recoveryroute" not in inspect.getsource(mod)
        except Exception as exc:  # pragma: no cover
            src_ok[modname] = f"unreadable: {exc}"
    rec.check("6.13 neither the planner path, the registry nor the matcher references recoveryroute",
              all(v is True for v in src_ok.values()), src_ok)
    refs = []
    for p in (ROOT / "friday").rglob("*.py"):
        if p.name != "recoveryroute.py" and "recoveryroute" in p.read_text(encoding="utf-8", errors="replace"):
            refs.append(p.relative_to(ROOT).as_posix())
    rec.check("6.14 only session.py (the one hook) and config.py (the switch) mention recoveryroute", sorted(refs) == ["friday/config.py", "friday/session.py"], refs)

    if heavy:
        a, b = _probe("without"), _probe("with")
        PROMPT_STATS.update(a)
        rec.check("6.15 the planner-prompt probe ran in a fresh process", "error" not in a and "error" not in b, (a.get("error"), b.get("error")))
        if "error" not in a and "error" not in b:
            rec.check("6.16 building the planner tools + prompt does not import recoveryroute", a["rr_loaded"] is False, a)
            rec.check("6.17 tool-description text is byte-identical with recoveryroute imported or not",
                      a["tool_lines_sha"] == b["tool_lines_sha"] and a["tool_lines_len"] == b["tool_lines_len"], (a["tool_lines_len"], b["tool_lines_len"]))
            rec.check("6.18 the whole decision prompt is byte-identical (same length, same hash)",
                      a["prompt_sha"] == b["prompt_sha"] and a["prompt_len"] == b["prompt_len"], (a["prompt_len"], b["prompt_len"]))
            rec.check("6.19 the planner tool names are identical and contain none of the four",
                      a["names"] == b["names"] and not (FOUR & set(a["names"])) and "files.restore" not in a["names"])


# ==========================================================================================================
# the session suite: sections 3-6 end to end through Session.handle and the REAL Executor
# ==========================================================================================================


class FellThrough(Exception):
    """BRAIN.understand was reached: the recovery router did not claim the sentence."""


def _brain_tripwire(_text):
    raise FellThrough("BRAIN.understand")


class Out:
    def __init__(self, result=None, fell=False, error=None) -> None:
        self.result, self.fell, self.error = result, fell, error

    @property
    def speech(self) -> str:
        return getattr(self.result, "speech", "") or ""

    @property
    def ok(self) -> bool:
        return bool(getattr(self.result, "ok", False))

    @property
    def data(self) -> dict:
        return getattr(self.result, "data", None) or {}


def reset_session() -> None:
    from friday.session import SESSION

    SESSION.pending = None
    SESSION.last_skill = None
    SESSION.last_args = {}
    SESSION.last_data = {}
    SESSION.last_route_gate = None
    SESSION._followup = None


async def handle(text: str, actor: str = "text") -> Out:
    from friday.session import SESSION

    reset_session()
    BRAIN.understand = _brain_tripwire  # type: ignore[method-assign]
    try:
        return Out(result=await SESSION.handle(text, actor=actor))
    except FellThrough:
        return Out(fell=True)
    except Exception as exc:  # a crash is reported, not hidden
        return Out(error=f"{type(exc).__name__}: {exc}")
    finally:
        del BRAIN.understand


class Spy:
    """Records every EXECUTOR.run call and passes it through to the real Executor."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self.blocked: list[str] = []

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    @contextlib.contextmanager
    def installed(self):
        real = EXECUTOR.run

        async def run(skill_name, args=None, *, actor="text"):
            self.calls.append((skill_name, dict(args or {}), actor))
            if skill_name not in FOUR:
                # HARD SAFETY: this suite only ever lets the four recovery skills reach the real Executor.
                # A (mutated) router that names anything else is recorded -- which is how it is caught --
                # but the call is never executed.
                self.blocked.append(skill_name)
                return SkillResult(speech="(blocked by the test harness)", ok=False)
            return await real(skill_name, args, actor=actor)

        EXECUTOR.run = run  # type: ignore[method-assign]
        try:
            yield self
        finally:
            del EXECUTOR.run


class SW:
    """One isolated environment: work folder, current + legacy stores, confirmation handler, tripwire."""

    SETTINGS = ("snapshot_dir", "snapshot_retention_days", "snapshot_keep_newest", "undo_window_days",
                "snapshot_cleanup_batch", "max_snapshot_mb")

    def __init__(self, tmp: Path) -> None:
        from friday.session import SESSION

        self.tmp = tmp
        self.work, self.snaps, self.legacy = tmp / "work", tmp / "snaps", tmp / "legacy"
        for d in (self.work, self.snaps, self.legacy):
            d.mkdir()
        self._saved = {k: getattr(CFG.files, k) for k in self.SETTINGS}
        self._paths = (paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS)
        self._routing = CFG.brain.recovery_routing
        paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = self.snaps, self.legacy
        CFG.files.snapshot_dir = ""
        CFG.files.snapshot_retention_days = 30
        CFG.files.snapshot_keep_newest = 0
        CFG.files.undo_window_days = 0
        CFG.files.snapshot_cleanup_batch = 100
        CFG.files.max_snapshot_mb = 256
        CFG.brain.recovery_routing = True
        self.answer = True
        self.asked: list[tuple[str, str]] = []
        self.violations: list[str] = []
        self._unlink, self._rmdir = os.unlink, os.rmdir
        real_roots = [p.resolve() for p in REAL_STORES]

        def watch(fn):
            def inner(p, *a, **k):
                if isinstance(p, (str, bytes, os.PathLike)):
                    try:
                        rp = Path(os.fsdecode(p)).resolve()
                    except (OSError, ValueError):
                        rp = None
                    if rp is not None and any(rp == r or r in rp.parents for r in real_roots):
                        self.violations.append(str(rp))
                        raise AssertionError(f"the suite tried to remove inside the user's real store: {rp}")
                return fn(p, *a, **k)
            return inner

        os.unlink, os.rmdir = watch(os.unlink), watch(os.rmdir)

        async def confirm(skill, args, preview):
            self.asked.append((skill.name, preview))
            return self.answer

        self._session_confirm = SESSION._confirm
        EXECUTOR.set_confirm_handler(confirm)

    def close(self) -> None:
        os.unlink, os.rmdir = self._unlink, self._rmdir
        EXECUTOR.set_confirm_handler(self._session_confirm)
        paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = self._paths
        for k, v in self._saved.items():
            setattr(CFG.files, k, v)
        CFG.brain.recovery_routing = self._routing

    def file(self, name: str, content: str = "x") -> Path:
        p = self.work / name
        p.write_text(content, encoding="utf-8")
        return p

    def clear(self) -> None:
        for base in (self.snaps, self.legacy):
            for n in os.listdir(base):
                p = base / n
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        self.asked.clear()
        self.answer = True

    def tokens(self, base: Path | None = None) -> list[str]:
        return sorted(n for n in os.listdir(base or self.snaps) if recovery.is_token(n))

    def keep(self, name: str, content: str = "kept", days: float = 100) -> str:
        """An unreferenced copy (as if its undo entry was reversed), aged `days`; the file stays."""
        token = recovery.preserve(self.file(name, content), reason="overwritten by files.write")
        mp = self.snaps / token / "meta.json"
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * DAY))
        mp.write_text(json.dumps(meta), encoding="utf-8")
        return token

    def deleted(self, name: str, content: str = "gone") -> tuple[Path, str]:
        """A file that was 'deleted by FRIDAY': taken into the recovery store; returns (original path, id)."""
        p = self.file(name, content)
        return p, recovery.preserve(p, reason="deleted by files.delete", move=True)

    def legacy_copy(self, name: str, content: str = "legacy") -> str:
        # Phase 35: the guard treats `paths.LEGACY_SNAPSHOTS` as a real store, so don't ALSO make it the write
        # target. Write into self.legacy as the "current" store while LEGACY points at an unused temp folder.
        paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = self.legacy, self.legacy.parent / "legacy-unused"
        try:
            p = self.file(name, content)
            return recovery.preserve(p, reason="overwritten by files.write")
        finally:
            paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = self.snaps, self.legacy


def audit_rows(skill: str) -> list[dict]:
    return [dict(r) for r in audit.recent(300) if r["skill"] == skill]


async def session_suite(rec: Rec) -> None:
    with tempfile.TemporaryDirectory(prefix="friday_recroute_") as t, store.use_temp_db():
        w = SW(Path(t))
        try:
            await _session_suite(rec, w)
        finally:
            w.close()


async def _session_suite(rec: Rec, w: SW) -> None:
    rr = active_rr()
    rec.section("3s. ambiguous restore through Session.handle (real Executor, two fake snapshots)")
    a = w.file("a.txt", "alpha")
    t1 = recovery.preserve(a, reason="overwritten by files.write")
    b_path, t2 = w.deleted("b.txt", "beta")
    for text in ("restore that file", "bring back the file you deleted", "recover my deleted file",
                 "restore the snapshot", "restore snapshot 3", "restore the file", "restore snapshot"):
        sp = Spy()
        with sp.installed():
            out = await handle(text)
        rec.check(f"3s {text!r}: refused (ok=False), real ids in the speech",
                  out.error is None and not out.fell and not out.ok and t1 in out.speech and t2 in out.speech, out.error or out.speech[:120])
        rec.check(f"3s {text!r}: ONLY files.recoveries was executed, nothing was asked",
                  sp.names() == ["files.recoveries"] and not w.asked, (sp.names(), w.asked))
        rec.check(f"3s {text!r}: nothing changed (snapshots + files intact)",
                  w.tokens() == sorted([t1, t2]) and a.exists() and not b_path.exists())
        rec.check(f"3s {text!r}: it is a clarification, not a result", out.data.get("clarification") == "recovery_snapshot", out.data.get("clarification"))
    w.clear()
    sp = Spy()
    with sp.installed():
        out = await handle("restore that file")
    rec.check("3s empty store: says there is nothing to restore", out.error is None and not out.ok and "nothing to restore" in out.speech.lower(), out.speech)
    rec.check("3s empty store: still only the listing ran", sp.names() == ["files.recoveries"] and not w.asked)

    rec.section("1s. a listing request never reaches cleanup / migrate / recover")
    w.clear()
    old = w.keep("old.txt", "old", days=100)
    legacy_tok = w.legacy_copy("lg.txt")
    all_names: list[str] = []
    for text in LIST_PHRASES[:12] + ["what files can you recover", "do I have any recovery snapshots"]:
        sp = Spy()
        with sp.installed():
            out = await handle(text)
        all_names += sp.names()
        rec.check(f"1s {text!r}: ok, exactly one files.recoveries call (limit 10)",
                  out.error is None and not out.fell and out.ok and sp.calls and [c[0] for c in sp.calls] == ["files.recoveries"]
                  and sp.calls[0][1] == {"limit": 10}, (out.error, sp.calls))
    sp = Spy()
    with sp.installed():
        out = await handle("show all recovery snapshots")
    all_names += sp.names()
    rec.check("1s 'show all recovery snapshots' -> files.recoveries with limit 50", sp.calls and sp.calls[0][1] == {"limit": 50}, sp.calls)
    rec.check("1s the listing shows the real id", old in out.speech, out.speech[:100])
    rec.check("1s across all listing requests only files.recoveries ever ran", set(all_names) == {"files.recoveries"}, set(all_names))
    rec.check("1s ...no confirmation was asked and nothing was deleted or copied",
              not w.asked and w.tokens() == [old] and w.tokens(w.legacy) == [legacy_tok])
    rows = audit_rows("files.recoveries")
    rec.check("1s the routed listing is audited (files.recoveries rows exist, no other recovery skill rows)",
              len(rows) >= 14 and not any(audit_rows(s) for s in FOUR - {"files.recoveries"}), len(rows))

    rec.section("2s. explicit id: files.recover runs through the Executor and asks first")
    w.clear()
    p, tok = w.deleted("c.txt", "precious")
    w.answer = False
    sp = Spy()
    with sp.installed():
        out = await handle(f"restore snapshot {tok}")
    rec.check("2s a declined restore: it asked, the file is still gone, not ok",
              out.error is None and not out.ok and w.asked and w.asked[0][0] == "files.recover" and tok in w.asked[0][1] and not p.exists(), (out.error, w.asked, out.speech))
    rec.check("2s ...exactly one executor call: files.recover with exactly {'snapshot': id}",
              sp.calls == [("files.recover", {"snapshot": tok}, "text")], sp.calls)
    rec.check("2s ...the copy is still in the store", w.tokens() == [tok])
    rows = audit_rows("files.recover")
    rec.check("2s ...the declined attempt is audited as denied", any(r["decision"] == "denied" for r in rows), [r["decision"] for r in rows])
    w.answer, w.asked = True, []
    sp = Spy()
    with sp.installed():
        out = await handle(f"restore the file from snapshot {tok}")
    rec.check("2s an approved restore brings the file back", out.ok and p.exists() and p.read_text(encoding="utf-8") == "precious", (out.speech, p.exists()))
    rec.check("2s ...it asked once and only files.recover ran", len(w.asked) == 1 and sp.names() == ["files.recover"], (w.asked, sp.names()))
    rec.check("2s ...audited as confirmed", any(r["decision"] == "confirmed" for r in audit_rows("files.recover")))
    w.asked = []
    sp = Spy()
    with sp.installed():
        out = await handle("restore snapshot 20260101T101010-0123456789ab")
    rec.check("2s a well-formed but unknown id is refused by the skill, nothing restored", not out.ok and p.exists() and sp.names() in (["files.recover"], []), (sp.names(), out.speech))
    p.unlink()
    for text in (f"restore snapshot {tok.replace('T', 't')}", f"restore snapshot {tok}f", "restore c.txt", f"restore c.txt from snapshot {tok}"):
        w.asked = []
        sp = Spy()
        with sp.installed():
            out = await handle(text)
        rec.check(f"2s {text!r}: files.recover never ran, nothing restored",
                  "files.recover" not in sp.names() and not p.exists() and not any(a_[0] == "files.recover" for a_ in w.asked), (sp.names(), w.asked))

    rec.section("4s. cleanup: asks, a decline changes nothing, unattended actors are refused, audited")
    w.clear()
    old = w.keep("o1.txt", "o1", days=100)
    sp = Spy()
    w.answer = False
    with sp.installed():
        out = await handle("clean up expired recovery snapshots")
    rec.check("4s declined cleanup: asked, not ok, the copy is untouched",
              out.error is None and not out.ok and old in w.tokens() and len(w.asked) == 1 and w.asked[0][0] == "files.recovery_cleanup", (out.error, w.asked))
    rec.check("4s ...the question says it is permanent", "PERMANENTLY" in w.asked[0][1], w.asked[0][1][:100])
    rec.check("4s ...exactly one call, files.recovery_cleanup with no arguments", sp.calls == [("files.recovery_cleanup", {}, "text")], sp.calls)
    for actor in ("scheduler", "trigger"):
        w.asked = []
        w.answer = True
        sp = Spy()
        with sp.installed():
            out = await handle("clean up expired recovery snapshots", actor=actor)
        rec.check(f"4s cleanup as {actor!r}: denied by the unattended ceiling, nothing asked, nothing deleted",
                  out.error is None and not out.ok and "not allowed" in out.speech.lower() and not w.asked and old in w.tokens(), (out.error, out.speech[:140], w.asked))
        rec.check(f"4s ...the Executor saw actor {actor!r} (the route does not launder the actor)", sp.calls and sp.calls[0][2] == actor, sp.calls)
        rec.check(f"4s ...a 'blocked' audit row exists for {actor}", any(r["decision"] == "blocked" and r["actor"] == actor for r in audit_rows("files.recovery_cleanup")))
    w.asked, w.answer = [], True
    sp = Spy()
    with sp.installed():
        out = await handle("delete old recovery snapshots")
    rec.check("4s approved cleanup deletes the old copy", out.ok and old not in w.tokens() and len(w.asked) == 1 and sp.names() == ["files.recovery_cleanup"], (out.speech, w.asked))
    rec.check("4s ...audit rows: denied, blocked and confirmed all present",
              {r["decision"] for r in audit_rows("files.recovery_cleanup")} >= {"denied", "blocked", "confirmed"},
              [r["decision"] for r in audit_rows("files.recovery_cleanup")])
    for text in ("delete all recovery snapshots", "delete the recovery copies", "prune every recovery copy", "clean up all recovery snapshots"):
        w.clear()
        keep = w.keep("e.txt", "e", days=100)
        sp = Spy()
        with sp.installed():
            out = await handle(text)
        rec.check(f"4s {text!r}: explained, NOTHING ran, nothing asked, nothing deleted",
                  out.error is None and not out.fell and not out.ok and out.speech and sp.calls == [] and not w.asked and keep in w.tokens(), (out.error, sp.calls, out.speech[:80]))
    rec.check("4s the explanation names the safe wording", "clean up expired recovery snapshots" in out.speech)

    rec.section("4s. migrate: asks, a decline changes nothing, unattended actors are refused")
    w.clear()
    lt = w.legacy_copy("m.txt", "migrate-me")
    w.answer = False
    sp = Spy()
    with sp.installed():
        out = await handle("migrate old recovery snapshots")
    rec.check("4s declined migrate: asked once, nothing copied, source untouched",
              out.error is None and not out.ok and [x[0] for x in w.asked] == ["files.recovery_migrate"] and w.tokens() == [] and w.tokens(w.legacy) == [lt], (out.error, w.asked))
    rec.check("4s ...exactly one call, files.recovery_migrate with no arguments", sp.calls == [("files.recovery_migrate", {}, "text")], sp.calls)
    for actor in ("scheduler", "trigger"):
        w.asked, w.answer = [], True
        sp = Spy()
        with sp.installed():
            out = await handle("migrate old recovery snapshots", actor=actor)
        rec.check(f"4s migrate as {actor!r}: refused (a human must confirm), nothing copied",
                  out.error is None and not out.ok and not w.asked and w.tokens() == [], (out.error, out.speech[:120], w.asked))
    w.asked, w.answer = [], True
    sp = Spy()
    with sp.installed():
        out = await handle("migrate old recovery snapshots")
    rec.check("4s approved migrate copies the snapshot and leaves the old folder alone",
              out.ok and w.tokens() == [lt] and w.tokens(w.legacy) == [lt] and len(w.asked) == 1, (out.speech, w.tokens(), w.tokens(w.legacy)))
    rows = audit_rows("files.recovery_migrate")
    rec.check("4s ...audit rows exist for migrate (denied / blocked / confirmed)", {r["decision"] for r in rows} >= {"denied", "blocked", "confirmed"}, [r["decision"] for r in rows])
    w.clear()
    w.asked = []
    sp = Spy()
    with sp.installed():
        out = await handle("migrate my database")
    rec.check("4s 'migrate my database' is not ours: it reaches the normal router, nothing ran", out.fell and sp.calls == [] and not w.asked)

    rec.section("5s. ordinary requests fall through untouched; the kill switch")
    for text in ("restore my window", "restore", "list my files", "show snapshots", "how do recovery snapshots work",
                 "show recovery snapshots and clean them up", "restore the second one", "clean up my downloads"):
        sp = Spy()
        with sp.installed():
            out = await handle(text)
        rec.check(f"5s {text!r}: reaches BRAIN, no executor call, nothing asked", out.fell and sp.calls == [] and not w.asked, (out.error, sp.calls))
    calls: list[str] = []
    real_classify = active_rr().classify

    def counting(text):
        calls.append(text)
        return real_classify(text)

    active_rr().classify = counting
    try:
        CFG.brain.recovery_routing = False
        sp = Spy()
        with sp.installed():
            out = await handle("show my recovery snapshots")
        rec.check("5s kill switch OFF: the router is never called, the sentence reaches BRAIN, nothing ran", calls == [] and out.fell and sp.calls == [], (calls, sp.calls))
        sp = Spy()
        with sp.installed():
            out = await handle("restore snapshot " + ID)
        rec.check("5s kill switch OFF: even an explicit id runs nothing", calls == [] and out.fell and sp.calls == [] and not w.asked)
        CFG.brain.recovery_routing = True
        sp = Spy()
        with sp.installed():
            out = await handle("show my recovery snapshots")
        rec.check("5s kill switch ON again: the router is consulted and routes", calls == ["show my recovery snapshots"] and not out.fell and sp.names() == ["files.recoveries"], (calls, sp.names()))
    finally:
        active_rr().classify = real_classify
        CFG.brain.recovery_routing = True

    rec.section("6s. the session refuses a route that names a non-recovery skill")
    victim = w.file("victim.txt", "do not delete")
    rr = active_rr()
    for bad in ("files.delete", "files.write", "files.restore", "files.move", "plan.run", "files.recovery_cleanup_all"):
        for kind in ("list", "recover", "cleanup", "migrate", "clarify_restore"):
            fake = rr.RecoveryRoute(kind=kind, skill=bad, args={"path": str(victim), "snapshot": ID})
            real = rr.classify
            rr.classify = lambda _t, _f=fake: _f
            try:
                sp = Spy()
                with sp.installed():
                    out = await handle("show my recovery snapshots")
            finally:
                rr.classify = real
            rec.check(f"6s a {kind!r} route naming {bad!r}: refused, EXECUTOR.run never called, file intact",
                      out.fell and sp.calls == [] and victim.exists() and not w.asked, (out.error, sp.calls))
    sp = Spy()
    with sp.installed():
        await handle("show my recovery snapshots")
        await handle("clean up expired recovery snapshots")
        await handle(f"restore snapshot {ID}")
        await handle("migrate old recovery snapshots")
        await handle("restore that file")
    rec.check("6s every routed turn went through EXECUTOR.run (list, cleanup, recover, migrate, clarify-listing)",
              sp.names() == ["files.recoveries", "files.recovery_cleanup", "files.recover", "files.recovery_migrate", "files.recoveries"], sp.names())

    rec.section("J. nothing real was touched")
    rec.check("J1 the tripwire on os.unlink / os.rmdir never saw the user's real stores", w.violations == [], w.violations)
    rec.check("J2 both stores were redirected for the whole run", paths.SNAPSHOTS == w.snaps and paths.LEGACY_SNAPSHOTS == w.legacy)


# ==========================================================================================================
# mutation machinery
# ==========================================================================================================


@contextlib.contextmanager
def src_mutant(subs=(), append: str = ""):
    """Run with a private, deliberately broken copy of recoveryroute.py installed as friday.recoveryroute."""
    import friday

    src = Path(recoveryroute.__file__).read_text(encoding="utf-8")
    for old, new in subs:
        assert src.count(old) == 1, f"mutation anchor not found exactly once: {old!r} ({src.count(old)})"
        src = src.replace(old, new, 1)
    src += append
    mod = types.ModuleType("friday.recoveryroute")
    mod.__file__ = recoveryroute.__file__
    saved_mod, saved_attr = sys.modules["friday.recoveryroute"], getattr(friday, "recoveryroute", None)
    sys.modules["friday.recoveryroute"] = mod
    friday.recoveryroute = mod  # type: ignore[attr-defined]
    try:
        exec(compile(src, "<mutant recoveryroute>", "exec"), mod.__dict__)
        yield mod
    finally:
        sys.modules["friday.recoveryroute"] = saved_mod
        friday.recoveryroute = saved_attr  # type: ignore[attr-defined]


@contextlib.contextmanager
def session_mutant(old: str, new: str):
    import friday.session as sm

    func = sm.Session._maybe_route_recovery
    src = textwrap.dedent(inspect.getsource(func))
    assert src.count(old) == 1, f"session mutation anchor not found exactly once: {old!r} ({src.count(old)})"
    fn = ast.parse(src.replace(old, new, 1)).body[0]
    fn.decorator_list = []
    ns = dict(vars(sm))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "<mutant session>", "exec"), ns)
    saved = sm.Session._maybe_route_recovery
    sm.Session._maybe_route_recovery = ns[fn.name]
    try:
        yield
    finally:
        sm.Session._maybe_route_recovery = saved


@contextlib.contextmanager
def internal_flag_mutant(name: str):
    sk = REGISTRY.get(name)
    saved = sk.internal
    sk.internal = False
    try:
        yield
    finally:
        sk.internal = saved


def _wrap(append_body: str) -> str:
    return "\n\n_orig_classify = classify\n\n\ndef classify(text):\n    r = _orig_classify(text)\n" + append_body


MUTATIONS = [
    # -- the classifier: matching discipline
    ("migrate pattern uses match() instead of fullmatch() (trailing clauses accepted)",
     lambda: src_mutant([("_MIGRATE.fullmatch(s)", "_MIGRATE.match(s)")])),
    ("cleanup pattern uses match() instead of fullmatch()",
     lambda: src_mutant([("_CLEAN.fullmatch(s)", "_CLEAN.match(s)")])),
    ("recover-with-id pattern uses match() instead of fullmatch()",
     lambda: src_mutant([("_RECOVER_WITH_REF.fullmatch(s)", "_RECOVER_WITH_REF.match(s)")])),
    ("listing patterns use search() instead of fullmatch()",
     lambda: src_mutant([("p.fullmatch(s) for p in _LIST", "p.search(s) for p in _LIST")])),
    ("the vague-restore pattern uses match() instead of fullmatch()",
     lambda: src_mutant([("_RECOVER_VAGUE.fullmatch(s)", "_RECOVER_VAGUE.match(s)")])),
    ("a bare 'snapshots' / 'copies' / 'files' is accepted as the recovery noun",
     lambda: src_mutant([('r"(?:recovery\\s+(?:snapshots?|copies|copy|store|files?|folder)"',
                          'r"(?:(?:recovery\\s+)?(?:snapshots?|copies|copy|store|files?|folder)"')])),
    ("bare 'show recovery' / 'list recovery' is accepted",
     lambda: src_mutant([('re.compile(r"recovery\\s+list")', 're.compile(r"(?:show\\s+|list\\s+|display\\s+)?recovery(?:\\s+list)?")')])),
    ("a bare 'restore' / 'recover' is treated as a recovery request",
     lambda: src_mutant([('rf"(?:recovery\\s+{_VERB}"', 'rf"(?:{_VERB}|recovery\\s+{_VERB}"')])),
    # -- ids
    ("any string is accepted as an id (is_token validation dropped)",
     lambda: src_mutant([("_ID if recovery.is_token(m.group(0)) else _BAD_ID", "_ID"),
                         ("len(found) == 1 and recovery.is_token(found[0])", "len(found) == 1")])),
    ("any 28-character string is accepted as an id (shape + validation dropped)",
     lambda: src_mutant([(r"\d{8}[Tt]\d{6}-[0-9A-Fa-f]{12}", r"[\w.-]{28}"),
                         ("_ID if recovery.is_token(m.group(0)) else _BAD_ID", "_ID"),
                         ("len(found) == 1 and recovery.is_token(found[0])", "len(found) == 1")])),
    ("ids are case-normalised (a lower-case 't' / upper-case hex id is repaired into a valid one)",
     lambda: src_mutant([("found.append(m.group(0))", "found.append(m.group(0)[:8] + 'T' + m.group(0)[9:].lower())"),
                         ("recovery.is_token(m.group(0))", "recovery.is_token(m.group(0)[:8] + 'T' + m.group(0)[9:].lower())")])),
    ("an id followed by an extra character still matches (no trailing boundary)",
     lambda: src_mutant([(r"{12}(?![\w-])", r"{12}")])),
    ("an id preceded by an extra character still matches (no leading boundary)",
     lambda: src_mutant([(r"(?<![\w-])\d{8}", r"\d{8}")])),
    ("any single word after 'restore' is treated as a snapshot reference",
     lambda: src_mutant([("if snap or ref in (_ID, _BAD_ID):", "if True:")])),
    ("recover args carry extra keys (a path to steer)",
     lambda: src_mutant([('args={"snapshot": token}', 'args={"snapshot": token, "path": "C:/Windows/System32"}')])),
    ("'restore that file' is routed to files.recover with a guessed id",
     lambda: src_mutant([("return RecoveryRoute(kind=CLARIFY_RESTORE, skill=LIST, args={\"limit\": 10}, speech=reason)",
                          'return RecoveryRoute(kind="recover", skill=RECOVER, args={"snapshot": "20260101T000000-000000000000"}, speech=reason)')])),
    # -- listing
    ("a listing request is routed to cleanup",
     lambda: src_mutant(append=_wrap('    if r is not None and r.kind == "list":\n        return RecoveryRoute(kind="cleanup", skill=CLEANUP)\n    return r\n'))),
    ("a listing request is routed to files.recover (guessed id)",
     lambda: src_mutant(append=_wrap('    if r is not None and r.kind == "list":\n        return RecoveryRoute(kind="recover", skill=RECOVER, args={"snapshot": "20260101T000000-000000000000"})\n    return r\n'))),
    ("'all' no longer widens the listing to 50",
     lambda: src_mutant([("50 if wants_all else 10", "10")])),
    ("polite trailing words ('for me', 'please') are no longer stripped",
     lambda: src_mutant([('s = re.sub(r"(?:\\s+(?:for\\s+me|please|now|thanks|thank\\s+you))+$", "", s).strip()', "s = s.strip()")])),
    # -- cleanup / explain / migrate
    ("the 'all' guard is dropped (delete ALL recovery snapshots -> cleanup)",
     lambda: src_mutant([("if explicit and not (words & _ALL_WORDS):", "if explicit:")])),
    ("any delete verb is explicit (the age-word requirement is dropped)",
     lambda: src_mutant([("explicit = bool(_CLEAN_SOFT.fullmatch(m.group(\"verb\"))) or bool(words & _AGE_WORDS)", "explicit = True")])),
    ("age words are ignored (so 'delete old recovery snapshots' no longer cleans up)",
     lambda: src_mutant([("or bool(words & _AGE_WORDS)", "or False")])),
    ("the EXPLAIN route is replaced by a cleanup route",
     lambda: src_mutant([("kind=EXPLAIN,\n            speech=(", 'kind="cleanup", skill=CLEANUP,\n            speech=(')])),
    ("the EXPLAIN route is dropped (falls through to the normal router)",
     lambda: src_mutant(append=_wrap("    if r is not None and r.kind == EXPLAIN:\n        return None\n    return r\n"))),
    ("migrate is allowed without the word 'recovery'",
     lambda: src_mutant([('r"recovery\\s+(?:snapshots?|copies|store|files?|folder)"\n    r"(?:\\s+(?:to|into)',
                          'r"(?:recovery\\s+)?(?:snapshots?|copies|store|files?|folder)"\n    r"(?:\\s+(?:to|into)')])),
    ("the recovery noun in the cleanup pattern is any word ('clean up my downloads', 'delete old files')",
     lambda: src_mutant([("{_RNP}(?:\\s+(?:that\\s+)?(?:are|is)", "(?:{_RNP}|\\w+)(?:\\s+(?:that\\s+)?(?:are|is)")])),
    # -- the constants
    ("ROUTED_SKILLS allows files.delete",
     lambda: src_mutant([("frozenset({LIST, RECOVER, CLEANUP, MIGRATE})", 'frozenset({LIST, RECOVER, CLEANUP, MIGRATE, "files.delete"})')])),
    ("ROUTED_SKILLS is a mutable set",
     lambda: src_mutant([("frozenset({LIST, RECOVER, CLEANUP, MIGRATE})", "{LIST, RECOVER, CLEANUP, MIGRATE}")])),
    ("a cleanup route is emitted with a skill argument (a planner-style steer)",
     lambda: src_mutant([('return RecoveryRoute(kind="cleanup", skill=CLEANUP)\n    m = _CLEAN.fullmatch(s)',
                          'return RecoveryRoute(kind="cleanup", skill=CLEANUP, args={"retention_days": 0})\n    m = _CLEAN.fullmatch(s)')])),
    # -- the session hook
    ("the session ignores the kill switch",
     lambda: session_mutant("if not CFG.brain.recovery_routing:", "if False:")),
    ("the session accepts a route naming ANY skill",
     lambda: session_mutant("if route.skill and route.skill not in recoveryroute.ROUTED_SKILLS:", "if False:")),
    ("the session calls the skill function directly, bypassing EXECUTOR.run",
     lambda: session_mutant("return await self._run(route.skill, route.args, actor=actor)", "return REGISTRY.get(route.skill).fn(**route.args)")),
    ("the session launders the actor to 'text' (unattended ceiling bypass)",
     lambda: session_mutant("return await self._run(route.skill, route.args, actor=actor)", 'return await self._run(route.skill, route.args, actor="text")')),
    ("the session turns an EXPLAIN route into a cleanup run",
     lambda: session_mutant('return SkillResult(speech=route.speech, ok=False, data={"clarification": "recovery_cleanup_scope"})',
                            'return await self._run("files.recovery_cleanup", {}, actor=actor)')),
    ("the session guesses: an unclear restore recovers the first listed snapshot",
     lambda: session_mutant('rows = (listing.data or {}).get("snapshots") or []',
                            'rows = (listing.data or {}).get("snapshots") or []\n        if rows:\n            return await self._run("files.recover", {"snapshot": rows[0]["id"]}, actor=actor)')),
    ("the clarification no longer shows the real ids",
     lambda: session_mutant('speech=f"{route.speech}\\n{listing.speech}\\nTell me which one, for example: restore snapshot <id>",',
                            "speech=route.speech,")),
    ("an empty store is not reported as 'nothing to restore'",
     lambda: session_mutant("if not listing.ok or not rows:", "if False:")),
    # -- the planner's view
    ("a recovery skill is made visible to the planner (internal flag dropped)",
     lambda: internal_flag_mutant("files.recovery_cleanup")),
    ("files.restore is made visible to the planner",
     lambda: internal_flag_mutant("files.restore")),
]


def run_mutated(make) -> list[str]:
    quiet = Rec(loud=False)
    try:
        with make():
            rr = active_rr()
            pure_suite(quiet, rr)
            static_suite(quiet, rr, heavy=False)
            if not quiet.failed:  # only pay for the end-to-end suite when the cheap ones missed it
                asyncio.run(session_suite(quiet))
    except AssertionError as exc:
        if "mutation anchor" in str(exc):
            raise
        quiet.failed.append(f"assertion: {exc}")
    except Exception as exc:  # a crash is also a detection: the suite did not pass
        quiet.failed.append(f"crashed: {type(exc).__name__}: {exc}")
    return quiet.failed


def main() -> int:
    t0 = time.perf_counter()
    real_default = paths._default_snapshots()
    real_before = sorted(os.listdir(real_default)) if real_default.exists() else []
    legacy_before = sorted(os.listdir(paths.LEGACY_SNAPSHOTS)) if Path(paths.LEGACY_SNAPSHOTS).exists() else []

    loud = Rec(loud=True)
    rr = active_rr()
    pure_suite(loud, rr)
    static_suite(loud, rr, heavy=True)
    asyncio.run(session_suite(loud))

    print("\n--- 7. mutation testing: break each guard; the suite must notice ---\n")
    clean = not loud.failed
    gcheck("K0 the unmutated suite is clean (mutations only count against a green baseline)", clean, loud.failed[:6])
    killed = 0
    for label, make in MUTATIONS:
        failed = run_mutated(make)
        caught = bool(failed)
        killed += caught
        gcheck(f"K mutation caught: {label}", caught and clean, "SURVIVED" if not caught else f"first: {failed[0][:90]}")
    gcheck(f"K all {len(MUTATIONS)} mutations killed", killed == len(MUTATIONS) and clean, f"{killed}/{len(MUTATIONS)}")
    gcheck(f"K at least 15 mutations ran ({len(MUTATIONS)})", len(MUTATIONS) >= 15)

    print("\n--- L. the user's real stores were never touched by the suite ---\n")
    real_after = sorted(os.listdir(real_default)) if real_default.exists() else []
    legacy_after = sorted(os.listdir(paths.LEGACY_SNAPSHOTS)) if Path(paths.LEGACY_SNAPSHOTS).exists() else []
    gcheck("L1 the real recovery store is unchanged", real_before == real_after, (real_before, real_after))
    gcheck("L2 the old in-repository data/snapshots is unchanged", legacy_before == legacy_after, (legacy_before, legacy_after))
    gcheck("L3 the router module on disk is the one that was tested (no mutant left installed)", sys.modules["friday.recoveryroute"] is recoveryroute)

    total = loud.total + len(GLOBAL)
    passed = (loud.total - len(loud.failed)) + sum(1 for _l, ok in GLOBAL if ok)
    failures = loud.failed + [lbl for lbl, ok in GLOBAL if not ok]
    if PROMPT_STATS and "prompt_len" in PROMPT_STATS:
        print(f"\nplanner prompt probe: {PROMPT_STATS['n_tools']} tools, tool lines {PROMPT_STATS['tool_lines_len']} chars, "
              f"whole decision prompt {PROMPT_STATS['prompt_len']} chars (identical with recoveryroute imported or not)")
    if failures:
        print("\nFAILED:")
        for lbl in failures:
            print(f"  - {lbl}")
    print(f"\nPhase 34.0 recovery routing  ({time.perf_counter() - t0:.1f}s)")
    print(f"checks: {passed}/{total} passed, mutations caught: {killed}/{len(MUTATIONS)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
