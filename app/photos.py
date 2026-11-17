"""配图库：一次把「节日 / 节气 / 天气 / 光线 / 兴趣题材」全部照全。

图源（按可用性排序，全部本地缓存，显示时不再联网）：
  1. local    —— 你自己的摄影文件夹（最优先，也最像你）
  2. pexels   —— 配了免费 key 就能按主题关键词真搜（风景/植物/地形/建筑/天气/光线）
  3. spotlight—— Windows 聚焦图库（4K，Getty/500px，带中文标题与完整描述）
  4. bing     —— Bing 每日壁纸（带中文描述，每天新增）

设计要点：**下载到本地**，所以面板打开是零延迟、断网也能看；
每次同步把新图并入库，旧的按容量上限清理。
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as _dt
import hashlib
import html
import json
import os
import random
import re
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import attrs as _attrs
import store

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 同一时刻只允许一轮同步。否则两次同步会互相覆盖索引：
# 实测留下的后果是索引从 176 条被写回 171 条、磁盘上多出 88 个孤儿文件。
_SYNC_LOCK = threading.Lock()

LIB_DIR = store.ART_DIR / "library"
LIB_INDEX = "photos"
MAX_FILES = 320
MAX_BYTES = 14 * 1024 * 1024
# 容量到了先淘汰这些（随手能再抓一批的）
PRUNE_FIRST = {"bing", "spotlight"}
# 这些是稀缺资源，尽量留到最后
PRUNE_LAST = {"local", "astro", "apod", "nasa"}

SPOTLIGHT_URL = ("https://fd.api.iris.microsoft.com/v4/api/selection"
                 "?placement={pl}&bcnt=8&country=cn&locale=zh-CN&fmt=json&page={pg}")
SPOTLIGHT_PLACEMENTS = ["88000820", "88000821"]
# 服务端每次都从大池子里随机取样，所以多翻几页能捞出几十上百张互不相同的图。
SPOTLIGHT_PAGES = 12
BING_URL = "https://www.bing.com/HPImageArchive.aspx?format=js&idx={idx}&n=8&mkt=zh-CN"
DOWNLOAD_WORKERS = 5

# ------------------------------------------------------------------ 标签体系
#
# 打标与匹配整套挪到 attrs.py（三层属性 + 中英双语）。这里只做一层包装，
# 让索引里存的是「结构化的属性」而不是一堆散词。

PEXELS_QUERIES = [
    "landscape photography golden hour", "mountain range sunrise fog",
    "forest trees mist autumn", "lake reflection calm water",
    "architecture old town street", "sea coast cliffs waves",
    "flowers plants macro nature", "desert dunes canyon terrain",
    "snow winter landscape", "city skyline dusk blue hour",
    "milky way night sky stars", "aurora northern lights",
]

# NASA 图片搜索：天文这块的主力
NASA_QUERIES = [
    "milky way galaxy night sky", "aurora northern lights", "nebula deep space",
    "meteor shower", "star trails long exposure", "lunar eclipse",
    "full moon", "saturn jupiter planet", "earth from space",
    "observatory telescope",
]


def attrs_of(text: str) -> dict:
    """给一段描述打属性（见 attrs.py）。"""
    return _attrs.describe(text)


def _flat_tags(text: str) -> list[str]:
    """索引里存的扁平标签（保持向后兼容，同时带上层类别名）。"""
    d = attrs_of(text)
    flat: list[str] = []
    for layer, cats in d.items():
        if layer == "flat":
            continue
        flat.extend(cats)
    flat.extend(d.get("flat") or [])
    return list(dict.fromkeys(flat))[:28]


# -------------------------------------------------------------------- 图源

def _http_json(url: str, headers: dict | None = None, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def fetch_spotlight(pages: int = SPOTLIGHT_PAGES) -> list[dict]:
    """多页捞图。每页 4 张、内容随机不重复，翻 12 页通常能拿到 70+ 张唯一图。"""
    out: list[dict] = []
    seen: set[str] = set()
    jobs = [(pl, pg) for pl in SPOTLIGHT_PLACEMENTS for pg in range(1, pages + 1)]

    def one(job):
        pl, pg = job
        got = []
        try:
            d = _http_json(SPOTLIGHT_URL.format(pl=pl, pg=pg), timeout=18)
            for it in (d.get("batchrsp") or {}).get("items") or []:
                o = json.loads(it["item"])
                ad = o.get("ad") or {}
                asset = (ad.get("landscapeImage") or {}).get("asset") or ""
                if not asset:
                    continue
                got.append({
                    "src": "spotlight", "url": asset,
                    "title": (ad.get("title") or "").strip(),
                    "desc": (ad.get("description") or "").strip(),
                    "credit": (ad.get("copyright") or "").strip(),
                    "w": 3840, "h": 2160,
                })
        except Exception as exc:
            store.log(f"spotlight {pl}/{pg} failed: {exc}")
        return got

    # 低并发：这个接口并发高了会限流
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for got in ex.map(one, jobs):
            for ph in got:
                k = ph["url"]
                if k in seen:
                    continue
                seen.add(k)
                out.append(ph)
    return out


def fetch_bing() -> list[dict]:
    out: list[dict] = []
    for idx in (0, 8):
        try:
            d = _http_json(BING_URL.format(idx=idx), timeout=15)
            for im in d.get("images") or []:
                url = im.get("url") or ""
                if not url:
                    continue
                if url.startswith("/"):
                    url = "https://www.bing.com" + url
                out.append({
                    "src": "bing", "url": url,
                    "title": (im.get("title") or "").strip(),
                    "desc": (im.get("copyright") or "").strip(),
                    "credit": (im.get("copyright") or "").strip(),
                    "w": 1920, "h": 1080,
                    "date": (im.get("startdate") or ""),
                })
        except Exception as exc:
            store.log(f"bing idx={idx} failed: {exc}")
    return out


def fetch_pexels(key: str, queries: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    if not key:
        return out
    for q in (queries or PEXELS_QUERIES):
        try:
            url = ("https://api.pexels.com/v1/search?" + urllib.parse.urlencode({
                "query": q, "per_page": 8, "orientation": "landscape", "size": "large",
            }))
            d = _http_json(url, headers={"Authorization": key}, timeout=20)
            for p in d.get("photos") or []:
                src = (p.get("src") or {})
                u = src.get("large2x") or src.get("large") or src.get("original") or ""
                if not u:
                    continue
                out.append({
                    "src": "pexels", "url": u,
                    "title": (p.get("alt") or q).strip()[:80],
                    "desc": f"{q}。{(p.get('alt') or '').strip()}",
                    "credit": f"© {p.get('photographer') or 'Pexels'} / Pexels",
                    "link": p.get("url") or "",
                    "w": p.get("width") or 1880, "h": p.get("height") or 1057,
                })
        except Exception as exc:
            store.log(f"pexels '{q}' failed: {exc}")
    return out


# --------------------------------------------------------------- NASA（天文）

NASA_APOD_URL = ("https://api.nasa.gov/planetary/apod"
                 "?api_key={key}&start_date={start}&end_date={end}")
NASA_SEARCH_URL = ("https://images-api.nasa.gov/search"
                   "?q={q}&media_type=image&page_size=12")
NASA_APOD_CHUNK = 30          # 一次要太多会超时，30 天一段正好
NASA_APOD_CHUNKS = 4
# 值得当壁纸的线索
_NASA_WANT = ["milky way", "galaxy", "nebula", "aurora", "meteor", "star", "night sky",
              "moon", "eclipse", "sunset", "sunrise", "horizon", "earth", "landscape",
              "cloud", "ocean", "mountain", "desert", "ice", "glacier", "observatory",
              "twilight", "comet", "space", "launch", "rocket"]
# 一看就是机构照 / 人像，不要
_NASA_AVOID = ["crew", "astronaut", "spacesuit", "briefing", "press conference", "meeting",
               "award", "ceremony", "visitor", "technician", "engineer", "mockup",
               "portrait", "training", "headquarters", "signing", "administrator"]
# 视频页误标成图片的占位内容
_NASA_PLACEHOLDER = ["to view this video", "enable javascript", "youtube.com/embed"]


def _nasa_good(title: str, desc: str) -> bool:
    blob = f"{title} {desc}".lower()
    if any(p in blob for p in _NASA_PLACEHOLDER):
        return False
    if not any(w in blob for w in _NASA_WANT):
        return False
    if any(w in blob for w in _NASA_AVOID):
        return False
    return True


def fetch_nasa(cfg: dict, chunks: int = NASA_APOD_CHUNKS) -> list[dict]:
    """NASA 图源：APOD（每日一图，带完整英文说明）+ Images 搜索（按主题）。

    用户要的是「我喜欢天文摄影」，而 Windows 聚焦 / Bing 壁纸里星空的比重很低，
    所以单独开一路。两边都是公开接口、不需要 key（DEMO_KEY 够用）。

    注意：APOD 一次要 120 天会超时，得按 30 天分段拉；偶尔某段被限流就跳过，
    别让整轮失败。
    """
    out: list[dict] = []
    key = str(((cfg.get("photos") or {}).get("nasa_key")) or "DEMO_KEY").strip()

    # 1) APOD：分段拉，带长说明文字，打标质量最好。
    #    它有小时配额（DEMO_KEY 每小时 30 次），而内容是每天一更 ——
    #    所以 20 小时内只拉一次，别每轮同步都去撞限流。
    meta = store.read_cache("nasa-apod-meta") or {}
    apod_fresh = False
    try:
        last = _dt.datetime.fromisoformat(str(meta.get("at") or ""))
        apod_fresh = (_dt.datetime.now() - last).total_seconds() < 20 * 3600
    except (ValueError, TypeError):
        apod_fresh = False

    end = _dt.date.today()
    apod_ok = 0
    for i in range(0 if apod_fresh else max(1, chunks)):
        seg_end = end - _dt.timedelta(days=NASA_APOD_CHUNK * i)
        seg_start = seg_end - _dt.timedelta(days=NASA_APOD_CHUNK - 1)
        try:
            d = _http_json(NASA_APOD_URL.format(key=key, start=seg_start, end=seg_end),
                           timeout=28)
            if isinstance(d, dict):
                d = [d]
            for x in d or []:
                if not isinstance(x, dict) or x.get("media_type") != "image":
                    continue
                title = str(x.get("title") or "").strip()
                desc = re.sub(r"\s+", " ", str(x.get("explanation") or "")).strip()
                if not _nasa_good(title, desc):
                    continue
                url = x.get("hdurl") or x.get("url") or ""
                if not url:
                    continue
                copy = str(x.get("copyright") or "").strip()
                date = str(x.get("date") or "")
                out.append({
                    "src": "apod", "url": url,
                    "title": title, "desc": desc[:600],
                    "credit": (f"© {copy} / NASA APOD" if copy else "NASA APOD"),
                    "link": ("https://apod.nasa.gov/apod/ap"
                             + date.replace("-", "")[2:] + ".html") if date else "",
                    "w": 1920, "h": 1080, "date": date,
                })
                apod_ok += 1
        except Exception as exc:
            store.log(f"nasa apod 段{i + 1} failed: {exc}")
    if apod_ok:
        store.write_cache("nasa-apod-meta",
                          {"at": _dt.datetime.now().isoformat(timespec="seconds"),
                           "got": apod_ok})

    # 2) NASA Images：按主题补一批（银河 / 极光 / 星云 / 流星…）
    for q in NASA_QUERIES:
        try:
            d = _http_json(NASA_SEARCH_URL.format(q=urllib.parse.quote(q)), timeout=20)
            for it in ((d.get("collection") or {}).get("items") or []):
                meta = (it.get("data") or [{}])[0]
                title = str(meta.get("title") or "").strip()
                desc = re.sub(r"\s+", " ", str(meta.get("description") or "")).strip()
                if not _nasa_good(title, desc):
                    continue
                # 尺寸档位里挑一张够大的横幅图
                best = None
                for cand in (it.get("links") or []):
                    if cand.get("render") != "image":
                        continue
                    w = int(cand.get("width") or 0)
                    h = int(cand.get("height") or 0)
                    if w >= 1600 and w >= h * 1.2 and (best is None or w > int(best.get("width") or 0)):
                        best = cand
                if best is None:
                    continue
                out.append({
                    "src": "nasa", "url": best.get("href") or "",
                    "title": title, "desc": desc[:600],
                    "credit": f"NASA · {meta.get('center') or 'Image Library'}",
                    "link": f"https://images.nasa.gov/details/{meta.get('nasa_id') or ''}",
                    "w": int(best.get("width") or 0), "h": int(best.get("height") or 0),
                    "date": str(meta.get("date_created") or "")[:10],
                })
        except Exception as exc:
            store.log(f"nasa images '{q}' failed: {exc}")
    store.log(f"nasa fetched: apod+nasa {len(out)} 张")
    return out


def retag(force: bool = False) -> dict:
    """给已有条目重新打标。

    升级属性体系之后必须跑一次，否则老条目还是旧标签，
    新加的「事件/时段」层一个都命不中。
    """
    items = _load_index()
    changed = 0
    for it in items:
        if not force and isinstance(it.get("attrs"), dict) and it["attrs"]:
            continue
        text = f"{it.get('title','')} {it.get('desc','')} {it.get('credit','')}"
        parsed = attrs_of(text)
        it["tags"] = _flat_tags(text)
        it["attrs"] = {k: v for k, v in parsed.items() if k != "flat"}
        it["category"] = category_of(it)
        changed += 1
    if changed:
        _save_index(items)
        store.log(f"retag: 重新打了 {changed} 张的属性")
    return {"total": len(items), "retagged": changed,
            "bySource": _count_by(items, "src")}


def _count_by(items: list[dict], field: str) -> dict:
    out: dict[str, int] = {}
    for i in items:
        k = str(i.get(field) or "?")
        out[k] = out.get(k, 0) + 1
    return out


# ------------------------------------------------- ESA / ESO 天文图库（免 key）

# 这三个是专业天文台自己的图片流，质量比「图片搜索」高一个档次，
# 而且不需要任何 key。enclosure 里给的是 screen 尺寸（约 400KB、1920 级别），
# 拿来当壁纸正好；large 档动辄 5MB，几十张就能吃掉几百兆，不用。
ASTRO_FEEDS = [
    ("https://esahubble.org/images/feed/", "ESA/Hubble", "哈勃空间望远镜"),
    ("https://esawebb.org/images/feed/", "ESA/Webb", "韦布空间望远镜"),
    ("https://www.eso.org/public/images/feed/", "ESO", "欧洲南方天文台"),
]
_ITEM_RE = re.compile(r"<(item|entry)\b.*?</\1>", re.S)
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_ENC_RE = re.compile(r'<enclosure[^>]*url=["\']([^"\']+)["\']', re.I)

# 天文台图库里混着光谱图、光变曲线、会议公告 —— 这些不能当壁纸。
# 但「artist's concept / impression」要留：黑洞、系外行星的想象图往往很好看。
_ASTRO_AVOID = ["spectrum", "spectra", "spectral", "diagram", "chart", "plot",
                "light curve", "schematic", "infographic", "timeline", "poster",
                "workshop", "symposium", "conference", "meeting", "announcement",
                "press release", "award", "call for", "deadline", "proposal",
                "data release", "catalogue", "catalog", "table"]


def _astro_good(title: str, desc: str) -> bool:
    blob = f"{title} {desc}".lower()
    return not any(w in blob for w in _ASTRO_AVOID)


def _tag_text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S | re.I)
    if not m:
        return ""
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
    return html.unescape(s)


def _strip_html(s: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _http_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                              "Accept": "application/rss+xml,text/xml,*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read().decode("utf-8", "ignore")


def fetch_astro(cfg: dict, per_feed: int = 24) -> list[dict]:
    """天文台图片流：哈勃 / 韦布 / ESO。免 key，画质好，正对「喜欢天文摄影」。"""
    out: list[dict] = []
    for url, label, cn in ASTRO_FEEDS:
        try:
            raw = _http_text(url, timeout=20)
        except Exception as exc:
            store.log(f"astro feed failed {label}: {exc}")
            continue
        for m in _ITEM_RE.finditer(raw):
            block = m.group(0)
            title = _strip_html(_tag_text(block, "title"))
            link = _strip_html(_tag_text(block, "link"))
            desc = _strip_html(_tag_text(block, "description"))
            enc = _ENC_RE.search(block)
            img = enc.group(1) if enc else ""
            if not img:
                im = _IMG_RE.search(html.unescape(_tag_text(block, "description")))
                img = im.group(1) if im else ""
            if not img or not title:
                continue
            if not _astro_good(title, desc):
                continue
            out.append({
                "src": "astro", "url": img,
                "title": title, "desc": desc[:600],
                "credit": f"{label} · {cn}",
                "link": link,
                "w": 1920, "h": 1080,
                "date": _strip_html(_tag_text(block, "pubDate"))[:31],
            })
            if len(out) >= per_feed * len(ASTRO_FEEDS):
                break
    return out


def scan_local(cfg: dict) -> list[dict]:
    """你自己的摄影文件夹。文件名里的中文会被当成描述参与匹配。"""
    out: list[dict] = []
    dirs = ((cfg.get("photos") or {}).get("dirs")) or []
    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    for d in dirs:
        p = Path(str(d)).expanduser()
        if not p.is_dir():
            continue
        for f in sorted(p.rglob("*")):
            if f.suffix.lower() not in exts or not f.is_file():
                continue
            try:
                if f.stat().st_size > MAX_BYTES:
                    continue
            except OSError:
                continue
            out.append({"src": "local", "url": f.as_uri(), "path": str(f),
                        "title": f.stem, "desc": f.stem.replace("-", " ").replace("_", " "),
                        "credit": "本人作品", "w": 0, "h": 0})
            if len(out) >= 120:
                return out
    return out


# -------------------------------------------------------------------- 下载

def _pid(photo: dict) -> str:
    raw = photo.get("path") or photo.get("url") or ""
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def _looks_like_image(head: bytes) -> bool:
    return (head[:3] == b"\xff\xd8\xff"                       # jpeg
            or head[:8] == b"\x89PNG\r\n\x1a\n"               # png
            or head[:4] == b"RIFF" and head[8:12] == b"WEBP"  # webp
            or head[:2] == b"BM")                             # bmp


def _download(photo: dict) -> str | None:
    dst = LIB_DIR / f"{_pid(photo)}.jpg"
    if dst.exists() and dst.stat().st_size > 4096:
        return dst.name
    if photo.get("path"):
        try:
            import shutil
            shutil.copy2(photo["path"], dst)
            return dst.name
        except OSError:
            return None
    # 临时名带上进程与线程号：两个 worker 撞同一张图时不会再互相抢文件
    tmp = dst.with_name(f"{dst.stem}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        req = urllib.request.Request(photo["url"], headers={"User-Agent": UA,
                                                            "Accept": "image/*,*/*"})
        with urllib.request.urlopen(req, timeout=40, context=CTX) as r:
            head = r.read(16)
            if not _looks_like_image(head):
                return None
            total = 0
            with open(tmp, "wb") as fh:
                fh.write(head)
                total += len(head)
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return None
                    fh.write(chunk)
        os.replace(tmp, dst)
        return dst.name
    except Exception as exc:
        store.log(f"download failed {photo.get('url','')[:70]}: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


_INDEX_CACHE: dict = {"mtime": 0.0, "items": []}


def _load_index() -> list[dict]:
    """读图库索引，带一层内存缓存。

    索引现在有几百条、带完整属性，每次 /api/state 都重新解析一遍太浪费
    （那是请求路径）。文件没变就直接复用。
    """
    try:
        mt = store.cache_path(LIB_INDEX).stat().st_mtime
    except OSError:
        mt = 0.0
    if mt and mt == _INDEX_CACHE.get("mtime"):
        return _INDEX_CACHE["items"]
    data = store.read_cache(LIB_INDEX)
    items = data if isinstance(data, list) else []
    _INDEX_CACHE["mtime"] = mt
    _INDEX_CACHE["items"] = items
    return items


def _save_index(items: list[dict], newer_than: str = "") -> None:
    """写索引；把「本轮开始之后别人才写进去的」条目并回来。

    以前是直接覆盖，两轮同步同时收尾会把对方刚写进去的条目抹掉
    （实测 176 条被写回 171 条，磁盘上留下 88 个索引里没有的孤儿）。

    但不能无脑并集 —— 那样 `_prune()` 淘汰旧图就永远删不掉了。
    所以只保留 `addedAt >= newer_than`（也就是本轮开始后才入库的）；
    本轮的 items 对自己知道的每一条都有最终决定权。传空字符串则是完全以 items 为准。
    """
    latest = _load_index()
    merged: dict[str, dict] = {}
    for it in items:
        if isinstance(it, dict) and (it.get("id") or it.get("file")):
            merged[it.get("id") or it.get("file")] = it
    kept = 0
    for it in latest:
        if not isinstance(it, dict):
            continue
        key = it.get("id") or it.get("file")
        if not key or key in merged:
            continue
        if newer_than and str(it.get("addedAt") or "") >= newer_than:
            merged[key] = it
            kept += 1
    if kept:
        store.log(f"index merged: 并回 {kept} 条他处新入库的图")
    store.write_cache(LIB_INDEX, list(merged.values()))


def cleanup_orphans(dry_run: bool = True) -> dict:
    """清掉索引里没有、也没有任何条目引用的图（并发同步留下的孤儿与 .part 残留）。"""
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    ref = {i.get("file") for i in _load_index()}
    orphans: list[str] = []
    freed = 0
    for p in sorted(LIB_DIR.iterdir()):
        if not p.is_file() or p.name in ref:
            continue
        try:
            freed += p.stat().st_size
        except OSError:
            continue
        orphans.append(p.name)
        if not dry_run:
            try:
                p.unlink()
            except OSError as exc:
                store.log(f"orphan unlink failed {p.name}: {exc}")
    if not dry_run and orphans:
        store.log(f"photos cleanup: 清掉 {len(orphans)} 个孤儿，释放 {freed / 1048576:.1f} MB")
    return {"orphans": len(orphans), "bytes": freed, "dryRun": dry_run,
            "sample": orphans[:6]}


def _prune(items: list[dict]) -> list[dict]:
    """超容量就淘汰。淘汰顺序是「先扔随手能再抓的，后扔稀缺的」。

    天文图（哈勃/韦布/ESO/APOD）很难再凑齐，**自己的照片更是碰都不碰** ——
    所以聚焦/Bing 壁纸先走，自己的照片一个都不淘汰。

    注意：local 条目要整个排除在淘汰名单之外。只保留文件、把索引条目丢掉的话，
    下一轮孤儿清理会把它当垃圾删掉。
    """
    if len(items) <= MAX_FILES:
        return items

    mine = [it for it in items if str(it.get("src") or "") == "local"]
    others = [it for it in items if str(it.get("src") or "") != "local"]
    need = len(items) - MAX_FILES
    if need <= 0 or not others:
        return items

    def rank(it: dict) -> tuple[int, str]:
        src = str(it.get("src") or "")
        tier = 0 if src in PRUNE_FIRST else (2 if src in PRUNE_LAST else 1)
        return tier, str(it.get("addedAt") or "")

    ordered = sorted(others, key=rank)
    drop, keep = ordered[:need], ordered[need:]
    for it in drop:
        try:
            (LIB_DIR / it["file"]).unlink(missing_ok=True)
        except OSError:
            pass
    store.log(f"prune: 淘汰 {len(drop)} 张（优先聚焦/Bing），保留自己的 {len(mine)} 张")
    return mine + keep


def sync(cfg: dict, force: bool = False, pages: int = SPOTLIGHT_PAGES) -> dict:
    """一次把各个题材都抓一轮，新图下载入库。

    - 同一时刻只跑一轮（拿不到锁就直接返回，不排队、不打架）
    - 默认 6 小时内不重复同步（下载挺重），force=True 时强制
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        store.log("photos sync skipped: 已有一轮在跑")
        return {"total": len(_load_index()), "added": 0, "skipped": "busy"}
    try:
        return _sync_locked(cfg, force, pages)
    finally:
        _SYNC_LOCK.release()


