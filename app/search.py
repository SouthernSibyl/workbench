"""深度搜索：只负责「排队」和「取结果」，自己不抓网页。

为什么这么设计（先读，别改）：

1. **本机抓不了「广」**。实测：维基 / GitHub API 走宿主代理直接超时，
   百度第二次就弹验证码，搜狗只给跳转链，Bing 的中文 query 被截成首字。
   本地能用的搜索源只有 360 一个，正文能抽的只有 IT之家 / 博客园 / 投资界
   这一档静态站。靠这个做「多源深度搜索」是假的。

2. **所以搜索交给外面的 agent**（WorkBuddy 定时任务，它有服务端的 WebSearch /
   domain_search，不受本机代理限制）。工作台只做三件事：
     写任务文件  →  agent 搜完写结果文件  →  这里读出来给前端

3. **结果里必须有出处**。没有 url 的条目一律丢 —— 摘要可以概括，但不能无源。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

import store

QUEUE_DIR: Path = store.DATA_DIR / "outbox" / "search"
RESULT_DIR: Path = store.CACHE_DIR / "search"

# 结果多久算过期。搜索类内容时效性强，但重搜很贵，所以给一天。
RESULT_TTL_HOURS = 24

# 信源分层：一手 > 二手 > 三手。排序和展示都按这个来。
TIER_ONE = ("gov.cn", "edu.cn", "arxiv.org", "github.com", "openai.com", "anthropic.com",
            "microsoft.com", "google.com", "apple.com", "deepmind.com", "nature.com",
            "science.org", "ieee.org", "acm.org")
TIER_THREE = ("baijiahao.baidu.com", "toutiao.com", "sohu.com", "163.com/dy",
              "zhuanlan.zhihu.com", "csdn.net", "jianshu.com", "36kr.com/p")


def _now() -> dt.datetime:
    """带时区的当前时间。

    以前返回 naive datetime，而结果文件里的时间戳是 agent 写的、带 +08:00，
    两者相减直接 TypeError（踩过）。统一 aware，别再改回去。
    """
    return dt.datetime.now().astimezone()


def _norm(topic: str) -> str:
    """归一化：去掉空白与标点，全角转半角，大小写统一。

    幂等键建立在这上面 —— 同一个话题重搜要覆盖旧结果，不能攒出十份。
    """
    s = (topic or "").strip().lower()
    s = s.replace("　", " ")
    s = re.sub(r"[\s,，、。；;：:！!？?\"'“”‘’（）()【】\[\]]+", "", s)
    return s


def task_id(topic: str) -> str:
    return hashlib.sha1(_norm(topic).encode("utf-8")).hexdigest()[:12]


def _ensure_dirs() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            import time
            time.sleep(0.2 * (i + 1))
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 队列

def submit(topic: str, depth: str = "normal") -> dict:
    """把话题写成一个待处理任务。同一个话题重复提交只更新不重复。"""
    topic = (topic or "").strip()
    if len(topic) < 2:
        return {"ok": False, "message": "话题太短"}
    if len(topic) > 80:
        return {"ok": False, "message": "话题太长，拆开搜更准"}

    _ensure_dirs()
    tid = task_id(topic)
    path = QUEUE_DIR / f"task-{tid}.json"

    old = None
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            old = None

    task = {
        "id": tid,
        "topic": topic,
        "depth": depth if depth in ("normal", "deep") else "normal",
        "createdAt": (old or {}).get("createdAt") or _now().isoformat(timespec="seconds"),
        "requestedAt": _now().isoformat(timespec="seconds"),
        "times": int((old or {}).get("times") or 0) + 1,
        "status": "pending",
    }
    _atomic_write(path, task)
    return {"ok": True, "task": task, "path": str(path), "prompt": prompt_for(topic, depth)}


def pending() -> list[dict]:
    _ensure_dirs()
    out = []
    for p in sorted(QUEUE_DIR.glob("task-*.json")):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if t.get("status") == "pending":
            out.append(t)
    return out


def mark_done(tid: str) -> None:
    """agent 写回结果后，把任务标记成完成（不清文件，留痕）。"""
    p = QUEUE_DIR / f"task-{tid}.json"
    if not p.exists():
        return
    try:
        t = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    t["status"] = "done"
    t["doneAt"] = _now().isoformat(timespec="seconds")
    _atomic_write(p, t)


# ---------------------------------------------------------------- 结果

def _result_path(tid: str) -> Path:
    return RESULT_DIR / f"{tid}.json"


def get(topic: str) -> dict:
    """读某个话题的结果。三种状态：无 / 排队中 / 有结果。"""
    tid = task_id(topic)
    rp = _result_path(tid)
    task = None
    tp = QUEUE_DIR / f"task-{tid}.json"
    if tp.exists():
        try:
            task = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            pass

    if not rp.exists():
        return {"ok": True, "status": "queued" if task else "none",
                "task": task, "id": tid}

    try:
        data = json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "message": "结果文件坏了", "id": tid, "status": "broken"}

    data["id"] = tid
    data["status"] = "ready"
    data["stale"] = _is_stale(data.get("doneAt") or data.get("createdAt") or "")
    data["sources"] = _clean_sources(data.get("sources") or [])
    return {"ok": True, **data}


def _is_stale(iso: str) -> bool:
    try:
        t = dt.datetime.fromisoformat(iso)
    except Exception:
        return True
    now = _now()
    if t.tzinfo is None:                     # agent 可能写不带时区的时间
        t = t.replace(tzinfo=now.tzinfo)
    return (now - t) > dt.timedelta(hours=RESULT_TTL_HOURS)


def _clean_sources(sources: list) -> list:
    """只保留有 url 的条目，按信源层级排序。

    没有出处的摘要一律不给 —— 宁可少显示几条，也不让用户看见无源结论。
    """
    out = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        tier = int(s.get("tier") or 0)
        if not tier:
            if any(h in url.lower() for h in TIER_ONE):
                tier = 1
            elif any(h in url.lower() for h in TIER_THREE):
                tier = 3
            else:
                tier = 2
        out.append({
            "title": str(s.get("title") or host)[:120],
            "url": url,
            "host": host,
            "tier": max(1, min(3, tier)),
            "note": str(s.get("note") or "")[:200],
        })
    out.sort(key=lambda x: (x["tier"], x["host"]))
    # 同一个域名最多留 3 条，避免一个聚合站刷屏
    seen: dict[str, int] = {}
    kept = []
    for s in out:
        n = seen.get(s["host"], 0)
        if n >= 3:
            continue
        seen[s["host"]] = n + 1
        kept.append(s)
    return kept[:24]


# ---------------------------------------------------------------- 给外面 agent 的提示词

PROMPT_TEMPLATE = """你是「深度搜索」的执行方。工作台把话题排进队列，你负责搜完并把结果写成文件。

