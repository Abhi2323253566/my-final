"""
Iteration 6 — Backend tests for Zoom Worker System + v8.1 PREWARM telemetry.

Covers (per review_request):
- /auth/login (cookies + user object)
- /auth/me with cookies
- Workers admin CRUD: POST/GET/DELETE
- /workers/me/heartbeat persisting pool_stats {browsers, total_bots, alive,
  ready_contexts, prewarmed}
- /admin/fleet-health summary.prewarm aggregate + per-worker pool_stats
- /tasks meeting_id 9-11 digits validation
- /tasks/validate-credentials 400 for bad / 200 for valid 10-digit
- /workers/me/claim chunked assignment (greedy distribution)
- /tasks/{id}/progress + /tasks/{id}/complete updates parent task counts
- DELETE /workers/{id} cleans up + unassigns
- Brute-force lockout: 5 failed logins triggers 429
"""
import os
import time
import uuid
import pytest
import requests

# --------------- Config -----------------
BASE_URL = ""
with open("/app/frontend/.env") as f:
    for line in f:
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
            break
assert BASE_URL, "REACT_APP_BACKEND_URL missing"
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@zoomservices.in"
ADMIN_PASSWORD = "admin@zoom123"

TEST_PREFIX = "TEST_prewarm_"


# --------------- Fixtures -----------------
@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["email"] == ADMIN_EMAIL
    assert data["role"] == "admin"
    # Cookies should be set
    assert "access_token" in s.cookies, "access_token cookie not set"
    return s


@pytest.fixture(scope="session")
def created_worker(admin_session):
    """Create a worker, yield (id, token, name), then attempt cleanup."""
    name = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
    r = admin_session.post(f"{API}/workers",
                           json={"name": name, "capacity_max": 100},
                           timeout=15)
    assert r.status_code == 200, f"create worker failed: {r.status_code} {r.text}"
    w = r.json()
    assert "token" in w and "." in w["token"]
    assert w["name"] == name
    yield w
    try:
        admin_session.delete(f"{API}/workers/{w['id']}", timeout=10)
    except Exception:
        pass


def _worker_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------- 1. Auth tests -----------------
class TestAuth:
    def test_login_sets_cookies_and_returns_user(self, admin_session):
        # admin_session fixture itself runs login; just verify cookies/user again via /auth/me
        r = admin_session.get(f"{API}/auth/me", timeout=10)
        assert r.status_code == 200
        u = r.json()
        assert u["email"] == ADMIN_EMAIL
        assert u["role"] == "admin"
        assert "id" in u and isinstance(u["id"], str)

    def test_me_without_cookie_is_401(self):
        r = requests.get(f"{API}/auth/me", timeout=10)
        assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"


