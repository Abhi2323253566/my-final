"""Performance/load test — simulate 30 RDPs polling claim+heartbeat concurrently."""
import asyncio
import os
import time
import requests

API = os.environ.get("API_URL") or "https://meeting-center.preview.emergentagent.com"
ADMIN_EMAIL = "ppandit6926@gmail.com"
ADMIN_PASSWORD = "alok@zoom123"


def login_admin():
    s = requests.Session()
    s.post(f"{API}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=10).raise_for_status()
    return s


def cleanup(s, prefix):
    for w in s.get(f"{API}/api/workers", timeout=10).json():
        if w["name"].startswith(prefix):
            s.delete(f"{API}/api/workers/{w['id']}", timeout=10)
    for t in s.get(f"{API}/api/tasks/active", timeout=10).json():
        s.post(f"{API}/api/tasks/{t['id']}/cancel", timeout=10)


def create_worker(s, name, cap):
    r = s.post(f"{API}/api/workers", json={"name": name, "capacity_max": cap}, timeout=10)
    r.raise_for_status()
    return r.json()


def hb(token, load=0):
    return requests.post(f"{API}/api/workers/me/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_load": load, "cpu_pct": 0, "ram_pct": 0, "hostname": "x", "os_info": "y"}, timeout=10)


def claim(token):
    return requests.post(f"{API}/api/workers/me/claim?max_tasks=5",
        headers={"Authorization": f"Bearer {token}"}, timeout=10)


import concurrent.futures as cf


def main():
    s = login_admin()
    cleanup(s, "load-")

    print("Creating 30 workers...")
    workers = []
    for i in range(30):
        w = create_worker(s, f"load-w{i+1:02d}", 30)
        workers.append(w)
    print(f"Created {len(workers)} workers")

    # Heartbeat all (parallel)
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=30) as ex:
        list(ex.map(lambda w: hb(w["token"]), workers))
    t1 = time.time()
    print(f"30 parallel heartbeats: {t1-t0:.2f}s")

    # No active task — claim should be FAST (early return)
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=30) as ex:
        results = list(ex.map(lambda w: claim(w["token"]), workers))
    t1 = time.time()
    print(f"30 parallel claims (no task): {t1-t0:.2f}s  status_codes={[r.status_code for r in results[:5]]}")

    # Create a task with 300 members
    r = s.post(f"{API}/api/tasks", json={
        "meeting_id": "9876543210", "meeting_password": "x",
        "members": 300, "name_source": "NamesIn", "timeout": 600,
    }, timeout=10)
    r.raise_for_status()
    tid = r.json()["id"]
    print(f"Created task {tid[:8]} with 300 members")

    # All 30 workers claim simultaneously
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=30) as ex:
        results = list(ex.map(lambda w: claim(w["token"]), workers))
    t1 = time.time()
    total_claimed = sum(sum(t["members"] for t in r.json().get("tasks", [])) for r in results)
    print(f"30 parallel claims (with 300 task): {t1-t0:.2f}s  total_claimed={total_claimed}/300")

    # Per-worker distribution
    counts = []
    for r in results:
        members = sum(t["members"] for t in r.json().get("tasks", []))
        counts.append(members)
    print(f"  Distribution: min={min(counts)}, max={max(counts)}, avg={sum(counts)/30:.1f}")

    # cleanup
    s.post(f"{API}/api/tasks/{tid}/cancel", timeout=10)
    cleanup(s, "load-")
    print("Cleanup done.")


if __name__ == "__main__":
    main()
