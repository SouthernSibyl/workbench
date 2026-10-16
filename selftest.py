"""同进程自测：不起窗口、不依赖外部 shell 生命周期。

卡住的时候最有用的一招：设环境变量 `WB_DUMP_AFTER=180`，
到点会把所有线程的栈打出来再退出 —— 一眼看出谁在等谁。
"""

import datetime
import faulthandler
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

if os.environ.get("WB_DUMP_AFTER"):
    faulthandler.dump_traceback_later(int(os.environ["WB_DUMP_AFTER"]), exit=True)

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

import actions  # noqa: E402
import ai  # noqa: E402
import calendar_cn  # noqa: E402
import main  # noqa: E402
import photos  # noqa: E402
import sources  # noqa: E402
import store  # noqa: E402
import weather  # noqa: E402

OK = []
BAD = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    tail = ("  " + str(detail)[:200]) if detail else ""
    print(("  OK  " if cond else " FAIL ") + name + tail)


print("=== 1. 配置 / 目录 ===")
store.ensure_dirs()
cfg = store.load_config()
check("启动项是列表", isinstance(cfg["launchers"], list),
      f"{len(cfg['launchers'])} 项（默认空，由用户自己添加）")
check("启动项字段齐全",
      all(l.get("id") and l.get("title") and l.get("kind")
          for l in cfg["launchers"]),
      [l.get("title") for l in cfg["launchers"]])
check("资讯源有分类", all(f.get("cat") for f in cfg["news"]["feeds"]),
      f"{len(cfg['news']['feeds'])} 个源")
check("娱乐黑名单", len(cfg["news"]["exclude"]) > 10, len(cfg["news"]["exclude"]))
check("数据目录", store.DATA_DIR.exists(), store.DATA_DIR)

print("=== 1b. 配置表驱动（一切个性化都该能在这里改）===")
import settings_schema  # noqa: E402

_sfields = settings_schema.all_fields()
check("配置项数量", len(_sfields) >= 30, f"{len(_sfields)} 项")
check("配置项路径不重复", len({f["path"] for f in _sfields}) == len(_sfields))
check("每项都有标签和类型",
      all(f.get("label") and f.get("type") for f in _sfields))
_smiss = [f["path"] for f in _sfields
          if settings_schema.get_path(cfg, f["path"], "__MISS__") == "__MISS__"]
check("配置项路径都能取到值", not _smiss, _smiss)
# 这几项以前写死在代码里、界面上根本改不了，现在必须是配置项
for _p in ("news.boost", "news.chars_per_minute", "news.max_age_days", "news.brief",
           "news.exclude", "ai.mode", "ai.web_url", "ai.vision_model", "ai.enabled",
           "photos.rotate", "photos.tag_local", "photos.vision_tagging",
           "photos.nasa", "photos.nasa_key", "power.keepalive_seconds",
           "power.prefetch_tomorrow", "power.sweep_browser_cache",
           "signals.enabled", "signals.browser_history", "signals.days",
           "window.width", "window.height", "profile.name"):
    check(f"可配置：{_p}", settings_schema.get_path(cfg, _p, None) is not None)
_blk = settings_schema.blank_config()
settings_schema.set_path(_blk, "a.b.c", 7)
check("set_path 会建中间层", settings_schema.get_path(_blk, "a.b.c") == 7)
check("加权词表进了配置而不是代码常量",
      isinstance(cfg["news"].get("boost"), list) and len(cfg["news"]["boost"]) > 10,
      f"{len(cfg['news']['boost'])} 个词")

print("=== 1c. 本地推导打标（不联网、零成本那两层）===")
check("时段划分：6 点 -> 黎明", photos._slot_of_hour(6) == "黎明")
check("时段划分：13 点 -> 正午", photos._slot_of_hour(13) == "正午")
check("时段划分：17 点 -> 黄昏", photos._slot_of_hour(17) == "黄昏")
check("时段划分：23 点 -> 夜晚", photos._slot_of_hour(23) == "夜晚")
check("季节：1 月 -> 冬 / 9 月 -> 秋",
      photos._season_of_month(1) == "冬" and photos._season_of_month(9) == "秋")
check("文件名抠出时刻与季节",
      photos._stamp_from_name("PXL_20261225_193045.jpg") == ("蓝调", "冬"),
      photos._stamp_from_name("PXL_20261225_193045.jpg"))
check("文件名只有日期时只给季节",
      photos._stamp_from_name("IMG_20251001.jpg") == ("", "秋"))
check("文件名没时间就不瞎猜（免得误伤）",
      photos._stamp_from_name("IMG_1234.jpg") == ("", ""))


