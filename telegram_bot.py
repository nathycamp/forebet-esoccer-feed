"""Telegram pick assistant: alerts for 'play' picks and odds-based advice on reply.

Runs inside the feed service. It only reads the feed's own /picks and
/picks/choose (through the Flask test client, no network hop) and talks to the
Telegram Bot API. It never logs in to or bets on any bookmaker.

Settings (Railway variables):
  TELEGRAM_BOT_TOKEN  bot token from @BotFather (bot is off without it)
  TELEGRAM_CHAT_ID    chat(s) that get alerts and may reply, comma-separated
                      (a private chat id and/or a group id such as -1001234567890)
  TELEGRAM_ADMIN_ID   the only user who may change /bank and /lines
                      (default: the first private chat id)
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
# Shown on the feed's health page so a silent bot is visible from outside.
STATUS = {"running": False, "chats": 0, "last_poll": None, "last_error": None,
          "alerts_sent": 0, "results_sent": 0}

WAT = timezone(timedelta(hours=1))
MIN_PROBABILITY = .60
KELLY_FRACTION = .25
MAX_STAKE_SHARE = .05
ALERT_MIN_MINUTES = 1  # picks often turn 'play' only 1-3 minutes before kickoff
ALERT_CANDIDATES = 10  # every 'play' pick in the window, not just the top 2
RESULT_WAIT_HOURS = 2
ALERT_MAX_MINUTES = 20
SELECTION_RE = re.compile(r"\b(over|under|o|u)\s*(\d+\.5)\s*[:@= ]\s*(\d+(?:\.\d+)?)", re.I)
LEAGUE_NAMES = {"esb8": "eSoccer Battle (8 min)", "volta6": "Volta (6 min)",
                "gt12": "GT League (12 min)", "h2hgg": "H2H GG (8 min)"}


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


def bet_name(selection):
    """'under 5.5' -> 'UNDER 5.5 goals'; other markets are shown as they are."""
    parts = selection.split()
    if len(parts) == 2 and parts[0] in ("over", "under"):
        return f"{parts[0].upper()} {parts[1]} goals"
    return selection


def other_options(options, best, limit=3):
    """Other 60%+ options worth a look (skipping near-certain, tiny-price ones)."""
    useful = [option for option in options or []
              if option["selection"] != best and option["min_odds"] >= 1.15][:limit]
    return "\n".join(f"• {bet_name(option['selection'])} — {option['probability']:.0%} — odds above {option['min_odds']}"
                     for option in useful)


def alert_text(pick):
    others = other_options(pick.get("playable_options"), pick["selection"])
    return (
        f"⚽ <b>BET ALERT</b> — {LEAGUE_NAMES.get(pick['league'], pick['league'])}\n"
        f"🕐 Kickoff <b>{wat(pick['kickoff_utc'])}</b> (Lagos time)\n"
        f"👥 <b>{pick['home_tag']} vs {pick['away_tag']}</b>\n\n"
        f"✅ Best bet: <b>{bet_name(pick['selection'])}</b>\n"
        f"📊 Chance: <b>{pick['probability']:.0%}</b>\n"
        f"💰 Only bet if odds are <b>above {pick['fair_odds']}</b>\n"
        + (f"\nOther good options:\n{others}\n" if others else "")
        + "\n↩️ Reply with the odds you see to check, e.g. <code>u5.5 1.50</code>"
    )


def result_text(selection, score, home, away):
    """'✅ WON — UNDER 5.5 goals · final 2-1' for a finished alerted game (None if not over/under)."""
    parts = selection.split()
    if len(parts) != 2 or parts[0] not in ("over", "under"):
        return None
    side, line = parts
    goals = sum(int(part) for part in score.split("-"))
    won = goals > float(line) if side == "over" else goals < float(line)
    return (f"{'✅ <b>WON</b>' if won else '❌ <b>LOST</b>'} — {bet_name(selection)}\n"
            f"👥 {home} vs {away} · final <b>{score}</b> ({goals} goals)")


def stake_text(stake, bank, min_stake):
    if not bank:
        return "💵 Stake: up to 5% of your bankroll"
    if stake < min_stake:
        return f"💵 Edge too small for a {min_stake:g} stake — skip"
    return f"💵 Stake: <b>{stake:g}</b> ({stake / bank:.1%} of {bank:g} bankroll)"


def advice_text(choice, bank, min_stake):
    """Choose the best value option at the league's floor from a /picks/choose result."""
    header = (f"👥 <b>{choice['home_tag']} vs {choice['away_tag']}</b> — "
              f"kickoff {wat(choice['kickoff_utc'])}\n\n")
    if choice.get("best") is None and not choice.get("options"):
        return header + f"⛔ <b>NO BET</b> — {choice.get('advice')}"
    floor = choice.get("min_probability", MIN_PROBABILITY)
    playable = [option for option in choice.get("options", [])
                if option.get("value", 0) > 0 and option["probability"] >= floor]
    checked = []
    for option in choice.get("options", []):
        name = f"{bet_name(option['selection'])} @ {option.get('odds', '-')}"
        if option in playable:
            checked.append(f"✅ {name} — good price")
        elif option["probability"] < floor:
            checked.append(f"❌ {name} — chance only {option['probability']:.0%}")
        else:
            checked.append(f"❌ {name} — price too low (needs above {option['fair_odds']})")
    checked_text = "\nChecked:\n" + "\n".join(checked)
    status = choice.get("call_status", "ok")
    if status != "ok" and not status.startswith("no call: too close"):
        return header + f"⛔ <b>NO BET</b> — {status}\n" + checked_text
    if choice.get("advice", "").startswith("wait"):
        return header + "⏳ <b>WAIT</b> — a player's previous game isn't at half time yet\n" + checked_text
    if not playable:
        return header + "⛔ <b>NO BET</b> — no price is high enough\n" + checked_text
    best = max(playable, key=lambda option: option["value"])
    return (header
            + f"✅ <b>BET: {bet_name(best['selection'])} @ {best['odds']}</b>\n"
            + f"📊 Chance {best['probability']:.0%} · expected profit {best['value']:+.0%}\n"
            + stake_text(kelly_stake(best["probability"], best["odds"], bank), bank, min_stake) + "\n"
            + checked_text)


