# Meta Linux Controller -- architecture

v1, decided and built 2026-11-25; the watch layer (#17) added the
same day on the v1.1 branch. This document records the decisions;
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
| watch open | `POST /watch/open` | `{window?, scale, fps, record, idle_timeout_s}` -- start a watch session (#17) |
| watch close | `POST /watch/close` | end the session; the open recording segment is finalized |
| watch record | `POST /watch/record` | `{on}` -- toggle the rolling recording on the live capture |
| watch frame | `GET /watch/frame` | the latest frame as a JPEG |
| watch stream | `GET /watch/stream` | MJPEG multipart stream of frames as they are captured |
| type in window | `POST /type_in_window` | `{window_id, text, press_enter?}` -- focus by id, verify, type (#14) |
| lock / unlock | `POST /lock`, `POST /unlock` | freeze the hands + exec until unlocked (#12) |
| rotate tokens | `POST /tokens/rotate` | replace both tokens; new values returned once (#11) |
| audit | `GET /audit?limit=N` | recent audit-log entries (#16) |

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

## Live eyes -- the watch layer (#17)

Natalie's design: "a layer over the XServer with a video recorder so
you can watch in real time. Think like how you do with chrome." The
controller's stills (one `import` per call) stay as the lightweight
fallback; the watch layer is for *watching* -- seeing a hands action
land, the way a page change is seen in the browser.

- **Component:** `watch/mlc_watch.py`, imported by the daemon (the
  daemon runs fine without it; `/watch/*` then says
  `watch_unavailable`). One active session at a time.
- **Capture:** one ffmpeg `x11grab` process per session (XCB capture,
  MIT-SHM on this X server), full root window or one window
  (`-window_id`). Session parameters: `scale` (default 0.5 -- the
  desktop is 3840x1080 across two monitors, so the default feed is
  1920x540) and `fps` (default 10). The root size comes from
  `xrandr`'s current size: `xdotool getdisplaygeometry` reports only
  one screen (1920x1080) on this dual-monitor XLibre setup.
- **Frames out:** ffmpeg encodes MJPEG to stdout; a reader thread in
  the manager splits frames and keeps the latest. `/watch/frame`
  serves that JPEG; `/watch/stream` serves the same frames as a
  multipart MJPEG stream. Token auth and bind scope are the
  controller's, unchanged.
- **Recorder:** with `record` on, the same ffmpeg writes a second
  output: fragmented-MP4 segments (`-segment_time 60`) under
  `~/meta-linux-controller/watch/recordings/`, pruned to the last 30
  minutes by a janitor thread. Fragmented MP4 matters: a plain MP4
  segment that is still open when a session ends loses its moov and
  the whole segment. Just as load-bearing: on close, ffmpeg is
  signalled *while its stdout reader is still draining* -- stopping
  the drain first wedges ffmpeg on the full pipe and even SIGKILL
  then leaves an unplayable segment (both failure modes were hit and
  fixed during the build; a closed 8-second session now leaves a
  valid 8.6-second segment).
- **Guardrails:** capture runs only while a session is open. A
  session with no stream consumers and no frame pulls auto-stops
  after its idle timeout (default 120 s; the reason is kept in
  `/status` as `watch.last_stop`). Recording that was explicitly
  switched on counts as a standing instruction and is exempt from
  idle auto-stop. Nothing records when no session is open and record
  mode was not explicitly enabled.
- **Client:** `watch-open / watch-close / watch-record /
  watch-frame`, and `watch`, which opens a session if needed, pulls
  the stream, saves frames to a directory, and closes what it opened.
- **Measured on her machine** (i5-10210U, 8 threads), default
  settings (10 fps, half scale): the capture ffmpeg uses ~25% of one
  core with recording off, ~43% with it on (~3% / ~5% of the whole
  machine). Action-to-visible latency, measured from the VM by
  polling `/watch/frame` after sending Enter to a terminal: ~1.2 s,
  dominated by the per-request frame-fetch cadence; the stream
  itself delivers continuously at the session frame rate.

## v1.1 -- service and hardening (#10-#16)

- **Headless-first (#10).** The daemon no longer presumes a
  display. A watcher thread probes the X socket for its `$DISPLAY`
  target every 3 s. With no display: `/status`, `/exec` and the chat
  queue answer normally; eyes/hands return
  `{"ok": false, "error": "no_display"}` (HTTP 503). When the socket
  appears the daemon attaches; when it vanishes it detaches. The
  systemd --user unit stays as installed; `enable-boot.sh` is the
  one shipped step that needs Natalie's sudo (`loginctl
  enable-linger natalie`) to make the user service start at boot. A
  system-unit variant ships in the repo marked *not installed*.
- **Token scopes (#11).** `token.txt` (full) and `read-token.txt`
  (read), both mode 600 on the PC; the VM client mirrors the pair.
  Read covers status, screenshot, tree, windows, chat GETs, audit
  GET, and the watch frame/stream; full covers everything. Wrong
  scope: 403 `insufficient_scope` naming the needed scope.
  `POST /tokens/rotate` (full only) replaces both at once -- old
  values fail immediately -- and returns the new pair exactly once.
- **Guardrails (#12).** The exec denylist lives in one commented
  block in the daemon (Natalie's hard lines; see `docs/safety.md`)
  and refuses with `confirmation_required` + the rule name unless
  the request carries `"confirm": true`. The lock (`/lock`,
  `/unlock`, full scope) freezes hands + exec behind
  `{"ok": false, "error": "locked"}` and persists in `lock.json`
  across restarts; `/status` reports it.
- **Transport failover (#13, client-side).** The VM client tries
  the SSH port-forward first, then an SSH-exec'd curl on the PC's
  loopback (token read on the PC, never in transit on the VM side).
  `status` prints which transport answered. A daemon refusal is an
  answer and is never retried on the other road.
- **Window eyes + atomic typing (#14).** `/screenshot` takes a
  `window` id (`import -window <id>`). `/type_in_window` focuses by
  id, polls until the focused id matches (mismatch is a clean 409,
  never a silent wrong-window type -- v1's focus bug made this a
  hard requirement), then types.
- **Chat receipts (#15).** Inbox messages carry `seen`. Only a
  full-token `GET /chat/inbox` marks them (that is Muse reading;
  the VM watcher hook polls exactly that way, reading the PC's
  token file each cycle, so rotation does not break it). Read-token
  fetches never mark. `/status` reports
  `chat_last_full_inbox_poll_s_ago`; muse-chat polls with the read
  token, ticks her seen lines with a ✓, and shows the heartbeat in
  its header. The inbox response shape is v1's plus the `seen`
  field -- nothing removed, nothing renamed.
- **Audit log (#16).** Append-only JSONL at
  `~/meta-linux-controller/audit.log`: `{ts, verb, scope, summary,
  result}` per call, exec outputs hashed (SHA-256, first 12) and
  never stored, refusals (401/403/denylist/locked/no_display)
  logged like everything else, rotation at ~5 MB keeping one old
  file, `GET /audit?limit=N` to read it back.

## Client shape

A small CLI on the Muse VM wraps the verbs:
`ping / status / windows / tree / screenshot FILE / click / type /
key / scroll / launch / focus / exec`. It reads the token from the
token file and speaks to the local end of the SSH forward.
