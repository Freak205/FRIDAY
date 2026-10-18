"""Phase 30.0 — the direct-route safety gate.

`BRAIN.understand` picks ONE skill for a sentence and fills its arguments with regexes. That is
the fast path for "what time is it" and "open Chrome", and it is also where the Phase 29
benchmark found the biggest measured failure class (33 of 65 failing runs): it asked
"Which src?" for a request that named both paths, picked `network.status` for the word "port",
handled only the first clause of a compound request, and passed a whole sentence to a skill as
its argument ("Typed 70 characters." into the wrong window).

This module decides, from the user's own words and the router's own decision, whether a direct
route may be EXECUTED. It never decides what to do instead: an unsafe route is handed to the
normal planner (`plan.run`), which has the tool schemas, the intent guard, the post-condition
verification and the evidence-grounded completion the direct path lacks. Nothing here executes,
authorises or widens anything; permission, confirmation and the audit trail all still apply to
whatever finally runs.

The question each check asks is "do the arguments faithfully and completely account for what
was said?" -- not "is the match score high?". Measured on the benchmark's own prompts the scores
of right and wrong routes overlap (0.65-0.86 for both), so no threshold on them separates the two.

  unconsumed_operand   an explicit operand (a path, a URL, a file name, a quoted string) in the
                       request appears in NO argument. The route silently dropped part of the
                       request ("put photo1.jpg and photo2.jpg into a new folder" -> mkdir of
                       the parent folder; "what port is in <path>?" -> screen.observe).
  unfillable_slot      a required argument the extractor has NO strategy for (files.copy `src`,
                       ui.click `label`) is "missing" although the request says more than the
                       skill's own vocabulary. Asking the user for it is asking for what they
                       already said; the router simply cannot read it. (A truly bare "copy a file"
                       is still asked about: a planner would have nothing to work from either.)
  whole_sentence_arg   an argument that names a target (app, window, label, ...) is the whole
                       sentence, or any argument still contains the skill's own verb
                       ("type hello world" types "type hello world").
  second_instruction   an argument still contains a second instruction ("invoice and tell me the
                       total due").
  compound_request     the request itself has an action verb in two or more clauses: one skill
                       call cannot be all of it.
  action_mismatch      the user asked to change something and the chosen skill can only look
                       ("click Increment" -> screen.active_window). Not applied to a near-verbatim
                       match of the skill's own taught phrasing.
  ambiguous_route      two different skills scored (nearly) the same, the winner is not a confident
                       match, and its arguments do not back it up (none, or an echo of the sentence). A rival that scores HIGHER than the chosen skill is not ambiguity:
                       it means an evidence-based tie-break (`brain.engine._prefer_known_app`)
                       overrode the matcher on purpose.

Phase 30.1 adds two narrow, deterministic refinements so the gate does not divert what is plainly one
safe skill call to the (much less reliable) planner:

  refine()             an EXPLICIT store instruction ("remember my bank password is X", "please remember that
                       my car is blue", "keep in mind I prefer dark mode") is routed to `memory.remember`, the
                       skill that actually WRITES. The matcher scores "remember ..." closest to `memory.recall`
                       (whose body hands such a phrase to remember() internally), which action_mismatch then
                       (rightly) refused -- sending a plain note-taking request to the planner, whose
                       LLM-written reply ("Bank password remembered") replaced the skill's own redacted
                       "Noted - ...". Only an anchored store verb counts; a question ("do you remember ...",
                       "what did I tell you ..."), "remember to <do something>" and a second clause are left
                       exactly as they were.
  ambiguous_route      is waived for a clear read-only QUESTION ("how much is the marketing budget"): a
                       question never asks to change anything, so a tie with a mutating rival (notes.add) is
                       not ambiguity; and `knowledge.ask` may win a tie with another read-only skill when the
                       knowledge base actually has documents in it (it declines, never invents, when nothing
                       matches). Anything that is not a question, or contains an action verb, is judged as before.

Skills that parse their own phrasing (`extract.parses_own_input`: schedule/routine/memory/notes/
math/timer/whatsapp/project/weather) legitimately receive most of the sentence and are exempt from
the argument-shape checks; the operand and action checks still apply to them.

Kill switch: `CFG.brain.direct_route_guard`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from friday import intent
from friday.brain.engine import Action, Understanding
from friday.brain.extract import can_fill, extract, parses_own_input
from friday.brain.matcher import Match
from friday.registry import REGISTRY

# A route that scores at least this well against one skill's own taught phrasing is a near-verbatim
# match of that skill (the same bar Session._maybe_route_to_plan uses for the same reason).
CONFIDENT_SCORE = 0.90
# Two DIFFERENT skills within AMBIGUITY_MARGIN of each other, with the winner below AMBIGUITY_CEILING,
# is a coin-flip rather than a decision.
AMBIGUITY_MARGIN = 0.015
AMBIGUITY_CEILING = 0.80

# Argument names that hold a TARGET (a window, a file, a control), where a value that is the whole
# sentence can only be wrong. Free-text arguments (query, text, expression ...) are not here: a
# search query legitimately is most of the sentence.
_TARGET_PARAMS = frozenset({
    "app", "application", "program", "window", "label", "target", "source", "src", "dst", "contact", "name",
})  # path-like arguments are checked by the operand check instead: a long absolute path is not "the sentence"

# Arguments that hold a location. A path is checked as an OPERAND (is the user's path in an argument?);
# its folder names are not words of the request, so the verb and second-instruction checks skip it.
_PATH_PARAMS = frozenset({"path", "file", "folder", "directory", "url"})

_STOP = frozenset((
    "the a an to in on of for at by with from into onto and or then also please can could would will you me my "
    "your our this that these those it its is are was were be do does did just now up out all any some i we "
    "as if so than but not no yes there here what whats how which who when where why about over under"
).split())


# -- explicit operands ----------------------------------------------------------------------------

_URL = re.compile(r"""\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s"'<>]+""")
_WIN_PATH = re.compile(r"""(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s"'<>|?*]*""")
_UNC_PATH = re.compile(r"""\\\\[^\s"'<>|?*]+""")
_REL_PATH = re.compile(r"""(?<![\w.])(?:~|\.{1,2})[\\/][^\s"'<>|?*]*""")
_QUOTED = re.compile(r"""\"([^\"]{2,})\"|“([^”]{2,})”""")
# Real file extensions only: "youtube.com" and "e.g." are not file operands.
_FILE_EXTS = (
    "pdf|docx?|xlsx?|pptx?|txt|md|csv|tsv|json|ya?ml|xml|toml|ini|cfg|log|rtf|sql|"
    "png|jpe?g|gif|bmp|svg|webp|mp[34]|wav|mkv|avi|mov|zip|7z|rar|tar|gz|"
    "py|js|ts|html?|css|bat|ps1|sh|exe|msi|dll|lnk|bak"
)
_FILE = re.compile(rf"""(?<![\w.\\/:@\-])[\w\-]+(?:\.[\w\-]+)*\.(?:{_FILE_EXTS})(?![\w\\/])""", re.IGNORECASE)
_TRAIL = ".,;:!?)]}\"'"


