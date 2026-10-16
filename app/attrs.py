"""图片属性体系：三层标签 + 中英双语关键词。

为什么要分层：
  平铺一堆关键词的问题是「流星雨」和「夜」混在一起，
  没法表达「今天夜里 + 正好赶上流星雨 → 该用哪张」这种条件。

三层：
  L1 题材 —— 拍的是什么（天象 / 地形 / 植物 / 建筑 / 水景 / 生命 / 人文 / 天气）
  L2 场景 —— 什么时候、什么光（时段 + 季节 + 天气）
  L3 事件 —— 发生了什么（流星雨 / 银河 / 极光 / 云海 / 雾凇 / 彩虹 …）

每个类别都带 zh / en 两套词：中文源（Bing 壁纸、Windows 聚焦）用中文描述，
NASA 的英文源只能靠英文词打标。只靠一套词的话，天文图一张都命不中。

单字关键词只在语义足够明确时才用（雪、雾、山…）——曾经因为「火星」里的「星」
命中了「星轨」，把一张白天拍的图排到了夜景第一位。
"""

from __future__ import annotations

import datetime as _dt
import re

# 单字里语义明确、不会误伤的
SAFE_SINGLE = set("雪雾雨云冰花山林湖海峰桥岛晴溪田草楼塔屋寺")
# 会被别的词吃掉的单字，一律不用（星/月/夜/光/影 → 用双字）
_BANNED_SINGLE = set("星月夜光影天地水火风日云")


def _ok(word: str) -> bool:
    if len(word) >= 2:
        return True
    return word.lower() in SAFE_SINGLE


def _clean(words) -> list[str]:
    seen, out = set(), []
    for w in words:
        w = str(w).strip()
        if not w or w in seen or not _ok(w):
            continue
        seen.add(w)
        out.append(w)
    return out


# --------------------------------------------------------------- 词表