def _sync_locked(cfg: dict, force: bool = False,
                 pages: int = SPOTLIGHT_PAGES) -> dict:
    started = _dt.datetime.now().isoformat(timespec="seconds")
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    meta = store.read_cache("photos-meta") or {}
    if not force and meta.get("lastSync"):
        try:
            age = _dt.datetime.now() - _dt.datetime.fromisoformat(meta["lastSync"])
            if age.total_seconds() < 6 * 3600 and _load_index():
                return {"total": len(_load_index()), "added": 0, "skipped": True}
        except (ValueError, TypeError):
            pass

    items = _load_index()
    have = {it.get("id") for it in items}

    sources: list[dict] = []
    sources += scan_local(cfg)
    pk = str(((cfg.get("photos") or {}).get("pexels_key")) or "").strip()
    if pk:
        sources += fetch_pexels(pk)
    else:
        sources += fetch_spotlight(pages)
        sources += fetch_bing()
    # 天文这一路单独抓：聚焦 / Bing 里星空比重太低
    if (cfg.get("photos") or {}).get("nasa", True):
        sources += fetch_astro(cfg)
        sources += fetch_nasa(cfg)

    added = 0
    pending: list[dict] = []
    for ph in sources:
        pid = _pid(ph)
        if pid in have:
            continue
        have.add(pid)
        pending.append(ph)

    # 并行下载，否则一百来张图要等十几分钟
    def dl(ph):
        return ph, _download(ph)

    with cf.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        for ph, file in ex.map(dl, pending):
            if not file:
                continue
            text = f"{ph.get('title','')} {ph.get('desc','')} {ph.get('credit','')}"
            parsed = attrs_of(text)
            items.append({
                "id": _pid(ph), "src": ph["src"], "file": file,
                "url": ph.get("url", ""), "link": ph.get("link", ""),
                "title": ph.get("title", ""), "desc": ph.get("desc", ""),
                "credit": ph.get("credit", ""),
                "tags": _flat_tags(text),
                "attrs": {k: v for k, v in parsed.items() if k != "flat"},
                "w": ph.get("w"), "h": ph.get("h"),
                "addedAt": _dt.datetime.now().isoformat(timespec="seconds"),
            })
            added += 1

    items = _prune(items)
    _save_index(items, started)
    # 每轮同步顺手扫一次孤儿：以前并发惹的锅，现在能自己长回来
    try:
        cleaned = cleanup_orphans(dry_run=False)
    except Exception as exc:
        store.log(f"orphan cleanup failed: {exc}")
        cleaned = {"orphans": 0, "bytes": 0}
    store.write_cache("photos-meta", {
        "lastSync": _dt.datetime.now().isoformat(timespec="seconds"),
        "total": len(items),
    })
    return {"total": len(items), "added": added,
            "cleaned": cleaned.get("orphans", 0),
            "freedMB": round((cleaned.get("bytes") or 0) / 1048576, 1)}


# -------------------------------------------------------------------- 选图
#
# 选图 = 一次「按条件算分」。条件是今天的场景：
#   天气（晴/云/雾/雪）× 时段（黎明/正午/黄昏/夜晚/蓝调）× 季节
#   × 事件（流星雨/银河/极光/满月/云海…）× 你的题材偏好
# 全部在 Python 里算完，一天（严格说是一个时段）只算一次并存进缓存，
# 前端拿到的就是「用哪张、为什么用这张」。

# 天气 phase -> 时段档位
_PHASE_SLOT = {
    "夜色": "夜", "夜间": "夜", "夜晚": "夜",
    "日出时分": "晨", "上午": "晨",
    "正午强光": "昼", "午后斜阳": "昼", "正午": "昼",
    "日落黄金时刻": "昏", "蓝调时刻": "昏", "黄昏": "昏",
}
_SLOT_NAME = {"夜": "夜晚", "晨": "黎明", "昼": "正午", "昏": "黄昏"}
_SLOT_CN = {"夜": "夜里", "晨": "清晨", "昼": "白天", "昏": "黄昏"}


