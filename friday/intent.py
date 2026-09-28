"""Phase 20.0 — intent -> action alignment.

Phase 19 made the planner's *decisions* well-formed. What it exposed is a
different failure: a well-formed decision can pick a tool that has nothing to
do with what the user asked. `friday.permissions` answers "is this tool call
allowed at all, and does a human have to confirm it?" by RISK TIER — and L1
("reversible write") is auto-approved, so `ui.click("Close")` sails through
for the goal "inspect my project". Permission tier is not intent
authorization.

    USER INTENT -> ALLOWED ACTION CLASSES -> TOOL DECISION
                -> existing permission / confirmation -> execution

This module is the small deterministic piece in the middle. It adds nothing to
the permission system and replaces nothing in it; both checks must pass.

  * `ActionClass` — the smallest vocabulary that separates "looks at things"
    from "changes things". Every skill gets one, declared next to its tier on
    the `@skill(action=...)` decorator (the registry stays the single source of
    truth); a handful of generic tools (click, key press, git) classify PER CALL
    via an `ActionRule`, because the same tool is a harmless tab switch or a
    "Delete" button depending on the target.
  * `GoalScope` — which classes the user's OWN request authorizes, derived from
    the goal text alone with a small verb lexicon (`derive_scope`). Nothing else
    can widen it: not remembered context, not past experience, not a proactive
    suggestion, not the planner's own reasoning. Read/observe is always in scope.
  * `check_alignment` — one pure function the orchestrator calls after the
    decision is parsed and validated, before the executor sees it.

Deliberately keyword-based (same philosophy as `friday.risk` and
`friday.intelligence.discovery.classify_mode`): transparent, auditable,
zero-cost, and biased toward the fail-safe side — an over-strict scope costs the
planner one bounded replan and, at worst, an honest "that isn't what you asked
for"; an over-loose one would let a read-only goal act.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from friday.risk import is_consequential


class ActionClass(str, Enum):
    READ = "read"                    # returns stored/derived information
    OBSERVE = "observe"              # looks at the live desktop/browser/system
    NAVIGATE = "navigate"            # goes somewhere (a URL, a tab, back/forward)
    OPEN = "open"                    # launches/opens an app, project, file
    FOCUS = "focus"                  # switches/arranges windows and desktops
    MODIFY = "modify"                # types, writes, saves, configures, runs code
    DELETE = "delete"                # removes, closes, terminates, forgets
    COMMUNICATE = "communicate"      # sends/composes a message to another party
    TRANSACT = "transact"            # spends money / makes a commitment
    SYSTEM_CHANGE = "system_change"  # settings, power state, network, credentials


A = ActionClass
PASSIVE: frozenset[ActionClass] = frozenset({A.READ, A.OBSERVE})
# Low-impact, reversible "get me there" actions.
LIGHT: frozenset[ActionClass] = frozenset({A.OPEN, A.FOCUS, A.NAVIGATE})

_PHRASE: dict[ActionClass, str] = {
    A.READ: "read information",
    A.OBSERVE: "observe the screen",
    A.NAVIGATE: "navigate somewhere",
    A.OPEN: "open or launch something",
    A.FOCUS: "switch or rearrange windows",
    A.MODIFY: "change something (click, type, write, save or configure)",
    A.DELETE: "delete, close or terminate something",
    A.COMMUNICATE: "send or compose a message to someone",
    A.TRANSACT: "spend money or commit to something",
    A.SYSTEM_CHANGE: "change a system setting or power state",
}


# -- per-skill classification ------------------------------------------------------


@dataclass(frozen=True)
class ActionRule:
    """A per-CALL classifier for a tool whose action class depends on its
    arguments (a click on "Back" vs "Delete"). `possible` is every class it can
    return — used to decide whether the tool is worth offering the planner at
    all for a given goal. `target_keys` names the argument(s) holding the
    UI target, for the explicit-target check in `check_alignment`."""

    classify: Callable[[dict[str, Any]], ActionClass]
    possible: frozenset[ActionClass]
    target_keys: tuple[str, ...] = ()
    label: str = "varies"  # shown to the planner next to the tier, e.g. "[L1 click]"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


# Labels that merely move around. Anything not recognised is assumed able to
# change state (Save / OK / Apply / Submit ...) — unknown clicks fail closed.
_NAV_LABEL_WORDS = frozenset({
    "back", "forward", "next", "previous", "prev", "home", "tab", "tabs", "more", "details",
    "expand", "collapse", "open", "view", "show", "page", "up", "down", "first", "last",
    "top", "bottom", "overview", "docs", "about", "menu", "link", "read", "continue", "scroll",
})
_FOCUS_LABEL_WORDS = frozenset({"minimize", "maximize", "restore", "snap"})
_CLOSE_LABEL_WORDS = frozenset({"close", "exit", "quit", "discard", "terminate", "kill", "end"})

# risk.py's consequential phrases, bucketed by what they would do.
_COMMUNICATE_PHRASES = ("send", "post", "publish", "share", "tweet", "reply", "forward")
_TRANSACT_PHRASES = ("buy", "purchase", "pay", "checkout", "place order", "donate", "subscribe", "upgrade plan")
_SYSTEM_PHRASES = ("change password", "reset password", "revoke")


def classify_click(args: dict[str, Any]) -> ActionClass:
    label = str(args.get("label") or args.get("target") or args.get("query") or "").strip().lower()
    if not label:
        return A.MODIFY
    if is_consequential(label):
        if any(p in label for p in _COMMUNICATE_PHRASES):
            return A.COMMUNICATE
        if any(p in label for p in _TRANSACT_PHRASES):
            return A.TRANSACT
        if any(p in label for p in _SYSTEM_PHRASES):
            return A.SYSTEM_CHANGE
        return A.DELETE
    words = _words(label)
    if words & _CLOSE_LABEL_WORDS:
        return A.DELETE  # "Close" on a window/app: a state-changing desktop action
    if words & _FOCUS_LABEL_WORDS:
        return A.FOCUS
    if words <= _NAV_LABEL_WORDS or (words & _NAV_LABEL_WORDS and len(words) <= 2):
        return A.NAVIGATE
    return A.MODIFY


_NAV_KEYS = frozenset({
    "tab", "shift+tab", "up", "down", "left", "right", "pageup", "pagedown", "page up", "page down",
    "home", "end", "arrowup", "arrowdown", "arrowleft", "arrowright", "alt+left", "alt+right",
    "ctrl+tab", "ctrl+shift+tab", "control+tab", "ctrl+pageup", "ctrl+pagedown",
})
_FOCUS_KEYS = frozenset({"alt+tab", "win+tab", "win+d", "win+m", "win+up", "win+down", "win+left", "win+right"})
_CLOSE_KEYS = frozenset({"ctrl+w", "control+w", "alt+f4", "ctrl+f4", "ctrl+shift+w", "delete", "del", "backspace", "shift+delete"})


def classify_key(args: dict[str, Any]) -> ActionClass:
    key = str(args.get("key") or args.get("combo") or "").strip().lower().replace(" + ", "+")
    if key in _NAV_KEYS:
        return A.NAVIGATE
    if key in _FOCUS_KEYS:
        return A.FOCUS
    if key in _CLOSE_KEYS:
        return A.DELETE
    return A.MODIFY  # Enter, Delete, ctrl+s, ctrl+v, a bare letter ... all can change state


_GIT_READ = frozenset({"status", "log", "diff", "show", "remote", "blame"})


def classify_git(args: dict[str, Any]) -> ActionClass:
    parts = str(args.get("subcommand") or "status").split()
    first = parts[0] if parts else "status"
    if first in _GIT_READ or (first == "branch" and all(p.startswith("-") for p in parts[1:])):
        return A.READ
    return A.MODIFY


CLICK_RULE = ActionRule(
    classify_click,
    frozenset({A.NAVIGATE, A.FOCUS, A.MODIFY, A.DELETE, A.COMMUNICATE, A.TRANSACT, A.SYSTEM_CHANGE}),
    target_keys=("label", "target", "query"), label="click",
)
KEY_RULE = ActionRule(
    classify_key, frozenset({A.NAVIGATE, A.FOCUS, A.MODIFY, A.DELETE}), target_keys=("key", "combo"), label="keypress",
)
GIT_RULE = ActionRule(classify_git, frozenset({A.READ, A.MODIFY}), label="git")


def action_label(tool: str, *, tier_hint: str = "") -> str:
    """A short display word for a tool's action class ("open", "click", ...)."""
    res = _resolve(tool, tier_hint)
    return res.label if isinstance(res, ActionRule) else res.value