ATTRS: dict[str, dict[str, dict[str, list[str]]]] = {
    "题材": {
        "天象": {
            "zh": ["银河", "星空", "星轨", "星云", "极光", "流星", "星座", "夜空", "天文",
                   "深空", "月食", "日食", "行星", "土星", "木星", "火星", "月球", "流星雨",
                   "星野", "银河拱桥", "天文台", "射电望远镜", "卫星", "彗星"],
            "en": ["galaxy", "milky way", "nebula", "aurora", "auroral", "meteor", "shooting star",
                   "star trail", "night sky", "constellation", "deep space", "cosmos",
                   "astronomy", "astronomical", "astrophotography",
                   "jupiter", "saturn", "mars", "moon", "lunar", "eclipse", "comet", "observatory",
                   "hubble", "james webb", "iss", "space station", "star cluster", "supernova",
                   "perseid", "geminid", "quadrantid", "leonid", "northern lights", "zodiacal"],
        },
        "地形": {
            "zh": ["山脉", "山峰", "雪山", "峡谷", "峭壁", "悬崖", "沙漠", "沙丘", "冰川", "火山",
                   "高原", "盆地", "岩", "溶洞", "峡谷", "草原", "苔原", "荒原", "盐湖", "丹霞",
                   "雅丹", "地缝", "天坑", "梯田", "丘陵", "戈壁"],
            "en": ["mountain", "peak", "summit", "ridge", "canyon", "gorge", "cliff", "desert",
                   "dune", "glacier", "volcano", "plateau", "valley", "basin", "cave", "canyon",
                   "steppe", "tundra", "badlands", "salt flat", "range"],
        },
        "植物": {
            "zh": ["森林", "树林", "红叶", "枫叶", "樱花", "花海", "野花", "荷花", "竹林",
                   "苔藓", "藤蔓", "芦苇", "草地", "麦田", "稻田", "花园", "银杏", "落叶",
                   "嫩芽", "松林", "白桦", "水杉", "红杉", "仙人掌", "苔原植物"],
            "en": ["forest", "woodland", "tree", "leaf", "leaves", "flower", "blossom", "cherry",
                   "maple", "autumn", "foliage", "moss", "fern", "reed", "meadow", "grass",
                   "wheat", "rice field", "garden", "pine", "birch", "sequoia", "cactus", "bloom"],
        },
        "建筑": {
            "zh": ["建筑", "教堂", "神庙", "古堡", "城堡", "宫殿", "塔", "摩天", "天际线",
                   "村落", "古镇", "水乡", "桥", "灯塔", "寺院", "修道院", "广场", "街道",
                   "屋顶", "木屋", "小屋", "石屋", "民宅", "胡同", "码头", "风车", "遗迹", "废墟"],
            "en": ["architecture", "building", "church", "cathedral", "temple", "castle", "palace",
                   "tower", "skyline", "village", "town", "bridge", "lighthouse",
                   "monastery", "abbey", "plaza", "street", "rooftop", "facade", "ruin", "windmill",
                   "harbor", "pier", "cityscape", "cabin", "cottage", "house"],
        },
        "水景": {
            "zh": ["海", "海岸", "海滩", "礁石", "海浪", "湖泊", "湖面", "河流", "溪流",
                   "瀑布", "冰川湖", "倒影", "海湾", "峡湾", "湿地", "沼泽", "喷泉", "潮汐"],
            "en": ["ocean", "sea", "coast", "coastline", "beach", "reef", "wave", "lake",
                   "river", "stream", "waterfall", "reflection", "bay", "fjord", "wetland",
                   "marsh", "tide", "shore"],
        },
        "生命": {
            "zh": ["飞鸟", "候鸟", "鹿", "狐狸", "熊", "马群", "羊群", "鲸", "海豚", "企鹅",
                   "蝴蝶", "蜜蜂", "昆虫", "鹤", "鹰", "天鹅", "松鼠", "羚羊", "大象", "狮子"],
            "en": ["bird", "deer", "fox", "bear", "horse", "sheep", "whale", "dolphin",
                   "penguin", "butterfly", "bee", "insect", "crane", "eagle", "swan",
                   "squirrel", "antelope", "elephant", "lion", "tiger", "wildlife"],
        },
        "人文": {
            "zh": ["人物", "剪影", "行人", "集市", "节庆", "灯笼", "烟火", "渔船", "牧人",
                   "茶田", "农田", "劳作", "朝圣", "街头"],
            "en": ["people", "silhouette", "crowd", "market", "festival", "lantern",
                   "firework", "fisherman", "shepherd", "farmer", "pilgrim", "street life"],
        },
    },
    "时段": {
        "夜晚": {"zh": ["夜晚", "夜景", "夜间", "深夜", "星空", "银河", "星轨", "极光", "灯火", "月光", "夜空"],
                 "en": ["night", "nighttime", "midnight", "nocturnal", "starry", "after dark", "moonlit"]},
        "黎明": {"zh": ["黎明", "破晓", "晨曦", "日出", "朝霞", "晨光", "曙光"],
                 "en": ["dawn", "daybreak", "sunrise", "first light", "early morning"]},
        "清晨": {"zh": ["清晨", "早晨", "晨雾", "晨", "露水"],
                 "en": ["morning", "misty morning", "dew"]},
        "正午": {"zh": ["正午", "烈日", "中午", "阳光", "明亮", "晴空"],
                 "en": ["noon", "midday", "bright", "sunny"]},
        "黄昏": {"zh": ["黄昏", "日落", "傍晚", "暮色", "晚霞", "夕阳", "落日"],
                 "en": ["sunset", "dusk", "evening", "golden hour", "twilight"]},
        "蓝调": {"zh": ["蓝调", "蓝调时刻", "入夜", "华灯"],
                 "en": ["blue hour", "afterglow"]},
    },
    "季节": {
        "春": {"zh": ["春天", "春季", "春日", "春色", "新绿", "花开", "樱花", "嫩芽", "融雪", "油菜花"],
               "en": ["spring", "springtime", "blossom season", "new growth"]},
        "夏": {"zh": ["夏天", "夏季", "夏日", "夏夜", "盛夏", "蝉", "绿荫", "雷雨", "萤火"],
               "en": ["summer", "summertime", "midsummer"]},
        "秋": {"zh": ["秋天", "秋季", "秋日", "秋色", "秋高", "红叶", "银杏", "金黄", "稻穗", "丰收", "霜降"],
               "en": ["autumn", "fall foliage", "harvest"]},
        "冬": {"zh": ["冬天", "冬季", "冬日", "寒冬", "雪景", "冰封", "雾凇", "寒", "白雪"],
               "en": ["winter", "wintertime", "snow", "frozen", "frost"]},
    },
    "天气": {
        "晴": {"zh": ["晴朗", "晴空", "万里无云", "碧空", "蓝天", "明媚"],
               "en": ["clear sky", "cloudless", "sunshine", "blue sky"]},
        "云": {"zh": ["云海", "积云", "层云", "云层", "多云", "流云", "云雾"],
               "en": ["cloud", "cloudy", "sea of clouds", "overcast"]},
        "雾": {"zh": ["晨雾", "浓雾", "雾气", "薄雾", "雾凇", "霾"],
               "en": ["fog", "mist", "haze", "foggy"]},
        "雨": {"zh": ["雨", "阵雨", "暴雨", "雨后", "湿润", "雨滴", "积水"],
               "en": ["rain", "rainy", "rainfall", "wet", "droplet"]},
        "雪": {"zh": ["雪", "飘雪", "积雪", "暴雪", "初雪", "冰雪", "雪原"],
               "en": ["snow", "snowfall", "snowy", "blizzard", "ice", "icy"]},
        "风暴": {"zh": ["风暴", "雷暴", "闪电", "台风", "飓风", "龙卷"],
                 "en": ["storm", "thunder", "lightning", "hurricane", "typhoon", "tornado"],
                 },
    },
    "事件": {
        "流星雨": {"zh": ["流星雨", "流星", "英仙座", "双子座", "象限仪", "狮子座", "猎户座"],
                   "en": ["meteor shower", "meteor", "shooting star", "perseid", "geminid",
                          "quadrantid", "leonid", "orionid"]},
        "银河": {"zh": ["银河", "银河拱桥", "银心", "星河"],
                 "en": ["milky way", "galactic", "galactic core"]},
        "极光": {"zh": ["极光", "北极光", "南极光", "极光带"],
                 "en": ["aurora", "northern lights", "southern lights", "auroral"]},
        "日食月食": {"zh": ["日食", "月食", "日全食", "月全食", "红月"],
                     "en": ["eclipse", "solar eclipse", "lunar eclipse", "blood moon"]},
        "云海": {"zh": ["云海", "翻腾的云", "云雾缭绕"],
                 "en": ["sea of clouds", "cloud inversion"]},
        "雾凇": {"zh": ["雾凇", "树挂", "冰挂", "雨凇"],
                 "en": ["rime ice", "hoarfrost", "frost flowers"]},
        "彩虹": {"zh": ["彩虹", "双虹", "日晕", "光柱", "幻日"],
                 "en": ["rainbow", "double rainbow", "halo", "sun dog", "light pillar"]},
        "长曝": {"zh": ["长曝", "长曝光", "延时", "轨迹", "星轨"],
                 "en": ["long exposure", "star trail", "time-lapse", "light trail"]},
        "满月": {"zh": ["满月", "圆月", "超级月亮", "月出", "月落"],
                 "en": ["full moon", "supermoon", "moonrise", "moonset"]},
        "海雾": {"zh": ["海雾", "平流雾", "海市蜃楼"],
                 "en": ["sea fog", "advection fog", "mirage"]},
    },
    "色彩": {
        "金": {"zh": ["金色", "金黄", "金光", "暖黄"], "en": ["golden", "gold", "amber"]},
        "蓝": {"zh": ["湛蓝", "深蓝", "蔚蓝", "碧蓝"], "en": ["blue", "azure", "turquoise"]},
        "绿": {"zh": ["翠绿", "碧绿", "青绿"], "en": ["green", "emerald", "verdant"]},
        "红": {"zh": ["绯红", "火红", "殷红", "丹红"], "en": ["red", "crimson", "scarlet"]},
        "黑白": {"zh": ["黑白", "单色", "灰调"], "en": ["black and white", "monochrome"]},
        "紫粉": {"zh": ["紫色", "薰衣草", "粉紫", "洋红"], "en": ["purple", "violet", "magenta", "lavender"]},
    },
    "构图": {
        "倒影": {"zh": ["倒影", "水面镜面", "镜像"], "en": ["reflection", "mirror"]},
        "剪影": {"zh": ["剪影", "轮廓"], "en": ["silhouette", "backlit"]},
        "广角": {"zh": ["广角", "全景", "辽阔", "一望无际"], "en": ["panorama", "wide", "vast", "sweeping"]},
        "特写": {"zh": ["特写", "微距", "细节"], "en": ["close-up", "macro", "detail"]},
        "航拍": {"zh": ["航拍", "俯拍", "鸟瞰", "无人机"], "en": ["aerial", "from above", "drone", "bird's eye"]},
        "对称": {"zh": ["对称", "中轴"], "en": ["symmetry", "symmetrical"]},
    },
}

