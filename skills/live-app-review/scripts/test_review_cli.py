#!/usr/bin/env python3
"""Test of review.py (the live-app-review CLI). Python 3.9+, standard library only.

Run: python3 scripts/test_review_cli.py
Everything runs in temp folders, on 127.0.0.1 only. It starts the real fixture apps from tests/fixtures.
It never calls the real tailscale: a fake one (tests/fixtures/bin/tailscale) is first on PATH for every review.py call.
The proxy is a small stub (below), so this file does not depend on review-proxy.py.
Set CLI_TEST_REAL_PROXY=1 to use the real review-proxy.py instead of the stub.
`verify` tests drive headless Chromium ($CHROMIUM, else /usr/bin/chromium) over a pipe.
"""
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
FIX = SKILL / "tests" / "fixtures"
REVIEW = SCRIPTS / "review.py"
HOST = "demo-node.example.test"  # the fake tailscale answers with this name
results = []

STUB_PROXY = r'''
import argparse, http.server, json, os, secrets, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
ap = argparse.ArgumentParser()
ap.add_argument("--config"); ap.add_argument("--port", type=int); ap.add_argument("--public-host"); ap.add_argument("--expires-at"); ap.add_argument("--check", action="store_true")
a = ap.parse_args()
if a.check:
    print("ok"); sys.exit(0)
cfg = json.load(open(a.config))
token, cookie = os.environ["REVIEW_TOKEN"], secrets.token_hex(16)
apps = {x["name"]: x["port"] for x in cfg.get("apps", [])}
routes = sorted(((k, f"http://127.0.0.1:{apps[v]}" if v in apps else v.rstrip("/")) for k, v in cfg["routes"].items()), key=lambda kv: -len(kv[0]))
screens = cfg["frame"]["screens"]
expires = datetime.fromisoformat(a.expires_at.replace("Z", "+00:00")) if a.expires_at else None
WRAP = ("<!doctype html><html lang=en><head><meta charset=utf-8><title>stub</title></head><body style='margin:0'>"
        "<iframe id=cmt-frame style='width:100%;height:600px;border:0'></iframe><script>var S=" + json.dumps(screens).replace("<", "\\u003c") +
        ";var m=/screen=([^&]+)/.exec(location.hash);var id=m?decodeURIComponent(m[1]):S[0].id;"
        "document.getElementById('cmt-frame').src=S.filter(function(s){return s.id===id})[0].src;</script></body></html>")


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *x):
        pass

    def out(self, status, body, ctype="text/html; charset=utf-8", extra=()):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        for k, v in [("Content-Type", ctype), ("Content-Length", str(len(raw))), ("Referrer-Policy", "no-referrer"), ("X-Robots-Tag", "noindex"), ("Cache-Control", "no-store")] + list(extra):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if expires and datetime.now(timezone.utc) >= expires:
            return self.out(410, "<title>Review ended</title>This review has ended")
        if urllib.parse.parse_qs(u.query).get("t") == [token]:
            return self.out(302, "", extra=[("Location", u.path), ("Set-Cookie", f"review_session={cookie}; HttpOnly; Secure; SameSite=Lax; Path=/")])
        if f"review_session={cookie}" not in (self.headers.get("Cookie") or ""):
            return self.out(403, "Forbidden")
        if u.path == "/__review/meta":
            av = cfg.get("appVersion")
            return self.out(200, json.dumps({"version": av if isinstance(av, str) else None, "expiresAt": a.expires_at, "readOnly": cfg.get("readOnly", True),
                                             "resetEnabled": bool(cfg.get("reset"))}), "application/json; charset=utf-8")
        if u.path in ("/__review/", "/__review/index.html"):
            return self.out(200, WRAP)
        base = next((v for k, v in routes if u.path == k or u.path.startswith(k.rstrip("/") + "/")), None)
        try:
            with urllib.request.urlopen(base + self.path, timeout=10) as r:
                return self.out(r.status, r.read(), r.headers.get("Content-Type", "text/plain"))
        except urllib.error.HTTPError as e:
            return self.out(e.code, e.read(), e.headers.get("Content-Type", "text/plain"))
        except Exception:
            return self.out(503, "<title>Review paused</title>Review paused, try again later")

    do_HEAD = do_GET


srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H)
print(f"http://127.0.0.1:{a.port}/__review/?t={token}", flush=True)  # like the real proxy. review.py must send this to /dev/null
print(f"review-proxy: listening on 127.0.0.1:{a.port}", file=sys.stderr, flush=True)
srv.serve_forever()
'''


def check(name):
    def wrap(fn):
        t0 = time.time()
        try:
            fn()
            results.append((name, None))
            print(f"PASS  {name} ({time.time() - t0:.1f}s)", flush=True)
        except Exception as e:  # noqa: BLE001 - report every failure and go on
            results.append((name, e))
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:600]}", flush=True)
        return fn
    return wrap


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def busy(port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.3).close()
        return True
    except OSError:
        return False


