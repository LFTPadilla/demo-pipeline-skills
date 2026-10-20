"""Stub apps for the proxy and browser tests: a small single-page app, a form-login API, and tiny HTTP helpers.
They exist only in the test process. Python 3.9+, standard library only."""
import base64
import hashlib
import http.client
import http.server
import json
import secrets
import socket
import threading
import time
import urllib.parse

# Fake credentials for the stub login. They exist only in this process and in the environment of the child processes it starts.
LOGIN_USER = "tester"
LOGIN_PASS = "pw-" + secrets.token_urlsafe(12)
APP_TOKEN = "app-" + secrets.token_hex(10)


APP_SHELL = """<!doctype html><html><head><meta charset="utf-8"><title>Fixture app</title></head><body>
<div id="view"></div><p>token: <span id="token"></span></p>
<script>
const views = {
 '/': '<h1 id="home-title">Home page</h1><button id="go-orders">Go to orders</button><p id="home-text">Home paragraph</p><div id="home-box" style="margin-top:300px;height:120px;width:50%;background:#dde">Home box</div><div style="height:1500px"></div>',
 '/orders': '<h1 id="orders-title">Orders page</h1><button id="go-home">Back home</button><p id="orders-text">Orders paragraph</p>'
};
views['/hashapp#/a'] = '<h1 id="ha-title">Hash view A</h1><p id="ha-text">A text</p>';
views['/hashapp#/b'] = '<h1 id="hb-title">Hash view B</h1><p id="hb-text">B text</p>';
function render() { document.getElementById('view').innerHTML = views[location.pathname === '/hashapp' ? '/hashapp' + location.hash : location.pathname] || '<h1>Not found</h1>'; }
addEventListener('hashchange', render);
function go(p) { history.pushState({}, '', p); render(); }
document.addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  window.clicks = (window.clicks || 0) + 1;
  if (b.id === 'go-orders') go('/orders'); else if (b.id === 'go-home') go('/');
});
addEventListener('popstate', render);
try { document.getElementById('token').textContent = localStorage.getItem('app_token') || 'none'; } catch (e) {}
render();
</script></body></html>"""


class AppHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fixture"

    def log_message(self, *args):
        pass

    def send(self, status, body, ctype="text/html", headers=()):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or ""):
            out = b""
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if not size:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        self.server.hits.append((self.command, self.path))
        path = self.path.split("?")[0]
        if path in ("/", "/orders", "/settings", "/hashapp"):
            self.send(200, APP_SHELL)
        elif path == "/denied":
            self.send(200, "x", headers=[("X-Frame-Options", "DENY"), ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")])
        elif path == "/ok-frame":
            self.send(200, "x", headers=[("X-Frame-Options", "SAMEORIGIN"), ("Content-Security-Policy", "frame-ancestors 'self'")])
        elif path == "/redirect":
            self.send(302, "", headers=[("Location", f"http://127.0.0.1:{self.server.server_address[1]}/orders")])
        elif path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (b"one", b"two"):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                self.wfile.flush()
                time.sleep(0.7)
            self.wfile.write(b"0\r\n\r\n")
        elif path == "/ws" and (self.headers.get("Upgrade") or "").lower() == "websocket":
            accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.wfile.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
            self.close_connection = True
            while True:  # echo raw bytes: enough to prove the tunnel works in both directions
                data = self.connection.recv(1024)
                if not data:
                    return
                self.connection.sendall(b"echo:" + data)
        else:
            self.send(404, "not found")

    do_HEAD = do_GET

    def do_POST(self):
        self.server.hits.append((self.command, self.path))
        data = self.body()
        self.send(200, json.dumps({"method": self.command, "path": self.path, "body": data.decode(), "headers": {k.lower(): v for k, v in self.headers.items()}}), "application/json")


class ApiHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send(200, {"service": "api", "path": self.path})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        self.server.calls.append(form)
        if self.path == "/api/v1/auth/token" and form.get("username") == LOGIN_USER and form.get("password") == LOGIN_PASS:
            self.send(200, {"access_token": APP_TOKEN + self.server.suffix, "token_type": "bearer", "expires_in": self.server.expires})
        else:
            self.send(401, {"detail": "bad credentials"})


def serve(handler):
    """Start a stub server on a free port. It stops when the process ends."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.hits, server.calls = [], []
    server.expires, server.suffix = 3600, ""   # the login answer: lifetime in seconds, and a suffix that makes a renewed token different
    server.daemon_threads = True
    server.handle_error = lambda *_a: None   # a test that closes a connection early must not print a traceback
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def hreq(port, method, path, headers=None, body=None, chunked=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        c.request(method, path, body=body, headers=headers or {}, encode_chunked=chunked)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def raw_status(port, data, timeout=6):
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
    head, _, body = buf.partition(b"\r\n\r\n")
    return (int(head.split()[1]) if head else 0), body


def cookie_for(proxy):
    """The review_session cookie of a proxy that has .port and .token."""
    status, h, _ = hreq(proxy.port, "GET", f"/__review/?t={proxy.token}")
    assert status == 302, status
    return h["set-cookie"].split(";")[0]


def comments_api(proxy, cookie, method="GET", query="?project=api-test", body=None):
    data = json.dumps(body).encode() if body is not None else None
    if not query.startswith("?") and method in ("GET", "DELETE"):
        query = "?project=api-test" + query  # "&id=x" style: the project comes first
    hdr = {"Cookie": cookie, **({"Content-Type": "application/json"} if data else {})}
    status, _h, raw_body = hreq(proxy.port, method, "/__review/api/comments" + query, hdr, data)
    return status, json.loads(raw_body or b"null")
