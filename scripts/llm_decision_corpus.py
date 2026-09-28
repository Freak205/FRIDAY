"""Phase 19.0 — the planner-decision corpus used by scripts/smoke_llm_decision_parser.py.

Every entry is one raw model reply plus the deterministic outcome the parse ->
validate boundary (`friday.decision`) must produce for it. Two provenances,
kept explicitly separate so nothing constructed is ever presented as observed:

  observed     verbatim qwen2.5:3b planner replies captured on 2026-09-20 through
               the unmodified pre-Phase-19 pipeline (12 real `plan.run` scenarios
               plus re-sampling each unique production prompt x4 = 156 replies,
               121 distinct). Byte-exact, loaded from
               `llm_decision_corpus_observed.json`; only the selection is editorial.
  constructed  representative replies for failure classes that did not happen to
               occur in that capture (fenced JSON, truncation, empty output, ...) —
               either documented in PLAN.md (Phases 5/15/17/18) or well-known LLM
               output failure classes. Not observed; labelled as such.

`expect` grammar:
  valid:<call|done|ask>            accepted as-is (bare object; whitespace only)
  normalized:<call|done|ask>       accepted after a harmless structural normalization
                                   (fence / prose / list-wrap / wrapper / extra fragment)
  recovered:<marker>               accepted via a named semantic-adjacent recovery
                                   (`Decision.recovered`, e.g. action_was_tool_name)
  invalid:<reason>                 rejected with this InvalidReason — never executed
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_OBSERVED = json.loads(
    (Path(__file__).with_name("llm_decision_corpus_observed.json")).read_text(encoding="utf-8")
)["samples"]


@dataclass(frozen=True)
class Case:
    id: str
    category: str          # one of the 18 required categories (or an extra, prefixed "x-")
    text: str | None
    source: str            # "observed" | "constructed"
    expect: str
    allow_ask: bool = False


def _o(id_: str, category: str, key: str, expect: str, allow_ask: bool = False) -> Case:
    return Case(id_, category, _OBSERVED[key], "observed", expect, allow_ask)


def _c(id_: str, category: str, text: str | None, expect: str, allow_ask: bool = False) -> Case:
    return Case(id_, category, text, "constructed", expect, allow_ask)


CALL_TIME = '{"action":"call","tool":"system.time","args":{}}'

CASES: list[Case] = [
    # 1. valid expected JSON
    _o("valid-observed-screen", "1-valid", "obs_valid_call_screen_observe", "valid:call"),
    _o("valid-observed-files-read-int-arg", "1-valid", "obs_valid_call_files_read_path_and_max_chars", "valid:call"),
    _c("valid-call-min", "1-valid", CALL_TIME, "valid:call"),
    _c("valid-done", "1-valid", '{"action":"done","summary":"All set."}', "valid:done"),
    _c("valid-ask-discovery", "1-valid", '{"action":"ask","question":"Which project do you mean?"}', "valid:ask", True),
    _c("valid-action-case", "1-valid", '{"action":"CALL","tool":"system.time"}', "valid:call"),
    # 2. extra whitespace
    _o("whitespace-observed-pretty-printed", "2-whitespace", "obs_pretty_printed_call", "valid:call"),
    _c("whitespace-padded", "2-whitespace", '  \n\n' + CALL_TIME + '  \n\t', "valid:call"),
    # 3. fenced JSON
    _c("fence-json", "3-fenced", '```json\n' + CALL_TIME + '\n```', "valid:call"),
    _c("fence-bare", "3-fenced", '```\n' + CALL_TIME + '\n```', "valid:call"),
    _c("fence-unclosed", "3-fenced", '```json\n' + CALL_TIME, "valid:call"),
    # 4. prose before / 5. prose after
    _c("prose-before", "4-prose-before", 'Sure, here is the decision:\n' + CALL_TIME, "normalized:call"),
    _c("prose-before-stray-brace", "4-prose-before", 'Using {curly} braces per the schema: ' + CALL_TIME, "normalized:call"),
    _c("prose-after", "5-prose-after", CALL_TIME + '\nLet me know if you need anything else!', "normalized:call"),
    _c("prose-both", "5-prose-after", 'Okay.\n' + CALL_TIME + '\nDone.', "normalized:call"),
    _c("prose-too-long", "5-prose-after", ("This is a long explanation. " * 25) + CALL_TIME, "invalid:unexpected_shape"),
    # 6. valid JSON with unknown keys
    _o("unknown-keys-observed-failure-flag", "6-unknown-keys", "obs_unknown_keys_failure_flag", "valid:call"),
    _o("unknown-keys-observed-cwd", "6-unknown-keys", "obs_unknown_keys_cwd", "valid:call"),
    _c("unknown-keys-confidence", "6-unknown-keys",
       '{"action":"call","tool":"system.time","args":{},"confidence":0.9,"notes":"x"}', "valid:call"),
    # 7. missing action
    _c("missing-action-tool-only", "7-missing-action", '{"tool":"system.time","args":{}}', "invalid:missing_action"),
    _c("missing-action-empty-string", "7-missing-action", '{"action":"","tool":"system.time"}', "invalid:missing_action"),
    _c("missing-action-null", "7-missing-action", '{"action":null,"tool":"system.time"}', "invalid:missing_action"),
    _c("missing-action-foo-bar", "7-missing-action", '{"foo":"bar"}', "invalid:missing_action"),
    # 8. missing args
    _c("missing-args-optional-ok", "8-missing-args", '{"action":"call","tool":"system.time"}', "valid:call"),
    _c("missing-args-required", "8-missing-args", '{"action":"call","tool":"files.read"}', "invalid:invalid_args"),
    _c("missing-args-null-required", "8-missing-args", '{"action":"call","tool":"files.read","args":{"path":null}}', "invalid:invalid_args"),
    # 9. wrong args type
    _c("args-list", "9-wrong-args-type", '{"action":"call","tool":"files.read","args":["a.txt"]}', "invalid:invalid_args"),
    _c("args-plain-string", "9-wrong-args-type", '{"action":"call","tool":"files.read","args":"a.txt"}', "invalid:invalid_args"),
    _c("args-number", "9-wrong-args-type", '{"action":"call","tool":"system.time","args":5}', "invalid:invalid_args"),
    _c("args-bool", "9-wrong-args-type", '{"action":"call","tool":"system.time","args":true}', "invalid:invalid_args"),
    _c("args-unknown-name", "9-wrong-args-type", '{"action":"call","tool":"system.time","args":{"foo":"bar"}}', "invalid:invalid_args"),
    _c("args-wrong-scalar-type", "9-wrong-args-type",
       '{"action":"call","tool":"files.read","args":{"path":"a.txt","max_chars":"lots"}}', "invalid:invalid_args"),
    _c("args-dict-for-str", "9-wrong-args-type",
       '{"action":"call","tool":"files.read","args":{"path":{"p":"a.txt"}}}', "invalid:invalid_args"),
    # 10. unknown action
    _c("unknown-action-verb", "10-unknown-action", '{"action":"open_vscode"}', "invalid:unknown_action"),
    _c("unknown-action-search", "10-unknown-action", '{"action":"search","query":"cats"}', "invalid:unknown_action"),
    _c("unknown-action-ask-not-allowed", "10-unknown-action", '{"action":"ask","question":"Which?"}', "invalid:unknown_action"),
    # 11. wrong-shaped but parseable JSON
    _c("shape-empty-object", "11-wrong-shape", "{}", "invalid:missing_action"),
    _c("shape-answer-object", "11-wrong-shape", '{"answer":"The time is 3pm"}', "invalid:missing_action"),
    _c("shape-list-of-strings", "11-wrong-shape", '["a","b"]', "invalid:unexpected_shape"),
    _c("shape-action-not-string", "11-wrong-shape", '{"action":["call"],"tool":"system.time"}', "invalid:unexpected_shape"),
    _c("shape-two-unrelated-objects", "11-wrong-shape", '{"a":1} {"b":2}', "invalid:unexpected_shape"),
    _o("shape-observed-action-is-tool-name", "11-wrong-shape", "obs_action_is_tool_name_no_args",
       "recovered:action_was_tool_name"),
    _o("shape-observed-action-is-tool-name-args", "11-wrong-shape", "obs_action_is_tool_name_with_args",
       "recovered:action_was_tool_name"),
    _o("shape-observed-action-is-tool-name-discovery", "11-wrong-shape", "obs_action_is_tool_name_screen",
       "recovered:action_was_tool_name", True),
    # 12. truncated JSON
    _c("truncated-mid-args", "12-truncated", '{"action":"call","tool":"system.time","args":{', "invalid:malformed_json"),
    _c("truncated-mid-string", "12-truncated", '{"action":"call","tool":"files.read","args":{"path":"C:\\\\Us', "invalid:malformed_json"),
    _c("truncated-observed-cut", "12-truncated", _OBSERVED["obs_valid_call_screen_observe"][:60], "invalid:malformed_json"),
    _c("bad-syntax-trailing-comma", "12-truncated", '{"action":"call","tool":"system.time",}', "invalid:malformed_json"),
    _c("bad-syntax-single-quotes", "12-truncated", "{'action': 'call', 'tool': 'system.time'}", "invalid:malformed_json"),
    # 13. multiple JSON objects
    _o("multi-observed-decision-plus-fragment", "13-multiple-objects", "obs_two_objects_trailing_subgoal_fragment", "valid:call"),
    _c("multi-two-different-decisions", "13-multiple-objects",
       '{"action":"call","tool":"system.time"}\n{"action":"call","tool":"system.battery"}', "invalid:unexpected_shape"),
    _c("multi-identical-duplicates", "13-multiple-objects", CALL_TIME + "\n" + CALL_TIME, "valid:call"),
    # 14. empty output
    _c("empty-string", "14-empty", "", "invalid:empty_output"),
    _c("empty-whitespace", "14-empty", "  \n\t  ", "invalid:empty_output"),
    _c("empty-none", "14-empty", None, "invalid:empty_output"),
    # 15. plain English answer
    _c("english-open-vscode", "15-plain-english", "I think you should open VS Code.", "invalid:malformed_json"),
    _c("english-sure", "15-plain-english", "Sure! I'll check the time for you right away.", "invalid:malformed_json"),
    _c("english-answer-not-action", "15-plain-english", "The current time is 3:30 PM.", "invalid:malformed_json"),
    _o("english-observed-repeat-guard-lecture", "15-plain-english", "obs_prose_reply_repeat_guard", "invalid:malformed_json"),
    _o("english-observed-subgoal-walkthrough", "15-plain-english", "obs_prose_reply_subgoal_walkthrough", "invalid:malformed_json"),
    # 16. model saying "done"
    _o("done-observed", "16-done", "obs_done_with_summary", "valid:done"),
    _c("done-bare-word", "16-done", "done", "invalid:malformed_json"),
    _c("done-sentence", "16-done", "Done. The goal has been completed.", "invalid:malformed_json"),
    _c("done-no-summary", "16-done", '{"action":"done"}', "valid:done"),
    # 17. invalid action value
    _c("invalid-action-malformed-literal", "17-invalid-action", '{"action":"malformed"}', "invalid:unknown_action"),
    _c("invalid-action-call-tool", "17-invalid-action", '{"action":"call_tool","tool":"system.time"}', "invalid:unknown_action"),
    _c("invalid-action-conflicting-tool", "17-invalid-action", '{"action":"system.time","tool":"system.battery"}', "invalid:unknown_action"),
    _c("invalid-action-registered-but-not-offered", "17-invalid-action", '{"action":"apps.close","args":{"app":"x"}}',
       "invalid:unknown_action"),
    _o("invalid-action-observed-flattened-args-l2-tool", "17-invalid-action", "obs_flattened_args_l2_tool",
       "invalid:unknown_action"),
    _c("flattened-args-action-is-tool", "17-invalid-action", '{"action":"ui.read","app":"Chrome","limit":4000}',
       "invalid:invalid_args"),
    _c("flattened-args-action-call", "17-invalid-action", '{"action":"call","tool":"files.read","path":"a.txt"}',
       "invalid:invalid_args"),
    _c("action-is-tool-with-noise-key", "18-recoverable",
       '{"action":"system.time","reason":"r","confidence":1}', "recovered:action_was_tool_name"),
    # 18. unexpected but recoverable structure
    _c("recover-list-wrapped", "18-recoverable", '[{"action":"done","summary":"ok"}]', "normalized:done"),
    _c("recover-single-key-wrapper", "18-recoverable", '{"decision":{"action":"done","summary":"ok"}}', "normalized:done"),
    _c("recover-response-wrapper-call", "18-recoverable",
       '{"response":{"action":"call","tool":"system.time","args":{}}}', "normalized:call"),
    _o("recover-observed-extra-closing-brace", "18-recoverable", "obs_extra_closing_brace_trailing_fields", "normalized:call"),
    _c("recover-args-json-string", "18-recoverable",
       '{"action":"call","tool":"files.read","args":"{\\"path\\": \\"a.txt\\"}"}', "recovered:args_json_string"),
    _c("recover-numeric-string-int-arg", "18-recoverable",
       '{"action":"call","tool":"files.read","args":{"path":"a.txt","max_chars":"500"}}', "valid:call"),
    _c("recover-null-optional-arg", "18-recoverable",
       '{"action":"call","tool":"files.read","args":{"path":"a.txt","max_chars":null}}', "valid:call"),
    # x. unknown / missing tool (the tool-protection cases)
    _o("tool-observed-hallucinated", "x-tool", "obs_unknown_tool_hallucinated", "invalid:unknown_tool"),
    _o("tool-observed-typo", "x-tool", "obs_unknown_tool_typo", "invalid:unknown_tool"),
    _c("tool-does-not-exist", "x-tool", '{"action":"call","tool":"something_that_does_not_exist"}', "invalid:unknown_tool"),
    _c("tool-missing", "x-tool", '{"action":"call","args":{}}', "invalid:missing_tool"),
    _c("tool-empty", "x-tool", '{"action":"call","tool":"","args":{}}', "invalid:missing_tool"),
    _c("tool-not-string", "x-tool", '{"action":"call","tool":{"name":"system.time"}}', "invalid:missing_tool"),
    _c("tool-semantically-incomplete", "x-tool", '{"action":"call","tool":"browser.open","args":{}}', "valid:call"),
]
# ^ the last entry is asserted separately in the smoke script: `browser.open` is a real, registered L1 tool that is
#   NOT offered to the (L0-only) catalog the corpus runs under, so it is a POLICY case (valid decision, refused later
#   by the orchestrator's tool_not_allowed) — not a format failure. See smoke_llm_decision_parser.py section 5.

OBSERVED_COUNT = sum(1 for c in CASES if c.source == "observed")
CONSTRUCTED_COUNT = sum(1 for c in CASES if c.source == "constructed")
