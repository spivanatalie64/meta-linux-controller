#!/usr/bin/env python3
"""Meta Linux Controller daemon -- v1.

A small token-authenticated HTTP API that lets Muse see and drive
Natalie's PC (X11 session) from the Muse VM. Python 3 standard
library only for the server itself; desktop work is done with tools
already on the machine:

  - screenshots: ImageMagick `import -window root`
  - input + windows: xdotool (XTEST)
  - UI tree: AT-SPI via GObject introspection (gi.repository.Atspi) --
    pyatspi is not installable on this machine without sudo (it is a
    distro package and not published for Python 3.14 on PyPI), and the
    Atspi typelib talks to the same accessibility bus.

Bind scope is a hard line: 127.0.0.1 plus the machine's Tailscale
address only. Never 0.0.0.0. Every endpoint requires the token.

See docs/architecture.md and docs/safety.md in the repo.
"""

import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1.0"
STARTED = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.environ.get("MLC_TOKEN_FILE", os.path.join(HERE, "token.txt"))
PORT = int(os.environ.get("MLC_PORT", "8771"))
MAX_EXEC_TIMEOUT = 120
MAX_OUTPUT_CHARS = 65536


def load_token():
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        return tok or None
    except OSError:
        return None


TOKEN = load_token()

# ------------------------------------------------------------ watch (#17)
# Live X11 capture (MJPEG stream + latest frame + rolling recording),
# implemented in mlc_watch.py next to this file. Optional: if the
# module is missing the daemon still runs and /watch/* says so.
try:
    import mlc_watch as _mlc_watch

    WATCH = _mlc_watch.WatchManager(HERE, os.environ)
except Exception as _watch_err:  # pragma: no cover
    WATCH = None
    sys.stderr.write(f"mlc: watch layer unavailable: {_watch_err}\n")


def tailnet_ip():
    """Best-effort discovery of this machine's Tailscale IPv4 address."""
    env_ip = os.environ.get("MLC_TAILNET_IP", "").strip()
    if env_ip:
        return env_ip
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.startswith("100."):
                return line
    except Exception:
        pass
    return None


def run_cmd(args, timeout=20, input_bytes=None):
    """Run a command, return (exit_code, stdout_bytes, stderr_bytes)."""
    p = subprocess.run(
        args, capture_output=True, timeout=timeout, input=input_bytes
    )
    return p.returncode, p.stdout, p.stderr


# ---------------------------------------------------------------- eyes

def do_screenshot():
    code, out, err = run_cmd(
        ["import", "-window", "root", "png:-"], timeout=30
    )
    if code != 0 or not out:
        raise RuntimeError(
            "import failed: " + err.decode(errors="replace")[:200]
        )
    return out


def do_windows():
    # This xdotool build requires an explicit pattern; "" matches all.
    code, out, _ = run_cmd(
        ["xdotool", "search", "--onlyvisible", ""], timeout=10
    )
    ids = out.decode(errors="replace").split()
    active = None
    try:
        _, aout, _ = run_cmd(["xdotool", "getactivewindow"], timeout=5)
        active = aout.decode(errors="replace").strip() or None
    except Exception:
        pass
    wins = []
    for wid in ids:
        try:
            _, nout, _ = run_cmd(["xdotool", "getwindowname", wid], timeout=5)
            name = nout.decode(errors="replace").strip()
        except Exception:
            name = ""
        if not name:
            continue
        geo = {}
        try:
            _, gout, _ = run_cmd(
                ["xdotool", "getwindowgeometry", "--shell", wid], timeout=5
            )
            for line in gout.decode(errors="replace").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    geo[k.lower()] = v
        except Exception:
            pass
        wins.append(
            {
                "id": wid,
                "title": name,
                "x": int(geo["x"]) if geo.get("x", "").lstrip("-").isdigit() else None,
                "y": int(geo["y"]) if geo.get("y", "").lstrip("-").isdigit() else None,
                "width": int(geo["width"]) if geo.get("width", "").isdigit() else None,
                "height": int(geo["height"]) if geo.get("height", "").isdigit() else None,
                "focused": wid == active,
            }
        )
    return wins


