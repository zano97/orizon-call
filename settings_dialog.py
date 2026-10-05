"""
Settings dialog for Orizon Call.

Opened from the widget's right-click menu while idle. Lets the user pick
the output format (WAV / FLAC / MP3), the destination folder, loudness
normalization, dual-track layout, auto-balance, system-audio capture,
hiding the widget from screen sharing, automatic call detection and
in-app update checks —
without touching the terminal. Values persist via app_settings and are
applied to the recorder immediately on save (they take effect from the
next recording).
"""

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

import app_settings
import screen_privacy
import updater

# Orizon design tokens (kept in sync with floating_widget.py)
_BG = "#0f172a"        # gray.900
_FIELD = "#1e293b"     # gray.800
_BORDER = "#334155"    # gray.700
_TEXT = "#f8fafc"      # gray.50
_TEXT_DIM = "#94a3b8"  # gray.400
_BRAND = "#6bef1a"     # brand.400
_BRAND_DARK = "#4ac300"  # brand.500

_STYLE = f"""
QDialog {{
    background: {_BG};
}}
QLabel {{
    color: {_TEXT};
    background: transparent;
}}
QLabel[hint="true"] {{
    color: {_TEXT_DIM};
    font-size: 11px;
}}
QLabel[section="true"] {{
    color: {_BRAND};
    font-weight: 600;
    padding-top: 6px;
}}
QCheckBox {{
    color: {_TEXT};
    background: transparent;
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {_BORDER};
    border-radius: 4px;
    background: {_FIELD};
}}
QCheckBox::indicator:checked {{
    background: {_BRAND};
    border-color: {_BRAND_DARK};
}}
QCheckBox:disabled {{ color: {_TEXT_DIM}; }}
QCheckBox::indicator:disabled {{
    background: {_FIELD};
    border-color: {_BORDER};
}}
QComboBox, QLineEdit {{
    color: {_TEXT};
    background: {_FIELD};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    min-width: 240px;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    color: {_TEXT};
    background: {_FIELD};
    border: 1px solid {_BORDER};
    selection-background-color: {_BORDER};
}}
QPushButton {{
    color: {_TEXT};
    background: {_FIELD};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ background: {_BORDER}; }}
QPushButton[accent="true"] {{
    color: #08110a;
    background: {_BRAND};
    border: 1px solid {_BRAND_DARK};
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background: {_BRAND_DARK}; color: {_TEXT}; }}
"""

_FORMATS = [
    ("wav", "WAV — qualità piena (predefinito)"),
    ("flac", "FLAC — compresso senza perdite"),
    ("mp3", "MP3 — file leggero, ideale da condividere"),
]

_CALL_MODES = [
    ("propose", "Proponi di registrarla (predefinito)"),
    ("auto", "Avvia subito la registrazione"),
    ("off", "Non fare nulla"),
]

_LUFS = [
    (-16.0, "-16 LUFS — voce / podcast"),
    (-14.0, "-14 LUFS — streaming"),
]


class SettingsDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Impostazioni — Orizon Call")
        self.setModal(True)
        self.setStyleSheet(_STYLE)

        s = app_settings.load_settings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        # Formato di salvataggio
        self._format = QComboBox(self)
        for value, label in _FORMATS:
            self._format.addItem(label, value)
        self._select_data(self._format, s["output_format"])
        form.addRow("Formato audio:", self._format)

        # Cartella di destinazione
        self._dir_edit = QLineEdit(self)
        self._dir_edit.setReadOnly(True)
        self._dir_edit.setText(s["output_dir"])
        self._dir_edit.setPlaceholderText(str(app_settings.default_downloads_dir()) + "  (predefinita)")
        browse = QPushButton("Sfoglia…", self)
        browse.clicked.connect(self._pick_dir)
        reset = QPushButton("Predefinita", self)
        reset.clicked.connect(lambda: self._dir_edit.setText(""))
        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        dir_row.addWidget(self._dir_edit, 1)
        dir_row.addWidget(browse)
        dir_row.addWidget(reset)
        form.addRow("Salva in:", dir_row)

        layout.addLayout(form)

        # Normalizzazione volume
        self._normalize = QCheckBox("Normalizza il volume a fine registrazione", self)
        self._normalize.setChecked(s["normalize"])
        self._lufs = QComboBox(self)
        for value, label in _LUFS:
            self._lufs.addItem(label, value)
        self._select_data(self._lufs, s["normalize_lufs"])
        self._lufs.setEnabled(s["normalize"])
        self._normalize.toggled.connect(self._lufs.setEnabled)
        norm_row = QHBoxLayout()
        norm_row.setSpacing(10)
        norm_row.addWidget(self._normalize)
        norm_row.addWidget(self._lufs, 1)
        layout.addLayout(norm_row)

        # Altre opzioni
        self._system_audio = QCheckBox(
            "Registra anche l'audio di sistema (la voce degli altri)", self)
        self._system_audio.setChecked(s["system_audio"])
        layout.addWidget(self._system_audio)

        self._auto_balance = QCheckBox(
            "Bilancia automaticamente i volumi di microfono e sistema", self)
        self._auto_balance.setChecked(s["auto_balance"])
        layout.addWidget(self._auto_balance)

        self._dual_track = QCheckBox(
            "Traccia doppia: sinistra = microfono, destra = sistema (per trascrizione)", self)
        self._dual_track.setChecked(s["dual_track"])
        layout.addWidget(self._dual_track)

        hint = QLabel("Le modifiche valgono dalla prossima registrazione.", self)
        hint.setProperty("hint", True)
        layout.addWidget(hint)

        # Privacy e call
        section = QLabel("Privacy e call", self)
        section.setProperty("section", True)
        layout.addWidget(section)

        self._hide_share = QCheckBox(
            "Nascondi il widget a chi vede il mio schermo (condivisione e registrazioni schermo)", self)
        self._hide_share.setChecked(s["hide_from_screen_share"])
        layout.addWidget(self._hide_share)
        if not screen_privacy.is_supported():
            self._hide_share.setEnabled(False)
            share_hint = QLabel(screen_privacy.unsupported_reason(), self)
            share_hint.setProperty("hint", True)
            share_hint.setWordWrap(True)
            layout.addWidget(share_hint)

        call_form = QFormLayout()
        call_form.setSpacing(10)
        self._call_mode = QComboBox(self)
        for value, label in _CALL_MODES:
            self._call_mode.addItem(label, value)
        self._select_data(self._call_mode, s["call_detection"])
        call_form.addRow("Quando inizia una call:", self._call_mode)
        layout.addLayout(call_form)

        self._ignored = list(s["call_detection_ignored"])
        self._reset_ignored = QPushButton(self)
        self._reset_ignored.clicked.connect(self._clear_ignored)
        self._refresh_ignored_button()
        ignored_row = QHBoxLayout()
        ignored_row.addWidget(self._reset_ignored)
        ignored_row.addStretch()
        layout.addLayout(ignored_row)

        call_hint = QLabel("La call viene riconosciuta quando un'altra app (Meet, Zoom, Teams…) "
                           "usa il microfono: la proposta compare in circa 2 secondi.", self)
        call_hint.setProperty("hint", True)
        call_hint.setWordWrap(True)
        layout.addWidget(call_hint)

        # Aggiornamenti
        updates = QLabel("Aggiornamenti", self)
        updates.setProperty("section", True)
        layout.addWidget(updates)
        self._auto_update = QCheckBox(
            "Controlla gli aggiornamenti e proponi di installarli", self)
        self._auto_update.setChecked(s["auto_update_check"])
        layout.addWidget(self._auto_update)
        if not updater.is_managed_install():
            update_hint = QLabel("Questa copia non è stata installata con l'installer "
                                 "(cartella di sviluppo): si aggiorna con 'git pull'.", self)
            update_hint.setProperty("hint", True)
            update_hint.setWordWrap(True)
            layout.addWidget(update_hint)

        # Pulsanti
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Annulla", self)
        cancel.clicked.connect(self.reject)
        save = QPushButton("Salva", self)
        save.setProperty("accent", True)
        save.setDefault(True)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    # ---------- helpers ----------

    @staticmethod
    def _select_data(combo: QComboBox, value) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    def _refresh_ignored_button(self) -> None:
        if self._ignored:
            self._reset_ignored.setText(
                f"Ripristina app ignorate ({', '.join(self._ignored)})")
            self._reset_ignored.setVisible(True)
        else:
            self._reset_ignored.setVisible(False)

    def _clear_ignored(self) -> None:
        self._ignored = []
        self._refresh_ignored_button()

    def _pick_dir(self) -> None:
        start = self._dir_edit.text() or str(app_settings.default_downloads_dir())
        chosen = QFileDialog.getExistingDirectory(
            self, "Scegli la cartella delle registrazioni", start)
        if chosen:
            self._dir_edit.setText(chosen)

    # ---------- values ----------

    def values(self) -> dict:
        return {
            "output_format": self._format.currentData(),
            "output_dir": self._dir_edit.text().strip(),
            "dual_track": self._dual_track.isChecked(),
            "auto_balance": self._auto_balance.isChecked(),
            "normalize": self._normalize.isChecked(),
            "normalize_lufs": self._lufs.currentData(),
            "system_audio": self._system_audio.isChecked(),
            "hide_from_screen_share": self._hide_share.isChecked(),
            "call_detection": self._call_mode.currentData(),
            "call_detection_ignored": list(self._ignored),
            "auto_update_check": self._auto_update.isChecked(),
        }

    def save(self) -> dict:
        """Persist the dialog values and return them."""
        values = self.values()
        app_settings.save_settings(values)
        return values


def open_settings(parent, recorder, apply: bool = True, before_exec=None):
    """Show the dialog; on save, persist (and, with ``apply``, push the
    values into the recorder). ``before_exec(dialog)`` runs just before the
    modal loop (platform tweaks such as the macOS floating level).
    Returns the saved values dict, or None if the user cancelled."""
    dialog = SettingsDialog(parent)
    if before_exec is not None:
        try:
            before_exec(dialog)
        except Exception:
            pass
    if dialog.exec() == QDialog.DialogCode.Accepted:
        values = dialog.save()
        if apply:
            app_settings.apply_to_recorder(recorder, values)
        return values
    return None
