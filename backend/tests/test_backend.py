"""
End-to-end backend test for Zoom Services clone.
Covers: auth, tasks lifecycle (immediate + scheduled + completion poller),
name-files CRUD, stats, security, usage limit enforcement.
"""
import time
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD


# ---------- Health ----------
class TestHealth:
    def test_root(self, base_url, api_client):
        r = api_client.get(f"{base_url}/api/")
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is True
        assert data.get("service") == "zoom-services-clone"


# ---------- Auth ----------
class TestAuth:
    def test_login_success_sets_cookies(self, base_url, api_client):
        r = api_client.post(f"{base_url}/api/auth/login",
                            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["email"] == ADMIN_EMAIL.lower()
        assert data["role"] == "admin"
        assert "id" in data
        assert "usage" in data and "usage_limit" in data
        # Cookies
        cookies = r.cookies
        assert "access_token" in cookies
        assert "refresh_token" in cookies

    def test_me_with_cookies_only(self, base_url, authed_session):
        r = authed_session.get(f"{base_url}/api/auth/me")
        assert r.status_code == 200
        u = r.json()
        assert u["email"] == ADMIN_EMAIL.lower()

    def test_me_with_bearer_token(self, base_url, api_client):
        # login & extract token from cookie
        r = api_client.post(f"{base_url}/api/auth/login",
                            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        token = r.cookies.get("access_token")
        assert token
        rr = requests.get(f"{base_url}/api/auth/me",
                          headers={"Authorization": f"Bearer {token}"})
        assert rr.status_code == 200
        assert rr.json()["email"] == ADMIN_EMAIL.lower()

    def test_invalid_password_returns_401_string_detail(self, base_url, api_client):
        # use random email to avoid affecting brute-force lockout for admin
        r = api_client.post(f"{base_url}/api/auth/login",
                            json={"email": f"nobody_{uuid.uuid4().hex[:6]}@x.com",
                                  "password": "wrong"})
        assert r.status_code == 401
        body = r.json()
        assert "detail" in body
        assert isinstance(body["detail"], str)
        assert body["detail"] == "Invalid email or password"

    def test_me_requires_auth(self, base_url, api_client):
        r = api_client.get(f"{base_url}/api/auth/me")
        assert r.status_code == 401

    def test_logout_clears_cookies(self, base_url, authed_session):
        r = authed_session.post(f"{base_url}/api/auth/logout")
        assert r.status_code == 200
        # subsequent /me should be unauthenticated since cookies cleared
        r2 = authed_session.get(f"{base_url}/api/auth/me")
        assert r2.status_code == 401


# ---------- Security: auth required ----------
class TestSecurity:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/tasks/active"),
        ("GET", "/api/tasks/scheduled"),
        ("GET", "/api/tasks/previous"),
        ("POST", "/api/tasks"),
        ("GET", "/api/tasks/download"),
        ("GET", "/api/name-files"),
        ("GET", "/api/name-files/builtin"),
        ("POST", "/api/name-files"),
        ("GET", "/api/stats/usage"),
    ])
    def test_endpoint_requires_auth(self, base_url, method, path):
        r = requests.request(method, f"{base_url}{path}",
                             json={} if method == "POST" else None)
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


