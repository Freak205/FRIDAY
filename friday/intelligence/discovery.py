"""Phase 17.0 — pure, stateless helpers for open-ended goal understanding.

Deliberately small and dependency-light, matching the role/size of
`friday.intelligence.experience`/`friday.intelligence.context_resolver`: no
class, no state, no orchestration logic of its own. This module only turns
evidence that `friday.orchestrator`/`friday.intelligence.evaluator` already
produce into bounded strings/lists for `friday.skills.plan.run` — it never
decides *whether* to run a tool, never calls the LLM, and never duplicates
`friday.intelligence.goals`'s utterance-shape classification (`GoalKind`).

`classify_mode` answers a different question than `goals.classify`: not
"how is this utterance shaped" but "what kind of epistemic task is this" —
see `friday.intelligence.goals.GoalMode`. Deterministic heuristics only, no
embedding, no model call, matching this codebase's own convention for cheap
routing decisions (see `goals.looks_decomposable`, `goals.looks_multi_step`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from friday.intelligence.goals import GoalMode

if TYPE_CHECKING:
    from friday.orchestrator import Observation

# -- mode classification -----------------------------------------------------

_DIAGNOSTIC_MARKERS = (
    # Phase 17.0 real-model validation (scripts/smoke_open_ended_live.py)
    # found "why might my checks be failing" wasn't recognized — the modal
    # auxiliary list was too narrow. Broadened to cover "might"/"could"/
    # "would" alongside the original negated-verb forms.
    re.compile(
        r"\bwhy\s+(is|isn'?t|are|aren'?t|does|doesn'?t|do|don'?t|won'?t|"
        r"can'?t|couldn'?t|wouldn'?t|hasn'?t|might|could|would)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bwhat'?s wrong with\b", re.IGNORECASE),
    re.compile(r"\bwhat is wrong with\b", re.IGNORECASE),
    re.compile(r"\bwhat'?s broken\b", re.IGNORECASE),
    re.compile(r"\bdiagnose\b", re.IGNORECASE),
)

_INVESTIGATIVE_MARKERS = (
    re.compile(r"\bfigure out what\b", re.IGNORECASE),
    re.compile(r"\bfind out what\b", re.IGNORECASE),
    re.compile(r"\bwhat needs (my )?attention\b", re.IGNORECASE),
    re.compile(r"\bwhat should i (work on|do) next\b", re.IGNORECASE),
    re.compile(r"\bwhat'?s (left|outstanding|todo)\b", re.IGNORECASE),
    re.compile(r"\btell me what needs attention\b", re.IGNORECASE),
)

_INFO_SEEKING_MARKERS = (
    re.compile(r"\bwhat'?s (on|happening on) my screen\b", re.IGNORECASE),
    re.compile(r"\bwhat is (on|happening on) my screen\b", re.IGNORECASE),
    re.compile(r"\bwhat does it say\b", re.IGNORECASE),
    re.compile(r"\bread (this|that|it) back\b", re.IGNORECASE),
    re.compile(r"\bwhat'?s happening on my (desktop|screen)\b", re.IGNORECASE),
)

_OPEN_ENDED_VERBS = ("make", "improve", "fix", "prepare", "handle")
# Phrasal verbs whose object can land in between ("get my project ready",
# "clean this up", "sort this out", "work on this") — a plain substring
# check would miss these, so each gets its own loose "verb ... particle"
# regex instead.
_OPEN_ENDED_VERB_PATTERNS = (
    re.compile(r"\bclean\b.*\bup\b", re.IGNORECASE),
    re.compile(r"\bsort\b.*\bout\b", re.IGNORECASE),
    re.compile(r"\bget\b.*\bready\b", re.IGNORECASE),
    re.compile(r"\bwork\b.*\bon\b", re.IGNORECASE),
)
_OPEN_ENDED_TARGETS = ("this", "it", "things", "everything")
# Phase 17.0 real-model validation found "make my FRIDAY project better"
# wasn't recognized as a vague target — a plain "my project" substring
# missed the common case of a named project in between ("my FRIDAY
# project", "my college project"). A short bounded gap keeps this from
# matching an unrelated later use of "project" in a long sentence.
_OPEN_ENDED_PROJECT_TARGET = re.compile(r"\bmy\b.{0,25}\bproject\b", re.IGNORECASE)


def classify_mode(text: str) -> GoalMode:
    """Deterministic epistemic-task classification. Order matters: a
    diagnostic "why" question is checked before the broader open-ended
    catch-all, same precedence style as `goals.classify`."""
    low = f" {(text or '').lower().strip()} "

    if any(p.search(low) for p in _DIAGNOSTIC_MARKERS):
        return GoalMode.DIAGNOSTIC
    if any(p.search(low) for p in _INVESTIGATIVE_MARKERS):
        return GoalMode.INVESTIGATIVE
    if any(p.search(low) for p in _INFO_SEEKING_MARKERS):
        return GoalMode.INFORMATION_SEEKING

    has_vague_verb = any(v in low for v in _OPEN_ENDED_VERBS) or any(
        p.search(low) for p in _OPEN_ENDED_VERB_PATTERNS
    )
    has_vague_target = (
        any(f" {t} " in low or low.strip().endswith(t) for t in _OPEN_ENDED_TARGETS)
        or bool(_OPEN_ENDED_PROJECT_TARGET.search(low))
    )
    if has_vague_verb and has_vague_target and len((text or "").split()) >= 3:
        return GoalMode.OPEN_ENDED

    return GoalMode.DIRECT_ACTION


# -- evidence block for the planner context ----------------------------------


def build_evidence_block(observations: "list[Observation]", context: str = "", *, max_chars: int = 800) -> str:
    """Fold a discovery pass's observations into the same bounded ambient
    `context` string `plan.run` already threads through every prompt (see
    `_append_working_memory`/`_append_experience` in `friday.skills.plan`) —
    never a second, parallel context channel."""
    from friday.intelligence import evaluator

    if not observations:
        return context

    lines = []
    for o in observations:
        speech = (o.speech or "").strip()
        if not speech:
            continue
        label = evaluator.classify_evidence(o)
        lines.append(f"- [{label}] {o.step.tool}: {speech[:160]}")
    if not lines:
        return context

    block = ("Discovery evidence so far:\n" + "\n".join(lines))[:max_chars]
    return f"{context}\n\n{block}".strip() if context else block


# -- unknowns extraction ------------------------------------------------------

_HEDGE_MARKERS = (
    "unclear", "not sure", "couldn't determine", "cannot determine",
    "could not determine", "unknown", "uncertain", "not certain",
    "couldn't establish", "could not establish", "couldn't verify", "could not verify",
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def extract_unknowns(summary: str, observations: "list[Observation]") -> list[str]:
    """Bounded, evidence-derived list of what discovery still couldn't
    resolve — never invented. Two sources, both grounded in real evidence:
    (1) any observation the evaluator itself flagged UNCERTAIN or FAILURE
    (the tool's own admission it couldn't fully confirm something), and
    (2) sentences in the planner's own summary that hedge (see
    `_HEDGE_MARKERS`) — i.e. the model's own stated uncertainty, not a
    conclusion this function draws on its own."""
    from friday.intelligence import evaluator

    unknowns: list[str] = []
    for o in observations:
        label = evaluator.classify_evidence(o)
        if label in ("unconfirmed", "failed_check"):
            detail = (o.speech or o.error or "no detail").strip()
            unknowns.append(f"{o.step.tool}: {detail}"[:200])

    for sentence in _SENTENCE_SPLIT.split(summary or ""):
        low = sentence.lower()
        if any(marker in low for marker in _HEDGE_MARKERS):
            cleaned = sentence.strip()
            if cleaned:
                unknowns.append(cleaned[:200])

    seen: set[str] = set()
    deduped: list[str] = []
    for u in unknowns:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


# -- anti-overclaiming guard ---------------------------------------------------

_CAUSE_CLAIM = re.compile(r"\bis the cause\b", re.IGNORECASE)
_FIX_CLAIM = re.compile(
    r"\b(i(?:'ve| have)? fixed|that(?:'s| is) fixed|fixed the issue|fixed it)\b", re.IGNORECASE
)


def guard_against_overclaiming(summary: str, observations: "list[Observation]") -> str:
    """Phase 17.0 anti-hallucination rule, deterministic: never let a
    hypothesis read as a confirmed fact. Rewrites confident cause/fix
    language that the observation trace doesn't actually back up — never
    fabricates or removes real evidence, only softens unsupported
    confidence in the wording."""
    if not summary:
        return summary

    from friday.registry import REGISTRY

    def _tier(tool: str) -> str:
        skill = REGISTRY.get(tool)
        return skill.tier if skill else ""

    verified_mutation = any(o.ok and _tier(o.step.tool) != "L0" for o in observations)

    text = summary
    hedged = False
    if _CAUSE_CLAIM.search(text):
        hedged = True
        text = _CAUSE_CLAIM.sub("may be the cause", text)
    if not verified_mutation and _FIX_CLAIM.search(text):
        hedged = True
        text = _FIX_CLAIM.sub("attempted a fix for", text)

    if hedged and not text.lower().startswith(("i couldn't", "i could not", "i attempted")):
        text = f"I couldn't fully verify everything, but: {text}"
    return text


# -- Phase 18.0: evidence-sufficiency gate -----------------------------------
#
# "Do not investigate because you can. Investigate only when the current
# evidence is insufficient for the user's actual goal." The discovery loop
# (friday.orchestrator.Orchestrator.run_goal, discovery_mode=True) previously
# had exactly one stop signal: the model itself choosing {"action": "done"}
# each turn — with no deterministic check for "does the evidence already
# answer this?" A small local model that keeps calling tools instead of
# recognizing it already has the answer burns its whole step budget or trips
# the repeated-call guard before stopping (see PLAN.md's Phase 17.0 "Known
# limitations"). `assess_sufficiency` is a small, pure, additive check
# consulted before each further discovery step — never a second planner,
# never a model call, never a replacement for the repeat-guard or the
# max_discovery_steps/max_discovery_time_s ceilings, which remain the hard
# backstops. It only ever looks at real `Observation`s from this run — never
# `context`/working-memory/experience text — so ambient context or a
# retrieved past episode can never masquerade as current evidence.


class Sufficiency(str, Enum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    CONTRADICTORY = "contradictory"
    UNCERTAIN = "uncertain"


_OFF_TOPIC_MARKERS = re.compile(
    r"\b(wallpaper|unrelated (chrome|browser) window|unrelated readme formatting|"
    r"off[- ]topic|not relevant to (this|the) (goal|task))\b",
    re.IGNORECASE,
)

_STRONG_SIGNAL_MARKERS = re.compile(
    r"(\w*Error\b|\bException\b|\btraceback\b|\bnot found\b|\bno such file\b|"
    r"\bpermission denied\b|\bcannot import\b)",
    re.IGNORECASE,
)

_POSITIVE_STATE_MARKERS = re.compile(
    r"\b(started successfully|is running|has succeeded|succeeded|completed successfully|"
    r"working correctly|now available|is connected)\b",
    re.IGNORECASE,
)
_NEGATIVE_STATE_MARKERS = re.compile(
    r"\b(connection refused|not running|has failed|crashed|not found|was refused|"
    r"is unreachable|timed out|no longer (working|available))\b",
    re.IGNORECASE,
)

_CAUSAL_MARKERS = re.compile(
    r"\b(because|caused by|is the cause|may be the cause|due to|is missing|"
    r"is misconfigured|is not installed|isn'?t installed|failed to start|"
    r"refused to start|is disabled|is blocked by)\b",
    re.IGNORECASE,
)

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of", "in", "on",
    "for", "and", "or", "my", "this", "that", "it", "what", "why", "how", "do", "does",
    "did", "i", "you", "your", "me", "with", "at", "from", "about", "into", "find",
    "tell", "show", "check", "look", "let", "should", "would", "could", "can", "not",
    # Discovery-pass meta-vocabulary (friday.skills.plan._run_discovery wraps
    # every goal in a fixed "Investigate before acting: {goal}. Gather
    # evidence only..." instructional prompt before handing it to run_goal —
    # see that function). These words describe the *act* of investigating,
    # not the *subject*, and appear in effectively every discovery prompt
    # regardless of topic — without excluding them, a tool name that happens
    # to contain one (e.g. a skill literally named "...investigate...")
    # spuriously "overlaps" with any goal at all. Filtered the same way
    # grammatical stopwords are, not treated as topical signal either side.
    "investigate", "investigating", "gather", "gathering", "evidence",
    "summary", "summarize", "unknown", "unknowns", "found", "attempt",
    "attempting", "acting", "respond", "remains", "proceed", "determine",
    "establish", "confirm", "confirmed", "once", "enough", "yet", "anything",
})


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9']+", (text or "").lower())
        if len(w) >= 3 and w not in _STOPWORDS
    }


def _call_key(o: "Observation") -> str:
    """Same construction as `Orchestrator.run_goal`'s own repeat-guard
    `call_key` (orchestrator.py) — reused here, not redefined, so "current
    state" for the sufficiency gate lines up exactly with what the guard
    already considers "the same call"."""
    return f"{o.step.tool}:{json.dumps(o.step.args, sort_keys=True, default=str)}"


def _current_evidence(observations: "list[Observation]") -> "list[Observation]":
    """Collapse a run's raw observation trace to "current state": the latest
    observation per `tool:args` key (a later call to the same tool+args
    supersedes an earlier one — Phase 18.0 §8 staleness; two calls to the
    same tool with *different* args, e.g. reading two different files, stay
    independent), then drop duplicate-content entries (§14 — "read README
    twice" isn't two independent confirmations). The repeat-guard's own
    synthetic "Blocked: ... not repeating it" warnings are never evidence."""
    from friday.orchestrator import NOT_EXECUTED_ERRORS

    latest: dict[str, Observation] = {}
    for o in observations:
        if o.error in NOT_EXECUTED_ERRORS:
            continue
        latest[_call_key(o)] = o

    seen_text: set[str] = set()
    out: list[Observation] = []
    for o in latest.values():
        text = (o.speech or "").strip().lower()
        if text and text in seen_text:
            continue
        if text:
            seen_text.add(text)
        out.append(o)
    return out


def is_relevant(goal: str, observation: "Observation") -> bool:
    """§3 evidence relevance. Permissive by default — an observation the
    planner chose to gather in service of the stated goal is presumed
    on-topic — excluding only explicit off-topic phrasing or empty content.
    A concrete error/exception is always relevant to a diagnostic/
    investigative goal regardless of literal wording overlap (a
    `ModuleNotFoundError` answers "why isn't it starting" even though the
    words don't overlap); otherwise some token overlap between the goal and
    the observation's speech/tool/args is required, so an unrelated but
    successful call (e.g. listing a directory) doesn't count as evidence for
    a question it doesn't actually address."""
    text = f"{observation.speech} {observation.error}".strip()
    if not text:
        return False
    if _OFF_TOPIC_MARKERS.search(text):
        return False
    if _STRONG_SIGNAL_MARKERS.search(text):
        return True

    goal_tokens = _tokens(goal)
    if not goal_tokens:
        return True
    arg_text = " ".join(str(v) for v in observation.step.args.values())
    obs_tokens = _tokens(text) | _tokens(observation.step.tool) | _tokens(arg_text)
    return bool(goal_tokens & obs_tokens)


def has_contradiction(observations: "list[Observation]") -> bool:
    """§7 contradictory evidence — deliberately narrow, not a semantic
    database: positive state language (e.g. "started successfully") and
    negative state language (e.g. "connection refused") both appearing among
    current, relevant evidence means don't confidently conclude either way."""
    blob = " | ".join(f"{o.speech} {o.error}" for o in observations)
    return bool(_POSITIVE_STATE_MARKERS.search(blob) and _NEGATIVE_STATE_MARKERS.search(blob))


def _is_conclusive(goal_mode: GoalMode, observation: "Observation") -> bool:
    """§5 diagnostic sufficiency: a directly observed failure (a concrete
    error/exception signal, or wording that names a cause) is conclusive
    evidence even though the call itself failed — `ModuleNotFoundError:
    flask` answers "why isn't it starting." A bare "exited with code 1" is
    not conclusive on its own; it names no cause.

    A DIAGNOSTIC goal specifically needs more than "a relevant tool call
    succeeded" — a successful-but-merely-contextual read (e.g. "this appears
    to be a Flask app") doesn't itself explain *why* something is failing,
    so for that mode only a strong error/exception signal or explicit causal
    wording counts. Every other mode (investigative/information-seeking/
    open-ended — answering "what does X say," "what needs attention," "what's
    on screen") is satisfied by any successful, substantive, relevant read,
    matching brief §11's "the last observation answers the goal" cases.

    Phase 24.6: that "any successful, substantive read" bar excludes a line that only
    reports work UNDER WAY ("Attempting to delete report.csv.", "Searching for
    report.csv...", `_is_progress_line`) — it says something was started, not what came of
    it. This is the one place every stop/coverage decision reads conclusiveness from
    (`assess_sufficiency` -> the discovery sufficiency stop and the look-only coverage stop,
    per-clause coverage -> the premature-`done` nudge and the "no result yet" hint), so an
    attempt can no longer satisfy a goal or a clause on its own; the run simply asks the
    planner for the actual result. A concrete error signal (above) and a diagnostic
    goal's causal wording are still conclusive, and a failure is unaffected: it is
    `ok=False`, so its own conclusiveness never depended on this line."""
    text = f"{observation.speech} {observation.error}"
    if _STRONG_SIGNAL_MARKERS.search(text):
        return True
    if goal_mode is GoalMode.DIAGNOSTIC:
        return bool(_CAUSAL_MARKERS.search(text))
    return observation.ok and bool((observation.speech or "").strip()) and not _is_progress_line(observation)


def assess_sufficiency(goal: str, observations: "list[Observation]") -> Sufficiency:
    """The Phase 18.0 gate: does the evidence gathered so far already answer
    the goal? Consulted before each further discovery step (see
    `Orchestrator.run_goal`) — never during main (mutating) execution.

    SUFFICIENT   -> stop; the caller can skip the next LLM call entirely.
    CONTRADICTORY -> evidence conflicts; never confidently conclude either way.
    UNCERTAIN    -> relevant evidence exists but doesn't conclusively answer it.
    INSUFFICIENT -> no relevant evidence yet (e.g. nothing observed at all).

    Contradiction is checked over the full current-evidence pool, before the
    relevance filter — two observations reporting conflicting state (e.g.
    "server started successfully" vs. "connection refused") are exactly the
    kind of evidence whose own wording rarely echoes the goal's phrasing, so
    requiring goal-token overlap first would (and, empirically, did) let a
    real contradiction slip past unnoticed. The paired positive/negative
    marker regexes are narrow enough that this doesn't fire on unrelated
    evidence in practice.

    `goal` here is whatever string the caller is using as the planning
    prompt — for FRIDAY's real discovery pass that's `_run_discovery`'s
    wrapped "Investigate before acting: {goal}..." instruction, not the bare
    user utterance; `classify_mode`'s regexes are search-based so they still
    match correctly through that wrapper.
    """
    current = _current_evidence(observations)
    if has_contradiction(current):
        return Sufficiency.CONTRADICTORY
    relevant = [o for o in current if is_relevant(goal, o)]
    if not relevant:
        return Sufficiency.INSUFFICIENT
    mode = classify_mode(goal)
    # Phase 24.8: a result the TOOL itself flagged unconfirmed (`data["uncertain"]`) cannot answer the goal -- per-clause
    # coverage and the look-only stop already refuse it; without this the discovery stop reported it as the finished answer.
    if any(_is_conclusive(mode, o) and not (o.data or {}).get("uncertain") for o in relevant):
        return Sufficiency.SUFFICIENT
    return Sufficiency.UNCERTAIN


# -- Phase 21.0: goal coverage ------------------------------------------------
#
# `assess_sufficiency` judges RELEVANCE: "is there a real observation about this
# goal?". It cannot tell "the goal has two parts and one is answered": for
# "check the time and battery level" the `system.time` result is relevant and
# conclusive, so the gate would happily declare the whole goal done (Phase 20.0
# report §12.1 flagged exactly this before letting the gate into the main loop).
# Coverage adds the missing question — for EACH conjunct the goal itself names,
# is there evidence? — without a second evaluator: it reuses `_current_evidence`,
# `_is_conclusive`, `classify_mode` and the same off-topic exclusion, and a goal
# with a single clause is delegated to `assess_sufficiency` unchanged.
#
# Bounded and honest by construction: at most `MAX_CLAUSES` requirements, each a
# fragment of the user's OWN text (never an invented success condition); status
# comes only from real `Observation`s of this run (never context/experience);
# clause keywords are matched, so an unknown phrasing errs toward UNSATISFIED —
# the failure direction is "asks the planner once more / falls back to the
# planner's own `done`", never a false "all covered".

MAX_CLAUSES = 5

_CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:[,;]\s*(?:and\s+|then\s+)?|\band then\b|\band also\b|\bafter that\b|\bthen\b|\band\b|\balso\b)\s*",
    re.IGNORECASE,
)
_EDGE_PUNCT = " .!?,;:\"'"
# A fragment that opens with a negation ("...just say done, no decomposition needed") is a
# constraint on the request, not a second thing to accomplish.
_NEGATED_FRAGMENT = re.compile(r"^(?:no|not|never|without|nothing|don'?t|do not|dont)\b", re.IGNORECASE)


