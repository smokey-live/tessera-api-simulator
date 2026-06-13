# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).parents[1]
APP_DIR = ROOT / "app"
WINDOWS_DIR = ROOT / "packaging" / "windows"
ASSETS_DIR = WINDOWS_DIR / "assets"


a = Analysis(
    [str(WINDOWS_DIR / "windows_tray.py")],
    pathex=[str(ROOT), str(APP_DIR)],
    binaries=[],
    datas=[
        (str(APP_DIR / "default_state.json"), "app"),
        (str(APP_DIR / "endpoints.json"), "app"),
        (str(ASSETS_DIR / "sl-icon.png"), "packaging/windows/assets"),
    ],
    hiddenimports=[
        "log_store",
        "syslog_server",
        "tessera_sim",
        "topology_monitor",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TesseraMonitoringAndControl",
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
    icon=str(ASSETS_DIR / "sl-icon.ico"),
)