def do_tree():
    """Walk the AT-SPI desktop tree. Returns (text, focused_window_name)."""
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    lines = []
    focused_window = [None]
    count = [0]
    MAX_NODES = 3000
    MAX_DEPTH = 14

    def fmt(obj):
        try:
            role = obj.get_role_name() or "?"
        except Exception:
            role = "?"
        try:
            name = obj.get_name() or ""
        except Exception:
            name = ""
        geo = ""
        try:
            ext = obj.get_extents(Atspi.CoordType.SCREEN)
            if ext.width > 0 and ext.height > 0:
                geo = f" @{ext.x},{ext.y} {ext.width}x{ext.height}"
        except Exception:
            pass
        flags = ""
        try:
            states = obj.get_state_set()
            if states.contains(Atspi.StateType.FOCUSED):
                flags += " [focused]"
            if states.contains(Atspi.StateType.ACTIVE):
                flags += " [active]"
                if role in ("frame", "window", "dialog") and name:
                    focused_window[0] = name
        except Exception:
            pass
        label = f'{role} "{name}"' if name else role
        return label + geo + flags

    def walk(obj, depth):
        if count[0] >= MAX_NODES or depth > MAX_DEPTH or obj is None:
            return
        count[0] += 1
        lines.append("  " * depth + fmt(obj))
        try:
            n = obj.get_child_count()
        except Exception:
            return
        for i in range(n):
            try:
                child = obj.get_child_at_index(i)
            except Exception:
                child = None
            if child is not None:
                walk(child, depth + 1)

    desktop = Atspi.get_desktop(0)
    walk(desktop, 0)
    return "\n".join(lines), focused_window[0]


# ---------------------------------------------------------------- hands

def do_click(x, y, button=1):
    run_cmd(
        ["xdotool", "mousemove", "--sync", str(int(x)), str(int(y)),
         "click", str(int(button))],
        timeout=10,
    )


def do_type(text):
    run_cmd(
        ["xdotool", "type", "--clearmodifiers", "--delay", "12", "--", text],
        timeout=60,
    )


def do_key(keys):
    run_cmd(["xdotool", "key", "--clearmodifiers", keys], timeout=10)


def do_scroll(x, y, dy):
    button = "5" if dy > 0 else "4"
    clicks = min(abs(int(dy)), 25)
    if clicks == 0:
        clicks = 1
    args = ["xdotool", "mousemove", "--sync", str(int(x)), str(int(y))]
    for _ in range(clicks):
        args += ["click", button]
    run_cmd(args, timeout=15)


