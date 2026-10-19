"""Experimental, league-specific goal model and probability ensemble.

Only observed final results enter training. Parameters are fixed initial choices,
not claimed to be tuned or calibrated. No neural network or external API is used.
"""

import math
from collections import defaultdict
from datetime import timedelta

from .validation import utc
from .common_opponents import compare_common_opponents


MODEL_VERSION = "recent-form-v2"
SCORE_MODELS = ("independent_nb", "independent_poisson", "shared_pace_nb")


def tag(value):
    return " ".join(value.casefold().split())


def clamp(value, low, high):
    return max(low, min(high, value))


def normalize(values):
    total = sum(values.values())
    return {key: value / total for key, value in values.items()}


def goal_pmf(mean, dispersion):
    """Negative binomial predictive distribution; variance = mean + dispersion*mean²."""
    shape = 1 / max(dispersion, .001)
    success = shape / (shape + mean)
    values = [success ** shape]
    cumulative = values[0]
    for k in range(1, 501):
        values.append(values[-1] * (k - 1 + shape) / k * (1 - success))
        cumulative += values[-1]
        if k >= 15 and cumulative > 1 - 1e-10:
            break
    total = sum(values)
    return [value / total for value in values]


def poisson_pmf(mean):
    values = [math.exp(-mean)]
    cumulative = values[0]
    for k in range(1, 501):
        values.append(values[-1] * mean / k)
        cumulative += values[-1]
        if k >= 15 and cumulative > 1 - 1e-10:
            break
    return [p / cumulative for p in values]


def shared_pace_matrix(home_mean, away_mean, dispersion):
    """Gamma-distributed common pace, with conditionally Poisson goal counts.

    E[pace]=1 and Var[pace]=dispersion. The joint distribution is negative
    multinomial, with negative binomial goal marginals. This permits positive
    score covariance; it cannot represent negative score covariance.
    """
    shape = 1 / max(dispersion, .001)
    denominator = shape + home_mean + away_mean
    home = goal_pmf(home_mean, dispersion)
    away = goal_pmf(away_mean, dispersion)
    constant = shape * math.log(shape / denominator) - math.lgamma(shape)
    matrix = []
    for h in range(len(home)):
        for a in range(len(away)):
            log_p = (constant + math.lgamma(shape+h+a) - math.lgamma(h+1) - math.lgamma(a+1)
                     + h * math.log(home_mean/denominator) + a * math.log(away_mean/denominator))
            matrix.append((h, a, math.exp(log_p)))
    total = sum(p for _, _, p in matrix)
    return [(h, a, p / total) for h, a, p in matrix]


