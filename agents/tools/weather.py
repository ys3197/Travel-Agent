"""
天气工具 — OpenWeatherMap API
Rochester, NY: lat=43.1566, lon=-77.6088

需要设置环境变量 OPENWEATHER_API_KEY
免费 tier 支持当天天气 + 5日预报，足够用。
"""

import os
from datetime import datetime

import httpx

from cache import result_cache

ROCHESTER_LAT = 43.1566
ROCHESTER_LON = -77.6088
API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
BASE_URL = "https://api.openweathermap.org/data/2.5"


async def get_weather(date: str | None = None) -> dict:
    """
    获取 Rochester 天气。
    date: "2026-08-10" 格式，None 表示今天。
    返回: {temp_c, feels_like_c, description, wind_kph, humidity_pct, is_outdoor_ok}
    """
    today = datetime.now().strftime("%Y-%m-%d")
    target_date = date or today
    cache_key = ("weather", "rochester", target_date)

    cached = result_cache.get(*cache_key)
    if cached:
        return cached

    if not API_KEY:
        return _mock_weather(target_date)

    async with httpx.AsyncClient(timeout=10) as client:
        if target_date == today:
            resp = await client.get(
                f"{BASE_URL}/weather",
                params={"lat": ROCHESTER_LAT, "lon": ROCHESTER_LON,
                        "appid": API_KEY, "units": "metric", "lang": "zh_cn"},
            )
            resp.raise_for_status()
            data = resp.json()
            result = _parse_current(data)
        else:
            # 5日预报，找最接近目标日期的条目
            resp = await client.get(
                f"{BASE_URL}/forecast",
                params={"lat": ROCHESTER_LAT, "lon": ROCHESTER_LON,
                        "appid": API_KEY, "units": "metric", "lang": "zh_cn"},
            )
            resp.raise_for_status()
            data = resp.json()
            result = _parse_forecast(data, target_date)

    result_cache.set(*cache_key, value=result, ttl_sec=1800)
    return result


def _parse_current(data: dict) -> dict:
    temp = data["main"]["temp"]
    desc = data["weather"][0]["description"]
    wind = data["wind"]["speed"] * 3.6
    humidity = data["main"]["humidity"]
    return {
        "temp_c": round(temp, 1),
        "feels_like_c": round(data["main"]["feels_like"], 1),
        "description": desc,
        "wind_kph": round(wind, 1),
        "humidity_pct": humidity,
        "is_outdoor_ok": _outdoor_ok(temp, desc, wind),
        "advisory": _advisory(temp, desc),
    }


def _parse_forecast(data: dict, target_date: str) -> dict:
    for item in data["list"]:
        dt_str = item["dt_txt"][:10]
        if dt_str == target_date:
            temp = item["main"]["temp"]
            desc = item["weather"][0]["description"]
            wind = item["wind"]["speed"] * 3.6
            return {
                "temp_c": round(temp, 1),
                "feels_like_c": round(item["main"]["feels_like"], 1),
                "description": desc,
                "wind_kph": round(wind, 1),
                "humidity_pct": item["main"]["humidity"],
                "is_outdoor_ok": _outdoor_ok(temp, desc, wind),
                "advisory": _advisory(temp, desc),
            }
    return _mock_weather(target_date)


def _outdoor_ok(temp: float, desc: str, wind_kph: float) -> bool:
    bad_weather = any(w in desc for w in ["雷", "暴", "大雨", "暴雪", "大雪"])
    too_cold = temp < -10
    too_hot = temp > 38
    too_windy = wind_kph > 50
    return not (bad_weather or too_cold or too_hot or too_windy)


def _advisory(temp: float, desc: str) -> str:
    notes = []
    if temp < 0:
        notes.append("气温零下，注意保暖，路面可能结冰")
    elif temp < 10:
        notes.append("天气较凉，建议携带外套")
    elif temp > 30:
        notes.append("天气炎热，注意防晒补水")
    if "雨" in desc:
        notes.append("有降雨，建议携带雨具")
    if "雪" in desc:
        notes.append("有降雪，驾驶请注意路况")
    return "；".join(notes) if notes else "天气适宜出行"


def _mock_weather(date: str) -> dict:
    return {
        "temp_c": 22.0,
        "feels_like_c": 21.0,
        "description": "晴转多云",
        "wind_kph": 15.0,
        "humidity_pct": 55,
        "is_outdoor_ok": True,
        "advisory": "天气适宜出行（模拟数据，请设置 OPENWEATHER_API_KEY）",
    }
