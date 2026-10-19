"""Phase 59 -- the planner's scope prefilter (`intent.offered_for`) never hides a tool the intent guard would allow, and no longer offers
tools the guard is certain to refuse.

Traced: `offered_for` keeps a tool when one of its possible action classes is reachable from the goal's scope (read/observe always,
plus the allowed and supporting classes). That is a superset of what `check_alignment` permits -- swept below over 40 goals x every tool
x argument variants, zero violations. One thing it did not mirror: `check_alignment` refuses a "targeted" class (system change,
communicate) unless the goal is about the tool's own domain, so for "set the volume to 30" it offered system.lock and the brightness / wifi
toggles -- tools every valid call of which is refused, each a wasted intent rejection (three end the run) and ~60 characters of catalog.
Tools that take no text argument decide that on the tool alone (`intent._never_aligned`); nothing else is touched.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phase21_common import check, finish, scenario  # noqa: E402

from friday import intent  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402

REGISTRY.discover()
TOOLS = [(s.name, s.tier) for s in REGISTRY.all() if not s.internal and s.name != "plan.run"]
NAMES = {n for n, _ in TOOLS}

GOALS = [
    "what time is it", "click Delete", "click the Send button", "press the Back button", "right-click Reply", "drag report.csv onto Trash",
    "type hello into the search box", "git commit my changes", "run git status", "git push", "open chrome and click Sign in",
    "fix the flask bug", "click OK", "hit Enter", "press ctrl+s to save", "close the notepad window", "switch to desktop 2", "scroll down",
    "snap this window to the left", "play some music", "", "Chrome", "tell me the weather and then email it", "delete report.csv",
    "move a.txt to the archive folder", "copy the file to D:", "write notes to a.txt", "what is 2 plus 2", "show recent actions",
    "remember that I like tea", "forget what I told you about tea", "schedule a daily backup", "cancel the timer", "restart my computer",
    "mute the volume", "navigate to github.com", "focus the terminal", "read my clipboard then clear it", "download the report",
    "type my name in the form and submit it", "set the volume to 30", "turn the brightness down and lock my pc", "message Sam on whatsapp",
    "lock my pc", "turn off the wifi", "set the volume to 30 and send Sam a message that I'm late",
]
ARGS = {
    "label": ["Delete", "Send", "Back", "OK", "Reply", "Sign in", "Trash", "Search", "Submit", "Buy now"],
    "target": ["Delete", "Send", "Back", "OK", "Reply", "Sign in", "Trash", "Search", "Submit", "Buy now"],
    "query": ["Delete", "Send"], "key": ["enter", "ctrl+s", "delete", "f5", "escape"], "combo": ["ctrl+s", "alt+f4", "enter", "ctrl+c"],
    "subcommand": ["status", "commit", "push", "log", "branch", "checkout main", "reset --hard"], "source": ["report.csv"],
    "app": ["chrome", "notepad"], "mode": ["shutdown", "restart"], "contact": ["Sam"], "plan": ["balanced"], "name": ["volume", "lock"],
}


def variants(tool: str) -> list[dict]:
    out: list[dict] = [{}]
    for p in REGISTRY.get(tool).params:
        if p.name in ARGS:
            out = [dict(v, **{p.name: val}) for v in out for val in ARGS[p.name]][:200]
    return out


def offered(goal: str) -> set[str]:
    return intent.offered_for(intent.derive_scope(goal), TOOLS)


def main() -> int:
    t0 = time.perf_counter()

    scenario("A. soundness: the prefilter never hides a tool the guard would allow")
    bad: list[str] = []
    swept = 0
    for g in GOALS:
        sc = intent.derive_scope(g)
        keep = intent.offered_for(sc, TOOLS)
        for name, tier in TOOLS:
            if name in keep:
                continue
            for a in variants(name):
                swept += 1
                if intent.check_alignment(sc, name, a, tier_hint=tier).aligned:
                    bad.append(f"{g!r}: {name} {a}")
                    break
    check(f"A1 over {len(GOALS)} goals x {len(TOOLS)} tools x argument variants ({swept} hidden-tool calls) the guard allows none of what is hidden",
          not bad, str(bad[:3]))
    check("A2 read and observe tools are offered for every goal, however narrow",
          all({n for n, t in TOOLS if intent.possible_classes(n, tier_hint=t) <= intent.PASSIVE} <= offered(g) for g in GOALS))
    check("A3 the prefilter only ever removes: its result is within the catalog and the same for the same goal",
          all(offered(g) <= NAMES and offered(g) == offered(g) for g in GOALS))

    scenario("B. what the goal asks for stays available")
    expect = {
        "set the volume to 30": {"system.volume.set", "system.volume.up", "system.volume.down", "system.volume.mute"},
        "mute the volume": {"system.volume.mute"},
        "lock my pc": {"system.lock"},
        "turn the brightness down": {"system.brightness.set", "system.brightness.down"},
        "turn off the wifi": {"network.wifi.toggle"},
        "message Sam on whatsapp": {"whatsapp.compose", "whatsapp.send"},
        "fix the flask bug": {"files.write", "files.read", "shell.run"},
        "open chrome": {"apps.open"},
        "delete report.csv": {"files.delete"},
    }
    for goal, tools in expect.items():
        missing = tools - offered(goal)
        check(f"B1 {goal!r}: {sorted(tools)[0]}{' and others' if len(tools) > 1 else ''} still offered", not missing, str(missing))
    both = offered("set the volume to 30 and lock my pc")
    check("B2 a compound goal keeps the tools of BOTH parts", {"system.volume.set", "system.lock"} <= both)
    both = offered("turn the brightness down, mute the volume and message Sam on whatsapp")
    check("B3 ...across three parts, system and communicate alike",
          {"system.brightness.down", "system.volume.mute", "whatsapp.compose"} <= both)
    check("B4 a personal-memory request keeps the memory tools", {"memory.remember", "memory.recall"} <= offered("remember that my wifi code is on the fridge"))
    check("B5 a read-only question offers read / observe tools only",
          all(intent.possible_classes(n, tier_hint=dict(TOOLS)[n]) & intent.PASSIVE for n in offered("what time is it")))

    scenario("C. what is no longer offered, and why that is safe")
    vol = offered("set the volume to 30")
    gone = {"system.lock", "system.brightness.set", "system.brightness.up", "system.brightness.down", "network.wifi.toggle", "whatsapp.send"}
    check("C1 a volume goal no longer offers the unrelated system toggles or the send tool", not (gone & vol), str(gone & vol))
    sc = intent.derive_scope("set the volume to 30")
    check("C2 ...and the guard really refuses each of them, with any arguments",
          all(not intent.check_alignment(sc, n, a, tier_hint=dict(TOOLS)[n]).aligned for n in gone if n in NAMES for a in variants(n)))
    check("C3 the catalog the planner sees shrank for that goal (previously all of them were offered)", len(vol) <= len(NAMES) - 5, f"{len(vol)}/{len(NAMES)}")

    scenario("D. when the scope is unknown or ambiguous nothing is hidden by this rule")
    check("D1 no scope = every tool", intent.offered_for(None, TOOLS) == NAMES)
    unspecified = intent.derive_scope("Chrome")
    check("D2 an unclassified statement (basis 'unspecified') keeps its conservative low-impact set and hides no passive tool",
          unspecified.basis == "unspecified" and {n for n, t in TOOLS if intent.possible_classes(n, tier_hint=t) <= intent.PASSIVE} <= intent.offered_for(unspecified, TOOLS))
    check("D3 an empty goal is read-only: only passive tools", all(intent.possible_classes(n, tier_hint=dict(TOOLS)[n]) & intent.PASSIVE for n in offered("")))
    check("D4 tools that take text keep being offered whenever their class is allowed (an argument may name the goal's subject)",
          {"system.power_plan", "system.shutdown", "whatsapp.compose"} <= offered("set the volume to 30 and message Sam on whatsapp")
          and {"system.power_plan", "system.shutdown"} <= offered("set the volume to 30"))
    check("D5 a click / key rule tool is never decided here: still offered for an explicit-click goal",
          {"ui.click", "browser.click"} <= offered("click Delete"))
    check("D6 the guard is untouched: system.lock under a volume goal is still a mismatch, and under its own goal still allowed",
          not intent.check_alignment(sc, "system.lock", {}, tier_hint="L1").aligned
          and intent.check_alignment(intent.derive_scope("lock my pc"), "system.lock", {}, tier_hint="L1").aligned)

    return finish("Phase 59 -- scope prefilter soundness", time.perf_counter() - t0, min_assertions=24, min_scenarios=4)


if __name__ == "__main__":
    sys.exit(main())
