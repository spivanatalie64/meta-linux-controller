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
