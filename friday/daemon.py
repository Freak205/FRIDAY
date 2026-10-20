"""The FRIDAY daemon.

One long-lived process. Clients (CLI, tray, overlay, voice, phone bridge) connect
over HTTP + WebSocket on localhost, so every interface takes an identical path
through the brain and the permission guard.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from friday import __version__, audit, store
from friday.brain import BRAIN
from friday.bus import BUS
from friday.config import CFG
from friday.log import get, setup
from friday.registry import REGISTRY
from friday.session import SESSION

log = get(__name__)


class Say(BaseModel):
    text: str
    actor: str = "text"


class Teach(BaseModel):
    skill: str
    utterance: str | None = None


class Invoke(BaseModel):
    skill: str
    args: dict[str, Any] = {}
    actor: str = "text"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    setup()
    log.info("FRIDAY %s starting", __version__)
    store.init()
    audit.start_session("daemon")
    REGISTRY.discover()
    await asyncio.to_thread(BRAIN.warm)

    from friday.jobs import SCHEDULER
    from friday.triggers import WATCHER

    SCHEDULER.start()
    WATCHER.start()

    log.info("ready — %d skills, listening on %s:%s",
             len(REGISTRY), CFG.daemon.host, CFG.daemon.port)
    await BUS.publish("daemon.ready", skills=len(REGISTRY))
    yield

    WATCHER.stop()
    SCHEDULER.stop()

    from friday import browser

    if browser.is_open():
        await browser.close()

    log.info("FRIDAY shutting down")


app = FastAPI(title="FRIDAY", version=__version__, lifespan=lifespan)


# -- Phase 62: who may talk to the daemon ------------------------------------------------------------------------------------
#
# The daemon has no authentication, so the boundary is WHERE it listens and WHICH web pages can reach it. Loopback is not enough on its
# own: a browser lets any web page open a WebSocket to 127.0.0.1 (no same-origin policy, no preflight), and with DNS rebinding any page can
# read a GET route; both then see the unfiltered event stream and (websocket) run skills as the user. Browsers always say which page they
# act for (`Origin`) and which name they dialled (`Host`); command-line and local-program clients send no `Origin`.
#   * the default `host` is loopback, and a non-loopback host refuses to start unless `daemon.allow_remote` is set (`check_bind`);
#   * a request whose `Host` is not a loopback name is refused (DNS rebinding) unless `allow_remote`;
#   * a request that carries an `Origin` is refused unless it is this daemon's own origin (same scheme-less host:port as `Host`).
#   * (Phase 63) remote mode additionally requires a bearer token on every request and WebSocket handshake.
# In local mode this does not authenticate anyone: another program on the same machine still has full access.

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback(host: str) -> bool:
    import ipaddress

    h = (host or "").strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


# Phase 63: remote mode (`daemon.allow_remote`) is only available with a shared secret. It is read from the environment -- never from
# config.yaml (which sits in the repository / a synced folder), never generated, never logged or returned -- must be at least 32
# characters, and every HTTP request and every WebSocket handshake must present it as `Authorization: Bearer <token>`. It is a
# shared secret for a program on the other end, not a user identity: it does not say WHO is calling and gives no per-user rights.
TOKEN_ENV = "FRIDAY_DAEMON_TOKEN"
MIN_TOKEN_CHARS = 32


def daemon_token() -> str | None:
    """The configured token, or None when it is missing or too weak to count (short, or a few repeated characters)."""
    import os

    token = os.environ.get(TOKEN_ENV, "").strip()
    return token if len(token) >= MIN_TOKEN_CHARS and len(set(token)) >= 8 else None


def token_matches(header: str | None, token: str | None) -> bool:
    import hmac

    if not token or not header:
        return False
    scheme, _, presented = header.strip().partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(presented.strip().encode(), token.encode())


def check_bind(host: str, allow_remote: bool) -> None:
    """Refuse to start where the daemon would be reachable without protection. Raises RuntimeError with what to do.

    * loopback host, remote mode off: fine (local clients; Host/Origin guard below).
    * a non-loopback host without `daemon.allow_remote`: refused.
    * `daemon.allow_remote` (any host): refused unless a strong `FRIDAY_DAEMON_TOKEN` is set -- the daemon has no other authentication."""
    if not allow_remote:
        if is_loopback(host):
            return
        raise RuntimeError(
            f"refusing to start the daemon on {host!r}: it would be reachable from other machines. Use a loopback host "
            "(127.0.0.1), or set daemon.allow_remote: true in config.yaml AND a token in the FRIDAY_DAEMON_TOKEN environment variable."
        )
    if daemon_token() is None:
        raise RuntimeError(
            f"refusing to start the daemon in remote mode (daemon.allow_remote) on {host!r}: no usable access token. Set the "
            f"{TOKEN_ENV} environment variable to a secret of at least {MIN_TOKEN_CHARS} characters "
            "(for example: python -c \"import secrets; print(secrets.token_urlsafe(32))\"); clients send it as 'Authorization: Bearer <token>'."
        )
    log.warning("daemon in remote mode on %s: every request needs the %s token (shared secret; no user identity)", host, TOKEN_ENV)


def _host_name(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):  # [::1]:8765
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def request_allowed(host_header: str | None, origin: str | None, allow_remote: bool) -> bool:
    from urllib.parse import urlsplit

    if not allow_remote and not is_loopback(_host_name(host_header or "")):
        return False
    if origin is not None:
        try:
            same = urlsplit(origin).netloc.lower() == (host_header or "").strip().lower()
        except ValueError:
            same = False
        if not same:  # a page from anywhere else, or "null"
            return False
    return True


class LocalOnlyMiddleware:
    """Pure ASGI (so it covers WebSockets): see the block comment above."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            remote = CFG.daemon.allow_remote
            if not request_allowed(headers.get("host"), headers.get("origin"), remote):
                log.warning("daemon refused a %s request (host=%r origin=%r)", scope["type"], headers.get("host"), headers.get("origin"))
                await self._refuse(scope, receive, send, 403, b'{"detail":"forbidden"}')
                return
            # Phase 63: in remote mode every request must carry the token, checked BEFORE any handler, the session, the executor or the event
            # stream is reached. No usable token configured = nothing is served (the app can be started without `serve()`). The reply is the
            # same whether the header is missing, malformed or wrong; neither the header nor the token is logged.
            if remote and not token_matches(headers.get("authorization"), daemon_token()):
                log.warning("daemon refused an unauthenticated %s request", scope["type"])
                await self._refuse(scope, receive, send, 401, b'{"detail":"unauthorized"}', challenge=True)
                return
        await self.inner(scope, receive, send)

    @staticmethod
    async def _refuse(scope: dict, receive: Any, send: Any, status: int, body: bytes, *, challenge: bool = False) -> None:
        if scope["type"] == "websocket":
            await receive()  # the connect message: the socket is closed without ever being accepted
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if challenge:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(LocalOnlyMiddleware)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "skills": len(REGISTRY),
        "voice": CFG.voice.enabled,
        "pending": SESSION.pending.kind if SESSION.pending else None,
    }


