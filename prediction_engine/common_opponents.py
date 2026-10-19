"""In-memory comparison of A→C and B→C paths in the result graph.

The main model already fits these matches. Its residuals, rather than a second
copy of the raw win counts, provide a conservative optional adjustment.
"""

import math
from collections import defaultdict


def compare_common_opponents(model, home, away):
    def grouped(player):
        peers = defaultdict(list)
        for index, game in enumerate(reversed(model.games[player][-20:])):
            age = (model.as_of - game["kickoff"]).total_seconds() / 86400
            weight = .5 ** (index / 10 + age / 7)
            peers[game["opponent"]].append((game, weight))
        return peers

    def summarize(player, observations):
        effective = sum(weight for _, weight in observations)
        gf = ga = expected_for = expected_against = time = 0.
        for game, weight in observations:
            gf += weight * game["goals_for"]
            ga += weight * game["goals_against"]
            expected_for += weight * model.rate(dict(game, player=player))
            expected_against += weight * model.rate({
                "player": game["opponent"], "opponent": player,
                "club": game["opponent_club"], "opponent_club": game["club"],
                "side": "away" if game["side"] == "home" else "home"})
            time += weight * game["kickoff"].timestamp()
        # Four league-average games pull unstable residual ratios toward 1.
        prior = 4 * (model.baseline["home"] + model.baseline["away"]) / 2
        attack_residual = math.log((gf + prior) / (expected_for + prior))
        defense_residual = math.log((expected_against + prior) / (ga + prior))
        public = {"matches": len(observations), "effective_matches": round(effective, 3),
            "wins": sum(game["goals_for"] > game["goals_against"] for game, _ in observations),
            "draws": sum(game["goals_for"] == game["goals_against"] for game, _ in observations),
            "losses": sum(game["goals_for"] < game["goals_against"] for game, _ in observations),
            "weighted_goals_for": round(gf / effective, 3),
            "weighted_goals_against": round(ga / effective, 3),
            "attack_residual_factor": round(math.exp(attack_residual), 4),
            "defense_residual_factor": round(math.exp(defense_residual), 4),
            "latest_kickoff": max(game["kickoff"] for game, _ in observations).isoformat(),
            "clubs_used": sorted({game["club"] for game, _ in observations}),
            "opponent_clubs": sorted({game["opponent_club"] for game, _ in observations})}
        return public, effective, time / effective, attack_residual, defense_residual

    home_peers, away_peers = grouped(home), grouped(away)
    comparisons = []
    total_support = home_attack = home_defense = away_attack = away_defense = 0.
    for peer in sorted((home_peers.keys() & away_peers.keys()) - {home, away}):
        hs, hn, ht, ha, hd = summarize(home, home_peers[peer])
        aws, an, at, aa, ad = summarize(away, away_peers[peer])
        # Same opponent at very different times is a weaker comparison.
        proximity = .5 ** (abs(ht-at) / (2*86400))
        support = min(hn, an, 3.) * proximity
        total_support += support
        home_attack += support * ha
        home_defense += support * hd
        away_attack += support * aa
        away_defense += support * ad
        comparisons.append({"opponent_tag": model.player_names.get(peer, peer),
                            "home_player": hs, "away_player": aws,
                            "matched_support": round(support, 3),
                            "time_proximity_weight": round(proximity, 3)})
    count = len(comparisons)
    # Fixed experimental cap; never assigned a major weight without validation.
    weight = .25 * total_support / (total_support + 6) * count / (count + 2)
    if total_support:
        home_log = weight * (home_attack - away_defense) / total_support
        away_log = weight * (away_attack - home_defense) / total_support
    else:
        home_log = away_log = 0.
    cap = math.log(1.1)
    home_factor = math.exp(max(-cap, min(cap, home_log)))
    away_factor = math.exp(max(-cap, min(cap, away_log)))
    return {"shared_opponent_count": count,
            "status": "no_shared_history" if not count else "limited" if count < 3 or total_support < 4 else "available",
            "matched_support": round(total_support, 3), "candidate_weight": round(weight, 4),
            "home_goal_factor": home_factor, "away_goal_factor": away_factor,
            "comparisons": comparisons,
            "interpretation": "Residual factors above 1 mean better attack or defense than the existing model expected; this is not a guarantee of beating the next opponent."}
