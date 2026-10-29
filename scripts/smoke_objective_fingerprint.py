"""Phase 89 -- keyed objective fingerprints: a restart can tell an equivalent verified retry from an unrelated verified step.

`friday.objectivefp` stores, per step that a restart will need to compare, a handful of HMAC digests (identity of the call, same-tool target,
target value-set) plus the action kind and a key id, under the DPAPI-protected subkey (Phase 87). `steps.reconstruct` replays the Phase 84
recovery rule from them where the stored evidence allows, and keeps the Phase 85 behavior everywhere else.

Everything is synthetic and isolated: a throwaway database (`iso_env.isolated_runtime`), throwaway key files under the temp folder, and a MOCK
DPAPI (`keystore._protect/_unprotect`), so no real key is created, read or changed, nothing real runs, and `data/` is never named. The real
key path is only stat-ed, at the end, to prove it was not created.

  A  parity: for a table of (failed call, later call) pairs, `objectivefp.equivalent` == `verify._redone`, through `steps._records`
  B  identical calls are recognized without persisting a single raw argument (progress, logs, database)
  C  same tool, same target
  D  cross-tool: same action kind and the same non-empty target set, and the near misses
  E  read objectives
  F  an unrelated verified step is rejected, and the stale flag is dropped by `reconstruct`
  G  a contradicted read-back with an unverified retry / with an equivalent verified retry
  H  legacy records without fingerprints
  I  key unavailable, malformed fingerprints, other versions, other keys: undecided, never a guess
  J  persistence round trips, non-mutation, refresh stability, nothing shown to the user
  K  privacy of the fingerprint itself
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
import phase21_common as H  # noqa: E402
from phase21_common import check, scenario  # noqa: E402

from friday import audit, auditview, dbplace, keystore, objectivefp, owner, paths, steps, store, verify  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.steps import StepRecord, StepState  # noqa: E402

REGISTRY.discover()

DEAD_ID = owner.Identity("fake-dead89", 4_200_089, 1.0, owner._host())
SENT_PATH = r"C:\Users\fpuser\SENTINEL-PATH-7731\private-notes.txt"
SENT_URL = "https://example.test/doc?token=SENTINEL-URLQ-9981&u=alice"
SENT_CMD = "curl -H Authorization:Bearer-SENTINEL-CRED-5521 https://example.test/x"
SENT_TEXT = "SENTINEL-BODY-4417 confidential body"
SENT_TOKENS = ("sentinel-path-7731", "private-notes", "sentinel-urlq-9981", "alice", "example.test", "sentinel-cred-5521", "bearer",
               "curl", "sentinel-body-4417", "confidential", "fpuser")


def probe_dead(pid, start, host):  # noqa: ANN001
    return owner.DEAD if pid == DEAD_ID.pid else owner.UNKNOWN


# -- fixture tools: kinds and identifying arguments chosen so every branch of `same_objective` has a pair ------------------------

def _fixture(name: str, tier: str, action: str, policy: dict) -> None:
    @skill(name=name, tier=tier, action=action, description="Phase 89 fixture")
    def _f(**kw):  # noqa: ANN003, ARG001
        return SkillResult(speech="ok")

    auditview.POLICY[name] = policy


K, O = auditview.KEEP, auditview.OMIT
_fixture("test.fp_write", "L1", "modify", {"name": K, "text": O})
_fixture("test.fp_save", "L1", "modify", {"name": K})
_fixture("test.fp_send", "L1", "communicate", {"name": K})
_fixture("test.fp_read1", "L0", "read", {"name": K})
_fixture("test.fp_read2", "L0", "read", {"name": K})
_fixture("test.fp_look", "L0", "observe", {"name": K})
_fixture("test.fp_blob", "L1", "modify", {"text": O})
_fixture("test.fp_two", "L1", "modify", {"a": K, "b": K})
W, SAVE, SEND, R1, R2, LOOK, BLOB, TWO = ("test.fp_write", "test.fp_save", "test.fp_send", "test.fp_read1", "test.fp_read2",
                                          "test.fp_look", "test.fp_blob", "test.fp_two")


# -- a mock DPAPI and throwaway keys --------------------------------------------------------------------------------------------

PREFIX = b"MOCKDPAPI89:"
REAL = (keystore._protect, keystore._unprotect)


def mock_protect(data: bytes) -> bytes:
    pad = hashlib.sha256(b"mock-pad-89").digest() * 8
    return PREFIX + bytes(a ^ b for a, b in zip(data, pad))


def mock_unprotect(blob: bytes) -> bytes:
    if not blob.startswith(PREFIX):
        raise keystore.KeyUnavailable("undecryptable")
    pad = hashlib.sha256(b"mock-pad-89").digest() * 8
    return bytes(a ^ b for a, b in zip(blob[len(PREFIX):], pad))


_KEYDIR: Path | None = None
_N = [0]


def new_key() -> Path:
    """Switch to a brand-new random key (a fresh file under the temp folder). The previous key's file is left where it was."""
    assert _KEYDIR is not None
    _N[0] += 1
    paths.KEY_FILE = _KEYDIR / f"fp{_N[0]}.key"
    keystore._forget()
    objectivefp._forget()
    return paths.KEY_FILE


def use_key(path: Path | None) -> None:
    paths.KEY_FILE = path
    keystore._forget()
    objectivefp._forget()


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


# -- observations, goals, stored progress ---------------------------------------------------------------------------------------

def obs(tool: str, args: dict, kind: str) -> SimpleNamespace:
    """kind: verified | completed | contra (the tool said yes, the read-back said no) | fail (the tool itself failed)."""
    ver = None
    ok = True
    if kind == "verified":
        ver = verify.Verification(verify.VerifyStatus.VERIFIED, [verify.Check("fixture effect", True)])
    elif kind == "contra":
        ok, ver = False, verify.Verification(verify.VerifyStatus.FAILED, [verify.Check("fixture effect", False)])
    elif kind == "fail":
        ok = False
    return SimpleNamespace(step=SimpleNamespace(tool=tool, args=dict(args), subgoal_id="", subgoal=""), ok=ok, error="", speech="x",
                           verification=ver, audit_ids=[])


def recs(observations: list) -> list[StepRecord]:
    return steps._records(observations)[0]  # noqa: SLF001


def store_goal(observations: list, status: str = "failed") -> str:
    """A goal whose audit rows and stored progress come from the REAL path: `_records` -> `build_report` -> `progress_of`."""
    g = goals_mod.create("p89 goal", "p89 goal")
    goals_mod.update_status(g.id, goals_mod.GoalStatus(status))
    for o in observations:
        with owner._use_identity(DEAD_ID), audit.goal_scope(g.id):
            rid = audit.record(actor="text", skill=o.step.tool, tier="L1", args={}, decision="auto")
        if o.ok or getattr(o.verification, "status", None) is verify.VerifyStatus.FAILED:   # a contradicted step: the row repeats "yes"
            audit.complete(rid, ok=True, result="Done.", duration_ms=1)
        else:
            audit.complete(rid, ok=False, result="Could not.", error="boom", duration_ms=1)
        o.audit_ids = [rid]
    report = steps.build_report(observations, stopped="completed")
    goals_mod.set_progress(g.id, steps.progress_of(report, "completed"))
    return g.id


