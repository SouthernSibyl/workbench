"""把 dist/Workbench 安装到 LOCALAPPDATA，并在桌面放一个快捷方式。

只需在构建后跑一次；之后更新重跑即可。不生成任何 bat/ps1。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dist" / "Workbench"
INSTALL_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Workbench"
EXE = INSTALL_DIR / "Workbench.exe"
LNK_NAME = "工作台.lnk"


def desktop_dir() -> Path:
    for name in ("Desktop", "桌面"):
        p = Path.home() / name
        if p.exists():
            return p
    return Path(os.path.join(os.environ["USERPROFILE"], "Desktop"))


def _digest(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_file(a: Path, b: Path) -> bool:
    """内容是否一致（大小 + sha1）。"""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return _digest(a) == _digest(b)
    except OSError:
        return False


def copy_files() -> None:
    if not SRC.exists():
        raise SystemExit(f"找不到构建产物：{SRC}")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    skipped: list[str] = []
    for item in SRC.rglob("*"):
        rel = item.relative_to(SRC)
        dst = INSTALL_DIR / rel
        if item.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, dst)
            n += 1
        except PermissionError:
            # 常见于 VCRUNTIME140.dll：杀软扫描 / 刚退出的进程还捏着句柄。
            # 内容一样就跳过 —— 这个文件本来就不会变；不一样才算真失败。
            if _same_file(item, dst):
                skipped.append(str(rel))
                continue
            raise
    print(f"已复制 {n} 个文件 -> {INSTALL_DIR}")
    if skipped:
        print(f"  其中 {len(skipped)} 个被占用但内容已一致，跳过：{', '.join(skipped)}")


def make_shortcut(target: Path, lnk: Path) -> bool:
    """真 .lnk（用 pywin32）。失败返回 False，由调用方兜底。"""
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return False
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(str(lnk))
        sc.TargetPath = str(target)
        sc.WorkingDirectory = str(target.parent)
        sc.IconLocation = f"{target},0"
        sc.Description = "工作台 · 本地快捷启动面板"
        sc.WindowStyle = 1
        sc.Save()
        return lnk.exists()
    except Exception as exc:  # noqa: BLE001
        print("创建 .lnk 失败：", exc)
        return False


def make_hardlink(target: Path, link: Path) -> bool:
    """兜底：桌面上放一个指向 exe 的硬链接，双击同样是启动它。"""
    try:
        subprocess.run(["cmd", "/c", "mklink", "/H", str(link), str(target)],
                       check=True, capture_output=True)
        return link.exists()
    except subprocess.CalledProcessError as exc:
        print("硬链接失败：", exc.stderr.decode("utf-8", "ignore")[:200])
        return False


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("只支持 Windows")
    copy_files()

    desk = desktop_dir()
    lnk = desk / LNK_NAME
    legacy = desk / "工作台.exe"

    ok = make_shortcut(EXE, lnk)
    if ok:
        print(f"桌面快捷方式已创建：{lnk}")
        if legacy.exists():
            try:
                legacy.unlink()
                print("已清掉旧的硬链接占位：", legacy)
            except OSError:
                pass
    else:
        ok = make_hardlink(EXE, legacy)
        print(f"已改用桌面硬链接：{legacy}" if ok else "两种方式都失败了，请手动建快捷方式")

    print()
    print("启动：双击桌面「工作台」")
    print("程序目录：", INSTALL_DIR)
    print("配置/缓存：", Path(os.environ["APPDATA"]) / "Workbench")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
