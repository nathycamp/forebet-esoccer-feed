"""Telegram pick assistant: alerts for 'play' picks and odds-based advice on reply.

Runs inside the feed service. It only reads the feed's own /picks and
/picks/choose (through the Flask test client, no network hop) and talks to the
Telegram Bot API. It never logs in to or bets on any bookmaker.

Settings (Railway variables):
  TELEGRAM_BOT_TOKEN  bot token from @BotFather (bot is off without it)
  TELEGRAM_CHAT_ID    the only chat the bot alerts and answers
  TELEGRAM_BANK       optional starting bankroll for stake sizing (/bank changes it)
  TELEGRAM_LINES      optional default lines for alerts, e.g. "4.5,5.5" (/lines changes it)
  TELEGRAM_MIN_STAKE  smallest stake worth suggesting (default 100)
"""

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

WAT = timezone(timedelta(hours=1))
MIN_PROBABILITY = .60
KELLY_FRACTION = .25
MAX_STAKE_SHARE = .05
ALERT_MIN_MINUTES = 2
ALERT_MAX_MINUTES = 20
SELECTION_RE = re.compile(r"\b(over|under|o|u)\s*(\d+\.5)\s*[:@= ]\s*(\d+(?:\.\d+)?)", re.I)


def kelly_stake(probability, odds, bank, fraction=KELLY_FRACTION, cap=MAX_STAKE_SHARE):
    """Quarter-Kelly stake, capped at 5% of the bank; 0 when there is no edge."""
    edge = odds - 1
    if edge <= 0 or not bank:
        return 0.
    kelly = (edge * probability - (1 - probability)) / edge
    return round(bank * min(max(kelly * fraction, 0.), cap), 2)


def parse_odds_text(text):
    """'o4.5 1.55 u5.5:1.70' -> 'over4.5:1.55,under5.5:1.7' (the /picks/choose format)."""
    parts = []
    for side, line, price in SELECTION_RE.findall(text or ""):
        side = "over" if side.lower().startswith("o") else "under"
        parts.append(f"{side}{line}:{price}")
    return ",".join(parts)


def wat(iso):
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return moment.astimezone(WAT).strftime("%H:%M")


def format_lines(goal_lines, around):
    """Compact chances for the lines near the pick: '4.5 o73% u27% · 5.5 o59% u41%'."""
    wanted = [str(around + step) for step in (-1, 0, 1, 2)]
    shown = [f"{line} o{goal_lines[line]['over']:.0%} u{goal_lines[line]['under']:.0%}"
             for line in wanted if line in (goal_lines or {})]
    return " · ".join(shown)


def playable_text(options, limit=5):
    """'o4.5 >1.37 (73%) · u7.5 >1.41 (71%)' for the most useful 60%+ options."""
    useful = [option for option in options or [] if option["min_odds"] >= 1.15][:limit]
    return " · ".join(f"{option['selection'][0]}{option['selection'].split()[1]} >{option['min_odds']} "
                      f"({option['probability']:.0%})" for option in useful) or "none"


def alert_text(pick):
    line = float(pick["selection"].split()[-1])
    return (
        f"⚽ <b>PLAY</b> · {pick['league']} · kickoff {wat(pick['kickoff_utc'])} WAT\n"
        f"<b>{pick['home_tag']} v {pick['away_tag']}</b>\n"
        f"<b>{pick['selection'].capitalize()}</b> — {pick['probability']:.0%} "
        f"(league {pick['league_rate']:.0%}), expected goals {pick['expected_goals']}\n"
        f"Bet only if odds are above <b>{pick['fair_odds']}</b>\n"
        f"Playable (60%+, bet if odds above): {playable_text(pick.get('playable_options'))}\n"
        f"Lines: {format_lines(pick.get('goal_lines'), line)}\n"
        f"↩️ Reply with your odds, e.g. <code>o4.5 1.55 u5.5 1.70</code>"
    )


