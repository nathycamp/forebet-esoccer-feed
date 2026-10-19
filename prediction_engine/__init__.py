"""Stateless predictions from the JSON a downstream consumer already has."""

from datetime import datetime, timedelta, timezone

from .model import MODEL_VERSION, LeagueModel
from .validation import LEAGUES, clean_rows, utc


def predict_feed(feed, *, now=None, use_common_opponents=False):
    """Return winner, total-goal and score probabilities; no I/O or retained state.

    ``now`` is injectable for reproducible snapshot analysis. Production callers
    should omit it so elapsed kickoffs and old snapshots are rejected correctly.
    """
    if not isinstance(feed, dict) or not isinstance(feed.get("leagues"), dict):
        raise ValueError("feed must contain a leagues object")
    now = now or datetime.now(timezone.utc)
    now = utc(now.isoformat())
    output = {"generated_at": now.isoformat(), "source_fetched_at": feed.get("fetched_at"),
              "model_version": MODEL_VERSION, "validation_status": "experimental_uncalibrated",
              "probability_scale": "0_to_1", "stale": bool(feed.get("stale", True)),
              "live_prediction_status": "disabled_until_validated",
              "leagues": {}, "diagnostics": {}}
    leagues = feed.get("leagues", {})
    for league in LEAGUES:
        source_at = feed.get("league_fetched_at", {}).get(league) or feed.get("fetched_at")
        try:
            source_age = (now - utc(source_at)).total_seconds()
            # Per-league freshness and clock checks replace the feed-wide flag,
            # so one league's problem does not withhold every league's picks.
            has_league_time = bool(feed.get("league_fetched_at", {}).get(league))
            stale = ((not has_league_time and bool(feed.get("stale", True)))
                     or source_age > 360 or source_age < -60)
        except (TypeError, ValueError, AttributeError):
            source_age, stale = None, True
        clock_check = feed.get("league_clock_checks", {}).get(league) or {}
        stale |= clock_check.get("status") == "mismatch"
        rows, errors = clean_rows(leagues.get(league))
        completed = [row for row in rows if row["status"] == "played"
                     and now - timedelta(days=30) <= utc(row["kickoff_utc"]) < now]
        future_results = sum(row["status"] == "played" and utc(row["kickoff_utc"]) >= now for row in rows)
        pending = [row for row in rows if row["status"] == "upcoming" and utc(row["kickoff_utc"]) <= now]
        upcoming = [row for row in rows if row["status"] == "upcoming" and utc(row["kickoff_utc"]) > now]
        model = LeagueModel(completed, now)
        forecasts = []
        for row in upcoming:
            forecast = model.predict(row, use_common_opponents=use_common_opponents)
            if stale:
                forecast["warnings"].append("source_feed_stale")
                forecast["pick"] = None
                forecast["winner_assessment"] = "unavailable: stale source"
            if errors:
                forecast["warnings"].append("source_rows_rejected")
            if not completed:
                forecast["warnings"].append("no_league_results_goals_use_generic_prior")
            if future_results:
                forecast["warnings"].append("future_dated_results_excluded")
            forecasts.append(forecast)
        output["leagues"][league] = forecasts
        output["diagnostics"][league] = {"completed_games_used": len(completed),
            "players_with_results": sum(bool(games) for games in model.games.values()), "future_fixtures": len(forecasts),
            "past_kickoff_without_result": len(pending), "stale": stale,
            "future_dated_results_excluded": future_results,
            "source_age_seconds": round(source_age, 1) if source_age is not None else None,
            "rejected_rows": len(errors), "errors": errors[:10]}
        output["diagnostics"][league]["live_games_excluded"] = sum(row["status"] == "live" for row in rows)
        output["stale"] |= stale
    return output
