#!/usr/bin/env bash
# =============================================================================
# Zoom Worker Migration: any old version → v8.3.2 (admin-cap + tap-and-join)
# Safe to run multiple times. Preserves .env / WORKER_TOKEN. No data loss.
#
# Usage on your existing VPS:
#   curl -fsSL https://rdp-pool-manager.preview.emergentagent.com/api/worker/migrate.sh -o migrate.sh
#   chmod +x migrate.sh && sudo bash migrate.sh
#
# Or pass a custom install dir:
#   sudo bash migrate.sh /opt/zoom-worker
# =============================================================================
set -e

INSTALL_DIR="${1:-/opt/zoom-worker}"
DASHBOARD_URL="${DASHBOARD_URL:-https://rdp-pool-manager.preview.emergentagent.com}"
WORKER_PY_URL="${DASHBOARD_URL}/api/worker/zoom_worker_pool.py"
REQS_URL="${DASHBOARD_URL}/api/worker/requirements.txt"
SERVICE_NAME="zoom-worker"

bold(){ echo -e "\033[1m$1\033[0m"; }
ok(){   echo -e "\033[32m✓\033[0m $1"; }
warn(){ echo -e "\033[33m⚠\033[0m $1"; }
err(){  echo -e "\033[31m✗\033[0m $1"; }

bold "==> Zoom Worker Migration to v8.3.2"
echo "    install dir : $INSTALL_DIR"
echo "    dashboard   : $DASHBOARD_URL"
echo

# ----------------------------------------------------------- 1. STOP old worker
bold "[1/8] Stopping any existing worker process…"

# systemd
if systemctl list-units --full -all 2>/dev/null | grep -Fq "${SERVICE_NAME}.service"; then
  systemctl stop ${SERVICE_NAME} 2>/dev/null || true
  ok  "stopped systemd service: ${SERVICE_NAME}"
fi
# pm2
if command -v pm2 >/dev/null 2>&1; then
  pm2 stop zoom-worker 2>/dev/null || true
  pm2 stop worker 2>/dev/null || true
  pm2 delete zoom-worker 2>/dev/null || true
fi
# pkill any leftover python processes running the worker
pkill -f 'zoom_worker_pool.py' 2>/dev/null || true
pkill -f 'zoom_worker.py'      2>/dev/null || true
sleep 2
# Kill orphan chromium too (frees RAM before new bootstrap)
pkill -f 'chromium' 2>/dev/null || true
pkill -f 'chrome'   2>/dev/null || true
sleep 1
ok "all old worker / chromium processes killed"

# ----------------------------------------------------------- 2. BACKUP old env
bold "[2/8] Backing up existing .env (preserves WORKER_TOKEN)…"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

OLD_ENV=""
if [ -f .env ]; then
  cp .env .env.backup.$(date +%Y%m%d-%H%M%S)
  OLD_ENV=".env"
  ok "backed up existing .env"
elif [ -f ../.env ]; then
  cp ../.env .env
  OLD_ENV=".env"
  warn "found .env in parent dir, copied it here"
fi

# Extract existing values from old .env if present
OLD_TOKEN=""
OLD_DASHBOARD=""
if [ -n "$OLD_ENV" ] && [ -f "$OLD_ENV" ]; then
  OLD_TOKEN=$(grep -E '^WORKER_TOKEN=' "$OLD_ENV" | head -1 | cut -d= -f2- | tr -d '"' || true)
  OLD_DASHBOARD=$(grep -E '^DASHBOARD_URL=' "$OLD_ENV" | head -1 | cut -d= -f2- | tr -d '"' || true)
  [ -n "$OLD_TOKEN" ] && ok "preserved existing WORKER_TOKEN"
fi

# ----------------------------------------------------------- 3. SYSTEM PACKAGES
bold "[3/8] Installing OS dependencies (apt)…"
if command -v apt >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt update -qq
  apt install -y -qq \
    python3 python3-pip python3-venv \
    xvfb curl wget \
    libnss3 libxss1 libasound2 libatk-bridge2.0-0 libgtk-3-0 libgbm1 \
    libxshmfence1 fonts-liberation libu2f-udev libdrm2 libxcomposite1 libxdamage1 \
    libxrandr2 libxkbcommon0 libpango-1.0-0 libcairo2 libatk1.0-0 libcups2 \
    >/dev/null
  ok "OS dependencies installed"
else
  warn "apt not found — skipping OS deps. Make sure you have python3, xvfb, chromium libs manually."
fi

# ----------------------------------------------------------- 4. PYTHON VENV
bold "[4/8] Setting up Python venv…"
if [ ! -d venv ]; then
  python3 -m venv venv
  ok "created venv"
else
  ok "venv already exists"
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install -q --upgrade pip

# ----------------------------------------------------------- 5. DOWNLOAD WORKER
bold "[5/8] Downloading v8.3.2 worker…"
# backup old worker
if [ -f zoom_worker_pool.py ]; then
  cp zoom_worker_pool.py zoom_worker_pool.py.backup.$(date +%Y%m%d-%H%M%S)
fi
if [ -f zoom_worker.py ]; then
  cp zoom_worker.py zoom_worker.py.backup.$(date +%Y%m%d-%H%M%S)
fi
curl -fsSL "$WORKER_PY_URL" -o zoom_worker_pool.py
ok "downloaded zoom_worker_pool.py ($(wc -l < zoom_worker_pool.py) lines)"

if curl -fsSL "$REQS_URL" -o requirements.txt 2>/dev/null; then
  ok "downloaded requirements.txt"