def wait_for(fn, timeout=15, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is not alive
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def group_alive(pgid):
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                rest = (d / "stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(rest[2]) == pgid and rest[0] != "Z":
                return True
    return False


# The checks run when they are defined, so the stub proxy file must exist before the first one.
STUB_FILE = Path(tempfile.mkdtemp(prefix="lar-stub-")) / "stub_proxy.py"
STUB_FILE.write_text(STUB_PROXY)


class Env:
    """One temp review folder with the fixtures, a random fake login, ports, and a fake tailscale."""
    ts_user = None

    def __init__(self, funnel_port=None, edit=None):
        self.dir = Path(tempfile.mkdtemp(prefix="lar-cli-"))
        shutil.copytree(FIX, self.dir, ignore=shutil.ignore_patterns("bin", "__pycache__"), dirs_exist_ok=True)
        self.api, self.web, self.proxy = free_port(), free_port(), free_port()
        self.user, self.pw = "u" + secrets.token_hex(4), "p" + secrets.token_hex(8)
        (self.dir / ".fixture.env").write_text(f"FIXTURE_USER={self.user}\nFIXTURE_PASS={self.pw}\n")
        os.chmod(self.dir / ".fixture.env", 0o600)
        c = self.cfg()
        c["apps"][0]["port"], c["apps"][1]["port"], c["proxy"]["port"] = self.api, self.web, self.proxy
        if funnel_port:
            c["funnel"]["port"] = funnel_port
        if edit:
            edit(c)
        self.save(c)
        self.ts = self.dir / "ts"
        self.ts.mkdir()
        me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        self.env = dict(os.environ, PATH=f"{FIX / 'bin'}{os.pathsep}{os.environ['PATH']}", FAKE_TS_DIR=str(self.ts), FAKE_TS_OPERATOR=me)
        self.env.pop("REVIEW_TOKEN", None)
        if not os.environ.get("CLI_TEST_REAL_PROXY"):
            self.env["REVIEW_PROXY_SCRIPT"] = str(STUB_FILE)
        self.env.setdefault("CHROMIUM", "/usr/bin/chromium")
        self.pgids = set()

    def cfg(self):
        return json.loads((self.dir / "review.json").read_text())

    def save(self, c):
        (self.dir / "review.json").write_text(json.dumps(c, indent=1))

    def run(self, *args, env=None, timeout=90, cwd=None):
        e = dict(self.env, **(env or {}))
        r = subprocess.run([sys.executable, str(REVIEW), "-C", str(self.dir), *args], capture_output=True, text=True, timeout=timeout, env=e, cwd=cwd)
        self.track()
        return r

    def state(self):
        try:
            return json.loads((self.dir / ".review" / "state.json").read_text())
        except (OSError, ValueError):
            return None

    def track(self):
        st = self.state()
        for c in (st or {}).get("components", []):
            if c.get("pgid"):
                self.pgids.add(c["pgid"])

    def calls(self):
        f = self.ts / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

    def link(self, r):
        lines = r.stdout.splitlines()
        return lines[lines.index("Share link:") + 1]

    def cleanup(self):
        try:
            self.run("down", timeout=60)
        except Exception:  # noqa: BLE001
            pass
        for g in self.pgids:
            try:
                os.killpg(g, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)


def with_env(**kw):
    """Run the test body with a fresh Env. The body gets it as the only argument."""
    def deco(fn):
        def run():
            e = Env(**kw)
            try:
                fn(e)
            finally:
                e.cleanup()
        run.__name__ = fn.__name__
        run.__doc__ = fn.__doc__
        return run
    return deco


def named(name, **kw):
    def deco(fn):
        return check(name)(with_env(**kw)(fn))
    return deco


def no_ttl(c):
    c.pop("ttl", None)


# ---------------------------------------------------------------- tests
@named("lifecycle: up, status, logs, link, down", edit=None)
def _(e):
    r = e.run("up")
    assert r.returncode == 0, r.stderr + r.stdout
    link = e.link(r)
    assert link.startswith(f"http://127.0.0.1:{e.proxy}/__review/?t="), link
    st = e.state()
    assert [c["name"] for c in st["components"]] == ["api", "web", "proxy", "watchdog"], st
    assert "t=" not in json.dumps(st)
    s = e.run("status")
    assert s.returncode == 0, s.stdout + s.stderr
    for w in ("COMPONENT", "api", "web", "proxy", "funnel", "watchdog", "Expires:"):
        assert w in s.stdout, (w, s.stdout)
    js = json.loads(e.run("status", "--json").stdout)
    assert js["running"] is True and js["secondsLeft"] > 600000 and {c["name"] for c in js["components"]} == {"api", "web", "proxy", "watchdog"}, js
    assert e.run("link").stdout.strip() == link
    again = e.run("up")  # a second up changes nothing
    assert again.returncode == 0 and "Share link:" not in again.stdout and e.state()["components"] == st["components"], again.stdout
    lp = e.run("logs", "proxy", "-n", "5")
    assert lp.returncode == 0 and "listening" in lp.stdout, lp.stdout
    la = e.run("logs")
    assert "==> api <==" in la.stdout and "==> proxy <==" in la.stdout and "==> watchdog <==" in la.stdout, la.stdout
    d = e.run("down")
    assert d.returncode == 0, d.stderr
    assert e.state() is None and not (e.dir / ".review" / "token").exists() and not list((e.dir / ".review").glob("*.pid"))
    assert not busy(e.api) and not busy(e.web) and not busy(e.proxy)
    assert e.run("down").returncode == 0  # down is idempotent
    for cmd in ("status", "logs", "link", "verify"):
        assert e.run(cmd).returncode == 6, cmd


@named("detach: processes lead their own session and die with down")
def _(e):
    r = e.run("up")  # subprocess.run returns only when no child keeps the output pipes open
    assert r.returncode == 0, r.stderr
    st = e.state()
    for c in st["components"]:
        assert pid_alive(c["pid"]) and os.getpgid(c["pid"]) == c["pid"] == c["pgid"] and os.getsid(c["pid"]) == c["pid"], c
        assert c["startTicks"], c
    cmd = (Path(f"/proc/{[c for c in st['components'] if c['name'] == 'proxy'][0]['pid']}/cmdline").read_bytes().split(b"\0"))
    assert b"--expires-at" in cmd and b"--config" in cmd and f"{e.proxy}".encode() in cmd and e.state()["publicHost"] == HOST
    assert (e.dir / ".review" / "proxy.log").read_text().count("listening") == 1
    token = e.link(r).rsplit("t=", 1)[1]
    assert b"".join(cmd).find(token.encode()) < 0
    e.run("down")
    assert wait_for(lambda: not any(group_alive(c["pgid"]) for c in st["components"]), 5)


@named("up: health failure rolls back (exit 4) and shows the log")
def _(e):
    def bad(c):
        no_ttl(c)
        c["apps"][1]["command"] = ["python3", "-c", "import time; time.sleep(60)"]
        c["apps"][1]["health"]["timeoutSeconds"] = 2
    c = e.cfg()
    bad(c)
    e.save(c)
    r = e.run("up")
    assert r.returncode == 4 and "not healthy" in r.stderr, (r.returncode, r.stderr)
    assert e.state() is None and not busy(e.api) and not (e.dir / ".review" / "token").exists()
    assert all(not group_alive(g) for g in e.pgids), "a group is still alive"
    c["apps"][1]["command"] = ["python3", "-c", "import sys; print('boom line', file=sys.stderr); sys.exit(2)"]
    e.save(c)
    r = e.run("up")
    assert r.returncode == 4 and "exited early" in r.stderr and "boom line" in r.stderr, r.stderr
    assert not busy(e.api) and e.state() is None
    c["apps"][1]["command"] = ["definitely-not-a-command-xyz"]
    e.save(c)
    r = e.run("up")
    assert r.returncode == 4 and "could not start" in r.stderr and not busy(e.api), r.stderr


@named("up: env file with mode 0644 is refused (exit 3), no value printed")
def _(e):
    os.chmod(e.dir / ".fixture.env", 0o644)
    r = e.run("up")
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert f"chmod 600 {e.dir / '.fixture.env'}" in r.stderr
    assert e.pw not in r.stdout + r.stderr and e.user not in r.stdout + r.stderr
    assert not busy(e.api) and e.state() is None
    p = e.run("preflight", "--json")
    rows = {x["id"]: x for x in json.loads(p.stdout)["checks"]}
    assert p.returncode == 1 and rows[f"envfile:{e.dir / '.fixture.env'}"]["status"] == "FAIL"
    assert rows[f"envfile:{e.dir / '.fixture.env'}"]["fix"] == f"chmod 600 {e.dir / '.fixture.env'}"
    (e.dir / ".fixture.env").unlink()
    (e.dir / ".fixture.env").mkdir()
    r = e.run("up")
    assert r.returncode == 3 and "not a regular file" in r.stderr, r.stderr


@named("up: secret-like env key and literal session field are refused (exit 3)")
def _(e):
    c = e.cfg()
    c["apps"][0]["env"]["API_TOKEN"] = "x" + secrets.token_hex(4)
    e.save(c)
    r = e.run("up")
    assert r.returncode == 3 and "API_TOKEN" in r.stderr and c["apps"][0]["env"]["API_TOKEN"] not in r.stderr, r.stderr
    c = e.cfg()
    del c["apps"][0]["env"]["API_TOKEN"]
    lit = "lit" + secrets.token_hex(6)
    c["session"]["login"]["fields"]["password"] = lit
    e.save(c)
    r = e.run("up")
    assert r.returncode == 3 and "password" in r.stderr and lit not in r.stdout + r.stderr, r.stderr
    assert e.state() is None and not busy(e.api)


@named("config errors: unknown key, ttl, expiresAt, both (exit 2)")
def _(e):
    def tweak(fn):
        c = e.cfg()
        fn(c)
        e.save(c)
        return e.run("up")
    r = tweak(lambda c: c.__setitem__("bogusKey", 1))
    assert r.returncode == 2 and "bogusKey" in r.stderr, r.stderr
    r = tweak(lambda c: (c.pop("bogusKey", None), c["apps"][0].__setitem__("surprise", 1)))
    assert r.returncode == 2 and "surprise" in r.stderr, r.stderr
    r = tweak(lambda c: (c["apps"][0].pop("surprise"), c.__setitem__("ttl", "7x")))
    assert r.returncode == 2 and "ttl" in r.stderr, r.stderr
    r = tweak(lambda c: (c.pop("ttl"), c.__setitem__("expiresAt", (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))))
    assert r.returncode == 2 and "past" in r.stderr, r.stderr
    r = tweak(lambda c: c.__setitem__("ttl", "1h"))
    assert r.returncode == 2 and "not both" in r.stderr, r.stderr
    r = tweak(lambda c: (c.pop("ttl"), c.pop("expiresAt"), c["routes"].pop("/")))
    assert r.returncode == 2 and "routes" in r.stderr, r.stderr
    (e.dir / "review.json").write_text("{not json")
    assert e.run("up").returncode == 2
    (e.dir / "review.json").unlink()
    assert e.run("up").returncode == 2
    assert e.run("bogus-command").returncode == 2
    assert e.state() is None and not busy(e.api)


@named("preflight and up: a port in use fails (exit 1) and starts nothing")
def _(e):
    s = socket.socket()
    s.bind(("127.0.0.1", e.proxy))
    s.listen(1)
    try:
        r = e.run("up")
        assert r.returncode == 1 and "port:proxy" in r.stdout and f"port {e.proxy} is in use (proxy). Stop that process or change proxy.port" in r.stdout, r.stdout
        assert not busy(e.api) and e.state() is None
        assert e.run("preflight").returncode == 1
    finally:
        s.close()
    s = socket.socket()
    s.bind(("127.0.0.1", e.api))
    s.listen(1)
    try:
        r = e.run("preflight", "--json")
        rows = {x["id"]: x for x in json.loads(r.stdout)["checks"]}
        assert r.returncode == 1 and rows["port:api"]["status"] == "FAIL" and rows["port:web"]["status"] == "PASS", rows
        assert "(api)" in rows["port:api"]["fix"]
    finally:
        s.close()


@named("funnel: on and off through the fake tailscale, link and proxy args", funnel_port=8443)
def _(e):
    r = e.run("up", "--funnel")
    assert r.returncode == 0, r.stderr + r.stdout
    assert f"funnel --bg --https=8443 http://127.0.0.1:{e.proxy}" in e.calls(), e.calls()
    link = e.link(r)
    assert link.startswith(f"https://{HOST}:8443/__review/?t="), link
    assert e.state()["funnel"] == {"on": True, "port": 8443}
    s = e.run("status")
    assert re.search(r"funnel\s+funnel\s+-\s+8443\s+on", s.stdout), s.stdout
    assert e.run("link").stdout.strip() == link
    pid = [c for c in e.state()["components"] if c["name"] == "proxy"][0]["pid"]
    assert f"--public-host\0{HOST}:8443\0".encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
    d = e.run("down")
    assert d.returncode == 0 and "funnel --https=8443 off" in e.calls(), e.calls()
    assert not any("reset" in c or "serve" in c for c in e.calls())
    # --no-funnel wins over funnel.enabled in the config
    c = e.cfg()
    c["funnel"]["enabled"] = True
    e.save(c)
    n = len(e.calls())
    r = e.run("up", "--no-funnel")
    assert r.returncode == 0 and not any("--bg" in x for x in e.calls()[n:]) and e.link(r).startswith("http://127.0.0.1:"), r.stdout
    e.run("down")
    r = e.run("up")  # config enabled, no flag
    assert r.returncode == 0 and e.link(r).startswith("https://"), r.stdout


@named("funnel: publicHost null reads the DNS name from tailscale")
def _(e):
    c = e.cfg()
    c["publicHost"] = None
    e.save(c)
    r = e.run("up", "--funnel")
    assert r.returncode == 0 and e.link(r).startswith(f"https://{HOST}/__review/?t="), r.stdout + r.stderr
    assert "status --json" in e.calls()
    assert e.state()["publicHost"] == HOST
    e.run("down")
    assert "funnel --https=443 off" in e.calls()


OPERATOR_FIX = 'run "sudo tailscale set --operator=$USER" once.'
ME = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()


@named("funnel: without the Tailscale operator, preflight fails with the exact fix and up starts nothing. A funnel call that is refused exits 5 and rolls back")
def _(e):
    env = dict(FAKE_TS_NEED_OPERATOR="1")
    e.env.pop("FAKE_TS_OPERATOR")
    pf = json.loads(e.run("preflight", "--funnel", "--json").stdout)
    row = {x["id"]: x for x in pf["checks"]}["tailscale:operator"]
    assert pf["ok"] is False and row["status"] == "FAIL" and row["fix"] == OPERATOR_FIX, row
    r = e.run("up", "--funnel", env=env)
    assert r.returncode == 1 and f"fix: {OPERATOR_FIX}" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert e.state() is None and not busy(e.proxy) and not busy(e.api)
    r = e.run("up", "--funnel", "--skip-preflight", env=env)   # the node refuses the call itself
    assert r.returncode == 5 and OPERATOR_FIX in r.stderr, (r.returncode, r.stderr, r.stdout)
    assert e.state() is None and not busy(e.proxy) and not busy(e.api)
    assert not any("sudo" in x for x in e.calls())


@named("funnel: a failed funnel off stops everything (exit 5), keeps a funnel marker, and a later down retries")
def _(e):
    assert e.run("up", "--funnel").returncode == 0
    st = e.state()
    d = e.run("down", env=dict(FAKE_TS_FAIL_OFF="1"))
    assert d.returncode == 5 and "Funnel is still public" in d.stderr, d.stderr
    assert not busy(e.proxy) and not any(group_alive(c["pgid"]) for c in st["components"])
    left = e.state()
    assert left and left["funnel"]["on"] is True and left["funnel"]["stillPublic"] is True and left["components"] == [], left  # the marker: nothing runs, Funnel may be on
    up = e.run("up", env=dict(FAKE_TS_FAIL_OFF="1"))  # a new up must not start while Funnel is still on
    assert up.returncode == 5 and not busy(e.api), (up.returncode, up.stderr)
    d = e.run("down")
    assert d.returncode == 0 and e.state() is None, d.stderr
    assert e.calls()[-1].endswith("off") and (e.ts / "funnel.json").exists() is False


@named("watchdog: a failed funnel off at expiry keeps the marker, retries, and down finishes the job", edit=lambda c: c.__setitem__("ttl", "5s"))
def _(e):
    assert e.run("up", "--funnel", env=dict(FAKE_TS_FAIL_OFF="1")).returncode == 0
    wd = [c for c in e.state()["components"] if c["name"] == "watchdog"][0]
    assert wait_for(lambda: not busy(e.proxy), 20), "the watchdog did not stop the proxy"
    log = e.dir / ".review" / "watchdog.log"
    assert wait_for(lambda: "down failed (exit 5)" in log.read_text(), 10), log.read_text()
    left = e.state()
    assert left and left["funnel"]["on"] is True and (e.ts / "funnel.json").exists(), left
    d = e.run("down")  # no failure switch now
    assert d.returncode == 0 and e.state() is None and not (e.ts / "funnel.json").exists(), d.stderr
    assert wait_for(lambda: not pid_alive(wd["pid"]), 15), "the watchdog did not exit"


@named("preflight: one row per check, each fake switch gives the right result")
def _(e):
    def pf(*args, env=None, ret=None):
        r = e.run("preflight", "--json", *args, env=env)
        assert ret is None or r.returncode == ret, (r.returncode, r.stdout, r.stderr)
        d = json.loads(r.stdout)
        return r, {x["id"]: x for x in d["checks"]}
    r, rows = pf("--funnel", ret=0)
    want = {"config", f"envfile:{e.dir / '.fixture.env'}", "port:proxy", "port:api", "port:web", "tailscale:installed", "tailscale:login",
            "tailscale:funnel", "tailscale:port", "tailscale:operator", "tailscale:host"}
    assert want <= set(rows), set(rows) ^ want
    assert all(x["status"] == "PASS" for x in rows.values()), rows
    r, rows = pf("--no-funnel", ret=0)
    assert not [k for k in rows if k.startswith("tailscale:")], rows
    r, rows = pf("--funnel", env=dict(FAKE_TS_BACKEND="Stopped"), ret=1)
    assert rows["tailscale:login"]["status"] == "FAIL" and rows["tailscale:login"]["fix"] == "Run: tailscale up"
    r, rows = pf("--funnel", env=dict(FAKE_TS_NO_FUNNEL="1"), ret=1)
    assert rows["tailscale:funnel"]["fix"] == 'Funnel is off for this node. Turn on HTTPS and add the "funnel" node attribute in the tailnet policy.'
    r, rows = pf("--funnel", env=dict(FAKE_TS_PORTS="443,10000"), ret=0)
    assert rows["tailscale:port"]["status"] == "PASS"
    c = e.cfg()
    c["funnel"]["port"] = 8443
    e.save(c)
    r, rows = pf("--funnel", env=dict(FAKE_TS_PORTS="443,10000"), ret=1)
    assert rows["tailscale:port"]["status"] == "FAIL" and rows["tailscale:port"]["fix"] == "Funnel allows ports 443, 10000", rows["tailscale:port"]
    e.env.pop("FAKE_TS_OPERATOR")
    r, rows = pf("--funnel", ret=1)
    assert rows["tailscale:operator"]["status"] == "FAIL" and rows["tailscale:operator"]["fix"] == OPERATOR_FIX
    r, rows = pf("--funnel", env=dict(FAKE_TS_DENY_PREFS="1", FAKE_TS_OPERATOR="someone"), ret=1)
    assert rows["tailscale:operator"]["status"] == "FAIL"
    empty = Path(tempfile.mkdtemp(prefix="lar-nobin-"))
    try:
        r, rows = pf("--funnel", env=dict(PATH=str(empty)), ret=1)
        assert rows["tailscale:installed"]["fix"] == "Install Tailscale: https://tailscale.com/download", rows
    finally:
        shutil.rmtree(empty)
    c["publicHost"] = None
    e.save(c)
    r, rows = pf("--funnel", env=dict(FAKE_TS_OPERATOR=ME), ret=0)
    assert rows["tailscale:host"]["status"] == "PASS" and rows["tailscale:host"]["message"] == HOST
    t = e.run("preflight", "--funnel")  # the text table shows the fix under a FAIL
    assert "CHECK" in t.stdout and "RESULT" in t.stdout and f"fix: {OPERATOR_FIX}" in t.stdout, t.stdout
    c["bogus"] = 1
    e.save(c)
    r = e.run("preflight")
    assert r.returncode == 1 and "config" in r.stdout and "bogus" in r.stdout
    r = e.run("up")
    assert r.returncode == 2


@named("preflight: an external app must answer its health URL")
def _(e):
    c = e.cfg()
    del c["apps"][1]["command"]
    e.save(c)
    r = e.run("preflight", "--json")
    rows = {x["id"]: x for x in json.loads(r.stdout)["checks"]}
    assert r.returncode == 1 and rows["health:web"]["fix"] == f"app web is external and not reachable at http://127.0.0.1:{e.web}/health", rows["health:web"]
    assert "port:web" not in rows
    ext = subprocess.Popen([sys.executable, str(e.dir / "fixture_app.py"), "--mode", "web", "--port", str(e.web)], cwd=e.dir, start_new_session=True)
    try:
        assert wait_for(lambda: busy(e.web))
        r = e.run("up")
        assert r.returncode == 0, r.stderr + r.stdout
        web = [c for c in e.state()["components"] if c["name"] == "web"][0]
        assert web["managed"] is False and "pid" not in web
        assert "external" in e.run("status").stdout
        assert e.run("down").returncode == 0
        assert ext.poll() is None and busy(e.web), "down touched the external app"
    finally:
        ext.kill()
        ext.wait()


@named("expiry: ttl and expiresAt set the time, status shows the time left")
def _(e):
    c = e.cfg()
    c["ttl"] = "2h"
    e.save(c)
    r = e.run("up")
    assert r.returncode == 0, r.stderr
    exp = datetime.fromisoformat(e.state()["expiresAt"].replace("Z", "+00:00"))
    assert abs((exp - datetime.now(timezone.utc)).total_seconds() - 7200) < 60
    s = e.run("status")
    assert re.search(r"Expires: \S+Z \(in 1h \d+m\)", s.stdout), s.stdout
    js = json.loads(e.run("status", "--json").stdout)
    assert 7000 < js["secondsLeft"] <= 7200 and js["expiresAt"] == e.state()["expiresAt"]
    e.run("down")
    c.pop("ttl")
    c["expiresAt"] = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    e.save(c)
    assert e.run("up").returncode == 0
    assert re.search(r"Expires: \S+Z \(in 2h 5\dm\)", e.run("status").stdout), e.run("status").stdout
    e.run("down")
    c.pop("expiresAt")
    e.save(c)
    r = e.run("up")
    assert r.returncode == 0 and "Expires: never" in e.run("status").stdout
    assert "watchdog" not in [x["name"] for x in e.state()["components"]]


@named("watchdog: runs the down logic at expiry and turns funnel off", edit=lambda c: c.__setitem__("ttl", "6s"))
def _(e):
    r = e.run("up", "--funnel")
    assert r.returncode == 0, r.stderr + r.stdout
    st = e.state()
    wd = [c for c in st["components"] if c["name"] == "watchdog"][0]
    assert pid_alive(wd["pid"])
    assert wait_for(lambda: e.state() is None, 25), "state.json is still there after expiry"
    assert "funnel --https=443 off" in e.calls(), e.calls()
    assert not busy(e.api) and not busy(e.web) and not busy(e.proxy)
    assert wait_for(lambda: not pid_alive(wd["pid"]), 5), "watchdog did not exit"
    assert "expired: running down" in (e.dir / ".review" / "watchdog.log").read_text()
    assert not (e.dir / ".review" / "token").exists() and not list((e.dir / ".review").glob("*.pid"))


@named("stale state: status reports it, up cleans it and starts again", edit=no_ttl)
def _(e):
    assert e.run("up", "--funnel").returncode == 0
    st = e.state()
    for c in st["components"]:
        os.killpg(c["pgid"], signal.SIGKILL)
    assert wait_for(lambda: not any(pid_alive(c["pid"]) for c in st["components"]), 5)
    s = e.run("status")
    assert s.returncode == 1 and "stale: run review.py down" in s.stderr, (s.returncode, s.stderr)
    n = len(e.calls())
    r = e.run("up", "--funnel")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "funnel --https=443 off" in e.calls()[n:], "the stale cleanup did not turn funnel off"
    new = e.state()
    assert {c["pid"] for c in new["components"]}.isdisjoint({c["pid"] for c in st["components"]}) and e.run("status").returncode == 0
    # one dead component: up restarts everything
    web = [c for c in new["components"] if c["name"] == "web"][0]
    os.killpg(web["pgid"], signal.SIGKILL)
    assert wait_for(lambda: not pid_alive(web["pid"]), 5)
    assert e.run("status").returncode == 1
    r = e.run("up", "--funnel")
    assert r.returncode == 0 and e.run("status").returncode == 0, r.stderr
    api_new = [c for c in e.state()["components"] if c["name"] == "api"][0]
    assert api_new["pid"] != [c for c in new["components"] if c["name"] == "api"][0]["pid"]
    # down on a stale state also works
    for c in e.state()["components"]:
        os.killpg(c["pgid"], signal.SIGKILL)
    wait_for(lambda: not pid_alive(api_new["pid"]), 5)
    assert e.run("down").returncode == 0 and e.state() is None


@named("down never signals a recycled PID (start ticks differ)", edit=no_ttl)
def _(e):
    victim = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        run = e.dir / ".review"
        run.mkdir(mode=0o700)
        (run / "state.json").write_text(json.dumps({"version": 1, "startedAt": "2026-01-01T00:00:00Z", "expiresAt": None, "publicHost": None, "proxyPort": e.proxy,
                                                    "funnel": {"on": False, "port": 443},
                                                    "components": [{"name": "proxy", "kind": "proxy", "pid": victim.pid, "pgid": victim.pid, "startTicks": 1, "port": e.proxy}]}))
        s = e.run("status")
        assert s.returncode == 1 and "dead" in s.stdout, s.stdout
        d = e.run("down")
        assert d.returncode == 0 and e.state() is None
        time.sleep(0.3)
        assert victim.poll() is None, "down killed a process that is not ours"
    finally:
        victim.kill()
        victim.wait()


@named("lock: a second up or down fails with exit 1 while the lock is held")
def _(e):
    import fcntl
    (e.dir / ".review").mkdir(mode=0o700)
    fd = os.open(e.dir / ".review" / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert e.run("down").returncode == 1
        assert e.run("up").returncode == 1 and not busy(e.api)
    finally:
        os.close(fd)


@named("usage: -f needs a name, unknown names, cwd as review folder", edit=no_ttl)
def _(e):
    assert e.run("up").returncode == 0
    assert e.run("logs", "-f").returncode == 2
    r = e.run("logs", "nosuch")
    assert r.returncode == 2 and "nosuch" in r.stderr
    r = subprocess.run([sys.executable, str(REVIEW), "status"], cwd=e.dir, env=e.env, capture_output=True, text=True)
    assert r.returncode == 0 and "proxy" in r.stdout, r.stderr
    h = subprocess.run([sys.executable, str(REVIEW), "--help"], capture_output=True, text=True).stdout
    assert "_watchdog" not in h and "verify" in h


@named("logs -f follows a log until down", edit=no_ttl)
def _(e):
    assert e.run("up").returncode == 0
    p = subprocess.Popen([sys.executable, str(REVIEW), "-C", str(e.dir), "logs", "api", "-f"], stdout=subprocess.PIPE, text=True, env=e.env)
    try:
        time.sleep(1.2)
        marker = "follow-" + secrets.token_hex(4)
        with open(e.dir / ".review" / "app-api.log", "a") as f:
            f.write(marker + "\n")
        time.sleep(1.2)
        e.run("down")
        out, _ = p.communicate(timeout=15)
        assert marker in out and p.returncode == 0, out
    finally:
        p.kill()


@named("env files: parser rules and errors name the line, never the value")
def _(e):
    sys.path.insert(0, str(SCRIPTS))
    import review
    f = e.dir / "t.env"
    f.write_text("# c\n\nexport A=1\nB='two words'\nC=\"q\"\nD=x=y\nE='a\"\n")
    os.chmod(f, 0o600)
    assert review.parse_env_file(f) == {"A": "1", "B": "two words", "C": "q", "D": "x=y", "E": "'a\""}
    secret = "s3cret" + secrets.token_hex(4)
    f.write_text(f"A=1\n\nthis has {secret} in it\n")
    try:
        review.parse_env_file(f)
        raise AssertionError("no error")
    except review.Stop as x:
        assert x.code == 2 and "line 3" in str(x) and secret not in str(x), str(x)
    r = e.run("up")  # the same file through the CLI
    assert r.returncode == 0
    e.run("down")
    (e.dir / ".fixture.env").write_text(f"FIXTURE_USER={e.user}\nbad line {e.pw}\n")
    os.chmod(e.dir / ".fixture.env", 0o600)
    r = e.run("up")
    assert r.returncode == 2 and "line 2" in r.stderr and e.pw not in r.stderr + r.stdout, r.stderr


@named("verify --local: rows, one PNG per tab, console error WARN", edit=no_ttl)
def _(e):
    assert e.run("up").returncode == 0
    v = e.run("verify", "--local", timeout=180)
    assert v.returncode == 0, v.stdout + v.stderr
    for w in ("CHECK", "RESULT", "DETAIL", "gate", "token", "headers", "tab:home", "tab:orders", "tab:settings", "tab:errors", "verify:"):
        assert w in v.stdout, (w, v.stdout)
    shots = e.dir / ".review" / "verify"
    for sid in ("home", "orders", "settings", "errors"):
        png = (shots / f"{sid}.png").read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000, sid
    rep = json.loads((shots / "report.json").read_text())
    rows = {x["id"]: x for x in rep["checks"]}
    assert rep["ok"] is True and "dns" not in rows
    assert all(rows[k]["status"] == "PASS" for k in ("gate", "token", "headers")), rows
    assert rows["tab:errors"]["status"] == "WARN" and "fixture console error" in rows["tab:errors"]["detail"], rows["tab:errors"]
    assert all(rows[f"tab:{k}"]["status"] in ("PASS", "WARN") for k in ("home", "orders", "settings"))
    assert re.search(r"verify: \d+ passed, 0 failed, \d+ warnings", v.stdout)
    j = json.loads(e.run("verify", "--local", "--json", "--screenshots-dir", str(e.dir / "shots2"), timeout=180).stdout)
    assert j["ok"] and (e.dir / "shots2" / "errors.png").exists()
    token = e.run("link").stdout.strip().rsplit("t=", 1)[1]  # the token comes from the proxy environment, there is no token file
    assert not (e.dir / ".review" / "token").exists()
    assert e.run("verify", "--local", env=dict(REVIEW_TOKEN=token)).returncode == 0  # REVIEW_TOKEN wins when it is set
    bad = e.run("verify", "--local", "--json", env=dict(REVIEW_TOKEN="wrong-token"), timeout=60)  # a wrong token fails fast, with no tab waits
    rows = {x["id"]: x for x in json.loads(bad.stdout)["checks"]}
    assert bad.returncode == 1 and rows["token"]["status"] == "FAIL" and rows["tab:home"]["detail"].startswith("not run"), rows


def no_session(c):
    no_ttl(c)
    c.pop("session")


@named("R7: verify --local with the real proxy and no session block: the tab with no API call of its own has no console error", edit=no_session)
def _(e):
    e.env.pop("REVIEW_PROXY_SCRIPT", None)  # the stub proxy has no wrapper page. This test needs the real one
    assert e.run("up").returncode == 0
    v = e.run("verify", "--local", "--json", timeout=180)
    rows = {x["id"]: x for x in json.loads(v.stdout)["checks"]}
    assert v.returncode == 0 and rows["tab:settings"]["status"] == "PASS", v.stdout   # orders and home call the API without a login: 401 is their own warning


@named("verify --local: a paused page in the frame fails (exit 1)", edit=no_ttl)
def _(e):
    assert e.run("up").returncode == 0
    web = [c for c in e.state()["components"] if c["name"] == "web"][0]
    os.killpg(web["pgid"], signal.SIGKILL)  # the proxy now answers "Review paused" for every page
    assert wait_for(lambda: not busy(e.web), 5)
    v = e.run("verify", "--local", "--json", timeout=180)
    rows = {x["id"]: x for x in json.loads(v.stdout)["checks"]}
    assert v.returncode == 1 and rows["gate"]["status"] == "PASS", v.stdout
    assert rows["tab:home"]["status"] == "FAIL" and "Review paused" in rows["tab:home"]["detail"], rows["tab:home"]


@named("verify: public mode asks DoH, then connects to the IP (fake DoH, no TLS hop)", funnel_port=10000, edit=no_ttl)
def _(e):
    seen = {}

    class Doh(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["path"], seen["accept"] = self.path, self.headers.get("Accept")
            body = json.dumps({"Status": 0, "Answer": [{"name": HOST, "type": 5, "data": "alias.example."}, {"name": HOST, "type": 1, "data": "127.0.0.1"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/dns-json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Doh)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = dict(REVIEW_DOH_URL=f"http://127.0.0.1:{srv.server_port}/dns-query", REVIEW_VERIFY_WAIT="5")   # R6: 5 s instead of 60 s
        assert e.run("up", "--funnel").returncode == 0
        v = e.run("verify", "--json", env=env, timeout=60)
        assert v.returncode == 1, v.stdout  # nothing speaks TLS on the funnel port here
        rows = {x["id"]: x for x in json.loads(v.stdout)["checks"]}
        assert re.search(r"waited [3-6] s for Funnel to propagate", rows["gate"]["detail"]), rows["gate"]   # R6: the gate was retried, and the wait is reported
        assert rows["dns"]["status"] == "PASS" and "127.0.0.1" in rows["dns"]["detail"], rows["dns"]
        assert seen["accept"] == "application/dns-json" and f"name={HOST}" in seen["path"] and "type=A" in seen["path"], seen
        assert rows["gate"]["status"] == "FAIL" and rows["tab:home"]["status"] == "FAIL", rows
        e.run("down")
        assert e.run("up", "--no-funnel").returncode == 0  # funnel off: public verify refuses at the dns row
        v = e.run("verify", "--json", env=env, timeout=60)
        rows = {x["id"]: x for x in json.loads(v.stdout)["checks"]}
        assert v.returncode == 1 and rows["dns"]["status"] == "FAIL" and "Funnel is off" in rows["dns"]["detail"], rows["dns"]
        assert "waited" not in rows["dns"]["detail"], "Funnel off: waiting cannot help, so verify does not wait"
    finally:
        srv.shutdown()


@check("R6: wait_for_gate retries until the gate passes, and returns the seconds it waited")
def _():
    sys.path.insert(0, str(SCRIPTS))
    import review_verify
    now, calls = [0.0], []
    sleep = lambda n: now.__setitem__(0, now[0] + n)  # noqa: E731 - a fake clock: sleeping moves it
    clock = lambda: now[0]  # noqa: E731

    def late():  # the gate passes on the fourth try
        calls.append(now[0])
        return len(calls) >= 4, True
    assert review_verify.wait_for_gate(late, 60, clock, sleep) == 6.0 and len(calls) == 4, calls
    def never(retry):
        def attempt():
            calls.append(now[0])
            return False, retry
        return attempt
    calls.clear()
    now[0] = 0.0
    assert review_verify.wait_for_gate(never(True), 10, clock, sleep) == 10.0 and len(calls) == 6, calls   # never passes: it gives up at the limit
    calls.clear()
    now[0] = 0.0
    assert review_verify.wait_for_gate(never(False), 60, clock, sleep) == 0 and len(calls) == 1   # retry is useless: one try
    assert review_verify.wait_for_gate(lambda: (True, True), 60, clock, sleep) == 0   # first try ok: no wait
    assert review_verify.wait_for_gate(never(True), 0, clock, sleep) == 0   # limit 0 (local): one try
    assert review_verify.GATE_WAIT == 60


@named("secrets: token, password and cookie appear only where the spec allows", funnel_port=8443)
def _(e):
    outs = []
    r = e.run("up", "--funnel")
    assert r.returncode == 0, r.stderr
    link = e.link(r)
    token = link.rsplit("t=", 1)[1]
    allowed = {link}
    outs.append(("up", "\n".join(x for x in r.stdout.splitlines() if x not in allowed), r.stderr))
    # the cookie value comes from the stub proxy (or the real one)
    req = urllib.request.Request(f"http://127.0.0.1:{e.proxy}/__review/?t={token}")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=5)
    except urllib.error.HTTPError as x:
        cookie = re.match(r"review_session=([^;]+)", x.headers.get("Set-Cookie", "")).group(1)
    for args in (("status",), ("status", "--json"), ("logs",), ("logs", "proxy"), ("preflight", "--funnel"), ("preflight", "--json")):
        r = e.run(*args)
        outs.append((" ".join(args), r.stdout, r.stderr))
    r = e.run("link")
    assert r.stdout.strip() == link
    v = subprocess.Popen([sys.executable, str(REVIEW), "-C", str(e.dir), "verify", "--local"], env=e.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    leaked = False
    while v.poll() is None:  # no process may carry the token in its arguments
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                try:
                    if token.encode() in (d / "cmdline").read_bytes():
                        leaked = True
                except OSError:
                    pass
        time.sleep(0.2)
    so, se = v.communicate()
    outs.append(("verify", so, se))
    assert not leaked, "the token is in a process argument"
    pub = e.run("verify", env=dict(REVIEW_DOH_URL="http://127.0.0.1:9/x", REVIEW_VERIFY_WAIT="0"))
    outs.append(("verify public", pub.stdout, pub.stderr))
    outs.append(("down", *(lambda d: (d.stdout, d.stderr))(e.run("down"))))
    for name, so, se in outs:
        for secret, label in ((token, "token"), (e.pw, "password"), (cookie, "cookie")):
            assert secret not in so and secret not in se, f"{label} in the output of {name}"
    run = e.dir / ".review"
    assert stat.S_IMODE(run.stat().st_mode) == 0o700 and (run / ".gitignore").read_text().strip() == "*"
    assert not (run / "token").exists() and not (run / "state.json").exists()
    assert (run / "proxy.log").exists() and (run / "verify" / "report.json").exists()
    for f in run.rglob("*"):
        if f.is_file():
            raw = f.read_bytes()
            for secret, label in ((token, "token"), (e.pw, "password"), (cookie, "cookie")):
                assert secret.encode() not in raw, f"{label} in {f.relative_to(run)}"
    assert not any(token in x for x in e.calls())


@named("secrets: no file holds the token, modes are private", edit=no_ttl)
def _(e):
    r = e.run("up")
    run = e.dir / ".review"
    tok = e.link(r).rsplit("t=", 1)[1]
    assert not (run / "token").exists() and stat.S_IMODE((run / "state.json").stat().st_mode) == 0o600
    for f in run.iterdir():
        if f.is_file():
            assert tok.encode() not in f.read_bytes(), f.name
    for f in e.dir.rglob("*"):
        if f.is_file() and ".review" not in f.parts and f.name != "review.json":
            try:
                assert tok.encode() not in f.read_bytes(), f
            except OSError:
                pass
    assert r.returncode == 0


@named("up: an up that died half way leaves a stale state, and the next up starts everything", edit=no_ttl)
def _(e):
    slow = ("import runpy, sys, time; time.sleep(6); sys.argv = ['fixture_app.py', '--mode', 'web', '--port', sys.argv[1]]; "
            "runpy.run_path('fixture_app.py', run_name='__main__')")
    c = e.cfg()
    c["apps"][1]["command"] = ["python3", "-c", slow, "{port}"]
    e.save(c)
    p = subprocess.Popen([sys.executable, str(REVIEW), "-C", str(e.dir), "up"], env=e.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_for(lambda: e.state() and any(x["name"] == "web" for x in e.state()["components"]), 15), "up did not reach the slow app"
    p.kill()  # SIGKILL: no cleanup runs
    p.wait()
    e.track()
    st = e.state()
    assert st and not st.get("complete") and not any(x["name"] == "proxy" for x in st["components"]), st
    r = e.run("up", timeout=120)
    assert r.returncode == 0 and "Share link:" in r.stdout, (r.returncode, r.stdout, r.stderr)
    st = e.state()
    assert st["complete"] is True and [x["name"] for x in st["components"]] == ["api", "web", "proxy"], st
    assert busy(e.proxy) and busy(e.web) and busy(e.api)


@named("up: SIGTERM during start rolls back like Ctrl-C", edit=no_ttl)
def _(e):
    c = e.cfg()
    c["apps"][1]["command"] = ["python3", "-c", "import time; time.sleep(60)"]
    c["apps"][1]["health"]["timeoutSeconds"] = 60
    e.save(c)
    p = subprocess.Popen([sys.executable, str(REVIEW), "-C", str(e.dir), "up"], env=e.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_for(lambda: e.state() and any(x["name"] == "web" for x in e.state()["components"]), 15)
    e.track()
    p.terminate()
    p.wait(timeout=30)
    assert wait_for(lambda: e.state() is None, 5) and not busy(e.api), "state or app left behind"
    assert all(not group_alive(g) for g in e.pgids)


@named("up --funnel on a running review without Funnel is an error, not a silent no-op", edit=no_ttl)
def _(e):
    assert e.run("up").returncode == 0
    r = e.run("up", "--funnel")
    assert r.returncode == 1 and "Funnel off" in r.stderr and "review.py down" in r.stderr and "Share link:" not in r.stdout, (r.returncode, r.stderr)
    assert not any("--bg" in x for x in e.calls()) and e.state()["funnel"]["on"] is False
    assert e.run("up", "--no-funnel").returncode == 0 and e.run("up").returncode == 0


@named("down: kills the children of an app whose leader died first", edit=no_ttl)
def _(e):
    (e.dir / "w.py").write_text("import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-m', 'http.server', sys.argv[1], '--bind', '127.0.0.1'])\ntime.sleep(600)\n")
    c = e.cfg()
    c["apps"][1]["command"] = ["python3", "w.py", "{port}"]
    c["apps"][1]["health"]["url"] = "http://127.0.0.1:{port}/"
    e.save(c)
    assert e.run("up").returncode == 0
    web = [x for x in e.state()["components"] if x["name"] == "web"][0]
    os.kill(web["pid"], signal.SIGKILL)  # the leader dies, the http.server child lives on
    assert wait_for(lambda: not pid_alive(web["pid"]), 5) and busy(e.web)
    d = e.run("down")
    assert d.returncode == 0, d.stderr
    assert wait_for(lambda: not busy(e.web), 8), "the orphan child still listens"
    assert not group_alive(web["pgid"]) and e.state() is None


@named("config: the names proxy, watchdog and funnel are reserved for apps (exit 2)")
def _(e):
    for name in ("proxy", "watchdog", "funnel"):
        c = e.cfg()
        c["apps"][0]["name"] = name
        e.save(c)
        r = e.run("up")
        assert r.returncode == 2 and "reserved" in r.stderr and not busy(e.web), (name, r.returncode, r.stderr)


@named("preflight and up: a mock rule that only the proxy rejects fails before any app starts")
def _(e):
    c = e.cfg()
    c["mocks"] = [{"path": "/x/{id}", "by": "zz", "table": {}, "json": 1}]
    e.save(c)
    real = dict(REVIEW_PROXY_SCRIPT=str(SCRIPTS / "review-proxy.py"))
    p = e.run("preflight", env=real)
    assert p.returncode == 1 and "proxy-config" in p.stdout and "by must name" in p.stdout, p.stdout
    u = e.run("up", env=real)
    assert u.returncode == 1 and "by must name" in u.stdout and not busy(e.api) and e.state() is None, (u.returncode, u.stdout)


@named("config: an argument that looks like a secret gives a warning without the value")
def _(e):
    val = "v" + secrets.token_hex(6)
    c = e.cfg()
    c["reset"]["command"] = ["python3", "fixture_reset.py", "--db-password=" + val]
    c["appVersion"] = {"command": ["git", "--token=" + val]}
    e.save(c)
    r = e.run("preflight")
    assert "reset.command has an argument that looks like a secret (--db-password=...)" in r.stderr and "appVersion.command" in r.stderr, r.stderr
    assert val not in r.stdout + r.stderr



# ---------------------------------------------------------------- token reuse and Funnel failures
def gate_status(port, token):
    """HTTP status of the proxy for ?t=<token>, without following the redirect."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        return urllib.request.build_opener(NoRedirect).open(f"http://127.0.0.1:{port}/__review/?t={token}", timeout=5).status
    except urllib.error.HTTPError as x:
        return x.code


@named("R1 and R4: up uses REVIEW_TOKEN, prints that token, and writes it to no file", edit=no_ttl)
def _(e):
    tok = secrets.token_urlsafe(24)
    r = e.run("up", env=dict(REVIEW_TOKEN=tok))
    assert r.returncode == 0, r.stderr + r.stdout
    assert e.link(r) == f"http://127.0.0.1:{e.proxy}/__review/?t={tok}", e.link(r)
    assert gate_status(e.proxy, tok) == 302 and gate_status(e.proxy, "x" * 32) == 403, "the proxy does not use the token from the environment"
    assert e.run("link").stdout.strip() == e.link(r)  # read back from the proxy environment, not from REVIEW_TOKEN
    for f in e.dir.rglob("*"):
        if f.is_file() and f.name != "review.json":
            assert tok.encode() not in f.read_bytes(), f
    assert tok not in r.stderr
    assert e.run("down").returncode == 0
    again = e.run("up", env=dict(REVIEW_TOKEN=tok))  # a migration: the old link works again
    assert again.returncode == 0 and e.link(again) == e.link(r), again.stderr
    assert e.run("down").returncode == 0
    first = e.run("up")  # no REVIEW_TOKEN: a new random token for each up
    e.run("down")
    second = e.run("up")
    assert e.link(first) != e.link(second) and tok not in e.link(first) + e.link(second)


@named("R1: a REVIEW_TOKEN with a bad format is refused (exit 2) before anything starts", edit=no_ttl)
def _(e):
    for bad in ("short", "has space in it 123456789", "x" * 201):
        r = e.run("up", env=dict(REVIEW_TOKEN=bad))
        assert r.returncode == 2 and "REVIEW_TOKEN must have" in r.stderr and bad not in r.stderr + r.stdout, (bad, r.returncode, r.stderr)
        assert e.state() is None and not busy(e.api) and not busy(e.proxy)


@named("funnel: a permission failure in down prints the sudo line, keeps a marker, and status warns", edit=no_ttl)
def _(e):
    assert e.run("up", "--funnel").returncode == 0
    d = e.run("down", env=dict(FAKE_TS_DENY_OFF="1"))
    sudo = "sudo tailscale funnel --https=443 off"
    assert d.returncode == 5 and f"Run: {sudo}" in d.stderr and "still public" in d.stderr, (d.returncode, d.stderr)
    assert "Funnel is off" not in d.stdout + d.stderr and "turned off" not in d.stdout + d.stderr
    assert not busy(e.proxy) and (e.ts / "funnel.json").exists()
    st = e.state()
    assert st and st["funnel"]["stillPublic"] is True and st["funnel"]["on"] is True and st["components"] == [], st
    s = e.run("status")
    assert s.returncode == 1 and f"Funnel still public, run: {sudo}" in s.stderr and "on (still public)" in s.stdout, (s.returncode, s.stdout, s.stderr)
    assert json.loads(e.run("status", "--json").stdout)["funnel"]["stillPublic"] is True
    d = e.run("down")  # after the human ran the sudo line, or when the permission is back
    assert d.returncode == 0 and e.state() is None and not (e.ts / "funnel.json").exists(), d.stderr


@named("watchdog: a permission failure at expiry logs the sudo line and keeps the marker", edit=lambda c: c.__setitem__("ttl", "5s"))
def _(e):
    assert e.run("up", "--funnel", env=dict(FAKE_TS_DENY_OFF="1")).returncode == 0
    log = e.dir / ".review" / "watchdog.log"
    assert wait_for(lambda: log.exists() and "Run: sudo tailscale funnel --https=443 off" in log.read_text(), 25), log.read_text() if log.exists() else "no log"
    assert not busy(e.proxy)
    st = e.state()
    assert st and st["funnel"]["stillPublic"] is True and (e.ts / "funnel.json").exists(), st
    assert e.run("down").returncode == 0 and e.state() is None  # the failure switch is off now


@named("state of an older version: a Funnel that review.py did not turn on is still turned off by down, and link still prints the public link", edit=no_ttl)
def _(e):
    assert e.run("up").returncode == 0
    st = e.state()
    st["funnel"] = {"on": False, "port": 443, "mode": "external"}   # what the removed external mode wrote
    st["publicHost"] = HOST
    (e.dir / ".review" / "state.json").write_text(json.dumps(st))
    (e.ts / "funnel.json").write_text(json.dumps({"port": 443, "target": f"http://127.0.0.1:{e.proxy}"}))
    assert e.run("link").stdout.strip().startswith(f"https://{HOST}/__review/?t=")
    d = e.run("down")
    assert d.returncode == 0 and e.state() is None and "funnel --https=443 off" in e.calls() and not (e.ts / "funnel.json").exists(), d.stderr
    assert not busy(e.proxy)
    c = e.cfg()
    c["funnel"]["mode"] = "external"   # the old review.json is refused with a message that says what to do
    e.save(c)
    r = e.run("up")
    assert r.returncode == 2 and "funnel.mode was removed" in r.stderr and OPERATOR_FIX.rstrip(".") in r.stderr, r.stderr


failed = [(n, e) for n, e in results if e]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
shutil.rmtree(STUB_FILE.parent, ignore_errors=True)
sys.exit(1 if failed else 0)
