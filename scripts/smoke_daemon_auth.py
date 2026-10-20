"""Phase 63 -- remote mode is authenticated, or it does not start.

Phase 62 left `daemon.allow_remote: true` as an informed opt-in to a fully unauthenticated API (run skills, stream every bus event). Traced: there is
no credential mechanism anywhere in the app to reuse (config.yaml is plain, environment variables are only used for locations and test tripwires), no
client of the daemon in the repository, and the GUI / CLI use the backend in-process, so local mode needs no credential and gets none. Remote mode now needs a
shared secret from the ENVIRONMENT (`FRIDAY_DAEMON_TOKEN`: >= 32 characters, >= 8 distinct) and every HTTP request and WebSocket handshake must carry it as
`Authorization: Bearer <token>`:
  * `check_bind` refuses to start in remote mode (any host) without a usable token;
  * the ASGI guard, which runs before any route, the session, the executor or the event stream, answers 401 (websocket: closed before accept) otherwise, with one
    generic reply for missing / malformed / wrong credentials; with no usable token configured it serves nothing at all (so `uvicorn friday.daemon:app` cannot
    bypass `serve()`); the Phase 62 Host / Origin checks still run first and are not relaxed by a valid token.
It is a shared secret for a program on the other end -- not a user identity, not per-user authorization, and plain HTTP is not encrypted.

What these tests are: header-level checks through the ASGI app (Starlette TestClient) with a synthetic test-only token, uvicorn stubbed, no socket opened. They prove
the guard's decisions; they do not prove a real remote deployment, TLS, or resistance to a local attacker who can read the environment.
"""

from __future__ import annotations

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
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from friday import daemon  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

TOKEN = "test-only-token-0123456789-abcdefghijklmnop-XYZ"  # synthetic; never a real credential
WRONG = "test-only-token-0123456789-abcdefghijklmnop-XYa"
HOST = "http://192.168.1.5:8765"  # a remote client's view of the daemon; nothing is sent there
ROUTES = [("get", "/health", None), ("get", "/skills", None), ("get", "/audit", None), ("post", "/say", {"text": "what time is it"}),
          ("post", "/invoke", {"skill": "system.time"}), ("post", "/cancel", None), ("post", "/teach", {"skill": "system.time", "utterance": "x y z"})]


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(record.getMessage())


def call(c: TestClient, method: str, path: str, body, headers: dict | None = None):
    return getattr(c, method)(path, **({"json": body} if body is not None else {}), headers=headers or {})


def ws_refused(c: TestClient, headers: dict, path: str = "/ws") -> bool:
    try:
        with c.websocket_connect(path, headers={"host": "192.168.1.5:8765", **headers}) as ws:
            ws.send_json({"text": "what time is it"})
            ws.receive_json()
        return False
    except WebSocketDisconnect:
        return True
    except Exception:  # noqa: BLE001
        return True


def main() -> int:
    t0 = time.perf_counter()
    REGISTRY.discover()
    saved = (CFG.daemon.host, CFG.daemon.allow_remote)
    cap = Capture()
    logging.getLogger().addHandler(cap)
    try:
        with iso_env.isolated_runtime():
            run(cap)
    finally:
        logging.getLogger().removeHandler(cap)
        os.environ.pop(daemon.TOKEN_ENV, None)
        CFG.daemon.host, CFG.daemon.allow_remote = saved
    return finish("Phase 63 -- authenticated remote mode", time.perf_counter() - t0, min_assertions=50, min_scenarios=8)


