"""Phase 65 -- what a remote WebSocket client may learn from the event stream.

Traced: `/ws` forwards the bus to every connected client; since Phase 64 every payload is credential-checked, but every authenticated client (one shared token,
no identity) still received every request's content: utterances (`session.heard`, `session.unknown`), tool arguments (`skill.start`, `orchestrator.step`,
`brain.understood`), goals, confirmation previews, the planner's reply (`orchestrator.decision`), reply speech and read-back (`skill.done`,
`session.awaiting_confirm`), window titles (`trigger.*`), proactive text and late results. Phase 65 replaces the pass-through with `friday.remote_events`:
an explicit allowlist of topics, each with an allowlist of strictly validated scalar fields; everything else -- unknown and future topics included -- is not
delivered. In-process subscribers keep the original events.

Synthetic secrets and payloads; the WebSocket is exercised through Starlette's TestClient (header level, no socket, no real network).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncio  # noqa: E402

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from friday import auditview, daemon, remote_events, toolview  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402

S = "zq7-SYNTH-secret-8841"
TOKEN = "test-only-token-0123456789-abcdefghijklmnop-XYZ"
CONTENT_FIELDS = {"args", "goal", "text", "speech", "preview", "raw", "raw_first", "raw_last", "detail", "description", "question", "message", "content",
                  "reason", "error", "summary", "options", "candidates", "unmet", "wanted", "suggestion", "reasons", "name", "job", "title", "budget"}

# every topic with a realistic payload carrying private content next to its operational fields
SAMPLES: dict[str, dict] = {
    "daemon.ready": {"skills": 107},
    "skill.start": {"skill": "files.write", "args": {"path": "a.txt", "content": f"my password is {S}"}},
    "skill.done": {"skill": "clipboard.read", "ms": 41, "speech": f"The clipboard holds: my password is {S}"},
    "skill.error": {"skill": "web.fetch", "error": f"HTTP 401 for https://x.test/?api_key={S}"},
    "permission.confirm_requested": {"skill": "input.type", "tier": "L2", "preview": f"input.type(text='my password is {S}')"},
    "permission.declined": {"skill": "input.type"},
    "permission.denied": {"skill": "shell.run", "reason": f"policy denies shell.run {S}"},
    "orchestrator.start": {"goal": f"log in, password: {S}", "actor": "text", "mode": "llm", "steps": 0},
    "orchestrator.step": {"tool": "input.type", "args": {"text": f"my password is {S}"}},
    "orchestrator.observation": {"tool": "files.read", "ms": 12, "blocked": False},
    "orchestrator.done": {"goal": f"password: {S}", "ok": False, "stopped": "failure", "actor": "text", "executed": 2},
    "orchestrator.cancelling": {"tool": "shell.run", "interruptible": False, "actor": "text"},
    "orchestrator.replan": {"goal": "g", "attempt": 1, "max_replans": 2, "reason": f"failed: token={S}"},
    "orchestrator.verification": {"tool": "system.volume.set", "status": "verified", "reason": f"found my password is {S}"},
    "orchestrator.decision": {"valid": True, "reason": "", "repaired": False, "repair_succeeded": False, "model_calls": 1, "ms": 900, "discovery": False,
                              "recovered": "", "raw_first": f'{{"tool":"input.type","args":{{"text":"{S}"}}}}', "raw_last": "x", "budget": {"est_tokens": 1000}},
    "orchestrator.subgoal": {"goal": "g", "index": 1, "description": f"type my password {S}"},
    "orchestrator.coverage_nudge": {"goal": "g", "nudges": 1, "unmet": [f"read {S}"]},
    "orchestrator.action_nudge": {"goal": "g", "nudges": 1, "wanted": [f"click {S}"]},
    "orchestrator.action_unfinished": {"goal": f"password: {S}", "wanted": ["x"]},
    "orchestrator.evidence_stop": {"goal": "g", "actor": "text", "steps": 3},
    "orchestrator.intent_mismatch": {"action": "delete", "args": {"path": S}, "goal": f"password: {S}", "rejections": 1, "repeated": False, "tool": "files.delete"},
    "orchestrator.tool_family": {"args": {"label": S}, "goal": "g", "rejections": 1, "repeated": True, "suggestion": f"use {S}", "tool": "ui.click"},
    "job.start": {"job": f"backup {S}", "id": 7},
    "job.done": {"job": f"backup {S}", "id": 7, "ok": True, "ms": 300, "detail": f"my password is {S}"},
    "session.heard": {"text": f"please remember my password is {S}", "actor": "voice"},
    "session.awaiting_confirm": {"actor": "text", "preview": f"type my password {S}", "skill": "input.type", "speech": f"Shall I type {S}?", "tier": "L2"},
    "session.stop_requested": {"declined_confirmation": True, "goal_running": True, "source": "gui"},
    "session.awaiting_slot": {"skill": "timer.set", "slot": "duration"},
    "session.recovery_route": {"kind": "list", "skill": "files.recoveries", "actor": "text"},
}
DENIED = {
    "brain.understood": {"action": "act", "skill": "memory.recall", "score": 0.7, "args": {"query": f"my password {S}"}},
    "session.unknown": {"text": f"blorp {S}"},
    "session.no_antecedent": {"actor": "text", "bare": True, "matched": "x", "text": f"it {S}"},
    "session.clarifying": {"options": ["a", S]},
    "session.context_ambiguous": {"candidates": [S], "reason": "r"},
    "session.scope_expansion": {"actor": "text", "goal_id": "g1", "text": f"yes fix {S}"},
    "session.direct_route_rejected": {"action": "a", "reasons": [S], "skill": "s"},
    "proactive.notice": {"action": "speak", "at": "now", "event_type": "window", "text": f"you have {S}"},
    "trigger.window.changed": {"title": f"Bank - {S}", "pid": 1},
    "trigger.process.started": {"name": f"{S}.exe"},
    "backend.late_result": {"speech": f"finished: {S}", "ok": True, "request_id": "r1", "actor": "voice"},
    "future.topic": {"skill": "ok", "ms": 1},
    "skill.start.extra": {"skill": "files.write"},
    "Skill.start": {"skill": "files.write"},
    "orchestrator.new_internal_event": {"tool": "files.read", "ms": 3},
}


def received(topic: str, data: dict) -> tuple[dict | None, dict]:
    """(what a stream consumer gets or None, what an in-process subscriber got) -- through the real bus."""
    seen: dict = {}

    async def handler(event):
        seen.update(event.data)

    async def go():
        BUS.subscribe(topic, handler)
        q = BUS.stream()
        try:
            await BUS.publish(topic, **data)
            got = q.get_nowait() if not q.empty() else None
        finally:
            BUS.unsubscribe(topic, handler)
            BUS.drop(q)
        return got

    got = asyncio.run(go())
    return (got.data if got is not None else None), seen


def frames(client: TestClient, headers: dict, text: str) -> list[dict]:
    out = []
    with client.websocket_connect("/ws", headers=headers) as ws:
        ws.send_json({"text": text})
        for _ in range(80):
            m = ws.receive_json()
            out.append(m)
            if m["topic"] == "result":
                break
    return out


def main() -> int:
    t0 = time.perf_counter()
    REGISTRY.discover()
    saved = CFG.daemon.allow_remote
    try:
        with iso_env.isolated_runtime():
            run()
    finally:
        CFG.daemon.allow_remote = saved
        os.environ.pop(daemon.TOKEN_ENV, None)
    return finish("Phase 65 -- remote event policy", time.perf_counter() - t0, min_assertions=30, min_scenarios=8)


def run() -> None:
    scenario("A. the policy table itself")
    bad = {t: sorted(set(f) & CONTENT_FIELDS) for t, f in remote_events.POLICY.items() if set(f) & CONTENT_FIELDS}
    check("A1 no allowed field is content-bearing by name (args, goal, text, speech, preview, raw*, detail, reason, ...)", not bad, str(bad))
    check("A2 every allowed field has a validator (a callable), none is a free pass-through", all(callable(v) for f in remote_events.POLICY.values() for v in f.values()))
    check("A3 every sample topic below is classified exactly once: allowed or denied", set(SAMPLES) == set(remote_events.POLICY) and not set(DENIED) & set(remote_events.POLICY),
          str(set(SAMPLES) ^ set(remote_events.POLICY)))

    scenario("B. each permitted event carries only its approved fields")
    problems, leaks, internal_changed = [], [], []
    for topic, payload in SAMPLES.items():
        ext, internal = received(topic, dict(payload))
        approved = set(remote_events.POLICY[topic])
        if ext is None or not set(ext) <= approved or any(k not in payload or ext[k] != payload[k] for k in ext):
            problems.append((topic, ext))
        if S in json.dumps(ext, default=str):
            leaks.append(topic)
        if internal != payload:
            internal_changed.append(topic)
    check(f"B1 all {len(SAMPLES)} permitted topics are delivered with a subset of their approved fields, values unchanged", not problems, str(problems[:2]))
    check("B2 none of them delivers the secret that sat next to the approved fields", not leaks, str(leaks))
    check("B3 in-process subscribers received every original payload unchanged (private fields included)", not internal_changed, str(internal_changed))
    ext, _ = received("skill.start", SAMPLES["skill.start"])
    check("B4 skill.start is the tool's name and nothing else", ext == {"skill": "files.write"}, str(ext))
    ext, _ = received("skill.done", SAMPLES["skill.done"])
    check("B5 skill.done carries the duration but neither the read-back nor any speech", ext == {"skill": "clipboard.read", "ms": 41}, str(ext))
    ext, _ = received("session.heard", SAMPLES["session.heard"])
    check("B6 session.heard says someone spoke (and as whom), not what", ext == {"actor": "voice"}, str(ext))
    ext, _ = received("orchestrator.decision", SAMPLES["orchestrator.decision"])
    check("B7 the planner decision event is status only: no raw reply, no budget dict", ext == {"valid": True, "repaired": False, "repair_succeeded": False, "model_calls": 1, "ms": 900, "discovery": False}, str(ext))
    ext, _ = received("permission.confirm_requested", SAMPLES["permission.confirm_requested"])
    check("B8 a confirmation request names the skill and tier, not the call", ext == {"skill": "input.type", "tier": "L2"}, str(ext))
    ext, _ = received("orchestrator.step", SAMPLES["orchestrator.step"])
    check("B9 orchestrator.step names the tool, never its arguments", ext == {"tool": "input.type"}, str(ext))
    ext, _ = received("orchestrator.done", SAMPLES["orchestrator.done"])
    check("B10 the outcome of a goal (ok, stop reason, steps run) is visible without the goal text", ext == {"ok": False, "stopped": "failure", "actor": "text", "executed": 2}, str(ext))

    scenario("C. suppressed, unknown and look-alike topics never reach a stream consumer")
    delivered = {t: received(t, dict(p))[0] for t, p in DENIED.items()}
    check("C1 nothing at all is delivered for the suppressed, unknown, future and look-alike topics", all(v is None for v in delivered.values()), str({t: v for t, v in delivered.items() if v is not None}))
    check("C2 ...while in-process subscribers still got each of them", all(received(t, dict(p))[1] == p for t, p in DENIED.items()))
    check("C3 default deny: a topic is only remote after it is added to POLICY", remote_events.project("brand.new.event", {"skill": "x"}) is None)

    scenario("D. field validators drop anything that is not a plain scalar of the expected shape")
    P = remote_events.project
    check("D1 a nested payload in an allowed field is dropped, not cleaned", P("skill.start", {"skill": {"name": "x", "args": S}}) == {})
    check("D2 an over-long, spaced or credential-shaped identifier is dropped", P("skill.start", {"skill": "x" * 65}) == {} and P("skill.start", {"skill": f"files.write password={S}"}) == {}
          and P("skill.start", {"skill": f"a b"}) == {})
    check("D3 a number field refuses booleans and strings; a flag refuses numbers", P("skill.done", {"skill": "a", "ms": True}) == {"skill": "a"} and P("skill.done", {"skill": "a", "ms": "9"}) == {"skill": "a"}
          and P("orchestrator.observation", {"tool": "a", "blocked": 1}) == {"tool": "a"})
    check("D4 a tier outside L0-L3 is dropped", P("permission.confirm_requested", {"skill": "a", "tier": f"L9 {S}"}) == {"skill": "a"})
    check("D5 unknown extra fields are dropped even when harmless-looking", P("skill.start", {"skill": "a", "ms": 3, "note": "hi", "extra": 1}) == {"skill": "a"})
    check("D6 an empty projection of a listed topic is still just the topic (status only)", P("orchestrator.action_unfinished", {"goal": S, "wanted": [S]}) == {})

    scenario("E. Phase 64 sanitizing still applies to what is permitted, and fails closed")
    real = toolview.redact_credentials

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    toolview.redact_credentials = boom  # type: ignore[assignment]
    try:
        out = P("skill.start", {"skill": "files.write"})
    finally:
        toolview.redact_credentials = real  # type: ignore[assignment]
    check("E1 if the credential check fails, the permitted field becomes the fixed notice, never the original", out is not None and out.get("skill") != "files.write" and "could not be scrubbed" in str(out), str(out))
    real_se = auditview.sanitize_event
    auditview.sanitize_event = boom  # type: ignore[assignment]
    try:
        out = P("skill.start", {"skill": "files.write"})
        ext, _ = received("skill.start", {"skill": "files.write", "args": {"text": S}})
    finally:
        auditview.sanitize_event = real_se  # type: ignore[assignment]
    check("E2 if projection itself fails, no event is sent", out is None and ext is None)
    check("E3 the Phase 64 sanitizer is still the second layer (a permitted string passes through scrub_free_text)", "sanitize_event" in Path(remote_events.__file__).read_text(encoding="utf-8"))

    scenario("F. over the WebSocket (local mode): a request's events are status only")
    with TestClient(daemon.app, base_url="http://127.0.0.1:8765") as c:
        got = frames(c, {"host": "127.0.0.1:8765"}, f"what time is it, my password is {S}")
    events = [f for f in got if f["topic"] != "result"]
    check("F1 the stream shows activity (session.heard, skill.start) with approved fields only",
          {"session.heard", "skill.start"} <= {f["topic"] for f in events} and all(set(f["data"]) <= set(remote_events.POLICY[f["topic"]]) for f in events), str([f["topic"] for f in events]))
    check("F2 no frame except the requester's own `result` mentions the utterance or its secret", S not in json.dumps(events) and "what time" not in json.dumps(events).lower())
    check("F3 brain.* and the other suppressed topics never arrive", not any(f["topic"].startswith("brain.") for f in events) and all(f["topic"] in remote_events.POLICY for f in events))
    check("F4 the requester still gets its answer (the result frame is its own reply)", got[-1]["topic"] == "result" and "speech" in got[-1]["data"])

    scenario("G. remote mode: token and Host / Origin protections still apply, and the stream is the same minimal one")
    CFG.daemon.allow_remote = True
    os.environ[daemon.TOKEN_ENV] = TOKEN
    try:
        with TestClient(daemon.app, base_url="http://192.168.1.5:8765") as c:
            h = {"host": "192.168.1.5:8765"}
            for label, headers in (("no token", h), ("wrong token", {**h, "authorization": "Bearer " + TOKEN[:-1] + "a"}),
                                   ("valid token, cross-origin", {**h, "authorization": f"Bearer {TOKEN}", "origin": "https://evil.example"})):
                try:
                    frames(c, headers, "what time is it")
                    refused = False
                except Exception:  # noqa: BLE001
                    refused = True
                check(f"G1 {label}: the websocket is refused", refused)
            got = frames(c, {**h, "authorization": f"Bearer {TOKEN}"}, f"what time is it, token: {S}")
        events = [f for f in got if f["topic"] != "result"]
        check("G2 with the valid token the client receives only permitted topics and fields, and no secret", events and S not in json.dumps(events)
              and all(f["topic"] in remote_events.POLICY and set(f["data"]) <= set(remote_events.POLICY[f["topic"]]) for f in events))
    finally:
        CFG.daemon.allow_remote = False
        os.environ.pop(daemon.TOKEN_ENV, None)

    scenario("H. documentation and wiring")
    doc = remote_events.__doc__ or ""
    check("H1 the module documents deny-by-default, the allowlist and what is suppressed", all(w in doc for w in ("DENY BY DEFAULT", "ALLOWLIST", "Suppressed", "NO CONTENT")))
    bus_src = (Path(daemon.__file__).resolve().parent / "bus.py").read_text(encoding="utf-8")
    check("H2 the bus delivers to stream consumers only through the projection", "remote_events.project" in bus_src and "sanitize_event(topic, data)" not in bus_src)
    check("H3 the debug bus line still logs no payload", 'log.debug("bus %s fields=%s"' in bus_src)


if __name__ == "__main__":
    sys.exit(main())
