"""本地信号采集：agent 使用痕迹 / 项目轨迹 / 浏览器搜索记录 / 最近文件。

全部只读，失败一律降级为空，不影响启动。
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

WB_HOME = Path.home() / ".workbuddy"
DESKTOP = Path.home() / "Desktop"

_WEBKIT_EPOCH_OFFSET = 11644473600  # 1601-01-01 -> 1970-01-01（秒）


# ------------------------------------------------------------ agent 使用痕迹

def _recent_skills(days: int = 30) -> list[dict]:
    p = WB_HOME / "usage-log.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    out = []
    for bucket in ("skills", "plugins", "tools"):
        for item in (data.get(bucket) or {}).values():
            if not isinstance(item, dict):
                continue
            last = str(item.get("lastUsedDate") or "")
            if last >= cutoff:
                out.append({"id": item.get("id") or "", "type": item.get("type") or bucket,
                            "last": last, "count": len(item.get("recentDates") or [])})
    out.sort(key=lambda x: (x["count"], x["last"]), reverse=True)
    return out[:14]


def _recent_projects() -> list[dict]:
    """从 sessions/*.json 里拿最近活跃的工作目录。"""
    seen: dict[str, dict] = {}
    for f in glob.glob(str(WB_HOME / "sessions" / "*.json")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cwd = str(d.get("cwd") or "").strip()
        if not cwd or d.get("kind") == "prewarm":
            continue
        ts = int(d.get("updatedAt") or d.get("startedAt") or 0)
        cur = seen.get(cwd)
        if cur is None or ts > cur["ts"]:
            seen[cwd] = {"path": cwd, "name": Path(cwd).name or cwd, "ts": ts,
                         "kind": d.get("kind") or ""}
    rows = sorted(seen.values(), key=lambda x: x["ts"], reverse=True)
    return rows[:10]


def _recent_agent_tools(limit: int = 12) -> list[str]:
    """从 traces 里统计最近用过的工具/技能名（agent 使用痕迹）。"""
    names: Counter[str] = Counter()
    trace_root = WB_HOME / "traces"
    if not trace_root.is_dir():
        return []
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=14)).timestamp()
    files = sorted(glob.glob(str(trace_root / "*" / "*.jsonl")),
                   key=lambda p: os.path.getmtime(p), reverse=True)[:40]
    for f in files:
        try:
            if os.path.getmtime(f) < cutoff:
                continue
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"span_start"' not in line and '"name"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    span = obj.get("span") or {}
                    name = str(span.get("name") or "").strip()
                    if name and name not in ("mcp_tools", "Agent workflow"):
                        names[name] += 1
                    if len(names) > 400:
                        break
        except OSError:
            continue
    skip = {"outer", "inner"}
    return [n for n, _ in names.most_common(40) if n.lower() not in skip][:limit]


# -------------------------------------------------------------- 浏览器搜索记录

_BROWSER_HISTORY = [
    ("Edge", Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data/Default/History"),
    ("Chrome", Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data/Default/History"),
]


def _webkit_to_dt(v: int) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromtimestamp(v / 1_000_000 - _WEBKIT_EPOCH_OFFSET)
    except (OverflowError, OSError, ValueError):
        return None


def _browser_history(days: int = 14) -> dict:
    out = {"searches": [], "sites": [], "browser": ""}
    for name, db in _BROWSER_HISTORY:
        if not db.exists():
            continue
        tmp = Path(tempfile.gettempdir()) / f"wbhist_{name}.db"
        try:
            shutil.copy2(db, tmp)
            for extra in ("-wal", "-shm"):
                src = Path(str(db) + extra)
                if src.exists():
                    shutil.copy2(src, Path(str(tmp) + extra))
        except OSError:
            continue
        try:
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cutoff = (_dt.datetime.now() - _dt.timedelta(days=days))
            cutoff_wk = int((cutoff.timestamp() + _WEBKIT_EPOCH_OFFSET) * 1_000_000)
            for row in con.execute(
                "SELECT k.term AS term, u.last_visit_time AS t FROM keyword_search_terms k "
                "JOIN urls u ON u.id = k.url_id WHERE u.last_visit_time > ? "
                "ORDER BY u.last_visit_time DESC LIMIT 120", (cutoff_wk,)):
                term = (row["term"] or "").strip()
                if term:
                    out["searches"].append({"text": term, "at": str(_webkit_to_dt(row["t"]) or "")[:16]})
            for row in con.execute(
                "SELECT title AS title, url AS url, last_visit_time AS t FROM urls "
                "WHERE title <> '' AND last_visit_time > ? "
                "ORDER BY last_visit_time DESC LIMIT 120", (cutoff_wk,)):
                out["sites"].append({"title": (row["title"] or "").strip(),
                                     "url": row["url"] or "",
                                     "at": str(_webkit_to_dt(row["t"]) or "")[:16]})
            con.close()
            out["browser"] = name
            if out["searches"] or out["sites"]:
                break
        except sqlite3.Error:
            continue
        finally:
            for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
                try:
                    p.unlink()
                except OSError:
                    pass
    # 去重
    seen = set()
    uniq = []
    for s in out["searches"]:
        k = s["text"].lower()
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    out["searches"] = uniq[:30]
    seen = set()
    uniq = []
    for s in out["sites"]:
        k = s["title"].lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(s)
    out["sites"] = uniq[:40]
    return out


# ---------------------------------------------------------------- 最近动过的文件

def _recent_files(days: int = 14, limit: int = 18) -> list[dict]:
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).timestamp()
    rows = []
    for pat in ("*", "*/*"):
        for f in glob.glob(str(DESKTOP / pat)):
            p = Path(f)
            try:
                if not p.is_file() or p.name.startswith("~$") or p.name.startswith("."):
                    continue
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            rows.append({"name": p.name, "path": str(p), "ts": st.st_mtime, "size": st.st_size})
    rows.sort(key=lambda x: x["ts"], reverse=True)
    return rows[:limit]


