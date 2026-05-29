"""
Zoom Worker (v4) — Battle-tested simple flow.

Adapted from user's proven working code:
- URL: https://app.zoom.us/wc/{meeting_code}/join (no extra params)
- Exact selectors: input-for-pwd, input-for-name, preview-join-button
- JS-clicked join button
- 1920x1080 window (not tiny — Zoom UI needs space)
- Multiprocess spawning (one OS process per bot — truly isolated)
"""

import os
import sys
import gc
import time
import json
import signal
import socket
import shutil
import tempfile
import threading
import traceback
import multiprocessing as mp
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

try:
    from dotenv import load_dotenv
except ImportError:
    print("Please run: pip install -r requirements.txt"); sys.exit(1)

import requests

try:
    import psutil
except ImportError:
    psutil = None

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
except ImportError:
    print("Selenium missing. Run: pip install -r requirements.txt"); sys.exit(1)

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
SPAWN_DELAY_MS = int(os.environ.get("SPAWN_DELAY_MS", "300"))
SPAWN_BATCH = int(os.environ.get("SPAWN_BATCH", "5"))  # parallel processes per batch
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "5"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
CHROME_BIN = os.environ.get("CHROME_BIN", "")
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "")
LOCAL_NAMES_FILE = os.environ.get("LOCAL_NAMES_FILE", "").strip()

if not DASHBOARD_URL or not WORKER_TOKEN:
    print("ERROR: DASHBOARD_URL and WORKER_TOKEN must be set in .env"); sys.exit(1)

API = f"{DASHBOARD_URL}/api"
HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}

# In-memory state
RUNNING: Dict[str, dict] = {}      # task_id -> { processes:[Process], joined_counter (shared), started_at }
RUNNING_LOCK = threading.Lock()
STOP = threading.Event()
_LOCAL_NAMES: List[str] = []


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- Names ----------------
def _load_local_names() -> List[str]:
    global _LOCAL_NAMES
    if _LOCAL_NAMES: return _LOCAL_NAMES
    if not LOCAL_NAMES_FILE: return []
    p = Path(LOCAL_NAMES_FILE)
    if not p.exists():
        log(f"WARN: LOCAL_NAMES_FILE not found: {LOCAL_NAMES_FILE}")
        return []
    try:
        names = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        _LOCAL_NAMES = names
        log(f"Loaded {len(names)} names from {LOCAL_NAMES_FILE}")
        return names
    except Exception as e:
        log(f"local names load failed: {e}"); return []


def _pick_local_names(count: int) -> List[str]:
    import random as _r
    pool = _load_local_names()
    if not pool: return []
    if count <= len(pool): return _r.sample(pool, count)
    out: List[str] = []
    while len(out) < count:
        sh = pool[:]; _r.shuffle(sh)
        out.extend(sh[: count - len(out)])
    return out


# ---------------- Dashboard API ----------------
def heartbeat(load_override: int = 0):
    if psutil:
        cpu = psutil.cpu_percent(interval=None); ram = psutil.virtual_memory().percent
    else:
        cpu, ram = 0.0, 0.0
    payload = {"current_load": load_override, "cpu_pct": float(cpu), "ram_pct": float(ram),
               "hostname": socket.gethostname(),
               "os_info": f"{sys.platform} (Chrome WC v4-mp)"}
    try:
        requests.post(f"{API}/workers/me/heartbeat", headers=HEADERS, json=payload, timeout=10)
    except Exception as e:
        log(f"heartbeat err: {e}")


def claim_tasks(n: int = 5) -> List[dict]:
    try:
        r = requests.post(f"{API}/workers/me/claim", headers=HEADERS,
                          params={"max_tasks": n}, timeout=15)
        if r.status_code != 200: return []
        return r.json().get("tasks", [])
    except Exception:
        return []


def report_progress(task_id: str, joined: int):
    try:
        requests.patch(f"{API}/tasks/{task_id}/progress", headers=HEADERS,
                       json={"joined_count": joined}, timeout=10)
    except Exception:
        pass


