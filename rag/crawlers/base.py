import asyncio
import random
import logging
from typing import Any

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


async def random_delay(min_s: float = 2.0, max_s: float = 5.0):
    delay = random.uniform(min_s, max_s)
    await asyncio.sleep(delay)


def random_ua() -> str:
    return random.choice(USER_AGENTS)


async def safe_goto(page: Any, url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return
        except Exception as e:
            logger.warning(f"Attempt {attempt+1} failed for {url}: {e}")
            if attempt < retries - 1:
                await random_delay(3, 7)
    raise RuntimeError(f"Failed to load {url} after {retries} attempts")
