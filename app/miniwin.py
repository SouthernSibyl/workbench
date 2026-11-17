"""待办小窗：一个独立窗口，可以「钉」在别的窗口上面。

实现方式是 **Edge / Chrome 的应用模式窗口**，不是自己画的窗口 ——
所以不用引入任何 GUI 依赖，通知也走同一套系统能力。真正的原生部分都用
ctypes 直接调 user32 完成：

  1. 判断窗口在不在（`IsWindow` + 按标题找句柄）—— 不能用进程号，
     Chromium 会把窗口交接给另一个进程，盯进程号必然误判（主窗口踩过这个坑）；
  2. 图钉 = `SetWindowPos(HWND_TOPMOST)`，解开 = `HWND_NOTOPMOST`。
     Chromium 在窗口被激活时会自己重排 z 序，所以由 keeper 线程每秒复查一次；
  3. 位置记忆：`GetWindowRect` 读到就存，下次开窗带 `--window-position` 回来；
  4. **不在任务栏占位**：加 `WS_EX_TOOLWINDOW`。任务栏按钮和 Alt+Tab 都会消失，
     但窗口本身、置顶、独立性都不变（这是 PowerToys Run / DeskPins 那类
     「桌面小组件」的标准做法）；
  5. **高度跟着内容走**：页面量好自己的内容高度报上来，`set_size()` 改窗口。
     用户一旦手动拖过大小，就从此听用户的（`STATE["auto"]` 关掉）。
"""

from __future__ import annotations

import ctypes
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

import actions
import store

MINI_TITLE = "待办小窗"
CORNER_MARGIN = 26      # 首次开窗离屏幕边沿留多远（_corner_position 也用这个）

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WM_CLOSE = 0x0010
SW_SHOW = 5
SW_RESTORE = 9

# 进程内共享的小窗状态：接口改它、keeper 读它
#   auto：高度是否跟着内容走。用户手动拖过大小就置 False，从此听用户的。
STATE = {"pinned": True, "open": False, "rect": None, "opened": 0.0,
         "auto": True, "tool": False}

_LOCK = threading.Lock()
_SAVED = {"rect": None, "at": 0.0}   # 上次落盘的位置与时间（防抖）
_LAST_SET: tuple[int, int, int, int] | None = None   # 我们最后一次设的 (x,y,w,h)


# ------------------------------------------------------------------ 基础封装

def _u32():
    return ctypes.windll.user32


def find_window(title: str = MINI_TITLE) -> int | None:
    """按标题找小窗句柄。找不到 = 小窗没开。"""
    try:
        return actions.find_app_window(title)
    except Exception:
        return None


def is_open() -> bool:
    return bool(find_window())


# --------------------------------------------------------------- 样式与尺寸

_LONGPTR = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long


def _get_ex(hwnd: int) -> int:
    u = _u32()
    u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowLongPtrW.restype = _LONGPTR
    return int(u.GetWindowLongPtrW(ctypes.c_void_p(int(hwnd)), GWL_EXSTYLE)) & 0xFFFFFFFF


def _set_ex(hwnd: int, value: int) -> bool:
    u = _u32()
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, _LONGPTR]
    u.SetWindowLongPtrW.restype = _LONGPTR
    ok = u.SetWindowLongPtrW(ctypes.c_void_p(int(hwnd)), GWL_EXSTYLE, int(value) & 0xFFFFFFFF)
    return int(ok) != 0


def work_area() -> tuple[int, int, int, int]:
    """桌面工作区（扣掉任务栏）。DPI 不感知进程拿到的是虚拟化坐标，自洽即可。"""
    try:
        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = _RECT()
        if not _u32().SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return (0, 0, _u32().GetSystemMetrics(0), _u32().GetSystemMetrics(1))
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        return (0, 0, 1600, 900)


