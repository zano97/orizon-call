#!/usr/bin/env python3
"""
Build the Orizon Call installers for the operating system this runs on.

    python packaging/build.py              # build + self-test
    python packaging/build.py --checksums dist/release   # SHA256SUMS.txt only

Output in dist/release/:
  macOS    OrizonCall-<v>-macos-<arch>.dmg  (first install, drag to Applications)
           OrizonCall-<v>-macos-<arch>.zip  (used by in-app updates)
  Windows  OrizonCall-<v>-windows-x64-setup.exe
  Linux    OrizonCall-<v>-linux-<arch>.AppImage

Signing is optional and driven by environment variables (the release
workflow sets them from repository secrets):

  macOS    MACOS_SIGN_IDENTITY   "Developer ID Application: Name (TEAMID)"
           APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD  → notarization
           (without an identity the app is ad-hoc signed: it runs, but
           Gatekeeper asks for confirmation on first launch)
  Windows  WINDOWS_CERT_PFX (path), WINDOWS_CERT_PASSWORD → signtool
           (without it SmartScreen warns on first install)

Other knobs: ORIZON_PORTAUDIO_LIB (Linux, skip building PortAudio),
APPIMAGETOOL (path to appimagetool), ISCC (path to Inno Setup's ISCC.exe).
"""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
RELEASE = DIST / "release"

sys.path.insert(0, str(ROOT))
from version import APP_NAME, __version__  # noqa: E402

APPIMAGETOOL_URL = ("https://github.com/AppImage/appimagetool/releases/download/"
                    "continuous/appimagetool-{arch}.AppImage")


def say(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def run(cmd, **kw):
    print("   $", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(folder: Path) -> Path:
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.name != "SHA256SUMS.txt")
    out = folder / "SHA256SUMS.txt"
    out.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files), encoding="utf-8")
    print(out.read_text(encoding="utf-8"))
    return out


def pyinstaller(env: dict) -> None:
    say(f"PyInstaller ({APP_NAME} {__version__})")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", DIST, "--workpath", BUILD / "pyinstaller",
         PACKAGING / "orizon_call.spec"], env=env)


def self_test(cmd, env=None) -> None:
    """Run the packaged app's --self-test (no window, no audio device)."""
    say("Self-test of the packaged app")
    report = BUILD / "self-test.txt"
    report.unlink(missing_ok=True)
    env = dict(os.environ if env is None else env)
    result = subprocess.run([str(c) for c in cmd] + ["--self-test", str(report)],
                            env=env, timeout=300)
    if report.exists():
        print(report.read_text(encoding="utf-8"))
    if result.returncode != 0 or not report.exists():
        raise SystemExit(f"self-test failed (exit code {result.returncode})")


# ---------- macOS ----------

def _is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    return magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
                     b"\xbe\xba\xfe\xca", b"\xfe\xed\xfa\xcf")


def mac_sign(app: Path, identity: str) -> None:
    """Sign inside-out: every Mach-O, then the bundle. Hardened runtime and
    a secure timestamp with a real identity (required for notarization)."""
    say(f"Signing ({'ad-hoc' if identity == '-' else identity})")
    entitlements = PACKAGING / "macos" / "entitlements.plist"
    base = ["codesign", "--force", "--sign", identity]
    if identity != "-":
        base += ["--options", "runtime", "--timestamp", "--entitlements", entitlements]
    machos = [p for p in app.rglob("*") if p.is_file() and not p.is_symlink() and _is_macho(p)]
    # Deepest first, the main executable last.
    main_exe = app / "Contents" / "MacOS" / APP_NAME
    machos.sort(key=lambda p: (p == main_exe, -len(p.parts)))
    for p in machos:
        run(base + [p], stdout=subprocess.DEVNULL)
    run(base + [app])
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app])


def mac_notarize(path: Path) -> None:
    say(f"Notarizing {path.name}")
    run(["xcrun", "notarytool", "submit", path, "--wait",
         "--apple-id", os.environ["APPLE_ID"], "--team-id", os.environ["APPLE_TEAM_ID"],
         "--password", os.environ["APPLE_APP_PASSWORD"]])


def build_macos(args) -> list:
    arch = os.environ.get("ORIZON_TARGET_ARCH") or platform.machine()
    helper = ROOT / "helpers" / "system_audio_capture"
    from macos_system_audio import binary_matches_host
    if not helper.exists() or not binary_matches_host(helper, machine=arch, translated=False):
        say(f"Building the ScreenCaptureKit helper for {arch}")
        run(["bash", ROOT / "helpers" / "build.sh"])
    env = dict(os.environ, ORIZON_TARGET_ARCH=arch)
    pyinstaller(env)
    app = DIST / f"{APP_NAME}.app"

    identity = os.environ.get("MACOS_SIGN_IDENTITY") or "-"
    mac_sign(app, identity)
    notarize = identity != "-" and all(os.environ.get(k) for k in
                                       ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD"))
    stem = f"OrizonCall-{__version__}-macos-{arch}"
    RELEASE.mkdir(parents=True, exist_ok=True)
    zip_path = RELEASE / f"{stem}.zip"

    def make_zip():
        zip_path.unlink(missing_ok=True)
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, zip_path])

    make_zip()
    if notarize:
        mac_notarize(zip_path)
        run(["xcrun", "stapler", "staple", app])
        make_zip()   # the update zip carries the stapled ticket

    if not args.no_self_test:
        self_test([app / "Contents" / "MacOS" / APP_NAME])

    say("Disk image")
    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    run(["ditto", app, stage / app.name])
    (stage / "Applications").symlink_to("/Applications")
    dmg = RELEASE / f"{stem}.dmg"
    dmg.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", stage,
         "-ov", "-format", "UDZO", dmg])
    if identity != "-":
        run(["codesign", "--force", "--sign", identity, "--timestamp", dmg])
        if notarize:
            mac_notarize(dmg)
            run(["xcrun", "stapler", "staple", dmg])
    return [dmg, zip_path]


