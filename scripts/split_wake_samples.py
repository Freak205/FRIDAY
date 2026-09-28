"""Phase 10.X.8 -- stratified, no-leakage train/test split of a
record_wake_samples.py corpus, so scripts/train_wake_verifier.py and
scripts/evaluate_wake_samples.py can be pointed at genuinely disjoint
subsets of the SAME recording session's positive clips.

Why this exists: only one real-mic recording round exists
(data/wakeword_samples/20260913_234931) -- the "train on round1, validate on
round2" approach scripts/train_wake_verifier.py's own docstring recommends
isn't available yet. Splitting the existing 60 positive clips is the next
best honest alternative, NOT a substitute for a second, independently
recorded round -- see the printed warning and PLAN.md Phase 10.X.8 for why
that matters (12 speakers/positions worth of correlated room/mic/voice
conditions from one sitting, not 60 independent samples).

Splits only the 60 POSITIVE_KINDS clips, stratified per kind (so train and
test each get a proportional share of normal/quiet/loud/conversational/
distance), with a fixed seed for reproducibility. The 3 negative clips
(silence/speech/noise) are referenced UNCHANGED by both splits -- there is
only one recording of each, the verifier's negative training class needs
them, and they are never treated as positive in either split, so there is no
"detection" leakage. It does mean the held-out false-positive numbers are
not validated against literally-unseen negative audio to the same degree
the positive recall numbers are held out -- report this caveat, don't hide
it.

No audio is copied. Each output manifest's "wav" entries hold the
ORIGINAL clip's absolute path, so evaluate_wake_samples.py and
train_wake_verifier.py work against these directories completely unmodified
-- they just resolve `samples_dir / entry["wav"]`, and pathlib returns an
absolute RHS unchanged regardless of the LHS.

Usage:
    python scripts/split_wake_samples.py --samples-dir data/wakeword_samples/20260913_234931
    python scripts/split_wake_samples.py --samples-dir ... --train-frac 0.8 --seed 42 --out-dir data/wakeword_samples/20260913_234931_split
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

POSITIVE_KINDS = {"wake_normal", "wake_quiet", "wake_loud", "wake_conversational", "wake_distance"}
NEGATIVE_KINDS = {"silence", "speech", "noise"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples-dir", required=True)
    parser.add_argument("--train-frac", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", default=None, help="Default: <samples-dir>_split")
    args = parser.parse_args()

    samples_dir = Path(args.samples_dir).resolve()
    manifest = json.loads((samples_dir / "manifest.json").read_text(encoding="utf-8"))

    by_kind: dict[str, list[dict]] = defaultdict(list)
    negatives: list[dict] = []
    for entry in manifest["attempts"]:
        if entry["kind"] in POSITIVE_KINDS:
            by_kind[entry["kind"]].append(entry)
        elif entry["kind"] in NEGATIVE_KINDS:
            negatives.append(entry)
        else:
            raise ValueError(f"unknown kind {entry['kind']!r} in {samples_dir / 'manifest.json'}")

    rng = random.Random(args.seed)
    train_entries: list[dict] = []
    test_entries: list[dict] = []
    split_summary: dict[str, dict[str, int]] = {}
    for kind, entries in sorted(by_kind.items()):
        entries = list(entries)
        rng.shuffle(entries)
        if len(entries) > 1:
            n_train = max(1, min(len(entries) - 1, round(len(entries) * args.train_frac)))
        else:
            n_train = len(entries)
        train_entries += entries[:n_train]
        test_entries += entries[n_train:]
        split_summary[kind] = {"train": n_train, "test": len(entries) - n_train, "total": len(entries)}

    def to_absolute(entries: list[dict]) -> list[dict]:
        out = []
        for e in entries:
            e2 = dict(e)
            e2["wav"] = str((samples_dir / e["wav"]).resolve())
            out.append(e2)
        return out

    out_dir = Path(args.out_dir) if args.out_dir else samples_dir.parent / f"{samples_dir.name}_split"
    train_dir = out_dir / "train"
    test_dir = out_dir / "test"
    train_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    common = {"source_samples_dir": str(samples_dir), "seed": args.seed, "train_frac": args.train_frac}
    train_manifest = {**common, "split": "train", "attempts": to_absolute(train_entries) + to_absolute(negatives)}
    test_manifest = {**common, "split": "test", "attempts": to_absolute(test_entries) + to_absolute(negatives)}

    (train_dir / "manifest.json").write_text(json.dumps(train_manifest, indent=2), encoding="utf-8")
    (test_dir / "manifest.json").write_text(json.dumps(test_manifest, indent=2), encoding="utf-8")

    print(f"Source: {samples_dir}")
    print(f"Train:  {len(train_entries)} positive clips -> {train_dir}")
    print(f"Test:   {len(test_entries)} positive clips (held out of training) -> {test_dir}")
    print(f"Negatives: {len(negatives)} clips referenced UNCHANGED by BOTH splits -- see module docstring caveat")
    print("By kind:")
    for kind, s in split_summary.items():
        print(f"  {kind:>22}: train={s['train']:<3} test={s['test']:<3} total={s['total']}")
    print(
        "\nCAVEAT: this is a within-session split, not a second independent recording "
        "round. Train and test clips still share the same sitting's room acoustics, "
        "mic gain, and short-term voice state -- held-out numbers below are a real "
        "leakage check for the verifier's *model weights*, but not full proof of "
        "generalization to a different day/session. See PLAN.md Phase 10.X.8."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