def board_text(board, limit=8):
    """The /top board: every game ready or waiting, best edge first, plus a count of skips."""
    ready = [row for row in board if row.get("selection") and not row["advice"].startswith("skip")][:limit]
    skipped = sum(row["advice"].startswith("skip") for row in board)
    if not ready:
        return f"No game to bet right now ({skipped} skipped)." if board else "No games in the next 20 minutes."
    blocks = []
    for row in ready:
        state = "✅ BET NOW" if row["advice"] == "play" else "⏳ wait (previous game not at half time)"
        edge = f" · edge {row['edge'] * 100:+.0f}" if row.get("edge") is not None else ""
        blocks.append(f"{state}\n⚽ {LEAGUE_NAMES.get(row['league'], row['league'])} — {wat(row['kickoff_utc'])}\n"
                      f"👥 <b>{row['home_tag']} vs {row['away_tag']}</b>\n"
                      f"{bet_name(row['selection'])} — {row['probability']:.0%}{edge}"
                      + (f" — odds above {row['fair_odds']}" if row.get("fair_odds") else ""))
    footer = f"\n\n({skipped} other game{'s' if skipped != 1 else ''} skipped: too close, new players or low chance)" if skipped else ""
    return "📋 <b>Games in the next 20 min</b>\n\n" + "\n\n".join(blocks) + footer


HELP = ("I post a <b>BET ALERT</b> when a game is ready to bet, 2–20 min before kickoff.\n\n"
        "↩️ Reply to an alert with the odds you see, e.g. <code>u5.5 1.50 o3.5 1.55</code>, "
        "and I'll say which to bet, or that none is good enough.\n\n"
        "/top — every game in the next 20 min: bet now, wait or skipped\n"
        "/help — this message\n"
        "Owner only: /bank 5000 (bankroll for stakes), /lines 4.5,5.5 (/lines off)")


