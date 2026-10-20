"""Read rendered Forebet pages through the Shifter residential proxy."""

import logging
import json
import os
import re
import sys
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from patchright.sync_api import sync_playwright


logger = logging.getLogger(__name__)
LEAGUES = {
    "esb8": "https://www.forebet.com/en/esoccer/esoccer-battle-8-mins-play",
    "volta6": "https://www.forebet.com/en/esoccer/esoccer-battle-volta-6-mins-play",
    "h2hgg": "https://www.forebet.com/en/esoccer/esoccer-h2h-gg-league-8-mins-play",
    "gt12": "https://www.forebet.com/en/esoccer/esoccer-liga-pro-12-mins-play",
}
MATCH_MINUTES = {"esb8": 8, "volta6": 6, "h2hgg": 8, "gt12": 12}
FIXTURE_FIELDS = r"""() => Array.from(document.querySelectorAll('.rcnt'))
    .filter(row => row.querySelector(':scope > .predict, :scope > .predict_y, :scope > .predict_no'))
    .map(row => {
        const field = selector => row.querySelector(selector)?.innerText.trim() || '';
        const clock = field(':scope > .lmin_td');
        const currentScore = field(':scope > .lscr_td .l_scr');
        const finished = /\bFT\b/i.test(clock);
        const live = !finished && (Boolean(currentScore) || (Boolean(clock) && clock !== '0'));
        return {
            home: field('.homeTeam'),
            away: field('.awayTeam'),
            date: field('.date_bah'),
            display_utc_offset_minutes: (() => {
                const selected = document.querySelector('#tzSel')?.value;
                return selected && selected !== 'no' && Number.isFinite(Number(selected))
                    ? Number(selected) * 60 : -new Date().getTimezoneOffset();
            })(),
            probabilities: Array.from(row.querySelectorAll(':scope > .fprc > span'),
                span => span.innerText.trim()),
            pred: field(':scope > .predict .forepr, :scope > .predict_y .forepr, :scope > .predict_no .forepr'),
            score: field(':scope > .ex_sc.tabonly'),
            avg_goals: field(':scope > .avg_sc.tabonly'),
            status: finished ? 'played' : live ? 'live' : 'upcoming',
            actual_score: finished ? currentScore : '',
            live_score: live ? currentScore : '',
            match_clock: finished || live ? clock : '',
        };
    })"""


def parse_team(value):
    match = re.fullmatch(r"(.+?)\s*\(([^()]*)\)\s*", value)
    if not match or not match.group(1).strip() or not match.group(2).strip():
        raise ValueError("team or player tag missing")
    return match.group(1).strip(), match.group(2).strip()


def parse_kickoff(value, display_utc_offset_minutes=0):
    value = " ".join(value.split())
    for date_format in ("%d/%m/%Y %H:%M", "%m/%d/%Y %I:%M %p"):
        try:
            displayed_zone = timezone(timedelta(minutes=float(display_utc_offset_minutes)))
            return datetime.strptime(value, date_format).replace(tzinfo=displayed_zone).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError("invalid kickoff date")


def parse_fixture(fields):
    home_club, home_tag = parse_team(fields["home"])
    away_club, away_tag = parse_team(fields["away"])
    source_kickoff = parse_kickoff(fields["date"], fields.get("display_utc_offset_minutes", 0))
    correction = int(os.environ.get("FOREBET_KICKOFF_CORRECTION_MINUTES", "0"))
    if not -180 <= correction <= 180:
        raise ValueError("kickoff correction must be within three hours")
    kickoff = source_kickoff + timedelta(minutes=correction)
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
    if status not in {"played", "live", "upcoming"}:
        raise ValueError("invalid fixture status")
    actual_score = None
    if status == "played":
        result = re.fullmatch(r"(\d+)\s*-\s*(\d+)", fields["actual_score"])
        if not result:
            raise ValueError("invalid final score")
        actual_score = f"{result.group(1)}-{result.group(2)}"
    live_score = None
    if status == "live" and fields.get("live_score"):
        result = re.fullmatch(r"(\d+)\s*-\s*(\d+)", fields["live_score"])
        if result:
            live_score = f"{result.group(1)}-{result.group(2)}"
        else:
            logger.warning("Invalid live score for %s vs %s", home_tag, away_tag)
    match_clock = fields.get("match_clock") or None
    avg_goals = float(fields["avg_goals"])
    if avg_goals < 0:
        raise ValueError("invalid average goals")
    return {
        "home_tag": home_tag,
        "away_tag": away_tag,
        "home_club": home_club,
        "away_club": away_club,
        "kickoff_utc": kickoff.isoformat(),
        "source_kickoff_utc": source_kickoff.isoformat(),
        "source_display_time": fields["date"],
        "source_utc_offset_minutes": fields.get("display_utc_offset_minutes", 0),
        "kickoff_correction_minutes": correction,
        "kickoff_time_status": "inferred_from_live_clocks" if correction else "source_unverified",
        "p1": probabilities[0],
        "px": probabilities[1],
        "p2": probabilities[2],
        "pred": pred,
        "correct_score": f"{score.group(1)}-{score.group(2)}",
        "avg_goals": avg_goals,
        "status": status,
        "actual_score": actual_score,
        "live_score": live_score,
        "match_clock": match_clock,
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


def check_live_clock(fixtures, observed_at, duration=None):
    """Detect clock drift without guessing a new offset or making requests.

    A clock held at full time while Forebet awaits FT keeps aging, so only
    clocks before the league's final minute are compared.
    """
    residuals = []
    last_minute = duration - 1 if duration else 20
    for row in fixtures:
        clock = str(row.get("match_clock") or "").strip().rstrip("'′")
        if row["status"] != "live" or not clock.isdigit() or not 0 <= int(clock) <= last_minute:
            continue
        kickoff = datetime.fromisoformat(row["kickoff_utc"])
        residuals.append((observed_at-kickoff).total_seconds()/60-int(clock))
    residual = statistics.median(residuals) if residuals else None
    status = "unverified_no_live_clock" if residual is None else "consistent" if abs(residual) <= 7 else "mismatch"
    if status == "mismatch":
        logger.warning("Kickoff clock mismatch: %.1f minutes across %d live fixtures", residual, len(residuals))
    return {"status": status, "live_fixtures_checked": len(residuals),
            "median_residual_minutes": round(residual, 2) if residual is not None else None}


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


# Advertising and analytics hosts the fixture rows never need. Cloudflare's
# challenge (challenges.cloudflare.com) and Forebet's own scripts stay allowed.
THIRD_PARTY_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "adservice.google.com",
    "googletagmanager.com", "googletagservices.com", "google-analytics.com", "analytics.google.com",
    "amazon-adsystem.com", "adnxs.com", "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "pubmatic.com", "rubiconproject.com", "openx.net", "casalemedia.com", "smartadserver.com",
    "teads.tv", "33across.com", "sharethrough.com", "media.net", "adsrvr.org", "bidswitch.net",
    "facebook.net", "connect.facebook.net", "scorecardresearch.com", "quantserve.com",
    "hotjar.com", "clarity.ms", "onesignal.com", "yandex.ru", "mc.yandex.ru",
    # Google's consent and ad-blocker messaging: up to ~575 KB in some cycles.
    "fundingchoicesmessages.google.com",
)