def prog(gid: str) -> dict:
    return copy.deepcopy(goals_mod.get(gid).progress)


def put(gid: str, p: dict) -> None:
    goals_mod.set_progress(gid, p)


def stored_json(gid: str) -> str:
    return json.dumps(goals_mod.get(gid).progress, sort_keys=True)


def flags(gid: str, **kw) -> list[tuple[str, bool]]:  # noqa: ANN003
    return [(s.state.value, s.recovered) for s in steps.reconstruct(gid, probe_dead, **kw).steps]


def tampered(gid: str, *, flag: dict[int, bool] | None = None, fp=None, drop_fp: tuple[int, ...] = ()) -> str:  # noqa: ANN001
    """Edit a stored progress in place: set recovered flags (a stale/legacy flag), replace one step's fp, or remove fps."""
    p = prog(gid)
    for i, v in (flag or {}).items():
        if v:
            p["steps"][i]["recovered"] = True
        else:
            p["steps"][i].pop("recovered", None)
    if fp is not None:
        i, new = fp
        p["steps"][i]["fp"] = new
    for i in drop_fp:
        p["steps"][i].pop("fp", None)
    put(gid, p)
    return gid


def fp_of(tool: str, args: dict) -> dict | None:
    key = objectivefp.subkey()
    return None if key is None else objectivefp.make(tool, args, key=key)


def rec(tool: str, state: StepState, fp: dict | None) -> StepRecord:
    return StepRecord(1, tool, state, fp=fp)


def tokens_in(text: str) -> list[str]:
    low = text.lower()
    return [t for t in SENT_TOKENS if t in low]


def fp_values(p: dict) -> list[str]:
    return [v for s in p.get("steps", []) if isinstance(s.get("fp"), dict) for k, v in s["fp"].items() if k in ("i", "o", "s", "kid")]


# =================================================================================================================================

def main() -> int:
    global _KEYDIR
    t0 = time.perf_counter()
    real_key = dbplace.canonical_key()
    real_before = real_key.exists() if real_key else None
    cap = Capture()
    root_logger = logging.getLogger()
    old_level = root_logger.level
    root_logger.addHandler(cap)
    root_logger.setLevel(logging.DEBUG)
    saved = (keystore._protect, keystore._unprotect)
    try:
        with iso_env.isolated_runtime(), tempfile.TemporaryDirectory(prefix="friday-fp89-", ignore_cleanup_errors=True) as kd:
            _KEYDIR = Path(kd)
            keystore._protect, keystore._unprotect = mock_protect, mock_unprotect
            new_key()
            run_all(cap)
    finally:
        keystore._protect, keystore._unprotect = saved
        keystore._forget()
        objectivefp._forget()
        root_logger.removeHandler(cap)
        root_logger.setLevel(old_level)

    if real_key is not None:
        check("the user's real key path was not created (stat only)", real_key.exists() == real_before and not real_before)
    check("the genuine DPAPI functions were restored", (keystore._protect, keystore._unprotect) == REAL)
    return H.finish("Phase 89 objective fingerprints", time.perf_counter() - t0, min_assertions=250, min_scenarios=11)


