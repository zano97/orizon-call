"""
Floating pill/circle widget for the audio recorder.

Idle: dark circle with green black hole logo — click to start recording.
Recording: smooth animated expansion to pill with timer, VU meters,
mute, pause and stop buttons.
Paused: pill with yellow indicator, resume and stop buttons.
Saving: pill shows a "Salvataggio…" state while a worker thread
finalizes the file — the GUI thread is never blocked.
Right-click: context menu (start/stop, pause, mute, open folder, quit).

All recorder calls that can block (start/stop) run on worker threads and
report back via queued signals, so the widget stays responsive and
animations never stutter.
"""

import sys
import threading
import traceback
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
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

_LOGO_SVG_PATH = Path(__file__).resolve().parent / "assets" / "orizon-icon.svg"
_logo_renderer = None  # lazy singleton; False = tried and failed


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
        center = anchor.mapToGlobal(anchor.rect().center())
        x = center.x() - self.width() // 2
        y = anchor.mapToGlobal(anchor.rect().topLeft()).y() - self.height() - 10
        screen = _screen_for(center)
        if screen:
            avail = screen.availableGeometry()
            x = max(avail.left() + 6, min(x, avail.right() - self.width() - 6))
            if y < avail.top() + 6:  # no room above -> below
                y = anchor.mapToGlobal(anchor.rect().bottomLeft()).y() + 10
        self.move(x, y)
        self.setWindowOpacity(0.0)
        self.show()
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
        self.setToolTip("Clicca per registrare — trascina per spostare")

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
        self._mute_btn.clicked.connect(self._toggle_mute)

        self._pause_btn = IconButton("pause", self)
        self._pause_btn.setToolTip("Pausa")
        self._pause_btn.clicked.connect(self._toggle_pause)

        self._stop_btn = IconButton("stop", self)
        self._stop_btn.setToolTip("Stop e salva")
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
            self._set_macos_floating_level()
        elif sys.platform == 'linux' and self._raise_timer is None:
            # Wayland workaround: periodically raise window to stay on top.
            self._raise_timer = QTimer(self)
            self._raise_timer.setInterval(5000)
            self._raise_timer.timeout.connect(self.raise_)
            self._raise_timer.start()

    def _set_macos_floating_level(self) -> None:
        try:
            from AppKit import NSApplication, NSFloatingWindowLevel
            # NSWindowCollectionBehavior: CanJoinAllSpaces = 1 << 0,
            # FullScreenAuxiliary = 1 << 8 (NOT 1 << 4, which is
            # 'Stationary' and would drop the widget from fullscreen apps).
            behavior = (1 << 0) | (1 << 8)
            ns_app = NSApplication.sharedApplication()
            for window in ns_app.windows():
                window.setLevel_(NSFloatingWindowLevel)
                window.setCollectionBehavior_(behavior)
        except Exception:
            pass

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
        menu.addAction("Apri cartella registrazioni", self._open_recordings_folder)
        menu.addSeparator()
        menu.addAction("Chiudi Orizon Call", self._quit_requested)

        menu.exec(event.globalPos())

    # ---------- Public API (used by api_server.py) ----------

    def wait_for_status_change(self, timeout: float = 1.0) -> None:
        """Block until the recorder state (or mic mute) changes, or the
        timeout expires. Elapsed time and levels keep riding the timeout."""
        self._recorder.wait_for_state_change(timeout)

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
            if self._pending_stop:
                # A stop (e.g. via API) arrived while the start worker ran:
                # honor it now that the recording actually exists.
                self._pending_stop = False
                QTimer.singleShot(0, lambda: self._handle_stop())
        else:
            self._pending_stop = False
            self.setToolTip("Clicca per registrare — trascina per spostare")
            if getattr(self, "_start_interactive", True):
                self._show_start_error(payload)
            else:
                Toast(f"Impossibile avviare la registrazione: {payload}",
                      kind="error").show_above(self)
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
        box.exec()

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
        self.setToolTip("Clicca per registrare — trascina per spostare")

        if ok and payload:
            self.recording_stopped.emit(payload)
            from pathlib import Path as _P
            name = _P(payload).name
            if not quit_after:
                Toast(f"Salvato: {name}\nClicca per aprire la cartella",
                      on_click=self._open_recordings_folder).show_above(self)
        elif not ok:
            if quit_after:
                # A toast would die with the process: the failure must be
                # seen before we exit.
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("Orizon Call — Errore di salvataggio")
                box.setText("Errore durante il salvataggio della registrazione.")
                box.setInformativeText(payload)
                box.exec()
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

    def _open_recordings_folder(self) -> None:
        from pathlib import Path as _P
        rec_dir = self._recorder._output_dir or (_P.home() / "Downloads")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(rec_dir)))

    def _quit_requested(self) -> None:
        self._quit_app(confirm=True)

    def _quit_app(self, confirm: bool = False) -> None:
        """Quit, stopping and saving any recording first (asynchronously —
        quit must not freeze the UI either)."""
        if self._busy or self._recorder.state == RecordingState.STOPPING:
            # A save is already in flight: don't kill its worker thread —
            # exit as soon as it completes.
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
                if box.exec() != QMessageBox.StandardButton.Yes:
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

        # Reconcile: the recorder can stop itself (disk full, devices lost,
        # emergency save). Never leave a stuck 'recording' pill behind.
        if (self._ui_recording and not self._busy
                and state == RecordingState.IDLE):
            self._ui_recording = False
            self._status_dot.stop_pulsing(COLOR_WHITE_DIM)
            self._shrink_to_circle()
            self.setToolTip("Clicca per registrare — trascina per spostare")
            path = self._recorder.output_path
            if path:
                self.recording_stopped.emit(str(path))
                Toast("Registrazione interrotta automaticamente.\n"
                      f"File salvato: {path.name}",
                      kind="warn", duration_ms=6000,
                      on_click=self._open_recordings_folder).show_above(self)
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
