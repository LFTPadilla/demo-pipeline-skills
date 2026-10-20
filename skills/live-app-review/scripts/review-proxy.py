#!/usr/bin/env python3
"""Token-gated review proxy for a RUNNING web app. Python 3.9+, standard library only.

Usage: REVIEW_TOKEN=<optional> review-proxy.py --config review.json [--port N] [--public-host HOST] [--expires-at ISO] [--check]

review.json is the one source of settings. review_config.py checks it, the same way review.py does. The review folder is the folder of review.json.
The proxy renders the wrapper page from assets/template/review-frame.html. The flags are runtime values that review.py computes:
the port, the public host (an empty value turns the host of the config off), and the end time.

Routes (the proxy listens on 127.0.0.1 only). The first match ends the request:
  any path, after expiry   410 page
  no cookie                403 page
  /__review/               the wrapper page, then static files from the review folder
  /__review/layer.css      the comment layer (mockup-review/assets/layer), also layer.js
  /__review/meta           app version, expiry time, read-only flag, reset flag (JSON)
  /__review/reset          POST: run the reset command from review.json
  /__review/api/comments   comments API, same contract as assets/worker/worker.js, stored in a JSON file
  /__review/session        optional server-side auto-login
  mock rules               canned JSON answers from review.json
  read-only check          403 for POST, PUT, PATCH and DELETE that are not in allowWrite
  everything else          reverse proxy to the upstream with the longest matching route prefix

Every request needs the session cookie. A request with ?t=<token> sets the cookie and redirects without t.
The share link goes to stdout once. It is never written to a file or a log.
"""
import argparse
import copy
import hashlib
import hmac
import http.client
import http.server
import json
import math
import mimetypes
import os
import posixpath
import re
import secrets
import select
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_config  # noqa: E402  the one validator for review.json

PREFIX = review_config.PREFIX

