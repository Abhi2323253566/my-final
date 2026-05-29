"""
Zoom Worker v8 — Ultra-Optimized Playwright Browser Pool Worker
================================================================

Key optimizations vs v7-lean (Selenium multiprocess):
  1. BROWSER POOLING  — 1 chromium = many contexts (15-25 tabs per browser).
                         RAM drops ~70% vs 1-process-per-bot.
  2. PLAYWRIGHT       — faster, leaner, native async, better selectors.
  3. XVFB (Linux)     — virtual display so chromium runs without GUI overhead.
  4. AUTO CLEANUP     — page.close + context.close on meeting end + periodic
                         `pkill -f chromium` sweep for orphans.
  5. SEQUENTIAL FILL  — relies on backend's DISTRIBUTION_MODE=greedy.
  6. HEALTH MONITOR   — separate thread monitors cpu/ram and DYNAMICALLY
                         lowers reported_capacity to backend (`if cpu>75: max-=5`).
  7. AUTO RESTART     — if any browser dies, the pool spawns a replacement.
  8. ZERO IDLE        — pool grows on demand, shrinks when tasks complete.
"""
from __future__ import annotations

import os
import sys
import gc
import time
import json
import socket
import signal
import asyncio
import logging
import platform
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:
    print("Run: pip install -r requirements.txt"); sys.exit(1)

import requests

try:
    import psutil
