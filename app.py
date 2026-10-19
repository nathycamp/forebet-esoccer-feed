"""Railway HTTP service for Forebet esoccer predictions."""

import logging
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify

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


@app.get("/predictions")
def predictions():
    with cache_lock:
        leagues = {}
        league_fetched_at = {}
        league_changed_at = {}
        league_clock_checks = {}
        now = datetime.now(timezone.utc)
        too_old = False
        for key in LEAGUES:
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
        return jsonify({
            "fetched_at": cache["fetched_at"],
            "source": "railway-shifter-reader",
            "stale": cache["stale"] or too_old,
            "league_fetched_at": league_fetched_at,
            "league_changed_at": league_changed_at,
            "league_clock_checks": league_clock_checks,
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