class LeagueModel:
    def __init__(self, rows, as_of):
        self.as_of = as_of
        self.rows = sorted((row for row in rows if row.get("status") == "played"
                            and row.get("actual_score") and utc(row["kickoff_utc"]) < as_of
                            and utc(row.get("result_seen_at", as_of.isoformat())) <= as_of),
                           key=lambda row: utc(row["kickoff_utc"]))
        self.games = defaultdict(list)
        self.player_names = {}
        self.ratings = defaultdict(lambda: 1500.)
        self.attack, self.defense = defaultdict(lambda: 1.), defaultdict(lambda: 1.)
        self.club_attack, self.club_defense = defaultdict(lambda: 1.), defaultdict(lambda: 1.)
        self.effective = defaultdict(float)
        last_played = {}
        for row in self.rows:
            home, away = tag(row["home_tag"]), tag(row["away_tag"])
            self.player_names[home], self.player_names[away] = row["home_tag"], row["away_tag"]
            hg, ag = map(int, row["actual_score"].split("-"))
            kickoff = utc(row["kickoff_utc"])
            for player in (home, away):
                if player in last_played:
                    days = (kickoff - last_played[player]).total_seconds() / 86400
                    self.ratings[player] = 1500 + (self.ratings[player] - 1500) * .5 ** (days / 14)
                last_played[player] = kickoff
            expected = 1 / (1 + 10 ** ((self.ratings[away] - self.ratings[home]) / 400))
            actual = 1. if hg > ag else 0. if hg < ag else .5
            change = 24 * (actual - expected)
            for player, opponent, gf, ga, club, opponent_club, side in (
                (home, away, hg, ag, row["home_club"], row["away_club"], "home"),
                (away, home, ag, hg, row["away_club"], row["home_club"], "away"),
            ):
                self.games[player].append({"opponent": opponent, "goals_for": gf,
                    "goals_against": ga, "club": tag(club), "opponent_club": tag(opponent_club),
                    "side": side, "kickoff": kickoff, "opponent_rating": self.ratings[opponent]})
            self.ratings[home] += change
            self.ratings[away] -= change
        for player, last in last_played.items():
            days = (as_of - last).total_seconds() / 86400
            self.ratings[player] = 1500 + (self.ratings[player] - 1500) * .5 ** (days / 14)

        # Each player's latest 20 games receive both match-order and time decay.
        observations = []
        for player, games in self.games.items():
            for index, game in enumerate(reversed(games[-20:])):
                days = (as_of - game["kickoff"]).total_seconds() / 86400
                weight = .5 ** (index / 10 + days / 7)
                observations.append(dict(game, player=player, weight=weight))
                self.effective[player] += weight
        total_weight = sum(game["weight"] for game in observations)
        league_mean = sum(g["weight"] * g["goals_for"] for g in observations) / total_weight if total_weight else 2.5
        self.baseline = {}
        for side in ("home", "away"):
            subset = [game for game in observations if game["side"] == side]
            self.baseline[side] = max(.2, (20 * league_mean + sum(g["weight"] * g["goals_for"] for g in subset))
                                      / (20 + sum(g["weight"] for g in subset)))
        # Regularized multiplicative Poisson fit: opponent defense and club are
        # included in the exposure when estimating each player's attack.
        dimensions = (("player", self.attack, 12.), ("opponent", self.defense, 12.),
                      ("club", self.club_attack, 40.), ("opponent_club", self.club_defense, 40.))
        for _ in range(8):
            for dimension, parameters, prior_games in dimensions:
                goals, exposure = defaultdict(float), defaultdict(float)
                for game in observations:
                    key = game[dimension]
                    prior_exposure = prior_games * league_mean
                    if key not in goals:
                        goals[key] = exposure[key] = prior_exposure
                    base = self.rate(game) / parameters[key]
                    goals[key] += game["weight"] * game["goals_for"]
                    exposure[key] += game["weight"] * base
                for key in goals:
                    estimate = goals[key] / max(exposure[key], .01)
                    parameters[key] = clamp(math.sqrt(parameters[key] * estimate), .35, 2.8)
        numerator = denominator = 0.
        for game in observations:
            mean = self.rate(game)
            numerator += game["weight"] * ((game["goals_for"] - mean) ** 2 - mean)
            denominator += game["weight"] * mean ** 2
        self.dispersion = clamp(numerator / denominator if denominator else 0., 0., .35)
        covariance = exposure = 0.
        for row in self.rows:
            home, away = tag(row["home_tag"]), tag(row["away_tag"])
            hg, ag = map(int, row["actual_score"].split("-"))
            hm = self.rate({"side": "home", "player": home, "opponent": away,
                            "club": tag(row["home_club"]), "opponent_club": tag(row["away_club"])})
            am = self.rate({"side": "away", "player": away, "opponent": home,
                            "club": tag(row["away_club"]), "opponent_club": tag(row["home_club"])})
            days = (as_of - utc(row["kickoff_utc"])).total_seconds() / 86400
            weight = .5 ** (days / 7)
            covariance += weight * (hg - hm) * (ag - am)
            exposure += weight * hm * am
        # Shrink the league's residual covariance toward zero. This is a
        # candidate estimate, not an inferred causal measure of playing style.
        exposure += 40 * self.baseline["home"] * self.baseline["away"]
        self.shared_dispersion = clamp(covariance / exposure, 0., .35)

    def rate(self, game):
        return (self.baseline[game["side"]] * self.attack[game["player"]]
                * self.defense[game["opponent"]] * self.club_attack[game["club"]]
                * self.club_defense[game["opponent_club"]])

    def form(self, player):
        games = self.games[player]
        windows = {}
        for size in (5, 10, 20):
            recent = games[-size:]
            n = len(recent)
            windows[str(size)] = {"matches": n,
                "wins": sum(g["goals_for"] > g["goals_against"] for g in recent),
                "draws": sum(g["goals_for"] == g["goals_against"] for g in recent),
                "losses": sum(g["goals_for"] < g["goals_against"] for g in recent),
                "goals_for_per_game": round(sum(g["goals_for"] for g in recent) / n, 3) if n else None,
                "goals_against_per_game": round(sum(g["goals_against"] for g in recent) / n, 3) if n else None,
                "opponent_elo_average": round(sum(g["opponent_rating"] for g in recent) / n, 1) if n else None}
        return {"history_matches": len(games), "effective_recent_matches": round(self.effective[player], 2),
                "elo": round(self.ratings[player], 1), "windows": windows,
                "attack_strength": round(self.attack[player], 4),
                "defensive_strength": round(1 / self.defense[player], 4),
                "strength_scale": "1_is_league_average_higher_is_stronger",
                "last_result_kickoff": games[-1]["kickoff"].isoformat() if games else None}

    def predict(self, row, *, use_forebet=True, score_model="independent_nb", include_score_distribution=False,
                use_common_opponents=False):
        if score_model not in SCORE_MODELS:
            raise ValueError("Unknown score model")
        if utc(row["kickoff_utc"]) <= self.as_of or row.get("status") != "upcoming":
            raise ValueError("Only future fixtures can be forecast")
        home, away = tag(row["home_tag"]), tag(row["away_tag"])
        home_rate = self.rate({"side": "home", "player": home, "opponent": away,
                               "club": tag(row["home_club"]), "opponent_club": tag(row["away_club"])})
        away_rate = self.rate({"side": "away", "player": away, "opponent": home,
                               "club": tag(row["away_club"]), "opponent_club": tag(row["home_club"])})
        common = compare_common_opponents(self, home, away)
        common["adjustment_applied"] = bool(use_common_opponents and common["shared_opponent_count"])
        if use_common_opponents:
            home_rate *= common["home_goal_factor"]
            away_rate *= common["away_goal_factor"]
        hm, am = clamp(home_rate, .1, 15), clamp(away_rate, .1, 15)
        support = min(self.effective[home], self.effective[away])
        common_dispersion = self.dispersion + 1 / (12 + support)
        pace_weight = min(.7, self.shared_dispersion / common_dispersion)
        if score_model == "independent_poisson":
            hp, ap = poisson_pmf(hm), poisson_pmf(am)
        else:
            hp = goal_pmf(hm, self.dispersion + 1 / (12 + self.effective[home]))
            ap = goal_pmf(am, self.dispersion + 1 / (12 + self.effective[away]))
        joint = {(h, a): ph * pa for h, ph in enumerate(hp) for a, pa in enumerate(ap)}
        if score_model == "shared_pace_nb" and pace_weight > 0:
            joint = {key: p * (1-pace_weight) for key, p in joint.items()}
            for h, a, p in shared_pace_matrix(hm, am, common_dispersion):
                joint[h, a] = joint.get((h, a), 0.) + pace_weight * p
        matrix = [(h, a, p, "1" if h > a else "2" if a > h else "X") for (h, a), p in joint.items()]
        goal_probabilities = {key: sum(p for _, _, p, outcome in matrix if outcome == key)
                              for key in ("1", "X", "2")}
        decisive_home = 1 / (1 + 10 ** ((self.ratings[away] - self.ratings[home]) / 400))
        draw = goal_probabilities["X"]
        elo = {"1": (1 - draw) * decisive_home, "X": draw, "2": (1 - draw) * (1 - decisive_home)}
        model = {key: .7 * goal_probabilities[key] + .3 * elo[key] for key in elo}
        forebet = normalize({"1": row["p1"], "X": row["px"], "2": row["p2"]})
        minimum = min(len(self.games[home]), len(self.games[away]))
        own_weight = .75 * support / (support + 8)
        if not use_forebet:
            own_weight = 1.
        ensemble = {key: own_weight * model[key] + (1 - own_weight) * forebet[key] for key in elo}
        # Reweight score cells by outcome so winner, totals and exact scores agree.
        matrix = [(h, a, p * ensemble[outcome] / goal_probabilities[outcome]) for h, a, p, outcome in matrix]
        top_scores = sorted(matrix, key=lambda cell: cell[2], reverse=True)[:5]
        expected_home = sum(h * p for h, a, p in matrix)
        expected_away = sum(a * p for h, a, p in matrix)
        totals = {}
        for line in (1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5):
            over = sum(p for h, a, p in matrix if h + a > line)
            totals[str(line)] = {"over": round(over, 6), "under": round(1 - over, 6)}
        btts = sum(p for h, a, p in matrix if h > 0 and a > 0)
        h2h = [g for g in self.games[home] if g["opponent"] == away][-10:]
        warnings = ["experimental_model_not_yet_validated"]
        if minimum < 10:
            warnings.append("limited_player_history")
        if any(not self.games[p] or self.games[p][-1]["kickoff"] < self.as_of - timedelta(days=2)
               for p in (home, away)):
            warnings.append("missing_or_old_player_results")
        result = {key: row[key] for key in ("home_tag", "away_tag", "home_club", "away_club", "kickoff_utc")}
        if row.get("fixture_id"):
            result["fixture_id"] = row["fixture_id"]
        output = {**result, "model_version": MODEL_VERSION, "generated_at": self.as_of.isoformat(),
                "score_model": score_model,
                "common_opponent_adjustment": common["adjustment_applied"],
                "probabilities": {key: round(p, 6) for key, p in ensemble.items()},
                "most_likely_outcome": max(ensemble, key=ensemble.get),
                "pick": None,
                "winner_assessment": "no clear winner",
                "prediction_type": "prematch",
                "calibration_status": "unvalidated",
                "forebet_probabilities": {key: round(p, 6) for key, p in forebet.items()},
                "form_model_probabilities": {key: round(p, 6) for key, p in model.items()},
                "ensemble_weights": {"form_model": round(own_weight, 4), "forebet": round(1-own_weight, 4)},
                "expected_goals": {"home": round(expected_home, 3), "away": round(expected_away, 3),
                                   "total": round(expected_home + expected_away, 3)},
                "most_likely_score": f"{top_scores[0][0]}-{top_scores[0][1]}",
                "top_scores": [{"score": f"{h}-{a}", "probability": round(p, 6)} for h, a, p in top_scores],
                "totals": totals, "btts": {"yes": round(btts, 6), "no": round(1-btts, 6)},
                "evidence": {"home": self.form(home), "away": self.form(away),
                    "common_opponents": common,
                    "matchup": {
                        "home_goals_before_outcome_blend": round(hm, 3),
                        "away_goals_before_outcome_blend": round(am, 3),
                        "home_league_goal_rate": round(self.baseline["home"], 3),
                        "away_league_goal_rate": round(self.baseline["away"], 3),
                        "home_club_attack_factor": round(self.club_attack[tag(row["home_club"])], 4),
                        "home_club_concession_factor": round(self.club_defense[tag(row["home_club"])], 4),
                        "away_club_attack_factor": round(self.club_attack[tag(row["away_club"])], 4),
                        "away_club_concession_factor": round(self.club_defense[tag(row["away_club"])], 4),
                        "shared_pace_weight": round(pace_weight, 4) if score_model == "shared_pace_nb" else 0.},
                    "head_to_head": {"matches": len(h2h),
                        "home_player_wins": sum(g["goals_for"] > g["goals_against"] for g in h2h),
                        "draws": sum(g["goals_for"] == g["goals_against"] for g in h2h),
                        "away_player_wins": sum(g["goals_for"] < g["goals_against"] for g in h2h)},
                    "league_training_matches": len(self.rows)},
                "data_quality": "limited" if minimum < 10 else "developing",
                "warnings": warnings}
        ranked = sorted(ensemble.values(), reverse=True)
        # A conservative display rule, not a calibrated confidence threshold.
        if ranked[0] >= .55 and ranked[0] - ranked[1] >= .15 and minimum >= 10:
            output["pick"] = output["most_likely_outcome"]
            output["winner_assessment"] = "experimental lean"
        else:
            warnings.append("no_clear_winner")
        for key in ("source_kickoff_utc", "kickoff_correction_minutes", "kickoff_time_status"):
            if key in row:
                output[key] = row[key]
        if row.get("kickoff_time_status") in {"source_unverified", "inferred_from_live_clocks"}:
            warnings.append("kickoff_time_not_independently_verified")
        if include_score_distribution:
            output["score_distribution"] = {f"{h}-{a}": p for h, a, p in matrix}
        return output