def check_chunk_status(task_id: str) -> str:
    """Returns 'active' | 'cancelled' | 'completed' | 'unknown'. Worker polls this
    to detect if dashboard cancelled the task — and tear down early."""
    try:
        r = requests.get(f"{API}/tasks/{task_id}/chunk-status", headers=HEADERS, timeout=8)
        if r.status_code == 200:
            d = r.json()
            return d.get("chunk_status") or d.get("task_status") or "unknown"
    except Exception:
        pass
    return "unknown"


def complete_task(task_id: str, success: bool, joined: int, error: Optional[str] = None):
    try:
        requests.post(f"{API}/tasks/{task_id}/complete", headers=HEADERS,
                      json={"success": success, "joined_count": joined, "error": error},
                      timeout=15)
    except Exception:
        pass


# ---------------- Bot subprocess ----------------
# This runs in its OWN process (mp.Process) — fully isolated Chrome + Selenium
def bot_process(meeting_id: str, password: str, name: str, hold_seconds: int,
                headless: bool, chrome_bin: str, joined_event: 'mp.synchronize.Event',
                task_prefix: str = ""):
    """Single bot — join Zoom meeting and stay until hold_seconds or terminated."""
    import tempfile as _tf
    # Per-task profile prefix so kill_orphans() can avoid killing OTHER tasks' bots
    pfx = f"zb-{task_prefix}-" if task_prefix else "zb-"
    profile_dir = _tf.mkdtemp(prefix=pfx)
    driver = None
    try:
        opts = ChromeOptions()
        if headless:
            opts.add_argument("--headless")
        if chrome_bin:
            opts.binary_location = chrome_bin
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--mute-audio")
        opts.add_argument("--window-size=1280,720")
        opts.add_argument("--use-fake-ui-for-media-stream")
        opts.add_argument("--use-fake-device-for-media-stream")
        opts.add_argument("--autoplay-policy=no-user-gesture-required")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-default-apps")
        opts.add_argument("--no-first-run")
        opts.add_argument("--disable-sync")
        opts.add_argument("--disable-translate")
        opts.add_argument("--log-level=3")
        opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_experimental_option("prefs", {
            "profile.default_content_setting_values.media_stream_mic": 1,
            "profile.default_content_setting_values.media_stream_camera": 1,
            "profile.default_content_setting_values.notifications": 2,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        })

        # Driver resolution priority:
        # 1. Explicit CHROMEDRIVER_PATH env var (most reliable, user-provided)
        # 2. Selenium Manager auto-resolve
        # 3. Basic Service() fallback
        driver_path = os.environ.get("CHROMEDRIVER_PATH", "").strip()
        try:
            if driver_path and os.path.exists(driver_path):
                service = ChromeService(executable_path=driver_path, log_path=os.devnull)
                driver = webdriver.Chrome(service=service, options=opts)
            else:
                driver = webdriver.Chrome(options=opts)
        except Exception:
            service = ChromeService()
            driver = webdriver.Chrome(service=service, options=opts)

        driver.set_page_load_timeout(60)
        driver.get(f"https://app.zoom.us/wc/{meeting_id}/join")
        time.sleep(5)  # critical: let Zoom's JS finish initial render

        wait = WebDriverWait(driver, 20)

        # Password input (only if password provided)
        if password:
            try:
                pwd_el = wait.until(EC.presence_of_element_located(
                    (By.XPATH, "//input[@id='input-for-pwd']")))
                pwd_el.clear(); pwd_el.send_keys(password)
            except TimeoutException:
                # No password field — meeting may not require one
                pass

        # Name input
        name_el = wait.until(EC.presence_of_element_located(
            (By.XPATH, "//input[@id='input-for-name']")))
        name_el.clear(); name_el.send_keys(name)

        # Join button — JS click (more reliable than .click())
        join_btn = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(@class,'preview-join-button')]")))
        driver.execute_script("arguments[0].click();", join_btn)

        # Wait until in meeting room (one of these indicators must appear)
        try:
            WebDriverWait(driver, 35).until(EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".meeting-app, .meeting-client, .footer__leave-btn")),
                EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Leave')]")),
                EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'Please wait') or contains(text(),'Waiting Room')]")),
            ))
        except TimeoutException:
            pass  # may still be in form; declare joined anyway

        # Signal joined to parent process
        joined_event.set()

        # Try to auto-mute mic after 1.5 sec (best effort)
        time.sleep(1.5)
        try:
            mic_btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label*='mute my microphone' i]")
            lbl = (mic_btn.get_attribute("aria-label") or "").lower()
            if "unmute" not in lbl:
                driver.execute_script("arguments[0].click();", mic_btn)
        except Exception:
            pass

        # Try to auto-stop video (best effort) — if "Stop my video" is visible,
        # click it. If only "Start my video" is visible, video is already off.
        try:
            vid_btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label*='stop my video' i]")
            driver.execute_script("arguments[0].click();", vid_btn)
        except Exception:
            pass

        # Hold the bot in the meeting
        end = time.time() + hold_seconds
        while time.time() < end:
            time.sleep(20)
            try:
                _ = driver.title  # liveness probe
            except Exception:
                break

    except Exception as e:
        # Quiet — parent process tracks failures via joined_event timeout
        try: print(f"[bot {name}] error: {type(e).__name__}: {str(e)[:120]}", flush=True)
        except Exception: pass
    finally:
        try:
            if driver: driver.quit()
        except Exception: pass
        shutil.rmtree(profile_dir, ignore_errors=True)


