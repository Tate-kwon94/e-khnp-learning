#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from automation import EKHNPAutomator
from config import Settings


def _answer_norm(automator: EKHNPAutomator, item: dict) -> str:
    ans = str(item.get("answer_option_norm", "")).strip()
    if ans:
        return ans
    options = [str(x).strip() for x in item.get("options", []) if str(x).strip()]
    try:
        idx = int(item.get("answer_index", 0))
    except Exception:  # noqa: BLE001
        idx = 0
    if 1 <= idx <= len(options):
        return automator._normalize_answer_text(options[idx - 1])
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Answer-bank shuffle remap consistency check")
    parser.add_argument("--bank", default="rag/exam_answer_bank.json", help="answer-bank json path")
    parser.add_argument("--limit", type=int, default=0, help="max questions to sample (0=all)")
    parser.add_argument("--shuffles", type=int, default=5, help="shuffle trials per question")
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    parser.add_argument("--show-fails", type=int, default=10, help="max fail samples")
    args = parser.parse_args()

    bank_path = Path(args.bank)
    if not bank_path.exists():
        print(f"[ERR] answer-bank not found: {bank_path}")
        return 2

    random.seed(args.seed)

    settings = Settings()
    settings.exam_answer_bank_path = str(bank_path)
    automator = EKHNPAutomator(settings=settings, log_fn=None)

    items = [x for x in automator._answer_bank_items.values() if isinstance(x, dict)]
    if not items:
        print("[ERR] no answer-bank items")
        return 2
    if args.limit > 0 and len(items) > args.limit:
        items = random.sample(items, args.limit)

    total_trials = 0
    pass_trials = 0
    skipped = 0
    fail_rows: list[dict[str, str]] = []

    for item in items:
        question = str(item.get("question", "")).strip()
        options = [str(x).strip() for x in item.get("options", []) if str(x).strip()]
        if not question or len(options) < 2:
            skipped += 1
            continue
        ans_norm = _answer_norm(automator, item)
        if not ans_norm:
            skipped += 1
            continue

        base_norms = [automator._normalize_answer_text(x) for x in options]
        if base_norms.count(ans_norm) > 1:
            skipped += 1
            continue

        for _ in range(max(1, int(args.shuffles))):
            shuffled = list(options)
            random.shuffle(shuffled)
            now_norms = [automator._normalize_answer_text(x) for x in shuffled]
            expected_idx, _, _ = automator._map_answer_option_norm_to_choice(ans_norm, now_norms)
            if expected_idx < 1:
                skipped += 1
                continue

            pred = automator._lookup_answer_bank_choice(
                question=question,
                options=shuffled,
                exam_meta=item.get("exam_meta") if isinstance(item.get("exam_meta"), dict) else None,
            )
            total_trials += 1
            if isinstance(pred, dict) and int(pred.get("choice", 0)) == expected_idx:
                pass_trials += 1
                continue
            fail_rows.append(
                {
                    "question": question[:80],
                    "expected_idx": str(expected_idx),
                    "pred_idx": str(int(pred.get("choice", 0)) if isinstance(pred, dict) else 0),
                }
            )

    fail_trials = total_trials - pass_trials
    pass_rate = (pass_trials / total_trials) if total_trials > 0 else 0.0
    print(
        "shuffle-check:",
        f"trials={total_trials}",
        f"pass={pass_trials}",
        f"fail={fail_trials}",
        f"pass_rate={pass_rate:.4f}",
        f"skipped={skipped}",
    )
    if fail_rows:
        print("fail_samples:")
        for row in fail_rows[: max(1, int(args.show_fails))]:
            print(
                f"- expected={row['expected_idx']} pred={row['pred_idx']} "
                f"question={row['question']}"
            )
    return 0 if fail_trials == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
