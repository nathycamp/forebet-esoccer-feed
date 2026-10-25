"""Railway HTTP service for Forebet esoccer predictions."""

import logging
import gzip
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, jsonify, request

import telegram_bot
from prediction_engine import MODEL_VERSION, predict_feed
from prediction_engine.model import tag
from prediction_engine.validation import LEAGUES as LEAGUE_MINUTES, utc as parse_time
from reader import LEAGUES, demote_last_proxy, load_free_proxy_pool, refresh_free_proxy_pool


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
app = Flask(__name__)
cache_lock = threading.Lock()
cache = {
    "last_fetch": None,
    "last_status": "error",
    "fetched_at": None,
    "stale": True,
    "leagues": {},
}
MAX_CACHE_AGE_SECONDS = 6 * 60
READER_TIMEOUT_SECONDS = int(os.environ.get("READER_TIMEOUT_SECONDS", "100"))
# Picks flip from "wait" to "play" only 2-4 minutes before kickoff, so read every minute.
READER_INTERVAL_SECONDS = int(os.environ.get("READER_INTERVAL_SECONDS", "60"))
after_refresh = []  # callables run right after each read, e.g. Telegram alerts
proxy_session_id = uuid.uuid4().hex[:12]
FIXTURE_FIELDS = {
    "home_tag", "away_tag", "home_club", "away_club", "kickoff_utc",
    "source_kickoff_utc", "source_display_time", "source_utc_offset_minutes",
    "kickoff_correction_minutes", "kickoff_time_status", "p1", "px", "p2",
    "pred", "correct_score", "avg_goals", "status", "actual_score",
    "live_score", "match_clock",
}
PICK_FIELDS = {
    "home_tag", "away_tag", "home_club", "away_club", "kickoff_utc",
    "goals_line", "goals_call", "goals_probability", "expected_goals",
    "goals_confidence", "favourite", "favourite_probability",
    "favourite_strength", "call_status", "warnings",
    "one_x_two", "double_chance", "winner_markets_trust", "safest_option",
    "goal_lines", "kickoff_adjusted_minutes", "listed_kickoff_utc", "best_available",
    "advice", "previous_game", "playable_options",
}
GOAL_LINES = [line + .5 for line in range(0, 10)]
# Picking the likeliest of the lines a bookmaker shows (e.g. 4.5 and 5.5) also
# beat league rates in esb8 and volta6 replays (71-75% landed vs 59-60%).
MAX_AVAILABLE_LINES = 8
# Replays of the feed's own results showed a goals edge at the league line here
# (2026-10-23: 60%+ calls landed esb8 71%, volta6 74%, gt12 78% vs 52-56% league
# rates); h2hgg's calls only matched its low-scoring league rate (+6), so it stays out.
TRUSTED_GOAL_LEAGUES = {"esb8", "volta6", "gt12"}
# Near-even calls and players new to the feed carry no real model opinion. 3 games each is enough: on a
# replay of the feed's own results (2026-10-25), calls at the play threshold hit 75% with 3-4 games of
# history (8 calls) and 75% with 5+ (199), but 61% with 0-2 (18), so returning players get calls sooner.
MIN_CALL_PROBABILITY = .55
MIN_PLAYER_GAMES = 3
# Form changes between sessions, so only imminent games get picks.
PICK_WINDOW_MINUTES = 20
# Replays: over/under at the league line beat league rates in esb8 and volta6
# (73% landed at 60%+), while model 1X2 and double-chance picks did worse than
# simply taking the league's most common result. So the safest option is the
# goals call wherever that edge applies, and top picks rank those first.
# A steady Forebet delay in the proven leagues shifts kickoffs instead of
# withholding picks; an uneven or extreme delay still withholds them.
DELAY_ADJUST_LEAGUES = TRUSTED_GOAL_LEAGUES
DELAY_MIN_FIXTURES = 2
DELAY_MAX_SPREAD_MINUTES = 3
DELAY_MAX_MINUTES = 45
TOP_PICK_MIN_PROBABILITY = .60
# GT12 scores swing more (frequent 10+ goal games), so it needs 65%: its 65%+
# replay calls landed 72-77% against 52-60% league rates.
LEAGUE_MIN_PROBABILITY = {"gt12": .65}
# Replays: 60%+ calls landed 72% when each player's latest game was known, 67% when
# it was not, so a pick waits until any earlier game of its players is half played.
PREVIOUS_GAME_MIN_SHARE = .5
TOP_PICKS = 2
picks_lock = threading.Lock()
picks_cache = {"key": None, "result": None}