def _crafted_exif() -> bytes:
    """手写一段最小 EXIF（不依赖任何第三方库），用来钉住解析器行为。"""
    import struct as _s

    def ent(tag, typ, cnt, val):
        # IFD 条目固定 12 字节：tag(2) type(2) count(4) value(4)
        return _s.pack("<HHII", tag, typ, cnt, val)

    ifd0 = (_s.pack("<H", 2)
            + ent(0x8769, 4, 1, 38)      # ExifIFD 指针
            + ent(0x0112, 3, 1, 1)       # Orientation
            + _s.pack("<I", 0))
    exif = (_s.pack("<H", 3)
            + ent(0x829A, 5, 1, 80)      # ExposureTime = 15/1
            + ent(0x920A, 5, 1, 88)      # FocalLength  = 100/1
            + ent(0xA405, 3, 1, 16)      # FocalLengthIn35mmFilm = 16
            + _s.pack("<I", 0))
    rat = _s.pack("<II", 15, 1) + _s.pack("<II", 100, 1)
    return (b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00"
            + b"II" + _s.pack("<HI", 0x2A, 8) + ifd0 + exif + rat)


_ex = photos._exif_facts(_crafted_exif())
check("EXIF 读出曝光时间 15s", _ex.get("exposure") == 15.0, _ex)
check("EXIF 读出等效焦距 16mm", _ex.get("focal35") == 16)
check("EXIF 读出实际焦距 100mm", _ex.get("focal") == 100.0)
check("EXIF 读出方向", _ex.get("orientation") == 1)
check("没有 EXIF 的图返回空 dict（不能抛异常）",
      photos._exif_facts(b"\xff\xd8\xff\xe0" + b"\x00" * 64) == {})
_fa, _fn = photos._facts_attrs(_ex, "x.jpg")
check("16mm 等效 -> 构图含广角", "广角" in (_fa.get("构图") or []), _fa)
check("15s 曝光 -> 事件含长曝", "长曝" in (_fa.get("事件") or []), _fa)
check("EXIF 拍摄时间 -> 时段/季节", bool(_fn), _fn)
check("打标可以被配置关掉",
      photos.tag_local({"photos": {"tag_local": False}}).get("skipped") == "disabled")
check("分类按来源兜底", photos.category_of({"src": "astro", "attrs": {}}) == "天象")
check("分类优先看题材",
      photos.category_of({"src": "bing", "attrs": {"题材": ["植物"]}}) == "植物")
check("图库统计带分类与打标数",
      isinstance(photos.stats().get("byCategory"), dict)
      and "byTagged" in photos.stats(), photos.stats().get("byTagged"))

print("=== 2. 日期 / 节气 ===")
TODAY = datetime.date.today().isoformat()
ctx = calendar_cn.today_context(None, [])
check("今日上下文", ctx["iso"] == TODAY, f"{ctx['dateText']} ({TODAY})")
check("接受 date 对象", calendar_cn.today_context(datetime.date.today(), [])["iso"] == TODAY)
check("接受 datetime 对象",
      calendar_cn.today_context(datetime.datetime.now(), [])["iso"] == TODAY)
ctx2 = calendar_cn.today_context(datetime.datetime(2026, 10, 23), [])
check("霜降识别", ctx2["festival"] == "霜降", ctx2["festival"])
ctx3 = calendar_cn.today_context(datetime.datetime(2026, 10, 1), [])
check("国庆识别", ctx3["festival"] == "国庆节", ctx3["festival"])

print("=== 3. 天气 ===")
wx = weather.current(cfg, force=True)
check("天气取到", wx.get("ok") is True, f"{wx.get('city')} {wx.get('text')} {wx.get('temp')}°C")
check("天气有城市", bool(wx.get("city")), wx.get("city"))
check("天气有时段", bool(wx.get("phase")), f"{wx.get('phase')} / 日出{wx.get('sunrise')} 日落{wx.get('sunset')}")
check("天气有配图关键词", len(wx.get("keywords") or []) > 0, wx.get("keywords"))
check("天气有明日", isinstance(wx.get("tomorrow"), dict), wx.get("tomorrow"))
code_text, code_icon, code_kw = weather.describe(63)
check("WMO 映射", code_text == "中雨" and code_icon == "rain", f"{code_text}/{code_icon}")

print("=== 4. 图片标签体系（三层属性）===")
_a = photos.attrs_of("秋日清晨的湖面倒影，远处山脉与森林，薄雾笼罩，木屋与桥")
check("命中地形", "地形" in _a.get("题材", []), _a.get("题材"))
check("命中植物", "植物" in _a.get("题材", []))
check("命中建筑", "建筑" in _a.get("题材", []))
check("命中水景", "水景" in _a.get("题材", []))
check("命中秋季", "秋" in _a.get("季节", []), _a.get("季节"))
check("命中清晨", "清晨" in _a.get("时段", []), _a.get("时段"))
check("命中雾", "雾" in _a.get("天气", []), _a.get("天气"))
check("单字不会误伤（火星 != 星轨）",
      "长曝" not in photos.attrs_of("火星与泥泞地面的探测照片").get("事件", []))

