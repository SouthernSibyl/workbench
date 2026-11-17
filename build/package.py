"""把 dist/Workbench 打成可以直接发出去的 zip（绿色版）。

用法（先打包，再执行本脚本）：
    python -m PyInstaller --noconfirm build/Workbench.spec
    python build/package.py

产物：dist/workbench-<版本>-win64.zip
解压后双击 Workbench.exe 即可，不写注册表、不需要安装器。
和 install.py 的区别：install.py 是「装到我这台机器」，本脚本是「发给别人」。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dist" / "Workbench"


def app_version() -> str:
    """从 main.py 里读版本号，不 import —— 那是整个服务，没必要为了一个字符串拉起来。"""
    text = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "0.0.0"


def main() -> int:
    if not SRC.is_dir():
        print(f"找不到构建产物：{SRC}\n先跑：python -m PyInstaller --noconfirm build/Workbench.spec",
              file=sys.stderr)
        return 1

    ver = app_version()
    out = ROOT / "dist" / f"workbench-{ver}-win64.zip"
    files = [p for p in SRC.rglob("*") if p.is_file()]

    # 只存一份时间戳，解压出来不会出现「每个文件都被改过」
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(files):
            rel = p.relative_to(SRC.parent)          # 顶层目录保留：解压出一个 Workbench/
            z.write(p, rel.as_posix())

    mb = out.stat().st_size / 1048576
    print(f"{out.relative_to(ROOT)}  {mb:.1f} MB  {len(files)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