def query_options(fields_allowed=FIXTURE_FIELDS, with_status=True, extra=()):
    allowed = {"league", "player", "limit", "fields", *extra} | ({"status"} if with_status else set())
    unknown = set(request.args) - allowed
    if unknown:
        raise ValueError("Unknown query parameter: " + ", ".join(sorted(unknown)))

    def choices(name, allowed, default):
        if name not in request.args:
            return default
        values = [value.strip().lower() for group in request.args.getlist(name) for value in group.split(",")]
        if not values or any(not value or value not in allowed for value in values):
            raise ValueError(f"Invalid {name}; allowed values: {', '.join(sorted(allowed))}")
        return set(values)

    leagues = choices("league", LEAGUES, set(LEAGUES))
    statuses = choices("status", {"live", "upcoming", "played"}, None)
    fields = choices("fields", fields_allowed, None)
    limit = None
    for name in ("limit", "player", *extra):
        if len(request.args.getlist(name)) > 1:
            raise ValueError(f"Specify {name} only once")
    if "limit" in request.args:
        value = request.args["limit"]
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000 per league")
        limit = int(value)
    player = None
    if "player" in request.args:
        player = " ".join(request.args["player"].casefold().split())
        if not player or len(player) > 100:
            raise ValueError("player must be a player tag between 1 and 100 characters")
    return leagues, statuses, fields, limit, player


def is_future_fixture(row, now):
    try:
        return datetime.fromisoformat(row["kickoff_utc"].replace("Z", "+00:00")) > now
    except (KeyError, ValueError, TypeError):
        return False