# 每层在匹配时的分量
LAYER_WEIGHT = {"事件": 3.2, "时段": 2.4, "天气": 1.7, "季节": 1.2,
                "题材": 0.9, "色彩": 0.5, "构图": 0.4}

# 用户点名的偏好（可在设置里改）
DEFAULT_LIKES = ["天象", "地形", "植物", "建筑"]
LIKE_BONUS = 1.6


def _flatten() -> dict[str, tuple[str, str]]:
    """词 -> (层, 类别)，用于快速查表。英文词统一小写。"""
    table: dict[str, tuple[str, str]] = {}
    for layer, cats in ATTRS.items():
        for cat, langs in cats.items():
            for w in _clean(langs.get("zh") or []):
                table.setdefault(w, (layer, cat))
            for w in _clean(langs.get("en") or []):
                table.setdefault(w.lower(), (layer, cat))
    return table


_WORD_TABLE = _flatten()


def describe(text: str) -> dict:
    """把一段描述文字拆成属性。

    返回 {"题材": [...], "时段": [...], ..., "flat": [...]}，
    只有命中非空的层才会出现。

    英文词必须按**词边界**匹配：不然 "ice" 会命中 "office / service"，
    "sea" 会命中 "season"，一张办公室照片就被打上「雪」。
    """
    blob = str(text or "")
    low = blob.lower()
    out: dict[str, list[str]] = {}
    flat: list[str] = []
    for word, (layer, cat) in _WORD_TABLE.items():
        if word.isascii():
            hit = re.search(r"\b" + re.escape(word) + r"\b", low) is not None
        else:
            hit = word in blob
        if not hit:
            continue
        out.setdefault(layer, [])
        if cat not in out[layer]:
            out[layer].append(cat)
        if word not in flat:
            flat.append(word)
    out["flat"] = flat
    return out


