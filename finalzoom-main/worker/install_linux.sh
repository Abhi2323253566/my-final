#!/usr/bin/env bash
# install_linux.sh — one-shot installer for an Ubuntu 22.04 RDP/server node
# implementing the architecture doc EXACTLY:
#   - Playwright + chromium
#   - Xvfb virtual display
#   - PM2 process manager
#   - Redis (for backend queue — optional on worker side)
#
# Run as root or with sudo on a fresh box, then drop your .env in place.
set -e

echo "==> apt update + base deps"
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv \
  xvfb x11-utils \
  redis-server \
  curl wget git build-essential \
  ca-certificates fonts-liberation \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
  libcairo2 libasound2t64 libatspi2.0-0 libgtk-3-0 libnspr4

echo "==> install Node.js LTS + PM2"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi
npm install -g pm2 || true

echo "==> python venv + playwright"
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium --with-deps

echo "==> create logs/"
mkdir -p logs

chmod +x start_xvfb.sh || true

echo ""
echo "✓ install complete."
echo "Next steps:"
echo "  1. Drop your .env in $(pwd)/.env  (download from dashboard 'Add Worker' button)"
echo "  2. Test foreground:    source .venv/bin/activate && source ./start_xvfb.sh && python zoom_worker_pool.py"
echo "  3. Production:         pm2 start ecosystem.config.js && pm2 save"
echo "  4. Reboot persistence: pm2 startup  (run the printed command)"
