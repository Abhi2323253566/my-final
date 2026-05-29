# Linux RDP Setup — Zoom Worker v8 (Playwright Pool)

Ultra-optimized worker for Ubuntu 22.04 / Debian RDP nodes — implements the
**full optimization architecture**:

| Optimization                    | Impact   | Where                          |
|--------------------------------|----------|--------------------------------|
| **Browser pooling (1→N tabs)** | EXTREME  | `zoom_worker_pool.py`          |
| **Remove GUI (XVFB)**          | EXTREME  | `start_xvfb.sh`                |
| **Sequential RDP fill**        | HUGE     | backend `DISTRIBUTION_MODE=greedy` |
| **Disable video/audio**        | HUGE     | post-join CSS + mute/video off |
| **Context reuse**              | HUGE     | per-bot `BrowserContext`       |
| **Auto cleanup**               | HUGE     | `page.close → context.close`   |
| **Auto health monitor**        | HUGE     | thread + dynamic cap throttle  |
| **Auto restart (PM2)**         | HUGE     | `ecosystem.config.js`          |
| **Redis queue (optional)**     | MEDIUM   | backend `redis_queue.py`       |

---

## 1. Spec recommendation

| RAM   | CPU  | Expected stable bots (pool) |
|-------|------|------------------------------|
| 4 GB  | 2    | 25–35                        |
| 8 GB  | 2–4  | 50–80                        |
| 16 GB | 4    | 100–140                      |
| 32 GB | 8    | **180–220** ✅ sweet spot   |
| 64 GB | 8–16 | 250–350                      |

The pool packs ~120 MB per bot (vs ~210 MB in v7 multi-process).

---

## 2. One-shot install (Ubuntu 22.04)

```bash
# create the worker dashboard entry first (Workers → Add Worker) → download .env
mkdir -p ~/zoom-worker && cd ~/zoom-worker

# pull all files from your dashboard
DASH=https://your-dashboard.example.com
curl -sO "$DASH/api/worker/zoom_worker_pool.py"
curl -sO "$DASH/api/worker/requirements.txt"
curl -sO "$DASH/api/worker/start_xvfb.sh"
curl -sO "$DASH/api/worker/ecosystem.config.js"
curl -sO "$DASH/api/worker/install_linux.sh"
chmod +x start_xvfb.sh install_linux.sh

# drop your .env (downloaded when you created the worker on dashboard)
nano .env   # paste DASHBOARD_URL=… WORKER_TOKEN=…

# auto-install everything
sudo bash install_linux.sh
```

After install completes:

```bash
# foreground sanity check
source .venv/bin/activate
source ./start_xvfb.sh
python zoom_worker_pool.py
```

You should see:

```
Zoom Worker v8 (Playwright pool) starting
  dashboard=https://…
  cpu=8c  ram=32.0G  safe_cap=64
  tabs_per_browser=20  headless=false  poll=5s
pool: launched chromium (pool size 1)
```

Dashboard → Workers page → worker shows 🟢 online within 5s.

---

## 3. Production (PM2 + reboot persistence)

```bash
pm2 start ecosystem.config.js
pm2 save
pm2 startup            # run the command it prints (one-time)
```

Logs: `~/zoom-worker/logs/worker.out.log` & `worker.err.log`
Status: `pm2 list` · Restart: `pm2 restart zoom-worker-pool`

---

## 4. Key .env tunables

```env
# Connection
DASHBOARD_URL=https://your-dashboard.example.com
WORKER_TOKEN=<id>.<secret>

# Pooling
TABS_PER_BROWSER=20            # 15-25 = sweet spot
HEADLESS=false                 # true if no Xvfb installed (less stealth though)
POLL_INTERVAL=5
SPAWN_DELAY_MS=250
MAX_CONCURRENT_TASKS=5

# Auto-capacity (lets backend pick best worker)
AUTO_CAPACITY=true
RAM_PER_BOT_MB=120
BOTS_PER_CPU=8
RAM_HEADROOM_PCT=20
MAX_CAPACITY_HARD_CAP=500

# Dynamic throttle (architecture doc rule: if cpu>75 → max_members -= 5)
CPU_THROTTLE_PCT=75
RAM_THROTTLE_PCT=85
DYNAMIC_LIMIT_STEP=5

# Anti-kick / safety
BOT_REJOIN_MAX=2
KICK_DETECT_WINDOW=180
PRE_SPAWN_FREE_RAM_PCT=12

# Cleanup
CLEANUP_INTERVAL_SEC=300       # periodic orphan-chromium sweep
```

---

## 5. Sequential vs Weighted distribution

Backend `.env`:

```env
DISTRIBUTION_MODE=greedy     # RDP1 fills first, then RDP2, then RDP3 (default)
# OR
DISTRIBUTION_MODE=weighted   # spread proportionally to capacity from the start
```

---

## 6. How browser pooling works

```
                 Workers/me/claim → 50 bots
                          ↓
           ┌──────────────┴──────────────┐
           │   BrowserPool (this RDP)    │
           │                             │
           │   ┌─────────────────────┐   │
           │   │  Chromium #1        │   │
           │   │  ├ context (bot 1)  │   │
           │   │  ├ context (bot 2)  │   │
           │   │  └ … up to 20      │   │
           │   └─────────────────────┘   │
           │                             │
           │   ┌─────────────────────┐   │
           │   │  Chromium #2        │   │
           │   │  ├ context (bot 21) │   │
           │   │  └ … up to 20      │   │
           │   └─────────────────────┘   │
           │                             │
           │   Chromium #3 (10 contexts)│
           └─────────────────────────────┘
```

When a bot's meeting ends:
  1. `page.close()` → tab gone
  2. `context.close()` → cookies/storage isolated session destroyed
  3. If browser has 0 contexts left → `browser.close()` → chromium process exits → RAM freed

---

## 7. Auto-failover (dashboard side)

If a worker stops heartbeating for **45 s** (`HEALTH_STALE_SECONDS`):
  - The backend's `task_poller` marks all of its active chunks **failed**.
  - The unjoined members get added back to `members_claimed`'s remaining pool.
  - The next polling RDP picks them up (zero-idle behaviour).

This is the "auto failover" + "zero idle RDP" feature from the architecture doc.

---

## 8. Multiple RDPs — copy & repeat

For every additional RDP:
  1. Dashboard → Workers → Add Worker (unique name)
  2. Download fresh `.env`
  3. Repeat install steps above
  4. Backend auto-distributes (greedy fill or weighted)

---

## 9. Common issues

| Problem                              | Fix                                                  |
|--------------------------------------|------------------------------------------------------|
| `playwright: chromium not found`     | `python -m playwright install chromium --with-deps`  |
| `cannot open display :99`            | Run `source ./start_xvfb.sh` first                   |
| All bots kicked instantly            | Increase `SPAWN_DELAY_MS=600`, set HEADLESS=false    |
| Worker reads RAM > 90% & bots crash  | Reduce `BOTS_PER_CPU=5`, or `MAX_CAPACITY_HARD_CAP`  |
| `pm2 list` says errored              | `pm2 logs zoom-worker-pool --lines 100`              |
| Want to throttle harder on CPU spike | Lower `CPU_THROTTLE_PCT=65`                          |
