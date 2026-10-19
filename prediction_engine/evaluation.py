"""Retrospective diagnostic only; exact historical result availability is unknown."""

import math
from datetime import timedelta

from .model import MODEL_VERSION, SCORE_MODELS, LeagueModel, poisson_pmf
from .validation import LEAGUES, clean_rows, utc


def available_before(row, cutoff, duration):
    """Prefer an observed publication time; otherwise use the documented delay."""
    observed = row.get("result_seen_at")
    available = utc(observed) if observed else utc(row["kickoff_utc"]) + timedelta(minutes=duration+5)
    return utc(row["kickoff_utc"]) < cutoff and available < cutoff


def prematch_forebet(row):
    """Only a separately captured prematch observation is a fair comparator."""
    saved = row.get("prematch_forebet")
    if not isinstance(saved, dict):
        return None
    try:
        if utc(saved["observed_at"]) >= utc(row["kickoff_utc"]):
            return None
        values = [saved[key] for key in ("p1", "px", "p2")]
        if any(isinstance(p, bool) or not isinstance(p, (float, int)) or not math.isfinite(p)
               or not 0 <= p <= 100 for p in values) or abs(sum(values)-100) > 2:
            return None
        return dict(zip(("1", "X", "2"), (p/sum(values) for p in values)))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def backtest_feed(feed, *, limit=30):
    if not 1 <= limit <= 500:
        raise ValueError("backtest limit must be between 1 and 500 per league")
    output = {"model_version": MODEL_VERSION, "method": "chronological_form_model_only",
              "validation_status": "retrospective_diagnostic_only",
              "assumptions": [
                  "Historical result publication times are unavailable in a single feed snapshot.",
                  "A result is assumed available after league match duration plus five minutes.",
                  "Forebet probabilities on completed games may have changed; they are excluded from this test.",
                  "This tests the form model, not the live ensemble or its calibration."], "leagues": {}}
    for league, duration in LEAGUES.items():
        rows, errors = clean_rows(feed.get("leagues", {}).get(league))
        source_at = utc(feed.get("league_fetched_at", {}).get(league) or feed["fetched_at"])
        played = [row for row in rows if row["status"] == "played" and utc(row["kickoff_utc"]) < source_at]
        totals = {name: {"accuracy": 0., "log_loss": 0., "brier_score": 0.}
                  for name in ("form_model", "league_baseline")}
        n = 0
        goals_mae = score_hits = btts_brier = 0.
        baseline_goals_mae = baseline_score_hits = 0.
        goal_brier = {name: {line: 0. for line in ("2.5", "4.5", "6.5")}
                      for name in ("form_model", "league_baseline")}
        calibration = {name: {"matches": 0, "probability_sum": 0., "correct": 0}
                       for name in ("below_50", "50_to_60", "60_to_70", "70_to_80", "80_plus")}
        leans = lean_hits = 0
        forebet_n = 0
        forebet_metrics = {name: {"accuracy": 0., "log_loss": 0., "brier_score": 0.}
                           for name in ("form_model", "forebet")}
        for row in played[-limit:]:
            cutoff = utc(row["kickoff_utc"]) - timedelta(microseconds=1)
            training = [previous for previous in played if
                        cutoff - timedelta(days=30) <= utc(previous["kickoff_utc"])
                        and available_before(previous, cutoff, duration)]
            if len(training) < 30:
                continue
            model = LeagueModel(training, cutoff)
            target = dict(row, status="upcoming", actual_score=None, p1=100/3, px=100/3, p2=100/3)
            prediction = model.predict(target, use_forebet=False)
            home, away = map(int, row["actual_score"].split("-"))
            outcome = "1" if home > away else "2" if away > home else "X"
            baseline = {"1": 1, "X": 1, "2": 1}
            for previous in training:
                h, a = map(int, previous["actual_score"].split("-"))
                baseline["1" if h > a else "2" if a > h else "X"] += 1
            baseline = {key: value / (len(training)+3) for key, value in baseline.items()}
            for name, probabilities in (("form_model", prediction["probabilities"]),
                                         ("league_baseline", baseline)):
                totals[name]["accuracy"] += max(probabilities, key=probabilities.get) == outcome
                totals[name]["log_loss"] -= math.log(max(probabilities[outcome], 1e-12))
                totals[name]["brier_score"] += sum((p - (key == outcome)) ** 2 for key, p in probabilities.items())
            strongest = max(prediction["probabilities"], key=prediction["probabilities"].get)
            probability = prediction["probabilities"][strongest]
            bucket = ("below_50" if probability < .5 else "50_to_60" if probability < .6
                      else "60_to_70" if probability < .7 else "70_to_80" if probability < .8 else "80_plus")
            calibration[bucket]["matches"] += 1
            calibration[bucket]["probability_sum"] += probability
            calibration[bucket]["correct"] += strongest == outcome
            if prediction["pick"] is not None:
                leans += 1
                lean_hits += prediction["pick"] == outcome
            reference = prematch_forebet(row)
            if reference:
                forebet_n += 1
                for name, probabilities in (("form_model", prediction["probabilities"]), ("forebet", reference)):
                    forebet_metrics[name]["accuracy"] += max(probabilities, key=probabilities.get) == outcome
                    forebet_metrics[name]["log_loss"] -= math.log(max(probabilities[outcome], 1e-12))
                    forebet_metrics[name]["brier_score"] += sum((p-(key == outcome))**2 for key,p in probabilities.items())
            baseline_home = sum(int(p["actual_score"].split("-")[0]) for p in training)/len(training)
            baseline_away = sum(int(p["actual_score"].split("-")[1]) for p in training)/len(training)
            baseline_goals_mae += abs(baseline_home + baseline_away - home - away)
            baseline_score_hits += f"{int(baseline_home)}-{int(baseline_away)}" == row["actual_score"]
            goal_p = poisson_pmf(baseline_home + baseline_away)
            for line in goal_brier["form_model"]:
                actual_over = home+away > float(line)
                goal_brier["form_model"][line] += (prediction["totals"][line]["over"]-actual_over)**2
                baseline_over = sum(p for goals,p in enumerate(goal_p) if goals > float(line))
                goal_brier["league_baseline"][line] += (baseline_over-actual_over)**2
            goals_mae += abs(prediction["expected_goals"]["total"] - home - away)
            score_hits += prediction["most_likely_score"] == row["actual_score"]
            btts_brier += (prediction["btts"]["yes"] - (home > 0 and away > 0)) ** 2
            n += 1
        for metrics in totals.values():
            for key in metrics:
                metrics[key] = round(metrics[key] / n, 6) if n else None
        output["leagues"][league] = {"matches": n, **totals,
            "total_goals_mae": round(goals_mae/n, 6) if n else None,
            "correct_score_accuracy": round(score_hits/n, 6) if n else None,
            "btts_brier_score": round(btts_brier/n, 6) if n else None,
            "baseline_total_goals_mae": round(baseline_goals_mae/n, 6) if n else None,
            "baseline_correct_score_accuracy": round(baseline_score_hits/n, 6) if n else None,
            "over_goals_brier": {name: {line: round(value/n,6) if n else None for line,value in values.items()}
                                 for name,values in goal_brier.items()},
            "winner_calibration": {name: {"matches": c["matches"],
                "mean_predicted_probability": round(c["probability_sum"]/c["matches"],6) if c["matches"] else None,
                "observed_accuracy": round(c["correct"]/c["matches"],6) if c["matches"] else None}
                for name,c in calibration.items()},
            "experimental_leans": {"matches": leans, "coverage": round(leans/n,6) if n else None,
                                   "accuracy": round(lean_hits/leans,6) if leans else None},
            "forebet_comparison": {"matches": forebet_n,
                "status": "matched_prematch_observations" if forebet_n else "unavailable_no_prematch_observations",
                "metrics": {name: {k: round(v/forebet_n,6) if forebet_n else None for k,v in values.items()}
                            for name,values in forebet_metrics.items()}},
            "rejected_rows": len(errors)}
    return output


