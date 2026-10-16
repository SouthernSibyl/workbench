"""天气：Open-Meteo 免费接口（无需 key）+ IP 定位。

两条路径，界面走第一条：
  - `peek()`        —— 只读缓存，**绝不联网**。界面第一屏用它，保证秒开。
  - `refresh_async()` —— 后台刷新，刷完回调通知界面重画。

`current()` 是同步版（会联网），只给后台线程用，别放进请求处理路径。
"""

from __future__ import annotations

import datetime as _dt
import json
import ssl
import threading
import time
import urllib.parse
import urllib.request

import store

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0"

TTL = 15 * 60  # 秒

# WMO weather code -> (中文, 图标名, 用于配图匹配的关键词)
# 关键词要**够具体**：单字「星 / 月 / 夜 / 光 / 影」会误伤（"火星"里的星），一律不用。
WMO = {
    0: ("晴", "sun", ["晴", "晴朗", "阳光", "明媚", "蓝天", "万里无云"]),
    1: ("大部晴朗", "sun", ["晴", "晴朗", "阳光", "蓝天"]),
    2: ("多云", "cloud-sun", ["多云", "云海", "云层", "浮云"]),
    3: ("阴", "cloud", ["阴天", "乌云", "云层", "灰调"]),
    45: ("有雾", "fog", ["雾", "晨雾", "浓雾", "薄雾", "朦胧", "雾气"]),
    48: ("雾凇", "fog", ["雾凇", "树挂", "霜", "冰晶"]),
    51: ("小毛毛雨", "drizzle", ["雨", "细雨", "湿润"]),
    53: ("毛毛雨", "drizzle", ["雨", "细雨", "湿润"]),
    55: ("浓毛毛雨", "drizzle", ["雨", "细雨", "湿润"]),
    56: ("冻毛毛雨", "drizzle", ["雨", "冰", "冻雨"]),
    57: ("冻毛毛雨", "drizzle", ["雨", "冰", "冻雨"]),
    61: ("小雨", "rain", ["雨", "小雨", "雨滴", "湿润"]),
    63: ("中雨", "rain", ["雨", "雨中", "湿润"]),
    65: ("大雨", "rain", ["雨", "暴雨", "瓢泼"]),
    66: ("冻雨", "rain", ["雨", "冰", "冻雨"]),
    67: ("冻雨", "rain", ["雨", "冰", "冻雨"]),
    71: ("小雪", "snow", ["雪", "落雪", "冬"]),
    73: ("中雪", "snow", ["雪", "飘雪", "冬"]),
    75: ("大雪", "snow", ["雪", "雪原", "积雪", "冬"]),
    77: ("雪粒", "snow", ["雪", "冰晶", "冬"]),
    80: ("小阵雨", "rain", ["雨", "阵雨", "云"]),
    81: ("阵雨", "rain", ["雨", "阵雨", "云"]),
    82: ("强阵雨", "rain", ["雨", "暴雨", "乌云"]),
    85: ("小阵雪", "snow", ["雪", "阵雪", "冬"]),
    86: ("大阵雪", "snow", ["雪", "阵雪", "冬"]),
    95: ("雷阵雨", "thunder", ["雷雨", "闪电", "乌云"]),
    96: ("雷暴伴冰雹", "thunder", ["雷雨", "冰", "闪电"]),
    99: ("雷暴伴冰雹", "thunder", ["雷雨", "冰", "闪电"]),
}


def describe(code) -> tuple[str, str, list[str]]:
    try:
        return WMO.get(int(code), ("未知", "cloud", []))
    except (TypeError, ValueError):
        return ("未知", "cloud", [])


# ------------------------------------------------------------------- 定位

