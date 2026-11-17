"""待办与定时提醒的规则层：时间怎么算、怎么算「到点」、界面该显示什么字。

为什么单开一层：主窗和「待办小窗」是两个页面、两个进程里的同一份数据，
如果"到点"的判断各写一份，迟早会出现「小窗弹了、主窗又弹一次」。
所以规则只在这里写一次，两边都只问这里要结果。
"""

from __future__ import annotations

import datetime as _dt

# 提醒最远能约到多久之后。
# 提醒是本地扫描线程触发的（不依赖 Windows 计划任务），所以真正的硬限制只有
# 「datetime 能表示 + 界面能选」；取 10 年 —— 再远 datetime 仍能存，但那条提醒
# 早已失去意义，且跨年份的配置迁移更容易出问题。默认不额外收窄（= 上限）。
HORIZON_MAX = 24 * 366 * 10      # 10 年，系统实际能表示与触发的最大跨度
HORIZON_HOURS = HORIZON_MAX
# 过了这个点还没报出去的提醒，标成「补发」——机器刚睡醒，别装作准时
LATE_AFTER_MINUTES = 120
# 再久（一天以上）就别吵了：直接销账，不弹窗
SILENT_AFTER_HOURS = 24

FMT = "%Y-%m-%dT%H:%M"


def now() -> _dt.datetime:
    return _dt.datetime.now().replace(microsecond=0)


def fmt(when: _dt.datetime) -> str:
    return when.strftime(FMT)


def parse(raw) -> _dt.datetime | None:
    """把配置里的字符串读成 datetime。坏值一律返回 None，不抛异常。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        got = _dt.datetime.fromisoformat(text)
    except ValueError:
        for pattern in (FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                got = _dt.datetime.strptime(text[:len(text)], pattern)
                break
            except ValueError:
                continue
        else:
            return None
    return got.replace(microsecond=0, tzinfo=None)


def horizon_hours(cfg: dict | None = None) -> int:
    """提醒能约多远。配置里写大了也不认 —— 上限是 HORIZON_MAX（10 年）。"""
    try:
        want = int(((cfg or {}).get("reminder") or {}).get("horizon_hours") or HORIZON_HOURS)
    except (TypeError, ValueError):
        want = HORIZON_HOURS
    return max(1, min(HORIZON_MAX, want))


def clamp(raw, cfg: dict | None = None) -> tuple[_dt.datetime | None, str]:
    """校验用户给的时间。返回 (时间, 错误说明)。

    规则：不能是过去（给 60 秒容差，免得手点慢一点就被拒），
    最远不超过 horizon_hours（默认 = 72 小时）。
    """
    when = parse(raw)
    if when is None:
        return None, "时间格式看不懂，要 2026-10-22T09:30 这样"
    ref = now()
    if when < ref - _dt.timedelta(seconds=60):
        return None, "这个时间已经过去了"
    limit = ref + _dt.timedelta(hours=horizon_hours(cfg))
    if when > limit:
        return None, f"最多只能约到 {horizon_hours(cfg)} 小时后（{fmt(limit)}）"
    return when, ""


def next_upcoming(items: list[dict], limit: int = 3) -> list[dict]:
    """接下来会响的几条（未完成、有提醒、还没响过）。"""
    rows = []
    for t in (items or []):
        if not isinstance(t, dict) or t.get("done") or t.get("reminded"):
            continue
        when = parse(t.get("remindAt"))
        if when is None:
            continue
        rows.append((when, t))
    rows.sort(key=lambda pair: pair[0])
    return [dict(t, remindAt=fmt(when)) for when, t in rows[:limit]]


def collect_due(items: list[dict], moment: _dt.datetime | None = None) -> tuple[list[dict], list[dict]]:
    """扫一遍到点的提醒。

    返回 (要报的, 直接销账的)：
      - 要报的：时间到了、没完成、还没报过 —— 顺手把 reminded 标上；
      - 直接销账的：过期一天以上（关机/睡眠太久），标上 reminded 但不弹，
        否则一开机会有十条陈年提醒一起炸。
    """
    ref = moment or now()
    fire: list[dict] = []
    silent: list[dict] = []
    for t in (items or []):
        if not isinstance(t, dict) or t.get("done") or t.get("reminded"):
            continue
        when = parse(t.get("remindAt"))
        if when is None or when > ref:
            continue
        late_minutes = (ref - when).total_seconds() / 60.0
        t["reminded"] = True
        t["remindedAt"] = fmt(ref)
        if late_minutes > SILENT_AFTER_HOURS * 60:
            silent.append(t)
            continue
        fire.append(dict(t, late=late_minutes > LATE_AFTER_MINUTES,
                         lateMinutes=int(late_minutes)))
    return fire, silent


# --------------------------------------------------------------- 人间的话

def _hm(when: _dt.datetime) -> str:
    return when.strftime("%H:%M")


def pretty(raw, moment: _dt.datetime | None = None) -> str:
    """一条提醒该显示成什么：12 分钟后 / 今天 21:30 / 明天 09:00 / 10月23日 09:00。"""
    when = parse(raw)
    if when is None:
        return ""
    ref = moment or now()
    delta = (when - ref).total_seconds()
    if delta < -60:
        return _hm(when) + "（已过）"
    if delta < 60:
        return "就是现在"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟后"
    today = ref.date()
    if when.date() == today:
        return "今天 " + _hm(when)
    if when.date() == today + _dt.timedelta(days=1):
        return "明天 " + _hm(when)
    if when.date() == today + _dt.timedelta(days=2):
        return "后天 " + _hm(when)
    return f"{when.month}月{when.day}日 {_hm(when)}"


def summary(items: list[dict]) -> dict:
    """给界面的一行统计：几条、还剩几条、有没有提醒在路上。"""
    total = len(items or [])
    left = sum(1 for t in (items or []) if isinstance(t, dict) and not t.get("done"))
    armed = sum(1 for t in (items or [])
                if isinstance(t, dict) and t.get("remindAt") and not t.get("reminded")
                and not t.get("done"))
    return {"total": total, "left": left, "armed": armed}