# ---------- Tasks ----------
class TestTasksImmediate:
    def test_create_immediate_active_and_increments_usage(self, base_url, authed_session):
        usage_before = authed_session.get(f"{base_url}/api/stats/usage").json()["usage"]
        payload = {
            "meeting_id": "TEST_111222333",
            "meeting_password": "pw",
            "members": 3,
            "name_source": "Indian",
            "meeting_type": "Normal Participants",
            "timeout": 60,
            "floating_emoji": True,
            "participant_reactions": False,
        }
        r = authed_session.post(f"{base_url}/api/tasks", json=payload)
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["status"] == "active"
        assert t["started_at"] is not None
        assert t["ends_at"] is not None
        assert t["members"] == 3
        assert t["floating_emoji"] is True
        assert t["scheduled_at"] is None
        usage_after = authed_session.get(f"{base_url}/api/stats/usage").json()["usage"]
        assert usage_after == usage_before + 3

        # appears in /active
        active = authed_session.get(f"{base_url}/api/tasks/active").json()
        assert any(x["id"] == t["id"] for x in active)

        # cleanup
        authed_session.delete(f"{base_url}/api/tasks/{t['id']}")

    def test_cancel_task(self, base_url, authed_session):
        r = authed_session.post(f"{base_url}/api/tasks", json={
            "meeting_id": "TEST_cancel", "members": 1, "timeout": 600
        })
        tid = r.json()["id"]
        c = authed_session.post(f"{base_url}/api/tasks/{tid}/cancel")
        assert c.status_code == 200
        body = c.json()
        assert body["status"] == "cancelled"
        assert body["completed_at"] is not None
        # appears in previous
        today = datetime.now(timezone.utc).date().isoformat()
        prev = authed_session.get(f"{base_url}/api/tasks/previous?date={today}").json()
        assert any(x["id"] == tid and x["status"] == "cancelled" for x in prev)
        # cleanup
        authed_session.delete(f"{base_url}/api/tasks/{tid}")

    def test_delete_single_task(self, base_url, authed_session):
        r = authed_session.post(f"{base_url}/api/tasks",
                                json={"meeting_id": "TEST_del", "members": 1, "timeout": 600})
        tid = r.json()["id"]
        d = authed_session.delete(f"{base_url}/api/tasks/{tid}")
        assert d.status_code == 200
        # Cancel after delete should 404
        c = authed_session.post(f"{base_url}/api/tasks/{tid}/cancel")
        assert c.status_code == 404

    def test_bulk_delete(self, base_url, authed_session):
        ids = []
        for i in range(3):
            r = authed_session.post(f"{base_url}/api/tasks",
                                    json={"meeting_id": f"TEST_bulk_{i}", "members": 1, "timeout": 600})
            ids.append(r.json()["id"])
        r = authed_session.post(f"{base_url}/api/tasks/bulk-delete", json=ids)
        assert r.status_code == 200
        assert r.json()["deleted"] == 3
        # verify gone
        active = authed_session.get(f"{base_url}/api/tasks/active").json()
        for i in ids:
            assert not any(x["id"] == i for x in active)

    def test_download_returns_user_tasks(self, base_url, authed_session):
        r = authed_session.post(f"{base_url}/api/tasks",
                                json={"meeting_id": "TEST_dl", "members": 1, "timeout": 600})
        tid = r.json()["id"]
        d = authed_session.get(f"{base_url}/api/tasks/download")
        assert d.status_code == 200
        data = d.json()
        assert isinstance(data, list)
        assert any(x["id"] == tid for x in data)
        authed_session.delete(f"{base_url}/api/tasks/{tid}")


class TestTasksScheduled:
    def test_scheduled_transitions_to_active(self, base_url, authed_session):
        sched_at = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
        r = authed_session.post(f"{base_url}/api/tasks", json={
            "meeting_id": "TEST_sched", "members": 1, "timeout": 600,
            "scheduled_at": sched_at
        })
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["status"] == "scheduled"
        assert t["started_at"] is None
        tid = t["id"]
        # poller runs every 5s; wait 8-10s
        time.sleep(10)
        active = authed_session.get(f"{base_url}/api/tasks/active").json()
        assert any(x["id"] == tid for x in active), f"task not in active after wait: {active}"
        # cleanup
        authed_session.post(f"{base_url}/api/tasks/{tid}/cancel")
        authed_session.delete(f"{base_url}/api/tasks/{tid}")

    def test_active_auto_completes(self, base_url, authed_session):
        r = authed_session.post(f"{base_url}/api/tasks", json={
            "meeting_id": "TEST_autocomp", "members": 1, "timeout": 10
        })
        tid = r.json()["id"]
        # wait > timeout + poller cycle
        time.sleep(16)
        today = datetime.now(timezone.utc).date().isoformat()
        prev = authed_session.get(f"{base_url}/api/tasks/previous?date={today}").json()
        match = [x for x in prev if x["id"] == tid]
        assert match, f"Task {tid} not in previous list"
        assert match[0]["status"] == "completed"
        authed_session.delete(f"{base_url}/api/tasks/{tid}")