def _ip_locate() -> dict | None:
    """两个免费定位源轮流试，谁先通用谁。"""
    sources = [
        ("http://ip-api.com/json/?lang=zh-CN",
         lambda d: (d.get("city") or d.get("regionName"), d.get("lat"), d.get("lon"))),
        ("https://ipwho.is/?lang=zh",
         lambda d: (d.get("city"), d.get("latitude"), d.get("longitude"))),
    ]
    for url, pick in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8, context=CTX) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
            if d.get("status") not in (None, "success") and d.get("success") is not True:
                continue
            city, lat, lon = pick(d)
            if lat is not None and lon is not None:
                return {"city": city or "当前位置", "lat": float(lat),
                        "lon": float(lon), "from": "ip"}
        except Exception as exc:
            store.log(f"locate {url.split('/')[2]} failed: {exc}")
    return None


def locate(cfg: dict, force: bool = False) -> dict:
    """返回 {city, lat, lon}。优先配置，其次 IP 定位，最后用默认。"""
    prof = cfg.get("profile") or {}
    if not force and prof.get("lat") and prof.get("lon"):
        return {"city": prof.get("city") or "当前位置",
                "lat": float(prof["lat"]), "lon": float(prof["lon"]), "from": "config"}

    if not prof.get("lat"):
        hit = _ip_locate()
        if hit:
            # 存下来，免得每次启动都依赖外部接口
            try:
                store.patch_config(profile={"city": hit["city"],
                                            "lat": hit["lat"], "lon": hit["lon"]})
                store.write_cache("geo", hit)
            except Exception:
                pass
            return hit

    cached = store.read_cache("geo")
    if cached and cached.get("lat"):
        return {"city": cached.get("city") or "当前位置",
                "lat": float(cached["lat"]), "lon": float(cached["lon"]), "from": "cache"}
    if prof.get("city"):
        return {"city": prof["city"], "lat": 39.9042, "lon": 116.4074, "from": "guess"}
    return {"city": "北京", "lat": 39.9042, "lon": 116.4074, "from": "default"}


# ------------------------------------------------------------------- 抓取