def feed_response(payload):
    """Compact, conditionally cached response; never initiates a reader job."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    response = Response(body, mimetype="application/json")
    # A weak validator covers the same JSON in either wire encoding.
    response.set_etag(hashlib.sha256(body).hexdigest(), weak=True)
    response.headers["Cache-Control"] = "no-cache"
    response.vary.add("Accept-Encoding")
    response.make_conditional(request)
    if response.status_code == 200 and len(body) >= 512 and request.accept_encodings["gzip"] > 0:
        response.set_data(gzip.compress(body, compresslevel=1, mtime=0))
        response.headers["Content-Encoding"] = "gzip"
    return response


def now_utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_reader():
    """Isolate Chrome so a stuck read cannot block future scheduled jobs."""
    environment = os.environ.copy()
    environment["SHIFTER_SESSION_ID"] = proxy_session_id
    process = subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).with_name("reader.py"))],
        stdout=subprocess.PIPE,
        start_new_session=True,
        env=environment,
    )
    try:
        output, _ = process.communicate(timeout=READER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise TimeoutError(f"Reader exceeded {READER_TIMEOUT_SECONDS} seconds") from exc
    if process.returncode != 0:
        raise RuntimeError(f"Reader exited with code {process.returncode}")
    return json.loads(output)


PROGRESS = {"upcoming": 0, "live": 1, "played": 2}


def keep_progress(fixtures, previous):
    """Never let a game go backwards: Forebet sometimes shows a game it already had
    live (or finished) as upcoming again, which would stall the previous-game wait."""
    seen = {(row.get("home_tag"), row.get("away_tag"), row.get("kickoff_utc")): row for row in previous}
    kept = []
    for row in fixtures:
        earlier = seen.get((row.get("home_tag"), row.get("away_tag"), row.get("kickoff_utc")))
        if earlier and PROGRESS.get(earlier.get("status"), 0) > PROGRESS.get(row.get("status"), 0):
            kept.append(earlier)
        else:
            kept.append(row)
    return kept


def merge_older_copy(fixtures, previous):
    """Forebet also serves older copies of a page. Take its fixtures (it may list new upcoming games) but
    never lose a result we already have: games it shows behind are kept as we had them (keep_progress), and
    finished games it no longer lists are added back."""
    merged = keep_progress(fixtures, previous)
    listed = {(row.get("home_tag"), row.get("away_tag"), row.get("kickoff_utc")) for row in merged}
    missing = [row for row in previous if row.get("status") in ("played", "live")
               and (row.get("home_tag"), row.get("away_tag"), row.get("kickoff_utc")) not in listed]
    return sorted(merged + missing, key=lambda row: row.get("kickoff_utc", ""))


def latest_result(page):
    """Kickoff of the newest finished game on a league page ('' when none)."""
    return max((row.get("kickoff_utc", "") for row in (page or {}).get("fixtures", [])
                if row.get("status") == "played"), default="")


def refresh_pages():
    global proxy_session_id
    attempted_at = now_utc()
    try:
        results = run_reader()
        completed_at = now_utc()
        successful = {key: page for key, page in results.items() if page.get("status") == "ok"}
        stale_pages = False
        with cache_lock:
            # Forebet sometimes serves an older copy of a page. Dropping it froze the league until it went
            # stale (no picks for many minutes), so it is merged instead: its fixtures are used, our newer
            # results are kept, and nothing rolls back.
            for key in list(successful):
                previous = cache["leagues"].get(key)
                if previous and latest_result(successful[key]) < latest_result(previous):
                    logger.warning("Older Forebet %s page (newest result %s, ours %s): merged, keeping our newer "
                                   "results", key, latest_result(successful[key]), latest_result(previous))
                    successful[key] = dict(successful[key], fixtures=merge_older_copy(successful[key]["fixtures"],
                                                                                    previous["fixtures"]))
                    stale_pages = True
        if stale_pages:
            # Old copies seem tied to the route, so the next read goes another way.
            proxy_session_id = uuid.uuid4().hex[:12]
            demote_last_proxy()
        complete = len(successful) == len(LEAGUES)
        with cache_lock:
            for key, page in successful.items():
                previous = cache["leagues"].get(key)
                if previous:
                    page["fixtures"] = keep_progress(page["fixtures"], previous["fixtures"])
                page["fetched_at"] = page.get("fetched_at", completed_at)
                page["changed_at"] = (
                    previous.get("changed_at", page["fetched_at"])
                    if previous and previous["fixtures"] == page["fixtures"]
                    else page["fetched_at"]
                )
                cache["leagues"][key] = page
            cache["last_fetch"] = attempted_at
            if successful:
                cache["fetched_at"] = completed_at
            # A partial read keeps the leagues it got; each league's own age (snapshot)
            # decides freshness, so one missed league does not mark the whole feed stale.
            cache["last_status"] = "ok" if complete else "partial" if successful else "error"
            cache["stale"] = not successful
        logger.info("Reader cycle: %d/%d leagues read", len(successful), len(LEAGUES))
        if not successful:
            proxy_session_id = uuid.uuid4().hex[:12]
            logger.warning("No leagues read; rotating Shifter session for next cycle")
    except Exception:
        logger.exception("Reader cycle failed")
        proxy_session_id = uuid.uuid4().hex[:12]
        logger.warning("Rotating Shifter session after reader failure")
        with cache_lock:
            cache["last_fetch"] = attempted_at
            cache["last_status"] = "error"
            cache["stale"] = True


@app.get("/")
def health():
    with cache_lock:
        return jsonify({
            "status": "ok",
            "last_fetch": cache["last_fetch"],
            "last_status": cache["last_status"],
            "free_proxies_ready": len(load_free_proxy_pool()),
            "league_age_seconds": {key: round((datetime.now(timezone.utc) - datetime.fromisoformat(page["fetched_at"].replace("Z", "+00:00"))).total_seconds())
                                   for key, page in cache["leagues"].items() if page.get("fetched_at")},
            "telegram": dict(telegram_bot.STATUS),
        })


def snapshot(selected, now):
    """Copy the cached feed for the selected leagues with freshness metadata."""
    with cache_lock:
        leagues = {}
        league_fetched_at = {}
        league_changed_at = {}
        league_clock_checks = {}
        too_old = False
        for key in LEAGUES:
            if key not in selected:
                continue
            saved = cache["leagues"].get(key)
            leagues[key] = saved["fixtures"] if saved else {"status": "error"}
            fetched_at = saved.get("fetched_at") if saved else None
            league_fetched_at[key] = fetched_at
            league_changed_at[key] = saved.get("changed_at") if saved else None
            league_clock_checks[key] = saved.get("clock_check") if saved else None
            if league_clock_checks[key] and league_clock_checks[key].get("status") == "mismatch":
                too_old = True
            if not fetched_at or (now - datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))).total_seconds() > MAX_CACHE_AGE_SECONDS:
                too_old = True
        return {
            "fetched_at": cache["fetched_at"],
            "source": "railway-shifter-reader",
            "stale": cache["stale"] or too_old,
            "league_fetched_at": league_fetched_at,
            "league_changed_at": league_changed_at,
            "league_clock_checks": league_clock_checks,
            "leagues": leagues,
        }


@app.get("/predictions")
def predictions():
    try:
        selected, statuses, fields, limit, player = query_options()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    now = datetime.now(timezone.utc)
    payload = snapshot(selected, now)
    leagues = payload["leagues"]
    # Work on copies after releasing the cache lock so filtering cannot alter
    # the cached full feed or hold up the background refresh.
    for key, rows in leagues.items():
        if not isinstance(rows, list):
            continue
        if request.args:
            rows = [row for row in rows if (statuses is None or row.get("status") in statuses)
                    and not (statuses is not None and row.get("status") == "upcoming"
                             and not is_future_fixture(row, now))
                    and (player is None or matches_player(row, player))]
            rows.sort(key=lambda row: row.get("kickoff_utc", ""), reverse=statuses == {"played"})
            if limit is not None:
                rows = rows[:limit]
            if fields is not None:
                rows = [{name: value for name, value in row.items() if name in fields} for row in rows]
            leagues[key] = rows
    return feed_response(payload)


def matches_player(row, player):
    return any(" ".join(str(row.get(side, "")).casefold().split()) == player
               for side in ("home_tag", "away_tag"))


def unique_fixtures(rows):
    """One row per players + kickoff (Forebet sometimes lists a game twice under two club names)."""
    rank = {"played": 0, "live": 1, "upcoming": 2}
    best = {}
    for row in rows:
        try:
            key = (tag(row["home_tag"]), tag(row["away_tag"]), parse_time(row["kickoff_utc"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if key not in best or rank.get(row.get("status"), 3) < rank.get(best[key].get("status"), 3):
            best[key] = row
    return list(best.values())


def league_totals(rows, now):
    totals = []
    for row in rows:
        try:
            if row.get("status") == "played" and now - datetime.fromisoformat(row["kickoff_utc"].replace("Z", "+00:00")).astimezone(timezone.utc) <= timedelta(days=30):
                totals.append(sum(int(goals) for goals in row["actual_score"].split("-")))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return totals


def league_goal_line(rows, now):
    """The league's usual x.5 line from this snapshot's results, and how often it went over."""
    totals = league_totals(rows, now)
    if not totals:
        return None, None
    line = min(8.5, max(1.5, math.floor(sum(totals) / len(totals)) + .5))
    return line, sum(total > line for total in totals) / len(totals)


def outcome_rates(rows):
    counts = {"1": 1, "X": 1, "2": 1}
    for row in rows:
        try:
            if row.get("status") == "played":
                home, away = map(int, row["actual_score"].split("-"))
                counts["1" if home > away else "2" if away > home else "X"] += 1
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    total = sum(counts.values())
    return {key: value / total for key, value in counts.items()}


def safest_option(pick, winner, rates, over, over_rate, league):
    """The likeliest selection across 1X2, double chance and the league goals line."""
    home, away = pick["home_tag"], pick["away_tag"]
    side = "over" if over >= .5 else "under"
    options = [("1x2", f"1 ({home} win)", winner["1"], rates["1"]),
               ("1x2", "X (draw)", winner["X"], rates["X"]),
               ("1x2", f"2 ({away} win)", winner["2"], rates["2"]),
               ("double_chance", f"1X ({home} or draw)", winner["1"] + winner["X"], rates["1"] + rates["X"]),
               ("double_chance", f"X2 ({away} or draw)", winner["X"] + winner["2"], rates["X"] + rates["2"]),
               ("double_chance", "12 (no draw)", winner["1"] + winner["2"], rates["1"] + rates["2"]),
               ("over_under", f"{side} {pick['goals_line']}", max(over, 1 - over),
                over_rate if side == "over" else 1 - over_rate)]
    proven = league in TRUSTED_GOAL_LEAGUES and pick["goals_call"] is not None
    # A high "no draw" chance mostly mirrors the league's draw rate, so the proven goals call wins.
    market, selection, probability, league_rate = options[-1] if proven else max(options, key=lambda option: option[2])
    return {"market": market, "selection": selection, "probability": round(probability, 4),
            "fair_odds": round(1 / probability, 2) if probability else None,
            "league_rate": round(league_rate, 4), "proven_edge": proven}


def goal_lines(totals):
    """Over/under chances and fair odds at every x.5 line, to compare with a bookmaker's prices."""
    lines = {}
    for line, value in sorted(totals.items(), key=lambda item: float(item[0])):
        if float(line) % 1 != .5 or not .5 <= float(line) <= 9.5:
            continue
        over = value["over"]
        lines[line] = {"over": round(over, 4), "under": round(1 - over, 4),
                       "fair_over_odds": round(1 / over, 2) if over > .001 else None,
                       "fair_under_odds": round(1 / (1 - over), 2) if over < .999 else None}
    return lines


