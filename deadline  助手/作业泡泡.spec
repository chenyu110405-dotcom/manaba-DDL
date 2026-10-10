# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

block_cipher = None

# Collect package assets (HTML, JS, Avatars)
added_files = [
    ('manaba_deadline/dashboard.html', 'manaba_deadline'),
    ('manaba_deadline/dashboard.js', 'manaba_deadline'),
    ('manaba_deadline/avatars/*.jpg', 'manaba_deadline/avatars'),
]

a = Analysis(
    ['manaba_deadline/__main__.py'],
    pathex=[],
    binaries=[],
    datas=added_files,
    hiddenimports=[
        'playwright',
        'beautifulsoup4',
        'soupsieve',
        'tzdata',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['unittest', 'tests'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='作业泡泡',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Keep console to let playwright display browser & status
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
