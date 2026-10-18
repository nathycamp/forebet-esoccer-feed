"""Small HTTP probe for Forebet connectivity from the deployment network."""

import json
import os
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


def probe_forebet():
    request = Request(
        FOREBET_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
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

    html = body.decode("utf-8", errors="replace").lower()
    challenged = any(marker in html for marker in CHALLENGE_MARKERS)
    clean = status == 200 and not challenged
    return {
        "reachable": clean,
        "http_status": status,
        "challenge_detected": challenged,
        "content_length": len(body),
        "verdict": "CLEAN" if clean else "CHALLENGED",
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.respond(200, {"ok": True})
        elif path == "/test":
            self.respond(200, probe_forebet())
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
