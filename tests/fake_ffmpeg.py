"""
Cross-platform fake ``ffmpeg`` for the post-processing tests.

A tiny Python program stands in for ffmpeg so the same tests run on
Linux, macOS and Windows (where the real differences live: pythonw,
CREATE_NO_WINDOW, file replace/unlink on a file a child just wrote).

* POSIX: an executable script named ``ffmpeg`` (python shebang).
* Windows: ``ffmpeg.bat`` launching the script — found by shutil.which
  through PATHEXT and runnable by subprocess.

Behaviour is driven by environment variables so a test can pick the
exit code, whether an output file is produced, and what the loudnorm
measurement pass prints. ``IMAGEIO_FFMPEG_EXE`` is pointed at the fake
as well, so the recorder's candidate list contains only the fake (it
would otherwise fall back to the real bundled binary).
"""

import os
import stat
import sys
from pathlib import Path

_IMPL = r'''
import os
import sys

args = sys.argv[1:]
log = os.environ.get("FAKE_FFMPEG_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(" ".join(args) + "\n")
last = args[-1] if args else ""
if last == "-":  # loudnorm measurement pass: -f null -
    rc = int(os.environ.get("FAKE_FFMPEG_MEASURE_RC", "0"))
    js = os.environ.get("FAKE_FFMPEG_JSON")
    if rc == 0 and js:
        with open(js, encoding="utf-8") as f:
            sys.stderr.write("[Parsed_loudnorm_0 @ 0x1]\n" + f.read())
    sys.exit(rc)
rc = int(os.environ.get("FAKE_FFMPEG_RC", "0"))
if os.environ.get("FAKE_FFMPEG_OUTPUT", "write") == "write" and last and not last.startswith("-"):
    with open(last, "wb") as f:
        f.write(b"fake-ffmpeg-output\n")
sys.exit(rc)
'''

_ALWAYS_OK = r'''
import sys
last = sys.argv[-1] if len(sys.argv) > 1 else ""
if last != "-" and last and not last.startswith("-"):
    with open(last, "wb") as f:
        f.write(b"ok-output\n")
sys.exit(0)
'''


def _write_launcher(bin_dir: Path, body: str, name: str = "ffmpeg") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        impl = bin_dir / f"{name}_impl.py"
        impl.write_text(body, encoding="utf-8")
        launcher = bin_dir / f"{name}.bat"
        launcher.write_text(f'@"{sys.executable}" "%~dp0{name}_impl.py" %*\r\n', encoding="utf-8")
    else:
        launcher = bin_dir / name
        launcher.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


def install(tmp_path: Path, monkeypatch, rc: int = 0, output: str = "write",
            measure_rc: int = 0, measure_json: str = None, log: Path = None) -> Path:
    """Put the fake ffmpeg first on PATH (and as IMAGEIO_FFMPEG_EXE).
    Returns the launcher path."""
    bin_dir = tmp_path / "fakebin"
    launcher = _write_launcher(bin_dir, _IMPL)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", str(launcher))
    monkeypatch.setenv("FAKE_FFMPEG_RC", str(rc))
    monkeypatch.setenv("FAKE_FFMPEG_OUTPUT", output)
    monkeypatch.setenv("FAKE_FFMPEG_MEASURE_RC", str(measure_rc))
    if measure_json is not None:
        js = tmp_path / "loudnorm.json"
        js.write_text(measure_json, encoding="utf-8")
        monkeypatch.setenv("FAKE_FFMPEG_JSON", str(js))
    else:
        monkeypatch.delenv("FAKE_FFMPEG_JSON", raising=False)
    if log is not None:
        monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
    else:
        monkeypatch.delenv("FAKE_FFMPEG_LOG", raising=False)
    return launcher


def install_always_ok(tmp_path: Path) -> Path:
    """A second, independent fake that always succeeds (fallback tests)."""
    return _write_launcher(tmp_path / "okbin", _ALWAYS_OK)
