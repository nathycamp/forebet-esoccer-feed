import unittest
from unittest.mock import MagicMock, patch

import app
import ingest
import reader


class ReaderServiceTests(unittest.TestCase):
    def setUp(self):
        with app.cache_lock:
            app.cache.update({
                "last_fetch": None,
                "last_status": "error",
                "fetched_at": None,
                "stale": True,
                "pages": {},
            })

    def test_health_and_preview(self):
        with app.app.test_client() as client:
            self.assertEqual(client.get("/").get_json(), {
                "status": "ok", "last_fetch": None, "last_status": "error"
            })
        with patch.object(app, "read_pages", return_value={
            key: {"html": "<body>hello</body>", "text": "hello" * 120, "status": "ok"}
            for key in app.LEAGUES
        }), patch.object(app, "post_pages", return_value=True):
            app.refresh_pages()
        with app.app.test_client() as client:
            data = client.get("/predictions").get_json()
        self.assertFalse(data["stale"])
        self.assertEqual(data["pages"]["esb8"]["text_length"], 600)
        self.assertEqual(len(data["pages"]["esb8"]["text"]), 503)

    def test_failed_league_retains_last_good_text(self):
        with app.cache_lock:
            app.cache["pages"]["esb8"] = {"html": "old", "text": "old text", "status": "ok"}
        with patch.object(app, "read_pages", return_value={
            "esb8": {"status": "error", "error": "challenged"},
            "volta6": {"html": "new", "text": "new text", "status": "ok"},
        }), patch.object(app, "post_pages", return_value=True):
            app.refresh_pages()
        with app.app.test_client() as client:
            data = client.get("/predictions").get_json()
        self.assertTrue(data["stale"])
        self.assertEqual(data["pages"]["esb8"]["text"], "old text")

    @patch.object(ingest.requests, "post")
    def test_ingest_sends_full_text(self, post):
        self.assertTrue(ingest.post_pages({"esb8": {
            "status": "ok", "text": "full text", "html": "<html>full text</html>"
        }}, "2026-10-18T17:00:00Z"))
        self.assertEqual(post.call_args.kwargs["json"]["pages"], {"esb8": "full text"})
        self.assertEqual(post.call_args.kwargs["timeout"], 30)

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
