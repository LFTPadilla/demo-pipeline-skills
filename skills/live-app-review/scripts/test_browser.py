#!/usr/bin/env python3
"""Browser tests of live-app-review in one run: the comment layer in frame mode, the real proxy with a stub app, and the whole stack.

Run: uv run --with playwright python scripts/test_browser.py
Needs: Chromium (bundled with Playwright, else /usr/bin/chromium).
Everything stays on 127.0.0.1 in temp folders. The fake tailscale (tests/fixtures/bin/tailscale) is first on PATH for the end-to-end part,
so no real tailnet, Docker, Cloudflare, or wrangler is touched. Logins and tokens are random values made at run time.

1. Layer: a small stub plays the proxy (wrapper, meta, reset, comments API, read-only 403). It tests the layer UI: empty space, the panel
   list, tab notes, highlights, viewport, app version, read-only toast, reset button, name, reactions, cross-origin banner.
2. Frame: the real review-proxy.py in front of a stub single-page app and a stub login API: pins, scroll, navigation, auto-login, hash routes.
3. End to end: review.py, the real proxy, the fixture apps. Phase 1 has no Funnel. Phase 2 has Funnel on and a short ttl: expiry, watchdog.
"""
import calendar
import copy
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
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

SKILL = Path(__file__).resolve().parent.parent
MOCKUP = SKILL.parent / "mockup-review"
FIXTURES = FIX = SKILL / "tests" / "fixtures"
sys.path.insert(0, str(FIX))
from stub_apps import (LOGIN_PASS, LOGIN_USER, APP_TOKEN, AppHandler, ApiHandler, comments_api, cookie_for, hreq, serve)  # noqa: E402

PROXY = SKILL / "scripts" / "review-proxy.py"
REVIEW = SKILL / "scripts" / "review.py"
FRAME_TEMPLATE = SKILL / "assets" / "template" / "review-frame.html"
STATIC_TEMPLATE = MOCKUP / "assets" / "template" / "index.html"
EXPECT_TIMEOUT = 10000
results = []


def check(name):
    """Register a test. A failure is recorded and the run continues."""
    def wrap(fn):
        t0 = time.time()
        try:
            fn()
            results.append((name, None))
            print(f"PASS  {name} ({time.time() - t0:.1f}s)", flush=True)
        except Exception as e:  # noqa: BLE001 - report every failure
            results.append((name, e))
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:600]}", flush=True)
        return fn
    return wrap


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def open_panel(page):
    if not page.locator(".cmt-panel.cmt-open").count():
        page.get_by_role("button", name=re.compile(r"^Comments \(\d+\)$")).click()


def pin_center(page, i=0):
    return page.evaluate("i=>{const r=document.querySelectorAll('.cmt-pin')[i].getBoundingClientRect();return [r.left+r.width/2+scrollX,r.top+r.height/2+scrollY]}", i)


# ================================================================ 1. Layer in frame mode, with a stub for the proxy
SCREENS = [
    {"id": "home", "title": "Home", "src": "/", "note": "Start here. Press C to comment."},
    {"id": "orders", "title": "Orders", "src": "/orders", "highlight": "#order-1"},
    {"id": "settings", "title": "Settings", "src": "/settings", "note": "Saving is blocked in this review.", "highlight": "#save-settings"},
    {"id": "errors", "title": "Errors", "src": "/errors"},
]
PAGES = {"/": "index.html", "/orders": "orders.html", "/settings": "settings.html", "/errors": "errors.html", "/search": "search.html"}


