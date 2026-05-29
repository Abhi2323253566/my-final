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
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
SPAWN_DELAY_MS = int(os.environ.get("SPAWN_DELAY_MS", "250"))
SPAWN_BATCH = int(os.environ.get("SPAWN_BATCH", "0"))  # 0 = auto-tune from RAM/CPU
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "5"))
# Default headless OFF: Zoom's anti-bot detection kicks headless Chrome more
# aggressively. On RDP boxes there's always a desktop session, so windowed is fine.
HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"
CHROME_BIN = os.environ.get("CHROME_BIN", "")
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "")
LOCAL_NAMES_FILE = os.environ.get("LOCAL_NAMES_FILE", "").strip()
# Auto-rejoin: if a bot is kicked out of the meeting (detected as gone within
# the first KICK_DETECT_WINDOW seconds), retry up to BOT_REJOIN_MAX times.
BOT_REJOIN_MAX = int(os.environ.get("BOT_REJOIN_MAX", "2"))
KICK_DETECT_WINDOW = int(os.environ.get("KICK_DETECT_WINDOW", "180"))
# Auto-capacity: when AUTO_CAPACITY=true, the worker reports a live safe-capacity
# value to the dashboard every heartbeat (computed from THIS machine's free RAM
# + CPU). The dashboard uses this instead of the static `capacity_max` set when
# the worker was created — so a 64 GB / 8 CPU RDP automatically gets ~200 bots
# without manual config.
AUTO_CAPACITY = os.environ.get("AUTO_CAPACITY", "true").lower() == "true"
# RAM tuning knobs (per-Chrome estimate + headroom). With v7-lean flags
# (cap V8 heap, no images, no GPU, no media cache, throttled renderer) a
# single Chrome bot is ~200 MB RAM and ~0.18 CPU. Defaults below reflect that.
RAM_PER_BOT_MB = int(os.environ.get("RAM_PER_BOT_MB", "200"))     # estimated MB per lean Chrome bot
RAM_HEADROOM_PCT = float(os.environ.get("RAM_HEADROOM_PCT", "20"))  # leave this % for OS + Zoom
# Per-CPU upper bound. With renderer throttling + visibility=hidden,
# 5 bots/core is safe on modern Xeon/Ryzen RDPs.
BOTS_PER_CPU = float(os.environ.get("BOTS_PER_CPU", "5.0"))
# Hard absolute cap so we never report > this many even on a 512 GB monster
MAX_CAPACITY_HARD_CAP = int(os.environ.get("MAX_CAPACITY_HARD_CAP", "500"))
# Pre-spawn safety: if free RAM drops below this %, pause spawning more bots
# in the current task until RAM recovers (or move on).
PRE_SPAWN_FREE_RAM_PCT = float(os.environ.get("PRE_SPAWN_FREE_RAM_PCT", "12"))


def _machine_specs() -> dict:
    """Return current machine specs (cpu_count, total_ram_gb, free_ram_gb, ram_pct)."""
    try:
        import psutil as _ps
        vm = _ps.virtual_memory()
        return {
            "cpu_count": _ps.cpu_count(logical=True) or 2,
            "total_ram_gb": vm.total / (1024 ** 3),
            "free_ram_gb": vm.available / (1024 ** 3),
            "ram_pct": float(vm.percent),
            "cpu_pct": float(_ps.cpu_percent(interval=None)),
        }
    except Exception:
        return {"cpu_count": 2, "total_ram_gb": 4.0, "free_ram_gb": 2.0, "ram_pct": 50.0, "cpu_pct": 0.0}


