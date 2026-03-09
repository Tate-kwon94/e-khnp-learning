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
    st.title("e-KHNP Automation (M3 Prototype)")

    if "logs" not in st.session_state:
        st.session_state.logs = []

    settings = Settings()

    st.subheader("마일스톤 진행 현황")
    milestone_cols = st.columns(4)
    with milestone_cols[0]:
        st.metric(
            "M1 프로젝트 골격",
            "100%",
            help="기본 파일 구조, 설정 로딩, Streamlit 실행 뼈대 완료",
        )
    with milestone_cols[1]:
        st.metric(
            "M2 로그인 자동화",
            "95%",
            help="로그인 선택자 보정 및 성공/실패 판정 로직 완료",
        )
    with milestone_cols[2]:
        st.metric(
            "M3 학습현황·첫과목 진입",
            "90%",
            help="나의 학습현황 이동 + 수강과정 첫 행 학습하기 클릭/강의실 진입 확인",
        )
    with milestone_cols[3]:
        st.metric(
            "M4 강의 재생·완료처리",
            "78%",
            help="차시 진행률 판독 + 파란색 완료 확인 + 우하단 Next로 다음 차시 반복 처리",
        )
    st.progress(0.88, text="전체 진행률 88%")

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

    st.subheader("설정 확인")
    st.write(
        {
            "base_url": settings.base_url,
            "login_url": settings.login_url,
            "headless": settings.headless,
            "timeout_ms": settings.timeout_ms,
            "user_id_set": bool(user_id_input),
            "user_password_set": bool(user_password_input),
        }
    )

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        run_login = st.button("로그인 테스트 실행", use_container_width=True)
    with col2:
        run_learning_status = st.button("로그인 + 나의 학습현황 이동", use_container_width=True)
    with col3:
        run_first_course = st.button("첫 과목 학습 시작", use_container_width=True)
    with col4:
        run_complete_lesson = st.button("첫 과목 모든 차시 완료(반복)", use_container_width=True)
    with col5:
        if st.button("로그 초기화", use_container_width=True):
            st.session_state.logs = []

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
        append_log("로그인 + 첫 과목 진입 + 모든 차시 반복 자동 진행을 시작합니다.")
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

    st.subheader("실행 로그")
    st.code("\n".join(st.session_state.logs) if st.session_state.logs else "(아직 로그 없음)")


if __name__ == "__main__":
    main()
