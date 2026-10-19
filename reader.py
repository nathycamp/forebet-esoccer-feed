"""Read rendered Forebet pages through the Shifter residential proxy."""

import logging
import json
import os
import re
import sys
from datetime import datetime, timezone

from patchright.sync_api import sync_playwright


logger = logging.getLogger(__name__)
LEAGUES = {
    "esb8": "https://www.forebet.com/en/esoccer/esoccer-battle-8-mins-play",
    "volta6": "https://www.forebet.com/en/esoccer/esoccer-battle-volta-6-mins-play",
    "h2hgg": "https://www.forebet.com/en/esoccer/esoccer-h2h-gg-league-8-mins-play",
    "gt12": "https://www.forebet.com/en/esoccer/esoccer-liga-pro-12-mins-play",
}
FIXTURE_FIELDS = """() => Array.from(document.querySelectorAll('.rcnt'))
    .filter(row => row.querySelector(':scope > .predict, :scope > .predict_y, :scope > .predict_no'))
    .map(row => {
        const field = selector => row.querySelector(selector)?.innerText.trim() || '';
        const finished = field(':scope > .lmin_td').includes('FT');
        return {
            home: field('.homeTeam'),
            away: field('.awayTeam'),
            date: field('.date_bah'),
            probabilities: Array.from(row.querySelectorAll(':scope > .fprc > span'),
                span => span.innerText.trim()),
            pred: field(':scope > .predict .forepr, :scope > .predict_y .forepr, :scope > .predict_no .forepr'),
            score: field(':scope > .ex_sc.tabonly'),
            avg_goals: field(':scope > .avg_sc.tabonly'),
            status: finished ? 'played' : 'upcoming',
            actual_score: finished ? field(':scope > .lscr_td .l_scr') : '',
        };
    })"""


def parse_team(value):
    match = re.fullmatch(r"(.+?)\s*\(([^()]*)\)\s*", value)
    if not match or not match.group(1).strip() or not match.group(2).strip():
        raise ValueError("team or player tag missing")
    return match.group(1).strip(), match.group(2).strip()


def parse_kickoff(value):
    value = " ".join(value.split())
    for date_format in ("%d/%m/%Y %H:%M", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError("invalid kickoff date")


def parse_fixture(fields):
    home_club, home_tag = parse_team(fields["home"])
    away_club, away_tag = parse_team(fields["away"])
    kickoff = parse_kickoff(fields["date"])
    probabilities = [int(value.strip().rstrip("%")) for value in fields["probabilities"]]
    if len(probabilities) != 3 or any(value < 0 or value > 100 for value in probabilities):
        raise ValueError("invalid 1X2 probabilities")
    pred = fields["pred"].upper()
    if pred not in {"1", "X", "2"}:
        raise ValueError("invalid predicted outcome")
    score = re.fullmatch(r"(\d+)\s*-\s*(\d+)", fields["score"])
    if not score:
        raise ValueError("invalid predicted score")
    status = fields["status"]
    if status not in {"played", "upcoming"}:
        raise ValueError("invalid fixture status")
    actual_score = None
    if status == "played":
        result = re.fullmatch(r"(\d+)\s*-\s*(\d+)", fields["actual_score"])
        if not result:
            raise ValueError("invalid final score")
        actual_score = f"{result.group(1)}-{result.group(2)}"
    avg_goals = float(fields["avg_goals"])
    if avg_goals < 0:
        raise ValueError("invalid average goals")
    return {
        "home_tag": home_tag,
        "away_tag": away_tag,
        "home_club": home_club,
        "away_club": away_club,
        "kickoff_utc": kickoff.isoformat(),
        "p1": probabilities[0],
        "px": probabilities[1],
        "p2": probabilities[2],
        "pred": pred,
        "correct_score": f"{score.group(1)}-{score.group(2)}",
        "avg_goals": avg_goals,
        "status": status,
        "actual_score": actual_score,
    }


def extract_fixtures(page, league):
    rows = page.evaluate(FIXTURE_FIELDS)
    fixtures = []
    skipped = 0
    for index, fields in enumerate(rows):
        try:
            fixtures.append(parse_fixture(fields))
        except (KeyError, TypeError, ValueError) as exc:
            skipped += 1
            if skipped <= 5:
                logger.warning("Skipping %s fixture row %d: %s", league, index, exc)
    if skipped > 5:
        logger.warning("Skipped %d unparsable %s rows in total", skipped, league)
    if not fixtures:
        raise RuntimeError("No valid fixture rows found")
    logger.info("Parsed %s: %d/%d fixtures", league, len(fixtures), len(rows))
    return fixtures


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


def visit(page, url, fixture_page=False):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if not clear_cloudflare(page):
        raise RuntimeError("Cloudflare challenge did not clear")
    if fixture_page:
        page.wait_for_function("""() => Array.from(document.querySelectorAll('.rcnt')).some(
            row => row.querySelector(':scope > .predict, :scope > .predict_y, :scope > .predict_no')
                && row.querySelector('.date_bah')?.textContent.trim()
                && row.querySelectorAll(':scope > .fprc > span').length === 3
        )""", timeout=12000)
    else:
        page.wait_for_timeout(1000)


def read_pages():
    """Return each league's parsed fixtures, or an error status."""
    username = os.environ["SHIFTER_USER"]
    session_id = os.environ.get("SHIFTER_SESSION_ID")
    if session_id and "-sid-" not in username:
        username = f"{username}-sid-{session_id}-ttl-600"
    proxy = {
        "server": f"http://{os.environ['SHIFTER_HOST']}:{os.environ['SHIFTER_PORT']}",
        "username": username,
        "password": os.environ["SHIFTER_PASS"],
    }
    leagues = {}
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
                    visit(page, url, fixture_page=True)
                    leagues[key] = {
                        "fixtures": extract_fixtures(page, key),
                        "status": "ok",
                        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                except Exception as exc:
                    leagues[key] = {"status": "error", "error": str(exc)}
                    logger.exception("Failed to read %s", key)
        finally:
            context.close()
    return leagues


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    json.dump(read_pages(), sys.stdout)
    sys.stdout.write("\n")
