"""Phase 10.X.7 Steps 7/8 — train a lightweight, voice-specific wake-word
verifier from real recordings (scripts/record_wake_samples.py), using
openWakeWord's built-in speaker-dependent verifier support
(`openwakeword.custom_verifier_model`): a small scikit-learn logistic
regression trained on the *pretrained* model's own embedding features, not a
from-scratch retrain.

Why this, not a full custom "FRIDAY-accent hey_jarvis" model: a real custom
retrain (openwakeword/train.py) needs torch, torch_audiomentations,
torchaudio, speechbrain, and tens of thousands of synthetic TTS positive
clips plus hundreds of hours of negative/background audio for augmentation —
none of that is installed, and this project deliberately stays off PyTorch
(see PLAN.md's Risks table: "No PyTorch; ONNX everywhere"). The verifier path
needs only scikit-learn (already an installed dependency here), trains in
well under a minute on CPU from the same real recordings
scripts/record_wake_samples.py already collects, and directly targets "does
THIS voice/room/mic combination sound like the wake phrase" rather than
re-deriving phrase recognition from zero. See PLAN.md Phase 10.X.7 for the
measured evidence this was chosen from.

At runtime it's applied as a genuine two-stage detector via openWakeWord's
own native `custom_verifier_models`/`custom_verifier_threshold` support (see
friday/voice/wakeword.py's WakeWordDetector): the pretrained model acts as a
cheap, high-recall/low-precision Stage 1 candidate gate (a low, permissive
threshold), and this verifier is Stage 2 — it re-scores only the frames that
already cleared Stage 1, using the *same* embedding features already
computed, so there's no extra continuous inference cost.

IMPORTANT — `--positive-threshold`: openWakeWord's own default
(`train_custom_verifier`'s hardcoded 0.5) silently produces ZERO positive
training examples if the pretrained model's raw score for your voice never
reaches 0.5 for any recorded clip — which is exactly the failure mode this
whole phase exists to investigate. This script calls the lower-level
`get_reference_clip_features` directly instead, with a configurable
`--positive-threshold` that defaults to 0.05 specifically so a weak base
model doesn't also break verifier training. Check the printed "positive
feature vectors collected" count — if it's implausibly low relative to the
number of positive clips, the base model is barely firing on your voice at
all and no verifier will fully fix that (see PLAN.md Phase 10.X.7's honest
conclusion on whether the pretrained model itself is adequate).

Usage:
    python scripts/train_wake_verifier.py --samples-dir data/wakeword_samples/20260913_120000
    python scripts/train_wake_verifier.py --samples-dir ... --positive-threshold 0.02 --out data/models/wake_verifier.pkl

Then, to actually use it:
    1. Set voice.wakeword.verifier_model_path in config.yaml to the --out path.
    2. Re-run scripts/evaluate_wake_samples.py --verifier-model <path> against
       a FRESH recording round (not the one used to train) to check it
       actually generalizes instead of just memorizing this session's audio.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import paths  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.voice.types import WakeWordBackendError  # noqa: E402
from friday.voice.wakeword import WakeWordDetector  # noqa: E402

POSITIVE_KINDS = {"wake_normal", "wake_quiet", "wake_loud", "wake_conversational", "wake_distance"}
NEGATIVE_KINDS = {"silence", "speech", "noise"}


def resolve_model_path(model_name: str, models_dir: Path) -> Path:
    """Mirrors WakeWordDetector._load()'s own resolution — triggers the
    (cached-after-first-run) download if needed, then returns the actual
    .onnx file so we can hand openWakeWord an existing path (required for
    `train_custom_verifier`'s feature-key alignment — see module docstring).
    """
    from openwakeword.utils import download_models

    models_dir.mkdir(parents=True, exist_ok=True)
    download_models([model_name], target_directory=str(models_dir))
    candidates = sorted(models_dir.glob(f"{model_name}*.onnx"))
    if not candidates:
        raise WakeWordBackendError(f"model file for '{model_name}' not found in {models_dir}")
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples-dir", required=True, action="append", help="Manifest dir from record_wake_samples.py. Repeatable to combine multiple recording rounds.")
    parser.add_argument("--model", default=CFG.voice.wakeword.model)
    parser.add_argument("--models-dir", default=str(paths.MODELS / "openwakeword"))
    parser.add_argument("--positive-threshold", type=float, default=0.05, help="See module docstring — NOT openWakeWord's 0.5 default.")
    parser.add_argument("--out", default=str(paths.MODELS / "wake_verifier.pkl"))
    args = parser.parse_args()

    import openwakeword
    from openwakeword.custom_verifier_model import get_reference_clip_features, train_verifier_model
    import numpy as np

    model_path = resolve_model_path(args.model, Path(args.models_dir))
    model_name = model_path.stem
    print(f"Base model: {model_path} (feature key: {model_name!r})")

    oww = openwakeword.Model(
        wakeword_models=[str(model_path)],
        inference_framework="onnx",
        melspec_model_path=str(model_path.parent / "melspectrogram.onnx"),
        embedding_model_path=str(model_path.parent / "embedding_model.onnx"),
        ncpu=1,
    )

    positive_paths: list[Path] = []
    negative_paths: list[Path] = []
    for samples_dir_str in args.samples_dir:
        samples_dir = Path(samples_dir_str)
        manifest = json.loads((samples_dir / "manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["attempts"]:
            wav_path = samples_dir / entry["wav"]
            if entry["kind"] in POSITIVE_KINDS:
                positive_paths.append(wav_path)
            elif entry["kind"] in NEGATIVE_KINDS:
                negative_paths.append(wav_path)

    print(f"Positive clips: {len(positive_paths)}   Negative clips: {len(negative_paths)}")
    if not positive_paths or not negative_paths:
        print("FAILED: need at least one positive and one negative clip — run scripts/record_wake_samples.py first.")
        return 1

    print(f"Extracting positive features (threshold={args.positive_threshold})...")
    positive_features = np.vstack([
        get_reference_clip_features(str(p), oww, model_name, threshold=args.positive_threshold, N=5)
        for p in positive_paths
    ])
    print(f"  -> {positive_features.shape[0]} positive feature vectors collected")
    if positive_features.shape[0] == 0:
        print(
            "FAILED: zero positive feature vectors — the base model's raw score never reached "
            f"{args.positive_threshold} on ANY recorded clip. Try --positive-threshold 0.0 (accepts "
            "every frame from positive clips, noisiest but always non-empty) or re-record closer to "
            "the mic. If even --positive-threshold 0.0 still trains a useless verifier (near-chance "
            "accuracy below), the pretrained model itself is not picking up any signal for this "
            "voice/room/mic and a verifier cannot rescue that — see PLAN.md Phase 10.X.7."
        )
        return 1

    print("Extracting negative features (all frames, no threshold)...")
    negative_features = np.vstack([
        get_reference_clip_features(str(p), oww, model_name, threshold=0.0, N=1)
        for p in negative_paths
    ])
    print(f"  -> {negative_features.shape[0]} negative feature vectors collected")

    print("Training logistic-regression verifier...")
    features = np.vstack((positive_features, negative_features))
    labels = np.array([1] * positive_features.shape[0] + [0] * negative_features.shape[0])
    pipeline = train_verifier_model(features, labels)

    # In-sample accuracy is a sanity check only (this is the data it was
    # trained on, not held-out) — real validation is a second
    # scripts/record_wake_samples.py round scored via
    # scripts/evaluate_wake_samples.py --verifier-model, per the module
    # docstring.
    in_sample_acc = pipeline.score(features, labels)
    print(f"In-sample accuracy (NOT a generalization estimate): {in_sample_acc:.3f}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        pickle.dump(pipeline, f)
    print(f"\nVerifier saved to {out_path}")
    print(
        "\nNext steps:\n"
        f"  1. Set voice.wakeword.verifier_model_path: \"{out_path.as_posix()}\" in config.yaml\n"
        "  2. Record a FRESH round: python scripts/record_wake_samples.py --out-dir data/wakeword_samples/round2\n"
        f"  3. python scripts/evaluate_wake_samples.py --samples-dir data/wakeword_samples/round2 "
        f"--verifier-model \"{out_path.as_posix()}\"\n"
        "     ...and compare its detection/false-positive numbers against the same round scored "
        "WITHOUT --verifier-model."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
