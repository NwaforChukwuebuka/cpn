"""
Resilient queue/worker orchestration for full workflow jobs.

Design goals:
- Handle large submission spikes (e.g. 1000 users) safely via queueing.
- Run only N workflows in parallel (limited concurrency).
- Apply retries/backoff and optional checkpoint resume per job.
- Stagger AdsPower step starts globally across workers to reduce API rate-limit failures.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from modules.full_workflow.runner import (
    STOP_AFTER_CHOICES,
    WorkflowCheckpointStore,
    run_full_workflow_resilient_async,
)


class AdsPowerStartGate:
    """
    Cross-worker gate that enforces a minimum delay between AdsPower step starts.

    Share one instance across all workers in the process.
    """

    def __init__(self, stagger_seconds: float = 2.0) -> None:
        self._stagger_seconds = max(0.0, float(stagger_seconds))
        self._next_allowed_monotonic = 0.0
        self._lock = asyncio.Lock()

    async def wait_turn(self, step_name: str) -> None:
        if self._stagger_seconds <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed_monotonic - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed_monotonic = time.monotonic() + self._stagger_seconds


class FileWorkflowCheckpointStore(WorkflowCheckpointStore):
    """JSON-on-disk checkpoint store for resume-after-failure."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)

    def _path_for(self, job_id: str) -> Path:
        safe = "".join(ch for ch in job_id if ch.isalnum() or ch in ("-", "_"))
        return self._directory / f"{safe}.json"

    async def load(self, job_id: str) -> dict[str, Any] | None:
        path = self._path_for(job_id)
        if not path.is_file():
            return None

        def _read() -> dict[str, Any] | None:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None

        return await asyncio.to_thread(_read)

    async def save(self, job_id: str, checkpoint: dict[str, Any]) -> None:
        path = self._path_for(job_id)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")

        await asyncio.to_thread(_write)


@dataclass(slots=True)
class WorkflowJobRequest:
    """Submitted job payload."""

    user_id: str
    state: str = "Florida"
    stop_after: str = "first_premier"
    capital_one_stop_after_step: int | None = None
    steve_morse_delay_seconds: float = 3.0
    profile_base_index: int | None = None
    template: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkflowJobRecord:
    """In-memory state of a queued/running/completed job."""

    job_id: str
    request: WorkflowJobRequest
    status: str = "queued"  # queued | running | succeeded | failed
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    profile_base_index: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "user_id": self.request.user_id,
            "state": self.request.state,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "profile_base_index": self.profile_base_index,
            "error": self.error,
            "has_result": self.result is not None,
            "metadata": self.request.metadata,
        }


