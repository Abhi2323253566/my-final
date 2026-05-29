#!/usr/bin/env bash
###############################################################################
# FinalZoom — Clean Slate Script
# Use this BEFORE deploy.sh if your VPS already has random old stuff.
# It removes nginx, docker containers, pm2, old systemd units, old app dirs.
# DOES NOT touch MongoDB data (taaki agar dobara use karna ho to bachi rahe).
###############################################################################
set -uo pipefail

echo ">> Stopping known services..."
systemctl stop finalzoom-backend nginx apache2 2>/dev/null || true
systemctl disable finalzoom-backend apache2 2>/dev/null || true

# pm2 (Node)
if command -v pm2 >/dev/null 2>&1; then
  pm2 delete all 2>/dev/null || true
  pm2 kill   2>/dev/null || true
  npm uninstall -g pm2 2>/dev/null || true
fi

# docker
if command -v docker >/dev/null 2>&1; then
  echo ">> Stopping & removing all docker containers..."
  docker ps -aq | xargs -r docker rm -f || true
  docker images -q | xargs -r docker rmi -f || true
  systemctl stop docker docker.socket 2>/dev/null || true
fi

# nginx vhosts (default + custom)
echo ">> Cleaning nginx vhosts..."
rm -f /etc/nginx/sites-enabled/* 2>/dev/null || true

# Apache (if installed by mistake)
apt-get remove -y --purge apache2 apache2-utils apache2-bin 2>/dev/null || true

# Old systemd units that look like apps
for unit in /etc/systemd/system/*.service; do
  case "$(basename "$unit")" in
    finalzoom-backend.service|nginx.service|mongod.service|redis-server.service|ssh.service|systemd-*) ;;
    *)
      # Show, don't auto-delete random units. Sirf finalzoom related.
      if grep -q -iE "zoom|finalzoom" "$unit" 2>/dev/null; then
        echo "   removing $unit"
        systemctl stop "$(basename "$unit")" 2>/dev/null || true
        systemctl disable "$(basename "$unit")" 2>/dev/null || true
        rm -f "$unit"
      fi
      ;;
  esac
done
systemctl daemon-reload || true

# Free ports
for p in 80 443 8000 8001 3000 5000; do
  fuser -k "${p}/tcp" 2>/dev/null || true
done

# Old app dirs (sirf agar finalzoom/zoom related ho)
for d in /opt/finalzoom /opt/zoom /var/www/finalzoom /var/www/zoom /srv/finalzoom /root/finalzoom; do
  [[ -d "$d" ]] && { echo "   wiping $d"; rm -rf "$d"; }
done

# Re-enable nginx default later via deploy.sh
echo ">> Clean slate done. Ab deploy.sh chala."
