from __future__ import annotations

import argparse
import re
import time
from datetime import datetime

from automation import EKHNPAutomator
from config import Settings


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _print(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def _extract_progress_percent(text: str) -> int | None:
    m = re.search(r"학습진도율\s*(\d{1,3})\s*%", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Overnight progress runner for e-KHNP learning automation")
    parser.add_argument("--target-percent", type=int, default=80, help="목표 진도율(기본 80)")
    parser.add_argument("--max-cycles", type=int, default=30, help="최대 반복 사이클 수(기본 30)")
    parser.add_argument("--lessons-per-cycle", type=int, default=1, help="사이클당 진행할 차시 수(기본 1)")
    parser.add_argument("--sleep-seconds", type=int, default=5, help="사이클 간 대기(초)")
    args = parser.parse_args()

    settings = Settings()
    settings.headless = True
    settings.timeout_ms = max(settings.timeout_ms, 90000)

    if not settings.user_id or not settings.user_password:
        _print("중단: EKHNP_USER_ID / EKHNP_USER_PASSWORD 환경변수가 필요합니다.")
        return 1

    target_percent = max(1, min(args.target_percent, 100))
    max_cycles = max(1, args.max_cycles)
    lessons_per_cycle = max(1, args.lessons_per_cycle)
    sleep_seconds = max(0, args.sleep_seconds)

    _print(
        f"시작: target={target_percent}% max_cycles={max_cycles} lessons_per_cycle={lessons_per_cycle}"
    )

    last_percent: int | None = None
    for cycle in range(1, max_cycles + 1):
        _print(f"[Cycle {cycle}/{max_cycles}] 차시 진행 시작")

        automator = EKHNPAutomator(settings=settings, log_fn=_print)
        run_result = automator.login_and_complete_first_course_lesson(
            stop_rule="manual",
            manual_lesson_limit=lessons_per_cycle,
            safety_max_lessons=max(lessons_per_cycle + 2, 6),
        )
        _print(f"[Cycle {cycle}] 차시 진행 결과: success={run_result.success} msg={run_result.message}")

        probe = EKHNPAutomator(settings=settings, log_fn=_print).login_and_probe_comprehensive_exam(max_questions=1)
        _print(f"[Cycle {cycle}] 상태 확인 결과: success={probe.success} msg={probe.message}")

        if probe.success:
            _print(f"완료: 종합평가 진입 가능(>= {target_percent}%) 상태입니다.")
            return 0

        pct = _extract_progress_percent(probe.message)
        if pct is not None:
            last_percent = pct
            _print(f"[Cycle {cycle}] 현재 진도율 추정: {pct}%")
            if pct >= target_percent:
                _print("완료: 목표 진도율 도달")
                return 0

        if cycle < max_cycles and sleep_seconds > 0:
            _print(f"[Cycle {cycle}] 다음 사이클까지 {sleep_seconds}초 대기")
            time.sleep(sleep_seconds)

    if last_percent is not None:
        _print(f"종료: 최대 사이클 도달(마지막 진도율 추정 {last_percent}%)")
    else:
        _print("종료: 최대 사이클 도달(진도율 판독값 없음)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