def _compute_safe_capacity(specs: Optional[dict] = None) -> int:
    """How many bots can THIS box safely run RIGHT NOW?

    Calculation:
      - by_ram = (total_RAM - headroom) / RAM_per_bot
      - by_cpu = cpu_count * BOTS_PER_CPU
      - capacity = min(by_ram, by_cpu, MAX_CAPACITY_HARD_CAP)

    Examples (v7-lean defaults: 200 MB/bot, 20% headroom, 5 bots/CPU, hard cap 500):
      - 8 CPU / 64 GB  →  ram: (64*0.8*1024)/200 = 262 ; cpu: 8*5 = 40   →  min(262,40,500) = 40
      - 4 CPU / 32 GB  →  ram: 131 ; cpu: 20 → 20
      - 4 CPU / 16 GB  →  ram:  65 ; cpu: 20 → 20
      - 2 CPU / 8 GB   →  ram:  32 ; cpu: 10 → 10
      - 2 CPU / 4 GB   →  ram:  16 ; cpu: 10 → 10

    NOTE: v7 worker uses heavy Chrome resource trimming (no images, no GPU, V8
    capped to 256 MB, renderer throttled via visibilityState=hidden after join),
    so per-bot footprint drops to ~200 MB / 0.18 CPU. If your RDPs are stable
    above the auto value, raise `BOTS_PER_CPU=8` (or higher) in the worker .env.
    """
    s = specs or _machine_specs()
    by_ram = int((s["total_ram_gb"] * 1024 * (100 - RAM_HEADROOM_PCT) / 100) / RAM_PER_BOT_MB)
    by_cpu = int(s["cpu_count"] * BOTS_PER_CPU)
    cap = min(by_ram, by_cpu, MAX_CAPACITY_HARD_CAP)
    return max(1, cap)


def _free_ram_pct() -> float:
    try:
        import psutil as _ps
        return 100.0 - _ps.virtual_memory().percent
    except Exception:
        return 50.0


def _auto_spawn_batch() -> int:
    """Auto-tune parallel Chrome spawns based on this machine's RAM + CPU.
    - 8 CPU / 64 GB → ~10 parallel (lots of headroom for 30-50 bots)
    - 4 CPU / 16 GB → ~6 parallel
    - 2 CPU / 4 GB  → ~3 parallel
    User can still override via SPAWN_BATCH env var."""
    if SPAWN_BATCH > 0:
        return SPAWN_BATCH
    try:
        import psutil as _ps
        cpus = _ps.cpu_count(logical=True) or 2
        gb = _ps.virtual_memory().total / (1024 ** 3)
    except Exception:
        cpus, gb = 2, 4
    # Each Chrome instance ≈ 350 MB RAM + 0.3 CPU. Be conservative — leave 30% RAM headroom.
    by_ram = int((gb * 0.7) / 0.35)        # how many bots fit in RAM at start
    by_cpu = max(2, int(cpus * 1.5))       # spawn parallelism by CPU
    batch = min(by_ram, by_cpu, 12)        # don't go above 12 parallel spawns at once
    return max(3, batch)

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
    s = _machine_specs()
    payload = {
        "current_load": load_override,
        "cpu_pct": s["cpu_pct"],
        "ram_pct": s["ram_pct"],
        "hostname": socket.gethostname(),
        "os_info": f"{sys.platform} (Chrome WC v7-lean)",
        "cpu_count": s["cpu_count"],
        "ram_free_gb": round(s["free_ram_gb"], 2),
    }
    if AUTO_CAPACITY:
        # Live safe capacity computed from THIS machine right now. Dashboard uses
        # this to balance bots across the fleet — bigger boxes get proportionally
        # more bots, regardless of what was manually set when the worker was created.
        payload["reported_capacity"] = _compute_safe_capacity(s)
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


def _is_still_in_meeting(driver) -> bool:
    """Quick check: are we still inside the Zoom meeting room?
    Returns False if we've been kicked, the page navigated away, or we're
    back on the join/preview screen."""
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        return False
    if "/wc/" not in url and "zoom.us" not in url:
        return False
    # Strong positive signals — any of these means we're in the meeting
    try:
        in_meeting_selectors = [
            "button[aria-label*='leave' i]",
            "button[aria-label*='mute my microphone' i]",
            "button[aria-label*='unmute my microphone' i]",
            ".footer__leave-btn",
            ".meeting-app",
            ".meeting-client",
        ]
        for sel in in_meeting_selectors:
            try:
                if driver.find_elements(By.CSS_SELECTOR, sel):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    # If preview/join screen is back, we've been kicked out
    try:
        if driver.find_elements(By.XPATH, "//button[contains(@class,'preview-join-button')]"):
            return False
        if driver.find_elements(By.XPATH, "//*[contains(text(),'removed') or contains(text(),'has ended') or contains(text(),'Sign In')]"):
            return False
    except Exception:
        pass
    # Default: assume still in (avoid false-positive kicks)
    return True


