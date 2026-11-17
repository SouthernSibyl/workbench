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
# 别断言具体几个 —— 用户随时会自己加（这次就加了个 MAA），写死 == 3 就会红
check("启动项字段齐全",
      bool(cfg["launchers"]) and all(l.get("id") and l.get("title") and l.get("kind")
                                     for l in cfg["launchers"]),
      [l.get("title") for l in cfg["launchers"]])
check("启动项都能启动（有 path / command / url / server）",
      all(l.get("path") or l.get("command") or l.get("url") or l.get("server")
          for l in cfg["launchers"]),
      [(l.get("title"), l.get("kind")) for l in cfg["launchers"]])
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

# 密钥留空 = 不改：设置面板的密码框永远空着，空着回传不能把真 Key 冲掉
_before = {"ai": {"api_key": "sk-real"}, "photos": {"pexels_key": "px", "nasa_key": ""}}
_incoming = {"ai": {"api_key": ""}, "photos": {"pexels_key": "  ", "nasa_key": ""}}
_kept = settings_schema.keep_blank_secrets(_incoming, _before)
check("空密钥沿用旧值", _incoming["ai"]["api_key"] == "sk-real", _incoming)
check("空白也算留空", _incoming["photos"]["pexels_key"] == "px", _incoming)
check("旧值本来就空 -> 不动", _incoming["photos"]["nasa_key"] == "", _incoming)
check("沿用了哪些要说清楚",
      set(_kept) == {"ai.api_key", "photos.pexels_key"}, _kept)

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
_st = photos.stats()
check("图库统计带 tagged 总数（前端要显示「已打标 N 张」）",
      isinstance(_st.get("tagged"), int) and _st["tagged"] == sum(_st["byTagged"].values()),
      f"tagged={_st.get('tagged')} byTagged={_st.get('byTagged')}")

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
check("状态含启动器", len(st["launchers"]) == len(cfg["launchers"]),
      f"{len(st['launchers'])} 项")
check("状态含简报条目", len(st["daily"]["items"]) >= 6,
      f"{len(st['daily']['items'])} 条")
check("条目带要点", all(i.get("points") for i in st["daily"]["items"]),
      st["daily"]["items"][0]["points"][0][:36] if st["daily"]["items"] else "无")
check("阅读预算字段", st["daily"]["readMinutes"] == 15.0 and st["daily"]["perItemChars"] == 600,
      f"{st['daily']['readMinutes']} 分钟 / 每条 {st['daily']['perItemChars']} 字")
check("状态含配图", "hero" in st["photos"], f"库内 {st['photos'].get('total')} 张")

if ai.ai_ready(store.load_config()):
    code, body = get("/api/expand", {"index": 1})
    ex = json.loads(body)
    check("POST /api/expand", ex.get("ok") and len(ex.get("summary", "")) > 150,
          f"{ex.get('chars')} 字 / {ex.get('mins')} 分钟")
else:
    # 未配 Key：**不再报错**。compose_summary 会退回本地重排（只用源站摘要+要点，
    # 不编造），所以这里断言的是「有产出、且标明是谁写的」。
    # （2026-10-21 之前这里是 400：generate_summary 一上来就 raise，十条一起报错。）
    code, body = get("/api/expand", {"index": 1})
    res = json.loads(body)
    check("未配 Key 时 /api/expand 走本地兜底",
          res.get("ok") is True and res.get("by") in ("local", "ai") and res.get("chars", 0) > 0,
          f"by={res.get('by')} chars={res.get('chars')}")

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

if ai.ai_ready(store.load_config()):
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
else:
    print("  --   未配置模型，简述批量生成跳过（Key 就位后整段会自动跑）")

code, body = get("/api/todos", {"action": "add", "text": "自测待办"})
td = json.loads(body)
check("POST /api/todos 添加", td.get("ok") and len(td["todos"]) >= 1, len(td.get("todos", [])))
tid = td["todos"][-1]["id"]
check("新待办默认莫奈绿", td["todos"][-1].get("color") == "green", td["todos"][-1].get("color"))
code, body = get("/api/todos", {"action": "set_color", "id": tid, "color": "blue"})
_col = [t for t in json.loads(body)["todos"] if t["id"] == tid][0]
check("待办换颜色", _col.get("color") == "blue", _col.get("color"))
try:
    code, body = get("/api/todos", {"action": "set_color", "id": tid, "color": "rainbow"})
    check("未知颜色被拒", False, f"竟然接受了（HTTP {code}）")
except urllib.error.HTTPError as e:
    check("未知颜色被拒", e.code == 400, e.code)
code, body = get("/api/todos", {"action": "toggle", "id": tid})
check("POST /api/todos 勾选", json.loads(body)["todos"][-1]["done"] is True)
code, body = get("/api/todos", {"action": "remove", "id": tid})
check("POST /api/todos 删除", all(t["id"] != tid for t in json.loads(body)["todos"]))

print("=== 9b. 待办：不限量 + 72 小时定时 ===")
import todos as _todos  # noqa: E402

_ts = time.strftime("%Y-%m-%dT%H:%M")
code, body = get("/api/todos")
_td = json.loads(body)
# 相对时间预设（「10 分钟后」那批）已按用户要求全部撤掉，接口里不该再出现
check("待办接口不再下发相对时间预设", "presets" not in _td, list(_td.keys()))
check("待办接口带列表上限 px 与自适应开关",
      isinstance(_td.get("maxListPx"), int) and _td["maxListPx"] > 100
      and isinstance(_td.get("autoFit"), bool),
      f"maxListPx={_td.get('maxListPx')} autoFit={_td.get('autoFit')}")
check("待办列表不再有数量上限", isinstance(_td.get("todos"), list))

code, body = get("/api/todos", {"action": "add", "text": "甲\n乙\n丙"})
_multi = json.loads(body)
check("一次可加多行", _multi.get("ok") and len(_multi["todos"]) >= 3, len(_multi.get("todos", [])))
for _t in _multi["todos"]:
    if _t["text"] in ("甲", "乙", "丙"):
        tid = _t["id"]

_horizon = int(_td.get("horizonHours") or 72)
_far = _todos.fmt(_todos.now() + datetime.timedelta(hours=_horizon + 1))
try:
    code, body = get("/api/todos", {"action": "set_remind", "id": tid, "remindAt": _far})
    check("超过 72 小时的定时被拒", False, f"竟然接受了（HTTP {code}）")
