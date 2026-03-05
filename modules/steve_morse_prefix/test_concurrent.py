"""
Test script: run 10 concurrent "users" through the Steve Morse five-digit decoder.

Each user runs in its own thread with its own browser. Use a short --delay for
faster testing (default 3s); increase for production to avoid rate limits.

Usage (from repo root):
  python -m modules.steve_morse_prefix.test_concurrent [--delay SECS] [--headless]
"""

from __future__ import annotations

import argparse
import asyncio
import time

from .run_five_digit_decoder import async_run_five_digit_decoder


async def run_one_user(user_id: int, state: str, delay_seconds: float, headless: bool) -> dict:
    """Run a single user through the decoder; return result with user_id and timing."""
    start = time.perf_counter()
    result = await async_run_five_digit_decoder(
        state,
        delay_seconds=delay_seconds,
        headless=headless,
    )
    elapsed = time.perf_counter() - start
    return {
        "user_id": user_id,
        "ok": result.get("ok", False),
        "prefix_5": result.get("prefix_5"),
        "state": result.get("state"),
        "error": result.get("error"),
        "elapsed_sec": round(elapsed, 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test 10 concurrent Steve Morse decoder users")
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Seconds between group tries (default 3; use 20+ to avoid rate limits)",
    )
    parser.add_argument(
        "--state",
        default="Florida",
        help="State for all users (default Florida)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browsers headless (default True)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_false",
        dest="headless",
        help="Show browser windows",
    )
    parser.add_argument(
        "-n",
        "--users",
        type=int,
        default=10,
        help="Number of concurrent users (default 10)",
    )
    args = parser.parse_args()

    n = args.users
    print(f"Starting {n} concurrent users (state={args.state!r}, delay={args.delay}s, headless={args.headless})...")
    start_all = time.perf_counter()

    tasks = [
        run_one_user(i + 1, args.state, args.delay, args.headless)
        for i in range(n)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_elapsed = time.perf_counter() - start_all

    # Normalize exceptions to result dicts
    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"user_id": "?", "ok": False, "error": str(r), "elapsed_sec": None})
        else:
            out.append(r)

    ok_count = sum(1 for r in out if r.get("ok"))
    print(f"\nDone in {total_elapsed:.1f}s wall-clock. Success: {ok_count}/{n}")
    for r in out:
        uid = r.get("user_id", "?")
        status = "OK" if r.get("ok") else "FAIL"
        prefix = r.get("prefix_5") or "-"
        err = r.get("error") or ""
        elapsed = r.get("elapsed_sec")
        el_str = f"{elapsed}s" if elapsed is not None else "N/A"
        print(f"  User {uid}: {status}  prefix={prefix}  time={el_str}  {err}")
    print()
    if ok_count == n:
        print("Steve Morse handled all 10 users successfully.")
    else:
        print(f"Some runs failed ({n - ok_count}). Rate limiting or transient errors are possible with low --delay.")


if __name__ == "__main__":
    asyncio.run(main())