# ---------------- Task runner ----------------
def run_task(task: dict):
    task_id = task["id"]
    meeting_id = task["meeting_id"]
    password = task.get("meeting_password") or ""
    members = int(task.get("members", 0))
    timeout_sec = int(task.get("timeout", 7200))

    # Pick names: LOCAL file overrides; else server-sent
    local = _pick_local_names(members) if LOCAL_NAMES_FILE else []
    if local:
        names = local
        log(f"  using LOCAL names ({len(_LOCAL_NAMES)} in pool)")
    else:
        names = task.get("names") or [f"User{i+1}" for i in range(members)]

    log(f"▶ task {task_id[:8]} | meeting={meeting_id} members={members} timeout={timeout_sec}s batch={SPAWN_BATCH}")

    # Per-task profile prefix so each task's bots are isolated and cleanup
    # only targets THIS task's processes (won't kill other running tasks).
    task_prefix = task_id[:8]

    processes: List[mp.Process] = []
    joined_events: List[mp.synchronize.Event] = []

    with RUNNING_LOCK:
        RUNNING[task_id] = {"processes": processes, "joined": 0, "started_at": time.time(), "prefix": task_prefix}

    # Spawn all bot processes in batches of SPAWN_BATCH with stagger
    for i in range(members):
        if STOP.is_set(): break
        ev = mp.Event()
        joined_events.append(ev)
        p = mp.Process(
            target=bot_process,
            args=(meeting_id, password, names[i], timeout_sec, HEADLESS, CHROME_BIN, ev, task_prefix),
            daemon=True,
        )
        p.start()
        processes.append(p)

        # After every SPAWN_BATCH starts, pause briefly so we don't slam Chrome launch
        if (i + 1) % SPAWN_BATCH == 0:
            time.sleep(SPAWN_DELAY_MS / 1000.0)
        else:
            time.sleep(0.08)  # tiny inter-process gap

    # Watcher loop: report progress + watch for cancel/end of meeting
    last_reported = 0
    deadline = time.time() + timeout_sec + 60
    progress_check_until = time.time() + 90
    cancel_check_interval = 10  # seconds
    last_cancel_check = 0

    while time.time() < deadline and not STOP.is_set():
        joined = sum(1 for ev in joined_events if ev.is_set())
        alive  = sum(1 for p in processes if p.is_alive())

        if joined != last_reported:
            report_progress(task_id, joined)
            with RUNNING_LOCK: RUNNING[task_id]["joined"] = joined
            log(f"  ✓ joined {joined}/{members}  (alive procs: {alive})")
            last_reported = joined

        # Check if dashboard cancelled this task
        if time.time() - last_cancel_check > cancel_check_interval:
            last_cancel_check = time.time()
            status = check_chunk_status(task_id)
            if status in ("cancelled", "failed"):
                log(f"  ⚠ task {task_id[:8]} {status} by dashboard — tearing down")
                break

        if time.time() > progress_check_until and alive == 0:
            break
        if alive == 0:
            break
        time.sleep(3)

    # Cleanup all bot processes
    log(f"  cleaning up task {task_id[:8]}…")
    for p in processes:
        try:
            if p.is_alive(): p.terminate()
        except Exception: pass
    time.sleep(2)
    for p in processes:
        try:
            if p.is_alive(): p.kill()
            p.join(timeout=3)
        except Exception: pass

    # Force-kill orphan chrome.exe + wipe THIS task's profile dirs only.
    # NOTE: Don't blow away other running tasks — pass the task_prefix so we
    # only clean up zb-{this_task_prefix}-* and skip any zb-{other_task_prefix}-*.
    kill_orphans(only_prefix=task_prefix)
    gc.collect()

    final_joined = sum(1 for ev in joined_events if ev.is_set())
    with RUNNING_LOCK:
        RUNNING.pop(task_id, None)
    complete_task(task_id, success=True, joined=final_joined)
    log(f"✓ task {task_id[:8]} complete (joined {final_joined}/{members}). Ready for next.")