def steady_delay(check):
    """Whole minutes a league runs late when its live clocks agree, else None."""
    check = check or {}
    delay, spread = check.get("median_residual_minutes"), check.get("residual_spread_minutes")
    if (check.get("status") != "mismatch" or delay is None or spread is None
            or check.get("live_fixtures_checked", 0) < DELAY_MIN_FIXTURES
            or spread > DELAY_MAX_SPREAD_MINUTES or not 0 < delay <= DELAY_MAX_MINUTES):
        return None
    return round(delay)


def adjust_delays(feed):
    """Shift unplayed kickoffs in leagues with a steady delay; returns (feed, minutes by league)."""
    delays = {league: minutes for league in DELAY_ADJUST_LEAGUES
              if (minutes := steady_delay(feed.get("league_clock_checks", {}).get(league)))}
    if not delays:
        return feed, {}
    feed = json.loads(json.dumps(feed))
    for league, minutes in delays.items():
        rows = feed["leagues"].get(league)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if row.get("status") in ("upcoming", "live"):
                listed = datetime.fromisoformat(row["kickoff_utc"].replace("Z", "+00:00"))
                row["listed_kickoff_utc"] = row["kickoff_utc"]
                row["kickoff_utc"] = (listed + timedelta(minutes=minutes)).isoformat()
        feed["league_clock_checks"][league] = dict(feed["league_clock_checks"][league], status="adjusted")
    return feed, delays


def previous_game(rows, pick, duration):
    """The players' latest unfinished game before this kickoff, with the share already played."""
    players = {tag(pick["home_tag"]), tag(pick["away_tag"])}
    kickoff = parse_time(pick["kickoff_utc"])
    earliest = kickoff - timedelta(minutes=2 * duration + 15)
    pending = []
    for row in rows:
        try:
            start = parse_time(row["kickoff_utc"])
        except (KeyError, ValueError, TypeError):
            continue
        if (row.get("status") not in ("upcoming", "live") or not earliest <= start < kickoff
                or not players & {tag(row.get("home_tag", "")), tag(row.get("away_tag", ""))}):
            continue
        clock = str(row.get("match_clock") or "").strip().rstrip("'′")
        share = min(int(clock), duration) / duration if row["status"] == "live" and clock.isdigit() else 0.
        pending.append({"kickoff_utc": row["kickoff_utc"], "home_tag": row.get("home_tag"),
                        "away_tag": row.get("away_tag"), "status": row["status"],
                        "score": row.get("live_score"), "played_share": round(share, 2)})
    return min(pending, key=lambda game: game["played_share"]) if pending else None


