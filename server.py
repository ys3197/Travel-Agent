"""
Rochester Travel Agent — FastAPI + SSE backend
启动: uvicorn server:app --host 0.0.0.0 --port 8080
"""
import asyncio
import json
import logging
import sys
from datetime import date as _date
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))

from pipeline import run_pipeline
from user_profile import (
    FitnessLevel, GroupType, Interest, Transport, TravelStyle, UserProfile,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

STATIC = Path(__file__).parent / "static"

TRANSPORT_MAP = {"自驾": Transport.OWN_CAR, "租车": Transport.RENTAL_CAR,
                 "Uber/Lyft": Transport.UBER_LYFT, "公共交通": Transport.PUBLIC}
STYLE_MAP    = {"穷游": TravelStyle.BUDGET, "适中": TravelStyle.MIDRANGE, "精致": TravelStyle.LUXURY}
GROUP_MAP    = {"独自": GroupType.SOLO, "两人": GroupType.COUPLE,
                "家庭（含小孩）": GroupType.FAMILY, "朋友/同学": GroupType.FRIENDS}
GROUP_SIZE   = {"独自": 1, "两人": 2, "家庭（含小孩）": 3, "朋友/同学": 4}
FITNESS_MAP  = {"轻松型": FitnessLevel.EASY, "普通型": FitnessLevel.MODERATE, "活跃型": FitnessLevel.ACTIVE}
INTEREST_MAP = {"自然风光": Interest.NATURE, "酒庄品酒": Interest.WINERY,
                "本地美食": Interest.FOOD,  "历史文化": Interest.HISTORY,
                "艺术展览": Interest.ARTS,  "户外运动": Interest.OUTDOOR}


class PlanRequest(BaseModel):
    date: str = ""
    transport: str = "自驾"
    style: str = "适中"
    group: str = "独自"
    fitness: str = "普通型"
    interests: list[str] = ["自然风光", "本地美食"]
    budget: int = 80
    start_time: str = "09:00"
    end_time: str = "19:00"
    departure: str = "University of Rochester"
    user_input: str = ""
    special: str = ""
    has_kids: bool = False
    kids_age: int | None = None


@app.get("/")
async def index():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.post("/api/plan")
async def api_plan(req: PlanRequest):
    trip_date = req.date or _date.today().strftime("%Y-%m-%d")

    profile = UserProfile(
        transport=TRANSPORT_MAP.get(req.transport, Transport.OWN_CAR),
        style=STYLE_MAP.get(req.style, TravelStyle.MIDRANGE),
        group=GROUP_MAP.get(req.group, GroupType.SOLO),
        group_size=GROUP_SIZE.get(req.group, 1),
        fitness=FITNESS_MAP.get(req.fitness, FitnessLevel.MODERATE),
        interests=[INTEREST_MAP[i] for i in req.interests if i in INTEREST_MAP],
        budget_usd=float(req.budget),
        start_time=req.start_time,
        end_time=req.end_time,
        has_kids=req.has_kids,
        kids_age_min=req.kids_age,
        accessible="轮椅" in req.special,
        pet_friendly="宠物" in req.special,
        departure=req.departure,
        notes=req.user_input,
    )

    queue: asyncio.Queue = asyncio.Queue()

    async def run_and_enqueue():
        try:
            result = await run_pipeline(
                profile,
                req.user_input or "帮我规划一天的行程",
                on_status=lambda m: queue.put_nowait(("status", m)),
                on_token=lambda _: None,
                date=trip_date,
            )
            # 附加图片 URL
            if isinstance(result.itinerary, dict):
                from agents.tools.images import get_attraction_image
                for act in result.itinerary.get("activities", []):
                    if act.get("category") not in ("departure", "food"):
                        act["image_url"] = get_attraction_image(act["name"]) or ""

            # 附加可下载的 markdown 文本
            from agents.planner import format_itinerary
            md = format_itinerary(result.itinerary) if isinstance(result.itinerary, dict) else (result.itinerary or "")

            queue.put_nowait(("result", {
                "itinerary": result.itinerary,
                "warnings": result.warnings,
                "latency_sec": result.latency_sec,
                "intent": result.intent.value,
                "markdown": md,
            }))
        except Exception as e:
            logger.exception("Pipeline error")
            queue.put_nowait(("error", str(e)))
        finally:
            queue.put_nowait(None)

    async def generate():
        asyncio.create_task(run_and_enqueue())
        while True:
            item = await queue.get()
            if item is None:
                break
            kind, data = item
            yield f"data: {json.dumps({'type': kind, 'data': data}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