except urllib.error.HTTPError as e:
    check(f"超过 {_horizon} 小时的定时被拒", e.code == 400, f"HTTP {e.code}")

_near = _todos.fmt(_todos.now() + datetime.timedelta(hours=2))
code, body = get("/api/todos", {"action": "set_remind", "id": tid, "remindAt": _near})
_armed = [t for t in json.loads(body)["todos"] if t["id"] == tid][0]
check("2 小时后可以设", code == 200 and _armed["remindAt"] == _near, _armed.get("remindAt"))

code, body = get("/api/todos", {"action": "set_remind", "id": tid, "remindAt": ""})
check("取消定时", [t for t in json.loads(body)["todos"] if t["id"] == tid][0]["remindAt"] == "")

# 到点判定：纯函数，不依赖后台线程的 10 秒节拍
_now = _todos.now()
_probe = [{"id": "p1", "text": "到点了", "done": False, "remindAt": _todos.fmt(_now),
           "reminded": False, "remindedAt": ""}]
_fire, _silent = _todos.collect_due(_probe, _now)
check("到点会被判定为要报", len(_fire) == 1 and _probe[0]["reminded"] is True)
_probe2 = [{"id": "p2", "text": "勾了的", "done": True,
            "remindAt": _todos.fmt(_now - datetime.timedelta(minutes=5)),
            "reminded": False, "remindedAt": ""}]
check("已完成的不会报", _todos.collect_due(_probe2, _now)[0] == [])
_probe3 = [{"id": "p3", "text": "过期一天", "done": False,
            "remindAt": _todos.fmt(_now - datetime.timedelta(hours=30)),
            "reminded": False, "remindedAt": ""}]
_f3, _s3 = _todos.collect_due(_probe3, _now)
check("过期一天以上只销账不弹", _f3 == [] and len(_s3) == 1)

for _t in json.loads(get("/api/todos")[1])["todos"]:
    if _t["text"] in ("甲", "乙", "丙"):
        get("/api/todos", {"action": "remove", "id": _t["id"]})

print("=== 9c. 待办小窗 ===")
# 自测跑在**真配置**上：测完要把 mini.pinned / 尺寸还原，别把用户的设置改脏
# （以前就是没还原，跑一次自测小窗就不钉了）
_mini0 = dict(store.load_config().get("mini") or {})
code, body = get("/mini")
check("GET /mini 小窗页面", code == 200 and "待办小窗".encode() in body, len(body))
check("GET /static/mini.js", get("/static/mini.js")[0] == 200)
check("GET /static/todoui.js", b"TodoUI" in get("/static/todoui.js")[1])
code, body = get("/api/mini", {"action": "close"})
check("没开窗时 close 不炸", json.loads(body).get("ok") is True)
code, body = get("/api/mini", {"action": "pin"})
check("图钉接口可用", json.loads(body).get("mini", {}).get("pinned") is True)
code, body = get("/api/mini", {"action": "unpin"})
check("解下图钉", json.loads(body).get("mini", {}).get("pinned") is False)
code, body = get("/api/mini", {"action": "size", "w": 360, "h": 300})
_res = json.loads(body)
check("改大小接口可用", _res.get("ok") is True, _res.get("message"))
check("改大小接口带回矩形", isinstance(_res.get("rect"), list), _res.get("rect"))
# 拖拽重排：待办与启动项都要能按给定 id 顺序重排
# （自测跑在真数据上：待办的**顺序**也要还原，不能把用户的排列改乱）
_tids = [t["id"] for t in json.loads(get("/api/todos")[1])["todos"]]
_tids0 = list(_tids)
if len(_tids) >= 3:
    _rev = [_tids[2], _tids[0], _tids[1]] + _tids[3:]
    _r = json.loads(get("/api/todos", {"action": "reorder", "ids": _rev})[1])
    check("待办可按给定顺序重排",
          [t["id"] for t in _r["todos"]][:3] == _rev[:3],
          [t["id"] for t in _r["todos"]][:3])
try:      # 空顺序应该被拒（400），get() 会抛，得接住
    get("/api/todos", {"action": "reorder", "ids": []})
    check("空顺序被拒", False, "竟然接受了")
except urllib.error.HTTPError as _e:
    check("空顺序被拒", _e.code == 400, f"HTTP {_e.code}")
_lids = [l["id"] for l in json.loads(get("/api/launchers")[1])["launchers"]]
if len(_lids) >= 2:
    _r = json.loads(get("/api/launchers", {"action": "reorder", "ids": list(reversed(_lids))})[1])
    check("启动项可按给定顺序重排",
          [l["id"] for l in _r["launchers"]] == list(reversed(_lids)),
          [l["title"] for l in _r["launchers"]][:3])

try:      # 还原，别留脏
    store.mutate_mini(pinned=bool(_mini0.get("pinned", True)),
                      width=int(_mini0.get("width") or 360),
                      height=int(_mini0.get("height") or 560))
except Exception as _e:
    print("  --   还原 mini 配置失败:", _e)
_tids_now = [t["id"] for t in json.loads(get("/api/todos")[1])["todos"]]
if _tids0 and _tids0 != _tids_now:
    get("/api/todos", {"action": "reorder", "ids": _tids0})   # 顺序也还原
    print("  --   待办顺序已还原")

# 图标路由：内置的 book.svg 一定在；配置里的启动项图标按 id 取，
# 取不到要体面地 404，不能 500（陌生机器上多半没有自定义图标）
code, body = get("/icon/book")
check("GET /icon/book（内置图标）", code == 200 and b"<svg" in body, f"{len(body)}B")
try:
    get("/icon/_nope_")
    check("GET /icon/不存在的图标", False, "竟然 200")
except urllib.error.HTTPError as _e:
    check("GET /icon/不存在的图标", _e.code == 404, f"HTTP {_e.code}")
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
_skel = {"generatedAt": _now.isoformat(timespec="seconds"), "generatedBy": "local",
         "news": {"items": [{"title": "t"}]}}
check("骨架+模型可用 -> 要重做", ai.needs_refresh(_skel, _iso, ai_ok=True) is True)
check("骨架+没模型 -> 不重做", ai.needs_refresh(_skel, _iso, ai_ok=False) is False)

