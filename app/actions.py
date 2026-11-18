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

# 启动别人的程序时，必须从环境里摘掉的变量。
#
# `ELECTRON_RUN_AS_NODE=1` 是最要命的一个：Electron 应用（Trae、VS Code、Qoder、
# WorkBuddy 自己）一旦看到它，就以**纯 Node 运行时**启动 —— 不建窗口、不读
# resources、不写日志，几十秒后静默退出（退出码 0）。表现就是「点了没反应」，
# 而且日志里还是「已打开」。
#
# 这个变量不是用户设的，是从「启动工作台的那个终端」继承下来的：只要工作台
# 是从一个 Electron 应用的子进程里起的（比如在某个 IDE 的终端里跑），
# 它启动的所有 Electron 程序都会变成哑的。2026-11-18 实测：
#   explorer 双击 → 60MB / 17 线程 / 正常出窗口
#   带 ELECTRON_RUN_AS_NODE=1 → 32MB / 1 线程 / 20 秒后退出
#
# VSCODE_* 是同一族的：VS Code 的 fork 看到 IPC 钩子会以为自己是被某个 IDE
# 拉起的 CLI 子进程，去连一个不存在的管道然后放弃。
_STRIP_ENV = frozenset({
    "ELECTRON_RUN_AS_NODE",
    "ELECTRON_NO_ATTACH_CONSOLE",
    "ELECTRON_FORCE_IS_PACKAGED",
    "VSCODE_IPC_HOOK",
    "VSCODE_IPC_HOOK_CLI",
    "VSCODE_IPC_HOOK_EXTHOST",
    "VSCODE_PID",
    "VSCODE_CWD",
    "VSCODE_DEV",
    "VSCODE_NLS_CONFIG",
    "VSCODE_CODE_CACHE_PATH",
    # 启动 .py 启动项时，父进程的 Python 环境会把它带歪
    "PYTHONHOME",
    "PYTHONPATH",
})


def clean_env() -> dict:
    """给「被启动的程序」一份干净的环境变量。

    只摘掉会改变目标程序行为的那几个（见 `_STRIP_ENV`），其余原样传下去 ——
    用户自己设的代理、PATH、语言设置都还在。
    """
    env = dict(os.environ)
    for key in _STRIP_ENV:
        env.pop(key, None)
    return env


