# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

路径全部按「本文件所在目录」推算（SPECPATH 是 PyInstaller 注入的全局变量，
值就是 build/ 的绝对路径），所以仓库 clone 到任何位置都能直接打包：

    pyinstaller build/Workbench.spec --noconfirm

产物：dist/Workbench/Workbench.exe（onedir，含 _internal）。
接着跑 build/install.py 装到 %LOCALAPPDATA%\\Programs\\Workbench 并建桌面快捷方式。
"""

from pathlib import Path

_BUILD_DIR = Path(SPECPATH).resolve()          # .../build
_ROOT = _BUILD_DIR.parent                      # 仓库根
_APP = _ROOT / "app"
_WEB = _APP / "web"
_ICON = _BUILD_DIR / "icon.ico"

a = Analysis(
    [str(_APP / "main.py")],
    pathex=[str(_APP)],
    binaries=[],
    datas=[(str(_WEB), "web")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Workbench',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(_ICON)],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Workbench',
)
