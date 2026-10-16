"""配置项的唯一真相（表驱动）。

前端设置面板由这张表渲染，后端由 `GET /api/settings-schema` 吐出去。

为什么要这样：以前字段在三个地方各手写一遍（`web/index.html` 的表单、
`web/app.js` 的 fillSettings / saveSettings），加一个配置项要改三处，
结果就是漏 —— 政经加权词、阅读速度、窗口尺寸、心跳阈值这些全都改不了，
只能改代码。现在只改这一个文件。

字段类型：
    text      单行文本          password  密码
    int       整数（带 min/max）
    bool      开关
    select    下拉（options = [[值, 显示名], ...]）
    lines     多行，一行一项 -> list[str]
    keywords  多行/逗号分隔关键词 -> list[str]
    feeds     资讯源，一行「分类 | 名称 | 地址」
    ann       纪念日，一行「YYYY-MM-DD | 名称」
    json      原始 JSON（启动项这种结构复杂的）

`warn` 是显眼的风险提示（红字），`hint` 是普通说明（灰字）。
"""

from __future__ import annotations

import copy

GROUPS: list[dict] = [
    {
        "key": "profile",
        "title": "个人",
        "fields": [
            {"path": "profile.name", "label": "称呼", "type": "text",
             "hint": "只用在提示词里，可留空"},
            {"path": "profile.city", "label": "城市", "type": "text",
             "hint": "留空则按你的网络位置自动定位。改了会清掉缓存的经纬度，下次重新定位"},
        ],
    },
    {
        "key": "ai",
        "title": "大模型",
        "fields": [
            {"path": "ai.enabled", "label": "启用模型", "type": "bool",
             "default": True,
             "hint": "关掉后简报退化成「只抓标题 + 原文摘要」的本地骨架，不联网调模型"},
            {"path": "ai.mode", "label": "调用方式", "type": "select", "default": "api",
             "options": [["api", "接口调用"], ["web", "网页版（自己贴提示词）"]],
             "hint": "网页版模式不花 token，但要你手动把提示词贴到聊天页"},
            {"path": "ai.base_url", "label": "接口地址", "type": "text",
             "ph": "https://api.deepseek.com/v1",
             "hint": "任何 OpenAI 兼容接口都行"},
            {"path": "ai.api_key", "label": "API Key", "type": "password", "ph": "sk-..."},
            {"path": "ai.model", "label": "模型名", "type": "text", "ph": "deepseek-chat"},
            {"path": "ai.vision_model", "label": "视觉模型", "type": "text",
             "hint": "给图片打标用的多模态模型。留空则不传图（配合下面「配图与壁纸」里的视觉模式）"},
            {"path": "ai.web_url", "label": "网页版入口", "type": "text",
             "ph": "https://chat.deepseek.com/",
             "hint": "网页版模式打开哪个聊天页"},
        ],
    },
    {
        "key": "news",
        "title": "资讯",
        "fields": [
            {"path": "news.count", "label": "每天几条", "type": "int",
             "min": 4, "max": 20, "default": 10},
            {"path": "news.read_minutes", "label": "全部读完要几分钟", "type": "int",
             "min": 5, "max": 60, "default": 15,
             "hint": "和「每条多少字」联动：条数 × 条长 ≈ 这个阅读量"},
            {"path": "news.chars_per_minute", "label": "每分钟读多少字", "type": "int",
             "min": 150, "max": 1200, "default": 400,
             "hint": "按你的阅读速度算每条简述该写多长"},
            {"path": "news.max_age_days", "label": "资讯最多几天前", "type": "int",
             "min": 1, "max": 30, "default": 3,
             "hint": "超过这个天数的条目直接丢掉，免得混进旧闻"},
            {"path": "news.brief", "label": "生成当日总览", "type": "bool", "default": True,
             "hint": "简报顶部那两三句话"},
            {"path": "news.feeds", "label": "资讯源", "type": "feeds",
             "hint": "一行一个：分类 | 名称 | 地址。分类可选 要闻 / 财经 / 科技 / 学术"},
            {"path": "news.exclude", "label": "丢弃关键词", "type": "keywords",
             "hint": "标题命中就丢掉。娱乐、体育、生活八卦这类都在这"},
            {"path": "news.boost", "label": "加权关键词", "type": "keywords",
             "hint": "标题命中就加分，用来把政经、学术类顶到前面。可按自己口味改"},
        ],
    },
    {
        "key": "photos",
        "title": "配图与壁纸",
        "fields": [
            {"path": "photos.dirs", "label": "我自己的摄影文件夹", "type": "lines",
             "hint": "一行一个目录，里面的图会被优先选作壁纸"},
            {"path": "photos.likes", "label": "配图偏好", "type": "keywords",
             "hint": "可选：天象 / 地形 / 植物 / 建筑 / 水景 / 生命 / 人文。命中会加分"},
            {"path": "photos.rotate", "label": "按时段轮换壁纸", "type": "bool", "default": True,
             "hint": "关掉就一天固定一张"},
            {"path": "photos.tag_local", "label": "本地推导图片属性", "type": "bool",
             "default": True,
             "hint": "读图片自身的拍摄时间(EXIF)、焦段、曝光、来源和文件名来打标签。"
                     "不联网、不花钱，也能覆盖时段/季节/构图这些层"},
            {"path": "photos.vision_tagging", "label": "视觉模式打标（预留）", "type": "bool",
             "default": False,
             "hint": "本轮只做到「开关就位」——打开也不会调用模型，因为它还没接线，"
                     "开之前不会有任何 token 消耗",
             "warn": "接线之后：识图更准（能看出画面里是山是海是花），"
                     "但会把图片传给上面配的视觉模型，token 消耗可能明显变高。默认关闭"},
            {"path": "photos.pexels_key", "label": "Pexels Key", "type": "password",
             "hint": "可选。填了会按主题真搜图，不填就用内置图源"},
            {"path": "photos.nasa_key", "label": "NASA Key", "type": "password",
             "hint": "可选。留空会退回 DEMO_KEY（每小时只有 30 次配额）"},
            {"path": "photos.nasa", "label": "抓 NASA 图源", "type": "bool", "default": True},
        ],
    },
    {
        "key": "signals",
        "title": "本地痕迹与隐私",
        "fields": [
            {"path": "signals.enabled", "label": "采集本机使用痕迹", "type": "bool",
             "default": True,
             "warn": "开启后，生成简报时会读本机痕迹（AI 助手使用记录、"
                     "浏览器搜索词与页面标题、桌面最近动过的文件名），"
                     "压成一段文本**随提示词发给你配置的模型服务**。"
                     "全部采集都在本机完成、只读；关掉后简报的「与你相关」一类会变弱"},
            {"path": "signals.browser_history", "label": "包含浏览器历史", "type": "bool",
             "default": True,
             "warn": "会只读 Edge / Chrome 的浏览历史库，提取最近搜索词与访问过的页面标题"},
            {"path": "signals.days", "label": "追溯多少天", "type": "int",
             "min": 1, "max": 60, "default": 14},
        ],
    },
    {
        "key": "power",
        "title": "运行",
        "fields": [
            {"path": "power.prefetch_tomorrow", "label": "每天 0 点预生成次日内容",
             "type": "bool", "default": True,
             "hint": "夜里备好缓存，早上打开就是新的"},
            {"path": "power.keepalive_seconds", "label": "心跳超时（秒）", "type": "int",
             "min": 60, "max": 7200, "default": 600,
             "hint": "多久没收到页面心跳就认为窗口关了。别调太小 —— 浏览器会节流后台窗口的"
                     "定时器、电脑睡眠期间更是完全停摆，阈值太紧会把还在用的服务误杀"},
            {"path": "power.sweep_browser_cache", "label": "自动清理浏览器缓存", "type": "bool",
             "default": True,
             "hint": "开窗用的浏览器配置会长很快，后台清掉闲置的缓存目录"},
            {"path": "window.width", "label": "窗口宽", "type": "int",
             "min": 900, "max": 3840, "default": 1420},
            {"path": "window.height", "label": "窗口高", "type": "int",
             "min": 600, "max": 2160, "default": 900},
        ],
    },
    {
        "key": "theme",
        "title": "外观",
        "fields": [
            {"path": "theme", "label": "配色", "type": "select", "default": "light",
             "options": [["light", "暖白"], ["paper", "羊皮纸"], ["dark", "暗色"]]},
        ],
    },
    {
        "key": "adv",
        "title": "高级",
        "fields": [
            {"path": "anniversaries", "label": "自定义纪念日", "type": "ann",
             "hint": "一行一个：2026-11-08 | 某人生日"},
            {"path": "launchers", "label": "启动项（JSON）", "type": "json",
             "hint": "界面上「添加启动项」会自动维护这里，一般不用手改"},
        ],
    },
]


def all_fields() -> list[dict]:
    return [f for g in GROUPS for f in g["fields"]]


def get_path(cfg: dict, path: str, default=None):
    """按 "a.b.c" 取值。"""
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(cfg: dict, path: str, value) -> None:
    """按 "a.b.c" 就地写值，中间层不存在就建。"""
    parts = path.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def blank_config() -> dict:
    """只有 schema 里那些字段的空壳，用来做校验/示例。"""
    out: dict = {}
    for f in all_fields():
        set_path(out, f["path"], copy.deepcopy(f.get("default")))
    return out