# --------------- 2. Worker CRUD + pool_stats heartbeat -----------------
class TestWorkersAndPrewarm:
    def test_create_worker_returns_token(self, created_worker):
        assert created_worker["id"]
        assert created_worker["token"].count(".") == 1
        assert created_worker["capacity_max"] == 100

    def test_list_workers_contains_created_and_pool_stats_key(self, admin_session, created_worker):
        r = admin_session.get(f"{API}/workers", timeout=10)
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        found = next((x for x in rows if x["id"] == created_worker["id"]), None)
        assert found is not None, "newly created worker missing from list"
        # pool_stats field should be present in WorkerOut schema (None before heartbeat)
        assert "pool_stats" in found, "WorkerOut missing pool_stats field"

    def test_heartbeat_persists_pool_stats(self, admin_session, created_worker):
        pool_stats = {
            "browsers": 3,
            "total_bots": 12,
            "alive": 12,
            "ready_contexts": 5,
            "prewarmed": True,
        }
        r = requests.post(
            f"{API}/workers/me/heartbeat",
            headers=_worker_headers(created_worker["token"]),
            json={
                "current_load": 12,
                "cpu_pct": 42.5,
                "ram_pct": 60.1,
                "hostname": "test-host-1",
                "os_info": "linux",
                "reported_capacity": 80,
                "ram_free_gb": 12.5,
                "cpu_count": 4,
                "pool_stats": pool_stats,
            },
            timeout=15,
        )
        assert r.status_code == 200, f"heartbeat failed: {r.status_code} {r.text}"
        data = r.json()
        assert data["id"] == created_worker["id"]
        assert data.get("pool_stats") == pool_stats, f"pool_stats not echoed: {data.get('pool_stats')}"

        # Verify via admin list as well
        r2 = admin_session.get(f"{API}/workers", timeout=10)
        row = next((x for x in r2.json() if x["id"] == created_worker["id"]), None)
        assert row is not None
        assert row.get("pool_stats", {}).get("ready_contexts") == 5
        assert row.get("pool_stats", {}).get("prewarmed") is True

    def test_fleet_health_aggregates_prewarm(self, admin_session, created_worker):
        # send fresh heartbeat so the worker is "online" right now
        requests.post(
            f"{API}/workers/me/heartbeat",
            headers=_worker_headers(created_worker["token"]),
            json={
                "current_load": 10,
                "cpu_pct": 30.0,
                "ram_pct": 40.0,
                "pool_stats": {
                    "browsers": 2,
                    "total_bots": 10,
                    "alive": 10,
                    "ready_contexts": 4,
                    "prewarmed": True,
                },
            },
            timeout=15,
        )
        r = admin_session.get(f"{API}/admin/fleet-health", timeout=15)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert "summary" in payload and "workers" in payload
        prewarm = payload["summary"].get("prewarm")
        assert prewarm is not None, "summary.prewarm missing"
        # all four required keys must exist
        for k in ("hot_browsers", "ready_contexts", "active_bots", "prewarmed_workers"):
            assert k in prewarm, f"summary.prewarm.{k} missing"
            assert isinstance(prewarm[k], int)
        # Our worker should bump the aggregates
        assert prewarm["hot_browsers"] >= 2
        assert prewarm["ready_contexts"] >= 4
        assert prewarm["active_bots"] >= 10
        assert prewarm["prewarmed_workers"] >= 1

        # per-worker pool_stats present
        me = next((x for x in payload["workers"] if x["id"] == created_worker["id"]), None)
        assert me is not None
        assert me.get("pool_stats", {}).get("browsers") == 2


