import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from prediction_engine import predict_feed
from prediction_engine.model import LeagueModel, SCORE_MODELS, goal_pmf, shared_pace_matrix
from prediction_engine.validation import clean_rows
from prediction_engine.common_opponents import compare_common_opponents


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def fixture(index=0, **updates):
    value = {"home_tag": "Alice", "away_tag": "Bob", "home_club": "Arsenal",
             "away_club": "Chelsea", "kickoff_utc": (NOW - timedelta(minutes=20*(index+1))).isoformat(),
             "status": "played", "actual_score": "4-1", "p1": 45, "px": 25, "p2": 30}
    value.update(updates)
    return value


def snapshot(rows):
    return {"fetched_at": NOW.isoformat(), "stale": False,
            "leagues": {key: copy.deepcopy(rows) for key in ("esb8", "volta6", "h2hgg", "gt12")}}


class PredictionTests(unittest.TestCase):
    def setUp(self):
        self.played = [fixture(index) for index in range(30)]
        self.future = fixture(kickoff_utc=(NOW + timedelta(minutes=15)).isoformat(),
                              status="upcoming", actual_score=None)

    def test_normalized_coherent_outputs_and_player_form(self):
        result = predict_feed(snapshot(self.played + [self.future]), now=NOW)
        self.assertFalse(result["stale"])
        row = result["leagues"]["esb8"][0]
        self.assertAlmostEqual(sum(row["probabilities"].values()), 1, places=5)
        self.assertGreater(row["probabilities"]["1"], row["probabilities"]["2"])
        self.assertGreater(row["expected_goals"]["home"], row["expected_goals"]["away"])
        self.assertEqual(row["evidence"]["home"]["windows"]["5"]["wins"], 5)
        self.assertEqual(row["evidence"]["home"]["windows"]["20"]["matches"], 20)
        overs = [item["over"] for item in row["totals"].values()]
        self.assertEqual(overs, sorted(overs, reverse=True))
        for values in row["totals"].values():
            self.assertAlmostEqual(sum(values.values()), 1)
        self.assertAlmostEqual(sum(row["btts"].values()), 1)
        self.assertLessEqual(sum(score["probability"] for score in row["top_scores"]), 1)
        json.dumps(result, allow_nan=False)

    def test_pending_results_never_become_training_or_upcoming_forecasts(self):
        pending = fixture(status="upcoming", actual_score=None)
        data = predict_feed(snapshot([pending, self.future]), now=NOW)
        self.assertEqual(len(data["leagues"]["esb8"]), 1)
        self.assertEqual(data["diagnostics"]["esb8"]["completed_games_used"], 0)
        self.assertEqual(data["diagnostics"]["esb8"]["past_kickoff_without_result"], 1)

    def test_prediction_needs_no_disk_or_network_access(self):
        feed = snapshot(self.played + [self.future])
        with patch("builtins.open", side_effect=AssertionError("disk access")), \
                patch("socket.socket", side_effect=AssertionError("network access")):
            result = predict_feed(feed, now=NOW)
        self.assertEqual(len(result["leagues"]["esb8"]), 1)

    def test_no_future_or_unavailable_result_leakage(self):
        future_result = fixture(kickoff_utc=(NOW + timedelta(minutes=3)).isoformat(), actual_score="0-40")
        late_result = fixture(result_seen_at=(NOW + timedelta(minutes=1)).isoformat(), actual_score="0-40")
        baseline = LeagueModel(self.played, NOW).predict(self.future)
        augmented = LeagueModel(self.played + [future_result, late_result], NOW).predict(self.future)
        self.assertEqual(baseline, augmented)

    def test_null_result_publication_time_means_unknown(self):
        rows = [dict(row, result_seen_at=None) for row in self.played]
        result = predict_feed(snapshot(rows + [self.future]), now=NOW)
        self.assertEqual(result["diagnostics"]["esb8"]["completed_games_used"], 30)

    def test_leagues_are_isolated_and_inputs_not_modified(self):
        feed = snapshot(self.played + [self.future])
        original = copy.deepcopy(feed)
        expected = predict_feed(feed, now=NOW)["leagues"]["esb8"]
        self.assertEqual(feed, original)
        feed["leagues"]["gt12"] = [fixture(index, actual_score="0-9") for index in range(30)]
        self.assertEqual(predict_feed(feed, now=NOW)["leagues"]["esb8"], expected)

    def test_duplicate_and_conflicting_results(self):
        rows, errors = clean_rows([self.played[0], self.played[0]])
        self.assertEqual(len(rows), 1)
        self.assertFalse(errors)
        rows, errors = clean_rows([self.played[0], dict(self.played[0], actual_score="0-1")])
        self.assertFalse(rows)
        self.assertTrue(errors)

    def test_stale_source_and_malformed_rows_are_visible(self):
        feed = snapshot(self.played + [self.future, {"broken": True}])
        feed["fetched_at"] = (NOW - timedelta(minutes=7)).isoformat()
        result = predict_feed(feed, now=NOW)
        self.assertTrue(result["stale"])
        self.assertEqual(result["diagnostics"]["esb8"]["rejected_rows"], 1)
        self.assertIn("source_feed_stale", result["leagues"]["esb8"][0]["warnings"])

    def test_new_player_falls_back_to_forebet_with_warning(self):
        new = dict(self.future, home_tag="New player")
        row = predict_feed(snapshot(self.played + [new]), now=NOW)["leagues"]["esb8"][0]
        self.assertEqual(row["probabilities"], {"1": .45, "X": .25, "2": .3})
        self.assertIn("limited_player_history", row["warnings"])
        self.assertEqual(row["ensemble_weights"]["form_model"], 0)

    def test_timezone_offsets_are_respected(self):
        row = dict(self.future, kickoff_utc="2026-10-19T04:15:00-08:00")
        result = predict_feed(snapshot(self.played + [row]), now=NOW)
        self.assertEqual(result["leagues"]["esb8"][0]["kickoff_utc"], "2026-10-19T12:15:00+00:00")

    def test_goal_distribution_extremes(self):
        for mean in (.1, 3, 15):
            for dispersion in (.001, .4):
                pmf = goal_pmf(mean, dispersion)
                self.assertAlmostEqual(sum(pmf), 1, places=9)
                self.assertTrue(all(0 <= value <= 1 for value in pmf))
                self.assertAlmostEqual(sum(k*p for k, p in enumerate(pmf)), mean, places=3)

    def test_shared_pace_joint_distribution_has_expected_covariance(self):
        matrix = shared_pace_matrix(2., 3., .2)
        self.assertAlmostEqual(sum(p for h, a, p in matrix), 1, places=8)
        self.assertAlmostEqual(sum(h*p for h, a, p in matrix), 2, places=6)
        self.assertAlmostEqual(sum(a*p for h, a, p in matrix), 3, places=6)
        covariance = sum((h-2)*(a-3)*p for h, a, p in matrix)
        self.assertAlmostEqual(covariance, 1.2, places=6)

    def test_all_candidates_preserve_score_and_outcome_probability_agreement(self):
        model = LeagueModel(self.played, NOW)
        model.shared_dispersion = .1
        for variant in SCORE_MODELS:
            row = model.predict(self.future, score_model=variant, include_score_distribution=True)
            outcomes = {"1": 0., "X": 0., "2": 0.}
            for score, probability in row["score_distribution"].items():
                h, a = map(int, score.split("-"))
                outcomes["1" if h>a else "2" if a>h else "X"] += probability
            self.assertAlmostEqual(sum(outcomes.values()), 1, places=6)
            for outcome, p in outcomes.items():
                self.assertAlmostEqual(p, row["probabilities"][outcome], places=5)

    def test_stronger_opponent_defense_reduces_scoring_expectation(self):
        model = LeagueModel(self.played, NOW)
        original = model.predict(self.future)["evidence"]["matchup"]["home_goals_before_outcome_blend"]
        # A lower concession factor means a stronger defense.
        model.defense["bob"] *= .5
        after = model.predict(self.future)
        self.assertLess(after["evidence"]["matchup"]["home_goals_before_outcome_blend"], original)
        self.assertAlmostEqual(after["evidence"]["away"]["defensive_strength"], 1/model.defense["bob"], places=3)

    def test_comparison_ignores_post_match_forebet_values(self):
        from prediction_engine.evaluation import compare_models
        feed = snapshot([fixture(index) for index in range(45)])
        before = compare_models(feed, limit=2)
        for rows in feed["leagues"].values():
            for row in rows:
                row.update(p1=100, px=0, p2=0)
        self.assertEqual(before, compare_models(feed, limit=2))
        self.assertEqual(before["leagues"]["esb8"]["matches"], 2)

    def test_common_opponents_respect_home_away_orientation_and_exclude_direct_games(self):
        games = [fixture(0, home_tag="Alice", away_tag="Carl", actual_score="4-1"),
                 fixture(1, home_tag="Carl", away_tag="Bob", actual_score="2-1"),
                 fixture(2, home_tag="Dora", away_tag="Alice", actual_score="1-3"),
                 fixture(3, home_tag="Bob", away_tag="Dora", actual_score="0-0"),
                 fixture(4, home_tag="Alice", away_tag="Bob", actual_score="4-0")]
        model = LeagueModel(games, NOW)
        result = compare_common_opponents(model, "alice", "bob")
        self.assertEqual(result["shared_opponent_count"], 2)
        carl = next(row for row in result["comparisons"] if row["opponent_tag"] == "Carl")
        self.assertEqual(carl["home_player"]["weighted_goals_for"], 4)
        self.assertEqual(carl["away_player"]["weighted_goals_for"], 1)
        self.assertEqual(carl["away_player"]["weighted_goals_against"], 2)
        self.assertEqual(carl["away_player"]["losses"], 1)
        reversed_result = compare_common_opponents(model, "bob", "alice")
        self.assertAlmostEqual(result["home_goal_factor"], reversed_result["away_goal_factor"])
        self.assertAlmostEqual(result["away_goal_factor"], reversed_result["home_goal_factor"])
        base = model.predict(self.future)
        adjusted = model.predict(self.future, use_common_opponents=True)
        self.assertFalse(base["common_opponent_adjustment"])
        self.assertTrue(adjusted["common_opponent_adjustment"])
        self.assertNotEqual(base["probabilities"], adjusted["probabilities"])
        self.assertAlmostEqual(sum(adjusted["probabilities"].values()), 1, places=5)
        for name in ("home_goal_factor", "away_goal_factor"):
            self.assertGreaterEqual(result[name], 1/1.1)
            self.assertLessEqual(result[name], 1.1)

    def test_no_common_opponents_means_no_extra_adjustment(self):
        model = LeagueModel(self.played, NOW)
        result = model.predict(self.future)
        self.assertEqual(result["evidence"]["common_opponents"]["shared_opponent_count"], 0)
        self.assertEqual(result, model.predict(self.future, use_common_opponents=True))

    def test_common_opponents_use_only_recent_available_results(self):
        games = [fixture(index, away_tag="Carl" if index == 25 else "Dora") for index in range(30)]
        games.append(fixture(31, home_tag="Bob", away_tag="Carl"))
        games.append(fixture(32, home_tag="Bob", away_tag="Dora", result_seen_at=(NOW+timedelta(minutes=1)).isoformat()))
        model = LeagueModel(games, NOW)
        common = compare_common_opponents(model, "alice", "bob")
        self.assertEqual(common["shared_opponent_count"], 0)

    def test_backtest_cannot_use_forebet_post_match_probabilities(self):
        from prediction_engine.evaluation import backtest_feed
        feed = snapshot([fixture(index) for index in range(45)])
        first = backtest_feed(feed, limit=2)
        self.assertEqual(first["leagues"]["esb8"]["matches"], 2)
        for rows in feed["leagues"].values():
            for row in rows:
                row.update(p1=0, px=0, p2=100)
        self.assertEqual(first, backtest_feed(feed, limit=2))

    def test_live_games_are_recognized_but_never_forecast_or_trained_on(self):
        live = fixture(status="live", actual_score=None, live_score="2-0", match_clock="2",
                       kickoff_utc=(NOW+timedelta(minutes=1)).isoformat())
        rows, errors = clean_rows([live])
        self.assertFalse(errors)
        self.assertEqual(rows[0]["status"], "live")
        output = predict_feed(snapshot(self.played+[live, self.future]), now=NOW)
        self.assertEqual(output["live_prediction_status"], "disabled_until_validated")
        self.assertEqual(output["diagnostics"]["gt12"]["live_games_excluded"], 1)
        self.assertEqual(output["diagnostics"]["gt12"]["completed_games_used"], 30)
        self.assertEqual(len(output["leagues"]["gt12"]), 1)
        with self.assertRaises(ValueError):
            LeagueModel(self.played, NOW).predict(live)

    def test_weak_winner_and_stale_sources_have_no_pick(self):
        weak = fixture(status="upcoming", actual_score=None, home_tag="New A", away_tag="New B",
                       kickoff_utc=(NOW+timedelta(minutes=10)).isoformat(), p1=41, px=25, p2=34)
        prediction = predict_feed(snapshot([weak]), now=NOW)["leagues"]["gt12"][0]
        self.assertEqual(prediction["winner_assessment"], "no clear winner")
        self.assertIsNone(prediction["pick"])
        self.assertEqual(prediction["most_likely_outcome"], "1")
        self.assertEqual(prediction["calibration_status"], "unvalidated")
        old = snapshot(self.played+[self.future])
        old["stale"] = True
        prediction = predict_feed(old, now=NOW)["leagues"]["gt12"][0]
        self.assertIsNone(prediction["pick"])
        self.assertEqual(prediction["winner_assessment"], "unavailable: stale source")

    def test_backtest_reports_baselines_and_does_not_fake_forebet_comparison(self):
        from prediction_engine.evaluation import backtest_feed
        feed = snapshot([fixture(index) for index in range(45)])
        result = backtest_feed(feed, limit=3)["leagues"]["gt12"]
        self.assertEqual(result["forebet_comparison"]["matches"], 0)

        self.assertEqual(result["forebet_comparison"]["status"], "unavailable_no_prematch_observations")
        self.assertIsNotNone(result["baseline_total_goals_mae"])
        self.assertEqual(sum(v["matches"] for v in result["winner_calibration"].values()), 3)
        for rows in feed["leagues"].values():
            for row in rows:
                row["prematch_forebet"] = {"observed_at": (datetime.fromisoformat(row["kickoff_utc"])-timedelta(minutes=2)).isoformat(),
                                           "p1": 50, "px": 25, "p2": 25}
        result = backtest_feed(feed, limit=3)["leagues"]["gt12"]
        self.assertEqual(result["forebet_comparison"]["matches"], 3)
        for rows in feed["leagues"].values():
            for row in rows:
                row["prematch_forebet"]["observed_at"] = row["kickoff_utc"]
        result = backtest_feed(feed, limit=3)["leagues"]["gt12"]
        self.assertEqual(result["forebet_comparison"]["matches"], 0)

    def test_training_availability_prefers_observed_publication_time(self):
        from prediction_engine.evaluation import available_before
        early = fixture(kickoff_utc=(NOW-timedelta(minutes=30)).isoformat(),
                        result_seen_at=(NOW+timedelta(minutes=1)).isoformat())
        self.assertFalse(available_before(early, NOW, 12))
        early["result_seen_at"] = (NOW-timedelta(minutes=1)).isoformat()
        self.assertTrue(available_before(early, NOW, 12))

    def test_clock_mismatch_disables_a_winner_lean(self):
        feed = snapshot(self.played+[self.future])
        feed["league_clock_checks"] = {"gt12": {"status": "mismatch"}}
        result = predict_feed(feed, now=NOW)
        self.assertIsNone(result["leagues"]["gt12"][0]["pick"])
        self.assertTrue(result["diagnostics"]["gt12"]["stale"])

    def test_live_score_is_partial_goal_evidence_only(self):
        live = fixture(kickoff_utc=(NOW-timedelta(minutes=4)).isoformat(), status="live",
                       actual_score=None, live_score="6-5", match_clock="4")
        quiet = predict_feed(snapshot(self.played+[self.future, live]), now=NOW)
        busy = predict_feed(snapshot(self.played+[self.future, live]), now=NOW, use_live_scores=True)
        before, after = quiet["leagues"]["esb8"][0], busy["leagues"]["esb8"][0]
        self.assertGreater(after["expected_goals"]["total"], before["expected_goals"]["total"])
        self.assertEqual(after["evidence"]["home"]["elo"], before["evidence"]["home"]["elo"])
        self.assertEqual(after["evidence"]["home"]["windows"], before["evidence"]["home"]["windows"])
        self.assertIn("live_score_evidence_used", after["warnings"])
        self.assertEqual(busy["diagnostics"]["esb8"]["live_games_used"], 1)
        self.assertEqual(quiet["diagnostics"]["esb8"]["live_games_excluded"], 1)

    def test_one_league_clock_mismatch_leaves_other_leagues_fresh(self):
        feed = snapshot(self.played+[self.future])
        feed["stale"] = True
        feed["league_fetched_at"] = {key: NOW.isoformat() for key in feed["leagues"]}
        feed["league_clock_checks"] = {"gt12": {"status": "mismatch"}}
        result = predict_feed(feed, now=NOW)
        self.assertTrue(result["stale"])
        self.assertTrue(result["diagnostics"]["gt12"]["stale"])
        self.assertFalse(result["diagnostics"]["esb8"]["stale"])
        self.assertNotIn("source_feed_stale", result["leagues"]["esb8"][0]["warnings"])


if __name__ == "__main__":
    unittest.main()
