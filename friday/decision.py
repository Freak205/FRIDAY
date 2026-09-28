"""Phase 19.0 — the typed validation boundary around the planner's decision contract.

The planner LLM (`friday.orchestrator.Orchestrator.run_goal`) has always been
asked for exactly one JSON object per turn:

    {"action": "call", "tool": "<name>", "args": {...},
     "reason": "...", "expected_outcome": "...", "subgoal_index": <int>}
    {"action": "done", "summary": "..."}
    {"action": "ask",  "question": "..."}          # discovery mode only

This module does NOT invent a parallel schema — it makes that existing
contract explicit and enforces it in two deliberately separate steps:

    PARSE     `extract_json_object`  "Can I recover one JSON object from this text?"
    VALIDATE  `validate_decision`    "Is that object a valid FRIDAY decision?"

Parse success is never decision success: `{"action": "call", "tool":
"browser.open", "args": {}}` is well-formed JSON and can still be an invalid
decision (wrong tool name, missing required argument, ...). Everything here is
pure and deterministic — no model call, no I/O, no registry mutation — so it is
cheap to test exhaustively (scripts/smoke_llm_decision_parser.py) and can
never itself become a way to execute something.

Safe normalization only. Harmless formatting is recovered (markdown fences,
whitespace, a short prose preamble/postamble, a one-element list wrapping the
object, a single-key wrapper such as {"decision": {...}}, action-name case,
numeric strings for int params, a JSON-encoded string standing in for the args
object). A *semantic* field is never guessed: a reply with a "tool" but no
"action", or plain English like "I think you should open VS Code.", is never
turned into an action — it becomes an `InvalidDecision` with a structured
reason, which the orchestrator may spend one bounded repair attempt on (and
which is never executed either way).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# -- limits -------------------------------------------------------------------

# "Short surrounding prose" only. A reply that is mostly an essay with a JSON
# object somewhere inside it is not a decision we should guess at.
MAX_SURROUNDING_PROSE_CHARS = 400
# Bounds on what an InvalidDecision retains / what a repair prompt may echo back.
RAW_EXCERPT_CHARS = 200
_TEXT_FIELD_CHARS = 300


# The keys of the existing decision contract. Anything else at the top level is
# "unknown" — ignored when harmless noise (`"confidence": 0.9`), but never
# silently discarded when it looks like the model FLATTENED a tool's arguments
# next to "action" instead of nesting them under "args".
_DECISION_KEYS = frozenset({
    "action", "tool", "args", "reason", "expected_outcome", "subgoal_index", "summary", "question",
})

# -- vocabulary ---------------------------------------------------------------


class DecisionKind(str, Enum):
    CALL = "call"
    DONE = "done"
    ASK = "ask"


class InvalidReason(str, Enum):
    """Why a model reply is not a valid decision. Every member is an
    LLM-output *format/contract* failure — never a tool failure, a permission
    denial, a declined confirmation, or a user cancellation (those are
    decided after a decision is accepted, by the executor, and never flow
    back into structured-output repair)."""

    EMPTY_OUTPUT = "empty_output"
    MALFORMED_JSON = "malformed_json"  # no JSON object recoverable (prose only, truncated, bad syntax)
    UNEXPECTED_SHAPE = "unexpected_shape"  # JSON, but not decision-shaped / ambiguous / over-wrapped
    MISSING_ACTION = "missing_action"
    UNKNOWN_ACTION = "unknown_action"
    MISSING_TOOL = "missing_tool"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGS = "invalid_args"
    # A REPAIR reply that declares "done" although nothing has been observed:
    # a repair is asked to fix the *format* of a decision, not to stand in for
    # completing the goal. (A first-attempt "done" with no evidence is not
    # rejected here — the evaluator decides what it is worth; see
    # friday.intelligence.evaluator.evaluate_goal.)
    UNSUPPORTED_DONE = "unsupported_done"


@dataclass(frozen=True, slots=True)
class Decision:
    """A validated planner decision. `raw` is the normalized dict (unknown keys
    already dropped) — `_resolve_subgoal_index` reads `subgoal_index` from it."""

    kind: DecisionKind
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    question: str = ""
    reason: str = ""
    expected_outcome: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # Non-empty when a harmless-but-not-bare structure was normalized to get
    # here (currently only "action_was_tool_name") — for metrics/audit; it
    # never changes what the decision means or how it is executed.
    recovered: str = ""


@dataclass(frozen=True, slots=True)
class InvalidDecision:
    """INVALID_DECISION with a structured reason. Never executed."""

    reason: InvalidReason
    detail: str = ""
    raw_excerpt: str = ""
    # The tool name the failure was about (unknown_tool / invalid_args), so the
    # repair prompt can show just that one tool's signature.
    tool: str = ""

    def describe(self) -> str:
        return f"{self.reason.value}: {self.detail}" if self.detail else self.reason.value

    def to_dict(self) -> dict[str, str]:
        return {"reason": self.reason.value, "detail": self.detail}


# -- step 1: parse ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Extraction:
    obj: dict[str, Any] | None
    failure: InvalidDecision | None = None
    # True when anything beyond a bare `{...}` had to be stripped/unwrapped —
    # informational (metrics/tests); never changes what the decision means.
    normalized: bool = False


_DECODER = json.JSONDecoder()
_FENCE_MARKER = re.compile(r"```[A-Za-z]*")


def _excerpt(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:RAW_EXCERPT_CHARS] + ("…" if len(text) > RAW_EXCERPT_CHARS else "")


def _balanced_end(text: str, start: int) -> int | None:
    """Index just past the bracket that closes the `{`/`[` at `start` (string-
    and escape-aware depth count), or None if it never closes — i.e. the
    reply is truncated/unbalanced from here on."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def scan_json_values(text: str) -> list[tuple[int, int, Any]]:
    """Every top-level JSON object/array embedded in `text`, as (start, end,
    value), left to right and non-overlapping. Positions that don't begin a
    valid JSON value are skipped — this is what makes "prose, then JSON" and
    a stray `{curly}` in the preamble both work without a fuzzy parser.

    A bracket that opens but never closes means the reply is truncated from
    there: scanning STOPS (a `None` sentinel value is appended at that
    position), so a complete object nested inside a truncated one — e.g. the
    `{}` of `"args": {}` in a reply cut off mid-way — is never mistaken for
    the reply's own object."""
    found: list[tuple[int, int, Any]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "{[":
            try:
                value, end = _DECODER.raw_decode(text, i)
            except json.JSONDecodeError:
                end = _balanced_end(text, i)
                if end is None:
                    found.append((i, n, None))  # truncated tail
                    break
                i = end  # balanced but not valid JSON ({curly}, single quotes, trailing comma): skip it whole
                continue
            found.append((i, end, value))
            i = end
        else:
            i += 1
    return found


def _unwrap(obj: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """`{"decision": {...}}`-style single-key wrapper -> the inner object.
    Only when the outer object has exactly one key and the inner value is a
    dict that itself carries an "action" — never a guess about a multi-key
    object."""
    if "action" not in obj and len(obj) == 1:
        (inner,) = obj.values()
        if isinstance(inner, dict) and "action" in inner:
            return inner, True
    return obj, False


def extract_json_object(text: str | None) -> Extraction:
    """Recover exactly one JSON object from a model reply, or say precisely why not."""
    raw = text or ""
    stripped = raw.strip()
    if not stripped:
        return Extraction(None, InvalidDecision(InvalidReason.EMPTY_OUTPUT, "the reply was empty"))

    values = scan_json_values(stripped)
    truncated = any(v is None for _s, _e, v in values)
    values = [t for t in values if t[2] is not None]

    candidates: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    normalized = False
    for start, end, value in values:
        if isinstance(value, dict):
            candidates.append(value)
            spans.append((start, end))
        elif isinstance(value, list):
            dicts = [v for v in value if isinstance(v, dict)]
            if dicts:
                normalized = True  # a list wrapping object(s)
                for d in dicts:
                    candidates.append(d)
                    spans.append((start, end))

    if truncated:
        # A bracket that never closes: the reply was cut off (or is unbalanced).
        # Whatever complete objects precede it are not trusted on their own —
        # the cut-off part may have been the actual decision.
        return Extraction(None, InvalidDecision(
            InvalidReason.MALFORMED_JSON, "the JSON object is truncated/unbalanced", _excerpt(stripped),
        ))

    if not candidates:
        if values:
            return Extraction(None, InvalidDecision(
                InvalidReason.UNEXPECTED_SHAPE, "the JSON value is not an object", _excerpt(stripped),
            ))
        if "{" not in stripped:
            detail = "no JSON object in the reply (plain text)"
        elif stripped.count("{") > stripped.count("}"):
            detail = "the JSON object is truncated/unbalanced"
        else:
            detail = "the JSON object has invalid syntax"
        return Extraction(None, InvalidDecision(InvalidReason.MALFORMED_JSON, detail, _excerpt(stripped)))

    unwrapped: list[dict[str, Any]] = []
    for c in candidates:
        inner, did = _unwrap(c)
        normalized = normalized or did
        unwrapped.append(inner)

    # De-duplicate identical objects (a model repeating itself is not ambiguity).
    unique: list[dict[str, Any]] = []
    for c in unwrapped:
        if c not in unique:
            unique.append(c)

    with_action = [c for c in unique if "action" in c]
    if len(with_action) > 1:
        return Extraction(None, InvalidDecision(
            InvalidReason.UNEXPECTED_SHAPE,
            f"the reply contains {len(with_action)} different decision objects — exactly one is required",
            _excerpt(stripped),
        ))
    if len(with_action) == 1:
        chosen = with_action[0]
    elif len(unique) == 1:
        chosen = unique[0]
    else:
        return Extraction(None, InvalidDecision(
            InvalidReason.UNEXPECTED_SHAPE,
            f"the reply contains {len(unique)} JSON objects and none is a decision",
            _excerpt(stripped),
        ))

    # Surrounding prose must be short — measured against the text outside the
    # JSON spans, ignoring whitespace and code-fence markers.
    covered = sorted(set(spans))
    outside_parts: list[str] = []
    cursor = 0
    for start, end in covered:
        if start > cursor:
            outside_parts.append(stripped[cursor:start])
        cursor = max(cursor, end)
    outside_parts.append(stripped[cursor:])
    outside = _FENCE_MARKER.sub("", "".join(outside_parts)).strip()
    if outside:
        normalized = True
    if len(outside) > MAX_SURROUNDING_PROSE_CHARS:
        return Extraction(None, InvalidDecision(
            InvalidReason.UNEXPECTED_SHAPE,
            f"too much prose around the JSON ({len(outside)} chars) — reply with the JSON object only",
            _excerpt(stripped),
        ))
    return Extraction(chosen, None, normalized)


# -- step 2: validate ---------------------------------------------------------


@dataclass(slots=True)
class ToolCatalog:
    """What the validator needs to know about tools — reusing the existing
    skill registry rather than duplicating it.

    `offered`  the tool names the planner was actually shown this run.
    `allowed`  the orchestrator's own allow-list (None = any registered skill).
    `params_of` optional lookup returning a registered skill's `Param` list
               (name/type/required) or None for a tool with no registry
               metadata (e.g. a test double); defaults to `REGISTRY`.
    """

    offered: Collection[str] = ()
    allowed: Collection[str] | None = None
    params_of: Callable[[str], Sequence[Any] | None] | None = None
    exists: Callable[[str], bool] | None = None

    def knows(self, tool: str) -> bool:
        """A name is "known" if the planner was shown it, the orchestrator's
        allow-list names it, or it is a registered skill. A *known* tool that
        just isn't permitted in this run is a POLICY stop (existing
        `tool_not_allowed` path in `Orchestrator._run_step`), not an LLM
        format failure — so it never enters structured-output repair."""
        if tool in self.offered:
            return True
        if self.allowed is not None and tool in self.allowed:
            return True
        if self.exists is not None:
            return bool(self.exists(tool))
        from friday.registry import REGISTRY

        return REGISTRY.get(tool) is not None

    def params(self, tool: str) -> Sequence[Any] | None:
        if self.params_of is not None:
            return self.params_of(tool)
        from friday.registry import REGISTRY

        skill = REGISTRY.get(tool)
        return None if skill is None else skill.params


def _invalid(
    reason: InvalidReason, detail: str, obj: dict[str, Any] | None = None, tool: str = "",
) -> InvalidDecision:
    excerpt = ""
    if obj is not None:
        try:
            excerpt = _excerpt(json.dumps(obj, default=str, ensure_ascii=False))
        except (TypeError, ValueError):
            excerpt = ""
    return InvalidDecision(reason, detail, excerpt, tool)


def _text_field(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()[:_TEXT_FIELD_CHARS]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _coerce_param(name: str, ptype: Any, value: Any) -> tuple[Any, str]:
    """(normalized value, error). Only harmless scalar normalization; anything
    else that doesn't match the declared type is an error, never a guess."""
    if ptype is bool:
        if isinstance(value, bool):
            return value, ""
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true", ""
        return value, f"'{name}' must be true or false"
    if ptype is int:
        if isinstance(value, bool):
            return value, f"'{name}' must be an integer"
        if isinstance(value, int):
            return value, ""
        if isinstance(value, float) and value.is_integer():
            return int(value), ""
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value.strip()), ""
        return value, f"'{name}' must be an integer"
    if ptype is float:
        if isinstance(value, bool):
            return value, f"'{name}' must be a number"
        if isinstance(value, (int, float)):
            return value, ""
        if isinstance(value, str):
            try:
                return float(value.strip()), ""
            except ValueError:
                pass
        return value, f"'{name}' must be a number"
    if ptype is str:
        if isinstance(value, str):
            return value, ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value), ""
        return value, f"'{name}' must be a string"
    if ptype is list:
        return (value, "") if isinstance(value, list) else (value, f"'{name}' must be a list")
    if ptype is dict:
        return (value, "") if isinstance(value, dict) else (value, f"'{name}' must be an object")
    return value, ""  # an annotation this layer doesn't understand: leave it to the skill


def _validate_args(tool: str, raw_args: Any, catalog: ToolCatalog) -> tuple[dict[str, Any] | None, InvalidDecision | None]:
    if raw_args is None:
        args: Any = {}
    elif isinstance(raw_args, str):
        # A JSON-encoded string standing in for the object — recoverable only
        # if it decodes to an object; a bare string is not an args object.
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = None
        if not isinstance(args, dict):
            return None, InvalidDecision(
                InvalidReason.INVALID_ARGS, "args must be a JSON object, not a string",
            )
    else:
        args = raw_args
    if not isinstance(args, dict):
        return None, InvalidDecision(
            InvalidReason.INVALID_ARGS, f"args must be a JSON object, not {type(args).__name__}",
        )
    if not all(isinstance(k, str) for k in args):
        return None, InvalidDecision(InvalidReason.INVALID_ARGS, "argument names must be strings")

    params = catalog.params(tool)
    if params is None:
        return dict(args), None  # no registry metadata: structure-only check

    by_name = {p.name: p for p in params}
    unknown = sorted(k for k in args if k not in by_name)
    if unknown:
        valid = ", ".join(by_name) or "none"
        return None, InvalidDecision(
            InvalidReason.INVALID_ARGS,
            f"'{tool}' has no argument(s) {', '.join(repr(k) for k in unknown)}; valid arguments: {valid}",
        )

    out: dict[str, Any] = {}
    for name, value in args.items():
        p = by_name[name]
        if value is None and not p.required:
            continue  # null for an optional argument == omitted
        normalized, err = _coerce_param(name, p.type, value)
        if err:
            return None, InvalidDecision(InvalidReason.INVALID_ARGS, f"'{tool}': {err}")
        out[name] = normalized
    missing = [p.name for p in params if p.required and p.name not in out]
    if missing:
        return None, InvalidDecision(
            InvalidReason.INVALID_ARGS,
            f"'{tool}' is missing required argument(s): {', '.join(missing)}",
        )
    return out, None


def validate_decision(
    obj: dict[str, Any] | None, *, catalog: ToolCatalog, allow_ask: bool = False,
    recover_action_as_tool: bool = True,
) -> Decision | InvalidDecision:
    """Does `obj` represent a valid FRIDAY decision? Never raises, never guesses.

    `recover_action_as_tool` (default on) is the single normalization here
    that touches a semantic field, and it exists because it was the dominant
    real failure mode of qwen2.5:3b in Phase 19.0's baseline capture: the
    model puts the tool name in the "action" slot —
    `{"action": "system.time", "reason": "..."}`. It is applied only when the
    "action" string EXACTLY equals a tool name the planner was offered this
    run and no conflicting "tool" field is present; the resulting call then
    goes through the same tool/args validation, the same repeat guard, and the
    same permission executor as any other call. An unrelated string
    ("open_vscode", "navigate") is still an unknown_action."""
    if not isinstance(obj, dict):
        return InvalidDecision(InvalidReason.UNEXPECTED_SHAPE, "the reply is not a JSON object")

    if "action" not in obj or obj.get("action") in (None, ""):
        return _invalid(InvalidReason.MISSING_ACTION, 'the object has no "action" field', obj)
    action_raw = obj["action"]
    if not isinstance(action_raw, str):
        return _invalid(InvalidReason.UNEXPECTED_SHAPE, '"action" must be a string', obj)
    action = action_raw.strip().lower()
    valid_actions = ["call", "done"] + (["ask"] if allow_ask else [])
    recovered = ""
    if action not in valid_actions and recover_action_as_tool:
        named = action_raw.strip()
        if named in catalog.offered and obj.get("tool") in (None, "", named):
            stray = [k for k in obj if k not in _DECISION_KEYS]
            params = catalog.params(named)
            flattened = sorted(k for k in stray if params is not None and k in {p.name for p in params})
            if flattened:
                # {"action": "ui.read", "app": ..., "limit": ...}: the arguments sit next to
                # "action". Recovering would silently drop them (the tool would run with its
                # defaults instead of what the model asked for) — invalid, so it is repaired.
                return _invalid(
                    InvalidReason.INVALID_ARGS,
                    f"argument(s) {', '.join(repr(k) for k in flattened)} must be inside the \"args\" object, "
                    'and "action" must be "call" with the tool name in "tool"', obj, named,
                )
            if not stray or params is not None:
                obj = {**obj, "tool": named}
                action, recovered = "call", "action_was_tool_name"
    if action not in valid_actions:
        return _invalid(
            InvalidReason.UNKNOWN_ACTION,
            f'unknown action {action_raw!r}; valid actions: {", ".join(valid_actions)}', obj,
        )

    reason = _text_field(obj.get("reason"))
    expected = _text_field(obj.get("expected_outcome"))

    if action == "done":
        raw = {"action": "done", "summary": _text_field(obj.get("summary"))}
        return Decision(DecisionKind.DONE, summary=raw["summary"], reason=reason, raw=raw)

    if action == "ask":
        question = obj.get("question")
        if not isinstance(question, str) or not question.strip():
            return _invalid(InvalidReason.UNEXPECTED_SHAPE, '"ask" needs a non-empty "question"', obj)
        q = question.strip()[:_TEXT_FIELD_CHARS]
        return Decision(DecisionKind.ASK, question=q, reason=reason, raw={"action": "ask", "question": q})

    tool = obj.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return _invalid(InvalidReason.MISSING_TOOL, '"call" needs a non-empty "tool" name', obj)
    tool = tool.strip()
    if not catalog.knows(tool):
        return _invalid(InvalidReason.UNKNOWN_TOOL, f"'{tool}' is not an available tool", obj, tool)

    if tool not in catalog.offered:
        # A real tool that simply isn't permitted in this run (e.g. an L1 tool
        # requested during the L0-only discovery pass). That is a POLICY stop —
        # the orchestrator's existing `tool_not_allowed` path rejects it — not
        # an LLM format failure, so it is neither repaired nor arg-validated
        # (its args are irrelevant: nothing will run). Structure-only check.
        raw_args = obj.get("args")
        if raw_args is not None and not isinstance(raw_args, dict):
            raw_args = {}
        policy_args = dict(raw_args or {})
        return Decision(
            DecisionKind.CALL, tool=tool, args=policy_args, reason=reason, expected_outcome=expected,
            raw={"action": "call", "tool": tool, "args": policy_args, "expected_outcome": expected},
            recovered=recovered,
        )

    args, err = _validate_args(tool, obj.get("args"), catalog)
    if err is not None:
        return _invalid(err.reason, err.detail, obj, tool)
    assert args is not None

    # Arguments placed NEXT TO "action" instead of under "args" — e.g.
    # {"action": "ui.read", "app": "Chrome", "limit": 4000}. Silently calling the
    # tool without them would change what the model asked for, so it is invalid
    # (repaired), never quietly dropped. Needs the tool's parameter names, so it
    # applies to registry-backed runs; without them, unknown keys stay harmless noise.
    params = catalog.params(tool)
    if params is not None:
        param_names = {p.name for p in params}
        flattened = sorted(k for k in obj if k not in _DECISION_KEYS and k in param_names)
        if flattened:
            return _invalid(
                InvalidReason.INVALID_ARGS,
                f"argument(s) {', '.join(repr(k) for k in flattened)} must be inside the \"args\" object",
                obj, tool,
            )

    if isinstance(obj.get("args"), str) and not recovered:
        recovered = "args_json_string"
    raw = {"action": "call", "tool": tool, "args": args, "expected_outcome": expected}
    if "subgoal_index" in obj:
        raw["subgoal_index"] = obj["subgoal_index"]
    return Decision(
        DecisionKind.CALL, tool=tool, args=args, reason=reason, expected_outcome=expected, raw=raw,
        recovered=recovered,
    )


def parse_and_validate(
    text: str | None, *, catalog: ToolCatalog, allow_ask: bool = False,
    recover_action_as_tool: bool = True,
) -> tuple[Decision | InvalidDecision, Extraction]:
    """Both steps, kept separate internally: extraction failure is reported
    as-is; only a successfully extracted object is validated."""
    extraction = extract_json_object(text)
    if extraction.failure is not None:
        return extraction.failure, extraction
    return validate_decision(
        extraction.obj, catalog=catalog, allow_ask=allow_ask,
        recover_action_as_tool=recover_action_as_tool,
    ), extraction


# -- repair prompt --------------------------------------------------------------

_SECRET = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\"?\s*[:=]\s*\"?)([^\s\",}]+)"
)