def _canon(value: object) -> str:
    return str(value).strip().lower().replace("\\", "/")


def explicit_operands(text: str) -> list[str]:
    """The things in `text` that are unmistakably operands: URLs, absolute / UNC / relative paths,
    file names with a real extension, and "double quoted" strings. Canonicalised (lower-case,
    forward slashes). A bare word is never an operand here: this is the part that is CERTAIN."""
    found: list[str] = []
    rest = text or ""
    for pattern in (_URL, _WIN_PATH, _UNC_PATH, _REL_PATH):
        for m in pattern.finditer(rest):
            found.append(m.group(0).rstrip(_TRAIL))
        rest = pattern.sub(" ", rest)
    for m in _FILE.finditer(rest):
        found.append(m.group(0))
    for m in _QUOTED.finditer(text or ""):
        found.append(m.group(1) or m.group(2))
    out: list[str] = []
    for item in found:
        c = _canon(item)
        if c and c not in out and any(ch.isalnum() for ch in c):
            out.append(c)
    return out


def mask_operands(text: str) -> str:
    """`text` with every explicit operand replaced by the neutral word "operand". The folder and file
    names inside a path are not words of the request (a folder called reports is not the verb "report"), and the
    ":" and "." inside a path are not clause boundaries. Verb analysis must run on this."""
    rest = text or ""
    for pattern in (_URL, _WIN_PATH, _UNC_PATH, _REL_PATH, _FILE, _QUOTED):
        rest = pattern.sub(" operand ", rest)
    return rest


def _consumed(operand: str, values: list[str]) -> bool:
    """Is the operand accounted for by one of the arguments? The argument may hold it (a resolved or
    expanded form: "~/a.txt" -> "c:/users/me/a.txt") or be its stem ("notepad" for "notepad.exe"); a short
    argument that merely appears somewhere inside a long operand ("doc" in ".../docs/x.pdf") does not count."""
    bare = operand.lstrip("~./")
    for v in values:
        if operand in v or (bare and bare in v):
            return True
        if len(v) >= 3 and (operand.startswith(v) or operand.endswith("/" + v)):
            return True
    return False


def unconsumed_operands(text: str, args: dict) -> list[str]:
    values = [_canon(v) for v in args.values() if v not in (None, "") and not isinstance(v, bool)]
    return [op for op in explicit_operands(text) if not _consumed(op, values)]


# -- word helpers ---------------------------------------------------------------------------------


def _content(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP]


def _own_verbs(skill_name: str) -> set[str]:
    """The skill's own action word(s): the words of the last segment of its name that the project's
    verb table recognises as verbs ("input.type" -> type, "ui.right_click" -> click; "screen.find_text"
    -> find, not text)."""
    return {
        w for w in re.split(r"[._]", skill_name.rsplit(".", 1)[-1])
        if len(w) >= 4 and intent.is_action_word(w)
    }


# -- the checks (each returns a reason code, or None when it has no objection) ----------------------


def _check_unconsumed_operand(text: str, u: Understanding) -> str | None:
    left = unconsumed_operands(text, u.args)
    return f"unconsumed_operand:{left[0]}" if left else None


_VOCAB: dict[str, frozenset[str]] = {}


def _vocab(skill) -> frozenset[str]:
    """Every content word the skill itself uses to describe what it does: its name, description,
    taught phrasings and parameter names."""
    if skill.name not in _VOCAB:
        words: set[str] = set(_content(skill.name.replace(".", " ").replace("_", " ")))
        words |= set(_content(skill.description))
        for example in skill.examples:
            words |= set(_content(example))
        for p in skill.params:
            words |= set(_content(p.name.replace("_", " ")))
        _VOCAB[skill.name] = frozenset(words)
    return _VOCAB[skill.name]


def _is_bare(text: str, u: Understanding, skill) -> bool:
    """Nothing in the request but the skill's own vocabulary: "copy a file", "rename the file"."""
    return not explicit_operands(text) and all(w in _vocab(skill) for w in _content(u.normalized))


def _check_unfillable_slot(text: str, u: Understanding) -> str | None:
    if u.action is not Action.ASK_SLOT:
        return None
    skill = REGISTRY.get(u.skill or "")
    if skill is None:
        return None
    by_name = {p.name: p for p in skill.params}
    for name in u.missing:
        param = by_name.get(name)
        if param is not None and not can_fill(skill, param) and not _is_bare(text, u, skill):
            return f"unfillable_slot:{name}"
    return None


