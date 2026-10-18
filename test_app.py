import unittest
from unittest.mock import patch

import app


SAMPLE_ROW = """
<div class="rcnt tr_0">
  <span class="homeTeam">Liverpool (Kray)</span>
  <span class="awayTeam">PSG (OG)</span>
  <span class="date_bah">18/10/2026 13:16</span>
  <div class="fprc"><span>45</span><span>21</span><span>34</span></div>
  <div class="predict"><span class="forepr">1</span></div>
  <div class="ex_sc tabonly">3 - 2</div>
  <div class="avg_sc tabonly">4.81</div>
</div>
"""


class PredictionTests(unittest.TestCase):
    def test_parses_forebet_row(self):
        fixture = app.parse_predictions(SAMPLE_ROW)[0]
        self.assertEqual(fixture, {
            "home": "Liverpool (Kray)",
            "away": "PSG (OG)",
            "kickoff_utc": "2026-10-18T13:16:00Z",
            "p1": 45,
            "px": 21,
            "p2": 34,
            "pred": "1",
            "pred_score": "3-2",
            "avg_goals": 4.81,
        })

    def test_challenge_is_reported_per_league_and_cached(self):
        app._cache_data = None
        app._cache_expires = 0
        body = b"<title>Just a moment...</title>"
        with patch.object(app, "fetch_page", return_value=(403, body)) as fetch:
            first = app.get_predictions()
            second = app.get_predictions()
        self.assertIs(first, second)
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(first["leagues"], {
            key: {"error": "challenged"} for key in app.LEAGUES
        })


if __name__ == "__main__":
    unittest.main()
