"""
List Yourself (listyourself.net) 411 listing form filler.

Fills the single-page form with profile data, then either:
- Solves reCAPTCHA via Anti-Captcha (.env ANTICAPTCHA_API_KEY or --anticaptcha-key) and injects
  the token so the submit button enables and form is submitted, or
- Waits for the user to solve reCAPTCHA in the AdsPower browser, then clicks "Add Listing".

Uses Playwright via AdsPower browser; same data/profile.json as Capital One / First Premier.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from os import environ

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
except ImportError:
    pass

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
DEFAULT_LOG_PATH = DEFAULT_PROJECT_ROOT / "data" / "listyourself_log.json"
DEFAULT_STEP_TIMEOUT_MS = 25_000
DEFAULT_NAV_TIMEOUT_MS = 60_000
DEFAULT_CAPTCHA_WAIT_SECONDS = 120
DEFAULT_ADSPOWER_API = "http://127.0.0.1:50325"
ANTICAPTCHA_API_BASE = "https://api.anti-captcha.com"
ANTICAPTCHA_POLL_INTERVAL_SEC = 3
ANTICAPTCHA_POLL_MAX_WAIT_SEC = 120
LOG_PREFIX = "[List Yourself]"


def _log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


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


def _normalize_error(ex: BaseException) -> str:
    """Return a short, user-friendly error message; avoid dumping Playwright call logs."""
    msg = str(ex).strip()
    if not msg:
        return f"{type(ex).__name__}"
    # Playwright timeout often includes "Call log: ..."; keep only the first line or a short summary
    if "Timeout" in type(ex).__name__ or "Timeout" in msg:
        if "exceeded" in msg:
            return "Navigation or operation timed out (try again or check network)."
        return msg.split("\n")[0][:200]
    if "Call log:" in msg:
        msg = msg.split("Call log:")[0].strip()
    first_line = msg.split("\n")[0].strip()
    return first_line[:300] if len(first_line) > 300 else first_line


def _fill_form(
    page,
    profile: dict,
    steps_data: dict,
    step_timeout_ms: int,
) -> list[str]:
    """Fill the listing form and set defaults. Returns list of errors."""
    errors: list[str] = []
    filled: set[tuple[str, str]] = set()

    # Set select defaults (country prefix, address country)
    for sel_spec in steps_data.get("selects_defaults", []):
        sel = sel_spec.get("selector")
        value = sel_spec.get("value")
        if not sel or not value:
            continue
        try:
            loc = page.locator(sel)
            if _count(loc) > 0:
                loc.first.select_option(value=value)
                _log(f"  Set select {sel_spec.get('label', sel)} = {value}")
        except Exception as e:
            _log(f"  Select {sel}: {e}")
            errors.append(f"Select {sel}: {e}")

    # Set radio defaults (Residential, Call Me)
    for radio_spec in steps_data.get("radio_defaults", []):
        name = radio_spec.get("name")
        value = radio_spec.get("value")
        if not name or not value:
            continue
        try:
            loc = page.locator(f"input[name='{name}'][value='{value}']")
            if _count(loc) > 0 and not loc.first.is_checked():
                loc.first.click()
                _log(f"  Set radio {radio_spec.get('label', name)} = {value}")
        except Exception as e:
            _log(f"  Radio {name}: {e}")

    # Fill text fields from profile
    for field_spec in steps_data.get("fields", []):
        profile_key = field_spec.get("profile_key")
        if not profile_key:
            continue
        value = get_profile_value(profile, profile_key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        value_str = str(value).strip()
        selector = field_spec.get("selector") or ""
        if not selector or (profile_key, selector) in filled:
            continue

        _log(f"  {profile_key}: '{value_str[:25]}{'...' if len(value_str) > 25 else ''}'")

        try:
            loc = page.locator(selector)
            if _count(loc) == 0:
                errors.append(f"No element for {profile_key} ({selector})")
                continue
            first = loc.first
            first.wait_for(state="visible", timeout=step_timeout_ms)
            first.fill(value_str)
            filled.add((profile_key, selector))
        except Exception as e:
            _log(f"  ERROR: {e}")
            errors.append(f"Field {profile_key}: {e}")

    return errors


def _wait_for_recaptcha_then_submit(
    page,
    submit_selector: str,
    wait_seconds: int,
    step_timeout_ms: int,
) -> tuple[bool, str | None]:
    """
    Wait for submit button to become enabled (reCAPTCHA solved), then click it.
    Returns (success, error_message).
    """
    _log(f"  reCAPTCHA: solve it in the AdsPower browser. Waiting up to {wait_seconds}s for submit to enable...")
    try:
        btn = page.locator(submit_selector)
        btn.wait_for(state="visible", timeout=step_timeout_ms)
        # Wait until button is not disabled (reCAPTCHA solved)
        page.wait_for_function(
            "sel => { const el = document.querySelector(sel); return el && !el.disabled; }",
            arg=submit_selector,
            timeout=wait_seconds * 1000,
        )
        _log("  Submit button enabled; clicking Add Listing...")
        btn.click()
        return True, None
    except PlaywrightTimeout:
        return False, f"Submit button did not become enabled within {wait_seconds}s (solve reCAPTCHA in browser)."
    except Exception as e:
        return False, str(e)


def _solve_recaptcha_v2_anticaptcha(
    api_key: str,
    website_url: str,
    website_key: str,
) -> tuple[str | None, str | None]:
    """
    Solve reCAPTCHA v2 via Anti-Captcha API (createTask + getTaskResult).
    Returns (g_recaptcha_response_token, error_message). On success token is non-None.
    """
    if requests is None:
        return None, "requests not installed (pip install requests)"
    create_url = f"{ANTICAPTCHA_API_BASE}/createTask"
    payload = {
        "clientKey": api_key,
        "task": {
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
        },
    }
    try:
        r = requests.post(create_url, json=payload, timeout=30)
        data = r.json() if r.text else {}
        if r.status_code != 200:
            return None, f"Anti-Captcha createTask HTTP {r.status_code}: {r.text[:300]}"
        if data.get("errorId", 0) != 0:
            return None, data.get("errorDescription", data.get("errorCode", "createTask failed"))
        task_id = data.get("taskId")
        if task_id is None:
            return None, "Anti-Captcha response had no taskId"
    except Exception as e:
        return None, f"Anti-Captcha createTask: {e}"

    result_url = f"{ANTICAPTCHA_API_BASE}/getTaskResult"
    deadline = time.monotonic() + ANTICAPTCHA_POLL_MAX_WAIT_SEC
    while time.monotonic() < deadline:
        time.sleep(ANTICAPTCHA_POLL_INTERVAL_SEC)
        try:
            r = requests.post(result_url, json={"clientKey": api_key, "taskId": task_id}, timeout=30)
            data = r.json() if r.text else {}
            if r.status_code != 200:
                continue
            if data.get("errorId", 0) != 0:
                return None, data.get("errorDescription", data.get("errorCode", "getTaskResult error"))
            status = data.get("status")
            if status == "ready":
                solution = data.get("solution") or {}
                token = solution.get("gRecaptchaResponse")
                if token:
                    return token, None
                return None, "Anti-Captcha solution had no gRecaptchaResponse"
            if status == "processing":
                continue
            return None, f"Anti-Captcha unexpected status: {status}"
        except Exception as e:
            _log(f"  getTaskResult: {e}")
            continue
    return None, f"Anti-Captcha timed out after {ANTICAPTCHA_POLL_MAX_WAIT_SEC}s"


def _inject_recaptcha_token_then_submit(
    page,
    token: str,
    submit_selector: str,
    step_timeout_ms: int,
) -> tuple[bool, str | None]:
    """
    Inject the g-recaptcha-response token into the page, call enableBtn(), then click submit.
    The listyourself page uses enableBtn() to enable #submitRequest when reCAPTCHA is solved.
    """
    try:
        page.evaluate(
            """(function(token) {
                var el = document.getElementById("g-recaptcha-response") || document.querySelector("textarea[name=\\"g-recaptcha-response\\"]");
                if (el) el.value = token;
                if (typeof window.enableBtn === "function") window.enableBtn();
            })""",
            token,
        )
        page.wait_for_timeout(500)
        btn = page.locator(submit_selector)
        btn.wait_for(state="visible", timeout=step_timeout_ms)
        if btn.is_disabled():
            return False, "Submit button still disabled after injecting token (callback may not have run)."
        _log("  Token injected; clicking Add Listing...")
        btn.click()
        return True, None
    except Exception as e:
        return False, str(e)


def run_filler(
    profile_path: Path,
    steps_config_path: Path,
    log_path: Path,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    captcha_wait_seconds: int = DEFAULT_CAPTCHA_WAIT_SECONDS,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    leave_browser_open: bool = False,
    anticaptcha_api_key: str | None = None,
) -> dict:
    """
    Run List Yourself filler: open listing page, fill form, solve reCAPTCHA (Anti-Captcha or manual), submit.
    If anticaptcha_api_key is set, reCAPTCHA is solved via Anti-Captcha and token is injected; else waits for user to solve in browser.
    Returns dict with ok, error, submitted, log_path.
    """
    _log(f"Loading profile from {profile_path}...")
    profile = load_json(profile_path)
    if not profile:
        _log(f"ERROR: Could not load profile from {profile_path}")
        return {
            "ok": False,
            "error": f"Could not load profile from {profile_path}",
            "submitted": False,
            "log_path": str(log_path),
        }
    _log("Profile loaded.")

    _log(f"Loading steps from {steps_config_path}...")
    steps_data = load_json(steps_config_path)
    if not steps_data:
        _log("ERROR: Could not load steps config")
        return {
            "ok": False,
            "error": "Could not load steps config",
            "submitted": False,
            "log_path": str(log_path),
        }
    listing_url = steps_data.get("listing_url", "https://www.listyourself.net/ListYourself/listing.jsp")
    submit_selector = steps_data.get("submit_button_selector", "#submitRequest")
    _log("Steps loaded.")

    all_errors: list[str] = []
    submitted = False

    if sync_playwright is None:
        _log("ERROR: Playwright not installed. pip install playwright && playwright install chromium")
        return {
            "ok": False,
            "error": "Playwright not installed. pip install playwright && playwright install chromium",
            "submitted": False,
            "log_path": str(log_path),
        }

    _log("Starting AdsPower profile...")
    ws_url, start_err = _adspower_start(adspower_profile, adspower_api_base)
    if not ws_url:
        _log(f"ERROR: {start_err or 'Failed to start AdsPower'}")
        return {
            "ok": False,
            "error": start_err or f"Failed to start AdsPower profile {adspower_profile}",
            "submitted": False,
            "log_path": str(log_path),
        }
    _log("AdsPower started, connecting browser...")

    try:
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

                _log(f"Navigating to {listing_url}...")
                try:
                    # Use domcontentloaded to avoid timeout on slow resources (ads, etc.)
                    page.goto(listing_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                    page.wait_for_timeout(1500)
                except PlaywrightTimeout:
                    err = "Navigation timed out (site slow or unreachable; try again)."
                    all_errors.append(err)
                    _log(f"Navigation failed: {err}")
                except Exception as e:
                    err = _normalize_error(e)
                    all_errors.append(f"Navigate: {err}")
                    _log(f"Navigation failed: {err}")

                if not all_errors:
                    _log("Waiting for form...")
                    try:
                        page.wait_for_selector("input[name='F_TELNO']", state="visible", timeout=nav_timeout_ms)
                        page.wait_for_timeout(500)
                    except PlaywrightTimeout:
                        err = "Form did not appear in time (page may still be loading)."
                        all_errors.append(err)
                        _log(f"Form wait failed: {err}")
                    except Exception as e:
                        err = _normalize_error(e)
                        all_errors.append(f"Form not visible: {err}")
                        _log(f"Form wait failed: {err}")

                if not all_errors:
                    _log("Filling form...")
                    fill_errors = _fill_form(page, profile, steps_data, step_timeout_ms)
                    if fill_errors:
                        all_errors.extend(fill_errors)
                        _log("Form had fill errors.")
                    else:
                        sitekey = steps_data.get("recaptcha_sitekey", "6LcJsvoqAAAAAEtBQtEYdR8Cun6rbXcyMUICxRgW")
                        if anticaptcha_api_key:
                            _log("Form filled. Solving reCAPTCHA via Anti-Captcha...")
                            recaptcha_token, solve_err = _solve_recaptcha_v2_anticaptcha(
                                anticaptcha_api_key, listing_url, sitekey
                            )
                            if solve_err or not recaptcha_token:
                                all_errors.append(solve_err or "Anti-Captcha failed")
                                _log(f"  {solve_err}")
                            else:
                                ok, err = _inject_recaptcha_token_then_submit(
                                    page, recaptcha_token, submit_selector, step_timeout_ms
                                )
                                if not ok:
                                    all_errors.append(err or "Submit failed")
                                    _log(f"  {err}")
                                else:
                                    submitted = True
                                    _log("Submitted Add Listing.")
                                    try:
                                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                                        page.wait_for_timeout(2000)
                                    except Exception:
                                        pass
                        else:
                            _log("Form filled. Waiting for reCAPTCHA (solve in AdsPower browser)...")
                            ok, err = _wait_for_recaptcha_then_submit(
                                page, submit_selector, captcha_wait_seconds, step_timeout_ms
                            )
                            if not ok:
                                all_errors.append(err or "Submit failed")
                                _log(f"  {err}")
                            else:
                                submitted = True
                                _log("Submitted Add Listing.")
                                try:
                                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                                    page.wait_for_timeout(2000)
                                except Exception:
                                    pass

            except Exception as e:
                err = _normalize_error(e)
                _log(f"Error: {err}")
                all_errors.append(err)
            finally:
                if not leave_browser_open:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as e:
        err = _normalize_error(e)
        _log(f"Unexpected error: {err}")
        all_errors.append(err)
    finally:
        if not leave_browser_open:
            _log("Stopping AdsPower profile...")
            try:
                _adspower_stop(adspower_profile, adspower_api_base)
                _log("AdsPower stopped.")
            except Exception as e:
                _log(f"Warning: could not stop AdsPower: {_normalize_error(e)}")
        else:
            _log("Leaving browser and AdsPower open (close manually when done).")

    result = {
        "ok": len(all_errors) == 0,
        "error": "; ".join(all_errors) if all_errors else None,
        "submitted": submitted,
        "log_path": str(log_path),
    }
    log_entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "product": "listyourself",
        "url": listing_url,
        "profile_path": str(profile_path),
        "submitted": submitted,
        "ok": result["ok"],
        "errors": all_errors,
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log_entry, indent=2), encoding="utf-8")
        _log(f"Wrote log: {log_path}")
    except Exception as e:
        _log(f"Warning: could not write log file: {_normalize_error(e)}")
    _log(f"Done. Submitted: {submitted}. Errors: {len(all_errors)}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List Yourself: Fill 411 listing form from profile.json, wait for reCAPTCHA in AdsPower, submit."
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH, help="profile.json path")
    parser.add_argument("--steps", type=Path, default=DEFAULT_STEPS_CONFIG, help="steps.json path")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH, help="Output log JSON path")
    parser.add_argument(
        "--captcha-wait",
        type=int,
        default=DEFAULT_CAPTCHA_WAIT_SECONDS,
        help=f"Seconds to wait for reCAPTCHA to be solved (default {DEFAULT_CAPTCHA_WAIT_SECONDS})",
    )
    parser.add_argument("--adspower-profile", type=str, default=DEFAULT_ADSPOWER_PROFILE, help="AdsPower profile ID")
    parser.add_argument(
        "--adspower-api",
        type=str,
        default=None,
        help="AdsPower API base (default: http://127.0.0.1:50325)",
    )
    parser.add_argument(
        "--leave-open",
        action="store_true",
        help="Leave AdsPower browser open after run (do not stop profile)",
    )
    parser.add_argument(
        "--anticaptcha-key",
        type=str,
        default=environ.get("ANTICAPTCHA_API_KEY"),
        help="Anti-Captcha API key (or set in .env as ANTICAPTCHA_API_KEY)",
    )
    args = parser.parse_args()

    root = DEFAULT_PROJECT_ROOT
    profile_path = (args.profile if args.profile.is_absolute() else root / args.profile).resolve()
    steps_config_path = (args.steps if args.steps.is_absolute() else root / args.steps).resolve()
    log_path = (args.log if args.log.is_absolute() else root / args.log).resolve()
    api_base = args.adspower_api or DEFAULT_ADSPOWER_API

    result = run_filler(
        profile_path=profile_path,
        steps_config_path=steps_config_path,
        log_path=log_path,
        captcha_wait_seconds=args.captcha_wait,
        adspower_profile=args.adspower_profile,
        adspower_api_base=api_base,
        leave_browser_open=args.leave_open,
        anticaptcha_api_key=args.anticaptcha_key,
    )

    if not result["ok"] and result.get("error"):
        print(result["error"], file=sys.stderr)
    print(f"Submitted: {result['submitted']}; log: {result['log_path']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
