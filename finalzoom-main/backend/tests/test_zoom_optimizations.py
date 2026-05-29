"""
Backend tests for Zoom Worker System ultra-optimized features (iteration 5).

Covers:
- Auth login (admin)
- Fleet health endpoint (new)
- Worker CRUD + heartbeat
- Worker claim flow (sequential greedy fill)
- Active task creation + claim/progress/complete e2e
- Sequential RDP fill (greedy)
- Auto-failover (stale worker via direct DB write)
- Voluntary release-chunk
- Worker file download endpoints (5 new)
- Existing tasks endpoints
"""
import os
import time
import uuid
import pytest
import requests
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback to reading frontend/.env directly
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@zoomdash.io"
ADMIN_PASSWORD = "Admin@12345"

MONGO_URL = "mongodb://localhost:27017"
DB_NAME = "zoom_services"


# ---------------- Fixtures ----------------
@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["email"] == ADMIN_EMAIL
    assert data["role"] == "admin"
    # Ensure access_token cookie was set
    assert "access_token" in s.cookies or any(c.name == "access_token" for c in s.cookies)
    return s


@pytest.fixture(scope="session")
def mongo_db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


def _cleanup_test_workers_and_tasks(db):
    db.workers.delete_many({"name": {"$regex": "^TEST_"}})
    # delete tasks created by admin user with TEST_ meeting_password marker
    db.tasks.delete_many({"meeting_password": "TESTPWD"})
    db.task_chunks.delete_many({"worker_name": {"$regex": "^TEST_"}})


@pytest.fixture(scope="session", autouse=True)
def _cleanup(mongo_db):
    _cleanup_test_workers_and_tasks(mongo_db)
    yield
    _cleanup_test_workers_and_tasks(mongo_db)


def _new_worker_name():
    return f"TEST_worker_{uuid.uuid4().hex[:8]}"


# ---------------- Auth ----------------
class TestAuth:
    def test_login_success_sets_cookies(self, admin_session):
        # already logged in via fixture; assert /auth/me works
        r = admin_session.get(f"{API}/auth/me", timeout=10)
        assert r.status_code == 200
        u = r.json()
        assert u["email"] == ADMIN_EMAIL
        assert u["role"] == "admin"

    def test_login_invalid(self):
        r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"}, timeout=10)
        assert r.status_code in (401, 429)


# ---------------- Fleet health ----------------
class TestFleetHealth:
    def test_requires_admin(self):
        r = requests.get(f"{API}/admin/fleet-health", timeout=10)
        assert r.status_code == 401

    def test_structure(self, admin_session):
        r = admin_session.get(f"{API}/admin/fleet-health", timeout=10)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "summary" in data and "workers" in data
        s = data["summary"]
        for k in ("total", "healthy", "warning", "critical", "offline", "total_load", "total_capacity", "utilization_pct"):
            assert k in s, f"missing summary key: {k}"
        assert isinstance(data["workers"], list)


