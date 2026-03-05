"""
Module E — Capital One credit card application filler.

Reads profile (dict or file) and fills the 8-step Capital One application form
using Playwright via AdsPower browser. Uses steps config (dict or file) for step/field mapping.

Concurrent execution: use run_filler_from_data() or run_filler_async() with in-memory
profile and steps_config. Each session uses its own adspower_profile (e.g. from a pool).
No global state; log and HTML paths are per-call. Optional log_callback for per-session logging.
"""

from __future__ import annotations

import sys
from pathlib import Path

# When run as script (e.g. python modules/capital_one/run_filler.py), ensure project root is on path
if __name__ == "__main__":
    _root = Path(__file__).resolve().parent.parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

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
DEFAULT_LOG_PATH = DEFAULT_PROJECT_ROOT / "data" / "tri_merge_log.json"
CAPITAL_ONE_APPLY_URL = "https://applynow.capitalone.com/?productId=37216"
DEFAULT_STEP_TIMEOUT_MS = 25_000
DEFAULT_NAV_TIMEOUT_MS = 60_000
DEFAULT_ADSPOWER_API = "http://127.0.0.1:50325"
LOG_PREFIX = "[Capital One]"

# Thread-local log callback for concurrent runs; set inside _run_filler_core.
_log_callback: threading.local = threading.local()


def _log(msg: str) -> None:
    """Log to callback if set (per-session), else to stdout. Safe for concurrent use."""
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
    """Start AdsPower profile; return (Puppeteer/CDP WebSocket URL, error_message)."""
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
        cdp_url = ws.get("puppeteer") or ws.get("selenium")  # Playwright uses CDP/puppeteer endpoint
        if not cdp_url:
            return None, f"AdsPower response had no ws.puppeteer: {json.dumps(out)[:300]}"
        return cdp_url, None
    except requests.exceptions.ConnectionError as e:
        return None, f"Cannot reach AdsPower at {api_base}. Is it running with Local API enabled? {e}"
    except Exception as e:
        return None, str(e)


def _adspower_stop(profile_id: str, api_base: str) -> None:
    """Stop AdsPower profile. Never raises — logs and swallows all errors so the main run can finish."""
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


def get_profile_value(profile: dict, key: str) -> str | int | None:
    """Resolve dot-notation key from profile (e.g. capital_one.legal_first_name, address.street)."""
    val = profile
    for part in key.split("."):
        if not isinstance(val, dict) or part not in val:
            return None
        val = val[part]
    if val is None:
        return None
    if isinstance(val, (str, int)):
        return val
    return str(val)


def _get_visible_step_section(page, step_num: int | None = None):
    """
    Return the visible step section locator (cdk stepper content section), or None.
    If step_num is provided, prefer that exact step section.
    """
    normalized_step_num: int | None
    if isinstance(step_num, int):
        normalized_step_num = step_num
    elif isinstance(step_num, str) and step_num.isdigit():
        normalized_step_num = int(step_num)
    else:
        normalized_step_num = None

    selectors: list[str] = []
    if normalized_step_num is not None and normalized_step_num > 0:
        zero_idx = normalized_step_num - 1
        selectors.append(f"section#cdk-stepper-web-shell0-content-{zero_idx}:not(.hidden)")
        selectors.append(f"section#cdk-stepper-web-shell0-content-{zero_idx}")
    selectors.append("section[id^='cdk-stepper-web-shell0-content-']:not(.hidden)")
    selectors.append("section[id^='cdk-stepper-web-shell0-content-']")

    for sel in selectors:
        try:
            sections = page.locator(sel)
            count = _count(sections)
            for i in range(count):
                sec = sections.nth(i)
                try:
                    if not sec.is_visible():
                        continue
                    if normalized_step_num is None:
                        return sec
                    # If we asked for a specific step, ensure counter matches when present.
                    counter = sec.locator(".step-counter")
                    if _count(counter) == 0:
                        return sec
                    text = (counter.first.inner_text() or "").strip()
                    if re.search(rf"\b{normalized_step_num}\s+of\s+8\b", text, re.I):
                        return sec
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _get_step_scope(page, step_num: int | None = None):
    """Return a scope object (visible step section when possible, else page)."""
    section = _get_visible_step_section(page, step_num=step_num)
    return section if section is not None else page


def _get_scope_id_for_log(scope, page) -> str:
    """Return a string describing the active scope (section id or page) for debug logging."""
    if scope is page:
        return "page (no step section)"
    try:
        aid = scope.first.get_attribute("id")
        return aid if aid else "(no id)"
    except Exception as e:
        return f"error: {e}"


