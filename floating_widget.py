"""
Floating pill/circle widget for the audio recorder.

Idle: dark circle with green black hole logo - click to start recording.
Recording: smooth animated expansion to pill with timer, pause, and stop buttons.
Paused: pill with yellow indicator, resume and stop buttons.
Right-click: context menu to quit the app.
"""

import math
import sys

from PyQt6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
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
    QToolTip,
    QWidget,
)

from audio_recorder import AudioRecorder, RecordingState


# ---------- Colors ----------

COLOR_GREEN = QColor(80, 255, 30)
COLOR_RED = QColor(220, 38, 38)
COLOR_RED_DARK = QColor(185, 28, 28)
COLOR_YELLOW = QColor(234, 179, 8)
COLOR_BG_DARK = QColor(12, 12, 14, 240)
COLOR_WHITE = QColor(255, 255, 255)
COLOR_WHITE_DIM = QColor(200, 200, 200)
COLOR_BTN_HOVER = QColor(63, 63, 70)
COLOR_BORDER = QColor(63, 63, 70)

# ---------- Dimensions ----------

CIRCLE_SIZE = 60
PILL_WIDTH = 290  # widened to fit dual VU meter + mute button
PILL_HEIGHT = 56
EXPAND_DURATION = 400   # ms
SHRINK_DURATION = 250   # ms


class StatusDot(QWidget):
    """Small pulsing dot indicating recording/paused status."""

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
    """Thin vertical bar showing real-time audio level (VU meter)."""

    def __init__(self, parent=None, tint: QColor = None):
        super().__init__(parent)
        self.setFixedSize(6, 32)
        self._level = 0.0
        self._base_color = tint if tint is not None else COLOR_GREEN

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Dark background
        p.setBrush(QColor(30, 30, 30))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, w, h, 3, 3)

        # Filled level (bottom to top)
        if self._level > 0.01:
            fill_h = max(2, int(h * self._level))
            if self._level < 0.75:
                color = self._base_color
            elif self._level < 0.9:
                color = COLOR_YELLOW
            else:
                color = COLOR_RED
            p.setBrush(color)
            p.drawRoundedRect(0, h - fill_h, w, fill_h, 3, 3)

        p.end()


