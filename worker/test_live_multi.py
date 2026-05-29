"""Multi-bot live stress test.

Spawns N bots concurrently in a SINGLE Chromium process (TABS_PER_BROWSER
shared model — same architecture used in production).

  • Audio: pre-muted on preview screen + belt-and-suspenders mute after join
  • Video: no fake media device → no green-frame leak possible
  • Reactions: ON with short interval so the host can clearly see them fire

Usage:
    NUM_BOTS=8 HOLD_SECONDS=300 python /app/worker/test_live_multi.py
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Force-config BEFORE import.
os.environ.setdefault("HEADLESS", "true")
os.environ.setdefault("REACTIONS_ENABLED", "true")
os.environ.setdefault("JOIN_WITH_AUDIO_MUTED", "true")
os.environ.setdefault("JOIN_WITH_VIDEO_OFF", "true")
os.environ.setdefault("STRICT_ANTI_LEAVE", "true")
os.environ.setdefault("REACTION_INTERVAL_MIN", "8")
os.environ.setdefault("REACTION_INTERVAL_MAX", "18")
os.environ.setdefault("PERSISTENT_CACHE", "false")
os.environ.setdefault("DEBUG_DUMP_DOM", "true")
os.environ.setdefault("DASHBOARD_URL", "http://localhost:8001")
os.environ.setdefault("WORKER_TOKEN", "livetest-dummy")

import zoom_worker_pool as zwp  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("multitest")

MEETING_ID = os.environ.get("TEST_MEETING_ID", "81023933289")
PASSWORD = os.environ.get("TEST_MEETING_PASSWORD", "145700")
NUM_BOTS = int(os.environ.get("NUM_BOTS", "8"))
HOLD_SECONDS = int(os.environ.get("HOLD_SECONDS", "300"))
STAGGER_MS = int(os.environ.get("STAGGER_MS", "1500"))  # spacing between joins

NAMES = [
    "Aarav Sharma", "Priya Patel", "Rohan Verma", "Ananya Iyer",
    "Vivaan Gupta", "Diya Mehta", "Ishaan Kapoor", "Sara Joshi",
    "Kabir Nair", "Aisha Reddy",
]


async def one_bot(idx: int, browser_slot, name: str):
    slot = zwp.BotSlot(name=name, task_id="multitest")
    browser_slot.bots[name] = slot
    task_cfg = {
        "participant_reactions": True,
        "floating_emoji": True,
        "reaction_interval_min": int(os.environ["REACTION_INTERVAL_MIN"]),
        "reaction_interval_max": int(os.environ["REACTION_INTERVAL_MAX"]),
    }
    # Staggered join so we don't slam Zoom at the same instant
    await asyncio.sleep((idx * STAGGER_MS) / 1000.0)
    log.info(f"[{name}] launching (idx={idx})")
    try:
        ok = await zwp.run_bot(
            slot=slot,
            browser_slot=browser_slot,
            meeting_id=MEETING_ID,
            password=PASSWORD,
            hold_seconds=HOLD_SECONDS,
            pool=None,
            task_cfg=task_cfg,
        )
        log.info(f"[{name}] DONE joined={slot.joined} rejoins={slot.rejoins} ok={ok}")
        return slot
    except Exception as e:
        log.error(f"[{name}] CRASHED: {e}")
        return slot


async def main():
    log.info("=" * 78)
    log.info("ULTRA PRO MULTI-BOT TEST | bots=%s hold=%ss meeting=%s",
             NUM_BOTS, HOLD_SECONDS, MEETING_ID)
    log.info("HEADLESS=%s REACTIONS=%s A/V=OFF STRICT_ANTI_LEAVE=%s",
             zwp.HEADLESS, zwp.REACTIONS_ENABLED, zwp.STRICT_ANTI_LEAVE)
    log.info("=" * 78)

    async with zwp.async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=zwp.HEADLESS,
            args=zwp.CHROMIUM_ARGS,
        )
        browser_slot = zwp.BrowserSlot(browser=browser)

        names = NAMES[:NUM_BOTS]
        tasks = [
            asyncio.create_task(one_bot(i, browser_slot, n))
            for i, n in enumerate(names)
        ]
        slots = await asyncio.gather(*tasks, return_exceptions=False)

        # Summary
        joined_n = sum(1 for s in slots if getattr(s, "joined", False))
        total_rejoins = sum(getattr(s, "rejoins", 0) for s in slots)
        log.info("=" * 78)
        log.info("RESULT: joined=%s/%s   total_rejoins=%s",
                 joined_n, len(slots), total_rejoins)
        for s in slots:
            log.info("  • %-18s joined=%s rejoins=%s",
                     s.name, s.joined, s.rejoins)
        log.info("=" * 78)

        try:
            await browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
