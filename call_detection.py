"""
Automatic call detection.

A call in Meet/Zoom/Teams/Slack/… always means one thing at the OS level:
another application keeps the microphone open. That signal is portable,
needs no per-app integration and works for browser calls too:

- Windows 10/11: the privacy "capability access" registry (the same data
  behind the microphone icon in the taskbar) — an app whose
  ``LastUsedTimeStop`` is 0 is using the mic right now.
- macOS 14.2+: CoreAudio process objects (``kAudioProcessPropertyIsRunningInput``),
  per process, so this app's own capture is excluded. Older macOS: the
  default input device's ``IsRunningSomewhere`` flag, which cannot tell
  this app from others — only trusted while this app is not capturing.
- Linux: PulseAudio/PipeWire recording streams (source outputs), skipping
  this process, monitor sources and always-on tools (volume meters,
  EasyEffects…).

``CallDetector`` debounces the raw signal into "call started" / "call
ended" events; ``CallAssistant`` turns them into a proposal to record (or
an automatic start) and, when the call ends, a proposal to stop and save.
Probes run on a background thread: a hung audio server can never freeze
the widget.
"""

import ctypes
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from orizon_logging import get_logger

log = get_logger("calls")

MODES = ("off", "propose", "auto")

# Probes are cheap (a registry read, a few CoreAudio property reads, one
# PulseAudio round trip): poll often so the prompt shows up about 2 s
# after the call app grabs the microphone.
POLL_INTERVAL_S = 0.5
START_DELAY_S = 1.5     # mic held this long by another app = a call
END_DELAY_S = 12.0      # mic released this long = the call ended


# ---------- App names ----------

# Substring of the raw identifier (exe path, bundle id, stream app name),
# lower-case → name shown to the user. First match wins.
_KNOWN_APPS = (
    ("zoom", "Zoom"),
    ("teams", "Microsoft Teams"),
    ("webex", "Webex"),
    ("slack", "Slack"),
    ("discord", "Discord"),
    ("skype", "Skype"),
    ("facetime", "FaceTime"),
    ("avconferenced", "FaceTime"),
    ("whatsapp", "WhatsApp"),
    ("telegram", "Telegram"),
    ("signal", "Signal"),
    ("gotomeeting", "GoTo Meeting"),
    ("msedge", "Microsoft Edge"),
    ("microsoft edge", "Microsoft Edge"),
    ("edgemac", "Microsoft Edge"),
    ("chromium", "Chromium"),
    ("chrome", "Google Chrome"),
    ("firefox", "Firefox"),
    ("brave", "Brave"),
    ("vivaldi", "Vivaldi"),
    ("opera", "Opera"),
    ("safari", "Safari"),
    ("com.apple.webkit", "Safari"),
)

# Linux recording streams that are not calls: volume meters and
# always-on audio processing would otherwise look like an endless call.
_LINUX_BACKGROUND_APPS = (
    "pavucontrol", "pulseaudio volume control", "gnome-control-center",
    "gnome-settings-daemon", "plasmashell", "plasma-pa", "kmix",
    "easyeffects", "pulseeffects", "noisetorch", "speech-dispatcher",
    "pwvucontrol", "helvum", "qpwgraph", "xfce4-pulseaudio-plugin",
)


