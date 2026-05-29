#!/usr/bin/env bash
###############################################################################
# FinalZoom — GitHub One-Shot Installer
# Repo: https://github.com/tradeg847-stack/finalzoom.git
#
# - Purana sab band (pm2, docker, apache, nginx vhosts, port 80/443/8001)
# - Fresh clone + MongoDB7 + Redis + Nginx + Python3.11 + Node20
# - Backend systemd, Frontend build, Let's Encrypt SSL
#
# USAGE (VPS root):
#   DOMAIN=yourdomain.com \
#   EMAIL=you@gmail.com \
#   ADMIN_EMAIL=admin@yourdomain.com \
#   ADMIN_PASSWORD='ChangeMe!Strong#2026' \
#   bash install.sh
###############################################################################
set -euo pipefail

# ====== CONFIG ================================================================
REPO_URL="${REPO_URL:-https://github.com/tradeg847-stack/finalzoom.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"

APP_NAME="finalzoom"
APP_DIR="/opt/${APP_NAME}"
APP_USER="finalzoom"
DB_NAME="finalzoom"
BACKEND_PORT="8001"

: "${DOMAIN:?Set DOMAIN env (export DOMAIN=example.com)}"
: "${EMAIL:?Set EMAIL env (export EMAIL=you@gmail.com)}"
: "${ADMIN_EMAIL:?Set ADMIN_EMAIL env}"
: "${ADMIN_PASSWORD:?Set ADMIN_PASSWORD env}"
ADMIN_NAME="${ADMIN_NAME:-Admin}"
JWT_SECRET="${JWT_SECRET:-$(openssl rand -hex 48 2>/dev/null || head -c48 /dev/urandom | xxd -p | tr -d '\n')}"

echo "==================================================================="
echo " FinalZoom GitHub Installer"
echo " Repo    : ${REPO_URL} (${REPO_BRANCH})"
echo " Domain  : https://${DOMAIN}"
echo " AppDir  : ${APP_DIR}"
echo " Admin   : ${ADMIN_EMAIL}"
echo "==================================================================="
sleep 2

# ====== 1. STOP & CLEAN OLD ===================================================
echo ">> [1/10] Stopping & cleaning previous deployment..."
systemctl stop "${APP_NAME}-backend" nginx apache2 2>/dev/null || true
systemctl disable "${APP_NAME}-backend" apache2 2>/dev/null || true
rm -f "/etc/systemd/system/${APP_NAME}-backend.service"

# pm2
if command -v pm2 >/dev/null 2>&1; then
  pm2 delete all 2>/dev/null || true
  pm2 kill 2>/dev/null || true
fi

# docker containers (sab)
if command -v docker >/dev/null 2>&1; then
  docker ps -aq | xargs -r docker rm -f 2>/dev/null || true
fi

# Apache remove
apt-get remove -y --purge apache2 apache2-utils apache2-bin 2>/dev/null || true

# nginx vhosts (purane sab hata do — port 8000 wala bhi)
rm -f /etc/nginx/sites-enabled/* 2>/dev/null || true
rm -f /etc/nginx/sites-available/finalzoom* 2>/dev/null || true
rm -f /etc/nginx/sites-available/zoom* 2>/dev/null || true
rm -f /etc/nginx/sites-available/default 2>/dev/null || true
rm -f /etc/nginx/conf.d/*.conf 2>/dev/null || true

# Old app dirs
rm -rf /opt/finalzoom /opt/zoom /var/www/finalzoom /var/www/zoom /root/finalzoom-main 2>/dev/null || true

# Free ports
for p in 80 443 8001 3000 5000 8000; do
  fuser -k "${p}/tcp" 2>/dev/null || true
done

systemctl daemon-reload || true
sleep 1

# ====== 2. SYSTEM PACKAGES ====================================================
echo ">> [2/10] Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
  curl wget git unzip rsync ca-certificates gnupg lsb-release ufw \
  build-essential pkg-config xxd \
  software-properties-common apt-transport-https \
  nginx certbot python3-certbot-nginx \
  redis-server

# Python 3.11
if ! command -v python3.11 >/dev/null 2>&1; then
  add-apt-repository -y ppa:deadsnakes/ppa || true
  apt-get update -y
  apt-get install -y python3.11 python3.11-venv python3.11-dev || \
    apt-get install -y python3 python3-venv python3-dev
fi
PYBIN="$(command -v python3.11 || command -v python3)"
echo ">> Python: ${PYBIN}"

# Node 20
NODE_MAJOR="$(node -v 2>/dev/null | sed 's/v\([0-9]*\).*/\1/' || echo 0)"
if [[ "${NODE_MAJOR}" -lt 20 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi
npm install -g yarn@1.22.22 --silent

# ====== 3. MongoDB 7 ==========================================================
if ! command -v mongod >/dev/null 2>&1; then
  echo ">> [3/10] Installing MongoDB 7..."
  curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | gpg --yes -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
  UB_CODENAME="$(lsb_release -cs)"
  case "${UB_CODENAME}" in
    jammy|focal) MONGO_CODENAME="${UB_CODENAME}";;
    noble) MONGO_CODENAME="jammy";;
    *) MONGO_CODENAME="jammy";;
  esac
  echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] https://repo.mongodb.org/apt/ubuntu ${MONGO_CODENAME}/mongodb-org/7.0 multiverse" \
    > /etc/apt/sources.list.d/mongodb-org-7.0.list
  apt-get update -y
  apt-get install -y mongodb-org
