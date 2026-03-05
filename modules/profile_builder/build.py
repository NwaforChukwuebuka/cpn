"""
Module D — Profile Builder (Steps 5–8).

Builds profile from template and optional full_cpn / verification data.
No browser automation; config and validation only.
Output is used by Module E (e.g. Capital One) to fill applications.

Concurrent execution: use build_profile_from_data() with in-memory dicts for
each session. No global state; each call is isolated. Safe for multiple
Telegram bot users when each passes their own template, full_cpn, and verification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

# Default paths relative to project root (cwd when run from repo)
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TEMPLATE = Path(__file__).resolve().parent / "profile_template.json"
DEFAULT_FULL_CPN = DEFAULT_PROJECT_ROOT / "data" / "full_cpn.json"
DEFAULT_VERIFICATION = DEFAULT_PROJECT_ROOT / "data" / "verification.json"
DEFAULT_OUTPUT = DEFAULT_PROJECT_ROOT / "data" / "profile.json"

# Validation rules (from AUTOMATION_PLAN)
INCOME_MIN = 50_000
INCOME_MAX = 80_000
JOB_TYPE_REQUIRED = "Self Employed"
TIME_AT_ADDRESS_STR = "5 Years 5 Months"
TIME_ON_JOB_STR = "5 Years 5 Months"


def _project_root(root: Path | None) -> Path:
    return root or DEFAULT_PROJECT_ROOT


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def format_address_line(address: dict) -> str:
    parts = [
        address.get("street", "").strip(),
        address.get("city", "").strip(),
        address.get("state", "").strip(),
        address.get("zip", "").strip(),
    ]
    return ", ".join(p for p in parts if p)


def validate_income(income: int | None, min_val: int | None, max_val: int | None) -> list[str]:
    errs = []
    if income is not None and (income < INCOME_MIN or income > INCOME_MAX):
        errs.append(f"annual_income must be between ${INCOME_MIN:,} and ${INCOME_MAX:,}, got {income}")
    if min_val is not None and (min_val < INCOME_MIN or min_val > INCOME_MAX):
        errs.append(f"annual_income_min must be {INCOME_MIN}-{INCOME_MAX}, got {min_val}")
    if max_val is not None and (max_val < INCOME_MIN or max_val > INCOME_MAX):
        errs.append(f"annual_income_max must be {INCOME_MIN}-{INCOME_MAX}, got {max_val}")
    return errs


def validate_job_type(job_type: str | None) -> list[str]:
    if job_type is None or not str(job_type).strip():
        return ["job_type is required"]
    if str(job_type).strip() != JOB_TYPE_REQUIRED:
        return [f"job_type must be '{JOB_TYPE_REQUIRED}', got '{job_type}'"]
    return []


def validate_times(time_at: str | None, time_on_job: str | None) -> list[str]:
    errs = []
    if time_at is not None and TIME_AT_ADDRESS_STR not in (time_at or ""):
        errs.append(f"time_at_address should be '{TIME_AT_ADDRESS_STR}', got '{time_at}'")
    if time_on_job is not None and TIME_ON_JOB_STR not in (time_on_job or ""):
        errs.append(f"time_on_job should be '{TIME_ON_JOB_STR}', got '{time_on_job}'")
    return errs


def profile_for_output(profile: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of profile with internal keys (prefix _) removed, for writing or serialization."""
    return {k: v for k, v in profile.items() if not k.startswith("_")}


def _apply_cpn(profile: dict[str, Any], full_cpn: dict[str, Any] | None) -> None:
    """Set profile['cpn'] from full_cpn dict if present. In-place, stateless per call."""
    if not full_cpn:
        return
    if full_cpn.get("full"):
        profile["cpn"] = full_cpn["full"]
    elif full_cpn.get("ok") and "full" in full_cpn:
        profile["cpn"] = full_cpn["full"]


def _apply_verification(profile: dict[str, Any], verification: dict[str, Any] | None) -> None:
    """Attach verification status to profile for logging. In-place, stateless per call."""
    if verification is None:
        return
    profile["_verification_status"] = verification.get("status")
    profile["_verification_ok"] = verification.get("ok")


