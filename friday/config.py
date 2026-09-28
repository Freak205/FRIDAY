"""Typed configuration loaded from config.yaml."""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from friday import paths

Policy = Literal["auto", "confirm", "deny"]
Tier = Literal["L0", "L1", "L2", "L3"]


class DaemonConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class IdentityConfig(BaseModel):
    name: str = "FRIDAY"
    wake_word: str = "hey friday"
    user_name: str = ""


class BrainConfig(BaseModel):
    match_threshold: float = 0.62
    clarify_threshold: float = 0.45
    embedding_model: str = "BAAI/bge-small-en-v1.5"


class PermissionsConfig(BaseModel):
    tiers: dict[Tier, Policy] = Field(
        default_factory=lambda: {"L0": "auto", "L1": "auto", "L2": "confirm", "L3": "confirm"}
    )
    unattended_ceiling: Tier = "L1"
    overrides: dict[str, Policy] = Field(default_factory=dict)


class KnowledgeConfig(BaseModel):
    # Cosine similarity a document chunk needs to count as relevant.
    match_threshold: float = 0.4
    # Characters per chunk when splitting a document for embedding.
    chunk_size: int = 1000
    chunk_overlap: int = 150
    # Safety cap when indexing a whole folder.
    max_files: int = 500


class OcrConfig(BaseModel):
    # Full path to tesseract.exe; blank = autodetect via PATH / common Windows install dirs.
    tesseract_cmd: str = ""
    # Words below this OCR confidence (0-100) are dropped from the result.
    min_confidence: int = 0


class BrowserConfig(BaseModel):
    # Chromium engine either way. Blank = Playwright's bundled Chromium
    # (`playwright install chromium`). "chrome" = launch the real, installed
    # Google Chrome binary via Playwright's channel support when present,
    # falling back to bundled Chromium automatically if it isn't — see
    # friday/browser.py's ensure_open(). Either way this uses FRIDAY's own
    # profile_dir below, never your normal signed-in Chrome profile.
    channel: str = "chrome"
    headless: bool = False
    # Persistent profile directory so logins (WhatsApp Web, ChatGPT, ...) survive
    # a restart. Never read by FRIDAY itself — see friday/browser.py for why.
    profile_dir: str = "data/browser_profile"
    nav_timeout_ms: int = 20000
    action_timeout_ms: int = 10000


class DesktopObserverConfig(BaseModel):
    """Phase 9: read-only desktop situational awareness — see friday/desktop_observer.py.

    Purely observational; the skill/module this configures never moves the
    mouse, sends input, or performs any action, and never sends a screenshot
    or screen text anywhere off this machine.
    """

    # Master switch for `screen.observe` and the lightweight ambient context
    # `plan.run` attaches to its planner prompt (see friday/skills/plan.py).
    enabled: bool = True
    # Screenshots are saved to data/cache like screen.capture; off by default
    # since most callers only need the deterministic/OCR fields below.
    include_screenshot: bool = False
    # Requires Tesseract (see friday.ocr) — falls back to an empty, clearly
    # marked result rather than failing when it isn't installed.
    include_ocr: bool = True
    # Caps how much OCR text can flow into a single observation, so a dense
    # screen never turns into an oversized planner prompt.
    max_ocr_chars: int = 1000
    # Caps how many open windows are listed per observation.
    max_windows: int = 20


class LlmConfig(BaseModel):
    # Only provider implemented so far. Kept as a string (not a Literal) so an
    # unrecognised value fails with a clear runtime message, not a config crash.
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    # Deliberately no default model — do not assume one is pulled. Blank means
    # "use whatever CFG.llm.model the caller supplies", and the provider still
    # reports an actionable error if that model isn't installed.
    model: str = ""
    temperature: float = 0.3
    timeout_s: float = 60.0
    # Phase 21.0: the context window requested from the provider (Ollama
    # `options.num_ctx`), tokens. One central value for every caller: Ollama
    # reloads the model when a request asks for a different window. 0 = send
    # nothing and take the provider's own default. 6144 was MEASURED, not guessed
    # (PLAN.md Phase 21.0 §4, qwen2.5:3b on the 4 GB RTX 2050): Ollama's default 4096
    # window silently truncated 2 of 6 production-shaped planner prompts — e.g. a
    # 4337-token prompt was cut to 2050 tokens, under half of it — while 6144
    # truncated none (worst realistic prompt 4935 tokens), generation stays ~21 ms/token
    # at every size, and the extra VRAM is ~74 MiB (2229 of 4096 MiB in use). 8192
    # bought nothing further for the measured prompts.
    num_ctx: int = 6144