print("=== 5. 资讯池（分类 + 过滤娱乐）===")
pool = ai.fetch_news_pool(cfg)
cats = sorted({p["cat"] for p in pool})
check("抓到资讯", len(pool) > 20, f"{len(pool)} 条，分类 {cats}")
check("覆盖多分类", len(cats) >= 3, cats)
ent = [p for p in pool if any(k in p["title"] for k in cfg["news"]["exclude"])]
check("娱乐已被过滤", len(ent) == 0, f"残留 {len(ent)} 条")
withsum = [p for p in pool if p.get("summary")]
check("带来源摘要", len(withsum) > len(pool) * 0.3, f"{len(withsum)}/{len(pool)}")

print("=== 6. 兜底简报（无模型路径）===")
sig = sources.collect(cfg)
ranked = ai.rank_pool(pool, sig, 12)
check("轮流排序", len(ranked) == 12, [p["cat"] for p in ranked])
fb = ai._fallback_items(pool, 10)
check("兜底条目", len(fb) == 10 and all(i["points"] for i in fb),
      f"{len(fb)} 条，例：{fb[0]['points'][0][:38]}")
check("阅读预算", ai.per_item_chars(cfg) == 600 and ai.read_budget(cfg) == 6000,
      f"每条 {ai.per_item_chars(cfg)} 字 / 共 {ai.read_budget(cfg)} 字")

print("=== 7. 提示词 ===")
p = ai.build_prompt(cfg, ctx, wx)
check("提示词含要点要求", "points" in p["user"] and "items" in p["user"])
check("提示词含天气", "°C" in p["user"] or "晴" in p["user"] or "云" in p["user"])
check("提示词禁止娱乐", "娱乐" in p["user"])
check("提示词不含 summary 要求", "不要写 summary" in p["user"])

print("=== 8. 模型 JSON 合并（含编造内容拦截）===")
pool_small = [{"title": "真实标题A", "url": "http://a", "source": "S", "cat": "要闻", "summary": "源摘要A"},
              {"title": "真实标题B", "url": "http://b", "source": "S", "cat": "科技", "summary": ""}]
fake = {
    "quote": "测试短句。", "quoteFrom": "T", "tags": ["甲", "乙"],
    "recommend": {"title": "做点什么", "detail": "细节"},
    "brief": "今天的总览。",
    "items": [
        {"title": "真实标题A", "cat": "要闻", "points": ["要点一", "要点二"], "why": "与你有关"},
        {"title": "编造的标题", "cat": "科技", "points": ["不该出现"]},
        {"title": "真实标题B", "cat": "科技", "points": ["要点三"]},
    ],
}
payload = {"fields": {"quote": "", "recommend": {}, "tags": []}, "news": {}}
ai._merge_model_json(payload, fake, pool_small, 10)
items = payload["news"]["items"]
titles = [i["title"] for i in items]
check("保留真实条目", "真实标题A" in titles and "真实标题B" in titles, titles)
check("拦掉编造条目", "编造的标题" not in titles)
check("要点已带入", items[0]["points"] == ["要点一", "要点二"], items[0]["points"])
check("链接回填原地址", items[0]["url"] == "http://a")
check("记录了 brief", payload["news"]["brief"] == "今天的总览。")
check("分类归一", items[1]["cat"] == "科技", items[1]["cat"])

print("=== 9. JSON 容错 ===")
samples = ['{"quote":"a"}', '```json\n{"quote":"b"}\n```', '好的：\n{"quote":"c"}\n完', '{"quote":"嵌{套}","n":1}']
n = 0
for s in samples:
    try:
        if ai._extract_json(s).get("quote"):
            n += 1
    except Exception as exc:
        print("     解析失败:", exc)
check("容错 4/4", n == 4, f"{n}/4")

print("=== 10. 真实模型生成 ===")
if ai.ai_ready(cfg):
    t0 = time.time()
    daily = ai.build_daily(cfg, ctx, force=True, wx=wx)
    items = (daily.get("news") or {}).get("items") or []
    check("模型生成成功", daily.get("generatedBy") == "ai" and len(items) >= 6,
          f"{len(items)} 条 / {time.time()-t0:.0f}s / err={daily.get('aiError') or '无'}")
    check("条目都带要点", all(i.get("points") for i in items),
          f"例：{items[0]['points'][0][:40] if items else '无'}")
    check("分类覆盖", len({i["cat"] for i in items}) >= 3,
          sorted({i["cat"] for i in items}))
    t1 = time.time()
    ex = ai.expand_item(cfg, ctx, 0)
    check("简述生成", ex.get("ok") and ex.get("chars", 0) > 200,
          f"{ex.get('chars')} 字 / 约 {ex.get('mins')} 分钟 / {time.time()-t1:.0f}s")
    check("简述已落盘", bool(ai.build_daily(cfg, ctx).get("news") is not None))
else:
    print("  --   未配置模型，跳过")

print("=== 11. HTTP 接口 ===")
main.PORT_CANDIDATES = [39411]
srv, live = main.bind_server()
check("绑定成功且没有撞上旧实例", srv is not None and live is None,
      f"srv={srv is not None} live={live}")
