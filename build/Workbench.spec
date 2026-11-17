# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包描述。

所有路径都从本文件位置推出来，不写死绝对路径 —— 换台机器 clone 下来就能构建。
构建：python -m PyInstaller --noconfirm build/Workbench.spec
产物：dist/Workbench/（onedir，启动比 onefile 快，也方便单独换前端文件）
"""

import os

HERE = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(HERE)

a = Analysis(
    [os.path.join(ROOT, 'app', 'main.py')],
    pathex=[os.path.join(ROOT, 'app')],
    binaries=[],
    datas=[(os.path.join(ROOT, 'app', 'web'), 'web')],
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
    icon=[os.path.join(HERE, 'icon.ico')],
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