def _check_whole_sentence_arg(u: Understanding) -> str | None:
    if parses_own_input(u.skill or ""):
        return None
    said = set(_content(u.normalized))
    verbs = _own_verbs(u.skill or "")
    for key, value in u.args.items():
        if not isinstance(value, str) or key in _PATH_PARAMS:
            continue
        got = set(_content(value))
        if verbs & got and len(got) >= 2:
            return f"whole_sentence_arg:{key} contains the skill's own verb"
        if key in _TARGET_PARAMS and len(said) >= 4 and len(got) >= 4 and len(got & said) / len(said) >= 0.75:
            return f"whole_sentence_arg:{key} is the whole sentence"
    return None


def _check_second_instruction(u: Understanding) -> str | None:
    if parses_own_input(u.skill or ""):
        return None
    for key, value in u.args.items():
        if key in _PATH_PARAMS:
            continue
        if isinstance(value, str) and any(clause for clause in intent.clause_verbs(mask_operands(value))[1:]):
            return f"second_instruction:{key}"
    return None


# Phase 38: a request is more than one when two of its clauses are each a request. The verb table alone misses the
# read half ("what is my laptop's name" has no action verb) and phrasal system verbs ("shut it down"), so
# "what is my laptop's name and shut it down" used to look like ONE request and ran as a bare system.shutdown.
_CLAUSE_JOIN = re.compile(r"\s*(?:[,;]|\band\s+then\b|\band\s+also\b|\bthen\b|\balso\b|\bafter\s+that\b|\bplus\b|\band\b)\s*", re.IGNORECASE)
_READ_LEAD = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:(?:what|when|where|who|whom|whose|which|how)\b"
    r"|(?:tell|show|remind)\s+me\b|do\s+i\s+have\b|do\s+you\s+(?:know|remember)\b|is\s+my\b|are\s+my\b)",
    re.IGNORECASE,
)
_SYSTEM_IMPERATIVE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:shut\s*(?:\w+\s+)?down|shutdown|power\s+(?:off|down)|turn\s+(?:it\s+)?off"
    r"|restart|reboot|log\s*(?:off|out)|sign\s*out|hibernate|lock\b|sleep\b)",
    re.IGNORECASE,
)


def request_clauses(text: str) -> list[str]:
    """The clauses of `text` that are each a request in their own right: a question (interrogative lead), a spoken
    system command ("shut it down"), or anything the verb table recognises as an action."""
    masked = mask_operands(text)
    out: list[str] = []
    for clause in _CLAUSE_JOIN.split(masked):
        if clause.strip() and (
            _READ_LEAD.match(clause) or _SYSTEM_IMPERATIVE.match(clause)
            or any(intent.clause_verbs(clause))
        ):
            out.append(clause.strip())
    return out


def _check_compound_request(text: str, u: Understanding) -> str | None:
    if u.skill and u.skill.startswith("memory.") and len(request_clauses(text)) >= 2:
        # the memory skills parse their own input (exempt below), but one of them silently answering half of a
        # two-part request -- or storing it -- is exactly the dropped intent this check exists for
        return "compound_request:memory"
    if sum(1 for c in _CLAUSE_JOIN.split(mask_operands(text)) if _READ_LEAD.match(c)) >= 2:
        return "compound_request:questions"  # two questions, one skill call: "what time is it and what's the weather"
    if parses_own_input(u.skill or "") or u.score >= CONFIDENT_SCORE:
        return None
    acting = [c for c in intent.clause_verbs(mask_operands(text)) if c]
    return "compound_request" if max(len(acting), len(request_clauses(text))) >= 2 else None


# Phase 38: one read-only skill answers ONE kind of fact. "what's my battery level and cpu usage" matches system.info
# (cpu / memory / disk) at 0.86 and would answer the cpu half only, in a reply that sounds complete. When the sentence
# names facts from two different kinds, the chosen skill has to cover every one of them, else the request is not direct.
# Deliberately small: a handful of kinds of machine fact, and only skills that answer exactly one kind are listed (a skill
# not listed here is never judged by this check).
_FACTS = {
    "battery": r"batter(?:y|ies)|charging|plugged\s+in",
    "resources": r"cpu|processor|ram|memory|disk|storage",
    "gpu": r"gpu|graphics\s+card",
    "time": r"time|date|clock",
    "weather": r"weather|forecast|rain|raining",
    "volume": r"volume",
    "brightness": r"brightness",
    "network": r"wi-?fi|network|internet|ip\s+address",
}
_FACTS_ANSWERED = (
    ("system.battery", {"battery"}), ("system.info", {"resources"}), ("process.top", {"resources"}),
    ("system.time", {"time"}), ("weather.", {"weather"}), ("system.volume.", {"volume"}),
    ("system.brightness.", {"brightness"}), ("network.", {"network"}),
)


def facts_named(text: str) -> set[str]:
    t = (text or "").lower()
    return {kind for kind, words in _FACTS.items() if re.search(rf"\b(?:{words})\b", t)}


def _check_multiple_facts(text: str, u: Understanding) -> str | None:
    answered = next((kinds for prefix, kinds in _FACTS_ANSWERED if (u.skill or "").startswith(prefix)), None)
    if answered is None:
        return None
    named = facts_named(text)
    return f"multiple_facts:{'+'.join(sorted(named))}" if len(named) >= 2 and not named <= answered else None


# Phase 38: "I forgot my umbrella" is a thing that happened, not "forget my umbrella" and not a request of any kind.
_FORGOT_STATEMENT = re.compile(r"^\s*(?:please\s+)?(?:i|we)(?:\s+have|['’]ve)?\s+(?:forgot(?:ten)?)\b", re.IGNORECASE)
# The only ways to tell FRIDAY to delete a stored memory / document by voice: an imperative lead.
_FORGET_COMMAND = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+|i\s+(?:want|need)\s+you\s+to\s+|you\s+(?:can|should|must)\s+)?"
    r"(?:forget|delete|erase|remove|clear|wipe|stop\s+remembering)\b", re.IGNORECASE
)


