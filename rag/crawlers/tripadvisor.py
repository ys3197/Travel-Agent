"""
TripAdvisor 景点评论爬虫 — Rochester & Finger Lakes 地区

策略：
  1. 抓 Rochester / Finger Lakes 景点列表页
  2. 每个景点取前 2 页评论（约 20 条）
  3. 合并成一篇"游记"存入 JSONL

用法:
    python -m rag.crawlers.tripadvisor
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

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "raw"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = OUTPUT_DIR / "tripadvisor_rochester.jsonl"

BASE = "https://www.tripadvisor.com"

_stealth = Stealth(navigator_languages_override=("en-US", "en"))

# Rochester 及周边景点页 URL（直接定位，跳过搜索）
ATTRACTION_LIST_URLS = [
    # Rochester 市区景点
    "/Attractions-g60894-Activities-Rochester_New_York.html",
    # Finger Lakes 地区
    "/Attractions-g47059-Activities-Finger_Lakes_Region_New_York.html",
    # Letchworth State Park（单独景点，评论多）
    "/Attraction_Review-g48307-d110003-Reviews-Letchworth_State_Park-Castile_New_York.html",
    # Watkins Glen State Park
    "/Attraction_Review-g48551-d143757-Reviews-Watkins_Glen_State_Park-Watkins_Glen_New_York.html",
    # High Falls Rochester
    "/Attraction_Review-g60894-d209673-Reviews-High_Falls-Rochester_New_York.html",
    # Strong National Museum of Play
    "/Attraction_Review-g60894-d104080-Reviews-Strong_National_Museum_of_Play-Rochester_New_York.html",
    # George Eastman Museum
    "/Attraction_Review-g60894-d276820-Reviews-George_Eastman_Museum-Rochester_New_York.html",
    # Seneca Lake wine trail
    "/Attraction_Review-g47059-d2216070-Reviews-Seneca_Lake_Wine_Trail-Finger_Lakes_Region_New_York.html",
]


async def make_page(context):
    page = await context.new_page()
    await _stealth.apply_stealth_async(page)
    return page


async def get(page, url: str) -> str | None:
    full = url if url.startswith("http") else BASE + url
    for attempt in range(3):
        try:
            await page.goto(full, wait_until="networkidle", timeout=30000)
            # dismiss cookie banner if present
            try:
                await page.click("button#onetrust-accept-btn-handler", timeout=3000)
            except Exception:
                pass
            return await page.content()
        except Exception as e:
            logger.warning(f"Attempt {attempt+1} failed for {full}: {e}")
        await asyncio.sleep(random.uniform(2, 4))
    return None


def parse_attraction_links(html: str) -> list[str]:
    """从景点列表页提取各景点详情页 URL。"""
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.select("a[href*='Attraction_Review']"):
        href = a.get("href", "")
        # 去掉 query string，取干净路径
        clean = href.split("?")[0].split("#")[0]
        if clean and "/Attraction_Review-" in clean:
            links.add(clean)
    return list(links)


def parse_reviews(html: str) -> tuple[str, list[str], str]:
    """
    从景点页提取：标题、评论文本列表、标签。
    返回 (attraction_name, reviews, tags_str)
    """
    soup = BeautifulSoup(html, "html.parser")

    # 景点名称
    name_el = (
        soup.select_one("h1[data-automation='mainH1']") or
        soup.select_one("h1.HjBfq") or
        soup.select_one("h1")
    )
    name = name_el.get_text(strip=True) if name_el else "Unknown Attraction"

    # 评论：尝试多个 selector（TripAdvisor 经常改 class）
    review_els = (
        soup.select("div[data-automation='reviewCard'] .biGQs") or
        soup.select(".review-container .entry .partial_entry") or
        soup.select("div.review-container q") or
        soup.select("span[data-automation='reviewText']") or
        soup.select(".yCeTE") or
        soup.select("._T.FKffI span") or
        soup.select("div.fIrGe._T")
    )

    reviews = []
    for el in review_els:
        text = el.get_text(separator=" ", strip=True)
        if len(text) > 50:
            reviews.append(text)

    # 景点类别标签
    tag_els = soup.select(".DtcWZ span") or soup.select(".category-label")
    tags = [t.get_text(strip=True) for t in tag_els if t.get_text(strip=True)]

    return name, reviews, ", ".join(tags)


def reviews_to_note(attraction_name: str, reviews: list[str], tags: str, url: str) -> dict:
    """把一个景点的所有评论合并成一条游记 JSONL 记录。"""
    combined = "\n\n".join(f"[Review {i+1}] {r}" for i, r in enumerate(reviews))
    raw_text = f"{attraction_name}\n\n{combined}"

    # 从评论里猜预算（ticket price 等）
    budget_match = re.search(r"\$(\d+)", combined)
    budget_hint = int(budget_match.group(1)) * 7 if budget_match else None  # 换算成人民币估算

    # 推断标签
    tag_list = []
    text_lower = combined.lower()
    if any(w in text_lower for w in ["family", "kids", "children"]):
        tag_list.append("亲子")
    if any(w in text_lower for w in ["hike", "trail", "outdoor"]):
        tag_list.append("户外")
    if any(w in text_lower for w in ["wine", "winery", "tasting"]):
        tag_list.append("酒庄")
    if any(w in text_lower for w in ["free", "no charge", "no admission"]):
        tag_list.append("免费")

    return {
        "source": "tripadvisor",
        "url": BASE + url if not url.startswith("http") else url,
        "title": f"{attraction_name} — Visitor Reviews",
        "region": "rochester_ny",
        "tags": tag_list,
        "budget_hint": budget_hint,
        "duration_days": 1,
        "raw_text": raw_text,
        "crawled_at": datetime.utcnow().isoformat(),
        "attractions": [],
        "daily_schedule": [],
    }


def review_next_page(url: str, page: int) -> str:
    """生成 TripAdvisor 评论翻页 URL（每页10条，offset = page*10）。"""
    # URL 格式：.../Reviews-gXXX.html → .../Reviews-or10-gXXX.html
    if "-Reviews-" in url:
        return re.sub(r"-Reviews(-or\d+)?-", f"-Reviews-or{page * 10}-", url)
    return url


async def crawl_attraction(page, url: str, max_review_pages: int = 2) -> dict | None:
    """抓一个景点的多页评论，合并成一条记录。"""
    all_reviews = []
    name = ""
    tags = ""

    for pg in range(max_review_pages):
        page_url = review_next_page(url, pg) if pg > 0 else url
        html = await get(page, page_url)
        if not html:
            break

        n, reviews, t = parse_reviews(html)
        if pg == 0:
            name = n
            tags = t
        all_reviews.extend(reviews)

        if not reviews:
            break
        await asyncio.sleep(random.uniform(2, 4))

    if not name or not all_reviews:
        logger.warning(f"No content from {url}")
        return None

    logger.info(f"  {name}: {len(all_reviews)} reviews")
    return reviews_to_note(name, all_reviews, tags, url)


async def crawl(max_attractions: int = 40):
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
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        page = await make_page(context)

        # 1. 收集景点 URL
        attraction_urls: list[str] = []
        for list_url in ATTRACTION_LIST_URLS:
            if "Attraction_Review" in list_url:
                if BASE + list_url not in seen_urls:
                    attraction_urls.append(list_url)
                continue

            logger.info(f"Fetching list: {list_url}")
            html = await get(page, list_url)
            if html:
                links = parse_attraction_links(html)
                new = [l for l in links if BASE + l not in seen_urls]
                attraction_urls.extend(new)
                logger.info(f"  Found {len(new)} new links")
            await asyncio.sleep(random.uniform(3, 5))

        attraction_urls = list(dict.fromkeys(attraction_urls))
        logger.info(f"Total to crawl: {len(attraction_urls)}")

        # 2. 抓评论
        collected = 0
        with open(OUTPUT_PATH, "a", encoding="utf-8") as out:
            for url in attraction_urls:
                if collected >= max_attractions:
                    break
                logger.info(f"[{collected+1}/{len(attraction_urls)}] {url}")
                note = await crawl_attraction(page, url)
                if note:
                    out.write(json.dumps(note, ensure_ascii=False) + "\n")
                    out.flush()
                    seen_urls.add(note["url"])
                    collected += 1
                await asyncio.sleep(random.uniform(4, 8))

        await browser.close()

    logger.info(f"Done. Total saved: {collected}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(crawl(max_attractions=40))
