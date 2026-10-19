import unittest
from datetime import datetime, timedelta, timezone
import subprocess
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
        with patch.object(app, "run_reader", return_value={
            key: {"fixtures": [{"home_tag": "Mani", "kickoff_utc": "2026-10-18T16:55:00+00:00"}], "status": "ok"}
            for key in app.LEAGUES
        }):
            app.refresh_pages()
        with app.app.test_client() as client:
            data = client.get("/predictions").get_json()
        self.assertFalse(data["stale"])
        self.assertEqual(data["leagues"]["esb8"][0]["home_tag"], "Mani")
        self.assertTrue(all(data["league_fetched_at"].values()))
        self.assertEqual(data["league_fetched_at"], data["league_changed_at"])

    def test_unchanged_fixtures_preserve_change_time(self):
        fixture = {"home_tag": "Mani"}
        with patch.object(app, "run_reader", return_value={
            key: {"fixtures": [fixture], "status": "ok"} for key in app.LEAGUES
        }):
            app.refresh_pages()
            first_changed_at = app.cache["leagues"]["esb8"]["changed_at"]
            app.refresh_pages()
        with app.app.test_client() as client:
            data = client.get("/predictions").get_json()
        self.assertEqual(data["league_changed_at"]["esb8"], first_changed_at)

    def test_old_cache_is_marked_stale_even_without_failed_refresh(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat()
        with app.cache_lock:
            app.cache["stale"] = False
            for key in app.LEAGUES:
                app.cache["leagues"][key] = {
                    "fixtures": [{"home_tag": "old"}], "fetched_at": old,
                    "changed_at": old, "status": "ok",
                }
        with app.app.test_client() as client:
            data = client.get("/predictions").get_json()
        self.assertTrue(data["stale"])

    def test_failed_league_retains_last_good_fixtures(self):
        with app.cache_lock:
            app.cache["leagues"]["esb8"] = {"fixtures": [{"home_tag": "old"}], "status": "ok"}
        with patch.object(app, "run_reader", return_value={
            "esb8": {"status": "error", "error": "challenged"},
            "volta6": {"fixtures": [{"home_tag": "new"}], "status": "ok"},
        }):
            app.refresh_pages()
        with app.app.test_client() as client:
            data = client.get("/predictions").get_json()
        self.assertTrue(data["stale"])
        self.assertEqual(data["leagues"]["esb8"][0]["home_tag"], "old")
        self.assertEqual(data["leagues"]["h2hgg"], {"status": "error"})

    def test_full_refresh_failure_rotates_proxy_session(self):
        previous_session = app.proxy_session_id
        with patch.object(app, "run_reader", return_value={
            key: {"status": "error"} for key in app.LEAGUES
        }):
            app.refresh_pages()
        self.assertNotEqual(app.proxy_session_id, previous_session)

    def test_reader_subprocess_receives_session_id(self):
        process = MagicMock()
        process.returncode = 0
        process.communicate.return_value = (b"{}", None)
        with patch.object(app.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(app.run_reader(), {})
        self.assertEqual(popen.call_args.kwargs["env"]["SHIFTER_SESSION_ID"], app.proxy_session_id)

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
            "status": "upcoming",
            "actual_score": "",
        }, {
            "home": "Incomplete row",
            "away": "Juventus (DEKSON)",
            "date": "18/10/2026 16:55",
            "probabilities": ["46", "23", "31"],
            "pred": "1",
            "score": "4 - 3",
            "avg_goals": "6.11",
            "status": "upcoming",
            "actual_score": "",
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
            "status": "upcoming", "actual_score": None,
        })
        self.assertEqual(reader.parse_kickoff("10/18/2026 4:55 PM").isoformat(),
                         "2026-10-18T16:55:00+00:00")

    def test_parses_finished_fixture_with_actual_score(self):
        fixture = reader.parse_fixture({
            "home": "England (kkikus)", "away": "Argentina (noeN)",
            "date": "17/10/2026 20:07",
            "probabilities": ["20", "18", "62"],
            "pred": "2", "score": "2 - 3", "avg_goals": "6.02",
            "status": "played", "actual_score": "2 - 4",
        })
        self.assertEqual(fixture["status"], "played")
        self.assertEqual(fixture["actual_score"], "2-4")
        self.assertEqual(fixture["correct_score"], "2-3")

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

    def test_visit_waits_for_fixture_rows(self):
        page = MagicMock()
        with patch.object(reader, "clear_cloudflare", return_value=True):
            reader.visit(page, "https://www.forebet.com/en/esoccer", fixture_page=True)
        page.wait_for_function.assert_called_once()
        page.wait_for_timeout.assert_not_called()

    def test_hung_reader_is_killed(self):
        process = MagicMock()
        process.pid = 12345
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("reader", app.READER_TIMEOUT_SECONDS),
            (b"", None),
        ]
        with patch.object(app.subprocess, "Popen", return_value=process), patch.object(app.os, "killpg") as kill:
            with self.assertRaises(TimeoutError):
                app.run_reader()
        kill.assert_called_once_with(12345, app.signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