except ImportError:
    psutil = None

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
except ImportError:
    print("Playwright missing. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------- env
ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
SPAWN_DELAY_MS = int(os.environ.get("SPAWN_DELAY_MS", "250"))
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "5"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"   # v8.3.4: default headless
LOCAL_NAMES_FILE = os.environ.get("LOCAL_NAMES_FILE", "").strip()

# v8.3.4: ===== JOIN with AUDIO/VIDEO OFF =====
# When true, the bot toggles "mic off" + "video off" on the Zoom preview screen
# BEFORE clicking Join, so it enters the meeting silent + cameraless.
JOIN_WITH_AUDIO_MUTED = os.environ.get("JOIN_WITH_AUDIO_MUTED", "true").lower() == "true"
JOIN_WITH_VIDEO_OFF   = os.environ.get("JOIN_WITH_VIDEO_OFF", "true").lower() == "true"
# Push the (possibly visible) chromium window off-screen so even in non-headless
# mode the RDP user never sees floating browsers. Works on both Win + Linux.
OFFSCREEN_WINDOW = os.environ.get("OFFSCREEN_WINDOW", "true").lower() == "true"

# Browser pooling: 1 chromium = TABS_PER_BROWSER contexts.
# 20 is the sweet spot (RAM efficient + no shared-process crash).
TABS_PER_BROWSER = int(os.environ.get("TABS_PER_BROWSER", "20"))
BOT_REJOIN_MAX = int(os.environ.get("BOT_REJOIN_MAX", "2"))
KICK_DETECT_WINDOW = int(os.environ.get("KICK_DETECT_WINDOW", "180"))

# Auto health-thresholds. When CPU/RAM exceed these we throttle.
CPU_THROTTLE_PCT = float(os.environ.get("CPU_THROTTLE_PCT", "75"))
RAM_THROTTLE_PCT = float(os.environ.get("RAM_THROTTLE_PCT", "85"))
DYNAMIC_LIMIT_STEP = int(os.environ.get("DYNAMIC_LIMIT_STEP", "5"))

# Capacity computation (lean Playwright = ~120 MB per tab w/ shared browser).
AUTO_CAPACITY = os.environ.get("AUTO_CAPACITY", "true").lower() == "true"
RAM_PER_BOT_MB = int(os.environ.get("RAM_PER_BOT_MB", "120"))
RAM_HEADROOM_PCT = float(os.environ.get("RAM_HEADROOM_PCT", "20"))
BOTS_PER_CPU = float(os.environ.get("BOTS_PER_CPU", "8.0"))
MAX_CAPACITY_HARD_CAP = int(os.environ.get("MAX_CAPACITY_HARD_CAP", "500"))
PRE_SPAWN_FREE_RAM_PCT = float(os.environ.get("PRE_SPAWN_FREE_RAM_PCT", "12"))

# Periodic cleanup sweep (kills orphan chromium not tracked by pool).
CLEANUP_INTERVAL_SEC = int(os.environ.get("CLEANUP_INTERVAL_SEC", "300"))

# ===== PREWARM / HOT POOL =====
# Architecture: PREWARM EVERYTHING that gets repeatedly created.
# A prewarmed browser+context+tab = instant 1-3s joins (vs 10-15s cold start).
PREWARM_BROWSERS = int(os.environ.get("PREWARM_BROWSERS", "2"))           # hot browsers at boot
PREWARM_CONTEXTS = int(os.environ.get("PREWARM_CONTEXTS", "10"))          # ready contexts standing by
PREWARM_MIN_READY = int(os.environ.get("PREWARM_MIN_READY", "5"))         # auto-warmup floor
PREWARM_MAX_READY = int(os.environ.get("PREWARM_MAX_READY", "20"))        # auto-shrink ceiling
# v8.3: default to the actual JOIN form page (not the homepage) so #meeting-id input
# is already mounted before the task even arrives. Saves ~1.5-2s per join.
PREWARM_PRELOAD_URL = os.environ.get("PREWARM_PRELOAD_URL", "https://app.zoom.us/wc/join").strip()
PREWARM_ENABLED = os.environ.get("PREWARM_ENABLED", "true").lower() == "true"
WARMUP_INTERVAL_SEC = int(os.environ.get("WARMUP_INTERVAL_SEC", "15"))    # how often we top up
SHRINK_IDLE_SEC = int(os.environ.get("SHRINK_IDLE_SEC", "120"))           # close idle hot browser after

# v8.3: ===== PERSISTENT PROFILE — disk cache + baked storage_state =====
# Persistent disk cache makes Chromium re-use Zoom SDK js/css across browser
# restarts AND across all contexts in the pool. Single shared dir is safe because
# Chromium serialises writes per-profile.
PERSISTENT_CACHE = os.environ.get("PERSISTENT_CACHE", "true").lower() == "true"
PERSISTENT_CACHE_DIR = os.environ.get("PERSISTENT_CACHE_DIR", "/tmp/zoom-disk-cache")
PERSISTENT_CACHE_SIZE_MB = int(os.environ.get("PERSISTENT_CACHE_SIZE_MB", "256"))
# Storage state = cookies + localStorage snapshot taken once during bootstrap.
# Loaded into EVERY new BrowserContext so cookie banner is pre-dismissed,
# "Join Audio" popup is suppressed, and Zoom locale + consent flags are set.
STORAGE_STATE_PATH = os.environ.get("STORAGE_STATE_PATH", "/tmp/zoom-storage-state.json")
STORAGE_STATE_REFRESH_HOURS = int(os.environ.get("STORAGE_STATE_REFRESH_HOURS", "24"))
FORM_PREWARM_WAIT_MS = int(os.environ.get("FORM_PREWARM_WAIT_MS", "1500"))  # max wait for #meeting-id mount

# v8.3.3: ===== match prewarm pool size to admin's capacity_max =====
# When ON, the worker grows its READY context pool to equal whatever
# capacity_max admin sets in dashboard. e.g. admin sets 50 -> 50 prewarmed
# tabs sit waiting; admin lowers to 20 -> we shrink to 20.
# Hard upper safety limit prevents OOM on giant caps.
PREWARM_MATCH_ADMIN_CAP = os.environ.get("PREWARM_MATCH_ADMIN_CAP", "true").lower() == "true"
PREWARM_HARD_CEILING = int(os.environ.get("PREWARM_HARD_CEILING", "120"))  # never more than this many ready contexts
# v8.3.1: when a join fails, dump page HTML + screenshot + URL to /tmp so we can
# tune selectors against the EXACT Zoom WebClient build the user is hitting.
DEBUG_DUMP_DOM = os.environ.get("DEBUG_DUMP_DOM", "true").lower() == "true"
DEBUG_DUMP_DIR = os.environ.get("DEBUG_DUMP_DIR", "/tmp/zoom-debug")

# ============================================================================
# KNOWN ZOOM SELECTORS — tried in order, first hit wins. Add new variants here
# whenever Zoom ships a WebClient update. NEVER remove the old ones; older
# Zoom builds in private clouds (zoomgov, china) may still use them.
# ============================================================================
ZOOM_SELECTORS = {
    "name_input": [
        "#input-for-name",         # standard wc/{id}/join
        "#inputname",              # legacy
        "input[name='inputname']",
        "input[aria-label*='name' i]",
        "input[placeholder*='Your Name' i]",
        "input[placeholder*='name' i][type='text']",
    ],
    "password_input": [
        "#input-for-pwd",
        "#inputpasscode",
        "input[name='inputpasscode']",
        "input[type='password']",
        "input[aria-label*='passcode' i]",
        "input[aria-label*='password' i]",
        "input[placeholder*='password' i]",
        "input[placeholder*='passcode' i]",
    ],
    "join_button": [
        "button.preview-join-button",
        "button#joinBtn",
        "button[type='submit']:has-text('Join')",
        "button:has-text('Join'):not(:has-text('Audio'))",
        "button[aria-label='Join']",
        "button.zm-btn--primary:has-text('Join')",
    ],
    "in_meeting": [
        ".meeting-app",
        ".meeting-client",
        ".footer__leave-btn",
        "button[aria-label*='leave' i]",
        "button[aria-label*='mute my microphone' i]",
        "button[aria-label*='unmute my microphone' i]",
        "[class*='meeting-info']",
    ],
    "audio_join": [
        "button.join-audio-by-voip__join-btn",
        "button:has-text('Join Audio by Computer')",
        "button:has-text('Computer Audio')",
        "button[aria-label*='audio' i][aria-label*='computer' i]",
    ],
    # v8.3.4: PREVIEW SCREEN toggles (before clicking Join) — these are different
    # from the in-meeting mute/camera buttons. Zoom shows them on /wc/{id}/join
    # next to the name input. Multiple Zoom builds use different DOM.
    "preview_mute_audio": [
        "#preview-audio-control-button",
        "button#preview-audio-button",
        "button[aria-label='Mute']",
        "button[aria-label*='join with audio off' i]",
        "button[aria-label*='audio off' i]",
        "button[title*='Mute' i]",
        # checkbox style on some builds:
        "input#wc_join_audio_no",
        "input[name='join-audio']",
    ],
    "preview_stop_video": [
        "#preview-video-control-button",
        "button#preview-video-button",
        "button[aria-label='Stop Video']",
        "button[aria-label*='join with video off' i]",
        "button[aria-label*='video off' i]",
        "button[title*='Stop Video' i]",
        "input#wc_join_video_no",
        "input[name='join-video']",
    ],
    # Visual indicators that the toggle is ALREADY in the "off" state, so we
    # don't accidentally click and re-enable mic/cam.
    "preview_audio_is_off_hint": [
        "button[aria-label*='unmute' i]",
        "button[aria-label*='audio is muted' i]",
        ".preview-audio-control-button--off",
    ],
    "preview_video_is_off_hint": [
        "button[aria-label*='start video' i]",
        "button[aria-label*='video is off' i]",
        ".preview-video-control-button--off",
    ],
    "form_ready_any": [   # ANY of these = join form has mounted
        "#input-for-name", "#input-for-pwd", "#inputname",
        "#join-confno", "input[name='confno']",
        "button.preview-join-button", "button#joinBtn",
    ],
}


async def _premute_on_preview(page) -> dict:
    """v8.3.4: on the Zoom preview/join screen, toggle the mic + camera to OFF
    BEFORE the bot clicks Join. So the bot enters the meeting silently with
    no video. Returns {audio_muted, video_off} flags. Idempotent — checks
    for "already off" hints before clicking so we don't re-enable."""
    result = {"audio_muted": False, "video_off": False}

    # === MUTE MIC ===
    if JOIN_WITH_AUDIO_MUTED:
        try:
            # Already off?
            already_off = False
            for sel in ZOOM_SELECTORS["preview_audio_is_off_hint"]:
                try:
                    if await page.locator(sel).count() > 0:
                        already_off = True
                        break
                except Exception:
                    continue
            if already_off:
                result["audio_muted"] = True
            else:
                # Try checkbox-style first (no DOM disturbance), then button toggles
                done = False
                for sel in ZOOM_SELECTORS["preview_mute_audio"]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() == 0:
                            continue
                        tag = (await loc.evaluate("e => e.tagName") or "").lower()
                        if tag == "input":
                            await loc.check(timeout=1500)
                        else:
                            await loc.click(timeout=1500)
                        done = True
                        break
                    except Exception:
                        continue
                result["audio_muted"] = done
        except Exception:
            pass

    # === STOP VIDEO ===
    if JOIN_WITH_VIDEO_OFF:
        try:
            already_off = False
            for sel in ZOOM_SELECTORS["preview_video_is_off_hint"]:
                try:
                    if await page.locator(sel).count() > 0:
                        already_off = True
                        break
                except Exception:
                    continue
            if already_off:
                result["video_off"] = True
            else:
                done = False
                for sel in ZOOM_SELECTORS["preview_stop_video"]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() == 0:
                            continue
                        tag = (await loc.evaluate("e => e.tagName") or "").lower()
                        if tag == "input":
                            await loc.check(timeout=1500)
                        else:
                            await loc.click(timeout=1500)
                        done = True
                        break
                    except Exception:
                        continue
                result["video_off"] = done
        except Exception:
            pass

    return result


async def _wait_any(page, selectors: List[str], timeout_ms: int = 10_000):
    """Race multiple selectors — whichever appears first wins. Returns the
    locator that matched, or None on timeout. Eliminates blind `wait_for_timeout`.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    poll = 0.10
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                pass
        await asyncio.sleep(poll)
        poll = min(0.30, poll * 1.3)
    return None


async def _smart_fill(page, selectors: List[str], value: str, timeout_ms: int = 10_000) -> bool:
    """Try every selector in order — first one that's visible gets filled. Uses
    JS `.value` set + dispatch input/change for max speed (skips per-key delay)."""
    loc = await _wait_any(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        # Fast path: JS value set + event dispatch (~50ms vs ~300ms for .type())
        handle = await loc.element_handle()
        if handle:
            await handle.evaluate("""
                (el, v) => {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }
            """, value)
            return True
        await loc.fill(value, timeout=3000)
        return True
    except Exception:
        try:
            await loc.fill(value, timeout=3000)
            return True
        except Exception:
            return False


async def _smart_click(page, selectors: List[str], timeout_ms: int = 10_000) -> bool:
    loc = await _wait_any(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        await loc.click(timeout=3000)
        return True
    except Exception:
        try:
            handle = await loc.element_handle()
            if handle:
                await handle.evaluate("el => el.click()")
                return True
        except Exception:
            pass
        return False


async def _debug_dump(page, stage: str, name: str):
    """Dump page URL + HTML + screenshot when a join fails so we can tune
    selectors against the EXACT Zoom build hit. Triggered only if DEBUG_DUMP_DOM=true.
    Filenames printed to log so user can `tail` and share."""
    if not DEBUG_DUMP_DOM:
        return
    try:
        os.makedirs(DEBUG_DUMP_DIR, exist_ok=True)
        ts = int(time.time())
        base = f"{DEBUG_DUMP_DIR}/{stage}_{name}_{ts}"
        try:
            url = page.url
        except Exception:
            url = "?"
        try:
            html = await page.content()
            with open(f"{base}.html", "w", encoding="utf-8") as f:
                f.write(f"<!-- URL: {url} -->\n{html}")
        except Exception:
            pass
        try:
            await page.screenshot(path=f"{base}.png", full_page=False)
        except Exception:
            pass
        log.warning(f"DEBUG_DUMP[{stage}] -> {base}.html + {base}.png  url={url}")
    except Exception as e:
        log.debug(f"_debug_dump fail: {e}")


if not DASHBOARD_URL or not WORKER_TOKEN:
    print("ERROR: DASHBOARD_URL and WORKER_TOKEN must be set in .env"); sys.exit(1)

API = f"{DASHBOARD_URL}/api"
HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zoom-worker-v8")

# ---------------------------------------------------------------- chromium args
# Ultra-optimized flag set straight from the architecture doc + extra RAM saves.
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-webgl",
    "--disable-3d-apis",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-mode",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-popup-blocking",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--disable-logging",
    "--disable-translate",
    "--disable-sync",
    "--disable-notifications",
    "--disable-default-apps",
    "--disable-renderer-backgrounding",
    "--disable-features=Translate,BackForwardCache,OptimizationHints,MediaRouter,"
    "DialMediaRouteProvider,CalculateNativeWinOcclusion,InterestFeedContentSuggestions,"
    "GlobalMediaControls,ImprovedCookieControls,AutomationControlled,IsolateOrigins,site-per-process",
    "--mute-audio",
    "--autoplay-policy=no-user-gesture-required",
    "--use-fake-ui-for-media-stream",
    # v8.3.5 GREEN-SCREEN FIX:
    # `--use-fake-device-for-media-stream` (removed) made Chrome generate a
    # green/yellow test-pattern video stream the moment Zoom called
    # getUserMedia({video:true}). Even with JOIN_WITH_VIDEO_OFF the bot
    # broadcast 1-2 frames of that green pattern before our "stop video"
    # click landed. Removing this flag means there is NO video source at
    # all — combined with `permissions=["microphone"]` below, Zoom's video
    # request is denied and no green frames can ever leak out.
    "--no-first-run",
    "--no-default-browser-check",
    "--metrics-recording-only",
    "--password-store=basic",
    "--use-mock-keychain",
    "--ash-no-nudges",
    "--deny-permission-prompts",
    "--log-level=3",
    # v8.3: cache settings flipped based on PERSISTENT_CACHE env var (see below)
    "--renderer-process-limit=2",
    "--blink-settings=imagesEnabled=false",
    "--force-device-scale-factor=0.75",
    "--js-flags=--max-old-space-size=256 --max-semi-space-size=8",
    "--window-size=800,600",
    "--lang=en-US,en",
    "--disable-blink-features=AutomationControlled",
]

# v8.3.4: when running visible (HEADLESS=false on Windows RDP for example)
# push the window off-screen so the operator never sees floating chromium UIs.
if OFFSCREEN_WINDOW and not HEADLESS:
    CHROMIUM_ARGS += [
        "--window-position=-32000,-32000",
        "--start-minimized",
    ]

# v8.3: when persistent cache is on, KEEP Zoom SDK assets on disk so subsequent
# joins reuse js/css/wasm instead of redownloading. Saves ~1-2s per join + ~30%
# network. When off, fall back to v8.1 RAM-saving mode.
if PERSISTENT_CACHE:
    try:
        os.makedirs(PERSISTENT_CACHE_DIR, exist_ok=True)
    except Exception:
        pass
    CHROMIUM_ARGS += [
        f"--disk-cache-dir={PERSISTENT_CACHE_DIR}",
        f"--disk-cache-size={PERSISTENT_CACHE_SIZE_MB * 1024 * 1024}",
    ]
else:
    CHROMIUM_ARGS += [
        "--media-cache-size=1",
        "--disk-cache-size=1",
        "--aggressive-cache-discard",
    ]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- pool models
@dataclass
class BotSlot:
    name: str
    task_id: str
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    joined_at: float = 0.0
    joined: bool = False
    rejoins: int = 0
    closed: bool = False


@dataclass
class BrowserSlot:
    """One chromium process that hosts many isolated BrowserContext tabs."""
    browser: Browser
    bots: Dict[str, BotSlot] = field(default_factory=dict)  # bot_key -> slot
    started_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    @property
    def alive(self) -> bool:
        try:
            return self.browser.is_connected()
        except Exception:
            return False

    @property
    def free_slots(self) -> int:
        return max(0, TABS_PER_BROWSER - len(self.bots))


@dataclass
class ReadyContext:
    """A pre-built BrowserContext + about:blank Page waiting to be claimed.
    Cuts join latency dramatically — newContext()+newPage()+goto() typically
    takes 1.5-3s when cold; a warm context drops that to <100ms.
    """
    browser_slot: "BrowserSlot"
    context: BrowserContext
    page: Page
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------- machine specs
def _machine_specs() -> dict:
    try:
        vm = psutil.virtual_memory()
        return {
            "cpu_count": psutil.cpu_count(logical=True) or 2,
            "total_ram_gb": vm.total / (1024 ** 3),
            "free_ram_gb": vm.available / (1024 ** 3),
            "ram_pct": float(vm.percent),
            "cpu_pct": float(psutil.cpu_percent(interval=None)),
        }
    except Exception:
        return {"cpu_count": 2, "total_ram_gb": 4.0, "free_ram_gb": 2.0,
                "ram_pct": 50.0, "cpu_pct": 0.0}


def _free_ram_pct() -> float:
    try:
        return 100.0 - psutil.virtual_memory().percent
    except Exception:
        return 50.0


def _compute_safe_capacity(specs: Optional[dict] = None) -> int:
    s = specs or _machine_specs()
    by_ram = int((s["total_ram_gb"] * 1024 * (100 - RAM_HEADROOM_PCT) / 100) / RAM_PER_BOT_MB)
    by_cpu = int(s["cpu_count"] * BOTS_PER_CPU)
    cap = min(by_ram, by_cpu, MAX_CAPACITY_HARD_CAP)
    return max(1, cap)


# ---------------------------------------------------------------- v8.3 bootstrap
async def bootstrap_storage_state(pw) -> bool:
    """One-shot warm-up at worker boot: opens a throwaway browser, navigates to the
    Zoom join page, dismisses cookie banners, sets localStorage flags that skip
    the 'Join with Audio' prompt, then saves the resulting cookies + localStorage
    to STORAGE_STATE_PATH. Every subsequent BrowserContext loads this snapshot
    via `storage_state=` so the bot lands DIRECTLY on the name/password form.

    Returns True if a fresh snapshot was written, False if cached snapshot was reused.
    """
    # Skip if a recent snapshot already exists
    try:
        if os.path.exists(STORAGE_STATE_PATH):
            age_h = (time.time() - os.path.getmtime(STORAGE_STATE_PATH)) / 3600
            if age_h < STORAGE_STATE_REFRESH_HOURS:
                log.info(f"bootstrap: reusing storage_state ({age_h:.1f}h old)")
                return False
    except Exception:
        pass

    log.info("bootstrap: building Zoom storage_state (cookies + localStorage)…")
    browser = None
    try:
        browser = await pw.chromium.launch(
            headless=HEADLESS, args=CHROMIUM_ARGS,
            chromium_sandbox=False,
        )
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 800, "height": 600},
            ignore_https_errors=True,
            bypass_csp=True,
            # v8.3.5: MIC ONLY. No camera permission → Zoom's video
            # getUserMedia call is rejected by the browser, so the bot
            # CANNOT broadcast video (no green screen ever).
            permissions=["microphone"],
            locale="en-US",
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://zoom.us/", wait_until="domcontentloaded", timeout=20_000)
        except Exception as e:
            log.warning(f"bootstrap: zoom.us reachable check failed: {e}")

        # Accept cookie banner if present (best-effort)
        for sel in [
            "button#onetrust-accept-btn-handler",
            "button[aria-label='Accept Cookies']",
            "button:has-text('Accept All')",
            "button:has-text('I Accept')",
        ]:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    break
            except Exception:
                pass

        # Seed localStorage / sessionStorage flags that suppress in-meeting prompts.
        # These keys are what Zoom's web client checks before showing
        # the "Join with Computer Audio" modal + the marketing banner.
        try:
            await page.evaluate("""
                () => {
                    try {
                        localStorage.setItem('webclient_audio_setting', 'computer');
                        localStorage.setItem('zm_audio_choice', 'computer');
                        localStorage.setItem('skip_audio_join', '1');
                        localStorage.setItem('zoom_locale', 'en-US');
                        localStorage.setItem('zm_cookie_consent', 'accepted');
                        localStorage.setItem('_zm_cookie_consent_v2', 'all');
                        localStorage.setItem('OptanonAlertBoxClosed', new Date().toISOString());
                        localStorage.setItem('hideNewMeetingPromote', '1');
                        sessionStorage.setItem('webclient_audio_setting', 'computer');
                    } catch(e) {}
                }
            """)
        except Exception:
            pass

        # Now warm the actual join form (so the form-page HTML/JS is in disk cache)
        try:
            await page.goto("https://app.zoom.us/wc/join", wait_until="domcontentloaded", timeout=15_000)
            # Wait for the meeting-id input to mount — confirms SDK JS executed.
            for sel in ["#join-confno", "input[name='confno']", "input[placeholder*='Meeting ID' i]"]:
                try:
                    await page.wait_for_selector(sel, timeout=FORM_PREWARM_WAIT_MS)
                    break
                except Exception:
                    pass
        except Exception as e:
            log.debug(f"bootstrap: join-form preload soft-fail: {e}")

        # Persist snapshot
        try:
            os.makedirs(os.path.dirname(STORAGE_STATE_PATH) or ".", exist_ok=True)
            await ctx.storage_state(path=STORAGE_STATE_PATH)
            log.info(f"bootstrap: storage_state saved -> {STORAGE_STATE_PATH}")
        except Exception as e:
            log.warning(f"bootstrap: storage_state save failed: {e}")
            return False

        await ctx.close()
        return True
    except Exception as e:
        log.warning(f"bootstrap: aborted ({e})")
        return False
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass


def _new_context_kwargs() -> dict:
    """Common kwargs for every BrowserContext we create — includes the warmed
    storage_state snapshot if it exists on disk."""
    kw = dict(
        user_agent=UA,
        viewport={"width": 800, "height": 600},
        ignore_https_errors=True,
        bypass_csp=True,
        # v8.3.5: MIC ONLY (no camera) — see CHROMIUM_ARGS note. With
        # --use-fake-device-for-media-stream removed AND camera permission
        # not granted, Zoom's getUserMedia({video:true}) is rejected →
        # bot physically cannot transmit a video stream, so the host can
        # never see a green/black test-pattern frame.
        permissions=["microphone"],
        locale="en-US",
    )
    if os.path.exists(STORAGE_STATE_PATH):
        kw["storage_state"] = STORAGE_STATE_PATH
    return kw


# ---------------------------------------------------------------- pool manager
class BrowserPool:
    """Owns a small pool of long-lived chromium browsers. Each browser hosts
    up to TABS_PER_BROWSER isolated BrowserContexts (one per bot).

    PREWARM enhancements:
      • At boot we launch PREWARM_BROWSERS chromium processes and PREWARM_CONTEXTS
        ready BrowserContexts (each with a preloaded about:blank/Zoom page).
      • acquire_ready_context() returns a hot context in O(1).
      • An auto-warmup loop keeps `>= PREWARM_MIN_READY` standby contexts.
      • An auto-shrink loop closes idle hot browsers after SHRINK_IDLE_SEC.
    """

    def __init__(self, pw):
        self.pw = pw
        self.browsers: List[BrowserSlot] = []
        self.ready: List[ReadyContext] = []  # warm standby pool
        self.lock = asyncio.Lock()
        self._prewarmed = False
        # v8.3.3: dynamic targets — start at env defaults, retune to admin_cap on each heartbeat
        self.target_ready: int = max(0, PREWARM_CONTEXTS)
        self.target_min: int = max(0, PREWARM_MIN_READY)
        self.target_max: int = max(0, PREWARM_MAX_READY)
        self.target_browsers: int = max(0, PREWARM_BROWSERS)

    def retune(self, admin_cap: Optional[int]):
        """v8.3.3: when admin updates capacity_max, immediately resize our
        ready-context pool to match. "jitna limit, utne ready"."""
        if not PREWARM_MATCH_ADMIN_CAP or admin_cap is None or admin_cap <= 0:
            return
        cap = min(int(admin_cap), PREWARM_HARD_CEILING)
        # Ready pool target = full admin cap (every slot prewarmed)
        self.target_ready = cap
        # Browsers needed = ceil(cap / TABS_PER_BROWSER)
        self.target_browsers = max(1, (cap + TABS_PER_BROWSER - 1) // TABS_PER_BROWSER)
        # Min/Max watermarks stay close to target so warmup keeps it full
        self.target_min = cap
        self.target_max = cap

    async def _launch_browser(self) -> BrowserSlot:
        browser = await self.pw.chromium.launch(
            headless=HEADLESS,
            args=CHROMIUM_ARGS,
            chromium_sandbox=False,
            handle_sigterm=False,
            handle_sigint=False,
            handle_sighup=False,
        )
        slot = BrowserSlot(browser=browser)
        log.info(f"pool: launched chromium (pool size {len(self.browsers) + 1})")
        return slot

    async def _make_ready_context(self, browser_slot: BrowserSlot) -> Optional[ReadyContext]:
        """Pre-create one BrowserContext + Page + (optionally) preload zoom shell.
        v8.3: loads warmed storage_state (cookies + localStorage) and waits for
        the join-form DOM to mount so the next step is literally just fill+click.
        """
        try:
            ctx = await browser_slot.browser.new_context(**_new_context_kwargs())

            async def _block(route):
                try:
                    if route.request.resource_type in ("image", "media", "font"):
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    try: await route.continue_()
                    except Exception: pass
            await ctx.route("**/*", _block)
            page = await ctx.new_page()

            # PRELOAD: hit the Zoom join form shell so DNS/TLS/SDK JS/CSS cache is warm
            # AND the #meeting-id input is already mounted.
            if PREWARM_PRELOAD_URL:
                try:
                    await page.goto(PREWARM_PRELOAD_URL, wait_until="domcontentloaded", timeout=15_000)
                    # v8.3.1: verify the form actually mounted using the shared
                    # ZOOM_SELECTORS list (single source of truth).
                    await _wait_any(page, ZOOM_SELECTORS["form_ready_any"],
                                    timeout_ms=FORM_PREWARM_WAIT_MS)
                except Exception:
                    # about:blank fallback — still gives us instant context handoff
                    try: await page.goto("about:blank", timeout=5_000)
                    except Exception: pass

            return ReadyContext(browser_slot=browser_slot, context=ctx, page=page)
        except Exception as e:
            log.debug(f"prewarm: ready context build failed: {e}")
            return None

    async def prewarm(self):
        """One-shot bootstrap — called once from main()."""
        if self._prewarmed or not PREWARM_ENABLED:
            return
        async with self.lock:
            # Launch hot browsers up to current target_browsers
            for _ in range(max(0, self.target_browsers)):
                try:
                    slot = await self._launch_browser()
                    self.browsers.append(slot)
                except Exception as e:
                    log.warning(f"prewarm: launch failed: {e}")
            # Spread ready contexts across hot browsers
            needed = max(0, self.target_ready)
            if needed and self.browsers:
                idx = 0
                while needed > 0:
                    target = self.browsers[idx % len(self.browsers)]
                    if target.free_slots <= 0:
                        idx += 1
                        if idx >= len(self.browsers) * 2:
                            break
                        continue
                    rc = await self._make_ready_context(target)
                    if rc:
                        self.ready.append(rc)
                        needed -= 1
                    idx += 1
        self._prewarmed = True
        log.info(
            f"prewarm: hot_browsers={len(self.browsers)} "
            f"ready_contexts={len(self.ready)}/{self.target_ready} "
            f"preload_url={PREWARM_PRELOAD_URL!r}"
        )

    async def acquire_ready_context(self) -> Optional[ReadyContext]:
        """O(1) handoff of a pre-built context. Returns None if pool empty."""
        async with self.lock:
            while self.ready:
                rc = self.ready.pop(0)
                if rc.browser_slot.alive:
                    rc.browser_slot.last_used_at = time.time()
                    return rc
                # browser died — drop and continue
            return None

    async def topup_ready(self):
        """AUTO WARMUP ENGINE — keep at least target_min contexts on standby
        (target = admin's capacity_max when PREWARM_MATCH_ADMIN_CAP=true)."""
        if not PREWARM_ENABLED:
            return
        try:
            async with self.lock:
                # Drop dead entries first
                self.ready = [r for r in self.ready if r.browser_slot.alive]
                if len(self.ready) >= self.target_min:
                    return
                # Refill up to target_max
                deficit = self.target_max - len(self.ready)
                if deficit <= 0:
                    return
                # Find/grow browser slots — we need ceil(target_max/TABS_PER_BROWSER)
                self.browsers = [b for b in self.browsers if b.alive]
                # Grow browser fleet up to dynamic target
                while len(self.browsers) < self.target_browsers:
                    try:
                        slot = await self._launch_browser()
                        self.browsers.append(slot)
                    except Exception:
                        break
                target: Optional[BrowserSlot] = None
                for b in self.browsers:
                    if b.free_slots > 0:
                        target = b; break
                if target is None:
                    return
                # Build a few at a time so we don't stall (5 per cycle = ~10s for 50)
                build = min(deficit, 5, target.free_slots)
                for _ in range(build):
                    rc = await self._make_ready_context(target)
                    if rc:
                        self.ready.append(rc)
                    else:
                        break
        except Exception as e:
            log.debug(f"topup_ready err: {e}")

    async def shrink_idle(self):
        """AUTO SHRINK ENGINE — close hot browsers that have been idle too long."""
        if not PREWARM_ENABLED:
            return
        try:
            async with self.lock:
                keep: List[BrowserSlot] = []
                now = time.time()
                for b in self.browsers:
                    idle = (now - b.last_used_at) > SHRINK_IDLE_SEC
                    if not b.bots and idle and len(self.browsers) > 1:
                        # Drop any ready contexts hosted in this browser
                        self.ready = [r for r in self.ready if r.browser_slot is not b]
                        try: await b.browser.close()
                        except Exception: pass
                        log.info("shrink: closed idle hot browser")
                    else:
                        keep.append(b)
                self.browsers = keep
        except Exception as e:
            log.debug(f"shrink_idle err: {e}")

    async def acquire_slot(self) -> BrowserSlot:
        async with self.lock:
            # Remove dead browsers
            self.browsers = [b for b in self.browsers if b.alive]
            # Find a browser with free capacity
            for b in self.browsers:
                if b.free_slots > 0:
                    b.last_used_at = time.time()
                    return b
            slot = await self._launch_browser()
            self.browsers.append(slot)
            return slot

    async def release_browser_if_empty(self):
        """Close any chromium that has 0 bots — frees RAM aggressively.
        Respects prewarm: keeps at least target_browsers hot at all times
        (these are the ones serving the warm-standby contexts)."""
        async with self.lock:
            keep: List[BrowserSlot] = []
            hot_target = max(0, self.target_browsers) if PREWARM_ENABLED else 0
            for b in self.browsers:
                if not b.bots and b.alive and len(keep) >= hot_target:
                    # Drop ready contexts hosted here
                    self.ready = [r for r in self.ready if r.browser_slot is not b]
                    try:
                        await b.browser.close()
                        log.info(f"pool: closed empty chromium (remaining {len(keep)})")
                    except Exception:
                        pass
                else:
                    keep.append(b)
            self.browsers = keep

    async def shutdown(self):
        async with self.lock:
            for r in self.ready:
                try: await r.context.close()
                except Exception: pass
            self.ready.clear()
            for b in self.browsers:
                try:
                    await b.browser.close()
                except Exception:
                    pass
            self.browsers.clear()

    def stats(self) -> dict:
        # v8.3: include storage_state freshness so dashboard knows tap-and-join is armed
        ss_age_h = None
        try:
            if os.path.exists(STORAGE_STATE_PATH):
                ss_age_h = round((time.time() - os.path.getmtime(STORAGE_STATE_PATH)) / 3600, 2)
        except Exception:
            pass
        return {
            "browsers": len(self.browsers),
            "total_bots": sum(len(b.bots) for b in self.browsers),
            "alive": sum(1 for b in self.browsers if b.alive),
            "ready_contexts": len(self.ready),
            "prewarmed": self._prewarmed,
            "version": "v8.3.5-no-green-screen",
            "storage_state_age_hours": ss_age_h,
            "persistent_cache": PERSISTENT_CACHE,
            "preload_url": PREWARM_PRELOAD_URL,
            # v8.3.3: expose dynamic targets so dashboard can show "X/Y ready"
            "target_ready": self.target_ready,
            "target_browsers": self.target_browsers,
            "match_admin_cap": PREWARM_MATCH_ADMIN_CAP,
        }


# ---------------------------------------------------------------- API helpers
def heartbeat(load_override: int, capacity_override: Optional[int] = None,
              pool_stats: Optional[dict] = None) -> Optional[int]:
    """Send heartbeat. Returns the admin-set `capacity_max` from the server
    response so the worker can locally enforce the hard ceiling (avoids even
    *attempting* to claim more tasks than admin allows)."""
    s = _machine_specs()
    payload = {
        "current_load": load_override,
        "cpu_pct": s["cpu_pct"],
        "ram_pct": s["ram_pct"],
        "hostname": socket.gethostname(),
        "os_info": f"{platform.system()} {platform.release()} (Playwright v8.3.5-no-green-screen)",
        "cpu_count": s["cpu_count"],
        "ram_free_gb": round(s["free_ram_gb"], 2),
    }
    if AUTO_CAPACITY:
        cap = capacity_override if capacity_override is not None else _compute_safe_capacity(s)
        payload["reported_capacity"] = cap
    if pool_stats:
        payload["pool_stats"] = pool_stats
    try:
        r = requests.post(f"{API}/workers/me/heartbeat", headers=HEADERS, json=payload, timeout=10)
        if r.status_code == 200:
            try:
                return int(r.json().get("capacity_max")) or None
            except Exception:
                return None
    except Exception as e:
        log.warning(f"heartbeat err: {e}")
    return None


def claim_tasks(n: int = 5) -> List[dict]:
    try:
        r = requests.post(f"{API}/workers/me/claim", headers=HEADERS,
                          params={"max_tasks": n}, timeout=15)
        if r.status_code != 200:
            return []
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


# ---------------------------------------------------------------- name loading
_LOCAL_NAMES: List[str] = []


def _load_local_names() -> List[str]:
    global _LOCAL_NAMES
    if _LOCAL_NAMES:
        return _LOCAL_NAMES
    if not LOCAL_NAMES_FILE:
        return []
    p = Path(LOCAL_NAMES_FILE)
    if not p.exists():
        log.warning(f"LOCAL_NAMES_FILE not found: {LOCAL_NAMES_FILE}")
        return []
    try:
        names = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        _LOCAL_NAMES = names
        log.info(f"loaded {len(names)} names from {LOCAL_NAMES_FILE}")
        return names
    except Exception as e:
        log.warning(f"local names load failed: {e}")
        return []


def _pick_local_names(count: int) -> List[str]:
    import random as _r
    pool = _load_local_names()
    if not pool:
        return []
    if count <= len(pool):
        return _r.sample(pool, count)
    out: List[str] = []
    while len(out) < count:
        sh = pool[:]
        _r.shuffle(sh)
        out.extend(sh[: count - len(out)])
    return out


# ---------------------------------------------------------------- bot lifecycle
async def _join_meeting(page: Page, meeting_id: str, password: str, name: str) -> bool:
    """Single join attempt — returns True if entered meeting.
    v8.3.1: smart multi-selector + zero blind waits. Tries every known Zoom
    selector and dumps the page DOM on failure for selector-tuning."""
    try:
        await page.goto(f"https://app.zoom.us/wc/{meeting_id}/join",
                        wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        log.debug(f"goto failed for {name}: {e}")
        return False

    # CDP-style stealth (kept from v8.1)
    try:
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});"
            "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
        )
    except Exception:
        pass

    # ===== Wait for ANY form element (no more blind 4s sleep) =====
    form = await _wait_any(page, ZOOM_SELECTORS["form_ready_any"], timeout_ms=15_000)
    if form is None:
        log.warning(f"{name}: join form never mounted")
        await _debug_dump(page, "no_form", name)
        return False

    # ===== Password (optional) =====
    if password:
        await _smart_fill(page, ZOOM_SELECTORS["password_input"], password, timeout_ms=3000)
        # Don't fail on missing — many meetings need no password

    # ===== Name (required) =====
    if not await _smart_fill(page, ZOOM_SELECTORS["name_input"], name, timeout_ms=8000):
        log.warning(f"{name}: could not find name input")
        await _debug_dump(page, "no_name_input", name)
        return False

    # ===== v8.3.4: Pre-mute mic + stop video on the preview screen =====
    # This way bot enters meeting silent + no camera (no Zoom popup, no host approval needed).
    pm = await _premute_on_preview(page)
    if JOIN_WITH_AUDIO_MUTED or JOIN_WITH_VIDEO_OFF:
        log.debug(f"{name}: pre-toggle mic_muted={pm['audio_muted']} video_off={pm['video_off']}")

    # ===== Click Join =====
    if not await _smart_click(page, ZOOM_SELECTORS["join_button"], timeout_ms=8000):
        # Last resort: hit Enter on the name field
        try:
            await page.keyboard.press("Enter")
        except Exception:
            await _debug_dump(page, "no_join_button", name)
            return False

    # ===== Wait for "in meeting" — any of the meeting-shell selectors =====
    in_meeting_loc = await _wait_any(page, ZOOM_SELECTORS["in_meeting"], timeout_ms=35_000)
    if in_meeting_loc is None:
        log.warning(f"{name}: never entered meeting (timeout)")
        await _debug_dump(page, "no_meeting_shell", name)
        return await _is_in_meeting(page)  # fall through to legacy verify

    # ===== Dismiss any "Join Audio by Computer" prompt (best-effort, fast) =====
    try:
        audio_btn = await _wait_any(page, ZOOM_SELECTORS["audio_join"], timeout_ms=1500)
        if audio_btn is not None:
            try: await audio_btn.click(timeout=1500)
            except Exception: pass
    except Exception:
        pass

    return await _is_in_meeting(page)


