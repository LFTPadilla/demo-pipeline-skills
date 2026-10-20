#!/usr/bin/env python3
"""HTTP tests of review-proxy.py. review.json is its only source of settings. Python 3.9+, standard library only, no browser.

Run: python3 scripts/test_proxy.py
Everything runs in a temp dir. It binds 127.0.0.1 only. It starts the real proxy and the fixture apps in tests/fixtures.
Credentials and tokens are random values made at run time. Output: one PASS or FAIL line per check, then a count.
"""
import http.client
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# 16 zero bytes, base64. A websocket handshake fixture, not a credential.
WS_KEY = "A" * 22 + "=="
SKILL = Path(__file__).resolve().parent.parent
PROXY = SKILL / "scripts" / "review-proxy.py"
TEMPLATE = SKILL / "assets" / "template" / "review-frame.html"
FIX = SKILL / "tests" / "fixtures"
sys.path.insert(0, str(FIX))
from stub_apps import LOGIN_PASS, LOGIN_USER, AppHandler, ApiHandler, comments_api, hreq, raw_status, serve  # noqa: E402
JSON_CT = "application/json; charset=utf-8"
results = []
cleanups = []


def check(name):
    """Register a test. A failure is recorded and the run continues."""
    def wrap(fn):
        try:
            fn()
            results.append((name, None))
            print(f"PASS {name}", flush=True)
        except Exception as e:  # noqa: BLE001 - report every failure
            results.append((name, e))
            print(f"FAIL {name}: {type(e).__name__}: {str(e)[:400]}", flush=True)
        return fn
    return wrap


# ---- Helpers ----
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body)

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


