#!/usr/bin/env bash
###############################################################################
# FinalZoom — One-Shot VPS Deployer  (Ubuntu 22.04 / 20.04, root)
#
# Yeh script:
#   1. Purana deploy clean karta hai (nginx vhost, systemd unit, pm2, docker)
#   2. MongoDB 7 + Redis 7 + Nginx + Node 20 + Python 3.11 install karta hai
#   3. /opt/finalzoom mein app deploy karta hai (backend + frontend + worker)
#   4. Frontend build karta hai
#   5. systemd se backend (uvicorn + 2 workers) chalata hai
#   6. Nginx reverse proxy + Let's Encrypt SSL (certbot) lagata hai
#
# USAGE (VPS pe root se):
#   export DOMAIN="yourdomain.com"
#   export EMAIL="you@example.com"
#   export ADMIN_EMAIL="admin@yourdomain.com"
#   export ADMIN_PASSWORD="StrongPassword123!"
#   export REPO_TARBALL_URL=""   # OPTIONAL: agar zip URL se kheechna hai
#   bash deploy.sh
#
# Agar source code (finalzoom-main folder) already /root ya /tmp mein hai,
# bas us folder ke ANDAR jaake `bash deploy/deploy.sh` chala do — script
# automatically pick kar legi.
###############################################################################
set -euo pipefail

# ---------- 0. Config ----------------------------------------------------------
APP_NAME="finalzoom"
APP_DIR="/opt/${APP_NAME}"
APP_USER="finalzoom"
DB_NAME="finalzoom"
BACKEND_PORT="8001"

: "${DOMAIN:?Set DOMAIN env (e.g. export DOMAIN=example.com)}"
: "${EMAIL:?Set EMAIL env for Let's Encrypt (e.g. export EMAIL=you@example.com)}"
: "${ADMIN_EMAIL:?Set ADMIN_EMAIL env}"
: "${ADMIN_PASSWORD:?Set ADMIN_PASSWORD env}"
ADMIN_NAME="${ADMIN_NAME:-Admin}"

# Generate strong secret if not provided
JWT_SECRET="${JWT_SECRET:-$(openssl rand -hex 48)}"

echo "==================================================================="
echo " FinalZoom Deployer"
echo " Domain      : https://${DOMAIN}"
echo " App dir     : ${APP_DIR}"
echo " Admin email : ${ADMIN_EMAIL}"
echo "==================================================================="
sleep 2

# ---------- 1. Locate source ---------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(dirname "${SCRIPT_DIR}")"   # parent of deploy/

if [[ ! -d "${SRC_DIR}/backend" || ! -d "${SRC_DIR}/frontend" ]]; then
  if [[ -n "${REPO_TARBALL_URL:-}" ]]; then
    echo ">> Downloading source from ${REPO_TARBALL_URL}"
    TMP=$(mktemp -d)
    curl -fsSL "${REPO_TARBALL_URL}" -o "${TMP}/src.zip"
    apt-get update -y && apt-get install -y unzip
    unzip -q "${TMP}/src.zip" -d "${TMP}"
    SRC_DIR="$(find "${TMP}" -maxdepth 2 -type d -name backend -printf '%h\n' | head -1)"
    [[ -z "${SRC_DIR}" ]] && { echo "!! backend folder not found in tarball"; exit 1; }
  else
    echo "!! Source not found. Run script from inside finalzoom-main/ OR set REPO_TARBALL_URL."
    exit 1
  fi
fi
echo ">> Source: ${SRC_DIR}"

# ---------- 2. Clean previous deployment ---------------------------------------
echo ">> Cleaning previous deployment artifacts..."
systemctl stop "${APP_NAME}-backend" 2>/dev/null || true
systemctl disable "${APP_NAME}-backend" 2>/dev/null || true
rm -f "/etc/systemd/system/${APP_NAME}-backend.service"

# pm2
if command -v pm2 >/dev/null 2>&1; then
  pm2 delete all 2>/dev/null || true
  pm2 kill 2>/dev/null || true
fi

# docker (sirf agar related containers hain)
if command -v docker >/dev/null 2>&1; then
  docker ps -aq --filter "name=${APP_NAME}" | xargs -r docker rm -f || true
fi

# nginx vhosts
rm -f /etc/nginx/sites-enabled/default
rm -f "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"

# free port 80/443/8001
for p in 80 443 8001; do
  fuser -k "${p}/tcp" 2>/dev/null || true
done

systemctl daemon-reload || true
echo ">> Cleanup done."

# ---------- 3. System packages -------------------------------------------------
echo ">> Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y
apt-get install -y \
  curl wget git unzip ca-certificates gnupg lsb-release ufw \
  build-essential pkg-config \
  software-properties-common apt-transport-https \
  nginx certbot python3-certbot-nginx \
  redis-server \
  python3.11 python3.11-venv python3.11-dev python3-pip || \
apt-get install -y python3 python3-venv python3-dev python3-pip

# Python 3.11 fallback
if ! command -v python3.11 >/dev/null 2>&1; then
  add-apt-repository -y ppa:deadsnakes/ppa || true
  apt-get update -y
  apt-get install -y python3.11 python3.11-venv python3.11-dev || true
fi
PYBIN="$(command -v python3.11 || command -v python3)"
echo ">> Using ${PYBIN}"