# The wrapper page loads the comment layer from these two URLs. The files are the canonical layer of the sibling skill mockup-review.
LAYER_DIR = Path(__file__).resolve().parent.parent.parent / "mockup-review" / "assets" / "layer"
LAYER_FILES = {"layer.css": "text/css; charset=utf-8", "layer.js": "text/javascript; charset=utf-8"}
COOKIE = "review_session"
COOKIE_AGE = 7 * 24 * 3600
SESSION_TTL = 600
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers", "transfer-encoding", "upgrade"}
OVERRIDE = {"x-http-method-override", "x-http-method", "x-method-override"}  # an app could turn a GET into a DELETE with these
DROP_IN = HOP | OVERRIDE | {"host", "content-length", "expect", "forwarded", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip"}
DROP_OUT = HOP | {"server", "date", "referrer-policy", "x-robots-tag"}

# ---- Comments store: same limits and rules as assets/worker/worker.js ----
MAX = dict(text=4000, author=80, where=200, path=1000, screen=64, version=20, comments=500, replies=100, body=200000, reactors=50)
REACTIONS = ("👍", "✅", "👀", "❤️", "🎉", "❓")  # the same fixed set as the layer and the Worker
ID = re.compile(r"[a-z0-9]{4,40}")
APP_VERSION = re.compile(r"[A-Za-z0-9._+-]{0,40}")
PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
COMMENT_FIELDS = ["project", "type", "id", "n", "screen", "version", "appVersion", "path", "fx", "fy", "pageX", "pageY", "where", "text", "author", "createdAt", "resolved", "replies", "reactions"]
REPLY_FIELDS = ["project", "type", "commentId", "id", "text", "author", "createdAt", "reactions"]
PATCH_FIELDS = ["project", "id", "replyId", "text", "resolved", "author", "react", "on"]
DELETE_PARAMS = ["project", "id", "replyId", "author"]


class Fail(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def exact(keys, allowed):
    extra = [k for k in keys if k not in allowed]
    if extra:
        raise Fail(400, "unknown field: " + extra[0])


def text(v, name, limit, required=True):
    if not isinstance(v, str):
        raise Fail(400, name + " must be a string")
    if required and not v.strip():
        raise Fail(400, name + " is required")
    if len(v) > limit:
        raise Fail(400, f"{name} is longer than {limit} characters")
    return v


def num(b, name):
    if name not in b:
        return 0
    v = b[name]
    try:
        ok = not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
    except OverflowError:  # an integer too large for a float: the Worker reads it as Infinity
        ok = False
    if not ok:
        raise Fail(400, name + " must be a number")
    return v


def ident(v, name):
    if not isinstance(v, str) or not ID.fullmatch(v):
        raise Fail(400, name + " must be 4 to 40 lowercase letters or digits")
    return v


def when(b):
    if "createdAt" not in b:
        d = datetime.now(timezone.utc)
    else:
        v = b["createdAt"]
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
        except (AttributeError, ValueError, OverflowError):
            raise Fail(400, "createdAt must be an ISO date") from None
    return d.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dflt(b, k, d):  # JavaScript "b[k] ?? d"
    return d if b.get(k) is None else b[k]


def app_version(v):
    if not isinstance(v, str) or not APP_VERSION.fullmatch(v):
        raise Fail(400, "appVersion must have up to 40 characters from A-Z a-z 0-9 . _ + -")
    return v


def reactions(v):  # { emoji: [names] } from a client. Only the fixed set, at most 50 unique names of up to 80 characters
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise Fail(400, "reactions must be an object")
    out = {}
    for e, names in v.items():
        if e not in REACTIONS:
            raise Fail(400, "reaction not allowed: " + e)
        if not isinstance(names, list) or len(names) > MAX["reactors"]:
            raise Fail(400, f"reactions must be a list of up to {MAX['reactors']} names")
        uniq = list(dict.fromkeys(text(n, "reaction name", MAX["author"]) for n in names))
        if uniq:
            out[e] = uniq
    return out


def react(item, b):  # "on" is the wanted state, so a repeated request changes nothing
    e, on, who = b["react"], b.get("on"), text(b.get("author"), "author", MAX["author"])
    if not isinstance(e, str) or e not in REACTIONS:
        raise Fail(400, "reaction not allowed")
    if not isinstance(on, bool):
        raise Fail(400, "on must be true or false")
    names = item.get("reactions", {}).get(e, [])
    if on and who not in names:
        if len(names) >= MAX["reactors"]:
            raise Fail(422, "reaction limit reached")
        names = names + [who]
    elif not on:
        names = [n for n in names if n != who]
    rx = dict(item.get("reactions", {}), **{e: names})
    item["reactions"] = {k: v for k, v in rx.items() if v}
    if not item["reactions"]:
        del item["reactions"]


def make_reply(r):
    if not isinstance(r, dict):
        raise Fail(400, "reply must be an object")
    exact(r.keys(), [k for k in REPLY_FIELDS if k not in ("project", "type", "commentId")])
    return {"id": ident(r.get("id"), "reply id"), "author": text(r.get("author"), "author", MAX["author"]),
            "text": text(r.get("text"), "text", MAX["text"]), "createdAt": when(r), **({"reactions": rx} if (rx := reactions(r.get("reactions"))) else {})}


def group(c):
    return (c["screen"], c["version"])


def next_n(lst, c, hw):  # hw: highest number ever used per screen and version, so a delete never frees a number
    return max(hw.get("|".join(group(c)), 0), max((x.get("n", 0) for x in lst if group(x) == group(c)), default=0)) + 1


def backfill(lst, hw):  # old comments without n get the next free number once, in creation order
    for c in sorted(lst, key=lambda x: (x["createdAt"], x["id"])):
        if not c.get("n"):
            c["n"] = next_n(lst, c, hw)
            hw["|".join(group(c))] = c["n"]


def find(lst, cid):
    for c in lst:
        if c["id"] == cid:
            return c
    raise Fail(404, "comment not found")


def create(lst, b, hw):
    if b.get("type") == "reply":
        exact(b.keys(), REPLY_FIELDS)
        c = find(lst, ident(b.get("commentId"), "commentId"))
        r = make_reply({k: b[k] for k in ("id", "author", "text", "createdAt", "reactions") if k in b})
        if len(c["replies"]) >= MAX["replies"]:
            raise Fail(422, "reply limit reached")
        if any(x["id"] == r["id"] for x in c["replies"]):
            raise Fail(409, "reply exists")
        c["replies"].append(r)
        return
    if b.get("type") != "comment":
        raise Fail(400, "type must be comment or reply")
    exact(b.keys(), COMMENT_FIELDS)
    if len(lst) >= MAX["comments"]:
        raise Fail(422, "comment limit reached")
    reps = b.get("replies")
    if reps is not None and (not isinstance(reps, list) or len(reps) > MAX["replies"]):
        raise Fail(400, "invalid replies")
    if "n" in b:
        num(b, "n")  # accepted and ignored: the server numbers comments
    c = {
        "id": ident(b.get("id"), "id"),
        "screen": text(dflt(b, "screen", "main"), "screen", MAX["screen"]),
        "version": text(dflt(b, "version", "v1"), "version", MAX["version"]),
        "appVersion": app_version(dflt(b, "appVersion", "")),
        "path": text(dflt(b, "path", ""), "path", MAX["path"], False),
        "fx": num(b, "fx"), "fy": num(b, "fy"), "pageX": num(b, "pageX"), "pageY": num(b, "pageY"),
        "where": text(dflt(b, "where", ""), "where", MAX["where"], False),
        "text": text(b.get("text"), "text", MAX["text"]),
        "author": text(b.get("author"), "author", MAX["author"]),
        "createdAt": when(b),
        "resolved": b.get("resolved") is True,
        "replies": [make_reply(r) for r in (reps or [])],
        **({"reactions": rx} if (rx := reactions(b.get("reactions"))) else {}),
    }
    if any(x["id"] == c["id"] for x in lst):
        raise Fail(409, "comment exists")
    c["n"] = hw["|".join(group(c))] = next_n(lst, c, hw)
    lst.append(c)


def target(lst, b):  # only the author can change or delete text. Authors are typed names: this stops mistakes, not attackers
    c = find(lst, ident(b.get("id"), "id"))
    if "replyId" not in b:
        return c, c
    for r in c["replies"]:
        if r["id"] == b["replyId"]:
            return c, r
    raise Fail(404, "reply not found")


def edit(lst, b):
    exact(b.keys(), PATCH_FIELDS)
    c, item = target(lst, b)
    if "react" in b:
        if "text" in b or "resolved" in b:
            raise Fail(400, "react cannot go with text or resolved")
        return react(item, b)
    if "text" not in b and "resolved" not in b:
        raise Fail(400, "nothing to change")
    if "text" in b:
        if item["author"] != text(b.get("author"), "author", MAX["author"]):
            raise Fail(403, "only the author can edit")
        item["text"] = text(b["text"], "text", MAX["text"])
    if "resolved" in b:
        if not isinstance(b["resolved"], bool) or item is not c:
            raise Fail(400, "resolved must be a boolean on a comment")
        c["resolved"] = b["resolved"]


def remove(lst, q):
    c, item = target(lst, q)
    if item["author"] != q.get("author", ""):
        raise Fail(403, "only the author can delete")
    if item is c:
        lst.remove(c)
    else:
        c["replies"].remove(item)


class Store:
    # ponytail: one global lock and one JSON file. Fine for a few reviewers and 500 comments per project.
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                sys.exit(f"{self.path}: expected a JSON object of project to comment list")

    def persist(self, data):  # atomic: write a temp file in the same folder, then rename
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def handle(self, method, query, raw):
        try:
            return self._handle(method, query, raw)
        except Fail as e:
            return e.status, {"error": str(e)}
        except OSError:
            return 500, {"error": "server error"}

    def _handle(self, method, query, raw):
        if method not in ("GET", "POST", "PATCH", "DELETE"):
            raise Fail(405, "method not allowed")
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        q = dict(pairs)
        body = None
        if method in ("POST", "PATCH"):
            if len(raw) > MAX["body"]:
                raise Fail(413, "payload too large")
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):  # RecursionError: JSON nested too deep
                raise Fail(400, "invalid JSON") from None
            if not isinstance(body, dict):
                raise Fail(400, "body must be an object")
        else:
            exact([k for k, _ in pairs], ["project"] if method == "GET" else DELETE_PARAMS)
        project = body.get("project") if body is not None else q.get("project")
        if not isinstance(project, str) or not PROJECT.fullmatch(project):
            raise Fail(400, "invalid project")
        with self.lock:
            lst = copy.deepcopy(self.data.get(project, []))
            for c in lst:
                c.setdefault("appVersion", "")  # comments from before this field existed
            hw = dict(self.data.get("n:" + project, {}))  # "n:" cannot be a project name
            backfill(lst, hw)
            if method == "GET":
                return 200, {"comments": lst}
            status = 200
            if method == "POST":
                create(lst, body, hw)
                status = 201
            elif method == "PATCH":
                edit(lst, body)
            else:
                remove(lst, q)
            data = dict(self.data, **{project: lst, "n:" + project: hw})
            self.persist(data)
            self.data = data
            return status, {"ok": True}


# ---- Upstreams and auto-login ----
class Upstream:
    def __init__(self, url):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "http" or not u.hostname or u.path not in ("", "/") or u.query or u.username:
            sys.exit(f"upstream must look like http://127.0.0.1:3000 (http only, no path): {url}")
        self.host, self.port = u.hostname, u.port or 80
        self.netloc = u.netloc
        self.origin = f"http://{u.netloc}"


class Router:
    def __init__(self, routes):
        self.routes = {}
        for r in routes:
            prefix, sep, url = r.partition("=")
            if not sep or not prefix.startswith("/"):
                sys.exit(f"--route must look like /prefix=http://host:port, got: {prefix}")
            prefix = prefix.rstrip("/") or "/"
            if prefix == PREFIX or prefix.startswith(PREFIX + "/"):
                sys.exit(f"{PREFIX} is reserved for the review pages")
            self.routes[prefix] = Upstream(url)
        if "/" not in self.routes:
            sys.exit("a default route is required: --route /=http://127.0.0.1:3000")

    def pick(self, path):  # longest prefix wins. The prefix must end at a path segment
        for prefix in sorted(self.routes, key=len, reverse=True):
            if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
                return self.routes[prefix]


def resolve_env(node, missing):  # strings that start with $ come from the environment
    if isinstance(node, str) and node.startswith("$"):
        v = os.environ.get(node[1:], "")
        if not v:
            missing.append(node[1:])
        return v
    if isinstance(node, dict):
        return {k: resolve_env(v, missing) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_env(v, missing) for v in node]
    return node


class Session:
    """Logs in to the app on the server. The credentials stay in memory. They are never written or printed."""

    def __init__(self, raw, router, label):  # raw: the session object of review.json. review_config.validate has refused literal login values
        missing = []
        cfg = resolve_env(raw, missing)
        if missing:
            sys.exit(f"{label} needs these environment variables: " + ", ".join(sorted(set(missing))))
        try:
            self.login = cfg["login"]
            self.token_field = cfg["tokenField"]
            self.store = cfg["store"]
            assert self.login["path"].startswith("/") and isinstance(self.login["fields"], dict)
            self.login.setdefault("method", "POST")
            self.login.setdefault("route", "/")
            assert self.login["encoding"] in ("form", "json") and isinstance(self.token_field, str)
            assert isinstance(self.store, list) and all(
                s["type"] in ("localStorage", "sessionStorage", "cookie") and isinstance(s["key"], str) and isinstance(s["value"], str) for s in self.store)
        except (KeyError, TypeError, AssertionError):
            sys.exit(f"{label}: expected login{{route,method,path,encoding,fields}}, tokenField and store[{{type,key,value}}]")
        self.router = router
        self.lock = threading.Lock()
        self.cache = None

    def clear(self):  # after a reset the app has new data, so the next call logs in again
        with self.lock:
            self.cache = None

    def get(self):
        with self.lock:
            if self.cache and self.cache[0] > time.time():
                return self.cache[1]
            payload, ttl = self.do_login()
            self.cache = (time.time() + ttl, payload)
            return payload

    def do_login(self):
        lg, up = self.login, self.router.pick(self.login["route"])
        if lg["encoding"] == "form":
            body, ctype = urllib.parse.urlencode(lg["fields"]).encode(), "application/x-www-form-urlencoded"
        else:
            body, ctype = json.dumps(lg["fields"]).encode(), "application/json"
        conn = http.client.HTTPConnection(up.host, up.port, timeout=15)
        try:
            conn.request(lg["method"], lg["path"], body=body, headers={"Content-Type": ctype, "Accept": "application/json"})
            resp = conn.getresponse()
            data = resp.read(1 << 20)
        except (OSError, http.client.HTTPException) as e:
            raise Fail(502, "login failed") from e
        finally:
            conn.close()
        if not 200 <= resp.status < 300:
            print(f"review-proxy: auto-login answered HTTP {resp.status}", file=sys.stderr)
            raise Fail(502, "login failed")
        try:
            j = json.loads(data)
            token = j
            for part in self.token_field.split("."):
                token = token[part]
            assert isinstance(token, str) and token
        except (ValueError, KeyError, TypeError, AssertionError):
            print("review-proxy: auto-login answer has no token", file=sys.stderr)
            raise Fail(502, "login failed") from None
        ttl = SESSION_TTL
        if isinstance(j.get("expires_in"), int) and j["expires_in"] > 0:
            ttl = min(ttl, j["expires_in"])
        store = [{"type": s["type"], "key": s["key"].replace("{token}", token), "value": s["value"].replace("{token}", token)} for s in self.store]
        return {"store": store}, ttl


# ---- Config (review.json), patterns, mocks, app version ----
WRITE = ("POST", "PUT", "PATCH", "DELETE")
RESET_GAP = 30  # seconds between two accepted resets
PAUSE_SECONDS = 30
PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
APP_VERSION_VALUE = re.compile(r"[A-Za-z0-9._+-]{1,40}")
CONFIG_BLOCK = re.compile(r'(<script\b[^>]*\bid="cmt-config"[^>]*>)(.*?)(</script>)', re.S)
TTL = re.compile(r"([1-9][0-9]*)([smhd])")


def page(title, text):  # small inline pages: no host name, no path, no stack trace
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="color-scheme" content="light dark"><title>{title}</title></head>'
            f'<body style="font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 16px"><h1>{title}</h1><p>{text}</p></body></html>').encode()


