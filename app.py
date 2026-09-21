"""
Rochester Travel Agent — Streamlit Interface

用法:
    pip install streamlit
    streamlit run app.py
"""

import asyncio
import streamlit as st

from user_profile import (
    UserProfile, Transport, TravelStyle, GroupType,
    FitnessLevel, Interest,
)
from pipeline import run_pipeline, Intent
from agents.planner import format_itinerary

# ── 页面配置 ──────────────────────────────────────────────

st.set_page_config(
    page_title="Rochester Travel Agent",
    page_icon="🗺️",
    layout="centered",
)

st.title("🗺️ Rochester 一日游规划")
st.caption("纽约上州 · AI 行程规划 · 数据本地化")

# ── Session State 初始化 ──────────────────────────────────

if "result" not in st.session_state:
    st.session_state.result = None
if "existing_plan" not in st.session_state:
    st.session_state.existing_plan = ""

# ── 问卷区 ────────────────────────────────────────────────

with st.form("profile_form"):
    st.subheader("告诉我们你的出行偏好")

    col1, col2 = st.columns(2)

    with col1:
        transport = st.radio(
            "出行方式",
            options=["自驾", "租车", "Uber/Lyft", "公共交通"],
            help="Rochester 郊区景点（手指湖/莱奇沃斯）无公共交通，建议有车出行",
        )
        style = st.radio(
            "消费风格",
            options=["穷游", "适中", "精致"],
        )
        group = st.radio(
            "出行人员",
            options=["独自", "两人", "家庭（含小孩）", "朋友/同学"],
        )

    with col2:
        fitness = st.radio(
            "体力水平",
            options=["轻松型（平路为主）", "普通型（偶尔爬坡）", "活跃型（徒步OK）"],
        )
        interests = st.multiselect(
            "兴趣偏好（可多选）",
            options=["自然风光", "酒庄品酒", "本地美食", "历史文化", "艺术展览", "户外运动"],
            default=["自然风光", "本地美食"],
        )
        budget = st.number_input(
            "全天人均预算（USD）",
            min_value=20, max_value=500, value=80, step=10,
        )

    col3, col4, col5 = st.columns(3)
    with col3:
        from datetime import date as _date
        trip_date = st.date_input("出行日期", value=_date.today())
    with col4:
        start_time = st.time_input("出发时间", value=None)
    with col5:
        end_time = st.time_input("结束时间", value=None)

    has_kids = "家庭" in group
    kids_age = None
    if has_kids:
        kids_age = st.number_input("小孩年龄（最小的）", min_value=1, max_value=17, value=6)

    st.divider()
    user_input = st.text_area(
        "✏️ 用自己的话描述这次旅行",
        placeholder="例如：特别想看瀑布和峡谷，不喜欢太商业化的地方，中午想吃本地特色，下午想轻松一点不要太赶……",
        height=100,
        help="这里写的内容会直接影响行程规划，越具体越好",
    )
    departure = st.text_input(
        "出发地点",
        value="University of Rochester",
        placeholder="如：University of Rochester / Downtown Hotel / Airbnb on East Ave",
        help="填写你出发的地点，帮助计算到第一个景点的交通时间",
    )
    special = st.text_input(
        "特殊需求",
        placeholder="轮椅无障碍 / 携带宠物 / 其他（可不填）",
    )

    submitted = st.form_submit_button("🚀 开始规划", use_container_width=True)

# ── 修改行程区 ────────────────────────────────────────────

if st.session_state.result and st.session_state.result.intent == Intent.FULL_DAY_PLAN:
    with st.expander("✏️ 修改当前行程"):
        modify_input = st.text_input(
            "告诉我想怎么改",
            placeholder="例如：把下午改成去酒庄品酒",
        )
        if st.button("修改行程") and modify_input:
            user_input = modify_input
            submitted = True
            st.session_state.existing_plan = (
                st.session_state.result.itinerary
                if isinstance(st.session_state.result.itinerary, str)
                else format_itinerary(st.session_state.result.itinerary)
            )

# ── 构建 UserProfile & 运行 Pipeline ─────────────────────

TRANSPORT_MAP = {
    "自驾": Transport.OWN_CAR,
    "租车": Transport.RENTAL_CAR,
    "Uber/Lyft": Transport.UBER_LYFT,
    "公共交通": Transport.PUBLIC,
}
STYLE_MAP = {
    "穷游": TravelStyle.BUDGET,
    "适中": TravelStyle.MIDRANGE,
    "精致": TravelStyle.LUXURY,
}
GROUP_MAP = {
    "独自": GroupType.SOLO,
    "两人": GroupType.COUPLE,
    "家庭（含小孩）": GroupType.FAMILY,
    "朋友/同学": GroupType.FRIENDS,
}
GROUP_SIZE_MAP = {
    "独自": 1, "两人": 2,
    "家庭（含小孩）": 3, "朋友/同学": 4,
}
FITNESS_MAP = {
    "轻松型（平路为主）": FitnessLevel.EASY,
    "普通型（偶尔爬坡）": FitnessLevel.MODERATE,
    "活跃型（徒步OK）": FitnessLevel.ACTIVE,
}
INTEREST_MAP = {
    "自然风光": Interest.NATURE,
    "酒庄品酒": Interest.WINERY,
    "本地美食": Interest.FOOD,
    "历史文化": Interest.HISTORY,
    "艺术展览": Interest.ARTS,
    "户外运动": Interest.OUTDOOR,
}