async def _is_in_meeting(page: Page) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "zoom.us" not in url and "/wc/" not in url:
        return False
    for sel in ZOOM_SELECTORS["in_meeting"]:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    # Negative signal — still on preview screen
    try:
        for sel in ZOOM_SELECTORS["join_button"]:
            if await page.locator(sel).count() > 0:
                return False
    except Exception:
        pass
    return True


async def _post_join_optimize(page: Page):
    """Reduce CPU/RAM use after the bot is in the meeting:
       - **Guarantee mic is muted** + video is off (safety net if pre-mute missed)
       - hide all <video>/<canvas> via CSS so GPU isn't decoding remote streams
       - flip document.visibilityState='hidden' so Chrome throttles renderer ~10×
    """
    # Belt-and-suspenders MIC MUTE — if pre-mute on preview failed, mute now
    if JOIN_WITH_AUDIO_MUTED:
        for _ in range(2):  # retry once
            try:
                # Look for "unmute" label first → means currently MUTED, skip
                unmute_state = page.locator("button[aria-label*='unmute my microphone' i]").first
                if await unmute_state.count() > 0:
                    break  # already muted ✓
                mic = page.locator("button[aria-label*='mute my microphone' i]").first
                if await mic.count() > 0:
                    await mic.click(timeout=2000)
                    await page.wait_for_timeout(200)
            except Exception:
                pass

    # Belt-and-suspenders STOP VIDEO
    if JOIN_WITH_VIDEO_OFF:
        for _ in range(2):
            try:
                # If "Start Video" is present → means already off, skip
                start_state = page.locator("button[aria-label*='start video' i]").first
                if await start_state.count() > 0:
                    break  # already off ✓
                vid = page.locator("button[aria-label*='stop my video' i]").first
                if await vid.count() > 0:
                    await vid.click(timeout=2000)
                    await page.wait_for_timeout(200)
            except Exception:
                pass
    # Kill remote video render + throttle
    try:
        await page.evaluate(
            """
            (() => {
              const s = document.createElement('style');
              s.textContent = `video,canvas,.video-tile,.video-container,
                .gallery-video-container__main-view,.speaker-active-container,
                .shared-container{display:none !important;visibility:hidden !important;}`;
              document.head.appendChild(s);
              const kill = () => {
                document.querySelectorAll('video').forEach(v => {
                  try { v.pause(); v.srcObject=null; v.style.display='none'; } catch(e){}
                });
              };
              kill();
              setInterval(kill, 4000);
              Object.defineProperty(document, 'visibilityState', {get: () => 'hidden'});
              Object.defineProperty(document, 'hidden', {get: () => true});
              document.dispatchEvent(new Event('visibilitychange'));
            })();
            """
        )
    except Exception:
        pass