# ---- Stub of the proxy ----
class Stub:
    """One stub server. Tests change its fields before they open a page."""

    def __init__(self):
        self.screens = copy.deepcopy(SCREENS)
        self.meta = {"version": "new2222", "expiresAt": None, "readOnly": True, "resetEnabled": False}
        self.meta_status = 200
        self.comments = []          # served by the comments API
        self.posts = []             # every POST/PATCH body of the comments API
        self.resets = []            # headers of every POST /__review/reset
        self.reset_answer = (200, {"ok": True, "durationMs": 5})
        self.sessions = 0
        self.has_session = True     # like a review.json with a session block: the wrapper then calls /__review/session
        self.start_mode = None
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
                data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw) if raw else {}
                except ValueError:
                    return {}

            def wrapper(self):
                cfg = {"project": "fixture-review", "title": "Fixture shop review", "version": "v1", "reviewNote": "Sample data only.",
                       "apiBase": "/__review/api/comments", "frame": {"screens": stub.screens}}
                if stub.has_session is not None:   # None plays an older proxy: it sends no flag
                    cfg["session"] = stub.has_session
                if stub.start_mode:
                    cfg["startMode"] = stub.start_mode
                text = json.dumps(cfg).replace("<", "\\u003c")
                html = FRAME_TEMPLATE.read_text(encoding="utf-8")
                html = re.sub(r'(<script type="application/json" id="cmt-config">).*?(</script>)', lambda m: m.group(1) + text + m.group(2), html, count=1, flags=re.S)
                self.send(200, html, "text/html; charset=utf-8")

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in ("/__review/", "/__review/index.html"):
                    return self.wrapper()
                if path in ("/__review/layer.css", "/__review/layer.js"):   # the real proxy serves the same two files
                    return self.send(200, (MOCKUP / "assets" / "layer" / path.rsplit("/", 1)[1]).read_bytes(), "text/css" if path.endswith(".css") else "text/javascript")
                if path == "/__review/meta":
                    return self.send(stub.meta_status, stub.meta if stub.meta_status == 200 else {"error": "x"})
                if path == "/__review/session":
                    stub.sessions += 1
                    return self.send(200, {"store": []})
                if path == "/__review/api/comments":
                    return self.send(200, {"comments": stub.comments})
                if path in PAGES:
                    return self.send(200, (FIXTURES / "app" / PAGES[path]).read_bytes(), "text/html; charset=utf-8")
                if path == "/api/me":
                    return self.send(401, {"detail": "no"})
                return self.send(404, {"detail": "not found"})

            def do_POST(self):
                path = urlsplit(self.path).path
                if path == "/__review/api/comments":
                    b = self.body()
                    stub.posts.append(("POST", b))
                    if b.get("type") == "reply":
                        for c in stub.comments:
                            if c["id"] == b.get("commentId"):
                                c["replies"].append({k: b[k] for k in ("id", "author", "text", "createdAt")})
                    else:
                        c = {k: v for k, v in b.items() if k not in ("type", "project")}
                        c.setdefault("replies", [])
                        stub.comments.append(c)
                    return self.send(200, {"ok": True})
                if path == "/__review/reset":
                    stub.resets.append({k.lower(): v for k, v in self.headers.items()})
                    status, body = stub.reset_answer
                    return self.send(status, body)
                if path == "/api/settings":
                    self.body()
                    return self.send(403, {"detail": "read-only review"}, extra={"X-Review-Read-Only": "1"})
                if path == "/api/forbidden":
                    self.body()
                    return self.send(403, {"detail": "no"})
                return self.send(404, {"detail": "not found"})

            def do_PATCH(self):
                if urlsplit(self.path).path == "/__review/api/comments":
                    b = self.body()
                    stub.posts.append(("PATCH", b))
                    for c in stub.comments:
                        if c["id"] == b.get("id") and "resolved" in b:
                            c["resolved"] = b["resolved"]
                    return self.send(200, {"ok": True})
                return self.send(404, {"detail": "not found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def seed(cid, **kw):
    c = {"id": cid, "n": kw.pop("n", 1), "screen": "path:/", "version": "v1", "path": "#home-title", "fx": 0.5, "fy": 0.5, "pageX": 100, "pageY": 100,
         "where": "Fixture shop", "text": "Old problem " + cid, "author": "Ana", "createdAt": "2026-10-01T10:00:00.000Z", "resolved": False,
         "replies": [], "appVersion": ""}
    c.update(kw)
    return c


# ---- Page helpers ----
def open_review(ctx, stub, hash_=""):
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(stub.base + "/__review/" + hash_)
    page.wait_for_selector("#cmt-root[data-mode]", state="attached")
    page.errors = errors
    wait_frame(page)
    return page


def wait_frame(page, path="/"):
    page.wait_for_function(
        "p => { const f = document.getElementById('cmt-frame'); try { return f.contentDocument.readyState === 'complete' && f.contentDocument.location.pathname === p; } catch (e) { return false; } }",
        arg=path, timeout=EXPECT_TIMEOUT)
    page.wait_for_timeout(700)   # one syncFrame poll (500 ms): the layer has bound the new document


def frame_of(page):
    return page.frame_locator("#cmt-frame")


def tab(page, title):
    page.get_by_role("tab", name=title).click()


def pref(page, key):
    return page.evaluate("k => { const p = JSON.parse(localStorage.getItem('cmt:fixture-review:prefs') || '{}'); return p[k]; }", key)


def frame_rect(page):
    return page.evaluate("() => { const r = document.getElementById('cmt-frame').getBoundingClientRect(); return {w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.left)}; }")


def hl_marked(page):
    return page.evaluate("() => [...document.getElementById('cmt-frame').contentDocument.querySelectorAll('[data-cmt-hl]')].map(e => e.id)")


def set_name(page, name="Ana"):
    page.evaluate("n => { const k = 'cmt:fixture-review:prefs'; const p = JSON.parse(localStorage.getItem(k) || '{}'); p.name = n; localStorage.setItem(k, JSON.stringify(p)); }", name)


def run_layer(browser):
    new_ctx = lambda **kw: browser.new_context(viewport={"width": 1440, "height": 1100}, **kw)  # noqa: E731

    # ---- Static checks ----
    @check("wrapper template: it loads the layer from /__review and holds no copy, and keeps cmtLogin")
    def _():
        html = FRAME_TEMPLATE.read_text(encoding="utf-8")
        assert 'href="layer.css"' in html and 'src="layer.js"' in html and "cmt:begin" not in html and "window.cmt = " not in html
        assert html.count('id="cmt-config"') == 1, "the wrapper needs exactly one cmt-config block"
        assert "function cmtLogin" in html and "window.cmtFrameReady" in html, "the wrapper keeps cmtLogin and cmtFrameReady"

    stub = Stub()
    stub.meta = {"version": "new2222", "expiresAt": None, "readOnly": True, "resetEnabled": False}

    # ---- L8: tab note and highlights ----
    @check("L8: the tab note follows the active tab and is hidden when the tab has no note")
    def _():
        ctx = new_ctx()
        page = open_review(ctx, stub)
        note = page.locator("#cmt-screen-note")
        assert note.get_attribute("role") == "note"
        expect(note).to_have_text("Start here. Press C to comment.", timeout=EXPECT_TIMEOUT)
        assert page.evaluate("() => { const n = document.getElementById('cmt-screen-note'); const f = document.getElementById('cmt-frame'); return n.nextElementSibling === f && n.previousElementSibling.localName === 'cmt-tabs'; }"), "note sits between the tab bar and the iframe"
        tab(page, "Orders")
        wait_frame(page, "/orders")
        expect(note).to_be_hidden(timeout=EXPECT_TIMEOUT)
        assert note.get_attribute("hidden") is not None
        tab(page, "Settings")
        wait_frame(page, "/settings")
        expect(note).to_have_text("Saving is blocked in this review.", timeout=EXPECT_TIMEOUT)
        ctx.close()

    @check("L8: the note is plain text, not HTML")
    def _():
        s2 = Stub()
        s2.screens[0]["note"] = "<b id=x>bold</b> <img src=x onerror=window.pwned=1>"
        ctx = new_ctx()
        page = open_review(ctx, s2)
        assert page.locator("#cmt-screen-note b").count() == 0
        assert "<b id=x>" in page.locator("#cmt-screen-note").inner_text()
        assert page.evaluate("() => window.pwned") is None
        ctx.close()
        s2.close()

    @check("L8: highlight toggle shows only on tabs with a selector, marks elements, and remembers its state")
    def _():
        ctx = new_ctx()
        page = open_review(ctx, stub)
        toggle = page.locator("#cmt-hl-toggle")
        assert toggle.get_attribute("hidden") is not None, "home has no highlight, so the toggle is hidden"
        assert hl_marked(page) == []
        tab(page, "Orders")
        wait_frame(page, "/orders")
        expect(toggle).to_be_visible(timeout=EXPECT_TIMEOUT)
        assert toggle.inner_text() == "Highlights" and toggle.get_attribute("aria-pressed") == "true"
        assert hl_marked(page) == ["order-1"], hl_marked(page)
        outline = page.evaluate("() => { const d = document.getElementById('cmt-frame').contentDocument; return getComputedStyle(d.getElementById('order-1')).outlineWidth; }")
        assert outline == "3px", outline
        assert page.evaluate("() => document.getElementById('cmt-frame').contentDocument.querySelectorAll('#cmt-hl-style').length") == 1
        # the marks survive a DOM change (the MutationObserver must not loop or drop them)
        page.evaluate("() => { const d = document.getElementById('cmt-frame').contentDocument; d.body.append(Object.assign(d.createElement('p'), { textContent: 'later' })); }")
        page.wait_for_timeout(500)
        assert hl_marked(page) == ["order-1"]
        toggle.click()
        assert toggle.get_attribute("aria-pressed") == "false" and hl_marked(page) == []
        assert pref(page, "highlight") is False
        page.reload()
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        wait_frame(page)
        tab(page, "Orders")
        wait_frame(page, "/orders")
        assert toggle.get_attribute("aria-pressed") == "false" and hl_marked(page) == [], "the choice persists"
        toggle.click()
        assert hl_marked(page) == ["order-1"]
        tab(page, "Settings")
        wait_frame(page, "/settings")
        assert hl_marked(page) == ["save-settings"]
        tab(page, "Home")
        wait_frame(page, "/")
        assert hl_marked(page) == []
        ctx.close()

    @check("L8: a bad selector marks nothing and raises no error; the selector never enters a stylesheet")
    def _():
        s2 = Stub()
        s2.screens = [{"id": "home", "title": "Home", "src": "/"}, {"id": "bad", "title": "Bad", "src": "/orders", "highlight": "###[["}]
        ctx = new_ctx()
        page = open_review(ctx, s2)
        tab(page, "Bad")
        wait_frame(page, "/orders")
        assert hl_marked(page) == []
        assert page.errors == [], page.errors
        css = page.evaluate("() => { const d = document.getElementById('cmt-frame').contentDocument; return [...d.querySelectorAll('style')].map(s => s.textContent).join(' '); }")
        assert "###" not in css
        ctx.close()
        s2.close()

    # ---- L13: viewport ----
    @check("L13: viewport selector sets iframe sizes, grey backdrop, html class, and the saved choice")
    def _():
        ctx = new_ctx()
        page = open_review(ctx, stub)
        group = page.locator("#cmt-viewport")
        assert group.get_attribute("role") == "group" and group.get_attribute("aria-label") == "Viewport"
        btns = page.locator("#cmt-viewport button.cmt-vp")
        assert [btns.nth(i).inner_text() for i in range(3)] == ["Desktop", "Tablet", "Mobile"]
        assert [btns.nth(i).get_attribute("data-vp") for i in range(3)] == ["desktop", "tablet", "mobile"]
        assert page.locator('[data-vp="desktop"]').get_attribute("aria-pressed") == "true"
        full = frame_rect(page)
        assert full["w"] == 1440 and page.evaluate("() => window.cmt.viewport()") == "desktop"
        page.click('[data-vp="mobile"]')
        r = frame_rect(page)
        assert (r["w"], r["h"]) == (390, 844), r
        assert r["x"] == (1440 - 390) // 2, r
        assert page.evaluate("() => document.documentElement.classList.contains('cmt-vp-mobile')")
        assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") != "rgb(255, 255, 255)", "grey backdrop"
        assert page.locator('[data-vp="mobile"]').get_attribute("aria-pressed") == "true"
        assert page.evaluate("() => window.cmt.viewport()") == "mobile" and pref(page, "viewport") == "mobile"
        page.click('[data-vp="tablet"]')
        r = frame_rect(page)
        assert (r["w"], r["h"]) == (834, 1112), r   # a 1100 px window: the preset height holds and the page scrolls
        page.set_viewport_size({"width": 1440, "height": 1400})
        page.wait_for_timeout(200)
        r = frame_rect(page)
        assert (r["w"], r["h"]) == (834, 1112), r
        page.set_viewport_size({"width": 600, "height": 1100})
        page.wait_for_timeout(200)
        assert frame_rect(page)["w"] == 600, "max-width:100% keeps the frame in a narrow window"
        page.set_viewport_size({"width": 1440, "height": 1100})
        page.click('[data-vp="desktop"]')
        r = frame_rect(page)
        assert r["w"] == 1440 and r["x"] == 0, r
        assert not page.evaluate("() => document.getElementById('cmt-frame').getAttribute('style')"), "desktop removes inline sizing"
        assert not page.evaluate("() => /cmt-vp-(tablet|mobile)/.test(document.documentElement.className)")
        page.click('[data-vp="tablet"]')
        page.reload()
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        assert frame_rect(page)["w"] == 834, "the choice persists"
        ctx.close()

    @check("L13: pins move when the viewport changes")
    def _():
        s2 = Stub()
        s2.comments = [seed("pin1aaaa", path="#home-box")]
        ctx = new_ctx()
        page = open_review(ctx, s2)
        pin = page.locator(".cmt-pin")
        expect(pin).to_have_count(1, timeout=EXPECT_TIMEOUT)
        x0 = pin.bounding_box()["x"]
        page.click('[data-vp="mobile"]')
        page.wait_for_timeout(500)
        expect(pin).to_have_count(1, timeout=EXPECT_TIMEOUT)
        x1 = pin.bounding_box()["x"]
        fr = frame_rect(page)
        assert abs(x1 - x0) > 50, (x0, x1)
        assert fr["x"] <= x1 <= fr["x"] + fr["w"], (x1, fr)
        ctx.close()
        s2.close()

    # ---- L11: meta, app version, stale comments ----
    @check("L11: window.cmt.meta() returns the answer, and null when meta fails; the layer keeps working")
    def _():
        ctx = new_ctx()
        page = open_review(ctx, stub)
        assert page.evaluate("() => window.cmt.meta()") == stub.meta
        ctx.close()
        s2 = Stub()
        s2.meta_status = 500
        s2.comments = [seed("pin2aaaa", appVersion="old1111")]
        ctx = new_ctx()
        page = open_review(ctx, s2)
        assert page.evaluate("() => window.cmt.meta()") is None
        expect(page.locator(".cmt-pin")).to_have_count(1, timeout=EXPECT_TIMEOUT)
        assert page.locator(".cmt-pin.cmt-stale").count() == 0, "no version, no stale"
        assert page.locator("#cmt-reset").get_attribute("hidden") is not None
        assert page.errors == []
        ctx.close()
        s2.close()

    @check("L11: a new comment carries appVersion from meta, and no appVersion key when meta has no version")
    def _():
        for version, expect_key in (("new2222", True), (None, False)):
            s2 = Stub()
            s2.meta["version"] = version
            ctx = new_ctx()
            page = open_review(ctx, s2)
            frame_of(page).locator("#home-title").click()
            pop = page.locator(".cmt-pop")
            expect(pop).to_be_visible(timeout=EXPECT_TIMEOUT)
            pop.get_by_placeholder("Your name").fill("Ana")
            pop.get_by_label("Comment").fill("New problem")
            pop.get_by_role("button", name="Save").click()
            for _ in range(40):
                if s2.posts:
                    break
                page.wait_for_timeout(100)
            assert s2.posts, "the comment reached the API"
            method, body = s2.posts[0]
            assert method == "POST" and body["type"] == "comment"
            assert ("appVersion" in body) == expect_key, body
            if expect_key:
                assert body["appVersion"] == "new2222"
            ctx.close()
            s2.close()

    @check("L11: a comment saved right after a deploy carries the new version, not the one from the last refresh")
    def _():
        s2 = Stub()
        s2.meta["version"] = "old1111"
        ctx = new_ctx()
        page = open_review(ctx, s2)
        assert page.evaluate("() => window.cmt.meta().version") == "old1111"
        frame_of(page).locator("#home-title").click()
        pop = page.locator(".cmt-pop")
        expect(pop).to_be_visible(timeout=EXPECT_TIMEOUT)
        s2.meta["version"] = "new3333"   # a deploy between the last refresh and the click on Save
        pop.get_by_placeholder("Your name").fill("Ana")
        pop.get_by_label("Comment").fill("Fresh problem")
        pop.get_by_role("button", name="Save").click()
        for _ in range(40):
            if s2.posts:
                break
            page.wait_for_timeout(100)
        assert s2.posts and s2.posts[0][1]["appVersion"] == "new3333", s2.posts
        assert len(s2.posts) == 1
        ctx.close()
        s2.close()

    @check("L11: a stale comment shows the dashed pin, the from tag, and both actions; Still an issue asks for a name first")
    def _():
        s2 = Stub()
        s2.comments = [seed("stale001", appVersion="old1111"), seed("same0001", n=2, path="#home-text", appVersion="new2222", text="Same version"),
                       seed("plain001", n=3, path="#me", text="No version")]
        ctx = new_ctx()
        page = open_review(ctx, s2)
        expect(page.locator(".cmt-pin")).to_have_count(3, timeout=EXPECT_TIMEOUT)
        assert page.locator(".cmt-pin.cmt-stale").count() == 1, "only the comment with another version is stale"
        page.locator(".cmt-pin.cmt-stale").click()
        pop = page.locator(".cmt-pop")
        expect(pop.locator("em.cmt-tag.cmt-from")).to_have_text("from old1111", timeout=EXPECT_TIMEOUT)
        assert pop.locator("button.cmt-still").inner_text() == "Still an issue"
        assert pop.locator("button.cmt-fixed").inner_text() == "Resolved in new2222"
        page.get_by_role("button", name=re.compile(r"^Comments \(")).click()
        assert page.locator(".cmt-item em.cmt-from").count() == 1
        assert page.locator(".cmt-item em.cmt-from").first.inner_text() == "from old1111"
        page.locator(".cmt-panel").get_by_role("button", name="Close").click()
        # no saved name: the action focuses the name field and sends nothing
        pop.locator("button.cmt-still").click()
        assert page.evaluate("() => document.activeElement && document.activeElement.getAttribute('aria-label')") == "Your name"
        page.wait_for_timeout(200)
        assert s2.posts == []
        pop.get_by_placeholder("Your name").fill("Ben")
        pop.locator("button.cmt-still").click()
        for _ in range(40):
            if s2.posts:
                break
            page.wait_for_timeout(100)
        method, body = s2.posts[0]
        assert method == "POST" and body["type"] == "reply" and body["commentId"] == "stale001"
        assert body["text"] == "Still an issue in new2222" and body["author"] == "Ben" and "appVersion" not in body, body
        expect(page.locator(".cmt-pop button.cmt-still")).to_have_count(0, timeout=EXPECT_TIMEOUT)
        assert page.locator(".cmt-pop button.cmt-fixed").count() == 0, "a confirmed comment shows no buttons"
        assert page.locator(".cmt-pop em.cmt-from").count() == 1, "but it keeps the from tag"
        ctx.close()
        s2.close()

    @check("L11: Resolved in <version> adds a reply, then sends PATCH resolved:true, and the pin stops being stale")
    def _():
        s2 = Stub()
        s2.comments = [seed("stale002", appVersion="old1111")]
        ctx = new_ctx()
        page = open_review(ctx, s2)
        set_name(page, "Ana")
        page.reload()
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        wait_frame(page)
        page.locator(".cmt-pin.cmt-stale").click()
        page.locator(".cmt-pop button.cmt-fixed").click()
        for _ in range(40):
            if len(s2.posts) >= 2:
                break
            page.wait_for_timeout(100)
        assert [m for m, _b in s2.posts] == ["POST", "PATCH"], s2.posts
        assert s2.posts[0][1]["text"] == "Resolved in new2222" and s2.posts[0][1]["type"] == "reply"
        patch = s2.posts[1][1]
        assert patch["id"] == "stale002" and patch["resolved"] is True and "appVersion" not in patch, patch
        assert page.locator(".cmt-pin.cmt-stale").count() == 0
        ctx.close()
        s2.close()

    @check("L11: Export JSON keeps appVersion")
    def _():
        s2 = Stub()
        s2.comments = [seed("exp00001", appVersion="old1111")]
        ctx = new_ctx()
        page = open_review(ctx, s2)
        expect(page.locator(".cmt-pin")).to_have_count(1, timeout=EXPECT_TIMEOUT)
        assert page.evaluate("() => window.cmt.exportJSON().comments[0].appVersion") == "old1111"
        ctx.close()
        s2.close()

    # ---- L3: read-only toast ----
    @check("L3: a read-only 403 shows the toast once, from fetch and XMLHttpRequest, and it hides after 4 seconds")
    def _():
        ctx = new_ctx()
        page = open_review(ctx, stub)
        tab(page, "Settings")
        wait_frame(page, "/settings")
        page.get_by_role("button", name="Browse").click()
        toast = page.locator("#cmt-toast")
        assert toast.get_attribute("role") == "status" and toast.get_attribute("aria-live") == "polite"
        assert "cmt-toast" in (toast.get_attribute("class") or "") and "cmt-ui" in (toast.get_attribute("class") or "")
        page.evaluate("() => { window.__shows = 0; new MutationObserver(ms => ms.forEach(m => { if (m.target.classList.contains('cmt-show') && !(m.oldValue || '').includes('cmt-show')) window.__shows++; })).observe(document.getElementById('cmt-toast'), { attributes: true, attributeFilter: ['class'], attributeOldValue: true }); }")
        frame_of(page).locator("#save-settings").click()
        expect(toast).to_have_class(re.compile(r"cmt-show"), timeout=EXPECT_TIMEOUT)
        assert toast.inner_text() == "Read-only review: this change was not sent to the app."
        # a second block inside 2 seconds, from XHR this time, shows no new toast
        page.evaluate("() => { const x = new (document.getElementById('cmt-frame').contentWindow.XMLHttpRequest)(); x.open('POST', '/api/settings'); x.send('{}'); }")
        page.wait_for_timeout(600)
        assert page.evaluate("() => window.__shows") == 1
        expect(toast).not_to_have_class(re.compile(r"cmt-show"), timeout=6000)
        # XHR alone triggers the toast (after the 2 second limit)
        page.wait_for_timeout(1500)
        page.evaluate("() => { const x = new (document.getElementById('cmt-frame').contentWindow.XMLHttpRequest)(); x.open('POST', '/api/settings'); x.send('{}'); }")
        expect(toast).to_have_class(re.compile(r"cmt-show"), timeout=EXPECT_TIMEOUT)
        ctx.close()

    @check("L3: a 403 without the X-Review-Read-Only header shows no toast")
    def _():
        ctx = new_ctx()
        page = open_review(ctx, stub)
        res = page.evaluate("() => document.getElementById('cmt-frame').contentWindow.fetch('/api/forbidden', { method: 'POST', body: '{}' }).then(r => r.status)")
        assert res == 403
        page.wait_for_timeout(500)
        assert "cmt-show" not in (page.locator("#cmt-toast").get_attribute("class") or "")
        ctx.close()

    # ---- L12: reset ----
    @check("L12: the reset button is hidden unless meta says resetEnabled is true")
    def _():
        ctx = new_ctx()
        page = open_review(ctx, stub)
        assert page.locator("#cmt-reset").get_attribute("hidden") is not None
        ctx.close()
        s2 = Stub()
        s2.meta["resetEnabled"] = True
        ctx = new_ctx()
        page = open_review(ctx, s2)
        btn = page.locator("#cmt-reset")
        expect(btn).to_be_visible(timeout=EXPECT_TIMEOUT)
        assert btn.inner_text() == "Reset demo data"
        ctx.close()
        s2.close()

    @check("L12: reset asks first, sends the CSRF header, re-runs the login, reloads the iframe, and shows the toast")
    def _():
        s2 = Stub()
        s2.meta["resetEnabled"] = True
        ctx = new_ctx()
        page = open_review(ctx, s2)
        btn = page.locator("#cmt-reset")
        expect(btn).to_be_visible(timeout=EXPECT_TIMEOUT)
        dialogs = []
        page.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        btn.click()
        page.wait_for_timeout(300)
        assert dialogs == ["Reset the demo data? Everything reviewers changed in the app goes back to its start state. Comments stay."], dialogs
        assert s2.resets == [], "Cancel sends nothing"
        page.evaluate("() => { document.getElementById('cmt-frame').contentWindow.__marker = 1; }")
        sessions = s2.sessions
        page.once("dialog", lambda d: d.accept())
        btn.click()
        expect(page.locator("#cmt-toast")).to_have_text("Demo data reset.", timeout=EXPECT_TIMEOUT)
        assert len(s2.resets) == 1 and s2.resets[0].get("x-review-reset") == "1", s2.resets
        assert s2.sessions > sessions, "cmtLogin ran again"
        wait_frame(page)
        assert page.evaluate("() => document.getElementById('cmt-frame').contentWindow.__marker") is None, "the iframe reloaded"
        assert btn.is_enabled() and btn.inner_text() == "Reset demo data"
        ctx.close()
        s2.close()

    @check("L12: reset shows the 429 and error messages, and the button works again")
    def _():
        s2 = Stub()
        s2.meta["resetEnabled"] = True
        ctx = new_ctx()
        page = open_review(ctx, s2)
        btn = page.locator("#cmt-reset")
        expect(btn).to_be_visible(timeout=EXPECT_TIMEOUT)
        page.on("dialog", lambda d: d.accept())
        s2.reset_answer = (429, {"error": "rate limited", "retryAfterSeconds": 17})
        btn.click()
        expect(page.locator("#cmt-toast")).to_have_text("Reset is limited to once every 30 seconds. Try again in 17 s.", timeout=EXPECT_TIMEOUT)
        assert btn.is_enabled()
        s2.reset_answer = (500, {"ok": False, "error": "reset failed (exit 3)", "durationMs": 9})
        btn.click()
        expect(page.locator("#cmt-toast")).to_have_text("Reset failed: reset failed (exit 3)", timeout=EXPECT_TIMEOUT)
        assert btn.is_enabled() and btn.inner_text() == "Reset demo data"
        ctx.close()
        s2.close()

    # ---- Empty space: where a hover and a click can land ----
    def hl_box(page):
        return page.locator(".cmt-hl").bounding_box()

    def iframe_body(page):
        return page.evaluate("() => { const r = document.getElementById('cmt-frame').contentDocument.body.getBoundingClientRect(); return {w: r.width, h: r.height}; }")

    @check("empty space: the outline never covers body, and it follows the pointer over one container")
    def _():
        ctx = new_ctx()
        page = open_review(ctx, stub)
        top = page.evaluate("() => document.getElementById('cmt-frame').getBoundingClientRect().top")
        body, seen = iframe_body(page), set()
        for x, y in ((1300, 110), (1300, 150), (1300, 480), (4, 300)):   # between two blocks, right of a narrow box, and the page margin
            page.mouse.move(x, top + y)
            page.wait_for_timeout(120)
            box = hl_box(page)
            assert box is not None, (x, y)
            assert box["height"] < body["h"] - 100 and box["width"] < body["w"], f"outline covers body at {x},{y}: {box}"
            seen.add((round(box["y"]), round(box["height"])))
        assert len(seen) >= 2, f"the outline did not follow the pointer: {seen}"
        ctx.close()

    @check("empty space: a click anchors to the nearest element, never to body, and the pin stays on it after a scroll")
    def _():
        s2 = Stub()
        ctx = new_ctx()
        page = open_review(ctx, s2)
        top = page.evaluate("() => document.getElementById('cmt-frame').getBoundingClientRect().top")
        page.mouse.click(1300, top + 480)
        pop = page.locator(".cmt-pop")
        expect(pop).to_be_visible(timeout=EXPECT_TIMEOUT)
        pop.get_by_placeholder("Your name").fill("Ana")
        pop.get_by_label("Comment").fill("Empty area")
        pop.get_by_role("button", name="Save").click()
        for _ in range(40):
            if s2.posts:
                break
            page.wait_for_timeout(100)
        body = s2.posts[0][1]
        assert body["path"] not in ("", "body", "html"), body
        assert 0 <= body["fx"] <= 1 and 0 <= body["fy"] <= 1
        assert not body["where"].startswith("Page"), body
        el = frame_of(page).locator(body["path"]).bounding_box()
        pin = page.locator(".cmt-pin").first.bounding_box()
        cx, cy = pin["x"] + pin["width"] / 2, pin["y"] + pin["height"] / 2
        assert el["x"] - 2 <= cx <= el["x"] + el["width"] + 2 and el["y"] - 2 <= cy <= el["y"] + el["height"] + 2, (el, cx, cy)
        page.evaluate("() => document.getElementById('cmt-frame').contentWindow.scrollTo(0, 30)")
        page.wait_for_timeout(300)
        pin2 = page.locator(".cmt-pin").first.bounding_box()
        assert abs((pin["y"] - pin2["y"]) - 30) <= 2, (pin, pin2)   # the pin moved with the element
        ctx.close()
        s2.close()

    @check("empty space: the gray area around a small viewport is not the app. No outline, no composer, no comment")
    def _():
        s2 = Stub()
        ctx = new_ctx()
        page = open_review(ctx, s2)
        page.get_by_role("button", name="Mobile").click()
        page.wait_for_timeout(500)
        page.mouse.move(100, 400)
        page.wait_for_timeout(150)
        assert hl_box(page) is None, "outline on the wrapper page"
        page.mouse.click(100, 400)
        page.wait_for_timeout(300)
        assert page.locator(".cmt-pop").count() == 0 and not s2.posts
        ctx.close()
        s2.close()

    @check("empty space: the outline hides when the pointer leaves the iframe, and when the iframe scrolls")
    def _():
        ctx = new_ctx()
        page = open_review(ctx, stub)
        top = page.evaluate("() => document.getElementById('cmt-frame').getBoundingClientRect().top")
        page.mouse.move(700, top + 140)
        page.wait_for_timeout(120)
        assert hl_box(page) is not None
        page.evaluate("() => document.getElementById('cmt-frame').contentDocument.body.dispatchEvent(new MouseEvent('mouseout', {bubbles: true, relatedTarget: null}))")
        expect(page.locator(".cmt-hl")).to_be_hidden(timeout=EXPECT_TIMEOUT)
        page.mouse.move(700, top + 141)
        page.wait_for_timeout(120)
        assert hl_box(page) is not None
        page.evaluate("() => document.getElementById('cmt-frame').contentWindow.scrollTo(0, 40)")
        expect(page.locator(".cmt-hl")).to_be_hidden(timeout=EXPECT_TIMEOUT)
        ctx.close()

    # ---- Panel list at 340 px and at a 375 px viewport ----
    LONG = "When the lead submits the request the tool does not show the new status until the page reloads and then the whole plan is shown again. " * 3
    WHERE = "Alex Kim from the north office of the sample company review desk › Request overview of the plan"

    def panel_stub():
        s2 = Stub()
        s2.comments = [seed("aaaa1", n=1, text=LONG, where=WHERE, replies=[{"id": "r1r1", "author": "Ben", "text": "Seen", "createdAt": "2026-10-02T10:00:00.000Z"}] * 2),
                       seed("bbbb2", n=2, text="Short one", where="Fixture shop", resolved=True),
                       seed("cccc3", n=1, screen="path:/orders", path="#orders-title", text=LONG, where="Orders"),
                       seed("dddd4", n=2, screen="path:/orders", path="#orders-text", text="Second on orders", where="Orders")]
        return s2

    for vw in (1440, 375):
        @check(f"panel at {vw} px: items grow with their text, never overlap, and a long where label stays on one line")
        def _(vw=vw):
            s2 = panel_stub()
            ctx = browser.new_context(viewport={"width": vw, "height": 900})
            page = open_review(ctx, s2)
            page.get_by_role("button", name=re.compile(r"^Comments \(\d+\)$")).click()
            page.get_by_role("button", name=re.compile(r"^All \(")).click()
            expect(page.locator(".cmt-item")).to_have_count(4, timeout=EXPECT_TIMEOUT)
            assert page.locator(".cmt-panel").bounding_box()["width"] == (340 if vw > 600 else vw)
            rows = page.evaluate("""() => [...document.querySelectorAll('.cmt-item')].map(e => {
              const j = e.querySelector('.cmt-ijump'), t = e.querySelector('.cmt-itext'), w = e.querySelector('.cmt-where'), r = e.getBoundingClientRect();
              return {top: r.top, bottom: r.bottom, cut: j.scrollHeight > j.clientHeight + 1, lines: Math.round(t.clientHeight / parseFloat(getComputedStyle(t).lineHeight)),
                      whereH: w.getBoundingClientRect().height, whereTitle: w.title, ellipsis: getComputedStyle(w).textOverflow, meta: e.querySelector('.cmt-imeta').innerText}; })""")
            assert not any(r["cut"] for r in rows), rows
            assert all(a["bottom"] <= b["top"] for a, b in zip(rows, rows[1:])), "items overlap"
            assert max(r["lines"] for r in rows) <= 3
            assert all(r["whereH"] < 22 and r["ellipsis"] == "ellipsis" for r in rows), rows
            assert rows[0]["whereTitle"] == WHERE
            assert "Ana" in rows[0]["meta"] and "2 replies" in rows[0]["meta"], rows[0]["meta"]
            assert "Resolved" in rows[1]["meta"]
            ctx.close()
            s2.close()

    @check("panel: long text is clamped with Show more. The toggle shows the whole text. Short text has no toggle")
    def _():
        s2 = panel_stub()
        ctx = new_ctx()
        page = open_review(ctx, s2)
        page.get_by_role("button", name=re.compile(r"^Comments \(\d+\)$")).click()
        page.get_by_role("button", name=re.compile(r"^All \(")).click()
        items = page.locator(".cmt-item")
        expect(items).to_have_count(4, timeout=EXPECT_TIMEOUT)
        expect(items.nth(0).get_by_role("button", name="Show more")).to_be_visible()
        expect(items.nth(1).get_by_role("button", name="Show more")).to_be_hidden()
        assert items.nth(0).locator(".cmt-itext").evaluate("e => e.scrollHeight > e.clientHeight + 1")
        items.nth(0).get_by_role("button", name="Show more").click()
        assert items.nth(0).locator(".cmt-itext").evaluate("e => e.scrollHeight <= e.clientHeight + 1")
        expect(items.nth(0).get_by_role("button", name="Show less")).to_be_visible()
        items.nth(0).locator(".cmt-ijump").click()   # the thread view shows all of it
        expect(page.locator(".cmt-pop .cmt-text").first).to_have_text(LONG, timeout=EXPECT_TIMEOUT)
        ctx.close()
        s2.close()

    @check("panel: items sit under one sticky header per screen, and numbers count inside the group")
    def _():
        s2 = panel_stub()
        ctx = new_ctx()
        page = open_review(ctx, s2)
        page.get_by_role("button", name=re.compile(r"^Comments \(\d+\)$")).click()
        page.get_by_role("button", name=re.compile(r"^All \(")).click()
        expect(page.locator(".cmt-gh")).to_have_text(["Home", "Orders"], timeout=EXPECT_TIMEOUT)
        assert page.locator(".cmt-gh").first.evaluate("e => getComputedStyle(e).position") == "sticky"
        groups = page.evaluate("() => [...document.querySelectorAll('.cmt-grp')].map(g => [...g.querySelectorAll('.cmt-item b')].map(b => b.textContent))")
        assert groups == [["#1", "#2"], ["#1", "#2"]], groups
        ctx.close()
        s2.close()

    @check("cross-origin iframe: a banner says pins cannot attach, and page comments still work")
    def _():
        s2 = Stub()
        s2.screens = [{"id": "home", "title": "Home", "src": s2.base.replace("127.0.0.1", "localhost") + "/"}]   # another host is another origin
        ctx = new_ctx()
        p = ctx.new_page()
        p.goto(s2.base + "/__review/")
        p.wait_for_selector("#cmt-root[data-mode]", state="attached")
        expect(p.locator(".cmt-banner")).to_contain_text("another origin", timeout=EXPECT_TIMEOUT)
        expect(p.locator(".cmt-banner")).to_contain_text("Page comments still work")
        p.get_by_role("button", name="Page comment").click()
        pop = p.locator(".cmt-pop")
        expect(pop).to_be_visible(timeout=EXPECT_TIMEOUT)
        pop.get_by_placeholder("Your name").fill("Ana")
        pop.get_by_label("Comment").fill("Whole page comment")
        pop.get_by_role("button", name="Save").click()
        expect(p.locator(".cmt-pin")).to_have_count(1)
        assert p.evaluate("cmt.exportJSON().comments[0].path") == "" and p.evaluate("cmt.screen()") == "home"
        p.reload()
        p.wait_for_selector("#cmt-root[data-mode]", state="attached")
        expect(p.locator(".cmt-pin")).to_have_count(1, timeout=EXPECT_TIMEOUT)
        expect(p.locator(".cmt-banner")).to_be_visible(timeout=EXPECT_TIMEOUT)
        ctx.close()
        s2.close()

    # ---- Prefill, start mode, and no session call ----
    SEARCH = [
        {"id": "plain", "title": "Plain", "src": "/search#/plain"},
        {"id": "sx", "title": "Search", "src": "/search#/a", "note": "Open the Churn tab.",
         "prefill": [{"selector": "#client-search", "value": "Quillfeather", "submit": "Enter", "delayMs": 2000, "waitForSelector": "#loaded"}]},
        {"id": "keys", "title": "Keys", "src": "/search#/k",
         "prefill": [{"selector": "#key-search", "value": "Marrowdale", "submit": "Enter", "delayMs": 2000, "waitForSelector": "#loaded", "keyEvents": True}]},
        {"id": "brag", "title": "Brag", "src": "/search#/brag",
         "prefill": [{"selector": "#client-search", "value": "Tessellate", "delayMs": 2000, "waitForSelector": "#loaded"},
                     {"click": "#brag-tab", "waitForSelector": "#brag-panel:not([hidden])"}]},
        {"id": "none", "title": "None", "src": "/search#/none",
         "prefill": [{"selector": "#client-search", "value": "Quill", "submit": "none"}]},
        {"id": "gone", "title": "Gone", "src": "/search#/gone", "prefill": [{"selector": "#nope", "value": "x"}]},
        {"id": "opt", "title": "Optional", "src": "/search#/opt",
         "prefill": [{"click": "#tab-that-appears-after-load", "optional": True},
                     {"selector": "#client-search", "value": "Quillfeather", "submit": "Enter", "delayMs": 2000, "waitForSelector": "#loaded"}]},
    ]

    def open_search(stub_, first="plain"):
        ctx = new_ctx()
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.goto(stub_.base + "/__review/#screen=" + first)
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        wait_frame(page, "/search")
        return ctx, page

    def flog(page):
        return page.query_selector("#cmt-frame").content_frame().evaluate("() => [window.__log, document.getElementById('client-search').value, document.getElementById('key-search').value]")

    @check("prefill: a React-like search page loads sample data. It runs once per arrival, never on a screen without prefill, and also after a hash-only tab switch")
    def _():
        s2 = Stub()
        s2.screens = copy.deepcopy(SEARCH)
        ctx, page = open_search(s2)
        page.wait_for_timeout(3000)
        log, v1, v2 = flog(page)
        assert log["enter"] == 0 and v1 == "" and v2 == "", "the plain screen has no prefill: nothing was typed"
        expect(page.locator("#cmt-screen-note")).to_be_hidden()
        tab(page, "Search")
        note = page.locator("#cmt-screen-note")
        expect(note).to_contain_text("Loading sample data…", timeout=3000)   # the tab switch changes only the hash: no load event, so no 8 s wait
        expect(note).to_contain_text("Open the Churn tab.")
        expect(frame_of(page).locator("#loaded")).to_have_text("Loaded Quillfeather", timeout=8000)
        expect(note).to_have_text("Open the Churn tab.", timeout=EXPECT_TIMEOUT)
        page.wait_for_timeout(2500)   # five more polls
        log, v1, _v2 = flog(page)
        assert log["enter"] == 1 and v1 == "Quillfeather", (log, v1)
        tab(page, "Plain")   # leave the screen and come back: it fills again, so the tab shows its own data
        page.wait_for_timeout(1200)
        tab(page, "Search")
        page.wait_for_function("() => document.getElementById('cmt-frame').contentWindow.__log.enter === 2", timeout=8000)
        assert page.errors == [], page.errors
        ctx.close()
        s2.close()

    @check("prefill: a page that reacts only to key events and a debounce also loads")
    def _():
        s2 = Stub()
        s2.screens = copy.deepcopy(SEARCH)
        ctx, page = open_search(s2, "keys")
        expect(frame_of(page).locator("#loaded")).to_have_text("Loaded Marrowdale", timeout=10000)
        ctx.close()
        s2.close()

    @check("prefill: steps run in order, and a click step works in Comment mode without a comment composer")
    def _():
        s2 = Stub()
        s2.screens = copy.deepcopy(SEARCH)
        ctx, page = open_search(s2, "brag")
        assert pref(page, "mode") == "comment"
        expect(frame_of(page).locator("#brag-panel")).to_be_visible(timeout=10000)
        assert frame_of(page).locator("#loaded").inner_text() == "Loaded Tessellate"
        log = flog(page)[0]
        assert log["mousedown"] == 1 and log["clicks"] == 1, log
        expect(page.locator("#cmt-screen-note")).to_be_hidden(timeout=EXPECT_TIMEOUT)
        assert page.locator(".cmt-pop").count() == 0, "the click belongs to the app"
        ctx.close()
        s2.close()

    @check("prefill: submit none types the value and sends no Enter")
    def _():
        s2 = Stub()
        s2.screens = copy.deepcopy(SEARCH)
        ctx, page = open_search(s2, "none")
        page.wait_for_function("() => document.getElementById('cmt-frame').contentDocument.getElementById('client-search').value === 'Quill'", timeout=8000)
        page.wait_for_timeout(2500)
        log, v1, _v2 = flog(page)
        assert log["enter"] == 0 and v1 == "Quill", (log, v1)
        ctx.close()
        s2.close()

    @check("prefill: an optional click on an absent element is skipped at once, and the next steps run")
    def _():
        s2 = Stub()
        s2.screens = copy.deepcopy(SEARCH)
        ctx, page = open_search(s2, "opt")
        expect(frame_of(page).locator("#loaded")).to_have_text("Loaded Quillfeather", timeout=8000)
        expect(page.locator("#cmt-screen-note")).to_be_hidden(timeout=EXPECT_TIMEOUT)
        ctx.close()
        s2.close()

    @check("prefill: a selector that never appears gives a short warning after 15 s")
    def _():
        s2 = Stub()
        s2.screens = copy.deepcopy(SEARCH)
        ctx, page = open_search(s2, "gone")
        note = page.locator("#cmt-screen-note")
        expect(note).to_have_text("Loading sample data…", timeout=3000)
        expect(note).to_contain_text("Sample data did not load (#nope not found)", timeout=20000)
        assert page.errors == [], page.errors
        ctx.close()
        s2.close()

    @check("start mode: browse starts in Browse mode with the new hint. A saved choice wins. The default stays Comment")
    def _():
        s2 = Stub()
        s2.start_mode = "browse"
        ctx = new_ctx()
        page = open_review(ctx, s2)
        assert pref(page, "mode") == "browse"
        expect(page.locator(".cmt-note")).to_contain_text("Press C or click Comment to leave feedback.", timeout=EXPECT_TIMEOUT)
        frame_of(page).locator("#home-text").click()
        assert page.locator(".cmt-pop").count() == 0, "Browse mode: a click comments nothing"
        page.get_by_role("button", name="Comment", exact=True).click()
        assert pref(page, "mode") == "comment"
        expect(page.locator(".cmt-note")).not_to_contain_text("Press C or click Comment")
        page.reload()
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        assert pref(page, "mode") == "comment", "the saved choice wins"
        ctx.close()
        ctx = new_ctx()
        page = open_review(ctx, stub)
        assert pref(page, "mode") == "comment", "no startMode: Comment, as before"
        ctx.close()
        s2.close()

    @check("R7: without a session block the wrapper never calls /__review/session. With one, or from an older proxy, it does")
    def _():
        s2 = Stub()
        s2.has_session = False
        ctx = new_ctx()
        page = open_review(ctx, s2)
        page.wait_for_timeout(500)
        assert s2.sessions == 0 and page.errors == [], (s2.sessions, page.errors)
        ctx.close()
        s2.close()
        s3 = Stub()
        ctx = new_ctx()
        open_review(ctx, s3)
        assert s3.sessions >= 1, "with a session block the wrapper still logs in"
        ctx.close()
        s3.close()
        s4 = Stub()
        s4.has_session = None
        ctx = new_ctx()
        open_review(ctx, s4)
        assert s4.sessions >= 1, "an older proxy sends no flag: the wrapper asks, as before"
        ctx.close()
        s4.close()

    # ---- Static mockup mode ----
    @check("static mockup: the new frame-mode UI does not exist and window.cmt stays the same")
    def _():
        ctx = new_ctx()
        page = ctx.new_page()
        built = Path(tempfile.mkdtemp(prefix="lar-static-")) / "index.html"
        subprocess.run([sys.executable, str(MOCKUP / "scripts" / "inject.py"), str(STATIC_TEMPLATE), str(built)], check=True, capture_output=True)
        page.goto(built.as_uri())
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        for sel in ("#cmt-reset", "#cmt-hl-toggle", "#cmt-viewport", "#cmt-toast", "#cmt-screen-note"):
            assert page.locator(sel).count() == 0, sel
        assert page.evaluate("() => [window.cmt.meta(), window.cmt.viewport()]") == [None, "desktop"]
        ctx.close()

    stub.close()


# ================================================================ 2. The real proxy with a stub app
LOGIN = {"login": {"route": "/api", "method": "POST", "path": "/api/v1/auth/token", "encoding": "form",
                   "fields": {"username": "$REVIEW_LOGIN_USER", "password": "$REVIEW_LOGIN_PASS"}},
         "tokenField": "access_token",
         "store": [{"type": "localStorage", "key": "app_token", "value": "{token}"}, {"type": "cookie", "key": "app_session", "value": "{token}"}]}


def live_proxy(tmp, name, project, screens, app, api_srv, session=LOGIN):
    """review-proxy.py as a child process with a review.json that routes to the stub app and the stub login API."""
    d = tmp / name
    d.mkdir()
    cfg = {"project": project, "title": "Frame test", "reviewNote": "Sample data only.", "readOnly": False, "frame": {"screens": screens},
           "routes": {"/": f"http://127.0.0.1:{app.server_address[1]}", "/api": f"http://127.0.0.1:{api_srv.server_address[1]}"}}
    if session:
        cfg["session"] = session
    (d / "review.json").write_text(json.dumps(cfg), encoding="utf-8")
    token, port = "rv-" + secrets.token_urlsafe(24), free_port()
    out, err = tmp / f"{name}.out", tmp / f"{name}.err"
    env = dict(os.environ, REVIEW_TOKEN=token, REVIEW_LOGIN_USER=LOGIN_USER, REVIEW_LOGIN_PASS=LOGIN_PASS)
    proc = subprocess.Popen([sys.executable, str(PROXY), "--config", str(d / "review.json"), "--port", str(port)], env=env, stdout=open(out, "w"), stderr=open(err, "w"),
                            start_new_session=True)
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError("review-proxy exited: " + err.read_text())
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.1)

    def stop():
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
                proc.wait(timeout=8)
                return
            except (ProcessLookupError, subprocess.TimeoutExpired):
                continue
    return SimpleNamespace(port=port, base=f"http://127.0.0.1:{port}", token=token, out=out, err=err, dir=d, stop=stop, link=lambda: out.read_text().strip())


def run_frame(browser, tmp):
    app, api_srv = serve(AppHandler), serve(ApiHandler)
    home, orders = {"id": "home", "title": "Home", "src": "/"}, {"id": "orders", "title": "Orders", "src": "/orders"}
    proxy = live_proxy(tmp, "main", "frame-fix", [home, orders], app, api_srv)
    numpx = live_proxy(tmp, "num", "num-fix", [home], app, api_srv)
    nosess = live_proxy(tmp, "nosess", "nosess-fix", [home], app, api_srv, session=None)
    ctx = browser.new_context(viewport={"width": 1000, "height": 900})
    state = {}
    fl = lambda page: page.frame_locator("#cmt-frame")
    in_frame = lambda page: page.query_selector("#cmt-frame").content_frame()

    def save_comment(page, text, name="Ana"):
        pop = page.locator(".cmt-pop")
        expect(pop).to_be_visible(timeout=EXPECT_TIMEOUT)
        nm = pop.locator('input[aria-label="Your name"]')
        if nm.count():
            nm.fill(name)
        pop.locator("textarea").fill(text)
        pop.get_by_role("button", name="Save").click()
        expect(pop.locator('textarea[aria-label="Reply"]')).to_be_visible(timeout=EXPECT_TIMEOUT)

    def at_screen(page, sid):
        page.wait_for_function("s => cmt.screen() === s", arg=sid, timeout=6000)

    @check("frame: the token link loads the app in the iframe, drops t from the URL and runs in shared mode")
    def _():
        page = ctx.new_page()
        page.goto(f"{proxy.base}/__review/?t={proxy.token}")
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        expect(fl(page).locator("#home-title")).to_be_visible(timeout=EXPECT_TIMEOUT)
        assert page.evaluate("location.search") == "" and page.evaluate("cmt.mode()") == "shared"
        at_screen(page, "path:/")
        expect(page.get_by_role("tab", name="Home")).to_have_attribute("aria-selected", "true")
        assert page.locator("iframe#cmt-frame").bounding_box()["width"] >= 998  # fills the window
        state["page"] = page

    page = state["page"]

    @check("R7: the real proxy without a session block. No /__review/session request, no console error")
    def _():
        p = ctx.new_page()
        asked, errs = [], []
        p.on("request", lambda r: asked.append(r.url) if "/__review/session" in r.url else None)
        p.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        p.goto(f"{nosess.base}/__review/?t={nosess.token}")
        p.wait_for_selector("#cmt-root[data-mode]", state="attached")
        expect(fl(p).locator("#home-title")).to_be_visible(timeout=EXPECT_TIMEOUT)
        p.wait_for_timeout(500)
        assert asked == [] and errs == [], (asked, errs)
        p.close()
        q = ctx.new_page()   # the proxy with a session block still gets its auto-login
        asked = []
        q.on("request", lambda r: asked.append(r.url) if "/__review/session" in r.url else None)
        q.goto(f"{proxy.base}/__review/?t={proxy.token}")
        q.wait_for_selector("#cmt-root[data-mode]", state="attached")
        expect(fl(q).locator("#home-title")).to_be_visible(timeout=EXPECT_TIMEOUT)
        assert asked, "the auto-login call is still made"
        q.close()

    @check("frame: hover outlines an element inside the iframe")
    def _():
        fl(page).locator("#home-text").hover()
        hl = page.locator(".cmt-hl")
        expect(hl).to_be_visible()
        box, el = hl.bounding_box(), fl(page).locator("#home-text").bounding_box()
        assert abs(box["x"] - el["x"]) < 2 and abs(box["y"] - el["y"]) < 2 and abs(box["width"] - el["width"]) < 2 and abs(box["height"] - el["height"]) < 2, (box, el)

    @check("frame: Comment mode blocks the app, and a click pins a comment on the iframe element")
    def _():
        fl(page).locator("#go-orders").click()
        page.locator(".cmt-pop").get_by_role("button", name="Cancel").click()
        assert in_frame(page).evaluate("window.clicks") is None and in_frame(page).evaluate("location.pathname") == "/"
        box = fl(page).locator("#home-box").bounding_box()
        fl(page).locator("#home-box").click(position={"x": box["width"] * 0.75, "y": 40})
        save_comment(page, "Box comment")
        expect(page.locator(".cmt-pin")).to_have_count(1)
        expect(page.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, open")
        x, y = pin_center(page)
        assert abs(x - (box["x"] + box["width"] * 0.75)) < 3 and abs(y - (box["y"] + 40)) < 3, (x, y, box)
        c = page.evaluate("cmt.exportJSON().comments[0]")
        assert c["screen"] == "path:/" and c["path"] == "#home-box" and c["where"].startswith("Home page"), c
        page.keyboard.press("Escape")

    @check("frame: the pin follows iframe scrolling and hides when its anchor is out of view")
    def _():
        _x, y0 = pin_center(page)
        in_frame(page).evaluate("scrollTo(0, 150)")
        page.wait_for_timeout(150)
        _x, y1 = pin_center(page)
        assert abs((y0 - y1) - 150) < 2, (y0, y1)
        in_frame(page).evaluate("scrollTo(0, 1100)")
        expect(page.locator(".cmt-pin")).to_be_hidden()
        in_frame(page).evaluate("scrollTo(0, 0)")
        expect(page.locator(".cmt-pin")).to_be_visible()

    @check("frame: pins follow DOM changes in the app (debounced) and hide when the anchor is removed")
    def _():
        _x, y0 = pin_center(page)
        in_frame(page).evaluate("document.getElementById('home-text').style.height = '160px'")  # pushes the box down
        want = fl(page).locator("#home-box").bounding_box()["y"] + 40
        assert want - y0 > 50, (want, y0)
        page.wait_for_function("w => { const r = document.querySelector('.cmt-pin').getBoundingClientRect(); return Math.abs(r.top + r.height / 2 - w) < 2 }", arg=want, timeout=3000)
        in_frame(page).evaluate("document.getElementById('home-text').style.height = ''")
        in_frame(page).evaluate("document.getElementById('home-box').id = 'home-box-gone'")
        expect(page.locator(".cmt-pin")).to_have_count(0, timeout=3000)
        in_frame(page).evaluate("document.getElementById('home-box-gone').id = 'home-box'")
        expect(page.locator(".cmt-pin")).to_have_count(1, timeout=3000)

    @check("frame: client-side navigation switches the screen id and its pins, and back restores them")
    def _():
        page.get_by_role("button", name="Browse").click()
        fl(page).locator("#go-orders").click()
        at_screen(page, "path:/orders")
        expect(page.locator(".cmt-pin")).to_have_count(0)
        expect(page.get_by_role("tab", name="Orders")).to_have_attribute("aria-selected", "true")
        assert in_frame(page).evaluate("window.clicks") == 1
        page.get_by_role("button", name="Comment", exact=True).click()
        fl(page).locator("#orders-text").click()
        save_comment(page, "Orders comment")
        expect(page.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, open")
        page.keyboard.press("Escape")
        in_frame(page).evaluate("history.back()")
        at_screen(page, "path:/")
        expect(page.locator(".cmt-pin")).to_have_count(1)
        expect(page.get_by_role("tab", name="Home")).to_have_attribute("aria-selected", "true")
        assert sorted(c["screen"] for c in page.evaluate("cmt.exportJSON().comments")) == ["path:/", "path:/orders"]

    @check("frame: a tab loads another page and a panel item jumps back to its page")
    def _():
        page.get_by_role("tab", name="Orders").click()
        at_screen(page, "path:/orders")
        expect(fl(page).locator("#orders-title")).to_be_visible()
        expect(page.locator(".cmt-pin")).to_have_count(1)
        open_panel(page)
        page.locator('[aria-label="Filter"] button').nth(2).click()
        page.locator(".cmt-item", has_text="Box comment").click()
        at_screen(page, "path:/")
        expect(page.locator(".cmt-pop")).to_contain_text("Box comment")
        expect(page.locator(".cmt-pin")).to_have_count(1)
        page.get_by_role("button", name="Close", exact=True).first.click()
        page.keyboard.press("Escape")

    @check("frame: a page reload restores the pins from the proxy store")
    def _():
        page.reload()
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        expect(fl(page).locator("#home-title")).to_be_visible(timeout=EXPECT_TIMEOUT)
        at_screen(page, "path:/")
        expect(page.locator(".cmt-pin")).to_have_count(1, timeout=EXPECT_TIMEOUT)
        expect(page.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, open")
        stored = json.loads(hreq(proxy.port, "GET", "/__review/api/comments?project=frame-fix", {"Cookie": cookie_for(proxy)})[2])["comments"]
        assert len(stored) == 2

    @check("frame: auto-login put the token in the shared origin storage before the app loaded")
    def _():
        expect(fl(page).locator("#token")).to_have_text(APP_TOKEN)
        assert in_frame(page).evaluate("localStorage.getItem('app_token')") == APP_TOKEN
        assert "app_session=" + APP_TOKEN in in_frame(page).evaluate("document.cookie")
        assert len(api_srv.calls) == 1 and api_srv.calls[0] == {"username": LOGIN_USER, "password": LOGIN_PASS}, api_srv.calls  # one login, cached
        status, _h, body = hreq(proxy.port, "GET", "/__review/session")
        assert status == 403 and APP_TOKEN.encode() not in body
        status, h, body = hreq(proxy.port, "GET", "/__review/session", {"Cookie": cookie_for(proxy)})
        assert status == 200 and h["cache-control"] == "no-store" and json.loads(body)["store"][0] == {"type": "localStorage", "key": "app_token", "value": APP_TOKEN}
        assert len(api_srv.calls) == 1

    @check("frame: a javascript: or off-origin screen id from a comment or the URL hash never loads in the iframe")
    def _():
        ck = cookie_for(proxy)
        bad = ["path:javascript:window.top.__pwn=1", "path://evil.example/x", "path:/\\evil.example/x", "path:data:text/html,<script>top.__pwn=1</script>",
               "path:blob:http://127.0.0.1/x"]
        for i, sid in enumerate(bad):
            body = {"project": "frame-fix", "type": "comment", "id": f"xss0000{i}", "text": f"entry {i}", "author": "Eve", "screen": sid, "version": "v1", "where": f"xss entry {i}"}
            assert comments_api(proxy, ck, "POST", "?project=frame-fix", body)[0] == 201
        p = ctx.new_page()
        try:
            p.goto(f"{proxy.base}/__review/")
            expect(fl(p).locator("#home-title")).to_be_visible(timeout=EXPECT_TIMEOUT)
            open_panel(p)
            for i in range(len(bad)):
                p.locator(".cmt-item", has_text=f"xss entry {i}").click()
                p.wait_for_timeout(300)
                assert p.evaluate("window.__pwn") is None, bad[i]
                src = p.evaluate("document.getElementById('cmt-frame').getAttribute('src')") or ""
                assert src.startswith(("/", f"{proxy.base}/")) and not src.startswith("//"), (bad[i], src)
                assert in_frame(p).evaluate("location.origin") == proxy.base
            p.evaluate("location.hash = '#screen=path:javascript:window.top.__h=1'")
            p.wait_for_timeout(400)
            assert p.evaluate("window.__h") is None
            assert in_frame(p).evaluate("location.origin") == proxy.base
        finally:
            p.close()
            for i in range(len(bad)):
                comments_api(proxy, ck, "DELETE", f"?project=frame-fix&id=xss0000{i}&author=Eve")

    @check("frame: a hash route is part of the screen id, so pins do not show on another hash view")
    def _():
        p = ctx.new_page()
        try:
            p.goto(f"{proxy.base}/__review/")
            expect(fl(p).locator("#home-title")).to_be_visible(timeout=EXPECT_TIMEOUT)
            in_frame(p).evaluate("location.href = '/hashapp#/a'")
            at_screen(p, "path:/hashapp#/a")
            fl(p).locator("#ha-text").click()
            save_comment(p, "Hash A comment")
            p.keyboard.press("Escape")
            expect(p.locator(".cmt-pin")).to_have_count(1)
            in_frame(p).evaluate("location.hash = '#/b'")
            at_screen(p, "path:/hashapp#/b")
            expect(p.locator(".cmt-pin")).to_have_count(0)
            in_frame(p).evaluate("history.back()")
            at_screen(p, "path:/hashapp#/a")
            expect(p.locator(".cmt-pin")).to_have_count(1)
            assert p.evaluate("cmt.exportJSON().comments.filter(c => c.text === 'Hash A comment')[0].screen") == "path:/hashapp#/a"
        finally:
            p.close()

    @check("frame: shared mode keeps a pin number after the newest comment is deleted, before and after a reload")
    def _():
        p = ctx.new_page()
        labels = lambda: sorted(p.locator(".cmt-pin").evaluate_all("els=>els.map(e=>e.getAttribute('aria-label').split(',')[0])"))
        try:
            p.goto(f"{numpx.base}/__review/?t={numpx.token}")
            expect(fl(p).locator("#home-title")).to_be_visible(timeout=EXPECT_TIMEOUT)
            for sel, text in (("#home-title", "One"), ("#home-text", "Two")):
                fl(p).locator(sel).click()
                save_comment(p, text)
                p.keyboard.press("Escape")
            assert labels() == ["Comment 1", "Comment 2"], labels()
            p.locator('.cmt-pin[aria-label^="Comment 2,"]').click()  # delete the newest
            p.locator(".cmt-pop").get_by_role("button", name="Delete").first.click()
            expect(p.locator(".cmt-pin")).to_have_count(1)
            expect(p.locator(".cmt-status")).to_have_text("Synced", timeout=EXPECT_TIMEOUT)
            fl(p).locator("#home-box").click()
            save_comment(p, "Three")
            p.keyboard.press("Escape")
            assert labels() == ["Comment 1", "Comment 3"], labels()  # the browser and the server agree on the number
            expect(p.locator(".cmt-status")).to_have_text("Synced", timeout=EXPECT_TIMEOUT)
            p.reload()
            p.wait_for_selector("#cmt-root[data-mode]", state="attached")
            expect(p.locator(".cmt-pin")).to_have_count(2, timeout=EXPECT_TIMEOUT)
            assert labels() == ["Comment 1", "Comment 3"], labels()
        finally:
            p.close()

    @check("frame: the wrapper renews the app login after the app token expires, when the window gets focus")
    def _():
        api_srv.expires = 1
        p2 = live_proxy(tmp, "renew", "frame-fix", [home], app, api_srv)
        c = browser.new_context()
        try:
            page = c.new_page()
            page.goto(f"{p2.base}/__review/?t={p2.token}")
            page.wait_for_function("localStorage.getItem('app_token')", timeout=6000)
            assert page.evaluate("localStorage.getItem('app_token')") == APP_TOKEN
            api_srv.suffix = "-renewed"
            time.sleep(1.3)  # the proxy cache lives min(10 minutes, expires_in) = 1 second
            page.evaluate("dispatchEvent(new Event('focus'))")
            page.wait_for_function("t => localStorage.getItem('app_token') === t", arg=APP_TOKEN + "-renewed", timeout=6000)
        finally:
            api_srv.expires, api_srv.suffix = 3600, ""
            c.close()
            p2.stop()

    @check("proxy: credentials, tokens and cookies never appear in the proxy output or in any file of the run")
    def _():
        out, err = proxy.out.read_text(), proxy.err.read_text()
        assert out.strip() == proxy.link() and len(out.splitlines()) == 1 and proxy.token in out
        secrets_ = [LOGIN_PASS, LOGIN_USER, APP_TOKEN, cookie_for(proxy).split("=", 1)[1]]
        assert not any(s in err for s in secrets_ + [proxy.token]), err
        assert not any(s in out for s in secrets_), out
        for f in tmp.rglob("*"):  # every file the run wrote: logs, stores, configs. Only a .out file holds a share link
            if f.is_file():
                data = f.read_bytes()
                for s in secrets_ + ([] if f.suffix == ".out" else [proxy.token]):
                    assert s.encode() not in data, (f.name, "holds a secret")
    ctx.close()
    for p in (proxy, numpx, nosess):
        p.stop()
    app.shutdown()
    api_srv.shutdown()


# ================================================================ 3. The whole stack: review.py, the real proxy, the fixture apps
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


def e2e_http(method, url, headers=None, data=None):
    """One request without redirects. Returns (status, headers, body text)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as r:
            return r.status, r.headers, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")


class Review:
    """A temp review folder with the fixtures, random fake login and ports, and the fake tailscale first on PATH."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lar-e2e-"))
        shutil.copytree(FIX, self.dir, ignore=shutil.ignore_patterns("bin", "__pycache__"), dirs_exist_ok=True)
        self.api, self.web, self.proxy = free_port(), free_port(), free_port()
        user, pw = "u" + secrets.token_hex(4), "p" + secrets.token_hex(8)
        (self.dir / ".fixture.env").write_text(f"FIXTURE_USER={user}\nFIXTURE_PASS={pw}\n")
        os.chmod(self.dir / ".fixture.env", 0o600)
        self.edit(lambda c: (c["apps"][0].update(port=self.api), c["apps"][1].update(port=self.web), c["proxy"].update(port=self.proxy),
                             c.pop("ttl", None), c.update(ttl="7d")))
        self.ts = self.dir / "ts"
        self.ts.mkdir()
        me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        self.env = dict(os.environ, PATH=f"{FIX / 'bin'}{os.pathsep}{os.environ['PATH']}", FAKE_TS_DIR=str(self.ts), FAKE_TS_OPERATOR=me)
        for k in ("REVIEW_TOKEN", "REVIEW_PROXY_SCRIPT"):  # this test uses the real proxy
            self.env.pop(k, None)
        self.pgids = set()

    def edit(self, fn):
        f = self.dir / "review.json"
        c = json.loads(f.read_text())
        fn(c)
        f.write_text(json.dumps(c, indent=1))

    def run(self, *args, timeout=90):
        r = subprocess.run([sys.executable, str(REVIEW), "-C", str(self.dir), *args], capture_output=True, text=True, timeout=timeout, env=self.env)
        st = self.state()
        for c in (st or {}).get("components", []):
            if c.get("pgid"):
                self.pgids.add(c["pgid"])
        return r

    def state(self):
        try:
            return json.loads((self.dir / ".review" / "state.json").read_text())
        except (OSError, ValueError):
            return None

    def link(self):
        r = self.run("link")
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def local_url(self, link, path="/__review/"):
        """The link of `up` uses the public host when Funnel is on. The test opens the same token on 127.0.0.1."""
        return f"http://127.0.0.1:{self.proxy}{path}?t=" + link.split("t=", 1)[1]

    def calls(self):
        f = self.ts / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

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


# ---- page helpers ----
def e2e_wait_frame(page, path="/"):
    page.wait_for_function(
        "p => { const f = document.getElementById('cmt-frame'); try { return f.contentDocument.readyState === 'complete' && f.contentDocument.location.pathname === p; } catch (e) { return false; } }",
        arg=path, timeout=EXPECT_TIMEOUT)
    page.wait_for_timeout(700)   # one layer poll (500 ms): the layer has bound the new document


def frame(page):
    return page.frame_locator("#cmt-frame")


def e2e_tab(page, title, path):
    page.get_by_role("tab", name=title).click()
    e2e_wait_frame(page, path)


def e2e_hl_marked(page):
    return page.evaluate("() => [...document.getElementById('cmt-frame').contentDocument.querySelectorAll('[data-cmt-hl]')].map(e => e.id)")


def pin_vs_box(page):
    """Pin centre and the centre of #home-box, both in wrapper coordinates."""
    return page.evaluate("""() => {
        const f = document.getElementById('cmt-frame'), fr = f.getBoundingClientRect();
        const r = f.contentDocument.getElementById('home-box').getBoundingClientRect();
        const p = document.querySelector('.cmt-pin').getBoundingClientRect();
        return { bx: fr.left + r.left, by: fr.top + r.top, bw: r.width, bh: r.height, px: p.left + p.width / 2, py: p.top + p.height / 2,
                 fw: Math.round(fr.width), fh: Math.round(fr.height) };
    }""")


def inside(b, slack=24):
    return b["bx"] - slack <= b["px"] <= b["bx"] + b["bw"] + slack and b["by"] - slack <= b["py"] <= b["by"] + b["bh"] + slack


def start_page(ctx, url):
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector("#cmt-root[data-mode]", state="attached", timeout=EXPECT_TIMEOUT)
    e2e_wait_frame(page)
    return page


def phase1(browser):
    rv = Review()
    S = {}
    try:
        @check("e2e 1: review.py up starts the apps, the real proxy and the watchdog, and prints one local link")
        def _():
            r = rv.run("up")
            assert r.returncode == 0, r.stderr + r.stdout
            lines = r.stdout.splitlines()
            link = lines[lines.index("Share link:") + 1]
            assert link.startswith(f"http://127.0.0.1:{rv.proxy}/__review/?t="), link
            S["link"] = link
            st = rv.state()
            assert [c["name"] for c in st["components"]] == ["api", "web", "proxy", "watchdog"], st
            S["comps"] = st["components"]
            assert rv.run("status").returncode == 0

        if not S.get("link"):
            return rv, S
        ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
        S["ctx"] = ctx
        base = f"http://127.0.0.1:{rv.proxy}"

        @check("e2e 2: the token link opens the wrapper, drops the token from the URL, and auto-login signs the iframe in")
        def _():
            S["page"] = page = start_page(ctx, S["link"])
            assert "t=" not in page.url, page.url
            expect(frame(page).locator("#me")).to_have_text("reviewer", timeout=EXPECT_TIMEOUT)
            expect(frame(page).locator("#token")).to_have_text("present", timeout=EXPECT_TIMEOUT)
            assert page.evaluate("() => window.cmt.mode()") == "shared"
            st, _h, _b = e2e_http("GET", base + "/__review/")
            assert st == 403, "no cookie, no access"

        page = S.get("page")
        if not page:
            return rv, S

        @check("e2e 3: meta shows the app version and the reset button is visible")
        def _():
            meta = page.evaluate("() => window.cmt.meta()")
            assert meta["version"] == "abc1234" and meta["resetEnabled"] is True and meta["readOnly"] is True and meta["expiresAt"], meta
            expect(page.locator("#cmt-reset")).to_be_visible(timeout=EXPECT_TIMEOUT)

        @check("e2e 4: tab notes and highlights follow the tabs")
        def _():
            expect(page.locator("#cmt-screen-note")).to_have_text("Start here. Press C to comment.", timeout=EXPECT_TIMEOUT)
            assert page.locator("#cmt-hl-toggle").get_attribute("hidden") is not None
            e2e_tab(page, "Orders", "/orders")
            expect(page.locator("#cmt-screen-note")).to_be_hidden(timeout=EXPECT_TIMEOUT)
            expect(page.locator("#cmt-hl-toggle")).to_be_visible(timeout=EXPECT_TIMEOUT)
            assert e2e_hl_marked(page) == ["order-1"], e2e_hl_marked(page)
            e2e_tab(page, "Settings", "/settings")
            expect(page.locator("#cmt-screen-note")).to_have_text("Saving is blocked in this review.", timeout=EXPECT_TIMEOUT)
            assert e2e_hl_marked(page) == ["save-settings"], e2e_hl_marked(page)

        @check("e2e 5: mock rules answer before the app: table hit, table miss, literal path, mocksFile; other paths reach the API")
        def _():
            e2e_tab(page, "Orders", "/orders")
            f = frame(page)
            expect(f.locator("#order-1")).to_contain_text('"source":"mock"', timeout=EXPECT_TIMEOUT)
            expect(f.locator("#order-1")).to_contain_text("Mock widget")
            expect(f.locator("#order-9")).to_contain_text("404")
            expect(f.locator("#order-9")).to_contain_text("mock: no such order")
            expect(f.locator("#order-special")).to_contain_text("a literal segment beats {id}")
            got = page.evaluate("() => fetch('/api/orders').then(r => r.json())")
            assert got["orders"][0]["source"] == "api", got

        @check("e2e 6: a comment gets the app version, and its pin stays on its element when the viewport changes")
        def _():
            e2e_tab(page, "Home", "/")
            frame(page).locator("#home-box").click()
            pop = page.locator(".cmt-pop")
            expect(pop).to_be_visible(timeout=EXPECT_TIMEOUT)
            pop.get_by_placeholder("Your name").fill("Ana")
            pop.get_by_label("Comment").fill("The box is too grey")
            pop.get_by_role("button", name="Save").click()
            expect(page.locator(".cmt-pin")).to_have_count(1, timeout=EXPECT_TIMEOUT)
            fetch_all = "() => fetch('/__review/api/comments?project=fixture-review').then(r => r.json())"
            assert wait_for(lambda: page.evaluate(fetch_all)["comments"], 8), "the comment never reached the proxy"
            comments = page.evaluate(fetch_all)["comments"]
            assert len(comments) == 1 and comments[0]["appVersion"] == "abc1234", comments
            assert page.locator(".cmt-pin.cmt-stale").count() == 0, "same version is not stale"
            desk = pin_vs_box(page)
            assert desk["fw"] == 1440 and inside(desk), desk
            page.click('[data-vp="mobile"]')
            page.wait_for_timeout(900)
            mob = pin_vs_box(page)
            assert mob["fw"] == 390 and mob["fh"] == 844 and inside(mob), mob
            assert abs(mob["px"] - desk["px"]) > 50, "the pin moved with the element"
            page.click('[data-vp="tablet"]')
            page.wait_for_timeout(900)
            tab_ = pin_vs_box(page)
            assert tab_["fw"] == 834 and inside(tab_), tab_
            page.click('[data-vp="desktop"]')
            page.wait_for_timeout(900)
            assert inside(pin_vs_box(page))

        @check("e2e 6b: Tablet and Mobile keep 834x1112 and 390x844 in a window that is shorter than the preset")
        def _():
            page.set_viewport_size({"width": 1440, "height": 700})
            size = lambda: page.evaluate("() => { const r = document.getElementById('cmt-frame').getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }")  # noqa: E731
            try:
                page.click('[data-vp="tablet"]')
                page.wait_for_timeout(700)
                assert size() == [834, 1112], size()
                page.click('[data-vp="mobile"]')
                page.wait_for_timeout(700)
                assert size() == [390, 844], size()
                page.click('[data-vp="desktop"]')
                page.wait_for_timeout(700)
                assert size()[0] == 1440 and size()[1] <= 700, size()  # desktop fills the window again, no page scroll
                assert page.evaluate("() => document.documentElement.scrollHeight <= innerHeight")
            finally:
                page.set_viewport_size({"width": 1440, "height": 1100})

        @check("e2e 7: read-only blocks a POST with the toast, allowWrite lets one through, and nothing changed in the app")
        def _():
            page.get_by_role("button", name="Browse").click()
            e2e_tab(page, "Settings", "/settings")
            before = json.loads((rv.dir / ".fixture-state" / "state.json").read_text())["settings"]
            frame(page).locator("#save-settings").click()
            expect(page.locator("#cmt-toast")).to_have_class(re.compile(r"cmt-show"), timeout=EXPECT_TIMEOUT)
            expect(page.locator("#cmt-toast")).to_have_text("Read-only review: this change was not sent to the app.")
            expect(frame(page).locator("#result")).to_contain_text("read-only review", timeout=EXPECT_TIMEOUT)
            expect(frame(page).locator("#result")).to_contain_text("403")
            assert json.loads((rv.dir / ".fixture-state" / "state.json").read_text())["settings"] == before
            e2e_tab(page, "Home", "/")
            frame(page).locator("#create-order").click()
            expect(frame(page).locator("#result")).to_contain_text("201", timeout=EXPECT_TIMEOUT)

        @check("e2e 8: the reset button runs the fixture reset command, and every reviewer's changes go back to the seed")
        def _():
            orders = lambda: page.evaluate("() => fetch('/api/orders').then(r => r.json())")["orders"]  # noqa: E731
            assert len(orders()) == 3, "the allowed create-order added one"
            page.once("dialog", lambda d: d.accept())
            page.locator("#cmt-reset").click()
            expect(page.locator("#cmt-toast")).to_have_text("Demo data reset.", timeout=EXPECT_TIMEOUT)
            assert len(orders()) == 2
            e2e_wait_frame(page)
            expect(frame(page).locator("#me")).to_have_text("reviewer", timeout=EXPECT_TIMEOUT)  # the login ran again
            log = rv.dir / ".review" / "reset.log"
            assert log.exists() and oct(log.stat().st_mode & 0o777) == "0o600"
            page.once("dialog", lambda d: d.accept())
            page.locator("#cmt-reset").click()
            expect(page.locator("#cmt-toast")).to_contain_text("Reset is limited to once every 30 seconds", timeout=EXPECT_TIMEOUT)

        @check("e2e 9: a dead app gives the paused page (HTML and JSON) and status fails")
        def _():
            for c in rv.state()["components"]:
                if c["name"] in ("web", "api"):
                    os.killpg(c["pgid"], signal.SIGKILL)
            assert wait_for(lambda: not busy(rv.web) and not busy(rv.api), 5)
            # the browser sends the cookie. Playwright's own request client does not send a Secure cookie to http://127.0.0.1
            get = ("p => fetch(p, {headers: {Accept: p === '/' ? 'text/html' : 'application/json'}})"
                   ".then(async r => ({status: r.status, retry: r.headers.get('retry-after'), text: await r.text()}))")
            res = page.evaluate(get, "/")
            assert res["status"] == 503 and res["retry"] == "30" and "Review paused, try again later" in res["text"], res
            res = page.evaluate(get, "/api/orders")
            assert res["status"] == 503 and json.loads(res["text"]) == {"detail": "review paused", "retryAfterSeconds": 30}, res
            assert "token" not in res["text"].lower()
            s = rv.run("status")
            assert s.returncode == 1, s.stdout + s.stderr

        @check("e2e 10: down ends every process and port, removes the token, and keeps the comments")
        def _():
            ctx.close()
            d = rv.run("down")
            assert d.returncode == 0, d.stderr
            assert wait_for(lambda: not any(group_alive(c["pgid"]) for c in S["comps"]), 8)
            assert not any(busy(p) for p in (rv.api, rv.web, rv.proxy))
            assert rv.state() is None and not (rv.dir / ".review" / "token").exists() and not list((rv.dir / ".review").glob("*.pid"))
            assert (rv.dir / ".review" / "comments.json").exists()
            assert rv.run("down").returncode == 0
    except BaseException:
        rv.cleanup()
        raise
    return rv, S


def phase2(browser, rv):
    """Same folder: the comment from phase 1 is still there, the app version changed, Funnel is on, and the ttl is short."""
    rv.edit(lambda c: c.update(appVersion="def5678", ttl="25s"))
    S = {}

    @check("e2e 11: up --funnel turns Funnel on through the fake tailscale and prints the public link")
    def _():
        r = rv.run("up", "--funnel")
        assert r.returncode == 0, r.stderr + r.stdout
        link = r.stdout.splitlines()[r.stdout.splitlines().index("Share link:") + 1]
        assert link.startswith("https://demo-node.example.test/__review/?t="), link
        S["link"], S["comps"] = link, rv.state()["components"]
        assert any(c.startswith("funnel --bg --https=443 http://127.0.0.1:") for c in rv.calls()), rv.calls()
        assert [c["name"] for c in S["comps"]] == ["api", "web", "proxy", "watchdog"]

    if not S.get("link"):
        return
    base = f"http://127.0.0.1:{rv.proxy}"
    wd = [c for c in S["comps"] if c["name"] == "watchdog"][0]
    os.kill(wd["pid"], signal.SIGSTOP)   # hold the watchdog, so the proxy shows its 410 page before down runs (contract R2)

    @check("e2e 12: a comment from the older app version shows as stale with both actions")
    def _():
        ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
        try:
            page = start_page(ctx, rv.local_url(S["link"]))
            expect(page.locator(".cmt-pin")).to_have_count(1, timeout=EXPECT_TIMEOUT)
            expect(page.locator(".cmt-pin.cmt-stale")).to_have_count(1, timeout=EXPECT_TIMEOUT)
            page.locator(".cmt-pin.cmt-stale").click()
            pop = page.locator(".cmt-pop")
            expect(pop.locator("em.cmt-from")).to_have_text("from abc1234", timeout=EXPECT_TIMEOUT)
            assert pop.locator("button.cmt-still").inner_text() == "Still an issue"
            assert pop.locator("button.cmt-fixed").inner_text() == "Resolved in def5678"
            assert page.evaluate("() => window.cmt.meta().version") == "def5678"
        finally:
            ctx.close()

    @check("e2e 13: after the expiry time every request answers 410 before the token gate, and no cookie is set")
    def _():
        exp = rv.state()["expiresAt"]
        end = calendar.timegm(time.strptime(exp, "%Y-%m-%dT%H:%M:%SZ"))
        assert wait_for(lambda: time.time() > end + 0.5, 60, 0.5)
        token_url = rv.local_url(S["link"])
        for method, url in (("GET", base + "/__review/"), ("GET", token_url), ("POST", base + "/api/orders"), ("DELETE", base + "/api/orders/1"),
                            ("GET", base + "/__review/meta"), ("HEAD", base + "/")):
            st, h, body = e2e_http(method, url, headers={"Accept": "text/html"}, data=b"{}" if method == "POST" else None)
            assert st == 410, (method, url.split("?")[0], st)
            assert "set-cookie" not in {k.lower() for k in h.keys()}
            if method != "HEAD":
                assert "This review has ended" in body and "Review ended" in body
        assert rv.state() is not None, "the held watchdog has not run yet"

    @check("e2e 14: the watchdog runs down: processes and ports end, Funnel is turned off, the log says why")
    def _():
        os.kill(wd["pid"], signal.SIGCONT)
        assert wait_for(lambda: rv.state() is None, 25), "the watchdog did not run down"
        assert wait_for(lambda: not any(group_alive(c["pgid"]) for c in S["comps"]), 12), "a process group is still alive"
        assert not any(busy(p) for p in (rv.api, rv.web, rv.proxy))
        assert any(c.startswith("funnel --https=443 off") for c in rv.calls()), rv.calls()
        assert "expired: running down" in (rv.dir / ".review" / "watchdog.log").read_text()
        assert not (rv.dir / ".review" / "token").exists()
        assert rv.run("down").returncode == 0, "down after the watchdog is still safe"


def main():
    tmp = Path(tempfile.mkdtemp(prefix="live-app-review-browser-"))
    rv = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception:  # noqa: BLE001 - bundled browser missing or wrong version
                browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium")
            try:
                run_layer(browser)
                run_frame(browser, tmp)
                rv, _S = phase1(browser)
                if rv and rv.state() is None and all(e is None for _n, e in results):
                    phase2(browser, rv)
            finally:
                browser.close()
                if rv:
                    rv.cleanup()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, e in results if e]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