# ---------- Windows ----------

def _find_tool(env_var: str, names, extra_dirs=()) -> str:
    if os.environ.get(env_var):
        return os.environ[env_var]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for d in extra_dirs:
        for name in names:
            cand = Path(d) / name
            if cand.is_file():
                return str(cand)
    raise SystemExit(f"{names[0]} not found (set {env_var})")


def win_sign(path: Path) -> None:
    pfx = os.environ.get("WINDOWS_CERT_PFX")
    if not pfx:
        return
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    dirs = sorted((p / "x64" for p in kits.glob("10.*")), reverse=True) if kits.is_dir() else []
    signtool = _find_tool("SIGNTOOL", ["signtool.exe", "signtool"], dirs)
    say(f"Signing {path.name}")
    run([signtool, "sign", "/f", pfx, "/p", os.environ.get("WINDOWS_CERT_PASSWORD", ""),
         "/fd", "sha256", "/tr", "http://timestamp.digicert.com", "/td", "sha256", path])


def build_windows(args) -> list:
    pyinstaller(dict(os.environ))
    app_dir = DIST / APP_NAME
    exe = app_dir / f"{APP_NAME}.exe"
    win_sign(exe)
    if not args.no_self_test:
        self_test([exe])
    say("Installer (Inno Setup)")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    iscc = _find_tool("ISCC", ["ISCC.exe", "iscc"],
                      [Path(pf86) / "Inno Setup 6", Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6"])
    RELEASE.mkdir(parents=True, exist_ok=True)
    base = f"OrizonCall-{__version__}-windows-x64-setup"
    run([iscc, "/Q", f"/DAppVersion={__version__}", f"/DSourceDir={app_dir}",
         f"/DOutputDir={RELEASE}", f"/DOutputBaseFilename={base}",
         PACKAGING / "windows" / "installer.iss"])
    setup = RELEASE / f"{base}.exe"
    win_sign(setup)
    return [setup]


# ---------- Linux ----------

def _appimagetool(arch: str) -> str:
    if os.environ.get("APPIMAGETOOL"):
        return os.environ["APPIMAGETOOL"]
    tool = BUILD / "tools" / f"appimagetool-{arch}.AppImage"
    if not tool.exists():
        say("Downloading appimagetool")
        tool.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(APPIMAGETOOL_URL.format(arch=arch), tool)
        tool.chmod(0o755)
    return str(tool)


def build_linux(args) -> list:
    arch = platform.machine()
    env = dict(os.environ)
    if not env.get("ORIZON_PORTAUDIO_LIB"):
        say("Building PortAudio (ALSA only)")
        out = BUILD / "portaudio"
        run(["bash", PACKAGING / "linux" / "build_portaudio.sh", out])
        env["ORIZON_PORTAUDIO_LIB"] = str(out / "libportaudio.so.2")
    pyinstaller(env)
    bundle = DIST / "orizon-call"

    leaked = [p.name for p in bundle.rglob("*")
              if p.name.startswith(("libasound.so", "libjack", "libstdc++.so", "libgcc_s.so"))]
    if leaked:
        raise SystemExit(f"host libraries leaked into the bundle: {leaked}")

    say("AppDir")
    appdir = BUILD / "AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    (appdir / "usr" / "lib").mkdir(parents=True)
    shutil.copytree(bundle, appdir / "usr" / "lib" / "orizon-call", symlinks=True)
    shutil.copy2(PACKAGING / "linux" / "AppRun", appdir / "AppRun")
    (appdir / "AppRun").chmod(0o755)
    shutil.copy2(PACKAGING / "linux" / "orizon-call.desktop", appdir / "orizon-call.desktop")
    icon = ROOT / "assets" / "icons" / "orizon-call-256.png"
    shutil.copy2(icon, appdir / "orizon-call.png")
    shutil.copy2(icon, appdir / ".DirIcon")
    icons_dir = appdir / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
    icons_dir.mkdir(parents=True)
    shutil.copy2(icon, icons_dir / "orizon-call.png")

    say("AppImage")
    RELEASE.mkdir(parents=True, exist_ok=True)
    out = RELEASE / f"OrizonCall-{__version__}-linux-{arch}.AppImage"
    out.unlink(missing_ok=True)
    tool_env = dict(os.environ, ARCH=arch, APPIMAGE_EXTRACT_AND_RUN="1")
    run([_appimagetool(arch), "--no-appstream", appdir, out], env=tool_env)
    out.chmod(0o755)
    if not args.no_self_test:
        # CI machines often lack FUSE: extract-and-run tests the same files.
        self_test([out], env=dict(os.environ, APPIMAGE_EXTRACT_AND_RUN="1"))
    return [out]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--no-self-test", action="store_true",
                        help="Skip running the packaged app's --self-test.")
    parser.add_argument("--checksums", type=Path, metavar="DIR",
                        help="Only write DIR/SHA256SUMS.txt for the files in DIR.")
    args = parser.parse_args()
    if args.checksums:
        write_checksums(args.checksums)
        return
    if sys.platform == "darwin":
        outputs = build_macos(args)
    elif sys.platform == "win32":
        outputs = build_windows(args)
    else:
        outputs = build_linux(args)
    say("Done")
    for p in outputs:
        print(f"   {p}  ({p.stat().st_size / 1e6:.1f} MB)  sha256 {sha256(p)}")


if __name__ == "__main__":
    main()
