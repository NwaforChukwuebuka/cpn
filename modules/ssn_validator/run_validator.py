"""
Module B — SSN Validator / Deceased Check (Step 3).

Reads partial CPN data (dict or file), completes last 4 digits, and checks Death Masterfile
and issuance status at https://www.ssn-verify.com/ using an AdsPower browser profile.

Designed for concurrent execution: no global state, session-isolated API.
Each caller must pass its own partial_cpn data and adspower_profile (e.g. from a profile pool).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[misc, assignment]

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None  # type: ignore[misc, assignment]

try:
    import requests
except ImportError:
    requests = None  # type: ignore[misc, assignment]

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[misc, assignment]

from modules.adspower_profiles import DEFAULT_ADSPOWER_PROFILE

DEFAULT_ADSPOWER_API = "http://127.0.0.1:50325"
SSN_VERIFY_URL = "https://www.ssn-verify.com/"
DEFAULT_INPUT_PATH = Path("data/partial_cpn.json")
DEFAULT_OUTPUT_PATH = Path("data/full_cpn.json")
DEFAULT_SAVE_HTML_PATH = Path("data/ssn_result.html")


def _error_result(
    error: str,
    full_ssn: str | None = None,
    last_four: str | None = None,
) -> dict[str, Any]:
    """Build a consistent error result dict (no shared state)."""
    return {
        "ok": False,
        "error": error,
        "full": full_ssn,
        "last_four": last_four,
        "deceased_check": "error",
        "issuance_status": "error",
        "results": {},
        "raw_result": None,
    }


def normalize_partial(partial_cpn: dict[str, Any]) -> dict[str, str] | None:
    """
    Normalize partial CPN from a dict. Returns dict with 'area' and 'group' (3- and 2-digit)
    or None if invalid. Stateless; safe to call from any thread/async context.
    """
    area = partial_cpn.get("area")
    group = partial_cpn.get("group")
    if area is not None and group is not None:
        return {
            "area": f"{int(area):03d}" if isinstance(area, int) else str(area).zfill(3),
            "group": f"{int(group):02d}" if isinstance(group, int) else str(group).zfill(2),
        }
    partial = partial_cpn.get("partial") or ""
    prefix_5 = partial_cpn.get("prefix_5") or ""
    s = (prefix_5.strip() or partial.strip()).replace(" ", "").strip()
    if not s:
        return None
    if "-" in s:
        parts = s.split("-")
        if len(parts) >= 2:
            a, g = parts[0].strip(), parts[1].strip()
            if a.isdigit() and g.isdigit():
                return {"area": a.zfill(3), "group": g.zfill(2)}
    return None


def _load_partial(path: Path) -> dict[str, str] | None:
    """Load and normalize partial_cpn.json from file. Returns dict with area, group or None."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return normalize_partial(data)


def _adspower_start(profile_id: str, api_base: str) -> tuple[str | None, str | None]:
    """Start AdsPower profile; return (Puppeteer WebSocket URL, error_message)."""
    if requests is None:
        return None, "requests not installed"
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
        puppeteer_url = ws.get("puppeteer")
        if not puppeteer_url:
            return None, f"AdsPower response had no ws.puppeteer: {json.dumps(out)[:300]}"
        return puppeteer_url, None
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
    except Exception:
        pass


async def _adspower_start_async(profile_id: str, api_base: str) -> tuple[str | None, str | None]:
    """Start AdsPower profile (async). Return (Puppeteer WebSocket URL, error_message)."""
    if httpx is None:
        return None, "httpx not installed (pip install httpx for async support)"
    url = f"{api_base.rstrip('/')}/api/v2/browser-profile/start"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json={"profile_id": profile_id})
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
    except httpx.ConnectError as e:
        return None, f"Cannot reach AdsPower at {api_base}. Is it running with Local API enabled? {e}"
    except Exception as e:
        return None, str(e)


async def _adspower_stop_async(profile_id: str, api_base: str) -> None:
    """Stop AdsPower profile (async)."""
    if httpx is None:
        return
    url = f"{api_base.rstrip('/')}/api/v2/browser-profile/stop"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={"profile_id": profile_id})
    except Exception:
        pass


