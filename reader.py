"""Read rendered Forebet pages through the Shifter residential proxy."""

import logging
import os
from datetime import datetime, timezone

from patchright.sync_api import sync_playwright


logger = logging.getLogger(__name__)
LEAGUES = {
    "esb8": "https://www.forebet.com/en/esoccer/esoccer-battle-8-mins-play",
    "volta6": "https://www.forebet.com/en/esoccer/esoccer-battle-volta-6-mins-play",
    "h2hgg": "https://www.forebet.com/en/esoccer/esoccer-h2h-gg-league-8-mins-play",
    "gt12": "https://www.forebet.com/en/esoccer/esoccer-liga-pro-12-mins-play",
}
def is_challenged(page):
    title = page.title().lower()
    try:
        body = page.inner_text("body", timeout=5000).lower()
    except Exception:
        body = ""
    return "just a moment" in title or "verify you are human" in body


def click_cloudflare(page):
    for frame in page.frames:
        if "challenges.cloudflare.com" not in frame.url:
            continue
        try:
            checkbox = frame.locator('input[type="checkbox"]')
            if checkbox.count():
                checkbox.first.click(timeout=3000)
                logger.info("Clicked Cloudflare checkbox inside iframe")
                return True
        except Exception as exc:
            logger.warning("Iframe checkbox click failed: %s", exc)
        try:
            box = frame.frame_element().bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                logger.info("Clicked Cloudflare iframe")
                return True
        except Exception as exc:
            logger.warning("Cloudflare iframe click failed: %s", exc)
    for locator in (
        page.locator('input[type="checkbox"]'),
        page.get_by_text("Verify you are human", exact=False),
    ):
        try:
            if locator.count():
                locator.first.click(timeout=3000)
                logger.info("Clicked visible Cloudflare control")
                return True
        except Exception as exc:
            logger.warning("Cloudflare control click failed: %s", exc)
    return False


def clear_cloudflare(page):
    for attempt in range(2):
        if not is_challenged(page):
            return True
        logger.warning("Cloudflare challenge detected, attempt %d", attempt + 1)
        page.wait_for_timeout(3000)
        if not click_cloudflare(page):
            logger.warning("No visible Cloudflare checkbox was available to click")
        page.wait_for_timeout(5000)
        if not is_challenged(page):
            return True
        if attempt == 0:
            page.wait_for_timeout(10000)
    return not is_challenged(page)


def visit(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if not clear_cloudflare(page):
        raise RuntimeError("Cloudflare challenge did not clear")
    page.wait_for_timeout(5000)


def read_pages():
    """Return each league's raw HTML and rendered text, or an error status."""
    proxy = {
        "server": f"http://{os.environ['SHIFTER_HOST']}:{os.environ['SHIFTER_PORT']}",
        "username": os.environ["SHIFTER_USER"],
        "password": os.environ["SHIFTER_PASS"],
    }
    pages = {}
    profile = os.environ.get("BROWSER_PROFILE_DIR", "/tmp/forebet-chrome-profile")
    os.makedirs(profile, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            profile,
            channel="chrome",
            headless=False,
            proxy=proxy,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 800},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            logger.info("Visiting Forebet homepage at %s", datetime.now(timezone.utc).isoformat())
            try:
                visit(page, "https://www.forebet.com/")
            except Exception:
                logger.exception("Homepage visit failed; continuing with league pages")
            for key, url in LEAGUES.items():
                logger.info("Reading %s at %s", key, datetime.now(timezone.utc).isoformat())
                try:
                    visit(page, url)
                    html = page.content()
                    text = page.inner_text("body")
                    if len(text.strip()) < 100:
                        raise RuntimeError("Rendered page text is empty or incomplete")
                    pages[key] = {"html": html, "text": text, "status": "ok"}
                    logger.info("Read %s: %d text characters", key, len(text))
                except Exception as exc:
                    pages[key] = {"status": "error", "error": str(exc)}
                    logger.exception("Failed to read %s", key)
        finally:
            context.close()
    return pages