class RequirementStatus(str, Enum):
    SATISFIED = "satisfied"      # a real, successful, relevant observation answers it
    UNSATISFIED = "unsatisfied"  # nothing relevant observed yet
    FAILED = "failed"            # every relevant observation failed
    UNKNOWN = "unknown"          # relevant evidence exists but it doesn't conclusively answer it


@dataclass(frozen=True)
class Requirement:
    text: str
    status: RequirementStatus


@dataclass(frozen=True)
class CoverageResult:
    requirements: tuple[Requirement, ...]

    @property
    def multi_clause(self) -> bool:
        return len(self.requirements) > 1

    @property
    def all_satisfied(self) -> bool:
        return bool(self.requirements) and all(r.status is RequirementStatus.SATISFIED for r in self.requirements)

    @property
    def any_failed(self) -> bool:
        return any(r.status is RequirementStatus.FAILED for r in self.requirements)

    def unmet(self) -> list[str]:
        return [r.text for r in self.requirements if r.status is not RequirementStatus.SATISFIED]

    def to_dict(self) -> dict:
        return {
            "all_satisfied": self.all_satisfied,
            "requirements": [{"text": r.text, "status": r.status.value} for r in self.requirements],
        }


def _stem(word: str) -> str:
    """Just enough plural folding for keyword matching ("files" ~ "file",
    "batteries" ~ "battery"); applied to both sides, so it only has to be consistent."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _clause_keywords(text: str) -> set[str]:
    """The content words of one clause: its verbs (the shared intent lexicon), filler
    and grammatical words removed, lightly stemmed — what the clause is ABOUT."""
    from friday import intent

    return {
        _stem(w) for w in re.findall(r"[a-z0-9']+", (text or "").lower())
        if len(w) >= 3 and w not in _STOPWORDS and w not in intent._STOP
    }


def derive_clauses(goal: str, *, max_clauses: int = MAX_CLAUSES) -> list[str]:
    """Split a goal into the independent requirements its own text names
    ("check the time and battery level" -> ["check the time", "battery level"]).

    Deterministic and literal: only the user's words, split on connectors; a clause
    with no content word left after stripping verbs/filler ("and then finish it")
    carries no requirement and is dropped; a goal with no connector — or one that
    reduces to a single requirement — comes back as ONE clause (itself), so
    single-clause goals are never affected. Capped at `max_clauses`."""
    text = (goal or "").strip()
    if not text:
        return []
    parts = [p.strip(_EDGE_PUNCT) for p in _CLAUSE_SPLIT_RE.split(text)]
    clauses = [p for p in parts if p and not _NEGATED_FRAGMENT.match(p) and _clause_keywords(p)]
    if len(clauses) <= 1:
        return [text]
    return clauses[:max_clauses]


def _data_text(o: "Observation") -> str:
    """Phase 22.0: the same bounded, sanitized excerpt of a step's real tool data that the
    PLANNER is shown (`friday.toolview.excerpt`) — so coverage is judged from exactly the
    evidence the planner had, no more (never data it could not see) and no less."""
    from friday import toolview
    from friday.config import CFG

    if not CFG.planner.tool_data_excerpts or not o.ok or not o.data:
        return ""
    return toolview.excerpt(o.step.tool, o.data, speech=o.speech, max_chars=CFG.planner.tool_data_step_chars)


def _clause_relevant(clause_keywords: set[str], o: "Observation", with_data: bool = False) -> bool:
    """Is this observation ABOUT the clause? Keyword overlap between the clause and the
    observation's speech/tool/args only (plus, with `with_data`, the tool-data excerpt the
    planner saw). Deliberately stricter than `is_relevant`: that one treats any error
    text as relevant to any goal, which for per-clause coverage would let one unrelated
    error "cover" every clause."""
    text = f"{o.speech} {o.error}".strip()
    if not text or _OFF_TOPIC_MARKERS.search(text):
        return False
    arg_text = " ".join(str(v) for v in o.step.args.values())
    data_text = _data_text(o) if with_data else ""
    obs = {_stem(w) for w in re.findall(r"[a-z0-9']+", f"{text} {o.step.tool} {arg_text} {data_text}".lower()) if len(w) >= 3}
    return bool(clause_keywords & obs)


# -- Phase 23.0: compound goal modeling (acquisition vs. answer) -------------
#
# Phase 22's report (§10, group A): "Read X and tell me Y" decomposes (Phase 11.2)
# into subgoals like "open the file" / "read the content" / "confirm it", and the 3B
# model fixates on the acquisition-shaped subgoal 0 and re-reads instead of answering
# from what it already has — even though the goal-coverage/tool-data machinery already
# lets the PLANNER itself answer correctly when there is no subgoal scaffold in the way
# (see scripts/smoke_tool_data.py section F1: 0 subgoals, 2 model calls, correct
# answer). The fix therefore targets the SUBGOAL mechanism specifically — never the
# existing coverage/evidence-stop machinery above, which is intentionally left
# byte-for-byte unchanged (its speech-only evidence stop, and its "the data view never
# ends a run" invariant, are both still exactly what scripts/smoke_tool_data.py pins).
#
# `classify_requirement_kind` is the one new deterministic (never model-called)
# classifier: does a requirement's own words ask to ACQUIRE new evidence, or to
# ANSWER/explain something from evidence already gathered? `Orchestrator.decompose_goal`
# uses it to tag every subgoal it proposes (see friday.intelligence.goals.SubgoalKind);
# `Orchestrator.run_goal` uses `subgoal_step_satisfied` to decide, from real tagged
# evidence only, when a subgoal's own work is genuinely done — never merely because a
# tool ran.

_ANSWER_VERB_RE = re.compile(
    r"\b(tell|explain|describe|summar(?:ize|ise)|answer|report|say|identify|clarify|confirm)\b",
    re.IGNORECASE,
)
_ANSWER_QWORD_RE = re.compile(r"\b(what|why|how|which|who|whom|whose|whether)\b", re.IGNORECASE)


def classify_requirement_kind(text: str) -> str:
    """Phase 23.0 — ACQUISITION or ANSWER (`friday.intelligence.goals.SubgoalKind`
    values), from `text`'s own words alone: deterministic, no model call. Biased toward
    ACQUISITION (the universal pre-Phase-23 behavior: a requirement needs its own
    evidence) — only text that plainly asks for an explanation, a summary or names a
    question word is ANSWER. Matches the brief's own examples: "obtain README evidence"
    -> acquisition, "answer technologies from README evidence" -> answer (contains
    "answer"); "tell me what technologies I used" -> answer (contains "tell"/"what")."""
    t = text or ""
    if _ANSWER_VERB_RE.search(t) or _ANSWER_QWORD_RE.search(t):
        return "answer"
    return "acquisition"


def subgoal_step_satisfied(goal: str, observations: "list[Observation]") -> bool:
    """Phase 23.0 — did `observations` (already filtered by the caller to whatever was
    attributed to ONE subgoal — see `Orchestrator._subgoal_evidence`, which uses
    `PlanStep.subgoal`) produce real, successful, substantive evidence? Reuses the same
    conclusiveness bar as goal coverage/sufficiency (`_is_conclusive`) so a subgoal is
    never credited merely because a tool ran: a failed call, or one with no real
    speech/substance, never counts — evidence, not inference.

    Phase 24.5: a line that only reports work IN PROGRESS ("Attempting to delete
    report.csv.", "Still working on it...") is not a result either (`_is_progress_line`).
    Found by the end-to-end suite: without this an attempt line closed the acquisition
    subgoal, so the answer was composed one step BEFORE the tool's final result and could
    only ever say the evidence was insufficient."""
    if not observations:
        return False
    mode = classify_mode(goal)
    return any(
        o.ok and not (o.data or {}).get("uncertain") and _is_conclusive(mode, o) and not _is_progress_line(o)
        for o in observations
    )


def assess_coverage(goal: str, observations: "list[Observation]") -> CoverageResult:
    """Per-requirement evidence status for `goal`, from this run's real observations —
    judged from each step's speech / tool / args (the Phase 21 rule, unchanged).
    See `_assess_coverage` for the rules and `assess_coverage_seen` for the data-aware view."""
    return _assess_coverage(goal, observations, with_data=False)


def assess_coverage_seen(goal: str, observations: "list[Observation]") -> CoverageResult:
    """Phase 22.0: coverage judged from everything the PLANNER was shown — speech AND the
    tool-data excerpt (`friday.toolview`). For deciding what to TELL the planner (the
    "these parts have no result" hint, the premature-`done` nudge); never for ending a run."""
    return _assess_coverage(goal, observations, with_data=True)


def _assess_coverage(goal: str, observations: "list[Observation]", *, with_data: bool) -> CoverageResult:
    """Per-requirement evidence status for `goal`, from this run's real observations.

    A single-clause goal is delegated to `assess_sufficiency` (one requirement whose
    status maps 1:1 from its verdict), so nothing changes for it. For a
    multi-clause goal each clause is SATISFIED only by a conclusive, successful
    observation that is about that clause (`_is_conclusive`, same bar as Phase 18),
    FAILED when every relevant RESULT failed (a progress line is not a result — Phase
    24.6), UNKNOWN when relevant evidence
    exists but isn't conclusive, UNSATISFIED when nothing relevant was observed.

    `with_data` (Phase 22.0; False = the Phase 21 behaviour) also lets the words of the
    tool-data excerpt the planner was shown count as "about the clause". It is for deciding
    what to TELL the planner (the "these parts have no result" hint, the premature-`done`
    nudge): a part whose answer is in the data the planner can see is not missing. It is
    deliberately NOT used for the evidence STOP, which ends a run on its speech alone — a
    fact that lives only in `data` still needs the planner to say it."""
    clauses = derive_clauses(goal)
    if len(clauses) <= 1:
        verdict = assess_sufficiency(goal, observations)
        status = {
            Sufficiency.SUFFICIENT: RequirementStatus.SATISFIED,
            Sufficiency.INSUFFICIENT: RequirementStatus.UNSATISFIED,
            Sufficiency.UNCERTAIN: RequirementStatus.UNKNOWN,
            Sufficiency.CONTRADICTORY: RequirementStatus.UNKNOWN,
        }[verdict]
        return CoverageResult((Requirement(clauses[0] if clauses else (goal or ""), status),))

    mode = classify_mode(goal)
    current = _current_evidence(observations)
    reqs: list[Requirement] = []
    for clause in clauses:
        keywords = _clause_keywords(clause)
        relevant = [o for o in current if _clause_relevant(keywords, o, with_data)]
        if not relevant:
            status = RequirementStatus.UNSATISFIED
        elif any(o.ok and not (o.data or {}).get("uncertain") and _is_conclusive(mode, o) for o in relevant):
            # A result the tool itself flagged unconfirmed (the evaluator's UNCERTAIN)
            # is evidence that something happened, not that the clause is answered.
            status = RequirementStatus.SATISFIED
        elif (results := [o for o in relevant if not _is_progress_line(o)]) and all(not o.ok for o in results):
            # Phase 24.6: a progress line ("Counting the rows...") is not a result, so it
            # neither satisfies the clause (`_is_conclusive`) nor keeps it from FAILED when
            # every real result did fail — "progress, then the failure" is a failure, not a
            # clause still waiting on a result that will never come.
            status = RequirementStatus.FAILED
        else:
            status = RequirementStatus.UNKNOWN
        reqs.append(Requirement(clause, status))
    return CoverageResult(tuple(reqs))


# -- Phase 24 reliability contract (stated at Phase 24.10; every line is pinned by a smoke suite) --------
#
# What the final-answer pipeline `finalize_answer` = `ground_answer` -> `normalize_answer` GUARANTEES, on
# every route that reaches the user, while `CFG.planner.answer_grounding_guard` is on. It is a set of
# deterministic string checks (regex + stems, no model call, no retry), so each guarantee is scoped to what
# those checks can see -- the "not guaranteed" list below is the other half of the contract.
#
# GUARANTEED
#   * A concrete 3+ digit number in the answer (whole value; thousands separators and glued units such as
#     "500GB" normalized) is stated by real evidence (an ok, non-uncertain observation) or by the goal.
#   * A filename in the answer (of the recognized extension vocabulary) is stated, as a whole name, by real
#     evidence or by the goal.
#   * A completed-action claim ("deleted", "created", "sent" ...) needs a real, non-attempt, non-negated,
#     non-hedged outcome of that action family; a failed action's speech never grounds a success.
#   * When a claim names several files, EACH explicitly extracted file needs its own real outcome for that
#     action (24.9); where object boundaries are ambiguous the claim is allowed, not guessed.
#   * Progress / attempt / uncertain evidence ("Attempting to ...", "Working on it...", a tool-flagged
#     `uncertain` result) never establishes completion, and never satisfies a stop rule (`_is_conclusive`).
#   * A goal-relevant real result the answer leaves out (a failure, a requested number, a second clause) is
#     restated from the evidence -- "Also: <real speech>" -- never fabricated; restatement is looked at again
#     until nothing new is missing (bounded), so grounding is IDEMPOTENT.
#   * A success-vs-failure conflict between different tools about the same file is preserved (both sides are
#     stated), never silently resolved in favour of one.
#   * One unsupported claim fails the WHOLE answer closed, with the real evidence restated.
#   * Normalization is removal only (whole sentences, verified as a character subsequence at run time,
#     otherwise the input is returned), idempotent, and never removes something completeness/conflict
#     checks or the goal's own filenames/numbers depend on. It cannot add a word, so it cannot add a fact.
#   * Order is fixed in ONE place: compose -> `guard_against_overclaiming` (Phase 17, composer route) ->
#     `ground_answer` -> `normalize_answer` -> the answer. Nothing after normalization touches the text.
#   * Both model-authored routes are guarded: the Phase 23 composer (`_answer_from_evidence`) and the
#     planner's `done` summary (`_ground_done_summary`; a run with zero real observations is left alone).
#     The deterministic summary routes only ever carry the tools' own speech (a failed / partial stop is
#     returned exactly as the tools said it; a completed run's is normalized).
#   * The kill switch removes ALL of it: with `answer_grounding_guard` off no route calls `ground_answer` or
#     `normalize_answer` and the text is returned as before Phase 24 (Phase 17's separate softening remains).
#
# NOT GUARANTEED (documented boundaries, deliberately not widened)
#   * 1-2 digit invented values are never checked; a number in a call's ARGS counts as evidence.
#   * The filename extension vocabulary is closed ("setup.exe", "config.toml" are unchecked); "Node.js" counts
#     as a filename.
#   * Per-object grounding covers FILENAMES only: contacts, app names, quoted job names are not objects.
#     Elliptical / list / markdown / multi-line key-value shapes are allowed unchecked.
#   * Verbs, stems and synonyms are matched as prefixes/substrings: a synonym outside the vocabulary is not
#     recognized, a stem can over-match ("closest"), and "in flight" wording outside the attempt vocabulary
#     ("queued", "started") is not classified as progress.
#   * Only the progress and conflict phrasings the regexes know are detected; a conflict needs a shared filename.
#   * Semantic correctness is not judged: an answer built only from real words can still be a poor summary.
#   * Live / external-system behaviour: a tool that reports success for something that did not happen is
#     believed (post-condition checks live in friday/verify.py, not here).
#   * An honest answer naming a file/number only a FAILED observation carries fails closed.
# The full per-phase list of known limitations is in the project memory (friday-phase24-answer-grounding).
#
# -- Phase 24.1: deterministic grounding guard for the composed answer -------
#
# `Orchestrator._answer_from_evidence` (Phase 23.0) already shows the composer ONLY the
# real, bounded evidence and makes it structurally incapable of calling a tool — but
# nothing previously checked its OWN reply for claims that evidence never actually
# produced. `guard_against_overclaiming` (Phase 17.0, above) softens confident
# cause/fix WORDING; this is a second, independent, deterministic check on CONCRETE
# claims — a specific number, a filename, a completed-action verb — never another
# model call, never a retry loop. Two fail-closed triggers:
#   1. no real (ok, non-uncertain) evidence at all -> whatever the composer said,
#      there is nothing to ground it in.
#   2. the answer names a number, filename or completed-action verb that the evidence
#      itself never produced (checked against every real observation's speech, tool
#      name and args/data — not the composer's own wording).
# Either one replaces the answer with a plain, evidence-only statement that says the
# evidence is insufficient, rather than letting the unsupported claim through — never
# by fabricating a correction, only by falling back to what the evidence itself says.
#
# -- Phase 24.2: broaden which claims trigger #2 -----------------------------
#
# Phase 24.1's claim list was narrow: a fixed set of completed-action verbs plus
# numbers/filenames. Anything else the composer asserted — "I found the invoice",
# "I changed your settings", "I retrieved the report" — passed through unchecked
# purely because it didn't happen to match one of those patterns, not because it was
# actually grounded. Phase 24.2 stays inside the same architecture (still pure regex/
# string matching, still no model call, still one fail-closed replacement of the
# WHOLE answer): it (a) adds retrieval verbs (found/located/discovered/identified/
# retrieved/fetched/searched) and the previously-missing changed/modified to
# `_ACTION_CLAIM_STEMS`, (b) tightens what counts as "supporting evidence" for an
# action claim to the observation's own reported OUTCOME (speech + data) rather than
# also accepting a bare tool-name substring match (`_observation_outcome_text` —
# closes "the tool ran" != "the tool confirmed this specific outcome"), and (c) adds
# two narrow exclusions — a negated verb ("never deleted") and a verb followed by an
# evaluative/filler continuation ("found this interesting") — so common words don't
# turn an ordinary conversational sentence into a false "unsupported claim". Every
# other Phase 24.1 mechanism (filename/number checks, the "no real evidence at all"
# trigger, the fail-closed whole-answer replacement) is unchanged.

_FILENAME_CLAIM_RE = re.compile(
    r"\b[\w][\w\-]*\.(?:txt|md|csv|log|json|ya?ml|ini|cfg|conf|py|js|jsx|ts|tsx|"
    r"html?|xml|sh|bat|zip|db|docx?|xlsx?|pptx?|pdf|png|jpe?g|gif|bmp|mp3|wav|mp4)\b",
    re.IGNORECASE,
)
# Single/double-digit numbers are too common in ordinary evidence text (and any short
# haystack) to check reliably with a plain substring match — restricted to 3+ digits
# (or a decimal) so this only ever flags a claim specific enough to be worth checking.
_NUMBER_CLAIM_RE = re.compile(r"\b\d{3,}(?:\.\d+)?(?!\d)|\b\d+\.\d+(?!\d)")  # 24.8: a glued unit ("500GB") is still a number

# A small, transparent word list, same philosophy as `classify_requirement_kind` above
# and `derive_scope`/`classify_mode` elsewhere in this codebase: each completed-action
# verb the composer might claim maps to the substrings a REAL tool name or speech for
# that action would plausibly contain. Fails closed the other way too — a verb not in
# this list is never checked (never a false claim of fabrication), matching this
# guard's "never block legitimate evidence-backed wording" bias.
_ACTION_CLAIM_STEMS: dict[str, tuple[str, ...]] = {
    "deleted": ("delet", "remov"),
    "removed": ("remov", "delet"),
    "created": ("creat",),
    "sent": ("sen",),
    "installed": ("install",),
    "uninstalled": ("uninstall",),
    "uploaded": ("upload",),
    "downloaded": ("download",),
    "moved": ("mov",),
    "renamed": ("renam",),
    "purchased": ("purchas", "bought"),
    "booked": ("book",),
    "scheduled": ("schedul",),
    "cancelled": ("cancel",),
    "canceled": ("cancel",),
    "closed": ("clos",),
    "opened": ("open",),
    "saved": ("sav",),
    "copied": ("copi", "copy"),
    "written": ("writ",),
    "launched": ("launch",),
    "restarted": ("restart",),
    # "chang"/"modif" added both ways (also on "changed"/"modified" below) so
    # differently-worded evidence and answer ("Removed the draft" / "I deleted
    # the draft") still ground each other -- Phase 24.2 test 7.
    "updated": ("updat", "chang", "modif"),
    "upgraded": ("upgrad",),
    # Phase 24.2: "created/changed/deleted/opened/updated resources" (brief §1) --
    # "changed"/"modified" were previously uncovered entirely.
    "changed": ("chang", "modif", "updat"),
    "modified": ("modif", "chang", "updat"),
    # Phase 24.2: "retrieved/found/searched information" (brief §1). Deliberately
    # asymmetric: something *found* also proves a *search* happened, but a *search*
    # alone (no confirmed result) does not itself prove something was *found* --
    # see `ground_answer`'s test 4 (unsupported retrieval claim).
    "found": ("found", "locat", "discover", "identif"),
    "located": ("locat", "found"),
    "discovered": ("discover", "found", "locat"),
    "identified": ("identif", "found"),
    "retrieved": ("retriev", "fetch", "obtain", "found"),
    "fetched": ("fetch", "retriev", "download", "obtain"),
    "searched": ("search", "quer", "found", "locat", "discover"),
}
_ACTION_CLAIM_RE = re.compile(
    r"\b(" + "|".join(re.escape(v) for v in _ACTION_CLAIM_STEMS) + r")\b", re.IGNORECASE,
)
# Phase 24.2 §5 (avoid false positives): a claim verb directly following a negation
# ("never deleted", "didn't find", "wasn't opened" -- within the same CLAUSE, so
# bounded to a short run of chars that ends neither the sentence nor the clause) is a
# NEGATIVE statement, not an assertion that something happened, and needs no evidence
# to ground it. The exclusion class also stops at a comma/semicolon/colon, not just
# sentence-enders -- without that, "I never deleted the file, just moved it" would let
# the earlier "never" spuriously suppress the later, unrelated "moved" claim too.
_CLAIM_NEGATED_RE = re.compile(
    r"\b(?:not|never|no|n't|cannot|can'?t|couldn'?t|wouldn'?t|didn'?t|doesn'?t|"
    r"won'?t|isn'?t|wasn'?t|weren'?t|hasn'?t|haven'?t|hadn'?t)\b[^.!?,;:]{0,25}$",
    re.IGNORECASE,
)
# Phase 24.2 §5: a claim verb followed by an evaluative/filler word rather than a
# concrete object ("found this interesting", "found that useful") is ordinary
# conversational wording, not a claim about a specific completed result -- brief's
# own examples ("found", "opened", "changed", "created" used as filler) name exactly
# this shape. A real result claim almost always names an object ("found the file",
# "found 3 matches"), which this pattern does not match, so it still gets checked.
_CLAIM_FILLER_RE = re.compile(
    r"^\s*(?:this|it|that|them)?\s*(?:interesting|helpful|useful|great|nice|fine|"
    r"odd|strange|curious|surprising|weird|funny|annoying|confusing|handy|cool)\b",
    re.IGNORECASE,
)


def _evidence_haystack(observations: "list[Observation]", goal: str = "") -> str:
    """Every real word the evidence (and the goal itself, so a value the USER named is
    never flagged as unsupported) actually contains — speech, tool name, args and
    (stringified) data. Pure string-building, no model call."""
    parts: list[str] = [goal or ""]
    for o in observations:
        if o.speech:
            parts.append(o.speech)
        if o.step is not None:
            parts.append(o.step.tool or "")
            if o.step.args:
                try:
                    parts.append(json.dumps(o.step.args, default=str))
                except TypeError:
                    parts.append(str(o.step.args))
        if o.data:
            try:
                parts.append(json.dumps(o.data, default=str))
            except TypeError:
                parts.append(str(o.data))
    return " ".join(parts).lower()


def _stated_in(token: str, hay: str, *, number: bool) -> bool:
    """Phase 24.8: does the evidence haystack state `token` as a WHOLE value? A plain substring test
    let "999" ride on "1999" and "report.csv" on "myreport.csv": the answer then named a value the
    evidence never produced. A number may not sit inside a longer run of digits (a unit glued to it,
    "1999KB", is fine); a filename may not continue an identifier on either side ("myreport.csv",
    "old-report.csv", "report.csv2", "report.csv.bak"), while a path prefix ("C:\\docs\\report.csv") still counts."""
    before, after = (r"(?<!\d)", r"(?!\d)") if number else (r"(?<![\w\-])", r"(?!\w|\.\w)")
    return re.search(before + re.escape(token) + after, hay) is not None


# "1,999" and "1999" are the same value: the answer is checked with its thousands separators removed, against the
# evidence as written AND with them removed (the old substring test only accepted "1,999" over "1999" by accident)
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _observation_outcome_text(o: "Observation") -> str:
    """Phase 24.2: what `o` itself actually reported HAPPENED — its speech and
    returned data. Deliberately excludes the tool name and call args: those name
    what was REQUESTED, not what the tool said occurred, so a tool whose own name
    happens to contain a claim verb (e.g. an "email.search" call that merely ran)
    can never, by itself, stand in for the tool's own confirmation that the claimed
    outcome actually occurred — the exact loophole test 8 (below) closes: the old
    Phase 24.1 check also matched on `o.step.tool`, so a plausibly-named tool that
    ran but reported nothing conclusive could wrongly ground a claim. Only used for
    the action-claim check; the number/filename check still reads the full
    `_evidence_haystack` (tool name and args included), because a concrete VALUE
    (a filename, a count) genuinely can live in a call's arguments, not just its
    result."""
    parts = [o.speech or ""]
    if o.data:
        try:
            parts.append(json.dumps(o.data, default=str))
        except TypeError:
            parts.append(str(o.data))
    return " ".join(parts).lower()


# -- Phase 24.3: goal-aware completeness guard -------------------------------
#
# Phase 24.1/24.2 catch an answer that CLAIMS something the evidence never showed.
# This is the complementary failure: an answer that makes no false claim at all (every
# concrete thing it says really did happen) but SILENTLY DROPS a real, important,
# goal-relevant result the evidence already produced -- "Created report.csv
# successfully." in the evidence, "I checked the location." in the answer. Grounded,
# but not an answer to what was asked.
#
# Deliberately reuses, rather than reimplements, Phase 18/21/23's own goal-shape
# machinery -- no second planner, no duplicated coverage system:
#   - `is_relevant`/`_current_evidence` (Phase 18): is this observation about the goal
#     at all, deduped/staleness-aware.
#   - `_is_conclusive`/`classify_mode` (Phase 18): is this observation a real,
#     substantive result -- never "a tool merely ran".
#   - `derive_clauses`/`_clause_keywords`/`_clause_relevant` (Phase 21): the goal's
#     OWN words, split into the separate requirements it names, and whether an
#     observation is about one of them.
# The only genuinely new pieces are: (a) treating a FAILED call with real, relevant,
# substantive speech as important too (a resource that doesn't exist, or an action
# that didn't happen, is itself the important result -- `_is_conclusive` requires
# `ok` for anything but a diagnostic goal, which is right for "should discovery keep
# going" but wrong here), (b) a loose word/number-overlap "did the answer mention
# this" check, and (c) treating an explicit "how many/how much" clause as needing the
# actual VALUE, not just the topic word.
#
# Conservative by construction, matching the brief's own framing: a clause is only
# ever required to be acknowledged when it has real, relevant, conclusive evidence
# AND is one of the goal's own `derive_clauses` fragments -- i.e. something the
# user's own words actually named. A single-clause goal with several relevant
# observations (e.g. "Check report.csv" turning up both an existence result and an
# incidental row count) only requires ANY ONE of them to be acknowledged: an
# incidental detail the goal's own text never separated out as its own requirement
# is never forced into the answer just because some observation happened to surface
# it. Ambiguous cases (no clause-relevant evidence at all, or a "how many" ask with
# no concrete number in evidence) are always allowed, never flagged.

_QUANTITY_REQUEST_RE = re.compile(
    r"\bhow (?:many|much|long)\b|\bnumber of\b|\bcount of\b|\btotal (?:number|count) of\b|"
    r"\bwhat(?:'s| is) the (?:count|number|total|size|amount|value)\b",
    re.IGNORECASE,
)
_NUMBER_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


def _important_observations(
    goal: str, observations: "list[Observation]", state: "_EvidenceState | None" = None,
) -> "list[Observation]":
    """The subset of `observations` an answer to `goal` should not silently ignore:
    real (non-uncertain), goal-relevant (`is_relevant`), and reporting a CONCRETE
    result rather than incidental tool metadata -- a found/not-found resource, a
    retrieved value, a completed or failed action, a conclusive search result. Not
    every relevant observation qualifies: `_is_conclusive` is the same bar Phase 18's
    evidence-sufficiency gate uses to decide "is this a real answer, not just a tool
    that ran". A FAILED call is kept unconditionally (rather than only when it also
    carries a strong-signal marker) as long as it has real, relevant, non-empty
    speech of its own -- "no such file" or "the delete failed" IS the important
    result for that goal, never something to filter out for being unsuccessful.

    Phase 24.4: an attempt/progress line ("Attempting to delete...") is never itself
    an important RESULT, and an observation a later one already resolved
    (`_resolve_evidence` -- the attempt a later success/failure replaced, the success a
    later same-operation failure overrode) is no longer the current answer, so the
    answer is not required to repeat it."""
    mode = classify_mode(goal)
    state = state or _resolve_evidence(observations)
    important: list[Observation] = []
    for o in state.ordered:
        if id(o) in state.superseded or state.kind[id(o)] == "tentative":
            continue
        if not is_relevant(goal, o):
            continue
        if o.ok:
            if _is_conclusive(mode, o):
                important.append(o)
        elif (o.speech or o.error or "").strip():
            important.append(o)
    return important


def _answer_acknowledges(o: "Observation", answer: str) -> bool:
    """Loose, deliberately forgiving overlap check -- the brief is explicit that exact
    wording is never required. Shares Phase 21's own stemmed content-word vocabulary
    (`_clause_keywords`, which also strips the shared intent-verb lexicon) so
    "Removed" (evidence) and "I deleted the draft" (answer) still overlap via the
    shared object noun even when the verb itself doesn't stem-match; a shared number
    additionally closes the gap the word-level check alone would miss for a value
    claim ("42" doesn't survive `_clause_keywords`'s length-3 floor)."""
    obs_text = _observation_outcome_text(o)
    obs_numbers = set(_NUMBER_TOKEN_RE.findall(obs_text))
    overlap = bool(
        _clause_keywords(obs_text) & _clause_keywords(answer)
        or (obs_numbers and obs_numbers & set(_NUMBER_TOKEN_RE.findall(answer or "")))
        # Phase 24.4: a failing call's own speech often never names its target
        # ("Delete failed: permission denied."), so the file the call was aimed at
        # (its args) counts as naming the same subject.
        or _object_files(o) & _object_files_of_text(answer)
    )
    # Phase 24.4: a FAILED result is only acknowledged by an answer that actually says
    # something negative -- "I deleted report.csv" shares every content word with
    # "Delete failed" yet reports the opposite outcome.
    if overlap and _evidence_kind(o) == "failure" and not _answer_polarity(answer)[1]:
        return False
    return overlap


def _clause_gap(clause: str, important: "list[Observation]", answer: str) -> "Observation | None":
    """Is `clause` -- one requirement the goal's own words name (`derive_clauses`) --
    acknowledged by `answer`? Returns the one important, clause-relevant observation
    the answer fails to acknowledge, or None when the clause has no important
    evidence to check (nothing to require) or is already satisfied.

    A clause that explicitly asks for a VALUE (`_QUANTITY_REQUEST_RE` -- "how many",
    "how much", ...) needs the concrete number itself to appear in the answer, not
    just the topic word, so "it has some rows" still fails a "how many rows" ask. Any
    other clause (a plain existence/status/action ask) is satisfied by ANY ONE of its
    relevant important observations being acknowledged -- other, incidental evidence
    that also happens to overlap the same clause's words is never additionally
    required (brief §5/§6B)."""
    keywords = _clause_keywords(clause)
    relevant = [o for o in important if not keywords or _clause_relevant(keywords, o, with_data=True)]
    if not relevant:
        return None
    # Phase 24.4: a concrete result beats a generic status ("Found several files.") --
    # when the clause has both, only the concrete one can satisfy it.
    relevant = [o for o in relevant if not _is_generic(o)] or relevant
    if _QUANTITY_REQUEST_RE.search(clause):
        numeric = [o for o in relevant if _NUMBER_TOKEN_RE.search(_observation_outcome_text(o))]
        if not numeric:
            return None  # nothing concrete in evidence to require -- ambiguous, allow
        answer_numbers = set(_NUMBER_TOKEN_RE.findall(answer or ""))
        for o in numeric:
            if not (set(_NUMBER_TOKEN_RE.findall(_observation_outcome_text(o))) & answer_numbers):
                return o
        return None
    if not any(_answer_acknowledges(o, answer) for o in relevant):
        return relevant[0]
    return None


def _completeness_gaps(goal: str, answer: str, important: "list[Observation]") -> "list[Observation]":
    """Phase 24.3: which of `goal`'s own explicit requirements (`derive_clauses`) name
    an important real result (`important` — see `_important_observations`) that
    `answer` never acknowledges. Empty = complete. Checked per CLAUSE, never per
    observation, so a single-clause goal with several relevant observations only
    needs ANY ONE acknowledged, while a goal that names a second requirement
    explicitly ("... and tell me how many rows") gets that requirement checked on its
    own. `important` is computed once by the caller (`ground_answer` also uses it to
    decide whether there is any real evidence at all — a real, conclusive NEGATIVE
    result, e.g. "no such file", is important even though it isn't `ok`)."""
    if not important:
        return []
    gaps: list[Observation] = []
    seen: set[int] = set()
    for clause in derive_clauses(goal):
        gap = _clause_gap(clause, important, answer)
        if gap is not None and id(gap) not in seen:
            seen.add(id(gap))
            gaps.append(gap)
    return gaps


def _completeness_fallback(answer: str, gaps: "list[Observation]") -> str:
    """Never invents a correction: appends the REAL, already-gathered observation
    speech the answer silently left out, verbatim, on top of the (otherwise grounded,
    still-kept) answer -- the same "restate real evidence, never guess" philosophy as
    `ground_answer`'s own unsupported-claim fallback below, just additive rather than
    replacing, since nothing here was actually wrong."""
    seen: set[str] = set()
    extras: list[str] = []
    for o in gaps:
        text = (o.speech or o.error or "").strip()
        if text and text not in seen:
            seen.add(text)
            extras.append(text)
    if not extras:
        return answer
    base = answer.rstrip()
    if base and base[-1] not in ".!?":
        base += "."
    return f"{base} Also: {' '.join(extras)}"[:900]


# -- Phase 24.4: final-answer evidence prioritization -------------------------
#
# Phases 24.1-24.3 treat every real observation as an equal, timeless witness: an
# "Attempting to delete report.csv." line grounds "I deleted report.csv" exactly as well
# as the real "Deleted report.csv." would, a success that a later call of the same
# operation contradicted still grounds a claim, and a vague intermediate status can
# satisfy a goal a concrete final result answers. This section decides WHICH
# observations get to speak for the final answer -- deterministically (word lists and
# the evidence's own order, no model, no numeric score) and by reusing the existing
# machinery (`_current_evidence`, `is_relevant`, `_is_conclusive`, the 24.2 claim-verb
# table, the 24.3 `important` set) rather than building a second evaluator.
#
# Every observation is one `kind`:
#   failure    -- the call failed, or its speech reports a failure ("Delete failed")
#   tentative  -- an attempt/progress/assumption, not a result ("Attempting to ...",
#                 "Searching the folder...", "Presumably deleted"): says what was TRIED
#   success    -- a real reported result (a vague one -- "Found several files." -- is
#                 additionally `_is_generic`: true, but not the concrete answer)
#   neutral    -- no speech at all (its data may still ground a value)
#
# Precedence between two observations of the SAME ACTION (a shared action-verb family
# from the 24.2 table) on a compatible OBJECT (a shared filename, or one side names none
# -- two different named files never interact):
#   - a tentative observation is superseded by a later success/failure of that action;
#   - a generic success is superseded by a later concrete success;
#   - a success and a failure of the same operation (same tool) -> the LATER one wins
#     (a retry that worked, or a later failure overriding an earlier assumption of
#     success) -- `_current_evidence` already does this for identical tool+args;
#   - a success and a failure reported by DIFFERENT tools (independent witnesses) are a
#     genuine conflict that order cannot settle: neither is dropped, and an answer that
#     takes one side without the other gets the other appended (`_conflict_gaps`), never
#     a silently invented resolution.
# Recency is only ever the tie-breaker (`prioritize_evidence`): a later observation
# about something else ("Opening another directory.") never overrides an earlier useful
# one, because supersession needs the same action AND object.
#
# What may GROUND a completed-action claim follows: real (ok), non-tentative,
# non-superseded observations (an attempt only ever grounds "searched"). Filename and
# number grounding are unchanged -- an attempted/superseded observation still proves the
# file or count exists in the evidence.

_ATTEMPT_RE = re.compile(
    r"\b(?:attempt(?:ing|ed)|trying to|about to|going to|preparing to|starting to|"
    r"working on|in progress|pending|please wait|one moment|will be)\b",
    re.IGNORECASE,
)
# A short line ending in an ellipsis is a progress message ("Searching the folder..."); a
# long one is more likely a truncated real result, so it is not treated as progress.
_PROGRESS_ELLIPSIS_RE = re.compile(r"(?:\.\.\.|…)\s*$")
_ASSUMED_RE = re.compile(
    r"\b(?:presumably|supposedly|should have|might have|may have|probably)\b", re.IGNORECASE,
)
_FAILURE_RE = re.compile(
    r"\b(?:fail(?:ed|s|ure)?|couldn'?t|could not|can'?t|cannot|unable to|denied|refused|"
    r"not found|no such|doesn'?t exist|does not exist|not permitted|not allowed)\b",
    re.IGNORECASE,
)
_SUCCESS_WORD_RE = re.compile(r"\b(?:successfully|succeeded|worked|completed)\b", re.IGNORECASE)
# Deliberately broad and lenient, ANSWER side only: any negative/failure wording counts as
# the answer saying "it didn't work", so an honest negative answer is never penalized for
# using different words than the evidence.
_ANSWER_NEGATIVE_RE = re.compile(
    r"\b(?:no|not|never|none|nothing|missing|unable|unavailable|unreachable|denied|refused|"
    r"fail(?:ed|s|ure)?|error|exception|timed out|problem|issue|blocked|couldn'?t|can'?t|"
    r"cannot|won'?t|didn'?t|wasn'?t|isn'?t|doesn'?t|hasn'?t|haven'?t|don'?t|aren'?t|weren'?t)\b",
    re.IGNORECASE,
)
_VAGUE_RE = re.compile(r"\b(?:several|some|multiple|various|a few|many|a number of)\b", re.IGNORECASE)
_REASSURANCE_RE = re.compile(
    r"\b(?:no|not any|without(?: any)?|zero)\s+(?:\w+\s+)?"
    r"(?:errors?|issues?|problems?|failures?|warnings?|trouble|complications?|glitch(?:es)?|hiccups?|worr(?:y|ies))\b"
    r"|\bnothing (?:went )?wrong\b|\bnot a problem\b",
    re.IGNORECASE,
)


def _evidence_kind(o: "Observation") -> str:
    """failure / tentative / success / neutral -- see the section comment above."""
    if not o.ok:
        return "failure"
    text = f"{o.speech or ''} {o.error or ''}".strip().lower()
    if not text:
        return "neutral"
    if _ASSUMED_RE.search(text):
        return "tentative"
    has_result = bool(
        _FAILURE_RE.search(text) or _SUCCESS_WORD_RE.search(text) or _ACTION_CLAIM_RE.search(text)
    )
    if not has_result and (
        _ATTEMPT_RE.search(text) or (len(text) <= 80 and _PROGRESS_ELLIPSIS_RE.search(text))
    ):
        return "tentative"
    return "failure" if _FAILURE_RE.search(text) else "success"


def _is_progress_line(o: "Observation") -> bool:
    """A successful call whose whole report is that work is UNDER WAY ("Attempting to
    delete report.csv.", "Still working on it...", "Searching the folder..."): the
    tentative kind, minus the two weaker triggers that are fine for grounding a claim but
    too eager to decide that a subgoal is still open -- a hedge word ("probably") and the
    bare future "will be" ("It will be sunny tomorrow." is a forecast, a real answer)."""
    if not o.ok or _evidence_kind(o) != "tentative":
        return False
    text = f"{o.speech or ''} {o.error or ''}".strip().lower()
    if _ASSUMED_RE.search(text):
        return False
    return any(m.group(0).lower() != "will be" for m in _ATTEMPT_RE.finditer(text)) or bool(
        len(text) <= 80 and _PROGRESS_ELLIPSIS_RE.search(text)
    )


def _is_generic(o: "Observation") -> bool:
    """A vague status with nothing concrete to hold on to ("Found several files."): a
    quantifier word and no filename or number."""
    text = f"{o.speech or ''} {o.error or ''}"
    return bool(_VAGUE_RE.search(text) and not _FILENAME_CLAIM_RE.search(text) and not re.search(r"\d", text))


def _object_files_of_text(text: str) -> set[str]:
    return {m.lower() for m in _FILENAME_CLAIM_RE.findall(text or "")}


def _object_files(o: "Observation") -> set[str]:
    """The filenames an observation is ABOUT: named in its speech or aimed at by its args."""
    parts = [o.speech or "", o.error or ""]
    if o.step is not None and o.step.args:
        parts.append(" ".join(str(v) for v in o.step.args.values()))
    return _object_files_of_text(" ".join(parts))


def _action_keys(o: "Observation") -> set[str]:
    """Which 24.2 claim-verb families (`_ACTION_CLAIM_STEMS`) the observation's own speech
    talks about -- "Delete failed" and "Deleted x" both land in the delete/remove family."""
    text = f"{o.speech or ''} {o.error or ''}".lower()
    return {
        key for key, stems in _ACTION_CLAIM_STEMS.items()
        if any(re.search(r"\b" + re.escape(stem), text) for stem in stems)
    }


def _object_link(a: "Observation", b: "Observation") -> str:
    fa, fb = _object_files(a), _object_files(b)
    if fa and fb:
        return "shared" if fa & fb else "disjoint"
    return "one-sided" if (fa or fb) else "none"


# Phase 24.8: an existence report ("report.csv still exists.") has no failure or action wording, so the
# precedence rules cannot see that it flatly contradicts another tool's "Deleted report.csv successfully.".
_STILL_THERE_RE = re.compile(r"\bstill\s+(?:exists?|there|present|in place|around)\b", re.IGNORECASE)
_REMOVAL_KEYS = frozenset({"deleted", "removed"})


@dataclass(frozen=True)
class _EvidenceState:
    ordered: "list[Observation]"                              # current, non-uncertain; oldest -> newest
    kind: "dict[int, str]"                                    # id(observation) -> kind
    superseded: "frozenset[int]"                              # ids a later observation resolved
    conflicts: "list[tuple[Observation, Observation]]"        # unresolved success-vs-failure pairs


def _resolve_evidence(observations: "list[Observation]") -> _EvidenceState:
    """Apply the precedence rules from the section comment above. Uncertain observations
    are never part of the state at all -- they can neither speak for the answer nor
    override anything."""
    position = {id(o): i for i, o in enumerate(observations)}
    # `_current_evidence` lets a later call of the same tool+args replace an earlier one
    # (Phase 18 staleness) -- right for a fresh RESULT, wrong for a later progress/attempt
    # line ("Still working on it..."), which must not erase a result that call already
    # produced (recency is not correctness).
    resulted: set[str] = set()
    kept: list[Observation] = []
    for o in observations:
        if _evidence_kind(o) == "tentative" and _call_key(o) in resulted:
            continue
        if _evidence_kind(o) != "tentative":
            resulted.add(_call_key(o))
        kept.append(o)
    ordered = sorted(
        (o for o in _current_evidence(kept) if not (o.data or {}).get("uncertain")),
        key=lambda o: position[id(o)],
    )
    kind = {id(o): _evidence_kind(o) for o in ordered}
    superseded: set[int] = set()
    pairs: list[tuple[Observation, Observation]] = []
    for i, later in enumerate(ordered):
        if kind[id(later)] not in ("success", "failure"):
            continue  # only a real result can resolve or contradict an earlier observation
        later_keys = _action_keys(later)
        for earlier in ordered[:i]:
            earlier_kind = kind[id(earlier)]
            if earlier_kind == "neutral" or not (_action_keys(earlier) & later_keys):
                continue
            link = _object_link(earlier, later)
            if link == "disjoint":
                continue
            if earlier_kind == "tentative" or (
                earlier_kind == "success" and kind[id(later)] == "success"
                and _is_generic(earlier) and not _is_generic(later)
            ):
                superseded.add(id(earlier))
            elif earlier_kind != kind[id(later)] and link != "none":
                if earlier.step.tool == later.step.tool:
                    superseded.add(id(earlier))
                else:
                    pairs.append((earlier, later))
    # Phase 24.8: one tool reports the thing deleted/removed, ANOTHER says the same file "still exists" -- an
    # unresolved conflict like any other. Paired (removal, denial) whichever came first, which is the order
    # `_conflict_gaps` reads as (success, failure).
    for denial in ordered:
        if kind[id(denial)] == "tentative" or not _STILL_THERE_RE.search(f"{denial.speech or ''} {denial.error or ''}"):
            continue
        for removal in ordered:
            if (
                removal is not denial and kind[id(removal)] == "success" and removal.step.tool != denial.step.tool
                and _action_keys(removal) & _REMOVAL_KEYS and _object_link(removal, denial) == "shared"
                and not any(e is removal and l is denial for e, l in pairs)
            ):
                pairs.append((removal, denial))
    conflicts = [(e, l) for e, l in pairs if id(e) not in superseded and id(l) not in superseded]
    return _EvidenceState(ordered, kind, frozenset(superseded), conflicts)


def prioritize_evidence(
    goal: str, observations: "list[Observation]", state: "_EvidenceState | None" = None,
) -> "list[Observation]":
    """The observations that may speak for the final answer, best first: (1) real +
    conclusive + directly relevant, (2) real + conclusive, (3) real + relevant and
    substantive, (4) everything else real. A failed/negative observation is real
    evidence and competes on the same terms (a relevant failure is conclusive). An
    observation a later one superseded sorts after every current one -- kept, never
    discarded -- and recency only breaks ties inside a tier. Uncertain observations
    are not in the list at all."""
    state = state or _resolve_evidence(observations)
    mode = classify_mode(goal)
    position = {id(o): i for i, o in enumerate(state.ordered)}

    def tier(o: "Observation") -> int:
        kind = state.kind[id(o)]
        relevant = is_relevant(goal, o)
        substantive = bool((o.speech or o.error or "").strip()) and kind != "tentative"
        conclusive = (
            substantive and not _is_generic(o)
            and (_is_conclusive(mode, o) or (kind == "failure" and relevant))
        )
        if conclusive and relevant:
            return 0
        if conclusive:
            return 1
        return 2 if relevant and substantive else 3

    return sorted(state.ordered, key=lambda o: (id(o) in state.superseded, tier(o), -position[id(o)]))


def _grounding_pool(state: _EvidenceState, *, include_attempts: bool = False) -> "list[Observation]":
    """The observations that can ground a completed-action claim: real (ok), not a
    failure, not an unresolved attempt, and not overridden by a later same-operation
    result. A superseded GENERIC success stays (it is still true, just not the
    answer); an attempt only counts when `include_attempts` (a "searched" claim)."""
    pool: list[Observation] = []
    for o in state.ordered:
        if not o.ok:
            continue
        kind = state.kind[id(o)]
        if kind == "failure":
            continue
        if kind == "tentative":
            if include_attempts:
                pool.append(o)
            continue
        if id(o) in state.superseded and not _is_generic(o):
            continue
        pool.append(o)
    return pool


def _answer_polarity(answer: str) -> tuple[bool, bool]:
    """(claims something happened, says something did not) -- the two sides an answer
    can take on a success-vs-failure question. Reuses the 24.2 negation/filler rules so
    "never deleted" is negative and "found this interesting" is neither."""
    text = answer or ""
    positive = bool(_SUCCESS_WORD_RE.search(text))
    # Phase 24.8: "no errors" / "without any problem" / "nothing went wrong" DENY a problem -- they reassure, they are
    # not the answer saying something did not happen, so they must not hide a conflicting failure behind a confident success
    negative = bool(_ANSWER_NEGATIVE_RE.search(_REASSURANCE_RE.sub(" ", text)) or _STILL_THERE_RE.search(text))
    for m in _ACTION_CLAIM_RE.finditer(text):
        if _CLAIM_NEGATED_RE.search(text[max(0, m.start() - 40):m.start()]):
            negative = True
        elif not _CLAIM_FILLER_RE.match(text[m.end():m.end() + 40]):
            positive = True
    return positive, negative


def _conflict_gaps(goal: str, answer: str, state: _EvidenceState) -> "list[Observation]":
    """For every unresolved success-vs-failure conflict (different tools reporting
    opposite outcomes of the same action on the same object) about the goal: if the
    answer commits to ONE side only, the observation for the other side -- which the
    answer silently dropped rather than reconciled -- is the gap. An answer saying both
    (or neither) is left alone."""
    positive, negative = _answer_polarity(answer)
    if positive == negative:
        return []
    gaps: list[Observation] = []
    for earlier, later in state.conflicts:
        if not (is_relevant(goal, earlier) and is_relevant(goal, later)):
            continue
        success, failure = (earlier, later) if state.kind[id(earlier)] == "success" else (later, earlier)
        gaps.append(failure if positive else success)
    return gaps


# Phase 24.5: the 24.2 "found/retrieved/searched" family -- READ verbs. The composer's dedicated
# evidence-only answer is held to them (unchanged); a planner's free-form `done` summary is
# not (`ground_answer(..., retrieval_claims=False)`): it routinely paraphrases what a read
# tool merely LISTED ("Two outstanding items found: ..." over "TODO.md lists: ..."), and the
# same summary's numbers, filenames and state-changing verbs are still all checked.
_RETRIEVAL_CLAIM_VERBS = frozenset(
    {"found", "located", "discovered", "identified", "retrieved", "fetched", "searched"}
)


# -- Phase 24.9: per-object action grounding ---------------------------------
#
# The 24.2 claim check asks whether SOME real result reported this KIND of action ("delet"/"remov" for "Deleted"); the
# 24.1/24.8 filename check asks whether each named file occurs SOMEWHERE in the evidence (the goal counts). Neither ties the
# two together, so "Deleted report.csv and notes.txt." passed on the evidence "Deleted report.csv successfully." as soon as
# the goal -- or any other observation -- merely MENTIONED notes.txt: the action was grounded for one object and the other
# was only "present". This section asks the missing question per object: for every concrete file the claim names, does a real
# result report THAT action about THAT file?
#
# Two small, separate pieces, both reusing the existing machinery (`_ACTION_CLAIM_RE`, `_FILENAME_CLAIM_RE`, `_stated_in`,
# the 24.4 grounding pool, the 24.2 negation rule) and neither a parser:
#   - `_claimed_objects` (answer side) reads the files ONE claim verb is about, from two deterministic shapes only: the
#     names right after the verb ("Deleted report.csv, notes.txt and data.csv") or, when the verb is passive, the names right
#     before it ("report.csv and notes.txt were deleted"). Every verb occurrence is read on its own within its own sentence
#     or line, so "Deleted report.csv. Deleted notes.txt." is simply two claims. The words that may sit between the verb and
#     its first file are a closed class (determiners, generic file nouns), and between two files only joiners, articles,
#     a unit in parentheses or one place word ("X to Archive and Y to Backup"). Anything else -- a comma splice, a contrast,
#     a negation, an "or", a name that starts a new clause ("... and notes.txt is locked"), a stray noun ("... and kept
#     notes.txt"), a destination, a verb with no file named at all -- yields NO object, and the claim is then judged exactly as
#     before. Unsure means allow.
#   - `_grounds_object` (evidence side) asks whether ONE observation's own reported outcome (speech + data) states the action
#     about the file: in one sentence (with no contrast or exclusion between the action and the file, and the file not merely
#     the subject of "... still exists"), or in the sentence after a lead-in that states the action without naming a file
#     ("Found 3 matches. The closest is a.csv."), or in its returned data when the data itself pairs the two ("deleted":
#     [...]). Not counted: a negated action ("was not removed"), an offer / plan / skip / zero ("I can delete", "Dry run:
#     would delete", "Skipped deleting", "0 files deleted"). Its tool name never counts. Its call args count in exactly one
#     case: an outcome that states the action but names no file at all ("File deleted.") is about the call it answers, so the
#     files that call was aimed at are its objects -- args never turn an outcome that lacks the action, or names another file,
#     into support (the 24.2 "what was asked for is not what happened" rule).
# Which observations may be asked is unchanged: the 24.4 grounding pool (real, non-failure, non-attempt, not overridden), so a
# failed delete, an attempt line or an overridden success can never turn into support for the object it mentions. Goal
# completeness (24.3) and conflicts (24.4) are not this helper's business and stay where they were.

_OBJECT_GAP_MAX_WORDS = 5
# The only words that may sit between a claim verb and its FIRST file ("Deleted the old backup file called report.csv"):
# determiners, quantifiers, generic file nouns. Anything else -- a contrast, a negation, an alternative, another
# verb, a place ("... from Downloads"), a stray noun ("Deleted the cache and kept notes.txt") -- means the file may
# belong to another clause, and it is not attributed to this verb.
_OBJECT_GAP_WORDS = frozenset({
    "the", "a", "an", "this", "these", "those", "both", "all", "each", "every", "my", "your", "our", "their",
    "its", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "file", "files", "document",
    "documents", "copy", "copies", "item", "items", "following", "requested", "named", "called", "specified", "old",
    "new", "temp", "temporary", "backup", "duplicate", "extra", "stale", "also", "successfully", "already", "now",
    "only", "just",
})
_OBJECT_EITHER_RE = re.compile(r"\b(?:or|either)\b", re.IGNORECASE)
# A claim never reaches past the end of its sentence or its LINE ("Deleted report.csv\nnotes.txt: permission denied")
_OBJECT_BREAK_RE = re.compile(r"(?<=[.!?\u2026])\s+|\s*\n\s*")
# The only text that may sit between two names of one list: separators, "and"-words, an article, an adjective of the
# file ("and the old notes.txt"), a unit in parentheses, or ONE place word after to/from/in ("X to Archive and Y to Backup")
_OBJECT_LIST_GAP_RE = re.compile(
    r"(?:[\s,&]|\b(?:and|plus|then|also|as well as|along with|the|a|an|both|all|files?|documents?|successfully|old|new|"
    r"temp|temporary|backup|duplicate|extra|stale)\b|\([^()]{0,30}\)"
    r"|\b(?:to|into|from|in|on|at)\s+(?:the\s+)?[^\s,;.()]+(?:\s+(?:folder|directory|drive))?)*",
    re.IGNORECASE,
)
# Markdown / quote decoration around a name ("**report.csv**", "`notes.txt`", 'notes.txt') and a path prefix
# ("C:/docs/", drive-and-backslash paths) are not text between names
_OBJECT_DECOR_RE = re.compile(r"[*`\"'\u2018\u2019\u201c\u201d]")
_OBJECT_PATH_RE = re.compile(r"\S*[/\x5c]\S*")
# A name followed by one of these opens a NEW clause ("... and notes.txt is locked"): it is a subject, not an object
_OBJECT_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:now|also|then|just|already|really|actually|currently|again)\s+)*"
    r"(?:was|were|is|are|isn'?t|wasn'?t|weren'?t|aren'?t|still|remains?|remained|stays?|stayed|has|have|had|hasn'?t|haven'?t|"
    r"contains?|does|did|doesn'?t|didn'?t|can|can'?t|cannot|could|couldn'?t|will|won'?t|would|should|might|may|seems?|looks?|"
    r"appears?|appeared|needs?|needed|exists?|fail(?:ed|s)?)\b",
    re.IGNORECASE,
)
_OBJECT_PASSIVE_RE = re.compile(
    r"(?:\b(?:has|have|had)\s+(?:(?:\w+ly|also|now|both|all|already|just|then)\s+)*)?"
    r"\b(?:was|were|is|are|been|be|got|gets?)\b(?:\s+(?:\w+ly|also|now|both|all|already|just|then))*\s*$", re.IGNORECASE,
)
# A passive under a negation ("Neither X nor Y was deleted", "None of X and Y were deleted") says nothing FOR its names: no
# object. (A future / possible passive -- "Y will be deleted", "Y can be removed" -- needs no rule: the modal sits between
# the name and the auxiliary, which ends the chain of names.)
_OBJECT_NEG_LEAD_RE = re.compile(r"\b(?:neither|nor|none|no|not|never|nobody|nothing|cannot)\b|n't\b", re.IGNORECASE)
# A comma or colon may follow the first words only when they introduce a list ("Deleted 2 files, report.csv and notes.txt")
_OBJECT_COLON_RE = re.compile(r"\b(?:(?:files|documents|items|copies|attachments)\s*[:,]|following\s*:)\s*$", re.IGNORECASE)


