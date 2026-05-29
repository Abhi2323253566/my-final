"""
Backend tests for the new RDP Worker fleet management endpoints.
Covers: /api/workers CRUD, worker-auth, /workers/me/heartbeat,
/workers/me/claim, /tasks/{id}/progress, /tasks/{id}/complete,
and the updated task_poller grace behaviour.
"""
import os
import time
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests


# ---------------- helpers ----------------
def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _unique(prefix: str = "TEST_W") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def workers_cleanup(authed_session, base_url):
    """Track worker ids created in the test and remove them at teardown."""
    created: list[str] = []
    yield created
    for wid in created:
        try:
            authed_session.delete(f"{base_url}/api/workers/{wid}", timeout=10)
        except Exception:
            pass


@pytest.fixture
def tasks_cleanup(authed_session, base_url):
    created: list[str] = []
    yield created
    for tid in created:
        try:
            authed_session.delete(f"{base_url}/api/tasks/{tid}", timeout=10)
        except Exception:
            pass


# =================================================================
# Worker CRUD (user-authenticated)
# =================================================================
class TestWorkerCRUD:
    def test_create_worker_returns_plain_token(self, authed_session, base_url, workers_cleanup):
        name = _unique()
        r = authed_session.post(f"{base_url}/api/workers", json={"name": name, "capacity_max": 50})
        assert r.status_code == 200, r.text
        body = r.json()
        workers_cleanup.append(body["id"])

        # one-time plaintext token must be present and in '<id>.<secret>' format
        assert "token" in body and isinstance(body["token"], str)
        assert "." in body["token"]
        wid, secret = body["token"].split(".", 1)
        assert wid == body["id"]
        assert len(secret) > 16
        assert body["name"] == name
        assert body["capacity_max"] == 50
        assert body["status"] == "offline"          # no heartbeat yet
        assert body["current_load"] == 0

    def test_list_workers_hides_token_hash(self, authed_session, base_url, workers_cleanup):
        # create one so the list is non-empty
        r = authed_session.post(f"{base_url}/api/workers", json={"name": _unique()})
        assert r.status_code == 200
        workers_cleanup.append(r.json()["id"])

        r = authed_session.get(f"{base_url}/api/workers")
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list) and len(rows) >= 1
        for row in rows:
            assert "token_hash" not in row
            assert "token" not in row
            assert {"id", "name", "status", "capacity_max", "current_load"}.issubset(row.keys())

    def test_duplicate_name_returns_400(self, authed_session, base_url, workers_cleanup):
        name = _unique()
        r1 = authed_session.post(f"{base_url}/api/workers", json={"name": name})
        assert r1.status_code == 200
        workers_cleanup.append(r1.json()["id"])

        r2 = authed_session.post(f"{base_url}/api/workers", json={"name": name})
        assert r2.status_code == 400
        assert "already exists" in r2.json().get("detail", "").lower()

    def test_delete_worker_unassigns_active_tasks(self, authed_session, base_url, workers_cleanup, tasks_cleanup):
        # create worker
        r = authed_session.post(f"{base_url}/api/workers", json={"name": _unique(), "capacity_max": 100})
        assert r.status_code == 200
        w = r.json()
        token = w["token"]

        # create an active task (no schedule)
        rt = authed_session.post(f"{base_url}/api/tasks", json={
            "meeting_id": "111222333", "members": 2, "timeout": 600,
        })
        assert rt.status_code == 200, rt.text
        task = rt.json()
        tasks_cleanup.append(task["id"])
        assert task["status"] == "active"

        # worker claims it
        claim = requests.post(f"{base_url}/api/workers/me/claim?max_tasks=5", headers=_bearer(token), timeout=15)
        assert claim.status_code == 200, claim.text
        claimed_ids = [t["id"] for t in claim.json()["tasks"]]
        assert task["id"] in claimed_ids

        # delete the worker
        d = authed_session.delete(f"{base_url}/api/workers/{w['id']}")
        assert d.status_code == 200

        # task should now have worker_id cleared
        active = authed_session.get(f"{base_url}/api/tasks/active").json()
        rec = next((t for t in active if t["id"] == task["id"]), None)
        assert rec is not None
        assert rec["worker_id"] in (None, "")
        # we already deleted the worker, no need to clean it up
        # workers_cleanup.append(w["id"]) intentionally omitted


