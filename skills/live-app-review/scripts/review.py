#!/usr/bin/env python3
"""Run a live-app review: start the apps and the proxy, turn Tailscale Funnel on, and stop it all again.
Python 3.9+, standard library only.

  review.py [-C DIR | --review-dir DIR] <command> [flags]      DIR holds review.json (default: current folder)
    up         [--funnel | --no-funnel] [--skip-preflight]
    down
    status     [--json]
    logs       [NAME] [-n LINES] [-f]
    preflight  [--funnel | --no-funnel] [--json]
    verify     [--local] [--json] [--screenshots-dir DIR]
    link

Exit codes: 0 ok, 1 a check failed, 2 usage or invalid review.json, 3 security refusal,
4 an app or the proxy is not healthy, 5 a tailscale command failed, 6 nothing is running.
Messages go to stderr. Tables, JSON and the share link go to stdout.
Runtime files live in <DIR>/.review/. The review token is never written to a file. It lives in the proxy environment and in the link that `up` and `link` print.
`up` uses REVIEW_TOKEN when it is set (16 to 200 characters from A-Z a-z 0-9 _ . ~ -). Otherwise it makes a new token.
review.py turns Funnel on and off itself. That needs the Tailscale operator: run "sudo tailscale set --operator=$USER" once.
"""
import argparse
import contextlib
import fcntl
import http.client
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from review_config import FUNNEL_PORTS, ConfigError, parse_time, validate  # noqa: E402  one validator for review.py and the proxy
RUN = ".review"
TICK = 0.5
SUDO_CASE = re.compile(r"access denied|permission denied|operator", re.I)
OPERATOR_FIX = 'run "sudo tailscale set --operator=$USER" once.'
TOKEN_RX = re.compile(r"[A-Za-z0-9_.~-]{16,200}")  # the same rule as review-proxy.py
WATCHDOG_RETRIES, WATCHDOG_RETRY_SECONDS = 6, 5
HAS_PROC = Path("/proc/self/stat").exists()
_children = []  # Popen objects that this process started. They are reaped in the wait loops.


class Stop(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg)
        self.code = code


def die(code, msg):
    raise Stop(code, msg)


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- config
def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def load_config(rd):
    f = rd / "review.json"
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(2, f"review.json not found in {rd}")
    except (OSError, UnicodeDecodeError) as e:
        die(2, f"cannot read {f}: {getattr(e, 'strerror', None) or type(e).__name__}")
    except ValueError as e:
        die(2, f"review.json is not valid JSON: {e}")
    try:
        return validate(raw)
    except ConfigError as e:
        die(e.code, str(e))


# ---------------------------------------------------------------- env files
def env_problem(path):
    """Return (exit code, message) when an env file must be refused, else None."""
    try:
        st = os.stat(path)
    except OSError as e:
        return 2, f"cannot read env file {path}: {e.strerror}"
    if not stat.S_ISREG(st.st_mode):
        return 3, f"env file {path} is not a regular file"
    if st.st_uid != os.getuid():
        return 3, f"env file {path} is not owned by the current user"
    if st.st_mode & 0o077:
        return 3, f"env file {path} is readable by other users. Run: chmod 600 {path}"
    return None


def parse_env_file(path):
    p = env_problem(path)
    if p:
        die(*p)
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        die(2, f"env file {path} is not UTF-8 text")
    out = {}
    for i, line in enumerate(lines, 1):
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        m = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", t)
        if not m:
            die(2, f"env file {path}: line {i} is not KEY=VALUE")
        k, v = m.groups()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def env_files(cfg, rd):
    fs = [cfg["proxy"]["envFile"]] + [a["envFile"] for a in cfg["apps"] if a["command"]]
    return [rd / f for f in fs if f]


def rel(rd, p):
    return (rd / p).resolve() if not os.path.isabs(p) else Path(p)


# ---------------------------------------------------------------- run folder, state, lock
def ensure_run(rd):
    d = rd / RUN
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    g = d / ".gitignore"
    if not g.exists():
        g.write_text("*\n", encoding="utf-8")
    return d


