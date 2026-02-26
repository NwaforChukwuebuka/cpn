"""
Module B — SSN Validator / Deceased Check (Step 3).

Reads partial_cpn.json, completes last 4 digits, and checks Death Masterfile
and issuance status at https://www.ssn-verify.com/ using an AdsPower browser profile.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[misc, assignment]

try:
    import requests
except ImportError:
    requests = None  # type: ignore[misc, assignment]

DEFAULT_ADSPOWER_PROFILE = "k19jxstf"
DEFAULT_ADSPOWER_API = "http://127.0.0.1:50325"
SSN_VERIFY_URL = "https://www.ssn-verify.com/"
DEFAULT_INPUT_PATH = Path("data/partial_cpn.json")
DEFAULT_OUTPUT_PATH = Path("data/full_cpn.json")
DEFAULT_SAVE_HTML_PATH = Path("data/ssn_result.html")


def _load_partial(path: Path) -> dict | None:
    """Load and normalize partial_cpn.json. Returns dict with area, group (3- and 2-digit strings) or None."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    area = data.get("area")
    group = data.get("group")
    if area is not None and group is not None:
        return {
            "area": f"{int(area):03d}" if isinstance(area, int) else str(area).zfill(3),
            "group": f"{int(group):02d}" if isinstance(group, int) else str(group).zfill(2),
        }
    partial = data.get("partial") or ""
    prefix_5 = data.get("prefix_5") or ""
    s = prefix_5.strip() or partial.strip()
    if not s:
        return None
    s = s.replace(" ", "").strip()
    if "-" in s:
        parts = s.split("-")
        if len(parts) >= 2:
            a, g = parts[0].strip(), parts[1].strip()
            if a.isdigit() and g.isdigit():
                return {"area": a.zfill(3), "group": g.zfill(2)}
    return None


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


def _run_flow(
    input_path: Path,
    output_path: Path,
    last_four: str | None,
    adspower_profile: str,
    adspower_api_base: str,
    save_html_path: Path | None = None,
) -> dict:
    partial = _load_partial(input_path)
    if not partial:
        return {
            "ok": False,
            "error": f"Could not load or parse partial CPN from {input_path}",
            "full": None,
            "last_four": None,
            "deceased_check": "error",
            "issuance_status": "error",
            "results": {},
            "raw_result": None,
        }

    area = partial["area"]
    group = partial["group"]
    last4 = last_four if last_four and len(last_four) == 4 and last_four.isdigit() else f"{random.randint(0, 9999):04d}"
    full_ssn = f"{area}-{group}-{last4}"

    ws_url, start_err = _adspower_start(adspower_profile, adspower_api_base)
    if not ws_url:
        return {
            "ok": False,
            "error": start_err or f"Failed to start AdsPower profile {adspower_profile}.",
            "full": full_ssn,
            "last_four": last4,
            "deceased_check": "error",
            "issuance_status": "error",
            "results": {},
            "raw_result": None,
        }

    if sync_playwright is None:
        _adspower_stop(adspower_profile, adspower_api_base)
        return {
            "ok": False,
            "error": "Playwright not installed. pip install playwright && playwright install chromium",
            "full": full_ssn,
            "last_four": last4,
            "deceased_check": "error",
            "issuance_status": "error",
            "results": {},
            "raw_result": None,
        }

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url)
            try:
                # Use the first already-open page (AdsPower's window); do not create a second tab/window
                page = None
                for ctx in browser.contexts:
                    if ctx.pages:
                        page = ctx.pages[0]
                        break
                if page is None:
                    page = browser.new_page()
                page.goto(SSN_VERIFY_URL, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1500)

                # SSN form: three fields by id (area, group, series) and submit by id (ssn-verify.html)
                page.locator("#area").fill(area)
                page.locator("#group").fill(group)
                page.locator("#series").fill(last4)
                page.locator("#ssn-submit").click()
                page.wait_for_timeout(3000)

                if save_html_path:
                    save_html_path.parent.mkdir(parents=True, exist_ok=True)
                    save_html_path.write_text(page.content(), encoding="utf-8")

                body_text = page.locator("body").inner_text()
                body_lower = body_text.lower()

                # Parse result table (same format for valid and invalid): SSN, Issuance Location,
                # First Year Issued, Last Year Issued, Estimated Age, Valid?
                results_table: dict[str, str] = {}
                table = page.locator("table.table-bordered")
                if table.count() > 0:
                    rows = table.locator("tbody tr")
                    # Match longest first so "first year issued" before "last year issued"
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

                # Result: we want INVALID (not issued) — valid.html: .text-money + "is a valid SSN";
                # invalid.html: .text-negative-money + "may have been issued after June 25, 2011".
                has_valid_ssn = page.locator("span.text-money").count() > 0 and "is a valid ssn" in body_lower
                has_invalid_ssn = (
                    page.locator("span.text-negative-money").count() > 0
                    or "may have been issued after june 25, 2011" in body_lower
                )

                if has_invalid_ssn and not has_valid_ssn:
                    issuance_status = "not_issued"  # invalid / not in pre-2011 DB — what we want
                elif has_valid_ssn:
                    issuance_status = "issued"  # already issued — we don't want this
                else:
                    issuance_status = "error"

                # Death Masterfile: if present on page, "No record" = not deceased
                if "no record" in body_lower:
                    deceased_check = "no_record"
                elif "death" in body_lower and ("record" in body_lower or "found" in body_lower):
                    deceased_check = "found"
                else:
                    deceased_check = "no_record"

                # Success = SSN is invalid (not issued), i.e. usable for our purpose
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
        return {
            "ok": False,
            "error": str(e),
            "full": full_ssn,
            "last_four": last4,
            "deceased_check": "error",
            "issuance_status": "error",
            "results": {},
            "raw_result": None,
        }
    finally:
        _adspower_stop(adspower_profile, adspower_api_base)


def run(
    input_path: Path | None = None,
    output_path: Path | None = None,
    last_four: str | None = None,
    adspower_profile: str = DEFAULT_ADSPOWER_PROFILE,
    adspower_api_base: str = DEFAULT_ADSPOWER_API,
    save_html_path: Path | None = None,
) -> bool:
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
