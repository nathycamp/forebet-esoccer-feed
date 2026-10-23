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

    def test_other_options_skip_the_best_and_near_certain_lines(self):
        options = [{"selection": "over 1.5", "probability": .99, "min_odds": 1.01},
                   {"selection": "over 4.5", "probability": .73, "min_odds": 1.37},
                   {"selection": "under 7.5", "probability": .71, "min_odds": 1.41}]
        self.assertEqual(telegram_bot.other_options(options, "over 4.5"),
                         "• UNDER 7.5 goals — 71% — odds above 1.41")
        self.assertEqual(telegram_bot.other_options([], "over 4.5"), "")

    def test_board_text_shows_ready_and_waiting_games(self):
        board = [{"league": "gt12", "kickoff_utc": "2026-10-23T19:15:00+00:00", "home_tag": "David", "away_tag": "Eros",
                  "selection": "under 5.5", "probability": .77, "fair_odds": 1.3, "edge": .08, "advice": "play"},
                 {"league": "esb8", "kickoff_utc": "2026-10-23T19:31:00+00:00", "home_tag": "Sena", "away_tag": "Mani",
                  "selection": "over 4.5", "probability": .65, "fair_odds": 1.54, "edge": .04,
                  "advice": "wait (previous game pending)"},
                 {"league": "h2hgg", "kickoff_utc": "2026-10-23T19:16:00+00:00", "home_tag": "A", "away_tag": "B",
                  "selection": None, "probability": None, "fair_odds": None, "edge": None,
                  "advice": "skip (no call: new players)"}]
        text = telegram_bot.board_text(board)
        self.assertIn("BET NOW", text)
        self.assertIn("David vs Eros", text)
        self.assertIn("edge +8", text)
        self.assertIn("wait", text)
        self.assertIn("1 other game skipped", text)
        self.assertIn("No game to bet", telegram_bot.board_text(board[2:]))
        self.assertIn("No games", telegram_bot.board_text([]))

    def test_odds_text_is_read_in_any_common_form(self):
        self.assertEqual(telegram_bot.parse_odds_text("o4.5 1.55 u5.5:1.70 Over 6.5 @2.1"),
                         "over4.5:1.55,under5.5:1.70,over6.5:2.1")
        self.assertEqual(telegram_bot.parse_odds_text("hello"), "")


class BotTests(unittest.TestCase):
    def setUp(self):
        test_queries.PicksTests.setUp(self)  # same sample feed as the picks tests
        self.bot = telegram_bot.PickBot(app.app.test_client(), "token", "42,-100", bank=1000, min_stake=10)
        self.sent = []

        def send(text, chat_id, reply_to=None):
            self.sent.append((str(chat_id), text))
            return len(self.sent)
        self.bot.send = send

    def message(self, text, chat=42, sender=42, reply_to=None):
        message = {"chat": {"id": chat}, "from": {"id": sender}, "text": text, "message_id": 99}
        if reply_to:
            message["reply_to_message"] = {"message_id": reply_to}
        return message

    def test_alerts_go_to_every_chat_once(self):
        with patch.object(app, "TOP_PICK_MIN_PROBABILITY", 0.), patch.object(app, "play_advice", return_value="play"):
            self.assertEqual(self.bot.check_alerts(), 2)  # top 2 picks
            self.assertEqual(self.bot.check_alerts(), 0)
        self.assertEqual([chat for chat, _ in self.sent], ["42", "-100", "42", "-100"])
        text = self.sent[0][1]
        self.assertIn("BET ALERT", text)
        self.assertIn("Alice vs Bob", text)
        self.assertIn("Lagos time", text)
        self.assertIn("Only bet if odds are", text)
        self.assertEqual(self.bot.alert_games[("-100", 2)], ("esb8", "Alice", "Bob"))

    def test_skip_picks_are_not_alerted(self):
        with patch.object(app, "play_advice", return_value="skip (below 60%)"):
            self.assertEqual(self.bot.check_alerts(), 0)

    def test_reply_with_odds_in_a_group_gives_a_clear_answer(self):
        self.bot.alert_games[("-100", 7)] = ("esb8", "Alice", "Bob")
        answer = self.bot.handle(self.message("o4.5 9.0 u4.5 9.0 o5.5 1.01 u5.5 9.0", chat=-100, sender=5, reply_to=7))
        self.assertIn("Alice vs Bob", answer)
        self.assertIn("BET: UNDER 5.5 goals @ 9.0", answer)
        self.assertIn("Stake", answer)
        self.assertIn("chance only", answer)
        stingy = self.bot.handle(self.message("u5.5 1.01", chat=-100, reply_to=7))
        self.assertIn("NO BET", stingy)
        self.assertIn("price too low", stingy)

    def test_only_the_owner_changes_settings(self):
        self.assertIn("Only the owner", self.bot.handle(self.message("/bank 5000", chat=-100, sender=5)))
        self.assertIn("5000", self.bot.handle(self.message("/bank 5,000", chat=-100, sender=42)))
        self.assertEqual(self.bot.bank, 5000)

    def test_commands_and_other_chats(self):
        self.assertIsNone(self.bot.handle(self.message("/bank 5000", chat=7)))
        self.assertIn("<code>7</code>", self.bot.handle(self.message("/chatid", chat=7)))
        self.assertIn("4.5,5.5", self.bot.handle(self.message("/lines 4.5,5.5")))
        self.assertIn("x.5", self.bot.handle(self.message("/lines 5")))
        self.assertIn("best line", self.bot.handle(self.message("/lines off")))
        self.assertIn("Reply to a BET ALERT", self.bot.handle(self.message("o4.5 1.8")))
        self.assertIn("/top", self.bot.handle(self.message("/help")))
        self.assertIn("Games in the next 20 min", self.bot.handle(self.message("/top")))
        self.assertIsNone(self.bot.handle(self.message("thanks")))


if __name__ == "__main__":
    unittest.main()