def _tidy_gap(gap: str) -> str:
    return _OBJECT_DECOR_RE.sub("", _OBJECT_PATH_RE.sub("", gap))


def _object_gap_ok(gap: str) -> bool:
    """May `gap` (the words between a claim verb and its first file) still belong to the same clause as the verb?"""
    if re.search(r"[;\u2014]|--", gap) or (re.search(r"[:,]", gap) and not _OBJECT_COLON_RE.search(gap)):
        return False
    words = re.findall(r"[\w']+", gap.lower())
    return len(words) <= _OBJECT_GAP_MAX_WORDS and all(w in _OBJECT_GAP_WORDS or w.isdigit() for w in words)


def _claimed_objects(answer: str, start: int, end: int) -> "list[str] | None":
    """The filenames the claim verb at `answer[start:end]` is about, lower-cased; `[]` when none can be told apart
    safely; `None` when the claim asserts nothing about specific files (an alternative "report.csv or notes.txt", a
    negated passive). See the section comment above for the only shapes recognised."""
    lo, hi = 0, len(answer)
    for b in _OBJECT_BREAK_RE.finditer(answer):
        if b.end() <= start:
            lo = b.end()
        elif b.start() >= end:
            hi = b.start()
            break
    sentence = answer[lo:hi]
    v0, v1 = start - lo, end - lo
    names = [(m.start(), m.end(), m.group(0).lower()) for m in _FILENAME_CLAIM_RE.finditer(sentence)]
    found: list[str] = []
    passive = _OBJECT_PASSIVE_RE.search(sentence[:v0])
    if passive:  # "report.csv and notes.txt were deleted": the chain of names that ends at the auxiliary
        if _OBJECT_NEG_LEAD_RE.search(sentence[:v0]):
            return None
        cursor = passive.start()
        for s, e, name in reversed([n for n in names if n[1] <= cursor]):
            gap = _tidy_gap(sentence[e:cursor])
            if _OBJECT_EITHER_RE.search(gap):
                return None
            if not _OBJECT_LIST_GAP_RE.fullmatch(gap):
                break
            found.append(name)
            cursor = s
        return found
    cursor = v1
    for s, e, name in names:
        if s < v1:
            continue
        gap = _tidy_gap(sentence[cursor:s])
        if found:
            if _OBJECT_EITHER_RE.search(gap):
                return None
            if not _OBJECT_LIST_GAP_RE.fullmatch(gap):
                break
        elif not _object_gap_ok(gap):
            break
        if _OBJECT_SUBJECT_RE.match(_OBJECT_DECOR_RE.sub("", sentence[e:e + 40])):
            break
        found.append(name)
        cursor = e
    return found


