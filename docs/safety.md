# Consent and safety rules

These rules govern Meta Linux Controller. They are written here, and
the ones the daemon can enforce mechanically are enforced in the code.

## Looking is free

Muse may, without asking first: take screenshots, read the UI tree
and window list, check status, and read files. Seeing the machine is
never the sensitive step.

## Ask first

Muse asks Natalie before anything that acts in the world as her or
cannot be undone:

- sending or posting anything as Natalie (messages, email, posts)
- purchases and payments
- deletions
- account changes
- anything irreversible

The end-to-end smoke test is the one pre-agreed exception in scope:
opening one new terminal and typing one harmless `echo` line into it.

## The kill switch is Natalie's

Stopping the service (`systemctl --user stop meta-linux-controller`)
or deleting the token file ends Muse's access immediately, no
questions, no re-enabling from Muse's side. The token copy on the
Muse VM is worthless the moment the PC's token changes.

## Hard lines in the code

- **Token required.** Every endpoint, including `/ping`, answers 401
  without a token.
- **Two token scopes (v1.1, #11).** The *read* token can look:
  status, screenshots, tree, windows, the chat queues, the audit
  log. The *full* token can act, and can do everything read can.
  A read token reaching for an act verb gets a 403 that names the
  scope it needs. Tokens rotate on demand (`POST /tokens/rotate`,
  full token only): both values are replaced at once, the old ones
  die immediately, and the new values are returned exactly once so
  the client can rewrite its files. Token values are never written
  to any log.
- **Bind scope.** 127.0.0.1 and the Tailscale address only. Never
  0.0.0.0, never the LAN, never the public internet.
- **Exec denylist (v1.1, #12).** Natalie's hard lines are refused
  in code unless the request explicitly confirms them: recursive
  `rm` of `/`, `/home`, `~` or system directories; `mkfs`; `dd`
  onto a raw disk device; `shutdown` / `reboot` / `poweroff` /
  `halt` (including via `systemctl`); fork bombs. A refusal names
  the rule it hit.
- **The hands lock (v1.1, #12).** `POST /lock` freezes every hands
  verb and exec behind `{"ok": false, "error": "locked"}` until
  `POST /unlock`. Looking still works while locked, and the lock
  survives a daemon restart -- it is a state Natalie (or Muse, on
  her word) sets, not a session flag.
- **Audit log (v1.1, #16).** Every call appends one line to
  `~/meta-linux-controller/audit.log`: time, verb, which token
  scope was used, a short summary (for exec, the command truncated
  to 200 characters), and the result -- refusals included. Command
  *outputs* are never stored, only a truncated SHA-256 of them.
  Token values never appear. The log rotates at ~5 MB, keeping one
  old file. (The systemd journal stays quiet as before; the audit
  log is the deliberate, local record.)
- **No credential storage.** The daemon never stores credentials of
  Natalie's. Its own tokens are the only secrets it holds.