def run(cap: Capture) -> None:
    import uvicorn

    scenario("A. remote mode without a usable token does not start")
    os.environ.pop(daemon.TOKEN_ENV, None)
    for host in ("0.0.0.0", "192.168.1.5", "127.0.0.1"):
        try:
            daemon.check_bind(host, True)
            ok = False
        except RuntimeError as exc:
            ok = daemon.TOKEN_ENV in str(exc)
        check(f"A1 allow_remote on {host} with no token is refused, naming the variable to set", ok)
    weak = {"too short (31 chars)": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p"[:31], "one repeated character": "a" * 64, "whitespace only": " " * 40,
            "few distinct characters": "ab" * 30, "empty": ""}
    for label, value in weak.items():
        os.environ[daemon.TOKEN_ENV] = value
        try:
            daemon.check_bind("0.0.0.0", True)
            ok = False
        except RuntimeError:
            ok = True
        check(f"A2 a weak token ({label}) does not count", ok and daemon.daemon_token() is None)
    os.environ[daemon.TOKEN_ENV] = TOKEN
    daemon.check_bind("0.0.0.0", True)
    check("A3 a strong token lets remote mode start", daemon.daemon_token() == TOKEN)
    daemon.check_bind("127.0.0.1", False)
    check("A4 local mode needs no token at all", True)
    calls: list[dict] = []
    real_run = uvicorn.run
    uvicorn.run = lambda app, **kw: calls.append(kw)  # type: ignore[assignment]
    try:
        os.environ.pop(daemon.TOKEN_ENV, None)
        CFG.daemon.host, CFG.daemon.allow_remote = "0.0.0.0", True
        try:
            daemon.serve()
            started = True
        except RuntimeError as exc:
            started, msg = False, str(exc)
        check("A5 serve() in remote mode without a token never reaches uvicorn", not started and calls == [])
        check("A6 the error never contains a token value", TOKEN not in msg)
        os.environ[daemon.TOKEN_ENV] = TOKEN
        daemon.serve()
        check("A7 with a token it starts", len(calls) == 1)
    finally:
        uvicorn.run = real_run  # type: ignore[assignment]
        CFG.daemon.host, CFG.daemon.allow_remote = "127.0.0.1", False

    # ---- remote mode, app served directly (not through serve()) -------------------------------------------------
    spies: dict[str, list] = {"exec": [], "handle": []}
    real_exec, real_handle = EXECUTOR.run, SESSION.handle

    async def spy_exec(name, *a, **k):
        spies["exec"].append(name)
        return await real_exec(name, *a, **k)

    async def spy_handle(text, **k):
        spies["handle"].append(text)
        return await real_handle(text, **k)

    EXECUTOR.run, SESSION.handle = spy_exec, spy_handle  # type: ignore[method-assign]
    CFG.daemon.allow_remote = True
    try:
        scenario("B. fail closed: remote mode with no usable token serves nothing (even if serve() was bypassed)")
        os.environ.pop(daemon.TOKEN_ENV, None)
        with TestClient(daemon.app, base_url=HOST, headers={"authorization": f"Bearer {TOKEN}"}) as c:
            codes = [call(c, m, p, b).status_code for m, p, b in ROUTES]
            check("B1 every route answers 401 even to a client presenting some token", codes == [401] * len(ROUTES), str(codes))
            check("B2 the websocket is refused too", ws_refused(c, {"authorization": f"Bearer {TOKEN}"}))
        check("B3 nothing reached the session or the executor", spies == {"exec": [], "handle": []}, str(spies))

        scenario("C. HTTP: only the right bearer token gets through")
        os.environ[daemon.TOKEN_ENV] = TOKEN
        with TestClient(daemon.app, base_url=HOST) as c:
            bad_headers = {
                "no Authorization header": {}, "wrong token": {"authorization": f"Bearer {WRONG}"}, "token missing after Bearer": {"authorization": "Bearer"},
                "empty token": {"authorization": "Bearer "}, "Basic scheme": {"authorization": f"Basic {TOKEN}"}, "bare token, no scheme": {"authorization": TOKEN},
                "token with a suffix": {"authorization": f"Bearer {TOKEN}x"}, "token truncated": {"authorization": f"Bearer {TOKEN[:-1]}"},
                "token in a different header": {"x-api-key": TOKEN, "x-auth-token": TOKEN},
            }
            for label, hdr in bad_headers.items():
                codes = [call(c, m, p, b, hdr).status_code for m, p, b in ROUTES]
                check(f"C1 {label}: every route -> 401", codes == [401] * len(ROUTES), str(codes))
            q = [c.get(f"/health?token={TOKEN}&access_token={TOKEN}").status_code, c.get("/audit", params={"token": TOKEN}).status_code]
            check("C2 a token in the query string is not accepted (it would end up in logs)", q == [401, 401], str(q))
            r1, r2 = c.get("/audit"), c.get("/audit", headers={"authorization": f"Bearer {WRONG}"})
            check("C3 missing and wrong credentials get the same generic reply, with a Bearer challenge",
                  r1.status_code == r2.status_code == 401 and r1.text == r2.text == '{"detail":"unauthorized"}' and r1.headers.get("www-authenticate") == "Bearer")
            good = {"authorization": f"Bearer {TOKEN}"}
            codes = [call(c, m, p, b, good).status_code for m, p, b in ROUTES]
            check("C4 with the right token every route answers", codes == [200] * len(ROUTES), str(codes))
            check("C5 the scheme name is case-insensitive", c.get("/health", headers={"authorization": f"bearer {TOKEN}"}).status_code == 200)
            check("C6 a call made with the token really runs", "system.time" in spies["exec"] and "what time is it" in spies["handle"], str(spies))

        scenario("D. WebSocket: authenticated before it is accepted")
        before_exec, before_handle, queues = list(spies["exec"]), list(spies["handle"]), len(BUS._queues)
        with TestClient(daemon.app, base_url=HOST) as c:
            check("D1 no credentials: closed before accept", ws_refused(c, {}))
            check("D2 wrong token: closed", ws_refused(c, {"authorization": f"Bearer {WRONG}"}))
            check("D3 token only in the query string: closed", ws_refused(c, {}, path=f"/ws?token={TOKEN}"))
            check("D4 Basic scheme: closed", ws_refused(c, {"authorization": f"Basic {TOKEN}"}))
            check("D5 the refused sockets ran nothing and never subscribed to the event stream",
                  spies["exec"] == before_exec and spies["handle"] == before_handle and len(BUS._queues) == queues, f"{spies} {len(BUS._queues)} vs {queues}")
            with c.websocket_connect("/ws", headers={"host": "192.168.1.5:8765", "authorization": f"Bearer {TOKEN}"}) as ws:
                ws.send_json({"text": "what time is it"})
                topics = []
                for _ in range(60):
                    m = ws.receive_json()
                    topics.append(m["topic"])
                    if m["topic"] == "result":
                        break
            check("D6 with the token the socket works: events stream and the utterance runs", "skill.start" in topics and topics[-1] == "result", str(topics))

        scenario("E. the Phase 62 Host / Origin checks still apply, with or without a valid token")
        good = {"authorization": f"Bearer {TOKEN}"}
        with TestClient(daemon.app, base_url=HOST) as c:
            evil = c.get("/audit", headers={**good, "origin": "https://evil.example"}).status_code
            null = c.get("/audit", headers={**good, "origin": "null"}).status_code
            other = c.get("/audit", headers={**good, "origin": "http://localhost:3000"}).status_code
            check("E1 a valid token does not open the daemon to another web page", evil == null == other == 403, f"{evil} {null} {other}")
            check("E2 ...nor a cross-origin websocket", ws_refused(c, {**good, "origin": "https://evil.example"}))
            check("E3 the daemon's own origin with the token is fine", c.get("/audit", headers={**good, "origin": HOST}).status_code == 200)
            check("E4 a cross-origin request WITHOUT a token is refused (403 before 401: nothing is revealed)", c.get("/audit", headers={"origin": "https://evil.example"}).status_code == 403)

        scenario("F. rejected requests cannot run anything or see private data")
        before = (list(spies["exec"]), list(spies["handle"]), len(BUS._queues))
        with TestClient(daemon.app, base_url=HOST) as c:
            leaks = [call(c, m, p, b).text for m, p, b in ROUTES] + [c.get("/audit", headers={"authorization": f"Bearer {WRONG}"}).text]
        check("F1 refusals carry no daemon data (no skills, no audit, no speech)", all("skill" not in t and "speech" not in t and "ok" not in t for t in leaks), str(leaks[:2]))
        check("F2 nothing was executed, handled or subscribed by any rejected attempt", (spies["exec"], spies["handle"], len(BUS._queues)) == before)
    finally:
        EXECUTOR.run, SESSION.handle = real_exec, real_handle  # type: ignore[method-assign]

    scenario("G. local mode is unchanged")
    CFG.daemon.allow_remote = False
    os.environ.pop(daemon.TOKEN_ENV, None)
    with TestClient(daemon.app, base_url="http://127.0.0.1:8765") as c:
        codes = [call(c, m, p, b).status_code for m, p, b in ROUTES]
        check("G1 no token, no Authorization header: every route answers, as before Phase 63", codes == [200] * len(ROUTES), str(codes))
        check("G2 a stray Authorization header is ignored in local mode", c.get("/health", headers={"authorization": "Bearer nonsense"}).status_code == 200)
        os.environ[daemon.TOKEN_ENV] = TOKEN
        check("G3 a token that happens to be set is not demanded in local mode", c.get("/health").status_code == 200)
        os.environ.pop(daemon.TOKEN_ENV, None)
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8765"}) as ws:
            ws.send_json({"text": "what time is it"})
            topics = [ws.receive_json()["topic"] for _ in range(4)]
        check("G4 a local websocket without credentials works", "result" in topics or "skill.start" in topics, str(topics))
        check("G5 Host / Origin protection is unchanged in local mode", c.get("/health", headers={"origin": "https://evil.example"}).status_code == 403
              and c.get("/health", headers={"host": "evil.example"}).status_code == 403)

    scenario("H. the secret stays a secret")
    blob = "\n".join(cap.text)
    check("H1 the token never appeared in any log line during the whole run", TOKEN not in blob and WRONG not in blob, blob[-200:])
    check("H2 the config model has no token field and config.yaml holds none", "token" not in json.dumps(CFG.daemon.model_dump()).lower()
          and "token" not in [k for k in CFG.daemon.model_dump()] and TOKEN not in Path(daemon.__file__).resolve().parent.parent.joinpath("config.yaml").read_text(encoding="utf-8"))
    check("H3 the comparison is constant-time (hmac.compare_digest)", "compare_digest" in Path(daemon.__file__).read_text(encoding="utf-8"))
    check("H4 there is no default token: without the variable none is used", (os.environ.pop(daemon.TOKEN_ENV, None), daemon.daemon_token())[1] is None)


if __name__ == "__main__":
    sys.exit(main())