def fill_step(
    page,
    profile: dict,
    step_spec: dict,
    step_timeout_ms: int,
    pause_after_fill_seconds: float = 0,
) -> list[str]:
    """
    Fill one step: for each field, resolve value and try label then placeholder;
    then click Continue. Returns list of errors (empty if ok).
    """
    errors: list[str] = []
    # Track filled by (profile_key, field_id) so the same value can fill multiple fields (e.g. SSN + Confirm SSN).
    filled: set[tuple[str, str]] = set()
    scope = _get_step_scope(page, step_spec.get("step"))
    _log(f"  [DEBUG] Active step section: {_get_scope_id_for_log(scope, page)}")

    for field_spec in step_spec.get("fields", []):
        profile_key = field_spec.get("profile_key")
        if not profile_key:
            continue
        field_id = field_spec.get("selector") or field_spec.get("label") or field_spec.get("placeholder") or profile_key
        if (profile_key, field_id) in filled:
            continue
        value = get_profile_value(profile, profile_key)
        is_truthy = value is not None and value != "" and value is not False and str(value).strip().lower() not in ("false", "no", "0")

        # Radio: click the option that matches the step's label.
        # select_when "true" (default): click when profile value is truthy (e.g. us_citizen: true -> "Yes").
        # select_when "false": click when profile value is falsy (e.g. secondary_citizenship: false -> "No").
        if field_spec.get("type") == "radio":
            select_when = (field_spec.get("select_when") or "true").lower()
            should_click = is_truthy if select_when == "true" else (not is_truthy)
            if not should_click:
                continue
            label = field_spec.get("label") or ""
            if not label:
                continue
            _log(f"  Radio: selecting '{label}' for {profile_key}")
            optional = field_spec.get("optional") is True
            try:
                # Prefer explicit selector(s) from steps.json for styled radios.
                clicked = False
                selectors = field_spec.get("selectors") or ([field_spec.get("selector")] if field_spec.get("selector") else [])
                for sel in selectors:
                    if not sel:
                        continue
                    loc = scope.locator(sel)
                    if _count(loc) > 0:
                        _log(f"  Clicking radio via selector: {sel}")
                        loc.first.click()
                        clicked = True
                        break
                if not clicked and optional:
                    # Optional step-2 "either/or" question: find by question text, then click our answer.
                    # Ensures we answer whichever second question is shown (mandatory for Continue).
                    section = None
                    question_pattern = None
                    if label == "No":
                        question_pattern = re.compile(r"another country|citizenship in another", re.I)
                    elif label == "Resident":
                        question_pattern = re.compile(r"residency|resident status|U\.S\. residency", re.I)
                    if question_pattern:
                        # Try fieldset/radiogroup first, then any block that contains question + radios
                        section = scope.locator("fieldset, [role='radiogroup']").filter(has=scope.get_by_text(question_pattern))
                        if _count(section) == 0:
                            section = scope.locator("[class*='field'], [class*='radio'], .form-group").filter(has=scope.get_by_text(question_pattern)).filter(has=scope.locator("input[type=radio], [role='radio']"))
                        if section and _count(section) > 0:
                            opt = section.first.get_by_text(re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
                            if _count(opt) > 0:
                                _log(f"  Clicking '{label}' (found by question text)...")
                                opt.first.click()
                                clicked = True
                if not clicked and not optional:
                    # Try visible label text first (only for required fields).
                    label_loc = scope.get_by_text(re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
                    if _count(label_loc) > 0:
                        _log(f"  Clicking visible label text '{label}'...")
                        label_loc.first.click()
                        clicked = True
                if not clicked and not optional:
                    # Fallback to associated label.
                    label_loc = scope.get_by_label(label, exact=False)
                    if _count(label_loc) > 0:
                        _log(f"  Clicking associated label '{label}'...")
                        label_loc.first.click()
                        clicked = True
                if not clicked:
                    if optional:
                        _log(f"  (question not present, skipping)")
                        continue
                    raise RuntimeError(f"Could not click radio option '{label}'")
                filled.add((profile_key, field_id))
                _log(f"  Selected '{label}'.")
            except Exception as e:
                if optional:
                    _log(f"  (optional, skipping: {e})")
                else:
                    _log(f"  ERROR radio {profile_key}: {e}")
                    errors.append(f"Field {profile_key} (radio): {e}")
            continue

        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        value_str = str(value).strip()
        _log(f"  Field {profile_key}: filling with '{value_str[:20]}{'...' if len(value_str) > 20 else ''}'")

        # Try selector first (for id-based targeting), then label, then placeholder
        locator = None
        if field_spec.get("selector"):
            locator = scope.locator(field_spec["selector"])
        if (locator is None or _count(locator) == 0) and field_spec.get("label"):
            locator = scope.get_by_label(field_spec["label"], exact=False)
        if (locator is None or _count(locator) == 0) and field_spec.get("placeholder"):
            locator = scope.get_by_placeholder(field_spec["placeholder"], exact=False)
        if locator is None or _count(locator) == 0:
            msg = f"No matching input found for {profile_key}"
            _log(f"  ERROR: {msg}")
            errors.append(msg)
            continue
        try:
            n = _count(locator)
            if n == 0:
                continue
            first = locator.first
            _log(f"  Waiting for input to be visible...")
            first.wait_for(state="visible", timeout=step_timeout_ms)
            # <select> elements need select_option(); fill() does not set the value
            is_select = first.evaluate("el => el.tagName === 'SELECT'")
            if is_select:
                _log(f"  Selecting option value '{value_str}'...")
                first.select_option(value=value_str)
                # Confirm the selected value matches what we intended
                selected = first.evaluate("el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text + ' (' + el.value + ')' : '(none)'")
                _log(f"  Confirmed selection: {selected}")
            elif field_spec.get("google_autocomplete"):
                # Google Maps Places autocomplete: type address, wait for suggestion, click it.
                _log(f"  Typing address for Google autocomplete...")
                first.click()
                first.fill("")
                page.wait_for_timeout(300)
                first.type(value_str, delay=60)
                # Wait for .pac-item suggestions to appear
                pac = page.locator(".pac-container .pac-item")
                try:
                    pac.first.wait_for(state="visible", timeout=7000)
                    # Click the first suggestion that best matches
                    count = _count(pac)
                    _log(f"  {count} autocomplete suggestion(s) found; clicking first...")
                    pac.first.click()
                    page.wait_for_timeout(500)
                    _log(f"  Autocomplete selection made.")
                except Exception:
                    # No suggestions appeared — press Escape and leave text as-is
                    _log(f"  Warning: no Google autocomplete suggestions for '{value_str}'; proceeding with typed text.")
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
            continue

    # Do not continue if field errors exist on this step.
    if errors:
        _log("  Step has field errors; NOT clicking Continue.")
        return errors

    # Pause so you can view what was picked before Continue is clicked
    if pause_after_fill_seconds and pause_after_fill_seconds > 0:
        secs = int(pause_after_fill_seconds) if pause_after_fill_seconds >= 1 else pause_after_fill_seconds
        _log(f"  Pausing {secs}s so you can view selections...")
        page.wait_for_timeout(int(pause_after_fill_seconds * 1000))

    # Click Continue (optional continue_selector from steps.json, then fallbacks)
    btn_text = step_spec.get("continue_button") or "Continue"
    _log(f"  Clicking '{btn_text}'...")
    clicked = False
    patterns = []
    search_roots = [scope, page] if scope is not page else [scope]
    if step_spec.get("continue_selector"):
        for root in search_roots:
            patterns.append(root.locator(step_spec["continue_selector"]))
    for root in search_roots:
        patterns.extend([
            root.get_by_role("button", name=re.compile(re.escape(btn_text), re.I)),
            root.get_by_role("link", name=re.compile(re.escape(btn_text), re.I)),
            root.get_by_text(re.compile(re.escape(btn_text), re.I)),
            root.locator(f"button:has-text('{btn_text}')"),
            root.locator(f"input[type='submit'][value*='{btn_text[:4]}']"),
            root.locator(f"[data-testid*='continue'], [data-testid*='submit']"),
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
        _log(f"  ERROR: Continue button '{btn_text}' not found or not clickable")
        errors.append(f"Continue button '{btn_text}' not found or not clickable")
    else:
        _log(f"  Clicked '{btn_text}'.")

    return errors


def _click_agreement_next_button(page) -> bool:
    """Click the Next button on the agreement page (after verifying information). Returns True if clicked."""
    patterns = [
        page.locator("button.next-btn:not(.preapprove-btn)"),  # Next, not Submit my application
        page.get_by_role("button", name=re.compile(r"^next$", re.I)),
        page.locator("button:has-text('Next')"),
    ]
    for loc in patterns:
        try:
            if _count(loc) > 0:
                loc.first.wait_for(state="visible", timeout=5000)
                loc.first.click()
                return True
        except Exception:
            continue
    return False


def _click_agreement_submit_button(page) -> bool:
    """Click the final 'Submit my application' button on the agreement page (after Next). Returns True if clicked."""
    patterns = [
        page.locator("[data-testid='preapprove-cta-button']"),
        page.get_by_role("button", name=re.compile(r"submit\s+my\s+application", re.I)),
        page.locator("button.preapprove-btn"),
        page.locator("button:has-text('Submit my application')"),
    ]
    for loc in patterns:
        try:
            if _count(loc) > 0:
                loc.first.wait_for(state="visible", timeout=10000)
                loc.first.click()
                return True
        except Exception:
            continue
    return False


def _count(locator) -> int:
    try:
        return locator.count()
    except Exception:
        return 0


# Transient loading text that appears briefly after Continue — NOT real errors; always ignore.
_LOADING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^loading[,.]?\s*(please\s*wait)?\.{0,3}$", re.I),
    re.compile(r"^please\s*wait\.{0,3}$", re.I),
    re.compile(r"^submitting\.{0,3}$", re.I),
    re.compile(r"^processing\.{0,3}$", re.I),
)

# Soft address-validation warnings: Capital One shows these but still lets you
# proceed by clicking Continue a second time.  We auto-retry once.
_ADDRESS_SOFT_WARNING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"can'?t validate your residential address", re.I),
    re.compile(r"could not locate that address", re.I),
    re.compile(r"couldn'?t locate that address", re.I),
    re.compile(r"couldn'?t find that address", re.I),
    re.compile(r"unable to (verify|validate|locate).*address", re.I),
    re.compile(r"address.*could not be (found|verified|validated)", re.I),
]

# (pattern, human-readable message) for when the form did not advance after Continue
# (used only after the soft-warning retry has already been attempted)
_FORM_DID_NOT_ADVANCE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"couldn't locate that address", re.I), "couldn't locate that address"),
    (re.compile(r"could not (verify|validate|find).*address", re.I), "address could not be verified"),
    (re.compile(r"invalid address|address could not be verified", re.I), "address could not be verified"),
    (re.compile(r"we couldn't find|unable to verify", re.I), "information could not be verified"),
    (re.compile(r"couldn't find that address", re.I), "couldn't find that address"),
    (re.compile(r"please (enter|verify|check).*address", re.I), "please check address"),
    (re.compile(r"address (is )?not (valid|found|recognized)", re.I), "address is not valid"),
    (re.compile(r"enter a valid.*address", re.I), "enter a valid address"),
]

