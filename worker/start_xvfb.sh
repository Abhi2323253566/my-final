#!/usr/bin/env bash
# start_xvfb.sh — boot a virtual X display so chromium can run on a headless
# Ubuntu / Debian RDP node WITHOUT consuming any GPU or window-manager RAM.
# This is mentioned in the architecture doc:  "XVFB instead of full GUI".
#
# Usage:
#   sudo apt update && sudo apt install -y xvfb x11-utils
#   ./start_xvfb.sh                # starts on :99 in background, exports DISPLAY
#   source ./start_xvfb.sh         # same, but exports into current shell
#
# Or call from systemd / PM2 (see ecosystem.config.js).
set -e

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1280x720x16}"
XVFB_LOCKFILE="/tmp/.X${DISPLAY_NUM}-lock"

# Already running?
if [ -e "$XVFB_LOCKFILE" ] && pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1; then
  echo "Xvfb already running on :${DISPLAY_NUM}"
else
  echo "starting Xvfb :${DISPLAY_NUM} ${SCREEN_GEOMETRY} …"
  Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp -nolisten unix \
      >/tmp/xvfb.log 2>&1 &
  sleep 1
fi

export DISPLAY=":${DISPLAY_NUM}"
echo "DISPLAY=${DISPLAY}"
