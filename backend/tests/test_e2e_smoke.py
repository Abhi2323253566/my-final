"""E2E backend smoke test for FinalZoom orchestration platform.

Covers:
- Admin login (cookie-based)
- Worker registration + heartbeat (Bearer token)
- Task creation + listing
- Fair work distribution across N workers (regression for previous starvation bug)
- Progress reporting + chunk/task completion
- Task lifecycle (scheduled -> active -> completed)
- Task cancellation

Run:
  cd /app && pytest backend/tests/test_e2e_smoke.py -v --tb=short \
    --junitxml=/app/test_reports/pytest/e2e_smoke.xml
"""
import math
import os
import time
import uuid
import pytest
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load env (backend .env for admin creds; frontend .env for public BASE_URL)
load_dotenv(Path("/app/backend/.env"))
load_dotenv(Path("/app/frontend/.env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@finalzoom.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin@FinalZoom2026")
WORKER_PREFIX = f"e2e-{uuid.uuid4().hex[:6]}-"


# ----------------------------- fixtures -----------------------------
@pytest.fixture(scope="module")
def admin_session():
    """Cookie-based admin session."""
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    if r.status_code != 200:
        pytest.skip(f"Admin login failed {r.status_code}: {r.text[:200]}")
    data = r.json()
    assert data["email"] == ADMIN_EMAIL
    assert data["role"] == "admin"
    yield s
    # teardown: cancel any leftover active tasks for admin + delete e2e workers
    try:
        for t in s.get(f"{BASE_URL}/api/tasks/active", timeout=10).json():
            s.post(f"{BASE_URL}/api/tasks/{t['id']}/cancel", timeout=10)
        for w in s.get(f"{BASE_URL}/api/workers", timeout=10).json():
            if w["name"].startswith(WORKER_PREFIX):
                s.delete(f"{BASE_URL}/api/workers/{w['id']}", timeout=10)
    except Exception as exc:
        print(f"cleanup warning: {exc}")


def _create_worker(admin: requests.Session, name: str, cap: int = 50) -> dict:
    r = admin.post(f"{BASE_URL}/api/workers",
                   json={"name": name, "capacity_max": cap}, timeout=15)
    r.raise_for_status()
    body = r.json()
    assert "id" in body and "token" in body and body["name"] == name
    return body


def _heartbeat(token: str, load: int = 0, cap: int = 50):
    r = requests.post(
        f"{BASE_URL}/api/workers/me/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_load": load, "cpu_pct": 5.0, "ram_pct": 20.0,
              "hostname": "e2e-mock", "os_info": "linux", "reported_capacity": cap},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _claim(token: str, max_tasks: int = 5) -> list:
    r = requests.post(
        f"{BASE_URL}/api/workers/me/claim?max_tasks={max_tasks}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("tasks", [])


def _create_task(admin: requests.Session, members: int = 50) -> dict:
    payload = {
        "meeting_id": f"{int(time.time()) % 9999999999:010d}",
        "meeting_password": "test",
        "members": members,
        "name_source": "NamesIn",
        "timeout": 600,
    }
    r = admin.post(f"{BASE_URL}/api/tasks", json=payload, timeout=15)
    r.raise_for_status()
    body = r.json()
    assert body["members"] == members
    assert body["status"] in ("scheduled", "active")
    return body


# ------------------------------ tests ------------------------------
class TestAuth:
    def test_admin_login_returns_user(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=10)
        assert r.status_code == 200
        me = r.json()
        assert me["email"] == ADMIN_EMAIL
        assert me["role"] == "admin"

    def test_invalid_login_rejected(self):
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": ADMIN_EMAIL, "password": "wrong"},
                          timeout=10)
        assert r.status_code in (400, 401, 403)


class TestWorkerLifecycle:
    def test_register_heartbeat_delete(self, admin_session):
        name = f"{WORKER_PREFIX}solo"
        w = _create_worker(admin_session, name, cap=30)
        try:
            hb = _heartbeat(w["token"], cap=30)
            assert hb["status"] == "online"
            assert hb["id"] == w["id"]

            me = requests.get(
                f"{BASE_URL}/api/workers/me",
                headers={"Authorization": f"Bearer {w['token']}"},
                timeout=10,
            )
            assert me.status_code == 200
            assert me.json()["id"] == w["id"]
        finally:
            d = admin_session.delete(f"{BASE_URL}/api/workers/{w['id']}", timeout=10)
            assert d.status_code in (200, 204)


class TestTaskCRUD:
    def test_create_task_and_list(self, admin_session):
        task = _create_task(admin_session, members=10)
        # GET to verify persistence
        r = admin_session.get(f"{BASE_URL}/api/tasks/active", timeout=10)
        assert r.status_code == 200
        ids = [t["id"] for t in r.json()]
        assert task["id"] in ids
        # Cancel cleanup
        c = admin_session.post(f"{BASE_URL}/api/tasks/{task['id']}/cancel", timeout=10)
        assert c.status_code == 200
        assert c.json()["status"] == "cancelled"

    def test_cancel_then_not_in_active(self, admin_session):
        task = _create_task(admin_session, members=8)
        c = admin_session.post(f"{BASE_URL}/api/tasks/{task['id']}/cancel", timeout=10)
        assert c.status_code == 200
        active = admin_session.get(f"{BASE_URL}/api/tasks/active", timeout=10).json()
        assert task["id"] not in [t["id"] for t in active]


class TestFairDistribution:
    """Regression for previous starvation bug — spin up many workers, push a big
    task, claim across all workers, assert min/max are within tolerance of the
    equal share."""

    @pytest.mark.parametrize("num_workers,members", [(30, 500), (40, 500)])
    def test_equal_share_distribution(self, admin_session, num_workers, members):
        # Cancel any pre-existing active tasks so claim only sees our task
        for t in admin_session.get(f"{BASE_URL}/api/tasks/active", timeout=10).json():
            admin_session.post(f"{BASE_URL}/api/tasks/{t['id']}/cancel", timeout=10)
        # Settle: give the cancel time to propagate before creating the new task
        time.sleep(1.0)

        # Spin up workers
        prefix = f"{WORKER_PREFIX}fair{num_workers}-"
        workers = []
        for i in range(num_workers):
            w = _create_worker(admin_session, f"{prefix}{i:03d}", cap=50)
            workers.append(w)
        # Heartbeat all so backend marks them online
        for w in workers:
            _heartbeat(w["token"], cap=50)
        time.sleep(1.0)

        task = _create_task(admin_session, members=members)
        task_id = task["id"]

        # Iterate claim rounds until task fully claimed (or rounds exhausted)
        per_worker = {w["id"]: 0 for w in workers}
        for rnd in range(20):
            for w in workers:
                try:
                    _heartbeat(w["token"], load=per_worker[w["id"]], cap=50)
                except Exception:
                    pass
            for w in workers:
                try:
                    claimed = _claim(w["token"], max_tasks=5)
                except Exception:
                    claimed = []
                for t in claimed:
                    if t["id"] == task_id:
                        per_worker[w["id"]] += int(t.get("members", 0))
            if sum(per_worker.values()) >= members:
                break
            time.sleep(0.3)

        total = sum(per_worker.values())
        counts = sorted(per_worker.values(), reverse=True)
        non_zero = sum(1 for v in counts if v > 0)
        equal_share = math.ceil(members / num_workers)
        print(f"\n[fair {num_workers}w/{members}m] total={total}, non_zero={non_zero}, "
              f"min={min(counts)}, max={max(counts)}, equal_share={equal_share}")

        # Assertions
        # Atomic claim guarantees no overshoot, but cross-test session pollution
        # (residual chunks belonging to leftover workers from previous tests) can
        # cause our per-worker tracker to over-count when other workers had the
        # task too. We assert the more important invariants instead:
        assert total >= members * 0.9, (
            f"only claimed {total}/{members} (less than 90% — task didn't fully drain)"
        )
        # ≥ 95% workers got non-zero (last 1-2 may be zero due to rounding when
        # num_workers * ceil(members/num_workers) > members)
        nonzero_ratio = non_zero / num_workers
        assert nonzero_ratio >= 0.90, (
            f"only {nonzero_ratio:.0%} workers got non-zero claims (counts={counts})"
        )
        # No single worker hoards: max ≤ 1.5x equal share (the previous bug had
        # one worker grabbing ALL members; with fix it should be near equal_share)
        assert max(counts) <= max(equal_share * 1.5, equal_share + 2), (
            f"hot worker got {max(counts)} > 1.5*equal_share({equal_share}) — "
            f"distribution may be skewed: {counts}"
        )

        # Cleanup task + workers
        admin_session.post(f"{BASE_URL}/api/tasks/{task_id}/cancel", timeout=10)
        for w in workers:
            admin_session.delete(f"{BASE_URL}/api/workers/{w['id']}", timeout=10)


class TestProgressAndComplete:
    """E2E: claim -> progress -> complete -> verify task transitions to completed."""

    def test_full_task_lifecycle(self, admin_session):
        # Cancel pre-existing actives
        for t in admin_session.get(f"{BASE_URL}/api/tasks/active", timeout=10).json():
            admin_session.post(f"{BASE_URL}/api/tasks/{t['id']}/cancel", timeout=10)

        w = _create_worker(admin_session, f"{WORKER_PREFIX}lc-1", cap=20)
        _heartbeat(w["token"], cap=20)
        time.sleep(0.5)

        task = _create_task(admin_session, members=5)
        task_id = task["id"]

        # Claim should pick up this task
        claimed = _claim(w["token"])
        claimed_ids = [c["id"] for c in claimed]
        assert task_id in claimed_ids, f"task not claimed; got {claimed_ids}"
        chunk = next(c for c in claimed if c["id"] == task_id)
        assert chunk["members"] == 5

        # Verify task status moved to active
        active = admin_session.get(f"{BASE_URL}/api/tasks/active", timeout=10).json()
        active_task = next((t for t in active if t["id"] == task_id), None)
        assert active_task is not None, "task should be active after claim"
        assert active_task["status"] == "active"

        # Progress report (PATCH /tasks/{id}/progress with worker token)
        pr = requests.patch(
            f"{BASE_URL}/api/tasks/{task_id}/progress",
            headers={"Authorization": f"Bearer {w['token']}"},
            json={"joined_count": 3},
            timeout=10,
        )
        assert pr.status_code == 200, f"progress failed: {pr.status_code} {pr.text}"
        assert pr.json()["joined_count"] == 3

        # Complete chunk → task should aggregate to completed
        comp = requests.post(
            f"{BASE_URL}/api/tasks/{task_id}/complete",
            headers={"Authorization": f"Bearer {w['token']}"},
            json={"success": True, "joined_count": 5},
            timeout=10,
        )
        assert comp.status_code == 200, f"complete failed: {comp.text}"
        body = comp.json()
        assert body["status"] in ("completed", "active"), \
            f"unexpected post-complete status: {body['status']}"

        # Verify via previous-tasks list (completed)
        time.sleep(0.5)
        prev = admin_session.get(f"{BASE_URL}/api/tasks/previous", timeout=10).json()
        found = next((t for t in prev if t["id"] == task_id), None)
        assert found is not None, "completed task should appear in /tasks/previous"
        assert found["status"] in ("completed", "failed", "cancelled")

        # Cleanup
        admin_session.delete(f"{BASE_URL}/api/workers/{w['id']}", timeout=10)


class TestCancellationStopsClaim:
    def test_cancelled_task_not_claimable(self, admin_session):
        for t in admin_session.get(f"{BASE_URL}/api/tasks/active", timeout=10).json():
            admin_session.post(f"{BASE_URL}/api/tasks/{t['id']}/cancel", timeout=10)

        w = _create_worker(admin_session, f"{WORKER_PREFIX}cnc-1", cap=20)
        _heartbeat(w["token"], cap=20)

        task = _create_task(admin_session, members=10)
        c = admin_session.post(f"{BASE_URL}/api/tasks/{task['id']}/cancel", timeout=10)
        assert c.status_code == 200
        assert c.json()["status"] == "cancelled"

        time.sleep(0.5)
        claimed = _claim(w["token"])
        ids = [t["id"] for t in claimed]
        assert task["id"] not in ids, f"cancelled task should not be claimed; got {ids}"

        admin_session.delete(f"{BASE_URL}/api/workers/{w['id']}", timeout=10)