print("=== 13b. 跨零点补刷（_daily_check）===")
# 起真线程太慢也不该真去抓新闻，所以把 _worker 换成探针，只验证「该不该动手」
_seen = []
_real_worker = main._worker
_real_read_cache = store.read_cache
try:
    main._worker = lambda cfg_override=None: _seen.append(time.time())
    main._ROLLOVER.update(iso="", tries=0, last=0.0)

    store.read_cache = lambda key: _overnight            # 昨晚预生成的那份
    got = main._daily_check()
    check("过夜缓存 -> 派活", got == "rebuild", got)
    time.sleep(0.2)
    check("派出去的活真的跑了一次", len(_seen) == 1, len(_seen))

    got = main._daily_check()
    check("刚补过 -> 进冷却，不连刷", got == "cooldown", got)

    store.read_cache = lambda key: _good                  # 已经换成今天的
    got = main._daily_check()
    check("已经新鲜 -> 不动手", got == "fresh", got)

    store.read_cache = lambda key: None                   # 今天还没有
    main._ROLLOVER.update(iso="", tries=0, last=0.0)
    got = main._daily_check()
    check("没有缓存 -> 派活", got == "rebuild", got)
    time.sleep(0.2)

    main._ROLLOVER.update(tries=main._ROLLOVER_MAX_TRIES, last=0.0)
    store.read_cache = lambda key: _overnight
    got = main._daily_check()
    check("到次数上限 -> 收手", got == "maxed", got)

    with main.LOCK:
        main.STATE["generating"] = True
    got = main._daily_check()
    check("正在生成 -> 不插队", got == "busy", got)
    with main.LOCK:
        main.STATE["generating"] = False
finally:
    main._worker = _real_worker
    store.read_cache = _real_read_cache
    main._ROLLOVER.update(iso="", tries=0, last=0.0)

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
# 这一段**不碰网络，也不碰真实图库**：
#   · 图源换成两个本地文件 —— 以前调真的 photos.sync()，网慢时会卡在 SSL read
#     上十几分钟，一个自测不该因为对方网站慢就挂住；
#   · 库目录换成临时目录 —— sync 会顺手 prunes 掉「索引里没有的文件」，
#     跑一次自测就把用户真实图库里的东西删掉，这不能接受。
import base64
import tempfile as _tfp
from pathlib import Path as _P

_real_lib = photos.LIB_DIR
_real_snap = photos._load_index()
_lib = _P(_tfp.mkdtemp(prefix="wb-lib-")) / "library"
_lib.mkdir(parents=True, exist_ok=True)
photos.LIB_DIR = _lib
store.write_cache("photos", [])
photos._INDEX_CACHE = {"mtime": 0.0, "items": []}

_src_dir = _P(_tfp.mkdtemp(prefix="wb-photo-"))
_FAKE_JPG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPDs0NDT/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
for _i in range(2):
    (_src_dir / f"t{_i}.jpg").write_bytes(_FAKE_JPG)

photos.scan_local = lambda _c: [
    {"src": "test", "path": str(_src_dir / f"t{_i}.jpg"), "title": f"自测图 {_i}"}
    for _i in range(2)]
for _fn in ("fetch_spotlight", "fetch_bing", "fetch_pexels", "fetch_astro", "fetch_nasa"):
    if hasattr(photos, _fn):
        setattr(photos, _fn, lambda *a, **k: [])
photos._SYNC_LOCK = threading.Lock()

_res = {}
_barrier = threading.Barrier(3)


def _syncer(tag):
    _barrier.wait()
    _res[tag] = photos.sync(cfg, force=True)


_ts = [threading.Thread(target=_syncer, args=(i,)) for i in range(3)]
[t.start() for t in _ts]
[t.join() for t in _ts]
check("三路并发的同步只放一路进",
      sum(1 for v in _res.values() if v.get("skipped") == "busy") >= 1, _res)
check("同步真的入库了", sum(v.get("added", 0) for v in _res.values()) == 2,
      {k: v.get("added") for k, v in _res.items()})

# 孤儿 / 死链在「刚同步完」这个一致状态上判 —— 放到后面「人为插入一条不存在的
# 条目」之后再判，等于自己制造死链去考自己
_pst = photos.stats()
check("配图库无孤儿", _pst["orphans"] == 0, f"孤儿 {_pst['orphans']} 个")
check("配图库无死链",
      all((photos.LIB_DIR / i["file"]).exists() for i in photos._load_index()),
      f"{_pst['total']} 条")

try:
    _snap = photos._load_index()
    _t0 = (_now - datetime.timedelta(seconds=5)).isoformat(timespec="seconds")
    _other = {"id": "_selftest_other", "file": "_selftest_other.jpg", "src": "test",
              "addedAt": _now.isoformat(timespec="seconds")}
    # 待淘汰的那条要真的「旧」：刚入库的条目会被 newer_than 并回来，
    # 那是设计（防止并发同步互相抹掉），不是 bug
    _doomed = dict(_snap[0], addedAt=(_now - datetime.timedelta(days=90)).isoformat(timespec="seconds"))
    _rest = [i for i in _snap if i["id"] != _doomed["id"]]
    store.write_cache("photos", _rest + [_doomed, _other])
    photos._save_index(_rest, newer_than=_t0)
    _after = photos._load_index()
    _ids = {i.get("id") for i in _after}
    check("并回别处刚入库的条目", "_selftest_other" in _ids)
    check("自己淘汰的条目真删了", _doomed.get("id") not in _ids)
    check("条数 = 保留 + 并回", len(_after) == len(_rest) + 1,
          f"{len(_rest)} + 1 -> {len(_after)}")

