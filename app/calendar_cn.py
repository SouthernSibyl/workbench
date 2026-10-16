"""日期 / 节日 / 节气 / 纪念日。

- 公历固定节日：内嵌表
- 二十四节气：内嵌 2025-2027 精确日期
- 用户自定义纪念日：来自配置
配图主题色（palette）会在美术素材就位后被替换为真实插画。
"""

from __future__ import annotations

import datetime as _dt

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 公历节日（月, 日）-> 名称
SOLAR_FESTIVALS: dict[tuple[int, int], str] = {
    (1, 1): "元旦",
    (1, 10): "中国人民警察节",
    (2, 14): "情人节",
    (3, 8): "国际妇女节",
    (3, 12): "植树节",
    (4, 1): "愚人节",
    (5, 1): "国际劳动节",
    (5, 4): "青年节",
    (5, 12): "国际护士节",
    (6, 1): "国际儿童节",
    (7, 1): "建党节",
    (8, 1): "建军节",
    (9, 3): "中国人民抗日战争胜利纪念日",
    (9, 10): "教师节",
    (10, 1): "国庆节",
    (10, 24): "程序员节",
    (11, 11): "双十一",
    (12, 13): "国家公祭日",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
    (12, 31): "跨年夜",
}

# 二十四节气精确日期（年 -> [(月, 日, 名称)]）
SOLAR_TERMS: dict[int, list[tuple[int, int, str]]] = {
    2025: [
        (1, 5, "小寒"), (1, 20, "大寒"), (2, 3, "立春"), (2, 18, "雨水"),
        (3, 5, "惊蛰"), (3, 20, "春分"), (4, 4, "清明"), (4, 20, "谷雨"),
        (5, 5, "立夏"), (5, 21, "小满"), (6, 5, "芒种"), (6, 21, "夏至"),
        (7, 7, "小暑"), (7, 22, "大暑"), (8, 7, "立秋"), (8, 23, "处暑"),
        (9, 7, "白露"), (9, 23, "秋分"), (10, 8, "寒露"), (10, 23, "霜降"),
        (11, 7, "立冬"), (11, 22, "小雪"), (12, 7, "大雪"), (12, 21, "冬至"),
    ],
    2026: [
        (1, 5, "小寒"), (1, 20, "大寒"), (2, 4, "立春"), (2, 18, "雨水"),
        (3, 5, "惊蛰"), (3, 20, "春分"), (4, 5, "清明"), (4, 20, "谷雨"),
        (5, 5, "立夏"), (5, 21, "小满"), (6, 5, "芒种"), (6, 21, "夏至"),
        (7, 7, "小暑"), (7, 23, "大暑"), (8, 7, "立秋"), (8, 23, "处暑"),
        (9, 7, "白露"), (9, 23, "秋分"), (10, 8, "寒露"), (10, 23, "霜降"),
        (11, 7, "立冬"), (11, 22, "小雪"), (12, 7, "大雪"), (12, 22, "冬至"),
    ],
    2027: [
        (1, 5, "小寒"), (1, 20, "大寒"), (2, 4, "立春"), (2, 19, "雨水"),
        (3, 6, "惊蛰"), (3, 21, "春分"), (4, 5, "清明"), (4, 20, "谷雨"),
        (5, 6, "立夏"), (5, 21, "小满"), (6, 6, "芒种"), (6, 21, "夏至"),
        (7, 7, "小暑"), (7, 23, "大暑"), (8, 8, "立秋"), (8, 23, "处暑"),
        (9, 8, "白露"), (9, 23, "秋分"), (10, 8, "寒露"), (10, 24, "霜降"),
        (11, 7, "立冬"), (11, 22, "小雪"), (12, 7, "大雪"), (12, 22, "冬至"),
    ],
}

