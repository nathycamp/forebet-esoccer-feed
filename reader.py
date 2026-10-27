"""Read rendered Forebet pages through the Shifter residential proxy or free proxifly proxies."""

import logging
import json
import os
import random
import re
import socket
import ssl
import sys
import statistics
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from patchright.sync_api import sync_playwright

import esoccerbet


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


def superseded(row, fixtures):
    """A 'live' row is frozen when either player already has a later game live or played."""
    players = {" ".join(str(row.get(side, "")).casefold().split()) for side in ("home_tag", "away_tag")}
    return any(other["kickoff_utc"] > row["kickoff_utc"] and other.get("status") in ("live", "played")
               and players & {" ".join(str(other.get(side, "")).casefold().split()) for side in ("home_tag", "away_tag")}
               for other in fixtures)


def check_live_clock(fixtures, observed_at, duration=None):
    """Detect clock drift without guessing a new offset or making requests.

    A clock held at full time while Forebet awaits FT keeps aging, so only
    clocks before the league's final minute are compared.
    """
    residuals = []
    last_minute = duration - 1 if duration else 20
    for row in fixtures:
        clock = str(row.get("match_clock") or "").strip().rstrip("'′")
        if (row["status"] != "live" or not clock.isdigit() or not 0 <= int(clock) <= last_minute
                or superseded(row, fixtures)):
            continue
        kickoff = datetime.fromisoformat(row["kickoff_utc"])
        residuals.append((observed_at-kickoff).total_seconds()/60-int(clock))
    residual = statistics.median(residuals) if residuals else None
    status = "unverified_no_live_clock" if residual is None else "consistent" if abs(residual) <= 7 else "mismatch"
    if status == "mismatch":
        logger.warning("Kickoff clock mismatch: %.1f minutes across %d live fixtures", residual, len(residuals))
    return {"status": status, "live_fixtures_checked": len(residuals),
            "median_residual_minutes": round(residual, 2) if residual is not None else None,
            "residual_spread_minutes": round(max(residuals) - min(residuals), 2) if residuals else None}


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


def fresh_url(url, now=None):
    """Tag a league URL per read so a cache cannot serve an old copy (CACHE_BUST=0 disables)."""
    if os.environ.get("CACHE_BUST", "1") != "1":
        return url
    stamp = int((now or datetime.now(timezone.utc)).timestamp())
    return f"{url}{'&' if '?' in url else '?'}t={stamp}"


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


PROXY_LIST_URL = "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/US/data.txt"
LAST_PROXY_FILE = os.environ.get("LAST_PROXY_FILE", "/tmp/forebet-last-proxy")
FREE_PROXY_POOL_FILE = os.environ.get("FREE_PROXY_POOL_FILE", "/tmp/forebet-free-proxies.json")
# A free proxy that times out on league pages sits out this long, seeds included.
BENCH_FILE = os.environ.get("PROXY_BENCH_FILE", "/tmp/forebet-proxy-bench.json")
BENCH_MINUTES = int(os.environ.get("PROXY_BENCH_MINUTES", "20"))
# Missed leagues are retried through the next proxy only while this much of the
# app's reader timeout is left, so a retry never costs the leagues already read.
RETRY_RESERVE_SECONDS = 90
MAX_PROXY_ATTEMPTS = 3
MIN_FREE_PROXIES = 5


def tunnel_opens(proxy, host="www.forebet.com", port=443, timeout=6):
    """True when a free proxy tunnels to Forebet (HTTP CONNECT or SOCKS5) and Forebet answers over TLS."""
    parts = urlsplit(proxy)
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=timeout) as conn:
            conn.settimeout(timeout)
            if parts.scheme == "socks5":
                conn.sendall(b"\x05\x01\x00")
                if conn.recv(2) != b"\x05\x00":
                    return False
                conn.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + port.to_bytes(2, "big"))
                reply = conn.recv(10)
                if len(reply) < 2 or reply[1] != 0:
                    return False
            else:
                conn.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
                if conn.recv(4096).split(b" ")[1:2] != [b"200"]:
                    return False
            # Any HTTP reply (Cloudflare's 403 included) shows the proxy really carries Forebet traffic.
            with ssl.create_default_context().wrap_socket(conn, server_hostname=host) as secure:
                secure.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                return secure.recv(16).startswith(b"HTTP/")
    except (OSError, ValueError):
        return False


def free_proxies(limit=MAX_PROXY_ATTEMPTS, sample=800, deadline=20, exclude=()):
    """Return up to limit proxies from the proxifly US list, fastest tunnels first."""
    with urllib.request.urlopen(PROXY_LIST_URL, timeout=10) as response:
        listed = [line.strip() for line in response.read().decode().splitlines()
                  if line.startswith(("http://", "socks5://")) and line.strip() not in exclude]
    random.shuffle(listed)
    found = []
    pool = ThreadPoolExecutor(max_workers=128)
    futures = {pool.submit(tunnel_opens, proxy): proxy for proxy in listed[:sample]}
    try:
        for future in as_completed(futures, timeout=deadline):
            if future.result():
                found.append(futures[future])
                if len(found) >= limit:
                    break
    except TimeoutError:
        pass
    pool.shutdown(wait=False, cancel_futures=True)
    logger.info("Free proxy list: %d listed, %d tested, %d tunnel to Forebet", len(listed), min(sample, len(listed)), len(found))
    return found