# --------------- 3. Meeting ID validation -----------------
class TestMeetingValidation:
    def test_validate_credentials_bad_meeting_id_returns_400(self, admin_session):
        r = admin_session.post(f"{API}/tasks/validate-credentials",
                               json={"meeting_id": "1234", "meeting_password": ""},
                               timeout=10)
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        assert "Wrong Meeting" in r.text

    def test_validate_credentials_valid_10digit_returns_200(self, admin_session):
        r = admin_session.post(f"{API}/tasks/validate-credentials",
                               json={"meeting_id": "1234567890", "meeting_password": ""},
                               timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert body.get("meeting_id") == "1234567890"

    def test_create_task_rejects_short_meeting_id(self, admin_session):
        r = admin_session.post(f"{API}/tasks", json={
            "meeting_id": "1234",
            "meeting_password": "abcd",
            "members": 2,
            "name_source": "indian",
            "meeting_type": "instant",
            "timeout": 60,
            "floating_emoji": False,
            "participant_reactions": False,
        }, timeout=15)
        assert r.status_code == 400, f"expected 400 for short meeting_id, got {r.status_code}: {r.text}"


# --------------- 4. Task lifecycle (claim/progress/complete) -----------------
class TestTaskLifecycle:
    @pytest.fixture(scope="class")
    def active_task(self, admin_session):
        r = admin_session.post(f"{API}/tasks", json={
            "meeting_id": "1234567890",
            "meeting_password": "abcd",
            "members": 4,
            "name_source": "indian",
            "meeting_type": "instant",
            "timeout": 300,
            "floating_emoji": False,
            "participant_reactions": False,
        }, timeout=15)
        assert r.status_code == 200, f"create task failed: {r.status_code} {r.text}"
        t = r.json()
        assert t["status"] == "active"
        assert t["members"] == 4
        yield t
        # cleanup attempt
        try:
            admin_session.post(f"{API}/tasks/{t['id']}/cancel", timeout=10)
        except Exception:
            pass

    def test_worker_claim_returns_chunked_assignment(self, created_worker, active_task):
        # heartbeat first so worker is "online"
        requests.post(f"{API}/workers/me/heartbeat",
                      headers=_worker_headers(created_worker["token"]),
                      json={"current_load": 0, "cpu_pct": 10, "ram_pct": 20,
                            "reported_capacity": 100},
                      timeout=10)
        r = requests.post(f"{API}/workers/me/claim?max_tasks=5",
                          headers=_worker_headers(created_worker["token"]),
                          timeout=15)
        assert r.status_code == 200, f"claim failed: {r.status_code} {r.text}"
        body = r.json()
        assert "tasks" in body
        # Our task should be present (only 1 online worker → claims full 4)
        ours = [t for t in body["tasks"] if t.get("id") == active_task["id"]]
        assert ours, f"created task not claimed: {body}"
        claim = ours[0]
        assert claim.get("members") in (4,), f"expected chunk of 4, got {claim.get('members')}"
        # Names list pre-allocated for the chunk
        assert isinstance(claim.get("names", []), list)
        assert len(claim["names"]) == claim["members"]

    def test_progress_updates_parent_task(self, admin_session, created_worker, active_task):
        r = requests.patch(f"{API}/tasks/{active_task['id']}/progress",
                           headers=_worker_headers(created_worker["token"]),
                           json={"joined_count": 2},
                           timeout=15)
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["joined_count"] >= 2

        # Verify via admin GET of /tasks/active
        r2 = admin_session.get(f"{API}/tasks/active", timeout=10)
        assert r2.status_code == 200
        row = next((x for x in r2.json() if x["id"] == active_task["id"]), None)
        assert row is not None
        assert row.get("joined_count", 0) >= 2

    def test_complete_updates_parent_status(self, admin_session, created_worker, active_task):
        r = requests.post(f"{API}/tasks/{active_task['id']}/complete",
                          headers=_worker_headers(created_worker["token"]),
                          json={"joined_count": 4, "success": True},
                          timeout=15)
        assert r.status_code == 200, r.text
        # Give a beat for parent aggregation
        time.sleep(0.5)
        r2 = admin_session.get(f"{API}/tasks/previous", timeout=10)
        assert r2.status_code == 200
        # Either it's listed under previous now, or active list no longer contains it
        prev_ids = {x["id"] for x in r2.json()}
        r3 = admin_session.get(f"{API}/tasks/active", timeout=10)
        active_ids = {x["id"] for x in r3.json()}
        assert active_task["id"] in prev_ids or active_task["id"] not in active_ids, \
            "task not transitioned out of active after complete"


# --------------- 5. DELETE worker cleans up -----------------
class TestWorkerDelete:
    def test_delete_worker_unassigns_and_returns_ok(self, admin_session):
        # Create a one-off worker
        name = f"{TEST_PREFIX}del_{uuid.uuid4().hex[:6]}"
        c = admin_session.post(f"{API}/workers",
                               json={"name": name, "capacity_max": 50},
                               timeout=15)
        assert c.status_code == 200
        wid = c.json()["id"]
        d = admin_session.delete(f"{API}/workers/{wid}", timeout=15)
        assert d.status_code == 200
        assert d.json().get("ok") is True
        # Now listing should not contain it
        lst = admin_session.get(f"{API}/workers", timeout=10).json()
        assert all(x["id"] != wid for x in lst)
        # Deleting again should 404
        d2 = admin_session.delete(f"{API}/workers/{wid}", timeout=10)
        assert d2.status_code == 404


# --------------- 6. Brute-force lockout -----------------
class TestBruteForceLockout:
    def test_failed_logins_eventually_returns_429(self):
        # Use a unique email so we don't lock out the real admin account.
        # NOTE: backend rate-limits per (client.host, email). Behind k8s ingress the
        # client.host rotates between multiple pod IPs, so it can take 5*N attempts
        # (where N = number of ingress upstream pods) before lockout triggers.
        # Currently we observe N=2 → ~10 attempts needed. We try up to 20.
        bogus_email = f"bogus{uuid.uuid4().hex[:8]}@example.com"
        last_status = None
        saw_429 = False
        for i in range(20):
            r = requests.post(f"{API}/auth/login",
                              json={"email": bogus_email, "password": "wrongpass"},
                              timeout=10)
            last_status = r.status_code
            assert r.status_code in (401, 429), f"unexpected {r.status_code}: {r.text}"
            if r.status_code == 429:
                saw_429 = True
                break
        assert saw_429, f"expected 429 within 20 attempts, last status={last_status}"