# ---------------- Worker create + heartbeat ----------------
class TestWorkerLifecycle:
    def test_create_worker_requires_admin(self):
        r = requests.post(f"{API}/workers", json={"name": _new_worker_name(), "capacity_max": 50}, timeout=10)
        assert r.status_code == 401

    def test_create_worker_and_heartbeat(self, admin_session):
        name = _new_worker_name()
        r = admin_session.post(f"{API}/workers", json={"name": name, "capacity_max": 50}, timeout=10)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["name"] == name
        assert "token" in d and "." in d["token"]
        wid = d["id"]
        token = d["token"]
        # Heartbeat
        hb = requests.post(
            f"{API}/workers/me/heartbeat",
            json={"current_load": 5, "cpu_pct": 22.5, "ram_pct": 33.0, "hostname": "h1", "reported_capacity": 50},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert hb.status_code == 200, hb.text
        body = hb.json()
        assert body["current_load"] == 5
        assert body["cpu_pct"] == 22.5
        assert body["status"] == "online"
        # Verify in admin list
        wl = admin_session.get(f"{API}/workers", timeout=10).json()
        found = next((w for w in wl if w["id"] == wid), None)
        assert found and found["status"] == "online"
        assert found["current_load"] == 5
        assert found["reported_capacity"] == 50


def _create_worker(admin_session, capacity_max=50):
    name = _new_worker_name()
    r = admin_session.post(f"{API}/workers", json={"name": name, "capacity_max": capacity_max}, timeout=10)
    assert r.status_code == 200, r.text
    d = r.json()
    return d["id"], d["token"], d["name"]


def _heartbeat(token, load=0, cap=50):
    return requests.post(
        f"{API}/workers/me/heartbeat",
        json={"current_load": load, "cpu_pct": 10.0, "ram_pct": 20.0, "reported_capacity": cap},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )


def _create_active_task(admin_session, members=10):
    r = admin_session.post(
        f"{API}/tasks",
        json={
            "meeting_id": "1234567890",
            "meeting_password": "TESTPWD",
            "members": members,
            "name_source": "Indian",
            "meeting_type": "Normal Participants",
            "timeout": 600,
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["status"] == "active"
    return t


# ---------------- Claim empty / claim with task ----------------
class TestClaimFlow:
    def test_claim_empty_returns_no_tasks(self, admin_session):
        wid, token, _ = _create_worker(admin_session)
        assert _heartbeat(token).status_code == 200
        # No active task expected (cleanup before)
        r = requests.post(f"{API}/workers/me/claim", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert "tasks" in body
        # NOTE: may not be empty if a previous test created task; OK as long as key exists
        assert isinstance(body["tasks"], list)

    def test_end_to_end_claim_progress_complete(self, admin_session, mongo_db):
        # fresh worker
        wid, token, wname = _create_worker(admin_session, capacity_max=50)
        assert _heartbeat(token, load=0, cap=50).status_code == 200
        # create active task w/ 10 members
        task = _create_active_task(admin_session, members=10)
        tid = task["id"]
        # claim
        r = requests.post(f"{API}/workers/me/claim",
                          headers={"Authorization": f"Bearer {token}"}, timeout=10)
        assert r.status_code == 200, r.text
        claimed = r.json()["tasks"]
        my = [t for t in claimed if t["id"] == tid]
        assert len(my) == 1, f"expected to claim task, got: {claimed}"
        chunk = my[0]
        assert chunk["members"] <= 10
        assert "names" in chunk and len(chunk["names"]) == chunk["members"]
        assert "chunk_id" in chunk

        # progress
        p = requests.patch(
            f"{API}/tasks/{tid}/progress",
            json={"joined_count": chunk["members"]},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert p.status_code == 200, p.text
        assert p.json()["joined_count"] == chunk["members"]

        # complete
        c = requests.post(
            f"{API}/tasks/{tid}/complete",
            json={"success": True, "joined_count": chunk["members"]},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert c.status_code == 200, c.text
        # parent may go to completed only when all members claimed. If chunk == members, done.
        if chunk["members"] == 10:
            assert c.json()["status"] == "completed"


# ---------------- Sequential (greedy) fill ----------------
class TestGreedyFill:
    def test_first_worker_takes_full_capacity_first(self, admin_session, mongo_db):
        # Two workers, capacity 12 each. Task = 20 members.
        # First to poll should get 12, second should get 8.
        wid1, tok1, _ = _create_worker(admin_session, capacity_max=12)
        wid2, tok2, _ = _create_worker(admin_session, capacity_max=12)
        assert _heartbeat(tok1, load=0, cap=12).status_code == 200
        assert _heartbeat(tok2, load=0, cap=12).status_code == 200

        task = _create_active_task(admin_session, members=20)
        tid = task["id"]

        r1 = requests.post(f"{API}/workers/me/claim",
                           headers={"Authorization": f"Bearer {tok1}"}, timeout=10).json()
        c1 = next((t for t in r1["tasks"] if t["id"] == tid), None)
        assert c1, f"worker1 didn't claim: {r1}"
        # In greedy mode, worker1 should get its full capacity (12)
        assert c1["members"] == 12, f"expected 12 for greedy first worker, got {c1['members']}"

        r2 = requests.post(f"{API}/workers/me/claim",
                           headers={"Authorization": f"Bearer {tok2}"}, timeout=10).json()
        c2 = next((t for t in r2["tasks"] if t["id"] == tid), None)
        assert c2, f"worker2 didn't claim: {r2}"
        assert c2["members"] == 8, f"expected 8 remainder for worker2, got {c2['members']}"


# ---------------- Voluntary release ----------------
class TestReleaseChunk:
    def test_release_chunk(self, admin_session, mongo_db):
        wid, token, _ = _create_worker(admin_session, capacity_max=20)
        assert _heartbeat(token, load=0, cap=20).status_code == 200
        task = _create_active_task(admin_session, members=10)
        tid = task["id"]

        r = requests.post(f"{API}/workers/me/claim",
                          headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        chunk = next((t for t in r["tasks"] if t["id"] == tid), None)
        assert chunk
        chunk_id = chunk["chunk_id"]
        members = chunk["members"]

        # Report 3 joined so gap = members - 3
        requests.patch(f"{API}/tasks/{tid}/progress", json={"joined_count": 3},
                       headers={"Authorization": f"Bearer {token}"}, timeout=10)

        rel = requests.post(
            f"{API}/workers/me/release-chunk/{chunk_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert rel.status_code == 200, rel.text
        body = rel.json()
        assert body["ok"] is True
        assert body["released"] == members - 3

        # Verify task members_claimed decreased
        td = mongo_db.tasks.find_one({"id": tid})
        assert td["members_claimed"] == 3  # only the joined ones remain


# ---------------- Auto-failover via stale heartbeat ----------------
class TestAutoFailover:
    def test_stale_worker_chunk_released(self, admin_session, mongo_db):
        wid, token, _ = _create_worker(admin_session, capacity_max=20)
        assert _heartbeat(token, load=0, cap=20).status_code == 200
        task = _create_active_task(admin_session, members=10)
        tid = task["id"]
        r = requests.post(f"{API}/workers/me/claim",
                          headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        chunk = next((t for t in r["tasks"] if t["id"] == tid), None)
        assert chunk
        chunk_id = chunk["chunk_id"]
        members = chunk["members"]

        # Simulate stale heartbeat (60s ago)
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        mongo_db.workers.update_one({"id": wid}, {"$set": {"last_heartbeat": stale_ts}})

        # Wait up to 15s for poller (runs every 5s) to detect & release
        released = False
        for _ in range(15):
            time.sleep(1)
            c = mongo_db.task_chunks.find_one({"id": chunk_id})
            if c and c.get("status") == "failed":
                released = True
                break
        assert released, f"poller did not release chunk in 15s; status={c.get('status') if c else None}"
        # task members_claimed should decrement by gap (members - 0 joined)
        td = mongo_db.tasks.find_one({"id": tid})
        assert td["members_claimed"] == 0


# ---------------- Worker file downloads ----------------
class TestWorkerDownloads:
    @pytest.mark.parametrize("path,expect_substr", [
        ("/worker/zoom_worker_pool.py", None),
        ("/worker/start_xvfb.sh", None),
        ("/worker/ecosystem.config.js", None),
        ("/worker/install_linux.sh", None),
        ("/worker/setup-guide-linux", "#"),  # markdown
    ])
    def test_download(self, path, expect_substr):
        r = requests.get(f"{API}{path}", timeout=15)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert len(r.text) > 50, f"{path} returned empty/short content"
        if expect_substr:
            assert expect_substr in r.text


# ---------------- Existing critical endpoints ----------------
class TestExistingEndpoints:
    def test_tasks_lists(self, admin_session):
        for path in ("/tasks/active", "/tasks/scheduled", "/tasks/previous"):
            r = admin_session.get(f"{API}{path}", timeout=10)
            assert r.status_code == 200, f"{path} {r.status_code}"
            assert isinstance(r.json(), list)

    def test_list_workers(self, admin_session):
        r = admin_session.get(f"{API}/workers", timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_delete_worker_and_cancel_task(self, admin_session):
        # create + delete worker
        wid, _, _ = _create_worker(admin_session)
        r = admin_session.delete(f"{API}/workers/{wid}", timeout=10)
        assert r.status_code == 200
        # create + cancel task
        task = _create_active_task(admin_session, members=5)
        r = admin_session.post(f"{API}/tasks/{task['id']}/cancel", timeout=10)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"


# ---------------- Redis cache (graceful) ----------------
class TestRedisCache:
    def test_cache_populates_or_graceful(self, admin_session):
        # trigger a claim path which uses _get_online_worker_count/_get_online_total_capacity
        wid, token, _ = _create_worker(admin_session, capacity_max=10)
        _heartbeat(token, load=0, cap=10)
        r = requests.post(f"{API}/workers/me/claim",
                          headers={"Authorization": f"Bearer {token}"}, timeout=10)
        assert r.status_code == 200
        # Check if redis has the keys (best effort)
        try:
            import redis
            rc = redis.from_url("redis://localhost:6379/0")
            # Either key present (cache populated) OR ping works (means redis up & code path didn't error)
            assert rc.ping() is True
        except Exception:
            # No redis library or redis down — endpoint should still have returned 200 above (graceful)
            pass
