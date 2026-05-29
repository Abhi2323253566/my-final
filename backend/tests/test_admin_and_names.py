"""
Backend tests for iteration 4:
- Admin user management CRUD (/api/admin/users)
- Role guards (admin-only endpoints reject regular users)
- Random + no-repeat name resolution on worker claim
- Shared worker fleet (admin worker claims tasks created by regular users)
- Worker file download endpoints (zoom_worker.py, NamesIn.txt, requirements, install.ps1, setup-guide)
- Builtin name pools include NamesIn=11006
"""
import os
import time
import uuid
import requests
import pytest


BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL not set"


# ---------------- helpers ----------------
def _u():
    return uuid.uuid4().hex[:10]


def _admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": os.environ.get("ADMIN_EMAIL", "ppandit6926@gmail.com"),
                     "password": os.environ.get("ADMIN_PASSWORD", "alok@zoom123")},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def admin():
    return _admin_session()


@pytest.fixture
def temp_user(admin):
    """Create a regular user via admin API and yield (creds, user_dict, session)."""
    email = f"TEST_user_{_u()}@example.com"
    password = "testpass123"
    r = admin.post(f"{BASE_URL}/api/admin/users",
                   json={"email": email, "password": password, "name": "TEST User", "role": "user", "usage_limit": 100})
    assert r.status_code == 200, r.text
    u = r.json()
    s = requests.Session()
    r2 = s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r2.status_code == 200, r2.text
    yield {"email": email, "password": password}, u, s
    # cleanup
    try:
        admin.delete(f"{BASE_URL}/api/admin/users/{u['id']}")
    except Exception:
        pass