def _join_once(driver, meeting_id: str, password: str, name: str) -> bool:
    """Single join attempt. Returns True if successfully entered the meeting."""
    driver.get(f"https://app.zoom.us/wc/{meeting_id}/join")
    time.sleep(5)
    wait = WebDriverWait(driver, 20)
    if password:
        try:
            pwd_el = wait.until(EC.presence_of_element_located(
                (By.XPATH, "//input[@id='input-for-pwd']")))
            pwd_el.clear(); pwd_el.send_keys(password)
        except TimeoutException:
            pass
    name_el = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//input[@id='input-for-name']")))
    name_el.clear(); name_el.send_keys(name)
    join_btn = wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//button[contains(@class,'preview-join-button')]")))
    driver.execute_script("arguments[0].click();", join_btn)
    try:
        WebDriverWait(driver, 35).until(EC.any_of(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".meeting-app, .meeting-client, .footer__leave-btn")),
            EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Leave')]")),
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'Please wait') or contains(text(),'Waiting Room')]")),
        ))
    except TimeoutException:
        pass
    # Sanity-check we're actually in the room (not still on join form)
    time.sleep(2)
    return _is_still_in_meeting(driver)


# ---------------- Bot subprocess ----------------
# This runs in its OWN process (mp.Process) — fully isolated Chrome + Selenium
def bot_process(meeting_id: str, password: str, name: str, hold_seconds: int,
                headless: bool, chrome_bin: str, joined_event: 'mp.synchronize.Event',
                task_prefix: str = "", rejoin_max: int = 2,
                kick_detect_window: int = 180):
    """Single bot — join Zoom meeting and stay until hold_seconds or terminated.
    Auto-rejoins up to ``rejoin_max`` times if kicked within first ``kick_detect_window`` sec."""
    import tempfile as _tf
    # Per-task profile prefix so kill_orphans() can avoid killing OTHER tasks' bots
    pfx = f"zb-{task_prefix}-" if task_prefix else "zb-"
    profile_dir = _tf.mkdtemp(prefix=pfx)
    driver = None
    try:
        opts = ChromeOptions()
        if headless:
            opts.add_argument("--headless=new")
        if chrome_bin:
            opts.binary_location = chrome_bin
        opts.add_argument(f"--user-data-dir={profile_dir}")

        # ====== CORE FLAGS ======
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")     # use /tmp instead of shared mem
        opts.add_argument("--mute-audio")
        # Smaller window = less GPU/render memory per bot. Zoom WC still works.
        opts.add_argument("--window-size=800,600")
        opts.add_argument("--use-fake-ui-for-media-stream")
        opts.add_argument("--use-fake-device-for-media-stream")
        opts.add_argument("--autoplay-policy=no-user-gesture-required")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--log-level=3")

        # ====== RAM / CPU MINIMIZATION ======
        # Cap V8 JavaScript heap to 256 MB per Chrome (default is 4 GB!)
        opts.add_argument("--js-flags=--max-old-space-size=256 --max-semi-space-size=8")
        # Disable GPU acceleration entirely (saves big chunk of GPU/RAM)
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--disable-accelerated-2d-canvas")
        opts.add_argument("--disable-accelerated-jpeg-decoding")
        opts.add_argument("--disable-accelerated-mjpeg-decode")
        opts.add_argument("--disable-accelerated-video-decode")
        # Disable WebGL & 3D (Zoom doesn't need them for joining)
        opts.add_argument("--disable-webgl")
        opts.add_argument("--disable-3d-apis")
        # Disable image rendering — biggest single RAM savings (set via prefs below too)
        opts.add_argument("--blink-settings=imagesEnabled=false")
        # Render at 0.75x scale — less pixel work
        opts.add_argument("--force-device-scale-factor=0.75")
        # Background tab throttling stays ON to save CPU when offscreen
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-background-mode")
        opts.add_argument("--disable-component-update")
        opts.add_argument("--disable-domain-reliability")
        opts.add_argument("--disable-client-side-phishing-detection")
        opts.add_argument("--disable-hang-monitor")
        opts.add_argument("--disable-prompt-on-repost")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--disable-print-preview")
        opts.add_argument("--disable-breakpad")           # disable crash reporter
        opts.add_argument("--disable-crash-reporter")
        opts.add_argument("--disable-logging")
        opts.add_argument("--disable-permissions-api")
        opts.add_argument("--disable-features=Translate,BackForwardCache,OptimizationHints,"
                          "MediaRouter,DialMediaRouteProvider,CalculateNativeWinOcclusion,"
                          "InterestFeedContentSuggestions,GlobalMediaControls,ImprovedCookieControls,"
                          "AutomationControlled,IsolateOrigins,site-per-process")
        opts.add_argument("--metrics-recording-only")
        opts.add_argument("--password-store=basic")
        opts.add_argument("--use-mock-keychain")
        opts.add_argument("--ash-no-nudges")
        opts.add_argument("--deny-permission-prompts")
        # Limit Chrome's own renderer process count (default is unbounded)
        opts.add_argument("--renderer-process-limit=2")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-default-apps")
        opts.add_argument("--disable-sync")
        opts.add_argument("--disable-translate")
        opts.add_argument("--disable-application-cache")
        opts.add_argument("--media-cache-size=1")         # 1 byte = effectively disable media cache
        opts.add_argument("--disk-cache-size=1")          # disable disk cache
        opts.add_argument("--aggressive-cache-discard")

        # ====== STEALTH (reduce Zoom anti-bot kicks) ======
        # Real user-agent so Zoom doesn't see "HeadlessChrome" in the UA string
        opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36")
        opts.add_argument("--lang=en-US,en")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_experimental_option("prefs", {
            "profile.default_content_setting_values.media_stream_mic": 1,
            "profile.default_content_setting_values.media_stream_camera": 1,
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_setting_values.images": 2,         # block all images
            "profile.default_content_setting_values.geolocation": 2,
            "profile.default_content_setting_values.plugins": 2,
            "profile.default_content_setting_values.popups": 2,
            "profile.managed_default_content_settings.images": 2,        # block images (managed-level)
            "profile.password_manager_enabled": False,
            "credentials_enable_service": False,
            "translate_site_blacklist": ["zoom.us"],
            "translate_whitelists": {},
            "translate.enabled": False,
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

        # CDP-level stealth: hide navigator.webdriver = true and tweak plugins
        # so Zoom's bot-detection JS sees a "normal" browser fingerprint.
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    "Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});"
                    "Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});"
                )
            })
        except Exception:
            pass

        # First join attempt (+ optional rejoins)
        joined_ok = False
        for attempt in range(rejoin_max + 1):
            try:
                if _join_once(driver, meeting_id, password, name):
                    joined_ok = True
                    break
            except Exception as je:
                try: print(f"[bot {name}] join attempt {attempt+1} error: {type(je).__name__}: {str(je)[:120]}", flush=True)
                except Exception: pass
            time.sleep(3)

        if not joined_ok:
            return  # all attempts failed — exit, parent will see no joined_event

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

        # ==== HEAVY RAM/CPU SAVINGS ====
        # 1) Stop receiving INCOMING video — saves big chunk of CPU + RAM since
        #    we don't need to render other participants' video streams for a bot.
        #    Zoom WC menu: Settings → "Stop incoming video"  OR  right-click thumbnail.
        try:
            # Approach 1: keyboard shortcut Alt+V toggles video for self; not for incoming.
            # Approach 2: directly hide all <video> elements via CSS so the GPU/canvas
            # work for remote videos stops. Saves the most RAM/CPU per bot.
            driver.execute_script(
                "const s=document.createElement('style');"
                "s.textContent='video,canvas,.video-tile,.video-container,.gallery-video-container__main-view,"
                ".speaker-active-container,.shared-container{display:none !important;visibility:hidden !important;}';"
                "document.head.appendChild(s);"
            )
        except Exception:
            pass

        # 2) Drop the framerate by pausing/hiding any remaining <video> elements
        #    periodically (Zoom may re-create them on speaker switch).
        try:
            driver.execute_script(
                "(function(){function kill(){document.querySelectorAll('video').forEach(v=>{try{v.pause();v.srcObject=null;v.style.display='none';}catch(e){}});}"
                "kill();setInterval(kill,4000);})();"
            )
        except Exception:
            pass

        # 3) Tab visibility = hidden makes Chrome throttle the renderer ~10x.
        #    The bot is still IN the meeting; Zoom just stops feeding it visible
        #    UI updates. This is the single biggest CPU saver for held bots.
        try:
            driver.execute_script(
                "Object.defineProperty(document, 'visibilityState', {get:() => 'hidden'});"
                "Object.defineProperty(document, 'hidden', {get:() => true});"
                "document.dispatchEvent(new Event('visibilitychange'));"
            )
        except Exception:
            pass

        # Hold the bot in the meeting — actively check we're still in;
        # if kicked within the first kick_detect_window seconds, try to rejoin.
        joined_at = time.time()
        end = joined_at + hold_seconds
        rejoin_attempts = 0
        while time.time() < end:
            time.sleep(15)
            try:
                _ = driver.title  # liveness probe
            except Exception:
                break
            # Periodically verify we're still inside the meeting room
            in_room = _is_still_in_meeting(driver)
            if not in_room:
                # Within kick-detect window AND we still have rejoin budget?
                if (time.time() - joined_at) <= kick_detect_window and rejoin_attempts < rejoin_max:
                    rejoin_attempts += 1
                    try: print(f"[bot {name}] kicked — rejoin {rejoin_attempts}/{rejoin_max}", flush=True)
                    except Exception: pass
                    try:
                        if _join_once(driver, meeting_id, password, name):
                            joined_at = time.time()
                            continue
                    except Exception:
                        pass
                # Out of rejoin budget OR past detection window → end naturally
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

    log(f"▶ task {task_id[:8]} | meeting={meeting_id} members={members} timeout={timeout_sec}s batch={_auto_spawn_batch()} rejoin={BOT_REJOIN_MAX}")

    # Per-task profile prefix so each task's bots are isolated and cleanup
    # only targets THIS task's processes (won't kill other running tasks).
    task_prefix = task_id[:8]

    processes: List[mp.Process] = []
    joined_events: List[mp.synchronize.Event] = []

    with RUNNING_LOCK:
        RUNNING[task_id] = {"processes": processes, "joined": 0, "started_at": time.time(), "prefix": task_prefix}

    # Spawn all bot processes in batches of effective_batch with stagger
    effective_batch = _auto_spawn_batch()
    spawned = 0
    for i in range(members):
        if STOP.is_set(): break

        # Pre-spawn RAM safety gate: if free RAM dropped below threshold,
        # wait up to 60 s for it to recover. If still low, stop spawning
        # this task's remaining bots — they'd just OOM-thrash. The members
        # we couldn't spawn will be re-claimed by other workers via the
        # 8-second server-side mop-up.
        free_pct = _free_ram_pct()
        if free_pct < PRE_SPAWN_FREE_RAM_PCT:
            log(f"  ⚠ free RAM={free_pct:.0f}% < {PRE_SPAWN_FREE_RAM_PCT}% — pausing spawn (waited at bot {i}/{members})")
            wait_started = time.time()
            while time.time() - wait_started < 60:
                if STOP.is_set(): break
                time.sleep(3)
                free_pct = _free_ram_pct()
                if free_pct >= PRE_SPAWN_FREE_RAM_PCT:
                    log(f"  ✓ RAM recovered to {free_pct:.0f}% — resuming")
                    break
            else:
                log(f"  ✗ RAM still low ({free_pct:.0f}%) — stopping spawn for this task at {i}/{members}")
                break

        ev = mp.Event()
        joined_events.append(ev)
        p = mp.Process(
            target=bot_process,
            args=(meeting_id, password, names[i], timeout_sec, HEADLESS, CHROME_BIN, ev, task_prefix,
                  BOT_REJOIN_MAX, KICK_DETECT_WINDOW),
            daemon=True,
        )
        p.start()
        processes.append(p)
        spawned += 1

        # After every effective_batch starts, pause briefly so we don't slam Chrome launch
        if (i + 1) % effective_batch == 0:
            time.sleep(SPAWN_DELAY_MS / 1000.0)
        else:
            time.sleep(0.08)  # tiny inter-process gap

    if spawned < members:
        log(f"  ! spawned {spawned}/{members} bots only — others will be picked up by fleet mop-up")

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
    log(f"Zoom worker v7-lean (greedy fill + ultra-low-RAM Chrome + auto-capacity) starting")
    log(f"  dashboard={DASHBOARD_URL}")
    log(f"  poll={POLL_INTERVAL}s  batch={_auto_spawn_batch()} (auto)  spawn_delay={SPAWN_DELAY_MS}ms  headless={HEADLESS}  rejoin_max={BOT_REJOIN_MAX}")
    s = _machine_specs()
    log(f"  machine: {s['cpu_count']}c CPU / {s['total_ram_gb']:.1f}G RAM → safe_capacity={_compute_safe_capacity(s)}")
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