else
  echo ">> [3/10] MongoDB already installed"
fi

systemctl enable --now mongod
systemctl enable --now redis-server
systemctl enable --now nginx

# ====== 4. APP USER ===========================================================
echo ">> [4/10] Creating app user..."
id -u "${APP_USER}" >/dev/null 2>&1 || useradd -m -s /bin/bash "${APP_USER}"

# ====== 5. CLONE REPO =========================================================
echo ">> [5/10] Cloning ${REPO_URL}..."
rm -rf "${APP_DIR}"
git clone --depth 1 --branch "${REPO_BRANCH}" "${REPO_URL}" "${APP_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# Sanity check
[[ -d "${APP_DIR}/backend" ]] || { echo "!! backend/ missing in repo"; exit 1; }
[[ -d "${APP_DIR}/frontend" ]] || { echo "!! frontend/ missing in repo"; exit 1; }

# ====== 6. BACKEND .ENV + VENV + DEPS =========================================
echo ">> [6/10] Backend env + Python deps..."
cat > "${APP_DIR}/backend/.env" <<EOF
MONGO_URL=mongodb://127.0.0.1:27017
DB_NAME=${DB_NAME}
JWT_SECRET=${JWT_SECRET}
ADMIN_EMAIL=${ADMIN_EMAIL}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
ADMIN_NAME=${ADMIN_NAME}
REDIS_URL=redis://127.0.0.1:6379/0
CORS_ORIGINS=https://${DOMAIN}
USAGE_LIMIT=15000
DISTRIBUTION_MODE=greedy
HEALTH_STALE_SECONDS=45
EOF
chmod 600 "${APP_DIR}/backend/.env"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}/backend/.env"

# Strip Emergent-internal packages (not on public PyPI)
sed -i '/^emergentintegrations/d' "${APP_DIR}/backend/requirements.txt"

sudo -u "${APP_USER}" "${PYBIN}" -m venv "${APP_DIR}/.venv"
sudo -u "${APP_USER}" bash -c "
  source '${APP_DIR}/.venv/bin/activate' && \
  pip install --upgrade pip wheel setuptools && \
  pip install -r '${APP_DIR}/backend/requirements.txt'
" || { echo '!! Python deps install FAILED'; exit 1; }

# Optional: try Emergent's private index (won't fail script if unavailable)
sudo -u "${APP_USER}" bash -c "
  source '${APP_DIR}/.venv/bin/activate' && \
  pip install emergentintegrations --extra-index-url https://d33sy5i8bnduwe.cloudfront.net/simple/ 2>/dev/null
" || echo ">> (info) emergentintegrations skipped — not required for this app"

# ====== 7. FRONTEND BUILD =====================================================
echo ">> [7/10] Building frontend..."
cat > "${APP_DIR}/frontend/.env" <<EOF
REACT_APP_BACKEND_URL=https://${DOMAIN}
WDS_SOCKET_PORT=0
EOF
chown "${APP_USER}:${APP_USER}" "${APP_DIR}/frontend/.env"

sudo -u "${APP_USER}" bash -c "
  cd '${APP_DIR}/frontend' && \
  yarn install --network-timeout 600000
" || { echo '!! yarn install FAILED'; exit 1; }
sudo -u "${APP_USER}" bash -c "
  cd '${APP_DIR}/frontend' && \
  CI=false NODE_OPTIONS=--max-old-space-size=2048 yarn build
