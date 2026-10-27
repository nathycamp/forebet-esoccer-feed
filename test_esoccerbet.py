import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import app
import esoccerbet

NOW = datetime(2026, 10, 27, 20, 50, tzinfo=timezone.utc)
HOME = """<div class='matchCard'>
    <div class='team'>
        <a href='/fifa-8-minutes/dekson/'>dekson</a>
        <span class='tmName'>roma</span>
    </div>
    <div class='score'>
        <span class='nums'>3 - 2</span>
        <!--<span class='htScore'>3 – 2</span>-->
        <span class='matchDate'>27/10 20:29</span>
    </div>
    <div class='team right'>
        <a href='/fifa-8-minutes/krkula/'>krkula</a>
        <span class='tmName'>sassuolo</span>
    </div>
</div>"""
LEAGUE = """<div class='matchCard'>
    <div class='team'>
        <a href='threat/'>threat</a>
        <span class='tmName'>manchester city</span>
    </div>
    <div class='score'>
        <span class='nums'><span class='ht'>(0)</span>2 - 3<span class='ht'>(2)</span></span>
        <span class='matchDate'>10/27 20:36</span>
    </div>
    <div class='team right'>
        <a href='unleashed/'>unleashed</a>
        <span class='tmName'>paris saint-germain</span>
    </div>
</div>"""
SCHEDULE = ("<tr class='linhasTR'><td>20:53<br><span style='opacity:.5'>10/27</span></td><td><a href='/fifa-8-minutes/"
            "mani/'>mani</a><span class='teamName'>bologna</span></td><td><a href='/fifa-8-minutes/sena/'>sena</a>"
            "<span class='teamName'>napoli</span></td></tr>"
            "<tr class='linhasTR'><td>01:00<br><span style='opacity:.5'>10/27</span></td><td><a href='/fifa-12-minutes/"
            "king/'>king</a><span class='teamName'>napoli</span></td><td><a href='/fifa-12-minutes/fred/'>fred</a>"
            "<span class='teamName'>lens</span></td></tr>")


def forebet_row(home, away, kickoff, status="upcoming", score=None):
    return {"home_tag": home, "away_tag": away, "home_club": "x", "away_club": "y", "kickoff_utc": kickoff,
            "p1": 40, "px": 20, "p2": 40, "pred": "1", "correct_score": "2-1", "avg_goals": 4.5, "status": status,
            "actual_score": score, "live_score": None, "match_clock": None}


class EsoccerbetTest(unittest.TestCase):
    def test_pages_are_read(self):
        home = esoccerbet.results(HOME, "/", NOW)
        self.assertEqual(home, [{"minutes": "8", "home": "dekson", "away": "krkula", "home_club": "roma",
                                 "away_club": "sassuolo", "kickoff": datetime(2026, 10, 27, 20, 29, tzinfo=timezone.utc),
                                 "score": "3-2"}])
        league = esoccerbet.results(LEAGUE, "/fifa-8-minutes/", NOW)
        self.assertEqual((league[0]["home"], league[0]["score"], league[0]["kickoff"].hour), ("threat", "2-3", 20))
        games = esoccerbet.schedule(SCHEDULE, NOW)
        self.assertEqual([(g["home"], g["kickoff"].isoformat()) for g in games],
                         [("mani", "2026-10-27T20:53:00+00:00"),
                          ("king", "2026-10-28T01:00:00+00:00")])     # Brazil's date: 01:00 UTC is the next day

    def test_merge_fills_results_adds_games_and_keeps_names(self):
        leagues = {
            "esb8": [forebet_row("DEKSON", "Krkula", "2026-10-27T20:30:00+00:00"),
                     forebet_row("Mani", "Sena", "2026-10-27T19:00:00+00:00", "played", "1-1")],
            "h2hgg": [forebet_row("THREAT", "UNLEASHED", "2026-10-27T18:00:00+00:00", "played", "0-0")],
            "gt12": {"status": "error"},
        }
        games = (esoccerbet.results(HOME, "/", NOW) + esoccerbet.results(LEAGUE, "/fifa-8-minutes/", NOW)
                 + esoccerbet.schedule(SCHEDULE, NOW)
                 + [{"minutes": "8", "home": "nobody", "away": "else", "home_club": "", "away_club": "",
                     "kickoff": NOW, "score": "1-0"}])
        merged = app.merge_backup(leagues, games, NOW)
        esb8 = {(r["home_tag"], r["kickoff_utc"][11:16]): r for r in merged["esb8"]}
        # Forebet's unfinished game gets esoccerbet's result (kickoff 1 minute apart).
        self.assertEqual((esb8["DEKSON", "20:30"]["status"], esb8["DEKSON", "20:30"]["actual_score"]), ("played", "3-2"))
        self.assertNotIn("source", esb8["DEKSON", "20:30"])
        # A game Forebet does not list is added with Forebet's case for the players.
        self.assertEqual((esb8["Mani", "20:53"]["away_tag"], esb8["Mani", "20:53"]["source"]), ("Sena", "esoccerbet"))
        self.assertEqual(merged["h2hgg"][-1]["home_tag"], "THREAT")          # 8 minutes, h2hgg players
        self.assertEqual(merged["gt12"][0]["home_tag"], "king")               # an unknown GT player keeps its name
        self.assertEqual(len(merged["esb8"]) + len(merged["h2hgg"]), 5)       # unknown 8-minute players are left out
        self.assertEqual(leagues["esb8"][0]["status"], "upcoming")             # the cache is not changed

    def test_feed_stays_fresh_on_esoccerbet_when_forebet_is_down(self):
        esoccerbet.ROWS.clear()
        esoccerbet.remember(esoccerbet.schedule(SCHEDULE, NOW), NOW)
        with app.cache_lock:
            saved = dict(app.cache)
            app.cache.update(leagues={}, stale=True)
        try:
            with patch.dict(esoccerbet.STATUS, {"fetched_at": NOW.isoformat()}):
                feed = app.snapshot({"gt12"}, NOW)
            self.assertFalse(feed["stale"])
            self.assertEqual(feed["leagues"]["gt12"][0]["source"], "esoccerbet")
        finally:
            with app.cache_lock:
                app.cache.clear()
                app.cache.update(saved)
            esoccerbet.ROWS.clear()


if __name__ == "__main__":
    unittest.main()