def make_tool_window(hwnd: int) -> bool:
    """让小窗不进任务栏、不进 Alt+Tab。

    为什么不用「把 owner 设成主窗」那种真正的合并：owner 窗口会在主窗关闭时
    被一起销毁，而「主窗关了小窗还在」是早就定下的行为（看门狗也照着写的）。
    工具窗两边都占：不占任务栏，也不连坐。
    """
    if not hwnd:
        return False
    try:
        ex = _get_ex(hwnd)
        if ex & WS_EX_TOOLWINDOW:
            STATE["tool"] = True
            return True
        if not _set_ex(hwnd, ex | WS_EX_TOOLWINDOW):
            store.log(f"mini: 设工具窗样式失败 hwnd={hwnd}")
            return False
        # 改了样式必须让边框重算，否则任务栏按钮不会消失
        rc = rect(hwnd)
        if rc:
            x, y, w, h = rc
            _setpos_xy(hwnd, HWND_TOPMOST if (_get_ex(hwnd) & WS_EX_TOPMOST) else 0,
                       x, y, w, h, SWP_FRAMECHANGED | SWP_NOACTIVATE)
        STATE["tool"] = True
        store.log(f"mini: 已设为工具窗（不进任务栏 / Alt+Tab）hwnd={hwnd}")
        return True
    except Exception as exc:
        store.log(f"mini: 工具窗样式异常 {exc}")
        return False


def _setpos_xy(hwnd: int, insert_after: int, x: int, y: int, w: int, h: int,
               extra_flags: int = 0) -> bool:
    u = _u32()
    u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.SetWindowPos.restype = ctypes.c_int
    return bool(u.SetWindowPos(ctypes.c_void_p(int(hwnd)),
                               ctypes.c_void_p(insert_after & 0xFFFFFFFFFFFFFFFF),
                               int(x), int(y), int(w), int(h),
                               SWP_NOACTIVATE | extra_flags))


def set_size(h: int, w: int = 0, hwnd: int | None = None, natural: int = 0) -> dict:
    """把窗口改成给定大小。

    h —— 内容高度，页面量好报上来（唯一的自变量）。
    w —— 传 0 就用配置里的宽度。**宽度不跟着内容走**：文字会跟着换行，
         一换行宽度又变，越缩越小（实测从 360 一路缩到 318）。
         宽度只能由设置里改，或者用户自己拖。

    位置：这是「挂在屏幕底边」的小组件，所以**底边贴住工作区下沿** ——
    长高往上长、变矮往下收；x 不动（用户把它拖到哪就留在哪）。
    """
    hwnd = hwnd or find_window()
    left, top, aw, ah = work_area()
    if w and int(w) > 0:
        w = max(260, min(800, int(w)))
    else:
        w, _h0 = _sane_size((store.load_config().get("mini") or {}).get("width"), None)
    h = max(150, min(int(ah) - 60, int(h)))
    nat = int(natural) if natural and 0 < natural < h else h
    if not hwnd:
        # 窗口没开也照样收下：写进配置，下次开窗就按这个来。
        # 否则「页面刚报完尺寸、用户紧接着关了窗」会把这次上报丢掉。
        try:
            store.mutate_mini(width=w, height=nat)
        except Exception:
            pass
        return {"ok": True, "open": False, "rect": [None, None, w, h],
                "message": "小窗没开，尺寸已记住"}
    rc = rect(hwnd)
    if not rc:
        return {"ok": False, "message": "读不到窗口位置"}
    x, y = rc[0], rc[1]
    # 位置怎么跟着高度变：
    #  · 原本就贴着屏幕下沿（默认就摆在那儿）→ 底边不动，往上长。
    #    这是「挂在角落的小组件」该有的样子：长高了往上顶，底边一直在原处。
    #  · 用户自己挪过（底边不贴边）→ 顶边也不动，就地往下长。
    #    之前不管哪种都强行贴到屏幕下沿，结果在小窗里加一条待办，
    #    窗口就被「拽」回屏幕底部 —— 用户挪到哪都白挪。
    anchor = top + ah - CORNER_MARGIN          # 角落锚点：离工作区下沿留一点边距
    if abs((rc[1] + rc[3]) - anchor) <= 12:
        y = anchor - h                          # 贴着角落：往上长，底边不动
    if y + h > top + ah:
        y = top + ah - h
    if x + w > left + aw:
        x = left + aw - w
    if x < left:
        x = left
    ok = _setpos_xy(hwnd, HWND_TOPMOST if (_get_ex(hwnd) & WS_EX_TOPMOST) else 0,
                    x, y, w, h, SWP_NOZORDER)
    global _LAST_SET
    _LAST_SET = (x, y, w, h)
    if ok:
        STATE["rect"] = (x, y, w, h)
        # 第 4 个是实际高度，第 5 个是「去掉弹层后的内容高度」——两个都要记
        _persist_rect((x, y, w, h, nat))
    store.log(f"mini: 改大小 {w}x{h} @{x},{y} ok={ok}（自然高 {nat}）")
    return {"ok": ok, "rect": [x, y, w, h]}