async def run_bot(slot: BotSlot, browser_slot: BrowserSlot, meeting_id: str,
                  password: str, hold_seconds: int,
                  pool: Optional["BrowserPool"] = None) -> bool:
    """One bot lifecycle inside a shared browser:
       newContext → newPage → join → hold → cleanup.
    Returns True if it ever joined.

    Optimization: if `pool` is provided AND has a ready prewarmed context,
    we use it INSTEAD of creating a fresh one (instant handoff).
    """
    try:
        ctx = None
        page = None
        # ===== PREWARM PATH =====
        if pool is not None:
            rc = await pool.acquire_ready_context()
            if rc is not None:
                ctx = rc.context
                page = rc.page
                browser_slot = rc.browser_slot
                slot.context = ctx
                slot.page = page

        # ===== COLD PATH (fallback) =====
        if ctx is None:
            ctx = await browser_slot.browser.new_context(**_new_context_kwargs())
            # Block images + media for extra savings
            async def _block(route):
                try:
                    if route.request.resource_type in ("image", "media", "font"):
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    try: await route.continue_()
                    except Exception: pass
            await ctx.route("**/*", _block)
            slot.context = ctx
            page = await ctx.new_page()
            slot.page = page

        # Join with retry
        joined = False
        for attempt in range(BOT_REJOIN_MAX + 1):
            try:
                if await _join_meeting(page, meeting_id, password, slot.name):
                    joined = True; break
            except Exception as e:
                log.debug(f"[{slot.name}] attempt {attempt+1} err: {e}")
            await asyncio.sleep(3)

        if not joined:
            return False

        slot.joined = True
        slot.joined_at = time.time()
        await _post_join_optimize(page)

        # Hold + monitor for kick / cancel
        end = time.time() + hold_seconds
        while time.time() < end and not slot.closed:
            await asyncio.sleep(15)
            try:
                in_room = await _is_in_meeting(page)
            except Exception:
                break
            if not in_room:
                # Within kick window? attempt rejoin.
                if (time.time() - slot.joined_at) <= KICK_DETECT_WINDOW and slot.rejoins < BOT_REJOIN_MAX:
                    slot.rejoins += 1
                    log.info(f"[{slot.name}] kicked, rejoin {slot.rejoins}/{BOT_REJOIN_MAX}")
                    try:
                        if await _join_meeting(page, meeting_id, password, slot.name):
                            slot.joined_at = time.time()
                            await _post_join_optimize(page)
                            continue
                    except Exception:
                        pass
                break
        return True
    finally:
        # ===== AUTO CLEANUP: meeting ended → close page + context =====
        try:
            if slot.page:
                await slot.page.close()
        except Exception: pass
        try:
            if slot.context:
                await slot.context.close()
        except Exception: pass
        slot.closed = True
        browser_slot.bots.pop(slot.name, None)


