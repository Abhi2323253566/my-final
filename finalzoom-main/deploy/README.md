# FinalZoom — VPS Deployment Guide (Hinglish)

Yeh guide tumhare **finalzoom-main** zip ko Ubuntu 22.04 / 20.04 VPS pe SSL + domain ke saath deploy karta hai. Purana deploy clean karke fresh setup hota hai.

---

## Stack (deploy hone wala)

| Layer | What | Port |
|---|---|---|
| Reverse proxy + SSL | Nginx + certbot (Let's Encrypt) | 80, 443 |
| Frontend | React 19 build → Nginx static | — |
| Backend | FastAPI + uvicorn (2 workers) via **systemd** | 127.0.0.1:8001 |
| DB | MongoDB 7.0 | 127.0.0.1:27017 |
| Cache/locks | Redis 7 | 127.0.0.1:6379 |
| Worker scripts | Served as downloads from `/api/worker/*` (Playwright bots alag RDP VPS pe chalte hain) | — |

---

## Pre-requisites

1. **VPS**: Ubuntu 22.04 (preferred) ya 20.04, **root SSH**, kam se kam 2 vCPU / 2 GB RAM / 20 GB disk.
2. **Domain**: A record `yourdomain.com` → VPS IP. (Optional `www.yourdomain.com` bhi bana lo.)
3. DNS propagate ho jaane ka 2-5 min wait kar lo before SSL.

---

## STEP 1 — Source VPS pe le aao

VPS pe SSH karke:

```bash
# Apne local se zip upload karo (apne PC se):
#   scp finalzoom-main.zip root@VPS_IP:/root/

cd /root
apt-get update -y && apt-get install -y unzip rsync
unzip -q finalzoom-main.zip       # → /root/finalzoom-main/
cd finalzoom-main
ls deploy/                        # deploy.sh + clean.sh dikhne chahiye
```

---

## STEP 2 — (Optional) Purana deploy clean karo

```bash
sudo bash deploy/clean.sh
```

Yeh script:
- pm2, Apache, random docker containers, purane nginx vhosts hata deti hai
- Ports 80/443/8001 free karti hai
- MongoDB data ko **NAHI** chhedti (agar bachana ho)

---

## STEP 3 — Env variables set karo + deploy

```bash
export DOMAIN="yourdomain.com"
export EMAIL="you@gmail.com"                 # Let's Encrypt notifications
export ADMIN_EMAIL="admin@yourdomain.com"    # Dashboard admin login
export ADMIN_PASSWORD="ChangeMe!Strong#2026" # Strong password
# JWT_SECRET auto-generate ho jayega; explicit dena ho to: export JWT_SECRET="..."

sudo -E bash deploy/deploy.sh
```

**`-E` flag zaroori hai** taaki env variables `sudo` mein pass ho. (Agar already root ho to `sudo` chhod sakte ho.)

Yeh ek shot mein:
1. MongoDB 7 + Redis 7 + Nginx + Node 20 + Python 3.11 install karega
2. `/opt/finalzoom` pe code copy karke `finalzoom` user banayega
3. Backend ke liye Python venv + deps, `.env` file
4. Frontend `yarn install && yarn build`
5. **systemd unit** `finalzoom-backend.service` — auto-start on boot, auto-restart on crash
6. Nginx vhost `yourdomain.com` ka, `/` → React build, `/api/*` → uvicorn
7. **Let's Encrypt SSL** auto-install + HTTP→HTTPS redirect
8. UFW firewall (SSH + Nginx Full)

Run hone mein ~5-8 min.

---

## STEP 4 — Verify

```bash
# Backend health
curl -s https://yourdomain.com/api/  

# Service status
sudo systemctl status finalzoom-backend

# Logs (live)
sudo journalctl -u finalzoom-backend -f
sudo tail -f /var/log/finalzoom-backend.err.log
sudo tail -f /var/log/nginx/access.log
```

Browser mein `https://yourdomain.com` kholo → login screen aaye → admin email + password se login karo.

---

## Common Operations

### Restart backend (after .env change)
```bash
sudo systemctl restart finalzoom-backend
```

### Code update + frontend rebuild
```bash
cd /opt/finalzoom
# code update (rsync naya zip ya git pull)
sudo systemctl restart finalzoom-backend
sudo -u finalzoom bash -c "cd /opt/finalzoom/frontend && yarn build"
sudo systemctl reload nginx
```

### Change admin password
```bash
sudo nano /opt/finalzoom/backend/.env   # ADMIN_PASSWORD edit
sudo systemctl restart finalzoom-backend
```

### MongoDB shell
```bash
mongosh finalzoom
```

### Backup MongoDB
```bash
mongodump --db finalzoom --out /root/backups/$(date +%F)
```

### SSL auto-renewal check
```bash
sudo certbot renew --dry-run
```

---

## File locations

| Path | What |
|---|---|
| `/opt/finalzoom/` | App root |
| `/opt/finalzoom/backend/.env` | Backend secrets |
| `/opt/finalzoom/frontend/.env` | `REACT_APP_BACKEND_URL` |
| `/opt/finalzoom/frontend/build/` | Nginx-served static |
| `/etc/systemd/system/finalzoom-backend.service` | Backend unit |
| `/etc/nginx/sites-available/finalzoom` | Nginx vhost |
| `/var/log/finalzoom-backend.{log,err.log}` | Backend logs |
| `/var/log/nginx/{access,error}.log` | Nginx logs |

---

## Troubleshooting

**Backend nahi chal raha:**
```bash
sudo journalctl -u finalzoom-backend -n 100 --no-pager
# Usually: MongoDB down, .env missing, port already in use
sudo systemctl status mongod
sudo ss -tlnp | grep 8001
```

**SSL fail hua:**
- DNS A record check kar — `dig +short yourdomain.com` se VPS IP aana chahiye
- Port 80 open hai? `sudo ufw status`
- Manually retry: `sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com`

**Frontend white screen / API 502:**
- `REACT_APP_BACKEND_URL` correctly `https://yourdomain.com` set hai? (`/opt/finalzoom/frontend/.env`)
- Rebuild karo: `cd /opt/finalzoom/frontend && sudo -u finalzoom yarn build && sudo systemctl reload nginx`

**MongoDB install fail (ubuntu 24 noble):**
- Script auto-fallback karta hai jammy repo pe. Manual: `apt-get install -y mongodb-org` ke baad service start kar do.

**Login "Invalid credentials":**
- `.env` ka `ADMIN_EMAIL` + `ADMIN_PASSWORD` confirm karo
- Backend restart: `sudo systemctl restart finalzoom-backend`
- Logs: `sudo tail -50 /var/log/finalzoom-backend.log`

---

## One-line copy-paste (sab kuch ek baar mein)

VPS pe root login karke, zip ko `/root/finalzoom-main.zip` pe rakho, fir:

```bash
cd /root && \
apt-get update -y && apt-get install -y unzip rsync && \
unzip -oq finalzoom-main.zip && \
cd finalzoom-main && \
sudo bash deploy/clean.sh && \
DOMAIN="yourdomain.com" \
EMAIL="you@gmail.com" \
ADMIN_EMAIL="admin@yourdomain.com" \
ADMIN_PASSWORD="ChangeMe!Strong#2026" \
bash deploy/deploy.sh
```

Bas. ~6-8 minute mein `https://yourdomain.com` live.
