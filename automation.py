from dataclasses import dataclass
import re
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
                        if completed_lessons > 0:
                            return LoginResult(
                                False,
                                f"{completed_lessons}개 차시 완료 후 중단: {complete_result.message}",
                                complete_result.current_url,
                            )
                        return complete_result

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
            total = page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                      const r = el.getBoundingClientRect();
                      return r.width > 0 && r.height > 0;
                    };
                    const getLabel = (el) => ((el.textContent || el.value || '').trim());
                    const isLessonBtn = (el) => {
                      const txt = getLabel(el);
                      return txt === '학습하기' || txt === '이어 학습하기' || txt === '이어학습하기';
                    };

                    // 1) 차시 목록 테이블 기준으로 우선 계산 (가장 정확)
                    const rowElems = Array.from(
                      document.querySelectorAll(
                        'tr a, tr button, tr input[type="button"], tr input[type="submit"], tr span'
                      )
                    ).filter((el) => isVisible(el) && isLessonBtn(el));
                    if (rowElems.length > 0) {
                      const rowSet = new Set();
                      for (const el of rowElems) {
                        const tr = el.closest('tr');
                        if (!tr) continue;
                        const key = (tr.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (key) rowSet.add(key);
                      }
                      if (rowSet.size > 0) return rowSet.size;
                      return rowElems.length;
                    }

                    // 2) 전체 페이지 기준 폴백: "모든 학습하기 개수 - 1" 보정
                    const allElems = Array.from(
                      document.querySelectorAll(
                        'a,button,input[type="button"],input[type="submit"],span'
                      )
                    ).filter((el) => isVisible(el) && isLessonBtn(el));
                    if (!allElems.length) return 0;
                    const rawCount = allElems.length;
                    // 강의실 전체 버튼에는 차시 외 진입용 버튼이 1개 포함되어 있어 -1 보정.
                    if (rawCount <= 1) return rawCount;
                    return rawCount - 1;
                }
                """
            )
        except Exception:  # noqa: BLE001
            return None

        if isinstance(total, int) and total > 0:
            return total
        return None

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

    def _enter_first_course_internal(self, page: Page) -> tuple[LoginResult, Optional[Page]]:
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

        learning_page = self._start_learning_from_progress_panel(target_page)
        if learning_page is not None:
            return (
                LoginResult(
                    True,
                    f"학습 시작 클릭 성공(학습진행현황): {first_title or '첫 번째 과정'}",
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
        self._detected_total_lessons = self._extract_total_lessons_from_classroom_buttons(page)
        if self._detected_total_lessons:
            self._log(
                f"강의실 학습하기 버튼 개수 감지: 총 {self._detected_total_lessons}차시"
            )
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

    def _complete_lesson_steps(self, page: Page) -> LoginResult:
        self._log("차시 단계 자동 진행 시작")
        candidate_page = page
        max_clicks = 120
        step_wait_ms = 8000

        for _ in range(max_clicks):
            progress = self._wait_for_step_progress(candidate_page, wait_ms=12000)
            if progress is None:
                # 학습창 내 iframe 또는 다른 팝업 페이지를 다시 탐색
                picked = self._find_page_with_progress(candidate_page.context.pages)
                if picked is not None:
                    candidate_page = picked
                    progress = self._wait_for_step_progress(candidate_page, wait_ms=12000)

            if progress is None:
                self._dump_player_debug(candidate_page, "progress_not_found")
                return LoginResult(False, "현재/전체 단계 표시를 찾지 못했습니다.", candidate_page.url)

            current_step, total_step = progress
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
                next_clicked = False
                if all_blue:
                    next_clicked = self._click_final_next_if_available(candidate_page)
                else:
                    self._log("모든 단계 파란색 완료 확인 실패 (화면 상태 점검 필요)")
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
        timeout_ms = min(self.settings.timeout_ms, 70000)
        ticks = max(1, timeout_ms // 500)

        for _ in range(ticks):
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible():
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
