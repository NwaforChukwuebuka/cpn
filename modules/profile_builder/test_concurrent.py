"""
Test script: run N concurrent profile builds to verify no shared state and async safety.

Uses build_profile_async() with distinct full_cpn per "user". No browser/AdsPower;
pure in-memory builds. Run from repo root:

  python -m modules.profile_builder.test_concurrent [-n N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .build import build_profile_async, profile_for_output


async def run_one_user(
    user_id: int,
    template: dict,
    full_cpn: dict,
    verification: dict | None,
) -> dict:
    """Build profile for one user; return result with user_id and built cpn."""
    start = time.perf_counter()
    profile, errors = await build_profile_async(
        template,
        full_cpn=full_cpn,
        verification=verification,
    )
    elapsed = time.perf_counter() - start
    cpn = profile.get("cpn", "") if profile else ""
    return {
        "user_id": user_id,
        "ok": bool(profile) and not errors,
        "cpn": cpn,
        "errors": errors,
        "elapsed_sec": round(elapsed, 4),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test N concurrent profile builder users")
    parser.add_argument(
        "-n",
        "--users",
        type=int,
        default=10,
        help="Number of concurrent users (default 10)",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=Path(__file__).resolve().parent / "profile_template.json",
        help="Profile template JSON path",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent.parent
    template_path = args.template if args.template.is_absolute() else args.template
    if not template_path.is_file():
        template_path = Path(__file__).resolve().parent / "profile_template.json"
    if not template_path.is_file():
        print("Error: template not found:", template_path)
        return

    template = json.loads(template_path.read_text(encoding="utf-8"))
    # One base full_cpn; we'll vary last_four per user so each profile has distinct cpn
    base_full_cpn = {
        "ok": True,
        "full": "642-39-0000",  # placeholder, replaced per user
        "last_four": "0000",
        "deceased_check": "no_record",
        "issuance_status": "not_issued",
        "results": {},
    }
    verification = {"ok": True, "status": "ok"}

    n = args.users
    print(f"Starting {n} concurrent profile builds...")
    start_all = time.perf_counter()

    tasks = []
    for i in range(n):
        full_cpn = dict(base_full_cpn)
        last_four = f"{i:04d}"  # 0000, 0001, ..., 0009
        full_cpn["full"] = f"642-39-{last_four}"
        full_cpn["last_four"] = last_four
        tasks.append(run_one_user(i + 1, template, full_cpn, verification))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_elapsed = time.perf_counter() - start_all

    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append({
                "user_id": "?",
                "ok": False,
                "cpn": "",
                "errors": [str(r)],
                "elapsed_sec": None,
            })
        else:
            out.append(r)

    ok_count = sum(1 for r in out if r.get("ok"))
    cpns = {r.get("cpn") for r in out if r.get("cpn")}
    print(f"\nDone in {total_elapsed:.3f}s wall-clock. Success: {ok_count}/{n}")
    print(f"Distinct CPNs built: {len(cpns)} (expected {n})")
    for r in out:
        uid = r.get("user_id", "?")
        status = "OK" if r.get("ok") else "FAIL"
        cpn = r.get("cpn", "")
        errs = r.get("errors") or []
        el = r.get("elapsed_sec")
        el_str = f"{el}s" if el is not None else "N/A"
        err_str = "; ".join(errs[:2]) if errs else ""
        print(f"  User {uid}: {status}  cpn={cpn}  time={el_str}  {err_str}")
    print()
    if ok_count == n and len(cpns) == n:
        print("Profile builder handled all users concurrently with no cross-talk.")
    else:
        print(f"Check: {ok_count}/{n} ok, {len(cpns)} distinct CPNs.")


if __name__ == "__main__":
    asyncio.run(main())