if submitted:
    if not interests:
        st.warning("请至少选择一个兴趣偏好")
        st.stop()

    start_str = start_time.strftime("%H:%M") if start_time else "09:00"
    end_str = end_time.strftime("%H:%M") if end_time else "19:00"

    profile = UserProfile(
        transport=TRANSPORT_MAP[transport],
        style=STYLE_MAP[style],
        group=GROUP_MAP[group],
        group_size=GROUP_SIZE_MAP[group],
        fitness=FITNESS_MAP[fitness],
        interests=[INTEREST_MAP[i] for i in interests],
        budget_usd=float(budget),
        start_time=start_str,
        end_time=end_str,
        has_kids=has_kids,
        kids_age_min=int(kids_age) if kids_age else None,
        accessible="轮椅" in special,
        pet_friendly="宠物" in special,
        departure=departure.strip(),
        notes=user_input,
    )

    final_input = user_input or "帮我规划一天的行程"

    stream_placeholder = st.empty()

    with st.status("规划中…", expanded=True) as status:
        status_placeholder = st.empty()

        def on_status(msg: str):
            status_placeholder.write(msg)

        def on_token(partial: str):
            import re
            names = re.findall(r'"name"\s*:\s*"([^"]+)"', partial)
            if names:
                stream_placeholder.markdown(
                    "**AI 正在撰写说明：**\n" + "\n".join(f"- {n}" for n in names)
                )

        result = asyncio.run(
            run_pipeline(profile, final_input, st.session_state.existing_plan,
                         on_token=on_token, on_status=on_status,
                         date=trip_date.strftime("%Y-%m-%d"))
        )
        status_placeholder.write("✅ 验证行程约束…")
        status.update(label="规划完成！", state="complete")

    stream_placeholder.empty()
    st.session_state.result = result
    st.session_state.existing_plan = ""

# ── 结果展示 ──────────────────────────────────────────────

if st.session_state.result:
    result = st.session_state.result
    st.divider()

    # 延迟与 intent 状态栏
    col_a, col_b = st.columns(2)
    col_a.metric("响应时间", f"{result.latency_sec:.1f}s",
                 delta="⚠️ 超 SLA" if result.latency_sec > 30 else "✅ 正常")
    col_b.metric("规划类型", {
        Intent.FULL_DAY_PLAN:    "完整行程",
        Intent.ATTRACTION_QUERY: "景点查询",
        Intent.BUDGET_CHECK:     "预算检查",
        Intent.MODIFY_PLAN:      "修改行程",
    }.get(result.intent, result.intent.value))

    # 警告
    if result.warnings:
        for w in result.warnings:
            st.warning(w)

    # 行程正文
    st.subheader("📋 行程安排")

    if isinstance(result.itinerary, dict):
        from agents.tools.images import get_attraction_image

        plan = result.itinerary
        activities = plan.get("activities", [])

        for act in activities:
            category = act.get("category", "")
            name = act["name"]
            travel = act.get("travel_min_to_next", 0)
            transport = act.get("transport_to_next", "")
            travel_note = f"↓ {transport}，约 {travel} min" if travel else ""

            # 出发地
            if category == "departure":
                st.markdown(f"### 📍 出发地：{name}")
                if travel_note:
                    st.caption(travel_note)
                st.divider()
                continue

            # 午餐
            if category == "food":
                st.markdown(
                    f"**{act['start_time']}–{act['end_time']}** 🍽️ {name}　"
                    f"${act['cost_usd']:.0f}/人"
                )
                if act.get("notes"):
                    st.caption(act["notes"])
                st.divider()
                continue

            # 景点卡片
            img_url = get_attraction_image(name)
            if img_url:
                col_img, col_txt = st.columns([1, 2])
                with col_img:
                    st.image(img_url, use_container_width=True)
                with col_txt:
                    st.markdown(
                        f"**{act['start_time']}–{act['end_time']}**　{name}\n\n"
                        f"费用：**${act['cost_usd']:.0f}/人**"
                    )
                    if act.get("notes"):
                        st.info(act["notes"])
                    if travel_note:
                        st.caption(travel_note)
            else:
                st.markdown(
                    f"**{act['start_time']}–{act['end_time']}** {name}　"
                    f"${act['cost_usd']:.0f}/人"
                )
                if act.get("notes"):
                    st.info(act["notes"])
                if travel_note:
                    st.caption(travel_note)

            st.divider()

        last_slot = [a for a in activities if a.get("category") != "departure"]
        if last_slot:
            st.markdown(f"🏁 **行程结束：{last_slot[-1]['end_time']}**")

        st.markdown(f"**全天预计费用：${plan.get('total_cost_usd', 0):.0f}/人**")
        if plan.get("summary"):
            st.success(plan["summary"])

        itinerary_text = format_itinerary(plan)
    else:
        itinerary_text = result.itinerary or "暂无结果"
        st.markdown(itinerary_text)

    # 下载按钮
    st.download_button(
        "📥 下载行程",
        data=itinerary_text,
        file_name="rochester_itinerary.md",
        mime="text/markdown",
    )