# ------------------------------------------------- 分类：光照 × 天气
#
# 轮换要「按分类挑」，就必须先有可信的分类。以前拿 attrs 那套词表直接当分类用，
# 有两个毛病：① 覆盖率极低（320 张里只有 50 张带「时段」、60 张带「天气」），
# 硬按 (时段×天气) 分桶会得到 1–2 张的桶，轮换立刻退化成原地打转；
# ② 噪声大（"Meteor Shower" 里的 shower 被判成「雨」、「NIRCam」被判成正午）。
#
# 所以分类单独一套**严格词表**：只在证据明确时才贴标签，拿不准就留空。
# 光照有默认值（没有夜景/晨昏证据的风景照就是白天拍的），天气没有默认值。

_LIGHT_WORDS = (
    ("夜", ("银河", "星空", "星轨", "星云", "夜空", "极光", "月光", "月亮", "夜景", "深夜", "灯火",
            "galaxy", "milky way", "nebula", "aurora", "star trail", "stars", "starry",
            "night", "moonlight", "deep space", "constellation")),
    ("晨昏", ("日出", "日落", "黄昏", "黎明", "晨曦", "晚霞", "朝霞", "破晓", "暮色", "夕阳",
             "蓝调", "sunrise", "sunset", "dusk", "dawn", "twilight", "golden hour", "blue hour")),
)

_WX_WORDS = (
    ("雨", ("下雨", "雨天", "大雨", "阵雨", "暴雨", "雨后", "rain", "rainy", "rainfall", "drizzle")),
    ("雪", ("下雪", "雪景", "积雪", "飘雪", "初雪", "暴雪", "snow", "snowy", "snowfall")),
    ("雾", ("晨雾", "浓雾", "薄雾", "雾气", "大雾", "fog", "mist", "misty", "haze")),
    ("云", ("云海", "云层", "积云", "多云", "流云", "云雾", "cloudy", "overcast", "sea of clouds")),
    ("晴", ("晴朗", "晴空", "万里无云", "sunny", "clear sky", "blue sky")),
)

_EVENT_WORDS = {
    # 只认「流星雨」本身，**不要**把「猎户座 / 英仙座 / 双子座」单独当事件词：
    # 猎户座飞船（Artemis II 拍的地球照）、猎户座星云都会被误判成流星雨 ——
    # 实测事件分类里混进了一张 NASA 的飞船照。星座名只有跟在「流星雨」后面才算。
    "流星雨": ("流星雨", "流星", "meteor shower", "perseid", "geminid",
             "leonid", "orionid", "quadrantid"),
    "银河": ("银河", "银心", "星河", "milky way", "galactic center"),
    "极光": ("极光", "北极光", "南极光", "aurora", "northern lights"),
    "满月": ("满月", "圆月", "supermoon", "full moon"),
    "日食月食": ("日食", "月食", "eclipse", "红月"),
    "彩虹": ("彩虹", "日晕", "幻日", "rainbow"),
    "云海": ("云海", "sea of clouds"),
    "雾凇": ("雾凇", "树挂", "冰挂"),
}

_SLOT_LIGHT = {"夜": "夜", "晨": "晨昏", "昏": "晨昏", "昼": "昼"}
_ADJACENT_LIGHT = {"夜": ("晨昏", "昼"), "晨昏": ("夜", "昼"), "昼": ("晨昏", "夜")}
# 光照「近邻」——只用来给同类里的图排序打分，不是过滤条件
_LIGHT_NEAR = {"夜": ("晨昏",), "晨昏": ("夜", "昼"), "昼": ("晨昏",)}


def _light_rank(want: str, have: str) -> int:
    """光照契合度：同档 2 分 / 近邻 1 分 / 相反 0 分。拿不准一律 1 分。"""
    if not want or not have:
        return 1
    if want == have:
        return 2
    return 1 if have in _LIGHT_NEAR.get(want, ()) else 0

# ------------------------------------------------------------ 大分类（互斥）
#
# 小标签（流星雨 / 鲜花 / 雨天 / 雪山 …）太多太碎，直接拿来轮换等于没分类。
# 这里把每张图**归到且只归到一个**大分类，判定按优先级来，先命中先归：
#   天象（银河/流星雨/极光/月食/深空）> 天气（雨雪雾云/彩虹/雾凇）
#   > 景点（山野地形）> 建筑 > 植物 > 水景 > 其他
# ⚠ 曾有一个「人文」大类，但它始终进不了 MACRO_ORDER（全库只有一两张，凑不够
#   MIN_MACRO_AVAIL，单列一类等于永远选不出来，还会让「每张图都归到 MACRO_ORDER」
#   这条自测一直红）。所以「人文」并进「其他」，见 macro_of 末尾。
# 轮换在**大分类层面**按「最久没用」挑（不是固定顺序循环），每个大类内部再按
# 「光照 × 天气 × 最久没用」排序 —— 这样既不会连着几天都是天象，也不会在同一类里
# 那几张来回跳。详见 _next_macro 的注释。
MACRO_ORDER = ("天气", "景点", "建筑", "天象", "水景", "植物", "其他")
MIN_MACRO_AVAIL = 4      # 当前光照下少于这么多张的大类，这一轮跳过
_ASTRO_EVENTS = {"流星雨", "银河", "极光", "满月", "日食月食"}
_WEATHER_EVENTS = {"彩虹", "云海", "雾凇"}


def macro_of(item: dict, attrs: dict | None = None, st: dict | None = None) -> str:
    """这张图属于哪个**大分类**（互斥，必然返回一个）。"""
    a = attrs if isinstance(attrs, dict) else _item_attrs(item)
    t = st if isinstance(st, dict) else strict_tags(item, a)
    theme = set(a.get("题材") or [])
    ev = set(t.get("events") or set())
    if (ev & _ASTRO_EVENTS) or "天象" in theme or item.get("src") == "astro":
        return "天象"
    if t.get("weather") or (ev & _WEATHER_EVENTS):
        return "天气"
    for name, key in (("景点", "地形"), ("建筑", "建筑"),
                      ("植物", "植物"), ("水景", "水景")):
        if key in theme:
            return name
    # 「人文」不再单列：它凑不够 MIN_MACRO_AVAIL，单列一个大类等于永远轮不到它。
    # 归进「其他」，至少还能被正常选中（全库约 1 张）。
    return "其他"


def _last_macro(hist: list[dict], macro_by_file: dict[str, str]) -> str:
    """上一次用的是哪个大分类 —— 循环从这里往后走。"""
    for rec in reversed(hist):
        m = macro_by_file.get(str(rec.get("file") or ""))
        if m:
            return m
        cat = str(rec.get("cat") or "")
        for name in MACRO_ORDER:
            if cat.startswith(name):
                return name
    return ""


def _next_macro(items: list[dict], stag: dict, sc: dict, macro_last: dict,
                used: dict[str, str], iso: str) -> tuple[str, list[dict]]:
    """挑下一个大分类：**按「最久没用」驱动**，不是按固定顺序 +1。

    这里踩过一个很隐蔽的坑。原来游标是 `MACRO_ORDER.index(last) + 1` —— 每次
    固定往前走一格。但时段是规律的（夜/昼/昏循环），每个时段能选的大类又是
    固定的（夜里只有天象够格、晨昏只有建筑/景点），于是游标每轮只推进一格，
    下一时段又从同一格出发：

        夜 → 天象 → 游标到水景；昼 → 水景 → 游标到植物；昏 → 植物 → 游标回其他
        → 夜里其他不够格被跳过 → 又回到天象

    锁死成「天象→水景→植物」这条 3 步短轨，看起来就像「大类在轮，但永远是
    那几张」。现在改成看每个大类**上次是哪天用的**，最久没用的先上 —— 不管
    时段怎么重复，循环都会自己散开，不会卡进短轨。

    光照要不要收窄看**这个光照下全库的素材量**：夜 93 张、昼 180 张够，就按
    光照筛（夜里只出夜景/深空、白天不出夜空）；晨昏只有 14 张，收窄等于把选择
    压到十张以内，所以直接用整个大类，光照只当排序偏好。
    """
    light = str(sc.get("lightGroup") or _SLOT_LIGHT.get(str(sc.get("slot") or ""), "昼"))
    pools: dict[str, list[dict]] = {}
    for i in items:
        pools.setdefault(macro_of(i, None, stag[id(i)]), []).append(i)

    light_total = sum(1 for p in pools.values() for i in p if stag[id(i)]["light"] == light)
    narrow = light_total >= MIN_LIGHT_POOL

    cands: list[tuple[int, int, str, list[dict]]] = []
    for name in MACRO_ORDER:
        whole = pools.get(name) or []
        if len(whole) < MIN_MACRO_AVAIL:
            continue
        if narrow:
            pool = [i for i in whole if stag[id(i)]["light"] == light]
            if len(pool) < MIN_MACRO_AVAIL:
                continue
        else:
            pool = whole
        if all(str(used.get(str(i.get("file") or ""))) == iso for i in pool):
            continue                      # 这一类今天已经全用过了，这圈跳过
        d = str(macro_last.get(name) or "")
        days = _days_since(iso, d) if d else 9999
        cands.append((-days, MACRO_ORDER.index(name), name, pool))

    if cands:
        cands.sort(key=lambda x: (x[0], x[1]))
        return cands[0][2], cands[0][3]

    # 一个大类都不满足（图库太小）：退回「光照」大桶，再不行就用全库
    light_pool = [i for i in items if stag[id(i)]["light"] == light]
    if len(light_pool) >= MIN_BUCKET:
        return "光照", light_pool
    return "全库", list(items)


def _inner_bucket(macro: str, pool: list[dict], stag: dict, sc: dict) -> tuple[str, list[dict]]:
    """大类内部再按天气细分（够大才单开，不够就留在大类里）。"""
    light = str(sc.get("lightGroup") or "昼")
    wx = str(sc.get("wxGroup") or "")
    if wx:
        wxp = [i for i in pool if stag[id(i)]["weather"] == wx]
        if len(wxp) >= MIN_BUCKET:
            return f"{macro}·{light}·{wx}", wxp
    return f"{macro}·{light}", pool

# 不是「照片」的素材：图表、示意图、软件合成图、地图截取……
# 这些当壁纸很难看，还会在事件分类里挤掉真正的实拍（实测「英仙座流星雨」
# 那 8 张里有一半是 NASA 的示意图/光谱图）。选图时直接排除。
_NON_PHOTO_WORDS = (
    "graph", "chart", "diagram", "infographic", "plot", "spectrum", "spectra",
    "illustration", "schematic", "artist's concept", "artist concept", "rendering",
    "simulation", "software image", "annotated", "location of", "map of",
    "示意图", "图表", "曲线图", "分布图", "地图", "标注图", "合成图",
)

# 有人、在室内、在开会的照片：是照片，但当壁纸很难看，而且描述常常在讲
# 「这次任务/这次会议」，跟画面内容严重不符（那张「飞往流星雨观测地的机舱照」
# 就是这么被当成流星雨的）。壁纸不要它们。
_PEOPLE_WORDS = (
    "astronaut", "crew", "cabin", "on board the", "press conference", "interview",
    "portrait of", "briefing",
    "宇航员", "机舱", "座舱", "合影", "发布会", "座谈会", "研讨会现场",
)


def is_photo_like(item: dict) -> bool:
    """这张图值不值得当壁纸（排除图表/示意图/合成图，以及人物会议照）。"""
    blob = f"{item.get('title','')} {item.get('desc','')}".lower()
    return not any(w in blob for w in _NON_PHOTO_WORDS + _PEOPLE_WORDS)


def _wx_group(text: str) -> str:
    low = str(text or "").lower()
    for name, words in _WX_WORDS:
        if any(w.lower() in low for w in words):
            return name
    return ""


_TAG_MEMO: dict[str, tuple[str, dict]] = {}


def strict_tags(item: dict, attrs: dict | None = None) -> dict:
    """把一张图归到「光照 × 天气 × 事件」三个分类轴上（严格词表）。

    光照一定有值：夜 → 晨昏 → 昼（没证据的默认白天，这是风景照的常态）。
    天气只有证据时才给，拿不准留空 —— 空 = 天气中性，任何天气都能用。

    结果按 (文件名 + 文本指纹) 缓存：一次选图要对 290 多张各算一遍，
    不缓存的话每次换时段都要多花 300+ms。
    """
    blob = " ".join([
        str(item.get("title") or ""), str(item.get("desc") or ""),
        " ".join(str(t) for t in (item.get("tags") or [])),
    ]).lower()
    # 事件只认「标题 + 描述」：索引里的 tags 是老打标器推出来的（把「猎户座」
    # 也当成流星雨），拿它判事件会把一张猎户座飞船照塞进流星雨分类。
    text_only = " ".join([str(item.get("title") or ""),
                          str(item.get("desc") or "")]).lower()
    key = str(item.get("file") or id(item))
    sig = f"{len(blob)}|{hash(blob)}|{item.get('src')}"
    hit = _TAG_MEMO.get(key)
    if hit and hit[0] == sig:
        return hit[1]

    light = ""
    for name, words in _LIGHT_WORDS:
        if any(w.lower() in blob for w in words):
            light = name
            break
    a = attrs if isinstance(attrs, dict) else _item_attrs(item)
    if not light:
        # 深空 / 望远镜图没有「白天」可言：一律算夜
        if item.get("src") == "astro" or "天象" in (a.get("题材") or []):
            light = "夜"
        else:
            light = "昼"
    wx = _wx_group(blob)
    # 事件分「强/弱」两档：
    #   标题里写明「Perseid Meteor Shower」才是强证据；
    #   只在描述里提到「meteor shower」的算弱证据 —— NASA 那张机组在机舱里
    #   开会的照片，描述写的是「飞往流星雨观测地途中讨论任务」，图里根本没有流星。
    title_low = str(item.get("title") or "").lower()
    evs_title = {e for e, words in _EVENT_WORDS.items()
                 if any(w.lower() in title_low for w in words)}
    evs_any = {e for e, words in _EVENT_WORDS.items()
               if any(w.lower() in text_only for w in words)}
    out = {"light": light, "weather": wx, "events": evs_any, "events_title": evs_title}
    if len(_TAG_MEMO) > 4000:
        _TAG_MEMO.clear()
    _TAG_MEMO[key] = (sig, out)
    return out


