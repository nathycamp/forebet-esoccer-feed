"""Backup source: games and results from esoccerbet.org, read with plain requests (no browser, no proxy).

27 Oct: Forebet stopped updating every eSoccer league for hours while esoccerbet kept listing upcoming games and
results minutes old. Its pages hold the schedule (upcoming games, kickoff in UTC) and the last 20 results of
each league; we keep what we read in memory for a few days (nothing is stored), and the feed merges these rows
into the Forebet ones (merge in app.py): a result fills a Forebet game still shown as unfinished, and a game
Forebet does not list is added, marked source "esoccerbet" so the bots can treat it as a shadow game.

esoccerbet names its leagues by match length: 6 minutes is our volta6, 12 minutes gt12, and 8 minutes holds
both esb8 and h2hgg (told apart by the players we know from Forebet's rows); 10 minutes (Live Arena) is not ours.
Names come in lower case; the merge puts back the case Forebet and SportyBet use.
"""
import html
import logging
import re
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

BASE = "https://esoccerbet.org"
PAGES = ("/schedule/", "/", "/fifa-6-minutes/", "/fifa-8-minutes/", "/fifa-12-minutes/")
MINUTES_LEAGUE = {"6": "volta6", "12": "gt12"}       # 8 minutes: esb8 or h2hgg by player
KEEP_DAYS = 3
BRAZIL_OFFSET_HOURS = 3
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

ROWS = {}            # (minutes, home, away, kickoff) -> row, every game seen in the last KEEP_DAYS
STATUS = {"fetched_at": None, "last_error": None, "rows": 0}
LOCK = threading.Lock()

CARD = re.compile(r"<div class='matchCard'>(.*?)<span class='tmName'>[^<]*</span>\s*</div>\s*</div>", re.S)
PLAYER = re.compile(r"<a href='(?:/fifa-(\d+)-minutes/)?([^/']+)/'>[^<]*</a>\s*<span class='tmName'>([^<]*)</span>")
SCHEDULE_ROW = re.compile(r"<tr class='linhasTR'><td>(\d{1,2}:\d{2})<br><span[^>]*>(\d{1,2})/(\d{1,2})</span></td>(.*?)</tr>",
                          re.S)
PAGE_MINUTES = re.compile(r"/fifa-(\d+)-minutes/$")


def fetch(path, timeout=20):
    request = urllib.request.Request(BASE + path, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def nearest_year(month, day, clock, now):
    """The kickoff (UTC) for a day and month shown without a year: the year that puts it nearest to now.
    The time is UTC (it matches SportyBet's kickoffs) but the date is Brazil's (UTC-3: the schedule said
    "Updated 10/27 17:49" at 20:49 UTC and listed a 01:00 game under 10/27), so before 03:00 it is the next day."""
    hour, minute = (int(part) for part in clock.split(":"))
    options = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            moment = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            continue
        options.append(moment + timedelta(days=1) if hour < BRAZIL_OFFSET_HOURS else moment)
    return min(options, key=lambda moment: abs(moment - now))


def results(page, path, now):
    """Finished games on a results page: [{minutes, home, away, home_club, away_club, kickoff, score}]."""
    page_minutes = (PAGE_MINUTES.search(path) or [None, None])[1]
    out = []
    for card in CARD.finditer(page + "<span class='tmName'></span></div></div>"):
        text = card.group(0)
        players = PLAYER.findall(text)
        nums = re.search(r"<span class='nums'>(.*?)</span>\s*(?:<!--.*?-->\s*)?<span class='matchDate'>([^<]+)</span>",
                         text, re.S)
        if len(players) != 2 or not nums:
            continue
        goals = re.sub(r"<span class='ht'>\(\d+\)</span>", "", nums.group(1))
        goals = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", re.sub(r"<[^>]+>", "", goals))
        when = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2}) (\d{1,2}:\d{2})\s*", nums.group(2))
        minutes = players[0][0] or page_minutes
        if not goals or not when or not minutes:
            continue
        first, second, clock = int(when.group(1)), int(when.group(2)), when.group(3)
        # The home page writes day/month (27/10), the league pages month/day (10/27).
        month, day = (first, second) if path != "/" else (second, first)
        if month > 12:
            month, day = day, month
        out.append({"minutes": minutes, "home": html.unescape(players[0][1]), "away": html.unescape(players[1][1]),
                    "home_club": html.unescape(players[0][2]).strip(), "away_club": html.unescape(players[1][2]).strip(),
                    "kickoff": nearest_year(month, day, clock, now), "score": f"{goals.group(1)}-{goals.group(2)}"})
    return out


def schedule(page, now):
    """Upcoming games on the schedule page."""
    out = []
    for clock, month, day, cells in SCHEDULE_ROW.findall(page):
        players = re.findall(r"<a href='/fifa-(\d+)-minutes/([^/']+)/'>[^<]*</a><span class='teamName'>([^<]*)</span>",
                             cells)
        if len(players) != 2:
            continue
        out.append({"minutes": players[0][0], "home": html.unescape(players[0][1]), "away": html.unescape(players[1][1]),
                    "home_club": html.unescape(players[0][2]).strip(), "away_club": html.unescape(players[1][2]).strip(),
                    "kickoff": nearest_year(int(month), int(day), clock, now), "score": None})
    return out


def remember(games, now):
    """Add the games read now to ROWS (a result replaces the same game's upcoming row) and drop old ones."""
    with LOCK:
        for game in games:
            key = (game["minutes"], game["home"].casefold(), game["away"].casefold(), game["kickoff"])
            if game["score"] or key not in ROWS:
                ROWS[key] = game
        cutoff = now - timedelta(days=KEEP_DAYS)
        for key in [key for key, game in ROWS.items() if game["kickoff"] < cutoff]:
            del ROWS[key]
        STATUS["rows"] = len(ROWS)


def refresh(now=None):
    """Read every page once; a page that fails is skipped and logged."""
    now = now or datetime.now(timezone.utc)
    games, failed = [], []
    for path in PAGES:
        try:
            page = fetch(path)
            games += schedule(page, now) if path == "/schedule/" else results(page, path, now)
        except Exception as exc:
            failed.append(f"{path}: {type(exc).__name__}")
    remember(games, now)
    with LOCK:
        if len(failed) < len(PAGES):
            STATUS["fetched_at"] = now.isoformat()
        STATUS["last_error"] = "; ".join(failed) or None
    if failed:
        logger.warning("esoccerbet: %d/%d pages failed (%s)", len(failed), len(PAGES), "; ".join(failed))
    logger.info("esoccerbet: %d games read, %d kept", len(games), STATUS["rows"])
    return games


def rows():
    with LOCK:
        return list(ROWS.values())
