import unittest
from unittest.mock import MagicMock, patch

import app
import reader


class ReaderServiceTests(unittest.TestCase):
    def setUp(self):
        with app.cache_lock:
            app.cache.update({
                "last_fetch": None,
                "last_status": "error",
                "fetched_at": None,
                "stale": True,
                "leagues": {},
            })

    def test_health_and_structured_fixtures(self):
        with app.app.test_client() as client:
            self.assertEqual(client.get("/").get_json(), {
                "status": "ok", "last_fetch": None, "last_status": "error"
            })
        with patch.object(app, "read_pages", return_value={
            key: {"fixtures": [{"home_tag": "Mani", "kickoff_utc": "2026-10-18T16:55:00+00:00"}], "status": "ok"}
            for key in app.LEAGUES
        }):
            app.refresh_pages()
        with app.app.test_client() as client:
            data = client.get("/predictions").get_json()
        self.assertFalse(data["stale"])
        self.assertEqual(data["leagues"]["esb8"][0]["home_tag"], "Mani")

    def test_failed_league_retains_last_good_fixtures(self):
        with app.cache_lock:
            app.cache["leagues"]["esb8"] = {"fixtures": [{"home_tag": "old"}], "status": "ok"}
        with patch.object(app, "read_pages", return_value={
            "esb8": {"status": "error", "error": "challenged"},
            "volta6": {"fixtures": [{"home_tag": "new"}], "status": "ok"},
        }):
            app.refresh_pages()
        with app.app.test_client() as client:
            data = client.get("/predictions").get_json()
        self.assertTrue(data["stale"])
        self.assertEqual(data["leagues"]["esb8"][0]["home_tag"], "old")
        self.assertEqual(data["leagues"]["h2hgg"], {"status": "error"})

    def test_parses_fixture_fields_from_dom(self):
        page = MagicMock()
        page.evaluate.return_value = [{
            "home": "Bologna (Jankulovski)",
            "away": "Juventus (DEKSON)",
            "date": "18/10/2026 16:55",
            "probabilities": ["46", "23", "31"],
            "pred": "1",
            "score": "4 - 3",
            "avg_goals": "6.11",
        }, {
            "home": "Incomplete row",
            "away": "Juventus (DEKSON)",
            "date": "18/10/2026 16:55",
            "probabilities": ["46", "23", "31"],
            "pred": "1",
            "score": "4 - 3",
            "avg_goals": "6.11",
        }]
        with self.assertLogs(reader.logger, level="WARNING"):
            fixtures = reader.extract_fixtures(page, "esb8")
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0], {
            "home_tag": "Jankulovski", "away_tag": "DEKSON",
            "home_club": "Bologna", "away_club": "Juventus",
            "kickoff_utc": "2026-10-18T16:55:00+00:00",
            "p1": 46, "px": 23, "p2": 31, "pred": "1",
            "correct_score": "4-3", "avg_goals": 6.11,
        })
        self.assertEqual(reader.parse_kickoff("10/18/2026 4:55 PM").isoformat(),
                         "2026-10-18T16:55:00+00:00")

    def test_cloudflare_click_and_wait(self):
        page = MagicMock()
        page.title.side_effect = ["Just a moment", "Forebet"]
        page.inner_text.return_value = ""
        with patch.object(reader, "click_cloudflare", return_value=True) as click:
            self.assertTrue(reader.clear_cloudflare(page))
        click.assert_called_once_with(page)
        self.assertEqual([call.args[0] for call in page.wait_for_timeout.call_args_list], [3000, 5000])

    def test_invisible_iframe_is_not_clicked(self):
        page = MagicMock()
        frame = MagicMock()
        frame.url = "https://challenges.cloudflare.com/widget"
        frame.locator.return_value.count.return_value = 0
        frame.frame_element.return_value.bounding_box.return_value = {
            "x": 192, "y": 304, "width": 0, "height": 0,
        }
        page.frames = [frame]
        page.locator.return_value.count.return_value = 0
        page.get_by_text.return_value.count.return_value = 0
        self.assertFalse(reader.click_cloudflare(page))
        page.mouse.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
