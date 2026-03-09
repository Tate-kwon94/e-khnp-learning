from dataclasses import dataclass
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Optional

from playwright.sync_api import Frame
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from config import Settings


LogFn = Optional[Callable[[str], None]]


@dataclass
class LoginResult:
    success: bool
    message: str
    current_url: str = ""
    next_lesson_clicked: bool = False


class EKHNPAutomator:
    def __init__(self, settings: Settings, log_fn: LogFn = None) -> None:
        self.settings = settings
        self.log_fn = log_fn
        self._detected_total_lessons: Optional[int] = None
        self._exam_gate_blocked: bool = False
        self._tesseract_path: Optional[str] = None
        self._ocr_unavailable_logged: bool = False

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    def login(self) -> LoginResult:
        if not self.settings.user_id or not self.settings.user_password:
            return LoginResult(False, "환경변수에 EKHNP_USER_ID / EKHNP_USER_PASSWORD를 설정하세요.")

        self._log("브라우저를 시작합니다.")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.settings.headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.settings.timeout_ms)
            dialog_messages: list[str] = []
            page.on("dialog", lambda dialog: self._handle_dialog(dialog, dialog_messages))

            try:
                page.goto(self.settings.login_url, wait_until="commit")
                self._log(f"로그인 페이지 이동: {self.settings.login_url}")
                if not self._wait_login_form_ready(page):
                    return LoginResult(False, "로그인 폼 로딩 타임아웃", page.url)

                # 사이트 구조가 바뀔 수 있으므로 대표적인 선택자를 순차 시도합니다.
                id_candidates = [
                    "#j_userId",
                    'input[name="j_userId"]',
                    'input[placeholder*="사번 또는 아이디"]',
                    'input[name="id"]',
                    'input[name="userId"]',
                    'input[type="text"]',
                ]
                pw_candidates = [
                    "#j_password",
                    'input[name="j_password"]',
                    'input[placeholder*="비밀번호를 입력해 주세요"]',
                    'input[name="password"]',
                    'input[name="userPw"]',
                    'input[type="password"]',
                ]
                submit_candidates = [
                    "a.btn-login",
                    'a[onclick*="doLogin"]',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("로그인")',
                    'a:has-text("로그인")',
                ]

                id_filled = self._fill_first_visible(page, id_candidates, self.settings.user_id)
                pw_filled = self._fill_first_visible(page, pw_candidates, self.settings.user_password)
                submitted = self._click_first_visible(page, submit_candidates)

                if not id_filled or not pw_filled:
                    return LoginResult(False, "로그인 입력창을 찾지 못했습니다.", page.url)
                if not submitted:
                    now_url = page.url
                    if (
                        "login/process.do" in now_url
                        or "param=success" in now_url
                        or "loginpage.do" not in now_url
                    ):
                        self._log("로그인 버튼 감지는 실패했지만 로그인 처리 URL 변화를 감지했습니다.")
                    else:
                        return LoginResult(False, "로그인 버튼을 찾지 못했습니다.", page.url)

                return self._wait_login_result(page, dialog_messages)
            except PlaywrightTimeoutError:
                return LoginResult(False, "타임아웃이 발생했습니다.", page.url)
            except Exception as exc:  # noqa: BLE001
                return LoginResult(False, f"오류 발생: {exc}", page.url)
            finally:
                context.close()
                browser.close()

    def login_and_open_learning_status(self) -> LoginResult:
        if not self.settings.user_id or not self.settings.user_password:
            return LoginResult(False, "환경변수에 EKHNP_USER_ID / EKHNP_USER_PASSWORD를 설정하세요.")

        self._log("브라우저를 시작합니다.")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.settings.headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.settings.timeout_ms)
            dialog_messages: list[str] = []
            page.on("dialog", lambda dialog: self._handle_dialog(dialog, dialog_messages))

            try:
                page.goto(self.settings.login_url, wait_until="commit")
                self._log(f"로그인 페이지 이동: {self.settings.login_url}")
                if not self._wait_login_form_ready(page):
                    return LoginResult(False, "로그인 폼 로딩 타임아웃", page.url)

                id_candidates = [
                    "#j_userId",
                    'input[name="j_userId"]',
                    'input[placeholder*="사번 또는 아이디"]',
                    'input[name="id"]',
                    'input[name="userId"]',
                    'input[type="text"]',
                ]
                pw_candidates = [
                    "#j_password",
                    'input[name="j_password"]',
                    'input[placeholder*="비밀번호를 입력해 주세요"]',
                    'input[name="password"]',
                    'input[name="userPw"]',
                    'input[type="password"]',
                ]
                submit_candidates = [
                    "a.btn-login",
                    'a[onclick*="doLogin"]',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("로그인")',
                    'a:has-text("로그인")',
                ]

                id_filled = self._fill_first_visible(page, id_candidates, self.settings.user_id)
                pw_filled = self._fill_first_visible(page, pw_candidates, self.settings.user_password)
                submitted = self._click_first_visible(page, submit_candidates)

                if not id_filled or not pw_filled:
                    return LoginResult(False, "로그인 입력창을 찾지 못했습니다.", page.url)
                if not submitted:
                    now_url = page.url
                    if (
                        "login/process.do" in now_url
                        or "param=success" in now_url
                        or "loginpage.do" not in now_url
                    ):
                        self._log("로그인 버튼 감지는 실패했지만 로그인 처리 URL 변화를 감지했습니다.")
                    else:
                        return LoginResult(False, "로그인 버튼을 찾지 못했습니다.", page.url)

                login_result = self._wait_login_result(page, dialog_messages)
                if not login_result.success:
                    return login_result

                nav_result = self._open_learning_status(page)
                return nav_result
            except PlaywrightTimeoutError:
                return LoginResult(False, "타임아웃이 발생했습니다.", page.url)
            except Exception as exc:  # noqa: BLE001
                return LoginResult(False, f"오류 발생: {exc}", page.url)
            finally:
                context.close()
                browser.close()

    def login_and_enter_first_course(self) -> LoginResult:
        if not self.settings.user_id or not self.settings.user_password:
            return LoginResult(False, "환경변수에 EKHNP_USER_ID / EKHNP_USER_PASSWORD를 설정하세요.")

        self._log("브라우저를 시작합니다.")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.settings.headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.settings.timeout_ms)
            dialog_messages: list[str] = []
            page.on("dialog", lambda dialog: self._handle_dialog(dialog, dialog_messages))

            try:
                page.goto(self.settings.login_url, wait_until="commit")
                self._log(f"로그인 페이지 이동: {self.settings.login_url}")
                if not self._wait_login_form_ready(page):
                    return LoginResult(False, "로그인 폼 로딩 타임아웃", page.url)

                id_candidates = [
                    "#j_userId",
                    'input[name="j_userId"]',
                    'input[placeholder*="사번 또는 아이디"]',
                    'input[name="id"]',
                    'input[name="userId"]',
                    'input[type="text"]',
                ]
                pw_candidates = [
                    "#j_password",
                    'input[name="j_password"]',
                    'input[placeholder*="비밀번호를 입력해 주세요"]',
                    'input[name="password"]',
                    'input[name="userPw"]',
                    'input[type="password"]',
                ]
                submit_candidates = [
                    "a.btn-login",
                    'a[onclick*="doLogin"]',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("로그인")',
                    'a:has-text("로그인")',
                ]

                id_filled = self._fill_first_visible(page, id_candidates, self.settings.user_id)
                pw_filled = self._fill_first_visible(page, pw_candidates, self.settings.user_password)
                submitted = self._click_first_visible(page, submit_candidates)

                if not id_filled or not pw_filled:
                    return LoginResult(False, "로그인 입력창을 찾지 못했습니다.", page.url)
                if not submitted:
                    now_url = page.url
                    if (
                        "login/process.do" in now_url
                        or "param=success" in now_url
                        or "loginpage.do" not in now_url
                    ):
                        self._log("로그인 버튼 감지는 실패했지만 로그인 처리 URL 변화를 감지했습니다.")
                    else:
                        return LoginResult(False, "로그인 버튼을 찾지 못했습니다.", page.url)

                login_result = self._wait_login_result(page, dialog_messages)
                if not login_result.success:
                    return login_result

                status_result = self._open_learning_status(page)
                if not status_result.success:
                    return status_result

                return self._enter_first_course(page)
            except PlaywrightTimeoutError:
                return LoginResult(False, "타임아웃이 발생했습니다.", page.url)
            except Exception as exc:  # noqa: BLE001
                return LoginResult(False, f"오류 발생: {exc}", page.url)
            finally:
                context.close()
                browser.close()

    def login_and_probe_comprehensive_exam(self, max_questions: int = 12) -> LoginResult:
        if not self.settings.user_id or not self.settings.user_password:
            return LoginResult(False, "환경변수에 EKHNP_USER_ID / EKHNP_USER_PASSWORD를 설정하세요.")

        safe_max_questions = max(1, min(max_questions, 60))
        self._log("브라우저를 시작합니다.")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.settings.headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.settings.timeout_ms)
            dialog_messages: list[str] = []
            page.on("dialog", lambda dialog: self._handle_dialog(dialog, dialog_messages))

            try:
                page.goto(self.settings.login_url, wait_until="commit")
                self._log(f"로그인 페이지 이동: {self.settings.login_url}")
                if not self._wait_login_form_ready(page):
                    return LoginResult(False, "로그인 폼 로딩 타임아웃", page.url)

                id_candidates = [
                    "#j_userId",
                    'input[name="j_userId"]',
                    'input[placeholder*="사번 또는 아이디"]',
                    'input[name="id"]',
                    'input[name="userId"]',
                    'input[type="text"]',
                ]
                pw_candidates = [
                    "#j_password",
                    'input[name="j_password"]',
                    'input[placeholder*="비밀번호를 입력해 주세요"]',
                    'input[name="password"]',
                    'input[name="userPw"]',
                    'input[type="password"]',
                ]
                submit_candidates = [
                    "a.btn-login",
                    'a[onclick*="doLogin"]',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("로그인")',
                    'a:has-text("로그인")',
                ]

                id_filled = self._fill_first_visible(page, id_candidates, self.settings.user_id)
                pw_filled = self._fill_first_visible(page, pw_candidates, self.settings.user_password)
                submitted = self._click_first_visible(page, submit_candidates)

                if not id_filled or not pw_filled:
                    return LoginResult(False, "로그인 입력창을 찾지 못했습니다.", page.url)
                if not submitted:
                    now_url = page.url
                    if (
                        "login/process.do" in now_url
                        or "param=success" in now_url
                        or "loginpage.do" not in now_url
                    ):
                        self._log("로그인 버튼 감지는 실패했지만 로그인 처리 URL 변화를 감지했습니다.")
                    else:
                        return LoginResult(False, "로그인 버튼을 찾지 못했습니다.", page.url)

                login_result = self._wait_login_result(page, dialog_messages)
                if not login_result.success:
                    return login_result

                status_result = self._open_learning_status(page)
                if not status_result.success:
                    return status_result

                classroom_result, classroom_page = self._open_first_course_classroom_internal(page)
                if not classroom_result.success or classroom_page is None:
                    return classroom_result

                self._refresh_classroom_page(classroom_page)
                progress_status = self._extract_learning_progress_status(classroom_page)
                current_percent = int(progress_status.get("current_percent", 0))
                if 0 < current_percent < 80:
                    return LoginResult(
                        False,
                        f"종합평가 응시 조건 미달: 학습진도율 {current_percent}% (최소 80% 필요)",
                        classroom_page.url,
                    )
                if current_percent >= 80:
                    self._log(f"종합평가 응시 조건 충족: 학습진도율 {current_percent}%")
                else:
                    self._log("학습진도율 수치 판독 실패(0%). 응시 버튼 탐색으로 진행합니다.")

                exam_page = self._open_comprehensive_exam_popup(classroom_page)
                if exam_page is None:
                    return LoginResult(False, "종합평가 응시 팝업을 찾지 못했습니다.", classroom_page.url)

                probe = self._probe_exam_question_stream(exam_page, max_questions=safe_max_questions)
                dom_ok = probe.get("dom_readable_count", 0)
                visited = probe.get("visited_count", 0)
                ocr_needed = max(0, visited - dom_ok)
                total_hint = probe.get("total_hint")

                total_hint_msg = f" / total_hint={total_hint}" if isinstance(total_hint, int) and total_hint > 0 else ""
                return LoginResult(
                    True,
                    f"종합평가 텍스트 탐침 완료: DOM판독 {dom_ok}/{visited}, OCR필요추정 {ocr_needed}{total_hint_msg}",
                    exam_page.url,
                )
            except PlaywrightTimeoutError:
                return LoginResult(False, "타임아웃이 발생했습니다.", page.url)
            except Exception as exc:  # noqa: BLE001
                return LoginResult(False, f"오류 발생: {exc}", page.url)
            finally:
                context.close()
                browser.close()

    def login_and_solve_exam_with_rag(
        self,
        max_questions: int = 60,
        rag_top_k: Optional[int] = None,
        confidence_threshold: Optional[float] = None,
    ) -> LoginResult:
        if not self.settings.user_id or not self.settings.user_password:
            return LoginResult(False, "환경변수에 EKHNP_USER_ID / EKHNP_USER_PASSWORD를 설정하세요.")

        try:
            from rag_solver import RagExamSolver
        except Exception as exc:  # noqa: BLE001
            return LoginResult(False, f"RAG 솔버 로딩 실패: {exc}")

        top_k = rag_top_k if rag_top_k is not None else self.settings.rag_top_k
        conf_th = confidence_threshold if confidence_threshold is not None else self.settings.rag_conf_threshold
        safe_max_questions = max(1, min(max_questions, 120))
        solver = RagExamSolver(
            index_path=self.settings.rag_index_path,
            generate_model=self.settings.rag_generate_model,
            embed_model=self.settings.rag_embed_model,
            ollama_base_url=self.settings.ollama_base_url,
        )

        self._log("브라우저를 시작합니다.")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.settings.headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.settings.timeout_ms)
            dialog_messages: list[str] = []
            page.on("dialog", lambda dialog: self._handle_dialog(dialog, dialog_messages))

            try:
                page.goto(self.settings.login_url, wait_until="commit")
                self._log(f"로그인 페이지 이동: {self.settings.login_url}")
                if not self._wait_login_form_ready(page):
                    return LoginResult(False, "로그인 폼 로딩 타임아웃", page.url)

                id_candidates = [
                    "#j_userId",
                    'input[name="j_userId"]',
                    'input[placeholder*="사번 또는 아이디"]',
                    'input[name="id"]',
                    'input[name="userId"]',
                    'input[type="text"]',
                ]
                pw_candidates = [
                    "#j_password",
                    'input[name="j_password"]',
                    'input[placeholder*="비밀번호를 입력해 주세요"]',
                    'input[name="password"]',
                    'input[name="userPw"]',
                    'input[type="password"]',
                ]
                submit_candidates = [
                    "a.btn-login",
                    'a[onclick*="doLogin"]',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("로그인")',
                    'a:has-text("로그인")',
                ]

                id_filled = self._fill_first_visible(page, id_candidates, self.settings.user_id)
                pw_filled = self._fill_first_visible(page, pw_candidates, self.settings.user_password)
                submitted = self._click_first_visible(page, submit_candidates)

                if not id_filled or not pw_filled:
                    return LoginResult(False, "로그인 입력창을 찾지 못했습니다.", page.url)
                if not submitted:
                    now_url = page.url
                    if (
                        "login/process.do" in now_url
                        or "param=success" in now_url
                        or "loginpage.do" not in now_url
                    ):
                        self._log("로그인 버튼 감지는 실패했지만 로그인 처리 URL 변화를 감지했습니다.")
                    else:
                        return LoginResult(False, "로그인 버튼을 찾지 못했습니다.", page.url)

                login_result = self._wait_login_result(page, dialog_messages)
                if not login_result.success:
                    return login_result

                status_result = self._open_learning_status(page)
                if not status_result.success:
                    return status_result

                classroom_result, classroom_page = self._open_first_course_classroom_internal(page)
                if not classroom_result.success or classroom_page is None:
                    return classroom_result

                self._refresh_classroom_page(classroom_page)
                progress = self._extract_learning_progress_status(classroom_page)
                cur_pct = int(progress.get("current_percent", 0))
                if 0 < cur_pct < 80:
                    return LoginResult(
                        False,
                        f"종합평가 응시 조건 미달: 학습진도율 {cur_pct}% (최소 80% 필요)",
                        classroom_page.url,
                    )

                exam_page = self._open_comprehensive_exam_popup(classroom_page)
                if exam_page is None and self._exam_gate_blocked:
                    return LoginResult(
                        False,
                        "종합평가 응시 제한(학습진도율 80%) 팝업 감지. 먼저 미완료 차시를 진행해 주세요.",
                        classroom_page.url,
                    )
                if exam_page is None:
                    return LoginResult(False, "종합평가 응시 팝업을 찾지 못했습니다.", classroom_page.url)

                solve_result = self._solve_exam_stream_with_rag(
                    exam_page=exam_page,
                    solver=solver,
                    max_questions=safe_max_questions,
                    top_k=max(1, int(top_k)),
                    confidence_threshold=float(conf_th),
                )
                if not solve_result.get("success"):
                    return LoginResult(False, str(solve_result.get("message", "시험 자동풀이 실패")), exam_page.url)

                if not self._wait_exam_finished(exam_page, timeout_ms=2 * 60 * 1000):
                    return LoginResult(False, "시험 자동풀이 후 완료 화면을 확인하지 못했습니다.", exam_page.url)

                return LoginResult(
                    True,
                    f"종합평가 자동풀이 완료: solved={solve_result.get('solved', 0)}",
                    exam_page.url,
                )
            except PlaywrightTimeoutError:
                return LoginResult(False, "타임아웃이 발생했습니다.", page.url)
            except Exception as exc:  # noqa: BLE001
                return LoginResult(False, f"오류 발생: {exc}", page.url)
            finally:
                context.close()
                browser.close()

    def login_and_run_completion_workflow(
        self,
        check_interval_minutes: int = 10,
        max_timefill_checks: int = 24,
        safety_max_lessons: int = 80,
    ) -> LoginResult:
        if not self.settings.user_id or not self.settings.user_password:
            return LoginResult(False, "환경변수에 EKHNP_USER_ID / EKHNP_USER_PASSWORD를 설정하세요.")

        interval_ms = max(1, check_interval_minutes) * 60 * 1000
        check_limit = max(1, min(max_timefill_checks, 72))

        self._log("브라우저를 시작합니다.")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.settings.headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.settings.timeout_ms)
            dialog_messages: list[str] = []
            page.on("dialog", lambda dialog: self._handle_dialog(dialog, dialog_messages))

            try:
                page.goto(self.settings.login_url, wait_until="commit")
                self._log(f"로그인 페이지 이동: {self.settings.login_url}")
                if not self._wait_login_form_ready(page):
                    return LoginResult(False, "로그인 폼 로딩 타임아웃", page.url)

                id_candidates = [
                    "#j_userId",
                    'input[name="j_userId"]',
                    'input[placeholder*="사번 또는 아이디"]',
                    'input[name="id"]',
                    'input[name="userId"]',
                    'input[type="text"]',
                ]
                pw_candidates = [
                    "#j_password",
                    'input[name="j_password"]',
                    'input[placeholder*="비밀번호를 입력해 주세요"]',
                    'input[name="password"]',
                    'input[name="userPw"]',
                    'input[type="password"]',
                ]
                submit_candidates = [
                    "a.btn-login",
                    'a[onclick*="doLogin"]',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("로그인")',
                    'a:has-text("로그인")',
                ]

                id_filled = self._fill_first_visible(page, id_candidates, self.settings.user_id)
                pw_filled = self._fill_first_visible(page, pw_candidates, self.settings.user_password)
                submitted = self._click_first_visible(page, submit_candidates)

                if not id_filled or not pw_filled:
                    return LoginResult(False, "로그인 입력창을 찾지 못했습니다.", page.url)
                if not submitted:
                    now_url = page.url
                    if (
                        "login/process.do" in now_url
                        or "param=success" in now_url
                        or "loginpage.do" not in now_url
                    ):
                        self._log("로그인 버튼 감지는 실패했지만 로그인 처리 URL 변화를 감지했습니다.")
                    else:
                        return LoginResult(False, "로그인 버튼을 찾지 못했습니다.", page.url)

                login_result = self._wait_login_result(page, dialog_messages)
                if not login_result.success:
                    return login_result

                status_result = self._open_learning_status(page)
                if not status_result.success:
                    return status_result

                classroom_result, classroom_page = self._open_first_course_classroom_internal(page)
                if not classroom_result.success or classroom_page is None:
                    return classroom_result

                # 1) 학습진도율 수료기준 도달 전까지 차시 진행
                self._log("1단계: 학습하기를 통해 학습진도율을 수료기준까지 올립니다.")
                self._refresh_classroom_page(classroom_page)
                initial_progress = self._extract_learning_progress_status(classroom_page)
                if initial_progress.get("incomplete_count", 0) > 0:
                    self._log("미완료 차시가 존재하여 우선 '미완료 학습하기'로 진입합니다.")
                    learning_page = self._open_incomplete_lesson_popup(classroom_page)
                else:
                    learning_page = self._start_learning_from_progress_panel(classroom_page)
                if learning_page is None:
                    return LoginResult(False, "학습창을 열지 못해 진도율 단계 시작 실패", classroom_page.url)

                completed_lessons = 0
                recovery_attempts = 0
                while completed_lessons < safety_max_lessons:
                    complete_result = self._complete_lesson_steps(learning_page)
                    if not complete_result.success:
                        if self._is_recoverable_lesson_failure(complete_result.message) and recovery_attempts < 3:
                            recovery_attempts += 1
                            self._log(f"진도율 단계 복구 시도: 팝업 재시작 {recovery_attempts}/3")
                            recovered_page = self._recover_learning_popup(
                                current_learning_page=learning_page,
                                classroom_page=classroom_page,
                                context_pages=page.context.pages,
                            )
                            if recovered_page is not None:
                                learning_page = recovered_page
                                continue
                        return LoginResult(
                            False,
                            f"진도율 단계 중단: {complete_result.message}",
                            complete_result.current_url,
                        )
                    recovery_attempts = 0
                    completed_lessons += 1
                    self._log(f"진도율 단계 누적 완료 차시: {completed_lessons}")
                    if not complete_result.next_lesson_clicked:
                        break
                    learning_page.wait_for_timeout(2200)

                self._refresh_classroom_page(classroom_page)
                progress_status = self._extract_learning_progress_status(classroom_page)
                if not progress_status["progress_ok"] or progress_status["incomplete_count"] > 0:
                    self._log("학습진도율 기준 미충족 또는 미완료 차시 감지: 보완 학습을 시도합니다.")
                    for retry in range(min(safety_max_lessons, 40)):
                        if progress_status["progress_ok"] and progress_status["incomplete_count"] <= 0:
                            break
                        self._log(f"미완료 차시 보완 시도 {retry + 1}")
                        extra_page = self._open_incomplete_lesson_popup(classroom_page)
                        if extra_page is None:
                            break
                        extra_result = self._complete_lesson_steps(extra_page)
                        if not extra_result.success:
                            if self._is_recoverable_lesson_failure(extra_result.message):
                                self._log("미완료 차시 보완 중 정체 감지: 팝업 재시작 후 재시도합니다.")
                                recovered_page = self._recover_learning_popup(
                                    current_learning_page=extra_page,
                                    classroom_page=classroom_page,
                                    context_pages=page.context.pages,
                                )
                                if recovered_page is not None:
                                    continue
                            return LoginResult(
                                False,
                                f"미완료 차시 보완 실패: {extra_result.message}",
                                extra_result.current_url,
                            )
                        self._refresh_classroom_page(classroom_page)
                        progress_status = self._extract_learning_progress_status(classroom_page)

                    if not progress_status["progress_ok"] or progress_status["incomplete_count"] > 0:
                        return LoginResult(
                            False,
                            "학습진도율 수료기준 미충족 또는 미완료 차시가 남아 있어 중단합니다.",
                            classroom_page.url,
                        )

                # 2) 시험평가
                self._log("2단계: 종합평가 응시를 진행합니다.")
                exam_gate = self._extract_learning_progress_status(classroom_page)
                exam_gate_percent = int(exam_gate.get("current_percent", 0))
                if 0 < exam_gate_percent < 80:
                    self._log(
                        f"종합평가 응시 기준 미달 상태 감지: {exam_gate_percent}% (80% 이상 필요, 미완료 차시 보완 진행)"
                    )
                if exam_gate_percent >= 80:
                    self._log(f"종합평가 응시 조건 충족: 학습진도율 {exam_gate_percent}%")
                else:
                    self._log("학습진도율 수치 판독 실패(0%). 응시 버튼 탐색으로 진행합니다.")
                exam_page = self._open_comprehensive_exam_popup(classroom_page)
                if exam_page is None and self._exam_gate_blocked:
                    self._log("응시 제한 팝업 확인: 미완료 차시를 진행한 뒤 종합평가를 재시도합니다.")
                    for retry in range(min(safety_max_lessons, 40)):
                        self._refresh_classroom_page(classroom_page)
                        exam_gate = self._extract_learning_progress_status(classroom_page)
                        exam_gate_percent = int(exam_gate.get("current_percent", 0))
                        if exam_gate_percent >= 80 and exam_gate["incomplete_count"] <= 0:
                            break

                        self._log(f"응시 조건 보완(미완료 차시) {retry + 1}")
                        extra_page = self._open_incomplete_lesson_popup(classroom_page)
                        if extra_page is None:
                            break
                        extra_result = self._complete_lesson_steps(extra_page)
                        if not extra_result.success:
                            if self._is_recoverable_lesson_failure(extra_result.message):
                                self._log("응시 조건 보완 중 정체 감지: 팝업 재시작 후 재시도합니다.")
                                recovered_page = self._recover_learning_popup(
                                    current_learning_page=extra_page,
                                    classroom_page=classroom_page,
                                    context_pages=page.context.pages,
                                )
                                if recovered_page is not None:
                                    continue
                            return LoginResult(
                                False,
                                f"응시 조건 보완 실패: {extra_result.message}",
                                extra_result.current_url,
                            )

                    self._refresh_classroom_page(classroom_page)
                    exam_gate = self._extract_learning_progress_status(classroom_page)
                    exam_gate_percent = int(exam_gate.get("current_percent", 0))
                    if 0 < exam_gate_percent < 80:
                        return LoginResult(
                            False,
                            f"종합평가 응시 조건 미달: 학습진도율 {exam_gate_percent}% (최소 80% 필요)",
                            classroom_page.url,
                        )
                    exam_page = self._open_comprehensive_exam_popup(classroom_page)
                if exam_page is None:
                    return LoginResult(False, "종합평가 응시 팝업을 찾지 못했습니다.", classroom_page.url)

                self._log("시험평가 완료를 대기합니다. (수동 풀이 또는 별도 자동풀이 가능)")
                if not self._wait_exam_finished(exam_page, timeout_ms=60 * 60 * 1000):
                    return LoginResult(False, "시험평가 완료를 확인하지 못했습니다.", exam_page.url)
                self._log("시험평가 완료 신호를 감지했습니다.")

                # 3) 학습시간 부족 시 1차시 재생 유지 + 10분 간격 체크
                self._refresh_classroom_page(classroom_page)
                time_status = self._extract_study_time_status(classroom_page)
                progress_status = self._extract_learning_progress_status(classroom_page)
                if not progress_status["progress_ok"] or progress_status["incomplete_count"] > 0:
                    return LoginResult(
                        False,
                        "시험평가 후 학습진도율 기준 미충족(미완료 차시 존재 포함)으로 중단합니다.",
                        classroom_page.url,
                    )
                if time_status["requirement_known"] and time_status["shortage_seconds"] <= 0:
                    return LoginResult(
                        True,
                        "수료 시나리오 완료: 진도율/시험평가 이후 학습시간도 수료기준 충족",
                        classroom_page.url,
                    )

                self._log("3단계: 학습시간이 부족해 1차시 재생 유지 모드로 진입합니다.")
                keepalive_page = self._open_first_lesson_popup_for_timefill(classroom_page)
                if keepalive_page is None:
                    return LoginResult(False, "학습시간 보충용 1차시 학습창을 열지 못했습니다.", classroom_page.url)

                for idx in range(check_limit):
                    self._log(f"학습시간 보충 대기: {idx + 1}/{check_limit} (다음 확인 {check_interval_minutes}분 후)")
                    keepalive_page.wait_for_timeout(interval_ms)
                    self._refresh_classroom_page(classroom_page)
                    time_status = self._extract_study_time_status(classroom_page)
                    progress_status = self._extract_learning_progress_status(classroom_page)
                    if (
                        progress_status["progress_ok"]
                        and progress_status["incomplete_count"] <= 0
                        and time_status["requirement_known"]
                        and time_status["shortage_seconds"] <= 0
                    ):
                        return LoginResult(
                            True,
                            "수료 시나리오 완료: 학습시간 부족분이 충족되었습니다.",
                            classroom_page.url,
                        )

                return LoginResult(
                    False,
                    "학습시간 보충 체크 제한 횟수에 도달했습니다. (기준 충족 미확인)",
                    classroom_page.url,
                )
            except PlaywrightTimeoutError:
                return LoginResult(False, "타임아웃이 발생했습니다.", page.url)
            except Exception as exc:  # noqa: BLE001
                return LoginResult(False, f"오류 발생: {exc}", page.url)
            finally:
                context.close()
                browser.close()

    def login_and_complete_first_course_lesson(
        self,
        stop_rule: str = "auto",
        manual_lesson_limit: Optional[int] = None,
        safety_max_lessons: int = 80,
    ) -> LoginResult:
        self._detected_total_lessons = None
        if not self.settings.user_id or not self.settings.user_password:
            return LoginResult(False, "환경변수에 EKHNP_USER_ID / EKHNP_USER_PASSWORD를 설정하세요.")

        self._log("브라우저를 시작합니다.")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.settings.headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.settings.timeout_ms)
            dialog_messages: list[str] = []
            page.on("dialog", lambda dialog: self._handle_dialog(dialog, dialog_messages))

            try:
                page.goto(self.settings.login_url, wait_until="commit")
                self._log(f"로그인 페이지 이동: {self.settings.login_url}")
                if not self._wait_login_form_ready(page):
                    return LoginResult(False, "로그인 폼 로딩 타임아웃", page.url)

                id_candidates = [
                    "#j_userId",
                    'input[name="j_userId"]',
                    'input[placeholder*="사번 또는 아이디"]',
                    'input[name="id"]',
                    'input[name="userId"]',
                    'input[type="text"]',
                ]
                pw_candidates = [
                    "#j_password",
                    'input[name="j_password"]',
                    'input[placeholder*="비밀번호를 입력해 주세요"]',
                    'input[name="password"]',
                    'input[name="userPw"]',
                    'input[type="password"]',
                ]
                submit_candidates = [
                    "a.btn-login",
                    'a[onclick*="doLogin"]',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("로그인")',
                    'a:has-text("로그인")',
                ]

                id_filled = self._fill_first_visible(page, id_candidates, self.settings.user_id)
                pw_filled = self._fill_first_visible(page, pw_candidates, self.settings.user_password)
                submitted = self._click_first_visible(page, submit_candidates)

                if not id_filled or not pw_filled:
                    return LoginResult(False, "로그인 입력창을 찾지 못했습니다.", page.url)
                if not submitted:
                    now_url = page.url
                    if (
                        "login/process.do" in now_url
                        or "param=success" in now_url
                        or "loginpage.do" not in now_url
                    ):
                        self._log("로그인 버튼 감지는 실패했지만 로그인 처리 URL 변화를 감지했습니다.")
                    else:
                        return LoginResult(False, "로그인 버튼을 찾지 못했습니다.", page.url)

                login_result = self._wait_login_result(page, dialog_messages)
                if not login_result.success:
                    return login_result

                status_result = self._open_learning_status(page)
                if not status_result.success:
                    return status_result

                enter_result, lesson_page = self._enter_first_course_internal(page)
                if not enter_result.success or lesson_page is None:
                    return enter_result

                completed_lessons = 0
                detected_total_lessons: Optional[int] = self._detected_total_lessons
                if detected_total_lessons:
                    self._log(f"총 차시 수(강의실 학습하기 버튼 기준): {detected_total_lessons}차시")
                recovery_attempts = 0

                while completed_lessons < safety_max_lessons:
                    if stop_rule in {"auto", "detected_total"} and detected_total_lessons is None:
                        detected_total_lessons = self._extract_total_lessons(lesson_page)
                        if detected_total_lessons:
                            self._log(f"총 차시 수 감지: {detected_total_lessons}차시")
                        elif stop_rule == "detected_total":
                            return LoginResult(
                                False,
                                "총 차시 수를 감지하지 못했습니다. 'Next 버튼 기준' 모드로 실행해 주세요.",
                                lesson_page.url,
                            )

                    if stop_rule == "manual" and manual_lesson_limit and completed_lessons >= manual_lesson_limit:
                        return LoginResult(
                            True,
                            f"요청한 반복 수 완료: 총 {completed_lessons}개 차시 완료",
                            lesson_page.url,
                        )

                    if (
                        stop_rule in {"auto", "detected_total"}
                        and detected_total_lessons
                        and completed_lessons >= detected_total_lessons
                    ):
                        return LoginResult(
                            True,
                            f"감지된 총 차시 완료: 총 {completed_lessons}/{detected_total_lessons} 차시 완료",
                            lesson_page.url,
                        )

                    complete_result = self._complete_lesson_steps(lesson_page)
                    if not complete_result.success:
                        if self._is_recoverable_lesson_failure(complete_result.message) and recovery_attempts < 3:
                            recovery_attempts += 1
                            self._log(f"차시 정체 감지: 팝업 재시작 복구 {recovery_attempts}/3")
                            recovered_page = self._recover_learning_popup(
                                current_learning_page=lesson_page,
                                classroom_page=None,
                                context_pages=page.context.pages,
                            )
                            if recovered_page is not None:
                                lesson_page = recovered_page
                                continue
                        if completed_lessons > 0:
                            return LoginResult(
                                False,
                                f"{completed_lessons}개 차시 완료 후 중단: {complete_result.message}",
                                complete_result.current_url,
                            )
                        return complete_result

                    recovery_attempts = 0
                    completed_lessons += 1
                    self._log(f"차시 완료 누적: {completed_lessons}")

                    if not complete_result.next_lesson_clicked:
                        return LoginResult(
                            True,
                            f"차시 자동 진행 완료: 총 {completed_lessons}개 차시 완료",
                            complete_result.current_url,
                        )

                    self._log("다음 차시로 이동, 로딩 대기")
                    lesson_page.wait_for_timeout(2500)

                return LoginResult(
                    False,
                    f"안전 제한({safety_max_lessons})에 도달해 중단",
                    lesson_page.url,
                )
            except PlaywrightTimeoutError:
                return LoginResult(False, "타임아웃이 발생했습니다.", page.url)
            except Exception as exc:  # noqa: BLE001
                return LoginResult(False, f"오류 발생: {exc}", page.url)
            finally:
                context.close()
                browser.close()

    def _extract_total_lessons(self, page: Page) -> Optional[int]:
        scopes: list[Any] = [page] + list(page.frames)
        patterns = [
            r"(?:차시|lesson)\s*(\d{1,3})\s*/\s*(\d{1,3})",
            r"(\d{1,3})\s*/\s*(\d{1,3})\s*(?:차시|lesson)",
            r"총\s*(\d{1,3})\s*차시",
        ]

        for scope in scopes:
            try:
                text = scope.locator("body").inner_text(timeout=1500)
            except Exception:  # noqa: BLE001
                continue

            for pat in patterns[:2]:
                matches = re.findall(pat, text, flags=re.IGNORECASE)
                for cur, total in matches:
                    try:
                        cur_i = int(cur)
                        total_i = int(total)
                        if total_i >= 1 and 1 <= cur_i <= total_i:
                            return total_i
                    except ValueError:
                        continue

            total_matches = re.findall(patterns[2], text, flags=re.IGNORECASE)
            for total in total_matches:
                try:
                    total_i = int(total)
                    if total_i >= 1:
                        return total_i
                except ValueError:
                    continue
        return None

    def _extract_total_lessons_from_classroom_buttons(self, page: Page) -> Optional[int]:
        try:
            stats = page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                      const r = el.getBoundingClientRect();
                      return r.width > 0 && r.height > 0;
                    };
                    const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                    const getLabel = (el) => normalize(el.textContent || el.value || '');
                    const isLessonBtn = (el) => {
                      const txt = getLabel(el);
                      return txt === '학습하기' || txt === '이어 학습하기' || txt === '이어학습하기';
                    };
                    const toInt = (v) => (Number.isFinite(v) ? Math.trunc(v) : 0);
                    const extractTotals = (text) => {
                      const out = [];
                      if (!text) return out;
                      const totalPat = /총\\s*(\\d{1,3})\\s*차시/gi;
                      let m;
                      while ((m = totalPat.exec(text)) !== null) {
                        out.push(toInt(parseInt(m[1], 10)));
                      }
                      const ratioPat = /(\\d{1,3})\\s*\\/\\s*(\\d{1,3})/g;
                      while ((m = ratioPat.exec(text)) !== null) {
                        const cur = toInt(parseInt(m[1], 10));
                        const tot = toInt(parseInt(m[2], 10));
                        if (cur >= 1 && tot >= cur) out.push(tot);
                      }
                      return out.filter((n) => n >= 1 && n <= 400);
                    };

                    const result = {
                      scopedLessonSectionLessonNoCount: 0,
                      scopedLessonSectionNumberedCardCount: 0,
                      scopedLessonSectionButtonRowCount: 0,
                      scopedLessonSectionButtonCountMinPositive: 0,
                      scopedLessonNoCount: 0,
                      scopedButtonRowCount: 0,
                      globalButtonCountRaw: 0,
                      globalButtonCountAdjusted: 0,
                      explicitTotalFromText: 0,
                      explicitTotalFromScripts: 0,
                    };

                    // A) 명시적 총 차시 텍스트를 우선 탐지 (신뢰도 높음)
                    const bodyText = normalize(document.body && document.body.innerText);
                    const textTotals = extractTotals(bodyText);
                    if (textTotals.length) {
                      result.explicitTotalFromText = Math.max(...textTotals);
                    }

                    // B) 스크립트 내 총 차시 힌트 탐지 (신뢰도 중상)
                    const scriptTotals = [];
                    for (const s of Array.from(document.querySelectorAll('script'))) {
                      const txt = s.textContent || '';
                      scriptTotals.push(...extractTotals(txt));
                    }
                    if (scriptTotals.length) {
                      result.explicitTotalFromScripts = Math.max(...scriptTotals);
                    }

                    // C-1) "학습 차시" 섹션 기반 차시 행 탐지 (최우선)
                    const findScopedCounts = (includeFn) => {
                      const titleNodes = Array.from(document.querySelectorAll('div,span,strong,h1,h2,h3,h4'));
                      const scopedCounts = [];
                      for (const title of titleNodes) {
                        const titleText = normalize(title.textContent);
                        if (!includeFn(titleText)) continue;
                        let container = title;
                        for (let depth = 0; depth < 6 && container; depth++) {
                          const rows = Array.from(container.querySelectorAll('tr'));
                          const lessonNos = new Set();
                          const buttonRows = new Set();
                          const numberedCards = new Set();
                          const lessonBtns = Array.from(
                            container.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                          ).filter((el) => isVisible(el) && isLessonBtn(el));

                          for (const btn of lessonBtns) {
                            let node = btn;
                            for (let up = 0; up < 8 && node; up++) {
                              const txt = normalize(node.innerText || node.textContent || '');
                              const m1 = txt.match(/(?:^|\\s)(\\d{1,3})\\s*[\\.|\\)]\\s*\\S+/);
                              const m2 = txt.match(/(\\d{1,3})\\s*차시/);
                              const m = m1 || m2;
                              if (m) {
                                numberedCards.add(m[1]);
                                break;
                              }
                              node = node.parentElement;
                            }
                          }

                          for (const tr of rows) {
                            const rowText = normalize(tr.innerText || tr.textContent || '');
                            const noMatch = rowText.match(/(\\d{1,3})\\s*차시/);
                            if (noMatch) {
                              lessonNos.add(noMatch[1]);
                            }
                            const btn = Array.from(
                              tr.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                            ).find((el) => isVisible(el) && isLessonBtn(el));
                            if (btn) {
                              const rowKey = normalize(tr.innerText || tr.textContent || '');
                              if (rowKey) buttonRows.add(rowKey);
                            }
                          }
                          if (lessonNos.size > 0 || buttonRows.size > 0) {
                            scopedCounts.push({
                              lessonNoCount: lessonNos.size,
                              numberedCardCount: numberedCards.size,
                              buttonRowCount: buttonRows.size,
                              lessonButtonCount: lessonBtns.length,
                            });
                          } else if (numberedCards.size > 0 || lessonBtns.length > 0) {
                            scopedCounts.push({
                              lessonNoCount: 0,
                              numberedCardCount: numberedCards.size,
                              buttonRowCount: 0,
                              lessonButtonCount: lessonBtns.length,
                            });
                          }
                          container = container.parentElement;
                        }
                      }
                      return scopedCounts;
                    };

                    const lessonSectionCounts = findScopedCounts(
                      (titleText) => titleText.includes('학습 차시') || titleText.includes('학습차시')
                    );
                    if (lessonSectionCounts.length) {
                      result.scopedLessonSectionLessonNoCount = Math.max(
                        ...lessonSectionCounts.map((item) => toInt(item.lessonNoCount))
                      );
                      result.scopedLessonSectionNumberedCardCount = Math.max(
                        ...lessonSectionCounts.map((item) => toInt(item.numberedCardCount))
                      );
                      result.scopedLessonSectionButtonRowCount = Math.max(
                        ...lessonSectionCounts.map((item) => toInt(item.buttonRowCount))
                      );
                      const positiveButtonCounts = lessonSectionCounts
                        .map((item) => toInt(item.lessonButtonCount))
                        .filter((n) => n > 0);
                      if (positiveButtonCounts.length) {
                        result.scopedLessonSectionButtonCountMinPositive = Math.min(...positiveButtonCounts);
                      }
                    }

                    // C-2) "학습진행현황" 섹션 기반 차시 행 탐지 (보조)
                    const titleNodes = Array.from(document.querySelectorAll('div,span,strong,h1,h2,h3,h4'));
                    const scopedCounts = [];
                    for (const title of titleNodes) {
                      const titleText = normalize(title.textContent);
                      if (!titleText.includes('학습진행현황')) continue;
                      let container = title;
                      for (let depth = 0; depth < 6 && container; depth++) {
                        const rows = Array.from(container.querySelectorAll('tr'));
                        const lessonNos = new Set();
                        const buttonRows = new Set();
                        for (const tr of rows) {
                          const rowText = normalize(tr.innerText || tr.textContent || '');
                          const noMatch = rowText.match(/(\\d{1,3})\\s*차시/);
                          if (noMatch) {
                            lessonNos.add(noMatch[1]);
                          }
                          const btn = Array.from(
                            tr.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                          ).find((el) => isVisible(el) && isLessonBtn(el));
                          if (btn) {
                            const rowKey = normalize(tr.innerText || tr.textContent || '');
                            if (rowKey) buttonRows.add(rowKey);
                          }
                        }
                        if (lessonNos.size > 0 || buttonRows.size > 0) {
                          scopedCounts.push({
                            lessonNoCount: lessonNos.size,
                            buttonRowCount: buttonRows.size,
                          });
                        }
                        container = container.parentElement;
                      }
                    }
                    if (scopedCounts.length) {
                      result.scopedLessonNoCount = Math.max(
                        ...scopedCounts.map((item) => toInt(item.lessonNoCount))
                      );
                      result.scopedButtonRowCount = Math.max(
                        ...scopedCounts.map((item) => toInt(item.buttonRowCount))
                      );
                    }

                    // D) 전체 페이지 기준 폴백: "모든 학습하기 개수 - 1" 보정
                    const allElems = Array.from(
                      document.querySelectorAll(
                        'a,button,input[type="button"],input[type="submit"],span'
                      )
                    ).filter((el) => isVisible(el) && isLessonBtn(el));
                    const rawCount = toInt(allElems.length);
                    result.globalButtonCountRaw = rawCount;
                    // 강의실 전체 버튼에는 차시 외 진입용 버튼이 1개 포함되어 있어 -1 보정.
                    result.globalButtonCountAdjusted = rawCount <= 1 ? rawCount : rawCount - 1;
                    return result;
                }
                """
            )
        except Exception:  # noqa: BLE001
            return None

        if not isinstance(stats, dict):
            return None

        explicit_text_total = stats.get("explicitTotalFromText")
        if isinstance(explicit_text_total, int) and explicit_text_total > 0:
            return explicit_text_total

        explicit_script_total = stats.get("explicitTotalFromScripts")
        if isinstance(explicit_script_total, int) and explicit_script_total > 0:
            return explicit_script_total

        scoped_lesson_section_lesson_no_count = stats.get("scopedLessonSectionLessonNoCount")
        if isinstance(scoped_lesson_section_lesson_no_count, int) and scoped_lesson_section_lesson_no_count >= 2:
            return scoped_lesson_section_lesson_no_count

        scoped_lesson_section_numbered_card_count = stats.get("scopedLessonSectionNumberedCardCount")
        if (
            isinstance(scoped_lesson_section_numbered_card_count, int)
            and scoped_lesson_section_numbered_card_count >= 2
        ):
            return scoped_lesson_section_numbered_card_count

        scoped_lesson_section_button_row_count = stats.get("scopedLessonSectionButtonRowCount")
        if isinstance(scoped_lesson_section_button_row_count, int) and scoped_lesson_section_button_row_count >= 2:
            return scoped_lesson_section_button_row_count

        scoped_lesson_section_button_count_min_positive = stats.get("scopedLessonSectionButtonCountMinPositive")
        if (
            isinstance(scoped_lesson_section_button_count_min_positive, int)
            and scoped_lesson_section_button_count_min_positive >= 2
        ):
            return scoped_lesson_section_button_count_min_positive

        scoped_lesson_no_count = stats.get("scopedLessonNoCount")
        if isinstance(scoped_lesson_no_count, int) and scoped_lesson_no_count >= 2:
            return scoped_lesson_no_count

        # 버튼 개수는 1이 자주 과소감지되는 패턴이어서 2 이상일 때만 신뢰합니다.
        scoped_button_row_count = stats.get("scopedButtonRowCount")
        if isinstance(scoped_button_row_count, int) and scoped_button_row_count >= 2:
            return scoped_button_row_count

        global_button_count_raw = stats.get("globalButtonCountRaw")
        global_button_count_adjusted = stats.get("globalButtonCountAdjusted")
        # 전체 화면에서 raw 버튼이 2개 이상이면(메인 + 차시들), 보정값을 신뢰합니다.
        if (
            isinstance(global_button_count_raw, int)
            and isinstance(global_button_count_adjusted, int)
            and global_button_count_raw >= 2
            and global_button_count_adjusted >= 2
        ):
            return global_button_count_adjusted

        return None

    def _prime_lesson_list_dom(self, page: Page) -> None:
        # 강의실이 긴 페이지인 경우 하단 "학습 차시" DOM이 늦게 채워질 수 있어 선탐색합니다.
        try:
            page.locator('text=학습 차시').first.scroll_into_view_if_needed(timeout=2000)
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(900)
        except Exception:  # noqa: BLE001
            pass

    def _open_learning_status(self, page: Page) -> LoginResult:
        if "/login/process.do" in page.url:
            self._log("로그인 처리 페이지(process.do) 감지: 메인 전환 대기")
            for _ in range(20):
                if "/login/process.do" not in page.url:
                    break
                page.wait_for_timeout(500)
            if "/login/process.do" in page.url:
                self._log("자동 전환 지연: 메인 주소 재요청")
                origin_match = re.match(r"^https?://[^/]+", page.url)
                origin = origin_match.group(0) if origin_match else self.settings.base_url
                page.goto(origin, wait_until="domcontentloaded")
                page.wait_for_timeout(1000)

        # 로그인 직후 process.do 중간 화면이 잠깐 보일 수 있어 네비게이션 안정화 대기.
        try:
            page.wait_for_load_state("networkidle", timeout=min(self.settings.timeout_ms, 12000))
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1200)

        self._log("상단 메뉴 'My학습포털' hover 시도")
        top_menu_candidates = [
            'a:has-text("My학습포털")',
            'li:has-text("My학습포털")',
            'span:has-text("My학습포털")',
        ]
        top_menu_hovered = self._hover_first_visible(page, top_menu_candidates, max_items=20)
        if not top_menu_hovered:
            self._log("'My학습포털' hover는 건너뛰고 링크 직접 클릭을 시도합니다.")

        page.wait_for_timeout(400)
        self._log("드롭다운 메뉴 '나의 학습현황' 클릭 시도")
        try:
            page.wait_for_function(
                """
                () => {
                    const hasLink = document.querySelectorAll('a[href*="/usr/member/dash/detail.do"]').length > 0;
                    const hasText = (document.body && document.body.innerText || '').includes('나의 학습현황');
                    return hasLink || hasText;
                }
                """,
                timeout=min(self.settings.timeout_ms, 12000),
            )
        except PlaywrightTimeoutError:
            pass

        status_menu_candidates = [
            'a:has-text("나의 학습현황")',
            'a[href*="/usr/member/dash/detail.do"]',
            'li:has-text("나의 학습현황") a',
            'button:has-text("나의 학습현황")',
        ]
        clicked = self._click_first_visible(page, status_menu_candidates, max_items=40)
        if not clicked:
            clicked = page.evaluate(
                """
                () => {
                    const hrefTarget = document.querySelector('a[href*="/usr/member/dash/detail.do"]');
                    if (hrefTarget) {
                      hrefTarget.click();
                      return true;
                    }
                    const elements = Array.from(document.querySelectorAll('a,button,li,span'));
                    const target = elements.find((el) =>
                      (el.textContent || '').trim() === '나의 학습현황'
                    );
                    if (!target) return false;
                    target.click();
                    return true;
                }
                """
            )
        if not clicked:
            return LoginResult(False, "'나의 학습현황' 메뉴 클릭 실패", page.url)

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1200)
        body_text = page.locator("body").inner_text(timeout=5000)
        if "나의 학습현황" in body_text or "My Learning" in body_text:
            return LoginResult(True, "나의 학습현황 페이지 이동 성공", page.url)
        return LoginResult(False, "나의 학습현황 클릭 후 이동 확인 실패", page.url)

    def _enter_first_course(self, page: Page) -> LoginResult:
        result, _ = self._enter_first_course_internal(page)
        return result

    def _open_first_course_classroom_internal(self, page: Page) -> tuple[LoginResult, Optional[Page]]:
        self._log("수강과정 목록 로딩 대기")
        try:
            page.wait_for_selector("table tbody tr", timeout=min(self.settings.timeout_ms, 15000))
        except PlaywrightTimeoutError:
            return LoginResult(False, "수강과정 테이블을 찾지 못했습니다.", page.url), None

        try:
            first_title = (
                page.locator("table tbody tr").first.locator("td").nth(3).inner_text(timeout=3000).strip()
            )
        except Exception:  # noqa: BLE001
            first_title = ""

        self._log(f"첫 번째 과정: {first_title or '제목 확인 실패'}")

        first_row_button = page.locator("table tbody tr").first.locator(
            'a:has-text("학습하기"), button:has-text("학습하기"), input[value*="학습하기"]'
        ).first
        if first_row_button.count() == 0:
            return LoginResult(False, "첫 번째 과정의 '학습하기' 버튼을 찾지 못했습니다.", page.url), None

        popup_page = None
        target_page = page
        try:
            with page.expect_popup(timeout=7000) as popup_info:
                first_row_button.click()
            popup_page = popup_info.value
            popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
            target_page = popup_page
        except Exception:  # noqa: BLE001
            popup_page = None

        if popup_page is None:
            try:
                before_url = page.url
                first_row_button.click()
                page.wait_for_timeout(2500)
                now_url = page.url
                if "/usr/classroom/" not in now_url and "/usr/classroom/" not in before_url:
                    return LoginResult(False, "첫 번째 과정 클릭 후 강의실 진입 확인 실패", now_url), None
            except Exception as exc:  # noqa: BLE001
                now_url = page.url
                if "/usr/classroom/" not in now_url:
                    return LoginResult(False, f"'학습하기' 클릭 실패: {exc}", now_url), None
            target_page = page

        return (
            LoginResult(
                True,
                f"강의실 진입 성공: {first_title or '첫 번째 과정'}",
                target_page.url,
            ),
            target_page,
        )

    def _enter_first_course_internal(self, page: Page) -> tuple[LoginResult, Optional[Page]]:
        classroom_result, target_page = self._open_first_course_classroom_internal(page)
        if not classroom_result.success or target_page is None:
            return classroom_result, None

        learning_page = self._start_learning_from_progress_panel(target_page)
        if learning_page is not None:
            return (
                LoginResult(
                    True,
                    f"학습 시작 클릭 성공(학습진행현황): {classroom_result.message.replace('강의실 진입 성공: ', '')}",
                    learning_page.url,
                ),
                learning_page,
            )
        return (
            LoginResult(
                False,
                "강의실 진입은 성공했지만 학습진행현황의 하단 '학습하기' 버튼 클릭 실패",
                target_page.url,
            ),
            None,
        )

    def _start_learning_from_progress_panel(self, page: Page) -> Optional[Page]:
        self._log("강의실 하단 '학습진행현황'의 학습하기 버튼 클릭 시도")
        page.wait_for_timeout(1200)
        before_pages = list(page.context.pages)
        self._prime_lesson_list_dom(page)
        self._detected_total_lessons = self._extract_total_lessons_from_classroom_buttons(page)
        if self._detected_total_lessons is None:
            # 지연 렌더링/가상 스크롤 대응: 한번 더 강제 로드 후 재탐색
            self._prime_lesson_list_dom(page)
            self._detected_total_lessons = self._extract_total_lessons_from_classroom_buttons(page)
        if self._detected_total_lessons:
            self._log(
                f"강의실 학습하기 버튼 개수 감지: 총 {self._detected_total_lessons}차시"
            )
        # 미완료 차시가 있으면 우선 해당 항목으로 진입해 실제 진도율 상승을 우선합니다.
        incomplete_popup = self._open_incomplete_lesson_popup(page)
        if incomplete_popup is not None:
            self._log("미완료 차시 우선 학습창 열기 성공")
            return incomplete_popup

        try:
            page.locator('text=학습진행현황').first.scroll_into_view_if_needed(timeout=1500)
        except Exception:  # noqa: BLE001
            pass

        scoped_candidates = [
            'div:has-text("학습진행현황") span[onclick*="doFirstScript"]',
            'div:has-text("학습진행현황") a:has-text("학습하기")',
            'div:has-text("학습진행현황") a:has-text("이어 학습하기")',
            'div:has-text("학습진행현황") button:has-text("학습하기")',
            'div:has-text("학습진행현황") button:has-text("이어 학습하기")',
            'div:has-text("학습진행현황") input[value*="학습하기"]',
            'div:has-text("학습진행현황") input[value*="이어 학습하기"]',
            'a[onclick*="doStudyPopup"]:has-text("이어 학습하기")',
            'a[onclick*="doStudyPopup"]',
            'a[onclick*="doLearning"]',
            'span[onclick*="doFirstScript"]',
            'a:has-text("학습 하기")',
        ]
        clicked = False
        popup_page: Optional[Page] = None
        try:
            with page.expect_popup(timeout=15000) as popup_info:
                clicked = self._click_first_visible(page, scoped_candidates, max_items=30)
            popup_page = popup_info.value if clicked else None
        except Exception:  # noqa: BLE001
            clicked = self._click_first_visible(page, scoped_candidates, max_items=30)

        if clicked:
            page.wait_for_timeout(1500)
            if popup_page is not None:
                popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
                self._log(f"학습창 팝업 감지: {popup_page.url}")
                return popup_page
            picked = self._pick_learning_page(page.context.pages, before_pages)
            if picked is not None and picked != page:
                self._log(f"학습창 선택: pages={len(page.context.pages)} / url={picked.url}")
                return picked
            self._log("학습하기 클릭 후 팝업 창이 감지되지 않았습니다.")
            return None

        # DOM 구조가 바뀌는 경우를 대비해 "학습진행현황" 텍스트 근처에서만 클릭합니다.
        clicked = page.evaluate(
            """
            () => {
                const isTargetButton = (el) => {
                  const txt = ((el.textContent || el.value || '').trim());
                  return txt === '학습하기' || txt === '이어 학습하기' || txt === '이어학습하기';
                };

                const titleNodes = Array.from(document.querySelectorAll('div,span,strong,h1,h2,h3,h4'));
                for (const title of titleNodes) {
                  const titleText = (title.textContent || '').trim();
                  if (!titleText.includes('학습진행현황')) continue;

                  let container = title;
                  for (let depth = 0; depth < 6 && container; depth++) {
                    const cands = Array.from(
                      container.querySelectorAll('a,button,input[type="button"],input[type="submit"]')
                    );
                    const btn = cands.find(isTargetButton);
                    if (btn) {
                      btn.click();
                      return true;
                    }
                    container = container.parentElement;
                  }
                }
                const studyPopupBtn = document.querySelector('a[onclick*="doStudyPopup"]');
                if (studyPopupBtn) {
                  studyPopupBtn.click();
                  return true;
                }
                const doLearningBtn = document.querySelector('a[onclick*="doLearning"]');
                if (doLearningBtn) {
                  doLearningBtn.click();
                  return true;
                }
                const doFirstBtn = document.querySelector('span[onclick*="doFirstScript"]');
                if (doFirstBtn) {
                  doFirstBtn.click();
                  return true;
                }
                return false;
            }
            """
        )
        if clicked:
            page.wait_for_timeout(1500)
            picked = self._pick_learning_page(page.context.pages, before_pages)
            if picked is not None and picked != page:
                self._log(f"학습창 선택(폴백): pages={len(page.context.pages)} / url={picked.url}")
                return picked
            self._log("학습하기 클릭(폴백) 후에도 팝업 창이 감지되지 않았습니다.")
            self._dump_player_debug(page, "start_click_no_popup")
            return None
        self._dump_player_debug(page, "start_button_not_found")
        return None

    def _open_comprehensive_exam_popup(self, page: Page) -> Optional[Page]:
        self._exam_gate_blocked = False
        self._log("강의실 '종합평가 응시하기' 버튼 클릭 시도")
        page.wait_for_timeout(1000)
        before_pages = list(page.context.pages)

        try:
            page.locator("text=종합평가").first.scroll_into_view_if_needed(timeout=2500)
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass

        selectors = [
            'div:has-text("종합평가") a:has-text("응시하기")',
            'div:has-text("종합평가") button:has-text("응시하기")',
            'div:has-text("종합평가") span:has-text("응시하기")',
            'div:has-text("종합평가") a:has-text("click")',
            'div:has-text("종합평가") button:has-text("click")',
            'div:has-text("종합평가") span:has-text("click")',
            'div:has-text("종합평가") input[value*="응시"]',
            'div:has-text("시험평가") a:has-text("응시하기")',
            'div:has-text("시험평가") button:has-text("응시하기")',
            'a:has-text("종합평가")',
            'a:has-text("응시하기")',
            'button:has-text("응시하기")',
            'input[value*="응시하기"]',
        ]
        clicked = False
        popup_page: Optional[Page] = None
        try:
            with page.expect_popup(timeout=12000) as popup_info:
                clicked = self._click_first_visible(page, selectors, max_items=40)
            popup_page = popup_info.value if clicked else None
        except Exception:  # noqa: BLE001
            clicked = self._click_first_visible(page, selectors, max_items=40)

        if not clicked:
            clicked = page.evaluate(
                """
                () => {
                  const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                  const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const titleNodes = Array.from(document.querySelectorAll('div,span,strong,h1,h2,h3,h4,li,p'));
                  const isExamTitle = (txt) => txt.includes('종합평가') || txt.includes('시험평가');
                  const isExamButton = (txt) =>
                    txt.includes('응시하기')
                    || txt.includes('응시하기click')
                    || (txt.includes('응시') && txt.includes('click'))
                    || txt.includes('재응시')
                    || txt.includes('평가응시');

                  for (const title of titleNodes) {
                    const tt = normalize(title.textContent);
                    if (!isExamTitle(tt)) continue;
                    let container = title;
                    for (let depth = 0; depth < 8 && container; depth++) {
                      const cands = Array.from(
                        container.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                      );
                      const target = cands.find((el) => isVisible(el) && isExamButton(normalize(el.textContent || el.value)));
                      if (target) {
                        target.click();
                        return true;
                      }
                      container = container.parentElement;
                    }
                  }

                  const all = Array.from(document.querySelectorAll('a,button,input[type="button"],input[type="submit"],span'));
                  const fallback = all.find((el) => {
                    const txt = normalize(el.textContent || el.value);
                    return isVisible(el) && isExamButton(txt);
                  });
                  if (fallback) {
                    fallback.click();
                    return true;
                  }
                  return false;
                }
                """
            )

        if clicked:
            page.wait_for_timeout(1500)
            if popup_page is not None:
                popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
                self._log(f"종합평가 팝업 감지: {popup_page.url}")
                return popup_page

            picked = self._pick_exam_page(page.context.pages, before_pages)
            if picked is not None and picked != page:
                self._log(f"종합평가 창 선택: pages={len(page.context.pages)} / url={picked.url}")
                return picked

        if self._dismiss_exam_progress_gate_notice(page):
            self._exam_gate_blocked = True
            self._log("종합평가 응시 제한 알림 감지: 미완료 차시를 먼저 진행합니다.")
            return None

        self._log("종합평가 응시 팝업을 찾지 못했습니다.")
        return None

    def _dismiss_exam_progress_gate_notice(self, page: Page) -> bool:
        try:
            detected = page.evaluate(
                """
                () => {
                  const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                  const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const hasGateText = (txt) =>
                    txt.includes('학습 진도율') && txt.includes('80') && txt.includes('응시') && txt.includes('가능');

                  const textNodes = Array.from(document.querySelectorAll('div,p,span,li,strong,h1,h2,h3,h4'));
                  let noticeNode = null;
                  for (const node of textNodes) {
                    const txt = normalize(node.textContent);
                    if (hasGateText(txt)) {
                      noticeNode = node;
                      break;
                    }
                  }
                  if (!noticeNode) return false;

                  let modal = noticeNode;
                  for (let i = 0; i < 8 && modal; i++) {
                    const modalText = normalize(modal.innerText || modal.textContent || '');
                    if (hasGateText(modalText)) break;
                    modal = modal.parentElement;
                  }
                  if (!modal) modal = noticeNode;

                  const buttons = Array.from(
                    modal.querySelectorAll('button,a,input[type="button"],input[type="submit"],span')
                  );
                  const okBtn = buttons.find((el) => {
                    const txt = normalize(el.textContent || el.value || '');
                    return isVisible(el) && (txt === '확인' || txt.includes('확인'));
                  });
                  if (okBtn) {
                    okBtn.click();
                    return true;
                  }

                  const globalButtons = Array.from(document.querySelectorAll('button,a,input[type="button"],input[type="submit"],span'));
                  const globalOk = globalButtons.find((el) => {
                    const txt = normalize(el.textContent || el.value || '');
                    return isVisible(el) && (txt === '확인' || txt.includes('확인'));
                  });
                  if (globalOk) {
                    globalOk.click();
                    return true;
                  }
                  return true;
                }
                """
            )
        except Exception:  # noqa: BLE001
            return False

        if detected:
            page.wait_for_timeout(600)
            return True
        return False

    def _probe_exam_question_stream(self, page: Page, max_questions: int = 12) -> dict[str, int]:
        visited_keys: set[str] = set()
        dom_readable_count = 0
        visited_count = 0
        total_hint = 0

        self._log(f"종합평가 문항 탐침 시작 (최대 {max_questions}문항)")
        for _ in range(max_questions):
            snap = self._extract_exam_question_snapshot(page)
            if snap is None:
                self._log("문항 텍스트 판독 실패: OCR 폴백 필요 가능성")
                break

            visited_count += 1
            key = str(snap.get("key", ""))
            text_len = int(snap.get("text_len", 0))
            option_count = int(snap.get("option_count", 0))
            current = int(snap.get("current", 0))
            total = int(snap.get("total", 0))
            source = str(snap.get("source", "dom"))
            if total > 0:
                total_hint = max(total_hint, total)
            if text_len >= 40 and option_count >= 2 and source == "dom":
                dom_readable_count += 1

            self._log(
                "문항 판독: "
                f"idx={visited_count} current/total={current}/{total} "
                f"text_len={text_len} option_count={option_count} source={source}"
            )

            if key in visited_keys:
                self._log("이전 문항과 동일 화면으로 판단되어 탐침을 종료합니다.")
                break
            visited_keys.add(key)

            if total > 0 and current >= total:
                self._log("마지막 문항으로 보여 탐침을 종료합니다.")
                break

            if not self._click_exam_next(page):
                self._log("다음 문항 버튼을 찾지 못해 탐침을 종료합니다.")
                break

            if not self._wait_exam_question_change(page, key):
                self._log("다음 클릭 후 문항 변화가 감지되지 않아 탐침을 종료합니다.")
                break

        return {
            "visited_count": visited_count,
            "dom_readable_count": dom_readable_count,
            "total_hint": total_hint,
        }

    def _solve_exam_stream_with_rag(
        self,
        exam_page: Page,
        solver: Any,
        max_questions: int = 60,
        top_k: int = 6,
        confidence_threshold: float = 0.72,
    ) -> dict[str, Any]:
        visited_keys: set[str] = set()
        solved = 0
        skipped = 0

        self._log(
            f"종합평가 RAG 자동풀이 시작 (max={max_questions}, top_k={top_k}, conf>={confidence_threshold:.2f})"
        )
        for _ in range(max_questions):
            snap = self._extract_exam_question_snapshot(exam_page)
            if snap is None:
                return {"success": False, "message": "문항 텍스트를 읽지 못했습니다.", "solved": solved, "skipped": skipped}

            key = str(snap.get("key", ""))
            if key in visited_keys:
                return {"success": True, "message": "이미 본 문항으로 종료", "solved": solved, "skipped": skipped}
            visited_keys.add(key)

            question = str(snap.get("question_text", "")).strip()
            full_text = str(snap.get("full_text", "")).strip()
            source = str(snap.get("source", "dom"))
            options = [str(x).strip() for x in snap.get("options", []) if str(x).strip()]
            current = int(snap.get("current", 0))
            total = int(snap.get("total", 0))
            if not question:
                question = full_text

            if len(options) < 2:
                if source != "ocr":
                    snap_ocr = self._extract_exam_question_snapshot(exam_page, force_ocr=True)
                    if snap_ocr:
                        options = [str(x).strip() for x in snap_ocr.get("options", []) if str(x).strip()]
                        question = str(snap_ocr.get("question_text", "")).strip() or str(
                            snap_ocr.get("full_text", "")
                        ).strip()
                        source = str(snap_ocr.get("source", source))
                if len(options) < 2:
                    return {
                        "success": False,
                        "message": f"보기 추출 실패(current/total={current}/{total}, source={source})",
                        "solved": solved,
                        "skipped": skipped,
                    }

            try:
                decision = solver.solve(question=question, options=options, top_k=top_k)
            except Exception as exc:  # noqa: BLE001
                return {
                    "success": False,
                    "message": f"RAG 풀이 호출 실패: {exc}",
                    "solved": solved,
                    "skipped": skipped,
                }
            choice = int(getattr(decision, "choice", 0))
            confidence = float(getattr(decision, "confidence", 0.0))
            reason = str(getattr(decision, "reason", ""))
            evidence_ids = list(getattr(decision, "evidence_ids", []))
            self._log(
                "RAG 풀이: "
                f"Q {current}/{total} -> choice={choice}, conf={confidence:.2f}, source={source}, evidence={evidence_ids[:2]}"
            )

            if choice < 1 or choice > len(options):
                return {
                    "success": False,
                    "message": f"LLM 선택지 번호 비정상: {choice}",
                    "solved": solved,
                    "skipped": skipped,
                }
            if confidence < confidence_threshold:
                retry_top_k = min(20, max(int(top_k) + 2, int(top_k * 1.5)))
                self._log(
                    "LLM 신뢰도 낮음으로 재질문 1회 시도: "
                    f"Q {current}/{total} conf={confidence:.2f} -> top_k={retry_top_k}"
                )
                try:
                    retry_decision = solver.solve(question=question, options=options, top_k=retry_top_k)
                    retry_choice = int(getattr(retry_decision, "choice", 0))
                    retry_conf = float(getattr(retry_decision, "confidence", 0.0))
                    retry_reason = str(getattr(retry_decision, "reason", ""))
                    retry_evidence_ids = list(getattr(retry_decision, "evidence_ids", []))
                    self._log(
                        "RAG 재질문 결과: "
                        f"Q {current}/{total} -> choice={retry_choice}, conf={retry_conf:.2f}, evidence={retry_evidence_ids[:2]}"
                    )
                    if retry_choice >= 1 and retry_choice <= len(options) and retry_conf >= confidence:
                        choice = retry_choice
                        confidence = retry_conf
                        reason = retry_reason or reason
                        evidence_ids = retry_evidence_ids or evidence_ids
                except Exception as exc:  # noqa: BLE001
                    self._log(f"RAG 재질문 실패: {exc}")

                if confidence < confidence_threshold:
                    skipped += 1
                    return {
                        "success": False,
                        "message": f"LLM 신뢰도 낮음(conf={confidence:.2f}): {reason}",
                        "solved": solved,
                        "skipped": skipped,
                    }

            if not self._click_exam_option(exam_page, choice):
                return {
                    "success": False,
                    "message": f"선택지 클릭 실패: {choice}",
                    "solved": solved,
                    "skipped": skipped,
                }
            solved += 1

            if total > 0 and current >= total:
                self._click_exam_submit_if_present(exam_page)
                return {"success": True, "message": "마지막 문항 제출 완료", "solved": solved, "skipped": skipped}

            if not self._click_exam_next(exam_page):
                self._click_exam_submit_if_present(exam_page)
                return {"success": True, "message": "다음 버튼 없음, 제출 시도 후 종료", "solved": solved, "skipped": skipped}

            if not self._wait_exam_question_change(exam_page, key):
                self._click_exam_submit_if_present(exam_page)
                return {"success": True, "message": "문항 변화 없음, 제출 시도 후 종료", "solved": solved, "skipped": skipped}

        return {"success": False, "message": "문항 상한 도달", "solved": solved, "skipped": skipped}

    @staticmethod
    def _parse_exam_text_payload(raw_text: str) -> Optional[dict[str, Any]]:
        normalized = re.sub(r"\s+", " ", raw_text).strip()
        if len(normalized) < 20:
            return None

        line_options: list[str] = []
        for ln in raw_text.splitlines():
            s = ln.strip()
            if not s:
                continue
            if re.match(r"^(?:[1-5]|[A-Ea-e]|[가-마])\s*[\.\)]\s*.+$", s):
                cleaned = re.sub(r"^(?:[1-5]|[A-Ea-e]|[가-마])\s*[\.\)]\s*", "", s)
                line_options.append(cleaned.strip())
            elif re.match(r"^[①②③④⑤]\s*.+$", s):
                cleaned = re.sub(r"^[①②③④⑤]\s*", "", s)
                line_options.append(cleaned.strip())
            elif re.match(r"^\[(?:[1-5])\]\s*.+$", s):
                cleaned = re.sub(r"^\[(?:[1-5])\]\s*", "", s)
                line_options.append(cleaned.strip())

        options = [x for x in line_options if x]
        option_count = len(options)

        cur = 0
        tot = 0
        ratio_match = re.search(r"(\d{1,3})\s*/\s*(\d{1,3})", raw_text)
        if ratio_match:
            try:
                cur = int(ratio_match.group(1))
                tot = int(ratio_match.group(2))
            except ValueError:
                cur = 0
                tot = 0
        if cur == 0:
            cur_match = re.search(r"(?:문항|문제)\s*(\d{1,3})", raw_text)
            if cur_match:
                try:
                    cur = int(cur_match.group(1))
                except ValueError:
                    cur = 0

        question_text = normalized
        if options:
            split_pat = r"(?:[1-5]|[A-Ea-e]|[가-마])\s*[\.\)]\s+|[①②③④⑤]\s+|\[(?:[1-5])\]\s+"
            q_part = re.split(split_pat, normalized, maxsplit=1)[0].strip()
            if q_part:
                question_text = q_part

        return {
            "full_text": normalized,
            "text_len": len(normalized),
            "current": cur,
            "total": tot,
            "question_text": question_text,
            "options": options,
            "option_count": option_count,
        }

    @staticmethod
    def _score_exam_snapshot(payload: dict[str, Any]) -> int:
        return (
            int(payload.get("option_count", 0)) * 1000
            + min(int(payload.get("text_len", 0)), 900)
            + (80 if int(payload.get("current", 0)) > 0 else 0)
            + (60 if int(payload.get("total", 0)) > 0 else 0)
        )

    def _ensure_tesseract(self) -> bool:
        if self._tesseract_path is None:
            self._tesseract_path = shutil.which("tesseract") or ""
        if not self._tesseract_path and not self._ocr_unavailable_logged:
            self._ocr_unavailable_logged = True
            self._log("OCR 폴백 비활성: tesseract 실행 파일을 찾지 못했습니다.")
        return bool(self._tesseract_path)

    def _ocr_text_from_scope(self, scope: Any) -> str:
        if not self._ensure_tesseract():
            return ""

        img_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                img_path = tmp.name
            scope.locator("body").first.screenshot(path=img_path, timeout=4500)

            cmd = [str(self._tesseract_path), img_path, "stdout", "-l", "kor+eng", "--psm", "6"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout

            fallback_cmd = [str(self._tesseract_path), img_path, "stdout", "--psm", "6"]
            fallback_proc = subprocess.run(
                fallback_cmd, capture_output=True, text=True, timeout=15, check=False
            )
            if fallback_proc.returncode == 0:
                return fallback_proc.stdout or ""
        except Exception:  # noqa: BLE001
            return ""
        finally:
            if img_path:
                try:
                    os.remove(img_path)
                except Exception:  # noqa: BLE001
                    pass
        return ""

    def _extract_exam_question_snapshot(self, page: Page, force_ocr: bool = False) -> Optional[dict[str, Any]]:
        scopes: list[Any] = [page] + list(page.frames)
        best_snapshot: Optional[dict[str, Any]] = None
        best_score = -1

        for scope in scopes:
            body_text = ""
            try:
                body_text = scope.locator("body").inner_text(timeout=2000)
            except Exception:  # noqa: BLE001
                body_text = ""

            parsed_dom = self._parse_exam_text_payload(body_text) if body_text else None
            picked = parsed_dom
            source = "dom"

            needs_ocr = force_ocr or picked is None or int(picked.get("option_count", 0)) < 2
            if needs_ocr:
                ocr_text = self._ocr_text_from_scope(scope)
                parsed_ocr = self._parse_exam_text_payload(ocr_text) if ocr_text else None
                if parsed_ocr is not None and (
                    picked is None or self._score_exam_snapshot(parsed_ocr) > self._score_exam_snapshot(picked)
                ):
                    picked = parsed_ocr
                    source = "ocr"

            if picked is None:
                continue

            key_source = re.sub(r"[^0-9A-Za-z가-힣]+", "", str(picked.get("full_text", "")))[:240]
            if not key_source:
                continue

            snapshot = {
                "key": key_source,
                "text_len": int(picked.get("text_len", 0)),
                "option_count": int(picked.get("option_count", 0)),
                "current": int(picked.get("current", 0)),
                "total": int(picked.get("total", 0)),
                "question_text": str(picked.get("question_text", "")),
                "options": list(picked.get("options", [])),
                "full_text": str(picked.get("full_text", "")),
                "source": source,
            }
            score = self._score_exam_snapshot(snapshot)
            if score > best_score:
                best_snapshot = snapshot
                best_score = score

        return best_snapshot

    def _click_exam_next(self, page: Page) -> bool:
        selectors = [
            'button:has-text("다음")',
            'a:has-text("다음")',
            'input[value*="다음"]',
            'button:has-text("Next")',
            'a:has-text("Next")',
            'a[onclick*="next"]',
            'button[onclick*="next"]',
        ]
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            if self._click_first_visible(scope, selectors, max_items=20):
                page.wait_for_timeout(900)
                return True

        try:
            clicked = page.evaluate(
                """
                () => {
                  const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const cands = Array.from(document.querySelectorAll('a,button,input[type="button"],input[type="submit"],span'));
                  const target = cands.find((el) => {
                    const txt = normalize(el.textContent || el.value);
                    return isVisible(el) && (txt.includes('다음') || txt.includes('next') || txt.includes('다음문항'));
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """
            )
            if clicked:
                page.wait_for_timeout(900)
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _click_exam_option(self, page: Page, choice: int) -> bool:
        if choice < 1:
            return False
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            try:
                radios = scope.locator('input[type="radio"]')
                cnt = radios.count()
                if cnt >= choice:
                    target = radios.nth(choice - 1)
                    if target.is_visible():
                        target.check(force=True)
                        page.wait_for_timeout(300)
                        return True
            except Exception:  # noqa: BLE001
                pass

        # 라디오가 없거나 숨김 처리일 때 텍스트 라벨로 클릭 시도
        choice_tokens = {
            1: ["①", "1.", "1)", "1번"],
            2: ["②", "2.", "2)", "2번"],
            3: ["③", "3.", "3)", "3번"],
            4: ["④", "4.", "4)", "4번"],
            5: ["⑤", "5.", "5)", "5번"],
        }.get(choice, [f"{choice}.", f"{choice})"])

        try:
            clicked = page.evaluate(
                """
                ({ tokens }) => {
                  const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                  const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const cands = Array.from(document.querySelectorAll('label,li,td,div,span,a,button'));
                  for (const el of cands) {
                    if (!isVisible(el)) continue;
                    const txt = normalize(el.textContent || el.value || '');
                    if (!txt || txt.length > 220) continue;
                    if (tokens.some((t) => txt.startsWith(t) || txt.includes(` ${t} `))) {
                      el.click();
                      return true;
                    }
                  }
                  return false;
                }
                """,
                {"tokens": choice_tokens},
            )
            if clicked:
                page.wait_for_timeout(300)
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _click_exam_submit_if_present(self, page: Page) -> bool:
        selectors = [
            'button:has-text("제출")',
            'a:has-text("제출")',
            'input[value*="제출"]',
            'button:has-text("완료")',
            'a:has-text("완료")',
            'button:has-text("채점")',
            'a:has-text("채점")',
        ]
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            if self._click_first_visible(scope, selectors, max_items=12):
                page.wait_for_timeout(1000)
                return True
        return False

    def _wait_exam_question_change(self, page: Page, prev_key: str, timeout_ms: int = 12000) -> bool:
        ticks = max(1, timeout_ms // 500)
        for _ in range(ticks):
            page.wait_for_timeout(500)
            snap = self._extract_exam_question_snapshot(page)
            if snap is None:
                continue
            now_key = str(snap.get("key", ""))
            if now_key and now_key != prev_key:
                return True
        return False

    def _has_course_end_notice(self, page: Page) -> bool:
        end_keywords = [
            "학습이 종료되었습니다",
            "학습이 종료",
            "학습 종료",
            "학습이 완료되었습니다",
            "학습 완료",
            "수강이 종료되었습니다",
        ]
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            try:
                text = scope.locator("body").inner_text(timeout=1200)
            except Exception:  # noqa: BLE001
                continue
            if any(k in text for k in end_keywords):
                return True
        return False

    def _wait_exam_finished(self, exam_page: Page, timeout_ms: int = 60 * 60 * 1000) -> bool:
        ticks = max(1, timeout_ms // 5000)
        done_keywords = [
            "제출완료",
            "응시완료",
            "시험평가 완료",
            "평가완료",
            "채점결과",
            "득점",
            "점수",
            "재응시",
        ]
        for _ in range(ticks):
            if exam_page.is_closed():
                return True
            scopes: list[Any] = [exam_page] + list(exam_page.frames)
            for scope in scopes:
                try:
                    body = scope.locator("body").inner_text(timeout=1200)
                except Exception:  # noqa: BLE001
                    continue
                if any(k in body for k in done_keywords):
                    return True
            exam_page.wait_for_timeout(5000)
        return False

    def _open_first_lesson_popup_for_timefill(self, page: Page) -> Optional[Page]:
        self._log("학습시간 보충용 1차시 학습창 열기 시도")
        before_pages = list(page.context.pages)
        try:
            page.locator('text=학습 차시').first.scroll_into_view_if_needed(timeout=2500)
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass

        selectors = [
            'div:has-text("학습 차시") a:has-text("학습하기")',
            'div:has-text("학습 차시") a:has-text("이어 학습하기")',
            'div:has-text("학습차시") a:has-text("학습하기")',
            'div:has-text("학습차시") a:has-text("이어 학습하기")',
            'a:has-text("학습하기")',
            'a:has-text("이어 학습하기")',
        ]
        clicked = False
        popup_page: Optional[Page] = None
        try:
            with page.expect_popup(timeout=12000) as popup_info:
                clicked = self._click_first_visible(page, selectors, max_items=40)
            popup_page = popup_info.value if clicked else None
        except Exception:  # noqa: BLE001
            clicked = self._click_first_visible(page, selectors, max_items=40)

        if clicked:
            page.wait_for_timeout(1500)
            if popup_page is not None:
                popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
                self._log(f"학습시간 보충용 학습창 팝업 감지: {popup_page.url}")
                return popup_page
            picked = self._pick_learning_page(page.context.pages, before_pages)
            if picked is not None and picked != page:
                self._log(f"학습시간 보충용 학습창 선택: pages={len(page.context.pages)} / url={picked.url}")
                return picked
        return None

    def _refresh_classroom_page(self, classroom_page: Page) -> None:
        # 중요: 강의(팝업/플레이어) 창이 아니라 강의실 메인 페이지만 새로고침합니다.
        self._log("강의실 새로고침으로 학습진행현황을 업데이트합니다.")
        try:
            classroom_page.reload(wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            try:
                classroom_page.goto(classroom_page.url, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
        classroom_page.wait_for_timeout(1200)

    def _extract_learning_progress_status(self, classroom_page: Page) -> dict[str, int | bool]:
        table_rows = self._extract_completion_table_rows(classroom_page)
        progress_row = table_rows.get("학습진도율", {})

        current_percent = 0
        required_percent = 0
        if progress_row:
            current_percent = self._parse_percent_value(str(progress_row.get("actual", "")))
            required_percent = self._parse_percent_value(str(progress_row.get("required", "")))

        try:
            body = classroom_page.locator("body").inner_text(timeout=5000)
        except Exception:  # noqa: BLE001
            body = ""

        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if current_percent == 0 or required_percent == 0:
            for line in lines:
                if "학습진도율" in line or "진도율" in line:
                    pcts = [int(x) for x in re.findall(r"(\d{1,3})\s*%", line)]
                    if pcts and current_percent == 0:
                        current_percent = max(current_percent, pcts[0])
                    if len(pcts) >= 2 and required_percent == 0:
                        required_percent = max(required_percent, pcts[1])
                    if required_percent == 0 and "수료기준" in line:
                        req_in_line = re.search(r"수료기준[^0-9]{0,12}(\d{1,3})\s*%", line)
                        if req_in_line:
                            required_percent = max(required_percent, int(req_in_line.group(1)))

        if required_percent == 0:
            for line in lines:
                if "수료기준" in line and "%" in line:
                    req = re.search(r"(\d{1,3})\s*%", line)
                    if req:
                        required_percent = max(required_percent, int(req.group(1)))

        if required_percent == 0 and current_percent > 0:
            required_percent = 100

        incomplete_count = len(re.findall(r"미완료", body))
        progress_ok = (
            current_percent >= required_percent
            if required_percent > 0
            else current_percent >= 100
        )
        self._log(
            "학습진도율 상태: "
            f"current={current_percent}% required={required_percent}% "
            f"incomplete={incomplete_count}"
        )
        return {
            "current_percent": current_percent,
            "required_percent": required_percent,
            "incomplete_count": incomplete_count,
            "progress_ok": progress_ok,
        }

    def _open_incomplete_lesson_popup(self, page: Page) -> Optional[Page]:
        before_pages = list(page.context.pages)
        try:
            page.locator('text=학습 차시').first.scroll_into_view_if_needed(timeout=2500)
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass

        selectors = [
            'div:has-text("미완료") a:has-text("학습하기")',
            'div:has-text("미완료") a:has-text("이어 학습하기")',
            'div:has-text("미완료") button:has-text("학습하기")',
            'div:has-text("미완료") button:has-text("이어 학습하기")',
            'tr:has-text("미완료") a:has-text("학습하기")',
            'tr:has-text("미완료") a:has-text("이어 학습하기")',
        ]
        clicked = False
        popup_page: Optional[Page] = None
        try:
            with page.expect_popup(timeout=12000) as popup_info:
                clicked = self._click_first_visible(page, selectors, max_items=60)
            popup_page = popup_info.value if clicked else None
        except Exception:  # noqa: BLE001
            clicked = self._click_first_visible(page, selectors, max_items=60)

        if not clicked:
            clicked = page.evaluate(
                """
                () => {
                  const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                  const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const isLessonBtn = (txt) => (
                    txt === '학습하기' || txt === '이어 학습하기' || txt === '이어학습하기'
                  );

                  const containers = Array.from(document.querySelectorAll('tr,li,div,section,article'));
                  for (const c of containers) {
                    const ctxt = normalize(c.innerText || c.textContent || '');
                    if (!ctxt.includes('미완료')) continue;
                    const cands = Array.from(
                      c.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                    );
                    const target = cands.find((el) => isVisible(el) && isLessonBtn(normalize(el.textContent || el.value)));
                    if (target) {
                      target.click();
                      return true;
                    }
                  }
                  return false;
                }
                """
            )

        if clicked:
            page.wait_for_timeout(1500)
            if popup_page is not None:
                popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
                return popup_page
            picked = self._pick_learning_page(page.context.pages, before_pages)
            if picked is not None and picked != page:
                return picked
        return None

    def _extract_study_time_status(self, classroom_page: Page) -> dict[str, int | bool]:
        table_rows = self._extract_completion_table_rows(classroom_page)
        time_row = table_rows.get("학습시간", {})

        current_seconds = 0
        required_seconds = 0
        if time_row:
            current_seconds = self._parse_duration_to_seconds(str(time_row.get("actual", "")))
            required_seconds = self._parse_duration_to_seconds(str(time_row.get("required", "")))

        try:
            body = classroom_page.locator("body").inner_text(timeout=5000)
        except Exception:  # noqa: BLE001
            body = ""

        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]

        if current_seconds == 0 or required_seconds == 0:
            for line in lines:
                if "학습시간" in line and "수료기준" in line:
                    left, right = line.split("수료기준", 1)
                    if current_seconds == 0:
                        current_seconds = max(current_seconds, self._parse_duration_to_seconds(left))
                    if required_seconds == 0:
                        required_seconds = max(required_seconds, self._parse_duration_to_seconds(right))

        if current_seconds == 0:
            for line in lines:
                if "나의 학습시간" in line or "학습시간" in line:
                    current_seconds = max(current_seconds, self._parse_duration_to_seconds(line))
        if required_seconds == 0:
            for line in lines:
                if "수료기준" in line and ("시간" in line or "분" in line or "초" in line or ":" in line):
                    required_seconds = max(required_seconds, self._parse_duration_to_seconds(line))

        shortage_seconds = max(required_seconds - current_seconds, 0) if required_seconds > 0 else 0
        requirement_known = required_seconds > 0

        self._log(
            "학습시간 상태: "
            f"current={self._format_seconds(current_seconds)}, "
            f"required={self._format_seconds(required_seconds)}, "
            f"shortage={self._format_seconds(shortage_seconds)}"
        )
        return {
            "current_seconds": current_seconds,
            "required_seconds": required_seconds,
            "shortage_seconds": shortage_seconds,
            "requirement_known": requirement_known,
        }

    def _extract_completion_table_rows(self, classroom_page: Page) -> dict[str, dict[str, str]]:
        try:
            raw = classroom_page.evaluate(
                """
                () => {
                  const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                  const tables = Array.from(document.querySelectorAll('table'));
                  for (const table of tables) {
                    const headerCells = Array.from(table.querySelectorAll('tr:first-child th, tr:first-child td'))
                      .map((el) => normalize(el.textContent));
                    const hasHeaders =
                      headerCells.some((h) => h.includes('항목'))
                      && headerCells.some((h) => h.includes('수료기준'))
                      && headerCells.some((h) => h.includes('나의실적'));
                    if (!hasHeaders) continue;

                    const rows = {};
                    const trs = Array.from(table.querySelectorAll('tr')).slice(1);
                    for (const tr of trs) {
                      const cells = Array.from(tr.querySelectorAll('th,td')).map((el) => normalize(el.textContent));
                      if (cells.length < 3) continue;
                      const item = cells[0];
                      rows[item] = {
                        required: cells[1] || '',
                        actual: cells[2] || '',
                        result: cells[3] || '',
                      };
                    }
                    return rows;
                  }
                  return {};
                }
                """
            )
        except Exception:  # noqa: BLE001
            return {}

        if not isinstance(raw, dict):
            return {}

        result: dict[str, dict[str, str]] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            result[k] = {
                "required": str(v.get("required", "")),
                "actual": str(v.get("actual", "")),
                "result": str(v.get("result", "")),
            }
        return result

    @staticmethod
    def _parse_duration_to_seconds(text: str) -> int:
        src = text.replace(",", " ").strip()
        h = 0
        m = 0
        s = 0

        h_match = re.search(r"(\d{1,3})\s*시간", src)
        if h_match:
            h = int(h_match.group(1))
        m_match = re.search(r"(\d{1,3})\s*분", src)
        if m_match:
            m = int(m_match.group(1))
        s_match = re.search(r"(\d{1,3})\s*초", src)
        if s_match:
            s = int(s_match.group(1))

        if h == 0 and m == 0 and s == 0:
            hms = re.search(r"(\d{1,3})\s*:\s*(\d{1,2})(?:\s*:\s*(\d{1,2}))?", src)
            if hms:
                if hms.group(3) is None:
                    m = int(hms.group(1))
                    s = int(hms.group(2))
                else:
                    h = int(hms.group(1))
                    m = int(hms.group(2))
                    s = int(hms.group(3))

        return h * 3600 + m * 60 + s

    @staticmethod
    def _parse_percent_value(text: str) -> int:
        m = re.search(r"(\d{1,3})(?:\.\d+)?\s*%", text)
        if not m:
            return 0
        try:
            return int(m.group(1))
        except ValueError:
            return 0

    @staticmethod
    def _format_seconds(value: int) -> str:
        total = max(0, int(value))
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _complete_lesson_steps(self, page: Page) -> LoginResult:
        self._log("차시 단계 자동 진행 시작")
        candidate_page = page
        max_clicks = 120
        step_wait_ms = 8000
        missing_progress_after_next = 0

        for _ in range(max_clicks):
            progress = self._wait_for_step_progress(candidate_page, wait_ms=12000)
            if progress is None:
                # 학습창 내 iframe 또는 다른 팝업 페이지를 다시 탐색
                picked = self._find_page_with_progress(candidate_page.context.pages)
                if picked is not None:
                    candidate_page = picked
                    progress = self._wait_for_step_progress(candidate_page, wait_ms=12000)

            if progress is None:
                if self._has_course_end_notice(candidate_page):
                    return LoginResult(
                        True,
                        "학습 종료 안내 문구 감지: 차시 완료 처리",
                        candidate_page.url,
                        next_lesson_clicked=False,
                    )
                self._dump_player_debug(candidate_page, "progress_not_found")
                return LoginResult(False, "현재/전체 단계 표시를 찾지 못했습니다.", candidate_page.url)

            current_step, total_step = progress
            missing_progress_after_next = 0
            self._log(f"차시 단계 진행: {current_step}/{total_step}")

            blue_ready = self._wait_until_step_blue(candidate_page, current_step, timeout_ms=240000)
            if not blue_ready:
                self._dump_player_debug(candidate_page, "step_not_blue")
                return LoginResult(
                    False,
                    f"{current_step}/{total_step} 단계가 파란색 완료 상태로 확인되지 않았습니다.",
                    candidate_page.url,
                )

            if total_step <= 1 and current_step >= total_step:
                next_clicked = self._click_final_next_if_available(candidate_page)
                moved = self._wait_next_lesson_loaded(candidate_page, current_step, total_step) if next_clicked else False
                return LoginResult(
                    True,
                    f"차시 완료: {current_step}/{total_step}",
                    candidate_page.url,
                    next_lesson_clicked=moved,
                )
            if current_step >= total_step:
                all_blue = self._wait_all_steps_blue(candidate_page, total_step, timeout_ms=180000)
                if not all_blue:
                    self._log("모든 단계 파란색 완료 확인 실패 (화면 상태 점검 필요)")
                    # 빨간 단계가 남아있으면 해당 단계를 먼저 재생합니다.
                    recovered = self._recover_red_step(candidate_page)
                    if recovered:
                        self._log("빨간 단계를 우선 재생하기 위해 해당 단계로 이동합니다.")
                        candidate_page.wait_for_timeout(1500)
                        continue
                # 파란색 집계가 흔들려도 실제 완료 반영을 위해 마지막 Next는 항상 시도합니다.
                next_clicked = self._click_final_next_if_available(candidate_page)
                if not next_clicked:
                    next_clicked = self._click_next_button(candidate_page)
                moved = self._wait_next_lesson_loaded(candidate_page, current_step, total_step) if next_clicked else False
                return LoginResult(
                    True,
                    f"차시 완료: {current_step}/{total_step}",
                    candidate_page.url,
                    next_lesson_clicked=moved,
                )

            self._log("다음 클릭 전 5초 대기")
            candidate_page.wait_for_timeout(step_wait_ms)

            clicked = self._click_next_button(candidate_page)
            if not clicked:
                if self._has_course_end_notice(candidate_page):
                    return LoginResult(
                        True,
                        "학습 종료 안내 문구 감지: 차시 완료 처리",
                        candidate_page.url,
                        next_lesson_clicked=False,
                    )
                return LoginResult(
                    False,
                    f"'다음' 버튼 클릭 실패: {current_step}/{total_step}에서 중단",
                    candidate_page.url,
                )

            progressed = self._wait_progress_change(candidate_page, current_step, total_step)
            if not progressed:
                # 즉시 증분이 없는 강의도 있어 한번 더 읽어보고 끝판단
                check = self._extract_step_progress(candidate_page)
                if check is None:
                    # 전환 애니메이션/프레임 재구성 구간에서 일시적으로 못 읽는 케이스 완화
                    check = self._wait_for_step_progress(candidate_page, wait_ms=12000)
                    if check is None:
                        picked = self._find_page_with_progress(candidate_page.context.pages)
                        if picked is not None:
                            candidate_page = picked
                            check = self._wait_for_step_progress(candidate_page, wait_ms=12000)
                    if check is None:
                        if self._has_course_end_notice(candidate_page):
                            return LoginResult(
                                True,
                                "학습 종료 안내 문구 감지: 차시 완료 처리",
                                candidate_page.url,
                                next_lesson_clicked=False,
                            )
                        missing_progress_after_next += 1
                        if missing_progress_after_next <= 3:
                            self._log(
                                "다음 클릭 후 단계정보 미판독: "
                                f"{missing_progress_after_next}/3 재시도"
                            )
                            candidate_page.wait_for_timeout(6000)
                            continue
                        self._dump_player_debug(candidate_page, "progress_lost_after_next")
                        return LoginResult(False, "다음 클릭 후 단계 정보를 읽지 못했습니다.", candidate_page.url)
                if check[0] == current_step and check[1] == total_step:
                    self._log("단계 증가가 없어 추가 대기/복구 후 재시도를 진행합니다.")
                    advanced = False
                    for retry_idx in range(3):
                        recovered = self._recover_red_step(candidate_page)
                        if recovered:
                            self._log(f"복구 클릭 후 재시도 {retry_idx + 1}/3")
                        else:
                            self._log(f"추가 대기 후 재시도 {retry_idx + 1}/3")
                        candidate_page.wait_for_timeout(step_wait_ms)
                        reclicked = self._click_next_button(candidate_page)
                        if not reclicked:
                            continue
                        reprogress = self._wait_progress_change(candidate_page, current_step, total_step)
                        if reprogress:
                            advanced = True
                            break
                    if not advanced:
                        if self._has_course_end_notice(candidate_page):
                            return LoginResult(
                                True,
                                "학습 종료 안내 문구 감지: 차시 완료 처리",
                                candidate_page.url,
                                next_lesson_clicked=False,
                            )
                        self._dump_player_debug(candidate_page, "recovery_failed")
                        return LoginResult(
                            False,
                            f"다음 클릭 후 단계가 증가하지 않았습니다: {current_step}/{total_step}",
                            candidate_page.url,
                        )

        return LoginResult(False, "최대 클릭 횟수를 초과했습니다.", candidate_page.url)

    def _extract_step_progress(self, page: Page) -> Optional[tuple[int, int]]:
        scopes: list[Any] = [page] + list(page.frames)
        merged: list[tuple[int, int]] = []
        for scope in scopes:
            merged.extend(self._extract_step_progress_from_scope(scope))

        if not merged:
            return None
        merged.sort(key=lambda x: (x[1], x[0]), reverse=True)
        return merged[0]

    @staticmethod
    def _extract_step_progress_from_scope(scope: Any) -> list[tuple[int, int]]:
        # 우선 명시적 페이지 카운터 셀렉터를 탐지합니다.
        for cur_sel, tot_sel in [(".curPage", ".totPage"), (".middle_curPage", ".middle_totPage")]:
            try:
                cur_loc = scope.locator(cur_sel).first
                tot_loc = scope.locator(tot_sel).first
                if cur_loc.count() > 0 and tot_loc.count() > 0:
                    cur_text = cur_loc.inner_text(timeout=1200).strip()
                    tot_text = tot_loc.inner_text(timeout=1200).strip()
                    c = int(re.sub(r"[^0-9]", "", cur_text) or "0")
                    t = int(re.sub(r"[^0-9]", "", tot_text) or "0")
                    if t >= 1 and 1 <= c <= t:
                        return [(c, t)]
            except Exception:  # noqa: BLE001
                pass

        # 형식 예: 01/06, 1/6, 10/20, 01/01
        try:
            body_text = scope.locator("body").inner_text(timeout=2000)
        except Exception:  # noqa: BLE001
            return []

        matches = re.findall(r"(\d{1,3})\s*/\s*(\d{1,3})", body_text)
        candidates: list[tuple[int, int]] = []
        for cur, total in matches:
            try:
                c = int(cur)
                t = int(total)
                if t >= 1 and 1 <= c <= t:
                    candidates.append((c, t))
            except ValueError:
                continue
        return candidates

    def _click_next_button(self, page: Page) -> bool:
        scopes: list[Any] = [page] + list(page.frames)

        # 1) 콘텐츠 프레임(01.html 등) 내부 nextPage를 최우선 시도
        for scope in page.frames:
            try:
                clicked = scope.evaluate(
                    """
                    () => {
                        const btn = document.querySelector('button.nextPage.movePage, button.nextPage');
                        if (btn) {
                          btn.click();
                          return true;
                        }
                        if (typeof nextPage === 'function') {
                          nextPage();
                          return true;
                        }
                        return false;
                    }
                    """
                )
                if clicked:
                    page.wait_for_timeout(700)
                    return True
            except Exception:  # noqa: BLE001
                continue

        # 2) 팝업 본문 네비게이션(다음/Next)
        selectors = [
            '#nextBtn a.next',
            'a[onclick*="doNext"]',
            "button.nextPage",
            'button:has-text("다음")',
            'a:has-text("다음")',
            '[role="button"]:has-text("다음")',
            'span:has-text("다음")',
            'div:has-text("다음")',
        ]
        for scope in scopes:
            if self._click_first_visible(scope, selectors, max_items=25):
                page.wait_for_timeout(600)
                return True

            # 하단 네비게이션 우측 "다음" 텍스트를 직접 찾는 폴백
            clicked = scope.evaluate(
                """
                () => {
                    const nodes = Array.from(document.querySelectorAll('a,button,span,div'));
                    const visibles = nodes.filter((n) => {
                      const txt = (n.textContent || '').trim();
                      if (txt !== '다음') return false;
                      const r = n.getBoundingClientRect();
                      return r.width > 0 && r.height > 0;
                    });
                    if (!visibles.length) return false;
                    visibles.sort((a, b) => b.getBoundingClientRect().y - a.getBoundingClientRect().y);
                    visibles[0].click();
                    return true;
                }
                """
            )
            if clicked:
                page.wait_for_timeout(600)
                return True

            # 콘텐츠 컨트롤러 전용 버튼 강제 클릭
            clicked = scope.evaluate(
                """
                () => {
                    const btn = document.querySelector('button.nextPage.movePage, button.nextPage, .nextPage');
                    if (!btn) return false;
                    btn.click();
                    return true;
                }
                """
            )
            if clicked:
                page.wait_for_timeout(600)
                return True
        return False

    def _wait_progress_change(self, page: Page, prev_cur: int, prev_total: int) -> bool:
        for _ in range(16):
            page.wait_for_timeout(500)
            now = self._extract_step_progress(page)
            if now is None:
                continue
            cur, total = now
            if total != prev_total:
                return True
            if cur > prev_cur:
                return True
            if cur >= total and prev_cur < prev_total:
                return True
        return False

    def _recover_red_step(self, page: Page) -> bool:
        info_frame = self._find_info_bar_frame(page)
        if info_frame is not None:
            try:
                clicked = info_frame.evaluate(
                    """
                    () => {
                        const red = document.querySelector('#frameTable .frameTd.frameRed');
                        if (!red) return false;
                        red.click();
                        return true;
                    }
                    """
                )
                if clicked:
                    self._log("붉은 단계 클릭으로 복구 시도 완료")
                    return True
            except Exception:  # noqa: BLE001
                pass

        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            clicked = scope.evaluate(
                """
                () => {
                    const isRedTone = (cssColor) => {
                      const m = cssColor && cssColor.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                      if (!m) return false;
                      const r = parseInt(m[1], 10);
                      const g = parseInt(m[2], 10);
                      const b = parseInt(m[3], 10);
                      return r >= 170 && g <= 120 && b <= 120;
                    };

                    const elems = Array.from(document.querySelectorAll('a,button,li,span,div,td'));
                    const numbered = elems.filter((el) => {
                      const txt = (el.textContent || '').trim();
                      if (!/^\\d{1,2}$/.test(txt)) return false;
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0;
                    });

                    for (const el of numbered) {
                      const style = window.getComputedStyle(el);
                      const cls = (el.className || '').toString();
                      if (
                        isRedTone(style.backgroundColor) ||
                        isRedTone(style.color) ||
                        /red|frameRed/i.test(cls)
                      ) {
                        el.click();
                        return true;
                      }
                    }
                    return false;
                }
                """
            )
            if clicked:
                self._log("붉은 단계 클릭으로 복구 시도 완료")
                return True
        return False

    def _find_info_bar_frame(self, page: Page) -> Optional[Frame]:
        for fr in page.frames:
            if "/learning/simple/infoBar.do" in fr.url:
                return fr
        return None

    def _wait_until_step_blue(self, page: Page, step_no: int, timeout_ms: int = 45000) -> bool:
        info_frame = self._find_info_bar_frame(page)
        if info_frame is None:
            return True

        ticks = max(1, timeout_ms // 500)
        for _ in range(ticks):
            try:
                is_blue = info_frame.evaluate(
                    """
                    (stepNo) => {
                        const el = document.querySelector('#frame' + stepNo);
                        if (!el) return false;
                        const cls = (el.className || '').toString();
                        return /frameOn/.test(cls);
                    }
                    """,
                    step_no,
                )
                if is_blue:
                    return True
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(500)
        return False

    def _wait_all_steps_blue(self, page: Page, total_step: int, timeout_ms: int = 45000) -> bool:
        info_frame = self._find_info_bar_frame(page)
        if info_frame is None:
            return True

        ticks = max(1, timeout_ms // 500)
        for _ in range(ticks):
            try:
                done = info_frame.evaluate(
                    """
                    (totalStep) => {
                        const items = Array.from(document.querySelectorAll('#frameTable .frameTd'));
                        if (!items.length) return false;
                        const onCnt = items.filter((el) => /frameOn/.test((el.className || '').toString())).length;
                        return onCnt >= totalStep;
                    }
                    """,
                    total_step,
                )
                if done:
                    return True
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(500)
        return False

    def _click_final_next_if_available(self, page: Page) -> bool:
        # 모든 단계 완료 시 우하단 파란 "다음 ( Next )" 버튼이 나타나는 케이스 처리
        selectors = [
            '#nextBtn a.next:has-text("다음")',
            '#nextBtn a.next',
            'a.next:has-text("다음")',
            'a[onclick*="doNext"]',
        ]
        clicked = self._click_first_visible(page, selectors, max_items=10)
        if clicked:
            self._log("우하단 '다음 ( Next )' 버튼 클릭 완료")
            page.wait_for_timeout(1200)
        return clicked

    def _wait_next_lesson_loaded(self, page: Page, prev_cur: int, prev_total: int, timeout_ms: int = 15000) -> bool:
        ticks = max(1, timeout_ms // 500)
        for _ in range(ticks):
            page.wait_for_timeout(500)
            progress = self._extract_step_progress(page)
            if progress is None:
                continue
            cur, total = progress
            # 다음 차시 진입 시 보통 1/x로 돌아오거나, 이전 차시보다 작은 페이지 번호가 됩니다.
            if cur == 1 and total >= 1 and (prev_cur >= prev_total):
                return True
            if cur < prev_cur:
                return True
            if total != prev_total and cur <= total:
                return True
        return False

    @staticmethod
    def _pick_learning_page(current_pages: list[Page], old_pages: list[Page]) -> Optional[Page]:
        old_set = set(old_pages)
        new_pages = [pg for pg in current_pages if pg not in old_set]
        for pg in reversed(new_pages):
            if "/learning/" in pg.url or "popup.do" in pg.url:
                return pg
        for pg in reversed(new_pages):
            if "/usr/classroom/" in pg.url or "player" in pg.url.lower():
                return pg
        for pg in reversed(current_pages):
            if "/learning/" in pg.url or "popup.do" in pg.url:
                return pg
        for pg in reversed(current_pages):
            if "/usr/classroom/" in pg.url or "player" in pg.url.lower():
                return pg
        if new_pages:
            return new_pages[-1]
        return None

    @staticmethod
    def _pick_exam_page(current_pages: list[Page], old_pages: list[Page]) -> Optional[Page]:
        old_set = set(old_pages)
        new_pages = [pg for pg in current_pages if pg not in old_set]
        exam_hints = ["exam", "test", "quiz", "evaluation", "eval", "popup.do", "/learning/"]
        for pg in reversed(new_pages):
            url = pg.url.lower()
            if any(h in url for h in exam_hints):
                return pg
        for pg in reversed(new_pages):
            return pg
        for pg in reversed(current_pages):
            url = pg.url.lower()
            if any(h in url for h in exam_hints):
                return pg
        return None

    def _find_page_with_progress(self, pages: list[Page]) -> Optional[Page]:
        for pg in reversed(pages):
            progress = self._extract_step_progress(pg)
            if progress is not None:
                return pg
        for pg in reversed(pages):
            if "/learning/" in pg.url or "popup.do" in pg.url:
                return pg
        return None

    def _wait_for_step_progress(self, page: Page, wait_ms: int = 8000) -> Optional[tuple[int, int]]:
        ticks = max(1, wait_ms // 500)
        for _ in range(ticks):
            found = self._extract_step_progress(page)
            if found is not None:
                return found
            page.wait_for_timeout(500)
        return None

    def _dump_player_debug(self, page: Page, tag: str) -> None:
        try:
            from pathlib import Path

            out = Path("artifacts") / "player_debug"
            out.mkdir(parents=True, exist_ok=True)
            safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", tag)
            for idx, pg in enumerate(page.context.pages):
                ptag = f"{safe_tag}_page{idx}"
                try:
                    pg.screenshot(path=str(out / f"{ptag}.png"), full_page=True)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    body = pg.locator("body").inner_text(timeout=3000)
                    (out / f"{ptag}.txt").write_text(f"URL={pg.url}\n\n{body}", encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    (out / f"{ptag}.html").write_text(pg.content(), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                for fi, fr in enumerate(pg.frames):
                    try:
                        fbody = fr.locator("body").inner_text(timeout=2000)
                        ftag = f"{ptag}_frame{fi}"
                        (out / f"{ftag}.txt").write_text(f"URL={fr.url}\n\n{fbody}", encoding="utf-8")
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        ftag = f"{ptag}_frame{fi}"
                        (out / f"{ftag}.html").write_text(fr.content(), encoding="utf-8")
                    except Exception:  # noqa: BLE001
                        pass
            self._log(f"디버그 저장: artifacts/player_debug/{safe_tag}.png")
        except Exception:  # noqa: BLE001
            pass

    def _wait_login_result(self, page: Page, dialog_messages: list[str]) -> LoginResult:
        success_text = "메인페이지로 이동 중입니다."
        failure_keywords = ["로그인 실패", "아이디", "비밀번호", "오류", "실패"]
        start_url = self.settings.login_url.rstrip("/")

        if dialog_messages:
            return LoginResult(False, f"로그인 대화상자: {dialog_messages[-1]}", page.url)

        try:
            page.get_by_text(success_text, exact=False).first.wait_for(timeout=6000)
            self._log("성공 안내 문구를 감지했습니다.")
            return LoginResult(True, f"로그인 성공 신호 감지: {success_text}", page.url)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(1500)
        current_url = page.url.rstrip("/")
        if current_url and current_url != start_url:
            if "/login/process.do" in current_url:
                self._log("로그인 process.do URL 감지, 다음 단계에서 메인 전환을 재시도합니다.")
                return LoginResult(True, "로그인 성공 추정 (process.do 감지)", page.url)
            self._log("URL 변경을 감지했습니다.")
            return LoginResult(True, "로그인 성공 추정 (URL 변경)", page.url)

        try:
            page_text = page.locator("body").inner_text(timeout=3000)
        except PlaywrightTimeoutError:
            page_text = ""
        if any(keyword in page_text for keyword in failure_keywords):
            return LoginResult(False, "로그인 실패 문구가 감지되었습니다.", page.url)
        return LoginResult(False, "로그인 결과를 확정하지 못했습니다. 선택자/성공조건 보정이 필요합니다.", page.url)

    @staticmethod
    def _handle_dialog(dialog, dialog_messages: list[str]) -> None:
        message = dialog.message
        dialog_messages.append(message)
        dialog.dismiss()

    @staticmethod
    def _fill_first_visible(page: Page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            locator = page.locator(selector)
            count = min(locator.count(), 5)
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if item.is_visible():
                        item.fill(value)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def _wait_login_form_ready(self, page: Page) -> bool:
        selector = '#j_userId, input[name="j_userId"], input[placeholder*="사번 또는 아이디"]'
        # 로그인 문서가 늦게 interactive 상태로 전환되는 케이스가 있어 하한 대기시간을 확보합니다.
        timeout_ms = min(max(self.settings.timeout_ms, 90000), 180000)
        ticks = max(1, timeout_ms // 500)

        for _ in range(ticks):
            try:
                ready_state = page.evaluate("document.readyState")
                if ready_state not in {"interactive", "complete"}:
                    page.wait_for_timeout(500)
                    continue
                loc = page.locator(selector).first
                if loc.count() > 0 and (loc.is_visible() or loc.is_enabled()):
                    return True
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(500)

        # 마지막 보수적 대기 1회
        try:
            page.wait_for_selector(selector, timeout=8000)
            return True
        except PlaywrightTimeoutError:
            return False

    @staticmethod
    def _is_recoverable_lesson_failure(message: str) -> bool:
        msg = message or ""
        keywords = [
            "파란색 완료 상태로 확인되지 않았습니다",
            "다음 클릭 후 단계 정보를 읽지 못했습니다",
            "다음 클릭 후 단계가 증가하지 않았습니다",
            "현재/전체 단계 표시를 찾지 못했습니다",
        ]
        return any(k in msg for k in keywords)

    @staticmethod
    def _find_classroom_page(pages: list[Page]) -> Optional[Page]:
        for p in reversed(pages):
            try:
                if p.is_closed():
                    continue
                if "/usr/classroom/main.do" in p.url:
                    return p
            except Exception:  # noqa: BLE001
                continue
        return None

    def _recover_learning_popup(
        self,
        current_learning_page: Optional[Page],
        classroom_page: Optional[Page],
        context_pages: list[Page],
    ) -> Optional[Page]:
        if current_learning_page is not None:
            try:
                if not current_learning_page.is_closed():
                    current_learning_page.close()
                    self._log("정체된 학습 팝업을 종료했습니다.")
            except Exception:  # noqa: BLE001
                pass

        target_classroom = classroom_page
        if target_classroom is not None:
            try:
                if target_classroom.is_closed():
                    target_classroom = None
            except Exception:  # noqa: BLE001
                target_classroom = None
        if target_classroom is None:
            target_classroom = self._find_classroom_page(context_pages)
        if target_classroom is None:
            self._log("복구 실패: 강의실 페이지를 찾지 못했습니다.")
            return None

        try:
            target_classroom.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

        self._refresh_classroom_page(target_classroom)
        recovered_page = self._start_learning_from_progress_panel(target_classroom)
        if recovered_page is None:
            self._log("복구 실패: 강의실에서 학습창 재오픈을 못했습니다.")
            return None
        self._log("복구 성공: 이어 학습 팝업을 다시 열었습니다.")
        return recovered_page

    @staticmethod
    def _click_first_visible(page: Any, selectors: list[str], max_items: int = 8) -> bool:
        for selector in selectors:
            locator = page.locator(selector)
            count = min(locator.count(), max_items)
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if item.is_visible():
                        try:
                            item.click(timeout=2500, no_wait_after=True)
                        except Exception:  # noqa: BLE001
                            item.click(timeout=1500, force=True, no_wait_after=True)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    @staticmethod
    def _hover_first_visible(page: Any, selectors: list[str], max_items: int = 8) -> bool:
        for selector in selectors:
            locator = page.locator(selector)
            count = min(locator.count(), max_items)
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if item.is_visible():
                        item.hover(timeout=2000)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False