def compare_models(feed, *, limit=60, common_opponents=False):
    """Compare fixed candidates on identical targets, without picking a winner.

    This reuses a single retrospective snapshot. It is not an untouched test
    set and must not be presented as evidence of prospective improvement.
    """
    if not 1 <= limit <= 500:
        raise ValueError("comparison limit must be between 1 and 500")
    candidates = ({"baseline": {"use_common_opponents": False},
                   "common_opponents": {"use_common_opponents": True}} if common_opponents
                  else {name: {"score_model": name} for name in SCORE_MODELS})
    output = {"method": "matched_chronological_common_opponent_comparison" if common_opponents else "matched_chronological_form_only_comparison",
              "validation_status": "retrospective_candidate_comparison",
              "note": "Same snapshot reused; no independent test set. Result availability assumed after match duration plus five minutes. Forebet probabilities excluded.",
              "leagues": {}}
    for league, duration in LEAGUES.items():
        rows, errors = clean_rows(feed.get("leagues", {}).get(league))
        source_at = utc(feed.get("league_fetched_at", {}).get(league) or feed["fetched_at"])
        played = [row for row in rows if row["status"] == "played" and utc(row["kickoff_utc"]) < source_at]
        metrics = {name: {"winner_accuracy": 0., "winner_log_loss": 0.,
            "score_log_loss": 0., "exact_score_accuracy": 0., "total_goals_mae": 0.,
            "btts_brier": 0., "over_2_5_brier": 0., "over_4_5_brier": 0., "over_6_5_brier": 0.}
                   for name in candidates}
        count = 0
        shared_count = 0
        for row in played[-limit:]:
            cutoff = utc(row["kickoff_utc"]) - timedelta(microseconds=1)
            training = [p for p in played if cutoff - timedelta(days=30) <= utc(p["kickoff_utc"])
                        and available_before(p, cutoff, duration)]
            if len(training) < 30:
                continue
            model = LeagueModel(training, cutoff)
            target = dict(row, status="upcoming", actual_score=None, p1=100/3, px=100/3, p2=100/3)
            home, away = map(int, row["actual_score"].split("-"))
            outcome = "1" if home > away else "2" if away > home else "X"
            for name, options in candidates.items():
                p = model.predict(target, use_forebet=False, include_score_distribution=True, **options)
                m = metrics[name]
                m["winner_accuracy"] += p["most_likely_outcome"] == outcome
                m["winner_log_loss"] -= math.log(max(p["probabilities"][outcome], 1e-12))
                m["score_log_loss"] -= math.log(max(p["score_distribution"].get(row["actual_score"], 0.), 1e-12))
                m["exact_score_accuracy"] += p["most_likely_score"] == row["actual_score"]
                m["total_goals_mae"] += abs(p["expected_goals"]["total"] - home - away)
                m["btts_brier"] += (p["btts"]["yes"] - (home > 0 and away > 0)) ** 2
                for line in (2.5, 4.5, 6.5):
                    m[f"over_{str(line).replace('.', '_')}_brier"] += (p["totals"][str(line)]["over"] - (home+away > line)) ** 2
            shared_count += p["evidence"]["common_opponents"]["shared_opponent_count"] > 0
            count += 1
        output["leagues"][league] = {"matches": count, "rejected_rows": len(errors),
            "matches_with_shared_opponents": shared_count,
            "models": {name: {key: round(value/count, 6) if count else None for key, value in m.items()}
                       for name, m in metrics.items()}}
    return output
