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
import calendar_cn
import photos
import settings_schema
import sources
import store
import weather

APP_NAME = "工作台"
APP_VERSION = "0.1.0"
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
    return best[1], best[2]


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
    is_today = daily is not None
    if daily is None:
        daily, _old_date = _latest_daily()
    daily = daily or {}

    # 壁纸是「算」出来的：按今天的天气/时段/季节/天象事件打分，
    # 一天（严格说是一个时段）只算一次，算完存起来，前端只管显示。
    chosen = photos.choose_daily(cfg, ctx, wx)
    hero = ([chosen["main"]] if chosen.get("main") else []) + (chosen.get("alts") or [])

    with LOCK:
        generating = STATE["generating"]
        note = STATE["note"]
        rev = STATE["rev"]

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
            "quote": (daily.get("fields") or {}).get("quote", ""),
            "quoteFrom": (daily.get("fields") or {}).get("quoteFrom", ""),
            "recommend": (daily.get("fields") or {}).get("recommend") or {},
            "tags": daily.get("tags") or (daily.get("fields") or {}).get("tags") or [],
            "brief": (daily.get("news") or {}).get("brief") or "",
            "items": (daily.get("news") or {}).get("items") or [],
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
        },
        "todos": cfg.get("todos") or [],
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
        try:
            text = ai.generate_summary(cfg, items[i])
            ai.save_summary(cfg, ctx, i, text)
            return i, True, len(text)
        except Exception as exc:
            store.log(f"expand_all[{i}] failed: {exc}")
            return i, False, str(exc)[:120]

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, i) for i in todo]
        for fut in cf.as_completed(futs):
            i, ok, info = fut.result()
            with LOCK:
                STATE["expanding"]["done"] += 1
                STATE["rev"] += 1
            store.log(f"expand_all {i}: {'ok ' + str(info) + ' 字' if ok else 'fail ' + str(info)}")

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
            need = ai.needs_refresh(cache, ctx["iso"])
            if need and cache:
                why = f"缓存生成于 {str(cache.get('generatedAt'))[:16]}，已过夜"
            else:
                why = "没有缓存"
        else:
            need = cache is None
            why = "没有缓存"

        if need:
            with LOCK:
                STATE["note"] = "正在整理今日简报"
            store.log(f"refresh today's briefing: {why}")
            ai.build_daily(cfg, ctx, force=True, wx=wx)

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
        elif ai.ai_ready(cfg):
            need = ai.needs_refresh(cache, ctx["iso"])
        else:
            need = cache is None
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
        if path == "/api/ping":
            return self._json({"app": "workbench", "version": APP_VERSION,
                               "port": getattr(self.server, "server_port", 0)})
        if path == "/api/state":
            return self._json(build_state())
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

        if path == "/api/launch":
            cfg = store.load_config()
            wanted = str(body.get("id") or "")
            entry = next((l for l in (cfg.get("launchers") or []) if l.get("id") == wanted), None)
            if entry is None:
                return self._json({"ok": False, "message": "找不到这个启动项"}, 404)
            result = actions.launch(cfg, entry)
            store.log(f"launch {wanted}: {result}")
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
            else:
                return self._json({"ok": False, "message": "不认识的操作"}, 400)

            items = store.normalize_launchers(items)
            store.patch_config(launchers=items)
            with LOCK:
                STATE["rev"] += 1
            store.log(f"launchers {act}: 现在 {len(items)} 项")
            return self._json({"ok": True, "message": msg, "launchers": items})

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
            todos = list(cfg.get("todos") or [])
            act = str(body.get("action") or "")
            now = _dt.datetime.now().isoformat(timespec="seconds")
            if act == "add":
                text = str(body.get("text") or "").strip()[:200]
                if not text:
                    return self._json({"ok": False, "message": "内容为空"}, 400)
                todos.append({"id": f"t{int(time.time()*1000)}", "text": text,
                              "done": False, "at": now})
            elif act == "toggle":
                tid = str(body.get("id") or "")
                for t in todos:
                    if t.get("id") == tid:
                        t["done"] = not t.get("done")
            elif act == "remove":
                tid = str(body.get("id") or "")
                todos = [t for t in todos if t.get("id") != tid]
            elif act == "edit":
                tid = str(body.get("id") or "")
                text = str(body.get("text") or "").strip()[:200]
                for t in todos:
                    if t.get("id") == tid and text:
                        t["text"] = text
            elif act == "clear_done":
                todos = [t for t in todos if not t.get("done")]
            else:
                return self._json({"ok": False, "message": "未知操作"}, 400)
            store.patch_config(todos=todos)
            with LOCK:
                STATE["rev"] += 1
            return self._json({"ok": True, "todos": todos})

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

        hwnd = WINDOW.get("hwnd")
        if not hwnd:
            hwnd = actions.find_app_window()
            if hwnd:
                WINDOW["hwnd"] = hwnd
                store.log(f"watchdog: 认到窗口句柄 {hwnd}")
        if not hwnd:
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
    args = ap.parse_args()

    store.ensure_dirs()
    store.log(f"boot v{APP_VERSION} frozen={getattr(sys, 'frozen', False)}")
    mark("解释器就绪 + 目录就位")

    if args.prefetch:
        return prebuild_once()

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

    # 本地推导打标：纯本地活（读图片自带的 EXIF / 文件名），但也要扫几百个文件，
    # 所以同样挪到后台 + 延迟，绝不挡启动。
    threading.Thread(target=_deferred_tag, kwargs={"delay": 15},
                     name="wb-tag", daemon=True).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
