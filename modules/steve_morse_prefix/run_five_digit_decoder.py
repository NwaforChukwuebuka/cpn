"""
Playwright automation: get first five digits (AAA-GG) via Steve Morse SSN decoder.

Implements the flow in note.md and INVESTIGATION_STEPHEN_MORSE_FLOW.md:
  1. Resolve state to latest issuance period from state_area_ranges.json.
  2. On https://stevemorse.org/ssn/ssn.html: select that state in Three-Digit Decoder,
     verify 3-digit range matches config (prefer website if mismatch).
  3. Pick random 3-digit area in verified range.
  4. On Five-Digit Decoder: set area, then try random groups until #wherewhen shows
     "Not Issued"; return that area+group as the 5-digit prefix.

Rate limiting: each Five-Digit lookup hits the server; use --delay between tries
(e.g. 20+ seconds) to avoid bans.

Concurrency: Use async_run_five_digit_decoder() from async code (e.g. Telegram bot).
Each call runs in a thread with its own browser, so many users can run in parallel
without blocking the event loop or each other.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

from .steve_morse import DEFAULT_DATA_PATH, get_latest_state_range

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[misc, assignment]

try:
    import requests
except ImportError:
    requests = None  # type: ignore[misc, assignment]

try:
    from modules.adspower_refresh import rotate_ip_sync
except ImportError:
    rotate_ip_sync = None  # type: ignore[misc, assignment]

URL = "https://stevemorse.org/ssn/ssn.html"
NOT_ISSUED_MARKER = "Not Issued"
GROUPS_PER_AREA = 10  # Try this many random groups per area before switching to next area
MAX_AREA_TRIES = 20  # Try up to this many different areas before giving up
DEFAULT_DELAY_SECONDS = 20

# When the server is unhappy (e.g. rate limit), the page can show these; we refresh IP and retry.
RATE_LIMIT_PHRASES = ("unexplained error", "please notify me")
MAX_REFRESH_RETRIES = 2
REFRESH_WAIT_AFTER_SECONDS = 15
# When returning due to rate limit: save screenshot and keep browser open this many seconds for troubleshooting
RATE_LIMIT_TROUBLESHOOT_SECONDS = 300
# MobileHop proxy reset: open in new tab when rate limited; page may show error but IP is changed. Wait then reload first tab.
DEFAULT_IP_RESET_URL = "https://portal.mobilehop.com/proxies/6438d27add2d4134b4fd835110e39664/reset"
IP_RESET_WAIT_SECONDS = 12  # Page says "Please wait 4-10 seconds"
DEFAULT_ADSPOWER_API = "http://127.0.0.1:50325"
DEFAULT_REFRESH_HOST = "127.0.0.1:20725"
LOG_PREFIX = "[Steve Morse]"


def _log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


def _is_rate_limited(text: str) -> bool:
    """True if the result text indicates server rate limit / error (e.g. 'unexplained error', 'Please notify me')."""
    if not text:
        return False
    lower = text.lower()
    return any(phrase in lower for phrase in RATE_LIMIT_PHRASES)


def _refresh_ip_via_reset_url(page, ip_reset_url: str, wait_seconds: int = IP_RESET_WAIT_SECONDS) -> bool:
    """
    Open ip_reset_url in a new tab (e.g. MobileHop proxy reset). Page may show an error but IP is changed.
    Wait, close the tab, then reload the first tab so subsequent requests use the new IP.
    Returns True if the flow completed without exception.
    """
    try:
        context = page.context
        new_tab = context.new_page()
        try:
            _log(f"Opening IP reset URL in new tab (wait {wait_seconds}s)...")
            new_tab.goto(ip_reset_url, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(wait_seconds)
        finally:
            new_tab.close()
        _log("Reloading Steve Morse tab to use new IP...")
        page.reload(wait_until="domcontentloaded", timeout=30_000)
        return True
    except Exception as e:
        _log(f"IP reset via URL failed: {e}")
        return False


def _adspower_start(profile_id: str, api_base: str) -> tuple[str | None, str | None]:
    """Start AdsPower profile; return (Puppeteer WebSocket URL, error_message)."""
    if requests is None:
        return None, "requests not installed"
    url = f"{api_base.rstrip('/')}/api/v2/browser-profile/start"
    try:
        r = requests.post(url, json={"profile_id": profile_id}, timeout=30)
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
        puppeteer_url = ws.get("puppeteer")
        if not puppeteer_url:
            return None, f"AdsPower response had no ws.puppeteer: {json.dumps(out)[:300]}"
        return puppeteer_url, None
    except requests.exceptions.ConnectionError as e:
        return None, f"Cannot reach AdsPower at {api_base}. Is it running with Local API enabled? {e}"
    except Exception as e:
        return None, str(e)


def _adspower_stop(profile_id: str, api_base: str) -> None:
    """Stop AdsPower profile. Never raises."""
    if requests is None:
        return
    url = f"{api_base.rstrip('/')}/api/v2/browser-profile/stop"
    try:
        requests.post(url, json={"profile_id": profile_id}, timeout=10)
    except Exception:
        pass


def _parse_ssn_range(text: str) -> tuple[int, int] | None:
    """Parse '766 to 772' -> (766, 772). Returns None if format doesn't match."""
    if not text or " to " not in text:
        return None
    parts = text.strip().split(" to ", 1)
    if len(parts) != 2:
        return None
    try:
        low = int(parts[0].strip())
        high = int(parts[1].strip())
        if 1 <= low <= high <= 999:
            return (low, high)
    except ValueError:
        pass
    return None


