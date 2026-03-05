"""
Test script: run 10 concurrent "users" through the SSN validator.

Each user uses a distinct AdsPower profile from the shared pool. Requires
AdsPower running with Local API and 10 profiles available.

Usage (from repo root):
  python -m modules.ssn_validator.test_concurrent [--input PATH] [-n N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from modules.adspower_profiles import get_profile_for_index
from .run_validator import run_validation_async


async def run_one_user(
    user_id: int,
    partial_cpn: dict,
    adspower_api_base: str,
) -> dict:
    """Run a single user through the validator; return result with user_id and timing."""
    profile_id = get_profile_for_index(user_id - 1)  # 0-based index
    start = time.perf_counter()
    result = await run_validation_async(
        partial_cpn,
        adspower_profile=profile_id,
        adspower_api_base=adspower_api_base,
    )
    elapsed = time.perf_counter() - start
    return {
        "user_id": user_id,
        "profile_id": profile_id,
        "ok": result.get("ok", False),
        "error": result.get("error"),
        "results": result.get("results"),
        "elapsed_sec": round(elapsed, 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test 10 concurrent SSN validator users (AdsPower)")
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        default=Path("data/partial_cpn.json"),
        help="Path to partial_cpn.json (default: data/partial_cpn.json)",
    )
    parser.add_argument(
        "-n",
        "--users",
        type=int,
        default=10,
        help="Number of concurrent users (default 10)",
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
        help="Seconds to wait before starting each user (avoids AdsPower 'too many requests'); 0 = all at once (default 4)",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent.parent
    input_path = args.input if args.input.is_absolute() else root / args.input
    if not input_path.is_file():
        print(f"Error: partial_cpn file not found: {input_path}")
        return
    partial_cpn = json.loads(input_path.read_text(encoding="utf-8"))

    n = args.users
    stagger = args.stagger
    print(f"Starting {n} concurrent SSN validator users (AdsPower profiles 0..{n-1})...")
    if stagger > 0:
        print(f"  Stagger: {stagger}s between each profile start")
    print(f"  partial_cpn: {partial_cpn}")
    start_all = time.perf_counter()

    async def run_with_stagger(i: int):
        if stagger > 0 and i > 0:
            await asyncio.sleep(stagger * i)
        return await run_one_user(i + 1, partial_cpn, args.adspower_api)

    tasks = [run_with_stagger(i) for i in range(n)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_elapsed = time.perf_counter() - start_all

    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"user_id": "?", "profile_id": "?", "ok": False, "error": str(r), "elapsed_sec": None})
        else:
            out.append(r)

    ok_count = sum(1 for r in out if r.get("ok"))
    print(f"\nDone in {total_elapsed:.1f}s wall-clock. Success: {ok_count}/{n}")
    for r in out:
        uid = r.get("user_id", "?")
        pid = r.get("profile_id", "?")
        status = "OK" if r.get("ok") else "FAIL"
        err = r.get("error") or ""
        elapsed = r.get("elapsed_sec")
        el_str = f"{elapsed}s" if elapsed is not None else "N/A"
        res = r.get("results") or {}
        ssn = res.get("ssn", "")
        print(f"  User {uid} (profile {pid}): {status}  time={el_str}  ssn={ssn}  {err}")
    print()
    if ok_count == n:
        print("SSN validator handled all 10 users successfully.")
    else:
        print(f"Some runs failed ({n - ok_count}). Check AdsPower is running and profiles are available.")


if __name__ == "__main__":
    asyncio.run(main())