main._SERVER = srv
threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True).start()
time.sleep(0.6)
base = "http://127.0.0.1:39411"


def get(path, data=None):
    req = urllib.request.Request(base + path)
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
        req.method = "POST"
    _t = time.perf_counter()
    try:
        # 模型调用偶尔要 30 秒以上，超时给宽一点；真超时会把所有线程栈打出来
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read()
    except urllib.error.HTTPError:
        raise                          # 403/404 是被测行为，不是卡住
    except Exception as exc:
        # 服务线程和自测在同一进程里 —— 卡住时把所有栈打出来，一眼看出谁在等谁
        print(f"      [断] {path}: {type(exc).__name__}，全部线程栈：")
        faulthandler.dump_traceback()
        raise
    _ms = (time.perf_counter() - _t) * 1000
    if _ms > 5000:
        print(f"      [慢] {path} 用了 {_ms:.0f} ms")
    return r.status, body


code, body = get("/api/ping")
check("GET /api/ping", code == 200 and b"workbench" in body)
code, body = get("/api/state")
st = json.loads(body)
check("GET /api/state", code == 200 and st["today"]["iso"] == TODAY)
check("状态含天气", st["weather"].get("ok") is True, st["weather"].get("text"))
check("状态含启动器", isinstance(st["launchers"], list),
      f"{len(st['launchers'])} 项")
check("状态含简报条目", len(st["daily"]["items"]) >= 6,
      f"{len(st['daily']['items'])} 条")
check("条目带要点", all(i.get("points") for i in st["daily"]["items"]),
      st["daily"]["items"][0]["points"][0][:36] if st["daily"]["items"] else "无")
check("阅读预算字段", st["daily"]["readMinutes"] == 15.0 and st["daily"]["perItemChars"] == 600,
      f"{st['daily']['readMinutes']} 分钟 / 每条 {st['daily']['perItemChars']} 字")
check("状态含配图", "hero" in st["photos"], f"库内 {st['photos'].get('total')} 张")

if not ai.ai_ready(cfg):
    print("=== 11a/11b. 简述生成 -- 未配置模型，跳过 ===")
else:
    code, body = get("/api/expand", {"index": 1})
    ex = json.loads(body)
    check("POST /api/expand", ex.get("ok") and len(ex.get("summary", "")) > 150,
          f"{ex.get('chars')} 字 / {ex.get('mins')} 分钟")

    print("=== 11b. 一次生成全部简述 ===")
    # 先把今天的简述全清掉，验证批量任务真能从零补齐
    _key = f"daily-{datetime.date.today().isoformat()}"
    _p = store.read_cache(_key) or {}
    for _it in ((_p.get("news") or {}).get("items") or []):
        _it["summary"] = ""
        _it["stub"] = True
    store.write_cache(_key, _p)
    check("简述已清空", all(not i.get("summary")
          for i in (store.read_cache(_key).get("news") or {}).get("items") or []))

    code, body = get("/api/expand-all", {"index": 0})
    check("POST /api/expand-all", json.loads(body).get("ok") is True)
    _t0 = time.time()
    _e = {"running": True, "done": 0, "total": 0}
    while time.time() - _t0 < 300:
        st = json.loads(get("/api/state")[1])
        _e = st["status"]["expanding"]
        if not _e["running"] and _e["done"] > 0:
            break
        time.sleep(4)
    check("批量生成跑完", _e["done"] == _e["total"] and _e["total"] >= 6,
          f"{_e['done']}/{_e['total']} / {time.time()-_t0:.0f}s")
    _items = st["daily"]["items"]
    _ready = sum(1 for i in _items if i.get("summary") and not i.get("stub")
                 and len(i["summary"]) > 200)
    check("全部条目都有简述", _ready == len(_items), f"{_ready}/{len(_items)} 条")
    _lens = [len(i.get("summary") or "") for i in _items]
    check("每条都够长", all(250 <= n <= 1100 for n in _lens), _lens)
    check("点的那条优先", bool(_items[0].get("summary")))

code, body = get("/api/todos", {"action": "add", "text": "自测待办"})
td = json.loads(body)
check("POST /api/todos 添加", td.get("ok") and len(td["todos"]) >= 1, len(td.get("todos", [])))
tid = td["todos"][-1]["id"]
code, body = get("/api/todos", {"action": "toggle", "id": tid})
check("POST /api/todos 勾选", json.loads(body)["todos"][-1]["done"] is True)
code, body = get("/api/todos", {"action": "remove", "id": tid})
check("POST /api/todos 删除", all(t["id"] != tid for t in json.loads(body)["todos"]))