class FullWorkflowQueueService:
    """
    Queue + worker pool for resilient full workflow execution.

    Typical usage from Telegram bot:
      1) Create one service on startup.
      2) await service.start()
      3) submit_job() for each user request
      4) optionally await wait_for_job(job_id)
      5) await service.shutdown() on app stop
    """

    def __init__(
        self,
        *,
        template: dict[str, Any],
        capital_one_steps: dict[str, Any],
        first_premier_steps: dict[str, Any],
        concurrency_limit: int = 3,
        adspower_api_base: str = "http://127.0.0.1:50325",
        retry_attempts: int = 3,
        retry_backoff_seconds: tuple[float, ...] = (5.0, 15.0, 45.0),
        adspower_stagger_seconds: float = 2.0,
        checkpoint_store: WorkflowCheckpointStore | None = None,
        resume_from_checkpoint: bool = True,
        profile_base_slots: list[int] | None = None,
        on_job_done: Callable[[WorkflowJobRecord], Awaitable[None] | None] | None = None,
        on_progress: Callable[[WorkflowJobRecord, str], Awaitable[None]] | None = None,
    ) -> None:
        self._template = dict(template)
        self._capital_one_steps = dict(capital_one_steps)
        self._first_premier_steps = dict(first_premier_steps)
        self._concurrency_limit = max(1, int(concurrency_limit))
        self._adspower_api_base = adspower_api_base
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_backoff_seconds = retry_backoff_seconds
        self._checkpoint_store = checkpoint_store
        self._resume_from_checkpoint = resume_from_checkpoint
        self._on_job_done = on_job_done
        self._on_progress = on_progress

        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._records: dict[str, WorkflowJobRecord] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._running = False
        self._records_lock = asyncio.Lock()

        self._adspower_gate = AdsPowerStartGate(stagger_seconds=adspower_stagger_seconds)
        self._profile_slots = asyncio.Queue()
        slots = profile_base_slots or [0, 3, 6]
        for base_idx in slots:
            self._profile_slots.put_nowait(int(base_idx))

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker_loop(i), name=f"workflow-worker-{i}")
            for i in range(self._concurrency_limit)
        ]

    async def shutdown(self) -> None:
        if not self._running:
            return
        self._running = False
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    async def submit_job(self, request: WorkflowJobRequest) -> str:
        if request.stop_after not in STOP_AFTER_CHOICES:
            raise ValueError(f"Invalid stop_after={request.stop_after}; must be one of {STOP_AFTER_CHOICES}")
        if not self._running:
            await self.start()

        job_id = uuid.uuid4().hex
        record = WorkflowJobRecord(job_id=job_id, request=request)
        async with self._records_lock:
            self._records[job_id] = record
        await self._queue.put(job_id)
        return job_id

    async def get_job(self, job_id: str) -> WorkflowJobRecord | None:
        async with self._records_lock:
            return self._records.get(job_id)

    async def list_jobs(self) -> list[dict[str, Any]]:
        async with self._records_lock:
            return [rec.to_dict() for rec in self._records.values()]

    async def wait_for_job(self, job_id: str, timeout_seconds: float | None = None) -> WorkflowJobRecord | None:
        record = await self.get_job(job_id)
        if record is None:
            return None
        if timeout_seconds is None:
            await record.done_event.wait()
        else:
            await asyncio.wait_for(record.done_event.wait(), timeout=timeout_seconds)
        return record

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id is None:
                    return
                await self._run_job(job_id, worker_id)
            finally:
                self._queue.task_done()

    async def _run_job(self, job_id: str, worker_id: int) -> None:
        async with self._records_lock:
            record = self._records.get(job_id)
        if record is None:
            return

        record.status = "running"
        record.started_at = time.time()

        # Notify user that processing has started (first progress message).
        if self._on_progress is not None:
            try:
                await self._on_progress(record, "🚀 Starting automation — 0%")
            except Exception as exc:
                print(f"[WFQ][job={record.job_id}] on_progress (start) failed: {type(exc).__name__}: {exc}")

        leased_slot = False
        profile_base_index = record.request.profile_base_index
        try:
            if profile_base_index is None:
                profile_base_index = await self._profile_slots.get()
                leased_slot = True
            record.profile_base_index = profile_base_index

            def _log(msg: str) -> None:
                # Keep logs session-scoped and worker-scoped.
                print(
                    f"[WFQ][worker={worker_id}][job={record.job_id}][user={record.request.user_id}] {msg}",
                    flush=True,
                )

            async def _progress(stage: str) -> None:
                if self._on_progress is not None:
                    try:
                        await self._on_progress(record, stage)
                    except Exception as exc:
                        print(f"[WFQ][job={record.job_id}] on_progress failed: {type(exc).__name__}: {exc}")

            result = await run_full_workflow_resilient_async(
                job_id=record.job_id,
                state=record.request.state,
                template=record.request.template or self._template,
                capital_one_steps=self._capital_one_steps,
                first_premier_steps=self._first_premier_steps,
                profile_base_index=profile_base_index,
                adspower_api_base=self._adspower_api_base,
                stop_after=record.request.stop_after,
                capital_one_stop_after_step=record.request.capital_one_stop_after_step,
                steve_morse_delay_seconds=record.request.steve_morse_delay_seconds,
                steve_morse_headless=False,
                log_callback=_log,
                progress_callback=_progress,
                retry_attempts=self._retry_attempts,
                retry_backoff_seconds=self._retry_backoff_seconds,
                adspower_step_gate=self._adspower_gate,
                checkpoint_store=self._checkpoint_store,
                resume_from_checkpoint=self._resume_from_checkpoint,
            )

            record.result = result
            record.error = result.get("error")
            record.status = "succeeded" if result.get("ok") else "failed"
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            record.status = "failed"
            record.result = {"ok": False, "error": record.error, "job_id": record.job_id}
        finally:
            if leased_slot and profile_base_index is not None:
                await self._profile_slots.put(profile_base_index)
            record.ended_at = time.time()
            record.done_event.set()
            if self._on_job_done is not None:
                maybe = self._on_job_done(record)
                if asyncio.iscoroutine(maybe):
                    try:
                        await maybe
                    except Exception as exc:
                        print(f"[WFQ][job={record.job_id}] on_job_done callback failed: {type(exc).__name__}: {exc}")