# ---------------------------------------------------------------- task runner
class TaskRunner:
    def __init__(self, pool: BrowserPool):
        self.pool = pool
        self.tasks: Dict[str, dict] = {}     # task_id -> {bots, joined, started_at}
        self.tasks_lock = asyncio.Lock()
        # dynamic_floor lets the health monitor LOWER live capacity
        self.dynamic_floor: Optional[int] = None

    def joined_count(self) -> int:
        return sum(t.get("joined", 0) for t in self.tasks.values())

    def total_bots_alive(self) -> int:
        n = 0
        for t in self.tasks.values():
            for b in t["bots"]:
                if b.joined and not b.closed:
                    n += 1
        return n

    async def run_task(self, task: dict):
        task_id = task["id"]
        meeting_id = task["meeting_id"]
        password = task.get("meeting_password") or ""
        members = int(task.get("members", 0))
        timeout_sec = int(task.get("timeout", 7200))

        local = _pick_local_names(members) if LOCAL_NAMES_FILE else []
        names = local if local else (task.get("names") or [f"User{i+1}" for i in range(members)])

        log.info(f"▶ task {task_id[:8]} | mid={meeting_id} members={members} timeout={timeout_sec}s")

        bot_slots: List[BotSlot] = []
        async with self.tasks_lock:
            self.tasks[task_id] = {"bots": bot_slots, "joined": 0, "started_at": time.time()}

        runners: List[asyncio.Task] = []
        # SEQUENTIAL FILL with browser pooling — pack browsers up first.
        for i in range(members):
            # RAM safety gate
            if _free_ram_pct() < PRE_SPAWN_FREE_RAM_PCT:
                log.warning(f"  RAM low ({_free_ram_pct():.0f}%), pausing spawn at {i}/{members}")
                waited = 0
                while waited < 60 and _free_ram_pct() < PRE_SPAWN_FREE_RAM_PCT:
                    await asyncio.sleep(3); waited += 3
                if _free_ram_pct() < PRE_SPAWN_FREE_RAM_PCT:
                    log.warning(f"  stopping spawn at {i}/{members} (RAM stayed low)")
                    break

            browser_slot = await self.pool.acquire_slot()
            slot = BotSlot(name=names[i], task_id=task_id)
            browser_slot.bots[slot.name + f"#{i}"] = slot
            bot_slots.append(slot)

            runners.append(asyncio.create_task(
                run_bot(slot, browser_slot, meeting_id, password, timeout_sec, pool=self.pool)
            ))
            # tiny stagger so chromium doesn't get hammered
            await asyncio.sleep(SPAWN_DELAY_MS / 1000.0)

        # Watcher: progress + cancel checks
        last_reported = 0
        deadline = time.time() + timeout_sec + 60
        last_cancel_check = 0.0
        while time.time() < deadline:
            joined = sum(1 for s in bot_slots if s.joined and not s.closed)
            if joined != last_reported:
                report_progress(task_id, joined)
                async with self.tasks_lock:
                    self.tasks[task_id]["joined"] = joined
                last_reported = joined
                log.info(f"  ✓ joined {joined}/{members} task={task_id[:8]}")

            if time.time() - last_cancel_check > 10:
                last_cancel_check = time.time()
                status = check_chunk_status(task_id)
                if status in ("cancelled", "failed"):
                    log.warning(f"  task {task_id[:8]} {status} by dashboard")
                    for s in bot_slots: s.closed = True
                    break

            alive_runners = sum(1 for r in runners if not r.done())
            if alive_runners == 0:
                break
            await asyncio.sleep(3)

        # Stop everything for this task
        for s in bot_slots:
            s.closed = True
        # Wait briefly for cleanups
        try:
            await asyncio.wait(runners, timeout=15)
        except Exception:
            pass
        for r in runners:
            if not r.done():
                r.cancel()

        await self.pool.release_browser_if_empty()
        gc.collect()

        final_joined = sum(1 for s in bot_slots if s.joined)
        async with self.tasks_lock:
            self.tasks.pop(task_id, None)
        complete_task(task_id, success=True, joined=final_joined)
        log.info(f"✓ task {task_id[:8]} done: joined {final_joined}/{members}")


