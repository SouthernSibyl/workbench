"""三个快捷启动动作的执行层。"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
import webbrowser
from pathlib import Path

import store

CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008

_NODE_CANDIDATES = [
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
]

_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _popen(args, cwd: str | None = None) -> None:
    flags = CREATE_NO_WINDOW
    try:
        subprocess.Popen(args, cwd=cwd or None, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        store.log(f"popen failed: {args} -> {exc}")
        raise


# ------------------------------------------------------------------ 通用命令

def run_shell(command: str, cwd: str = "") -> dict:
    """执行用户配置的启动命令。走 shell 以便支持引号路径与 && 串联。"""
    command = str(command or "").strip()
    if not command:
        return {"ok": False, "message": "该启动项还没有配置命令"}
    workdir = str(cwd or "").strip() or None
    if workdir and not Path(workdir).exists():
        workdir = None
    try:
        subprocess.Popen(command, cwd=workdir, shell=True,
                         creationflags=CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        store.log(f"run_shell failed: {command} -> {exc}")
        return {"ok": False, "message": f"启动失败：{exc}"}
    return {"ok": True, "message": "已发出启动指令"}


# -------------------------------------------------------------------- 读书站

def _probe_reader(port: int, timeout: float = 0.7) -> str | None:
    url = f"http://127.0.0.1:{port}/api/books"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200 and b"books" in resp.read(4096):
                return f"http://127.0.0.1:{port}"
    except Exception:
        return None
    return None


def find_node(node_dir: str = "") -> str | None:
    if node_dir:
        cand = Path(node_dir) / "node.exe"
        if cand.exists():
            return str(cand)
    for c in _NODE_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("node")


def open_url(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:
        store.log(f"open_url failed: {exc}")


def launch_ebook(cfg: dict, entry: dict) -> dict:
    port = int(entry.get("port") or 8787)
    for p in range(port, port + 21):
        hit = _probe_reader(p)
        if hit:
            open_url(hit)
            return {"ok": True, "message": f"读书站已在运行（{p}），已为你打开"}
    server = str(entry.get("server") or "").strip()
    books = str(entry.get("books") or "").strip()
    if not server or not Path(server).exists():
        return {"ok": False, "message": "找不到 reader-server.mjs，请检查配置"}
    node = find_node(str(entry.get("node_dir") or ""))
    if not node:
        return {"ok": False, "message": "找不到 node，请检查配置里的 node 目录"}
    args = [node, server]
    if books:
        args += ["--books", books]
    args += ["--port", str(port)]
    try:
        subprocess.Popen(args, cwd=str(Path(server).parent),
                         creationflags=CREATE_NO_WINDOW | DETACHED,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        return {"ok": False, "message": f"启动读书站失败：{exc}"}
    for _ in range(40):
        time.sleep(0.5)
        for p in range(port, port + 21):
            hit = _probe_reader(p, timeout=0.4)
            if hit:
                open_url(hit)
                return {"ok": True, "message": f"读书站已启动（{p}）"}
    return {"ok": False, "message": "读书站启动超时，请看 workbench.log"}


# ------------------------------------------------------------------ 统一入口

def launch(cfg: dict, entry: dict) -> dict:
    kind = str(entry.get("kind") or "file")
    if kind == "ebook":
        return launch_ebook(cfg, entry)
    if kind == "url":
        url = str(entry.get("url") or "").strip()
        if not url:
            return {"ok": False, "message": "该启动项还没有配置网址"}
        open_url(url)
        return {"ok": True, "message": "已打开"}
    if kind == "shell":
        return run_shell(entry.get("command") or "", entry.get("cwd") or "")
    return launch_path(entry.get("path") or entry.get("target") or "",
                       entry.get("args") or "", entry.get("cwd") or "")


def launch_path(path: str, args: str = "", cwd: str = "") -> dict:
    """打开任意文件 / 程序 / 文件夹 / 快捷方式。

    不带参数时一律交给系统关联去开（os.startfile）——
    .exe、.lnk、文件夹、文档、图片、视频都能正确处理，
    比自己解析命令行靠谱得多。
    """
    raw = str(path or "").strip().strip('"')
    if not raw:
        return {"ok": False, "message": "这个启动项还没绑定文件"}
    p = Path(raw).expanduser()
    if not p.exists():
        return {"ok": False, "message": f"文件不在了：{raw}"}

    arg_list = [a for a in str(args or "").replace(",", " ").split() if a]
    is_prog = p.suffix.lower() in (".exe", ".cmd", ".bat", ".com")
    if arg_list and is_prog:
        try:
            _popen([str(p), *arg_list], cwd or str(p.parent))
        except OSError as exc:
            return {"ok": False, "message": f"启动失败：{exc}"}
        return {"ok": True, "message": f"已启动 {p.name}"}
    try:
        os.startfile(str(p))            # noqa: S606 —— Windows 专用，走系统关联
    except OSError as exc:
        return {"ok": False, "message": f"打不开：{exc}"}
    if p.is_dir():
        return {"ok": True, "message": f"已打开文件夹 {p.name}"}
    return {"ok": True, "message": f"已打开 {p.name}"}


# ---------------------------------------------------------- 选文件用的目录浏览

QUICK_DIRS = [
    ("桌面", "{home}/Desktop"),
    ("下载", "{home}/Downloads"),
    ("文档", "{home}/Documents"),
    ("图片", "{home}/Pictures"),
    ("开始菜单", "%APPDATA%/Microsoft/Windows/Start Menu/Programs"),
    ("已装程序", "%LOCALAPPDATA%/Programs"),
]
_LIST_MAX = 400
_SKIP_FILES = {"desktop.ini", "thumbs.db", ".ds_store", "iconcache.db"}


def _quick_dirs() -> list[dict]:
    out = []
    for label, tmpl in QUICK_DIRS:
        raw = tmpl.replace("{home}", str(Path.home()))
        raw = os.path.expandvars(raw.replace("/", os.sep))
        p = Path(raw)
        if p.is_dir():
            out.append({"name": label, "path": str(p), "kind": "quick"})
    return out


def list_dir(raw: str = "") -> dict:
    """给「添加启动项」用的目录列表。

    raw 为空时返回磁盘根 + 常用位置；否则列出该目录下的子目录与文件。
    只读操作，不做任何写入。
    """
    if not str(raw or "").strip():
        roots = []
        for letter in "CDEFGH":
            p = Path(f"{letter}:/")
            if p.exists():
                roots.append({"name": f"{letter}: 盘", "path": str(p), "kind": "dir"})
        return {"path": "", "parent": "", "quick": _quick_dirs(),
                "dirs": roots, "files": []}

    p = Path(str(raw).strip().strip('"')).expanduser()
    if not p.is_dir():
        return {"ok": False, "message": f"不是文件夹：{p}", "path": str(p),
                "parent": "", "dirs": [], "files": [], "quick": []}

    dirs, files = [], []
    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except OSError as exc:
        return {"ok": False, "message": f"读不了这个目录：{exc}", "path": str(p),
                "parent": str(p.parent), "dirs": [], "files": [], "quick": []}

    for e in entries:
        if e.name.startswith(".") or e.name.startswith("~$"):
            continue
        if e.name.lower() in _SKIP_FILES:
            continue
        try:
            is_dir = e.is_dir()
        except OSError:
            continue
        if is_dir:
            if len(dirs) < _LIST_MAX:
                dirs.append({"name": e.name, "path": str(e), "kind": "dir"})
            continue
        ext = e.suffix.lower()
        if ext not in LAUNCH_EXTS:
            continue
        if len(files) < _LIST_MAX:
            files.append({"name": e.name, "path": str(e), "kind": "file", "ext": ext})
    return {"ok": True, "path": str(p), "parent": str(p.parent),
            "dirs": dirs, "files": files, "quick": [],
            "counts": {"dirs": len(dirs), "files": len(files)}}


# 值得出现在「可添加的快捷方式」里的类型
LAUNCH_EXTS = {".exe", ".lnk", ".bat", ".cmd", ".url", ".msc",
               ".txt", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
               ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
               ".mp3", ".flac", ".wav", ".mp4", ".mkv", ".avi", ".mov",
               ".zip", ".rar", ".7z", ".py", ".mjs", ".js", ".json", ".ini"}


def open_window(url: str, width: int = 1380, height: int = 880) -> dict:
    """用系统浏览器的「应用模式」开一个无地址栏的独立窗口。

    这个函数在启动关键路径上 —— 它返回之前用户什么都看不到，
    所以里面一件慢活都不能干（浏览器缓存的清理已挪去 deferred_sweep）。

    返回 {"exe":..., "pid":...|None, "fallback":bool}。
    `pid` 是那个窗口所属浏览器进程的进程号，交给看门狗当「窗口还在不在」的判据。

    ⚠️ 三个 --disable-* 参数是**必需**的，别删：
    Chromium 会把后台 / 被遮挡窗口的页面定时器节流甚至冻结（页面被回收，
    JS 完全不跑）。前端的 5 秒心跳就是这么断的 —— 心跳一断，
    看门狗就以为「窗口已经关了」把整个服务退掉，表现是
    「用着用着说连不上，重连没用，只能重启」。实测日志里连着自杀过 4 次。
    """
    profile = store.DATA_DIR / "browser"
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError:
        profile = None
    for exe in _EDGE_CANDIDATES:
        if not Path(exe).exists():
            continue
        args = [exe, f"--app={url}", f"--window-size={width},{height}",
                "--no-first-run", "--no-default-browser-check",
                # 别再往这个配置里下东西了
                "--disable-component-update", "--disable-background-networking",
                "--disable-sync", "--no-service-autorun", "--disable-default-apps",
                "--disable-client-side-phishing-detection",
                "--disable-domain-reliability", "--disable-breakpad",
                # 心跳保命三件套：不许节流后台定时器、不许因为窗口被遮挡就
                # 降级/冻结、不许冻结后台渲染进程
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disk-cache-size=52428800", "--media-cache-size=10485760",
                "--disable-features=" + ",".join([
                    "msEdgeSidebarV2", "msProvenanceData", "msEntityExtraction",
                    "msShoppingHub", "msWallet", "msSubresourceFilter",
                    "msEdgeShoppingAssistant", "msFeedback",
                    # Windows 的「窗口被遮挡」原生检测 —— 它会直接冻掉被盖住的页面
                    "CalculateNativeWinOcclusion",
                    # 别把页面丢进「睡眠标签页」：一睡就没心跳了
                    "msSleepingTabsEnabled", "msEdgeSleepingTabsEnabled",
                    "msSleepingTabsPolicy",
                ])]
        if profile and "msedge" in exe.lower():
            args.append(f"--user-data-dir={profile}")
        try:
            proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True)
            return {"exe": exe, "pid": proc.pid, "fallback": False}
        except OSError:
            continue
    open_url(url)
    return {"exe": "default-browser", "pid": None, "fallback": True}


_STILL_ACTIVE = 259

# 应用窗口的标题。页面 <title> 就是它，Edge 的应用模式窗口标题取自页面标题。
APP_WINDOW_TITLE = "工作台"


def window_alive(pid: int | None) -> bool | None:
    """⚠️ 别用它判断「窗口还在不在」。

    这是按**进程号**查活着的写法，看起来很直接，但实测会误判：
    Edge 会**交接** —— 我们 `open_window()` 起的那次 msedge.exe 会退出，
    窗口交给另一个浏览器进程托管。于是记录的 pid 早就没了，窗口却好好地开着，
    按 pid 判断就会把正在用的窗口当成「已关闭」把服务退掉。

    现在只留作诊断用。判断窗口请看 `window_open()` + `find_app_window()`。
    """
    if not pid:
        return None
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def find_app_window(title: str = APP_WINDOW_TITLE) -> int | None:
    """按标题找应用窗口的句柄。找不到返回 None。

    只看**可见**的顶层窗口 —— 无头浏览器（截图用）不产生可见窗口，不会误命中。
    纯 ctypes 调 user32，不碰 COM。
    """
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip() == title:
                found.append(int(hwnd))
                return False
            return True

        u32.EnumWindows(_cb, 0)
        return found[0] if found else None
    except Exception:
        return None


def window_open(hwnd: int | None) -> bool | None:
    """那个窗口句柄还有效吗（窗口是否还开着）。None = 没句柄可查。"""
    if not hwnd:
        return None
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsWindow(int(hwnd)))
    except Exception:
        return None


def deferred_sweep(delay: int = 25) -> None:
    """后台清理浏览器缓存。

    刻意延迟：一是别拖慢启动，二是等 Edge 把正在用的目录都摸过一遍，
    这样 `_sweep_browser_caches` 的「闲置一小时」判定才能正确放过它们。
    """
    try:
        time.sleep(max(0, delay))
        _sweep_browser_caches(store.DATA_DIR / "browser")
    except Exception as exc:
        store.log(f"deferred sweep failed: {exc}")


# 浏览器配置里「纯缓存 / 自动下载的数据，删了会自己长回来」的东西（目录或文件）
_BROWSER_CACHE_ITEMS = [
    "component_crx_cache", "ProvenanceData", "Edge Entity Extraction",
    "Edge Wallet", "Edge Shopping", "Subresource Filter", "Crowd Deny",
    "GrShaderCache", "ShaderCache", "Crashpad", "OptimizationGuidePredictionModels",
    "BrowserMetrics", "BrowserMetrics-spare.pma",
    "EdgeLanguageDetectionModel", "Speech Recognition", "hyphen-data",
    "SafetyTips", "WidevineCdm", "ZxcvbnData", "MEIPreload",
    "Default/Cache", "Default/Code Cache", "Default/GPUCache",
    "Default/DawnGraphiteCache", "Default/DawnWebGPUCache",
    "Default/Service Worker/CacheStorage", "Default/Service Worker/ScriptCache",
]
# 多久没被动过才算「没人用」，可以清
_IDLE_MINUTES = 60


def _sweep_browser_caches(profile: Path, older_than_minutes: int = _IDLE_MINUTES) -> int:
    """清掉闲置的浏览器缓存，返回释放的 MB。

    只有当它「一小时没人碰过」才动——正在使用的配置会被持续写入，
    这样就不会误删正在用的缓存。其余情况一概不碰。
    """
    cutoff = time.time() - older_than_minutes * 60
    freed = 0
    for rel in _BROWSER_CACHE_ITEMS:
        target = profile / rel
        if not target.exists():
            continue
        size = 0
        newest = 0.0
        try:
            if target.is_file():
                newest = target.stat().st_mtime
                size = target.stat().st_size
            else:
                for root, _dirs, files in os.walk(target):
                    for name in files:
                        try:
                            st = os.stat(os.path.join(root, name))
                        except OSError:
                            continue
                        newest = max(newest, st.st_mtime)
                        size += st.st_size
        except OSError:
            continue
        if newest > cutoff:
            continue                      # 还在用，放过
        try:
            if target.is_file():
                target.unlink(missing_ok=True)
            else:
                shutil.rmtree(target, ignore_errors=True)
            freed += size
        except OSError as exc:
            store.log(f"browser cache sweep failed {rel}: {exc}")
    if freed:
        store.log(f"browser cache swept: {freed / 1048576:.1f} MB")
    return int(freed / 1048576)