finally:
    # 收尾：库目录与索引都换回真实的那份
    photos.LIB_DIR = _real_lib
    store.write_cache("photos", _real_snap)

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
    # 先空跑一次当热身：第一次调用要读池子、编译正则、建缓存，
    # 在磁盘冷 + 机器忙的时候单独就要 1s 以上 —— 那测的是冷启动，不是「请求路径会不会等网络」
    ai.build_prompt(cfg, ctx, _wx.peek(cfg), fast=True)
    _t = time.time()
    _fast = ai.build_prompt(cfg, ctx, _wx.peek(cfg), fast=True)
    _ms = (time.time() - _t) * 1000
    # ⚠️ 这里不能断言 `len(_pool_calls) == 0`：allow_stale 的设计就是
    # 「先把旧池给你，重抓丢到后台」，后台线程随时可能开始抓 —— 断言零次必然是有竞态的。
    # 真正要保证的是：fast 路径**没有等**抓取，而且拿到的是旧池。
    check("fast 模式没等抓取（不阻塞请求路径）", _ms < 2000, f"{_ms:.0f} ms")
    check("fast 模式给的是旧池（重抓走后台）",
          bool(_fast["pool"]) and "过期的探针条目" in str(_fast["pool"][0].get("title") or ""),
          f"{len(_pool_calls)} 次后台抓取（异步，不计较）")
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
# 夜里图库只有天象可挑，白天挑昼景也是对的 —— 所以这条断言要看时段，
# 否则同一个测试在凌晨全绿、到中午就红（时间依赖的假失败）。
_slot = str(_ch.get("slot") or "")
if any(k in _slot for k in ("夜", "晨昏", "凌晨")):
    check("夜里命中的是天象相关内容",
          "天象" in str(_ch.get("main", {}).get("attrs", {}).get("题材") or "")
          or "天象" in _ch.get("why", ""),
          _ch.get("why"))
else:
    check("白天也照样算出壁纸（昼景）", bool(_ch.get("main")), f"{_slot} {_ch.get('why')}")
_ch2 = photos.choose_daily(cfg, ctx, _wx.peek(cfg))
check("第二次直接吃缓存", _ch2.get("cached") is True)
check("缓存键带时段", bool(_ch.get("slot")), _ch.get("slot"))

print("=== 17b. 壁纸轮换：分类 + 不重复 + 事件定向 ===")
# 事故复盘（两轮）：
#   ① 最早是「全库取分数最高的那张」→ 连着一周同一张；
#   ② 改成「按分数划 82% 的线再抽签」→ 分数挤、属性覆盖率低（320 张里只有
#      50 张带时段），池子常被压到十几张，还是那几张在转。
# 现在的验收标准：**先分类（光照×天气），再在分类里按「最久没用」轮换**；
# 事件发生时切到更小的事件分类，同样在里面轮换。

def _scene(day, slot, wx="", events=None, season="秋"):
    return {"iso": day.isoformat(), "slot": slot,
            "slotCn": "夜里" if slot == "夜" else "白天",
            "lightGroup": photos._SLOT_LIGHT[slot], "wxGroup": wx,
            "eventDetail": events or [], "时段": [], "天气": [],
            "季节": [season], "事件": [], "偏好": [], "festival": "", "weatherText": ""}


_tags_by_file = {str(i.get("file")): photos.strict_tags(i)
                 for i in photos._load_index()
                 if (photos.LIB_DIR / i.get("file", "")).exists()}

_hist: list = []
_rows: list = []
for _d in range(14):
    _day = datetime.date(2026, 12, 1) + datetime.timedelta(days=_d)
    for _slot in ("夜", "昼"):
        _got = photos.pick(cfg, {"iso": _day.isoformat(), "season": "秋"}, {},
                           limit=3, scene=_scene(_day, _slot), history=_hist)
        if _got:
            _rows.append((_day.isoformat(), _slot, _got[0]))
            _hist.append({"d": _day.isoformat(), "slot": _slot, "file": _got[0]["file"]})

_files = [m["file"] for _i, _s, m in _rows]
check("轮换：14 天 28 个时段拿到 28 张不同图",
      len(_files) == 28 and len(set(_files)) == 28, f"{len(set(_files))} 种 / {len(_files)} 次")
check("轮换：同一天的昼与夜不是同一张",
      all(a != b for (i1, _s1, a), (i2, _s2, b) in zip(_rows[::2], _rows[1::2])
          if i1 == i2))
_seed_scene = _scene(datetime.date(2026, 12, 2), "夜")
check("轮换：同一天同一时段是稳定的（刷新页面不乱跳）",
      photos.pick(cfg, {}, {}, limit=1, scene=_seed_scene, history=[])[0]["file"]
      == photos.pick(cfg, {}, {}, limit=1, scene=_seed_scene, history=[])[0]["file"])
check("轮换：没有缓存时 peek_chosen 返回 None",
      photos.peek_chosen(cfg, {"iso": "1999-01-01", "season": "春"}, {}) is None)

# 分类必须真的对上：夜里挑的是夜图，白天挑的是昼图
_night_bad = [m["title"][:20] for _i, _s, m in _rows
              if _s == "夜" and _tags_by_file.get(m["file"], {}).get("light") != "夜"]
_day_bad = [m["title"][:20] for _i, _s, m in _rows
            if _s == "昼" and _tags_by_file.get(m["file"], {}).get("light") != "昼"]
check("分类：夜间壁纸都来自「夜」类", not _night_bad, _night_bad[:3])
check("分类：白天壁纸都来自「昼」类", not _day_bad, _day_bad[:3])

# 天气只在分类够大时才单独成桶：只有一两张雨景时不能切过去来回跳
_rain = [f for f, t in _tags_by_file.items() if t.get("weather") == "雨"
         and t.get("light") == "昼"]
_p = photos.pick(cfg, {"iso": "2026-12-03"}, {}, limit=1,
                 scene=_scene(datetime.date(2026, 12, 3), "昼", wx="雨"), history=[])
check("分类：小桶不下沉（雨景不足 %d 张就不单开「昼×雨」）" % photos.MIN_BUCKET,
      bool(_p) and not _p[0]["category"].endswith("×雨"),
      f"{len(_rain)} 张雨景 / {_p[0]['category'] if _p else '无'}")

# 事件：英仙座流星雨极大期那晚必须切到事件分类，且挑中的确实是流星雨图
_ev_day = datetime.date(2026, 8, 11)      # night=True 会 +1 天 → 落在极大期
_evs = _at.astro_events(_ev_day, night=True)
check("事件：8/11 夜判定为英仙座流星雨极大期",
      any(e["event"] == "流星雨" and e.get("hard") for e in _evs),
      [(e["event"], e.get("hard")) for e in _evs])
_hist2: list = []
_picks: list = []
for _d in range(2):
    _day = _ev_day + datetime.timedelta(days=_d)
    _got = photos.pick(cfg, {"iso": _day.isoformat()}, {}, limit=1,
                       scene=_scene(_day, "夜", events=_at.astro_events(_day, night=True),
                                    season="夏"), history=_hist2)
    if _got:
        _picks.append(_got[0])
        _hist2.append({"d": _day.isoformat(), "slot": "夜", "file": _got[0]["file"]})