def playable_options(goal_lines_table, floor=TOP_PICK_MIN_PROBABILITY):
    """Every over/under at the league's floor or above, with the lowest odds worth taking (its fair odds)."""
    options = []
    for line, chances in (goal_lines_table or {}).items():
        for side in ("over", "under"):
            if chances[side] >= floor and chances[f"fair_{side}_odds"]:
                options.append({"selection": f"{side} {line}", "probability": chances[side],
                                "min_odds": chances[f"fair_{side}_odds"]})
    return sorted(options, key=lambda option: option["probability"], reverse=True)


def waiting(row):
    game = row.get("previous_game")
    return bool(game) and game["played_share"] < PREVIOUS_GAME_MIN_SHARE


def build_picks(feed, now):
    """Goal calls at each league's usual line, with a weak favourite as context."""
    feed, delays = adjust_delays(feed)
    listed = {(key, row["home_tag"], row["away_tag"], row["kickoff_utc"]): row["listed_kickoff_utc"]
              for key in delays for row in feed["leagues"][key] if "listed_kickoff_utc" in row}
    forecasts = predict_feed(feed, now=now, use_live_scores=True)
    leagues = {}
    over_rates = {}
    for key, rows in feed["leagues"].items():
        if not isinstance(rows, list):
            leagues[key] = {"status": "error"}
            continue
        diagnostics = forecasts["diagnostics"][key]
        rows = unique_fixtures(rows)
        line, over_rate = league_goal_line(rows, now)
        totals = league_totals(rows, now)
        over_rates[key] = {str(goal_line): round(sum(total > goal_line for total in totals) / len(totals), 4)
                           for goal_line in GOAL_LINES} if totals else {}
        rates = outcome_rates(rows)
        picks = []
        for forecast in forecasts["leagues"][key]:
            # Replays favoured form probabilities pulled halfway to league rates over Forebet's.
            form = forecast["form_model_probabilities"]
            winner = {side: .5 * form[side] + .5 * rates[side] for side in rates}
            favourite = max(winner, key=winner.get)
            pick = {name: forecast[name] for name in ("home_tag", "away_tag", "home_club", "away_club", "kickoff_utc")}
            pick.update({"goals_line": line, "goals_call": None, "goals_probability": None,
                         "expected_goals": forecast["expected_goals"]["total"],
                         "goals_confidence": "trusted" if key in TRUSTED_GOAL_LEAGUES else "low",
                         "favourite": {"1": forecast["home_tag"], "2": forecast["away_tag"], "X": "draw"}[favourite],
                         "favourite_probability": round(winner[favourite], 4),
                         "favourite_strength": "weak", "call_status": "ok",
                         "warnings": forecast["warnings"],
                         "one_x_two": {side: round(p, 4) for side, p in winner.items()},
                         "double_chance": {"1X": round(winner["1"] + winner["X"], 4),
                                           "X2": round(winner["X"] + winner["2"], 4),
                                           "12": round(winner["1"] + winner["2"], 4)},
                         "winner_markets_trust": "not trusted: did not beat league rates in replays",
                         "safest_option": None, "goal_lines": None, "playable_options": []})
            pick["previous_game"] = previous_game(rows, pick, LEAGUE_MINUTES[key])
            if key in delays:
                pick["kickoff_adjusted_minutes"] = delays[key]
                pick["listed_kickoff_utc"] = listed.get((key, pick["home_tag"], pick["away_tag"], pick["kickoff_utc"]))
                pick["warnings"] = pick["warnings"] + [f"kickoff_adjusted_for_{delays[key]}_minute_delay"]
            if diagnostics["stale"]:
                pick.update({"call_status": "withheld: stale source", "favourite": None, "favourite_probability": None,
                             "one_x_two": None, "double_chance": None})
            elif line is None:
                pick["call_status"] = "withheld: no league results"
            else:
                over = forecast["totals"][str(line)]["over"]
                probability = max(over, 1 - over)
                history = min(forecast["evidence"][side]["history_matches"] for side in ("home", "away"))
                pick["goals_probability"] = round(probability, 4)
                pick["goal_lines"] = goal_lines(forecast["totals"])
                if history < MIN_PLAYER_GAMES:
                    pick["call_status"] = "no call: new players"
                elif probability < MIN_CALL_PROBABILITY:
                    pick["call_status"] = "no call: too close"
                else:
                    pick["goals_call"] = "over" if over >= .5 else "under"
                if history >= MIN_PLAYER_GAMES:
                    pick["safest_option"] = safest_option(pick, winner, rates, over, over_rate, key)
                    if key in TRUSTED_GOAL_LEAGUES:
                        pick["playable_options"] = playable_options(pick["goal_lines"], min_probability(key))
            picks.append(pick)
        leagues[key] = picks
    return {"generated_at": forecasts["generated_at"], "source_fetched_at": feed["fetched_at"],
            "stale": forecasts["stale"], "model_version": MODEL_VERSION,
            "validation_status": "experimental_uncalibrated",
            "goals_line_basis": "league mean of this snapshot's results, rounded down to x.5",
            "league_fetched_at": feed["league_fetched_at"], "leagues": leagues, "league_over_rates": over_rates}


