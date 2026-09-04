"""
Floating pill/circle widget for the audio recorder.

Idle: dark circle with green black hole logo — click to start recording.
Recording: smooth animated expansion to pill with timer, VU meters,
mute, pause and stop buttons.
Paused: pill with yellow indicator, resume and stop buttons.
Saving: pill shows a "Salvataggio…" state while a worker thread
finalizes the file — the GUI thread is never blocked.
Right-click: context menu (start/stop, pause, mute, open folder, quit).
Tray / menu-bar icon (where the platform has one): the same controls plus
"show the widget", so the app is always reachable even when the floating
circle ended up behind a fullscreen window or on a disconnected screen.

All recorder calls that can block (start/stop) run on worker threads and
report back via queued signals, so the widget stays responsive and
animations never stutter. A window-manager close request never kills a
recording: it is routed through the normal stop-and-save path.
"""

import sys
import threading
import traceback
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSettings,
    Qt,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QWidget,
)

from audio_recorder import AudioRecorder, RecordingState
from orizon_logging import get_logger

log = get_logger("widget")


# ---------- Colors (Orizon design system, orizon-design-theme tokens) ----------

COLOR_GREEN = QColor(107, 239, 26)        # brand.400 #6bef1a (MAIN_BRAND_COLOR)
COLOR_GREEN_LIGHT = QColor(142, 242, 74)  # brand.300 #8ef24a
COLOR_GREEN_DARK = QColor(74, 195, 0)     # brand.500 #4ac300
COLOR_BLUE = QColor(80, 180, 255)
COLOR_RED = QColor(220, 38, 38)           # error.600 #dc2626
COLOR_RED_DARK = QColor(185, 28, 28)      # error.700 #b91c1c
COLOR_YELLOW = QColor(234, 179, 8)        # yellow.500 #eab308
COLOR_BG_DARK = QColor(11, 18, 32, 240)   # gray.900/950 blend (slate)
COLOR_WHITE = QColor(255, 255, 255)
COLOR_WHITE_DIM = QColor(203, 213, 225)   # gray.300 #cbd5e1
COLOR_BTN_HOVER = QColor(51, 65, 85)      # gray.700 #334155
COLOR_BORDER = QColor(51, 65, 85)         # gray.700 #334155

# ---------- Dimensions ----------

CIRCLE_SIZE = 60
PILL_WIDTH = 290
PILL_HEIGHT = 56
EXPAND_DURATION = 400   # ms
SHRINK_DURATION = 250   # ms

_SETTINGS_ORG = "OrizonCall"
_SETTINGS_APP = "OrizonCall"

# ---------- Orizon logo (official glyph, tinted brand green) ----------

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_ICONS_DIR = _ASSETS_DIR / "icons"
_LOGO_SVG_PATH = _ASSETS_DIR / "orizon-icon.svg"
_TRAY_GLYPH_PATH = _ICONS_DIR / "orizon-glyph-512.png"
_logo_renderer = None  # lazy singleton; False = tried and failed

_IDLE_TOOLTIP = "Clicca per registrare — trascina per spostare"


def app_icon() -> QIcon:
    """The application icon with every rendered size attached, so Qt
    picks a crisp bitmap for title bars, task switchers and the tray
    instead of down-scaling the 256 px PNG. On Windows the multi-size
    .ico is preferred (native tray/taskbar sizes)."""
    ico = _ICONS_DIR / "orizon-call.ico"
    if sys.platform == 'win32' and ico.exists():
        icon = QIcon(str(ico))
        if not icon.isNull():
            return icon
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256, 512):
        png = _ICONS_DIR / f"orizon-call-{size}.png"
        if png.exists():
            icon.addFile(str(png))
    return icon


def apply_macos_floating(widget: QWidget, activate: bool = False) -> None:
    """macOS: put ``widget``'s NSWindow at the floating level and let it
    join every Space, including fullscreen apps — the app's main use case
    is a call running fullscreen. Applied to the floating widget, every
    toast and every dialog, because Qt creates a fresh NSWindow for each.
    ``activate`` brings the (accessory, dock-less) app forward so a modal
    dialog does not open behind the active application."""
    if sys.platform != 'darwin':
        return
    try:
        from AppKit import NSApplication, NSFloatingWindowLevel
        # NSWindowCollectionBehavior: CanJoinAllSpaces = 1 << 0,
        # FullScreenAuxiliary = 1 << 8 (NOT 1 << 4, which is
        # 'Stationary' and would drop the widget from fullscreen apps).
        behavior = (1 << 0) | (1 << 8)
        ns_app = NSApplication.sharedApplication()
        window = None
        try:
            import objc
            view = objc.objc_object(c_void_p=int(widget.winId()))
            window = view.window()
        except Exception:
            window = None
        targets = [window] if window is not None else list(ns_app.windows())
        for win in targets:
            win.setLevel_(NSFloatingWindowLevel)
            win.setCollectionBehavior_(behavior)
        if activate:
            ns_app.activateIgnoringOtherApps_(True)
    except Exception:
        pass


def _get_logo_renderer():
    """QSvgRenderer for the official Orizon glyph, or None if unavailable
    (missing asset / QtSvg not installed) — callers fall back to the
    procedural vortex logo."""
    global _logo_renderer
    if _logo_renderer is None:
        try:
            from PyQt6.QtCore import QByteArray
            from PyQt6.QtSvg import QSvgRenderer
            svg = _LOGO_SVG_PATH.read_text(encoding="utf-8")
            svg = svg.replace("currentColor", COLOR_GREEN.name())
            renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            _logo_renderer = renderer if renderer.isValid() else False
        except Exception:
            _logo_renderer = False
    return _logo_renderer or None


def _screen_for(point: QPoint):
    screen = QApplication.screenAt(point)
    return screen or QApplication.primaryScreen()


# ---------- Toast ----------