def _check_forget_intent(text: str, u: Understanding) -> str | None:
    """`memory.forget` / `knowledge.forget` delete stored data, so the words must ask for that: an imperative
    forget/delete/erase ("forget my old address"). "I forgot my umbrella" and "don't forget my umbrella" do not."""
    if (u.skill or "") not in ("memory.forget", "knowledge.forget"):
        return None
    if not _FORGET_COMMAND.match(text or ""):
        return "not_a_forget_instruction"
    # memory.forget deletes what the user TOLD FRIDAY; a document / file / note is another skill's (knowledge.forget, files,
    # notes), so a "forget the knowledge about onboarding" the matcher gave to memory.forget by a hair is not direct
    target = _MEMORY_FORGET.match(text or "")
    if u.skill == "memory.forget" and target and _NOT_MEMORY.search(target.group("target")) and not _TOLD_YOU.search(target.group("target")):
        return "forget_target_is_not_a_memory"
    return None


def _check_forgot_statement(text: str, u: Understanding) -> str | None:
    """"I forgot ..." describes the past. Whatever skill the matcher picked, it was not asked for -- unless the
    sentence closes with a recall question about a personal fact ("I forgot my blood type, what was it?")."""
    if not _FORGOT_STATEMENT.match(text or "") or personal_memory_question(text):
        return None
    return "not_a_request:past_statement"


def _is_recall_question(text: str) -> bool:
    """Is this a lookup that merely contains the word remember ("do you remember my wifi password", "did I tell you ...",
    "remember what I said about X")? The word
    "remember" is there, but the sentence ASKS (the memory skill's own definition of a question, with or without
    its "remember" lead). Every verb in it must be "remember"/"note"; "can you remember to lock the door" is not."""
    from friday.skills.memory import _LEAD, _QUESTION

    t = (text or "").strip()
    lead = _LEAD.match(t)
    if not (_QUESTION.match(t) or (lead and _QUESTION.match(t[lead.end():]))):
        return False
    verbs = [w for clause in intent.clause_verbs(mask_operands(t)) for _g, w in clause]
    return bool(verbs) and all(w in ("remember", "note") for w in verbs)


def _check_action_mismatch(text: str, u: Understanding) -> str | None:
    skill = u.skill or ""
    # A near-verbatim match of the skill's own taught phrasing is the skill designer's statement of
    # what the phrase means ("add 340 and 195" -> math.calculate, "google how to FIX a tyre" ->
    # web.search); a verb inside such a phrase is part of its argument, not a second request.
    if REGISTRY.get(skill) is None or u.score >= CONFIDENT_SCORE:
        return None
    possible = intent.possible_classes(skill)
    scope = intent.derive_scope(mask_operands(text))
    if scope.basis != "verbs":
        return None  # no recognised verb at all ("crank it up"): nothing to compare against
    if scope.allowed & intent.ACTING and possible <= intent.PASSIVE:
        if skill == "memory.recall" and (_is_recall_question(text) or personal_memory_question(text)):
            return None  # "do you remember ..." / "remind me what my X is" is a lookup, not a change
        return "action_mismatch:asked to change something, the skill can only look"
    return None


def _corroborated(u: Understanding) -> bool:
    """Do the extracted arguments back the choice of skill? A number, or a value that is only PART of what
    was said (contact=rahul, message=checking in), is evidence the skill's own parser recognised its
    phrasing. No arguments at all (screen.observe), or an argument that just echoes the sentence
    (memory.recall(query=<the whole request>)), is not."""
    said = set(_content(u.normalized))
    for value in u.args.values():
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str) and value.strip():
            got = set(_content(value))
            if got and (not said or len(got & said) / len(said) < 0.75):
                return True
    return False


# A sentence that opens with an interrogative word. The phrase is a QUESTION, which can only ask to be told something.
_QUESTION_LEAD = re.compile(r"^\s*(?:please\s+)?(?:how|what|when|where|who|whom|whose|which|why)\b", re.IGNORECASE)


def _kb_has_documents() -> bool:
    """Is anything indexed in the knowledge base? One cheap read-only SELECT on the current database; any
    failure counts as "no" (the route is then judged by the ordinary, stricter rule)."""
    try:
        from friday import knowledge

        return bool(knowledge.list_documents())
    except Exception:
        return False


def _clear_read_question(text: str, u: Understanding, rival) -> bool:
    """Is this a plain question, answered by a read-only skill, whose tie with `rival` cannot hurt?

    Yes when the sentence opens with an interrogative word and names no action verb (so it cannot be a
    request to do something), the chosen skill can only look, and EITHER the rival would change something
    (a question is never a request to change anything: "how much is the marketing budget" is not
    "add a note: marketing budget"), OR the chosen skill is `knowledge.ask` and documents are indexed (a
    read-only retrieval that declines, rather than invents, when nothing matches)."""
    skill = REGISTRY.get(u.skill or "")
    other = REGISTRY.get(rival.skill)
    if skill is None or other is None or not _QUESTION_LEAD.match(text or ""):
        return False
    # derive_scope() deliberately grants nothing for a question ("how do I delete X" asks for advice), so the verbs are
    # read directly: any verb other than a look/read/show one makes this more than a plain question.
    masked = mask_operands(text)
    if intent.derive_scope(masked).allowed & intent.ACTING:
        return False
    if any(group not in ("passive", "present") for clause in intent.clause_verbs(masked) for group, _w in clause):
        return False
    mine = intent.possible_classes(skill.name)
    if not mine or not mine <= intent.PASSIVE:
        return False
    theirs = intent.possible_classes(other.name)
    if theirs and not theirs <= intent.PASSIVE:
        return True
    return skill.name == "knowledge.ask" and _kb_has_documents()


