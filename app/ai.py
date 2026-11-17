"""个性化内容生成：RSS 抓取 + OpenAI 兼容接口调用 + 缓存。

设计要点：**永远先读缓存**。模型调用永远发生在后台线程，绝不阻塞界面。
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor as _TPE

import rank
import store
import sources
import photos

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------- HTTP 小工具

def _http_get(url: str, timeout: int = 14) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        return raw


def _http_post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
        **headers,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


# ------------------------------------------------------------------ RSS 抓取

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _declared_encoding(raw: bytes) -> str:
    head = raw[:400].decode("ascii", "ignore").lower()
    m = re.search(r'encoding\s*=\s*["\']([\w.-]+)["\']', head)
    return (m.group(1) if m else "").lower()


def _parse_xml(raw: bytes):
    """解析 XML：兼容 UTF-8 / GBK / GB18030 等多种声明。"""
    encodings = [e for e in (_declared_encoding(raw), "utf-8", "gb18030", "gbk") if e]
    text = None
    tried = set()
    for enc in encodings:
        if enc in tried:
            continue
        tried.add(enc)
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", "ignore")
    # ElementTree 不接受带 encoding 声明的 str
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text).strip()
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def fetch_feed(url: str, limit: int = 20) -> list[dict]:
    try:
        raw = _http_get(url, timeout=14)
    except Exception:
        return []
    root = _parse_xml(raw)
    if root is None:
        return []
    items: list[dict] = []
    for node in root.iter():
        if _strip_ns(node.tag) not in ("item", "entry"):
            continue
        title = link = pub = summary = ""
        for child in node:
            name = _strip_ns(child.tag)
            if name == "title" and not title:
                title = (child.text or "").strip()
            elif name == "link" and not link:
                link = (child.get("href") or child.text or "").strip()
            elif name in ("pubDate", "published", "updated") and not pub:
                pub = (child.text or "").strip()
            elif name in ("description", "summary", "content") and not summary:
                summary = _clean_text(child.text or "")
        if title:
            items.append({"title": title, "link": link, "published": pub,
                          "summary": summary[:220]})
        if len(items) >= limit:
            break
    return items


def _clean_text(html: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
              .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", txt).strip()


def _is_entertainment(text: str, excludes: list[str]) -> bool:
    return any(k and k in text for k in excludes)


def _parse_date(text: str):
    """RSS 日期解析：RFC822 与 ISO 都吃。"""
    s = (text or "").strip()
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def fetch_news_pool(cfg: dict, force: bool = False,
                    allow_stale: bool = False) -> list[dict]:
    """抓一轮资讯池：按来源分好类，丢掉娱乐类与过期条目。

    结果缓存 15 分钟（同一轮里 build_prompt / 多次刷新不会重复抓全网）。

    `allow_stale=True`：过期了也先用旧池，重抓丢到后台 —— 这是给「界面要立刻出东西」
    的路径用的（比如 /api/prompt）。真正生成简报时不要用它，那要的是当天的资讯。
    """
    nc = cfg.get("news") or {}
    ttl = int(nc.get("pool_ttl_seconds") or 900)
    cached = store.read_cache("news-pool")
    have = (cached.get("items") if isinstance(cached, dict) else None) or None
    if not force and have:
        try:
            age = (_dt.datetime.now()
                   - _dt.datetime.fromisoformat(cached.get("at", ""))).total_seconds()
        except (ValueError, TypeError):
            age = 10 ** 9
        if age < ttl:
            return have
        if allow_stale:
            # 界面要的是「立刻有东西可看」，不是「绝对最新」。
            # 先把旧池给它，重抓放到后台去 —— 这条路上不许等 14 个源。
            _refresh_pool_async(cfg)
            return have

    feeds = [f for f in (nc.get("feeds") or []) if isinstance(f, dict)]
    excludes = list(nc.get("exclude") or store.NEWS_EXCLUDE)
    per_feed = int(nc.get("per_feed") or 14)
    max_age = int(nc.get("max_age_days") or 3)
    cutoff = _dt.datetime.now() - _dt.timedelta(days=max_age)

    pool: list[dict] = []
    seen: set[str] = set()
    stale_total = 0

    def pull(f):
        url = str((f or {}).get("url") or "").strip()
        if not url:
            return []
        name = str((f or {}).get("name") or url)
        cat = str((f or {}).get("cat") or "要闻")
        rows = []
        for it in fetch_feed(url, limit=per_feed):
            blob = f"{it['title']} {it.get('summary','')}"
            if _is_entertainment(blob, excludes):
                continue
            pub = _parse_date(it.get("published", ""))
            rows.append((it, pub, name, cat))
        if not rows:
            store.log(f"feed gave nothing: {name}")
        return rows

    # 并发抓。14 个源串行要两分钟；并发高了才不会被某一个慢源拖死
    #（一个源卡满 14 秒超时，7 路下会让整轮多等一整批）。
    with _TPE(max_workers=min(12, max(2, len(feeds) or 2))) as ex:
        for rows in ex.map(pull, feeds):
            for it, pub, name, cat in rows:
                title = it["title"]
                key = title.lower()
                if key in seen:
                    continue
                if pub is not None and pub < cutoff:
                    stale_total += 1
                    continue      # 过期条目（对付那些偷偷换回存档的源）
                seen.add(key)
                pool.append({"title": title, "url": it["link"], "source": name,
                             "cat": cat, "summary": it.get("summary", ""),
                             "published": it.get("published", ""),
                             "at": pub.isoformat(timespec="minutes") if pub else ""})

    if stale_total:
        store.log(f"news pool dropped {stale_total} stale items (>{max_age}d)")
    pool.sort(key=lambda p: p.get("at") or "", reverse=True)
    if pool:
        store.write_cache("news-pool", {
            "at": _dt.datetime.now().isoformat(timespec="seconds"), "items": pool})
    return pool


_POOL_LOCK = threading.Lock()
_pool_kicked = 0.0


def _refresh_pool_async(cfg: dict, min_interval: int = 60) -> bool:
    """后台重抓资讯池，带节流。"""
    global _pool_kicked
    with _POOL_LOCK:
        now = time.time()
        if now - _pool_kicked < min_interval:
            return False
        _pool_kicked = now

    def job():
        try:
            fetch_news_pool(cfg, force=True)
            store.log("news pool refreshed in background")
        except Exception as exc:
            store.log(f"async pool refresh failed: {exc}")

    threading.Thread(target=job, name="wb-pool", daemon=True).start()
    return True


# --------------------------------------------------------------- 模型调用

def ai_ready(cfg: dict) -> bool:
    ai = cfg.get("ai") or {}
    return bool(str(ai.get("base_url") or "").strip() and str(ai.get("api_key") or "").strip())


def vision_ready(cfg: dict) -> bool:
    """视觉识图要用多模态模型（ai.vision_model），与文字模型分开配。"""
    ai = cfg.get("ai") or {}
    return bool(str(ai.get("base_url") or "").strip() and str(ai.get("api_key") or "").strip()
                and str(ai.get("vision_model") or "").strip())


def _image_mime(path: Path) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".bmp": "image/bmp"}.get(path.suffix.lower(),
                                                             "image/jpeg")


def chat(cfg: dict, system: str, user: str, timeout: int = 90,
         temperature: float = 0.7, image_path=None) -> str:
    """OpenAI 兼容调用。

    `image_path` 传了就发多模态消息（模型自动取 `ai.vision_model`），
    用于给图片做识图打标与介绍生成；没传就是普通的文字调用。
    """
    import base64
    from pathlib import Path as _P
    ai = cfg.get("ai") or {}
    base = str(ai.get("base_url") or "").strip().rstrip("/")
    key = str(ai.get("api_key") or "").strip()
    if not base or not key:
        raise RuntimeError("未配置 API")
    if image_path is not None:
        img = _P(image_path)
        model = str(ai.get("vision_model") or "").strip()
        if not model:
            raise RuntimeError("没配置视觉模型（ai.vision_model），发不了图")
        b64 = base64.b64encode(img.read_bytes()).decode("ascii")
        mime = _image_mime(img)
        user_content: object = [
            {"type": "text", "text": user},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
    else:
        model = str(ai.get("model") or "").strip()
        user_content = user
    if not base.endswith("/chat/completions"):
        base = base + "/chat/completions"
    payload = {
        "model": model or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "stream": False,
    }
    resp = _http_post_json(base, payload, {"Authorization": f"Bearer {key}"}, timeout=timeout)
    choices = resp.get("choices") or []
    if not choices:
        raise RuntimeError("模型返回为空")
    return str((choices[0].get("message") or {}).get("content") or "")


def _extract_json(text: str) -> dict:
    """尽量从模型回复里抠出 JSON：容忍 ```json 包裹、前后寒暄、多余括号。"""
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json|JSON)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    # 括号配平扫描，找出第一个完整的 JSON 对象
    start = raw.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        break
        start = raw.find("{", start + 1)
    raise ValueError("回复里没有找到合法的 JSON")


# ------------------------------------------------------------------ 兜底内容

# 这里原来有一组「把一件小事做到今天的极限」之类的万能励志句，无模型时轮流用。
# 已经删掉：它们跟墙上那张照片毫无关系，而每日一句现在要介绍壁纸
# （见 wallpaper_quote）。


_WP_LAYER_ORDER = ("事件", "题材", "天气", "时段", "季节", "构图")


def _is_cjk_heavy(text: str) -> bool:
    if not text:
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cjk >= max(8, len(text) * 0.25)


def wallpaper_block(wp: dict | None) -> str:
    """把「今天选中的那张壁纸」写成一段给模型看的信息。没有就返回空串。"""
    if not wp:
        return "（今天还没选出壁纸）"
    a = wp.get("attrs") or {}
    lines = [f"标题：{wp.get('title') or '（无标题）'}",
             f"来源：{wp.get('credit') or wp.get('src') or '未知'}"]
    tags = []
    for layer in _WP_LAYER_ORDER:
        vals = [str(x) for x in (a.get(layer) or []) if x]
        if vals:
            tags.append(f"{layer} {'/'.join(vals[:2])}")
    if tags:
        lines.append("属性：" + "；".join(tags))
    desc = re.sub(r"\s+", " ", str(wp.get("desc") or "")).strip()
    if desc:
        lines.append(f"原图说明：{desc[:200]}")
    # 不给模型看「为什么挑它」：那是选图打分的内部理由（天气×时段×命中标签），
    # 与照片本身无关，喂给它只会把介绍写成「今天适合看它」那种个性化话。
    return "\n".join(lines)


def wallpaper_quote(wp: dict | None, ctx: dict, wx: dict | None = None) -> tuple[str, str]:
    """没有模型时，用壁纸自身的元数据写一段「这张图是什么」的介绍。

    以前这里会带上「今天这张是…」和「外面是阴、27.3°C，看它正合适」这类
    个性化尾巴 —— 图一换、字没换，就成了「墙上换了图、话还是上一张的」。
    现在只描述这张图本身，交给 photos.local_intro 生成（无时间性、可复用）。
    """
    import photos      # 延迟导入：photos 在本模块下方也被引用，函数内引更稳
    if not wp:
        return ("这张壁纸还没选出来 —— 图库是空的，或者这会儿正在抓图。"), "工作台"
    credit = str(wp.get("credit") or "").strip()
    return photos.local_intro(wp), credit or "工作台"


def _quote_for(wp: dict | None) -> str:
    """这句描述的是哪张图 —— 壁纸按时段换了要拿它比对。"""
    return str((wp or {}).get("file") or "")


def _fallback_daily(ctx: dict, wx: dict | None = None,
                    wallpaper: dict | None = None) -> dict:
    """没有模型时的兜底文案。会尽量用上天气与节气，别让它显得空。"""
    # 以前这里是「把一件小事做到今天的极限」这类万能励志句，跟墙上那张照片毫无关系。
    # 现在改成照着当天选中的壁纸写，至少说的是「这张拍的是什么」。
    q, src = wallpaper_quote(wallpaper, ctx, wx)
    fes = ctx.get("festival") or ""
    wx = wx or {}

    bits = [ctx.get("dateText", "")]
    if wx.get("ok"):
        bits.append(f"{wx.get('city','')}{wx.get('text','')}")
        if wx.get("temp") is not None:
            bits.append(f"{wx['temp']}°C")
        if wx.get("phase"):
            bits.append(wx["phase"])
    if fes:
        bits.append(fes)

    if fes and wx.get("ok"):
        title = f"{fes} · {wx.get('text','')}"
    elif fes:
        title = f"今天是{fes}"
    elif wx.get("ok"):
        title = f"{wx.get('text','')}的一天"
    else:
        title = "把今天留给一件具体的事"

    detail = " · ".join(b for b in bits if b)
    if wx.get("ok"):
        detail += f"。{wx.get('phase','')}，"
        detail += ("光线柔和，适合出去拍点东西。" if wx.get("phase") in
                   ("日出时分", "日落黄金时刻", "蓝调时刻", "午后斜阳")
                   else "在室内把手上那件事收个尾也不错。")
    detail += " 这条是本地兜底；正式推荐由每日任务生成，与你的使用痕迹相关。"

    return {
        "quote": q,
        "quoteFrom": src,
        "quoteFor": _quote_for(wallpaper),
        "recommend": {"title": title, "detail": detail, "action": ""},
        "tags": [],
        "generatedBy": "local",
    }


# ---------------------------------------------------------------- 主生成入口

SYSTEM_PROMPT = (
    "你是一个中文个人工作台的编辑。你只输出 JSON，不要解释、不要 markdown 代码块。"
    "你的活儿是：把一天的公开资讯读一遍，写成一份克制的当日简报。"
    "风格要求：具体、干净、不用 emoji、不喊口号、不写「值得关注」「引发热议」这种空话。"
    "严格禁止娱乐八卦、明星、综艺、影视票房、体育赛事类内容，一条都不要出现。"
)

USER_TEMPLATE = """今天是 {date}（{weekday}）{festival_line}{weather_line}。

【今天墙上这张照片】
{wallpaper_block}

【使用者最近的活动痕迹】
{brief_signals}

【本地已抓到的线索】（{pool_count} 条。可直接用，也可以不用 —— 它只是底料，不是全部）
{pool}

【你的检索任务】
上面这些是工作台在本机抓的，覆盖面有限：受网络限制，海外一手源基本抓不到，
聚合站转载多、原始出处少。所以在动笔之前，请你自己另外检索 6-10 个查询，补齐四类：

  1. 这位使用者真正关心的方向上的**最新进展**（不是泛泛的行业综述）
  2. 上面线索里只有一家在说的事 —— 去交叉验证，看别人怎么说
  3. 中文媒体没覆盖、但英文一手源有的（论文 / 官方公告 / 仓库 / 监管文件）
  4. **反方**：某条热点的质疑、失败案例、被证伪的说法

外部检索到的每一条，必须带真实可打开的 url；给不出 url 的，一条都不要写。

最终 10 条里，**至少 3 条来自你自己检索**（不是上面列表里的）。
如果确实一条都没搜到，就老实只用列表，并在 brief 末尾写一句「今天只用到本地线索」。

请输出如下 JSON：

{{
  "recommend": {{
    "title": "一件今天值得做的事，12 字以内",
    "detail": "一到两句话说明为什么是今天、怎么做，40-80 字",
    "action": "可选：一条可执行的命令/链接/关键词，没有就留空字符串"
  }},
  "tags": ["3-6 个概括使用者当前关注方向的中文标签"],
  "brief": "当日总览，2-3 句话，说清今天最要紧的几件事走向",
  "github": [
    {{
      "name": "owner/repo",
      "url": "仓库地址（github.com/owner/repo）",
      "desc": "一句话说清它是什么，40 字以内",
      "note": "缩略简报，2-3 句、60-100 字：它是什么、为什么这段时间涨星、能用在哪",
      "why": "和这位使用者的哪件事有关，20-40 字，没关联就留空字符串",
      "stars": "近期涨星的大致量级，如「本周 +3k」",
      "lang": "主要语言"
    }}
  ],
  "items": [
    {{
      "title": "标题：候选里的原样照抄，或你自己搜到的那条的原标题",
      "cat": "要闻",
      "points": ["要点1", "要点2", "要点3"],
      "why": "这条跟使用者有什么关系，20-40 字；没关联就留空字符串",
      "source": "来源名或域名",
      "url": "必须能打开；自己搜的条目尤其要填真链接",
      "external": "自己检索来的写 true，用的上面列表里的写 false"
    }}
  ]
}}

硬性要求：
1. 一共挑 {count} 条。cat 只能从这些里选：要闻、财经、科技、学术、与你相关。
   「要闻」偏政治外交与国计民生（相当于新闻联播的分量），「财经」偏经济金融产业，
   「科技」偏技术产品，「学术」偏论文与研究进展，「与你相关」是结合使用者痕迹挑出的、
   他个人会真正用上的那几条。
2. points 是这条的**要点**：3-5 条，每条是一个完整判断句（18-30 字），要带具体的人名、
   机构、数字或事件，不能写「情况有所改善」这种空壳句。**不要**在 points 里重复标题。
3. 不要写 summary 字段 —— 详细简述会另开一轮生成。
4. 候选里没有的标题、链接一律不得出现。宁可少写几条。
5. why 必须是一句**通顺完整的中文**，说清「这条和这位使用者的哪件事有关」；
   不要出现语义不通的词语拼接，写不出来就留空字符串。
6. 绝对不写娱乐、明星、综艺、影视、体育赛事内容。
7. recommend 的 detail 可以顺一下「今天墙上这张照片」的氛围，但不要写成景点广告或口号。
8. github 是「近期涨星很快的开源项目」推荐，**不在候选池里**，由写答案的一方自己
   检索 GitHub 近一周的热门/快速涨星项目后挑选，**3-5 条为宜，不要贪多**。
   个性化只依据上面那份「使用痕迹」与兴趣词：有就贴合他实际在弄的东西，
   没有就挑通用性强、一看就懂的项目，**不要凭想象给他编一个画像**。
   name/url 必须是真实存在的仓库，不得编造；note 是给折叠区看的缩略简报，2-3 句话
   说清「是什么、为什么涨、能用在哪」，不要写成标题复读；stars 写大致量级，不要捏造
   精确数字；拿不到可靠数据就省略整个 github 字段，宁缺毋滥。"""

EXPAND_SYSTEM = (
    "你是一个中文编辑，给新闻写「简述」。只输出正文，不要标题、不要 markdown 标记、"
    "不要 emoji、不要「据介绍」「据悉」这类填充词。风格克制、信息密度高、句子短。"
)

EXPAND_TEMPLATE = """请为下面这条资讯写一段中文简述。

标题：{title}
来源：{source}
分类：{cat}
已知要点：
{points}

候选原文摘要（仅供你参考，不要照抄）：
{ref}

要求：
1. 长度 {target} 字左右（上下浮动不超过 15%），分成 2-4 个自然段。
2. 第一段说清「发生了什么」，要有具体的时间、地点、机构、数字。
3. 中间段说清「意味着什么」：背景、影响、牵扯到谁、后续会怎样。
4. 最后一段可以点一句「和读者有什么关系」，但不要生硬、不要喊口号。
5. 只依据标题、要点和参考摘要里已有的信息写，**不得编造**具体数字、人名、引语。
   信息不足时就写得概括一些，宁可短，也不要编。
6. 不要复述标题，不要写「本文」「这条新闻」这类元叙述。"""


def read_budget(cfg: dict) -> int:
    nc = cfg.get("news") or {}
    minutes = int(nc.get("read_minutes") or 15)
    cpm = int(nc.get("chars_per_minute") or 400)
    return max(1200, minutes * cpm)


def per_item_chars(cfg: dict) -> int:
    nc = cfg.get("news") or {}
    n = max(1, int(nc.get("count") or 10))
    return max(220, read_budget(cfg) // n)




def _weather_line(wx: dict | None) -> str:
    """提示词里那句天气。API 模式与「只重写一句」共用。"""
    if not (wx and wx.get("ok")):
        return ""
    temp = f"、{wx.get('temp')}°C" if wx.get("temp") is not None else ""
    return f"，{wx.get('city','')}{wx.get('text','')}{temp}，{wx.get('phase','')}"




def build_prompt(cfg: dict, ctx: dict, wx: dict | None = None,
                 fast: bool = False, wallpaper: dict | None = None) -> dict:
    """组装提示词（API 模式与网页版模式共用同一份）。

    `fast=True` 是给「界面点一下就要看到提示词」的路径用的：
    资讯池允许用旧的一版（后台再刷），不为了绝对最新让人干等十几秒。

    `wallpaper` 是今天墙上那张图。每日一句要介绍它，所以得先选好壁纸再写提示词；
    没传就只读缓存（不重算，免得给 /api/prompt 添几百毫秒）。
    """
    # 本机痕迹是隐私相关的，走 signals.enabled 开关（设置里可关）。
    # 关掉就整段不给模型看，也不去读浏览器历史 —— 不能只是「显示上隐藏」。
    sig_cfg = cfg.get("signals") or {}
    if sig_cfg.get("enabled", True):
        signals = sources.collect(cfg)
        sig_text = sources.interest_brief(signals) or "（暂无本地痕迹）"
    else:
        signals = {"skills": [], "projects": [], "tools": [], "searches": [],
                   "sites": [], "files": [], "browser": "",
                   "disabled": True}
        sig_text = "（未启用本地痕迹采集）"
    news_cfg = cfg.get("news") or {}
    count = int(news_cfg.get("count") or 10)
    _t_pool = time.perf_counter()
    pool = fetch_news_pool(cfg, allow_stale=fast)
    _pool_ms = (time.perf_counter() - _t_pool) * 1000
    pool = rank_pool(pool, signals, count * 5, boost=news_cfg.get("boost"))
    if _pool_ms > 1500:
        store.log(f"build_prompt: 取资讯池花了 {_pool_ms:.0f}ms 条数={len(pool)} fast={fast}")

    pool_text = "\n".join(
        f"- [{p['cat']}|{p['source']}] {p['title']} :: {p['url']}"
        + (f"\n  摘要：{p['summary'][:150]}" if p.get("summary") else "")
        for p in pool[:60]
    ) or "（抓取失败或没有配置资讯源）"

    weather_line = _weather_line(wx)

    if wallpaper is None:
        wallpaper = photos.peek_chosen(cfg, ctx, wx)

    user = USER_TEMPLATE.format(
        date=ctx["dateText"], weekday=ctx["weekday"],
        festival_line=f"，今天是{ctx['festival']}" if ctx.get("festival") else "",
        weather_line=weather_line,
        wallpaper_block=wallpaper_block(wallpaper),
        brief_signals=sig_text, pool=pool_text, pool_count=len(pool), count=count,
    )
    return {
        "system": SYSTEM_PROMPT,
        "user": user,
        "combined": SYSTEM_PROMPT + "\n\n" + user,
        "pool": pool,
        "signals": signals,
        "count": count,
        "stats": (f"痕迹 {len(signals.get('searches') or [])} 条搜索 / "
                  f"{len(pool)} 条候选 / 目标 {count} 条 × {per_item_chars(cfg)} 字"),
        "signalsEnabled": bool(sig_cfg.get("enabled", True)),
        "target": str((cfg.get("ai") or {}).get("web_url") or "").strip(),
    }


# 分类优先级，用于兜底排序
_CAT_ORDER = ["要闻", "财经", "科技", "学术"]
_CAT_ALIAS = {"与你相关": "与你相关", "学术": "学术", "科技": "科技",
              "财经": "财经", "要闻": "要闻", "国际": "要闻", "经济": "财经"}


def rank_pool(pool: list[dict], signals: dict, limit: int,
              boost: list[str] | None = None) -> list[dict]:
    """无模型时也要有个像样的排序：按分类轮流取，避免全被一个源刷屏。

    `boost` 是加权关键词（配置里的 `news.boost`）。以前这里是写死的
    `store.NEWS_BOOST` 常量，改不了 —— 现在从配置来，不传才回落到内置那串。
    """
    if not pool:
        return []
    boost_words = list(boost) if boost else list(store.NEWS_BOOST)
    interest_words: list[str] = []
    for s in (signals.get("searches") or [])[:20]:
        interest_words.extend([w for w in re.split(r"[\s,，、。]+", s.get("text", "")) if len(w) >= 2])
    for s in (signals.get("sites") or [])[:20]:
        interest_words.extend([w for w in re.split(r"[\s,，、。\-|]+", s.get("title", "")) if len(w) >= 2])
    interest_words = list(dict.fromkeys(interest_words))[:60]

    def score(p: dict) -> float:
        s = 0.0
        blob = p.get("title", "")
        s += 2.0 * sum(1 for w in interest_words if w in blob)
        s += 1.1 * sum(1 for w in boost_words if w in blob)   # 偏政经 / 学术（关键词可配）
        if p.get("cat") in _CAT_ORDER:
            s += max(0.0, 2.0 - _CAT_ORDER.index(p["cat"]))
        return s

    # 个性化生效后交给 rank 模块重排（它带硬性多样性约束：同来源 ≤2、同分类 ≤4）。
    # 原来的分类轮换在 rank.rerank 里已经包含，这里就不重复做了。
    if rank.active():
        base = {p.get("title"): score(p) for p in pool}
        return rank.rerank(pool, limit, base)

    buckets: dict[str, list[dict]] = {}
    for p in sorted(pool, key=score, reverse=True):
        buckets.setdefault(p.get("cat") or "要闻", []).append(p)
    out: list[dict] = []
    idx = 0
    order = _CAT_ORDER + [k for k in buckets if k not in _CAT_ORDER]
    # 分类轮流取，避免全被一个源刷屏。
    # 终止条件必须看「这一轮有没有真的加上东西」—— 曾经写成
    # `while len(out) < limit and any(buckets...)`，候选总数少于 limit 时
    # idx 会一直涨、那个 any 又恒为真，于是**死循环**（/api/prompt 永不返回）。
    while len(out) < limit:
        appended = False
        for c in order:
            b = buckets.get(c) or []
            if idx < len(b):
                out.append(b[idx])
                appended = True
                if len(out) >= limit:
                    break
        if not appended:
            break
        idx += 1
    return out


def _sentences(text: str, n: int = 3) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?；;])", text or "") if p.strip()]
    return [p for p in parts if len(p) >= 8][:n]


def _fallback_items(pool: list[dict], count: int) -> list[dict]:
    """没有模型时的兜底：要点直接取源站摘要里的句子。"""
    out = []
    for p in pool[:count]:
        pts = _sentences(p.get("summary") or "", 3)
        if not pts:
            pts = [p["title"]]
        out.append({
            "title": p["title"], "cat": p.get("cat") or "要闻",
            "points": pts, "why": "", "source": p.get("source", ""),
            "url": p.get("url", ""), "summary": (p.get("summary") or "")[:400],
            "mins": 0, "stub": True,
            # 发布日期留着：既能显示「几小时前」，也是判断简报新不新鲜的依据
            "at": p.get("at") or "",
        })
    return out


def _merge_model_json(payload: dict, obj: dict, pool: list[dict], count: int) -> None:
    """把模型返回的 JSON 合并进 payload（API 与网页版共用）。

    「每天一句话」这套按天生成的文案已经退役——首页文字现在直接取
    「当前这张图的介绍」（见 photos.intro_batch）。模型再返回 quote
    也不采纳：quote 读旧 locally-bound 兜底句，只把它当占位。
    """
    old = payload.get("fields") or {}
    rec = obj.get("recommend")
    fields = {
        # 本地兜底句也是绑定壁纸的（_fallback_daily），留着当占位；
        # 图库里的 intro 一就位，build_state 就会换它显示
        "quote": str(old.get("quote") or "")[:400],
        "quoteFrom": str(old.get("quoteFrom") or "")[:80],
        "quoteFor": str(old.get("quoteFor") or ""),
        "recommend": rec if isinstance(rec, dict) else old.get("recommend") or {},
        "tags": ([str(t).strip()[:20] for t in (obj.get("tags") or []) if str(t).strip()][:8]
                 or old.get("tags") or []),
        "generatedBy": "ai",
    }
    payload["fields"] = fields
    payload["tags"] = fields["tags"]

    raw_items = obj.get("items") or obj.get("sections")
    by_title = {p["title"].strip(): p for p in pool}
    # 兼容老结构：sections[].items[] 拍平
    flat: list[dict] = []
    if isinstance(raw_items, list):
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            if isinstance(it.get("items"), list) and not it.get("title"):
                for sub in it["items"]:
                    if isinstance(sub, dict):
                        sub = dict(sub)
                        sub.setdefault("cat", it.get("name"))
                        flat.append(sub)
            else:
                flat.append(it)

    if flat:
        prev = {i.get("title"): i for i in ((payload.get("news") or {}).get("items") or [])}
        items = []
        used: set[str] = set()
        for it in flat[:count]:
            title = str(it.get("title") or "").strip()
            if not title or title in used:
                continue                      # 空标题或重复的直接丢
            ref = by_title.get(title)
            external = False
            if ref is None:
                # 池外条目 —— 这是「自己检索」补上来的。
                # 以前这里 `if not ref: continue` 直接丢，等于把「更广」这条腿砍了：
                # 模型搜到的、池子里没有的一律不收，那还搜什么。
                # 现在只要求有一条能打开的 http 链接，就算合法来路。
                url = str(it.get("url") or "").strip()
                if not url.startswith("http"):
                    continue                  # 没链接 = 无源，仍然丢
                host = re.sub(r"^https?://", "", url).split("/")[0].lower()
                ref = {
                    "title": title[:160],
                    "cat": str(it.get("cat") or "要闻"),
                    "source": str(it.get("source") or host)[:60],
                    "url": url[:400],
                    "at": "",
                    "summary": "",
                }
                external = True
            used.add(title)
            pts = [str(p).strip()[:80] for p in (it.get("points") or []) if str(p).strip()][:6]
            if not pts:
                pts = _sentences(ref.get("summary") or "", 3) or [ref["title"]]
            old = prev.get(ref["title"]) or {}
            cat = str(it.get("cat") or ref.get("cat") or "要闻").strip()
            items.append({
                "title": ref["title"],
                "cat": _CAT_ALIAS.get(cat, "要闻"),
                "points": pts,
                "why": str(it.get("why") or "").strip()[:120],
                "source": ref.get("source", ""),
                "url": ref.get("url", ""),
                "summary": old.get("summary") or "",      # 简述保留，别覆盖
                "summaryBy": old.get("summaryBy") or "",  # 作者标记跟简述一起保留，
                # 否则同日重收一次 ingest，模型级简述会被降级成 local，
                # 前端标错、expand 轮还会把它们全部重写一遍（2026-10-28 踩过）。
                "mins": old.get("mins") or 0,
                "stub": bool(old.get("stub")),
                "at": ref.get("at") or old.get("at") or "",
                # 标出来源：external=自己检索补的。前端给个不同的标记，
                # 也方便统计「今天到底有几条是搜来的」。
                "external": external,
            })
        if items:
            payload["news"] = {
                "brief": str(obj.get("brief") or "").strip()[:300],
                "items": items,
                "poolSize": len(pool),
                "source": "ai",
            }

    # GitHub 涨星推荐：外部代写方自己检索后放进答案里的可选段。
    # 不在候选池校验范围（那是给新闻用的），但也要洗一遍：没名字/没链接的丢，
    # 链接必须是 http(s)。同日重复 ingest 时，新一轮没给就不动旧的。
    raw_gh = obj.get("github")
    if isinstance(raw_gh, list):
        repos = []
        for g in raw_gh[:20]:
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "").strip()
            url = str(g.get("url") or "").strip()
            if not name or not url.startswith("http"):
                continue
            repos.append({
                "name": name[:100],
                "url": url[:300],
                "desc": str(g.get("desc") or "").strip()[:200],
                "note": str(g.get("note") or "").strip()[:300],
                "why": str(g.get("why") or "").strip()[:160],
                "stars": str(g.get("stars") or "").strip()[:40],
                "lang": str(g.get("lang") or "").strip()[:30],
            })
        if repos:
            payload["github"] = repos[:8]   # 折叠区别贪多：封顶 8，正常 3-5
    payload["generatedBy"] = "ai"


def build_daily(cfg: dict, ctx: dict, force: bool = False,
                wx: dict | None = None) -> dict:
    """生成（或读取缓存）当天的个性化内容。"""
    key = f"daily-{ctx['iso']}"
    if not force:
        cached = store.read_cache(key)
        if (cached and ((cached.get("news") or {}).get("items") or [])):
            return cached

    # 先把今天这面墙定下来，再写提示词 —— 每日一句要介绍这张图。
    # 选图本身只用本地打分，不依赖模型，所以可以放在前面。
    # 只给「今天」选图：预生成明天简报时不去提前锁死明天的壁纸
    # （提前缓存会跟图库同步/淘汰脱节，第二天就可能指到一张已不存在的图）。
    try:
        if str(ctx.get("iso") or "") == _dt.date.today().isoformat():
            _chosen = photos.choose_daily(cfg, ctx, wx)
            wp = (_chosen or {}).get("main")
        else:
            wp = {}          # 显式「明天还没有壁纸」，别去读缓存里预选的图
    except Exception as _exc:
        store.log(f"build_daily: 选壁纸失败，跳过壁纸介绍（{_exc}）")
        wp = None

    prompt = build_prompt(cfg, ctx, wx, wallpaper=wp)
    pool = prompt["pool"]
    count = prompt["count"]
    per = per_item_chars(cfg)

    payload: dict = {
        "date": ctx["iso"],
        "fields": _fallback_daily(ctx, wx, wp),
        "news": {"brief": "", "items": _fallback_items(pool, count),
                 "poolSize": len(pool), "source": "local"},
        "tags": [],
        "signals": prompt["signals"],
        "generatedBy": "local",
        "generatedAt": _dt.datetime.now().isoformat(timespec="seconds"),
        "aiError": "",
    }

    if ai_ready(cfg):
        try:
            obj = _extract_json(chat(cfg, prompt["system"], prompt["user"], timeout=180))
            _merge_model_json(payload, obj, pool, count)
        except Exception as exc:  # 网络 / 鉴权 / 解析，任何一种都只降级
            payload["aiError"] = f"{type(exc).__name__}: {exc}"[:200]
            store.log(f"build_daily ai failed: {exc}")

    items_out = (payload.get("news") or {}).get("items") or []
    payload["incomplete"] = not items_out

    if payload["incomplete"]:
        # 这次没抓到任何资讯（网络抖一下就会这样）。空壳绝不能覆盖掉已有的好缓存，
        # 否则开机只剩一个空简报，而且下次启动看到缓存存在就不会再重试了。
        old = store.read_cache(key) or {}
        if (old.get("news") or {}).get("items"):
            store.log("build_daily: 本轮没抓到资讯，保留原有缓存")
            return old

    store.write_cache(key, payload)
    store.log(f"build_daily done: {len(items_out)} 条, 每条约 {per} 字"
              + ("（空壳，下次会重试）" if payload["incomplete"] else ""))
    return payload


def is_real_summary(item: dict, cfg: dict) -> bool:
    """判断一条的简述是否「已经能看了」。

    阈值分两档，目的是**不要让界面反复重试同一个必然失败的动作**：
      - 本地重排（summaryBy=local）：同样的材料拼一万遍也是同一个结果，
        所以只要拼出东西（≥30 字）就算可用，再重试纯属浪费；
      - 模型级（summaryBy=ai）：要够 180 字（或半个目标字数）才算数，
        不够就还有升级空间。
    """
    s = (item.get("summary") or "").strip()
    if not s or item.get("stub"):
        return False
    need = 30 if summary_by(item) == "local" else max(180, int(per_item_chars(cfg) * 0.5))
    return len(s) >= need


def summary_by(item: dict) -> str:
    """这条简述是谁写的：ai（模型）/ local（本地重排）/ 空（还没有）。

    本地重排的简述同样能看，只是信息密度不如模型写的 —— 所以要有这个标记，
    界面才能提示「本地整理，待升级」，每日任务也知道该不该重写。
    """
    by = str(item.get("summaryBy") or "").strip()
    if by:
        return by
    return "local" if (item.get("summary") or "").strip() and not item.get("stub") else ""


def _clip_sent(s: str, limit: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= limit:
        return s
    marks = list(re.finditer(r"[。！？!?；;]", s[:limit]))
    return s[:marks[-1].end()] if marks else s[:limit].rstrip("，,、；; ") + "…"


def local_summary(cfg: dict, item: dict) -> str:
    """没有模型时的本地简述：只重排「源站摘要 + 要点」，不编造任何新信息。

    为什么要它：以前没 Key 时点「简述」直接报错，十条全失败（日志里
    `expand_item failed: 还没配置模型` 连着刷屏）。现在先把材料拼成一段
    能读的简述，界面上标「本地整理」，模型可用（或每日任务代写）再升级。

    材料边界：只用 item 里已经有的东西 —— 标题、来源、发布时间、要点、源站摘要。
    宁短不编，所以材料不足时就短，绝不往里添数字和人名。
    """
    target = int(per_item_chars(cfg) or 600)
    title = str(item.get("title") or "").strip()
    src = str(item.get("source") or "").strip()
    facts = [_clip_sent(s, 200) for s in _sentences(item.get("summary") or "", 6)]
    # 第一句常常就是标题的复述，去掉，省 40 字
    if facts and title and (facts[0] in title or title in facts[0]):
        facts = facts[1:] or facts
    pts = [str(p).strip().rstrip("。；;，, ") for p in (item.get("points") or [])
           if str(p).strip()]

    paras: list[str] = []
    if facts:
        paras.append("".join(facts[:3]))
    if pts:
        # 要点是判断句，连起来就是第二段；不要「具体来看」这类过渡词
        joined = "；".join(pts) + "。"
        if not paras or joined not in paras[0]:
            paras.append(joined)

    text = "\n".join(p.strip() for p in paras if p.strip())
    if not text and title:
        # 连摘要和要点都没有：只给一句出处，保证不是空的
        text = f"{title}（{src}）"
    if len(text) > int(target * 1.15):
        text = _clip_sent(text.replace("\n", ""), int(target * 1.15))
    if len(text) < 60 and title:
        # 材料太少（常见：只有两三条要点）。补一句出处与原文指引，
        # 都是事实信息，不添内容。
        tail = f"以上来自{src}" if src else "详见原文"
        at = str(item.get("at") or "")[:10]
        if at:
            tail += f"（{at}）"
        text = text.rstrip("。") + "。" + tail + "。"
    return text.strip()


def compose_summary(cfg: dict, item: dict, retries: int = 2) -> tuple[str, str]:
    """写一条简述：有模型就调模型（失败重试），没模型或仍失败就本地重排。

    返回 (文本, 作者标记)。**永不抛异常** —— 界面点「简述」最怕的就是
    点了没反应还报错，这里保证每次都有东西落盘。
    """
    last = ""
    if ai_ready(cfg):
        for attempt in range(1, max(1, retries) + 1):
            try:
                return generate_summary(cfg, item), "ai"
            except Exception as exc:                     # 网络 / 鉴权 / 解析
                last = f"{type(exc).__name__}: {exc}"
                store.log(f"compose_summary 第 {attempt}/{retries} 次失败: {last}")
                if attempt < retries:
                    time.sleep(0.6 * attempt)
    text = local_summary(cfg, item)
    if not text:
        raise RuntimeError("本地也没有可用材料（标题/摘要都空）")
    if last:
        store.log(f"compose_summary 落到本地重排（模型不可用：{last}）")
    return text, "local"


def _clean_summary(text: str) -> str:
    text = re.sub(r"^#+\s*", "", (text or "").strip(), flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\s*(简述|正文|摘要)[:：]\s*", "", text.strip())
    return text.strip()


def generate_summary(cfg: dict, item: dict, target: int | None = None) -> str:
    """只生成简述文本，不落盘。失败直接抛异常。"""
    if not ai_ready(cfg):
        raise RuntimeError("还没配置模型，简述需要 API Key")
    target = target or per_item_chars(cfg)
    prompt = EXPAND_TEMPLATE.format(
        title=item.get("title", ""), source=item.get("source", ""),
        cat=item.get("cat", ""),
        points="\n".join("- " + p for p in (item.get("points") or [])),
        ref=(item.get("summary") or "").strip() or "（无）",
        target=target,
    )
    text = _clean_summary(chat(cfg, EXPAND_SYSTEM, prompt, timeout=180, temperature=0.5))
    if len(text) < 80:
        raise RuntimeError("模型返回内容过短")
    return text


_CACHE_LOCK = threading.RLock()


def save_summary(cfg: dict, ctx: dict, index: int, text: str,
                 by: str = "ai") -> dict:
    """把某一条的简述写回当天缓存（并发安全）。"""
    key = f"daily-{ctx['iso']}"
    cpm = int((cfg.get("news") or {}).get("chars_per_minute") or 400)
    with _CACHE_LOCK:
        payload = store.read_cache(key) or {}
        news = payload.get("news") or {}
        items = news.get("items") or []
        if index < 0 or index >= len(items):
            return {}
        items[index]["summary"] = text
        items[index]["summaryBy"] = by
        items[index]["mins"] = max(1, round(len(text) / cpm, 1))
        items[index]["stub"] = False
        news["items"] = items
        payload["news"] = news
        store.write_cache(key, payload)
        return items[index]


def expand_item(cfg: dict, ctx: dict, index: int,
                force: bool = False) -> dict:
    """为第 index 条生成详细简述（懒生成，结果写回当天缓存）。

    不会再出现「点了没反应」：本地重排兜底保证一定有产出，
    模型可用时优先用模型（失败重试 2 次才落本地）。
    """
    key = f"daily-{ctx['iso']}"
    payload = store.read_cache(key) or {}
    news = payload.get("news") or {}
    items = news.get("items") or []
    if index < 0 or index >= len(items):
        return {"ok": False, "message": "没有这一条"}
    item = items[index]

    if not force and is_real_summary(item, cfg):
        return {"ok": True, "summary": item["summary"], "mins": item.get("mins") or 0,
                "cached": True, "chars": len(item["summary"]),
                "by": summary_by(item)}

    try:
        text, by = compose_summary(cfg, item)
    except Exception as exc:
        store.log(f"expand_item failed: {exc}")
        return {"ok": False, "message": f"生成失败：{exc}"[:160]}

    saved = save_summary(cfg, ctx, index, text, by=by)
    store.log(f"expand_item[{index}] {len(text)} 字（{by}）")
    return {"ok": True, "summary": text, "mins": (saved or {}).get("mins", 0),
            "chars": len(text), "cached": False, "by": by}


def ensure_summaries(cfg: dict, ctx: dict) -> dict:
    """把当天所有条目的简述补齐：本地重排，零成本、不失败。

    这是「稳定预生成」的那一半 —— 每天缓存一落地，每条都有一个能读的简述，
    用户点开任意一条都不用等。模型写的（summaryBy=ai）不动，只补缺口。
    """
    key = f"daily-{ctx['iso']}"
    with _CACHE_LOCK:
        payload = store.read_cache(key) or {}
        items = ((payload.get("news") or {}).get("items")) or []
        if not items:
            return {"filled": 0, "total": 0, "ai": 0, "local": 0}
        filled = ai_n = local_n = 0
        for i, it in enumerate(items):
            if is_real_summary(it, cfg):
                if summary_by(it) == "ai":
                    ai_n += 1
                else:
                    local_n += 1
                continue
            text, by = compose_summary(cfg, it)
            if not text:
                continue
            items[i]["summary"] = text
            items[i]["summaryBy"] = by
            items[i]["mins"] = max(1, round(len(text) / cpm_of(cfg)))
            items[i]["stub"] = False
            filled += 1
            if by == "local":
                local_n += 1
        if filled:
            payload.setdefault("news", {})["items"] = items
            store.write_cache(key, payload)
            store.log(f"ensure_summaries: 补了 {filled} 条（本地重排）")
    return {"filled": filled, "total": len(items), "ai": ai_n, "local": local_n}


def cpm_of(cfg: dict) -> int:
    return int((cfg.get("news") or {}).get("chars_per_minute") or 400)


def expand_requests(cfg: dict, ctx: dict) -> list[dict]:
    """列出还差「模型级」简述的条目，连同提示词一起打包（给外部代写用）。

    跟每日简报同一套分工：工作台只拼提示词，内容交给外面的模型写，
    最后 `ingest_expand` 按 index 收回。已经有模型级简述的条目不再出现，
    所以每天重跑也只写缺的那几条。
    """
    payload = store.read_cache(f"daily-{ctx['iso']}") or {}
    items = ((payload.get("news") or {}).get("items")) or []
    out: list[dict] = []
    for i, it in enumerate(items):
        if summary_by(it) == "ai":
            continue
        target = int(per_item_chars(cfg) or 600)
        user = EXPAND_TEMPLATE.format(
            title=it.get("title", ""), source=it.get("source", ""),
            cat=it.get("cat", ""),
            points="\n".join("- " + p for p in (it.get("points") or [])),
            ref=(it.get("summary") or "").strip() or "（无）",
            target=target,
        )
        out.append({
            "index": i, "title": it.get("title", ""),
            "source": it.get("source", ""), "cat": it.get("cat", ""),
            "target": target,
            "system": EXPAND_SYSTEM, "user": user,
        })
    return out


def ingest_expand(cfg: dict, ctx: dict, obj: dict) -> dict:
    """把外部写好的简述收进缓存：{"summaries": [{"index": n, "text": "…"}]}。

    逐条校验：index 必须在范围内、正文清洗后不少于 80 字，否则跳过并记账 ——
    宁缺勿烂，坏条目不会把好缓存冲掉。
    """
    raws = (obj or {}).get("summaries") or (obj or {}).get("items") or []
    saved, skipped = [], []
    for one in raws if isinstance(raws, list) else []:
        if not isinstance(one, dict):
            skipped.append("非对象条目")
            continue
        try:
            idx = int(one.get("index"))
        except (TypeError, ValueError):
            skipped.append(f"index 无效：{str(one.get('index'))[:20]}")
            continue
        text = _clean_summary(str(one.get("text") or ""))
        if len(text) < 80:
            skipped.append(f"第 {idx} 条太短（{len(text)} 字）")
            continue
        got = save_summary(cfg, ctx, idx, text, by="ai")
        if got:
            saved.append({"index": idx, "chars": len(text)})
        else:
            skipped.append(f"第 {idx} 条越界")
    store.log(f"ingest_expand: 收下 {len(saved)} 条，跳过 {len(skipped)} 条"
              + (f"（{'; '.join(skipped[:3])}）" if skipped else ""))
    return {"ok": bool(saved), "saved": saved, "skipped": skipped,
            "total": len(saved) + len(skipped)}



def ingest_text(cfg: dict, ctx: dict, text: str, wx: dict | None = None) -> dict:
    """网页版模式：把模型回复贴回来，解析后写进当天缓存。"""
    obj = _extract_json(text)
    # 和 /api/prompt 用同一份池（fast=True），不然用户照着旧提示词写的内容
    # 会因为标题对不上而被校验整批丢掉。
    prompt = build_prompt(cfg, ctx, wx, fast=True)
    key = f"daily-{ctx['iso']}"
    payload = store.read_cache(key) or {}
    if not payload or not (payload.get("news") or {}).get("items"):
        payload = {
            "date": ctx["iso"],
            "fields": _fallback_daily(ctx, wx),
            "news": {"brief": "", "items": _fallback_items(prompt["pool"], prompt["count"]),
                     "poolSize": len(prompt["pool"]), "source": "local"},
            "tags": [],
            "signals": prompt["signals"],
            "generatedBy": "local",
        }
    payload.setdefault("signals", prompt["signals"])
    payload.pop("aiError", None)
    _merge_model_json(payload, obj, prompt["pool"], prompt["count"])
    payload["generatedAt"] = _dt.datetime.now().isoformat(timespec="seconds")
    payload["generatedBy"] = "ai"
    store.write_cache(key, payload)
    return payload


def needs_refresh(cache: dict | None, iso: str, ai_ok: bool = False) -> bool:
    """当天这份缓存还能不能用？

    三种情况必须重做：
      1. 没有条目（上次网络抖动留下的空壳）
      2. 生成时间不是今天 —— 说明它是昨晚预生成的，里面的资讯已经是隔夜的了
      3. 当时没有模型、只写了本地骨架（generatedBy=local），而现在模型可用了
         —— 有机会就要把「真简报」补上，别让一份骨架占着当天的位置
    """
    if not cache:
        return True
    if not (((cache.get("news") or {}).get("items")) or []):
        return True
    if ai_ok and str(cache.get("generatedBy") or "local") != "ai":
        return True
    gen = str(cache.get("generatedAt") or "")[:10]
    return gen != iso


def prefetch_tomorrow(cfg: dict, wx: dict | None = None) -> None:
    """后台预生成次日内容——这样明天开机是「零等待」。

    只认「有条目的」缓存：空壳会自动重试，不会把明天的位置占死。
    """
    try:
        tomorrow = _dt.datetime.now() + _dt.timedelta(days=1)
        ctx = calendar_tomorrow(tomorrow, cfg)
        key = f"daily-{ctx['iso']}"
        cached = store.read_cache(key)
        if cached and not needs_refresh(cached, ctx["iso"], ai_ok=ai_ready(cfg)):
            return
        store.log("prefetch tomorrow start")
        out = build_daily(cfg, ctx, force=True, wx=wx)
        got = len(((out.get("news") or {}).get("items")) or [])
        store.log(f"prefetch tomorrow done: {got} 条"
                  + ("（没抓到，下次开机再试）" if not got else ""))
    except Exception as exc:
        store.log(f"prefetch tomorrow failed: {exc}")


def calendar_tomorrow(now: _dt.datetime, cfg: dict) -> dict:
    import calendar_cn
    return calendar_cn.today_context(now, (cfg.get("anniversaries") or []))