# ------------------------------------------------------- 天文事件（按日期）

# 年度流星雨：名称、起始、极大、结束（月, 日）
METEOR_SHOWERS = [
    ("象限仪座流星雨", (1, 1), (1, 3), (1, 5)),
    ("天琴座流星雨", (4, 16), (4, 22), (4, 25)),
    ("宝瓶座η流星雨", (5, 3), (5, 6), (5, 10)),
    ("英仙座流星雨", (7, 17), (8, 12), (8, 24)),
    ("猎户座流星雨", (10, 2), (10, 21), (11, 7)),
    ("狮子座流星雨", (11, 6), (11, 17), (11, 30)),
    ("双子座流星雨", (12, 4), (12, 14), (12, 17)),
    ("小熊座流星雨", (12, 17), (12, 22), (12, 26)),
]


def _in_window(d: _dt.date, start: tuple[int, int], end: tuple[int, int]) -> bool:
    """判断某天是否落在「(月, 日) 起 ~ (月, 日) 止」的窗口里，跨年也算。"""
    keys = [start, end]
    s = _dt.date(d.year, keys[0][0], keys[0][1])
    e = _dt.date(d.year, keys[1][0], keys[1][1])
    if e < s:                       # 跨年窗口（比如 12/17 ~ 1/5）
        return d >= s or d <= e
    return s <= d <= e


