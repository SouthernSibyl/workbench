"""回测：排序到底有没有把「你会打高分的」排到前面。

做法是**留一法**：把某一条的评分藏起来，用其余评分重新训练，
再让它给当天的十条打分，看被藏起来的那条排到第几。

为什么要藏：`rank` 里有一个「已评分条目按那次的分微调」的项，
不藏的话等于考试时把答案摆在桌上，回测结果会虚高。

用法：
    python tools/rank_eval.py           # 摘要
    python tools/rank_eval.py -v        # 逐条明细

数据从 `%APPDATA%\\Workbench\\cache\\daily-*.json`（每天的十条）
和 `feedback.jsonl`（你打的分）里读。评分少于 10 条时，结论只能当方向看 ——
十几条样本分不出 0.05 的差别，这是数学，不是谦辞。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import rank      # noqa: E402
import store     # noqa: E402

MIN_RATED_PER_DAY = 3      # 一天里至少评过几条才拿来当考卷


def load_days() -> list[dict]:
    """读每天的十条，附上你打过的分（没评过是 None）。"""
    days = []
    for p in sorted(store.CACHE_DIR.glob("daily-*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = ((d.get("news") or {}).get("items") or [])
        if items:
            days.append({"date": p.stem.replace("daily-", ""), "items": items})
    return days


def attach_scores(days: list[dict]) -> list[dict]:
    rm = rank.rated_map()
    out = []
    for d in days:
        rows = []
        for it in d["items"]:
            key = rank._norm(it.get("title") or "")[:80]
            hit = rm.get(key)
            rows.append(dict(it, _score=(hit["score"] if hit else None)))
        rated = [r for r in rows if r["_score"] is not None]
        if len(rated) >= MIN_RATED_PER_DAY:
            out.append({"date": d["date"], "items": rows, "rated": len(rated)})
    return out


def spearman(pairs: list[tuple[float, float]]) -> float | None:
    """两个向量的秩相关，并列按平均秩处理。"""
    if len(pairs) < 3:
        return None

    def ranks(vals):
        idx = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and vals[idx[j + 1]] == vals[idx[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r

    xs = ranks([p[0] for p in pairs])
    ys = ranks([p[1] for p in pairs])
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = sum((a - mx) ** 2 for a in xs) ** 0.5
    dy = sum((b - my) ** 2 for b in ys) ** 0.5
    if not dx or not dy:
        return None
    return num / (dx * dy)


def evaluate(days: list[dict], verbose: bool = False) -> dict:
    rows_all = rank._latest()
    res = {"places": [], "rho": [], "top3": [0, 0], "detail": [], "orig": []}

    for d in days:
        items = d["items"]
        for held in [r for r in items if r["_score"] is not None]:
            hkey = rank._norm(held.get("title") or "")[:80]
            train = [r for r in rows_all
                     if rank._norm(r.get("title") or "")[:80] != hkey]
            st = rank._aggregate(train, [])
            scored = [rank._item_score(dict(it), st, 0.0, use_known=False)["score"]
                      for it in items]

            truth = [it["_score"] for it in items]
            order = sorted(range(len(items)), key=lambda i: -scored[i])
            place = order.index(items.index(held)) + 1
            res["places"].append(place)
            res["orig"].append(items.index(held) + 1)

            pairs = [(scored[i], truth[i]) for i in range(len(items))
                     if truth[i] is not None and items[i] is not held]
            rho = spearman(pairs)
            if rho is not None:
                res["rho"].append(rho)

            high = [i for i in range(len(items))
                    if truth[i] is not None and truth[i] >= 8]
            if high:
                res["top3"][0] += sum(1 for i in high if i in order[:3])
                res["top3"][1] += len(high)

            res["detail"].append({
                "date": d["date"], "score": held["_score"], "place": place,
                "was": items.index(held) + 1,
                "title": (held.get("title") or "")[:24],
            })
            if verbose:
                print(f"  {d['date']} {held['_score']:>2}分  "
                      f"原标题第 {items.index(held) + 1} 位 → 排序后第 {place} 位  "
                      f"{held.get('title', '')[:22]}")
    return res


def main() -> None:
    days = attach_scores(load_days())
    if not days:
        print("没有可回测的日子：需要每天的缓存里至少有 "
              f"{MIN_RATED_PER_DAY} 条是你打过分的。")
        print("先正常用几天、顺手给几条打分，再回来跑这个。")
        return

    print("可回测的日子：")
    for d in days:
        print(f"  {d['date']}  {len(d['items'])} 条，其中 {d['rated']} 条评过分")

    res = evaluate(days, "-v" in sys.argv)
    places, orig = res["places"], res["orig"]
    n = len(places)
    avg = sum(places) / n
    base = sum(orig) / n
    rho = sum(res["rho"]) / len(res["rho"]) if res["rho"] else None
    hit, total = res["top3"]

    print(f"\n留一法：{n} 条被藏起来过的评分\n")
    print(f"  被藏那条的平均名次    排序后 {avg:5.2f}   （随机 = 5.50，原标题 {base:5.2f}）")
    if rho is not None:
        print(f"  分数与名次的秩相关    {rho:+.2f}   （1 = 完全一致，0 = 无关）")
    if total:
        print(f"  高分(≥8)进前 3 的比例  {hit}/{total}    （随机 = {total * 0.3:.0f}/{total}）")
    print("\n  样本只有十几条时，这些数字只能看方向，不能当显著性。")
    print("  真正该看的是：平均名次有没有明显低于 5.5、秩相关是不是正的。")


if __name__ == "__main__":
    main()
