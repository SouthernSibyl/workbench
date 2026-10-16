"""配置 / 缓存的读写层。原子写 + 线程安全，纯标准库。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

APP_NAME = "Workbench"


def _roaming() -> Path:
    v = os.environ.get("APPDATA")
    if v:
        return Path(v)
    return Path.home() / "AppData" / "Roaming"


DATA_DIR = _roaming() / APP_NAME
CONFIG_PATH = DATA_DIR / "config.json"
CACHE_DIR = DATA_DIR / "cache"
ART_DIR = DATA_DIR / "art"
LOG_PATH = DATA_DIR / "workbench.log"

_lock = threading.RLock()

# ---------------------------------------------------------------- 默认启动器

# 默认是空的：启动项和这台机器强相关（装在哪、怎么启动只有用户自己知道），
# 所以首次运行不预设任何条目，由用户在界面上点「添加启动项」自己挑。
#
# 想改成「开箱带几个示例」，往下面这个列表里加 dict 即可，字段见 README：
#   kind="file"  -> {"id","title","subtitle","icon","accent","kind","path"}
#   kind="shell" -> {"id","title","subtitle","icon","accent","kind","command","cwd"}
#   kind="url"   -> {"id","title","subtitle","icon","accent","kind","url"}
#   kind="ebook" -> {"id","title","subtitle","icon","accent","kind",
#                    "node_dir","server","books","port"}
DEFAULT_LAUNCHERS: list[dict] = []

# 资讯源：只留「要闻 / 财经 / 科技 / 学术」，娱乐一律不进。
# 2026-10 实测：人民网 / 新华网的 RSS 已停更（仍在推 2025-06-05 的存档），
# 所以政经主力换成中新网各频道 + 界面新闻，全部是当天内容。
DEFAULT_FEEDS = [
    {"name": "中新网·国内", "url": "https://www.chinanews.com.cn/rss/china.xml", "cat": "要闻"},
    {"name": "中新网·滚动", "url": "https://www.chinanews.com.cn/rss/scroll-news.xml", "cat": "要闻"},
    {"name": "中新网·国际", "url": "https://www.chinanews.com.cn/rss/world.xml", "cat": "要闻"},
    {"name": "界面新闻", "url": "https://a.jiemian.com/index.php?m=article&a=rss", "cat": "要闻"},
    {"name": "中新网·财经", "url": "https://www.chinanews.com.cn/rss/finance.xml", "cat": "财经"},
    {"name": "IT之家", "url": "https://www.ithome.com/rss/", "cat": "科技"},
    {"name": "少数派", "url": "https://sspai.com/feed", "cat": "科技"},
    {"name": "Solidot", "url": "https://www.solidot.org/index.rss", "cat": "科技"},
    {"name": "爱范儿", "url": "https://www.ifanr.com/feed", "cat": "科技"},
    {"name": "量子位", "url": "https://www.qbitai.com/feed", "cat": "科技"},
    {"name": "arXiv cs.AI", "url": "http://export.arxiv.org/rss/cs.AI", "cat": "学术"},
    {"name": "Nature", "url": "https://www.nature.com/nature.rss", "cat": "学术"},
    {"name": "中新网·教育", "url": "https://www.chinanews.com.cn/rss/edu.xml", "cat": "学术"},
]

# 已停更的源，配置里出现它们就说明是老配置，自动换掉
DEAD_FEED_MARKERS = ("people.com.cn", "xinhuanet.com", "caixin.com", "thepaper.cn", "36kr.com")

# 娱乐 / 体育 / 生活八卦关键词，命中即丢弃（用户明确要求不要推送这类）
NEWS_EXCLUDE = [
    # 娱乐
    "明星", "综艺", "八卦", "绯闻", "恋情", "分手", "选秀", "追星", "饭圈",
    "剧集", "电视剧", "电影票房", "网红", "直播带货", "选美", "真人秀",
    "演唱会", "热搜榜", "娱记", "影评", "游戏攻略", "爱豆", "顶流",
    # 体育赛事
    "世巡赛", "联赛", "世界杯", "奥运会", "亚运会", "全运会", "夺冠", "球队",
    "球员", "球星", "球赛", "赛程", "战绩", "半决赛", "中超", "CBA", "NBA",
    "网球", "高尔夫", "拳击", "马拉松", "锦标赛", "热身赛", "积分榜",
    # 生活 / 猎奇 / 软文
    "养生", "减肥", "星座", "生肖运势", "情感故事", "奇闻", "猎奇",
    "带货", "种草", "探店", "网红打卡",
]

# 正经政经 / 学术关键词，用来在排序时加权（用户要的是新闻联播那种分量）
NEWS_BOOST = [
    "国务院", "中共中央", "政治局", "全国人大", "全国政协", "中央经济工作会议",
    "习近平", "李强", "外交部", "国防部", "发改委", "财政部", "人民银行", "央行",
    "政策", "改革", "规划", "条例", "意见", "通知", "发布", "签署", "会谈",
    "经济", "金融", "贸易", "出口", "进口", "关税", "投资", "产业", "制造业",
    "科技", "创新", "芯片", "人工智能", "能源", "航天", "量子", "生物",
    "中美", "欧盟", "联合国", "一带一路", "峰会", "论坛",
]

DEFAULT_CONFIG = {
    "profile": {"name": "", "city": "", "lat": None, "lon": None},
    "ai": {
        "mode": "api",
        "base_url": "",
        "api_key": "",
        "model": "",
        # 给图片打标用的多模态模型；留空则不传图
        "vision_model": "",
        "web_url": "https://chat.deepseek.com/",
        "enabled": True,
    },
    "news": {
        "feeds": DEFAULT_FEEDS,
        "count": 10,
        "exclude": NEWS_EXCLUDE,
        # 加权关键词以前是写死在代码里的常量，现在进配置，界面上能改
        "boost": NEWS_BOOST,
        "brief": True,
        # 阅读预算：所有条目简述加起来正好 15 分钟读完
        "read_minutes": 15,
        "chars_per_minute": 400,
        "max_age_days": 3,
    },
    "todos": [],
    "photos": {
        "pexels_key": "",
        "nasa_key": "",
        "nasa": True,
        "dirs": [],
        # 默认偏好：天象类排第一，其次是地形 / 植物 / 建筑
        "likes": ["天象", "地形", "植物", "建筑"],
        "rotate": True,
        # 本地推导属性（来源 / EXIF / 文件名）：零成本，默认开
        "tag_local": True,
        # 视觉模式打标：更准，但会把图传给模型、烧 token，默认关
        "vision_tagging": False,
        "retagged": True,
    },
    # 本机痕迹采集（决定简报里「与你相关」那一类）。关掉后不采集、提示词里也不出现。
    "signals": {"enabled": True, "browser_history": True, "days": 14},
    "anniversaries": [],
    "launchers": DEFAULT_LAUNCHERS,
    "window": {"width": 1420, "height": 900},
    # keepalive_seconds：多久没收到页面心跳就认为窗口关了。
    # 别调小 —— 浏览器会把后台窗口的定时器节流，电脑睡眠期间更是完全停摆，
    # 阈值太紧会把还在用的服务误杀（历史上被这个坑过）。
    "power": {"prefetch_tomorrow": True, "keepalive_seconds": 600,
              "sweep_browser_cache": True},
    "theme": "light",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, ART_DIR / "holiday", ART_DIR / "daily"):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


CONFIG_VERSION = 7


# ------------------------------------------------------------ 启动项（可自选文件）

# 图标底色：按 id 稳定取一个，不用用户操心配色
LAUNCH_ACCENTS = ["#3f7fc4", "#c9772f", "#4f8f6a", "#8a5fb0",
                  "#c0504d", "#3b8f9e", "#7a6a3f", "#5c6bc0"]

# 常见类型的友好名称，省得用户自己想标题
EXT_LABEL = {
    ".exe": "程序", ".lnk": "快捷方式", ".url": "链接", ".bat": "批处理", ".cmd": "脚本",
    ".pdf": "PDF", ".doc": "Word", ".docx": "Word", ".xls": "表格", ".xlsx": "表格",
    ".ppt": "幻灯片", ".pptx": "幻灯片", ".txt": "文本", ".md": "笔记",
    ".jpg": "图片", ".jpeg": "图片", ".png": "图片", ".webp": "图片",
    ".mp3": "音乐", ".flac": "音乐", ".wav": "音乐",
    ".mp4": "视频", ".mkv": "视频", ".avi": "视频", ".mov": "视频",
    ".zip": "压缩包", ".rar": "压缩包", ".7z": "压缩包",
    ".py": "脚本", ".mjs": "脚本", ".js": "脚本",
}


def launcher_id(title: str, path: str) -> str:
    """给启动项生成一个稳定、好认的 id。"""
    import hashlib as _h
    import re as _re
    base = _re.sub(r"[^0-9a-zA-Z\u4e00-\u9fa5]+", "-", title or "").strip("-").lower()
    if not base:
        base = _re.sub(r"[^0-9a-zA-Z\u4e00-\u9fa5]+", "-",
                       Path(path).stem or "item").strip("-").lower()
    base = base[:28] or "item"
    return f"{base}-{_h.sha1(str(path).encode('utf-8', 'ignore')).hexdigest()[:5]}"


def build_launcher(path: str, title: str = "", icon: str = "") -> dict:
    """从一个文件路径造出一个启动项。标题留空就按文件名推。"""
    p = Path(str(path or "").strip().strip('"')).expanduser()
    name = (title or "").strip()
    if not name:
        name = p.stem or p.name or "新启动项"
        name = name.replace("_", " ").strip()
    ext = p.suffix.lower()
    sub = EXT_LABEL.get(ext, "文件夹" if ext == "" else ext.lstrip(".").upper())
    if p.parent and str(p.parent) not in ("", "."):
        sub = f"{sub} · {p.parent}"
    lid = launcher_id(name, path)
    accent = LAUNCH_ACCENTS[int(lid[-2:], 16) % len(LAUNCH_ACCENTS)] \
        if all(c in "0123456789abcdef" for c in lid[-2:]) else LAUNCH_ACCENTS[0]
    entry = {
        "id": lid,
        "title": name[:24],
        "subtitle": sub[:60],
        "icon": (icon or "").strip(),
        "accent": accent,
        "kind": "file",
        "path": str(p),
    }
    if ext == ".url":
        entry["kind"] = "file"          # .url 交给系统关联打开就是浏览器
    return entry


def normalize_launchers(items) -> list[dict]:
    """清洗启动项列表：补齐字段、丢掉空壳、id 去重。"""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in (items or []):
        if not isinstance(raw, dict):
            continue
        entry = dict(raw)
        # 老配置里没有 kind 字段：有 command 的当年就是「跑命令」那一类
        kind = str(entry.get("kind") or ("shell" if entry.get("command") else "file"))
        if kind == "file":
            target = str(entry.get("path") or "").strip()
            if not target:
                continue
            entry["path"] = target
        elif kind == "shell":
            if not str(entry.get("command") or "").strip():
                continue
        elif kind == "url":
            if not str(entry.get("url") or "").strip():
                continue
        entry["kind"] = kind
        title = str(entry.get("title") or "").strip() or "启动项"
        entry["title"] = title[:24]
        entry["subtitle"] = str(entry.get("subtitle") or "")[:60]
        entry["icon"] = str(entry.get("icon") or "").strip()
        entry["accent"] = str(entry.get("accent") or "").strip() or LAUNCH_ACCENTS[0]
        lid = str(entry.get("id") or "").strip()
        if not lid or lid in seen:
            lid = launcher_id(title, entry.get("path") or entry.get("command") or lid)
        while lid in seen:
            lid += "x"
        entry["id"] = lid
        seen.add(lid)
        out.append(entry)
    return out


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate(cfg: dict) -> tuple[dict, bool]:
    """把老版本的 config.json 升到当前版本。

    老配置会盖住新默认值（用户配置优先），所以新增的字段得像这样补进来。
    """
    try:
        ver = int(cfg.get("configVersion") or 1)
    except (TypeError, ValueError):
        ver = 1
    if ver >= CONFIG_VERSION:
        return cfg, False

    changed = False

    # --- 启动器：补齐字段、清洗老配置里的多余键 ---
    old = list(cfg.get("launchers") or [])
    cleaned = normalize_launchers(old)
    if cleaned != old:
        cfg["launchers"] = cleaned
        changed = True

    # --- 资讯源：老配置没有分类字段，或含已停更的源，整体换成新的实时源 ---
    news = cfg.get("news") or {}
    feeds = [f for f in (news.get("feeds") or []) if isinstance(f, dict)]
    missing_cat = not feeds or not all(f.get("cat") for f in feeds)
    has_dead = any(any(m in str(f.get("url") or "") for m in DEAD_FEED_MARKERS)
                   for f in feeds)
    if ver < 3 or missing_cat or has_dead:
        cfg["news"] = {**news, "feeds": [dict(f) for f in DEFAULT_FEEDS]}
        changed = True
    if ver < 4 or not (cfg.get("news") or {}).get("exclude"):
        cfg["news"] = {**(cfg.get("news") or {}), "exclude": list(NEWS_EXCLUDE)}
        changed = True
    if int((cfg.get("news") or {}).get("count") or 0) in (0, 6, 8):
        cfg["news"] = {**(cfg.get("news") or {}), "count": DEFAULT_CONFIG["news"]["count"]}
        changed = True
    if ver < 5:
        news5 = cfg.get("news") or {}
        news5.setdefault("read_minutes", DEFAULT_CONFIG["news"]["read_minutes"])
        news5.setdefault("chars_per_minute", DEFAULT_CONFIG["news"]["chars_per_minute"])
        news5.setdefault("max_age_days", DEFAULT_CONFIG["news"]["max_age_days"])
        cfg["news"] = news5
        cfg.setdefault("todos", [])
        changed = True

    if ver < 6:
        # 启动项升级成「可自选文件」：补齐 kind/path 并把老字段清洗干净
        cfg["launchers"] = normalize_launchers(cfg.get("launchers"))
        cfg.setdefault("photos", {})
        changed = True

    if ver < 7:
        # 一批原来写死在代码里的东西搬进了配置（政经加权词 news.boost、心跳阈值
        # power.keepalive_seconds、窗口尺寸、隐私开关 signals.* …），
        # 并且全部暴露到设置界面（见 app/settings_schema.py）。
        # 缺的键已由 _deep_merge 补上默认值，这里把版本推上去让它落实盘。
        for key in ("news", "photos", "signals", "power", "ai", "window"):
            cfg.setdefault(key, {})
        changed = True

    cfg["configVersion"] = CONFIG_VERSION
    return cfg, True


def load_config() -> dict:
    """读取配置：缺字段用默认值补齐，老版本自动迁移并落盘。"""
    with _lock:
        raw = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        merged = _deep_merge(DEFAULT_CONFIG, raw)
        merged, changed = _migrate(merged)
        if changed:
            try:
                _atomic_write(CONFIG_PATH,
                              json.dumps(merged, ensure_ascii=False, indent=2))
            except OSError:
                pass
        return merged


def save_config(cfg: dict) -> dict:
    merged = _deep_merge(DEFAULT_CONFIG, cfg or {})
    merged["configVersion"] = CONFIG_VERSION
    with _lock:
        _atomic_write(CONFIG_PATH, json.dumps(merged, ensure_ascii=False, indent=2))
    return merged


def patch_config(**sections) -> dict:
    with _lock:
        cfg = load_config()
        cfg = _deep_merge(cfg, sections)
        return save_config(cfg)


# ------------------------------------------------------------------- 缓存

def cache_path(key: str) -> Path:
    safe = "".join(c for c in key if c.isalnum() or c in "-_.")
    return CACHE_DIR / f"{safe}.json"


def read_cache(key: str):
    p = cache_path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_cache(key: str, obj) -> None:
    with _lock:
        _atomic_write(cache_path(key), json.dumps(obj, ensure_ascii=False, indent=2))


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        ensure_dirs()
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
