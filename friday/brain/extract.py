"""L3 — slot extraction.

Pull a skill's arguments out of the utterance. Strategy is chosen from the
parameter's name and type, and values are resolved against live system state
where possible (installed apps, real paths) rather than guessed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from friday.log import get
from friday.registry import Param, Skill

log = get(__name__)

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "half": 50, "max": 100, "maximum": 100, "full": 100, "min": 0, "minimum": 0,
}

# Leading intent phrasing stripped when isolating an app name or search query.
# Ordered longest-first so "search for" beats "search".
_LEAD_VERBS = re.compile(
    r"^\s*(please\s+)?"
    r"(can you\s+|could you\s+|would you\s+|will you\s+)?"
    r"(i (?:need|want|would like)(?: to (?:open|see|find))?|"
    r"get me|give me|let me see|i'm looking for|looking for|"
    r"open up|open|launch|start up|start|run|fire up|boot up|"
    r"bring up|pull up|load up|load|"
    r"close|quit|exit|kill|shut down|shut|stop|"
    r"switch to|switch over to|focus on|focus|go to|jump to|"
    r"show me|show|display|reveal|"
    r"search for|search|find me|find|look for|look up|locate|"
    r"dig up|dig out|track down|where is|where's|"
    r"read me|read)\s+",
    re.IGNORECASE,
)

# Possessives and determiners left behind once the verb is gone.
_DETERMINERS = re.compile(
    r"^(the|a|an|my|our|this|that|some|any)\s+", re.IGNORECASE
)

_TRAILING = re.compile(
    r"\s+(please|for me|now|app|application|program|window|file|folder)\s*$",
    re.IGNORECASE,
)

_NEGATIVE = re.compile(r"\b(un|not|off|disable|stop)\b", re.IGNORECASE)

_PATHISH = re.compile(r"""["']?((?:[a-zA-Z]:[\\/]|\.{0,2}[\\/~])[^"'\s]*)["']?""")

_EXT = re.compile(
    r"\b(pdf|docx?|xlsx?|pptx?|txt|md|csv|json|png|jpe?g|gif|mp4|mp3|zip|py|js|ts|html?)\b",
    re.IGNORECASE,
)


# WhatsApp compose: "message/text <contact> [on whatsapp] [: | asking | saying] <message>".
_WHATSAPP_VERB = re.compile(
    r"^\s*(please\s+)?(can you\s+)?"
    r"(message|text|whatsapp|write to|prepare a (?:whatsapp\s+)?message (?:for|to)|"
    r"draft a (?:whatsapp\s+)?message (?:for|to)|write a (?:whatsapp\s+)?message to)\s+",
    re.IGNORECASE,
)
_WHATSAPP_SAY = re.compile(
    r"\s*(?::|,)?\s*\b(?:asking|saying|that says|telling (?:him|her|them)|"
    r"to ask|and (?:ask|say)|to say)\b\s*[:,]?\s*|\s*:\s*",
    re.IGNORECASE,
)


def _whatsapp_contact_from_head(head: str) -> str:
    h = _WHATSAPP_VERB.sub("", head).strip()
    h = re.sub(r"\bon whatsapp\b", "", h, flags=re.IGNORECASE).strip()
    h = re.sub(r"\bthat\b\s*$", "", h, flags=re.IGNORECASE).strip()
    h = _DETERMINERS.sub("", h).strip()
    return h


def _whatsapp_slot(text: str, which: str) -> str | None:
    s = text.strip()
    match = _WHATSAPP_SAY.search(s)
    if not match:
        return None
    head, tail = s[: match.start()], s[match.end():]
    contact = _whatsapp_contact_from_head(head)
    body = tail.strip().strip('"“”').strip()
    if not contact or not body:
        return None
    return contact if which == "contact" else body


def _numbers(text: str) -> list[int]:
    found = [int(m) for m in re.findall(r"\b(\d{1,4})\b", text)]
    for word, value in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", text, re.IGNORECASE):
            found.append(value)
    return found


# See the project.* extraction branch below for why these exist.
_PROJECT_LEAD = re.compile(
    r"^\s*(please\s+)?(can you\s+)?(check on|check|inspect|look at)\s+",
    re.IGNORECASE,
)
_PROJECT_TRAILING_CLAUSE = re.compile(
    r"\s+(?:and then|and also|and|then|after that|once you)\s+.+$", re.IGNORECASE,
)
# "open the project IN VS CODE" -- project.open always opens in VS Code, so
# this trailing phrase names the destination the skill already implies, not
# part of the project's name; stripping it is what turns "project in vs
# code" (garbage, ProjectNotFound) into a bare "project", handled next.
_PROJECT_TRAILING_TOOL = re.compile(
    r"\s+in\s+(?:vs\s*code|visual\s*studio\s*code|vscode)\s*$", re.IGNORECASE,
)
# A residual bare "project" (no actual name survived stripping) means none
# was ever named -- e.g. "open the project", "check the project" -- and
# should resolve to the current/default project exactly like an explicitly
# blank name does (see friday.project.resolve's own "this"/"current"/"my
# project" special-casing), not be passed through as a literal folder query.
_PROJECT_BARE_NOUN = {"project", "projects", "the project", "this project", "my project"}


def _strip_to_object(text: str) -> str:
    """Remove leading intent phrasing and trailing filler, leaving the object.

    Applied repeatedly because utterances stack them: "can you please pull up
    my chrome" needs three passes before "chrome" is left.
    """
    s = text.strip()
    for _ in range(4):
        before = s
        s = _LEAD_VERBS.sub("", s).strip()
        s = _DETERMINERS.sub("", s).strip()
        s = _TRAILING.sub("", s).strip()
        if s == before:
            break
    return s


# Verbs that act on an existing job, plus the nouns that name one.
_JOB_VERB = re.compile(
    r"^\s*(please\s+)?(can you\s+)?"
    r"(delete|remove|cancel|drop|get rid of|kill|"
    r"pause|disable|turn off|stop|"
    r"resume|enable|turn on|reactivate|restart|"
    r"run|trigger|execute|fire|start|"
    r"show|check|how did|what happened to|history of)\s+",
    re.IGNORECASE,
)

_JOB_NOUN = re.compile(
    r"\b(job|task|reminder|automation|schedule[d]?|routine|rule|trigger)\b",
    re.IGNORECASE,
)


def _job_name(text: str) -> str | None:
    """Extract a job's name from 'delete the lock the pc job'.

    The job noun is the anchor: everything between the leading verb and the
    noun is the name. Falls back to verb-stripping when no noun is present.
    """
    s = text.strip()

    noun = _JOB_NOUN.search(s)
    if noun:
        # Prefer the span before the noun; "delete the X job" -> "X".
        head = s[: noun.start()].strip()
        head = _JOB_VERB.sub("", head).strip()
        head = _DETERMINERS.sub("", head).strip()
        if head:
            return head
        # "delete the job called X" -> take what follows instead.
        tail = s[noun.end():].strip()
        tail = re.sub(r"^(called|named|for|to)\s+", "", tail, flags=re.I).strip()
        tail = _DETERMINERS.sub("", tail).strip()
        if tail:
            return tail

    stripped = _JOB_VERB.sub("", s).strip()
    stripped = _DETERMINERS.sub("", stripped).strip()
    stripped = _JOB_NOUN.sub("", stripped).strip()
    return stripped or None


def _resolve_app_name(candidate: str) -> str:
    """Match a phrase against actually-installed apps, trying shorter suffixes.

    Handles residue the verb stripper misses: "i need chrome" -> "chrome",
    because "chrome" resolves against the Start Menu index and the full phrase
    does not.
    """
    from friday.skills.apps import resolve_app

    words = candidate.split()
    if not words:
        return candidate

    # Full phrase first, then progressively drop leading words.
    for start in range(len(words)):
        attempt = " ".join(words[start:])
        if resolve_app(attempt) is not None:
            return attempt
    return candidate


# Time units. Order is load-bearing: Python's alternation is first-match, not
# longest-match, so any unit that is a prefix of another must come after it.
# "weekday" must be tried before "week", or "every weekday at 9am" captures
# "every week" and drops the rest of the clause.
_UNIT_ALT = (
    r"weekdays?|weekends?|"
    r"mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|"
    r"mornings?|afternoons?|evenings?|nights?|"
    r"seconds?|secs?|minutes?|mins?|hours?|hrs?|"
    r"weeks?|days?"
)

# Time expressions that introduce or terminate a schedule clause.
_WHEN_SPAN = re.compile(
    r"("
    rf"(?:every|each)\s+(?:\d+\s*)?(?:{_UNIT_ALT})"
    r"(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
    rf"|in\s+\d+\s*(?:{_UNIT_ALT})"
    r"|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
    r"|when(?:ever)?\s+.+?(?=\s+(?:then|tell|check|run|do|lock|open|close|send|show|play)\b|$)"
    r"|daily|hourly|weekly|tomorrow|tonight"
    r")",
    re.IGNORECASE,
)


def _split_schedule(text: str) -> tuple[str, str]:
    """Separate a scheduling utterance into (command, when).

    "every day at 8am tell me my battery" -> ("tell me my battery",
                                              "every day at 8am")
    """
    match = _WHEN_SPAN.search(text)
    if not match:
        return text.strip(), ""

    when = match.group(0).strip()
    command = (text[: match.start()] + " " + text[match.end():]).strip()

    # Strip the scheduling verb that framed the sentence.
    command = re.sub(
        r"^\s*(please\s+)?(can you\s+)?"
        r"(schedule|set up|set|create|make|add|automate|remind me to|remind me)\s+",
        "", command, flags=re.IGNORECASE,
    ).strip()
    command = re.sub(r"^(a|an|the)\s+(job|task|reminder|automation)\s+(to\s+)?", "",
                     command, flags=re.IGNORECASE).strip()
    command = re.sub(r"\s+(then|and)\s*$", "", command, flags=re.IGNORECASE).strip()
    command = re.sub(r"^\s*(then|to)\s+", "", command, flags=re.IGNORECASE).strip()

    return command, when


def _extract_one(param: Param, text: str, skill: Skill) -> Any | None:
    name = param.name.lower()
    ptype = param.type

    # --- scheduling: command and when are two halves of one utterance -----
    if skill.name.startswith("schedule.") and name in ("command", "when"):
        command, when = _split_schedule(text)
        return (command or None) if name == "command" else (when or None)

    # --- scheduling: the job's name, as in "delete the morning job" -------
    if skill.name.startswith("schedule.") and name == "name":
        return _job_name(text)

    # --- routines: the skill parses the name and steps itself --------------
    if skill.name.startswith("routine.") and name in ("steps", "name"):
        return text.strip() or None

    # --- utilities that do their own phrase parsing ------------------------
    if skill.name in ("notes.add", "math.calculate", "timer.set") and name in (
        "text", "expression", "duration"
    ):
        return text.strip() or None

    if skill.name == "weather.now" and name == "location":
        # "weather in mumbai" -> mumbai; a bare "what's the weather" -> None,
        # which wttr.in resolves by IP.
        place = re.search(r"\b(?:in|for|at)\s+([a-z][a-z\s-]{1,30})$", text, re.I)
        return place.group(1).strip() if place else None

    # --- project name/path: fuzzy-resolved by friday.project, so the raw ----
    # stripped phrase is enough here; unresolved names fail in the skill
    # itself with a clean spoken error rather than here.
    #
    # Phase 15.0: found on the real machine against the brief's own literal
    # example ("Inspect my FRIDAY project, and tell me what I should work on
    # next.") -- `_LEAD_VERBS` was never taught "inspect"/"check on"/"look
    # at" (it's tuned for app/file/search objects), so those verbs, and any
    # trailing "and tell me ..." clause, became PART of the extracted name.
    # `project.resolve()`'s fuzzy match then only succeeded by accident when
    # enough of the real folder name survived ("check on my friday project"
    # happened to still fuzzy-match "FRIDAY"; the brief's own longer example
    # did not: name ended up as the entire sentence, ProjectNotFound). The
    # trailing clause is stripped FIRST so it can never dilute the match.
    # A bare "what's my project status" (no project named at all) is a
    # related, deliberately NOT-fixed limitation -- same "fix only the
    # demonstrated case" discipline Phase 12.0 already applied to this same
    # module's other known gaps (see PLAN.md Phase 15.0).
    if skill.name.startswith("project.") and name == "name":
        obj = _PROJECT_TRAILING_CLAUSE.sub("", text).strip()
        obj = _PROJECT_LEAD.sub("", obj).strip()
        obj = _PROJECT_TRAILING_TOOL.sub("", obj).strip()
        obj = _strip_to_object(obj)
        if obj.lower() in _PROJECT_BARE_NOUN:
            obj = ""
        return obj or None

    # --- plan.run: the whole utterance is the goal, verbatim ---------------
    if skill.name == "plan.run" and name == "goal":
        return text.strip() or None

    # --- whatsapp.compose: "message/text <contact> [on whatsapp] <say-verb> ---
    # <message>" — see _whatsapp_slot. Deliberately best-effort: phrasings it
    # can't split (e.g. no colon and no say-verb at all) fall through to
    # ASK_SLOT, which asks for contact/message one at a time instead of
    # guessing wrong and drafting a message to/for the wrong person.
    if skill.name == "whatsapp.compose" and name in ("contact", "message"):
        return _whatsapp_slot(text, name)

    # --- memory: pass the utterance through intact -------------------------
    # These skills do their own phrase stripping, and the generic query
    # handler would mangle "remember that my sister's name is priya" into
    # "sister's name is priya" by treating "remember" as a search verb.
    if skill.name.startswith("memory.") and name in ("text", "query"):
        return text.strip() or None

    # --- explicit paths ---------------------------------------------------
    if name in ("path", "file", "folder", "directory"):
        m = _PATHISH.search(text)
        if m:
            return str(Path(m.group(1)).expanduser())
        return None

    # --- app / window names ------------------------------------------------
    if name in ("app", "application", "program", "window"):
        obj = _strip_to_object(text)
        if not obj:
            return None
        # Resolving against installed apps cleans up any residue left behind.
        return _resolve_app_name(obj)

    # --- search queries ----------------------------------------------------
    if name in ("query", "term", "search", "text"):
        obj = _strip_to_object(text)
        obj = re.sub(
            r"\b(called|named|about|titled|for me|file|files|document|documents)\b",
            "", obj, flags=re.I,
        ).strip()
        obj = _EXT.sub("", obj).strip()
        obj = _DETERMINERS.sub("", obj).strip()
        obj = re.sub(r"\s+", " ", obj)
        return obj or None

    # --- file extension filter ---------------------------------------------
    if name in ("extension", "ext", "filetype"):
        m = _EXT.search(text)
        return m.group(1).lower() if m else None

    # --- booleans ----------------------------------------------------------
    if ptype is bool:
        if name == "state":  # mute/unmute style toggles
            return not bool(re.search(r"\bun(mute)?\b|\bturn (it )?on\b", text, re.I))
        return not bool(_NEGATIVE.search(text))

    # --- numbers -----------------------------------------------------------
    if ptype in (int, float):
        nums = _numbers(text)
        if not nums:
            return None
        value = nums[0]
        if name in ("level", "percent", "volume"):
            value = max(0, min(100, value))
        return ptype(value)

    # --- mode-style enums --------------------------------------------------
    if name == "mode":
        for candidate in ("restart", "reboot", "logoff", "sign out", "shutdown"):
            if candidate in text.lower():
                return {"reboot": "restart", "sign out": "logoff"}.get(
                    candidate, candidate
                )
        return None

    if name == "region":
        return "window" if re.search(r"\bwindow\b", text, re.I) else "screen"

    return None


def extract(skill: Skill, text: str) -> tuple[dict[str, Any], list[str]]:
    """Fill a skill's parameters from `text`.

    Returns (args, missing) — `missing` lists required params that could not be
    resolved, so the caller can ask instead of failing.
    """
    args: dict[str, Any] = {}
    missing: list[str] = []

    for param in skill.params:
        value = _extract_one(param, text, skill)
        if value is not None and value != "":
            args[param.name] = value
        elif param.required:
            missing.append(param.name)

    log.debug("extract %s from %r -> %s (missing=%s)", skill.name, text, args, missing)
    return args, missing