def _check_ambiguous_route(u: Understanding, text: str = "") -> str | None:
    if u.score >= AMBIGUITY_CEILING or _corroborated(u):
        return None
    rival = next((c for c in u.candidates if c.skill != u.skill), None)
    if rival is not None and 0 <= u.score - rival.score < AMBIGUITY_MARGIN:
        if _clear_read_question(text or u.utterance, u, rival):
            return None
        return f"ambiguous_route:{rival.skill}"
    return None


# -- Phase 30.1: an explicit store instruction goes to the skill that stores --------------------------------

# An anchored store verb: remember / memorise / keep in mind / don't forget, then the thing to keep. "note",
# "save" and "store" are deliberately NOT here: "note ..." is notes.add's phrasing and "save"/"store" mostly mean
# files. "can you remember ..." / "do you remember ..." are questions, so there is no "can you" in the lead.
_STORE_LEAD = re.compile(
    r"^\s*(?:please\s+)?(?:remember|memori[sz]e|keep in mind|don'?t forget)\s+(?P<that>that\s+)?(?P<fact>\S.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
# "remember to call mom" is a reminder, "remember this" / "remember it" has nothing to store.
_NOT_A_FACT_START = frozenset("to this it them these those me us".split())


def explicit_store(text: str) -> str | None:
    """The fact to store when `text` is an unmistakable instruction to remember something, else None:
    an anchored store verb; the rest is a statement (at least two words, not a question, not "to <do
    something>", no question mark anywhere at the end); and nothing else is being asked in the same breath."""
    m = _STORE_LEAD.match(text or "")
    if not m:
        return None
    fact = m.group("fact")
    if text.rstrip().endswith("?"):
        return None
    from friday.skills.memory import _QUESTION  # the memory skill's own idea of a question

    if _QUESTION.match(fact):
        return None
    words = re.findall(r"[\w']+", fact)
    if len(words) < 2 or (not m.group("that") and words[0].lower() in _NOT_A_FACT_START):
        return None
    if sum(1 for clause in intent.clause_verbs(mask_operands(text)) if clause) >= 2 or len(request_clauses(text)) >= 2:
        return None  # "remember my laptop's name is Orion and shut it down" stores nothing and shuts nothing down
    return fact


# Phase 38: the one shape of store instruction that is safe to make deterministic whatever the matcher guessed:
# "remember my <thing> is <value>". Anything vaguer ("remember the meeting at 5") keeps the narrower Phase 30.1 rule.
_ASSERTION = re.compile(r"^(?:that\s+)?(?:my|our)\s+(?P<scope>(?:[a-z]+(?:['’]s)?\s+){0,2}[a-z]+(?:['’]s)?)\s+(?:is|are|was|were)\s+\S", re.IGNORECASE)


# An unmistakable request to LIST the indexed documents. The matcher scores "list my documents" a hair closer
# to knowledge.ask (which would search for "list my") than to knowledge.list, so the explicit listing is made
# deterministic here; anything with extra words ("list my plans for the budget") is not matched.
_LIST_DOCS = re.compile(
    r"^\s*(?:please\s+)?(?:can you\s+)?(?:list|show|display)(?:\s+me)?(?:\s+all)?(?:\s+(?:of\s+)?(?:my|the|your))?"
    r"(?:\s+(?:indexed|saved|stored))?\s+(?:documents|docs|knowledge base|knowledge)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def explicit_list_documents(text: str) -> bool:
    return bool(_LIST_DOCS.match(text or ""))


# A plain question about a personal fact: "what is / what's / what are my <1-4 plain words>". Nothing else fits
# (no other verb, no digits, no punctuation inside, no "and"), so it can only ask to be told something.
_PERSONAL_FACT_Q = re.compile(
    r"^\s*(?:please\s+)?(?:what\s+is|what['\u2019]s|what\s+are)\s+my\s+(?P<thing>[a-z]+(?:['\u2019]s)?(?:\s+[a-z]+(?:['\u2019]s)?){0,3})\s*\??\s*$",
    re.IGNORECASE,
)
# The system / developer things "my ..." can also name. Those have skills of their own that must keep winning
# ("what is my git status", "what is my cpu usage"), so a question naming one is never taken for a personal fact.
_NOT_PERSONAL = frozenset((
    "git commit branch repo repository status log cpu ram disk storage battery volume brightness ip network usage "
    "time date weather screen and or"
).split())


def personal_fact_question(text: str) -> bool:
    m = _PERSONAL_FACT_Q.match(text or "")
    return bool(m) and not any(w in _NOT_PERSONAL for w in re.findall(r"[a-z]+", m.group("thing").lower()))


def _recall_for_personal_fact(text: str, u: Understanding) -> Understanding:
    """Phase 30.3: "what is my blood type" scores a coin-flip between dev.git and memory.recall (and "what is my shoe
    size" between memory.recall and weather.now), so the gate sent it to the LLM planner, which invented an answer.
    A plain personal-fact question is memory.recall's job, and recall only reads. Only a decision that IS such a
    tie with memory.recall (within the ambiguity margin, below the ceiling, arguments not backing the other skill)
    is changed; a confident pick (a system-status skill, dev.git for "what is my git status") is left alone."""
    recall = REGISTRY.get("memory.recall")
    if recall is None or not personal_fact_question(text) or u.score >= AMBIGUITY_CEILING:
        return u
    tied = next((c for c in u.candidates if c.skill == recall.name and 0 <= u.score - c.score < AMBIGUITY_MARGIN), None)
    if u.skill == recall.name:
        if not any(0 <= u.score - c.score < AMBIGUITY_MARGIN for c in u.candidates if c.skill != recall.name):
            return u  # recall is already the clear pick
    elif tied is None or _corroborated(u):
        return u
    args, missing = extract(recall, u.normalized or text)
    if missing:
        return u
    # The skills that tied with recall are what is being decided against, not rivals of the new pick.
    rest = [c for c in u.candidates if c.skill != recall.name and not 0 <= u.score - c.score < AMBIGUITY_MARGIN]
    return Understanding(
        action=Action.ACT, utterance=u.utterance, normalized=u.normalized, skill=recall.name, args=args,
        score=u.score, candidates=[Match(skill=recall.name, score=u.score, matched_example="")] + rest,
    )


# Phase 37: a question ABOUT THE USER ("when is my mother's birthday", "do i have any allergies", "remind me what my
# locker number is", "tell me my blood type") can only be answered from what the user told FRIDAY. The matcher
# scores these against whichever skill has a similar frame (system.time, weather.now, timer.set, battery ...) and
# the first two of those were SAFE direct routes: FRIDAY answered a birthday question with the clock, and a
# "remind me what my locker number is" would have set a timer. The question frame, a possessive phrase whose noun
# is a personal one, and the absence of any system / file word are all required, so it is deliberately narrow.
_PM_FRAME = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
    r"(?:(?:what|when|where|who|whom|whose|which)\b|how\s+(?:old|tall)\b|(?:tell|remind)\s+me\s+(?:what|when|where|who|which)\b"
    r"|tell\s+me\s+(?=my\b)|do\s+you\s+(?:know|remember)\b|do\s+i\s+have\b|did\s+i\s+(?:tell|say|mention)\b)",
    re.IGNORECASE,
)
_DO_I_HAVE = re.compile(r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?do\s+i\s+have\b", re.IGNORECASE)
_MY_PHRASE = re.compile(r"\bmy\s+((?:[a-z]+(?:['\u2019]s)?\s+){0,2}[a-z]+(?:['\u2019]s)?)", re.IGNORECASE)
_PERSONAL_NOUNS = frozenset((
    "allergy allergies birthday birthdate anniversary blood size phone passport licence license plate locker insurance "
    "doctor dentist therapist mother mom mum father dad sister brother sibling wife husband spouse partner girlfriend "
    "boyfriend son daughter child kids cousin aunt uncle grandmother grandfather grandma grandpa boss manager teacher "
    "landlord neighbor neighbour roommate dog cat pet hobby hobbies favorite favourite medication prescription "
    "address nickname"
).split())
# Words that make "my <noun>" a thing on this machine (a file's size, the screen, a MAC address) rather than a fact
# about the user. Added to the system / developer words the narrower personal-fact rule already refuses.
_NOT_PERSONAL_MORE = frozenset((
    "file files folder folders directory download downloads document documents window windows display monitor drive font "
    "image picture photo app apps browser tab clipboard mac os version"
).split())


# Phase 38, gap 1: "what is my laptop's name" -- the owner is a thing, not on the personal-noun list, and the matcher
# scores it as system.battery ("laptop"), which then ties with process.top, so the gate sent it to the planner. No skill
# reports a machine's name, so a "<thing>'s name" can only be what the user once told FRIDAY. Only the attribute
# "name"/"nickname" is added (never "laptop" itself: "my laptop's battery level" stays a system question), and the
# owner may not be a network / account / process word whose name a system skill DOES answer ("my wifi name").
_NAME_OF_MINE = re.compile(r"^(?:[a-z]+\s+)?[a-z]+(?:'s)?\s+(?:name|nickname)$", re.IGNORECASE)
_NAME_NOT_PERSONAL = frozenset(
    "wifi wi fi network ssid router hotspot bluetooth vpn user username account profile host hostname process service "
    "task device session server domain workgroup printer gpu cpu ram project workspace desktop repo branch monitor app".split()
)
# Phase 38, gap 2: "I forgot my blood type, what was it?" opens with a statement, so the question frame never matched
# (it only reached recall on a lucky 0.65 score). The statement only counts with a recall question closing it, so
# "I forgot my passport at home" is not a lookup.
_FORGOT_Q = re.compile(
    r"^\s*(?:please\s+)?(?:i|i['\u2019]ve)\s+(?:forgot(?:ten)?|have\s+forgotten|can['\u2019]?t\s+remember|cannot\s+remember"
    r"|don['\u2019]?t\s+remember|do\s+not\s+remember)\s+(?P<rest>my\b.+?)[,;:]?\s*"
    r"(?:and\s+)?(?:what\s+(?:was|is|were|are)\s+(?:it|that|they)|(?:can|could|would)\s+you\s+(?:tell|remind)\s+me"
    r"|do\s+you\s+(?:know|remember)(?:\s+(?:it|that|them))?)\s*[?.!]?\s*$",
    re.IGNORECASE,
)


def _personal_scope(scope: str) -> bool:
    """Is the phrase after "my" a personal fact the user can have told FRIDAY: a personal noun, or the "name" of
    something that is not a network / account / process / hardware thing?"""
    scope = re.sub(r"\s+(?:was|is|were|are)$", "", scope.strip().lower())  # "what did I say my laptop's name was"
    scope_words = re.findall(r"[a-z]+", scope)
    if any(w in _PERSONAL_NOUNS for w in scope_words):
        return True
    return _NAME_OF_MINE.match(scope.replace("’", "'")) is not None and not any(w in _NAME_NOT_PERSONAL for w in scope_words)


def personal_statement(text: str) -> bool:
    """"My cat's name is Tom" / "my blood type is O positive": a DECLARATION about the user. It tells FRIDAY something
    and asks nothing, so it is never a lookup; whether to keep it is for the user to say ("remember ...")."""
    t = (text or "").strip()
    m = _ASSERTION.match(t)
    if m is None or len(t.split()) > 20 or "?" in t or _READ_LEAD.match(t) or len(request_clauses(t)) >= 1:
        return False
    words = re.findall(r"[a-z]+", t.lower().replace("’", "'"))
    return not any(w in _NOT_PERSONAL or w in _NOT_PERSONAL_MORE for w in words) and _personal_scope(m.group("scope"))


def personal_memory_question(text: str) -> bool:
    t = (text or "").strip()
    if len(t.split()) > 14 or len(request_clauses(t)) >= 2:
        return False
    forgot = _FORGOT_Q.match(t)
    if not (forgot or _PM_FRAME.match(t)):
        return False
    words = re.findall(r"[a-z]+", t.lower().replace("\u2019", "'"))
    if any(w in _NOT_PERSONAL or w in _NOT_PERSONAL_MORE for w in words):
        return False
    if _DO_I_HAVE.match(t):
        scope = t
    else:
        m = _MY_PHRASE.search(forgot.group("rest") if forgot else t)
        if m is None:
            return False
        scope = m.group(1)
    return _personal_scope(scope)


def _recall_for_personal_memory(text: str, u: Understanding) -> Understanding:
    """Make `memory.recall` (which only reads) the decision for a `personal_memory_question`, whatever the matcher
    preferred, unless the phrase is a near-verbatim taught example of another skill (score >= CONFIDENT_SCORE) or
    it is already a clear recall. The recall skill itself declines when nothing stored backs the answer."""
    recall = REGISTRY.get("memory.recall")
    if recall is None:
        return u
    if u.skill:
        if u.action not in (Action.ACT, Action.ASK_SLOT):
            return u
        if u.skill != "plan.run" and u.score >= CONFIDENT_SCORE and not u.skill.startswith("memory."):
            return u
        if u.skill == recall.name and not any(
            0 <= u.score - c.score < AMBIGUITY_MARGIN for c in u.candidates if c.skill != recall.name
        ):
            return u
    elif u.action is not Action.UNKNOWN:
        return u  # no skill was matched at all ("what is my favorite food"): that is where recall is needed most
    args, missing = extract(recall, u.normalized or text)
    if missing:
        return u
    rest = [c for c in u.candidates if c.skill not in (recall.name, u.skill) and not 0 <= u.score - c.score < AMBIGUITY_MARGIN]
    return Understanding(
        action=Action.ACT, utterance=u.utterance, normalized=u.normalized, skill=recall.name, args=args,
        score=u.score, candidates=[Match(skill=recall.name, score=u.score, matched_example="")] + rest,
    )


def _hold_statement(text: str, u: Understanding) -> Understanding | None:
    """Phase 38: a sentence that only DESCRIBES something ("My cat's name is Tom", "I forgot my umbrella") asks for
    nothing, so nothing runs: not a recall (it is not a question), not a clock / lock / forget (the matcher's guess).
    FRIDAY says what it would take instead. Left alone: a near-verbatim taught phrase of the memory skills (the
    existing "my sister's name is priya" convention), and any decision that is already not a direct skill call."""
    if personal_statement(text):
        if (u.skill or "").startswith("memory.remember") or u.action not in (Action.ACT, Action.ASK_SLOT, Action.UNKNOWN):
            return None
        speech = "That sounds like something to keep rather than a question. If you want me to store it, say 'remember' and then the fact."
    elif _FORGOT_STATEMENT.match(text or "") and not request_clauses(text):
        speech = "Sounds like that slipped your mind. I haven't forgotten or deleted anything; tell me what you'd like me to do."
    elif _DONT_FORGET_THING.match(text or "") and len(request_clauses(text)) <= 1 and u.action in (Action.ACT, Action.ASK_SLOT, Action.UNKNOWN):
        speech = ("I haven't stored or deleted anything. If you want me to keep something in mind, say 'remember' and then the fact, "
                  "like 'remember my umbrella is by the door'.")
    else:
        return None
    return Understanding(action=Action.UNKNOWN, utterance=u.utterance, normalized=u.normalized, score=u.score, speech=speech)


# "don't forget my umbrella": a bare thing, not a fact ("don't forget my car is parked on level 3" states one and keeps
# storing, and "don't forget to call mom" / "don't forget that ..." keep their own paths). Nothing is stored, nothing deleted.
_DONT_FORGET_THING = re.compile(
    r"^\s*(?:please\s+)?(?:don['’]?t|do\s+not)\s+forget\s+(?!to\b|that\b)(?!.*\b(?:is|are|was|were)\b)\S.*$", re.IGNORECASE | re.DOTALL
)

# Phase 38: an imperative forget of something the user TOLD FRIDAY is `memory.forget`, whatever the matcher guessed. The
# matcher cannot tell it from `knowledge.forget` (indexed documents: 0.78 vs 0.77) or from recall, and an ambiguous
# delete rightly goes to the planner -- which made "forget that my cat is named Tom" a roundabout, non-deterministic
# route. Evidence for memory is required and documents veto it: a sentence about a document / file / note is left to
# the ordinary rules, and "I forgot ..." / "don't forget ..." never match (they have no imperative lead).
_MEMORY_FORGET = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+|i\s+(?:want|need)\s+you\s+to\s+|you\s+(?:can|should|must)\s+)?"
    r"(?:forget|delete|erase|remove|clear|wipe)\s+(?P<target>\S.*?)\s*[.!]?\s*$", re.IGNORECASE | re.DOTALL
)
_TOLD_YOU = re.compile(
    r"\b(?:the\s+fact|what\s+(?:i|you)\s+(?:said|told|know)|(?:i|you)\s+(?:told|said|mentioned)|that\s+(?:i|my|our)\b|memor(?:y|ies))", re.IGNORECASE
)
_NOT_MEMORY = re.compile(
    r"\b(?:documents?|docs?|files?|pdfs?|folders?|knowledge|index(?:ed)?|library|articles?|papers?|notes?|reminders?|alarms?"
    r"|routines?|schedules?|tasks?|history|cache|downloads?)\b|\.\w{2,4}\b", re.IGNORECASE
)


