"""
Full workflow runner: Steve Morse -> SSN Validator -> Profile Builder -> Capital One -> First Premier.

Provides:
- A basic async runner for direct usage.
- A resilient async runner with retries/backoff, optional checkpoint resume,
  and optional AdsPower step-gating hook for staggered starts.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from modules.adspower_profiles import get_profile_for_index
from modules.steve_morse_prefix.run_five_digit_decoder import async_run_five_digit_decoder
from modules.ssn_validator.run_validator import run_validation_async
from modules.profile_builder.build import build_profile_async
from modules.capital_one.run_filler import run_filler_async as capital_one_run_filler_async
from modules.first_premier.run_filler import run_filler_async as first_premier_run_filler_async

STOP_AFTER_CHOICES = ("stevemorse", "ssn", "profile", "capital_one", "first_premier")
_STEP_INDEX = {name: idx for idx, name in enumerate(STOP_AFTER_CHOICES)}

# Common transient failure markers from network/browser/runtime layers.
_TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "temporarily unavailable",
    "too many request",
    "429",
    "502",
    "503",
    "504",
    "econnreset",
    "socket",
)


class WorkflowCheckpointStore(Protocol):
    """Storage interface for optional resume checkpoints."""

    async def load(self, job_id: str) -> dict[str, Any] | None:
        ...

    async def save(self, job_id: str, checkpoint: dict[str, Any]) -> None:
        ...


class AdspowerStepGate(Protocol):
    """Hook used by queue services to stagger AdsPower starts."""

    async def wait_turn(self, step_name: str) -> None:
        ...


def _is_transient_error(error: str | None) -> bool:
    if not error:
        return False
    text = error.lower()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


def _elapsed(start_wall: float) -> float:
    return round(time.perf_counter() - start_wall, 2)


async def _run_step_with_retries(
    *,
    step_name: str,
    run_once: Callable[[], Awaitable[dict[str, Any]]],
    retry_attempts: int,
    retry_backoff_seconds: tuple[float, ...],
    log_callback: Callable[[str], None] | None,
) -> tuple[bool, dict[str, Any], str | None]:
    attempts = max(1, retry_attempts)

    for attempt in range(1, attempts + 1):
        try:
            out = await run_once()
        except Exception as exc:  # defensive boundary for worker stability
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        ok = bool(out.get("ok"))
        err = out.get("error")
        if ok:
            return True, out, None

        transient = _is_transient_error(err)
        is_last = attempt >= attempts
        if is_last or not transient:
            return False, out, err

        delay = retry_backoff_seconds[min(attempt - 1, len(retry_backoff_seconds) - 1)] if retry_backoff_seconds else 0.0
        if log_callback is not None:
            try:
                log_callback(
                    f"{step_name}: transient failure on attempt {attempt}/{attempts}; retrying in {delay}s: {err}"
                )
            except Exception:
                pass
        if delay > 0:
            await asyncio.sleep(delay)

    return False, {"ok": False, "error": f"{step_name} failed"}, f"{step_name} failed"


# User-facing progress labels (for non-technical users)
PROGRESS_GETTING_CPN = "⏳ Getting your CPN..."
PROGRESS_VALIDATING_CPN = "⏳ Validating your CPN..."
PROGRESS_BUILDING_PROFILE = "⏳ Building your profile..."
PROGRESS_APPLICATION_STEP_1 = "⏳ Completing your application (step 1 of 2)..."
PROGRESS_APPLICATION_STEP_2 = "⏳ Completing your application (step 2 of 2)..."


async def run_full_workflow_resilient_async(
    *,
    job_id: str | None,
    state: str,
    template: dict[str, Any],
    capital_one_steps: dict[str, Any],
    first_premier_steps: dict[str, Any],
    profile_base_index: int = 0,
    adspower_api_base: str = "http://127.0.0.1:50325",
    stop_after: str = "first_premier",
    capital_one_stop_after_step: int | None = None,
    steve_morse_delay_seconds: float = 3.0,
    steve_morse_headless: bool = False,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
    retry_attempts: int = 3,
    retry_backoff_seconds: tuple[float, ...] = (5.0, 15.0, 45.0),
    adspower_step_gate: AdspowerStepGate | None = None,
    checkpoint_store: WorkflowCheckpointStore | None = None,
    resume_from_checkpoint: bool = True,
) -> dict[str, Any]:
    """
    Resilient workflow runner for high-volume bots.

    Features:
    - Per-step retries with exponential backoff for transient failures.
    - Optional checkpoint save/load for resume-after-failure.
    - Optional shared AdsPower gate to stagger start calls across workers.
    - Stateless execution per invocation; safe for concurrent sessions.
    """
    if stop_after not in STOP_AFTER_CHOICES:
        return {
            "ok": False,
            "error": f"Invalid stop_after: {stop_after}. Must be one of {STOP_AFTER_CHOICES}",
            "elapsed_sec": 0.0,
            "steve_result": None,
            "ssn_result": None,
            "partial_cpn": None,
            "profile": None,
            "cap_result": None,
            "fp_result": None,
            "last_completed": None,
            "stop_after": stop_after,
            "job_id": job_id,
        }

    def _log(msg: str) -> None:
        if log_callback is not None:
            try:
                log_callback(msg)
            except Exception:
                pass

    async def _save_checkpoint(result_obj: dict[str, Any], last_completed: str | None) -> None:
        if checkpoint_store is None or not job_id:
            return
        try:
            await checkpoint_store.save(
                job_id,
                {
                    "job_id": job_id,
                    "last_completed": last_completed,
                    "updated_at": time.time(),
                    "result": result_obj,
                },
            )
        except Exception as exc:
            _log(f"checkpoint save warning: {type(exc).__name__}: {exc}")

    start_wall = time.perf_counter()
    result: dict[str, Any] = {
        "ok": False,
        "error": None,
        "elapsed_sec": 0.0,
        "steve_result": None,
        "ssn_result": None,
        "partial_cpn": None,
        "profile": None,
        "cap_result": None,
        "fp_result": None,
        "last_completed": None,
        "stop_after": stop_after,
        "job_id": job_id,
    }

    # Optional resume path from checkpoint.
    if resume_from_checkpoint and checkpoint_store is not None and job_id:
        try:
            saved = await checkpoint_store.load(job_id)
        except Exception as exc:
            _log(f"checkpoint load warning: {type(exc).__name__}: {exc}")
            saved = None
        if saved and isinstance(saved, dict):
            saved_result = saved.get("result")
            saved_last = saved.get("last_completed")
            if isinstance(saved_result, dict):
                result.update(saved_result)
                result["job_id"] = job_id
            if isinstance(saved_last, str):
                result["last_completed"] = saved_last

    last_completed = result.get("last_completed")
    if isinstance(last_completed, str) and _STEP_INDEX[last_completed] >= _STEP_INDEX[stop_after]:
        result["ok"] = result.get("error") is None
        result["elapsed_sec"] = _elapsed(start_wall)
        return result

    async def _notify(stage: str) -> None:
        if progress_callback is not None:
            try:
                await progress_callback(stage)
            except Exception:
                pass

    # --- Step 1: Steve Morse ---
    if not (last_completed and _STEP_INDEX[last_completed] >= _STEP_INDEX["stevemorse"]):
        _log("Step 1/5: Steve Morse (five-digit decoder)...")
        await _notify(PROGRESS_GETTING_CPN)
        steve_profile_id = get_profile_for_index(profile_base_index)

        async def _run_steve() -> dict[str, Any]:
            if adspower_step_gate is not None:
                await adspower_step_gate.wait_turn("stevemorse")
            return await async_run_five_digit_decoder(
                state,
                delay_seconds=steve_morse_delay_seconds,
                headless=steve_morse_headless,
                adspower_profile=steve_profile_id,
                adspower_api_base=adspower_api_base,
            )

        ok, steve_result, err = await _run_step_with_retries(
            step_name="stevemorse",
            run_once=_run_steve,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            log_callback=log_callback,
        )
        result["steve_result"] = steve_result
        if not ok:
            result["error"] = err or "Steve Morse failed"
            result["elapsed_sec"] = _elapsed(start_wall)
            await _save_checkpoint(result, result.get("last_completed"))
            return result
        result["partial_cpn"] = {
            "area": steve_result.get("area"),
            "group": steve_result.get("group"),
            "prefix_5": steve_result.get("prefix_5"),
        }
        result["last_completed"] = "stevemorse"
        await _save_checkpoint(result, "stevemorse")

    if stop_after == "stevemorse":
        result["ok"] = True
        result["elapsed_sec"] = _elapsed(start_wall)
        return result

    # --- Step 2: SSN Validator ---
    if not (result.get("last_completed") and _STEP_INDEX[result["last_completed"]] >= _STEP_INDEX["ssn"]):
        _log("Step 2/5: SSN Validator...")
        await _notify(PROGRESS_VALIDATING_CPN)
        partial_cpn = result.get("partial_cpn") or {}
        ssn_profile_id = get_profile_for_index(profile_base_index)

        async def _run_ssn() -> dict[str, Any]:
            if adspower_step_gate is not None:
                await adspower_step_gate.wait_turn("ssn")
            return await run_validation_async(
                partial_cpn,
                adspower_profile=ssn_profile_id,
                adspower_api_base=adspower_api_base,
            )

        ok, ssn_result, err = await _run_step_with_retries(
            step_name="ssn",
            run_once=_run_ssn,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            log_callback=log_callback,
        )
        result["ssn_result"] = ssn_result
        if not ok:
            result["error"] = err or "SSN Validator failed"
            result["elapsed_sec"] = _elapsed(start_wall)
            await _save_checkpoint(result, result.get("last_completed"))
            return result
        result["last_completed"] = "ssn"
        await _save_checkpoint(result, "ssn")

    if stop_after == "ssn":
        result["ok"] = True
        result["elapsed_sec"] = _elapsed(start_wall)
        return result

    # --- Step 3: Profile Builder ---
    if not (result.get("last_completed") and _STEP_INDEX[result["last_completed"]] >= _STEP_INDEX["profile"]):
        _log("Step 3/5: Profile Builder...")
        await _notify(PROGRESS_BUILDING_PROFILE)
        full_cpn = result.get("ssn_result")
        profile, build_errors = await build_profile_async(
            template,
            full_cpn=full_cpn,
            verification=None,
        )
        result["profile"] = profile
        if not profile or not profile.get("cpn"):
            msg = "Profile build failed or cpn not set"
            if build_errors:
                msg += "; " + "; ".join(build_errors[:3])
            result["error"] = msg
            result["elapsed_sec"] = _elapsed(start_wall)
            await _save_checkpoint(result, result.get("last_completed"))
            return result
        result["last_completed"] = "profile"
        await _save_checkpoint(result, "profile")

    if stop_after == "profile":
        result["ok"] = True
        result["elapsed_sec"] = _elapsed(start_wall)
        return result

    # --- Step 4: Capital One ---
    if not (result.get("last_completed") and _STEP_INDEX[result["last_completed"]] >= _STEP_INDEX["capital_one"]):
        _log("Step 4/5: Capital One filler...")
        await _notify(PROGRESS_APPLICATION_STEP_1)
        cap_profile_id = get_profile_for_index(profile_base_index + 1)
        profile = result.get("profile")

        async def _run_cap() -> dict[str, Any]:
            if adspower_step_gate is not None:
                await adspower_step_gate.wait_turn("capital_one")
            return await capital_one_run_filler_async(
                profile,
                capital_one_steps,
                log_path=None,
                stop_after_step=capital_one_stop_after_step,
                adspower_profile=cap_profile_id,
                adspower_api_base=adspower_api_base,
            )

        ok, cap_result, err = await _run_step_with_retries(
            step_name="capital_one",
            run_once=_run_cap,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            log_callback=log_callback,
        )
        result["cap_result"] = cap_result
        if not ok:
            result["error"] = err or "Capital One filler failed"
            result["elapsed_sec"] = _elapsed(start_wall)
            await _save_checkpoint(result, result.get("last_completed"))
            return result
        result["last_completed"] = "capital_one"
        await _save_checkpoint(result, "capital_one")

    if stop_after == "capital_one":
        result["ok"] = True
        result["elapsed_sec"] = _elapsed(start_wall)
        return result

    # --- Step 5: First Premier ---
    if not (result.get("last_completed") and _STEP_INDEX[result["last_completed"]] >= _STEP_INDEX["first_premier"]):
        _log("Step 5/5: First Premier filler...")
        await _notify(PROGRESS_APPLICATION_STEP_2)
        fp_profile_id = get_profile_for_index(profile_base_index + 2)
        profile = result.get("profile")

        async def _run_fp() -> dict[str, Any]:
            if adspower_step_gate is not None:
                await adspower_step_gate.wait_turn("first_premier")
            return await first_premier_run_filler_async(
                profile,
                first_premier_steps,
                log_path=None,
                adspower_profile=fp_profile_id,
                adspower_api_base=adspower_api_base,
            )

        ok, fp_result, err = await _run_step_with_retries(
            step_name="first_premier",
            run_once=_run_fp,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            log_callback=log_callback,
        )
        result["fp_result"] = fp_result
        if not ok:
            result["error"] = err or "First Premier filler failed"
            result["elapsed_sec"] = _elapsed(start_wall)
            await _save_checkpoint(result, result.get("last_completed"))
            return result
        result["last_completed"] = "first_premier"
        await _save_checkpoint(result, "first_premier")

    result["ok"] = True
    result["error"] = None
    result["elapsed_sec"] = _elapsed(start_wall)
    return result


async def run_full_workflow_async(
    *,
    state: str,
    template: dict[str, Any],
    capital_one_steps: dict[str, Any],
    first_premier_steps: dict[str, Any],
    profile_base_index: int = 0,
    adspower_api_base: str = "http://127.0.0.1:50325",
    stop_after: str = "first_premier",
    capital_one_stop_after_step: int | None = None,
    steve_morse_delay_seconds: float = 3.0,
    steve_morse_headless: bool = False,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Basic async runner kept for backward compatibility.

    This is a single-attempt variant. For resilient production behavior,
    prefer run_full_workflow_resilient_async().
    """
    return await run_full_workflow_resilient_async(
        job_id=None,
        state=state,
        template=template,
        capital_one_steps=capital_one_steps,
        first_premier_steps=first_premier_steps,
        profile_base_index=profile_base_index,
        adspower_api_base=adspower_api_base,
        stop_after=stop_after,
        capital_one_stop_after_step=capital_one_stop_after_step,
        steve_morse_delay_seconds=steve_morse_delay_seconds,
        steve_morse_headless=steve_morse_headless,
        log_callback=log_callback,
        progress_callback=None,
        retry_attempts=1,
        retry_backoff_seconds=(),
        adspower_step_gate=None,
        checkpoint_store=None,
        resume_from_checkpoint=False,
    )