# =================================================================
# Worker auth (Bearer token)
# =================================================================
class TestWorkerAuth:
    def test_no_bearer_returns_401_with_string_detail(self, base_url):
        r = requests.post(f"{base_url}/api/workers/me/heartbeat", json={"current_load": 0}, timeout=10)
        assert r.status_code == 401
        body = r.json()
        assert "detail" in body
        # detail must be a string, NOT a FastAPI validation array
        assert isinstance(body["detail"], str), f"expected str detail, got {type(body['detail']).__name__}: {body['detail']}"

    def test_malformed_token_no_dot(self, base_url):
        r = requests.post(f"{base_url}/api/workers/me/heartbeat",
                          headers=_bearer("not-a-valid-token-without-dot"),
                          json={"current_load": 0}, timeout=10)
        assert r.status_code == 401
        assert isinstance(r.json().get("detail"), str)

    def test_wrong_secret_returns_401(self, authed_session, base_url, workers_cleanup):
        r = authed_session.post(f"{base_url}/api/workers", json={"name": _unique()})
        assert r.status_code == 200
        w = r.json()
        workers_cleanup.append(w["id"])
        wid = w["id"]

        # Right worker id, wrong secret
        bad = f"{wid}.{'x' * 40}"
        r2 = requests.post(f"{base_url}/api/workers/me/heartbeat",
                           headers=_bearer(bad), json={"current_load": 0}, timeout=10)
        assert r2.status_code == 401
        assert isinstance(r2.json().get("detail"), str)


# =================================================================
# Heartbeat / /workers/me
# =================================================================
class TestHeartbeat:
    def test_heartbeat_updates_fields_and_flips_online(self, authed_session, base_url, workers_cleanup):
        r = authed_session.post(f"{base_url}/api/workers", json={"name": _unique(), "capacity_max": 80})
        assert r.status_code == 200
        w = r.json()
        workers_cleanup.append(w["id"])
        token = w["token"]

        hb = requests.post(f"{base_url}/api/workers/me/heartbeat",
                           headers=_bearer(token),
                           json={
                               "current_load": 7,
                               "cpu_pct": 42.5,
                               "ram_pct": 71.2,
                               "hostname": "RDP-TEST",
                               "os_info": "Windows Server 2022",
                           },
                           timeout=15)
        assert hb.status_code == 200, hb.text
        body = hb.json()
        assert body["status"] == "online"
        assert body["current_load"] == 7
        assert body["cpu_pct"] == pytest.approx(42.5)
        assert body["ram_pct"] == pytest.approx(71.2)
        assert body["hostname"] == "RDP-TEST"
        assert body["os_info"] == "Windows Server 2022"
        assert body["last_heartbeat"]

        # GET /workers/me returns the same
        me = requests.get(f"{base_url}/api/workers/me", headers=_bearer(token), timeout=10)
        assert me.status_code == 200
        m = me.json()
        assert m["id"] == w["id"]
        assert m["current_load"] == 7
        assert m["status"] == "online"

        # user-side list also reports status=online
        listing = authed_session.get(f"{base_url}/api/workers").json()
        row = next((x for x in listing if x["id"] == w["id"]), None)
        assert row is not None
        assert row["status"] == "online"


