"""
Refresh (rotate) IP for an AdsPower profile.

Opens the AdsPower refresh page in a browser and clicks the Refresh button.
Used when the Steve Morse decoder (or other automation) hits rate limits
(e.g. "unexplained error" or "Please notify me") so the same profile can
get a new IP and retry.

Usage:
  python -m modules.adspower_refresh --profile k17oolu5
  python -m modules.adspower_refresh -p k17oolu5 --headless
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None  # type: ignore[misc, assignment]

DEFAULT_REFRESH_HOST = "127.0.0.1:20725"


def get_refresh_url(profile_id: str, host: str = DEFAULT_REFRESH_HOST) -> str:
    """Build the AdsPower refresh page URL for the given profile."""
    return f"https://start.adspower.net/?id={profile_id}&host={host}"


async def rotate_ip_async(
    profile_id: str,
    *,
    headless: bool = False,
    host: str = DEFAULT_REFRESH_HOST,
) -> bool:
    """
    Open the AdsPower refresh page and click the Refresh button to rotate IP.

    Args:
        profile_id: AdsPower profile ID (e.g. k17oolu5)
        headless: If True, run browser in headless mode
        host: Host for the refresh URL (default 127.0.0.1:20725)

    Returns:
        True if refresh was triggered successfully, False otherwise.
    """
    if async_playwright is None:
        raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

    refresh_url = get_refresh_url(profile_id, host)
    playwright = None
    browser = None
    page = None

    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(refresh_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_load_state("load", timeout=30_000)

        await asyncio.sleep(10)

        refresh_selector = "#app > div._refresh_13aoe_8 > div > div > span"
        refresh_button = await page.query_selector(refresh_selector)

        if not refresh_button:
            refresh_elements = await page.query_selector_all('[class*="refresh"], [class*="Refresh"]')
            if not refresh_elements:
                all_elements = await page.query_selector_all("button, span, a")
                refresh_elements = []
                for el in all_elements:
                    try:
                        text = await el.inner_text()
                        if text and "refresh" in text.lower():
                            refresh_elements.append(el)
                    except Exception:
                        continue
            if refresh_elements:
                refresh_button = refresh_elements[0]

        if not refresh_button:
            try:
                path = f"refresh_page_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=path)
            except Exception:
                pass
            return False

        is_visible = await refresh_button.is_visible()
        if not is_visible:
            await refresh_button.scroll_into_view_if_needed()
            await asyncio.sleep(1)

        await refresh_button.click()
        await asyncio.sleep(2)
        await asyncio.sleep(5)
        return True

    except Exception:
        try:
            if page:
                path = f"refresh_error_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=path)
        except Exception:
            pass
        return False
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass


def rotate_ip_sync(
    profile_id: str,
    *,
    headless: bool = True,
    host: str = DEFAULT_REFRESH_HOST,
) -> bool:
    """Synchronous wrapper for rotate_ip_async. Use from sync code (e.g. inside run_five_digit_decoder)."""
    return asyncio.run(rotate_ip_async(profile_id, headless=headless, host=host))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh (rotate) IP for an AdsPower profile."
    )
    parser.add_argument("--profile", "-p", required=True, help="AdsPower profile ID (e.g. k17oolu5)")
    parser.add_argument("--host", default=DEFAULT_REFRESH_HOST, help="Host for refresh URL")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    args = parser.parse_args()

    if async_playwright is None:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return 1

    success = asyncio.run(rotate_ip_async(args.profile, headless=args.headless, host=args.host))
    return 0 if success else 1


if __name__ == "__main__":
    exit(main())