def _fetch(lat: float, lon: float) -> dict:
    q = urllib.parse.urlencode({
        "latitude": round(lat, 4), "longitude": round(lon, 4),
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                   "is_day,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "sunrise,sunset,precipitation_probability_max,uv_index_max",
        "hourly": "temperature_2m,weather_code,precipitation_probability",
        "timezone": "auto", "forecast_days": 4,
    })
    req = urllib.request.Request(f"https://api.open-meteo.com/v1/forecast?{q}",
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _light_phase(now: _dt.datetime, sunrise: str, sunset: str) -> tuple[str, list[str]]:
    """按日出日落判断光线阶段，并给出配图关键词。"""
    def hm(s):
        try:
            h, m = str(s).split("T")[1].split(":")[:2]
            return int(h) * 60 + int(m)
        except Exception:
            return None
    sr, ss = hm(sunrise), hm(sunset)
    cur = now.hour * 60 + now.minute
    if sr is None or ss is None:
        sr, ss = 6 * 60, 18 * 60
    if cur < sr - 45:
        return "夜色", ["夜景", "星空", "银河", "灯火", "城市夜景"]
    if cur < sr + 40:
        return "日出时分", ["日出", "朝霞", "晨曦", "晨雾", "金光", "黎明"]
    if cur < sr + 150:
        return "上午", ["晨光", "清新", "通透"]
    if cur < ss - 150:
        return "正午强光", ["正午", "阳光", "蓝天", "明媚", "晴空"]
    if cur < ss - 35:
        return "午后斜阳", ["斜阳", "暖光", "金光", "光影", "午后"]
    if cur < ss + 35:
        return "日落黄金时刻", ["日落", "黄昏", "暮色", "晚霞", "剪影", "夕阳", "金光"]
    if cur < ss + 95:
        return "蓝调时刻", ["暮色", "蓝调", "夜色", "灯火"]
    return "夜间", ["夜景", "星空", "银河", "灯火", "城市夜景"]


def _pack(raw: dict, loc: dict) -> dict:
    cur = raw.get("current") or {}
    daily = raw.get("daily") or {}
    code = cur.get("weather_code")
    text, icon, kw = describe(code)
    is_day = bool(cur.get("is_day", 1))
    now = _dt.datetime.now()

    def pick(key, idx=0):
        v = daily.get(key) or []
        return v[idx] if len(v) > idx else None

    sunrise, sunset = pick("sunrise"), pick("sunset")
    phase, lkw = _light_phase(now, sunrise, sunset)

    tomorrow = None
    if len(daily.get("time") or []) > 1:
        t_code = (daily.get("weather_code") or [None, None])[1]
        t_text, t_icon, _ = describe(t_code)
        tomorrow = {
            "text": t_text, "icon": t_icon,
            "max": (daily.get("temperature_2m_max") or [None, None])[1],
            "min": (daily.get("temperature_2m_min") or [None, None])[1],
        }

    return {
        "ok": True,
        "city": loc.get("city") or "",
        "locationFrom": loc.get("from") or "",
        "text": text,
        "icon": icon,
        "code": code,
        "isDay": is_day,
        "temp": cur.get("temperature_2m"),
        "feels": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind": cur.get("wind_speed_10m"),
        "precip": cur.get("precipitation"),
        "max": pick("temperature_2m_max"),
        "min": pick("temperature_2m_min"),
        "rainChance": pick("precipitation_probability_max"),
        "uv": pick("uv_index_max"),
        "sunrise": (sunrise or "")[11:16],
        "sunset": (sunset or "")[11:16],
        "phase": phase,
        "tomorrow": tomorrow,
        "keywords": list(dict.fromkeys(kw + lkw)),
        "updatedAt": now.isoformat(timespec="seconds"),
    }


def current(cfg: dict, force: bool = False) -> dict:
    key = "weather"
    if not force:
        cached = store.read_cache(key)
        if cached and cached.get("ok"):
            try:
                age = _dt.datetime.now() - _dt.datetime.fromisoformat(cached["updatedAt"])
                if age.total_seconds() < TTL:
                    return cached
            except (KeyError, ValueError):
                pass
    loc = locate(cfg)
    try:
        raw = _fetch(loc["lat"], loc["lon"])
        data = _pack(raw, loc)
        data["lat"], data["lon"] = loc["lat"], loc["lon"]
        store.write_cache(key, data)
        return data
    except Exception as exc:
        store.log(f"weather failed: {exc}")
        stale = store.read_cache(key)
        if stale and stale.get("ok"):
            stale["stale"] = True
            return stale
        return {"ok": False, "text": "天气获取失败", "icon": "cloud",
                "keywords": [], "city": loc.get("city") or ""}


def peek(cfg: dict) -> dict:
    """只读缓存、不联网。界面第一屏专用 —— 这就是「秒开」的关键。"""
    cached = store.read_cache("weather")
    if not (cached and cached.get("ok")):
        return {"ok": False, "text": "", "icon": "cloud", "keywords": [],
                "city": "", "stale": True, "ageSeconds": -1}
    try:
        age = (_dt.datetime.now()
               - _dt.datetime.fromisoformat(cached["updatedAt"])).total_seconds()
    except (KeyError, ValueError):
        age = 10 ** 9
    out = dict(cached)
    out["ageSeconds"] = int(age)
    out["stale"] = age >= TTL
    return out


_REFRESH_LOCK = threading.Lock()
_last_kick = 0.0


def refresh_async(cfg: dict, min_interval: int = 45, on_done=None) -> bool:
    """后台刷新天气。带节流：`min_interval` 秒内只会踢一次，
    所以界面就算每几秒轮询也不会把接口打爆。
    """
    global _last_kick
    with _REFRESH_LOCK:
        now = time.time()
        if now - _last_kick < min_interval:
            return False
        _last_kick = now

    def job():
        try:
            current(cfg, force=True)
        except Exception as exc:
            store.log(f"weather async refresh failed: {exc}")
        finally:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass

    threading.Thread(target=job, name="wb-weather", daemon=True).start()
    return True