@contextlib.contextmanager
def lock(rd):
    d = ensure_run(rd)
    fd = os.open(d / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        die(1, "another review.py up or down is running for this folder")
    try:
        yield
    finally:
        os.close(fd)


def read_state(rd):
    try:
        return json.loads((rd / RUN / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_private(path, text):
    tmp = Path(str(path) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save_state(rd, st):
    write_private(rd / RUN / "state.json", json.dumps(st, indent=1) + "\n")


def read_token(rd):
    """The token is never stored in a file. REVIEW_TOKEN wins when it is set. Else read it from the environment of the running proxy (Linux)."""
    if os.environ.get("REVIEW_TOKEN"):
        return os.environ["REVIEW_TOKEN"]
    for c in (read_state(rd) or {}).get("components", []):
        if c.get("kind") == "proxy" and is_ours(c):
            try:
                for item in Path(f"/proc/{c['pid']}/environ").read_bytes().split(b"\0"):
                    if item.startswith(b"REVIEW_TOKEN="):
                        return item[len(b"REVIEW_TOKEN="):].decode() or None
            except OSError:
                pass
    return None


def share_link(st, token):
    f = st.get("funnel") or {}
    if (f.get("on") or f.get("mode") == "external") and st.get("publicHost"):   # mode external: a state from an older version. Still public, so the link is public
        port = "" if f.get("port") == 443 else f":{f['port']}"
        return f"https://{st['publicHost']}{port}/__review/?t={token}"
    return f"http://127.0.0.1:{st['proxyPort']}/__review/?t={token}"


# ---------------------------------------------------------------- processes
def proc_info(pid):
    """(state, pgrp, start ticks) from /proc/<pid>/stat, or None."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        rest = raw[raw.rindex(")") + 2:].split()
        return rest[0], int(rest[2]), int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def is_ours(c):
    """True when the recorded process still runs. The start ticks guard against a recycled PID."""
    pid = c.get("pid")
    if not pid:
        return False
    if HAS_PROC:
        info = proc_info(pid)
        return bool(info) and info[0] not in ("Z", "X") and c.get("startTicks") in (None, info[2])
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reap():
    for p in list(_children):
        if p.poll() is not None:
            _children.remove(p)


def group_alive(pgid):
    reap()
    if HAS_PROC:
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                i = proc_info(int(d.name))
                if i and i[1] == pgid and i[0] not in ("Z", "X"):
                    return True
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def orphan_group(c):
    """True when the group leader is gone but members still run. The kernel keeps a PGID reserved while a member lives, so a dead leader means the group is ours."""
    pgid = c.get("pgid") or c.get("pid")
    if not HAS_PROC or not pgid:
        return False
    lead = proc_info(pgid)
    if lead and lead[0] not in ("Z", "X"):  # a live process with this PID leads the group: either ours, or a recycled PID that is not ours
        return False
    return any((i := proc_info(int(d.name))) and i[1] == pgid and i[0] not in ("Z", "X") for d in Path("/proc").iterdir() if d.name.isdigit())


def stop(c):
    pgid = c.get("pgid") or c["pid"]
    if not is_ours(c) and not orphan_group(c):
        return
    for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
        try:
            if pgid == os.getpgrp():
                os.kill(c["pid"], sig)
            else:
                os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        end = time.time() + wait
        while time.time() < end:
            if not group_alive(pgid) and not is_ours(c):
                return
            time.sleep(0.1)


def spawn(rd, argv, cwd, env, logname, devnull_out=False):
    log = os.open(rd / RUN / logname, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        out = subprocess.DEVNULL if devnull_out else log
        # Detach method: start_new_session runs setsid in the child. The child leads its own session and group, so it outlives this shell.
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=log, start_new_session=True, close_fds=True)
    finally:
        os.close(log)
    _children.append(p)
    info = proc_info(p.pid)
    return p, {"pid": p.pid, "pgid": p.pid, "startTicks": info[2] if info else None}


def pidfile(rd, name, pid):
    write_private(rd / RUN / f"{name}.pid", f"{pid}\n")


def tail_lines(path, n):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def http_status(url, timeout=3):
    """HTTP status of a GET, or None when the connection fails. A redirect is not followed."""
    u = urllib.parse.urlsplit(url)
    c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        c.request("GET", (u.path or "/") + ("?" + u.query if u.query else ""))
        r = c.getresponse()
        r.read(1024)
        return r.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        c.close()


def port_busy(port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.5).close()
        return True
    except OSError:
        return False


def ts(args, timeout=30):
    try:
        return subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def child_env(drop_token=True):
    e = dict(os.environ)
    if drop_token:
        e.pop("REVIEW_TOKEN", None)
    return e


def sub_port(s, port):
    return s.replace("{port}", str(port))


# ---------------------------------------------------------------- preflight
def tailscale_status():
    r = ts(["status", "--json"])
    if r is None or r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def current_user():
    return pwd.getpwuid(os.getuid()).pw_name


def proxy_config_problem(rd):
    """Run the proxy in check mode: it applies every rule of review.json that the proxy enforces. None when it accepts the file."""
    script = os.environ.get("REVIEW_PROXY_SCRIPT") or str(HERE / "review-proxy.py")
    try:
        r = subprocess.run([sys.executable, script, "--config", str(rd / "review.json"), "--check"], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "the proxy check did not run"
    if r.returncode == 0:
        return None
    return (r.stderr.strip().splitlines() or ["the proxy rejects review.json"])[-1]


def preflight(rd, cfg, funnel_on, state=None):
    rows = []

    def add(cid, status, msg, fix=""):
        rows.append({"id": cid, "status": status, "message": msg, "fix": fix})

    add("config", "PASS", "review.json is valid")
    bad = proxy_config_problem(rd)
    add("proxy-config", "FAIL" if bad else "PASS", bad or "the proxy accepts review.json", "Fix review.json" if bad else "")
    for f in env_files(cfg, rd):
        p = env_problem(f)
        add(f"envfile:{f}", "FAIL" if p else "PASS", p[1] if p else "mode and owner are correct", f"chmod 600 {f}" if p else "")
    mine = {c["port"] for c in (state or {}).get("components", []) if c.get("managed", True) and c.get("port") and is_ours(c)}
    pp = cfg["proxy"]["port"]
    busy = port_busy(pp) and pp not in mine
    add("port:proxy", "FAIL" if busy else "PASS", f"port {pp} is in use (proxy)" if busy else f"port {pp} is free",
        f"port {pp} is in use (proxy). Stop that process or change proxy.port" if busy else "")
    for a in cfg["apps"]:
        if a["command"]:
            busy = port_busy(a["port"]) and a["port"] not in mine
            fix = f"port {a['port']} is in use ({a['name']}). Stop that process or change apps[].port"
            add(f"port:{a['name']}", "FAIL" if busy else "PASS", fix if busy else f"port {a['port']} is free", fix if busy else "")
        else:
            url = sub_port(a["health"]["url"], a["port"])
            s = http_status(url)
            ok = s is not None and s < 500
            fix = f"app {a['name']} is external and not reachable at {url}"
            add(f"health:{a['name']}", "PASS" if ok else "FAIL", f"HTTP {s}" if ok else fix, "" if ok else fix)
    if funnel_on:
        funnel_checks(cfg, add)
    return rows


def funnel_checks(cfg, add):
    if not shutil.which("tailscale"):
        add("tailscale:installed", "FAIL", "tailscale is not installed", "Install Tailscale: https://tailscale.com/download")
        add("tailscale:host", "PASS" if cfg["publicHost"] else "FAIL", cfg["publicHost"] or "no public host", "" if cfg["publicHost"] else "Set publicHost in review.json")
        return
    add("tailscale:installed", "PASS", "tailscale found")
    js = tailscale_status()
    if not js or js.get("BackendState") != "Running":
        add("tailscale:login", "FAIL", "tailscale is not running", "Run: tailscale up")
    else:
        add("tailscale:login", "PASS", "BackendState is Running")
    me = (js or {}).get("Self") or {}
    caps = set(me.get("CapMap") or {}) | set(me.get("Capabilities") or [])
    if {"funnel", "https"} <= caps:
        add("tailscale:funnel", "PASS", "this node can use Funnel")
    else:
        add("tailscale:funnel", "FAIL", "this node cannot use Funnel",
            'Funnel is off for this node. Turn on HTTPS and add the "funnel" node attribute in the tailnet policy.')
    allowed = list(FUNNEL_PORTS)
    for k in caps:
        if isinstance(k, str) and k.startswith("https://tailscale.com/cap/funnel-ports?ports="):
            allowed = [int(x) for x in k.split("=", 1)[1].split(",") if x.strip().isdigit()]
    port = cfg["funnel"]["port"]
    if port in allowed and port in FUNNEL_PORTS:
        add("tailscale:port", "PASS", f"port {port} is allowed")
    else:
        add("tailscale:port", "FAIL", f"port {port} is not allowed", "Funnel allows ports " + ", ".join(str(p) for p in allowed))
    op = os.geteuid() == 0
    if not op:
        r = ts(["debug", "prefs"])
        try:
            op = r is not None and r.returncode == 0 and json.loads(r.stdout).get("OperatorUser") == current_user()
        except ValueError:
            op = False
    add("tailscale:operator", "PASS" if op else "FAIL", "operator is set" if op else "operator is not set", "" if op else OPERATOR_FIX)
    host = cfg["publicHost"] or ((me.get("DNSName") or "").rstrip(".") or None)
    add("tailscale:host", "PASS" if host else "FAIL", host or "no public host", "" if host else "Set publicHost in review.json")


def print_preflight(rows, as_json=False):
    if as_json:
        print(json.dumps({"ok": not any(r["status"] == "FAIL" for r in rows), "checks": rows}, indent=1))
        return
    w = max(len(r["id"]) for r in rows)
    print(f"{'CHECK'.ljust(w)}  RESULT  MESSAGE")
    for r in rows:
        print(f"{r['id'].ljust(w)}  {r['status'].ljust(6)}  {r['message']}")
        if r["fix"] and r["status"] != "PASS":
            print(f"{' ' * w}          fix: {r['fix']}")


# ---------------------------------------------------------------- up and down
def expiry_for(cfg, now=None):
    now = now or now_utc()
    if cfg["expiresAt"]:
        d = parse_time(cfg["expiresAt"])
        if d <= now:
            die(2, "expiresAt is in the past")
        return d.replace(microsecond=0)
    if cfg["ttl"]:
        n, u = int(cfg["ttl"][:-1]), cfg["ttl"][-1]
        return now + timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[u]: n})
    return None


def managed(st):
    return [c for c in st["components"] if c.get("managed", True)]


def fail_start(rd, st, name, logname, msg):
    eprint(msg)
    for line in tail_lines(rd / RUN / logname, 20):
        eprint("  " + line)
    do_down(rd, st)
    die(4, "")


def die_on_signal(signum, _frame):  # SIGTERM and SIGHUP run the same rollback as Ctrl-C
    raise SystemExit(128 + signum)


def cmd_up(rd, cfg, funnel, skip_pre, explicit=None):
    expiry_for(cfg)  # an expiresAt in the past is an error before anything starts
    token = os.environ.get("REVIEW_TOKEN") or secrets.token_urlsafe(24)  # REVIEW_TOKEN keeps links that were already sent
    if not TOKEN_RX.fullmatch(token):
        die(2, "REVIEW_TOKEN must have 16 to 200 characters from A-Z a-z 0-9 _ . ~ -")
    envs = {}
    for f in env_files(cfg, rd):
        envs[f] = parse_env_file(f)
    with lock(rd):
        st = read_state(rd)
        if st:
            # Only a finished `up` writes "complete". A state without it came from an interrupted start, so it is stale.
            if st.get("complete") and any(c["kind"] == "proxy" for c in managed(st)) and all(is_ours(c) for c in managed(st)):
                if explicit is not None and explicit != bool((st.get("funnel") or {}).get("on")):
                    die(1, f"This review already runs with Funnel {'on' if (st.get('funnel') or {}).get('on') else 'off'}. Run: review.py down, then review.py up {'--funnel' if explicit else '--no-funnel'}")
                print_status(rd, st)
                return 0
            eprint("Found a stale state. Stopping what is left.")
            if do_down(rd, st) == 5:
                die(5, "Funnel is still on from the earlier run. Fix tailscale, then run: review.py down")
        if not skip_pre:
            rows = preflight(rd, cfg, funnel)
            if any(r["status"] == "FAIL" for r in rows):
                print_preflight(rows)
                return 1
        expiry = expiry_for(cfg)
        host = cfg["publicHost"]
        if not host and funnel:
            js = tailscale_status()
            host = ((((js or {}).get("Self") or {}).get("DNSName") or "").rstrip(".")) or None
            if not host:
                die(2, "Set publicHost in review.json")
        fport, pport = cfg["funnel"]["port"], cfg["proxy"]["port"]
        run = ensure_run(rd)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, die_on_signal)
        st = {"version": 1, "startedAt": iso(now_utc()), "expiresAt": iso(expiry) if expiry else None, "publicHost": host,
              "proxyPort": pport, "funnel": {"on": False, "port": fport}, "components": []}
        save_state(rd, st)
        try:
            for a in cfg["apps"]:
                url = sub_port(a["health"]["url"], a["port"])
                comp = {"name": a["name"], "kind": "app", "managed": bool(a["command"]), "port": a["port"], "healthUrl": url}
                if not a["command"]:
                    st["components"].append(comp)
                    continue
                env = child_env()
                if a["envFile"]:
                    env.update(envs[rd / a["envFile"]])
                env.update({k: sub_port(v, a["port"]) for k, v in a["env"].items()})
                argv = [sub_port(x, a["port"]) for x in a["command"]]
                try:
                    p, info = spawn(rd, argv, rel(rd, a["cwd"]), env, f"app-{a['name']}.log")
                except OSError as e:
                    do_down(rd, st)
                    die(4, f"app {a['name']} could not start: {e.strerror}")
                comp.update(info)
                st["components"].append(comp)
                pidfile(rd, f"app-{a['name']}", p.pid)
                save_state(rd, st)
                end = time.time() + a["health"]["timeoutSeconds"]
                while True:
                    s = http_status(url)
                    if s is not None and s < 500:
                        break
                    if p.poll() is not None:
                        fail_start(rd, st, a["name"], f"app-{a['name']}.log", f"app {a['name']} exited early (code {p.returncode})")
                    if time.time() > end:
                        fail_start(rd, st, a["name"], f"app-{a['name']}.log", f"app {a['name']} was not healthy at {url} after {a['health']['timeoutSeconds']:g} s")
                    time.sleep(TICK)
            script = os.environ.get("REVIEW_PROXY_SCRIPT") or str(HERE / "review-proxy.py")
            argv = [sys.executable, script, "--config", str(rd / "review.json"), "--port", str(pport)]
            # Without Funnel the proxy must serve plain HTTP on 127.0.0.1, so an empty value overrides publicHost from the config.
            argv += ["--public-host", (host + (f":{fport}" if fport != 443 else "")) if funnel and host else ""]
            if expiry:
                argv += ["--expires-at", iso(expiry)]
            env = child_env()
            if cfg["proxy"]["envFile"]:
                env.update(envs[rd / cfg["proxy"]["envFile"]])
            env["REVIEW_TOKEN"] = token
            p, info = spawn(rd, argv, rd, env, "proxy.log", devnull_out=True)  # the proxy prints the link on stdout. Nothing may keep it.
            st["components"].append({"name": "proxy", "kind": "proxy", "port": pport, **info})
            pidfile(rd, "proxy", p.pid)
            save_state(rd, st)
            end = time.time() + 15
            while http_status(f"http://127.0.0.1:{pport}/__review/meta") != 403:
                if p.poll() is not None:
                    fail_start(rd, st, "proxy", "proxy.log", f"proxy exited early (code {p.returncode})")
                if time.time() > end:
                    fail_start(rd, st, "proxy", "proxy.log", "proxy was not ready after 15 s")
                time.sleep(0.2)
            if funnel:
                st["funnel"]["on"] = True  # set before the call: a timeout may leave Funnel half on, and down must turn it off
                save_state(rd, st)
                r = ts(["funnel", "--bg", f"--https={fport}", f"http://127.0.0.1:{pport}"], 60)
                out = "" if r is None else (r.stdout + r.stderr)
                if r is None or r.returncode != 0:
                    if r is not None:  # tailscale refused the command, so Funnel is not on
                        st["funnel"]["on"] = False
                    do_down(rd, st)
                    die(5, OPERATOR_FIX if SUDO_CASE.search(out) else (out.strip() or "tailscale funnel failed to run"))
            if expiry:
                argv = [sys.executable, str(Path(__file__).resolve()), "-C", str(rd), "_watchdog"]
                p, info = spawn(rd, argv, rd, child_env(), "watchdog.log")
                st["components"].append({"name": "watchdog", "kind": "watchdog", **info})
                pidfile(rd, "watchdog", p.pid)
                save_state(rd, st)
            st["complete"] = True
            save_state(rd, st)
        except BaseException:
            if read_state(rd):  # a Stop that already ran do_down has removed it
                do_down(rd, st)
            raise
        print_status(rd, st)
        print("Share link:")
        print(share_link(st, token))
    return 0


def do_down(rd, st, skip=None):
    """Stop everything that state.json lists. The caller holds the lock. Return the exit code."""
    funnel_failed, out = False, ""
    f = st.get("funnel") or {}
    legacy = f.get("mode") == "external" and (st.get("complete") or f.get("stillPublic"))  # a state from an older version, with a Funnel that review.py did not turn on
    if f.get("on") or legacy:
        r = ts(["funnel", f"--https={f['port']}", "off"], 60)
        out = "" if r is None else r.stdout + r.stderr
        funnel_failed = (r is None or r.returncode != 0) and funnel_state(st) != "off"  # off may fail only because nothing was served
    comps = st.get("components", [])
    order = [c for c in comps if c["kind"] == "watchdog"] + [c for c in comps if c["kind"] == "proxy"] + [c for c in comps if c["kind"] == "app"][::-1]
    for c in order:
        if c["name"] != skip and c.get("managed", True):
            stop(c)
    run = rd / RUN
    for c in comps:
        (run / (f"app-{c['name']}.pid" if c["kind"] == "app" else f"{c['name']}.pid")).unlink(missing_ok=True)
    if funnel_failed:  # keep a state with the funnel flag and a marker, so that the next down, up or watchdog tries again
        save_state(rd, dict(st, components=[], complete=False, funnel=dict(f, stillPublic=True)))
        if SUDO_CASE.search(out):
            eprint(f"Funnel is still public. Run: {sudo_off(f)}")
        eprint("Funnel is still public. Run: review.py down to try again, or: tailscale funnel status")
        return 5
    (run / "state.json").unlink(missing_ok=True)
    return 0


def sudo_off(f):
    return f"sudo tailscale funnel --https={f['port']} off"


def cmd_down(rd):
    if not (rd / RUN).exists():
        eprint("Nothing is running.")
        return 0
    with lock(rd):
        st = read_state(rd)
        if not st:
            for p in (rd / RUN).glob("*.pid"):
                p.unlink(missing_ok=True)
            eprint("Nothing is running.")
            return 0
        return do_down(rd, st)


def cmd_watchdog(rd):
    while True:
        st = read_state(rd)
        if not st:
            return 0
        mine = [c for c in st["components"] if c["kind"] == "watchdog"]
        if mine and mine[0].get("pid") != os.getpid():
            return 0
        if not st.get("expiresAt"):
            return 0
        left = (parse_time(st["expiresAt"]) - datetime.now(timezone.utc)).total_seconds()
        if left <= 0:
            break
        time.sleep(min(left, 5))
    print("expired: running down", flush=True)
    for _ in range(WATCHDOG_RETRIES):
        try:
            with lock(rd):
                st = read_state(rd)
                code = do_down(rd, st, skip="watchdog") if st else 0
        except Stop:
            code = 1
        if code == 0:
            return 0
        print(f"down failed (exit {code}). Funnel may still be public. Trying again in {WATCHDOG_RETRY_SECONDS} s", flush=True)
        time.sleep(WATCHDOG_RETRY_SECONDS)
    print("down failed. Run: review.py down", flush=True)
    return 5


# ---------------------------------------------------------------- status, logs, link
def fmt_left(sec):
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


def funnel_state(st):
    r = ts(["funnel", "status"], 10) if shutil.which("tailscale") else None
    if r is None or r.returncode != 0:
        return "unknown"
    return "on" if f":{st['proxyPort']}" in r.stdout else "off"


def collect(st):
    rows, ok = [], True
    for c in st["components"]:
        mg = c.get("managed", True)
        alive = is_ours(c) if mg else None
        if c["kind"] == "proxy":
            health = "ok" if alive and http_status(f"http://127.0.0.1:{c['port']}/__review/meta") == 403 else "down" if alive else "-"
        elif c["kind"] == "app":
            s = http_status(c["healthUrl"])
            health = "ok" if s is not None and s < 500 else "down"
        else:
            health = "-"
        state = "external" if not mg else "running" if alive else "dead"
        if (mg and not alive) or (c["kind"] in ("proxy", "app") and health != "ok"):
            ok = False
        rows.append({"name": c["name"], "kind": c["kind"], "pid": c.get("pid"), "port": c.get("port"), "state": state, "health": health})
    f = st.get("funnel") or {}
    fs = funnel_state(st)
    if f.get("on") and fs == "off":
        ok = False
    still = bool(f.get("stillPublic")) and fs != "off"  # a marker from a failed off. It clears when tailscale no longer serves the proxy
    if still:
        ok = False
    exp = parse_time(st["expiresAt"]) if st.get("expiresAt") else None
    left = int((exp - datetime.now(timezone.utc)).total_seconds()) if exp else None
    if left is not None and left <= 0:
        ok = False
    return rows, {"on": bool(f.get("on")), "port": f.get("port"), "state": fs, "stillPublic": still}, left, ok


def print_status(rd, st, as_json=False):
    rows, fun, left, ok = collect(st)
    stale = any(r["state"] == "dead" for r in rows) and not any(r["state"] == "running" for r in rows)
    if as_json:
        print(json.dumps({"running": ok, "components": rows, "funnel": fun, "expiresAt": st.get("expiresAt"), "secondsLeft": left}, indent=1))
    else:
        line = lambda r: [r["name"], r["kind"], str(r["pid"] or "-"), str(r["port"] or "-"), r["state"], r["health"]]
        table = [["COMPONENT", "KIND", "PID", "PORT", "STATE", "HEALTH"]]
        table += [line(r) for r in rows if r["kind"] != "watchdog"]
        table.append(["funnel", "funnel", "-", str(fun["port"] or "-"), fun["state"] + (" (still public)" if fun["stillPublic"] else ""), "-"])
        table += [line(r) for r in rows if r["kind"] == "watchdog"]
        w = [max(len(x[i]) for x in table) for i in range(6)]
        for x in table:
            print("  ".join(c.ljust(w[i]) for i, c in enumerate(x)).rstrip())
        print("Expires: never" if left is None else f"Expires: {st['expiresAt']} ({'Expired' if left <= 0 else 'in ' + fmt_left(left)})")
    if stale:
        eprint("stale: run review.py down")
    if fun["stillPublic"]:
        eprint(f"Funnel still public, run: {sudo_off(st['funnel'])}")
    return 0 if ok else 1


def need_state(rd):
    st = read_state(rd)
    if not st:
        die(6, "Nothing is running. Run: review.py up")
    return st


def log_file(rd, name):
    return rd / RUN / (f"{name}.log" if name in ("proxy", "watchdog") else f"app-{name}.log")


def cmd_logs(rd, name, n, follow):
    st = need_state(rd)
    names = [c["name"] for c in st["components"]]
    if name and name not in names:
        die(2, f"unknown component {name}. Use one of: {', '.join(names)}")
    if follow and not name:
        die(2, "-f needs a NAME")
    if name:
        print("\n".join(tail_lines(log_file(rd, name), n)))
        if follow:
            path = log_file(rd, name)
            pos = path.stat().st_size if path.exists() else 0
            try:
                while read_state(rd):
                    if path.exists() and path.stat().st_size > pos:
                        with open(path, "rb") as f:
                            f.seek(pos)
                            chunk = f.read()
                        pos += len(chunk)
                        sys.stdout.write(chunk.decode("utf-8", "replace"))
                        sys.stdout.flush()
                    time.sleep(0.3)
            except KeyboardInterrupt:
                pass
        return 0
    for c in st["components"]:
        if c.get("managed", True):
            print(f"==> {c['name']} <==")
            print("\n".join(tail_lines(log_file(rd, c["name"]), n)))
    return 0


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(prog="review.py", description="Run a live-app review.")
    ap.add_argument("-C", "--review-dir", dest="review_dir", default=".", help="folder that holds review.json (default: current folder)")
    sub = ap.add_subparsers(dest="cmd", metavar="command", required=True)

    def funnel_flags(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--funnel", dest="funnel", action="store_true", default=None, help="turn Funnel on")
        g.add_argument("--no-funnel", dest="funnel", action="store_false", help="keep Funnel off")

    p = sub.add_parser("up", help="start the apps, the proxy and Funnel")
    funnel_flags(p)
    p.add_argument("--skip-preflight", action="store_true")
    sub.add_parser("down", help="stop everything that up started")
    p = sub.add_parser("status", help="show components and time left")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("logs", help="show component logs")
    p.add_argument("name", nargs="?", help="proxy, watchdog or an app name")
    p.add_argument("-n", type=int, default=50, metavar="LINES")
    p.add_argument("-f", action="store_true", help="follow (needs NAME)")
    p = sub.add_parser("preflight", help="check this machine before up")
    funnel_flags(p)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="check the running review like a reviewer")
    p.add_argument("--local", action="store_true", help="use the local proxy port, not the public host")
    p.add_argument("--json", action="store_true")
    p.add_argument("--screenshots-dir")
    sub.add_parser("link", help="print the share link")
    sub.add_parser("_watchdog")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    rd = Path(a.review_dir).resolve()
    try:
        if a.cmd == "_watchdog":
            return cmd_watchdog(rd)
        if a.cmd == "down":
            return cmd_down(rd)
        if a.cmd == "status":
            st = need_state(rd)
            return print_status(rd, st, a.json)
        if a.cmd == "logs":
            return cmd_logs(rd, a.name, a.n, a.f)
        if a.cmd == "link":
            st = need_state(rd)
            tok = read_token(rd)
            if not tok:
                die(6, "Cannot read the review token. On Linux it comes from the running proxy. Elsewhere set REVIEW_TOKEN")
            print(share_link(st, tok))
            return 0
        if a.cmd == "preflight":
            try:
                cfg = load_config(rd)
            except Stop as e:
                rows = [{"id": "config", "status": "FAIL", "message": str(e), "fix": "Fix review.json"}]
                print_preflight(rows, a.json)
                return 1
            rows = preflight(rd, cfg, cfg["funnel"]["enabled"] if a.funnel is None else a.funnel, read_state(rd))
            print_preflight(rows, a.json)
            return 1 if any(r["status"] == "FAIL" for r in rows) else 0
        cfg = load_config(rd)
        if a.cmd == "up":
            return cmd_up(rd, cfg, cfg["funnel"]["enabled"] if a.funnel is None else a.funnel, a.skip_preflight, a.funnel)
        if a.cmd == "verify":
            st = need_state(rd)
            tok = read_token(rd)
            if not tok:
                die(6, "Cannot read the review token. On Linux it comes from the running proxy. Elsewhere set REVIEW_TOKEN")
            sys.path.insert(0, str(HERE))
            import review_verify
            shots = Path(a.screenshots_dir) if a.screenshots_dir else rd / RUN / "verify"
            return review_verify.run(cfg, st, tok, a.local, a.json, shots)
    except Stop as e:
        if str(e):
            eprint(str(e))
        return e.code
    return 2


if __name__ == "__main__":
    sys.exit(main())
