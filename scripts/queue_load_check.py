#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from task_queue import TaskQueueManager


def _make_runner(idx: int, sleep_sec: float):
    def _runner(log_fn):
        log_fn(f"job-{idx} start")
        time.sleep(max(0.05, float(sleep_sec)))
        log_fn(f"job-{idx} done")
        return {"success": True, "message": f"job-{idx} completed", "idx": idx}

    return _runner


def run_load_check(workers: int, jobs: int, sleep_sec: float) -> dict[str, Any]:
    manager = TaskQueueManager(
        worker_count=max(1, int(workers)),
        max_pending=max(int(jobs) + 5, 20),
        max_history=max(int(jobs) + 20, 100),
    )

    job_ids: list[str] = []
    for idx in range(1, jobs + 1):
        job_id = manager.submit(name=f"load-check-{idx}", runner=_make_runner(idx, sleep_sec))
        job_ids.append(job_id)

    started_at = time.time()
    max_running = 0
    max_pending = 0
    pending_seen = False

    while True:
        stats = manager.get_stats()
        running = int(stats.get("running", 0))
        pending = int(stats.get("pending", 0))
        max_running = max(max_running, running)
        max_pending = max(max_pending, pending)
        if pending > 0:
            pending_seen = True

        done = int(stats.get("succeeded", 0)) + int(stats.get("failed", 0))
        if done >= jobs:
            break
        time.sleep(0.15)

    elapsed = time.time() - started_at
    final_jobs = manager.list_jobs(limit=max(jobs, 20))
    failures = [j for j in final_jobs if j.get("status") == "failed"]

    summary = {
        "workers": workers,
        "jobs": jobs,
        "sleep_sec": sleep_sec,
        "elapsed_sec": round(elapsed, 3),
        "max_running_observed": max_running,
        "max_pending_observed": max_pending,
        "pending_seen": pending_seen,
        "all_succeeded": len(failures) == 0,
        "queue_guard_ok": max_running <= workers,
        "queue_overflow_to_pending_ok": (jobs > workers and pending_seen) or (jobs <= workers),
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Task queue load check")
    parser.add_argument("--workers", type=int, default=5, help="worker count")
    parser.add_argument("--jobs", type=int, default=15, help="number of submitted jobs")
    parser.add_argument("--sleep-sec", type=float, default=1.2, help="sleep seconds per job")
    parser.add_argument(
        "--report-path",
        type=str,
        default="logs/queue_load_report.json",
        help="output report path",
    )
    args = parser.parse_args()

    result = run_load_check(workers=max(1, args.workers), jobs=max(1, args.jobs), sleep_sec=max(0.05, args.sleep_sec))

    out_path = Path(args.report_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