def _parse_page_result(page, body_text: str) -> tuple[str, str, dict[str, str]]:
    """Parse SSN-Verify result page; returns (issuance_status, deceased_check, results_table). Stateless."""
    body_lower = body_text.lower()
    results_table: dict[str, str] = {}
    table = page.locator("table.table-bordered")
    if table.count() > 0:
        rows = table.locator("tbody tr")
        label_to_key = [
            ("valid?", "valid_status"),
            ("estimated age", "estimated_age"),
            ("first year issued", "first_year_issued"),
            ("last year issued", "last_year_issued"),
            ("issuance location", "issuance_location"),
            ("ssn", "ssn"),
        ]
        for i in range(rows.count()):
            tr = rows.nth(i)
            th_text = tr.locator("th").inner_text().strip().lower()
            td_text = " ".join(tr.locator("td").inner_text().strip().split()) or "—"
            for label, key in label_to_key:
                if th_text.startswith(label):
                    results_table[key] = td_text
                    break
    has_valid_ssn = page.locator("span.text-money").count() > 0 and "is a valid ssn" in body_lower
    has_invalid_ssn = (
        page.locator("span.text-negative-money").count() > 0
        or "may have been issued after june 25, 2011" in body_lower
    )
    if has_invalid_ssn and not has_valid_ssn:
        issuance_status = "not_issued"
    elif has_valid_ssn:
        issuance_status = "issued"
    else:
        issuance_status = "error"
    if "no record" in body_lower:
        deceased_check = "no_record"
    elif "death" in body_lower and ("record" in body_lower or "found" in body_lower):
        deceased_check = "found"
    else:
        deceased_check = "no_record"
    return issuance_status, deceased_check, results_table


async def _parse_page_result_async(page) -> tuple[str, str, dict[str, str]]:
    """Async version: parse SSN-Verify result page. Stateless."""
    body_text = await page.locator("body").inner_text()
    body_lower = body_text.lower()
    results_table: dict[str, str] = {}
    table = page.locator("table.table-bordered")
    if await table.count() > 0:
        rows = table.locator("tbody tr")
        label_to_key = [
            ("valid?", "valid_status"),
            ("estimated age", "estimated_age"),
            ("first year issued", "first_year_issued"),
            ("last year issued", "last_year_issued"),
            ("issuance location", "issuance_location"),
            ("ssn", "ssn"),
        ]
        n = await rows.count()
        for i in range(n):
            tr = rows.nth(i)
            th_text = (await tr.locator("th").inner_text()).strip().lower()
            td_text = " ".join((await tr.locator("td").inner_text()).strip().split()) or "—"
            for label, key in label_to_key:
                if th_text.startswith(label):
                    results_table[key] = td_text
                    break
    has_valid_ssn = (await page.locator("span.text-money").count() > 0) and "is a valid ssn" in body_lower
    has_invalid_ssn = (
        await page.locator("span.text-negative-money").count() > 0
        or "may have been issued after june 25, 2011" in body_lower
    )
    if has_invalid_ssn and not has_valid_ssn:
        issuance_status = "not_issued"
    elif has_valid_ssn:
        issuance_status = "issued"
    else:
        issuance_status = "error"
    if "no record" in body_lower:
        deceased_check = "no_record"
    elif "death" in body_lower and ("record" in body_lower or "found" in body_lower):
        deceased_check = "found"
    else:
        deceased_check = "no_record"
    return issuance_status, deceased_check, results_table