class PlannerConfig(BaseModel):
    # Master switch for `plan.run` — the LLM-driven multi-step bridge.
    enabled: bool = True
    # Phase 16.0: raised 8 -> 16. Proven too low by `scripts/smoke_long_horizon.py`
    # scenario D — a genuine, entirely-successful 12-step real task (well
    # within the brief's own "8-12 step" long-horizon range) hit `step_limit`
    # at step 8 with zero room for even one forgiven failure. 16 keeps a
    # real hard ceiling (still bounded, still no unattended-forever loop)
    # while giving a 12-step task headroom for a couple of replans too.
    max_steps: int = 16
    # Above Session._confirm's 60s wait, so a step needing human confirmation
    # doesn't get timed out by the orchestrator before the person can answer.
    step_timeout_s: float = 90.0
    # Hard ceiling on the whole run, independent of max_steps * step_timeout_s.
    total_timeout_s: float = 300.0
    # Blank = fall through to CFG.llm.model. Provider is already global
    # (CFG.llm.provider) — no separate planner-level provider setting.
    model: str = ""
    # Phase 10: how many times a failed step is forgiven and replanned (the
    # loop continues so the model can see the failure and try something else)
    # instead of stopping the plan outright — see friday/orchestrator.py's
    # `run_goal` and friday/intelligence/evaluator.py. Bounded by this count
    # regardless; still capped by max_steps either way. A permission denial
    # is never replanned, regardless of this setting — see Orchestrator.run_goal.
    # Phase 16.0: raised 0 -> 2 (was "off" since Phase 10). Proven too
    # restrictive by `scripts/smoke_long_horizon.py` scenario I — a
    # realistic task with one ordinary transient step failure and two prior
    # real successes failed the WHOLE plan outright under the old default,
    # discarding real progress for a failure that was trivially recoverable.
    # Still bounded (never more than 2 forgiven failures per goal) and still
    # never applies to a permission denial or declined confirmation.
    max_replans: int = 2
    # Phase 11.2: upper bound on how many bounded subgoals
    # `Orchestrator.decompose_goal` may break a multi-step goal into before
    # adaptive per-action execution takes over. Not every goal decomposes at
    # all (see friday.intelligence.goals.looks_decomposable) — this only
    # caps the ones that do.
    max_subgoals: int = 8
    # Phase 17.0: bounds on the read-only discovery pre-phase `plan.run` runs
    # for an open-ended/diagnostic/investigative goal before deciding whether
    # any mutating action is warranted — see friday.intelligence.discovery
    # and friday.skills.plan._maybe_discover. Deliberately smaller than
    # max_steps/total_timeout_s: investigation must stay cheap and bounded,
    # never grow into a second unbounded planning loop.
    max_discovery_steps: int = 6
    max_discovery_time_s: float = 90.0
    max_evidence_items: int = 8
    # Phase 19.0: how many times an INVALID planner decision (malformed JSON,
    # wrong shape, unknown tool, invalid args, unsupported "done" — see
    # friday.decision) may be sent back to the model with a compact repair
    # prompt before the step stops truthfully. Bounded hard at 2; 0 turns
    # repair off (an invalid decision then stops immediately). Never applies
    # to a permission denial, a declined confirmation, a tool failure, or a
    # cancellation — only to the model's own format/contract failures.
    decision_repair_attempts: int = 1
    # Phase 19.0: ask Ollama to constrain the planner's reply to the decision
    # JSON Schema (friday.decision.decision_json_schema: action enum, tool
    # enum = the tools actually offered, args an object). ON because it was
    # MEASURED, not assumed — qwen2.5:3b via Ollama 0.34.2, 90 paired replies on
    # the production prompts: 100% valid vs 81-97% plain, 0 hallucinated tools,
    # median latency 1687 vs 1837 ms; end-to-end through a real daemon: 100%
    # decision-valid and 0 planning_failed runs vs 96.9% / 3 with the parser +
    # repair layer alone (PLAN.md Phase 19.0). The deterministic validator and
    # the bounded repair stay in force either way — constrained decoding fixes
    # syntax and tool names, not argument names or "done" honesty. A server that
    # rejects the schema is retried once without it (friday.llm.OllamaProvider).
    # Set false to send plain prompts.
    structured_output: bool = True
    # Phase 20.0: intent -> action alignment (friday/intent.py). The planner's
    # chosen tool must fit the ACTION CLASS the user's own goal authorizes
    # (a read-only goal cannot click/type/write/delete/send), checked after
    # tool/arg validation and BEFORE permission/confirmation — never instead
    # of them. `intent_guard` is the master switch (off only to measure the
    # pre-Phase-20 behaviour); `intent_prefilter` additionally hides tools whose
    # every possible action class is out of scope from the planner's prompt and
    # JSON-Schema tool enum, so a small model is rarely even tempted (the guard
    # stays authoritative either way); `max_intent_rejections` bounds how many
    # mismatched decisions one run may burn before it stops truthfully.
    intent_guard: bool = True
    intent_prefilter: bool = True
    max_intent_rejections: int = 3
    # Phase 21.0: goal coverage. A multi-clause goal ("check the time and battery")
    # is split into its own conjuncts (friday.intelligence.discovery.derive_clauses)
    # and a planner `done` — or an evidence-driven stop — is only trusted once each
    # clause has real evidence. `goal_coverage` is the master switch (off only to
    # measure the pre-Phase-21 behaviour); `max_coverage_nudges` bounds how many
    # times one run may send a premature `done` back to the planner (then the
    # planner is believed: coverage is keyword-based and must never trap a run).
    goal_coverage: bool = True
    max_coverage_nudges: int = 1
    # Phase 21.0: an explicit "read more" / "next page" may advance a page cursor
    # instead of being blocked as an identical read (friday.intent.is_continuation_request).
    continuation_reads: bool = True
    # Phase 21.0: a user's own explicit follow-up ("yes, fix it") may widen the
    # PREVIOUS report-only goal's scope, same Goal.id, through the normal
    # permission/confirmation pipeline (friday.intent.derive_expansion).
    scope_expansion: bool = True
    # Phase 21.0: planner prompt budget. Before each planner call the prompt size is
    # estimated and, if it would not fit in `llm.num_ctx` (less a reserve for the reply),
    # shrunk lowest-value-first (ambient context, then old history, then tool
    # descriptions). `chars_per_token` is a deliberately conservative estimate
    # calibrated against Ollama's own `prompt_eval_count`: production-shaped planner
    # prompts measured 3.95-3.99 chars/token (PLAN.md Phase 21.0 §4), so 3.5 leaves
    # ~12% margin for denser text (paths, JSON, code) in history.
    prompt_budget: bool = True
    prompt_budget_fraction: float = 0.85
    chars_per_token: float = 3.5
    # Phase 22.0: post-condition verification. After a state-changing tool call
    # reports success, friday.verify reads the real state back (file exists, volume
    # level, window gone...) and the step is VERIFIED / FAILED / UNVERIFIED on that
    # evidence — never on the tool's own claim or the planner's `done`.
    # `verify_timeout_s` bounds one read-back (including its settle polling).
    postcondition_verify: bool = True
    verify_timeout_s: float = 4.0
    # Phase 22.0: the planner sees a bounded, sanitized excerpt of a step's real tool
    # data (friday.toolview), not just its one-line speech. Per-step and whole-prompt
    # caps, in characters; 0 disables the excerpt (the pre-Phase-22 prompt).
    tool_data_excerpts: bool = True
    tool_data_step_chars: int = 500
    tool_data_total_chars: int = 1500
    # Phase 22.0: the repeat guard compares calls by what they DO, not how they are
    # spelled (path/url normalisation, size caps on a paged read). Off = the
    # Phase 20/21 exact-argument identity.
    semantic_repeat_guard: bool = True
    # Phase 22.0: a bare "yes" / "yes, fix it" with nothing pending and no previous
    # finding to act on is answered with a clarifying question instead of being
    # matched lexically to some skill (meta.undo, whatsapp.send...).
    confirmation_guard: bool = True
    # Phase 23.0: evidence-grounded goal completion (friday.intelligence.discovery /
    # Orchestrator._answer_from_evidence). The Phase 11.2 subgoal breakdown now
    # distinguishes ACQUISITION subgoals (need their own new evidence) from ANSWER ones
    # (explain something from evidence a prior subgoal already gathered) —
    # `subgoal_evidence_advance` lets the subgoal pointer move forward on real, tagged
    # evidence alone (`PlanStep.subgoal`), so a small model that never emits
    # `subgoal_index` doesn't stay fixated on an already-satisfied subgoal (PLAN.md
    # Phase 22.0 report §10: "the 3B model fixates on subgoal 0 and re-reads instead of
    # answering from what it already has"). `answer_from_evidence` is the master switch
    # for then stopping tool execution once the current subgoal is ANSWER-kind and
    # answerable from real, non-contradictory evidence already gathered under the
    # subgoals before it, and composing the final answer with one bounded, JSON-schema-
    # free (so it is structurally incapable of choosing a tool) LLM call
    # (`Orchestrator._answer_from_evidence`) instead of asking the planner to choose
    # another tool. Both are additive and only ever apply when `run_goal` was given a
    # `subgoals` breakdown — every other caller (including the existing goal-coverage
    # evidence stop, unchanged) is untouched.
    subgoal_evidence_advance: bool = True
    answer_from_evidence: bool = True
    # Phase 24.1, broadened Phase 24.2: a deterministic, lightweight guard on the TEXT
    # `Orchestrator._answer_from_evidence` composes — never another model call, never a
    # retry loop. `guard_against_overclaiming` (Phase 17.0) already softens confident
    # cause/fix wording; this instead checks the answer for CONCRETE claims (a specific
    # number, a filename, a completed-action verb like "deleted"/"sent", or — Phase
    # 24.2 — a created/changed/opened/updated resource or a found/retrieved/searched
    # result) that the supplied evidence never actually produced, and — only when
    # evidence is missing entirely or such a claim is found unsupported — replaces the
    # answer with a plain evidence-only statement that says so, instead of letting the
    # claim through. Master switch (off = the Phase 23.0 behavior verbatim: whatever the
    # composer said, used as-is after `guard_against_overclaiming` alone). See
    # friday.intelligence.discovery.ground_answer. Phase 24.5: the same switch also
    # governs the planner's own `done` summary (`orchestrator._ground_done_summary`) --
    # the other route a final answer takes to the user -- for any run that observed
    # something; a run that observed nothing (a plain conversational reply) is untouched.
    answer_grounding_guard: bool = True


