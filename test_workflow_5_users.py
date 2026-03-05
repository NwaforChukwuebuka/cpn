"""
Test: submit 5 workflow jobs to the queue and wait for all to complete.

Uses FullWorkflowQueueService with concurrency_limit=3 (so 3 run in parallel,
2 wait in queue). Short run: Capital One stops after step 1.

Usage (from repo root):
  python test_workflow_5_users.py [--stop-after STEP] [--concurrency N]

Requires: AdsPower running with Local API; profile template and steps in repo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from modules.full_workflow import (
    FullWorkflowQueueService,
    WorkflowJobRequest,
    STOP_AFTER_CHOICES,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Test 5 users through workflow queue")
    parser.add_argument(
        "--stop-after",
        choices=list(STOP_AFTER_CHOICES),
        default="first_premier",
        help="Stop after this step (default: first_premier)",
    )
    parser.add_argument(
        "--capital-one-stop-after",
        type=int,
        default=1,
        metavar="N",
        help="Stop Capital One after step N (default: 1 for quick test)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Max concurrent workflows (default: 3)",
    )
    parser.add_argument(
        "--users",
        type=int,
        default=5,
        help="Number of users/jobs to submit (default: 5)",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="Disable checkpoint store (no resume)",
    )
    args = parser.parse_args()

    template_path = ROOT / "modules" / "profile_builder" / "profile_template.json"
    cap_path = ROOT / "modules" / "capital_one" / "steps.json"
    fp_path = ROOT / "modules" / "first_premier" / "steps.json"
    if not template_path.is_file() or not cap_path.is_file() or not fp_path.is_file():
        print("ERROR: Missing template or steps. Run from repo root.")
        return 1

    template = json.loads(template_path.read_text(encoding="utf-8"))
    capital_one_steps = json.loads(cap_path.read_text(encoding="utf-8"))
    first_premier_steps = json.loads(fp_path.read_text(encoding="utf-8"))

    checkpoint_dir = None if args.no_checkpoint else (ROOT / "data" / "workflow_checkpoints")
    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        from modules.full_workflow import FileWorkflowCheckpointStore
        checkpoint_store = FileWorkflowCheckpointStore(checkpoint_dir)
    else:
        checkpoint_store = None

    service = FullWorkflowQueueService(
        template=template,
        capital_one_steps=capital_one_steps,
        first_premier_steps=first_premier_steps,
        concurrency_limit=args.concurrency,
        adspower_stagger_seconds=3.0,
        checkpoint_store=checkpoint_store,
        resume_from_checkpoint=checkpoint_store is not None,
        profile_base_slots=[0, 3, 6],
    )

    n = args.users
    print(f"Submitting {n} workflow jobs (concurrency={args.concurrency}, stop_after={args.stop_after}, cap_one_stop={args.capital_one_stop_after})...")
    start_wall = time.perf_counter()

    job_ids: list[str] = []
    for i in range(n):
        req = WorkflowJobRequest(
            user_id=f"user_{i+1}",
            state="Florida",
            stop_after=args.stop_after,
            capital_one_stop_after_step=args.capital_one_stop_after,
            steve_morse_delay_seconds=3.0,
        )
        job_id = await service.submit_job(req)
        job_ids.append(job_id)
        print(f"  Submitted job {job_id} for {req.user_id}")

    print(f"\nWaiting for all {n} jobs to complete...")
    results: list[tuple[str, object]] = []
    for jid in job_ids:
        record = await service.wait_for_job(jid)
        results.append((jid, record))

    await service.shutdown()
    total_sec = round(time.perf_counter() - start_wall, 1)

    print(f"\n--- Summary (wall-clock {total_sec}s) ---")
    ok_count = 0
    for jid, record in results:
        if record is None:
            print(f"  {jid}: no record")
            continue
        r = record
        status = getattr(r, "status", "?")
        user_id = getattr(r.request, "user_id", "?")
        err = getattr(r, "error", None)
        started = getattr(r, "started_at", None)
        ended = getattr(r, "ended_at", None)
        elapsed = round(ended - started, 1) if (started is not None and ended is not None) else None
        if status == "succeeded":
            ok_count += 1
        print(f"  {user_id} ({jid[:8]}...): {status}  elapsed={elapsed}s  {err or ''}")
    print(f"\nSuccess: {ok_count}/{n}")
    return 0 if ok_count == n else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
