from __future__ import annotations

from datetime import UTC, datetime
import html
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
import time
import traceback
from typing import Any, Callable
import uuid

import streamlit as st
import streamlit.components.v1 as components

from automation import EKHNPAutomator
from config import Settings
from task_queue import QueueCapacityError
from task_queue import TaskQueueManager
from task_queue import get_task_queue


LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
AUDIT_LOG_FILE = LOG_DIR / "security_audit.log"
RUNTIME_DIR = Path(".runtime")
RUNTIME_DIR.mkdir(exist_ok=True)
ACCESS_GUARD_FILE = RUNTIME_DIR / "access_guard.json"
ACCESS_GUARD_LOCK = threading.Lock()
ACCESS_GUARD = {"fail_count": 0, "lock_until": 0.0}
ADMIN_GUARD_FILE = RUNTIME_DIR / "admin_guard.json"
ADMIN_GUARD_LOCK = threading.Lock()
ADMIN_GUARD = {"fail_count": 0, "lock_until": 0.0}
SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|비밀번호)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)(access_code|접속코드)\s*[:=]\s*([^\s,;]+)"),
]
SESSION_LOG_MAX_LINES = 1500
ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _restrict_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        return


def append_log(message: str) -> None:
    timestamp = _utc_now_iso()
    line = f"[{timestamp}] {message}"
    logs = st.session_state.setdefault("logs", [])
    logs.append(line)
    if len(logs) > SESSION_LOG_MAX_LINES:
        st.session_state.logs = logs[-SESSION_LOG_MAX_LINES:]
    logfile = LOG_DIR / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.log"
    with logfile.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    _restrict_file_permissions(logfile)


def _is_client_local_time_enabled(settings: Settings) -> bool:
    return str(getattr(settings, "app_time_display_mode", "") or "").strip().lower() == "client-local"


def _render_client_localized_html(
    inner_html: str,
    *,
    height_px: int,
    scrolling: bool = False,
) -> None:
    if os.getenv("APP_TIME_DISPLAY_MODE", "client-local").strip().lower() != "client-local":
        st.markdown(inner_html, unsafe_allow_html=True)
        return
    block_id = f"localized-{uuid.uuid4().hex}"
    payload = f"""
<div id="{block_id}">{inner_html}</div>
<script>
(() => {{
  const root = document.getElementById({json.dumps(block_id)});
  if (!root) return;
  const pad = (n) => String(n).padStart(2, '0');
  const formatLocal = (iso) => {{
    try {{
      const d = new Date(String(iso || ''));
      if (Number.isNaN(d.getTime())) return String(iso || '');
      return `${{d.getFullYear()}}-${{pad(d.getMonth() + 1)}}-${{pad(d.getDate())}} ${{
        pad(d.getHours())
      }}:${{pad(d.getMinutes())}}:${{pad(d.getSeconds())}}`;
    }} catch (e) {{
      return String(iso || '');
    }}
  }};
  root.querySelectorAll('[data-utc]').forEach((node) => {{
    const iso = node.getAttribute('data-utc') || '';
    const value = formatLocal(iso);
    node.textContent = value;
    node.setAttribute('title', `${{value}} (${{
      Intl.DateTimeFormat().resolvedOptions().timeZone || 'local'
    }})`);
  }});
}})();
</script>
"""
    components.html(payload, height=max(36, int(height_px)), scrolling=scrolling)


def _split_log_line_timestamp(line: str) -> tuple[str, str]:
    raw = str(line or "")
    match = re.match(r"^\[([^\]]+)\]\s*(.*)$", raw)
    if not match:
        return "", raw
    return str(match.group(1)).strip(), str(match.group(2)).strip()