class PickBot:
    def __init__(self, client, token, chat_ids, bank=None, lines=None, min_stake=100., admin_id=None):
        self.client = client
        self.token = token
        self.chat_ids = [str(chat).strip() for chat in str(chat_ids).split(",") if str(chat).strip()]
        private = [chat for chat in self.chat_ids if not chat.startswith("-")]
        self.admin_id = str(admin_id) if admin_id else (private[0] if private else None)
        self.bank = bank
        self.lines = lines
        self.min_stake = min_stake
        self.alerted = set()
        self.alert_games = {}  # (chat id, message id) -> (league, home, away)
        self.last_game = {}    # chat id -> latest alerted game
        self.pending = {}      # alerted game -> {selection, messages} until its result is posted
        self.offset = None
        self.lock = threading.Lock()

    # --- Telegram API ---------------------------------------------------------
    def api(self, method, http_timeout=35, **params):
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        request = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=data)
        with urllib.request.urlopen(request, timeout=http_timeout) as response:
            return json.load(response)

    def send(self, text, chat_id, reply_to=None):
        result = self.api("sendMessage", http_timeout=15, chat_id=chat_id, text=text, parse_mode="HTML",
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
        """Alert each new 'play' pick kicking off in 1-20 minutes, to every chat, then post results."""
        now = now or datetime.now(timezone.utc)
        sent = 0
        for pick in self.top_picks(ALERT_CANDIDATES):
            kickoff = datetime.fromisoformat(pick["kickoff_utc"].replace("Z", "+00:00"))
            minutes = (kickoff - now).total_seconds() / 60
            key = (pick["league"], pick["home_tag"], pick["away_tag"], pick["kickoff_utc"])
            if pick.get("advice") != "play" or not ALERT_MIN_MINUTES <= minutes <= ALERT_MAX_MINUTES:
                continue
            with self.lock:
                if key in self.alerted:
                    continue
                self.alerted.add(key)
            game = (pick["league"], pick["home_tag"], pick["away_tag"])
            messages = []
            for chat in self.chat_ids:
                try:
                    message_id = self.send(alert_text(pick), chat)
                except Exception:
                    logger.exception("Telegram alert to %s failed", chat)
                    continue
                messages.append((chat, message_id))
                with self.lock:
                    self.alert_games[(chat, message_id)] = game
                    self.last_game[chat] = game
            with self.lock:
                self.pending[key] = {"selection": pick["selection"], "messages": messages}
            STATUS["alerts_sent"] += 1
            logger.info("Telegram alert sent: %s %s v %s %s (%.0f%%), kickoff %s",
                        pick["league"], pick["home_tag"], pick["away_tag"], pick["selection"],
                        pick["probability"] * 100, pick["kickoff_utc"])
            sent += 1
        with self.lock:  # forget games that kicked off long ago
            cutoff = (now - timedelta(hours=RESULT_WAIT_HOURS)).isoformat()
            self.alerted = {key for key in self.alerted if key[3] >= cutoff}
        self.check_results(now)
        return sent

    def check_results(self, now=None):
        """Reply to each alert with WON / LOST once Forebet shows the final score."""
        now = now or datetime.now(timezone.utc)
        with self.lock:
            pending = dict(self.pending)
        for key, alert in pending.items():
            league, home, away, kickoff = key
            status, payload = self.get("/predictions", league=league, status="played", player=home, limit=20)
            rows = payload.get("leagues", {}).get(league, []) if status == 200 else []
            target = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            final = next((row for row in rows if row.get("home_tag") == home and row.get("away_tag") == away
                          and abs((datetime.fromisoformat(row["kickoff_utc"].replace("Z", "+00:00")) - target)
                                  .total_seconds()) <= 25 * 60 and row.get("actual_score")), None)
            if not final:
                if now - target > timedelta(hours=RESULT_WAIT_HOURS):
                    with self.lock:
                        self.pending.pop(key, None)
                continue
            text = result_text(alert["selection"], final["actual_score"], home, away)
            for chat, message_id in alert["messages"] if text else []:
                try:
                    self.send(text, chat, reply_to=message_id)
                except Exception:
                    logger.exception("Telegram result to %s failed", chat)
            STATUS["results_sent"] += 1
            logger.info("Telegram result sent: %s %s v %s %s -> %s", league, home, away, alert["selection"],
                        final["actual_score"])
            with self.lock:
                self.pending.pop(key, None)

    # --- commands -------------------------------------------------------------
    def handle(self, message):
        chat = str(message.get("chat", {}).get("id"))
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command == "/chatid":  # lets the owner add a new group: tells its id
            return f"This chat's id is <code>{chat}</code>"
        if chat not in self.chat_ids:
            return None
        if command in ("/start", "/help"):
            return HELP
        if command in ("/bank", "/lines") and str(message.get("from", {}).get("id")) != self.admin_id:
            return "Only the owner can change settings."
        if command == "/bank":
            try:
                self.bank = float(text.split()[1].replace(",", ""))
            except (IndexError, ValueError):
                return f"Bankroll is {self.bank or 'not set'}. Use /bank 5000"
            return f"Bankroll set to {self.bank:g}. Stakes are sized to the edge, at most 5% of it."
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
            params = {"lines": self.lines} if self.lines else {}
            status, payload = self.get("/picks", **params)
            if status != 200:
                return "The feed is unavailable right now; try again in a minute."
            return board_text(payload.get("board", []))
        odds = parse_odds_text(text)
        if not odds:
            return "Send odds like <code>u5.5 1.50</code>, or /help" if command else None
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        game = self.alert_games.get((chat, reply_to)) or self.last_game.get(chat)
        if not game:
            return "Reply to a BET ALERT so I know which game the odds are for."
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
                self.send(answer, message["chat"]["id"], reply_to=message.get("message_id"))

    def poll_forever(self):
        while True:
            try:
                self.poll_once()
                STATUS["last_poll"] = datetime.now(timezone.utc).isoformat()
            except Exception as exc:
                # 409 means another instance (e.g. during a redeploy) is polling.
                STATUS["last_error"] = f"{datetime.now(timezone.utc).isoformat()} {exc}"[:200]
                logger.warning("Telegram polling paused: %s", exc)
                time.sleep(10)


def start(client, scheduler=None):
    """Start command polling when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set; returns the bot."""
    token, chat_ids = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_ids:
        logger.info("Telegram bot off: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return None
    bank = os.environ.get("TELEGRAM_BANK")
    bot = PickBot(client, token, chat_ids, bank=float(bank) if bank else None,
                  lines=os.environ.get("TELEGRAM_LINES") or None,
                  min_stake=float(os.environ.get("TELEGRAM_MIN_STAKE", "100")),
                  admin_id=os.environ.get("TELEGRAM_ADMIN_ID"))

    # Alerts run right after each feed read (app.after_refresh), so a pick that turns
    # "play" is sent within seconds of the read that shows it.
    threading.Thread(target=bot.poll_forever, name="telegram-poll", daemon=True).start()
    STATUS.update(running=True, chats=len(bot.chat_ids))
    logger.info("Telegram bot on for chats %s", ", ".join(bot.chat_ids))
    return bot