def friendly_app_name(raw: str) -> str:
    """Human name for a raw app identifier: 'C:#Program Files#Zoom#bin#Zoom.exe'
    → 'Zoom', 'com.google.Chrome.helper' → 'Google Chrome'."""
    raw = (raw or "").strip()
    low = raw.lower()
    for needle, name in _KNOWN_APPS:
        if needle in low:
            return name
    # Unknown app: last path component without extension, or the package
    # family name without its publisher hash.
    base = raw.replace("\\", "#").replace("/", "#").split("#")[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    if "_" in base and not base.startswith("_"):
        base = base.split("_", 1)[0]
    return base or raw or "un'altra app"


@dataclass
class MicUsage:
    """One probe: the apps (friendly names) using the microphone, other
    than this one. ``includes_self`` means the probe cannot tell this app's
    own capture apart, so it is only meaningful while this app is not
    capturing."""
    apps: List[str] = field(default_factory=list)
    includes_self: bool = False


def _dedupe(names: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ---------- Windows ----------

_WIN_CONSENT_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                    r"\CapabilityAccessManager\ConsentStore\microphone")


def _own_executables_windows() -> List[str]:
    """Registry spelling ('#' for '\\', lower-case) of this interpreter. A
    venv's python.exe is a launcher that runs the base interpreter, which
    is the process that actually opens the mic: exclude both."""
    out = set()
    for p in (sys.executable, getattr(sys, "_base_executable", "") or ""):
        if not p:
            continue
        folder = os.path.dirname(os.path.abspath(p))
        # The shortcut runs pythonw.exe, the terminal python.exe: the
        # base interpreter may be either flavour.
        for exe in (os.path.basename(p), "python.exe", "pythonw.exe"):
            out.add(os.path.join(folder, exe).replace("\\", "#").replace("/", "#").lower())
    return sorted(out)


def active_from_consent(entries, own_exes: Iterable[str]) -> List[str]:
    """``entries``: (key name, LastUsedTimeStart, LastUsedTimeStop). An
    entry is live when it started and has not stopped yet."""
    own = {e.lower() for e in own_exes}
    apps = []
    for name, start, stop in entries:
        if not start or stop:
            continue
        if name.lower() in own:
            continue
        apps.append(friendly_app_name(name))
    return _dedupe(apps)


def _iter_consent_entries():
    import winreg

    def walk(key, path):
        try:
            handle = winreg.OpenKey(key, path)
        except OSError:
            return
        with handle:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(handle, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    yield from walk(handle, name)
                    continue
                try:
                    with winreg.OpenKey(handle, name) as sub:
                        start = winreg.QueryValueEx(sub, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(sub, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                yield name, start, stop

    yield from walk(winreg.HKEY_CURRENT_USER, _WIN_CONSENT_KEY)


def probe_windows() -> Optional[MicUsage]:
    return MicUsage(active_from_consent(_iter_consent_entries(), _own_executables_windows()))


# ---------- macOS (CoreAudio via ctypes) ----------

def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


class _MacAudio:
    SYSTEM_OBJECT = 1
    SCOPE_GLOBAL = _fourcc("glob")
    ELEMENT_MAIN = 0
    PROCESS_LIST = _fourcc("prs#")
    PROCESS_PID = _fourcc("ppid")
    PROCESS_BUNDLE_ID = _fourcc("pbid")
    PROCESS_RUNNING_INPUT = _fourcc("piri")
    DEFAULT_INPUT = _fourcc("dIn ")
    RUNNING_SOMEWHERE = _fourcc("gone")
    UTF8 = 0x08000100

    def __init__(self):
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        addr_p = ctypes.POINTER(_AudioObjectPropertyAddress)
        ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, addr_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, addr_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._ca = ca
        self._cf = cf

    def _addr(self, selector):
        return _AudioObjectPropertyAddress(selector, self.SCOPE_GLOBAL, self.ELEMENT_MAIN)

    def _get(self, obj, selector, ctype):
        value = ctype()
        size = ctypes.c_uint32(ctypes.sizeof(value))
        addr = self._addr(selector)
        status = self._ca.AudioObjectGetPropertyData(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(value))
        if status != 0:
            raise OSError(status, f"CoreAudio property {selector:#x}")
        return value.value

    def process_objects(self) -> Optional[List[int]]:
        """CoreAudio process objects (macOS 14.2+), None if unsupported."""
        addr = self._addr(self.PROCESS_LIST)
        size = ctypes.c_uint32(0)
        if self._ca.AudioObjectGetPropertyDataSize(
                self.SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
            return None
        count = size.value // 4
        if count == 0:
            return []
        arr = (ctypes.c_uint32 * count)()
        if self._ca.AudioObjectGetPropertyData(
                self.SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                ctypes.byref(size), arr) != 0:
            return None
        return list(arr[: size.value // 4])

    def bundle_id(self, obj) -> str:
        ref = ctypes.c_void_p()
        size = ctypes.c_uint32(ctypes.sizeof(ref))
        addr = self._addr(self.PROCESS_BUNDLE_ID)
        if self._ca.AudioObjectGetPropertyData(
                obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(ref)) != 0:
            return ""
        if not ref.value:
            return ""
        try:
            buf = ctypes.create_string_buffer(512)
            if self._cf.CFStringGetCString(ref, buf, len(buf), self.UTF8):
                return buf.value.decode("utf-8", errors="replace")
            return ""
        finally:
            self._cf.CFRelease(ref)

    def default_input_running(self) -> bool:
        device = self._get(self.SYSTEM_OBJECT, self.DEFAULT_INPUT, ctypes.c_uint32)
        if not device:
            return False
        return bool(self._get(device, self.RUNNING_SOMEWHERE, ctypes.c_uint32))


def _macos_app_name(pid: int, bundle_id: str) -> str:
    if bundle_id:
        name = friendly_app_name(bundle_id)
        if name != bundle_id:
            return name
    try:
        from AppKit import NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None and app.localizedName():
            return friendly_app_name(str(app.localizedName()))
    except Exception:
        pass
    return friendly_app_name(bundle_id) if bundle_id else f"processo {pid}"


_mac_audio: "Optional[_MacAudio]" = None


def probe_macos() -> Optional[MicUsage]:
    global _mac_audio
    if _mac_audio is None:
        _mac_audio = _MacAudio()
    ca = _mac_audio
    objects = ca.process_objects()
    if objects is None:
        # Before macOS 14.2: device-level only.
        if ca.default_input_running():
            return MicUsage(["un'altra app"], includes_self=True)
        return MicUsage([], includes_self=True)
    own = os.getpid()
    apps = []
    for obj in objects:
        try:
            if not ca._get(obj, ca.PROCESS_RUNNING_INPUT, ctypes.c_uint32):
                continue
            pid = ca._get(obj, ca.PROCESS_PID, ctypes.c_int32)
        except OSError:
            continue
        if pid == own:
            continue
        apps.append(_macos_app_name(pid, ca.bundle_id(obj)))
    return MicUsage(_dedupe(apps))


# ---------- Linux (PulseAudio / PipeWire) ----------

def active_from_pulse(outputs, sources, own_pid: int) -> List[str]:
    """``outputs``: pulsectl source outputs (recording streams);
    ``sources``: {index: source}. Keeps live streams of other processes
    on real inputs (not monitors), minus known background tools."""
    from platform_audio import _is_monitor_source
    apps = []
    for so in outputs:
        props = getattr(so, "proplist", {}) or {}
        if str(props.get("application.process.id", "")) == str(own_pid):
            continue
        if getattr(so, "corked", False):
            continue
        src = sources.get(getattr(so, "source", None))
        if src is not None and _is_monitor_source(src):
            continue
        raw = (props.get("application.name")
               or props.get("application.process.binary")
               or getattr(so, "name", "") or "")
        binary = str(props.get("application.process.binary", "")).lower()
        low = str(raw).lower()
        if any(bg in low or bg == binary for bg in _LINUX_BACKGROUND_APPS):
            continue
        apps.append(friendly_app_name(str(raw)))
    return _dedupe(apps)


class _PulseProbe:
    def __init__(self):
        self._pulse = None

    def __call__(self) -> Optional[MicUsage]:
        import pulsectl
        try:
            if self._pulse is None:
                # No autospawn: polling must never start a sound server.
                self._pulse = pulsectl.Pulse("orizon-call-calls", connect=False)
                self._pulse.connect(autospawn=False)
            sources = {s.index: s for s in self._pulse.source_list()}
            outputs = self._pulse.source_output_list()
        except Exception:
            self.close()   # reconnect on the next poll (server restart)
            raise
        return MicUsage(active_from_pulse(outputs, sources, os.getpid()))

    def close(self) -> None:
        if self._pulse is not None:
            try:
                self._pulse.close()
            except Exception:
                pass
            self._pulse = None


def platform_probe() -> "Optional[Callable[[], Optional[MicUsage]]]":
    """The microphone-usage probe for this platform, None if there is none."""
    if sys.platform == "win32":
        return probe_windows
    if sys.platform == "darwin":
        return probe_macos
    if sys.platform.startswith("linux"):
        try:
            import pulsectl  # noqa: F401
        except Exception:
            return None
        return _PulseProbe()
    return None


# ---------- Debouncing state machine ----------

@dataclass
class CallEvent:
    kind: str   # "started" | "ended"
    app: str


class CallDetector:
    """Pure state machine (no Qt, no clock): feed it probes with a
    timestamp, get "started"/"ended" events. A brief mic grab (a
    notification sound check, a sub-second test of the device) is not a call;
    a few seconds of silence mid-call (an app reopening the device) is
    not the end of one."""

    def __init__(self, start_delay: float = START_DELAY_S,
                 end_delay: float = END_DELAY_S, ignored: Iterable[str] = ()):
        self.start_delay = start_delay
        self.end_delay = end_delay
        self.ignored = {a.lower() for a in ignored}
        self.active_app: Optional[str] = None
        self._candidate_since: Optional[float] = None
        self._absent_since: Optional[float] = None

    def reset(self) -> None:
        self.active_app = None
        self._candidate_since = None
        self._absent_since = None

    def update(self, now: float, apps: Optional[List[str]]) -> Optional[CallEvent]:
        """``apps`` None = unknown right now (cannot observe): keep the
        current state, restart the debounce timers."""
        if apps is None:
            self._candidate_since = None
            self._absent_since = None
            return None
        apps = [a for a in apps if a.lower() not in self.ignored]
        if self.active_app is None:
            if not apps:
                self._candidate_since = None
                return None
            if self._candidate_since is None:
                self._candidate_since = now
            if now - self._candidate_since >= self.start_delay:
                self.active_app = apps[0]
                self._candidate_since = None
                self._absent_since = None
                return CallEvent("started", self.active_app)
            return None
        if apps:
            self._absent_since = None
            return None
        if self._absent_since is None:
            self._absent_since = now
        if now - self._absent_since >= self.end_delay:
            app, self.active_app = self.active_app, None
            self._absent_since = None
            return CallEvent("ended", app)
        return None


# ---------- Background polling ----------

class _ProbeThread(threading.Thread):

    def __init__(self, probe, deliver, interval: float):
        super().__init__(daemon=True, name="call-detect")
        self._probe = probe
        self._deliver = deliver
        self._interval = interval
        self._stop_event = threading.Event()
        self._failures = 0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                usage = self._probe()
                self._failures = 0
            except Exception:
                usage = None
                self._failures += 1
                if self._failures == 1:
                    log.warning("Call detection probe failed", exc_info=True)
            if not self._stop_event.is_set():
                self._deliver(usage)
            # A probe that keeps failing (no sound server, API missing)
            # backs off up to 30 s instead of retrying twice a second.
            delay = self._interval if not self._failures else min(
                30.0, self._interval * (2 ** min(self._failures, 6)))
            self._stop_event.wait(delay)
        close = getattr(self._probe, "close", None)
        if close is not None:
            close()


class CallAssistant(QObject):
    """
    Glue between the detector and the widget.

    ``propose``: when a call starts and nothing is recording, a prompt
    (toast with buttons, itself hidden from screen sharing) offers to
    record. ``auto``: the recording starts by itself. When the call ends,
    a recording linked to it gets a prompt to stop and save (``auto``
    stops by itself the recordings it started).

    The widget is duck-typed (``recorder_state_name``, ``is_busy``,
    ``is_capturing_mic``, ``show_prompt``, ``start_recording_for_call``,
    ``stop_recording_for_call``, ``notify``, ``recording_started``,
    ``recording_stopped``) so the logic is testable without audio.
    """

    _usage = pyqtSignal(object)   # probe thread → GUI thread

    def __init__(self, widget, mode: str = "propose", ignored: Iterable[str] = (),
                 probe=None, interval: float = POLL_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic,
                 on_ignored_changed: "Optional[Callable[[List[str]], None]]" = None):
        super().__init__(widget if isinstance(widget, QObject) else None)
        self._widget = widget
        self._probe = probe if probe is not None else platform_probe()
        self._interval = interval
        self._clock = clock
        self._on_ignored_changed = on_ignored_changed
        self._detector = CallDetector(ignored=ignored)
        self._ignored = list(ignored)
        self._mode = "off"
        self._thread: Optional[_ProbeThread] = None
        self._prompt = None
        self._linked = False        # current recording belongs to the detected call
        self._auto_started = False  # ... and was started automatically
        self._usage.connect(self._on_usage)
        widget.recording_started.connect(self._on_recording_started)
        widget.recording_stopped.connect(self._on_recording_stopped)
        self.set_mode(mode)

    # -- configuration --

    @property
    def available(self) -> bool:
        return self._probe is not None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def ignored(self) -> List[str]:
        return list(self._ignored)

    def set_ignored(self, apps: Iterable[str]) -> None:
        self._ignored = _dedupe(apps)
        self._detector.ignored = {a.lower() for a in self._ignored}

    def set_mode(self, mode: str) -> None:
        mode = mode if mode in MODES else "propose"
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "off" or self._probe is None:
            self._stop_thread()
            self._close_prompt()
            self._detector.reset()
            if mode != "off" and self._probe is None:
                log.info("Call detection is not available on this system.")
        else:
            self._start_thread()

    def shutdown(self) -> None:
        self._mode = "off"   # drop a probe result still in flight
        self._stop_thread()
        self._close_prompt()

    def _start_thread(self) -> None:
        if self._thread is None:
            self._thread = _ProbeThread(self._probe, self._usage.emit, self._interval)
            self._thread.start()

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread.stop()
            self._thread = None

    # -- probe results (GUI thread) --

    @pyqtSlot(object)
    def _on_usage(self, usage) -> None:
        if self._mode == "off":
            return
        self.feed(usage)

    def feed(self, usage: Optional[MicUsage]) -> None:
        apps = None
        if usage is not None:
            apps = usage.apps
            if usage.includes_self and self._widget.is_capturing_mic():
                apps = None   # our own capture would look like a call
        event = self._detector.update(self._clock(), apps)
        if event is None:
            return
        log.info("Call %s (%s)", event.kind, event.app)
        if event.kind == "started":
            self._on_call_started(event.app)
        else:
            self._on_call_ended(event.app)

    def _idle(self) -> bool:
        return self._widget.recorder_state_name() == "idle" and not self._widget.is_busy

    def _on_call_started(self, app: str) -> None:
        if not self._idle():
            # Already recording (started by hand just before): the call
            # end will still offer to stop it.
            self._linked = self._widget.recorder_state_name() in ("recording", "paused")
            return
        if self._mode == "auto":
            self._auto_started = True
            self._linked = True
            self._widget.start_recording_for_call(app, automatic=True)
            return
        self._close_prompt()
        self._prompt = self._widget.show_prompt(
            f"Sembra che sia iniziata una call su {app}.\nVuoi registrarla?",
            [("Registra", lambda: self._accept(app), True),
             ("Non ora", self._dismiss, False),
             ("Mai per quest'app", lambda: self._ignore(app), False)],
            on_timeout=self._dismiss)

    def _on_call_ended(self, app: str) -> None:
        self._close_prompt()
        state = self._widget.recorder_state_name()
        if not self._linked or state not in ("recording", "paused") or self._widget.is_busy:
            self._linked = self._auto_started = False
            return
        if self._mode == "auto" and self._auto_started:
            self._widget.stop_recording_for_call(app, automatic=True)
            return
        self._prompt = self._widget.show_prompt(
            f"La call su {app} sembra terminata.\nFermare e salvare la registrazione?",
            [("Stop e salva", lambda: self._widget.stop_recording_for_call(app, automatic=False), True),
             ("Continua", self._clear_prompt, False)],
            on_timeout=self._clear_prompt)

    # -- prompt actions --

    def _accept(self, app: str) -> None:
        self._prompt = None
        if self._idle() and self._detector.active_app is not None:
            self._linked = True
            self._widget.start_recording_for_call(app, automatic=False)

    def _dismiss(self) -> None:
        # "Not now": the detector stays in the call, so nothing is
        # proposed again until this call ends and a new one starts.
        self._prompt = None

    def _ignore(self, app: str) -> None:
        self._prompt = None
        self.set_ignored(self._ignored + [app])
        self._detector.reset()
        if self._on_ignored_changed is not None:
            self._on_ignored_changed(self.ignored)
        self._widget.notify(f"Non proporrò più di registrare le call di {app}. "
                            "Puoi ripristinarlo dalle Impostazioni.",
                            kind="info", duration_ms=5000)

    def _clear_prompt(self) -> None:
        self._prompt = None

    def _close_prompt(self) -> None:
        prompt, self._prompt = self._prompt, None
        if prompt is not None:
            try:
                prompt.close_silently()
            except Exception:
                pass

    # -- recording lifecycle --

    @pyqtSlot(str)
    def _on_recording_started(self, _path: str) -> None:
        # Started by hand (or via API) while a call is on: link it, and
        # drop a "record?" prompt that is now moot.
        if self._detector.active_app is not None:
            self._linked = True
            self._close_prompt()

    @pyqtSlot(str)
    def _on_recording_stopped(self, _path: str) -> None:
        self._linked = False
        self._auto_started = False
        # A pending "stop?" prompt makes no sense any more.
        self._close_prompt()