def _run_flow(
    state: str,
    data_path: Path | None,
    delay_seconds: float,
    headless: bool,
    adspower_profile: str | None = None,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    refresh_host: str = DEFAULT_REFRESH_HOST,
    ip_reset_url: str | None = DEFAULT_IP_RESET_URL,
) -> dict:
    """
    Run the full flow in the browser. Returns a result dict suitable for JSON output.

    When adspower_profile is set, uses that AdsPower browser (so traffic uses that IP).
    If the page shows rate-limit text ("unexplained error", "Please notify me"), triggers
    IP refresh: when ip_reset_url is set, opens it in a new tab (e.g. MobileHop proxy reset),
    waits, closes tab, reloads first tab; otherwise uses AdsPower refresh page and retries.
    """
    config = get_latest_state_range(state, data_path)
    if not config:
        return {
            "ok": False,
            "error": f"State not found or no data: {state}. Check state_area_ranges.json.",
            "prefix_5": None,
            "area": None,
            "group": None,
            "state": state,
            "date_range_used": None,
            "verified_range": None,
        }

    config_label = config["label"]
    config_low = config["low"]
    config_high = config["high"]

    ws_url: str | None = None
    if adspower_profile:
        ws_url, start_err = _adspower_start(adspower_profile, adspower_api_base)
        if not ws_url:
            return {
                "ok": False,
                "error": start_err or f"Failed to start AdsPower profile {adspower_profile}",
                "prefix_5": None,
                "area": None,
                "group": None,
                "state": state,
                "date_range_used": config_label,
                "verified_range": [config_low, config_high],
            }

    try:
        with sync_playwright() as p:
            if ws_url:
                browser = p.chromium.connect_over_cdp(ws_url)
            else:
                browser = p.chromium.launch(headless=headless)
            try:
                page = browser.new_page() if not ws_url else None
                if page is None and ws_url:
                    for ctx in browser.contexts:
                        if ctx.pages:
                            page = ctx.pages[0]
                            break
                    if page is None:
                        page = browser.new_page()

                for refresh_attempt in range(MAX_REFRESH_RETRIES + 1):
                    # Step 1: Navigate (or reload after IP refresh)
                    page.goto(URL, wait_until="domcontentloaded", timeout=30_000)

                    # Step 2b: Select latest state in Three-Digit Decoder and read 3-digit range
                    state_select = page.locator('select[name="state"]')
                    state_select.select_option(label=config_label)

                    ssn_option = page.locator('select[name="ssn"]').evaluate(
                        """(sel) => {
                            const opt = sel.options[sel.selectedIndex];
                            return opt ? opt.textContent.trim() : '';
                        }"""
                    )
                    web_range = _parse_ssn_range(ssn_option) if ssn_option else None

                    if web_range is None:
                        return {
                            "ok": False,
                            "error": f"Could not read 3-digit range from page (got: {ssn_option!r})",
                            "prefix_5": None,
                            "area": None,
                            "group": None,
                            "state": state,
                            "date_range_used": config_label,
                            "verified_range": [config_low, config_high],
                        }

                    web_low, web_high = web_range
                    if (web_low, web_high) != (config_low, config_high):
                        low, high = web_low, web_high
                    else:
                        low, high = config_low, config_high

                    # Step 3 & 4: Try multiple areas; for each area, try GROUPS_PER_AREA random groups
                    # Vary both area and group: if one area has all groups issued, try another area
                    all_groups = [f"{i:02d}" for i in range(1, 100)]
                    found_group = None
                    last_area_str = None
                    last_result_text = None
                    hit_rate_limit = False
                    tried_areas: set[str] = set()

                    for area_attempt in range(MAX_AREA_TRIES):
                        # Pick a new random area (avoid repeating if possible)
                        area = random.randint(low, high)
                        area_str = f"{area:03d}"
                        if len(tried_areas) < high - low + 1 and area_str in tried_areas:
                            for a in range(low, high + 1):
                                candidate = f"{a:03d}"
                                if candidate not in tried_areas:
                                    area_str = candidate
                                    break
                        tried_areas.add(area_str)
                        last_area_str = area_str

                        page.locator('select[name="ssn1"]').select_option(value=area_str)
                        groups = random.sample(all_groups, min(GROUPS_PER_AREA, len(all_groups)))

                        for i, group in enumerate(groups):
                            page.locator('select[name="ssn2"]').select_option(value=group)
                            page.wait_for_timeout(800)
                            result_el = page.locator("#wherewhen")
                            result_el.wait_for(state="visible", timeout=5_000)
                            text = result_el.text_content() or ""
                            text = text.strip()
                            last_result_text = text

                            if NOT_ISSUED_MARKER in text:
                                found_group = group
                                break

                            if _is_rate_limited(text):
                                hit_rate_limit = True
                                last_result_text = text
                                break

                            if i < len(groups) - 1 and delay_seconds > 0:
                                page.wait_for_timeout(int(delay_seconds * 1000))

                        if found_group is not None:
                            prefix_5 = f"{area_str}-{found_group}"
                            return {
                                "ok": True,
                                "error": None,
                                "prefix_5": prefix_5,
                                "area": area_str,
                                "group": found_group,
                                "state": state,
                                "date_range_used": config_label,
                                "verified_range": [low, high],
                            }

                        if hit_rate_limit:
                            break
                        # No "Not Issued" for this area; try next area

                    if hit_rate_limit and refresh_attempt < MAX_REFRESH_RETRIES:
                        ok = False
                        if ip_reset_url:
                            _log("Rate limit hit. Refreshing IP via reset URL in new tab...")
                            ok = _refresh_ip_via_reset_url(page, ip_reset_url, IP_RESET_WAIT_SECONDS)
                            if ok:
                                _log("IP reset URL completed. Retrying...")
                                time.sleep(2)
                                continue
                        if not ok and adspower_profile and rotate_ip_sync:
                            _log(f"Rate limit hit. Attempting IP refresh for profile {adspower_profile} (host={refresh_host})...")
                            ok = rotate_ip_sync(adspower_profile, headless=True, host=refresh_host)
                            if ok:
                                _log("IP refresh succeeded. Waiting then retrying...")
                                time.sleep(REFRESH_WAIT_AFTER_SECONDS)
                                continue
                            else:
                                _log("IP refresh failed (refresh button not found or error). Check AdsPower refresh page at https://start.adspower.net/")
                        elif not ok and not ip_reset_url:
                            _log("Rate limit hit. IP refresh skipped: no ip_reset_url and adspower_refresh not available.")
                    if hit_rate_limit:
                        # Screenshot and keep browser open for visual troubleshooting
                        try:
                            screenshot_dir = Path.cwd() / "data"
                            screenshot_dir.mkdir(parents=True, exist_ok=True)
                            timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
                            screenshot_path = screenshot_dir / f"steve_morse_rate_limit_{timestamp}.png"
                            page.screenshot(path=str(screenshot_path))
                            _log(f"Screenshot saved: {screenshot_path}")
                        except Exception as e:
                            _log(f"Could not save screenshot: {e}")
                        _log(f"Keeping browser open for {RATE_LIMIT_TROUBLESHOOT_SECONDS}s for troubleshooting...")
                        time.sleep(RATE_LIMIT_TROUBLESHOOT_SECONDS)
                        err = f"Server returned rate-limit style message: {last_result_text!r}. Use --adspower-profile and refresh to rotate IP and retry."
                        return {
                            "ok": False,
                            "error": err,
                            "prefix_5": None,
                            "area": last_area_str,
                            "group": None,
                            "state": state,
                            "date_range_used": config_label,
                            "verified_range": [low, high],
                        }

                    return {
                        "ok": False,
                        "error": f"Did not find a 'Not Issued' prefix after trying {len(tried_areas)} area(s) × {GROUPS_PER_AREA} groups each. Last result: {last_result_text!r}. Consider rate limit / delays.",
                        "prefix_5": None,
                        "area": last_area_str,
                        "group": None,
                        "state": state,
                        "date_range_used": config_label,
                        "verified_range": [low, high],
                    }
            finally:
                browser.close()
    finally:
        if adspower_profile:
            _adspower_stop(adspower_profile, adspower_api_base)


