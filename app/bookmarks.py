"""收藏：读到一半想留下的资讯 / 仓库，单独存一份。

为什么不用 daily 缓存：缓存是「今天」的，第二天会被新的一份覆盖。
收藏是跨天的心愿单，得有自己的文件。

为什么用一条 JSON 而不是 jsonl：收藏要能「取消」，jsonl 只能追加。
几十上百条的量级，整体重写一次比维护墓碑标记简单。

id 取 `kind + 链接（没有链接就用标题）` 的哈希：
同一篇今天收藏、明天取消、后天再收藏，id 都一样，不会攒出两条。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path

import store

PATH: Path = store.DATA_DIR / "bookmarks.json"
MAX_ITEMS = 500            # 超出就丢最旧的：这是「心愿单」，不是档案库

_lock = threading.Lock()
_cache: list | None = None
_mtime = 0.0


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def key_for(kind: str, item: dict) -> str:
    """稳定的收藏 id。链接优先 —— 标题会被编辑，链接不会。"""
    raw = str(item.get("url") or item.get("title") or "").strip()
    if not raw:
        return ""
    return hashlib.sha1((kind + "|" + raw).encode("utf-8")).hexdigest()[:12]


def _clip(v, n: int) -> str:
    return str(v or "").strip()[:n]


def _entry(kind: str, item: dict) -> dict:
    return {
        "id": key_for(kind, item),
        "kind": kind,                      # news / github
        "title": _clip(item.get("title") or item.get("name"), 200),
        "url": _clip(item.get("url"), 400),
        "source": _clip(item.get("source"), 60),
        "cat": _clip(item.get("cat"), 20),
        "brief": _clip(item.get("note") or item.get("desc") or item.get("summary"), 240),
        "lang": _clip(item.get("lang"), 20),
        "stars": _clip(item.get("stars"), 20),
        "savedAt": _now().isoformat(timespec="seconds"),
    }


def load() -> list[dict]:
    """收藏列表，新的在前。靠 mtime 缓存，别每次请求都读盘。"""
    global _cache, _mtime
    try:
        m = PATH.stat().st_mtime if PATH.exists() else 0.0
    except OSError:
        m = 0.0
    if _cache is not None and m == _mtime:
        return list(_cache)
    items: list[dict] = []
    if PATH.exists():
        try:
            obj = json.loads(PATH.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                obj = obj.get("items") or []
            if isinstance(obj, list):
                items = [x for x in obj if isinstance(x, dict) and x.get("id")]
        except Exception as exc:
            store.log(f"bookmarks 读不出来：{exc}")
            items = list(_cache or [])
    _cache, _mtime = items, m
    return list(items)


def ids() -> set[str]:
    return {x["id"] for x in load()}


def _write(items: list[dict]) -> None:
    global _cache, _mtime
    PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, PATH)
    except Exception as exc:
        store.log(f"bookmarks 写不进去：{exc}")
    _cache = items
    try:
        _mtime = PATH.stat().st_mtime if PATH.exists() else 0.0
    except OSError:
        _mtime = 0.0


def toggle(kind: str, item: dict) -> dict:
    """收藏 / 取消收藏。返回这份收藏现在是开是关，以及总数。"""
    key = key_for(kind, item)
    if not key:
        return {"ok": False, "message": "这条没有链接也没有标题，收不了"}
    with _lock:
        items = load()
        on = any(x["id"] == key for x in items)
        if on:
            items = [x for x in items if x["id"] != key]
        else:
            items = [_entry(kind, item)] + [x for x in items if x["id"] != key]
            if len(items) > MAX_ITEMS:
                items = items[:MAX_ITEMS]
        _write(items)
    return {"ok": True, "on": not on, "count": len(items), "id": key,
            "items": items}


def remove(bid: str) -> dict:
    with _lock:
        items = load()
        left = [x for x in items if x["id"] != bid]
        if len(left) != len(items):
            _write(left)
        return {"ok": True, "count": len(left), "items": left}