class IntelligenceConfig(BaseModel):
    """Phase 10: the bounded state/memory/experience layer — see friday/intelligence/.

    Every field here is a bound, not a feature switch: the intelligence layer
    must never accumulate unbounded conversation/tool history or dump an
    oversized context into a planner prompt (see friday/intelligence/state.py
    and friday/intelligence/working_memory.py).
    """

    # IntelligenceState: how many recent actions/results/failures/turns it
    # keeps before the oldest are dropped (a deque, not a list — see state.py).
    max_recent_actions: int = 20
    max_recent_results: int = 20
    max_failures: int = 10
    max_conversation_turns: int = 10
    # Working memory: bounded context handed to the planner alongside the
    # desktop-observer summary (see friday/skills/plan.py).
    working_memory_max_chars: int = 400
    working_memory_max_facts: int = 3
    # Experience retrieval: how many past episodes friday.intelligence.episodes
    # surfaces as guidance, and the minimum cosine similarity to count.
    episode_retrieval_k: int = 3
    episode_retrieval_threshold: float = 0.5
    # Phase 11.1: turning retrieved episodes into planner context — see
    # friday/intelligence/experience.py. Its own switch, separate from
    # desktop_observer.enabled, since past-goal experience is unrelated to
    # live screen observation and a user may want one without the other.
    experience_enabled: bool = True
    # Total episodes (successes + failures combined) surfaced per plan.run
    # call — deliberately small; this is evidence for the planner, not a
    # dump of everything similar that ever happened.
    experience_max_episodes: int = 3
    # Hard ceiling on the formatted experience block's length, matching the
    # [:max_chars] truncation pattern WorkingMemory.as_context already uses.
    experience_context_max_chars: int = 4000

    # Phase 11.4: bounded contextual memory + reference resolution — see
    # friday/intelligence/context_memory.py and context_resolver.py. Own
    # switch, same precedent as experience_enabled above: context memory is
    # about conversational continuity (files/apps/contacts just touched),
    # unrelated to live screen observation, so a user can have one without
    # the other.
    context_memory_enabled: bool = True
    # Deque bound on ContextMemory — a recent entity naturally falls out of
    # the window once this many newer ones have been remembered since. Not a
    # feature switch: this number must never be unbounded (see brief §3).
    context_max_items: int = 20
    # Minimum confidence to auto-resolve an ordinary reference ("read it")
    # without asking. A stricter bar applies to a call that friday.risk
    # judges consequential (send/delete/pay/...) — see
    # context_consequential_confidence_threshold.
    context_confidence_threshold: float = 0.6
    context_consequential_confidence_threshold: float = 0.85
    # "my usual browser" / "my project" style personalization only ever
    # resolves against an explicit stored preference (friday.memory, kind=
    # "preference") — never inferred from repeated use. This is the minimum
    # similarity for that stored preference to count as a match.
    personalization_confidence_threshold: float = 0.6
    # Bounded planner-context block plan.run attaches, same [:max_chars]
    # truncation pattern as working_memory_max_chars/experience_context_max_chars.
    context_block_max_chars: int = 400

    # Phase 11.5: proactive situational intelligence — see
    # friday/intelligence/proactive.py. A pure notification/suggestion layer
    # on top of the state above (INTEL/goals/CONTEXT/SELF_STATE); disabling
    # it never changes reactive (asked-for) behavior at all.
    proactive_enabled: bool = True
    # Minimum seconds before the same (event_type, entity, goal) fingerprint
    # is allowed to notify again — the core anti-spam guard (brief §7).
    proactive_cooldown_seconds: int = 300
    # Hard ceiling on proactive notifications within proactive_window_minutes,
    # regardless of how many distinct relevant events fire in that window.
    proactive_max_notifications: int = 3
    proactive_window_minutes: int = 15


