#!/usr/bin/env python3
"""meta-linux-controller daemon -- v1.1.

Natalie's PC control daemon. Headless-first: it starts and serves
with no display attached (status, exec and the chat queue answer;
the eyes/hands verbs return a clean no_display error), watches for
the session display, and attaches when it appears.

v1.1 changes (issues #10-#16):
  * headless-first display tracking (#10)
  * two token scopes -- read and full -- with rotation (#11)
  * guardrails in code: exec denylist + a hands lock (#12)
  * window-scoped screenshot and verified type_in_window (#14)
  * chat seen receipts + last-poll heartbeat (#15)
  * append-only audit log of every call (#16)
  * the watch layer (#17) rides along unchanged

Stdlib only. Binds loopback + the tailnet address. Every call needs
a token; the read token can look, the full token can act.
"""
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FULL_PATH = os.path.join(APP_DIR, "token.txt")
TOKEN_READ_PATH = os.path.join(APP_DIR, "read-token.txt")
LOCK_PATH = os.path.join(APP_DIR, "lock.json")
STATE_PATH = os.path.join(APP_DIR, "state.json")
AUDIT_PATH = os.path.join(APP_DIR, "audit.log")
CHAT_DIR = os.path.join(APP_DIR, "chat")
INBOX_PATH = os.path.join(CHAT_DIR, "inbox.jsonl")    # Natalie -> Muse
OUTBOX_PATH = os.path.join(CHAT_DIR, "outbox.jsonl")  # Muse -> Natalie
PORT = int(os.environ.get("MLC_PORT", "8771"))
TAILNET_IP = os.environ.get("MLC_TAILNET_IP", "100.94.26.49")
VERSION = "1.1.0"

try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    HAS_ATSPI = True
except Exception:
    HAS_ATSPI = False

try:
    import mlc_watch
    HAS_WATCH = True
except Exception:
    HAS_WATCH = False

# ------------------------------------------------------------------
# Natalie's hard lines (#12). An exec command matching one of these
# is refused unless the request carries "confirm": true. The list is
# deliberately tight: disk wipes, filesystem formatting, power
# control, fork bombs, and recursive deletes of the places that end
# a machine or a home.
# ------------------------------------------------------------------
_SYSTEM_ROOTS = ("/", "/*", "/home", "/home/*", "~", "~/", "$HOME",
                 "/etc", "/usr", "/var", "/bin", "/sbin", "/lib",
                 "/lib64", "/opt", "/boot", "/root", "/sys", "/proc",
                 "/dev")


def _denylist_rule(cmd):
    """Return the name of the hard-line rule cmd trips, or None."""
    c = cmd.strip()
    if re.search(r":\s*\(\s*\)\s*\{", c) or ":(){" in c.replace(" ", ""):
        return "fork bomb"
    if re.search(r"\bmkfs(\.[a-z0-9]+)?\b", c):
        return "mkfs (format a filesystem)"
    if re.search(r"\bdd\b[^|;&]*\bof=/dev/(sd|nvme|vd|hd)[a-z0-9]*", c):
        return "dd onto a raw disk device"
    for segment in re.split(r"[|;&]+", c):
        words = segment.split()
        while words and (words[0] == "sudo" or "=" in words[0]):
            words = words[1:]
        if words and words[0] in ("shutdown", "reboot", "poweroff",
                                  "halt"):
            return "power control"
    if re.search(r"\bsystemctl\b[^|;&]*\b(poweroff|reboot|halt)\b", c):
        return "systemctl power control"
    m = re.search(r"\brm\b([^|;&]*)", c)
    if m:
        args = m.group(1)
        flags = "".join(re.findall(r"-[a-zA-Z]+", args))
        if "r" in flags or "--recursive" in args:
            targets = [t for t in re.findall(r"[^\s]+", args)
                       if not t.startswith("-")]
            for t in targets:
                base = t.rstrip("/") or "/"
                if t in _SYSTEM_ROOTS or base in _SYSTEM_ROOTS \
                        or base.startswith("/home/") and base.count("/") == 2:
                    return "recursive rm of %s" % t
    return None


