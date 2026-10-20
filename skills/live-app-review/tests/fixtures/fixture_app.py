#!/usr/bin/env python3
"""Tiny fixture app for the live-app-review tests. Python 3.9+, standard library only. Sample data only.

  fixture_app.py --mode web --port N   static pages from ./app plus GET /health
  fixture_app.py --mode api --port N   JSON API under /api plus GET /health

Env: FIXTURE_USER and FIXTURE_PASS (login), FIXTURE_STATE_DIR (default .fixture-state, relative to the cwd).
The api mode keeps its data in <state dir>/state.json and reads it on every request, so fixture_reset.py works without a restart.
"""
import argparse
import http.server
import json
import os
import re
import secrets
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = {"orders": [{"id": 1, "item": "Widget", "source": "api"}, {"id": 2, "item": "Gadget", "source": "api"}], "settings": {"theme": "light"}}
PAGES = {"/": "index.html", "/orders": "orders.html", "/settings": "settings.html", "/errors": "errors.html"}
TOKEN = "fx-" + secrets.token_hex(12)   # one token per process. It is not a real credential


def state_file():
    return Path(os.environ.get("FIXTURE_STATE_DIR", ".fixture-state")) / "state.json"


def load():
    f = state_file()
    if not f.exists():
        save(SEED)
    return json.loads(f.read_text(encoding="utf-8"))


def save(data):
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data), encoding="utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "web"

    def log_message(self, *args):
        pass

    def send(self, status, body, ctype="application/json", extra=()):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {}
        return dict(urllib.parse.parse_qsl(raw.decode()))

    def do_GET(self):
        path = self.path.partition("?")[0]
        if path == "/health":
            return self.send(200, b"ok", "text/plain")
        if self.mode == "web":
            page = PAGES.get(path)
            if not page:
                return self.send(404, {"detail": "not found"})
            # DENY makes the proxy rewrite the header to SAMEORIGIN, so the wrapper can frame the page
            return self.send(200, (HERE / "app" / page).read_bytes(), "text/html; charset=utf-8", [("X-Frame-Options", "DENY")])
        if path == "/api/me":
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                return self.send(401, {"detail": "not signed in"})
            return self.send(200, {"user": "reviewer"})
        if path == "/api/orders":
            return self.send(200, {"orders": load()["orders"]})
        m = re.fullmatch(r"/api/orders/(\d+)", path)
        if m:
            hit = [o for o in load()["orders"] if o["id"] == int(m.group(1))]
            return self.send(200, hit[0]) if hit else self.send(404, {"detail": "no such order", "source": "api"})
        self.send(404, {"detail": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        path = self.path.partition("?")[0]
        if self.mode != "api":
            return self.send(404, {"detail": "not found"})
        b = self.body()
        if path == "/api/login":
            user, pw = os.environ.get("FIXTURE_USER"), os.environ.get("FIXTURE_PASS")
            if not user or not pw:
                return self.send(503, {"detail": "fixture credentials are not set"})
            if b.get("username") == user and b.get("password") == pw:
                return self.send(200, {"access_token": TOKEN, "expires_in": 3600})
            return self.send(401, {"detail": "bad credentials"})
        data = load()
        if path == "/api/orders":
            order = {"id": max([o["id"] for o in data["orders"]] + [0]) + 1, "item": str(b.get("item", "Item"))[:60], "source": "api"}
            data["orders"].append(order)
            save(data)
            return self.send(201, order)
        if path == "/api/settings":
            data["settings"].update(b if isinstance(b, dict) else {})
            save(data)
            return self.send(200, {"ok": True})
        self.send(404, {"detail": "not found"})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/orders/(\d+)", self.path.partition("?")[0])
        if self.mode != "api" or not m:
            return self.send(404, {"detail": "not found"})
        data = load()
        data["orders"] = [o for o in data["orders"] if o["id"] != int(m.group(1))]
        save(data)
        self.send(200, {"ok": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["web", "api"], required=True)
    ap.add_argument("--port", type=int, required=True)
    a = ap.parse_args()
    Handler.mode = a.mode
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