_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _popen(args, cwd: str | None = None) -> subprocess.Popen:
    """拉起一个 GUI 程序：干净环境 + 独立进程组，不等它。

    `close_fds=True` 之外没什么可做的 —— GUI 程序的创建本身只要几十毫秒，
    真正的初始化是它自己的事，不该让界面的请求等它。
    """
    try:
        return subprocess.Popen(args, cwd=cwd or None, env=clean_env(),
                                creationflags=CREATE_NO_WINDOW,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        store.log(f"popen failed: {args} -> {exc}")
        raise


def _died_immediately(proc: subprocess.Popen, wait: float = 0.45) -> str | None:
    """等一小会儿看它有没有当场死掉，死了就把退出码报上来。

    只抓「立刻退出」这一种（缺 DLL、路径不对、单实例转发失败）。
    Electron 冷启动十几秒是正常的，不能等它 —— 那是把「慢」误判成「坏」。
    """
    try:
        code = proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    return f"程序启动后立刻退出了（退出码 {code}）"


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
        subprocess.Popen(command, cwd=workdir, shell=True, env=clean_env(),
                         creationflags=CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        store.log(f"run_shell failed: {command} -> {exc}")
        return {"ok": False, "message": f"启动失败：{exc}"}
    return {"ok": True, "message": "已发出启动指令"}


# ------------------------------------------------------------------ 本地服务

def _probe_reader(port: int, timeout: float = 0.7) -> str | None:
    """这个端口上是不是已经跑着我想要的本地服务了。

    「已经在跑就直接打开，别再起一个」—— 重复启动 node 服务会抢端口失败，
    用户看到的是一个不报错也不打开的按钮。
    """
    url = f"http://127.0.0.1:{port}/api/books"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200 and b"books" in resp.read(4096):
                return f"http://127.0.0.1:{port}"
    except Exception:
        return None
    return None


_NODE_CANDIDATES = [
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
]


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


# ------------------------------------------------------------------ 统一入口

def launch(cfg: dict, entry: dict) -> dict:
    kind = str(entry.get("kind") or "file")
    if kind == "url":
        url = str(entry.get("url") or "").strip()
        if not url:
            return {"ok": False, "message": "该启动项还没有配置网址"}
        open_url(url)
        return {"ok": True, "message": "已打开"}
    if kind == "shell":
        return run_shell(entry.get("command") or "", entry.get("cwd") or "")
    if kind in ("node", "ebook"):
        return launch_node(entry)
    return launch_path(entry.get("path") or entry.get("target") or "",
                       entry.get("args") or "", entry.get("cwd") or "")


def launch_node(entry: dict) -> dict:
    """跑一个本地 node 脚本并打开它的网址（读书站就是这一类）。

    1.0.0 把这套逻辑整段删了（当时的理由是「只有本机有」），
    结果配置里那条 `kind: ebook` 变成了「这个启动项还没绑定文件」——
    点得动、看不懂。这里按「通用 node 服务」重新实现：
    脚本路径、node 目录、端口都从配置读，任何人的 node 项目都能这么配。
    """
    port = int(entry.get("port") or 8787)
    # 只探前后两个端口：读书站这类服务的端口是固定的，21 连探在
    # 防火墙丢包的机器上要白等十几秒
    for p in range(port, port + 3):
        hit = _probe_reader(p)
        if hit:
            open_url(hit)
            return {"ok": True, "message": f"已经在运行（{p}），已为你打开"}
    script = str(entry.get("server") or "").strip()
    if not script or not Path(script).exists():
        return {"ok": False, "message": "找不到要启动的脚本，请重新配置这一项"}
    node = find_node(str(entry.get("node_dir") or ""))
    if not node:
        return {"ok": False, "message": "找不到 node，请重新配置这一项"}
    args = [node, script]
    books = str(entry.get("books") or "").strip()
    if books:
        args += ["--books", books]
    args += ["--port", str(port)]
    try:
        proc = _popen(args, cwd=str(Path(script).parent))
    except OSError as exc:
        return {"ok": False, "message": f"启动失败：{exc}"}
    dead = _died_immediately(proc, wait=0.9)   # node 起服务比 GUI 快，等久一点
    if dead:
        return {"ok": False, "message": dead}
    open_url(f"http://127.0.0.1:{port}")
    return {"ok": True, "message": "已启动"}


def launch_path(path: str, args: str = "", cwd: str = "") -> dict:
    """打开任意文件 / 程序 / 文件夹 / 快捷方式。

    可执行文件（exe/cmd/bat/com）走 Popen，其余交给系统关联。
    以前是「无参数就一律 os.startfile」，那条路有两个必坏的点：
    `ShellExecute` 不让我们控制环境变量，也不保证工作目录是程序自己那层。
    而 macOS/Linux 上 os.startfile 根本不存在 —— 那部分是给分发版留的。
    """
    raw = str(path or "").strip().strip('"')
    if not raw:
        return {"ok": False, "message": "这个启动项还没绑定文件"}
    p = Path(raw).expanduser()
    if not p.exists():
        return {"ok": False, "message": f"文件不在了：{raw}"}

    arg_list = [a for a in str(args or "").replace(",", " ").split() if a]
    is_prog = p.suffix.lower() in (".exe", ".cmd", ".bat", ".com")

    # 可执行文件一律自己拉起来，**不**交给 ShellExecute。
    # 两个原因（2026-11-18 实测）：
    #   1. ShellExecute 用调用者的环境变量 —— 父进程里若有 ELECTRON_RUN_AS_NODE=1，
    #      Trae / VS Code 这类会被启动成纯 Node 进程：不建窗口、不写日志，
    #      20 秒后安静退出，界面还显示「已打开」。
    #   2. ShellExecute 的默认工作目录是调用者的 cwd，不是程序自己那层。
    # 这两条都是「点了没反应」的成因，而不带参数恰恰是最常见的用法。
    if is_prog:
        if p.suffix.lower() in (".cmd", ".bat"):
            # 批处理不是 PE 文件，CreateProcess 直接拉起会 193 报错，
            # 必须先过 cmd.exe。/c 执行完就退，不等脚本里 start 出去的东西。
            try:
                _popen(["cmd", "/c", str(p), *arg_list], cwd or str(p.parent))
            except OSError as exc:
                return {"ok": False, "message": f"启动失败：{exc}"}
            return {"ok": True, "message": f"已执行 {p.name}"}
        try:
            proc = _popen([str(p), *arg_list], cwd or str(p.parent))
        except OSError as exc:
            return {"ok": False, "message": f"启动失败：{exc}"}
        dead = _died_immediately(proc)
        if dead:
            return {"ok": False, "message": dead}
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


def browser_exe() -> str | None:
    """能开「应用模式窗口」的 Chromium 内核浏览器（Edge 优先）。"""
    for exe in _EDGE_CANDIDATES:
        if Path(exe).exists():
            return exe
    return None


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
            proc = subprocess.Popen(args, env=clean_env(),
                                    creationflags=CREATE_NO_WINDOW,
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


def focus_app_window(title: str = APP_WINDOW_TITLE) -> bool:
    """把已经开着的主窗提到前台（最小化的先还原）。纯 ctypes，不碰 COM。"""
    hwnd = find_app_window(title)
    if not hwnd:
        return False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        if u32.IsIconic(int(hwnd)):
            u32.ShowWindow(int(hwnd), 9)        # SW_RESTORE
        u32.SetForegroundWindow(int(hwnd))
        return True
    except Exception:
        return False


def window_open(hwnd: int | None) -> bool | None:
    """那个窗口句柄还有效吗（窗口是否还开着）。None = 没句柄可查。"""
    if not hwnd:
        return None
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsWindow(int(hwnd)))
    except Exception:
        return None


def deferred_sweep_mini(delay: int = 32) -> None:
    """小窗那份浏览器配置也清一遍（它和图库无关，纯粹是 Chromium 的缓存）。"""
    try:
        time.sleep(max(0, delay))
        _sweep_browser_caches(store.DATA_DIR / "browser-mini")
    except Exception as exc:
        store.log(f"mini sweep failed: {exc}")


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