# =================================================================
# Claim / Progress / Complete
# =================================================================
class TestClaimProgressComplete:
    def _make_worker(self, authed_session, base_url, workers_cleanup, capacity=10, load=0):
        r = authed_session.post(f"{base_url}/api/workers", json={"name": _unique(), "capacity_max": capacity})
        assert r.status_code == 200
        w = r.json()
        workers_cleanup.append(w["id"])
        if load:
            requests.post(f"{base_url}/api/workers/me/heartbeat",
                          headers=_bearer(w["token"]),
                          json={"current_load": load}, timeout=10)
        return w

    def _make_task(self, authed_session, base_url, tasks_cleanup, members=3, timeout=600, scheduled_at=None):
        body = {"meeting_id": "999000111", "members": members, "timeout": timeout}
        if scheduled_at:
            body["scheduled_at"] = scheduled_at
        r = authed_session.post(f"{base_url}/api/tasks", json=body)
        assert r.status_code == 200, r.text
        t = r.json()
        tasks_cleanup.append(t["id"])
        return t

    def test_claim_returns_names_indian_with_cycling(self, authed_session, base_url, workers_cleanup, tasks_cleanup):
        w = self._make_worker(authed_session, base_url, workers_cleanup, capacity=100)
        members = 45        # > builtin Indian pool size (40), forces cycling
        t = self._make_task(authed_session, base_url, tasks_cleanup, members=members)

        r = requests.post(f"{base_url}/api/workers/me/claim?max_tasks=5",
                          headers=_bearer(w["token"]), timeout=15)
        assert r.status_code == 200, r.text
        claimed = r.json()["tasks"]
        match = next((c for c in claimed if c["id"] == t["id"]), None)
        assert match is not None, f"task {t['id']} not in claimed {claimed}"
        assert match["worker_id"] == w["id"]
        assert match["worker_name"] == w["name"]
        assert "names" in match
        assert len(match["names"]) == members
        # Builtin Indian pool has 40 items -> first 5 should repeat at index 40
        assert match["names"][0] == match["names"][40]

    def test_claim_respects_capacity_left(self, authed_session, base_url, workers_cleanup, tasks_cleanup):
        # capacity_max=5, current_load=4 -> capacity_left=1
        w = self._make_worker(authed_session, base_url, workers_cleanup, capacity=5, load=4)
        # create 3 tasks
        ids = [self._make_task(authed_session, base_url, tasks_cleanup, members=1)["id"] for _ in range(3)]

        r = requests.post(f"{base_url}/api/workers/me/claim?max_tasks=5",
                          headers=_bearer(w["token"]), timeout=15)
        assert r.status_code == 200, r.text
        claimed_ids = [c["id"] for c in r.json()["tasks"]]
        # Only one of our tasks should have been claimed by THIS worker
        mine_claimed = [i for i in claimed_ids if i in ids]
        assert len(mine_claimed) == 1, f"expected exactly 1 claimed (capacity_left=1), got {mine_claimed}"

    def test_claim_skips_scheduled_tasks(self, authed_session, base_url, workers_cleanup, tasks_cleanup):
        w = self._make_worker(authed_session, base_url, workers_cleanup, capacity=50)
        # scheduled 5 minutes from now -> status="scheduled", not claimable
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        t = self._make_task(authed_session, base_url, tasks_cleanup, members=2, scheduled_at=future)
        assert t["status"] == "scheduled"

        r = requests.post(f"{base_url}/api/workers/me/claim?max_tasks=10",
                          headers=_bearer(w["token"]), timeout=15)
        assert r.status_code == 200
        ids = [c["id"] for c in r.json()["tasks"]]
        assert t["id"] not in ids, "scheduled task should NOT be claimable yet"

    def test_progress_404_if_task_not_assigned_to_worker(self, authed_session, base_url, workers_cleanup, tasks_cleanup):
        w1 = self._make_worker(authed_session, base_url, workers_cleanup)
        w2 = self._make_worker(authed_session, base_url, workers_cleanup)
        t = self._make_task(authed_session, base_url, tasks_cleanup, members=2)

        # w1 claims
        c = requests.post(f"{base_url}/api/workers/me/claim?max_tasks=5",
                          headers=_bearer(w1["token"]), timeout=15)
        assert c.status_code == 200
        assert any(x["id"] == t["id"] for x in c.json()["tasks"])

        # w2 tries to report progress -> 404
        r = requests.patch(f"{base_url}/api/tasks/{t['id']}/progress",
                           headers=_bearer(w2["token"]),
                           json={"joined_count": 1}, timeout=10)
        assert r.status_code == 404

        # w1 reports progress -> 200, joined_count persisted
        r2 = requests.patch(f"{base_url}/api/tasks/{t['id']}/progress",
                            headers=_bearer(w1["token"]),
                            json={"joined_count": 2}, timeout=10)
        assert r2.status_code == 200, r2.text
        assert r2.json()["joined_count"] == 2

    def test_complete_marks_status_and_persists(self, authed_session, base_url, workers_cleanup, tasks_cleanup):
        w = self._make_worker(authed_session, base_url, workers_cleanup)
        t_ok = self._make_task(authed_session, base_url, tasks_cleanup, members=2)
        t_fail = self._make_task(authed_session, base_url, tasks_cleanup, members=2)

        c = requests.post(f"{base_url}/api/workers/me/claim?max_tasks=10",
                          headers=_bearer(w["token"]), timeout=15)
        assert c.status_code == 200
        claimed_ids = [x["id"] for x in c.json()["tasks"]]
        assert t_ok["id"] in claimed_ids and t_fail["id"] in claimed_ids

        # success
        r1 = requests.post(f"{base_url}/api/tasks/{t_ok['id']}/complete",
                           headers=_bearer(w["token"]),
                           json={"success": True, "joined_count": 2}, timeout=10)
        assert r1.status_code == 200, r1.text
        assert r1.json()["status"] == "completed"
        assert r1.json()["joined_count"] == 2

        # failure
        r2 = requests.post(f"{base_url}/api/tasks/{t_fail['id']}/complete",
                           headers=_bearer(w["token"]),
                           json={"success": False, "joined_count": 1, "error": "rdp crashed"}, timeout=10)
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["status"] == "failed"
        assert body["joined_count"] == 1
        assert body["error"] == "rdp crashed"

        # /tasks/previous exposes worker_id/joined_count/error
        prev = authed_session.get(f"{base_url}/api/tasks/previous").json()
        fail_rec = next((x for x in prev if x["id"] == t_fail["id"]), None)
        assert fail_rec is not None
        assert fail_rec["worker_id"] == w["id"]
        assert fail_rec["worker_name"] == w["name"]
        assert fail_rec["error"] == "rdp crashed"

    def test_complete_404_if_not_assigned(self, authed_session, base_url, workers_cleanup, tasks_cleanup):
        w1 = self._make_worker(authed_session, base_url, workers_cleanup)
        w2 = self._make_worker(authed_session, base_url, workers_cleanup)
        t = self._make_task(authed_session, base_url, tasks_cleanup, members=2)
        c = requests.post(f"{base_url}/api/workers/me/claim?max_tasks=5",
                          headers=_bearer(w1["token"]), timeout=15)
        assert c.status_code == 200
        assert any(x["id"] == t["id"] for x in c.json()["tasks"])

        r = requests.post(f"{base_url}/api/tasks/{t['id']}/complete",
                          headers=_bearer(w2["token"]),
                          json={"success": True}, timeout=10)
        assert r.status_code == 404