PAGE_ENDED = page("Review ended", "This review has ended. Ask the sender for a new link if you still need access.")
PAGE_PAUSED = page("Review paused", "Review paused, try again later. The app is not answering right now.")


def segments(path):  # one trailing slash is ignored. Each segment is percent-decoded
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return [urllib.parse.unquote(s) for s in path.split("/")[1:]]


class Pattern:
    """A path with {name} segments. Each {name} matches exactly one non-empty segment. Other segments are literal."""

    def __init__(self, path, label):
        if not isinstance(path, str) or not path.startswith("/"):
            sys.exit(f"{label}: path must be a string that starts with /")
        self.parts, seen = [], []
        for seg in (path[:-1] if len(path) > 1 and path.endswith("/") else path).split("/")[1:]:
            m = PARAM.fullmatch(seg)
            if m and m[1] in seen:
                sys.exit(f"{label}: parameter {{{m[1]}}} is used twice")
            if m:
                seen.append(m[1])
            self.parts.append((m[1], None) if m else (None, urllib.parse.unquote(seg)))
        self.names = seen
        self.literals = [lit for name, lit in self.parts if name is None]

    def match(self, segs):
        if len(segs) != len(self.parts):
            return None
        out = {}
        for (name, lit), seg in zip(self.parts, segs):
            if name is not None:
                if not seg or seg in (".", "..") or "/" in seg or "\\" in seg or "\0" in seg:  # the proxy forwards the raw path, so a dot segment could reach another route
                    return None
                out[name] = seg
            elif lit != seg:
                return None
        return out


