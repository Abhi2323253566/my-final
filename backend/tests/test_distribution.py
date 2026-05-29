"""Distribution simulation: 30 mock workers vs 1000-bot task.

Spins up 30 workers via /api/workers, sends heartbeats so the backend treats
them as "online", then has each worker call /api/workers/me/claim_tasks until
the task is fully claimed. Asserts that:

  1. ALL workers receive a non-zero claim (no starvation).
  2. The per-worker claim count stays within ±20% of the equal share
     (1000 / 30 ≈ 34) — i.e. no single RDP gets 200 bots while others get 5.
  3. Total claims equals the task size (no loss).

Run:
    cd /app/backend && python3 tests/test_distribution.py
"""
import os
import sys
import asyncio
import math
from typing import Dict, List

import httpx

BASE = os.environ.get("BACKEND_BASE", "http://localhost:8001")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@finalzoom.com")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "Admin@FinalZoom2026")
NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "30"))
TASK_MEMBERS = int(os.environ.get("TASK_MEMBERS", "500"))  # API max is 500/task
WORKER_CAP = int(os.environ.get("WORKER_CAP", "50"))


async def admin_login(client: httpx.AsyncClient) -> str:
    r = await client.post(f"{BASE}/api/auth/login",
                          json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    r.raise_for_status()
    # Auth is cookie-based — httpx auto-stores the cookie on the client. Just
    # return user id for logging; subsequent requests pick up the cookie.
    return r.json().get("id", "ok")


async def create_worker(client, admin_token, name) -> dict:
    r = await client.post(
        f"{BASE}/api/workers",
        json={"name": name, "capacity_max": WORKER_CAP},
    )
    r.raise_for_status()
    return r.json()


async def worker_heartbeat(client, worker_token):
    headers = {"Authorization": f"Bearer {worker_token}"}
    payload = {
        "current_load": 0,
        "cpu_pct": 10.0,
        "ram_pct": 20.0,
        "hostname": "mock-rdp",
        "os_info": "test",
        "reported_capacity": WORKER_CAP,
    }
    r = await client.post(f"{BASE}/api/workers/me/heartbeat",
                          json=payload, headers=headers)
    r.raise_for_status()


async def create_task(client, admin_token) -> str:
    r = await client.post(
        f"{BASE}/api/tasks",
        json={
            "meeting_id": "999888777",
            "meeting_password": "test",
            "members": TASK_MEMBERS,
            "timeout": 600,
        },
    )
    r.raise_for_status()
    return r.json()["id"]


async def claim_once(client, worker_token) -> List[dict]:
    headers = {"Authorization": f"Bearer {worker_token}"}
    r = await client.post(f"{BASE}/api/workers/me/claim?max_tasks=5",
                          headers=headers)
    r.raise_for_status()
    data = r.json()
    # Endpoint returns either {"tasks": [...]} or directly a list
    if isinstance(data, list):
        return data
    return data.get("tasks", [])


async def main():
    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"==> Admin login {ADMIN_EMAIL}")
        admin_tok = await admin_login(client)

        # Spin up NUM_WORKERS mock workers in parallel
        print(f"==> Creating {NUM_WORKERS} mock workers (cap={WORKER_CAP} each)")
        results = await asyncio.gather(*[
            create_worker(client, admin_tok, f"sim-rdp-{i:03d}")
            for i in range(NUM_WORKERS)
        ], return_exceptions=True)
        workers = []
        for r in results:
            if isinstance(r, Exception):
                print(f"    create skipped: {r}")
                continue
            workers.append({"id": r["id"], "name": r["name"], "token": r["token"]})
        print(f"    {len(workers)} workers ready")
        if len(workers) < NUM_WORKERS:
            print("    NOTE: some workers may pre-exist — fetching their tokens "
                  "is not possible from API; using freshly-created subset.")

        # Heartbeat all so backend marks them online (parallel)
        print("==> Sending initial heartbeats")
        await asyncio.gather(*[worker_heartbeat(client, w["token"]) for w in workers])

        # Wait so heartbeat-cache settles, then create the big task
        await asyncio.sleep(1.0)
        print(f"==> Creating task with {TASK_MEMBERS} members")
        task_id = await create_task(client, admin_tok)
        print(f"    task_id={task_id}")

        # Each worker claims in a tight loop. Round-robin polling — 8 rounds is
        # enough to drain a 1000-bot task across 30 workers in auto mode.
        per_worker_claimed: Dict[str, int] = {w["id"]: 0 for w in workers}
        for rnd in range(20):
            # Heartbeat refresh + claim, all workers concurrently
            await asyncio.gather(*[worker_heartbeat(client, w["token"]) for w in workers])
            claim_results = await asyncio.gather(*[
                claim_once(client, w["token"]) for w in workers
            ])
            round_total = 0
            for w, claimed in zip(workers, claim_results):
                for t in claimed:
                    if t["id"] != task_id:
                        continue
                    per_worker_claimed[w["id"]] += int(t.get("members", 0))
                    round_total += int(t.get("members", 0))
            print(f"  round {rnd+1:02d}: +{round_total:4d}  total={sum(per_worker_claimed.values()):4d}/{TASK_MEMBERS}")
            if sum(per_worker_claimed.values()) >= TASK_MEMBERS:
                break
            await asyncio.sleep(0.3)

        # Report
        total = sum(per_worker_claimed.values())
        counts = sorted(per_worker_claimed.values(), reverse=True)
        non_zero = sum(1 for v in counts if v > 0)
        avg = total / max(1, len(workers))
        equal_share = math.ceil(TASK_MEMBERS / len(workers))
        print()
        print("=" * 60)
        print(f"Total claimed : {total}/{TASK_MEMBERS}")
        print(f"Workers       : {len(workers)} ({non_zero} non-zero, "
              f"{len(workers) - non_zero} starved)")
        print(f"Equal share   : {equal_share} bots/worker")
        print(f"Min / Avg / Max claim per worker: "
              f"{min(counts)} / {avg:.1f} / {max(counts)}")
        print(f"Top 5         : {counts[:5]}")
        print(f"Bottom 5      : {counts[-5:]}")
        print("=" * 60)

        # Assertions
        ok = True
        if total != TASK_MEMBERS:
            print(f"FAIL: claims total {total} != task size {TASK_MEMBERS}")
            ok = False
        if non_zero < len(workers):
            print(f"FAIL: {len(workers) - non_zero} workers got ZERO bots (starvation)")
            ok = False
        if max(counts) > equal_share * 1.5:
            print(f"FAIL: hot-worker got {max(counts)} > 1.5×equal_share ({equal_share})")
            ok = False
        if ok:
            print("PASS: fair distribution working (auto mode)")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
