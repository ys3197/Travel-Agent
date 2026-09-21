"""
景点名归一化 —— 无重依赖，供 retriever（运行时）和 build_poi_table（离线）共用。

语料按实体组织（每条记录的 title 就是景点名），但不同来源的写法有出入：
'The Strong National Museum of Play' / 'Taughannock Falls State Park' 指的是
poi.py 里的 'Strong National Museum of Play' / 'Taughannock Falls'。
这里只做**确定性**归一化，不做模糊或前缀匹配——对不上就是真的对不上。
"""

# poi.py 的景点名与语料标题对不上时的人工映射（语义父级 / 地理容器）
POI_ALIASES = {
    "keuka lake wineries":    "Keuka Lake",
    "seneca lake wine trail": "Seneca Lake",
    "ontario beach park":     "Charlotte, Rochester, New York",
}

# 归一化时剥掉的尾缀（只剥明确的通用后缀）
_DROP_SUFFIXES = (
    "state park",
    "ski resort",
    "rochester ny",
    "rochester new york",
    "rochester",
)


def normalize_poi(name: str) -> str:
    """'The Strong National Museum of Play' → 'strong national museum of play'"""
    s = name.lower().strip()
    s = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in s)
    s = " ".join(s.split())
    if s.startswith("the "):
        s = s[4:]
    for suf in _DROP_SUFFIXES:
        if s.endswith(" " + suf):
            s = s[: -(len(suf) + 1)].strip()
            break
    return s