def status_ok(v):
    return isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 599


class Mock:
    def __init__(self, raw, idx, label):
        if not isinstance(raw, dict):
            sys.exit(f"{label}: a mock rule must be an object")
        m = raw.get("method", "GET")
        if not isinstance(m, str) or not re.fullmatch(r"[A-Za-z]+|\*", m):
            sys.exit(f"{label}: method must be an HTTP method or *")
        self.method = m.upper()
        self.pattern = Pattern(raw.get("path"), label)
        if self.pattern.parts and self.pattern.parts[0] == (None, PREFIX[1:]):
            sys.exit(f"{label}: a mock rule cannot match a path under {PREFIX}")
        self.status = raw.get("status", 200)
        if not status_ok(self.status):
            sys.exit(f"{label}: status must be an integer from 100 to 599")
        self.has_json, self.json = "json" in raw, raw.get("json")
        self.by, self.table = raw.get("by"), None
        if self.by is not None and self.by not in self.pattern.names:
            sys.exit(f"{label}: by must name a {{param}} of path")
        if "table" in raw:
            t = raw["table"]
            if not isinstance(t, dict) or self.by is None:
                sys.exit(f"{label}: table must be an object and needs by")
            self.table = {}
            for k, e in t.items():
                if not isinstance(e, dict) or "json" not in e or not status_ok(e.get("status", 200)):
                    sys.exit(f"{label}: table entry {k!r} must be an object with json and an optional status")
                self.table[k] = (e.get("status", 200), e["json"])
        if not self.has_json and self.table is None:
            sys.exit(f"{label}: a mock rule needs json or table")
        # most specific first: more literal segments, more literal characters, an exact method over *, then list order
        self.key = (-len(self.pattern.literals), -sum(map(len, self.pattern.literals)), 1 if self.method == "*" else 0, idx)

    def answer(self, method, segs):
        if self.method not in ("*", method) and not (method == "HEAD" and self.method == "GET"):
            return None
        params = self.pattern.match(segs)
        if params is None:
            return None
        if self.table is not None and params[self.by] in self.table:
            return self.table[params[self.by]]
        return (self.status, self.json) if self.has_json else None


def pick_mock(mocks, method, segs):  # mocks are sorted by specificity
    for m in mocks:
        ans = m.answer(method, segs)
        if ans is not None:
            return ans


def run_group(argv, cwd, env, timeout, out):
    """Run argv without a shell in its own process group. Returns (exit code or None on timeout, stdout bytes)."""
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.DEVNULL if out is subprocess.PIPE else subprocess.STDOUT, start_new_session=True)
    try:
        data, _ = p.communicate(timeout=timeout)
        return p.returncode, data or b""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return None, b""


SECRET_ENV = {"REVIEW_TOKEN"}  # variables that reset and appVersion commands must not inherit: the token and the login secrets (set in main)


def child_env():
    return {k: v for k, v in os.environ.items() if k not in SECRET_ENV}