def _run_flow_from_data(
    partial: dict[str, str],
    last_four: str | None,
    adspower_profile: str,
    adspower_api_base: str,
    save_html_path: Path | None = None,
) -> dict[str, Any]:
    """
    Run validation using in-memory partial data. No file I/O for input.
    Each call is independent; use a distinct adspower_profile per concurrent session.
    """
    area = partial["area"]
    group = partial["group"]
    last4 = (
        last_four
        if last_four and len(last_four) == 4 and last_four.isdigit()
        else f"{random.randint(0, 9999):04d}"
    )
    full_ssn = f"{area}-{group}-{last4}"

    ws_url, start_err = _adspower_start(adspower_profile, adspower_api_base)
    if not ws_url:
        return _error_result(
            start_err or f"Failed to start AdsPower profile {adspower_profile}.",
            full_ssn=full_ssn,
            last_four=last4,
        )

    if sync_playwright is None:
        _adspower_stop(adspower_profile, adspower_api_base)
        return _error_result(
            "Playwright not installed. pip install playwright && playwright install chromium",
            full_ssn=full_ssn,
            last_four=last4,
        )

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url)
            try:
                page = None
                for ctx in browser.contexts:
                    if ctx.pages:
                        page = ctx.pages[0]
                        break
                if page is None:
                    page = browser.new_page()
                page.goto(SSN_VERIFY_URL, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1500)
                page.locator("#area").fill(area)
                page.locator("#group").fill(group)
                page.locator("#series").fill(last4)
                page.locator("#ssn-submit").click()
                page.wait_for_timeout(3000)

                if save_html_path:
                    save_html_path.parent.mkdir(parents=True, exist_ok=True)
                    save_html_path.write_text(page.content(), encoding="utf-8")

                body_text = page.locator("body").inner_text()
                issuance_status, deceased_check, results_table = _parse_page_result(page, body_text)
                ok = issuance_status == "not_issued"
                return {
                    "ok": ok,
                    "error": None,
                    "full": full_ssn,
                    "last_four": last4,
                    "deceased_check": deceased_check,
                    "issuance_status": issuance_status,
                    "results": results_table,
                    "raw_result": body_text[:2000] if body_text else None,
                }
            finally:
                browser.close()
    except Exception as e:
        return _error_result(str(e), full_ssn=full_ssn, last_four=last4)
    finally:
        _adspower_stop(adspower_profile, adspower_api_base)


def _run_flow(
    input_path: Path,
    output_path: Path,
    last_four: str | None,
    adspower_profile: str,
    adspower_api_base: str,
    save_html_path: Path | None = None,
) -> dict[str, Any]:
    """Run validation from file input (CLI/single-run). Delegates to _run_flow_from_data."""
    partial = _load_partial(input_path)
    if not partial:
        return _error_result(f"Could not load or parse partial CPN from {input_path}")
    return _run_flow_from_data(
        partial,
        last_four=last_four,
        adspower_profile=adspower_profile,
        adspower_api_base=adspower_api_base,
        save_html_path=save_html_path,
    )


async def _run_flow_async(
    partial: dict[str, str],
    last_four: str | None,
    adspower_profile: str,
    adspower_api_base: str,
    save_html_path: Path | None = None,
) -> dict[str, Any]:
    """
    Async validation flow: no blocking, no shared state. Use a distinct
    adspower_profile per concurrent session (e.g. from a profile pool).
    """
    area = partial["area"]
    group = partial["group"]
    last4 = (
        last_four
        if last_four and len(last_four) == 4 and last_four.isdigit()
        else f"{random.randint(0, 9999):04d}"
    )
    full_ssn = f"{area}-{group}-{last4}"

    ws_url, start_err = await _adspower_start_async(adspower_profile, adspower_api_base)
    if not ws_url:
        return _error_result(
            start_err or f"Failed to start AdsPower profile {adspower_profile}.",
            full_ssn=full_ssn,
            last_four=last4,
        )

    if async_playwright is None:
        await _adspower_stop_async(adspower_profile, adspower_api_base)
        return _error_result(
            "Playwright not installed. pip install playwright && playwright install chromium",
            full_ssn=full_ssn,
            last_four=last4,
        )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws_url)
            try:
                page = None
                for ctx in browser.contexts:
                    if ctx.pages:
                        page = ctx.pages[0]
                        break
                if page is None:
                    page = await browser.new_page()
                await page.goto(SSN_VERIFY_URL, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1500)
                await page.locator("#area").fill(area)
                await page.locator("#group").fill(group)
                await page.locator("#series").fill(last4)
                await page.locator("#ssn-submit").click()
                await page.wait_for_timeout(3000)

                if save_html_path:
                    save_html_path.parent.mkdir(parents=True, exist_ok=True)
                    save_html_path.write_text(await page.content(), encoding="utf-8")

                issuance_status, deceased_check, results_table = await _parse_page_result_async(page)
                body_text = await page.locator("body").inner_text()
                ok = issuance_status == "not_issued"
                return {
                    "ok": ok,
                    "error": None,
                    "full": full_ssn,
                    "last_four": last4,
                    "deceased_check": deceased_check,
                    "issuance_status": issuance_status,
                    "results": results_table,
                    "raw_result": body_text[:2000] if body_text else None,
                }
            finally:
                await browser.close()
    except Exception as e:
        return _error_result(str(e), full_ssn=full_ssn, last_four=last4)
    finally:
        await _adspower_stop_async(adspower_profile, adspower_api_base)


