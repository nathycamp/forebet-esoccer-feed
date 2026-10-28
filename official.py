"""The leagues' own data: games and results from the organisers' public feeds (the ones their websites read).

27 Oct: Forebet stopped updating eSoccer for hours. The organisers publish every game with its kickoff (UTC),
players and final score:
- ESportsBattle (football.esportsbattle.com) runs eSB (8 minutes, our esb8) and Volta (6 minutes, volta6);
  its "2x6" tournaments are another format and are left out;
- H2H GG League (h2hggl.com, data from api-h2h.cdn-hudstats.com) is our h2hgg;
- GT Sports Leagues (gtleagues.com) is our gt12. Its site refuses Nigeria and answers only requests that look like
  its own pages (their Origin/Referer), so it is read with those headers, and through GT_PROXY when set.

Nothing is stored: the games read are kept in memory for KEEP_DAYS (HISTORY_DAYS are read at start-up), and the
feed merges them into the Forebet rows (app.merge_backup) like esoccerbet's. ESportsBattle and GT run more
tournaments than Forebet lists; only games between players Forebet lists are merged for those leagues
(KNOWN_PLAYERS_ONLY): on the replay of 2,461 recorded games (28 Oct) that beat Forebet's own data (+17.8 vs +16.6
units, worst learner version +4.4 vs +2.7), while all players served best for H2H (the same single league).
"""
import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

ESB = "https://football.esportsbattle.com/api"
H2H = "https://api-h2h.cdn-hudstats.com/v1"
GT = "https://api.gtleagues.com/api"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
GT_HEADERS = {"Origin": "https://www.gtleagues.com", "Referer": "https://www.gtleagues.com/"}
HISTORY_DAYS = 7          # the replay (28 Oct): 7 days of history beat 3
KEEP_DAYS = 8
WINDOW = (timedelta(hours=3), timedelta(hours=2))     # each refresh reads games from 3 hours ago to 2 hours ahead
GT_PAGE = 100
ESB_WORKERS = 8
KNOWN_PLAYERS_ONLY = {"esb8", "volta6", "gt12"}

ROWS = {}            # (league, home, away, kickoff) -> game
STATUS = {}          # source -> {"fetched_at", "last_error", "games"}
LOCK = threading.Lock()


def get(url, headers=None, timeout=25, proxy=None):
    request = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT, "Accept": "application/json"},
                                                       **(headers or {})))
    opener = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
              if proxy else urllib.request.build_opener())
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def game(league, home, away, home_club, away_club, kickoff, score, status, source):
    return {"league": league, "home": home, "away": away, "home_club": home_club or "", "away_club": away_club or "",
            "kickoff": kickoff, "score": score, "status": status, "source": source}


# ------------------------------------------------------------------ ESportsBattle: eSB and Volta
def esb_league(name):
    if "2x6" in name:
        return None
    return "volta6" if name.startswith("Volta") else "esb8"


def esportsbattle(start, end):
    """Games of the tournaments starting between start and end (UTC)."""
    query = urllib.parse.urlencode({"page": 1, "dateFrom": f"{start:%Y/%m/%d %H:%M}", "dateTo": f"{end:%Y/%m/%d %H:%M}"})
    out = []
    pages = get(f"{ESB}/tournaments?{query}")
    tournaments = list(pages.get("tournaments") or [])
    for page in range(2, (pages.get("totalPages") or 1) + 1):
        tournaments += get(f"{ESB}/tournaments?{query.replace('page=1', f'page={page}')}").get("tournaments") or []
    tournaments = [(t, esb_league(t.get("token_international") or "")) for t in tournaments]
    tournaments = [(t, league) for t, league in tournaments if league]
    with ThreadPoolExecutor(ESB_WORKERS) as pool:
        matches = list(pool.map(lambda item: get(f"{ESB}/tournaments/{item[0]['id']}/matches"), tournaments))
    for (tournament, league), listed in zip(tournaments, matches):
        for match in listed:
            one, two = match["participant1"], match["participant2"]
            status = {1: "upcoming", 2: "live", 3: "played"}.get(match.get("status_id"))
            if not status:
                continue
            score = f"{one.get('score')}-{two.get('score')}" if status == "played" else None
            out.append(game(league, one["nickname"], two["nickname"], (one.get("team") or {}).get("token_international"),
                            (two.get("team") or {}).get("token_international"), utc(match["date"]), score, status,
                            "esportsbattle"))
    return out


