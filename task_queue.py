from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import queue
import threading
import traceback
import uuid
from typing import Any, Callable


RunnerFn = Callable[[Callable[[str], None]], dict[str, Any]]


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _restrict_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        return


@dataclass
class Job:
    job_id: str
    name: str
    status: str
    created_at: str
    created_ts: float
    started_at: str | None = None
    started_ts: float | None = None
    finished_at: str | None = None
    finished_ts: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    traceback: str | None = None
    owner: str | None = None
    owner_label: str | None = None
    role: str | None = None
    logs: list[str] = field(default_factory=list)


class QueueCapacityError(RuntimeError):
    pass


class TaskQueueManager:
    def __init__(
        self,
        worker_count: int = 1,
        max_logs_per_job: int = 1500,
        max_pending: int = 20,
        max_history: int = 200,
        history_dir: str = ".runtime/job_history",
    ) -> None:
        self.worker_count = max(1, worker_count)
        self.max_logs_per_job = max(200, max_logs_per_job)
        self.max_pending = max(1, max_pending)
        self.max_history = max(self.max_pending + 10, max_history)
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[tuple[str, RunnerFn]] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._runner_registry: dict[str, tuple[str, RunnerFn, str | None, str | None, str | None]] = {}
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._start_workers()

    def _start_workers(self) -> None:
        for idx in range(self.worker_count):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"task-worker-{idx + 1}",
                daemon=True,
            )
            thread.start()
            self._workers.append(thread)

    def submit(
        self,
        name: str,
        runner: RunnerFn,
        owner: str | None = None,
        owner_label: str | None = None,
        role: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        now_ts = now.timestamp()
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id=job_id,
            name=name,
            status="pending",
            created_at=now_iso,
            created_ts=now_ts,
            owner=owner,
            owner_label=owner_label,
            role=role,
        )
        with self._lock:
            pending_or_running = sum(1 for j in self._jobs.values() if j.status in {"pending", "running"})
            running_now = sum(1 for j in self._jobs.values() if j.status == "running")
            pending_now = sum(1 for j in self._jobs.values() if j.status == "pending")
            if pending_or_running >= self.max_pending:
                raise QueueCapacityError(
                    f"큐 대기 한도 초과: 현재 {pending_or_running}개, 한도 {self.max_pending}개"
                )
            self._prune_jobs_unlocked()
            now_iso = _utc_now_iso()
            if running_now >= self.worker_count:
                job.logs.append(
                    f"[{now_iso}] 대기열 등록: 실행 슬롯 사용중({running_now}/{self.worker_count}), 대기 {pending_now + 1}건"
                )
            else:
                job.logs.append(
                    f"[{now_iso}] 즉시 실행 대기: 실행 슬롯 여유({running_now}/{self.worker_count})"
                )
            self._jobs[job_id] = job
            self._runner_registry[job_id] = (name, runner, owner, owner_label, role)
        self._queue.put((job_id, runner))
        return job_id

    def find_active_job(self, owner: str | None = None, name: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            candidates = [
                j
                for j in self._jobs.values()
                if j.status in {"pending", "running"} and (owner is None or j.owner == owner)
            ]
            if name:
                candidates = [j for j in candidates if str(j.name) == str(name)]
            if not candidates:
                return None
            picked = sorted(candidates, key=lambda j: j.created_ts)[0]
            return self._snapshot(picked)

    def owner_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            grouped: dict[str, dict[str, Any]] = {}
            for j in self._jobs.values():
                owner_key = str(j.owner or "unknown")
                row = grouped.setdefault(
                    owner_key,
                    {
                        "owner": owner_key,
                        "owner_label": str(j.owner_label or ""),
                        "pending": 0,
                        "running": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "total": 0,
                        "latest_created_at": j.created_at,
                        "latest_created_ts": float(j.created_ts),
                    },
                )
                row["total"] += 1
                if j.status == "pending":
                    row["pending"] += 1
                elif j.status == "running":
                    row["running"] += 1
                elif j.status == "succeeded":
                    row["succeeded"] += 1
                elif j.status == "failed":
                    row["failed"] += 1
                if float(j.created_ts) >= float(row["latest_created_ts"]):
                    row["latest_created_ts"] = float(j.created_ts)
                    row["latest_created_at"] = j.created_at
                    if j.owner_label:
                        row["owner_label"] = str(j.owner_label)
            rows = sorted(grouped.values(), key=lambda x: float(x["latest_created_ts"]), reverse=True)
            for row in rows:
                row.pop("latest_created_ts", None)
            return rows

    def retry_job(self, job_id: str, owner: str | None = None, role: str | None = None) -> str:
        with self._lock:
            original = self._runner_registry.get(job_id)
            if original is None:
                raise RuntimeError("재시도할 작업 원본을 찾지 못했습니다.")
            name, runner, orig_owner, orig_owner_label, orig_role = original
        return self.submit(
            name=f"{name} (retry)",
            runner=runner,
            owner=owner if owner is not None else orig_owner,
            owner_label=orig_owner_label,
            role=role if role is not None else orig_role,
        )

    def list_jobs(self, limit: int = 20, owner: str | None = None, include_logs: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            jobs = self._jobs.values()
            if owner is not None:
                jobs = [j for j in jobs if j.owner == owner]
            jobs = sorted(jobs, key=lambda j: j.created_ts, reverse=True)
            return [self._snapshot(job, include_logs=include_logs) for job in jobs[: max(1, limit)]]

    def get_job(self, job_id: str, owner: str | None = None, include_logs: bool = True) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if owner is not None and job.owner != owner:
                return None
            return self._snapshot(job, include_logs=include_logs)

    def get_stats(self, owner: str | None = None) -> dict[str, int]:
        with self._lock:
            pending = 0
            running = 0
            succeeded = 0
            failed = 0
            jobs = self._jobs.values()
            if owner is not None:
                jobs = [j for j in jobs if j.owner == owner]
            total = 0
            for job in jobs:
                total += 1
                if job.status == "pending":
                    pending += 1
                elif job.status == "running":
                    running += 1
                elif job.status == "succeeded":
                    succeeded += 1
                elif job.status == "failed":
                    failed += 1
            return {
                "pending": pending,
                "running": running,
                "succeeded": succeeded,
                "failed": failed,
                "total": total,
            }

    def _worker_loop(self) -> None:
        while True:
            job_id, runner = self._queue.get()
            self._set_running(job_id)
            try:
                result = runner(lambda message: self._append_log(job_id, message))
                if not isinstance(result, dict):
                    result = {"message": str(result), "success": True}
                if "success" not in result:
                    result["success"] = True
                if not bool(result.get("success", True)):
                    message = str(result.get("message", "작업 실패")).strip() or "작업 실패"
                    raise RuntimeError(message)
                self._set_finished(job_id, result=result)
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                self._append_log(job_id, f"오류: {exc}")
                self._set_finished(job_id, error=str(exc), tb=tb)
            finally:
                self._queue.task_done()

    def _set_running(self, job_id: str) -> None:
        now = datetime.now(UTC)
        now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        now_ts = now.timestamp()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "running"
            job.started_at = now_iso
            job.started_ts = now_ts
            job.logs.append(f"[{now_iso}] 작업 시작")
            self._trim_logs(job)

    def _set_finished(
        self,
        job_id: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        tb: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        now_ts = now.timestamp()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.finished_at = now_iso
            job.finished_ts = now_ts
            if error:
                job.status = "failed"
                job.error = error
                job.traceback = tb
                job.logs.append(f"[{now_iso}] 작업 실패: {error}")
            else:
                job.status = "succeeded"
                job.result = result
                message = ""
                if result:
                    message = str(result.get("message", ""))
                if message:
                    job.logs.append(f"[{now_iso}] 작업 완료: {message}")
                else:
                    job.logs.append(f"[{now_iso}] 작업 완료")
            self._trim_logs(job)
            snapshot = self._snapshot(job)
        self._persist_job_snapshot(snapshot)

    def _append_log(self, job_id: str, message: str) -> None:
        now_iso = _utc_now_iso()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.logs.append(f"[{now_iso}] {message}")
            self._trim_logs(job)

    def _trim_logs(self, job: Job) -> None:
        if len(job.logs) > self.max_logs_per_job:
            excess = len(job.logs) - self.max_logs_per_job
            del job.logs[:excess]

    def _prune_jobs_unlocked(self) -> None:
        if len(self._jobs) < self.max_history:
            return
        ordered = sorted(self._jobs.values(), key=lambda j: j.created_ts)
        for job in ordered:
            if len(self._jobs) < self.max_history:
                break
            if job.status in {"succeeded", "failed"}:
                self._jobs.pop(job.job_id, None)
                self._runner_registry.pop(job.job_id, None)
        if len(self._jobs) < self.max_history:
            return
        if ordered:
            self._jobs.pop(ordered[0].job_id, None)
            self._runner_registry.pop(ordered[0].job_id, None)

    def _persist_job_snapshot(self, snapshot: dict[str, Any]) -> None:
        try:
            path = self.history_dir / f"{snapshot.get('job_id', 'unknown')}.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            _restrict_file_permissions(path)
        except Exception:  # noqa: BLE001
            return

    def _snapshot(self, job: Job, include_logs: bool = True) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "name": job.name,
            "status": job.status,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "result": job.result,
            "error": job.error,
            "traceback": job.traceback,
            "owner": job.owner,
            "owner_label": job.owner_label,
            "role": job.role,
            "logs": list(job.logs) if include_logs else [],
            "history_path": str((self.history_dir / f"{job.job_id}.json").as_posix()),
        }


_MANAGER: TaskQueueManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_task_queue(
    worker_count: int = 1,
    max_pending: int = 20,
    max_history: int = 200,
) -> TaskQueueManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = TaskQueueManager(
                worker_count=worker_count,
                max_pending=max_pending,
                max_history=max_history,
            )
        return _MANAGER
