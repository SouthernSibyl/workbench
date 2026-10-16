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
import re
import ssl
import struct
import threading
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

    # 同一天内稳定、跨天轮换；同一时段内不会跳来跳去
    seed = f"{item.get('id')}-{scene.get('iso')}-{scene.get('slot')}"
    s += (int(hashlib.sha1(seed.encode()).hexdigest()[:6], 16) % 1000) / 1000 * 0.8
    return s, hits


def _why_text(scene: dict, hits: dict) -> str:
    """把命中情况说成一句人话。"""
    parts = []
    for label, key in (("事件", "事件"), ("时段", "时段"), ("天气", "天气"),
                       ("季节", "季节"), ("题材", "偏好"), ("节气", "节气")):
        got = hits.get(key)
        if got:
            parts.append(f"{label} {'/'.join(got[:3])}")
    head = f"{scene.get('slotCn','现在')}"
    if scene.get("weatherText"):
        head += f"·{scene['weatherText']}"
    if not parts:
        return f"{head}｜没找到贴合的属性，按每日轮换挑的"
    return f"{head}｜命中 " + "、".join(parts)


def pick(cfg: dict, ctx: dict, wx: dict, limit: int = 10,
         scene: dict | None = None) -> list[dict]:
    items = [i for i in _load_index() if (LIB_DIR / i.get("file", "")).exists()]
    if not items:
        return []
    sc = scene or scene_of(cfg, ctx, wx)
    # 属性只算一次：每条都要用于打分和展示，重复算的话几百条会明显变慢
    cache = {id(i): _item_attrs(i) for i in items}
    scored = sorted(((_score(i, sc, cfg, cache[id(i)]), i) for i in items),
                    key=lambda pair: pair[0][0], reverse=True)
    out = []
    for (score, hits), i in scored[:limit]:
        out.append({
            "file": i["file"], "src": i["src"], "title": i.get("title", ""),
            "desc": (i.get("desc") or "")[:220], "credit": i.get("credit", ""),
            "url": f"/art/library/{i['file']}",
            "link": i.get("link") or i.get("url") or "",
            "tags": (i.get("tags") or [])[:10],
            "attrs": {k: v for k, v in cache[id(i)].items() if k != "flat"},
            "score": round(score, 2),
            "hits": hits,
            "why": _why_text(sc, hits),
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
            cached["cached"] = True
            return cached

    cands = pick(cfg, ctx, wx or {}, limit=12, scene=scene)
    main = cands[0] if cands else None
    payload = {
        "iso": scene["iso"],
        "slot": scene["slot"],
        "scene": scene,
        "why": main["why"] if main else "配图库是空的",
        "main": main,
        "alts": cands[1:],
        "picked": len(cands),
        "computedAt": _dt.datetime.now().isoformat(timespec="seconds"),
        "cached": False,
    }
    if main:
        store.write_cache(key, payload)
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
    return {"total": len(items), "bySource": by,
            "byCategory": dict(by_cat), "byTagged": dict(by_tagged),
            "tagged": sum(by_tagged.values()),
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
    } for i in items[offset:offset + limit]]
    return {"ok": True, "total": len(items), "offset": offset, "limit": limit,
            "byCategory": dict(by_cat), "bySource": dict(by_src), "items": page}
