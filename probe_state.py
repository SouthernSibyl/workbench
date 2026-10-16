"""看看 /api/state 的 570ms 花在哪。"""
import cProfile
import io
import os
import pstats
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "app"))

import main  # noqa: E402
import store  # noqa: E402

# 预热
main.build_state()

print("=== 分段计时 ===")
import calendar_cn  # noqa: E402
import photos  # noqa: E402
import weather  # noqa: E402


def t(label, fn, n=3):
    best = 1e9
    for _ in range(n):
        s = time.perf_counter()
        r = fn()
        best = min(best, (time.perf_counter() - s) * 1000)
    print(f"  {label:34} {best:7.1f} ms")
    return r


cfg = t("store.load_config", store.load_config)
ctx = t("calendar_cn.today_context",
        lambda: calendar_cn.today_context(None, cfg.get("anniversaries") or []))
wx = t("weather.peek", lambda: weather.peek(cfg))
t("store.read_cache(daily)", lambda: store.read_cache(f"daily-{ctx['iso']}"))
t("photos._load_index", lambda: photos._load_index())
idx = photos._load_index()
t("171 次 exists()",
  lambda: sum(1 for i in idx if (photos.LIB_DIR / i.get("file", "")).exists()))
t("photos.pick", lambda: photos.pick(cfg, ctx, wx, limit=10))
t("build_state（整体）", main.build_state)

print()
print("=== 热点函数（build_state 累计 1 次）===")
pr = cProfile.Profile()
pr.enable()
for _ in range(5):
    main.build_state()
pr.disable()
buf = io.StringIO()
pstats.Stats(pr, stream=buf).sort_stats("cumulative").print_stats(18)
print("".join(buf.getvalue().splitlines(True)[4:28]))
