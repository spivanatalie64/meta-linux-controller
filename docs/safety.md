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
  without the token. The daemon refuses to start with no token.
- **Bind scope.** 127.0.0.1 and the Tailscale address only. Never
  0.0.0.0, never the LAN, never the public internet.
- **Exec timeouts are mandatory.** Default 30 s, hard cap 120 s; a
  runaway command's whole process group is killed, not left loose.
- **No credential storage.** The daemon never stores credentials of
  Natalie's. Its own token is the only secret it holds.
- **Quiet logs.** The daemon logs method, path, and status only --
  never the token, never headers, never request bodies (typed text
  and commands are not written to the log).