code, body = get("/icon/book")
check("GET /icon/book", code == 200 and b"<svg" in body, f"{len(body)}B")
code, body = get("/api/prompt")
check("GET /api/prompt", code == 200 and len(json.loads(body)["combined"]) > 500)
code, body = get("/")
check("GET / 首页", code == 200 and b"<!DOCTYPE html>" in body, len(body))
code, body = get("/static/app.js")
check("GET /static/app.js", code == 200 and len(body) > 8000, len(body))
_js = body.decode("utf-8", "ignore")
check("前端能识别服务断线", "noteOffline" in _js and "offline-bar" in _js)
check("断线提示可自愈", "location.reload" in _js and "/api/ping" in _js)
check("报错文案已本地化", "本地服务没在运行" in _js and "Failed to fetch" not in _js)
# 页面卸载事件在「重载」时也触发 —— 绑在它上面的退出请求会让按 F5 直接关掉整个服务。
# （「退出」按钮走 /api/quit 是对的，这里禁的是「页面一卸载就退出」。）
check("重载页面不会关掉服务",
      "sendBeacon" not in _js
      and not re.search(r"addEventListener\(\s*['\"]pagehide", _js),
      "不该在页面卸载时请求退出")
code, body = get("/static/style.css")
_css = body.decode("utf-8", "ignore")
check("断线样式已定义", ".offline-bar" in _css and ".offline-bar.show" in _css)
try:
    get("/art/../../../../Windows/win.ini")
    check("目录穿越拦截", False, "竟然能读")
except urllib.error.HTTPError as exc:
    check("目录穿越拦截", exc.code in (403, 404), f"HTTP {exc.code}")

# 注意：srv.shutdown() 只停 serve_forever，**不会关监听套接字** ——
# 后面如果还有 HTTP 测试，连接会堆在 backlog 里没人处理，一路等到超时。
# 所以一定要放到所有 HTTP 测试之后。

print("=== 12. 独占绑定 / 实例锁 ===")
import socket as _socket  # noqa: E402

_probe = _socket.socket()
_probe.bind(("127.0.0.1", 0))
_tp = _probe.getsockname()[1]
_probe.close()
_s1 = main._QuietServer(("127.0.0.1", _tp), main.Handler)
try:
    _s2 = main._QuietServer(("127.0.0.1", _tp), main.Handler)
    _s2.server_close()
    check("同端口二次绑定被拒", False, "两个 socket 绑上了同一端口")
except OSError:
    check("同端口二次绑定被拒", True)
_s1.server_close()

lock_a = main.InstanceLock()
check("首个实例拿到锁", lock_a.acquire() is True)
lock_a.write_port(12345)
check("端口写回读回", main.InstanceLock.read_port() == 12345, main.InstanceLock.read_port())

print("=== 13. 跨天新鲜度 ===")
_now = datetime.datetime.now()
_iso = _now.date().isoformat()
_good = {"generatedAt": _now.isoformat(timespec="seconds"),
         "news": {"items": [{"title": "t"}]}}
_overnight = {"generatedAt": (_now - datetime.timedelta(days=1, hours=2)).isoformat(timespec="seconds"),
              "news": {"items": [{"title": "t"}]}}
_empty = {"generatedAt": _now.isoformat(timespec="seconds"), "news": {"items": []}}
check("当天生成 -> 不再重做", ai.needs_refresh(_good, _iso) is False)
check("隔夜预生成 -> 要重做", ai.needs_refresh(_overnight, _iso) is True)
check("空壳 -> 要重做", ai.needs_refresh(_empty, _iso) is True)
check("无缓存 -> 要重做", ai.needs_refresh(None, _iso) is True)

_key = f"daily-{_iso}"
_backup = store.read_cache(_key)
try:
    store.write_cache(_key, _good)
    _real = ai.fetch_news_pool
    ai.fetch_news_pool = lambda c, force=False, **kw: []       # 模拟网络抖动
    try:
        ai.build_daily(cfg, calendar_cn.today_context(None, []), force=True, wx=None)
    finally:
        ai.fetch_news_pool = _real
    _kept = ((store.read_cache(_key) or {}).get("news") or {}).get("items") or []
    check("抓不到资讯时不覆盖旧简报", len(_kept) == 1, len(_kept))
finally:
    if _backup is not None:
        store.write_cache(_key, _backup)

print("=== 14. 配图库并发与索引 ===")
_res = {}
_barrier = threading.Barrier(3)


def _syncer(tag):
    _barrier.wait()
    _res[tag] = photos.sync(cfg)


_ts = [threading.Thread(target=_syncer, args=(i,)) for i in range(3)]
[t.start() for t in _ts]
[t.join() for t in _ts]
check("三路并发的同步只放一路进", sum(1 for v in _res.values()
                                       if v.get("skipped") == "busy") >= 1, _res)