# What makes evidence text NOT report an action: an offer / request / plan ("I can delete Y", "Dry run: would delete Y",
# "Do you want me to delete Y?"), a skip or cancellation, a zero ("Nothing was deleted.", "0 files deleted.")
_EVIDENCE_HEDGED_RE = re.compile(
    r"\b(?:nothing|none|nobody|neither|nor|zero|would|will|can|could|should|may|might|shall|want|skipp\w*|cancel\w*|"
    r"declin\w*|dry.run|going to|about to)\b[^.!?;:]{0,30}$|\b0\s+(?:\w+\s+){0,2}$",
    re.IGNORECASE,
)
# An evidence sentence that names a file only as the SUBJECT of a state ("notes.txt still exists") reports no action on it
_EVIDENCE_STATE_RE = re.compile(
    r"^\s*(?:(?:now|also|then|just|already|really|actually|currently|again)\s+)*"
    r"(?:still|remains?|remained|stays?|stayed|exists?|contains?|has|have|had|does|did|doesn'?t|didn'?t|can|can'?t|cannot|"
    r"could|couldn'?t|will|won'?t|would|should|might|may|seems?|looks?|appears?|needs?|(?:is|are|was|were)\s+not|"
    r"isn'?t|aren'?t|wasn'?t|weren'?t|fail(?:ed|s)?)\b",
    re.IGNORECASE,
)
# Between the action word and the file, a contrast or an exclusion means the action is about something else
# ("Deleted report.csv but left notes.txt in place", "... and kept notes.txt")
_EVIDENCE_CONTRAST_RE = re.compile(
    r"\b(?:but|however|although|though|whereas|while|except|excluding|instead|kept|left|skipp\w*|not|no)\b|n't\b", re.IGNORECASE,
)
# A later sentence (after a lead-in such as "Deleted 2 files.") that names the file as skipped / left / kept is not a result for it
_LEAD_IN_STOP_RE = re.compile(
    r"\b(?:skipp?(?:ed|ing)?|left|kept|unchanged|untouched|remain\w*|still|except|excluding|ignored|preserved|locked)\b",
    re.IGNORECASE,
)
# In returned data: a file listed under a key that says it did NOT happen (failed / remaining / skipped ...)
_DATA_NEGATIVE_KEY_RE = re.compile(r"fail|remain|skip|error|pending|left|kept|unchanged|locked|denied|missing", re.IGNORECASE)