# CSS selectors for inline form error containers (checked as fast indicator first)
_ERROR_ELEMENT_SELECTORS = (
    "[role='alert']",
    "[data-testid*='error']",
    "[class*='error-message']",
    "[class*='ErrorMessage']",
    "[class*='field-error']",
    "[class*='inline-error']",
    "[class*='form-error']",
    "[class*='helper--error']",   # Capital One: grv-textfield__helper--error
    "[id$='_error_split']",       # Capital One: e.g. PHYSICAL_STREET_ADDRESS_error_split
    ".alert-danger",
    "[aria-live='assertive']",
    "[aria-live='polite']",
)


def _refill_address_on_step3(page, step_spec: dict, profile: dict) -> bool:
    """
    On address step (3), clear and refill the full address before retrying Continue.

    Capital One sometimes switches to a different DOM variant on address-validation
    errors, replacing IDs like #PHYSICAL_STREET_ADDRESS with
    id="address.residential.addressLine1" etc.  This function tries the original
    step-spec selector first, then falls back to the alternative IDs, then to
    label-based lookup.
    """
    _log("  Refilling full address before retry...")
    refilled_any = False
    scope = _get_step_scope(page, 3)
    _log(f"  [DEBUG] Active step section (refill address): {_get_scope_id_for_log(scope, page)}")

    # Mapping from profile-key suffix → Capital One's alternative ID suffix
    _ALT_ID_MAP = {
        "street":  "address.residential.addressLine1",
        "line2":   "address.residential.addressLine2",
        "zip":     "address.residential.zipcode",
        "city":    "address.residential.city",
        "state":   "address.residential.state",
    }

    def _find_field_locator(field_spec: dict):
        """
        Try several selector strategies and return the first that finds an element.
        Returns (locator, found) or (None, False).
        """
        # 1. Original selector from steps.json
        sel = field_spec.get("selector")
        if sel:
            loc = scope.locator(sel)
            if _count(loc) > 0:
                return loc

        # 2. Alternative address.residential.* IDs (post-validation-error DOM variant)
        pkey = field_spec.get("profile_key") or ""
        suffix = pkey.split(".")[-1]
        alt_id = _ALT_ID_MAP.get(suffix)
        if alt_id:
            # Dots in id attr require attribute selector syntax, not #foo.bar
            loc = scope.locator(f'[id="{alt_id}"]')
            if _count(loc) > 0:
                return loc

        # 3. Label-based fallback
        label = field_spec.get("label")
        if label:
            loc = scope.get_by_label(label, exact=False)
            if _count(loc) > 0:
                return loc

        return None

    # ── Pass 1: clear every address field ─────────────────────────────────────
    for field_spec in step_spec.get("fields", []):
        profile_key = field_spec.get("profile_key") or ""
        if not profile_key.startswith("address."):
            continue
        try:
            loc = _find_field_locator(field_spec)
            if loc is None:
                _log(f"  Skipping clear for {profile_key}: field not found on page.")
                continue
            first = loc.first
            first.wait_for(state="visible", timeout=5000)
            is_select = first.evaluate("el => el.tagName === 'SELECT'")
            if is_select:
                try:
                    first.select_option(index=0)
                except Exception:
                    pass
            else:
                # Use JS to wipe the value and fire the input/change events so
                # Angular/React state syncs; avoids click() which causes scroll-jitter.
                first.evaluate(
                    "el => {"
                    "  el.value = '';"
                    "  el.dispatchEvent(new Event('input',  {bubbles:true}));"
                    "  el.dispatchEvent(new Event('change', {bubbles:true}));"
                    "}"
                )
            _log(f"  Cleared {profile_key}.")
        except Exception as e:
            _log(f"  Warning: could not clear {profile_key}: {e}")
            continue

    page.wait_for_timeout(600)  # let Angular re-validate after clearing

    # ── Pass 2: refill every address field from profile ────────────────────────
    for field_spec in step_spec.get("fields", []):
        profile_key = field_spec.get("profile_key") or ""
        if not profile_key.startswith("address."):
            continue

        value = get_profile_value(profile, profile_key)
        if value is None:
            continue
        value_str = str(value).strip()
        if not value_str:
            continue

        try:
            loc = _find_field_locator(field_spec)
            if loc is None:
                _log(f"  Skipping refill for {profile_key}: field not found on page.")
                continue
            first = loc.first
            first.wait_for(state="visible", timeout=5000)

            is_select = first.evaluate("el => el.tagName === 'SELECT'")
            if is_select:
                first.select_option(value=value_str)
                selected = first.evaluate(
                    "el => el.options[el.selectedIndex]"
                    " ? el.options[el.selectedIndex].text + ' (' + el.value + ')'"
                    " : '(none)'"
                )
                _log(f"  Refilled {profile_key}: {selected}")
            elif field_spec.get("google_autocomplete"):
                # Clear first via JS, then type slowly to trigger autocomplete
                first.evaluate(
                    "el => {"
                    "  el.value = '';"
                    "  el.dispatchEvent(new Event('input', {bubbles:true}));"
                    "}"
                )
                page.wait_for_timeout(300)
                first.type(value_str, delay=60)
                pac = page.locator(".pac-container .pac-item")
                try:
                    pac.first.wait_for(state="visible", timeout=6000)
                    pac.first.click()
                    page.wait_for_timeout(500)
                    _log(f"  Refilled {profile_key} via autocomplete.")
                except Exception:
                    _log(f"  Warning: no autocomplete for {profile_key}; using typed text.")
                    try:
                        first.press("Escape")
                    except Exception:
                        pass
            else:
                first.fill(value_str)
                _log(f"  Refilled {profile_key}: {value_str}")

            refilled_any = True
        except Exception as e:
            _log(f"  Warning: could not refill {profile_key}: {e}")
            continue

    _log(f"  Address refill complete ({'some fields updated' if refilled_any else 'no fields updated'}).")
    return refilled_any


