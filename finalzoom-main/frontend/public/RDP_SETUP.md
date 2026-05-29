# RDP Setup Guide — Zoom Services Worker Fleet

Yeh guide aapko **bilkul scratch se** ek Windows RDP/VPS pe Zoom worker bot setup karne ke liye step-by-step bataati hai. Hindi + English mix me — easy to follow.

---

## 0. Pehle Dashboard pe Worker banayein
1. Login → **Workers** page (top bar me link)
2. **"Add Worker"** → naam do (e.g. `RDP-Box-1`), capacity do (32GB/8CPU ke liye **80**)
3. **Token copy karo** (only once shown!) ya **"Download .env file"** click karke ready-made `.env` download kar lo

---

## 1. Windows RDP / VPS Spec (recommended)

| Spec | Minimum | Recommended (sweet spot) |
|------|---------|--------------------------|
| OS | Windows Server 2019 / 10 Pro | **Windows Server 2022 Standard** |
| CPU | 4 vCPU | **8 vCPU** |
| RAM | 16 GB | **32 GB** |
| Disk | 80 GB SSD | **NVMe SSD 120+ GB** |
| Network | 500 Mbps | **1 Gbps unmetered** |
| IP | Any | **Indian IP** if Indian meetings |

**Capacity rule of thumb**:
- 32 GB / 8 CPU → 80–100 bots safely (210 MB per bot avg)
- 64 GB / 16 CPU → 180–220 bots
- Plain (no sandboxing) → 35–50 bots only

---

## 2. Windows tweaks (one-time)

Run PowerShell **as Administrator** and execute:

```powershell
# Power plan = High Performance (no sleep)
powercfg -setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c

# Disable screen lock / sleep
powercfg -change -monitor-timeout-ac 0
powercfg -change -standby-timeout-ac 0
powercfg -change -hibernate-timeout-ac 0

# Disable Windows Defender real-time scanning on Zoom folder (Server only — domain-joined boxes may need policy override)
Add-MpPreference -ExclusionPath "$env:APPDATA\Zoom"
Add-MpPreference -ExclusionProcess "Zoom.exe"

# TCP tuning
netsh int tcp set global autotuninglevel=normal
netsh int tcp set global rss=enabled

# Disable Telemetry / Cortana (optional, frees ~300MB)
Get-Service -Name DiagTrack | Stop-Service -Force
Set-Service -Name DiagTrack -StartupType Disabled
```

Disable Zoom auto-update (registry):
```powershell
New-Item -Path "HKLM:\SOFTWARE\Policies\Zoom\Zoom Meetings\General" -Force | Out-Null
New-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Zoom\Zoom Meetings\General" -Name "EnableClientAutoUpdate" -Value 0 -PropertyType DWORD -Force
```

Set a fixed pagefile (8 GB) so Windows doesn't shrink/grow under load:
- System Properties → Advanced → Performance → Settings → Advanced → Virtual memory → Change → set Custom 8192 MB on C: drive → Set → OK.

---

## 3. Install software

### 3a. Zoom Desktop client
1. Download from https://zoom.us/client/latest/ZoomInstallerFull.exe
2. Install with defaults
3. **Open once, sign-out, close** — yeh first-run prompts ko clear karta hai
4. In Settings (gear icon):
   - ✅ "Always mute my microphone when joining"
   - ✅ "Always turn off my video when joining"
   - ❌ "Show me the 'Join Audio' dialog when joining"
   - ❌ "Ask me to confirm when I leave a meeting"

### 3b. VB-Audio Virtual Cable (free, **important** — saves ~30% CPU)
1. https://vb-audio.com/Cable/ → Download → Extract → Run **VBCABLE_Setup_x64.exe** as Admin
2. Reboot
3. In Zoom Settings → Audio → set both **Speaker** and **Microphone** to "CABLE Input/Output"

### 3c. Sandboxie-Plus (free — for running 80+ Zoom instances on one user)
1. https://sandboxie-plus.com/downloads/ → Download Sandboxie-Plus
2. Install with defaults
3. Open Sandboxie Control → right-click **"Sandbox"** → New Sandbox → create **Box1** through **Box20** (or however many bots per user you need)
4. For each box: Right-click → Sandbox Options → "Drop process rights from administrators" = NO (Zoom needs them)

> **Why Sandboxie?** Zoom only allows 1 instance per Windows user. Sandboxie tricks Zoom into thinking each sandbox is a fresh user → you can run 20+ Zoom instances per Windows account. Combine with multiple Windows accounts for 100+.

### 3d. Python 3.10+
1. https://www.python.org/downloads/windows/ → install → **check "Add to PATH"**
2. Verify: open CMD → `python --version`

### 3e. NSSM (run worker as Windows service so it survives RDP disconnects)
1. https://nssm.cc/download → unzip nssm.exe somewhere (e.g. `C:\tools\nssm.exe`)

---

## 4. Deploy the worker script