class Toast(QWidget):
    """
    Frameless auto-dismissing notification with fade in/out.
    Replaces QToolTip (which vanishes on mouse move and looks like a
    debug artifact). Click to trigger the optional action and dismiss.
    """

    _active: "list[Toast]" = []   # keep references while visible

    def __init__(self, text: str, kind: str = "info", on_click=None,
                 duration_ms: int = 3500):
        super().__init__(None)
        self._on_click = on_click
        self._accent = {
            "info": COLOR_GREEN,
            "error": COLOR_RED,
            "warn": COLOR_YELLOW,
        }.get(kind, COLOR_GREEN)

        flags = (Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint)
        if sys.platform != 'darwin':
            # On macOS a Tool window hides whenever the app is inactive —
            # which is this app's normal condition (accessory, no dock).
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        if on_click is not None:
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        label = QLabel(text, self)
        label.setStyleSheet(
            f"color: {COLOR_WHITE.name()}; font-size: 13px; background: transparent;"
        )
        label.setWordWrap(True)
        label.setMaximumWidth(320)
        layout.addWidget(label)
        self.adjustSize()

        self._fade_in = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_in.setDuration(160)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)

        self._fade_out = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_out.setDuration(300)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.finished.connect(self._dismiss)

        QTimer.singleShot(duration_ms, self._start_fade_out)

    # -- lifecycle --

    def show_above(self, anchor: QWidget) -> None:
        # Stack above the toasts still visible instead of covering them
        # (the older one is often the more important message); keep the
        # pile short.
        visible = [t for t in Toast._active if t.isVisible()]
        if len(visible) >= 4:
            visible[0]._start_fade_out()
            visible = visible[1:]
        offset = sum(t.height() + 6 for t in visible)

        center = anchor.mapToGlobal(anchor.rect().center())
        x = center.x() - self.width() // 2
        y = anchor.mapToGlobal(anchor.rect().topLeft()).y() - self.height() - 10 - offset
        screen = _screen_for(center)
        if screen:
            avail = screen.availableGeometry()
            x = max(avail.left() + 6, min(x, avail.right() - self.width() - 6))
            if y < avail.top() + 6:  # no room above -> below
                y = anchor.mapToGlobal(anchor.rect().bottomLeft()).y() + 10 + offset
        self.move(x, y)
        self.setWindowOpacity(0.0)
        self.show()
        apply_macos_floating(self)
        self._fade_in.start()
        Toast._active.append(self)

    def _start_fade_out(self) -> None:
        if self.isVisible():
            self._fade_out.start()

    def _dismiss(self) -> None:
        self.close()
        if self in Toast._active:
            Toast._active.remove(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._on_click is not None:
            try:
                self._on_click()
            except Exception:
                log.exception("Toast action failed")
        self._dismiss()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.fillPath(path, QBrush(QColor(15, 23, 42, 245)))
        p.setPen(QPen(QColor(self._accent.red(), self._accent.green(),
                             self._accent.blue(), 160), 1.2))
        p.drawPath(path)
        # Accent bar on the left edge.
        bar = QPainterPath()
        bar.addRoundedRect(QRectF(4, 8, 3, self.height() - 16), 1.5, 1.5)
        p.fillPath(bar, QBrush(self._accent))
        p.end()


# ---------- Small widgets ----------

class StatusDot(QWidget):
    """Small pulsing dot indicating recording/paused/saving status."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._opacity = 1.0
        self._color = COLOR_RED

        self._pulse_anim = QPropertyAnimation(self, b"dot_opacity")
        self._pulse_anim.setDuration(800)
        self._pulse_anim.setStartValue(1.0)
        self._pulse_anim.setEndValue(0.25)
        self._pulse_anim.setLoopCount(-1)
        self._pulse_anim.setEasingCurve(QEasingCurve.Type.InOutSine)

    def start_pulsing(self, color: QColor = COLOR_RED) -> None:
        self._color = color
        self._pulse_anim.start()

    def stop_pulsing(self, color: QColor = COLOR_YELLOW) -> None:
        self._pulse_anim.stop()
        self._color = color
        self._opacity = 1.0
        self.update()

    def get_dot_opacity(self) -> float:
        return self._opacity

    def set_dot_opacity(self, val: float) -> None:
        self._opacity = val
        self.update()

    dot_opacity = pyqtProperty(float, get_dot_opacity, set_dot_opacity)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self._color)
        color.setAlphaF(self._opacity)
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 10, 10)
        painter.end()


class LevelBar(QWidget):
    """
    Thin vertical VU meter with smooth ballistics: fast attack, slow
    release, and a peak-hold marker that decays — reads like a real
    meter instead of a flickering bar.
    """

    ATTACK = 0.55    # fraction of the way up per tick
    RELEASE = 0.12   # fraction of the way down per tick
    PEAK_DECAY = 0.025

    def __init__(self, parent=None, tint: QColor = None):
        super().__init__(parent)
        self.setFixedSize(6, 32)
        self._display = 0.0
        self._peak = 0.0
        self._base_color = tint if tint is not None else COLOR_GREEN

    def push_level(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        if level > self._display:
            self._display += (level - self._display) * self.ATTACK
        else:
            self._display += (level - self._display) * self.RELEASE
        if self._display > self._peak:
            self._peak = self._display
        else:
            self._peak = max(0.0, self._peak - self.PEAK_DECAY)
        self.update()

    def reset(self) -> None:
        self._display = 0.0
        self._peak = 0.0
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        p.setBrush(QColor(30, 30, 30))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, w, h, 3, 3)

        if self._display > 0.01:
            fill_h = max(2, int(h * self._display))
            if self._display < 0.75:
                color = self._base_color
            elif self._display < 0.9:
                color = COLOR_YELLOW
            else:
                color = COLOR_RED
            p.setBrush(color)
            p.drawRoundedRect(0, h - fill_h, w, fill_h, 3, 3)

        if self._peak > 0.04:
            peak_y = h - max(2, int(h * self._peak))
            p.setPen(QPen(QColor(255, 255, 255, 170), 1))
            p.drawLine(1, peak_y, w - 1, peak_y)

        p.end()


class DualLevelBar(QWidget):
    """Two side-by-side VU meters: mic + system audio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 36)
        self._mic = LevelBar(self, tint=COLOR_GREEN)
        self._sys = LevelBar(self, tint=COLOR_BLUE)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._mic)
        layout.addWidget(self._sys)
        self._mic.setToolTip("Microfono (tu)")
        self._sys.setToolTip("Audio di sistema (gli altri)")

    def push_levels(self, mic: float, sys_level: float) -> None:
        self._mic.push_level(mic)
        self._sys.push_level(sys_level)

    def reset(self) -> None:
        self._mic.reset()
        self._sys.reset()


