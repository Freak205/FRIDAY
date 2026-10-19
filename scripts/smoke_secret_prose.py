"""Phase 56 -- a credential written in ordinary words is redacted at the shared scrubber, so every reader of it is covered.

Phase 55 found that "my password is ..." style text can survive the audit scrubber. Traced first (not assumed): the shared prose pass
`toolview._PROSE_REDACTIONS` -- used by every audit argument/result/error field via `auditview._scrub` -- left 12 of 28 common phrasings
intact ("my API key is X", "api key: X", "password X", "Password - X", "passcode is X", "set the passphrase to X", "use X as the
password", "the password for the wifi is X", "secret key is X", "the token for github is X", ...), and `auditview.scrub_free_text`
(episode errors / context) applied NO credential rule at all (27 of 28 survived). This suite pins the fix at those two boundaries, then
follows a secret through the real paths that store and show it (audit rows, episodes, goal progress, the goal-status and recent-history
answers, the daemon's audit view). Synthetic secrets only; throwaway databases.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iso_env  # noqa: E402
import phase21_common as H  # noqa: E402
import smoke_goal_steps as G  # noqa: E402  (fixture skills + long-lived loop)
from phase21_common import call, check, done, finish, scenario  # noqa: E402

from friday import audit, auditview, store, toolview  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import episodes  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult  # noqa: E402

S = "zq7-SYNTH-secret-8841"

LEAKY = [  # every phrasing here must lose S at the shared boundaries
    f"open it and my password is {S}", f"my Password Is: {S}", f"password: {S}", f"Password - {S}", f"password {S}",
    f"my API key is {S}", f"api key: {S}", f"the API-KEY is {S}", f"token: {S}", f"my access token is {S}", f"token {S}",
    f"client secret is {S}", f"the client_secret: {S}", f"clientsecret = {S}", f"passcode is {S}", f"PASSWORD IS {S}",
    f"set the passphrase to {S}", f"use {S} as the password", f"the password for the wifi is {S} thanks",
    f"my password is \"{S}\"", f"the password was {S}.", f"credentials: user=bob pass={S}", f"secret key is {S}",
    f"my secret is {S}, and then open notepad", f"the token for github is {S}", f"api key = {S}", f"ssh key passphrase: {S}",
    f"change the password to {S} please", f"Password – {S}", f"Reset my passphrase to '{S}'",
]
BENIGN = [
    "the session is active", "I forgot my password, can you open the reset page", "change password settings", "open the password manager",
    "the token ring network is down", "secret santa list for friday", "what is a password hash", "passwords are hard to remember",
    "show tokens used today", "the api key page is open", "password-protected file", "enter your password in the box",
    "Opened the password manager.", "Timer set for 5 minutes.", "use the keyboard as the input", "set the volume to 40",
    "the pass is open this week", "authorization is pending", "I need a strong password for my account",
]


def main() -> int:
    t0 = time.perf_counter()
    saved_tools = H.lock_down_real_tools(REGISTRY)
    saved = CFG.desktop_observer.enabled
    CFG.desktop_observer.enabled = False
    try:
        scenario("A. the shared scrubbers redact free-text credentials")
        boundaries = {
            "toolview.sanitize(prose)": lambda t: toolview.sanitize(t, prose=True),
            "toolview.redact_credentials": toolview.redact_credentials,
            "audit INTENT arg": lambda t: auditview.sanitize_args("plan.run", {"goal": t})["goal"],
            "audit KEEP arg": lambda t: auditview.sanitize_args("apps.open", {"app": t})["app"],
            "audit result": lambda t: auditview.sanitize_result("x.y", t),
            "audit error": lambda t: auditview.sanitize_error(t),
            "scrub_free_text": auditview.scrub_free_text,
        }
        for name, fn in boundaries.items():
            leaked = [c for c in LEAKY if S in fn(c)]
            check(f"A1 {name}: none of {len(LEAKY)} phrasings keeps the secret", not leaked, str(leaked[:3]))
        out = auditview.scrub_free_text(f"my API key is {S} and I use it daily")
        check("A2 the project's marker is used and the keyword stays, for debugging", "[redacted]" in out and "API key" in out, out)
        check("A3 a quoted value is redacted whole, quotes included", S not in toolview.sanitize(f"my password is \"{S} x y\"", prose=True))
        check("A4 text before the credential survives", toolview.sanitize(f"open notepad then my password is {S}", prose=True)
              .startswith("open notepad then my password"))
        check("A5 a multi-word passphrase after 'is' is redacted to the end of its clause, not left half-visible",
              "horse" not in toolview.sanitize("my passphrase is correct horse battery staple", prose=True))
        check("A6 a lone key=value in plain (non-prose) tool output with a spaced name is redacted too",
              S not in toolview.sanitize(f"api key: {S}") and S not in toolview.sanitize(f"client secret = {S}"))

        scenario("B. idempotent, and the fixed-point guard never has to fall back")
        stable = all(toolview.sanitize(toolview.sanitize(c, prose=True), prose=True) == toolview.sanitize(c, prose=True) for c in LEAKY + BENIGN)
        check("B1 sanitizing twice equals once, for every phrasing and every benign sentence", stable)
        check("B2 the audit arg sanitizer keeps a readable redaction (not the 'omitted N chars' fallback)",
              all("[redacted]" in auditview.sanitize_args("plan.run", {"goal": c})["goal"] for c in LEAKY[:12]))
        check("B3 redact_credentials twice equals once", all(toolview.redact_credentials(toolview.redact_credentials(c)) == toolview.redact_credentials(c)
                                                              for c in LEAKY))

        scenario("C. credential-named fields and nested structures stay protected")
        args = {"goal": "open it", "password": S, "nested": {"api_key": S, "list": [{"token": S}]}, "text": S}
        out = json.dumps(auditview.sanitize_args("plan.run", args))
        check("C1 a credential-named field is redacted whatever it holds", S not in out and "[redacted]" in out, out)
        out = json.dumps(auditview.sanitize_args("files.write", {"path": "a.txt", "content": f"password: {S}", "Authorization": S}))
        check("C2 content is omitted and an Authorization field redacted", S not in out)
        check("C3 a header, a bearer token and a URL password are still redacted",
              S not in toolview.sanitize(f"Authorization: Bearer {S}") and S not in toolview.sanitize(f"https://u:{S}@h.test/x"))

        scenario("D. ordinary text is left alone")
        changed = [b for b in BENIGN if toolview.sanitize(b, prose=True) != b]
        check("D1 no benign sentence is altered", not changed, str(changed))
        check("D2 ordinary audit text keeps its value", auditview.sanitize_result("apps.open", "Opened the password manager.") == "Opened the password manager."
              and auditview.sanitize_args("apps.open", {"app": "KeePass"}) == {"app": "KeePass"})
        check("D3 a number-bearing request is not mistaken for a credential",
              toolview.sanitize("set a timer for 10 minutes and the volume to 30", prose=True) == "set a timer for 10 minutes and the volume to 30")
        real = toolview.redact_credentials

        def broken(_t: str) -> str:
            raise RuntimeError("boom")

        toolview.redact_credentials = broken  # type: ignore[assignment]
        try:
            failed_out = auditview.scrub_free_text(f"my password is {S}")
        finally:
            toolview.redact_credentials = real  # type: ignore[assignment]
        check("D4 a scrubber that fails stores a marker, never the original text", S not in failed_out and failed_out.startswith("[omitted"), failed_out)

        with iso_env.isolated_runtime():
            real_paths()
    finally:
        CFG.desktop_observer.enabled = saved
        CFG.permissions.overrides = saved_tools
        G.GATE.set()
    return finish("Phase 56 -- free-text credential redaction", time.perf_counter() - t0, min_assertions=30, min_scenarios=6)



def real_paths() -> None:
    scenario("E. a secret followed through the real paths that store and show it")
    G.WORLD.clear()
    G.WORLD["write"] = SkillResult(speech=f"Could not log in: token: {S}", ok=False)
    goal = f"create folder a, write a file in it, then send it. my API key is {S}"
    r = G.run_plan(goal, [call("test.g_mkdir", {"name": "a"}), call("test.g_write", {"name": "a"}), done("done")])
    gid = r.data["goal_id"]
    rows = [dict(x) for x in store.conn().execute("SELECT * FROM audit").fetchall()]
    check("E1 no audit row (any column) holds the secret", S not in json.dumps(rows, default=str))
    check("E2 the failing step's error was stored without it, but still says what failed",
          any("Could not log in" in (x["result"] or "") and S not in (x["result"] or "") for x in rows), str([x["result"] for x in rows][-3:]))
    eps = [dict(x) for x in store.conn().execute("SELECT * FROM episodes").fetchall()]
    check("E3 the episode (goal text, errors, context, plan) holds no secret and still has its goal", eps and S not in json.dumps(eps, default=str, ensure_ascii=False)
          and "create folder a" in eps[-1]["goal_text"], str(eps[-1]["goal_text"]) if eps else "no episode")
    prog = store.conn().execute("SELECT progress FROM goals WHERE id=?", (gid,)).fetchone()[0]
    check("E4 goals.progress holds no secret", S not in (prog or ""))
    status = G.on_loop(EXECUTOR.run("meta.goal_status", {}, actor="text"))
    check("E5 the goal-status answer (speech and data) holds no secret", S not in status.speech + json.dumps(status.data, default=str), status.speech)
    recent = G.on_loop(EXECUTOR.run("meta.recent", {"count": 20}, actor="text"))
    check("E6 the recent-history answer holds no secret", S not in recent.speech + json.dumps(recent.data, default=str))
    from friday import daemon

    served = G.on_loop(daemon.recent_audit(limit=50))
    check("E7 the daemon's audit view holds no secret", S not in json.dumps(served, default=str))
    check("E8 data.steps of the run itself carries no secret", S not in json.dumps(r.data, default=str) and "Could not log in" in json.dumps(r.data))
    check("E9 ordinary outcomes are unchanged: the successful step is still recorded as succeeded with its result",
          any(x["skill"] == "test.g_mkdir" and x["ok"] == 1 and x["result"] == "Created the folder." for x in rows))
    check("E10 the user's own goal row still holds the request (needed to resume it) - and no reader above returns it",
          S in store.conn().execute("SELECT original_request FROM goals WHERE id=?", (gid,)).fetchone()[0])

    scenario("F. history is not rewritten by this change")
    store.conn().execute("INSERT INTO audit (at, actor, skill, tier, args, decision, ok, result) VALUES (?,?,?,?,?,?,?,?)",
                         ("2025-01-01T00:00:00+00:00", "text", "x.legacy", "L0", "{}", "auto", 1, f"my password is {S}"))
    store.conn().commit()
    old = next(x for x in audit.recent(100) if x["skill"] == "x.legacy")
    check("F1 an old row is returned exactly as stored (clean it with the existing, operator-run `db scrub`)", old["result"] == f"my password is {S}")


if __name__ == "__main__":
    sys.exit(main())
