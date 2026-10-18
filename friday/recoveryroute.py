"""Phase 34 — deterministic natural-language routing for the operator-only recovery skills.

The four recovery skills (`files.recoveries`, `files.recover`, `files.recovery_cleanup`,
`files.recovery_migrate`) are `internal`: the planner is never offered them, because three more tool
lines cost every planner prompt context it does not have to spare (Phase 33). Until now the only way
in was the console / CLI command `recovery ...`. This module lets a plain sentence reach them -- and
nothing else does.

It is a CLASSIFIER, not an actor. `classify(text)` maps one sentence to a `RecoveryRoute` or `None`;
it never executes, never authorises and never widens anything. `Session` runs the resulting skill
through the ordinary `_run` -> `run_verified` -> `Executor.run` path, so tier policy, confirmation,
the unattended-actor ceiling, risk escalation, audit and undo all apply exactly as for the console
command. A route names one of four skills in `ROUTED_SKILLS`; anything else is a bug and is refused.

Design rules (each has a test and a mutation in scripts/smoke_recovery_route.py):

  * FULL-MATCH ONLY. The whole sentence (after polite fillers like "please" / "can you") must be one
    recognised request. A sentence with extra clauses, an extra file name or any unmatched word is not
    ours and falls through untouched to the normal router/planner ("restore report.docx from snapshot
    <id>", "show recovery snapshots and clean them up", "how do recovery snapshots work?").
  * THE NOUN IS REQUIRED. "recovery snapshots / copies / store / files" or "recoverable files" -- a bare
    "snapshots" or "copies" (VM snapshots, screenshots, file copies) is never enough on its own. The one
    exception is `restore/recover snapshot|copy <id>` where the id itself is the evidence.
  * IDS ARE NEVER INFERRED. Only a string of exactly the shape `recovery.is_token` accepts is an id.
    "snapshot 3", "the second one", a file name or a wrong-case id never selects a snapshot: the user is
    shown the real list and asked which one. File names are not matched against snapshot paths either.
  * LISTING IS READ-ONLY. A list/question sentence routes to `files.recoveries` and nothing else.
  * CLEANUP NEEDS EXPLICIT WORDING. "clean up ..." / "prune ..." or an age word ("old", "expired",
    "stale", ...) with a delete verb. "delete the recovery copies" or "delete ALL recovery snapshots"
    is not what `files.recovery_cleanup` does (it keeps everything inside retention and everything an
    undo needs), so those get an explanation and nothing runs. Cleanup/migrate/recover keep their
    own confirmation and risk tiers; this module has no way to skip them.
  * MIGRATE NEEDS "migrate" AND "recovery". Never a side effect of anything else.

Kill switch: `CFG.brain.recovery_routing`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from friday import recovery

LIST = "files.recoveries"
RECOVER = "files.recover"
CLEANUP = "files.recovery_cleanup"
MIGRATE = "files.recovery_migrate"
ROUTED_SKILLS = frozenset({LIST, RECOVER, CLEANUP, MIGRATE})

# kinds that do not run a skill by themselves
CLARIFY_RESTORE = "clarify_restore"  # show the real list and ask which id
EXPLAIN = "explain"  # say what FRIDAY can do instead; nothing runs

_ID = "<id>"
_BAD_ID = "<badid>"
_TOKEN_SHAPE = re.compile(r"(?<![\w-])\d{8}[Tt]\d{6}-[0-9A-Fa-f]{12}(?![\w-])")


@dataclass(frozen=True)
class RecoveryRoute:
    kind: str  # "list" | "recover" | "cleanup" | "migrate" | CLARIFY_RESTORE | EXPLAIN
    skill: str = ""
    args: dict = field(default_factory=dict)
    speech: str = ""  # only for CLARIFY_RESTORE / EXPLAIN


# -- normalisation ----------------------------------------------------------------------------------


def _normalise(text: str) -> tuple[str, str | None]:
    """(sentence with the id replaced by a placeholder, the id or None). An id-shaped string that is not
    exactly a valid token (wrong case, ...) becomes `<badid>`: it is evidence the user named a snapshot,
    never a snapshot."""
    found: list[str] = []

    def sub(m: re.Match[str]) -> str:
        found.append(m.group(0))
        return f" {_ID if recovery.is_token(m.group(0)) else _BAD_ID} "

    s = _TOKEN_SHAPE.sub(sub, text or "")
    s = s.replace("’", "'").lower()
    s = re.sub(r"[?!.,;:\"]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(
        r"^(?:(?:hey|ok|okay|hi|so)\s+)?(?:friday\s+)?(?:please\s+)?"
        r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
        r"(?:(?:i\s+(?:want|need|would\s+like)|i'd\s+like|let's)\s+(?:you\s+)?(?:to\s+)?)?",
        "", s,
    )
    s = re.sub(r"(?:\s+(?:for\s+me|please|now|thanks|thank\s+you))+$", "", s).strip()
    return s, (found[0] if len(found) == 1 and recovery.is_token(found[0]) else None)


# -- grammar ----------------------------------------------------------------------------------------

_DET = r"(?:(?:the|my|your|all|any|old|older|saved|available|expired|stale|friday's|current|recent|every)\s+)*"
_RNP = (
    r"(?:recovery\s+(?:snapshots?|copies|copy|store|files?|folder)"
    r"|recoverable\s+(?:files?|snapshots?|copies|items))"
)
_HAVE = r"(?:\s+(?:do\s+(?:you|i)\s+have|are\s+there|(?:you|friday)\s+(?:have|kept|saved)|i\s+have|available|exist))?"

_LIST = [
    re.compile(rf"(?:recovery\s+)?(?:list|show|display|view|see|give|get)(?:\s+me)?\s+{_DET}{_RNP}{_HAVE}"),
    re.compile(rf"(?:show|tell)\s+me\s+(?:what|which)\s+{_RNP}\s+(?:there\s+are|i\s+have|you\s+have|are\s+there|exist)"),
    re.compile(rf"(?:what|which)\s+{_RNP}\s+(?:do\s+(?:you|i)\s+have|are\s+there|(?:you|friday)\s+(?:have|kept|saved)|i\s+have|exist)"),
    re.compile(rf"(?:do|have)\s+(?:i|you)\s+(?:have\s+)?(?:any|some)\s+{_RNP}"),
    re.compile(rf"are\s+there\s+(?:any|some)\s+{_RNP}"),
    re.compile(r"(?:what|which)\s+(?:files?\s+|things\s+|folders?\s+)?(?:can|could)\s+(?:friday|you)\s+(?:recover|restore|bring\s+back)"),
    re.compile(r"(?:what|which)\s+recoverable\s+(?:files?|items)\s+(?:do\s+(?:you|i)\s+have|are\s+there|(?:you|friday)\s+have)"),
    re.compile(r"recovery\s+list"),
]

_VERB = r"(?:restore|recover|bring\s+back|put\s+back|get\s+back|undelete)"
_OBJ = r"(?:(?:the|my|that|this|a)\s+)?(?:(?:deleted|overwritten|lost|old)\s+)?(?:file|folder|document)s?"
_SNAP = r"(?:(?:the|my|this|that|a)\s+)?(?:recovery\s+)?(?:snapshot|copy)"
_RECOVER_WITH_REF = re.compile(
    rf"(?:recovery\s+)?{_VERB}\s+(?:{_OBJ}\s+)?(?:from\s+)?(?:(?P<snap>{_SNAP})\s+)?"
    rf"(?:(?:with\s+)?(?:id|number)\s+)?(?P<ref>\S+)"
)
_RECOVER_VAGUE = re.compile(
    rf"(?:recovery\s+{_VERB}"
    rf"|(?:recovery\s+)?{_VERB}\s+(?:(?:the|my|that|this|a|those|these|some)\s+)?(?:(?:deleted|overwritten|lost|old|previous|recent)\s+)?"
    rf"(?:files?|folders?|documents?|snapshots?|recovery\s+(?:snapshots?|copies|copy)|copy|copies)"
    rf"(?:\s+(?:you|friday|i)\s+(?:just\s+)?(?:deleted|overwrote|removed|saved))?"
    rf"(?:\s+from\s+(?:the\s+)?(?:recovery(?:\s+(?:copies|snapshots|store))?|snapshots?))?)"
)

_CLEAN_VERB = r"(?:clean\s*up|tidy\s+up|prune|clear\s+out|clear|purge|delete|remove|empty|get\s+rid\s+of)"
_CLEAN = re.compile(
    rf"(?P<verb>{_CLEAN_VERB})\s+(?P<q>(?:(?:the|my|your|all|any|every|old|older|expired|stale|unneeded|unused|outdated|"
    rf"saved|of|those|these|friday's)\s+)*){_RNP}(?:\s+(?:that\s+)?(?:are|is)\s+(?:old|expired|stale|unneeded|unused|outdated))?"
)
_CLEAN_CMD = re.compile(r"recovery\s+(?:clean\s*up|cleanup)")
_AGE_WORDS = frozenset({"old", "older", "expired", "stale", "unneeded", "unused", "outdated"})
_CLEAN_SOFT = re.compile(r"(?:clean\s*up|tidy\s+up|prune)")
_ALL_WORDS = frozenset({"all", "every", "everything"})

_MIGRATE = re.compile(
    r"(?:migrate\s+(?:(?:the|my|your|all|any|old|legacy|previous|existing)\s+)*"
    r"recovery\s+(?:snapshots?|copies|store|files?|folder)"
    r"(?:\s+(?:to|into)\s+(?:the\s+)?(?:new|current)\s+(?:store|location|folder))?"
    r"|recovery\s+migrate)"
)


def _clarify_restore(reason: str) -> RecoveryRoute:
    return RecoveryRoute(kind=CLARIFY_RESTORE, skill=LIST, args={"limit": 10}, speech=reason)


def classify(text: str) -> RecoveryRoute | None:
    """The recovery route for `text`, or None when the sentence is not (exactly) a recovery request."""
    s, token = _normalise(text)
    if not s:
        return None

    if _MIGRATE.fullmatch(s):
        return RecoveryRoute(kind="migrate", skill=MIGRATE)

    if _CLEAN_CMD.fullmatch(s):
        return RecoveryRoute(kind="cleanup", skill=CLEANUP)
    m = _CLEAN.fullmatch(s)
    if m:
        words = set(m.group("q").split()) | set(s.split())
        explicit = bool(_CLEAN_SOFT.fullmatch(m.group("verb"))) or bool(words & _AGE_WORDS)
        if explicit and not (words & _ALL_WORDS):
            return RecoveryRoute(kind="cleanup", skill=CLEANUP)
        return RecoveryRoute(
            kind=EXPLAIN,
            speech=(
                "I only clean up recovery copies that are past the retention period and that no undo entry "
                "needs; I won't delete all of them or guess which ones you mean. "
                "Say \"clean up expired recovery snapshots\" and I'll show you what would go and ask first."
            ),
        )

    m = _RECOVER_WITH_REF.fullmatch(s)
    if m:
        ref, snap = m.group("ref"), m.group("snap")
        if ref == _ID and token:
            return RecoveryRoute(kind="recover", skill=RECOVER, args={"snapshot": token})
        if snap or ref in (_ID, _BAD_ID):
            return _clarify_restore(
                "I won't guess which recovery copy you mean: I need the full recovery id exactly as listed "
                "(a number, a position like \"the second one\" or a file name isn't enough)."
            )
        # otherwise ("restore file.txt", "recover password") this is not obviously a recovery request;
        # the vague pattern below decides, and anything it does not recognise falls through.
    if _RECOVER_VAGUE.fullmatch(s):
        return _clarify_restore("I won't guess which recovery copy you mean.")

    if any(p.fullmatch(s) for p in _LIST):
        wants_all = bool(re.search(r"\ball\b", s))
        return RecoveryRoute(kind="list", skill=LIST, args={"limit": 50 if wants_all else 10})
    return None