def call(port, method, path, headers=None, body=None, timeout=15):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return Resp(r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
    finally:
        c.close()


def raw(port, data, timeout=6):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    buf = b""
    try:
        s.sendall(data)
        while True:
            d = s.recv(65536)
            if not d:
                break
            buf += d
    except (socket.timeout, OSError):
        pass
    finally:
        s.close()
    return buf


def wait_listen(port, proc=None, tries=100):
    for _ in range(tries):
        if proc is not None and proc.poll() is not None:
            raise RuntimeError("process exited early")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listens on port {port}")


def kill_group(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            continue


def stub(fn=None):
    """A tiny upstream. fn(handler, hit_number) returns (status, content type, bytes) and may sleep. It records every request in .hits."""
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def any(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            hits.append((self.command, self.path, body))
            status, ctype, out = fn(self, len(hits)) if fn else (200, "application/json", b'{"stub":true}')
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(out)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = any

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    srv.handle_error = lambda *a: None
    srv.hits = hits
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cleanups.append(srv.shutdown)
    return srv


# ---- Test world: fixture apps, review folders, proxies ----
TMP = Path(tempfile.mkdtemp(prefix="review-proxy-test-"))
FX_USER = "user-" + secrets.token_hex(4)
FX_PASS = "pw-" + secrets.token_urlsafe(12)
STATE = TMP / "state"
APPS = TMP / "apps"
shutil.copytree(FIX, APPS, ignore=shutil.ignore_patterns("bin", "__pycache__"))
API_PORT, WEB_PORT = free_port(), free_port()
BASE_ENV = {k: v for k, v in os.environ.items() if k != "REVIEW_TOKEN"}
BASE_ENV.update(FIXTURE_USER=FX_USER, FIXTURE_PASS=FX_PASS, FIXTURE_STATE_DIR=str(STATE))
n_review = [0]


def start_apps():
    for mode, port in (("api", API_PORT), ("web", WEB_PORT)):
        p = subprocess.Popen([sys.executable, "fixture_app.py", "--mode", mode, "--port", str(port)], cwd=APPS, env=BASE_ENV,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        cleanups.append(lambda p=p: kill_group(p))
        wait_listen(port, p)


def review(edit=None, files=None):
    """A new review folder: a copy of the fixtures with the live ports. edit(cfg) changes the config dict."""
    n_review[0] += 1
    d = TMP / f"review{n_review[0]}"
    shutil.copytree(FIX, d, ignore=shutil.ignore_patterns("bin", "__pycache__"))
    cfg = json.loads((FIX / "review.json").read_text())
    for a in cfg["apps"]:
        a["port"] = {"api": API_PORT, "web": WEB_PORT}[a["name"]]
    cfg["proxy"]["port"] = free_port()
    if edit:
        edit(cfg)
    (d / "review.json").write_text(json.dumps(cfg), encoding="utf-8")
    for name, content in (files or {}).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(content, encoding="utf-8")
    return d


def to_stub(url, keep_session=False):
    def edit(cfg):
        cfg["routes"] = {"/": url}
        if not keep_session:
            cfg.pop("session", None)
    return edit


def combine(*edits):
    def edit(cfg):
        for e in edits:
            e(cfg)
    return edit


def run_proxy(args, env=None, timeout=20):
    e = dict(BASE_ENV)
    e.update(env or {})
    return subprocess.run([sys.executable, str(PROXY), *args], capture_output=True, text=True, timeout=timeout, env=e)


class Proxy:
    def __init__(self, review_dir, args=(), env=None, port=True, token=True):
        self.dir = review_dir
        self.token = "rv-" + secrets.token_urlsafe(24) if token else None   # token=None: the proxy makes its own, and the link holds it
        n_review[0] += 1
        self.out, self.err = TMP / f"proxy{n_review[0]}.out", TMP / f"proxy{n_review[0]}.err"
        self.cfg = json.loads((review_dir / "review.json").read_text())
        self.port = free_port() if port is True else self.cfg["proxy"]["port"]
        cmd = [sys.executable, str(PROXY), "--config", str(review_dir / "review.json")]
        if port is True:
            cmd += ["--port", str(self.port)]
        env_all = dict(BASE_ENV, **({"REVIEW_TOKEN": self.token} if self.token else {}))
        env_all.update(env or {})
        self.proc = subprocess.Popen(cmd + list(args), env=env_all, stdin=subprocess.DEVNULL, stdout=open(self.out, "w"), stderr=open(self.err, "w"),
                                     start_new_session=True)
        cleanups.append(self.stop)
        wait_listen(self.port, self.proc)
        self._cookie = None

    def stop(self):
        if self.proc.poll() is None:
            kill_group(self.proc)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def cookie(self):
        if self._cookie is None:
            r = call(self.port, "GET", f"/__review/?t={self.token}")
            assert r.status == 302, r.status
            self._cookie = r.headers["set-cookie"].split(";")[0]
        return self._cookie

    def req(self, method, path, headers=None, body=None, cookie=True, **kw):
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = self.cookie
        return call(self.port, method, path, h, body, **kw)

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post_json(self, path, obj, **kw):
        return self.req("POST", path, {"Content-Type": "application/json"}, json.dumps(obj).encode(), **kw)

    def link(self):
        return self.out.read_text().strip()


def started(review_dir, *args, **kw):
    return Proxy(review_dir, args, **kw)


def utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def config_block(html):
    m = re.search(r'<script type="application/json" id="cmt-config">(.*?)</script>', html, re.S)
    assert m, "no cmt-config block"
    return json.loads(m.group(1))




def http_contract(app, api_srv, proxy, review_dir):
    cookie = proxy.cookie

    @check("proxy: token gate answers 403 without token, with a wrong token and with a wrong cookie, and never contacts the app")
    def _():
        before = len(app.hits)
        for path in ("/__review/", "/", "/orders", "/__review/api/comments?project=x", "/__review/session", "/__review/?t=wrong", "/?t=" + proxy.token[:-1]):
            status, h, body = hreq(proxy.port, "GET", path)
            assert status == 403, (path, status)
            assert len(body) < 400 and b"<p>" in body
            assert h["referrer-policy"] == "no-referrer" and h["x-robots-tag"] == "noindex", h
        assert hreq(proxy.port, "GET", "/", {"Cookie": "review_session=bogus"})[0] == 403
        assert hreq(proxy.port, "GET", "/", {"Cookie": "review_session=" + proxy.token})[0] == 403  # the token itself is not the cookie
        assert hreq(proxy.port, "POST", "/__review/api/comments", {"Content-Type": "application/json"}, b"{}")[0] == 403
        assert len(app.hits) == before, app.hits[before:]

    @check("proxy: a valid t sets the session cookie, redirects without t, and the cookie opens the app")
    def _():
        status, h, _b = hreq(proxy.port, "GET", f"/__review/?a=1&t={proxy.token}&b=2")
        assert status == 302 and h["location"] == "/__review/?a=1&b=2", h
        attrs = [a.strip() for a in h["set-cookie"].split(";")]
        assert attrs[0].startswith("review_session=") and proxy.token not in h["set-cookie"]
        assert {"HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=604800"} <= set(attrs), attrs
        assert h["referrer-policy"] == "no-referrer" and h["x-robots-tag"] == "noindex"
        status, h, body = hreq(proxy.port, "GET", "/__review/", {"Cookie": cookie})
        assert status == 200 and b"cmt-config" in body and h["x-robots-tag"] == "noindex" and h["referrer-policy"] == "no-referrer"
        status, h, body = hreq(proxy.port, "GET", "/orders", {"Cookie": cookie})
        assert status == 200 and b"Fixture app" in body and h["x-robots-tag"] == "noindex"
        assert hreq(proxy.port, "GET", "/?t=its-an-app-param", {"Cookie": cookie})[0] == 200  # the app can own a t parameter
        assert hreq(proxy.port, "GET", f"//evil.example/x?t={proxy.token}")[1]["location"] == "/evil.example/x"  # no protocol-relative redirect
        _s, h, _b = hreq(proxy.port, "GET", f"/\\evil.example/x?t={proxy.token}")  # a browser reads "/\\host" as "//host"
        assert h["location"] == "/evil.example/x", h
        _s, h, _b = hreq(proxy.port, "GET", f"/__review/?%74={proxy.token}&b=1&t=nope")  # an encoded key is still the token, and must not stay in the URL
        assert _s == 302 and h["location"] == "/__review/?b=1" and proxy.token not in h["location"], h

    @check("proxy: forwards method, body, headers and status, drops hop-by-hop headers and the review cookie")
    def _():
        hdr = {"Cookie": cookie + "; app_session=abc", "X-Custom": "kept", "Connection": "keep-alive, X-Drop", "X-Drop": "gone", "Content-Type": "application/json", "Host": "evil.example"}
        status, _h, body = hreq(proxy.port, "POST", "/echo?x=1", hdr, b'{"k":"v"}')
        got = json.loads(body)
        assert status == 200 and got["method"] == "POST" and got["path"] == "/echo?x=1" and got["body"] == '{"k":"v"}'
        seen = got["headers"]
        assert seen["x-custom"] == "kept" and "x-drop" not in seen, seen
        assert seen["cookie"] == "app_session=abc", seen  # review_session never reaches the app
        assert seen["host"] == f"127.0.0.1:{app.server_address[1]}", seen  # the request cannot choose the upstream host
        assert seen["x-forwarded-proto"] == "http" and seen["x-forwarded-host"] == "evil.example", seen
        _s, _h, body = hreq(proxy.port, "POST", "/echo", {"Cookie": cookie}, iter([b"ab", b"cd", b"ef"]), chunked=True)
        assert json.loads(body)["body"] == "abcdef"  # chunked request body

    @check("proxy: longest route prefix wins, the default route takes the rest")
    def _():
        status, _h, body = hreq(proxy.port, "GET", "/api/ping?z=1", {"Cookie": cookie})
        assert status == 200 and json.loads(body) == {"service": "api", "path": "/api/ping?z=1"}
        before = len(app.hits)
        assert hreq(proxy.port, "GET", "/apix", {"Cookie": cookie})[0] == 404  # /apix is not under /api: the app answers
        assert len(app.hits) == before + 1

    @check("proxy: rewrites only X-Frame-Options DENY to SAMEORIGIN, keeps the app CSP as it is, rewrites upstream redirects")
    def _():
        _s, h, _b = hreq(proxy.port, "GET", "/denied", {"Cookie": cookie})
        assert h["x-frame-options"] == "SAMEORIGIN", h
        assert h["content-security-policy"] == "default-src 'self'; frame-ancestors 'none'", h  # not our business
        _s, h, _b = hreq(proxy.port, "GET", "/ok-frame", {"Cookie": cookie})
        assert h["x-frame-options"] == "SAMEORIGIN" and h["content-security-policy"] == "frame-ancestors 'self'", h
        _s, h, _b = hreq(proxy.port, "GET", "/redirect", {"Cookie": cookie})
        assert h["location"] == "/orders" and h["server"] == "review-proxy", h

    @check("proxy: a malformed Content-Length or chunk size gets 400, and the proxy keeps working")
    def _():
        head = f"POST /echo HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\nConnection: close\r\n"
        for extra in ("Content-Length: abc\r\n\r\n", "Content-Length: -5\r\n\r\n", "Transfer-Encoding: chunked\r\n\r\nZZ\r\nab\r\n0\r\n\r\n",
                      "Transfer-Encoding: chunked\r\n\r\n-5\r\nab\r\n0\r\n\r\n"):
            assert raw_status(proxy.port, (head + extra).encode())[0] == 400, extra
        assert hreq(proxy.port, "GET", "/orders", {"Cookie": cookie})[0] == 200

    @check("proxy: streams a chunked response instead of buffering it")
    def _():
        c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=15)
        t0 = time.time()
        c.request("GET", "/stream", headers={"Cookie": cookie})
        r = c.getresponse()
        first = r.read(3)
        assert first == b"one" and time.time() - t0 < 0.6, (first, time.time() - t0)
        assert r.read() == b"two"
        c.close()

    @check("proxy: WebSocket upgrade is tunneled in both directions and needs the cookie")
    def _():
        hs = lambda ck: (f"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                         f"Sec-WebSocket-Key: {WS_KEY}\r\n{ck}\r\n").encode()
        assert raw_status(proxy.port, hs(""))[0] == 403
        s = socket.create_connection(("127.0.0.1", proxy.port), timeout=6)
        try:
            s.sendall(hs(f"Cookie: {cookie}\r\n"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s.recv(4096)
            assert buf.startswith(b"HTTP/1.1 101") and b"ICX+Yqv66kxgM0FcWaLWlFLwTAI=" in buf, buf
            s.sendall(b"hello-tunnel")
            assert s.recv(4096) == b"echo:hello-tunnel"
        finally:
            s.close()

    @check("proxy: no open proxy (absolute-URL and CONNECT request lines are refused)")
    def _():
        canary = serve(AppHandler)
        try:
            url = f"http://127.0.0.1:{canary.server_address[1]}/"
            status, _b = raw_status(proxy.port, f"GET {url} HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\nConnection: close\r\n\r\n".encode())
            assert status == 400, status
            status, _b = raw_status(proxy.port, f"GET {url} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            assert status in (400, 403), status
            status, _b = raw_status(proxy.port, f"CONNECT 127.0.0.1:{canary.server_address[1]} HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\nConnection: close\r\n\r\n".encode())
            assert status >= 400, status
            assert hreq(proxy.port, "GET", "/x", {"Cookie": cookie, "Host": f"127.0.0.1:{canary.server_address[1]}"})[0] == 404
            assert canary.hits == [], canary.hits  # nothing reached the canary
        finally:
            canary.shutdown()

    @check("proxy: /__review/ blocks path traversal, hidden files, symlink escapes and the comments file")
    def _():
        (review_dir.parent / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
        (review_dir / ".hidden").write_text("TOP-SECRET", encoding="utf-8")
        try:
            (review_dir / "link.txt").symlink_to(review_dir.parent / "secret.txt")
        except OSError:
            pass
        for path in ("/__review/../secret.txt", "/__review/%2e%2e/secret.txt", "/__review/..%2fsecret.txt", "/__review/%2e%2e%2fsecret.txt",
                     "/__review/a/../../secret.txt", "/__review//etc/passwd", "/__review/%2fetc%2fpasswd", "/__review/.hidden",
                     "/__review/link.txt", "/__review/x%00.html", "/__review/..\\secret.txt", "/__review/../review.comments.json", "/__review/.review/comments.json"):
            status, body = raw_status(proxy.port, f"GET {path} HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\nConnection: close\r\n\r\n".encode())
            assert status in (400, 404) and b"TOP-SECRET" not in body and b"root:" not in body, (path, status)
        assert hreq(proxy.port, "GET", "/__review/index.html", {"Cookie": cookie})[0] == 200
        assert hreq(proxy.port, "PUT", "/__review/index.html", {"Cookie": cookie})[0] == 405




def comments_contract():
    ok = {"project": "api-test", "type": "comment", "id": "api00001", "text": "hi", "author": "Ana", "screen": "path:/", "version": "v1"}
    cf = TMP / "comments-test.json"
    q = started(review(combine(to_stub("http://127.0.0.1:9"), lambda c: c["proxy"].update(commentsFile=str(cf)))))
    ck = q.cookie
    call = lambda *a, **k: comments_api(q, ck, *a, **k)

    @check("proxy API: create, reply, edit, resolve and delete follow the Worker contract")
    def _():
        assert call() == (200, {"comments": []})
        assert call("POST", "", ok) == (201, {"ok": True})
        assert call("POST", "", ok)[0] == 409
        r = {"project": "api-test", "type": "reply", "commentId": "api00001", "id": "rep00001", "text": "reply", "author": "Bo"}
        assert call("POST", "", r)[0] == 201 and call("POST", "", r)[0] == 409
        assert call("POST", "", {**r, "commentId": "nope0000", "id": "rep00002"})[0] == 404
        assert call("PATCH", "", {"project": "api-test", "id": "api00001", "replyId": "rep00001", "text": "x", "author": "Ana"})[0] == 403
        assert call("PATCH", "", {"project": "api-test", "id": "api00001", "replyId": "rep00001", "text": "edited", "author": "Bo"})[0] == 200
        assert call("PATCH", "", {"project": "api-test", "id": "api00001", "resolved": True, "author": "Zed"})[0] == 200
        assert call("PATCH", "", {"project": "api-test", "id": "api00001", "replyId": "rep00001", "resolved": True})[0] == 400
        assert call("PATCH", "", {"project": "api-test", "id": "api00001"})[0] == 400
        c = call()[1]["comments"][0]
        assert c["resolved"] is True and c["replies"][0]["text"] == "edited" and c["n"] == 1 and c["createdAt"].endswith("Z")
        assert call("DELETE", "&id=api00001&replyId=rep00001&author=Ana")[0] == 403
        assert call("DELETE", "&id=api00001&replyId=rep00001&author=Bo")[0] == 200
        assert call("DELETE", "&id=api00001&author=Bo")[0] == 403
        assert call("DELETE", "&id=api00001&author=Ana")[0] == 200
        assert call() == (200, {"comments": []})

    pc = lambda m="GET", extra="", body=None: call(m, extra, body) if m != "GET" else call(query="?project=rxp")

    @check("proxy API: reactions toggle on a comment and a reply, are idempotent, and return to empty")
    def _():
        base = {"project": "rxp", "type": "comment", "id": "rx000001", "text": "hi", "author": "Ana", "screen": "a", "version": "v1"}
        assert pc("POST", "", base)[0] == 201
        assert pc("POST", "", {"project": "rxp", "type": "reply", "commentId": "rx000001", "id": "rx000002", "text": "re", "author": "Bo"})[0] == 201
        react = lambda who, on, e="👍", **kw: pc("PATCH", "", {"project": "rxp", "id": "rx000001", "react": e, "on": on, "author": who, **kw})
        got = lambda: pc("GET")[1]["comments"][0]
        assert react("Bo", True)[0] == 200 and react("Bo", True)[0] == 200   # a repeat changes nothing
        assert got()["reactions"] == {"👍": ["Bo"]}
        assert react("Cy", True)[0] == 200 and got()["reactions"] == {"👍": ["Bo", "Cy"]}
        assert react("Cy", True, "✅")[0] == 200 and got()["reactions"] == {"👍": ["Bo", "Cy"], "✅": ["Cy"]}
        assert react("Bo", False)[0] == 200 and react("Bo", False)[0] == 200
        assert react("Cy", False)[0] == 200 and react("Cy", False, "✅")[0] == 200
        assert "reactions" not in got(), got()   # on, then off: empty again
        assert react("Zed", True, replyId="rx000002")[0] == 200
        assert got()["replies"][0]["reactions"] == {"👍": ["Zed"]} and "reactions" not in got()
        assert react("Zed", False, replyId="rx000002")[0] == 200 and "reactions" not in got()["replies"][0]

    @check("proxy API: reactions refuse an emoji outside the set, bad fields, more than 50 names, and long names")
    def _():
        p = {"project": "rxp", "id": "rx000001"}
        patch = lambda **kw: pc("PATCH", "", {**p, "author": "Bo", "on": True, "react": "👍", **kw})[0]
        assert patch(react="💩") == 400 and patch(react=5) == 400 and patch(react="👍👍") == 400
        assert patch(on="yes") == 400 and patch(author="") == 400 and patch(author="x" * 81) == 400 and patch(foo=1) == 400
        assert patch(text="x") == 400 and patch(resolved=True) == 400   # react does not mix with other changes
        assert patch(replyId="nope0000") == 404
        post = lambda cid, **kw: pc("POST", "", {"project": "rxp", "type": "comment", "id": cid, "text": "t", "author": "Ana", **kw})[0]
        assert post("rx000010", reactions={"💩": ["a"]}) == 400 and post("rx000011", reactions=[]) == 400 and post("rx000012", reactions={"👍": "a"}) == 400
        assert post("rx000013", reactions={"👍": ["a"] * 51}) == 400 and post("rx000014", reactions={"👍": ["x" * 81]}) == 400
        assert post("rx000015", reactions={"👍": ["a", "a", "b"], "✅": []}) == 201   # an import keeps unique names, drops empty lists
        assert [c["reactions"] for c in pc("GET")[1]["comments"] if c["id"] == "rx000015"] == [{"👍": ["a", "b"]}]
        for i in range(50):
            assert pc("PATCH", "", {**p, "id": "rx000015", "react": "🎉", "on": True, "author": f"n{i}"})[0] == 200
        assert pc("PATCH", "", {**p, "id": "rx000015", "react": "🎉", "on": True, "author": "n50"})[0] == 422
        assert pc("PATCH", "", {**p, "id": "rx000015", "react": "🎉", "on": True, "author": "n0"})[0] == 200   # already there: no error

    @check("proxy API: validation limits match the Worker")
    def _():
        bad = lambda **kw: call("POST", "", {**ok, "id": "val" + secrets.token_hex(3), **kw})[0]
        assert bad(text="x" * 4001) == 400 and bad(author="x" * 81) == 400 and bad(where="x" * 201) == 400
        assert bad(path="x" * 1001) == 400 and bad(screen="x" * 65) == 400 and bad(version="x" * 21) == 400
        assert bad(text="   ") == 400 and bad(fx="1") == 400 and bad(fx=float("nan")) == 400 and bad(createdAt="not a date") == 400
        assert bad(fx=10**400) == 400 and bad(n=10**400) == 400  # the Worker reads these as Infinity: 400, not a dropped connection
        assert bad(createdAt="0001-01-01T00:00:00+05:00") == 400 and bad(createdAt="9999-12-31T23:59:59-05:00") == 400
        assert bad(surprise=1) == 400 and bad(project="Bad Name") == 400 and bad(id="ab") == 400 and bad(type="other") == 400
        assert call("POST", "", {**ok, "id": "val00099", "replies": [{"id": "rr000001", "author": "Bo", "text": "t"}]})[0] == 201
        assert call("POST", "", {**ok, "id": "val00098", "replies": [{"id": "rr000001", "author": "Bo", "text": "t", "extra": 1}]})[0] == 400
        s, b = call("POST", "", {**ok, "id": "val00097", "surprise": 1})
        assert s == 400 and "unknown field" in b["error"]
        assert call(query="?project=api-test&bogus=1")[0] == 400 and call(query="?project=Bad")[0] == 400
        assert call("PUT", "")[0] == 405
        status, _h, body = hreq(q.port, "POST", "/__review/api/comments", {"Cookie": ck, "Content-Type": "application/json"}, b"[1]")
        assert status == 400 and json.loads(body)["error"] == "body must be an object"
        assert hreq(q.port, "POST", "/__review/api/comments", {"Cookie": ck}, b"{not json")[0] == 400
        assert hreq(q.port, "POST", "/__review/api/comments", {"Cookie": ck}, b"[" * 100000)[0] == 400  # nested too deep for the parser
        assert hreq(q.port, "POST", "/__review/api/comments", {"Cookie": ck}, b"x" * 200001)[0] == 413  # request size limit
        assert hreq(q.port, "POST", "/__review/api/comments", {"Cookie": ck}, iter([b"{}"]), chunked=True)[0] == 411

    @check("proxy API: numbers are assigned by the server and stay stable after a delete")
    def _():
        mk = lambda i, **kw: call("POST", "", {**ok, "project": "num-test", "id": f"num{i:05d}", **kw})
        for i in (1, 2, 3):
            assert mk(i, n=99)[0] == 201  # a client n is ignored
        assert [c["n"] for c in call(query="?project=num-test")[1]["comments"]] == [1, 2, 3]
        assert call("DELETE", "?project=num-test&id=num00002&author=Ana")[0] == 200
        mk(4)
        mk(5, screen="path:/orders")
        mk(6, version="v2")
        got = {c["id"]: c["n"] for c in call(query="?project=num-test")[1]["comments"]}
        assert got == {"num00001": 1, "num00003": 3, "num00004": 4, "num00005": 1, "num00006": 1}, got

    @check("proxy API: a number is never used twice, even when the newest comment is deleted")
    def _():
        mk = lambda i: call("POST", "", {**ok, "project": "hw-test", "id": f"hw{i:05d}"})
        nums = lambda: {c["id"]: c["n"] for c in call(query="?project=hw-test")[1]["comments"]}
        for i in (1, 2, 3):
            assert mk(i)[0] == 201
        assert call("DELETE", "?project=hw-test&id=hw00003&author=Ana")[0] == 200
        assert mk(4)[0] == 201 and nums() == {"hw00001": 1, "hw00002": 2, "hw00004": 4}, nums()
        for i in (1, 2, 4):
            assert call("DELETE", f"?project=hw-test&id=hw{i:05d}&author=Ana")[0] == 200
        assert mk(5)[0] == 201 and nums() == {"hw00005": 5}, nums()  # even an empty list keeps counting

    @check("proxy API: concurrent writes keep every comment and unique numbers")
    def _():
        errors = []

        def work(t):
            for i in range(15):
                s, _b = call("POST", "", {**ok, "project": "conc-test", "id": f"c{t}x{i:03d}", "author": f"u{t}"})
                if s != 201:
                    errors.append(s)
        threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        got = call(query="?project=conc-test")[1]["comments"]
        assert not errors and len(got) == 120 and sorted(c["n"] for c in got) == list(range(1, 121))

    @check("proxy API: store file is written atomically (no temp files left, owner-only mode, valid JSON)")
    def _():
        assert [f.name for f in TMP.glob("comments-test.json*")] == ["comments-test.json"], list(TMP.glob("comments-test.json*"))
        assert (cf.stat().st_mode & 0o777) == 0o600
        assert len(json.loads(cf.read_text())["conc-test"]) == 120

    @check("proxy API: comments survive a proxy restart")
    def _():
        before = call(query="?project=num-test")[1]
        q.stop()
        q2 = started(q.dir)   # the same review folder, a new process
        assert comments_api(q2, q2.cookie, query="?project=num-test")[1] == before
        assert comments_api(q2, q2.cookie, "POST", "", {**ok, "project": "num-test", "id": "num00007"})[0] == 201
        assert [c["n"] for c in comments_api(q2, q2.cookie, query="?project=num-test")[1]["comments"]][-1] == 5  # next after 4 in path:/

    @check("proxy API: old comments without n are numbered once, in creation order, and the limit of 500 holds")
    def _():
        legacy = TMP / "legacy.json"
        mk = lambda i, at, **kw: {"id": f"leg{i:05d}", "screen": "path:/", "version": "v1", "path": "", "fx": 0, "fy": 0, "pageX": 0, "pageY": 0, "where": "",
                                  "text": "old", "author": "Ana", "createdAt": at, "resolved": False, "replies": [], **kw}
        legacy.write_text(json.dumps({
            "old-test": [mk(1, "2030-01-03T00:00:00.000Z"), mk(2, "2030-01-01T00:00:00.000Z"), mk(3, "2030-01-02T00:00:00.000Z"),
                         mk(4, "2030-01-04T00:00:00.000Z", n=7, screen="path:/orders")],
            "full-test": [mk(i, f"2030-01-01T00:00:{i % 60:02d}.000Z", n=i + 1) for i in range(500)]}), encoding="utf-8")
        p2 = started(review(combine(to_stub("http://127.0.0.1:9"), lambda c: c["proxy"].update(commentsFile=str(legacy)))))
        try:
            c2 = p2.cookie
            got = {c["id"]: c["n"] for c in comments_api(p2, c2, query="?project=old-test")[1]["comments"]}
            assert got == {"leg00001": 3, "leg00002": 1, "leg00003": 2, "leg00004": 7}, got
            assert comments_api(p2, c2, "POST", "", {**ok, "project": "old-test", "id": "leg00005"})[0] == 201
            stored = {c["id"]: c["n"] for c in json.loads(legacy.read_text())["old-test"]}
            assert stored == {"leg00001": 3, "leg00002": 1, "leg00003": 2, "leg00004": 7, "leg00005": 4}, stored  # backfilled once, then stored
            s, b = comments_api(p2, c2, "POST", "", {**ok, "project": "full-test", "id": "leg99999"})
            assert s == 422 and "limit" in b["error"], (s, b)
        finally:
            p2.stop()




def misc_contract():
    @check("proxy: share link is the only stdout line, https with --public-host, and a token is generated when none is given")
    def _():
        p = started(review(combine(to_stub("http://127.0.0.1:9"), lambda c: c.update(readOnly=False))), "--public-host", "https://my-pc.example.test", token=None)
        try:
            lines = p.out.read_text().splitlines()
            assert len(lines) == 1 and re.fullmatch(r"https://my-pc\.example\.test/__review/\?t=[A-Za-z0-9_-]{30,}", lines[0]), lines
            p.token = lines[0].split("t=")[1]
            assert p.token not in p.err.read_text()
            status, h, _b = hreq(p.port, "GET", f"/__review/?t={p.token}")
            assert status == 302 and "set-cookie" in h
            status, _h, body = hreq(p.port, "POST", "/echo", {"Cookie": h["set-cookie"].split(";")[0]})
            assert status == 503  # nothing listens upstream: a clean 503 (review paused), no hang, no traceback
        finally:
            p.stop()

    @check("proxy: a silent or half-sent request is closed after the idle timeout, before the token gate")
    def _():
        p = started(review(combine(to_stub("http://127.0.0.1:9"), lambda c: c["proxy"].update(idleTimeout=2))))
        try:
            for data in (b"", b"GET / HTTP/1.1\r\nHost: x\r\n", b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 60000\r\n\r\n"):
                s = socket.create_connection(("127.0.0.1", p.port), timeout=9)
                t0 = time.time()
                try:
                    if data:
                        s.sendall(data)
                    s.recv(4096)  # the server closes the connection: b"" or an error answer
                except socket.timeout:
                    raise AssertionError(f"the server still waits after 9 s: {data!r}") from None
                except OSError:
                    pass
                finally:
                    s.close()
                assert time.time() - t0 < 6, (data, time.time() - t0)
            assert hreq(p.port, "GET", "/", {})[0] == 403  # still serving
        finally:
            p.stop()


def main():
    start_apps()
    # a small script for the appVersion tests: counts its runs, records whether it saw the token, prints two lines
    ver = ("import os\nn = int(open('count.txt').read()) + 1 if os.path.exists('count.txt') else 1\n"
           "open('count.txt', 'w').write(str(n))\nopen('seen_token.txt', 'w').write('yes' if 'REVIEW_TOKEN' in os.environ else 'no')\n"
           "print('v%d' % n)\nprint('second line')\n")

    # ---------- meta ----------
    @check("meta: four keys, JSON headers, values from the config, 405 for other methods, 403 without a cookie")
    def _():
        p = started(review())
        r = p.get("/__review/meta")
        assert r.status == 200 and r.headers["content-type"] == JSON_CT and r.headers["cache-control"] == "no-store", (r.status, r.headers)
        j = r.json()
        assert sorted(j) == ["expiresAt", "readOnly", "resetEnabled", "version"], j
        assert j["version"] == "abc1234" and j["readOnly"] is True and j["resetEnabled"] is True, j
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", j["expiresAt"]), j
        assert abs(utc(j["expiresAt"]) - (time.time() + 7 * 86400)) < 120, j  # ttl 7d from the start of the proxy
        assert call(p.port, "GET", "/__review/meta").status == 403
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            r = p.req(m, "/__review/meta")
            assert r.status == 405 and r.json() == {"error": "method not allowed"} and r.headers["content-type"] == JSON_CT, (m, r.status, r.body)
        assert p.req("HEAD", "/__review/meta").status == 405

    @check("meta: no appVersion, ttl or reset gives null, null, readOnly true by default, resetEnabled false")
    def _():
        def edit(cfg):
            for k in ("appVersion", "ttl", "reset", "readOnly"):
                cfg.pop(k, None)
        p = started(review(edit))
        assert p.get("/__review/meta").json() == {"version": None, "expiresAt": None, "readOnly": True, "resetEnabled": False}

    @check("meta: expiresAt in the config, offset and no-zone forms, and --expires-at wins")
    def _():
        def at(v):
            return lambda cfg: (cfg.pop("ttl", None), cfg.update(expiresAt=v))
        assert started(review(at("2099-01-01T01:00:00+01:00"))).get("/__review/meta").json()["expiresAt"] == "2099-01-01T00:00:00Z"
        assert started(review(at("2097-01-01T00:00:00"))).get("/__review/meta").json()["expiresAt"] == "2097-01-01T00:00:00Z"
        p = started(review(at("2099-01-01T00:00:00Z")), "--expires-at", "2098-02-03T04:05:06Z")
        assert p.get("/__review/meta").json()["expiresAt"] == "2098-02-03T04:05:06Z"

    @check("appVersion command: first stdout line, no shell, review folder as cwd, no token in its environment, cached")
    def _():
        d = review(lambda c: c.update(appVersion={"command": ["python3", "ver.py"]}), {"ver.py": ver})
        p = started(d)
        assert p.get("/__review/meta").json()["version"] == "v1"
        assert p.get("/__review/meta").json()["version"] == "v1"  # cached for 10 seconds: the script ran once
        assert (d / "count.txt").read_text() == "1" and (d / "seen_token.txt").read_text() == "no"
        d2 = review(lambda c: c.update(appVersion={"command": "python3 ver.py"}), {"ver.py": ver})  # a string is split with shlex, not run in a shell
        assert started(d2).get("/__review/meta").json()["version"] == "v1"
        d3 = review(lambda c: c.update(appVersion={"command": "echo hi; touch pwned"}), {})
        assert started(d3).get("/__review/meta").json()["version"] is None and not (d3 / "pwned").exists()

    @check("appVersion command: a failing, bad-value or slow command gives null (5 second limit)")
    def _():
        d = review(lambda c: c.update(appVersion={"command": ["python3", "bad.py"]}), {"bad.py": "import sys\nprint('v9')\nsys.exit(1)\n"})
        assert started(d).get("/__review/meta").json()["version"] is None
        d = review(lambda c: c.update(appVersion={"command": ["python3", "bad.py"]}), {"bad.py": "print('has space')\n"})
        assert started(d).get("/__review/meta").json()["version"] is None
        d = review(lambda c: c.update(appVersion={"command": ["python3", "nothing-here.py"]}), {})
        assert started(d).get("/__review/meta").json()["version"] is None
        d = review(lambda c: c.update(appVersion={"command": ["python3", "slow.py"]}), {"slow.py": "import time\ntime.sleep(30)\n"})
        t0 = time.time()
        assert started(d).get("/__review/meta", timeout=12).json()["version"] is None
        assert 4 <= time.time() - t0 < 10, time.time() - t0

    # ---------- mocks ----------
    @check("mocks: fixture rules answer from the proxy, with headers, a compact body and no upstream call")
    def _():
        p = started(review())
        r = p.get("/api/orders/1")
        assert r.status == 200 and r.headers["content-type"] == JSON_CT and r.headers["cache-control"] == "no-store", r.headers
        assert r.body == b'{"id":1,"item":"Mock widget","source":"mock"}', r.body
        assert p.get("/api/orders/2").json()["item"] == "Mock gadget"
        r = p.get("/api/orders/9")  # table miss: the rule's own status and json
        assert r.status == 404 and r.json() == {"detail": "mock: no such order", "source": "mock"}, (r.status, r.body)
        assert p.get("/api/orders/special").json()["id"] == "special"  # a literal segment beats {id}
        assert p.get("/api/orders/1?x=1").json()["source"] == "mock" and p.get("/api/orders/1/").json()["source"] == "mock"
        assert p.get("/api/orders/%31").json()["item"] == "Mock widget"  # percent-decoded before the compare
        assert p.get("/api/mock-file").json() == {"source": "mock-file"}  # from mocksFile
        r = p.get("/api/orders")
        assert r.status == 200 and all(o["source"] == "api" for o in r.json()["orders"]), r.body  # no rule: the real app answers
        r = p.req("HEAD", "/api/orders/1")
        assert r.status == 200 and r.body == b"" and r.headers["content-type"] == JSON_CT
        assert call(p.port, "GET", "/api/orders/1").status == 403  # a mock never skips the cookie

    @check("mocks: a POST mock answers under read-only, and the app is not called")
    def _():
        s = stub()
        p = started(review(combine(to_stub(s.url), lambda c: c.update(mocks=[{"method": "POST", "path": "/api/mock-create", "status": 201, "json": {"ok": True}}]))))
        r = p.post_json("/api/mock-create", {"a": 1})
        assert r.status == 201 and r.json() == {"ok": True}
        assert s.hits == []
        assert p.post_json("/api/other", {}).status == 403 and s.hits == []

    @check("mocks: specificity, method, list order, mocksFile order, table fall-through, odd statuses and values")
    def _():
        s = stub()
        rules = [
            {"path": "/api/{a}/x", "json": {"r": "a"}},
            {"path": "/api/zz/{b}", "json": {"r": "b"}},            # more literal characters wins
            {"method": "*", "path": "/thing/{id}", "json": {"r": "star"}},
            {"method": "GET", "path": "/thing/{id}", "json": {"r": "exact"}},  # an exact method beats *
            {"path": "/same/{a}", "json": {"r": "first"}},
            {"path": "/same/{b}", "json": {"r": "second"}},          # the earlier rule wins on a tie
            {"path": "/lit/{y}/{z}", "json": {"r": "1lit"}},
            {"path": "/lit/a/{x}", "json": {"r": "2lit"}},           # more literal segments wins, whatever the list order
            {"path": "/tbl/{id}", "by": "id", "table": {"1": {"json": {"r": "t1"}}, "3": {"status": 202, "json": {"r": "t3"}}}},
            {"path": "/{any}/{x}", "json": {"r": "fallback"}},
            {"path": "/dup", "json": {"r": "inline"}},
            {"path": "/gone", "status": 204, "json": None},
            {"path": "/falsy", "json": False},
            {"path": "/Case/{id}", "method": "get", "json": {"r": "case"}},
        ]
        d = review(combine(to_stub(s.url), lambda c: c.update(mocks=rules, mocksFile="m.json")),
                   {"m.json": json.dumps([{"path": "/dup", "json": {"r": "file"}}, {"path": "/only-file", "json": {"r": "file-only"}}])})
        p = started(d)
        j = lambda m, path: p.req(m, path).json()
        assert j("GET", "/api/zz/x") == {"r": "b"} and j("GET", "/api/q/x") == {"r": "a"}
        assert j("GET", "/thing/1") == {"r": "exact"} and j("POST", "/thing/1") == {"r": "star"} and j("DELETE", "/thing/1") == {"r": "star"}
        assert j("GET", "/same/1") == {"r": "first"}
        assert j("GET", "/lit/a/b") == {"r": "2lit"} and j("GET", "/lit/c/b") == {"r": "1lit"}
        assert j("GET", "/tbl/1") == {"r": "t1"}
        r = p.get("/tbl/3")
        assert r.status == 202 and r.json() == {"r": "t3"}
        assert j("GET", "/tbl/2") == {"r": "fallback"}  # table miss and no json: the search goes on
        assert j("GET", "/dup") == {"r": "inline"} and j("GET", "/only-file") == {"r": "file-only"}  # inline rules come first
        r = p.get("/gone")
        assert r.status == 204 and r.body == b""
        assert p.get("/falsy").body == b"false"
        assert j("GET", "/Case/5") == {"r": "case"}
        assert s.hits == [], s.hits
        assert p.get("/a/b/c").json() == {"stub": True} and len(s.hits) == 1  # no rule matches three segments: the upstream answers

    @check("mocks: a path under /__review is never mocked, and a mock cannot hide the review pages")
    def _():
        p = started(review(lambda c: c.update(mocks=[{"path": "/{x}/meta", "json": {"mock": True}}])))
        assert p.get("/__review/meta").json()["readOnly"] is True  # the internal route comes first
        assert p.get("/other/meta").json() == {"mock": True}

    # ---------- read-only ----------
    @check("read-only: POST, PUT, PATCH and DELETE give 403 with the marker header and the exact body")
    def _():
        p = started(review())
        for m, path in (("POST", "/api/settings"), ("PUT", "/api/orders/7"), ("PATCH", "/api/orders/7"), ("DELETE", "/api/orders/7")):
            r = p.req(m, path, {"Content-Type": "application/json"}, b'{"a":1}')
            assert r.status == 403 and r.body == b'{"detail":"read-only review"}', (m, r.status, r.body)
            assert r.headers["content-type"] == JSON_CT and r.headers["x-review-read-only"] == "1", r.headers
        assert p.get("/api/orders").status == 200
        assert p.req("OPTIONS", "/api/orders").status != 403
        assert "x-review-read-only" not in p.get("/api/orders").headers
        r = p.post_json("/api/orders", {"item": "Allowed"})  # allowWrite in the fixture
        assert r.status == 201 and r.json()["source"] == "api", (r.status, r.body)

    @check("read-only: allowWrite matches method and path pattern, and readOnly false lets writes through")
    def _():
        s = stub()
        mk = lambda e=None: review(combine(to_stub(s.url), lambda c: c.update(allowWrite=[{"method": "DELETE", "path": "/api/items/{id}"}]), e or (lambda c: None)))
        p = started(mk())
        n = len(s.hits)
        assert p.req("DELETE", "/api/items/5").status == 200 and len(s.hits) == n + 1
        assert p.req("DELETE", "/api/items/5/extra").status == 403
        assert p.req("PUT", "/api/items/5").status == 403
        assert p.req("DELETE", "/api/items").status == 403
        assert len(s.hits) == n + 1
        p = started(mk(lambda c: c.update(readOnly=False)))
        assert p.post_json("/x", {}).status == 200 and p.get("/__review/meta").json()["readOnly"] is False

    @check("read-only: the proxy's own auto-login call and the comments API are not blocked")
    def _():
        p = started(review())
        r = p.get("/__review/session")
        assert r.status == 200, (r.status, r.body)
        item = r.json()["store"][0]
        assert item["key"] == "app_token" and item["value"].startswith("fx-"), item
        c = {"project": "fixture-review", "type": "comment", "id": "ro000001", "text": "hello", "author": "tester"}
        assert p.post_json("/__review/api/comments", c).status == 201

    # ---------- comments: appVersion ----------
    @check("comments: appVersion is stored, always returned, validated, and not allowed in PATCH or in a reply")
    def _():
        d = review()
        p = started(d)
        base = {"project": "fixture-review", "type": "comment", "text": "t", "author": "a", "screen": "home", "version": "v1"}
        assert p.post_json("/__review/api/comments", dict(base, id="av000001", appVersion="abc1234")).status == 201
        assert p.post_json("/__review/api/comments", dict(base, id="av000002")).status == 201
        for i, bad in enumerate(["has space", "x" * 41, 5, ["a"]]):
            r = p.post_json("/__review/api/comments", dict(base, id=f"av00001{i}", appVersion=bad))
            assert r.status == 400 and "appVersion" in r.json()["error"], (bad, r.status, r.body)
        assert p.post_json("/__review/api/comments", dict(base, id="av000020", appVersion="x" * 40)).status == 201
        lst = p.get("/__review/api/comments?project=fixture-review").json()["comments"]
        by = {c["id"]: c for c in lst}
        assert by["av000001"]["appVersion"] == "abc1234" and by["av000002"]["appVersion"] == "" and "appVersion" in by["av000002"], by
        r = p.req("PATCH", "/__review/api/comments", {"Content-Type": "application/json"},
                  json.dumps({"project": "fixture-review", "id": "av000001", "resolved": True, "appVersion": "v2"}).encode())
        assert r.status == 400 and "unknown field" in r.json()["error"], r.body
        r = p.post_json("/__review/api/comments", {"project": "fixture-review", "type": "reply", "commentId": "av000001", "id": "rp000001",
                                                  "text": "t", "author": "a", "appVersion": "v2"})
        assert r.status == 400 and "unknown field" in r.json()["error"], r.body
        assert (d / ".review" / "comments.json").is_file() and (d / ".review").stat().st_mode & 0o777 == 0o700

    @check("comments: a store file from before appVersion existed returns appVersion as an empty string")
    def _():
        d = review(files={".review/comments.json": json.dumps({"fixture-review": [
            {"id": "old00001", "n": 1, "screen": "home", "version": "v1", "path": "", "fx": 0, "fy": 0, "pageX": 0, "pageY": 0, "where": "",
             "text": "old", "author": "a", "createdAt": "2026-01-01T00:00:00.000Z", "resolved": False, "replies": []}]})})
        c = started(d).get("/__review/api/comments?project=fixture-review").json()["comments"]
        assert c[0]["appVersion"] == "", c

    # ---------- reset ----------
    RESET = {"command": ["python3", "fixture_reset.py"], "cwd": ".", "timeoutSeconds": 20}

    @check("reset: 405 and CSRF and origin checks, success restores the data, the cache of 30 seconds limits a second run")
    def _():
        p = started(review())
        assert p.post_json("/api/orders", {"item": "Extra"}).status == 201
        assert len(p.get("/api/orders").json()["orders"]) >= 3
        r = p.get("/__review/reset")
        assert r.status == 405 and r.headers["allow"] == "POST" and r.json() == {"error": "method not allowed"}, (r.status, r.headers)
        r = p.req("POST", "/__review/reset")
        assert r.status == 403 and r.json() == {"error": "missing csrf header"}
        r = p.req("POST", "/__review/reset", {"X-Review-Reset": "0"})
        assert r.status == 403 and r.json() == {"error": "missing csrf header"}
        for origin in ("http://evil.example", "null", f"http://127.0.0.1:{p.port + 1}"):
            r = p.req("POST", "/__review/reset", {"X-Review-Reset": "1", "Origin": origin})
            assert r.status == 403 and r.json() == {"error": "bad origin"}, (origin, r.status, r.body)
        assert call(p.port, "POST", "/__review/reset", {"X-Review-Reset": "1"}).status == 403  # no cookie
        r = p.req("POST", "/__review/reset", {"X-Review-Reset": "1", "Origin": f"http://127.0.0.1:{p.port}"})  # rejected tries did not use the limit
        assert r.status == 200 and r.headers["cache-control"] == "no-store", (r.status, r.body)
        j = r.json()
        assert sorted(j) == ["durationMs", "ok"] and j["ok"] is True and isinstance(j["durationMs"], int) and j["durationMs"] >= 0, j
        assert not [h for h in r.headers if h.startswith("access-control-")]
        assert [o["id"] for o in p.get("/api/orders").json()["orders"]] == [1, 2]
        r = p.req("POST", "/__review/reset", {"X-Review-Reset": "1"})
        j = r.json()
        assert r.status == 429 and j["error"] == "rate limited" and 1 <= j["retryAfterSeconds"] <= 30, (r.status, j)
        assert r.headers["retry-after"] == str(j["retryAfterSeconds"])

    @check("reset: a non-zero exit gives 500 with the exit code, and the failed run still counts for the limit")
    def _():
        p = started(review(), env={"FIXTURE_RESET_FAIL": "1"})
        r = p.req("POST", "/__review/reset", {"X-Review-Reset": "1"})
        j = r.json()
        assert r.status == 500 and j["ok"] is False and j["error"] == "reset failed (exit 3)" and isinstance(j["durationMs"], int), (r.status, j)
        assert p.req("POST", "/__review/reset", {"X-Review-Reset": "1"}).status == 429

    @check("reset: a timeout gives 504 and kills the whole process group")
    def _():
        sleeper = ("import os, subprocess, sys, time\nfrom pathlib import Path\n"
                   "c = subprocess.Popen(['sleep', '60'])\nPath('pids.txt').write_text('%d %d' % (os.getpid(), c.pid))\ntime.sleep(60)\n")
        d = review(lambda c: c.update(reset={"command": ["python3", "sleeper.py"], "cwd": ".", "timeoutSeconds": 1}), {"sleeper.py": sleeper})
        p = started(d)
        t0 = time.time()
        r = p.req("POST", "/__review/reset", {"X-Review-Reset": "1"}, timeout=20)
        j = r.json()
        assert r.status == 504 and j["ok"] is False and j["error"] == "reset timed out", (r.status, j)
        assert 0.9 <= time.time() - t0 < 8, time.time() - t0
        pids = [int(x) for x in (d / "pids.txt").read_text().split()]
        time.sleep(0.3)
        for pid in pids:
            try:
                os.kill(pid, 0)
                state = Path(f"/proc/{pid}/stat").read_text().split()[2] if Path(f"/proc/{pid}/stat").exists() else "gone"
                assert state in ("Z", "gone"), f"pid {pid} still runs"
            except ProcessLookupError:
                pass

    @check("reset: a command that cannot start gives 500, a missing config gives 404 for every method")
    def _():
        p = started(review(lambda c: c.update(reset={"command": ["./does-not-exist"], "cwd": ".", "timeoutSeconds": 5})))
        r = p.req("POST", "/__review/reset", {"X-Review-Reset": "1"})
        assert r.status == 500 and r.json()["error"] == "reset could not start" and r.json()["ok"] is False, r.body
        p = started(review(lambda c: c.update(reset={"command": ["python3", "x.py"], "cwd": "no-such-folder", "timeoutSeconds": 5})))
        assert p.req("POST", "/__review/reset", {"X-Review-Reset": "1"}).json()["error"] == "reset could not start"
        p = started(review(lambda c: c.pop("reset")))
        for m in ("GET", "POST"):
            r = p.req(m, "/__review/reset", {"X-Review-Reset": "1"})
            assert r.status == 404 and r.json() == {"error": "reset disabled"}, (m, r.status, r.body)

    @check("reset: a run in progress counts for the limit")
    def _():
        p = started(review(), env={"FIXTURE_RESET_SLEEP": "2"})
        first = []
        t = threading.Thread(target=lambda: first.append(p.req("POST", "/__review/reset", {"X-Review-Reset": "1"}, timeout=20)))
        t.start()
        time.sleep(0.6)
        r = p.req("POST", "/__review/reset", {"X-Review-Reset": "1"})
        assert r.status == 429 and r.json()["retryAfterSeconds"] >= 1, (r.status, r.body)
        t.join(15)
        assert first and first[0].status == 200, first and first[0].body

    @check("reset: argv only, the request body and query never reach the command, no REVIEW_TOKEN, cwd, log file mode, new session")
    def _():
        dump = ("import json, os, sys\nfrom pathlib import Path\n"
                "Path(__file__).with_name('dump.json').write_text(json.dumps({'argv': sys.argv, 'has_token': 'REVIEW_TOKEN' in os.environ, "
                "'marker': os.environ.get('TEST_MARKER'), 'stdin': sys.stdin.read(), 'cwd': os.getcwd(), 'leader': os.getsid(0) == os.getpid()}))\n"
                "print('hello-from-reset')\nprint('to-stderr', file=sys.stderr)\n")
        d = review(lambda c: c.update(reset={"command": ["python3", "../dump.py"], "cwd": "sub", "timeoutSeconds": 10}), {"dump.py": dump, "sub/keep.txt": "x"})
        p = started(d, env={"TEST_MARKER": "m1"})
        r = p.req("POST", "/__review/reset?cmd=--evil&x=1", {"X-Review-Reset": "1", "Content-Type": "application/json"}, b'{"cmd":"rm -rf x"}')
        assert r.status == 200, r.body
        got = json.loads((d / "dump.json").read_text())
        assert got["argv"] == ["../dump.py"] and got["has_token"] is False and got["marker"] == "m1" and got["stdin"] == "", got
        assert got["cwd"] == str((d / "sub").resolve()) and got["leader"] is True, got
        log = d / ".review" / "reset.log"
        assert "hello-from-reset" in log.read_text() and "to-stderr" in log.read_text()
        assert log.stat().st_mode & 0o777 == 0o600 and (d / ".review").stat().st_mode & 0o777 == 0o700
        assert p.token not in log.read_text()

    @check("reset: after success the next /__review/session logs in again (auto-login cache cleared)")
    def _():
        def login(h, n):
            if h.path == "/login":
                return 200, "application/json", json.dumps({"access_token": f"t-{logins.append(1) or len(logins)}", "expires_in": 3600}).encode()
            return 200, "application/json", b"{}"
        logins = []
        s = stub(login)
        sess = {"login": {"route": "/", "method": "POST", "path": "/login", "encoding": "json", "fields": {"username": "$FIXTURE_USER", "password": "$FIXTURE_PASS"}},
                "tokenField": "access_token", "store": [{"type": "localStorage", "key": "k", "value": "{token}"}]}
        p = started(review(combine(to_stub(s.url), lambda c: c.update(session=sess, reset={"command": ["python3", "-c", "pass"], "cwd": ".", "timeoutSeconds": 10}))))
        assert p.get("/__review/session").json()["store"][0]["value"] == "t-1"
        assert p.get("/__review/session").json()["store"][0]["value"] == "t-1" and len(logins) == 1
        assert p.req("POST", "/__review/reset", {"X-Review-Reset": "1"}).status == 200
        assert p.get("/__review/session").json()["store"][0]["value"] == "t-2" and len(logins) == 2

    # ---------- 410 ----------
    @check("expiry: every method and path answers 410 before the token gate, with no cookie and no upstream call")
    def _():
        s = stub()
        p = started(review(to_stub(s.url)), "--expires-at", "2000-01-01T00:00:00Z")
        p._cookie = "review_session=made-up"  # an ended review hands out no cookie
        paths = ["/", "/__review/", "/__review/meta", "/__review/api/comments?project=x", "/api/orders", "/__review/reset", "/x/y?z=1", f"/__review/?t={p.token}", "/?t=wrong"]
        for m in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            for path in paths:
                for cookie in (True, False):
                    r = p.req(m, path, {"Authorization": "Bearer junk"}, cookie=cookie)
                    assert r.status == 410, (m, path, cookie, r.status)
                    assert r.headers["cache-control"] == "no-store" and "set-cookie" not in r.headers and "location" not in r.headers, (m, path, r.headers)
                    if m == "HEAD":
                        assert r.body == b""
                    else:
                        assert "<title>Review ended</title>" in r.text and "This review has ended" in r.text and '<html lang="en">' in r.text, r.text
        ws = raw(p.port, f"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {WS_KEY}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        assert ws.startswith(b"HTTP/1.1 410"), ws[:60]
        assert raw(p.port, b"GET http://other.example/ HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 410")
        assert s.hits == []

    @check("expiry: a past expiresAt in the config ends the review, --expires-at in the future wins over it")
    def _():
        past = lambda c: (c.pop("ttl", None), c.update(expiresAt="2000-01-01T00:00:00Z"))
        assert call(started(review(past)).port, "GET", "/__review/meta").status == 410
        assert started(review(past), "--expires-at", "2099-01-01T00:00:00Z").get("/__review/meta").status == 200

    @check("expiry: the review works before the end time and answers 410 after it (ttl counts from the proxy start)")
    def _():
        t0 = time.time()
        p = started(review(lambda c: c.update(ttl="3s")))
        j = p.get("/__review/meta").json()
        assert abs(utc(j["expiresAt"]) - (t0 + 3)) < 2.5, j
        assert p.get("/api/orders").status == 200
        time.sleep(max(0, t0 + 3.2 - time.time()))
        assert p.get("/__review/meta").status == 410 and p.get("/api/orders").status == 410
        assert call(p.port, "GET", f"/__review/?t={p.token}").status == 410

    # ---------- 503 ----------
    @check("paused: a closed upstream gives 503 with Retry-After, an HTML page for browsers and JSON for others")
    def _():
        dead = free_port()
        p = started(review(to_stub(f"http://127.0.0.1:{dead}")))
        r = p.get("/anything", headers={"Accept": "text/html,application/xhtml+xml"})
        assert r.status == 503 and r.headers["retry-after"] == "30" and r.headers["content-type"].startswith("text/html"), (r.status, r.headers)
        assert "<title>Review paused</title>" in r.text and "Review paused, try again later" in r.text and '<html lang="en">' in r.text
        assert str(dead) not in r.text and "127.0.0.1" not in r.text and "Traceback" not in r.text
        for h in ({}, {"Accept": "application/json"}, {"Accept": "*/*"}):
            r = p.get("/anything", headers=h)
            assert r.status == 503 and r.body == b'{"detail":"review paused","retryAfterSeconds":30}', (h, r.status, r.body)
            assert r.headers["content-type"] == JSON_CT and r.headers["retry-after"] == "30"
        assert p.req("HEAD", "/anything").status == 503
        p2 = started(review(combine(to_stub(f"http://127.0.0.1:{dead}"), lambda c: c.update(readOnly=False))))
        r = p2.post_json("/anything", {"a": 1})
        assert r.status == 503 and r.json()["detail"] == "review paused"

    @check("paused: an upstream that does not answer gives 503 after the timeout (proxy.upstreamTimeoutSeconds)")
    def _():
        def hang(h, n):
            time.sleep(6)
            return 200, "text/plain", b"late"
        s = stub(hang)
        p = started(review(combine(to_stub(s.url), lambda c: c["proxy"].update(upstreamTimeoutSeconds=1))))
        t0 = time.time()
        r = p.get("/slow", timeout=12)
        assert r.status == 503 and 0.8 <= time.time() - t0 < 4.5, (r.status, time.time() - t0)

    @check("paused: a WebSocket tunnel that cannot connect answers 503 JSON")
    def _():
        p = started(review(to_stub(f"http://127.0.0.1:{free_port()}")))
        out = raw(p.port, (f"GET /ws HTTP/1.1\r\nHost: x\r\nCookie: {p.cookie}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {WS_KEY}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head, _, body = out.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 503") and b"Retry-After: 30" in head, head
        assert json.loads(body) == {"detail": "review paused", "retryAfterSeconds": 30}

    @check("paused: /__review/session keeps its 502 login failed answer")
    def _():
        p = started(review(lambda c: [a.update(port=free_port()) for a in c["apps"] if a["name"] == "api"]))
        r = p.get("/__review/session")
        assert r.status == 502 and r.json() == {"error": "login failed"}, (r.status, r.body)

    @check("paused: an error after the response headers closes the connection and is not turned into a 503")
    def _():
        class Cut(http.server.BaseHTTPRequestHandler):  # a chunked body that stops before the last chunk
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"5\r\nshort\r\n")

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Cut)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cleanups.append(srv.shutdown)
        p = started(review(to_stub(f"http://127.0.0.1:{srv.server_address[1]}")))
        c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=10)
        c.request("GET", "/cut", headers={"Cookie": p.cookie})
        r = c.getresponse()
        assert r.status == 200
        try:
            r.read()
            raise AssertionError("expected a cut body")
        except http.client.IncompleteRead:
            pass
        finally:
            c.close()

    # ---------- wrapper ----------
    @check("wrapper: / and /index.html are rendered from the template with the generated cmt-config")
    def _():
        fx = json.loads((FIX / "review.json").read_text())
        p = started(review())
        r = p.get("/__review/")
        assert r.status == 200 and r.headers["content-type"].startswith("text/html") and '<iframe id="cmt-frame"' in r.text
        assert r.text.count('id="cmt-config"') == 1
        assert config_block(r.text) == {"project": "fixture-review", "title": "Fixture shop review", "version": "v1", "reviewNote": fx["reviewNote"],
                                         "apiBase": "/__review/api/comments", "startMode": "comment", "session": True,
                                         "frame": {"screens": fx["frame"]["screens"]}}
        for path in ("/__review/index.html", "/__review/%2e/index.html", "/__review//index.html"):
            assert p.get(path).body == r.body, path
        assert p.req("HEAD", "/__review/").body == b"" and p.req("POST", "/__review/").status == 405
        assert call(p.port, "GET", "/__review/").status == 403

    @check("wrapper: the proxy serves the canonical layer at /__review/layer.css and layer.js, and refuses to start without it")
    def _():
        layer = SKILL.parent / "mockup-review" / "assets" / "layer"
        p = started(review())
        for name, ctype in (("layer.css", "text/css"), ("layer.js", "text/javascript")):
            r = p.get("/__review/" + name)
            assert r.status == 200 and r.headers["content-type"].startswith(ctype) and r.body == (layer / name).read_bytes(), (name, r.status)
            assert call(p.port, "GET", "/__review/" + name).status == 403   # the token gate covers the layer too
        assert 'src="layer.js"' in p.get("/__review/").text
        d = TMP / "no-layer" / "live-app-review" / "scripts"   # a copy without the sibling skill mockup-review
        d.mkdir(parents=True)
        shutil.copy(PROXY, d / "review-proxy.py")
        shutil.copy(PROXY.with_name("review_config.py"), d / "review_config.py")
        shutil.copytree(SKILL / "assets", d.parent / "assets")
        r = subprocess.run([sys.executable, str(d / "review-proxy.py"), "--config", str(review() / "review.json"), "--check"], capture_output=True, text=True, timeout=20, env=BASE_ENV)
        assert r.returncode != 0 and "comment layer not found" in r.stderr and "mockup-review" in r.stderr, r.stderr

    @check("wrapper: a </script> in a note or a title cannot end the block, and the JSON round-trips")
    def _():
        evil = "</script><script>alert(1)</script><!-- \u00e9 \u2028 &"

        def edit(c):
            c.update(title="T " + evil[:30], reviewNote=evil, reviewVersion="r2")
            c["frame"] = {"screens": [{"id": "a-1", "title": "A", "src": "/a?x=1", "note": evil, "highlight": "a[href='</script>']"}]}
        html = started(review(edit)).get("/__review/").text
        tpl = TEMPLATE.read_text(encoding="utf-8")
        assert html.count("</script>") == tpl.count("</script>") and html.count("<script") == tpl.count("<script"), "extra script tag"
        cfg = config_block(html)
        s = cfg["frame"]["screens"][0]
        assert cfg["reviewNote"] == evil and s["note"] == evil and s["highlight"] == "a[href='</script>']" and cfg["version"] == "r2", cfg
        assert "\\u003c/script>" in html

    @check("wrapper: startMode, prefill and the session flag reach cmt-config")
    def _():
        step = {"selector": "#q", "value": "Quillfeather", "submit": "Enter", "delayMs": 2000, "waitForSelector": "#loaded"}

        def edit(c):
            c["startMode"] = "browse"
            c["frame"] = {"screens": [{"id": "a", "title": "A", "src": "/a", "prefill": [step]}, {"id": "b", "title": "B", "src": "/b"}]}
        cfg = config_block(started(review(edit)).get("/__review/").text)
        assert cfg["startMode"] == "browse" and cfg["session"] is True and cfg["frame"]["screens"][0]["prefill"] == [step] and "prefill" not in cfg["frame"]["screens"][1], cfg
        cfg = config_block(started(review(lambda c: c.pop("session"))).get("/__review/").text)
        assert cfg["startMode"] == "comment" and cfg["session"] is False, cfg

    @check("wrapper: static files stay served, but review.json, mocksFile, the comments file and dot paths are hidden, a stale index.html is ignored")
    def _():
        d = review(lambda c: c["proxy"].update(commentsFile="data/comments.json"),
                   {"extra.txt": "visible", ".hidden": "x", "index.html": "STALE-INDEX", "data/other.txt": "o"})
        p = started(d)
        assert p.post_json("/__review/api/comments", {"project": "fixture-review", "type": "comment", "id": "st000001", "text": "t", "author": "a"}).status == 201
        assert (d / "data" / "comments.json").is_file()
        r = p.get("/__review/extra.txt")
        assert r.status == 200 and r.text == "visible"
        assert p.get("/__review/data/other.txt").status == 200
        for path in ("/__review/review.json", "/__review/mocks.json", "/__review/data/comments.json", "/__review/.hidden", "/__review/.review/token",
                     "/__review/%2e%2e/review.json", "/__review/nothing"):
            assert p.get(path).status == 404, path
        assert "STALE-INDEX" not in p.get("/__review/").text

    @check("wrapper: a missing template is a clear startup error")
    def _():
        d = TMP / "no-template" / "scripts"
        d.mkdir(parents=True)
        shutil.copy(PROXY, d / "review-proxy.py")
        shutil.copy(PROXY.with_name("review_config.py"), d / "review_config.py")
        r = subprocess.run([sys.executable, str(d / "review-proxy.py"), "--config", str(review() / "review.json"), "--port", str(free_port())],
                           capture_output=True, text=True, timeout=20, env=BASE_ENV)
        assert r.returncode != 0 and "review-frame.html" in r.stderr and "Traceback" not in r.stderr, r.stderr

    # ---------- hardening after review ----------
    @check("hardening: a {param} never matches a dot segment, so allowWrite and mocks cannot be escaped with .. or %2e%2e")
    def _():
        s = stub()
        allow = [{"method": "POST", "path": "/api/items/{id}"}, {"method": "POST", "path": "/api/items/{id}/like"}]
        p = started(review(combine(to_stub(s.url), lambda c: c.update(allowWrite=allow, mocks=[{"path": "/api/m/{id}", "json": {"mock": 1}}]))))
        assert p.post_json("/api/items/5", {}).status == 200
        n = len(s.hits)
        for path in ("/api/items/..", "/api/items/%2e%2e", "/api/items/%2E%2E", "/api/items/.", "/api/items/..%2flike", "/api/items/%2e%2e/like", "/api/items/../like"):
            r = p.post_json(path, {})
            assert r.status == 403, (path, r.status)
        assert len(s.hits) == n, s.hits[n:]
        assert p.get("/api/m/7").json() == {"mock": 1} and p.get("/api/m/%2e%2e").status == 200 and p.get("/api/m/%2e%2e").json() != {"mock": 1}

    @check("hardening: a WebSocket upgrade that the app does not answer with 101 is a normal response and no byte tunnel")
    def _():
        s = stub()
        p = started(review(combine(to_stub(s.url), lambda c: c.update(readOnly=False))))
        c = socket.create_connection(("127.0.0.1", p.port), timeout=6)
        try:
            c.sendall((f"GET /api/x HTTP/1.1\r\nHost: x\r\nCookie: {p.cookie}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {WS_KEY}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            first = c.recv(65536)
            assert first.startswith(b"HTTP/1.1 200"), first[:60]
            try:
                c.sendall(f"DELETE /api/orders/1 HTTP/1.1\r\nHost: x\r\nCookie: {p.cookie}\r\nContent-Length: 0\r\n\r\n".encode())
                c.recv(65536)
            except OSError:
                pass
        finally:
            c.close()
        time.sleep(0.3)
        assert [h for h in s.hits if h[0] == "DELETE"] == [], s.hits

    @check("hardening: an open WebSocket tunnel is closed at expiry")
    def _():
        up = socket.socket()
        up.bind(("127.0.0.1", 0))
        up.listen(5)
        cleanups.append(up.close)

        def serve():
            while True:
                try:
                    conn, _a = up.accept()
                except OSError:
                    return
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                while True:
                    d = conn.recv(65536)
                    if not d:
                        break
                    conn.sendall(d)
        threading.Thread(target=serve, daemon=True).start()
        t0 = time.time()
        p = started(review(to_stub(f"http://127.0.0.1:{up.getsockname()[1]}")), "--expires-at", datetime.fromtimestamp(t0 + 4, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        c = socket.create_connection(("127.0.0.1", p.port), timeout=12)
        try:
            c.sendall((f"GET /ws HTTP/1.1\r\nHost: x\r\nCookie: {p.cookie}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {WS_KEY}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            assert c.recv(65536).startswith(b"HTTP/1.1 101")
            c.sendall(b"before")
            assert c.recv(100) == b"before"
            time.sleep(max(0, t0 + 5.5 - time.time()))
            got = b"x"
            try:
                c.sendall(b"after")
                got = c.recv(100)
            except OSError:
                got = b""
            assert got == b"", got  # closed, no echo
        finally:
            c.close()

    @check("hardening: read-only drops X-HTTP-Method-Override style headers and the _method parameter, and the proxy never forwards the headers")
    def _():
        seen = []
        s = stub(lambda h, n: (seen.append(h.headers.get("X-HTTP-Method-Override")), (200, "text/plain", b"ok"))[1])
        p = started(review(to_stub(s.url)))
        for h in ({"X-HTTP-Method-Override": "DELETE"}, {"X-HTTP-Method": "DELETE"}, {"X-Method-Override": "PUT"}):
            r = p.get("/api/items/1", headers=h)
            assert r.status == 403 and r.body == b'{"detail":"read-only review"}' and r.headers["x-review-read-only"] == "1", (h, r.status)
        for path in ("/api/items/1?_method=DELETE", "/api/items/1?a=1&_METHOD=put"):
            assert p.get(path).status == 403, path
        assert s.hits == [] and p.get("/api/items/1").status == 200
        p2 = started(review(combine(to_stub(s.url), lambda c: c.update(readOnly=False))))
        assert p2.get("/api/items/1", headers={"X-HTTP-Method-Override": "DELETE"}).status == 200
        assert seen == [None, None], seen  # the plain GET and the override GET: the header never reached the app

    @check("hardening: env files named in proxy.envFile and apps[].envFile are never served, even without a leading dot")
    def _():
        d = review(lambda c: (c["proxy"].update(envFile="review.env"), c["apps"][0].update(envFile="conf/app.env")),
                   {"review.env": "DB_PASSWORD=hunter2\n", "conf/app.env": "X_KEY=abc\n", "plain.txt": "visible"})
        p = started(d)
        for path in ("/__review/review.env", "/__review/conf/app.env", "/__review/conf/../review.env"):
            r = p.get(path)
            assert r.status == 404 and "hunter2" not in r.text, (path, r.status)
        assert p.get("/__review/plain.txt").status == 200

    @check("hardening: reset and appVersion commands do not inherit the login secrets, and a failing version command is logged")
    def _():
        probe = "import os\nopen('env_seen.txt', 'w').write(','.join(k for k in ('FIXTURE_PASS', 'FIXTURE_USER', 'REVIEW_TOKEN', 'PATH') if k in os.environ))\nprint('v1')\n"
        d = review(lambda c: c.update(appVersion={"command": ["python3", "probe.py"]}), {"probe.py": probe})
        p = started(d)
        assert p.get("/__review/meta").json()["version"] == "v1"
        assert (d / "env_seen.txt").read_text() == "PATH"
        d = review(lambda c: c.update(appVersion={"command": ["python3", "nothing-here.py"]}), {})
        p = started(d)
        assert p.get("/__review/meta").json()["version"] is None
        assert "appVersion command failed" in p.err.read_text() and "nothing-here" not in p.err.read_text()

    @check("hardening: --check reports a bad config and exits, and prints ok for a good one")
    def _():
        d = review()
        r = run_proxy(["--config", str(d / "review.json"), "--check"])
        assert r.returncode == 0 and r.stdout.strip() == "ok", (r.returncode, r.stdout, r.stderr)
        d = review(lambda c: c.update(mocks=[{"path": "/x/{a}", "by": "zz", "table": {}, "json": 1}]))
        r = run_proxy(["--config", str(d / "review.json"), "--check"])
        assert r.returncode != 0 and "by must name" in r.stderr and r.stdout == "", (r.returncode, r.stdout, r.stderr)

    # ---------- runtime flags ----------
    @check("flags: proxy.port, --port, publicHost, --public-host and proxy.idleTimeout set the port, the link and the idle limit")
    def _():
        a = stub(lambda h, n: (200, "text/plain", b"A"))
        d = review(combine(to_stub(a.url), lambda c: c["proxy"].update(idleTimeout=1)))
        cfg_port = json.loads((d / "review.json").read_text())["proxy"]["port"]
        p = started(d, port=False)  # no --port: proxy.port from the config
        assert p.port == cfg_port and p.get("/x").text == "A"
        assert p.link() == f"https://demo-node.example.test/__review/?t={p.token}", p.link()  # publicHost from the config
        assert p.err.read_text().startswith(f"review-proxy: listening on 127.0.0.1:{cfg_port}, comments in {(d / '.review' / 'comments.json').resolve()}")
        s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        t0 = time.time()
        try:
            assert s.recv(10) == b"" and time.time() - t0 < 4  # a silent connection is closed after the idle timeout
        finally:
            s.close()
        p.stop()
        p = started(d, "--public-host", "")  # an empty value turns the config host off (review.py sends it when Funnel is off)
        assert p.link() == f"http://127.0.0.1:{p.port}/__review/?t={p.token}" and p.port != cfg_port, p.link()
        p = started(d, "--public-host", "https://flag.example.test")
        assert p.link() == f"https://flag.example.test/__review/?t={p.token}" and p.token not in p.err.read_text()

    @check("flags: the token gate and cookie work, and --config is required, and a bad --expires-at is refused")
    def _():
        p = started(review())
        r = call(p.port, "GET", "/__review/")
        assert r.status == 403 and "no longer valid" in r.text
        assert call(p.port, "GET", "/__review/?t=wrong").status == 403
        r = call(p.port, "GET", f"/__review/?t={p.token}&keep=1")
        assert r.status == 302 and r.headers["location"] == "/__review/?keep=1"
        ck = r.headers["set-cookie"]
        assert ck.startswith("review_session=") and all(x in ck for x in ("HttpOnly", "Secure", "SameSite=Lax")), ck
        assert p.get("/").headers.get("x-frame-options") == "SAMEORIGIN"  # the fixture web app sends DENY
        assert p.get("/orders").status == 200 and p.get("/api/orders").json()["orders"]
        r = run_proxy(["--port", str(free_port())])
        assert r.returncode == 2 and "--config" in r.stderr, (r.returncode, r.stderr)
        r = run_proxy(["--config", str(review() / "review.json"), "--port", str(free_port())], env={"REVIEW_TOKEN": "short"})
        assert r.returncode != 0 and "REVIEW_TOKEN" in r.stderr
        for old in ("--review-dir", "--route", "--session-config", "--comments-file", "--idle-timeout", "--upstream-timeout", "--read-only"):
            assert run_proxy(["--config", str(review() / "review.json"), old, "x"]).returncode == 2, old   # the old flags are gone
        r = run_proxy(["--config", str(review() / "review.json"), "--port", str(free_port()), "--expires-at", "tomorrow"])
        assert r.returncode != 0 and "Traceback" not in r.stderr, r.stderr

    # ---------- config validation ----------
    # review_config.validate is the one validator. review.py and the proxy both call it. This table tests it once, in this process.
    sys.path.insert(0, str(SKILL / "scripts"))
    import review_config
    secret = "lit-" + secrets.token_hex(6)

    def pop(k):
        return lambda c: c.pop(k)

    def setk(**kw):
        return lambda c: c.update(kw)

    def pf(steps):
        return lambda c: c["frame"]["screens"][0].update(prefill=steps)

    CASES = [
        ("project missing", pop("project"), "project", 2),
        ("project name invalid", setk(project="Bad Name"), "project", 2),
        ("unknown top-level key", setk(unknownKey=1), "unknown key unknownKey", 2),
        ("unknown key in an app", lambda c: c["apps"][0].update(extra=1), "unknown key extra", 2),
        ("funnel.mode was removed", lambda c: c["funnel"].update(mode="external"), "funnel.mode was removed", 2),
        ("funnel port not allowed", lambda c: c["funnel"].update(port=444), "funnel.port", 2),
        ("title empty", setk(title=""), "title", 2),
        ("reviewVersion too long", setk(reviewVersion="v" * 21), "reviewVersion", 2),
        ("reviewNote too long", setk(reviewNote="n" * 301), "reviewNote", 2),
        ("publicHost not a host", setk(publicHost="bad host!"), "publicHost", 2),
        ("proxy.port out of range", lambda c: c["proxy"].update(port=70000), "proxy.port", 2),
        ("proxy.idleTimeout zero", lambda c: c["proxy"].update(idleTimeout=0), "proxy.idleTimeout", 2),
        ("proxy.upstreamTimeoutSeconds negative", lambda c: c["proxy"].update(upstreamTimeoutSeconds=-1), "proxy.upstreamTimeoutSeconds", 2),
        ("app name used twice", lambda c: c["apps"].append(dict(c["apps"][0])), "duplicate app name", 2),
        ("app name reserved", lambda c: c["apps"][0].update(name="proxy"), "reserved", 2),
        ("app port not an integer", lambda c: c["apps"][0].update(port="x"), "port", 2),
        ("routes missing", pop("routes"), "routes", 2),
        ("route to an unknown app", setk(routes={"/": "nope"}), "routes[/]", 2),
        ("route under /__review", setk(routes={"/": "web", "/__review": "web"}), "bad path prefix", 2),
        ("no default route", setk(routes={"/api": "api"}), 'needs the key "/"', 2),
        ("route to https", setk(routes={"/": "https://example.com"}), "routes[/]", 2),
        ("route with a path", setk(routes={"/": "http://127.0.0.1:9/base"}), "routes[/]", 2),
        ("mock under /__review", setk(mocks=[{"path": "/__review/x", "json": 1}]), "stay out of /__review", 2),
        ("mock without json or table", setk(mocks=[{"path": "/x"}]), "needs json or table", 2),
        ("mock status out of range", setk(mocks=[{"path": "/x", "status": 99, "json": 1}]), "status", 2),
        ("mock path without a slash", setk(mocks=[{"path": "x", "json": 1}]), "must start with /", 2),
        ("allowWrite with GET", setk(allowWrite=[{"method": "GET", "path": "/x"}]), "allowWrite[0]", 2),
        ("allowWrite bad path", setk(allowWrite=[{"method": "POST", "path": "orders"}]), "allowWrite[0]", 2),
        ("ttl and expiresAt together", setk(expiresAt="2099-01-01T00:00:00Z"), "not both", 2),
        ("ttl invalid", setk(ttl="7w"), "ttl", 2),
        ("expiresAt invalid", lambda c: (c.pop("ttl"), c.update(expiresAt="tomorrow")), "ISO 8601", 2),
        ("appVersion literal invalid", setk(appVersion="has space"), "appVersion", 2),
        ("appVersion command has the wrong type", setk(appVersion={"command": 5}), "appVersion", 2),
        ("reset.command empty", setk(reset={"command": []}), "reset.command", 2),
        ("reset.timeoutSeconds zero", setk(reset={"command": ["x"], "timeoutSeconds": 0}), "timeoutSeconds", 2),
        ("screen id used twice", setk(frame={"screens": [{"id": "a", "src": "/"}, {"id": "a", "src": "/"}]}), "duplicate id", 2),
        ("screen src is a network path", setk(frame={"screens": [{"id": "a", "src": "//evil.example/"}]}), "single /", 2),
        ("screen note too long", setk(frame={"screens": [{"id": "a", "src": "/", "note": "n" * 301}]}), "note", 2),
        ("readOnly not a boolean", setk(readOnly="yes"), "readOnly", 2),
        ("startMode not browse or comment", setk(startMode="view"), "startMode", 2),
        ("prefill not a list", pf("x"), "prefill must be a list", 2),
        ("prefill with 11 steps", pf([{"selector": "#a", "value": "v"}] * 11), "at most 10 steps", 2),
        ("prefill step without a value", pf([{"selector": "#a"}]), "value is required", 2),
        ("prefill step without a selector", pf([{"value": "v"}]), "selector is required", 2),
        ("prefill value not a string", pf([{"selector": "#a", "value": 5}]), "value must be a string", 2),
        ("prefill selector empty", pf([{"selector": "", "value": "v"}]), "selector must be a string", 2),
        ("prefill delayMs too large", pf([{"selector": "#a", "value": "v", "delayMs": 10001}]), "delayMs must be an integer from 0 to 10000", 2),
        ("prefill delayMs negative", pf([{"selector": "#a", "value": "v", "delayMs": -1}]), "delayMs", 2),
        ("prefill delayMs not an integer", pf([{"selector": "#a", "value": "v", "delayMs": 1.5}]), "delayMs", 2),
        ("prefill submit not allowed", pf([{"selector": "#a", "value": "v", "submit": "Space"}]), 'submit must be "Enter", "Tab" or "none"', 2),
        ("prefill unknown key", pf([{"selector": "#a", "value": "v", "typo": 1}]), "unknown key typo", 2),
        ("prefill click with a value", pf([{"click": "#a", "value": "v"}]), "unknown key value", 2),
        ("prefill click not a string", pf([{"click": 5}]), "click must be a string", 2),
        ("prefill waitForSelector not a string", pf([{"click": "#a", "waitForSelector": 5}]), "waitForSelector must be a string", 2),
        ("prefill step is not an object", pf(["#a"]), "must be an object", 2),
        ("prefill in the wrong place", lambda c: c.update(prefill=[]), "unknown key prefill", 2),
        ("session with a literal credential", lambda c: c["session"]["login"]["fields"].update(password=secret), "Literal values found for: password", 3),
        ("a secret-like key in an app env", lambda c: c["apps"][0]["env"].update(DB_PASSWORD="x"), "looks like a secret", 3),
    ]

    @check(f"config: validate refuses {len(CASES)} bad files with a clear message and the right exit code, and never prints a credential value")
    def _():
        review_config.validate(json.loads((FIX / "review.json").read_text()))   # the fixture itself is valid
        for name, edit, needle, code in CASES:
            cfg = json.loads((FIX / "review.json").read_text())
            edit(cfg)
            try:
                review_config.validate(cfg)
            except review_config.ConfigError as e:
                assert needle in str(e) and e.code == code and secret not in str(e), (name, e.code, str(e))
            else:
                raise AssertionError(name + ": the file was accepted")

    @check("config: startMode and prefill are accepted, and the normalized copy keeps them")
    def _():
        cfg = json.loads((FIX / "review.json").read_text())
        assert review_config.validate(cfg)["startMode"] == "comment"
        cfg["startMode"] = "browse"
        steps = [{"selector": "input[placeholder*='Search']", "value": "Quillfeather", "submit": "Enter", "delayMs": 2000, "waitForSelector": ".loaded"},
                 {"selector": "#a", "value": "v", "submit": "Tab"}, {"selector": "#b", "value": "v", "submit": "none", "delayMs": 0}, {"click": "[role=tab]", "waitForSelector": "#x"}]
        cfg["frame"]["screens"][1]["prefill"] = steps
        out = review_config.validate(cfg)
        assert out["startMode"] == "browse" and out["frame"]["screens"][1]["prefill"] == steps and "prefill" not in out["frame"]["screens"][2]

    def bad_cfg(name, edit, needle, files=None, env=None):   # what only the proxy can check: it builds the path patterns, the mock rules and the login
        @check(f"config error (proxy only): {name}")
        def _():
            d = review(edit, files)
            r = run_proxy(["--config", str(d / "review.json"), "--check"], env=env)
            assert r.returncode != 0, "the proxy started"
            assert needle in r.stderr and "Traceback" not in r.stderr and r.stdout == "", (r.stdout, r.stderr[-300:])

    bad_cfg("mock by not in path", setk(mocks=[{"path": "/x/{a}", "by": "b", "table": {}, "json": 1}]), "by must name")
    bad_cfg("mock table without by", setk(mocks=[{"path": "/x/{a}", "table": {"1": {"json": 1}}}]), "needs by")
    bad_cfg("mock table entry without json", setk(mocks=[{"path": "/x/{a}", "by": "a", "table": {"1": {"status": 200}}}]), "table entry")
    bad_cfg("mock parameter used twice", setk(mocks=[{"path": "/x/{a}/{a}", "json": 1}]), "used twice")
    bad_cfg("mocksFile missing", setk(mocksFile="nope.json"), "mocksFile")
    bad_cfg("mocksFile not an array", setk(mocksFile="obj.json"), "JSON array", files={"obj.json": "{}"})

    @check("config error (proxy only): a login variable that is missing is named without a value, a literal login value is refused, and an unreadable file gives a clean message")
    def _():
        r = run_proxy(["--config", str(review() / "review.json"), "--check"], env={"FIXTURE_USER": ""})
        assert "needs these environment variables" not in r.stderr   # --check does not read the login. Starting does
        r = run_proxy(["--config", str(review() / "review.json"), "--port", str(free_port())], env={"FIXTURE_USER": ""})
        assert r.returncode != 0 and "needs these environment variables: FIXTURE_USER" in r.stderr and "Traceback" not in r.stderr, r.stderr
        d = review(lambda c: c["session"]["login"]["fields"].update(password=secret))
        r = run_proxy(["--config", str(d / "review.json"), "--port", str(free_port())])
        assert r.returncode == 3 and secret not in r.stderr + r.stdout, (r.returncode, r.stderr)
        (d / "review.json").write_text("{not json")
        r = run_proxy(["--config", str(d / "review.json"), "--port", str(free_port())])
        assert r.returncode != 0 and "--config" in r.stderr and "Traceback" not in r.stderr, r.stderr
        r = run_proxy(["--config", str(d / "missing.json"), "--port", str(free_port())])
        assert r.returncode != 0 and "--config" in r.stderr and "Traceback" not in r.stderr, r.stderr

    # ---------- HTTP contract: gate, forwarding, WebSocket, static paths, comments API, link ----------
    app, api_srv = serve(AppHandler), serve(ApiHandler)
    cleanups.extend([app.shutdown, api_srv.shutdown])
    login = {"login": {"route": "/api", "method": "POST", "path": "/api/v1/auth/token", "encoding": "form",
                       "fields": {"username": "$REVIEW_LOGIN_USER", "password": "$REVIEW_LOGIN_PASS"}},
             "tokenField": "access_token", "store": [{"type": "localStorage", "key": "app_token", "value": "{token}"}]}

    def plain(c):   # no mocks, writes allowed: the proxy as a plain gate and forwarder
        c.update(routes={"/": f"http://127.0.0.1:{app.server_address[1]}", "/api": f"http://127.0.0.1:{api_srv.server_address[1]}"}, session=login, readOnly=False, mocks=[])
        c.pop("mocksFile", None)
    review_dir = review(plain)
    proxy = started(review_dir, "--public-host", "", env={"REVIEW_LOGIN_USER": LOGIN_USER, "REVIEW_LOGIN_PASS": LOGIN_PASS})   # plain HTTP, no public host
    http_contract(app, api_srv, proxy, review_dir)
    comments_contract()
    misc_contract()


if __name__ == "__main__":
    try:
        main()
    finally:
        for fn in reversed(cleanups):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(TMP, ignore_errors=True)
    failed = [n for n, e in results if e]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)