def _forget_memory(text: str, u: Understanding) -> Understanding | None:
    skill = REGISTRY.get("memory.forget")
    m = _MEMORY_FORGET.match(text or "")
    if skill is None or m is None or _NOT_MEMORY.search(m.group("target")) or len(request_clauses(text)) >= 2:
        return None
    mine = _MY_PHRASE.search(m.group("target"))
    # evidence that this is something the user said: an explicit "the fact I told you" frame, or "my <personal thing>"
    if not (_TOLD_YOU.search(m.group("target")) or (mine is not None and (u.skill == skill.name or _personal_scope(mine.group(1))))):
        return None
    if u.skill and u.skill != skill.name and u.score >= CONFIDENT_SCORE and u.skill != "plan.run":
        return None  # a near-verbatim phrase of another skill ("delete my 3pm reminder") stays that skill's
    args, missing = extract(skill, text.strip())
    if missing:
        return None
    # the rivals are not carried over: knowledge.forget at 0.77 would make this deliberate decision a "coin-flip"
    return Understanding(
        action=Action.ACT, utterance=u.utterance, normalized=u.normalized, skill=skill.name, args=args,
        score=u.score, candidates=[Match(skill=skill.name, score=u.score, matched_example="")],
    )


def _store_assertion(text: str, u: Understanding) -> Understanding | None:
    """Phase 38: "Remember my laptop's name is Orion." / "Remember my blood type is O positive." Whatever the matcher
    guessed (screen.observe, notes.add, the planner), an anchored store verb plus a "my <thing> is <value>" assertion
    is `memory.remember`: nothing smaller than a planner may be trusted to keep a user's fact. The raw sentence is
    passed, not the normalised one, which lower-cases the value and drops characters ("O+" became "o"). The decision
    still runs through `assess`, the executor (L1 permission), the audit trail and the skill's own redaction."""
    skill = REGISTRY.get("memory.remember")
    fact = explicit_store(text)
    m = _ASSERTION.match(fact) if fact is not None else None
    if skill is None or m is None or not _personal_scope(m.group("scope")):
        return None  # "remember my car is blue" keeps the narrower Phase 30.1 rule; only personal facts are made deterministic
    if u.skill and u.skill != skill.name and u.score >= CONFIDENT_SCORE and u.skill != "plan.run" and not u.skill.startswith("memory."):
        return None
    args, missing = extract(skill, text.strip())
    if missing:
        return None
    # The matcher's other guesses are not rivals of an unmistakable store instruction, so none is carried over:
    # left in, notes.add at 0.65 would make this deliberate decision a "coin-flip" for `ambiguous_route`.
    return Understanding(
        action=Action.ACT, utterance=u.utterance, normalized=u.normalized, skill=skill.name, args=args,
        score=u.score, candidates=[Match(skill=skill.name, score=u.score, matched_example="")],
    )


