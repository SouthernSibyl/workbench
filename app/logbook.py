"""分级日志 —— 分级与结构照抄 Angular（@angular-devkit/core 的 logger，MIT）。

对照 packages/angular_devkit/core/src/logger/logger.ts：

    type LogLevel = 'debug' | 'info' | 'warn' | 'error' | 'fatal'
    LogEntry = { level, message, timestamp, name, path[] }
    LoggerApi = { createChild(name), log(level, msg, meta),
                  debug, info, warn, error, fatal }

我们在 LogEntry 上多加一个**必填**的 intent —— 这条日志是为了什么发生的。
它不是注释，是事后复盘唯一的线索：光有「18:22 摆了 4 格」没法判断做得对不对，
得知道「因为用户从 3 窗切到了 4 窗」。

落盘是 JSONL（一行一个对象），方便 grep：

    {"ts":"2026-10-22T22:40:11.123+0800","ms":1761050411123,
     "level":"info","name":"wall","path":["main","wall"],
     "message":"apply layout","intent":"用户从 3 窗切到 4 窗","meta":{"n":4}}

为什么不用标准库 logging：这里要的是「结构化的意图流水」，能按 ns 过滤、
能直接喂给 /api/logs 给界面看，不是往控制台打给人扫。日志写不进去也绝不能
把业务带崩，所以所有 I/O 都吞异常。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

LEVELS = ("debug", "info", "warn", "error", "fatal")
_RANK = {name: i for i, name in enumerate(LEVELS)}

MAX_BYTES = 8 * 1024 * 1024      # 单文件 8MB 封顶，超了换下一个序号
KEEP_DAYS = 14                   # 只留两周，够复盘，不会把盘塞满

_lock = threading.RLock()
_state: dict = {"dir": None, "level": "info", "pruned": ""}


def _default_dir() -> Path:
    v = os.environ.get("APPDATA")
    base = Path(v) if v else Path.home() / "AppData" / "Roaming"
    return base / "Workbench" / "logs"


def init(data_dir) -> None:
    """数据目录定了以后调一次（store 那边会调）。只记路径，不开线程。"""
    with _lock:
        _state["dir"] = Path(data_dir) / "logs"


def set_level(level: str) -> None:
    """过滤级别。写错了就退回 info —— 配置错不该把日志全吞掉。"""
    level = str(level or "").lower()
    with _lock:
        _state["level"] = level if level in _RANK else "info"


def _enabled(level: str) -> bool:
    # 两条满足其一就写：①不低于配置的级别 ②本身是 warn 及以上。
    # 第二条是兜底 —— 就算把级别配成 fatal，也不该看不见警告和报错。
    rank = _RANK.get(level, _RANK["info"])
    return rank >= _RANK.get(str(_state["level"]), _RANK["info"]) or rank >= _RANK["warn"]


def _day() -> str:
    return time.strftime("%Y%m%d")


def _file_for(day: str) -> Path:
    d = _state["dir"] or _default_dir()
    d.mkdir(parents=True, exist_ok=True)
    base = d / f"workbench-{day}.jsonl"
    if not base.exists() or base.stat().st_size < MAX_BYTES:
        return base
    for i in range(1, 1000):
        alt = d / f"workbench-{day}.{i:03d}.jsonl"
        if not alt.exists() or alt.stat().st_size < MAX_BYTES:
            return alt
    return base


def _prune() -> None:
    """按天切之后老文件要有人扫；一天只扫一次，别每写一行都 stat 一轮。"""
    day = _day()
    if _state["pruned"] == day:
        return
    _state["pruned"] = day
    d = _state["dir"] or _default_dir()
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        for p in d.glob("workbench-*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


class Logger:
    """一个命名空间。child() 往下挂，path 记成 ["main","wall"] 这种。"""

    __slots__ = ("name", "path")

    def __init__(self, name: str, path: tuple = ()) -> None:
        self.name = str(name)
        self.path = tuple(path) or (self.name,)

    def child(self, name: str) -> "Logger":
        """对应 Angular 的 createChild(name)。"""
        return Logger(str(name), self.path + (str(name),))

    # ------------------------------------------------------------ 写日志

    def log(self, level: str, message: str, *, intent: str, **meta) -> None:
        level = str(level or "info").lower()
        if level not in _RANK:
            level = "info"
        if not str(intent or "").strip():
            raise ValueError("日志必须写 intent：说清这条是为了什么发生的")
        if not _enabled(level):
            return
        now = time.time()
        lt = time.localtime(now)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", lt)
                  + f".{int(now * 1000) % 1000:03d}"
                  + time.strftime("%z", lt),
            "ms": int(now * 1000),
            "level": level,
            "name": self.name,
            "path": list(self.path),
            "message": str(message)[:4000],
            "intent": str(intent)[:2000],
        }
        if meta:
            entry["meta"] = meta
        self._write(entry)

    def debug(self, message: str, *, intent: str, **meta) -> None:
        self.log("debug", message, intent=intent, **meta)

    def info(self, message: str, *, intent: str, **meta) -> None:
        self.log("info", message, intent=intent, **meta)

    def warn(self, message: str, *, intent: str, **meta) -> None:
        self.log("warn", message, intent=intent, **meta)

    def error(self, message: str, *, intent: str, **meta) -> None:
        self.log("error", message, intent=intent, **meta)

    def fatal(self, message: str, *, intent: str, **meta) -> None:
        self.log("fatal", message, intent=intent, **meta)

    def _write(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with _lock:
                _prune()
                path = _file_for(_day())
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError:
            pass            # 日志落盘失败绝不能把业务带崩


def get(name: str, *down: str) -> Logger:
    """取一个命名空间：get("main").child("wall") 或 get("main","wall") 都行。"""
    lg = Logger(name)
    for part in down:
        lg = lg.child(part)
    return lg


def tail(level: str | None = None, limit: int = 200,
         since: float | None = None) -> list[dict]:
    """给 /api/logs 用：取最近的若干条，按时间升序返回。

    只扫最近 3 个文件 —— 今天的文件才是热的那份，再往前翻没意义，
    也免得一次把两周的流水全读进内存。
    """
    d = _state["dir"] or _default_dir()
    try:
        files = sorted(d.glob("workbench-*.jsonl"))[-3:]
    except OSError:
        return []
    want = _RANK.get(str(level or "").lower())
    out: list[dict] = []
    for path in reversed(files):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue            # 半行（正在写）直接跳过
                    if not isinstance(item, dict):
                        continue
                    if want is not None and _RANK.get(str(item.get("level")), -1) < want:
                        continue
                    if since and float(item.get("ms") or 0) < since:
                        continue
                    out.append(item)
        except OSError:
            continue
        if len(out) >= max(limit * 4, 400):
            break
    out.sort(key=lambda e: float(e.get("ms") or 0))
    return out[-max(1, int(limit)):]
