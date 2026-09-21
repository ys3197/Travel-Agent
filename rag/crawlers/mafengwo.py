"""
马蜂窝游记爬虫 — 北美纽约上州地区
反检测：playwright-stealth + 中文 locale + 预热会话 + 随机行为

用法:
    python -m rag.crawlers.mafengwo
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

_stealth = Stealth(
    navigator_languages_override=("zh-CN", "zh"),
    navigator_platform_override="Win32",
)

from rag.crawlers.base import random_delay

logger = logging.getLogger(__name__)

SEARCH_QUERIES = [
    "罗切斯特 纽约",
    "手指湖",
    "莱奇沃斯",
    "沃特金斯峡谷",
    "伊萨卡 康奈尔",
    "康宁玻璃博物馆",
    "尼亚加拉瀑布",
    "纽约上州",
]

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "raw"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 模拟真实 Chrome 126 指纹
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


async def make_stealth_context(playwright):
    """创建带反检测措施的浏览器 context。"""
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-web-security",
            "--lang=zh-CN",
        ],
    )
    context = await browser.new_context(
        user_agent=CHROME_UA,
        viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        extra_http_headers={
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Referer": "https://www.mafengwo.cn/",
        },
    )
    return browser, context


async def warmup_session(page):
    """访问首页预热 cookie / session，模拟真实用户。"""
    logger.info("Warming up session via homepage...")
    try:
        await page.goto("https://www.mafengwo.cn/", wait_until="networkidle", timeout=20000)
        await random_delay(2, 4)
        # 模拟滚动
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.5)")
        await random_delay(1, 2)
    except Exception as e:
        logger.warning(f"Warmup failed (non-fatal): {e}")


def is_waf_blocked(html: str) -> bool:
    return "WAF拦截" in html or "probev3.js" in html or "腾讯云WAF" in html


async def fetch_note_list(page, query: str, max_pages: int = 3) -> list[str]:
    urls = []
    for page_num in range(1, max_pages + 1):
        search_url = (
            f"https://www.mafengwo.cn/search/q.php"
            f"?q={query}&type=note&p={page_num}"
        )
        logger.info(f"  Searching: {search_url}")

        try:
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            logger.warning(f"Navigation failed: {e}")
            break

        # 等待内容出现（JS 渲染）
        try:
            await page.wait_for_selector(
                ".search-list-wrap, ._j_search_item, .notes-list, li.item",
                timeout=10000,
            )
        except Exception:
            pass  # 后面通过 HTML 判断

        await random_delay(1, 2)
        await page.evaluate("window.scrollBy(0, 300)")
        await random_delay(0.5, 1)

        html = await page.content()

        if is_waf_blocked(html):
            logger.error("WAF blocked — stopping this query")
            debug = OUTPUT_DIR.parent / f"debug_waf_{query[:8]}.html"
            debug.write_text(html, encoding="utf-8")
            break

        soup = BeautifulSoup(html, "html.parser")

        # 尝试多个 selector（马蜂窝改版后结构可能变化）
        items = (
            soup.select(".search-list-wrap .item") or
            soup.select("._j_search_item") or
            soup.select(".notes-list .item") or
            soup.select("li.item") or
            soup.select("[class*='note-item']") or
            soup.select("[class*='search-item']")
        )

        if not items:
            logger.warning(f"No items for '{query}' p{page_num} — saving debug HTML")
            debug = OUTPUT_DIR.parent / f"debug_empty_{query[:8]}_p{page_num}.html"
            debug.write_text(html, encoding="utf-8")
            break

        for item in items:
            link = item.select_one("a[href*='/i/']") or item.select_one("a[href]")
            if link and link.get("href"):
                href = link["href"]
                if href.startswith("/i/") or "/i/" in href:
                    if not href.startswith("http"):
                        href = "https://www.mafengwo.cn" + href
                    if href not in urls:
                        urls.append(href)

        logger.info(f"  '{query}' p{page_num}: {len(items)} items, {len(urls)} URLs so far")
        await random_delay(3, 6)

    return urls


async def fetch_note_detail(page, url: str) -> dict | None:
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as e:
        logger.warning(f"Failed to load {url}: {e}")
        return None

    await random_delay(1.5, 3)
    await page.evaluate("window.scrollBy(0, 400)")
    await random_delay(0.5, 1)

    html = await page.content()

    if is_waf_blocked(html):
        logger.error(f"WAF blocked on detail page: {url}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    title_el = (
        soup.select_one("h1.title") or
        soup.select_one(".head .title") or
        soup.select_one("h1")
    )
    title = title_el.get_text(strip=True) if title_el else ""

    content_el = (
        soup.select_one(".content") or
        soup.select_one(".article-content") or
        soup.select_one(".post-content")
    )
    raw_text = content_el.get_text(separator="\n", strip=True) if content_el else ""

    if not title or len(raw_text) < 100:
        logger.warning(f"Insufficient content at {url} (title={bool(title)}, text_len={len(raw_text)})")
        return None

    tag_els = soup.select(".tag-list .tag") or soup.select(".tags span")
    tags = [t.get_text(strip=True) for t in tag_els]

    budget_match = re.search(r"人均[\s:：]*(\d+)", raw_text + title)
    budget_hint = int(budget_match.group(1)) if budget_match else None

    days_match = re.search(r"(\d+)\s*天", title + raw_text[:300])
    duration_days = int(days_match.group(1)) if days_match else None

    return {
        "source": "mafengwo",
        "url": url,
        "title": title,
        "region": "rochester_ny",
        "tags": tags,
        "budget_hint": budget_hint,
        "duration_days": duration_days,
        "raw_text": raw_text,
        "crawled_at": datetime.utcnow().isoformat(),
        "attractions": [],
        "daily_schedule": [],
    }


async def crawl(max_notes: int = 80):
    output_path = OUTPUT_DIR / "mafengwo_rochester.jsonl"
    seen_urls: set[str] = set()

    if output_path.exists():
        with open(output_path) as f:
            for line in f:
                try:
                    seen_urls.add(json.loads(line)["url"])
                except Exception:
                    pass
        logger.info(f"Resuming: {len(seen_urls)} notes already crawled")

    async with async_playwright() as p:
        browser, context = await make_stealth_context(p)
        page = await context.new_page()

        # 每个页面都注入 stealth patch
        await _stealth.apply_stealth_async(page)

        # 预热 session
        await warmup_session(page)

        collected = 0
        with open(output_path, "a", encoding="utf-8") as out:
            for query in SEARCH_QUERIES:
                if collected >= max_notes:
                    break

                logger.info(f"=== Query: {query} ===")
                note_urls = await fetch_note_list(page, query, max_pages=3)

                for url in note_urls:
                    if collected >= max_notes:
                        break
                    if url in seen_urls:
                        logger.info(f"  Skip (seen): {url}")
                        continue

                    logger.info(f"  [{collected+1}] {url}")
                    note = await fetch_note_detail(page, url)

                    if note:
                        out.write(json.dumps(note, ensure_ascii=False) + "\n")
                        out.flush()
                        seen_urls.add(url)
                        collected += 1
                        logger.info(f"  Saved: {note['title'][:40]}")

                    # 随机长延迟，避免频率过高
                    await random_delay(3, 7)

                # 每个 query 之间停更久
                await random_delay(5, 10)

        await browser.close()

    logger.info(f"Done. Total collected: {collected}")
    return output_path


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(crawl(max_notes=80))