ADVICE_ORDER = {"play": 0, "wait": 1, "skip": 2}


def board_row(row, league, lines):
    """A one-line summary of a game: its best option, chance, edge and advice."""
    option = row.get("best_available") if lines else row.get("safest_option")
    entry = {"league": league, "kickoff_utc": row["kickoff_utc"], "home_tag": row["home_tag"],
             "away_tag": row["away_tag"], "advice": row["advice"], "selection": None, "probability": None,
             "fair_odds": None, "edge": None}
    if option:
        entry.update(selection=option["selection"], probability=option["probability"],
                     fair_odds=option.get("fair_odds"), edge=round(pick_edge(option), 4))
    return entry


def pick_edge(row):
    """Model chance minus the league's rate for the same selection."""
    rate = row.get("league_rate")
    return row["probability"] - rate if rate is not None else 0.


def min_probability(league):
    return LEAGUE_MIN_PROBABILITY.get(league, TOP_PICK_MIN_PROBABILITY)


def play_advice(probability, proven, status="ok", floor=TOP_PICK_MIN_PROBABILITY):
    """'play' only for proven-edge picks at the league's floor (60%, GT12 65%); others say why to skip."""
    if status != "ok" and not status.startswith("no call: too close"):
        return f"skip ({status})"
    if probability is None:
        return "skip (no pick)"
    if probability < floor:
        return f"skip (below {floor:.0%})"
    return "play" if proven else "skip (no proven edge)"


def row_advice(row, league, lines):
    advice = base_advice(row, league, lines)
    option = row.get("best_available") if lines else row.get("safest_option")
    if advice == "play" and option and no_edge(option):
        return NO_EDGE
    return "wait (previous game pending)" if advice == "play" and waiting(row) else advice


NO_EDGE = "skip (no edge over league average)"


def no_edge(option):
    """A pick no likelier than the league's own rate for it has nothing for the model to add."""
    return option.get("league_rate") is not None and pick_edge(option) <= 0


def base_advice(row, league, lines):
    if lines:
        best = row.get("best_available")
        if row["call_status"] == "no call: new players" or row["call_status"].startswith("withheld"):
            return play_advice(None, False, row["call_status"])
        return play_advice(best and best["probability"], league in TRUSTED_GOAL_LEAGUES,
                           floor=min_probability(league))
    return play_advice(row["goals_probability"] if row["goals_call"] else None,
                       league in TRUSTED_GOAL_LEAGUES, row["call_status"], floor=min_probability(league))


def parse_lines(value):
    """?lines=4.5,5.5: the x.5 goal lines a bookmaker offers."""
    try:
        lines = sorted({float(part) for part in value.split(",")})
    except ValueError:
        lines = []
    if not lines or len(lines) > MAX_AVAILABLE_LINES or any(line not in GOAL_LINES for line in lines):
        raise ValueError("lines must be up to 8 comma-separated x.5 goal lines from 0.5 to 9.5, e.g. lines=4.5,5.5")
    return lines


def best_available(pick, lines, over_rates):
    """The likeliest over/under among the offered lines, with fair odds and the league rate."""
    if not pick.get("goal_lines") or pick["call_status"] == "no call: new players":
        return None
    options = []
    for line in lines:
        chances = pick["goal_lines"].get(str(line))
        if not chances:
            continue
        rate = over_rates.get(str(line))
        for side in ("over", "under"):
            options.append({"selection": f"{side} {line}", "probability": chances[side],
                            "fair_odds": chances[f"fair_{side}_odds"],
                            "league_rate": None if rate is None else round(rate if side == "over" else 1 - rate, 4)})
    return max(options, key=lambda option: option["probability"]) if options else None


def current_picks(now):
    """Compute picks once per feed refresh; requests only filter the result."""
    feed = snapshot(set(LEAGUES), now)
    key = json.dumps([feed["fetched_at"], feed["league_fetched_at"], feed["stale"]])
    with picks_lock:
        if picks_cache["key"] != key:
            picks_cache["result"] = build_picks(feed, now)
            picks_cache["key"] = key
        return json.loads(json.dumps(picks_cache["result"]))


