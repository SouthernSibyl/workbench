"""工作台 —— 本地快捷启动面板。

单进程本地服务：启动 → 开窗口 → 关窗口自动退出。
个性化内容后台生成、启动时只读缓存，所以开面板永远是「秒开」。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as _dt
import json
import os
import socket
import socketserver
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import actions
import ai
import bookmarks
import calendar_cn
import logbook
import rank as ranker
import search
import miniwin
import photos
import settings_schema
import sources
import store
import todos as todo_logic
import weather

APP_NAME = "工作台"
APP_VERSION = "1.0.0"
# 排序算法版本。改了评分算法就 +1：当天已生成的列表会按新算法重排一次
# （见 _migrate_rank_version），否则分数会在列表里乱序，看着像没生效。
# 1 = 老版（收缩目标钉在 5.5）  2 = 分层收缩 + 主题优先 + 探索配额
# 3 = 加「已经打过分的条目按那条分微调」，列表顺序与分数徽标不再打架
RANK_VERSION = 3
PORT_CANDIDATES = list(range(39217, 39240))

LOCK = threading.RLock()
STATE: dict = {
    "startedAt": time.time(),
    "generating": False,
    "note": "准备中",
    "aiError": "",
    "lastPing": 0.0,
    "pings": 0,
    "rev": 0,
    # 批量生成简述的进度：{"running":bool,"done":n,"total":m}
    "expanding": {"running": False, "done": 0, "total": 0},
    # 到点但还没被任何页面「接走」的提醒。谁先看到谁弹，弹完 ack 掉，
    # 这样主窗和小窗同时开着也只响一次。
    "ring": [],
}

_SERVER: ThreadingHTTPServer | None = None

# 那个应用窗口的信息（open_window 的返回值 + 后来认到的窗口句柄）。
# 看门狗靠 `hwnd` 判断「窗口还在不在」—— 不用 pid，Edge 会交接进程。
WINDOW: dict = {"pid": None, "exe": "", "fallback": False, "hwnd": None}


def _bump_rev() -> None:
    """告诉界面「有新东西了」，让它重新拉一次 state。"""
    with LOCK:
        STATE["rev"] += 1


# ------------------------------------------------------------------ 资源定位

def web_dir() -> Path:
    here = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        for cand in (base / "web", Path(getattr(sys, "_MEIPASS", base)) / "web"):
            if cand.is_dir():
                return cand
    return here / "web"


MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


# ------------------------------------------------------------------ 状态组装

def _latest_daily() -> tuple[dict | None, str]:
    best: tuple[float, dict, str] | None = None
    if store.CACHE_DIR.is_dir():
        for f in store.CACHE_DIR.glob("daily-*.json"):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
                ts = f.stat().st_mtime
            except (OSError, ValueError):
                continue
            if best is None or ts > best[0]:
                best = (ts, obj, f.stem.replace("daily-", ""))
    if best is None:
        return None, ""
    obj, date = best[1], best[2]
    return _migrate_rank_version(obj, date), date


def _migrate_rank_version(obj: dict, date: str) -> dict:
    """排序算法换代后，把当天已经排版好的列表按新算法重排一次。

    不这么做的话，界面上的分数会一路乱序（8 分排在 10 分前面），
    看着像算法没生效 —— 而原因只是那份列表是旧算法排的。
    一天只排一次，不是每次刷新都排：读着读着列表跳来跳去比乱序更糟。
    """
    news = obj.get("news") or {}
    items = news.get("items") or []
    if not items or int(news.get("rankVersion") or 0) >= RANK_VERSION:
        return obj
    try:
        items.sort(key=lambda it: -ranker.explain(it)["score"])   # 稳定排序，同分不动
        news["items"] = items
        news["rankVersion"] = RANK_VERSION
        obj["news"] = news
        store.write_cache(f"daily-{date}", obj)
        store.log(f"按新排序重排 {date} 的 {len(items)} 条")
    except Exception as exc:
        store.log(f"重排失败（不影响显示）：{exc}")
    return obj


def _art_candidates(ctx: dict) -> dict:
    """找本地美术素材：节日图 / 当日图。美术外包后可把图片丢进 art 目录。"""
    out = {"holiday": [], "daily": []}
    names = [ctx.get("festival") or "", ctx["iso"],
             f"{ctx['month']:02d}-{ctx['day']:02d}",
             f"{ctx['month']}-{ctx['day']}"]
    holiday = store.ART_DIR / "holiday"
    if holiday.is_dir():
        for f in sorted(holiday.iterdir()):
            if not f.is_file():
                continue
            stem = f.stem
            if any(n and (stem == n or stem.startswith(n + "-") or stem.startswith(n + "_")) for n in names):
                out["holiday"].append(f"/art/holiday/{f.name}")
    daily = store.ART_DIR / "daily"
    if daily.is_dir():
        for f in sorted(daily.iterdir()):
            if f.is_file() and f.stem in (ctx["iso"], f"{ctx['month']:02d}-{ctx['day']:02d}"):
                out["daily"].append(f"/art/daily/{f.name}")
    return out


# 「每天一句话」这套「壁纸按时段换、句子按天写、对不上就后台重写」的程序已退役 ——
# 它永远在追赶壁纸。替代机制：**每张图有自己的介绍**（存进索引、与图永久绑定），
# 显示时直接取「现在墙上这张」。介绍不再全库预生成：显示到哪张，才为哪张现写；
# 有模型时后台只把这一张升级成识图版，界面签名里带 quoteFor，变了就重画。

_INTRO_KICK = {"file": "", "at": 0.0}
_INTRO_KICK_LOCK = threading.Lock()


def _ensure_intro_one(file: str, min_interval: int = 600) -> None:
    """按需给「现在这张」补正式介绍（后台线程，同一张图有节流）。

    不做全库预热：一次只处理当前显示的那张，换图才会轮到下一张。
    """
    file = str(file or "")
    if not file:
        return
    cfg = store.load_config()
    try:
        if not ai.ai_ready(cfg):
            return          # 没有 Key：本地介绍已经在了，等 Key 回来再升级
    except Exception:
        return

    with _INTRO_KICK_LOCK:
        now = time.time()
        if _INTRO_KICK["file"] == file and now - _INTRO_KICK["at"] < min_interval:
            return
        _INTRO_KICK.update(file=file, at=now)

    def job():
        try:
            res = photos.intro_one(cfg, file)
            if res.get("changed"):
                _bump_rev()
        except Exception as exc:
            store.log(f"intro one failed: {exc}")

    threading.Thread(target=job, name="wb-intro-one", daemon=True).start()


def _with_rank_why(item: dict) -> dict:
    """补上 summaryBy / rated / rankWhy 三个界面要用的字段。

    rankWhy 是「这条为什么在这个位置」。算法只在内部用分数，理由算完就丢，
    界面上就永远看不出它跑偏了 —— 而跑偏恰恰是最难自己发现的那种错。
    """
    out = dict(item,
               summaryBy=ai.summary_by(item),
               rated=ranker.score_of(item.get("title") or ""),
               # bmKey：这条在不在收藏里，前端按 id 比对。id 由后端算，
               # 前端自己拼哈希迟早两边对不上（改一次规范化就全丢）。
               bmKey=bookmarks.key_for("news", item))
    try:
        out["rankWhy"] = "；".join(ranker.explain(item).get("reasons") or [])
    except Exception:
        out["rankWhy"] = ""
    return out


def build_state() -> dict:
    cfg = store.load_config()
    ann = cfg.get("anniversaries") or []
    ctx = calendar_cn.today_context(None, ann)

    # 天气只读缓存、绝不联网。缓存过期就在这里踢一次后台刷新，
    # 界面拿到的是「可能略旧但立刻可用」的数据 —— 请求路径上不许有网络 I/O。
    wx = weather.peek(cfg)
    if not wx.get("ok") or wx.get("stale"):
        weather.refresh_async(cfg, on_done=_bump_rev)

    daily = store.read_cache(f"daily-{ctx['iso']}")
    if daily is not None:
        daily = _migrate_rank_version(daily, ctx["iso"])
    is_today = daily is not None
    if daily is None:
        daily, _old_date = _latest_daily()
    daily = daily or {}

    # 壁纸是「算」出来的：按今天的天气/时段/季节/天象事件打分，
    # 一天（严格说是一个时段）只算一次，算完存起来，前端只管显示。
    chosen = photos.choose_daily(cfg, ctx, wx)
    wmain = (chosen.get("main") or {}) if isinstance(chosen, dict) else {}
    wfile = str(wmain.get("file") or "")

    # 介绍以索引里的最新值为准（壁纸缓存可能是几天前算的）。没有就现写一条
    # 本地介绍 —— 只写这一张，不做全库预生成。
    bound = photos.intro_for(wfile) if wfile else {}
    if not bound.get("intro") and wfile:
        bound = photos.ensure_local_intro(wfile)
    intro = str(bound.get("intro") or "").strip()
    intro_by = str(bound.get("introBy") or "").strip()
    if wfile:
        wmain["intro"] = intro
        # 有模型时把「现在这张」升级成识图/文字版（后台，按图节流）
        if intro_by not in ("vision", "text"):
            _ensure_intro_one(wfile)

    # 备选也覆盖成各自绑定的最新介绍。没有介绍的就现算一条本地版带在响应里
    # ——但不落盘：按 N 换图可能只是看看，没必要每刷一次就写 10 条进索引。
    # 真正切到哪张，那边的 ensure_local_intro 会负责落盘。
    for _h in ((chosen.get("alts") or []) if isinstance(chosen, dict) else []):
        if not isinstance(_h, dict):
            continue
        _b = photos.intro_for(_h.get("file"))
        if _b.get("intro"):
            _h["intro"] = _b["intro"]
        else:
            _live = photos.local_intro(_h)
            if _live:
                _h["intro"] = _live

    fields = (daily.get("fields") or {})
    hero = ([chosen["main"]] if chosen.get("main") else []) + (chosen.get("alts") or [])

    with LOCK:
        generating = STATE["generating"]
        note = STATE["note"]
        rev = STATE["rev"]
        ring = [dict(r) for r in STATE["ring"]]
    # 待办在这里统一洗一遍：老配置里手写的条目也补齐 remindAt/reminded 这些字段
    todo_items = store.normalize_todos(cfg.get("todos"))

    return {
        "app": {
            "name": APP_NAME,
            "version": APP_VERSION,
            "dataDir": str(store.DATA_DIR),
            "artDir": str(store.ART_DIR),
            "webDir": str(web_dir()),
            "startedAt": STATE["startedAt"],
        },
        "now": {
            "iso": _dt.datetime.now().isoformat(timespec="seconds"),
            "clock": _dt.datetime.now().strftime("%H:%M:%S"),
        },
        "today": ctx,
        "weather": wx,
        "art": _art_candidates(ctx),
        "photos": {"hero": hero[:10], "chosen": chosen, **photos.stats()},
        "daily": {
            # 绑定显示：只用「当前这张图的介绍」。绝不退回另一张图的旧句子，
            # 否则图换了字没换，就成了「讲冰岛、墙上星系」。
            "quote": intro,
            "quoteFrom": str(wmain.get("credit") or wmain.get("src") or "").strip(),
            "quoteFor": wfile,
            "introBound": intro_by in ("vision", "text"),
            "recommend": (daily.get("fields") or {}).get("recommend") or {},
            "tags": daily.get("tags") or (daily.get("fields") or {}).get("tags") or [],
            "brief": (daily.get("news") or {}).get("brief") or "",
            # GitHub 涨星速递：外部代写方检索后随答案一起收回的可选段（ai.py 合并）
            # bmKey 同资讯：能不能收藏由后端算好，前端只做比对
            "github": [dict(g, bmKey=bookmarks.key_for("github", g))
                       for g in (daily.get("github") or [])],
            # summaryBy：这条简述是谁写的（ai / local）。本地重排的也要让界面
            # 标出来，用户才知道「这段是本地拼的，模型版稍后到」。
            # rated：我给它打过几分（没评过是 None）。评分入口不只一个，
            # 但回显只有这一处 —— 列表和抽屉都读它，才不会一边显示 8 一边显示空。
            # rankWhy：这条为什么排在这个位置。以前 rank.explain 只返回分数，
            # 理由算完就丢了，界面上根本看不到 —— 于是「算法跑偏了」永远发现不了。
            "items": [_with_rank_why(i) for i in
                      ((daily.get("news") or {}).get("items") or [])],
            "newsSource": (daily.get("news") or {}).get("source") or "",
            "poolSize": (daily.get("news") or {}).get("poolSize") or 0,
            "generatedBy": daily.get("generatedBy") or "",
            "generatedAt": daily.get("generatedAt") or "",
            "aiError": daily.get("aiError") or "",
            "isToday": is_today,
            # 这份内容是今天生成的，还是昨晚预生成留到现在的？
            "overnight": bool(daily) and str(daily.get("generatedAt") or "")[:10] != ctx["iso"],
            "cacheKey": f"daily-{ctx['iso']}",
            "readMinutes": round(ai.read_budget(cfg) /
                                 max(1, int((cfg.get("news") or {}).get("chars_per_minute") or 400)), 1),
            "perItemChars": ai.per_item_chars(cfg),
            "expanded": sum(1 for i in ((daily.get("news") or {}).get("items") or [])
                            if i.get("summary")),
            # 排序现在到底按什么在排 —— 直接引用 rank.summary()，
            # 前端不再自己拼这句话，否则两处措辞迟早对不上。
            "rank": ranker.summary(),
        },
        "todos": todo_items,
        # 收藏：跨天的心愿单，不跟 daily 走（缓存会被明天覆盖）
        "bookmarks": bookmarks.load(),
        "reminders": {
            # 到点的提醒（谁先接走谁弹，见 STATE["ring"] 与 /api/todos 的 reminder_ack）
            "ring": ring,
            "next": todo_logic.next_upcoming(todo_items, 3),
            "stats": todo_logic.summary(todo_items),
            "horizonHours": todo_logic.horizon_hours(cfg),
            # 上限（不是当前配置值）：界面据此限制输入框，两边不会各写一个数
            "capHours": todo_logic.HORIZON_MAX,
            "capAt": todo_logic.fmt(todo_logic.now()
                                    + _dt.timedelta(hours=todo_logic.HORIZON_MAX)),
            "now": _dt.datetime.now().isoformat(timespec="seconds"),
            "sound": bool((cfg.get("reminder") or {}).get("sound", True)),
            "toast": bool((cfg.get("reminder") or {}).get("system_toast", True)),
        },
        "mini": {
            "open": miniwin.is_open(),
            "pinned": bool(miniwin.STATE.get("pinned")),
            "rect": miniwin.STATE.get("rect"),
        },
        "signals": {
            "skills": (daily.get("signals") or {}).get("skills") or [],
            "projects": (daily.get("signals") or {}).get("projects") or [],
            "tools": (daily.get("signals") or {}).get("tools") or [],
            "searches": (daily.get("signals") or {}).get("searches") or [],
            "sites": (daily.get("signals") or {}).get("sites") or [],
            "files": (daily.get("signals") or {}).get("files") or [],
            "browser": (daily.get("signals") or {}).get("browser") or "",
        },
        "launchers": cfg.get("launchers") or [],
        "ai": {
            "ready": ai.ai_ready(cfg),
            "mode": str((cfg.get("ai") or {}).get("mode") or "api"),
            "model": (cfg.get("ai") or {}).get("model") or "",
            "baseUrl": (cfg.get("ai") or {}).get("base_url") or "",
            "webUrl": (cfg.get("ai") or {}).get("web_url") or "",
        },
        "status": {
            "generating": generating,
            "note": note,
            "rev": rev,
            "port": getattr(_SERVER, "server_port", 0),
            "expanding": dict(STATE["expanding"]),
        },
    }


# ------------------------------------------------------------------ 后台任务

def _run_expand_all(cfg: dict, ctx: dict, first: int | None = None,
                    force: bool = False, workers: int = 3) -> None:
    """一次性把所有条目的简述都生成出来。first 那条优先（用户刚点的那条）。"""
    key = f"daily-{ctx['iso']}"
    payload = store.read_cache(key) or {}
    items = (payload.get("news") or {}).get("items") or []

    order: list[int] = []
    if first is not None and 0 <= first < len(items):
        order.append(first)
    order += [i for i in range(len(items)) if i not in order]
    todo = [i for i in order
            if force or not ai.is_real_summary(items[i], cfg)]

    with LOCK:
        STATE["expanding"] = {"running": bool(todo), "done": 0, "total": len(todo)}

    if not todo:
        with LOCK:
            STATE["rev"] += 1
        return

    store.log(f"expand_all start: {len(todo)} 条，{workers} 路并发")

    def work(i):
        # compose_summary 内部：模型优先（失败重试 2 次）→ 本地重排兜底，
        # 所以这里不会再出现十条一起 `failed: 还没配置模型` 的刷屏。
        try:
            info = ai.expand_item(cfg, ctx, i, force=force)
            return i, bool(info.get("ok")), info
        except Exception as exc:
            store.log(f"expand_all[{i}] failed: {exc}")
            return i, False, {"message": str(exc)[:120]}

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, i) for i in todo]
        for fut in cf.as_completed(futs):
            i, ok, info = fut.result()
            with LOCK:
                STATE["expanding"]["done"] += 1
                STATE["rev"] += 1
            if ok:
                store.log(f"expand_all {i}: ok {info.get('chars')} 字（{info.get('by')}）")
            else:
                store.log(f"expand_all {i}: fail {info.get('message')}")

    # 兜底：万一上面还有缺口（材料太空之类），本地重排再补一遍，
    # 保证「每条新闻都有能读的简述」这件事不依赖模型。
    back = ai.ensure_summaries(cfg, ctx)
    if back.get("filled"):
        store.log(f"expand_all: 兜底补了 {back['filled']} 条")
    with LOCK:
        STATE["expanding"]["running"] = False
        STATE["rev"] += 1
    store.log("expand_all done")


def _worker(cfg_override: dict | None = None) -> None:
    try:
        cfg = store.load_config()
        ann = cfg.get("anniversaries") or []
        ctx = calendar_cn.today_context(None, ann)
        with LOCK:
            STATE["generating"] = True
            STATE["note"] = "正在取天气"

        wx = weather.current(cfg, force=True)

        # 配图库单独开一条线程：它可能要下载几十张图，别把简报卡在后面
        def _photo_job():
            try:
                res = photos.sync(cfg)
                store.log(f"photos sync: {res}")
                # 来了新图就重算今天用哪张（新图可能更贴合今晚的天象）
                if res.get("added"):
                    photos.choose_daily(cfg, ctx, wx, force=True)
                with LOCK:
                    STATE["rev"] += 1
            except Exception as exc:
                store.log(f"photos sync failed: {exc}")

        threading.Thread(target=_photo_job, name="wb-photos", daemon=True).start()

        # 属性体系升级过的话，给存量图片补一次标签（只跑一次）
        if not (cfg.get("photos") or {}).get("retagged"):
            try:
                r = photos.retag()
                store.log(f"retag once: {r}")
                store.patch_config(photos={"retagged": True})
            except Exception as exc:
                store.log(f"retag failed: {exc}")

        cache = store.read_cache(f"daily-{ctx['iso']}")
        mode = str((cfg.get("ai") or {}).get("mode") or "api")

        if mode == "web":
            # 网页版：只保证有资讯骨架与本地痕迹，正文等用户贴回来
            need = cache is None or not (cache.get("news") or cache.get("signals"))
            why = "缺少骨架"
        elif ai.ai_ready(cfg):
            # 有模型时只留「当天的」。昨晚预生成的那份算陈旧——
            # 界面先拿它顶住（秒开），后台再换成今天真正的新内容。
            need = ai.needs_refresh(cache, ctx["iso"], ai_ok=True)
            if need and cache:
                gen = str(cache.get("generatedAt") or "")[:16]
                if str(cache.get("generatedBy") or "local") != "ai":
                    why = "只有本地骨架（写的时候没模型），补一份真的"
                else:
                    why = f"缓存生成于 {gen}，已过夜"
            else:
                why = "没有缓存"
        else:
            # 没配模型也要换掉隔夜那份：标题同样是当天抓的，才算「今天的速递」。
            # （以前这里是 `need = cache is None`，结果骨架一旦写成就再也不刷新，
            #   第二天看到的是前一天的新闻标题。）
            need = ai.needs_refresh(cache, ctx["iso"])
            why = "没有缓存" if cache is None else "缓存是隔夜的（没配模型，重建骨架）"

        if need:
            with LOCK:
                STATE["note"] = "正在整理今日简报"
            store.log(f"refresh today's briefing: {why}")
            ai.build_daily(cfg, ctx, force=True, wx=wx)

        # 简报一落地就把每条都补上能读的简述（本地重排，零成本、不失败）。
        # 这是「稳定预生成」的另一半：用户点开任意一条都不用等、也不会看到报错。
        with LOCK:
            STATE["note"] = "正在预生成各条简述"
        sums = ai.ensure_summaries(cfg, ctx)
        store.log(f"ensure_summaries: 共 {sums.get('total')} 条，"
                  f"模型级 {sums.get('ai')} / 本地 {sums.get('local')}"
                  f"（新补 {sums.get('filled')}）")

        if (cfg.get("power") or {}).get("prefetch_tomorrow", True):
            with LOCK:
                STATE["note"] = "正在预生成明日内容"
            ai.prefetch_tomorrow(cfg, wx)

        with LOCK:
            STATE["note"] = "已是最新"
            STATE["rev"] += 1
    except Exception as exc:
        store.log("worker failed:\n" + traceback.format_exc())
        with LOCK:
            STATE["note"] = "生成失败，已用缓存兜底"
            STATE["aiError"] = str(exc)[:200]
    finally:
        with LOCK:
            STATE["generating"] = False


def start_worker() -> None:
    threading.Thread(target=_worker, name="wb-worker", daemon=True).start()


# 跨日补刷的记账：iso = 当前认到的日期，tries = 今天已经补了几次
_ROLLOVER = {"iso": "", "tries": 0, "last": 0.0}
_ROLLOVER_MAX_TRIES = 4        # 一天最多补几次（模型欠费时别把重试变成刷接口）
_ROLLOVER_COOLDOWN = 1800      # 两次补刷之间至少隔 30 分钟


def _daily_check() -> str:
    """看一眼「今天这份简报要不要补」，要补就把 `_worker` 派出去。

    返回一个短标签方便自测与日志：busy / fresh / maxed / cooldown / rebuild / error。
    """
    try:
        cfg = store.load_config()
        ctx = calendar_cn.today_context(None, cfg.get("anniversaries") or [])
        iso = ctx["iso"]
        if _ROLLOVER["iso"] != iso:
            _ROLLOVER.update(iso=iso, tries=0, last=0.0)
            store.log(f"daily watcher: 日期跨到 {iso}，看看要不要补一份")

        with LOCK:
            busy = STATE["generating"]
        if busy:
            return "busy"

        mode = str((cfg.get("ai") or {}).get("mode") or "api")
        ai_ok = mode != "web" and ai.ai_ready(cfg)
        cache = store.read_cache(f"daily-{iso}")
        if mode == "web":
            stale = cache is None or not (cache.get("news") or cache.get("signals"))
        else:
            stale = ai.needs_refresh(cache, iso, ai_ok=ai_ok)
        if not stale:
            return "fresh"

        if _ROLLOVER["tries"] >= _ROLLOVER_MAX_TRIES:
            return "maxed"
        if time.time() - _ROLLOVER["last"] < _ROLLOVER_COOLDOWN:
            return "cooldown"
        _ROLLOVER["tries"] += 1
        _ROLLOVER["last"] = time.time()
        # 没配模型也照样补：这条路的 stale 只来自「没缓存 / 没条目 / 生成日期过夜」，
        # 三种都值得重抓一遍标题 —— 骨架也是当天的骨架。
        store.log(f"daily watcher: {iso} 的简报不合格，第 {_ROLLOVER['tries']} 次补刷")
        with LOCK:
            STATE["note"] = "正在补今天的简报"
        threading.Thread(target=_worker, name="wb-worker-rollover", daemon=True).start()
        return "rebuild"
    except Exception:
        store.log("daily watcher failed:\n" + traceback.format_exc())
        return "error"


def _daily_watcher() -> None:
    """零点之后，只要工作台还开着，就自己把当天那份简报补出来。

    为什么必须有它：开机只走一遍 `_worker`，之后主线程就只是睡。壁纸挂着过夜、
    跨到第二天早上，看到的还是昨晚那份（`generatedAt` 停在昨天）。这里每 60 秒
    看一眼日期，一变就再走一遍 `_worker`（它会重建今天，并顺手预生成明天）。

    「只要有机会」的另外两条路，靠别处兜：
      - 0 点整机器没开 / 没醒：下次打开时 `_worker` 自检缓存发现过夜，会重建；
      - 0 点整机器开着但工作台没开：WorkBuddy 的 0 点自动化跑 `--prefetch`。
    三条路都只做同一件事，所以谁先到都不冲突（`needs_refresh` 幂等）。

    ⚠️ 三个护栏：
      1. 只在「日期变了」或「当天缓存不合格」时动手，正常一天最多触发一次；
      2. 有冷却与次数上限 —— 模型欠费 / 网络断的时候，重试不能变成刷接口；
      3. 生成中（`STATE["generating"]`）直接跳过，不跟 `_worker` 抢同一份缓存。
    """
    store.log(f"daily watcher: 起，每 60 秒看一次日期"
              f"（一天最多补 {_ROLLOVER_MAX_TRIES} 次，间隔 ≥ {_ROLLOVER_COOLDOWN // 60} 分钟）")
    while True:
        time.sleep(60)
        _daily_check()

# ------------------------------------------------------------------ 到点提醒

_REMINDER_TICK = 10          # 每 10 秒看一次表：够准，又不至于空转
_RING_TTL = 2 * 3600         # 一条没人接的提醒最多在队列里躺 2 小时


def _reminder_once() -> int:
    """扫一遍「到点了没有」，把结果放进 STATE["ring"]，返回本次报了几条。

    这里只做两件事：标记（reminded）与入队。**不弹窗** ——
    弹窗是页面的活（系统通知要页面有权限、要跟着窗口走）。
    好处是窗口关着的时候提醒也不会丢：它会留在队列里，下次开窗补报。
    """
    cfg = store.load_config()
    snapshot = store.normalize_todos(cfg.get("todos"))
    fire, silent = todo_logic.collect_due(snapshot)
    if not fire and not silent:
        return 0

    marks = {t["id"]: t.get("remindedAt") or todo_logic.fmt(todo_logic.now())
             for t in list(fire) + list(silent)}

    def apply(items):
        for t in items:
            at = marks.get(t.get("id"))
            if at and t.get("remindAt"):
                t["reminded"] = True
                t["remindedAt"] = at
        return items

    store.mutate_todos(apply)
    stamp = time.time()
    with LOCK:
        STATE["ring"] = [r for r in STATE["ring"]
                         if stamp - float(r.get("queuedAt") or 0) < _RING_TTL]
        for t in fire:
            # 同一条提醒重复入队没意义（重启后 reminded 已经置位，本来也不会再来）
            if any(r.get("id") == t["id"] for r in STATE["ring"]):
                continue
            STATE["ring"].append({
                "id": t["id"],
                "text": t.get("text") or "",
                "at": t.get("remindAt") or "",
                "queuedAt": stamp,
                "late": bool(t.get("late")),
                "lateMinutes": int(t.get("lateMinutes") or 0),
            })
        STATE["rev"] += 1
    if fire:
        store.log("reminder: 到点 " + "；".join(
            f"{t.get('text')}（{t.get('remindAt')}）" for t in fire)[:300])
    if silent:
        store.log(f"reminder: {len(silent)} 条过期一天以上，直接销账不弹")
    return len(fire)


def _reminder_loop() -> None:
    """一直守着表。工作台开着 = 提醒有人管；小窗钉在桌面上就是这个前提。"""
    store.log(f"reminder: 起，每 {_REMINDER_TICK}s 扫一次（最长约 {todo_logic.HORIZON_MAX} 小时）")
    while True:
        try:
            _reminder_once()
        except Exception:
            store.log("reminder loop failed:\n" + traceback.format_exc())
        time.sleep(_REMINDER_TICK)


def _todos_payload(cfg: dict | None = None) -> dict:
    """主窗、小窗共用的待办快照。别再各自拼一份 —— 拼法一多就对不上。"""
    cfg = cfg or store.load_config()
    items = store.normalize_todos(cfg.get("todos"))
    with LOCK:
        ring = [dict(r) for r in STATE["ring"]]
    return {
        "ok": True,
        "todos": items,
        "ring": ring,
        "stats": todo_logic.summary(items),
        "next": todo_logic.next_upcoming(items, 3),
        # 列表最高能长到多少 px。由后端按工作区算：**不能用 vh**，
        # 窗口高度本身由内容决定，前端用 vh 会自己参考自己。
        "maxListPx": miniwin.max_list_px(),
        "autoFit": bool(miniwin.STATE.get("auto")),
        "horizonHours": todo_logic.horizon_hours(cfg),
        "capHours": todo_logic.HORIZON_MAX,
        "capAt": todo_logic.fmt(todo_logic.now()
                                + _dt.timedelta(hours=todo_logic.HORIZON_MAX)),
        "now": _dt.datetime.now().isoformat(timespec="seconds"),
        "theme": str(cfg.get("theme") or "light"),
        # 小窗是一个独立页面，这两个开关得跟着数据走，它读不到主窗的设置面板
        "sound": bool((cfg.get("reminder") or {}).get("sound", True)),
        "toast": bool((cfg.get("reminder") or {}).get("system_toast", True)),
        "mini": {"open": miniwin.is_open(),
                 "pinned": bool(miniwin.STATE.get("pinned"))},
    }


def _deferred_tag(delay: int = 15) -> None:
    """后台把图库里的图片属性补齐（本地推导，不联网不花钱）。

    为什么延迟 + 放后台：一是启动路径上一个慢活都不能有；二是图库同步也在跑，
    等它先落地再打标能少做一遍。只补没打过标的条目，所以是增量的。
    打完 `_bump_rev()`，界面下次拉 state 就能看到分类统计变了。
    """
    try:
        time.sleep(max(0, delay))
        r = photos.tag_local(store.load_config())
        if r.get("tagged"):
            store.log(f"deferred tag: 补了 {r['tagged']} 张，依据 {r.get('byNote')}")
            _bump_rev()
    except Exception:
        store.log("deferred tag failed:\n" + traceback.format_exc())


def _purge_intro_once() -> None:
    """一次性清掉旧「全库预生成」留下的本地介绍。

    现在改成「显示到哪张、才为哪张现写」。清空之后不会出现提前写好的
    320 段文字；当前这张会由 build_state 立刻补一条本地介绍（无模型时）。
    """
    if store.read_cache("intro-on-demand-v1"):
        return
    try:
        r = photos.purge_prebuilt_intros()
        store.write_cache("intro-on-demand-v1", {
            "at": _dt.datetime.now().isoformat(timespec="seconds"),
            "cleared": r.get("cleared", 0)})
        if r.get("cleared"):
            store.log(f"intro: 清掉预生成的本地介绍 {r['cleared']} 条（改为按需）")
    except Exception:
        store.log("purge intros failed:\n" + traceback.format_exc())


def prebuild_once() -> int:
    """0 点定时任务（WorkBuddy 自动化）用的无窗口预生成。

    只备好**今天**的简报缓存就退出：不绑端口、不拿实例锁、不开窗口，
    跟 GUI 完全解耦 —— 此时工作台多半根本没开。明早打开秒出新内容；
    就算这步失败，应用打开时自测发现缓存缺/旧也会自己兜底重建。
    """
    try:
        cfg = store.load_config()
        ann = cfg.get("anniversaries") or []
        ctx = calendar_cn.today_context(None, ann)
        wx = weather.current(cfg, force=True)
        cache = store.read_cache(f"daily-{ctx['iso']}")
        mode = str((cfg.get("ai") or {}).get("mode") or "api")
        if mode == "web":
            need = cache is None or not (cache.get("news") or cache.get("signals"))
        else:
            # 没配模型也走 needs_refresh：隔夜的标题（generatedAt 停在前一天）
            # 必须换掉，否则 0 点那趟等于白跑。
            need = ai.needs_refresh(cache, ctx["iso"], ai_ok=ai.ai_ready(cfg))
        if not need:
            store.log(f"prefetch: {ctx['iso']} 的缓存已是今天生成的，跳过")
            return 0
        store.log(f"prefetch: 开始预生成 {ctx['iso']} 的简报")
        out = ai.build_daily(cfg, ctx, force=True, wx=wx)
        got = len(((out.get("news") or {}).get("items")) or [])
        store.log(f"prefetch: 完成，{got} 条" + ("（没抓到，明早打开会重试）" if not got else ""))
        return 0
    except Exception:
        store.log("prefetch failed:\n" + traceback.format_exc())
        return 1


# ---------------------------------------------------- 外部模型代工（不花自己的 Key）

def outbox_dir() -> Path:
    """给「代工方」（WorkBuddy 之类的自动化）投递/取件的目录。"""
    return store.DATA_DIR / "outbox"


def dump_prompt(out_dir: str | None = None) -> int:
    """把「今天这份简报的提示词」写成文件，等别人来写答案。

    为什么要有这条路：`--prefetch` 是让**工作台自己**调 API，花的是配置里那把 Key。
    如果用户更愿意让 WorkBuddy 那边的模型来写（配额本来就有），就走这条：
    提示词由工作台拼（资讯池、去重、加权、本机痕迹都在本地做），
    答案由外面的模型给，最后 `--ingest` 收回来按同一套校验合并。
    好处是「谁生成」可以换，「怎么校验」只有一份。
    """
    try:
        cfg = store.load_config()
        ctx = calendar_cn.today_context(None, cfg.get("anniversaries") or [])
        wx = weather.peek(cfg)
        p = ai.build_prompt(cfg, ctx, wx, fast=True)
        out = Path(out_dir) if out_dir else outbox_dir()
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"prompt-{ctx['iso']}.json"
        path.write_text(json.dumps({
            "date": ctx["iso"],
            "dateText": ctx.get("dateText", ""),
            "weekday": ctx.get("weekday", ""),
            "festival": ctx.get("festival", ""),
            "system": p["system"],
            "user": p["user"],
            "combined": p["combined"],
            "stats": p["stats"],
            "poolSize": len(p["pool"]),
            "count": p["count"],
            "answerTo": str(out / f"answer-{ctx['iso']}.json"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        store.log(f"dump_prompt: {ctx['iso']} 提示词已写到 {path}（{len(p['pool'])} 条候选）")
        print(str(path))
        return 0
    except Exception:
        store.log("dump_prompt failed:\n" + traceback.format_exc())
        return 1


def ingest_once(src: str) -> int:
    """把代工方写好的 JSON 收进缓存。

    两条校验规则（2026-11-05 改）：
      · 标题在候选池里 → 用池子里的 url/source（防编造）
      · 标题不在池子里、但带合法 http 链接 → 收，标 external=true（这是「自己检索」补的）
    没链接的一律丢。
    """
    try:
        text = Path(src).read_text(encoding="utf-8")
        cfg = store.load_config()
        ctx = calendar_cn.today_context(None, cfg.get("anniversaries") or [])

        # 答案里的 date 优先。以前只用「现在」算日期：23:58 导出、00:05 收回，
        # 整批答案就被写进次日缓存，标题全对不上、静默丢光，还只返回 1。
        try:
            _obj = ai._extract_json(text)
            _want = str((_obj or {}).get("date") or "").strip()
        except Exception:
            _want = ""
        if len(_want) == 10 and _want[4] == "-" and _want[7] == "-" and _want != ctx["iso"]:
            try:
                ctx = calendar_cn.today_context(_dt.date.fromisoformat(_want),
                                                cfg.get("anniversaries") or [])
                store.log(f"ingest: 按答案里的日期 {_want} 入库")
            except Exception:
                pass

        wx = weather.peek(cfg)
        payload = ai.ingest_text(cfg, ctx, text, wx=wx)
        items = ((payload.get("news") or {}).get("items")) or []
        got = len(items)
        brief = str((payload.get("news") or {}).get("brief") or "")
        store.log(f"ingest: {ctx['iso']} 收下 {got} 条，总览 {len(brief)} 字")
        print(json.dumps({"ok": bool(got), "items": got, "briefChars": len(brief),
                          "generatedBy": payload.get("generatedBy"),
                          "cache": str(store.CACHE_DIR / f'daily-{ctx["iso"]}.json')},
                         ensure_ascii=False))
        return 0 if got else 1
    except Exception:
        store.log("ingest failed:\n" + traceback.format_exc())
        return 1


def dump_expand(out_dir: str | None = None) -> int:
    """导出「还差模型级简述」的条目，等外面的模型来写。

    与 `--dump-prompt` 同一套分工：提示词工作台拼（标题/要点/源站摘要都在本地），
    内容外部写，`--ingest-expand` 按 index 收回。已经是模型级的条目不再导出，
    所以每天重跑只写缺的那几条，不会重复花钱。
    """
    try:
        cfg = store.load_config()
        ctx = calendar_cn.today_context(None, cfg.get("anniversaries") or [])
        reqs = ai.expand_requests(cfg, ctx)
        out = Path(out_dir) if out_dir else outbox_dir()
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"expand-{ctx['iso']}.json"
        path.write_text(json.dumps({
            "date": ctx["iso"],
            "count": len(reqs),
            "requests": reqs,
            "answerTo": str(out / f"expand-answer-{ctx['iso']}.json"),
            "answerShape": '{"summaries": [{"index": 0, "text": "……"}, ...]}',
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        store.log(f"dump_expand: {ctx['iso']} 有 {len(reqs)} 条待写，已写到 {path}")
        print(str(path))
        return 0
    except Exception:
        store.log("dump_expand failed:\n" + traceback.format_exc())
        return 1


def ingest_expand_once(src: str) -> int:
    """把外部写好的各条简述收进缓存（index 越界/太短的会被跳过并记账）。"""
    try:
        obj = json.loads(Path(src).read_text(encoding="utf-8"))
        cfg = store.load_config()
        ctx = calendar_cn.today_context(None, cfg.get("anniversaries") or [])
        res = ai.ingest_expand(cfg, ctx, obj)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    except Exception:
        store.log("ingest_expand failed:\n" + traceback.format_exc())
        return 1


# ------------------------------------------------------------------ HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = f"Workbench/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    # ---------------------------------------------------------- 基础工具

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError):
                pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8", "ignore"))
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            data = path.read_bytes()
        except OSError:
            self._send(500, b"read error", "text/plain; charset=utf-8")
            return
        self._send(200, data, MIME.get(path.suffix.lower(), "application/octet-stream"))

    # ------------------------------------------------------------- GET

    def do_GET(self) -> None:
        try:
            self._route_get()
        except Exception:
            store.log("GET failed:\n" + traceback.format_exc())
            self._send(500, b"internal error", "text/plain; charset=utf-8")

    def _route_get(self) -> None:
        path = unquote(urlparse(self.path).path)

        if path in ("/", "/index.html"):
            return self._file(web_dir() / "index.html")
        if path == "/mini":
            # 待办小窗：独立窗口，和主窗共用同一份数据（同一个后端）
            return self._file(web_dir() / "mini.html")
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            return self._safe_file(web_dir(), rel)
        if path == "/favicon.ico":
            return self._file(web_dir() / "favicon.svg")
        if path.startswith("/art/"):
            return self._safe_file(store.ART_DIR, path[len("/art/"):])
        if path.startswith("/icon/"):
            return self._icon(path[len("/icon/"):])
        if path.startswith("/launcher-icon/"):
            return self._launcher_icon(path[len("/launcher-icon/"):])
        if path == "/api/fs":
            q = parse_qs(urlparse(self.path).query)
            return self._json(actions.list_dir((q.get("path") or [""])[0]))
        if path == "/api/launchers":
            cfg = store.load_config()
            return self._json({"ok": True, "launchers": cfg.get("launchers") or []})
        if path == "/api/todos":
            # 轻量接口：只回待办与提醒。主窗每 12 秒轮询一次 state 太重，
            # 小窗更不该为了几条待办去算壁纸和天气。
            return self._json(_todos_payload())
        if path == "/api/ping":
            return self._json({"app": "workbench", "version": APP_VERSION,
                               "port": getattr(self.server, "server_port", 0)})
        if path == "/api/state":
            return self._json(build_state())
        if path == "/api/bookmarks":
            items = bookmarks.load()
            return self._json({"ok": True, "count": len(items), "items": items})
        if path == "/api/feedback":
            # 只看统计，不读全量 jsonl（那样每 12 秒轮询会烧 CPU）
            return self._json({"ok": True, "summary": ranker.summary(),
                               "stats": ranker.stats()})

        if path == "/api/search":
            # 只读文件，不发任何网络请求。搜索本体由外面的 agent 干，见 app/search.py
            q = parse_qs(urlparse(self.path).query)
            topic = (q.get("topic") or [""])[0].strip()
            if not topic:
                return self._json({"ok": True, "stats": search.stats(),
                                   "pending": search.pending()})
            return self._json(search.get(topic))

        if path == "/api/config":
            cfg = store.load_config()
            return self._json({"config": cfg, "path": str(store.CONFIG_PATH)})
        if path == "/api/settings-schema":
            # 设置面板由这张表渲染 —— 加配置项只改 app/settings_schema.py 一处。
            # 顺带把图库现状带上，设置里就能直接看到「各类多少张、打了多少标」。
            return self._json({"groups": settings_schema.GROUPS,
                               "library": photos.stats()})
        if path == "/api/library":
            q = parse_qs(urlparse(self.path).query)
            return self._json(photos.library_page(
                None,
                category=(q.get("category") or [""])[0],
                src=(q.get("src") or [""])[0],
                offset=int((q.get("offset") or ["0"])[0] or 0),
                limit=int((q.get("limit") or ["60"])[0] or 60)))
        if path == "/api/prompt":
            _t = time.perf_counter()
            cfg = store.load_config()
            ctx = calendar_cn.today_context(None, cfg.get("anniversaries") or [])
            # fast=True：资讯池过期也先用旧的一版，后台再刷。
            # 不加这个，点一下「网页生成」要等 20 多秒（实测 24.5s → 0.14s）
            p = ai.build_prompt(cfg, ctx, weather.peek(cfg), fast=True)
            _ms = (time.perf_counter() - _t) * 1000
            if _ms > 2000:
                store.log(f"/api/prompt 慢：{_ms:.0f}ms")
            return self._json({
                "system": p["system"], "user": p["user"], "combined": p["combined"],
                "stats": p["stats"], "target": p["target"] or "https://chat.deepseek.com/",
            })
        if path == "/api/logs":
            q = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(1000, int((q.get("limit") or ["200"])[0])))
            except (TypeError, ValueError):
                limit = 200
            try:
                since = float((q.get("since") or ["0"])[0]) if q.get("since") else None
            except (TypeError, ValueError):
                since = None
            lvl = (q.get("level") or [""])[0].strip() or None
            return self._json({"ok": True,
                               "entries": logbook.tail(level=lvl, limit=limit,
                                                       since=since)})
        if path == "/api/log":
            try:
                txt = store.LOG_PATH.read_text(encoding="utf-8")[-8000:]
            except OSError:
                txt = ""
            return self._send(200, txt.encode("utf-8"), "text/plain; charset=utf-8")
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def _safe_file(self, root: Path, rel: str) -> None:
        try:
            target = (root / rel).resolve()
            root_r = root.resolve()
            if root_r not in target.parents and target != root_r:
                return self._send(403, b"forbidden", "text/plain; charset=utf-8")
        except OSError:
            return self._send(400, b"bad path", "text/plain; charset=utf-8")
        return self._file(target)

    def _icon(self, name: str) -> None:
        """启动器图标：用户放在 art/icons 里的优先，否则用内置的。"""
        safe = "".join(c for c in name if c.isalnum() or c in "-_.")
        if not safe:
            return self._send(404, b"no icon", "text/plain; charset=utf-8")
        for ext in (".png", ".svg", ".jpg", ".webp"):
            user = store.ART_DIR / "icons" / f"{safe}{ext}"
            if user.is_file():
                return self._file(user)
        for ext in (".svg", ".png", ".jpg", ".webp"):
            builtin = web_dir() / "icons" / f"{safe}{ext}"
            if builtin.is_file():
                return self._file(builtin)
        return self._send(404, b"no icon", "text/plain; charset=utf-8")

    def _launcher_icon(self, lid: str) -> None:
        """启动项图标：icon 字段是文件路径就直接用它，否则回落到内置/用户图标。"""
        lid = "".join(c for c in lid if c.isalnum() or c in "-_.")
        cfg = store.load_config()
        entry = next((l for l in (cfg.get("launchers") or []) if l.get("id") == lid), None)
        icon = str((entry or {}).get("icon") or "").strip()
        if icon:
            p = Path(icon).expanduser()
            if p.is_file():
                return self._file(p)
            return self._icon(icon)
        return self._icon(lid)

    # ------------------------------------------------------------ POST

    def do_POST(self) -> None:
        try:
            self._route_post()
        except Exception:
            store.log("POST failed:\n" + traceback.format_exc())
            self._json({"ok": False, "message": "内部错误，见 workbench.log"}, 500)

    def _same_origin(self) -> bool:
        """写接口只认自家页面发来的请求。

        服务只绑 127.0.0.1，但**任何网页**都能朝 localhost 发请求（CSRF）。
        浏览器会给跨站请求带上 Sec-Fetch-Site / Origin，据此挡掉外部页面；
        没有这两个头的（curl、本机脚本、自测）照旧放行。
        """
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = getattr(self.server, "server_port", 0)
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _route_post(self) -> None:
        path = urlparse(self.path).path
        body = self._body()

        if path != "/api/ping" and not self._same_origin():
            store.log(f"POST {path} 被跨站拦截")
            return self._json({"ok": False, "message": "拒绝跨站请求"}, 403)

        if path == "/api/ping":
            with LOCK:
                STATE["lastPing"] = time.time()
                STATE["pings"] += 1
            return self._json({"ok": True})

        if path == "/api/feedback":
            act = str(body.get("action") or "")
            if act == "rate":
                try:
                    score = int(body.get("score"))
                except Exception:
                    return self._json({"ok": False, "message": "score 必须是 0-10"}, 400)
                r = ranker.record(body, score, str(body.get("note") or ""))
                return self._json(dict(r, summary=ranker.summary()))
            if act == "open":
                # 点开＝弱正向信号（OPEN_WEIGHT 条评分）。不改变「评分」的定义，
                # 只是让每天 10 条内容里那些「我点开读了但没打分」的也留下痕迹。
                return self._json(dict(ranker.record_open(body),
                                       summary=ranker.summary()))
            return self._json({"ok": False, "message": f"不认识的 action：{act}"}, 400)

        if path == "/api/bookmarks":
            act = str(body.get("action") or "toggle")
            if act == "remove":
                return self._json(bookmarks.remove(str(body.get("id") or "")))
            kind = str(body.get("kind") or "news")
            if kind not in ("news", "github"):
                return self._json({"ok": False, "message": f"不认识的 kind：{kind}"}, 400)
            item = body.get("item") if isinstance(body.get("item"), dict) else body
            return self._json(bookmarks.toggle(kind, item))

        if path == "/api/search":
            act = str(body.get("action") or "")
            if act == "submit":
                r = search.submit(str(body.get("topic") or ""),
                                  str(body.get("depth") or "normal"))
                if r.get("ok"):
                    store.log(f"搜索排队：{r['task']['topic']}")
                return self._json(r)
            if act == "prompt":
                return self._json({"ok": True, "prompt": search.prompt_for(
                    str(body.get("topic") or ""))})
            if act == "done":
                search.mark_done(str(body.get("id") or ""))
                return self._json({"ok": True})
            return self._json({"ok": False, "message": f"不认识的 action：{act}"}, 400)

        if path == "/api/launch":
            cfg = store.load_config()
            wanted = str(body.get("id") or "")
            entry = next((l for l in (cfg.get("launchers") or []) if l.get("id") == wanted), None)
            if entry is None:
                return self._json({"ok": False, "message": "找不到这个启动项"}, 404)
            result = actions.launch(cfg, entry)
            store.log(f"launch {wanted}: {result}")
            logbook.get("actions").child("launch").info(
                f"launch {wanted}",
                intent="用户点了快捷启动项，走 actions.launch 分发",
                ok=bool(result.get("ok")),
                # 键名不能叫 message：info(self, message, *, intent, **meta) 的第一个
                # 位置参数就是 message，再传同名关键字会 TypeError
                # （got multiple values for argument 'message'），整个 POST 变 500，
                # 前端弹「调用失败」但程序其实已经开起来了。2026-11-03 修。
                detail=str(result.get("message") or "")[:200])
            return self._json(result)

        if path == "/api/launchers":
            cfg = store.load_config()
            items = list(cfg.get("launchers") or [])
            act = str(body.get("action") or "add")

            if act == "add":
                target = str(body.get("path") or "").strip()
                if not target:
                    return self._json({"ok": False, "message": "还没选文件"}, 400)
                entry = store.build_launcher(target, str(body.get("title") or ""),
                                             str(body.get("icon") or ""))
                if any(l.get("id") == entry["id"] for l in items):
                    return self._json({"ok": False, "message": "这个已经加过了"}, 409)
                items.append(entry)
                msg = f"已添加「{entry['title']}」"
            elif act == "update":
                lid = str(body.get("id") or "")
                hit = next((l for l in items if l.get("id") == lid), None)
                if hit is None:
                    return self._json({"ok": False, "message": "找不到这个启动项"}, 404)
                for key in ("title", "subtitle", "icon", "accent", "path", "args",
                            "cwd", "url", "command", "kind"):
                    if key in body:
                        hit[key] = str(body.get(key) or "").strip()
                msg = "已更新"
            elif act == "remove":
                lid = str(body.get("id") or "")
                keep = [l for l in items if l.get("id") != lid]
                if len(keep) == len(items):
                    return self._json({"ok": False, "message": "找不到这个启动项"}, 404)
                items = keep
                msg = "已移除"
            elif act == "move":
                lid = str(body.get("id") or "")
                step = int(body.get("step") or 0)
                idx = next((i for i, l in enumerate(items) if l.get("id") == lid), -1)
                if idx < 0:
                    return self._json({"ok": False, "message": "找不到这个启动项"}, 404)
                new = max(0, min(len(items) - 1, idx + step))
                items.insert(new, items.pop(idx))
                msg = "已调整顺序"
            elif act == "reorder":
                # 拖拽重排：前端把拖完的 id 顺序整串发过来
                ids = [str(x) for x in (body.get("ids") or [])]
                if not ids:
                    return self._json({"ok": False, "message": "没给顺序"}, 400)
                rank = {lid: i for i, lid in enumerate(ids)}
                # 没在列表里的排在后面，且保持原来的相对次序（sorted 是稳定的）
                items = sorted(items, key=lambda l: rank.get(str(l.get("id")), len(rank)))
                msg = "顺序已调整"
            else:
                return self._json({"ok": False, "message": "不认识的操作"}, 400)

            items = store.normalize_launchers(items)
            store.patch_config(launchers=items)
            with LOCK:
                STATE["rev"] += 1
            store.log(f"launchers {act}: 现在 {len(items)} 项")
            return self._json({"ok": True, "message": msg, "launchers": items})

        if path == "/api/window":
            # 小窗上的「↗」：主窗还开着就把它提到前面，关掉了就重新开一个
            if actions.find_app_window():
                actions.focus_app_window()
                return self._json({"ok": True, "message": "已切到工作台"})
            win = store.load_config().get("window") or {}
            port = getattr(self.server, "server_port", 0)
            info = actions.open_window(f"http://127.0.0.1:{port}",
                                       int(win.get("width") or 1380),
                                       int(win.get("height") or 880))
            WINDOW.update(info)
            WINDOW["hwnd"] = actions.find_app_window()
            store.log(f"mini: 从 小窗 叫起主窗 hwnd={WINDOW.get('hwnd')}")
            return self._json({"ok": True, "message": "正在打开工作台"})

        if path == "/api/mini":
            act = str(body.get("action") or "open")
            port = getattr(self.server, "server_port", 0)
            cfg = store.load_config()
            if act in ("open", "show"):
                res = miniwin.open_mini(port, cfg)
            elif act == "close":
                ok = miniwin.close()
                res = {"ok": True, "message": "小窗已关" if ok else "小窗本来就没开"}
                res.update({"mini": {"open": False,
                                     "pinned": bool(miniwin.STATE.get("pinned"))}})
                with LOCK:
                    STATE["rev"] += 1
                return self._json(res)
            elif act in ("pin", "unpin", "toggle_pin"):
                # 注意别写成 bool(body.get("pinned"))：没带这个字段时 bool(None) 是 False，
                # 点「钉住」会变成「解开」。
                if act == "pin":
                    want = True
                elif act == "unpin":
                    want = False
                else:
                    want = not bool(miniwin.STATE.get("pinned"))
                res = miniwin.set_pin(want)
            elif act == "size":
                # 页面只报内容高度（h）；w 不传 —— 宽度不跟着内容走，
                # 否则文字换行会引起「越缩越小」的死循环（360 → 318 实测）。
                # h0 是「不带弹层」的自然高度：存进配置，下次开窗就用它，
                # 免得先按旧尺寸开一下、再被页面改一次（看着像闪了一下）。
                res = miniwin.set_size(int(body.get("h") or 0),
                                       int(body.get("w") or 0),
                                       natural=int(body.get("h0") or 0))
                res.update({"maxListPx": miniwin.max_list_px(),
                            "autoFit": bool(miniwin.STATE.get("auto"))})
            else:
                return self._json({"ok": False, "message": "不认识的操作"}, 400)
            with LOCK:
                STATE["rev"] += 1
            res.update({"mini": {"open": miniwin.is_open(),
                                 "pinned": bool(miniwin.STATE.get("pinned"))}})
            return self._json(res)

        if path == "/api/refresh":
            # 手动刷新时顺手丢掉资讯池缓存，确保抓的是刚刚的新闻
            try:
                store.cache_path("news-pool").unlink(missing_ok=True)
            except OSError:
                pass
            threading.Thread(target=_worker, daemon=True).start()
            return self._json({"ok": True, "message": "已在后台重新生成"})

        if path == "/api/wallpaper":
            # 只重算壁纸：不碰简报、不碰图库。天气顺手刷一次（都在后台线程，
            # 请求路径上照样没有网络 I/O）。给定时任务/自动化用。
            def job():
                try:
                    cfg2 = store.load_config()
                    ctx2 = calendar_cn.today_context(None, cfg2.get("anniversaries") or [])
                    wx2 = weather.current(cfg2, force=True)
                    chosen = photos.choose_daily(cfg2, ctx2, wx2, force=True)
                    store.log(f"wallpaper recompute: {(chosen or {}).get('why') or '空'}")
                    # 换上的这张没有正式介绍的话，按需补这一张（不做全库预热）
                    _wm = (chosen or {}).get("main") or {}
                    _ensure_intro_one(str(_wm.get("file") or ""))
                except Exception:
                    store.log("wallpaper recompute failed:\n" + traceback.format_exc())
                with LOCK:
                    STATE["rev"] += 1

            threading.Thread(target=job, name="wb-wallpaper", daemon=True).start()
            return self._json({"ok": True, "message": "正在重算壁纸"})

        if path == "/api/expand":
            idx = int(body.get("index", -1))
            cfg = store.load_config()
            ctx = calendar_cn.today_context(None, cfg.get("anniversaries") or [])
            res = ai.expand_item(cfg, ctx, idx, force=bool(body.get("force")))
            if res.get("ok"):
                with LOCK:
                    STATE["rev"] += 1
            return self._json(res, 200 if res.get("ok") else 400)

        if path == "/api/expand-all":
            idx = body.get("index")
            first = int(idx) if isinstance(idx, (int, float, str)) and str(idx).lstrip("-").isdigit() else None
            force = bool(body.get("force"))
            with LOCK:
                if STATE["expanding"]["running"]:
                    return self._json({"ok": True, "message": "已经在生成了",
                                       "expanding": dict(STATE["expanding"])})
            cfg = store.load_config()
            ctx = calendar_cn.today_context(None, cfg.get("anniversaries") or [])
            threading.Thread(target=_run_expand_all, args=(cfg, ctx, first, force),
                             name="wb-expandall", daemon=True).start()
            return self._json({"ok": True, "message": "正在生成全部简述"})

        if path == "/api/todos":
            cfg = store.load_config()
            act = str(body.get("action") or "")
            now = _dt.datetime.now().isoformat(timespec="seconds")
            tid = str(body.get("id") or "")
            msg = ""
            err = ""
            extra: dict = {}

            def find(items):
                return next((t for t in items if t.get("id") == tid), None)

            if act == "add":
                text = str(body.get("text") or "").strip()
                if not text:
                    return self._json({"ok": False, "message": "内容为空"}, 400)
                # 一次性加多条：换行就是分隔符（小窗里可以一口气贴一串）
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if not lines:
                    return self._json({"ok": False, "message": "内容为空"}, 400)
                when, why = (None, "")
                if body.get("remindAt"):
                    when, why = todo_logic.clamp(body.get("remindAt"), cfg)
                    if why:
                        return self._json({"ok": False, "message": why}, 400)

                def do_add(items):
                    stamp = int(time.time() * 1000)
                    for i, line in enumerate(lines):
                        entry = {"id": f"t{stamp}{i:02d}", "text": line[:200],
                                 "color": store.TODO_DEFAULT_COLOR,
                                 "done": False, "at": now,
                                 "remindAt": "", "reminded": False, "remindedAt": ""}
                        if when:
                            entry["remindAt"] = todo_logic.fmt(when)
                        items.append(entry)
                    return items
                new_items = store.mutate_todos(do_add)
                msg = f"加了 {len(lines)} 条" if len(lines) > 1 else "记下了"
                if when:
                    msg += f"，{todo_logic.pretty(todo_logic.fmt(when))}提醒你"
                extra["added"] = [t["id"] for t in new_items[-len(lines):]]

            elif act == "toggle":
                def do_toggle(items):
                    hit = find(items)
                    if hit:
                        hit["done"] = not hit.get("done")
                    return items
                new_items = store.mutate_todos(do_toggle)

            elif act == "remove":
                new_items = store.mutate_todos(lambda items: [t for t in items if t.get("id") != tid])

            elif act == "edit":
                text = str(body.get("text") or "").strip()[:200]
                if not text:
                    return self._json({"ok": False, "message": "内容不能为空"}, 400)

                def do_edit(items):
                    hit = find(items)
                    if hit:
                        hit["text"] = text
                    return items
                new_items = store.mutate_todos(do_edit)
                msg = "改好了"

            elif act == "set_color":
                color = str(body.get("color") or "").strip().lower()
                if color not in store.TODO_COLORS:
                    return self._json({"ok": False, "message": "没有这个颜色"}, 400)

                def do_color(items):
                    hit = find(items)
                    if hit:
                        hit["color"] = color
                    return items
                new_items = store.mutate_todos(do_color)
                msg = "颜色换好了"

            elif act == "set_remind":
                raw = str(body.get("remindAt") or "").strip()
                if not raw:
                    def do_clear(items):
                        hit = find(items)
                        if hit:
                            hit["remindAt"] = ""
                            hit["reminded"] = False
                            hit["remindedAt"] = ""
                        return items
                    new_items = store.mutate_todos(do_clear)
                    msg = "已取消定时"
                else:
                    when, why = todo_logic.clamp(raw, cfg)
                    if why:
                        return self._json({"ok": False, "message": why}, 400)

                    def do_set(items):
                        hit = find(items)
                        if hit:
                            hit["remindAt"] = todo_logic.fmt(when)
                            hit["reminded"] = False      # 改时间 = 重新上弦
                            hit["remindedAt"] = ""
                        return items
                    new_items = store.mutate_todos(do_set)
                    msg = f"会在{todo_logic.pretty(todo_logic.fmt(when))}提醒你"
                    store.log(f"todo reminder set: {when} → {tid}")

            elif act == "snooze":
                try:
                    minutes = int(body.get("minutes") or 10)
                except (TypeError, ValueError):
                    minutes = 10
                minutes = max(1, min(todo_logic.horizon_hours(cfg) * 60, minutes))
                target = todo_logic.now() + _dt.timedelta(minutes=minutes)

                def do_snooze(items):
                    hit = find(items)
                    if hit:
                        hit["remindAt"] = todo_logic.fmt(target)
                        hit["reminded"] = False
                        hit["remindedAt"] = ""
                        hit["done"] = False
                    return items
                new_items = store.mutate_todos(do_snooze)
                msg = f"{minutes} 分钟后再提醒"
                extra["remindAt"] = todo_logic.fmt(target)

            elif act == "reorder":
                # 拖拽重排：前端把拖完的 id 顺序整串发过来
                ids = [str(x) for x in (body.get("ids") or [])]
                if not ids:
                    return self._json({"ok": False, "message": "没给顺序"}, 400)
                rank = {x: i for i, x in enumerate(ids)}

                def do_reorder(items):
                    return sorted(items, key=lambda t: rank.get(str(t.get("id")), len(rank)))
                new_items = store.mutate_todos(do_reorder)
                msg = "顺序已调整"

            elif act == "clear_done":
                new_items = store.mutate_todos(lambda items: [t for t in items if not t.get("done")])
                msg = "已清掉完成项"

            elif act == "reminder_ack":
                # 页面已经弹过了：从队列里拿掉，另一个窗口就不会再弹一遍
                with LOCK:
                    before = len(STATE["ring"])
                    STATE["ring"] = [r for r in STATE["ring"] if r.get("id") != tid]
                    STATE["rev"] += 1
                new_items = store.normalize_todos(cfg.get("todos"))
                msg = "已确认" if len(STATE["ring"]) != before else ""

            else:
                return self._json({"ok": False, "message": "未知操作"}, 400)

            with LOCK:
                STATE["rev"] += 1
            payload = _todos_payload()
            payload.update({"message": msg, **extra})
            return self._json(payload)

        if path == "/api/photos":
            cfg = store.load_config()
            n = int(body.get("pages") or photos.SPOTLIGHT_PAGES)

            def job():
                with LOCK:
                    STATE["note"] = "正在扩充配图库"
                try:
                    res = photos.sync(cfg, force=True, pages=n)
                    store.log(f"manual photos sync: {res}")
                except Exception as exc:
                    store.log(f"manual photos sync failed: {exc}")
                with LOCK:
                    STATE["rev"] += 1
                    STATE["note"] = "配图库已更新"

            threading.Thread(target=job, daemon=True).start()
            return self._json({"ok": True, "message": "正在后台扩图"})

        if path == "/api/ingest":
            cfg = store.load_config()
            ctx = calendar_cn.today_context(None, cfg.get("anniversaries") or [])
            text = str(body.get("text") or "")
            if not text.strip():
                return self._json({"ok": False, "message": "内容是空的"}, 400)
            try:
                ai.ingest_text(cfg, ctx, text, weather.current(cfg))
                with LOCK:
                    STATE["rev"] += 1
                    STATE["note"] = "已应用网页版生成的内容"
                return self._json({"ok": True})
            except Exception as exc:
                store.log(f"ingest failed: {exc}")
                return self._json({"ok": False, "message": f"解析失败：{exc}"[:200]})

        if path == "/api/open-web":
            cfg = store.load_config()
            url = str((cfg.get("ai") or {}).get("web_url") or "").strip()
            if not url:
                return self._json({"ok": False, "message": "还没填网页版地址，去「设置」里填"})
            actions.open_url(url)
            return self._json({"ok": True, "url": url})

        if path == "/api/config":
            incoming = body.get("config")
            if not isinstance(incoming, dict):
                return self._json({"ok": False, "message": "配置格式不对"}, 400)
            # 城市换了就把缓存的经纬度清掉 —— 不清的话天气还会照着旧城市算
            before = store.load_config()
            old_city = str(settings_schema.get_path(before, "profile.city") or "").strip()
            new_city = str(settings_schema.get_path(incoming, "profile.city") or "").strip()
            if old_city != new_city:
                settings_schema.set_path(incoming, "profile.lat", None)
                settings_schema.set_path(incoming, "profile.lon", None)
                store.log(f"城市改为「{new_city or '（自动定位）'}」，经纬度已清空待重新定位")
            # 密码框永远显示空 → 空着回传等于「不改」，别把真 Key 冲掉
            kept = settings_schema.keep_blank_secrets(incoming, before)
            if kept:
                store.log(f"配置保存：{'、'.join(kept)} 留空，沿用旧值")
            saved = store.save_config(incoming)
            with LOCK:
                STATE["rev"] += 1
            return self._json({"ok": True, "config": saved})

        if path == "/api/open-dir":
            target = str(body.get("what") or "art")
            p = {"art": store.ART_DIR, "data": store.DATA_DIR,
                 "config": store.DATA_DIR}.get(target, store.ART_DIR)
            try:
                os.startfile(str(p))  # type: ignore[attr-defined]
            except (OSError, AttributeError):
                return self._json({"ok": False, "message": "打不开目录"})
            return self._json({"ok": True, "path": str(p)})

        if path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=shutdown, args=("界面上点了退出",),
                             daemon=True).start()
            return

        return self._json({"ok": False, "message": "not found"}, 404)


# ------------------------------------------------------------------ 生命周期

def shutdown(reason: str = "") -> None:
    """收工。带上原因，免得下次又对着「进程怎么没了」发呆。"""
    up = time.time() - STATE["startedAt"]
    store.log(f"shutting down（{reason or '未说明'}），已运行 {up / 60:.1f} 分钟")
    logbook.get("main").info(
        "shutting down",
        intent=f"退出：{reason or '未说明'}",
        minutes=round(up / 60, 1))
    # 小窗是独立浏览器进程，服务一退它就只剩一个「连不上」的空壳 —— 顺手关掉
    try:
        miniwin.close()
    except Exception:
        pass
    time.sleep(0.35)
    try:
        if _SERVER is not None:
            _SERVER.shutdown()
    except Exception:
        pass
    time.sleep(0.2)
    os._exit(0)


def _watchdog(keepalive: int) -> None:
    """窗口关掉后自动退出。

    ⚠️ 这个判据踩过两个坑，两次都是「窗口明明开着，服务却自己退了」：

      1. **只看前端心跳**。浏览器会节流 / 冻结后台窗口的页面定时器，电脑睡眠时
         更是完全停摆 —— 心跳一断就误判成「窗口关了」，把正在用的服务退掉
         （workbench.log 里连着出现过 4 次 `no heartbeat, exiting`）。
      2. **看启动时那个 msedge.exe 的 pid**。看着更硬，其实也错：**Edge 会交接** ——
         我们起的那个 msedge.exe 会退出，窗口交给另一个浏览器进程托管。
         实测记录的 pid 早就没了，窗口却好好地开着，于是又被误判。

    所以现在按**窗口本身**判断：拿标题找到那个顶层窗口，用 `IsWindow` 看它还在不在。
    句柄失效就按标题再找一次（Edge 重开窗口时句柄会变）。
    实在找不到窗口（比如退回了默认浏览器）才退回心跳判断，阈值放大到 15 分钟 ——
    宁可多陪一会儿，也不能把正在用的窗口判死。

    另外带**休眠检测**：一轮 sleep 实际跳掉几十秒说明机器睡过，那轮跳过不判。

    还有一个例外：**待办小窗本身就是一个窗口**。小窗钉在桌面上、主窗关掉，
    服务必须继续跑 —— 否则小窗里的提醒和待办全成了摆设。
    所以「主窗不在但小窗在」不算收工。
    """
    stale_logged = False
    while True:
        t0 = time.time()
        time.sleep(3)
        if time.time() - t0 > 30:
            with LOCK:
                STATE["lastPing"] = time.time()
            store.log("watchdog: 时钟跳变（多半是电脑睡眠），跳过本轮心跳判定")
            stale_logged = False
            continue

        if WINDOW.get("fallback"):
            continue                      # 退回默认浏览器开的，拿不到句柄，别瞎退

        with LOCK:
            pings = STATE["pings"]
            last = STATE["lastPing"]
        silent = (time.time() - last) if last else 0.0
        mini_alive = miniwin.is_open()

        hwnd = WINDOW.get("hwnd")
        if not hwnd:
            hwnd = actions.find_app_window()
            if hwnd:
                WINDOW["hwnd"] = hwnd
                store.log(f"watchdog: 认到窗口句柄 {hwnd}")
        if not hwnd:
            if mini_alive:
                with LOCK:
                    STATE["lastPing"] = time.time()
                continue                  # 只有小窗在桌面：它是窗口，算「还在用」
            # 没有窗口可管（--no-window，或窗口还没出现）：只能靠心跳，阈值放宽
            if pings and last and silent > max(keepalive, 900):
                store.log(f"watchdog: 心跳静默 {silent:.0f}s、也没有窗口句柄可查，收工")
                shutdown("心跳停了")
                return
            continue

        if actions.window_open(hwnd):
            # 窗口还在 —— 心跳静默只能说明页面被冻住了，不是窗口关了
            if not stale_logged and pings and last and silent > keepalive:
                store.log(f"watchdog: 心跳静默 {silent:.0f}s，但窗口 {hwnd} 还在 —— 不退")
                stale_logged = True
            continue

        # 句柄失效：Edge 重开窗口时句柄会变，按标题再找一次
        again = actions.find_app_window()
        if again:
            WINDOW["hwnd"] = again
            stale_logged = False
            continue

        if mini_alive:
            # 主窗关了但小窗还在：小窗接管，服务不退
            store.log("watchdog: 主窗关了，小窗还在 —— 继续服务小窗")
            WINDOW["hwnd"] = None
            stale_logged = False
            continue

        if silent > 20 or not last:
            store.log(f"watchdog: 窗口句柄 {hwnd} 失效、也找不到新窗口"
                      f"（心跳静默 {silent:.0f}s），收工")
            shutdown("窗口关了")
            return


class InstanceLock:
    """进程级单实例锁。

    抢到锁的是「主实例」；抢不到说明已经有人在跑，从 `instance.port` 读出端口，
    把窗口指过去就行 —— 不会再起第二个服务。
    锁文件与端口文件分开：被锁住的字节别的进程读不了，所以不能把端口写在锁文件里。
    """

    def __init__(self) -> None:
        self.lock_path = store.DATA_DIR / "instance.lock"
        self.port_path = store.DATA_DIR / "instance.port"
        self.fh = None

    def acquire(self) -> bool:
        store.ensure_dirs()
        try:
            fh = open(self.lock_path, "a+b")
        except OSError:
            return True  # 锁文件都建不了，就别拦着启动了
        try:
            fh.seek(0)
            if fh.read(1) == b"":
                fh.seek(0)
                fh.write(b"L")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fh = fh
            return True
        except OSError:
            fh.close()
            return False

    def write_port(self, port: int) -> None:
        try:
            store._atomic_write(self.port_path, f"{port}\n")
        except OSError as exc:
            store.log(f"write port failed: {exc}")

    @staticmethod
    def read_port():
        try:
            txt = (store.DATA_DIR / "instance.port").read_text(
                encoding="utf-8", errors="ignore").strip()
            return int(txt.splitlines()[0]) if txt else None
        except (OSError, ValueError, IndexError):
            return None


def probe_workbench(port: int) -> bool:
    """这个端口上是不是自家服务。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/ping", timeout=0.4) as resp:
            return resp.status == 200 and b"workbench" in resp.read(400)
    except Exception:
        return False