def _action_starts(text: str, stems: "tuple[str, ...]") -> "list[int]":
    """Where `text` reports the action (a word starting with one of `stems`) as DONE: not negated ("was not removed"), not an
    offer / plan / skip / zero (`_EVIDENCE_HEDGED_RE`). `text` has had its reassurances ("no errors") taken out."""
    return [
        m.start() for stem in stems for m in re.finditer(r"\b" + re.escape(stem), text)
        if not (_CLAIM_NEGATED_RE.search(text[max(0, m.start() - 40):m.start()])
                or _EVIDENCE_HEDGED_RE.search(text[max(0, m.start() - 40):m.start()]))
    ]


def _has_action(text: str, stems: "tuple[str, ...]") -> bool:
    return bool(_action_starts(_REASSURANCE_RE.sub(" ", text), stems))


def _name_starts(text: str, name: str) -> "list[int]":
    """Where `text` names the file as an OBJECT: a whole-value name that is not the subject of "... still exists"."""
    return [
        m.start() for m in re.finditer(r"(?<![\w\-])" + re.escape(name) + r"(?!\w|\.\w)", text)
        if not _EVIDENCE_STATE_RE.match(_OBJECT_DECOR_RE.sub("", text[m.end():m.end() + 40]))
    ]


def _data_says(data: str, stems: "tuple[str, ...]", name: str) -> bool:
    """The returned data itself pairs the action with the file: 'deleted': ['a.csv', 'b.csv'] / {'name': 'a.csv', 'action': 'deleted'}."""
    n = r"(?<![\w\-])" + re.escape(name) + r"(?!\w|\.\w)"
    return any(
        re.search(re.escape(stem) + r"[^\]}{]{0,80}" + n + "|" + n + r"[^\]}{]{0,80}" + re.escape(stem), data)
        for stem in stems
    )


