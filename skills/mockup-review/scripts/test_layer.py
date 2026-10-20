#!/usr/bin/env python3
"""Headless test of mockup-review: layer drift check, local mode, inject.py, the deploy.sh guard, then shared mode on a local `wrangler dev`.

Run: uv run --with playwright python scripts/test_layer.py
Needs: node (npm exec wrangler), curl, and Chromium (bundled, else /usr/bin/chromium).
Everything runs in a temp dir. It never logs in to Cloudflare and never deploys.
"""
import functools
import http.client
import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import inject  # noqa: E402  the build step under test
TEMPLATE = (SKILL / "assets" / "template" / "index.html").read_text(encoding="utf-8")
EXPECT_TIMEOUT = 8000
WRANGLER = re.search(r"WRANGLER_VERSION=(\S+)", (SKILL / "scripts" / "deploy.sh").read_text(encoding="utf-8")).group(1)   # one pinned version, set in deploy.sh
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
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:400]}", flush=True)
        return fn
    return wrap


# ---- Fixture: the real layer from the template, two small screens ----
def fixture(version="v1", renamed=False):
    title_id = "a-title2" if renamed else "a-title"
    screens = f"""<main>
<section data-screen="a" data-title="Alpha"><h1 id="{title_id}">Alpha screen</h1><p id="a-text">Alpha paragraph</p>
<a id="a-link" href="#blocked">A link</a> <button id="a-btn" onclick="window.clicks=(window.clicks||0)+1">Count</button>
<div id="a-box" style="height:200px;width:50%;background:#eee">box</div></section>
<section data-screen="b" data-title="Beta"><h1 id="b-title">Beta screen</h1><p id="b-text">Beta paragraph</p></section>
</main>"""
    html = re.sub(r"<!-- proto:begin.*?<!-- proto:end -->", lambda _m: screens, TEMPLATE, flags=re.S)
    cfg = json.dumps({"project": "fix", "title": "Test prototype", "version": version, "reviewNote": "Sample data only."})
    return inject.build(re.sub(r'(<script type="application/json" id="cmt-config">).*?(</script>)', lambda m: m.group(1) + cfg + m.group(2), html, flags=re.S))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- Page helpers ----
def open_page(ctx, url):
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_selector("#cmt-root[data-mode]", state="attached")
    return page


def add_comment(page, sel, text, name="Ana", **click):
    page.click(sel, **click)
    pop = page.locator(".cmt-pop")
    expect(pop).to_be_visible(timeout=EXPECT_TIMEOUT)
    nm = pop.locator('input[aria-label="Your name"]')
    if nm.count():
        nm.fill(name)
    pop.locator("textarea").fill(text)
    pop.get_by_role("button", name="Save").click()
    expect(pop.locator('textarea[aria-label="Reply"]')).to_be_visible(timeout=EXPECT_TIMEOUT)


def open_panel(page):
    if not page.locator(".cmt-panel.cmt-open").count():
        page.get_by_role("button", name=re.compile(r"^Comments \(\d+\)$")).click()


def rect(page, sel):  # document coordinates of an element
    return page.evaluate("s=>{const r=document.querySelector(s).getBoundingClientRect();return {l:r.left+scrollX,t:r.top+scrollY,w:r.width,h:r.height}}", sel)


def pin_center(page, i=0):
    return page.evaluate("i=>{const r=document.querySelectorAll('.cmt-pin')[i].getBoundingClientRect();return [r.left+r.width/2+scrollX,r.top+r.height/2+scrollY]}", i)


def md(page):
    return page.evaluate("cmt.exportMarkdown()")


