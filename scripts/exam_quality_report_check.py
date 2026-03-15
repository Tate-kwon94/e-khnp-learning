#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_report(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return raw if isinstance(raw, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize exam quality report alignment health")
    parser.add_argument("--report-dir", default="logs/exam_quality_reports")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    rows: list[dict[str, Any]] = []
    alignment_ok = 0
    warning_rows = 0
    actionable_warnings = 0
    legacy_warnings = 0

    for path in sorted(report_dir.glob("exam_quality_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        payload = _read_report(path)
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
        aligned = questions > 0 and matched >= questions and known >= questions
        legacy_format = questions > 0 and detail_coverage < 0.35
        if aligned:
            alignment_ok += 1
        else:
            warning_rows += 1
            if legacy_format:
                legacy_warnings += 1
            else:
                actionable_warnings += 1
        rows.append(
            {
                "path": path.as_posix(),
                "created_at": str(meta.get("created_at", "") or ""),
                "course_title": str(meta.get("course_title", "") or ""),
                "attempt_no": int(meta.get("attempt_no", 0) or 0),
                "questions": questions,
                "matched": matched,
                "known": known,
                "correct": correct,
                "rich_rows": rich_rows,
                "detail_coverage": round(detail_coverage, 3),
                "status": "ok" if aligned else ("legacy-warn" if legacy_format else "warn"),
            }
        )
        if len(rows) >= max(1, int(args.limit)):
            break

    payload = {
        "meta": {
            "created_at": _now_iso(),
            "report_dir": report_dir.as_posix(),
            "limit": max(1, int(args.limit)),
        },
        "summary": {
            "reports": len(rows),
            "alignment_ok": alignment_ok,
            "warnings": actionable_warnings,
            "warning_rows": warning_rows,
            "actionable_warnings": actionable_warnings,
            "legacy_warnings": legacy_warnings,
        },
        "rows": rows,
    }

    out_path = Path(args.report_path) if args.report_path else Path("logs/exam_quality_report_check_latest.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"reports={payload['summary']['reports']} "
        f"alignment_ok={payload['summary']['alignment_ok']} "
        f"warnings={payload['summary']['warnings']} "
        f"warning_rows={payload['summary']['warning_rows']} "
        f"actionable_warnings={payload['summary']['actionable_warnings']} "
        f"legacy_warnings={payload['summary']['legacy_warnings']}"
    )
    print(f"report_path={out_path.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
