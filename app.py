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

from prediction_engine import MODEL_VERSION, predict_feed
from reader import LEAGUES


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
READER_TIMEOUT_SECONDS = 100
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
}
# Replays of the feed's own results showed a goals edge at the league line only here.
TRUSTED_GOAL_LEAGUES = {"esb8", "volta6"}
# Near-even calls and players new to the feed carry no real model opinion.
MIN_CALL_PROBABILITY = .55
MIN_PLAYER_GAMES = 5
# Form changes between sessions, so only imminent games get picks.
PICK_WINDOW_MINUTES = 20
picks_lock = threading.Lock()
picks_cache = {"key": None, "result": None}


def query_options(fields_allowed=FIXTURE_FIELDS, with_status=True):
    allowed = {"league", "player", "limit", "fields"} | ({"status"} if with_status else set())
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
    for name in ("limit", "player"):
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


def refresh_pages():
    global proxy_session_id
    attempted_at = now_utc()
    try:
        results = run_reader()
        completed_at = now_utc()
        successful = {key: page for key, page in results.items() if page.get("status") == "ok"}
        complete = len(successful) == len(LEAGUES)
        with cache_lock:
            for key, page in successful.items():
                previous = cache["leagues"].get(key)
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
            cache["last_status"] = "ok" if complete else "error"
            cache["stale"] = not complete
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


def league_goal_line(rows, now):
    """The league's usual x.5 line from its own recent results in this snapshot."""
    totals = []
    for row in rows:
        try:
            if row.get("status") == "played" and now - datetime.fromisoformat(row["kickoff_utc"].replace("Z", "+00:00")).astimezone(timezone.utc) <= timedelta(days=30):
                totals.append(sum(int(goals) for goals in row["actual_score"].split("-")))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return min(8.5, max(1.5, math.floor(sum(totals) / len(totals)) + .5)) if totals else None


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


def build_picks(feed, now):
    """Goal calls at each league's usual line, with a weak favourite as context."""
    forecasts = predict_feed(feed, now=now, use_live_scores=True)
    leagues = {}
    for key, rows in feed["leagues"].items():
        if not isinstance(rows, list):
            leagues[key] = {"status": "error"}
            continue
        diagnostics = forecasts["diagnostics"][key]
        line = league_goal_line(rows, now)
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
                         "warnings": forecast["warnings"]})
            if diagnostics["stale"]:
                pick.update({"call_status": "withheld: stale source", "favourite": None, "favourite_probability": None})
            elif line is None:
                pick["call_status"] = "withheld: no league results"
            else:
                over = forecast["totals"][str(line)]["over"]
                probability = max(over, 1 - over)
                history = min(forecast["evidence"][side]["history_matches"] for side in ("home", "away"))
                pick["goals_probability"] = round(probability, 4)
                if history < MIN_PLAYER_GAMES:
                    pick["call_status"] = "no call: new players"
                elif probability < MIN_CALL_PROBABILITY:
                    pick["call_status"] = "no call: too close"
                else:
                    pick["goals_call"] = "over" if over >= .5 else "under"
            picks.append(pick)
        leagues[key] = picks
    return {"generated_at": forecasts["generated_at"], "source_fetched_at": feed["fetched_at"],
            "stale": forecasts["stale"], "model_version": MODEL_VERSION,
            "validation_status": "experimental_uncalibrated",
            "goals_line_basis": "league mean of this snapshot's results, rounded down to x.5",
            "league_fetched_at": feed["league_fetched_at"], "leagues": leagues}


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
        selected, _, fields, limit, player = query_options(PICK_FIELDS, with_status=False)
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
            if limit is not None:
                rows = rows[:limit]
            if fields is not None:
                rows = [{name: value for name, value in row.items() if name in fields} for row in rows]
        leagues[key] = rows
    payload["leagues"] = leagues
    return feed_response(payload)


def start_scheduler():
    scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
    scheduler.add_job(
        refresh_pages,
        "interval",
        minutes=2,
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
        id="forebet-reader",
    )
    scheduler.start()
    return scheduler


if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), use_reloader=False)
