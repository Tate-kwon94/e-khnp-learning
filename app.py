from datetime import datetime
from pathlib import Path

import streamlit as st

from automation import EKHNPAutomator
from config import Settings


LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def append_log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    st.session_state.logs.append(line)
    logfile = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    with logfile.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> None:
    st.set_page_config(page_title="e-KHNP Automation", layout="wide")
    st.title("e-KHNP Automation (M5 Workflow Prototype)")

    if "logs" not in st.session_state:
        st.session_state.logs = []

    settings = Settings()

    st.subheader("마일스톤 진행 현황")
    milestone_cols = st.columns(5)
    with milestone_cols[0]:
        st.metric(
            "M1 프로젝트 골격",
            "100%",
            help="기본 파일 구조, 설정 로딩, Streamlit 실행 뼈대 완료",
        )
    with milestone_cols[1]:
        st.metric(
            "M2 로그인 자동화",
            "98%",
            help="로그인 선택자 보정 및 성공/실패 판정 로직 완료",
        )
    with milestone_cols[2]:
        st.metric(
            "M3 학습현황·첫과목 진입",
            "96%",
            help="나의 학습현황 이동 + 수강과정 첫 행 학습하기 클릭/강의실 진입 확인",
        )
    with milestone_cols[3]:
        st.metric(
            "M4 강의 재생·완료처리",
            "94%",
            help="차시 진행률 판독 + 파란색 완료 확인 + 우하단 Next로 다음 차시 반복 처리",
        )
    with milestone_cols[4]:
        st.metric(
            "M5 수료 순서 자동화",
            "90%",
            help="원클릭 실행 + 진도율→시험평가→학습시간 보충 + 1차시 직접진입/동적시간체크 + 시험없음/응시보존/정답지 인덱싱 재응시",
        )
    st.progress(0.97, text="전체 진행률 97%")

    st.subheader("로그인 정보 입력")
    input_col1, input_col2 = st.columns(2)
    with input_col1:
        user_id_input = st.text_input(
            "아이디",
            value=settings.user_id,
            placeholder="사번 또는 아이디",
            autocomplete="username",
        )
    with input_col2:
        user_password_input = st.text_input(
            "비밀번호",
            value=settings.user_password,
            type="password",
            placeholder="비밀번호",
            autocomplete="current-password",
        )
    st.caption("입력값은 현재 실행 세션에서만 사용됩니다. 로그에는 비밀번호를 저장하지 않습니다.")

    show_browser = st.checkbox(
        "브라우저 창 보기 (디버그용)",
        value=not settings.headless,
        help="체크하면 실제 브라우저 창이 열립니다.",
    )
    stop_mode_label = st.radio(
        "차시 반복 종료 기준",
        [
            "자동(총 차시 감지 우선, 실패 시 Next 버튼 기준)",
            "Next 버튼이 없을 때까지",
            "직접 차시 수 입력",
        ],
        index=0,
    )
    manual_lesson_limit = None
    if stop_mode_label == "직접 차시 수 입력":
        manual_lesson_limit = int(
            st.number_input("반복할 차시 수", min_value=1, max_value=200, value=3, step=1)
        )
    exam_probe_limit = int(
        st.number_input("종합평가 탐침 최대 문항 수", min_value=1, max_value=60, value=12, step=1)
    )
    rag_docs_dir = st.text_input("RAG 문서 폴더", value=settings.rag_docs_dir)
    rag_index_path = st.text_input("RAG 인덱스 파일", value=settings.rag_index_path)
    rag_embed_model = st.text_input("RAG 임베딩 모델", value=settings.rag_embed_model)
    rag_generate_model = st.text_input("RAG 생성 모델", value=settings.rag_generate_model)
    rag_top_k = int(st.number_input("RAG top-k", min_value=1, max_value=20, value=settings.rag_top_k, step=1))
    rag_conf_threshold = float(
        st.number_input(
            "RAG 신뢰도 임계치",
            min_value=0.0,
            max_value=1.0,
            value=settings.rag_conf_threshold,
            step=0.01,
            format="%.2f",
        )
    )
    rag_web_search_enabled = st.checkbox(
        "웹 검색 강제 참조",
        value=True,
        disabled=True,
        help="문항 풀이 시 웹 검색 결과를 항상 참조합니다. (고정)",
    )
    rag_web_top_n = int(
        st.number_input("웹 검색 상위 결과 수", min_value=1, max_value=8, value=settings.rag_web_top_n, step=1)
    )
    rag_web_timeout_sec = int(
        st.number_input("웹 검색 타임아웃(초)", min_value=3, max_value=20, value=settings.rag_web_timeout_sec, step=1)
    )
    rag_web_weight = float(
        st.number_input(
            "웹 점수 가중치",
            min_value=0.0,
            max_value=0.8,
            value=settings.rag_web_weight,
            step=0.05,
            format="%.2f",
        )
    )
    timefill_check_interval_min = int(
        st.number_input(
            "학습시간 부족 체크 기본 간격(분, 실제 5~10분 동적)",
            min_value=1,
            max_value=60,
            value=10,
            step=1,
        )
    )
    timefill_check_limit = int(
        st.number_input("학습시간 부족 체크 최대 횟수", min_value=1, max_value=72, value=24, step=1)
    )
    completion_max_courses = int(
        st.number_input("수료 자동 최대 과정 수", min_value=1, max_value=40, value=settings.completion_max_courses, step=1)
    )
    exam_answer_bank_path = st.text_input("시험 정답 인덱스 파일", value=settings.exam_answer_bank_path)
    exam_auto_retry_max = int(
        st.number_input("시험 자동 재응시 최대 횟수", min_value=0, max_value=4, value=settings.exam_auto_retry_max, step=1)
    )
    exam_retry_requires_answer_index = st.checkbox(
        "정답 인덱스 없으면 재응시 중단",
        value=settings.exam_retry_requires_answer_index,
        help="점수 미달 시 결과지에서 정답 인덱싱이 되지 않으면 자동 재응시를 중단합니다.",
    )
    one_click_force_reindex = st.checkbox(
        "원클릭 실행 시 RAG 인덱스 강제 재생성",
        value=False,
        help="체크 시 인덱스 파일이 있어도 다시 생성합니다.",
    )

    st.subheader("설정 확인")
    st.write(
        {
            "base_url": settings.base_url,
            "login_url": settings.login_url,
            "headless": settings.headless,
            "timeout_ms": settings.timeout_ms,
            "user_id_set": bool(user_id_input),
            "user_password_set": bool(user_password_input),
            "ollama_base_url": settings.ollama_base_url,
            "rag_docs_dir": rag_docs_dir,
            "rag_index_path": rag_index_path,
            "rag_embed_model": rag_embed_model,
            "rag_generate_model": rag_generate_model,
            "rag_top_k": rag_top_k,
            "rag_conf_threshold": rag_conf_threshold,
            "rag_chunk_size": settings.rag_chunk_size,
            "rag_chunk_overlap": settings.rag_chunk_overlap,
            "rag_min_chunk_chars": settings.rag_min_chunk_chars,
            "rag_max_chunks": settings.rag_max_chunks,
            "rag_storage_limit_gb": settings.rag_storage_limit_gb,
            "rag_prune_old_indexes": settings.rag_prune_old_indexes,
            "rag_pass_score": settings.rag_pass_score,
            "rag_low_conf_floor": settings.rag_low_conf_floor,
            "rag_web_search_enabled": rag_web_search_enabled,
            "rag_web_top_n": rag_web_top_n,
            "rag_web_timeout_sec": rag_web_timeout_sec,
            "rag_web_weight": rag_web_weight,
            "completion_max_courses": completion_max_courses,
            "exam_answer_bank_path": exam_answer_bank_path,
            "exam_auto_retry_max": exam_auto_retry_max,
            "exam_retry_requires_answer_index": exam_retry_requires_answer_index,
        }
    )

    run_one_click = st.button(
        "원클릭 전체 자동 실행 (인덱스 확인 → 수료 자동)",
        type="primary",
        use_container_width=True,
    )

    col1, col2, col3, col4, col5, col6, col7, col8, col9 = st.columns(9)
    with col1:
        run_login = st.button("로그인 테스트 실행", use_container_width=True)
    with col2:
        run_learning_status = st.button("로그인 + 나의 학습현황 이동", use_container_width=True)
    with col3:
        run_first_course = st.button("첫 과목 학습 시작", use_container_width=True)
    with col4:
        run_complete_lesson = st.button("강의 순차 완료(첫 행→다음)", use_container_width=True)
    with col5:
        run_exam_probe = st.button("종합평가 텍스트 탐침", use_container_width=True)
    with col6:
        run_completion_flow = st.button("수료 순서 자동(진도→시험→시간)", use_container_width=True)
    with col7:
        run_rag_index = st.button("RAG 인덱스 생성", use_container_width=True)
    with col8:
        run_exam_rag_solve = st.button("종합평가 LLM 풀이(RAG)", use_container_width=True)
    with col9:
        if st.button("로그 초기화", use_container_width=True):
            st.session_state.logs = []

    if run_one_click:
        append_log("원클릭 전체 자동 실행을 시작합니다. (필요 시 인덱스 생성 → 수료 자동)")
        settings.user_id = user_id_input.strip()
        settings.user_password = user_password_input
        settings.headless = not show_browser
        settings.completion_max_courses = completion_max_courses
        settings.rag_docs_dir = rag_docs_dir.strip()
        settings.rag_index_path = rag_index_path.strip()
        settings.rag_embed_model = rag_embed_model.strip()
        settings.rag_generate_model = rag_generate_model.strip()
        settings.rag_top_k = rag_top_k
        settings.rag_conf_threshold = rag_conf_threshold
        settings.rag_web_search_enabled = rag_web_search_enabled
        settings.rag_web_top_n = rag_web_top_n
        settings.rag_web_timeout_sec = rag_web_timeout_sec
        settings.rag_web_weight = rag_web_weight
        settings.exam_answer_bank_path = exam_answer_bank_path.strip()
        settings.exam_auto_retry_max = exam_auto_retry_max
        settings.exam_retry_requires_answer_index = exam_retry_requires_answer_index

        index_path = Path(settings.rag_index_path) if settings.rag_index_path else None
        need_reindex = bool(one_click_force_reindex)
        if index_path is not None and not index_path.exists():
            need_reindex = True
            append_log(f"RAG 인덱스 파일이 없어 자동 생성합니다: {index_path}")

        one_click_blocked = False
        if need_reindex:
            try:
                from rag_index import build_rag_index

                index_result = build_rag_index(
                    docs_dir=settings.rag_docs_dir,
                    index_path=settings.rag_index_path,
                    embed_model=settings.rag_embed_model,
                    chunk_size=settings.rag_chunk_size,
                    overlap=settings.rag_chunk_overlap,
                    min_chunk_chars=settings.rag_min_chunk_chars,
                    max_chunks=settings.rag_max_chunks,
                    max_total_size_gb=settings.rag_storage_limit_gb,
                    prune_old_indexes=settings.rag_prune_old_indexes,
                    ollama_base_url=settings.ollama_base_url,
                    log_fn=append_log,
                )
                append_log(
                    "원클릭 사전 인덱싱 완료: "
                    f"files={index_result.get('files')} chunks={index_result.get('chunks')} "
                    f"path={index_result.get('index_path')}"
                )
            except Exception as exc:  # noqa: BLE001
                one_click_blocked = True
                st.error(f"원클릭 중 RAG 인덱스 생성 실패: {exc}")
                append_log(f"원클릭 중 RAG 인덱스 생성 실패: {exc}")

        if not one_click_blocked:
            automator = EKHNPAutomator(settings, log_fn=append_log)
            result = automator.login_and_run_completion_workflow(
                check_interval_minutes=timefill_check_interval_min,
                max_timefill_checks=timefill_check_limit,
            )
            if result.success:
                st.success(result.message)
            else:
                st.error(result.message)
            append_log(f"결과: {result.message} / url={result.current_url}")

    if run_login:
        append_log("로그인 테스트를 시작합니다.")
        settings.user_id = user_id_input.strip()
        settings.user_password = user_password_input
        settings.headless = not show_browser
        automator = EKHNPAutomator(settings, log_fn=append_log)
        result = automator.login()
        if result.success:
            st.success(result.message)
        else:
            st.error(result.message)
        append_log(f"결과: {result.message} / url={result.current_url}")

    if run_learning_status:
        append_log("로그인 + 나의 학습현황 이동 테스트를 시작합니다.")
        settings.user_id = user_id_input.strip()
        settings.user_password = user_password_input
        settings.headless = not show_browser
        automator = EKHNPAutomator(settings, log_fn=append_log)
        result = automator.login_and_open_learning_status()
        if result.success:
            st.success(result.message)
        else:
            st.error(result.message)
        append_log(f"결과: {result.message} / url={result.current_url}")

    if run_first_course:
        append_log("로그인 + 나의 학습현황 + 첫 과목 학습 시작 테스트를 시작합니다.")
        settings.user_id = user_id_input.strip()
        settings.user_password = user_password_input
        settings.headless = not show_browser
        automator = EKHNPAutomator(settings, log_fn=append_log)
        result = automator.login_and_enter_first_course()
        if result.success:
            st.success(result.message)
        else:
            st.error(result.message)
        append_log(f"결과: {result.message} / url={result.current_url}")

    if run_complete_lesson:
        append_log("로그인 + 강의 목록 첫 행부터 순차 진입 + 차시 자동 완료를 시작합니다.")
        settings.user_id = user_id_input.strip()
        settings.user_password = user_password_input
        settings.headless = not show_browser
        automator = EKHNPAutomator(settings, log_fn=append_log)
        if stop_mode_label.startswith("자동"):
            stop_rule = "auto"
        elif stop_mode_label.startswith("Next 버튼"):
            stop_rule = "next_only"
        else:
            stop_rule = "manual"
        result = automator.login_and_complete_first_course_lesson(
            stop_rule=stop_rule,
            manual_lesson_limit=manual_lesson_limit,
        )
        if result.success:
            st.success(result.message)
        else:
            st.error(result.message)
        append_log(f"결과: {result.message} / url={result.current_url}")

    if run_exam_probe:
        append_log("로그인 + 첫 과목 강의실 진입 + 종합평가 텍스트 탐침을 시작합니다.")
        settings.user_id = user_id_input.strip()
        settings.user_password = user_password_input
        settings.headless = not show_browser
        automator = EKHNPAutomator(settings, log_fn=append_log)
        result = automator.login_and_probe_comprehensive_exam(max_questions=exam_probe_limit)
        if result.success:
            st.success(result.message)
        else:
            st.error(result.message)
        append_log(f"결과: {result.message} / url={result.current_url}")

    if run_completion_flow:
        append_log("수료 순서 자동 실행을 시작합니다. (진도율 → 시험평가 → 학습시간 보충)")
        settings.user_id = user_id_input.strip()
        settings.user_password = user_password_input
        settings.headless = not show_browser
        settings.completion_max_courses = completion_max_courses
        settings.rag_docs_dir = rag_docs_dir.strip()
        settings.rag_index_path = rag_index_path.strip()
        settings.rag_embed_model = rag_embed_model.strip()
        settings.rag_generate_model = rag_generate_model.strip()
        settings.rag_top_k = rag_top_k
        settings.rag_conf_threshold = rag_conf_threshold
        settings.rag_web_search_enabled = rag_web_search_enabled
        settings.rag_web_top_n = rag_web_top_n
        settings.rag_web_timeout_sec = rag_web_timeout_sec
        settings.rag_web_weight = rag_web_weight
        settings.exam_answer_bank_path = exam_answer_bank_path.strip()
        settings.exam_auto_retry_max = exam_auto_retry_max
        settings.exam_retry_requires_answer_index = exam_retry_requires_answer_index
        automator = EKHNPAutomator(settings, log_fn=append_log)
        result = automator.login_and_run_completion_workflow(
            check_interval_minutes=timefill_check_interval_min,
            max_timefill_checks=timefill_check_limit,
        )
        if result.success:
            st.success(result.message)
        else:
            st.error(result.message)
        append_log(f"결과: {result.message} / url={result.current_url}")

    if run_rag_index:
        append_log("RAG 인덱스 생성을 시작합니다.")
        settings.rag_docs_dir = rag_docs_dir.strip()
        settings.rag_index_path = rag_index_path.strip()
        settings.rag_embed_model = rag_embed_model.strip()
        try:
            from rag_index import build_rag_index

            result = build_rag_index(
                docs_dir=settings.rag_docs_dir,
                index_path=settings.rag_index_path,
                embed_model=settings.rag_embed_model,
                chunk_size=settings.rag_chunk_size,
                overlap=settings.rag_chunk_overlap,
                min_chunk_chars=settings.rag_min_chunk_chars,
                max_chunks=settings.rag_max_chunks,
                max_total_size_gb=settings.rag_storage_limit_gb,
                prune_old_indexes=settings.rag_prune_old_indexes,
                ollama_base_url=settings.ollama_base_url,
                log_fn=append_log,
            )
            st.success(
                f"RAG 인덱스 완료: files={result.get('files')} chunks={result.get('chunks')} path={result.get('index_path')}"
            )
            append_log(
                f"결과: RAG 인덱스 완료 files={result.get('files')} chunks={result.get('chunks')} path={result.get('index_path')}"
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"RAG 인덱스 실패: {exc}")
            append_log(f"결과: RAG 인덱스 실패: {exc}")

    if run_exam_rag_solve:
        append_log("종합평가 LLM 풀이(RAG) 실행을 시작합니다.")
        settings.user_id = user_id_input.strip()
        settings.user_password = user_password_input
        settings.headless = not show_browser
        settings.rag_docs_dir = rag_docs_dir.strip()
        settings.rag_index_path = rag_index_path.strip()
        settings.rag_embed_model = rag_embed_model.strip()
        settings.rag_generate_model = rag_generate_model.strip()
        settings.rag_top_k = rag_top_k
        settings.rag_conf_threshold = rag_conf_threshold
        settings.rag_web_search_enabled = rag_web_search_enabled
        settings.rag_web_top_n = rag_web_top_n
        settings.rag_web_timeout_sec = rag_web_timeout_sec
        settings.rag_web_weight = rag_web_weight
        settings.exam_answer_bank_path = exam_answer_bank_path.strip()
        settings.exam_auto_retry_max = exam_auto_retry_max
        settings.exam_retry_requires_answer_index = exam_retry_requires_answer_index
        automator = EKHNPAutomator(settings, log_fn=append_log)
        result = automator.login_and_solve_exam_with_rag(
            max_questions=60,
            rag_top_k=rag_top_k,
            confidence_threshold=rag_conf_threshold,
        )
        if result.success:
            st.success(result.message)
        else:
            st.error(result.message)
        append_log(f"결과: {result.message} / url={result.current_url}")

    st.subheader("실행 로그")
    st.code("\n".join(st.session_state.logs) if st.session_state.logs else "(아직 로그 없음)")


if __name__ == "__main__":
    main()