def block_forebet_assets(context, page, third_party=False):
    """Skip visual assets (and optionally ad/analytics hosts) without disabling Chrome's HTTP cache."""
    extensions = ("png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "ico",
                  "woff", "woff2", "ttf", "otf", "mp4", "webm")
    urls = [f"*forebet.com/*.{extension}*" for extension in extensions]
    if third_party:
        urls += [f"*{host}/*" for host in THIRD_PARTY_HOSTS]
    session = context.new_cdp_session(page)
    session.send("Network.enable")
    session.send("Network.setBlockedURLs", {"urls": urls})
    logger.info("Blocking Forebet image, font and media URLs%s", " and ad/analytics hosts" if third_party else "")
    return session


class ByteMeter:
    """Sum bytes actually received over the network, per page visit and per host."""

    def __init__(self, context, page):
        self.label = "home"
        self.urls = {}
        self.by_label = defaultdict(int)
        self.by_host = defaultdict(int)
        self.session = context.new_cdp_session(page)
        self.session.on("Network.responseReceived", self.response)
        self.session.on("Network.loadingFinished", self.finished)
        self.session.send("Network.enable")

    def response(self, event):
        self.urls[event["requestId"]] = event["response"]["url"]

    def finished(self, event):
        size = int(event.get("encodedDataLength") or 0)
        self.by_label[self.label] += size
        self.by_host[urlsplit(self.urls.pop(event["requestId"], "")).hostname or "unknown"] += size

    def log(self):
        total = sum(self.by_label.values())
        hosts = sorted(self.by_host.items(), key=lambda item: item[1], reverse=True)[:5]
        logger.info("Network bytes this cycle: %d KB (%s); top hosts: %s", total // 1024,
                    ", ".join(f"{label} {size // 1024}" for label, size in self.by_label.items()),
                    ", ".join(f"{host} {size // 1024}" for host, size in hosts))


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
            timezone_id="UTC",
        )
        meter = None
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                meter = ByteMeter(context, page)
            except Exception:
                logger.exception("Could not measure network bytes; continuing normally")

            def block():
                try:
                    block_forebet_assets(context, page, third_party=os.environ.get("BLOCK_THIRD_PARTY", "1") == "1")
                except Exception:
                    logger.exception("Could not block visual assets; continuing normally")
            # Blocking before the homepage saves its images and ads too; set
            # BLOCK_BEFORE_HOMEPAGE=0 to restore blocking only after it.
            blocking = os.environ.get("BLOCK_FOREBET_ASSETS", "1") == "1"
            early = blocking and os.environ.get("BLOCK_BEFORE_HOMEPAGE", "1") == "1"
            if early:
                block()
            logger.info("Visiting Forebet homepage at %s", datetime.now(timezone.utc).isoformat())
            try:
                visit(page, "https://www.forebet.com/")
                if blocking and not early:
                    block()
            except Exception:
                logger.exception("Homepage visit failed; continuing with league pages")
            for key, url in LEAGUES.items():
                if meter:
                    meter.label = key
                logger.info("Reading %s at %s", key, datetime.now(timezone.utc).isoformat())
                try:
                    visit(page, url, fixture_page=True)
                    fixtures = extract_fixtures(page, key)
                    observed_at = datetime.now(timezone.utc)
                    leagues[key] = {
                        "fixtures": fixtures,
                        "status": "ok",
                        "fetched_at": observed_at.isoformat().replace("+00:00", "Z"),
                        "clock_check": check_live_clock(fixtures, observed_at, MATCH_MINUTES[key]),
                    }
                except Exception as exc:
                    leagues[key] = {"status": "error", "error": str(exc)}
                    logger.exception("Failed to read %s", key)
        finally:
            if meter:
                try:
                    meter.log()
                except Exception:
                    logger.exception("Could not log network bytes")
            context.close()
    return leagues


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    json.dump(read_pages(), sys.stdout)
    sys.stdout.write("\n")