def _click_continue_button(page) -> bool:
    """Click the Continue button on whichever step is currently visible. Returns True if clicked."""
    current_step = _get_visible_step_num(page)
    scope = _get_step_scope(page, current_step)
    _log(f"  [DEBUG] Active step section (Continue): {_get_scope_id_for_log(scope, page)}")
    patterns = []
    search_roots = [scope, page] if scope is not page else [scope]
    for root in search_roots:
        patterns.extend([
            root.get_by_role("button", name=re.compile(r"continue", re.I)),
            root.get_by_role("link", name=re.compile(r"continue", re.I)),
            root.locator("button:has-text('Continue')"),
            root.locator("[data-testid*='continue'], [data-testid*='submit']"),
        ])
    for loc in patterns:
        try:
            if _count(loc) > 0:
                loc.first.wait_for(state="visible", timeout=3000)
                loc.first.click()
                return True
        except Exception:
            continue
    return False


def _is_still_on_step(page, step_num: int) -> bool:
    """Check the visible step counter text (e.g., '3 of 8')."""
    try:
        step_text = f"{step_num} of 8"
        counter = page.locator(f".step-counter:has-text('{step_text}')")
        return _count(counter) > 0 and counter.first.is_visible()
    except Exception:
        return False


def _get_visible_step_num(page) -> int | None:
    """Return currently visible step number from the '.step-counter' element (e.g. ' 3 of 8 ')."""
    try:
        counters = page.locator(".step-counter")
        count = _count(counters)
        for i in range(count):
            c = counters.nth(i)
            try:
                if not c.is_visible():
                    continue
                text = (c.inner_text() or "").strip()
                m = re.search(r"\b(\d+)\s+of\s+\d+\b", text, re.I)
                if m:
                    return int(m.group(1))
            except Exception:
                continue
    except Exception:
        pass
    return None


