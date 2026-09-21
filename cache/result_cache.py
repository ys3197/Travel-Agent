"""
简单 TTL 内存缓存，用于 POI / 天气查询结果。
key: (query_type, city, date) → value: 任意结果
"""

import time
from typing import Any

_store: dict[str, tuple[Any, float]] = {}


def _key(*parts) -> str:
    return "|".join(str(p) for p in parts)


def get(query_type: str, *args) -> Any | None:
    k = _key(query_type, *args)
    if k in _store:
        value, expires_at = _store[k]
        if time.time() < expires_at:
            return value
        del _store[k]
    return None


def set(query_type: str, *args, value: Any, ttl_sec: int = 3600):
    k = _key(query_type, *args)
    _store[k] = (value, time.time() + ttl_sec)


def clear():
    _store.clear()
