#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from automation import EKHNPAutomator
from config import Settings


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _contains_any(logs: list[str], patterns: list[str]) -> bool:
    regs = [re.compile(p) for p in patterns]
    return any(any(r.search(line) for r in regs) for line in logs)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot full smoke: one-click workflow(timefill->exam->bypass) validation"
    )
    parser.add_argument("--user-id", default="", help="EKHNP user id (fallback: env)")
    parser.add_argument("--user-password", default="", help="EKHNP password (fallback: env)")
    parser.add_argument("--completion-max-courses", type=int, default=3)
    parser.add_argument("--check-interval-minutes", type=int, default=3)
    parser.add_argument("--max-timefill-checks", type=int, default=2)
    parser.add_argument("--safety-max-lessons", type=int, default=30)
    parser.add_argument("--strict", action="store_true", help="Fail if timefill/exam/bypass markers are not all seen")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    settings = Settings()
    settings.user_id = args.user_id.strip() or settings.user_id
    settings.user_password = args.user_password.strip() or settings.user_password
    settings.completion_max_courses = max(1, min(50, int(args.completion_max_courses)))
    settings.headless = bool(args.headless)
    settings.timeout_ms = max(settings.timeout_ms, 30000)

    if not settings.user_id or not settings.user_password:
        print("error: missing credentials (set --user-id/--user-password or env EKHNP_USER_ID/EKHNP_USER_PASSWORD)")
        return 2

    logs: list[str] = []

    def _log(msg: str) -> None:
        line = str(msg)
        logs.append(line)
        print(f"[{_now_iso()}] {line}", flush=True)

    started = _now_iso()
    automator = EKHNPAutomator(settings=settings, log_fn=_log)
    result = automator.login_and_run_completion_workflow(
        check_interval_minutes=max(1, int(args.check_interval_minutes)),
        max_timefill_checks=max(1, int(args.max_timefill_checks)),
        safety_max_lessons=max(6, int(args.safety_max_lessons)),
    )
    finished = _now_iso()

    markers = {
        "timefill_seen": _contains_any(
            logs,
            [
                r"학습시간 부족",
                r"학습시간 보충",
                r"학습시간 .*충족",
            ],
        ),
        "comprehensive_exam_seen": _contains_any(
            logs,
            [
                r"comprehensive-exam-opened",
                r"종합평가 자동",
                r"시험 자동 풀이",
                r"종합평가 .*응시 시작",
                r"종합평가 .*응시하기",
            ],
        ),
        "inline_quiz_seen": _contains_any(logs, [r"inline-quiz-detected", r"inline-quiz-advanced", r"inline-quiz-gate-opened"]),
        "round_next_seen": _contains_any(logs, [r"round-next-clicked"]),
        "final_next_seen": _contains_any(logs, [r"final-next-clicked"]),
        "incomplete_lesson_opened_seen": _contains_any(logs, [r"incomplete-lesson-opened"]),
        "completed_lesson_skipped_seen": _contains_any(logs, [r"completed-lesson-skipped"]),
        "resume_fallback_seen": _contains_any(logs, [r"resume-fallback-opened"]),
        "same_completed_lesson_reopened_seen": _contains_any(logs, [r"same-completed-lesson-reopened"]),
        "lesson_progress_unchanged_seen": _contains_any(logs, [r"lesson-progress-still-unchanged"]),
        "counter_source_mismatch_seen": _contains_any(logs, [r"counter-source-mismatch"]),
        "bypass_seen": _contains_any(logs, [r"우회", r"deferred"]),
        "numeric_strict_seen": _contains_any(logs, [r"숫자 문항 엄격 검증 적용", r"strict-numeric"]),
        "negative_evidence_seen": _contains_any(logs, [r"Negative Evidence 감점 적용"]),
        "exam_quality_ok_seen": _contains_any(logs, [r"시험 파싱 품질 확인"]),
        "exam_quality_warn_seen": _contains_any(logs, [r"시험 파싱 품질 경고"]),
        "proxy_preflight_ok_seen": _contains_any(logs, [r"proxy-preflight-ok"]),
        "proxy_preflight_mismatch_seen": _contains_any(logs, [r"proxy-preflight-mismatch", r"proxy-preflight-failed"]),
    }

    payload: dict[str, Any] = {
        "meta": {
            "started_at": started,
            "finished_at": finished,
            "completion_max_courses": settings.completion_max_courses,
            "check_interval_minutes": max(1, int(args.check_interval_minutes)),
            "max_timefill_checks": max(1, int(args.max_timefill_checks)),
            "safety_max_lessons": max(6, int(args.safety_max_lessons)),
            "strict_mode": bool(args.strict),
        },
        "result": {
            "success": bool(result.success),
            "message": str(result.message),
            "url": str(result.current_url),
        },
        "diagnostics": automator.get_runtime_diagnostics(),
        "markers": markers,
        "log_count": len(logs),
        "logs_tail": logs[-200:],
    }

    if args.report_path:
        out = Path(args.report_path)
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out = Path("logs") / f"full_smoke_report_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    strict_ok = bool(markers["timefill_seen"] and markers["comprehensive_exam_seen"] and markers["bypass_seen"])
    print(
        "full-smoke: "
        f"success={result.success} "
        f"timefill={markers['timefill_seen']} comprehensive_exam={markers['comprehensive_exam_seen']} "
        f"inline_quiz={markers['inline_quiz_seen']} round_next={markers['round_next_seen']} "
        f"final_next={markers['final_next_seen']} incomplete_lesson={markers['incomplete_lesson_opened_seen']} "
        f"completed_skipped={markers['completed_lesson_skipped_seen']} "
        f"resume_fallback={markers['resume_fallback_seen']} "
        f"same_completed_reopened={markers['same_completed_lesson_reopened_seen']} "
        f"progress_unchanged={markers['lesson_progress_unchanged_seen']} "
        f"counter_mismatch={markers['counter_source_mismatch_seen']} "
        f"proxy_ok={markers['proxy_preflight_ok_seen']} proxy_fail={markers['proxy_preflight_mismatch_seen']} "
        f"bypass={markers['bypass_seen']} "
        f"numeric={markers['numeric_strict_seen']} neg_evidence={markers['negative_evidence_seen']} "
        f"exam_quality_ok={markers['exam_quality_ok_seen']} exam_quality_warn={markers['exam_quality_warn_seen']}"
    )
    print(f"report_path={out}")

    if args.strict and not strict_ok:
        return 1
    return 0 if bool(result.success) else 1


if __name__ == "__main__":
    raise SystemExit(main())
