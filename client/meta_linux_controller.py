#!/usr/bin/env python3
"""Meta Linux Controller client -- runs on the Muse VM.

Wraps the daemon's verbs. Reads the token from
~/workspace/tools/meta-linux-controller-token.txt (mode 600; override
with MLC_TOKEN_FILE). Talks to the local end of the SSH port-forward
to Natalie's PC; if that port is not answering, it starts the forward
first (ssh -F ~/workspace/tools/sshcfg-pc-ts -N -L 8771:127.0.0.1:8771
natalie-pc-ts). Override the base URL with MLC_BASE.

Usage:
  meta_linux_controller.py ping
  meta_linux_controller.py status
  meta_linux_controller.py windows
  meta_linux_controller.py tree [--out FILE]
  meta_linux_controller.py screenshot FILE.png
  meta_linux_controller.py click X Y [--button N]
  meta_linux_controller.py type TEXT
  meta_linux_controller.py key KEYS
  meta_linux_controller.py scroll X Y DY
  meta_linux_controller.py launch COMMAND...
  meta_linux_controller.py focus TITLE
  meta_linux_controller.py exec [--timeout N] COMMAND...
  meta_linux_controller.py watch-open [--window ID] [--scale F]
                                      [--fps N] [--record]
  meta_linux_controller.py watch-close
  meta_linux_controller.py watch-record on|off
  meta_linux_controller.py watch-frame FILE.jpg
  meta_linux_controller.py watch [--seconds N] [--out DIR]
                                 [--window ID] [--scale F] [--fps N]
                                 [--record] [--interval S] [--keep-open]
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("MLC_BASE", "http://127.0.0.1:8771")
TOKEN_FILE = os.environ.get(
    "MLC_TOKEN_FILE",
    os.path.expanduser("~/workspace/tools/meta-linux-controller-token.txt"),
)
SSH_CONFIG = os.path.expanduser("~/workspace/tools/sshcfg-pc-ts")
TUNNEL_LOG = "/tmp/mlc-tunnel.log"


def token():
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def port_answers():
    host = "127.0.0.1"
    port = int(BASE.rsplit(":", 1)[1].split("/")[0])
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def ensure_tunnel():
    if port_answers():
        return
    with open(TUNNEL_LOG, "ab") as log:
        subprocess.Popen(
            ["ssh", "-F", SSH_CONFIG, "-N",
             "-L", "8771:127.0.0.1:8771", "natalie-pc-ts"],
            stdout=log, stderr=log, start_new_session=True,
        )
    for _ in range(30):
        if port_answers():
            return
        time.sleep(0.5)
    raise SystemExit(f"tunnel did not come up; see {TUNNEL_LOG}")


def call(method, path, body=None, raw=False):
    ensure_tunnel()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"X-Token": token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            obj = json.loads(payload.decode())
        except Exception:
            obj = {"ok": False, "error": payload.decode(errors="replace")[:300]}
        print(json.dumps(obj, indent=2))
        raise SystemExit(f"HTTP {e.code}")
    if raw:
        return payload
    return json.loads(payload.decode())


def main():
    ap = argparse.ArgumentParser(prog="meta_linux_controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    sub.add_parser("status")
    sub.add_parser("windows")
    t = sub.add_parser("tree")
    t.add_argument("--out")
    s = sub.add_parser("screenshot")
    s.add_argument("file")
    c = sub.add_parser("click")
    c.add_argument("x", type=int)
    c.add_argument("y", type=int)
    c.add_argument("--button", type=int, default=1)
    ty = sub.add_parser("type")
    ty.add_argument("text")
    k = sub.add_parser("key")
    k.add_argument("keys")
    sc = sub.add_parser("scroll")
    sc.add_argument("x", type=int)
    sc.add_argument("y", type=int)
    sc.add_argument("dy", type=int)
    la = sub.add_parser("launch")
    la.add_argument("command", nargs="+")
    fo = sub.add_parser("focus")
    fo.add_argument("title")
    ex = sub.add_parser("exec")
    ex.add_argument("--timeout", type=int, default=30)
    ex.add_argument("command", nargs="+")
    wo = sub.add_parser("watch-open")
    wo.add_argument("--window")
    wo.add_argument("--scale", type=float, default=0.5)
    wo.add_argument("--fps", type=int, default=10)
    wo.add_argument("--record", action="store_true")
    wo.add_argument("--idle-timeout", type=int, default=120)
    sub.add_parser("watch-close")
    wr = sub.add_parser("watch-record")
    wr.add_argument("onoff", choices=["on", "off"])
    wf = sub.add_parser("watch-frame")
    wf.add_argument("file")
    wa = sub.add_parser("watch")
    wa.add_argument("--seconds", type=float, default=10)
    wa.add_argument("--out", default="/tmp/mlc-watch-frames")
    wa.add_argument("--window")
    wa.add_argument("--scale", type=float, default=0.5)
    wa.add_argument("--fps", type=int, default=10)
    wa.add_argument("--record", action="store_true")
    wa.add_argument("--interval", type=float, default=0.25)
    wa.add_argument("--keep-open", action="store_true")
    ci = sub.add_parser("chat-inbox")
    ci.add_argument("--after", type=int, default=0)
    co = sub.add_parser("chat-outbox")
    co.add_argument("--after", type=int, default=0)
    cs = sub.add_parser("chat-send")
    cs.add_argument("text")
    cr = sub.add_parser("chat-reply")
    cr.add_argument("text")
    args = ap.parse_args()

    if args.cmd == "ping":
        print(json.dumps(call("GET", "/ping"), indent=2))
    elif args.cmd == "status":
        print(json.dumps(call("GET", "/status"), indent=2))
    elif args.cmd == "windows":
        print(json.dumps(call("GET", "/windows"), indent=2))
    elif args.cmd == "tree":
        res = call("GET", "/tree")
        print("focused window:", res.get("focused_window"))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(res.get("tree", ""))
            print(f"tree written to {args.out}")
        else:
            print(res.get("tree", ""))
    elif args.cmd == "screenshot":
        data = call("POST", "/screenshot", body={}, raw=True)
        with open(args.file, "wb") as f:
            f.write(data)
        print(f"screenshot: {len(data)} bytes -> {args.file}")
    elif args.cmd == "click":
        print(json.dumps(call("POST", "/click",
                              {"x": args.x, "y": args.y, "button": args.button})))
    elif args.cmd == "type":
        print(json.dumps(call("POST", "/type", {"text": args.text})))
    elif args.cmd == "key":
        print(json.dumps(call("POST", "/key", {"keys": args.keys})))
    elif args.cmd == "scroll":
        print(json.dumps(call("POST", "/scroll",
                              {"x": args.x, "y": args.y, "dy": args.dy})))
    elif args.cmd == "launch":
        print(json.dumps(call("POST", "/launch",
                              {"command": " ".join(args.command)})))
    elif args.cmd == "focus":
        print(json.dumps(call("POST", "/focus", {"title": args.title})))
    elif args.cmd == "exec":
        res = call("POST", "/exec",
                   {"command": " ".join(args.command), "timeout": args.timeout})
        sys.stdout.write(res.get("stdout", ""))
        if res.get("stderr"):
            sys.stderr.write(res["stderr"])
        print(f"[exit_code={res.get('exit_code')} timed_out={res.get('timed_out')}]",
              file=sys.stderr)
        return res.get("exit_code") or 0
    elif args.cmd == "watch-open":
        body = {"scale": args.scale, "fps": args.fps,
                "record": args.record, "idle_timeout_s": args.idle_timeout}
        if args.window:
            body["window"] = args.window
        print(json.dumps(call("POST", "/watch/open", body), indent=2))
    elif args.cmd == "watch-close":
        print(json.dumps(call("POST", "/watch/close", {})))
    elif args.cmd == "watch-record":
        print(json.dumps(call("POST", "/watch/record",
                              {"on": args.onoff == "on"})))
    elif args.cmd == "watch-frame":
        data = call("GET", "/watch/frame", raw=True)
        with open(args.file, "wb") as f:
            f.write(data)
        print(f"watch frame: {len(data)} bytes -> {args.file}")
    elif args.cmd == "watch":
        st = call("GET", "/status")
        opened = False
        if not st.get("watch", {}).get("active"):
            body = {"scale": args.scale, "fps": args.fps,
                    "record": args.record}
            if args.window:
                body["window"] = args.window
            print(json.dumps(call("POST", "/watch/open", body)))
            opened = True
        os.makedirs(args.out, exist_ok=True)
        ensure_tunnel()
        req = urllib.request.Request(
            BASE + "/watch/stream", headers={"X-Token": token()})
        resp = urllib.request.urlopen(req, timeout=args.seconds + 30)
        buf = bytearray()
        saved = 0
        last_save = 0.0
        start = time.time()
        try:
            while time.time() - start < args.seconds:
                chunk = resp.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s < 0:
                        buf.clear()
                        break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e < 0:
                        if s:
                            del buf[:s]
                        break
                    frame = bytes(buf[s:e + 2])
                    del buf[:e + 2]
                    now = time.time()
                    if now - last_save >= args.interval:
                        saved += 1
                        last_save = now
                        with open(os.path.join(
                                args.out, f"frame-{saved:04d}.jpg"),
                                "wb") as f:
                            f.write(frame)
        finally:
            resp.close()
        print(f"watch: saved {saved} frames to {args.out}")
        if opened and not args.keep_open:
            print(json.dumps(call("POST", "/watch/close", {})))
    elif args.cmd == "chat-inbox":
        print(json.dumps(call("GET", f"/chat/inbox?after={args.after}"), indent=2))
    elif args.cmd == "chat-outbox":
        print(json.dumps(call("GET", f"/chat/outbox?after={args.after}"), indent=2))
    elif args.cmd == "chat-send":
        print(json.dumps(call("POST", "/chat/send", {"text": args.text})))
    elif args.cmd == "chat-reply":
        print(json.dumps(call("POST", "/chat/reply", {"text": args.text})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