def scene_of(cfg: dict, ctx: dict, wx: dict | None = None) -> dict:
    """今天（此刻）的取图条件。"""
    wx = wx or {}
    iso = str(ctx.get("iso") or _dt.date.today().isoformat())
    try:
        day = _dt.date.fromisoformat(iso)
    except ValueError:
        day = _dt.date.today()

    phase = str(wx.get("phase") or "")
    slot = _PHASE_SLOT.get(phase, "昼")
    night = slot == "夜"

    events = _attrs.astro_events(day, night=night)
    wx_attrs = attrs_of(f"{wx.get('text','')} {wx.get('phase','')}")
    likes = ((cfg.get("photos") or {}).get("likes")
             or list(_attrs.DEFAULT_LIKES))

    times = [_SLOT_NAME[slot]]
    if slot == "昏":
        times.append("蓝调")
    if night or slot == "晨":
        times.append("夜晚" if night else "清晨")

    return {
        "iso": iso,
        "slot": slot,
        "slotCn": _SLOT_CN[slot],
        "lightGroup": _SLOT_LIGHT.get(slot, "昼"),      # 分类轴①：光照
        "wxGroup": _wx_group(f"{wx.get('text','')} {wx.get('phase','')}"),   # 分类轴②：天气
        "时段": list(dict.fromkeys(times)),
        "天气": wx_attrs.get("天气") or [],
        "季节": [ctx.get("season") or ""],
        "事件": list(dict.fromkeys([t for e in events for t in e["tags"]])),
        "eventDetail": events,
        "偏好": list(likes),
        "festival": ctx.get("festival") or "",
        "weatherText": str(wx.get("text") or ""),
        "temp": wx.get("temp"),
    }


def _item_attrs(item: dict) -> dict:
    """条目的属性 = 索引里存的（本地推导 / 模型打的）+ 现场从标题描述推的。

    两边都要。只信存下来的会把标题里的「秋色」漏掉；只信现场推的，
    本地从 EXIF 补出来的「时段/季节」又白打了。
    """
    words = " ".join(str(t) for t in (item.get("tags") or []))
    out = attrs_of(f"{item.get('title','')} {item.get('desc','')} {words}")
    stored = item.get("attrs")
    if isinstance(stored, dict):
        for layer, cats in stored.items():
            if layer == "flat" or not isinstance(cats, list):
                continue
            lst = out.setdefault(layer, [])
            for c in cats:
                if c not in lst:
                    lst.append(c)
    return out


# ------------------------------------------------------- 轮换：别老是同一张

# 教训：这一版之前，`pick()` 是「取分数最高的那张」。听起来很合理，实际结果是
# **连续 4 天、昼/夜共 7 个时段，墙上始终是同一张图** —— 因为那张的属性恰好
# 每一场都命中（天象+极光+秋），基础分 9.4 甩开别人一大截。
# 原来其实写了个「跨天轮换」的哈希抖动，但幅度只有 0~0.8，纯属杯水车薪。
#
# 现在改成两步：
#   ① 按分数筛出一个「够好」的候选池（分数不低于冠军的 82%）
#   ② 在池子里按「日期 + 时段」做**确定性加权抽签** —— 好图更常被选中，
#      但每次都会换；同一天同一时段又是稳定的（刷新页面不会乱跳）
# 再加一条硬规则：REPEAT_DAYS 天内用过的图直接不进池子。

HISTORY_KEY = "wallpaper-history"
REPEAT_DAYS = 7        # 这么多天内不重复用同一张
POOL_RATIO = 0.82      # 候选池门槛：分数不低于这批里最高的这个比例
POOL_MIN = 12          # 池子至少留这么多张（分数两头分化时别缩成一两张）
POOL_MAX = 40          # 池子最多这么多张，别把明显不搭的也卷进来
_HISTORY_KEEP = 600    # 历史最多留这么多条（一天 3~4 条 → 约四五个月）


def _bootstrap_history() -> list[dict]:
    """老版本没有轮换历史 —— 从已有的 `wallpaper-<日期>-<时段>.json` 缓存里补建一份。

    不做这件事的话，升级后第一次挑图不知道「哪些最近刚用过」，很可能又挑到
    昨天那张（正是用户抱怨的现象）。
    """
    try:
        cdir = store.cache_path(HISTORY_KEY).parent
        out: list[dict] = []
        for p in sorted(cdir.glob("wallpaper-*.json"))[-40:]:
            stem = p.stem[len("wallpaper-"):]
            if "-" not in stem:
                continue
            iso, slot = stem.rsplit("-", 1)
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            main = ((data or {}).get("main") or {}) if isinstance(data, dict) else {}
            if main.get("file"):
                out.append({"d": iso, "slot": slot, "file": main["file"],
                            "title": str(main.get("title") or "")[:60],
                            "at": str((data or {}).get("computedAt") or "")})
        if out:
            store.write_cache(HISTORY_KEY, out[-_HISTORY_KEEP:])
            store.log(f"wallpaper-history: 从 {len(out)} 个旧壁纸缓存补建了轮换历史")
        return out[-_HISTORY_KEEP:]
    except Exception:
        return []


def _load_history() -> list[dict]:
    data = store.read_cache(HISTORY_KEY)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return _bootstrap_history()


def _last_used(history: list[dict]) -> dict[str, str]:
    """file -> 最近一次被用到的日期（ISO 字符串）。"""
    out: dict[str, str] = {}
    for rec in history:
        f = str(rec.get("file") or "")
        d = str(rec.get("d") or "")
        if f and d and d > out.get(f, ""):
            out[f] = d
    return out


def _days_since(iso: str, used: str) -> int:
    try:
        return (_dt.date.fromisoformat(iso) - _dt.date.fromisoformat(used)).days
    except (ValueError, TypeError):
        return 9999


def record_use(scene: dict, file: str, title: str = "", category: str = "",
               event: str = "") -> None:
    """记一笔「这天、这个时段用了这张、属于哪个分类」。同一天同一时段只留最后一次。

    `event` 非空表示这次是**事件插队**用的 —— 事件调度靠它算冷却与衰减。
    """
    if not file:
        return
    hist = _load_history()
    key = (str(scene.get("iso") or ""), str(scene.get("slot") or ""))
    hist = [r for r in hist if (str(r.get("d") or ""), str(r.get("slot") or "")) != key]
    hist.append({"d": key[0], "slot": key[1], "file": file,
                 "title": str(title)[:60], "cat": str(category)[:24],
                 "ev": str(event or "")[:20],
                 "at": _dt.datetime.now().isoformat(timespec="seconds")})
    store.write_cache(HISTORY_KEY, hist[-_HISTORY_KEEP:])


def _file_ok(name: str) -> bool:
    """这张图现在还在图库里吗（文件存在且非空）。

    壁纸缓存是「算一次存起来」的，图库却会同步/淘汰/改名 —— 缓存里
    指向一张已经不在磁盘上的图，前端就会显示一片黑。所有从缓存里
    取图的地方都必须先过这一关。
    """
    name = str(name or "")
    if not name:
        return False
    try:
        p = LIB_DIR / name
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def peek_chosen(cfg: dict, ctx: dict, wx: dict | None = None) -> dict | None:
    """只读缓存里「今天这个时段已经选好的那张」—— 给提示词用，不重算。"""
    try:
        sc = scene_of(cfg, ctx, wx)
        cached = store.read_cache(f"wallpaper-{sc['iso']}-{sc['slot']}")
        if isinstance(cached, dict) and cached.get("main"):
            main = dict(cached["main"])
            if not _file_ok(main.get("file")):
                return None
            main["why"] = cached.get("why") or main.get("why") or ""
            return main
    except Exception:
        pass
    return None


def _score(item: dict, scene: dict, cfg: dict,
           attrs: dict | None = None) -> tuple[float, dict]:
    """返回 (分数, 命中明细)。明细拿去给用户看「为什么选这张」。"""
    a = attrs if isinstance(attrs, dict) else _item_attrs(item)
    flat = set(item.get("tags") or [])
    s = 0.0
    hits: dict[str, list[str]] = {}

    def layer(layer_name: str, wants: list[str], weight: float) -> None:
        nonlocal s
        have = set(a.get(layer_name) or [])
        got = [w for w in wants if w and (w in have or w in flat)]
        if got:
            s += weight * len(got)
            hits[layer_name] = got

    layer("事件", scene.get("事件") or [], _attrs.LAYER_WEIGHT["事件"])
    layer("时段", scene.get("时段") or [], _attrs.LAYER_WEIGHT["时段"])
    layer("天气", scene.get("天气") or [], _attrs.LAYER_WEIGHT["天气"])
    layer("季节", scene.get("季节") or [], _attrs.LAYER_WEIGHT["季节"])

    # 题材偏好（天文/地形/植物/建筑…）
    likes = [x for x in (scene.get("偏好") or []) if x]
    fav = [c for c in likes if c in set(a.get("题材") or [])]
    if fav:
        s += _attrs.LIKE_BONUS * len(fav)
        hits["偏好"] = fav

    # 节日 / 节气带来的额外气味
    fest = _attrs.festival_attrs(scene.get("festival") or "", scene.get("slotCn") or "")
    if fest:
        got = [w for w in fest if w in flat or w in set(a.get("flat") or [])]
        if got:
            s += 0.9 * len(got)
            hits["节气"] = got

    if item.get("src") == "local":
        s += 6.0                     # 自己的照片永远最优先
    elif item.get("src") in ("astro", "apod", "nasa"):
        s += 1.0                     # 望远镜实拍 / NASA 官方图，用户点名喜欢天文摄影

    # 这里原来还有个「跨天轮换」的哈希抖动（0~0.8）。没用 —— 分差远大于 0.8，
    # 结果就是同一张图连着赢了 4 天。轮换交给 `pick()` 里的候选池抽签，
    # 打分只负责「这张贴不贴合当下的天气/时段/季节」。
    return s, hits


def _why_text(scene: dict, hits: dict) -> str:
    """把命中情况说成一句人话。"""
    parts = []
    for label, key in (("事件", "事件"), ("时段", "时段"), ("天气", "天气"),
                       ("季节", "季节"), ("题材", "偏好"), ("节气", "节气")):
        got = hits.get(key)
        if got:
            parts.append(f"{label} {'/'.join(got[:3])}")
    head = f"{scene.get('slotCn', '现在')}"
    if scene.get("weatherText"):
        head += f"·{scene['weatherText']}"
    if not parts:
        return f"{head}｜按分类轮换挑的"
    return f"{head}｜命中 " + "、".join(parts)


MIN_BUCKET = 6          # 正常分类少于这么多张就不算「分类」，往上一层退
MIN_LIGHT_POOL = 40     # 某个光照下全库素材少于此数时，不再按光照收窄（保护晨昏）
EVENT_MIN_BUCKET = 2    # 事件分类的门槛更低：图库里可能真的只有两张流星雨实拍，
                        # 而这两张正是「特定事件」该用的；宁可用它，也不要退回普通分类
EVENT_WEAK_MIN = 3      # 只有描述提到事件、标题没写的，得凑够这么多张才敢硬切
EVENT_HARD_MIN = 0.8    # 事件权重 ≥ 这个才「硬切」到事件分类里轮换
ROTATION_VERSION = 10   # 轮换算法版本：变了就让旧缓存重算（见 choose_daily）
# 8：why 文案从「95 张里轮换」改成「今天从 95 张里挑的」。
# 9：289 张全部补齐了精写介绍（2026-11-05 晚，157 条新写 + 132 条旧档合并），
#    「只用有介绍的图」这条终于能真正生效 —— 缓存里还是旧池子选出来的图，重算一遍。
# 10：why 从「夜里多云｜天象·夜，今天从 107 张里挑的」改成一句主谓宾
#     （字段用竖线硬串读着像日志）。缓存里存着旧句子，必须重算。
                        # 2 = 分类轮换；3 = 事件词表去掉星座名；
                        # 4 = 事件分强/弱证据；5 = 大分类互斥 + 大分类层循环（天气→景点→建筑→…）
                        # 6 = 事件加权插队（权重 × 稀缺度 × 衰减，带冷却与互斥）
                        # 7 = 大类层改「最久没用」驱动（不再固定 +1，防 3 步短轨）；
                        #     光照收窄改看全库素材量（MIN_LIGHT_POOL）；_rotate 加光照偏好

# ---------------------------------------------------------- 事件插队（加权）
#
# 事件不该「有就上」：流星雨一年八场、场场挂好几天，重要性却最低；满月一个月一次；
# 日食月食一年两三次但极重要。所以插队要算权重，而不是按峰值分排队。
#
# 权重 = 基础权重 × 稀缺度（天象大类多久没出）× 衰减（刚插过队就压低）× 峰值程度
# 再叠三道闸：
#   ① 冷却：同一事件插队后 N 天内不再插（流星雨 12 天 / 满月 25 天 / 极光 20 天 / 日食 90 天）；
#   ② 互斥：一天最多一次事件插队；同一晚多个事件只上一个（权重最高的赢，流星雨垫底）；
#   ③ 流星雨专闸：必须「没有别的事件候选」且「天象大类 ≥5 天没出现」才允许。
# 加权后低于 EVENT_JUMP_MIN 的一律不插队，正常走大分类循环。
EVENT_WEIGHT = {"日食月食": 1.0, "极光": 0.95, "满月": 0.90, "流星雨": 0.40}
EVENT_COOLDOWN = {"日食月食": 90, "极光": 20, "满月": 25, "流星雨": 12}
EVENT_JUMP_MIN = 0.5
METEOR_MIN_ASTRO_GAP = 5      # 流星雨插队前，天象大类至少这么多天没出现


def _event_pool(items: list[dict], stag: dict, sc: dict, e: dict) -> list[dict]:
    """这个事件能用的图：标题写明 > 描述沾边（后者要够多）。"""
    want = [t for t in (e.get("tags") or []) if t]
    if not want:
        return []
    strong = [i for i in items
              if any(t in (stag[id(i)].get("events_title") or set()) for t in want)]
    weak = [i for i in items if any(t in stag[id(i)]["events"] for t in want)]
    if strong:
        pool, need = strong, 1
    elif len(weak) >= EVENT_WEAK_MIN:
        pool, need = weak, EVENT_MIN_BUCKET
    else:
        return []
    if str(sc.get("lightGroup")) == "夜":
        # 夜里的事件不该挑到白天拍的图；但事件图本来就少，夜里不足 3 张时
        # 宁可放宽到全部事件图，也不要因为「凑不够」就放弃这个事件分类。
        night = [i for i in pool if stag[id(i)]["light"] == "夜"]
        pool = night if len(night) >= 3 else pool
    return pool if len(pool) >= need else []