@app.get("/skills")
async def skills() -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "tier": s.tier,
            "description": s.description,
            "params": [
                {"name": p.name, "required": p.required, "description": p.description}
                for p in s.params
            ],
            "examples": s.examples,
        }
        for s in sorted(REGISTRY.all(), key=lambda x: x.name)
        if not s.internal
    ]


@app.post("/say")
async def say(body: Say) -> dict[str, Any]:
    """The main entry point: natural language in, result out."""
    result = await SESSION.handle(body.text, actor=body.actor)
    return {"speech": result.speech, "ok": result.ok, "data": result.data}


@app.post("/invoke")
async def invoke(body: Invoke) -> dict[str, Any]:
    """Call a skill directly, bypassing the brain but not the permission guard.

    Phase 15.0: found on the real machine — a denied call (e.g. an
    unattended actor over its tier ceiling) raised `PermissionError_`
    uncaught, turning a routine policy decision into a 500 Internal Server
    Error instead of the same clean `{"ok": false, ...}` shape `/say`
    already returns for the identical denial via `Session._run`'s own
    `except PermissionError_`. Mirrors that handling here rather than
    inventing a new error contract.
    """
    from friday.permissions import EXECUTOR, PermissionError_

    try:
        result = await EXECUTOR.run(body.skill, body.args, actor=body.actor)
    except PermissionError_ as exc:
        return {"speech": f"I'm not allowed to do that. {exc}", "ok": False, "data": {}}
    except KeyError:
        return {"speech": "That skill isn't available.", "ok": False, "data": {}}
    return {"speech": result.speech, "ok": result.ok, "data": result.data}


@app.post("/cancel")
async def cancel() -> dict[str, Any]:
    """Ask whatever `plan.run` goal is currently running to stop (Phase 16.0;
    unified with the GUI button / hotkey / typed "stop" in Phase 28.0 — all go
    through `Session.stop`). No further step starts; a step in flight is
    interrupted when it can be and otherwise finishes. Cooperative, not forced:
    the running `/say` (or equivalent) request keeps its own task and returns
    normally, reporting `stopped="cancelled"` with what it already did. Safe to
    call with nothing running (nothing is armed) and safe to repeat."""
    result = await SESSION.stop(source="daemon")
    return {"ok": True, "speech": result.speech, "data": result.data}


@app.post("/teach")
async def teach(body: Teach) -> dict[str, Any]:
    if body.utterance:
        BRAIN.teach(body.utterance, body.skill)
        return {"ok": True, "speech": f"\"{body.utterance}\" now means {body.skill}."}
    result = await SESSION.teach_last(body.skill)
    return {"ok": result.ok, "speech": result.speech}


@app.get("/audit")
async def recent_audit(limit: int = 20) -> list[dict[str, Any]]:
    return audit.recent(limit)


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    """Bidirectional channel: send utterances, receive every bus event."""
    await ws.accept()
    queue = BUS.stream()

    async def pump() -> None:
        while True:
            event = await queue.get()
            await ws.send_json(
                {"topic": event.topic, "data": event.data, "at": event.at.isoformat()}
            )

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_json()
            if text := msg.get("text"):
                result = await SESSION.handle(text, actor=msg.get("actor", "text"))
                await ws.send_json(
                    {"topic": "result", "data": {"speech": result.speech, "ok": result.ok}}
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        pump_task.cancel()
        BUS.drop(queue)


def serve() -> None:
    import uvicorn

    check_bind(CFG.daemon.host, CFG.daemon.allow_remote)
    uvicorn.run(
        app,
        host=CFG.daemon.host,
        port=CFG.daemon.port,
        log_config=None,
        access_log=False,
    )