_OBSERVE_PREFIXES = ("screen.", "ui.", "system.", "process.", "network.")
_OBSERVE_NAMES = frozenset({"browser.read", "browser.inspect", "apps.list", "clipboard.read"})

# Only for a tool name the registry has never heard of (tests, a caller's own
# tool world): the verb it ends in. Unknown verbs classify as MODIFY — fail closed.
_SUFFIX_CLASS: dict[str, ActionClass] = {
    **dict.fromkeys(("read", "get", "list", "search", "find", "inspect", "status", "info", "ask", "recall",
                     "history", "calculate", "fetch", "now"), A.READ),
    **dict.fromkeys(("observe", "capture", "active_window"), A.OBSERVE),
    **dict.fromkeys(("open", "launch", "reveal", "start"), A.OPEN),
    **dict.fromkeys(("focus", "switch", "minimize", "maximize", "restore", "snap"), A.FOCUS),
    **dict.fromkeys(("delete", "remove", "kill", "close", "forget", "terminate"), A.DELETE),
    **dict.fromkeys(("send", "compose", "reply", "post"), A.COMMUNICATE),
    **dict.fromkeys(("buy", "pay", "purchase", "checkout"), A.TRANSACT),
    **dict.fromkeys(("shutdown", "lock", "restart"), A.SYSTEM_CHANGE),
}


def _default_class(name: str, tier: str) -> ActionClass:
    if tier == "L0":
        return A.OBSERVE if name.startswith(_OBSERVE_PREFIXES) or name in _OBSERVE_NAMES else A.READ
    # A non-read-only skill that never declared an action: the most consequential
    # class its tier can plausibly be, so only a goal that names it authorizes it.
    return {"L1": A.MODIFY, "L2": A.DELETE, "L3": A.SYSTEM_CHANGE}.get(tier, A.MODIFY)


def _resolve(tool: str, tier_hint: str = "") -> ActionClass | ActionRule:
    from friday.registry import REGISTRY

    sk = REGISTRY.get(tool)
    if sk is not None:
        declared = getattr(sk, "action", None)
        if isinstance(declared, ActionRule):
            return declared
        if declared:
            return ActionClass(declared)
        return _default_class(tool, sk.tier)
    # Not a registered skill: use the tier the caller advertised, else the verb.
    if tier_hint == "L0":
        return _default_class(tool, "L0")
    return _SUFFIX_CLASS.get(tool.rsplit(".", 1)[-1].lower(), A.MODIFY)


