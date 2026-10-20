"""Phase 61 -- direct (single-skill) replies: failure text is credential-checked, read-back content is deliberately not.

Traced: a direct skill call reaches the user through `Session._run` (GUI, voice, `/say`, the websocket `result`), or through
`EXECUTOR.run` itself (`/invoke`, plan steps, jobs); both end in `Executor.run`, which also publishes `skill.done` / `skill.error` to
the event bus -- and the bus is streamed, unfiltered, to every websocket client. Reproduced with synthetic secrets before changing
anything:
  * a skill that RAISES: `Session._run` spoke "That failed: <exception text>" and `skill.error` broadcast it -- library text
    ("upstream said token=S ... my API key is S") the user never asked to hear;
  * a skill that FAILS with a message built from a library error ("HTTP 401 for https://...?api_key=S"): `/invoke`, the Session reply and the
    `skill.done` event all carried it;
  * a skill that SUCCEEDS with read-back content ("The clipboard holds: my password is S"): same three -- by design, that is what the user asked for.
Phase 61 closes the first two (the failure branch of `Executor.run`, the exception branch of `Session._run`, the event text, the verification
failure message of `run_verified`) with the existing `toolview.safe_speech`, and leaves the third as it is.
Synthetic secrets; throwaway databases; nothing real runs.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import daemon, toolview, verify  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

S = "zq7-SYNTH-secret-8841"
WORLD: dict = {}
RAN: list[str] = []


@skill(name="test.do_raise", tier="L0", description="Phase 61 fixture: raises a library-style error")
def do_raise() -> SkillResult:
    RAN.append("raise")
    raise RuntimeError(WORLD["exc"])


@skill(name="test.do_fail", tier="L0", description="Phase 61 fixture: fails with a message")
def do_fail() -> SkillResult:
    RAN.append("fail")
    return SkillResult(speech=WORLD["fail"], ok=False, data={"path": "a.txt"})


@skill(name="test.do_read", tier="L0", description="Phase 61 fixture: reads content back")
def do_read() -> SkillResult:
    RAN.append("read")
    return SkillResult(speech=WORLD["read"], data={"text": WORLD["read"]})


@skill(name="test.do_set", tier="L1", action="modify", description="Phase 61 fixture: a state change whose read-back fails")
def do_set() -> SkillResult:
    RAN.append("set")
    return SkillResult(speech="Done.")


verify.register("test.do_set", verify.Verifier(
    lambda a, d, b: [verify.Check("setting", False, "the new value", f"found my password is {S}")]
))

LEAKY_FAILS = [
    f"I couldn't fetch it: HTTP 401 for https://api.test/x?api_key={S}", f"Login failed: my password is {S}", f"Server said: token = {S}",
    f"Could not connect. API key: {S}", f"Use {S} as the password and retry", f"Authorization: Bearer {S}", f"client secret is {S}",
]
BENIGN_FAILS = [
    "File not found: report.csv", "the pass is open this week", "authorization is pending", "I couldn't open the password manager.",
    "Timed out after 30 seconds.", "Access is denied: C:\\Users\\me\\Documents",
]


def main() -> int:
    t0 = time.perf_counter()
    with iso_env.isolated_runtime():
        asyncio.run(run())
    return finish("Phase 61 -- direct reply credential handling", time.perf_counter() - t0, min_assertions=15, min_scenarios=6)


async def direct(name: str) -> tuple[SkillResult, dict, list]:
    """The three ways a direct call is answered: Session (GUI/voice/say), /invoke (API), and what the event stream saw meanwhile."""
    q = BUS.stream()
    try:
        via_session = await SESSION._run(name, {}, actor="text")
        via_api = await daemon.invoke(daemon.Invoke(skill=name))
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    finally:
        BUS.drop(q)
    return via_session, via_api, events


def event_leaks(events: list) -> list[str]:
    return [e.topic for e in events if S in json.dumps(e.data, default=str)]


async def run() -> None:
    REGISTRY.discover()

    scenario("A. a failure message built from a library error is credential-checked, wherever it goes")
    leaks = []
    for msg in LEAKY_FAILS:
        WORLD["fail"] = msg
        r, api, ev = await direct("test.do_fail")
        if S in r.speech or S in json.dumps(api, default=str) or event_leaks(ev):
            leaks.append(msg)
    check(f"A1 none of {len(LEAKY_FAILS)} credential patterns survives in the Session reply, the /invoke reply or the event stream", not leaks, str(leaks[:2]))
    WORLD["fail"] = f"I couldn't fetch it: HTTP 401 for https://api.test/x?api_key={S}"
    r, api, ev = await direct("test.do_fail")
    check("A2 the failure is still a failure with its own words around the redaction", r.ok is False and api["ok"] is False and "I couldn't fetch it" in r.speech and "[redacted]" in r.speech, r.speech)
    check("A3 the data of the failure is untouched", r.data == {"path": "a.txt"} and api["data"] == {"path": "a.txt"})
    check("A4 the websocket-bound skill.done event carries the checked text too", any(e.topic == "skill.done" and "[redacted]" in e.data.get("speech", "") for e in ev))
    from friday import store
    rows = [dict(x) for x in store.conn().execute("SELECT * FROM audit WHERE skill='test.do_fail'").fetchall()]
    check("A5 the audit rows hold no secret either (as before)", rows and S not in json.dumps(rows, default=str))

    scenario("B. an exception message is credential-checked in the reply and in skill.error")
    WORLD["exc"] = f"upstream said: token={S} and my API key is {S}"
    r, api_unused, ev = await direct_raise()
    check("B1 the spoken 'That failed: ...' reply holds no secret", S not in r.speech and r.speech.startswith("That failed:") and r.ok is False, r.speech)
    check("B2 the skill.error event (streamed to websocket clients) holds none", not event_leaks(ev) and any(e.topic == "skill.error" for e in ev))
    WORLD["exc"] = "disk is full"
    r, _, ev = await direct_raise()
    check("B3 an ordinary exception message is spoken as before", r.speech == "That failed: disk is full", r.speech)

    scenario("C. read-back content is NOT filtered: the user asked for exactly that")
    WORLD["read"] = f"The clipboard holds: my password is {S}"
    r, api, ev = await direct("test.do_read")
    check("C1 a successful reply keeps what the skill read back, in the Session reply, /invoke and data",
          S in r.speech and S in api["speech"] and S in json.dumps(api["data"]), r.speech)
    check("C2 (documented limit) the event stream still carries it, since the bus is not per-requester", "skill.done" in event_leaks(ev))

    scenario("D. ordinary failure messages are unchanged")
    changed = []
    for msg in BENIGN_FAILS:
        WORLD["fail"] = msg
        r, api, _ = await direct("test.do_fail")
        if r.speech != msg or api["speech"] != msg:
            changed.append(msg)
    check("D1 benign failure text is identical in the Session reply and /invoke", not changed, str(changed))
    check("D2 toolview.safe_speech itself leaves them alone", all(toolview.safe_speech(m) == m for m in BENIGN_FAILS))
    WORLD["fail"] = "File not found: report.csv"
    r, _, _ = await direct("test.do_fail")
    check("D3 the failure flag and data survive", r.ok is False and r.data == {"path": "a.txt"})

    scenario("E. when the check itself fails, the failure text is withheld, not passed through")
    real = toolview.sanitize

    def broken(*_a, **_k):
        raise RuntimeError("boom")

    toolview.sanitize = broken  # type: ignore[assignment]
    try:
        WORLD["fail"] = f"Login failed: my password is {S}"
        r1, api1, ev1 = await direct("test.do_fail")
        WORLD["exc"] = f"token={S}"
        r2, _, ev2 = await direct_raise()
    finally:
        toolview.sanitize = real  # type: ignore[assignment]
    check("E1 a failing reply becomes the fixed notice in every destination", r1.speech == toolview.WITHHELD and api1["speech"] == toolview.WITHHELD and not event_leaks(ev1))
    check("E2 an exception reply becomes the fixed notice too, and skill.error carries no secret", r2.speech == f"That failed: {toolview.WITHHELD}" and not event_leaks(ev2))

    scenario("F. the post-condition failure message of a direct state change")
    RAN.clear()
    r = await SESSION._run("test.do_set", {}, actor="text")
    check("F1 'It did not take effect -- ...' reports the failed read-back without the credential it quotes",
          r.ok is False and "did not take effect" in r.speech and S not in r.speech, r.speech)
    check("F2 the skill itself ran once (nothing is repeated)", RAN == ["set"])


async def direct_raise():
    q = BUS.stream()
    try:
        r = await SESSION._run("test.do_raise", {}, actor="text")
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    finally:
        BUS.drop(q)
    return r, None, events


if __name__ == "__main__":
    sys.exit(main())