## 任务
读这个文件，里面可能有多个待处理任务（status=pending）：
{task_dir}

每个任务形如：{{"id":"<12位>","topic":"<话题>","depth":"normal|deep"}}

## 搜什么
对每个 topic，**不要只搜一次**。先把话题拆成 8-12 条查询，四类各 2-3 条：
- 本体：「{topic} 是什么」「{topic} 原理 / 机制」
- 时效：「{topic} 2026」「{topic} 最新进展」
- 反面：「{topic} 争议」「{topic} 失败 / 问题 / 被质疑」
- 一手：`site:github.com {topic}`、`site:arxiv.org {topic}`、官方文档 / 公告
英文再补 1-2 条（英文资料常常更全）。

**硬性下限**：最终至少覆盖 8 个不同域名。不够就换关键词继续搜，
直到够数，或者在 brief 里明确写「只找到 N 个可信来源」。

## 怎么写
搜完后写一个文件：{result_dir}\\<id>.json ，UTF-8，不要 markdown 围栏，结构固定：

{{
  "id": "<照抄任务里的 id>",
  "topic": "<照抄任务里的 topic>",
  "createdAt": "<现在时间，ISO 8601，本地时区 +08:00>",
  "doneAt": "<同上>",
  "brief": "200-320 字的中文综述。先说结论，再说关键分歧，最后说这条信息有多可靠。不要罗列，不要复述标题。",
  "points": ["3-6 条要点，每条一句话，必须是事实而非评价"],
  "sources": [
    {{"title": "原文标题", "url": "https://...", "tier": 1, "note": "这条支撑了什么，15 字以内"}}
  ],
  "queries": ["你实际搜过的查询，原样列出，方便以后复现"],
  "caveats": "哪里没查到、哪里有矛盾、哪条存疑。没有就写空字符串。"
}}

## 红线
- **不许编造** URL、标题、数字、机构名、日期。搜不到就不写，不猜。
- 每条 source 必须有真实可打开的 url；写不出 url 的条目直接删掉。
- tier 按信源分级：1=一手（官方/论文/仓库/原始数据），2=媒体，3=聚合转载与自媒体。
- brief 里不要出现「据报道」「据悉」这类无主语的转述；要么点名来源，要么不写。
- 一个域名最多 3 条来源。
- 只处理 status=pending 的任务；写完后不用改任务文件（工作台会自动标记）。

## 完成后
打印：处理了几个任务、每个任务的 id、写到了哪个绝对路径、各覆盖多少个域名。
如果有任务失败，打印 {{"ok":false,"reason":"..."}}，不要交半成品。
"""


def prompt_for(topic: str = "", depth: str = "normal") -> str:
    """给用户直接粘进 WorkBuddy 定时任务的提示词。"""
    return PROMPT_TEMPLATE.format(
        task_dir=str(QUEUE_DIR),
        result_dir=str(RESULT_DIR),
        topic=topic or "<话题>",
    )


def stats() -> dict:
    _ensure_dirs()
    done = list(RESULT_DIR.glob("*.json"))
    return {"queued": len(pending()), "results": len(done),
            "queueDir": str(QUEUE_DIR), "resultDir": str(RESULT_DIR)}
