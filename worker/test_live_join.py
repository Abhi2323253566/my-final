"""Standalone live-meeting smoke test.

Boots ONE Playwright bot, joins the user's real Zoom meeting with
audio/video strictly OFF, runs the reactions loop for HOLD_SECONDS,
then cleans up.

Usage:
    python /app/worker/test_live_join.py
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Force-config the worker BEFORE we import it so env vars take effect.
os.environ.setdefault("HEADLESS", "true")
os.environ.setdefault("REACTIONS_ENABLED", "true")
os.environ.setdefault("JOIN_WITH_AUDIO_MUTED", "true")
os.environ.setdefault("JOIN_WITH_VIDEO_OFF", "true")
os.environ.setdefault("STRICT_ANTI_LEAVE", "true")
os.environ.setdefault("REACTION_INTERVAL_MIN", "12")
os.environ.setdefault("REACTION_INTERVAL_MAX", "25")
os.environ.setdefault("PERSISTENT_CACHE", "false")
os.environ.setdefault("DEBUG_DUMP_DOM", "true")
# Dummy values so the module-level guard doesn't sys.exit.
os.environ.setdefault("DASHBOARD_URL", "http://localhost:8001")
os.environ.setdefault("WORKER_TOKEN", "livetest-dummy")

import zoom_worker_pool as zwp  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("livetest")

MEETING_ID = os.environ.get("TEST_MEETING_ID", "81023933289")
PASSWORD = os.environ.get("TEST_MEETING_PASSWORD", "145700")
BOT_NAME = os.environ.get("TEST_BOT_NAME", "Aarav Sharma")
HOLD_SECONDS = int(os.environ.get("TEST_HOLD_SECONDS", "240"))


async def main():
    log.info("=" * 70)
    log.info("ULTRA PRO LIVE TEST — joining %s as %s for %ss",
             MEETING_ID, BOT_NAME, HOLD_SECONDS)
    log.info("HEADLESS=%s REACTIONS=%s A/V=OFF STRICT_ANTI_LEAVE=%s",
             zwp.HEADLESS, zwp.REACTIONS_ENABLED, zwp.STRICT_ANTI_LEAVE)
    log.info("=" * 70)

    async with zwp.async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=zwp.HEADLESS,
            args=zwp.CHROMIUM_ARGS,
        )
        browser_slot = zwp.BrowserSlot(browser=browser)

        slot = zwp.BotSlot(name=BOT_NAME, task_id="livetest")
        browser_slot.bots[slot.name] = slot

        task_cfg = {
            "participant_reactions": True,
            "floating_emoji": True,
            "reaction_interval_min": int(os.environ["REACTION_INTERVAL_MIN"]),
            "reaction_interval_max": int(os.environ["REACTION_INTERVAL_MAX"]),
        }

        ok = await zwp.run_bot(
            slot=slot,
            browser_slot=browser_slot,
            meeting_id=MEETING_ID,
            password=PASSWORD,
            hold_seconds=HOLD_SECONDS,
            pool=None,
            task_cfg=task_cfg,
        )
        log.info("run_bot returned: %s  (joined=%s, rejoins=%s)",
                 ok, slot.joined, slot.rejoins)

        try:
            await browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
