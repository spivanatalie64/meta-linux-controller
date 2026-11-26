# meta-linux-controller

> **Personal project -- built only for Natalie Cole-Clift. Not for distribution.**
> This exists because of Google Mobile Services (GMS) tracking: it is part of getting one person's digital life onto her own machines, under her own control. It is not a product, and it is not made for anyone else.
> The repository is left public, effectively as a template: if you want a starting point like this for your own setup, you are welcome to use it as one.

A small control daemon for my Linux PC that lets Muse actually work on my computer: see the screen, read the UI, click, type, launch apps, and run commands -- through a token-authenticated API over my Tailscale network, and nothing else.

It is the PC counterpart of the adapter on my phone: same idea, same rules. Eyes and hands for Muse, on my machine, under my control -- I can stop it or revoke its token at any time.

## How it works

- A daemon runs in my graphical session on the PC (Cinnamon/X11) and owns the display connection.
- Eyes: screenshots, the AT-SPI accessibility tree (the UI as text), and the window list.
- Hands: click, type, key presses, scroll, app launch and focus -- plus a shell endpoint, because SSH proved the pattern and this makes it one surface.
- Every call needs the token. The daemon binds to loopback and my tailnet address only -- never the public internet.

## Status

v1 in progress. The work is filed as issues, assigned, and gathered in the v1 milestone. Apache License 2.0, matching my other repos.

## Rules it runs under

- Looking is free: screenshots, trees, window lists, status.
- Ask first: anything that sends, posts, buys, deletes, or changes accounts -- the same consent rules Muse follows everywhere else for me.
- The kill switch is mine: stop the service or delete the token and Muse's hands come off the machine.
