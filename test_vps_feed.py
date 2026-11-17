"""Run on its own (python3 -m unittest test_vps_feed): vps_feed stands in for reader and apscheduler."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

os.environ["STATE_DIR"] = tempfile.mkdtemp()
import vps_feed  # noqa: E402


class ForebetCopyTests(unittest.TestCase):
    def setUp(self):
        vps_feed.app.cache["leagues"].clear()

    def test_railways_forebet_h2h_games_are_copied_and_kept(self):
        now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
        rows = [{"home_tag": "TIGER", "away_tag": "SAINT", "kickoff_utc": "2026-11-17T12:30:00+00:00", "status": "upcoming"},
                {"home_tag": "DUSK", "away_tag": "FAITH", "kickoff_utc": "2026-11-17T10:00:00+00:00", "actual_score": "1-2"},
                {"home_tag": "OLD", "away_tag": "GAME", "kickoff_utc": "2026-11-17T07:00:00+00:00"},          # too old
                {"home_tag": "INSTINCT", "away_tag": "TUSK", "kickoff_utc": "2026-11-17T12:30:00+00:00",
                 "source": "h2hggl"}]                                                                          # official
        calls = []

        def fetch_json(url, timeout=None):
            calls.append(url)
            return {"leagues": {"h2hgg": rows}}
        self.assertTrue(vps_feed.copy_forebet(fetch_json, now))
        self.assertIn("league=h2hgg", calls[0])
        kept = vps_feed.app.cache["leagues"]["h2hgg"]["fixtures"]
        self.assertEqual([r["home_tag"] for r in kept], ["TIGER", "DUSK"])
        vps_feed.app.cache["leagues"].clear()                                   # a restart while Railway is down
        self.assertTrue(vps_feed.load_copy(now))
        self.assertEqual(len(vps_feed.app.cache["leagues"]["h2hgg"]["fixtures"]), 2)
        with vps_feed.app.app.test_client() as client:
            served = client.get("/predictions?league=h2hgg").get_json()["leagues"]["h2hgg"]
        self.assertIn("TIGER", {r.get("home_tag") for r in served})

    def test_railway_down_keeps_the_last_copy(self):
        def down(url, timeout=None):
            raise OSError("railway down")
        self.assertFalse(vps_feed.copy_forebet(down))


if __name__ == "__main__":
    unittest.main()