def refine(text: str, u: Understanding) -> Understanding:
    """Phase 30.1: route an explicit store instruction to `memory.remember` (see the module docstring).

    The matcher often scores "remember my X is Y" closest to `memory.recall`, whose body quietly stores it --
    a write recorded as a read. Only a decision that is currently `memory.recall`/`memory.list`, or that already
    has `memory.remember` among its candidates, is changed; the new decision still runs through the executor
    (L1 permission, audit under the real skill name, redaction in the skill) and through `assess` like any
    other direct route."""
    if personal_memory_question(text):
        return _recall_for_personal_memory(text, u)
    held = _hold_statement(text, u)
    if held is not None:
        return held
    stored = _store_assertion(text, u)
    if stored is not None:
        return stored
    forgotten = _forget_memory(text, u)
    if forgotten is not None:
        return forgotten
    if u.action not in (Action.ACT, Action.ASK_SLOT) or not u.skill or u.skill in ("memory.remember", "plan.run"):
        return u
    if u.skill == "knowledge.ask" and explicit_list_documents(text):
        lister = REGISTRY.get("knowledge.list")
        if lister is not None:
            args, missing = extract(lister, u.normalized or text)
            if not missing:
                rest = [c for c in u.candidates if c.skill not in ("knowledge.ask", "knowledge.list")]
                return Understanding(
                    action=Action.ACT, utterance=u.utterance, normalized=u.normalized, skill="knowledge.list", args=args,
                    score=u.score, candidates=[Match(skill="knowledge.list", score=u.score, matched_example="")] + rest,
                )
        return u
    if personal_fact_question(text):
        return _recall_for_personal_fact(text, u)
    if u.skill not in ("memory.recall", "memory.list") and not any(c.skill == "memory.remember" for c in u.candidates):
        return u
    skill = REGISTRY.get("memory.remember")
    if skill is None or explicit_store(text) is None:
        return u
    args, missing = extract(skill, u.normalized or text)
    if missing:
        return u
    # The old pick (recall) is not a rival of the new one, it is the thing being corrected: leaving it in the
    # candidate list would make `ambiguous_route` call this deliberate override a coin-flip with itself.
    rest = [c for c in u.candidates if c.skill not in (skill.name, u.skill)]
    return Understanding(
        action=Action.ACT, utterance=u.utterance, normalized=u.normalized, skill=skill.name, args=args,
        score=u.score, candidates=[Match(skill=skill.name, score=u.score, matched_example="")] + rest,
    )


@dataclass(frozen=True)
class Assessment:
    safe: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def assess(text: str, u: Understanding) -> Assessment:
    """May this direct route be executed as it stands? Only ACT and ASK_SLOT decisions for a real
    skill other than the planner itself are examined; everything else is not a direct execution."""
    if u.action not in (Action.ACT, Action.ASK_SLOT) or not u.skill or u.skill == "plan.run":
        return Assessment(True)
    reasons = [
        r for r in (
            _check_unconsumed_operand(text, u),
            _check_unfillable_slot(text, u),
            _check_whole_sentence_arg(u),
            _check_second_instruction(u),
            _check_compound_request(text, u),
            _check_multiple_facts(text, u),
            _check_action_mismatch(text, u),
            _check_ambiguous_route(u, text),
            _check_forget_intent(text, u),
            _check_forgot_statement(text, u),
        ) if r
    ]
    return Assessment(not reasons, tuple(reasons))