def run_all(cap: Capture) -> None:
    # ---------------------------------------------------------------------------------------------------------------------- A
    scenario("A  parity: equivalent() == verify._redone() through steps._records")
    P1, P2, U1, U2 = r"C:\fp\a.txt", r"C:\fp\b.txt", "https://example.test/a", "https://example.test/a?x=1"
    table = [
        ("same tool, same path, other content", ("files.write", {"path": P1, "content": "x"}), ("files.write", {"path": P1, "content": "y"})),
        ("same tool, same path (case/slash differ)", ("files.write", {"path": "C:/FP/a.txt", "content": "x"}), ("files.write", {"path": P1, "content": "z"})),
        ("same tool, other path", ("files.write", {"path": P1, "content": "x"}), ("files.write", {"path": P2, "content": "x"})),
        ("identical call", ("files.write", {"path": P1, "content": "x"}), ("files.write", {"path": P1, "content": "x"})),
        ("write -> copy (target sets differ)", ("files.write", {"path": P1, "content": "x"}), ("files.copy", {"src": P1, "dst": P2})),
        ("copy -> move, same pair", ("files.copy", {"src": P1, "dst": P2}), ("files.move", {"src": P1, "dst": P2})),
        ("copy -> move, swapped (same value set)", ("files.copy", {"src": P1, "dst": P2}), ("files.move", {"src": P2, "dst": P1})),
        ("copy -> move, other dst", ("files.copy", {"src": P1, "dst": P2}), ("files.move", {"src": P1, "dst": r"C:\fp\c.txt"})),
        ("write -> unclassified append (raw)", ("files.write", {"path": P1, "content": "x"}), ("files.append", {"path": P1, "content": "x"})),
        ("append -> append identical", ("files.append", {"path": P1, "content": "x"}), ("files.append", {"path": P1, "content": "x"})),
        ("append -> append other content (raw)", ("files.append", {"path": P1, "content": "x"}), ("files.append", {"path": P1, "content": "y"})),
        ("delete -> write same path (kind differs)", ("files.delete", {"path": P1}), ("files.write", {"path": P1, "content": "x"})),
        ("browser.open same url", ("browser.open", {"url": U1}), ("browser.open", {"url": U1})),
        ("browser.open other query", ("browser.open", {"url": U1}), ("browser.open", {"url": U2})),
        ("browser.open -> web.fetch (kind differs)", ("browser.open", {"url": U1}), ("web.fetch", {"url": U1})),
        ("shell.run same argv0, other args", ("shell.run", {"command": "git status", "cwd": P1}), ("shell.run", {"command": "git log", "cwd": P1})),
        ("shell.run other argv0", ("shell.run", {"command": "git status", "cwd": P1}), ("shell.run", {"command": "dir", "cwd": P1})),
        ("clipboard.write identical (raw)", ("clipboard.write", {"text": "hello"}), ("clipboard.write", {"text": "hello"})),
        ("clipboard.write other text (raw)", ("clipboard.write", {"text": "hello"}), ("clipboard.write", {"text": "bye"})),
        ("notes.add -> memory.remember same text (identity has the tool)", ("notes.add", {"text": "t"}), ("memory.remember", {"text": "t"})),
        ("failed read -> same tool", ("web.search", {"query": "a"}), ("web.search", {"query": "b"})),
        ("failed read -> other read tool", ("web.search", {"query": "a"}), ("files.read", {"path": P1})),
        ("failed read -> state change", ("files.read", {"path": P1}), ("shell.run", {"command": "dir"})),
        ("unregistered tool identical", ("ghost.tool", {"x": 1}), ("ghost.tool", {"x": 1})),
        ("unregistered tool other args", ("ghost.tool", {"x": 1}), ("ghost.tool", {"x": 2})),
        ("unregistered tools, same args", ("ghost.tool", {"x": 1}), ("ghost.two", {"x": 1})),
        ("fixture write -> same", (W, {"name": "A", "text": "1"}), (W, {"name": "A", "text": "2"})),
        ("fixture write -> other name", (W, {"name": "A"}), (W, {"name": "B"})),
        ("fixture write -> save (same kind, same target)", (W, {"name": "A", "text": "1"}), (SAVE, {"name": "A"})),
        ("fixture write -> save (other target)", (W, {"name": "A"}), (SAVE, {"name": "B"})),
        ("fixture write -> send (other kind)", (W, {"name": "A"}), (SEND, {"name": "A"})),
        ("fixture write -> two {A} == {A}", (W, {"name": "A"}), (TWO, {"a": "A", "b": "A"})),
        ("fixture write -> two {A} != {A,B}", (W, {"name": "A"}), (TWO, {"a": "A", "b": "B"})),
        ("fixture write -> blob (raw)", (W, {"name": "A"}), (BLOB, {"text": "A"})),
        ("fixture write with no name -> save with no name (empty target)", (W, {"text": "1"}), (SAVE, {})),
        ("fixture write with no name -> same shape", (W, {"text": "1"}), (W, {"text": "2"})),
        ("blob identical", (BLOB, {"text": "same"}), (BLOB, {"text": "same"})),
        ("blob other content", (BLOB, {"text": "same"}), (BLOB, {"text": "other"})),
        ("fixture read -> same read tool", (R1, {"name": "A"}), (R1, {"name": "B"})),
        ("fixture read -> other read tool (same kind)", (R1, {"name": "A"}), (R2, {"name": "Z"})),
        ("fixture read -> observe (other kind)", (R1, {"name": "A"}), (LOOK, {"name": "A"})),
        ("fixture read -> state change", (R1, {"name": "A"}), (W, {"name": "A"})),
    ]
    both_true = both_false = 0
    for label, f, l in table:
        new_key()
        o1, o2 = obs(*f, "contra"), obs(*l, "verified")
        a, b = recs([o1, o2])
        oracle = bool(verify._redone(o1, o2))  # noqa: SLF001
        check(f"A  [{label}] both fingerprints are valid", objectivefp.valid(a.fp) and objectivefp.valid(b.fp), f"{a.fp} {b.fp}")
        if not (objectivefp.valid(a.fp) and objectivefp.valid(b.fp)):
            continue
        got = objectivefp.equivalent(a.tool, a.fp, b.tool, b.fp)
        check(f"A  [{label}] equivalent() == _redone() == {oracle}", got == oracle, f"fp={got} live={oracle}")
        check(f"A  [{label}] recovered() answers the same way, and so does the live flag", objectivefp.recovered(a, [b]) is oracle and a.recovered == oracle,
              f"{objectivefp.recovered(a, [b])} {a.recovered}")
        both_true += oracle
        both_false += not oracle
    check("A  the table exercises both outcomes", both_true >= 8 and both_false >= 12, f"{both_true} true / {both_false} false")

    # ---------------------------------------------------------------------------------------------------------------------- B
    scenario("B  identical calls are recognized; no raw argument is persisted anywhere")
    new_key()
    calls = [
        ("files.write", {"path": SENT_PATH, "content": SENT_TEXT}, {"path": SENT_PATH, "content": SENT_TEXT}),
        ("browser.open", {"url": SENT_URL}, {"url": SENT_URL}),
        ("shell.run", {"command": SENT_CMD, "cwd": SENT_PATH}, {"command": SENT_CMD, "cwd": SENT_PATH}),
        ("clipboard.write", {"text": SENT_TEXT}, {"text": SENT_TEXT}),
    ]
    observations = []
    for tool, a1, a2 in calls:
        observations += [obs(tool, a1, "contra"), obs(tool, a2, "verified")]
    r = recs(observations)
    for n in range(0, len(r), 2):
        a, b = r[n], r[n + 1]
        check(f"B  {a.tool}: identical retry is equivalent via the identity digest", a.fp and b.fp and a.fp["i"] == b.fp["i"]
              and objectivefp.equivalent(a.tool, a.fp, b.tool, b.fp), str((a.fp, b.fp)))
        check(f"B  {a.tool}: and the live decision agrees", a.recovered)
    gid = store_goal(observations)
    p = prog(gid)
    check("B  every contradicted failure and every later verified step carries a fingerprint", all(isinstance(s.get("fp"), dict) for s in p["steps"]), str(p))
    blob = json.dumps(p, sort_keys=True)
    check("B  the stored progress contains no raw path, URL, query, command, credential or content", not tokens_in(blob), str(tokens_in(blob)))
    check("B  ...not in any of the fingerprints either", not tokens_in(json.dumps([s["fp"] for s in p["steps"]])))
    rows = " ".join(str(dict(x)) for x in store.conn().execute("SELECT * FROM audit WHERE goal_id=?", (gid,)).fetchall())
    check("B  ...and none of it in the goal's audit rows", not tokens_in(rows), str(tokens_in(rows)))
    log_text = "\n".join(cap.lines)
    check("B  no sentinel in the captured log output", not tokens_in(log_text), str(tokens_in(log_text)))
    check("B  no fingerprint digest in the log output", not any(v in log_text for v in fp_values(p)))
    check("B  the recovered flags survive a rebuild (equivalent verified retries)", [f[1] for f in flags(gid)] == [True, False] * 4, str(flags(gid)))
    # no mutation of the observations that supplied the arguments
    check("B  the observations' own arguments were not altered", observations[0].step.args == {"path": SENT_PATH, "content": SENT_TEXT})

    # ---------------------------------------------------------------------------------------------------------------------- C
    scenario("C  same tool, same target")
    new_key()
    a = fp_of(W, {"name": "Report", "text": "version one"})
    b = fp_of(W, {"name": "Report", "text": "version two, quite different"})
    c = fp_of(W, {"name": "Other", "text": "version one"})
    check("C  other content, same target: 'o' equal", a["o"] == b["o"])
    check("C  ...but the identity differs (the content is in it)", a["i"] != b["i"])
    check("C  ...so it is equivalent", objectivefp.equivalent(W, a, W, b))
    check("C  other target: 'o' and 'i' differ", a["o"] != c["o"] and a["i"] != c["i"])
    check("C  ...and it is not equivalent", not objectivefp.equivalent(W, a, W, c))
    d = fp_of(W, {"name": "  report ", "text": "q"})
    check("C  names are normalized as objective() does (case, whitespace)", a["o"] == d["o"])
    pa, pb = fp_of("files.write", {"path": "C:/FP/A.txt", "content": "1"}), fp_of("files.write", {"path": r"c:\fp\a.txt", "content": "2"})
    check("C  paths are normalized as objective() does (case, separators)", pa["o"] == pb["o"])
    check("C  the action kind and the state-change bit are recorded", a["k"] == "modify" and a["c"] == 1 and a["t"] == "t")

    # ---------------------------------------------------------------------------------------------------------------------- D
    scenario("D  cross-tool recovery")
    new_key()
    w = fp_of(W, {"name": "A", "text": "1"})
    s = fp_of(SAVE, {"name": "A"})
    check("D  write -> save on one name: same kind, same non-empty target set", objectivefp.equivalent(W, w, SAVE, s))
    check("D  ...and that is exactly what steps.same_objective says", steps.same_objective(W, {"name": "A", "text": "1"}, SAVE, {"name": "A"}))
    check("D  the value-set digest is what carries it", "s" in w and w["s"] == s["s"] and w["o"] == s["o"] or w["s"] == s["s"])
    sd = fp_of(SEND, {"name": "A"})
    check("D  another action kind on the same name is not recovery", not objectivefp.equivalent(W, w, SEND, sd) and w["s"] == sd["s"] and w["k"] != sd["k"])
    sb = fp_of(SAVE, {"name": "B"})
    check("D  the same kind on another name is not recovery", not objectivefp.equivalent(W, w, SAVE, sb))
    t2 = fp_of(TWO, {"a": "A", "b": "B"})
    check("D  a different target SET ({A} vs {A,B}) is not recovery", not objectivefp.equivalent(W, w, TWO, t2))
    t1 = fp_of(TWO, {"a": "A", "b": "A"})
    check("D  the same SET through another shape ({A} == {A,A}) is", objectivefp.equivalent(W, w, TWO, t1) and steps.same_objective(W, {"name": "A"}, TWO, {"a": "A", "b": "A"}))
    sw = fp_of(TWO, {"a": "A", "b": "B"})
    sw2 = fp_of(TWO, {"a": "B", "b": "A"})
    check("D  value sets ignore which key held which value", sw["s"] == sw2["s"] and sw["o"] != sw2["o"])
    blob = fp_of(BLOB, {"text": "A"})
    check("D  a raw objective never matches another tool, however similar", blob["t"] == "r" and "s" not in blob and not objectivefp.equivalent(W, w, BLOB, blob))
    e1, e2 = fp_of(W, {"text": "only content"}), fp_of(SAVE, {})
    check("D  an empty target never matches another tool", "s" not in e1 and "s" not in e2 and not objectivefp.equivalent(W, e1, SAVE, e2)
          and not steps.same_objective(W, {"text": "only content"}, SAVE, {}))
    cp = fp_of("files.copy", {"src": r"C:\fp\a.txt", "dst": r"C:\fp\b.txt"})
    mv = fp_of("files.move", {"src": r"C:\fp\b.txt", "dst": r"C:\fp\a.txt"})
    check("D  real tools: copy -> move over the same two paths", objectivefp.equivalent("files.copy", cp, "files.move", mv))

    # ---------------------------------------------------------------------------------------------------------------------- E
    scenario("E  read objectives keep the existing semantics (no invented target equivalence)")
    new_key()
    r1, r1b, r2, lk, wr = (fp_of(R1, {"name": "A"}), fp_of(R1, {"name": "B"}), fp_of(R2, {"name": "Z"}), fp_of(LOOK, {"name": "A"}),
                           fp_of(W, {"name": "A"}))
    check("E  a failed read has c == 0", r1["c"] == 0 and r1["k"] == "read")
    check("E  ...matches a later read of the same tool whatever it named", objectivefp.equivalent(R1, r1, R1, r1b))
    check("E  ...matches a later read of another tool of the same kind", objectivefp.equivalent(R1, r1, R2, r2))
    check("E  ...not a different kind of read (observe)", not objectivefp.equivalent(R1, r1, LOOK, lk))
    check("E  ...not a state change, even on the same name", not objectivefp.equivalent(R1, r1, W, wr))
    for f, ft, l, lt in ((r1, R1, r1b, R1), (r1, R1, r2, R2), (r1, R1, lk, LOOK), (r1, R1, wr, W)):
        got = objectivefp.equivalent(ft, f, lt, l)
        want = bool(verify._redone(obs(ft, {"name": "A"}, "contra"), obs(lt, {"name": "A"}, "verified")))  # noqa: SLF001
        check(f"E  {ft} -> {lt} agrees with verify._redone", got == want, f"{got} {want}")
    check("E  a state-changing failure is not given read semantics: write -> other-named save is not equivalent",
          not objectivefp.equivalent(W, wr, SAVE, fp_of(SAVE, {"name": "Q"})))
    rb = fp_of(R1, {"name": "A", "extra": "x" * 50})
    check("E  a read's fingerprint still stores only digests", set(rb) <= objectivefp._FIELDS and "x" * 20 not in json.dumps(rb))  # noqa: SLF001

    # ---------------------------------------------------------------------------------------------------------------------- F
    scenario("F  an unrelated verified step: rejected, and the stale recovered flag is dropped")
    new_key()
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified")])
    check("F  live: the unrelated verified step did not recover it", [s_.get("recovered", False) for s_ in prog(gid)["steps"]] == [False, False])
    r = recs([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified")])
    check("F  recovered() is False (the fingerprints prove it unrelated)", objectivefp.recovered(r[0], r[1:]) is False)
    tampered(gid, flag={0: True})                      # a stale flag, as Phase 85 could not judge
    check("F  stored with a stale flag", prog(gid)["steps"][0].get("recovered") is True)
    check("F  reconstruct drops it: failed, not recovered, then verified", flags(gid) == [("failed", False), ("verified", False)], str(flags(gid)))
    rr = steps.reconstruct(gid, probe_dead)
    check("F  the account says it failed", "Failed:" in rr.tail() and rr.outcome in ("partial", "failed"), f"{rr.outcome} {rr.tail()}")
    check("F  reconcile_recovery=False (the writers) leaves the flag as stored", flags(gid, reconcile_recovery=False)[0] == ("failed", True))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(SEND, {"name": "A"}, "verified")])
    tampered(gid, flag={0: True})
    check("F  another kind of action on the same name does not preserve it", flags(gid)[0] == ("failed", False), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified"), obs(SAVE, {"name": "A"}, "verified")])
    tampered(gid, flag={0: True})
    check("F  one equivalent verified step among unrelated ones keeps it", flags(gid)[0] == ("failed", True), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified"), obs(W, {"name": "C"}, "verified")])
    tampered(gid, flag={0: True})
    check("F  two unrelated verified steps still drop it", flags(gid)[0] == ("failed", False), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified"), obs(W, {"name": "C"}, "contra"),
                      obs(W, {"name": "C", "text": "x"}, "verified")])
    tampered(gid, flag={0: True, 2: True})
    check("F  each failure is judged on its own later steps", [f[1] for f in flags(gid)] == [False, False, True, False], str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "verified"), obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified")])
    tampered(gid, flag={1: True})
    check("F  a verified step BEFORE the failure counts for nothing", flags(gid)[1] == ("failed", False), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "fail"), obs(W, {"name": "B"}, "completed")])
    tampered(gid, flag={0: True})
    check("F  a plain tool failure (no read-back verdict) keeps its Phase 54 flag, never judged by fingerprints", flags(gid)[0] == ("failed", True), str(flags(gid)))
    check("F  ...and none was even made for it", "fp" not in prog(gid)["steps"][0])

    # ---------------------------------------------------------------------------------------------------------------------- G
    scenario("G  a contradicted read-back with an unverified / an equivalent verified retry")
    new_key()
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A", "text": "again"}, "completed")])
    tampered(gid, flag={0: True})
    check("G  completed-only retry: flag dropped (Phase 84: nothing confirmed it)", flags(gid) == [("failed", False), ("completed", False)], str(flags(gid)))
    check("G  ...no fingerprint was made when there is no verified step after it", "fp" not in prog(gid)["steps"][0])
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A"}, "completed"), obs(W, {"name": "B"}, "verified")])
    tampered(gid, flag={0: True})
    check("G  an equivalent but only COMPLETED retry, then an unrelated verified step: dropped", flags(gid)[0] == ("failed", False), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A", "text": "again"}, "verified")])
    check("G  live: an equivalent verified retry recovers it", prog(gid)["steps"][0].get("recovered") is True)
    check("G  rebuilt: the flag stays, and the account shows the recovery", flags(gid) == [("failed", True), ("verified", False)]
          and "Failed:" not in steps.reconstruct(gid, probe_dead).tail(), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(SAVE, {"name": "A"}, "verified")])
    check("G  cross-tool equivalent verified retry: kept", flags(gid) == [("failed", True), ("verified", False)], str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A"}, "fail")])
    tampered(gid, flag={0: True})
    check("G  a later failed step recovers nothing", flags(gid)[0] == ("failed", False), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A", "text": "again"}, "verified")])
    p = prog(gid)
    p["steps"][1]["ver"] = {"status": "verified", "checks": [["fixture effect", None]]}      # its evidence is no longer adequate
    put(gid, p)
    got = flags(gid)
    check("G  a later step whose stored verdict no longer proves it (rebuilt completed) is not adequate evidence: flag dropped",
          got == [("failed", False), ("completed", False)], str(got))

    # ---------------------------------------------------------------------------------------------------------------------- H
    scenario("H  legacy records without fingerprints keep Phase 85 behavior")
    new_key()
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified")])
    tampered(gid, flag={0: True}, drop_fp=(0, 1))
    check("H  a later verified step, no fingerprints: the flag stays (nothing durable says otherwise)", flags(gid) == [("failed", True), ("verified", False)], str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A"}, "completed")])
    tampered(gid, flag={0: True}, drop_fp=(0, 1))
    check("H  no later verified step, no fingerprints: the flag is dropped", flags(gid) == [("failed", False), ("completed", False)], str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified")])
    tampered(gid, flag={0: True}, drop_fp=(1,))
    check("H  the failed step has a fingerprint but the verified one does not: undecided, kept", flags(gid)[0] == ("failed", True), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified")])
    tampered(gid, flag={0: True}, drop_fp=(0,))
    check("H  the verified step has one but the failed step does not: undecided, kept", flags(gid)[0] == ("failed", True), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified"), obs(W, {"name": "C"}, "verified")])
    tampered(gid, flag={0: True}, drop_fp=(2,))
    check("H  one unrelated verified step proven, another undecided: undecided, kept", flags(gid)[0] == ("failed", True), str(flags(gid)))
    gid = store_goal([obs(W, {"name": "A"}, "fail"), obs(W, {"name": "A"}, "completed")])
    check("H  a legacy plain failure is unchanged", flags(gid) == [("failed", True), ("completed", False)], str(flags(gid)))
    p = prog(gid)
    check("H  and a record without any fp field round-trips through progress_of unchanged",
          steps.progress_of(steps.reconstruct(gid, probe_dead, reconcile_recovery=False), "completed")["steps"] == p["steps"])

    # ---------------------------------------------------------------------------------------------------------------------- I
    scenario("I  no key, malformed fingerprints, other versions, other keys: undecided, never a guess")
    good_key = new_key()
    base = [obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A", "text": "again"}, "verified")]
    unrel = [obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified")]
    g_equiv = store_goal(base)
    g_unrel = store_goal(unrel)
    tampered(g_unrel, flag={0: True})
    g_none = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A"}, "completed")])
    tampered(g_none, flag={0: True})
    fa, fb = prog(g_equiv)["steps"][0]["fp"], prog(g_unrel)["steps"][1]["fp"]

    # I.1 key unavailable at READ time
    use_key(None)
    check("I  no key location: subkey() is None", objectivefp.subkey() is None)
    check("I  ...recovered() cannot tell", objectivefp.recovered(rec(W, StepState.FAILED, fa), [rec(W, StepState.VERIFIED, fa)]) is None)
    check("I  ...an equivalent retry keeps its flag", flags(g_equiv)[0] == ("failed", True), str(flags(g_equiv)))
    check("I  ...an unrelated one is NOT dropped either (the fingerprints cannot be trusted without the key)", flags(g_unrel)[0] == ("failed", True), str(flags(g_unrel)))
    check("I  ...but the key-free Phase 85 rule still drops a flag with no verified step after it", flags(g_none)[0] == ("failed", False), str(flags(g_none)))
    use_key(good_key)
    check("I  the key comes back: the unrelated flag is dropped again", flags(g_unrel)[0] == ("failed", False) and flags(g_equiv)[0] == ("failed", True))

    for label, setup in (
        ("corrupt key file", lambda: (good_key.write_bytes(b"not a protected blob"), use_key(good_key))),
        ("empty key file", lambda: (good_key.write_bytes(b""), use_key(good_key))),
    ):
        setup()
        before_bytes = good_key.read_bytes()
        check(f"I  {label}: subkey() is None", objectivefp.subkey() is None)
        check(f"I  {label}: the flag of an unrelated retry is kept (undecided)", flags(g_unrel)[0] == ("failed", True), str(flags(g_unrel)))
        check(f"I  {label}: the key file is never replaced", good_key.read_bytes() == before_bytes)
        # restore the real key for the next case: a new key would have another kid, so rebuild the goals under it
        good_key = new_key()
        g_equiv, g_unrel = store_goal(base), store_goal(unrel)
        tampered(g_unrel, flag={0: True})
        fa, fb = prog(g_equiv)["steps"][0]["fp"], prog(g_unrel)["steps"][1]["fp"]

    real_unprotect = keystore._unprotect
    keystore._unprotect = lambda b: (_ for _ in ()).throw(keystore.KeyUnavailable("undecryptable"))
    keystore._forget()
    check("I  DPAPI refusing to decrypt: subkey() is None, flag kept", objectivefp.subkey() is None and flags(g_unrel)[0] == ("failed", True))
    keystore._unprotect = real_unprotect
    keystore._forget()

    # I.2 key unavailable at WRITE time: nothing set, behavior as before
    use_key(None)
    r = recs([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A", "text": "again"}, "verified")])
    check("I  no key at write time: no fingerprint on any step", all(x.fp is None for x in r))
    check("I  ...and the live recovery decision is the same as ever", [x.recovered for x in r] == [True, False])
    r = recs(unrel)
    check("I  ...for an unrelated retry too", [x.recovered for x in r] == [False, False] and all(x.fp is None for x in r))
    gid = store_goal(base)
    check("I  ...the stored progress has no fp field", all("fp" not in s_ for s_ in prog(gid)["steps"]))
    check("I  ...and rebuilds exactly like a legacy record (kept: a verified step follows)", flags(gid)[0] == ("failed", True))
    gone = new_key()
    real_protect = keystore._protect

    def no_dpapi(_: bytes) -> bytes:
        raise keystore.KeyUnavailable("no_dpapi")

    keystore._protect = no_dpapi
    r = recs(base)
    check("I  DPAPI unavailable when a key would be created: no fingerprint, no key file made", all(x.fp is None for x in r) and not gone.exists())
    keystore._protect = real_protect
    boom = keystore.derive

    def explode(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("boom SENTINEL-BOOM-1234 /private/path")

    keystore.derive = explode
    try:
        check("I  an unexpected error from the key store is swallowed: subkey() is None", objectivefp.subkey() is None)
        r = recs(base)
        check("I  ...the run is unaffected", all(x.fp is None for x in r) and r[0].recovered)
    finally:
        keystore.derive = boom
    check("I  ...and the error text was not logged", "sentinel-boom-1234" not in "\n".join(cap.lines).lower() and "/private/path" not in "\n".join(cap.lines))

    # I.3 malformed fingerprints
    new_key()
    F = fp_of(W, {"name": "A"})
    L = fp_of(W, {"name": "A", "text": "again"})
    U = fp_of(W, {"name": "B"})
    check("I  the reference fingerprints are valid", objectivefp.valid(F) and objectivefp.valid(L) and objectivefp.valid(U))
    bads = {
        "version 2": {**F, "v": 2}, "version as string": {**F, "v": "1"}, "version as bool": {**F, "v": True}, "version 0": {**F, "v": 0},
        "kid not hex": {**F, "kid": "Z" * 16}, "kid short": {**F, "kid": F["kid"][:8]}, "kid upper": {**F, "kid": F["kid"].upper()},
        "kid int": {**F, "kid": 5},
        "kind empty": {**F, "k": ""}, "kind int": {**F, "k": 5}, "kind long": {**F, "k": "x" * 65},
        "c = 2": {**F, "c": 2}, "c bool": {**F, "c": True}, "c string": {**F, "c": "1"},
        "t unknown": {**F, "t": "x"}, "t int": {**F, "t": 1},
        "i short": {**F, "i": F["i"][:30]}, "i upper": {**F, "i": F["i"].upper()}, "i int": {**F, "i": 7}, "i none": {**F, "i": None},
        "o not hex": {**F, "o": "g" * 32}, "o long": {**F, "o": F["o"] + "00"},
        "s not hex": {**F, "s": "g" * 32}, "s on a raw objective": {**F, "t": "r"},
        "missing i": {k: v for k, v in F.items() if k != "i"}, "missing kid": {k: v for k, v in F.items() if k != "kid"},
        "missing v": {k: v for k, v in F.items() if k != "v"},
        "unknown field": {**F, "x": "y"}, "raw arg smuggled in": {**F, "path": r"C:\secret.txt"},
        "empty dict": {},
    }
    for name, bad in bads.items():
        check(f"I  malformed ({name}): valid() is False", objectivefp.valid(bad) is False)
        check(f"I  malformed ({name}) on the failed step: undecided", objectivefp.recovered(rec(W, StepState.FAILED, bad), [rec(W, StepState.VERIFIED, L)]) is None)
        check(f"I  malformed ({name}) on the later step: undecided", objectivefp.recovered(rec(W, StepState.FAILED, F), [rec(W, StepState.VERIFIED, bad)]) is None)
    for name, bad in {"string": "abc", "list": [1, 2], "number": 7, "none": None, "bool": True}.items():
        check(f"I  non-dict fingerprint ({name}): valid() is False and recovered() undecided",
              objectivefp.valid(bad) is False and objectivefp.recovered(rec(W, StepState.FAILED, bad), [rec(W, StepState.VERIFIED, L)]) is None)
    for name in ("version 2", "kid not hex", "unknown field", "t unknown", "i short", "empty dict"):
        gid = store_goal(unrel)
        tampered(gid, flag={0: True}, fp=(1, bads[name] if name != "unknown field" else bads[name]))
        # damage the LATER step's fingerprint (the failed step's stays valid)
        check(f"I  stored with a damaged later fingerprint ({name}): the unrelated flag is kept, not dropped", flags(gid)[0] == ("failed", True), str(flags(gid)))
        gid = store_goal(unrel)
        tampered(gid, flag={0: True}, fp=(0, bads[name]))
        check(f"I  stored with a damaged failed-step fingerprint ({name}): kept", flags(gid)[0] == ("failed", True), str(flags(gid)))
    for name, bad in {"string": "abc", "list": [1], "number": 7}.items():
        gid = store_goal(unrel)
        tampered(gid, flag={0: True}, fp=(0, bad))
        check(f"I  stored non-dict fp ({name}) is read as no fingerprint: kept", flags(gid)[0] == ("failed", True), str(flags(gid)))

    # I.4 other versions / other keys
    new_key()
    k1_failed, k1_later = fp_of(W, {"name": "A"}), fp_of(W, {"name": "B"})
    k1_equiv = fp_of(W, {"name": "A", "text": "again"})
    new_key()
    k2_failed, k2_later = fp_of(W, {"name": "A"}), fp_of(W, {"name": "B"})
    k2_equiv = fp_of(W, {"name": "A", "text": "again"})
    check("I  two keys have different key ids", k1_failed["kid"] != k2_failed["kid"])
    check("I  ...and the same call has different digests under them", k1_failed["i"] != k2_failed["i"] and k1_failed["o"] != k2_failed["o"])
    ver_ = StepState.VERIFIED
    check("I  fingerprints made under another key than the current one: undecided (even an unrelated pair)",
          objectivefp.recovered(rec(W, StepState.FAILED, k1_failed), [rec(W, ver_, k1_later)]) is None)
    check("I  ...even an equivalent pair, which would otherwise be a True",
          objectivefp.recovered(rec(W, StepState.FAILED, k1_failed), [rec(W, ver_, k1_equiv)]) is None)
    check("I  current-key fingerprints decide", objectivefp.recovered(rec(W, StepState.FAILED, k2_failed), [rec(W, ver_, k2_later)]) is False
          and objectivefp.recovered(rec(W, StepState.FAILED, k2_failed), [rec(W, ver_, k2_equiv)]) is True)
    check("I  mixed: failed under the current key, later under another: undecided", objectivefp.recovered(rec(W, StepState.FAILED, k2_failed), [rec(W, ver_, k1_later)]) is None)
    check("I  ...and the reverse", objectivefp.recovered(rec(W, StepState.FAILED, k1_failed), [rec(W, ver_, k2_later)]) is None)
    # `recovered` over several later steps
    Fk, Lk, Uk = k2_failed, k2_equiv, k2_later
    for label, later, want in (
        ("a COMPLETED equivalent step is no evidence", [rec(W, StepState.COMPLETED, Lk)], False),
        ("an UNKNOWN equivalent step is no evidence", [rec(W, StepState.UNKNOWN, Lk)], False),
        ("a FAILED equivalent step is no evidence", [rec(W, StepState.FAILED, Lk)], False),
        ("completed-equivalent then verified-unrelated", [rec(W, StepState.COMPLETED, Lk), rec(W, ver_, Uk)], False),
        ("verified-unrelated then verified-equivalent", [rec(W, ver_, Uk), rec(W, ver_, Lk)], True),
        ("verified without fp, then verified-equivalent: the equivalent one decides", [rec(W, ver_, None), rec(W, ver_, Lk)], True),
        ("verified without fp, then verified-unrelated: undecided", [rec(W, ver_, None), rec(W, ver_, Uk)], None),
        ("verified-unrelated only", [rec(W, ver_, Uk)], False),
        ("no later steps", [], False),
    ):
        check(f"I  recovered(): {label}", objectivefp.recovered(rec(W, StepState.FAILED, Fk), later) is want, str(objectivefp.recovered(rec(W, StepState.FAILED, Fk), later)))
    check("I  a failed step with no fingerprint: undecided", objectivefp.recovered(rec(W, StepState.FAILED, None), [rec(W, ver_, Lk)]) is None)
    # reconstruct with stored fingerprints of an older key
    new_key()
    gid = store_goal(unrel)
    tampered(gid, flag={0: True})
    check("I  stored under key 1, current key 1: dropped", flags(gid)[0] == ("failed", False))
    new_key()
    check("I  the key changed since (stored fingerprints belong to another key): kept, never a false 'unrelated'", flags(gid)[0] == ("failed", True), str(flags(gid)))

    # ---------------------------------------------------------------------------------------------------------------------- J
    scenario("J  persistence round trips, non-mutation, refresh stability, nothing shown")
    new_key()
    gid = store_goal([obs(W, {"name": "A", "text": SENT_TEXT}, "contra"), obs(W, {"name": "A", "text": "v2"}, "verified"),
                      obs(W, {"name": "B"}, "contra"), obs(W, {"name": "C"}, "verified")])
    stored = prog(gid)
    fps_stored = [s_.get("fp") for s_ in stored["steps"]]
    check("J  the contradicted failures and the verified steps after them are fingerprinted", all(isinstance(x, dict) for x in fps_stored), str(fps_stored))
    rep = steps.reconstruct(gid, probe_dead, reconcile_recovery=False)
    check("J  reconstruct carries each fingerprint verbatim", [s_.fp for s_ in rep.steps] == fps_stored)
    again = steps.progress_of(rep, "completed")
    check("J  progress_of writes them back unchanged (the whole step list round-trips)", again["steps"] == stored["steps"], str(again["steps"]))
    check("J  _steps_view treats them as part of the record", steps._steps_view(again) == steps._steps_view(stored))  # noqa: SLF001
    changed = prog(gid)
    changed["steps"][0]["fp"] = {**fps_stored[0], "i": "0" * 32}
    check("J  ...and a different fingerprint is a different view (so a real change is still written)", steps._steps_view(changed) != steps._steps_view(stored))  # noqa: SLF001

    rows_before = [tuple(r) for r in store.conn().execute("SELECT * FROM audit ORDER BY id").fetchall()]
    goal_before = stored_json(gid)
    all_goals = json.dumps([(r["id"], r["progress"]) for r in store.conn().execute("SELECT id, progress FROM goals ORDER BY id").fetchall()])
    steps.reconstruct(gid, probe_dead)
    steps.reconstruct(gid, probe_dead, reconcile_recovery=False)
    check("J  reconstruct (both modes) leaves the stored progress byte-identical", stored_json(gid) == goal_before)
    check("J  ...every goal's stored progress", all_goals == json.dumps([(r["id"], r["progress"]) for r in store.conn().execute("SELECT id, progress FROM goals ORDER BY id").fetchall()]))
    check("J  ...and every audit row", rows_before == [tuple(r) for r in store.conn().execute("SELECT * FROM audit ORDER BY id").fetchall()])
    snapshot = copy.deepcopy(goals_mod.get(gid).progress)
    tampered(gid, flag={2: True})                      # a stale flag that the fingerprints will drop in the account
    stale_json = stored_json(gid)
    check("J  the read path drops the stale flag...", flags(gid)[2] == ("failed", False))
    check("J  ...without writing the correction back (stored JSON byte-identical)", stored_json(gid) == stale_json)
    steps.refresh_goals(probe_dead)
    after1 = stored_json(gid)
    n2 = steps.refresh_goals(probe_dead)
    check("J  refresh_goals: the second call rewrites nothing", n2 == 0, str(n2))
    check("J  ...the stored JSON is byte-identical after both calls", stored_json(gid) == after1 == stale_json, stored_json(gid)[:200])
    check("J  ...and the fingerprints are still in it", [s_.get("fp") for s_ in prog(gid)["steps"]] == fps_stored)
    del snapshot
    # settle_interrupted_goals carries the fingerprints as well
    g2 = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "A", "text": "v2"}, "verified")], status="running")
    before_fps = [s_.get("fp") for s_ in prog(g2)["steps"]]
    steps.settle_interrupted_goals(probe_dead)
    check("J  settle_interrupted_goals writes the rebuilt progress with fingerprints intact", [s_.get("fp") for s_ in prog(g2)["steps"]] == before_fps
          and all(before_fps), str(prog(g2)))
    rep = steps.reconstruct(gid, probe_dead)
    shown = json.dumps(rep.to_dict()) + " " + rep.tail() + " " + " ".join(repr(s_.to_dict()) for s_ in rep.steps)
    check("J  nothing user-facing carries a fingerprint (to_dict, tail)", not any(v in shown for v in fp_values(prog(gid))) and '"fp"' not in shown)
    check("J  ...nor the sentinel content", not tokens_in(shown) and not tokens_in(stored_json(gid)))
    check("J  a step known only from the audit log has no fingerprint", all(s_.fp is None for s_ in steps.reconstruct(store_goal([]), probe_dead).steps))

    # ---------------------------------------------------------------------------------------------------------------------- K
    scenario("K  the fingerprint itself is private and unambiguous")
    new_key()
    fp = fp_of(W, {"name": "SECRET-NAME-5150", "text": SENT_TEXT})
    check("K  only the documented fields", set(fp) <= objectivefp._FIELDS and set(fp) >= objectivefp._REQUIRED)  # noqa: SLF001
    check("K  digests are lower-case hex of the stated lengths", all(len(fp[k_]) == 32 and set(fp[k_]) <= set("0123456789abcdef") for k_ in ("i", "o", "s"))
          and len(fp["kid"]) == 16)
    dumped = json.dumps(fp)
    check("K  no raw name or content in it", "secret-name-5150" not in dumped.lower() and not tokens_in(dumped))
    check("K  the version is recorded", fp["v"] == objectivefp.FP_VERSION == 1)
    check("K  the same key and the same call give the same fingerprint", fp == fp_of(W, {"name": "SECRET-NAME-5150", "text": SENT_TEXT}))
    base_call = {"path": r"C:\fp\k.txt", "content": "abc"}
    b0 = fp_of("files.write", base_call)
    check("K  changing only the content changes 'i', not 'o'", fp_of("files.write", {**base_call, "content": "abd"})["i"] != b0["i"]
          and fp_of("files.write", {**base_call, "content": "abd"})["o"] == b0["o"])
    check("K  changing the path changes 'i', 'o' and 's'", all(fp_of("files.write", {**base_call, "path": r"C:\fp\l.txt"})[k_] != b0[k_] for k_ in ("i", "o", "s")))
    check("K  changing the tool changes 'i' (the identity includes it)", fp_of("files.copy", {"src": base_call["path"], "dst": base_call["path"]})["i"] != b0["i"])
    check("K  an extra OMITted argument changes 'i' only", fp_of("files.write", {**base_call, "mode": "x"})["o"] == b0["o"])
    amb1 = fp_of(TWO, {"a": "ab", "b": "c"})
    amb2 = fp_of(TWO, {"a": "a", "b": "bc"})
    check("K  field boundaries are unambiguous: ('ab','c') != ('a','bc')", amb1["o"] != amb2["o"] and amb1["s"] != amb2["s"] and amb1["i"] != amb2["i"])
    check("K  _enc is length-prefixed: parts, label and count all matter",
          objectivefp._enc("l", "ab", "c") != objectivefp._enc("l", "a", "bc") and objectivefp._enc("l", "a") != objectivefp._enc("l2", "a")  # noqa: SLF001
          and objectivefp._enc("l", "a", "") != objectivefp._enc("l", "a") and objectivefp._enc("l", "") != objectivefp._enc("l"))  # noqa: SLF001
    k1 = objectivefp.subkey()
    new_key()
    k2 = objectivefp.subkey()
    check("K  two different keys give different subkeys, key ids and digests", k1 != k2 and objectivefp.kid_of(k1) != objectivefp.kid_of(k2)
          and objectivefp.make("files.write", base_call, key=k1)["i"] != objectivefp.make("files.write", base_call, key=k2)["i"])
    check("K  the key id does not reveal the subkey", k1.hex()[:16] != objectivefp.kid_of(k1) and k1.hex() not in json.dumps(objectivefp.make("files.write", base_call, key=k1)))
    # an attacker who knows the value but not the key cannot reproduce a digest
    v_path = steps.objective("files.write", {"path": r"C:\fp\k.txt"})[1][0][1]
    f_ = objectivefp.make("files.write", {"path": r"C:\fp\k.txt"}, key=k1)
    guesses = set()
    for empty_key in (b"", b"\x00" * 32, b"friday", hashlib.sha256(b"friday").digest()):
        guesses.add(objectivefp._mac(empty_key, "valueset", v_path))   # noqa: SLF001
        guesses.add(objectivefp._mac(empty_key, "objective", "target", "path", v_path))  # noqa: SLF001
    for raw in (v_path, r"C:\fp\k.txt", r"c:\fp\k.txt"):
        for h in (hashlib.sha256, hashlib.sha1, hashlib.md5):
            d = h(raw.encode()).hexdigest()
            guesses.update({d[:32], d[:16]})
        guesses.add(hmac.new(b"", raw.encode(), hashlib.sha256).hexdigest()[:32])
    check("K  an unkeyed / empty-key hash of a low-entropy path does not reproduce 'o' or 's'", f_["o"] not in guesses and f_["s"] not in guesses)
    check("K  ...nor the subkey's own derivation by a different purpose", f_["o"] != objectivefp._mac(keystore.derive("another-purpose", 1), "objective", "target", "path", v_path))  # noqa: SLF001
    args = {"path": r"C:\fp\k.txt", "content": "abc"}
    keep = copy.deepcopy(args)
    objectivefp.make("files.write", args, key=k1)
    check("K  make() never mutates its arguments", args == keep)
    real_kind, real_identity = steps._action_kind, verify._identity  # noqa: SLF001
    steps._action_kind = lambda tool: "x" * 65  # noqa: SLF001
    try:
        check("K  an over-long action kind yields no fingerprint rather than a stored one", objectivefp.make("files.write", args, key=k1) is None)
    finally:
        steps._action_kind = real_kind  # noqa: SLF001
    verify._identity = lambda tool, a: (_ for _ in ()).throw(ValueError("SENTINEL-IDENT-6606 " + str(a)))  # noqa: SLF001
    try:
        check("K  a call that cannot be fingerprinted yields None, not an exception", objectivefp.make("files.write", args, key=k1) is None)
    finally:
        verify._identity = real_identity  # noqa: SLF001
    check("K  ...and its error text (which held the arguments) was not logged", "sentinel-ident-6606" not in "\n".join(cap.lines).lower()
          and "private" not in "\n".join(cap.lines).lower())
    # Phase 89 follow-ups: full-match validation, and an empty stored fingerprint is "no fingerprint"
    for fld in ("kid", "i", "o", "s"):
        bad = {**fp, fld: fp[fld] + "\n"}
        check(f"K  a trailing newline on '{fld}' makes the fingerprint invalid", objectivefp.valid(bad) is False and objectivefp.valid(fp) is True)
        check(f"K  ...and undecided for recovery ({fld})", objectivefp.recovered(rec(W, StepState.FAILED, bad), [rec(W, StepState.VERIFIED, fp)]) is None)
    check("K  a leading newline or space is invalid too", objectivefp.valid({**fp, "i": "\n" + fp["i"]}) is False and objectivefp.valid({**fp, "kid": " " + fp["kid"]}) is False)
    new_key()
    gid = store_goal([obs(W, {"name": "A"}, "contra"), obs(W, {"name": "B"}, "verified")])
    tampered(gid, flag={0: True}, fp=(0, {}))
    empty_json = stored_json(gid)
    check("K  an empty stored fp is read as absent: reconstruct gives the step no fingerprint", steps.reconstruct(gid, probe_dead, reconcile_recovery=False).steps[0].fp is None)
    check("K  ...the view of {} equals the view of no fp at all", steps._steps_view(prog(gid)) == steps._steps_view(  # noqa: SLF001
        {**prog(gid), "steps": [{k_: v for k_, v in s_.items() if k_ != "fp"} if n_ == 0 else s_ for n_, s_ in enumerate(prog(gid)["steps"])]}))
    check("K  ...so the flag is undecided and kept", flags(gid)[0] == ("failed", True), str(flags(gid)))
    steps.refresh_goals(probe_dead)
    check("K  refresh_goals first call leaves the stored bytes alone", stored_json(gid) == empty_json, stored_json(gid)[:200])
    check("K  ...and the second call rewrites nothing", steps.refresh_goals(probe_dead) == 0 and stored_json(gid) == empty_json)
    all_logs = "\n".join(cap.lines)
    seen = []
    for r_ in store.conn().execute("SELECT progress FROM goals").fetchall():
        try:
            seen += fp_values(json.loads(r_["progress"] or "{}"))
        except ValueError:
            pass
    check("K  across the whole run no stored digest, key or sentinel reached the log", seen and not any(v in all_logs for v in seen) and not tokens_in(all_logs)
          and k1.hex() not in all_logs and k2.hex() not in all_logs)
    stored_text = " ".join(str(r_["progress"]) for r_ in store.conn().execute("SELECT progress FROM goals").fetchall())
    check("K  across the whole run no sentinel reached any stored progress", not tokens_in(stored_text), str(tokens_in(stored_text)))


if __name__ == "__main__":
    sys.exit(main())