def find_running(scan_all: bool = True) -> int | None:
    """找已经在跑的自家服务。

    `scan_all=False` 只探「上次记录的那个端口」。正常路径这就够了 ——
    端口号是主实例自己写进 instance.port 的，没必要把 23 个候选端口挨个敲一遍。
    实测每个死端口的探测要 ~36ms，24 次就是 ~870ms，全花在启动路径上白等。
    """
    preferred = InstanceLock.read_port()
    if preferred and probe_workbench(preferred):
        return preferred
    if not scan_all:
        return None
    for port in PORT_CANDIDATES:
        if not port or port == preferred:
            continue
        if probe_workbench(port):
            return port
    return None


def bind_server(preferred: int | None = None) -> tuple[ThreadingHTTPServer | None, int | None]:
    """绑一个可用端口。返回 `(server, 已跑着的端口)`。

    先试「上次用的那个端口」，再按候选列表走。

    关键：**正常路径一次端口探测都不做**。探测一个死端口要等 ~400ms 超时
    （防火墙把 SYN 丢了，不会立刻 RST），而「绑不上」是瞬时的 ——
    所以只在 bind 失败时才有必要去探「这个端口上是不是自家旧实例」。
    """
    order = ([preferred] if preferred else []) + PORT_CANDIDATES
    seen: set[int] = set()
    for port in order:
        if not port or port in seen:
            continue
        seen.add(port)
        try:
            srv = _QuietServer(("127.0.0.1", port), Handler)
            srv.daemon_threads = True
            return srv, None
        except OSError:
            # 端口被人占着。占它的是自家旧实例就复用它，否则换下一个。
            if probe_workbench(port):
                return None, port
            continue
    return None, None


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 下 SO_REUSEADDR 会让两个进程绑到同一个端口（Linux 不会），
    # 所以关掉它，改用 SO_EXCLUSIVEADDRUSE 做真正的独占绑定。
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        # 绕开 HTTPServer.server_bind 里的 socket.getfqdn()。
        # 本地服务只绑 127.0.0.1、用不到主机名，实测能省十几毫秒（13ms → 0.1ms）。
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        store.log("server error:\n" + traceback.format_exc())