def load_free_proxy_pool():
    try:
        with open(FREE_PROXY_POOL_FILE) as handle:
            return [server for server in json.load(handle) if isinstance(server, str)]
    except (OSError, ValueError, TypeError):
        return []


def refresh_free_proxy_pool(minimum=MIN_FREE_PROXIES, spare=3):
    """Retest the pooled free proxies and top the pool up so at least `minimum` tested ones stay ready."""
    pool = load_free_proxy_pool()
    with ThreadPoolExecutor(max_workers=16) as pool_tester:
        alive = [server for server, ok in zip(pool, pool_tester.map(tunnel_opens, pool)) if ok]
    if len(alive) < minimum + spare:
        try:
            alive += free_proxies(limit=minimum + spare - len(alive), exclude=set(alive))
        except Exception:
            logger.exception("Could not load the free proxy list")
    temporary = FREE_PROXY_POOL_FILE + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(alive, handle)
    os.replace(temporary, FREE_PROXY_POOL_FILE)
    level = logging.INFO if len(alive) >= minimum else logging.WARNING
    logger.log(level, "Free proxy pool: %d tested and ready (minimum %d)", len(alive), minimum)
    return alive


def shifter_proxy():
    username = os.environ["SHIFTER_USER"]
    session_id = os.environ.get("SHIFTER_SESSION_ID")
    if session_id and "-sid-" not in username:
        username = f"{username}-sid-{session_id}-ttl-600"
    return {
        "server": f"http://{os.environ['SHIFTER_HOST']}:{os.environ['SHIFTER_PORT']}",
        "username": username,
        "password": os.environ["SHIFTER_PASS"],
    }


def proxy_candidates():
    """Yield Shifter first, then (as a backup) the last free proxy that worked and fresh ones from the list.

    PROXY_SOURCE=shifter disables the backup; PROXY_SOURCE=proxifly skips Shifter.
    """
    source = os.environ.get("PROXY_SOURCE", "auto")
    if source != "proxifly" and os.environ.get("SHIFTER_USER"):
        yield shifter_proxy()
    if source == "shifter":
        return
    servers = []
    try:
        with open(LAST_PROXY_FILE) as handle:
            servers.append(handle.read().strip())
    except OSError:
        pass
    servers += seeds()
    servers += load_free_proxy_pool()
    if len(set(filter(None, servers))) < MAX_PROXY_ATTEMPTS:
        try:
            servers += free_proxies(exclude=set(servers))
        except Exception:
            logger.exception("Could not load the free proxy list")
    servers = list(dict.fromkeys(filter(None, servers)))
    benched = load_bench()
    # Benched proxies go last rather than vanish, in case nothing else works.
    servers = [server for server in servers if server not in benched] + [server for server in servers if server in benched]
    for server in servers[:MAX_PROXY_ATTEMPTS]:
        yield {"server": server}


def seeds():
    """FREE_PROXY_SEEDS lists proxies known to clear Cloudflare; they survive redeploys."""
    return [seed.strip() for seed in os.environ.get("FREE_PROXY_SEEDS", "").split(",") if seed.strip()]


def load_bench(now=None):
    """Free proxies benched for timing out, with when each may be used again."""
    now = now or datetime.now(timezone.utc).timestamp()
    try:
        with open(BENCH_FILE) as handle:
            return {server: until for server, until in json.load(handle).items() if until > now}
    except (OSError, ValueError, AttributeError):
        return {}


def bench_proxy(server, now=None):
    now = now or datetime.now(timezone.utc).timestamp()
    bench = load_bench(now)
    bench[server] = now + BENCH_MINUTES * 60
    try:
        temporary = BENCH_FILE + ".tmp"
        with open(temporary, "w") as handle:
            json.dump(bench, handle)
        os.replace(temporary, BENCH_FILE)
    except OSError:
        logger.exception("Could not bench proxy")
    if os.path.exists(LAST_PROXY_FILE):
        try:
            with open(LAST_PROXY_FILE) as handle:
                if handle.read().strip() == server:
                    os.remove(LAST_PROXY_FILE)
        except OSError:
            pass


def drop_free_proxy(server):
    """Remove a free proxy that failed in Chrome, so later cycles try others."""
    pool = load_free_proxy_pool()
    if server in pool:
        temporary = FREE_PROXY_POOL_FILE + ".tmp"
        with open(temporary, "w") as handle:
            json.dump([other for other in pool if other != server], handle)
        os.replace(temporary, FREE_PROXY_POOL_FILE)