class SttConfig(BaseModel):
    # faster-whisper model size. Phase 7 optimization benchmarked
    # tiny/base/small on this machine with a TTS-synthesized test set (see
    # PLAN.md Phase 7P — real-mic validation is still a human-run step,
    # scripts/voice_mic_latency_test.py): "small" was both ~3x slower than
    # "base" AND less accurate on that set (misheard "Chrome" as "Kroom"
    # twice), so it's no longer the default. "tiny" tied "base" on accuracy
    # and was ~2x faster still — it's a live option worth trying — but "base"
    # is the default here as the safer pick for real-world noise/accent
    # robustness, which the synthetic benchmark can't measure.
    model: str = "base"
    language: str = "en"
    # "cpu" is the safe default — verified working on this machine. "cuda"/
    # "auto" need the CUDA Toolkit's cuBLAS/cuDNN installed system-wide (not
    # just a GPU present); SttEngine falls back to CPU automatically if a
    # non-CPU attempt fails.
    device: str = "cpu"
    compute_type: str = "int8"
    # Whisper beam search width. Benchmarking measured only a ~5-10% latency
    # difference between greedy (1) and the library's default of 5 on this
    # CPU for short commands (the encoder dominates, not beam search), with
    # no measurable accuracy difference — see PLAN.md Phase 7P. Left at 1
    # since it's never worse here, but don't expect it alone to fix latency.
    beam_size: int = 1
    # Phase 10.X.3: faster-whisper's own internal (Silero) VAD pass over the
    # segment before decoding, on top of the energy-based VadSession that
    # already trims capture.py's recording. Off by default in the original
    # Phase 7 benchmark; candidate for reducing hallucinated trailing words
    # on the silence tail VadSession's silence_timeout_s leaves in — see
    # scripts/benchmark_stt.py for the measurement before flipping this.
    vad_filter: bool = False
    # Optional short domain-vocabulary hint passed as Whisper's
    # `initial_prompt` (e.g. "FRIDAY, Jarvis, VS Code, Chrome, WhatsApp,
    # terminal, browser, project"). Blank = no prompt. Only set this if
    # scripts/benchmark_stt.py actually shows it helps — an initial prompt
    # can just as easily bias the model into hallucinating the hinted words
    # instead of transcribing what was actually said.
    initial_prompt: str = ""
    # Whether a multi-segment result conditions each segment's decoding on
    # the text already decoded for the previous segment. Only matters for
    # audio long enough to produce multiple segments; short voice commands
    # are normally one segment. Faster-whisper's own default is True; kept
    # explicit here since long silence-padded audio conditioning on a
    # half-formed previous segment is a known source of repetition loops.
    condition_on_previous_text: bool = True
    # Phase 10.X.5: known FRIDAY entity names passed as faster-whisper's
    # `hotwords` decode option (friday/voice/vocabulary.py builds the actual
    # string) — a decode-time bias toward these words being *recognized*
    # correctly, never a post-hoc text replacement, so it can't put a word in
    # the transcript the user didn't say (see that module's docstring for why
    # this is safe where a generic spell-corrector would not be). Empty list
    # = no hint, same "blank = off" convention as initial_prompt above.
    # Defaulted ON with friday.voice.vocabulary.DEFAULT_VOCABULARY: measured
    # with `scripts/benchmark_stt.py --vocabulary default` against the 12
    # valid (non-contaminated) real-mic samples from data/voice_test_samples —
    # base model: avg WER 0.208 -> 0.167, every sample that was already
    # correct stayed correct, the one regression-free change fixed the
    # repeatedly-reported "open VS Code" -> "open me a score" mishearing.
    # Still just 12 samples from one sitting — re-check with
    # scripts/benchmark_stt.py once a fresh, larger sample set exists, and
    # set this back to [] if a bigger benchmark shows it isn't holding up.
    vocabulary: list[str] = Field(
        default_factory=lambda: [
            "FRIDAY", "VS Code", "WhatsApp", "Chrome", "Notepad",
            "Windows", "GitHub", "Python", "Ollama", "ChatGPT",
        ]
    )
    # How long a silence has to run after speech before an utterance ends.
    # Short enough to feel responsive, long enough to tolerate a natural
    # mid-sentence breath (see PLAN.md Phase 7P for the measurements behind
    # this default).
    silence_timeout_s: float = 0.8
    # Hard cap so a stuck/noisy mic can't listen forever.
    max_recording_s: float = 15.0
    # RMS energy above this (float32 samples, roughly 0-1) counts as speech.
    silence_rms_threshold: float = 0.012
    # Utterances shorter than this (total speech, not counting the trailing
    # silence that ended them) are treated as noise/false triggers rather
    # than sent to STT — a cough or a chair creak, not a command.
    min_speech_s: float = 0.25
    # How much audio immediately before VAD detects speech is kept and
    # prepended to the recording — covers the ramp-up of a soft-onset word
    # (and, since the microphone stream stays open between activations, the
    # moment right after the hotkey is pressed) so the first word isn't
    # clipped.
    pre_roll_ms: float = 300.0
    # Phase 10.X.4: use the bundled Silero VAD model (already downloaded for
    # openWakeWord's own gating, see wakeword.py) to decide is_speech instead
    # of the raw `silence_rms_threshold` compare. A 20-utterance real-mic test
    # showed 13/20 captures running all the way to `max_recording_s` because
    # this room's noise floor sits close enough to the RMS threshold that
    # background noise alone kept resetting the silence timer — see
    # friday/voice/vad_model.py for the measurements. Falls back to
    # RMS-threshold detection automatically if the model can't be loaded, so
    # this is safe to leave on; set False only to force the old RMS-only
    # behavior (e.g. while debugging).
    vad_enabled: bool = True
    # Minimum Silero VAD speech probability (0-1) to count a block as speech
    # and start an utterance. 0.5 was validated against the recordings from
    # the test above — real speech scored 0.7-0.99, this room's background
    # noise never scored above ~0.45. Only used when `vad_enabled` is True
    # and the model loaded.
    vad_threshold: float = 0.5
    # Once speech has started, is_speech is compared against this LOWER
    # threshold instead of `vad_threshold` — a word's probability trace dips
    # mid-word (a consonant, a brief dip in loudness), and a single shared
    # threshold right at the entry point occasionally drops enough of those
    # dips to undercount `min_speech_s` and wrongly reject a short, real
    # command as noise. 0.35 was the lowest value in the same validation that
    # still kept every capture's stop time in the 2-5s target range.
    vad_sustain_threshold: float = 0.35
    # sounddevice device index; null = system default input.
    input_device: int | None = None


