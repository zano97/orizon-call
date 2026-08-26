"""Test fixtures: insert the project root on sys.path so `import audio_recorder` works."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# GUI tests (settings dialog) need a real QApplication without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
