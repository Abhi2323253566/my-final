# FinalZoom — Product Requirements & State

## Original Problem Statement
User imported their existing codebase (FinalZoom / Zoom Services API) into the
Emergent platform and asked to:
1. Deploy it fully on the Emergent preview environment.
2. (Future) Provide a guide to deploy it on their own VPS server, replacing
   an older deployment they had there.

Language: Hindi / Hinglish.

## Tech Stack
- Backend: FastAPI + MongoDB (motor), supervisor-managed on port 8001.
- Frontend: React 19 + TailwindCSS, supervisor-managed on port 3000.
- Routing: All API endpoints mounted under `/api` via `APIRouter(prefix="/api")`
  (server.py line 76). External preview routes `/api/*` to backend, everything
  else to frontend.

## Environment (already configured)
- `/app/backend/.env`: MONGO_URL, DB_NAME=finalzoom_db, JWT_SECRET, ADMIN_EMAIL,
  ADMIN_PASSWORD, ADMIN_NAME, USAGE_LIMIT, DISTRIBUTION_MODE,
  HEALTH_STALE_SECONDS, CORS_ORIGINS.
- `/app/frontend/.env`: REACT_APP_BACKEND_URL (preview URL), WDS_SOCKET_PORT.

## Admin Credentials (live)
- Email: admin@finalzoom.com
- Password: Admin@FinalZoom2026
(See `/app/memory/test_credentials.md`.)

## What's Implemented (2026-02)
- 2026-02: Codebase migrated from `/app/finalzoom-main/` to `/app/backend` and
  `/app/frontend`. Dependencies installed (pip + yarn).
- 2026-02: Verified `/api` prefix is correctly configured (was already in code).
- 2026-02: Supervisor services running (backend + frontend).
- 2026-02: Fixed admin login blocker — changed ADMIN_EMAIL from
  `admin@finalzoom.local` (rejected by Pydantic EmailStr — reserved TLD) to
  `admin@finalzoom.com`. Old user removed from DB, seed re-created on restart.
  Login verified via curl on the external preview URL.
- 2026-02: **Multi-RDP scale fixes (1000-bot / 30-RDP support)**:
  * `server.py` — `DISTRIBUTION_MODE` default changed `greedy` → `auto`. Auto
    mode does strict EQUAL split `ceil(members / online_count)` per task so a
    500-bot task across 30 online RDPs gives each ≈ 17 bots (verified by
    `/app/backend/tests/test_distribution.py` — 30/30 workers, no starvation,
    max 17 / min 7 spread). Modes: `auto` (default), `weighted`, `even`,
    `greedy`. Configurable via `/app/backend/.env`.
  * `zoom_worker.py` — Meeting-end cleanup: when `_meeting_has_ended(driver)`
    returns True, the bot now `time.sleep(MEETING_END_GRACE_SEC)` (default 5s)
    then `driver.quit()` + process exit. No more zombie chromes after host
    ends meeting.
  * `zoom_worker.py` — Browser warm-up throttle: cross-process
    `mp.Semaphore(BROWSER_WARMUP_LIMIT)` (default 3) gates every Chrome
    launch on an RDP. Prevents RAM/CPU spike on big tasks.
  * `zoom_worker.py` — Strict mute / camera-off: added `_inject_strict_media_stubs`
    via CDP `Page.addScriptToEvaluateOnNewDocument` to override
    `getUserMedia` with silent-oscillator audio + black-canvas video, plus
    Chrome flags `--use-fake-{ui,device}-for-media-stream`,
    `--disable-webrtc-hw-{en,de}coding`, `--enable-usermedia-screen-capturing`.
    No more "tu tu" leaks or green-screen frames.
  * Created `/app/backend/tests/test_distribution.py` — async simulation that
    spawns 30 mock workers + 500-bot task and asserts no starvation.
- 2026-02 (latest):
  * Wave-join (10s gap between joins per RDP), pro-level disk-cache pre-warm,
    and strict JS anti-leave guards added to
    `/app/frontend/public/worker/zoom_worker.py`.
  * `/app/frontend/src/components/CreateTaskPanel.jsx` — Meeting ID & Password
    now persist in localStorage with a manual "Clear ID/Pwd" button.
  * **Distribution starvation bug FIXED**: `worker_claim_tasks` MOP-UP path
    used to trigger after just 15s of task age, allowing late-polling workers
    to grab their full 50-bot capacity and starve others. Replaced with a
    stall-aware MOP-UP (`MOPUP_STALL_SECS`, default 45s; based on
    `last_claim_at` timestamp, not age) and capped MOP-UP take at
    `2 × fair_share` so even mop-up stays roughly even. Verified by
    `tests/test_distribution.py` for 30/35/40 RDPs — all PASS.
  * Test cleanup hardened: `tests/test_distribution.py` now wipes lingering
    `sim-rdp-*` workers AND cancels stale active tasks before every run.

## Live URLs
- Preview: https://vps-deploy-guide-3.preview.emergentagent.com
- Production: https://vps-deploy-guide-3.emergent.host

## Backlog (P0 → P2)
- P1: Run `testing_agent_v3_fork` for end-to-end coverage of auth, tasks,
  workers, name-files, admin overview, topup flows.
- P1: Live RDP-health dashboard (RAM/CPU per worker) for managing 30+ RDPs.
- P2: Produce a step-by-step VPS deployment guide (Docker Compose +
  Nginx reverse proxy + MongoDB + Supervisor) for the user's own server.
- P2: Optionally add a healthcheck endpoint and /api/version surfacing.

## Known Gotchas
- Do not revert ADMIN_EMAIL to `.local` TLD — Pydantic EmailStr will reject it
  at login time and lock everyone out.
- Backend route prefix is set at the APIRouter, not at `include_router`. Keep
  `app.include_router(api)` as-is.
- `tasks.members` Pydantic constraint is **max 500** per task. For 1000-bot
  meetings the user should create two tasks; the simulation test reflects this.
- `BROWSER_WARMUP_LIMIT` (default 3) and `MEETING_END_GRACE_SEC` (default 5)
  are tunable per-RDP via the worker's `.env` if a beefy server can take more.