_snap = photos._load_index()
try:
    _t0 = (_now - datetime.timedelta(seconds=5)).isoformat(timespec="seconds")
    _other = {"id": "_selftest_other", "file": "_selftest_other.jpg", "src": "test",
              "addedAt": _now.isoformat(timespec="seconds")}
    store.write_cache("photos", _snap + [_other])
    _doomed = _snap[0]
    photos._save_index(_snap[1:], newer_than=_t0)
    _after = photos._load_index()
    _ids = {i.get("id") for i in _after}
    check("并回别处刚入库的条目", "_selftest_other" in _ids)
    check("自己淘汰的条目真删了", _doomed.get("id") not in _ids)
    check("条数 = 保留 + 并回", len(_after) == len(_snap), f"{len(_snap)} -> {len(_after)}")
finally:
    store.write_cache("photos", _snap)

_pst = photos.stats()
check("配图库无孤儿", _pst["orphans"] == 0, f"孤儿 {_pst['orphans']} 个")
check("配图库无死链",
      all((photos.LIB_DIR / i["file"]).exists() for i in photos._load_index()),
      f"{_pst['total']} 条")

print("=== 15. 浏览器缓存治理 ===")
check("有缓存清理清单", len(actions._BROWSER_CACHE_ITEMS) >= 15,
      f"{len(actions._BROWSER_CACHE_ITEMS)} 项")
check("闲置阈值合理", 10 <= actions._IDLE_MINUTES <= 720, actions._IDLE_MINUTES)
import inspect as _inspect  # noqa: E402

_src_win = _inspect.getsource(actions.open_window)
check("启动参数带缓存上限", "--disk-cache-size" in _src_win)
check("启动参数关掉组件下载", "--disable-component-update" in _src_win)
# 清理必须不在 open_window 里（它在关键路径上，返回前用户什么都看不到）
check("开窗口里不做清理", "_sweep_browser_caches" not in _src_win)
_src_defer = _inspect.getsource(actions.deferred_sweep)
check("清理挪到 deferred_sweep 且带延迟", "sleep" in _src_defer
      and "_sweep_browser_caches" in _src_defer)
check("开完窗口才启动清理", "deferred_sweep" in _inspect.getsource(main.main))

print("=== 15b. 窗口存活判据（同类坑踩过两次，钉住）===")
# 坑 1：只看心跳 -> 浏览器冻掉后台窗口的页面定时器，被误判成「窗口关了」
# 坑 2：看启动时那个 msedge.exe 的 pid -> Edge 会交接进程，还是误判
check("开窗口带了三个防冻结参数",
      all(s in _src_win for s in ("--disable-background-timer-throttling",
                                  "--disable-backgrounding-occluded-windows",
                                  "--disable-renderer-backgrounding")))
check("开窗口关掉了窗口遮挡检测", "CalculateNativeWinOcclusion" in _src_win)
check("开窗口禁用了睡眠标签页", "msSleepingTabsEnabled" in _src_win)
_src_wd = _inspect.getsource(main._watchdog)
check("看门狗不再拿进程号判窗口（Edge 会交接进程）",
      "window_alive(" not in _src_wd)
check("看门狗按窗口句柄判窗口", "window_open(" in _src_wd)
check("句柄失效会按标题再找一次", "find_app_window(" in _src_wd)
check("看门狗带休眠检测（睡眠唤醒不判死）", "时钟跳变" in _src_wd)
check("退回默认浏览器时不瞎退", "fallback" in _src_wd)
check("不存在的标题找不到窗口",
      actions.find_app_window("这个标题肯定不存在-wb测试") is None)
check("没有句柄时 window_open 返回 None", actions.window_open(None) is None)
check("句柄无效时 window_open 返回 False", actions.window_open(987654321) is False)

print("=== 16. 启动路径不许有白等 ===")
# 端口是空的就不该去探 —— 探一个死端口要等 ~400ms 超时（防火墙丢 SYN，不立刻 RST）
_probes = []
_real_probe = main.probe_workbench
main.probe_workbench = lambda p: (_probes.append(p), False)[1]
try:
    _free = _socket.socket()
    _free.bind(("127.0.0.1", 0))
    _fp = _free.getsockname()[1]
    _free.close()
    _s, _live = main.bind_server(_fp)
    check("空闲端口直接绑上、不探测", _s is not None and _live is None,
          f"srv={_s is not None} live={_live}")
    check("一次探测都没发生", len(_probes) == 0, f"{len(_probes)} 次")
    if _s:
        _s.server_close()

    # 端口被占时必须探，而且要认得出「占它的是自家服务」
    _hold = _socket.socket()
    _hold.bind(("127.0.0.1", _fp))
    _hold.listen(1)
    try:
        _probes.clear()
        _s2, _live2 = main.bind_server(_fp)
        check("端口被占时才去探", len(_probes) >= 1, f"{len(_probes)} 次")
        # 探不出是自家服务时，绝不能把别人的端口当成自己的（live2 必须是 None）
        check("认不出自家服务就不认领该端口", _live2 is None, f"live={_live2}")
        if _s2:
            _s2.server_close()
    finally:
        _hold.close()
finally:
    main.probe_workbench = _real_probe

# /api/state 这条路上不许有网络 I/O：缓存过期也只在后台刷新
import weather as _wx  # noqa: E402

_net = []


