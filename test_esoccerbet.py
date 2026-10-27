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
        # A browser's copy of a page writes the same attributes with double quotes.
        self.assertEqual(esoccerbet.results(HOME.replace("'", '"'), "/", NOW), home)
        self.assertEqual(len(esoccerbet.schedule(SCHEDULE.replace("'", '"'), NOW)), 2)
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

    def test_pages_read_by_the_reader_browser_are_taken(self):
        esoccerbet.ROWS.clear()
        pages = {key: {"status": "ok", "fixtures": []} for key in app.LEAGUES}
        pages["_esoccerbet"] = {"status": "ok", "pages": {"/schedule/": SCHEDULE}, "failed": []}
        try:
            with patch.object(app, "run_reader", return_value=pages):
                app.refresh_pages()
            self.assertEqual(esoccerbet.STATUS["read_by"], "browser")
            self.assertEqual(len(esoccerbet.rows()), 2)
            with app.cache_lock:
                self.assertNotIn("_esoccerbet", app.cache["leagues"])
        finally:
            esoccerbet.ROWS.clear()

    def test_official_games_carry_their_league_and_source(self):
        leagues = {"gt12": [forebet_row("Voodoo", "Walnut", "2026-10-27T16:30:00+00:00")], "esb8": []}
        games = [{"league": "gt12", "home": "Voodoo", "away": "Walnut", "home_club": "", "away_club": "",
                  "kickoff": datetime(2026, 10, 27, 16, 30, tzinfo=timezone.utc), "score": "4-3", "status": "played",
                  "source": "gtleagues"},
                 {"league": "esb8", "home": "Mani", "away": "Krkula", "home_club": "", "away_club": "",
                  "kickoff": datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc), "score": None, "status": "upcoming",
                  "source": "esportsbattle"}]
        merged = app.merge_backup(leagues, games, NOW)
        self.assertEqual((merged["gt12"][0]["actual_score"], merged["gt12"][0]["result_source"]), ("4-3", "gtleagues"))
        self.assertEqual((merged["esb8"][0]["home_tag"], merged["esb8"][0]["source"]), ("Mani", "esportsbattle"))

    def test_official_feeds_are_read(self):
        import official
        esb = {"/tournaments?": {"totalPages": 1, "tournaments": [
                   {"id": 1, "token_international": "Champions League A 2026-10-27"},
                   {"id": 2, "token_international": "Volta World Cup B 2026-10-27"},
                   {"id": 3, "token_international": "International A 2x6 2026-10-27"}]},
               "/tournaments/1/": [{"date": "2026-10-27T16:35:00Z", "status_id": 3,
                                    "participant1": {"nickname": "Mani", "score": 7, "team": {"token_international": "x"}},
                                    "participant2": {"nickname": "Krkula", "score": 1, "team": {}}}],
               "/tournaments/2/": [{"date": "2026-10-27T23:00:00Z", "status_id": 1,
                                    "participant1": {"nickname": "Groma"}, "participant2": {"nickname": "Senya"}}]}
        h2h = [{"startDate": "2026-10-27T17:16:00Z", "isCancelled": False, "tournamentName": "Esoccer H2H GG League",
                "participantAName": "TUSK", "participantBName": "CLINICAL", "matchStatus": "MATCH_ENDED",
                "teamAScore": 0, "teamBScore": 1}]
        gt = [{"kickoff": "2026-10-27T17:15:00.000Z", "status": 3, "result": {"stats": {"home_score": 3, "away_score": 1}},
               "participants": [{"side": "home", "participant": {"player": {"nickname": "Vendetta"}, "team": {"name": "a"}}},
                                {"side": "away", "participant": {"player": {"nickname": "King"}, "team": {"name": "b"}}}]}]

        def fake(url, headers=None, timeout=25, proxy=None):
            if url.startswith(official.ESB):
                return next(value for key, value in esb.items() if key in url)
            if url.startswith(official.GT):
                self.assertEqual(headers["Origin"], "https://www.gtleagues.com")
                return gt
            return h2h
        start, end = datetime(2026, 10, 27, 16, tzinfo=timezone.utc), datetime(2026, 10, 28, tzinfo=timezone.utc)
        with patch.object(official, "get", side_effect=fake):
            battle = official.esportsbattle(start, end)
            self.assertEqual([(g["league"], g["home"], g["score"], g["status"]) for g in battle],
                             [("esb8", "Mani", "7-1", "played"), ("volta6", "Groma", None, "upcoming")])   # no 2x6
            self.assertEqual([(g["home"], g["score"]) for g in official.h2h(start)], [("TUSK", "0-1")])
            self.assertEqual([(g["league"], g["home"], g["away"], g["score"]) for g in official.gt(start, end)],
                             [("gt12", "Vendetta", "King", "3-1")])


if __name__ == "__main__":
    unittest.main()