@app.get("/picks")
def picks():
    try:
        selected, _, fields, limit, player = query_options(PICK_FIELDS, with_status=False, extra=("top", "lines"))
        lines = parse_lines(request.args["lines"]) if "lines" in request.args else None
        top_count = None
        if "top" in request.args:
            value = request.args["top"]
            if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 10:
                raise ValueError("top must be an integer between 1 and 10")
            top_count = int(value)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    now = datetime.now(timezone.utc)
    try:
        payload = current_picks(now)
    except Exception:
        logger.exception("Could not build picks")
        return jsonify({"error": "picks unavailable"}), 503
    payload["league_fetched_at"] = {key: value for key, value in payload["league_fetched_at"].items() if key in selected}
    leagues = {}
    candidates = []
    board = []
    for key in LEAGUES:
        if key not in selected:
            continue
        rows = payload["leagues"].get(key, {"status": "error"})
        if isinstance(rows, list):
            # Kickoffs that elapse between refreshes are no longer upcoming.
            rows = sorted((row for row in rows if is_future_fixture(row, now)), key=lambda row: row["kickoff_utc"])
            # The next kickoff, and anything else starting within the window.
            horizon = now + timedelta(minutes=PICK_WINDOW_MINUTES)
            rows = [row for row in rows if (row["kickoff_utc"] == rows[0]["kickoff_utc"]
                                            or not is_future_fixture(row, horizon))
                    and (player is None or matches_player(row, player))]
            if lines:
                rates = payload.get("league_over_rates", {}).get(key, {})
                for row in rows:
                    row["best_available"] = best_available(row, lines, rates)
                # Only the offered lines count, so top picks rank the best of those.
                candidates += [dict(row, league=key, market="over_under", proven_edge=key in TRUSTED_GOAL_LEAGUES,
                                    **row["best_available"]) for row in rows if row["best_available"]
                               and (top_count or row["best_available"]["probability"] >= min_probability(key))]
            else:
                candidates += [dict(row, league=key, **row["safest_option"]) for row in rows if row["safest_option"]
                               and (top_count or row["safest_option"]["probability"] >= min_probability(key))]
            for row in rows:
                row["advice"] = row_advice(row, key, lines)
                board.append(board_row(row, key, lines))
            if limit is not None:
                rows = rows[:limit]
            if fields is not None:
                rows = [{name: value for name, value in row.items() if name in fields} for row in rows]
        leagues[key] = rows
    payload["leagues"] = leagues
    # One row per game in the window across all leagues: play, then wait, then skip; biggest edge first.
    payload["board"] = sorted(board, key=lambda row: (ADVICE_ORDER.get(row["advice"].split()[0], 3),
                                                      -(row["edge"] or 0), row["kickoff_utc"]))
    # ?top=N ranks every eligible game; by default only options at 60%+ qualify.
    # Proven goals calls first, then the biggest edge over the league's own rate for
    # that selection: a 74% GT12 under 5.5 in a league where 70% stay under is a
    # smaller edge (and shorter price) than a 67% volta6 under 5.5 against 50%.
    top = sorted(candidates, key=lambda row: (row["proven_edge"], pick_edge(row), row["probability"]),
                 reverse=True)[:top_count or TOP_PICKS]
    for row in top:
        row["edge"] = round(pick_edge(row), 4)
    for row in top:
        row["advice"] = play_advice(row["probability"], row["proven_edge"], floor=min_probability(row["league"]))
        row["min_probability"] = min_probability(row["league"])
        if row["advice"] == "play" and no_edge(row):
            row["advice"] = NO_EDGE
        if row["advice"] == "play" and waiting(row):
            row["advice"] = "wait (previous game pending)"
    top_keys = {"league", "market", "selection", "probability", "league_rate", "proven_edge", "fair_odds", "advice",
                "playable_options", "previous_game", "min_probability", "edge"}
    top = [{name: value for name, value in row.items() if (name in fields if fields is not None
            else name not in ("safest_option", "one_x_two", "double_chance")) or name in top_keys} for row in top]
    payload["top_picks"] = top
    payload["top_picks_status"] = "ok" if top else "no top pick right now"
    payload.pop("league_over_rates", None)
    if lines:
        payload["lines_offered"] = lines
    if top_count:
        del payload["leagues"]
    return feed_response(payload)


def parse_selection(text):
    """'over4.5', 'over 4.5', 'o4.5' or 'u5.5' -> ('over'|'under', 4.5)."""
    text = text.strip().casefold().replace(" ", "")
    for prefix, side in (("over", "over"), ("under", "under"), ("o", "over"), ("u", "under")):
        if text.startswith(prefix):
            try:
                line = float(text[len(prefix):])
            except ValueError:
                break
            if line in GOAL_LINES:
                return side, line
            break
    raise ValueError(f"Invalid selection {text!r}; use e.g. over4.5 or under5.5 with an x.5 line from 0.5 to 9.5")


def parse_odds(value):
    """?odds=over4.5:1.85,under5.5:1.70 -> {('over', 4.5): 1.85, ...}."""
    odds = {}
    for part in value.split(","):
        selection, _, price = part.partition(":")
        try:
            price = float(price)
        except ValueError:
            raise ValueError("odds must look like over4.5:1.85,under5.5:1.70 (decimal odds)") from None
        if not 1 < price <= 1000:
            raise ValueError("decimal odds must be above 1")
        odds[parse_selection(selection)] = price
    if not odds or len(odds) > 2 * MAX_AVAILABLE_LINES:
        raise ValueError("give between 1 and 16 priced selections")
    return odds