check("事件：切到「事件·」分类",
      bool(_picks) and _picks[0]["category"].startswith("事件·"),
      _picks[0]["category"] if _picks else "无")
check("事件：挑中的图带流星雨属性",
      bool(_picks) and "流星雨" in (_tags_by_file.get(_picks[0]["file"], {}).get("events") or set()),
      _picks[0]["title"][:24] if _picks else "无")
check("事件：事件分类里也要轮换（桶里多于一张时必须换）",
      len(_picks) == 2 and (_picks[0]["file"] != _picks[1]["file"] or _picks[0]["pool"] == 1),
      [(p["title"][:16], p.get("pool")) for p in _picks])

# 图表 / 示意图不能当壁纸：事件分类里最容易混进来（NASA 的光谱图、软件合成图）
_bad_photo = [m["title"][:20] for _i, _s, m in _rows if not photos.is_photo_like(m)]
check("选图：图表/示意图类素材被排除", not _bad_photo, _bad_photo[:3])

print("=== 17c. 大分类：互斥 + 循环 ===")
_all_items = [i for i in photos._load_index()
              if (photos.LIB_DIR / i.get("file", "")).exists() and photos.is_photo_like(i)]
check("每张图都归到 MACRO_ORDER 里的一个大类",
      all(photos.macro_of(i) in photos.MACRO_ORDER for i in _all_items), len(_all_items))
_hist3, _day_picks = [], []
for _k in range(10):
    _day = datetime.date(2026, 11, 1) + datetime.timedelta(days=_k)
    _got = photos.pick(cfg, {"iso": _day.isoformat()}, {}, limit=1,
                       scene=_scene(_day, "昼"), history=_hist3)
    if _got:
        _day_picks.append(_got[0])
        _hist3.append({"d": _day.isoformat(), "slot": "昼",
                       "file": _got[0]["file"], "cat": _got[0]["category"]})
_seq = [m["macro"] for m in _day_picks]
check("大类循环：相邻两次不是同一个大类",
      all(a != b for a, b in zip(_seq, _seq[1:])), _seq)
check("大类循环：10 天覆盖 5 个以上大类", len(set(_seq)) >= 5, _seq)
_idx = [photos.MACRO_ORDER.index(m) for m in _seq if m in photos.MACRO_ORDER]
# 原来这里断言「只往后走，不走回头路」。但大类层已经改成**按最久没用驱动**
# （固定 +1 的游标会被规律的时段锁进短轨，见 _next_macro 注释），回头是合法的。
# 真正该保证的是：一个轮转周期之内不重复同一个大类。
# 阈值是 6 不是 7：白天（昼）够得上 MIN_MACRO_AVAIL 的只有 6 个大类
# （天象被光照排除），6 个类轮一圈必然回到第一个 —— 要求 7 次不重复在数学上不可能。
# 实测序列 天气→景点→建筑→水景→植物→其他→天气，正好间隔 6 天。
check("大类循环：连续 6 次不重复同一个大类",
      all(len(set(_seq[i:i + 6])) == len(_seq[i:i + 6]) for i in range(max(0, len(_seq) - 5))),
      _seq)
check("大类与光照一致：白天不出「天象」夜景",
      all(m["macro"] != "天象" for m in _day_picks), _seq)

# 回归：防「短轨」。曾经大类游标是每次 +1，而时段规律（夜/昼/昏）、每个时段
# 能选的大类又固定，于是锁死成 天象→水景→植物→天象 的 3 步循环。
_h4, _m4 = [], []
for _k in range(9):
    _d4 = datetime.date(2026, 12, 1) + datetime.timedelta(days=_k)
    for _sl4, _ph4 in (("夜", "夜色"), ("昼", "正午强光"), ("昏", "蓝调时刻")):
        _g4 = photos.pick(cfg, {"iso": _d4.isoformat()},
                          {"ok": True, "city": "北京市", "text": "晴",
                           "temp": 20, "phase": _ph4},
                          limit=1, scene=None, history=_h4)
        if _g4:
            _m4.append(_g4[0]["macro"])
            _h4.append({"d": _d4.isoformat(), "slot": _sl4,
                        "file": _g4[0]["file"], "cat": _g4[0]["category"],
                        "macro": _g4[0]["macro"]})
check("大类轮换：昼夜昏循环 9 天仍覆盖 5 个以上大类（防短轨）",
      len({m for m in _m4 if m in photos.MACRO_ORDER}) >= 5,
      sorted({m for m in _m4 if m in photos.MACRO_ORDER}))

print("=== 17d. 事件加权插队（权重 / 冷却 / 互斥 / 流星雨专闸）===")
_NIGHT = [i for i in _all_items if photos.strict_tags(i)["light"] == "夜"]
_METEOR = {"event": "流星雨", "tags": ["流星雨", "英仙座流星雨"], "title": "英仙座流星雨",
           "note": "极大期", "score": 1.0, "hard": True}
_MOON = {"event": "满月", "tags": ["满月"], "title": "满月", "note": "月光很亮",
         "score": 0.6, "hard": True}
_DAY = datetime.date(2026, 8, 12)


def _hist(days_ago, ev=""):
    if days_ago is None or not _NIGHT:
        return []
    d = (_DAY - datetime.timedelta(days=days_ago)).isoformat()
    return [{"d": d, "slot": "夜", "file": _NIGHT[0]["file"],
             "cat": ("事件·" + ev) if ev else "天象·夜", "ev": ev}]


def _ev_pick(events, hist):
    got = photos.pick(cfg, {"iso": _DAY.isoformat()}, {}, limit=1,
                      scene=_scene(_DAY, "夜", events=events), history=hist)
    return got[0] if got else None