def main() -> int:
    t0 = time.perf_counter()

    def mark(label: str) -> None:
        store.log(f"  startup +{(time.perf_counter() - t0) * 1000:.0f}ms  {label}")

    ap = argparse.ArgumentParser(description="工作台")
    ap.add_argument("--no-window", action="store_true", help="不要自动开窗口")
    ap.add_argument("--port", type=int, default=0, help="指定端口")
    ap.add_argument("--prefetch", action="store_true",
                    help="无窗口预生成当日简报缓存后退出（给 0 点定时任务用）")
    ap.add_argument("--dump-prompt", nargs="?", const="", default=None, metavar="DIR",
                    help="把今天的提示词写成文件后退出（交给外部模型代工，不花配置里的 Key）")
    ap.add_argument("--ingest", metavar="FILE", default=None,
                    help="把代工方写好的 JSON 收进当天简报缓存后退出")
    ap.add_argument("--dump-expand", nargs="?", const="", default=None, metavar="DIR",
                    help="导出还差模型级简述的条目（给外部模型代写第二轮）")
    ap.add_argument("--ingest-expand", metavar="FILE", default=None,
                    help="把代工方写好的各条简述收进当天缓存后退出")
    args = ap.parse_args()

    store.ensure_dirs()
    store.log(f"boot v{APP_VERSION} frozen={getattr(sys, 'frozen', False)}")
    mark("解释器就绪 + 目录就位")
    _purge_intro_once()

    if args.prefetch:
        return prebuild_once()
    if args.dump_prompt is not None:
        return dump_prompt(args.dump_prompt or None)
    if args.ingest:
        return ingest_once(args.ingest)
    if args.dump_expand is not None:
        return dump_expand(args.dump_expand or None)
    if args.ingest_expand:
        return ingest_expand_once(args.ingest_expand)

    global PORT_CANDIDATES
    if args.port:
        PORT_CANDIDATES = [args.port]

    lock = InstanceLock()
    got_lock = lock.acquire()
    mark("实例锁" + ("（拿到）" if got_lock else "（被占）"))

    if not got_lock:
        # 有别的实例在跑（它在握着锁）。探它的端口 —— 活着的端口连接是瞬时的，
        # 只有死端口才会等超时，所以这条路不慢。
        port = find_running(scan_all=False)
        if port is None:
            time.sleep(1.5)          # 对方可能刚启动、还没绑上端口
            port = find_running(scan_all=True)
        if port:
            store.log(f"already running on {port}, focusing")
            mark("探测到已有实例")
            if not args.no_window:
                actions.open_window(f"http://127.0.0.1:{port}")
                mark("窗口已发出")
            return 0
        store.log("lock held but no service found; starting a fresh instance anyway")

    # 走到这里说明我们是唯一实例：直接绑端口。
    # 「上次用的端口」优先，绑不上才去探（绑不上的大概率是别的程序，不是我们）。
    global _SERVER
    # 显式指定的端口优先，其次才是「上次用的那个」。
    # 但记录里的端口必须落在候选范围内才认 —— 测试或异常可能往 instance.port
    # 里写一个临时端口，不能让它把正式实例带偏。
    _recorded = InstanceLock.read_port()
    _pref = args.port or (_recorded if _recorded in PORT_CANDIDATES else None)
    if _recorded and _pref != _recorded:
        if _recorded not in PORT_CANDIDATES:
            store.log(f"忽略记录里越界的端口 {_recorded}，回到候选范围")
        else:
            store.log(f"记录端口 {_recorded} 被显式指定的 {_pref} 覆盖")
    _SERVER, live = bind_server(_pref)
    if live:
        store.log(f"port {live} already serving; focusing instead")
        mark("绑定失败但端口是我们自己的，改为聚焦")
        if not args.no_window:
            actions.open_window(f"http://127.0.0.1:{live}")
            mark("窗口已发出")
        return 0
    if _SERVER is None:
        store.log("no free port among candidates")
        return 1
    port = _SERVER.server_port
    lock.write_port(port)
    mark(f"端口 {port} 绑定")

    cfg = store.load_config()
    keepalive = int((cfg.get("power") or {}).get("keepalive_seconds") or 600)
    win = cfg.get("window") or {}

    threading.Thread(target=_SERVER.serve_forever, kwargs={"poll_interval": 0.3},
                     daemon=True).start()
    threading.Thread(target=_watchdog, args=(max(keepalive, 30),), daemon=True).start()
    store.log(f"serving on http://127.0.0.1:{port}")
    mark("服务开始监听")

    # 先把服务跑起来，再让后台去生成内容 —— 界面永远不等网络
    start_worker()
    # 跨日补刷：0 点之后只要还开着，就自己把今天那份补出来（见 _daily_watcher）
    threading.Thread(target=_daily_watcher, name="wb-daily", daemon=True).start()

    # 待办提醒：到点了就入队，等页面来接（页面关了也丢不了）
    threading.Thread(target=_reminder_loop, name="wb-reminder", daemon=True).start()
    # 小窗守护：钉住 / 记位置 / 认出手动改大小。窗口可能还没出现，keeper 自己会等
    miniwin.start_keeper(bool((cfg.get("mini") or {}).get("pinned", True)),
                         bool((cfg.get("mini") or {}).get("auto", True)))

    if not args.no_window:
        WINDOW.update(actions.open_window(f"http://127.0.0.1:{port}",
                                          int(win.get("width") or 1380),
                                          int(win.get("height") or 880)))
        store.log(f"窗口已发出 pid={WINDOW.get('pid')} exe={WINDOW.get('exe')}")
        mark("窗口已发出")
        # 立刻认一次窗口句柄。认不到也没关系 —— 看门狗每轮会再找。
        WINDOW["hwnd"] = actions.find_app_window()
        store.log(f"窗口句柄 hwnd={WINDOW.get('hwnd')}")

    # 浏览器配置清理是纯维护工作，几秒到十几秒不等 ——
    # 绝不能挡在「窗口出现在屏幕上」前面，所以挪到后台、并且晚一点再动手。
    if (cfg.get("power") or {}).get("sweep_browser_cache", True):
        threading.Thread(target=actions.deferred_sweep,
                         kwargs={"delay": 25}, name="wb-sweep", daemon=True).start()
        # 小窗用的是自己的浏览器配置（独立进程才能保证窗口大小/位置），
        # 那份缓存一样会长胖，一起清。
        threading.Thread(target=actions.deferred_sweep_mini,
                         kwargs={"delay": 32}, name="wb-sweep-mini", daemon=True).start()

    # 「上次开着小窗」的话，开机也把它摆回来（延迟 2 秒，别和主窗抢启动）
    if (cfg.get("mini") or {}).get("auto_open") and not args.no_window:
        def _reopen_mini():
            time.sleep(2.0)
            try:
                miniwin.open_mini(port, store.load_config())
            except Exception as exc:
                store.log(f"mini auto open failed: {exc}")
        threading.Thread(target=_reopen_mini, name="wb-mini-open", daemon=True).start()

    # 本地推导打标：纯本地活（读图片自带的 EXIF / 文件名），但也要扫几百个文件，
    # 所以同样挪到后台 + 延迟，绝不挡启动。
    threading.Thread(target=_deferred_tag, kwargs={"delay": 15},
                     name="wb-tag", daemon=True).start()
    # 介绍不再全库预生成：显示到哪张，build_state 才为哪张现写/升级。

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