# ---- Local mode ----
def run_local(browser, base, served):
    ctx = browser.new_context(viewport={"width": 1000, "height": 800})
    state = {}

    @check("local: mode is local, hint shown, Comment mode on, tabs rendered")
    def _():
        page = open_page(ctx, base + "/")
        assert page.evaluate("cmt.mode()") == "local"
        expect(page.locator(".cmt-note")).to_contain_text("Comment mode is on", timeout=EXPECT_TIMEOUT)
        assert page.evaluate("document.documentElement.classList.contains('cmt-on')")
        expect(page.locator("cmt-tabs .cmt-tab")).to_have_count(2)
        expect(page.get_by_role("tab", name="Alpha")).to_have_attribute("aria-selected", "true")
        state["page"] = page

    page = state["page"]

    @check("local: hover outlines the anchor element")
    def _():
        page.hover("#a-text")
        hl = page.locator(".cmt-hl")
        expect(hl).to_be_visible()
        box, el = hl.bounding_box(), page.locator("#a-text").bounding_box()
        assert abs(box["x"] - el["x"]) < 2 and abs(box["width"] - el["width"]) < 2

    @check("local: Comment mode blocks page links and buttons")
    def _():
        page.click("#a-link")
        page.locator(".cmt-pop").get_by_role("button", name="Cancel").click()
        page.click("#a-btn")
        page.locator(".cmt-pop").get_by_role("button", name="Cancel").click()
        assert page.evaluate("location.hash") == "#screen=a"
        assert page.evaluate("window.clicks") is None

    @check("local: create comment makes a numbered pin")
    def _():
        add_comment(page, "#a-title", "First comment")
        expect(page.locator(".cmt-pin")).to_have_count(1)
        expect(page.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, open")

    @check("local: reply, edit and delete a reply")
    def _():
        pop = page.locator(".cmt-pop")
        pop.locator('textarea[aria-label="Reply"]').fill("Reply one")
        pop.get_by_role("button", name="Reply", exact=True).click()
        expect(pop).to_contain_text("Reply one")
        pop.get_by_role("button", name="Edit").nth(1).click()  # first Edit belongs to the comment, second to the reply
        pop.locator('textarea[aria-label="Edit text"]').fill("Reply edited")
        pop.get_by_role("button", name="Save").click()
        expect(pop).to_contain_text("Reply edited")
        pop.get_by_role("button", name="Delete").nth(1).click()
        expect(pop).not_to_contain_text("Reply edited")
        pop.locator('textarea[aria-label="Reply"]').fill("Reply final")
        pop.get_by_role("button", name="Reply", exact=True).click()
        expect(pop).to_contain_text("Reply final")

    @check("local: resolve, counts and filter")
    def _():
        page.locator(".cmt-pop").get_by_role("button", name="Resolve").click()
        expect(page.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, resolved")
        open_panel(page)
        f = page.locator('[aria-label="Filter"] button')
        expect(f.nth(0)).to_have_text("Open (0)")
        expect(f.nth(1)).to_have_text("Resolved (1)")
        expect(f.nth(2)).to_have_text("All (1)")
        expect(page.locator(".cmt-item")).to_have_count(0)
        f.nth(1).click()
        expect(page.locator(".cmt-item")).to_have_count(1)
        f.nth(2).click()
        expect(page.locator(".cmt-item")).to_contain_text("1 reply")
        page.get_by_role("button", name="Close", exact=True).first.click()

    @check("local: screen switch shows the right pins and keeps per-screen numbers")
    def _():
        page.get_by_role("tab", name="Beta").click()
        expect(page.locator(".cmt-pin")).to_have_count(0)
        assert page.evaluate("location.hash") == "#screen=b"
        add_comment(page, "#b-title", "Beta comment")
        expect(page.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, open")
        page.get_by_role("tab", name="Alpha").click()
        expect(page.locator(".cmt-pin")).to_have_count(1)
        expect(page.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, resolved")

    @check("local: panel item switches screen and opens the thread")
    def _():
        page.get_by_role("tab", name="Beta").click()
        open_panel(page)
        page.locator('[aria-label="Filter"] button').nth(2).click()
        page.locator(".cmt-item", has_text="First comment").click()
        expect(page.get_by_role("tab", name="Alpha")).to_have_attribute("aria-selected", "true")
        expect(page.locator(".cmt-pop")).to_contain_text("First comment")

    @check("local: reload keeps comments, and #screen=b opens Beta")
    def _():
        page.reload()
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        assert len(page.evaluate("cmt.exportJSON().comments")) == 2
        assert not any("appVersion" in c for c in page.evaluate("cmt.exportJSON().comments")), "a static mockup has no app version"
        p2 = open_page(ctx, base + "/#screen=b")
        expect(p2.get_by_role("tab", name="Beta")).to_have_attribute("aria-selected", "true")
        expect(p2.locator("#b-title")).to_be_visible()
        expect(p2.locator("#a-title")).to_be_hidden()
        expect(p2.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, open")
        p2.close()

    @check("local: resize keeps pins inside their anchor box")
    def _():
        page.get_by_role("tab", name="Alpha").click()
        page.keyboard.press("Escape")
        wide = page.locator("#a-box").bounding_box()
        add_comment(page, "#a-box", "Offset comment", position={"x": wide["width"] * 0.75, "y": 50})
        page.keyboard.press("Escape")
        ratios = []
        for width in (1000, 375, 700):
            page.set_viewport_size({"width": width, "height": 800})
            page.wait_for_timeout(150)
            box = rect(page, "#a-box")
            idx = page.evaluate("[...document.querySelectorAll('.cmt-pin')].findIndex(p=>p.getAttribute('aria-label').startsWith('Comment 2'))")
            x, y = pin_center(page, idx)
            assert box["l"] <= x <= box["l"] + box["w"] and box["t"] <= y <= box["t"] + box["h"], (width, box, x, y)
            ratios.append(round((x - box["l"]) / box["w"], 2))
        assert max(ratios) - min(ratios) < 0.03, ratios  # same fractional offset at every width
        page.set_viewport_size({"width": 1000, "height": 800})

    @check("local: keyboard C toggles mode, Esc closes, [ and ] step through comments")
    def _():
        page.locator("body").press("c")
        expect(page.get_by_role("button", name="Browse")).to_have_attribute("aria-pressed", "true")
        page.click("#a-text")
        expect(page.locator(".cmt-pop")).to_have_count(0)
        page.locator("body").press("c")
        expect(page.get_by_role("button", name="Comment", exact=True)).to_have_attribute("aria-pressed", "true")
        open_panel(page)
        page.locator('[aria-label="Filter"] button').nth(2).click()
        page.get_by_role("button", name="Close", exact=True).first.click()
        page.locator("body").press("]")
        expect(page.locator(".cmt-pop")).to_be_visible()
        first = page.locator(".cmt-pop").get_attribute("aria-label")
        page.locator("body").press("]")
        expect(page.locator(".cmt-pop")).not_to_have_attribute("aria-label", first)
        page.locator("body").press("Escape")
        expect(page.locator(".cmt-pop")).to_have_count(0)

    @check("local: export Markdown content")
    def _():
        text = md(page)
        for want in ("# Test prototype: review comments", "## Alpha", "- [x] **#1** [Alpha screen] Ana: First comment",
                     "  - Ana: Reply final", "- [ ] **#2**", "## Beta", "- [ ] **#1** [Beta screen] Ana: Beta comment"):
            assert want in text, f"missing {want!r} in:\n{text}"
        state["json"] = page.evaluate("cmt.exportJSON()")
        state["md"] = text

    @check("local: import JSON merges by id")
    def _():
        fresh = browser.new_context()
        p = open_page(fresh, base + "/")
        data = state["json"]
        assert p.evaluate("o=>cmt.importJSON(o)", data) == 3
        assert p.evaluate("o=>cmt.importJSON(o)", data) == 0  # same ids: nothing new
        data2 = json.loads(json.dumps(data))
        data2["comments"][0]["replies"].append({"id": "extra001", "author": "Bo", "text": "Extra reply", "createdAt": "2030-01-01T00:00:00Z"})
        assert p.evaluate("o=>cmt.importJSON(o)", data2) == 1
        assert "  - Bo: Extra reply" in md(p)
        p.reload()
        p.wait_for_selector("#cmt-root[data-mode]", state="attached")
        assert len(p.evaluate("cmt.exportJSON().comments")) == 3  # persisted
        open_panel(p)
        p.set_input_files('input[type=file]', files=[{"name": "c.json", "mimeType": "application/json", "buffer": json.dumps(data).encode()}])
        expect(p.locator(".cmt-note")).to_contain_text("Imported 0 new items", timeout=EXPECT_TIMEOUT)
        fresh.close()

    def version_checks(p):
        expect(p.locator(".cmt-pin")).to_have_count(0)
        expect(p.get_by_role("button", name="Comments (0)")).to_be_visible()
        open_panel(p)
        sel = p.locator('select[aria-label="Version"]')
        assert set(sel.locator("option").all_inner_texts()) == {"v1", "v2 (current)", "All versions"}
        p.locator('[aria-label="Filter"] button').nth(2).click()
        sel.select_option("all")
        expect(p.locator(".cmt-item", has_text="First comment")).to_contain_text("from v1")
        sel.select_option("v1")
        # "First comment" anchor (#a-title) is gone, so it has no pin. "Offset comment" anchor (#a-box) still exists.
        labels = p.locator(".cmt-pin").evaluate_all("els=>els.map(e=>e.getAttribute('aria-label'))")
        assert labels == ["Comment 2, open"], labels
        p.locator(".cmt-item", has_text="First comment").click()  # anchor gone: thread still opens, read-only
        pop = p.locator(".cmt-pop")
        expect(pop).to_contain_text("First comment")
        assert pop.get_by_role("button", name="Resolve").count() + pop.get_by_role("button", name="Reopen").count() == 0
        assert pop.locator('textarea[aria-label="Reply"]').count() == 0
        assert "(from v1)" in md(p)
        p.close()

    @check("local: version change hides old pins and lists them as from v1")
    def _():
        (served / "index.html").write_text(fixture("v2", renamed=True), encoding="utf-8")
        try:
            version_checks(open_page(ctx, base + "/"))
        finally:
            (served / "index.html").write_text(fixture("v1"), encoding="utf-8")

    @check("local: 375 px layout, no horizontal overflow, targets 36 px or larger")
    def _():
        p = open_page(ctx, base + "/")
        p.set_viewport_size({"width": 375, "height": 700})
        open_panel(p)
        assert abs(p.locator(".cmt-panel").bounding_box()["width"] - 375) < 1
        assert p.evaluate("document.documentElement.scrollWidth") <= 375
        bar = p.locator(".cmt-bar").bounding_box()
        assert bar["x"] >= 0 and bar["x"] + bar["width"] <= 375, bar
        small = p.evaluate("[...document.querySelectorAll('.cmt-ui button, .cmt-ui select')].filter(e=>e.offsetParent&&e.getBoundingClientRect().height<36).length")
        assert small == 0, small
        p.close()

    @check("local: a static host shows no key notice")
    def _():
        expect(page.locator(".cmt-banner")).to_be_hidden()

    @check("local: pin numbers stay stable after a delete, and a new comment takes the next number")
    def _():
        c2 = browser.new_context(viewport={"width": 1000, "height": 800})
        p = open_page(c2, base + "/")
        for sel, text in (("#a-title", "One"), ("#a-text", "Two"), ("#a-box", "Three")):
            add_comment(p, sel, text)
            p.keyboard.press("Escape")
        labels = lambda: sorted(p.locator(".cmt-pin").evaluate_all("els=>els.map(e=>e.getAttribute('aria-label').split(',')[0])"))
        assert labels() == ["Comment 1", "Comment 2", "Comment 3"]
        p.locator('.cmt-pin[aria-label^="Comment 2,"]').click()
        p.locator(".cmt-pop").get_by_role("button", name="Delete").first.click()
        expect(p.locator(".cmt-pin")).to_have_count(2)
        assert labels() == ["Comment 1", "Comment 3"], labels()  # no renumbering
        add_comment(p, "#a-link", "Four")
        p.keyboard.press("Escape")
        assert labels() == ["Comment 1", "Comment 3", "Comment 4"], labels()
        p.locator('.cmt-pin[aria-label^="Comment 4,"]').click()  # delete the newest: its number must not come back
        p.locator(".cmt-pop").get_by_role("button", name="Delete").first.click()
        expect(p.locator(".cmt-pin")).to_have_count(2)
        add_comment(p, "#a-btn", "Five")
        p.keyboard.press("Escape")
        assert labels() == ["Comment 1", "Comment 3", "Comment 5"], labels()
        p.locator('.cmt-pin[aria-label^="Comment 5,"]').click()
        p.locator(".cmt-pop").get_by_role("button", name="Delete").first.click()
        add_comment(p, "#a-link", "Four again")
        p.keyboard.press("Escape")
        assert labels() == ["Comment 1", "Comment 3", "Comment 6"], labels()
        assert "**#3**" in md(p) and "**#6**" in md(p) and "**#2**" not in md(p)
        p.reload()
        p.wait_for_selector("#cmt-root[data-mode]", state="attached")
        assert labels() == ["Comment 1", "Comment 3", "Comment 6"], labels()
        c2.close()

    @check("local: old stored comments without n are numbered once, in creation order")
    def _():
        mk = lambda i, at: {"id": f"old0000{i}", "screen": "a", "version": "v1", "path": "#a-title", "fx": 0.5, "fy": 0.5, "pageX": 5, "pageY": 5,
                            "where": "Alpha screen", "text": f"old {i}", "author": "Ana", "createdAt": at, "resolved": False, "replies": []}
        legacy = [mk(1, "2030-01-03T00:00:00Z"), mk(2, "2030-01-01T00:00:00Z"), mk(3, "2030-01-02T00:00:00Z")]
        c3 = browser.new_context()
        c3.add_init_script("if (!localStorage.getItem('cmt:fix:v2')) localStorage.setItem('cmt:fix:v2', %s)" % json.dumps(json.dumps(legacy)))
        p = open_page(c3, base + "/")
        got = {c["id"]: c["n"] for c in p.evaluate("cmt.exportJSON().comments")}
        assert got == {"old00002": 1, "old00003": 2, "old00001": 3}, got
        stored = json.loads(p.evaluate("localStorage.getItem('cmt:fix:v2')"))
        assert {c["id"]: c["n"] for c in stored} == got  # saved, so it happens once
        c3.close()
    ctx.close()


def run_blocked(browser, base):
    ctx = browser.new_context()
    ctx.add_init_script("Object.defineProperty(window,'localStorage',{get(){throw new DOMException('blocked','SecurityError')}})")

    @check("local: storage blocked keeps working in memory and says Not saved")
    def _():
        p = open_page(ctx, base + "/")
        add_comment(p, "#a-title", "Memory only")
        expect(p.locator(".cmt-status")).to_have_text("Not saved")
        expect(p.locator(".cmt-note")).to_contain_text("Not saved")
        assert "Memory only" in md(p)
    ctx.close()


# ---- Layer behavior: empty space, name, keys, drafts, time, links, unread, reactions ----
def run_ux(browser, base):
    VP = {"width": 1000, "height": 800}
    MOUSEOUT = "document.body.dispatchEvent(new MouseEvent('mouseout', {bubbles: true, relatedTarget: null}))"

    def fresh():
        ctx = browser.new_context(viewport=VP)
        return ctx, open_page(ctx, base + "/")

    def stored(page):
        return page.evaluate("JSON.parse(localStorage.getItem('cmt:fix:v2') || '[]')")

    def box(page, sel):
        return page.locator(sel).bounding_box()

    def close_to(a, b, tol=3):
        return all(abs(a[k] - b[k]) <= tol for k in ("x", "y", "width", "height"))

    @check("empty space: hover outlines the nearest container, never body, and the outline hides when the pointer leaves the window")
    def _():
        ctx, page = fresh()
        sec = box(page, "section[data-screen=a]")
        for x, y in ((500, 700), (3, 300), (990, 200)):   # below the content, in the page margin, beside it
            page.mouse.move(x, y)
            page.wait_for_timeout(120)
            hl = box(page, ".cmt-hl")
            assert hl and close_to(hl, sec), f"outline at {x},{y} is {hl}, the section is {sec}"
        page.evaluate(MOUSEOUT)
        expect(page.locator(".cmt-hl")).to_be_hidden(timeout=EXPECT_TIMEOUT)
        ctx.close()

    @check("empty space: a click anchors to the nearest element, not body. The pin sits on its edge and follows it on resize")
    def _():
        ctx, page = fresh()
        sec = box(page, "section[data-screen=a]")
        page.mouse.click(500, 700)
        pop = page.locator(".cmt-pop")
        expect(pop).to_be_visible(timeout=EXPECT_TIMEOUT)
        pop.locator('input[aria-label="Your name"]').fill("Ana")
        pop.locator("textarea").fill("Below the content")
        pop.get_by_role("button", name="Save").click()
        expect(pop.locator('textarea[aria-label="Reply"]')).to_be_visible(timeout=EXPECT_TIMEOUT)
        c = stored(page)[-1]
        assert c["path"] not in ("body", "html", "") and c["where"] != "Page", c
        x, y = pin_center(page)
        assert abs(y - (sec["y"] + sec["height"])) <= 3 and sec["x"] <= x <= sec["x"] + sec["width"], (x, y, sec)
        page.set_viewport_size({"width": 700, "height": 800})
        page.wait_for_timeout(300)
        sec2, (x2, y2) = box(page, "section[data-screen=a]"), pin_center(page)
        assert sec2["x"] <= x2 <= sec2["x"] + sec2["width"] and abs(y2 - (sec2["y"] + sec2["height"])) <= 3, (x2, y2, sec2)
        ctx.close()

    @check("name: asked once in the first composer, shown in the toolbar, edited in place, and old comments keep their name")
    def _():
        ctx, page = fresh()
        page.click("#a-title")
        pop = page.locator(".cmt-pop")
        assert pop.locator('input[aria-label="Your name"]').count() == 1
        pop.locator('input[aria-label="Your name"]').fill("Ana")
        pop.locator("textarea").fill("First")
        pop.locator("textarea").press("Enter")
        expect(pop.locator('textarea[aria-label="Reply"]')).to_be_visible(timeout=EXPECT_TIMEOUT)
        name = page.locator(".cmt-bar .cmt-nm")
        expect(name).to_have_text("Commenting as Ana ✎")
        pop.get_by_role("button", name="Close").click()
        page.click("#a-text")
        expect(pop.locator("textarea").first).to_be_focused(timeout=EXPECT_TIMEOUT)   # the text box has the focus at once
        assert pop.locator('input[aria-label="Your name"]').count() == 0, "the name field must not come back"
        pop.get_by_role("button", name="Cancel").click()
        name.click()
        inp = page.locator('.cmt-bar input[aria-label="Your name"]')
        inp.fill("Bea")
        inp.press("Enter")
        expect(name).to_have_text("Commenting as Bea ✎")
        page.click("#a-text")
        pop.locator("textarea").fill("Second")
        pop.locator("textarea").press("Enter")
        expect(pop.locator('textarea[aria-label="Reply"]')).to_be_visible(timeout=EXPECT_TIMEOUT)
        assert [c["author"] for c in stored(page)] == ["Ana", "Bea"]   # the rename did not rewrite the first comment
        page.reload()
        expect(page.locator(".cmt-bar .cmt-nm")).to_have_text("Commenting as Bea ✎", timeout=EXPECT_TIMEOUT)
        ctx.close()

    @check("keys: Enter saves, Shift+Enter adds a line, Ctrl+Enter saves, and Enter on a button does not save")
    def _():
        ctx, page = fresh()
        page.click("#a-title")
        pop = page.locator(".cmt-pop")
        pop.locator('input[aria-label="Your name"]').fill("Ana")
        pop.locator("textarea").click()
        page.keyboard.type("line one")
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("line two")
        assert pop.locator('textarea[aria-label="Comment"]').count() == 1, "Shift+Enter must not save"
        page.keyboard.press("Enter")
        expect(pop.locator('textarea[aria-label="Reply"]')).to_be_visible(timeout=EXPECT_TIMEOUT)
        assert stored(page)[-1]["text"] == "line one\nline two"
        pop.locator('textarea[aria-label="Reply"]').fill("Reply by Ctrl+Enter")
        pop.locator('textarea[aria-label="Reply"]').press("Control+Enter")
        expect(pop.locator(".cmt-reply")).to_have_count(1, timeout=EXPECT_TIMEOUT)
        ctx.close()

    @check("draft: Esc or a click elsewhere keeps the unsent text for the next composer. Cancel throws it away")
    def _():
        ctx, page = fresh()
        page.evaluate("localStorage.setItem('cmt:fix:prefs', JSON.stringify({name: 'Ana', seen: true, mode: 'comment'}))")
        page.reload()
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        pop = page.locator(".cmt-pop")
        page.click("#a-title")
        page.keyboard.type("half written")
        page.keyboard.press("Escape")
        expect(pop).to_have_count(0)
        page.click("#a-text")
        expect(pop.locator("textarea")).to_have_value("half written", timeout=EXPECT_TIMEOUT)
        expect(pop).to_contain_text("unsent text is back")
        page.click("#a-title")   # a click elsewhere replaces the composer: the text moves along
        expect(pop.locator("textarea")).to_have_value("half written", timeout=EXPECT_TIMEOUT)
        pop.get_by_role("button", name="Cancel").click()
        page.click("#a-text")
        expect(pop.locator("textarea")).to_have_value("", timeout=EXPECT_TIMEOUT)
        ctx.close()

    @check("time: a comment shows relative time, and the full date is in the title")
    def _():
        ctx, page = fresh()
        add_comment(page, "#a-title", "Timed")
        t = page.locator(".cmt-pop .cmt-meta time")
        expect(t).to_have_text("just now")
        assert len(t.get_attribute("title")) > 8 and t.get_attribute("datetime").endswith("Z")
        ctx.close()

    @check("link: #comment=<id> opens that comment, also on its other screen. The Link button copies a link that holds the id")
    def _():
        ctx, page = fresh()
        page.get_by_role("tab", name="Beta").click()
        add_comment(page, "#b-title", "On beta")
        cid = stored(page)[-1]["id"]
        page.get_by_role("button", name="Link").click()
        expect(page.get_by_role("button", name="Copied")).to_be_visible(timeout=EXPECT_TIMEOUT)
        p2 = ctx.new_page()
        p2.goto(base + "/#comment=" + cid)
        p2.wait_for_selector("#cmt-root[data-mode]", state="attached")
        expect(p2.locator(".cmt-pop")).to_contain_text("On beta", timeout=EXPECT_TIMEOUT)
        expect(p2.get_by_role("tab", name="Beta")).to_have_attribute("aria-selected", "true")
        ctx.close()

    @check("new: comments of other people since the last visit show as a count. Opening the panel clears it")
    def _():
        ctx, page = fresh()
        page.evaluate("""() => {
          const old = new Date(Date.now() - 3600e3).toISOString(), now = new Date().toISOString();
          localStorage.setItem('cmt:fix:prefs', JSON.stringify({name: 'Ana', seen: true, mode: 'comment', lastSeen: old}));
          localStorage.setItem('cmt:fix:v2', JSON.stringify([{id: 'new00001', n: 1, screen: 'a', version: 'v1', path: '#a-title', fx: .5, fy: .5, pageX: 1, pageY: 1, where: 'Alpha', text: 'From Bob', author: 'Bob', createdAt: now, resolved: false, replies: []},
            {id: 'new00002', n: 2, screen: 'a', version: 'v1', path: '#a-text', fx: .5, fy: .5, pageX: 1, pageY: 1, where: 'Alpha', text: 'Mine', author: 'Ana', createdAt: now, resolved: false, replies: []}]));
        }""")
        page.reload()
        expect(page.locator(".cmt-new")).to_have_text("1 new", timeout=EXPECT_TIMEOUT)
        open_panel(page)
        expect(page.locator(".cmt-new")).to_be_hidden()
        page.reload()
        expect(page.locator(".cmt-new")).to_be_hidden()
        ctx.close()

    @check("reactions: a click toggles, chips show counts and names, twice returns to empty, and the export, the panel and a reload show them")
    def _():
        ctx, page = fresh()
        add_comment(page, "#a-title", "React to me", name="Ana")
        pop = page.locator(".cmt-pop")
        chips = pop.locator(".cmt-react .cmt-chip")
        pop.get_by_role("button", name="Add reaction").first.click()
        pop.get_by_role("button", name="React 👍").click()
        expect(chips.filter(has_text="👍")).to_have_text("👍 1")
        assert chips.filter(has_text="👍").get_attribute("title") == "Ana" and chips.filter(has_text="👍").get_attribute("aria-pressed") == "true"
        pop.get_by_role("button", name="Add reaction").first.click()
        pop.get_by_role("button", name="React ✅").click()
        expect(chips.filter(has_text="✅")).to_have_text("✅ 1")
        page.evaluate("""() => { const c = JSON.parse(localStorage.getItem('cmt:fix:v2')); c[0].reactions['👍'].push('Bo'); localStorage.setItem('cmt:fix:v2', JSON.stringify(c)); }""")
        page.reload()
        page.wait_for_selector("#cmt-root[data-mode]", state="attached")
        open_panel(page)
        expect(page.locator(".cmt-item .cmt-rx")).to_have_text("👍2 ✅1", timeout=EXPECT_TIMEOUT)
        assert "[👍 2 · ✅ 1]" in md(page), md(page)
        page.locator(".cmt-item .cmt-ijump").click()
        expect(chips.filter(has_text="👍")).to_have_attribute("title", "Ana, Bo", timeout=EXPECT_TIMEOUT)
        chips.filter(has_text="👍").click()   # Ana takes hers back
        expect(chips.filter(has_text="👍")).to_have_text("👍 1")
        chips.filter(has_text="👍").click()   # Ana puts it on again
        expect(chips.filter(has_text="👍")).to_have_text("👍 2")
        chips.filter(has_text="✅").click()   # on, then off: the chip is gone
        expect(chips.filter(has_text="✅")).to_have_count(0)
        assert stored(page)[0]["reactions"] == {"👍": ["Bo", "Ana"]}, stored(page)[0]
        ctx.close()

    @check("reactions: a reply takes them too, and the export shows them after the reply")
    def _():
        ctx, page = fresh()
        add_comment(page, "#a-title", "Parent", name="Ana")
        pop = page.locator(".cmt-pop")
        pop.locator('textarea[aria-label="Reply"]').fill("Child")
        pop.get_by_role("button", name="Reply", exact=True).click()
        expect(pop.locator(".cmt-reply")).to_have_count(1, timeout=EXPECT_TIMEOUT)
        pop.locator(".cmt-reply").get_by_role("button", name="Add reaction").click()
        pop.get_by_role("button", name="React 🎉").click()
        expect(pop.locator(".cmt-reply .cmt-chip", has_text="🎉")).to_have_text("🎉 1")
        assert "Ana: Child [🎉 1]" in md(page), md(page)
        ctx.close()

    @check("emoji: the palette inserts at the cursor in the composer and in the reply box, with no network call")
    def _():
        ctx, page = fresh()
        reqs = []
        page.on("request", lambda r: reqs.append(r.url) if not r.url.startswith(base) else None)
        page.click("#a-title")
        pop = page.locator(".cmt-pop")
        pop.locator('input[aria-label="Your name"]').fill("Ana")
        ta = pop.locator("textarea")
        ta.fill("ab")
        ta.evaluate("e => e.setSelectionRange(1, 1)")
        pop.get_by_role("button", name="Add emoji").click()
        pop.get_by_role("button", name="Insert 🔥").click()
        expect(ta).to_have_value("a🔥b")
        pop.get_by_role("button", name="Save").click()
        expect(pop.locator('textarea[aria-label="Reply"]')).to_be_visible(timeout=EXPECT_TIMEOUT)
        reply = pop.locator('textarea[aria-label="Reply"]')
        reply.fill("x")
        pop.get_by_role("button", name="Add emoji").click()
        pop.get_by_role("button", name="Insert ⚠️").click()
        expect(reply).to_have_value("x⚠️")
        assert not reqs, reqs
        ctx.close()


# ---- Build: the repo template holds a marker, a built page holds the layer ----
@check("build: the template holds only the marker. inject.py builds the layer in, upgrades an old block, is idempotent, and leaves the rest alone")
def inject_test():
    assert "<!-- cmt:layer -->" in TEMPLATE and "window.cmt" not in TEMPLATE and "cmt:begin" not in TEMPLATE
    layer = (SKILL / "assets" / "layer" / "layer.js").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "index.html", Path(d) / "dist" / "index.html"
        src.write_text(TEMPLATE, encoding="utf-8")
        run = lambda *a: subprocess.run([sys.executable, str(SKILL / "scripts" / "inject.py"), *map(str, a)], capture_output=True, text=True)
        assert run(src, out).returncode == 0
        built = out.read_text(encoding="utf-8")
        assert layer in built and built.count("<!-- cmt:begin") == 1 and built.count("window.cmt =") == 1
        assert built.split("<!-- cmt:begin")[0] == TEMPLATE.split("<!-- cmt:layer -->")[0]   # the rest is untouched
        assert src.read_text(encoding="utf-8") == TEMPLATE, "the source page must not change when an output file is given"
        old = re.sub(r"(<!-- cmt:begin[^>]*-->).*?(<!-- cmt:end -->)", r"\1<script>/*old*/</script>\2", built, flags=re.S)
        src.write_text(old, encoding="utf-8")
        assert run(src).returncode == 0 and src.read_text(encoding="utf-8") == built   # an old layer is replaced in place
        assert run(src).returncode == 0 and src.read_text(encoding="utf-8") == built   # and a second run changes nothing
        src.write_text("<html></html>", encoding="utf-8")
        assert run(src).returncode != 0   # no marker, no layer


# ---- Shared mode ----
def api(base, path="", method="GET", body=None, key=None):
    req = urllib.request.Request(f"{base}/api/comments{path}", method=method, data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("content-type", "application/json")
    if key:
        req.add_header("x-review-key", key)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def start_wrangler(tmp):
    build = tmp / "worker-build"
    (build / "public").mkdir(parents=True)
    shutil.copy(SKILL / "assets" / "worker" / "worker.js", build / "worker.js")
    cfg = (SKILL / "assets" / "worker" / "wrangler.jsonc.template").read_text(encoding="utf-8")
    cfg = cfg.replace("__NAME__", "layer-test").replace("__KV_ID__", "0" * 32).replace("__DATE__", time.strftime("%Y-%m-%d", time.gmtime()))
    (build / "wrangler.jsonc").write_text(cfg, encoding="utf-8")
    (build / "public" / "index.html").write_text(fixture(), encoding="utf-8")
    (build / ".dev.vars").write_text("REVIEW_KEY=test\n", encoding="utf-8")  # temp dir only, never the repo
    port = free_port()
    log = open(tmp / "wrangler.log", "w")
    proc = subprocess.Popen(
        ["npm", "exec", "--yes", f"wrangler@{WRANGLER}", "--", "dev", "--port", str(port), "--ip", "127.0.0.1",
         "--inspector-port", str(free_port()), "--persist-to", str(tmp / "state")],
        cwd=build, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        env={**os.environ, "CI": "1", "WRANGLER_SEND_METRICS": "false", "NO_COLOR": "1"})
    base = f"http://127.0.0.1:{port}"
    for _ in range(180):
        if proc.poll() is not None:
            raise RuntimeError("wrangler dev exited:\n" + (tmp / "wrangler.log").read_text()[-1500:])
        try:
            if api(base, "?project=fix")[0] == 401:
                return proc, base
        except Exception:  # noqa: BLE001 - server not up yet
            pass
        time.sleep(0.5)
    raise RuntimeError("wrangler dev did not start:\n" + (tmp / "wrangler.log").read_text()[-1500:])


def stop(proc):
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


def run_shared(browser, base):
    K = "test"

    @check("shared: API returns 401 without the key (GET and POST) and with a wrong key")
    def _():
        assert api(base, "?project=fix")[0] == 401
        assert api(base, "?project=fix", key="nope")[0] == 401
        assert api(base, "", "POST", {"project": "fix", "type": "comment"})[0] == 401
        assert api(base, "?project=fix&id=abcd1234", "DELETE")[0] == 401
        assert api(base, "?project=fix", key=K) == (200, {"comments": []})

    @check("shared: API validates input and answers JSON errors")
    def _():
        ok = {"project": "val", "type": "comment", "id": "val00001", "text": "hi", "author": "Ana", "screen": "a", "version": "v1"}
        assert api(base, "", "POST", {**ok, "text": "x" * 4001}, K)[0] == 400
        assert api(base, "", "POST", {**ok, "author": "x" * 81}, K)[0] == 400
        s, b = api(base, "", "POST", {**ok, "surprise": 1}, K)
        assert s == 400 and "unknown field" in b["error"]
        assert api(base, "", "POST", {**ok, "project": "Bad Name"}, K)[0] == 400
        assert api(base, "", "POST", ok, K)[0] == 201
        assert api(base, "", "POST", ok, K)[0] == 409
        assert api(base, "", "PATCH", {"project": "val", "id": "val00001", "text": "changed", "author": "Bo"}, K)[0] == 403
        assert api(base, "", "PATCH", {"project": "val", "id": "val00001", "text": "changed", "author": "Ana"}, K)[0] == 200
        assert api(base, "?project=val&id=val00001&author=Bo", "DELETE", key=K)[0] == 403
        assert api(base, "?project=val&id=val00001&author=Ana", "DELETE", key=K)[0] == 200
        assert api(base, "?project=val&bogus=1", key=K)[0] == 400

    wk = lambda m="GET", extra="", body=None: api(base, "?project=rxw" if m == "GET" else extra, m, body, K)

    @check("shared: reactions toggle on a comment and a reply, are idempotent, and return to empty")
    def _():
        base = {"project": "rxw", "type": "comment", "id": "rx000001", "text": "hi", "author": "Ana", "screen": "a", "version": "v1"}
        assert wk("POST", "", base)[0] == 201
        assert wk("POST", "", {"project": "rxw", "type": "reply", "commentId": "rx000001", "id": "rx000002", "text": "re", "author": "Bo"})[0] == 201
        react = lambda who, on, e="👍", **kw: wk("PATCH", "", {"project": "rxw", "id": "rx000001", "react": e, "on": on, "author": who, **kw})
        got = lambda: wk("GET")[1]["comments"][0]
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

    @check("shared: reactions refuse an emoji outside the set, bad fields, more than 50 names, and long names")
    def _():
        p = {"project": "rxw", "id": "rx000001"}
        patch = lambda **kw: wk("PATCH", "", {**p, "author": "Bo", "on": True, "react": "👍", **kw})[0]
        assert patch(react="💩") == 400 and patch(react=5) == 400 and patch(react="👍👍") == 400
        assert patch(on="yes") == 400 and patch(author="") == 400 and patch(author="x" * 81) == 400 and patch(foo=1) == 400
        assert patch(text="x") == 400 and patch(resolved=True) == 400   # react does not mix with other changes
        assert patch(replyId="nope0000") == 404
        post = lambda cid, **kw: wk("POST", "", {"project": "rxw", "type": "comment", "id": cid, "text": "t", "author": "Ana", **kw})[0]
        assert post("rx000010", reactions={"💩": ["a"]}) == 400 and post("rx000011", reactions=[]) == 400 and post("rx000012", reactions={"👍": "a"}) == 400
        assert post("rx000013", reactions={"👍": ["a"] * 51}) == 400 and post("rx000014", reactions={"👍": ["x" * 81]}) == 400
        assert post("rx000015", reactions={"👍": ["a", "a", "b"], "✅": []}) == 201   # an import keeps unique names, drops empty lists
        assert [c["reactions"] for c in wk("GET")[1]["comments"] if c["id"] == "rx000015"] == [{"👍": ["a", "b"]}]
        for i in range(50):
            assert wk("PATCH", "", {**p, "id": "rx000015", "react": "🎉", "on": True, "author": f"n{i}"})[0] == 200
        assert wk("PATCH", "", {**p, "id": "rx000015", "react": "🎉", "on": True, "author": "n50"})[0] == 422
        assert wk("PATCH", "", {**p, "id": "rx000015", "react": "🎉", "on": True, "author": "n0"})[0] == 200   # already there: no error

    @check("shared: API caps a project at 500 comments")
    def _():
        for i in range(500):
            s, _b = api(base, "", "POST", {"project": "cap", "type": "comment", "id": f"cap{i:05d}", "text": "t", "author": "Ana"}, K)
            assert s == 201, (i, s)
        s, b = api(base, "", "POST", {"project": "cap", "type": "comment", "id": "cap99999", "text": "t", "author": "Ana"}, K)
        assert s == 422 and "limit" in b["error"], (s, b)

    ctx_a, ctx_b = browser.new_context(), browser.new_context()
    url = f"{base}/?k={K}"
    state = {}

    @check("shared: comment made in context A shows in context B after reload")
    def _():
        b = open_page(ctx_b, url)
        a = open_page(ctx_a, url)
        assert a.evaluate("cmt.mode()") == "shared" and b.evaluate("cmt.mode()") == "shared", (a.evaluate("cmt.mode()"), b.evaluate("cmt.mode()"))
        expect(a.locator(".cmt-status")).to_have_text("Synced", timeout=EXPECT_TIMEOUT)
        add_comment(a, "#a-title", "Shared comment", name="Ana")
        expect(a.locator(".cmt-status")).to_have_text("Synced", timeout=EXPECT_TIMEOUT)
        stored = api(base, "?project=fix", key=K)[1]["comments"]
        assert len(stored) == 1, stored
        b.reload()
        b.wait_for_selector("#cmt-root[data-mode]", state="attached")
        expect(b.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, open", timeout=EXPECT_TIMEOUT)
        state["a"], state["b"] = a, b

    a, b = state["a"], state["b"]

    @check("shared: editing a comment of your own reaches the Worker (the edit sends no replyId for a comment)")
    def _():
        a.locator(".cmt-pin").click()
        pa = a.locator(".cmt-pop")
        pa.get_by_role("button", name="Edit").first.click()
        pa.locator('textarea[aria-label="Edit text"]').fill("Shared comment, edited")
        pa.get_by_role("button", name="Save").click()
        expect(a.locator(".cmt-status")).to_have_text("Synced", timeout=EXPECT_TIMEOUT)
        assert api(base, "?project=fix", key=K)[1]["comments"][0]["text"] == "Shared comment, edited"
        a.keyboard.press("Escape")

    @check("shared: a reaction in A reaches the Worker and B, and a second click takes it back")
    def _():
        a.locator(".cmt-pin").click()
        pa = a.locator(".cmt-pop")
        pa.get_by_role("button", name="Add reaction").first.click()
        pa.get_by_role("button", name="React 🎉").click()
        expect(a.locator(".cmt-status")).to_have_text("Synced", timeout=EXPECT_TIMEOUT)
        assert api(base, "?project=fix", key=K)[1]["comments"][0]["reactions"] == {"🎉": ["Ana"]}
        b.reload()
        b.wait_for_selector("#cmt-root[data-mode]", state="attached")
        b.locator(".cmt-pin").click()
        expect(b.locator(".cmt-pop .cmt-react .cmt-chip", has_text="🎉")).to_have_attribute("title", "Ana", timeout=EXPECT_TIMEOUT)
        pa.locator(".cmt-react .cmt-chip", has_text="🎉").click()
        expect(a.locator(".cmt-status")).to_have_text("Synced", timeout=EXPECT_TIMEOUT)
        assert "reactions" not in api(base, "?project=fix", key=K)[1]["comments"][0]
        a.keyboard.press("Escape")
        b.keyboard.press("Escape")

    @check("shared: reply and resolve in B reach A on window focus")
    def _():
        b.locator(".cmt-pin").click()
        pop = b.locator(".cmt-pop")
        nm = pop.locator('input[aria-label="Your name"]')
        if nm.count():
            nm.fill("Bo")
        pop.locator('textarea[aria-label="Reply"]').fill("From B")
        pop.get_by_role("button", name="Reply", exact=True).click()
        pop.get_by_role("button", name="Resolve").click()
        expect(b.locator(".cmt-status")).to_have_text("Synced", timeout=EXPECT_TIMEOUT)
        a.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(a.locator(".cmt-pin")).to_have_attribute("aria-label", "Comment 1, resolved", timeout=EXPECT_TIMEOUT)
        assert "  - Bo: From B" in md(a)

    @check("shared: offline edits stay local, then sync when the network returns")
    def _():
        ctx_b.set_offline(True)
        b.get_by_role("tab", name="Beta").click()
        add_comment(b, "#b-text", "Written offline", name="Bo")
        expect(b.locator(".cmt-status")).to_have_text("Offline — saved locally", timeout=EXPECT_TIMEOUT)
        ctx_b.set_offline(False)
        b.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(b.locator(".cmt-status")).to_have_text("Synced", timeout=15000)
        assert any(c["text"] == "Written offline" for c in api(base, "?project=fix", key=K)[1]["comments"])

    @check("shared: pull-comments.sh prints the same Markdown as the page")
    def _():
        out = subprocess.run(["bash", str(SKILL / "scripts" / "pull-comments.sh"), url], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        b.reload()
        b.wait_for_selector("#cmt-root[data-mode]", state="attached")
        expect(b.locator(".cmt-status")).to_have_text("Synced", timeout=EXPECT_TIMEOUT)
        assert out.stdout.strip() == md(b).strip(), f"--- script\n{out.stdout}\n--- page\n{md(b)}"

    @check("shared: a page without the key falls back to local mode")
    def _():
        ctx_c = browser.new_context()
        p = open_page(ctx_c, f"{base}/")
        assert p.evaluate("cmt.mode()") == "local"
        expect(p.locator(".cmt-pin")).to_have_count(0)
        ctx_c.close()

    @check("shared: the server numbers comments, ignores a client n, and keeps numbers after a delete")
    def _():
        mk = lambda i, **kw: api(base, "", "POST", {"project": "num", "type": "comment", "id": f"num0000{i}", "text": "t", "author": "Ana", "screen": "a", "version": "v1", **kw}, K)
        for i in (1, 2, 3):
            assert mk(i, n=99)[0] == 201
        assert [c["n"] for c in api(base, "?project=num", key=K)[1]["comments"]] == [1, 2, 3]
        assert api(base, "?project=num&id=num00002&author=Ana", "DELETE", key=K)[0] == 200
        assert mk(4)[0] == 201 and mk(5, screen="b")[0] == 201
        got = {c["id"]: c["n"] for c in api(base, "?project=num", key=K)[1]["comments"]}
        assert got == {"num00001": 1, "num00003": 3, "num00004": 4, "num00005": 1}, got
        assert api(base, "?project=num&id=num00004&author=Ana", "DELETE", key=K)[0] == 200  # delete the newest: its number is not used again
        assert mk(8)[0] == 201
        got = {c["id"]: c["n"] for c in api(base, "?project=num", key=K)[1]["comments"]}
        assert got["num00008"] == 5, got
        assert mk(6, n="x")[0] == 400

    @check("shared: a link without the key shows a visible notice, a wrong key says so, and Dismiss hides it")
    def _():
        ctx_c = browser.new_context()
        p = open_page(ctx_c, f"{base}/")
        banner = p.locator(".cmt-banner")
        expect(banner).to_be_visible(timeout=EXPECT_TIMEOUT)
        expect(banner).to_contain_text("no review key")
        expect(banner).to_contain_text("Download JSON")
        expect(p.locator(".cmt-status")).to_have_text("Saved on this device")
        p.get_by_role("button", name="Dismiss").click()
        expect(banner).to_be_hidden()
        p2 = open_page(ctx_c, f"{base}/?k=wrong")
        expect(p2.locator(".cmt-banner")).to_contain_text("key in this link is wrong", timeout=EXPECT_TIMEOUT)
        p3 = open_page(ctx_c, f"{base}/?k={K}")
        assert p3.evaluate("cmt.mode()") == "shared"
        expect(p3.locator(".cmt-banner")).to_be_hidden()
        ctx_c.close()
    ctx_a.close()
    ctx_b.close()



WHOAMI_TWO = """ ⛅️ wrangler 4.0.0
Getting User settings...
👋 You are logged in with an OAuth Token, associated with the email test@example.com.
┌──────────────────┬──────────────────────────────────┐
│ Account Name     │ Account ID                       │
├──────────────────┼──────────────────────────────────┤
│ First account    │ {a} │
│ Second account   │ {b} │
└──────────────────┴──────────────────────────────────┘
"""
STUB_NPM = """#!/usr/bin/env bash
# Stand-in for: npm exec --yes wrangler@<pinned version> -- <wrangler args>
shift 4
echo "$*" >> "$STUB_LOG"
case "$1" in
  whoami) cat "$STUB_WHOAMI" ;;
  kv) [[ "$2 $3" == "namespace list" ]] && echo '[{"id":"00000000000000000000000000000001","title":"guard-test-comments"}]' ;;
  deploy) echo "Deployed https://guard-test.example.workers.dev" ;;
esac
exit 0
"""


def run_deploy_guard(tmp):
    bin_dir, proto = tmp / "stubbin", tmp / "proto"
    bin_dir.mkdir()
    proto.mkdir()
    (proto / "index.html").write_text(fixture(), encoding="utf-8")
    (bin_dir / "npm").write_text(STUB_NPM, encoding="utf-8")
    (bin_dir / "npm").chmod(0o755)
    A, B = "a" * 32, "b" * 32

    def deploy(whoami, extra_env=None):
        n = len(list(tmp.glob("stub-*.log")))
        (tmp / "whoami.txt").write_text(whoami, encoding="utf-8")
        log = tmp / f"stub-{n}.log"
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_WHOAMI": str(tmp / "whoami.txt"), "STUB_LOG": str(log), "BUILD_DIR": str(tmp / f"build-{n}")}
        env.pop("CLOUDFLARE_ACCOUNT_ID", None)
        env.update(extra_env or {})
        log.touch()
        r = subprocess.run(["bash", str(SKILL / "scripts" / "deploy.sh"), str(proto), "guard-test"], env=env, capture_output=True, text=True, timeout=60)
        return r, log.read_text()

    @check("deploy.sh: more than one account without CLOUDFLARE_ACCOUNT_ID stops and lists the accounts")
    def _():
        r, calls = deploy(WHOAMI_TWO.format(a=A, b=B))
        assert r.returncode == 3 and r.stdout == "", (r.returncode, r.stdout, r.stderr)
        assert A in r.stderr and B in r.stderr and "CLOUDFLARE_ACCOUNT_ID" in r.stderr, r.stderr
        assert "deploy" not in calls and "kv" not in calls, calls  # nothing was created or deployed

    @check("deploy.sh: more than one account with CLOUDFLARE_ACCOUNT_ID goes on, and one account needs no variable")
    def _():
        r, calls = deploy(WHOAMI_TWO.format(a=A, b=B), {"CLOUDFLARE_ACCOUNT_ID": B})
        assert r.returncode == 0 and re.fullmatch(r"https://guard-test\.example\.workers\.dev/\?k=[A-Za-z0-9_-]+\n", r.stdout), (r.stdout, r.stderr)
        assert "deploy" in calls
        one = WHOAMI_TWO.format(a=A, b=B).replace(f"│ Second account   │ {B} │\n", "")
        r, _c = deploy(one)
        assert r.returncode == 0 and ".workers.dev/?k=" in r.stdout, (r.stdout, r.stderr)

    @check("deploy.sh: builds the layer into the deployed page only, and pins Wrangler")
    def _():
        raw = tmp / "proto-raw"
        raw.mkdir()
        (raw / "index.html").write_text(TEMPLATE, encoding="utf-8")
        n = len(list(tmp.glob("stub-*.log")))
        (tmp / "whoami.txt").write_text(WHOAMI_TWO.format(a=A, b=B), encoding="utf-8")
        log = tmp / f"stub-{n}.log"
        log.touch()
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_WHOAMI": str(tmp / "whoami.txt"), "STUB_LOG": str(log), "BUILD_DIR": str(tmp / f"build-{n}"), "CLOUDFLARE_ACCOUNT_ID": B}
        r = subprocess.run(["bash", str(SKILL / "scripts" / "deploy.sh"), str(raw), "guard-test"], env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        deployed = (tmp / f"build-{n}" / "public" / "index.html").read_text(encoding="utf-8")
        assert "window.cmt =" in deployed and "<!-- cmt:layer -->" not in deployed
        assert (raw / "index.html").read_text(encoding="utf-8") == TEMPLATE, "deploy.sh must not change the source page"
        assert re.fullmatch(r"\d+\.\d+\.\d+", WRANGLER) and "@latest" not in (SKILL / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    @check("deploy.sh: not logged in still stops with exit code 2")
    def _():
        r, calls = deploy("You are not authenticated. Please run `wrangler login`.\n")
        assert r.returncode == 2 and "wrangler login" in r.stderr and "deploy" not in calls, (r.returncode, r.stderr)



def main():
    tmp = Path(tempfile.mkdtemp(prefix="mockup-review-test-"))
    served = tmp / "static"
    served.mkdir()
    (served / "index.html").write_text(fixture(), encoding="utf-8")
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(served))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    proc = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception:  # noqa: BLE001 - bundled browser missing or wrong version
                browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium")
            run_local(browser, base, served)
            run_blocked(browser, base)
            run_ux(browser, base)
            run_deploy_guard(tmp)
            try:
                proc, shared_base = start_wrangler(tmp)
                run_shared(browser, shared_base)
            except Exception as e:  # noqa: BLE001
                results.append(("shared: wrangler dev starts", e))
                print(f"FAIL  shared: wrangler dev starts: {str(e)[:600]}")
            browser.close()
    finally:
        if proc:
            stop(proc)
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, e in results if e]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