def _scrub(text: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)


def _suggest(name: str, choices: Collection[str], n: int = 3) -> list[str]:
    import difflib

    return difflib.get_close_matches(name, list(choices), n=n, cutoff=0.6)


def repair_schema_text(*, allow_ask: bool) -> str:
    text = (
        "Reply with ONLY one JSON object — no prose, no markdown fences. "
        'To call a tool: {"action": "call", "tool": "<name from the list>", "args": {"<argument>": <value>}}. '
        'When the goal is satisfied: {"action": "done", "summary": "<result>"}.'
    )
    if allow_ask:
        text += ' If you cannot proceed without the user: {"action": "ask", "question": "<one question>"}.'
    return text


def build_repair_request(
    goal: str,
    invalid: InvalidDecision,
    *,
    tool_names: Sequence[str],
    tool_signature: Callable[[str], str] | None = None,
    recent_steps: Sequence[str] = (),
    allow_ask: bool = False,
    call_tool: str = "",
) -> tuple[str, str]:
    """(system, user) for the single bounded repair attempt. Deliberately
    small: the exact schema, the goal, the tool NAMES (plus the signature of
    the one tool the failure was about), at most three one-line recent steps,
    the reason, and a bounded, secret-scrubbed excerpt of the bad reply. Not
    the whole conversation, not ambient desktop context, not retrieved
    experience — nothing the model needs to fix a formatting/contract error
    and nothing that isn't already in its own previous prompt."""
    system = (
        "You control a desktop assistant by choosing exactly one tool call at a time. "
        + repair_schema_text(allow_ask=allow_ask)
        + " Only use tool names from the list and only the argument names shown for a tool."
    )
    lines = [f"Goal: {goal.strip()[:300]}", "Available tools: " + ", ".join(tool_names)]
    if call_tool and tool_signature is not None:
        sig = tool_signature(call_tool)
        if sig:
            lines.append(f"Arguments for {call_tool}: {sig}")
    if invalid.reason is InvalidReason.UNKNOWN_TOOL and call_tool:
        close = _suggest(call_tool, tool_names)
        if close:
            lines.append("Closest valid tool names: " + ", ".join(close))
    if recent_steps:
        lines.append("Steps so far:\n" + "\n".join(recent_steps[-3:]))
    else:
        lines.append("Steps so far: (nothing yet)")
    lines.append(f"The previous response was invalid because: {invalid.describe()}.")
    if invalid.raw_excerpt:
        lines.append(f"Previous response (truncated): {_scrub(invalid.raw_excerpt)}")
    lines.append("Return ONLY a valid decision matching the required schema.")
    return system, "\n".join(lines)


# -- structured-output schema (Ollama `format`) --------------------------------


def decision_json_schema(tool_names: Sequence[str], *, allow_ask: bool = False) -> dict[str, Any]:
    """JSON Schema for the existing decision contract, for providers that can
    constrain decoding to a schema (Ollama's `format` field). Only ever used
    when `CFG.planner.structured_output` is on — see PLAN.md Phase 19.0 §
    "Native structured output" for the measurement that decides the default."""
    actions = ["call", "done"] + (["ask"] if allow_ask else [])
    props: dict[str, Any] = {
        "action": {"type": "string", "enum": actions},
        "tool": {"type": "string", "enum": list(tool_names)} if tool_names else {"type": "string"},
        "args": {"type": "object"},
        "summary": {"type": "string"},
        "reason": {"type": "string"},
        "expected_outcome": {"type": "string"},
        "subgoal_index": {"type": "integer"},
    }
    if allow_ask:
        props["question"] = {"type": "string"}
    return {"type": "object", "properties": props, "required": ["action"]}
