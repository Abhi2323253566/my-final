from dotenv import load_dotenv
from pathlib import Path
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import sys
import json
import math
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Any

import re
import bcrypt
import jwt
from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, Query
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr, field_validator


# ---------------- Zoom credential format helpers ----------------
def normalize_meeting_id(raw: str) -> str:
    """Strip spaces, dashes, and any non-digit characters from a meeting ID."""
    return re.sub(r"\D", "", raw or "")


def validate_zoom_credentials(meeting_id: str, password: str) -> None:
    """Raise HTTPException(400) if Zoom meeting ID / password format is clearly wrong.
    Zoom meeting IDs are 9, 10, or 11 digits. Passwords (if present) are 1-10 chars,
    letters/digits only (per Zoom rules)."""
    mid = normalize_meeting_id(meeting_id)
    if not mid:
        raise HTTPException(status_code=400, detail="Wrong Meeting ID: cannot be empty")
    if not mid.isdigit():
        raise HTTPException(status_code=400, detail="Wrong Meeting ID: must contain digits only")
    if len(mid) < 9 or len(mid) > 11:
        raise HTTPException(
            status_code=400,
            detail=f"Wrong Meeting ID: must be 9-11 digits (got {len(mid)} digits)",
        )
    if password:
        if len(password) > 10:
            raise HTTPException(
                status_code=400,
                detail="Wrong Meeting Password: max 10 characters allowed by Zoom",
            )
        if not re.fullmatch(r"[A-Za-z0-9]+", password):
            raise HTTPException(
                status_code=400,
                detail="Wrong Meeting Password: only letters and digits allowed",
            )

# ---------------- Redis (optional cache layer) ----------------
try:
    from redis_queue import cache_get, cache_set, try_lock, release_lock
except Exception:
    # graceful: degrade to no-op if module missing
    async def cache_get(*_a, **_kw): return None
    async def cache_set(*_a, **_kw): return None
    async def try_lock(*_a, **_kw): return True
    async def release_lock(*_a, **_kw): return None


# ---------------- Mongo ----------------
mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

# ---------------- App ----------------
app = FastAPI(title="Zoom Services API")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("zoom")

# ---------------- Auth helpers ----------------
JWT_ALG = "HS256"


def jwt_secret() -> str:
    return os.environ["JWT_SECRET"]


def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode("utf-8"), h.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=12),
        "type": "access",
    }
    return jwt.encode(payload, jwt_secret(), algorithm=JWT_ALG)


def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
        "type": "refresh",
    }
    return jwt.encode(payload, jwt_secret(), algorithm=JWT_ALG)


def set_auth_cookies(response: Response, access: str, refresh: str):
    response.set_cookie("access_token", access, httponly=True, secure=False, samesite="lax", max_age=12 * 3600, path="/")
    response.set_cookie("refresh_token", refresh, httponly=True, secure=False, samesite="lax", max_age=7 * 86400, path="/")