class TtsConfig(BaseModel):
    enabled: bool = True
    # Adapter name — only "sapi" (pyttsx3/Windows SAPI5, friday/voice/tts.py)
    # is implemented. Kept as a string, not a Literal, so a typo or a
    # not-yet-built engine fails with a clear runtime message rather than a
    # config-load crash, and so a future local engine (e.g. Piper) can be
    # added without a config-shape change — see PLAN.md Phase 8I.
    engine: str = "sapi"
    # pyttsx3/SAPI voice id; blank = system default voice. Run
    # `python scripts/list_voices.py` to see installed voices and their ids.
    voice_id: str = ""
    # Words per minute; null = engine default.
    rate: int | None = None
    # 0.0-1.0; null = engine default.
    volume: float | None = None


class WakeWordConfig(BaseModel):
    """Phase 8: always-listening activation. See PLAN.md Phase 8A/8O — there
    is no pretrained "FRIDAY" wake-word model, so `model` defaults to a
    practical placeholder phrase from openWakeWord's pretrained set, not the
    word "FRIDAY". Ctrl+Alt+V (`voice.activation_hotkey`) always works
    regardless of this setting or of wake-word init failing at startup.
    """

    enabled: bool = True
    # Only "openwakeword" is implemented (friday/voice/wakeword.py) — see
    # that file's docstring for why it was chosen. Kept as a string for the
    # same reason as TtsConfig.engine above.
    engine: str = "openwakeword"
    # One of openWakeWord's pretrained models: alexa, hey_jarvis, hey_mycroft,
    # hey_rhasspy, timer, weather. Downloaded once (~6MB total) into
    # `models_dir` on first use — needs internet that one time, then fully
    # local/offline. "hey_jarvis" is the closest fit to an assistant-style
    # call phrase among the pretrained options.
    model: str = "hey_jarvis"
    # Minimum confidence (0-1) to count as a wake detection. Higher = fewer
    # false activations but easier to miss a real one; see PLAN.md Phase 8J
    # for the CPU-load measurement this doesn't affect (threshold is just a
    # comparison, not extra inference). Phase 10.X.2: raised from openWakeWord's
    # own 0.5 baseline to 0.6 because strict wake-word gating (see
    # VoiceConfig.follow_up_enabled) means a false activation now runs
    # whatever the mic happens to be picking up as a real command instead of
    # just opening an already-gated follow-up window — biasing toward fewer
    # false wakes over catching every marginal "hey jarvis" is the safer
    # trade-off here. Tune per-room/mic in config.yaml if needed.
    threshold: float = 0.6
    # Blank = data/models/openwakeword.
    models_dir: str = ""
    # After a full interaction ends and FRIDAY returns to idle, wait this
    # long — and drop any audio buffered during the interaction — before
    # resuming wake-word scanning. Guards against FRIDAY's own voice tail or
    # residual room echo immediately re-triggering itself.
    cooldown_s: float = 1.5
    # Phase 10.X.6: rolling-window wake decision. Instead of requiring a
    # single 80ms openWakeWord frame to cross `threshold` on its own (the old
    # behavior — brittle for an accented "hey jarvis" whose peak frame score
    # can dip just under threshold for one frame in the middle of the
    # phrase), scores from the last `window_s` seconds are kept and a
    # detection fires once at least `persist_frames` of them have crossed
    # `threshold` — see WakeScoreWindow in friday/voice/wakeword.py. Applies
    # to both the main wake-word scan and the barge-in speaking-watcher.
    window_s: float = 1.0
    # How many frames within the window must cross `threshold` before firing.
    # 1 = old single-frame-trigger behavior. >=2 gives a lower `threshold` a
    # cheap guard against a single random noise spike, since real speech
    # spans many more than one 80ms frame while noise usually doesn't.
    persist_frames: int = 2
    # Verbose per-frame diagnostics (score/peak/rms/frame count/time since
    # last detection) at INFO level instead of DEBUG — off by default so
    # production logs aren't spammed; turn on to tune threshold/window_s
    # against this specific room/microphone (see scripts/test_wakeword_mic.py).
    debug: bool = False
    # Phase 10.X.7: optional voice-specific verifier layered on top of the
    # pretrained model (see friday/voice/wakeword.py's WakeWordDetector and
    # scripts/train_wake_verifier.py). Blank (the default) disables it
    # entirely — pretrained-model-only behavior is unchanged. Trains in
    # minutes on CPU from ~20-40 of your own "Hey Jarvis" recordings plus
    # some non-wake speech (scripts/record_wake_samples.py); see PLAN.md
    # Phase 10.X.7 for why this, not a full retrain, is the recommended path
    # if the pretrained model's raw scores turn out to be the real problem.
    verifier_model_path: str = ""
    # Base-model score needed before the (cheap) verifier even runs — keep
    # this low/permissive (openWakeWord's own default is 0.1); the verifier,
    # not this number, makes the final accept/reject call.
    verifier_threshold: float = 0.1