# ------------------------------------------------------------------
# Small state helpers
# ------------------------------------------------------------------
def _read_file(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _write_secret(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(value + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class State:
    """Tokens, lock, heartbeat -- the daemon's mutable core."""

    def __init__(self):
        self.token_full = _read_file(TOKEN_FULL_PATH)
        if not self.token_full:
            self.token_full = secrets.token_urlsafe(32)
            _write_secret(TOKEN_FULL_PATH, self.token_full)
        self.token_read = _read_file(TOKEN_READ_PATH)
        if not self.token_read:
            self.token_read = secrets.token_urlsafe(32)
            _write_secret(TOKEN_READ_PATH, self.token_read)
        self.locked = False
        try:
            with open(LOCK_PATH) as fh:
                self.locked = bool(json.load(fh).get("locked"))
        except Exception:
            pass
        self.last_full_inbox_poll = None
        try:
            with open(STATE_PATH) as fh:
                self.last_full_inbox_poll = json.load(fh).get(
                    "last_full_inbox_poll")
        except Exception:
            pass
        self.mutex = threading.Lock()

    def scope_of(self, presented):
        if presented and secrets.compare_digest(presented,
                                                 self.token_full):
            return "full"
        if presented and secrets.compare_digest(presented,
                                                self.token_read):
            return "read"
        return None

    def set_locked(self, value):
        with self.mutex:
            self.locked = bool(value)
            tmp = LOCK_PATH + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"locked": self.locked}, fh)
            os.replace(tmp, LOCK_PATH)
        return self.locked

    def note_full_inbox_poll(self):
        with self.mutex:
            self.last_full_inbox_poll = time.time()
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"last_full_inbox_poll":
                           self.last_full_inbox_poll}, fh)
            os.replace(tmp, STATE_PATH)

    def rotate_tokens(self):
        with self.mutex:
            self.token_full = secrets.token_urlsafe(32)
            self.token_read = secrets.token_urlsafe(32)
            _write_secret(TOKEN_FULL_PATH, self.token_full)
            _write_secret(TOKEN_READ_PATH, self.token_read)
            return self.token_full, self.token_read


STATE = State()

# ------------------------------------------------------------------
# Audit log (#16): append-only JSONL, one line per call --
# {ts, verb, scope, summary, result}. Outputs are hashed, never
# stored; token values never appear. Rotates at ~5 MB, one old file.
# ------------------------------------------------------------------
_audit_lock = threading.Lock()
AUDIT_MAX = 5 * 1024 * 1024


def audit(verb, scope, summary, result, out_text=None):
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "verb": verb, "scope": scope,
             "summary": (summary or "")[:300], "result": result}
    if out_text is not None:
        entry["out_sha256"] = hashlib.sha256(
            out_text.encode("utf-8", "replace")).hexdigest()[:12]
    line = json.dumps(entry)
    with _audit_lock:
        try:
            if os.path.exists(AUDIT_PATH) \
                    and os.path.getsize(AUDIT_PATH) > AUDIT_MAX:
                os.replace(AUDIT_PATH, AUDIT_PATH + ".1")
            with open(AUDIT_PATH, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def audit_tail(limit):
    try:
        with open(AUDIT_PATH) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, limit):]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ------------------------------------------------------------------
# Display tracking (#10). The daemon probes the X socket for its
# $DISPLAY target; eyes/hands check display_up before touching X.
# ------------------------------------------------------------------
def _display_socket(display):
    m = re.match(r"(?:[^:]*):(\d+)", display or "")
    if not m:
        return None
    return "/tmp/.X11-unix/X%s" % m.group(1)


class DisplayWatch(threading.Thread):
    def __init__(self, env):
        super().__init__(daemon=True)
        self.env = env
        self.display = env.get("DISPLAY", ":0")
        self.up = False

    def probe(self):
        sock = _display_socket(self.display)
        return bool(sock and os.path.exists(sock))

    def run(self):
        while True:
            self.up = self.probe()
            time.sleep(3)


def build_env():
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XAUTHORITY", "/run/user/1000/gdm/Xauthority")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   "unix:path=/run/user/1000/bus")
    return env


ENV = build_env()
DISPLAY_WATCH = DisplayWatch(ENV)


def display_up():
    return DISPLAY_WATCH.up


def run(cmd, timeout=30, text=True):
    return subprocess.run(cmd, capture_output=True, text=text,
                          timeout=timeout, env=ENV)


