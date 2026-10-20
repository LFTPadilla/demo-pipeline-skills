"""The one validator for review.json. review.py and review-proxy.py both call validate().
Python 3.9+, standard library only. validate() returns a normalized copy, or raises ConfigError.
code 2 means a bad file. code 3 means a security refusal (a secret in the file, or a literal login value).
"""
import re
import shlex
import sys
from datetime import datetime, timezone

PREFIX = "/__review"
REQ = object()
SECRET_KEY = re.compile(r"pass|secret|token|key|credential|auth", re.I)
FUNNEL_PORTS = (443, 8443, 10000)
RESERVED = ("proxy", "watchdog", "funnel")  # component names that review.py uses itself


class ConfigError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def die(code, msg):
    raise ConfigError(code, msg)


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def _keys(o, allowed, where):
    if not isinstance(o, dict):
        die(2, f"{where} must be an object")
    extra = sorted(set(o) - set(allowed))
    if extra:
        die(2, f"{where}: unknown key {extra[0]}")


def _str(o, k, where, default=REQ, rx=None, lo=1, hi=10000):
    if k not in o:
        if default is REQ:
            die(2, f"{where}{k} is required")
        return default
    v = o[k]
    if not isinstance(v, str) or not lo <= len(v) <= hi or (rx and not re.fullmatch(rx, v)):
        die(2, f"{where}{k} must be a string of {lo} to {hi} characters" + (" in the right format" if rx else ""))
    return v


def _num(o, k, where, default, lo, hi, integer=True):
    v = o.get(k, default)
    ok = (type(v) is int) if integer else (type(v) in (int, float))
    if v is REQ or not ok or not lo <= v <= hi:
        die(2, f"{where}{k} must be {'an integer' if integer else 'a number'} from {lo} to {hi}")
    return v


def _bool(o, k, where, default):
    v = o.get(k, default)
    if not isinstance(v, bool):
        die(2, f"{where}{k} must be true or false")
    return v


def _argv(v, where):
    if not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v):
        die(2, f"{where} must be a list of strings with at least one item")
    return list(v)


def parse_time(v):
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _prefill(steps, where):
    """A prefill step types a value into an element ({selector, value, ...}) or clicks one ({click}). Steps run in order."""
    if not isinstance(steps, list) or len(steps) > 10:
        die(2, f"{where} must be a list of at most 10 steps")
    for i, st in enumerate(steps):
        w = f"{where}[{i}]"
        if isinstance(st, dict) and "click" in st:
            _keys(st, ["click", "waitForSelector", "optional"], w)
            _str(st, "click", w + ".", hi=200)
            _bool(st, "optional", w + ".", False)
        else:
            _keys(st, ["selector", "value", "submit", "delayMs", "waitForSelector", "keyEvents"], w)
            _bool(st, "keyEvents", w + ".", False)
            _str(st, "selector", w + ".", hi=200)
            _str(st, "value", w + ".", hi=200)
            if st.get("submit", "Enter") not in ("Enter", "Tab", "none"):
                die(2, f'{w}.submit must be "Enter", "Tab" or "none"')
            _num(st, "delayMs", w + ".", 0, 0, 10000)
        if "waitForSelector" in st:
            _str(st, "waitForSelector", w + ".", hi=200)