def classify_call(tool: str, args: dict[str, Any] | None = None, *, tier_hint: str = "") -> ActionClass:
    """The action class of THIS call. Registry-declared, or per-call for a rule."""
    res = _resolve(tool, tier_hint)
    return res.classify(args or {}) if isinstance(res, ActionRule) else res


def possible_classes(tool: str, *, tier_hint: str = "") -> frozenset[ActionClass]:
    res = _resolve(tool, tier_hint)
    return res.possible if isinstance(res, ActionRule) else frozenset({res})


def is_passive_call(tool: str, args: dict[str, Any] | None = None, *, tier_hint: str = "") -> bool:
    return classify_call(tool, args, tier_hint=tier_hint) in PASSIVE


# -- goal scope: which classes did the USER ask for? -----------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+(?:['\-][a-z0-9]+)*")
_SENT_SPLIT = re.compile(r"[.?!;:\n]+|\s[—–-]\s|[—–]")
_CLAUSE_SPLIT = re.compile(r",|\band then\b|\bthen\b|\band\b|\bafter that\b|\bonce you\b")

_NEG = frozenset({"not", "don't", "dont", "never", "without", "no", "can't", "cant", "won't"})
_QUESTION = frozenset({
    "what", "what's", "whats", "why", "how", "which", "who", "whom", "whose", "where", "when",
    "is", "are", "does", "did", "was", "were",
})
# "Should I delete this?" / "can we close it?" ask for advice or permission. "Can you
# close it?" (second person) is a polite request and is deliberately NOT here.
_MODAL_Q = frozenset({"can", "could", "may", "should", "shall", "will", "would", "do", "does", "did"})
_MODAL_SUBJ = frozenset({"i", "we", "it", "they", "he", "she"})
_DET = frozenset({"the", "a", "an", "my", "your", "his", "her", "its", "our", "their", "this", "that",
                  "these", "those", "any", "each", "every", "some", "of", "no"})
_CONJ = frozenset({"please", "and", "then", "also", "to", "just", "now", "first", "next", "finally", "you",
                   "me", "lets", "let's", "can", "could", "would", "will", "should", "hey", "jarvis",
                   "friday", "kindly", "ahead"})
_ING_OK = frozenset({"help", "try", "start", "begin", "keep", "by"})

# Verb groups. Read/observe verbs grant nothing (reading is always in scope);
# everything else grants the classes in _GRANTS.
_G_PASSIVE, _G_PRESENT, _G_OPEN, _G_FOCUS = "passive", "present", "open", "focus"
_G_MODIFY, _G_CLICK, _G_DELETE, _G_COMM, _G_TRANSACT, _G_SYSTEM = (
    "modify", "click", "delete", "communicate", "transact", "system")

_WORDS: dict[str, str] = {}


def _add(group: str, text: str) -> None:
    for w in text.split():
        _WORDS.setdefault(w, group)


_add(_G_PASSIVE,
     "inspect read summarize summarise analyze analyse describe explain list diagnose investigate identify "
     "examine compare count calculate find locate search lookup google verify confirm validate audit scan "
     "measure estimate translate define recall report give get fetch ask determine figure discover see")
_add(_G_PRESENT, "check view show review watch monitor display look")
_add(_G_OPEN, "open launch start navigate browse visit load switch focus resume continue")
_add(_G_FOCUS, "minimize minimise maximize maximise snap restore hide")
_add(_G_MODIFY,
     "fix repair resolve solve correct change edit modify update write create add save type enter fill set "
     "make install run execute rename move copy paste download remember note schedule remind toggle enable "
     "disable turn improve prepare handle clean sort organize organise build generate apply patch refactor "
     "append insert replace upgrade configure adjust tweak rewrite reset put store index undo revert record "
     "test raise lower increase decrease reduce boost")
_add(_G_CLICK, "click press tap select hit choose")
_add(_G_DELETE, "delete remove erase wipe clear trash uninstall kill close quit exit terminate forget discard drop purge")
_add(_G_COMM, "send message text email mail whatsapp reply post tweet share forward dm call publish ping")
_add(_G_TRANSACT, "buy purchase pay order checkout donate subscribe book")
_add(_G_SYSTEM, "mute unmute louder quieter dim brighten lock shutdown restart reboot hibernate")

_PHRASES: dict[tuple[str, ...], str] = {
    ("look", "up"): _G_PASSIVE, ("look", "into"): _G_PASSIVE, ("look", "for"): _G_PASSIVE,
    ("figure", "out"): _G_PASSIVE, ("find", "out"): _G_PASSIVE, ("tell", "me"): _G_PASSIVE,
    ("tell", "us"): _G_PASSIVE, ("go", "through"): _G_PRESENT, ("take", "a", "look"): _G_PRESENT,
    ("have", "a", "look"): _G_PRESENT,
    ("go", "to"): _G_OPEN, ("bring", "up"): _G_OPEN, ("pull", "up"): _G_OPEN, ("take", "me", "to"): _G_OPEN,
    ("head", "to"): _G_OPEN, ("jump", "to"): _G_OPEN, ("switch", "to"): _G_OPEN,
    ("work", "on"): _G_MODIFY, ("clean", "up"): _G_MODIFY, ("sort", "out"): _G_MODIFY,
    ("set", "up"): _G_MODIFY, ("get", "ready"): _G_MODIFY, ("turn", "up"): _G_MODIFY,
    ("turn", "down"): _G_MODIFY, ("turn", "on"): _G_MODIFY, ("turn", "off"): _G_MODIFY,
    ("get", "rid", "of"): _G_DELETE, ("end", "task"): _G_DELETE,
    ("place", "an", "order"): _G_TRANSACT,
    ("shut", "down"): _G_SYSTEM, ("log", "out"): _G_SYSTEM, ("sign", "out"): _G_SYSTEM,
    ("log", "off"): _G_SYSTEM, ("power", "off"): _G_SYSTEM,
}

