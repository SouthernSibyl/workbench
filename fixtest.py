"""针对本轮修复的专项验证：跨天刷新、空壳保护、配图库并发与孤儿清理。"""
import datetime
import os
import shutil
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "app"))

import ai  # noqa: E402
import calendar_cn  # noqa: E402
import photos  # noqa: E402
import store  # noqa: E402
import weather  # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  <- " + str(detail)) if detail else ""))


cfg = store.load_config()
now = datetime.datetime.now()
iso = now.strftime("%Y-%m-%d")

print("=== 1. calendar_cn 接受 date / datetime / None ===")
try:
    a = calendar_cn.today_context(now, [])
    b = calendar_cn.today_context(now.date(), [])
    c = calendar_cn.today_context(None, [])
    check("datetime 可用", a["iso"] == b["iso"] == c["iso"] == iso, a["iso"])
except Exception as exc:
    check("datetime 可用", False, exc)

print()
print("=== 2. needs_refresh 判定 ===")
good = {"generatedAt": now.isoformat(timespec="seconds"),
        "news": {"items": [{"title": "t"}]}}
overnight = {"generatedAt": (now - datetime.timedelta(hours=16)).isoformat(timespec="seconds"),
             "news": {"items": [{"title": "t"}]}}
empty = {"generatedAt": now.isoformat(timespec="seconds"), "news": {"items": []}}
check("当天生成 -> 不用刷新", ai.needs_refresh(good, iso) is False)
check("隔夜预生成 -> 要刷新", ai.needs_refresh(overnight, iso) is True)
check("空壳 -> 要刷新", ai.needs_refresh(empty, iso) is True)
check("没有缓存 -> 要刷新", ai.needs_refresh(None, iso) is True)

print()
print("=== 3. 空壳不得覆盖已有的好缓存 ===")
key = f"daily-{iso}"
backup = store.read_cache(key)
try:
    store.write_cache(key, good)
    real = ai.fetch_news_pool
    ai.fetch_news_pool = lambda c, force=False, **kw: []      # 模拟网络抖动抓不到
    out = ai.build_daily(cfg, calendar_cn.today_context(now, []), force=True, wx=None)
    kept = store.read_cache(key) or {}
    check("抓到 0 条时不覆盖旧内容",
          len(((kept.get("news") or {}).get("items")) or []) == 1,
          len(((kept.get("news") or {}).get("items")) or []))
    ai.fetch_news_pool = real
finally:
    if backup is not None:
        store.write_cache(key, backup)

print()
print("=== 4. 配图同步并发保护 ===")
res = {}
barrier = threading.Barrier(3)


def worker(tag):
    barrier.wait()
    res[tag] = photos.sync(cfg)


ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
[t.start() for t in ts]
[t.join() for t in ts]
busy = [v for v in res.values() if v.get("skipped") == "busy"]
check("三路并发的同步有且只有一路真跑", len(busy) >= 1, res)

print()
print("=== 5. 索引合并：不抹掉别人刚写的，但自己的淘汰要生效 ===")
snapshot = photos._load_index()          # 备份，测完原样还回去
try:
    n0 = len(snapshot)
    t0 = (now - datetime.timedelta(seconds=5)).isoformat(timespec="seconds")
    other = {"id": "_other_writer", "file": "_other.jpg", "src": "test",
             "title": "别处刚入库的", "addedAt": now.isoformat(timespec="seconds")}
    store.write_cache("photos", snapshot + [other])

    doomed = snapshot[0]
    mine = snapshot[1:]                  # 手里这份把第一条淘汰掉了
    photos._save_index(mine, newer_than=t0)

    after = photos._load_index()
    ids = {i.get("id") for i in after}
    check("别人刚入库的条目不丢", "_other_writer" in ids, len(after))
    check("自己淘汰的条目真的删掉了", doomed.get("id") not in ids)
    check("条数 = 保留的 + 并回的", len(after) == len(mine) + 1,
          f"{n0} -> {len(after)}")
finally:
    store.write_cache("photos", snapshot)
    check("索引已还原", len(photos._load_index()) == len(snapshot))

print()
print("=== 6. 孤儿清理（干跑）===")
dry = photos.cleanup_orphans(dry_run=True)
st = photos.stats()
print(f"      磁盘 {st['files']} 个文件 / 索引 {st['total']} 条 / 孤儿 {dry['orphans']} 个 "
      f"/ 可释放 {dry['bytes'] / 1048576:.1f} MB / 共 {st['mb']} MB")
check("孤儿数量与 stats 一致", dry["orphans"] == st["orphans"],
      f"{dry['orphans']} vs {st['orphans']}")

print()
print(f"通过 {len(OK)} 项，失败 {len(BAD)} 项")
if BAD:
    print("失败清单：", BAD)
sys.exit(1 if BAD else 0)