def _astro_gap_days(hist: list[dict], macro_by_file: dict[str, str], iso: str) -> int:
    """天象大类上一次出现距今多少天（没出现过 = 999）。"""
    for rec in reversed(hist):
        m = macro_by_file.get(str(rec.get("file") or ""))
        if not m:
            cat = str(rec.get("cat") or "")
            m = next((n for n in MACRO_ORDER if cat.startswith(n)), "")
        if m == "天象":
            return _days_since(iso, str(rec.get("d") or ""))
    return 999


def _event_last_jump(hist: list[dict], name: str) -> str:
    for rec in reversed(hist):
        if str(rec.get("ev") or "") == name:
            return str(rec.get("d") or "")
    return ""


def _event_weight(e: dict, hist: list[dict], macro_by_file: dict[str, str],
                  iso: str) -> tuple[float, dict]:
    """事件的插队权重 = 基础权重 × 稀缺度 × 插队衰减 × 峰值程度。"""
    name = str(e.get("event") or "")
    base = EVENT_WEIGHT.get(name, 0.6)
    gap = _astro_gap_days(hist, macro_by_file, iso)
    gap_f = min(1.5, 0.7 + 0.15 * min(gap, 6))      # 天象越久没出，越该出
    last = _event_last_jump(hist, name)
    decay = min(1.0, 0.35 + 0.08 * _days_since(iso, last)) if last else 1.0
    peak = 1.0 if e.get("hard") else max(0.6, float(e.get("score") or 0))
    w = base * gap_f * decay * peak
    return w, {"base": base, "gap": min(gap, 999), "gapF": round(gap_f, 2),
               "decay": round(decay, 2), "peak": round(peak, 2)}


def _event_bucket(items: list[dict], stag: dict, sc: dict, hist: list[dict],
                  macro_by_file: dict[str, str], iso: str):
    """加权事件调度：谁插队、插不插，都由权重和闸门决定。

    三道闸（顺序即优先级）：
      ① 冷却：同一事件插队后 N 天内不再插；
      ② 互斥：一天最多一次事件插队；同一晚多个事件只上一个（权重最高的赢）；
      ③ 流星雨专闸：只有「没有别的事件候选」且「天象大类 ≥ METEOR_MIN_ASTRO_GAP 天没出现」
         才允许 —— 它一年八场、场场挂好几天，重要性却最低，不能天天霸屏。
    """
    events = [e for e in (sc.get("eventDetail") or [])
              if e.get("hard") and not e.get("seasonal")]
    if not events:
        return None
    if any(str(r.get("d") or "") == iso and r.get("ev") for r in hist):
        return None                                   # ② 一天只插一次

    cands: list[tuple[float, dict, list[dict], dict]] = []
    for e in events:
        name = str(e.get("event") or "")
        last = _event_last_jump(hist, name)
        if last and _days_since(iso, last) < int(EVENT_COOLDOWN.get(name, 14)):
            continue                                  # ① 冷却中
        pool = _event_pool(items, stag, sc, e)
        if not pool:
            continue
        w, detail = _event_weight(e, hist, macro_by_file, iso)
        cands.append((w, e, pool, detail))
    if not cands:
        return None

    others = [c for c in cands if str(c[1].get("event") or "") != "流星雨"]
    if others:
        cands = others                                 # ② 有别的就轮不到流星雨
    elif _astro_gap_days(hist, macro_by_file, iso) < METEOR_MIN_ASTRO_GAP:
        return None                                   # ③ 天象刚出过，流星雨不插队

    w, e, pool, detail = max(cands, key=lambda c: c[0])
    if w < EVENT_JUMP_MIN:
        return None
    label = str(e.get("title") or e.get("event") or "")
    return f"事件·{label}", pool, e, w, detail


def _rotate(pool: list[dict], iso: str, slot: str,
            used: dict[str, str], score_of: dict[str, float],
            stag: dict | None = None, wx: str = "",
            light: str = "") -> list[dict]:
    """分类内轮换：**先轮换、后贴合、再质量**。

    排序键：① 上次用到的日期越久远越靠前（没用过 = 9999，最先上）
            ② 同样「资历」里，光照对得上的先上（夜里先上夜景，白天先上日景）
            ③ 再同样，天气对得上的先上（下雨就先把雨景用掉）
            ④ 分数高的先上（冷门图排在后面，轮完一圈才轮到）
            ⑤ 再同分就用 (日期|时段|文件) 的哈希定序 —— 同一天同一时段刷新不乱跳，
               不同时段/不同天必然不同。
    这样只要分类里的图没轮完一圈，就不会重复；而光照/天气只在「同样久没用」的
    那一档里起作用 —— 不会因为夜景只有几张就让它们天天来回跳。
    """
    def key(it: dict):
        f = str(it.get("file") or "")
        d = used.get(f)
        days = _days_since(iso, d) if d else 9999
        lrank = 1
        if light and stag is not None:
            lrank = _light_rank(light, stag[id(it)]["light"])
        wxrank = 1
        if wx and stag is not None:
            w = stag[id(it)]["weather"]
            wxrank = 2 if w == wx else (1 if not w else 0)
        h = int(hashlib.md5(f"{iso}|{slot}|{f}".encode("utf-8")).hexdigest()[:8], 16)
        return (-days, -lrank, -wxrank, -score_of.get(f, 0.0), h)

    return sorted(pool, key=key)


# 池子里至少要有这么多「写过介绍」的图，才**考虑**启用「只用有介绍的图」
MIN_INTRO_POOL = 40


def _desc_intro(desc: str, limit: int = 132) -> str:
    """没有精写介绍时，用官方简介的开头顶上。

    Spotlight 每张图都配了一段中文背景（desc），是给图片站写的那种通稿，
    质量参差但都是人话 —— 比在首页挂「介绍正在生成」强。只取开头两三句：
    整段摊在壁纸上会变成流水账，而壁纸说明本来就该是一口气读完的长度。
    """
    t = re.sub(r"\s+", "", str(desc or "")).strip()
    if not t:
        return ""
    out: list[str] = []
    for seg in re.split(r"(?<=[。！？])", t):
        seg = seg.strip()
        if not seg:
            continue
        joined = "".join(out)
        if out and len(joined) + len(seg) > limit:
            break
        out.append(seg)
        if len(joined) + len(seg) >= 55:
            break
    return "".join(out)[:limit]


def _intro_pool_balanced(subset: list[dict], need: int = 4) -> bool:
    """「有介绍」的这批图，铺得开吗？

    这条检查是 2026-11-05 加的。之前只要凑够 40 张就收窄到「只轮有介绍的图」，
    可库里 320 张只有 117 张有精写介绍，其中 97 张是天象 —— 收窄之后白天只剩
    景点/建筑各 5 张，看起来就像「壁纸卡住了，天天那几张」。
    现在多问一句：覆盖够不够广、有没有被单一大类吃掉。
    """
    n: dict[str, int] = {}
    for i in subset:
        cat = str(i.get("category") or "")
        m = next((x for x in MACRO_ORDER if cat.startswith(x)), "其他")
        n[m] = n.get(m, 0) + 1
    if not n:
        return False
    wide = sum(1 for v in n.values() if v >= need)
    top = max(n.values()) / len(subset)
    return wide >= 4 and top <= 0.6


def pick(cfg: dict, ctx: dict, wx: dict, limit: int = 10,
         scene: dict | None = None, history: list[dict] | None = None) -> list[dict]:
    """挑图：先定分类，再在分类里轮换（不再按分数划线抽签）。

    旧实现是「全库按分数排序 → 划一条 82% 的线 → 在池子里抽签」。问题在于
    分数很挤、池子常被压到十几张，抽来抽去就那几张；而且属性覆盖率太低，
    「贴合的图」本身就少。现在分两层分类：
      1. **大分类层**（互斥）：每张图只归一个 —— 天象 / 天气 / 景点 / 建筑 /
         水景 / 植物 / 其他（「人文」并入其他）；按**最久没用**挑，不是按
         `MACRO_ORDER` 固定步进（那条路会把轮换锁进 3 步短轨，见 `_next_macro`）。
      2. **大类内部**：光照（夜/晨昏/昼）→ 天气（雨/雪/雾/云/晴）细分，
         再按「最久没用」轮换，一圈没轮完不重复。
    硬事件（流星雨极大、满月、月食）优先于大类循环，直接切到更小的事件分类。

    返回的第 1 条是主壁纸，其余是「按 N 换」用的备选（同一分类里的下一批）。
    """
    items = [i for i in _load_index()
             if (LIB_DIR / i.get("file", "")).exists() and is_photo_like(i)]
    if not items:
        return []
    # 首页会把这张图的介绍整段摊开，所以优先只在「有正式介绍」的图里轮换。
    # 同步进来的新图要等介绍写好才进池子；库里如果几乎都没写（< 40 张），
    # 就不能把池子掏空 —— 宁可显示本地兜底文案，也别让壁纸没得选。
    described = [i for i in items if (i.get("intro") or "").strip()]
    if len(described) >= MIN_INTRO_POOL and _intro_pool_balanced(described):
        items = described
    else:
        # 不收窄：没精写介绍的图照样能上，首页文案退到官方简介的开头
        # （见 out 里的 intro 兜底）。宁可文案短一截，也不要壁纸天天同一批。
        pass
    sc = scene or scene_of(cfg, ctx, wx)
    iso = str(sc.get("iso") or "")
    slot = str(sc.get("slot") or "")

    # 属性/分类只算一次：几百条重复算会明显变慢
    attrs_map = {id(i): _item_attrs(i) for i in items}
    stag = {id(i): strict_tags(i, attrs_map[id(i)]) for i in items}
    score_map: dict[str, float] = {}
    hits_map: dict[str, dict] = {}
    for i in items:
        s, hits = _score(i, sc, cfg, attrs_map[id(i)])
        score_map[str(i.get("file") or "")] = s
        hits_map[str(i.get("file") or "")] = hits

    hist = history if history is not None else _load_history()
    used = _last_used(hist)
    macro_by_file = {str(i.get("file") or ""): macro_of(i, attrs_map[id(i)], stag[id(i)])
                     for i in items}

    ev = _event_bucket(items, stag, sc, hist, macro_by_file, iso)
    if ev:
        macro = "天象"
        name, pool, event, ev_weight, ev_detail = ev
    else:
        event, ev_weight, ev_detail = None, 0.0, {}
        # 每个大类上次是哪天用的 —— 大类层靠这个驱动（不能用固定顺序 +1，
        # 那样会被规律的时段锁进短轨，见 _next_macro 的注释）
        mlast: dict[str, str] = {}
        for rec in hist:
            f = str(rec.get("file") or "")
            m = macro_by_file.get(f) or str(rec.get("macro") or "")
            if not m:
                cat = str(rec.get("cat") or "")
                m = next((n for n in MACRO_ORDER if cat.startswith(n)), "")
            d = str(rec.get("d") or "")
            if m and d and d > mlast.get(m, ""):
                mlast[m] = d
        # 大分类层面轮换：最久没用的先上（顺序只用于打平手）
        macro, mpool = _next_macro(items, stag, sc, mlast, used, iso)
        name, pool = _inner_bucket(macro, mpool, stag, sc)

    ordered = _rotate(pool, iso, slot, used, score_map, stag,
                      str(sc.get("wxGroup") or ""),
                      light=str(sc.get("lightGroup") or _SLOT_LIGHT.get(slot, "昼")))

    out = []
    wx_label = str(sc.get("weatherText") or "")[:6]
    for i in ordered[:limit]:
        f = str(i.get("file") or "")
        hits = dict(hits_map.get(f) or {})
        if event:
            hits["事件"] = [event.get("event")] + list(hits.get("事件") or [])
        hits["分类"] = [name]
        if name.startswith("事件·"):
            # 事件插队值得说清楚「为什么今天破例」，但权重是内部数值，别端上桌。
            why = (f"{sc.get('slotCn', '现在')}｜{event.get('title') or ''}"
                   f"（{event.get('note') or ''}）到了，今天先插一张")
        else:
            # 工作台顶上这句是给用户看的，不是日志：
            # 「夜里多云｜天象·夜，今天从 107 张里挑的」把三个内部字段
            # 用竖线硬串起来，能读但不像人话。改成一句主谓宾。
            why = (f"{sc.get('slotCn', '现在')}{wx_label}"
                   f"，今天挑的是{macro if macro else name}")
        out.append({
            "file": i["file"], "src": i["src"], "title": i.get("title", ""),
            "desc": (i.get("desc") or "")[:220], "credit": i.get("credit", ""),
            "url": f"/art/library/{i['file']}",
            "link": i.get("link") or i.get("url") or "",
            "tags": (i.get("tags") or [])[:10],
            "attrs": {k: v for k, v in attrs_map[id(i)].items() if k != "flat"},
            "category": name,
            "macro": macro,
            "event": (event or {}).get("event") or "",
            "eventWeight": round(ev_weight, 2),
            "eventDetail": ev_detail,
            # 与图绑定的介绍（photos.intro_batch 生成、存索引）；
            # 没精写过就退到官方简介的开头 —— 首页那块字宁可短，不能空。
            "intro": ((i.get("intro") or "").strip()
                      or _desc_intro(i.get("desc") or ""))[:400],
            "introBy": (i.get("introBy") or "") if (i.get("intro") or "").strip()
                       else "desc",
            "score": round(score_map.get(f, 0.0), 2),
            "hits": hits,
            "why": why,
            "pool": len(pool),
            "rotation": [str(x.get("file") or "") for x in ordered[:12]],
        })
    return out