def run_validation(
    partial_cpn: dict[str, Any],
    last_four: str | None = None,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    save_html_path: Path | None = None,
) -> dict[str, Any]:
    """
    Run SSN validation (sync). Session-isolated: pass per-user partial_cpn and
    adspower_profile. No global state; safe for concurrent calls from multiple
    threads as long as each uses a different adspower_profile.
    """
    partial = normalize_partial(partial_cpn)
    if not partial:
        return _error_result("Invalid or missing partial CPN (need area/group or partial/prefix_5).")
    return _run_flow_from_data(
        partial,
        last_four=last_four,
        adspower_profile=adspower_profile,
        adspower_api_base=adspower_api_base,
        save_html_path=save_html_path,
    )


async def run_validation_async(
    partial_cpn: dict[str, Any],
    last_four: str | None = None,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    save_html_path: Path | None = None,
) -> dict[str, Any]:
    """
    Run SSN validation asynchronously. Session-isolated and non-blocking; safe
    for many concurrent users when each has a distinct adspower_profile.
    Uses async Playwright + httpx when available; otherwise runs sync flow in a thread.
    """
    partial = normalize_partial(partial_cpn)
    if not partial:
        return _error_result("Invalid or missing partial CPN (need area/group or partial/prefix_5).")
    if async_playwright and httpx:
        return await _run_flow_async(
            partial,
            last_four=last_four,
            adspower_profile=adspower_profile,
            adspower_api_base=adspower_api_base,
            save_html_path=save_html_path,
        )
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _run_flow_from_data(
            partial,
            last_four=last_four,
            adspower_profile=adspower_profile,
            adspower_api_base=adspower_api_base,
            save_html_path=save_html_path,
        ),
    )


def run(
    input_path: Path | None = None,
    output_path: Path | None = None,
    last_four: str | None = None,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    save_html_path: Path | None = None,
) -> bool:
    """
    CLI-style entry: read from input_path, write result to output_path.
    For concurrent/multi-user use, prefer run_validation() or run_validation_async()
    with in-memory partial_cpn and session-specific paths/profile.
    """
    input_path = input_path or DEFAULT_INPUT_PATH
    output_path = output_path or DEFAULT_OUTPUT_PATH
    result = _run_flow(
        input_path=input_path,
        output_path=output_path,
        last_four=last_four,
        adspower_profile=adspower_profile,
        adspower_api_base=adspower_api_base,
        save_html_path=save_html_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result.get("ok", False) is True


def main() -> int:
    parser = argparse.ArgumentParser(description="Module B: SSN Validator / Deceased check via SSN-Verify (AdsPower).")
    parser.add_argument("--input", "-i", type=Path, default=DEFAULT_INPUT_PATH, help="partial_cpn.json path")
    parser.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT_PATH, help="full_cpn.json path")
    parser.add_argument("--last-four", type=str, default=None, help="Last 4 digits (default: random)")
    parser.add_argument("--adspower-profile", type=str, default=DEFAULT_ADSPOWER_PROFILE, help="AdsPower profile ID")
    parser.add_argument("--adspower-api", type=str, default=None, help="AdsPower Local API base URL")
    parser.add_argument("--json", action="store_true", help="Print full JSON instead of summary")
    parser.add_argument("--save-html", type=Path, default=None, metavar="PATH", help="Save result page HTML to PATH for analysis")
    args = parser.parse_args()
    api_base = args.adspower_api or DEFAULT_ADSPOWER_API
    save_html = args.save_html
    ok = run(
        input_path=args.input,
        output_path=args.output,
        last_four=args.last_four,
        adspower_profile=args.adspower_profile,
        adspower_api_base=api_base,
        save_html_path=save_html,
    )
    result = json.loads(args.output.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        res = result.get("results") or {}
        lines = ["Results:"]
        for key in ("ssn", "issuance_location", "first_year_issued", "last_year_issued", "estimated_age", "valid_status"):
            if key in res:
                lines.append(f"  {key}: {res[key]}")
        if result.get("error"):
            lines.append(f"  error: {result['error']}")
        if save_html:
            lines.append(f"  result_html: {save_html}")
        print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