# Node 20 (for frontend build)
if ! command -v node >/dev/null 2>&1 || [[ "$(node -v | cut -d. -f1)" != "v20" && "$(node -v | cut -d. -f1)" != "v22" ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi
npm install -g yarn@1.22.22 --silent

# MongoDB 7
if ! command -v mongod >/dev/null 2>&1; then
  echo ">> Installing MongoDB 7..."
  curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
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
fi

systemctl enable --now mongod
systemctl enable --now redis-server
systemctl enable --now nginx
echo ">> System packages ready."

# ---------- 4. App user --------------------------------------------------------
id -u "${APP_USER}" >/dev/null 2>&1 || useradd -m -s /bin/bash "${APP_USER}"

# ---------- 5. Copy source -----------------------------------------------------
echo ">> Copying source to ${APP_DIR}..."
mkdir -p "${APP_DIR}"
rsync -a --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '__pycache__' \
  --exclude 'frontend/build' --exclude '.venv' \
  "${SRC_DIR}/" "${APP_DIR}/"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# ---------- 6. Backend env -----------------------------------------------------
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

# ---------- 7. Python venv + deps ----------------------------------------------
echo ">> Setting up Python venv..."
sudo -u "${APP_USER}" "${PYBIN}" -m venv "${APP_DIR}/.venv"
sudo -u "${APP_USER}" bash -c "
  source '${APP_DIR}/.venv/bin/activate' && \
  pip install --upgrade pip wheel setuptools && \
  pip install -r '${APP_DIR}/backend/requirements.txt'
"

# ---------- 8. Frontend build --------------------------------------------------
echo ">> Building frontend..."
cat > "${APP_DIR}/frontend/.env" <<EOF
REACT_APP_BACKEND_URL=https://${DOMAIN}
WDS_SOCKET_PORT=0
EOF
chown "${APP_USER}:${APP_USER}" "${APP_DIR}/frontend/.env"

sudo -u "${APP_USER}" bash -c "
  cd '${APP_DIR}/frontend' && \
  yarn install --frozen-lockfile --network-timeout 300000 || yarn install --network-timeout 300000
"
sudo -u "${APP_USER}" bash -c "
  cd '${APP_DIR}/frontend' && \
  CI=false NODE_OPTIONS=--max-old-space-size=2048 yarn build
"

# ---------- 9. systemd unit for backend ----------------------------------------
echo ">> Creating systemd unit..."
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
ExecStart=${APP_DIR}/.venv/bin/uvicorn server:app \\
  --host 127.0.0.1 \\
  --port ${BACKEND_PORT} \\
  --workers 2 \\
  --proxy-headers \\
  --forwarded-allow-ips='*'
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

# ---------- 10. Nginx vhost ----------------------------------------------------
echo ">> Configuring Nginx..."
cat > "/etc/nginx/sites-available/${APP_NAME}" <<EOF
# HTTP -> certbot will rewrite to 301 https
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} www.${DOMAIN};

    client_max_body_size 50M;

    # Frontend (React build)
    root ${APP_DIR}/frontend/build;
    index index.html;

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    # Backend API
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

    # gzip
    gzip on;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;
}
EOF
ln -sf "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"
nginx -t
systemctl reload nginx

# ---------- 11. UFW firewall ---------------------------------------------------
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH || true
  ufw allow 'Nginx Full' || true
  yes | ufw enable || true
fi

# ---------- 12. SSL via certbot ------------------------------------------------
echo ">> Requesting Let's Encrypt SSL for ${DOMAIN}..."
certbot --nginx \
  --non-interactive --agree-tos \
  --email "${EMAIL}" \
  -d "${DOMAIN}" -d "www.${DOMAIN}" \
  --redirect || \
certbot --nginx --non-interactive --agree-tos --email "${EMAIL}" -d "${DOMAIN}" --redirect

systemctl reload nginx

# ---------- 13. Health check ---------------------------------------------------
sleep 5
echo "==================================================================="
echo " Service status:"
systemctl --no-pager --lines=0 status "${APP_NAME}-backend" || true
echo "-------------------------------------------------------------------"
echo " Health probes:"
curl -sS -o /dev/null -w "  Backend  (127.0.0.1:${BACKEND_PORT})  -> HTTP %{http_code}\n" "http://127.0.0.1:${BACKEND_PORT}/api/" || true
curl -sS -o /dev/null -w "  HTTPS    (https://${DOMAIN})       -> HTTP %{http_code}\n" "https://${DOMAIN}/" || true
echo "==================================================================="
echo
echo " DEPLOY COMPLETE"
echo
echo "   Dashboard : https://${DOMAIN}"
echo "   Admin     : ${ADMIN_EMAIL} / (password you set)"
echo
echo " Logs:"
echo "   sudo journalctl -u ${APP_NAME}-backend -f"
echo "   sudo tail -f /var/log/${APP_NAME}-backend.err.log"
echo "   sudo tail -f /var/log/nginx/access.log"
echo
echo " Restart backend after code changes:"
echo "   sudo systemctl restart ${APP_NAME}-backend"
echo
echo " Rebuild frontend after code changes:"
echo "   cd ${APP_DIR}/frontend && sudo -u ${APP_USER} yarn build && sudo systemctl reload nginx"
echo "==================================================================="
