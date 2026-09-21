"""
Wikipedia 景点爬虫 — Rochester & Finger Lakes 地区

用 Wikipedia REST API 抓取景点条目正文，无需登录，无限速限制（合理使用）。

用法:
    python -m rag.crawlers.wikipedia
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "raw"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = OUTPUT_DIR / "wikipedia_rochester.jsonl"

# Wikipedia API — 获取条目完整正文（纯文本）
WIKI_API = "https://en.wikipedia.org/w/api.php"

# POI 工具里的景点 + 周边重要地点
TARGETS = [
    "Letchworth State Park",
    "Watkins Glen State Park",
    "George Eastman Museum",
    "Strong National Museum of Play",
    "High Falls (Rochester)",
    "Ontario Beach Park",
    "Seneca Lake",
    "Keuka Lake",
    "Taughannock Falls State Park",
    "Rochester Public Market",
    "Genesee Valley Park",
    "Bristol Mountain",
    "Canandaigua Lake",
    "Corning Museum of Glass",
    "Finger Lakes",
    "Finger Lakes wine region",
    "Rochester, New York",
    "Charlotte, Rochester, New York",  # Charlotte beach 区域
]

HEADERS = {"User-Agent": "RochesterTravelAgent/1.0 (educational project; contact: student@rochester.edu)"}


async def fetch_article(client: httpx.AsyncClient, title: str) -> dict | None:
    """用 MediaWiki API 抓取条目纯文本正文。"""
    params = {
        "action": "query",
        "titles": title,
        "prop": "extracts",
        "explaintext": True,   # 纯文本，去掉 wiki markup
        "exsectionformat": "plain",
        "format": "json",
        "redirects": 1,        # 自动跟随重定向
    }
    try:
        r = await client.get(WIKI_API, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"API error for '{title}': {e}")
        return None

    pages = data.get("query", {}).get("pages", {})
    page = next(iter(pages.values()))

    if "missing" in page:
        logger.warning(f"Page not found: '{title}'")
        return None

    actual_title = page.get("title", title)
    text = page.get("extract", "").strip()

    if len(text) < 200:
        logger.warning(f"Too short ({len(text)} chars): '{actual_title}'")
        return None

    return {"title": actual_title, "text": text}


def classify_tags(title: str, text: str) -> list[str]:
    t = (title + " " + text).lower()
    tags = []
    if any(w in t for w in ["park", "waterfall", "lake", "trail", "gorge", "falls", "nature"]):
        tags.append("nature")
    if any(w in t for w in ["winery", "wine", "vineyard", "tasting", "brewery"]):
        tags.append("winery")
    if any(w in t for w in ["museum", "history", "historic", "heritage", "eastman", "kodak"]):
        tags.append("history")
    if any(w in t for w in ["family", "children", "playground", "zoo"]):
        tags.append("family")
    if any(w in t for w in ["hike", "hiking", "trail", "outdoor", "skiing", "kayak"]):
        tags.append("outdoor")
    return tags


def extract_budget(text: str) -> int:
    """从正文里找门票/停车费（USD），换算成大致人民币。"""
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)\s*(?:per|admission|fee|ticket|vehicle)", text, re.I)
    if m:
        usd = float(m.group(1))
        return int(usd * 7)
    return -1


async def crawl():
    seen: set[str] = set()
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH) as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["url"])
                except Exception:
                    pass
        logger.info(f"Resuming: {len(seen)} already saved")

    async with httpx.AsyncClient(headers=HEADERS) as client:
        collected = 0
        with open(OUTPUT_PATH, "a", encoding="utf-8") as out:
            for title in TARGETS:
                url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
                if url in seen:
                    logger.info(f"Skip (seen): {title}")
                    continue

                logger.info(f"Fetching: {title}")
                article = await fetch_article(client, title)
                if not article:
                    continue

                text = article["text"]
                actual_title = article["title"]
                actual_url = f"https://en.wikipedia.org/wiki/{actual_title.replace(' ', '_')}"

                record = {
                    "source": "wikipedia",
                    "url": actual_url,
                    "title": actual_title,
                    "region": "rochester_ny",
                    "tags": classify_tags(actual_title, text),
                    "budget_hint": extract_budget(text),
                    "duration_days": 1,
                    "raw_text": f"{actual_title}\n\n{text}",
                    "crawled_at": datetime.utcnow().isoformat(),
                    "attractions": [],
                    "daily_schedule": [],
                }

                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                seen.add(actual_url)
                collected += 1
                logger.info(f"  Saved: {actual_title} ({len(text)} chars, tags={record['tags']})")

                await asyncio.sleep(0.5)  # Wikipedia 建议请求间隔

    logger.info(f"Done. Total saved: {collected}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(crawl())
