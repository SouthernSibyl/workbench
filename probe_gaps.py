"""跑一遍体检：节日覆盖范围、配图索引一致性、简报时效。"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import ai  # noqa: E402
import calendar_cn  # noqa: E402
import photos  # noqa: E402
import store  # noqa: E402

cfg = store.load_config()
now = datetime.datetime.now()

print("=== 节日 / 节气覆盖范围 ===")
for off in (0, 1, 30, 90, 200, 400, 700):
    d = now + datetime.timedelta(days=off)
    ctx = calendar_cn.today_context(d, cfg.get("anniversaries") or [])
    cd = ctx.get("countdown") or []
    first = cd[0]["title"] if cd else "-"
    print("  +%4d天 %s 节日=%-8s 未来条目=%2d 首条=%s"
          % (off, ctx["iso"], ctx.get("festival") or "无", len(cd), first))

print()
print("=== 配图库一致性 ===")
root = store.ART_DIR / "library"
disk = set(os.listdir(root)) if root.exists() else set()
idx = store.read_cache("photos") or []
ref = {i.get("file") for i in idx}
print("  磁盘文件 %d / 索引 %d / 孤儿(磁盘有索引无) %d / 死链 %d"
      % (len(disk), len(idx), len(disk - ref), len(ref - disk)))

print()
print("=== 简报时效 ===")
for iso in sorted(p.stem.replace("daily-", "")
                  for p in (store.DATA_DIR / "cache").glob("daily-*.json")):
    d = store.read_cache("daily-" + iso)
    pool = ((d.get("news") or {}).get("items")) or []
    newest = max((i.get("at") or "") for i in pool) if pool else ""
    print("  %s 生成于 %s / 来源=%s / %d 条 / 最新条目时间=%s"
          % (iso, d.get("generatedAt"), d.get("generatedBy"), len(pool), newest or "未记录"))

print()
print("=== 跨天新鲜度判断 ===")
today = now.strftime("%Y-%m-%d")
d = store.read_cache("daily-" + today) or {}
gen = str(d.get("generatedAt") or "")
stale = gen[:10] != today
print("  今日缓存生成于 %s -> %s" % (gen or "无", "过夜陈旧，需刷新" if stale else "当天的"))

print()
print("=== keepalive 行为 ===")
print("  power:", cfg.get("power"))
