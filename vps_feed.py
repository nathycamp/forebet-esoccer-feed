"""Light, official-only copy of the feed for the VPS (12 Nov, the user: "yes build vps feed ... make it light").

Railway's feed reads Forebet with a headless Chrome through a paid proxy; the VPS is small and shared, so this copy
never reads Forebet. It runs the same app.py (same /predictions and /picks) on the leagues' own data only
(official.py: ESportsBattle for eSB and Volta, H2H GG League's feed for H2H; not GT, which the bots no longer use),
with no Chrome, no proxy, no Telegram, no scheduler package: two small threads.

- Every OFFICIAL_S: the official sources' recent window (7 days of history once at start).
- Every PLAYERS_S: the players Railway's feed lists, saved to STATE_DIR/players.json, so games between players the
  main feed knows are treated the same here (others are "<source>-new", watched but never bet) even when Railway
  is down.
- Every COMPARE_S: Railway's /picks against this copy's, one short line in STATE_DIR/compare.jsonl (kept under
  COMPARE_MAX_BYTES), to judge whether the bots could use this copy when Railway is down. The bots do not read it
  yet (shadow).

Run: python3 vps_feed.py (HOST, PORT, MAIN_FEED, STATE_DIR from the environment); only Flask is needed.
"""
import json
import logging
import os
import sys
import threading
import time
import types
import urllib.request
from datetime import datetime, timezone

HOST, PORT = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8791"))
MAIN_FEED = os.environ.get("MAIN_FEED", "https://forebet-esoccer-feed-production.up.railway.app")
STATE_DIR = os.environ.get("STATE_DIR", os.path.dirname(os.path.abspath(__file__)))
OFFICIAL_S, PLAYERS_S, COMPARE_S = 60, 600, 600
COMPARE_MAX_BYTES = 5_000_000
LEAGUES = {"esb8": "https://www.forebet.com/en/esoccer/esoccer-battle-8-mins-play",
           "volta6": "https://www.forebet.com/en/esoccer/esoccer-battle-volta-6-mins-play",
           "h2hgg": "https://www.forebet.com/en/esoccer/esoccer-h2h-gg-league-8-mins-play"}

# Stand-ins for what app.py imports but this copy never runs: reader.py starts Chrome (patchright), and the
# scheduler package is not installed (the two threads below do its work).
_reader = types.ModuleType("reader")
_reader.LEAGUES = LEAGUES
_reader.demote_last_proxy = lambda *args, **kwargs: None
_reader.load_free_proxy_pool = lambda *args, **kwargs: []
_reader.refresh_free_proxy_pool = lambda *args, **kwargs: None
sys.modules["reader"] = _reader
for _name in ("apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background"):
    sys.modules[_name] = types.ModuleType(_name)
sys.modules["apscheduler.schedulers.background"].BackgroundScheduler = object

import app  # noqa: E402
import official  # noqa: E402

official.gt = lambda start, end: []          # GT is not read here
logger = logging.getLogger("vps_feed")
PLAYERS_FILE = os.path.join(STATE_DIR, "players.json")
COMPARE_FILE = os.path.join(STATE_DIR, "compare.jsonl")
BASE_PLAYERS = {league: set(names) for league, names in official.FOREBET_PLAYERS.items()}


def fetch(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": "forebet-vps-feed", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def use_players(listed):
    """The known players: the file shipped with the feed plus every player the main feed has listed."""
    for league in set(BASE_PLAYERS) | set(listed):
        official.FOREBET_PLAYERS[league] = BASE_PLAYERS.get(league, set()) | {name.casefold() for name in listed.get(league, ())}


def copy_players():
    """Take the players the main feed lists (kept on disk for when it is down)."""
    try:
        leagues = fetch(f"{MAIN_FEED}/predictions", timeout=90)["leagues"]
    except Exception as exc:
        logger.warning("players: main feed not read (%s)", type(exc).__name__)
        return False
    listed = {}
    for league, rows in leagues.items():
        if league in LEAGUES and isinstance(rows, list):
            for row in rows:
                if not str(row.get("source", "")).endswith("-new"):
                    listed.setdefault(league, set()).update(n for n in (row.get("home_tag"), row.get("away_tag")) if n)
    saved = load_players()
    for league, names in saved.items():
        listed.setdefault(league, set()).update(names)
    use_players(listed)
    tmp = PLAYERS_FILE + ".tmp"
    with open(tmp, "w") as handle:
        json.dump({league: sorted(names) for league, names in listed.items()}, handle)
    os.replace(tmp, PLAYERS_FILE)
    return True


def load_players():
    try:
        with open(PLAYERS_FILE) as handle:
            return {league: set(names) for league, names in json.load(handle).items()}
    except (OSError, ValueError):
        return {}


def board_key(row):
    return (row["league"], row["home_tag"].casefold(), row["away_tag"].casefold(), row["kickoff_utc"][:16])


def compare(main_board, own_board, now=None):
    """One line: how many coming games each lists, and for the shared ones how often the advice and the pick agree."""
    main = {board_key(row): row for row in main_board}
    own = {board_key(row): row for row in own_board}
    both = set(main) & set(own)
    advice = lambda row: (row.get("advice") or "").split()[0] if row.get("advice") else ""
    return {"at": (now or datetime.now(timezone.utc)).isoformat(), "main": len(main), "vps": len(own),
            "both": len(both), "only_main": len(set(main) - set(own)), "only_vps": len(set(own) - set(main)),
            "same_advice": sum(advice(main[k]) == advice(own[k]) for k in both),
            "same_selection": sum(main[k].get("selection") == own[k].get("selection") for k in both),
            "main_play": sum(advice(main[k]) == "play" for k in both),
            "both_play": sum(advice(main[k]) == advice(own[k]) == "play" for k in both),
            "differ": [[k[0], k[1], k[2], k[3], advice(main[k]), main[k].get("selection"), advice(own[k]),
                        own[k].get("selection")] for k in sorted(both)
                       if (advice(main[k]), main[k].get("selection")) != (advice(own[k]), own[k].get("selection"))][:20]}


def compare_once():
    try:
        main_board = fetch(f"{MAIN_FEED}/picks", timeout=60)["board"]
    except Exception as exc:
        line = {"at": datetime.now(timezone.utc).isoformat(), "main_error": type(exc).__name__}
    else:
        with app.app.test_client() as client:
            own = client.get("/picks").get_json() or {}
        line = compare(main_board, own.get("board") or [])
    if os.path.exists(COMPARE_FILE) and os.path.getsize(COMPARE_FILE) > COMPARE_MAX_BYTES:
        os.replace(COMPARE_FILE, COMPARE_FILE + ".1")
    with open(COMPARE_FILE, "a") as handle:
        handle.write(json.dumps(line) + "\n")


def every(seconds, job, first_delay=0):
    def loop():
        time.sleep(first_delay)
        while True:
            try:
                job()
            except Exception:
                logger.exception("%s failed", getattr(job, "__name__", "job"))
            time.sleep(seconds)
    threading.Thread(target=loop, daemon=True).start()


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    use_players(load_players())
    every(PLAYERS_S, copy_players)
    threading.Thread(target=lambda: official.refresh(history=True), daemon=True).start()   # serve while it loads
    every(OFFICIAL_S, official.refresh, first_delay=OFFICIAL_S * 3)
    every(COMPARE_S, compare_once, first_delay=120)
    app.app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