# ------------------------------------------------------------------ H2H GG League
def h2h(day):
    """Every H2H GG League game of a UTC day."""
    rows = get(f"{H2H}/schedule/fifa?date={day:%Y-%m-%d}T00:00:00%2B00:00")
    out = []
    for row in rows:
        if row.get("isCancelled") or row.get("tournamentName") != "Esoccer H2H GG League":
            continue
        ended = row.get("matchStatus") == "MATCH_ENDED"
        kickoff = utc(row["startDate"])
        status = "played" if ended else "upcoming" if kickoff > datetime.now(timezone.utc) else "live"
        score = f"{row['teamAScore']}-{row['teamBScore']}" if ended and row.get("teamAScore") is not None else None
        if ended and not score:
            continue
        out.append(game("h2hgg", row["participantAName"], row["participantBName"], row.get("teamAName"),
                        row.get("teamBName"), kickoff, score, status, "h2hggl"))
    return out


# ------------------------------------------------------------------ GT Leagues
def gt(start, end):
    """GT Leagues games kicking off between start and end."""
    proxy = os.environ.get("GT_PROXY") or None
    out, offset = [], 0
    window = f"between:{start:%Y-%m-%dT%H:%M:%S}.000Z,{end:%Y-%m-%dT%H:%M:%S}.999Z"
    while True:
        query = urllib.parse.urlencode({"kickoff": window, "limit": GT_PAGE, "offset": offset, "sort": "-kickoff,-matchNr"})
        rows = get(f"{GT}/fixtures?{query}", GT_HEADERS, proxy=proxy)
        for row in rows:
            sides = {part.get("side"): part.get("participant") or {} for part in row.get("participants") or []}
            home, away = sides.get("home") or {}, sides.get("away") or {}
            if not (home.get("player") and away.get("player")):
                continue
            stats = (row.get("result") or {}).get("stats") or {}
            finished = row.get("status") == 3 and stats.get("home_score") is not None
            kickoff = utc(row["kickoff"])
            status = "played" if finished else "upcoming" if kickoff > datetime.now(timezone.utc) else "live"
            out.append(game("gt12", home["player"]["nickname"].strip(), away["player"]["nickname"].strip(),
                            (home.get("team") or {}).get("name"), (away.get("team") or {}).get("name"), kickoff,
                            f"{stats['home_score']}-{stats['away_score']}" if finished else None, status, "gtleagues"))
        if len(rows) < GT_PAGE:
            return out
        offset += GT_PAGE


# ------------------------------------------------------------------ store
def remember(games, now):
    with LOCK:
        for one in games:
            key = (one["league"], one["home"].casefold(), one["away"].casefold(), one["kickoff"])
            if one["score"] or key not in ROWS or ROWS[key]["status"] != "played":
                ROWS[key] = one
        cutoff = now - timedelta(days=KEEP_DAYS)
        for key in [key for key, one in ROWS.items() if one["kickoff"] < cutoff]:
            del ROWS[key]


def read(source, reader, now):
    try:
        games = reader()
    except Exception as exc:
        with LOCK:
            STATUS.setdefault(source, {})["last_error"] = f"{type(exc).__name__} {getattr(exc, 'code', '')}".strip()
        logger.warning("%s: read failed (%s: %s)", source, type(exc).__name__, str(exc)[:120])
        return []
    remember(games, now)
    with LOCK:
        STATUS[source] = {"fetched_at": now.isoformat(), "last_error": None, "games": len(games)}
    return games


def refresh(now=None, history=False):
    """Read the recent window (or HISTORY_DAYS of history) from every source."""
    now = now or datetime.now(timezone.utc)
    start, end = ((now - timedelta(days=HISTORY_DAYS), now + WINDOW[1]) if history
                  else (now - WINDOW[0], now + WINDOW[1]))
    days = sorted({(start + timedelta(days=n)).date() for n in range((end - start).days + 2)
                   if start.date() <= (start + timedelta(days=n)).date() <= end.date()})
    total = 0
    total += len(read("esportsbattle", lambda: esportsbattle(start - timedelta(hours=2), end), now))
    total += len(read("h2hggl", lambda: [g for day in days for g in h2h(datetime(day.year, day.month, day.day))
                                         if start <= g["kickoff"] <= end], now))
    total += len(read("gtleagues", lambda: gt(start, end), now))
    logger.info("official sources: %d games read%s, %d kept", total, " (history)" if history else "", len(ROWS))
    return total


def rows():
    with LOCK:
        return list(ROWS.values())


def fresh_sources(now, max_age_s):
    with LOCK:
        return {source for source, status in STATUS.items() if status.get("fetched_at")
                and (now - datetime.fromisoformat(status["fetched_at"])).total_seconds() <= max_age_s}
