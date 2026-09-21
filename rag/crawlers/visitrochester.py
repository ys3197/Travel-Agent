"""
visitrochester.com 爬虫 — Rochester 官方旅游局景点介绍

策略：
  1. 抓景点/活动列表页，提取各景点详情链接
  2. 抓每个景点详情页，提取名称 + 描述 + 实用信息
  3. 格式化为 JSONL 存入 RAG

用法:
    python -m rag.crawlers.visitrochester
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

BASE = "https://www.visitrochester.com"
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "raw"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = OUTPUT_DIR / "visitrochester.jsonl"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_stealth = Stealth()

# 景点/活动分类列表页
LIST_PAGES = [
    "/things-to-do/attractions/",
    "/things-to-do/arts-culture/",
    "/things-to-do/outdoors-nature/",
    "/things-to-do/wineries-breweries/",
    "/things-to-do/family-fun/",
    "/things-to-do/",
]

# 同时直接加入一些已知重要景点页
DIRECT_PAGES = [
    "/listing/high-falls/",
    "/listing/strong-national-museum-of-play/",
    "/listing/george-eastman-museum/",
    "/listing/letchworth-state-park/",
    "/listing/maplewood-park/",
    "/listing/seneca-park-zoo/",
    "/listing/rochester-public-market/",
    "/listing/finger-lakes/",
]


async def get(page, path: str) -> str | None:
    url = path if path.startswith("http") else BASE + path
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="networkidle", timeout=25000)
            return await page.content()
        except Exception as e:
            logger.warning(f"Attempt {attempt+1} failed for {url}: {e}")
        await asyncio.sleep(random.uniform(1, 3))
    return None


def extract_listing_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.select("a[href*='/listing/']"):
        href = a["href"].split("?")[0]
        # 统一成以 /listing/ 开头的相对路径
        if BASE in href:
            href = href.replace(BASE, "")
        if href.startswith("/listing/") and len(href) > len("/listing/"):
            links.add(href)
    return list(links)


def parse_listing(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    # 名称
    title_el = soup.select_one("h1") or soup.select_one(".listing-title")
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None

    # 正文：取所有有意义的 <p> 和 <li>
    skip_phrases = {"your browser is not supported", "we recommend using"}
    desc_parts = []
    for el in soup.select("p, li"):
        text = el.get_text(separator=" ", strip=True)
        if len(text) < 40:
            continue
        if any(s in text.lower() for s in skip_phrases):
            continue
        desc_parts.append(text)

    raw_text = "\n\n".join(desc_parts)
    if len(raw_text) < 80:
        return None

    # 标签推断
    text_lower = raw_text.lower()
    tags = []
    if any(w in text_lower for w in ["park", "trail", "nature", "waterfall", "outdoor"]):
        tags.append("nature")
    if any(w in text_lower for w in ["winery", "wine", "brewery", "beer", "tasting"]):
        tags.append("winery")
    if any(w in text_lower for w in ["museum", "history", "historic", "art", "gallery"]):
        tags.append("history")
    if any(w in text_lower for w in ["family", "kids", "children", "playground"]):
        tags.append("family")
    if any(w in text_lower for w in ["free", "no admission", "no charge"]):
        tags.append("free")

    # 费用线索
    price_match = re.search(r"\$\s*(\d+)", raw_text)
    budget_hint = int(price_match.group(1)) * 7 if price_match else -1  # 换算为人民币估算

    full_url = url if url.startswith("http") else BASE + url
    return {
        "source": "visitrochester",
        "url": full_url,
        "title": title,
        "region": "rochester_ny",
        "tags": tags,
        "budget_hint": budget_hint,
        "duration_days": 1,
        "raw_text": f"{title}\n\n{raw_text}",
        "crawled_at": datetime.utcnow().isoformat(),
        "attractions": [],
        "daily_schedule": [],
    }


async def crawl(max_pages: int = 60):
    seen_urls: set[str] = set()
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH) as f:
            for line in f:
                try:
                    seen_urls.add(json.loads(line)["url"])
                except Exception:
                    pass
        logger.info(f"Resuming: {len(seen_urls)} already saved")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=UA, locale="en-US")
        page = await context.new_page()
        await _stealth.apply_stealth_async(page)

        # 1. 从列表页收集 listing 链接
        all_links: list[str] = list(DIRECT_PAGES)
        for list_path in LIST_PAGES:
            logger.info(f"List page: {list_path}")
            html = await get(page, list_path)
            if html:
                links = extract_listing_links(html)
                new = [l for l in links if BASE + l not in seen_urls]
                all_links.extend(new)
                logger.info(f"  +{len(new)} links (total {len(all_links)})")
            await asyncio.sleep(random.uniform(2, 4))

        all_links = list(dict.fromkeys(all_links))
        logger.info(f"Total listing pages to crawl: {len(all_links)}")

        # 2. 抓每个 listing 详情
        collected = 0
        with open(OUTPUT_PATH, "a", encoding="utf-8") as out:
            for path in all_links:
                if collected >= max_pages:
                    break
                full_url = BASE + path if not path.startswith("http") else path
                if full_url in seen_urls:
                    continue

                logger.info(f"[{collected+1}] {path}")
                html = await get(page, path)
                if not html:
                    continue

                note = parse_listing(html, path)
                if note:
                    out.write(json.dumps(note, ensure_ascii=False) + "\n")
                    out.flush()
                    seen_urls.add(full_url)
                    collected += 1
                    logger.info(f"  Saved: {note['title']}")
                else:
                    logger.warning(f"  No usable content")

                await asyncio.sleep(random.uniform(2, 4))

        await browser.close()

    logger.info(f"Done. Total saved: {collected}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(crawl(max_pages=60))