# ------------------------------------------------------------------- 对外接口

def collect(cfg: dict | None = None) -> dict:
    """采集本机痕迹。

    调用方（`ai.build_prompt`）已经先看过 `signals.enabled` 总开关；这里再管
    细项：`signals.browser_history` 关掉就不碰浏览器历史库。
    """
    cfg = cfg or {}
    sig = cfg.get("signals") or {}
    days = int(sig.get("days") or 14)
    if sig.get("browser_history", True):
        hist = _browser_history(days)
    else:
        hist = {"searches": [], "sites": [], "browser": ""}
    signals = {
        "skills": _recent_skills(),
        "projects": _recent_projects(),
        "tools": _recent_agent_tools(),
        "searches": hist["searches"],
        "sites": hist["sites"],
        "browser": hist["browser"],
        "files": _recent_files(days),
        "collectedAt": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    return signals


def interest_brief(signals: dict, limit: int = 28) -> str:
    """把信号压成一段给模型看的短文本。"""
    lines: list[str] = []
    if signals.get("skills"):
        lines.append("最近用的技能/插件：" + "、".join(
            s["id"] for s in signals["skills"][:10] if s.get("id")))
    if signals.get("tools"):
        lines.append("最近调用的工具：" + "、".join(signals["tools"][:12]))
    if signals.get("projects"):
        lines.append("最近打开的项目目录：" + "、".join(
            p["name"] for p in signals["projects"][:8]))
    if signals.get("searches"):
        lines.append("最近的搜索词：" + "、".join(
            s["text"] for s in signals["searches"][:limit]))
    if signals.get("sites"):
        lines.append("最近浏览的页面：" + "、".join(
            s["title"] for s in signals["sites"][:limit]))
    if signals.get("files"):
        lines.append("最近动过的桌面文件：" + "、".join(
            f["name"] for f in signals["files"][:12]))
    return "\n".join(lines)
