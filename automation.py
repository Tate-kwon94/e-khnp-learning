from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Optional
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

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
    class StopRequested(RuntimeError):
        pass

    def __init__(
        self,
        settings: Settings,
        log_fn: LogFn = None,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.settings = settings
        self.log_fn = log_fn
        self.stop_requested = stop_requested
        self._run_id = self._build_run_id()
        self._artifact_records: list[dict[str, Any]] = []
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
        self._answer_bank_optset_index: dict[str, list[dict[str, Any]]] = {}
        self._answer_bank_fuzzy_index: list[dict[str, Any]] = []
        self._verified_exam_course_order_index: dict[str, list[dict[str, Any]]] = {}
        self._verified_exam_qsig_index: dict[str, list[dict[str, Any]]] = {}
        self._deferred_courses_path = Path(
            getattr(self.settings, "exam_deferred_courses_path", ".runtime/deferred_exam_courses.json")
        )
        self._deferred_account_scope = self._build_deferred_account_scope()
        self._deferred_exam_course_history: dict[str, dict[str, Any]] = {}
        self._exam_quality_report_dir = Path(
            getattr(self.settings, "exam_quality_report_dir", "logs/exam_quality_reports")
        )
        self._question_evidence_fail_streak: dict[str, dict[str, Any]] = {}
        self._last_exam_solve_payload: dict[str, Any] = {}
        self._last_observed_course_progress_percent: int = -1
        self._last_opened_lesson_key: str = ""
        self._last_opened_lesson_title: str = ""
        self._last_opened_lesson_course_percent: int = -1
        self._last_proxy_preflight: dict[str, Any] = {}
        self._proxy_preflight_path = Path(".runtime") / "proxy_preflight_latest.json"
        self._answer_bank_course_order_optset_index: dict[str, list[dict[str, Any]]] = {}
        self._load_answer_bank()
        self._load_verified_exam_quality_index()
        self._load_deferred_exam_courses()

    def _load_answer_bank(self) -> None:
        self._answer_bank_items = {}
        self._answer_bank_qnorm_index = {}
        self._answer_bank_qsig_index = {}
        self._answer_bank_qsig_optset_index = {}
        self._answer_bank_optset_index = {}
        self._answer_bank_fuzzy_index = []
        self._answer_bank_course_order_optset_index = {}
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
            self._answer_bank_optset_index = {}
            self._answer_bank_fuzzy_index = []
            self._answer_bank_course_order_optset_index = {}

    def _load_verified_exam_quality_index(self) -> None:
        self._verified_exam_course_order_index = {}
        self._verified_exam_qsig_index = {}
        try:
            if not self._exam_quality_report_dir.exists():
                return
        except Exception:  # noqa: BLE001
            return

        reports = sorted(
            self._exam_quality_report_dir.glob("exam_quality_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        course_order_index: dict[str, list[dict[str, Any]]] = {}
        qsig_index: dict[str, list[dict[str, Any]]] = {}
        seen_entries: set[str] = set()

        for path in reports:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(raw, dict):
                continue
            meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
            rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
            course_title = str(meta.get("course_title", "") or "").strip()
            course_key = self._course_title_key(course_title)
            if not course_key:
                continue

            for row in rows:
                if not isinstance(row, dict):
                    continue
                question = str(row.get("question", "") or "").strip()
                correct_option = str(row.get("correct_option", "") or "").strip()
                if not question or not correct_option:
                    continue
                try:
                    correct_choice = int(row.get("correct_choice", 0) or 0)
                except Exception:  # noqa: BLE001
                    correct_choice = 0
                if correct_choice <= 0:
                    continue
                question_norm = self._normalize_question_text(question)
                question_sig = str(row.get("question_signature", "") or "").strip()
                normalized_sig = self._question_signature_from_norm(question_norm)
                try:
                    question_no = int(row.get("question_no", 0) or 0)
                except Exception:  # noqa: BLE001
                    question_no = 0

                entry = {
                    "course_title": course_title,
                    "course_key": course_key,
                    "question_no": question_no,
                    "question": question,
                    "question_norm": question_norm,
                    "question_signature": question_sig or normalized_sig,
                    "normalized_signature": normalized_sig,
                    "correct_choice": correct_choice,
                    "correct_option": correct_option,
                    "correct_option_norm": self._normalize_answer_text(correct_option),
                    "source_path": path.as_posix(),
                }
                entry_marker = "||".join(
                    [
                        course_key,
                        str(question_no),
                        question_sig or normalized_sig,
                        entry["correct_option_norm"],
                    ]
                )
                if entry_marker in seen_entries:
                    continue
                seen_entries.add(entry_marker)

                if question_no > 0:
                    course_order_index.setdefault(f"{course_key}||{question_no}", []).append(entry)
                for sig in {question_sig, normalized_sig}:
                    sig = str(sig or "").strip()
                    if sig:
                        qsig_index.setdefault(f"{course_key}||{sig}", []).append(entry)

        self._verified_exam_course_order_index = course_order_index
        self._verified_exam_qsig_index = qsig_index

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
        meta = raw.get("meta") if isinstance(raw, dict) else {}
        meta_scope = str(meta.get("account_scope", "")).strip() if isinstance(meta, dict) else ""
        for row in items:
            if not isinstance(row, dict):
                continue
            row_scope = str(row.get("account_scope", "")).strip()
            # 계정 스코프가 다르면 절대 로드하지 않습니다(다른 클라이언트/계정 이력 격리).
            if row_scope and row_scope != self._deferred_account_scope:
                continue
            # 레거시(스코프 없음) 레코드는 meta.account_scope가 현재와 일치할 때만 제한적으로 허용.
            if not row_scope and meta_scope != self._deferred_account_scope:
                continue
            key = self._course_title_key(str(row.get("title", "")))
            if not key:
                key = str(row.get("key", "")).strip().lower()
            if not key:
                continue
            title = str(row.get("title", "")).strip()
            reason = str(row.get("reason", "")).strip()
            updated_at = str(row.get("updated_at", "")).strip()
            payload = {
                "key": key,
                "title": title,
                "reason": reason,
                "updated_at": updated_at,
                "account_scope": self._deferred_account_scope,
            }
            self._deferred_exam_course_history[key] = payload
            self._deferred_exam_course_keys.add(key)

    def _save_deferred_exam_courses(self) -> None:
        try:
            self._deferred_courses_path.parent.mkdir(parents=True, exist_ok=True)
            existing_rows: list[dict[str, Any]] = []
            if self._deferred_courses_path.exists():
                try:
                    raw_prev = json.loads(self._deferred_courses_path.read_text(encoding="utf-8"))
                    items_prev = raw_prev.get("items") if isinstance(raw_prev, dict) else None
                    if isinstance(items_prev, list):
                        for row in items_prev:
                            if not isinstance(row, dict):
                                continue
                            row_scope = str(row.get("account_scope", "")).strip()
                            if row_scope and row_scope != self._deferred_account_scope:
                                existing_rows.append(dict(row))
                except Exception:  # noqa: BLE001
                    pass

            scoped_rows = []
            for row in self._deferred_exam_course_history.values():
                if not isinstance(row, dict):
                    continue
                merged_row = dict(row)
                merged_row["account_scope"] = self._deferred_account_scope
                scoped_rows.append(merged_row)

            rows = sorted(
                existing_rows + scoped_rows,
                key=lambda x: str(x.get("updated_at", "")),
                reverse=True,
            )
            payload = {
                "meta": {
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "count": len(rows),
                    "account_scope": self._deferred_account_scope,
                    "scoped_count": len(scoped_rows),
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
        optset_index: dict[str, list[dict[str, Any]]] = {}
        course_order_optset_index: dict[str, list[dict[str, Any]]] = {}
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
            if option_set_sig:
                optset_index.setdefault(option_set_sig, []).append(item)
            course_key = str(item.get("course_title_key", "")).strip()
            try:
                question_no = int(item.get("question_no", 0) or 0)
            except Exception:  # noqa: BLE001
                question_no = 0
            if course_key and question_no > 0 and option_set_sig:
                course_order_optset_index.setdefault(f"{course_key}||{question_no}||{option_set_sig}", []).append(item)
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
        self._answer_bank_optset_index = optset_index
        self._answer_bank_course_order_optset_index = course_order_optset_index
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

    def _is_classroom_access_denied(self, page: Optional[Page]) -> bool:
        if page is None:
            return False
        try:
            if page.is_closed():
                return False
        except Exception:  # noqa: BLE001
            return False
        try:
            body_text = page.locator("body").inner_text(timeout=2500)
        except Exception:  # noqa: BLE001
            body_text = ""
        compact = re.sub(r"\s+", " ", str(body_text or "")).strip()
        if "승인되지 않은 접근입니다" in compact:
            return True
        return "승인되지 않은 접근" in compact and "뒤로가기" in compact

    def _login_with_saved_credentials(
        self,
        page: Page,
        dialog_messages: Optional[list[str]] = None,
        *,
        log_prefix: str = "",
    ) -> LoginResult:
        prefix = f"{log_prefix}: " if log_prefix else ""
        page.goto(self.settings.login_url, wait_until="commit")
        self._log(f"{prefix}로그인 페이지 이동: {self.settings.login_url}")
        if not self._wait_login_form_ready(page):
            return LoginResult(False, f"{prefix}로그인 폼 로딩 타임아웃", page.url)

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
            return LoginResult(False, f"{prefix}로그인 입력창을 찾지 못했습니다.", page.url)
        if not submitted:
            now_url = page.url
            if (
                "login/process.do" in now_url
                or "param=success" in now_url
                or "loginpage.do" not in now_url
            ):
                self._log(f"{prefix}로그인 버튼 감지는 실패했지만 로그인 처리 URL 변화를 감지했습니다.")
            else:
                return LoginResult(False, f"{prefix}로그인 버튼을 찾지 못했습니다.", page.url)

        return self._wait_login_result(page, dialog_messages or [])

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    def _is_stop_requested(self) -> bool:
        try:
            return bool(self.stop_requested and self.stop_requested())
        except Exception:  # noqa: BLE001
            return False

    def _raise_if_stop_requested(self) -> None:
        if self._is_stop_requested():
            raise self.StopRequested("사용자 중단 요청으로 작업을 중지합니다.")

    def _wait_page_with_stop(self, page: Page, wait_ms: int, chunk_ms: int = 500) -> None:
        remaining = max(0, int(wait_ms))
        step = max(100, int(chunk_ms))
        while remaining > 0:
            self._raise_if_stop_requested()
            current = min(step, remaining)
            page.wait_for_timeout(current)
            remaining -= current

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _browser_launch_options(self) -> dict[str, Any]:
        launch_options: dict[str, Any] = {"headless": self.settings.headless}
        proxy_server = str(getattr(self.settings, "proxy_server", "") or "").strip()
        if proxy_server:
            proxy_payload: dict[str, str] = {"server": proxy_server}
            proxy_username = str(getattr(self.settings, "proxy_username", "") or "").strip()
            proxy_password = str(getattr(self.settings, "proxy_password", "") or "").strip()
            if proxy_username:
                proxy_payload["username"] = proxy_username
            if proxy_password:
                proxy_payload["password"] = proxy_password
            launch_options["proxy"] = proxy_payload
        return launch_options

    def _proxy_target_country(self) -> str:
        return str(getattr(self.settings, "proxy_country", "KR") or "KR").strip().upper()

    def _sanitize_proxy_server(self) -> str:
        raw = str(getattr(self.settings, "proxy_server", "") or "").strip()
        if not raw:
            return ""
        return re.sub(r"//([^:@/]+):([^@/]+)@", r"//***:***@", raw)

    def _write_proxy_preflight_record(self, payload: dict[str, Any]) -> None:
        self._last_proxy_preflight = dict(payload)
        try:
            self._proxy_preflight_path.parent.mkdir(parents=True, exist_ok=True)
            self._proxy_preflight_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            return

    def _probe_egress_with_browser(self, page: Page) -> dict[str, Any]:
        providers = [
            ("ipapi", "https://ipapi.co/json/"),
            ("ipwhois", "https://ipwho.is/"),
            ("ifconfig", "https://ifconfig.co/json"),
        ]
        for provider_name, url in providers:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=min(self.settings.timeout_ms, 15000))
                page.wait_for_timeout(400)
                try:
                    body = page.locator("body").inner_text(timeout=3000).strip()
                except Exception:  # noqa: BLE001
                    body = ""
                if not body:
                    continue
                data = json.loads(body)
                if not isinstance(data, dict):
                    continue
                ip = str(
                    data.get("ip")
                    or data.get("ip_address")
                    or data.get("query")
                    or data.get("ip_addr")
                    or ""
                ).strip()
                country = str(
                    data.get("country_code")
                    or data.get("countryCode")
                    or data.get("country")
                    or data.get("country_code_iso3")
                    or ""
                ).strip()
                if len(country) > 2 and re.fullmatch(r"[A-Za-z]{3}", country):
                    country = country[:2]
                if not country and isinstance(data.get("country"), str):
                    country_name = str(data.get("country") or "").strip().lower()
                    if country_name in {"south korea", "korea, republic of", "republic of korea"}:
                        country = "KR"
                country = country.upper()
                if ip or country:
                    return {
                        "provider": provider_name,
                        "url": url,
                        "ip": ip,
                        "country": country,
                        "raw": data,
                    }
            except Exception:  # noqa: BLE001
                continue
        return {}

    def _ensure_proxy_preflight(self, page: Page) -> Optional[LoginResult]:
        required = bool(getattr(self.settings, "proxy_required", False))
        target_country = self._proxy_target_country()
        proxy_server = self._sanitize_proxy_server()

        payload: dict[str, Any] = {
            "checked_at": self._utc_now_iso(),
            "status": "unknown",
            "message": "",
            "proxy": {
                "server": proxy_server,
                "required": required,
                "target_country": target_country,
            },
            "egress": {},
        }

        if not proxy_server and required:
            payload["status"] = "failed"
            payload["message"] = "proxy required but no proxy server configured"
            self._write_proxy_preflight_record(payload)
            self._log("proxy-preflight-failed: required proxy missing")
            return LoginResult(False, "한국 egress 프록시가 필수인데 설정되지 않았습니다.", page.url)

        egress = self._probe_egress_with_browser(page)
        if egress:
            payload["egress"] = {
                "provider": str(egress.get("provider", "") or ""),
                "ip": str(egress.get("ip", "") or ""),
                "country": str(egress.get("country", "") or ""),
            }
            detected_country = str(egress.get("country", "") or "").upper()
            if target_country and detected_country == target_country:
                payload["status"] = "ok"
                payload["message"] = f"egress country matched {target_country}"
                self._log(
                    "proxy-preflight-ok: "
                    f"provider={egress.get('provider', '')} ip={egress.get('ip', '')} country={detected_country}"
                )
            else:
                payload["status"] = "mismatch"
                payload["message"] = (
                    f"egress country mismatch target={target_country} detected={detected_country or '-'}"
                )
                self._log(
                    "proxy-preflight-mismatch: "
                    f"provider={egress.get('provider', '')} ip={egress.get('ip', '')} "
                    f"country={detected_country or '-'} target={target_country}"
                )
                self._write_proxy_preflight_record(payload)
                if required:
                    return LoginResult(
                        False,
                        (
                            "한국 egress 프리플라이트 불일치: "
                            f"target={target_country}, detected={detected_country or '-'}"
                        ),
                        page.url,
                    )
        else:
            payload["status"] = "unknown"
            payload["message"] = "egress detection failed"
            self._log("proxy-preflight-unknown: egress detection failed")
            self._write_proxy_preflight_record(payload)
            if required:
                return LoginResult(False, "한국 egress 프리플라이트 확인에 실패했습니다.", page.url)

        self._write_proxy_preflight_record(payload)
        return None

    def _open_browser_session(self, playwright: Any, dialog_messages: list[str]) -> tuple[Any, Any, Page]:
        browser = playwright.chromium.launch(**self._browser_launch_options())
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(self.settings.timeout_ms)
        page.on("dialog", lambda dialog: self._handle_dialog(dialog, dialog_messages))
        return browser, context, page

    @staticmethod
    def _build_run_id() -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        token = hashlib.sha1(str(time.time_ns()).encode("utf-8")).hexdigest()[:6]
        return f"{stamp}_{token}"

    def _note_artifact(
        self,
        path: Path | str,
        *,
        kind: str,
        label: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        raw_path = str(path).strip()
        if not raw_path:
            return
        norm_path = raw_path.replace("\\", "/")
        record: dict[str, Any] = {
            "path": norm_path,
            "kind": str(kind or "").strip() or "artifact",
            "label": str(label or "").strip(),
        }
        if metadata:
            record["metadata"] = {
                str(k): v for k, v in metadata.items() if str(k).strip()
            }
        dedupe_key = (
            record["path"],
            record["kind"],
            record["label"],
        )
        for existing in self._artifact_records:
            existing_key = (
                str(existing.get("path", "")),
                str(existing.get("kind", "")),
                str(existing.get("label", "")),
            )
            if existing_key == dedupe_key:
                return
        self._artifact_records.append(record)

    def get_runtime_diagnostics(self) -> dict[str, Any]:
        exam_payload = self._last_exam_solve_payload if isinstance(self._last_exam_solve_payload, dict) else {}
        exam_summary = {}
        if exam_payload:
            exam_summary = {
                "success": bool(exam_payload.get("success", False)),
                "message": str(exam_payload.get("message", "")),
                "solved": int(exam_payload.get("solved", 0) or 0),
                "skipped": int(exam_payload.get("skipped", 0) or 0),
                "low_conf_used": int(exam_payload.get("low_conf_used", 0) or 0),
            }
        return {
            "run_id": self._run_id,
            "artifact_paths": list(self._artifact_records),
            "last_course_title": str(self._last_opened_course_title or "").strip(),
            "last_lesson_key": str(self._last_opened_lesson_key or "").strip(),
            "last_lesson_title": str(self._last_opened_lesson_title or "").strip(),
            "last_course_progress_percent": int(self._last_observed_course_progress_percent),
            "last_exam_summary": exam_summary,
            "proxy_preflight": dict(self._last_proxy_preflight),
        }

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

    @staticmethod
    def _question_has_numeric_signal(question: str, options: list[str]) -> bool:
        joined = f"{question} {' '.join(options[:5])}"
        return bool(re.search(r"\d", joined))

    def _build_deferred_account_scope(self) -> str:
        user_id = re.sub(r"\s+", "", str(getattr(self.settings, "user_id", "") or "")).lower()
        base_url = str(getattr(self.settings, "base_url", "") or "").strip().lower()
        login_url = str(getattr(self.settings, "login_url", "") or "").strip().lower()
        seed = f"{user_id}||{base_url}||{login_url}"
        if not user_id:
            return "anonymous"
        return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]

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
                "account_scope": self._deferred_account_scope,
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

    def _maybe_skip_course_on_large_time_shortage(
        self,
        classroom_page: Page,
        *,
        time_status: dict[str, int | bool],
        check_limit: int,
        default_interval_minutes: int,
    ) -> Optional[LoginResult]:
        limit = max(1, int(check_limit))
        if limit > 2:
            return None
        if not isinstance(time_status, dict) or not bool(time_status.get("requirement_known", False)):
            return None

        try:
            shortage = max(0, int(time_status.get("shortage_seconds", 0)))
            required = max(0, int(time_status.get("required_seconds", 0)))
        except Exception:  # noqa: BLE001
            return None
        if shortage <= 0:
            return None

        ratio = (float(shortage) / float(required)) if required > 0 else 0.0
        if shortage < 30 * 60 and ratio < 0.30:
            return None

        wait_minutes = self._decide_timefill_check_interval_minutes(
            time_status=time_status,
            default_minutes=int(default_interval_minutes),
        )
        title = str(self._last_opened_course_title or "").strip() or "현재 과정"
        reason = (
            f"학습시간 부족 {self._format_seconds(shortage)} "
            f"(required={self._format_seconds(required)}, check_limit={limit}, next_check={wait_minutes}분)로 "
            "현재 스모크 런에서는 다음 강좌로 우회합니다."
        )
        self._mark_current_course_exam_deferred(reason)
        return LoginResult(True, f"과정 우회: {title} / {reason}", classroom_page.url)

    def login(self) -> LoginResult:
        if not self.settings.user_id or not self.settings.user_password:
            return LoginResult(False, "환경변수에 EKHNP_USER_ID / EKHNP_USER_PASSWORD를 설정하세요.")

        self._log("브라우저를 시작합니다.")
        with sync_playwright() as p:
            dialog_messages: list[str] = []
            browser, context, page = self._open_browser_session(p, dialog_messages)

            try:
                preflight = self._ensure_proxy_preflight(page)
                if preflight is not None:
                    return preflight
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
            dialog_messages: list[str] = []
            browser, context, page = self._open_browser_session(p, dialog_messages)

            try:
                preflight = self._ensure_proxy_preflight(page)
                if preflight is not None:
                    return preflight
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
            dialog_messages: list[str] = []
            browser, context, page = self._open_browser_session(p, dialog_messages)

            try:
                preflight = self._ensure_proxy_preflight(page)
                if preflight is not None:
                    return preflight
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
            dialog_messages: list[str] = []
            browser, context, page = self._open_browser_session(p, dialog_messages)

            try:
                preflight = self._ensure_proxy_preflight(page)
                if preflight is not None:
                    return preflight
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
            dialog_messages: list[str] = []
            browser, context, page = self._open_browser_session(p, dialog_messages)

            try:
                preflight = self._ensure_proxy_preflight(page)
                if preflight is not None:
                    return preflight
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
            dialog_messages: list[str] = []
            browser, context, page = self._open_browser_session(p, dialog_messages)

            try:
                preflight = self._ensure_proxy_preflight(page)
                if preflight is not None:
                    return preflight
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
                    quality = self._inspect_exam_quality_report(report)
                    if bool(quality.get("complete_alignment")):
                        self._log(f"시험 파싱 품질 확인: {quality.get('message', '')}")
                    else:
                        self._log(f"시험 파싱 품질 경고: {quality.get('message', '')}")
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
                    if retry_requires_answer_index and not bool(quality.get("complete_alignment")):
                        return LoginResult(
                            False,
                            (
                                "종합평가 점수 미달이며 시험 결과 파싱이 불완전해 자동 재응시를 중단합니다. "
                                f"({quality.get('message', '')})"
                            ),
                            classroom_page.url,
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
            self._raise_if_stop_requested()
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
            True,
            f"과정 처리 제한({max_courses})까지 완료: 처리 {handled_courses}개 (수료 {completed_courses}, 우회 {skipped_courses})",
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
            dialog_messages: list[str] = []
            browser, context, page = self._open_browser_session(p, dialog_messages)

            try:
                preflight = self._ensure_proxy_preflight(page)
                if preflight is not None:
                    return preflight
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
                classroom_page = self._refresh_classroom_page(classroom_page)
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
                    if bool(initial_progress.get("access_denied", False)):
                        recovered_classroom = self._relogin_and_reopen_course_classroom(
                            classroom_page,
                            preferred_title=self._last_opened_course_title,
                        )
                        if recovered_classroom is not None:
                            classroom_page = recovered_classroom
                            initial_progress = self._extract_learning_progress_status(classroom_page)
                            incomplete_count = int(initial_progress.get("incomplete_count", 0))
                    if incomplete_count > 0:
                        self._log("미완료 일반 차시를 직접 찾아 우선 진입합니다.")
                    else:
                        self._log("학습 차시 목록을 다시 스캔해 미수료 일반 차시를 우선 탐색합니다.")
                    learning_page = self._start_learning_from_progress_panel(classroom_page)
                    if learning_page is None:
                        if self._safe_refresh_non_exam_page(classroom_page, reason="진도율 단계 학습창 미오픈"):
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
                            self._raise_if_stop_requested()
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
                            self._wait_page_with_stop(learning_page, 2200)

                classroom_page = self._refresh_classroom_page(classroom_page)
                progress_status = self._extract_learning_progress_status(classroom_page)
                if (
                    not stage1_bypass_for_timefill
                    and (not progress_status["progress_ok"] or progress_status["incomplete_count"] > 0)
                ):
                    self._log("학습진도율 기준 미충족 또는 미완료 차시 감지: 보완 학습을 시도합니다.")
                    for retry in range(min(safety_max_lessons, 40)):
                        self._raise_if_stop_requested()
                        if progress_status["progress_ok"] and progress_status["incomplete_count"] <= 0:
                            break
                        self._log(f"미완료 차시 보완 시도 {retry + 1}")
                        if progress_status["incomplete_count"] > 0:
                            self._log("보완 대상 미완료 일반 차시를 직접 선택합니다.")
                        else:
                            self._log("미완료 일반 차시 재탐색 후 없으면 '이어 학습하기'로 보완합니다.")
                        extra_page = self._start_learning_from_progress_panel(classroom_page)
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
                        classroom_page = self._refresh_classroom_page(classroom_page)
                        progress_status = self._extract_learning_progress_status(classroom_page)

                    if not progress_status["progress_ok"] or progress_status["incomplete_count"] > 0:
                        return LoginResult(
                            False,
                            "학습진도율 수료기준 미충족 또는 미완료 차시가 남아 있어 중단합니다.",
                            classroom_page.url,
                        )

                # 2) 잔여 학습시간 보충
                self._log("2단계: 잔여 학습시간을 수료기준까지 보충합니다.")
                classroom_page = self._refresh_classroom_page(classroom_page)
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
                    skip_course = self._maybe_skip_course_on_large_time_shortage(
                        classroom_page=classroom_page,
                        time_status=time_status,
                        check_limit=check_limit,
                        default_interval_minutes=base_timefill_interval_minutes,
                    )
                    if skip_course is not None:
                        return skip_course
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
                        classroom_page = self._refresh_classroom_page(classroom_page)
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
                            classroom_page = self._refresh_classroom_page(classroom_page)
                            exam_gate = self._extract_learning_progress_status(classroom_page)
                            exam_gate_percent = int(exam_gate.get("current_percent", 0))
                            if exam_gate_percent >= 80 and exam_gate["incomplete_count"] <= 0:
                                break

                            self._log(f"응시 조건 보완(미완료 차시) {retry + 1}")
                            if exam_gate["incomplete_count"] > 0:
                                self._log("응시 조건 보완용 미완료 일반 차시를 직접 선택합니다.")
                            else:
                                self._log("미완료 일반 차시 재탐색 후 없으면 '이어 학습하기'로 응시 조건을 보완합니다.")
                            extra_page = self._start_learning_from_progress_panel(classroom_page)
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

                        classroom_page = self._refresh_classroom_page(classroom_page)
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
                        classroom_page = self._refresh_classroom_page(classroom_page)
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
                        quality = self._inspect_exam_quality_report(report)
                        if bool(quality.get("complete_alignment")):
                            self._log(f"시험 파싱 품질 확인: {quality.get('message', '')}")
                        else:
                            self._log(f"시험 파싱 품질 경고: {quality.get('message', '')}")
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

                        if retry_requires_answer_index and not bool(quality.get("complete_alignment")):
                            return LoginResult(
                                False,
                                f"{completion_guard.message} / 시험 결과 파싱이 불완전해 자동 재응시를 중단합니다. ({quality.get('message', '')})",
                                classroom_page.url,
                            )
                        if retry_requires_answer_index and added <= 0:
                            return LoginResult(
                                False,
                                f"{completion_guard.message} / 정답지 인덱싱 데이터가 없어 자동 재응시를 중단합니다.",
                                classroom_page.url,
                            )

                        retry_round += 1
                        self._log(f"종합평가 자동 재응시 시작: {retry_round}/{max_exam_retries}")
                        exam_page = self._open_comprehensive_exam_popup(classroom_page)
                        if exam_page is None:
                            return LoginResult(False, "정답지 학습 후 종합평가 재응시 팝업을 찾지 못했습니다.", classroom_page.url)

                # 4) 최종 수료 상태 확인
                classroom_page = self._refresh_classroom_page(classroom_page)
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
            dialog_messages: list[str] = []
            browser, context, page = self._open_browser_session(p, dialog_messages)

            try:
                preflight = self._ensure_proxy_preflight(page)
                if preflight is not None:
                    return preflight
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
        direct_url = self._learning_status_url(page)
        process_timeout_ms = min(max(self.settings.timeout_ms, 30000), 90000)
        verify_timeout_ms = min(max(self.settings.timeout_ms, 15000), 45000)

        try:
            if "/login/process.do" in page.url:
                self._log("로그인 처리 페이지(process.do) 감지: 메인 전환 대기")
                for _ in range(max(1, process_timeout_ms // 500)):
                    self._raise_if_stop_requested()
                    if "/login/process.do" not in page.url:
                        break
                    self._wait_page_with_stop(page, 500)
                if "/login/process.do" in page.url:
                    self._log("자동 전환 지연: '나의 학습현황' 주소로 직접 이동")
                    page.goto(direct_url, wait_until="domcontentloaded", timeout=process_timeout_ms)
                    self._wait_page_with_stop(page, 1200)

            try:
                page.wait_for_load_state("networkidle", timeout=min(verify_timeout_ms, 15000))
            except PlaywrightTimeoutError:
                pass
            self._wait_page_with_stop(page, 1200)

            if self._wait_for_learning_status_page(page, wait_ms=verify_timeout_ms):
                return LoginResult(True, "나의 학습현황 페이지 이동 성공(직접 확인)", page.url)

            self._log("상단 메뉴 'My학습포털' hover 시도")
            top_menu_candidates = [
                'a:has-text("My학습포털")',
                'li:has-text("My학습포털")',
                'span:has-text("My학습포털")',
            ]
            top_menu_hovered = self._hover_first_visible(page, top_menu_candidates, max_items=20)
            if not top_menu_hovered:
                self._log("'My학습포털' hover는 건너뛰고 링크 직접 클릭을 시도합니다.")

            self._wait_page_with_stop(page, 400)
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
                    timeout=min(verify_timeout_ms, 12000),
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
                page.goto(direct_url, wait_until="domcontentloaded", timeout=process_timeout_ms)
                self._wait_page_with_stop(page, 1200)
            else:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=min(verify_timeout_ms, 20000))
                except PlaywrightTimeoutError:
                    self._log("'나의 학습현황' 클릭 후 domcontentloaded 지연: 상태 텍스트로 재확인합니다.")
                self._wait_page_with_stop(page, 1200)

            if self._wait_for_learning_status_page(page, wait_ms=verify_timeout_ms):
                return LoginResult(True, "나의 학습현황 페이지 이동 성공", page.url)

            self._log("나의 학습현황 이동 재확인 실패: 직접 URL 재시도")
            try:
                page.goto(direct_url, wait_until="domcontentloaded", timeout=process_timeout_ms)
            except PlaywrightTimeoutError:
                self._log("나의 학습현황 직접 URL 이동이 지연되어 텍스트 확인으로 전환합니다.")
            self._wait_page_with_stop(page, 1200)
            if self._wait_for_learning_status_page(page, wait_ms=verify_timeout_ms):
                return LoginResult(True, "나의 학습현황 페이지 직접 이동 성공", page.url)
            return LoginResult(False, "나의 학습현황 클릭 후 이동 확인 실패", page.url)
        except PlaywrightTimeoutError:
            self._log("나의 학습현황 이동 중 타임아웃: 직접 URL 이동으로 마지막 재시도합니다.")
            try:
                page.goto(direct_url, wait_until="domcontentloaded", timeout=process_timeout_ms)
                self._wait_page_with_stop(page, 1200)
                if self._wait_for_learning_status_page(page, wait_ms=verify_timeout_ms):
                    return LoginResult(True, "나의 학습현황 페이지 직접 이동 성공(타임아웃 복구)", page.url)
            except Exception:  # noqa: BLE001
                pass
            return LoginResult(False, "로그인 후 '나의 학습현황' 이동 타임아웃", page.url)

    def _learning_status_url(self, page: Page) -> str:
        origin_match = re.match(r"^https?://[^/]+", str(getattr(page, "url", "") or ""))
        origin = origin_match.group(0) if origin_match else self.settings.base_url.rstrip("/")
        return f"{origin}/usr/member/dash/detail.do"

    def _is_learning_status_page(self, page: Page) -> bool:
        try:
            if "/usr/member/dash/detail.do" in str(page.url).lower():
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            body_text = page.locator("body").inner_text(timeout=1800)
        except Exception:  # noqa: BLE001
            body_text = ""
        return "나의 학습현황" in body_text or "My Learning" in body_text

    def _wait_for_learning_status_page(self, page: Page, wait_ms: int = 30000) -> bool:
        ticks = max(1, wait_ms // 500)
        for _ in range(ticks):
            self._raise_if_stop_requested()
            if self._is_learning_status_page(page):
                return True
            self._wait_page_with_stop(page, 500)
        return self._is_learning_status_page(page)

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

    def _find_startable_course_by_title(self, page: Page, preferred_title: str) -> tuple[str, Optional[Any]]:
        target_key = self._course_title_key(preferred_title)
        if not target_key:
            return "", None
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
            if self._course_title_key(title) == target_key:
                return title or str(preferred_title or "").strip(), button
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
        progress = self._extract_learning_progress_status(classroom_page)
        current_percent = int(progress.get("current_percent", 0) or 0)
        required_percent = int(progress.get("required_percent", 0) or 0)
        incomplete_count = int(progress.get("incomplete_count", 0) or 0)
        if current_percent >= required_percent and incomplete_count <= 0:
            exam_req = self._extract_exam_requirement_status(classroom_page)
            if bool(exam_req.get("has_exam", True)):
                return (
                    LoginResult(
                        True,
                        (
                            "강의실 진입 성공(학습 차시 완료 상태): "
                            f"{classroom_result.message.replace('강의실 진입 성공: ', '')} / "
                            f"학습진도율 {current_percent}% / 수료기준 {required_percent}%"
                        ),
                        classroom_page.url,
                    ),
                    classroom_page,
                    None,
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

    def _open_first_course_classroom_internal(
        self,
        page: Page,
        preferred_title: str = "",
    ) -> tuple[LoginResult, Optional[Page]]:
        self._log("수강과정 목록 로딩 대기")
        try:
            page.wait_for_selector("table tbody tr", timeout=min(self.settings.timeout_ms, 15000))
        except PlaywrightTimeoutError:
            return LoginResult(False, "수강과정 테이블을 찾지 못했습니다.", page.url), None

        first_title = ""
        first_row_button = None
        preferred_title = str(preferred_title or "").strip()
        if preferred_title:
            first_title, first_row_button = self._find_startable_course_by_title(page, preferred_title)
            if first_row_button is None:
                self._log(
                    f"지정 과정 재진입 실패: {preferred_title} / 첫 수강 가능 과정을 대신 탐색합니다."
                )
        if first_row_button is None:
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

    def _start_learning_from_progress_panel(
        self,
        page: Page,
        prefer_incomplete: bool = True,
        preferred_lesson_key: str = "",
    ) -> Optional[Page]:
        if self._is_classroom_access_denied(page):
            recovered = self._relogin_and_reopen_course_classroom(
                page,
                preferred_title=self._last_opened_course_title,
            )
            if recovered is not None:
                page = recovered
        self._log("강의실 하단 '학습진행현황'의 학습하기 버튼 클릭 시도")
        page.wait_for_timeout(1200)
        self._prime_lesson_list_dom(page)
        lesson_rows = self._extract_classroom_lesson_rows(page)
        self._detected_total_lessons = len(lesson_rows) or self._extract_total_lessons_from_classroom_buttons(page)
        if self._detected_total_lessons is None:
            # 지연 렌더링/가상 스크롤 대응: 한번 더 강제 로드 후 재탐색
            self._prime_lesson_list_dom(page)
            lesson_rows = self._extract_classroom_lesson_rows(page)
            self._detected_total_lessons = len(lesson_rows) or self._extract_total_lessons_from_classroom_buttons(page)
        if self._detected_total_lessons:
            self._log(
                f"강의실 학습하기 버튼 개수 감지: 총 {self._detected_total_lessons}차시"
            )
        lesson_popup = self._open_next_unfinished_lesson_popup(
            page,
            rows=lesson_rows,
            preferred_key=preferred_lesson_key if prefer_incomplete else "",
        )
        if lesson_popup is not None:
            return lesson_popup

        try:
            page.locator('text=학습진행현황').first.scroll_into_view_if_needed(timeout=1500)
        except Exception:  # noqa: BLE001
            pass

        resume_popup = self._open_resume_learning_popup(page)
        if resume_popup is not None:
            return resume_popup
        self._dump_player_debug(page, "start_button_not_found")
        return None

    def _relogin_and_reopen_course_classroom(
        self,
        page: Page,
        *,
        preferred_title: str = "",
    ) -> Optional[Page]:
        self._log("강의실 접근 거부 감지: 로그인부터 다시 진행합니다.")
        dialog_messages: list[str] = []
        login_result = self._login_with_saved_credentials(
            page,
            dialog_messages,
            log_prefix="강의실 재진입",
        )
        if not login_result.success:
            self._log(f"강의실 재로그인 실패: {login_result.message}")
            return None
        status_result = self._open_learning_status(page)
        if not status_result.success:
            self._log(f"강의실 재진입 실패(학습현황): {status_result.message}")
            return None
        classroom_result, reopened_page = self._open_first_course_classroom_internal(
            page,
            preferred_title=preferred_title or self._last_opened_course_title,
        )
        if not classroom_result.success or reopened_page is None:
            self._log(f"강의실 재진입 실패(과정): {classroom_result.message}")
            return None
        self._log(
            "강의실 재진입 성공: "
            f"{str(preferred_title or self._last_opened_course_title or '').strip() or '과정 재탐색'}"
        )
        return reopened_page

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
                self._log(f"comprehensive-exam-opened: popup {popup_page.url}")
                return popup_page

            # 사이트별 구현에서 "응시하기" 클릭 후 사전 안내 레이어(동의+시험 시작하기)가 1단계 더 필요할 수 있음.
            for _ in range(20):
                started_page = self._start_exam_from_notice_layer(page, before_pages, before_url)
                if started_page is not None:
                    return started_page

                now_url = page.url.lower()
                if now_url != before_url.lower() and any(
                    hint in now_url for hint in ["exam", "test", "evaluation", "eval", "exampaper"]
                ):
                    self._log(f"comprehensive-exam-opened: direct {page.url}")
                    return page

                picked = self._pick_exam_page(page.context.pages, before_pages)
                if picked is not None and picked != page:
                    self._log(f"comprehensive-exam-opened: selected {picked.url}")
                    return picked
                page.wait_for_timeout(500)

            now_url = page.url.lower()
            if now_url != before_url.lower() and any(
                hint in now_url for hint in ["exam", "test", "evaluation", "eval", "exampaper"]
            ):
                self._log(f"comprehensive-exam-opened: direct {page.url}")
                return page

            picked = self._pick_exam_page(page.context.pages, before_pages)
            if picked is not None and picked != page:
                self._log(f"comprehensive-exam-opened: selected {picked.url}")
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
        if now_url != before_url.lower() and any(
            h in now_url for h in ["exam", "test", "evaluation", "eval", "exampaper"]
        ):
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
            snap = self._extract_exam_question_snapshot(page, allow_ocr=False, prefer_structured=True)
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

    @staticmethod
    def _is_protected_evidence_id(evidence_id: str) -> bool:
        src = str(evidence_id or "").strip().lower()
        if not src:
            return False
        protected_tokens = (
            "law.go.kr",
            "국가법령정보센터",
            "법령",
            "시행령",
            "시행규칙",
            "조문",
        )
        return any(tok in src for tok in protected_tokens)

    def _negative_evidence_decay_half_life_sec(self) -> float:
        return max(300.0, float(getattr(self.settings, "rag_negative_evidence_decay_sec", 7200) or 7200))

    @staticmethod
    def _decode_negative_evidence_entry(entry: Any) -> tuple[float, float]:
        if isinstance(entry, dict):
            try:
                score = float(entry.get("score", 0.0) or 0.0)
            except Exception:  # noqa: BLE001
                score = 0.0
            try:
                updated_at = float(entry.get("updated_at", 0.0) or 0.0)
            except Exception:  # noqa: BLE001
                updated_at = 0.0
            return max(0.0, score), max(0.0, updated_at)
        try:
            score = float(entry or 0.0)
        except Exception:  # noqa: BLE001
            score = 0.0
        return max(0.0, score), 0.0

    def _decay_negative_evidence_score(self, score: float, updated_at: float, now_ts: float) -> float:
        s = max(0.0, float(score))
        ts = max(0.0, float(updated_at))
        if s <= 0.0:
            return 0.0
        if ts <= 0.0 or now_ts <= ts:
            return s
        delta = max(0.0, now_ts - ts)
        half_life = self._negative_evidence_decay_half_life_sec()
        # 지수 감쇠: half-life 경과 시 점수를 절반으로 감소.
        return s * (0.5 ** (delta / half_life))

    def _negative_evidence_penalties_for_question(self, question: str, min_streak: int = 2) -> dict[str, float]:
        q_sig = self._question_signature(question)
        if not q_sig:
            return {}
        slot = self._question_evidence_fail_streak.get(q_sig, {})
        if not isinstance(slot, dict):
            return {}
        now_ts = time.time()
        max_score = max(1.0, float(getattr(self.settings, "rag_negative_evidence_max_score", 6.0) or 6.0))
        base_penalty = max(
            0.0,
            min(0.95, float(getattr(self.settings, "rag_negative_evidence_base_penalty", 0.18) or 0.18)),
        )
        step_penalty = max(
            0.0,
            min(0.95, float(getattr(self.settings, "rag_negative_evidence_step_penalty", 0.12) or 0.12)),
        )
        max_penalty = max(
            base_penalty,
            min(0.95, float(getattr(self.settings, "rag_negative_evidence_max_penalty", 0.75) or 0.75)),
        )
        penalties: dict[str, float] = {}
        for eid, raw_entry in list(slot.items()):
            key = str(eid).strip()
            if not key:
                continue
            score_raw, updated_at = self._decode_negative_evidence_entry(raw_entry)
            score_decayed = self._decay_negative_evidence_score(score_raw, updated_at, now_ts)
            score_decayed = min(max_score, max(0.0, score_decayed))
            slot[key] = {"score": round(score_decayed, 4), "updated_at": now_ts}
            if score_decayed < float(min_streak):
                continue
            if key == "answer-bank":
                penalties[key] = 1.0
                continue
            if self._is_protected_evidence_id(key):
                continue
            # 반복 오답 근거는 soft penalty로 가중(차단 대신 순위 하향).
            penalties[key] = min(max_penalty, base_penalty + step_penalty * max(0.0, score_decayed - float(min_streak)))
        return penalties

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
            now_ts = time.time()
            max_score = max(1.0, float(getattr(self.settings, "rag_negative_evidence_max_score", 6.0) or 6.0))
            for eid in evidence_ids[:3]:
                prev_score, prev_updated_at = self._decode_negative_evidence_entry(slot.get(eid, 0.0))
                prev_score = self._decay_negative_evidence_score(prev_score, prev_updated_at, now_ts)
                if is_correct:
                    slot[eid] = {"score": 0.0, "updated_at": now_ts}
                else:
                    try:
                        conf = float(rec.get("confidence", 0.0) or 0.0)
                    except Exception:  # noqa: BLE001
                        conf = 0.0
                    # 고신뢰 오답은 다음 회차에서 동일 근거를 더 강하게 감점합니다.
                    penalty = 2 if conf >= 0.80 else 1
                    next_score = min(max_score, max(0.0, prev_score) + float(penalty))
                    slot[eid] = {"score": round(next_score, 4), "updated_at": now_ts}

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
        model_escalate_margin = max(
            0.0,
            min(0.35, float(getattr(self.settings, "rag_conf_escalate_margin", 0.08))),
        )
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
            f"model_escalate_margin={model_escalate_margin:.2f}, "
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
            snap = self._extract_exam_question_snapshot(
                exam_page,
                allow_ocr=False,
                prefer_structured=True,
            )
            if snap is None:
                snap = self._extract_exam_question_snapshot(
                    exam_page,
                    force_ocr=True,
                    allow_ocr=True,
                    prefer_structured=True,
                )
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
                for retry in range(2):
                    if retry > 0:
                        exam_page.wait_for_timeout(1200)
                    snap_retry = self._extract_exam_question_snapshot(
                        exam_page,
                        allow_ocr=False,
                        prefer_structured=True,
                    )
                    if snap_retry:
                        retry_options = [str(x).strip() for x in snap_retry.get("options", []) if str(x).strip()]
                        if len(retry_options) >= 2:
                            options = retry_options
                            question = str(snap_retry.get("question_text", "")).strip() or str(
                                snap_retry.get("full_text", "")
                            ).strip()
                            source = str(snap_retry.get("source", source))
                    if len(options) >= 2:
                        self._log(
                            "보기 추출 폴백 성공: "
                            f"retry={retry + 1}, stage=structured, source={source}, option_count={len(options)}"
                        )
                        break
                if len(options) < 2:
                    for retry in range(2):
                        if retry > 0:
                            exam_page.wait_for_timeout(1200)
                        snap_retry = self._extract_exam_question_snapshot(
                            exam_page,
                            force_ocr=True,
                            allow_ocr=True,
                            prefer_structured=True,
                        )
                        if snap_retry:
                            retry_options = [str(x).strip() for x in snap_retry.get("options", []) if str(x).strip()]
                            if len(retry_options) >= 2:
                                options = retry_options
                                question = str(snap_retry.get("question_text", "")).strip() or str(
                                    snap_retry.get("full_text", "")
                                ).strip()
                                source = str(snap_retry.get("source", source))
                        if len(options) >= 2:
                            self._log(
                                "보기 추출 폴백 성공: "
                                f"retry={retry + 1}, stage=ocr, source={source}, option_count={len(options)}"
                            )
                            break
                if len(options) >= 2 and "structured" in source:
                    self._log(
                        f"보기 추출 1순위 적용: structured source={source}, option_count={len(options)}"
                    )
                if len(options) >= 2 and "structured" not in source and source.startswith("dom"):
                    self._log(
                        f"보기 추출 2순위 적용: dom source={source}, option_count={len(options)}"
                    )
                if len(options) >= 2 and "ocr" in source:
                    self._log(
                        f"보기 추출 3순위 적용: ocr source={source}, option_count={len(options)}"
                    )
                if len(options) < 2:
                    self._dump_exam_dom_debug(exam_page, f"options_failed_q{current}_{total or total_hint or 0}")
                    return _payload(False, f"보기 추출 실패(current/total={current}/{total}, source={source})")

            numeric_question = self._question_has_numeric_signal(question, options)
            strict_numeric_primary = numeric_question
            negative_evidence_penalties = self._negative_evidence_penalties_for_question(question)
            hard_excluded_evidence_ids = {
                str(eid).strip()
                for eid, penalty in negative_evidence_penalties.items()
                if str(eid).strip() and float(penalty) >= 0.95
            }
            if negative_evidence_penalties:
                sampled = sorted(
                    (
                        f"{eid}:{float(negative_evidence_penalties[eid]):.2f}"
                        for eid in negative_evidence_penalties.keys()
                    )
                )[:3]
                self._log(
                    "Negative Evidence 감점 적용: "
                    f"Q {current}/{total} penalties={sampled}"
                )
            cached_answer = self._lookup_answer_bank_choice(
                question=question,
                options=options,
                exam_meta=exam_runtime_meta,
                course_title=self._last_opened_course_title,
                question_no=current,
            )
            used_answer_bank = False
            if cached_answer is not None and "answer-bank" not in hard_excluded_evidence_ids:
                choice = int(cached_answer.get("choice", 0))
                confidence = float(cached_answer.get("confidence", 0.98))
                reason = str(cached_answer.get("reason", "answer-bank"))
                evidence_ids: list[str] = ["answer-bank"]
                used_answer_bank = True
                self._log(f"정답 인덱스 매칭 사용: Q {current}/{total} -> choice={choice}, conf={confidence:.2f}")
            else:
                if cached_answer is not None and "answer-bank" in hard_excluded_evidence_ids:
                    self._log(f"Q {current}/{total} answer-bank 연속 실패 감지로 2순위 근거 전환")
                solve_top_k = max(int(top_k), int(top_k) + len(hard_excluded_evidence_ids))
                model_chain = [str(x).strip() for x in getattr(solver, "generate_models", []) if str(x).strip()]
                primary_model = model_chain[0] if model_chain else ""
                if strict_numeric_primary:
                    self._log(
                        "숫자 문항 엄격 검증 적용: "
                        f"Q {current}/{total}, primary={primary_model or 'unknown'}, high-capacity-review=enabled"
                    )
                try:
                    decision = solver.solve(
                        question=question,
                        options=options,
                        top_k=solve_top_k,
                        exclude_evidence_ids=sorted(hard_excluded_evidence_ids) if hard_excluded_evidence_ids else None,
                        evidence_penalties=negative_evidence_penalties if negative_evidence_penalties else None,
                        strict_numerical_check=strict_numeric_primary,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log(f"RAG 풀이 1차 실패: {exc} / 재시도 1회")
                    exam_page.wait_for_timeout(600)
                    retry_top_k = max(2, min(int(top_k), 8))
                    try:
                        decision = solver.solve(
                            question=question,
                            options=options,
                            top_k=retry_top_k + len(hard_excluded_evidence_ids),
                            exclude_evidence_ids=sorted(hard_excluded_evidence_ids) if hard_excluded_evidence_ids else None,
                            evidence_penalties=negative_evidence_penalties if negative_evidence_penalties else None,
                            strict_numerical_check=strict_numeric_primary,
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
                        top_k=retry_top_k + len(hard_excluded_evidence_ids),
                        exclude_evidence_ids=sorted(hard_excluded_evidence_ids) if hard_excluded_evidence_ids else None,
                        evidence_penalties=negative_evidence_penalties if negative_evidence_penalties else None,
                        strict_numerical_check=strict_numeric_primary,
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
                    model_chain = [str(x).strip() for x in getattr(solver, "generate_models", []) if str(x).strip()]
                    model_chain = [m for i, m in enumerate(model_chain) if m not in model_chain[:i]]
                    if len(model_chain) > 1 and not used_answer_bank:
                        for alt_model in model_chain[1:]:
                            strict_numeric_alt = numeric_question
                            try:
                                alt_decision = solver.solve(
                                    question=question,
                                    options=options,
                                    top_k=retry_top_k + len(hard_excluded_evidence_ids),
                                    exclude_evidence_ids=sorted(hard_excluded_evidence_ids) if hard_excluded_evidence_ids else None,
                                    evidence_penalties=negative_evidence_penalties if negative_evidence_penalties else None,
                                    preferred_models=[alt_model],
                                    strict_numerical_check=strict_numeric_alt,
                                )
                            except Exception as exc:  # noqa: BLE001
                                self._log(f"교차검증 모델 실패: model={alt_model}, err={exc}")
                                continue
                            alt_choice = int(getattr(alt_decision, "choice", 0))
                            alt_conf = float(getattr(alt_decision, "confidence", 0.0))
                            alt_reason = str(getattr(alt_decision, "reason", ""))
                            alt_eids = list(getattr(alt_decision, "evidence_ids", []))
                            self._log(
                                "교차검증 모델 결과: "
                                f"Q {current}/{total} model={alt_model} -> choice={alt_choice}, conf={alt_conf:.2f}"
                            )
                            if alt_choice < 1 or alt_choice > len(options):
                                continue
                            improved = alt_conf >= (confidence + model_escalate_margin)
                            if alt_conf >= confidence_threshold or improved:
                                prev_conf = confidence
                                choice = alt_choice
                                confidence = alt_conf
                                reason = f"[cross-check:{alt_model}] {alt_reason}".strip()
                                evidence_ids = alt_eids or evidence_ids
                                self._log(
                                    "저신뢰 모델 스위칭 반영: "
                                    f"Q {current}/{total} {prev_conf:.2f} -> {confidence:.2f}, model={alt_model}"
                                )
                                if confidence >= confidence_threshold:
                                    break

                if confidence < confidence_threshold:
                    dynamic_budget = fallback_low_conf_budget
                    if total > 0:
                        required_correct = (total * pass_score + 99) // 100
                        dynamic_budget = max(0, total - required_correct)
                    confidence_gate = round(confidence + 1e-9, 2)
                    low_conf_floor_gate = round(low_conf_floor + 1e-9, 2)

                    if confidence_gate >= low_conf_floor_gate and low_conf_used < dynamic_budget:
                        low_conf_used += 1
                        self._log(
                            "LLM 저신뢰 문항 허용 진행: "
                            f"Q {current}/{total} conf={confidence_gate:.2f} "
                            f"(used {low_conf_used}/{dynamic_budget}, floor={low_conf_floor_gate:.2f})"
                        )
                    else:
                        skipped += 1
                        return _payload(
                            False,
                            (
                                f"LLM 신뢰도 낮음(conf={confidence_gate:.2f}, floor={low_conf_floor_gate:.2f}, "
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
                    "exam_seq": str(snap.get("exam_seq", "") or "").strip(),
                    "exam_item_seq": str(snap.get("exam_item_seq", "") or "").strip(),
                    "options": list(options),
                    "selected_choice": int(choice),
                    "selected_option": options[choice - 1] if 1 <= choice <= len(options) else "",
                    "confidence": float(confidence),
                    "reason": reason,
                    "evidence_ids": list(evidence_ids or []),
                    "source": source,
                    "used_answer_bank": bool(used_answer_bank),
                    "blocked_evidence_ids": sorted(hard_excluded_evidence_ids),
                    "evidence_penalties": {
                        str(k): float(v) for k, v in negative_evidence_penalties.items() if str(k).strip()
                    },
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
            generate_fallback_models=str(getattr(self.settings, "rag_generate_model_fallbacks", "")).split(","),
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
            + min(EKHNPAutomator._exam_question_text_quality(str(payload.get("question_text", ""))), 520)
            + (80 if int(payload.get("current", 0)) > 0 else 0)
            + (60 if int(payload.get("total", 0)) > 0 else 0)
            + int(payload.get("structured_bonus", 0) or 0)
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
            resolved = shutil.which("tesseract") or ""
            if not resolved:
                for candidate in (
                    "/opt/homebrew/bin/tesseract",
                    "/usr/local/bin/tesseract",
                ):
                    if Path(candidate).exists():
                        resolved = candidate
                        break
            self._tesseract_path = resolved
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

    def _extract_exam_question_snapshot(
        self,
        page: Page,
        force_ocr: bool = False,
        allow_ocr: bool = True,
        prefer_structured: bool = False,
    ) -> Optional[dict[str, Any]]:
        scopes: list[Any] = [page] + list(page.frames)
        best_snapshot: Optional[dict[str, Any]] = None
        best_score = -1
        structured_options: list[str] = []
        if prefer_structured:
            structured_snapshot = self._extract_exam_question_structured(page)
            if structured_snapshot is not None:
                best_snapshot = structured_snapshot
                best_score = self._score_exam_snapshot(structured_snapshot)
            structured_options = self._extract_exam_options_structured(page, attempts=2, wait_ms=320)

        for scope in scopes:
            body_text = ""
            try:
                body_text = scope.locator("body").inner_text(timeout=2000)
            except Exception:  # noqa: BLE001
                body_text = ""

            parsed_dom = self._parse_exam_text_payload(body_text) if body_text else None
            picked = parsed_dom
            source = "dom"

            if picked is not None and len(structured_options) >= 2:
                picked = dict(picked)
                picked["options"] = list(structured_options[:5])
                picked["option_count"] = len(picked["options"])
                source = "structured+dom"

            needs_ocr = force_ocr or (allow_ocr and (picked is None or int(picked.get("option_count", 0)) < 2))
            if needs_ocr:
                ocr_text = self._ocr_text_from_scope(scope)
                parsed_ocr = self._parse_exam_text_payload(ocr_text) if ocr_text else None
                if parsed_ocr is not None and len(structured_options) >= 2:
                    parsed_ocr = dict(parsed_ocr)
                    parsed_ocr["options"] = list(structured_options[:5])
                    parsed_ocr["option_count"] = len(parsed_ocr["options"])
                if parsed_ocr is not None and (
                    picked is None or self._score_exam_snapshot(parsed_ocr) > self._score_exam_snapshot(picked)
                ):
                    picked = parsed_ocr
                    source = "structured+ocr" if len(structured_options) >= 2 else "ocr"

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
                "structured_bonus": int(picked.get("structured_bonus", 0) or 0),
            }
            score = self._score_exam_snapshot(snapshot)
            if score > best_score:
                best_snapshot = snapshot
                best_score = score

        return best_snapshot

    def _extract_exam_options_structured(self, page: Page, attempts: int = 1, wait_ms: int = 0) -> list[str]:
        scopes: list[Any] = [page] + list(page.frames)
        best_texts: list[str] = []
        best_input_count = 0

        max_attempts = max(1, int(attempts))
        for attempt in range(max_attempts):
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
                            s = s.replace(/^([①②③④⑤]|[1-5]|[A-Ea-e]|[가-마])(?:\\s*[\\.)]\\s*|\\s+)/, '');
                            return norm(s);
                          };
                          const pushOption = (bucket, seen, rawText) => {
                            const txt = cleanOpt(rawText);
                            if (!txt || txt.length < 1) return;
                            if (seen.has(txt)) return;
                            seen.add(txt);
                            bucket.push(txt);
                          };

                          const radios = Array.from(
                            document.querySelectorAll('input[name="choiceAnswers"], input[type="radio"], input[type="checkbox"]')
                          );
                          const answerAnchors = Array.from(
                            document.querySelectorAll(
                              'a.answer-item, li[id^="example-item-"] a, li[class*="example-item"] a, .answer-box li a, .ex li a, .answer-radio, .answer-item, li.multiple, li.choice, .example li, .answers li, .question-answer li, label'
                            )
                          ).filter(isVisible);

                          const seen = new Set();
                          const texts = [];
                          for (const input of radios) {
                            let txt = '';
                            const inputId = String(input.getAttribute('id') || '').trim();
                            if (inputId) {
                              const label = document.querySelector(`label[for="${inputId}"]`);
                              if (label && isVisible(label)) txt = norm(label.innerText || label.textContent || '');
                            }
                            const parentLabel = input.closest('label');
                            if (!txt && parentLabel && isVisible(parentLabel)) {
                              txt = norm(parentLabel.innerText || parentLabel.textContent || '');
                            }
                            if (!txt) {
                              const container = input.closest('li, td, tr, div, p');
                              if (container && isVisible(container)) txt = norm(container.innerText || container.textContent || '');
                            }
                            pushOption(texts, seen, txt);
                          }

                          for (const anchor of answerAnchors) {
                            let txt = cleanOpt(anchor.innerText || anchor.textContent || '');
                            if (!txt) {
                              const li = anchor.closest('li');
                              if (li) txt = cleanOpt(li.innerText || li.textContent || '');
                            }
                            pushOption(texts, seen, txt);
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
                break
            if attempt < max_attempts - 1 and wait_ms > 0:
                page.wait_for_timeout(max(50, int(wait_ms)))

        if len(best_texts) >= 2:
            return best_texts[:5]

        if best_input_count >= 2:
            return [f"선택지 {idx}" for idx in range(1, min(best_input_count, 5) + 1)]

        return []

    @classmethod
    def _looks_like_exam_header_only(cls, text: str) -> bool:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if not raw:
            return True
        q_norm = cls._normalize_question_text(raw)
        if not q_norm:
            return True
        cue_tokens = (
            "다음",
            "옳",
            "올바른",
            "잘못",
            "틀린",
            "설명",
            "내용",
            "관련",
            "경우",
            "해당",
            "아닌",
            "무엇",
            "보안상",
        )
        if q_norm in {"종합평가", "시험평가"}:
            return True
        if len(q_norm) < 8 and (
            "답안 제출하기" in raw or re.search(r"\b\d{1,3}\s*/\s*\d{1,3}\b", raw)
        ):
            return True
        if "답안 제출하기" in raw and not any(token in q_norm for token in cue_tokens):
            return True
        return False

    @classmethod
    def _exam_question_text_quality(cls, text: str) -> int:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if not raw:
            return 0
        q_norm = cls._normalize_question_text(raw)
        if not q_norm:
            return 0
        cue_tokens = (
            "다음",
            "옳",
            "올바른",
            "잘못",
            "틀린",
            "설명",
            "내용",
            "관련",
            "경우",
            "해당",
            "아닌",
            "무엇",
            "보안상",
        )
        score = min(len(q_norm), 220)
        if any(token in q_norm for token in cue_tokens):
            score += 160
        if "?" in raw:
            score += 100
        if not cls._looks_like_exam_header_only(raw):
            score += 220
        return score

    def _extract_exam_question_structured(self, page: Page) -> Optional[dict[str, Any]]:
        scopes: list[Any] = [page] + list(page.frames)
        best_payload: Optional[dict[str, Any]] = None
        best_score = -1

        for scope in scopes:
            try:
                info = scope.evaluate(
                    """
                    () => {
                      const norm = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                      const isShown = (el) => {
                        if (!el) return false;
                        let cur = el;
                        while (cur && cur.nodeType === 1) {
                          const style = window.getComputedStyle(cur);
                          if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
                          cur = cur.parentElement;
                        }
                        return true;
                      };
                      const cleanQuestion = (txt) => norm(txt).replace(/^\\d+\\s*[\\.)]?\\s*/, '');
                      const cleanOpt = (txt) => {
                        let s = norm(txt);
                        s = s.replace(/^\\d+\\s*[\\.)]\\s*/, '');
                        s = s.replace(/^([①②③④⑤]|[1-5]|[A-Ea-e]|[가-마])(?:\\s*[\\.)]\\s*|\\s+)/, '');
                        return norm(s);
                      };

                      const quizItems = Array.from(document.querySelectorAll('.quiz_li[id^="que_"], .quiz_li')).filter(
                        (el) => el.querySelector('.que, p.que, .top')
                      );
                      if (!quizItems.length) return null;

                      let active = quizItems.find(isShown) || null;
                      if (!active) {
                        const selected = document.querySelector('.answer-item.on, .answer-radio.on');
                        active = selected ? selected.closest('.quiz_li') : null;
                      }
                      if (!active) {
                        active = quizItems.find((el) => !String(el.getAttribute('style') || '').includes('display:none')) || quizItems[0];
                      }
                      if (!active) return null;

                      let current = 0;
                      const id = String(active.id || '').trim();
                      let m = id.match(/que_0*([1-9]\\d*)$/i) || id.match(/que_([1-9]\\d*)$/i);
                      if (m) current = parseInt(m[1], 10) || 0;
                      if (!current) {
                        const orderText = norm(active.querySelector('.top .order, .order')?.textContent || '');
                        m = orderText.match(/(\\d{1,3})/);
                        if (m) current = parseInt(m[1], 10) || 0;
                      }

                      let total = quizItems.length;
                      const navLinks = Array.from(document.querySelectorAll('.b_box a[onclick*="numberCount"], a[onclick*="numberCount"]'));
                      for (const link of navLinks) {
                        const oc = String(link.getAttribute('onclick') || '');
                        const match = oc.match(/numberCount\\s*:\\s*(\\d{1,3})/);
                        if (match) total = Math.max(total, parseInt(match[1], 10) || 0);
                      }

                      const title = norm(document.querySelector('p.title, .title')?.textContent || '');
                      let question = '';
                      const qSelectors = ['.top .que', 'p.que', '.que', '.question', '[class*="question-title"]', '[class*="question_text"]'];
                      for (const selector of qSelectors) {
                        const el = active.querySelector(selector);
                        const txt = cleanQuestion(el?.textContent || el?.innerText || '');
                        if (txt) {
                          question = txt;
                          break;
                        }
                      }
                      if (!question) {
                        const topBox = active.querySelector('.top');
                        question = cleanQuestion(topBox?.textContent || topBox?.innerText || '');
                      }
                      if (title && question && !question.includes(title) && !question.includes('[종합평가]')) {
                        question = `${title} ${question}`.trim();
                      }

                      const optionNodes = Array.from(
                        active.querySelectorAll(
                          'a.answer-item, li[id^="example-item-"] a, li[class*="example-item"] a, .ex li a, .answer-box li a, .answer-radio, .answer-item, li.multiple, li.choice, .example li, .answers li, .question-answer li, label'
                        )
                      );
                      const options = [];
                      const seen = new Set();
                      for (const node of optionNodes) {
                        if (!isShown(node)) continue;
                        const text = cleanOpt(
                          node.querySelector('p')?.textContent
                          || node.querySelector('p')?.innerText
                          || node.textContent
                          || node.innerText
                          || ''
                        );
                        if (!text || seen.has(text)) continue;
                        seen.add(text);
                        options.push(text);
                      }

                      if (options.length < 2) {
                        const radioInputs = Array.from(
                          active.querySelectorAll('input[name="choiceAnswers"], input[type="radio"], input[type="checkbox"]')
                        );
                        for (const input of radioInputs) {
                          let text = '';
                          const inputId = String(input.getAttribute('id') || '').trim();
                          if (inputId) {
                            const label = active.querySelector(`label[for="${inputId}"]`) || document.querySelector(`label[for="${inputId}"]`);
                            if (label && isShown(label)) text = cleanOpt(label.innerText || label.textContent || '');
                          }
                          if (!text) {
                            const wrapper = input.closest('label, li, td, tr, div, p');
                            if (wrapper && isShown(wrapper)) text = cleanOpt(wrapper.innerText || wrapper.textContent || '');
                          }
                          if (!text || seen.has(text)) continue;
                          seen.add(text);
                          options.push(text);
                        }
                      }

                      const examSeq = String(active.querySelector('input[name="examSeqs"]')?.value || '').trim();
                      const examItemSeq = String(active.querySelector('input[name="examItemSeqs"]')?.value || '').trim();
                      const fullParts = [];
                      if (title) fullParts.push(title);
                      if (current > 0 && total > 0) fullParts.push(`${current} / ${total}`);
                      if (question) fullParts.push(question);
                      for (const opt of options) fullParts.push(opt);
                      const fullText = norm(fullParts.join(' '));

                      return {
                        full_text: fullText,
                        text_len: fullText.length,
                        current,
                        total,
                        question_text: question,
                        options,
                        option_count: options.length,
                        exam_seq: examSeq,
                        exam_item_seq: examItemSeq,
                        structured_bonus: 2800,
                      };
                    }
                    """
                )
            except Exception:  # noqa: BLE001
                continue

            if not isinstance(info, dict):
                continue
            key = self._build_exam_snapshot_key(info)
            if not key:
                continue
            question_text = str(info.get("question_text", "")).strip()
            option_count = int(info.get("option_count", 0) or 0)
            quality = self._exam_question_text_quality(question_text)
            score = option_count * 1000 + quality + int(info.get("structured_bonus", 0) or 0)
            if score > best_score:
                best_payload = {
                    "key": key,
                    "text_len": int(info.get("text_len", 0) or 0),
                    "option_count": option_count,
                    "current": int(info.get("current", 0) or 0),
                    "total": int(info.get("total", 0) or 0),
                    "question_text": question_text,
                    "options": [str(x).strip() for x in info.get("options", []) if str(x).strip()],
                    "full_text": str(info.get("full_text", "") or ""),
                    "source": "structured-question",
                    "structured_bonus": int(info.get("structured_bonus", 0) or 0),
                    "exam_seq": str(info.get("exam_seq", "") or "").strip(),
                    "exam_item_seq": str(info.get("exam_item_seq", "") or "").strip(),
                }
                best_score = score

        return best_payload

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
        # 시험 UI 동적/잡음 텍스트 제거(타이머, 네비게이션, 헤더)
        src = re.sub(r"\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b", " ", src)
        src = re.sub(r"\b답안\s*제출하기\b", " ", src)
        src = re.sub(r"\b\d{1,3}\s*/\s*\d{1,3}\b", " ", src)
        src = re.sub(r"\b(?:이전|다음)\b", " ", src)
        src = re.sub(r"\[\s*종합평가\s*\]\s*-\s*", " ", src)
        # 유형 마커가 있으면 그 지점부터 문항 본문으로 간주
        marker_pos = -1
        for marker in ("객관식", "진위형", "주관식"):
            pos = src.find(marker)
            if pos >= 0 and (marker_pos < 0 or pos < marker_pos):
                marker_pos = pos
        if marker_pos > 0:
            src = src[marker_pos:]
        src = re.sub(r"^(?:문항|문제|q)\s*\d{1,3}\s*", " ", src)
        src = re.sub(r"^\d{1,3}\s*", " ", src)
        src = re.sub(r"^(?:객관식|주관식|단일형|복수형|보기)\s*", " ", src)
        # 문제 문자열에 보기 본문이 한 줄로 뒤섞여 붙는 경우 첫 보기 앞에서 잘라냅니다.
        option_start = re.search(
            r"\s(?:[1-5]|[①②③④⑤]|[A-Ea-e]|[가-마])(?:\s*[\.\)]|\s)\s*[0-9A-Za-z가-힣]",
            src,
        )
        if option_start and option_start.start() >= 6:
            src = src[: option_start.start()]
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
        self,
        question: str,
        options: list[str],
        exam_meta: Optional[dict[str, str]] = None,
        *,
        course_title: str = "",
        question_no: int = 0,
    ) -> Optional[dict[str, Any]]:
        if len(options) < 2:
            return None

        q_norm = self._normalize_answer_text(question)
        q_match_norm = self._normalize_question_text(question)
        q_sig = self._question_signature_from_norm(q_match_norm)
        opt_norms = [self._normalize_answer_text(x) for x in options]
        option_set_sig = self._option_set_signature_from_norms(opt_norms)
        course_key = self._course_title_key(course_title)

        if self._answer_bank_items:
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

        verified_candidates: list[dict[str, Any]] = []
        if course_key and question_no > 0:
            verified_candidates.extend(self._verified_exam_course_order_index.get(f"{course_key}||{question_no}", []))
        if course_key and q_sig:
            verified_candidates.extend(self._verified_exam_qsig_index.get(f"{course_key}||{q_sig}", []))

        seen_verified: set[int] = set()
        for item in verified_candidates:
            if not isinstance(item, dict):
                continue
            marker = id(item)
            if marker in seen_verified:
                continue
            seen_verified.add(marker)
            ans_opt_norm = str(item.get("correct_option_norm", "")).strip()
            if not ans_opt_norm:
                ans_opt_norm = self._normalize_answer_text(str(item.get("correct_option", "") or ""))
            if not ans_opt_norm:
                continue
            mapped_idx, mapped_sim, mapped_mode = self._map_answer_option_norm_to_choice(ans_opt_norm, opt_norms)
            if mapped_idx > 0 and mapped_mode in {"exact", "contains"}:
                conf = 0.995 if question_no > 0 else 0.99
                return {"choice": mapped_idx, "reason": "verified-report exact", "confidence": conf}
            if mapped_idx > 0 and mapped_sim >= 0.80:
                conf = min(0.985, 0.94 + 0.04 * mapped_sim)
                return {"choice": mapped_idx, "reason": "verified-report fuzzy", "confidence": conf}

        ordered_candidates: list[dict[str, Any]] = []
        if course_key and question_no > 0 and option_set_sig:
            ordered_candidates.extend(
                self._answer_bank_course_order_optset_index.get(f"{course_key}||{question_no}||{option_set_sig}", [])
            )
        if q_sig and option_set_sig:
            ordered_candidates.extend(self._answer_bank_qsig_optset_index.get(f"{q_sig}||{option_set_sig}", []))
        if q_norm:
            ordered_candidates.extend(self._answer_bank_qnorm_index.get(q_norm, []))
        if q_sig:
            ordered_candidates.extend(self._answer_bank_qsig_index.get(q_sig, []))
        if option_set_sig:
            ordered_candidates.extend(self._answer_bank_optset_index.get(option_set_sig, []))

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

        if option_set_sig:
            scoped_optset_items = [
                item
                for item in self._answer_bank_optset_index.get(option_set_sig, [])
                if isinstance(item, dict) and self._is_answer_item_scope_match(item, exam_meta)
            ]
            if scoped_optset_items:
                mapped_choices: set[int] = set()
                for item in scoped_optset_items:
                    saved_opts_norm = [self._normalize_answer_text(str(x)) for x in item.get("options", []) if str(x).strip()]
                    try:
                        idx_saved = int(item.get("answer_index", 0))
                    except Exception:  # noqa: BLE001
                        idx_saved = 0
                    if not (1 <= idx_saved <= len(saved_opts_norm)):
                        continue
                    mapped_idx, mapped_sim, _ = self._map_answer_option_norm_to_choice(saved_opts_norm[idx_saved - 1], opt_norms)
                    if mapped_idx > 0 and mapped_sim >= 0.72:
                        mapped_choices.add(mapped_idx)
                if len(mapped_choices) == 1:
                    only_choice = next(iter(mapped_choices))
                    conf = 0.97 if len(scoped_optset_items) == 1 else 0.95
                    return {"choice": only_choice, "reason": "answer-bank option-set", "confidence": conf}

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
        *,
        course_title: str = "",
        question_no: int = 0,
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
            "course_title": str(course_title or "").strip(),
            "course_title_key": self._course_title_key(course_title),
            "question_no": max(0, int(question_no)),
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

        normalized_lines: list[str] = []
        question_hint_tokens = (
            "[객관식",
            "[주관식",
            "[진위형",
            "객관식",
            "주관식",
            "진위형",
            "다음",
            "옳은",
            "올바른",
            "틀린",
            "잘못된",
            "설명한",
            "관련하여",
        )
        for ln in lines:
            match_inline_q = re.match(r"^(\d{1,3})[\.\)]\s*(.+)$", ln)
            if match_inline_q:
                tail = str(match_inline_q.group(2) or "").strip()
                if tail and (
                    tail.endswith("?")
                    or any(token in tail for token in question_hint_tokens)
                ):
                    normalized_lines.append(f"{match_inline_q.group(1)}.")
                    normalized_lines.append(tail)
                    continue
            normalized_lines.append(ln)
        lines = normalized_lines

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
                if re.fullmatch(r"\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}", s):
                    continue
                if re.fullmatch(r"\d{1,3}\s*/\s*\d{1,3}", s):
                    continue
                if s in {"답안 제출하기", "다음", "이전", "완료"}:
                    continue
                if "획득점수" in s and "점" in s:
                    continue
                if re.match(r"^\[종합평가\]\s*-\s*", s):
                    s = re.sub(r"^\[종합평가\]\s*-\s*", "", s).strip()
                    if not s:
                        continue
                if re.match(r"^\[객관식", s):
                    # "[객관식 단일형] 질문..." 형태에서 질문 본문은 유지
                    s = re.sub(r"^\[[^\]]+\]\s*", "", s).strip()
                    if not s:
                        continue

                # 보기 번호 + 텍스트 한 줄 포맷은 [정답] 처리보다 먼저 잡아야 정답 번호가 어긋나지 않습니다.
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
        raw_found = len(entries)
        deduped_entries: list[dict[str, Any]] = []
        seen_entry_keys: set[str] = set()
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            question = str(ent.get("question", "")).strip()
            options = [str(x).strip() for x in ent.get("options", []) if str(x).strip()]
            ans_idx = int(ent.get("answer_index", 0) or 0)
            if not question or len(options) < 2 or ans_idx <= 0:
                continue
            key = f"{self._make_answer_bank_key(question, options)}:{ans_idx}"
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
                course_title=self._last_opened_course_title,
                question_no=int(ent.get("q_no", 0) or 0),
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
            "reason": (
                f"resultYn={panel.get('resultYn', '')}, texts={len(unique_texts)}, "
                f"raw_entries={raw_found}, deduped={len(entries)}"
            ),
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
                    "question_norm": q_norm,
                    "question_signature": q_sig,
                    "options": list(opts),
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
                "course_title_key": self._course_title_key(course_title),
                "attempt_no": int(attempt_no),
                "exam_meta": solve_payload.get("exam_runtime_meta") or learn_payload.get("exam_meta") or {},
                "completion_state": completion_state or {},
                "learn_reason": str(learn_payload.get("reason", "")),
            },
            "summary": summary,
            "rows": rows,
        }

        path = ""
        warning_path = ""
        try:
            self._exam_quality_report_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_course = re.sub(r"[^0-9A-Za-z가-힣]+", "_", str(course_title or "course")).strip("_") or "course"
            out_path = self._exam_quality_report_dir / f"exam_quality_{stamp}_{safe_course}_try{int(attempt_no):02d}.json"
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            path = str(out_path)
            self._note_artifact(
                out_path,
                kind="exam-quality-report",
                label=f"{safe_course} try{int(attempt_no):02d}",
                metadata={"course_title": str(course_title or "").strip()},
            )
            self._log(f"시험 파싱 품질 리포트 저장: {path}")
            if matched < total or known < total:
                warning_dir = self._exam_quality_report_dir / "warning_snapshots"
                warning_dir.mkdir(parents=True, exist_ok=True)
                unresolved_rows = [
                    row
                    for row in rows
                    if not bool(row.get("matched_result_entry")) or not isinstance(row.get("is_correct"), bool)
                ]
                warning_payload = {
                    "meta": payload["meta"],
                    "summary": summary,
                    "unresolved_rows": unresolved_rows,
                }
                warning_out = warning_dir / f"exam_quality_warn_{stamp}_{safe_course}_try{int(attempt_no):02d}.json"
                warning_out.write_text(json.dumps(warning_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                warning_path = str(warning_out)
                self._note_artifact(
                    warning_out,
                    kind="exam-quality-warning-snapshot",
                    label=f"{safe_course} try{int(attempt_no):02d}",
                    metadata={"course_title": str(course_title or "").strip()},
                )
                self._log(f"시험 품질 경고 스냅샷 저장: {warning_path}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"시험 품질 리포트 저장 실패: {exc}")

        return {"path": path, "warning_path": warning_path, "rows": rows, "summary": summary}

    @staticmethod
    def _inspect_exam_quality_report(report: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = report if isinstance(report, dict) else {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        questions = int(summary.get("questions", 0) or 0)
        matched = int(summary.get("matched_result_entries", 0) or 0)
        known = int(summary.get("correctness_known", 0) or 0)
        correct = int(summary.get("correct", 0) or 0)
        complete_alignment = questions > 0 and matched >= questions and known >= questions
        issues: list[str] = []
        if questions <= 0:
            issues.append("questions=0")
        if matched < questions:
            issues.append(f"matched={matched}/{questions}")
        if known < questions:
            issues.append(f"known={known}/{questions}")
        return {
            "questions": questions,
            "matched": matched,
            "known": known,
            "correct": correct,
            "complete_alignment": complete_alignment,
            "message": ", ".join(issues) if issues else f"matched={matched}/{questions}, known={known}/{questions}, correct={correct}",
        }

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
            if self._click_next_arrow_like(scope):
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
            if self._click_next_arrow_like(scope):
                page.wait_for_timeout(900)
                return True
        return False

    @staticmethod
    def _click_next_arrow_like(scope: Any) -> bool:
        try:
            return bool(
                scope.evaluate(
                    """
                    () => {
                      const normalize = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const isVisible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const style = window.getComputedStyle(el);
                        if (!style) return false;
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                          return false;
                        }
                        if (style.pointerEvents === 'none') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const isInteractive = (el) => {
                        if (!el) return false;
                        const tag = (el.tagName || '').toLowerCase();
                        if (['a', 'button', 'input', 'summary'].includes(tag)) return true;
                        if (el.getAttribute('role') === 'button') return true;
                        if (el.hasAttribute('onclick')) return true;
                        if ((el.getAttribute('href') || '').length > 0) return true;
                        if ((el.getAttribute('tabindex') || '').length > 0) return true;
                        return false;
                      };
                      const pickTarget = (el) => (
                        el.closest('a,button,input,[role="button"],[onclick],.next,.nextPage,.btn_next,#nextBtn,[tabindex],#nextPage')
                        || el
                      );

                      const nodes = Array.from(document.querySelectorAll('a,button,input,div,span,i,img,svg,use'));
                      const nextKeywords = [
                        '다음', 'next', 'arrow', 'right', 'nextpage', 'btn_next', 'donext',
                        '다음차시', '다음목차', 'continue',
                        'chevron_right', 'keyboard_arrow_right', 'navigate_next',
                        'angle-right', 'fa-angle-right'
                      ];
                      const prevKeywords = ['prev', 'previous', 'left', 'back', '이전', '닫기', 'close', 'arrow_left', 'sk_prev'];
                      let best = null;
                      const vw = Math.max(window.innerWidth || 0, document.documentElement.clientWidth || 0);
                      const vh = Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0);

                      for (const n of nodes) {
                        if (!isVisible(n)) continue;
                        const target = pickTarget(n);
                        if (!target || !isVisible(target)) continue;
                        const clickEl = target;

                        const attrs = [
                          n.textContent,
                          n.getAttribute && n.getAttribute('alt'),
                          n.getAttribute && n.getAttribute('title'),
                          n.getAttribute && n.getAttribute('aria-label'),
                          n.getAttribute && n.getAttribute('src'),
                          n.getAttribute && n.getAttribute('class'),
                          n.getAttribute && n.getAttribute('id'),
                          target.textContent,
                          target.getAttribute && target.getAttribute('alt'),
                          target.getAttribute && target.getAttribute('title'),
                          target.getAttribute && target.getAttribute('aria-label'),
                          target.getAttribute && target.getAttribute('src'),
                          target.getAttribute && target.getAttribute('class'),
                          target.getAttribute && target.getAttribute('id'),
                          target.getAttribute && target.getAttribute('onclick'),
                          target.getAttribute && target.getAttribute('href'),
                          target.getAttribute && target.getAttribute('src'),
                        ].map(normalize).join(' ');

                        let score = 0;
                        if (nextKeywords.some((k) => attrs.includes(k))) score += 120;
                        if (
                          attrs.includes('>') || attrs.includes('›') || attrs.includes('＞')
                          || attrs.includes('→') || attrs.includes('»')
                          || attrs.includes('chevron-right') || attrs.includes('chevron_right')
                          || attrs.includes('keyboard_arrow_right') || attrs.includes('navigate_next')
                          || attrs.includes('arrow-right') || attrs.includes('angle-right')
                        ) {
                          score += 45;
                        }
                        if (
                          attrs.includes('nextpage')
                          || attrs.includes('arrow_right')
                          || attrs.includes('sk_next')
                          || attrs.includes('btn_next')
                          || attrs.includes('next_btn')
                        ) {
                          score += 90;
                        }
                        if (prevKeywords.some((k) => attrs.includes(k))) score -= 140;
                        if (attrs.includes('donextshowitem') || attrs.includes('nextindex')) score += 35;
                        if ((n.tagName || '').toLowerCase() === 'img' || (n.tagName || '').toLowerCase() === 'svg') score += 8;

                        const r = clickEl.getBoundingClientRect();
                        const cx = r.left + r.width * 0.5;
                        const cy = r.top + r.height * 0.5;
                        if (vw > 0 && (r.left + r.width * 0.5) >= vw * 0.55) score += 8;
                        if (vh > 0 && (r.top + r.height * 0.5) >= vh * 0.35) score += 6;
                        if (vw > 0 && cx >= vw * 0.82) score += 12;
                        if (vh > 0 && cy >= vh * 0.64) score += 12;
                        if (r.width >= 22 && r.height >= 22 && r.width <= 100 && r.height <= 100) {
                          const style = window.getComputedStyle(clickEl);
                          const br = normalize(style && style.borderRadius);
                          const circular =
                            br.includes('50%') ||
                            br.includes('9999') ||
                            Math.abs(r.width - r.height) <= Math.max(3, r.width * 0.2);
                          if (circular) score += 30;
                        }

                        if (!isInteractive(clickEl) && score < 95) continue;
                        if (!best || score > best.score) best = { target: clickEl, score };
                      }

                      // 텍스트/속성이 빈약한 플레이어(원형 아이콘만 노출) 대응
                      if (!best) {
                        const round = nodes
                          .map((n) => {
                            const target = pickTarget(n);
                            if (!target || !isVisible(target)) return null;
                            const t = target;
                            const r = t.getBoundingClientRect();
                            if (r.width < 24 || r.height < 24 || r.width > 96 || r.height > 96) return null;
                            const style = window.getComputedStyle(t);
                            const br = normalize(style && style.borderRadius);
                            const circular =
                              br.includes('50%') ||
                              br.includes('9999') ||
                              Math.abs(r.width - r.height) <= Math.max(3, r.width * 0.15);
                            if (!circular) return null;
                            const cx = r.left + r.width * 0.5;
                            const cy = r.top + r.height * 0.5;
                            if (vw > 0 && cx < vw * 0.62) return null;
                            if (vh > 0 && cy < vh * 0.58) return null;
                            if (!isInteractive(t)) return null;
                            const attrs = [
                              n.textContent, n.getAttribute && n.getAttribute('alt'),
                              n.getAttribute && n.getAttribute('title'),
                              n.getAttribute && n.getAttribute('aria-label'),
                              n.getAttribute && n.getAttribute('class'),
                              n.getAttribute && n.getAttribute('id'),
                              t.textContent, t.getAttribute && t.getAttribute('alt'),
                              t.getAttribute && t.getAttribute('title'),
                              t.getAttribute && t.getAttribute('aria-label'),
                              t.getAttribute && t.getAttribute('class'),
                              t.getAttribute && t.getAttribute('id'),
                            ].map(normalize).join(' ');
                            if (prevKeywords.some((k) => attrs.includes(k))) {
                              return null;
                            }
                            return { target: t, cx, cy };
                          })
                          .filter(Boolean);
                        if (round.length > 0) {
                          round.sort((a, b) => (b.cx - a.cx) || (b.cy - a.cy));
                          best = { target: round[0].target, score: 110 };
                        }
                      }

                      if (!best || best.score < 55) return false;
                      const el = best.target;
                      try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                      try { el.click(); return true; } catch (e) {}
                      try {
                        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                      } catch (e) {}
                      return false;
                    }
                    """
                )
            )
        except Exception:  # noqa: BLE001
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
            snap = self._extract_exam_question_snapshot(page, allow_ocr=False, prefer_structured=True)
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

    def _refresh_classroom_page(self, classroom_page: Page) -> Page:
        # 중요: 강의(팝업/플레이어) 창이 아니라 강의실 메인 페이지만 새로고침합니다.
        try:
            if self._is_exam_url(classroom_page.url):
                self._log("시험 페이지 감지: 강의실 새로고침을 건너뜁니다.")
                return classroom_page
        except Exception:  # noqa: BLE001
            pass
        if self._is_classroom_access_denied(classroom_page):
            recovered = self._relogin_and_reopen_course_classroom(
                classroom_page,
                preferred_title=self._last_opened_course_title,
            )
            if recovered is not None:
                classroom_page = recovered
        self._log("강의실 새로고침으로 학습진행현황을 업데이트합니다.")
        try:
            classroom_page.reload(wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            try:
                classroom_page.goto(classroom_page.url, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
        classroom_page.wait_for_timeout(1200)
        if self._is_classroom_access_denied(classroom_page):
            recovered = self._relogin_and_reopen_course_classroom(
                classroom_page,
                preferred_title=self._last_opened_course_title,
            )
            if recovered is not None:
                classroom_page = recovered
        return classroom_page

    def _extract_classroom_lesson_rows(self, page: Page) -> list[dict[str, Any]]:
        try:
            rows = page.evaluate(
                """
                () => {
                  const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                  const compact = (txt) => normalize(txt).replace(/\\s+/g, '');
                  const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const allButtons = Array.from(
                    document.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                  );
                  const isLessonBtn = (el) => {
                    const txt = compact(el.textContent || el.value || '');
                    const onclick = String(el.getAttribute('onclick') || '');
                    const href = String(el.getAttribute('href') || '');
                    return (
                      txt === '학습하기'
                      || txt === '이어학습하기'
                      || onclick.includes('doStudyPopup')
                      || onclick.includes('doLearning')
                      || href.includes('doStudyPopup')
                      || href.includes('doLearning')
                    );
                  };
                  const popupMeta = (btn) => {
                    const onclick = String(btn?.getAttribute('onclick') || '');
                    const href = String(btn?.getAttribute('href') || '');
                    return {
                      onclick,
                      href,
                      opensPopup:
                        onclick.includes('doStudyPopup')
                        || onclick.includes('MAIN.doStudyPopup')
                        || href.includes('doStudyPopup')
                        || href.includes('MAIN.doStudyPopup'),
                    };
                  };
                  const weekNoFrom = (titleEl) => {
                    if (!titleEl) return 0;
                    const orderText = normalize(titleEl.querySelector('.order')?.textContent || '');
                    let match = orderText.match(/(\\d{1,3})/);
                    if (match) return parseInt(match[1], 10);
                    match = normalize(titleEl.textContent || '').match(/^(\\d{1,3})\\s*[\\.\\)]/);
                    if (match) return parseInt(match[1], 10);
                    return 0;
                  };
                  const weekTitleFrom = (titleEl) => {
                    if (!titleEl) return '';
                    const clone = titleEl.cloneNode(true);
                    clone.querySelectorAll('.order').forEach((el) => el.remove());
                    return normalize(clone.textContent || '');
                  };
                  const lessonTitleFrom = (itemEl, statusText) => {
                    const sub = itemEl.querySelector('.top .sub');
                    const clone = sub ? sub.cloneNode(true) : itemEl.cloneNode(true);
                    clone.querySelectorAll('.period').forEach((el) => el.remove());
                    let title = normalize(clone.textContent || '');
                    if (statusText && title.startsWith(statusText)) {
                      title = normalize(title.slice(statusText.length));
                    }
                    return title.slice(0, 120);
                  };
                  const lessonHead = Array.from(document.querySelectorAll('.lct_head h4, .lct_head .title'))
                    .find((el) => normalize(el.textContent || '') === '학습 차시');
                  const lessonViews = [];
                  if (lessonHead) {
                    let node = lessonHead.closest('.lct_head');
                    while (node && node.nextElementSibling) {
                      node = node.nextElementSibling;
                      if (!node) break;
                      if (node.classList?.contains('lct_head')) break;
                      if (node.classList?.contains('lct_view')) lessonViews.push(node);
                    }
                  }
                  const views = lessonViews.length > 0
                    ? lessonViews
                    : Array.from(document.querySelectorAll('.lct_view')).filter((view) => {
                        const titleEl = view.querySelector(':scope > .title') || view.querySelector('.title');
                        return weekNoFrom(titleEl) > 0;
                      });
                  const out = [];
                  views.forEach((view) => {
                    const titleEl = view.querySelector(':scope > .title') || view.querySelector('.title');
                    const weekNo = weekNoFrom(titleEl);
                    if (weekNo <= 0) return;
                    const weekTitle = weekTitleFrom(titleEl);
                    const list = view.querySelector('ul.c_list2');
                    if (!list) return;
                    const items = Array.from(list.children).filter((el) => el.tagName === 'LI');
                    items.forEach((itemEl, itemIndex) => {
                      const btn = Array.from(
                        itemEl.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                      ).find((el) => isVisible(el) && isLessonBtn(el));
                      if (!btn) return;
                      const buttonIndex = allButtons.indexOf(btn);
                      if (buttonIndex < 0) return;
                      const meta = popupMeta(btn);
                      const statusText = normalize(itemEl.querySelector('.top .sub .period')?.textContent || '');
                      const title = lessonTitleFrom(itemEl, statusText);
                      const rowText = normalize(itemEl.innerText || itemEl.textContent || '');
                      const isInlineQuiz = title.includes('학습평가') || rowText.includes('학습평가');
                      const isCompleted = statusText.includes('학습완료');
                      const isIncomplete = statusText.includes('미완료') && !isInlineQuiz;
                      out.push({
                        key: `${weekNo}|${itemIndex + 1}|${title}|${statusText}|${meta.onclick || meta.href || ''}`,
                        button_index: buttonIndex,
                        button_text: normalize(btn.textContent || btn.value || ''),
                        button_onclick: meta.onclick,
                        lesson_no: weekNo,
                        lesson_index: itemIndex + 1,
                        week_title: weekTitle,
                        title,
                        status_text: statusText || (isInlineQuiz ? '학습평가' : (isIncomplete ? '미완료' : (isCompleted ? '학습완료' : '기타'))),
                        is_incomplete: isIncomplete,
                        is_completed: isCompleted,
                        is_inline_quiz: isInlineQuiz,
                        has_start_button: true,
                        opens_popup: meta.opensPopup,
                        row_text: rowText.slice(0, 260),
                      });
                    });
                  });
                  out.sort((a, b) => {
                    const aNo = a.lesson_no > 0 ? a.lesson_no : 9999;
                    const bNo = b.lesson_no > 0 ? b.lesson_no : 9999;
                    if (aNo !== bNo) return aNo - bNo;
                    const aIdx = a.lesson_index > 0 ? a.lesson_index : 9999;
                    const bIdx = b.lesson_index > 0 ? b.lesson_index : 9999;
                    if (aIdx !== bIdx) return aIdx - bIdx;
                    if (Boolean(a.opens_popup) !== Boolean(b.opens_popup)) {
                      return Boolean(a.opens_popup) ? -1 : 1;
                    }
                    return String(a.title || '').localeCompare(String(b.title || ''), 'ko');
                  });
                  return out;
                }
                """
            )
        except Exception:  # noqa: BLE001
            return []
        return rows if isinstance(rows, list) else []

    def _click_lesson_button_by_index(self, page: Page, button_index: int) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    (buttonIndex) => {
                      const isVisible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const style = window.getComputedStyle(el);
                        if (!style) return false;
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const buttons = Array.from(
                        document.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                      );
                      const target = buttons[buttonIndex];
                      if (!target || !isVisible(target)) return false;
                      try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                      try { target.click(); return true; } catch (e) {}
                      try {
                        target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                      } catch (e) {}
                      return false;
                    }
                    """,
                    button_index,
                )
            )
        except Exception:  # noqa: BLE001
            return False

    def _select_next_lesson_row(
        self,
        rows: list[dict[str, Any]],
        *,
        allow_unfinished_fallback: bool = True,
        preferred_key: str = "",
    ) -> Optional[dict[str, Any]]:
        completed_count = sum(1 for row in rows if bool(row.get("is_completed")) and not bool(row.get("is_inline_quiz")))
        if completed_count > 0:
            self._log(f"completed-lesson-skipped: count={completed_count}")

        current_percent = int(self._last_observed_course_progress_percent)
        preferred_key = str(preferred_key or "").strip()
        if preferred_key:
            preferred_rows = [
                row for row in rows
                if str(row.get("key", "")).strip() == preferred_key
                and not bool(row.get("is_completed"))
                and bool(row.get("has_start_button"))
            ]
            preferred_rows.sort(
                key=lambda row: (
                    0 if bool(row.get("opens_popup")) else 1,
                    0 if bool(row.get("is_incomplete")) else 1,
                    int(row.get("lesson_no", 9999) or 9999),
                    int(row.get("lesson_index", 9999) or 9999),
                )
            )
            for row in preferred_rows:
                self._log(
                    "recovery-preferred-lesson-selected: "
                    f"lesson={row.get('lesson_no', 0)}-{row.get('lesson_index', 0)} "
                    f"title={row.get('title', '')}"
                )
                return row
        blocked_incomplete_rows = [
            row for row in rows
            if bool(row.get("is_incomplete"))
            and not bool(row.get("is_inline_quiz"))
            and not bool(row.get("is_completed"))
            and not bool(row.get("opens_popup"))
        ]
        blocked_gate_rows: list[dict[str, Any]] = []
        if blocked_incomplete_rows:
            first_blocked = min(
                blocked_incomplete_rows,
                key=lambda row: (
                    int(row.get("lesson_no", 9999) or 9999),
                    int(row.get("lesson_index", 9999) or 9999),
                ),
            )
            blocked_gate_rows = [
                row for row in rows
                if bool(row.get("is_inline_quiz"))
                and not bool(row.get("is_completed"))
                and bool(row.get("opens_popup"))
                and (
                    int(row.get("lesson_no", 9999) or 9999),
                    int(row.get("lesson_index", 9999) or 9999),
                ) <= (
                    int(first_blocked.get("lesson_no", 9999) or 9999),
                    int(first_blocked.get("lesson_index", 9999) or 9999),
                )
            ]
            if blocked_gate_rows:
                gate_row = blocked_gate_rows[0]
                self._log(
                    "inline-quiz-gate-needed: "
                    f"blocked={first_blocked.get('lesson_no', 0)}-{first_blocked.get('lesson_index', 0)} "
                    f"gate={gate_row.get('lesson_no', 0)}-{gate_row.get('lesson_index', 0)} "
                    f"title={gate_row.get('title', '')}"
                )
        groups: list[list[dict[str, Any]]] = [
            [
                row for row in rows
                if bool(row.get("is_incomplete"))
                and not bool(row.get("is_inline_quiz"))
                and not bool(row.get("is_completed"))
                and bool(row.get("opens_popup"))
            ]
        ]
        if blocked_gate_rows:
            groups.append(blocked_gate_rows)
        groups.append(
            [
                row for row in rows
                if bool(row.get("is_incomplete"))
                and not bool(row.get("is_inline_quiz"))
                and not bool(row.get("is_completed"))
            ]
        )
        if allow_unfinished_fallback:
            groups.append(
                [
                    row for row in rows
                    if not bool(row.get("is_completed"))
                    and not bool(row.get("is_inline_quiz"))
                    and bool(row.get("opens_popup"))
                    and bool(row.get("has_start_button"))
                ]
            )
            groups.append(
                [
                    row for row in rows
                    if not bool(row.get("is_completed"))
                    and not bool(row.get("is_inline_quiz"))
                    and bool(row.get("has_start_button"))
                ]
            )

        seen_keys: set[str] = set()
        for group in groups:
            for row in group:
                key = str(row.get("key", "")).strip()
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                if (
                    key == self._last_opened_lesson_key
                    and current_percent >= 0
                    and self._last_opened_lesson_course_percent >= 0
                    and current_percent <= self._last_opened_lesson_course_percent
                ):
                    self._log(
                        "lesson-progress-still-unchanged: "
                        f"current={current_percent}% prev={self._last_opened_lesson_course_percent}%"
                    )
                    self._log(
                        "same-completed-lesson-reopened: "
                        f"lesson={row.get('lesson_no', 0)} title={row.get('title', '')}"
                    )
                    continue
                return row
        return None

    def _remember_opened_lesson_row(self, row: dict[str, Any]) -> None:
        self._last_opened_lesson_key = str(row.get("key", "")).strip()
        self._last_opened_lesson_title = str(row.get("title", "")).strip()
        self._last_opened_lesson_course_percent = int(self._last_observed_course_progress_percent)

    def _open_resume_learning_popup(self, page: Page) -> Optional[Page]:
        before_pages = list(page.context.pages)
        selectors = [
            'div:has-text("학습진행현황") a:has-text("이어 학습하기")',
            'div:has-text("학습진행현황") a:has-text("이어 학습 하기")',
            'div:has-text("학습진행현황") button:has-text("이어 학습하기")',
            'div:has-text("학습진행현황") button:has-text("이어 학습 하기")',
            'div:has-text("학습진행현황") input[value*="이어 학습하기"]',
            'div:has-text("학습진행현황") input[value*="이어 학습 하기"]',
            'a[onclick*="doStudyPopup"]:has-text("이어 학습하기")',
            'a[onclick*="doStudyPopup"]:has-text("이어 학습 하기")',
        ]
        clicked = False
        popup_page: Optional[Page] = None
        try:
            with page.expect_popup(timeout=15000) as popup_info:
                clicked = self._click_first_visible(page, selectors, max_items=20)
            popup_page = popup_info.value if clicked else None
        except Exception:  # noqa: BLE001
            clicked = self._click_first_visible(page, selectors, max_items=20)

        if not clicked:
            try:
                clicked = bool(
                    page.evaluate(
                        """
                        () => {
                          const normalize = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                          const compact = (txt) => normalize(txt).replace(/\\s+/g, '');
                          const isVisible = (el) => {
                            if (!el || !el.getBoundingClientRect) return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                          };
                          const nodes = Array.from(
                            document.querySelectorAll('a,button,input[type="button"],input[type="submit"],span')
                          );
                          const target = nodes.find((el) => {
                            const txt = compact(el.textContent || el.value || '');
                            return isVisible(el) && txt === '이어학습하기';
                          });
                          if (!target) return false;
                          target.click();
                          return true;
                        }
                        """
                    )
                )
            except Exception:  # noqa: BLE001
                clicked = False

        if clicked:
            self._log("resume-fallback-opened")
            page.wait_for_timeout(1500)
            if popup_page is not None:
                popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
                return popup_page
            picked = self._pick_learning_page(page.context.pages, before_pages)
            if picked is not None and picked != page:
                return picked
        return None

    def _open_next_unfinished_lesson_popup(
        self,
        page: Page,
        *,
        rows: Optional[list[dict[str, Any]]] = None,
        preferred_key: str = "",
    ) -> Optional[Page]:
        rows = rows if rows is not None else self._extract_classroom_lesson_rows(page)
        chosen = self._select_next_lesson_row(rows, preferred_key=preferred_key)
        if chosen is None:
            return None

        before_pages = list(page.context.pages)
        clicked = False
        popup_page: Optional[Page] = None
        try:
            with page.expect_popup(timeout=12000) as popup_info:
                clicked = self._click_lesson_button_by_index(page, int(chosen.get("button_index", -1)))
            popup_page = popup_info.value if clicked else None
        except Exception:  # noqa: BLE001
            clicked = self._click_lesson_button_by_index(page, int(chosen.get("button_index", -1)))

        if not clicked:
            return None

        self._remember_opened_lesson_row(chosen)
        if bool(chosen.get("is_inline_quiz")):
            self._log(
                "inline-quiz-gate-opened: "
                f"lesson={chosen.get('lesson_no', 0)}-{chosen.get('lesson_index', 0)} "
                f"title={chosen.get('title', '')} source=inline-quiz-gate"
            )
        else:
            self._log(
                "incomplete-lesson-opened: "
                f"lesson={chosen.get('lesson_no', 0)}-{chosen.get('lesson_index', 0)} "
                f"title={chosen.get('title', '')} status={chosen.get('status_text', '')} "
                f"source=incomplete-row"
            )
        page.wait_for_timeout(1500)
        if popup_page is not None:
            popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
            return popup_page
        picked = self._pick_learning_page(page.context.pages, before_pages)
        if picked is not None and picked != page:
            return picked
        return None

    def _extract_nonquiz_incomplete_lesson_count(self, classroom_page: Page) -> Optional[int]:
        rows = self._extract_classroom_lesson_rows(classroom_page)
        if not rows:
            return None
        return sum(
            1
            for row in rows
            if bool(row.get("is_incomplete")) and not bool(row.get("is_inline_quiz")) and not bool(row.get("is_completed"))
        )

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

        incomplete_count = self._extract_nonquiz_incomplete_lesson_count(classroom_page)
        if incomplete_count is None:
            incomplete_count = len(re.findall(r"미완료", body))
        self._last_observed_course_progress_percent = int(current_percent)
        is_error_page = current_url.startswith("chrome-error://") or "ERR_" in body[:600]
        access_denied = self._is_classroom_access_denied(classroom_page)
        known = bool(progress_signal_seen) and not is_error_page and not access_denied
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
            if access_denied:
                self._log("강의실 접근 거부 상태 감지: '승인되지 않은 접근입니다.'")
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
            "access_denied": access_denied,
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
        try:
            page.locator('text=학습 차시').first.scroll_into_view_if_needed(timeout=2500)
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass
        self._prime_lesson_list_dom(page)
        return self._open_next_unfinished_lesson_popup(page)

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
            self._raise_if_stop_requested()
            progress = self._wait_for_player_page_progress(candidate_page, wait_ms=12000)
            if progress is None:
                # 학습창 내 iframe 또는 다른 팝업 페이지를 다시 탐색
                picked = self._find_page_with_progress(candidate_page.context.pages)
                if picked is not None:
                    candidate_page = picked
                    progress = self._wait_for_player_page_progress(candidate_page, wait_ms=12000)

            if progress is None:
                if self._is_inline_learning_quiz_page(candidate_page):
                    self._log("inline-quiz-detected: progress counter unavailable, quiz gate fallback")
                    acted = False
                    for _quiz_try in range(6):
                        if not self._handle_inline_quiz_gate(candidate_page):
                            break
                        acted = True
                        self._wait_page_with_stop(candidate_page, 900)
                    if acted:
                        continue
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
            self._log(f"내부 페이지 진행: {current_step}/{total_step}")
            lesson_step = self._extract_lesson_step_progress(candidate_page)
            if lesson_step is not None:
                self._log(f"차시 단계 상태: {lesson_step[0]}/{lesson_step[1]}")
            page_fingerprint = self._extract_lesson_content_fingerprint(candidate_page)

            # 학습평가/퀴즈 화면은 종합평가와 별개로 취급하며 차시 진행이 막힐 때만 최소 처리합니다.
            quiz_progressed = False
            quiz_acted = False
            for _quiz_try in range(6):
                if not self._handle_inline_quiz_gate(candidate_page):
                    break
                quiz_acted = True
                if self._wait_player_page_change(
                    candidate_page,
                    current_step,
                    total_step,
                    prev_fingerprint=page_fingerprint,
                ):
                    quiz_progressed = True
                    break
            if quiz_progressed:
                continue
            if quiz_acted:
                self._wait_page_with_stop(candidate_page, 800)
                continue

            active_lesson_step_no = lesson_step[0] if lesson_step is not None else 0
            blue_ready = True
            if active_lesson_step_no > 0:
                blue_ready = self._wait_until_step_blue(candidate_page, active_lesson_step_no, timeout_ms=240000)
            if not blue_ready:
                self._dump_player_debug(candidate_page, "step_not_blue")
                return LoginResult(
                    False,
                    f"차시 단계 파란색 완료 상태 확인 실패: lesson={active_lesson_step_no}",
                    candidate_page.url,
                )

            if current_step < total_step:
                self._log("내부 페이지 다음 클릭 전 5초 대기")
                self._wait_page_with_stop(candidate_page, step_wait_ms)
                self._log(f"nav-decision: mode=inline {self._describe_navigation_state(candidate_page)}")

                clicked = self._click_inline_page_next(candidate_page)
                if not clicked:
                    if self._is_inline_learning_quiz_page(candidate_page):
                        quiz_acted_after_inline = False
                        for _quiz_try in range(6):
                            if not self._handle_inline_quiz_gate(candidate_page):
                                break
                            quiz_acted_after_inline = True
                            if self._wait_player_page_change(
                                candidate_page,
                                current_step,
                                total_step,
                                prev_fingerprint=page_fingerprint,
                                prev_lesson_step=lesson_step,
                            ):
                                clicked = True
                                break
                        if clicked:
                            continue
                        if quiz_acted_after_inline:
                            self._wait_page_with_stop(candidate_page, 800)
                            continue
                    return LoginResult(
                        False,
                        f"'동그라미 >' 버튼 클릭 실패: {current_step}/{total_step}에서 중단",
                        candidate_page.url,
                    )

                progressed = self._wait_player_page_change(
                    candidate_page,
                    current_step,
                    total_step,
                    prev_fingerprint=page_fingerprint,
                    prev_lesson_step=lesson_step,
                )
                if progressed:
                    continue

                quiz_acted_after_next = False
                for _quiz_try in range(6):
                    if not self._handle_inline_quiz_gate(candidate_page):
                        break
                    quiz_acted_after_next = True
                    if self._wait_player_page_change(
                        candidate_page,
                        current_step,
                        total_step,
                        prev_fingerprint=page_fingerprint,
                        prev_lesson_step=lesson_step,
                    ):
                        progressed = True
                        break
                if progressed:
                    continue
                if quiz_acted_after_next:
                    self._wait_page_with_stop(candidate_page, 800)
                    continue

                switched_lesson = self._wait_next_lesson_loaded(
                    candidate_page,
                    prev_lesson_step=lesson_step,
                    prev_page_progress=progress,
                    timeout_ms=2500,
                )
                if switched_lesson:
                    self._dump_player_debug(candidate_page, "unexpected_lesson_switch_after_inline_next")
                    return LoginResult(
                        False,
                        (
                            "내부 페이지 버튼(nextPage)이 다음 차시/다음 단계로 넘어간 것으로 보입니다: "
                            f"page={current_step}/{total_step}"
                        ),
                        candidate_page.url,
                    )

                check = self._extract_player_page_progress(candidate_page)
                if check is None:
                    check = self._wait_for_player_page_progress(candidate_page, wait_ms=12000)
                    if check is None:
                        picked = self._find_page_with_progress(candidate_page.context.pages)
                        if picked is not None:
                            candidate_page = picked
                            check = self._wait_for_player_page_progress(candidate_page, wait_ms=12000)
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
                                "내부 페이지 클릭 후 카운터 미판독: "
                                f"{missing_progress_after_next}/3 재시도"
                            )
                            self._wait_page_with_stop(candidate_page, 6000)
                            continue
                        self._dump_player_debug(candidate_page, "progress_lost_after_next")
                        return LoginResult(False, "내부 페이지 클릭 후 카운터를 읽지 못했습니다.", candidate_page.url)
                if check[1] != total_step:
                    self._log(
                        "counter-source-mismatch: "
                        f"page-before={current_step}/{total_step} page-after={check[0]}/{check[1]}"
                    )
                    self._dump_player_debug(candidate_page, "counter_source_mismatch")
                if check[1] != total_step or check[0] <= current_step:
                    forced = self._force_next_step_transition(candidate_page)
                    if forced:
                        self._log("내부 페이지 무변화 감지: 강제 페이지 전환 함수를 호출했습니다.")
                        if self._wait_player_page_change(
                            candidate_page,
                            current_step,
                            total_step,
                            prev_fingerprint=page_fingerprint,
                        ):
                            continue
                    self._log("내부 페이지 증가가 없어 추가 대기/복구 후 재시도를 진행합니다.")
                    advanced = False
                    for retry_idx in range(3):
                        if self._is_inline_learning_quiz_page(candidate_page):
                            self._log(f"학습평가 재시도 {retry_idx + 1}/3")
                            self._handle_inline_quiz_gate(candidate_page)
                        else:
                            recovered = self._recover_red_step(candidate_page)
                            if recovered:
                                self._log(f"복구 클릭 후 재시도 {retry_idx + 1}/3")
                            else:
                                self._log(f"추가 대기 후 재시도 {retry_idx + 1}/3")
                        self._wait_page_with_stop(candidate_page, step_wait_ms)
                        self._log(f"nav-decision: mode=inline-retry {self._describe_navigation_state(candidate_page)}")
                        reclicked = self._click_inline_page_next(candidate_page)
                        if not reclicked:
                            continue
                        reprogress = self._wait_player_page_change(
                            candidate_page,
                            current_step,
                            total_step,
                            prev_fingerprint=page_fingerprint,
                            prev_lesson_step=lesson_step,
                        )
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
                            f"내부 페이지 클릭 후 증가하지 않았습니다: {current_step}/{total_step}",
                            candidate_page.url,
                        )
                continue

            if total_step <= 1 and current_step >= total_step:
                self._log(f"nav-decision: mode=final-single {self._describe_navigation_state(candidate_page)}")
                next_clicked = self._click_final_next_if_available(candidate_page)
                moved = (
                    self._wait_next_lesson_loaded(
                        candidate_page,
                        prev_lesson_step=lesson_step,
                        prev_page_progress=progress,
                    )
                    if next_clicked
                    else False
                )
                return LoginResult(
                    True,
                    f"차시 완료: {current_step}/{total_step}",
                    candidate_page.url,
                    next_lesson_clicked=moved,
                )
            if current_step >= total_step:
                lesson_total = lesson_step[1] if lesson_step is not None else 0
                all_blue = self._wait_all_steps_blue(candidate_page, lesson_total, timeout_ms=180000) if lesson_total > 0 else True
                if not all_blue:
                    self._log("모든 단계 파란색 완료 확인 실패 (화면 상태 점검 필요)")
                    # 빨간 단계가 남아있으면 해당 단계를 먼저 재생합니다.
                    recovered = self._recover_red_step(candidate_page)
                    if recovered:
                        self._log("빨간 단계를 우선 재생하기 위해 해당 단계로 이동합니다.")
                        self._wait_page_with_stop(candidate_page, 1500)
                        continue
                # 파란색 집계가 흔들려도 실제 완료 반영을 위해 마지막 Next는 항상 시도합니다.
                self._log(f"nav-decision: mode=final {self._describe_navigation_state(candidate_page)}")
                next_clicked = self._click_final_next_if_available(candidate_page)
                if not next_clicked:
                    self._log(f"nav-decision: mode=fallback-next {self._describe_navigation_state(candidate_page)}")
                    next_clicked = self._click_next_button(candidate_page, final_stage_only=True)
                moved = (
                    self._wait_next_lesson_loaded(
                        candidate_page,
                        prev_lesson_step=lesson_step,
                        prev_page_progress=progress,
                    )
                    if next_clicked
                    else False
                )
                return LoginResult(
                    True,
                    f"차시 완료: {current_step}/{total_step}",
                    candidate_page.url,
                    next_lesson_clicked=moved,
                )

        return LoginResult(False, "최대 클릭 횟수를 초과했습니다.", candidate_page.url)

    def _force_next_step_transition(self, page: Page) -> bool:
        scopes: list[Any] = list(page.frames) + [page]
        for scope in scopes:
            try:
                result = scope.evaluate(
                    """
                    () => {
                      const clickAny = (el) => {
                        if (!el) return false;
                        try { el.click(); return true; } catch (e) {}
                        try {
                          el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                          return true;
                        } catch (e) {}
                        return false;
                      };

                      const direct = document.querySelector(
                        '#nextPage, a#nextPage, #nextBtn a.next, a[onclick*="doNext"], a[onclick*="nextPage"], button.nextPage'
                      );
                      if (clickAny(direct)) return { ok: true, mode: 'direct-click' };

                      if (typeof nextPage === 'function') {
                        nextPage();
                        return { ok: true, mode: 'fn-nextPage' };
                      }
                      if (typeof doNext === 'function') {
                        doNext();
                        return { ok: true, mode: 'fn-doNext' };
                      }
                      if (window.SUB && typeof window.SUB.doPage === 'function') {
                        window.SUB.doPage(1);
                        return { ok: true, mode: 'fn-SUB.doPage' };
                      }
                      if (window.parent && window.parent.SUB && typeof window.parent.SUB.doPage === 'function') {
                        window.parent.SUB.doPage(1);
                        return { ok: true, mode: 'fn-parent.SUB.doPage' };
                      }
                      if (window.parent && window.parent.RES && typeof window.parent.RES.doPage === 'function') {
                        window.parent.RES.doPage(1);
                        return { ok: true, mode: 'fn-parent.RES.doPage' };
                      }
                      if (window.top && window.top.RES && typeof window.top.RES.doPage === 'function') {
                        window.top.RES.doPage(1);
                        return { ok: true, mode: 'fn-top.RES.doPage' };
                      }
                      if (window.parent && typeof window.parent.doPage === 'function') {
                        window.parent.doPage(1);
                        return { ok: true, mode: 'fn-parent.doPage' };
                      }
                      return { ok: false, mode: 'none' };
                    }
                    """
                )
                if isinstance(result, dict) and result.get("ok"):
                    mode = str(result.get("mode", "unknown"))
                    self._log(f"강제 페이지 전환 호출 성공: {mode}")
                    page.wait_for_timeout(700)
                    return True
            except Exception:  # noqa: BLE001
                continue
        if self._click_round_next_with_pageinfo(page, timeout_ms=1800):
            return True
        return False

    def _extract_player_page_progress(self, page: Page) -> Optional[tuple[int, int]]:
        scopes: list[Any] = [page] + list(page.frames)
        merged: list[tuple[int, int, int]] = []
        for idx, scope in enumerate(scopes):
            merged.extend(
                self._extract_player_page_progress_from_scope(
                    scope,
                    scope_rank=(30 if idx > 0 else 0),
                )
            )

        if not merged:
            return None
        merged.sort(key=lambda x: (x[2], x[1], x[0]), reverse=True)
        return merged[0][0], merged[0][1]

    @staticmethod
    def _extract_player_page_progress_from_scope(scope: Any, scope_rank: int = 0) -> list[tuple[int, int, int]]:
        candidates: list[tuple[int, int, int]] = []

        def _push(parsed: Optional[tuple[int, int]], score: int) -> None:
            if parsed is None:
                return
            candidates.append((parsed[0], parsed[1], score + scope_rank))

        has_inline_next = False
        try:
            has_inline_next = bool(
                scope.evaluate(
                    """
                    () => !!document.querySelector(
                      'button.nextPage.movePage, button.nextPage, a.nextPage, #nextPage, a#nextPage'
                    )
                    """
                )
            )
        except Exception:  # noqa: BLE001
            has_inline_next = False

        # 우선 명시적 플레이어 페이지 카운터 셀렉터를 탐지합니다.
        try:
            page_info_loc = scope.locator("#pageInfoDiv").first
            if page_info_loc.count() > 0:
                page_info_text = page_info_loc.inner_text(timeout=1200).strip()
                parsed = EKHNPAutomator._parse_page_info_counter(page_info_text)
                _push(parsed, 320 if has_inline_next else 280)
        except Exception:  # noqa: BLE001
            pass

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
                        _push((c, t), 340 if has_inline_next else 300)
            except Exception:  # noqa: BLE001
                pass

        try:
            page_box_texts = scope.evaluate(
                """
                () => {
                  const normalize = (txt) => String(txt || '').replace(/\\s+/g, ' ').trim();
                  const nodes = Array.from(
                    document.querySelectorAll(
                      '#pageInfoDiv, .pageBox, .right.pageBox, .middle_pageBox, [class*="pageBox" i]'
                    )
                  );
                  return nodes
                    .map((el) => normalize(el.innerText || el.textContent || ''))
                    .filter((txt) => !!txt);
                }
                """
            )
        except Exception:  # noqa: BLE001
            page_box_texts = []

        if isinstance(page_box_texts, list):
            for raw_text in page_box_texts:
                parsed = EKHNPAutomator._parse_page_info_counter(str(raw_text or ""))
                _push(parsed, 260 if has_inline_next else 220)
        return candidates

    def _extract_step_progress(self, page: Page) -> Optional[tuple[int, int]]:
        return self._extract_lesson_step_progress(page)

    def _extract_lesson_step_progress(self, page: Page) -> Optional[tuple[int, int]]:
        info_frame = self._find_info_bar_frame(page)
        if info_frame is None:
            return None
        try:
            result = info_frame.evaluate(
                """
                () => {
                  const nodes = Array.from(new Set(document.querySelectorAll('#frameTable .frameTd, [id^="frame"]')));
                  const filtered = nodes.filter((el) => {
                    const id = String(el.id || '');
                    const txt = String(el.textContent || '').trim();
                    return /^frame\\d+$/.test(id) || /^\\d{1,3}$/.test(txt);
                  });
                  const total = filtered.length;
                  if (!total) return null;
                  let current = filtered.filter((el) => /frameOn/.test(String(el.className || ''))).length;
                  if (current <= 0) {
                    const red = filtered.findIndex((el) => /frameRed/.test(String(el.className || '')));
                    if (red >= 0) current = red + 1;
                  }
                  if (current <= 0) current = 1;
                  if (current > total) current = total;
                  return { current, total };
                }
                """
            )
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(result, dict):
            return None
        try:
            current = int(result.get("current", 0))
            total = int(result.get("total", 0))
        except Exception:  # noqa: BLE001
            return None
        if total < 1 or current < 1 or current > total:
            return None
        return current, total

    def _wait_for_player_page_progress(self, page: Page, wait_ms: int = 8000) -> Optional[tuple[int, int]]:
        ticks = max(1, wait_ms // 500)
        for _ in range(ticks):
            self._raise_if_stop_requested()
            found = self._extract_player_page_progress(page)
            if found is not None:
                return found
            self._wait_page_with_stop(page, 500)
        return None

    def _extract_lesson_content_fingerprint(self, page: Page) -> str:
        parts: list[str] = []
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            try:
                scope_url = str(getattr(scope, "url", "") or "")
            except Exception:  # noqa: BLE001
                scope_url = ""
            if "infoBar.do" in scope_url:
                continue
            try:
                body = scope.locator("body").inner_text(timeout=1200)
            except Exception:  # noqa: BLE001
                continue
            text = str(body or "").strip()
            if not text:
                continue
            text = re.sub(r"\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b", " ", text)
            text = re.sub(r"\b\d{1,3}\s*/\s*\d{1,3}\b", " ", text)
            text = re.sub(r"\b(?:다음|이전|next|prev|close|닫기)\b", " ", text, flags=re.IGNORECASE)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= 24:
                parts.append(text[:320])
            if len(parts) >= 3:
                break
        if not parts:
            return ""
        joined = " | ".join(parts)
        return hashlib.sha1(joined.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]

    def _wait_player_page_change(
        self,
        page: Page,
        prev_cur: int,
        prev_total: int,
        *,
        prev_fingerprint: str = "",
        prev_lesson_step: Optional[tuple[int, int]] = None,
    ) -> bool:
        for _ in range(16):
            self._wait_page_with_stop(page, 500)
            lesson_now = self._extract_lesson_step_progress(page)
            now = self._extract_player_page_progress(page)
            if now is None:
                current_fp = self._extract_lesson_content_fingerprint(page)
                if prev_fingerprint and current_fp and current_fp != prev_fingerprint:
                    if prev_lesson_step is None or lesson_now == prev_lesson_step:
                        return True
                continue
            cur, total = now
            if total != prev_total:
                continue
            if total == prev_total and cur > prev_cur:
                return True
            if total == prev_total and cur >= total and prev_cur < prev_total:
                return True
        return False

    @staticmethod
    def _parse_page_info_counter(text: str) -> Optional[tuple[int, int]]:
        raw = str(text or "").strip()
        if not raw:
            return None
        match = re.search(r"(\d{1,3})\s*/\s*(\d{1,3})", raw)
        if not match:
            return None
        try:
            cur = int(match.group(1))
            total = int(match.group(2))
        except Exception:  # noqa: BLE001
            return None
        if total < 1 or cur < 1 or cur > total:
            return None
        return cur, total

    def _collect_page_info_locators(self, page: Page) -> list[Any]:
        locators: list[Any] = []
        for scope in [page] + list(page.frames):
            try:
                loc = scope.locator("#pageInfoDiv").first
                if loc.count() > 0:
                    locators.append(loc)
            except Exception:  # noqa: BLE001
                continue
        return locators

    @staticmethod
    def _read_first_nonempty_locator_text(locators: list[Any], timeout_ms: int = 1000) -> str:
        for loc in locators:
            try:
                text = loc.inner_text(timeout=timeout_ms).strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                return text
        return ""

    @staticmethod
    def _format_progress_tuple(progress: Optional[tuple[int, int]]) -> str:
        if progress is None:
            return "-"
        return f"{int(progress[0])}/{int(progress[1])}"

    def _describe_navigation_state(self, page: Page) -> str:
        page_progress = self._extract_player_page_progress(page)
        lesson_progress = self._extract_lesson_step_progress(page)
        page_info_text = self._read_first_nonempty_locator_text(self._collect_page_info_locators(page), timeout_ms=300)

        inline_selectors = [
            "button.nextPage.movePage",
            "button.nextPage",
            "a.nextPage",
            "button[title*='다음페이지']",
            "button[aria-label*='다음페이지']",
            "a#nextPage",
            "#nextPage",
            "#nextPage i",
            "#nextPage img",
            ".lwd_bar .page a#nextPage",
            ".lwd_bar .page a:has(i.arrow_right)",
            ".lwd_bar .page a:has(i.sk_next)",
        ]
        final_selectors = [
            '#nextBtn a.next:has-text("다음")',
            '#nextBtn a.next',
            'a.next:has-text("다음")',
            'a.next:has-text(">")',
            'a.next:has-text("›")',
            '[aria-label*="다음"]',
            '[aria-label*="next"]',
            'a[onclick*="doNext"]',
        ]

        inline_visible = "none"
        for frame_idx, scope in enumerate(page.frames):
            selector = self._find_first_visible_selector(scope, inline_selectors, max_items=6)
            if selector:
                inline_visible = f"frame{frame_idx}:{selector}"
                break
        if inline_visible == "none" and page_info_text:
            inline_visible = f"pageInfo:{page_info_text}"

        final_selector = self._find_first_visible_selector(page, final_selectors, max_items=8)
        final_visible = f"page:{final_selector}" if final_selector else "none"

        return (
            f"page={self._format_progress_tuple(page_progress)} "
            f"lesson={self._format_progress_tuple(lesson_progress)} "
            f"pageInfo={page_info_text or '-'} "
            f"inlineNext={inline_visible} "
            f"finalNext={final_visible}"
        )

    def _click_round_next_with_pageinfo(self, page: Page, timeout_ms: int = 3500) -> bool:
        # 우하단 동그라미 화살표를 클릭하고 pageInfoDiv 증가가 확인될 때만 성공 처리합니다.
        # pageInfoDiv와 버튼이 서로 다른 frame에 있을 수 있어, 읽기/클릭 범위를 분리합니다.
        page_info_locators = self._collect_page_info_locators(page)
        if not page_info_locators:
            return False

        before_text = self._read_first_nonempty_locator_text(page_info_locators, timeout_ms=1200)
        before_counter = self._parse_page_info_counter(before_text)

        def _wait_page_info_advance() -> Optional[str]:
            ticks = max(1, timeout_ms // 250)
            for _ in range(ticks):
                page.wait_for_timeout(250)
                after_text_local = self._read_first_nonempty_locator_text(page_info_locators, timeout_ms=900)
                if not after_text_local:
                    continue
                after_counter_local = self._parse_page_info_counter(after_text_local)
                if (
                    before_counter is not None
                    and after_counter_local is not None
                    and after_counter_local[1] == before_counter[1]
                    and after_counter_local[0] > before_counter[0]
                ):
                    return after_text_local
                if before_text and after_text_local != before_text:
                    return after_text_local
            return None

        scopes: list[Any] = [page] + list(page.frames)
        candidate_selectors = [
            "#nextPage",
            "a#nextPage",
            "button#nextPage",
            "a.nextPage",
            "button.nextPage",
            '[id*="nextpage" i]',
            '[class*="nextpage" i]',
            '[class*="arrow_right" i]',
            '[class*="sk_next" i]',
            '[class*="btn_next" i]',
            'a:has-text(">")',
            'button:has-text(">")',
            'a:has-text("›")',
            'button:has-text("›")',
            'a:has-text("»")',
            'button:has-text("»")',
            '[aria-label*="next" i]',
            '[title*="next" i]',
        ]

        for scope_idx, scope in enumerate(scopes):
            scope_name = "page" if scope_idx == 0 else f"frame{scope_idx - 1}"
            for selector in candidate_selectors:
                try:
                    next_btn = scope.locator(selector).first
                    if next_btn.count() <= 0:
                        continue
                    if not next_btn.is_visible():
                        continue
                except Exception:  # noqa: BLE001
                    continue

                clicked = False
                try:
                    next_btn.click(timeout=2000, no_wait_after=True)
                    clicked = True
                except Exception:  # noqa: BLE001
                    try:
                        next_btn.click(timeout=1200, force=True, no_wait_after=True)
                        clicked = True
                    except Exception:  # noqa: BLE001
                        clicked = False

                if not clicked:
                    continue

                advanced_text = _wait_page_info_advance()
                if advanced_text is not None:
                    self._log(
                        "round-next-clicked: "
                        f"source=pageinfo selector={selector} scope={scope_name} "
                        f"pageInfoDiv {before_text} -> {advanced_text}"
                    )
                    return True

            # 마지막 폴백: 우하단(원형 화살표 영역)에서 right-arrow 후보를 좌표 기반으로 클릭
            try:
                clicked = bool(
                    scope.evaluate(
                        """
                        () => {
                          const norm = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                          const isVisible = (el) => {
                            if (!el || !el.getBoundingClientRect) return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                          };
                          const isInteractive = (el) => {
                            if (!el) return false;
                            const tag = (el.tagName || '').toLowerCase();
                            if (['a', 'button', 'input', 'summary'].includes(tag)) return true;
                            if ((el.getAttribute('role') || '').toLowerCase() === 'button') return true;
                            if (el.hasAttribute('onclick')) return true;
                            if ((el.getAttribute('href') || '').length > 0) return true;
                            return false;
                          };
                          const pickTarget = (el) => (
                            el.closest('a,button,input,[role=\"button\"],[onclick],[href],#nextPage,.nextPage,.btn_next') || el
                          );
                          const nodes = Array.from(document.querySelectorAll('a,button,input,div,span,i,img,svg,use'));
                          const vw = Math.max(window.innerWidth || 0, document.documentElement.clientWidth || 0);
                          const vh = Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0);
                          let best = null;
                          for (const n of nodes) {
                            if (!isVisible(n)) continue;
                            const t = pickTarget(n);
                            if (!isVisible(t)) continue;
                            const attrs = [
                              n.textContent, n.getAttribute && n.getAttribute('alt'),
                              n.getAttribute && n.getAttribute('title'),
                              n.getAttribute && n.getAttribute('aria-label'),
                              n.getAttribute && n.getAttribute('class'),
                              n.getAttribute && n.getAttribute('id'),
                              t.textContent, t.getAttribute && t.getAttribute('title'),
                              t.getAttribute && t.getAttribute('aria-label'),
                              t.getAttribute && t.getAttribute('class'),
                              t.getAttribute && t.getAttribute('id'),
                            ].map(norm).join(' ');
                            if (attrs.includes('prev') || attrs.includes('이전') || attrs.includes('left')) continue;
                            let score = 0;
                            if (attrs.includes('next') || attrs.includes('다음')) score += 60;
                            if (attrs.includes('nextpage') || attrs.includes('arrow_right') || attrs.includes('btn_next')) score += 60;
                            if (attrs.includes('>') || attrs.includes('›') || attrs.includes('»') || attrs.includes('→')) score += 35;
                            const r = t.getBoundingClientRect();
                            const cx = r.left + r.width * 0.5;
                            const cy = r.top + r.height * 0.5;
                            if (vw > 0 && cx >= vw * 0.65) score += 35;
                            if (vh > 0 && cy >= vh * 0.70) score += 35;
                            if (!isInteractive(t) && score < 120) continue;
                            if (!best || score > best.score) best = { el: t, score };
                          }
                          // 텍스트/속성 신호가 약한 경우(아이콘만 있는 플레이어)엔
                          // 우하단 원형 버튼군에서 가장 오른쪽 버튼을 next로 간주합니다.
                          if (!best) {
                            const roundLike = nodes
                              .map((n) => {
                                const t = pickTarget(n);
                                if (!t || !isVisible(t)) return null;
                                const r = t.getBoundingClientRect();
                                if (r.width < 24 || r.height < 24 || r.width > 96 || r.height > 96) return null;
                                const style = window.getComputedStyle(t);
                                const br = norm(style && style.borderRadius);
                                const circular =
                                  br.includes('50%') ||
                                  br.includes('9999') ||
                                  Math.abs(r.width - r.height) <= Math.max(3, r.width * 0.15);
                                if (!circular) return null;
                                const cx = r.left + r.width * 0.5;
                                const cy = r.top + r.height * 0.5;
                                if (vw > 0 && cx < vw * 0.58) return null;
                                if (vh > 0 && cy < vh * 0.62) return null;
                                if (!isInteractive(t) && !isInteractive(n)) return null;
                                return { el: t, cx, cy };
                              })
                              .filter(Boolean);
                            if (roundLike.length > 0) {
                              roundLike.sort((a, b) => (b.cx - a.cx) || (b.cy - a.cy));
                              best = { el: roundLike[0].el, score: 100 };
                            }
                          }
                          if (!best || best.score < 95) return false;
                          const el = best.el;
                          try { el.click(); return true; } catch (e) {}
                          try {
                            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            return true;
                          } catch (e) {}
                          return false;
                        }
                        """
                    )
                )
            except Exception:  # noqa: BLE001
                clicked = False

            if clicked:
                advanced_text = _wait_page_info_advance()
                if advanced_text is not None:
                    self._log(
                        "round-next-clicked: "
                        f"source=pageinfo-roundlike scope={scope_name} "
                        f"pageInfoDiv {before_text} -> {advanced_text}"
                    )
                    return True
        return False

    def _click_inline_page_next(self, page: Page) -> bool:
        if self._click_round_next_with_pageinfo(page, timeout_ms=2600):
            page.wait_for_timeout(500)
            return True

        for frame_idx, scope in enumerate(page.frames):
            trusted_selectors = [
                "button.nextPage.movePage",
                "button.nextPage",
                "a.nextPage",
                "button[title*='다음페이지']",
                "button[aria-label*='다음페이지']",
                "a#nextPage",
                "#nextPage",
                "#nextPage i",
                "#nextPage img",
                ".lwd_bar .page a#nextPage",
                ".lwd_bar .page a:has(i.arrow_right)",
                ".lwd_bar .page a:has(i.sk_next)",
            ]
            clicked_selector = self._click_first_visible_with_selector(scope, trusted_selectors, max_items=10)
            if clicked_selector:
                self._log(
                    "round-next-clicked: "
                    f"source=trusted-selector scope=frame{frame_idx} selector={clicked_selector}"
                )
                page.wait_for_timeout(700)
                return True
            try:
                clicked = scope.evaluate(
                    """
                    () => {
                        const clickAny = (el) => {
                          if (!el) return false;
                          try { el.click(); return true; } catch (e) {}
                          try {
                            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            return true;
                          } catch (e) {}
                          return false;
                        };
                        const btn = document.querySelector(
                          'button.nextPage.movePage, button.nextPage, a.nextPage, #nextPage, a#nextPage'
                        );
                        if (clickAny(btn)) return true;
                        if (typeof nextPage === 'function') {
                          nextPage();
                          return true;
                        }
                        if (window.parent && window.parent.RES && typeof window.parent.RES.doPage === 'function') {
                          window.parent.RES.doPage(1);
                          return true;
                        }
                        return false;
                    }
                    """
                )
            except Exception:  # noqa: BLE001
                clicked = False
            if clicked:
                self._log(f"round-next-clicked: source=scripted-fallback scope=frame{frame_idx}")
                page.wait_for_timeout(700)
                return True
        return False

    def _click_next_button(self, page: Page, *, final_stage_only: bool = False) -> bool:
        scopes: list[Any] = [page] + list(page.frames)
        log_prefix = "generic-final-next-clicked" if final_stage_only else "generic-next-clicked"

        if not final_stage_only and self._click_round_next_with_pageinfo(page, timeout_ms=2600):
            page.wait_for_timeout(500)
            return True

        for scope_idx, scope in enumerate(scopes):
            scope_name = "page" if scope_idx == 0 else f"frame{scope_idx - 1}"
            if final_stage_only:
                selectors = [
                    '#nextBtn a.next',
                    'a.next:has-text("다음")',
                    'button.next:has-text("다음")',
                    'a:has-text("다음 차시")',
                    'button:has-text("다음 차시")',
                    'a:has-text("다음")',
                    'button:has-text("다음")',
                    'a:has-text("Next")',
                    'button:has-text("Next")',
                    'a:has-text("계속")',
                    'button:has-text("계속")',
                    'a[onclick*="doNext"]',
                    'button[onclick*="doNext"]',
                    '[role="button"]:has-text("다음")',
                    '[aria-label*="다음"]',
                    '[aria-label*="next"]',
                    '[aria-label*="continue" i]',
                ]
            else:
                selectors = [
                    "a.next",
                    "button.next",
                    "a.nextPage",
                    "button.nextPage",
                    'a:has-text("다음")',
                    'button:has-text("다음")',
                    'a:has-text("Next")',
                    'button:has-text("Next")',
                    'a:has-text(">")',
                    'button:has-text(">")',
                    'a:has-text("›")',
                    'button:has-text("›")',
                    'a:has-text("»")',
                    'button:has-text("»")',
                    'a:has-text("→")',
                    'button:has-text("→")',
                    'a#nextPage',
                    '#nextPage',
                    '#nextPage img',
                    '#nextPage i',
                    '.lwd_bar .page a#nextPage',
                    '.lwd_bar .page a:has(i.arrow_right)',
                    '.lwd_bar .page a:has(i.sk_next)',
                    '.lwd_bar .page a:has(img[src*="next" i])',
                    '.lwd_bar .page a:has(img[src*="arrow_right" i])',
                    '#nextBtn a.next',
                    'a[onclick*="doNext"]',
                    'a[onclick*="nextPage"]',
                    'img[alt*="next" i]',
                    'img[alt*="다음" i]',
                    'img[src*="btn_next" i]',
                    'img[src*="arrow_right" i]',
                    '[class*="arrow_right" i]',
                    '[class*="arrow-right" i]',
                    '[class*="chevron_right" i]',
                    '[class*="chevron-right" i]',
                    '[class*="next" i][role="button"]',
                    '[title*="next" i]',
                    '[title*="다음" i]',
                    'a:has-text("다음 차시")',
                    'button:has-text("다음 차시")',
                    'a:has-text("다음목차")',
                    'button:has-text("다음목차")',
                    'a:has-text("다음 문항")',
                    'button:has-text("다음 문항")',
                    'a:has-text("계속")',
                    'button:has-text("계속")',
                    '[role="button"]:has-text("다음")',
                    '[aria-label*="다음"]',
                    '[aria-label*="next"]',
                    '[aria-label*="continue" i]',
                    'span:has-text("다음")',
                    'div:has-text("다음")',
                ]

            clicked_selector = self._click_first_visible_with_selector(scope, selectors, max_items=25)
            if clicked_selector:
                self._log(f"{log_prefix}: scope={scope_name} selector={clicked_selector}")
                page.wait_for_timeout(600)
                return True

            if not final_stage_only and self._click_next_arrow_like(scope):
                self._log(f"{log_prefix}: scope={scope_name} source=arrow-like")
                page.wait_for_timeout(600)
                return True

            clicked = scope.evaluate(
                """
                (finalStageOnly) => {
                    const nodes = Array.from(document.querySelectorAll('a,button,span,div,[role="button"]'));
                    const visibles = nodes.filter((n) => {
                      const txt = (n.textContent || '').trim().toLowerCase();
                      const aria = String(n.getAttribute('aria-label') || n.getAttribute('title') || '').trim().toLowerCase();
                      const pass = finalStageOnly
                        ? (txt === '다음' || txt === 'next' || txt === '다음 차시' || txt === '계속')
                        : (txt === '다음' || txt === 'next' || txt === '>' || txt === '›' || txt === '＞' || txt === '→');
                      if (!pass && !aria.includes('next') && !aria.includes('다음') && !aria.includes('continue')) return false;
                      const r = n.getBoundingClientRect();
                      return r.width > 0 && r.height > 0;
                    });
                    if (!visibles.length) return false;
                    visibles.sort((a, b) => b.getBoundingClientRect().y - a.getBoundingClientRect().y);
                    visibles[0].click();
                    return true;
                }
                """,
                final_stage_only,
            )
            if clicked:
                self._log(f"{log_prefix}: scope={scope_name} source=bottom-text-fallback")
                page.wait_for_timeout(600)
                return True

            if not final_stage_only:
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
                    self._log(f"{log_prefix}: scope={scope_name} source=controller-nextPage")
                    page.wait_for_timeout(600)
                    return True
                if self._click_next_arrow_like(scope):
                    self._log(f"{log_prefix}: scope={scope_name} source=arrow-like-2")
                    page.wait_for_timeout(600)
                    return True

        if not final_stage_only and self._click_round_next_with_pageinfo(page):
            page.wait_for_timeout(500)
            return True
        return False

    def _wait_progress_change(self, page: Page, prev_cur: int, prev_total: int) -> bool:
        return self._wait_player_page_change(page, prev_cur, prev_total)

    def _is_inline_learning_quiz_page(self, page: Page) -> bool:
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            try:
                url = str(getattr(scope, "url", "") or "").lower()
            except Exception:  # noqa: BLE001
                url = ""
            if "assessment" in url or "quiz" in url:
                return True
            try:
                detected = bool(
                    scope.evaluate(
                        """
                        () => {
                          const root = document.querySelector('.quiz.appraisal, .quiz, .appraisal');
                          if (root) return true;
                          return !!document.querySelector('button.next_btn, button.result_btn, button.confirm, .confirm.appraisal');
                        }
                        """
                    )
                )
            except Exception:  # noqa: BLE001
                detected = False
            if detected:
                return True
        return False

    def _handle_inline_quiz_gate(self, page: Page) -> bool:
        scopes: list[Any] = [page] + list(page.frames)
        for scope in scopes:
            try:
                result = scope.evaluate(
                    """
                    () => {
                      const norm = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const isVisible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const style = window.getComputedStyle(el);
                        if (!style) return false;
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                          return false;
                        }
                        if (style.pointerEvents === 'none') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const clickAny = (el) => {
                        if (!el || !isVisible(el)) return false;
                        try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                        try { el.click(); return true; } catch (e) {}
                        try {
                          el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                          return true;
                        } catch (e) {}
                        return false;
                      };

                      const quizRoot = Array.from(document.querySelectorAll('.quiz.appraisal, .quiz, .appraisal'))
                        .find((el) => isVisible(el));
                      if (!quizRoot) return { detected: false, acted: false, mode: '' };

                      // 1) 피드백 상태면 다음문제/결과보기 우선
                      const feedbackBtns = Array.from(
                        quizRoot.querySelectorAll('button.next_btn, button.result_btn, .feedback .next_btn, .feedback .result_btn')
                      ).filter((el) => isVisible(el));
                      if (feedbackBtns.length > 0) {
                        const picked = feedbackBtns[0];
                        if (clickAny(picked)) {
                          const cls = norm(picked.className || '');
                          return {
                            detected: true,
                            acted: true,
                            mode: cls.includes('result_btn') ? 'quiz-result-btn' : 'quiz-next-btn'
                          };
                        }
                        return { detected: true, acted: false, mode: 'quiz-feedback-visible' };
                      }

                      // 2) 현재 보이는 문제에서 선택지 하나 선택
                      const pages = Array.from(quizRoot.querySelectorAll('.page'));
                      const activePage = pages.find((el) => isVisible(el)) || quizRoot;
                      const options = Array.from(
                        activePage.querySelectorAll('li.multiple, li.choice, .example li, [role=\"option\"]')
                      ).filter((el) => isVisible(el));
                      let selected = options.find((el) => {
                        const cls = norm(el.className || '');
                        return cls.includes('on') || cls.includes('selected') || cls.includes('active') || cls.includes('toggle');
                      });
                      if (!selected && options.length > 0) {
                        if (clickAny(options[0])) {
                          selected = options[0];
                        }
                      }

                      // 3) 정답확인 버튼 클릭
                      const confirmBtn = Array.from(
                        activePage.querySelectorAll('button.confirm, .confirm.appraisal, .confirm')
                      ).find((el) => isVisible(el));
                      if (confirmBtn && clickAny(confirmBtn)) {
                        return {
                          detected: true,
                          acted: true,
                          mode: selected ? 'quiz-select+confirm' : 'quiz-confirm'
                        };
                      }

                      if (selected) return { detected: true, acted: true, mode: 'quiz-select' };
                      return { detected: true, acted: false, mode: 'quiz-visible' };
                    }
                    """
                )
            except Exception:  # noqa: BLE001
                continue

            if isinstance(result, dict) and bool(result.get("detected")):
                mode = str(result.get("mode", "quiz-detected"))
                self._log(f"inline-quiz-detected: {mode}")
            if isinstance(result, dict) and bool(result.get("acted")):
                mode = str(result.get("mode", "quiz-action"))
                self._log(f"inline-quiz-advanced: {mode}")
                page.wait_for_timeout(700)
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
            self._raise_if_stop_requested()
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
            self._wait_page_with_stop(page, 500)
        return False

    def _wait_all_steps_blue(self, page: Page, total_step: int, timeout_ms: int = 45000) -> bool:
        info_frame = self._find_info_bar_frame(page)
        if info_frame is None:
            return True

        ticks = max(1, timeout_ms // 500)
        for _ in range(ticks):
            self._raise_if_stop_requested()
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
            self._wait_page_with_stop(page, 500)
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
        clicked_selector = self._click_first_visible_with_selector(page, selectors, max_items=10)
        clicked = bool(clicked_selector)
        source = f"selector={clicked_selector}" if clicked_selector else ""
        if not clicked:
            clicked = self._click_next_arrow_like(page)
            if clicked:
                source = "arrow-like"
        if clicked:
            self._log(f"final-next-clicked: source={source or 'unknown'}")
            page.wait_for_timeout(1200)
        return clicked

    def _wait_next_lesson_loaded(
        self,
        page: Page,
        prev_lesson_step: Optional[tuple[int, int]] = None,
        prev_page_progress: Optional[tuple[int, int]] = None,
        timeout_ms: int = 15000,
    ) -> bool:
        ticks = max(1, timeout_ms // 500)
        prev_step_cur = int(prev_lesson_step[0]) if prev_lesson_step else 0
        prev_step_total = int(prev_lesson_step[1]) if prev_lesson_step else 0
        prev_page_cur = int(prev_page_progress[0]) if prev_page_progress else 0
        prev_page_total = int(prev_page_progress[1]) if prev_page_progress else 0
        prev_fingerprint = self._extract_lesson_content_fingerprint(page)
        for _ in range(ticks):
            self._wait_page_with_stop(page, 500)
            lesson_progress = self._extract_lesson_step_progress(page)
            if lesson_progress is not None:
                cur, total = lesson_progress
                if prev_step_total > 0:
                    if cur == 1 and total >= 1 and prev_step_cur >= max(prev_step_total, 1):
                        return True
                    if prev_step_cur > 1 and cur == 1:
                        return True
                    if prev_step_cur > 0 and total == prev_step_total and cur < prev_step_cur:
                        return True
                elif cur == 1 and total >= 1:
                    return True
            page_progress = self._extract_player_page_progress(page)
            if page_progress is not None and prev_page_total > 0:
                cur_page, total_page = page_progress
                if cur_page == 1 and total_page >= 1 and prev_page_cur >= max(prev_page_total, 1):
                    return True
            current_fp = self._extract_lesson_content_fingerprint(page)
            if prev_fingerprint and current_fp and current_fp != prev_fingerprint:
                if (
                    lesson_progress is not None
                    and prev_lesson_step is not None
                    and lesson_progress != prev_lesson_step
                    and int(lesson_progress[0]) == 1
                ):
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
        exam_hints = ["exam", "test", "evaluation", "eval", "popup.do", "exampaper"]
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
            progress = self._extract_player_page_progress(pg)
            if progress is not None or self._is_inline_learning_quiz_page(pg):
                return pg
        for pg in reversed(pages):
            if "/learning/" in pg.url or "popup.do" in pg.url:
                return pg
        return None

    def _wait_for_step_progress(self, page: Page, wait_ms: int = 8000) -> Optional[tuple[int, int]]:
        return self._wait_for_player_page_progress(page, wait_ms=wait_ms)

    def _dump_player_debug(self, page: Page, tag: str) -> None:
        try:
            out = Path("artifacts") / "player_debug" / self._run_id
            out.mkdir(parents=True, exist_ok=True)
            safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", tag)
            first_saved_path = ""
            for idx, pg in enumerate(page.context.pages):
                ptag = f"{safe_tag}_page{idx}"
                try:
                    png_path = out / f"{ptag}.png"
                    pg.screenshot(path=str(png_path), full_page=True)
                    self._note_artifact(
                        png_path,
                        kind="player-debug-screenshot",
                        label=ptag,
                        metadata={"url": pg.url},
                    )
                    if not first_saved_path:
                        first_saved_path = png_path.as_posix()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    body = pg.locator("body").inner_text(timeout=3000)
                    txt_path = out / f"{ptag}.txt"
                    txt_path.write_text(f"URL={pg.url}\n\n{body}", encoding="utf-8")
                    self._note_artifact(
                        txt_path,
                        kind="player-debug-text",
                        label=ptag,
                        metadata={"url": pg.url},
                    )
                    if not first_saved_path:
                        first_saved_path = txt_path.as_posix()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    html_path = out / f"{ptag}.html"
                    html_path.write_text(pg.content(), encoding="utf-8")
                    self._note_artifact(
                        html_path,
                        kind="player-debug-html",
                        label=ptag,
                        metadata={"url": pg.url},
                    )
                    if not first_saved_path:
                        first_saved_path = html_path.as_posix()
                except Exception:  # noqa: BLE001
                    pass
                for fi, fr in enumerate(pg.frames):
                    try:
                        fbody = fr.locator("body").inner_text(timeout=2000)
                        ftag = f"{ptag}_frame{fi}"
                        ftxt_path = out / f"{ftag}.txt"
                        ftxt_path.write_text(f"URL={fr.url}\n\n{fbody}", encoding="utf-8")
                        self._note_artifact(
                            ftxt_path,
                            kind="player-debug-frame-text",
                            label=ftag,
                            metadata={"url": fr.url},
                        )
                        if not first_saved_path:
                            first_saved_path = ftxt_path.as_posix()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        ftag = f"{ptag}_frame{fi}"
                        fhtml_path = out / f"{ftag}.html"
                        fhtml_path.write_text(fr.content(), encoding="utf-8")
                        self._note_artifact(
                            fhtml_path,
                            kind="player-debug-frame-html",
                            label=ftag,
                            metadata={"url": fr.url},
                        )
                        if not first_saved_path:
                            first_saved_path = fhtml_path.as_posix()
                    except Exception:  # noqa: BLE001
                        pass
            self._note_artifact(
                out,
                kind="player-debug-dir",
                label=safe_tag,
                metadata={"tag": safe_tag, "run_id": self._run_id},
            )
            if first_saved_path:
                self._log(f"디버그 저장: {first_saved_path}")
            else:
                self._log(f"디버그 저장: {out.as_posix()}")
        except Exception:  # noqa: BLE001
            pass

    def _dump_exam_dom_debug(self, page: Page, tag: str) -> None:
        try:
            out = Path("artifacts") / "exam_dom_debug" / self._run_id / re.sub(r"[^a-zA-Z0-9_-]", "_", tag)
            out.mkdir(parents=True, exist_ok=True)
            stats: list[dict[str, Any]] = []
            scopes: list[tuple[str, Any]] = [("page", page)] + [(f"frame_{idx}", fr) for idx, fr in enumerate(page.frames)]
            for name, scope in scopes:
                try:
                    url = str(scope.url)
                except Exception:  # noqa: BLE001
                    url = ""
                try:
                    html_path = out / f"{name}.html"
                    html_path.write_text(scope.content(), encoding="utf-8")
                    self._note_artifact(html_path, kind="exam-dom-html", label=name, metadata={"url": url})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    body_text = scope.locator("body").inner_text(timeout=2000)
                    body_path = out / f"{name}_body.txt"
                    body_path.write_text(f"URL={url}\n\n{body_text}", encoding="utf-8")
                    self._note_artifact(body_path, kind="exam-dom-text", label=name, metadata={"url": url})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    quick = scope.evaluate(
                        """
                        () => ({
                          radioCount: document.querySelectorAll('input[name="choiceAnswers"], input[type="radio"], input[type="checkbox"]').length,
                          answerItemCount: document.querySelectorAll('a.answer-item, .answer-item, .answer-radio, li[id^="example-item-"], li.multiple, li.choice, .example li, .answers li, .question-answer li, label').length,
                          quizCount: document.querySelectorAll('.quiz_li[id^="que_"], .quiz_li').length,
                          visibleText: (document.body && (document.body.innerText || document.body.textContent || '') || '').replace(/\\s+/g, ' ').trim().slice(0, 1200),
                        })
                        """
                    )
                except Exception:  # noqa: BLE001
                    quick = {}
                stats.append({"scope": name, "url": url, "quick": quick})
            stats_path = out / "quick_stats.json"
            stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
            self._note_artifact(stats_path, kind="exam-dom-stats", label=tag)
            self._log(f"시험 DOM 디버그 저장: {stats_path.as_posix()}")
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
            "내부 페이지 버튼(nextPage)이 다음 차시/다음 단계로 넘어간 것으로 보입니다",
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

    def _write_lesson_rows_debug_snapshot(self, page: Page, tag: str) -> None:
        try:
            out = Path("artifacts") / "player_debug" / self._run_id
            out.mkdir(parents=True, exist_ok=True)
            safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", tag)
            rows = self._extract_classroom_lesson_rows(page)
            payload = {
                "saved_at": self._utc_now_iso(),
                "classroom_url": str(getattr(page, "url", "") or ""),
                "last_lesson_key": str(self._last_opened_lesson_key or ""),
                "last_lesson_title": str(self._last_opened_lesson_title or ""),
                "last_course_title": str(self._last_opened_course_title or ""),
                "rows": rows,
            }
            out_path = out / f"{safe_tag}_lesson_rows.json"
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._note_artifact(
                out_path,
                kind="lesson-rows-snapshot",
                label=safe_tag,
                metadata={"classroom_url": str(getattr(page, "url", "") or "")},
            )
            self._log(f"디버그 저장: {out_path.as_posix()}")
        except Exception:  # noqa: BLE001
            return

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

        target_classroom = self._refresh_classroom_page(target_classroom)
        recovered_page = self._start_learning_from_progress_panel(
            target_classroom,
            preferred_lesson_key=self._last_opened_lesson_key,
        )
        if recovered_page is None:
            self._dump_player_debug(target_classroom, "recover_classroom_reopen_failed")
            self._write_lesson_rows_debug_snapshot(target_classroom, "recover_classroom_reopen_failed")
            self._log("복구 실패: 강의실에서 학습창 재오픈을 못했습니다.")
            return None
        self._log("복구 성공: 이어 학습 팝업을 다시 열었습니다.")
        return recovered_page

    @staticmethod
    def _click_first_visible_with_selector(page: Any, selectors: list[str], max_items: int = 8) -> str:
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
                        return selector
                except Exception:  # noqa: BLE001
                    continue
        return ""

    @staticmethod
    def _find_first_visible_selector(page: Any, selectors: list[str], max_items: int = 8) -> str:
        for selector in selectors:
            locator = page.locator(selector)
            count = min(locator.count(), max_items)
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if item.is_visible():
                        return selector
                except Exception:  # noqa: BLE001
                    continue
        return ""

    @staticmethod
    def _click_first_visible(page: Any, selectors: list[str], max_items: int = 8) -> bool:
        return bool(EKHNPAutomator._click_first_visible_with_selector(page, selectors, max_items=max_items))

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