def kill_orphans(only_prefix: str = ""):
    """Kill orphan chromedriver/chrome.exe processes + wipe their profile dirs.

    If ``only_prefix`` is supplied, only processes whose user-data-dir matches
    ``zb-{only_prefix}-`` are killed — this lets a single task clean up after
    itself without nuking other running tasks' bots.

    If ``only_prefix`` is empty, the function also protects bots belonging to
    currently RUNNING tasks (by reading their prefixes from the RUNNING dict).
    """
    if not psutil: return
    # Compute "live" prefixes from running tasks so we don't kill them
    live_prefixes: set = set()
    if not only_prefix:
        try:
            with RUNNING_LOCK:
                for tdata in RUNNING.values():
                    pfx = tdata.get("prefix")
                    if pfx:
                        live_prefixes.add(f"zb-{pfx}-")
        except Exception:
            pass

    killed = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            n = (p.info.get("name") or "").lower()
            if n not in {"chrome.exe", "chromedriver.exe", "chrome", "chromedriver"}:
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            if "zb-" not in cmd:
                continue
            if only_prefix:
                # Only kill THIS task's processes
                if f"zb-{only_prefix}-" not in cmd:
                    continue
            else:
                # Skip any live task's processes
                if any(pfx in cmd for pfx in live_prefixes):
                    continue
            p.kill(); killed += 1
        except Exception:
            continue
    if killed:
        log(f"  cleanup: killed {killed} orphan chrome processes"
            f"{f' (prefix=zb-{only_prefix}-*)' if only_prefix else ''}")
    # Wipe leftover profile dirs (matching scope)
    n = 0
    try:
        base = Path(tempfile.gettempdir())
        if only_prefix:
            patterns = [f"zb-{only_prefix}-*"]
        else:
            # All zb-* dirs except ones belonging to live tasks
            patterns = ["zb-*"]
        for pat in patterns:
            for p in base.glob(pat):
                # Skip live task dirs in global sweep
                if not only_prefix and any(pfx.rstrip("-") in p.name for pfx in live_prefixes):
                    continue
                shutil.rmtree(p, ignore_errors=True); n += 1
    except Exception:
        pass
    if n: log(f"  cleanup: wiped {n} orphan profile dirs")


