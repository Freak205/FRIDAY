"""Phase 64 -- credentials do not leave the process through bus events, log lines or failure-result data.

Traced (synthetic secrets): the daemon's `/ws` forwards every bus event to every connected client unfiltered. Leaks reproduced before the change --
`skill.start` / `orchestrator.step` / `orchestrator.continuation` carried the raw `args` (typed text, message bodies, a `password` field);
`orchestrator.decision` carried `raw_first` / `raw_last`, the planner model's whole reply (tool AND arguments); `session.heard`, `session.unknown`,
`orchestrator.start` (`goal`) and `permission.confirm_requested` (`preview`, which quotes the call) carried free text with "my password is X" in it.
Persistent logs: `log.debug("bus %s", event)` printed the whole event dict at DEBUG, and every `log.exception` wrote the exception message and traceback
(an HTTP error quotes its URL, a token may follow) to `friday.log`. Failure results returned `data` unchecked to API clients (`/invoke`, `/say`).

Phase 64: ONE outbound boundary in `EventBus.publish` (`auditview.sanitize_event`, a copy for stream consumers; in-process subscribers keep the original),
a `RedactingFilter` on every root log handler (`friday.log`), no payload in the debug bus line, and `auditview.scrub_tree` on a failing result's `data`
at `Executor.run` / `run_verified`. They reuse the existing helpers (`sanitize_args`, `scrub_free_text`, `redact_credentials`, `safe_speech`).
Deliberately unchanged: the `speech` of a reply (read-back content the user asked for), successful results' data, internal execution state.
Synthetic secrets; throwaway databases; header-level test client; no real network. Recognisable credential patterns only -- an unmarked secret is not caught.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from friday import auditview, daemon, toolview, verify  # noqa: E402
from friday import log as friday_log  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

S = "zq7-SYNTH-secret-8841"
TOKEN = "test-only-token-0123456789-abcdefghijklmnop-XYZ"


@skill(name="test.ev_fail", tier="L0", description="Phase 64 fixture: a failure whose data carries text")
def ev_fail() -> SkillResult:
    return SkillResult(speech="Could not do it.", ok=False, data={
        "detail": f"upstream said: my password is {S}", "path": "a.txt", "count": 3, "recoverable": True, "stranded_snapshot": "20260101-ab12cd34",
        "nested": {"note": f"API key: {S}", "items": [f"token={S}", "fine"]}})


@skill(name="test.ev_read", tier="L0", description="Phase 64 fixture: successful read-back")
def ev_read() -> SkillResult:
    return SkillResult(speech=f"Read: my password is {S}", data={"text": f"my password is {S}"})


@skill(name="test.ev_set", tier="L1", action="modify", description="Phase 64 fixture: a state change whose read-back fails")
def ev_set() -> SkillResult:
    return SkillResult(speech="Done.")


verify.register("test.ev_set", verify.Verifier(lambda a, d, b: [verify.Check("setting", False, "the new value", f"found my password is {S}")]))


class Grab(logging.Handler):
    """A handler as `friday.log.setup` builds them: formatter + the project's filter. Collects the final formatted text."""

    def __init__(self, with_filter: bool = True) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        if with_filter:
            self.addFilter(friday_log.RedactingFilter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def main() -> int:
    t0 = time.perf_counter()
    REGISTRY.discover()
    with iso_env.isolated_runtime():
        asyncio.run(run())
    return finish("Phase 64 -- events, logs and failure data", time.perf_counter() - t0, min_assertions=30, min_scenarios=6)


async def external(topic: str, **data) -> dict:
    """Publish and return what a stream consumer receives; also what an in-process subscriber received."""
    seen: dict = {}

    async def handler(event):
        seen.update(event.data)

    BUS.subscribe(topic, handler)
    q = BUS.stream()
    try:
        await BUS.publish(topic, **data)
        got = q.get_nowait()
    finally:
        BUS.unsubscribe(topic, handler)
        BUS.drop(q)
    assert got.topic == topic
    return {"external": got.data, "internal": seen}


async def run() -> None:
    scenario("A. stream consumers receive a sanitized copy; in-process subscribers keep the original")
    r = await external("skill.start", skill="input.type", args={"text": f"my password is {S}", "password": S})
    check("A1 skill.start: typed text and a password field never reach the stream", S not in json.dumps(r["external"]), str(r["external"]))
    check("A2 ...but the skill identity and the argument names do", r["external"]["skill"] == "input.type" and set(r["external"]["args"]) == {"text", "password"})
    check("A3 the in-process subscriber still gets the raw arguments (execution state untouched)", r["internal"]["args"]["text"] == f"my password is {S}")
    r = await external("skill.start", skill="files.write", args={"path": "notes.txt", "content": f"my API key is {S}"})
    check("A4 a file path (what was touched) survives, the body does not", r["external"]["args"]["path"] == "notes.txt" and S not in json.dumps(r["external"]))
    r = await external("orchestrator.step", tool="whatsapp.compose", args={"contact": "Sam", "message": f"password: {S}"})
    check("A5 orchestrator.step: the message body is omitted, the tool and contact stay", S not in json.dumps(r["external"]) and r["external"]["tool"] == "whatsapp.compose"
          and r["external"]["args"]["contact"] == "Sam", str(r["external"]))
    r = await external("orchestrator.continuation", goal="read it", tool="files.read", args={"path": "a.txt", "offset": 40})
    check("A6 orchestrator.continuation: structure and numbers survive", r["external"]["args"] == {"path": "a.txt", "offset": 40}, str(r["external"]))
    r = await external("orchestrator.decision", valid=True, reason="", repaired=False, model_calls=1, ms=812, raw_first=f'{{"action":"call","tool":"input.type","args":{{"text":"{S}"}}}}',
                       raw_last="ok", budget={"est_tokens": 1200})
    check("A7 orchestrator.decision: the planner's raw reply is replaced by its length", S not in json.dumps(r["external"]) and r["external"]["raw_first"].startswith("[omitted:"))
    check("A8 ...and the progress fields stay useful", r["external"]["valid"] is True and r["external"]["ms"] == 812 and r["external"]["budget"] == {"est_tokens": 1200})
    r = await external("session.heard", text=f"please remember my password is {S}", actor="text")
    check("A9 session.heard: the utterance's credential is redacted around its words", S not in json.dumps(r["external"]) and r["external"]["text"].startswith("please remember my password")
          and r["external"]["actor"] == "text", str(r["external"]))
    r = await external("session.heard", text="the pass is open and authorization is pending", actor="voice")
    check("A10 benign text is preserved verbatim", r["external"]["text"] == "the pass is open and authorization is pending")
    r = await external("orchestrator.start", goal=f"log in, my API key is {S}", actor="text", mode="llm")
    check("A11 a goal with a credential is scrubbed", S not in json.dumps(r["external"]) and r["external"]["mode"] == "llm")
    r = await external("permission.confirm_requested", skill="input.type", tier="L2", preview=f"input.type(text='my password is {S}')")
    check("A12 a confirmation preview quoting the call is scrubbed, the tier stays", S not in json.dumps(r["external"]) and r["external"]["tier"] == "L2")
    r = await external("skill.error", skill="web.fetch", error=f"HTTP 401 for https://api.test/x?api_key={S}")
    check("A13 a URL query token is stripped", S not in json.dumps(r["external"]) and "api.test/x" in r["external"]["error"])
    r = await external("future.topic", detail={"x": [f"token = {S}", {"y": f"my password is {S}"}]}, n=3, ok=True)
    check("A14 an event type nobody has thought of yet is covered by the same boundary (nested strings)", S not in json.dumps(r["external"]) and r["external"]["n"] == 3 and r["external"]["ok"] is True)
    r = await external("skill.done", skill="test.ev_read", speech=f"Read: my password is {S}", ms=4)
    check("A15 the reply's own speech (read-back the user asked for) is delivered as before", S in r["external"]["speech"] and r["external"]["ms"] == 4)

    scenario("B. sanitizer failure fails closed")
    real_scrub, real_args = toolview.redact_credentials, auditview.sanitize_args

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    toolview.redact_credentials = boom  # type: ignore[assignment]
    try:
        r = await external("session.heard", text=f"my password is {S}", actor="text")
    finally:
        toolview.redact_credentials = real_scrub  # type: ignore[assignment]
    check("B1 text that cannot be checked is replaced by the fixed notice, not passed through", S not in json.dumps(r["external"]) and "could not be scrubbed" in r["external"]["text"])
    auditview.sanitize_args = boom  # type: ignore[assignment]
    try:
        r = await external("skill.start", skill="input.type", args={"text": S})
    finally:
        auditview.sanitize_args = real_args  # type: ignore[assignment]
    check("B2 an event whose arguments cannot be sanitized loses its payload entirely", S not in json.dumps(r["external"]) and "withheld" in r["external"] and "args" not in r["external"], str(r["external"]))
    check("B3 in-process subscribers were still served normally in both cases", r["internal"]["args"] == {"text": S})

    scenario("C. the debug bus line no longer prints payloads")
    bus_log = logging.getLogger("friday.bus")
    grab = Grab(with_filter=False)  # no filter: prove the call site itself no longer writes the payload
    old_level = bus_log.level
    bus_log.setLevel(logging.DEBUG)
    bus_log.addHandler(grab)
    try:
        await BUS.publish("skill.start", skill="input.type", args={"text": f"my password is {S}"})
        await BUS.publish("session.heard", text=f"token: {S}", actor="text")
    finally:
        bus_log.removeHandler(grab)
        bus_log.setLevel(old_level)
    check("C1 even with filtering off and DEBUG on, the log says what happened (topic, field names) and no payload",
          grab.lines and S not in grab.text and "skill.start" in grab.text and "args" in grab.text, grab.text)

    scenario("D. exception messages and tracebacks in logs")
    lg = logging.getLogger("friday.test_log_privacy")
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    h = Grab()
    lg.addHandler(h)

    def risky():
        raise RuntimeError(f"upstream said: token={S} and my API key is {S}")

    try:
        risky()
    except RuntimeError:
        lg.exception("skill %s failed", "web.fetch")
    out = h.text
    check("D1 the exception message is redacted", S not in out, out)
    check("D2 the type, the call site and the message framing are kept", "RuntimeError" in out and "risky" in out and "Traceback" in out and "skill web.fetch failed" in out
          and "upstream said" in out, out)
    lg.warning("fetch failed: GET https://api.test/data?api_key=%s&x=1", S)
    lg.info("the pass is open; authorization is pending; timer set for 5 minutes")
    lg.warning("Authorization: Bearer %s", S)
    os.environ[daemon.TOKEN_ENV] = TOKEN
    lg.warning("startup saw %s in the environment", TOKEN)
    lg.warning("a stack follows", stack_info=True)
    os.environ.pop(daemon.TOKEN_ENV, None)
    out = h.text
    check("D3 a URL's query token, a bearer header and the configured daemon token never appear", S not in out and TOKEN not in out, out)
    check("D4 ordinary log text is preserved", "the pass is open; authorization is pending; timer set for 5 minutes" in out and "api.test/data" in out)
    check("D5 the level and logger name of every line are kept", all(ln.startswith(("ERROR", "WARNING", "INFO")) and "friday.test_log_privacy" in ln for ln in out.splitlines() if ln[:4] in ("ERRO", "WARN", "INFO")))
    real_scrub_ft = auditview.scrub_free_text
    auditview.scrub_free_text = boom  # type: ignore[assignment]
    try:
        lg.warning("login failed, my password is %s", S)
        try:
            risky()
        except RuntimeError:
            lg.exception("again")
    finally:
        auditview.scrub_free_text = real_scrub_ft  # type: ignore[assignment]
    check("D6 if the check fails the line keeps its level and logger but not its text", S not in h.text and friday_log.WITHHELD_LOG in h.lines[-1] and h.lines[-1].startswith("ERROR friday.test_log_privacy"), h.lines[-1])
    lg.removeHandler(h)
    root_handlers = logging.getLogger().handlers
    check("D7 the handlers `friday.log.setup` installed (console and file) carry the filter", len(root_handlers) >= 2 and all(
        any(isinstance(f, friday_log.RedactingFilter) for f in hd.filters) for hd in root_handlers))

    scenario("E. failure-result data")
    r = await SESSION._run("test.ev_fail", {}, actor="text")
    api = await daemon.invoke(daemon.Invoke(skill="test.ev_fail"))
    for label, data in (("Session reply", r.data), ("/invoke", api["data"])):
        check(f"E1 {label}: no credential anywhere in the data, nested lists and dicts included", S not in json.dumps(data), json.dumps(data))
        check(f"E2 {label}: keys, numbers, flags, ids and ordinary text are intact",
              data["path"] == "a.txt" and data["count"] == 3 and data["recoverable"] is True and data["stranded_snapshot"] == "20260101-ab12cd34"
              and data["nested"]["items"][1] == "fine" and data["detail"].startswith("upstream said"), json.dumps(data))
    ok = await SESSION._run("test.ev_read", {}, actor="text")
    check("E3 a SUCCESSFUL result's data is not touched (read-back is what the user asked for)", S in json.dumps(ok.data))
    r = await SESSION._run("test.ev_set", {}, actor="text")
    check("E4 a direct state change whose read-back failed: the data quoting what was read back is checked", r.ok is False and S not in json.dumps(r.data, default=str)
          and "verification" in r.data, json.dumps(r.data, default=str)[:200])

    async def decline(skill_, args, preview):
        return False

    from friday.registry import skill as skill_deco  # noqa: F401

    EXECUTOR.set_confirm_handler(decline)
    declined = await EXECUTOR.run("system.shutdown", {"mode": "restart"}, actor="text")
    check("E5 internal flags survive: a declined confirmation still reports confirmation_declined", declined.data.get("confirmation_declined") is True and declined.ok is False)

    scenario("F. end to end through the daemon's websocket (local mode, header-level client)")
    with TestClient(daemon.app, base_url="http://127.0.0.1:8765") as c:
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8765"}) as ws:
            ws.send_json({"text": f"what time is it, my password is {S}"})
            frames = []
            for _ in range(80):
                m = ws.receive_json()
                frames.append(m)
                if m["topic"] == "result":
                    break
    streamed = json.dumps([f for f in frames if f["topic"] != "result"])
    check("F1 nothing the daemon streamed while handling an utterance with a credential contains it", S not in streamed, streamed[:300])
    check("F2 the stream is still useful: heard / understood / skill events arrive with topics and times",
          {"session.heard"} <= {f["topic"] for f in frames} and all("at" in f for f in frames if f["topic"] != "result"))


if __name__ == "__main__":
    sys.exit(main())