@app.get("/picks/choose")
def choose():
    """For one game, choose among the selections the bookmaker actually offers."""
    try:
        unknown = set(request.args) - {"league", "home", "away", "lines", "odds"}
        if unknown:
            raise ValueError("Unknown query parameter: " + ", ".join(sorted(unknown)))
        for name in request.args:
            if len(request.args.getlist(name)) > 1:
                raise ValueError(f"Specify {name} only once")
        league = request.args.get("league", "").strip().lower()
        if league not in LEAGUES:
            raise ValueError("league is required: " + ", ".join(sorted(LEAGUES)))
        home, away = (" ".join(request.args.get(side, "").casefold().split()) for side in ("home", "away"))
        if not home or not away:
            raise ValueError("home and away player tags are required")
        odds = parse_odds(request.args["odds"]) if "odds" in request.args else {}
        lines = parse_lines(request.args["lines"]) if "lines" in request.args else []
        offered = sorted({(side, line) for line in lines for side in ("over", "under")} | set(odds))
        if not offered:
            raise ValueError("give lines=4.5,5.5 and/or odds=over4.5:1.85,under5.5:1.70")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    now = datetime.now(timezone.utc)
    try:
        payload = current_picks(now)
    except Exception:
        logger.exception("Could not build picks")
        return jsonify({"error": "picks unavailable"}), 503
    rows = payload["leagues"].get(league)
    games = [row for row in rows if is_future_fixture(row, now) and matches_player(row, home)
             and matches_player(row, away)] if isinstance(rows, list) else []
    if not games:
        return jsonify({"error": f"no upcoming {league} game between {home} and {away}"}), 404
    pick = min(games, key=lambda row: row["kickoff_utc"])
    rates = payload.get("league_over_rates", {}).get(league, {})
    options = []
    for side, line in offered:
        chances = (pick.get("goal_lines") or {}).get(str(line))
        if not chances:
            continue
        rate = rates.get(str(line))
        option = {"selection": f"{side} {line}", "probability": chances[side],
                  "fair_odds": chances[f"fair_{side}_odds"],
                  "league_rate": None if rate is None else round(rate if side == "over" else 1 - rate, 4)}
        if (side, line) in odds:
            option["odds"] = odds[(side, line)]
            option["value"] = round(chances[side] * odds[(side, line)] - 1, 4)
        options.append(option)
    # "too close" only concerns the league line; other offered lines can still be chosen.
    usable = pick["call_status"] != "no call: new players" and not pick["call_status"].startswith("withheld")
    best, advice = None, pick["call_status"]
    if usable and waiting(pick):
        usable, advice = False, "wait (previous game pending)"
    if usable and options:
        if odds:
            priced = [option for option in options if "value" in option]
            top = max(priced, key=lambda option: option["value"])
            if top["value"] > 0:
                best = top
                advice = (f"bet {top['selection']}: {top['probability']:.0%} chance at {top['odds']} "
                          f"(fair {top['fair_odds']}), expected return {top['value']:+.0%}")
            else:
                advice = "no bet: no offered price beats the model's fair odds"
        else:
            best = max(options, key=lambda option: option["probability"])
            advice = f"take {best['selection']} only at odds above {best['fair_odds']}"
            if best["probability"] < min_probability(league):
                advice = f"close call ({best['probability']:.0%}): skip unless {best['selection']} pays well above {best['fair_odds']}"
    elif usable:
        advice = "no goal lines available for this game"
    result = {name: pick.get(name) for name in ("home_tag", "away_tag", "kickoff_utc", "call_status", "expected_goals",
                                                "goals_line", "goals_call", "goals_probability", "warnings")}
    result.update({"league": league, "proven_edge": league in TRUSTED_GOAL_LEAGUES,
                   "min_probability": min_probability(league),
                   "options": sorted(options, key=lambda option: option.get("value", option["probability"]), reverse=True),
                   "best": best, "advice": advice, "source_fetched_at": payload["source_fetched_at"],
                   "generated_at": payload["generated_at"]})
    return feed_response(result)


def refresh_and_notify():
    refresh_pages()
    for hook in after_refresh:
        try:
            hook()
        except Exception:
            logger.exception("After-refresh hook failed")


def start_scheduler():
    scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
    scheduler.add_job(
        refresh_and_notify,
        "interval",
        seconds=READER_INTERVAL_SECONDS,
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
        id="forebet-reader",
    )
    # The proxifly list updates every 5 minutes; keep tested backups ready for when Shifter fails.
    if os.environ.get("PROXY_SOURCE", "auto") != "shifter":
        scheduler.add_job(
            refresh_free_proxy_pool,
            "interval",
            minutes=5,
            next_run_time=datetime.now(timezone.utc),
            max_instances=1,
            coalesce=True,
            id="free-proxy-pool",
        )
    scheduler.start()
    try:
        bot = telegram_bot.start(app.test_client(), scheduler)
    except Exception as exc:
        logger.exception("Telegram bot failed to start")
        telegram_bot.STATUS["last_error"] = f"start failed: {exc}"[:200]
        bot = None
    if bot:
        after_refresh.append(bot.check_alerts)
    return scheduler


if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), use_reloader=False)