def run_display(cmd, timeout=30, text=True):
    """run() for eyes/hands: a dead display becomes a clean
    no_display result instead of a stack of tool errors."""
    if not display_up():
        raise NoDisplay()
    return run(cmd, timeout=timeout, text=text)


class NoDisplay(Exception):
    pass


# ------------------------------------------------------------------
# Chat queue (same shape as v1; messages gain a seen flag, #15)
# ------------------------------------------------------------------
_chat_lock = threading.Lock()


def _chat_load(path):
    msgs = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        msgs.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return msgs


def _chat_append(path, text):
    os.makedirs(CHAT_DIR, exist_ok=True)
    with _chat_lock:
        msgs = _chat_load(path)
        msg = {"id": (msgs[-1]["id"] + 1) if msgs else 1,
               "ts": time.time(), "text": text, "seen": False}
        with open(path, "a") as fh:
            fh.write(json.dumps(msg) + "\n")
        return msg


def _chat_mark_seen(path, ids):
    with _chat_lock:
        msgs = _chat_load(path)
        changed = False
        for m in msgs:
            if m["id"] in ids and not m.get("seen"):
                m["seen"] = True
                changed = True
        if changed:
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                for m in msgs:
                    fh.write(json.dumps(m) + "\n")
            os.replace(tmp, path)
        return msgs


# ------------------------------------------------------------------
# Windows helpers (v1 logic: enumerate + match in Python;
# windowactivate --sync hangs on dwm, so it is never used)
# ------------------------------------------------------------------
def list_windows():
    r = run_display(["xdotool", "search", "--onlyvisible", ".",
                     "getwindowname", "%@", "getwindowgeometry", "%@"],
                    timeout=15)
    ids_r = run_display(["xdotool", "search", "--onlyvisible", "."],
                        timeout=15)
    ids = ids_r.stdout.split()
    active = run(["xdotool", "getactivewindow"], timeout=10) \
        .stdout.strip()
    wins = []
    for wid in ids:
        name = run(["xdotool", "getwindowname", wid],
                   timeout=10).stdout.strip()
        geo = run(["xdotool", "getwindowgeometry", wid],
                  timeout=10).stdout
        m = re.search(r"Position: (\d+),(\d+).*?Geometry: (\d+)x(\d+)",
                      geo, re.S)
        x = y = w = h = None
        if m:
            x, y, w, h = (int(v) for v in m.groups())
        wins.append({"id": wid, "title": name, "x": x, "y": y,
                     "width": w, "height": h,
                     "focused": wid == active})
    return wins


def focus_window(match):
    """Focus the first window whose title contains `match`
    (case-insensitive) or whose id equals it. Returns (id, title)."""
    wins = list_windows()
    chosen = None
    for w in wins:
        if w["id"] == str(match):
            chosen = w
            break
    if chosen is None:
        for w in wins:
            if match.lower() in (w["title"] or "").lower():
                chosen = w
                break
    if chosen is None:
        return None
    run(["xdotool", "windowactivate", chosen["id"]], timeout=10)
    time.sleep(0.4)
    active = run(["xdotool", "getactivewindow"],
                 timeout=10).stdout.strip()
    if active != chosen["id"]:
        run(["xdotool", "windowfocus", chosen["id"]], timeout=10)
        time.sleep(0.3)
    return chosen


def active_window_id():
    return run(["xdotool", "getactivewindow"], timeout=10) \
        .stdout.strip()


# ------------------------------------------------------------------
# AT-SPI tree (v1 behavior; optional enrichment only)
# ------------------------------------------------------------------
def _node_dict(node, depth):
    try:
        name = node.get_name() or ""
    except Exception:
        name = ""
    try:
        role = node.get_role_name() or ""
    except Exception:
        role = ""
    try:
        count = node.get_child_count()
    except Exception:
        count = 0
    out = {"role": role, "name": name, "children": []}
    if depth <= 0:
        if count:
            out["truncated_children"] = count
        return out
    for i in range(min(count, 200)):
        try:
            child = node.get_child_at_index(i)
        except Exception:
            child = None
        if child is not None:
            out["children"].append(_node_dict(child, depth - 1))
    return out