def _no_network(*_a, **_k):
    _net.append(1)
    raise RuntimeError("请求路径上不该联网")


_real_fetch = _wx._fetch
_wx._fetch = _no_network
try:
    _st = main.build_state()
    check("build_state 不联网取天气", len(_net) == 0, f"{len(_net)} 次网络调用")
    check("build_state 仍给出天气字段", "weather" in _st and "city" in _st["weather"])
finally:
    _wx._fetch = _real_fetch

check("天气有只读缓存入口", hasattr(_wx, "peek") and hasattr(_wx, "refresh_async"))
_peek = _wx.peek(cfg)
check("peek 返回 stale 标记", "stale" in _peek, _peek.get("stale"))

# 资讯池过期时，重抓必须走后台 —— 否则点一下「网页生成」要干等 20 多秒（实测 24.5s）
_pool_backup = store.read_cache("news-pool")
_pool_calls = []


def _no_feed(*_a, **_k):
    _pool_calls.append(1)
    return []


_real_feed = ai.fetch_feed
_real_http = ai._http_get
ai.fetch_feed = _no_feed
try:
    store.write_cache("news-pool", {
        "at": (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec="seconds"),
        "items": [{"title": "过期的探针条目", "url": "http://x", "source": "S",
                   "cat": "要闻", "summary": "", "at": ""}],
    })
    _t = time.time()
    _fast = ai.build_prompt(cfg, ctx, _wx.peek(cfg), fast=True)
    _ms = (time.time() - _t) * 1000
    check("fast 模式不重抓资讯（走旧池）", len(_pool_calls) == 0, f"{len(_pool_calls)} 次抓取")
    check("fast 模式够快", _ms < 2000, f"{_ms:.0f} ms")
    check("fast 模式仍给出候选池", len(_fast["pool"]) >= 1, len(_fast["pool"]))

    # 非 fast（真生成简报）时必须重抓
    _pool_calls.clear()
    ai.build_prompt(cfg, ctx, _wx.peek(cfg))
    check("生成简报时仍会重抓", len(_pool_calls) > 0, f"{len(_pool_calls)} 次")
finally:
    ai.fetch_feed = _real_feed
    ai._http_get = _real_http
    if _pool_backup is not None:
        store.write_cache("news-pool", _pool_backup)
    else:
        store.write_cache("news-pool", {"at": "", "items": []})

check("资讯池已还原", (store.read_cache("news-pool") or {}).get("items") is not None)

# 候选少于目标条数时，rank_pool 曾经会死循环（idx 一直涨、any() 恒为真）
# —— 表现是 /api/prompt 永不返回、后台线程把一颗核转满，整个应用跟着卡。
print("--- rank_pool 不许死循环 ---")
for _n in (0, 1, 3, 12, 49, 50, 51, 200):
    _pl = [{"title": f"t{i}", "cat": ["要闻", "财经", "科技", "学术"][i % 4]}
           for i in range(_n)]
    _t = time.time()
    _r = ai.rank_pool(_pl, {}, 50)
    _ms = (time.time() - _t) * 1000
    check(f"池 {_n} 条能在 0.5s 内取完", _ms < 500 and len(_r) == min(_n, 50),
          f"{len(_r)} 条 / {_ms:.0f} ms")

print("=== 17. 图片属性 / 天文事件 / 计算选图 ===")
import attrs as _at  # noqa: E402

_mw = _at.describe("The Milky Way spans the night sky, long-exposure photograph")
check("英文打标命中天象+夜晚",
      "天象" in _mw.get("题材", []) and "夜晚" in _mw.get("时段", []), _mw.get("flat")[:6])
_office = _at.describe("San Francisco office building service notice")
check("英文按词边界匹配（office 不命中冰/雪）",
      "雪" not in _office.get("天气", []) and "水景" not in _office.get("题材", []),
      _office.get("天气"))
_cn = _at.describe("银河拱桥下的雪山与湖面倒影")
check("中文打标命中天象+地形+水景",
      {"天象", "地形", "水景"} <= set(_cn.get("题材", [])), _cn.get("题材"))

_d = datetime.date(2026, 8, 12)
_ev = _at.astro_events(_d)
check("英仙座流星雨窗口",
      any("英仙座" in e["title"] for e in _ev), [e["title"] for e in _ev])
check("8 月算出银河季", any(e["title"] == "银河季" for e in _ev))
check("冬夜算出极光季",
      any(e["title"] == "极光季" for e in _at.astro_events(datetime.date(2026, 12, 1))))
_ph, _nm = _at.moon_phase(datetime.date(2026, 10, 26))
check("月相能算（2026-10-26 前后为满月）", _nm in ("满月", "盈凸月", "亏凸月"), f"{_nm} {_ph:.2f}")

_sc = photos.scene_of(cfg, ctx, _wx.peek(cfg))
check("取图条件含时段", bool(_sc.get("时段")), _sc.get("时段"))
check("取图条件含偏好", "天象" in (_sc.get("偏好") or []), _sc.get("偏好"))
check("取图条件含天象事件", isinstance(_sc.get("eventDetail"), list))