if _NIGHT:
    _m = _ev_pick([_METEOR], _hist(0))
    check("事件：天象刚出过（gap=0）→ 流星雨不插队",
          bool(_m) and not _m["category"].startswith("事件·"),
          _m["category"] if _m else "无")
    _m = _ev_pick([_METEOR], _hist(5))
    check("事件：天象 5 天没出 → 流星雨插队（权重 0.58）",
          bool(_m) and _m["category"].startswith("事件·") and _m.get("eventWeight", 0) >= photos.EVENT_JUMP_MIN,
          f"{_m['category']} / {_m.get('eventWeight')}" if _m else "无")
    _m = _ev_pick([_MOON, _METEOR], _hist(5))
    check("事件：满月与流星雨同晚 → 满月赢（互斥）",
          bool(_m) and _m.get("event") == "满月", _m.get("event") if _m else "无")
    _h = _hist(4, ev="流星雨") + [{"d": (_DAY - datetime.timedelta(days=9)).isoformat(),
                                  "slot": "夜", "file": _NIGHT[min(1, len(_NIGHT) - 1)]["file"],
                                  "cat": "天象·夜", "ev": ""}]
    _m = _ev_pick([_METEOR], _h)
    check("事件：流星雨插队后 12 天冷却内不再插",
          bool(_m) and not _m["category"].startswith("事件·"), _m["category"] if _m else "无")
    _h = _hist(5) + [{"d": _DAY.isoformat(), "slot": "昼",
                      "file": _all_items[0]["file"], "cat": "事件·满月", "ev": "满月"}]
    _m = _ev_pick([_METEOR], _h)
    check("事件：一天最多一次插队", bool(_m) and not _m["category"].startswith("事件·"),
          _m["category"] if _m else "无")
    _w, _ = photos._event_weight(_MOON, _hist(1, ev="满月"),
                                 {str(i["file"]): "天象" for i in _NIGHT}, _DAY.isoformat())
    check("事件：插队后权重被衰减", _w < photos.EVENT_WEIGHT["满月"], round(_w, 2))
    _m = _ev_pick([], _hist(5))
    check("事件：没有事件时正常走大分类（夜里=天象）",
          bool(_m) and _m["macro"] == "天象" and not _m["event"], _m["macro"] if _m else "无")


# record_use 会写真实缓存 —— 存一份、测完还原，别污染用户的历史
_hist_backup = store.read_cache(photos.HISTORY_KEY)
try:
    photos.record_use({"iso": "2000-01-01", "slot": "夜"}, "_selftest_a.jpg", "测试A")
    photos.record_use({"iso": "2000-01-01", "slot": "夜"}, "_selftest_b.jpg", "测试B")
    _h = photos._load_history()
    _same = [r for r in _h if r.get("d") == "2000-01-01" and r.get("slot") == "夜"]
    check("轮换：同一天同一时段只留最后一条",
          len(_same) == 1 and _same[0].get("file") == "_selftest_b.jpg", _same)
    check("轮换：历史记录了日期与时段",
          bool(_same) and _same[0].get("d") == "2000-01-01" and _same[0].get("slot") == "夜")
finally:
    store.write_cache(photos.HISTORY_KEY, _hist_backup or [])

print("=== 17c. 每日一句：介绍壁纸，而不是万能励志句 ===")
# 原来没模型时用的是「把一件小事做到今天的极限」这类句子，跟墙上那张图毫无关系。
_wp0 = _rows[0][2] if _rows else None
if _wp0:
    _q0, _src0 = ai.wallpaper_quote(_wp0, {"season": "秋", "dateText": "2026年12月1日"},
                                    {"ok": True, "text": "晴", "temp": 18})
    check("介绍提到了这张图本身", ("《" in _q0) or ("画面是" in _q0), _q0[:40])
    check("介绍不带个性化尾巴（今天/外面/温度）",
          all(t not in _q0 for t in ("今天", "外面", "°C", "正合适")), _q0[:60])
_long = {"title": "银河挂在雪山上方", "credit": "ESA/Hubble", "file": "x.jpg",
         "desc": "银河从山脊后面升起来，雪线在夜色里发亮。山脚下的小湖平得像一面镜子，"
                 "把整条银河又翻了一遍。风停着，连草叶都一动不动。"}
_q3, _ = ai.wallpaper_quote(_long, {}, {})
check("介绍有实质内容（长描述能带出来）", len(_q3) >= 45, f"{len(_q3)} 字")
_q_non, _ = ai.wallpaper_quote(None, {"season": "秋"}, {})
check("没有壁纸时兜底不崩", len(_q_non) > 5, _q_non[:30])
_pl = ai.build_prompt(
    cfg, {"iso": "2026-12-01", "dateText": "2026年12月1日", "weekday": "星期一",
          "season": "秋", "festival": ""},
    {"ok": True, "city": "北京市", "text": "晴", "temp": 12, "phase": "正午"},
    fast=True, wallpaper=_wp0)
check("提示词里带上了「今天墙上这张照片」", "今天墙上这张照片" in _pl["user"])
check("主提示词不再要求模型写每日一句（介绍走每图绑定）",
      "quote" not in _pl["user"], [ln for ln in _pl["user"].splitlines() if "quote" in ln][:2])
check("提示词不再要求 15-40 字短句", "15-40 字" not in _pl["user"])
# 壁纸按时段会换 —— 「按天写句子、对不上就重写」的机制已整体退役：
# 每张图有自己的介绍并永久绑定（见 photos.intro_batch），显示取图不带句
check("「每天一句话」的重写程序已退役",
      not hasattr(ai, "refresh_quote") and not hasattr(ai, "quote_prompt"))
_fd = ai._fallback_daily({"dateText": "2026年12月1日", "season": "秋"},
                         {"ok": True, "city": "北京市", "text": "晴"}, _wp0)
check("兜底句记下了「描述的是哪张图」",
      _fd.get("quoteFor") == (_wp0 or {}).get("file"), _fd.get("quoteFor"))

print("=== 17d. 每图一介：介绍与图片绑定，替掉每天一句话 ===")
# ① 落盘字段：介绍直接从索引里拿
_it0 = next((i for i in photos._load_index()
             if isinstance(i, dict) and i.get("file")), None)
_picked = photos.pick(cfg, {}, {}, limit=1, scene=_seed_scene, history=[])
check("pick 返回里带 intro 字段", bool(_picked) and "intro" in _picked[0],
      list(_picked[0].keys()) if _picked else "空")
# ② 标签清洗：模型自造词进不了打分计算
san = photos.sanitize_tags({"题材": ["天象", "自造词"], "时段": ["夜晚", "夜里"],
                            "未知的层": ["天象"], "季节": None})
check("标签清洗只认词表里的类别名",
      san == {"题材": ["天象"], "时段": ["夜晚"]}, san)
# ③ 本地兜底介绍：没模型也有内容，且不带时间气味
_li = photos.local_intro({"title": "银河挂在雪山上方", "credit": "ESA/Hubble",
                          "desc": "milky way over snowy range at night",
                          "src": "astro", "file": "x.jpg"})