# Words that are just as often nouns ("the note", "a set", "read text"): only a
# verb in imperative/infinitive position ("Close it", "then send", "to save").
_STRICT = frozenset(
    "text message mail email post share reply forward call note order book set run clear close start change "
    "update save copy drop type press turn sort handle record test dim lock ping hit choose select".split())
_CONSEQUENTIAL_GROUPS = frozenset({_G_MODIFY, _G_CLICK, _G_DELETE, _G_COMM, _G_TRANSACT, _G_SYSTEM})

_SYSTEM_NOUNS = frozenset({"volume", "brightness", "wifi", "wi-fi", "bluetooth", "airplane", "power", "night"})

_GRANTS: dict[str, frozenset[ActionClass]] = {
    _G_PASSIVE: frozenset(),
    _G_PRESENT: frozenset(),
    _G_OPEN: LIGHT,
    _G_FOCUS: frozenset({A.FOCUS}),
    _G_MODIFY: frozenset({A.MODIFY}),
    _G_CLICK: frozenset({A.MODIFY, A.NAVIGATE, A.FOCUS}),
    _G_DELETE: frozenset({A.DELETE}),
    _G_COMM: frozenset({A.COMMUNICATE}),
    _G_TRANSACT: frozenset({A.TRANSACT}),
    _G_SYSTEM: frozenset({A.SYSTEM_CHANGE}),
}


