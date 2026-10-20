"""`review.py verify`: check a running review like a reviewer would. Python 3.9+, standard library only.

Checks: dns (public only), gate, token, headers, and one tab:<id> row per screen.
Tabs render in headless Chromium over --remote-debugging-pipe (CDP on file descriptors 3 and 4, NUL separated messages).
The token and the cookie value never go into an output line, a file name or a process argument.
"""
import base64
import fcntl
import http.client
import json
import os
import select
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

COOKIE = "review_session"
META_KEYS = {"version", "expiresAt", "readOnly", "resetEnabled"}
DOH = "https://cloudflare-dns.com/dns-query"
CHROMES = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "/usr/bin/chromium"]
LOAD_JS = ("(function(){var f=document.getElementById('cmt-frame');try{var d=f.contentDocument;"
           "if(d.readyState==='complete'&&!/blank$/.test(d.location.pathname)){return d.body?d.body.innerText:''}}catch(e){}return null})()")


class _Tls(http.client.HTTPSConnection):
    """Connect to a fixed IP, but keep the host name for TLS (SNI and certificate checks) and the Host header."""

    def __init__(self, host, port, ip, **kw):
        super().__init__(host, port, **kw)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)


def find_chromium():
    for n in [os.environ.get("CHROMIUM")] + CHROMES:
        if n and shutil.which(n):
            return shutil.which(n)
    return None


class CdpError(Exception):
    pass


class Cdp:
    def __init__(self, argv):
        c2b_r, c2b_w = os.pipe()
        b2c_r, b2c_w = os.pipe()
        hi = lambda fd: fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)  # keep our ends away from the fixed numbers 3 and 4
        r, w = hi(c2b_r), hi(b2c_w)
        os.close(c2b_r)
        os.close(b2c_w)

        def child():
            os.dup2(r, 3)
            os.dup2(w, 4)

        # close_fds=False: Python fds are close-on-exec already, and the dup2 targets 3 and 4 must survive until exec.
        self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     close_fds=False, preexec_fn=child, start_new_session=True)
        os.close(r)
        os.close(w)
        self.w, self.r, self.buf, self.n, self.events = c2b_w, b2c_r, b"", 0, []

    def _recv(self, deadline):
        while b"\0" not in self.buf:
            left = deadline - time.time()
            if left <= 0:
                return None
            if select.select([self.r], [], [], left)[0]:
                chunk = os.read(self.r, 1 << 20)
                if not chunk:
                    raise CdpError("Chromium closed the pipe")
                self.buf += chunk
        msg, _, self.buf = self.buf.partition(b"\0")
        return json.loads(msg)

    def call(self, method, params=None, sid=None, timeout=30):
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        data = json.dumps(msg).encode() + b"\0"
        while data:
            data = data[os.write(self.w, data):]
        deadline = time.time() + timeout
        while True:
            m = self._recv(deadline)
            if m is None:
                raise CdpError(f"{method} timed out")
            if m.get("id") == self.n:
                if "error" in m:
                    raise CdpError(f"{method}: {m['error'].get('message')}")
                return m.get("result", {})
            self.events.append(m)

    def pump(self, seconds):
        end = time.time() + seconds
        while True:
            m = self._recv(end)
            if m is None:
                return
            self.events.append(m)

    def close(self):
        try:
            self.call("Browser.close", timeout=3)
        except Exception:  # noqa: BLE001 - the browser may close the pipe before it answers
            pass
        for fd in (self.w, self.r):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except OSError:
            pass
        self.proc.wait()


def console_errors(events, sid, start):
    out = []
    for m in events[start:]:
        if m.get("sessionId") != sid:
            continue
        p, text, url = m.get("params", {}), "", ""
        if m["method"] == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            text, url = (d.get("exception") or {}).get("description") or d.get("text", ""), d.get("url", "")
        elif m["method"] == "Runtime.consoleAPICalled" and p.get("type") == "error":
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
        elif m["method"] == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
            text, url = p["entry"].get("text", ""), p["entry"].get("url", "")
        else:
            continue
        if "favicon.ico" not in text and "favicon.ico" not in url:
            out.append((text or url).strip())
    return out


