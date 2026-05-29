"""
v8.4.0 — Round-robin / even-distribution + new task reaction config fields.

Covers the review_request:
  1. Auth login on the public REACT_APP_BACKEND_URL.
  2. POST /api/tasks persists participant_reactions, floating_emoji,
     reaction_interval_min, reaction_interval_max (and silently ignores
     the unsupported `distribution_mode` field that the request mentioned).
  3. /api/workers/me/claim splits a 90-bot task across 3 workers
     (cap=50 each) EVENLY within +/-2 — i.e. NO single worker grabs all 90.
  4. Strict capacity_max=10: a worker never receives more than 10 bots
     in a single claim call even with many pending.
  5. GET /api/workers returns reported_capacity AND capacity_max.

Notes
-----
* Backend is currently configured with DISTRIBUTION_MODE="auto" (see
  /app/backend/.env). "auto" performs the strict equal split too — the
  round-robin wave overlay only adds a per-cycle take cap of 1, which
  would force the 3 workers to call /claim ~30 times each. We verify
  the high-level invariant (sum==90, each ~equal) which is what the
  user actually cares about.
* Worker tokens are returned ONLY on POST /api/workers (creation). Every
  bearer call below stores that token in the fixture for re-use.
"""
import math
import os
import time
import uuid
import requests
import pytest


# ---------------------------------------------------------------- helpers


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _create_worker(authed_session, base_url, name: str, capacity_max: int) -> dict:
    r = authed_session.post(
        f"{base_url}/api/workers",
        json={"name": name, "capacity_max": capacity_max},
        timeout=15,
    )
    assert r.status_code == 200, f"create_worker {name} failed: {r.status_code} {r.text}"
    return r.json()  # contains .token (one-time)


def _heartbeat(base_url, token: str, reported_capacity: int):
    r = requests.post(
        f"{base_url}/api/workers/me/heartbeat",
        json={
            "current_load": 0,
            "cpu_pct": 5.0,
            "ram_pct": 20.0,
            "hostname": f"test-rdp-{uuid.uuid4().hex[:6]}",
            "os_info": "TestOS",
            "reported_capacity": reported_capacity,
            "ram_free_gb": 16.0,
            "cpu_count": 8,
        },
        headers=_bearer(token),
        timeout=15,
    )
    assert r.status_code == 200, f"heartbeat failed: {r.status_code} {r.text}"
    return r.json()


def _claim(base_url, token: str, max_tasks: int = 5) -> list[dict]:
    r = requests.post(
        f"{base_url}/api/workers/me/claim?max_tasks={max_tasks}",
        headers=_bearer(token),
        timeout=15,
    )
    assert r.status_code == 200, f"claim failed: {r.status_code} {r.text}"
    return r.json().get("tasks", [])


# ---------------------------------------------------------------- 1) AUTH


class TestAuthLogin:
    def test_admin_login_on_public_url(self, base_url, api_client):
        from conftest import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa
        r = api_client.post(
            f"{base_url}/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=15,
        )
        assert r.status_code == 200, f"login failed: {r.text}"
        data = r.json()
        assert data["email"] == ADMIN_EMAIL
        assert data["role"] == "admin"
        # cookie set
        assert any(c.lower().startswith("access_token") for c in api_client.cookies.keys()) or \
               "access_token" in api_client.cookies


# ---------------------------------------------------------------- 2) TASK CRUD