check("兜底介绍非空且为主题", len(_li) >= 14 and "今天" not in _li, _li)
check("兜底介绍不写天气尾巴", "°C" not in _li and "外面是" not in _li)
# ④ 视觉提示词：词表齐全、要求 JSON、禁止编造
_vip = photos._vision_intro_prompt({"title": "aurora over fjord", "credit": "ESA/Webb",
                                    "desc": "green aurora", "src": "astro", "file": "y.jpg"})
check("视觉提示词含全部词表层",
      all(f"{layer}(" in _vip for layer in photos._attrs.ATTRS), _vip[:120])
check("视觉提示词只让学生输出 JSON", "只输出 JSON" in _vip)
check("视觉提示词禁止编造", "不得编造" in _vip)
check("视觉提示词不提今天", "不要提「今天」" in _vip)
# ⑤ intro_progress 统计说得通
_prog = photos.intro_progress()
check("进度统计：done+local+none == total",
      _prog["done"] + _prog["local"] + _prog["none"] == _prog["total"], _prog)

print("=== 17e. 介绍按需 + 选图缓存防黑 ===")
# 事故：缓存里今天选的那张图后来被同步/淘汰掉了，前端就显示一片黑。
# 现在从缓存取图先验文件在不在，不在就重算。
_sc_now = photos.scene_of(cfg, ctx, _wx.peek(cfg))
_key_now = f"wallpaper-{_sc_now['iso']}-{_sc_now['slot']}"
_backup_now = store.read_cache(_key_now)
try:
    store.write_cache(_key_now, {
        "iso": _sc_now["iso"], "slot": _sc_now["slot"], "why": "占位",
        "main": {"file": "_selftest_missing.jpg", "title": "已不在图库", "src": "x"},
        "alts": [], "computedAt": "2000-01-01T00:00:00"})
    _rec = photos.choose_daily(cfg, ctx, _wx.peek(cfg))
    _rec_main = (_rec or {}).get("main") or {}
    check("缓存指向不存在的图会重算，不再糊黑图",
          bool(_rec_main) and (photos.LIB_DIR / str(_rec_main.get("file"))).is_file(),
          _rec_main.get("file"))
    check("重算结果不是吃缓存", _rec.get("cached") is not True)
finally:
    if _backup_now is not None:
        store.write_cache(_key_now, _backup_now)
    else:
        try:
            store.cache_path(_key_now).unlink()
        except OSError:
            pass

check("intro_for 对库外文件返回空", photos.intro_for("__not_in_library__.jpg") == {})
check("peek_chosen 不会返回不存在的图",
      (photos.peek_chosen(cfg, ctx, _wx.peek(cfg)) is None)
      or photos._file_ok((photos.peek_chosen(cfg, ctx, _wx.peek(cfg)) or {}).get("file")))

_st = main.build_state()
_st_main = ((_st.get("photos") or {}).get("chosen") or {}).get("main") or {}
check("首页介绍绑定当前壁纸（quoteFor == 壁纸文件）",
      _st["daily"].get("quoteFor") == str(_st_main.get("file") or ""),
      f"{_st['daily'].get('quoteFor')} vs {_st_main.get('file')}")
check("首页介绍不为空（本地也要有内容）",
      bool(_st["daily"].get("quote")) or not _st_main,
      (_st["daily"].get("quote") or "")[:40])
check("首页介绍不带个性化尾巴",
      all(t not in (_st["daily"].get("quote") or "") for t in ("外面是", "°C", "看它正合适")))

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

print("\n=== 21. 深度搜索：排队与结果过滤 ===")
import tempfile as _tf
import search as _search
from pathlib import Path as _P