def get_tree(app_match=None, depth=4):
    if not display_up():
        raise NoDisplay()
    if not HAS_ATSPI:
        return {"ok": False, "error": "atspi_unavailable"}
    desktop = Atspi.get_desktop(0)
    apps = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        app_name = ""
        try:
            app_name = app.get_name() or ""
        except Exception:
            pass
        if app_match and app_match.lower() not in app_name.lower():
            continue
        apps.append(_node_dict(app, depth))
    return {"ok": True, "apps": apps}


# ------------------------------------------------------------------
# Verb implementations. Each returns (status, obj_or_bytes, ctype,
# summary, result); ctype None means JSON.
# ------------------------------------------------------------------
def v_ping(ctx):
    return 200, {"ok": True, "version": VERSION}, None, "ping", "ok"


def v_status(ctx):
    watch = {"available": HAS_WATCH, "active": False}
    if HAS_WATCH:
        try:
            watch = WATCH.status()
        except Exception as e:
            watch = {"available": True, "active": False,
                     "error": str(e)}
    ago = None
    if STATE.last_full_inbox_poll:
        ago = round(time.time() - STATE.last_full_inbox_poll, 1)
    return 200, {"ok": True, "version": VERSION,
                 "display_up": display_up(),
                 "locked": STATE.locked,
                 "scope": ctx["scope"],
                 "atspi": HAS_ATSPI,
                 "chat_last_full_inbox_poll_s_ago": ago,
                 "watch": watch}, None, "status", "ok"


def _locked_or_display(ctx, need_display=True):
    if STATE.locked:
        return 423, {"ok": False, "error": "locked"}, None, \
            ctx["verb"], "refused:locked"
    if need_display and not display_up():
        return 503, {"ok": False, "error": "no_display"}, None, \
            ctx["verb"], "refused:no_display"
    return None


def v_screenshot(ctx):
    if not display_up():
        return 503, {"ok": False, "error": "no_display"}, None, \
            "screenshot", "refused:no_display"
    body = ctx["body"]
    window = body.get("window") or ctx["query"].get("window", [None])[0]
    cmd = ["import", "-window", str(window) if window else "root",
           "png:-"]
    r = run_display(cmd, timeout=30, text=False)
    if r.returncode != 0:
        return 500, {"ok": False, "error": "screenshot_failed",
                     "stderr": (r.stderr or b"")[:200].decode(
                         "utf-8", "replace")}, None, "screenshot", \
            "error:screenshot_failed"
    summary = "screenshot window=%s" % window if window else "screenshot"
    return 200, r.stdout, "image/png", summary, "ok"


def v_windows(ctx):
    try:
        wins = list_windows()
    except NoDisplay:
        return 503, {"ok": False, "error": "no_display"}, None, \
            "windows", "refused:no_display"
    return 200, {"ok": True, "windows": wins}, None, "windows", "ok"


def v_tree(ctx):
    body = ctx["body"]
    try:
        tree = get_tree(body.get("app"),
                        int(body.get("depth", 4)))
    except NoDisplay:
        return 503, {"ok": False, "error": "no_display"}, None, \
            "tree", "refused:no_display"
    return 200, tree, None, "tree app=%s" % body.get("app"), "ok"


def v_click(ctx):
    bad = _locked_or_display(ctx)
    if bad:
        return bad
    body = ctx["body"]
    run_display(["xdotool", "mousemove", str(int(body["x"])),
                 str(int(body["y"])), "click",
                 str(int(body.get("button", 1)))], timeout=15)
    return 200, {"ok": True}, None, \
        "click %s,%s" % (body.get("x"), body.get("y")), "ok"


def v_type(ctx):
    bad = _locked_or_display(ctx)
    if bad:
        return bad
    text = ctx["body"].get("text", "")
    run_display(["xdotool", "type", "--clearmodifiers", "--", text],
                timeout=30)
    return 200, {"ok": True, "typed": len(text)}, None, \
        "type %d chars" % len(text), "ok"


def v_key(ctx):
    bad = _locked_or_display(ctx)
    if bad:
        return bad
    keys = ctx["body"].get("keys", "")
    run_display(["xdotool", "key", "--clearmodifiers", keys],
                timeout=15)
    return 200, {"ok": True}, None, "key %s" % keys, "ok"


