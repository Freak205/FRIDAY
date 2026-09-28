"""Real-recording STT accuracy benchmark (Phase 10.X.3, extended 10.X.5).

Unlike scripts/voice_benchmark.py (TTS-synthesized audio — useful for
comparing model/decoding choices on clean, accent-free speech, but not a
substitute for the real thing), this script replays YOUR OWN recorded voice
against one or more STT configurations and reports word error rate (WER),
latency, and the actual substitutions made, so an Indian/Hyderabad-English
accuracy improvement can be measured instead of guessed at.

Record your samples first with:
    python scripts/voice_mic_latency_test.py --save-samples --utterances 20

That writes data/voice_test_samples/<id>.wav + <id>.txt + manifest.json.
Then compare configs against them:

    python scripts/benchmark_stt.py
    python scripts/benchmark_stt.py --models base,small.en --beam-sizes 1,5
    python scripts/benchmark_stt.py --vad-filter true,false
    python scripts/benchmark_stt.py --initial-prompt "FRIDAY, Jarvis, VS Code, Chrome, WhatsApp"
    python scripts/benchmark_stt.py --vocabulary default
    python scripts/benchmark_stt.py --vocabulary "FRIDAY,VS Code,WhatsApp"

Samples whose expected_text has no letters at all (e.g. a literal "=" typed
by mistake into voice_mic_latency_test.py's transcript prompt) are skipped
automatically with a warning — don't hand-edit manifest.json to "fix" those,
re-record that sample instead.

DO NOT treat a good WER here as proof STT is "fixed" without also doing the
live manual tests in MANUAL_VALIDATION.md — this script only tells you
whether one configuration is measurably better than another on the samples
you gave it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import paths  # noqa: E402
from friday.voice.stt import SttEngine  # noqa: E402
from friday.voice.vocabulary import DEFAULT_VOCABULARY, build_hotwords  # noqa: E402

SR = 16000


@dataclass
class Sample:
    id: str
    audio: np.ndarray
    expected_text: str


def load_samples(samples_dir: Path) -> list[Sample]:
    """Loads manifest.json's (wav, expected_text) pairs, skipping any entry
    whose expected_text has no letters at all — a real corruption seen in
    practice (Phase 10.X.3/10.X.4 round): the operator typed a literal "="
    into voice_mic_latency_test.py's "type what you actually said" prompt
    instead of leaving it blank, so "=" became that sample's WER "ground
    truth" and would silently reward/penalize configs at random. Don't
    optimize against those — this filters them out rather than trusting the
    caller to have already done so.
    """
    manifest_path = samples_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples: list[Sample] = []
    skipped_contaminated: list[str] = []
    for entry in manifest.get("samples", []):
        expected_text = entry["expected_text"]
        if not any(c.isalpha() for c in expected_text):
            skipped_contaminated.append(entry["id"])
            continue
        wav_path = samples_dir / entry["wav"]
        if not wav_path.exists():
            print(f"  (skipping {entry['id']}: {wav_path} missing)")
            continue
        with wave.open(str(wav_path), "rb") as wf:
            assert wf.getsampwidth() == 2, f"{wav_path} isn't 16-bit PCM"
            src_sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if src_sr != SR:
            duration_s = pcm.size / src_sr
            n_out = int(round(duration_s * SR))
            src_t = np.linspace(0, duration_s, num=pcm.size, endpoint=False)
            dst_t = np.linspace(0, duration_s, num=n_out, endpoint=False)
            pcm = np.interp(dst_t, src_t, pcm).astype(np.float32)
        samples.append(Sample(id=entry["id"], audio=pcm, expected_text=expected_text))
    if skipped_contaminated:
        print(
            f"  (skipping {len(skipped_contaminated)} contaminated sample(s) with no letters "
            f"in expected_text — ids: {', '.join(skipped_contaminated)})"
        )
    return samples


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", "", text.lower()).split()


def word_error_rate(expected: str, actual: str) -> tuple[float, list[str]]:
    """Standard WER via word-level Levenshtein edit distance. Returns
    (wer, alignment_notes) where alignment_notes is a short human-readable
    list of substitutions/insertions/deletions for the worst-offender report.
    """
    ref, hyp = _words(expected), _words(actual)
    n, m = len(ref), len(hyp)
    if n == 0:
        return (0.0 if m == 0 else 1.0), ([] if m == 0 else [f"inserted {' '.join(hyp)!r} (expected nothing)"])

    d = [[0] * (m + 1) for _ in range(n + 1)]
    op = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0], op[i][0] = i, "D"
    for j in range(m + 1):
        d[0][j], op[0][j] = j, "I"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                d[i][j], op[i][j] = d[i - 1][j - 1], "="
            else:
                sub, dele, ins = d[i - 1][j - 1] + 1, d[i - 1][j] + 1, d[i][j - 1] + 1
                best = min(sub, dele, ins)
                d[i][j] = best
                op[i][j] = "S" if best == sub else ("D" if best == dele else "I")

    # Backtrack for a short list of the actual substitutions (most useful
    # single diagnostic: "expected X, heard Y").
    notes: list[str] = []
    i, j = n, m
    while i > 0 or j > 0:
        code = op[i][j]
        if code == "=":
            i, j = i - 1, j - 1
        elif code == "S":
            notes.append(f"{ref[i - 1]!r} -> {hyp[j - 1]!r}")
            i, j = i - 1, j - 1
        elif code == "D":
            notes.append(f"{ref[i - 1]!r} -> (missing)")
            i -= 1
        else:  # "I"
            notes.append(f"(unexpected) {hyp[j - 1]!r}")
            j -= 1
    notes.reverse()
    return d[n][m] / n, notes


@dataclass
class Row:
    config: str
    sample_id: str
    expected: str
    actual: str
    wer: float
    notes: list[str]
    latency_s: float


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples-dir", default=str(paths.DATA / "voice_test_samples"))
    parser.add_argument("--models", default="base,small,base.en,small.en")
    parser.add_argument("--beam-sizes", default="1")
    parser.add_argument("--vad-filter", default="false", help="comma-separated true/false values to try")
    parser.add_argument("--initial-prompt", default="", help="single prompt string to try alongside no prompt")
    parser.add_argument(
        "--vocabulary", default="",
        help="comma-separated hotwords (faster-whisper's `hotwords` decode option, see "
        "friday/voice/vocabulary.py) to try alongside no vocabulary. Pass 'default' to use "
        "friday.voice.vocabulary.DEFAULT_VOCABULARY.",
    )
    args = parser.parse_args()

    samples_dir = Path(args.samples_dir)
    samples = load_samples(samples_dir)
    if not samples:
        print(
            f"No samples found in {samples_dir}.\n"
            "Record some first:\n"
            "    python scripts/voice_mic_latency_test.py --save-samples --utterances 20\n"
        )
        return 1
    print(f"Loaded {len(samples)} real-recorded sample(s) from {samples_dir}\n")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    beam_sizes = [int(b.strip()) for b in args.beam_sizes.split(",") if b.strip()]
    vad_filters = [v.strip().lower() == "true" for v in args.vad_filter.split(",") if v.strip()]
    prompts = [""] + ([args.initial_prompt] if args.initial_prompt else [])
    if args.vocabulary.strip().lower() == "default":
        vocabularies = [(), DEFAULT_VOCABULARY]
    elif args.vocabulary.strip():
        vocabularies = [(), tuple(w.strip() for w in args.vocabulary.split(",") if w.strip())]
    else:
        vocabularies = [()]

    rows: list[Row] = []
    for model_size in models:
        for beam_size in beam_sizes:
            for vad_filter in vad_filters:
                for prompt in prompts:
                    for vocabulary in vocabularies:
                        hotwords = build_hotwords(vocabulary)
                        config = f"model={model_size} beam={beam_size} vad_filter={vad_filter}" + (
                            f" prompt={prompt[:20]!r}..." if prompt else ""
                        ) + (f" hotwords={hotwords[:30]!r}..." if hotwords else "")
                        try:
                            engine = SttEngine(
                                model_size=model_size, device="cpu", compute_type="int8",
                                beam_size=beam_size, vad_filter=vad_filter, initial_prompt=prompt,
                                vocabulary=vocabulary,
                            )
                            engine._ensure_model()
                        except Exception as exc:
                            print(f"  [{config}] FAILED TO LOAD: {exc}")
                            continue
                        print(f"[{config}] loaded — transcribing {len(samples)} sample(s)...")
                        for sample in samples:
                            result = engine.transcribe(sample.audio)
                            actual = result.text if result.ok else f"<STT ERROR: {result.error}>"
                            wer, notes = word_error_rate(sample.expected_text, actual)
                            rows.append(Row(config, sample.id, sample.expected_text, actual, wer, notes, result.latency_s))

    if not rows:
        print("No configs produced any results.")
        return 1

    # -- per-sample detail ---------------------------------------------------
    print("\n" + "=" * 100)
    print("Per-sample results (worst first):")
    print("=" * 100)
    for row in sorted(rows, key=lambda r: -r.wer)[:25]:
        print(f"\n[{row.config}] sample {row.sample_id}  WER={row.wer:.2f}  latency={row.latency_s:.2f}s")
        print(f"  EXPECTED: {row.expected!r}")
        print(f"  ACTUAL:   {row.actual!r}")
        if row.notes:
            print(f"  DIFF:     {'; '.join(row.notes)}")

    # -- summary by config ----------------------------------------------------
    print("\n" + "=" * 100)
    configs = sorted(set(r.config for r in rows))
    print(f"{'config':<70} {'avg_wer':<9} {'max_wer':<9} {'avg_lat_s':<10}")
    print("-" * 100)
    summary = []
    for config in configs:
        subset = [r for r in rows if r.config == config]
        avg_wer = sum(r.wer for r in subset) / len(subset)
        max_wer = max(r.wer for r in subset)
        avg_lat = sum(r.latency_s for r in subset) / len(subset)
        print(f"{config:<70} {avg_wer:<9.3f} {max_wer:<9.3f} {avg_lat:<10.3f}")
        summary.append((config, avg_wer, max_wer, avg_lat))
    print("=" * 100)

    best = min(summary, key=lambda s: s[1])
    print(f"\nLowest average WER: {best[0]} (avg_wer={best[1]:.3f})")
    print(
        "\nReminder: WER here reflects only the samples you recorded. Don't change "
        "config.yaml's voice.stt defaults off a handful of samples — re-run with more "
        "samples across sessions/moods/background noise before trusting a small gap, "
        "and always confirm with a live MANUAL_VALIDATION.md pass afterward.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