def _grounds_object(o: "Observation", stems: "tuple[str, ...]", name: str) -> bool:
    """Does observation `o`'s own reported outcome state the action `stems` about the file `name`? Its tool name never
    counts, and neither do its call args -- with one narrow exception: an outcome that states the action but names no file at
    all ("File deleted.", "Deleted 2 files.") is about the call it answers, so the file(s) that call was aimed at are its
    objects. Args never turn an outcome that does not state the action, or one that names another file, into support."""
    segments = [s.strip() for s in re.split(r"(?<=[.!?\u2026])\s+|[;\n]+", (o.speech or "").lower()) if s.strip()]
    lead_in = False  # an earlier sentence of this observation states the action without naming any file
    for seg in segments:
        text = _REASSURANCE_RE.sub(" ", seg)
        acts, hits = _action_starts(text, stems), _name_starts(text, name)
        if any(not _EVIDENCE_CONTRAST_RE.search(text[min(p, q):max(p, q)]) for p in acts for q in hits):
            return True
        if hits and lead_in and not _ANSWER_NEGATIVE_RE.search(seg) and not _LEAD_IN_STOP_RE.search(seg):
            return True
        if acts and not _FILENAME_CLAIM_RE.search(text):
            lead_in = True
    if o.data:
        try:
            data = json.dumps(o.data, default=str, ensure_ascii=False).lower()
        except TypeError:
            data = str(o.data).lower()
        if _data_says(data, stems, name):
            return True
        if lead_in:  # "Deleted 2 files." + the names in the data -- unless the data files it under failed / remaining / skipped
            for m in re.finditer(r"(?<![\w\-])" + re.escape(name) + r"(?!\w|\.\w)", data):
                if not _DATA_NEGATIVE_KEY_RE.search(re.split(r"[\]}]", data[max(0, m.start() - 100):m.start()])[-1]):
                    return True
    outcome = _observation_outcome_text(o)
    if o.step is not None and o.step.args and _has_action(outcome, stems) and not _FILENAME_CLAIM_RE.search(outcome):
        return name in _object_files_of_text(" ".join(str(v) for v in o.step.args.values()))
    return False