def v_scroll(ctx):
    bad = _locked_or_display(ctx)
    if bad:
        return bad
    body = ctx["body"]
    clicks = int(body.get("clicks", 3))
    button = 4 if body.get("direction", "down") == "down" else 5
    run_display(["xdotool", "click", "--repeat", str(clicks),
                 str(button)], timeout=15)
    return 200, {"ok": True}, None, "scroll %s" % body.get(
        "direction", "down"), "ok"


def v_launch(ctx):
    bad = _locked_or_display(ctx)
    if bad:
        return bad
    command = ctx["body"].get("command", "")
    subprocess.Popen(["bash", "-lc", command], env=ENV,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return 200, {"ok": True, "launched": command}, None, \
        "launch %s" % command[:120], "ok"


def v_focus(ctx):
    bad = _locked_or_display(ctx)
    if bad:
        return bad
    match = ctx["body"].get("match", "")
    chosen = focus_window(match)
    if not chosen:
        return 404, {"ok": False, "error": "no_matching_window"}, \
            None, "focus %s" % match, "error:no_matching_window"
    return 200, {"ok": True, "focused": chosen}, None, \
        "focus %s" % match, "ok"


def v_type_in_window(ctx):
    """#14: focus the window by id, VERIFY the focused id, then
    type. The verify is the fix for v1's wrong-window typing."""
    bad = _locked_or_display(ctx)
    if bad:
        return bad
    body = ctx["body"]
    wid = str(body.get("window_id", ""))
    text = body.get("text", "")
    wins = list_windows()
    target = [w for w in wins if w["id"] == wid]
    if not target:
        return 404, {"ok": False, "error": "no_such_window",
                     "window_id": wid}, None, \
            "type_in_window %s" % wid, "error:no_such_window"
    run(["xdotool", "windowactivate", wid], timeout=10)
    focused = None
    for _ in range(10):
        time.sleep(0.2)
        focused = active_window_id()
        if focused == wid:
            break
    if focused != wid:
        run(["xdotool", "windowfocus", wid], timeout=10)
        time.sleep(0.4)
        focused = active_window_id()
    if focused != wid:
        return 409, {"ok": False, "error": "focus_mismatch",
                     "window_id": wid, "focused_id": focused}, None, \
            "type_in_window %s" % wid, "error:focus_mismatch"
    run_display(["xdotool", "type", "--clearmodifiers", "--", text],
                timeout=30)
    if body.get("press_enter"):
        run_display(["xdotool", "key", "--clearmodifiers", "Return"],
                    timeout=10)
    return 200, {"ok": True, "window_id": wid,
                 "focused_verified": True, "typed": len(text)}, \
        None, "type_in_window %s %d chars" % (wid, len(text)), "ok"


def v_exec(ctx):
    if STATE.locked:
        return 423, {"ok": False, "error": "locked"}, None, \
            "exec", "refused:locked"
    body = ctx["body"]
    command = body.get("command", "")
    rule = _denylist_rule(command)
    if rule and body.get("confirm") is not True:
        return 403, {"ok": False,
                     "error": "confirmation_required",
                     "rule": rule}, None, \
            "exec %s" % command[:200], "refused:denylist:%s" % rule
    timeout = int(body.get("timeout", 30))
    try:
        r = run(["bash", "-lc", command], timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        result = "ok" if r.returncode == 0 else \
            "error:exit_%d" % r.returncode
        return 200, {"ok": r.returncode == 0,
                     "returncode": r.returncode,
                     "stdout": r.stdout, "stderr": r.stderr}, None, \
            "exec %s" % command[:200], result
    except subprocess.TimeoutExpired:
        return 200, {"ok": False, "error": "timeout"}, None, \
            "exec %s" % command[:200], "error:timeout"


def v_lock(ctx):
    STATE.set_locked(True)
    return 200, {"ok": True, "locked": True}, None, "lock", "ok"


def v_unlock(ctx):
    STATE.set_locked(False)
    return 200, {"ok": True, "locked": False}, None, "unlock", "ok"


def v_rotate(ctx):
    full, read = STATE.rotate_tokens()
    # The new values are returned exactly once, here, so the client
    # can rewrite its local token files. They are never logged: the
    # audit summary for this verb is fixed text.
    return 200, {"ok": True, "full_token": full,
                 "read_token": read}, None, "tokens rotated", "ok"


def v_chat_send(ctx):
    msg = _chat_append(INBOX_PATH, ctx["body"].get("text", ""))
    return 200, {"ok": True, "message": msg}, None, \
        "chat/send %d chars" % len(msg["text"]), "ok"


def v_chat_reply(ctx):
    msg = _chat_append(OUTBOX_PATH, ctx["body"].get("text", ""))
    return 200, {"ok": True, "message": msg}, None, \
        "chat/reply %d chars" % len(msg["text"]), "ok"


def v_chat_inbox(ctx):
    msgs = _chat_load(INBOX_PATH)
    if ctx["scope"] == "full":
        # A full-token fetch is Muse reading her lines: mark them
        # seen and stamp the heartbeat (#15). Read-token fetches
        # (her console) never mark.
        msgs = _chat_mark_seen(INBOX_PATH, [m["id"] for m in msgs])
        STATE.note_full_inbox_poll()
    return 200, {"ok": True, "messages": msgs}, None, \
        "chat/inbox %d messages" % len(msgs), "ok"


def v_chat_outbox(ctx):
    msgs = _chat_load(OUTBOX_PATH)
    after = ctx["query"].get("after", [None])[0]
    if after is not None:
        try:
            after_id = int(after)
            msgs = [m for m in msgs if m["id"] > after_id]
        except ValueError:
            pass
    return 200, {"ok": True, "messages": msgs}, None, \
        "chat/outbox %d messages" % len(msgs), "ok"


def v_audit(ctx):
    try:
        limit = int(ctx["query"].get("limit", ["50"])[0])
    except ValueError:
        limit = 50
    entries = audit_tail(limit)
    return 200, {"ok": True, "entries": entries}, None, \
        "audit limit=%d" % limit, "ok"


# ------------------------------------------------------------------
# Watch verbs (#17)
# ------------------------------------------------------------------
def _watch_missing(ctx):
    return 503, {"ok": False, "error": "watch_unavailable"}, None, \
        ctx["verb"], "error:watch_unavailable"


def v_watch_open(ctx):
    if not HAS_WATCH:
        return _watch_missing(ctx)
    if not display_up():
        return 503, {"ok": False, "error": "no_display"}, None, \
            "watch/open", "refused:no_display"
    body = ctx["body"]
    info = WATCH.open_session(
        window_id=body.get("window"), scale=body.get("scale", 0.5),
        fps=body.get("fps", 10), record=bool(body.get("record", False)),
        idle_timeout_s=body.get("idle_timeout_s", 120))
    info = dict(info, ok=True)
    return 200, info, None, "watch/open", "ok"


def v_watch_close(ctx):
    if not HAS_WATCH:
        return _watch_missing(ctx)
    return 200, dict(WATCH.close_session(), ok=True), None, \
        "watch/close", "ok"


def v_watch_record(ctx):
    if not HAS_WATCH:
        return _watch_missing(ctx)
    on = bool(ctx["body"].get("on", True))
    return 200, dict(WATCH.set_record(on), ok=True), None, \
        "watch/record on=%s" % on, "ok"


def v_watch_frame(ctx):
    if not HAS_WATCH:
        return _watch_missing(ctx)
    data, seq = WATCH.latest_frame()
    if data is None:
        return 404, {"ok": False, "error": "no_active_session"}, \
            None, "watch/frame", "error:no_active_session"
    return 200, data, "image/jpeg", "watch/frame seq=%d" % seq, "ok"


def v_watch_stream(ctx):
    if not HAS_WATCH:
        return _watch_missing(ctx)
    handler = ctx["handler"]
    handler.send_response(200)
    handler.send_header("Content-Type",
                        "multipart/x-mixed-replace; boundary=frame")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    last = -1
    try:
        for data, seq in WATCH.frame_iter():
            if seq == last:
                continue
            last = seq
            handler.wfile.write(b"--frame\r\nContent-Type: "
                                b"image/jpeg\r\nContent-Length: "
                                + str(len(data)).encode()
                                + b"\r\n\r\n" + data + b"\r\n")
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    return "streamed"


# route -> (minimum scope, function). "read" routes also accept full.
ROUTES = {
    ("GET", "/ping"): ("read", v_ping),
    ("GET", "/status"): ("read", v_status),
    ("POST", "/screenshot"): ("read", v_screenshot),
    ("GET", "/screenshot"): ("read", v_screenshot),
    ("GET", "/windows"): ("read", v_windows),
    ("POST", "/tree"): ("read", v_tree),
    ("GET", "/chat/inbox"): ("read", v_chat_inbox),
    ("GET", "/chat/outbox"): ("read", v_chat_outbox),
    ("GET", "/audit"): ("read", v_audit),
    ("GET", "/watch/frame"): ("read", v_watch_frame),
    ("GET", "/watch/stream"): ("read", v_watch_stream),
    ("POST", "/click"): ("full", v_click),
    ("POST", "/type"): ("full", v_type),
    ("POST", "/key"): ("full", v_key),
    ("POST", "/scroll"): ("full", v_scroll),
    ("POST", "/launch"): ("full", v_launch),
    ("POST", "/focus"): ("full", v_focus),
    ("POST", "/type_in_window"): ("full", v_type_in_window),
    ("POST", "/exec"): ("full", v_exec),
    ("POST", "/lock"): ("full", v_lock),
    ("POST", "/unlock"): ("full", v_unlock),
    ("POST", "/tokens/rotate"): ("full", v_rotate),
    ("POST", "/chat/send"): ("full", v_chat_send),
    ("POST", "/chat/reply"): ("full", v_chat_reply),
    ("POST", "/watch/open"): ("full", v_watch_open),
    ("POST", "/watch/close"): ("full", v_watch_close),
    ("POST", "/watch/record"): ("full", v_watch_record),
}

WATCH = mlc_watch.WatchManager(APP_DIR, ENV) if HAS_WATCH else None


# ------------------------------------------------------------------
# HTTP plumbing: auth, dispatch, audit on every path
# ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload, ctype):
        if ctype is None:
            data = json.dumps(payload).encode()
            ctype = "application/json"
        else:
            data = payload
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        verb = "%s %s" % (method, path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode())
            except ValueError:
                body = {}
        # screenshot may be a plain GET; merge query into body
        if method == "GET" and query.get("window"):
            body["window"] = query["window"][0]

        scope = STATE.scope_of(self.headers.get("X-Token", ""))
        route = ROUTES.get((method, path))
        if route is None:
            self._send(404, {"ok": False, "error": "no_such_route"},
                       None)
            audit(verb, scope or "none", verb, "refused:404")
            return
        need, fn = route
        if scope is None:
            self._send(401, {"ok": False,
                             "error": "unauthorized"}, None)
            audit(verb, "none", verb, "refused:401")
            return
        if need == "full" and scope == "read":
            self._send(403, {"ok": False,
                             "error": "insufficient_scope",
                             "needs": "full"}, None)
            audit(verb, scope, verb, "refused:403_needs_full")
            return
        ctx = {"scope": scope, "body": body, "query": query,
               "handler": self, "verb": verb}
        try:
            out = fn(ctx)
        except NoDisplay:
            self._send(503, {"ok": False, "error": "no_display"},
                       None)
            audit(verb, scope, verb, "refused:no_display")
            return
        except Exception as e:
            self._send(500, {"ok": False, "error": "internal",
                             "detail": str(e)[:200]}, None)
            audit(verb, scope, verb, "error:internal:%s"
                  % str(e)[:80])
            return
        if out == "streamed":
            audit(verb, scope, "watch/stream", "ok")
            return
        status, payload, ctype, summary, result = out
        out_text = None
        if path == "/exec" and isinstance(payload, dict) \
                and "returncode" in payload:
            # Only a call that actually executed has output to hash;
            # refusals carry no out_sha256.
            out_text = (payload.get("stdout") or "") + \
                (payload.get("stderr") or "")
        self._send(status, payload, ctype)
        audit(verb, scope, summary, result, out_text=out_text)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    DISPLAY_WATCH.start()
    servers = []
    for host in ("127.0.0.1", TAILNET_IP):
        try:
            servers.append(ThreadingHTTPServer((host, PORT), Handler))
        except OSError as e:
            print("bind %s:%d failed: %s" % (host, PORT, e),
                  flush=True)
    if not servers:
        raise SystemExit("no bind succeeded")
    print("meta-linux-controller %s on port %d (display_up=%s, "
          "locked=%s)" % (VERSION, PORT, display_up(), STATE.locked),
          flush=True)
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