def moon_phase(d: _dt.date) -> tuple[float, str]:
    """粗略月相：返回 (0~1 的相位, 中文名)。误差在一天以内，够用来打标。"""
    # 以 2000-01-06 的新月为基准，朔望月 29.530588 天
    ref = _dt.date(2000, 1, 6)
    days = (d - ref).days
    phase = (days % 29.530588) / 29.530588
    if phase < 0.03 or phase > 0.97:
        name = "新月"
    elif phase < 0.22:
        name = "娥眉月"
    elif phase < 0.28:
        name = "上弦月"
    elif phase < 0.47:
        name = "盈凸月"
    elif phase < 0.53:
        name = "满月"
    elif phase < 0.72:
        name = "亏凸月"
    elif phase < 0.78:
        name = "下弦月"
    else:
        name = "残月"
    return phase, name


def astro_events(d: _dt.date, night: bool = False) -> list[dict]:
    """这一天有什么值得拍的天象。`night=True` 表示按「今晚」算。"""
    day = d + _dt.timedelta(days=1) if night else d
    out: list[dict] = []

    for name, start, peak, end in METEOR_SHOWERS:
        if _in_window(day, start, end):
            near = abs((day - _dt.date(day.year, *peak)).days)
            near = min(near, 365 - near) if near > 180 else near
            out.append({
                "event": "流星雨", "tags": ["流星雨", name],
                "title": name,
                "note": "极大期" if near <= 1 else f"距极大期 {near} 天",
                "score": 1.0 if near <= 1 else (0.8 if near <= 4 else 0.5),
            })

    # 北半球银河季：3 月 ~ 10 月，6~8 月最佳
    if 3 <= day.month <= 10:
        best = 6 <= day.month <= 8
        out.append({"event": "银河", "tags": ["银河"],
                    "title": "银河季", "note": "最佳期" if best else "可见期",
                    "score": 1.0 if best else 0.6})

    # 极光季：秋分到春分（北半球）
    if day.month in (9, 10, 11, 12, 1, 2, 3):
        out.append({"event": "极光", "tags": ["极光"],
                    "title": "极光季", "note": "北半球高纬",
                    "score": 0.7 if day.month in (10, 11, 2, 3) else 0.5})

    phase, name = moon_phase(day)
    if name == "满月":
        out.append({"event": "满月", "tags": ["满月"],
                    "title": "满月", "note": "月光很亮，适合拍地景",
                    "score": 0.6})
    elif name == "新月":
        out.append({"event": "银河", "tags": ["星轨", "星空"],
                    "title": "新月", "note": "没有月光干扰，适合深空与星轨",
                    "score": 0.8})
    return out


def festival_attrs(festival: str, season: str) -> list[str]:
    """节日 / 节气带来的额外关键词。"""
    if not festival:
        return []
    f = str(festival)
    extra: list[str] = []
    table = {
        "霜降": ["霜", "秋"], "立冬": ["冬"], "小雪": ["雪"], "大雪": ["雪"],
        "冬至": ["冬", "雪"], "小寒": ["冬", "冰"], "大寒": ["冬", "冰"],
        "立春": ["春", "新绿"], "雨水": ["雨", "春"], "惊蛰": ["春"],
        "春分": ["春", "花开"], "清明": ["春", "雨"], "谷雨": ["春", "雨"],
        "立夏": ["夏"], "小满": ["夏", "麦田"], "芒种": ["夏", "田"],
        "夏至": ["夏", "烈日"], "小暑": ["夏"], "大暑": ["夏", "烈日"],
        "立秋": ["秋"], "处暑": ["秋"], "白露": ["秋", "露水"],
        "秋分": ["秋", "金黄"], "寒露": ["秋", "露水"],
        "元旦": ["灯火"], "春节": ["灯笼", "节庆"], "元宵": ["灯笼", "满月"],
        "端午": ["夏"], "中秋": ["满月"],
        "国庆节": ["节庆"], "劳动节": ["节庆"], "青年节": ["夏"],
        "清明节": ["春", "雨"],
    }
    extra.extend(table.get(f, []))
    if season and season in "春夏秋冬":
        extra.append(season)
    return _clean(extra)