def ground_answer(
    goal: str, answer: str, observations: "list[Observation]", *, retrieval_claims: bool = True,
) -> str:
    """Phase 24.1, broadened Phase 24.2, completeness added Phase 24.3 — the
    deterministic grounding guard for `Orchestrator._answer_from_evidence`'s composed
    answer. Returns `answer` unchanged when it is already backed by real evidence and
    doesn't silently omit an important, goal-relevant result; otherwise returns
    either a plain evidence-only "insufficient" statement (an UNSUPPORTED claim —
    Phase 24.1/24.2) or `answer` with the omitted real result appended (an
    INCOMPLETE-but-grounded answer — Phase 24.3). Never fabricates a replacement fact
    in either case — every fallback only ever restates real observation speech, and
    never repeats an unsupported claim. A single unsupported claim fails the WHOLE
    answer closed (never a partial edit) — this was already Phase 24.1's behavior and
    is unchanged by Phase 24.2's broader claim detection or Phase 24.3's addition.

    Phase 24.4 changes only WHICH observations may speak for the answer (see the
    section above `_ATTEMPT_RE`): an attempt line grounds no completed-action claim, a
    later same-operation result overrides an earlier one, a cross-tool success-vs-
    failure conflict is preserved (the missing side is appended), and the fallback
    restates the prioritized evidence. It only ever removes grounding, never adds it.

    Phase 24.5: `retrieval_claims=False` (the planner's own `done` summary, see
    `Orchestrator._ground_done_summary`) stops "found/located/retrieved/..." from counting as
    a claim that needs its own evidence wording; everything else is checked exactly as before."""
    if not answer or not answer.strip():
        return answer

    real = [o for o in observations if o.ok and not (o.data or {}).get("uncertain")]
    # Phase 24.3: `real` alone (ok, non-uncertain) is the right bar for GROUNDING a
    # positive claim — but a genuinely conclusive NEGATIVE result ("no such file",
    # "the delete failed") is real evidence too, even though it isn't `ok`, and
    # answering honestly from it ("I couldn't find it") is not "no evidence at all".
    # `important` (below) is exactly that broader, still-conservative set.
    # Phase 24.4: one pass decides which observations may speak for the answer (an
    # attempt is not a result; a later same-operation result overrides an earlier one).
    state = _resolve_evidence(observations)
    important = _important_observations(goal, observations, state)
    if not real and not important:
        return (
            "I don't have enough confirmed evidence to answer that — the evidence "
            "gathered so far is insufficient, so I won't guess."
        )

    hay = _evidence_haystack(real, goal)
    pool = _grounding_pool(state)
    search_pool = _grounding_pool(state, include_attempts=True)
    unsupported = False
    for pattern, text, source in (
        (_FILENAME_CLAIM_RE, answer, hay),
        (_NUMBER_CLAIM_RE, _THOUSANDS_RE.sub("", answer), f"{hay} {_THOUSANDS_RE.sub('', hay)}"),
    ):
        for m in pattern.finditer(text):
            if not _stated_in(m.group(0).lower(), source, number=pattern is _NUMBER_CLAIM_RE):
                unsupported = True
                break
        if unsupported:
            break
    if not unsupported:
        for m in _ACTION_CLAIM_RE.finditer(answer):
            # Phase 24.8: the read-verb exemption only paraphrases a result that EXISTS; with nothing but
            # progress lines or failures behind it ("Searching for report.csv...") "Found report.csv." is
            # a claim like any other.
            if not retrieval_claims and pool and m.group(1).lower() in _RETRIEVAL_CLAIM_VERBS:
                continue
            stems = _ACTION_CLAIM_STEMS.get(m.group(1).lower(), ())
            if not stems:
                continue
            # Phase 24.2 §5: a negated ("never deleted") or filler ("found this
            # interesting") use of the verb is not a claim that needs grounding at
            # all -- skip it rather than checking it against the evidence.
            if _CLAIM_NEGATED_RE.search(answer[max(0, m.start() - 40):m.start()]):
                continue
            if _CLAIM_FILLER_RE.match(answer[m.end():m.end() + 40]):
                continue
            # Phase 24.2 §2/§8: only the observation's OWN reported outcome (speech
            # + data) can confirm the claim -- never the tool name/args alone (what
            # was asked for, not what happened). Evidence-side wording need not
            # match the answer's verb exactly: each stem is itself a synonym set
            # (e.g. "deleted" is also confirmed by evidence saying "removed").
            # Phase 24.4: ...and only a real RESULT counts -- an attempt line
            # ("Attempting to delete ...") or a success a later same-operation
            # failure overrode grounds nothing (an attempt still grounds "searched").
            candidates = search_pool if m.group(1).lower() == "searched" else pool
            if not any(
                any(stem in _observation_outcome_text(o) for stem in stems)
                for o in candidates
            ):
                unsupported = True
                break
            # Phase 24.9: ...and it must be a result about EACH file the claim names, not just about some file (a
            # "searched" claim only says where FRIDAY looked, so it is not held to its objects).
            if m.group(1).lower() != "searched" and any(
                not any(_grounds_object(o, stems, name) for o in candidates)
                for name in _claimed_objects(answer, m.start(), m.end()) or ()
            ):
                unsupported = True
                break

    if unsupported:
        # Phase 24.4: restate the PRIORITIZED evidence -- the conclusive results
        # (a relevant failure included), best first; never an attempt line or a
        # result a later observation overrode -- falling back to every real speech
        # only when nothing else remains.
        important_ids = {id(o) for o in important}
        shown = dict.fromkeys(
            (o.speech or "").strip()
            for o in prioritize_evidence(goal, observations, state)
            if o.speech and id(o) not in state.superseded and state.kind[id(o)] != "tentative"
            and (o.ok or id(o) in important_ids)
        )
        confirmed = " ".join(shown).strip()[:400] or " ".join(o.speech for o in real if o.speech).strip()[:400]
        tail = f" What the evidence actually confirms: {confirmed}" if confirmed else ""
        return (
            "Part of that isn't something the evidence I gathered actually shows, so I "
            "can't confirm it -- treating it as insufficient rather than guessing."
            + tail
        )

    # Phase 24.3: the answer makes no unsupported claim, but might still silently
    # OMIT an important, goal-relevant result the evidence already produced. Uses
    # `important` (not just `real`) so a real FAILED/negative result ("no such
    # file") -- excluded from `real`, since it can't ground a positive claim -- can
    # still be required to be communicated.
    gaps = _completeness_gaps(goal, answer, important)
    # Phase 24.4: an unresolved success-vs-failure conflict is preserved, not
    # silently resolved -- an answer that took only one side gets the other appended.
    gaps += [g for g in _conflict_gaps(goal, answer, state) if all(g is not x for x in gaps)]
    if not gaps:
        return answer
    out = _completeness_fallback(answer, gaps)
    # Phase 24.10: restating a result can itself change what the checks see (an appended failure gives a
    # neutral answer a negative polarity, which is what exposes a success-vs-failure conflict on the
    # next look; a clause with several unmet numbers reports one per look). Look again, bounded, and
    # append only observations not yet restated -- so grounding is idempotent: a second pass over its own
    # output adds nothing. Still only ever real evidence speech; never a fabricated fact.
    for _ in range(3):
        more = [g for g in _completeness_gaps(goal, out, important) + _conflict_gaps(goal, out, state)
                if all(g is not x for x in gaps)]
        if not more:
            break
        gaps += more
        out = _completeness_fallback(out, more)
    return out


# -- Phase 24.7: final-answer quality normalization ---------------------------
#
# Phases 24.1-24.6 decide WHAT an answer may claim and WHEN a run may stop. This section
# changes neither: it only tidies how an answer that has ALREADY passed `ground_answer`
# reads. Left alone that text can still repeat itself ("Created report.csv successfully.
# Created report.csv successfully." -- two tools reported the same thing), keep a status line
# the real result already replaced ("Attempting to delete report.csv. Deleted report.csv
# successfully.", "Searching... Found report.csv."), or carry a failure about a file the goal
# never mentioned ("Permission denied opening notes.txt." in an answer about report.csv).
#
# The one design rule is REMOVAL ONLY. The output is the input with whole sentences deleted:
# every character of it is a character of the input, in the input's order (checked at run
# time by `_is_subsequence`, falling back to the input if it ever were not). Nothing is
# rewritten, paraphrased, re-worded or inserted, so no number, filename or claim can appear
# that the grounded answer did not already contain, and nothing `ground_answer` rejected can
# come back. The order is fixed by construction: `finalize_answer` grounds first and only then
# normalizes, and `normalize_answer` is never handed an ungrounded claim to launder.
#
# Three rules, each applied only where the text and the evidence prove it is safe:
#   1. a progress / attempt sentence ("Attempting to delete report.csv.", "Searching...") is
#      dropped when a LATER real result in the same answer settles it -- the same action on the
#      same object (24.4's supersession rule, applied to sentences; a result that never mentions
#      what the line was about settles nothing) -- and nothing it names (a filename, a number)
#      would be lost with it. An unresolved attempt is honest about the goal not being finished,
#      so it stays.
#   2. a sentence the answer already says -- identical, or the very same words inside a longer
#      one that adds only filler ("Deleted report.csv." / "I deleted report.csv successfully.")
#      and no negation, hedge, contrast, condition or new detail -- is stated once.
#   3. a sentence that is a verbatim echo of a failing tool result about a file the goal never
#      named, and that nothing else in the run touches, is dropped while the answer still
#      speaks to the goal's own file. A failure about anything the goal names, anything the
#      other evidence connects to it, or one nothing else covers, stays.
# Every stage is re-checked with the same completeness / conflict tests `ground_answer`
# applies (24.3, 24.4): a stage that would leave the answer missing something it acknowledged
# before, or would drop a filename or number the goal itself names, is discarded.

_NORMALIZE_MAX_SENTENCES = 60
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?…])\s+")
_LIST_MARKER_RE = re.compile(r"\(?\d{1,3}[.)]")
# Phase 24.8: a run of symbols ("+", "#", "%", "$", a sign) is a token too -- "-5" is not "5", "50%" is not "$50",
# "C++" is not "C#"; only ordinary punctuation is ignored
_WORD_RE = re.compile(r"\w+(?:[.'’\-]\w+)*|[^\w\s.,;:!?'\"’()\[\]{}…]+")
# Words whose referent lives in the sentences around them: "It has 42 rows." said once per file is two facts, not one
_ANAPHORA = frozenset({"it", "its", "they", "them", "their", "this", "that", "these", "those", "he", "she", "him", "his",
                       "her", "one", "both", "each", "same"})
# A progress line that ANNOUNCES work ("Searching...", "Attempting to delete x.", "Please wait."), as opposed to one that
# merely reports a state ("The backup is in progress.", "a.txt, b.txt, ...")
_ANNOUNCE_RE = re.compile(
    r"^\W*(?:(?:still|now|just|currently)\s+)?[a-z]+ing\b"
    r"|\b(?:attempt(?:ing|ed)|trying to|about to|going to|preparing to|starting to|working on|please wait|one moment)\b",
    re.IGNORECASE,
)
_LEAD_INS = frozenset({"also", "additionally", "and", "then", "next", "finally", "plus"})
# The only words a longer sentence may add around a shorter one for the two to count as one
# statement: a subject pronoun, an auxiliary, an article, a filler adverb. Anything that could
# narrow or shift the claim ("... in Sheet1", "... from Downloads", "... 3 columns") is not here,
# so "report.csv has 42 rows." and "report.csv has 42 rows in Sheet1." are never merged.
_FILLER_WORDS = frozenset({
    "i", "we", "it", "was", "were", "is", "are", "has", "have", "had", "been", "the", "a", "an",
    "successfully", "now", "just", "already", "really", "actually", "done", "ok", "okay",
})
# Words that change WHAT a sentence asserts (contrast, condition, alternative, hedge, order).
# Two sentences only count as the same statement when neither has one the other lacks; the
# negation / failure / hedge lists the earlier phases already keep are reused alongside it.
_QUALIFIER_RE = re.compile(
    r"\b(?:but|however|although|though|yet|despite|whereas|while|unless|if|or|either|maybe|perhaps|"
    r"possibly|likely|might|may|could|would|should|seems?|appears?|apparently|reportedly|"
    r"unverified|unconfirmed|conflict\w*|contradict\w*|mismatch\w*|inconsisten\w*|discrepan\w*|"
    r"instead|only|except|still|first|second|later|earlier|before|after|previously)\b",
    re.IGNORECASE,
)
# A progress line about one of these is settled by ANY later result about the same file
# ("Searching for report.csv..." ... "report.csv contains 42 rows."): the read / search /
# open step evidently finished. A state-changing attempt ("Attempting to delete ...") is only
# ever settled by a later result of that same action.
_READ_ACTION_KEYS = _RETRIEVAL_CLAIM_VERBS | {"opened"}
# What a progress line is ABOUT once the announcing words are taken out: its content words
# minus the progress vocabulary, every action verb (24.2 stems) and any "-ing" announcer
# ("Counting", "Scanning"), and minus the placeholder nouns that name no particular thing ("the
# folder", "the requested file"). "Sending the email to bob..." is about {bob}: only a result
# that mentions bob can settle it -- "Sent email to alice." says nothing about him.
_PROGRESS_VOCAB = frozenset({
    "attempting", "attempted", "trying", "working", "going", "preparing", "starting", "progress",
    "pending", "please", "wait", "moment", "still",
})
_GENERIC_OBJECT_WORDS = frozenset({
    "file", "folder", "directory", "data", "request", "requested", "information", "info", "result",
    "item", "thing", "task", "operation", "content", "document", "page", "message", "window", "app",
    "application",
})
_ACTION_STEMS = tuple({stem for stems in _ACTION_CLAIM_STEMS.values() for stem in stems})