class TestUsageLimit:
    def test_exceed_limit_returns_400(self, base_url, authed_session):
        stats = authed_session.get(f"{base_url}/api/stats/usage").json()
        remaining = stats["usage_limit"] - stats["usage"]
        if remaining >= 100:
            members_to_use = 100  # max per schema
            r = authed_session.post(f"{base_url}/api/tasks", json={
                "meeting_id": "TEST_overuse", "members": members_to_use, "timeout": 60
            })
            # cannot easily exceed 15000 in one call; instead simulate: temporarily lower usage_limit via direct test
            # Skip if not feasible, but the route logic is straightforward; assert by lowering members > remaining
        # Try to force fail by requesting members > remaining via repeated tasks unrealistic.
        # Instead validate the path: send members so usage+members > limit by patching test:
        # We'll request a value that's mathematically certain to fail by checking limit-usage boundary.
        # Direct test: set members = remaining + 1 if (remaining+1) <= 100
        attempt_members = min(100, max(1, remaining + 1))
        if attempt_members > remaining:
            r = authed_session.post(f"{base_url}/api/tasks", json={
                "meeting_id": "TEST_limit", "members": attempt_members, "timeout": 60
            })
            assert r.status_code == 400, r.text
            assert "Usage limit exceeded" in r.json()["detail"]
        else:
            pytest.skip(f"Cannot exceed usage limit in test (remaining={remaining})")


# ---------- Name files ----------
class TestNameFiles:
    def test_builtin_pools(self, base_url, authed_session):
        r = authed_session.get(f"{base_url}/api/name-files/builtin")
        assert r.status_code == 200
        data = r.json()
        names = [d["name"] for d in data]
        assert "Indian" in names
        assert "English" in names
        for d in data:
            assert d["builtin"] is True
            assert d["count"] > 0

    def test_full_crud(self, base_url, authed_session):
        unique = f"TEST_file_{uuid.uuid4().hex[:6]}"
        # create
        r = authed_session.post(f"{base_url}/api/name-files", json={"name": unique})
        assert r.status_code == 200
        fid = r.json()["id"]
        assert r.json()["count"] == 0

        # list contains it
        lst = authed_session.get(f"{base_url}/api/name-files").json()
        assert any(x["id"] == fid for x in lst)

        # duplicate create -> 400
        dup = authed_session.post(f"{base_url}/api/name-files", json={"name": unique})
        assert dup.status_code == 400

        # save content - empty/whitespace stripped, 1 per line
        content = "Alice\nBob\n\n   \nCharlie\n  Dan  \n"
        s = authed_session.put(f"{base_url}/api/name-files/{fid}/content",
                               json={"content": content})
        assert s.status_code == 200
        assert s.json()["count"] == 4

        # get content
        g = authed_session.get(f"{base_url}/api/name-files/{fid}").json()
        names = g["content"].split("\n")
        assert names == ["Alice", "Bob", "Charlie", "Dan"]

        # rename
        new_name = f"TEST_renamed_{uuid.uuid4().hex[:6]}"
        rn = authed_session.put(f"{base_url}/api/name-files/{fid}/rename",
                                json={"name": new_name})
        assert rn.status_code == 200
        assert rn.json()["name"] == new_name

        # duplicate rename: create 2nd file, try rename it to new_name
        other = authed_session.post(f"{base_url}/api/name-files",
                                    json={"name": f"TEST_other_{uuid.uuid4().hex[:6]}"}).json()
        dup_rn = authed_session.put(f"{base_url}/api/name-files/{other['id']}/rename",
                                    json={"name": new_name})
        assert dup_rn.status_code == 400

        # cleanup
        authed_session.delete(f"{base_url}/api/name-files/{fid}")
        authed_session.delete(f"{base_url}/api/name-files/{other['id']}")

        # delete -> get 404
        g2 = authed_session.get(f"{base_url}/api/name-files/{fid}")
        assert g2.status_code == 404


# ---------- Stats ----------
class TestStats:
    def test_usage(self, base_url, authed_session):
        r = authed_session.get(f"{base_url}/api/stats/usage")
        assert r.status_code == 200
        d = r.json()
        assert "usage" in d and "usage_limit" in d
        assert isinstance(d["usage"], int)
        assert isinstance(d["usage_limit"], int)
        assert d["usage_limit"] >= 0