class DualLevelBar(QWidget):
    """Two side-by-side VU meters: mic + system, with a tiny label below each."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 36)
        self._mic = LevelBar(self, tint=COLOR_GREEN)
        self._sys = LevelBar(self, tint=QColor(80, 180, 255))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._mic)
        layout.addWidget(self._sys)
        self._mic.setToolTip("Microfono (tu)")
        self._sys.setToolTip("Audio sistema (gli altri)")

    def set_mic_level(self, level: float) -> None:
        self._mic.set_level(level)

    def set_sys_level(self, level: float) -> None:
        self._sys.set_level(level)


class PillButton(QPushButton):
    """Flat button styled for the pill widget."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setFixedSize(34, 34)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {COLOR_WHITE.name()};
                border: 1px solid {COLOR_BORDER.name()};
                border-radius: 17px;
                font-size: 16px;
            }}
            QPushButton:hover {{
                background: {COLOR_BTN_HOVER.name()};
            }}
            QPushButton:pressed {{
                background: {COLOR_RED_DARK.name()};
            }}
        """)


class FloatingRecorderWidget(QWidget):
    """
    Frameless, always-on-top floating widget.
    Circle (idle) -> Pill (recording/paused) with smooth custom animation.
    """

    recording_started = pyqtSignal(str)
    recording_stopped = pyqtSignal(str)
    recording_error = pyqtSignal(str)
    mute_changed = pyqtSignal(bool)

    def __init__(self, recorder: AudioRecorder, parent=None):
        super().__init__(parent)
        self._recorder = recorder
        self._drag_pos: QPointF | None = None
        self._is_dragging = False

        # Animation state: 0.0 = circle, 1.0 = pill
        self._anim_progress = 0.0
        self._anim_direction = 0  # 0=idle, 1=expanding, -1=shrinking
        self._center_anchor = QPointF()  # Center point to expand from/shrink to

        self._setup_window()
        self._setup_pill_contents()
        self._setup_animations()
        self._setup_timer()
        self._position_on_screen()

        self._recorder.set_error_callback(lambda msg: self.recording_error.emit(msg))
        self.recording_error.connect(self._on_error)

    # ---------- Animated property ----------

    def _get_anim_progress(self) -> float:
        return self._anim_progress

    def _set_anim_progress(self, val: float) -> None:
        self._anim_progress = val
        self._update_geometry_from_progress()
        self.update()

    anim_progress = pyqtProperty(float, _get_anim_progress, _set_anim_progress)

    def _update_geometry_from_progress(self) -> None:
        """Resize and reposition the window based on animation progress."""
        p = max(0.0, min(1.0, self._anim_progress))
        w = int(CIRCLE_SIZE + p * (PILL_WIDTH - CIRCLE_SIZE))
        h = int(CIRCLE_SIZE + p * (PILL_HEIGHT - CIRCLE_SIZE))

        # Keep centered on the anchor point
        cx = self._center_anchor.x()
        cy = self._center_anchor.y()
        x = int(cx - w / 2)
        y = int(cy - h / 2)

        # Clamp to screen
        screen = QApplication.primaryScreen()
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

    def _setup_pill_contents(self) -> None:
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(12, 0, 8, 0)
        self._layout.setSpacing(5)

        self._status_dot = StatusDot(self)
        self._level_bar = DualLevelBar(self)

        self._timer_label = QLabel("00:00", self)
        self._timer_label.setStyleSheet(
            f"color: {COLOR_WHITE.name()}; font-size: 15px; font-weight: 600;"
        )
        font = QFont("SF Mono, Menlo, Consolas, monospace", 14)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._timer_label.setFont(font)

        self._mute_btn = PillButton("\U0001F3A4", self)  # \uD83C\uDFA4
        self._mute_btn.setToolTip("Silenzia microfono")
        self._mute_btn.clicked.connect(self._toggle_mute)

        self._pause_btn = PillButton("\u23F8", self)
        self._pause_btn.setToolTip("Pausa")
        self._pause_btn.clicked.connect(self._toggle_pause)

        self._stop_btn = PillButton("\u23F9", self)
        self._stop_btn.setToolTip("Stop")
        self._stop_btn.clicked.connect(self._handle_stop)

        self._layout.addWidget(self._status_dot)
        self._layout.addWidget(self._level_bar)
        self._layout.addWidget(self._timer_label)
        self._layout.addStretch()
        self._layout.addWidget(self._mute_btn)
        self._layout.addWidget(self._pause_btn)
        self._layout.addWidget(self._stop_btn)

        self._set_pill_contents_visible(False)

    def _toggle_mute(self) -> None:
        self._set_mute(not self._recorder.is_mic_muted)

    def _setup_animations(self) -> None:
        self._progress_anim = QPropertyAnimation(self, b"anim_progress")
        self._progress_anim.finished.connect(self._on_anim_finished)

    def _setup_timer(self) -> None:
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(100)
        self._update_timer.timeout.connect(self._update_time_display)

    def _position_on_screen(self) -> None:
        # Position on the screen where the cursor is (multi-monitor support)
        screen = QApplication.screenAt(QCursor.pos())
        if not screen:
            screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = geom.right() - CIRCLE_SIZE - 30
            y = geom.bottom() - CIRCLE_SIZE - 80
            self.move(x, y)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == 'darwin':
            self._set_macos_floating_level()
        elif sys.platform == 'linux':
            # Wayland workaround: periodically raise window to stay on top
            self._raise_timer = QTimer(self)
            self._raise_timer.setInterval(5000)
            self._raise_timer.timeout.connect(self.raise_)
            self._raise_timer.start()

    def _set_macos_floating_level(self) -> None:
        try:
            from AppKit import NSApplication, NSFloatingWindowLevel
            ns_app = NSApplication.sharedApplication()
            for window in ns_app.windows():
                window.setLevel_(NSFloatingWindowLevel)
                window.setCollectionBehavior_(
                    1 << 0   # CanJoinAllSpaces
                    | 1 << 4  # FullScreenAuxiliary
                )
        except Exception:
            pass

    # ---------- Painting ----------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        p = self._anim_progress
        radius = h / 2  # Fully rounded ends

        # Draw the shape: interpolated rounded rect
        rect = QRectF(1, 1, w - 2, h - 2)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        # Background: interpolate from dark-green-glow to dark-flat
        if p < 1.0:
            # Draw circle/transitional background with gradient
            cx = w / 2
            cy = h / 2
            glow_r = max(w, h) / 2
            glow = QRadialGradient(cx, cy, glow_r)
            # Fade from green-tinted center to dark as we expand
            gi = int(15 * (1 - p))
            glow.setColorAt(0.0, QColor(gi, int(30 * (1 - p)), int(10 * (1 - p)), 245))
            glow.setColorAt(0.7, QColor(8, 8, 10, 245))
            glow.setColorAt(1.0, QColor(int(12 * p + 5 * (1 - p)),
                                        int(12 * p + 5 * (1 - p)),
                                        int(14 * p + 7 * (1 - p)), 240))
            painter.fillPath(path, QBrush(glow))
        else:
            painter.fillPath(path, QBrush(COLOR_BG_DARK))

        # Border: fade from green to gray
        border_color = QColor(
            int(60 * (1 - p) + 63 * p),
            int(180 * (1 - p) + 63 * p),
            int(20 * (1 - p) + 70 * p),
            int(80 + 120 * p),
        )
        painter.setPen(QPen(border_color, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

        # Draw the logo, fading out as pill expands
        if p < 0.85:
            logo_opacity = max(0.0, 1.0 - p * 2.0)
            painter.setOpacity(logo_opacity)
            self._paint_logo(painter, w, h)
            painter.setOpacity(1.0)

        painter.end()

    def _paint_logo(self, painter: QPainter, w: float, h: float) -> None:
        """Draw the green black hole vortex logo."""
        cx = w / 2
        cy = h / 2
        margin = 2

        # Clip to current shape
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(1, 1, w - 2, h - 2), h / 2, h / 2)
        painter.setClipPath(clip)

        painter.save()
        painter.translate(cx, cy)

        r = (min(w, h) - 2 * margin) / 2 - 2
        if r < 5:
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

            pen = QPen(QColor(80, 255, 30, alpha))
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
            pen = QPen(QColor(100, 255, 50, alpha))
            pen.setWidthF(1.1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), radius, radius)

        core_grad = QRadialGradient(0, 0, r * 0.12)
        core_grad.setColorAt(0.0, QColor(120, 255, 80, 180))
        core_grad.setColorAt(1.0, QColor(60, 200, 20, 0))
        painter.setBrush(QBrush(core_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(0, 0), r * 0.12, r * 0.12)

        painter.restore()
        painter.setClipping(False)

    # ---------- Mouse Events ----------

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
                # Update anchor so animation stays relative to current pos
                geo = self.geometry()
                self._center_anchor = QPointF(geo.center().x(), geo.center().y())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._is_dragging:
                self._handle_click()
            self._drag_pos = None
            self._is_dragging = False

    def contextMenuEvent(self, event) -> None:
        """Right-click context menu with quit option."""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: #1c1c1e;
                color: white;
                border: 1px solid #3f3f46;
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background: #3f3f46;
            }
        """)

        quit_action = menu.addAction("Chiudi Orizon Call")
        action = menu.exec(event.globalPos())

        if action == quit_action:
            self._quit_app()

    # ---------- Public API (used by api_server.py) ----------

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
            "segments": [str(p) for p in rec.segment_paths],
        }

    def recorder_state_name(self) -> str:
        return self._recorder.state.name.lower()

    # Slots used by the API server (must run on the GUI thread).
    @pyqtSlot()
    def api_start(self) -> None:
        self._start_recording()

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
        self._quit_app()

    @pyqtSlot()
    def api_mute(self) -> None:
        self._set_mute(True)

    @pyqtSlot()
    def api_unmute(self) -> None:
        self._set_mute(False)

    # ---------- Actions ----------

    def _handle_click(self) -> None:
        if self._recorder.state == RecordingState.IDLE and self._anim_direction == 0:
            self._start_recording()

    @pyqtSlot()
    def _start_recording(self) -> None:
        try:
            path = self._recorder.start()
            self._expand_to_pill()
            self._update_timer.start()
            self.recording_started.emit(str(path))
        except RuntimeError as e:
            self._show_start_error(str(e))

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
        state = self._recorder.state
        if state == RecordingState.RECORDING:
            self._recorder.pause()
            self._pause_btn.setText("\u25B6")
            self._pause_btn.setToolTip("Riprendi")
            self._status_dot.stop_pulsing(COLOR_YELLOW)
        elif state == RecordingState.PAUSED:
            self._recorder.resume()
            self._pause_btn.setText("\u23F8")
            self._pause_btn.setToolTip("Pausa")
            self._status_dot.start_pulsing(COLOR_RED)

    @pyqtSlot()
    def _handle_stop(self) -> None:
        self._update_timer.stop()
        self._status_dot.stop_pulsing(COLOR_WHITE_DIM)
        path = self._recorder.stop()
        self._shrink_to_circle()
        if path:
            self.recording_stopped.emit(str(path))
            QTimer.singleShot(500, lambda: QToolTip.showText(
                self.mapToGlobal(self.rect().center()),
                f"Salvato: {path.name}",
                self, self.rect(), 3000
            ))

    def _set_mute(self, muted: bool) -> None:
        self._recorder.set_mic_muted(muted)
        # Mute button UI update (the button is created in task 16).
        if hasattr(self, '_mute_btn'):
            self._mute_btn.setText("\U0001F507" if muted else "\U0001F3A4")
            self._mute_btn.setToolTip("Riattiva microfono" if muted else "Silenzia microfono")
        self.mute_changed.emit(muted)

    @pyqtSlot()
    def _quit_app(self) -> None:
        """Quit the application, stopping any recording first."""
        if self._recorder.state != RecordingState.IDLE:
            self._update_timer.stop()
            self._recorder.stop()
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
        self._progress_anim.setStartValue(1.0)
        self._progress_anim.setEndValue(0.0)
        self._progress_anim.start()

    def _on_anim_finished(self) -> None:
        if self._anim_direction == 1:
            # Expand finished -> show pill contents
            self.setFixedSize(PILL_WIDTH, PILL_HEIGHT)
            self._set_pill_contents_visible(True)
            self._status_dot.start_pulsing(COLOR_RED)
            self._pause_btn.setText("\u23F8")
            self._pause_btn.setToolTip("Pausa")
        elif self._anim_direction == -1:
            # Shrink finished -> lock to circle
            self.setFixedSize(CIRCLE_SIZE, CIRCLE_SIZE)

        self._anim_direction = 0
        self.update()

    # ---------- Timer ----------

    def _update_time_display(self) -> None:
        elapsed = self._recorder.elapsed_time
        total_sec = int(elapsed)
        hours = total_sec // 3600
        minutes = (total_sec % 3600) // 60
        seconds = total_sec % 60
        if hours > 0:
            self._timer_label.setText(f"{hours}:{minutes:02d}:{seconds:02d}")
        else:
            self._timer_label.setText(f"{minutes:02d}:{seconds:02d}")

        # Update both VU meters (mic + system audio).
        # Normalize: RMS 0.0-0.5 -> display 0.0-1.0 (with some headroom).
        self._level_bar.set_mic_level(min(1.0, self._recorder.mic_level * 3.0))
        self._level_bar.set_sys_level(min(1.0, self._recorder.sys_level * 3.0))

    # ---------- Helpers ----------

    def _set_pill_contents_visible(self, visible: bool) -> None:
        self._status_dot.setVisible(visible)
        self._level_bar.setVisible(visible)
        self._timer_label.setVisible(visible)
        self._mute_btn.setVisible(visible)
        self._pause_btn.setVisible(visible)
        self._stop_btn.setVisible(visible)

    def _on_error(self, message: str) -> None:
        QToolTip.showText(
            self.mapToGlobal(self.rect().center()),
            message,
            self, self.rect(), 5000
        )
