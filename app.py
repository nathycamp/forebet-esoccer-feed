"""Railway HTTP service for Forebet esoccer predictions."""

import logging
import os
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify

from reader import LEAGUES, read_pages


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


def now_utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def refresh_pages():
    attempted_at = now_utc()
    try:
        results = read_pages()
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
    except Exception:
        logger.exception("Reader cycle failed")
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


@app.get("/predictions")
def predictions():
    with cache_lock:
        leagues = {}
        league_fetched_at = {}
        league_changed_at = {}
        now = datetime.now(timezone.utc)
        too_old = False
        for key in LEAGUES:
            saved = cache["leagues"].get(key)
            leagues[key] = saved["fixtures"] if saved else {"status": "error"}
            fetched_at = saved.get("fetched_at") if saved else None
            league_fetched_at[key] = fetched_at
            league_changed_at[key] = saved.get("changed_at") if saved else None
            if not fetched_at or (now - datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))).total_seconds() > MAX_CACHE_AGE_SECONDS:
                too_old = True
        return jsonify({
            "fetched_at": cache["fetched_at"],
            "source": "railway-shifter-reader",
            "stale": cache["stale"] or too_old,
            "league_fetched_at": league_fetched_at,
            "league_changed_at": league_changed_at,
            "leagues": leagues,
        })


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