def max_list_px() -> int:
    """列表最高能长到多少（工作区高度的 40%）。

    这个数由后端算、前端只用来设 `--list-max`：**不能用 vh**，
    因为窗口高度本身由内容决定，用 vh 会绕回去自己参考自己。
    """
    _l, _t, _w, ah = work_area()
    return max(160, int(ah * 0.40))


def _topmost(hwnd: int) -> bool | None:
    try:
        ex = _u32().GetWindowLongW(int(hwnd), GWL_EXSTYLE)
        return bool(ex & WS_EX_TOPMOST)
    except Exception:
        return None


def _setpos(hwnd: int, insert_after: int) -> bool:
    """SetWindowPos 的正确调法。

    ⚠️ 这里必须把两个窗口参数声明成 `c_void_p` 并**显式传满位值**（HWND_TOPMOST = -1 →
    0xFFFF…FF）。用 `wintypes.HWND(-1)` 这种写法实测会被转换坏：函数照样返回成功，
    但窗口的 WS_EX_TOPMOST 位根本没加上 —— 表现就是「点了图钉没反应」，
    而且从返回值上完全看不出来。
    """
    u = _u32()
    u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.SetWindowPos.restype = ctypes.c_int
    return bool(u.SetWindowPos(ctypes.c_void_p(int(hwnd)),
                               ctypes.c_void_p(insert_after & 0xFFFFFFFFFFFFFFFF),
                               0, 0, 0, 0,
                               SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))


def set_topmost(hwnd: int, on: bool = True) -> bool:
    """图钉本身。成功返回 True（并复查一次扩展样式，确认真的钉上了）。"""
    try:
        ok = _setpos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST)
        if not ok:
            store.log(f"mini: SetWindowPos 失败 hwnd={hwnd} on={on}")
            return False
        now = _topmost(hwnd)
        if now is not None and now != bool(on):
            store.log(f"mini: 置顶设置没落地 hwnd={hwnd} 期望={on} 实际={now}")
            return False
        return True
    except Exception as exc:
        store.log(f"mini topmost failed: {exc}")
        return False


def rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口的 (x, y, 宽, 高)。"""
    try:
        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = _RECT()
        if not _u32().GetWindowRect(int(hwnd), ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        return None


def focus(hwnd: int | None = None) -> bool:
    """把小窗提到前台（点通知、点主窗上的小窗按钮时用）。"""
    hwnd = hwnd or find_window()
    if not hwnd:
        return False
    try:
        u = _u32()
        if u.IsIconic(int(hwnd)):
            u.ShowWindow(int(hwnd), SW_RESTORE)
        else:
            u.ShowWindow(int(hwnd), SW_SHOW)
        u.SetForegroundWindow(int(hwnd))
        return True
    except Exception:
        return False


def close() -> bool:
    """关掉小窗（发 WM_CLOSE，等于用户点右上角的叉）。"""
    hwnd = find_window()
    if not hwnd:
        return False
    try:
        return bool(_u32().PostMessageW(int(hwnd), WM_CLOSE, 0, 0))
    except Exception as exc:
        store.log(f"mini close failed: {exc}")
        return False


# -------------------------------------------------------------------- 开窗

def _on_screen(x: int, y: int, w: int = 0, h: int = 0) -> bool:
    """上次的位置还落在某个显示器里吗。

    只看左上角不够：260 宽的小窗放在 1421 时，右边已经出屏了。
    所以整块矩形都得在虚拟桌面里（允许贴边），否则就当没记过位置。
    """
    try:
        u = _u32()
        left, top = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        right, bottom = left + u.GetSystemMetrics(78), top + u.GetSystemMetrics(79)
        return (x >= left - 8 and y >= top - 8
                and x + max(w, 200) <= right + 8
                and y + max(h, 120) <= bottom + 8)
    except Exception:
        return True


def _sane_size(raw_w, raw_h) -> tuple[int, int]:
    """尺寸要「像个窗口」。

    踩过的坑：窗口最小化时 `GetWindowRect` 会返回 -21333,-21333 / 158×26 这种垃圾值，
    被位置记忆存下来，下次开窗就变成一条 158×26 的缝。
    """
    try:
        w, h = int(raw_w or 0), int(raw_h or 0)
    except (TypeError, ValueError):
        return 360, 560
    if w < 200 or h < 120:          # 小得离谱 = 最小化时的垃圾值
        return 360, 560
    return max(280, min(800, w)), max(360, min(1200, h))


def _bogus_rect(rc: tuple[int, ...]) -> bool:
    """这个矩形是不是假值（最小化时读到的 -21333,-21333 / 158×26 那种）。

    阈值只拦「根本不是窗口」的：画外坐标、或者小得离谱。
    早先按 280×360 拦（照着最小化时的 158×26 定的），结果合法的
    360×334 小窗也被当成垃圾 —— 位置永远存不进去，下一次开窗又回老地方。
    """
    x, y, w, h = rc[0], rc[1], rc[2], rc[3]
    return x <= -10000 or y <= -10000 or w < 200 or h < 120


def _corner_position(width: int, height: int, margin: int = CORNER_MARGIN) -> tuple[int, int]:
    """第一次开窗的落点：工作区右下角。

    小窗的定位就是「桌面角落的一个小面板」，第一次开不该让它出现在屏幕正中间
    盖住你在看的东西。之后位置由用户拖动决定，再也不会自动挪。
    """
    try:
        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = _RECT()
        # SPI_GETWORKAREA = 0x0030，扣掉任务栏之后剩下的区域
        if not _u32().SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return 0, 0
        return (max(r.left, r.right - width - margin),
                max(r.top, r.bottom - height - margin))
    except Exception:
        return 0, 0


def app_window_args(edge: str, url: str, width: int, height: int, pos: str,
                    profile) -> list[str]:
    """Edge/Chrome「应用模式窗口」的启动参数。

    单独抽出来是为了保留一份：那串 --disable-* 是防 Chromium 在后台节流
    定时器的（主窗为这件事踩过 4 次坑），参数抄一份到别处只会两处一起烂。
    """
    args = [edge, f"--app={url}", f"--window-size={width},{height}"]
    if pos:
        args.append(pos)
    args += [
        "--no-first-run", "--no-default-browser-check",
        "--disable-component-update", "--disable-background-networking",
        "--disable-sync", "--no-service-autorun", "--disable-default-apps",
        "--disable-client-side-phishing-detection", "--disable-domain-reliability",
        "--disable-breakpad", "--disable-search-engine-choice-screen",
        # 小窗的命就是「一直在旁边盯着」：被遮挡、失焦都不许节流定时器，
        # 否则提醒会晚到几分钟（主窗口为这件事踩过 4 次坑，见 actions.open_window）
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=" + ",".join([
            "CalculateNativeWinOcclusion", "msSleepingTabsEnabled",
            "msEdgeSleepingTabsEnabled", "msSleepingTabsPolicy",
            "msEdgeSidebarV2", "msEdgeShoppingAssistant", "msFeedback",
        ]),
        "--disk-cache-size=52428800", "--media-cache-size=10485760",
    ]
    if profile:
        args.append(f"--user-data-dir={profile}")
    return args


def open_mini(port: int, cfg: dict | None = None) -> dict:
    """开小窗。已经开着就把它提到前面，不开第二个。"""
    cfg = cfg or store.load_config()
    if find_window():
        focus()
        return {"ok": True, "already": True, "message": "小窗已经在桌面上了"}

    mini = cfg.get("mini") or {}
    width, height = _sane_size(mini.get("width"), mini.get("height"))
    # 开窗高度优先用「上次的内容自然高度」：mini.height 记的是窗口实际高度，
    # 可能被一次过期的落盘写歪（关窗时正好有一次延迟落盘，就会写回旧值）。
    # content_h 是页面量出来「去掉弹层后内容该有多高」，开窗直接用它，不会先大后小。
    _ch = mini.get("content_h")
    if isinstance(_ch, (int, float)) and 150 <= int(_ch) <= 1200:
        height = int(_ch)
    pos = ""
    x, y = None, None
    try:
        x, y = mini.get("x"), mini.get("y")
    except (TypeError, ValueError):
        x, y = None, None
    # 记过位置就用记的；第一次开就摆到工作区右下角
    if not (isinstance(x, (int, float)) and isinstance(y, (int, float))
            and _on_screen(int(x), int(y), width, height)):
        x, y = _corner_position(width, height)
    if x is not None and y is not None:
        pos = f"--window-position={int(x)},{int(y)}"

    url = f"http://127.0.0.1:{port}/mini?theme={cfg.get('theme') or 'light'}"
    profile = store.DATA_DIR / "browser-mini"
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError:
        profile = None

    edge = actions.browser_exe()
    if not edge:                                    # 没有 Chromium 就退回默认浏览器
        actions.open_url(url)
        return {"ok": True, "fallback": True, "message": "没找到 Edge，已用默认浏览器打开"}

    args = app_window_args(edge, url, width, height, pos, profile)
    try:
        subprocess.Popen(args, creationflags=actions.CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        store.log(f"mini open failed: {exc}")
        return {"ok": False, "message": f"打不开小窗：{exc}"}

    STATE["opened"] = time.time()
    store.log(f"mini: 开窗 {width}x{height} {pos or '(默认位置)'}")
    # **不在这儿等窗口**：浏览器启动 + 页面加载要 ~0.8 秒，等于把点击按钮的主窗
    # 卡住 0.8 秒（用户说「小窗冷启动很慢」，慢的一半在这儿）。
    # 现在立刻返回，后台线程等窗口真的出现，再做工具窗样式和图钉。
    threading.Thread(target=_await_window, name="wb-mini-open", daemon=True).start()
    return {"ok": True, "open": False, "pinned": bool(STATE["pinned"]),
            "message": "小窗正在打开…"}


def _await_window() -> None:
    """等小窗窗口出现，然后补上：工具窗样式 → 图钉。

    单独放后台线程，是为了不占住发起开窗的那个请求。
    """
    hwnd = None
    for _ in range(40):                       # 最多等 10 秒
        time.sleep(0.25)
        hwnd = find_window()
        if hwnd:
            break
    if not hwnd:
        store.log("mini: 等了 10 秒还没等到窗口")
        return
    STATE["open"] = True
    make_tool_window(hwnd)               # 先摘掉任务栏按钮，再钉
    if STATE["pinned"]:
        set_topmost(hwnd, True)
    store.log(f"mini: 窗口就绪 hwnd={hwnd}")


# ------------------------------------------------------------- 图钉与记忆

def set_pin(on: bool) -> dict:
    """改图钉状态：立刻生效，并记住（下次开窗还是这个状态）。"""
    STATE["pinned"] = bool(on)
    store.mutate_mini(pinned=bool(on))
    hwnd = find_window()
    ok = True
    if hwnd:
        ok = set_topmost(hwnd, bool(on))
    store.log(f"mini: 图钉 {'钉住' if on else '解下'}（窗口{'在' if hwnd else '不在'}）")
    return {"ok": ok, "pinned": bool(on),
            "message": ("已钉在最上层" if on else "已取消置顶") if hwnd else "已记住，下次开窗生效"}


def _persist_rect(rc: tuple[int, ...]) -> None:
    """位置/尺寸落盘。

    拖动的时候别一边拖一边写配置，所以延后 6 秒再落 —— 但**必须保证最后那一次
    改动会被落下来**。早先的写法是「6 秒内的改动直接丢掉」，结果最后一次改动
    永远存不进配置（窗口挪到最后那个位置，等于没记）。
    现在改成：记下待存的矩形，6 秒内没有新改动就把它写掉。
    """
    if _bogus_rect(rc):
        return                      # 最小化 / 刚创建时读到的垃圾值，不记
    if rc == _SAVED["rect"]:
        return
    _SAVED["rect"] = rc
    _SAVED["at"] = time.time()
    if _SAVED.get("timer"):
        return                      # 已经在等落盘了
    _SAVED["timer"] = True

    def _flush():
        time.sleep(6)
        with _LOCK:
            _SAVED["timer"] = False
            cur = _SAVED["rect"]
        if not cur:
            return
        try:
            store.mutate_mini(x=int(cur[0]), y=int(cur[1]),
                              width=int(cur[2]), height=int(cur[3]),
                              content_h=int(cur[4]) if len(cur) > 4 else None)
            store.log(f"mini: 记住位置 {cur[0]},{cur[1]} {cur[2]}x{cur[3]}")
        except Exception as exc:
            store.log(f"mini: 存位置失败 {exc}")

    threading.Thread(target=_flush, name="wb-mini-save", daemon=True).start()


def keeper() -> None:
    """每秒看一眼小窗：钉住、记位置、认出手动改大小。

    为什么要一直盯：Chromium 在窗口被激活 / 切换虚拟桌面时会自己重排 z 序，
    图钉不是「设一次管一辈子」的属性，得有人守着。
    """
    was_open = False
    while True:
        try:
            hwnd = find_window()
            alive = bool(hwnd)
            if alive != was_open:
                was_open = alive
                STATE["open"] = alive
                STATE["tool"] = False          # 新窗口要重新设一次工具窗样式
                _LAST_SET = None
                store.log(f"mini: 窗口{'出现' if alive else '关闭'}（hwnd={hwnd}）")
            if hwnd:
                if not STATE["tool"]:
                    make_tool_window(hwnd)
                if STATE["pinned"]:
                    if _topmost(hwnd) is False:
                        set_topmost(hwnd, True)
                # 最小化的时候 GetWindowRect 会给出画外坐标，这时候记位置等于记了垃圾
                if not _u32().IsIconic(int(hwnd)):
                    rc = rect(hwnd)
                    if rc:
                        STATE["rect"] = rc
                        # 大小变了、但不是我设的 → 用户自己拖过：从此听用户的
                        if _LAST_SET and rc[2:] != _LAST_SET[2:]:
                            if STATE["auto"]:
                                STATE["auto"] = False
                                store.log(f"mini: 检测到手动改大小 {rc[2]}x{rc[3]}，"
                                          f"高度自适应已关（要恢复就删掉 mini.auto）")
                                try:
                                    store.mutate_mini(auto=False)
                                except Exception:
                                    pass
                        _persist_rect(rc)
        except Exception as exc:
            store.log(f"mini keeper: {exc}")
        time.sleep(1.2)


def start_keeper(pinned: bool = True, auto: bool = True) -> None:
    STATE["pinned"] = bool(pinned)
    STATE["auto"] = bool(auto)
    threading.Thread(target=keeper, name="wb-mini-keeper", daemon=True).start()