def demote_last_proxy():
    """Stop reusing the last free proxy: it is moved to the back of the pool."""
    try:
        with open(LAST_PROXY_FILE) as handle:
            server = handle.read().strip()
        os.remove(LAST_PROXY_FILE)
    except OSError:
        return None
    pool = load_free_proxy_pool()
    if server in pool:
        pool = [other for other in pool if other != server] + [server]
        temporary = FREE_PROXY_POOL_FILE + ".tmp"
        with open(temporary, "w") as handle:
            json.dump(pool, handle)
        os.replace(temporary, FREE_PROXY_POOL_FILE)
    logger.info("Switching away from free proxy %s after an old Forebet page", server)
    return server


def remember_proxy(server):
    try:
        if server:
            with open(LAST_PROXY_FILE, "w") as handle:
                handle.write(server)
        elif os.path.exists(LAST_PROXY_FILE):
            os.remove(LAST_PROXY_FILE)
    except OSError:
        logger.exception("Could not update the last working proxy")


def read_esoccerbet(page):
    """The backup source's pages through this browser and proxy (its Cloudflare refuses Railway's own address)."""
    pages, failed = {}, []
    for path in esoccerbet.PAGES:
        try:
            visit(page, esoccerbet.BASE + path)
            pages[path] = page.content()
            logger.info("esoccerbet %s: %r, %d characters", path, page.title()[:60], len(pages[path]))
        except Exception as exc:
            failed.append(f"{path}: {type(exc).__name__}")
    if failed:
        logger.warning("esoccerbet pages failed in the browser: %s", "; ".join(failed))
    return {"status": "ok" if pages else "error", "pages": pages, "failed": failed}


def read_pages():
    """Return each league's parsed fixtures, or an error status."""
    retry = os.environ.get("PROXY_SOURCE", "auto") != "shifter"
    leagues = {}
    proxy = None
    started = datetime.now(timezone.utc)
    budget = int(os.environ.get("READER_TIMEOUT_SECONDS", "100")) - RETRY_RESERVE_SECONDS
    profile = os.environ.get("BROWSER_PROFILE_DIR", "/tmp/forebet-chrome-profile")
    os.makedirs(profile, exist_ok=True)
    with sync_playwright() as playwright:
        for number, proxy in enumerate(proxy_candidates(), 1):
            # A proxy that cannot clear the homepage is swapped for the next candidate.
            can_retry = retry and number <= MAX_PROXY_ATTEMPTS
            if "username" not in proxy:
                logger.info("Using backup free proxy %s (candidate %d)", proxy["server"], number)
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
                # SKIP_HOMEPAGE=1 goes straight to the league pages, which clear any
                # Cloudflare challenge themselves; off by default while it is trialled.
                if os.environ.get("SKIP_HOMEPAGE", "0") == "1":
                    logger.info("Skipping Forebet homepage")
                    if blocking and not early:
                        block()
                else:
                    logger.info("Visiting Forebet homepage at %s", datetime.now(timezone.utc).isoformat())
                    try:
                        visit(page, "https://www.forebet.com/")
                        if blocking and not early:
                            block()
                    except Exception:
                        if "username" not in proxy:
                            drop_free_proxy(proxy["server"])
                        out_of_time = leagues and (datetime.now(timezone.utc) - started).total_seconds() >= budget
                        if out_of_time:
                            # Keep what earlier proxies read rather than risk the reader timeout.
                            logger.warning("Homepage failed through %s; out of time, keeping the leagues read",
                                           proxy["server"])
                            break
                        if can_retry:
                            logger.warning("Homepage failed through %s; trying the next proxy",
                                           proxy["server"].rsplit("@", 1)[-1], exc_info=True)
                            continue
                        logger.exception("Homepage visit failed; continuing with league pages")
                for key, url in LEAGUES.items():
                    if leagues.get(key, {}).get("status") == "ok":
                        continue  # read through an earlier proxy this cycle
                    if meter:
                        meter.label = key
                    logger.info("Reading %s at %s", key, datetime.now(timezone.utc).isoformat())
                    try:
                        visit(page, fresh_url(url), fixture_page=True)
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
                if os.environ.get("ESOCCERBET_IN_BROWSER", "0") == "1" and "_esoccerbet" not in leagues:
                    leagues["_esoccerbet"] = read_esoccerbet(page)
            finally:
                if meter:
                    try:
                        meter.log()
                    except Exception:
                        logger.exception("Could not log network bytes")
                context.close()
            missed = [key for key in LEAGUES if leagues.get(key, {}).get("status") != "ok"]
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            if missed and "username" not in proxy:
                # Seeds are the proxies known to clear Cloudflare; benching them leaves
                # only ones that fail the homepage, so a slow seed is retried past, not benched.
                if proxy["server"] not in seeds():
                    bench_proxy(proxy["server"])
                if can_retry and number < MAX_PROXY_ATTEMPTS and elapsed < budget:
                    logger.warning("%s missed %s; retrying them through the next proxy",
                                   proxy["server"], ",".join(missed))
                    continue
            break
    if proxy and "username" not in proxy:
        read_any = any(page.get("status") == "ok" for page in leagues.values())
        remember_proxy(proxy["server"] if read_any else None)
    return leagues


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    json.dump(read_pages(), sys.stdout)
    sys.stdout.write("\n")