class VoiceConfig(BaseModel):
    enabled: bool = True
    activation_hotkey: str = "ctrl+alt+v"
    # Hard backstop so voice responses stay speakable regardless of what a
    # skill's `speech` field contains — see friday/voice/summarize.py. Lowered
    # from 320 in Phase 7P: dense enumerations (e.g. meta.capabilities) were
    # measured taking 30+ seconds for pyttsx3 to speak at the old cap, which
    # reads as FRIDAY being unresponsive even though it heard you instantly.
    max_speech_chars: int = 200
    # Privacy default: never keep raw audio. Opt-in only, for debugging.
    save_recordings: bool = False
    stt: SttConfig = Field(default_factory=SttConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    wakeword: WakeWordConfig = Field(default_factory=WakeWordConfig)
    # Phase 10.X.2: strict wake-word gating is the default. False means every
    # independent command needs its own "Hey Jarvis" (or Ctrl+Alt+V press) —
    # once a cycle finishes speaking, FRIDAY goes straight back to wake-word
    # standby instead of opening a no-wake-word listening window. Set True to
    # restore the old Phase 8 behavior (a short window where a second command
    # needs no wake word); `follow_up_timeout_s` below still controls that
    # window's length when re-enabled. This does not affect the separate
    # confirmation-answer listen (`confirm_listen_timeout_s`), which is not a
    # "follow-up" — answering a pending "...should I go ahead?" is part of
    # the same interaction that's already unlocked.
    follow_up_enabled: bool = False
    # Phase 8: after FRIDAY finishes speaking, how long the microphone stays
    # ready for a follow-up command with no wake word/hotkey needed (only
    # applies when follow_up_enabled is True). Silence for this long returns
    # to idle quietly (no "I didn't catch that").
    follow_up_timeout_s: float = 10.0
    # How long to listen for a spoken yes/no/instruction after FRIDAY speaks
    # an L2/L3 confirmation prompt ("...should I go ahead?"). Independent of
    # follow_up_timeout_s since answering a direct question deserves a bit
    # more patience than a speculative open-ended follow-up.
    confirm_listen_timeout_s: float = 12.0
    # Pause after TTS finishes and before the follow-up window starts
    # listening, so the tail of FRIDAY's own voice/room echo doesn't trip
    # VAD into thinking the user immediately started talking.
    post_tts_cooldown_s: float = 0.4
    # Phase 10.X.3: short activation chime played the instant "Hey Jarvis" is
    # detected (including mid-barge-in) — see friday/voice/sound.py.
    wake_sound_enabled: bool = True
    # Blank = the bundled friday/assets/audio/wake_chime.wav. Point this at
    # your own short (<=1s) WAV to replace it — see scripts/generate_wake_sound.py
    # for how the bundled one was synthesized (no copyrighted audio).
    wake_sound_path: str = ""
    # How long (seconds) the capture that follows the chime ignores as a
    # warmup window so the chime's own tail can't be mistaken by VAD for the
    # start of the command (see VadSession.warmup_ignore_s). Should be at
    # least the chime's duration; a little slack is safer than none.
    wake_sound_warmup_s: float = 0.85


class LoggingConfig(BaseModel):
    level: str = "INFO"


class GuiConfig(BaseModel):
    # Show the main window on launch instead of starting minimized to tray.
    start_minimized: bool = False
    always_on_top: bool = False
    # Poll intervals for the desktop UI's state hub (milliseconds).
    self_state_poll_ms: int = 300
    task_state_poll_ms: int = 750
    telemetry_poll_ms: int = 2000
    desktop_context_poll_ms: int = 4000
    # Desktop-context panel: include OCR text in the periodic observation.
    # Off by default — OCR is the most expensive part of desktop_observer.observe().
    desktop_context_include_ocr: bool = False
    # How long the core widget holds an ERROR look after an isolated failure
    # before reverting to idle, since nothing else clears SelfStatus.FAILED.
    error_hold_s: float = 4.0


class Config(BaseModel):
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    desktop_observer: DesktopObserverConfig = Field(default_factory=DesktopObserverConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    intelligence: IntelligenceConfig = Field(default_factory=IntelligenceConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    gui: GuiConfig = Field(default_factory=GuiConfig)


def load() -> Config:
    """Read config.yaml, falling back to defaults for anything absent."""
    raw: dict[str, Any] = {}
    if paths.CONFIG_PATH.exists():
        raw = yaml.safe_load(paths.CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return Config(**raw)


# Module-level singleton. Import as `from friday.config import CFG`.
CFG = load()