_ch = photos.choose_daily(cfg, ctx, _wx.peek(cfg), force=True)
check("算出了今天的壁纸", bool(_ch.get("main")), (_ch.get("main") or {}).get("title", "")[:30])
check("带了「为什么是这张」", bool(_ch.get("why")), _ch.get("why"))
check("命中的是天文相关内容",
      "天象" in str(_ch.get("main", {}).get("attrs", {}).get("题材") or "")
      or "天象" in _ch.get("why", ""),
      _ch.get("why"))
_ch2 = photos.choose_daily(cfg, ctx, _wx.peek(cfg))
check("第二次直接吃缓存", _ch2.get("cached") is True)
check("缓存键带时段", bool(_ch.get("slot")), _ch.get("slot"))

print("=== 18. 可自选文件的启动项 ===")
_fs = actions.list_dir("")
check("列得出常用位置", len(_fs.get("quick") or []) >= 3,
      [q["name"] for q in _fs.get("quick") or []])
check("列得出磁盘", len(_fs.get("dirs") or []) >= 1, [d["name"] for d in _fs.get("dirs") or []])
_desk = actions.list_dir(str(Path.home() / "Desktop"))
check("能列桌面", _desk.get("ok") is True and isinstance(_desk.get("files"), list),
      f"{len(_desk.get('files') or [])} 个文件")
check("不看 desktop.ini 这类系统文件",
      not any(f["name"].lower() == "desktop.ini" for f in _desk.get("files") or []))

_exe = Path.home() / "Desktop" / "DiskGenius.exe"
if _exe.exists():
    _entry = store.build_launcher(str(_exe))
    check("从文件造出启动项",
          _entry["kind"] == "file" and _entry["title"] and _entry["accent"],
          f"{_entry['title']} / {_entry['subtitle']}")
    check("id 稳定可复现",
          store.build_launcher(str(_exe))["id"] == _entry["id"])
else:
    _entry = store.build_launcher(str(Path.home() / "Desktop"))
    check("从文件夹造出启动项", _entry["kind"] == "file", _entry["title"])

_clean = store.normalize_launchers([
    {"id": "a", "title": "A", "command": "echo hi"},          # 老配置：无 kind
    {"title": "B", "path": "C:/x.exe"},                        # 无 id
    {"title": "空壳"},                                          # 什么都不指向 -> 丢掉
    {"id": "a", "title": "重名", "path": "C:/y.exe"},           # id 冲突
])
check("清洗后只留有内容的", len(_clean) == 3, [e["title"] for e in _clean])
check("老配置按 command 认成 shell", _clean[0]["kind"] == "shell")
check("空 path 被丢弃", all(e.get("path") or e.get("command") or e.get("url") for e in _clean))
check("id 不重复", len({e["id"] for e in _clean}) == len(_clean))

print("=== 19. 启动项接口 + 跨站拦截 ===")
_before = len(json.loads(get("/api/launchers")[1])["launchers"])
code, body = get("/api/launchers", {"action": "add", "path": str(Path.home() / "Desktop")})
_r = json.loads(body)
check("POST /api/launchers 添加", _r.get("ok") is True and
      len(_r.get("launchers") or []) == _before + 1, _r.get("message"))
_new_id = (_r.get("launchers") or [])[-1]["id"]
code, body = get("/api/launchers", {"action": "update", "id": _new_id, "title": "自测改名"})
check("改名", json.loads(body).get("ok") is True)
code, body = get("/api/launchers", {"action": "move", "id": _new_id, "step": -1})
check("排序", json.loads(body).get("ok") is True)
code, body = get("/api/launchers", {"action": "remove", "id": _new_id})
_r = json.loads(body)
check("移除并复原", _r.get("ok") is True and len(_r["launchers"]) == _before,
      f"{len(_r.get('launchers') or [])} 项")

# 跨站请求必须被挡掉（服务只绑本机，但任何网页都能朝 localhost 发请求）
_req = urllib.request.Request(base + "/api/launchers",
                              data=json.dumps({"action": "add", "path": "C:/x.exe"}).encode(),
                              headers={"Content-Type": "application/json",
                                       "Origin": "https://evil.example",
                                       "Sec-Fetch-Site": "cross-site"})
try:
    urllib.request.urlopen(_req, timeout=15)
    check("跨站写请求被拒", False, "竟然通过了")
except urllib.error.HTTPError as exc:
    check("跨站写请求被拒", exc.code == 403, f"HTTP {exc.code}")

# 所有 HTTP 测试都跑完了，这时候才收服务
try:
    srv.shutdown()
    srv.server_close()
except Exception:
    pass

print()
print(f"通过 {len(OK)} 项，失败 {len(BAD)} 项")
if BAD:
    print("失败清单：", BAD)
sys.exit(1 if BAD else 0)
