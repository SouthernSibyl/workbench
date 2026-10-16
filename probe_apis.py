"""探针：天气 / 图片 / 新闻源，全并行、短超时。"""
import ssl
import urllib.request
import concurrent.futures as cf

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def probe(item):
    name, url = item
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=6, context=CTX) as r:
            data = r.read(1200)
            return name, True, f"{r.status} {len(data)}B {(r.headers.get('Content-Type') or '')[:38]}"
    except Exception as exc:
        return name, False, f"{type(exc).__name__}: {str(exc)[:64]}"


GROUPS = {
    "天气": [
        ("ip-api 定位", "http://ip-api.com/json/?lang=zh-CN"),
        ("ipapi.co 定位", "https://ipapi.co/json/"),
        ("OpenMeteo 地理编码", "https://geocoding-api.open-meteo.com/v1/search?name=Beijing&count=1&language=zh"),
        ("OpenMeteo 天气", "https://api.open-meteo.com/v1/forecast?latitude=39.9&longitude=116.4&current=temperature_2m,weather_code&timezone=auto"),
        ("wttr.in", "https://wttr.in/Beijing?format=j1"),
    ],
    "图片": [
        ("Commons 搜索", "https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=search&gsrsearch=filetype:bitmap%20landscape%20fog&gsrnamespace=6&gsrlimit=5&prop=imageinfo&iiprop=url&iiurlwidth=1200"),
        ("Commons 特色图", "https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=categorymembers&gcmtitle=Category:Featured_pictures_of_landscapes&gcmtype=file&gcmlimit=5&prop=imageinfo&iiprop=url&iiurlwidth=1200"),
        ("Commons 分类成员", "https://commons.wikimedia.org/w/api.php?action=query&format=json&list=categorymembers&cmtitle=Category:Quality_images_of_plants&cmtype=file&cmlimit=5"),
        ("Bing 每日壁纸", "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=3&mkt=zh-CN"),
        ("Pexels 无key", "https://api.pexels.com/v1/search?query=landscape"),
        ("Openverse", "https://api.openverse.org/v1/images/?q=landscape&page_size=3"),
        ("Wallhaven", "https://wallhaven.cc/api/v1/search?q=landscape&categories=100&purity=100&sorting=toplist&ratios=16x9"),
        ("Wikimedia 大图直连", "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e0/Title.jpg/640px-Title.jpg"),
    ],
    "新闻": [
        ("新华网-时政", "http://www.xinhuanet.com/politics/news_politics.xml"),
        ("新华网-国际", "http://www.xinhuanet.com/world/news_world.xml"),
        ("人民网-时政", "http://www.people.com.cn/rss/politics.xml"),
        ("人民网-经济", "http://www.people.com.cn/rss/finance.xml"),
        ("中新网-滚动", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
        ("中新网-国内", "https://www.chinanews.com.cn/rss/china.xml"),
        ("央视-新闻", "http://news.cctv.com/rss/china.xml"),
        ("澎湃新闻", "https://www.thepaper.cn/rss_newsDetail_25950.xml"),
        ("财新网", "https://www.caixin.com/rss/all.xml"),
        ("36氪", "https://36kr.com/feed"),
        ("虎嗅", "https://www.huxiu.com/rss/0.xml"),
        ("IT之家", "https://www.ithome.com/rss/"),
        ("少数派", "https://sspai.com/feed"),
        ("Solidot", "https://www.solidot.org/index.rss"),
        ("arXiv cs.AI", "http://export.arxiv.org/rss/cs.AI"),
        ("Nature", "https://www.nature.com/nature.rss"),
        ("科学网", "http://www.sciencenet.cn/xml/news.aspx?di=0"),
        ("知乎日报", "https://www.zhihu.com/rss"),
        ("联合早报-中国", "https://www.zaobao.com/realtime/china/rss.xml"),
        ("界面新闻", "https://a.jiemian.com/index.php?m=article&a=rss"),
    ],
}

ALL = [(g, n, u) for g, items in GROUPS.items() for n, u in items]
with cf.ThreadPoolExecutor(max_workers=24) as ex:
    futs = {ex.submit(probe, (n, u)): (g, n) for g, n, u in ALL}
    results = {}
    for f in cf.as_completed(futs):
        g, n = futs[f]
        results.setdefault(g, []).append(f.result())

for g in GROUPS:
    print(f"========== {g} ==========")
    for n, ok, msg in sorted(results.get(g, []), key=lambda x: (not x[1], x[0])):
        print(("  OK  " if ok else " FAIL ") + n.ljust(16) + " " + msg)
