"""Phase 25 (Workstream 3) — regression coverage for the speech-field redaction fix.

The completion audit found that `friday.toolview` sanitizes a tool's `.data` (Phase 22.0)
but never its `.speech` — and a real skill (`clipboard.read`, `screen.find_text`,
`screen.click_text`) puts raw screen/clipboard content straight into `speech`. That text
used to flow unredacted into the orchestrator's evidence: the planner's own prompt
(`_history_line`), the LLM composer/grounding-guard input, and the deterministic goal
summary — real "planner context" / "model prompt" consumers.

The fix reuses the existing mechanism (`friday.toolview.sanitize`) at the one place every
`Observation` is built (`Orchestrator._run_step`), so every downstream consumer inherits it
for free. This suite pins:

  A  a secret embedded in a tool's SPEECH is redacted on the resulting Observation
  B  ...and is therefore absent from the planner history line and the deterministic
     goal summary built from it
  C  ordinary (non-secret) speech is preserved byte-for-byte — this is not a new
     truncation or rewrite, only credential-shaped substrings are touched
  D  a DIRECT single-step call (Session/EXECUTOR, no orchestrator involved) is
     untouched — "what's in my clipboard" still gets its raw answer, matching the
     brief's "do not modify the user-visible final speech unnecessarily"
  E  sanitize is applied even when the step also fails verification (obs.speech is
     rewritten by `_attach_verification`, but the tool's own original words it quotes
     back were already sanitized)

Real `Orchestrator.run_plan`/`_run_step`, real `EXECUTOR`, real permission gate; the only
fixtures are two L0 test skills. No Ollama, no real side effect.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday import toolview  # noqa: E402
from friday.orchestrator import Orchestrator, PlanStep  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

REGISTRY.discover()

SECRET_SPEECH = "Clipboard has 41 characters: db_password=hunter2, api_key=sk-abcdefghijklmnop1234"
ORDINARY_SPEECH = "notes.txt has 812 characters. Showing the first part."


def _register_fixtures() -> None:
    if REGISTRY.get("test.sdx_secret_speech") is not None:
        return

    @skill(name="test.sdx_secret_speech", tier="L0",
           description="stand-in for clipboard.read/screen.find_text (speech-redaction smoke)")
    def _secret() -> SkillResult:
        return SkillResult(speech=SECRET_SPEECH, data={})

    @skill(name="test.sdx_ordinary_speech", tier="L0", description="ordinary, non-sensitive speech (control)")
    def _ordinary() -> SkillResult:
        return SkillResult(speech=ORDINARY_SPEECH, data={})

    @skill(name="test.sdx_verify_fail", tier="L1", action="modify",
           description="a state-changing call whose speech carries a secret but verification fails")
    def _verify_fail() -> SkillResult:
        return SkillResult(speech="Set volume: token=abcdef0123456789abcd applied.", data={})


async def main_async() -> int:
    t0 = time.perf_counter()
    _register_fixtures()

    scenario("A: a secret in a tool's SPEECH is redacted on the resulting Observation")
    orch = Orchestrator(tools=["test.sdx_secret_speech"], actor="text")
    result = await orch.run_plan("read the clipboard", [PlanStep(tool="test.sdx_secret_speech")])
    obs = result.observations[0]
    check("the observation ran (tool actually executed)", obs.ok, obs.speech)
    check("'hunter2' is redacted from Observation.speech", "hunter2" not in obs.speech, obs.speech)
    check("'sk-abcdefghijklmnop1234' is redacted from Observation.speech", "sk-abcdefghijklmnop1234" not in obs.speech, obs.speech)
    check("the redaction marker is present", "[redacted]" in obs.speech, obs.speech)
    check("sanitize was applied via the real toolview module (no second framework)",
          obs.speech == toolview.sanitize(SECRET_SPEECH))

    scenario("B: the secret is therefore absent from planner history and the goal summary")
    line = Orchestrator._history_line(0, obs)
    check("'hunter2' does not reach the planner's history line", "hunter2" not in line, line)
    check("the API key does not reach the planner's history line", "sk-abcdefghijklmnop1234" not in line, line)

    from friday.orchestrator import _summarize

    summary = _summarize(result.observations)
    check("'hunter2' does not reach the deterministic goal summary", "hunter2" not in summary, summary)
    check("the API key does not reach the deterministic goal summary", "sk-abcdefghijklmnop1234" not in summary, summary)

    scenario("C: ordinary speech is preserved byte-for-byte — only credential-shaped text is touched")
    orch2 = Orchestrator(tools=["test.sdx_ordinary_speech"], actor="text")
    result2 = await orch2.run_plan("read notes", [PlanStep(tool="test.sdx_ordinary_speech")])
    obs2 = result2.observations[0]
    check("non-sensitive speech is byte-for-byte unchanged", obs2.speech == ORDINARY_SPEECH, obs2.speech)

    scenario("D: a DIRECT single-step call (no orchestrator) is untouched")
    direct = await EXECUTOR.run("test.sdx_secret_speech", {}, actor="text")
    check("a direct EXECUTOR.run call still returns the RAW speech (unaffected by the orchestrator-only fix)",
          direct.speech == SECRET_SPEECH, direct.speech)
    check("...i.e. 'what's in my clipboard' still reads back the real answer verbatim",
          "hunter2" in direct.speech)

    scenario("E: a step whose speech is later rewritten by verification still started from sanitized text")
    orch3 = Orchestrator(tools=["test.sdx_verify_fail"], actor="text", verify=False)
    result3 = await orch3.run_plan("do the thing", [PlanStep(tool="test.sdx_verify_fail")])
    obs3 = result3.observations[0]
    check("a token embedded in a state-changing call's speech is also redacted", "abcdef0123456789abcd" not in obs3.speech, obs3.speech)

    return finish("Phase 25 — speech-field redaction", time.perf_counter() - t0, min_assertions=10, min_scenarios=5)


def main() -> int:
    import asyncio

    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