@dataclass(frozen=True)
class GoalScope:
    """What the user's own request authorizes. `allowed` is authorized outright;
    `supporting` (open/focus/navigate) is authorized only for a target that is
    demonstrably about the goal (see `_target_relevant`). READ and OBSERVE are
    always in scope and are not listed."""

    goal: str
    allowed: frozenset[ActionClass]
    supporting: frozenset[ActionClass]
    intents: tuple[str, ...]
    basis: str  # verbs | unspecified | empty | clamped
    explicit_click: bool = False

    @property
    def read_only(self) -> bool:
        return not self.allowed and not self.supporting

    def describe(self) -> str:
        if self.read_only:
            return "reading and observing only"
        parts = sorted(c.value.replace("_", " ") for c in self.allowed)
        sup = sorted(c.value for c in self.supporting)
        text = "reading/observing" + ("; " + ", ".join(parts) if parts else "")
        if sup:
            text += f"; {'/'.join(sup)} only for something the goal itself names"
        return text

    def prompt_line(self) -> str:
        if self.read_only:
            return (
                "The user only asked you to look at, read or inspect something: use only tools that "
                "read or observe. Do not click, type, open, write, delete, send or change anything."
            )
        return (
            f"The user's request authorizes only these kinds of actions: {self.describe()}. "
            "Do not use a tool that does anything else."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal, "allowed": sorted(c.value for c in self.allowed),
            "supporting": sorted(c.value for c in self.supporting), "intents": list(self.intents),
            "basis": self.basis, "explicit_click": self.explicit_click, "read_only": self.read_only,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "GoalScope | None":
        try:
            return GoalScope(
                goal=str(d.get("goal", "")),
                allowed=frozenset(ActionClass(x) for x in d.get("allowed", [])),
                supporting=frozenset(ActionClass(x) for x in d.get("supporting", [])),
                intents=tuple(str(x) for x in d.get("intents", [])),
                basis=str(d.get("basis", "verbs")), explicit_click=bool(d.get("explicit_click", False)),
            )
        except (ValueError, TypeError):
            return None


def _lookup(toks: list[str], i: int, ignored: set[int]) -> tuple[str, int, str] | None:
    """(group, tokens consumed, first word) for a verb starting at toks[i], applying
    the position rules that keep nouns from being read as commands."""
    if i in ignored:
        return None
    prev = toks[i - 1] if i > 0 else ""
    word = toks[i]
    group, span = None, 1
    for n in (3, 2):
        if i + n <= len(toks) and tuple(toks[i:i + n]) in _PHRASES:
            group, span = _PHRASES[tuple(toks[i:i + n])], n
            break
    if group is None:
        if word == "tell":  # "tell Rahul ..." is a message; "tell me ..." was a phrase above
            group = _G_COMM
        else:
            group = _WORDS.get(word)
        if group is None:
            # An imperative is a base form: "why isn't it starting" / "it was deleted"
            # describe, they don't ask. Inflections therefore only count for the
            # read-type groups (which grant nothing / only supporting access), plus
            # a gerund right after a helper ("help fixing my app").
            for suf, n_cut in (("ing", 3), ("ed", 2), ("es", 2), ("s", 1)):
                if word.endswith(suf) and len(word) - n_cut >= 3:
                    stem = word[:-n_cut]
                    stem = stem if stem in _WORDS else (stem + "e" if stem + "e" in _WORDS else stem)
                    g = _WORDS.get(stem)
                    if g in (_G_PASSIVE, _G_PRESENT):
                        group = g
                        break
                    if g in _CONSEQUENTIAL_GROUPS and suf == "ing" and prev in _ING_OK:
                        group, word = g, stem
                        break
            if group is None:
                return None
    if group != _G_PASSIVE and prev in _DET:
        return None  # "the delete button", "my email", "a set of"
    if group in _CONSEQUENTIAL_GROUPS or group == _G_OPEN:
        if word in _STRICT and not (i == 0 or prev in _CONJ):
            return None
    return group, span, word


def derive_scope(goal: str, mode: str = "", wants_mutation: bool | None = None) -> GoalScope:
    """The action classes the user's own goal text authorizes. Pure and
    deterministic; `goal` is the ONLY thing consulted for authority (see the module
    docstring). `mode`/`wants_mutation` can only NARROW the result: a report-only
    goal (diagnostic / investigative / information-seeking, with no explicit ask
    to fix) is clamped to read/observe whatever verbs appear."""
    text = (goal or "").strip().lower()
    if not text:
        return GoalScope(goal or "", frozenset(), frozenset(), (), "empty")

    groups: list[str] = []
    explicit_click = False
    modifier = False
    system_noun = False
    statements = 0  # clauses that are not questions
    for sentence in _SENT_SPLIT.split(text):
        for sub in _CLAUSE_SPLIT.split(sentence):
            toks = _WORD_RE.findall(sub)
            if not toks:
                continue
            if toks[0] in _QUESTION or (len(toks) > 1 and toks[0] in _MODAL_Q and toks[1] in _MODAL_SUBJ):
                continue  # a question asks for an answer, never for an action
            statements += 1
            ignored: set[int] = set()
            for i, t in enumerate(toks):
                if t in _NEG:
                    ignored.update({i + 1, i + 2})
            if _SYSTEM_NOUNS & set(toks):
                system_noun = True
            i = 0
            while i < len(toks):
                hit = _lookup(toks, i, ignored)
                if hit is None:
                    i += 1
                    continue
                group, span, _word = hit
                groups.append(group)
                if group == _G_CLICK:
                    explicit_click = True
                if group == _G_MODIFY:
                    modifier = True
                i += span

    allowed: set[ActionClass] = set()
    for g in groups:
        allowed |= _GRANTS[g]
    if system_noun and modifier:
        allowed.add(A.SYSTEM_CHANGE)

    acting = [g for g in groups if g not in (_G_PASSIVE, _G_PRESENT)]
    if acting or _G_PRESENT in groups:
        supporting = set(LIGHT) - allowed
        basis = "verbs"
    elif groups or statements == 0:  # passive verbs, or nothing but questions: strictly read-only
        supporting, basis = set(), "verbs"
    else:
        # A statement with no recognised verb ("Chrome", "my FRIDAY project"): the
        # low-impact reversible actions, never anything consequential.
        allowed |= LIGHT
        supporting, basis = set(), "unspecified"

    if mode in ("diagnostic", "investigative", "information_seeking") and wants_mutation is False:
        allowed, supporting, explicit_click, basis = set(), set(), False, "clamped"

    return GoalScope(
        goal=goal.strip(), allowed=frozenset(allowed), supporting=frozenset(supporting),
        intents=tuple(dict.fromkeys(groups)), basis=basis, explicit_click=explicit_click,
    )


# -- Phase 21.0: explicit scope expansion ("yes, fix it") ------------------------------
#
# Phase 20's known limitation: after a read-only goal ("inspect my project") the user's
# natural "yes, fix it" was a NEW goal that had to stand on its own words. This is the
# one narrow way a follow-up may widen the PREVIOUS goal's scope, and it is as strict as
# `derive_scope` itself: only the follow-up's own literal text is consulted (no
# context / experience / proactive / model parameter — a test pins the signature), it
# must be a short directive that names a consequential verb AND points back at
# something already discussed ("it", "that", "them", "the issues"), and the result is
# only ever a wider `GoalScope`: alignment still gates every call, then the existing
# permission / confirmation pipeline runs unchanged. A bare "yes" grants nothing.

ACTING: frozenset[ActionClass] = frozenset({A.MODIFY, A.DELETE, A.COMMUNICATE, A.TRANSACT, A.SYSTEM_CHANGE})
_ANAPHORA = frozenset({
    "it", "that", "this", "them", "those", "these", "everything", "all", "things", "stuff",
    "issue", "issues", "problem", "problems", "error", "errors", "bug", "bugs",
})
_AFFIRM_LEAD = re.compile(
    r"^\s*(?:(?:yes|yeah|yep|yup|sure|ok|okay|alright|please|go ahead|do it|absolutely)\b[\s,.!:;-]*)+", re.I,
)
_MAX_EXPANSION_WORDS = 12


def derive_expansion(text: str, prior: GoalScope | None) -> GoalScope | None:
    """The `prior` goal's scope widened by the user's OWN follow-up `text`, or None if
    `text` is not an explicit, anaphoric directive to act ("yes, fix it", "go ahead
    and fix that", "please delete them"). Pure and deterministic. Only `text` is read
    for authority — never context, remembered entities, past experience, a proactive
    suggestion or any model output; a question ("should I fix it?"), a negation
    ("don't fix it") or a bare affirmation grants nothing."""
    if prior is None:
        return None
    if asks_for_undo(text):
        # Phase 22.0: "undo that" / "revert it" name a modify verb and an anaphor, but they ask
        # to REVERSE something that already happened — not to act on the findings just reported.
        # They belong to `meta.undo`; consuming them here widened the goal's scope instead.
        return None
    body = _AFFIRM_LEAD.sub("", (text or "").strip())
    words = _WORD_RE.findall(body.lower())
    if not words or len(words) > _MAX_EXPANSION_WORDS or not (set(words) & _ANAPHORA):
        return None
    new = derive_scope(body)
    widened = new.allowed & ACTING
    if not widened:
        return None
    allowed = prior.allowed | widened
    return GoalScope(
        goal=f"{prior.goal}. {(text or '').strip()}",
        allowed=frozenset(allowed),
        supporting=frozenset(LIGHT - allowed),
        intents=tuple(dict.fromkeys(prior.intents + new.intents)),
        basis="expanded",
        explicit_click=prior.explicit_click or new.explicit_click,
    )


# -- Phase 21.0: incremental ("read more") reads ---------------------------------------

_CONTINUATION = tuple(re.compile(p, re.I) for p in (
    r"\bread (?:some )?more\b", r"\bshow (?:me )?(?:some )?more\b", r"\bmore of (?:it|this|that)\b",
    r"\bcontinue(?: reading)?\b", r"\bkeep (?:reading|going)\b", r"\bgo on\b",
    r"\bnext (?:page|part|section|chunk|bit)\b", r"\bthe rest\b",
    r"^\W*(?:more|next)\W*$",
))
_PAGE_PARAMS = ("offset", "page")


def is_continuation_request(text: str) -> bool:
    """Do the user's own words explicitly ask for MORE of what was already read
    ("read more", "continue", "next page", "show the rest")? Deterministic, from the
    goal text only. False for an ordinary "read the file" — a re-read is not a
    continuation, and this flag alone never runs anything: it only lets a
    pagination-capable READ advance its own cursor (see `next_page_args`)."""
    body = (text or "").strip()
    return bool(body) and any(p.search(body) for p in _CONTINUATION)


def pagination_param(tool: str) -> str | None:
    """The registered cursor argument of a tool ('offset' / 'page'), else None.
    Read straight from the registry, so a tool paginates only if it declared it."""
    from friday.registry import REGISTRY

    sk = REGISTRY.get(tool)
    if sk is None:
        return None
    names = {p.name for p in sk.params}
    return next((p for p in _PAGE_PARAMS if p in names), None)


def next_page_args(tool: str, args: dict[str, Any] | None, prior_data: dict[str, Any] | None) -> dict[str, Any] | None:
    """`args` with the tool's page cursor advanced to where the previous identical call
    said the next part starts (`data["next_offset"]` / `data["next_page"]`), or None
    when it did not report one — nothing more to read, so the repeat guard's block
    stands. Never guesses a position the tool did not itself report, only ever moves
    a READ tool's cursor (a state-changing tool never gets here), and never changes
    what is being read."""
    param = pagination_param(tool)
    if param is None or is_passive_call(tool, args) is False:
        return None
    nxt = (prior_data or {}).get(f"next_{param}")
    if nxt is None or isinstance(nxt, bool):
        return None
    new_args = dict(args or {})
    if new_args.get(param) == nxt:
        return None
    new_args[param] = nxt
    return new_args


# -- relevance of a supporting (open/focus/navigate) target ------------------------

_STOP = frozenset({
    "the", "a", "an", "my", "your", "please", "can", "you", "and", "then", "for", "with", "this",
    "that", "it", "me", "to", "of", "in", "on", "at", "is", "are", "i", "hey", "jarvis", "friday",
}) | frozenset(_WORDS)

# What a tool is ABOUT, keyed by a segment of its dotted name ("system.volume.set" ->
# volume). A goal that never mentions the domain (or an alias) did not ask for it.
_DOMAIN_WORDS: dict[str, frozenset[str]] = {
    "project": frozenset({"project", "projects", "repo", "repository", "codebase"}),
    "apps": frozenset({"app", "apps", "application", "program"}),
    "files": frozenset({"file", "files", "folder", "folders", "document", "documents"}),
    "browser": frozenset({"browser", "page", "tab", "site", "website"}),
    "web": frozenset({"web", "website", "site", "page", "url", "link", "internet"}),
    "window": frozenset({"window", "windows"}),
    "desktop": frozenset({"desktop"}),
    "whatsapp": frozenset({"whatsapp", "chat", "message", "messages", "send", "text", "tell", "dm", "ping", "reply", "forward", "share"}),
    "notes": frozenset({"note", "notes"}),
    "volume": frozenset({"volume", "mute", "unmute", "louder", "quieter", "sound", "audio"}),
    "brightness": frozenset({"brightness", "dim", "dimmer", "brighten", "brighter"}),
    "wifi": frozenset({"wifi", "wi-fi", "internet", "network"}),
    "lock": frozenset({"lock"}),
    "shutdown": frozenset({"shutdown", "shut", "restart", "reboot", "hibernate", "power", "off", "log", "sign"}),
    "power": frozenset({"power", "plan", "performance", "battery"}),
    "process": frozenset({"process", "task", "program"}),
    "memory": frozenset({"memory", "remember"}),
    "schedule": frozenset({"schedule", "job", "task", "reminder"}),
    "knowledge": frozenset({"knowledge", "index", "document", "documents"}),
}
_GENERIC_SEGMENTS = frozenset({"system", "network", "set", "up", "down", "toggle", "get", "new", "plan"})


def _domain_words(tool: str) -> set[str]:
    words: set[str] = set()
    for seg in re.split(r"[._]", tool.lower()):
        if seg and seg not in _GENERIC_SEGMENTS:
            words |= _DOMAIN_WORDS.get(seg, frozenset({seg, seg.rstrip("s")}))
    return words


def _known_domain(tool: str) -> set[str] | None:
    """The alias words of a tool whose domain is in `_DOMAIN_WORDS`, else None
    (a domain we can't name is never grounds to reject)."""
    known: set[str] = set()
    for seg in re.split(r"[._]", tool.lower()):
        if seg in _DOMAIN_WORDS and seg not in _GENERIC_SEGMENTS:
            known |= _DOMAIN_WORDS[seg]
    return known or None


def _target_relevant(goal: str, tool: str, args: dict[str, Any]) -> bool:
    """Is this call plainly ABOUT the goal? True when the tool's own domain word
    (project.open <- "my project"; system.volume.set <- "turn the volume down") or a
    distinctive word of its target ("FRIDAY" in the args <- "check my FRIDAY project")
    appears in the goal."""
    goal_words = _words(goal)
    if goal_words & _domain_words(tool):
        return True
    target_words: set[str] = set()
    for v in args.values():
        if isinstance(v, str):
            target_words |= _words(v)
    return bool((target_words - _STOP) & (goal_words - _STOP))


# Classes where "the goal asked for this KIND of thing" is not enough: the goal
# must also be about the thing the tool acts on — "turn the volume down" is a
# system change, but it does not authorize system.lock or system.shutdown, and
# several of these tools are L1 (auto-approved), so no human sees them. Only for
# tools whose domain is known (`_known_domain`). Deliberately NOT applied to
#   modify — a fix legitimately touches files the goal never names
#            ("fix my Flask startup issue" -> requirements.txt);
#   delete — every delete-class tool is L2, so a human confirmation already
#            names the target, and "close this window" names no app at all.
_TARGETED_CLASSES = frozenset({A.COMMUNICATE, A.SYSTEM_CHANGE})


# -- the gate -----------------------------------------------------------------------


@dataclass(frozen=True)
class Alignment:
    aligned: bool
    action: ActionClass
    basis: str  # passive | allowed | supporting | explicit_target | no_scope | mismatch
    reason: str = ""


def check_alignment(
    scope: GoalScope | None, tool: str, args: dict[str, Any] | None = None, *, tier_hint: str = "",
) -> Alignment:
    """Is this call the KIND of action the user's goal authorizes? Says nothing
    about whether it is permitted or needs confirmation — that is still
    `friday.permissions`' job, after this."""
    args = args or {}
    cls = classify_call(tool, args, tier_hint=tier_hint)
    if scope is None:
        return Alignment(True, cls, "no_scope")
    if cls in PASSIVE:
        return Alignment(True, cls, "passive")

    rule = _resolve(tool, tier_hint)
    if isinstance(rule, ActionRule) and scope.explicit_click and rule.target_keys:
        label = next((str(args[k]).strip().lower() for k in rule.target_keys if args.get(k)), "")
        if len(label) >= 2 and re.search(rf"(?<![a-z0-9]){re.escape(label)}(?![a-z0-9])", scope.goal.lower()):
            return Alignment(True, cls, "explicit_target")  # the user literally named this control

    if cls in scope.allowed:
        domain = _known_domain(tool) if cls in _TARGETED_CLASSES and not isinstance(rule, ActionRule) else None
        if domain is None or _target_relevant(scope.goal, tool, args):
            return Alignment(True, cls, "allowed")
        reason = (
            f"intent_mismatch: the request does allow {_PHRASE[cls]}, but '{tool}' acts on something the "
            "request never mentioned. Use the tool that matches what the user named, or reply done."
        )
        return Alignment(False, cls, "mismatch", reason)
    if cls in scope.supporting and _target_relevant(scope.goal, tool, args):
        return Alignment(True, cls, "supporting")

    reason = (
        f"intent_mismatch: '{tool}' would {_PHRASE[cls]}, but the user's request only covers "
        f"{scope.describe()}. Choose a tool that only reads or observes, or reply done with what you have."
    )
    return Alignment(False, cls, "mismatch", reason)


def offered_for(scope: GoalScope | None, names_and_tiers: Iterable[tuple[str, str]]) -> set[str]:
    """Tool names worth showing the planner for this scope: any tool with at least one
    possible action class the scope could authorize. A pure prompt-shaping aid — the
    per-call `check_alignment` remains the authority."""
    names = [n for n, _ in names_and_tiers]
    if scope is None:
        return set(names)
    reachable = PASSIVE | scope.allowed | scope.supporting
    return {n for n, tier in names_and_tiers if possible_classes(n, tier_hint=tier) & reachable}


# -- normalized call identity, for the repeat guard --------------------------------------


_PATH_ARGS = frozenset({"path", "repo", "file", "folder", "directory", "cwd"})
_URL_ARGS = frozenset({"url"})


def _norm_path(value: str) -> str:
    """The same filesystem target however it is spelled: `~`, relative vs absolute,
    `/` vs `\\`, `..`, a trailing separator, and (Windows) letter case."""
    import os

    text = value.strip()
    if not text:
        return text
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(text)))
    except (OSError, ValueError):
        return text


