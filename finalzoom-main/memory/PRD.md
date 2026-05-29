# Zoom Worker System — Ultra-Optimized Architecture (PRD)

## Original Problem Statement
Build the "Zoom Worker System — Ultra Optimized Architecture" described by the user (Hinglish brief) covering:
- Smart sequential RDP filling
- Auto cleanup after meeting end
- Stable member allocation
- Low RAM, browser pooling, auto health monitor, zero idle RDP, auto restart unhealthy workers
- **PREWARM EVERYTHING POSSIBLE** — hot browsers, ready contexts, ready tabs, warm standby pool, auto-warmup engine, auto-shrink engine.

User uploaded `zoom-main.zip` containing the existing Zoom Services Clone (iter 1–9 already implemented). Asked: "extract karke implement kro" — meaning: integrate the zip + add the new optimizations on top.

## App Domain
Zoom meeting participant management tool — admin creates *tasks* that join Zoom meetings with N bot participants (custom names, optional emoji/reactions, optional scheduling). Tasks are picked up by a fleet of **RDP workers** (separate Linux/Windows VPS boxes running `zoom_worker_pool.py`).

## Architecture
- **Controller dashboard** (this Emergent container)
  - **Frontend**: React 19 + Tailwind dark theme, Lucide icons, sonner toasts, react-router v7
  - **Backend**: FastAPI + Motor (MongoDB async), JWT cookie auth + Bearer worker tokens, bcrypt hashing, async 5-second poller
  - **DB collections**: `users`, `tasks`, `task_chunks`, `name_files`, `workers`, `login_attempts`, `topup_requests`, `settings`
- **Worker pool** (`/app/worker/zoom_worker_pool.py` v8.1-prewarm — Playwright + browser pooling + PREWARM engine). Designed to run on Linux VPS / Windows RDP — NOT inside this Emergent container.

## User Personas
1. **Service operator (admin)** — dashboard owner, creates tasks, manages RDP fleet, monitors health
2. **End user (client)** — buys credits, creates own tasks
3. **RDP worker (machine)** — Linux/Windows VPS running the Playwright pool worker

## Iteration 11 — v8.3 "Tap-and-Join" (form-prewarm + storage_state + disk-cache)
**Goal:** Drop join latency from ~3-4s (v8.1 homepage prewarm) to ~0.8-1.2s by prewarming the actual JOIN FORM page with cookies, localStorage flags, and a shared on-disk Zoom SDK cache.

**Worker (`/app/worker/zoom_worker_pool.py` v8.1 → v8.3-tap-and-join):**
- `PREWARM_PRELOAD_URL` default changed from `https://app.zoom.us/wc/` → `https://app.zoom.us/wc/join` (the actual meeting-ID form).
- New `bootstrap_storage_state(pw)` — runs once at boot:
  - Opens throwaway browser → navigates to zoom.us → accepts cookie banner (OneTrust, etc.) → seeds localStorage flags (`webclient_audio_setting=computer`, `zm_cookie_consent=accepted`, `skip_audio_join=1`, `OptanonAlertBoxClosed=<ts>`, `hideNewMeetingPromote=1`) → navigates to `/wc/join` to cache SDK assets → saves snapshot to `STORAGE_STATE_PATH` (`/tmp/zoom-storage-state.json`).
  - Skipped if cached snapshot is < `STORAGE_STATE_REFRESH_HOURS` (24h) old.
- New `_new_context_kwargs()` helper — every `new_context()` call (both prewarm and cold-path in `run_bot`) auto-loads `storage_state=` if the snapshot file exists.
- `_make_ready_context()` now waits for one of the join-form selectors (`#join-confno`, `input[name='confno']`, `#input-for-name`) to mount before adding to the ready pool — confirms SDK JS has executed.
- New `PERSISTENT_CACHE` mode (default ON) — adds `--disk-cache-dir=<shared>` and `--disk-cache-size=256MB` to Chromium args so the Zoom SDK js/css/wasm survives browser restarts AND is shared across the pool. Disables the v8.1 `--aggressive-cache-discard`/`--disk-cache-size=1` flags when on.
- `pool.stats()` now reports `{version: "v8.3-tap-and-join", storage_state_age_hours, persistent_cache, preload_url}` so the dashboard can show "tap-and-join armed" state.
- New env knobs: `PERSISTENT_CACHE`, `PERSISTENT_CACHE_DIR`, `PERSISTENT_CACHE_SIZE_MB`, `STORAGE_STATE_PATH`, `STORAGE_STATE_REFRESH_HOURS`, `FORM_PREWARM_WAIT_MS`.

**Frontend (`/app/frontend/src/pages/WorkersPage.jsx`):**
- Pool column shows the **v8.3 tap-and-join** badge (green) when the worker reports the new version, plus the storage_state age (`state 0.5h`) so operator knows the bake is fresh.

**Expected latency budget (per bot join):**
| Stage | v8.1 (homepage prewarm) | v8.3 (tap-and-join) |
|---|---|---|
| DNS + TLS | cached | cached |
| SDK js/css/wasm download | ~1.5-2s | ~0ms (disk cache) |
| Cookie banner click | ~500ms | 0ms (storage_state) |
| Form HTML + DOM mount | ~1-1.5s | 0ms (already mounted) |
| `fill(name)` + `fill(pwd)` + `click(join)` | ~300ms | ~300ms |
| "Join Audio" prompt dismiss | ~500ms | 0ms (localStorage flag) |
| **Total** | **~3-4s** | **~0.8-1.2s** |
**Goal:** Add prewarming on top of the v8 browser-pool worker so bot joins drop from ~10s cold to 1–3s hot.