_tmp2 = _P(_tf.mkdtemp(prefix="wb-search-"))
_old_q, _old_r = _search.QUEUE_DIR, _search.RESULT_DIR
_search.QUEUE_DIR = _tmp2 / "queue"
_search.RESULT_DIR = _tmp2 / "result"
try:
    r1 = _search.submit("国产大模型 价格战")
    r2 = _search.submit("国产大模型，价格战！")      # 归一化后应命中同一个任务
    check("提交成功", r1.get("ok") is True)
    check("归一化后是同一个任务 id", r1["task"]["id"] == r2["task"]["id"],
          (r1["task"]["id"], r2["task"]["id"]))
    check("队列里只有 1 个任务", len(_search.pending()) == 1, len(_search.pending()))
    check("重复提交会累计次数", r2["task"]["times"] == 2, r2["task"]["times"])

    tid = r1["task"]["id"]
    check("没结果时状态是 queued", _search.get("国产大模型 价格战")["status"] == "queued")

    # 造一份结果：其中一条没有 url，应该被丢掉
    import json as _json
    _search.RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (_search.RESULT_DIR / f"{tid}.json").write_text(_json.dumps({
        "id": tid, "topic": "国产大模型 价格战",
        "doneAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "brief": "测试用综述。", "points": ["要点一"],
        "sources": [
            {"title": "转载", "url": "https://baijiahao.baidu.com/s?id=1", "tier": 3},
            {"title": "官方", "url": "https://www.deepseek.com/pricing", "tier": 1},
            {"title": "没有出处", "url": "", "tier": 2},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    got = _search.get("国产大模型 价格战")
    check("读到结果", got["status"] == "ready", got["status"])
    check("丢掉没有 url 的来源", len(got["sources"]) == 2, len(got["sources"]))
    check("一手源排在前面", got["sources"][0]["tier"] == 1, got["sources"][0]["tier"])
    check("带时区的时间不报错（这里踩过 TypeError）", got["stale"] is False, got["stale"])
finally:
    _search.QUEUE_DIR, _search.RESULT_DIR = _old_q, _old_r

print("=== 18. 排序：收缩目标 / 证据量 / 探索配额 ===")
import rank as _rank  # noqa: E402


def _row(title, src, cat, score, days_ago=0.0):
    at = (datetime.datetime.now().astimezone()
          - datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")
    return {"title": title, "source": src, "cat": cat, "score": score, "at": at}


_rows = [
    _row("AI 泡沫破裂分析", "路透", "财经", 10),
    _row("景顺：AI 建设必要调整", "格隆汇", "财经", 10),
    _row("杜德利警告 AI 基建", "财联社", "财经", 10),
    _row("锂电产业链周记", "界面", "财经", 5),
    _row("朝鲜会谈", "中新网", "要闻", 5),
    _row("能源论坛西安", "中新网", "要闻", 5),
]
_st = _rank._aggregate(_rows, [])

# 这是上一版最要命的错：收缩目标钉在 5.5，而人只给在意的东西打分（均分 7.6），
# 于是「你给 10 分的来源」贝叶斯均分反而低于全站均分，权重是负的。
check("个人均分跟着实际评分走（不是钉在 5.5）", _st["muUser"] > 6.5, _st["muUser"])
check("1 条 10 分的来源算出来高于我的均值",
      _st["sources"]["财联社"]["mu"] > _st["muUser"],
      f"{_st['sources']['财联社']['mu']} vs {_st['muUser']}")
check("2 条 5 分的来源算出来低于我的均值",
      _st["sources"]["中新网"]["mu"] < _st["muUser"],
      f"{_st['sources']['中新网']['mu']} vs {_st['muUser']}")
check("负面证据比正面证据可信（同样条数置信度更高）",
      _st["sources"]["中新网"]["confNeg"] > _st["sources"]["财联社"]["conf"],
      f"{_st['sources']['中新网']['confNeg']} vs {_st['sources']['财联社']['conf']}")

_one = _rank._aggregate([_row("a", "S", "科技", 10)], [])
_four = _rank._aggregate([_row(f"a{i}", "S", "科技", 10) for i in range(4)], [])
check("评 1 条只出一小部分力气", _one["sources"]["S"]["conf"] < 0.25,
      _one["sources"]["S"]["conf"])
check("评 4 条才出一半以上", _four["sources"]["S"]["conf"] >= 0.5,
      _four["sources"]["S"]["conf"])

check("评分数不到 3 条时不动排序",
      _rank._item_score({"title": "x", "source": "S", "cat": "科技"},
                        _rank._aggregate(_rows[:2], []), 0.0)["cold"] is True)

_stale = _rank._aggregate([_row("a", "S", "科技", 10, days_ago=180)], [])
check("半年前的评分只剩一点点力气", _stale["sources"]["S"]["conf"] < 0.15,
      _stale["sources"]["S"]["conf"])

# 探索配额：把陌生来源的内容分压到最低，它们仍然要能挤进列表
_old_stats = _rank.stats
_rank.stats = lambda force=False: _st
try:
    check("10 条留 2 个探索位", _rank.explore_slots(10) == 2)
    check("只有 5 条就不切", _rank.explore_slots(5) == 0)
    _pool = ([{"title": f"熟悉{i}", "source": "财联社", "cat": "财经"} for i in range(4)]
             + [{"title": f"陌生{i}", "source": f"陌生源{i}",
                 "cat": ["科技", "学术", "要闻"][i % 3]} for i in range(8)])
    _base = {f"陌生{i}": -10.0 for i in range(8)}
    _out = _rank.rerank(_pool, 10, _base)
    check("重排后条数够", len(_out) == 10, len(_out))
    _seen = [p for p in _out if str(p["source"]).startswith("陌生源")]
    check("内容分垫底的陌生来源也能占 2 个位子", len(_seen) >= 2, len(_seen))
    _cnt = {}
    for p in _out:
        _cnt[p["source"]] = _cnt.get(p["source"], 0) + 1
    check("来源够多时同来源不超过 2 条", max(_cnt.values()) <= 2, _cnt)
    # 池子太小时只能放宽，但不能因此给出半页
    _thin = [{"title": f"x{i}", "source": "唯一源", "cat": "要闻"} for i in range(4)]
    check("池子只有 4 条时也不给半页", len(_rank.rerank(_thin, 10, {})) == 4)
    check("结果是确定的（同一天刷新不跳）",
          [p["title"] for p in _rank.rerank(_pool, 10, _base)] ==
          [p["title"] for p in _out])
finally:
    _rank.stats = _old_stats

print("=== 19. 收藏：去重 / 取消 / 权限隔离 ===")
import bookmarks as _bm  # noqa: E402
import tempfile as _tf   # noqa: E402

_old_bm_path = _bm.PATH
_bm.PATH = Path(_tf.mkdtemp(prefix="bmtest-")) / "bookmarks.json"
_bm._cache = None
_bm._mtime = 0.0
try:
    check("空收藏是空列表", _bm.load() == [], _bm.load())

    _a = {"title": "路透：AI 泡沫", "url": "https://example.com/a",
          "source": "路透", "cat": "财经"}
    _r1 = _bm.toggle("news", _a)
    check("收藏成功", _r1["ok"] and _r1["on"] is True and _r1["count"] == 1, _r1["count"])
    _got = _bm.load()
    check("存下了标题与链接", _got[0]["title"] == _a["title"] and _got[0]["url"] == _a["url"])
    check("存下了收藏时间", bool(_got[0].get("savedAt")), _got[0].get("savedAt"))
    check("id 一样：同链接算同一条",
          _bm.key_for("news", _a) == _bm.key_for("news", dict(_a, title="改了标题")))
    check("id 不一样：资讯和仓库不串",
          _bm.key_for("news", _a) != _bm.key_for("github", _a))

    _r2 = _bm.toggle("news", _a)
    check("再点一次是取消", _r2["on"] is False and _r2["count"] == 0, _r2["count"])

    _bm.toggle("news", _a)
    _b = {"name": "owner/repo", "url": "https://github.com/owner/repo",
          "lang": "Rust", "stars": "1.2k"}
    _bm.toggle("github", _b)
    _list = _bm.load()
    check("资讯和仓库能共存", len(_list) == 2, len(_list))
    check("新的排在前面", _list[0]["kind"] == "github", _list[0]["kind"])
    check("仓库名从 name 取", _list[0]["title"] == "owner/repo", _list[0]["title"])

    _bm.remove(_list[0]["id"])
    check("移除一条", len(_bm.load()) == 1, len(_bm.load()))
    check("移除不存在的不报错", _bm.remove("nope")["ok"] is True)

    check("没有链接也没有标题：收不了",
          _bm.toggle("news", {"title": "", "url": ""})["ok"] is False)
    _c = {"title": "只有标题的条目"}
    check("只有标题也能收", _bm.toggle("news", _c)["on"] is True)
finally:
    _bm.PATH = _old_bm_path
    _bm._cache = None
    _bm._mtime = 0.0

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