def _norm_url(value: str) -> str:
    """Scheme/host case, a fragment and a trailing slash do not make a different page."""
    from urllib.parse import urlsplit, urlunsplit

    text = value.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.netloc:
        return text
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "", parts.query, ""))


def _coerce_like(value: Any, ptype: Any) -> Any:
    """A numeric/boolean STRING for an int/float/bool parameter, as that type ("4000" -> 4000)."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    try:
        if ptype is bool:
            low = text.lower()
            return True if low in ("true", "yes", "1") else False if low in ("false", "no", "0") else value
        if ptype is int:
            return int(float(text)) if float(text).is_integer() else value
        if ptype is float:
            return float(text)
    except ValueError:
        return value
    return value


def normalize_args(tool: str, args: dict[str, Any] | None, *, semantic: bool = False) -> dict[str, Any]:
    """Arguments in a comparable form: strings trimmed/case-folded/whitespace-collapsed,
    ints and integral floats unified, and any argument equal to the registered
    default dropped (so `files.read(path=x)` and `files.read(path=x, max_chars=<default>)`
    are the same call).

    `semantic=True` (Phase 22.0, the repeat guard's view of "the same call") also
    compares what a call DOES rather than how it is spelled: a path or URL argument is
    normalised to its target, an empty/None optional argument is absent, a numeric
    string equals the number, and — for a READ tool that declares a page cursor — a
    registered `presentational` size cap (`files.read(max_chars=...)`) is ignored, so
    changing only the cap can no longer pass for a new read while a different `offset`
    still does. A state-changing call never has anything ignored: only spelling."""
    from friday.registry import REGISTRY

    defaults: dict[str, Any] = {}
    types: dict[str, Any] = {}
    ignored: frozenset[str] = frozenset()
    sk = REGISTRY.get(tool)
    if sk is not None:
        defaults = {p.name: p.default for p in sk.params if not p.required}
        types = {p.name: p.type for p in sk.params}
        if semantic and sk.presentational and pagination_param(tool) is not None and is_passive_call(tool, args):
            ignored = frozenset(sk.presentational)

    def norm(v: Any) -> Any:
        if isinstance(v, str):
            return " ".join(v.split()).casefold()
        if isinstance(v, bool):
            return v
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, dict):
            return {str(k): norm(x) for k, x in sorted(v.items())}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        return v

    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k in ignored:
            continue
        if semantic:
            if k in types:
                v = _coerce_like(v, types[k])
            if isinstance(v, str):
                if k in _PATH_ARGS:
                    v = _norm_path(v)
                elif k in _URL_ARGS:
                    v = _norm_url(v)
            if k in defaults and (v is None or (isinstance(v, str) and not v.strip())) and (
                defaults[k] is None or (isinstance(defaults[k], str) and not defaults[k].strip())
            ):
                continue  # an explicitly empty optional argument is the same as leaving it out
        nv = norm(v)
        if k in defaults and nv == norm(defaults[k]):
            continue
        out[str(k)] = nv
    return out


# -- Phase 22.0: confirmations that refer to nothing --------------------------------------
#
# "Yes, fix it" only means something when there is something to say yes TO: a pending
# confirmation/question (handled first by the session), or the previous turn's report-only
# goal (`derive_expansion` above). With neither, the BRAIN's embedding matcher still picks
# the skill whose example phrasing is nearest — measured on this tree: "yes, fix it" ->
# meta.undo (0.74), a bare "yes" / "go ahead" -> whatsapp.send (0.73-0.87). Similarity to
# an example is not what the user asked for; these two pure predicates are the deterministic
# check the session applies to that guess.

_UNDO_INTENT = re.compile(
    r"\b(?:undo|un-do|revert|reverts|rollback|roll\s*back|reverse|"
    r"take\s+(?:\w+\s+){0,4}?back|put\s+(?:\w+\s+){0,2}?back|"
    r"(?:change|set|switch|turn|bring|move)\s+(?:it|that|this|them)\s+back|"
    r"go\s+back|step\s+back|back\s+to\s+(?:how|the|what|where)|"
    r"(?:the\s+way|how|as)\s+it\s+was|cancel\s+(?:that|it|what)|restore\s+(?:it|that|the))\b",
    re.I,
)
_FIX_DIRECTIVE = re.compile(
    r"\b(?:fix|repair|resolve|solve|correct)\s+(?:it|that|this|them|those|these|everything|all|"
    r"the\s+(?:issues?|problems?|errors?|bugs?))\b",
    re.I,
)


def asks_for_undo(text: str) -> bool:
    """Do the user's own words actually ask to undo/revert something? `meta.undo` is a
    state-changing tool; it may only be reached through an explicit undo word — never
    because "fix it" happens to sit near "undo that" in an embedding space."""
    return bool(_UNDO_INTENT.search(text or ""))


def is_confirmation_like(text: str) -> bool:
    """Is this utterance SHAPED like an answer to something that was just said — it leads with
    an affirmation ("yes, ...", "ok ...", "sure ...") or is a short "fix it / fix that / fix the
    issues" directive? Only such utterances are held to the explicit-undo rule; "oops", "scratch
    that" or "that was a mistake" are ordinary commands and keep whatever routing they had."""
    body = (text or "").strip()
    if not body:
        return False
    lead = _AFFIRM_LEAD.match(body)
    if lead and lead.end() > 0:
        return True
    return bool(_FIX_DIRECTIVE.search(body)) and len(_WORD_RE.findall(body.lower())) <= 8


def is_bare_affirmation(text: str) -> bool:
    """Is the whole utterance nothing but an affirmation ("yes", "yeah, go ahead",
    "ok please", "do it")? Deterministic, from the words alone. On its own it names no
    action, so it may only ever answer a question that is actually pending."""
    body = (text or "").strip()
    if not body:
        return False
    return not _AFFIRM_LEAD.sub("", body).strip(" \t,.!?:;-")