def _ensure_ssn_formatted(profile: dict[str, Any]) -> None:
    """Set profile['ssn_formatted'] from profile['cpn']. In-place."""
    cpn = profile.get("cpn")
    if not cpn:
        profile["ssn_formatted"] = ""
        return
    if re.match(r"^\d{3}-\d{2}-\d{4}$", cpn):
        profile["ssn_formatted"] = cpn
        return
    s = re.sub(r"\D", "", cpn)
    if len(s) == 9:
        profile["ssn_formatted"] = f"{s[:3]}-{s[3:5]}-{s[5:]}"
    else:
        profile["ssn_formatted"] = cpn


def build_profile_from_data(
    template: dict[str, Any],
    full_cpn: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """
    Build profile from in-memory template and optional full_cpn / verification.
    No file I/O; stateless and safe for concurrent use. Each caller passes
    their own dicts (e.g. per user session).
    """
    profile = dict(template)
    errors: list[str] = []

    _apply_cpn(profile, full_cpn)
    if not profile.get("cpn"):
        errors.append("cpn not set (missing full_cpn or 'full' field)")

    _apply_verification(profile, verification)

    addr = profile.get("address")
    if isinstance(addr, dict):
        profile["address"] = {**addr, "full": format_address_line(addr)}

    profile["capital_one"] = {
        "legal_first_name": profile.get("first_name", ""),
        "legal_middle_initial": (profile.get("middle_initial") or "").strip()[:1],
        "legal_last_name": profile.get("last_name", ""),
    }

    errors.extend(
        validate_income(
            profile.get("annual_income"),
            profile.get("annual_income_min"),
            profile.get("annual_income_max"),
        )
    )
    errors.extend(validate_job_type(profile.get("job_type")))
    errors.extend(
        validate_times(profile.get("time_at_address"), profile.get("time_on_job"))
    )

    _ensure_ssn_formatted(profile)

    return profile, errors


async def build_profile_async(
    template: dict[str, Any],
    full_cpn: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """
    Async wrapper: runs build_profile_from_data in a thread so the event loop
    is not blocked. Use from async bot handlers for consistent async API.
    """
    return await asyncio.to_thread(
        build_profile_from_data,
        template,
        full_cpn,
        verification,
    )


def build_profile(
    template_path: Path,
    full_cpn_path: Path | None,
    verification_path: Path | None,
    project_root: Path,
) -> tuple[dict[str, Any], list[str]]:
    """
    Build profile from file paths (CLI / single-run). Loads JSON then delegates
    to build_profile_from_data. For concurrent use, prefer build_profile_from_data
    with in-memory dicts.
    """
    template = load_json(template_path)
    if not template:
        return {}, [f"Could not load template: {template_path}"]

    full_cpn = load_json(full_cpn_path) if full_cpn_path and full_cpn_path.is_file() else None
    verification = (
        load_json(verification_path)
        if verification_path and verification_path.is_file()
        else None
    )

    return build_profile_from_data(template, full_cpn=full_cpn, verification=verification)


def main() -> int:
    parser = argparse.ArgumentParser(description="Module D: Build profile.json from template and full_cpn.json")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="Profile template JSON")
    parser.add_argument("--full-cpn", type=Path, default=DEFAULT_FULL_CPN, help="full_cpn.json path")
    parser.add_argument("--verification", type=Path, default=DEFAULT_VERIFICATION, help="verification.json path (optional)")
    parser.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT, help="Output profile.json path")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT, help="Project root for default paths")
    parser.add_argument("--strict", action="store_true", help="Fail on validation warnings (e.g. time_at_address text)")
    args = parser.parse_args()

    project_root = _project_root(args.project_root)
    full_cpn = args.full_cpn if args.full_cpn.is_absolute() else project_root / args.full_cpn
    verification = args.verification if args.verification.is_absolute() else project_root / args.verification
    output = args.output if args.output.is_absolute() else project_root / args.output

    profile, errors = build_profile(
        args.template,
        full_cpn,
        verification,
        project_root,
    )

    if not profile:
        for e in errors:
            print(e, file=sys.stderr)
        return 1

    if errors:
        for e in errors:
            print("Validation:", e, file=sys.stderr)
        if args.strict:
            return 1

    out = profile_for_output(profile)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
