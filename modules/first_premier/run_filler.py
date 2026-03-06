"""
First Premier credit card application filler.

Flow: main page (mypremiercreditcard.com) → click Apply Now → page_1 (About You) →
fill form → Continue → page_2 (address + SSN) → fill → Continue.

Uses Playwright via AdsPower browser, same profile format as Capital One.

Concurrent execution: use run_filler_from_data() or run_filler_async() with in-memory
profile and steps_config. Each session uses its own adspower_profile. No global state;
optional log_callback for per-session logging.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    sync_playwright = None  # type: ignore[misc, assignment]

try:
    import requests
except ImportError:
    requests = None  # type: ignore[misc, assignment]

from modules.adspower_profiles import DEFAULT_ADSPOWER_PROFILE

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PROFILE_PATH = DEFAULT_PROJECT_ROOT / "data" / "profile.json"
DEFAULT_STEPS_CONFIG = Path(__file__).resolve().parent / "steps.json"
DEFAULT_LOG_PATH = DEFAULT_PROJECT_ROOT / "data" / "first_premier_log.json"
DEFAULT_STEP_TIMEOUT_MS = 25_000
DEFAULT_NAV_TIMEOUT_MS = 60_000
DEFAULT_ADSPOWER_API = "http://127.0.0.1:50325"
LOG_PREFIX = "[First Premier]"

_log_callback: threading.local = threading.local()


def _log(msg: str) -> None:
    """Log to callback if set (per-session), else stdout. Safe for concurrent use."""
    cb = getattr(_log_callback, "callback", None)
    if cb is not None:
        try:
            cb(f"{LOG_PREFIX} {msg}")
        except Exception:
            pass
    else:
        print(f"{LOG_PREFIX} {msg}", flush=True)


def _project_root(root: Path | None) -> Path:
    return root or DEFAULT_PROJECT_ROOT


def _adspower_start(profile_id: str, api_base: str) -> tuple[str | None, str | None]:
    """Start AdsPower profile; return (CDP WebSocket URL, error_message)."""
    if requests is None:
        return None, "requests not installed (pip install requests)"
    url = f"{api_base.rstrip('/')}/api/v2/browser-profile/start"
    payload = {"profile_id": profile_id}
    try:
        r = requests.post(url, json=payload, timeout=30)
        body = r.text
        try:
            out = r.json()
        except Exception:
            out = {}
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {body[:500]}"
        if out.get("code") != 0:
            msg = out.get("msg", body) or "start failed"
            return None, f"AdsPower: {msg}"
        ws = (out.get("data") or {}).get("ws") or {}
        cdp_url = ws.get("puppeteer") or ws.get("selenium")
        if not cdp_url:
            return None, f"AdsPower response had no ws.puppeteer: {json.dumps(out)[:300]}"
        return cdp_url, None
    except requests.exceptions.ConnectionError as e:
        return None, f"Cannot reach AdsPower at {api_base}. Is it running with Local API enabled? {e}"
    except Exception as e:
        return None, str(e)


def _adspower_stop(profile_id: str, api_base: str) -> None:
    """Stop AdsPower profile."""
    if requests is None:
        return
    url = f"{api_base.rstrip('/')}/api/v2/browser-profile/stop"
    try:
        requests.post(url, json={"profile_id": profile_id}, timeout=10)
    except BaseException as e:
        _log(f"  Warning: could not stop AdsPower ({type(e).__name__}): {e}")


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def get_profile_value(profile: dict, key: str) -> str | int | bool | None:
    """Resolve dot-notation key from profile."""
    val = profile
    for part in key.split("."):
        if not isinstance(val, dict) or part not in val:
            return None
        val = val[part]
    if val is None:
        return None
    if isinstance(val, (str, int, bool)):
        return val
    return str(val)


def _count(locator) -> int:
    try:
        return locator.count()
    except Exception:
        return 0


# Selectors for the "Verify Address" modal (USPS address not found)
ADDRESS_VERIFICATION_MODAL_SELECTORS = (
    "#AddressVerificationModal",
    "#editAddressButton",
    "[id='AddressVerificationModalTitle']",
)


def _handle_address_verification_modal(page, wait_after_continue_ms: int = 3500) -> bool:
    """
    After Step 2 Continue, the site may show a "Verify Address" modal when USPS
    does not find the address. If the modal appears, click "Edit Address" to
    dismiss it so the user can correct the address. Returns True if the modal
    was found and Edit Address was clicked, False otherwise.
    """
    try:
        page.wait_for_timeout(wait_after_continue_ms)
    except Exception:
        pass
    for sel in ADDRESS_VERIFICATION_MODAL_SELECTORS:
        try:
            loc = page.locator(sel)
            if _count(loc) == 0:
                continue
            el = loc.first
            if not el.is_visible():
                continue
            # If we found the modal container, look for Edit Address button
            if "AddressVerificationModal" in sel or "AddressVerificationModalTitle" in sel:
                edit_btn = page.locator("#editAddressButton")
                if _count(edit_btn) > 0 and edit_btn.first.is_visible():
                    _log("  'Verify Address' modal (address not found by USPS) detected; clicking Edit Address...")
                    edit_btn.first.click()
                    page.wait_for_timeout(500)
                    return True
                continue
            if "editAddressButton" in sel:
                _log("  'Verify Address' modal (address not found by USPS) detected; clicking Edit Address...")
                el.click()
                page.wait_for_timeout(500)
                return True
        except Exception as e:
            _log(f"  (address modal check: {e})")
            continue
    return False


def _fill_address_with_autocomplete(page, address_str: str, step_timeout_ms: int) -> bool:
    """
    Fill #Address using Google Places autocomplete: type, wait for suggestions,
    click first .pac-item. Returns True if a suggestion was selected, False if
    we kept the typed text (no suggestions or error).
    """
    try:
        loc = page.locator("#Address")
        if _count(loc) == 0:
            return False
        first = loc.first
        first.wait_for(state="visible", timeout=step_timeout_ms)
        _log("  Typing address for autocomplete...")
        first.click()
        first.fill("")
        page.wait_for_timeout(300)
        first.type(address_str, delay=60)
        pac = page.locator(".pac-container .pac-item")
        try:
            pac.first.wait_for(state="visible", timeout=7000)
            n = _count(pac)
            _log(f"  {n} autocomplete suggestion(s) found; clicking first...")
            pac.first.click()
            page.wait_for_timeout(500)
            _log("  Autocomplete selection made.")
            return True
        except Exception:
            _log("  No autocomplete suggestions; keeping typed text.")
            try:
                first.press("Escape")
            except Exception:
                pass
            return False
    except Exception as e:
        _log(f"  Address autocomplete error: {e}")
        return False


def _refill_step2_and_continue(
    page,
    profile: dict,
    step_timeout_ms: int,
) -> list[str]:
    """
    After dismissing the Verify Address modal, refill the address form (with
    autocomplete on Address), refill SSN/ConfSSN, and click Continue.
    Returns list of errors (empty if ok).
    """
    errors: list[str] = []
    try:
        page.wait_for_selector("#Address", state="visible", timeout=step_timeout_ms)
        page.wait_for_timeout(500)
    except Exception as e:
        return [f"Address form not visible after Edit Address: {e}"]

    street = get_profile_value(profile, "address.street")
    line2 = get_profile_value(profile, "address.line2")
    city = get_profile_value(profile, "address.city")
    state = get_profile_value(profile, "address.state")
    zipcode = get_profile_value(profile, "address.zip")
    ssn = get_profile_value(profile, "ssn_formatted")

    if street:
        _fill_address_with_autocomplete(page, str(street).strip(), step_timeout_ms)
    if line2 is not None and str(line2).strip():
        try:
            page.locator("#Address2").first.fill(str(line2).strip())
        except Exception as e:
            errors.append(f"Address2: {e}")
    if city:
        try:
            page.locator("#City").first.fill(str(city).strip())
        except Exception as e:
            errors.append(f"City: {e}")
    if state:
        try:
            page.locator("#State").first.select_option(value=str(state).strip())
        except Exception as e:
            errors.append(f"State: {e}")
    if zipcode:
        try:
            page.locator("#ZipCode").first.fill(str(zipcode).strip())
        except Exception as e:
            errors.append(f"ZipCode: {e}")
    if ssn:
        ssn_str = str(ssn).strip()
        try:
            page.locator("#SSN").first.fill(ssn_str)
            page.locator("#ConfSSN").first.fill(ssn_str)
        except Exception as e:
            errors.append(f"SSN: {e}")

    if errors:
        return errors

    _log("  Clicking Continue (after refill)...")
    clicked = False
    for loc in [
        page.locator("form button[type='submit']"),
        page.get_by_role("button", name=re.compile(r"continue", re.I)),
        page.locator("button:has-text('Continue')"),
    ]:
        try:
            if _count(loc) > 0:
                loc.first.wait_for(state="visible", timeout=5000)
                loc.first.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        errors.append("Continue button not found after refill")
    return errors


def fill_step(
    page,
    profile: dict,
    step_spec: dict,
    step_timeout_ms: int,
) -> list[str]:
    """Fill one step (page_1 or page_2). Returns list of errors."""
    errors: list[str] = []
    filled: set[tuple[str, str]] = set()

    for field_spec in step_spec.get("fields", []):
        # Checkbox: check when "check": true or when profile key is truthy
        if field_spec.get("type") == "checkbox":
            should_check = field_spec.get("check") is True
            if not should_check:
                pk = field_spec.get("profile_key")
                if pk:
                    val = get_profile_value(profile, pk)
                    should_check = val is not None and val != "" and val is not False and str(val).strip().lower() not in ("false", "no", "0")
            if not should_check:
                continue
            sel = field_spec.get("selector") or ""
            if not sel:
                continue
            try:
                loc = page.locator(sel)
                if _count(loc) == 0:
                    _log(f"  Checkbox {sel} not found, skipping.")
                    continue
                first = loc.first
                first.wait_for(state="visible", timeout=step_timeout_ms)
                if not first.is_checked():
                    first.check(force=True)
                _log(f"  Checked {sel}.")
            except Exception as e:
                _log(f"  ERROR checkbox {sel}: {e}")
                errors.append(f"Checkbox {sel}: {e}")
            continue

        profile_key = field_spec.get("profile_key")
        if not profile_key:
            continue
        value = get_profile_value(profile, profile_key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        value_str = str(value).strip()
        field_id = field_spec.get("selector") or field_spec.get("label") or profile_key
        if (profile_key, field_id) in filled:
            continue

        _log(f"  Field {profile_key}: filling with '{value_str[:20]}{'...' if len(value_str) > 20 else ''}'")

        locator = None
        if field_spec.get("selector"):
            locator = page.locator(field_spec["selector"])
        if (locator is None or _count(locator) == 0) and field_spec.get("label"):
            locator = page.get_by_label(field_spec["label"], exact=False)
        if (locator is None or _count(locator) == 0) and field_spec.get("placeholder"):
            locator = page.get_by_placeholder(field_spec["placeholder"], exact=False)
        if locator is None or _count(locator) == 0:
            errors.append(f"No matching input for {profile_key}")
            continue
        try:
            first = locator.first
            first.wait_for(state="visible", timeout=step_timeout_ms)
            is_select = first.evaluate("el => el.tagName === 'SELECT'")
            if is_select:
                first.select_option(value=value_str)
            elif field_spec.get("google_autocomplete"):
                _log(f"  Using autocomplete for {profile_key}...")
                first.click()
                first.fill("")
                page.wait_for_timeout(300)
                first.type(value_str, delay=60)
                pac = page.locator(".pac-container .pac-item")
                try:
                    pac.first.wait_for(state="visible", timeout=7000)
                    n = _count(pac)
                    _log(f"  {n} suggestion(s); clicking first...")
                    pac.first.click()
                    page.wait_for_timeout(500)
                except Exception:
                    _log("  No suggestions; keeping typed text.")
                    try:
                        first.press("Escape")
                    except Exception:
                        pass
            else:
                first.fill(value_str)
            filled.add((profile_key, field_id))
            _log(f"  Filled.")
        except Exception as e:
            _log(f"  ERROR: {e}")
            errors.append(f"Field {profile_key}: {e}")

    if errors:
        _log("  Step has errors; NOT clicking Continue.")
        return errors

    btn_text = step_spec.get("continue_button") or "Continue"
    _log(f"  Clicking '{btn_text}'...")
    clicked = False
    patterns = []
    if step_spec.get("continue_selector"):
        patterns.append(page.locator(step_spec["continue_selector"]))
    patterns.extend([
        page.get_by_role("button", name=re.compile(re.escape(btn_text), re.I)),
        page.locator(f"button:has-text('{btn_text}')"),
        page.locator(f"form button[type='submit']"),
    ])
    for loc in patterns:
        try:
            if _count(loc) > 0:
                loc.first.wait_for(state="visible", timeout=5000)
                loc.first.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        errors.append(f"Continue button '{btn_text}' not found")
    else:
        _log(f"  Clicked '{btn_text}'.")

    return errors


def _run_filler_core(
    profile: dict[str, Any],
    steps_list: list[dict[str, Any]],
    main_url: str,
    apply_selectors: list[str],
    log_path: Path | None,
    *,
    profile_path: Path | None = None,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Core filler logic: in-memory profile and steps. Writes log only if log_path set.
    Thread-safe when each call uses its own adspower_profile; use log_callback for per-session logging.
    """
    all_errors: list[str] = []
    steps_completed = 0
    address_verification_modal_shown = False

    if log_callback is not None:
        _log_callback.callback = log_callback
    try:
        if sync_playwright is None:
            _log("ERROR: Playwright not installed. pip install playwright && playwright install chromium")
            return {
                "ok": False,
                "error": "Playwright not installed",
                "steps_completed": 0,
                "log_path": str(log_path) if log_path else None,
            }

        _log("Starting AdsPower profile...")
        ws_url, start_err = _adspower_start(adspower_profile, adspower_api_base)
        if not ws_url:
            _log(f"ERROR: {start_err or 'Failed to start AdsPower'}")
            return {
                "ok": False,
                "error": start_err or f"Failed to start AdsPower profile {adspower_profile}",
                "steps_completed": 0,
                "log_path": str(log_path) if log_path else None,
            }
        _log("AdsPower started, connecting browser...")

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url)
            _log("Browser connected.")
            try:
                page = None
                for ctx in browser.contexts:
                    if ctx.pages:
                        page = ctx.pages[0]
                        break
                if page is None:
                    page = browser.new_page()
                ctx = page.context
                ctx.set_default_navigation_timeout(nav_timeout_ms)
                ctx.set_default_timeout(step_timeout_ms)

                # Force desktop viewport so Apply Now is visible (responsive layout hides it on mobile)
                try:
                    page.set_viewport_size({"width": 1280, "height": 720})
                except Exception:
                    pass

                _log(f"Navigating to {main_url}...")
                try:
                    page.goto(main_url, wait_until="load")
                    page.wait_for_timeout(1500)
                except Exception as e:
                    all_errors.append(f"Navigate to main: {e}")
                    _log(f"Navigation failed: {e}")

                if not all_errors:
                    _log("Clicking Apply Now...")
                    apply_clicked = False
                    for sel in apply_selectors:
                        try:
                            loc = page.locator(sel)
                            if _count(loc) == 0:
                                continue
                            first = loc.first
                            try:
                                first.wait_for(state="visible", timeout=8000)
                                first.click()
                            except Exception:
                                # Element found but hidden (responsive layout); force click
                                first.click(force=True)
                            apply_clicked = True
                            _log(f"Clicked Apply Now via: {sel[:50]}...")
                            break
                        except Exception as e:
                            _log(f"  Selector failed {sel[:40]}: {e}")
                            continue
                    if not apply_clicked:
                        all_errors.append("Could not click Apply Now")
                    else:
                        _log("Waiting for application form (page 1)...")
                        try:
                            page.wait_for_selector("#FirstName", state="visible", timeout=nav_timeout_ms)
                            page.wait_for_timeout(1000)
                        except PlaywrightTimeout:
                            all_errors.append("Page 1 form did not appear after Apply Now")
                        except Exception as e:
                            all_errors.append(f"Wait for page 1: {e}")

                if not all_errors:
                    for i, step_spec in enumerate(steps_list):
                        step_num = step_spec.get("step", i + 1)
                        desc = step_spec.get("description", f"Step {step_num}")
                        _log(f"--- Step {step_num}: {desc} ---")
                        errs = fill_step(page, profile, step_spec, step_timeout_ms)
                        if errs:
                            for e in errs:
                                _log(f"  Error: {e}")
                            all_errors.extend([f"Step {step_num}: {e}" for e in errs])
                            break
                        steps_completed = step_num
                        _log(f"Step {step_num} done.")
                        if step_num == 2:
                            if _handle_address_verification_modal(page, wait_after_continue_ms=3500):
                                address_verification_modal_shown = True
                                _log("  Refilling address form (with autocomplete) and resubmitting...")
                                refill_errs = _refill_step2_and_continue(page, profile, step_timeout_ms)
                                if refill_errs:
                                    for e in refill_errs:
                                        _log(f"  Refill error: {e}")
                                    all_errors.extend(refill_errs)
                                    break
                                if _handle_address_verification_modal(page, wait_after_continue_ms=4000):
                                    msg = "Address still not found by USPS after refill with autocomplete — please correct the address manually and click Continue."
                                    _log(msg)
                                    all_errors.append(msg)
                                    break
                                _log("  Refill and resubmit completed; no modal on second check.")
                        if i + 1 < len(steps_list):
                            next_spec = steps_list[i + 1]
                            check_sel = next_spec.get("advance_check_selector") or (next_spec.get("fields") or [{}])[0].get("selector")
                            if check_sel:
                                first_sel = check_sel.split(",")[0].strip()
                                try:
                                    page.wait_for_selector(first_sel, state="visible", timeout=15000)
                                    page.wait_for_timeout(500)
                                except Exception as e:
                                    _log(f"  Warning: next step selector did not appear: {e}")
                                    all_errors.append(f"Step {step_num + 1} form did not appear: {e}")
                                    break

            except Exception as e:
                _log(f"Exception: {e}")
                all_errors.append(str(e))
            finally:
                _log("Leaving browser open (close manually or stop AdsPower when done).")

        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "product": "first_premier",
            "main_url": main_url,
            "profile_path": str(profile_path) if profile_path else "(in-memory)",
            "steps_completed": steps_completed,
            "ok": len(all_errors) == 0,
            "errors": all_errors,
            "address_verification_modal_shown": address_verification_modal_shown,
        }
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(log_entry, indent=2), encoding="utf-8")
            _log(f"Wrote log: {log_path}")
        _log(f"Done. Steps completed: {steps_completed}. Errors: {len(all_errors)}")

        result: dict[str, Any] = {
            "ok": len(all_errors) == 0,
            "error": "; ".join(all_errors) if all_errors else None,
            "steps_completed": steps_completed,
            "log_path": str(log_path) if log_path else None,
        }
        if log_path is None:
            result["log_entry"] = log_entry
        return result
    finally:
        if hasattr(_log_callback, "callback"):
            delattr(_log_callback, "callback")


