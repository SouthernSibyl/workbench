"""简报排序：把你的 1-10 评分变成推送权重，同时不让它长成茧房。

## 上一版错在哪（2026-11-07 实测，n=12）

收缩目标写死 5.5 分，但人只会给自己在意的东西打分 —— 你的 12 条均分是 7.67。
于是 `_bayes` 把每条都往下拽：一个你给 10 分的来源，贝叶斯均分 6.62，
**低于**全站均分 7.67，权重算出来是 **-0.33**；而「从没评过的来源」拿 +0.60 探索加成。
也就是说：你越认真评分，你夸过的来源越吃亏。符号是反的。

这不是参数没调好，是两个假设错了：

1. **假设评分能直接用绝对值比较。** 不能。你没评过的东西没有分数，
   只有「相对你自己的均值偏高还是偏低」才有意义。
2. **假设评分少的时候就该关掉个性化。** 不对。样本少该做的是**收缩得更狠**、
   让每条评分的力气正比于证据量，而不是一刀切 `n < 12 全关`。

## 现在怎么算

三层收缩（partial pooling），每层都往上一层靠：

    来源 → 它所属分类的均值 → 你自己的总均值 → 5.5 的绝对先验

权重乘 `n/(n+k)`：评 1 条只出 20% 的力气，评 4 条出一半。
所以「评一条就翻天」和「评了也没用」都不会发生。

学习的**主对象是主题，不是媒体**。看你的 10 分：路透、格隆汇、财联社、Bloomberg——
四家不同媒体，共同点是「AI 与资本/安全」；两家 5 分是能源论坛和锂电产业链。
按来源学，学到的是「财联社好、中新网差」，那正是茧房的形状：
以后中新网写 AI 也会被压下去。按主题学才是「我想看什么」。

## 防茧房不靠权重，靠配额

- 每天固定留 2 个**探索位**，完全不看个性化分，只按内容相关度 + 时效，
  优先给「没评过的来源」和「今天还没出现过的分类」；
- 同来源 ≤2、同分类 ≤4；
- 来源权重封顶 ±0.55：媒体品牌不是兴趣，不能靠它决定推送。

权重会被一次情绪化的评分带偏，配额不会。这是唯一可靠的做法。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from pathlib import Path

import store

FEEDBACK_PATH: Path = store.DATA_DIR / "feedback.jsonl"
# 点开抽屉＝弱正向信号，单独存。不混进 feedback.jsonl：
# 那个文件是「我打的分数」，应该只有我说过的话。
OPEN_PATH: Path = store.DATA_DIR / "opens.jsonl"
STATS_PATH: Path = store.CACHE_DIR / "rank-stats.json"

# ---- 先验与收缩强度 ----
PRIOR_C = 5.5          # 绝对先验：一条评分都没有时，只能假设「中性」
PRIOR_USER = 4.0       # 个人总均值的先验等效条数
K_CAT = 3.0            # 分类均值向个人均值收缩的强度
K_SRC = 4.0            # 来源均值向分类均值收缩的强度（正面证据：要多几条才算数）
K_SRC_BAD = 1.5        # 负面证据收缩得慢：一条「不想看」比一条「想看」可靠得多
K_KW = 2.0             # 主题偏差向 0 收缩的强度
SCALE = 2.5            # 0-10 分制里「一个标准差」，用来把分差换成无量纲的 z

# ---- 各维度权重（z 的系数）----
W_SRC = 1.6
W_CAT = 1.1
W_KW = 1.0
SRC_CAP = 0.55         # 来源维度封顶：品牌不该压过主题
KW_CAP = 1.4           # 主题维度封顶：避免一条标题里堆满高分词就飞起来

HALF_LIFE_H = 18.0     # 候选本身的时效衰减（小时）
RATE_HALF_LIFE_D = 60  # 评分本身的遗忘半衰期（天）：偏好会变
MIN_FEEDBACK = 3       # 有 3 条就开始用（收缩会自己控制力度），不是 12
EXPLORE_SLOTS = 2      # 每天给陌生来源留几个位子
OPEN_WEIGHT = 0.3      # 一次点开的证据量（≈ 三分之一条评分）
OPEN_LIFT = 0.8        # 点开 ≈「比我的平均兴趣略高一点」

_STOP = set("""
我们 你们 他们 这个 那个 什么 怎么 为什么 可以 已经 表示 认为 目前 今天 昨天
以及 但是 因为 所以 如果 这样 那样 一个 一些 没有 不是 就是 还是 可能 需要
进行 通过 发布 宣布 记者 报道 消息 相关 方面 情况 问题 工作 中国 国家 全球
""".split())


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).strip().lower()


def _day() -> str:
    return _now().date().isoformat()


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _parse_at(at) -> dt.datetime | None:
    s = str(at or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10:
            t = dt.datetime.fromisoformat(s).replace(tzinfo=_now().tzinfo)
        else:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_now().tzinfo)
        return t
    except Exception:
        return None


# ------------------------------------------------------------------ 记录

# 标题 -> 最近一次评分。界面要回显「这条我打过几分」：
# 不回显的话，用户评完刷一下就再也看不到自己评过什么，评分变成黑洞。
_RATED_CACHE: dict | None = None
_RATED_MTIME = 0.0


def record(item: dict, score: int, note: str = "") -> dict:
    """记一条评分。score=0 表示「不感兴趣」，10 表示「非常想看」。

    追加写 jsonl，一行一条。不做去重 —— 同一条改主意了要留痕，
    统计时取最后一条（见 _latest）。
    """
    global _RATED_CACHE
    score = max(0, min(10, int(score)))
    row = {
        "at": _now().isoformat(timespec="seconds"),
        "title": str(item.get("title") or "")[:200],
        "source": str(item.get("source") or "")[:60],
        "cat": str(item.get("cat") or "")[:20],
        "url": str(item.get("url") or "")[:300],
        "score": score,
        "note": str(note or "")[:200],
    }
    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FEEDBACK_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    # 统计是纯函数，重算一次最省事；数据量（几千行）重算不到 10ms
    _RATED_CACHE = None
    _rebuild()
    return {"ok": True, "row": row, "total": _load_cache().get("n", 0)}


def record_open(item: dict) -> dict:
    """记一次「点开看详情」。不当作评分，只算弱证据（OPEN_WEIGHT 条）。

    同一条只记第一次：反复点开同一篇不该越刷越像「我很喜欢」。
    评过分的也不记 —— 说过的话比点开重要，别重复计一遍。
    """
    title = str(item.get("title") or "").strip()
    if not title or score_of(title) is not None:
        return {"ok": True, "skipped": "已评分"}
    seen = {_norm(o.get("title"))[:80] for o in _opens()}
    if _norm(title)[:80] in seen:
        return {"ok": True, "skipped": "已记过"}
    row = {
        "at": _now().isoformat(timespec="seconds"),
        "title": title[:200],
        "source": str(item.get("source") or "")[:60],
        "cat": str(item.get("cat") or "")[:20],
    }
    OPEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OPEN_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _rebuild()
    return {"ok": True}


def rated_map() -> dict:
    """标题（归一化后截 80 字）-> {"score", "at"}。按文件 mtime 缓存。"""
    global _RATED_CACHE, _RATED_MTIME
    if not FEEDBACK_PATH.exists():
        return {}
    try:
        m = FEEDBACK_PATH.stat().st_mtime
    except OSError:
        return {}
    if _RATED_CACHE is not None and m == _RATED_MTIME:
        return _RATED_CACHE
    out: dict[str, dict] = {}
    for row in _latest():
        key = _norm(row.get("title"))[:80]
        if key:
            out[key] = {"score": int(row.get("score") or 0), "at": str(row.get("at") or "")}
    _RATED_CACHE = out
    _RATED_MTIME = m
    return out


def score_of(title: str) -> int | None:
    """这条给过几分。没评过返回 None（0 分是「不感兴趣」，不返回 0）。"""
    hit = rated_map().get(_norm(title)[:80])
    return None if hit is None else int(hit["score"])


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _latest() -> list[dict]:
    """同一标题只保留最后一次评分。"""
    last: dict[str, dict] = {}
    for row in _read_jsonl(FEEDBACK_PATH):
        key = _norm(row.get("title"))[:80]
        if key:
            last[key] = row
    return list(last.values())


def _opens() -> list[dict]:
    """点开记录，同标题只留第一次。"""
    first: dict[str, dict] = {}
    for row in _read_jsonl(OPEN_PATH):
        key = _norm(row.get("title"))[:80]
        if key and key not in first:
            first[key] = row
    return list(first.values())


# ------------------------------------------------------------------ 统计

_STATS: dict | None = None
_STATS_MTIME = 0.0


def _load_cache() -> dict:
    global _STATS, _STATS_MTIME
    if not STATS_PATH.exists():
        return _STATS or {}
    m = STATS_PATH.stat().st_mtime
    if _STATS is not None and m == _STATS_MTIME:
        return _STATS
    try:
        _STATS = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        _STATS_MTIME = m
    except Exception:
        _STATS = _STATS or {}
    return _STATS


def _words(title: str) -> list[str]:
    """中文没有空格，用 2-4 字滑窗当词。

    粗暴但够用：真正的分词要词典，而这里只需要「哪几个字常出现在我喜欢的标题里」。
    噪音靠「至少出现在 2 条评分里」+ 收缩到 0 过滤掉。
    """
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", str(title or ""))
    out = []
    for seg in s.split():
        if re.fullmatch(r"[A-Za-z0-9]+", seg):
            if len(seg) >= 2:
                out.append(seg.lower())
            continue
        for n in (2, 3, 4):
            for i in range(len(seg) - n + 1):
                w = seg[i:i + n]
                if w in _STOP:
                    continue
                out.append(w)
    return out


def _decay(at) -> float:
    """评分的「新鲜度」权重：60 天前的评分只有一半力气，120 天前四分之一。

    偏好会变。半年前你喜欢的东西不该和上周同权。
    """
    t = _parse_at(at)
    if t is None:
        return 0.6                       # 没时间戳的旧记录，给个中间值
    days = max(0.0, (_now() - t).total_seconds() / 86400.0)
    return max(0.12, 0.5 ** (days / RATE_HALF_LIFE_D))


def _aggregate(rows: list[dict], opens: list[dict] | None = None) -> dict:
    """纯函数：一堆评分（+点开记录）-> 各维度均值。不碰文件，方便回测。

    三层收缩：来源 → 它所属分类 → 个人总均值。
    """
    recs: list[tuple[float, float, str, str, set]] = []
    for r in rows:
        s = float(r.get("score") or 0)
        recs.append((s, _decay(r.get("at")), str(r.get("source") or ""),
                     str(r.get("cat") or ""), set(_words(r.get("title") or ""))))

    sw = sum(w for _, w, *_ in recs)
    sws = sum(s * w for s, w, *_ in recs)
    # 个人总均值：自己评分的中心，只往 5.5 轻轻靠（先验 4 条）
    mu_user = (sws + PRIOR_USER * PRIOR_C) / (sw + PRIOR_USER) if sw else PRIOR_C

    # 点开：弱证据，伪分数 = 我的均值 + 一点点。它不改 mu_user（那是评分的事），
    # 只给来源/分类/主题加一点薄薄的证据。
    pseudo: list[tuple[float, float, str, str, set]] = []
    for o in (opens or []):
        pseudo.append((mu_user + OPEN_LIFT, OPEN_WEIGHT * _decay(o.get("at")),
                       str(o.get("source") or ""), str(o.get("cat") or ""),
                       set(_words(o.get("title") or ""))))
    allrec = recs + pseudo

    cat_acc: dict[str, list[float]] = {}
    src_acc: dict[str, list[float]] = {}
    src_cat: dict[str, str] = {}          # 来源主要发在哪个分类（收缩的目标）
    kw_acc: dict[str, list[float]] = {}
    kw_n: dict[str, int] = {}
    for s, w, src, cat, words in allrec:
        if cat:
            a = cat_acc.setdefault(cat, [0.0, 0.0, 0.0])   # Σw, Σw·s, 条数
            a[0] += w
            a[1] += w * s
            a[2] += 1
        if src:
            a = src_acc.setdefault(src, [0.0, 0.0, 0.0])   # Σw, Σw·s, 条数
            a[0] += w
            a[1] += w * s
            a[2] += 1
            if cat and src not in src_cat:
                src_cat[src] = cat
        for word in words:
            a = kw_acc.setdefault(word, [0.0, 0.0])        # Σw, Σw·(s-μ)
            a[0] += w
            a[1] += w * (s - mu_user)
            kw_n[word] = kw_n.get(word, 0) + 1

    cats: dict[str, dict] = {}
    for c, (w, ws, n) in cat_acc.items():
        mu = (ws + K_CAT * mu_user) / (w + K_CAT)
        cats[c] = {"mu": round(mu, 3), "conf": round(w / (w + K_CAT), 3), "n": int(n)}

    # 来源收缩到「它自己那个分类」的均值 —— 财经号的均值不该被拿去和学术号比
    srcs: dict[str, dict] = {}
    for s, (w, ws, n) in src_acc.items():
        cat = src_cat.get(s) or ""
        target = (cats.get(cat) or {}).get("mu", mu_user)
        mu = (ws + K_SRC * target) / (w + K_SRC)
        srcs[s] = {"mu": round(mu, 3),
                   # 正面/负面各一个置信度：说「不想看」比说「想看」更可信，
                   # 所以负面证据用更小的 k，同样条数下权重更高。
                   "conf": round(w / (w + K_SRC), 3),
                   "confNeg": round(w / (w + K_SRC_BAD), 3),
                   "n": int(n), "cat": cat}

    # 主题：只看「相对我个人均值的偏差」，n>=2 才留，收缩到 0
    kws: dict[str, float] = {}
    for word, (w, wdev) in kw_acc.items():
        if kw_n.get(word, 0) < 2:
            continue
        kws[word] = round(wdev / (w + K_KW), 3)
    top_kw = dict(sorted(kws.items(), key=lambda kv: -abs(kv[1]))[:60])

    return {
        "n": len(rows),
        "nOpen": len(pseudo),
        "muUser": round(mu_user, 3),
        "mean": round(sws / sw, 2) if sw else PRIOR_C,
        "sources": srcs,
        "cats": cats,
        "keywords": top_kw,
        "builtAt": _now().isoformat(timespec="seconds"),
        "feedbackMtime": _feedback_mtime(),
        "opensMtime": _opens_mtime(),
    }


def _rebuild() -> dict:
    st = _aggregate(_latest(), _opens())
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = STATS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATS_PATH)
    except Exception:
        pass
    global _STATS
    _STATS = st
    return st


def _feedback_mtime() -> float:
    try:
        return FEEDBACK_PATH.stat().st_mtime if FEEDBACK_PATH.exists() else 0.0
    except OSError:
        return 0.0


def _opens_mtime() -> float:
    try:
        return OPEN_PATH.stat().st_mtime if OPEN_PATH.exists() else 0.0
    except OSError:
        return 0.0


def stats(force: bool = False) -> dict:
    """统计。文件不存在就重算一次，之后靠 mtime 缓存。

    两个文件都要比对：评分变了要点开记录变了都得重算。
    缓存说「评了 8 条」而文件已经没了，界面就会一直显示 8 条 ——
    重算一次比解释这个便宜。
    """
    if force or not STATS_PATH.exists():
        return _rebuild()
    cache = _load_cache()
    prev = cache.get("feedbackMtime")
    # 没有这个字段＝旧版本算的缓存，直接重算一次（顺便补上字段）
    if prev is None or abs(float(prev) - _feedback_mtime()) > 1e-6:
        return _rebuild()
    if "muUser" not in cache:            # 老结构，重新算
        return _rebuild()
    if abs(float(cache.get("opensMtime") or 0.0) - _opens_mtime()) > 1e-6:
        return _rebuild()
    return cache


# ------------------------------------------------------------------ 打分

def _age_hours(item: dict) -> float:
    t = _parse_at(item.get("at"))
    if t is None:
        return 24.0                      # 没有时间就当一天前
    return max(0.0, (_now() - t).total_seconds() / 3600.0)


def _fresh(item: dict) -> float:
    return math.exp(-_age_hours(item) / HALF_LIFE_H * 0.693)


def _base_score(item: dict, base: float) -> float:
    """不看个性化的分：内容相关度 + 时效。探索位就按这个排。"""
    return base + 1.2 * _fresh(item)


def explain(item: dict, base: float = 0.0) -> dict:
    """给一条资讯打分，并说清为什么。

    `base` 是调用方原有的「内容相关性」分（关键词命中、分类权重等）。
    个性化分只是叠加在它上面，不替换 —— 否则一接上评分，
    之前那套「按兴趣词和分类轮换」的经验就全丢了。
    """
    return _item_score(item, stats(), base)


def _item_score(item: dict, st: dict, base: float = 0.0,
                use_known: bool = True) -> dict:
    """纯函数版：统计结果由外面传进来。回测（tools/rank_eval.py）要用。

    use_known=False 关掉「按这条自己的评分微调」——回测时必须关：
    留一法把某条藏起来，如果还能读到它的真实评分，就是作弊。
    """
    n = int(st.get("n") or 0)
    if n < MIN_FEEDBACK:
        # 冷启动：只按时间排，别装作用户画像很准
        return {"score": _base_score(item, base), "reasons": [], "cold": True}

    mu = float(st.get("muUser") or PRIOR_C)
    total = base
    reasons: list[str] = []

    # ① 来源：封顶 ±0.55。品牌只是弱证据，压过它的是主题。
    src = str(item.get("source") or "")
    g = (st.get("sources") or {}).get(src)
    if g:
        z = _clamp((float(g["mu"]) - mu) / SCALE, -2.0, 2.0)
        conf = float(g.get("confNeg") if z < 0 else g.get("conf") or 0.0)
        add = _clamp(W_SRC * z * conf, -SRC_CAP, SRC_CAP)
        total += add
        if abs(add) >= 0.12:
            reasons.append(f"{src}：你评过的 {g['n']} 条均分 {g['mu']:.1f}"
                           + ("，比你的平均高" if add > 0 else "，比你的平均低"))

    # ② 分类
    cat = str(item.get("cat") or "")
    c = (st.get("cats") or {}).get(cat)
    if c:
        z = _clamp((float(c["mu"]) - mu) / SCALE, -2.0, 2.0)
        add = W_CAT * z * float(c["conf"])
        total += add
        if abs(add) >= 0.15:
            reasons.append(f"{cat}：均分 {c['mu']:.1f}（{c['n']} 条）")

    # ③ 主题：取标题里偏差最大的那一个词。
    #    用 max 而不是求和 —— 2-4 字滑窗对同一个短语会产生「基建 / AI基 / I基建」，
    #    相加等于把一句话数了三遍，一条标题就能把分顶到天花板。
    best = None
    kw = st.get("keywords") or {}
    for w in set(_words(item.get("title") or "")):
        d = kw.get(w)
        if d is None:
            continue
        if best is None or abs(float(d)) > abs(float(best[1])):
            best = (w, float(d))
    if best:
        add = _clamp(W_KW * best[1] / SCALE, -KW_CAP, KW_CAP)
        total += add
        if abs(add) >= 0.12:
            reasons.append(f"主题「{best[0]}」：你在这类标题上平均"
                           + ("打得更低" if add < 0 else "打得更高"))

    # ④ 时效
    age = _age_hours(item)
    fresh = _fresh(item)
    total += 1.2 * fresh
    if age < 6:
        reasons.append(f"{age:.0f} 小时内的新消息")

    # ⑤ 这一条自己已经被打过分的，直接按那次的分微调。
    #    刚抓来的新条目还没评分，所以这一项不影响「选哪些」，
    #    只让「我打了 10 分 → 下次加载它就上去了」这件事看得见。
    #    没有它，界面会出现「5 分排在 8 分前面」——哪怕背后是分类偏好在起作用，
    #    看起来也像排序坏了。
    known = score_of(item.get("title") or "") if use_known else None
    if known is not None:
        add = 0.7 * _clamp((known - mu) / SCALE, -1.2, 1.2)
        total += add
        reasons.append(f"你自己给过 {known} 分")

    return {"score": round(total, 4), "reasons": reasons, "cold": False}


# ------------------------------------------------------------------ 重排

def active() -> bool:
    """个性化是否已生效（评分数够不够）。"""
    return int(stats().get("n") or 0) >= MIN_FEEDBACK


def explore_slots(limit: int) -> int:
    """今天留几个探索位。太短的列表（<6 条）不切，切了就没内容了。"""
    if limit < 6:
        return 0
    return min(EXPLORE_SLOTS, max(1, limit // 4))


def rerank(pool: list[dict], limit: int, base: dict | None = None) -> list[dict]:
    """个性化重排 + 配额式探索 + 硬性多样性约束。

    第一遍按个性化分选 `limit - 探索位` 条（同来源 ≤2、同分类 ≤4）；
    第二遍从剩下的里挑探索位：**不看个性化分**，只按内容相关度 + 时效，
    优先「没评过的来源」和「今天还没出现过的分类」。
    """
    if not pool:
        return []
    base = base or {}
    st = stats()
    cold = int(st.get("n") or 0) < MIN_FEEDBACK
    rated_src = set((st.get("sources") or {}).keys())

    scored = []
    for i, p in enumerate(pool):
        t = str(p.get("title") or "")
        e = explain(p, float(base.get(t, 0.0)))
        scored.append({"score": e["score"], "i": i, "p": p, "reasons": e["reasons"]})
    scored.sort(key=lambda x: (-x["score"], x["i"]))

    reserve = 0 if cold else explore_slots(limit)
    main_quota = max(0, limit - reserve)

    out: list[dict] = []
    src_n: dict[str, int] = {}
    cat_n: dict[str, int] = {}

    def take(item: dict) -> None:
        out.append(item)
        s = str(item.get("source") or "")
        c = str(item.get("cat") or "")
        src_n[s] = src_n.get(s, 0) + 1
        cat_n[c] = cat_n.get(c, 0) + 1

    for t in scored:
        if len(out) >= main_quota:
            break
        p = t["p"]
        s = str(p.get("source") or "")
        c = str(p.get("cat") or "")
        if src_n.get(s, 0) >= 2 or cat_n.get(c, 0) >= 4:
            continue
        take(p)

    # 探索位：完全不看个性化分。先给「没评过的来源」，再给「今天还没出现的分类」，
    # 两个都不占就按内容相关度补 —— 但排到列表末尾，并且在界面上标出来。
    if reserve:
        picked = {id(p) for p in out}
        cats_now = set(cat_n.keys())
        cand = [t for t in scored if id(t["p"]) not in picked]

        def key(t):
            p = t["p"]
            unseen_src = 0 if str(p.get("source") or "") in rated_src else 1
            new_cat = 0 if str(p.get("cat") or "") in cats_now else 1
            b = _base_score(p, float(base.get(str(p.get("title") or ""), 0.0)))
            return (-unseen_src, -new_cat, -b, t["i"])

        for t in sorted(cand, key=key):
            if len(out) >= limit:
                break
            p = t["p"]
            s = str(p.get("source") or "")
            c = str(p.get("cat") or "")
            if src_n.get(s, 0) >= 2 or cat_n.get(c, 0) >= 4:
                continue
            take(p)

    # 不够 limit 时放宽约束补满（宁可重复，也别给用户半页）。
    # 分两档松：先放宽到 3 / 6，还差才彻底放开 —— 一上来就全放开，
    # 池子小的时候会变成「同一个来源占掉半页」。
    if len(out) < limit:
        for src_cap, cat_cap in ((3, 6), (99, 99)):
            if len(out) >= limit:
                break
            for t in scored:
                if len(out) >= limit:
                    break
                p = t["p"]
                if p in out:
                    continue
                s = str(p.get("source") or "")
                c = str(p.get("cat") or "")
                if src_n.get(s, 0) >= src_cap or cat_n.get(c, 0) >= cat_cap:
                    continue
                take(p)
    return out[:limit]


def summary() -> dict:
    """给前端看的一句话：现在算法在按什么排。"""
    st = stats()
    n = int(st.get("n") or 0)
    nopen = int(st.get("nOpen") or 0)
    if n < MIN_FEEDBACK:
        return {"active": False, "n": n, "need": MIN_FEEDBACK,
                "text": f"已评 {n} 条，满 {MIN_FEEDBACK} 条后开始按你的偏好排序（探索位一直保留）"}
    top_kw = sorted((st.get("keywords") or {}).items(), key=lambda kv: -kv[1])[:3]
    kw_txt = "、".join(k for k, v in top_kw[:2] if abs(float(v)) >= 0.1)
    txt = "按你评过的 " + str(n) + " 条选稿排序"
    if kw_txt:
        txt += "，主题最看重：" + kw_txt
    if nopen:
        txt += f"（另有 {nopen} 次点开算弱信号）"
    txt += f"　·　每天留 {EXPLORE_SLOTS} 个探索位"
    return {"active": True, "n": n, "nOpen": nopen, "need": MIN_FEEDBACK, "text": txt}
