"""Phase 62 -- who can talk to the daemon.

Traced: the daemon binds `CFG.daemon.host` (default and shipped config: 127.0.0.1, port 8765) with NO authentication on any route; the host is
free text, so `0.0.0.0` was accepted silently; and nothing looked at `Origin` or `Host`. Reproduced before the fix (Starlette TestClient, header-level
-- a real browser was not driven): a request claiming `Origin: https://evil.example` was served on `/say` and `/invoke`; `GET /audit` with
`Host: evil.example` (DNS rebinding) returned 200; and a cross-origin WebSocket to `/ws` was accepted, received the event stream and ran an utterance
-- the one that matters, because browsers apply neither the same-origin policy nor a preflight to WebSockets, so any page the user visits could do it
while the daemon runs, loopback or not.

Fix (no new authentication): a non-loopback `daemon.host` refuses to start unless `daemon.allow_remote` is set; a pure-ASGI guard refuses a request whose
`Host` is not loopback (unless allow_remote) or that carries an `Origin` other than the daemon's own. Clients that send no Origin (CLI, scripts, local
programs) are unaffected. This is NOT authentication: any program on the machine still has full access.

Header-level tests through the ASGI app; no socket is opened, no real network or browser is involved, and uvicorn is stubbed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from friday import daemon  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

EVIL = {"origin": "https://evil.example"}
TEST_TOKEN = "test-only-token-0123456789-abcdefghijklmnop-XYZ"  # synthetic; never a real credential


def refused_ws(client: TestClient, headers: dict) -> bool:
    try:
        with client.websocket_connect("/ws", headers={"host": "127.0.0.1:8765", **headers}) as ws:  # TestClient ignores base_url for websockets
            ws.send_json({"text": "what time is it"})
            ws.receive_json()
        return False
    except WebSocketDisconnect:
        return True
    except Exception:  # noqa: BLE001 -- a refused handshake surfaces differently across versions
        return True


def main() -> int:
    t0 = time.perf_counter()
    REGISTRY.discover()
    saved = (CFG.daemon.host, CFG.daemon.allow_remote)
    try:
        with iso_env.isolated_runtime():
            run()
    finally:
        CFG.daemon.host, CFG.daemon.allow_remote = saved
    return finish("Phase 62 -- daemon access boundary", time.perf_counter() - t0, min_assertions=30, min_scenarios=5)


def run() -> None:
    scenario("A. where the daemon may listen")
    import yaml

    shipped = yaml.safe_load(Path(daemon.__file__).resolve().parent.parent.joinpath("config.yaml").read_text(encoding="utf-8"))["daemon"]
    check("A1 the shipped config listens on loopback with remote access off", shipped["host"] == "127.0.0.1" and shipped["allow_remote"] is False, str(shipped))
    check("A2 the built-in defaults are the same", CFG.daemon.__class__().host == "127.0.0.1" and CFG.daemon.__class__().allow_remote is False)
    for h in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2", "LOCALHOST"):
        daemon.check_bind(h, False)
    check("A3 loopback hosts start without any opt-in", True)
    refused = []
    for h in ("0.0.0.0", "::", "", "192.168.1.5", "10.0.0.2", "example.com", "my-pc.local"):
        try:
            daemon.check_bind(h, False)
            refused.append(h)
        except RuntimeError as exc:
            assert "allow_remote" in str(exc)
    check("A4 every non-loopback host is refused without the opt-in, and the message says how to opt in", not refused, str(refused))
    import os

    os.environ[daemon.TOKEN_ENV] = TEST_TOKEN
    daemon.check_bind("0.0.0.0", True)
    check("A5 with daemon.allow_remote AND a token it is allowed (Phase 63: the token is mandatory; see smoke_daemon_auth)", True)

    import uvicorn

    calls: list[dict] = []
    real_run = uvicorn.run
    uvicorn.run = lambda app, **kw: calls.append(kw)  # type: ignore[assignment]
    try:
        CFG.daemon.host, CFG.daemon.allow_remote = "0.0.0.0", False
        try:
            daemon.serve()
            started = True
        except RuntimeError:
            started = False
        check("A6 serve() with a non-loopback host and no opt-in never reaches uvicorn", not started and calls == [])
        CFG.daemon.host = "127.0.0.1"
        daemon.serve()
        check("A7 serve() on loopback starts uvicorn on that host", len(calls) == 1 and calls[0]["host"] == "127.0.0.1")
        CFG.daemon.host, CFG.daemon.allow_remote = "0.0.0.0", True
        calls.clear()
        daemon.serve()
        check("A8 serve() with the opt-in and a token starts it on the requested host", len(calls) == 1 and calls[0]["host"] == "0.0.0.0")
    finally:
        os.environ.pop(daemon.TOKEN_ENV, None)
        uvicorn.run = real_run  # type: ignore[assignment]
        CFG.daemon.host, CFG.daemon.allow_remote = "127.0.0.1", False

    scenario("B. legitimate local clients are unaffected")
    ran: list[str] = []
    real_exec = EXECUTOR.run

    async def spy(name, *a, **k):
        ran.append(name)
        return await real_exec(name, *a, **k)

    EXECUTOR.run = spy  # type: ignore[method-assign]
    try:
        for host in ("127.0.0.1:8765", "localhost:8765"):
            with TestClient(daemon.app, base_url=f"http://{host}") as c:
                ok = [c.get("/health").status_code, c.get("/skills").status_code, c.get("/audit").status_code,
                      c.post("/invoke", json={"skill": "system.time"}).status_code, c.post("/say", json={"text": "what time is it"}).status_code]
                check(f"B1 {host}: /health /skills /audit /invoke /say all answer (no Origin, as a CLI or script sends)", ok == [200] * 5, str(ok))
        with TestClient(daemon.app, base_url="http://127.0.0.1:8765") as c:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8765"}) as ws:
                ws.send_json({"text": "what time is it"})
                topics = []
                for _ in range(60):
                    m = ws.receive_json()
                    topics.append(m["topic"])
                    if m["topic"] == "result":
                        break
            check("B2 a websocket client without an Origin still gets the event stream and its result", "result" in topics and "skill.start" in topics, str(topics))
            check("B2b an IPv6 loopback Host is accepted", c.get("/health", headers={"host": "[::1]:8765"}).status_code == 200)
            same = c.get("/health", headers={"origin": "http://127.0.0.1:8765"}).status_code
            check("B3 the daemon's own origin is accepted (a page it serves itself)", same == 200)
    finally:
        EXECUTOR.run = real_exec  # type: ignore[method-assign]

    scenario("C. other web pages and rebinding hosts are refused, and nothing runs")
    ran.clear()
    handled: list[str] = []
    real_handle = SESSION.handle

    async def spy_handle(text, **k):
        handled.append(text)
        return await real_handle(text, **k)

    SESSION.handle = spy_handle  # type: ignore[method-assign]
    EXECUTOR.run = spy  # type: ignore[method-assign]
    try:
        with TestClient(daemon.app, base_url="http://127.0.0.1:8765") as c:
            codes = {
                "POST /say, cross-origin": c.post("/say", json={"text": "what time is it"}, headers=EVIL).status_code,
                "POST /say as text/plain (no preflight)": c.post("/say", content='{"text":"x"}', headers={**EVIL, "content-type": "text/plain"}).status_code,
                "POST /invoke, cross-origin": c.post("/invoke", json={"skill": "system.time"}, headers=EVIL).status_code,
                "POST /cancel, cross-origin": c.post("/cancel", headers=EVIL).status_code,
                "GET /audit, cross-origin": c.get("/audit", headers=EVIL).status_code,
                "Origin: null": c.get("/health", headers={"origin": "null"}).status_code,
                "another local port (http://localhost:3000)": c.get("/health", headers={"origin": "http://localhost:3000"}).status_code,
                "GET /audit, Host: evil.example (DNS rebinding)": c.get("/audit", headers={"host": "evil.example:8765"}).status_code,
                "GET /skills, Host: 192.168.1.5:8765": c.get("/skills", headers={"host": "192.168.1.5:8765"}).status_code,
                "GET /health, Host: evil.example": c.get("/health", headers={"host": "evil.example"}).status_code,
            }
            for label, code in codes.items():
                check(f"C1 {label} -> 403", code == 403, str(code))
            check("C2 the refused POSTs reached neither the session nor the executor", handled == [] and ran == [], f"{handled} {ran}")
            check("C3 a cross-origin websocket is refused", refused_ws(c, EVIL))
            check("C4 ...so is a websocket from Origin null, from another local port, and with a rebinding Host",
                  refused_ws(c, {"origin": "null"}) and refused_ws(c, {"origin": "http://localhost:3000"}) and refused_ws(c, {"host": "evil.example:8765"}))
            check("C5 the refused websockets ran nothing", handled == [] and ran == [], f"{handled} {ran}")
            body = c.get("/audit", headers=EVIL).text
            check("C6 the refusal reveals nothing of the daemon's data", "skill" not in body and "forbidden" in body, body[:80])
    finally:
        SESSION.handle = real_handle  # type: ignore[method-assign]
        EXECUTOR.run = real_exec  # type: ignore[method-assign]

    scenario("D. with allow_remote: other hosts may connect, other web pages still may not")
    CFG.daemon.allow_remote = True
    os.environ[daemon.TOKEN_ENV] = TEST_TOKEN
    try:
        with TestClient(daemon.app, base_url="http://192.168.1.5:8765", headers={"authorization": f"Bearer {TEST_TOKEN}"}) as c:
            check("D1 a non-loopback Host is accepted when the operator opted in", c.get("/health").status_code == 200)
            check("D2 the daemon's own (LAN) origin is accepted", c.get("/health", headers={"origin": "http://192.168.1.5:8765"}).status_code == 200)
            check("D3 a cross-origin page is still refused, HTTP and websocket", c.get("/health", headers=EVIL).status_code == 403 and refused_ws(c, {**EVIL, "authorization": f"Bearer {TEST_TOKEN}"}))
    finally:
        CFG.daemon.allow_remote = False
        os.environ.pop(daemon.TOKEN_ENV, None)

    scenario("E. the header rules themselves")
    allow = daemon.request_allowed
    check("E1 loopback Host variants with and without a port, IPv6 included",
          all(allow(h, None, False) for h in ("127.0.0.1", "127.0.0.1:8765", "localhost", "LOCALHOST:8765", "[::1]:8765", "[::1]")))
    check("E2 a lookalike name is not loopback", not any(allow(h, None, False) for h in ("127.0.0.1.evil.example", "localhost.evil.example:8765", "evil.example")))
    check("E3 no Host header at all is refused unless remote is allowed", not allow(None, None, False) and not allow("", None, False))
    check("E4 only an identical origin passes: another scheme-host-port, a malformed or empty value does not",
          allow("127.0.0.1:8765", "http://127.0.0.1:8765", False) and not allow("127.0.0.1:8765", "http://127.0.0.1:9", False)
          and not allow("127.0.0.1:8765", "http://[bad", False) and not allow("127.0.0.1:8765", "", False))
    check("E5 an absent Origin (non-browser client) is always fine on a loopback Host", allow("127.0.0.1:8765", None, False))


if __name__ == "__main__":
    sys.exit(main())