# ---------------------------------------------------------------- health monitor
async def health_monitor(runner: TaskRunner, pool: BrowserPool, stop_event: asyncio.Event):
    """Background watcher: enforces dynamic limits + restarts dead chromium.
    Per the architecture doc:
        if cpu > 75: max_members -= 5
    """
    base_cap = _compute_safe_capacity()
    dyn_cap = base_cap
    while not stop_event.is_set():
        try:
            s = _machine_specs()
            # ===== DYNAMIC THROTTLE =====
            if s["cpu_pct"] > CPU_THROTTLE_PCT or s["ram_pct"] > RAM_THROTTLE_PCT:
                dyn_cap = max(1, dyn_cap - DYNAMIC_LIMIT_STEP)
                log.warning(
                    f"throttle: cpu={s['cpu_pct']:.0f}% ram={s['ram_pct']:.0f}% "
                    f"→ dynamic_cap={dyn_cap}"
                )
            elif s["cpu_pct"] < CPU_THROTTLE_PCT - 15 and s["ram_pct"] < RAM_THROTTLE_PCT - 10:
                # Recover gently
                fresh = _compute_safe_capacity(s)
                if dyn_cap < fresh:
                    dyn_cap = min(fresh, dyn_cap + DYNAMIC_LIMIT_STEP)
            runner.dynamic_floor = dyn_cap

            # ===== AUTO-RESTART DEAD CHROMIUM =====
            for b in list(pool.browsers):
                if not b.alive:
                    log.warning("health: chromium died, will be replaced on next acquire")
                    pool.browsers.remove(b)
        except Exception as e:
            log.warning(f"health monitor err: {e}")
        await asyncio.sleep(10)