def run_filler_from_data(
    profile: dict[str, Any],
    steps_config: dict[str, Any],
    log_path: Path | None = None,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Run First Premier filler from in-memory profile and steps config. Session-isolated;
    use a distinct adspower_profile per concurrent user. If log_path is None, result includes "log_entry".
    """
    if "steps" not in steps_config:
        return {
            "ok": False,
            "error": "steps_config must contain 'steps' list",
            "steps_completed": 0,
            "log_path": None,
        }
    main_url = steps_config.get("main_url", "https://www.mypremiercreditcard.com/")
    apply_selectors = steps_config.get("apply_now_selectors", ["a:has-text('Apply Now')"])
    return _run_filler_core(
        profile,
        steps_config["steps"],
        main_url,
        apply_selectors,
        log_path,
        profile_path=None,
        step_timeout_ms=step_timeout_ms,
        nav_timeout_ms=nav_timeout_ms,
        adspower_profile=adspower_profile,
        adspower_api_base=adspower_api_base,
        log_callback=log_callback,
    )


async def run_filler_async(
    profile: dict[str, Any],
    steps_config: dict[str, Any],
    log_path: Path | None = None,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Async wrapper: runs run_filler_from_data in a thread. Safe for many concurrent users
    when each has a distinct adspower_profile.
    """
    return await asyncio.to_thread(
        run_filler_from_data,
        profile,
        steps_config,
        log_path,
        step_timeout_ms,
        nav_timeout_ms,
        adspower_profile,
        adspower_api_base,
        log_callback,
    )


def run_filler(
    profile_path: Path,
    steps_config_path: Path,
    log_path: Path,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
) -> dict[str, Any]:
    """
    Run First Premier filler from file paths (CLI). For concurrent use, prefer
    run_filler_from_data or run_filler_async with in-memory data.
    """
    _log(f"Loading profile from {profile_path}...")
    profile = load_json(profile_path)
    if not profile:
        _log(f"ERROR: Could not load profile from {profile_path}")
        return {
            "ok": False,
            "error": f"Could not load profile from {profile_path}",
            "steps_completed": 0,
            "log_path": str(log_path),
        }
    _log("Profile loaded.")
    _log(f"Loading steps from {steps_config_path}...")
    steps_data = load_json(steps_config_path)
    if not steps_data or "steps" not in steps_data:
        _log("ERROR: Could not load steps config")
        return {
            "ok": False,
            "error": "Could not load steps config",
            "steps_completed": 0,
            "log_path": str(log_path),
        }
    _log(f"Steps loaded ({len(steps_data['steps'])} steps).")
    main_url = steps_data.get("main_url", "https://www.mypremiercreditcard.com/")
    apply_selectors = steps_data.get("apply_now_selectors", ["a:has-text('Apply Now')"])
    return _run_filler_core(
        profile,
        steps_data["steps"],
        main_url,
        apply_selectors,
        log_path,
        profile_path=profile_path,
        step_timeout_ms=step_timeout_ms,
        nav_timeout_ms=nav_timeout_ms,
        adspower_profile=adspower_profile,
        adspower_api_base=adspower_api_base,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="First Premier: Fill application from main page → Apply Now → page 1 → page 2"
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH, help="profile.json path")
    parser.add_argument("--steps", type=Path, default=DEFAULT_STEPS_CONFIG, help="steps.json path")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH, help="Output log JSON path")
    parser.add_argument("--adspower-profile", type=str, default=DEFAULT_ADSPOWER_PROFILE, help="AdsPower profile ID")
    parser.add_argument("--adspower-api", type=str, default=None, help="AdsPower API base (default: http://127.0.0.1:50325)")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT, help="Project root")
    args = parser.parse_args()

    root = _project_root(args.project_root)
    profile_path = args.profile if args.profile.is_absolute() else root / args.profile
    log_path = args.log if args.log.is_absolute() else root / args.log
    api_base = args.adspower_api or DEFAULT_ADSPOWER_API

    result = run_filler(
        profile_path=profile_path,
        steps_config_path=args.steps,
        log_path=log_path,
        adspower_profile=args.adspower_profile,
        adspower_api_base=api_base,
    )

    if not result["ok"] and result.get("error"):
        print(result["error"], file=sys.stderr)
    print(f"Steps completed: {result['steps_completed']}; log: {result['log_path']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
