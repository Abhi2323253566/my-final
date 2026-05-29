# FinalZoom — Ultra Pro Zoom Bot Manager

## Original Problem Statement
Build an "Ultra Pro" Zoom bot orchestrator that:
- Distributes 1000 bots evenly across 30–40 connected RDP workers with strict manual capacity limits.
- Joins meetings with **video + audio strictly OFF** (Nuclear stealth) but the **mic + camera icons must still be visible** so bots look like real participants.
- Optional bot reactions when the UI toggle is ON.
- Strict anti-leave guards (auto-rejoin on drop).
- Keeps browser tabs warmed up between meetings.
- 0% mid-meeting drops target.

User language: Hindi / Hinglish. Tone: confident, "Ultra Pro", ultra-accommodating.

## Architecture
- `/app/backend` — FastAPI + MongoDB (Motor). Admin auth, task scheduling,
  worker claim/heartbeat APIs, chunk distribution.
- `/app/frontend` — React 19 dashboard (workers, tasks, schedule, names).
- `/app/worker` — Python asyncio + Playwright headless Chromium pool
  (`zoom_worker_pool.py`). Joins Zoom Web Client, holds the meeting, mutes
  mic/cam, auto-rejoins, fires reactions if enabled.

## Key Tech
- Playwright headless Chromium + fake media stream flags.
- `enumerateDevices` mock so Zoom renders mic/cam icons even when no real
  hardware is present.
- `Alt+A` / `Alt+V` keyboard fallback to enforce mute.
- FastAPI lifecycle seeds admin on startup (with env-var override + hard
  fallback defaults).

## DB Schema (key collections)
- `workers`: `{ id, user_id, name, capacity_max, reported_capacity, last_heartbeat, ... }`
- `tasks`: `{ id, user_id, status, distribution_mode, reaction_interval_min, reaction_interval_max, ... }`
- `task_chunks`: `{ id, task_id, worker_id, status, started_at }`
- `users`: `{ id, email, password_hash, role, usage, usage_limit }`

## Key Endpoints
- `POST /api/auth/login`
- `POST /api/workers/me/claim`
- `POST /api/workers/me/heartbeat`
- `GET  /api/tasks/active|scheduled|previous`

## Changelog
- **2026-02 — Smart Auto-Distribute (Bulk Capacity)**:
  `WorkersPage.jsx` Bulk modal now has two modes via tab toggle —
  (a) **Fixed Capacity** (existing behaviour: same cap on every selected RDP),
  (b) **Smart Auto-Distribute**: operator enters Total Bots (e.g. 1000) and
  the UI evenly splits it across selected RDPs using
  `base = floor(total/N)` with remainder spilled into the first R workers
  (sorted alphabetically) so `sum === total` exactly.
  Live preview shows `total ÷ N = base (+1 on first R)`, min/max/sum and
  per-row `→ capacity` allocations. Apply patches each worker independently
  via `PATCH /api/workers/:id`.
- **2026-02 — Fake media flags restored (user request)**:
  Both `--use-fake-ui-for-media-stream` AND `--use-fake-device-for-media-stream`
  now in `CHROMIUM_ARGS` so Zoom shows mic + camera icons on each bot tile.
  Audio/video kept OFF via JOIN_WITH_VIDEO_OFF + post-join Alt+A/Alt+V mute +
  enumerateDevices mock.
- **2026-02 — Robust admin seed**:
  `seed_admin()` in `/app/backend/server.py` now uses `os.environ.get(...)`
  with hard fallback defaults (`admin@finalzoom.com` / `Admin@FinalZoom2026`)
  so login works on live deploy even when env vars are not set. Password is
  re-synced on every startup.
- **Earlier**: `enumerateDevices` mock, `Alt+A`/`Alt+V` fallback, Join-Audio
  prompt handling, 4-bot live stress test (0 rejoins).

## Pending / Backlog
- P1: Re-deploy production via Emergent **Deploy** button so DB seed + worker
  flags + Smart Auto-Distribute UI go live.
- P2: Reaction toggle wiring smoke test once user OKs further testing.

## Constraints
- User explicitly requested **no bot testing right now** (`ab testing mt kro`).
  Code edits only — no live Zoom join runs.