# 节日 / 节气 -> 默认配图配色（美术素材到位前用它生成渐变底图）
TERM_PALETTE: dict[str, tuple[str, str]] = {
    "小寒": ("#eaf0f6", "#b6c6d8"), "大寒": ("#e6ecf4", "#aebfd4"),
    "立春": ("#e8f4e4", "#aed3a0"), "雨水": ("#e2eef8", "#a3c6e6"),
    "惊蛰": ("#e9f5ea", "#93cfa4"), "春分": ("#fdf2dd", "#f4c37a"),
    "清明": ("#e9f2e5", "#a2c4a2"), "谷雨": ("#ecf5df", "#accb86"),
    "立夏": ("#fdeada", "#f2a35f"), "小满": ("#f8f3da", "#dcc96e"),
    "芒种": ("#f1f8dd", "#b5d16f"), "夏至": ("#fde8cb", "#f5a95c"),
    "小暑": ("#fde5d1", "#f08d52"), "大暑": ("#fbddc8", "#e97840"),
    "立秋": ("#f7edda", "#dcb673"), "处暑": ("#eff3e2", "#bcc783"),
    "白露": ("#e5eff3", "#a2c6d3"), "秋分": ("#f8eddb", "#e3bc7d"),
    "寒露": ("#e8f0f6", "#aec7dd"), "霜降": ("#f0eae3", "#cbbba7"),
    "立冬": ("#ebeff4", "#b2c0d1"), "小雪": ("#eff3f8", "#c6d1dc"),
    "大雪": ("#eaf0f7", "#bccbdf"), "冬至": ("#f0f3f7", "#adbcD2".lower()),
    "元旦": ("#fdf0e2", "#f0c088"), "情人节": ("#fde9ee", "#f0a3b6"),
    "国际妇女节": ("#fdeaf2", "#ef9dbc"), "植树节": ("#e9f5e6", "#9dcd97"),
    "愚人节": ("#f0f2f7", "#b9c3d6"), "国际劳动节": ("#fdece0", "#eeab73"),
    "青年节": ("#e9f0fa", "#93b4e0"), "国际儿童节": ("#fdf0e8", "#f3b183"),
    "建党节": ("#fdeaea", "#e79a9a"), "建军节": ("#e9f0e9", "#9ec19e"),
    "教师节": ("#fdf3e2", "#ecc98c"), "国庆节": ("#fdeae6", "#ee9b83"),
    "程序员节": ("#e8eef7", "#94b1d8"), "双十一": ("#fdeae4", "#ef9c84"),
    "平安夜": ("#eaf0f2", "#a9c2c6"), "圣诞节": ("#fdeceb", "#ea9f9d"),
    "跨年夜": ("#eceaf6", "#a9a3d8"),
}

_SEASON_PALETTE = [
    ("#edf2f8", "#b8cade"),
    ("#edf6ea", "#b4d2ae"),
    ("#edf6ea", "#b4d2ae"),
    ("#edf6ea", "#b4d2ae"),
    ("#fdf3e4", "#f0c98a"),
    ("#fdf3e4", "#f0c98a"),
    ("#fdf3e4", "#f0c98a"),
    ("#f7efe2", "#dfc194"),
    ("#f7efe2", "#dfc194"),
    ("#f7efe2", "#dfc194"),
    ("#e9eef6", "#b3c2d8"),
]


def festival_of(d: _dt.date, anniversaries: list[dict] | None = None,
                year: int | None = None) -> str:
    """当天的节日 / 节气 / 纪念日名称。优先级：自定义 > 公历节日 > 节气。"""
    for a in anniversaries or []:
        if str(a.get("date", "")).strip() == d.isoformat():
            title = str(a.get("title") or "").strip()
            if title:
                return title
    hit = SOLAR_FESTIVALS.get((d.month, d.day))
    if hit:
        return hit
    for m, day, t in SOLAR_TERMS.get(year or d.year, []):
        if m == d.month and day == d.day:
            return t
    return ""


def upcoming(d: _dt.date, anniversaries: list[dict] | None = None, days: int = 45) -> list[dict]:
    """未来若干天内的节日 / 纪念日倒计时。"""
    out: list[dict] = []
    for i in range(1, days + 1):
        nd = d + _dt.timedelta(days=i)
        t = festival_of(nd, anniversaries)
        if t:
            out.append({
                "date": nd.isoformat(),
                "title": t,
                "inDays": i,
                "label": nd.strftime("%m月%d日"),
                "tone": "today" if i == 0 else ("soon" if i <= 7 else "later"),
            })
    return out


def today_context(now: _dt.datetime | _dt.date | None = None,
                  anniversaries: list[dict] | None = None) -> dict:
    # 允许传 datetime / date / None 三种，内部统一成 date
    if now is None:
        d = _dt.date.today()
    elif isinstance(now, _dt.datetime):
        d = now.date()
    else:
        d = now
    title = festival_of(d, anniversaries)
    palette = TERM_PALETTE.get(title) or _SEASON_PALETTE[d.month - 1]
    return {
        "iso": d.isoformat(),
        "year": d.year,
        "month": d.month,
        "day": d.day,
        "weekday": WEEKDAYS[d.weekday()],
        "dateText": f"{d.year}年{d.month}月{d.day}日",
        "festival": title,
        "palette": {"from": palette[0], "to": palette[1]},
        "countdown": upcoming(d, anniversaries),
        "season": ["冬", "冬", "春", "春", "春", "夏", "夏", "夏", "秋", "秋", "秋", "冬"][d.month - 1],
    }