def render_tab(cdp, base, token, screen, shots):
    """Return (status, detail) for one screen."""
    sid = None
    tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
    try:
        sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            cdp.call(m, sid=sid)
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False}, sid)
        start = len(cdp.events)
        cdp.call("Page.navigate", {"url": f"{base}/__review/?t={token}#screen={screen['id']}"}, sid)
        text, end = None, time.time() + 20
        while text is None and time.time() < end:
            cdp.pump(0.25)
            text = cdp.call("Runtime.evaluate", {"expression": LOAD_JS, "returnByValue": True}, sid).get("result", {}).get("value")
        if text is not None:
            cdp.pump(0.5)
            text = cdp.call("Runtime.evaluate", {"expression": LOAD_JS, "returnByValue": True}, sid).get("result", {}).get("value") or text
        try:
            shot = cdp.call("Page.captureScreenshot", {"format": "png"}, sid)["data"]
            (shots / f"{screen['id']}.png").write_bytes(base64.b64decode(shot))
        except (CdpError, OSError):
            pass
        if text is None:
            return "FAIL", "the app frame did not load within 20 s"
        for bad in ("Review paused", "This review has ended"):
            if bad in text:
                return "FAIL", f"the frame shows: {bad}"
        errs = console_errors(cdp.events, sid, start)
        if errs:
            return "WARN", f"{len(errs)} console errors: {errs[0][:160]}"
        return "PASS", "loaded, no console errors"
    finally:
        try:
            cdp.call("Target.closeTarget", {"targetId": tid}, timeout=5)
        except CdpError:
            pass


GATE_WAIT = 60   # seconds a public verify keeps trying the gate. REVIEW_VERIFY_WAIT overrides it
GATE_STEP = 2


def wait_for_gate(attempt, limit, clock=time.time, sleep=time.sleep):
    """Call attempt() until it returns done, or it says retry is useless, or limit seconds pass. Returns the seconds waited."""
    t0 = clock()
    while True:
        done, retry = attempt()
        waited = clock() - t0
        if done or not retry or waited + GATE_STEP > limit:
            return waited
        sleep(GATE_STEP)


