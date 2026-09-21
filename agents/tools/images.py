"""
Wikipedia images — 为景点获取多张真实图片 URL。
使用 MediaWiki API generator=images，一次请求返回多图。
"""

import re
import httpx
from cache import result_cache

WIKI_API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "RochesterTravelAgent/1.0 (educational project)"}

_TITLE_MAP = {
    "High Falls Rochester":   "High Falls (Rochester, New York)",
    "Keuka Lake Wineries":    "Keuka Lake",
    "Seneca Lake Wine Trail": "Seneca Lake",
    "Taughannock Falls":      "Taughannock Falls State Park",
}

# 跳过这些文件名关键词（图标/旗帜/地图/模板图片）
_SKIP_PATTERNS = re.compile(
    r"(logo|flag|icon|map|locator|stub|commons|wikip|edit|button|"
    r"\.svg$|red_pog|blue_pog|cscr|featured)",
    re.I,
)


def get_attraction_images(name: str, count: int = 3, thumb_px: int = 400) -> list[str]:
    """返回景点最多 count 张 Wikipedia 图片 URL 列表。"""
    cached = result_cache.get("wiki_imgs", name)
    if cached is not None:
        return cached

    wiki_title = _TITLE_MAP.get(name, name)
    urls: list[str] = []
    try:
        resp = httpx.get(
            WIKI_API,
            params={
                "action":    "query",
                "generator": "images",
                "titles":    wiki_title,
                "prop":      "imageinfo",
                "iiprop":    "url|size",
                "iiurlwidth": thumb_px,
                "gimlimit":  20,
                "format":    "json",
                "redirects": 1,
            },
            headers=HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            title = page.get("title", "")
            if _SKIP_PATTERNS.search(title):
                continue
            info = (page.get("imageinfo") or [{}])[0]
            # 跳过太小的图（宽或高 < 200px）
            if info.get("width", 0) < 200 or info.get("height", 0) < 200:
                continue
            url = info.get("thumburl") or info.get("url")
            if url:
                urls.append(url)
            if len(urls) >= count:
                break
    except Exception:
        pass

    result_cache.set("wiki_imgs", name, value=urls, ttl_sec=86400)
    return urls


def get_attraction_image(name: str) -> str | None:
    """向后兼容：返回第一张图，无图返回 None。"""
    imgs = get_attraction_images(name, count=1)
    return imgs[0] if imgs else None
