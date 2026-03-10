from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
import traceback
from urllib import error, request

from automation import EKHNPAutomator
from config import Settings


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _print(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_progress_percent(text: str) -> int | None:
    m = re.search(r"학습진도율\s*(\d{1,3})\s*%", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_progress_triplet(text: str) -> tuple[int | None, int | None, int | None]:
    cur = None
    req = None
    inc = None

    m_cur = re.search(r"학습진도율\s*(\d{1,3})\s*%", text)
    if m_cur:
        try:
            cur = int(m_cur.group(1))
        except ValueError:
            cur = None

    m_req = re.search(r"수료기준\s*(\d{1,3})\s*%", text)
    if m_req:
        try:
            req = int(m_req.group(1))
        except ValueError:
            req = None

    m_inc = re.search(r"미완료\s*(\d{1,3})\s*개", text)
    if m_inc:
        try:
            inc = int(m_inc.group(1))
        except ValueError:
            inc = None

    return cur, req, inc


def _check_ollama_ready(base_url: str) -> tuple[bool, str]:
    url = f"{base_url.rstrip('/')}/api/tags"
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=8) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"Ollama 연결 실패: {exc}"

    models = body.get("models")
    if not isinstance(models, list):
        return False, "Ollama 응답 형식이 예상과 다릅니다."
    return True, f"Ollama 준비됨(models={len(models)})"


def _rag_index_ready(index_path: str) -> tuple[bool, str]:
    p = Path(index_path)
    if not p.exists():
        return False, f"RAG 인덱스 없음: {index_path}"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"RAG 인덱스 파싱 실패: {exc}"
    chunks = raw.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return False, "RAG 인덱스에 청크가 없습니다."
    return True, f"RAG 인덱스 준비됨(chunks={len(chunks)})"


def _build_rag_if_needed(settings: Settings) -> tuple[bool, str]:
    ready, msg = _rag_index_ready(settings.rag_index_path)
    if ready:
        return True, msg

    docs_dir = Path(settings.rag_docs_dir)
    if not docs_dir.exists():
        return False, f"RAG 문서 폴더 없음: {settings.rag_docs_dir}"

    docs = [p for p in docs_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".txt", ".md", ".pdf"}]
    if not docs:
        return False, f"RAG 문서 없음: {settings.rag_docs_dir}"

    try:
        from rag_index import build_rag_index

        result = build_rag_index(
            docs_dir=settings.rag_docs_dir,
            index_path=settings.rag_index_path,
            embed_model=settings.rag_embed_model,
            ollama_base_url=settings.ollama_base_url,
            log_fn=_print,
        )
        return True, f"RAG 인덱스 생성 완료(files={result.get('files')} chunks={result.get('chunks')})"
    except Exception as exc:  # noqa: BLE001
        return False, f"RAG 인덱스 생성 실패: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Overnight progress runner for e-KHNP learning automation")
    parser.add_argument("--target-percent", type=int, default=80, help="목표 진도율(기본 80)")
    parser.add_argument("--max-cycles", type=int, default=30, help="최대 반복 사이클 수(기본 30)")
    parser.add_argument("--lessons-per-cycle", type=int, default=1, help="사이클당 진행할 차시 수(기본 1)")
    parser.add_argument("--sleep-seconds", type=int, default=5, help="사이클 간 대기(초)")
    parser.add_argument(
        "--allow-exam-without-llm",
        action="store_true",
        help="LLM 준비 전에도 종합평가 응시를 허용(기본: 비허용)",
    )
    parser.add_argument(
        "--report-path",
        type=str,
        default="logs/overnight_status.json",
        help="상태 요약 리포트 경로",
    )
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
    report_path = Path(args.report_path)
    require_llm_before_exam = not bool(args.allow_exam_without_llm)

    _print(
        f"시작: target={target_percent}% max_cycles={max_cycles} lessons_per_cycle={lessons_per_cycle}"
    )

    last_percent: int | None = None
    exam_attempted = False
    for cycle in range(1, max_cycles + 1):
        _print(f"[Cycle {cycle}/{max_cycles}] 차시 진행 시작")
        report: dict[str, object] = {
            "updated_at": _now(),
            "cycle": cycle,
            "max_cycles": max_cycles,
            "target_percent": target_percent,
            "lessons_per_cycle": lessons_per_cycle,
            "last_percent": last_percent,
            "exam_attempted": exam_attempted,
            "require_llm_before_exam": require_llm_before_exam,
            "status": "running",
        }
        _write_report(report_path, report)

        try:
            automator = EKHNPAutomator(settings=settings, log_fn=_print)
            run_result = automator.login_and_complete_first_course_lesson(
                stop_rule="manual",
                manual_lesson_limit=lessons_per_cycle,
                safety_max_lessons=max(lessons_per_cycle + 2, 6),
            )
            _print(f"[Cycle {cycle}] 차시 진행 결과: success={run_result.success} msg={run_result.message}")
            report["lesson_run"] = {
                "success": run_result.success,
                "message": run_result.message,
                "url": run_result.current_url,
            }
            _write_report(report_path, report)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            _print(f"[Cycle {cycle}] 차시 진행 예외: {exc}")
            _print(tb)
            report["status"] = "error"
            report["error"] = str(exc)
            report["traceback"] = tb
            _write_report(report_path, report)
            return 3

        try:
            status_result = EKHNPAutomator(settings=settings, log_fn=_print).login_and_check_learning_progress()
            _print(
                f"[Cycle {cycle}] 진도 상태 확인: success={status_result.success} msg={status_result.message}"
            )
            report["progress_check"] = {
                "success": status_result.success,
                "message": status_result.message,
                "url": status_result.current_url,
            }
            _write_report(report_path, report)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            _print(f"[Cycle {cycle}] 상태 확인 예외: {exc}")
            _print(tb)
            report["status"] = "error"
            report["error"] = str(exc)
            report["traceback"] = tb
            _write_report(report_path, report)
            return 4

        pct, req, inc = _extract_progress_triplet(status_result.message)
        if pct is not None:
            last_percent = pct
            _print(f"[Cycle {cycle}] 현재 진도율 추정: {pct}%")
            report["last_percent"] = last_percent
            report["required_percent"] = req
            report["incomplete_count"] = inc
            _write_report(report_path, report)

        if pct is not None and pct >= target_percent:
            _print(f"[Cycle {cycle}] 목표 진도율 도달({pct}%). 종합평가 응시 준비 단계로 이동합니다.")
            llm_ready = True
            llm_messages: list[str] = []

            if require_llm_before_exam:
                ollama_ok, ollama_msg = _check_ollama_ready(settings.ollama_base_url)
                llm_messages.append(ollama_msg)
                if not ollama_ok:
                    llm_ready = False
                else:
                    idx_ok, idx_msg = _build_rag_if_needed(settings)
                    llm_messages.append(idx_msg)
                    if not idx_ok:
                        llm_ready = False

            for msg in llm_messages:
                _print(f"[Cycle {cycle}] LLM 준비: {msg}")
            report["llm_ready"] = llm_ready
            report["llm_messages"] = llm_messages
            _write_report(report_path, report)

            if not llm_ready:
                _print(f"[Cycle {cycle}] LLM 준비 미완료로 응시는 보류합니다.")
            else:
                try:
                    exam_attempted = True
                    exam_result = EKHNPAutomator(settings=settings, log_fn=_print).login_and_solve_exam_with_rag(
                        max_questions=60,
                        rag_top_k=settings.rag_top_k,
                        confidence_threshold=settings.rag_conf_threshold,
                    )
                    _print(
                        f"[Cycle {cycle}] 종합평가 LLM 응시 결과: success={exam_result.success} msg={exam_result.message}"
                    )
                    report["exam_attempted"] = exam_attempted
                    report["exam_result"] = {
                        "success": exam_result.success,
                        "message": exam_result.message,
                        "url": exam_result.current_url,
                    }
                    if exam_result.success:
                        report["status"] = "done"
                        _write_report(report_path, report)
                        return 0
                    _write_report(report_path, report)
                except Exception as exc:  # noqa: BLE001
                    tb = traceback.format_exc()
                    _print(f"[Cycle {cycle}] 종합평가 응시 예외: {exc}")
                    _print(tb)
                    report["exam_attempted"] = exam_attempted
                    report["exam_error"] = str(exc)
                    report["exam_traceback"] = tb
                    _write_report(report_path, report)

        if cycle < max_cycles and sleep_seconds > 0:
            _print(f"[Cycle {cycle}] 다음 사이클까지 {sleep_seconds}초 대기")
            time.sleep(sleep_seconds)

    if last_percent is not None:
        _print(f"종료: 최대 사이클 도달(마지막 진도율 추정 {last_percent}%)")
    else:
        _print("종료: 최대 사이클 도달(진도율 판독값 없음)")
    _write_report(
        report_path,
        {
            "updated_at": _now(),
            "status": "max_cycles_reached",
            "target_percent": target_percent,
            "max_cycles": max_cycles,
            "last_percent": last_percent,
            "exam_attempted": exam_attempted,
        },
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