async def cleanup_loop(pool: BrowserPool, runner: TaskRunner, stop_event: asyncio.Event):
    """Periodic orphan sweep — kills any rogue chromium NOT owned by our pool,
    plus closes empty pool browsers."""
    while not stop_event.is_set():
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)
        try:
            await pool.release_browser_if_empty()
            _orphan_chromium_sweep()
        except Exception as e:
            log.warning(f"cleanup err: {e}")


async def warmup_loop(pool: BrowserPool, stop_event: asyncio.Event):
    """AUTO WARMUP ENGINE — keep the warm-standby ready-context pool topped up
    and shrink idle hot browsers. Runs every WARMUP_INTERVAL_SEC."""
    while not stop_event.is_set():
        try:
            await pool.topup_ready()
            await pool.shrink_idle()
            st = pool.stats()
            log.debug(f"warmup: ready={st['ready_contexts']} browsers={st['browsers']} bots={st['total_bots']}")
        except Exception as e:
            log.debug(f"warmup err: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=WARMUP_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


def _orphan_chromium_sweep():
    """Kill chromium processes whose CWD/cmdline contains no recognizable pool
    marker. Best-effort — on Linux mainly; on Windows just no-ops if not psutil."""
    if not psutil:
        return
    # Our pool browsers are still alive — psutil cannot easily distinguish them
    # from orphans. We use a lightweight heuristic: if a chromium process has
    # been alive > 1h AND has parent_pid != our PID, treat as orphan.
    my_pid = os.getpid()
    killed = 0
    for p in psutil.process_iter(["pid", "name", "ppid", "create_time"]):
        try:
            n = (p.info.get("name") or "").lower()
            if not any(x in n for x in ("chromium", "chrome")):
                continue
            ppid = p.info.get("ppid")
            age = time.time() - p.info.get("create_time", time.time())
            # NOTE: under Playwright, chromium parents are the playwright host
            # process (NOT our worker PID directly). So we skip parents in the
            # PROCESS TREE rooted at our PID. Quick check: walk up.
            try:
                cur = psutil.Process(ppid)
                in_our_tree = False
                for _ in range(6):
                    if cur.pid == my_pid:
                        in_our_tree = True; break
                    cur = psutil.Process(cur.ppid())
                if in_our_tree:
                    continue
            except Exception:
                pass
            if age > 3600:  # > 1 hour and not in our tree
                p.kill(); killed += 1
        except Exception:
            continue
    if killed:
        log.info(f"cleanup: killed {killed} orphan chromium processes")


# ---------------------------------------------------------------- main
async def main():
    stop_event = asyncio.Event()

    def _sig(*_):
        log.info("signal received → shutting down")
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig_name):
            try:
                signal.signal(getattr(signal, sig_name), _sig)
            except Exception:
                pass

    s = _machine_specs()
    log.info(f"Zoom Worker v8.3.5 (headless + muted-join + offscreen + no-camera-perm) starting")
    log.info(f"  dashboard={DASHBOARD_URL}")
    log.info(f"  cpu={s['cpu_count']}c  ram={s['total_ram_gb']:.1f}G  "
             f"safe_cap={_compute_safe_capacity(s)}")
    log.info(f"  tabs_per_browser={TABS_PER_BROWSER}  headless={HEADLESS}  "
             f"poll={POLL_INTERVAL}s")
    log.info(f"  prewarm: enabled={PREWARM_ENABLED} browsers={PREWARM_BROWSERS} "
             f"contexts={PREWARM_CONTEXTS} min={PREWARM_MIN_READY} max={PREWARM_MAX_READY}")
    log.info(f"  v8.3:    persistent_cache={PERSISTENT_CACHE} ({PERSISTENT_CACHE_DIR}, {PERSISTENT_CACHE_SIZE_MB}MB)  "
             f"storage_state={STORAGE_STATE_PATH}  preload_url={PREWARM_PRELOAD_URL}")
    log.info(f"  v8.3.4:  headless={HEADLESS} offscreen={OFFSCREEN_WINDOW}  "
             f"mute_on_join={JOIN_WITH_AUDIO_MUTED}  video_off_on_join={JOIN_WITH_VIDEO_OFF}")

    async with async_playwright() as pw:
        # ===== v8.3: BOOTSTRAP — bake cookies + localStorage once per worker boot =====
        try:
            await bootstrap_storage_state(pw)
        except Exception as e:
            log.warning(f"bootstrap_storage_state failed (continuing without): {e}")

        pool = BrowserPool(pw)
        runner = TaskRunner(pool)

        # ===== PREWARM ENGINE: launch hot browsers + ready contexts upfront =====
        await pool.prewarm()

        # background tasks
        health = asyncio.create_task(health_monitor(runner, pool, stop_event))
        cleanup = asyncio.create_task(cleanup_loop(pool, runner, stop_event))
        warmup = asyncio.create_task(warmup_loop(pool, stop_event))

        try:
            admin_cap: Optional[int] = None
            while not stop_event.is_set():
                load = runner.total_bots_alive()
                cap_override = runner.dynamic_floor
                pool_stats = pool.stats()
                # heartbeat in a thread so it never blocks event loop.
                # Server returns the admin-set capacity_max so we can locally
                # enforce the hard ceiling.
                admin_cap = await asyncio.to_thread(heartbeat, load, cap_override, pool_stats) or admin_cap

                # ===== v8.3.3: retune the prewarm pool to match admin cap =====
                # "jitna limit, utne ready" — if admin sets 50, we keep 50 ready.
                pool.retune(admin_cap)

                # ===== v8.3.2 admin HARD CEILING =====
                # If admin set capacity_max=50 and we already have 50 bots,
                # don't even bother claiming more — let the next RDP take over.
                ceiling_hit = (admin_cap is not None and load >= admin_cap)
                if ceiling_hit:
                    log.debug(f"admin-cap hit: load={load} >= cap={admin_cap}, skipping claim")

                if not ceiling_hit and len(runner.tasks) < MAX_CONCURRENT_TASKS:
                    # Cap claim count to the remaining headroom under admin ceiling
                    headroom = (admin_cap - load) if admin_cap is not None else 9999
                    if headroom > 0:
                        n = min(5, MAX_CONCURRENT_TASKS - len(runner.tasks), max(1, headroom))
                        claimed = await asyncio.to_thread(claim_tasks, n)
                        for t in claimed:
                            asyncio.create_task(runner.run_task(t))

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        finally:
            log.info("shutdown: closing pool…")
            stop_event.set()
            health.cancel(); cleanup.cancel(); warmup.cancel()
            await pool.shutdown()
            # mark every in-flight task failed
            for tid, t in list(runner.tasks.items()):
                complete_task(tid, success=False, joined=t.get("joined", 0),
                              error="worker shutdown")
            log.info("bye")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