def choose_daily(cfg: dict, ctx: dict, wx: dict | None = None,
                 force: bool = False) -> dict:
    """今天这个时段用哪张壁纸 —— 算一次、存起来，前端只管显示。

    缓存键带上时段（夜/晨/昼/昏），所以从白天走到黄昏会自动换一张，
    但同一时段内不会乱跳。
    """
    scene = scene_of(cfg, ctx, wx)
    key = f"wallpaper-{scene['iso']}-{scene['slot']}"
    if not force:
        cached = store.read_cache(key)
        if isinstance(cached, dict) and cached.get("main"):
            if cached.get("algo") != ROTATION_VERSION:
                # 老算法算出来的缓存：分类口径都变了，直接重算一次，
                # 否则用户要等到下一次换时段才看得到新逻辑的效果。
                store.log(f"choose_daily: {key} 是旧轮换算法算的，重算")
            else:
                cached["alts"] = [a for a in (cached.get("alts") or [])
                                  if isinstance(a, dict) and _file_ok(a.get("file"))]
                main = cached.get("main") or {}
                if _file_ok(main.get("file")):
                    cached["cached"] = True
                    return cached
                # 缓存里的图已经被同步/淘汰掉了：别再拿一张不存在的图糊在墙上。
                store.log(f"choose_daily: 缓存里的 {main.get('file')} 已不在图库，重算 {key}")

    cands = pick(cfg, ctx, wx or {}, limit=12, scene=scene, history=_load_history())
    main = cands[0] if cands else None
    payload = {
        "iso": scene["iso"],
        "slot": scene["slot"],
        "algo": ROTATION_VERSION,
        "scene": scene,
        "category": (main or {}).get("category") or "",
        "why": main["why"] if main else "配图库是空的",
        "main": main,
        "alts": cands[1:],
        "picked": len(cands),
        "computedAt": _dt.datetime.now().isoformat(timespec="seconds"),
        "cached": False,
    }
    if main:
        store.write_cache(key, payload)
        # 记一笔，下次（换时段 / 明天）就不会再挑到这张
        record_use(scene, str(main.get("file") or ""), str(main.get("title") or ""),
                   str(main.get("category") or ""), str(main.get("event") or ""))
    return payload


def stats() -> dict:
    items = _load_index()
    by: dict[str, int] = {}
    by_cat: Counter[str] = Counter()
    by_tagged: Counter[str] = Counter()
    for i in items:
        if not isinstance(i, dict):
            continue
        by[i.get("src", "?")] = by.get(i.get("src", "?"), 0) + 1
        by_cat[str(i.get("category") or category_of(i))] += 1
        if i.get("attrsBy"):
            by_tagged[str(i["attrsBy"])] += 1
    files = 0
    total_bytes = 0
    ref = {i.get("file") for i in items if i.get("file")}
    orphans = 0
    try:
        for p in LIB_DIR.iterdir():
            if not p.is_file():
                continue
            files += 1
            total_bytes += p.stat().st_size
            if p.name not in ref:
                orphans += 1
    except OSError:
        pass
    intro = intro_progress(items)
    return {"total": len(items), "bySource": by,
            "byCategory": dict(by_cat), "byTagged": dict(by_tagged),
            "tagged": sum(by_tagged.values()),
            "introDone": intro.get("done", 0),
            "introLocal": intro.get("local", 0),
            "introNone": intro.get("none", 0),
            "visionTagged": intro.get("visionTagged", 0),
            "files": files,
            "mb": round(total_bytes / 1048576, 1),
            "orphans": orphans,
            "dir": str(LIB_DIR)}


# ======================================================== 本地推导打标（免费）

# 图片自身的拍摄信息（EXIF）+ 文件名 + 来源，就能推出不少属性 ——
# 不联网、不花钱。剩下真推不出来的（画面里到底是山是海是花）才需要模型，
# 由 `photos.vision_tagging` 那个开关控制，默认关。
#
# 各信号能覆盖的属性层：
#   来源 src        -> 题材（天文图源必是天象，这是事实不是猜）
#   EXIF 拍摄时间    -> 时段 + 季节
#   EXIF 等效焦距    -> 构图（广角 / 长焦）
#   EXIF 曝光时间    -> 事件（长曝）
#   EXIF 方向        -> 备注（竖幅）
#   文件名里的时间    -> 时段 + 季节（EXIF 被聊天软件洗掉时的兜底）

# 文件名里的时间戳：PXL_20260912_183045 / IMG_20260912_1830 / 2026-09-12 18.30.45
_NAME_STAMP = re.compile(
    r"(?:19|20)(\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})"
    r"(?:[ _\-T]+(\d{2})[-_.:]?(\d{2})(?:[-_.:]?(\d{2}))?)?"
)

# 时段边界按国内日出日落大致规律划
_HOUR_SLOTS = ((5, 7, "黎明"), (7, 11, "清晨"), (11, 16, "正午"),
               (16, 19, "黄昏"), (19, 21, "蓝调"))
_SEASON_BY_MONTH = ["冬", "冬", "春", "春", "春", "夏", "夏", "夏", "秋", "秋", "秋", "冬"]

_SRC_CATEGORY = {"astro": "天象", "apod": "天象", "nasa": "天象",
                 "local": "本人作品", "pexels": "主题搜索",
                 "spotlight": "聚焦精选", "bing": "每日壁纸"}
# 分类优先级：题材是画面内容，最适合当分类；没有就用来源兜底
_CATEGORY_ORDER = ["天象", "地形", "植物", "建筑", "水景", "生命", "人文", "天气"]

_IFD_TYPES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _slot_of_hour(h: int) -> str:
    for lo, hi, name in _HOUR_SLOTS:
        if lo <= h < hi:
            return name
    return "夜晚"


def _season_of_month(m: int) -> str:
    return _SEASON_BY_MONTH[max(1, min(12, m)) - 1]


def _stamp_from_name(name: str) -> tuple[str, str]:
    """从文件名抠拍摄信息，返回 (时段, 季节)。抠不到就是空串。"""
    for m in _NAME_STAMP.finditer(name or ""):
        mo, day, hh = int(m.group(2)), int(m.group(3)), m.group(4)
        if not (1 <= mo <= 12) or not (1 <= day <= 31):
            continue
        if hh is not None and 0 <= int(hh) <= 23:
            return _slot_of_hour(int(hh)), _season_of_month(mo)
        return "", _season_of_month(mo)
    return "", ""


def _exif_head(path: Path, n: int = 262144) -> bytes:
    """读文件开头一段就够 —— EXIF 的 APP1 段在最前面。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def _exif_facts(data: bytes) -> dict:
    """读 EXIF 里我们要的那几个字段。

    手写解析（纯标准库）—— 项目是零依赖的，不想为了读几个元数据引入 Pillow。
    任何异常都当「这张图没有 EXIF」处理，绝不影响上层。
    """
    try:
        i = data.find(b"Exif\x00\x00")
        if i < 0:
            return {}
        tiff = i + 6
        endian = {b"II": "<", b"MM": ">"}.get(data[tiff:tiff + 2])
        if not endian or tiff + 8 > len(data):
            return {}
        u16 = lambda o: struct.unpack_from(endian + "H", data, o)[0]   # noqa: E731
        u32 = lambda o: struct.unpack_from(endian + "I", data, o)[0]   # noqa: E731

        def entries(off: int) -> dict:
            out: dict[int, tuple] = {}
            if off <= 0 or off + 2 > len(data):
                return out
            for k in range(min(u16(off), 512)):
                e = off + 2 + k * 12
                if e + 12 > len(data):
                    break
                tag, typ, cnt = u16(e), u16(e + 2), u32(e + 4)
                size = _IFD_TYPES.get(typ, 0) * cnt
                out[tag] = (typ, cnt, (e + 8) if size <= 4 else (tiff + u32(e + 8)))
            return out

        def value(ent) -> object:
            typ, cnt, off = ent
            if off <= 0 or off >= len(data):
                return None
            if typ == 2:
                return data[off:off + cnt].split(b"\x00")[0].decode("ascii", "ignore")
            if typ == 3:
                return u16(off)
            if typ == 4:
                return u32(off)
            if typ in (5, 10) and off + 8 <= len(data):
                den = u32(off + 4)
                return (u32(off) / den) if den else None
            return None

        ifd0 = entries(tiff + u32(tiff + 4))
        sub = entries(tiff + u32(ifd0[0x8769][2])) if 0x8769 in ifd0 else {}

        def pick(tag: int, *srcs) -> object:
            for src in srcs:
                if tag in src:
                    v = value(src[tag])
                    if v not in (None, ""):
                        return v
            return None

        return {
            # 两个 IFD 都找：正规相机把拍摄参数放在 Exif 子 IFD，
            # 但不少导出工具（含 Pillow）会写进 IFD0，两边都要认。
            "datetime": pick(0x9003, sub, ifd0),     # DateTimeOriginal
            "focal35": pick(0xA405, sub, ifd0),      # 等效 35mm 焦距
            "focal": pick(0x920A, sub, ifd0),        # 实际焦距
            "exposure": pick(0x829A, sub, ifd0),     # 曝光时间（秒）
            "orientation": pick(0x0112, ifd0, sub),
        }
    except Exception:
        return {}


def _merge_layer(a: dict, layer: str, cat: str) -> None:
    lst = a.setdefault(layer, [])
    if cat not in lst:
        lst.append(cat)


def _facts_attrs(facts: dict, name: str) -> tuple[dict, list[str]]:
    """把 EXIF / 文件名里的拍摄信息翻成属性，并给出「依据」说明。"""
    out: dict[str, list[str]] = {}
    notes: list[str] = []

    dt = str(facts.get("datetime") or "")
    m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2})", dt)
    if m:
        mo, hh = int(m.group(2)), int(m.group(4))
        notes.append("EXIF 拍摄时间")
        _merge_layer(out, "季节", _season_of_month(mo))
        _merge_layer(out, "时段", _slot_of_hour(hh))
    else:
        slot, season = _stamp_from_name(name)
        if season:
            _merge_layer(out, "季节", season)
        if slot:
            _merge_layer(out, "时段", slot)
        if season or slot:
            notes.append("文件名里的时间")

    f35 = facts.get("focal35") or facts.get("focal")
    if isinstance(f35, (int, float)) and f35 > 0:
        if f35 < 24:
            _merge_layer(out, "构图", "广角")
            notes.append(f"等效{f35:.0f}mm 广角")
        elif f35 > 85:
            _merge_layer(out, "构图", "特写")
            notes.append(f"等效{f35:.0f}mm 长焦")

    exp = facts.get("exposure")
    if isinstance(exp, (int, float)) and exp >= 2.0:
        _merge_layer(out, "事件", "长曝")
        notes.append(f"曝光 {exp:g}s")

    o = facts.get("orientation")
    if isinstance(o, int) and 5 <= o <= 8:
        notes.append("竖幅")
    return out, notes


def category_of(item: dict) -> str:
    """条目归到哪一类：优先题材（画面内容），否则按来源兜底。"""
    a = item.get("attrs")
    if isinstance(a, dict):
        for c in _CATEGORY_ORDER:
            if c in (a.get("题材") or []):
                return c
        for layer in ("事件", "季节", "时段"):
            if a.get(layer):
                return str(a[layer][0])
    return _SRC_CATEGORY.get(str(item.get("src") or ""), "其他")


def tag_local(cfg: dict | None = None) -> dict:
    """给图库里的条目补属性 —— 只用图片自身的信息，不联网、不花钱。

    来源（确定性事实）+ EXIF（拍摄时间 / 焦段 / 曝光 / 方向）
    + 文件名里的时间戳 + 原有的标题描述关键词匹配。

    只处理「还没本地打过标」的条目，所以反复调用很廉价；打完写回索引，
    之后 `_score()` 就能用上这些属性（打分逻辑一行都不用改）。
    """
    if cfg is not None and not ((cfg.get("photos") or {}).get("tag_local", True)):
        return {"skipped": "disabled", "total": len(_load_index()), "tagged": 0}

    started = _dt.datetime.now().isoformat(timespec="seconds")
    items = _load_index()
    tagged = 0
    why: Counter[str] = Counter()
    for it in items:
        if not isinstance(it, dict) or it.get("attrsBy") == "local":
            continue
        src = str(it.get("src") or "")
        text = f"{it.get('title','')} {it.get('desc','')} {it.get('credit','')}"
        a = attrs_of(text)
        a.pop("flat", None)
        prov: list[str] = []

        # 1) 来源是确定性推导，不猜
        if src in ("astro", "apod", "nasa"):
            _merge_layer(a, "题材", "天象")
            prov.append("来源=天文图源")
        elif src == "local":
            prov.append("来源=本人作品")

        # 2) 图片自身：本人的照片优先读原图（拿 EXIF），否则退回库内副本 / 文件名
        name = str(it.get("file") or "")
        facts: dict = {}
        if src == "local":
            raw = str(it.get("path") or "")
            cand = Path(raw) if raw and Path(raw).is_file() else (LIB_DIR / name)
            if cand.is_file():
                facts = _exif_facts(_exif_head(cand))
            name = Path(raw).name if raw else name
        fa, fnotes = _facts_attrs(facts, name)
        for layer, cats in fa.items():
            for c in cats:
                _merge_layer(a, layer, c)
        prov.extend(fnotes)

        it["attrs"] = a
        it["attrsBy"] = "local"
        it["taggedAt"] = started
        it["tagNote"] = " · ".join(dict.fromkeys(prov))[:120]
        it["category"] = category_of(it)
        # tags 是给界面和「节气」匹配用的扁平词表：原有命中词 + 推导出的类别
        flat = _flat_tags(text)
        for _layer, cats in a.items():
            for c in cats:
                if c not in flat:
                    flat.append(c)
        it["tags"] = flat
        tagged += 1
        for p in prov:
            why[p] += 1

    if tagged:
        # `started` 之前的条目以我这份为准，之后的（并发同步新入库的）并回来
        _save_index(items, newer_than=started)
        store.log(f"tag_local: {tagged} 张，依据 {dict(why)}")
    return {"total": len(items), "tagged": tagged, "byNote": dict(why)}


def library_page(cfg: dict | None = None, category: str = "", src: str = "",
                 offset: int = 0, limit: int = 60) -> dict:
    """图库浏览：按分类 / 来源筛选后分页（只读）。"""
    everything = [i for i in _load_index() if isinstance(i, dict)]
    items = everything
    if category:
        items = [i for i in items if str(i.get("category") or category_of(i)) == category]
    if src:
        items = [i for i in items if str(i.get("src") or "") == src]
    items = sorted(items, key=lambda i: str(i.get("addedAt") or ""), reverse=True)
    by_cat: Counter[str] = Counter()
    by_src: Counter[str] = Counter()
    for i in everything:
        by_cat[str(i.get("category") or category_of(i))] += 1
        by_src[str(i.get("src") or "?")] += 1
    offset = max(0, int(offset))
    limit = max(1, min(200, int(limit or 60)))
    page = [{
        "file": i.get("file"), "src": i.get("src"),
        "category": str(i.get("category") or category_of(i)),
        "title": i.get("title", ""), "credit": i.get("credit", ""),
        "url": f"/art/library/{i.get('file')}",
        "attrs": {k: v for k, v in (i.get("attrs") or {}).items()},
        "tagNote": i.get("tagNote", ""), "addedAt": i.get("addedAt", ""),
        "intro": (i.get("intro") or "")[:160],
    } for i in items[offset:offset + limit]]
    return {"ok": True, "total": len(items), "offset": offset, "limit": limit,
            "byCategory": dict(by_cat), "bySource": dict(by_src), "items": page}


# ================================================== 视觉识图 + 每图一介
#
# 「每天一句话」这套按天重新写文案的做法已退役。替代它的机制：
#   1. 每张图生成一段介绍（intro 字段），**生成一次永久入索引，跟图绑定**；
#   2. 显示时直接取「当前这张图的介绍」——图按时段换了，文字立刻跟着换，
#      永远不会出现「文字讲冰岛、墙上星系」；
#   3. 打标从「照文字推」升级为「看图识别」：配了多模态模型（ai.vision_model）
#      后，识图与写介绍在**同一次调用**里完成，属性直接进选图打分（`pick/_score`）。
#
# 优先级：视觉调用（看图，标签+介绍）→ 文字调用（只有文字模型时，介绍）→
# 本地兜底介绍（没模型也即时有内容、跟图绑定；之后有模型会自动升级）。

_INTRO_LOCK = threading.Lock()

VISION_SYSTEM = (
    "你是一个中文照片编目员。只输出 JSON，不要解释、不要 markdown 代码块。"
    "介绍只有一条标准：让一个完全不懂的人看完这段文字，知道这张照片拍的是什么、"
    "这东西的来龙去脉。所有内容必须围绕照片本身 —— 画面里有什么、场景是什么、"
    "主体是什么、有哪些一眼能看见的关键信息，然后用科普的方式解释它是什么、"
    "为什么会这样、有什么背景或意义。"
    "严禁个性化（不写「今天适合看它」「与你有关」），严禁主观评价和形容词堆砌，"
    "严禁只谈风格构图色彩而不说内容，严禁罗列打标词，严禁复述标题。"
)

# 介绍做到位的标记：introBy 为 vision / text 才算「模型正式写的」
_INTRO_DONE_BY = ("vision", "text")
# 介绍规格版本：写介绍的要求改过一次（2 = 「客观画面 + 科普解释」两段式，
# 严禁个性化/主观/只谈样式）。本地介绍低于这个版本就在读取时免费重写，
# 模型版不重写（重写要花钱，且旧版也已满足「围绕图片本身」）。
_INTRO_SPEC = 4
# 4 = 去掉「来自 © 某人 / 某图库 的照片，《标题》」这个开头（二十多个字只说署名，
#     对「这张图是什么」零贡献），篇幅让给事实句；图说上限 160→190。
_INTRO_TRY_MAX = 3      # 单张图最多试这么多次，别让一张死图卡住整批
_SAVE_EVERY = 8         # 每写好这么多个就落一次盘（中断也不丢进度）
_FAIL_STREAK_STOP = 5   # 连续失败这么多就整轮放弃（多半是 Key/网络问题）


def sanitize_tags(raw: object) -> dict:
    """把模型吐回来的标签清洗成词表里的合法属性。

    只认 attrs.ATTRS 里登记过的「层名 → 类别名」，自造词一律丢弃 ——
    否则「深蓝的海」这种自由发挥会进不了打分计算（_score 只查词表类别）。
    """
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for layer, cats in raw.items():
        vocab = _attrs.ATTRS.get(str(layer))
        if not vocab or not isinstance(cats, list):
            continue
        for c in cats:
            if str(c).strip() in vocab:
                _merge_layer(out, str(layer), str(c).strip())
    return out


def _vision_intro_prompt(item: dict) -> str:
    """看图打标 + 写介绍：一次调用完成，标签直接进选图计算。"""
    meta = []
    if str(item.get("title") or "").strip():
        meta.append(f"标题：{str(item['title'])[:120]}")
    if str(item.get("credit") or "").strip():
        meta.append(f"来源：{str(item['credit'])[:80]}")
    desc = re.sub(r"\s+", " ", str(item.get("desc") or "")).strip()
    if desc:
        meta.append(f"原图说明：{desc[:220]}")
    lines = "\n".join(meta)
    vocab = "；".join(f"{layer}({'/'.join(cats)})" for layer, cats in _attrs.ATTRS.items())
    return f"""这张照片的已知信息：
{lines or "（无）"}

