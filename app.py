"""Small HTTP feed for Forebet esoccer predictions."""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


FOREBET_URL = "https://www.forebet.com/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CHALLENGE_MARKERS = (
    "verify you are human",
    "challenges.cloudflare",
    "just a moment",
    "__cf_chl",
)
LEAGUES = {
    "esb8": "https://www.forebet.com/en/esoccer/esoccer-battle-8-mins-play",
    "volta6": "https://www.forebet.com/en/esoccer/esoccer-battle-volta-6-mins-play",
    "h2hgg": "https://www.forebet.com/en/esoccer/esoccer-h2h-gg-league-8-mins-play",
    "gt12": "https://www.forebet.com/en/esoccer/esoccer-liga-pro-12-mins-play",
}
CACHE_SECONDS = 300
_cache_lock = threading.Lock()
_cache_data = None
_cache_expires = 0.0


def fetch_page(url):
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def is_challenge(body):
    html = body.decode("utf-8", errors="replace").lower()
    return any(marker in html for marker in CHALLENGE_MARKERS)


def probe_forebet():
    try:
        status, body = fetch_page(FOREBET_URL)
    except (URLError, TimeoutError, OSError) as exc:
        print(f"Forebet request failed: {exc}", flush=True)
        return {
            "reachable": False,
            "http_status": None,
            "challenge_detected": False,
            "content_length": 0,
            "verdict": "CHALLENGED",
            "error": str(exc),
        }

    challenged = is_challenge(body)
    clean = status == 200 and not challenged
    return {
        "reachable": clean,
        "http_status": status,
        "challenge_detected": challenged,
        "content_length": len(body),
        "verdict": "CLEAN" if clean else "CHALLENGED",
    }


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def classes(self):
        return set(self.attrs.get("class", "").lower().split())

    def first_class(self, *names):
        wanted = set(names)
        return next((node for node in self.walk() if node.classes() & wanted), None)

    def text(self):
        return " ".join(
            part for child in self.children
            for part in ([child] if isinstance(child, str) else [child.text()])
            if part
        ).strip()


class TreeParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        value = " ".join(data.split())
        if value:
            self.stack[-1].children.append(value)


def parse_fixture(row):
    def field(*classes):
        node = row.first_class(*classes)
        return node.text() if node else ""

    home = field("hometeam")
    away = field("awayteam")
    date_text = field("date_bah")
    kickoff_match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})", date_text)
    if not (home and away and kickoff_match):
        return None
    try:
        day, month, year, hour, minute = map(int, kickoff_match.groups())
        kickoff = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None

    probability_text = field("fprc")
    probabilities = re.findall(r"\b\d{1,3}\b", probability_text)
    if len(probabilities) < 3:
        return None
    p1, px, p2 = map(int, probabilities[:3])
    if any(p > 100 for p in (p1, px, p2)):
        return None

    pred_match = re.search(r"\b[1X2]\b", field("forepr"), re.IGNORECASE)
    score_match = re.search(r"\b(\d+)\s*-\s*(\d+)\b", field("ex_sc"))
    if not (pred_match and score_match):
        return None

    avg_text = field("avg_sc", "avg_goals", "avggoals", "avg_g")
    if not avg_text:
        # Forebet places average goals after the correct score in each row.
        row_text = row.text()
        score_position = row_text.find(score_match.group(0))
        if score_position >= 0:
            avg_text = row_text[score_position + len(score_match.group(0)):]
    avg_match = re.search(r"\b\d+(?:\.\d+)\b", avg_text)
    if not avg_match:
        return None

    return {
        "home": home,
        "away": away,
        "kickoff_utc": kickoff.isoformat().replace("+00:00", "Z"),
        "p1": p1,
        "px": px,
        "p2": p2,
        "pred": pred_match.group(0).upper(),
        "pred_score": f"{score_match.group(1)}-{score_match.group(2)}",
        "avg_goals": float(avg_match.group(0)),
    }


def parse_predictions(html):
    parser = TreeParser()
    parser.feed(html)
    rows = [node for node in parser.root.walk() if "rcnt" in node.classes()]
    if not rows:
        rows = [node for node in parser.root.walk() if node.tag == "tr"]
    fixtures = []
    for row in rows:
        fixture = parse_fixture(row)
        if fixture:
            fixtures.append(fixture)
    return fixtures


def get_predictions():
    global _cache_data, _cache_expires
    with _cache_lock:
        if _cache_data is not None and time.monotonic() < _cache_expires:
            return _cache_data
        leagues = {}
        for key, url in LEAGUES.items():
            try:
                status, body = fetch_page(url)
                if is_challenge(body) or status in (403, 429, 503):
                    leagues[key] = {"error": "challenged"}
                    print(f"{key}: Forebet challenge (HTTP {status})", flush=True)
                elif status != 200:
                    leagues[key] = {"error": f"http_{status}"}
                    print(f"{key}: Forebet returned HTTP {status}", flush=True)
                else:
                    leagues[key] = parse_predictions(body.decode("utf-8", errors="replace"))
            except Exception as exc:
                leagues[key] = {"error": "fetch_failed"}
                print(f"{key}: {exc}", flush=True)
        _cache_data = {
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "leagues": leagues,
        }
        _cache_expires = time.monotonic() + CACHE_SECONDS
        return _cache_data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.respond(200, {"ok": True})
        elif path == "/test":
            self.respond(200, probe_forebet())
        elif path == "/predictions":
            self.respond(200, get_predictions())
        else:
            self.respond(404, {"error": "not found"})

    def respond(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Listening on 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