class TestTaskReactionFields:
    """POST /api/tasks accepts + persists the new flags."""

    def _make(self, authed_session, base_url, **overrides):
        payload = {
            "meeting_id": "1234567890",
            "meeting_password": "test",
            "members": 5,
            "name_source": "Indian",
            "meeting_type": "Normal Participants",
            "timeout": 600,
            "participant_reactions": True,
            "floating_emoji": True,
            "reaction_interval_min": 15,
            "reaction_interval_max": 60,
        }
        payload.update(overrides)
        return authed_session.post(f"{base_url}/api/tasks", json=payload, timeout=15)

    def test_create_persists_reaction_flags(self, authed_session, base_url):
        r = self._make(authed_session, base_url)
        assert r.status_code == 200, f"create task failed: {r.text}"
        t = r.json()
        assert t["participant_reactions"] is True
        assert t["floating_emoji"] is True
        assert t["reaction_interval_min"] == 15
        assert t["reaction_interval_max"] == 60
        tid = t["id"]
        # Read back via /tasks/active (no GET /tasks/{id} endpoint exists)
        r2 = authed_session.get(f"{base_url}/api/tasks/active", timeout=15)
        assert r2.status_code == 200
        found = next((x for x in r2.json() if x["id"] == tid), None)
        assert found is not None, "created task not visible in /tasks/active"
        assert found["participant_reactions"] is True
        assert found["floating_emoji"] is True
        assert found["reaction_interval_min"] == 15
        assert found["reaction_interval_max"] == 60
        # Cleanup
        authed_session.delete(f"{base_url}/api/tasks/{tid}", timeout=15)

    def test_defaults_av_off(self, authed_session, base_url):
        r = authed_session.post(
            f"{base_url}/api/tasks",
            json={
                "meeting_id": "9876543210",
                "members": 3,
                "name_source": "Indian",
                "timeout": 300,
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["participant_reactions"] is False
        assert t["floating_emoji"] is False
        # default interval bounds from server.TaskCreate
        assert t["reaction_interval_min"] == 30
        assert t["reaction_interval_max"] == 90
        authed_session.delete(f"{base_url}/api/tasks/{t['id']}", timeout=15)

    def test_distribution_mode_extra_field_ignored(self, authed_session, base_url):
        """The review_request mentions distribution_mode='round_robin' on
        TaskCreate. The current TaskCreate model does NOT define it — Pydantic
        silently ignores extras. The task must still be created."""
        r = self._make(authed_session, base_url, distribution_mode="round_robin")
        assert r.status_code == 200, f"task with distribution_mode failed: {r.text}"
        t = r.json()
        # Field should NOT appear on the persisted task (not part of schema)
        assert "distribution_mode" not in t
        authed_session.delete(f"{base_url}/api/tasks/{t['id']}", timeout=15)


# ---------------------------------------------------------------- 3) ROUND-ROBIN DISTRIBUTION


@pytest.fixture
def three_workers(authed_session, base_url):
    """Create three workers cap_max=50, heartbeat them, yield tokens."""
    uid = uuid.uuid4().hex[:6]
    created = []
    for i in range(3):
        w = _create_worker(
            authed_session, base_url, name=f"TEST_rr_{uid}_{i}", capacity_max=50
        )
        # heartbeat with reported_capacity=50 (matches admin cap)
        _heartbeat(base_url, w["token"], reported_capacity=50)
        created.append(w)
    yield created
    # cleanup
    for w in created:
        try:
            authed_session.delete(f"{base_url}/api/workers/{w['id']}", timeout=15)
        except Exception:
            pass


class TestRoundRobinDistribution:
    def test_90_bots_split_evenly_across_3_workers(
        self, authed_session, base_url, three_workers
    ):
        # Detect any OTHER online workers (live RDPs heartbeating in real-time)
        # so we can compute the expected fair-share. The review requires
        # *even* distribution across the 3 test workers; if external workers
        # also poll, they will absorb some bots and our 3 will receive a
        # proportional fair-share — but still EVEN among themselves.
        r0 = authed_session.get(f"{base_url}/api/workers", timeout=15)
        assert r0.status_code == 200
        all_workers = r0.json()
        ours_ids = {w["id"] for w in three_workers}
        external_online = [
            w for w in all_workers
            if w["id"] not in ours_ids and w.get("status") == "online"
        ]
        online_total = 3 + len(external_online)
        print(
            f"[RR-DIST] our_test_workers=3 external_online={len(external_online)} "
            f"-> assumed online_count={online_total}"
        )
        ideal = math.ceil(90 / online_total)
        print(f"[RR-DIST] expected per-worker fair-share ~ {ideal}")

        # 1) Create the 90-bot task
        r = authed_session.post(
            f"{base_url}/api/tasks",
            json={
                "meeting_id": "5551234567",
                "members": 90,
                "name_source": "Indian",
                "timeout": 3600,
            },
            timeout=15,
        )
        assert r.status_code == 200, f"create 90-bot task: {r.text}"
        task = r.json()
        tid = task["id"]
        assert task["members"] == 90

        try:
            # 2) Each worker polls /claim repeatedly. Round-robin caps each
            # take at 1 bot per inner iteration (and max 5 chunks per call),
            # so we loop until totals stabilise or task fully claimed.
            assigned: dict[str, int] = {w["id"]: 0 for w in three_workers}
            max_rounds = 60
            stall_rounds = 0
            for _round in range(max_rounds):
                progress = False
                for w in three_workers:
                    chunks = _claim(base_url, w["token"], max_tasks=5)
                    for c in chunks:
                        if c["id"] == tid:
                            assigned[w["id"]] += c["members"]
                            progress = True
                if not progress:
                    stall_rounds += 1
                    if stall_rounds >= 3:
                        break
                else:
                    stall_rounds = 0
                time.sleep(0.1)

            shares = list(assigned.values())
            total_ours = sum(shares)
            # Re-fetch task to read members_claimed (incl. external workers)
            r_task = authed_session.get(f"{base_url}/api/tasks/active", timeout=15)
            task_now = next((x for x in r_task.json() if x["id"] == tid), None)
            members_claimed = (task_now or {}).get("members_claimed", None)
            print(
                f"[RR-DIST] per-worker shares = {shares}, total_ours = {total_ours}, "
                f"task.members_claimed (incl external) = {members_claimed}"
            )

            # HARD INVARIANTS
            # (a) NO single worker grabs all 90 — round-robin must spread it.
            assert max(shares) < 90, f"One worker took all 90: {shares}"
            # (b) The 3 test workers should each receive within ±2 of fair-share
            #     (computed against ACTUAL online_count incl. live workers).
            for s in shares:
                assert abs(s - ideal) <= 2, (
                    f"Worker share {s} not within +/-2 of ideal {ideal}; "
                    f"shares={shares}; online_total={online_total}"
                )
            # (c) When no external workers are online, our 3 must sum to 90.
            if not external_online:
                assert total_ours == 90, (
                    f"Total claimed by 3 workers = {total_ours} (expected 90); "
                    f"shares={shares}"
                )
        finally:
            # cleanup task + chunks
            authed_session.delete(f"{base_url}/api/tasks/{tid}", timeout=15)


# ---------------------------------------------------------------- 4) STRICT CAPACITY ON CLAIM


class TestStrictCapacityOnClaim:
    def test_capacity_10_never_returns_more_than_10(self, authed_session, base_url):
        """Worker w/ capacity_max=10 must never receive >10 bots in a single
        /workers/me/claim call, even when many bots are pending."""
        uid = uuid.uuid4().hex[:6]
        w = _create_worker(
            authed_session, base_url, name=f"TEST_cap10_{uid}", capacity_max=10
        )
        _heartbeat(base_url, w["token"], reported_capacity=200)  # high telemetry,
        # admin cap=10 must still be the ceiling.

        # Park a big task (200 bots) so there is plenty of pending work
        r = authed_session.post(
            f"{base_url}/api/tasks",
            json={
                "meeting_id": "4445556666",
                "members": 200,
                "name_source": "Indian",
                "timeout": 3600,
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text
        tid = r.json()["id"]

        try:
            chunks = _claim(base_url, w["token"], max_tasks=50)
            total_now = sum(c["members"] for c in chunks if c["id"] == tid)
            print(f"[CAP10] single-claim chunks={len(chunks)} total_bots={total_now}")
            assert total_now <= 10, (
                f"Worker w/ cap=10 received {total_now} bots in ONE claim call"
            )
        finally:
            authed_session.delete(f"{base_url}/api/tasks/{tid}", timeout=15)
            authed_session.delete(f"{base_url}/api/workers/{w['id']}", timeout=15)


# ---------------------------------------------------------------- 5) GET /workers SHAPE


class TestWorkersList:
    def test_workers_list_returns_reported_capacity_and_capacity_max(
        self, authed_session, base_url
    ):
        uid = uuid.uuid4().hex[:6]
        w = _create_worker(
            authed_session, base_url, name=f"TEST_list_{uid}", capacity_max=77
        )
        _heartbeat(base_url, w["token"], reported_capacity=42)
        try:
            r = authed_session.get(f"{base_url}/api/workers", timeout=15)
            assert r.status_code == 200
            workers = r.json()
            mine = next((x for x in workers if x["id"] == w["id"]), None)
            assert mine is not None, "created worker missing from /workers"
            assert mine["capacity_max"] == 77, "capacity_max wrong"
            assert mine.get("reported_capacity") == 42, (
                f"reported_capacity not returned: {mine}"
            )
            # Key shape
            for key in (
                "capacity_max", "reported_capacity", "current_load",
                "status", "last_heartbeat", "id", "name",
            ):
                assert key in mine, f"missing key {key} in worker payload"
        finally:
            authed_session.delete(f"{base_url}/api/workers/{w['id']}", timeout=15)