# ---------------- Admin user CRUD ----------------
class TestAdminUserCRUD:
    def test_create_user_success(self, admin):
        email = f"TEST_create_{_u()}@example.com"
        r = admin.post(f"{BASE_URL}/api/admin/users",
                       json={"email": email, "password": "secret123", "name": "Create Me",
                             "role": "user", "usage_limit": 250})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["email"] == email.lower()
        assert body["name"] == "Create Me"
        assert body["role"] == "user"
        assert body["usage_limit"] == 250
        assert body["usage"] == 0
        assert "password_hash" not in body
        assert "id" in body
        # cleanup
        admin.delete(f"{BASE_URL}/api/admin/users/{body['id']}")

    def test_create_duplicate_email_400(self, admin, temp_user):
        _, user, _ = temp_user
        r = admin.post(f"{BASE_URL}/api/admin/users",
                       json={"email": user["email"], "password": "anotherpass", "role": "user"})
        assert r.status_code == 400, r.text

    def test_create_short_password_400(self, admin):
        r = admin.post(f"{BASE_URL}/api/admin/users",
                       json={"email": f"TEST_short_{_u()}@example.com", "password": "abc"})
        assert r.status_code == 400, r.text

    def test_list_users(self, admin, temp_user):
        _, user, _ = temp_user
        r = admin.get(f"{BASE_URL}/api/admin/users")
        assert r.status_code == 200
        users = r.json()
        assert isinstance(users, list)
        ids = [u["id"] for u in users]
        assert user["id"] in ids
        # Schema check
        match = [u for u in users if u["id"] == user["id"]][0]
        for k in ("email", "name", "role", "usage", "usage_limit", "created_at"):
            assert k in match
        assert all("password_hash" not in u for u in users)

    def test_update_user_name_password_limit(self, admin, temp_user):
        creds, user, _ = temp_user
        r = admin.put(f"{BASE_URL}/api/admin/users/{user['id']}",
                      json={"name": "Renamed", "usage_limit": 999, "password": "newpass789"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Renamed"
        assert body["usage_limit"] == 999
        # password rotated -> old password should fail to login, new should succeed
        s = requests.Session()
        bad = s.post(f"{BASE_URL}/api/auth/login", json={"email": creds["email"], "password": creds["password"]})
        assert bad.status_code == 401
        good = s.post(f"{BASE_URL}/api/auth/login", json={"email": creds["email"], "password": "newpass789"})
        assert good.status_code == 200

    def test_update_role_cannot_demote_self(self, admin):
        me = admin.get(f"{BASE_URL}/api/auth/me").json()
        r = admin.put(f"{BASE_URL}/api/admin/users/{me['id']}", json={"role": "user"})
        assert r.status_code == 400, r.text

    def test_reset_usage(self, admin, temp_user):
        _, user, _ = temp_user
        r = admin.post(f"{BASE_URL}/api/admin/users/{user['id']}/reset-usage")
        assert r.status_code == 200, r.text
        assert r.json()["usage"] == 0

    def test_delete_user_cascade(self, admin):
        email = f"TEST_del_{_u()}@example.com"
        r = admin.post(f"{BASE_URL}/api/admin/users",
                       json={"email": email, "password": "secret123", "role": "user"})
        assert r.status_code == 200
        uid = r.json()["id"]
        # delete
        d = admin.delete(f"{BASE_URL}/api/admin/users/{uid}")
        assert d.status_code == 200, d.text
        # list should no longer include this user
        listed = admin.get(f"{BASE_URL}/api/admin/users").json()
        assert uid not in [u["id"] for u in listed]
        # deleting again -> 404
        again = admin.delete(f"{BASE_URL}/api/admin/users/{uid}")
        assert again.status_code == 404

    def test_delete_self_blocked(self, admin):
        me = admin.get(f"{BASE_URL}/api/auth/me").json()
        r = admin.delete(f"{BASE_URL}/api/admin/users/{me['id']}")
        assert r.status_code == 400


# ---------------- Role guards ----------------
class TestRoleGuards:
    def test_user_blocked_from_admin_users_list(self, temp_user):
        _, _, s = temp_user
        r = s.get(f"{BASE_URL}/api/admin/users")
        assert r.status_code == 403, r.text

    def test_user_blocked_from_admin_user_update(self, temp_user):
        _, u, s = temp_user
        r = s.put(f"{BASE_URL}/api/admin/users/{u['id']}", json={"name": "Hack"})
        assert r.status_code == 403

    def test_user_blocked_from_workers_get(self, temp_user):
        _, _, s = temp_user
        r = s.get(f"{BASE_URL}/api/workers")
        assert r.status_code == 403

    def test_user_blocked_from_workers_post(self, temp_user):
        _, _, s = temp_user
        r = s.post(f"{BASE_URL}/api/workers", json={"name": "should-not-create"})
        assert r.status_code == 403

    def test_user_blocked_from_workers_delete(self, temp_user):
        _, _, s = temp_user
        r = s.delete(f"{BASE_URL}/api/workers/some-id")
        assert r.status_code == 403

    def test_user_allowed_on_tasks(self, temp_user):
        _, _, s = temp_user
        # tasks are exposed as /tasks/active | /tasks/scheduled | /tasks/previous | /tasks/download
        for ep in ("active", "scheduled", "previous", "download"):
            r = s.get(f"{BASE_URL}/api/tasks/{ep}")
            assert r.status_code == 200, f"{ep}: {r.status_code} {r.text}"
            assert isinstance(r.json(), list)

    def test_user_allowed_on_name_files(self, temp_user):
        _, _, s = temp_user
        r = s.get(f"{BASE_URL}/api/name-files")
        assert r.status_code == 200
        b = s.get(f"{BASE_URL}/api/name-files/builtin")
        assert b.status_code == 200


# ---------------- Worker file endpoints (public) ----------------
class TestWorkerFiles:
    def test_zoom_worker_py(self):
        r = requests.get(f"{BASE_URL}/api/worker/zoom_worker.py", timeout=20)
        assert r.status_code == 200
        text = r.text
        assert "Zoom Worker (v4)" in text, "v4 banner missing"
        assert "input-for-pwd" in text
        assert "preview-join-button" in text

    def test_names_in_txt(self):
        r = requests.get(f"{BASE_URL}/api/worker/NamesIn.txt", timeout=30)
        assert r.status_code == 200
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        # ~11006 names; allow tolerance
        assert 10900 <= len(lines) <= 11100, f"unexpected line count: {len(lines)}"
        assert all(ln.strip() for ln in lines)

    def test_requirements_txt(self):
        r = requests.get(f"{BASE_URL}/api/worker/requirements.txt", timeout=10)
        assert r.status_code == 200
        assert len(r.text) > 0

    def test_install_ps1(self):
        r = requests.get(f"{BASE_URL}/api/worker/install.ps1", timeout=10)
        assert r.status_code == 200

    def test_setup_guide(self):
        r = requests.get(f"{BASE_URL}/api/worker/setup-guide", timeout=10)
        assert r.status_code == 200


# ---------------- Builtin pools ----------------
class TestBuiltinPools:
    def test_builtin_counts(self, admin):
        r = admin.get(f"{BASE_URL}/api/name-files/builtin")
        assert r.status_code == 200
        pools = {p["name"]: p for p in r.json()}
        assert "NamesIn" in pools
        assert "Indian" in pools
        assert "English" in pools
        assert pools["NamesIn"]["count"] == 11006, f"NamesIn count={pools['NamesIn']['count']}"
        assert pools["Indian"]["count"] == 40
        assert pools["English"]["count"] == 20


# ---------------- Shared worker fleet + random/unique names ----------------
class TestSharedFleetAndNames:
    @pytest.fixture
    def admin_worker(self, admin):
        wname = f"TEST_W_{_u()}"
        r = admin.post(f"{BASE_URL}/api/workers", json={"name": wname, "max_tasks": 50})
        assert r.status_code == 200, r.text
        body = r.json()
        wid = body["id"]
        token = body["token"]
        # heartbeat to mark online
        h = requests.post(f"{BASE_URL}/api/workers/me/heartbeat",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"current_load": 0, "status": "online"}, timeout=10)
        assert h.status_code == 200, h.text
        yield wid, token
        try:
            admin.delete(f"{BASE_URL}/api/workers/{wid}")
        except Exception:
            pass

    def _create_task(self, sess, name_source="NamesIn", members=5, meeting_id=None):
        meeting_id = meeting_id or f"shared{_u()[:6]}"
        # NO scheduled_at -> task is created with status='active' immediately
        payload = {
            "meeting_id": meeting_id,
            "meeting_password": "x",
            "members": members,
            "timeout": 600,
            "name_source": name_source,
        }
        r = sess.post(f"{BASE_URL}/api/tasks", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "active", f"expected active, got {body['status']}"
        return body

    def test_shared_fleet_admin_worker_claims_user_task(self, admin, temp_user, admin_worker):
        _, _, user_sess = temp_user
        wid, token = admin_worker
        task = self._create_task(user_sess, name_source="NamesIn", members=3)
        # admin's worker claims
        r = requests.post(f"{BASE_URL}/api/workers/me/claim",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"max_tasks": 5}, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        ids = [t["id"] for t in body["tasks"]]
        assert task["id"] in ids, f"admin worker didn't claim user task {task['id']}; got {ids}"
        claimed = [t for t in body["tasks"] if t["id"] == task["id"]][0]
        assert "names" in claimed
        assert len(claimed["names"]) == 3
        assert len(set(claimed["names"])) == 3

    def test_namesin_random_unique_and_varies(self, admin, temp_user, admin_worker):
        _, _, user_sess = temp_user
        wid, token = admin_worker
        # two tasks, same source/count, different meeting ids
        t1 = self._create_task(user_sess, name_source="NamesIn", members=20)
        t2 = self._create_task(user_sess, name_source="NamesIn", members=20)
        # claim both
        r = requests.post(f"{BASE_URL}/api/workers/me/claim",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"max_tasks": 10}, timeout=20)
        assert r.status_code == 200, r.text
        tasks = {t["id"]: t for t in r.json()["tasks"]}
        assert t1["id"] in tasks and t2["id"] in tasks
        names1 = tasks[t1["id"]]["names"]
        names2 = tasks[t2["id"]]["names"]
        assert len(names1) == 20 and len(set(names1)) == 20, "names1 not unique"
        assert len(names2) == 20 and len(set(names2)) == 20, "names2 not unique"
        # With 11006-pool and sample of 20, collisions are vanishingly rare
        assert names1 != names2, "two claims returned identical name lists"

    def test_indian_pool_cycling_and_fresh_shuffle(self, admin, temp_user, admin_worker):
        _, _, user_sess = temp_user
        wid, token = admin_worker
        task = self._create_task(user_sess, name_source="Indian", members=100)
        r = requests.post(f"{BASE_URL}/api/workers/me/claim",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"max_tasks": 5}, timeout=15)
        assert r.status_code == 200, r.text
        claimed = [t for t in r.json()["tasks"] if t["id"] == task["id"]]
        assert claimed, "Indian-pool task not claimed"
        names = claimed[0]["names"]
        assert len(names) == 100
        # First 40 should be unique (full shuffle of 40-name pool)
        first_40 = names[:40]
        assert len(set(first_40)) == 40, f"first 40 not unique: {len(set(first_40))}"
        # Beyond 40: pool repeats but every 40-block should be a permutation (unique within block)
        second_40 = names[40:80]
        assert len(set(second_40)) == 40, "second block of 40 not unique"