class AppVersion:
    def __init__(self, spec, review_dir, label):
        self.literal, self.argv, self.dir = None, None, review_dir
        self.lock, self.cache = threading.Lock(), None
        if spec is None:
            return
        if isinstance(spec, str):
            if not APP_VERSION_VALUE.fullmatch(spec):
                sys.exit(f"{label}: appVersion must have 1 to 40 characters from A-Z a-z 0-9 . _ + -")
            self.literal = spec
            return
        cmd = spec.get("command") if isinstance(spec, dict) else None
        try:
            argv = shlex.split(cmd) if isinstance(cmd, str) else cmd
        except ValueError:
            argv = None
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            sys.exit(f"{label}: appVersion must be a string or an object with a command (an argv list, or a string)")
        self.argv = argv

    def get(self):  # the first stdout line, checked and cached for 10 seconds. None on any failure
        if self.literal or not self.argv:
            return self.literal
        with self.lock:
            if self.cache and self.cache[0] > time.monotonic():
                return self.cache[1]
            value = None
            try:
                code, out = run_group(self.argv, self.dir, child_env(), 5, subprocess.PIPE)
                line = out.decode("utf-8", "replace").strip().splitlines()[0].strip() if out.strip() else ""
                if code == 0 and APP_VERSION_VALUE.fullmatch(line):
                    value = line
            except OSError:
                pass
            if value is None:
                print("review-proxy: the appVersion command failed or printed no valid version", file=sys.stderr)
            self.cache = (time.monotonic() + 10, value)
            return value


def login_vars(sess):  # names of the $VARIABLES in session.login.fields
    lg = sess.get("login") if isinstance(sess, dict) else None
    fields = lg.get("fields") if isinstance(lg, dict) else None
    names = set()
    for v in (fields.values() if isinstance(fields, dict) else []):
        names |= set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", v)) if isinstance(v, str) else set()
    return names


def parse_time(v, label):  # ISO 8601 with Z or an offset. No zone means UTC. Returns epoch seconds
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except (AttributeError, ValueError, OverflowError):
        sys.exit(f"{label}: not an ISO 8601 date and time")
    return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d).timestamp()