else
  cat > requirements.txt <<'PYREQ'
playwright>=1.40.0
requests>=2.31.0
psutil>=5.9.0
python-dotenv>=1.0.0
PYREQ
  warn "requirements.txt fetch failed, using default"
fi

# ----------------------------------------------------------- 6. PIP + PLAYWRIGHT
bold "[6/8] Installing Python libs + Playwright Chromium…"
pip install -q -r requirements.txt
playwright install --with-deps chromium 2>&1 | tail -5
ok "Python libs + Playwright Chromium installed"

# ----------------------------------------------------------- 7. .env CONFIG
bold "[7/8] Writing .env (v8.3.2 defaults)…"

# Ask for token if not preserved
if [ -z "$OLD_TOKEN" ]; then
  if [ -t 0 ]; then
    echo -n "Paste your WORKER_TOKEN (from dashboard /workers page): "
    read -r OLD_TOKEN
  else
    err "No WORKER_TOKEN found and stdin not interactive. Set WORKER_TOKEN env var or paste it in .env manually."
    exit 1
  fi
fi
[ -z "$OLD_DASHBOARD" ] && OLD_DASHBOARD="$DASHBOARD_URL"

cat > .env <<EOF
# ============================================================
#  Zoom Worker v8.3.2 — generated $(date)
#  Edit any value below, then: systemctl restart $SERVICE_NAME
# ============================================================

DASHBOARD_URL=$OLD_DASHBOARD
WORKER_TOKEN=$OLD_TOKEN

# ---- v8.3.4: Headless + offscreen + mute-on-join ----
HEADLESS=true
OFFSCREEN_WINDOW=true
JOIN_WITH_AUDIO_MUTED=true
JOIN_WITH_VIDEO_OFF=true

# ---- Browser pool sizing (tune to RAM) ----
TABS_PER_BROWSER=20

# ---- v8.1 PREWARM engine ----
PREWARM_ENABLED=true
PREWARM_BROWSERS=2
PREWARM_CONTEXTS=10
PREWARM_MIN_READY=5
PREWARM_MAX_READY=20
PREWARM_PRELOAD_URL=https://app.zoom.us/wc/join
WARMUP_INTERVAL_SEC=15
SHRINK_IDLE_SEC=120

# ---- v8.3 TAP-AND-JOIN (form-prewarm + storage_state + disk-cache) ----
PERSISTENT_CACHE=true
PERSISTENT_CACHE_DIR=/tmp/zoom-disk-cache
PERSISTENT_CACHE_SIZE_MB=256
STORAGE_STATE_PATH=/tmp/zoom-storage-state.json
STORAGE_STATE_REFRESH_HOURS=24
FORM_PREWARM_WAIT_MS=1500

# ---- v8.3.1 SMART SELECTORS + DEBUG DUMP ----
DEBUG_DUMP_DOM=true
DEBUG_DUMP_DIR=/tmp/zoom-debug

# ---- v8.3.2 ADMIN CAP enforcement ----
# Worker respects whatever capacity_max admin sets in dashboard.
# AUTO_CAPACITY=true means worker also reports its hardware-safe value;
# effective limit = min(admin_cap, hardware_safe).
AUTO_CAPACITY=true

# ---- Cleanup ----
CLEANUP_INTERVAL_SEC=300
EOF
ok ".env written"

# Wipe stale storage_state so v8.3.2 bootstrap runs fresh
rm -f /tmp/zoom-storage-state.json 2>/dev/null || true

# ----------------------------------------------------------- 8. SYSTEMD SERVICE
bold "[8/8] Installing/updating systemd service…"

# Detect display setup
if [ -z "${DISPLAY:-}" ]; then
  DISPLAY_ARG=":99"
else
  DISPLAY_ARG="$DISPLAY"
fi

cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Zoom RDP Worker (Playwright Pool v8.3.2 — admin-cap + tap-and-join)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
Environment="DISPLAY=$DISPLAY_ARG"
Environment="PYTHONUNBUFFERED=1"
ExecStartPre=/bin/bash -c 'pgrep -f "Xvfb $DISPLAY_ARG" >/dev/null || /usr/bin/Xvfb $DISPLAY_ARG -screen 0 1280x720x16 &'
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/zoom_worker_pool.py
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=append:/var/log/${SERVICE_NAME}.log
StandardError=append:/var/log/${SERVICE_NAME}.log
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME} >/dev/null 2>&1
systemctl restart ${SERVICE_NAME}
sleep 3

if systemctl is-active --quiet ${SERVICE_NAME}; then
  ok "systemd service ACTIVE"
else
  err "systemd service failed to start. Check: journalctl -u ${SERVICE_NAME} -n 50"
fi

echo
bold "==> Migration complete!"
echo
echo "  Service:  systemctl status $SERVICE_NAME"
echo "  Logs:     tail -f /var/log/${SERVICE_NAME}.log"
echo "  Restart:  systemctl restart $SERVICE_NAME"
echo "  Stop:     systemctl stop $SERVICE_NAME"
echo
echo "  Verify on dashboard:  $OLD_DASHBOARD/workers"
echo "  Expected: Pool column shows 'v8.3.2-admin-cap' + 'state 0.0h'"
echo
echo "  Debug dumps (if any join fails) → ls /tmp/zoom-debug/"
echo

# Quick health probe
echo "==> First few log lines:"
sleep 4
tail -n 25 /var/log/${SERVICE_NAME}.log 2>/dev/null || echo "(log not yet flushed — wait 10s then 'tail -f /var/log/${SERVICE_NAME}.log')"
