# Meta Linux Controller -- architecture

v1, decided and built 2026-11-25. This document records the decisions;
the code is `daemon/meta_linux_controllerd.py`.

## What it is

One daemon on Natalie's PC, inside her graphical session, exposing one
token-authenticated HTTP API. It is the PC counterpart of the phone
adapter: eyes, hands, and a shell, behind a single surface, so Muse
drives the PC the same way it drives the phone.

## Process model

- A **systemd --user service** (`meta-linux-controller.service`),
  `WantedBy=default.target`, `Restart=on-failure`.
- The unit sets the session environment explicitly, because a user
  service does not reliably inherit the graphical session's variables:
  `DISPLAY=:0`, `XAUTHORITY=/run/user/1000/gdm/Xauthority` (her
  `~/.Xauthority` is empty; the GDM cookie is the working one),
  `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`.
- **Linger is off on this machine** (`loginctl` reports `Linger=no`),
  so the service lives inside the login session: it starts when
  Natalie logs in and stops when the last session ends. It does not
  survive a full logout/reboot unattended. The one command that fixes
  it, run by Natalie (needs her sudo): `loginctl enable-linger natalie`.
  Same known caveat as the muse-browser daemon on the server.

## The stack, chosen from what is actually on the machine

Surveyed 2026-11-25 (Python 3.14.10; the live session is X11 --
dwm on XLibre, via gdm):

- **Screenshots:** ImageMagick `import -window root` -- already proven
  on this machine for screen capture. `/screenshot` returns raw PNG
  bytes (`Content-Type: image/png`), not base64-in-JSON.
- **Input and windows:** `xdotool` is installed; it drives XTEST for
  click/type/key/scroll and provides the window list, geometry, active
  window, and focus-by-title. No python-xlib needed. (`wmctrl` and
  `scrot` are absent and were not needed.)
- **UI tree:** AT-SPI over the session accessibility bus, via GObject
  introspection (`gi.repository.Atspi` -- the Atspi-2.0 typelib ships
  with at-spi2-core, and `libatspi` is present). pyatspi itself is **not**
  used: it is not published for Python 3.14 on PyPI, and the distro
  package would need sudo, which agents do not have. Same bus, same
  data. `org.gnome.desktop.interface toolkit-accessibility` was set to
  `true` (a user-level gsettings change) so applications expose their
  trees. The tree is text-first -- role, name, position, states -- the
  same language as the phone adapter.
- **Shell:** the daemon runs commands as Natalie via `/bin/bash -lc`,
  in the session environment, with a mandatory timeout (default 30 s,
  hard cap 120 s); on timeout the whole process group is killed.

## Verbs

All endpoints require the token. JSON in/out, except `/screenshot`
(raw PNG bytes out).

| Verb | Endpoint | Notes |
| --- | --- | --- |
| ping | `GET /ping` | liveness |
| status | `GET /status` | hostname, display, tailnet address, binds, token present, uptime |
| screenshot | `POST /screenshot` | raw PNG of the whole screen |
| windows | `GET /windows` | open windows: title, geometry, which is focused |
| tree | `GET /tree` | AT-SPI tree as text + the focused window's name |
| click | `POST /click` | `{x, y, button}` |
| type | `POST /type` | `{text}` |
| key | `POST /key` | `{keys}` -- xdotool syntax, e.g. `Return`, `ctrl+l` |
| scroll | `POST /scroll` | `{x, y, dy}` -- dy>0 scrolls down |
| launch | `POST /launch` | `{command}` via shell, or `{app}` desktop id via gtk-launch |
| focus | `POST /focus` | `{title}` substring match, raises the window |
| exec | `POST /exec` | `{command, timeout}` -> `{stdout, stderr, exit_code, timed_out}` |
| chat send | `POST /chat/send` | `{text}` -- a line from Natalie, queued for Muse |
| chat inbox | `GET /chat/inbox` | Muse reads her lines (`?after=<id>` cursor) |
| chat reply | `POST /chat/reply` | `{text}` -- Muse's answer, queued for her console |
| chat outbox | `GET /chat/outbox` | her console polls for replies (`?after=<id>` cursor) |

Focus matches by plain substring over **all** windows, enumerated in
the daemon: xdotool's own search is POSIX-regex (escapes bite), and on
a tagging window manager the target may sit on another tag --
`windowactivate` switches to it. Activation is not `--sync` (dwm does
not confirm it back, and `--sync` blocks).

## Chat

`client/muse-chat` is a single-file stdlib console program on the PC
(also linked from `~/.local/bin/muse-chat`). It posts her lines to
`/chat/send` over loopback and polls `/chat/outbox` every ~2 s,
printing replies prefixed `Muse:`. The queue is two append-only
JSONL files under `~/meta-linux-controller/chat/` on the PC
(`inbox.jsonl`, `outbox.jsonl`, monotonically numbered entries).
Nothing about the chat leaves her machine except to Muse over the
tailnet path above.

## State

- Everything lives on the PC under `~/meta-linux-controller/`:
  the daemon, and `token.txt` (mode 600). Nothing stateful lives in
  the repo; the repo holds source, the unit file, and these docs.
- The token is generated on the PC. A copy lives on the Muse VM at
  `~/workspace/tools/meta-linux-controller-token.txt` (mode 600).
  The value is never printed, logged, or committed. The daemon logs
  method + path + status only -- never headers, bodies, or the token.
- The daemon refuses to start if no token exists.

## Networking -- the exact path from the Muse VM

- The daemon binds **127.0.0.1:8771 and the PC's Tailscale address
  (100.94.26.49:8771)** -- never 0.0.0.0, never the LAN, never the
  public internet. On the tailnet itself, another of Natalie's devices
  can reach it directly at the Tailscale address with the token.
- The Muse VM's direct outbound TCP to tailnet addresses is blocked
  by the VM's egress policy (`Operation not permitted`), so Muse
  reaches the daemon through an **SSH port-forward over the tailnet**,
  using the existing SSH route:

  ```
  ssh -F ~/workspace/tools/sshcfg-pc-ts -N -L 8771:127.0.0.1:8771 natalie-pc-ts
  ```

  then `http://127.0.0.1:8771` on the VM is the daemon. The VM client
  (`client/meta_linux_controller.py`, installed on the VM at
  `~/workspace/tools/meta_linux_controller.py`) starts this forward
  automatically when the local port is not answering.

## Client shape

A small CLI on the Muse VM wraps the verbs:
`ping / status / windows / tree / screenshot FILE / click / type /
key / scroll / launch / focus / exec`. It reads the token from the
token file and speaks to the local end of the SSH forward.
