"""Turn spoken time expressions into trigger specifications.

    "every day at 8am"        -> cron,     {hour: 8,  minute: 0}
    "every weekday at 9:30"   -> cron,     {hour: 9,  minute: 30, day_of_week: mon-fri}
    "every 30 minutes"        -> interval, {minutes: 30}
    "in 20 minutes"           -> once,     {run_at: ...}
    "when the battery is low" -> event,    {event: battery.low}
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

_UNITS = {
    "second": "seconds", "seconds": "seconds", "sec": "seconds", "secs": "seconds",
    "minute": "minutes", "minutes": "minutes", "min": "minutes", "mins": "minutes",
    "hour": "hours", "hours": "hours", "hr": "hours", "hrs": "hours",
}

_DAY_SETS = {
    "weekday": "mon-fri", "weekdays": "mon-fri", "work day": "mon-fri",
    "weekend": "sat,sun", "weekends": "sat,sun",
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
}

_NAMED_TIMES = {
    "morning": (8, 0), "noon": (12, 0), "midday": (12, 0),
    "afternoon": (14, 0), "evening": (18, 0), "night": (21, 0),
    "midnight": (0, 0), "lunch": (13, 0), "lunchtime": (13, 0),
}

# Spoken phrases mapped to the event names emitted by friday.triggers.
# Order matters — the first match wins, so the most specific patterns come
# first. Network patterns precede battery ones because "disconnect" is a
# network word that also appears in charger phrasing.
_EVENTS: list[tuple[str, str]] = [
    # network — checked first, and anchored on network nouns
    (r"(wi-?fi|network|internet|connection).*(drop|disconnect|lost|lose|down|offline|out)", "network.disconnected"),
    (r"(disconnect|offline|lose|lost).*(wi-?fi|network|internet|connection)", "network.disconnected"),
    (r"(wi-?fi|network|internet|connection).*(connect|back|up|online|restore)", "network.connected"),
    (r"(reconnect|back online|internet.*returns)", "network.connected"),
    # battery — every pattern requires a battery/charger noun
    (r"battery.*(critical|very low|almost dead|about to die)", "battery.critical"),
    (r"battery.*(low|below|running out|getting low|dying)", "battery.low"),
    (r"battery.*(full|100|fully charged)", "battery.full"),
    (r"(charger|power).*(plug|connect)|(plug|connect).*(charger|power)|start(s)? charging", "battery.charging"),
    (r"(unplug|stop charging|on battery|charger.*(remove|out))", "battery.discharging"),
    # session state
    (r"(i am|i'm|go|goes|going|am).*(idle|away|afk|inactive)", "idle.started"),
    (r"(come back|i return|no longer idle|stop being idle|back at)", "idle.ended"),
    # files and apps
    (r"(new file|file.*(add|appear|arrive|show up)|something.*download)", "files.added"),
    (r"(switch|change|move).*(window|app|program)", "window.changed"),
    (r"(open|start|launch|run).*(program|process|app)", "process.started"),
    (r"(close|quit|stop|exit).*(program|process|app)", "process.stopped"),
]

_TIME = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.IGNORECASE
)
_EVERY_N = re.compile(
    r"\bevery\s+(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE
)
_IN_N = re.compile(
    r"\bin\s+(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE
)


def _parse_clock(text: str) -> tuple[int, int] | None:
    """Extract an hour/minute from '8', '8:30', '8pm', 'at 17:00'."""
    for name, (hour, minute) in _NAMED_TIMES.items():
        if re.search(rf"\b{name}\b", text, re.IGNORECASE):
            # An explicit clock time overrides the named period.
            match = _TIME.search(text)
            if not match:
                return hour, minute

    match = _TIME.search(text)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower().replace(".", "")

    if meridiem.startswith("p") and hour < 12:
        hour += 12
    elif meridiem.startswith("a") and hour == 12:
        hour = 0
    elif not meridiem and hour <= 7 and "morning" not in text.lower():
        # "at 6" spoken casually almost always means the evening.
        hour += 12

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_when(text: str) -> tuple[str, dict[str, Any]] | None:
    """Return (trigger_type, trigger_spec), or None if nothing parses."""
    low = text.lower().strip()
    if not low:
        return None

    # --- event triggers ---------------------------------------------------
    if re.search(r"\b(when|whenever|if|as soon as|any time)\b", low):
        for pattern, event in _EVENTS:
            if re.search(pattern, low):
                return "event", {"event": event, "human": text}

    # --- "every N units" --------------------------------------------------
    if match := _EVERY_N.search(low):
        amount = int(match.group(1))
        unit = _UNITS.get(match.group(2).rstrip("s"), None) or _UNITS.get(match.group(2))
        if unit:
            return "interval", {unit: amount, "human": text}

    # --- "in N units" -----------------------------------------------------
    if match := _IN_N.search(low):
        amount = int(match.group(1))
        unit = _UNITS.get(match.group(2).rstrip("s")) or _UNITS.get(match.group(2))
        delta = {
            "seconds": timedelta(seconds=amount),
            "minutes": timedelta(minutes=amount),
            "hours": timedelta(hours=amount),
        }.get(unit or "minutes", timedelta(minutes=amount))
        run_at = datetime.now() + delta
        return "once", {"run_at": run_at.isoformat(), "human": text}

    # --- recurring clock times -------------------------------------------
    recurring = bool(re.search(r"\b(every|each|daily|weekly|always)\b", low))
    clock = _parse_clock(low)

    if clock and recurring:
        hour, minute = clock
        spec: dict[str, Any] = {"hour": hour, "minute": minute, "human": text}
        for phrase, days in _DAY_SETS.items():
            if re.search(rf"\b{phrase}\b", low):
                spec["day_of_week"] = days
                break
        return "cron", spec

    if clock:
        # A bare time means the next occurrence of it.
        hour, minute = clock
        run_at = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_at <= datetime.now():
            run_at += timedelta(days=1)
        return "once", {"run_at": run_at.isoformat(), "human": text}

    # --- bare recurrence without a time ------------------------------------
    if recurring:
        if "hour" in low:
            return "interval", {"hours": 1, "human": text}
        if "day" in low or "daily" in low:
            return "cron", {"hour": 9, "minute": 0, "human": text}
        if "minute" in low:
            return "interval", {"minutes": 15, "human": text}

    return None
