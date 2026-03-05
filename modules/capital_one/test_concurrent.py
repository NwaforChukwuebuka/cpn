"""
Test script: run N concurrent Capital One filler "users" (each with its own AdsPower profile).

Requires AdsPower running with Local API. Use --stagger to avoid "too many requests".
Use --stop-after 1 for a quick concurrency test (only first step).

Usage (from repo root):
  python -m modules.capital_one.test_concurrent [-n N] [--stagger SECS] [--stop-after STEP]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from modules.adspower_profiles import get_profile_for_index

from .run_filler import run_filler_async


async def run_one_user(
    user_id: int,
    profile: dict,
    steps_config: dict,
    adspower_api_base: str,
    stop_after_step: int | None,
) -> dict:
    """Run Capital One filler for one user; return result with user_id and timing."""
    profile_id = get_profile_for_index(user_id - 1)
    start = time.perf_counter()
    result = await run_filler_async(
        profile,
        steps_config,
        log_path=None,
        stop_after_step=stop_after_step,
        adspower_profile=profile_id,
        adspower_api_base=adspower_api_base,
    )
    elapsed = time.perf_counter() - start
    return {
        "user_id": user_id,
        "profile_id": profile_id,
        "ok": result.get("ok", False),
        "error": result.get("error"),
        "steps_completed": result.get("steps_completed", 0),
        "elapsed_sec": round(elapsed, 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test N concurrent Capital One filler users (AdsPower)",
    )
    parser.add_argument(
        "-n",
        "--users",
        type=int,
        default=10,
        help="Number of concurrent users (default 10)",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="Path to profile.json (default: data/profile.json)",
    )
    parser.add_argument(
        "--steps",
        type=Path,
        default=None,
        help="Path to steps.json (default: modules/capital_one/steps.json)",
    )
    parser.add_argument(
        "--adspower-api",
        type=str,
        default="http://127.0.0.1:50325",
        help="AdsPower Local API base URL",
    )
    parser.add_argument(
        "--stagger",
        type=float,
        default=4.0,
        metavar="SECS",
        help="Seconds to wait before starting each user (avoids AdsPower rate limit); 0 = all at once (default 4)",
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        default=None,
        metavar="STEP",
        help="Stop after step N (e.g. 1 = only first step, for quick test)",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent.parent
    profile_path = args.profile or root / "data" / "profile.json"
    steps_path = args.steps or Path(__file__).resolve().parent / "steps.json"
    if not profile_path.is_file():
        print(f"Error: profile not found: {profile_path}")
        return
    if not steps_path.is_file():
        print(f"Error: steps config not found: {steps_path}")
        return

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    steps_config = json.loads(steps_path.read_text(encoding="utf-8"))

    n = args.users
    stagger = args.stagger
    stop_after = args.stop_after
    print(f"Starting {n} concurrent Capital One filler users (AdsPower profiles 0..{n-1})...")
    if stagger > 0:
        print(f"  Stagger: {stagger}s between each profile start")
    if stop_after is not None:
        print(f"  Stop after step: {stop_after}")
    start_all = time.perf_counter()

    async def run_with_stagger(i: int):
        if stagger > 0 and i > 0:
            await asyncio.sleep(stagger * i)
        return await run_one_user(
            i + 1,
            profile,
            steps_config,
            args.adspower_api,
            stop_after,
        )

    tasks = [run_with_stagger(i) for i in range(n)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_elapsed = time.perf_counter() - start_all

    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append({
                "user_id": "?",
                "profile_id": "?",
                "ok": False,
                "error": str(r),
                "steps_completed": 0,
                "elapsed_sec": None,
            })
        else:
            out.append(r)

    ok_count = sum(1 for r in out if r.get("ok"))
    print(f"\nDone in {total_elapsed:.1f}s wall-clock. Success: {ok_count}/{n}")
    for r in out:
        uid = r.get("user_id", "?")
        pid = r.get("profile_id", "?")
        status = "OK" if r.get("ok") else "FAIL"
        steps = r.get("steps_completed", 0)
        err = r.get("error") or ""
        el = r.get("elapsed_sec")
        el_str = f"{el}s" if el is not None else "N/A"
        print(f"  User {uid} (profile {pid}): {status}  steps={steps}  time={el_str}  {err}")
    print()
    if ok_count == n:
        print("Capital One filler handled all users successfully.")
    else:
        print(f"Some runs failed ({n - ok_count}). Check AdsPower and --stagger if you see 'too many requests'.")


if __name__ == "__main__":
    asyncio.run(main())
