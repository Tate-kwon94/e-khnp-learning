from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import queue
import threading
import traceback
import uuid
from typing import Any, Callable


RunnerFn = Callable[[Callable[[str], None]], dict[str, Any]]


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
    ) -> None:
        self.worker_count = max(1, worker_count)
        self.max_logs_per_job = max(200, max_logs_per_job)
        self.max_pending = max(1, max_pending)
        self.max_history = max(self.max_pending + 10, max_history)
        self._queue: queue.Queue[tuple[str, RunnerFn]] = queue.Queue()
        self._jobs: dict[str, Job] = {}
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
        role: str | None = None,
    ) -> str:
        now = datetime.utcnow()
        now_iso = now.isoformat(timespec="seconds") + "Z"
        now_ts = now.timestamp()
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id=job_id,
            name=name,
            status="pending",
            created_at=now_iso,
            created_ts=now_ts,
            owner=owner,
            role=role,
        )
        with self._lock:
            pending_or_running = sum(1 for j in self._jobs.values() if j.status in {"pending", "running"})
            if pending_or_running >= self.max_pending:
                raise QueueCapacityError(
                    f"큐 대기 한도 초과: 현재 {pending_or_running}개, 한도 {self.max_pending}개"
                )
            self._prune_jobs_unlocked()
            self._jobs[job_id] = job
        self._queue.put((job_id, runner))
        return job_id

    def list_jobs(self, limit: int = 20, owner: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            jobs = self._jobs.values()
            if owner is not None:
                jobs = [j for j in jobs if j.owner == owner]
            jobs = sorted(jobs, key=lambda j: j.created_ts, reverse=True)
            return [self._snapshot(job) for job in jobs[: max(1, limit)]]

    def get_job(self, job_id: str, owner: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if owner is not None and job.owner != owner:
                return None
            return self._snapshot(job)

    def get_stats(self, owner: str | None = None) -> dict[str, int]:
        with self._lock:
            pending = 0
            running = 0
            succeeded = 0
            failed = 0
            jobs = self._jobs.values()
            if owner is not None:
                jobs = [j for j in jobs if j.owner == owner]
            for job in jobs:
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
                "total": len(self._jobs),
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
                self._set_finished(job_id, result=result)
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                self._append_log(job_id, f"오류: {exc}")
                self._set_finished(job_id, error=str(exc), tb=tb)
            finally:
                self._queue.task_done()

    def _set_running(self, job_id: str) -> None:
        now = datetime.utcnow()
        now_iso = now.isoformat(timespec="seconds") + "Z"
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
        now = datetime.utcnow()
        now_iso = now.isoformat(timespec="seconds") + "Z"
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

    def _append_log(self, job_id: str, message: str) -> None:
        now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
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
        if len(self._jobs) < self.max_history:
            return
        if ordered:
            self._jobs.pop(ordered[0].job_id, None)

    def _snapshot(self, job: Job) -> dict[str, Any]:
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
            "role": job.role,
            "logs": list(job.logs),
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
