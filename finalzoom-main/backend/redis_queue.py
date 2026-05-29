"""
Lightweight Redis layer for the scheduler:
  - online-worker counters (currently in-memory + 5s TTL)
  - per-task claim hot-cache (avoid hammering Mongo when 30+ workers poll)
  - distributed lock for stale-worker auto-recovery sweep

If REDIS_URL is unset OR Redis is unreachable, EVERYTHING gracefully degrades to
in-memory only — the rest of the system keeps working.
"""
from __future__ import annotations
import os
import time
import logging
from typing import Optional

log = logging.getLogger("redis-queue")

try:
    import redis.asyncio as aioredis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

_client: Optional["aioredis.Redis"] = None
_disabled_until: float = 0.0


async def get_redis() -> Optional["aioredis.Redis"]:
    """Return a singleton aioredis client. None if unreachable or disabled."""
    global _client, _disabled_until
    if not _REDIS_AVAILABLE:
        return None
    if time.time() < _disabled_until:
        return None
    if _client is not None:
        return _client
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        _client = aioredis.from_url(url, decode_responses=True, socket_timeout=2)
        await _client.ping()
        log.info("redis: connected (%s)", url)
        return _client
    except Exception as e:
        log.warning("redis unreachable, disabling for 60s: %s", e)
        _client = None
        _disabled_until = time.time() + 60
        return None


async def cache_set(key: str, value: str, ex: int = 5) -> None:
    r = await get_redis()
    if not r: return
    try: await r.set(key, value, ex=ex)
    except Exception: pass


async def cache_get(key: str) -> Optional[str]:
    r = await get_redis()
    if not r: return None
    try: return await r.get(key)
    except Exception: return None


async def try_lock(key: str, ttl: int = 30) -> bool:
    """Distributed lock. True if we acquired."""
    r = await get_redis()
    if not r: return True  # no redis → degrade to "always own the lock"
    try:
        return bool(await r.set(key, "1", nx=True, ex=ttl))
    except Exception:
        return True


async def release_lock(key: str) -> None:
    r = await get_redis()
    if not r: return
    try: await r.delete(key)
    except Exception: pass
