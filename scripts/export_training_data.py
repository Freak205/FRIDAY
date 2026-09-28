"""Export recorded episodic experiences as JSONL training-data candidates.

Phase 10, section 13: a *foundation* for future fine-tuning, not a training
run and not a place to fabricate examples. Every row this script writes
comes from a real `friday.intelligence.episodes.Episode` — an actual
`plan.run` invocation that actually happened, recorded by
`friday/skills/plan.py`. If nothing has been recorded yet, this prints 0 and
exits cleanly rather than inventing sample data.

Each line is a JSON object shaped roughly like:

    {"instruction": "<the goal>", "context": "<bounded working context>",
     "response": "<plan taken + outcome>", "success": true, "corrected": false}

Usage:
    python scripts/export_training_data.py                  # all episodes
    python scripts/export_training_data.py --filter success  # successful only
    python scripts/export_training_data.py --filter corrected
    python scripts/export_training_data.py --filter failed
    python scripts/export_training_data.py --out data/train.jsonl

Secrets are sanitized before they ever reach the episodes table (see
`friday.intelligence.episodes._sanitize_args`); this script does not need to
(and does not) re-inspect argument values for that reason, but it does skip
the `context` field entirely if it looks like it still contains something
sensitive, as a second, cheap backstop.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import store  # noqa: E402
from friday.intelligence import corrections, episodes  # noqa: E402

_SECRET_PATTERN = re.compile(
    r"\b(password|passwd|token|secret|api[_-]?key|otp)\b\s*[:=]", re.IGNORECASE
)


def _sanitize_context(text: str) -> str:
    return "[context omitted: looked sensitive]" if _SECRET_PATTERN.search(text) else text


def _response_for(ep: episodes.Episode) -> str:
    steps = "; ".join(
        f"{s['tool']}({s.get('args', {})}) -> {'ok' if s.get('ok') else 'failed'}"
        for s in ep.plan
    ) or "(no steps taken)"
    outcome = "succeeded" if ep.success else f"stopped ({ep.stopped})"
    return f"Steps: {steps}. Outcome: {outcome}."


def _corrected_goal_ids() -> set[str]:
    return {c.goal_id for c in corrections.recent(limit=10_000) if c.goal_id}


def export(filter_kind: str, out_path: Path) -> int:
    all_eps = episodes.all_episodes()
    corrected_ids = _corrected_goal_ids()

    rows: list[dict] = []
    for ep in all_eps:
        was_corrected = bool(ep.goal_id and ep.goal_id in corrected_ids)

        if filter_kind == "success" and not ep.success:
            continue
        if filter_kind == "failed" and ep.success:
            continue
        if filter_kind == "corrected" and not was_corrected:
            continue
        # "all" and anything else: no filtering.

        rows.append({
            "instruction": ep.goal_text,
            "context": _sanitize_context(ep.context),
            "response": _response_for(ep),
            "success": ep.success,
            "corrected": was_corrected,
            "stopped": ep.stopped,
            "at": ep.at,
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--filter", choices=["all", "success", "failed", "corrected"], default="all",
        help="which real episodes to include (default: all)",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("data/training_export.jsonl"),
        help="output JSONL path (default: data/training_export.jsonl)",
    )
    args = parser.parse_args()

    store.init()
    count = export(args.filter, args.out)
    print(f"wrote {count} real episode(s) (filter={args.filter}) to {args.out}")


if __name__ == "__main__":
    main()