1. Create folder `C:\zoom-worker\`
2. Download `zoom_worker.py` and `requirements.txt` from this repo's `/app/worker/` folder, paste into `C:\zoom-worker\`
3. Place the `.env` file (downloaded from dashboard) in the same folder
4. Open the `.env` and verify:
   ```
   DASHBOARD_URL=https://your-dashboard.example.com
   WORKER_TOKEN=<id>.<secret>
   ZOOM_EXE=C:\Users\YourUser\AppData\Roaming\Zoom\bin\Zoom.exe
   POLL_INTERVAL=5
   SPAWN_DELAY_MS=400
   MAX_CONCURRENT_TASKS=20
   ```
5. Install requirements:
   ```
   cd C:\zoom-worker
   pip install -r requirements.txt
   ```
6. Test run (foreground first):
   ```
   python zoom_worker.py
   ```
   Output should show:
   ```
   [13:25:06] Zoom worker started — dashboard=https://...
   [13:25:06] polling every 5s, spawn_delay=400ms
   ```
   Dashboard → Workers page → should show this worker as **🟢 online** within 5 sec.

---

## 5. Install as Windows Service (production)

Once foreground test works, install as service via NSSM:

```powershell
C:\tools\nssm.exe install ZoomWorker "C:\Python310\python.exe" "C:\zoom-worker\zoom_worker.py"
C:\tools\nssm.exe set ZoomWorker AppDirectory "C:\zoom-worker"
C:\tools\nssm.exe set ZoomWorker AppStdout "C:\zoom-worker\worker.log"
C:\tools\nssm.exe set ZoomWorker AppStderr "C:\zoom-worker\worker.err.log"
C:\tools\nssm.exe set ZoomWorker Start SERVICE_AUTO_START

# Start it
net start ZoomWorker
```

Service ab auto-start hoga on reboot, RDP disconnect pe bhi chalti rahegi.

To stop / uninstall:
```
net stop ZoomWorker
C:\tools\nssm.exe remove ZoomWorker confirm
```

---

## 6. Verify end-to-end

1. Dashboard → Workers page → your worker should show **🟢 online**, hostname + OS visible, CPU/RAM stats updating every 5s
2. Dashboard → create a task with **2 members**, timeout **120 sec**, meeting ID of a test meeting you host
3. Within ~10 sec, the worker should claim it → in the **All Meetings** table you'll see `worker = RDP-Box-1` and `joined_count = 2`
4. After 120 sec, the row moves to **Previous Tasks** as `completed`

---

## 7. Common Issues & Fixes

| Problem | Fix |
|---------|-----|
| Worker shows offline | Check `worker.err.log` — usually wrong `DASHBOARD_URL` or expired token. Token is hashed server-side — if lost, delete worker on dashboard and re-create. |
| Zoom opens but doesn't auto-join | Zoom registry → make sure "Show 'Join Audio' dialog" is OFF. Also disable Zoom auto-update so v breaking changes are pinned. |
| Bots all join then 5 immediately drop | RAM full — reduce capacity_max in dashboard, or upgrade RAM. Check Task Manager → Zoom.exe instances. |
| Sandboxie says "Cannot start" | Run Sandboxie Control as admin → Sandbox → Terminate All Programs → retry. Some antivirus blocks Sandboxie kernel driver — whitelist it. |
| Bots get kicked after 30 sec | Zoom flagged bulk join. Increase `SPAWN_DELAY_MS=800` in .env. Stagger more. |
| Network timeouts on heartbeat | Firewall blocking outbound 443. Allow `python.exe` in Windows Defender Firewall. |
| Service stops randomly | Check Windows Event Viewer → Application → look for `ZoomWorker` errors. Increase NSSM restart-on-crash: `nssm set ZoomWorker AppExit Default Restart` |

---

## 8. Capacity tuning cheatsheet (32GB / 8CPU)

| Capacity setting | What to expect |
|------------------|----------------|
| 40 | Rock-solid, no swap, CPU < 50% |
| 60 | Solid, RAM ~70%, CPU ~60% |
| **80** ✅ | **Recommended.** RAM ~85%, CPU ~75%. Headroom for spikes. |
| 100 | Pushable. RAM 92-95%, CPU 85%. Risk of swap when reactions fire. |
| 120+ | Will start dropping bots. Add a 2nd RDP instead. |

---

## 9. Adding multiple RDPs

Dashboard supports unlimited workers. Each RDP:
- Create a **separate** worker in dashboard (`RDP-Box-1`, `RDP-Box-2`, …)
- Each gets its own token → its own `.env`
- Tasks are **automatically distributed** (first-come-first-served via atomic claim) — no manual assignment needed

To balance load across boxes, just set realistic `capacity_max` per worker. The dashboard's claim endpoint won't give more tasks than capacity_left.

---

## 10. Security tips
- Treat the WORKER_TOKEN like a password. Never commit it to GitHub.
- If a token leaks, delete the worker on the dashboard — that invalidates the token immediately.
- Use Windows Firewall to allow only outbound 443 to your dashboard domain.
- Don't reuse the RDP for other workloads (mining, scraping) — Zoom may flag the IP.

---

Done! Aapka fleet ready hai. Dashboard pe tasks create karo, RDP pe bots automatically join honge. 🚀

For questions / bugs, raise on the dashboard repo.
