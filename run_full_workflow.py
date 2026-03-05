"""
End-to-end workflow: Steve Morse → SSN Validator → Profile Builder → Capital One → First Premier.

Concurrent execution: use run_full_workflow_async() from modules.full_workflow (or this module)
with in-memory template and steps; pass a unique profile_base_index per user/session (0, 3, or 6
for 3 concurrent runs with 10 AdsPower profiles). No global state; each run is isolated.

Telegram bot usage:
  from modules.full_workflow import run_full_workflow_async, STOP_AFTER_CHOICES
  result = await run_full_workflow_async(
      state="Florida",
      template=template_dict,
      capital_one_steps=cap_steps_dict,
      first_premier_steps=fp_steps_dict,
      profile_base_index=session_slot,  # 0, 3, or 6
      log_callback=lambda msg: logger.info(f"[user_{user_id}] {msg}"),
  )

CLI (from repo root):
  python run_full_workflow.py [--state STATE] [--stop-after STEP] [--capital-one-stop-after N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Project root = parent of this file
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from modules.full_workflow import STOP_AFTER_CHOICES, run_full_workflow_async

# Re-export for backward compatibility (e.g. from run_full_workflow import run_full_workflow_async)
__all__ = ["run_full_workflow_async", "STOP_AFTER_CHOICES"]


def _cli_log(msg: str) -> None:
    print(f"[Workflow] {msg}", flush=True)


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Full workflow: Steve Morse → SSN → Profile → Capital One → First Premier",
    )
    parser.add_argument("--state", default="Florida", help="State for Steve Morse (default: Florida)")
    parser.add_argument(
        "--stop-after",
        choices=list(STOP_AFTER_CHOICES),
        default="first_premier",
        help="Stop after this step (default: first_premier = run all)",
    )
    parser.add_argument(
        "--steve-morse-delay",
        type=float,
        default=3.0,
        help="Seconds between Steve Morse group tries (default 3)",
    )
    parser.add_argument("--template", type=Path, default=None, help="Profile template JSON path")
    parser.add_argument("--adspower-api", default="http://127.0.0.1:50325", help="AdsPower API base URL")
    parser.add_argument(
        "--capital-one-stop-after",
        type=int,
        default=None,
        metavar="N",
        help="Stop Capital One after step N (e.g. 1 for quick test)",
    )
    parser.add_argument(
        "--profile-base-index",
        type=int,
        default=0,
        metavar="N",
        help="AdsPower profile base index for this run (0, 3, 6 for 3 concurrent users)",
    )
    args = parser.parse_args()

    template_path = args.template or (ROOT / "modules" / "profile_builder" / "profile_template.json")
    cap_one_steps_path = ROOT / "modules" / "capital_one" / "steps.json"
    first_premier_steps_path = ROOT / "modules" / "first_premier" / "steps.json"

    if not template_path.is_file():
        _cli_log(f"ERROR: Template not found: {template_path}")
        return 1
    if not cap_one_steps_path.is_file():
        _cli_log(f"ERROR: Capital One steps not found: {cap_one_steps_path}")
        return 1
    if not first_premier_steps_path.is_file():
        _cli_log(f"ERROR: First Premier steps not found: {first_premier_steps_path}")
        return 1

    template = json.loads(template_path.read_text(encoding="utf-8"))
    capital_one_steps = json.loads(cap_one_steps_path.read_text(encoding="utf-8"))
    first_premier_steps = json.loads(first_premier_steps_path.read_text(encoding="utf-8"))

    run_result = await run_full_workflow_async(
        state=args.state,
        template=template,
        capital_one_steps=capital_one_steps,
        first_premier_steps=first_premier_steps,
        profile_base_index=args.profile_base_index,
        adspower_api_base=args.adspower_api,
        stop_after=args.stop_after,
        capital_one_stop_after_step=args.capital_one_stop_after,
        steve_morse_delay_seconds=args.steve_morse_delay,
        steve_morse_headless=False,
        log_callback=_cli_log,
    )

    if run_result.get("error"):
        _cli_log(f"ERROR: {run_result['error']}")
    _cli_log(f"Finished in {run_result.get('elapsed_sec', 0)}s. ok={run_result.get('ok')}")
    return 0 if run_result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