**Worker (`/app/worker/zoom_worker_pool.py` v8 → v8.1-prewarm):**
- New `ReadyContext` dataclass: pre-built `BrowserContext` + `Page` (preloaded with `https://app.zoom.us/wc/`) waiting in a standby pool.
- `BrowserPool.prewarm()` — at boot launches `PREWARM_BROWSERS` (default 2) hot chromium processes and pre-creates `PREWARM_CONTEXTS` (default 10) ready contexts spread across them. Each ready context has the Zoom web client shell already loaded (DNS, TLS, JS bundle cached).
- `BrowserPool.acquire_ready_context()` — O(1) handoff of a hot context. Used by `run_bot()` before falling back to the cold `new_context()` path.
- `BrowserPool.topup_ready()` — AUTO-WARMUP ENGINE: keeps `>= PREWARM_MIN_READY` (default 5) standby contexts; refills up to `PREWARM_MAX_READY` (default 20).
- `BrowserPool.shrink_idle()` — AUTO-SHRINK ENGINE: closes hot browsers idle > `SHRINK_IDLE_SEC` (default 120s) so unused RAM is reclaimed; respects min `PREWARM_BROWSERS` floor.
- `warmup_loop()` background task drives topup + shrink every `WARMUP_INTERVAL_SEC` (default 15s).
- `release_browser_if_empty()` now respects the prewarm floor — never closes below `PREWARM_BROWSERS`.
- New env knobs (`.env`): `PREWARM_ENABLED`, `PREWARM_BROWSERS`, `PREWARM_CONTEXTS`, `PREWARM_MIN_READY`, `PREWARM_MAX_READY`, `PREWARM_PRELOAD_URL`, `WARMUP_INTERVAL_SEC`, `SHRINK_IDLE_SEC`.
- Heartbeat payload now includes `pool_stats`: `{browsers, total_bots, alive, ready_contexts, prewarmed}`.

**Backend (`/app/backend/server.py`):**
- `HeartbeatIn` accepts new `pool_stats` field; stored on the worker doc.
- `WorkerOut` + `/admin/fleet-health` + per-worker `_worker_out()` all surface `pool_stats`.
- `/admin/fleet-health.summary.prewarm` aggregates `{hot_browsers, ready_contexts, active_bots, prewarmed_workers}` across the online fleet.

**Frontend (`/app/frontend/src/pages/WorkersPage.jsx`):**
- New **PREWARM POOL** band inside Fleet Health Monitor card — shows Hot Browsers, Ready Contexts, Active Bots, Prewarmed RDPs.
- New **Pool** column in workers table showing per-worker `🔥 N hot / ⚡ N ready / prewarmed|cold` state.

## Highest-Impact Optimizations (now in code)
| Optimization | Where | Impact |
|---|---|---|
| Sequential RDP fill (greedy mode) | backend `DISTRIBUTION_MODE=greedy` | HUGE — one RDP fills before another |
| Browser pooling (1 chromium ↔ many contexts) | `BrowserPool` | EXTREME — RAM ↓ ~70% |
| Ultra-lean Chromium flags | `CHROMIUM_ARGS` (v7-lean carried fwd) | HUGE |
| Post-join CPU savings (hide video, visibility=hidden) | `_post_join_optimize` | HUGE |
| XVFB (Linux headless display) | `worker/start_xvfb.sh` | HUGE |
| PREWARMED browsers + contexts | NEW v8.1 | EXTREME — joins 1-3s |
| Auto-warmup engine | NEW v8.1 | HIGH |
| Auto-shrink engine | NEW v8.1 | HIGH (RAM reclaim) |
| Auto-restart dead chromium | `health_monitor` | HIGH |
| Dynamic capacity throttle on cpu>75 / ram>85 | `health_monitor` | HIGH |
| Stale-worker auto-failover (release chunks) | backend `task_poller` | HIGH |
| Greedy/weighted distribution toggle | backend env | MEDIUM |

## What's been implemented
- Full controller dashboard from zoom-main.zip (auth, tasks, name files, workers, top-ups, admin)
- v8.1-prewarm worker pool with PREWARM engine
- Backend + frontend telemetry for prewarm pool stats
- Seeded admin user from `.env`
- Test_credentials.md updated

## Prioritized Backlog
- **P1** — Real RDP smoke test (user must run `python zoom_worker_pool.py` on a Linux VPS with XVFB)
- **P2** — Persist pool_stats history in MongoDB for trend graphs
- **P2** — WebSocket push instead of 8s polling on /workers page
- **P2** — Per-task latency metric (join_started_at → joined_at) on chunk
- **P3** — Redis Pub/Sub channel so workers receive "stop task" instantly instead of via 5s poll

## Next Action Items
1. (User) Deploy worker on a Linux VPS (Oracle free tier / Contabo) — follow `/app/RDP_SETUP_LINUX.md`, set `DASHBOARD_URL` + `WORKER_TOKEN` in worker `.env`, run `xvfb-run -a python zoom_worker_pool.py`.
2. (User) Verify Hot Browsers + Ready Contexts > 0 on `/workers` after the worker boots.
3. (Optional) Tune `PREWARM_BROWSERS=3`, `PREWARM_CONTEXTS=15` on 16GB+ boxes.