请**看图**回答两件事，只输出 JSON，不要解释：

1. tags：画面属性打标。每一层只能从括号里的类别名里选 0-2 个，**必须原样使用这些词，不要自己造**；拿不准的层给空数组，全不确定就全部给空数组：
{vocab}

2. intro：100-160 字、三到四句的中文介绍，分两层写，两层都要：

   第一层 · 客观画面：说清画面上**实际看得见**的东西 —— 主体是什么、处在什么
   场景里、光线与明暗关系、最显眼的标志性细节（形状、结构、数量、层次）。
   只写看得见的信息，不做评价。

   第二层 · 科普解释：用一两句讲清「这是什么、为什么会这样、有什么背景或意义」
   —— 天体就说类型与形成，地貌就说成因与演化，天气现象就说得名与机理，
   人造物就说用途与历史。可以有一个准确的类比。

   红线：不要出现「震撼」「绝美」「治愈」「值得关注」这类词；不要罗列打标词
   （不要写「画面是星空、夜晚、天象」这种属性清单）；不要复述标题；
   不要提「今天」「你」「适合发朋友圈」；不得编造地名、年代、物种、摄影师，
   拍摄设备与机构一律照「来源」写。

{{"tags": {{"题材": [], "时段": [], "天气": [], "季节": [], "事件": [], "构图": [], "色彩": []}}, "intro": "…"}}"""


def _text_intro_prompt(item: dict) -> str:
    """没有视觉模型时：只凭元数据写介绍（文字调用）。"""
    a = _item_attrs(item)
    tags = []
    for layer in ("事件", "题材", "天气", "时段", "季节", "构图"):
        vals = [str(x) for x in (a.get(layer) or []) if x][:2]
        if vals:
            tags.append(f"{layer} {'/'.join(vals)}")
    meta = [f"标题：{str(item.get('title') or '（无标题）')[:120]}",
            f"来源：{str(item.get('credit') or item.get('src') or '未知')[:80]}"]
    if tags:
        meta.append("属性：" + "；".join(tags))
    desc = re.sub(r"\s+", " ", str(item.get("desc") or "")).strip()
    if desc:
        # 300 字给足：素材越具体，第二段「机理」才写得实。220 字时常常刚好
        # 截在事实句前面，模型只能拿标题硬凑。
        meta.append(f"原图说明：{desc[:300]}")
    return f"""这张照片的信息：
{chr(10).join(meta)}

请凭这些信息写一段两段式的中文介绍，只输出 JSON：
{{"intro": "…"}}

按下面两条写，总共 140-180 字（硬要求）：

1. **画面**（60-80 字）：只说「原图说明」里**写明**的东西。
   说明里没提到的光线、颜色、天气、人数、动作、表情，**一个字都不许补** ——
   没看图就别假装看见，宁可写得朴素。这一段的职责是「说清拍的是哪儿、有什么」。
2. **科普**（60-80 字）：解释这类东西的来历或机理。城市讲地理与规划，地貌讲地质成因，
   天体讲类型与演化，动物讲习性，植物讲生长条件，天气现象讲形成机制。
   要讲「为什么」，不要讲「多美」；不要把第一段的事实换个说法再说一遍。

格式（必须照做，这是最常见的失败）：
- **两段之间空一行**，用 `\n\n` 分隔，写进 JSON 的 intro 字段里；
- 只写一段、或两段粘成一坨，都算不合格；
- 每段都要够 60 字，别写成两句就收尾。

红线：
- 不要以「照片中」「画面里」「这张图」开头，直接进内容；
- 不写「震撼 / 绝美 / 治愈 / 仙境 / 叹为观止 / 值得一去」这类词；
- 不罗列打标词（不要写「这是一张天象、夜晚、秋季的照片」）；
- 不得编造地名、年代、物种、数字、摄影师、设备与机构；
- 不要提「今天」「你」；不要复述标题；不要 markdown 或「第一段」这类标记。"""


def _clip_text(text: str, limit: int = 150) -> str:
    """按句子边界截断，别把一句话切一半（旧兜底就常出现「因外形酷。」这种断头）。"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    marks = list(re.finditer(r"[。！？!?；;]", cut))
    if marks:
        return cut[:marks[-1].end()]
    return cut.rstrip("，,、；; ") + "…"


def _looks_like_id(title: str) -> bool:
    """标题只是个档案编号（ARC-2007-ACD07-0152-035）时，别把它当作品名念出来。

    这种标题对读者没有任何信息量，写成《ARC-2007-…》只会让文案更像机器写的。
    """
    t = str(title or "").strip()
    if len(t) < 8 or re.search(r"[\u4e00-\u9fff]", t):
        return False
    if len(t.split()) > 2:
        return False
    digits = sum(c.isdigit() for c in t)
    letters = sum(c.isalpha() for c in t)
    return digits >= 3 and letters >= 3


# 英文图说说不出所以然时，用「已经能确认的属性」写一句客观画面描述。
# 这些句子只陈述事实（画面里有流星/夜空/山野），不编造地点年代人物。
_EVENT_SENTENCE = {
    "流星雨": "画面是夜空中的流星，长曝光把划过的轨迹留了下来。",
    "银河": "画面是银河横过夜空的景象。",
    "极光": "画面是极光，出现在高纬度地区的夜空中。",
    "满月": "画面以月亮为主体。",
    "日食月食": "画面记录的是日月食过程。",
    "彩虹": "画面里有彩虹或日晕一类的大气光学现象。",
    "云海": "画面是云海翻涌的景象。",
    "雾凇": "画面是雾凇挂满枝头的景象。",
}
_THEME_SENTENCE = {
    "天象": "画面是夜空中的天象。",
    "地形": "画面以山野地貌为主。",
    "植物": "画面以植物为主。",
    "建筑": "画面以建筑为主。",
    "水景": "画面以水面与岸线为主。",
    "人文": "画面记录的是人文场景。",
}


def _scene_sentence(item: dict) -> str:
    """用已确认的属性写一句客观的画面描述（不编造）。"""
    st = strict_tags(item)
    ev = st.get("events_title") or st.get("events") or set()
    for name, line in _EVENT_SENTENCE.items():
        if name in ev:
            return line
    if st.get("light") == "夜":
        return "画面是夜空下的景象。"
    for cat in (_item_attrs(item).get("题材") or []):
        if cat in _THEME_SENTENCE:
            return _THEME_SENTENCE[cat]
    return ""


def local_intro(item: dict) -> str:
    """没有模型时的即时介绍：只照这张图自己的元数据写，跟图绑定。

    它是「这张照片的说明」，不是某天的一句话 —— 所以不提今天、不提天气、
    不写「外面几度看它正合适」，也不做任何主观评价。

    两段式，跟模型版同一套标准：
      1. 客观画面：这张照片拍的是什么（官方图说，中文图说优先；没有就标题 + 属性）；
      2. 科普解释：这类图是怎么来的、该怎么看（按来源给一句，见 _SOURCE_EXPLAIN）。
    标签只用来选图，绝不进介绍 —— 所以这里不会出现「这是一张天象类图像」
    这种只描述样式的句子。
    """
    a = (_item_attrs(item) if isinstance(item, dict) else {}) or {}
    title = str((item or {}).get("title") or "").strip().rstrip("？?")
    desc = re.sub(r"\s+", " ", str((item or {}).get("desc") or "")).strip()
    # 图说里常挂着「(© 摄影师/图库)」，那是署名不是内容，介绍里不需要再念一遍
    desc = re.sub(r"\s*[（(]©[^）)]*[）)]\s*", " ", desc).strip()
    cjk = len(re.findall(r"[\u4e00-\u9fff]", desc))

    # 署名不再进正文。以前开头固定是「来自 © 某人 / 某图库 的照片，《标题》。」
    # —— 二十到三十个字只说「谁拍的、叫什么」，对「这张图是什么」零贡献，
    # 却占掉正文五分之一。署名交给前端 quoteFrom（它本来就在显示 credit），
    # 省下来的篇幅让给事实句。
    parts = []
    if cjk >= 8 and desc:
        parts.append(_clip_text(desc, 190).rstrip("。；;，,、 "))
    else:
        # 英文图说 / 没有图说：不要硬塞原文，也别写「以原图说明为准」这种废话。
        # 用标题定位 + 已确认的属性写一句客观画面，这是事实陈述。
        line = _scene_sentence(item)
        if title and not _looks_like_id(title):
            line = f"《{title}》" + (line or "")
        if line:
            parts.append(line)
    # 第二段固定是科普：这类图怎么来的、颜色代表什么、该看哪儿
    note = _source_explain(item)
    if note:
        parts.append(note)
    q = "。".join(p for p in parts if p)
    q = re.sub(r"[。；]{2,}", "。", q)
    return (q + ("。" if not q.endswith("。") else "")).strip()


# 按来源给一句「这是什么 / 背景」。都是客观事实，不带主观评价。
_SOURCE_EXPLAIN = (
    ("韦布", "韦布空间望远镜主要在红外波段工作：红外光能穿过尘埃，看到更冷、更远的天体。"),
    ("Webb", "韦布空间望远镜主要在红外波段工作：红外光能穿过尘埃，看到更冷、更远的天体。"),
    ("哈勃", "哈勃空间望远镜在地球轨道上运行，避开了大气层对星光的扰动。"),
    ("Hubble", "哈勃空间望远镜在地球轨道上运行，避开了大气层对星光的扰动。"),
    ("ESO", "欧洲南方天文台在智利阿塔卡马沙漠运营多座望远镜，那里空气干燥、云量少，是地球上条件最好的光学天文台址之一。"),
    ("APOD", "每日一天文图（APOD）由 NASA 维护，每天选一张天文图像并配一段科普说明。"),
)