async def async_run_five_digit_decoder(
    state: str,
    *,
    data_path: Path | None = None,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    headless: bool = True,
    adspower_profile: str | None = None,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    refresh_host: str = DEFAULT_REFRESH_HOST,
    ip_reset_url: str | None = DEFAULT_IP_RESET_URL,
) -> dict:
    """
    Async entrypoint for the five-digit decoder flow. Runs the sync Playwright
    flow in a thread pool so the event loop is not blocked. Safe for many
    parallel user sessions (each gets its own thread and browser instance).

    When adspower_profile is set, uses that AdsPower browser; on rate-limit
    ("unexplained error" / "Please notify me") opens ip_reset_url in new tab to
    rotate IP (e.g. MobileHop), then retries.
    """
    if sync_playwright is None:
        return {
            "ok": False,
            "error": "Playwright not installed. Run: pip install playwright && playwright install chromium",
            "prefix_5": None,
            "area": None,
            "group": None,
            "state": state,
            "date_range_used": None,
            "verified_range": None,
        }
    path = data_path or DEFAULT_DATA_PATH
    return await asyncio.to_thread(
        _run_flow,
        state,
        path,
        delay_seconds,
        headless,
        adspower_profile,
        adspower_api_base,
        refresh_host,
        ip_reset_url,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Get first 5 digits (AAA-GG) via Steve Morse SSN decoder (Playwright)."
    )
    parser.add_argument(
        "state",
        nargs="?",
        default="Florida",
        help="State name (e.g. Florida, California)",
    )
    parser.add_argument(
        "--data",
        "-d",
        type=Path,
        default=None,
        help="Path to state_area_ranges.json",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Seconds between Five-Digit group tries (default {DEFAULT_DELAY_SECONDS})",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless",
    )
    parser.add_argument(
        "--adspower-profile",
        type=str,
        default=None,
        help="AdsPower profile ID; use when rate-limited (triggers IP refresh and retry)",
    )
    parser.add_argument(
        "--adspower-api",
        type=str,
        default=DEFAULT_ADSPOWER_API,
        help="AdsPower Local API base URL",
    )
    parser.add_argument(
        "--refresh-host",
        type=str,
        default=DEFAULT_REFRESH_HOST,
        help="Host for AdsPower IP refresh page URL",
    )
    parser.add_argument(
        "--ip-reset-url",
        type=str,
        default=None,
        metavar="URL",
        help="When rate limited, open this URL in a new tab to rotate IP (e.g. MobileHop proxy reset). Default: " + DEFAULT_IP_RESET_URL[:50] + "...",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Write result JSON to file (default: print only)",
    )
    args = parser.parse_args()

    if sync_playwright is None:
        print(
            json.dumps({
                "ok": False,
                "error": "Playwright not installed. Run: pip install playwright && playwright install chromium",
            }),
            indent=2,
        )
        return 1

    data_path = args.data or DEFAULT_DATA_PATH
    ip_reset_url = (args.ip_reset_url or DEFAULT_IP_RESET_URL) if args.ip_reset_url != "" else None
    result = _run_flow(
        state=args.state,
        data_path=data_path,
        delay_seconds=args.delay,
        headless=args.headless,
        adspower_profile=args.adspower_profile,
        adspower_api_base=args.adspower_api,
        refresh_host=args.refresh_host,
        ip_reset_url=ip_reset_url,
    )

    out = json.dumps(result, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out, encoding="utf-8")
    print(out)

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
