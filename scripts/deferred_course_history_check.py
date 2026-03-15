#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from automation import EKHNPAutomator
from config import Settings


def run_check() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        deferred_path = Path(tmp) / "deferred_exam_courses.json"
        settings_a = Settings(
            exam_deferred_courses_path=str(deferred_path),
            exam_quality_report_dir=str(Path(tmp) / "reports"),
            exam_answer_bank_path=str(Path(tmp) / "answer_bank.json"),
            user_id="account_a",
            user_password="pw_a",
        )
        settings_b = Settings(
            exam_deferred_courses_path=str(deferred_path),
            exam_quality_report_dir=str(Path(tmp) / "reports"),
            exam_answer_bank_path=str(Path(tmp) / "answer_bank.json"),
            user_id="account_b",
            user_password="pw_b",
        )

        a1 = EKHNPAutomator(settings=settings_a, log_fn=None)
        a1._last_opened_course_title = "강좌 1"
        a1._mark_current_course_exam_deferred("잔여 응시 2회")
        a1._last_opened_course_title = "강좌 2"
        a1._mark_current_course_exam_deferred("잔여 응시 2회")

        a2 = EKHNPAutomator(settings=settings_a, log_fn=None)
        loaded_keys_a = sorted(a2._deferred_exam_course_keys)
        course_titles = ["강좌 1", "강좌 2", "강좌 3", "강좌 4"]
        remaining = [
            t for t in course_titles if a2._course_title_key(t) not in a2._deferred_exam_course_keys
        ]
        b1 = EKHNPAutomator(settings=settings_b, log_fn=None)
        loaded_keys_b = sorted(b1._deferred_exam_course_keys)
        b1._last_opened_course_title = "강좌 B1"
        b1._mark_current_course_exam_deferred("잔여 응시 2회")
        a3 = EKHNPAutomator(settings=settings_a, log_fn=None)
        b2 = EKHNPAutomator(settings=settings_b, log_fn=None)

        return {
            "deferred_file_exists": deferred_path.exists(),
            "deferred_count_a": len(loaded_keys_a),
            "deferred_keys_a": loaded_keys_a,
            "deferred_count_b_before": len(loaded_keys_b),
            "deferred_count_a_after_b_write": len(a3._deferred_exam_course_keys),
            "deferred_count_b_after_write": len(b2._deferred_exam_course_keys),
            "next_course_candidate": remaining[0] if remaining else "",
            "skip_chain_ok": (len(loaded_keys_a) >= 2 and (remaining[0] if remaining else "") == "강좌 3"),
            "isolation_ok": (
                len(loaded_keys_b) == 0
                and len(a3._deferred_exam_course_keys) == 2
                and len(b2._deferred_exam_course_keys) == 1
            ),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deferred course history persistence check")
    parser.add_argument(
        "--report-path",
        type=str,
        default="logs/deferred_course_history_check.json",
        help="report output path",
    )
    args = parser.parse_args()

    result = run_check()
    out_path = Path(args.report_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