# 「探测器数据合成」只对真正的深空/观测图成立。NASA 图库里同时有大量
# 普通摄影（长曝光流星、发射现场、机舱合影），对它们说「颜色代表不同波段」
# 就是胡说 —— 之前那张流星长曝照的文案就是这么错的。
_SPACE_CUES = ("nebula", "galaxy", "telescope", "infrared", "deep space", "star cluster",
               "composite image", "wavelength", "星云", "银河", "望远镜", "深空", "红外",
               "星系", "脉冲星", "超新星", "黑洞", "系外行星", "光谱")
_PHOTO_CUES = ("exposure", "photo credit", "photograph", "photographer", "camera",
               "tripod", "lens", "拍摄", "摄影", "快门", "三脚架")


def _source_explain(item: dict) -> str:
    text = f"{item.get('credit','')} {item.get('title','')} {item.get('desc','')}"
    low = text.lower()
    for key, note in _SOURCE_EXPLAIN:
        if key in text:
            return note
    src = str(item.get("src") or "")
    if src in ("nasa", "apod"):
        # 先认「这是一张普通摄影」：长曝光、署名摄影记者、相机/三脚架……
        if any(w in low for w in _PHOTO_CUES):
            return ""
        if any(w in low for w in _SPACE_CUES):
            return ("NASA 的这类图像常由探测器数据合成，颜色往往代表不同波段或元素，"
                    "不一定是肉眼所见。")
        return ""      # 拿不准就不说，宁缺勿错
    if src in ("unsplash", "pexels"):
        return "这类照片来自开放图库，由世界各地的摄影师拍摄上传，题材以自然风景与城市景观为主。"
    return ""


def _intro_todo(items: list[dict]) -> list[dict]:
    """还差一张正式介绍（vision/text）的图。"""
    todo = []
    for it in items:
        if not isinstance(it, dict) or not (it.get("file") or ""):
            continue
        if str(it.get("introBy") or "") in _INTRO_DONE_BY:
            continue
        if int(it.get("introTry") or 0) >= _INTRO_TRY_MAX:
            continue
        todo.append(it)
    return todo


def intro_progress(items: list[dict] | None = None) -> dict:
    its = [i for i in (items if items is not None else _load_index())
           if isinstance(i, dict)]
    by: Counter[str] = Counter()
    for i in its:
        by[str(i.get("introBy") or "").strip() or "none"] += 1
    tagged_attrs = sum(1 for i in its if isinstance(i.get("attrs"), dict) and i["attrs"])
    return {"total": len(its),
            "done": by["vision"] + by["text"],
            "local": by["local"],
            "none": by["none"],
            "visionTagged": sum(1 for i in its if str(i.get("attrsBy") or "") == "vision"),
            "attrsDone": tagged_attrs,
            "by": dict(by)}


def _apply_intro(it: dict, tags: object, intro: str, by: str) -> None:
    """把一次生成的结果写进条目：介绍必写，看图标签（视觉模式）写回属性。"""
    if intro:
        it["intro"] = intro[:400].strip()
        it["introBy"] = by
        it["introAt"] = _dt.datetime.now().isoformat(timespec="seconds")
        if by == "local":
            it["introSpec"] = _INTRO_SPEC      # 本地介绍按当前规格写的，无需重写
    if isinstance(tags, dict) and tags:
        # 视觉标签是「看出来的」，比文字推导准：这层就交给它（不再是并集越滚越肥）
        a = it.get("attrs")
        a = dict(a) if isinstance(a, dict) else {}
        for layer, cats in tags.items():
            if cats:
                a[layer] = list(cats)
        it["attrs"] = a
        it["attrsBy"] = "vision"
        it["tagNote"] = "视觉识图"
        it["taggedAt"] = _dt.datetime.now().isoformat(timespec="seconds")


def _save_progress(done: int, fail: int, left: int) -> None:
    store.write_cache("photo-intro", {
        "at": _dt.datetime.now().isoformat(timespec="seconds"),
        "done": done, "fail": fail, "left": left})


def intro_batch(cfg: dict, force: bool = False,
                throttle: float = 1.0, on_update=None) -> dict:
    """给图库里每张图生成介绍并存档 ——「每图一介」的批处理。

    - 一张图一次调用：能识图时标签+介绍一起来；只能文字时就只写介绍；
      完全没模型就即时写本地兜底介绍（跟图绑定，之后自动升级）。
    - 成功结果每 _SAVE_EVERY 条落一次盘；连续失败就整轮放弃并保留进度。
    - `introTry` 记失败次数，超过 _INTRO_TRY_MAX 的图不再纠缠。
    - `force=True` 时连已写过（vision/text）的也重新生成一遍。
    """
    if not _INTRO_LOCK.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        return _intro_locked(cfg, force, throttle, on_update)
    finally:
        _INTRO_LOCK.release()


def _intro_locked(cfg: dict, force: bool, throttle: float, on_update) -> dict:
    import ai      # 延迟导入：ai 顶部 import photos，这里反着来会循环

    started = _dt.datetime.now().isoformat(timespec="seconds")
    items = _load_index()
    ai_ok = ai.ai_ready(cfg)
    vis_ok = ai.vision_ready(cfg) and bool(
        (cfg.get("photos") or {}).get("vision_tagging", True))
    if force:
        for it in items:
            if isinstance(it, dict):
                it.pop("introTry", None)
    todo = []
    for it in items:
        if not isinstance(it, dict) or not str(it.get("file") or ""):
            continue
        by = str(it.get("introBy") or "")
        if int(it.get("introTry") or 0) >= _INTRO_TRY_MAX:
            continue
        if by in _INTRO_DONE_BY:
            # 已写过介绍。视觉模型可用时，之前仅凭文字写的（text）
            # 也不算看过了图 —— 加入队列升级成「识图版」（标签重新打、介绍重写）
            if by == "text" and vis_ok:
                todo.append(it)
            continue
        todo.append(it)
    # 模型不可用时只补「连介绍都还没有」的（写本地兜底，之后自动升级）；
    # 已写过 local 的不重写，避免每轮空转
    if not ai_ok:
        todo = [it for it in todo if not it.get("introBy")]
    if not todo:
        prog = intro_progress(items)
        prog["changed"] = False
        prog["skipped"] = "nothing to do"
        return prog

    done = fail = 0
    fail_streak = 0
    by_note: Counter[str] = Counter()
    for i, it in enumerate(todo):
        path = LIB_DIR / str(it.get("file") or "")
        ok = False
        if not path.is_file():
            fail += 1
            it["introTry"] = int(it.get("introTry") or 0) + 1
            continue
        try:
            if vis_ok:
                obj = ai._extract_json(ai.chat(
                    cfg, VISION_SYSTEM, _vision_intro_prompt(it),
                    timeout=150, temperature=0.4, image_path=path))
                tags = sanitize_tags(obj.get("tags"))
                intro = re.sub(r"\s+", " ",
                               str(obj.get("intro") or "")).strip() or ""
                if not tags and (not intro or len(intro) < 30):
                    raise RuntimeError("视觉返回的内容太少")
                _apply_intro(it, tags, intro, "vision")
                ok = True
                by_note["vision"] += 1
            elif ai_ok:
                obj = ai._extract_json(ai.chat(
                    cfg, VISION_SYSTEM, _text_intro_prompt(it),
                    timeout=90, temperature=0.7))
                intro = re.sub(r"\s+", " ",
                               str(obj.get("intro") or "")).strip() or ""
                if len(intro) < 30:
                    raise RuntimeError("介绍太短")
                _apply_intro(it, None, intro, "text")
                ok = True
                by_note["text"] += 1
            else:
                li = local_intro(it)
                if li:
                    _apply_intro(it, None, li, "local")
                    ok = True
                    by_note["local"] += 1
        except Exception as exc:
            it["introTry"] = int(it.get("introTry") or 0) + 1
            store.log(f"intro '{str(it.get('title') or '')[:30]}' failed: {exc}")
        if ok:
            done += 1
            fail_streak = 0
            if on_update is not None:
                try:
                    on_update(it)
                except Exception:
                    pass
        else:
            fail += 1
            fail_streak += 1
        if done and done % _SAVE_EVERY == 0:
            _save_index(items, started)
        if done + fail >= 8 or i == len(todo) - 1:
            _save_progress(done, fail, len(todo) - i - 1)
        if fail_streak >= _FAIL_STREAK_STOP:
            store.log(f"intro batch abort: 连续失败 {fail_streak}，保留进度下轮再试")
            break
        time.sleep(throttle)

    _save_index(items, started)
    _save_progress(done, fail, len(todo) - done - fail)
    store.log(f"intro batch: 写好 {done} 张（失败 {fail}），依据 {dict(by_note)}")
    prog = intro_progress(_load_index())
    prog["changed"] = bool(done)
    prog["justDone"] = done
    prog["justFail"] = fail
    return prog


def intro_for(file: str) -> dict:
    """取这张图当前绑定的介绍（直接查索引，不经过壁纸缓存）。

    壁纸缓存可能是几天前算的，里面的 intro 早过期了；显示时必须以
    索引里的最新值为准，否则就会出现「墙上换了图、字还是上一张的」。
    """
    f = str(file or "")
    if not f:
        return {}
    items = _load_index()
    for it in items:
        if not (isinstance(it, dict) and str(it.get("file") or "") == f):
            continue
        # 本地介绍升级过规格（改成「客观画面 + 科普解释」两段式，见 _INTRO_SPEC）：
        # 顺手把旧版本地介绍重写一遍 —— 只动本地那份，模型写的不碰。
        # 重写本地是免费且确定性的；重写模型版要花钱，不能顺手做。
        if (str(it.get("introBy") or "") == "local"
                and int(it.get("introSpec") or 0) < _INTRO_SPEC):
            li = local_intro(it)
            if li:
                _apply_intro(it, None, li, "local")
                it["introSpec"] = _INTRO_SPEC
                _save_index(items)
        return {"file": f,
                "intro": str(it.get("intro") or "").strip(),
                "introBy": str(it.get("introBy") or "").strip()}
    return {}


def ensure_local_intro(file: str) -> dict:
    """当前显示的这张图还没有介绍时，现写一条本地介绍。

    只处理这一张 —— 不做全库预生成。返回 {"intro", "introBy"}。
    """
    f = str(file or "")
    if not f:
        return {}
    items = _load_index()
    for it in items:
        if not (isinstance(it, dict) and str(it.get("file") or "") == f):
            continue
        txt = str(it.get("intro") or "").strip()
        if not txt:
            txt = local_intro(it)
            if txt:
                _apply_intro(it, None, txt, "local")
                _save_index(items)
                store.log(f"local intro on demand: {f}")
        return {"intro": txt, "introBy": str(it.get("introBy") or "")}
    return {}


def intro_one(cfg: dict, file: str) -> dict:
    """给「现在墙上这张」生成正式介绍 —— 按需，一次只做一张。

    视觉模型可用就走识图（顺带把标签按画面重打），否则用文字模型；
    都没有就落本地介绍。这里**不会**去碰别的图，取代了原来的全库预生成。
    """
    import ai      # 延迟导入：ai 顶部 import photos，这里反着来会循环

    f = str(file or "")
    if not f:
        return {"skipped": "no file"}
    if not _INTRO_LOCK.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        items = _load_index()
        it = next((x for x in items
                   if isinstance(x, dict) and str(x.get("file") or "") == f), None)
        if it is None:
            return {"skipped": "not in library"}
        by = str(it.get("introBy") or "")
        if by in _INTRO_DONE_BY:
            return {"file": f, "introBy": by, "changed": False}
        ai_ok = ai.ai_ready(cfg)
        vis_ok = ai.vision_ready(cfg) and bool(
            (cfg.get("photos") or {}).get("vision_tagging", True))
        if not ai_ok and by == "local":
            return {"file": f, "introBy": by, "changed": False}
        path = LIB_DIR / f
        if not _file_ok(f):
            return {"file": f, "skipped": "file missing"}

        err = ""
        try:
            if vis_ok:
                obj = ai._extract_json(ai.chat(
                    cfg, VISION_SYSTEM, _vision_intro_prompt(it),
                    timeout=150, temperature=0.4, image_path=path))
                tags = sanitize_tags(obj.get("tags"))
                intro = re.sub(r"\s+", " ",
                               str(obj.get("intro") or "")).strip() or ""
                if not tags and (not intro or len(intro) < 30):
                    raise RuntimeError("视觉返回的内容太少")
                _apply_intro(it, tags, intro, "vision")
            elif ai_ok:
                obj = ai._extract_json(ai.chat(
                    cfg, VISION_SYSTEM, _text_intro_prompt(it),
                    timeout=90, temperature=0.7))
                intro = re.sub(r"\s+", " ",
                               str(obj.get("intro") or "")).strip() or ""
                if len(intro) < 30:
                    raise RuntimeError("介绍太短")
                _apply_intro(it, None, intro, "text")
            else:
                txt = local_intro(it)
                if txt:
                    _apply_intro(it, None, txt, "local")
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:160]
            it["introTry"] = int(it.get("introTry") or 0) + 1
            store.log(f"intro one '{f}' failed: {err}")
            # 失败也先挂一条本地介绍，别让墙上空着
            if not str(it.get("intro") or "").strip():
                txt = local_intro(it)
                if txt:
                    _apply_intro(it, None, txt, "local")

        _save_index(items)
        return {"file": f, "introBy": str(it.get("introBy") or ""),
                "changed": str(it.get("introBy") or "") in _INTRO_DONE_BY,
                "error": err}
    finally:
        _INTRO_LOCK.release()


def purge_prebuilt_intros() -> dict:
    """清掉「预生成」阶段写下的本地介绍。

    用户要的是「给现在这张图现写」，不是提前把 320 张都糊一段。
    清掉之后，显示到哪张就为哪张生成（ensure_local_intro / intro_one）。
    """
    items = _load_index()
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        if str(it.get("introBy") or "") == "local":
            for key in ("intro", "introBy", "introAt"):
                it.pop(key, None)
            n += 1
    if n:
        _save_index(items)
        store.log(f"purged prebuilt local intros: {n}")
    return {"cleared": n}

