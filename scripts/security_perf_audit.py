#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import Settings


def _scan_text_for_patterns(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for name, reg in patterns:
        for match in reg.finditer(text):
            findings.append(
                {
                    "name": name,
                    "span": [match.start(), match.end()],
                    "excerpt": text[max(0, match.start() - 24) : min(len(text), match.end() + 24)],
                }
            )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Security/performance quick audit for runtime logs and queue load")
    parser.add_argument("--report-path", default="logs/security_perf_audit_report.json")
    parser.add_argument("--queue-workers", type=int, default=5)
    parser.add_argument("--queue-jobs", type=int, default=25)
    parser.add_argument("--queue-sleep-sec", type=float, default=1.0)
    args = parser.parse_args()

    settings = Settings()
    raw_password = str(settings.user_password or "")
    raw_access_code = str(settings.app_access_code or "")
    sensitive_patterns: list[tuple[str, re.Pattern[str]]] = [
        ("password_field", re.compile(r"(?i)(password|passwd|비밀번호)\s*[:=]\s*[^\s,;]+")),
        ("access_code_field", re.compile(r"(?i)(access_code|접속코드)\s*[:=]\s*[^\s,;]+")),
    ]
    if raw_password:
        sensitive_patterns.append(("raw_user_password", re.compile(re.escape(raw_password))))
    if raw_access_code and len(raw_access_code) >= 8:
        sensitive_patterns.append(("raw_access_code", re.compile(re.escape(raw_access_code))))

    scan_targets = [
        ROOT_DIR / "logs",
        ROOT_DIR / ".runtime" / "job_history",
        ROOT_DIR / "logs" / "security_audit.log",
    ]
    files: list[Path] = []
    for target in scan_targets:
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(p for p in target.rglob("*") if p.is_file())

    leak_findings: list[dict[str, Any]] = []
    scanned_file_count = 0
    for path in files:
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        scanned_file_count += 1
        hits = _scan_text_for_patterns(txt, sensitive_patterns)
        for hit in hits:
            leak_findings.append(
                {
                    "file": str(path),
                    **hit,
                }
            )

    queue_report_path = ROOT_DIR / "logs" / "queue_load_report_security_perf.json"
    queue_cmd = [
        str(ROOT_DIR / ".venv" / "bin" / "python") if (ROOT_DIR / ".venv" / "bin" / "python").exists() else sys.executable,
        str(ROOT_DIR / "scripts" / "queue_load_check.py"),
        "--workers",
        str(max(1, int(args.queue_workers))),
        "--jobs",
        str(max(1, int(args.queue_jobs))),
        "--sleep-sec",
        str(max(0.1, float(args.queue_sleep_sec))),
        "--report-path",
        str(queue_report_path),
    ]
    queue_proc = subprocess.run(queue_cmd, capture_output=True, text=True, check=False)  # noqa: S603
    queue_report: dict[str, Any] = {}
    if queue_report_path.exists():
        try:
            queue_report = json.loads(queue_report_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            queue_report = {}

    payload = {
        "security": {
            "scanned_file_count": scanned_file_count,
            "finding_count": len(leak_findings),
            "findings": leak_findings[:200],
        },
        "performance": {
            "queue_cmd": queue_cmd,
            "queue_returncode": int(queue_proc.returncode),
            "queue_stdout_tail": queue_proc.stdout.splitlines()[-40:],
            "queue_stderr_tail": queue_proc.stderr.splitlines()[-40:],
            "queue_report": queue_report,
        },
    }

    out = Path(args.report_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"security_findings={len(leak_findings)} queue_rc={queue_proc.returncode} report={out}")

    if leak_findings:
        return 1
    if int(queue_proc.returncode) != 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
