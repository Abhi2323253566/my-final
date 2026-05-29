"""Integration test for EQUAL distribution + topup + worker patch.

Run: cd /app/backend && python tests/test_equal_distribution.py
"""
import os
import time
import requests

API = os.environ.get("API_URL") or "https://meeting-center.preview.emergentagent.com"
ADMIN_EMAIL = "ppandit6926@gmail.com"
ADMIN_PASSWORD = "alok@zoom123"


def login_admin() -> requests.Session:
    s = requests.Session()
    r = s.post(
        f"{API}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    r.raise_for_status()
    return s


def cleanup_workers(s: requests.Session, prefix: str):
    r = s.get(f"{API}/api/workers", timeout=10)
    r.raise_for_status()
    for w in r.json():
        if w["name"].startswith(prefix):
            s.delete(f"{API}/api/workers/{w['id']}", timeout=10)


def cleanup_active_tasks(s: requests.Session):
    r = s.get(f"{API}/api/tasks/active", timeout=10)
    r.raise_for_status()
    for t in r.json():
        s.post(f"{API}/api/tasks/{t['id']}/cancel", timeout=10)


def create_worker(s: requests.Session, name: str, cap: int) -> dict:
    r = s.post(f"{API}/api/workers", json={"name": name, "capacity_max": cap}, timeout=10)
    r.raise_for_status()
    return r.json()


def worker_heartbeat(token: str, load: int = 0):
    r = requests.post(
        f"{API}/api/workers/me/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_load": load, "cpu_pct": 0.0, "ram_pct": 0.0, "hostname": "t", "os_info": "t"},
        timeout=10,
    )
    r.raise_for_status()


def worker_claim(token: str) -> list:
    r = requests.post(
        f"{API}/api/workers/me/claim?max_tasks=5",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("tasks", [])


def create_task(s: requests.Session, members: int) -> str:
    payload = {
        "meeting_id": "1234567890",
        "meeting_password": "test",
        "members": members,
        "name_source": "Indian",
        "timeout": 600,
    }
    r = s.post(f"{API}/api/tasks", json=payload, timeout=10)
    r.raise_for_status()
    return r.json()["id"]


def test_equal_distribution():
    print("\n=== TEST: Equal Distribution ===")
    s = login_admin()
    cleanup_active_tasks(s)
    cleanup_workers(s, "eqtest-")

    workers = []
    for i in range(4):
        w = create_worker(s, f"eqtest-w{i+1}", 100)
        worker_heartbeat(w["token"], 0)
        workers.append(w)
    print(f"Created 4 workers, all heartbeated.")

    tid = create_task(s, 100)
    print(f"Created task {tid[:8]} with 100 members.")

    claims = []
    for w in workers:
        tasks = worker_claim(w["token"])
        got = sum(t["members"] for t in tasks)
        claims.append(got)
        print(f"  {w['name']} claimed {got}")
    print(f"Distribution: {claims}, total={sum(claims)}/100")
    assert all(c > 0 for c in claims), f"NOT EQUAL: {claims}"
    assert max(claims) - min(claims) <= 1, f"Distribution skewed: {claims}"
    print("PASS: equal distribution working")

    # cleanup
    s.post(f"{API}/api/tasks/{tid}/cancel", timeout=10)
    for w in workers:
        s.delete(f"{API}/api/workers/{w['id']}", timeout=10)


def test_worker_patch():
    print("\n=== TEST: Worker capacity PATCH ===")
    s = login_admin()
    cleanup_workers(s, "patch-test")
    w = create_worker(s, "patch-test-1", 50)
    r = s.patch(f"{API}/api/workers/{w['id']}", json={"capacity_max": 200, "name": "patch-test-renamed"}, timeout=10)
    r.raise_for_status()
    out = r.json()
    assert out["capacity_max"] == 200, f"capacity not updated: {out}"
    assert out["name"] == "patch-test-renamed", f"name not updated: {out}"
    print("PASS: PATCH /workers/{id} works")
    s.delete(f"{API}/api/workers/{w['id']}", timeout=10)


def test_topup_flow():
    print("\n=== TEST: Topup flow (request + decide) ===")
    s = login_admin()

    # Create a test user
    test_email = f"topup_test_{int(time.time())}@example.com"
    r = s.post(f"{API}/api/admin/users", json={
        "email": test_email, "password": "test1234",
        "name": "TopupTest", "usage_limit": 100, "credit_rate": 0.5,
    }, timeout=10)
    r.raise_for_status()
    user_id = r.json()["id"]
    print(f"  Created test user {test_email} (rate=0.5)")

    # Login as that user
    su = requests.Session()
    su.post(f"{API}/api/auth/login", json={"email": test_email, "password": "test1234"}, timeout=10).raise_for_status()

    # Submit topup request with tiny base64 image
    tiny_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    r = su.post(f"{API}/api/topup/request", json={
        "amount_rs": 200, "screenshot": tiny_png, "note": "test payment"
    }, timeout=10)
    r.raise_for_status()
    topup = r.json()
    assert topup["credits"] == 100, f"expected 100 credits (200*0.5), got {topup['credits']}"
    assert topup["status"] == "pending"
    print(f"  Submitted topup: ₹200 → {topup['credits']} credits (pending)")

    # Admin approves
    r = s.post(f"{API}/api/admin/topup-requests/{topup['id']}/decide",
               json={"action": "approve", "admin_note": "verified"}, timeout=10)
    r.raise_for_status()
    out = r.json()
    assert out["status"] == "approved"
    print(f"  Admin approved, status={out['status']}")

    # Verify user's usage_limit increased by 100
    users = s.get(f"{API}/api/admin/users", timeout=10).json()
    me = next(u for u in users if u["id"] == user_id)
    assert me["usage_limit"] == 200, f"expected 200 (100 + 100), got {me['usage_limit']}"
    print(f"  User usage_limit now: {me['usage_limit']} (was 100, +100 = 200)")

    # Cleanup
    s.delete(f"{API}/api/admin/users/{user_id}", timeout=10)
    print("PASS: topup flow working")


def test_admin_overview():
    print("\n=== TEST: Admin Overview ===")
    s = login_admin()
    r = s.get(f"{API}/api/admin/overview", timeout=10)
    r.raise_for_status()
    data = r.json()
    assert "users" in data and "revenue" in data and "tasks" in data and "workers" in data
    print(f"  Overview: revenue=₹{data['revenue']['total_rs']}, users={data['users']['total']}, workers={data['workers']['total']}")
    print("PASS: overview endpoint working")


if __name__ == "__main__":
    test_admin_overview()
    test_worker_patch()
    test_topup_flow()
    test_equal_distribution()
    print("\n=== ALL TESTS PASSED ===")