class IconButton(QPushButton):
    """
    Flat round button with a vector-painted icon (no emoji: consistent
    rendering on every platform and theme). Icons: mic, mic_off, pause,
    play, stop. `active` paints a colored background (used for mute).
    """

    def __init__(self, icon_name: str, parent=None):
        super().__init__("", parent)
        self._icon_name = icon_name
        self._active = False
        self.setFixedSize(34, 34)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def set_icon_name(self, name: str) -> None:
        if name != self._icon_name:
            self._icon_name = name
            self.update()

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        rect = QRectF(1, 1, w - 2, h - 2)

        # Background circle
        if self._active:
            bg = COLOR_RED_DARK if self.isDown() else COLOR_RED
            p.setBrush(QBrush(bg))
            p.setPen(Qt.PenStyle.NoPen)
        elif self.isDown():
            p.setBrush(QBrush(COLOR_BTN_HOVER))
            p.setPen(QPen(COLOR_BORDER, 1))
        elif self.underMouse():
            p.setBrush(QBrush(QColor(63, 63, 70, 160)))
            p.setPen(QPen(COLOR_BORDER, 1))
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(COLOR_BORDER, 1))
        p.drawEllipse(rect)

        color = COLOR_WHITE
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        cx, cy = w / 2, h / 2
        name = self._icon_name

        if name in ("mic", "mic_off"):
            # Capsule body
            body = QRectF(cx - 3, cy - 8, 6, 10)
            p.setBrush(QBrush(color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(body, 3, 3)
            # Stand arc + base
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(pen)
            arc_rect = QRectF(cx - 6, cy - 4, 12, 10)
            p.drawArc(arc_rect, 180 * 16, 180 * 16)
            p.drawLine(QPointF(cx, cy + 6), QPointF(cx, cy + 9))
            if name == "mic_off":
                slash = QPen(color, 2.0)
                slash.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(slash)
                p.drawLine(QPointF(cx - 7, cy - 8), QPointF(cx + 7, cy + 8))
        elif name == "pause":
            p.setBrush(QBrush(color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(cx - 5, cy - 6, 3.4, 12), 1.4, 1.4)
            p.drawRoundedRect(QRectF(cx + 1.6, cy - 6, 3.4, 12), 1.4, 1.4)
        elif name == "play":
            p.setBrush(QBrush(color))
            p.setPen(Qt.PenStyle.NoPen)
            tri = QPainterPath()
            tri.moveTo(cx - 4, cy - 6.5)
            tri.lineTo(cx + 6, cy)
            tri.lineTo(cx - 4, cy + 6.5)
            tri.closeSubpath()
            p.drawPath(tri)
        elif name == "stop":
            p.setBrush(QBrush(color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(cx - 5, cy - 5, 10, 10), 2.5, 2.5)

        p.end()


# ---------- Tray / menu-bar icon ----------

def _tray_icon() -> QIcon:
    """macOS: the brand glyph as a *template* (mask) icon, so the menu-bar
    item follows the light/dark menu bar like native status items.
    Elsewhere: the full-colour app icon (Qt scales it for the tray)."""
    if sys.platform == 'darwin' and _TRAY_GLYPH_PATH.exists():
        icon = QIcon(str(_TRAY_GLYPH_PATH))
        icon.setIsMask(True)
        return icon
    return app_icon()


class TrayController(QObject):
    """
    System-tray (Windows/Linux) or menu-bar (macOS) presence.

    The floating widget is a Tool window: no taskbar/dock entry. If it ends
    up behind a fullscreen app or on a screen that was unplugged, the tray
    icon is the way back ("Mostra il widget"). The menu mirrors the widget's
    right-click menu; native notifications cover the events a user may miss
    while looking at another window (auto-stop, save errors).

    Silently does nothing on platforms without a tray (e.g. some Wayland
    sessions without the StatusNotifier extension) — the widget alone
    still works exactly as before.
    """

    def __init__(self, widget: "FloatingRecorderWidget"):
        super().__init__(widget)
        self._widget = widget
        self._tray: "QSystemTrayIcon | None" = None
        self._last_state: "tuple[str, bool] | None" = None
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            icon = _tray_icon()
            if icon.isNull():
                return
            tray = QSystemTrayIcon(icon, self)
            self._menu = QMenu()
            self._act_record = self._menu.addAction("Avvia registrazione", widget.toggle_recording)
            self._act_pause = self._menu.addAction("Pausa", widget.toggle_pause)
            self._menu.addSeparator()
            self._menu.addAction("Mostra il widget", widget.bring_to_front)
            self._act_settings = self._menu.addAction("Impostazioni…", widget.open_settings)
            self._menu.addAction("Apri cartella registrazioni", widget.open_recordings_folder)
            self._menu.addSeparator()
            self._menu.addAction("Chiudi Orizon Call", widget.request_quit_interactive)
            self._menu.aboutToShow.connect(self._refresh)
            tray.setContextMenu(self._menu)
            tray.setToolTip("Orizon Call — pronto")
            tray.activated.connect(self._on_activated)
            tray.messageClicked.connect(widget.open_recordings_folder)
            tray.show()
            self._tray = tray
        except Exception:
            log.exception("Tray icon unavailable")
            self._tray = None

    @property
    def available(self) -> bool:
        return self._tray is not None

    def _on_activated(self, reason) -> None:
        # Left click / double click: bring the widget back. The context
        # menu is opened by the platform itself.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._widget.bring_to_front()

    def _refresh(self) -> None:
        state = self._widget.recorder_state_name()
        busy = self._widget.is_busy
        if state in ("recording", "paused"):
            self._act_record.setText("Stop e salva")
            self._act_record.setEnabled(not busy)
        elif state == "stopping":
            self._act_record.setText("Salvataggio in corso…")
            self._act_record.setEnabled(False)
        else:
            self._act_record.setText("Avvia registrazione")
            self._act_record.setEnabled(not busy)
        self._act_pause.setVisible(state in ("recording", "paused"))
        self._act_pause.setText("Riprendi" if state == "paused" else "Pausa")
        self._act_settings.setEnabled(state == "idle" and not busy)

    def set_state(self, state: str, busy: bool) -> None:
        """Keep the tooltip in sync; called on every state transition only
        (not every tick — tray tooltips are OS calls)."""
        if self._tray is None or (state, busy) == self._last_state:
            return
        self._last_state = (state, busy)
        label = {
            "recording": "in registrazione",
            "paused": "in pausa",
            "stopping": "salvataggio in corso…",
        }.get(state, "pronto")
        if busy and state == "idle":
            label = "avvio in corso…"
        self._tray.setToolTip(f"Orizon Call — {label}")

    def notify(self, title: str, message: str, critical: bool = False,
               msecs: int = 6000) -> None:
        if self._tray is None:
            return
        icon = (QSystemTrayIcon.MessageIcon.Critical if critical
                else QSystemTrayIcon.MessageIcon.Information)
        try:
            self._tray.showMessage(title, message, icon, msecs)
        except Exception:
            log.debug("Tray notification failed", exc_info=True)

    def hide(self) -> None:
        if self._tray is not None:
            self._tray.hide()


# ---------- Main widget ----------

class FloatingRecorderWidget(QWidget):
    """
    Frameless, always-on-top floating widget.
    Circle (idle) -> Pill (recording/paused/saving) with smooth animation.
    """

    recording_started = pyqtSignal(str)
    recording_stopped = pyqtSignal(str)
    recording_error = pyqtSignal(str)
    mute_changed = pyqtSignal(bool)

    # Worker-thread results, delivered queued on the GUI thread.
    _start_done = pyqtSignal(bool, str)   # ok, path-or-error
    _stop_done = pyqtSignal(bool, str, bool)  # ok, path-or-error, quit_after

    def __init__(self, recorder: AudioRecorder, parent=None):
        super().__init__(parent)
        self._recorder = recorder
        self._drag_pos: QPointF | None = None
        self._is_dragging = False
        self._busy = False          # a start/stop worker is in flight
        self._ui_recording = False  # pill currently shown
        self._pending_stop = False  # stop requested while a start worker ran
        self._quit_when_done = False  # quit once the in-flight stop finishes
        self._hover = 0.0
        self._raise_timer: QTimer | None = None
        self._closing = False       # aboutToQuit ran: let close events through
        self._quit_interactive = True   # show dialogs on the quit path?
        self._settings_dialog_open = False
        self._pending_settings: dict | None = None  # saved while recording
        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)

        # Animation state: 0.0 = circle, 1.0 = pill
        self._anim_progress = 0.0
        self._anim_direction = 0  # 0=idle, 1=expanding, -1=shrinking
        self._center_anchor = QPointF()

        self._setup_window()
        self._setup_pill_contents()
        self._setup_animations()
        self._setup_timer()
        self._position_on_screen()

        self._recorder.set_error_callback(self.recording_error.emit)
        self.recording_error.connect(self._on_error)
        self._start_done.connect(self._on_start_done)
        self._stop_done.connect(self._on_stop_done)

        self._tray = TrayController(self)

        # A monitor unplugged mid-call must not strand the widget off-screen.
        app = QApplication.instance()
        if app is not None:
            app.screenRemoved.connect(lambda _s: QTimer.singleShot(200, self._ensure_on_screen))
            app.primaryScreenChanged.connect(lambda _s: QTimer.singleShot(200, self._ensure_on_screen))

    # ---------- Animated properties ----------

    def _get_anim_progress(self) -> float:
        return self._anim_progress

    def _set_anim_progress(self, val: float) -> None:
        self._anim_progress = val
        self._update_geometry_from_progress()
        self.update()

    anim_progress = pyqtProperty(float, _get_anim_progress, _set_anim_progress)

    def _get_hover(self) -> float:
        return self._hover

    def _set_hover(self, val: float) -> None:
        self._hover = val
        self.update()

    hover_progress = pyqtProperty(float, _get_hover, _set_hover)

    def _update_geometry_from_progress(self) -> None:
        """Resize and reposition the window based on animation progress."""
        p = max(0.0, min(1.0, self._anim_progress))
        w = int(CIRCLE_SIZE + p * (PILL_WIDTH - CIRCLE_SIZE))
        h = int(CIRCLE_SIZE + p * (PILL_HEIGHT - CIRCLE_SIZE))

        cx = self._center_anchor.x()
        cy = self._center_anchor.y()
        x = int(cx - w / 2)
        y = int(cy - h / 2)

        # Clamp to the screen the widget is actually on (not the primary:
        # on a secondary monitor that would teleport the widget away).
        screen = _screen_for(QPoint(int(cx), int(cy)))
        if screen:
            avail = screen.availableGeometry()
            if x + w > avail.right():
                x = avail.right() - w - 5
            if x < avail.left():
                x = avail.left() + 5
            if y + h > avail.bottom():
                y = avail.bottom() - h - 5
            if y < avail.top():
                y = avail.top() + 5

        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.setGeometry(x, y, w, h)

    # ---------- Setup ----------

    def _setup_window(self) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        if sys.platform != 'darwin':
            flags |= Qt.WindowType.Tool

        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(CIRCLE_SIZE, CIRCLE_SIZE)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip(_IDLE_TOOLTIP)
        # Keyboard: reachable once activated (tray → "Mostra il widget").
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Orizon Call")

        self._hover_anim = QPropertyAnimation(self, b"hover_progress")
        self._hover_anim.setDuration(180)

    def _setup_pill_contents(self) -> None:
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(12, 0, 8, 0)
        self._layout.setSpacing(5)

        self._status_dot = StatusDot(self)
        self._level_bar = DualLevelBar(self)

        self._timer_label = QLabel("00:00", self)
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        self._timer_label.setFont(font)
        self._timer_label.setStyleSheet(
            f"color: {COLOR_WHITE.name()}; background: transparent;"
        )

        self._mute_btn = IconButton("mic", self)
        self._mute_btn.setToolTip("Silenzia microfono")
        self._mute_btn.setAccessibleName("Silenzia microfono")
        self._mute_btn.clicked.connect(self._toggle_mute)

        self._pause_btn = IconButton("pause", self)
        self._pause_btn.setToolTip("Pausa")
        self._pause_btn.setAccessibleName("Pausa")
        self._pause_btn.clicked.connect(self._toggle_pause)

        self._stop_btn = IconButton("stop", self)
        self._stop_btn.setToolTip("Stop e salva")
        self._stop_btn.setAccessibleName("Stop e salva")
        # Lambda: clicked(checked) must not leak into quit_after.
        self._stop_btn.clicked.connect(lambda: self._handle_stop())

        self._layout.addWidget(self._status_dot)
        self._layout.addWidget(self._level_bar)
        self._layout.addWidget(self._timer_label)
        self._layout.addStretch()
        self._layout.addWidget(self._mute_btn)
        self._layout.addWidget(self._pause_btn)
        self._layout.addWidget(self._stop_btn)

        self._set_pill_contents_visible(False)

    def _setup_animations(self) -> None:
        self._progress_anim = QPropertyAnimation(self, b"anim_progress")
        self._progress_anim.finished.connect(self._on_anim_finished)

    def _setup_timer(self) -> None:
        # Always running: drives the timer display, the VU meters AND the
        # reconciliation between recorder state and widget state (so a
        # recording that stops itself — disk full, device lost — never
        # leaves a stuck 'recording' pill).
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(100)
        self._update_timer.timeout.connect(self._tick)
        self._update_timer.start()

    def _position_on_screen(self) -> None:
        # Restore the saved position if it is still on a visible screen.
        saved = self._settings.value("widget/pos")
        if isinstance(saved, QPoint):
            for screen in QApplication.screens():
                if screen.availableGeometry().adjusted(0, 0, -CIRCLE_SIZE, -CIRCLE_SIZE).contains(saved):
                    self.move(saved)
                    return
        # Default: bottom-right of the screen where the cursor is.
        screen = _screen_for(QCursor.pos())
        if screen:
            geom = screen.availableGeometry()
            x = geom.right() - CIRCLE_SIZE - 30
            y = geom.bottom() - CIRCLE_SIZE - 80
            self.move(x, y)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == 'darwin':
            apply_macos_floating(self)
        elif (sys.platform == 'linux' and self._raise_timer is None
              and QApplication.platformName().lower().startswith('wayland')):
            # Native Wayland: WindowStaysOnTopHint is not honoured by every
            # compositor; periodically re-raise. Not on X11/XWayland, where
            # the hint works and a periodic raise would pop the widget over
            # open menus and dialogs.
            self._raise_timer = QTimer(self)
            self._raise_timer.setInterval(5000)
            self._raise_timer.timeout.connect(self._periodic_raise)
            self._raise_timer.start()

    def _periodic_raise(self) -> None:
        if QApplication.activeModalWidget() is None and QApplication.activePopupWidget() is None:
            self.raise_()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle_recording()
        elif key == Qt.Key.Key_P:
            self.toggle_pause()
        elif key == Qt.Key.Key_M:
            if self._recorder.state in (RecordingState.RECORDING, RecordingState.PAUSED):
                self._toggle_mute()
        elif key == Qt.Key.Key_Escape:
            self.clearFocus()
        else:
            super().keyPressEvent(event)

    # ---------- Painting ----------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        p = self._anim_progress
        radius = h / 2

        rect = QRectF(1, 1, w - 2, h - 2)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        if p < 1.0:
            cx = w / 2
            cy = h / 2
            glow_r = max(w, h) / 2
            glow = QRadialGradient(cx, cy, glow_r)
            hover_boost = self._hover * (1.0 - p)
            gi = int(15 * (1 - p) + 14 * hover_boost)
            glow.setColorAt(0.0, QColor(gi,
                                        int(30 * (1 - p) + 25 * hover_boost),
                                        int(10 * (1 - p) + 8 * hover_boost), 245))
            glow.setColorAt(0.7, QColor(8, 8, 10, 245))
            glow.setColorAt(1.0, QColor(int(12 * p + 5 * (1 - p)),
                                        int(12 * p + 5 * (1 - p)),
                                        int(14 * p + 7 * (1 - p)), 240))
            painter.fillPath(path, QBrush(glow))
        else:
            painter.fillPath(path, QBrush(COLOR_BG_DARK))

        # Border: brand green when idle (brighter on hover), slate as pill.
        hover_boost = self._hover * (1.0 - p)
        border_color = QColor(
            int(74 * (1 - p) + 51 * p + 25 * hover_boost),
            int(195 * (1 - p) + 65 * p + 44 * hover_boost),
            int(0 * (1 - p) + 85 * p + 26 * hover_boost),
            int(80 + 120 * p + 70 * hover_boost),
        )
        painter.setPen(QPen(border_color, 1.2 + 0.6 * hover_boost))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

        if p < 0.85:
            logo_opacity = max(0.0, 1.0 - p * 2.0)
            painter.setOpacity(logo_opacity)
            self._paint_logo(painter, w, h)
            painter.setOpacity(1.0)

        painter.end()

    def _paint_logo(self, painter: QPainter, w: float, h: float) -> None:
        """Draw the Orizon logo: the official brand glyph when the SVG
        asset is available, otherwise the procedural vortex fallback."""
        cx = w / 2
        cy = h / 2
        margin = 2

        clip = QPainterPath()
        clip.addRoundedRect(QRectF(1, 1, w - 2, h - 2), h / 2, h / 2)
        painter.setClipPath(clip)

        painter.save()
        painter.translate(cx, cy)
        scale = 1.0 + 0.05 * self._hover
        painter.scale(scale, scale)

        r = (min(w, h) - 2 * margin) / 2 - 2
        if r < 5:
            painter.restore()
            painter.setClipping(False)
            return

        renderer = _get_logo_renderer()
        if renderer is not None:
            side = r * 2 * 0.88
            renderer.render(painter, QRectF(-side / 2, -side / 2, side, side))
            painter.restore()
            painter.setClipping(False)
            return

        n_ellipses = 36
        base_angle = -35.0

        for i in range(n_ellipses):
            t = i / n_ellipses
            angle = base_angle + t * 180.0
            angle_from_base = abs(((angle - base_angle) % 180) - 90)
            opacity = 0.15 + 0.85 * (angle_from_base / 90.0) ** 0.6
            alpha = int(200 * opacity)

            pen = QPen(QColor(COLOR_GREEN.red(), COLOR_GREEN.green(),
                              COLOR_GREEN.blue(), alpha))
            pen.setWidthF(0.9)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            painter.save()
            painter.rotate(angle)
            rx = r * 0.92
            ry = r * 0.28
            painter.drawEllipse(QPointF(0, 0), rx, ry)
            painter.restore()

        for i, frac in enumerate([0.22, 0.42, 0.65]):
            radius = r * frac
            alpha = 140 - i * 25
            pen = QPen(QColor(COLOR_GREEN_LIGHT.red(), COLOR_GREEN_LIGHT.green(),
                              COLOR_GREEN_LIGHT.blue(), alpha))
            pen.setWidthF(1.1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), radius, radius)

        core_grad = QRadialGradient(0, 0, r * 0.12)
        core_grad.setColorAt(0.0, QColor(COLOR_GREEN_LIGHT.red(), COLOR_GREEN_LIGHT.green(),
                                         COLOR_GREEN_LIGHT.blue(), 180))
        core_grad.setColorAt(1.0, QColor(COLOR_GREEN_DARK.red(), COLOR_GREEN_DARK.green(),
                                         COLOR_GREEN_DARK.blue(), 0))
        painter.setBrush(QBrush(core_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(0, 0), r * 0.12, r * 0.12)

        painter.restore()
        painter.setClipping(False)

    # ---------- Mouse / hover ----------

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.start()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(0.0)
        self._hover_anim.start()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition() - QPointF(self.pos())
            self._is_dragging = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_pos is not None:
            new_pos = (event.globalPosition() - self._drag_pos).toPoint()
            if not self._is_dragging:
                diff = event.globalPosition() - (self._drag_pos + QPointF(self.pos()))
                if (diff.x() ** 2 + diff.y() ** 2) > 25:
                    self._is_dragging = True
            if self._is_dragging:
                self.move(new_pos)
                geo = self.geometry()
                self._center_anchor = QPointF(geo.center().x(), geo.center().y())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._is_dragging:
                self._settings.setValue("widget/pos", self.pos())
                self._settings.sync()  # survive os._exit / session end
            else:
                self._handle_click()
            self._drag_pos = None
            self._is_dragging = False

    def contextMenuEvent(self, event) -> None:
        """Right-click context menu with full controls."""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: #0f172a;
                color: white;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background: #334155;
            }
            QMenu::separator {
                height: 1px;
                background: #334155;
                margin: 4px 8px;
            }
        """)

        state = self._recorder.state
        if state == RecordingState.IDLE and not self._busy:
            menu.addAction("Avvia registrazione", lambda: self._start_recording())
        if state == RecordingState.RECORDING:
            menu.addAction("Pausa", self._toggle_pause)
        elif state == RecordingState.PAUSED:
            menu.addAction("Riprendi", self._toggle_pause)
        if state in (RecordingState.RECORDING, RecordingState.PAUSED):
            menu.addAction("Stop e salva", lambda: self._handle_stop())
            menu.addAction(
                "Riattiva microfono" if self._recorder.is_mic_muted else "Silenzia microfono",
                self._toggle_mute,
            )
        menu.addSeparator()
        settings_action = menu.addAction("Impostazioni…", self._open_settings)
        # Format/folder changes only make sense while nothing is recording.
        settings_action.setEnabled(state == RecordingState.IDLE and not self._busy)
        menu.addAction("Apri cartella registrazioni", self._open_recordings_folder)
        menu.addSeparator()
        menu.addAction("Chiudi Orizon Call", self._quit_requested)

        menu.exec(event.globalPos())

    def _open_settings(self) -> None:
        if self._recorder.state != RecordingState.IDLE or self._busy or self._settings_dialog_open:
            return
        from settings_dialog import open_settings
        import app_settings
        self._settings_dialog_open = True
        try:
            # The dialog runs a nested event loop: a queued API /start or a
            # tray click can start a recording meanwhile, so it only
            # persists and we apply here, after re-checking the state.
            values = open_settings(
                self, self._recorder, apply=False,
                before_exec=lambda dlg: apply_macos_floating(dlg, activate=True))
        finally:
            self._settings_dialog_open = False
        if not values:
            return
        if self._recorder.state == RecordingState.IDLE and not self._busy:
            app_settings.apply_to_recorder(self._recorder, values)
            self._pending_settings = None
            if values.get("system_audio") and not self._recorder.has_system_audio:
                Toast("Impostazioni salvate.\nAudio di sistema non disponibile su questo "
                      "computer: verrà registrato solo il microfono (vedi il log).",
                      kind="warn", duration_ms=7000).show_above(self)
            else:
                Toast("Impostazioni salvate.", kind="info").show_above(self)
        else:
            self._pending_settings = values
            Toast("Impostazioni salvate: valgono dalla prossima registrazione.",
                  kind="info").show_above(self)

    def _apply_pending_settings(self) -> None:
        if self._pending_settings is None:
            return
        import app_settings
        values, self._pending_settings = self._pending_settings, None
        try:
            app_settings.apply_to_recorder(self._recorder, values)
        except Exception:
            log.exception("Applying saved settings failed")

    def notify(self, message: str, kind: str = "warn", duration_ms: int = 6000) -> None:
        """Toast + native notification (tray) for events the user may not
        be looking at the widget for."""
        Toast(message, kind=kind, duration_ms=duration_ms).show_above(self)
        self._tray.notify("Orizon Call", message, critical=(kind == "error"))

    def closeEvent(self, event) -> None:
        """Window-manager close (Alt+F4, session logout, 'close' from a
        window list): never let it kill a recording. Route through the
        normal quit path (stop + save first, confirmation when a recording
        is running) and keep the widget alive until that has completed."""
        if self._closing:
            event.accept()
            return
        event.ignore()
        self._quit_app(confirm=True)

    @pyqtSlot()
    def shutdown(self) -> None:
        """aboutToQuit hook: stop timers, drop the tray icon and run the
        recorder's last-resort finalization (a no-op when idle)."""
        self._closing = True
        try:
            self._update_timer.stop()
            if self._raise_timer is not None:
                self._raise_timer.stop()
            self._tray.hide()
        except Exception:
            pass
        try:
            self._recorder.emergency_save()
        except Exception:
            log.exception("Emergency save at quit failed")

    # ---------- Public API (tray icon, signal handler, api_server.py) ----------

    @property
    def is_busy(self) -> bool:
        """A start/stop worker is in flight."""
        return self._busy

    @pyqtSlot()
    def request_quit(self) -> None:
        """Quit without asking: stop + save (incl. MP3/normalization), then
        exit. Used for SIGINT/SIGTERM and the REST API."""
        self._quit_app(confirm=False)

    @pyqtSlot()
    def request_quit_interactive(self) -> None:
        self._quit_app(confirm=True)

    @pyqtSlot()
    def toggle_recording(self) -> None:
        state = self._recorder.state
        if state in (RecordingState.RECORDING, RecordingState.PAUSED):
            self._handle_stop()
        elif state == RecordingState.IDLE and not self._busy:
            self._start_recording()

    @pyqtSlot()
    def toggle_pause(self) -> None:
        self._toggle_pause()

    @pyqtSlot()
    def open_settings(self) -> None:
        self._open_settings()

    @pyqtSlot()
    def open_recordings_folder(self) -> None:
        self._open_recordings_folder()

    def _ensure_on_screen(self) -> None:
        """Relocate the widget if no current screen shows it (monitor
        unplugged, resolution change). Keeps the animation anchor in sync."""
        geo = self.geometry()
        on_screen = any(s.availableGeometry().intersects(geo) for s in QApplication.screens())
        if on_screen:
            return
        screen = _screen_for(QCursor.pos())
        if screen:
            avail = screen.availableGeometry()
            self.move(avail.right() - self.width() - 30, avail.bottom() - self.height() - 80)
            self._settings.setValue("widget/pos", self.pos())
            self._settings.sync()
        geo = self.geometry()
        self._center_anchor = QPointF(geo.center().x(), geo.center().y())

    @pyqtSlot()
    def bring_to_front(self) -> None:
        """Make sure the widget is on a visible screen and on top (tray
        'Mostra il widget'). A widget left on an unplugged monitor is moved
        to the screen under the cursor."""
        self._ensure_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def wait_for_status_change(self, timeout: float = 1.0,
                               since: "int | None" = None) -> int:
        """Block until the recorder state (or mic mute) changes, or the
        timeout expires. Elapsed time and levels keep riding the timeout.
        Returns a change sequence number; pass it back as ``since`` to
        return immediately if a change already happened (no lost wake-ups)."""
        return self._recorder.wait_for_state_change(timeout, since)

    def recorder_status(self) -> dict:
        """Snapshot of recorder state — safe to call from any thread."""
        rec = self._recorder
        return {
            "state": rec.state.name.lower(),
            "elapsed": round(rec.elapsed_time, 1),
            "has_system_audio": rec.has_system_audio,
            "output_path": str(rec.output_path) if rec.output_path else None,
            "mic_level": round(rec.mic_level, 4),
            "sys_level": round(rec.sys_level, 4),
            "muted": rec.is_mic_muted,
            "dropped_chunks": rec.dropped_chunks,
            "input_overflows": rec.input_overflows,
            "segments": [str(p) for p in rec.segment_paths],
        }

    def recorder_state_name(self) -> str:
        return self._recorder.state.name.lower()

    # Slots used by the API server (must run on the GUI thread).
    @pyqtSlot()
    def api_start(self) -> None:
        self._start_recording(interactive=False)

    @pyqtSlot()
    def api_stop(self) -> None:
        self._handle_stop()

    @pyqtSlot()
    def api_pause(self) -> None:
        if self._recorder.state == RecordingState.RECORDING:
            self._toggle_pause()

    @pyqtSlot()
    def api_resume(self) -> None:
        if self._recorder.state == RecordingState.PAUSED:
            self._toggle_pause()

    @pyqtSlot()
    def api_quit(self) -> None:
        self._quit_app(confirm=False)

    @pyqtSlot()
    def api_mute(self) -> None:
        self._set_mute(True)

    @pyqtSlot()
    def api_unmute(self) -> None:
        self._set_mute(False)

    # ---------- Actions ----------

    def _handle_click(self) -> None:
        if (self._recorder.state == RecordingState.IDLE
                and self._anim_direction == 0 and not self._busy):
            self._start_recording()

    @pyqtSlot()
    def _start_recording(self, interactive: bool = True) -> None:
        """Start on a worker thread: opening streams (and the macOS helper,
        which can take seconds) must never block the GUI."""
        if self._busy or self._recorder.state != RecordingState.IDLE:
            return
        self._apply_pending_settings()
        self._busy = True
        self._start_interactive = interactive
        self.setToolTip("")

        def worker():
            try:
                path = self._recorder.start()
                self._start_done.emit(True, str(path))
            except Exception as e:
                log.error("Start failed: %s\n%s", e, traceback.format_exc())
                self._start_done.emit(False, str(e))

        threading.Thread(target=worker, daemon=True, name="start-worker").start()

    @pyqtSlot(bool, str)
    def _on_start_done(self, ok: bool, payload: str) -> None:
        self._busy = False
        if ok:
            self._ui_recording = True
            self._level_bar.reset()
            self._expand_to_pill()
            self.recording_started.emit(payload)
            if self._pending_stop or self._quit_when_done:
                # A stop or a quit (API, menu, signal) arrived while the
                # start worker ran: honor it now that the recording exists
                # — and consume the quit flag here, otherwise it would
                # fire after some later, unrelated stop.
                self._pending_stop = False
                quit_after = self._quit_when_done
                self._quit_when_done = False
                QTimer.singleShot(0, lambda: self._handle_stop(quit_after=quit_after))
        else:
            self._pending_stop = False
            self.setToolTip(_IDLE_TOOLTIP)
            if getattr(self, "_start_interactive", True):
                self._show_start_error(payload)
            else:
                # Nobody is looking at the widget (API/tray/signal start):
                # a persistent native notification, not a 3 s toast only.
                self.notify(f"Impossibile avviare la registrazione: {payload}",
                            kind="error", duration_ms=8000)
            if self._quit_when_done:
                self._quit_when_done = False
                QApplication.quit()

    def _show_start_error(self, message: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Orizon Call — Impossibile avviare la registrazione")
        box.setText("La registrazione non è stata avviata.")
        box.setInformativeText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        self._exec_dialog(box)

    @staticmethod
    def _exec_dialog(box) -> int:
        """Run a modal dialog that is visible over fullscreen apps and in
        front of the active application on macOS (accessory app)."""
        QTimer.singleShot(0, lambda: apply_macos_floating(box, activate=True))
        return box.exec()

    @pyqtSlot()
    def _toggle_pause(self) -> None:
        try:
            state = self._recorder.state
            if state == RecordingState.RECORDING:
                self._recorder.pause()
                self._pause_btn.set_icon_name("play")
                self._pause_btn.setToolTip("Riprendi")
                self._status_dot.stop_pulsing(COLOR_YELLOW)
            elif state == RecordingState.PAUSED:
                self._recorder.resume()
                self._pause_btn.set_icon_name("pause")
                self._pause_btn.setToolTip("Pausa")
                self._status_dot.start_pulsing(COLOR_RED)
        except RuntimeError as e:
            log.warning("Pause/resume rejected: %s", e)

    @pyqtSlot()
    def _handle_stop(self, quit_after: bool = False) -> None:
        """Stop on a worker thread; the pill switches to a 'saving' state
        immediately and shrinks when the file is finalized."""
        if quit_after:
            self._quit_when_done = True
        if self._busy:
            # Start or stop worker in flight. If it's a start, remember the
            # stop request (already acknowledged to the API caller); if it's
            # a stop, _quit_when_done above is all we needed to record.
            self._pending_stop = True
            return
        if self._recorder.state not in (RecordingState.RECORDING, RecordingState.PAUSED):
            if self._quit_when_done:
                self._quit_when_done = False
                QApplication.quit()
            return
        self._busy = True
        self._enter_saving_ui()

        def worker():
            try:
                path = self._recorder.stop()
                self._stop_done.emit(True, str(path) if path else "", quit_after)
            except Exception as e:
                log.error("Stop failed: %s\n%s", e, traceback.format_exc())
                self._stop_done.emit(False, str(e), quit_after)

        threading.Thread(target=worker, daemon=True, name="stop-worker").start()

    def _enter_saving_ui(self) -> None:
        self._status_dot.start_pulsing(COLOR_YELLOW)
        self._timer_label.setText("Salvataggio…")
        self._mute_btn.setVisible(False)
        self._pause_btn.setVisible(False)
        self._stop_btn.setEnabled(False)

    @pyqtSlot(bool, str, bool)
    def _on_stop_done(self, ok: bool, payload: str, quit_after: bool) -> None:
        self._busy = False
        self._ui_recording = False
        self._pending_stop = False  # already stopped; drop any queued stop
        quit_after = quit_after or self._quit_when_done
        self._quit_when_done = False
        self._stop_btn.setEnabled(True)
        self._status_dot.stop_pulsing(COLOR_WHITE_DIM)
        self._shrink_to_circle()
        self.setToolTip(_IDLE_TOOLTIP)
        # A mute must never carry over silently into the next call.
        self._set_mute(False)

        if ok and payload:
            self.recording_stopped.emit(payload)
            name = Path(payload).name
            if not quit_after:
                Toast(f"Salvato: {name}\nClicca per aprire la cartella",
                      on_click=self._open_recordings_folder).show_above(self)
        elif not ok:
            self._tray.notify("Orizon Call — errore di salvataggio", payload, critical=True)
            if quit_after and self._quit_interactive:
                # A toast would die with the process: the failure must be
                # seen before we exit.
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("Orizon Call — Errore di salvataggio")
                box.setText("Errore durante il salvataggio della registrazione.")
                box.setInformativeText(payload)
                self._exec_dialog(box)
            elif quit_after:
                # Non-interactive quit (API, SIGTERM): never block on a
                # dialog nobody will click — the log/notification carry it.
                log.error("Save failed while quitting: %s", payload)
            else:
                Toast(f"Errore durante il salvataggio: {payload}",
                      kind="error", duration_ms=6000).show_above(self)

        if quit_after:
            QApplication.quit()

    def _toggle_mute(self) -> None:
        self._set_mute(not self._recorder.is_mic_muted)

    def _set_mute(self, muted: bool) -> None:
        self._recorder.set_mic_muted(muted)
        self._mute_btn.set_icon_name("mic_off" if muted else "mic")
        self._mute_btn.set_active(muted)
        self._mute_btn.setToolTip("Riattiva microfono" if muted else "Silenzia microfono")
        self.mute_changed.emit(muted)

    def recordings_dir(self) -> Path:
        """Current destination folder (follows live settings changes and
        the recorder's fallbacks). Also used by api_server for /files, so
        the REST API always serves the same folder the app is saving into."""
        return self._recorder.output_directory

    def _open_recordings_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.recordings_dir())))

    def _quit_requested(self) -> None:
        self._quit_app(confirm=True)

    def _quit_app(self, confirm: bool = False) -> None:
        """Quit, stopping and saving any recording first (asynchronously —
        quit must not freeze the UI either)."""
        self._quit_interactive = confirm
        if self._busy or self._recorder.state == RecordingState.STOPPING:
            # A start or a save is already in flight: don't kill its worker
            # thread — exit as soon as it completes (_on_start_done turns
            # this into a stop, _on_stop_done into the actual quit).
            self._quit_when_done = True
            return
        state = self._recorder.state
        if state in (RecordingState.RECORDING, RecordingState.PAUSED):
            if confirm:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Question)
                box.setWindowTitle("Orizon Call")
                box.setText("È in corso una registrazione.")
                box.setInformativeText("Interrompere e salvare prima di uscire?")
                box.setStandardButtons(QMessageBox.StandardButton.Yes
                                       | QMessageBox.StandardButton.Cancel)
                box.button(QMessageBox.StandardButton.Yes).setText("Salva ed esci")
                box.button(QMessageBox.StandardButton.Cancel).setText("Annulla")
                if self._exec_dialog(box) != QMessageBox.StandardButton.Yes:
                    return
            # State may have changed while the dialog was open (API stop):
            # _handle_stop handles every case, including quitting directly
            # if the recorder is already idle.
            self._handle_stop(quit_after=True)
        else:
            QApplication.quit()

    # ---------- Animations ----------

    def _expand_to_pill(self) -> None:
        geo = self.geometry()
        self._center_anchor = QPointF(geo.center().x(), geo.center().y())
        self._anim_direction = 1

        self._progress_anim.stop()
        self._progress_anim.setDuration(EXPAND_DURATION)
        self._progress_anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self._progress_anim.setStartValue(0.0)
        self._progress_anim.setEndValue(1.0)
        self._progress_anim.start()

    def _shrink_to_circle(self) -> None:
        self._set_pill_contents_visible(False)
        geo = self.geometry()
        self._center_anchor = QPointF(geo.center().x(), geo.center().y())
        self._anim_direction = -1

        self._progress_anim.stop()
        self._progress_anim.setDuration(SHRINK_DURATION)
        self._progress_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._progress_anim.setStartValue(self._anim_progress)
        self._progress_anim.setEndValue(0.0)
        self._progress_anim.start()

    def _on_anim_finished(self) -> None:
        if self._anim_direction == 1:
            self.setFixedSize(PILL_WIDTH, PILL_HEIGHT)
            self._set_pill_contents_visible(True)
            # Derive the pill contents from the ACTUAL state: pause/stop
            # may have arrived while the expand animation was running.
            if self._busy:
                self._enter_saving_ui()
            elif self._recorder.state == RecordingState.PAUSED:
                self._status_dot.stop_pulsing(COLOR_YELLOW)
                self._pause_btn.set_icon_name("play")
                self._pause_btn.setToolTip("Riprendi")
            else:
                self._timer_label.setText("00:00")
                self._status_dot.start_pulsing(COLOR_RED)
                self._pause_btn.set_icon_name("pause")
                self._pause_btn.setToolTip("Pausa")
        elif self._anim_direction == -1:
            self.setFixedSize(CIRCLE_SIZE, CIRCLE_SIZE)

        self._anim_direction = 0
        self.update()

    # ---------- Periodic tick: timer, VU, state reconciliation ----------

    def _tick(self) -> None:
        state = self._recorder.state
        self._tray.set_state(state.name.lower(), self._busy)

        # Reconcile: the recorder can stop itself (disk full, devices lost,
        # emergency save). Never leave a stuck 'recording' pill behind.
        if (self._ui_recording and not self._busy
                and state == RecordingState.IDLE):
            self._ui_recording = False
            self._status_dot.stop_pulsing(COLOR_WHITE_DIM)
            self._shrink_to_circle()
            self.setToolTip(_IDLE_TOOLTIP)
            self._set_mute(False)
            path = self._recorder.output_path
            if path:
                self.recording_stopped.emit(str(path))
                Toast("Registrazione interrotta automaticamente.\n"
                      f"File salvato: {path.name}",
                      kind="warn", duration_ms=6000,
                      on_click=self._open_recordings_folder).show_above(self)
                self._tray.notify("Orizon Call — registrazione interrotta",
                                  f"File salvato: {path.name}", critical=True)
            return

        if not self._ui_recording or self._busy:
            return

        # Timer display
        elapsed = self._recorder.elapsed_time
        total_sec = int(elapsed)
        hours = total_sec // 3600
        minutes = (total_sec % 3600) // 60
        seconds = total_sec % 60
        if hours > 0:
            self._timer_label.setText(f"{hours}:{minutes:02d}:{seconds:02d}")
        else:
            self._timer_label.setText(f"{minutes:02d}:{seconds:02d}")

        # VU meters — frozen at zero while paused (no audio is captured).
        if state == RecordingState.PAUSED:
            self._level_bar.push_levels(0.0, 0.0)
        else:
            self._level_bar.push_levels(
                min(1.0, self._recorder.mic_level * 3.0),
                min(1.0, self._recorder.sys_level * 3.0),
            )

    # ---------- Helpers ----------

    def _set_pill_contents_visible(self, visible: bool) -> None:
        self._status_dot.setVisible(visible)
        self._level_bar.setVisible(visible)
        self._timer_label.setVisible(visible)
        self._mute_btn.setVisible(visible)
        self._pause_btn.setVisible(visible)
        self._stop_btn.setVisible(visible)

    def _on_error(self, message: str) -> None:
        Toast(message, kind="error", duration_ms=5000).show_above(self)