def do_launch(body):
    app = body.get("app")
    command = body.get("command")
    env = dict(os.environ)
    if app:
        p = subprocess.Popen(
            ["gtk-launch", app], env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return {"launched": app, "pid": p.pid}
    if not command:
        raise ValueError("launch needs 'command' or 'app'")
    p = subprocess.Popen(
        ["/bin/bash", "-lc", command], env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"launched": command, "pid": p.pid}


def do_focus(title):
    # Substring match in Python over ALL windows (not just visible):
    # xdotool's regex search is POSIX-flavoured and escapes bite, and on
    # a tagging window manager (dwm) the target may be on another tag --
    # windowactivate switches to it.
    code, out, _ = run_cmd(["xdotool", "search", ""], timeout=10)
    ids = out.decode(errors="replace").split()
    wid = None
    matched_name = None
    for cand in ids:
        try:
            _, nout, _ = run_cmd(["xdotool", "getwindowname", cand], timeout=5)
            name = nout.decode(errors="replace").strip()
        except Exception:
            continue
        if title in name:
            wid, matched_name = cand, name
            break
    if not wid:
        return {"ok": False, "error": f"no window matching {title!r}"}
    # No --sync here: on this window manager (dwm) activation is not
    # confirmed back, and --sync blocks until the call times out.
    run_cmd(["xdotool", "windowactivate", wid], timeout=5)
    run_cmd(["xdotool", "windowraise", wid], timeout=5)
    _, nout, _ = run_cmd(["xdotool", "getwindowname", wid], timeout=5)
    return {"ok": True, "focused": nout.decode(errors="replace").strip(), "id": wid}


# ---------------------------------------------------------------- shell

def do_exec(command, timeout_s):
    timeout_s = max(1, min(int(timeout_s or 30), MAX_EXEC_TIMEOUT))
    env = dict(os.environ)
    p = subprocess.Popen(
        ["/bin/bash", "-lc", command], env=env, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    timed_out = False
    try:
        out, err = p.communicate(timeout=timeout_s)
        code = p.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(p.pid), 9)
        except Exception:
            p.kill()
        out, err = p.communicate()
        code = None
    def cap(s):
        if len(s) > MAX_OUTPUT_CHARS:
            return s[:MAX_OUTPUT_CHARS], True
        return s, False
    out, out_trunc = cap(out or "")
    err, err_trunc = cap(err or "")
    return {
        "stdout": out,
        "stderr": err,
        "exit_code": code,
        "timed_out": timed_out,
        "truncated": out_trunc or err_trunc,
    }


# ---------------------------------------------------------------- chat
# A tiny store-and-forward queue so Natalie can talk to Muse from a
# console on the PC (see client/muse-chat). Her lines land in
# chat/inbox.jsonl; Muse's replies land in chat/outbox.jsonl. Files
# live under ~/meta-linux-controller/chat/ on the PC and nowhere else.

CHAT_DIR = os.path.join(HERE, "chat")
CHAT_INBOX = os.path.join(CHAT_DIR, "inbox.jsonl")
CHAT_OUTBOX = os.path.join(CHAT_DIR, "outbox.jsonl")
_chat_lock = threading.Lock()


def _chat_append(path, text):
    with _chat_lock:
        os.makedirs(CHAT_DIR, exist_ok=True)
        n = 0
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                n = sum(1 for _ in f)
        entry = {
            "id": n + 1,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "text": text,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return entry


def _chat_read(path, after=0):
    out = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("id", 0) > after:
                    out.append(entry)
    return out


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "MetaLinuxController/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Method + path + status only. Never headers, never bodies,
        # never the token.
        sys.stderr.write("mlc: %s\n" % (fmt % args))

    def _authed(self):
        if not TOKEN:
            return False
        given = self.headers.get("X-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            given = auth[len("Bearer "):]
        return hmac.compare_digest(given, TOKEN)

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_png(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_jpeg(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _watch_stream(self):
        """GET /watch/stream -- multipart/x-mixed-replace MJPEG."""
        if WATCH is None:
            self._send_json({"ok": False, "error": "watch_unavailable"}, 501)
            return
        try:
            session = WATCH.stream_open()
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 409)
            return
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=mlcframe",
            )
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.close_connection = True
            seq = 0
            while True:
                latest = WATCH.wait_frame(seq, timeout=20)
                if latest is None:
                    break
                frame, seq, _ts = latest
                part = (
                    b"--mlcframe\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    + frame + b"\r\n"
                )
                self.wfile.write(part)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            WATCH.stream_close(session)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _route(self, method):
        if not self._authed():
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        path, _, query = self.path.partition("?")
        if method == "GET" and path == "/watch/stream":
            self._watch_stream()
            return
        params = urllib.parse.parse_qs(query)
        try:
            after = int(params.get("after", ["0"])[0])
        except ValueError:
            after = 0
        try:
            if method == "GET" and path == "/ping":
                self._send_json({
                    "ok": True, "service": "meta-linux-controller",
                    "version": VERSION, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
            elif method == "GET" and path == "/status":
                self._send_json({
                    "ok": True,
                    "service": "meta-linux-controller",
                    "version": VERSION,
                    "hostname": socket.gethostname(),
                    "user": os.environ.get("USER", ""),
                    "display": os.environ.get("DISPLAY", ""),
                    "tailnet_ip": tailnet_ip(),
                    "binds": BINDS,
                    "token_present": bool(TOKEN),
                    "uptime_s": round(time.time() - STARTED, 1),
                    "pid": os.getpid(),
                    "watch": (WATCH.status() if WATCH is not None
                              else {"available": False}),
                })
            elif method == "POST" and path == "/screenshot":
                self._send_png(do_screenshot())
            elif method == "GET" and path == "/windows":
                self._send_json({"ok": True, "windows": do_windows()})
            elif method == "GET" and path == "/tree":
                text, focused = do_tree()
                self._send_json(
                    {"ok": True, "focused_window": focused, "tree": text}
                )
            elif method == "POST" and path == "/click":
                b = self._body()
                do_click(b.get("x", 0), b.get("y", 0), b.get("button", 1))
                self._send_json({"ok": True})
            elif method == "POST" and path == "/type":
                b = self._body()
                do_type(str(b.get("text", "")))
                self._send_json({"ok": True, "typed_chars": len(str(b.get("text", "")))})
            elif method == "POST" and path == "/key":
                b = self._body()
                do_key(str(b.get("keys", "")))
                self._send_json({"ok": True})
            elif method == "POST" and path == "/scroll":
                b = self._body()
                do_scroll(b.get("x", 0), b.get("y", 0), b.get("dy", 0))
                self._send_json({"ok": True})
            elif method == "POST" and path == "/launch":
                self._send_json({"ok": True, **do_launch(self._body())})
            elif method == "POST" and path == "/focus":
                b = self._body()
                self._send_json(do_focus(str(b.get("title", ""))))
            elif method == "POST" and path == "/exec":
                b = self._body()
                res = do_exec(str(b.get("command", "")), b.get("timeout", 30))
                self._send_json({"ok": True, **res})
            elif method == "POST" and path == "/chat/send":
                b = self._body()
                entry = _chat_append(CHAT_INBOX, str(b.get("text", "")))
                self._send_json({"ok": True, "id": entry["id"]})
            elif method == "GET" and path == "/chat/inbox":
                self._send_json(
                    {"ok": True, "messages": _chat_read(CHAT_INBOX, after)}
                )
            elif method == "POST" and path == "/chat/reply":
                b = self._body()
                entry = _chat_append(CHAT_OUTBOX, str(b.get("text", "")))
                self._send_json({"ok": True, "id": entry["id"]})
            elif method == "GET" and path == "/chat/outbox":
                self._send_json(
                    {"ok": True, "messages": _chat_read(CHAT_OUTBOX, after)}
                )
            elif method == "POST" and path == "/watch/open":
                if WATCH is None:
                    self._send_json(
                        {"ok": False, "error": "watch_unavailable"}, 501)
                else:
                    b = self._body()
                    info = WATCH.open_session(
                        window_id=b.get("window"),
                        scale=b.get("scale", 0.5),
                        fps=b.get("fps", 10),
                        record=bool(b.get("record", False)),
                        idle_timeout_s=b.get("idle_timeout_s", 120),
                    )
                    self._send_json({"ok": True, **info})
            elif method == "POST" and path == "/watch/close":
                if WATCH is None:
                    self._send_json(
                        {"ok": False, "error": "watch_unavailable"}, 501)
                else:
                    self._send_json(WATCH.close_session())
            elif method == "POST" and path == "/watch/record":
                if WATCH is None:
                    self._send_json(
                        {"ok": False, "error": "watch_unavailable"}, 501)
                else:
                    b = self._body()
                    self._send_json(WATCH.set_record(bool(b.get("on", False))))
            elif method == "GET" and path == "/watch/frame":
                if WATCH is None:
                    self._send_json(
                        {"ok": False, "error": "watch_unavailable"}, 501)
                else:
                    try:
                        frame, seq, ts = WATCH.get_frame()
                        self._send_jpeg(frame)
                    except Exception as e:
                        self._send_json({"ok": False, "error": str(e)}, 409)
            else:
                self._send_json({"ok": False, "error": "no such endpoint"}, 404)
        except subprocess.TimeoutExpired:
            self._send_json({"ok": False, "error": "command timed out"}, 504)
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


BINDS = []


def main():
    if not TOKEN:
        sys.stderr.write(
            "mlc: no token found at %s -- refusing to start\n" % TOKEN_FILE
        )
        return 1
    hosts = ["127.0.0.1"]
    tip = tailnet_ip()
    if tip and tip not in hosts:
        hosts.append(tip)
    servers = []
    for host in hosts:
        try:
            srv = ThreadingHTTPServer((host, PORT), Handler)
            srv.daemon_threads = True
            BINDS.append(f"{host}:{PORT}")
            servers.append(srv)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            sys.stderr.write(f"mlc: listening on {host}:{PORT}\n")
        except OSError as e:
            sys.stderr.write(f"mlc: could not bind {host}:{PORT}: {e}\n")
    if not servers:
        return 1
    sys.stderr.write("mlc: ready\n")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