" || { echo '!! yarn build FAILED'; exit 1; }

[[ -f "${APP_DIR}/frontend/build/index.html" ]] || { echo "!! Frontend build directory missing"; exit 1; }

# ====== 8. SYSTEMD UNIT =======================================================
echo ">> [8/10] Creating systemd unit..."
cat > "/etc/systemd/system/${APP_NAME}-backend.service" <<EOF
[Unit]
Description=FinalZoom FastAPI backend
After=network.target mongod.service redis-server.service
Wants=mongod.service redis-server.service

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}/backend
EnvironmentFile=${APP_DIR}/backend/.env
ExecStart=${APP_DIR}/.venv/bin/uvicorn server:app --host 127.0.0.1 --port ${BACKEND_PORT} --workers 2 --proxy-headers --forwarded-allow-ips=*
Restart=always
RestartSec=3
LimitNOFILE=65535
StandardOutput=append:/var/log/${APP_NAME}-backend.log
StandardError=append:/var/log/${APP_NAME}-backend.err.log

[Install]
WantedBy=multi-user.target
EOF

touch "/var/log/${APP_NAME}-backend.log" "/var/log/${APP_NAME}-backend.err.log"
chown "${APP_USER}:${APP_USER}" "/var/log/${APP_NAME}-backend"*.log

systemctl daemon-reload
systemctl enable "${APP_NAME}-backend"
systemctl restart "${APP_NAME}-backend"

# ====== 9. NGINX + SSL ========================================================
echo ">> [9/10] Configuring Nginx..."
cat > "/etc/nginx/sites-available/${APP_NAME}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} www.${DOMAIN};

    client_max_body_size 50M;

    root ${APP_DIR}/frontend/build;
    index index.html;

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    location /api/ {
        proxy_pass         http://127.0.0.1:${BACKEND_PORT};
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    gzip on;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;
}
EOF
ln -sf "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"
nginx -t
systemctl reload nginx

# Firewall
ufw allow OpenSSH 2>/dev/null || true
ufw allow 'Nginx Full' 2>/dev/null || true
yes | ufw enable 2>/dev/null || true

# SSL
echo ">> Requesting Let's Encrypt SSL..."
certbot --nginx --non-interactive --agree-tos --email "${EMAIL}" \
  -d "${DOMAIN}" -d "www.${DOMAIN}" --redirect 2>/dev/null || \
certbot --nginx --non-interactive --agree-tos --email "${EMAIL}" \
  -d "${DOMAIN}" --redirect || \
echo "!! SSL failed (DNS not pointing? www subdomain missing?). Retry manually:  certbot --nginx -d ${DOMAIN}"

systemctl reload nginx

# ====== 10. HEALTH CHECK ======================================================
sleep 5
echo "==================================================================="
echo " STATUS"
systemctl --no-pager --lines=0 status "${APP_NAME}-backend" || true
echo "-------------------------------------------------------------------"
BE_CODE=$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:${BACKEND_PORT}/api/" || echo "000")
HTTPS_CODE=$(curl -sS -o /dev/null -w "%{http_code}" "https://${DOMAIN}/" || echo "000")
echo "  Backend local : HTTP ${BE_CODE}"
echo "  HTTPS  domain : HTTP ${HTTPS_CODE}"

if [[ "${BE_CODE}" == "000" ]] || [[ "${BE_CODE}" == "502" ]]; then
  echo
  echo "!! Backend not responding. Last 40 error lines:"
  tail -n 40 "/var/log/${APP_NAME}-backend.err.log" 2>/dev/null || journalctl -u "${APP_NAME}-backend" --no-pager -n 40
fi
echo "==================================================================="
echo
echo " DEPLOYMENT COMPLETE"
echo "   Dashboard : https://${DOMAIN}"
echo "   Admin     : ${ADMIN_EMAIL}"
echo
echo " Common commands:"
echo "   sudo systemctl restart ${APP_NAME}-backend"
echo "   sudo journalctl -u ${APP_NAME}-backend -f"
echo "   sudo tail -f /var/log/${APP_NAME}-backend.err.log"
echo "   (code update)  cd ${APP_DIR} && sudo -u ${APP_USER} git pull && sudo systemctl restart ${APP_NAME}-backend"
echo "   (rebuild fe)   cd ${APP_DIR}/frontend && sudo -u ${APP_USER} yarn build && sudo systemctl reload nginx"
echo "==================================================================="

