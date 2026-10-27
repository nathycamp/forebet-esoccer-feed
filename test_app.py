import os
import unittest
from datetime import datetime, timedelta, timezone
import subprocess
from unittest.mock import MagicMock, patch

import app
import esoccerbet
import reader
import telegram_bot


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

    def test_free_proxies_back_up_shifter(self):
        environment = {"SHIFTER_USER": "u", "SHIFTER_HOST": "h", "SHIFTER_PORT": "1", "SHIFTER_PASS": "p",
                       "LAST_PROXY_FILE": "/nonexistent/last"}
        with patch.dict(os.environ, environment), \
                patch.object(reader, "LAST_PROXY_FILE", "/nonexistent/last"), \
                patch.object(reader, "load_free_proxy_pool", return_value=["socks5://1.1.1.1:1", "http://2.2.2.2:2", "http://3.3.3.3:3"]), \
                patch.object(reader, "free_proxies") as fetch:
            servers = [proxy["server"] for proxy in reader.proxy_candidates()]
        self.assertEqual(servers, ["http://h:1", "socks5://1.1.1.1:1", "http://2.2.2.2:2", "http://3.3.3.3:3"])
        fetch.assert_not_called()

    def test_slow_proxy_is_benched_and_missed_leagues_retried(self):
        import tempfile
        folder = tempfile.mkdtemp()
        reads = []

        def visit(page, url, fixture_page=False):
            reads.append((proxies[-1], url))
            if proxies[-1] == "socks5://slow:1" and "volta" in url:
                raise TimeoutError("Page.goto: Timeout 45000ms exceeded.")

        proxies = []
        playwright = MagicMock()
        playwright.__enter__.return_value.chromium.launch_persistent_context.side_effect = \
            lambda *args, proxy, **kwargs: proxies.append(proxy["server"]) or MagicMock(pages=[MagicMock()])
        environment = {"PROXY_SOURCE": "proxifly", "FREE_PROXY_SEEDS": "socks5://slow:1", "READER_TIMEOUT_SECONDS": "150"}
        with patch.dict(os.environ, environment), \
                patch.object(reader, "LAST_PROXY_FILE", f"{folder}/last"), \
                patch.object(reader, "BENCH_FILE", f"{folder}/bench.json"), \
                patch.object(reader, "load_free_proxy_pool", return_value=["http://fast:2"]), \
                patch.object(reader, "free_proxies", return_value=[]), \
                patch.object(reader, "sync_playwright", return_value=playwright), \
                patch.object(reader, "ByteMeter"), patch.object(reader, "block_forebet_assets"), \
                patch.object(reader, "visit", side_effect=visit), \
                patch.object(reader, "extract_fixtures", return_value=[]), \
                patch.object(reader, "check_live_clock", return_value={}):
            leagues = reader.read_pages()
            self.assertEqual({page["status"] for page in leagues.values()}, {"ok"})
            self.assertEqual(proxies, ["socks5://slow:1", "http://fast:2"])
            # The second proxy only reads what the first missed.
            league_reads = [url for proxy, url in reads if proxy == "http://fast:2" and "/esoccer/" in url]
            self.assertEqual(len(league_reads), 1)
            self.assertIn("volta", league_reads[0])
            self.assertNotIn("socks5://slow:1", reader.load_bench())  # seeds are never benched
        with patch.dict(os.environ, {"PROXY_SOURCE": "proxifly", "FREE_PROXY_SEEDS": ""}), \
                patch.object(reader, "LAST_PROXY_FILE", f"{folder}/none"), \
                patch.object(reader, "BENCH_FILE", f"{folder}/bench.json"), \
                patch.object(reader, "load_free_proxy_pool", return_value=["http://a:1", "http://b:2", "http://c:3"]):
            reader.bench_proxy("http://a:1")
            self.assertEqual([proxy["server"] for proxy in reader.proxy_candidates()], ["http://b:2", "http://c:3", "http://a:1"])

    def test_league_urls_are_tagged_per_read(self):
        moment = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(reader.fresh_url("https://x/a", moment), f"https://x/a?t={int(moment.timestamp())}")
        self.assertEqual(reader.fresh_url("https://x/a?b=1", moment), f"https://x/a?b=1&t={int(moment.timestamp())}")
        with patch.dict(os.environ, {"CACHE_BUST": "0"}):
            self.assertEqual(reader.fresh_url("https://x/a", moment), "https://x/a")

    def test_demoted_proxy_moves_to_back_of_pool(self):
        import json, tempfile
        folder = tempfile.mkdtemp()
        last, pool = f"{folder}/last", f"{folder}/pool.json"
        with open(last, "w") as handle:
            handle.write("http://a:1")
        with open(pool, "w") as handle:
            json.dump(["http://a:1", "http://b:2", "http://c:3"], handle)
        with patch.object(reader, "LAST_PROXY_FILE", last), patch.object(reader, "FREE_PROXY_POOL_FILE", pool):
            self.assertEqual(reader.demote_last_proxy(), "http://a:1")
            self.assertEqual(reader.load_free_proxy_pool(), ["http://b:2", "http://c:3", "http://a:1"])
            self.assertIsNone(reader.demote_last_proxy())
            reader.drop_free_proxy("http://b:2")
            self.assertEqual(reader.load_free_proxy_pool(), ["http://c:3", "http://a:1"])

    def test_games_never_go_backwards(self):
        live = {"home_tag": "A", "away_tag": "B", "kickoff_utc": "2026-10-23T18:44:00+00:00",
                "status": "live", "live_score": "1-0", "match_clock": "3"}
        reverted = dict(live, status="upcoming", live_score=None, match_clock=None)
        later = dict(live, status="played", actual_score="2-0", live_score=None)
        other = dict(reverted, home_tag="C")
        self.assertEqual(app.keep_progress([reverted, other], [live]), [live, other])
        self.assertEqual(app.keep_progress([later], [live]), [later])
        self.assertEqual(app.keep_progress([reverted], [later]), [later])

    def test_a_final_score_below_one_already_seen_is_ignored(self):
        live = {"home_tag": "A", "away_tag": "B", "kickoff_utc": "2026-10-25T15:38:00+00:00",
                "status": "live", "live_score": "4-1", "match_clock": "5"}
        early_ft = dict(live, status="played", actual_score="1-0", live_score=None, match_clock="FT")
        real_ft = dict(early_ft, actual_score="5-1")
        with self.assertLogs(app.logger, level="WARNING"):
            self.assertEqual(app.keep_progress([early_ft], [live]), [live])          # 1-0 after 4-1: wrong
        self.assertEqual(app.keep_progress([real_ft], [live]), [real_ft])            # 5-1: fine
        with self.assertLogs(app.logger, level="WARNING"):
            self.assertEqual(app.keep_progress([early_ft], [real_ft]), [real_ft])    # never back to 1-0

    def test_older_forebet_page_does_not_replace_newer_data(self):
        def page(*kickoffs):
            return {"status": "ok", "fixtures": [{"home_tag": "A", "away_tag": "B", "status": "played",
                                                  "actual_score": "1-0", "kickoff_utc": k} for k in kickoffs]}
        newer = {key: page("2026-10-23T10:14:00+00:00") for key in app.LEAGUES}
        with patch.object(app, "run_reader", return_value=newer):
            app.refresh_pages()
        older = dict(newer, volta6=page("2026-10-23T09:29:00+00:00"))
        upcoming = {"home_tag": "C", "away_tag": "D", "status": "upcoming", "kickoff_utc": "2026-10-23T10:40:00+00:00"}
        older["volta6"] = dict(older["volta6"], fixtures=older["volta6"]["fixtures"] + [upcoming])
        session = app.proxy_session_id
        with patch.object(app, "run_reader", return_value=older), patch.object(app, "demote_last_proxy") as demote, \
                self.assertLogs(app.logger, level="WARNING"):
            app.refresh_pages()
        demote.assert_called_once()
        self.assertNotEqual(app.proxy_session_id, session)
        with app.cache_lock:
            volta = app.cache["leagues"]["volta6"]["fixtures"]
            # Our newer result is kept, the older copy's games and its new upcoming game are taken.
            self.assertEqual(app.latest_result(app.cache["leagues"]["volta6"]), "2026-10-23T10:14:00+00:00")
            self.assertEqual([row["kickoff_utc"][11:16] for row in volta], ["09:29", "10:14", "10:40"])
            # Every league was read (the older copy was merged), so the feed is complete and fresh.
            self.assertEqual(app.cache["last_status"], "ok")
            self.assertFalse(app.cache["stale"])
        with patch.object(app, "run_reader", return_value={key: {"status": "error"} for key in app.LEAGUES}), \
                self.assertLogs(app.logger, level="WARNING"):
            app.refresh_pages()
        with app.cache_lock:
            self.assertEqual(app.cache["last_status"], "error")
            self.assertTrue(app.cache["stale"])

    def test_frozen_forebet_page_switches_proxy(self):
        now = datetime.now(timezone.utc)
        def page(minutes_ago):
            kickoff = (now - timedelta(minutes=minutes_ago)).isoformat()
            return {"status": "ok", "fixtures": [{"home_tag": "A", "away_tag": "B", "status": "live",
                                                  "live_score": "1-0", "kickoff_utc": kickoff}]}
        self.assertEqual(app.frozen_page(page(10)), "")
        self.assertTrue(app.frozen_page(page(90)))
        old_upcoming = page(90)
        old_upcoming["fixtures"][0].update(status="upcoming", live_score=None)
        self.assertTrue(app.frozen_page(old_upcoming))
        session = app.proxy_session_id
        with patch.object(app, "run_reader", return_value={key: page(90) for key in app.LEAGUES}), \
                patch.object(app, "demote_last_proxy") as demote, self.assertLogs(app.logger, level="WARNING"):
            app.refresh_pages()
        demote.assert_called_once()
        self.assertNotEqual(app.proxy_session_id, session)

    def test_hooks_run_right_after_each_read_even_if_one_fails(self):
        calls = []
        def broken():
            raise RuntimeError("boom")
        with patch.object(app, "refresh_pages", side_effect=lambda: calls.append("read")), \
                patch.object(app, "after_refresh", [broken, lambda: calls.append("alert")]), \
                self.assertLogs(app.logger, level="ERROR"):
            app.refresh_and_notify()
        self.assertEqual(calls, ["read", "alert"])

    def test_health_and_structured_fixtures(self):
        with app.app.test_client() as client, patch.object(app, "load_free_proxy_pool", return_value=["http://1.2.3.4:80"] * 5):
            self.assertEqual(client.get("/").get_json(), {
                "status": "ok", "last_fetch": None, "last_status": "error", "free_proxies_ready": 5,
                "esoccerbet": esoccerbet.STATUS, "league_age_seconds": {}, "telegram": telegram_bot.STATUS
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
            "source_kickoff_utc": "2026-10-18T16:55:00+00:00",
            "source_display_time": "18/10/2026 16:55",
            "source_utc_offset_minutes": 0,
            "kickoff_correction_minutes": 0,
            "kickoff_time_status": "source_unverified",
            "p1": 46, "px": 23, "p2": 31, "pred": "1",
            "correct_score": "4-3", "avg_goals": 6.11,
            "status": "upcoming", "actual_score": None,
            "live_score": None, "match_clock": None,
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

    def test_live_score_is_separate_from_final_and_predicted_scores(self):
        fields = {
            "home": "Arsenal (Walnut)", "away": "PSG (Scorpion)",
            "date": "19/10/2026 13:30", "probabilities": ["23", "40", "37"],
            "pred": "X", "score": "3 - 3", "avg_goals": "4.85",
            "status": "live", "actual_score": "", "live_score": "2 - 0",
            "match_clock": "7",
        }
        live = reader.parse_fixture(fields)
        self.assertEqual(live["status"], "live")
        self.assertEqual(live["live_score"], "2-0")
        self.assertEqual(live["match_clock"], "7")
        self.assertIsNone(live["actual_score"])
        self.assertEqual(live["correct_score"], "3-3")
        fields.update(status="played", actual_score="2 - 1", live_score="", match_clock="FT")
        finished = reader.parse_fixture(fields)
        self.assertEqual(finished["actual_score"], "2-1")
        self.assertIsNone(finished["live_score"])
        self.assertEqual(finished["match_clock"], "FT")

    def test_kickoff_conversion_respects_display_offset_and_day_rollover(self):
        self.assertEqual(reader.parse_kickoff("19/10/2026 23:45", -480).isoformat(),
                         "2026-10-20T07:45:00+00:00")
        # Etc/GMT+8 is UTC minus eight hours; fixed offset has no DST.
        self.assertEqual(reader.parse_kickoff("19/10/2026 07:00", -480).isoformat(),
                         "2026-10-19T15:00:00+00:00")
        self.assertEqual(reader.parse_kickoff("19/10/2026 00:15", 330).isoformat(),
                         "2026-10-18T18:45:00+00:00")

    def test_explicit_source_clock_correction_preserves_original(self):
        fields = {"home": "Arsenal (Walnut)", "away": "PSG (Scorpion)",
                  "date": "19/10/2026 06:00", "display_utc_offset_minutes": -480,
                  "probabilities": ["23", "40", "37"], "pred": "X", "score": "3 - 3",
                  "avg_goals": "4.85", "status": "upcoming", "actual_score": ""}
        with patch.dict("os.environ", {"FOREBET_KICKOFF_CORRECTION_MINUTES": "60"}):
            row = reader.parse_fixture(fields)
        self.assertEqual(row["source_kickoff_utc"], "2026-10-19T14:00:00+00:00")
        self.assertEqual(row["kickoff_utc"], "2026-10-19T15:00:00+00:00")
        self.assertEqual(row["kickoff_time_status"], "inferred_from_live_clocks")

    def test_live_clock_check_detects_hour_drift_and_api_marks_it_stale(self):
        now = datetime.now(timezone.utc)
        live = {"status": "live", "match_clock": "2",
                "kickoff_utc": (now-timedelta(minutes=64)).isoformat()}
        with self.assertLogs(reader.logger, level="WARNING"):
            check = reader.check_live_clock([live], now)
        self.assertEqual(check["status"], "mismatch")
        corrected = dict(live, kickoff_utc=(now-timedelta(minutes=4)).isoformat())
        self.assertEqual(reader.check_live_clock([corrected], now)["status"], "consistent")
        self.assertEqual(reader.check_live_clock([], now)["status"], "unverified_no_live_clock")
        frozen = {"home_tag": "Dreamer", "away_tag": "Groma", "status": "live", "match_clock": "4",
                  "kickoff_utc": (now - timedelta(minutes=18)).isoformat()}
        later = {"home_tag": "Groma", "away_tag": "bucho55", "status": "live", "match_clock": "3",
                 "kickoff_utc": (now - timedelta(minutes=9)).isoformat()}
        unfrozen = reader.check_live_clock([frozen, later], now, 6)
        self.assertEqual((unfrozen["live_fixtures_checked"], unfrozen["status"]), (1, "consistent"))
        awaiting_ft = {"status": "live", "match_clock": "12",
                       "kickoff_utc": (now-timedelta(minutes=25)).isoformat()}
        self.assertEqual(reader.check_live_clock([awaiting_ft], now, 12)["status"], "unverified_no_live_clock")
        self.assertEqual(reader.check_live_clock([dict(awaiting_ft, match_clock="11")], now, 12)["status"], "mismatch")
        with app.cache_lock:
            app.cache["stale"] = False
            for key in app.LEAGUES:
                app.cache["leagues"][key] = {"fixtures": [], "fetched_at": now.isoformat(),
                                            "clock_check": check}
        response = app.app.test_client().get("/predictions").get_json()
        self.assertTrue(response["stale"])
        self.assertEqual(response["league_clock_checks"]["gt12"]["status"], "mismatch")

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

    def test_visual_asset_blocking_keeps_cloudflare_and_scripts_available(self):
        context = MagicMock()
        session = reader.block_forebet_assets(context, MagicMock())
        methods = [call.args[0] for call in session.send.call_args_list]
        self.assertEqual(methods, ["Network.enable", "Network.setBlockedURLs"])
        patterns = session.send.call_args_list[1].args[1]["urls"]
        self.assertTrue(any("png" in pattern for pattern in patterns))
        self.assertTrue(all("forebet.com" in pattern for pattern in patterns))
        self.assertFalse(any("cloudflare.com" in pattern for pattern in patterns))

    def test_third_party_blocking_spares_cloudflare_and_forebet_scripts(self):
        session = reader.block_forebet_assets(MagicMock(), MagicMock(), third_party=True)
        patterns = session.send.call_args_list[1].args[1]["urls"]
        self.assertTrue(any("doubleclick.net" in pattern for pattern in patterns))
        self.assertFalse(any("cloudflare" in pattern for pattern in patterns))
        self.assertFalse(any(pattern.endswith(".js*") for pattern in patterns))

    def test_byte_meter_sums_per_page_and_host(self):
        context = MagicMock()
        meter = reader.ByteMeter(context, MagicMock())
        meter.response({"requestId": "1", "response": {"url": "https://www.forebet.com/en/esoccer"}})
        meter.finished({"requestId": "1", "encodedDataLength": 4096})
        meter.label = "esb8"
        meter.response({"requestId": "2", "response": {"url": "https://ads.doubleclick.net/x"}})
        meter.finished({"requestId": "2", "encodedDataLength": 2048})
        self.assertEqual(dict(meter.by_label), {"home": 4096, "esb8": 2048})
        self.assertEqual(meter.by_host["www.forebet.com"], 4096)
        with self.assertLogs(reader.logger, level="INFO") as logs:
            meter.log()
        self.assertIn("Network bytes this cycle: 6 KB", logs.output[0])

    def test_skip_homepage_goes_straight_to_league_pages(self):
        environment = {"SHIFTER_USER": "u", "SHIFTER_HOST": "h", "SHIFTER_PORT": "1", "SHIFTER_PASS": "p",
                       "SKIP_HOMEPAGE": "1", "BROWSER_PROFILE_DIR": "/tmp/forebet-test-profile"}
        playwright = MagicMock()
        with patch.dict(reader.os.environ, environment), \
                patch.object(reader, "sync_playwright") as launcher, \
                patch.object(reader, "visit") as visit, \
                patch.object(reader, "extract_fixtures", return_value=[]):
            launcher.return_value.__enter__.return_value = playwright
            reader.read_pages()
        urls = [call.args[1].split("?t=")[0] for call in visit.call_args_list]
        self.assertEqual(urls, list(reader.LEAGUES.values()))

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
