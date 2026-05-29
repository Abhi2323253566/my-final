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

## Live URLs
- Preview: https://vps-deploy-guide-3.preview.emergentagent.com
- Production: https://vps-deploy-guide-3.emergent.host

## Backlog (P0 → P2)
- P1: Run `testing_agent_v3_fork` for end-to-end coverage of auth, tasks,
  workers, name-files, admin overview, topup flows.
- P2: Produce a step-by-step VPS deployment guide (Docker Compose +
  Nginx reverse proxy + MongoDB + Supervisor) for the user's own server.
- P2: Optionally add a healthcheck endpoint and /api/version surfacing.

## Known Gotchas
- Do not revert ADMIN_EMAIL to `.local` TLD — Pydantic EmailStr will reject it
  at login time and lock everyone out.
- Backend route prefix is set at the APIRouter, not at `include_router`. Keep
  `app.include_router(api)` as-is.
