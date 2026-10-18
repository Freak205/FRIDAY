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


def _check_compound_request(text: str, u: Understanding) -> str | None:
    if parses_own_input(u.skill or "") or u.score >= CONFIDENT_SCORE:
        return None
    acting = [c for c in intent.clause_verbs(mask_operands(text)) if c]
    return "compound_request" if len(acting) >= 2 else None


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
        if skill == "memory.recall" and _is_recall_question(text):
            return None  # "do you remember ..." is a lookup that happens to contain the word remember
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
    if sum(1 for clause in intent.clause_verbs(mask_operands(text)) if clause) >= 2:
        return None
    return fact


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


def refine(text: str, u: Understanding) -> Understanding:
    """Phase 30.1: route an explicit store instruction to `memory.remember` (see the module docstring).

    The matcher often scores "remember my X is Y" closest to `memory.recall`, whose body quietly stores it --
    a write recorded as a read. Only a decision that is currently `memory.recall`/`memory.list`, or that already
    has `memory.remember` among its candidates, is changed; the new decision still runs through the executor
    (L1 permission, audit under the real skill name, redaction in the skill) and through `assess` like any
    other direct route."""
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
            _check_action_mismatch(text, u),
            _check_ambiguous_route(u, text),
        ) if r
    ]
    return Assessment(not reasons, tuple(reasons))