def validate(raw):
    """Return a normalized copy of review.json. Exit 2 for a bad file, exit 3 for a security refusal."""
    top = ["project", "title", "reviewVersion", "reviewNote", "publicHost", "startMode", "funnel", "proxy", "apps", "routes", "session", "readOnly",
           "allowWrite", "mocks", "mocksFile", "expiresAt", "ttl", "appVersion", "reset", "frame"]
    _keys(raw, top, "review.json")
    c = {"project": _str(raw, "project", "", rx=r"[a-z0-9][a-z0-9-]{0,63}")}
    c["title"] = _str(raw, "title", "", default=c["project"], hi=120)
    c["reviewVersion"] = _str(raw, "reviewVersion", "", default="v1", hi=20)
    c["reviewNote"] = _str(raw, "reviewNote", "", default="", lo=0, hi=300)
    c["startMode"] = raw.get("startMode", "comment")
    if c["startMode"] not in ("browse", "comment"):
        die(2, 'startMode must be "browse" or "comment"')
    ph = raw.get("publicHost")
    if ph is not None:
        ph = _str(raw, "publicHost", "", rx=r"[A-Za-z0-9.-]+")
    c["publicHost"] = ph
    f = raw.get("funnel", {})
    if isinstance(f, dict) and "mode" in f:
        die(2, "funnel.mode was removed. Delete it. review.py now turns Funnel on and off itself, and needs the Tailscale operator: run \"sudo tailscale set --operator=$USER\" once")
    _keys(f, ["enabled", "port"], "funnel")
    c["funnel"] = {"enabled": _bool(f, "enabled", "funnel.", False), "port": f.get("port", 443)}
    if c["funnel"]["port"] not in FUNNEL_PORTS or type(c["funnel"]["port"]) is not int:
        die(2, "funnel.port must be 443, 8443 or 10000")
    p = raw.get("proxy", {})
    _keys(p, ["port", "idleTimeout", "upstreamTimeoutSeconds", "commentsFile", "envFile"], "proxy")
    c["proxy"] = {"port": _num(p, "port", "proxy.", 8787, 1, 65535),
                  "idleTimeout": _num(p, "idleTimeout", "proxy.", 30, 0.001, 10**9, False),
                  "upstreamTimeoutSeconds": _num(p, "upstreamTimeoutSeconds", "proxy.", 60, 0.001, 10**9, False),
                  "commentsFile": _str(p, "commentsFile", "proxy.", default=".review/comments.json"),
                  "envFile": None if p.get("envFile") is None else _str(p, "envFile", "proxy.")}
    apps = raw.get("apps", [])
    if not isinstance(apps, list):
        die(2, "apps must be a list")
    c["apps"], seen, ports = [], set(), {c["proxy"]["port"]}
    for i, a in enumerate(apps):
        w = f"apps[{i}]"
        _keys(a, ["name", "command", "cwd", "envFile", "env", "port", "health"], w)
        name = _str(a, "name", w + ".", rx=r"[a-z][a-z0-9-]{0,31}")
        if name in RESERVED:
            die(2, f"{w}: the name {name} is reserved. Use another app name")
        if name in seen:
            die(2, f"{w}: duplicate app name {name}")
        seen.add(name)
        port = _num(a, "port", w + ".", REQ, 1, 65535)
        if port in ports:
            die(2, f"{w}: port {port} is used twice")
        ports.add(port)
        h = a.get("health", {})
        _keys(h, ["url", "timeoutSeconds"], w + ".health")
        env = a.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            die(2, f"{w}.env must be an object of strings")
        c["apps"].append({
            "name": name, "command": _argv(a["command"], w + ".command") if a.get("command") is not None else None,
            "cwd": _str(a, "cwd", w + ".", default="."), "envFile": None if a.get("envFile") is None else _str(a, "envFile", w + "."),
            "env": env, "port": port,
            "health": {"url": _str(h, "url", w + ".health.", default="http://127.0.0.1:{port}/", rx=r"http://.*"),
                       "timeoutSeconds": _num(h, "timeoutSeconds", w + ".health.", 60, 0.001, 10**6, False)}})
    routes = raw.get("routes")
    if not isinstance(routes, dict) or "/" not in routes:
        die(2, 'routes is required and needs the key "/"')
    for k, v in routes.items():
        if not k.startswith("/") or k == "/__review" or k.startswith("/__review/"):
            die(2, f"routes: bad path prefix {k}")
        if not (v in seen if isinstance(v, str) else False) and not (isinstance(v, str) and re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/?", v)):
            die(2, f"routes[{k}] must be an app name or http://127.0.0.1:<port>")
    c["routes"] = dict(routes)
    c["session"] = raw.get("session")
    if c["session"] is not None:
        s = c["session"]
        _keys(s, ["login", "tokenField", "store"], "session")
        _keys(s.get("login"), ["route", "method", "path", "encoding", "fields"], "session.login")
        if not isinstance(s["login"].get("fields"), dict):
            die(2, "session.login.fields must be an object")
        if not isinstance(s.get("store"), list) or not all(isinstance(x, dict) for x in s["store"]):
            die(2, "session.store must be a list of objects")
        for x in s["store"]:
            _keys(x, ["type", "key", "value"], "session.store item")
    c["readOnly"] = _bool(raw, "readOnly", "", True)
    c["allowWrite"] = raw.get("allowWrite", [])
    if not isinstance(c["allowWrite"], list):
        die(2, "allowWrite must be a list")
    for i, x in enumerate(c["allowWrite"]):
        _keys(x, ["method", "path"], f"allowWrite[{i}]")
        if x.get("method") not in ("POST", "PUT", "PATCH", "DELETE") or not isinstance(x.get("path"), str) or not x["path"].startswith("/"):
            die(2, f"allowWrite[{i}] needs a method (POST, PUT, PATCH or DELETE) and a path that starts with /")
    c["mocks"] = raw.get("mocks", [])
    if not isinstance(c["mocks"], list):
        die(2, "mocks must be a list")
    for i, m in enumerate(c["mocks"]):
        _keys(m, ["method", "path", "by", "table", "status", "json"], f"mocks[{i}]")
        if not isinstance(m.get("path"), str) or not m["path"].startswith("/") or m["path"] == PREFIX or m["path"].startswith(PREFIX + "/"):
            die(2, f"mocks[{i}].path must start with / and stay out of {PREFIX}")
        if "json" not in m and "table" not in m:
            die(2, f"mocks[{i}] needs json or table")
        if "status" in m:
            _num(m, "status", f"mocks[{i}].", 200, 100, 599)
    c["mocksFile"] = None if raw.get("mocksFile") is None else _str(raw, "mocksFile", "")
    c["expiresAt"], c["ttl"] = raw.get("expiresAt"), raw.get("ttl")
    if c["expiresAt"] is not None and c["ttl"] is not None:
        die(2, "Set expiresAt or ttl, not both")
    if c["expiresAt"] is not None and (not isinstance(c["expiresAt"], str) or parse_time(c["expiresAt"]) is None):
        die(2, "expiresAt must be an ISO 8601 time")
    if c["ttl"] is not None and (not isinstance(c["ttl"], str) or not re.fullmatch(r"[1-9][0-9]*[smhd]", c["ttl"])):
        die(2, "ttl must look like 7d, 36h, 30m or 90s")
    av = raw.get("appVersion")
    if isinstance(av, dict):
        _keys(av, ["command"], "appVersion")
        if not isinstance(av.get("command"), (str, list)) or not av["command"]:
            die(2, "appVersion.command must be a string or a list of strings")
    elif av is not None and (not isinstance(av, str) or not re.fullmatch(r"[A-Za-z0-9._+-]{1,40}", av)):
        die(2, "appVersion must be a short version string or an object with command")
    c["appVersion"] = av
    r = raw.get("reset")
    if r is not None:
        _keys(r, ["command", "cwd", "timeoutSeconds"], "reset")
        _argv(r.get("command"), "reset.command")
        _str(r, "cwd", "reset.", default=".")
        _num(r, "timeoutSeconds", "reset.", 120, 1, 3600)
    c["reset"] = r
    fr = raw.get("frame", {})
    _keys(fr, ["screens"], "frame")
    scr = fr.get("screens", [{"id": "home", "title": "Home", "src": "/"}])
    if not isinstance(scr, list) or not scr:
        die(2, "frame.screens must be a list with at least one screen")
    ids = set()
    for i, s in enumerate(scr):
        w = f"frame.screens[{i}]"
        _keys(s, ["id", "title", "src", "note", "highlight", "prefill"], w)
        sid = _str(s, "id", w + ".", rx=r"[a-z0-9][a-z0-9_-]{0,31}")
        if sid in ids:
            die(2, f"{w}: duplicate id {sid}")
        ids.add(sid)
        _str(s, "title", w + ".", default=sid, hi=40)
        src = _str(s, "src", w + ".", hi=500)
        if not src.startswith("/") or src.startswith("//"):
            die(2, f"{w}.src must start with a single /")
        _str(s, "note", w + ".", default="", lo=0, hi=300)
        _str(s, "highlight", w + ".", default="", lo=0, hi=200)
        _prefill(s.get("prefill", []), w + ".prefill")
    c["frame"] = {"screens": [dict(s, title=s.get("title", s["id"])) for s in scr]}
    argvs = [(f"app {a['name']}", a["command"]) for a in c["apps"] if a["command"]] + ([("reset.command", r["command"])] if r else [])
    if isinstance(av, dict):
        argvs.append(("appVersion.command", shlex.split(av["command"]) if isinstance(av["command"], str) else av["command"]))
    for where, argv in argvs:
        for x in argv:
            k, eq, v = x.partition("=") if isinstance(x, str) else ("", "", "")
            if eq and v and SECRET_KEY.search(k):
                eprint(f"warning: {where} has an argument that looks like a secret ({k}=...). Arguments are visible in ps. Use an env file instead")
    # Security refusals come after the structure checks.
    for a in c["apps"]:
        bad = sorted(k for k in a["env"] if SECRET_KEY.search(k))
        if bad:
            die(3, f"app {a['name']}: env key {', '.join(bad)} looks like a secret. Put it in the envFile (chmod 600), not in review.json")
    if c["session"]:
        lit = sorted(k for k, v in c["session"]["login"]["fields"].items() if not (isinstance(v, str) and re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", v)))
        if lit:
            die(3, "session.login.fields must hold $VARIABLE names. Literal values found for: " + ", ".join(lit))
    return c
