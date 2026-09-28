"""Real-microphone, human-in-the-loop latency test for Phase 7P — and, since
Phase 10.X.3, the recorder for the real-voice STT accuracy benchmark.

Nothing can substitute for actually speaking into the microphone — this
script cannot be run unattended by an agent, only by a person sitting at
the keyboard. It records the same fine-grained timestamps
scripts/voice_benchmark.py can't (real mic-open timing, real VAD reaction to
a real voice, real STT/TTS latency on real hardware) across at least 10
utterances, and writes a report so "did this actually get better" has
numbers behind it instead of a feeling.

It does NOT run anything through FRIDAY's session/executor (same safety
posture as scripts/smoke_voice.py) — it only exercises mic -> VAD -> STT,
optionally speaking the transcript back so you can also judge TTS.

Phase 10.X.3: pass --save-samples to also save each utterance's raw audio
(WAV, 16kHz mono) plus the transcript YOU confirm is correct into
data/voice_test_samples/ (a manifest.json alongside them) — this is the
"record ~15-20 natural commands in my own accent" step scripts/benchmark_stt.py
needs to compare STT model/decoding choices against your REAL voice instead
of only synthetic TTS audio. Off by default, matching this project's privacy
default (voice.save_recordings: false) — you're opting in to keep these,
specifically to make accuracy improvements measurable.

Usage:
    python scripts/voice_mic_latency_test.py
    python scripts/voice_mic_latency_test.py --utterances 10 --speak-back
    python scripts/voice_mic_latency_test.py --save-samples --utterances 20
    python scripts/voice_mic_latency_test.py --save-samples --fresh --utterances 20

Phase 10.X.5: `--fresh` saves into data/voice_test_samples_v2/ instead of the
original data/voice_test_samples/, so a new recording round can't merge with
(or silently overwrite) an earlier sample set — use it for the "record a
fresh, uncontaminated batch" step scripts/benchmark_stt.py's docstring asks
for. Speak naturally and don't over-enunciate; when asked to type what you
actually said, type the real words (leaving it blank re-uses the suggested
prompt) — typing a placeholder like "=" gets rejected now (see run_one()),
because that's exactly what silently corrupted several samples last round.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import paths  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.voice.capture import SAMPLE_RATE, record_utterance, warm_up_microphone  # noqa: E402
from friday.voice.stt import SttEngine  # noqa: E402
from friday.voice.tts import TtsEngine  # noqa: E402
from friday.voice.types import MicrophoneUnavailable  # noqa: E402

# A realistic mix of short/long, casual/technical, natural Indian-English
# phrasing — matching the examples in the Phase 10.X.3 brief. Feel free to
# say these in your own words rather than reading them verbatim; whatever
# you actually say becomes the recorded sample either way (you confirm/type
# the true transcript afterward when --save-samples is on).
SUGGESTED_PHRASES = [
    "Open Chrome.",
    "Open Notepad.",
    "Open VS Code.",
    "Open my project.",
    "Bro, open VS Code once.",
    "Just open Chrome once.",
    "Close that window.",
    "Actually, no — close it.",
    "Wait, don't do that.",
    "Check what is running.",
    "What's the status?",
    "Send him a message.",
    "Check my exam schedule.",
    "Look at my project and tell me what's wrong.",
    "Go to WhatsApp.",
    "Inspect my FRIDAY project.",
    "What is my upcoming exam schedule?",
    "Open Chrome and search for weather.",
    "Read what's on my screen.",
    "Set a timer for ten minutes.",  # short
    "What's the weather like today, and do I need to bring an umbrella?",  # 15-20 word query
    "Take a screenshot, then open it, and tell me what's on my screen.",  # mid-sentence pause
]


@dataclass
class UtteranceRecord:
    prompt: str
    transcript: str = ""
    first_attempt_ok: bool | None = None  # asked of the human after each utterance
    words_clipped: bool | None = None
    capture_outcome: str = ""
    capture_stop_reason: str = ""
    mic_stream_cold_start_s: float = 0.0
    time_to_first_audio_s: float = 0.0
    time_to_speech_start_s: float = 0.0
    recording_duration_s: float = 0.0
    stt_latency_s: float = 0.0
    total_latency_s: float = 0.0
    error: str = ""


def ask(prompt: str, default: str = "") -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return default


def yes_no(prompt: str) -> bool | None:
    ans = ask(f"{prompt} [y/n] ").lower()
    if ans in ("y", "yes"):
        return True
    if ans in ("n", "no"):
        return False
    return None


def _save_sample(samples_dir: Path, sample_id: str, audio: np.ndarray, expected_text: str) -> None:
    """Writes <sample_id>.wav (16-bit PCM, SAMPLE_RATE) + <sample_id>.txt,
    and appends/updates a manifest.json entry — the format
    scripts/benchmark_stt.py reads.
    """
    samples_dir.mkdir(parents=True, exist_ok=True)
    wav_path = samples_dir / f"{sample_id}.wav"
    pcm16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())
    (samples_dir / f"{sample_id}.txt").write_text(expected_text, encoding="utf-8")

    manifest_path = samples_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"samples": []}
    manifest["samples"] = [s for s in manifest["samples"] if s["id"] != sample_id]
    manifest["samples"].append({"id": sample_id, "wav": f"{sample_id}.wav", "expected_text": expected_text})
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def run_one(
    voice, stt: SttEngine, prompt: str, *,
    save_samples: bool = False, samples_dir: Path | None = None, sample_id: str = "",
) -> UtteranceRecord:
    rec = UtteranceRecord(prompt=prompt)
    print(f"\n>>> Say: \"{prompt}\"")
    print("    (speak naturally, once — 🎤 recording starts now)")
    t_activate = time.perf_counter()
    try:
        capture = record_utterance(
            silence_timeout_s=voice.stt.silence_timeout_s,
            max_duration_s=voice.stt.max_recording_s,
            rms_threshold=voice.stt.silence_rms_threshold,
            min_speech_s=voice.stt.min_speech_s,
            pre_roll_ms=voice.stt.pre_roll_ms,
            vad_enabled=voice.stt.vad_enabled,
            vad_threshold=voice.stt.vad_threshold,
            vad_sustain_threshold=voice.stt.vad_sustain_threshold,
            device=voice.stt.input_device,
        )
    except MicrophoneUnavailable as exc:
        rec.error = str(exc)
        print(f"    FAILED: {exc}")
        return rec

    rec.capture_outcome = capture.outcome
    rec.capture_stop_reason = capture.stop_reason
    rec.mic_stream_cold_start_s = capture.timings.get("stream_ready", t_activate) - t_activate
    if "first_audio" in capture.timings:
        rec.time_to_first_audio_s = capture.timings["first_audio"] - t_activate
    if "speech_start" in capture.timings:
        rec.time_to_speech_start_s = capture.timings["speech_start"] - t_activate
    rec.recording_duration_s = capture.duration_s

    if not capture.has_audio:
        print("    No speech detected.")
        rec.total_latency_s = time.perf_counter() - t_activate
        return rec

    print("    Processing...")
    result = stt.transcribe(capture.audio)
    rec.stt_latency_s = result.latency_s
    rec.transcript = result.text if result.ok else f"<STT error: {result.error}>"
    rec.total_latency_s = time.perf_counter() - t_activate
    print(f"    Heard: \"{rec.transcript}\"")
    print(f"    (mic ready in {rec.mic_stream_cold_start_s:.3f}s, "
          f"recorded {rec.recording_duration_s:.2f}s [{rec.capture_stop_reason}], "
          f"STT {rec.stt_latency_s:.2f}s, total {rec.total_latency_s:.2f}s)")

    rec.first_attempt_ok = yes_no("    Did it get that right on this one attempt?")
    rec.words_clipped = yes_no("    Were any of the first words cut off/missing?")

    if save_samples and samples_dir is not None:
        if rec.first_attempt_ok:
            expected_text = rec.transcript
        else:
            typed = ask("    Type exactly what you actually said (blank = use the prompt above): ")
            # A Phase 10.X.3/10.X.4 benchmark round got several samples
            # contaminated with a literal "=" here (the operator declining
            # the default by typing a punctuation placeholder instead of
            # leaving the line blank) — that string then silently became the
            # WER "ground truth" for that sample. Require at least one letter
            # in a non-blank answer instead of accepting anything typed.
            while typed and not any(c.isalpha() for c in typed):
                print(
                    "    That doesn't look like a transcript (no letters) — "
                    "leave it blank to use the prompt above, or type what you actually said."
                )
                typed = ask("    Type exactly what you actually said (blank = use the prompt above): ")
            expected_text = typed or prompt.rstrip(".")
        _save_sample(samples_dir, sample_id, capture.audio, expected_text)
        print(f"    saved {sample_id}.wav + expected transcript {expected_text!r}")

    return rec


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--utterances", type=int, default=len(SUGGESTED_PHRASES))
    parser.add_argument("--speak-back", action="store_true")
    parser.add_argument(
        "--report", default=str(paths.DATA / "voice_mic_latency_report.json"),
    )
    parser.add_argument(
        "--save-samples", action="store_true",
        help="Also save each utterance's audio + confirmed transcript to --samples-dir "
        "for scripts/benchmark_stt.py to replay against different STT configs.",
    )
    parser.add_argument("--samples-dir", default=None)
    parser.add_argument(
        "--fresh", action="store_true",
        help="Save into data/voice_test_samples_v2/ instead of the original "
        "data/voice_test_samples/ — use this for a new recording round so it "
        "can't merge with (or overwrite) an earlier, possibly contaminated, "
        "sample set. Ignored if --samples-dir is also given.",
    )
    args = parser.parse_args()
    if args.samples_dir is None:
        args.samples_dir = str(paths.DATA / ("voice_test_samples_v2" if args.fresh else "voice_test_samples"))

    voice = CFG.voice
    print(f"\nFRIDAY real-microphone latency test — model={voice.stt.model!r} "
          f"beam_size={voice.stt.beam_size} silence_timeout_s={voice.stt.silence_timeout_s} "
          f"pre_roll_ms={voice.stt.pre_roll_ms} vad_enabled={voice.stt.vad_enabled} "
          f"vad_threshold={voice.stt.vad_threshold}\n")
    print("This does NOT run any FRIDAY command — mic/VAD/STT only (+ optional TTS playback).\n")

    print("Warming up the microphone stream and loading the STT model...")
    warm_up_microphone(device=voice.stt.input_device)
    stt = SttEngine(
        model_size=voice.stt.model, language=voice.stt.language,
        device=voice.stt.device, compute_type=voice.stt.compute_type,
        beam_size=voice.stt.beam_size,
    )
    stt._ensure_model()
    tts = TtsEngine(voice_id=voice.tts.voice_id, rate=voice.tts.rate) if args.speak_back else None
    print("Ready.\n")

    if args.save_samples:
        print(f"Saving audio + confirmed transcripts to {args.samples_dir}\n"
              "(speak normally, in your own accent/phrasing — don't over-enunciate)\n")

    phrases = (SUGGESTED_PHRASES * ((args.utterances // len(SUGGESTED_PHRASES)) + 1))[: args.utterances]
    records: list[UtteranceRecord] = []
    for i, prompt in enumerate(phrases, 1):
        print(f"\n--- Utterance {i}/{len(phrases)} ---")
        ask("Press Enter when ready to speak...")
        rec = run_one(
            voice, stt, prompt,
            save_samples=args.save_samples, samples_dir=Path(args.samples_dir), sample_id=f"{i:02d}",
        )
        records.append(rec)
        if tts is not None and rec.transcript and not rec.error:
            tts.speak(rec.transcript)

    # -- summary ------------------------------------------------------------
    n = len(records)
    n_ok = sum(1 for r in records if r.first_attempt_ok)
    n_clipped = sum(1 for r in records if r.words_clipped)
    n_no_speech = sum(1 for r in records if r.capture_outcome == "no_speech")
    n_max_dur = sum(1 for r in records if r.capture_outcome == "max_duration")
    avg_stt = sum(r.stt_latency_s for r in records if r.stt_latency_s) / max(
        1, sum(1 for r in records if r.stt_latency_s)
    )
    avg_total = sum(r.total_latency_s for r in records) / max(1, n)
    avg_cold_start = sum(r.mic_stream_cold_start_s for r in records) / max(1, n)

    print("\n" + "=" * 70)
    print(f"Utterances: {n}")
    print(f"First-attempt recognition confirmed OK: {n_ok}/{n}")
    print(f"First words reported clipped: {n_clipped}/{n}")
    print(f"no_speech outcomes: {n_no_speech}/{n}  max_duration outcomes: {n_max_dur}/{n}")
    print(f"avg mic-ready latency: {avg_cold_start:.3f}s")
    print(f"avg STT latency: {avg_stt:.2f}s")
    print(f"avg total (activation -> transcript) latency: {avg_total:.2f}s")
    print("=" * 70)

    report = {
        "config": {
            "model": voice.stt.model, "beam_size": voice.stt.beam_size,
            "silence_timeout_s": voice.stt.silence_timeout_s,
            "min_speech_s": voice.stt.min_speech_s, "pre_roll_ms": voice.stt.pre_roll_ms,
            "vad_enabled": voice.stt.vad_enabled, "vad_threshold": voice.stt.vad_threshold,
        },
        "summary": {
            "utterances": n, "first_attempt_ok": n_ok, "words_clipped": n_clipped,
            "no_speech": n_no_speech, "max_duration": n_max_dur,
            "avg_mic_ready_s": avg_cold_start, "avg_stt_latency_s": avg_stt,
            "avg_total_latency_s": avg_total,
        },
        "utterances": [asdict(r) for r in records],
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nFull report written to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
