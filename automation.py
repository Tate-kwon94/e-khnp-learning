from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
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
        self._last_opened_course_title: str = ""
        self._deferred_exam_course_keys: set[str] = set()
        self._answer_bank_path = Path(getattr(self.settings, "exam_answer_bank_path", "rag/exam_answer_bank.json"))
        self._answer_bank_items: dict[str, dict[str, Any]] = {}
        self._answer_bank_qnorm_index: dict[str, list[dict[str, Any]]] = {}
        self._answer_bank_qsig_index: dict[str, list[dict[str, Any]]] = {}
        self._answer_bank_qsig_optset_index: dict[str, list[dict[str, Any]]] = {}
        self._answer_bank_fuzzy_index: list[dict[str, Any]] = []
        self._deferred_courses_path = Path(
            getattr(self.settings, "exam_deferred_courses_path", ".runtime/deferred_exam_courses.json")
        )
        self._deferred_exam_course_history: dict[str, dict[str, Any]] = {}
        self._exam_quality_report_dir = Path(
            getattr(self.settings, "exam_quality_report_dir", "logs/exam_quality_reports")
        )
        self._question_evidence_fail_streak: dict[str, dict[str, int]] = {}
        self._last_exam_solve_payload: dict[str, Any] = {}
        self._load_answer_bank()
        self._load_deferred_exam_courses()

    def _load_answer_bank(self) -> None:
        self._answer_bank_items = {}
        self._answer_bank_qnorm_index = {}
        self._answer_bank_qsig_index = {}
        self._answer_bank_qsig_optset_index = {}
        self._answer_bank_fuzzy_index = []
        try:
            if not self._answer_bank_path.exists():
                return
            raw = json.loads(self._answer_bank_path.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else None
            if isinstance(items, dict):
                self._answer_bank_items = {
                    str(k): v for k, v in items.items() if isinstance(v, dict)
                }
                self._rebuild_answer_bank_indexes()
        except Exception:  # noqa: BLE001
            self._answer_bank_items = {}
            self._answer_bank_qnorm_index = {}
            self._answer_bank_qsig_index = {}
            self._answer_bank_qsig_optset_index = {}
            self._answer_bank_fuzzy_index = []

    def _load_deferred_exam_courses(self) -> None:
        self._deferred_exam_course_keys = set()
        self._deferred_exam_course_history = {}
        try:
            if not self._deferred_courses_path.exists():
                return
            raw = json.loads(self._deferred_courses_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return

        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        for row in items:
            if not isinstance(row, dict):
                continue
            key = self._course_title_key(str(row.get("title", "")))
            if not key:
                key = str(row.get("key", "")).strip().lower()
            if not key:
                continue
            title = str(row.get("title", "")).strip()
            reason = str(row.get("reason", "")).strip()
            updated_at = str(row.get("updated_at", "")).strip()
            payload = {"key": key, "title": title, "reason": reason, "updated_at": updated_at}
            self._deferred_exam_course_history[key] = payload
            self._deferred_exam_course_keys.add(key)

    def _save_deferred_exam_courses(self) -> None:
        try:
            self._deferred_courses_path.parent.mkdir(parents=True, exist_ok=True)
            rows = sorted(
                self._deferred_exam_course_history.values(),
                key=lambda x: str(x.get("updated_at", "")),
                reverse=True,
            )
            payload = {
                "meta": {
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "count": len(rows),
                },
                "items": rows,
            }
            self._deferred_courses_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"우회 강좌 이력 저장 실패: {exc}")

    def _save_answer_bank(self) -> None:
        try:
            self._answer_bank_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "meta": {
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "count": len(self._answer_bank_items),
                },
                "items": self._answer_bank_items,
            }
            self._answer_bank_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"정답 인덱스 저장 실패: {exc}")

    def _rebuild_answer_bank_indexes(self) -> None:
        q_index: dict[str, list[dict[str, Any]]] = {}
        q_sig_index: dict[str, list[dict[str, Any]]] = {}
        q_sig_optset_index: dict[str, list[dict[str, Any]]] = {}
        fuzzy_index: list[dict[str, Any]] = []
        for item in self._answer_bank_items.values():
            if not isinstance(item, dict):
                continue

            q_norm = str(item.get("question_norm", "")).strip()
            if not q_norm:
                q_norm = self._normalize_answer_text(str(item.get("question", "")))
            if q_norm:
                q_index.setdefault(q_norm, []).append(item)
            q_match_norm = str(item.get("question_match_norm", "")).strip()
            if not q_match_norm:
                q_match_norm = self._normalize_question_text(str(item.get("question", "")))
            q_sig = str(item.get("question_signature", "")).strip()
            if not q_sig:
                q_sig = self._question_signature_from_norm(q_match_norm)
            if q_sig:
                q_sig_index.setdefault(q_sig, []).append(item)

            options = [str(x).strip() for x in item.get("options", []) if str(x).strip()]
            option_norms = [self._normalize_answer_text(x) for x in options]
            option_set_sig = str(item.get("option_set_signature", "")).strip()
            if not option_set_sig:
                option_set_sig = self._option_set_signature_from_norms(option_norms)
            if q_sig and option_set_sig:
                q_sig_optset_index.setdefault(f"{q_sig}||{option_set_sig}", []).append(item)
            option_tokens = [self._token_set_from_norm(x) for x in option_norms]

            ans_opt_norm = str(item.get("answer_option_norm", "")).strip()
            if not ans_opt_norm:
                try:
                    idx_saved = int(item.get("answer_index", 0))
                except Exception:  # noqa: BLE001
                    idx_saved = 0
                if 1 <= idx_saved <= len(option_norms):
                    ans_opt_norm = option_norms[idx_saved - 1]

            fuzzy_index.append(
                {
                    "item": item,
                    "q_norm": q_norm,
                    "q_match_norm": q_match_norm,
                    "q_tokens": self._token_set_from_norm(q_match_norm or q_norm),
                    "option_norms": option_norms,
                    "option_tokens": option_tokens,
                    "answer_opt_norm": ans_opt_norm,
                }
            )
        self._answer_bank_qnorm_index = q_index
        self._answer_bank_qsig_index = q_sig_index
        self._answer_bank_qsig_optset_index = q_sig_optset_index
        self._answer_bank_fuzzy_index = fuzzy_index

    @staticmethod
    def _is_exam_url(url: str) -> bool:
        src = (url or "").lower()
        exam_hints = [
            "/usr/classroom/exampaper/",
            "exampaper",
            "evaluation",
            "eval",
            "quiz",
            "test",
        ]
        return any(h in src for h in exam_hints)

    def _safe_refresh_non_exam_page(self, page: Page, reason: str = "", wait_ms: int = 1200) -> bool:
        try:
            current_url = page.url
        except Exception:  # noqa: BLE001
            current_url = ""

        if self._is_exam_url(current_url):
            self._log(f"시험 페이지로 판단되어 새로고침을 건너뜁니다. reason={reason}")
            return False

        msg = "정체 복구를 위해 비시험 페이지 새로고침을 시도합니다."
        if reason:
            msg += f" reason={reason}"
        self._log(msg)

        try:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(max(200, int(wait_ms)))
            return True
        except Exception:  # noqa: BLE001
            try:
                page.goto(current_url, wait_until="domcontentloaded")
                page.wait_for_timeout(max(200, int(wait_ms)))
                return True
            except Exception:  # noqa: BLE001
                return False

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    @staticmethod
    def _is_page_available(page: Optional[Page]) -> bool:
        if page is None:
            return False
        try:
            return not page.is_closed()
        except Exception:  # noqa: BLE001
            return False

    def _wait_with_page_guard(self, page: Optional[Page], total_ms: int, chunk_ms: int = 30000) -> bool:
        if not self._is_page_available(page):
            return False
        remain = max(0, int(total_ms))
        chunk = max(500, int(chunk_ms))
        while remain > 0:
            if not self._is_page_available(page):
                return False
            step = min(remain, chunk)
            try:
                page.wait_for_timeout(step)
            except Exception:  # noqa: BLE001
                return False
            remain -= step
        return True

    @staticmethod
    def _course_title_key(title: str) -> str:
        return re.sub(r"\s+", " ", str(title or "").strip()).lower()

    def _mark_current_course_exam_deferred(self, reason: str) -> None:
        title = str(self._last_opened_course_title or "").strip()
        if not title:
            title = "제목 확인 실패 과정"
        key = self._course_title_key(title)
        if key:
            self._deferred_exam_course_keys.add(key)
            self._deferred_exam_course_history[key] = {
                "key": key,
                "title": title,
                "reason": str(reason or "").strip(),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._save_deferred_exam_courses()
        self._log(f"과정 우회 등록: {title} / {reason}")

    def _maybe_skip_course_on_low_exam_attempts(
        self,
        classroom_page: Page,
        *,
        timefill_interval_minutes: int = 10,
        timefill_check_limit: int = 24,
    ) -> Optional[LoginResult]:
        threshold = max(0, int(getattr(self.settings, "exam_skip_course_remaining_threshold", 2)))
        if threshold <= 0:
            return None

        status = self._extract_exam_attempt_status(classroom_page)
        attempted = int(status.get("attempted", 0))
        max_attempt = int(status.get("max_attempt", 0))
        remaining = int(status.get("remaining", 0))
        if max_attempt <= 0:
            return None
        if remaining > threshold:
            return None

        self._log(
            "저잔여 응시 우회 조건 감지: "
            f"used={attempted}/{max_attempt}, remaining={remaining}, threshold={threshold}"
        )
        precheck = self._ensure_time_requirement_before_course_skip(
            classroom_page=classroom_page,
            default_interval_minutes=timefill_interval_minutes,
            check_limit=timefill_check_limit,
        )
        if precheck is not None:
            return precheck

        title = str(self._last_opened_course_title or "").strip() or "현재 과정"
        reason = (
            f"종합평가 잔여 응시 {remaining}회(used={attempted}/{max_attempt})로 "
            f"임계치 {threshold}회 이하입니다. 다음 강좌로 우회합니다."
        )
        self._mark_current_course_exam_deferred(reason)
        return LoginResult(True, f"과정 우회: {title} / {reason}", classroom_page.url)

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
                try:
                    debug_page = locals().get("classroom_page", None) or page
                    if self._is_page_available(debug_page):
                        self._dump_player_debug(debug_page, "completion_workflow_exception")
                except Exception:  # noqa: BLE001
                    pass
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

                exam_req = self._extract_exam_requirement_status(classroom_page)
                if bool(exam_req.get("known", False)) and not bool(exam_req.get("has_exam", True)):
                    return LoginResult(
                        True,
                        f"종합평가 없음으로 판단되어 탐침을 생략합니다. {exam_req.get('reason', '')}",
                        classroom_page.url,
                    )

                attempt_guard = self._enforce_exam_attempt_reserve(classroom_page)
                if attempt_guard is not None:
                    return attempt_guard

                exam_page = self._open_comprehensive_exam_popup(classroom_page)
                if exam_page is None:
                    return LoginResult(False, "종합평가 응시 팝업을 찾지 못했습니다.", classroom_page.url)
                self._stabilize_exam_page(exam_page)

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

    def login_and_check_learning_progress(self) -> LoginResult:
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

                classroom_result, classroom_page = self._open_first_course_classroom_internal(page)
                if not classroom_result.success or classroom_page is None:
                    return classroom_result

                self._refresh_classroom_page(classroom_page)
                progress_status = self._extract_learning_progress_status(classroom_page)
                cur = int(progress_status.get("current_percent", 0))
                req = int(progress_status.get("required_percent", 0))
                inc = int(progress_status.get("incomplete_count", 0))
                return LoginResult(
                    True,
                    f"학습진도율 {cur}% / 수료기준 {req}% / 미완료 {inc}개",
                    classroom_page.url,
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

                exam_req = self._extract_exam_requirement_status(classroom_page)
                if bool(exam_req.get("known", False)) and not bool(exam_req.get("has_exam", True)):
                    return LoginResult(
                        True,
                        f"시험평가 수료기준이 공란/(-)으로 확인되어 종합평가가 없는 과정입니다. {exam_req.get('reason', '')}",
                        classroom_page.url,
                    )

                attempt_guard = self._enforce_exam_attempt_reserve(classroom_page)
                if attempt_guard is not None:
                    return attempt_guard

                exam_page = self._open_comprehensive_exam_popup(classroom_page)
                if exam_page is None and self._exam_gate_blocked:
                    return LoginResult(
                        False,
                        "종합평가 응시 제한(학습진도율 80%) 팝업 감지. 먼저 미완료 차시를 진행해 주세요.",
                        classroom_page.url,
                    )
                if exam_page is None:
                    return LoginResult(False, "종합평가 응시 팝업을 찾지 못했습니다.", classroom_page.url)

                max_exam_retries = max(0, min(4, int(getattr(self.settings, "exam_auto_retry_max", 2))))
                retry_requires_answer_index = bool(
                    getattr(self.settings, "exam_retry_requires_answer_index", True)
                )
                retry_no_improve_limit = max(
                    0, min(4, int(getattr(self.settings, "exam_retry_no_improve_limit", 2)))
                )
                retry_round = 0
                last_retry_score: Optional[float] = None
                no_improve_streak = 0

                while True:
                    solve_result = self._auto_solve_exam_with_rag(
                        exam_page=exam_page,
                        dialog_messages=dialog_messages,
                        max_questions=max_questions,
                        rag_top_k=rag_top_k,
                        confidence_threshold=confidence_threshold,
                    )
                    if not solve_result.success:
                        return solve_result

                    self._close_post_exam_transient_pages(
                        pages=page.context.pages,
                        keep_pages=[page, classroom_page],
                    )
                    self._refresh_classroom_page(classroom_page)
                    state = self._extract_course_completion_state(classroom_page)

                    completed_by_status = self._is_course_marked_completed_in_status(page, self._last_opened_course_title)
                    if completed_by_status:
                        self._log(f"수료표(발급)에서 과정 완료 확인: {self._last_opened_course_title}")
                    known = bool(state.get("known", False))
                    completed = bool(state.get("completed", False)) or completed_by_status

                    attempt_no = retry_round + 1
                    attempt_payload = (
                        dict(self._last_exam_solve_payload)
                        if isinstance(self._last_exam_solve_payload, dict)
                        else {}
                    )
                    learn = self._learn_answers_from_result_panel(classroom_page)
                    report = self._write_exam_quality_report(
                        course_title=self._last_opened_course_title,
                        attempt_no=attempt_no,
                        solve_payload=attempt_payload,
                        learn_payload=learn,
                        completion_state=state,
                    )
                    report_path = str(report.get("path", "")).strip()
                    if report_path:
                        self._log(f"시험 파싱 품질 리포트 저장: {report_path}")
                    self._update_evidence_fail_history(
                        question_records=[x for x in attempt_payload.get("question_records", []) if isinstance(x, dict)],
                        report_rows=[x for x in report.get("rows", []) if isinstance(x, dict)],
                        fallback_failed_all=not completed,
                    )

                    if completed:
                        return solve_result
                    if not known:
                        return solve_result

                    retry_reason = str(state.get("reason", "")).strip()
                    if retry_reason:
                        self._log(f"종합평가 재응시 사유: {retry_reason}")
                    else:
                        self._log("종합평가 재응시 사유: 수료 상태 미완료(상세 사유 없음)")

                    current_score = self._extract_exam_score_from_message(retry_reason)
                    if current_score is not None:
                        if last_retry_score is None:
                            self._log(f"종합평가 점수 추적 시작: score={current_score:.1f}")
                        elif current_score > (last_retry_score + 0.01):
                            no_improve_streak = 0
                            self._log(
                                f"종합평가 점수 개선: prev={last_retry_score:.1f} -> now={current_score:.1f}"
                            )
                        else:
                            no_improve_streak += 1
                            self._log(
                                "종합평가 점수 비개선: "
                                f"prev={last_retry_score:.1f}, now={current_score:.1f}, "
                                f"streak={no_improve_streak}/{retry_no_improve_limit}"
                            )
                        last_retry_score = current_score
                    if retry_no_improve_limit > 0 and no_improve_streak >= retry_no_improve_limit:
                        return LoginResult(
                            False,
                            "종합평가 점수 비개선이 연속 감지되어 자동 재응시를 중단합니다. "
                            f"(streak={no_improve_streak}/{retry_no_improve_limit}) / {retry_reason}",
                            classroom_page.url,
                        )

                    if retry_round >= max_exam_retries:
                        return LoginResult(
                            False,
                            f"종합평가 점수 미달로 판단되어 중단합니다. {state.get('reason', '')}",
                            classroom_page.url,
                        )

                    added = int(learn.get("added", 0))
                    found = int(learn.get("found", 0))
                    self._log(
                        "시험 결과 정답지 인덱싱: "
                        f"found={found}, added={added}, detail={learn.get('reason', '')}"
                    )
                    if retry_requires_answer_index and added <= 0:
                        return LoginResult(
                            False,
                            "종합평가 점수 미달이며 정답지 인덱싱 데이터가 없어 자동 재응시를 중단합니다.",
                            classroom_page.url,
                        )

                    attempt_guard = self._enforce_exam_attempt_reserve(classroom_page)
                    if attempt_guard is not None:
                        return LoginResult(
                            False,
                            f"종합평가 점수 미달(재응시 준비) / {attempt_guard.message}",
                            classroom_page.url,
                        )

                    retry_round += 1
                    self._log(f"종합평가 자동 재응시 시작: {retry_round}/{max_exam_retries}")
                    exam_page = self._open_comprehensive_exam_popup(classroom_page)
                    if exam_page is None:
                        return LoginResult(False, "정답지 학습 후 종합평가 재응시 팝업을 찾지 못했습니다.", classroom_page.url)
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
        max_courses = max(1, min(40, int(self.settings.completion_max_courses)))
        handled_courses = 0
        completed_courses = 0
        skipped_courses = 0
        last_url = ""

        while handled_courses < max_courses:
            single = self._login_and_run_completion_workflow_single(
                check_interval_minutes=check_interval_minutes,
                max_timefill_checks=max_timefill_checks,
                safety_max_lessons=safety_max_lessons,
            )
            last_url = single.current_url
            if not single.success:
                if handled_courses > 0 and "수강 가능한 과정의 '학습하기/이어 학습하기' 버튼을 찾지 못했습니다." in single.message:
                    return LoginResult(
                        True,
                        f"모든 수강 가능 과정 처리 완료: 처리 {handled_courses}개 (수료 {completed_courses}, 우회 {skipped_courses})",
                        last_url,
                    )
                return single

            handled_courses += 1
            if str(single.message).startswith("과정 우회:"):
                skipped_courses += 1
                self._log(f"과정 우회 처리 완료: 누적 우회 {skipped_courses}개 / 처리 {handled_courses}개")
            else:
                completed_courses += 1
                self._log(f"과정 수료 완료 확인: 누적 수료 {completed_courses}개 / 처리 {handled_courses}개")

        return LoginResult(
            False,
            f"과정 처리 안전 제한({max_courses}) 도달: 처리 {handled_courses}개 (수료 {completed_courses}, 우회 {skipped_courses})",
            last_url,
        )

    def _login_and_run_completion_workflow_single(
        self,
        check_interval_minutes: int = 10,
        max_timefill_checks: int = 24,
        safety_max_lessons: int = 80,
    ) -> LoginResult:
        if not self.settings.user_id or not self.settings.user_password:
            return LoginResult(False, "환경변수에 EKHNP_USER_ID / EKHNP_USER_PASSWORD를 설정하세요.")

        base_timefill_interval_minutes = max(3, min(10, int(check_interval_minutes)))
        if base_timefill_interval_minutes != int(check_interval_minutes):
            self._log(
                f"학습시간 보충 확인 주기를 {check_interval_minutes}분에서 "
                f"{base_timefill_interval_minutes}분으로 보정합니다."
            )
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
                if not bool(initial_progress.get("known", True)):
                    for retry in range(2):
                        if not self._safe_refresh_non_exam_page(
                            classroom_page, reason=f"학습진도율 0/0 판독 재시도 {retry + 1}/2"
                        ):
                            break
                        initial_progress = self._extract_learning_progress_status(classroom_page)
                        if bool(initial_progress.get("known", True)):
                            break
                progress_already_ok = bool(initial_progress.get("progress_ok", False))
                incomplete_count = int(initial_progress.get("incomplete_count", 0))
                learning_page: Optional[Page] = None
                stage1_bypass_for_timefill = False
                if progress_already_ok and incomplete_count <= 0:
                    self._log("학습진도율/미완료 조건이 이미 충족되어 차시 진행 단계를 생략합니다.")
                else:
                    if incomplete_count > 0:
                        self._log("미완료 차시가 존재하여 우선 '미완료 학습하기'로 진입합니다.")
                        learning_page = self._open_incomplete_lesson_popup(classroom_page)
                    else:
                        learning_page = self._start_learning_from_progress_panel(classroom_page)
                    if learning_page is None:
                        if self._safe_refresh_non_exam_page(classroom_page, reason="진도율 단계 학습창 미오픈"):
                            if incomplete_count > 0:
                                learning_page = self._open_incomplete_lesson_popup(classroom_page)
                            else:
                                learning_page = self._start_learning_from_progress_panel(classroom_page)
                        if learning_page is None:
                            recheck = self._extract_learning_progress_status(classroom_page)
                            exam_req_for_bypass = self._extract_exam_requirement_status(classroom_page)
                            if (
                                int(recheck.get("incomplete_count", 0)) <= 0
                                and bool(exam_req_for_bypass.get("known", False))
                                and not bool(exam_req_for_bypass.get("has_exam", True))
                            ):
                                self._log(
                                    "진도율 단계 학습창 미탐지 + 미완료 없음 + 시험 없음 과정으로 판단되어 "
                                    "시간 보충 단계로 우회합니다."
                                )
                                stage1_bypass_for_timefill = True
                            else:
                                return LoginResult(False, "학습창을 열지 못해 진도율 단계 시작 실패", classroom_page.url)

                    if learning_page is not None:
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
                if (
                    not stage1_bypass_for_timefill
                    and (not progress_status["progress_ok"] or progress_status["incomplete_count"] > 0)
                ):
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

                # 2) 잔여 학습시간 보충
                self._log("2단계: 잔여 학습시간을 수료기준까지 보충합니다.")
                self._refresh_classroom_page(classroom_page)
                time_status = self._extract_study_time_status(classroom_page)
                progress_status = self._extract_learning_progress_status(classroom_page)
                known_required_seconds = (
                    int(time_status.get("required_seconds", 0))
                    if bool(time_status.get("requirement_known", False))
                    else 0
                )
                if not progress_status["progress_ok"] or progress_status["incomplete_count"] > 0:
                    return LoginResult(
                        False,
                        "학습시간 보충 전 학습진도율 기준 미충족(미완료 차시 존재 포함)으로 중단합니다.",
                        classroom_page.url,
                    )

                if not self._is_time_requirement_satisfied(
                    time_status,
                    required_seconds_floor=known_required_seconds,
                ):
                    self._log("학습시간이 부족해 1차시 재생 유지 모드로 진입합니다.")
                    keepalive_page = self._open_first_lesson_popup_for_timefill(classroom_page)
                    if keepalive_page is None:
                        return LoginResult(False, "학습시간 보충용 1차시 학습창을 열지 못했습니다.", classroom_page.url)

                    for idx in range(check_limit):
                        wait_minutes = self._decide_timefill_check_interval_minutes(
                            time_status=time_status,
                            default_minutes=base_timefill_interval_minutes,
                        )
                        self._log(
                            f"학습시간 보충 대기: {idx + 1}/{check_limit} "
                            f"(다음 확인 {wait_minutes}분 후, 남은시간 "
                            f"{self._format_seconds(int(time_status.get('shortage_seconds', 0)))} )"
                        )
                        waited = self._wait_with_page_guard(keepalive_page, wait_minutes * 60 * 1000)
                        if not waited:
                            self._log("학습시간 보충 대기 중 학습창 종료 감지: 학습창 재오픈을 시도합니다.")
                            keepalive_page = self._open_first_lesson_popup_for_timefill(classroom_page)
                            if keepalive_page is None:
                                return LoginResult(
                                    False,
                                    "학습시간 보충 대기 중 학습창이 종료되어 재오픈에 실패했습니다.",
                                    classroom_page.url,
                                )
                            continue
                        self._refresh_classroom_page(classroom_page)
                        time_status = self._extract_study_time_status(classroom_page)
                        if bool(time_status.get("requirement_known", False)):
                            known_required_seconds = max(
                                known_required_seconds,
                                int(time_status.get("required_seconds", 0)),
                            )
                        progress_status = self._extract_learning_progress_status(classroom_page)
                        if not progress_status["progress_ok"] or progress_status["incomplete_count"] > 0:
                            return LoginResult(
                                False,
                                "학습시간 보충 중 학습진도율 기준 미충족(미완료 차시 존재 포함)으로 중단합니다.",
                                classroom_page.url,
                            )
                        if self._is_time_requirement_satisfied(
                            time_status,
                            required_seconds_floor=known_required_seconds,
                        ):
                            self._log("학습시간 보충 완료: 수료기준 충족")
                            break
                    else:
                        return LoginResult(
                            False,
                            "학습시간 보충 체크 제한 횟수에 도달했습니다. (기준 충족 미확인)",
                            classroom_page.url,
                        )
                else:
                    self._log("학습시간 수료기준이 이미 충족되어 보충 단계를 생략합니다.")

                # 3) 시험평가 (과정별 수료기준이 공란/'-'이면 시험 없음으로 간주)
                exam_req = self._extract_exam_requirement_status(classroom_page)
                should_run_exam = True
                if bool(exam_req.get("known", False)) and not bool(exam_req.get("has_exam", True)):
                    should_run_exam = False
                    self._log(
                        f"3단계: 시험평가 수료기준이 공란/(-)으로 확인되어 종합평가를 생략합니다. {exam_req.get('reason', '')}"
                    )
                else:
                    self._log("3단계: 종합평가 응시를 진행합니다.")

                if should_run_exam:
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
                    skip_course = self._maybe_skip_course_on_low_exam_attempts(
                        classroom_page=classroom_page,
                        timefill_interval_minutes=base_timefill_interval_minutes,
                        timefill_check_limit=check_limit,
                    )
                    if skip_course is not None:
                        return skip_course
                    attempt_guard = self._enforce_exam_attempt_reserve(classroom_page)
                    if attempt_guard is not None:
                        return attempt_guard
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
                        skip_course = self._maybe_skip_course_on_low_exam_attempts(
                            classroom_page=classroom_page,
                            timefill_interval_minutes=base_timefill_interval_minutes,
                            timefill_check_limit=check_limit,
                        )
                        if skip_course is not None:
                            return skip_course
                        attempt_guard = self._enforce_exam_attempt_reserve(classroom_page)
                        if attempt_guard is not None:
                            return attempt_guard
                        exam_page = self._open_comprehensive_exam_popup(classroom_page)
                    if exam_page is None:
                        return LoginResult(False, "종합평가 응시 팝업을 찾지 못했습니다.", classroom_page.url)

                    max_exam_retries = max(0, min(4, int(getattr(self.settings, "exam_auto_retry_max", 2))))
                    retry_requires_answer_index = bool(
                        getattr(self.settings, "exam_retry_requires_answer_index", True)
                    )
                    retry_no_improve_limit = max(
                        0, min(4, int(getattr(self.settings, "exam_retry_no_improve_limit", 2)))
                    )
                    retry_round = 0
                    last_retry_score: Optional[float] = None
                    no_improve_streak = 0
                    while True:
                        solve_exam = self._auto_solve_exam_with_rag(
                            exam_page=exam_page,
                            dialog_messages=dialog_messages,
                            max_questions=60,
                            rag_top_k=self.settings.rag_top_k,
                            confidence_threshold=self.settings.rag_conf_threshold,
                        )
                        if not solve_exam.success:
                            return solve_exam
                        self._close_post_exam_transient_pages(
                            pages=page.context.pages,
                            keep_pages=[page, classroom_page],
                        )
                        self._refresh_classroom_page(classroom_page)
                        completion_guard = self._ensure_course_completed(classroom_page)
                        completed_by_guard = completion_guard is None
                        completed_by_status = self._is_course_marked_completed_in_status(page, self._last_opened_course_title)
                        completed = completed_by_guard or completed_by_status
                        if completed_by_status:
                            self._log(
                                f"시험평가 후 수료표(발급)에서 과정 완료 확인: {self._last_opened_course_title}"
                            )

                        attempt_no = retry_round + 1
                        attempt_payload = (
                            dict(self._last_exam_solve_payload)
                            if isinstance(self._last_exam_solve_payload, dict)
                            else {}
                        )
                        completion_state = {
                            "known": True,
                            "completed": bool(completed),
                            "reason": "" if completion_guard is None else str(completion_guard.message),
                        }
                        learn = self._learn_answers_from_result_panel(classroom_page)
                        report = self._write_exam_quality_report(
                            course_title=self._last_opened_course_title,
                            attempt_no=attempt_no,
                            solve_payload=attempt_payload,
                            learn_payload=learn,
                            completion_state=completion_state,
                        )
                        report_path = str(report.get("path", "")).strip()
                        if report_path:
                            self._log(f"시험 파싱 품질 리포트 저장: {report_path}")
                        self._update_evidence_fail_history(
                            question_records=[x for x in attempt_payload.get("question_records", []) if isinstance(x, dict)],
                            report_rows=[x for x in report.get("rows", []) if isinstance(x, dict)],
                            fallback_failed_all=not completed,
                        )

                        if completed:
                            self._log(f"시험평가 합격 확인: attempt_round={retry_round + 1}")
                            break

                        retry_reason = completion_guard.message.strip()
                        self._log(f"종합평가 재응시 사유: {retry_reason}")
                        current_score = self._extract_exam_score_from_message(retry_reason)
                        if current_score is not None:
                            if last_retry_score is None:
                                self._log(f"종합평가 점수 추적 시작: score={current_score:.1f}")
                            elif current_score > (last_retry_score + 0.01):
                                no_improve_streak = 0
                                self._log(
                                    f"종합평가 점수 개선: prev={last_retry_score:.1f} -> now={current_score:.1f}"
                                )
                            else:
                                no_improve_streak += 1
                                self._log(
                                    "종합평가 점수 비개선: "
                                    f"prev={last_retry_score:.1f}, now={current_score:.1f}, "
                                    f"streak={no_improve_streak}/{retry_no_improve_limit}"
                                )
                            last_retry_score = current_score

                        if retry_no_improve_limit > 0 and no_improve_streak >= retry_no_improve_limit:
                            return LoginResult(
                                False,
                                f"{completion_guard.message} / 점수 비개선이 연속 감지되어 자동 재응시를 중단합니다. "
                                f"(streak={no_improve_streak}/{retry_no_improve_limit})",
                                classroom_page.url,
                            )

                        if retry_round >= max_exam_retries:
                            return LoginResult(
                                False,
                                f"{completion_guard.message} / 재응시 제한 도달({retry_round}/{max_exam_retries})",
                                classroom_page.url,
                            )

                        added = int(learn.get("added", 0))
                        found = int(learn.get("found", 0))
                        self._log(
                            "시험 결과 정답지 인덱싱: "
                            f"found={found}, added={added}, detail={learn.get('reason', '')}"
                        )

                        if retry_requires_answer_index and added <= 0:
                            return LoginResult(
                                False,
                                f"{completion_guard.message} / 정답지 인덱싱 데이터가 없어 자동 재응시를 중단합니다.",
                                classroom_page.url,
                            )

                        skip_course = self._maybe_skip_course_on_low_exam_attempts(
                            classroom_page=classroom_page,
                            timefill_interval_minutes=base_timefill_interval_minutes,
                            timefill_check_limit=check_limit,
                        )
                        if skip_course is not None:
                            return skip_course
                        attempt_guard = self._enforce_exam_attempt_reserve(classroom_page)
                        if attempt_guard is not None:
                            return LoginResult(
                                False,
                                f"{completion_guard.message} / {attempt_guard.message}",
                                classroom_page.url,
                            )

                        retry_round += 1
                        self._log(f"종합평가 자동 재응시 시작: {retry_round}/{max_exam_retries}")
                        exam_page = self._open_comprehensive_exam_popup(classroom_page)
                        if exam_page is None:
                            return LoginResult(False, "정답지 학습 후 종합평가 재응시 팝업을 찾지 못했습니다.", classroom_page.url)

                # 4) 최종 수료 상태 확인
                self._refresh_classroom_page(classroom_page)
                completion_guard = self._ensure_course_completed(classroom_page)
                if completion_guard is not None:
                    return completion_guard
                return LoginResult(
                    True,
                    "수료 시나리오 완료: 진도율→학습시간→시험평가 기준 충족",
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

                completed_lessons = 0
                completed_courses = 0

                while completed_lessons < safety_max_lessons:
                    if stop_rule == "manual" and manual_lesson_limit and completed_lessons >= manual_lesson_limit:
                        return LoginResult(
                            True,
                            f"요청한 반복 수 완료: 총 {completed_lessons}개 차시 완료",
                            page.url,
                        )

                    ensure_status = self._ensure_learning_status_page(page)
                    if not ensure_status.success:
                        if completed_lessons > 0:
                            return LoginResult(
                                True,
                                f"진행 완료: 총 {completed_lessons}개 차시, {completed_courses}개 과정 완료",
                                page.url,
                            )
                        return ensure_status

                    if not self._has_startable_course(page):
                        if completed_lessons > 0:
                            return LoginResult(
                                True,
                                f"모든 수강 가능 과정 완료: 총 {completed_lessons}개 차시, {completed_courses}개 과정 완료",
                                page.url,
                            )
                        return LoginResult(False, "수강 가능한 과정의 학습 버튼을 찾지 못했습니다.", page.url)

                    enter_result, classroom_page, lesson_page = self._enter_first_course_with_context_internal(page)
                    if not enter_result.success or lesson_page is None:
                        if completed_lessons > 0 and not self._has_startable_course(page):
                            return LoginResult(
                                True,
                                f"모든 수강 가능 과정 완료: 총 {completed_lessons}개 차시, {completed_courses}개 과정 완료",
                                page.url,
                            )
                        return enter_result

                    completed_courses += 1
                    self._log(f"{completed_courses}번째 과정 학습 시작")

                    detected_total_lessons: Optional[int] = self._detected_total_lessons
                    if detected_total_lessons:
                        self._log(f"현재 과정 총 차시 수(강의실 학습하기 버튼 기준): {detected_total_lessons}차시")
                    recovery_attempts = 0
                    course_completed_lessons = 0

                    while completed_lessons < safety_max_lessons:
                        if stop_rule in {"auto", "detected_total"} and detected_total_lessons is None:
                            detected_total_lessons = self._extract_total_lessons(lesson_page)
                            if detected_total_lessons:
                                self._log(f"현재 과정 총 차시 수 감지: {detected_total_lessons}차시")
                            elif stop_rule == "detected_total":
                                self._close_if_transient_page(lesson_page, page)
                                self._close_if_transient_page(classroom_page, page)
                                return LoginResult(
                                    False,
                                    "총 차시 수를 감지하지 못했습니다. 'Next 버튼 기준' 모드로 실행해 주세요.",
                                    lesson_page.url,
                                )

                        if (
                            stop_rule == "detected_total"
                            and detected_total_lessons
                            and course_completed_lessons >= detected_total_lessons
                        ):
                            self._log(
                                f"현재 과정 감지된 총 차시 완료: {course_completed_lessons}/{detected_total_lessons}"
                            )
                            break

                        if stop_rule == "manual" and manual_lesson_limit and completed_lessons >= manual_lesson_limit:
                            self._close_if_transient_page(lesson_page, page)
                            self._close_if_transient_page(classroom_page, page)
                            return LoginResult(
                                True,
                                f"요청한 반복 수 완료: 총 {completed_lessons}개 차시 완료",
                                lesson_page.url,
                            )

                        complete_result = self._complete_lesson_steps(lesson_page)
                        if not complete_result.success:
                            if self._is_recoverable_lesson_failure(complete_result.message) and recovery_attempts < 3:
                                recovery_attempts += 1
                                self._log(f"차시 정체 감지: 팝업 재시작 복구 {recovery_attempts}/3")
                                recovered_page = self._recover_learning_popup(
                                    current_learning_page=lesson_page,
                                    classroom_page=classroom_page,
                                    context_pages=page.context.pages,
                                )
                                if recovered_page is not None:
                                    lesson_page = recovered_page
                                    continue
                            self._close_if_transient_page(lesson_page, page)
                            self._close_if_transient_page(classroom_page, page)
                            if completed_lessons > 0:
                                return LoginResult(
                                    False,
                                    f"{completed_lessons}개 차시 완료 후 중단: {complete_result.message}",
                                    complete_result.current_url,
                                )
                            return complete_result

                        recovery_attempts = 0
                        completed_lessons += 1
                        course_completed_lessons += 1
                        self._log(
                            f"차시 완료 누적: 전체 {completed_lessons}개 / 현재 과정 {course_completed_lessons}개"
                        )

                        if stop_rule == "manual" and manual_lesson_limit and completed_lessons >= manual_lesson_limit:
                            self._close_if_transient_page(lesson_page, page)
                            self._close_if_transient_page(classroom_page, page)
                            return LoginResult(
                                True,
                                f"요청한 반복 수 완료: 총 {completed_lessons}개 차시 완료",
                                complete_result.current_url,
                            )

                        if not complete_result.next_lesson_clicked:
                            self._log("현재 과정 완료로 판단: 강의 목록으로 돌아가 다음 과정을 확인합니다.")
                            break

                        self._log("다음 차시로 이동, 로딩 대기")
                        lesson_page.wait_for_timeout(2500)

                    self._close_if_transient_page(lesson_page, page)
                    self._close_if_transient_page(classroom_page, page)

                    ensure_status = self._ensure_learning_status_page(page)
                    if not ensure_status.success:
                        if completed_lessons > 0:
                            return LoginResult(
                                True,
                                f"진행 완료: 총 {completed_lessons}개 차시, {completed_courses}개 과정 완료",
                                page.url,
                            )
                        return ensure_status

                    if not self._has_startable_course(page):
                        return LoginResult(
                            True,
                            f"모든 수강 가능 과정 완료: 총 {completed_lessons}개 차시, {completed_courses}개 과정 완료",
                            page.url,
                        )
                    self._log("다음 수강 가능 과정이 감지되어 이어서 진행합니다.")

                return LoginResult(False, f"안전 제한({safety_max_lessons})에 도달해 중단", page.url)
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
                      const compact = txt.replace(/\\s+/g, '');
                      return compact === '학습하기' || compact === '이어학습하기';
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
            self._log("메뉴 클릭 실패: '나의 학습현황' 직접 URL 이동을 시도합니다.")
            origin_match = re.match(r"^https?://[^/]+", page.url)
            origin = origin_match.group(0) if origin_match else self.settings.base_url.rstrip("/")
            direct_url = f"{origin}/usr/member/dash/detail.do"
            try:
                page.goto(direct_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001
                return LoginResult(False, "'나의 학습현황' 메뉴 클릭 실패", page.url)

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1200)
        current_url = page.url.lower()
        if "/usr/member/dash/detail.do" in current_url:
            return LoginResult(True, "나의 학습현황 페이지 이동 성공(URL 확인)", page.url)
        body_text = page.locator("body").inner_text(timeout=5000)
        if "나의 학습현황" in body_text or "My Learning" in body_text:
            return LoginResult(True, "나의 학습현황 페이지 이동 성공", page.url)
        return LoginResult(False, "나의 학습현황 클릭 후 이동 확인 실패", page.url)

    def _enter_first_course(self, page: Page) -> LoginResult:
        result, _ = self._enter_first_course_internal(page)
        return result

    def _find_first_startable_course(self, page: Page) -> tuple[str, Optional[Any]]:
        button_selector = (
            'a:has-text("학습하기"), '
            'a:has-text("학습 하기"), '
            'a:has-text("이어 학습하기"), '
            'a:has-text("이어 학습 하기"), '
            'a:has-text("이어학습하기"), '
            'button:has-text("학습하기"), '
            'button:has-text("학습 하기"), '
            'button:has-text("이어 학습하기"), '
            'button:has-text("이어 학습 하기"), '
            'button:has-text("이어학습하기"), '
            'input[value*="학습하기"], '
            'input[value*="학습 하기"], '
            'input[value*="이어 학습하기"], '
            'input[value*="이어 학습 하기"], '
            'input[value*="이어학습하기"]'
        )
        rows = page.locator("table tbody tr")
        row_count = min(rows.count(), 80)
        for idx in range(row_count):
            row = rows.nth(idx)
            button = row.locator(button_selector).first
            if button.count() == 0:
                continue
            try:
                if not button.is_visible():
                    continue
            except Exception:  # noqa: BLE001
                continue

            title = ""
            cells = row.locator("td")
            cell_count = min(cells.count(), 8)
            for ci in [3, 2, 1, 0, 4, 5, 6, 7]:
                if ci >= cell_count:
                    continue
                try:
                    text = cells.nth(ci).inner_text(timeout=800).strip()
                except Exception:  # noqa: BLE001
                    continue
                if text and text not in {"-", "학습하기", "학습 하기", "이어 학습하기", "이어 학습 하기", "이어학습하기"}:
                    title = text
                    break
            if not title:
                title = f"{idx + 1}번째 과정"
            title_key = self._course_title_key(title)
            if title_key and title_key in self._deferred_exam_course_keys:
                history = self._deferred_exam_course_history.get(title_key, {})
                reason = str(history.get("reason", "")).strip()
                if reason:
                    self._log(f"우회 등록 과정 스킵: {title} / reason={reason}")
                else:
                    self._log(f"우회 등록 과정 스킵: {title}")
                continue
            return title, button
        return "", None

    def _has_startable_course(self, page: Page) -> bool:
        try:
            page.wait_for_selector("table tbody tr", timeout=min(self.settings.timeout_ms, 10000))
        except PlaywrightTimeoutError:
            return False
        _, button = self._find_first_startable_course(page)
        return button is not None

    def _ensure_learning_status_page(self, page: Page) -> LoginResult:
        try:
            has_status_text = page.evaluate(
                """
                () => {
                  const txt = (document.body && document.body.innerText || '');
                  return txt.includes('나의 학습현황') || txt.includes('My Learning');
                }
                """
            )
        except Exception:  # noqa: BLE001
            has_status_text = False

        if has_status_text:
            return LoginResult(True, "나의 학습현황 페이지 확인", page.url)
        open_result = self._open_learning_status(page)
        if open_result.success:
            return open_result

        # 상단 메뉴가 가려지는 화면(강의실/팝업 전환)에서는 직접 URL 이동으로 복구합니다.
        try:
            status_url = f"{self.settings.base_url.rstrip('/')}/usr/member/dash/detail.do"
            page.goto(status_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            body_text = page.locator("body").inner_text(timeout=3000)
            if "나의 학습현황" in body_text or "My Learning" in body_text:
                return LoginResult(True, "나의 학습현황 페이지 직접 이동 성공", page.url)
        except Exception:  # noqa: BLE001
            pass
        return open_result

    def _close_if_transient_page(self, target: Optional[Page], root_page: Page) -> None:
        if target is None or target == root_page:
            return
        try:
            if target.is_closed():
                return
        except Exception:  # noqa: BLE001
            return
        try:
            target.close()
        except Exception:  # noqa: BLE001
            pass

    def _is_course_marked_completed_in_status(self, root_page: Page, course_title: str) -> bool:
        title = str(course_title or "").strip()
        if not title:
            return False

        status_url = f"{self.settings.base_url.rstrip('/')}/usr/member/dash/detail.do"
        probe_page: Optional[Page] = None
        try:
            probe_page = root_page.context.new_page()
            probe_page.set_default_timeout(min(self.settings.timeout_ms, 15000))
            probe_page.goto(status_url, wait_until="domcontentloaded")
            probe_page.wait_for_timeout(800)
            found = bool(
                probe_page.evaluate(
                    """
                    ({ title }) => {
                      const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                      const rows = Array.from(document.querySelectorAll('tr[id^="_courseresult_"]'));
                      for (const tr of rows) {
                        const style = window.getComputedStyle(tr);
                        if (style && style.display === 'none') continue;
                        const rowText = normalize(tr.innerText || tr.textContent || '');
                        if (!rowText) continue;
                        if (!rowText.includes(title)) continue;
                        if (!rowText.includes('발급')) continue;
                        return true;
                      }
                      return false;
                    }
                    """,
                    {"title": title},
                )
            )
            return found
        except Exception:  # noqa: BLE001
            return False
        finally:
            if probe_page is not None:
                try:
                    probe_page.close()
                except Exception:  # noqa: BLE001
                    pass

    def _enter_first_course_with_context_internal(
        self, page: Page
    ) -> tuple[LoginResult, Optional[Page], Optional[Page]]:
        classroom_result, classroom_page = self._open_first_course_classroom_internal(page)
        if not classroom_result.success or classroom_page is None:
            return classroom_result, None, None

        learning_page = self._start_learning_from_progress_panel(classroom_page)
        if learning_page is not None:
            return (
                LoginResult(
                    True,
                    f"학습 시작 클릭 성공(학습진행현황): {classroom_result.message.replace('강의실 진입 성공: ', '')}",
                    learning_page.url,
                ),
                classroom_page,
                learning_page,
            )
        return (
            LoginResult(
                False,
                "강의실 진입은 성공했지만 학습진행현황의 하단 '학습하기' 버튼 클릭 실패",
                classroom_page.url,
            ),
            classroom_page,
            None,
        )

    def _open_first_course_classroom_internal(self, page: Page) -> tuple[LoginResult, Optional[Page]]:
        self._log("수강과정 목록 로딩 대기")
        try:
            page.wait_for_selector("table tbody tr", timeout=min(self.settings.timeout_ms, 15000))
        except PlaywrightTimeoutError:
            return LoginResult(False, "수강과정 테이블을 찾지 못했습니다.", page.url), None

        first_title, first_row_button = self._find_first_startable_course(page)
        if first_row_button is None:
            return LoginResult(False, "수강 가능한 과정의 '학습하기/이어 학습하기' 버튼을 찾지 못했습니다.", page.url), None

        self._log(f"수강 가능한 첫 과정: {first_title or '제목 확인 실패'}")
        self._last_opened_course_title = str(first_title or "").strip()

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
        result, _, learning_page = self._enter_first_course_with_context_internal(page)
        return result, learning_page

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
            'div:has-text("학습진행현황") a:has-text("학습 하기")',
            'div:has-text("학습진행현황") a:has-text("이어 학습하기")',
            'div:has-text("학습진행현황") a:has-text("이어 학습 하기")',
            'div:has-text("학습진행현황") button:has-text("학습하기")',
            'div:has-text("학습진행현황") button:has-text("학습 하기")',
            'div:has-text("학습진행현황") button:has-text("이어 학습하기")',
            'div:has-text("학습진행현황") button:has-text("이어 학습 하기")',
            'div:has-text("학습진행현황") input[value*="학습하기"]',
            'div:has-text("학습진행현황") input[value*="학습 하기"]',
            'div:has-text("학습진행현황") input[value*="이어 학습하기"]',
            'div:has-text("학습진행현황") input[value*="이어 학습 하기"]',
            'a[onclick*="doStudyPopup"]:has-text("이어 학습하기")',
            'a[onclick*="doStudyPopup"]:has-text("이어 학습 하기")',
            'a[onclick*="doStudyPopup"]',
            'a[onclick*="doLearning"]',
            'span[onclick*="doFirstScript"]',
            'a:has-text("학습 하기")',
            'button:has-text("학습 하기")',
            'a:has-text("학습하기")',
            'button:has-text("학습하기")',
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
                  const compact = txt.replace(/\\s+/g, '');
                  return compact === '학습하기' || compact === '이어학습하기';
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

    def _open_comprehensive_exam_popup(self, page: Page, allow_refresh_retry: bool = True) -> Optional[Page]:
        self._exam_gate_blocked = False
        self._log("강의실 '종합평가 응시하기' 버튼 클릭 시도")
        page.wait_for_timeout(1000)
        before_pages = list(page.context.pages)
        before_url = page.url

        try:
            page.locator("text=종합평가").first.scroll_into_view_if_needed(timeout=2500)
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass

        selectors = [
            'div:has-text("종합평가") a:has-text("응시하기")',
            'div:has-text("종합평가") button:has-text("응시하기")',
            'div:has-text("종합평가") span:has-text("응시하기")',
            'div:has-text("종합평가") a:has-text("응시하기 click")',
            'div:has-text("종합평가") a:has-text("응시하기click")',
            'div:has-text("종합평가") button:has-text("응시하기 click")',
            'div:has-text("종합평가") button:has-text("응시하기click")',
            'div:has-text("종합평가") a:has-text("평가응시")',
            'div:has-text("종합평가") button:has-text("평가응시")',
            'div:has-text("종합평가") a:has-text("재응시")',
            'div:has-text("종합평가") button:has-text("재응시")',
            'div:has-text("종합평가") a[onclick*="Eval"]',
            'div:has-text("종합평가") button[onclick*="Eval"]',
            'div:has-text("종합평가") a[onclick*="Exam"]',
            'div:has-text("종합평가") button[onclick*="Exam"]',
            'div:has-text("종합평가") a:has-text("click")',
            'div:has-text("종합평가") button:has-text("click")',
            'div:has-text("종합평가") span:has-text("click")',
            'div:has-text("종합평가") input[value*="응시"]',
            'div:has-text("시험평가") a:has-text("응시하기")',
            'div:has-text("시험평가") button:has-text("응시하기")',
            'div:has-text("시험평가") a:has-text("응시하기 click")',
            'div:has-text("시험평가") a:has-text("응시하기click")',
            'div:has-text("시험평가") a:has-text("재응시")',
            'div:has-text("시험평가") button:has-text("재응시")',
            'div:has-text("시험평가") a[onclick*="Eval"]',
            'div:has-text("시험평가") button[onclick*="Eval"]',
            'div:has-text("시험평가") a[onclick*="Exam"]',
            'div:has-text("시험평가") button[onclick*="Exam"]',
            'a:has-text("종합평가")',
            'a:has-text("응시하기")',
            'button:has-text("응시하기")',
            'a:has-text("평가응시")',
            'button:has-text("평가응시")',
            'a:has-text("재응시")',
            'button:has-text("재응시")',
            'a[onclick*="Eval"]',
            'button[onclick*="Eval"]',
            'a[onclick*="Exam"]',
            'button[onclick*="Exam"]',
            'input[value*="응시하기"]',
        ]
        clicked = False
        popup_page: Optional[Page] = None
        click_scopes: list[Any] = [page] + list(page.frames)
        for scope in click_scopes:
            clicked = self._click_first_visible(scope, selectors, max_items=40)
            if clicked:
                break

        if not clicked:
            for scope in click_scopes:
                try:
                    clicked = bool(
                        scope.evaluate(
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
                                || txt.includes('응시하기 click')
                                || (txt.includes('응시') && txt.includes('click'))
                                || txt.includes('재응시')
                                || txt.includes('평가응시')
                                || txt.includes('시험응시');
                              const hasExamAttr = (el) => {
                                const onclick = normalize(el.getAttribute('onclick'));
                                const href = normalize(el.getAttribute('href'));
                                const attrs = `${onclick} ${href}`.toLowerCase();
                                return attrs.includes('eval') || attrs.includes('exam') || attrs.includes('test');
                              };

                              for (const title of titleNodes) {
                                const tt = normalize(title.textContent);
                                if (!isExamTitle(tt)) continue;
                                let container = title;
                                for (let depth = 0; depth < 8 && container; depth++) {
                                  const cands = Array.from(
                                    container.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                                  );
                                  const target = cands.find((el) => {
                                    if (!isVisible(el)) return false;
                                    const txt = normalize(el.textContent || el.value);
                                    return isExamButton(txt) || hasExamAttr(el);
                                  });
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
                                return isVisible(el) && (isExamButton(txt) || hasExamAttr(el));
                              });
                              if (fallback) {
                                fallback.click();
                                return true;
                              }
                              return false;
                            }
                            """
                        )
                    )
                except Exception:  # noqa: BLE001
                    clicked = False
                if clicked:
                    break

        if clicked:
            page.wait_for_timeout(1500)
            if popup_page is not None:
                popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
                self._log(f"종합평가 팝업 감지: {popup_page.url}")
                return popup_page

            # 사이트별 구현에서 "응시하기" 클릭 후 사전 안내 레이어(동의+시험 시작하기)가 1단계 더 필요할 수 있음.
            for _ in range(20):
                started_page = self._start_exam_from_notice_layer(page, before_pages, before_url)
                if started_page is not None:
                    return started_page

                now_url = page.url.lower()
                if now_url != before_url.lower() and any(
                    hint in now_url for hint in ["exam", "test", "quiz", "evaluation", "eval"]
                ):
                    self._log(f"종합평가 페이지 직접 이동 감지: {page.url}")
                    return page

                picked = self._pick_exam_page(page.context.pages, before_pages)
                if picked is not None and picked != page:
                    self._log(f"종합평가 창 선택: pages={len(page.context.pages)} / url={picked.url}")
                    return picked
                page.wait_for_timeout(500)

            now_url = page.url.lower()
            if now_url != before_url.lower() and any(
                hint in now_url for hint in ["exam", "test", "quiz", "evaluation", "eval"]
            ):
                self._log(f"종합평가 페이지 직접 이동 감지: {page.url}")
                return page

            picked = self._pick_exam_page(page.context.pages, before_pages)
            if picked is not None and picked != page:
                self._log(f"종합평가 창 선택: pages={len(page.context.pages)} / url={picked.url}")
                return picked

        if self._dismiss_exam_progress_gate_notice(page):
            self._exam_gate_blocked = True
            self._log("종합평가 응시 제한 알림 감지: 미완료 차시를 먼저 진행합니다.")
            return None

        if allow_refresh_retry and self._safe_refresh_non_exam_page(page, reason="종합평가 응시 버튼 장시간 미탐지"):
            return self._open_comprehensive_exam_popup(page, allow_refresh_retry=False)

        self._log("종합평가 응시 팝업을 찾지 못했습니다.")
        return None

    def _start_exam_from_notice_layer(
        self, page: Page, before_pages: list[Page], before_url: str
    ) -> Optional[Page]:
        clicked = False
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            try:
                clicked = bool(
                    scope.evaluate(
                        """
                        () => {
                          const pop = document.querySelector('.c_popup2.exam_c_popup2');
                          if (!pop) return false;
                          const style = window.getComputedStyle(pop);
                          const visible = style.display !== 'none' && style.visibility !== 'hidden'
                            && Number(style.opacity || '1') > 0;
                          if (!visible) return false;

                          const chk = pop.querySelector('#i_a_01');
                          if (chk && !chk.checked) chk.click();

                          const startBtn = pop.querySelector('#examStart .execute, #examStartPre .execute, a.execute');
                          if (!startBtn) return false;
                          startBtn.click();
                          return true;
                        }
                        """
                    )
                )
            except Exception:  # noqa: BLE001
                clicked = False
            if clicked:
                break

        if not clicked:
            return None

        self._log("종합평가 사전안내 레이어 감지: 동의 후 '시험 시작하기' 클릭")
        page.wait_for_timeout(1200)

        picked = self._pick_exam_page(page.context.pages, before_pages)
        if picked is not None and picked != page:
            self._log(f"종합평가 창 선택(사전안내 후): pages={len(page.context.pages)} / url={picked.url}")
            return picked

        now_url = page.url.lower()
        if now_url != before_url.lower() and any(h in now_url for h in ["exam", "test", "quiz", "evaluation", "eval"]):
            self._log(f"종합평가 페이지 직접 이동 감지(사전안내 후): {page.url}")
            return page
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

            if not self._click_exam_next(page, current=current):
                self._log("다음 문항 버튼을 찾지 못해 탐침을 종료합니다.")
                break

            if not self._wait_exam_question_change(page, key, prev_current=current, prev_total=total):
                self._log("다음 클릭 후 문항 변화가 감지되지 않아 탐침을 종료합니다.")
                break

        return {
            "visited_count": visited_count,
            "dom_readable_count": dom_readable_count,
            "total_hint": total_hint,
        }

    def _blocked_evidence_ids_for_question(self, question: str, min_streak: int = 2) -> set[str]:
        q_sig = self._question_signature(question)
        if not q_sig:
            return set()
        slot = self._question_evidence_fail_streak.get(q_sig, {})
        if not isinstance(slot, dict):
            return set()
        return {str(eid).strip() for eid, streak in slot.items() if int(streak) >= int(min_streak) and str(eid).strip()}

    def _update_evidence_fail_history(
        self,
        question_records: list[dict[str, Any]],
        report_rows: list[dict[str, Any]],
        fallback_failed_all: bool = False,
    ) -> None:
        if not question_records:
            return
        result_by_qsig: dict[str, list[dict[str, Any]]] = {}
        for row in report_rows:
            if not isinstance(row, dict):
                continue
            q_sig = str(row.get("question_signature", "")).strip()
            if not q_sig:
                continue
            result_by_qsig.setdefault(q_sig, []).append(row)

        for rec in question_records:
            if not isinstance(rec, dict):
                continue
            question = str(rec.get("question", "")).strip()
            if not question:
                continue
            q_sig = self._question_signature(question)
            if not q_sig:
                continue
            evidence_ids = [str(x).strip() for x in rec.get("evidence_ids", []) if str(x).strip()]
            if not evidence_ids:
                continue
            slot = self._question_evidence_fail_streak.setdefault(q_sig, {})
            rows = result_by_qsig.get(q_sig, [])
            correctness_values = [r.get("is_correct") for r in rows if isinstance(r.get("is_correct"), bool)]
            if correctness_values:
                is_correct = any(bool(v) for v in correctness_values)
            else:
                is_correct = not fallback_failed_all
            for eid in evidence_ids[:3]:
                if is_correct:
                    slot[eid] = 0
                else:
                    slot[eid] = int(slot.get(eid, 0)) + 1

    def _solve_exam_stream_with_rag(
        self,
        exam_page: Page,
        solver: Any,
        max_questions: int = 60,
        top_k: int = 6,
        confidence_threshold: float = 0.72,
        dialog_messages: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        visited_keys: set[str] = set()
        solved = 0
        skipped = 0
        low_conf_used = 0
        total_hint = 0
        question_records: list[dict[str, Any]] = []
        pass_score = max(0, min(100, int(getattr(self.settings, "rag_pass_score", 80))))
        low_conf_floor = max(0.0, min(1.0, float(getattr(self.settings, "rag_low_conf_floor", 0.55))))
        fallback_low_conf_budget = max(1, int(max_questions * 0.2))

        def _payload(success: bool, message: str) -> dict[str, Any]:
            return {
                "success": bool(success),
                "message": str(message),
                "solved": solved,
                "skipped": skipped,
                "low_conf_used": low_conf_used,
                "question_records": list(question_records),
                "exam_runtime_meta": dict(exam_runtime_meta),
            }

        self._log(
            "종합평가 RAG 자동풀이 시작 "
            f"(max={max_questions}, top_k={top_k}, conf>={confidence_threshold:.2f}, "
            f"pass_score={pass_score}, low_conf_floor={low_conf_floor:.2f}, "
            f"web=always, web_top_n={self.settings.rag_web_top_n})"
        )
        exam_runtime_meta = self._extract_exam_runtime_meta(exam_page)
        if exam_runtime_meta:
            self._log(
                "시험 메타 감지: "
                f"courseActiveSeq={exam_runtime_meta.get('courseActiveSeq', '')}, "
                f"examPaperSeq={exam_runtime_meta.get('courseActiveExamPaperSeq', '')}"
            )
        for _ in range(max_questions):
            snap = self._extract_exam_question_snapshot(exam_page)
            if snap is None:
                return _payload(False, "문항 텍스트를 읽지 못했습니다.")

            question = str(snap.get("question_text", "")).strip()
            full_text = str(snap.get("full_text", "")).strip()
            source = str(snap.get("source", "dom"))
            options = [str(x).strip() for x in snap.get("options", []) if str(x).strip()]
            current = int(snap.get("current", 0))
            total = int(snap.get("total", 0))
            key = str(snap.get("key", ""))
            if key in visited_keys:
                if self._is_exam_last_question(current=current, total=total, total_hint=total_hint):
                    self._click_exam_submit_if_present(exam_page)
                    return _payload(True, "반복 감지 + 마지막 문항으로 판단되어 제출")
                return _payload(False, f"이전 문항 반복 감지(current/total={current}/{max(total, total_hint)})")
            visited_keys.add(key)
            if total > 0:
                total_hint = max(total_hint, total)
            if not question:
                question = full_text

            if len(options) < 2:
                for retry in range(3):
                    if retry > 0:
                        exam_page.wait_for_timeout(1200)
                    snap_retry = self._extract_exam_question_snapshot(exam_page, force_ocr=True)
                    if snap_retry:
                        retry_options = [str(x).strip() for x in snap_retry.get("options", []) if str(x).strip()]
                        if len(retry_options) >= 2:
                            options = retry_options
                            question = str(snap_retry.get("question_text", "")).strip() or str(
                                snap_retry.get("full_text", "")
                            ).strip()
                            source = str(snap_retry.get("source", source))
                    if len(options) < 2:
                        structured_options = self._extract_exam_options_structured(exam_page)
                        if len(structured_options) >= 2:
                            options = structured_options
                            source = f"{source}+structured"
                    if len(options) >= 2:
                        self._log(
                            f"보기 추출 폴백 성공: retry={retry + 1}, source={source}, option_count={len(options)}"
                        )
                        break
                if len(options) < 2:
                    return _payload(False, f"보기 추출 실패(current/total={current}/{total}, source={source})")

            blocked_evidence_ids = self._blocked_evidence_ids_for_question(question)
            if blocked_evidence_ids:
                self._log(
                    "연속 오답 근거 차단 적용: "
                    f"Q {current}/{total} blocked={sorted(blocked_evidence_ids)[:3]}"
                )
            cached_answer = self._lookup_answer_bank_choice(
                question=question,
                options=options,
                exam_meta=exam_runtime_meta,
            )
            used_answer_bank = False
            if cached_answer is not None and "answer-bank" not in blocked_evidence_ids:
                choice = int(cached_answer.get("choice", 0))
                confidence = float(cached_answer.get("confidence", 0.98))
                reason = str(cached_answer.get("reason", "answer-bank"))
                evidence_ids: list[str] = ["answer-bank"]
                used_answer_bank = True
                self._log(f"정답 인덱스 매칭 사용: Q {current}/{total} -> choice={choice}, conf={confidence:.2f}")
            else:
                if cached_answer is not None and "answer-bank" in blocked_evidence_ids:
                    self._log(f"Q {current}/{total} answer-bank 연속 실패 감지로 2순위 근거 전환")
                solve_top_k = max(int(top_k), int(top_k) + len(blocked_evidence_ids))
                try:
                    decision = solver.solve(
                        question=question,
                        options=options,
                        top_k=solve_top_k,
                        exclude_evidence_ids=sorted(blocked_evidence_ids) if blocked_evidence_ids else None,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log(f"RAG 풀이 1차 실패: {exc} / 재시도 1회")
                    exam_page.wait_for_timeout(600)
                    retry_top_k = max(2, min(int(top_k), 8))
                    try:
                        decision = solver.solve(
                            question=question,
                            options=options,
                            top_k=retry_top_k + len(blocked_evidence_ids),
                            exclude_evidence_ids=sorted(blocked_evidence_ids) if blocked_evidence_ids else None,
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        return _payload(False, f"RAG 풀이 호출 실패: {retry_exc}")
                choice = int(getattr(decision, "choice", 0))
                confidence = float(getattr(decision, "confidence", 0.0))
                reason = str(getattr(decision, "reason", ""))
                evidence_ids = list(getattr(decision, "evidence_ids", []))
            self._log(
                "RAG 풀이: "
                f"Q {current}/{total} -> choice={choice}, conf={confidence:.2f}, source={source}, evidence={evidence_ids[:2]}"
            )

            if choice < 1 or choice > len(options):
                return _payload(False, f"LLM 선택지 번호 비정상: {choice}")
            if confidence < confidence_threshold:
                retry_top_k = min(20, max(int(top_k) + 2, int(top_k * 1.5)))
                self._log(
                    "LLM 신뢰도 낮음으로 재질문 1회 시도: "
                    f"Q {current}/{total} conf={confidence:.2f} -> top_k={retry_top_k}"
                )
                try:
                    retry_decision = solver.solve(
                        question=question,
                        options=options,
                        top_k=retry_top_k + len(blocked_evidence_ids),
                        exclude_evidence_ids=sorted(blocked_evidence_ids) if blocked_evidence_ids else None,
                    )
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
                    dynamic_budget = fallback_low_conf_budget
                    if total > 0:
                        required_correct = (total * pass_score + 99) // 100
                        dynamic_budget = max(0, total - required_correct)

                    if confidence >= low_conf_floor and low_conf_used < dynamic_budget:
                        low_conf_used += 1
                        self._log(
                            "LLM 저신뢰 문항 허용 진행: "
                            f"Q {current}/{total} conf={confidence:.2f} "
                            f"(used {low_conf_used}/{dynamic_budget}, floor={low_conf_floor:.2f})"
                        )
                    else:
                        skipped += 1
                        return _payload(
                            False,
                            (
                                f"LLM 신뢰도 낮음(conf={confidence:.2f}, floor={low_conf_floor:.2f}, "
                                f"low_conf_used={low_conf_used}/{dynamic_budget}): {reason}"
                            ),
                        )

            if not self._click_exam_option(exam_page, choice, options=options, current=current):
                picked_text = options[choice - 1] if 1 <= choice <= len(options) else ""
                return _payload(False, f"선택지 클릭 실패: {choice} ({picked_text})")
            question_records.append(
                {
                    "question_no": current,
                    "question": question,
                    "question_norm": self._normalize_question_text(question),
                    "question_signature": self._question_signature(question),
                    "options": list(options),
                    "selected_choice": int(choice),
                    "selected_option": options[choice - 1] if 1 <= choice <= len(options) else "",
                    "confidence": float(confidence),
                    "reason": reason,
                    "evidence_ids": list(evidence_ids or []),
                    "source": source,
                    "used_answer_bank": bool(used_answer_bank),
                    "blocked_evidence_ids": sorted(blocked_evidence_ids),
                }
            )
            solved += 1

            is_last_question = self._is_exam_last_question(current=current, total=total, total_hint=total_hint)
            if is_last_question:
                self._click_exam_submit_if_present(exam_page)
                return _payload(True, "마지막 문항 제출 완료")

            if not self._click_exam_next(exam_page, current=current):
                if self._has_exam_submit_control(exam_page):
                    self._log("다음 버튼 없음 + 제출 버튼 감지: 최종 제출을 시도합니다.")
                    self._click_exam_submit_if_present(exam_page)
                    return _payload(True, "다음 버튼 없음 + 제출 버튼 감지로 최종 제출 시도 후 종료")
                return _payload(False, f"다음 문항 버튼을 찾지 못했습니다(current/total={current}/{max(total, total_hint)})")

            if not self._wait_exam_question_change(
                exam_page, key, prev_current=current, prev_total=max(total, total_hint)
            ):
                self._log("다음 클릭 후 문항 변화가 없어 1회 재시도합니다.")
                if self._click_exam_next(exam_page, current=current) and self._wait_exam_question_change(
                    exam_page, key, prev_current=current, prev_total=max(total, total_hint), timeout_ms=8000
                ):
                    continue
                latest_dialog = str(dialog_messages[-1]).strip() if dialog_messages else ""
                if latest_dialog and any(
                    token in latest_dialog
                    for token in [
                        "문항 답변을 선택하지",
                        "문항 답변을 선택",
                        "답변을 선택해주시기",
                        "답안을 선택",
                    ]
                ):
                    self._log("답변 미선택 경고 감지: 현재 문항 재선택 후 다음을 재시도합니다.")
                    if self._click_exam_option(exam_page, choice, options=options, current=current):
                        if self._click_exam_next(exam_page, current=current) and self._wait_exam_question_change(
                            exam_page,
                            key,
                            prev_current=current,
                            prev_total=max(total, total_hint),
                            timeout_ms=10000,
                        ):
                            continue
                diag = self._diagnose_exam_transition_block(
                    exam_page=exam_page,
                    current=current,
                    dialog_messages=dialog_messages,
                )
                return _payload(
                    False,
                    f"다음 클릭 후 문항 변화가 없습니다(current/total={current}/{max(total, total_hint)}). {diag}",
                )

        return _payload(False, "문항 상한 도달")

    def _auto_solve_exam_with_rag(
        self,
        exam_page: Page,
        dialog_messages: Optional[list[str]] = None,
        max_questions: int = 60,
        rag_top_k: Optional[int] = None,
        confidence_threshold: Optional[float] = None,
    ) -> LoginResult:
        try:
            from rag_solver import RagExamSolver
        except Exception as exc:  # noqa: BLE001
            return LoginResult(False, f"RAG 솔버 로딩 실패: {exc}", exam_page.url)

        top_k = rag_top_k if rag_top_k is not None else self.settings.rag_top_k
        conf_th = confidence_threshold if confidence_threshold is not None else self.settings.rag_conf_threshold
        safe_max_questions = max(1, min(int(max_questions), 120))
        solver = RagExamSolver(
            index_path=self.settings.rag_index_path,
            generate_model=self.settings.rag_generate_model,
            embed_model=self.settings.rag_embed_model,
            ollama_base_url=self.settings.ollama_base_url,
            web_search_enabled=True,
            web_top_n=self.settings.rag_web_top_n,
            web_timeout_sec=self.settings.rag_web_timeout_sec,
            web_weight=self.settings.rag_web_weight,
        )

        self._stabilize_exam_page(exam_page)
        solve_result = self._solve_exam_stream_with_rag(
            exam_page=exam_page,
            solver=solver,
            max_questions=safe_max_questions,
            top_k=max(1, int(top_k)),
            confidence_threshold=float(conf_th),
            dialog_messages=dialog_messages,
        )
        self._last_exam_solve_payload = dict(solve_result)
        if not solve_result.get("success"):
            return LoginResult(False, str(solve_result.get("message", "시험 자동풀이 실패")), exam_page.url)

        if not self._wait_exam_finished(exam_page, timeout_ms=2 * 60 * 1000):
            return LoginResult(False, "시험 자동풀이 후 완료 화면을 확인하지 못했습니다.", exam_page.url)
        self._log("시험평가 완료 신호를 감지했습니다.")
        return LoginResult(
            True,
            f"종합평가 자동풀이 완료: solved={solve_result.get('solved', 0)}",
            exam_page.url,
        )

    @staticmethod
    def _parse_exam_text_payload(raw_text: str) -> Optional[dict[str, Any]]:
        normalized = re.sub(r"\s+", " ", raw_text).strip()
        if len(normalized) < 20:
            return None

        line_options: list[str] = []
        raw_lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        for ln in raw_lines:
            s = ln.strip()
            if re.match(r"^(?:[1-5]|[A-Ea-e]|[가-마])\s*[\.\)]\s*.+$", s):
                cleaned = re.sub(r"^(?:[1-5]|[A-Ea-e]|[가-마])\s*[\.\)]\s*", "", s)
                line_options.append(cleaned.strip())
            elif re.match(r"^[①②③④⑤]\s*.+$", s):
                cleaned = re.sub(r"^[①②③④⑤]\s*", "", s)
                line_options.append(cleaned.strip())
            elif re.match(r"^\[(?:[1-5])\]\s*.+$", s):
                cleaned = re.sub(r"^\[(?:[1-5])\]\s*", "", s)
                line_options.append(cleaned.strip())

        # 보기 번호와 텍스트가 줄바꿈으로 분리된 포맷:
        # 1
        # 체르노빌 원전사고
        for idx, s in enumerate(raw_lines[:-1]):
            if not re.fullmatch(r"[1-5①②③④⑤]", s):
                continue
            nxt = raw_lines[idx + 1].strip()
            if not nxt:
                continue
            if re.fullmatch(r"[1-5①②③④⑤]", nxt):
                continue
            if re.match(r"^\d+\s*/\s*\d+$", nxt):
                continue
            if len(nxt) < 2:
                continue
            line_options.append(nxt)

        options: list[str] = []
        for opt in line_options:
            clean = opt.strip()
            if clean and clean not in options:
                options.append(clean)
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
            if q_part and q_part != normalized:
                question_text = q_part
            else:
                first_option_idx = -1
                for idx, s in enumerate(raw_lines[:-1]):
                    if re.fullmatch(r"[1-5①②③④⑤]", s):
                        nxt = raw_lines[idx + 1].strip()
                        if nxt in options:
                            first_option_idx = idx
                            break
                if first_option_idx > 0:
                    q_lines: list[str] = []
                    for ln in raw_lines[:first_option_idx]:
                        if re.fullmatch(r"\d+\s*/\s*\d+", ln):
                            continue
                        if re.fullmatch(r"\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}", ln):
                            continue
                        if re.fullmatch(r"\d+\.", ln):
                            continue
                        if ln in {"답안 제출하기", "다음", "[종합평가] - 원자력 안전문화"}:
                            continue
                        q_lines.append(ln)
                    q_joined = " ".join(q_lines).strip()
                    if q_joined:
                        question_text = q_joined

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

    def _extract_exam_runtime_meta(self, exam_page: Page) -> dict[str, str]:
        scopes: list[Any] = [exam_page] + list(exam_page.frames)
        keys = [
            "courseActiveSeq",
            "courseApplySeq",
            "courseActiveExamPaperSeq",
            "activeElementSeq",
        ]
        for scope in scopes:
            try:
                info = scope.evaluate(
                    """
                    ({ keys }) => {
                      const out = {};
                      for (const key of keys) {
                        const selectors = [
                          `input[name="${key}"]`,
                          `input[id="${key}"]`,
                          `input[name="${key.toLowerCase()}"]`,
                          `input[id="${key.toLowerCase()}"]`,
                        ];
                        let val = '';
                        for (const sel of selectors) {
                          const el = document.querySelector(sel);
                          if (el && String(el.value || '').trim()) {
                            val = String(el.value || '').trim();
                            break;
                          }
                        }
                        out[key] = val;
                      }
                      return out;
                    }
                    """,
                    {"keys": keys},
                )
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(info, dict):
                continue
            cleaned = {k: str(info.get(k, "")).strip() for k in keys if str(info.get(k, "")).strip()}
            if cleaned:
                return cleaned
        return {}

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

            key_source = self._build_exam_snapshot_key(picked)
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

    def _extract_exam_options_structured(self, page: Page) -> list[str]:
        scopes: list[Any] = [page] + list(page.frames)
        best_texts: list[str] = []
        best_input_count = 0

        for scope in scopes:
            try:
                info = scope.evaluate(
                    """
                    () => {
                      const norm = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                      const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style && style.display === 'none') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const cleanOpt = (txt) => {
                        let s = norm(txt);
                        s = s.replace(/^\\d+\\s*[\\.)]\\s*/, '');
                        s = s.replace(/^([①②③④⑤]|[1-5]|[A-Ea-e]|[가-마])\\s*[\\.)]?\\s*/, '');
                        return norm(s);
                      };

                      const radios = Array.from(
                        document.querySelectorAll('input[name="choiceAnswers"], input[type="radio"], input[type="checkbox"]')
                      ).filter(isVisible);
                      const answerAnchors = Array.from(
                        document.querySelectorAll(
                          'a.answer-item, li[id^="example-item-"] a, li[class*="example-item"] a, .answer-box li a'
                        )
                      ).filter(isVisible);

                      const seen = new Set();
                      const texts = [];
                      for (const input of radios) {
                        let txt = '';
                        const parentLabel = input.closest('label');
                        if (parentLabel) txt = norm(parentLabel.innerText || parentLabel.textContent || '');
                        if (!txt) {
                          const container = input.closest('li, td, tr, div, p');
                          if (container) txt = norm(container.innerText || container.textContent || '');
                        }
                        txt = cleanOpt(txt);
                        if (!txt || txt.length < 1) continue;
                        if (seen.has(txt)) continue;
                        seen.add(txt);
                        texts.push(txt);
                      }

                      for (const anchor of answerAnchors) {
                        let txt = cleanOpt(anchor.innerText || anchor.textContent || '');
                        if (!txt) {
                          const li = anchor.closest('li');
                          if (li) txt = cleanOpt(li.innerText || li.textContent || '');
                        }
                        if (!txt || txt.length < 1) continue;
                        if (seen.has(txt)) continue;
                        seen.add(txt);
                        texts.push(txt);
                      }

                      return {
                        optionTexts: texts,
                        inputCount: Math.max(radios.length, answerAnchors.length),
                      };
                    }
                    """
                )
            except Exception:  # noqa: BLE001
                continue

            if not isinstance(info, dict):
                continue
            option_texts = [str(x).strip() for x in info.get("optionTexts", []) if str(x).strip()]
            input_count = int(info.get("inputCount", 0) or 0)

            if len(option_texts) > len(best_texts):
                best_texts = option_texts
                best_input_count = input_count
            elif len(option_texts) == len(best_texts) and input_count > best_input_count:
                best_input_count = input_count

        if len(best_texts) >= 2:
            return best_texts[:5]

        if best_input_count >= 2:
            return [f"선택지 {idx}" for idx in range(1, min(best_input_count, 5) + 1)]

        return []

    @staticmethod
    def _build_exam_snapshot_key(payload: dict[str, Any]) -> str:
        question_text = str(payload.get("question_text", "") or "").strip()
        options = [str(x).strip() for x in payload.get("options", []) if str(x).strip()]

        # 시험 UI의 타이머/버튼 텍스트 등 동적 값은 키에서 배제해 동일 문항을 안정적으로 식별합니다.
        base_parts: list[str] = []
        if question_text:
            base_parts.append(question_text)
        if options:
            base_parts.append(" | ".join(options[:5]))
        if not base_parts:
            base_parts.append(str(payload.get("full_text", "") or ""))

        key_raw = " || ".join(base_parts)
        key_raw = re.sub(r"\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b", " ", key_raw)
        key_raw = re.sub(r"\b\d{1,3}\s*/\s*\d{1,3}\b", " ", key_raw)
        key_raw = re.sub(r"\s+", " ", key_raw).strip()
        return re.sub(r"[^0-9A-Za-z가-힣]+", "", key_raw)[:240]

    @staticmethod
    @lru_cache(maxsize=20000)
    def _normalize_answer_text_cached(text: str) -> str:
        src = str(text or "").lower()
        src = re.sub(r"\s+", " ", src).strip()
        src = re.sub(r"[^0-9a-z가-힣 ]+", " ", src)
        src = re.sub(r"\s+", " ", src).strip()
        return src

    @classmethod
    def _normalize_answer_text(cls, text: str) -> str:
        return cls._normalize_answer_text_cached(str(text or ""))

    @classmethod
    def _normalize_question_text(cls, text: str) -> str:
        src = cls._normalize_answer_text(text)
        if not src:
            return ""
        src = re.sub(r"^(?:문항|문제|q)\s*\d{1,3}\s*", " ", src)
        src = re.sub(r"^\d{1,3}\s*", " ", src)
        src = re.sub(r"^(?:객관식|주관식|단일형|복수형|보기)\s*", " ", src)
        src = re.sub(r"\s+", " ", src).strip()
        return src

    @staticmethod
    @lru_cache(maxsize=30000)
    def _token_set_from_norm_cached(text_norm: str) -> frozenset[str]:
        src = str(text_norm or "").strip()
        if not src:
            return frozenset()
        return frozenset(tok for tok in src.split(" ") if len(tok) >= 2)

    @classmethod
    def _token_set_from_norm(cls, text_norm: str) -> set[str]:
        return set(cls._token_set_from_norm_cached(str(text_norm or "")))

    @classmethod
    def _text_token_set(cls, text: str) -> set[str]:
        src = cls._normalize_answer_text(text)
        return cls._token_set_from_norm(src)

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        union = len(a | b)
        return float(inter / union) if union > 0 else 0.0

    @classmethod
    def _question_signature_from_norm(cls, question_match_norm: str) -> str:
        q_norm = str(question_match_norm or "").strip()
        if not q_norm:
            return ""
        q_toks = sorted(cls._token_set_from_norm(q_norm))
        base = " ".join(q_toks[:40]) if q_toks else q_norm[:220]
        return hashlib.sha1(base.encode("utf-8"), usedforsecurity=False).hexdigest()

    @classmethod
    def _question_signature(cls, question: str) -> str:
        return cls._question_signature_from_norm(cls._normalize_question_text(question))

    @classmethod
    def _option_set_signature_from_norms(cls, option_norms: list[str]) -> str:
        cleaned = sorted({str(x).strip() for x in option_norms if str(x).strip()})
        if not cleaned:
            return ""
        return hashlib.sha1("|".join(cleaned[:8]).encode("utf-8"), usedforsecurity=False).hexdigest()

    @classmethod
    def _option_set_signature(cls, options: list[str]) -> str:
        norms = [cls._normalize_answer_text(x) for x in options if str(x).strip()]
        return cls._option_set_signature_from_norms(norms)

    @classmethod
    def _make_answer_bank_key(cls, question: str, options: list[str]) -> str:
        q = cls._normalize_question_text(question)
        opts = [cls._normalize_answer_text(x) for x in options if cls._normalize_answer_text(x)]
        base = f"q={q}||o={'|'.join(opts[:5])}"
        return hashlib.sha1(base.encode("utf-8"), usedforsecurity=False).hexdigest()

    def _map_answer_option_norm_to_choice(
        self, answer_option_norm: str, current_option_norms: list[str]
    ) -> tuple[int, float, str]:
        ans = str(answer_option_norm or "").strip()
        if not ans:
            return 0, 0.0, ""
        ans_toks = self._token_set_from_norm(ans)
        best_idx = 0
        best_sim = 0.0
        best_mode = ""
        for idx, now_opt in enumerate(current_option_norms, start=1):
            if not now_opt:
                continue
            if ans == now_opt:
                return idx, 1.0, "exact"
            if ans in now_opt or now_opt in ans:
                return idx, 0.96, "contains"
            sim = self._jaccard(ans_toks, self._token_set_from_norm(now_opt))
            if sim > best_sim:
                best_sim = sim
                best_idx = idx
                best_mode = "jaccard"
        return best_idx, best_sim, best_mode

    @staticmethod
    def _is_answer_item_scope_match(item: dict[str, Any], exam_meta: Optional[dict[str, str]]) -> bool:
        if not exam_meta:
            return True
        item_meta = item.get("exam_meta")
        if not isinstance(item_meta, dict):
            return True
        target_course = str(exam_meta.get("courseActiveSeq", "")).strip()
        target_paper = str(exam_meta.get("courseActiveExamPaperSeq", "")).strip()
        item_course = str(item_meta.get("courseActiveSeq", "")).strip()
        item_paper = str(item_meta.get("courseActiveExamPaperSeq", "")).strip()
        if target_course and item_course and target_course != item_course:
            return False
        if target_paper and item_paper and target_paper != item_paper:
            return False
        return True

    def _lookup_answer_bank_choice(
        self, question: str, options: list[str], exam_meta: Optional[dict[str, str]] = None
    ) -> Optional[dict[str, Any]]:
        if not self._answer_bank_items:
            return None
        if len(options) < 2:
            return None

        exact_key = self._make_answer_bank_key(question, options)
        exact = self._answer_bank_items.get(exact_key)
        if isinstance(exact, dict) and self._is_answer_item_scope_match(exact, exam_meta):
            answer_opt_norm = str(exact.get("answer_option_norm", "")).strip()
            if not answer_opt_norm:
                saved_opts_exact = [self._normalize_answer_text(str(x)) for x in exact.get("options", [])]
                try:
                    exact_idx = int(exact.get("answer_index", 0))
                except Exception:  # noqa: BLE001
                    exact_idx = 0
                if 1 <= exact_idx <= len(saved_opts_exact):
                    answer_opt_norm = saved_opts_exact[exact_idx - 1]
            opt_norms_exact = [self._normalize_answer_text(x) for x in options]
            mapped_idx, mapped_sim, _ = self._map_answer_option_norm_to_choice(answer_opt_norm, opt_norms_exact)
            if 1 <= mapped_idx <= len(options):
                conf = 0.99 if mapped_sim >= 0.98 else min(0.98, 0.92 + 0.06 * mapped_sim)
                return {"choice": mapped_idx, "reason": "answer-bank exact", "confidence": conf}

        q_norm = self._normalize_answer_text(question)
        q_match_norm = self._normalize_question_text(question)
        q_sig = self._question_signature_from_norm(q_match_norm)
        opt_norms = [self._normalize_answer_text(x) for x in options]
        option_set_sig = self._option_set_signature_from_norms(opt_norms)
        ordered_candidates: list[dict[str, Any]] = []
        if q_sig and option_set_sig:
            ordered_candidates.extend(self._answer_bank_qsig_optset_index.get(f"{q_sig}||{option_set_sig}", []))
        if q_norm:
            ordered_candidates.extend(self._answer_bank_qnorm_index.get(q_norm, []))
        if q_sig:
            ordered_candidates.extend(self._answer_bank_qsig_index.get(q_sig, []))

        seen_items: set[int] = set()
        for item in ordered_candidates:
            if not isinstance(item, dict):
                continue
            marker = id(item)
            if marker in seen_items:
                continue
            seen_items.add(marker)
            if not self._is_answer_item_scope_match(item, exam_meta):
                continue
            ans_opt_norm = str(item.get("answer_option_norm", "")).strip()
            if not ans_opt_norm:
                try:
                    idx_saved = int(item.get("answer_index", 0))
                except Exception:  # noqa: BLE001
                    idx_saved = 0
                saved_opts = [self._normalize_answer_text(str(x)) for x in item.get("options", [])]
                if 1 <= idx_saved <= len(saved_opts):
                    ans_opt_norm = saved_opts[idx_saved - 1]
            if ans_opt_norm:
                mapped_idx, mapped_sim, mapped_mode = self._map_answer_option_norm_to_choice(ans_opt_norm, opt_norms)
                if mapped_idx > 0 and mapped_mode in {"exact", "contains"}:
                    conf = 0.98 if mapped_mode == "exact" else 0.97
                    return {"choice": mapped_idx, "reason": "answer-bank text-match", "confidence": conf}
                if mapped_idx > 0 and mapped_sim >= 0.76:
                    conf = min(0.96, 0.90 + 0.08 * mapped_sim)
                    return {"choice": mapped_idx, "reason": "answer-bank text-fuzzy", "confidence": conf}

            # 보기 순서 변경 불변 매칭: 문항 시그니처 + 보기 집합이 같으면 정답 텍스트로 재매핑합니다.
            saved_opts_norm = [self._normalize_answer_text(str(x)) for x in item.get("options", []) if str(x).strip()]
            saved_option_set_sig = str(item.get("option_set_signature", "")).strip()
            if not saved_option_set_sig:
                saved_option_set_sig = self._option_set_signature_from_norms(saved_opts_norm)
            if option_set_sig and saved_option_set_sig and option_set_sig == saved_option_set_sig:
                try:
                    idx_saved = int(item.get("answer_index", 0))
                except Exception:  # noqa: BLE001
                    idx_saved = 0
                if 1 <= idx_saved <= len(saved_opts_norm):
                    ans_from_saved = saved_opts_norm[idx_saved - 1]
                    mapped_idx, mapped_sim, _ = self._map_answer_option_norm_to_choice(ans_from_saved, opt_norms)
                    if mapped_idx > 0 and mapped_sim >= 0.72:
                        conf = min(0.97, 0.91 + 0.06 * mapped_sim)
                        return {"choice": mapped_idx, "reason": "answer-bank order-invariant", "confidence": conf}

        # 섞임/표현차 대응: 문항+보기 유사도 퍼지 매칭
        q_tokens = self._token_set_from_norm(q_match_norm or q_norm)
        now_opt_norms = opt_norms
        now_opt_tokens = [self._token_set_from_norm(x) for x in now_opt_norms]

        best_choice = 0
        best_score = 0.0
        best_map_sim = 0.0
        best_reason = ""
        for packed in self._answer_bank_fuzzy_index:
            item = packed.get("item")
            if not isinstance(item, dict):
                continue
            if not self._is_answer_item_scope_match(item, exam_meta):
                continue
            item_opt_tokens = packed.get("option_tokens", [])
            item_q_tokens = packed.get("q_tokens", set())
            if not isinstance(item_q_tokens, set):
                item_q_tokens = set()
            q_sim = self._jaccard(q_tokens, item_q_tokens)
            if q_sim < 0.30:
                continue

            opt_hit = 0
            for cur_toks in now_opt_tokens:
                if not cur_toks:
                    continue
                if any(self._jaccard(cur_toks, it) >= 0.56 for it in item_opt_tokens if it):
                    opt_hit += 1
            opt_sim = opt_hit / max(1, len(now_opt_tokens))
            if opt_sim < 0.50:
                continue

            ans_opt_norm = str(packed.get("answer_opt_norm", "")).strip()
            if not ans_opt_norm:
                continue

            mapped_idx, mapped_sim, _ = self._map_answer_option_norm_to_choice(ans_opt_norm, now_opt_norms)
            if mapped_idx == 0 and packed.get("option_norms"):
                saved_opt_norms = [str(x) for x in packed.get("option_norms", [])]
                saved_set = self._option_set_signature_from_norms(saved_opt_norms)
                if saved_set and saved_set == option_set_sig:
                    try:
                        idx_saved = int(item.get("answer_index", 0))
                    except Exception:  # noqa: BLE001
                        idx_saved = 0
                    if 1 <= idx_saved <= len(saved_opt_norms):
                        mapped_idx, mapped_sim, _ = self._map_answer_option_norm_to_choice(
                            saved_opt_norms[idx_saved - 1], now_opt_norms
                        )
            if mapped_idx == 0:
                continue

            score = 0.72 * q_sim + 0.28 * opt_sim
            if score > best_score:
                best_score = score
                best_choice = mapped_idx
                best_map_sim = mapped_sim
                best_reason = f"answer-bank fuzzy q={q_sim:.2f} opt={opt_sim:.2f}"

        if best_choice > 0 and best_score >= 0.62 and best_map_sim >= 0.70:
            conf = min(0.96, 0.74 + 0.20 * best_score)
            return {"choice": best_choice, "reason": best_reason, "confidence": conf}
        return None

    def _upsert_answer_bank_entry(
        self,
        question: str,
        options: list[str],
        answer_index: int,
        answer_text: str = "",
        source: str = "",
        exam_meta: Optional[dict[str, str]] = None,
    ) -> bool:
        if answer_index < 1 or answer_index > len(options):
            return False

        key = self._make_answer_bank_key(question, options)
        now_ts = datetime.now().isoformat(timespec="seconds")
        answer_opt = options[answer_index - 1].strip() if 1 <= answer_index <= len(options) else ""
        question_norm = self._normalize_answer_text(question)
        question_match_norm = self._normalize_question_text(question)
        option_norms = [self._normalize_answer_text(str(x)) for x in options]
        prev_hits = 0
        if key in self._answer_bank_items:
            try:
                prev_hits = int(self._answer_bank_items[key].get("hits", 0))
            except Exception:  # noqa: BLE001
                prev_hits = 0

        payload: dict[str, Any] = {
            "question": question.strip(),
            "question_norm": question_norm,
            "question_match_norm": question_match_norm,
            "question_signature": self._question_signature_from_norm(question_match_norm),
            "options": [str(x).strip() for x in options],
            "option_norms": option_norms,
            "option_set_signature": self._option_set_signature_from_norms(option_norms),
            "answer_index": int(answer_index),
            "answer_option": answer_opt,
            "answer_option_norm": self._normalize_answer_text(answer_opt),
            "answer_text": str(answer_text or "").strip(),
            "source": str(source or "").strip(),
            "updated_at": now_ts,
            "hits": prev_hits + 1,
        }
        if exam_meta:
            payload["exam_meta"] = {str(k): str(v) for k, v in exam_meta.items() if str(k).strip()}
        self._answer_bank_items[key] = payload
        self._rebuild_answer_bank_indexes()
        return True

    @staticmethod
    def _parse_js_object_map(text: str) -> dict[str, str]:
        src = str(text or "")
        pairs = re.findall(r"([A-Za-z0-9_]+)\s*:\s*'([^']*)'", src)
        out: dict[str, str] = {}
        for k, v in pairs:
            out[str(k)] = str(v)
        return out

    @staticmethod
    def _map_answer_token_to_index(token: str) -> int:
        t = str(token or "").strip()
        if not t:
            return 0
        circled = {"①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5}
        if t in circled:
            return circled[t]
        if t in {"A", "a", "가"}:
            return 1
        if t in {"B", "b", "나"}:
            return 2
        if t in {"C", "c", "다"}:
            return 3
        if t in {"D", "d", "라"}:
            return 4
        if t in {"E", "e", "마"}:
            return 5
        if t.isdigit():
            try:
                n = int(t)
            except Exception:  # noqa: BLE001
                return 0
            return n if 1 <= n <= 9 else 0
        return 0

    def _parse_answer_line(self, line: str, options: list[str]) -> tuple[int, str]:
        text = str(line or "").strip()
        if not text:
            return 0, ""
        m = re.search(r"정답\s*[:：]?\s*([①②③④⑤]|[1-5]|[A-Ea-e]|[가-마])(?:\s*번)?", text)
        if m:
            idx = self._map_answer_token_to_index(m.group(1))
            if 1 <= idx <= len(options):
                return idx, options[idx - 1]

        tail = re.sub(r"^.*정답\s*[:：]?\s*", "", text).strip()
        tail = re.sub(r"\(.*?\)", " ", tail).strip()
        tail_norm = self._normalize_answer_text(tail)
        if not tail_norm:
            return 0, ""
        for idx, opt in enumerate(options, start=1):
            opt_norm = self._normalize_answer_text(opt)
            if not opt_norm:
                continue
            if tail_norm == opt_norm or tail_norm in opt_norm or opt_norm in tail_norm:
                return idx, opt
        return 0, tail

    def _extract_answer_entries_from_review_text(self, raw_text: str) -> list[dict[str, Any]]:
        src = str(raw_text or "")
        if len(src.strip()) < 40:
            return []
        lines = [ln.strip() for ln in src.splitlines() if ln.strip()]
        if not lines:
            return []

        q_starts: list[tuple[int, str]] = []
        for idx, ln in enumerate(lines):
            m = re.match(r"^(\d{1,3})\.\s*$", ln)
            if m:
                q_starts.append((idx, m.group(1)))

        if not q_starts:
            return []

        results: list[dict[str, Any]] = []
        for pos, (start_idx, q_no) in enumerate(q_starts):
            end_idx = q_starts[pos + 1][0] if pos + 1 < len(q_starts) else len(lines)
            block = lines[start_idx + 1 : end_idx]
            if not block:
                continue

            question_parts: list[str] = []
            option_map: dict[int, str] = {}
            current_opt_no = 0
            answer_idx = 0
            answer_text = ""

            def append_opt_text(opt_no: int, text: str) -> None:
                clean = str(text or "").strip()
                if not clean:
                    return
                prev = option_map.get(opt_no, "")
                option_map[opt_no] = (prev + " " + clean).strip() if prev else clean

            for ln in block:
                s = ln.strip()
                if not s:
                    continue
                if "획득점수" in s and "점" in s:
                    continue
                if re.match(r"^\[객관식", s):
                    # "[객관식 단일형] 질문..." 형태에서 질문 본문은 유지
                    s = re.sub(r"^\[[^\]]+\]\s*", "", s).strip()
                    if not s:
                        continue

                m_ans = re.search(r"\[?\s*정답\s*\]?", s)
                if m_ans:
                    # 보기 텍스트에 [정답]이 붙어있는 경우(주요 포맷)
                    cleaned = re.sub(r"\[\s*정답\s*\]", " ", s).strip()
                    if current_opt_no in {1, 2, 3, 4, 5}:
                        answer_idx = current_opt_no
                        append_opt_text(current_opt_no, cleaned)
                        answer_text = option_map.get(current_opt_no, "").strip()
                        continue
                    idx_guess, txt_guess = self._parse_answer_line(s, [option_map.get(i, "") for i in range(1, 6)])
                    if idx_guess > 0:
                        answer_idx = idx_guess
                    if txt_guess:
                        answer_text = txt_guess
                    continue

                # 보기 번호 단독 줄 (1 / 2 / 3 / 4)
                if re.fullmatch(r"[1-5]", s):
                    current_opt_no = int(s)
                    if current_opt_no not in option_map:
                        option_map[current_opt_no] = ""
                    continue

                # 보기 번호 + 텍스트 한 줄 포맷
                m_opt_inline = re.match(r"^([1-5]|[A-Ea-e]|[가-마]|[①②③④⑤])\s*[\.\)]\s*(.+)$", s)
                if m_opt_inline:
                    token = m_opt_inline.group(1)
                    txt = m_opt_inline.group(2).strip()
                    idx = self._map_answer_token_to_index(token)
                    if 1 <= idx <= 5:
                        current_opt_no = idx
                        append_opt_text(idx, re.sub(r"\[\s*정답\s*\]", " ", txt).strip())
                        if "[정답]" in txt:
                            answer_idx = idx
                            answer_text = option_map.get(idx, "")
                        continue

                if current_opt_no in {1, 2, 3, 4, 5}:
                    append_opt_text(current_opt_no, re.sub(r"\[\s*정답\s*\]", " ", s).strip())
                    continue
                question_parts.append(s)

            options = [option_map.get(i, "").strip() for i in range(1, 6)]
            options = [x for x in options if x]
            question = " ".join(question_parts).strip()
            if not question or len(options) < 2:
                continue

            if answer_idx <= 0 and answer_text:
                at_norm = self._normalize_answer_text(answer_text)
                for idx, opt in enumerate(options, start=1):
                    on = self._normalize_answer_text(opt)
                    if at_norm and (at_norm == on or at_norm in on or on in at_norm):
                        answer_idx = idx
                        break

            if answer_idx <= 0 or answer_idx > len(options):
                continue

            results.append(
                {
                    "q_no": q_no,
                    "question": question,
                    "options": options[:5],
                    "answer_index": answer_idx,
                    "answer_text": answer_text or options[answer_idx - 1],
                }
            )
        return results

    @staticmethod
    def _extract_texts_from_page_and_frames(page: Page) -> list[str]:
        texts: list[str] = []
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            try:
                txt = scope.locator("body").inner_text(timeout=3000)
            except Exception:  # noqa: BLE001
                continue
            clean = str(txt or "").strip()
            if len(clean) >= 20:
                texts.append(clean)
        return texts

    def _click_review_confirm_button(self, scope: Any) -> bool:
        try:
            return bool(
                scope.evaluate(
                    """
                    () => {
                      const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                      const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const cands = Array.from(
                        document.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                      );
                      const target = cands.find((el) => {
                        if (!isVisible(el)) return false;
                        const txt = normalize(el.textContent || el.value || '');
                        const oc = String(el.getAttribute('onclick') || '').toLowerCase();
                        if (!txt.includes('확인')) return false;
                        if (!oc && el.tagName.toLowerCase() === 'span') return false;
                        return true;
                      });
                      if (!target) return false;
                      try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                      try { target.click(); return true; } catch (e) {}
                      try { target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); return true; } catch (e) {}
                      return false;
                    }
                    """
                )
            )
        except Exception:  # noqa: BLE001
            return False

    def _learn_answers_from_result_panel(self, classroom_page: Page) -> dict[str, Any]:
        original_url = ""
        try:
            original_url = classroom_page.url
        except Exception:  # noqa: BLE001
            original_url = ""

        panel = self._extract_exam_result_panel(classroom_page)
        onclick = str(panel.get("onclick", "") or "")
        if not onclick:
            return {"added": 0, "found": 0, "reason": "결과 버튼 onclick 미발견"}

        before_pages = list(classroom_page.context.pages)
        clicked = False
        try:
            clicked = bool(
                classroom_page.evaluate(
                    """
                    () => {
                      const anchors = Array.from(document.querySelectorAll('a[onclick*="doExamPaperPopup"], a[onclick*="doexampaperpopup"]'));
                      const parseResultYn = (oc) => {
                        const m = String(oc || '').match(/resultyn\\s*:\\s*['"]?([YN])['"]?/i);
                        return m ? String(m[1] || '').toUpperCase() : '';
                      };
                      const a = anchors.find((el) => parseResultYn(el.getAttribute('onclick') || '') === 'Y') || anchors[0];
                      if (!a) return false;
                      try { a.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                      try { a.click(); return true; } catch (e) {}
                      return false;
                    }
                    """
                )
            )
        except Exception:  # noqa: BLE001
            clicked = False
        if not clicked:
            return {"added": 0, "found": 0, "reason": "결과 버튼 클릭 실패"}

        classroom_page.wait_for_timeout(2500)
        after_pages = list(classroom_page.context.pages)
        new_pages = [pg for pg in after_pages if pg not in before_pages]
        candidate_pages: list[Page] = [classroom_page] + new_pages

        texts: list[str] = []
        for pg in candidate_pages:
            if pg is None:
                continue
            texts.extend(self._extract_texts_from_page_and_frames(pg))
            scopes: list[Any] = [pg] + list(pg.frames)
            clicked_confirm = False
            for scope in scopes:
                if self._click_review_confirm_button(scope):
                    clicked_confirm = True
                    break
            if clicked_confirm:
                pg.wait_for_timeout(1800)
                texts.extend(self._extract_texts_from_page_and_frames(pg))

        unique_texts: list[str] = []
        seen_hash: set[str] = set()
        for txt in texts:
            h = hashlib.sha1(txt.encode("utf-8"), usedforsecurity=False).hexdigest()
            if h in seen_hash:
                continue
            seen_hash.add(h)
            unique_texts.append(txt)

        entries: list[dict[str, Any]] = []
        for txt in unique_texts:
            entries.extend(self._extract_answer_entries_from_review_text(txt))
        deduped_entries: list[dict[str, Any]] = []
        seen_entry_keys: set[str] = set()
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            q_sig = self._question_signature(str(ent.get("question", "")))
            ans_idx = int(ent.get("answer_index", 0) or 0)
            key = f"{q_sig}:{ans_idx}"
            if key in seen_entry_keys:
                continue
            seen_entry_keys.add(key)
            deduped_entries.append(ent)
        entries = deduped_entries

        added = 0
        meta_map = self._parse_js_object_map(onclick)
        for ent in entries:
            ok = self._upsert_answer_bank_entry(
                question=str(ent.get("question", "")),
                options=[str(x) for x in ent.get("options", [])],
                answer_index=int(ent.get("answer_index", 0)),
                answer_text=str(ent.get("answer_text", "")),
                source="exam-result",
                exam_meta=meta_map,
            )
            if ok:
                added += 1
        if added > 0:
            self._save_answer_bank()

        # 결과 레이어 진입 후 원래 강의실 페이지로 복귀해 후속 동작(응시횟수/재응시)을 안정화합니다.
        try:
            current_url = classroom_page.url
        except Exception:  # noqa: BLE001
            current_url = ""
        if (
            original_url
            and current_url
            and current_url != original_url
            and "/usr/classroom/exampaper/result/detail/layer.do" in current_url
        ):
            restored = False
            try:
                classroom_page.go_back(wait_until="domcontentloaded")
                classroom_page.wait_for_timeout(800)
                restored = True
            except Exception:  # noqa: BLE001
                restored = False
            if not restored:
                try:
                    classroom_page.goto(original_url, wait_until="domcontentloaded")
                    classroom_page.wait_for_timeout(800)
                except Exception:  # noqa: BLE001
                    pass

        return {
            "added": added,
            "found": len(entries),
            "reason": f"resultYn={panel.get('resultYn', '')}, texts={len(unique_texts)}",
            "entries": entries,
            "exam_meta": meta_map,
            "raw_text_count": len(unique_texts),
        }

    def _build_exam_quality_rows(
        self,
        question_records: list[dict[str, Any]],
        result_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not question_records:
            return rows

        entry_index: list[dict[str, Any]] = []
        for ent in result_entries:
            if not isinstance(ent, dict):
                continue
            q = str(ent.get("question", "")).strip()
            opts = [str(x).strip() for x in ent.get("options", []) if str(x).strip()]
            ans_idx = int(ent.get("answer_index", 0) or 0)
            if not q or len(opts) < 2 or ans_idx < 1 or ans_idx > len(opts):
                continue
            q_norm = self._normalize_question_text(q)
            q_sig = self._question_signature_from_norm(q_norm)
            opt_norms = [self._normalize_answer_text(x) for x in opts]
            entry_index.append(
                {
                    "raw": ent,
                    "question": q,
                    "question_norm": q_norm,
                    "question_signature": q_sig,
                    "q_tokens": self._token_set_from_norm(q_norm),
                    "options": opts,
                    "option_norms": opt_norms,
                    "answer_index": ans_idx,
                    "answer_option_norm": opt_norms[ans_idx - 1],
                    "option_set_sig": self._option_set_signature_from_norms(opt_norms),
                }
            )

        for rec in question_records:
            if not isinstance(rec, dict):
                continue
            q = str(rec.get("question", "")).strip()
            opts = [str(x).strip() for x in rec.get("options", []) if str(x).strip()]
            sel_choice = int(rec.get("selected_choice", 0) or 0)
            sel_opt = str(rec.get("selected_option", "")).strip()
            q_norm = self._normalize_question_text(q)
            q_sig = self._question_signature_from_norm(q_norm)
            q_tokens = self._token_set_from_norm(q_norm)
            opt_norms = [self._normalize_answer_text(x) for x in opts]
            option_set_sig = self._option_set_signature_from_norms(opt_norms)

            best: Optional[dict[str, Any]] = None
            best_score = -1.0
            for ent in entry_index:
                score = 0.0
                if q_sig and q_sig == str(ent.get("question_signature", "")):
                    score += 0.82
                else:
                    q_sim = self._jaccard(q_tokens, set(ent.get("q_tokens", set())))
                    score += 0.62 * q_sim
                ent_opt_set = str(ent.get("option_set_sig", ""))
                if option_set_sig and ent_opt_set and option_set_sig == ent_opt_set:
                    score += 0.24
                else:
                    ent_opts = set(ent.get("option_norms", []))
                    score += 0.18 * self._jaccard(set(opt_norms), ent_opts)
                if score > best_score:
                    best = ent
                    best_score = score

            correct_choice = 0
            correct_option = ""
            is_correct: Optional[bool] = None
            matched = bool(best is not None and best_score >= 0.35)
            if matched and best is not None:
                ans_opt_norm = str(best.get("answer_option_norm", "")).strip()
                mapped_idx, mapped_sim, _ = self._map_answer_option_norm_to_choice(ans_opt_norm, opt_norms)
                if mapped_idx > 0 and mapped_sim >= 0.64:
                    correct_choice = mapped_idx
                    correct_option = opts[mapped_idx - 1] if mapped_idx <= len(opts) else ""
                    if sel_choice > 0:
                        is_correct = sel_choice == correct_choice

            rows.append(
                {
                    "question_no": int(rec.get("question_no", 0) or 0),
                    "question": q,
                    "question_signature": q_sig,
                    "selected_choice": sel_choice,
                    "selected_option": sel_opt,
                    "correct_choice": correct_choice,
                    "correct_option": correct_option,
                    "is_correct": is_correct,
                    "confidence": float(rec.get("confidence", 0.0) or 0.0),
                    "reason": str(rec.get("reason", "")),
                    "evidence_ids": [str(x) for x in rec.get("evidence_ids", []) if str(x).strip()],
                    "source": str(rec.get("source", "")),
                    "used_answer_bank": bool(rec.get("used_answer_bank", False)),
                    "matched_result_entry": matched,
                    "match_score": round(best_score if best_score > 0 else 0.0, 4),
                }
            )
        return rows

    def _write_exam_quality_report(
        self,
        *,
        course_title: str,
        attempt_no: int,
        solve_payload: dict[str, Any],
        learn_payload: dict[str, Any],
        completion_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        question_records = [x for x in solve_payload.get("question_records", []) if isinstance(x, dict)]
        result_entries = [x for x in learn_payload.get("entries", []) if isinstance(x, dict)]
        rows = self._build_exam_quality_rows(question_records, result_entries)

        total = len(rows)
        matched = sum(1 for r in rows if bool(r.get("matched_result_entry")))
        known = sum(1 for r in rows if isinstance(r.get("is_correct"), bool))
        correct = sum(1 for r in rows if r.get("is_correct") is True)
        wrong = sum(1 for r in rows if r.get("is_correct") is False)

        summary = {
            "questions": total,
            "matched_result_entries": matched,
            "correctness_known": known,
            "correct": correct,
            "wrong": wrong,
            "unknown": max(0, total - known),
        }
        payload = {
            "meta": {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "course_title": str(course_title or "").strip(),
                "attempt_no": int(attempt_no),
                "exam_meta": solve_payload.get("exam_runtime_meta") or learn_payload.get("exam_meta") or {},
                "completion_state": completion_state or {},
                "learn_reason": str(learn_payload.get("reason", "")),
            },
            "summary": summary,
            "rows": rows,
        }

        path = ""
        try:
            self._exam_quality_report_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_course = re.sub(r"[^0-9A-Za-z가-힣]+", "_", str(course_title or "course")).strip("_") or "course"
            out_path = self._exam_quality_report_dir / f"exam_quality_{stamp}_{safe_course}_try{int(attempt_no):02d}.json"
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            path = str(out_path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"시험 품질 리포트 저장 실패: {exc}")

        return {"path": path, "rows": rows, "summary": summary}

    def _click_exam_next(self, page: Page, current: int = 0) -> bool:
        selectors = [
            'button:has-text("다음")',
            'a:has-text("다음")',
            'input[value*="다음"]',
            'button:has-text("Next")',
            'a:has-text("Next")',
            'button:has-text(">")',
            'a:has-text(">")',
            'button:has-text("›")',
            'a:has-text("›")',
            '[aria-label*="다음"]',
            '[aria-label*="next"]',
            'a[onclick*="next"]',
            'button[onclick*="next"]',
        ]
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            if self._click_first_visible(scope, selectors, max_items=20):
                page.wait_for_timeout(900)
                return True

        # 현재 문항 번호를 알고 있으면 해당 문항의 next 핸들러를 우선 실행합니다.
        if current > 0:
            for scope in scopes:
                try:
                    clicked = scope.evaluate(
                        """
                        ({ current }) => {
                          const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                          const isVisible = (el) => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                          };
                          const pad2 = String(current).padStart(2, '0');
                          const pad3 = String(current).padStart(3, '0');
                          const ids = [`que_${pad2}`, `que_${pad3}`, `que_${current}`];
                          const activeQ = ids
                            .map((id) => document.getElementById(id))
                            .find((el) => el && isVisible(el));

                          const execOnclick = (el) => {
                            const oc = (el && el.getAttribute && el.getAttribute('onclick')) || '';
                            if (!oc) return false;
                            try {
                              // onclick 문자열 직접 실행
                              // eslint-disable-next-line no-new-func
                              const fn = new Function(oc);
                              fn.call(el);
                              return true;
                            } catch (e) {
                              return false;
                            }
                          };

                          const inScope = activeQ || document;
                          const cands = Array.from(
                            inScope.querySelectorAll('a[onclick],button[onclick],a,button,input[type="button"],input[type="submit"]')
                          ).filter(isVisible);
                          const target = cands.find((el) => {
                            const txt = norm(el.textContent || el.value);
                            const aria = norm(el.getAttribute('aria-label') || el.getAttribute('title') || '');
                            const oc = (el.getAttribute('onclick') || '').toLowerCase();
                            const byText = txt.includes('다음') || txt.includes('next') || txt.includes('다음문항');
                            const byArrow = txt === '>' || txt === '›' || txt === '＞' || txt === '→' || txt.endsWith(' >');
                            const byAria = aria.includes('다음') || aria.includes('next');
                            const byCount = oc.includes(`nowcount:${current}`) || oc.includes(`nowcount:'${current}'`);
                            const byNextApi = oc.includes('donextshowitem') || oc.includes('nextindex');
                            return byText || byArrow || byAria || byCount || byNextApi;
                          });
                          if (!target) return false;

                          try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                          try { target.click(); } catch (e) {}
                          if (execOnclick(target)) return true;
                          return true;
                        }
                        """,
                        {"current": current},
                    )
                    if clicked:
                        page.wait_for_timeout(900)
                        return True
                except Exception:  # noqa: BLE001
                    pass

        for scope in scopes:
            try:
                clicked = scope.evaluate(
                    """
                    () => {
                      const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const cands = Array.from(
                        document.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                      );
                      const target = cands.find((el) => {
                        const txt = normalize(el.textContent || el.value);
                        const aria = normalize(el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title')) || '');
                        return isVisible(el) && (
                          txt.includes('다음')
                          || txt.includes('next')
                          || txt.includes('다음문항')
                          || txt.includes('next question')
                          || txt === '>'
                          || txt === '›'
                          || txt === '＞'
                          || txt === '→'
                          || aria.includes('다음')
                          || aria.includes('next')
                        );
                      });
                      if (!target) return false;
                      try { target.click(); } catch (e) {
                        target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                      }
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

    def _click_exam_option(
        self, page: Page, choice: int, options: Optional[list[str]] = None, current: int = 0
    ) -> bool:
        if choice < 1:
            return False
        option_text = ""
        if options and 1 <= choice <= len(options):
            option_text = str(options[choice - 1]).strip()

        def _has_selected_answer(scope_obj: Any, question_no: int) -> bool:
            try:
                return bool(
                    scope_obj.evaluate(
                        """
                        ({ current }) => {
                          const isVisible = (el) => {
                            if (!el) return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                          };
                          const getCurrentRoot = () => {
                            if (current > 0) {
                              const pad2 = String(current).padStart(2, '0');
                              const pad3 = String(current).padStart(3, '0');
                              const ids = [`que_${pad2}`, `que_${pad3}`, `que_${current}`];
                              for (const id of ids) {
                                const el = document.getElementById(id);
                                if (el && isVisible(el)) return el;
                              }
                            }
                            return (
                              Array.from(document.querySelectorAll('.quiz_li')).find((el) => isVisible(el))
                              || document
                            );
                          };

                          const root = getCurrentRoot();
                          if (!root) return false;
                          if (root.querySelector('li.on .answer-item, li.on.answer-item, li.on')) return true;

                          const choiceInputs = Array.from(root.querySelectorAll('input[name="choiceAnswers"]'));
                          if (choiceInputs.some((el) => String(el.value || '').trim().length > 0)) return true;

                          const checked = root.querySelectorAll(
                            'input[type="radio"]:checked, input[type="checkbox"]:checked'
                          );
                          return checked.length > 0;
                        }
                        """,
                        {"current": question_no},
                    )
                )
            except Exception:  # noqa: BLE001
                return False

        scopes: list[Any] = [page] + list(page.frames)

        # 0) 현재 문항 컨테이너 우선 클릭 (que_01/que_001 패턴)
        for scope in scopes:
            try:
                clicked_in_current = scope.evaluate(
                    """
                    ({ choice, optionText, current }) => {
                      const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const execOnclick = (el) => {
                        const oc = (el && el.getAttribute && el.getAttribute('onclick')) || '';
                        if (!oc) return false;
                        try {
                          // eslint-disable-next-line no-new-func
                          const fn = new Function(oc);
                          fn.call(el);
                          return true;
                        } catch (e) {
                          return false;
                        }
                      };
                      const fire = (el) => {
                        if (!el || !isVisible(el)) return false;
                        try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
                        try { el.click(); return true; } catch (e) {}
                        try { el.dispatchEvent(new MouseEvent('click', { bubbles:true, cancelable:true })); return true; } catch (e) {}
                        if (execOnclick(el)) return true;
                        return false;
                      };
                      const pad2 = String(current).padStart(2, '0');
                      const pad3 = String(current).padStart(3, '0');
                      const ids = [`que_${pad2}`, `que_${pad3}`, `que_${current}`];
                      let root = null;
                      for (const id of ids) {
                        const el = document.getElementById(id);
                        if (el && isVisible(el)) { root = el; break; }
                      }
                      if (!root) return false;

                      // SUB.doChoice 기반 핸들러가 있으면 우선 실행
                      const choiceHandlers = Array.from(root.querySelectorAll('[onclick*="doChoice"], [onclick*="DOCHOICE"], [onclick*="dochoice"]'))
                        .filter(isVisible);
                      if (choiceHandlers.length >= choice) {
                        const h = choiceHandlers[choice - 1];
                        if (execOnclick(h)) return true;
                        if (fire(h)) return true;
                      }
                      if (window.SUB && typeof window.SUB.doChoice === 'function') {
                        const tryArgs = [
                          [choice],
                          [current, choice],
                          [String(current), choice],
                          [`que_${pad2}`, choice],
                          [`que_${pad3}`, choice],
                        ];
                        for (const args of tryArgs) {
                          try {
                            window.SUB.doChoice.apply(window.SUB, args);
                            return true;
                          } catch (e) {}
                        }
                      }

                      const targetText = norm(optionText || '');
                      if (targetText) {
                        const txtCands = Array.from(root.querySelectorAll('label,li,td,tr,div,span,a,button,p')).filter(isVisible);
                        for (const el of txtCands) {
                          const txt = norm(el.textContent || el.value || '');
                          if (!txt || txt.length > 300) continue;
                          if (txt.includes(targetText)) {
                            if (fire(el)) return true;
                            const linked = el.querySelector('input[type=\"radio\"],input[type=\"checkbox\"],label,a,button');
                            if (fire(linked)) return true;
                          }
                        }
                      }

                      const numbered = Array.from(root.querySelectorAll('label,li,td,tr,div,span,a,button,p')).filter((el) => {
                        if (!isVisible(el)) return false;
                        const txt = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                        return /^([①②③④⑤]|[1-5][\\.)]|[1-5]번)\\s*/.test(txt);
                      });
                      if (numbered.length >= choice) {
                        if (fire(numbered[choice - 1])) return true;
                      }

                      const radios = Array.from(root.querySelectorAll('input[type=\"radio\"],input[type=\"checkbox\"]')).filter(isVisible);
                      if (radios.length >= choice) {
                        if (fire(radios[choice - 1])) return true;
                      }
                      return false;
                    }
                    """,
                    {"choice": choice, "optionText": option_text, "current": current},
                )
                if clicked_in_current:
                    page.wait_for_timeout(250)
                    if _has_selected_answer(scope, current):
                        return True
            except Exception:  # noqa: BLE001
                pass

        # 1) 라디오/체크박스 직접 조작 (표준 form)
        for scope in scopes:
            try:
                radios = scope.locator('input[type="radio"], input[type="checkbox"]')
                cnt = radios.count()
                if cnt >= choice:
                    target = radios.nth(choice - 1)
                    try:
                        target.check(force=True)
                    except Exception:  # noqa: BLE001
                        try:
                            target.click(force=True)
                        except Exception:  # noqa: BLE001
                            pass
                    page.wait_for_timeout(250)
                    if _has_selected_answer(scope, current):
                        return True
            except Exception:  # noqa: BLE001
                pass

        # 2) 라디오/라벨/컨테이너 텍스트 매칭 폴백 (page + frames 모두)
        choice_tokens = {
            1: ["①", "1.", "1)", "1번"],
            2: ["②", "2.", "2)", "2번"],
            3: ["③", "3.", "3)", "3번"],
            4: ["④", "4.", "4)", "4번"],
            5: ["⑤", "5.", "5)", "5번"],
        }.get(choice, [f"{choice}.", f"{choice})"])

        for scope in scopes:
            try:
                clicked = scope.evaluate(
                    """
                    ({ tokens, optionText, choice }) => {
                      const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                      const norm = (txt) => normalize(txt).toLowerCase();
                      const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const execOnclick = (el) => {
                        const oc = (el && el.getAttribute && el.getAttribute('onclick')) || '';
                        if (!oc) return false;
                        try {
                          // eslint-disable-next-line no-new-func
                          const fn = new Function(oc);
                          fn.call(el);
                          return true;
                        } catch (e) {
                          return false;
                        }
                      };
                      const fireClick = (el) => {
                        if (!el) return false;
                        try { el.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (e) {}
                        try { el.click(); return true; } catch (e) {}
                        try { el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); return true; } catch (e) {}
                        if (execOnclick(el)) return true;
                        return false;
                      };
                      const clickBestTarget = (container) => {
                        if (!container || !isVisible(container)) return false;
                        const input = container.querySelector('input[type="radio"],input[type="checkbox"]');
                        if (input && fireClick(input)) return true;
                        const label = container.querySelector('label');
                        if (label && fireClick(label)) return true;
                        const btn = container.querySelector('button,a,span');
                        if (btn && fireClick(btn)) return true;
                        return fireClick(container);
                      };

                      const cands = Array.from(
                        document.querySelectorAll(
                          'label,li,td,tr,div,span,a,button,p'
                        )
                      );
                      const doChoiceHandlers = Array.from(
                        document.querySelectorAll('[onclick*="doChoice"], [onclick*="DOCHOICE"], [onclick*="dochoice"]')
                      ).filter(isVisible);
                      if (doChoiceHandlers.length >= choice) {
                        const h = doChoiceHandlers[choice - 1];
                        if (execOnclick(h)) return true;
                        if (fireClick(h)) return true;
                      }
                      if (window.SUB && typeof window.SUB.doChoice === 'function') {
                        const tryArgs = [
                          [choice],
                          [String(choice)],
                        ];
                        for (const args of tryArgs) {
                          try {
                            window.SUB.doChoice.apply(window.SUB, args);
                            return true;
                          } catch (e) {}
                        }
                      }
                      const optNorm = norm(optionText || '');

                      // 2-1) 선택지 텍스트 직접 매칭 우선
                      if (optNorm) {
                        for (const el of cands) {
                          if (!isVisible(el)) continue;
                          const txt = norm(el.textContent || el.value || '');
                          if (!txt || txt.length > 300) continue;
                          if (txt.includes(optNorm)) {
                            if (clickBestTarget(el)) return true;
                          }
                        }
                      }

                      // 2-2) 번호 토큰 매칭
                      for (const el of cands) {
                        if (!isVisible(el)) continue;
                        const raw = normalize(el.textContent || el.value || '');
                        const txt = raw.toLowerCase();
                        if (!txt || txt.length > 280) continue;
                        if (tokens.some((t) => raw.startsWith(t) || raw.includes(` ${t} `) || raw.includes(t))) {
                          if (clickBestTarget(el)) return true;
                        }
                      }

                      // 2-3) for/id 라벨 연결로 n번째 항목 시도
                      const labels = Array.from(document.querySelectorAll('label')).filter(isVisible);
                      if (labels.length >= choice) {
                        const lb = labels[choice - 1];
                        const fid = lb.getAttribute('for');
                        if (fid) {
                          const linked = document.getElementById(fid);
                          if (linked && fireClick(linked)) return true;
                        }
                        if (fireClick(lb)) return true;
                      }

                      return false;
                    }
                    """,
                    {"tokens": choice_tokens, "optionText": option_text, "choice": choice},
                )
                if clicked:
                    page.wait_for_timeout(250)
                    if _has_selected_answer(scope, current):
                        return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def _click_exam_submit_if_present(self, page: Page) -> bool:
        selectors = [
            'button:has-text("답안 제출하기")',
            'a:has-text("답안 제출하기")',
            'button:has-text("최종 제출")',
            'a:has-text("최종 제출")',
            'button:has-text("제출")',
            'a:has-text("제출")',
            'input[value*="제출"]',
            'button:has-text("완료")',
            'a:has-text("완료")',
            'button:has-text("채점")',
            'a:has-text("채점")',
            'button:has-text("시험 종료")',
            'a:has-text("시험 종료")',
        ]
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            if self._click_first_visible(scope, selectors, max_items=12):
                page.wait_for_timeout(1000)
                return True
        for scope in scopes:
            try:
                clicked = scope.evaluate(
                    """
                    () => {
                      const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const cands = Array.from(
                        document.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                      );
                      const target = cands.find((el) => {
                        const txt = normalize(el.textContent || el.value);
                        return isVisible(el) && (
                          txt.includes('답안 제출')
                          || txt.includes('최종 제출')
                          || txt.includes('제출')
                          || txt.includes('완료')
                          || txt.includes('채점')
                          || txt.includes('시험 종료')
                        );
                      });
                      if (!target) return false;
                      try { target.click(); } catch (e) {
                        target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                      }
                      return true;
                    }
                    """
                )
                if clicked:
                    page.wait_for_timeout(1000)
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def _wait_exam_question_change(
        self, page: Page, prev_key: str, prev_current: int = 0, prev_total: int = 0, timeout_ms: int = 12000
    ) -> bool:
        ticks = max(1, timeout_ms // 500)
        for _ in range(ticks):
            page.wait_for_timeout(500)
            snap = self._extract_exam_question_snapshot(page)
            if snap is None:
                continue
            now_key = str(snap.get("key", ""))
            now_current = int(snap.get("current", 0))
            now_total = int(snap.get("total", 0))
            if now_key and prev_key and now_key != prev_key:
                return True
            if prev_current > 0 and now_current > 0 and now_current != prev_current:
                return True
            if prev_total > 0 and now_total > 0 and now_total != prev_total:
                return True
        return False

    @staticmethod
    def _is_exam_last_question(current: int, total: int, total_hint: int) -> bool:
        if current <= 0:
            return False
        if total > 0:
            return current >= total
        if total_hint > 0:
            return current >= total_hint
        return False

    def _has_exam_submit_control(self, page: Page) -> bool:
        selectors = [
            'button:has-text("답안 제출하기")',
            'a:has-text("답안 제출하기")',
            'button:has-text("최종 제출")',
            'a:has-text("최종 제출")',
            'button:has-text("제출")',
            'a:has-text("제출")',
            'input[value*="제출"]',
            'button:has-text("시험 종료")',
            'a:has-text("시험 종료")',
        ]
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            try:
                for selector in selectors:
                    loc = scope.locator(selector)
                    if loc.count() > 0 and loc.first.is_visible():
                        return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _diagnose_exam_transition_block(
        self, exam_page: Page, current: int, dialog_messages: Optional[list[str]] = None
    ) -> str:
        latest_dialog = ""
        if dialog_messages:
            latest_dialog = str(dialog_messages[-1]).strip()

        diag_parts: list[str] = []
        if latest_dialog:
            diag_parts.append(f"latest_dialog='{latest_dialog}'")

        scopes: list[Any] = [exam_page] + list(exam_page.frames)
        for scope in scopes:
            try:
                info = scope.evaluate(
                    """
                    ({ current }) => {
                      const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                      const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const pad2 = String(current).padStart(2, '0');
                      const pad3 = String(current).padStart(3, '0');
                      const ids = [`que_${pad2}`, `que_${pad3}`, `que_${current}`];
                      const curBox = ids.map((id) => document.getElementById(id)).find((el) => el && isVisible(el));

                      const checkedCount = document.querySelectorAll(
                        'input[type="radio"]:checked, input[type="checkbox"]:checked'
                      ).length;
                      const nextBtn = Array.from(
                        (curBox || document).querySelectorAll('a,button,input[type="button"],input[type="submit"]')
                      ).find((el) => {
                        if (!isVisible(el)) return false;
                        const txt = normalize(el.textContent || el.value || '').toLowerCase();
                        const oc = (el.getAttribute('onclick') || '').toLowerCase();
                        return txt.includes('다음') || txt.includes('next') || oc.includes('donextshowitem');
                      });

                      const subKeys = [];
                      if (window.SUB && typeof window.SUB === 'object') {
                        for (const k of Object.keys(window.SUB)) {
                          if (/next|answer|que|check|choice|select|item|show/i.test(k)) subKeys.push(k);
                        }
                      }
                      return {
                        currentBoxId: curBox ? (curBox.id || '') : '',
                        checkedCount,
                        nextText: nextBtn ? normalize(nextBtn.textContent || nextBtn.value || '') : '',
                        nextOnclick: nextBtn ? ((nextBtn.getAttribute('onclick') || '').slice(0, 180)) : '',
                        subKeys: subKeys.slice(0, 25),
                      };
                    }
                    """,
                    {"current": current},
                )
                current_box = str(info.get("currentBoxId", "")).strip()
                checked_count = int(info.get("checkedCount", 0))
                next_text = str(info.get("nextText", "")).strip()
                next_onclick = str(info.get("nextOnclick", "")).strip()
                sub_keys = ",".join([str(x) for x in info.get("subKeys", []) if str(x).strip()])
                if current_box or next_text or next_onclick or sub_keys:
                    diag_parts.append(
                        "scope_diag="
                        f"box={current_box or '-'}, checked={checked_count}, "
                        f"next='{next_text or '-'}', onclick='{next_onclick or '-'}', "
                        f"SUB=[{sub_keys}]"
                    )
                    break
            except Exception:  # noqa: BLE001
                continue

        if not diag_parts:
            return "diag=none"
        return " | ".join(diag_parts)

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
            "총점",
            "취득점수",
            "합격",
            "불합격",
            "평가 결과",
            "수료",
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

    def _stabilize_exam_page(self, exam_page: Page, timeout_ms: int = 15000) -> None:
        try:
            exam_page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 10000))
        except Exception:  # noqa: BLE001
            pass
        try:
            exam_page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
        except Exception:  # noqa: BLE001
            pass

        ticks = max(1, timeout_ms // 400)
        shell_ready_seen = False
        for idx in range(ticks):
            try:
                body = exam_page.locator("body").inner_text(timeout=1200)
                body_str = (body or "").strip()
                if len(body_str) >= 40:
                    parsed = self._parse_exam_text_payload(body_str)
                    if parsed is not None and int(parsed.get("option_count", 0)) >= 2:
                        return

                    # 문제 틀(타이머/문항수/제출버튼)만 먼저 보이는 케이스에서
                    # 선택지 렌더링을 조금 더 기다립니다.
                    if "답안 제출하기" in body_str and re.search(r"\d{1,3}\s*/\s*\d{1,3}", body_str):
                        shell_ready_seen = True
                    if shell_ready_seen and idx >= int(ticks * 0.75):
                        return
            except Exception:  # noqa: BLE001
                pass
            exam_page.wait_for_timeout(400)

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
            'div:has-text("학습 차시") a:has-text("학습 하기")',
            'div:has-text("학습 차시") a:has-text("이어 학습하기")',
            'div:has-text("학습 차시") a:has-text("이어 학습 하기")',
            'div:has-text("학습 차시") button:has-text("학습하기")',
            'div:has-text("학습 차시") button:has-text("학습 하기")',
            'div:has-text("학습 차시") button:has-text("이어 학습하기")',
            'div:has-text("학습 차시") button:has-text("이어 학습 하기")',
            'div:has-text("학습 차시") input[value*="학습하기"]',
            'div:has-text("학습 차시") input[value*="학습 하기"]',
            'div:has-text("학습 차시") input[value*="이어 학습하기"]',
            'div:has-text("학습 차시") input[value*="이어 학습 하기"]',
            'div:has-text("학습차시") a:has-text("학습하기")',
            'div:has-text("학습차시") a:has-text("학습 하기")',
            'div:has-text("학습차시") a:has-text("이어 학습하기")',
            'div:has-text("학습차시") a:has-text("이어 학습 하기")',
            'div:has-text("학습차시") button:has-text("학습하기")',
            'div:has-text("학습차시") button:has-text("학습 하기")',
            'div:has-text("학습차시") button:has-text("이어 학습하기")',
            'div:has-text("학습차시") button:has-text("이어 학습 하기")',
            'div:has-text("학습차시") input[value*="학습하기"]',
            'div:has-text("학습차시") input[value*="학습 하기"]',
            'div:has-text("학습차시") input[value*="이어 학습하기"]',
            'div:has-text("학습차시") input[value*="이어 학습 하기"]',
            'div:has-text("학습진행현황") a:has-text("학습하기")',
            'div:has-text("학습진행현황") a:has-text("학습 하기")',
            'div:has-text("학습진행현황") a:has-text("이어 학습하기")',
            'div:has-text("학습진행현황") a:has-text("이어 학습 하기")',
            'div:has-text("학습진행현황") button:has-text("학습하기")',
            'div:has-text("학습진행현황") button:has-text("학습 하기")',
            'div:has-text("학습진행현황") button:has-text("이어 학습하기")',
            'div:has-text("학습진행현황") button:has-text("이어 학습 하기")',
            'a[onclick*="doStudyPopup"]',
            'a[onclick*="doLearning"]',
            'span[onclick*="doFirstScript"]',
            'a:has-text("학습하기")',
            'a:has-text("학습 하기")',
            'a:has-text("이어 학습하기")',
            'a:has-text("이어 학습 하기")',
            'button:has-text("학습하기")',
            'button:has-text("학습 하기")',
            'button:has-text("이어 학습하기")',
            'button:has-text("이어 학습 하기")',
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
            clicked = bool(
                page.evaluate(
                    """
                    () => {
                      const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                      const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const isLessonBtn = (txt, oc, href) => {
                        const compact = String(txt || '').replace(/\\s+/g, '');
                        if (compact === '학습하기' || compact === '이어학습하기') return true;
                        if (oc.includes('doStudyPopup') || oc.includes('doLearning') || oc.includes('doFirstScript')) return true;
                        if (href.includes('doStudyPopup') || href.includes('doLearning')) return true;
                        return false;
                      };
                      const lessonNoFrom = (txt) => {
                        const m = String(txt || '').match(/(\\d{1,3})\\s*차시/);
                        return m ? parseInt(m[1], 10) : 9999;
                      };
                      const rows = Array.from(document.querySelectorAll('tr,li,div,section,article'));
                      let best = null;
                      for (const row of rows) {
                        const rowText = normalize(row.innerText || row.textContent || '');
                        if (!rowText.includes('차시')) continue;
                        const lessonNo = lessonNoFrom(rowText);
                        if (lessonNo >= 9999) continue;
                        const cands = Array.from(
                          row.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                        );
                        const btn = cands.find((el) => {
                          if (!isVisible(el)) return false;
                          const txt = normalize(el.textContent || el.value || '');
                          const oc = String(el.getAttribute('onclick') || '');
                          const href = String(el.getAttribute('href') || '');
                          return isLessonBtn(txt, oc, href);
                        });
                        if (!btn) continue;
                        if (!best || lessonNo < best.lessonNo) {
                          best = { lessonNo, btn };
                        }
                      }
                      if (!best) return false;
                      try { best.btn.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                      try { best.btn.click(); return true; } catch (e) {}
                      try { best.btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); return true; } catch (e) {}
                      return false;
                    }
                    """
                )
            )

        if not clicked:
            clicked = bool(
                page.evaluate(
                    """
                    () => {
                      const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                      const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const isLessonBtn = (txt) => (
                        String(txt || '').replace(/\\s+/g, '') === '학습하기'
                        || String(txt || '').replace(/\\s+/g, '') === '이어학습하기'
                      );
                      const cands = Array.from(
                        document.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                      );
                      const scored = cands
                        .map((el) => {
                          if (!isVisible(el)) return null;
                          const txt = normalize(el.textContent || el.value || '');
                          const oc = String(el.getAttribute('onclick') || '');
                          const href = String(el.getAttribute('href') || '');
                          const inHint = normalize((el.closest('tr,li,div,section,article')?.innerText || ''));
                          let score = 0;
                          if (isLessonBtn(txt)) score += 4;
                          if (oc.includes('doStudyPopup') || oc.includes('doLearning') || oc.includes('doFirstScript')) score += 3;
                          if (href.includes('doStudyPopup') || href.includes('doLearning')) score += 2;
                          if (inHint.includes('학습 차시') || inHint.includes('학습차시') || inHint.includes('학습진행현황')) score += 2;
                          if (score <= 0) return null;
                          return { el, score };
                        })
                        .filter(Boolean)
                        .sort((a, b) => b.score - a.score);
                      if (!scored.length) return false;
                      const target = scored[0].el;
                      try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                      try { target.click(); return true; } catch (e) {}
                      try { target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); return true; } catch (e) {}
                      return false;
                    }
                    """
                )
            )

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
        self._log("학습시간 전용 선택자 실패: 일반 학습진행현황 선택자로 재시도합니다.")
        return self._start_learning_from_progress_panel(page)

    def _refresh_classroom_page(self, classroom_page: Page) -> None:
        # 중요: 강의(팝업/플레이어) 창이 아니라 강의실 메인 페이지만 새로고침합니다.
        try:
            if self._is_exam_url(classroom_page.url):
                self._log("시험 페이지 감지: 강의실 새로고침을 건너뜁니다.")
                return
        except Exception:  # noqa: BLE001
            pass
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
        progress_signal_seen = bool(progress_row)
        if progress_row:
            current_percent = self._parse_percent_value(str(progress_row.get("actual", "")))
            required_percent = self._parse_percent_value(str(progress_row.get("required", "")))

        current_url = ""
        try:
            current_url = classroom_page.url
        except Exception:  # noqa: BLE001
            current_url = ""
        try:
            body = classroom_page.locator("body").inner_text(timeout=5000)
        except Exception:  # noqa: BLE001
            body = ""

        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if current_percent == 0 or required_percent == 0:
            for line in lines:
                if "학습진도율" in line or "진도율" in line:
                    progress_signal_seen = True
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
        is_error_page = current_url.startswith("chrome-error://") or "ERR_" in body[:600]
        known = bool(progress_signal_seen) and not is_error_page
        if current_percent == 0 and required_percent == 0 and not progress_signal_seen:
            known = False
        progress_ok = (
            current_percent >= required_percent
            if required_percent > 0
            else current_percent >= 100
        )
        if known:
            self._log(
                "학습진도율 상태: "
                f"current={current_percent}% required={required_percent}% "
                f"incomplete={incomplete_count}"
            )
        else:
            self._log(
                "학습진도율 판독 불확실: "
                f"current={current_percent}% required={required_percent}% "
                f"incomplete={incomplete_count} url={current_url or '(unknown)'}"
            )
        return {
            "current_percent": current_percent,
            "required_percent": required_percent,
            "incomplete_count": incomplete_count,
            "progress_ok": progress_ok,
            "known": known,
        }

    def _extract_exam_attempt_status(self, classroom_page: Page) -> dict[str, int]:
        scopes: list[Any] = [classroom_page] + list(classroom_page.frames)
        for scope in scopes:
            try:
                text = scope.locator("body").inner_text(timeout=2000)
            except Exception:  # noqa: BLE001
                continue
            match = re.search(r"응시횟수\s*\(\s*(\d{1,2})\s*/\s*(\d{1,2})\s*\)", text)
            if not match:
                continue
            try:
                attempted = int(match.group(1))
                max_attempt = int(match.group(2))
            except ValueError:
                continue
            if max_attempt <= 0:
                continue
            remaining = max(0, max_attempt - attempted)
            return {
                "attempted": attempted,
                "max_attempt": max_attempt,
                "remaining": remaining,
            }
        return {"attempted": 0, "max_attempt": 0, "remaining": 0}

    def _extract_exam_requirement_status(self, classroom_page: Page) -> dict[str, Any]:
        rows = self._extract_completion_table_rows(classroom_page)
        exam_row = rows.get("시험평가")
        if not isinstance(exam_row, dict):
            return {
                "known": False,
                "has_exam": True,
                "required_text": "",
                "reason": "시험평가 row 미탐지",
            }

        required_text = str(exam_row.get("required", "") or "").strip()
        required_compact = re.sub(r"\s+", "", required_text).lower()
        no_exam_tokens = {
            "",
            "-",
            "--",
            "---",
            "—",
            "없음",
            "해당없음",
            "미해당",
            "n/a",
            "na",
        }

        no_exam = required_compact in no_exam_tokens
        if not no_exam and ("해당없음" in required_compact or "미해당" in required_compact):
            no_exam = True

        if no_exam:
            return {
                "known": True,
                "has_exam": False,
                "required_text": required_text,
                "reason": f"시험평가 수료기준={required_text or '(blank)'}",
            }
        return {
            "known": True,
            "has_exam": True,
            "required_text": required_text,
            "reason": f"시험평가 수료기준={required_text}",
        }

    def _enforce_exam_attempt_reserve(self, classroom_page: Page) -> Optional[LoginResult]:
        reserve = max(0, int(getattr(self.settings, "exam_attempt_reserve", 1)))
        status = self._extract_exam_attempt_status(classroom_page)
        attempted = int(status.get("attempted", 0))
        max_attempt = int(status.get("max_attempt", 0))
        remaining = int(status.get("remaining", 0))

        if max_attempt <= 0:
            self._log("응시횟수 판독 실패: 응시횟수 보호 규칙 검사는 건너뜁니다.")
            return None

        self._log(
            "시험 응시횟수 상태: "
            f"used={attempted}/{max_attempt}, remaining={remaining}, reserve={reserve}"
        )
        if remaining <= reserve:
            return LoginResult(
                False,
                f"응시횟수({attempted}/{max_attempt})로 남은 {remaining}회입니다. "
                f"각 강의당 마지막 {reserve}회 보존 규칙으로 자동 응시를 중단합니다.",
                classroom_page.url,
            )
        return None

    def _open_incomplete_lesson_popup(self, page: Page) -> Optional[Page]:
        before_pages = list(page.context.pages)
        try:
            page.locator('text=학습 차시').first.scroll_into_view_if_needed(timeout=2500)
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass

        selectors = [
            'div:has-text("미완료") a:has-text("학습하기")',
            'div:has-text("미완료") a:has-text("학습 하기")',
            'div:has-text("미완료") a:has-text("이어 학습하기")',
            'div:has-text("미완료") a:has-text("이어 학습 하기")',
            'div:has-text("미완료") button:has-text("학습하기")',
            'div:has-text("미완료") button:has-text("학습 하기")',
            'div:has-text("미완료") button:has-text("이어 학습하기")',
            'div:has-text("미완료") button:has-text("이어 학습 하기")',
            'tr:has-text("미완료") a:has-text("학습하기")',
            'tr:has-text("미완료") a:has-text("학습 하기")',
            'tr:has-text("미완료") a:has-text("이어 학습하기")',
            'tr:has-text("미완료") a:has-text("이어 학습 하기")',
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
                    String(txt || '').replace(/\\s+/g, '') === '학습하기'
                    || String(txt || '').replace(/\\s+/g, '') === '이어학습하기'
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

    def _ensure_time_requirement_before_course_skip(
        self,
        classroom_page: Page,
        *,
        default_interval_minutes: int,
        check_limit: int,
    ) -> Optional[LoginResult]:
        self._refresh_classroom_page(classroom_page)
        time_status = self._extract_study_time_status(classroom_page)
        known_required_seconds = (
            int(time_status.get("required_seconds", 0))
            if bool(time_status.get("requirement_known", False))
            else 0
        )
        if self._is_time_requirement_satisfied(
            time_status=time_status,
            required_seconds_floor=known_required_seconds,
        ):
            return None

        self._log("과정 우회 전 학습시간 미달 감지: 학습시간 보충을 먼저 진행합니다.")
        keepalive_page = self._open_first_lesson_popup_for_timefill(classroom_page)
        if keepalive_page is None:
            return LoginResult(False, "학습시간 보충용 1차시 학습창을 열지 못했습니다.", classroom_page.url)

        limit = max(1, min(int(check_limit), 72))
        for idx in range(limit):
            wait_minutes = self._decide_timefill_check_interval_minutes(
                time_status=time_status,
                default_minutes=int(default_interval_minutes),
            )
            self._log(
                f"우회 전 학습시간 보충 대기: {idx + 1}/{limit} "
                f"(다음 확인 {wait_minutes}분 후, 남은시간 "
                f"{self._format_seconds(int(time_status.get('shortage_seconds', 0)))} )"
            )
            waited = self._wait_with_page_guard(keepalive_page, wait_minutes * 60 * 1000)
            if not waited:
                self._log("우회 전 학습시간 보충 대기 중 학습창 종료 감지: 학습창 재오픈을 시도합니다.")
                keepalive_page = self._open_first_lesson_popup_for_timefill(classroom_page)
                if keepalive_page is None:
                    return LoginResult(False, "우회 전 학습시간 보충 학습창이 종료되어 재오픈에 실패했습니다.", classroom_page.url)
                continue
            self._refresh_classroom_page(classroom_page)
            time_status = self._extract_study_time_status(classroom_page)
            if bool(time_status.get("requirement_known", False)):
                known_required_seconds = max(
                    known_required_seconds,
                    int(time_status.get("required_seconds", 0)),
                )
            if self._is_time_requirement_satisfied(
                time_status=time_status,
                required_seconds_floor=known_required_seconds,
            ):
                self._log("우회 전 학습시간 보충 완료: 수료기준 충족")
                return None

        return LoginResult(
            False,
            "과정 우회 전 학습시간 보충 제한 횟수 내에 수료기준을 충족하지 못했습니다.",
            classroom_page.url,
        )

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

    @staticmethod
    def _decide_timefill_check_interval_minutes(
        time_status: dict[str, int | bool], default_minutes: int
    ) -> int:
        base = max(3, min(10, int(default_minutes)))
        if not isinstance(time_status, dict):
            return base
        if not bool(time_status.get("requirement_known", False)):
            return base

        try:
            shortage = max(0, int(time_status.get("shortage_seconds", 0)))
            required = max(0, int(time_status.get("required_seconds", 0)))
        except Exception:  # noqa: BLE001
            return base

        if shortage <= 0:
            return 3

        ratio = (float(shortage) / float(required)) if required > 0 else 0.0
        if shortage >= 2 * 3600 or ratio >= 0.70:
            return 10
        if shortage >= 3600 or ratio >= 0.45:
            return 9
        if shortage >= 1800 or ratio >= 0.25:
            return 8
        if shortage >= 900 or ratio >= 0.12:
            return 7
        if shortage >= 300:
            return 6
        if shortage >= 180:
            return 5
        if shortage >= 60:
            return 4
        return 3

    @staticmethod
    def _is_time_requirement_satisfied(
        time_status: dict[str, int | bool], required_seconds_floor: int = 0
    ) -> bool:
        if not isinstance(time_status, dict):
            return False
        try:
            current = max(0, int(time_status.get("current_seconds", 0)))
            required = max(0, int(time_status.get("required_seconds", 0)))
            shortage = max(0, int(time_status.get("shortage_seconds", 0)))
        except Exception:  # noqa: BLE001
            return False

        if bool(time_status.get("requirement_known", False)):
            return shortage <= 0
        floor_required = max(0, int(required_seconds_floor))
        if floor_required > 0 and current >= floor_required:
            return True
        return required > 0 and current >= required

    def _ensure_course_completed(self, classroom_page: Page) -> Optional[LoginResult]:
        state = self._extract_course_completion_state(classroom_page)
        known = bool(state.get("known", False))
        completed = bool(state.get("completed", False))
        reason = str(state.get("reason", "")).strip()

        if not known:
            return LoginResult(
                False,
                "과정 수료 상태(수료가능/수료완료)를 판독하지 못했습니다. 강의목록에서 상태를 확인해 주세요.",
                classroom_page.url,
            )
        if not completed:
            return LoginResult(
                False,
                f"시험 최종 제출 후 수료처리 미완료(또는 수료점수 미달)로 판단됩니다. {reason}",
                classroom_page.url,
            )
        self._log(f"과정 수료 상태 확인: 완료 ({reason})")
        return None

    def _extract_course_completion_state(self, classroom_page: Page) -> dict[str, Any]:
        # 우선순위: passOrFailTarget / 수료 배지 > 수료점수/시험평가 row > 본문 키워드
        known = False
        completed = False
        reason = ""
        exam_panel = self._extract_exam_result_panel(classroom_page)

        try:
            dom = classroom_page.evaluate(
                """
                () => {
                  const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                  const passOrFailTarget = normalize(document.querySelector('#passOrFailTarget')?.textContent || '');
                  const passProgresTarget = normalize(document.querySelector('#passProgresTarget')?.textContent || '');
                  const passOrFailTd = normalize(document.querySelector('#passOrFailTd')?.innerText || '');
                  return { passOrFailTarget, passProgresTarget, passOrFailTd };
                }
                """
            )
            pass_or_fail = str(dom.get("passOrFailTarget", "")).lower()
            pass_progress = str(dom.get("passProgresTarget", "")).lower()
            badge_text = str(dom.get("passOrFailTd", ""))

            if pass_or_fail in {"pass", "fail"}:
                known = True
                completed = pass_or_fail == "pass"
                reason = f"passOrFailTarget={pass_or_fail}"
            elif "수료완료" in badge_text or "수료가능" in badge_text:
                known = True
                completed = True
                reason = "passOrFailTd 배지=수료가능/수료완료"
            elif "수료불가능" in badge_text:
                known = True
                completed = False
                reason = "passOrFailTd 배지=수료불가능"
            elif pass_progress == "fail":
                known = True
                completed = False
                reason = "passProgresTarget=fail"
        except Exception:  # noqa: BLE001
            pass

        rows = self._extract_completion_table_rows(classroom_page)
        score_row = rows.get("수료점수", {})
        exam_row = rows.get("시험평가", {})
        for name, row in [("수료점수", score_row), ("시험평가", exam_row)]:
            blob = " ".join(
                [
                    str(row.get("required", "")),
                    str(row.get("actual", "")),
                    str(row.get("result", "")),
                ]
            ).lower()
            if not blob.strip():
                continue
            if "fail" in blob or "불합격" in blob:
                return {"known": True, "completed": False, "reason": f"{name}=fail"}
            if "pass" in blob or "합격" in blob:
                known = True
                completed = True
                reason = f"{name}=pass"

        if score_row:
            req_score = self._parse_score_value(str(score_row.get("required", "")))
            act_score = self._parse_score_value(str(score_row.get("actual", "")))
            if req_score is not None and act_score is not None:
                known = True
                completed = act_score >= req_score
                reason = f"수료점수 actual={act_score:.1f} required={req_score:.1f}"

        # 보조 신호: 종합평가 결과 버튼(item result + resultYn='Y')과 점수 텍스트
        panel_openable = bool(exam_panel.get("result_openable", False))
        panel_score = exam_panel.get("score")
        if panel_openable:
            panel_reason = "시험결과 버튼(resultYn=Y) 감지"
            if isinstance(panel_score, (int, float)):
                panel_reason += f" score={float(panel_score):.1f}"

            if not known:
                req_score = self._parse_score_value(str(score_row.get("required", "")))
                if req_score is None:
                    req_score = self._parse_score_value(str(exam_row.get("required", "")))
                if isinstance(panel_score, (int, float)) and req_score is not None:
                    known = True
                    completed = float(panel_score) >= float(req_score)
                    reason = f"{panel_reason} required={req_score:.1f}"
                else:
                    known = True
                    completed = False
                    reason = panel_reason
            elif reason:
                reason = f"{reason}; {panel_reason}"
            else:
                reason = panel_reason

        if not known:
            try:
                body = classroom_page.locator("body").inner_text(timeout=5000)
            except Exception:  # noqa: BLE001
                body = ""
            if "수료완료" in body or "수료가능" in body:
                known = True
                completed = True
                reason = "본문 키워드=수료가능/수료완료"
            elif "수료불가능" in body:
                known = True
                completed = False
                reason = "본문 키워드=수료불가능"

        return {"known": known, "completed": completed, "reason": reason}

    def _extract_exam_result_panel(self, classroom_page: Page) -> dict[str, Any]:
        try:
            info = classroom_page.evaluate(
                """
                () => {
                  const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                  const parseScore = (txt) => {
                    const m = (txt || '').match(/(\\d{1,3}(?:\\.\\d+)?)\\s*점/);
                    return m ? Number(m[1]) : null;
                  };
                  const parseResultYn = (onclick) => {
                    const src = String(onclick || '');
                    const m = src.match(/resultyn\\s*:\\s*['"]?([YN])['"]?/i);
                    if (m) return String(m[1] || '').toUpperCase();
                    const m2 = src.match(/resultYn\\s*:\\s*['"]?([YN])['"]?/);
                    if (m2) return String(m2[1] || '').toUpperCase();
                    return '';
                  };
                  const anchors = Array.from(
                    document.querySelectorAll(
                      'a.item.result, a[class*="item"][class*="result"], '
                      + 'a[onclick*="doExamPaperPopup"], a[onclick*="doexampaperpopup"]'
                    )
                  );
                  let picked = null;
                  for (const a of anchors) {
                    const oc = a.getAttribute('onclick') || '';
                    const txt = normalize(a.innerText || a.textContent || '');
                    const around = normalize((a.closest('.info')?.innerText || a.parentElement?.innerText || '') + ' ' + txt);
                    const resultYn = parseResultYn(oc);
                    const score = parseScore(around);
                    const hasPopupFn = /doexampaperpopup/i.test(oc);
                    const isResultCls = (a.className || '').toLowerCase().includes('result');
                    const isOpenable = hasPopupFn && resultYn === 'Y';
                    const candidate = {
                      resultYn,
                      score,
                      openable: isOpenable,
                      hasPopupFn,
                      isResultCls,
                      text: txt,
                      around,
                      onclick: oc,
                    };
                    if (!picked) {
                      picked = candidate;
                      continue;
                    }
                    const pickedScore = picked.score == null ? -1 : Number(picked.score);
                    const candScore = score == null ? -1 : Number(score);
                    if ((isOpenable && !picked.openable) || (candScore > pickedScore)) {
                      picked = candidate;
                    }
                  }
                  if (!picked) {
                    return { result_openable: false, score: null, resultYn: "", text: "", onclick: "", around: "" };
                  }
                  return {
                    result_openable: !!picked.openable,
                    score: picked.score,
                    resultYn: picked.resultYn || "",
                    text: picked.text || "",
                    onclick: picked.onclick || "",
                    around: picked.around || "",
                  };
                }
                """
            )
        except Exception:  # noqa: BLE001
            return {"result_openable": False, "score": None, "resultYn": "", "text": "", "onclick": "", "around": ""}

        score_val = info.get("score")
        try:
            score = float(score_val) if score_val is not None else None
        except Exception:  # noqa: BLE001
            score = None
        return {
            "result_openable": bool(info.get("result_openable", False)),
            "score": score,
            "resultYn": str(info.get("resultYn", "") or ""),
            "text": str(info.get("text", "") or ""),
            "onclick": str(info.get("onclick", "") or ""),
            "around": str(info.get("around", "") or ""),
        }

    @staticmethod
    def _parse_score_value(text: str) -> Optional[float]:
        src = (text or "").replace(",", " ")
        m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*점", src)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        # "70점 이상"과 같은 포맷에서 점수가 붙지 않은 숫자만 있는 경우 보조 파싱
        m2 = re.search(r"(\d{1,3}(?:\.\d+)?)", src)
        if m2:
            try:
                return float(m2.group(1))
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_exam_score_from_message(message: str) -> Optional[float]:
        src = (message or "").replace(",", " ")
        patterns = [
            r"actual\s*=\s*([0-9]{1,3}(?:\.[0-9]+)?)",
            r"score\s*=\s*([0-9]{1,3}(?:\.[0-9]+)?)",
            r"([0-9]{1,3}(?:\.[0-9]+)?)\s*점",
        ]
        for pattern in patterns:
            m = re.search(pattern, src, flags=re.IGNORECASE)
            if not m:
                continue
            try:
                return float(m.group(1))
            except ValueError:
                continue
        return None

    def _close_post_exam_transient_pages(self, pages: list[Page], keep_pages: list[Page]) -> None:
        keep_ids = {id(p) for p in keep_pages if p is not None}
        for p in list(pages):
            try:
                if p.is_closed():
                    continue
            except Exception:  # noqa: BLE001
                continue

            if id(p) in keep_ids:
                continue

            try:
                url = p.url.lower()
            except Exception:  # noqa: BLE001
                url = ""

            should_close = any(
                key in url
                for key in [
                    "/usr/classroom/exampaper/",
                    "surver",
                    "survey",
                    "popup",
                    "layer.do",
                ]
            )
            if not should_close:
                try:
                    txt = p.locator("body").inner_text(timeout=1200)
                except Exception:  # noqa: BLE001
                    txt = ""
                should_close = any(k in txt for k in ["설문", "종합평가", "답안 제출하기", "시험 시작하기"])

            if should_close:
                self._close_if_transient_page(p, keep_pages[0])

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
            "a.next",
            "button.next",
            'button:has-text("다음")',
            'a:has-text("다음")',
            'button:has-text(">")',
            'a:has-text(">")',
            'button:has-text("›")',
            'a:has-text("›")',
            '[role="button"]:has-text("다음")',
            '[aria-label*="다음"]',
            '[aria-label*="next"]',
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
                      const txt = (n.textContent || '').trim().toLowerCase();
                      const aria = String(n.getAttribute('aria-label') || n.getAttribute('title') || '').trim().toLowerCase();
                      const pass = txt === '다음' || txt === 'next' || txt === '>' || txt === '›' || txt === '＞' || txt === '→';
                      if (!pass && !aria.includes('next') && !aria.includes('다음')) return false;
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
            'a.next:has-text(">")',
            'a.next:has-text("›")',
            '[aria-label*="다음"]',
            '[aria-label*="next"]',
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
            lowered = current_url.lower()
            login_like_paths = [
                "/common/login/loginpage.do",
                "/login/loginpage.do",
                "/common/login/",
                "/member/login",
            ]
            if any(path in lowered for path in login_like_paths):
                self._log("URL 변경을 감지했지만 로그인 페이지로 판단되어 성공 판정을 보류합니다.")
            else:
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
        lower_message = (message or "").lower()
        is_exam_confirm = dialog.type == "confirm" and any(
            key in lower_message
            for key in [
                "시험",
                "응시",
                "제출",
                "답안",
                "종료",
                "브라우저 off",
                "뒤로가기",
                "비정상 종료",
            ]
        )
        if is_exam_confirm:
            dialog.accept()
        else:
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