def clear_auth_cookies(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, jwt_secret(), algorithms=[JWT_ALG])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------- Worker Auth ----------------
def _hash_token(t: str) -> str:
    return bcrypt.hashpw(t.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_token(t: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(t.encode("utf-8"), h.encode("utf-8"))
    except Exception:
        return False


async def get_current_worker(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Worker token required")
    raw = auth[7:].strip()
    # token format: "<worker_id>.<secret>"
    if "." not in raw:
        raise HTTPException(status_code=401, detail="Invalid worker token format")
    wid, secret = raw.split(".", 1)
    w = await db.workers.find_one({"id": wid}, {"_id": 0})
    if not w or not _verify_token(secret, w.get("token_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid worker token")
    return w


async def get_admin_user(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ---------------- Models ----------------
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str
    usage: int = 0
    usage_limit: int = 15000
    credit_rate: float = 1.0  # credits per ₹1 (e.g., 0.5 means ₹100 = 50 credits)


class TaskCreate(BaseModel):
    meeting_id: str
    meeting_password: Optional[str] = ""
    members: int = Field(ge=1, le=500)
    name_source: str = "NamesIn"          # "NamesIn" | "Indian" | "English" | custom file id
    meeting_type: str = "Normal Participants"  # or "Co-Host", etc.
    timeout: int = Field(default=7200, ge=10, le=86400)
    floating_emoji: bool = False
    participant_reactions: bool = False
    # v8.4: reaction interval (seconds). Worker picks random delay in [min,max]
    # between successive emoji clicks. Only used if participant_reactions OR
    # floating_emoji is True.
    reaction_interval_min: int = Field(default=30, ge=5, le=3600)
    reaction_interval_max: int = Field(default=90, ge=5, le=3600)
    scheduled_at: Optional[str] = None   # ISO string (IST input but stored as UTC)


class TaskOut(BaseModel):
    id: str
    user_id: str
    meeting_id: str
    meeting_password: Optional[str] = ""
    members: int
    name_source: str
    meeting_type: str
    timeout: int
    floating_emoji: bool
    participant_reactions: bool
    reaction_interval_min: int = 30
    reaction_interval_max: int = 90
    status: str  # scheduled | active | completed | failed | cancelled
    scheduled_at: Optional[str] = None
    started_at: Optional[str] = None
    ends_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str
    worker_id: Optional[str] = None
    worker_name: Optional[str] = None
    joined_count: int = 0
    last_progress_at: Optional[str] = None
    error: Optional[str] = None


class WorkerCreate(BaseModel):
    name: str
    capacity_max: int = 100


class WorkerOut(BaseModel):
    id: str
    name: str
    status: str  # online | offline
    capacity_max: int
    reported_capacity: Optional[int] = None  # what the worker says it can safely handle right now
    current_load: int = 0
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    ram_free_gb: Optional[float] = None
    cpu_count: Optional[int] = None
    hostname: Optional[str] = None
    os_info: Optional[str] = None
    last_heartbeat: Optional[str] = None
    created_at: str
    pool_stats: Optional[dict] = None  # {browsers, ready_contexts, total_bots, prewarmed}
    crash_count: int = 0  # cumulative main_loop crashes since worker started (from keep-alive supervisor)
    last_restart_at: Optional[str] = None  # ISO when keep-alive last restarted main_loop
    worker_started_at: Optional[str] = None  # ISO when worker process originally booted


class WorkerCreatedOut(WorkerOut):
    token: str  # one-time, plain token, shown only on creation


class HeartbeatIn(BaseModel):
    current_load: int = 0
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    hostname: Optional[str] = None
    os_info: Optional[str] = None
    reported_capacity: Optional[int] = None  # auto-computed safe max from worker RAM/CPU
    ram_free_gb: Optional[float] = None
    cpu_count: Optional[int] = None
    pool_stats: Optional[dict] = None  # browser pool prewarm stats from worker
    crash_count: Optional[int] = None  # cumulative crashes since worker started
    last_restart_at: Optional[str] = None  # ISO, last main_loop restart
    worker_started_at: Optional[str] = None  # ISO, worker process boot time


class TaskProgressIn(BaseModel):
    joined_count: int
    note: Optional[str] = None


class TaskCompleteIn(BaseModel):
    success: bool = True
    joined_count: Optional[int] = None
    error: Optional[str] = None


class NameFileCreate(BaseModel):
    name: str


class NameFileRename(BaseModel):
    name: str


class NameFileSave(BaseModel):
    content: str  # raw text, one name per line


class NameFileOut(BaseModel):
    id: str
    name: str
    count: int
    updated_at: str


# ---------------- Default name pools (built-in) ----------------
def _load_names_in() -> List[str]:
    try:
        with open(ROOT_DIR / "data_names_in.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception as e:
        log_msg = f"could not load NamesIn pool: {e}"
        print(log_msg)
    return []


BUILTIN_NAMES = {
    "NamesIn": _load_names_in() or [
        "User1", "User2", "User3"
    ],
    "Indian": [
        "Aarav", "Vihaan", "Aditya", "Vivaan", "Arjun", "Sai", "Reyansh", "Ayaan", "Krishna", "Ishaan",
        "Ananya", "Diya", "Aadhya", "Pari", "Aanya", "Saanvi", "Myra", "Anika", "Kiara", "Navya",
        "Rohan", "Karan", "Manish", "Rahul", "Amit", "Vikram", "Suresh", "Ramesh", "Deepak", "Sandeep",
        "Priya", "Neha", "Pooja", "Sneha", "Riya", "Nisha", "Anjali", "Kavya", "Meera", "Shruti",
    ],
    "English": [
        "John", "Michael", "David", "James", "Robert", "William", "Daniel", "Joseph", "Thomas", "Charles",
        "Emily", "Emma", "Olivia", "Sophia", "Ava", "Mia", "Isabella", "Charlotte", "Amelia", "Harper",
    ],
}

# ---------------- Auth Routes ----------------
@api.post("/auth/login", response_model=UserOut)
async def login(payload: LoginIn, response: Response, request: Request):
    email = payload.email.lower().strip()
    # Behind a load balancer / K8s ingress the direct connection is the LB,
    # so request.client.host is the same for every user → one failure locks
    # everyone out. Prefer the real client IP from X-Forwarded-For / X-Real-IP.
    xff = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or ""
    real_ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "x")
    identifier = f"{real_ip}:{email}"

    # brute force gate
    lock = await db.login_attempts.find_one({"identifier": identifier})
    if lock and lock.get("locked_until"):
        locked_until = datetime.fromisoformat(lock["locked_until"])
        if locked_until > datetime.now(timezone.utc):
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")

    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user["password_hash"]):
        await db.login_attempts.update_one(
            {"identifier": identifier},
            {"$inc": {"count": 1}, "$set": {
                "locked_until": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
                if (lock and lock.get("count", 0) + 1 >= 5) else None
            }},
            upsert=True,
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")

    await db.login_attempts.delete_one({"identifier": identifier})

    access = create_access_token(user["id"], user["email"])
    refresh = create_refresh_token(user["id"])
    set_auth_cookies(response, access, refresh)

    return UserOut(
        id=user["id"], email=user["email"], name=user["name"], role=user["role"],
        usage=user.get("usage", 0), usage_limit=user.get("usage_limit", int(os.environ.get("USAGE_LIMIT", 15000))),
        credit_rate=float(user.get("credit_rate", 1.0)),
    )


@api.get("/auth/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return UserOut(
        id=user["id"], email=user["email"], name=user["name"], role=user["role"],
        usage=user.get("usage", 0), usage_limit=user.get("usage_limit", int(os.environ.get("USAGE_LIMIT", 15000))),
        credit_rate=float(user.get("credit_rate", 1.0)),
    )


@api.post("/auth/logout")
async def logout(response: Response, _: dict = Depends(get_current_user)):
    clear_auth_cookies(response)
    return {"ok": True}


@api.post("/auth/refresh")
async def refresh(request: Request, response: Response):
    rt = request.cookies.get("refresh_token")
    if not rt:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        payload = jwt.decode(rt, jwt_secret(), algorithms=[JWT_ALG])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"id": payload["sub"]})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        access = create_access_token(user["id"], user["email"])
        response.set_cookie("access_token", access, httponly=True, secure=False, samesite="lax", max_age=12 * 3600, path="/")
        return {"ok": True}
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")


# ---------------- Tasks Routes ----------------
def _task_from_doc(d: dict) -> dict:
    return {
        "id": d["id"],
        "user_id": d["user_id"],
        "meeting_id": d["meeting_id"],
        "meeting_password": d.get("meeting_password", ""),
        "members": d["members"],
        "name_source": d["name_source"],
        "meeting_type": d["meeting_type"],
        "timeout": d["timeout"],
        "floating_emoji": d.get("floating_emoji", False),
        "participant_reactions": d.get("participant_reactions", False),
        "reaction_interval_min": d.get("reaction_interval_min", 30),
        "reaction_interval_max": d.get("reaction_interval_max", 90),
        "status": d["status"],
        "scheduled_at": d.get("scheduled_at"),
        "started_at": d.get("started_at"),
        "ends_at": d.get("ends_at"),
        "completed_at": d.get("completed_at"),
        "created_at": d["created_at"],
        "worker_id": d.get("worker_id"),
        "worker_name": d.get("worker_name"),
        "joined_count": d.get("joined_count", 0),
        "last_progress_at": d.get("last_progress_at"),
        "error": d.get("error"),
    }


@api.post("/tasks/validate-credentials")
async def validate_credentials_endpoint(payload: dict, user: dict = Depends(get_current_user)):
    """Live validation endpoint: returns {ok: true} or 400 with a 'Wrong Meeting ...' detail.
    Used by the Create Task form to detect a wrong meeting ID / password before submission.
    """
    mid = payload.get("meeting_id", "")
    pwd = payload.get("meeting_password", "") or ""
    validate_zoom_credentials(mid, pwd)
    return {"ok": True, "meeting_id": normalize_meeting_id(mid)}


@api.post("/tasks", response_model=TaskOut)
async def create_task(payload: TaskCreate, user: dict = Depends(get_current_user)):
    # Validate Zoom meeting credentials format (detects wrong meeting ID / password early)
    validate_zoom_credentials(payload.meeting_id, payload.meeting_password or "")
    # Normalize meeting id (strip spaces/dashes) so workers always get clean digits
    payload.meeting_id = normalize_meeting_id(payload.meeting_id)
    usage = user.get("usage", 0)
    limit = user.get("usage_limit", int(os.environ.get("USAGE_LIMIT", 15000)))
    if usage + payload.members > limit:
        raise HTTPException(status_code=400, detail=f"Usage limit exceeded ({usage}/{limit})")

    now = datetime.now(timezone.utc)
    tid = str(uuid.uuid4())

    if payload.scheduled_at:
        try:
            # Frontend sends ISO string (already converted to UTC)
            sched = datetime.fromisoformat(payload.scheduled_at.replace("Z", "+00:00"))
            if sched.tzinfo is None:
                sched = sched.replace(tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid scheduled_at format")
        status = "scheduled"
        started_at = None
        ends_at = None
    else:
        sched = None
        status = "active"
        started_at = now
        ends_at = now + timedelta(seconds=payload.timeout)

    doc = {
        "id": tid,
        "user_id": user["id"],
        "meeting_id": payload.meeting_id,
        "meeting_password": payload.meeting_password,
        "members": payload.members,
        "members_claimed": 0,
        "name_source": payload.name_source,
        "meeting_type": payload.meeting_type,
        "timeout": payload.timeout,
        "floating_emoji": payload.floating_emoji,
        "participant_reactions": payload.participant_reactions,
        "reaction_interval_min": int(payload.reaction_interval_min),
        "reaction_interval_max": int(max(payload.reaction_interval_max, payload.reaction_interval_min)),
        "status": status,
        "scheduled_at": sched.isoformat() if sched else None,
        "started_at": started_at.isoformat() if started_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
        "completed_at": None,
        "created_at": now.isoformat(),
    }
    await db.tasks.insert_one(doc)

    # increment usage on creation (matches "Usage: x / 15000" pattern)
    await db.users.update_one({"id": user["id"]}, {"$inc": {"usage": payload.members}})

    return TaskOut(**_task_from_doc(doc))


@api.get("/tasks/active", response_model=List[TaskOut])
async def list_active(user: dict = Depends(get_current_user)):
    docs = await db.tasks.find(
        {"user_id": user["id"], "status": "active"}, {"_id": 0}
    ).sort("started_at", -1).to_list(500)
    return [TaskOut(**_task_from_doc(d)) for d in docs]


@api.get("/tasks/scheduled", response_model=List[TaskOut])
async def list_scheduled(user: dict = Depends(get_current_user)):
    docs = await db.tasks.find(
        {"user_id": user["id"], "status": "scheduled"}, {"_id": 0}
    ).sort("scheduled_at", 1).to_list(500)
    return [TaskOut(**_task_from_doc(d)) for d in docs]


@api.get("/tasks/previous", response_model=List[TaskOut])
async def list_previous(date: Optional[str] = Query(None), user: dict = Depends(get_current_user)):
    q: dict = {"user_id": user["id"], "status": {"$in": ["completed", "failed", "cancelled"]}}
    if date:
        try:
            d = datetime.fromisoformat(date).date()
            start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()
            end = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)).isoformat()
            q["completed_at"] = {"$gte": start, "$lt": end}
        except Exception:
            pass
    docs = await db.tasks.find(q, {"_id": 0}).sort("completed_at", -1).to_list(1000)
    return [TaskOut(**_task_from_doc(d)) for d in docs]


@api.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: str, user: dict = Depends(get_current_user)):
    doc = await db.tasks.find_one({"id": task_id, "user_id": user["id"]}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Task not found")
    if doc["status"] in ("completed", "failed", "cancelled"):
        return TaskOut(**_task_from_doc(doc))
    now = datetime.now(timezone.utc).isoformat()
    await db.tasks.update_one(
        {"id": task_id},
        {"$set": {"status": "cancelled", "completed_at": now}},
    )
    # Cascade: mark all chunks cancelled so workers stop spawning new bots
    await db.task_chunks.update_many(
        {"task_id": task_id, "status": "active"},
        {"$set": {"status": "cancelled", "completed_at": now}},
    )
    doc["status"] = "cancelled"
    doc["completed_at"] = now
    return TaskOut(**_task_from_doc(doc))


@api.delete("/tasks/{task_id}")
async def delete_task(task_id: str, user: dict = Depends(get_current_user)):
    res = await db.tasks.delete_one({"id": task_id, "user_id": user["id"]})
    if res.deleted_count == 0:
        raise HTTPException(404, "Task not found")
    return {"ok": True}


@api.post("/tasks/bulk-delete")
async def bulk_delete(ids: List[str], user: dict = Depends(get_current_user)):
    res = await db.tasks.delete_many({"id": {"$in": ids}, "user_id": user["id"]})
    return {"deleted": res.deleted_count}


# ---------------- Workers ----------------
import secrets as _secrets


def _effective_capacity(w: dict) -> int:
    """Return the live safe capacity for a worker.

    Policy (v8.3.6 — STRICT ADMIN-ONLY CEILING, ULTRA MODE):
      - `capacity_max` (set by admin in dashboard) is the ONLY source of truth
        for scheduling. The scheduler assigns EXACTLY up to this many bots —
        never more, never less.
      - `reported_capacity` (auto-computed by worker from RAM/CPU) is kept as
        telemetry only (shown on dashboard) but is IGNORED by the scheduler.
        Rationale: admin paid for an RDP capable of N members, so the system
        must honour N strictly. "Auto-shrinking" caused under-utilization
        and unwanted "auto-limited" badges.
      - If admin wants "unlimited", they set capacity_max to a big number (e.g. 5000).
    """
    return max(0, int(w.get("capacity_max", 100)))


def _worker_out(w: dict) -> dict:
    return {
        "id": w["id"],
        "name": w["name"],
        "status": _worker_status(w),
        "capacity_max": w.get("capacity_max", 100),
        "reported_capacity": w.get("reported_capacity"),
        "current_load": w.get("current_load", 0),
        "cpu_pct": float(w.get("cpu_pct", 0.0)),
        "ram_pct": float(w.get("ram_pct", 0.0)),
        "ram_free_gb": w.get("ram_free_gb"),
        "cpu_count": w.get("cpu_count"),
        "hostname": w.get("hostname"),
        "os_info": w.get("os_info"),
        "last_heartbeat": w.get("last_heartbeat"),
        "created_at": w["created_at"],
        "pool_stats": w.get("pool_stats"),
        "crash_count": int(w.get("crash_count", 0)),
        "last_restart_at": w.get("last_restart_at"),
        "worker_started_at": w.get("worker_started_at"),
    }


def _worker_status(w: dict) -> str:
    lh = w.get("last_heartbeat")
    if not lh:
        return "offline"
    try:
        last = datetime.fromisoformat(lh)
        if (datetime.now(timezone.utc) - last).total_seconds() < 30:
            return "online"
    except Exception:
        pass
    return "offline"


# ---- Online worker count cache (5s TTL) ----
# Avoids re-running count_documents on every claim across all 30+ workers.
_online_count_cache = {"value": 0, "expires_at": 0.0}
_online_capacity_cache = {"total": 0, "expires_at": 0.0}


async def _get_online_worker_count(now_dt: datetime) -> int:
    """Returns approximate count of online workers (heartbeat in last 30s),
    cached for 5 seconds to avoid hammering MongoDB when many workers poll.
    Two-tier cache: Redis (cluster-wide) → in-process (single-instance).
    """
    import time as _time
    now_ts = _time.time()
    if now_ts < _online_count_cache["expires_at"]:
        return _online_count_cache["value"]
    # Try redis first (cluster-wide)
    cached = await cache_get("fleet:online_count")
    if cached is not None:
        try:
            v = int(cached)
            _online_count_cache["value"] = v
            _online_count_cache["expires_at"] = now_ts + 5.0
            return v
        except Exception:
            pass
    threshold = (now_dt - timedelta(seconds=30)).isoformat()
    cnt = await db.workers.count_documents({"last_heartbeat": {"$gte": threshold}})
    _online_count_cache["value"] = cnt
    _online_count_cache["expires_at"] = now_ts + 5.0
    await cache_set("fleet:online_count", str(cnt), ex=5)
    return cnt


async def _get_online_total_capacity(now_dt: datetime) -> int:
    """Sum of ADMIN-SET capacity across all online workers (heartbeat in last 30s).
    v8.3.6: STRICT admin-only mode — we sum `capacity_max` directly and ignore
    `reported_capacity` entirely. The admin's setting IS the capacity, period.
    Cached 5 s + Redis cluster cache. Used for capacity-weighted fair share so
    a 200-cap RDP gets more bots than a 50-cap RDP for the same task."""
    import time as _time
    now_ts = _time.time()
    if now_ts < _online_capacity_cache["expires_at"]:
        return _online_capacity_cache["total"]
    cached = await cache_get("fleet:online_capacity")
    if cached is not None:
        try:
            v = int(cached)
            _online_capacity_cache["total"] = v
            _online_capacity_cache["expires_at"] = now_ts + 5.0
            return v
        except Exception:
            pass
    threshold = (now_dt - timedelta(seconds=30)).isoformat()
    # v8.3.6: ONLY admin-set capacity_max counts. reported_capacity is telemetry only.
    pipeline = [
        {"$match": {"last_heartbeat": {"$gte": threshold}}},
        {"$group": {
            "_id": None,
            "total": {"$sum": {"$ifNull": ["$capacity_max", 0]}},
        }},
    ]
    res = await db.workers.aggregate(pipeline).to_list(1)
    total = int(res[0]["total"]) if res else 0
    _online_capacity_cache["total"] = total
    _online_capacity_cache["expires_at"] = now_ts + 5.0
    await cache_set("fleet:online_capacity", str(total), ex=5)
    return total


@api.post("/workers", response_model=WorkerCreatedOut)
async def create_worker(payload: WorkerCreate, user: dict = Depends(get_admin_user)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Worker name is required")
    exists = await db.workers.find_one({"name": name})
    if exists:
        raise HTTPException(400, "A worker with that name already exists")
    wid = str(uuid.uuid4())
    secret = _secrets.token_urlsafe(32)
    doc = {
        "id": wid,
        "user_id": user["id"],  # owner = admin
        "name": name,
        "token_hash": _hash_token(secret),
        "capacity_max": int(payload.capacity_max or 100),
        "current_load": 0,
        "cpu_pct": 0.0,
        "ram_pct": 0.0,
        "hostname": None,
        "os_info": None,
        "last_heartbeat": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.workers.insert_one(doc)
    out = _worker_out(doc)
    out["token"] = f"{wid}.{secret}"
    return out


@api.get("/workers", response_model=List[WorkerOut])
async def list_workers(_: dict = Depends(get_admin_user)):
    docs = await db.workers.find({}, {"_id": 0, "token_hash": 0}).sort("created_at", -1).to_list(500)
    return [_worker_out(d) for d in docs]


@api.delete("/workers/{worker_id}")
async def delete_worker(worker_id: str, _: dict = Depends(get_admin_user)):
    res = await db.workers.delete_one({"id": worker_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Worker not found")
    # Unassign any tasks owned by this worker
    await db.tasks.update_many(
        {"worker_id": worker_id, "status": "active"},
        {"$set": {"worker_id": None, "worker_name": None}},
    )
    return {"ok": True}


class WorkerUpdate(BaseModel):
    name: Optional[str] = None
    capacity_max: Optional[int] = None


@api.patch("/workers/{worker_id}", response_model=WorkerOut)
async def update_worker(worker_id: str, payload: WorkerUpdate, _: dict = Depends(get_admin_user)):
    w = await db.workers.find_one({"id": worker_id}, {"_id": 0})
    if not w:
        raise HTTPException(404, "Worker not found")
    updates: dict = {}
    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(400, "Worker name cannot be empty")
        dup = await db.workers.find_one({"name": new_name, "id": {"$ne": worker_id}})
        if dup:
            raise HTTPException(400, "A worker with that name already exists")
        updates["name"] = new_name
    if payload.capacity_max is not None:
        cap = int(payload.capacity_max)
        if cap < 1 or cap > 5000:
            raise HTTPException(400, "capacity_max must be between 1 and 5000")
        updates["capacity_max"] = cap
    if updates:
        await db.workers.update_one({"id": worker_id}, {"$set": updates})
        w.update(updates)
    return _worker_out(w)


# Worker-side endpoints (authenticated via Bearer worker token)
@api.post("/workers/me/heartbeat", response_model=WorkerOut)
async def worker_heartbeat(payload: HeartbeatIn, w: dict = Depends(get_current_worker)):
    now = datetime.now(timezone.utc).isoformat()
    updates = {
        "current_load": int(payload.current_load),
        "cpu_pct": float(payload.cpu_pct),
        "ram_pct": float(payload.ram_pct),
        "last_heartbeat": now,
    }
    if payload.hostname:
        updates["hostname"] = payload.hostname
    if payload.os_info:
        updates["os_info"] = payload.os_info
    # v8.3.6 STRICT MODE: Worker-reported live safe capacity is stored as
    # TELEMETRY ONLY (shown on dashboard). The scheduler IGNORES it and
    # honours the admin's `capacity_max` strictly. This prevents the system
    # from auto-shrinking the admin's chosen limit.
    if payload.reported_capacity is not None:
        rep = max(0, int(payload.reported_capacity))
        updates["reported_capacity"] = rep
    if payload.ram_free_gb is not None:
        updates["ram_free_gb"] = float(payload.ram_free_gb)
    if payload.cpu_count is not None:
        updates["cpu_count"] = int(payload.cpu_count)
    if payload.pool_stats is not None:
        # Pool prewarm telemetry (browsers, ready_contexts, total_bots, prewarmed)
        updates["pool_stats"] = payload.pool_stats
    # Worker-side keep-alive supervisor telemetry. Monotonic counter that only
    # ever increases per worker boot; resets when the operator restarts the
    # script. Lets the dashboard flag unstable RDPs at a glance.
    if payload.crash_count is not None:
        updates["crash_count"] = max(0, int(payload.crash_count))
    if payload.last_restart_at:
        updates["last_restart_at"] = payload.last_restart_at
    if payload.worker_started_at:
        # First heartbeat after a process restart sets this; we keep the
        # earliest known boot-time (don't overwrite if already set this run).
        if not w.get("worker_started_at") or w.get("worker_started_at") != payload.worker_started_at:
            updates["worker_started_at"] = payload.worker_started_at
    await db.workers.update_one({"id": w["id"]}, {"$set": updates})
    w.update(updates)
    return _worker_out(w)


@api.get("/workers/me")
async def worker_self(w: dict = Depends(get_current_worker)):
    return _worker_out(w)


@api.get("/tasks/{task_id}/chunk-status")
async def worker_chunk_status(task_id: str, w: dict = Depends(get_current_worker)):
    """Worker polls this to detect if its chunk (or parent task) was cancelled
    so it can stop spawning more bots and tear down quickly."""
    chunk = await db.task_chunks.find_one(
        {"task_id": task_id, "worker_id": w["id"]},
        sort=[("started_at", -1)],
        projection={"_id": 0, "status": 1, "members": 1, "joined_count": 1},
    )
    task = await db.tasks.find_one({"id": task_id}, {"_id": 0, "status": 1})
    return {
        "chunk_status": chunk.get("status") if chunk else "unknown",
        "task_status": task.get("status") if task else "unknown",
        "members_assigned": chunk.get("members") if chunk else 0,
    }


# Distribution mode:
#   "round_robin" (default v8.4) — WAVE-BASED EVEN FILL. Each worker is capped
#              at ceil((task.members_claimed + 1) / online_count) bots per
#              task at any moment. Result: with 30 RDPs online, the FIRST 30
#              bots are assigned 1 per RDP (wave 1); next 30 bring each RDP to
#              2 bots (wave 2); etc. Guarantees no RDP receives a 2nd bot
#              until every other RDP has its 1st.
#   "auto"   — auto-detects best strategy:
#              • If all online RDPs have the SAME effective capacity (homogeneous
#                fleet → typical user case: 30 identical RDPs), it does a strict
#                EQUAL split: each RDP gets ceil(members / online_count) bots.
#                Example: 1000 bots ÷ 30 RDPs = 34 per RDP — none starve.
#              • If RDPs are heterogeneous (different RAM/CPU), it switches to
#                capacity-weighted fair share so beefy RDPs do more work.
#   "weighted" — always capacity-weighted (old fair-share).
#   "even"     — always strict equal split (ignores capacity).
#   "greedy"   — old behaviour: one RDP fills up before the next gets anything.
# Switch via env var DISTRIBUTION_MODE on backend .env.
DISTRIBUTION_MODE = os.environ.get("DISTRIBUTION_MODE", "round_robin").lower()
# v8.4: hard cap on bots a single worker can take per claim cycle in round_robin
# mode. Keeps the wave pattern visible even when one worker polls 3x faster.
ROUND_ROBIN_TAKE_PER_CYCLE = int(os.environ.get("ROUND_ROBIN_TAKE_PER_CYCLE", "1"))


@api.post("/workers/me/claim")
async def worker_claim_tasks(
    max_tasks: int = Query(5, ge=1, le=50),
    w: dict = Depends(get_current_worker),
):
    """Load-balanced claim — splits big tasks EQUALLY across all online workers.

    Strategy (DISTRIBUTION_MODE=auto, default):
    - Count online workers (heartbeat within last 30s).
    - fair_share = ceil(task.members / online_count). Each worker takes at most
      this many members per task per claim cycle. A 1000-bot task with 30 online
      RDPs splits as ~34 bots per RDP — no RDP starves.
    - Capacity (RAM/CPU-derived) and remaining are upper bounds.
    - If no worker has claimed from the task for >45s (MOPUP_STALL_SECS), the
      MOP-UP path kicks in: workers may take up to 2× fair_share so the task
      can finish even when some RDPs crash mid-claim.
    """
    claimed: list[dict] = []
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    # v8.3.6 STRICT: effective_capacity == admin's capacity_max (no auto-override).
    # If admin set 1, this RDP gets exactly 1 bot — never more, never less.
    effective_cap = _effective_capacity(w)
    capacity_left = max(0, effective_cap - w.get("current_load", 0))
    if capacity_left <= 0:
        return {"tasks": []}

    # FAST PATH: if no active task exists at all, return immediately.
    # This avoids running the expensive count + per-attempt aggregations
    # 30 workers × every 5s when there's nothing to do.
    any_active = await db.tasks.find_one(
        {"status": "active", "$expr": {"$lt": [{"$ifNull": ["$members_claimed", 0]}, "$members"]}},
        projection={"_id": 0, "id": 1},
    )
    if not any_active:
        return {"tasks": []}

    # Count workers that heartbeated within the last 30s (treat them as "online")
    # We include the current worker since it's actively polling right now.
    online_count = await _get_online_worker_count(now_dt)
    if online_count < 1:
        online_count = 1
    # Capacity-weighted: bigger RDPs (8CPU/64GB → effective_cap 200) get more bots
    # than small RDPs (2CPU/4GB → effective_cap 30) for the same task.
    online_total_capacity = await _get_online_total_capacity(now_dt)
    if online_total_capacity < 1:
        online_total_capacity = max(1, effective_cap)
    my_capacity = max(1, effective_cap)

    # Loop until we run out of capacity or there are no more claimable members
    attempts = 0
    while len(claimed) < max_tasks and capacity_left > 0 and attempts < max_tasks * 3:
        attempts += 1
        # Find a task with members still unclaimed (members_claimed < members)
        task = await db.tasks.find_one(
            {
                "status": "active",
                "$expr": {"$lt": [{"$ifNull": ["$members_claimed", 0]}, "$members"]},
            },
            sort=[("started_at", 1)],
            projection={"_id": 0},
        )
        if not task:
            break

        old_claimed = task.get("members_claimed", 0)
        remaining = task["members"] - old_claimed

        # EQUAL DISTRIBUTION: every online worker should take ~ members/online_count
        # Compute how much THIS worker has already claimed for THIS task so we
        # don't keep grabbing more than our fair share on repeated polls.
        my_existing = await db.task_chunks.aggregate([
            {"$match": {"task_id": task["id"], "worker_id": w["id"]}},
            {"$group": {"_id": None, "total": {"$sum": "$members"}}},
        ]).to_list(1)
        my_already = my_existing[0]["total"] if my_existing else 0

        # ---- Compute fair share based on the active DISTRIBUTION_MODE ----
        # "auto"/"even": strict EQUAL split → ceil(members / online_count). This
        #                is what the user wants for 30 identical RDPs (1000 ÷ 30
        #                ≈ 34 bots each). Capacity is still respected as a cap.
        # "weighted":    capacity-weighted (bigger RDPs do more work).
        # "greedy":      one RDP fills up before the next gets any work.
        equal_share = math.ceil(task["members"] / online_count)
        weighted_share = math.ceil(task["members"] * (my_capacity / online_total_capacity))

        if DISTRIBUTION_MODE == "weighted":
            fair_share_total = weighted_share
        else:
            # auto / even / round_robin / anything else → strict equal split.
            # We also clamp by capacity_left so a small RDP never gets more
            # than it can hold.
            fair_share_total = equal_share

        # v8.4: ROUND-ROBIN / WAVE OVERLAY
        # In round_robin mode we OVERRIDE fair_share_total with a wave cap that
        # depends on how many bots have already been claimed across the fleet.
        # wave_cap = ceil((already_claimed_total + 1) / online_count)
        # → Wave 1: each worker max 1 bot (until all `online_count` claimed).
        # → Wave 2: each worker max 2 bots (until 2×online_count claimed).
        # → ... and so on. Gives a clean visual: every RDP joins 1 first.
        rr_take_cap = None
        if DISTRIBUTION_MODE == "round_robin":
            wave_cap = math.ceil((old_claimed + 1) / online_count)
            fair_share_total = wave_cap
            # Also clamp the per-cycle take so one worker can't grab 5+ bots
            # at once and break the wave pattern.
            rr_take_cap = max(1, ROUND_ROBIN_TAKE_PER_CYCLE)

        # Guarantee at least 1 bot per worker if it has capacity_left and the
        # task has unclaimed members — prevents tiny tasks (e.g. 10 bots on
        # 30 workers) from leaving most workers idle while a few grab all.
        fair_share_total = max(fair_share_total, 1)

        # v8.4.1 HARD PER-TASK PER-WORKER CEILING:
        # A worker must NEVER claim more bots from a single task than its admin
        # capacity_max. Without this, when bots leave naturally the worker's
        # current_load drops and the server happily ships the remaining bots
        # to the same RDP — overshooting its limit.
        # Example: 1 RDP cap=80, task=100 → first claim 80, then as bots leave
        # the server was re-claiming the remaining 20 onto the same RDP.
        # With this ceiling, the RDP gets exactly 80 from this task, ever.
        per_task_worker_ceiling = min(effective_cap, fair_share_total)
        fair_share_left = max(0, per_task_worker_ceiling - my_already)

        # MOP-UP: only triggers if the task has clearly stalled. We track the
        # task's `last_claim_at` (updated below on every successful claim). If
        # no worker has claimed for MOP_UP_STALL_SECS (default 45s), we assume
        # some workers crashed and release the cap — but only up to 2× the
        # fair share per worker so distribution stays reasonable.
        task_age = 0.0
        try:
            task_started = datetime.fromisoformat(task.get("started_at", now_iso))
            task_age = (now_dt - task_started).total_seconds()
        except Exception:
            pass

        last_claim_at_raw = task.get("last_claim_at") or task.get("started_at")
        secs_since_last_claim = task_age
        try:
            if last_claim_at_raw:
                last_claim_dt = datetime.fromisoformat(last_claim_at_raw)
                secs_since_last_claim = (now_dt - last_claim_dt).total_seconds()
        except Exception:
            pass
        mopup_stall_secs = int(os.environ.get("MOPUP_STALL_SECS", "45"))
        in_mopup = secs_since_last_claim > mopup_stall_secs

        if DISTRIBUTION_MODE == "greedy":
            # Sequential fill: this worker takes as much as it can RIGHT NOW —
            # but STILL respect the per-task per-worker ceiling so a single
            # RDP can't blow past its admin-set capacity_max on one task.
            greedy_room = max(0, per_task_worker_ceiling - my_already)
            take = min(remaining, capacity_left, greedy_room)
        elif in_mopup:
            # MOP-UP: task is stalled (no claims for >stall_secs). Release the
            # strict fair-share cap but keep a soft 2× fair_share limit so a
            # single worker can't slurp all remaining bots. Per-worker ceiling
            # still applies — capacity_max is sacred.
            soft_cap = max(1, fair_share_total * 2)
            mopup_room = max(0, per_task_worker_ceiling - my_already)
            take = min(remaining, capacity_left, soft_cap, mopup_room)
        else:
            if fair_share_left <= 0:
                # Already took our fair share for this task — try next task in
                # the queue instead of looping on the same one.
                break
            take = min(remaining, capacity_left, fair_share_left)
            # v8.4: in round_robin mode, also enforce per-cycle hard cap so
            # multiple polls don't let one worker leapfrog its peers.
            if rr_take_cap is not None:
                take = min(take, rr_take_cap)
        log.debug(f"[CLAIM] w={w['name']} task={task['id'][:8]} online={online_count} eq_share={equal_share} my_already={my_already} cap_left={capacity_left} rem={remaining} age={task_age:.1f}s mopup={in_mopup} take={take}")

        if take <= 0:
            break

        # ATOMIC lock-free claim using $inc with overshoot rollback.
        # Why not CAS-retry loop? With 30+ concurrent workers, CAS contention
        # caused some workers to lose their fair share entirely.
        # Strategy: unconditional $inc, then if we overshot task.members,
        # rollback just the overshoot amount.
        doc_after = await db.tasks.find_one_and_update(
            {"id": task["id"], "$expr": {"$lt": [{"$ifNull": ["$members_claimed", 0]}, "$members"]}},
            {"$inc": {"members_claimed": take}, "$set": {"last_claim_at": now_iso}},
            return_document=True,
            projection={"_id": 0, "members_claimed": 1, "members": 1},
        )
        if not doc_after:
            # Task fully claimed by other workers — try next task
            break

        overshoot = doc_after["members_claimed"] - doc_after["members"]
        if overshoot > 0:
            # We claimed more than what's left — rollback the overshoot
            await db.tasks.update_one(
                {"id": task["id"]},
                {"$inc": {"members_claimed": -overshoot}},
            )
            take = take - overshoot
            if take <= 0:
                # Fully lost the race — nothing left for us
                break

        # Create chunk record (per-worker assignment)
        chunk = {
            "id": str(uuid.uuid4()),
            "task_id": task["id"],
            "user_id": task["user_id"],
            "worker_id": w["id"],
            "worker_name": w["name"],
            "members": take,
            "joined_count": 0,
            "status": "active",
            "started_at": now_iso,
            "completed_at": None,
            "error": None,
        }
        await db.task_chunks.insert_one(chunk)

        # On first chunk, also mark the parent task with this worker_id/name for backward compat
        if old_claimed == 0:
            await db.tasks.update_one(
                {"id": task["id"]},
                {"$set": {"worker_id": w["id"], "worker_name": w["name"], "last_progress_at": now_iso}},
            )

        # Resolve names ONLY for this chunk size
        names = await _resolve_names(task["user_id"], task["name_source"], take)

        out = _task_from_doc(task)
        out["members"] = take         # worker only spawns this many
        out["names"] = names
        out["chunk_id"] = chunk["id"]
        claimed.append(out)
        capacity_left -= take

    return {"tasks": claimed}


@api.patch("/tasks/{task_id}/progress", response_model=TaskOut)
async def report_progress(task_id: str, payload: TaskProgressIn, w: dict = Depends(get_current_worker)):
    now = datetime.now(timezone.utc).isoformat()
    # Update THIS worker's chunk
    res = await db.task_chunks.find_one_and_update(
        {"task_id": task_id, "worker_id": w["id"], "status": {"$in": ["active", "completed"]}},
        {"$set": {"joined_count": int(payload.joined_count)}},
        sort=[("started_at", -1)],
        return_document=True,
        projection={"_id": 0},
    )
    if not res:
        # Backward compat: no chunk yet (very old tasks) — fall back to old direct field
        doc = await db.tasks.find_one_and_update(
            {"id": task_id, "worker_id": w["id"]},
            {"$set": {"joined_count": int(payload.joined_count), "last_progress_at": now}},
            return_document=True,
            projection={"_id": 0},
        )
        if not doc:
            raise HTTPException(404, "Task not found or not assigned to this worker")
        return TaskOut(**_task_from_doc(doc))

    # Update parent task's aggregated joined_count + last_progress_at
    agg = await db.task_chunks.aggregate([
        {"$match": {"task_id": task_id}},
        {"$group": {"_id": "$task_id", "joined": {"$sum": "$joined_count"}}},
    ]).to_list(1)
    total_joined = agg[0]["joined"] if agg else 0
    await db.tasks.update_one(
        {"id": task_id},
        {"$set": {"joined_count": total_joined, "last_progress_at": now}},
    )
    doc = await db.tasks.find_one({"id": task_id}, {"_id": 0})
    return TaskOut(**_task_from_doc(doc))


@api.post("/tasks/{task_id}/complete", response_model=TaskOut)
async def worker_complete_task(task_id: str, payload: TaskCompleteIn, w: dict = Depends(get_current_worker)):
    now = datetime.now(timezone.utc).isoformat()
    new_status = "completed" if payload.success else "failed"

    # Mark THIS worker's chunk complete
    chunk_updates = {"status": new_status, "completed_at": now}
    if payload.joined_count is not None:
        chunk_updates["joined_count"] = int(payload.joined_count)
    if payload.error:
        chunk_updates["error"] = payload.error

    res = await db.task_chunks.find_one_and_update(
        {"task_id": task_id, "worker_id": w["id"], "status": "active"},
        {"$set": chunk_updates},
        sort=[("started_at", -1)],
        return_document=True,
        projection={"_id": 0},
    )
    if not res:
        # Backward compat: no chunk — old task with direct worker_id
        doc = await db.tasks.find_one({"id": task_id, "worker_id": w["id"]}, {"_id": 0})
        if not doc:
            raise HTTPException(404, "Task not found or not assigned to this worker")
        updates = {"status": new_status, "completed_at": now}
        if payload.joined_count is not None: updates["joined_count"] = int(payload.joined_count)
        if payload.error: updates["error"] = payload.error
        await db.tasks.update_one({"id": task_id}, {"$set": updates})
        doc.update(updates)
        return TaskOut(**_task_from_doc(doc))

    # Recompute parent task status from all chunks (use projection to avoid huge fields)
    chunks = await db.task_chunks.find(
        {"task_id": task_id},
        {"_id": 0, "status": 1, "joined_count": 1, "members": 1, "worker_name": 1},
    ).to_list(500)
    total_joined = sum(c.get("joined_count", 0) for c in chunks)
    total_assigned = sum(c.get("members", 0) for c in chunks)
    all_done = all(c.get("status") in ("completed", "failed", "cancelled") for c in chunks)

    parent = await db.tasks.find_one({"id": task_id}, {"_id": 0})
    task_status = parent["status"]
    completed_at = parent.get("completed_at")

    # Mark parent complete when all chunks done AND all members were claimed
    if all_done and total_assigned >= parent.get("members", 0):
        # If any chunk failed and none succeeded, mark failed; else completed
        any_success = any(c.get("status") == "completed" for c in chunks)
        task_status = "completed" if any_success else "failed"
        completed_at = now

    # Worker name shown on parent = list of distinct workers (or first)
    worker_names = list({c["worker_name"] for c in chunks if c.get("worker_name")})
    primary_worker = ", ".join(worker_names) if len(worker_names) > 1 else (worker_names[0] if worker_names else parent.get("worker_name"))

    await db.tasks.update_one(
        {"id": task_id},
        {"$set": {
            "status": task_status,
            "joined_count": total_joined,
            "completed_at": completed_at,
            "worker_name": primary_worker,
            "last_progress_at": now,
        }},
    )
    parent = await db.tasks.find_one({"id": task_id}, {"_id": 0})
    return TaskOut(**_task_from_doc(parent))


async def _resolve_names(user_id: str, source: str, count: int) -> List[str]:
    """
    Return `count` names from the requested pool — RANDOMLY shuffled, with NO repeats
    until the pool is exhausted. If `count` exceeds pool size, the names cycle through
    a fresh random shuffle for each pass.
    """
    import random as _random

    pool: List[str] = []
    if source in BUILTIN_NAMES:
        pool = list(BUILTIN_NAMES[source])
    else:
        f = await db.name_files.find_one(
            {"user_id": user_id, "$or": [{"id": source}, {"name": source}]},
            {"_id": 0, "names": 1},
        )
        if f and f.get("names"):
            pool = list(f["names"])
    if not pool:
        pool = list(BUILTIN_NAMES.get("NamesIn") or BUILTIN_NAMES.get("Indian") or ["User"])

    # Random unique selection
    if count <= len(pool):
        return _random.sample(pool, count)

    # count > pool size: fill with multiple shuffled passes (each pass is a fresh random shuffle)
    out: List[str] = []
    while len(out) < count:
        shuffled = pool[:]
        _random.shuffle(shuffled)
        remaining = count - len(out)
        out.extend(shuffled[:remaining])
    return out


# ---------------- Custom Name Files ----------------
@api.get("/name-files", response_model=List[NameFileOut])
async def list_name_files(user: dict = Depends(get_current_user)):
    docs = await db.name_files.find({"user_id": user["id"]}, {"_id": 0}).sort("name", 1).to_list(500)
    return [NameFileOut(id=d["id"], name=d["name"], count=len(d.get("names", [])), updated_at=d["updated_at"]) for d in docs]


@api.get("/name-files/builtin")
async def builtin_pools(_: dict = Depends(get_current_user)):
    return [{"id": k, "name": k, "builtin": True, "count": len(v)} for k, v in BUILTIN_NAMES.items()]


@api.post("/name-files", response_model=NameFileOut)
async def create_name_file(payload: NameFileCreate, user: dict = Depends(get_current_user)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Name is required")
    exists = await db.name_files.find_one({"user_id": user["id"], "name": name})
    if exists:
        raise HTTPException(400, "A file with that name already exists")
    now = datetime.now(timezone.utc).isoformat()
    doc = {"id": str(uuid.uuid4()), "user_id": user["id"], "name": name, "names": [], "updated_at": now}
    await db.name_files.insert_one(doc)
    return NameFileOut(id=doc["id"], name=doc["name"], count=0, updated_at=now)


@api.get("/name-files/{file_id}")
async def get_name_file(file_id: str, user: dict = Depends(get_current_user)):
    doc = await db.name_files.find_one({"id": file_id, "user_id": user["id"]}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Not found")
    return {"id": doc["id"], "name": doc["name"], "content": "\n".join(doc.get("names", [])), "count": len(doc.get("names", [])), "updated_at": doc["updated_at"]}


@api.put("/name-files/{file_id}/rename", response_model=NameFileOut)
async def rename_name_file(file_id: str, payload: NameFileRename, user: dict = Depends(get_current_user)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Name is required")
    doc = await db.name_files.find_one({"id": file_id, "user_id": user["id"]}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Not found")
    dup = await db.name_files.find_one({"user_id": user["id"], "name": name, "id": {"$ne": file_id}})
    if dup:
        raise HTTPException(400, "A file with that name already exists")
    now = datetime.now(timezone.utc).isoformat()
    await db.name_files.update_one({"id": file_id}, {"$set": {"name": name, "updated_at": now}})
    return NameFileOut(id=file_id, name=name, count=len(doc.get("names", [])), updated_at=now)


@api.put("/name-files/{file_id}/content", response_model=NameFileOut)
async def save_name_file(file_id: str, payload: NameFileSave, user: dict = Depends(get_current_user)):
    doc = await db.name_files.find_one({"id": file_id, "user_id": user["id"]}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Not found")
    names = [ln.strip() for ln in payload.content.split("\n") if ln.strip()]
    now = datetime.now(timezone.utc).isoformat()
    await db.name_files.update_one({"id": file_id}, {"$set": {"names": names, "updated_at": now}})
    return NameFileOut(id=file_id, name=doc["name"], count=len(names), updated_at=now)


@api.delete("/name-files/{file_id}")
async def delete_name_file(file_id: str, user: dict = Depends(get_current_user)):
    res = await db.name_files.delete_one({"id": file_id, "user_id": user["id"]})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}


# ---------------- Stats ----------------
@api.get("/stats/usage")
async def usage_stats(user: dict = Depends(get_current_user)):
    fresh = await db.users.find_one({"id": user["id"]}, {"_id": 0, "password_hash": 0})
    return {
        "usage": fresh.get("usage", 0),
        "usage_limit": fresh.get("usage_limit", int(os.environ.get("USAGE_LIMIT", 15000))),
    }


@api.get("/tasks/download")
async def download_tasks(user: dict = Depends(get_current_user)):
    docs = await db.tasks.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    return [_task_from_doc(d) for d in docs]


# ---------------- Admin: User Management ----------------
class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None
    role: str = "user"
    usage_limit: int = 15000
    credit_rate: float = 1.0


class AdminUserUpdate(BaseModel):
    password: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    usage_limit: Optional[int] = None
    credit_rate: Optional[float] = None


class AdminUserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str
    usage: int = 0
    usage_limit: int = 15000
    credit_rate: float = 1.0
    created_at: str


def _user_admin_out(u: dict) -> dict:
    return {
        "id": u["id"],
        "email": u["email"],
        "name": u.get("name", ""),
        "role": u.get("role", "user"),
        "usage": int(u.get("usage", 0)),
        "usage_limit": int(u.get("usage_limit", 15000)),
        "credit_rate": float(u.get("credit_rate", 1.0)),
        "created_at": u.get("created_at", ""),
    }


@api.get("/admin/users", response_model=List[AdminUserOut])
async def admin_list_users(_: dict = Depends(get_admin_user)):
    docs = await db.users.find({}, {"_id": 0, "password_hash": 0}).sort("created_at", -1).to_list(1000)
    return [_user_admin_out(d) for d in docs]


@api.post("/admin/users", response_model=AdminUserOut)
async def admin_create_user(payload: AdminUserCreate, _: dict = Depends(get_admin_user)):
    email = payload.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "A user with that email already exists")
    if len(payload.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if payload.role not in ("admin", "user"):
        raise HTTPException(400, "role must be 'admin' or 'user'")
    doc = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": hash_password(payload.password),
        "name": (payload.name or email.split("@")[0]).strip(),
        "role": payload.role,
        "usage": 0,
        "usage_limit": int(payload.usage_limit),
        "credit_rate": float(payload.credit_rate or 1.0),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(doc)
    return _user_admin_out(doc)


@api.put("/admin/users/{user_id}", response_model=AdminUserOut)
async def admin_update_user(user_id: str, payload: AdminUserUpdate, admin: dict = Depends(get_admin_user)):
    u = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not u:
        raise HTTPException(404, "User not found")
    updates: dict = {}
    if payload.password:
        if len(payload.password) < 6:
            raise HTTPException(400, "Password must be at least 6 characters")
        updates["password_hash"] = hash_password(payload.password)
    if payload.name is not None:
        updates["name"] = payload.name.strip()
    if payload.role is not None:
        if payload.role not in ("admin", "user"):
            raise HTTPException(400, "role must be 'admin' or 'user'")
        # Prevent admin from demoting themselves
        if user_id == admin["id"] and payload.role != "admin":
            raise HTTPException(400, "Cannot demote yourself")
        updates["role"] = payload.role
    if payload.usage_limit is not None:
        updates["usage_limit"] = max(0, int(payload.usage_limit))
    if payload.credit_rate is not None:
        updates["credit_rate"] = max(0.0, float(payload.credit_rate))
    if updates:
        await db.users.update_one({"id": user_id}, {"$set": updates})
        u.update(updates)
    return _user_admin_out(u)


@api.post("/admin/users/{user_id}/reset-usage", response_model=AdminUserOut)
async def admin_reset_usage(user_id: str, _: dict = Depends(get_admin_user)):
    u = await db.users.find_one_and_update(
        {"id": user_id}, {"$set": {"usage": 0}},
        return_document=True, projection={"_id": 0, "password_hash": 0},
    )
    if not u:
        raise HTTPException(404, "User not found")
    return _user_admin_out(u)


@api.delete("/admin/users/{user_id}")
async def admin_delete_user(user_id: str, admin: dict = Depends(get_admin_user)):
    if user_id == admin["id"]:
        raise HTTPException(400, "Cannot delete yourself")
    res = await db.users.delete_one({"id": user_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "User not found")
    # Cascade: delete user's tasks, workers, name files
    await db.tasks.delete_many({"user_id": user_id})
    await db.workers.delete_many({"user_id": user_id})
    await db.name_files.delete_many({"user_id": user_id})
    await db.topup_requests.delete_many({"user_id": user_id})
    return {"ok": True}


# ---------------- Payment QR Settings ----------------
class PaymentSettingsIn(BaseModel):
    qr_image: Optional[str] = None       # base64 data URL or http URL
    upi_id: Optional[str] = None
    instructions: Optional[str] = None


class PaymentSettingsOut(BaseModel):
    qr_image: Optional[str] = None
    upi_id: Optional[str] = None
    instructions: Optional[str] = None


async def _get_settings() -> dict:
    doc = await db.settings.find_one({"id": "global"}, {"_id": 0})
    return doc or {"id": "global", "qr_image": None, "upi_id": None, "instructions": None}


@api.get("/settings/payment", response_model=PaymentSettingsOut)
async def get_payment_settings(_: dict = Depends(get_current_user)):
    s = await _get_settings()
    return PaymentSettingsOut(
        qr_image=s.get("qr_image"),
        upi_id=s.get("upi_id"),
        instructions=s.get("instructions"),
    )


@api.put("/admin/settings/payment", response_model=PaymentSettingsOut)
async def update_payment_settings(payload: PaymentSettingsIn, _: dict = Depends(get_admin_user)):
    updates: dict = {}
    if payload.qr_image is not None:
        # Accept either data:image/...;base64,... or plain URL
        if len(payload.qr_image) > 2_000_000:
            raise HTTPException(400, "QR image too large (max ~1.5 MB)")
        updates["qr_image"] = payload.qr_image
    if payload.upi_id is not None:
        updates["upi_id"] = payload.upi_id.strip()
    if payload.instructions is not None:
        updates["instructions"] = payload.instructions.strip()
    await db.settings.update_one(
        {"id": "global"},
        {"$set": {**updates, "id": "global"}},
        upsert=True,
    )
    s = await _get_settings()
    return PaymentSettingsOut(
        qr_image=s.get("qr_image"),
        upi_id=s.get("upi_id"),
        instructions=s.get("instructions"),
    )


# ---------------- Top-up Requests (User buys credits) ----------------
class TopupCreateIn(BaseModel):
    amount_rs: float = Field(gt=0, le=1_000_000)
    screenshot: str  # base64 data URL of payment proof
    note: Optional[str] = ""


class TopupOut(BaseModel):
    id: str
    user_id: str
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    amount_rs: float
    credits: int
    credit_rate: float
    screenshot: Optional[str] = None
    status: str  # pending | approved | rejected
    note: Optional[str] = ""
    admin_note: Optional[str] = ""
    created_at: str
    processed_at: Optional[str] = None


def _topup_out(t: dict, include_screenshot: bool = True) -> dict:
    return {
        "id": t["id"],
        "user_id": t["user_id"],
        "user_email": t.get("user_email"),
        "user_name": t.get("user_name"),
        "amount_rs": float(t.get("amount_rs", 0)),
        "credits": int(t.get("credits", 0)),
        "credit_rate": float(t.get("credit_rate", 1.0)),
        "screenshot": t.get("screenshot") if include_screenshot else None,
        "status": t.get("status", "pending"),
        "note": t.get("note", ""),
        "admin_note": t.get("admin_note", ""),
        "created_at": t.get("created_at", ""),
        "processed_at": t.get("processed_at"),
    }


@api.post("/topup/request", response_model=TopupOut)
async def create_topup_request(payload: TopupCreateIn, user: dict = Depends(get_current_user)):
    if not payload.screenshot or not payload.screenshot.startswith("data:image"):
        raise HTTPException(400, "Payment screenshot (data URL) is required")
    if len(payload.screenshot) > 3_000_000:
        raise HTTPException(400, "Screenshot too large (max ~2 MB)")
    rate = float(user.get("credit_rate", 1.0))
    credits = int(round(payload.amount_rs * rate))
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "user_email": user["email"],
        "user_name": user.get("name", ""),
        "amount_rs": float(payload.amount_rs),
        "credits": credits,
        "credit_rate": rate,
        "screenshot": payload.screenshot,
        "status": "pending",
        "note": (payload.note or "").strip(),
        "admin_note": "",
        "created_at": now,
        "processed_at": None,
    }
    await db.topup_requests.insert_one(doc)
    return _topup_out(doc)


@api.get("/topup/my", response_model=List[TopupOut])
async def list_my_topups(user: dict = Depends(get_current_user)):
    docs = await db.topup_requests.find(
        {"user_id": user["id"]}, {"_id": 0}
    ).sort("created_at", -1).to_list(200)
    # Don't include heavy screenshots in list view (only in detail)
    return [_topup_out(d, include_screenshot=False) for d in docs]


@api.get("/topup/{topup_id}", response_model=TopupOut)
async def get_topup_detail(topup_id: str, user: dict = Depends(get_current_user)):
    doc = await db.topup_requests.find_one({"id": topup_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Top-up not found")
    if user.get("role") != "admin" and doc["user_id"] != user["id"]:
        raise HTTPException(403, "Not allowed")
    return _topup_out(doc)


@api.get("/admin/topup-requests", response_model=List[TopupOut])
async def admin_list_topups(
    status: Optional[str] = Query(None, regex="^(pending|approved|rejected)$"),
    _: dict = Depends(get_admin_user),
):
    q: dict = {}
    if status:
        q["status"] = status
    docs = await db.topup_requests.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return [_topup_out(d, include_screenshot=False) for d in docs]


class TopupDecisionIn(BaseModel):
    action: str  # approve | reject
    admin_note: Optional[str] = ""
    override_credits: Optional[int] = None  # override credits to add


@api.post("/admin/topup-requests/{topup_id}/decide", response_model=TopupOut)
async def admin_decide_topup(topup_id: str, payload: TopupDecisionIn, _: dict = Depends(get_admin_user)):
    doc = await db.topup_requests.find_one({"id": topup_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Top-up not found")
    if doc["status"] != "pending":
        raise HTTPException(400, f"Already {doc['status']}")
    if payload.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be 'approve' or 'reject'")

    now = datetime.now(timezone.utc).isoformat()
    final_credits = int(payload.override_credits) if payload.override_credits is not None else int(doc["credits"])

    updates = {
        "status": "approved" if payload.action == "approve" else "rejected",
        "admin_note": (payload.admin_note or "").strip(),
        "processed_at": now,
        "credits": final_credits,
    }
    await db.topup_requests.update_one({"id": topup_id}, {"$set": updates})

    if payload.action == "approve" and final_credits > 0:
        # Increase user's usage_limit by the granted credits
        await db.users.update_one(
            {"id": doc["user_id"]},
            {"$inc": {"usage_limit": final_credits}},
        )

    doc.update(updates)
    return _topup_out(doc)


# ---------------- Admin Overview / Backup ----------------
@api.get("/admin/overview")
async def admin_overview(_: dict = Depends(get_admin_user)):
    # User stats
    users = await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(2000)
    total_users = len(users)
    total_usage_limit = sum(int(u.get("usage_limit", 0)) for u in users if u.get("role") != "admin")
    total_usage = sum(int(u.get("usage", 0)) for u in users if u.get("role") != "admin")
    total_balance = max(0, total_usage_limit - total_usage)

    # Topup stats
    pipeline_topup = [
        {"$match": {"status": "approved"}},
        {"$group": {
            "_id": None,
            "revenue": {"$sum": "$amount_rs"},
            "credits_sold": {"$sum": "$credits"},
        }},
    ]
    topup_agg = await db.topup_requests.aggregate(pipeline_topup).to_list(1)
    revenue = float(topup_agg[0]["revenue"]) if topup_agg else 0.0
    credits_sold = int(topup_agg[0]["credits_sold"]) if topup_agg else 0

    pending_topups = await db.topup_requests.count_documents({"status": "pending"})

    # Task stats
    active_tasks = await db.tasks.count_documents({"status": "active"})
    scheduled_tasks = await db.tasks.count_documents({"status": "scheduled"})

    # Worker stats
    workers = await db.workers.find({}, {"_id": 0, "token_hash": 0}).to_list(500)
    total_workers = len(workers)
    online_workers = sum(1 for w in workers if _worker_status(w) == "online")
    total_capacity = sum(int(w.get("capacity_max", 0)) for w in workers)
    current_load = sum(int(w.get("current_load", 0)) for w in workers if _worker_status(w) == "online")

    return {
        "users": {
            "total": total_users,
            "total_credits_assigned": total_usage_limit,
            "total_credits_used": total_usage,
            "total_credits_available": total_balance,
        },
        "revenue": {
            "total_rs": revenue,
            "credits_sold": credits_sold,
            "pending_topups": pending_topups,
        },
        "tasks": {
            "active": active_tasks,
            "scheduled": scheduled_tasks,
        },
        "workers": {
            "total": total_workers,
            "online": online_workers,
            "total_capacity": total_capacity,
            "current_load": current_load,
            "free_capacity": max(0, total_capacity - current_load),
        },
    }


HEALTH_STALE_SEC = int(os.environ.get("HEALTH_STALE_SECONDS", "45"))


# ---------------- Health ----------------
@api.get("/")
async def root():
    return {"service": "zoom-services-clone", "ok": True}


# ---------------- Fleet Health (admin) ----------------
@api.get("/admin/fleet-health")
async def admin_fleet_health(_: dict = Depends(get_admin_user)):
    """Live health snapshot of all workers — drives the auto-monitor UI panel.
    Each worker is classified healthy/warning/critical/offline based on:
      - last_heartbeat age
      - cpu_pct vs 75% threshold
      - ram_pct vs 85% threshold
      - current_load vs effective capacity
    """
    now = datetime.now(timezone.utc)
    workers = await db.workers.find({}, {"_id": 0, "token_hash": 0}).to_list(500)
    fleet: list[dict] = []
    total_load = 0
    total_cap = 0
    healthy = warning = critical = offline = 0
    for w in workers:
        eff_cap = _effective_capacity(w)
        load = int(w.get("current_load", 0))
        cpu = float(w.get("cpu_pct", 0))
        ram = float(w.get("ram_pct", 0))
        last_hb = w.get("last_heartbeat")
        age = None
        try:
            if last_hb:
                age = (now - datetime.fromisoformat(last_hb)).total_seconds()
        except Exception:
            pass

        if age is None or age > HEALTH_STALE_SEC:
            state = "offline"; offline += 1
        elif cpu > 90 or ram > 95 or (eff_cap > 0 and load >= eff_cap):
            state = "critical"; critical += 1
        elif cpu > 75 or ram > 85 or (eff_cap > 0 and load >= eff_cap * 0.9):
            state = "warning"; warning += 1
        else:
            state = "healthy"; healthy += 1

        if state != "offline":
            total_load += load
            total_cap += eff_cap

        fleet.append({
            "id": w["id"], "name": w["name"], "state": state,
            "cpu_pct": cpu, "ram_pct": ram,
            "load": load, "effective_cap": eff_cap,
            "ram_free_gb": w.get("ram_free_gb"),
            "cpu_count": w.get("cpu_count"),
            "hostname": w.get("hostname"),
            "os_info": w.get("os_info"),
            "last_heartbeat": last_hb,
            "heartbeat_age_sec": age,
            "pool_stats": w.get("pool_stats"),
            "crash_count": int(w.get("crash_count", 0)),
            "last_restart_at": w.get("last_restart_at"),
            "worker_started_at": w.get("worker_started_at"),
        })

    # Flag any RDP whose keep-alive supervisor has had to restart main_loop —
    # zero = perfectly stable, >0 = the operator should investigate even if
    # the worker is currently 'healthy'.
    unstable = sum(1 for w in workers if int(w.get("crash_count", 0)) > 0)

    return {
        "summary": {
            "total": len(workers),
            "healthy": healthy, "warning": warning,
            "critical": critical, "offline": offline,
            "unstable": unstable,  # workers with crash_count > 0
            "total_load": total_load, "total_capacity": total_cap,
            "utilization_pct": round((total_load / total_cap * 100), 1) if total_cap else 0.0,
            "prewarm": {
                "hot_browsers": sum(int((w.get("pool_stats") or {}).get("browsers", 0))
                                    for w in workers if _worker_status(w) == "online"),
                "ready_contexts": sum(int((w.get("pool_stats") or {}).get("ready_contexts", 0))
                                      for w in workers if _worker_status(w) == "online"),
                "active_bots": sum(int((w.get("pool_stats") or {}).get("total_bots", 0))
                                   for w in workers if _worker_status(w) == "online"),
                "prewarmed_workers": sum(1 for w in workers
                                         if _worker_status(w) == "online"
                                         and (w.get("pool_stats") or {}).get("prewarmed")),
            },
        },
        "workers": fleet,
    }


# ---------------- Worker voluntary release ----------------
@api.post("/workers/me/release-chunk/{chunk_id}")
async def worker_release_chunk(chunk_id: str, w: dict = Depends(get_current_worker)):
    """A worker calls this when it can no longer serve a chunk (e.g. RAM pressure,
    chromium crashed). The chunk's unjoined members are added back to the pool
    so OTHER workers can pick them up (auto-failover)."""
    chunk = await db.task_chunks.find_one(
        {"id": chunk_id, "worker_id": w["id"]}, {"_id": 0}
    )
    if not chunk:
        raise HTTPException(404, "Chunk not found or not owned by this worker")
    if chunk.get("status") != "active":
        return {"ok": True, "released": 0, "reason": f"already {chunk.get('status')}"}
    gap = max(0, int(chunk["members"]) - int(chunk.get("joined_count", 0)))
    now = datetime.now(timezone.utc).isoformat()
    if gap > 0:
        await db.tasks.update_one(
            {"id": chunk["task_id"]},
            {"$inc": {"members_claimed": -gap}},
        )
    await db.task_chunks.update_one(
        {"id": chunk_id},
        {"$set": {"status": "released", "completed_at": now,
                  "error": "Voluntary release by worker"}},
    )
    return {"ok": True, "released": gap}


# ---------------- Worker file downloads (public — no auth) ----------------
from fastapi.responses import FileResponse, PlainTextResponse

WORKER_DIR = ROOT_DIR.parent / "worker"


@api.get("/worker/zoom_worker.py")
async def download_worker_script():
    path = WORKER_DIR / "zoom_worker.py"
    if not path.exists():
        raise HTTPException(404, "Worker script not found")
    return FileResponse(str(path), media_type="text/x-python", filename="zoom_worker.py")


@api.get("/worker/zoom_worker_pool.py")
async def download_pool_worker_script():
    """v8 — Playwright browser-pool worker (Linux/Windows). Recommended."""
    path = WORKER_DIR / "zoom_worker_pool.py"
    if not path.exists():
        raise HTTPException(404, "Pool worker script not found")
    return FileResponse(str(path), media_type="text/x-python", filename="zoom_worker_pool.py")


@api.get("/worker/start_xvfb.sh", response_class=PlainTextResponse)
async def download_xvfb_script():
    path = WORKER_DIR / "start_xvfb.sh"
    if not path.exists():
        raise HTTPException(404, "start_xvfb.sh not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/x-shellscript")


@api.get("/worker/ecosystem.config.js", response_class=PlainTextResponse)
async def download_pm2_ecosystem():
    path = WORKER_DIR / "ecosystem.config.js"
    if not path.exists():
        raise HTTPException(404, "ecosystem.config.js not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/javascript")


@api.get("/worker/install_linux.sh", response_class=PlainTextResponse)
async def download_linux_installer():
    path = WORKER_DIR / "install_linux.sh"
    if not path.exists():
        raise HTTPException(404, "install_linux.sh not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/x-shellscript")


@api.get("/worker/requirements.txt")
async def download_worker_requirements():
    path = WORKER_DIR / "requirements.txt"
    if not path.exists():
        raise HTTPException(404, "requirements.txt not found")
    return FileResponse(str(path), media_type="text/plain", filename="requirements.txt")


@api.get("/worker/setup-guide", response_class=PlainTextResponse)
async def download_setup_guide():
    path = ROOT_DIR.parent / "RDP_SETUP.md"
    if not path.exists():
        raise HTTPException(404, "Setup guide not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown")


@api.get("/worker/setup-guide-linux", response_class=PlainTextResponse)
async def download_linux_setup_guide():
    path = ROOT_DIR.parent / "RDP_SETUP_LINUX.md"
    if not path.exists():
        raise HTTPException(404, "Linux setup guide not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown")


@api.get("/worker/install.ps1", response_class=PlainTextResponse)
async def download_install_script():
    path = WORKER_DIR / "install.ps1"
    if not path.exists():
        raise HTTPException(404, "install.ps1 not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain")


@api.get("/worker/migrate.sh", response_class=PlainTextResponse)
async def download_migration_script():
    """Migration script to upgrade an existing VPS worker to v8.3.2.
    Preserves WORKER_TOKEN, stops old process/service, installs new worker."""
    path = WORKER_DIR / "migrate_to_v832.sh"
    if not path.exists():
        raise HTTPException(404, "migrate_to_v832.sh not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/x-shellscript")


@api.get("/worker/NamesIn.txt", response_class=PlainTextResponse)
async def download_names_in_txt():
    path = WORKER_DIR / "NamesIn.txt"
    if not path.exists():
        raise HTTPException(404, "NamesIn.txt not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain")


# ---------------- Lifecycle ----------------
async def seed_admin():
    email = os.environ["ADMIN_EMAIL"].lower().strip()
    password = os.environ["ADMIN_PASSWORD"]
    name = os.environ.get("ADMIN_NAME", "Admin")
    usage_limit = int(os.environ.get("USAGE_LIMIT", 15000))
    existing = await db.users.find_one({"email": email})
    if existing is None:
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": email,
            "password_hash": hash_password(password),
            "name": name,
            "role": "admin",
            "usage": 0,
            "usage_limit": usage_limit,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        log.info("Seeded admin user %s", email)
    else:
        # Keep password in sync with env, ensure fields exist
        updates = {}
        if not verify_password(password, existing["password_hash"]):
            updates["password_hash"] = hash_password(password)
        if "usage" not in existing:
            updates["usage"] = 0
        if "usage_limit" not in existing:
            updates["usage_limit"] = usage_limit
        if "role" not in existing:
            updates["role"] = "admin"
        if "name" not in existing:
            updates["name"] = name
        if "id" not in existing:
            updates["id"] = str(uuid.uuid4())
        if updates:
            await db.users.update_one({"email": email}, {"$set": updates})


async def create_indexes():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("id", unique=True)
    await db.tasks.create_index([("user_id", 1), ("status", 1)])
    await db.tasks.create_index("id", unique=True)
    await db.tasks.create_index([("status", 1), ("worker_id", 1)])
    await db.tasks.create_index([("status", 1), ("started_at", 1)])  # for claim's sort
    await db.task_chunks.create_index("id", unique=True)
    await db.task_chunks.create_index([("task_id", 1), ("worker_id", 1), ("status", 1)])
    await db.task_chunks.create_index("worker_id")
    await db.name_files.create_index([("user_id", 1), ("name", 1)])
    await db.workers.create_index("id", unique=True)
    await db.workers.create_index([("user_id", 1), ("name", 1)])
    await db.workers.create_index("last_heartbeat")  # CRITICAL: claim counts online workers
    await db.login_attempts.create_index("identifier")
    await db.topup_requests.create_index([("user_id", 1), ("status", 1)])
    await db.topup_requests.create_index("id", unique=True)
    await db.settings.create_index("id", unique=True)


async def task_poller():
    """Background scheduler tick. Runs every 5s. Responsibilities:
      1. scheduled→active when due
      2. active→completed when end-time hit and no worker
      3. failed when worker timed out
      4. STALE WORKER AUTO-RECOVERY: if a worker hasn't heartbeated in
         HEALTH_STALE_SEC, release its active chunks back to the pool so
         OTHER workers can pick them up (zero-idle / auto-failover).
    """
    while True:
        try:
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()
            # scheduled -> active
            async for doc in db.tasks.find({"status": "scheduled", "scheduled_at": {"$lte": now_iso}}, {"_id": 0}):
                ends = (now + timedelta(seconds=doc["timeout"])).isoformat()
                await db.tasks.update_one(
                    {"id": doc["id"]},
                    {"$set": {"status": "active", "started_at": now_iso, "ends_at": ends}},
                )
            # active -> completed ONLY when no worker assigned (mock auto-complete)
            await db.tasks.update_many(
                {
                    "status": "active",
                    "ends_at": {"$lte": now_iso},
                    "$or": [{"worker_id": None}, {"worker_id": {"$exists": False}}],
                },
                {"$set": {"status": "completed", "completed_at": now_iso}},
            )
            # active with worker but timed-out by 60s grace -> mark failed (worker silent)
            grace = (now - timedelta(seconds=60)).isoformat()
            await db.tasks.update_many(
                {
                    "status": "active",
                    "ends_at": {"$lte": grace},
                    "worker_id": {"$nin": [None]},
                },
                {"$set": {
                    "status": "failed",
                    "completed_at": now_iso,
                    "error": "Worker timed out (no completion report within grace period)",
                }},
            )

            # ===== AUTO-FAILOVER: stale workers ⇒ release their active chunks =====
            stale_cutoff = (now - timedelta(seconds=HEALTH_STALE_SEC)).isoformat()
            stale_workers = await db.workers.find(
                {"$or": [
                    {"last_heartbeat": {"$lt": stale_cutoff}},
                    {"last_heartbeat": None},
                ]},
                {"_id": 0, "id": 1, "name": 1, "last_heartbeat": 1},
            ).to_list(500)
            stale_ids = [w["id"] for w in stale_workers]
            if stale_ids:
                # Find their unfinished chunks
                orphan_chunks = await db.task_chunks.find(
                    {"worker_id": {"$in": stale_ids}, "status": "active"},
                    {"_id": 0, "id": 1, "task_id": 1, "worker_id": 1, "members": 1, "joined_count": 1},
                ).to_list(1000)
                if orphan_chunks:
                    for c in orphan_chunks:
                        # Roll back members_claimed by the number we never delivered.
                        # joined_count is what successfully joined; remaining is the gap.
                        gap = max(0, int(c["members"]) - int(c.get("joined_count", 0)))
                        if gap > 0:
                            await db.tasks.update_one(
                                {"id": c["task_id"]},
                                {"$inc": {"members_claimed": -gap}},
                            )
                        await db.task_chunks.update_one(
                            {"id": c["id"]},
                            {"$set": {
                                "status": "failed",
                                "completed_at": now_iso,
                                "error": f"Worker went offline (heartbeat lost > {HEALTH_STALE_SEC}s)",
                            }},
                        )
                    log.info(
                        "auto-failover: released %d chunk(s) from %d stale worker(s)",
                        len(orphan_chunks), len(stale_ids),
                    )
                # Reset their current_load so the dashboard reflects truth.
                await db.workers.update_many(
                    {"id": {"$in": stale_ids}},
                    {"$set": {"current_load": 0}},
                )
        except Exception as e:
            log.exception("poller error: %s", e)
        await asyncio.sleep(5)


_poller_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def on_startup():
    await create_indexes()
    await seed_admin()
    global _poller_task
    _poller_task = asyncio.create_task(task_poller())


@app.on_event("shutdown")
async def on_shutdown():
    global _poller_task
    if _poller_task:
        _poller_task.cancel()
    client.close()


# ---------------- Mount + CORS ----------------
app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