def _wait_for_expected_step(page, expected_step_num: int, timeout_ms: int = 10000) -> bool:
    """Wait until the visible step counter matches expected step number."""
    ticks = max(1, timeout_ms // 500)
    for _ in range(ticks):
        current = _get_visible_step_num(page)
        if current == expected_step_num:
            return True
        page.wait_for_timeout(500)
    return _get_visible_step_num(page) == expected_step_num


def _verify_advanced_to_next_step(
    page,
    next_step_spec: dict,
    current_step_num: int,
    save_html_dir: Path | None = None,
    current_step_spec: dict | None = None,
    profile: dict | None = None,
) -> str | None:
    """
    After clicking Continue, actively poll to determine whether:
      (a) the next step appeared  → return None (all good), or
      (b) a soft address warning appeared → re-select state (step 3) if applicable, click Continue again, re-poll, or
      (c) a hard error appeared after retry → return error string.

    Also saves an error-state HTML snapshot when we detect a failure.
    """
    # Selectors that can prove next step is visible.
    check_selectors: list[str] = []
    raw_check = next_step_spec.get("advance_check_selector")
    if isinstance(raw_check, str) and raw_check.strip():
        check_selectors.append(raw_check.strip())
    elif isinstance(raw_check, list):
        check_selectors.extend([str(s).strip() for s in raw_check if str(s).strip()])

    for f in next_step_spec.get("fields", []):
        sel = f.get("selector")
        if sel and sel not in check_selectors:
            check_selectors.append(sel)
        for s in (f.get("selectors") or []):
            if s and s not in check_selectors:
                check_selectors.append(s)

    first_label = None
    for f in next_step_spec.get("fields", []):
        if f.get("label"):
            first_label = f["label"]
            break

    _log(
        "  Checking if form advanced to next step "
        f"(looking for {check_selectors[0] if check_selectors else (first_label or 'next field')})..."
    )

    MAX_RETRIES = 20
    RETRY_INTERVAL_MS = 5000

    def _is_advanced() -> bool:
        if _get_visible_step_num(page) == current_step_num + 1:
            return True
        next_scope = _get_visible_step_section(page, step_num=current_step_num + 1)
        if next_scope is None:
            return False
        for sel in check_selectors:
            try:
                loc = next_scope.locator(sel)
                if _count(loc) > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
        if first_label:
            try:
                label_loc = next_scope.get_by_label(first_label, exact=False)
                if _count(label_loc) > 0 and label_loc.first.is_visible():
                    return True
            except Exception:
                pass
        return False

    def _check_hard_error() -> str | None:
        """Return error message if a known hard error is visible, else None.
        On step 3 all address-related errors are retryable, so returns None."""
        current_scope = _get_step_scope(page, current_step_num)
        page_text_fragments: list[str] = []
        for err_sel in _ERROR_ELEMENT_SELECTORS:
            try:
                err_loc = current_scope.locator(err_sel)
                err_count = _count(err_loc)
                if err_count == 0:
                    continue
                for i in range(err_count):
                    err_elem = err_loc.nth(i)
                    if not err_elem.is_visible():
                        continue
                    err_text = (err_elem.inner_text() or "").strip()
                    if not err_text:
                        continue
                    if any(p.search(err_text) for p in _LOADING_PATTERNS):
                        continue
                    page_text_fragments.append(err_text)
            except Exception:
                continue
        combined = " ".join(page_text_fragments)
        if not combined:
            return None
        if current_step_num == 3:
            return None
        for pat, msg in _FORM_DID_NOT_ADVANCE_PATTERNS:
            if pat.search(combined):
                return msg
        return None

    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass

    for attempt in range(MAX_RETRIES + 1):
        page.wait_for_timeout(RETRY_INTERVAL_MS)

        if _is_advanced():
            if attempt > 0:
                _log(f"  Form advanced to next step (after {attempt} retry/retries).")
            else:
                _log("  Form advanced to next step.")
            return None

        hard_err = _check_hard_error()
        if hard_err:
            _log(f"  Hard error detected: {hard_err}")
            _save_error_html(page, save_html_dir, current_step_num)
            return f"Step {current_step_num}: form did not advance — {hard_err}"

        if attempt >= MAX_RETRIES:
            break

        _log(f"  Form did not advance; retrying Continue ({attempt + 1}/{MAX_RETRIES})...")

        if current_step_num == 3 and current_step_spec and profile:
            _refill_address_on_step3(page, current_step_spec, profile)

        if _click_continue_button(page):
            _log("  Clicked Continue (retry).")
        else:
            _log("  Warning: could not find Continue button.")

    _save_error_html(page, save_html_dir, current_step_num)
    return f"Step {current_step_num}: form did not advance after {MAX_RETRIES} retries"


def _save_error_html(page, save_html_dir: Path | None, step_num: int) -> None:
    """Save a post-error HTML snapshot so the failure state can be inspected."""
    if save_html_dir is None:
        return
    save_html_dir.mkdir(parents=True, exist_ok=True)
    path = save_html_dir / f"step_{step_num:02d}_error.html"
    try:
        path.write_text(page.content(), encoding="utf-8")
        _log(f"  Saved error HTML: {path.name}")
    except Exception:
        pass


def _save_page_html(page, save_dir: Path, step_num: int) -> None:
    """Save current page HTML for later analysis (step_01_1_of_8.html, etc.)."""
    save_dir.mkdir(parents=True, exist_ok=True)
    filename = f"step_{step_num:02d}_{step_num}_of_8.html"
    path = save_dir / filename
    try:
        html = page.content()
        path.write_text(html, encoding="utf-8")
    except Exception:
        pass


def _save_agreement_page_html(page, save_dir: Path | None) -> None:
    """After step 8, wait for the agreement page to load and save its HTML for later analysis."""
    if save_dir is None:
        return
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / "agreement_page.html"
    try:
        page.wait_for_timeout(4000)
        html = page.content()
        path.write_text(html, encoding="utf-8")
        _log(f"Saved agreement page HTML: {path.name}")
    except Exception as e:
        _log(f"Could not save agreement page HTML: {e}")


def _check_agreement_boxes(page) -> list[str]:
    """
    On the agreement page (after step 8), check the two required authorization checkboxes.
    Note: communication language defaults to English and is intentionally left unchanged.
    """
    errors: list[str] = []
    required_boxes = [
        ("Paperless communications", "ELECTRONIC_COMMUNICATIONS_DISCLOSURE"),
        ("SSN verification authorization", "SSN_VERIFICATION_AUTHORIZATION"),
    ]

    _log("Waiting for agreement page checkboxes...")
    first_box = page.locator(f'input[type="checkbox"]#{required_boxes[0][1]}')
    try:
        first_box.first.wait_for(state="visible", timeout=30000)
    except Exception as e:
        msg = f"Agreement page did not become ready: {e}"
        _log(f"  ERROR: {msg}")
        return [msg]

    for label, box_id in required_boxes:
        try:
            loc = page.locator(f'input[type="checkbox"]#{box_id}')
            if _count(loc) == 0:
                raise RuntimeError(f"Checkbox #{box_id} not found")
            box = loc.first
            box.wait_for(state="visible", timeout=7000)
            if box.is_checked():
                _log(f"  {label}: already checked.")
                continue
            _log(f"  Checking {label}...")
            try:
                box.check(force=True)
            except Exception:
                box.click(force=True)
            if not box.is_checked():
                raise RuntimeError("checkbox did not stay checked")
            _log(f"  {label}: checked.")
        except Exception as e:
            _log(f"  ERROR checking {label}: {e}")
            errors.append(f"{label}: {e}")

    return errors


def _run_filler_core(
    profile: dict[str, Any],
    steps_list: list[dict[str, Any]],
    apply_url: str,
    log_path: Path | None,
    *,
    profile_path: Path | None = None,
    stop_after_step: int | None = None,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    save_html_dir: Path | None = None,
    pause_after_fill_seconds: float = 0,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Core filler logic: takes in-memory profile and steps list. No file I/O for input.
    Writes log to log_path only if log_path is not None. Thread-safe when each
    call uses its own adspower_profile; set log_callback for per-session logging.
    """
    all_errors: list[str] = []
    steps_completed = 0

    if log_callback is not None:
        _log_callback.callback = log_callback
    try:
        if sync_playwright is None:
            _log("ERROR: Playwright not installed. pip install playwright && playwright install chromium")
            return {
                "ok": False,
                "error": "Playwright not installed. pip install playwright && playwright install chromium",
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
                    _log("Created new page.")
                ctx = page.context
                ctx.set_default_navigation_timeout(nav_timeout_ms)
                ctx.set_default_timeout(step_timeout_ms)

                _log(f"Navigating to {apply_url}...")
                nav_ok = False
                last_nav_error: Exception | None = None
                for attempt in (1, 2):
                    try:
                        _log(f"Navigation attempt {attempt}...")
                        page.goto(apply_url, wait_until="load")
                        _log("Load event fired; waiting for application form to render...")
                        nav_ok = True
                        break
                    except Exception as e:
                        last_nav_error = e
                        _log(f"Navigation attempt {attempt} failed: {e}")
                        try:
                            page.wait_for_timeout(1500)
                        except Exception:
                            pass
                if not nav_ok:
                    err_msg = f"Failed to navigate to apply page after retries: {last_nav_error}"
                    all_errors.append(err_msg)
                    _log(f"Navigation failed: {err_msg}")

                if nav_ok:
                    # Wait for step 1 form to be visible (SPA renders after load)
                    step1_spec = steps_list[0] if steps_list else {}
                    step1_first_label = None
                    for f in step1_spec.get("fields", []):
                        if f.get("label") and f.get("type") != "radio":
                            step1_first_label = f["label"]
                            break
                    if step1_first_label:
                        _log(f"Waiting for '{step1_first_label}' to be visible (form ready)...")
                        try:
                            page.get_by_label(step1_first_label, exact=False).first.wait_for(
                                state="visible", timeout=nav_timeout_ms
                            )
                            _log("Application form is visible.")
                            page.wait_for_timeout(500)
                        except Exception as e:
                            _log(f"Form did not appear in time: {e}")
                            all_errors.append(f"Step 1 form not visible: {e}")
                    else:
                        _log("Waiting 1s for page to settle...")
                        try:
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass

                    advance_already_verified_for_step: int | None = None  # set when _verify_advanced_to_next_step confirmed a step

                    for i, step_spec in enumerate(steps_list):
                        step_num = step_spec.get("step", i + 1)
                        if stop_after_step is not None and step_num > stop_after_step:
                            _log(f"Stopping after step (limit {stop_after_step}).")
                            break
                        # Skip re-check if the previous advance verification already confirmed we're on this step.
                        if advance_already_verified_for_step == step_num:
                            _log(f"  Advance to step {step_num} already verified; skipping counter re-check.")
                        elif not _wait_for_expected_step(page, step_num, timeout_ms=15000):
                            current_step = _get_visible_step_num(page)
                            # Step 1 only: if counter is unknown but step-1 form is visible, assume we're on step 1
                            if step_num == 1 and current_step is None:
                                try:
                                    step1_visible = False
                                    # Try selectors from step config first
                                    for f in step_spec.get("fields", []):
                                        sel = f.get("selector")
                                        if not sel:
                                            continue
                                        loc = page.locator(sel)
                                        if _count(loc) > 0 and loc.first.is_visible():
                                            step1_visible = True
                                            break
                                    # Fallback by labels if selector check did not succeed
                                    if not step1_visible:
                                        for f in step_spec.get("fields", []):
                                            lbl = f.get("label")
                                            if not lbl:
                                                continue
                                            loc = page.get_by_label(lbl, exact=False)
                                            if _count(loc) > 0 and loc.first.is_visible():
                                                step1_visible = True
                                                break
                                    if step1_visible:
                                        _log("  Step counter not found; step 1 form is visible, proceeding.")
                                        current_step = 1
                                except Exception:
                                    pass
                            if current_step != step_num:
                                msg = (
                                    f"Expected to be on step {step_num} of 8, "
                                    f"but current visible step is {current_step if current_step is not None else 'unknown'}."
                                )
                                _log(f"ERROR: {msg}")
                                _save_error_html(page, save_html_dir, step_num)
                                all_errors.append(msg)
                                break
                        desc = step_spec.get("description", f"Step {step_num}")
                        _log(f"--- Step {step_num} of 8: {desc} ---")
                        # Save this step's page HTML for analysis (1 of 8, 2 of 8, ...)
                        if save_html_dir is not None:
                            _save_page_html(page, save_html_dir, step_num)
                            _log(f"Saved HTML: step_{step_num:02d}_{step_num}_of_8.html")
                        errs = fill_step(page, profile, step_spec, step_timeout_ms, pause_after_fill_seconds)
                        if errs:
                            for e in errs:
                                _log(f"Step {step_num} error: {e}")
                            all_errors.extend([f"Step {step_num} ({desc}): {e}" for e in errs])
                        else:
                            steps_completed = step_num
                            _log(f"Step {step_num} done.")
                        # Verify the form actually advanced before moving on (catches address errors, etc.)
                        next_i = i + 1
                        if next_i < len(steps_list) and not errs:
                            next_spec = steps_list[next_i]
                            next_step_num = next_spec.get("step", next_i + 1)
                            advance_err = _verify_advanced_to_next_step(
                                page, next_spec, step_num, save_html_dir=save_html_dir,
                                current_step_spec=step_spec, profile=profile,
                            )
                            if advance_err:
                                _log(f"  ERROR: {advance_err}")
                                all_errors.append(advance_err)
                                break
                            # Mark next step as already-verified so the loop doesn't re-check the counter.
                            advance_already_verified_for_step = next_step_num
                        else:
                            try:
                                page.wait_for_timeout(500)
                                page.wait_for_load_state("domcontentloaded", timeout=5000)
                            except Exception:
                                pass

            except PlaywrightTimeout as e:
                _log(f"Timeout: {e}")
                all_errors.append(f"Timeout: {e}")
            except Exception as e:
                _log(f"Exception: {e}")
                all_errors.append(str(e))
            finally:
                try:
                    last_step_num = steps_list[-1].get("step", len(steps_list)) if steps_list else 0
                    if steps_completed == last_step_num:
                        _log("Page 8 completed; handling agreement page checkboxes.")
                        agreement_errors = _check_agreement_boxes(page)
                        if agreement_errors:
                            all_errors.extend([f"Agreement page: {e}" for e in agreement_errors])
                        else:
                            _log("Clicking Next on agreement page...")
                            if _click_agreement_next_button(page):
                                _log("Clicked Next.")
                                page.wait_for_timeout(2000)  # Let submit section appear
                                _log("Clicking 'Submit my application'...")
                                if _click_agreement_submit_button(page):
                                    _log("Clicked Submit my application.")
                                else:
                                    _log("Warning: Submit my application button not found or not clickable.")
                            else:
                                _log("Warning: Next button not found or not clickable.")
                        _save_agreement_page_html(page, save_html_dir)
                    else:
                        _log("Closing browser...")
                        browser.close()
                except Exception as e:
                    _log(f"Warning: cleanup skipped (browser/page may already be closed): {e}")
    finally:
        last_step_num = steps_list[-1].get("step", len(steps_list)) if steps_list else 0
        if steps_completed == last_step_num:
            _log("Leaving browser and AdsPower running for agreement page (close manually when done).")
        else:
            _log("Stopping AdsPower profile...")
            _adspower_stop(adspower_profile, adspower_api_base)
            _log("AdsPower stopped.")
        if hasattr(_log_callback, "callback"):
            delattr(_log_callback, "callback")

    log_entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "product": "capital_one_platinum",
        "url": apply_url,
        "profile_path": str(profile_path) if profile_path else "(in-memory)",
        "steps_completed": steps_completed,
        "ok": len(all_errors) == 0,
        "errors": all_errors,
        "save_html_dir": str(save_html_dir) if save_html_dir else None,
    }
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log_entry, indent=2), encoding="utf-8")
        _log(f"Wrote log: {log_path}")
    _log(f"Done. Steps completed: {steps_completed}. Errors: {len(all_errors)}")

    result = {
        "ok": len(all_errors) == 0,
        "error": "; ".join(all_errors) if all_errors else None,
        "steps_completed": steps_completed,
        "log_path": str(log_path) if log_path else None,
    }
    if log_path is None:
        result["log_entry"] = log_entry
    return result


def run_filler_from_data(
    profile: dict[str, Any],
    steps_config: dict[str, Any],
    log_path: Path | None = None,
    stop_after_step: int | None = None,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    save_html_dir: Path | None = None,
    pause_after_fill_seconds: float = 0,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Run the Capital One filler from in-memory profile and steps config. Session-isolated:
    no shared files; use a distinct adspower_profile per concurrent user. Optional
    log_path to write tri_merge_log.json; if None, result includes "log_entry" for caller to persist.
    """
    if "steps" not in steps_config:
        return {
            "ok": False,
            "error": "steps_config must contain 'steps' list",
            "steps_completed": 0,
            "log_path": None,
            "log_entry": None,
        }
    apply_url = steps_config.get("apply_url") or CAPITAL_ONE_APPLY_URL
    return _run_filler_core(
        profile,
        steps_config["steps"],
        apply_url,
        log_path,
        profile_path=None,
        stop_after_step=stop_after_step,
        step_timeout_ms=step_timeout_ms,
        nav_timeout_ms=nav_timeout_ms,
        save_html_dir=save_html_dir,
        pause_after_fill_seconds=pause_after_fill_seconds,
        adspower_profile=adspower_profile,
        adspower_api_base=adspower_api_base,
        log_callback=log_callback,
    )


async def run_filler_async(
    profile: dict[str, Any],
    steps_config: dict[str, Any],
    log_path: Path | None = None,
    stop_after_step: int | None = None,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    save_html_dir: Path | None = None,
    pause_after_fill_seconds: float = 0,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Async wrapper: runs run_filler_from_data in a thread so the event loop is not blocked.
    Safe for many concurrent users when each has a distinct adspower_profile.
    """
    return await asyncio.to_thread(
        run_filler_from_data,
        profile,
        steps_config,
        log_path,
        stop_after_step,
        step_timeout_ms,
        nav_timeout_ms,
        save_html_dir,
        pause_after_fill_seconds,
        adspower_profile,
        adspower_api_base,
        log_callback,
    )


def run_filler(
    profile_path: Path,
    steps_config_path: Path,
    log_path: Path,
    stop_after_step: int | None = None,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    save_html_dir: Path | None = None,
    pause_after_fill_seconds: float = 0,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
) -> dict[str, Any]:
    """
    Run the Capital One filler from file paths (CLI / single-run). Loads profile and steps
    from disk, then runs core logic. For concurrent use, prefer run_filler_from_data or
    run_filler_async with in-memory data and session-specific paths.
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
        _log(f"ERROR: Could not load steps config from {steps_config_path}")
        return {
            "ok": False,
            "error": f"Could not load steps config from {steps_config_path}",
            "steps_completed": 0,
            "log_path": str(log_path),
        }
    _log(f"Steps loaded ({len(steps_data['steps'])} steps).")
    apply_url = steps_data.get("apply_url") or CAPITAL_ONE_APPLY_URL
    return _run_filler_core(
        profile,
        steps_data["steps"],
        apply_url,
        log_path,
        profile_path=profile_path,
        stop_after_step=stop_after_step,
        step_timeout_ms=step_timeout_ms,
        nav_timeout_ms=nav_timeout_ms,
        save_html_dir=save_html_dir,
        pause_after_fill_seconds=pause_after_fill_seconds,
        adspower_profile=adspower_profile,
        adspower_api_base=adspower_api_base,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Module E: Fill Capital One credit card application from profile.json"
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH, help="profile.json path")
    parser.add_argument("--steps", type=Path, default=DEFAULT_STEPS_CONFIG, help="steps.json path")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH, help="tri_merge_log.json path")
    parser.add_argument("--adspower-profile", type=str, default=DEFAULT_ADSPOWER_PROFILE, help="AdsPower profile ID")
    parser.add_argument("--adspower-api", type=str, default=None, help="AdsPower Local API base URL (default: http://127.0.0.1:50325)")
    parser.add_argument("--stop-after", type=int, default=None, help="Stop after step N (e.g. 1 to only fill name)")
    parser.add_argument("--save-html-dir", type=Path, default=None, metavar="DIR", help="Save each 1/8..8/8 page HTML here (default: data/capital_one_pages)")
    parser.add_argument("--no-save-html", action="store_true", help="Do not save step HTML")
    parser.add_argument("--pause-after-fill", type=float, default=0, metavar="SECS", help="Pause this many seconds after filling each step so you can cross-check (default: 0). Use 0 to disable.")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT, help="Project root")
    args = parser.parse_args()

    root = _project_root(args.project_root)
    profile_path = args.profile if args.profile.is_absolute() else root / args.profile
    log_path = args.log if args.log.is_absolute() else root / args.log
    if args.no_save_html:
        save_html_dir = None
    elif args.save_html_dir is not None:
        save_html_dir = root / args.save_html_dir if not args.save_html_dir.is_absolute() else args.save_html_dir
    else:
        save_html_dir = root / "data" / "capital_one_pages"

    api_base = args.adspower_api or DEFAULT_ADSPOWER_API
    result = run_filler(
        profile_path=profile_path,
        steps_config_path=args.steps,
        log_path=log_path,
        stop_after_step=args.stop_after,
        save_html_dir=save_html_dir,
        pause_after_fill_seconds=args.pause_after_fill,
        adspower_profile=args.adspower_profile,
        adspower_api_base=api_base,
    )

    if not result["ok"] and result.get("error"):
        print(result["error"], file=sys.stderr)
    print(f"Steps completed: {result['steps_completed']}; log: {result['log_path']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
