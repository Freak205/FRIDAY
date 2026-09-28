"""Everyday utilities: timers, weather, notes, calculations.

Weather uses wttr.in, which needs no API key and no account.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import httpx

from friday import notify, paths
from friday.log import get
from friday.registry import SkillResult, skill

log = get(__name__)

NOTES_FILE = paths.DATA / "notes.md"

_DURATION = re.compile(
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE,
)

_WORD_NUMS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty five": 45, "sixty": 60, "half": 0.5,
}


def _parse_duration(text: str) -> timedelta | None:
    match = _DURATION.search(text)
    if match:
        amount = float(match.group(1))
        unit = match.group(2).lower()
    else:
        # "half an hour", "a minute", "twenty minutes"
        word = re.search(
            r"\b(" + "|".join(_WORD_NUMS) + r")\s+(?:an?\s+)?"
            r"(seconds?|minutes?|mins?|hours?|hrs?)\b",
            text, re.IGNORECASE,
        )
        if not word:
            return None
        amount = float(_WORD_NUMS[word.group(1).lower()])
        unit = word.group(2).lower()

    if unit.startswith(("s", "sec")):
        return timedelta(seconds=amount)
    if unit.startswith(("h", "hr")):
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


@skill(
    name="timer.set",
    tier="L1",
    action="modify",
    description="Set a timer that notifies you when it finishes",
    examples=[
        "set a timer for 10 minutes",
        "timer for 5 minutes",
        "wake me in 20 minutes",
        "remind me in half an hour",
        "set a 45 second timer",
        "ping me in an hour",
        "alert me in 15 minutes",
    ],
)
def set_timer(
    duration: Annotated[str, "how long, e.g. '10 minutes'"],
    label: Annotated[str, "optional name for the timer"] = "",
) -> SkillResult:
    delta = _parse_duration(duration)
    if delta is None or delta.total_seconds() <= 0:
        return SkillResult(
            speech="How long should the timer be? Try '10 minutes'.", ok=False
        )

    from friday import jobs

    fires_at = datetime.now() + delta
    name = (label or f"timer {fires_at.strftime('%H:%M')}").strip()[:40]

    total = delta.total_seconds()
    spoken = (
        f"{int(total)} seconds" if total < 60
        else f"{int(total // 60)} minutes" if total < 3600
        else f"{total / 3600:.1f} hours"
    )

    # A timer is just a one-shot job whose action is a notification.
    # `duration` arrives as the whole utterance, so label the job with the
    # parsed value — otherwise the jobs list reads "in set a timer for 1 min".
    jobs.create(
        name, "once",
        {"run_at": fires_at.isoformat(), "human": f"in {spoken}", "notify": False},
        [{"skill": "notify.send",
          "args": {"title": name, "message": f"Timer finished ({spoken})."}}],
    )
    jobs.SCHEDULER.reload()
    return SkillResult(
        speech=f"Timer set for {spoken}. I'll let you know at "
        f"{fires_at.strftime('%I:%M %p').lstrip('0')}.",
        data={"fires_at": fires_at.isoformat(), "seconds": total},
    )


@skill(
    name="notify.send",
    tier="L1",
    action="modify",
    description="Show a desktop notification",
    examples=["notify me", "send me a notification", "pop up a message"],
)
def send_notification(
    title: Annotated[str, "notification heading"] = "FRIDAY",
    message: Annotated[str, "notification body"] = "",
) -> SkillResult:
    shown = notify.send(title, message)
    return SkillResult(
        speech=message or title,
        ok=True,
        data={"displayed": shown},
    )


@skill(
    name="weather.now",
    tier="L0",
    description="Report the current weather",
    examples=[
        "what's the weather",
        "how's the weather outside",
        "is it going to rain",
        "what's the temperature",
        "weather in mumbai",
        "do i need a jacket",
        "how hot is it",
    ],
)
def weather(
    location: Annotated[str, "city name, blank for your current location"] = "",
) -> SkillResult:
    place = location.strip()
    # wttr.in resolves an empty path by IP, which is what we want by default.
    url = f"https://wttr.in/{place}?format=j1"

    try:
        response = httpx.get(
            url, timeout=15.0, headers={"User-Agent": "curl/8.0"},
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return SkillResult(speech=f"I couldn't get the weather: {exc}", ok=False)

    try:
        current = data["current_condition"][0]
        area = data["nearest_area"][0]["areaName"][0]["value"]
        temp = current["temp_C"]
        feels = current["FeelsLikeC"]
        desc = current["weatherDesc"][0]["value"]
        humidity = current["humidity"]
        today = data["weather"][0]
        low, high = today["mintempC"], today["maxtempC"]
        rain_chance = max(
            (int(h.get("chanceofrain", 0)) for h in today.get("hourly", [])),
            default=0,
        )
    except (KeyError, IndexError):
        return SkillResult(speech="The weather service returned something odd.", ok=False)

    speech = (
        f"{desc.lower().strip()} in {area}, {temp} degrees, feels like {feels}. "
        f"Today {low} to {high}."
    )
    if rain_chance >= 40:
        speech += f" {rain_chance} percent chance of rain."

    return SkillResult(
        speech=speech,
        data={
            "area": area, "temp_c": temp, "feels_like_c": feels,
            "description": desc, "humidity": humidity,
            "min_c": low, "max_c": high, "rain_chance": rain_chance,
        },
    )


@skill(
    name="notes.add",
    tier="L1",
    action="modify",
    description="Append a note to your notes file",
    examples=[
        "make a note that the meeting moved to tuesday",
        "add a note about the budget",
        "jot this down",
        "write that down for me",
        "note this",
    ],
)
def add_note(
    text: Annotated[str, "the note to write"],
) -> SkillResult:
    body = re.sub(
        r"^\s*(please\s+)?(make|add|write|jot|take)\s+(a\s+)?(note|this)\s*"
        r"(that|about|down|:)?\s*",
        "", text, flags=re.IGNORECASE,
    ).strip()
    body = re.sub(r"^\s*(down|that)\s+", "", body, flags=re.IGNORECASE).strip()

    if not body:
        return SkillResult(speech="What should the note say?", ok=False)

    paths.ensure()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with NOTES_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"- [{stamp}] {body}\n")

    return SkillResult(speech="Noted.", data={"note": body, "file": str(NOTES_FILE)})


@skill(
    name="notes.read",
    tier="L0",
    description="Read back your recent notes",
    examples=[
        "read my notes",
        "what are my notes",
        "show me what i noted",
        "what did i write down",
    ],
)
def read_notes(
    count: Annotated[int, "how many recent notes to read"] = 5,
) -> SkillResult:
    if not NOTES_FILE.exists():
        return SkillResult(speech="You haven't written any notes yet.")

    lines = [
        line.strip() for line in
        NOTES_FILE.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not lines:
        return SkillResult(speech="Your notes file is empty.")

    recent = lines[-count:]
    stripped = [re.sub(r"^- \[[^\]]+\]\s*", "", line) for line in recent]

    return SkillResult(
        speech=f"Your last {len(recent)} notes: " + "; ".join(stripped),
        data={"notes": recent, "total": len(lines)},
    )


@skill(
    name="math.calculate",
    tier="L0",
    description="Evaluate an arithmetic expression",
    examples=[
        "what's 15 percent of 2400",
        "calculate 47 times 89",
        "what is 2 to the power of 20",
        "how much is 1250 divided by 8",
        "add 340 and 195",
        "what's the square root of 576",
    ],
)
def calculate(
    expression: Annotated[str, "the sum to work out"],
) -> SkillResult:
    import ast
    import math
    import operator

    text = expression.lower()
    text = re.sub(r"^.*?(what'?s?|calculate|compute|how much is|work out)\s+", "", text)

    # Spoken arithmetic -> symbols.
    replacements = [
        (r"\bpercent of\b", "% of"),
        (r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*"),
        (r"\b(times|multiplied by)\b", "*"),
        (r"\b(divided by|over)\b", "/"),
        (r"\b(plus|and)\b", "+"),
        (r"\b(minus|less|subtract)\b", "-"),
        (r"\bto the power of\b", "**"),
        (r"\bsquared\b", "**2"),
        (r"\bcubed\b", "**3"),
        (r"\bsquare root of\b", "sqrt"),
        (r"\bpi\b", str(math.pi)),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)

    text = re.sub(r"sqrt\s*\(?\s*([\d.]+)\s*\)?", r"(\1)**0.5", text)
    text = re.sub(r"[^0-9+\-*/().% ]", "", text).strip()

    if not text or not re.search(r"\d", text):
        return SkillResult(speech="I couldn't find a sum in that.", ok=False)

    # ast.literal_eval can't do arithmetic, so walk a restricted node set —
    # never eval(), which would execute anything the matcher misroutes here.
    ops = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow,
        ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos,
    }

    def evaluate(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](evaluate(node.left), evaluate(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](evaluate(node.operand))
        raise ValueError("unsupported expression")

    try:
        value = evaluate(ast.parse(text, mode="eval").body)
    except Exception:
        return SkillResult(speech=f"I couldn't work out '{expression}'.", ok=False)

    if isinstance(value, float):
        rendered = f"{value:.4f}".rstrip("0").rstrip(".")
    else:
        rendered = f"{value:,}"

    return SkillResult(speech=rendered, data={"expression": text, "result": value})
