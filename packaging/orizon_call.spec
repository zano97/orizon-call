# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Orizon Call — one file for macOS, Windows and Linux.

Run through packaging/build.py (it sets the environment variables read
here and turns the output into a .dmg/.zip, a Windows installer or an
AppImage). Output: dist/Orizon Call.app (macOS), dist/Orizon Call/
(Windows), dist/orizon-call/ (Linux).

Environment:
  ORIZON_PORTAUDIO_LIB  Linux: the libportaudio.so.2 to bundle (built
                        without JACK by packaging/linux/build_portaudio.sh)
  ORIZON_TARGET_ARCH    macOS: arm64 | x86_64 (default: this machine)
"""

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))
from version import APP_NAME, BUNDLE_ID, __version__  # noqa: E402

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

datas = [(str(ROOT / "assets"), "assets")]
binaries = []
hiddenimports = ["PyQt6.QtSvg"]

if IS_MAC:
    helper = ROOT / "helpers" / "system_audio_capture"
    if not helper.exists():
        raise SystemExit("helpers/system_audio_capture missing: run helpers/build.sh first")
    binaries.append((str(helper), "helpers"))
    hiddenimports += ["AppKit", "objc"]
if IS_WIN:
    hiddenimports += ["pyaudiowpatch"]
if IS_LINUX:
    hiddenimports += ["pulsectl"]
    pa = os.environ.get("ORIZON_PORTAUDIO_LIB")
    if pa:
        binaries.append((pa, "."))

excludes = [
    "tkinter", "unittest", "pydoc_data", "pytest", "_pytest", "ruff",
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtQml",
    "PyQt6.QtQuick", "PyQt6.Qt3DCore", "PyQt6.QtMultimedia", "PyQt6.QtNetwork",
    "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtDesigner", "PyQt6.QtBluetooth",
    "PyQt6.QtPdf", "PyQt6.QtCharts", "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    runtime_hooks=[str(ROOT / "packaging" / "rthook_frozen.py")],
    excludes=excludes,
    noarchive=False,
)

if IS_LINUX:
    # Never ship these with the app:
    # - ALSA: its plugins (pulse/pipewire, the path to system audio) are
    #   found relative to the library, so it must be the host's own;
    # - JACK: not used, and it drags libdb along;
    # - libstdc++/libgcc_s: the host's copy is always newer than the build
    #   machine's (built on the oldest supported Ubuntu) and GPU drivers
    #   loaded into the process need the newer one.
    # - any libportaudio picked up from the system: ours is added above.
    _drop = ("libasound.so", "libjack", "libjacknet", "libjackserver", "libdb-",
             "libstdc++.so", "libgcc_s.so")
    _ours = os.path.realpath(os.environ.get("ORIZON_PORTAUDIO_LIB", "")) if os.environ.get(
        "ORIZON_PORTAUDIO_LIB") else None

    def _keep(entry):
        dest, src = entry[0], entry[1]
        base = os.path.basename(dest)
        if base.startswith(_drop):
            return False
        if base.startswith("libportaudio") and _ours and os.path.realpath(src) != _ours:
            return False
        return True

    a.binaries = [b for b in a.binaries if _keep(b)]

pyz = PYZ(a.pure)

if IS_MAC:
    icon = str(ROOT / "assets" / "icons" / "orizon-call.icns")
elif IS_WIN:
    icon = str(ROOT / "assets" / "icons" / "orizon-call.ico")
else:
    icon = None

version_file = None
if IS_WIN:
    from PyInstaller.utils.win32.versioninfo import (  # noqa: E402
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
        VarStruct, VSVersionInfo)
    nums = tuple(int(x) for x in __version__.split("-")[0].split(".")[:3]) + (0,)
    version_file = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo([StringTable("041004B0", [
                StringStruct("CompanyName", "Orizon"),
                StringStruct("FileDescription", APP_NAME),
                StringStruct("FileVersion", __version__),
                StringStruct("InternalName", "OrizonCall"),
                StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", __version__),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0410, 1200])]),
        ],
    )

exe_name = "orizon-call" if IS_LINUX else APP_NAME

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=exe_name,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon,
    version=version_file,
    target_arch=os.environ.get("ORIZON_TARGET_ARCH") or None,
    codesign_identity=None,       # build.py signs (inside-out, hardened runtime)
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=exe_name,
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier=BUNDLE_ID,
        version=__version__,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            # Menu-bar app: no Dock icon, no app menu (the widget and the
            # status item are the UI).
            "LSUIElement": True,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription":
                "Orizon Call usa il microfono per registrare la tua voce durante le call.",
            "NSHumanReadableCopyright": "© Orizon",
        },
    )