def iso_utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Config:
    """review.json, checked by review_config.validate (the same function that review.py uses). This class builds the runtime objects:
    path patterns, mock rules, the app version and the reset command. Their constructors check what the shared validator cannot."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.dir = self.path.parent
        label = self.label = self.path.name
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            sys.exit(f"--config: {e}")
        try:
            c = review_config.validate(raw)
        except review_config.ConfigError as e:
            print(f"{label}: {e}", file=sys.stderr)
            sys.exit(e.code)  # 2 for a bad file, 3 for a security refusal, as in review.py
        self.project, self.title, self.version, self.note = c["project"], c["title"], c["reviewVersion"], c["reviewNote"]
        self.public_host = c["publicHost"]
        self.port, self.idle, self.upstream_timeout, self.comments_file = (c["proxy"][k] for k in ("port", "idleTimeout", "upstreamTimeoutSeconds", "commentsFile"))
        ports = {a["name"]: a["port"] for a in c["apps"]}
        self.routes = [f"{prefix}={'http://127.0.0.1:%d' % ports[t] if t in ports else t}" for prefix, t in c["routes"].items()]
        self.session = c["session"]
        envs = [c["proxy"]["envFile"]] + [a["envFile"] for a in c["apps"]]
        self.env_files = {(self.dir / f).resolve() for f in envs if f}  # the static handler must never serve these
        self.read_only = c["readOnly"]
        self.allow_write = [(w["method"], Pattern(w["path"], f"{label}: allowWrite[{i}]")) for i, w in enumerate(c["allowWrite"])]
        rules = [(r, f"{label}: mocks[{i}]") for i, r in enumerate(c["mocks"])]
        self.mocks_file = (self.dir / c["mocksFile"]).resolve() if c["mocksFile"] else None
        if self.mocks_file:
            try:
                filed = json.loads(self.mocks_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                sys.exit(f"{label}: mocksFile: {e}")
            if not isinstance(filed, list):
                sys.exit(f"{label}: mocksFile must hold a JSON array of rules")
            rules += [(r, f"{c['mocksFile']}[{i}]") for i, r in enumerate(filed)]
        self.mocks = sorted((Mock(r, i, lbl) for i, (r, lbl) in enumerate(rules)), key=lambda m: m.key)
        self.expires_at, self.ttl = c["expiresAt"], c["ttl"]
        self.app_version = AppVersion(c["appVersion"], self.dir, label)
        rs = c["reset"]
        self.reset = rs and {"argv": rs["command"], "cwd": (self.dir / rs.get("cwd", ".")).resolve(), "timeout": rs.get("timeoutSeconds", 120)}
        self.screens = c["frame"]["screens"]
        self.start_mode = c["startMode"]

    def wrapper(self):  # the wrapper page: the template with this review's cmt-config
        tpl = Path(__file__).resolve().parent.parent / "assets" / "template" / "review-frame.html"
        try:
            html = tpl.read_text(encoding="utf-8")
        except OSError:
            sys.exit(f"wrapper template not found: {tpl}")
        for name in LAYER_FILES:
            if not (LAYER_DIR / name).is_file():
                sys.exit(f"comment layer not found: {LAYER_DIR / name}. live-app-review needs the skill mockup-review next to it")
        cfg = {"project": self.project, "title": self.title, "version": self.version, "reviewNote": self.note,
               "apiBase": PREFIX + "/api/comments", "startMode": self.start_mode, "session": bool(self.session), "frame": {"screens": self.screens}}
        blob = json.dumps(cfg, indent=2).replace("<", "\\u003c")  # a "</script>" in a note cannot end the block
        html, n = CONFIG_BLOCK.subn(lambda m: m.group(1) + "\n" + blob + "\n" + m.group(3), html, count=1)
        if n != 1:
            sys.exit(f"{tpl}: no cmt-config block")
        return html.encode("utf-8")

    def expiry(self):  # ttl counts from now: the start of this process
        if self.expires_at is not None:
            return parse_time(self.expires_at, f"{self.label}: expiresAt")
        if self.ttl is not None:
            m = TTL.fullmatch(self.ttl)
            return time.time() + int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]
        return None


# ---- Frame headers ----
def frame_fix(name, v):  # let the wrapper page frame the app: X-Frame-Options DENY becomes SAMEORIGIN. Nothing else changes
    return "SAMEORIGIN" if name.lower() == "x-frame-options" and v.strip().upper() == "DENY" else v


def relay(a, b, expires_at=None):  # copy bytes both ways until one side closes or the review ends. Used for WebSocket
    socks = [a, b]
    idle = 0
    try:
        while True:
            ready, _, _ = select.select(socks, [], [], 5)
            if expires_at is not None and time.time() >= expires_at:
                return
            if not ready:
                idle += 5
                if idle >= 600:
                    return
                continue
            idle = 0
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        print("review-proxy: connection error", file=sys.stderr)  # no details: they could hold headers


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 30  # seconds without a byte from the client. Closes slow or silent connections, also before the token gate
    protocol_version = "HTTP/1.1"
    server_version = "review-proxy"
    sys_version = ""

    def log_message(self, *args):  # request lines can hold the token, so nothing is logged
        pass

    def version_string(self):
        return self.server_version

    def end_headers(self):
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        super().end_headers()

    # -- small helpers --
    def reply(self, status, body=b"", ctype="text/html; charset=utf-8", extra=()):
        if status in (204, 304) or status < 200:
            body = b""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def reply_json(self, status, obj, extra=()):
        self.reply(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json; charset=utf-8", extra)

    def skip_body(self):  # read a small request body that the proxy does not use. Keep the connection when the body is read
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 65536 or n < 0:
            self.close_connection = True
        elif n:
            self.rfile.read(n)

    def paused(self, html):  # the app did not answer before the response headers
        self.close_connection = True
        extra = [("Retry-After", str(PAUSE_SECONDS))]
        if html:
            return self.reply(503, PAGE_PAUSED, extra=extra)
        self.reply_json(503, {"detail": "review paused", "retryAfterSeconds": PAUSE_SECONDS}, extra)

    def wants_html(self):
        return "text/html" in (self.headers.get("Accept") or "").lower()

    def drain(self):  # read a small unsent body, so closing the socket does not reset the connection before the client reads the answer
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= 65536:
            self.rfile.read(n)
        self.close_connection = True

    def bad_request(self):
        self.close_connection = True
        self.reply(400, b"Bad request")

    def forbidden(self):
        self.drain()
        self.reply(403, b"<!doctype html><title>Forbidden</title><p>This review link is missing or no longer valid. Ask for a new link.</p>")

    def read_body(self, limit=None):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or (limit is not None and n > limit):
            raise ValueError
        return self.rfile.read(n)

    # -- gate --
    def cookie_ok(self):
        want = self.server.cookie_value.encode()
        for line in self.headers.get_all("Cookie") or []:
            for part in line.split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE and hmac.compare_digest(v.encode(), want):
                    return True
        return False

    def handle_any(self):
        srv = self.server
        if srv.expires_at is not None and time.time() >= srv.expires_at:  # before everything else, also before the token gate
            self.drain()
            return self.reply(410, PAGE_ENDED)
        if not self.path.startswith("/"):  # absolute-form request lines ("GET http://other/") would make an open proxy
            self.drain()
            return self.reply(400, b"Bad request")
        path, _, query = self.path.partition("?")
        tokens = [v for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True) if k == "t"]
        if any(hmac.compare_digest(v.encode(), self.server.token.encode()) for v in tokens):
            rest = "&".join(p for p in query.split("&") if urllib.parse.unquote_plus(p.split("=", 1)[0]) != "t")  # same decoding as the token test
            cookie = f"{COOKIE}={self.server.cookie_value}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={COOKIE_AGE}"
            return self.reply(302, extra=[("Location", "/" + path.lstrip("/\\") + ("?" + rest if rest else "")), ("Set-Cookie", cookie)])
        if not self.cookie_ok():
            return self.forbidden()
        if path == PREFIX:
            return self.reply(302, extra=[("Location", PREFIX + "/" + ("?" + query if query else ""))])
        if path.startswith(PREFIX + "/"):
            sub = path[len(PREFIX) + 1:]
            if sub == "api/comments":
                return self.comments_api(query)
            if sub == "session":
                return self.session()
            if sub == "meta":
                return self.meta()
            if sub == "reset":
                return self.reset()
            return self.static(sub)
        segs = segments(path)
        hit = pick_mock(srv.mocks, self.command, segs)
        if hit is not None:  # a mock beats the read-only check and the routes. The app never sees the request
            self.skip_body()
            return self.reply_json(hit[0], hit[1])
        override = any(k.lower() in OVERRIDE for k in self.headers) or any(k.lower() == "_method" for k, _ in urllib.parse.parse_qsl(query, keep_blank_values=True))
        if srv.read_only and (override or (self.command in WRITE and not any(m == self.command and pat.match(segs) is not None for m, pat in srv.allow_write))):
            self.skip_body()
            return self.reply(403, b'{"detail":"read-only review"}', "application/json; charset=utf-8", [("X-Review-Read-Only", "1")])
        return self.forward(path)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any

    # -- /__review/ --
    def static(self, sub):
        if self.command not in ("GET", "HEAD"):
            return self.reply(405, b"Method not allowed")
        rel = urllib.parse.unquote(sub)
        if rel == "" or rel.endswith("/"):
            rel += "index.html"
        if sub in LAYER_FILES:
            return self.reply(200, (LAYER_DIR / sub).read_bytes(), LAYER_FILES[sub])
        if posixpath.normpath("/" + rel.lstrip("/")) == "/index.html":  # the wrapper always comes from the template
            return self.reply(200, self.server.wrapper)
        root = self.server.review_dir
        f = None
        try:
            if "\0" not in rel and "\\" not in rel:
                f = (root / rel).resolve()  # follows symlinks and removes ".."
        except (OSError, ValueError):
            pass
        inside = f is not None and root in f.parents and not any(p.startswith(".") for p in f.relative_to(root).parts)
        if not inside or not f.is_file() or f in self.server.hidden:
            return self.reply(404, b"Not found")
        self.reply(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")

    def meta(self):
        if self.command != "GET":
            return self.reply_json(405, {"error": "method not allowed"})
        srv = self.server
        self.reply_json(200, {"version": srv.app_version.get(), "expiresAt": iso_utc(srv.expires_at) if srv.expires_at is not None else None,
                              "readOnly": srv.read_only, "resetEnabled": srv.reset_cfg is not None})

    def reset(self):
        srv = self.server
        if srv.reset_cfg is None:
            return self.reply_json(404, {"error": "reset disabled"})
        if self.command != "POST":
            return self.reply_json(405, {"error": "method not allowed"}, [("Allow", "POST")])
        self.skip_body()  # the body and the query string never reach the command
        if self.headers.get("X-Review-Reset") != "1":
            return self.reply_json(403, {"error": "missing csrf header"})
        origin = self.headers.get("Origin")
        if origin is not None and urllib.parse.urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return self.reply_json(403, {"error": "bad origin"})
        now = time.monotonic()
        with srv.reset_lock:  # one run per 30 seconds for the whole proxy. A run in progress counts
            wait = srv.reset_last + RESET_GAP - now if srv.reset_last is not None else 0
            if srv.reset_busy or wait > 0:
                n = max(1, math.ceil(wait))
                return self.reply_json(429, {"error": "rate limited", "retryAfterSeconds": n}, [("Retry-After", str(n))])
            srv.reset_busy, srv.reset_last = True, now
        try:
            status, body = self.run_reset(now)
        finally:
            with srv.reset_lock:
                srv.reset_busy = False
        if status == 200 and srv.session:
            srv.session.clear()
        self.reply_json(status, body)

    def run_reset(self, t0):
        srv, cfg = self.server, self.server.reset_cfg
        ms = lambda: int((time.monotonic() - t0) * 1000)
        try:
            log = srv.runtime_dir / "reset.log"
            log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), 0o600)
                code, _ = run_group(cfg["argv"], cfg["cwd"], child_env(), cfg["timeout"], f)
        except (OSError, ValueError):
            return 500, {"ok": False, "error": "reset could not start", "durationMs": ms()}
        if code is None:
            return 504, {"ok": False, "error": "reset timed out", "durationMs": ms()}
        if code != 0:
            return 500, {"ok": False, "error": f"reset failed (exit {code})", "durationMs": ms()}
        return 200, {"ok": True, "durationMs": ms()}

    def comments_api(self, query):
        try:
            if self.headers.get("Transfer-Encoding"):
                raise OverflowError
            raw = self.read_body(MAX["body"])
        except OverflowError:
            self.close_connection = True
            return self.reply_json(411, {"error": "length required"})
        except ValueError:
            self.close_connection = True
            return self.reply_json(413, {"error": "payload too large"})
        status, obj = self.server.store.handle(self.command, query, raw)
        self.reply_json(status, obj)

    def session(self):
        sess = self.server.session
        if self.command != "GET" or sess is None:
            return self.reply_json(404, {"error": "not found"})
        try:
            self.reply_json(200, sess.get())
        except Fail as e:
            self.reply_json(e.status, {"error": str(e)})

    # -- reverse proxy --
    def forward(self, path):
        up = self.server.router.pick(path)
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self.tunnel(up)
        conn_tokens = {t.strip().lower() for t in (self.headers.get("Connection") or "").split(",")}
        headers = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in DROP_IN or lk in conn_tokens:
                continue
            if lk == "cookie":
                v = "; ".join(p.strip() for p in v.split(";") if p.strip().split("=", 1)[0] != COOKIE)
                if not v:
                    continue
            headers[k] = headers[k] + (", " if lk != "cookie" else "; ") + v if k in headers else v
        headers.update({"Host": up.netloc, "Connection": "close", "X-Forwarded-Proto": self.server.proto,
                        "X-Forwarded-Host": self.headers.get("Host", ""), "X-Forwarded-For": self.client_address[0]})
        body, chunked = None, False
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            headers["Transfer-Encoding"], body, chunked = "chunked", self.chunks(), True
        else:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0:
                    raise ValueError
            except ValueError:
                return self.bad_request()
            if n:
                headers["Content-Length"], body = str(n), self.sized(n)
            elif self.command in ("POST", "PUT", "PATCH"):
                headers["Content-Length"] = "0"
        conn = http.client.HTTPConnection(up.host, up.port, timeout=self.server.upstream_timeout)
        try:
            try:
                conn.request(self.command, self.path, body=body, headers=headers, encode_chunked=chunked)
                resp = conn.getresponse()
            except ValueError:  # a malformed chunk size in the request body
                return self.bad_request()
            except (OSError, http.client.HTTPException):
                return self.paused(self.wants_html())
            self.pass_response(resp, up)
        finally:
            conn.close()

    def sized(self, n):  # stream a request body of known length
        while n > 0:
            data = self.rfile.read(min(n, 65536))
            if not data:
                return
            n -= len(data)
            yield data

    def chunks(self):  # decode a chunked request body. The upstream call encodes it again
        while True:
            size = int(self.rfile.readline(1024).split(b";")[0].strip() or b"0", 16)
            if size < 0:
                raise ValueError
            if size == 0:
                while self.rfile.readline(65536) not in (b"\r\n", b"\n", b""):
                    pass
                return
            yield self.rfile.read(size)
            self.rfile.readline(8)

    def pass_response(self, resp, up):
        conn_tokens = {t.strip().lower() for t in (resp.getheader("Connection") or "").split(",")}
        self.send_response(resp.status, resp.reason)
        sized = False
        for k, v in resp.msg.items():
            lk = k.lower()
            if lk in DROP_OUT or lk in conn_tokens:
                continue
            if lk == "location" and v.startswith(up.origin):
                v = v[len(up.origin):] or "/"
            sized = sized or lk == "content-length"
            self.send_header(k, frame_fix(k, v))
        bodyless = self.command == "HEAD" or resp.status in (204, 304) or resp.status < 200
        stream_chunked = not sized and not bodyless and self.request_version != "HTTP/1.0"
        if stream_chunked:
            self.send_header("Transfer-Encoding", "chunked")
        elif not sized and not bodyless:
            self.close_connection = True
        self.end_headers()
        if bodyless:
            return
        try:
            while True:
                data = resp.read1(65536)
                if not data:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data) if stream_chunked else data)
            if stream_chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (OSError, http.client.HTTPException):
            self.close_connection = True

    def tunnel(self, up):  # WebSocket: send the handshake upstream, then copy bytes both ways
        self.close_connection = True
        clean = lambda v: re.sub(r"[\r\n]+", " ", v)
        lines = [f"{self.command} {self.path} HTTP/1.1", f"Host: {up.netloc}"]
        for k, v in self.headers.items():
            if k.lower() in OVERRIDE or k.lower() in ("host", "cookie", "forwarded", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"):
                continue
            lines.append(f"{k}: {clean(v)}")
        cookie = "; ".join(p.strip() for line in self.headers.get_all("Cookie") or [] for p in line.split(";") if p.strip().split("=", 1)[0] != COOKIE)
        if cookie:
            lines.append("Cookie: " + clean(cookie))
        lines += [f"X-Forwarded-Proto: {self.server.proto}", f"X-Forwarded-Host: {clean(self.headers.get('Host', ''))}", f"X-Forwarded-For: {self.client_address[0]}"]
        try:
            s = socket.create_connection((up.host, up.port), timeout=10)
        except OSError:
            return self.paused(False)
        try:
            s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))
            head = b""  # read the answer first. Only an HTTP 101 may open the byte tunnel. Any other answer is a normal HTTP response, so a keep-alive socket cannot carry more requests
            try:
                while b"\r\n\r\n" not in head and len(head) < 65536:
                    data = s.recv(65536)
                    if not data:
                        break
                    head += data
            except OSError:
                pass
            parts = head.split(b" ", 2)
            code = parts[1] if head.startswith(b"HTTP/1.") and len(parts) > 2 else b""
            if code != b"101":
                return self.reply(int(code), b"") if code.isdigit() and 100 <= int(code) <= 599 else self.paused(False)
            s.settimeout(None)
            self.connection.sendall(head)
            relay(self.connection, s, self.server.expires_at)
        except OSError:
            pass
        finally:
            s.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Token-gated review proxy for a running web app. review.json holds all settings.")
    ap.add_argument("--config", required=True, help="review.json. The review folder is the folder of this file")
    ap.add_argument("--port", type=int, help="runtime value from review.py. Default: proxy.port from the config")
    ap.add_argument("--public-host", help="public host name for the share link, for example my-pc.example.test. An empty value turns the host of the config off")
    ap.add_argument("--expires-at", metavar="ISO", help="end of the review, ISO 8601. It wins over expiresAt and ttl. After it every request gets 410")
    ap.add_argument("--check", action="store_true", help="read and check the config, print ok and exit. Nothing starts")
    a = ap.parse_args(argv)

    cfg = Config(a.config)
    if a.check:
        cfg.wrapper()  # the template and the layer files must exist
        print("ok")
        return
    review_dir = cfg.dir
    router = Router(cfg.routes)
    session = Session(cfg.session, router, f"{cfg.label} session") if cfg.session else None
    comments = (review_dir / cfg.comments_file).resolve()
    comments.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = os.environ.get("REVIEW_TOKEN") or secrets.token_urlsafe(24)
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{16,200}", token):
        sys.exit("REVIEW_TOKEN must have 16 to 200 characters from A-Z a-z 0-9 _ . ~ -")
    host = a.public_host if a.public_host is not None else cfg.public_host  # an empty flag turns the config host off
    if host:
        host = re.sub(r"^https?://", "", host).rstrip("/")
        if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", host):
            sys.exit("--public-host must be a host name")

    port = a.port if a.port is not None else cfg.port
    Handler.timeout = cfg.idle
    srv = Server(("127.0.0.1", port), Handler)
    srv.token, srv.router, srv.session, srv.review_dir, srv.comments_file = token, router, session, review_dir, comments
    srv.cookie_value = hmac.new(token.encode(), b"review-session-v1", hashlib.sha256).hexdigest()
    srv.proto = "https" if host else "http"
    srv.store = Store(comments)
    srv.runtime_dir = review_dir / ".review"
    srv.upstream_timeout = cfg.upstream_timeout
    srv.read_only = cfg.read_only
    srv.mocks, srv.allow_write = cfg.mocks, cfg.allow_write
    srv.app_version = cfg.app_version
    srv.reset_cfg, srv.reset_lock, srv.reset_last, srv.reset_busy = cfg.reset, threading.Lock(), None, False
    srv.wrapper = cfg.wrapper()
    srv.hidden = {comments, cfg.path} | ({cfg.mocks_file} if cfg.mocks_file else set()) | cfg.env_files
    SECRET_ENV.update(login_vars(cfg.session))
    srv.expires_at = parse_time(a.expires_at, "--expires-at") if a.expires_at else cfg.expiry()
    link = f"https://{host}{PREFIX}/?t={token}" if host else f"http://127.0.0.1:{srv.server_address[1]}{PREFIX}/?t={token}"
    print(f"review-proxy: listening on 127.0.0.1:{srv.server_address[1]}, comments in {comments}", file=sys.stderr)
    print(link, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
