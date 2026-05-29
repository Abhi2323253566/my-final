"""v8.3.6 STRICT capacity enforcement — HTTP integration tests.

Verifies the full round-trip: admin login → create worker → PATCH capacity_max →
send heartbeat with very HIGH reported_capacity → re-fetch list → confirm
`capacity_max` is unchanged (admin is law, reported is telemetry-only).
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL", "https://vps-deploy-guide-3.preview.emergentagent.com"
).rstrip("/")
ADMIN_EMAIL = "admin@finalzoom.com"
ADMIN_PASSWORD = "Admin@FinalZoom2026"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert data.get("email") == ADMIN_EMAIL
    return s


@pytest.fixture()
def created_worker(admin_session):
    """Create a throwaway worker, yield (id, token, name), then delete."""
    name = f"TEST_strict_cap_{uuid.uuid4().hex[:8]}"
    r = admin_session.post(
        f"{BASE_URL}/api/workers",
        json={"name": name, "capacity_max": 10},
        timeout=15,
    )
    assert r.status_code == 200, f"Create worker failed: {r.text}"
    body = r.json()
    wid = body["id"]
    token = body["token"]
    assert body["capacity_max"] == 10
    yield wid, token, name
    # Teardown
    admin_session.delete(f"{BASE_URL}/api/workers/{wid}", timeout=15)


# ---- Test 1: admin login + auth/me works -------------------------------------
def test_admin_login_returns_200(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=10)
    assert r.status_code == 200
    assert r.json().get("email") == ADMIN_EMAIL


# ---- Test 2: GET /api/workers has capacity_max & reported_capacity keys ------
def test_workers_list_has_capacity_fields(admin_session, created_worker):
    wid, _, name = created_worker
    r = admin_session.get(f"{BASE_URL}/api/workers", timeout=10)
    assert r.status_code == 200
    workers = r.json()
    target = next((w for w in workers if w["id"] == wid), None)
    assert target is not None, "Newly-created worker missing from list"
    assert "capacity_max" in target
    assert "reported_capacity" in target
    assert target["capacity_max"] == 10


# ---- Test 3: PATCH capacity_max to 1, 10, 100 — persists exactly -------------
@pytest.mark.parametrize("cap", [1, 10, 100])
def test_patch_capacity_persists_exactly(admin_session, created_worker, cap):
    wid, _, _ = created_worker
    r = admin_session.patch(
        f"{BASE_URL}/api/workers/{wid}",
        json={"capacity_max": cap},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    assert r.json()["capacity_max"] == cap

    # GET round-trip — value must persist verbatim
    r2 = admin_session.get(f"{BASE_URL}/api/workers", timeout=10)
    target = next(w for w in r2.json() if w["id"] == wid)
    assert target["capacity_max"] == cap, (
        f"PATCH/GET mismatch: PATCH={cap}, GET={target['capacity_max']}"
    )


# ---- Test 4: heartbeat with HIGH reported_capacity does NOT alter capacity_max
def test_high_reported_capacity_does_not_inflate_admin_cap(admin_session, created_worker):
    """The smoking gun: admin cap=50, worker reports 200 → cap_max stays 50."""
    wid, token, _ = created_worker

    # Admin sets cap to 50
    admin_session.patch(
        f"{BASE_URL}/api/workers/{wid}", json={"capacity_max": 50}, timeout=10
    )

    # Worker heartbeat with reported_capacity=200 (4x admin cap)
    hb_headers = {"Authorization": f"Bearer {token}"}
    hb_payload = {
        "current_load": 0,
        "cpu_pct": 12.0,
        "ram_pct": 40.0,
        "reported_capacity": 200,
        "hostname": "test-rdp",
        "os_info": "linux",
    }
    r = requests.post(
        f"{BASE_URL}/api/workers/me/heartbeat",
        json=hb_payload,
        headers=hb_headers,
        timeout=15,
    )
    assert r.status_code == 200, f"Heartbeat failed: {r.text}"
    hb_body = r.json()
    assert hb_body["capacity_max"] == 50, "Admin cap was inflated by reported_capacity!"
    assert hb_body["reported_capacity"] == 200, "reported telemetry not stored"

    # Re-confirm via GET /api/workers
    r2 = admin_session.get(f"{BASE_URL}/api/workers", timeout=10)
    target = next(w for w in r2.json() if w["id"] == wid)
    assert target["capacity_max"] == 50
    assert target["reported_capacity"] == 200


# ---- Test 5: heartbeat with LOW reported_capacity does NOT shrink admin_cap --
def test_low_reported_capacity_does_not_shrink_admin_cap(admin_session, created_worker):
    """Admin cap=50, worker reports 5 → scheduler still sees 50."""
    wid, token, _ = created_worker
    admin_session.patch(
        f"{BASE_URL}/api/workers/{wid}", json={"capacity_max": 50}, timeout=10
    )
    hb_headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(
        f"{BASE_URL}/api/workers/me/heartbeat",
        json={
            "current_load": 0,
            "cpu_pct": 80.0,
            "ram_pct": 90.0,
            "reported_capacity": 5,
        },
        headers=hb_headers,
        timeout=15,
    )
    assert r.status_code == 200
    assert r.json()["capacity_max"] == 50, "Admin cap shrank to reported_capacity!"

    r2 = admin_session.get(f"{BASE_URL}/api/workers", timeout=10)
    target = next(w for w in r2.json() if w["id"] == wid)
    assert target["capacity_max"] == 50
    assert target["reported_capacity"] == 5


# ---- Test 6: admin cap=1 is honored even when reported=70 (user's complaint) -
def test_admin_cap_one_strict_even_with_high_reported(admin_session, created_worker):
    wid, token, _ = created_worker
    admin_session.patch(
        f"{BASE_URL}/api/workers/{wid}", json={"capacity_max": 1}, timeout=10
    )
    hb_headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(
        f"{BASE_URL}/api/workers/me/heartbeat",
        json={
            "current_load": 0,
            "cpu_pct": 5.0,
            "ram_pct": 30.0,
            "reported_capacity": 70,
        },
        headers=hb_headers,
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["capacity_max"] == 1, "cap=1 was overridden — STRICT mode broken"
    assert body["reported_capacity"] == 70


# ---- Test 7: /api/admin/fleet-health no regression ---------------------------
def test_fleet_health_endpoint_ok(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/fleet-health", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    # Should be a dict with some heatmap/aggregation keys; just sanity-check it's JSON
    assert isinstance(body, (dict, list))