# ---------------- Main loop ----------------
def main_loop():
    log(f"Zoom worker v5 (multiprocess + load-balanced chunks + cancel-aware) starting")
    log(f"  dashboard={DASHBOARD_URL}")
    log(f"  poll={POLL_INTERVAL}s  batch={SPAWN_BATCH}  spawn_delay={SPAWN_DELAY_MS}ms  headless={HEADLESS}")
    if LOCAL_NAMES_FILE:
        _load_local_names()

    # Pre-flight: ensure Chrome can launch
    try:
        log("Pre-flight: testing Chrome launch…")
        pre_opts = ChromeOptions()
        if HEADLESS: pre_opts.add_argument("--headless")
        if CHROME_BIN: pre_opts.binary_location = CHROME_BIN
        pre_opts.add_argument("--no-sandbox")
        pre_opts.add_argument("--disable-dev-shm-usage")
        pre_opts.add_argument("--disable-gpu")
        pre_opts.add_argument(f"--user-data-dir={tempfile.mkdtemp(prefix='zb-pre-')}")
        # Try CHROMEDRIVER_PATH first
        if CHROMEDRIVER_PATH and os.path.exists(CHROMEDRIVER_PATH):
            log(f"  using CHROMEDRIVER_PATH={CHROMEDRIVER_PATH}")
            pre_service = ChromeService(executable_path=CHROMEDRIVER_PATH, log_path=os.devnull)
            d = webdriver.Chrome(service=pre_service, options=pre_opts)
        else:
            d = webdriver.Chrome(options=pre_opts)
        d.quit()
        log("Pre-flight OK — Chrome + chromedriver ready")
    except Exception as e:
        log(f"FATAL: Chrome launch failed: {type(e).__name__}: {e}")
        log("Diagnostic steps:")
        log("  1. Verify Chrome is installed: C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
        log("  2. Upgrade selenium: pip install --upgrade selenium")
        log("  3. Clear Selenium cache: rmdir /S /Q %LOCALAPPDATA%\\.cache\\selenium")
        log("  4. Manually download chromedriver matching your Chrome version from:")
        log("     https://googlechromelabs.github.io/chrome-for-testing/")
        log("     Then set CHROMEDRIVER_PATH=C:\\path\\to\\chromedriver.exe in .env")
        sys.exit(1)

    # Startup cleanup
    kill_orphans()

    last_idle = time.time()
    while not STOP.is_set():
        # Compute load = sum of alive bots across all running tasks
        with RUNNING_LOCK:
            load = sum(sum(1 for p in t["processes"] if p.is_alive())
                       for t in RUNNING.values())
        heartbeat(load_override=load)

        if len(RUNNING) < MAX_CONCURRENT_TASKS:
            tasks = claim_tasks(n=min(5, MAX_CONCURRENT_TASKS - len(RUNNING)))
            for t in tasks:
                threading.Thread(target=run_task, args=(t,), daemon=True).start()

        if not RUNNING and (time.time() - last_idle) > 300:
            kill_orphans(); gc.collect(); last_idle = time.time()

        STOP.wait(POLL_INTERVAL)

    log("stopping…")
    with RUNNING_LOCK:
        for tid, data in list(RUNNING.items()):
            for p in data.get("processes", []):
                try:
                    if p.is_alive(): p.terminate()
                except Exception: pass
            complete_task(tid, success=False, joined=data.get("joined", 0),
                          error="Worker shutdown")
    kill_orphans()


def _sig(_a, _b): STOP.set()


if __name__ == "__main__":
    # Windows multiprocessing safety
    mp.freeze_support()
    signal.signal(signal.SIGINT, _sig)
    if hasattr(signal, "SIGTERM"): signal.signal(signal.SIGTERM, _sig)
    try:
        main_loop()
    except KeyboardInterrupt:
        STOP.set()
    except Exception:
        traceback.print_exc(); sys.exit(1)