@dataclass(frozen=True)
class _Line:
    """One sentence of answer text dressed as an observation (ok, no step, no data), so the
    observation-level classifiers (`_evidence_kind`, `_is_progress_line`, `_action_keys`,
    `_object_files`) judge it by exactly the rules that judge real evidence."""

    speech: str
    ok: bool = True
    error: str = ""
    step: None = None
    data: None = None


@dataclass(frozen=True)
class _Sentence:
    text: str
    words: "tuple[str, ...]"          # comparison key: lower-cased words, no leading connective
    anchored: bool                    # concrete enough to be worth de-duplicating (3+ words, a file or a number)
    progress: bool                    # `_is_progress_line`: work announced, not done
    announces: bool                   # ...and it really is an announcement (`_ANNOUNCE_RE`), not a reported state
    result: bool                      # a real, concrete, un-hedged statement of an outcome
    failure: bool
    actions: "frozenset[str]"         # 24.2 claim-verb families the sentence talks about
    files: "frozenset[str]"
    numbers: "frozenset[str]"
    qualifiers: "frozenset[str]"
    keywords: "frozenset[str]"        # `_clause_keywords`: the sentence's stemmed content words
    subject: "frozenset[str]"         # what an announcement is about, verbs and progress words removed


def _subject_words(text: str) -> "frozenset[str]":
    return frozenset(
        w for w in _clause_keywords(text)
        if w not in _PROGRESS_VOCAB and w not in _GENERIC_OBJECT_WORDS
        and not w.startswith(_ACTION_STEMS) and not (w.endswith("ing") and len(w) > 4)
    )


def _sentence_spans(text: str) -> "list[tuple[int, int]]":
    """[start, end) of each sentence of `text`, surrounding whitespace excluded. A bare list marker
    ("1.", "2)") is not a sentence of its own: it stays with the item it introduces, so removing a
    repeated item takes its number along instead of stranding a "2."."""
    start, end = len(text) - len(text.lstrip()), len(text.rstrip())
    spans: list[tuple[int, int]] = []
    pos = start
    for m in _SENTENCE_BREAK_RE.finditer(text, start, end):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < end:
        spans.append((pos, end))
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and _LIST_MARKER_RE.fullmatch(text[merged[-1][0]:merged[-1][1]]):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _sentence_words(text: str) -> "tuple[str, ...]":
    """A sentence's comparison key: its lower-cased words, minus a leading connective
    ("Also: X" says the same as "X"). Punctuation and case never matter."""
    words = _WORD_RE.findall((text or "").lower())
    while words and words[0] in _LEAD_INS:
        words = words[1:]
    return tuple(words)


def _qualifiers(text: str) -> "frozenset[str]":
    low = (text or "").lower()
    found = {m.group(0) for rx in (_ANSWER_NEGATIVE_RE, _ASSUMED_RE, _QUALIFIER_RE) for m in rx.finditer(low)}
    found.update(marker for marker in _HEDGE_MARKERS if marker in low)
    return frozenset(found)


def _extra_words(long_words: "tuple[str, ...]", short_words: "tuple[str, ...]") -> "tuple[str, ...] | None":
    """The words `long_words` has around ONE contiguous copy of `short_words`; None when it holds
    no such copy."""
    n, m = len(long_words), len(short_words)
    if m == 0:
        return None
    for i in range(n - m + 1):
        if long_words[i:i + m] == short_words:
            return long_words[:i] + long_words[i + m:]
    return None


def _is_subsequence(small: str, big: str) -> bool:
    """Every character of `small` occurs in `big`, in order -- what "only ever removed text"
    means, checked rather than assumed."""
    rest = iter(big)
    return all(ch in rest for ch in small)


def _analyze_sentence(text: str) -> _Sentence:
    line = _Line(text)
    progress = _is_progress_line(line)
    kind = _evidence_kind(line)
    concrete = bool(_FILENAME_CLAIM_RE.search(text) or re.search(r"\d", text))
    words = _sentence_words(text)
    return _Sentence(
        text=text,
        words=words,
        # worth de-duplicating: says enough on its own (3+ words, or names a file) and leans on nothing around it ("It has ...")
        anchored=(len(words) >= 3 or bool(_FILENAME_CLAIM_RE.search(text))) and not _ANAPHORA.intersection(words),
        progress=progress,
        announces=progress and bool(_ANNOUNCE_RE.search(text)),
        result=not progress and kind != "tentative" and bool(
            concrete or _ACTION_CLAIM_RE.search(text) or _FAILURE_RE.search(text)
            or _SUCCESS_WORD_RE.search(text) or _ANSWER_NEGATIVE_RE.search(text)
        ),
        failure=kind == "failure",
        actions=frozenset(_action_keys(line)),
        files=frozenset(_object_files(line)),
        numbers=frozenset(_NUMBER_TOKEN_RE.findall(text)),
        qualifiers=_qualifiers(text),
        keywords=frozenset(_clause_keywords(text)),
        subject=_subject_words(text),
    )


def _resolves(progress: _Sentence, result: _Sentence) -> bool:
    """Does the later real `result` settle what the `progress` line only announced?"""
    if progress.files and result.files and not (progress.files & result.files):
        return False  # a result about a different file settles nothing about this one
    if not progress.subject <= result.keywords:
        return False  # the line is about something the result never mentions ("... to bob" / "Sent ... to alice")
    if progress.actions & result.actions:
        return True  # the same action ("delete" ... "Deleted", "Searching" ... "Found")
    if not progress.actions and not progress.files:
        return True  # pure filler ("Working on it...", "One moment."): nothing specific to settle
    return bool(progress.files & result.files) and progress.actions <= _READ_ACTION_KEYS


def _drop_resolved_progress(sents: "list[_Sentence]", kept: "list[int]") -> "list[int]":
    dropped: set[int] = set()
    for p in kept:
        if not sents[p].announces:
            continue  # only a line ANNOUNCING work is ever superseded; a reported state ("...is in progress.") is an answer
        rest = [j for j in kept if j != p and j not in dropped]
        if not any(j > p and sents[j].result and _resolves(sents[p], sents[j]) for j in rest):
            continue
        others = " ".join(sents[j].text for j in rest)
        # nothing the progress line names may disappear with it: a filename or number it alone
        # carries is a detail the answer still owes the reader
        if sents[p].files <= _object_files_of_text(others) and sents[p].numbers <= set(_NUMBER_TOKEN_RE.findall(others)):
            dropped.add(p)
    return [i for i in kept if i not in dropped]


def _repeated_in_evidence(observations: "list[Observation]") -> "set[tuple[str, ...]]":
    """Sentences that ONE observation's own speech already says more than once (a log's repeated
    error line, OCR text): that repetition is the tool's content -- "timed out" three times is a
    fact about the log -- not redundancy, so `_drop_restated` leaves it alone."""
    repeated: set[tuple[str, ...]] = set()
    for o in observations:
        speech = o.speech or ""
        keys = [_sentence_words(speech[a:b]) for a, b in _sentence_spans(speech)]
        repeated.update(k for k in keys if keys.count(k) > 1)
    return repeated


def _drop_restated(
    sents: "list[_Sentence]", kept: "list[int]", protected: "set[tuple[str, ...]] | None" = None,
) -> "list[int]":
    protected = protected or set()
    dropped: set[int] = set()
    for x, a in enumerate(kept):
        if a in dropped:
            continue
        for b in kept[x + 1:]:
            if b in dropped:
                continue
            sa, sb = sents[a], sents[b]
            if sa.words in protected or sb.words in protected:
                continue
            if sa.words == sb.words:
                if sa.anchored:
                    dropped.add(b)  # the same sentence again: keep the first
                continue
            short, long_ = (a, b) if len(sa.words) < len(sb.words) else (b, a)
            s, lg = sents[short], sents[long_]
            extra = _extra_words(lg.words, s.words) if s.anchored and s.qualifiers == lg.qualifiers else None
            if extra is not None and all(w in _FILLER_WORDS for w in extra):
                dropped.add(short)  # the longer sentence already says all of it
                if short == a:
                    break
    return [i for i in kept if i not in dropped]


def _drop_unrelated_failures(
    goal: str, sents: "list[_Sentence]", kept: "list[int]", observations: "list[Observation]",
) -> "list[int]":
    goal_files = _object_files_of_text(goal)
    if not goal_files:
        return kept  # a goal that names no file gives nothing to call "unrelated" to
    if classify_mode(goal) is not GoalMode.DIRECT_ACTION:
        return kept  # "why does report.csv fail to load?": a failure about ANOTHER file is exactly the answer
    failing = [
        (o, {_sentence_words((o.speech or "")[a:b]) for a, b in _sentence_spans(o.speech or "")})
        for o in observations
        if (o.speech or "").strip() and (not o.ok or _evidence_kind(o) == "failure")
    ]
    if not failing:
        return kept
    dropped: set[int] = set()
    for i in kept:
        s = sents[i]
        if not s.failure or s.progress or not s.files or s.files & goal_files:
            continue
        echoes = [o for o, keys in failing if s.words in keys]
        if not echoes or any(_object_files(o) & goal_files for o in echoes):
            continue  # not a copy of a failed tool's own words, or that call was aimed at the goal's file
        others = [o for o in observations if all(o is not e for e in echoes)]
        elsewhere = _evidence_haystack(others) + " " + " ".join(
            sents[j].text.lower() for j in kept if j != i and j not in dropped
        )
        if any(f in elsewhere for f in s.files):
            continue  # something else in the run is about this file, so it is not unrelated
        if not any(sents[j].files & goal_files for j in kept if j != i and j not in dropped):
            continue  # the answer must still speak to the goal's own file
        dropped.add(i)
    return [i for i in kept if i not in dropped]


def _rebuild(text: str, spans: "list[tuple[int, int]]", kept: "list[int]") -> str:
    """`text` with only the `kept` sentences: each keeps the separator that originally followed
    it, so line breaks between the survivors are as they were."""
    parts: list[str] = []
    for n, i in enumerate(kept):
        if n:
            prev = kept[n - 1]
            parts.append(text[spans[prev][1]:spans[prev + 1][0]])
        parts.append(text[spans[i][0]:spans[i][1]])
    return "".join(parts)


def normalize_answer(goal: str, answer: str, observations: "list[Observation]") -> str:
    """Phase 24.7 -- deterministic, removal-only tidying of an answer that has ALREADY been
    grounded (`ground_answer` / `finalize_answer`). See the section comment above for the three
    rules. Returns `answer` itself (unchanged) when there is nothing safe to remove, when the
    result would be empty, when a removal would cost the answer something the completeness /
    conflict checks (24.3, 24.4) saw in it, or on any unexpected error: this is cosmetic, so it
    must never be the reason an answer is lost. Never adds a character, so never adds a fact."""
    if not answer or not answer.strip() or not observations:
        return answer
    try:
        spans = _sentence_spans(answer)
        if len(spans) < 2 or len(spans) > _NORMALIZE_MAX_SENTENCES:
            return answer
        sents = [_analyze_sentence(answer[a:b]) for a, b in spans]
        state = _resolve_evidence(observations)
        important = _important_observations(goal, observations, state)

        def gap_ids(text: str) -> "set[int]":
            return (
                {id(o) for o in _completeness_gaps(goal, text, important)}
                | {id(o) for o in _conflict_gaps(goal, text, state)}
            )

        before = gap_ids(answer)
        low_answer = answer.lower()
        # details the request itself names -- explicitly requested filenames and numbers
        named = _object_files_of_text(goal) | set(_NUMBER_TOKEN_RE.findall(goal or ""))

        def acceptable(text: str) -> bool:
            if not text.strip() or not _is_subsequence(text, answer):
                return False
            low = text.lower()
            if any(d in low_answer and d not in low for d in named):
                return False
            return gap_ids(text) <= before

        tool_repeats = _repeated_in_evidence(observations)
        stages = (
            lambda k: _drop_resolved_progress(sents, k),
            lambda k: _drop_restated(sents, k, tool_repeats),
            lambda k: _drop_unrelated_failures(goal, sents, k, observations),
        )
        kept = list(range(len(sents)))
        for _ in range(3):  # a removal can enable another (bounded; idempotent at the fixpoint)
            changed = False
            for stage in stages:
                trial = stage(kept)
                if trial != kept and acceptable(_rebuild(answer, spans, trial)):
                    kept, changed = trial, True
            if not changed:
                break
        return answer if len(kept) == len(sents) else _rebuild(answer, spans, kept)
    except Exception:  # cosmetic post-processing: never the reason a grounded answer is lost
        return answer


def finalize_answer(
    goal: str, answer: str, observations: "list[Observation]", *, retrieval_claims: bool = True,
) -> str:
    """The whole final-answer pipeline after composition, in its one safe order: `ground_answer`
    (every claim, completeness and conflict check -- Phases 24.1-24.6, unchanged), THEN
    `normalize_answer` (Phase 24.7's removal-only tidying). Both final-answer routes call this
    -- the Phase 23 composer and the planner's own `done` summary -- so grounding can never run
    on normalized text, and normalization never sees a claim grounding has not passed. A
    fail-closed replacement or an "Also: ..." completion from `ground_answer` is normalized like
    any other text: its safety wording has nothing to remove."""
    return normalize_answer(
        goal, ground_answer(goal, answer, observations, retrieval_claims=retrieval_claims), observations,
    )
