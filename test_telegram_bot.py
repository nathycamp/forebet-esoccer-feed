import unittest
from unittest.mock import patch

import app
import telegram_bot
import test_queries


class FormattingTests(unittest.TestCase):
    def test_quarter_kelly_capped_at_five_percent(self):
        self.assertEqual(telegram_bot.kelly_stake(.70, 1.55, 1000), 38.64)
        self.assertEqual(telegram_bot.kelly_stake(.90, 3.0, 1000), 50.)
        self.assertEqual(telegram_bot.kelly_stake(.50, 1.80, 1000), 0.)
        self.assertEqual(telegram_bot.kelly_stake(.70, 1.55, None), 0.)

    def test_playable_text_skips_near_certain_low_odds_lines(self):
        options = [{"selection": "over 1.5", "probability": .99, "min_odds": 1.01},
                   {"selection": "over 4.5", "probability": .73, "min_odds": 1.37}]
        self.assertEqual(telegram_bot.playable_text(options), "o4.5 >1.37 (73%)")
        self.assertEqual(telegram_bot.playable_text([]), "none")

    def test_odds_text_is_read_in_any_common_form(self):
        self.assertEqual(telegram_bot.parse_odds_text("o4.5 1.55 u5.5:1.70 Over 6.5 @2.1"),
                         "over4.5:1.55,under5.5:1.70,over6.5:2.1")
        self.assertEqual(telegram_bot.parse_odds_text("hello"), "")


class BotTests(unittest.TestCase):
    def setUp(self):
        test_queries.PicksTests.setUp(self)  # same sample feed as the picks tests
        self.bot = telegram_bot.PickBot(app.app.test_client(), "token", "42", bank=1000, min_stake=10)
        self.sent = []

        def send(text, reply_to=None):
            self.sent.append(text)
            return len(self.sent)
        self.bot.send = send

    def message(self, text, chat=42, reply_to=None):
        message = {"chat": {"id": chat}, "text": text, "message_id": 99}
        if reply_to:
            message["reply_to_message"] = {"message_id": reply_to}
        return message

    def test_alerts_play_picks_once(self):
        with patch.object(app, "TOP_PICK_MIN_PROBABILITY", 0.), patch.object(app, "play_advice", return_value="play"):
            self.assertEqual(self.bot.check_alerts(), 2)  # top 2 picks
            self.assertEqual(self.bot.check_alerts(), 0)
        self.assertIn("PLAY", self.sent[0])
        self.assertIn("Alice v Bob", self.sent[0])
        self.assertIn("Playable (60%+", self.sent[0])
        self.assertEqual(self.bot.alert_games[1], ("esb8", "Alice", "Bob"))

    def test_skip_picks_are_not_alerted(self):
        with patch.object(app, "play_advice", return_value="skip (below 60%)"):
            self.assertEqual(self.bot.check_alerts(), 0)

    def test_reply_with_odds_gives_advice_and_stake(self):
        self.bot.alert_games[7] = ("esb8", "Alice", "Bob")
        answer = self.bot.handle(self.message("o4.5 9.0 u4.5 9.0 o5.5 1.01 u5.5 9.0", reply_to=7))
        self.assertIn("Bet under 5.5 @ 9.0", answer)
        self.assertIn("Alice v Bob", answer)
        self.assertIn("Bet ", answer)
        self.assertIn("stake", answer)
        stingy = self.bot.handle(self.message("o4.5 1.01 u4.5 1.01", reply_to=7))
        self.assertIn("No bet", stingy)

    def test_commands_and_other_chats(self):
        self.assertIsNone(self.bot.handle(self.message("/bank 5000", chat=7)))
        self.assertIn("5000", self.bot.handle(self.message("/bank 5,000")))
        self.assertEqual(self.bot.bank, 5000)
        self.assertIn("4.5,5.5", self.bot.handle(self.message("/lines 4.5,5.5")))
        self.assertIn("x.5", self.bot.handle(self.message("/lines 5")))
        self.assertIn("best line", self.bot.handle(self.message("/lines off")))
        self.assertIn("Reply to an alert", self.bot.handle(self.message("o4.5 1.8")))
        self.assertIn("/bank", self.bot.handle(self.message("/help")))
        self.assertIsNone(self.bot.handle(self.message("thanks")))


if __name__ == "__main__":
    unittest.main()
