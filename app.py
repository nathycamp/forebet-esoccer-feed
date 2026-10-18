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


def now_utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def refresh_pages():
    attempted_at = now_utc()
    try:
        results = read_pages()
        successful = {key: page for key, page in results.items() if page.get("status") == "ok"}
        complete = len(successful) == len(LEAGUES)
        with cache_lock:
            cache["leagues"].update(successful)
            cache["last_fetch"] = attempted_at
            if successful:
                cache["fetched_at"] = attempted_at
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
        for key in LEAGUES:
            saved = cache["leagues"].get(key)
            leagues[key] = saved["fixtures"] if saved else {"status": "error"}
        return jsonify({
            "fetched_at": cache["fetched_at"],
            "source": "railway-shifter-reader",
            "stale": cache["stale"],
            "leagues": leagues,
        })


def start_scheduler():
    scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
    scheduler.add_job(
        refresh_pages,
        "interval",
        minutes=5,
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
