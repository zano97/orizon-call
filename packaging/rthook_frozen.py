"""
PyInstaller runtime hook: runs before main.py inside the packaged app.

Linux only:
- PortAudio ships inside the bundle (built without JACK, see
  packaging/linux/build_portaudio.sh), but sounddevice looks it up with
  ``ctypes.util.find_library('portaudio')``, which only searches system
  paths: point it at the bundled copy.
- The bootloader may prepend the bundle to LD_LIBRARY_PATH. That is only
  needed by this process (the dynamic loader read it at start-up);
  children such as xdg-open or the file manager must not load the
  bundle's Qt/GLib, so restore the user's value for them.
"""

import os
import sys

if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
    _bundle = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))

    _orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if _orig is not None:
        os.environ["LD_LIBRARY_PATH"] = _orig
    else:
        _kept = [p for p in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
                 if p and os.path.realpath(p) != os.path.realpath(_bundle)]
        if _kept:
            os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(_kept)
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)

    import ctypes.util

    _find_library = ctypes.util.find_library

    def _bundled_find_library(name):
        if name == "portaudio":
            for candidate in ("libportaudio.so.2", "libportaudio.so"):
                path = os.path.join(_bundle, candidate)
                if os.path.exists(path):
                    return path
        return _find_library(name)

    ctypes.util.find_library = _bundled_find_library
