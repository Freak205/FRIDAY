"""Keyed objective fingerprints (Phase 89): what a stored step was aimed at, without storing what it was aimed at.

Phase 85 could not finish reconciling a legacy recovery flag after a restart: whether a later verified step "pursued the
same objective" (`verify._redone`) needs the arguments, and a stored step has none. A fingerprint keeps just enough of the
decision to be replayed later, and nothing a reader could use to recover the arguments.

What is stored per step (`StepRecord.fp`, persisted as `fp` in the goal's progress):

    v    representation version (1). Any other version is "unavailable", never compared.
    kid  key id: the first 8 bytes of an HMAC of a constant under the SAME subkey. Two fingerprints compare only if both carry
         the id of the key that is available now, so a lost, replaced or different-user key can never produce a false "unrelated".
    k    the step's action kind (`intent.action_label`, a coarse word such as "write"), as `same_objective` uses it
    c    1 if the call can change state (`steps._changes_state`), else 0 -- the rule for a failed READ differs
    t    "t" if the objective was identified by target (names / paths / URLs), "r" if only the whole call can identify it
    i    HMAC of the `verify._identity` string  -> "the identical call" (includes content, so only safe under the HMAC)
    o    HMAC of the `steps.objective` result   -> "same tool, same target" (or an identical raw call)
    s    HMAC of the sorted set of target values -> "another tool, same non-empty target set"; present only for a target
         objective with at least one identifying value

Every digest is HMAC-SHA256 (truncated to 16 bytes) under `keystore.derive("objective-fingerprint", 1)`, over length-prefixed
fields that start with a per-field label, so no two different inputs encode alike and no digest of one field can stand for
another. Raw arguments, paths, URLs, commands and content are never stored; an ordinary hash would NOT be private for such
low-entropy values, which is why the key store exists. Equal digests do reveal that two steps had equal input (inherent).

`equivalent` replays exactly the live decision `verify._redone` = `_identity` equal OR `steps.same_objective`:
  identical call                                  i == i
  failed step is a READ (c == 0)                  same tool, or same action kind
  state change, same tool                         o == o   (same kind and the same identifying pairs, or an identical raw call)
  state change, another tool                      both targets, non-empty, s == s, and the same action kind

`recovered` applies that to a contradicted read-back and its later steps, and answers True / False / None. None means "cannot
tell" (no key, a missing, malformed, other-version or other-key fingerprint): the caller keeps whatever it had and never guesses.
Nothing here writes, and nothing is logged except a reason (never an argument, a digest or a key).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import struct
from typing import Any

from friday import keystore
from friday.log import get

log = get(__name__)

FP_VERSION = 1
PURPOSE = "objective-fingerprint"
KEY_VERSION = 1
_DIGEST = 16   # bytes kept of each HMAC
_KID = 8
_MAX_KIND = 64
_HEX = {32: re.compile(r"^[0-9a-f]{32}$"), 16: re.compile(r"^[0-9a-f]{16}$")}
_FIELDS = frozenset({"v", "kid", "k", "c", "t", "i", "o", "s"})
_REQUIRED = frozenset({"v", "kid", "k", "c", "t", "i", "o"})
_warned: set[str] = set()


def _enc(label: str, *parts: str) -> bytes:
    """Unambiguous encoding: each field is a 4-byte big-endian length then its UTF-8 bytes; the label is the first field and the
    field count the second, so ("a", "bc") and ("ab", "c") -- or a different label over the same text -- never collide."""
    out = [label.encode("utf-8"), str(len(parts)).encode("ascii"), *(p.encode("utf-8", "surrogatepass") for p in parts)]
    return b"".join(struct.pack(">I", len(b)) + b for b in out)


def _mac(subkey: bytes, label: str, *parts: str, size: int = _DIGEST) -> str:
    return hmac.new(subkey, _enc(f"friday-objfp/v{FP_VERSION}/{label}", *parts), hashlib.sha256).digest()[:size].hex()


def subkey() -> bytes | None:
    """The fingerprint subkey, or None when the key store cannot supply one (no key, no DPAPI, an unsafe location, a corrupt key,
    a test entry point refused the real key...). Never raises, never creates a replacement key, never falls back to an unkeyed hash."""
    try:
        return keystore.derive(PURPOSE, KEY_VERSION)
    except Exception as exc:  # noqa: BLE001 -- KeyUnavailable, IsolationError...; the run must not depend on this
        _warn(type(exc).__name__ + ":" + str(getattr(exc, "reason", "")))
        return None


def _warn(what: str) -> None:
    if what not in _warned:
        _warned.add(what)
        log.warning("objective fingerprints unavailable (%s); steps keep their existing behavior", what)


def kid_of(key: bytes) -> str:
    return _mac(key, "kid", size=_KID)


def make(tool: str, args: dict[str, Any] | None, *, key: bytes) -> dict[str, Any] | None:
    """The fingerprint of one call, or None. Called while the arguments are still in hand; the result holds only digests."""
    from friday import steps, verify

    try:
        args = dict(args or {})
        kind = steps._action_kind(tool)                       # noqa: SLF001
        if not isinstance(kind, str) or not kind or len(kind) > _MAX_KIND:
            return None
        okind, pairs = steps.objective(tool, args)
        flat = [x for pair in pairs for x in pair]
        fp: dict[str, Any] = {
            "v": FP_VERSION,
            "kid": kid_of(key),
            "k": kind,
            "c": 1 if steps._changes_state(tool, args) else 0,  # noqa: SLF001
            "t": "t" if okind == "target" else "r",
            "i": _mac(key, "identity", verify._identity(tool, args)),  # noqa: SLF001
            "o": _mac(key, "objective", okind, *flat),
        }
        if okind == "target" and pairs:
            fp["s"] = _mac(key, "valueset", *sorted({v for _, v in pairs}))
        return fp
    except Exception as exc:  # noqa: BLE001
        _warn("make:" + type(exc).__name__)
        return None


def valid(fp: Any) -> bool:
    """Is this a well-formed fingerprint of THIS representation? Strict: unknown fields, wrong types or lengths, another version."""
    if not isinstance(fp, dict) or not _REQUIRED <= set(fp) <= _FIELDS:
        return False
    v = fp.get("v")
    if type(v) is not int or v != FP_VERSION:
        return False
    k = fp.get("k")
    if not isinstance(k, str) or not 0 < len(k) <= _MAX_KIND:
        return False
    if type(fp.get("c")) is not int or fp["c"] not in (0, 1) or fp.get("t") not in ("t", "r"):
        return False
    if not (isinstance(fp.get("kid"), str) and _HEX[16].fullmatch(fp["kid"])):
        return False
    for f in ("i", "o", "s"):
        if f in fp and not (isinstance(fp[f], str) and _HEX[32].fullmatch(fp[f])):
            return False
    return "s" not in fp or fp["t"] == "t"


def equivalent(failed_tool: str, failed: dict[str, Any], later_tool: str, later: dict[str, Any]) -> bool:
    """`verify._redone(failed, later)` from two VALID, same-key fingerprints (see the module docstring for the rule)."""
    if failed["i"] == later["i"]:
        return True
    if not failed["c"]:
        return failed_tool == later_tool or failed["k"] == later["k"]
    if failed_tool == later_tool:
        return failed["o"] == later["o"]
    return (
        failed["t"] == "t" == later["t"] and "s" in failed and "s" in later and failed["s"] == later["s"]
        and failed["k"] == later["k"]
    )


def recovered(failed: Any, later: list[Any]) -> bool | None:
    """Was a contradicted read-back (`failed`, a StepRecord) made good by one of the later steps? Mirrors the live rule: a later
    step recovers it only if it is VERIFIED and `_redone`. True / False when the fingerprints decide it, None when they cannot."""
    from friday.steps import StepState

    if not valid(failed.fp):
        return None
    key = subkey()
    if key is None or failed.fp["kid"] != kid_of(key):
        return None
    undecided = False
    for t in later:
        if t.state is not StepState.VERIFIED:   # a success nothing confirmed now is no evidence of a recovery
            continue
        if not valid(t.fp) or t.fp["kid"] != failed.fp["kid"]:
            undecided = True
            continue
        if equivalent(failed.tool, failed.fp, t.tool, t.fp):
            return True
    return None if undecided else False


def _forget() -> None:  # tests only
    _warned.clear()
