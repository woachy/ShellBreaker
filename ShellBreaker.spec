# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

_assets_bin_dir = Path('assets/bin')
_assets_bin_datas = (
    [(str(p), 'assets/bin') for p in sorted(_assets_bin_dir.iterdir()) if p.is_file()]
    if _assets_bin_dir.is_dir()
    else []
)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets/app_icon.png', 'assets'),
        ('assets/app_icon.ico', 'assets'),
    ] + _assets_bin_datas,
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
    a.binaries,
    a.datas,
    [],
    name='ShellBreaker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/app_icon.ico',
)