def _render_timestamp_span(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    escaped = html.escape(raw)
    if ISO_Z_RE.match(raw):
        return f"<span data-utc=\"{escaped}\">{escaped}</span>"
    return escaped


def _render_simple_html_table(
    rows: list[dict[str, Any]],
    *,
    columns: list[tuple[str, str]],
    height_px: int = 260,
    empty_text: str = "(데이터 없음)",
) -> None:
    if not rows:
        _render_client_localized_html(
            (
                "<div style='padding:10px; border:1px solid #d9d9d9; border-radius:6px; "
                "background:#fafafa; color:#666;'>"
                f"{html.escape(empty_text)}"
                "</div>"
            ),
            height_px=58,
            scrolling=False,
        )
        return

    header_html = "".join(
        f"<th style='text-align:left; padding:8px 10px; border-bottom:1px solid #d9d9d9;'>{html.escape(label)}</th>"
        for key, label in columns
    )
    body_rows: list[str] = []
    for row in rows:
        cell_html = []
        for key, _label in columns:
            value = row.get(key, "")
            text = _render_timestamp_span(str(value)) if isinstance(value, str) else html.escape(str(value))
            cell_html.append(
                "<td style='padding:8px 10px; border-bottom:1px solid #efefef; vertical-align:top;'>"
                f"{text}"
                "</td>"
            )
        body_rows.append("<tr>" + "".join(cell_html) + "</tr>")

    table_html = (
        "<div style='border:1px solid #d9d9d9; border-radius:6px; overflow:auto; background:#fff;'>"
        "<table style='width:100%; border-collapse:collapse; font-size:12px; line-height:1.45;'>"
        f"<thead><tr style='background:#fafafa;'>{header_html}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )
    _render_client_localized_html(table_html, height_px=height_px, scrolling=True)


def _is_access_code_enabled(settings: Settings) -> bool:
    return bool(settings.app_access_code or settings.app_access_code_hash)


def _verify_code(input_code: str, plain_code: str, code_hash: str) -> bool:
    candidate = input_code.strip()
    if code_hash:
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, code_hash)
    if plain_code:
        # compare_digest(str, str)는 비ASCII 입력에서 TypeError를 낼 수 있어 bytes 비교로 통일
        return hmac.compare_digest(candidate.encode("utf-8"), str(plain_code).encode("utf-8"))
    return True


def _verify_access_code(input_code: str, settings: Settings) -> bool:
    return _verify_code(input_code, settings.app_access_code, settings.app_access_code_hash)


def _load_access_guard_state() -> dict[str, float]:
    if not ACCESS_GUARD_FILE.exists():
        return {"fail_count": 0, "lock_until": 0.0}
    try:
        raw = json.loads(ACCESS_GUARD_FILE.read_text(encoding="utf-8"))
        fail_count = int(raw.get("fail_count", 0))
        lock_until = float(raw.get("lock_until", 0.0))
        return {"fail_count": max(0, fail_count), "lock_until": max(0.0, lock_until)}
    except Exception:  # noqa: BLE001
        return {"fail_count": 0, "lock_until": 0.0}


def _save_access_guard_state(state: dict[str, float]) -> None:
    ACCESS_GUARD_FILE.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
    _restrict_file_permissions(ACCESS_GUARD_FILE)


def _read_access_guard(now_ts: float) -> tuple[int, int]:
    with ACCESS_GUARD_LOCK:
        state = _load_access_guard_state()
        ACCESS_GUARD["fail_count"] = state["fail_count"]
        ACCESS_GUARD["lock_until"] = state["lock_until"]
        fail_count = int(state["fail_count"])
        lock_remaining = int(max(0, float(state["lock_until"]) - now_ts))
        return fail_count, lock_remaining


def _record_access_failure(now_ts: float, max_attempts: int, cooldown_sec: int) -> tuple[int, int]:
    with ACCESS_GUARD_LOCK:
        state = _load_access_guard_state()
        lock_until = float(state["lock_until"])
        if lock_until > now_ts:
            return 0, int(lock_until - now_ts)

        fail_count = int(state["fail_count"]) + 1
        if fail_count >= max_attempts:
            state["fail_count"] = 0
            state["lock_until"] = now_ts + cooldown_sec
            _save_access_guard_state(state)
            ACCESS_GUARD["fail_count"] = state["fail_count"]
            ACCESS_GUARD["lock_until"] = state["lock_until"]
            return 0, cooldown_sec

        state["fail_count"] = fail_count
        _save_access_guard_state(state)
        ACCESS_GUARD["fail_count"] = state["fail_count"]
        ACCESS_GUARD["lock_until"] = state["lock_until"]
        remaining_attempts = max_attempts - fail_count
        return remaining_attempts, 0


def _reset_access_guard() -> None:
    with ACCESS_GUARD_LOCK:
        state = {"fail_count": 0, "lock_until": 0.0}
        _save_access_guard_state(state)
        ACCESS_GUARD["fail_count"] = state["fail_count"]
        ACCESS_GUARD["lock_until"] = state["lock_until"]


def _load_admin_guard_state() -> dict[str, float]:
    if not ADMIN_GUARD_FILE.exists():
        return {"fail_count": 0, "lock_until": 0.0}
    try:
        raw = json.loads(ADMIN_GUARD_FILE.read_text(encoding="utf-8"))
        fail_count = int(raw.get("fail_count", 0))
        lock_until = float(raw.get("lock_until", 0.0))
        return {"fail_count": max(0, fail_count), "lock_until": max(0.0, lock_until)}
    except Exception:  # noqa: BLE001
        return {"fail_count": 0, "lock_until": 0.0}


def _save_admin_guard_state(state: dict[str, float]) -> None:
    ADMIN_GUARD_FILE.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
    _restrict_file_permissions(ADMIN_GUARD_FILE)


def _read_admin_guard(now_ts: float) -> tuple[int, int]:
    with ADMIN_GUARD_LOCK:
        state = _load_admin_guard_state()
        ADMIN_GUARD["fail_count"] = state["fail_count"]
        ADMIN_GUARD["lock_until"] = state["lock_until"]
        fail_count = int(state["fail_count"])
        lock_remaining = int(max(0, float(state["lock_until"]) - now_ts))
        return fail_count, lock_remaining


def _record_admin_failure(now_ts: float, max_attempts: int, cooldown_sec: int) -> tuple[int, int]:
    with ADMIN_GUARD_LOCK:
        state = _load_admin_guard_state()
        lock_until = float(state["lock_until"])
        if lock_until > now_ts:
            return 0, int(lock_until - now_ts)

        fail_count = int(state["fail_count"]) + 1
        if fail_count >= max_attempts:
            state["fail_count"] = 0
            state["lock_until"] = now_ts + cooldown_sec
            _save_admin_guard_state(state)
            ADMIN_GUARD["fail_count"] = state["fail_count"]
            ADMIN_GUARD["lock_until"] = state["lock_until"]
            return 0, cooldown_sec

        state["fail_count"] = fail_count
        _save_admin_guard_state(state)
        ADMIN_GUARD["fail_count"] = state["fail_count"]
        ADMIN_GUARD["lock_until"] = state["lock_until"]
        remaining_attempts = max_attempts - fail_count
        return remaining_attempts, 0


def _reset_admin_guard() -> None:
    with ADMIN_GUARD_LOCK:
        state = {"fail_count": 0, "lock_until": 0.0}
        _save_admin_guard_state(state)
        ADMIN_GUARD["fail_count"] = state["fail_count"]
        ADMIN_GUARD["lock_until"] = state["lock_until"]


def _audit_security_event(settings: Settings, event: str, **details: Any) -> None:
    if not settings.app_security_audit_enabled:
        return
    payload = {
        "ts": _utc_now_iso(),
        "event": event,
        "details": details,
    }
    with AUDIT_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")
    _restrict_file_permissions(AUDIT_LOG_FILE)


def _collect_secret_values(task_settings: Settings) -> list[str]:
    values = [
        task_settings.user_password,
        task_settings.app_access_code,
        task_settings.app_access_code_hash,
        task_settings.app_admin_code,
        task_settings.app_admin_code_hash,
    ]
    return [v for v in values if isinstance(v, str) and v]


def _sanitize_log_message(message: str, secret_values: list[str]) -> str:
    sanitized = message
    for secret in secret_values:
        if not secret:
            continue
        sanitized = sanitized.replace(secret, "***")
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub(lambda m: f"{m.group(1)}=***", sanitized)
    return sanitized


def _safe_log_fn(task_settings: Settings, log_fn: Callable[[str], None]) -> Callable[[str], None]:
    secrets = _collect_secret_values(task_settings)

    def _wrapped(message: str) -> None:
        safe = _sanitize_log_message(str(message), secrets)
        log_fn(safe)

    return _wrapped


def _owner_key_secret(settings: Settings) -> str:
    candidates = [
        str(settings.app_access_code_hash or "").strip(),
        str(settings.app_admin_code_hash or "").strip(),
        str(settings.app_access_code or "").strip(),
        str(settings.app_admin_code or "").strip(),
    ]
    for token in candidates:
        if token:
            return token
    return "khnp-owner-v1"


def _account_owner_key(user_id: str, fallback_viewer_id: str, secret: str) -> str:
    normalized = re.sub(r"\s+", "", str(user_id or "").strip()).lower()
    if not normalized:
        return f"anon:{fallback_viewer_id}"
    digest = hmac.new(secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"acct:{digest}"


def _account_owner_label(user_id: str, fallback_viewer_id: str) -> str:
    normalized = re.sub(r"\s+", "", str(user_id or "").strip())
    if normalized:
        return normalized
    return f"anonymous-{fallback_viewer_id[:6]}"


def _build_task_settings(
    *,
    user_id_input: str,
    user_password_input: str,
    show_browser: bool,
    completion_max_courses: int,
    rag_docs_dir: str,
    rag_index_path: str,
    rag_embed_model: str,
    rag_generate_model: str,
    rag_top_k: int,
    rag_conf_threshold: float,
    rag_web_search_enabled: bool,
    rag_web_top_n: int,
    rag_web_timeout_sec: int,
    rag_web_weight: float,
    exam_answer_bank_path: str,
    exam_auto_retry_max: int,
    exam_retry_requires_answer_index: bool,
) -> Settings:
    settings = Settings()
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
    return settings


def _result_payload(result: Any, *, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "success": bool(getattr(result, "success", False)),
        "message": str(getattr(result, "message", "")),
        "current_url": str(getattr(result, "current_url", "")),
    }
    if diagnostics:
        payload["diagnostics"] = diagnostics
        if not payload["current_url"]:
            payload["current_url"] = str(diagnostics.get("current_url", "") or "")
    return payload


def _run_automator_method(
    task_settings: Settings,
    method_name: str,
    log_fn: Callable[[str], None],
    stop_requested: Callable[[], bool],
    **kwargs: Any,
) -> dict[str, Any]:
    safe_log = _safe_log_fn(task_settings, log_fn)
    automator = EKHNPAutomator(task_settings, log_fn=safe_log, stop_requested=stop_requested)
    method = getattr(automator, method_name)
    try:
        result = method(**kwargs)
        payload = _result_payload(result, diagnostics=automator.get_runtime_diagnostics())
    except EKHNPAutomator.StopRequested as exc:
        payload = {
            "success": False,
            "canceled": True,
            "message": str(exc),
            "current_url": "",
            "diagnostics": automator.get_runtime_diagnostics(),
        }
        safe_log(str(exc))
    except Exception as exc:  # noqa: BLE001
        payload = {
            "success": False,
            "message": f"오류 발생: {exc}",
            "current_url": "",
            "traceback": traceback.format_exc(),
            "diagnostics": automator.get_runtime_diagnostics(),
        }
        safe_log(f"오류 발생: {exc}")
    safe_log(f"결과: {payload['message']} / url={payload['current_url']}")
    return payload


def _run_rag_index(
    task_settings: Settings,
    log_fn: Callable[[str], None],
    stop_requested: Callable[[], bool],
) -> dict[str, Any]:
    safe_log = _safe_log_fn(task_settings, log_fn)
    from rag_index import build_rag_index

    if stop_requested():
        message = "사용자 중단 요청으로 작업을 중지합니다."
        safe_log(message)
        return {"success": False, "canceled": True, "message": message}

    try:
        result = build_rag_index(
            docs_dir=task_settings.rag_docs_dir,
            index_path=task_settings.rag_index_path,
            embed_model=task_settings.rag_embed_model,
            chunk_size=task_settings.rag_chunk_size,
            overlap=task_settings.rag_chunk_overlap,
            min_chunk_chars=task_settings.rag_min_chunk_chars,
            max_chunks=task_settings.rag_max_chunks,
            max_total_size_gb=task_settings.rag_storage_limit_gb,
            prune_old_indexes=task_settings.rag_prune_old_indexes,
            ollama_base_url=task_settings.ollama_base_url,
            log_fn=safe_log,
        )
        message = (
            "RAG 인덱스 완료: "
            f"files={result.get('files')} chunks={result.get('chunks')} path={result.get('index_path')}"
        )
        safe_log(f"결과: {message}")
        return {"success": True, "message": message, "index_result": result}
    except Exception as exc:  # noqa: BLE001
        safe_log(f"RAG 인덱스 오류: {exc}")
        return {
            "success": False,
            "message": f"RAG 인덱스 오류: {exc}",
            "traceback": traceback.format_exc(),
        }


def _run_one_click(
    task_settings: Settings,
    check_interval_minutes: int,
    max_timefill_checks: int,
    force_reindex: bool,
    log_fn: Callable[[str], None],
    stop_requested: Callable[[], bool],
) -> dict[str, Any]:
    safe_log = _safe_log_fn(task_settings, log_fn)
    index_path = Path(task_settings.rag_index_path) if task_settings.rag_index_path else None
    if stop_requested():
        message = "사용자 중단 요청으로 작업을 중지합니다."
        safe_log(message)
        return {"success": False, "canceled": True, "message": message}
    need_reindex = bool(force_reindex)
    if index_path is not None and not index_path.exists():
        need_reindex = True
        safe_log(f"RAG 인덱스 파일이 없어 자동 생성합니다: {index_path}")

    if need_reindex:
        reindex_result = _run_rag_index(task_settings, safe_log, stop_requested)
        if bool(reindex_result.get("canceled", False)):
            return reindex_result

    max_resume_retry = max(0, min(4, int(getattr(task_settings, "app_resume_retry_max", 2) or 2)))
    backoff_sec = max(0.0, min(20.0, float(getattr(task_settings, "app_resume_retry_backoff_sec", 2) or 2)))
    transient_tokens = [
        "target page",
        "browser has been closed",
        "context has been closed",
        "page has been closed",
        "세션",
        "session",
        "로그아웃",
        "재로그인",
        "login",
        "타임아웃",
        "timeout",
    ]
    latest = _run_automator_method(
        task_settings,
        "login_and_run_completion_workflow",
        safe_log,
        stop_requested,
        check_interval_minutes=check_interval_minutes,
        max_timefill_checks=max_timefill_checks,
    )
    for retry_idx in range(1, max_resume_retry + 1):
        if bool(latest.get("success", False)):
            return latest
        if bool(latest.get("canceled", False)) or stop_requested():
            if not latest.get("message"):
                latest["message"] = "사용자 중단 요청으로 작업을 중지합니다."
            latest["success"] = False
            latest["canceled"] = True
            return latest
        message = str(latest.get("message", "")).lower()
        should_retry = any(token in message for token in transient_tokens)
        if not should_retry:
            return latest
        safe_log(
            "원클릭 일시 오류 감지: 자동 재로그인/재결합 재시도 "
            f"{retry_idx}/{max_resume_retry} (backoff={backoff_sec * retry_idx:.1f}s)"
        )
        if backoff_sec > 0:
            time.sleep(backoff_sec * retry_idx)
        latest = _run_automator_method(
            task_settings,
            "login_and_run_completion_workflow",
            safe_log,
            stop_requested,
            check_interval_minutes=check_interval_minutes,
            max_timefill_checks=max_timefill_checks,
        )
        if bool(latest.get("success", False)):
            safe_log(f"원클릭 자동 재결합 성공: retry={retry_idx}")
            return latest
    if not bool(latest.get("success", False)):
        safe_log(f"원클릭 자동 재시도 최종 실패: {latest.get('message', '')}")
    return latest


def _enqueue_job(
    settings: Settings,
    queue_manager: TaskQueueManager,
    name: str,
    runner: Callable[[Callable[[str], None], Callable[[], bool]], dict[str, Any]],
    owner: str,
    owner_label: str,
    role: str,
) -> str | None:
    active_job = queue_manager.find_active_job(owner=owner)
    if active_job is not None:
        active_job_id = str(active_job.get("job_id", ""))
        active_name = str(active_job.get("name", ""))
        active_status = str(active_job.get("status", ""))
        append_log(
            f"[QUEUE] 중복 등록 차단: owner={owner_label} new={name} existing={active_name}({active_status})/{active_job_id}"
        )
        _audit_security_event(
            settings,
            "queue_submit_blocked_active_owner",
            owner=owner_label,
            task=name,
            active_job_id=active_job_id,
            active_status=active_status,
            active_name=active_name,
        )
        if active_job_id:
            st.session_state.selected_job_id = active_job_id
            st.warning(
                "동일 계정에서 이미 실행 중인 작업이 있습니다. "
                f"기존 작업을 확인하세요. id={active_job_id} / status={active_status}"
            )
            return None
        st.warning("동일 계정에서 이미 실행 중인 작업이 있어 신규 등록을 차단했습니다.")
        return None

    try:
        job_id = queue_manager.submit(name, runner, owner=owner, owner_label=owner_label, role=role)
    except QueueCapacityError as exc:
        append_log(f"[QUEUE] 등록 거부: {name} / reason={exc}")
        _audit_security_event(settings, "queue_submit_rejected", task=name, reason=str(exc))
        st.error(f"작업 등록 실패: {exc}")
        return None
    st.session_state.selected_job_id = job_id
    append_log(f"[QUEUE] 작업 등록: {name} / id={job_id}")
    _audit_security_event(settings, "queue_submit", task=name, job_id=job_id)
    return job_id


def _render_scrollable_log_block(
    lines: list[str],
    *,
    height_px: int = 240,
    empty_text: str = "(로그 없음)",
    newest_first: bool = True,
    state_key: str = "default",
) -> None:
    rendered_lines: list[str] = []
    source_lines = list(reversed(lines)) if newest_first else list(lines)
    for line in source_lines:
        ts, rest = _split_log_line_timestamp(str(line))
        if ts:
            rendered_lines.append(
                "<div style='white-space:pre-wrap; word-break:break-word;'>"
                f"[{_render_timestamp_span(ts)}] {html.escape(rest)}"
                "</div>"
            )
        else:
            rendered_lines.append(
                "<div style='white-space:pre-wrap; word-break:break-word;'>"
                f"{html.escape(str(line))}"
                "</div>"
            )
    if not rendered_lines:
        rendered_lines.append(html.escape(empty_text))
    safe_state_key = re.sub(r"[^0-9A-Za-z_-]+", "-", str(state_key or "default")).strip("-") or "default"
    block_id = f"log-block-{safe_state_key}"
    content_sig = hashlib.sha1(
        "\n".join(str(line) for line in source_lines).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:20]
    payload = f"""
<div
  id="{block_id}"
  data-content-sig="{content_sig}"
  style="max-height:{int(height_px)}px; overflow-y:auto; padding:10px; border:1px solid #d9d9d9;
         border-radius:6px; background:#111111; color:#f3f3f3;
         font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
         font-size:12px; line-height:1.45;"
>
  {''.join(rendered_lines)}
</div>
<script>
(() => {{
  const root = document.getElementById({json.dumps(block_id)});
  if (!root) return;
  const storageKey = {json.dumps(f"log-scroll:{safe_state_key}")};
  const sig = root.getAttribute("data-content-sig") || "";
  const pad = (n) => String(n).padStart(2, "0");
  const formatLocal = (iso) => {{
    try {{
      const d = new Date(String(iso || ""));
      if (Number.isNaN(d.getTime())) return String(iso || "");
      return `${{d.getFullYear()}}-${{pad(d.getMonth() + 1)}}-${{pad(d.getDate())}} ${{
        pad(d.getHours())
      }}:${{pad(d.getMinutes())}}:${{pad(d.getSeconds())}}`;
    }} catch (_err) {{
      return String(iso || "");
    }}
  }};
  root.querySelectorAll("[data-utc]").forEach((node) => {{
    const iso = node.getAttribute("data-utc") || "";
    const value = formatLocal(iso);
    node.textContent = value;
    node.setAttribute("title", `${{value}} (${{
      Intl.DateTimeFormat().resolvedOptions().timeZone || "local"
    }})`);
  }});
  const getStore = () => {{
    try {{
      if (window.top && window.top.sessionStorage) return window.top.sessionStorage;
    }} catch (_err) {{}}
    try {{
      return window.sessionStorage;
    }} catch (_err) {{
      return null;
    }}
  }};
  const store = getStore();
  const loadState = () => {{
    if (!store) return {{}};
    try {{
      return JSON.parse(store.getItem(storageKey) || "{{}}");
    }} catch (_err) {{
      return {{}};
    }}
  }};
  const saveState = () => {{
    if (!store) return;
    try {{
      store.setItem(storageKey, JSON.stringify({{ sig, scrollTop: root.scrollTop }}));
    }} catch (_err) {{}}
  }};
  const state = loadState();
  if (String(state.sig || "") !== sig) {{
    root.scrollTop = 0;
  }} else if (typeof state.scrollTop === "number" && Number.isFinite(state.scrollTop)) {{
    root.scrollTop = state.scrollTop;
  }}
  let timer = null;
  root.addEventListener("scroll", () => {{
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(saveState, 80);
  }}, {{ passive: true }});
  window.setTimeout(saveState, 0);
}})();
</script>
"""
    components.html(payload, height=height_px + 28, scrolling=False)


def _render_queue_status(
    queue_manager: TaskQueueManager,
    owner: str | None = None,
    compact: bool = False,
    is_admin: bool = False,
) -> bool:
    st.subheader("작업 큐 상태")
    owner_filter = owner
    runtime_info = queue_manager.runtime_info()

    if is_admin and owner is None:
        owner_rows = queue_manager.owner_stats()
        active_owner_count = sum(1 for row in owner_rows if int(row.get("pending", 0)) > 0 or int(row.get("running", 0)) > 0)
        overall_stats = queue_manager.get_stats(owner=None)

        o1, o2, o3, o4, o5 = st.columns(5)
        o1.metric("활성 계정", active_owner_count)
        o2.metric("전체 Pending", overall_stats["pending"])
        o3.metric("전체 Running", overall_stats["running"])
        o4.metric("전체 Failed", overall_stats["failed"])
        o5.metric("전체 Total", overall_stats["total"])

        if owner_rows:
            table_rows = [
                {
                    "owner_label": str(row.get("owner_label") or row.get("owner")),
                    "pending": int(row.get("pending", 0)),
                    "running": int(row.get("running", 0)),
                    "failed": int(row.get("failed", 0)),
                    "succeeded": int(row.get("succeeded", 0)),
                    "total": int(row.get("total", 0)),
                    "latest_created_at": str(row.get("latest_created_at", "")),
                }
                for row in owner_rows
            ]
            st.caption("계정별 큐 현황")
            _render_simple_html_table(
                table_rows,
                columns=[
                    ("owner_label", "계정"),
                    ("pending", "Pending"),
                    ("running", "Running"),
                    ("failed", "Failed"),
                    ("succeeded", "Succeeded"),
                    ("total", "Total"),
                    ("latest_created_at", "최근 생성"),
                ],
                height_px=220,
                empty_text="계정별 큐 현황이 없습니다.",
            )

            owner_options = ["__all__"] + [str(row.get("owner", "")) for row in owner_rows]
            owner_labels = {
                "__all__": "전체 계정",
                **{
                    str(row.get("owner", "")): str(row.get("owner_label") or row.get("owner"))
                    for row in owner_rows
                },
            }
            selected_owner = st.selectbox(
                "관리자 계정 필터",
                options=owner_options,
                index=0,
                format_func=lambda x: owner_labels.get(str(x), str(x)),
            )
            if selected_owner != "__all__":
                owner_filter = str(selected_owner)

    stats = queue_manager.get_stats(owner=owner_filter)
    has_active_jobs = int(stats["pending"]) > 0 or int(stats["running"]) > 0
    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("Pending", stats["pending"])
    s2.metric("Running", stats["running"])
    s3.metric("Succeeded", stats["succeeded"])
    s4.metric("Failed", stats["failed"])
    s5.metric("Total", stats["total"])
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("설정 워커", runtime_info["configured_workers"])
    r2.metric("활성 워커", runtime_info["active_workers"])
    r3.metric("실행 슬롯 사용", runtime_info["running_jobs"])
    r4.metric("대기 작업", runtime_info["pending_jobs"])
    left, right = st.columns([3, 1])
    with left:
        st.caption("실행은 워커에서 순차 처리됩니다. 브라우저 탭을 닫아도 서버에서 계속 진행됩니다.")
    with right:
        st.button("상태 새로고침", width='stretch')

    jobs = queue_manager.list_jobs(limit=50, owner=owner_filter, include_logs=False)
    if not jobs:
        st.info("등록된 작업이 없습니다.")
        return has_active_jobs

    def _pending_position(target_job_id: str) -> tuple[int, int]:
        pending_jobs = [j for j in jobs if str(j.get("status")) == "pending"]
        for idx, row in enumerate(pending_jobs, start=1):
            if str(row.get("job_id")) == target_job_id:
                return idx, len(pending_jobs)
        return 0, len(pending_jobs)

    def _render_retry_button(job: dict[str, Any], button_key: str) -> None:
        if str(job.get("status")) != "failed":
            return
        if st.button("실패 작업 재시도", key=button_key, width='content'):
            owner_key = str(job.get("owner") or "").strip()
            if owner_key:
                active = queue_manager.find_active_job(owner=owner_key)
                if active is not None:
                    st.warning(
                        "동일 계정에 실행 중 작업이 있어 재시도를 대기시켰습니다. "
                        f"id={active.get('job_id', '-')}, status={active.get('status', '-')}"
                    )
                    st.session_state.selected_job_id = str(active.get("job_id", st.session_state.get("selected_job_id", "")))
                    return
            try:
                retry_job_id = queue_manager.retry_job(str(job.get("job_id", "")))
            except Exception as exc:  # noqa: BLE001
                st.error(f"재시도 등록 실패: {exc}")
                return
            st.session_state.selected_job_id = retry_job_id
            st.success(f"재시도 작업을 등록했습니다. id={retry_job_id}")

    def _render_stop_button(job: dict[str, Any], button_key: str) -> None:
        if str(job.get("status")) not in {"pending", "running"}:
            return
        if st.button("STOP", key=button_key, width='content'):
            owner_key = str(job.get("owner") or "").strip() or None
            canceled = queue_manager.cancel_job(str(job.get("job_id", "")), owner=owner_key)
            if canceled:
                st.session_state.selected_job_id = str(job.get("job_id", st.session_state.get("selected_job_id", "")))
                st.warning("중단 요청을 전송했습니다. 현재 단계 종료 후 작업이 멈춥니다.")
            else:
                st.info("이미 종료되었거나 중단할 수 없는 작업입니다.")

    def _render_job_logs(job: dict[str, Any], *, key_suffix: str, height_px: int) -> None:
        logs = list(job.get("logs") or [])
        _render_scrollable_log_block(
            logs,
            height_px=height_px,
            empty_text="(작업 로그 없음)",
            state_key=f"job-{key_suffix}",
        )

    def _render_job_diagnostics(job: dict[str, Any], *, key_suffix: str) -> None:
        diagnostic_path = str(job.get("diagnostic_path", "") or "").strip()
        result_payload = job.get("result") if isinstance(job.get("result"), dict) else {}
        diagnostics = result_payload.get("diagnostics") if isinstance(result_payload.get("diagnostics"), dict) else {}
        if diagnostic_path:
            st.caption(f"진단 번들: {diagnostic_path}")
        if not diagnostics:
            return
        artifact_rows = [row for row in diagnostics.get("artifact_paths", []) if isinstance(row, dict)]
        run_id = str(diagnostics.get("run_id", "") or "").strip()
        last_course_title = str(diagnostics.get("last_course_title", "") or "").strip()
        last_lesson_title = str(diagnostics.get("last_lesson_title", "") or "").strip()
        current_percent = diagnostics.get("last_course_progress_percent")
        exam_summary = diagnostics.get("last_exam_summary") if isinstance(diagnostics.get("last_exam_summary"), dict) else {}
        if not any([run_id, last_course_title, last_lesson_title, artifact_rows, exam_summary]):
            return
        with st.expander("진단 정보", expanded=False):
            if run_id:
                st.caption(f"자동화 run_id: {run_id}")
            if last_course_title:
                st.write(f"마지막 과정: {last_course_title}")
            if last_lesson_title:
                st.write(f"마지막 차시: {last_lesson_title}")
            if isinstance(current_percent, int) and current_percent >= 0:
                st.write(f"마지막 강의 진도율: {current_percent}%")
            if exam_summary:
                st.write(
                    "마지막 시험 요약: "
                    f"success={bool(exam_summary.get('success', False))}, "
                    f"solved={int(exam_summary.get('solved', 0) or 0)}, "
                    f"skipped={int(exam_summary.get('skipped', 0) or 0)}, "
                    f"low_conf={int(exam_summary.get('low_conf_used', 0) or 0)}"
                )
            if artifact_rows:
                lines = []
                for row in artifact_rows[:20]:
                    kind = str(row.get("kind", "") or "artifact")
                    label = str(row.get("label", "") or "").strip()
                    path = str(row.get("path", "") or "").strip()
                    prefix = f"{kind}"
                    if label:
                        prefix = f"{prefix} ({label})"
                    lines.append(f"{prefix}: {path}")
                st.code("\n".join(lines))

    if compact:
        current_job = next((job for job in jobs if job.get("status") in {"pending", "running"}), jobs[0])
        current_job_detail = queue_manager.get_job(str(current_job.get("job_id", "")), owner=owner_filter, include_logs=True)
        if current_job_detail:
            current_job = current_job_detail
        _render_client_localized_html(
            (
                "<div style='padding:4px 0 8px 0; font-size:13px;'>"
                f"현재 작업: <strong>{html.escape(str(current_job.get('name', '-') or '-'))}</strong>"
                f" / 상태: <strong>{html.escape(str(current_job.get('status', '-') or '-'))}</strong>"
                f" / 생성: {_render_timestamp_span(str(current_job.get('created_at', '-') or '-'))}"
                "</div>"
            ),
            height_px=46,
            scrolling=False,
        )
        if current_job.get("status") == "pending":
            pending_pos, pending_total = _pending_position(str(current_job.get("job_id", "")))
            if pending_pos > 0:
                st.info(f"대기 순번: {pending_pos}/{pending_total} (워커 {queue_manager.worker_count}개)")
        elif current_job.get("status") == "canceled":
            st.warning(current_job.get("result", {}).get("message", "작업이 중단되었습니다."))
        if current_job.get("status") == "succeeded":
            message = ""
            if current_job.get("result"):
                message = str(current_job["result"].get("message", ""))
            if message:
                st.success(message)
            else:
                st.success("작업 완료")
        elif current_job.get("status") == "failed":
            st.error(current_job.get("error") or "작업 실패")
        _render_retry_button(current_job, button_key=f"retry_compact_{current_job.get('job_id', '')}")
        history_path = str(current_job.get("history_path", "")).strip()
        if history_path:
            st.caption(f"작업 스냅샷: {history_path}")
        _render_job_diagnostics(current_job, key_suffix=f"compact_diag_{current_job.get('job_id', '')}")
        _render_job_logs(current_job, key_suffix=f"compact_{current_job.get('job_id', '')}", height_px=220)
        return has_active_jobs

    rows = [
        {
            "job_id": job["job_id"],
            "owner_label": str(job.get("owner_label") or job.get("owner") or ""),
            "name": job["name"],
            "status": job["status"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        }
        for job in jobs
    ]
    _render_simple_html_table(
        rows,
        columns=[
            ("job_id", "job_id"),
            ("owner_label", "계정"),
            ("name", "작업"),
            ("status", "상태"),
            ("created_at", "생성"),
            ("started_at", "시작"),
            ("finished_at", "종료"),
        ],
        height_px=260,
        empty_text="등록된 작업이 없습니다.",
    )

    job_ids = [job["job_id"] for job in jobs]
    default_job_id = st.session_state.get("selected_job_id")
    if default_job_id not in job_ids:
        default_job_id = job_ids[0]
    selected_index = job_ids.index(default_job_id)
    selected_job_id = st.selectbox("상세 작업", options=job_ids, index=selected_index)
    st.session_state.selected_job_id = selected_job_id

    job = queue_manager.get_job(selected_job_id, owner=owner_filter, include_logs=True)
    if not job:
        st.warning("선택한 작업 정보를 읽지 못했습니다.")
        return has_active_jobs

    if job["status"] == "succeeded":
        message = ""
        if job.get("result"):
            message = str(job["result"].get("message", ""))
        if message:
            st.success(message)
        else:
            st.success("작업 완료")
    elif job["status"] == "failed":
        st.error(job.get("error") or "작업 실패")
        _render_retry_button(job, button_key=f"retry_detail_{job.get('job_id', '')}")
    elif job["status"] == "canceled":
        st.warning(job.get("result", {}).get("message", "작업이 중단되었습니다."))

    if job.get("status") == "pending":
        pending_pos, pending_total = _pending_position(str(job.get("job_id", "")))
        if pending_pos > 0:
            st.info(f"대기 순번: {pending_pos}/{pending_total} (워커 {queue_manager.worker_count}개)")

    history_path = str(job.get("history_path", "")).strip()
    if history_path:
        st.caption(f"작업 스냅샷: {history_path}")
    _render_job_diagnostics(job, key_suffix=f"detail_diag_{job.get('job_id', '')}")

    _render_job_logs(job, key_suffix=f"detail_{job.get('job_id', '')}", height_px=260)

    if job.get("traceback"):
        with st.expander("실패 traceback"):
            st.code(str(job["traceback"]))
    return has_active_jobs


@st.fragment(run_every="1s")
def _render_live_queue_and_logs_fragment(
    queue_manager: TaskQueueManager,
    *,
    owner: str | None,
    compact: bool,
    is_admin: bool,
) -> None:
    has_active_jobs = _render_queue_status(
        queue_manager,
        owner=owner,
        compact=compact,
        is_admin=is_admin,
    )
    st.subheader("실행 로그")
    recent_logs = list(st.session_state.logs[-500:]) if st.session_state.logs else []
    _render_scrollable_log_block(
        recent_logs,
        height_px=260,
        empty_text="(아직 로그 없음)",
        state_key="session-live",
    )
    if has_active_jobs:
        st.caption("실시간 로그 업데이트: ON")
    else:
        st.caption("실시간 로그 업데이트: IDLE")


def _render_system_flow_diagram(start_label: str) -> None:
    st.caption("시스템 전체 동작도")
    _ = start_label
    max_width = 760
    font_size = "11px"
    comp_height = 700
    mermaid_code = """
flowchart TB
  subgraph S1["1단계: 계정 동기화"]
    direction TB
    A["ID/PW 입력"] --> B["Enter 또는 로그인/동기화"]
    B --> C["계정 기준 기존 작업 조회"]
  end

  subgraph S2["2단계: 실행 등록"]
    direction TB
    D["START 클릭"] --> E{"동일 계정 실행 중 작업 존재?"}
    E -- "예" --> F["신규 실행 차단<br/>기존 작업 화면으로 이동"]
    E -- "아니오" --> G{"동시 접속 인원 여유?<br/>(최대 5명)"}
    G -- "없음" --> H["대기열(Pending) 등록"] --> I["실행 대기"]
    G -- "있음" --> J["즉시 실행(Running)"]
  end

  subgraph S3["3단계: 자동화 실행"]
    direction TB
    K["원클릭 시작<br/>(인덱스 확인/필요 시 생성)"] --> L["수료 워크플로우 실행<br/>(진도 → 학습시간 → 시험)"]
    L --> M["자동 학습 진행 + 시간 보충"]
    M --> N["시험 자동 풀이<br/>(DOM → Structured → OCR 폴백)"]
  end

  subgraph S4["4단계: 판단/우회"]
    direction TB
    O{"AI 풀이 신뢰도 충분?"}
    O -- "아니오" --> P["재질문/재시도/우회"]
    P --> Q["우회 이력 저장<br/>(.runtime/deferred_exam_courses.json)"]
    O -- "예" --> R["제출/채점 진행"]
    Q --> R
  end

  subgraph S5["5단계: 결과 반영"]
    direction TB
    S["결과/품질 리포트/스냅샷 저장"] --> T["실시간 로그/상태 반영<br/>(1초 주기)"]
  end

  C --> D
  J --> K
  I --> T
  F --> T
  N --> O
  R --> S

  classDef info fill:#EAF2FF,stroke:#3B82F6,color:#1E3A8A,stroke-width:1.3px;
  classDef decision fill:#FFF7E6,stroke:#F59E0B,color:#92400E,stroke-width:1.3px;
  classDef queue fill:#FFEAEA,stroke:#EF4444,color:#991B1B,stroke-width:1.3px;
  classDef run fill:#ECFDF5,stroke:#10B981,color:#065F46,stroke-width:1.3px;
  classDef save fill:#F3F4F6,stroke:#6B7280,color:#111827,stroke-width:1.3px;

  class A,B,C,D,K,L,M,N,S,T info;
  class E,G,O decision;
  class F,H,I queue;
  class J,P,Q,R run;
"""
    mermaid_html = f"""
<style>
  .mermaid-wrap {{
    border: 1px solid #d9d9d9;
    border-radius: 8px;
    padding: 6px;
    background: #fafafa;
    overflow-x: auto;
  }}
  .mermaid-wrap .mermaid {{
    text-align: center;
    font-size: {font_size};
  }}
  .mermaid-wrap svg {{
    margin-left: auto;
    margin-right: auto;
    display: block;
    max-width: {max_width}px;
    width: 100%;
    height: auto;
  }}
</style>
<div class="mermaid-wrap">
  <pre class="mermaid">{html.escape(mermaid_code)}</pre>
</div>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{
    startOnLoad: true,
    securityLevel: "loose",
    theme: "default",
    themeVariables: {{
      fontSize: "{font_size}"
    }},
    flowchart: {{
      useMaxWidth: true
    }}
  }});
</script>
"""
    components.html(mermaid_html, height=comp_height, scrolling=True)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return raw if isinstance(raw, dict) else None


def _load_recent_failure_bundles(limit: int = 12) -> list[dict[str, Any]]:
    root = Path(".runtime") / "job_diagnostics"
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for summary_path in sorted(root.glob("*/summary.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        payload = _read_json_file(summary_path)
        if not payload:
            continue
        job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
        artifact_rows = [row for row in payload.get("artifact_paths", []) if isinstance(row, dict)]
        exam_summary = diagnostics.get("last_exam_summary") if isinstance(diagnostics.get("last_exam_summary"), dict) else {}
        rows.append(
            {
                "finished_at": str(job.get("finished_at", "") or ""),
                "job_id": str(job.get("job_id", "") or ""),
                "job_name": str(job.get("name", "") or ""),
                "owner_label": str(job.get("owner_label", "") or job.get("owner", "") or ""),
                "error": str(job.get("error", "") or ""),
                "course_title": str(diagnostics.get("last_course_title", "") or ""),
                "lesson_title": str(diagnostics.get("last_lesson_title", "") or ""),
                "run_id": str(diagnostics.get("run_id", "") or ""),
                "artifact_count": len(artifact_rows),
                "exam_solved": int(exam_summary.get("solved", 0) or 0) if exam_summary else 0,
                "summary_path": summary_path.as_posix(),
                "history_path": str(job.get("history_path", "") or ""),
            }
        )
        if len(rows) >= max(1, limit):
            break
    return rows


def _load_exam_quality_report_overview(limit: int = 12) -> tuple[dict[str, int], list[dict[str, Any]]]:
    root = Path("logs") / "exam_quality_reports"
    if not root.exists():
        return {"reports": 0, "alignment_ok": 0, "warnings": 0, "legacy_warnings": 0}, []
    rows: list[dict[str, Any]] = []
    alignment_ok = 0
    warnings = 0
    legacy_warnings = 0
    for report_path in sorted(root.glob("exam_quality_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        payload = _read_json_file(report_path)
        if not payload:
            continue
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        report_rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        questions = int(summary.get("questions", 0) or 0)
        matched = int(summary.get("matched_result_entries", 0) or 0)
        known = int(summary.get("correctness_known", 0) or 0)
        correct = int(summary.get("correct", 0) or 0)
        rich_rows = sum(
            1
            for row in report_rows
            if isinstance(row, dict)
            and str(row.get("question_norm", "") or "").strip()
            and isinstance(row.get("options"), list)
            and len([x for x in row.get("options", []) if str(x).strip()]) >= 2
        )
        detail_coverage = (float(rich_rows) / float(questions)) if questions > 0 else 0.0
        match_gap = max(0, questions - matched)
        known_gap = max(0, questions - known)
        accuracy = (float(correct) / float(known)) if known > 0 else 0.0
        is_ok = questions > 0 and matched >= questions and known >= questions
        is_legacy = (not is_ok) and questions > 0 and detail_coverage < 0.35
        if is_ok:
            alignment_ok += 1
        else:
            if is_legacy:
                legacy_warnings += 1
            else:
                warnings += 1
        rows.append(
            {
                "created_at": str(meta.get("created_at", "") or ""),
                "course_title": str(meta.get("course_title", "") or ""),
                "attempt_no": int(meta.get("attempt_no", 0) or 0),
                "questions": questions,
                "matched": matched,
                "known": known,
                "correct": correct,
                "accuracy_pct": round(accuracy * 100, 1),
                "match_gap": match_gap,
                "known_gap": known_gap,
                "status": "ok" if is_ok else ("legacy-warn" if is_legacy else "warn"),
                "path": report_path.as_posix(),
            }
        )
    rows.sort(
        key=lambda row: (
            0 if str(row.get("status")) == "warn" else 1,
            -int(row.get("match_gap", 0)),
            -int(row.get("known_gap", 0)),
            float(row.get("accuracy_pct", 0.0)),
            str(row.get("created_at", "")),
        )
    )
    return {
        "reports": len(rows[: max(1, limit)]),
        "alignment_ok": alignment_ok,
        "warnings": warnings,
        "legacy_warnings": legacy_warnings,
    }, rows[: max(1, limit)]


def _load_latest_proxy_preflight() -> dict[str, Any] | None:
    path = Path(".runtime") / "proxy_preflight_latest.json"
    payload = _read_json_file(path)
    if not payload:
        return None
    return payload


def _render_admin_failure_dashboard() -> None:
    st.subheader("최근 실패 작업")
    rows = _load_recent_failure_bundles(limit=15)
    if not rows:
        st.caption("저장된 실패 진단 번들이 아직 없습니다.")
        return
    m1, m2, m3 = st.columns(3)
    m1.metric("실패 번들", len(rows))
    m2.metric("아티팩트 포함", sum(1 for row in rows if int(row.get("artifact_count", 0)) > 0))
    m3.metric("시험 관여 실패", sum(1 for row in rows if int(row.get("exam_solved", 0)) > 0))
    table_rows = [
        {
            "finished_at": row["finished_at"],
            "job_id": row["job_id"],
            "job_name": row["job_name"],
            "course_title": row["course_title"],
            "lesson_title": row["lesson_title"],
            "artifact_count": row["artifact_count"],
            "summary_path": row["summary_path"],
        }
        for row in rows
    ]
    _render_simple_html_table(
        table_rows,
        columns=[
            ("finished_at", "종료"),
            ("job_id", "job_id"),
            ("job_name", "작업"),
            ("course_title", "과정"),
            ("lesson_title", "차시"),
            ("artifact_count", "artifact"),
            ("summary_path", "summary_path"),
        ],
        height_px=280,
        empty_text="저장된 실패 진단 번들이 아직 없습니다.",
    )
    selected_path = st.selectbox(
        "실패 진단 상세",
        options=[row["summary_path"] for row in rows],
        format_func=lambda path: next((f"{row['finished_at']} / {row['job_name']}" for row in rows if row["summary_path"] == path), path),
        key="admin_failure_bundle_select",
    )
    selected = next((row for row in rows if row["summary_path"] == selected_path), None)
    if selected:
        st.caption(f"진단 번들: {selected['summary_path']}")
        if selected.get("history_path"):
            st.caption(f"작업 스냅샷: {selected['history_path']}")
        detail_lines = [
            f"job_id={selected['job_id']}",
            f"job_name={selected['job_name']}",
            f"owner={selected['owner_label']}",
            f"course={selected['course_title']}",
            f"lesson={selected['lesson_title']}",
            f"run_id={selected['run_id']}",
            f"error={selected['error']}",
        ]
        st.code("\n".join(detail_lines))


def _render_admin_exam_quality_dashboard() -> None:
    st.subheader("시험 품질 리포트")
    stats, rows = _load_exam_quality_report_overview(limit=15)
    if not rows:
        st.caption("시험 품질 리포트가 아직 없습니다.")
        return
    q1, q2, q3 = st.columns(3)
    q1.metric("최근 리포트", stats["reports"])
    q2.metric("정합 OK", stats["alignment_ok"])
    q3.metric("경고", stats["warnings"])
    if int(stats.get("legacy_warnings", 0)) > 0:
        st.caption(f"legacy 경고: {int(stats.get('legacy_warnings', 0))}건")
    _render_simple_html_table(
        rows,
        columns=[
            ("created_at", "생성"),
            ("course_title", "과정"),
            ("attempt_no", "시도"),
            ("questions", "문항"),
            ("matched", "matched"),
            ("known", "known"),
            ("correct", "correct"),
            ("accuracy_pct", "정답률(%)"),
            ("status", "상태"),
            ("path", "path"),
        ],
        height_px=300,
        empty_text="시험 품질 리포트가 아직 없습니다.",
    )


def _render_admin_proxy_dashboard() -> None:
    st.subheader("현재 Egress / 프록시 프리플라이트")
    payload = _load_latest_proxy_preflight()
    if not payload:
        st.caption("저장된 프록시 프리플라이트 결과가 아직 없습니다.")
        return
    proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else {}
    egress = payload.get("egress") if isinstance(payload.get("egress"), dict) else {}
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("required", str(bool(proxy.get("required", False))))
    p2.metric("target", str(proxy.get("target_country", "") or "-"))
    p3.metric("detected", str(egress.get("country", "") or "-"))
    p4.metric("status", str(payload.get("status", "") or "-"))
    detail_rows = [
        {
            "checked_at": str(payload.get("checked_at", "") or ""),
            "server": str(proxy.get("server", "") or ""),
            "provider": str(egress.get("provider", "") or ""),
            "ip": str(egress.get("ip", "") or ""),
            "country": str(egress.get("country", "") or ""),
            "message": str(payload.get("message", "") or ""),
        }
    ]
    _render_simple_html_table(
        detail_rows,
        columns=[
            ("checked_at", "확인시각"),
            ("server", "proxy"),
            ("provider", "provider"),
            ("ip", "ip"),
            ("country", "country"),
            ("message", "message"),
        ],
        height_px=110,
        empty_text="프록시 프리플라이트 결과가 없습니다.",
    )


def main() -> None:
    st.set_page_config(page_title="e-KHNP Automation", layout="wide")
    st.title("e-KHNP Automation (Prototype)")
    st.caption("© Y.T. Kwon")

    settings = Settings()
    queue_manager = get_task_queue(
        worker_count=settings.app_worker_count,
        max_pending=settings.app_queue_max_pending,
        max_history=settings.app_queue_max_history,
    )

    if "access_granted" not in st.session_state:
        st.session_state.access_granted = False
    if "access_auth_ts" not in st.session_state:
        st.session_state.access_auth_ts = 0.0

    access_enabled = _is_access_code_enabled(settings)
    if not access_enabled and not settings.app_access_allow_open:
        _audit_security_event(settings, "access_blocked_no_code")
        st.error("보안 설정이 필요합니다. `.env`에 `APP_ACCESS_CODE` 또는 `APP_ACCESS_CODE_HASH`를 설정하세요.")
        st.stop()

    max_attempts = max(1, settings.app_access_max_attempts)
    cooldown_sec = max(30, settings.app_access_cooldown_sec)
    session_ttl_sec = max(60, settings.app_access_session_ttl_min * 60)
    now_ts = time.time()

    if st.session_state.access_granted and st.session_state.access_auth_ts > 0:
        if now_ts - float(st.session_state.access_auth_ts) > session_ttl_sec:
            st.session_state.access_granted = False
            st.session_state.access_auth_ts = 0.0

    if access_enabled and not st.session_state.access_granted:
        st.subheader("접속 코드")
        _, lock_remaining = _read_access_guard(now_ts)
        with st.form("access_code_form"):
            access_code_input = st.text_input(
                "접속 코드 입력",
                type="password",
                placeholder="관리자에게 문의하세요",
                disabled=lock_remaining > 0,
            )
            submit_access = st.form_submit_button(
                "입장",
                type="primary",
                width='stretch',
                disabled=lock_remaining > 0,
            )
        if submit_access:
            if _verify_access_code(access_code_input, settings):
                st.session_state.access_granted = True
                st.session_state.access_auth_ts = now_ts
                _reset_access_guard()
                _audit_security_event(settings, "access_granted", session_ttl_min=settings.app_access_session_ttl_min)
                st.rerun()
            else:
                remaining_attempts, new_lock_remaining = _record_access_failure(now_ts, max_attempts, cooldown_sec)
                if new_lock_remaining > 0:
                    _audit_security_event(
                        settings,
                        "access_locked",
                        cooldown_sec=new_lock_remaining,
                        max_attempts=max_attempts,
                    )
                    st.error(f"접속 코드 실패 횟수 초과로 {new_lock_remaining}초 잠금되었습니다.")
                else:
                    _audit_security_event(
                        settings,
                        "access_failed",
                        remaining_attempts=remaining_attempts,
                        max_attempts=max_attempts,
                    )
                    st.error(f"접속 코드가 올바르지 않습니다. 남은 시도 {remaining_attempts}회")
        if lock_remaining > 0:
            st.warning(f"보안 잠금 중입니다. {lock_remaining}초 후 다시 시도하세요.")
        st.caption(
            "코드는 `.env`의 `APP_ACCESS_CODE` 또는 `APP_ACCESS_CODE_HASH`로 관리합니다. "
            f"세션 유지 {settings.app_access_session_ttl_min}분, "
            f"실패 {max_attempts}회 시 {cooldown_sec}초 잠금."
        )
        st.stop()

    if "logs" not in st.session_state:
        st.session_state.logs = []
    default_ui_role = "admin" if settings.app_default_ui_role == "admin" else "user"
    force_ui_role = settings.app_force_ui_role if settings.app_force_ui_role in {"user", "admin"} else ""
    if "viewer_id" not in st.session_state:
        st.session_state.viewer_id = uuid.uuid4().hex
    if "ui_role" not in st.session_state:
        st.session_state.ui_role = default_ui_role
    if "admin_unlocked" not in st.session_state:
        st.session_state.admin_unlocked = False

    if force_ui_role:
        st.session_state.ui_role = force_ui_role
        st.caption(f"고정 모드: {'관리자' if force_ui_role == 'admin' else '사용자'}")
    else:
        st.subheader("화면 모드")
        selected_mode_label = st.radio(
            "모드 선택",
            ["사용자", "관리자"],
            index=1 if st.session_state.ui_role == "admin" else 0,
            horizontal=True,
        )
        selected_role = "admin" if selected_mode_label == "관리자" else "user"
        if selected_role != st.session_state.ui_role:
            st.session_state.ui_role = selected_role
            st.session_state.admin_unlocked = False
            st.rerun()

    is_admin = st.session_state.ui_role == "admin"
    viewer_id = str(st.session_state.viewer_id)

    if is_admin:
        admin_enabled = bool(settings.app_admin_code or settings.app_admin_code_hash)
        admin_max_attempts = max(1, settings.app_admin_max_attempts)
        admin_cooldown_sec = max(30, settings.app_admin_cooldown_sec)
        if not admin_enabled:
            st.error("관리자 코드가 설정되지 않았습니다. `.env`에 `APP_ADMIN_CODE` 또는 `APP_ADMIN_CODE_HASH`를 설정하세요.")
            st.stop()
        if not st.session_state.admin_unlocked:
            _, admin_lock_remaining = _read_admin_guard(now_ts)
            admin_code_input = st.text_input("관리자 비밀번호", type="password", placeholder="관리자 코드")
            if st.button("관리자 잠금 해제", type="primary", width='stretch', disabled=admin_lock_remaining > 0):
                if _verify_code(admin_code_input, settings.app_admin_code, settings.app_admin_code_hash):
                    st.session_state.admin_unlocked = True
                    _reset_admin_guard()
                    _audit_security_event(settings, "admin_unlock_success")
                    st.rerun()
                remaining_attempts, new_lock_remaining = _record_admin_failure(
                    now_ts,
                    admin_max_attempts,
                    admin_cooldown_sec,
                )
                if new_lock_remaining > 0:
                    _audit_security_event(
                        settings,
                        "admin_unlock_locked",
                        cooldown_sec=new_lock_remaining,
                        max_attempts=admin_max_attempts,
                    )
                    st.error(f"관리자 비밀번호 실패 횟수 초과로 {new_lock_remaining}초 잠금되었습니다.")
                else:
                    _audit_security_event(
                        settings,
                        "admin_unlock_failed",
                        remaining_attempts=remaining_attempts,
                        max_attempts=admin_max_attempts,
                    )
                    st.error(f"관리자 비밀번호가 올바르지 않습니다. 남은 시도 {remaining_attempts}회")
            if admin_lock_remaining > 0:
                st.warning(f"관리자 잠금 중입니다. {admin_lock_remaining}초 후 다시 시도하세요.")
            st.stop()
        top_col1, top_col2 = st.columns([4, 1])
        with top_col1:
            st.success("관리자 모드 잠금 해제됨")
        with top_col2:
            if st.button("관리자 잠금", width='stretch'):
                st.session_state.admin_unlocked = False
                st.rerun()

    if is_admin:
        st.subheader("마일스톤 진행 현황")
        milestones = [
            (
                "M1 프로젝트 골격",
                "100%",
                "기본 파일 구조, 설정 로딩, Streamlit 실행 뼈대 완료",
            ),
            (
                "M2 로그인 자동화",
                "99%",
                "로그인 선택자 보정 및 성공/실패 판정 로직 완료",
            ),
            (
                "M3 학습현황·첫과목 진입",
                "97%",
                "나의 학습현황 이동 + 수강과정 첫 행 학습하기 클릭/강의실 진입 확인",
            ),
            (
                "M4 강의 재생·완료처리",
                "97%",
                "차시 진행률 판독 + 미완료 차시 직접 진입 + 내부 next/우하단 Next 분리 처리",
            ),
            (
                "M5 수료 순서 자동화",
                "96%",
                "원클릭 실행 + 진도율→학습시간→시험 + 계정 동기화 후 Start 분리 + 미수료 차시 우선 보완",
            ),
            (
                "M6 종합평가 안정화",
                "97%",
                "structured 문항 추출 + 결과지 인덱싱/재응시/응시횟수 보호 및 품질 fail-fast",
            ),
            (
                "M7 RAG 풀이 고도화",
                "98%",
                "문항 정규화/보기 순서 불변 매칭 + 오답 근거 전환 + 품질 리포트",
            ),
            (
                "M8 원격 실행 서버화",
                "91%",
                "Streamlit user/admin 분리 + LaunchAgent + Tunnel 운영 안정화",
            ),
            (
                "M9 동시성/대기열",
                "97%",
                "APP_WORKER_COUNT=5 기준 동시 처리 + 초과 pending + 중복 실행 차단 + 실패 진단 번들",
            ),
            (
                "M10 운영/복구",
                "96%",
                "작업 스냅샷 + job diagnostics + 시험 품질 리포트 + UI 추적성 강화",
            ),
        ]
        row_size = 5
        for start in range(0, len(milestones), row_size):
            cols = st.columns(row_size)
            chunk = milestones[start : start + row_size]
            for col, (title, value, help_text) in zip(cols, chunk):
                with col:
                    st.metric(title, value, help=help_text)
        st.progress(0.99, text="전체 진행률 99%")
        st.caption("운영 실시간 현황")
        owner_rows = queue_manager.owner_stats()
        active_owner_count = sum(
            1 for row in owner_rows if int(row.get("pending", 0)) > 0 or int(row.get("running", 0)) > 0
        )
        overall_stats = queue_manager.get_stats(owner=None)
        op_col1, op_col2, op_col3, op_col4 = st.columns(4)
        with op_col1:
            st.metric("활성 계정", active_owner_count)
        with op_col2:
            st.metric("전체 Pending", overall_stats["pending"])
        with op_col3:
            st.metric("전체 Running", overall_stats["running"])
        with op_col4:
            st.metric("전체 Failed", overall_stats["failed"])
        _render_admin_proxy_dashboard()
        _render_admin_failure_dashboard()
        _render_admin_exam_quality_dashboard()

    if "account_sync_state" not in st.session_state:
        st.session_state.account_sync_state = {}
    if "account_user_id_input" not in st.session_state:
        st.session_state.account_user_id_input = settings.user_id
    if "account_user_password_input" not in st.session_state:
        st.session_state.account_user_password_input = settings.user_password

    st.subheader("계정 동기화")
    with st.form("account_sync_form", clear_on_submit=False):
        input_col1, input_col2 = st.columns(2)
        with input_col1:
            st.text_input(
                "아이디",
                key="account_user_id_input",
                placeholder="사번 또는 아이디",
                autocomplete="username",
            )
        with input_col2:
            st.text_input(
                "비밀번호",
                key="account_user_password_input",
                type="password",
                placeholder="비밀번호",
                autocomplete="current-password",
            )
        st.caption("Enter 또는 로그인/동기화 버튼으로 계정을 동기화한 뒤 Start를 눌러주세요.")
        login_sync_clicked = st.form_submit_button("로그인/동기화", type="primary", width='stretch')

    current_user_id_input = str(st.session_state.get("account_user_id_input", "")).strip()
    current_user_password_input = str(st.session_state.get("account_user_password_input", ""))
    sync_state_raw = st.session_state.get("account_sync_state", {})
    sync_state = dict(sync_state_raw) if isinstance(sync_state_raw, dict) else {}

    if login_sync_clicked:
        if not current_user_id_input or not current_user_password_input:
            st.session_state.account_sync_state = {}
            sync_state = {}
            st.error("ID/PW를 입력한 뒤 로그인/동기화를 눌러주세요.")
        else:
            sync_owner_key = _account_owner_key(current_user_id_input, viewer_id, _owner_key_secret(settings))
            sync_owner_label = _account_owner_label(current_user_id_input, viewer_id)
            sync_state = {
                "user_id": current_user_id_input,
                "user_password": current_user_password_input,
                "owner_key": sync_owner_key,
                "owner_label": sync_owner_label,
                "synced_at": _utc_now_iso(),
            }
            st.session_state.account_sync_state = sync_state
            append_log(f"[SYNC] 계정 동기화 완료: {sync_owner_label}")
            active_job = queue_manager.find_active_job(owner=sync_owner_key)
            if active_job is not None and str(active_job.get("job_id", "")).strip():
                st.session_state.selected_job_id = str(active_job.get("job_id", ""))
                st.success(
                    "계정 동기화 완료. "
                    f"실행 중 작업 id={active_job.get('job_id', '-')}, status={active_job.get('status', '-')}"
                )
            else:
                latest_jobs = queue_manager.list_jobs(limit=1, owner=sync_owner_key, include_logs=False)
                if latest_jobs:
                    latest_job = latest_jobs[0]
                    latest_job_id = str(latest_job.get("job_id", "")).strip()
                    if latest_job_id:
                        st.session_state.selected_job_id = latest_job_id
                    st.success(
                        "계정 동기화 완료. "
                        f"최근 작업 id={latest_job.get('job_id', '-')}, status={latest_job.get('status', '-')}"
                    )
                else:
                    st.success("계정 동기화 완료. 현재 계정의 기존 작업이 없습니다.")

    sync_ready = (
        bool(sync_state.get("user_id", "").strip())
        and bool(str(sync_state.get("user_password", "")))
        and bool(sync_state.get("owner_key", "").strip())
    )

    if is_admin:
        user_id_input = current_user_id_input
        user_password_input = current_user_password_input
    elif sync_ready:
        user_id_input = str(sync_state.get("user_id", "")).strip()
        user_password_input = str(sync_state.get("user_password", ""))
    else:
        user_id_input = current_user_id_input
        user_password_input = current_user_password_input

    if is_admin:
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
            st.number_input("종합평가 탐침 최대 문항 수", min_value=1, max_value=60, value=10, step=1)
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
                "학습시간 부족 체크 기본 간격(분, 실제 3~10분 동적)",
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
    else:
        st.info("사용자 모드: 원클릭 실행과 내 작업 상태만 사용 가능합니다.")
        show_browser = False
        stop_mode_label = "자동(총 차시 감지 우선, 실패 시 Next 버튼 기준)"
        manual_lesson_limit = None
        exam_probe_limit = 10
        rag_docs_dir = settings.rag_docs_dir
        rag_index_path = settings.rag_index_path
        rag_embed_model = settings.rag_embed_model
        rag_generate_model = settings.rag_generate_model
        rag_top_k = settings.rag_top_k
        rag_conf_threshold = settings.rag_conf_threshold
        rag_web_search_enabled = settings.rag_web_search_enabled
        rag_web_top_n = settings.rag_web_top_n
        rag_web_timeout_sec = settings.rag_web_timeout_sec
        rag_web_weight = settings.rag_web_weight
        timefill_check_interval_min = 10
        timefill_check_limit = 24
        completion_max_courses = settings.completion_max_courses
        exam_answer_bank_path = settings.exam_answer_bank_path
        exam_auto_retry_max = settings.exam_auto_retry_max
        exam_retry_requires_answer_index = settings.exam_retry_requires_answer_index
        one_click_force_reindex = False

    task_settings = _build_task_settings(
        user_id_input=user_id_input,
        user_password_input=user_password_input,
        show_browser=show_browser,
        completion_max_courses=completion_max_courses,
        rag_docs_dir=rag_docs_dir,
        rag_index_path=rag_index_path,
        rag_embed_model=rag_embed_model,
        rag_generate_model=rag_generate_model,
        rag_top_k=rag_top_k,
        rag_conf_threshold=rag_conf_threshold,
        rag_web_search_enabled=rag_web_search_enabled,
        rag_web_top_n=rag_web_top_n,
        rag_web_timeout_sec=rag_web_timeout_sec,
        rag_web_weight=rag_web_weight,
        exam_answer_bank_path=exam_answer_bank_path,
        exam_auto_retry_max=exam_auto_retry_max,
        exam_retry_requires_answer_index=exam_retry_requires_answer_index,
    )
    if not is_admin and sync_ready:
        queue_owner_key = str(sync_state.get("owner_key", "")).strip()
        queue_owner_label = str(sync_state.get("owner_label", "")).strip() or _account_owner_label(task_settings.user_id, viewer_id)
    else:
        queue_owner_key = _account_owner_key(task_settings.user_id, viewer_id, _owner_key_secret(settings))
        queue_owner_label = _account_owner_label(task_settings.user_id, viewer_id)

    if is_admin:
        if task_settings.user_id:
            st.caption(f"큐 계정 식별자: {queue_owner_label}")
        else:
            st.warning("아이디가 비어 있어 익명 세션 큐로 처리됩니다. 브라우저 재접속 시 작업 추적이 끊길 수 있습니다.")
    else:
        if sync_ready:
            st.caption(f"동기화된 계정: {queue_owner_label}")
        else:
            st.warning("ID/PW 입력 후 Enter 또는 로그인/동기화를 눌러 계정을 먼저 동기화하세요.")

    if is_admin:
        st.subheader("설정 확인")
        st.write(
            {
                "base_url": settings.base_url,
                "login_url": settings.login_url,
                "app_access_code_set": bool(settings.app_access_code),
                "app_access_code_hash_set": bool(settings.app_access_code_hash),
                "app_admin_code_set": bool(settings.app_admin_code or settings.app_admin_code_hash),
                "app_admin_max_attempts": admin_max_attempts,
                "app_admin_cooldown_sec": admin_cooldown_sec,
                "app_default_ui_role": settings.app_default_ui_role,
                "app_force_ui_role": settings.app_force_ui_role,
                "app_access_allow_open": settings.app_access_allow_open,
                "app_access_max_attempts": max_attempts,
                "app_access_cooldown_sec": cooldown_sec,
                "app_access_session_ttl_min": settings.app_access_session_ttl_min,
                "app_worker_count": settings.app_worker_count,
                "app_queue_max_pending": settings.app_queue_max_pending,
                "app_queue_max_history": settings.app_queue_max_history,
                "app_security_audit_enabled": settings.app_security_audit_enabled,
                "headless": task_settings.headless,
                "timeout_ms": task_settings.timeout_ms,
                "user_id_set": bool(task_settings.user_id),
                "user_password_set": bool(task_settings.user_password),
                "ollama_base_url": task_settings.ollama_base_url,
                "rag_docs_dir": task_settings.rag_docs_dir,
                "rag_index_path": task_settings.rag_index_path,
                "rag_embed_model": task_settings.rag_embed_model,
                "rag_generate_model": task_settings.rag_generate_model,
                "rag_top_k": task_settings.rag_top_k,
                "rag_conf_threshold": task_settings.rag_conf_threshold,
                "rag_chunk_size": task_settings.rag_chunk_size,
                "rag_chunk_overlap": task_settings.rag_chunk_overlap,
                "rag_min_chunk_chars": task_settings.rag_min_chunk_chars,
                "rag_max_chunks": task_settings.rag_max_chunks,
                "rag_storage_limit_gb": task_settings.rag_storage_limit_gb,
                "rag_prune_old_indexes": task_settings.rag_prune_old_indexes,
                "rag_pass_score": task_settings.rag_pass_score,
                "rag_low_conf_floor": task_settings.rag_low_conf_floor,
                "rag_web_search_enabled": task_settings.rag_web_search_enabled,
                "rag_web_top_n": task_settings.rag_web_top_n,
                "rag_web_timeout_sec": task_settings.rag_web_timeout_sec,
                "rag_web_weight": task_settings.rag_web_weight,
                "completion_max_courses": task_settings.completion_max_courses,
                "exam_answer_bank_path": task_settings.exam_answer_bank_path,
                "exam_deferred_courses_path": task_settings.exam_deferred_courses_path,
                "exam_quality_report_dir": task_settings.exam_quality_report_dir,
                "exam_auto_retry_max": task_settings.exam_auto_retry_max,
                "exam_retry_requires_answer_index": task_settings.exam_retry_requires_answer_index,
            }
        )

    one_click_button_label = "원클릭 전체 자동 실행 (인덱스 확인 → 수료 자동)" if is_admin else "START"
    one_click_disabled = (not is_admin) and (not sync_ready)
    active_queue_job = queue_manager.find_active_job(owner=queue_owner_key) if queue_owner_key else None
    start_col, stop_col = st.columns([5, 1])
    with start_col:
        run_one_click = st.button(
            one_click_button_label,
            type="primary",
            width='stretch',
            disabled=one_click_disabled,
        )
    with stop_col:
        stop_current_job = st.button(
            "STOP",
            width='stretch',
            disabled=active_queue_job is None,
        )
    if stop_current_job:
        if active_queue_job is not None and queue_manager.cancel_job(
            str(active_queue_job.get("job_id", "")),
            owner=queue_owner_key or None,
        ):
            st.session_state.selected_job_id = str(active_queue_job.get("job_id", st.session_state.get("selected_job_id", "")))
            st.warning("중단 요청을 전송했습니다. 현재 단계 종료 후 작업이 멈춥니다.")
        else:
            st.info("중단할 실행 중 작업이 없습니다.")
    show_flow = st.toggle(
        "실행 알고리즘 보기",
        value=False,
        help="데스크톱/모바일 모두 이 토글을 켜면 START 이후 전체 자동화 흐름도를 확인할 수 있습니다.",
    )
    if show_flow:
        _render_system_flow_diagram(one_click_button_label)

    run_login = False
    run_learning_status = False
    run_first_course = False
    run_complete_lesson = False
    run_exam_probe = False
    run_completion_flow = False
    run_rag_index = False
    run_exam_rag_solve = False
    if is_admin:
        col1, col2, col3, col4, col5, col6, col7, col8, col9 = st.columns(9)
        with col1:
            run_login = st.button("로그인 테스트 실행", width='stretch')
        with col2:
            run_learning_status = st.button("로그인 + 나의 학습현황 이동", width='stretch')
        with col3:
            run_first_course = st.button("첫 과목 학습 시작", width='stretch')
        with col4:
            run_complete_lesson = st.button("강의 순차 완료(첫 행→다음)", width='stretch')
        with col5:
            run_exam_probe = st.button("종합평가 텍스트 탐침", width='stretch')
        with col6:
            run_completion_flow = st.button("수료 순서 자동(진도→시간→시험)", width='stretch')
        with col7:
            run_rag_index = st.button("RAG 인덱스 생성", width='stretch')
        with col8:
            run_exam_rag_solve = st.button("종합평가 LLM 풀이(RAG)", width='stretch')
        with col9:
            if st.button("로그 초기화", width='stretch'):
                st.session_state.logs = []

    if stop_mode_label.startswith("자동"):
        stop_rule = "auto"
    elif stop_mode_label.startswith("Next 버튼"):
        stop_rule = "next_only"
    else:
        stop_rule = "manual"

    if run_one_click:
        job_id = _enqueue_job(
            settings,
            queue_manager,
            "원클릭 전체 자동 실행",
            lambda log_fn, stop_fn, s=task_settings: _run_one_click(
                s,
                check_interval_minutes=timefill_check_interval_min,
                max_timefill_checks=timefill_check_limit,
                force_reindex=one_click_force_reindex,
                log_fn=log_fn,
                stop_requested=stop_fn,
            ),
            owner=queue_owner_key,
            owner_label=queue_owner_label,
            role="admin" if is_admin else "user",
        )
        if job_id:
            st.info(f"원클릭 작업이 큐에 등록되었습니다. id={job_id}")

    if run_login:
        job_id = _enqueue_job(
            settings,
            queue_manager,
            "로그인 테스트",
            lambda log_fn, stop_fn, s=task_settings: _run_automator_method(s, "login", log_fn, stop_fn),
            owner=queue_owner_key,
            owner_label=queue_owner_label,
            role="admin" if is_admin else "user",
        )
        if job_id:
            st.info(f"로그인 테스트 작업이 큐에 등록되었습니다. id={job_id}")

    if run_learning_status:
        job_id = _enqueue_job(
            settings,
            queue_manager,
            "로그인 + 나의 학습현황 이동",
            lambda log_fn, stop_fn, s=task_settings: _run_automator_method(
                s, "login_and_open_learning_status", log_fn, stop_fn
            ),
            owner=queue_owner_key,
            owner_label=queue_owner_label,
            role="admin" if is_admin else "user",
        )
        if job_id:
            st.info(f"작업이 큐에 등록되었습니다. id={job_id}")

    if run_first_course:
        job_id = _enqueue_job(
            settings,
            queue_manager,
            "첫 과목 학습 시작",
            lambda log_fn, stop_fn, s=task_settings: _run_automator_method(
                s, "login_and_enter_first_course", log_fn, stop_fn
            ),
            owner=queue_owner_key,
            owner_label=queue_owner_label,
            role="admin" if is_admin else "user",
        )
        if job_id:
            st.info(f"작업이 큐에 등록되었습니다. id={job_id}")

    if run_complete_lesson:
        job_id = _enqueue_job(
            settings,
            queue_manager,
            "강의 순차 완료",
            lambda log_fn, stop_fn, s=task_settings, sr=stop_rule, ml=manual_lesson_limit: _run_automator_method(
                s,
                "login_and_complete_first_course_lesson",
                log_fn,
                stop_fn,
                stop_rule=sr,
                manual_lesson_limit=ml,
            ),
            owner=queue_owner_key,
            owner_label=queue_owner_label,
            role="admin" if is_admin else "user",
        )
        if job_id:
            st.info(f"작업이 큐에 등록되었습니다. id={job_id}")

    if run_exam_probe:
        job_id = _enqueue_job(
            settings,
            queue_manager,
            "종합평가 텍스트 탐침",
            lambda log_fn, stop_fn, s=task_settings, limit=exam_probe_limit: _run_automator_method(
                s,
                "login_and_probe_comprehensive_exam",
                log_fn,
                stop_fn,
                max_questions=limit,
            ),
            owner=queue_owner_key,
            owner_label=queue_owner_label,
            role="admin" if is_admin else "user",
        )
        if job_id:
            st.info(f"작업이 큐에 등록되었습니다. id={job_id}")

    if run_completion_flow:
        job_id = _enqueue_job(
            settings,
            queue_manager,
            "수료 순서 자동",
            lambda log_fn, stop_fn, s=task_settings: _run_automator_method(
                s,
                "login_and_run_completion_workflow",
                log_fn,
                stop_fn,
                check_interval_minutes=timefill_check_interval_min,
                max_timefill_checks=timefill_check_limit,
            ),
            owner=queue_owner_key,
            owner_label=queue_owner_label,
            role="admin" if is_admin else "user",
        )
        if job_id:
            st.info(f"작업이 큐에 등록되었습니다. id={job_id}")

    if run_rag_index:
        job_id = _enqueue_job(
            settings,
            queue_manager,
            "RAG 인덱스 생성",
            lambda log_fn, stop_fn, s=task_settings: _run_rag_index(s, log_fn, stop_fn),
            owner=queue_owner_key,
            owner_label=queue_owner_label,
            role="admin" if is_admin else "user",
        )
        if job_id:
            st.info(f"작업이 큐에 등록되었습니다. id={job_id}")

    if run_exam_rag_solve:
        job_id = _enqueue_job(
            settings,
            queue_manager,
            "종합평가 LLM 풀이(RAG)",
            lambda log_fn, stop_fn, s=task_settings: _run_automator_method(
                s,
                "login_and_solve_exam_with_rag",
                log_fn,
                stop_fn,
                max_questions=60,
                rag_top_k=rag_top_k,
                confidence_threshold=rag_conf_threshold,
            ),
            owner=queue_owner_key,
            owner_label=queue_owner_label,
            role="admin" if is_admin else "user",
        )
        if job_id:
            st.info(f"작업이 큐에 등록되었습니다. id={job_id}")

    _render_live_queue_and_logs_fragment(
        queue_manager,
        owner=None if is_admin else queue_owner_key,
        compact=not is_admin,
        is_admin=is_admin,
    )

if __name__ == "__main__":
    main()