# =================================================================
# Poller grace behaviour (worker-assigned tasks)
# =================================================================
class TestPollerGrace:
    def test_worker_owned_task_not_autocompleted_at_ends_at(self, authed_session, base_url, workers_cleanup, tasks_cleanup):
        # Create worker + a task with timeout=2s. Worker claims it.
        rW = authed_session.post(f"{base_url}/api/workers", json={"name": _unique(), "capacity_max": 50})
        assert rW.status_code == 200
        w = rW.json()
        workers_cleanup.append(w["id"])

        rT = authed_session.post(f"{base_url}/api/tasks", json={
            "meeting_id": "445566778", "members": 2, "timeout": 10,  # min allowed is 10
        })
        assert rT.status_code == 200, rT.text
        t = rT.json()
        tasks_cleanup.append(t["id"])

        c = requests.post(f"{base_url}/api/workers/me/claim?max_tasks=5",
                          headers=_bearer(w["token"]), timeout=15)
        assert c.status_code == 200
        assert any(x["id"] == t["id"] for x in c.json()["tasks"])

        # ends_at is now+10s; poller every 5s; grace = 60s.
        # Wait ~17s — past ends_at, well inside the 60s grace window.
        time.sleep(17)

        active = authed_session.get(f"{base_url}/api/tasks/active").json()
        rec = next((x for x in active if x["id"] == t["id"]), None)
        assert rec is not None, "worker-owned task should still be active during grace window"
        assert rec["status"] == "active"
        assert rec["worker_id"] == w["id"]

    def test_unassigned_task_still_autocompletes_at_ends_at(self, authed_session, base_url, tasks_cleanup):
        rT = authed_session.post(f"{base_url}/api/tasks", json={
            "meeting_id": "778899001", "members": 2, "timeout": 10,
        })
        assert rT.status_code == 200, rT.text
        t = rT.json()
        tasks_cleanup.append(t["id"])
        assert t["status"] == "active"
        assert t.get("worker_id") in (None, "")

        # poller cadence is 5s; wait >= timeout + 1 cycle
        time.sleep(17)
        prev = authed_session.get(f"{base_url}/api/tasks/previous").json()
        rec = next((x for x in prev if x["id"] == t["id"]), None)
        assert rec is not None, "unassigned task with no worker should auto-complete"
        assert rec["status"] == "completed"