def advice_text(choice, bank, min_stake):
    """Choose the best value option at 60%+ from a /picks/choose result."""
    header = f"<b>{choice['home_tag']} v {choice['away_tag']}</b> · kickoff {wat(choice['kickoff_utc'])} WAT\n"
    if choice.get("best") is None and not choice.get("options"):
        return header + f"⛔ No bet: {choice.get('advice')}"
    playable = [option for option in choice.get("options", [])
                if option.get("value", 0) > 0 and option["probability"] >= MIN_PROBABILITY]
    rows = "\n".join(
        f"{'✅' if option in playable else '·'} {option['selection']} @ {option.get('odds', '-')}: "
        f"{option['probability']:.0%}, fair {option['fair_odds']}, value {option.get('value', 0):+.0%}"
        for option in choice.get("options", []))
    if choice.get("call_status", "ok") != "ok" and not choice["call_status"].startswith("no call: too close"):
        return header + rows + f"\n⛔ No bet: {choice['call_status']}"
    if not playable:
        return header + rows + "\n⛔ <b>No bet</b>: no option is both 60%+ and above its fair odds"
    best = max(playable, key=lambda option: option["value"])
    stake = kelly_stake(best["probability"], best["odds"], bank)
    if not bank:
        stake_text = "set /bank to get a stake"
    elif stake < min_stake:
        stake_text = f"edge too small for the {min_stake:g} minimum — skip"
    else:
        stake_text = f"stake <b>{stake:g}</b> ({stake / bank:.1%} of {bank:g})"
    return (header + rows + f"\n👉 <b>Bet {best['selection']} @ {best['odds']}</b> — "
            f"{best['probability']:.0%}, expected return {best['value']:+.0%}\n{stake_text}")


