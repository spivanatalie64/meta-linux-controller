#!/bin/sh
# enable-boot.sh -- the ONE step of the Meta Linux Controller install
# that needs Natalie herself.
#
# Why: the controller runs as a systemd --user service. A user
# service normally lives only while its user is logged in. "Linger"
# is systemd's switch for "keep this user's services running from
# boot, logged in or not". Turning it on needs administrator rights
# once; nothing else in this project does.
#
# With linger on, the controller starts at boot in headless mode:
# status, exec and the chat queue answer immediately, and the eyes
# and hands attach on their own when the desktop logs in.
#
# Run it yourself in a terminal:
#     ./enable-boot.sh
# It will ask for YOUR password (sudo). Muse never sees it.
set -e
echo "This turns on systemd linger for user 'natalie' so the Meta"
echo "Linux Controller starts at boot, before anyone logs in."
echo "It runs:  loginctl enable-linger natalie"
echo "(You may be asked for your password. That is expected.)"
echo
loginctl enable-linger natalie
echo
echo "Linger is now: $(loginctl show-user natalie -p Linger --value)"
echo "Done. The controller will be up from the next boot onward."
