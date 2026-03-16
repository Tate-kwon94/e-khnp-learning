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


def _count_matches(lines: list[str], pattern: str) -> int:
    reg = re.compile(pattern)
    return sum(1 for ln in lines if reg.search(ln))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run completion workflow E2E and emit a compact report")
    parser.add_argument("--user-id", default="", help="EKHNP user id (fallback: env)")
    parser.add_argument("--user-password", default="", help="EKHNP user password (fallback: env)")
    parser.add_argument("--conf-threshold", type=float, default=0.62)
    parser.add_argument("--conf-escalate-margin", type=float, default=0.08)
    parser.add_argument("--completion-max-courses", type=int, default=2)
    parser.add_argument("--check-interval-minutes", type=int, default=3)
    parser.add_argument("--max-timefill-checks", type=int, default=1)
    parser.add_argument("--safety-max-lessons", type=int, default=20)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    settings = Settings()
    settings.user_id = args.user_id.strip() or settings.user_id
    settings.user_password = args.user_password.strip() or settings.user_password
    settings.headless = bool(args.headless)
    settings.rag_conf_threshold = float(args.conf_threshold)
    settings.rag_conf_escalate_margin = float(args.conf_escalate_margin)
    settings.completion_max_courses = max(1, min(40, int(args.completion_max_courses)))
    settings.timeout_ms = max(settings.timeout_ms, 30000)

    if not settings.user_id or not settings.user_password:
        print("error: missing credentials (set --user-id/--user-password or env EKHNP_USER_ID/EKHNP_USER_PASSWORD)")
        return 2

    logs: list[str] = []

    def _log(msg: str) -> None:
        text = str(msg)
        logs.append(text)
        print(f"[{_now_iso()}] {text}", flush=True)

    started_at = _now_iso()
    automator = EKHNPAutomator(settings=settings, log_fn=_log)
    result = automator.login_and_run_completion_workflow(
        check_interval_minutes=max(3, int(args.check_interval_minutes)),
        max_timefill_checks=max(1, int(args.max_timefill_checks)),
        safety_max_lessons=max(6, int(args.safety_max_lessons)),
    )
    finished_at = _now_iso()

    metrics = {
        "structured_first_count": _count_matches(logs, r"보기 추출 1순위 적용"),
        "dom_second_count": _count_matches(logs, r"보기 추출 2순위 적용"),
        "ocr_third_count": _count_matches(logs, r"보기 추출 3순위 적용"),
        "model_switch_count": _count_matches(logs, r"저신뢰 모델 스위칭 반영"),
        "cross_check_result_count": _count_matches(logs, r"교차검증 모델 결과"),
        "course_completed_count": _count_matches(logs, r"과정 수료 완료 확인"),
        "course_skipped_count": _count_matches(logs, r"과정 우회 처리 완료"),
        "remaining_attempt_skip_count": _count_matches(logs, r"종합평가 잔여 응시 .* 우회"),
        "exam_retry_count": _count_matches(logs, r"종합평가 자동 재응시 시작"),
        "answer_bank_match_count": _count_matches(logs, r"정답 인덱스 매칭 사용"),
        "proxy_preflight_ok_count": _count_matches(logs, r"proxy-preflight-ok"),
        "proxy_preflight_fail_count": _count_matches(logs, r"proxy-preflight-(?:mismatch|failed|unknown)"),
        "counter_source_mismatch_count": _count_matches(logs, r"counter-source-mismatch"),
    }

    payload: dict[str, Any] = {
        "meta": {
            "started_at": started_at,
            "finished_at": finished_at,
            "conf_threshold": float(settings.rag_conf_threshold),
            "conf_escalate_margin": float(settings.rag_conf_escalate_margin),
            "completion_max_courses": int(settings.completion_max_courses),
            "check_interval_minutes": max(3, int(args.check_interval_minutes)),
            "max_timefill_checks": max(1, int(args.max_timefill_checks)),
            "safety_max_lessons": max(6, int(args.safety_max_lessons)),
        },
        "result": {
            "success": bool(result.success),
            "message": str(result.message),
            "url": str(result.current_url),
        },
        "diagnostics": automator.get_runtime_diagnostics(),
        "metrics": metrics,
        "log_count": len(logs),
        "logs_tail": logs[-120:],
    }

    if args.report_path:
        out = Path(args.report_path)
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out = Path("logs") / f"completion_e2e_report_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"result_success={result.success} "
        f"completed={metrics['course_completed_count']} skipped={metrics['course_skipped_count']} "
        f"switches={metrics['model_switch_count']} structured={metrics['structured_first_count']}"
    )
    print(f"report_path={out}")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