def run(cfg, st, token, local, as_json, shots):
    rows, cookie = [], [None]

    def add(cid, status, detail):
        for secret in (token, cookie[0]):
            if secret:
                detail = detail.replace(secret, "***")
        rows.append({"id": cid, "status": status, "detail": detail})

    host, fport, pport = st.get("publicHost"), (st.get("funnel") or {}).get("port", 443), st["proxyPort"]
    ip = mk = None
    if local:
        base, mk = f"http://127.0.0.1:{pport}", lambda: http.client.HTTPConnection("127.0.0.1", pport, timeout=15)
    else:
        base = f"https://{host}" + ("" if fport == 443 else f":{fport}")

    def get(path, headers=None):
        c = mk()
        try:
            c.request("GET", path, headers=headers or {})
            r = c.getresponse()
            body = r.read(100000)
            return r, body
        finally:
            c.close()

    gate = {"reached": False, "ok": False, "reason": "an earlier check failed"}

    def attempt():
        """One try of dns (public only) and the gate. Returns (done, retry). Funnel needs time to propagate after it turns on, so a public miss may pass later."""
        nonlocal ip, mk
        del rows[:]
        gate.update(reached=False, ok=False, reason="an earlier check failed")
        if not local:
            try:
                fs = st.get("funnel") or {}
                if not fs.get("on") or not host:
                    raise ValueError("Funnel is off for this review. Use --local, or run: review.py up --funnel")
                url = os.environ.get("REVIEW_DOH_URL", DOH)
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"name": host, "type": "A"})
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/dns-json"}), timeout=10) as r:
                    ans = json.loads(r.read())
                ip = next(a["data"] for a in ans.get("Answer", []) if a.get("type") == 1)
                add("dns", "PASS", f"{host} resolves to {ip}")
                mk = lambda: _Tls(host, fport, ip, timeout=15)
            except StopIteration:
                mk = None
                add("dns", "FAIL", f"no A record for {host}")
            except Exception as e:  # noqa: BLE001 - any failure is one FAIL row
                mk = None
                add("dns", "FAIL", str(e) if isinstance(e, ValueError) else f"DNS lookup failed: {type(e).__name__}")
                if isinstance(e, ValueError):
                    return False, False   # Funnel is off: waiting does not help
        if mk is None:
            return False, True
        try:
            r, _ = get("/__review/")
            gate.update(reached=True, ok=r.status == 403)
            add("gate", "PASS" if gate["ok"] else "FAIL", f"HTTP {r.status} without a token" + ("" if gate["ok"] else ", expected 403"))
        except (OSError, http.client.HTTPException) as e:
            gate["reason"] = f"connection failed: {type(e).__name__}"
            add("gate", "FAIL", gate["reason"])
        return gate["ok"], True

    waited = wait_for_gate(attempt, 0 if local else float(os.environ.get("REVIEW_VERIFY_WAIT", GATE_WAIT)))
    if waited >= 1:
        rows[-1]["detail"] += f" (waited {int(waited)} s for Funnel to propagate)"
    ok, reason = gate["reached"], gate["reason"]
    if ok:
        try:
            r, _ = get("/__review/?t=" + urllib.parse.quote(token))
            sc = next((v for k, v in r.getheaders() if k.lower() == "set-cookie" and v.startswith(COOKIE + "=")), "")
            cookie[0] = sc.split(";", 1)[0].split("=", 1)[1] if sc else None
            loc = urllib.parse.urlsplit(r.getheader("Location") or "")
            flags = [f for f in ("HttpOnly", "Secure", "SameSite=Lax") if f.lower() not in sc.lower()]
            probs = ([f"HTTP {r.status}, expected 302"] if r.status != 302 else []) + (["no review_session cookie"] if not sc else [f"cookie lacks {f}" for f in flags]) \
                + (["Location still has t"] if "t" in urllib.parse.parse_qs(loc.query) else [])
            add("token", "FAIL" if probs else "PASS", "; ".join(probs) or "302, cookie flags set, token removed from the URL")
            if cookie[0]:
                r, body = get("/__review/meta", {"Cookie": f"{COOKIE}={cookie[0]}"})
                try:
                    keys = set(json.loads(body))
                except (ValueError, TypeError):
                    keys = set()
                h = lambda n: (r.getheader(n) or "")
                probs = ([f"HTTP {r.status}, expected 200"] if r.status != 200 else []) + ([] if keys == META_KEYS else ["meta keys are wrong"]) \
                    + [f"{n} is wrong" for n, v in (("Referrer-Policy", "no-referrer"), ("X-Robots-Tag", "noindex"), ("Cache-Control", "no-store")) if v not in h(n).lower()]
                add("headers", "FAIL" if probs else "PASS", "; ".join(probs) or "meta and security headers are right")
            else:
                add("headers", "FAIL", "not run: no cookie")
                ok, reason = False, "the token check failed"
        except (OSError, http.client.HTTPException) as e:
            ok = False
            reason = f"connection failed: {type(e).__name__}"
            for cid in ("token", "headers"):
                if not any(r["id"] == cid for r in rows):
                    add(cid, "FAIL", "not run: " + reason)
    else:
        for cid in ("gate", "token", "headers"):
            if not any(r["id"] == cid for r in rows):
                add(cid, "FAIL", "not run: " + reason)
    shots.mkdir(parents=True, exist_ok=True)
    screens = cfg["frame"]["screens"]
    chrome = find_chromium()
    if not ok or not chrome:
        for s in screens:
            add(f"tab:{s['id']}", "FAIL", "Chromium not found. Set CHROMIUM" if ok else "not run: " + reason)
    else:
        tmp = tempfile.mkdtemp(prefix="review-verify-")
        argv = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", f"--user-data-dir={tmp}", "--remote-debugging-pipe"]
        if not local:
            rules = f"MAP {host} {ip}" + (f", MAP {host}:{fport} {ip}:{fport}" if fport != 443 else "")
            argv.append(f"--host-resolver-rules={rules}")
        cdp = None
        try:
            cdp = Cdp(argv)
            for s in screens:
                try:
                    status, detail = render_tab(cdp, base, token, s, shots)
                except (CdpError, OSError, ValueError) as e:
                    status, detail = "FAIL", f"browser error: {e}"
                add(f"tab:{s['id']}", status, detail)
        except OSError as e:
            for s in screens:
                add(f"tab:{s['id']}", "FAIL", f"Chromium did not start: {e.strerror}")
        finally:
            if cdp:
                cdp.close()
            shutil.rmtree(tmp, ignore_errors=True)
    p, f, w = (sum(r["status"] == k for r in rows) for k in ("PASS", "FAIL", "WARN"))
    report = {"ok": f == 0, "checks": rows}
    (shots / "report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    if as_json:
        print(json.dumps(report, indent=1))
    else:
        width = max(len(r["id"]) for r in rows)
        print(f"{'CHECK'.ljust(width)}  RESULT  DETAIL")
        for r in rows:
            print(f"{r['id'].ljust(width)}  {r['status'].ljust(6)}  {r['detail']}")
        print(f"verify: {p} passed, {f} failed, {w} warnings")
    return 0 if f == 0 else 1