class PickBot:
    def __init__(self, client, token, chat_id, bank=None, lines=None, min_stake=100.):
        self.client = client
        self.token = token
        self.chat_id = str(chat_id)
        self.bank = bank
        self.lines = lines
        self.min_stake = min_stake
        self.alerted = set()
        self.alert_games = {}  # Telegram message id -> (league, home, away)
        self.last_game = None
        self.offset = None
        self.lock = threading.Lock()

    # --- Telegram API ---------------------------------------------------------
    def api(self, method, http_timeout=35, **params):
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        request = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=data)
        with urllib.request.urlopen(request, timeout=http_timeout) as response:
            return json.load(response)

    def send(self, text, reply_to=None):
        result = self.api("sendMessage", http_timeout=15, chat_id=self.chat_id, text=text, parse_mode="HTML",
                          disable_web_page_preview="true", reply_to_message_id=reply_to)
        return result.get("result", {}).get("message_id")

    # --- feed -----------------------------------------------------------------
    def get(self, path, **params):
        response = self.client.get(path + "?" + urllib.parse.urlencode(params))
        return response.status_code, response.get_json(silent=True) or {}

    def top_picks(self, count=2):
        params = {"top": count}
        if self.lines:
            params["lines"] = self.lines
        status, payload = self.get("/picks", **params)
        return payload.get("top_picks", []) if status == 200 else []

    # --- alerts ---------------------------------------------------------------
    def check_alerts(self, now=None):
        """Alert each new 'play' top pick kicking off in 2-20 minutes."""
        now = now or datetime.now(timezone.utc)
        sent = 0
        for pick in self.top_picks():
            kickoff = datetime.fromisoformat(pick["kickoff_utc"].replace("Z", "+00:00"))
            minutes = (kickoff - now).total_seconds() / 60
            key = (pick["league"], pick["home_tag"], pick["away_tag"], pick["kickoff_utc"])
            if pick.get("advice") != "play" or not ALERT_MIN_MINUTES <= minutes <= ALERT_MAX_MINUTES:
                continue
            with self.lock:
                if key in self.alerted:
                    continue
                self.alerted.add(key)
            message_id = self.send(alert_text(pick))
            game = (pick["league"], pick["home_tag"], pick["away_tag"])
            with self.lock:
                self.alert_games[message_id] = game
                self.last_game = game
            sent += 1
        with self.lock:  # forget games that kicked off long ago
            cutoff = (now - timedelta(hours=2)).isoformat()
            self.alerted = {key for key in self.alerted if key[3] >= cutoff}
        return sent

    # --- commands -------------------------------------------------------------
    def handle(self, message):
        if str(message.get("chat", {}).get("id")) != self.chat_id:
            return None
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/help"):
            return ("I alert you when the feed has a <b>play</b> pick, 2–20 min before kickoff.\n"
                    "↩️ Reply to an alert with your odds: <code>o4.5 1.55 u5.5 1.70</code>\n"
                    "/odds o4.5 1.55 … — odds for the latest alerted game\n"
                    "/bank 5000 — bankroll for stake sizing\n"
                    "/lines 4.5,5.5 — only alert on these lines (/lines off for any)\n"
                    "/top — current top 2 picks")
        if command == "/bank":
            try:
                self.bank = float(text.split()[1].replace(",", ""))
            except (IndexError, ValueError):
                return f"Bank is {self.bank or 'not set'}. Use /bank 5000"
            return f"Bank set to {self.bank:g}. Stakes are quarter-Kelly, at most 5% of the bank."
        if command == "/lines":
            value = text[len("/lines"):].strip().replace(" ", "")
            if value.lower() in ("", "off", "any"):
                self.lines = None
                return "Alerts use each league's best line."
            status, _ = self.get("/picks", lines=value)
            if status != 200:
                return "Lines must be x.5 values like /lines 4.5,5.5"
            self.lines = value
            return f"Alerts now pick from lines {value}."
        if command == "/top":
            picks = self.top_picks()
            if not picks:
                return "No top pick right now."
            return "\n\n".join(
                f"{pick['league']} {wat(pick['kickoff_utc'])} WAT · {pick['home_tag']} v {pick['away_tag']}\n"
                f"{pick['selection']} {pick['probability']:.0%} (fair {pick['fair_odds']}) · {pick['advice']}"
                for pick in picks)
        odds = parse_odds_text(text)
        if not odds:
            return "Send odds like <code>o4.5 1.55 u5.5 1.70</code>, or /help" if command else None
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        game = self.alert_games.get(reply_to) or self.last_game
        if not game:
            return "Reply to an alert so I know which game the odds are for."
        league, home, away = game
        status, choice = self.get("/picks/choose", league=league, home=home, away=away, odds=odds)
        if status != 200:
            return f"⛔ {choice.get('error', 'no advice available')}"
        return advice_text(choice, self.bank, self.min_stake)

    def poll_once(self):
        # Long polling: Telegram holds the request up to 25 s until a message arrives.
        updates = self.api("getUpdates", http_timeout=35, offset=self.offset, timeout=25,
                           allowed_updates='["message"]')
        for update in updates.get("result", []):
            self.offset = update["update_id"] + 1
            message = update.get("message")
            if not message:
                continue
            try:
                answer = self.handle(message)
            except Exception:
                logger.exception("Telegram command failed")
                answer = "Something went wrong reading the feed; try again in a minute."
            if answer:
                self.send(answer, reply_to=message.get("message_id"))

    def poll_forever(self):
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                # 409 means another instance (e.g. during a redeploy) is polling.
                logger.warning("Telegram polling paused: %s", exc)
                time.sleep(10)


def start(client, scheduler=None):
    """Start command polling when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set; returns the bot."""
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.info("Telegram bot off: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return None
    bank = os.environ.get("TELEGRAM_BANK")
    bot = PickBot(client, token, chat_id, bank=float(bank) if bank else None,
                  lines=os.environ.get("TELEGRAM_LINES") or None,
                  min_stake=float(os.environ.get("TELEGRAM_MIN_STAKE", "100")))

    # Alerts run right after each feed read (app.after_refresh), so a pick that turns
    # "play" is sent within seconds of the read that shows it.
    threading.Thread(target=bot.poll_forever, name="telegram-poll", daemon=True).start()
    logger.info("Telegram bot on for chat %s", chat_id)
    return bot
