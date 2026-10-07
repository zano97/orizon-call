"""Packaging guards: release notes extraction, checksums, the identities
that must never change between releases, and the self-test the release
workflow runs on every packaged build (here: from source)."""

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

import build  # noqa: E402
import release_notes  # noqa: E402
import version  # noqa: E402

CHANGELOG = """# Novità

## 1.2.0 (2026-11-01)

- Uno
- Due

## 1.1.0

- Vecchio
"""


def test_release_notes_section():
    assert release_notes.section(CHANGELOG, "v1.2.0") == "- Uno\n- Due"
    assert release_notes.section(CHANGELOG, "1.1.0") == "- Vecchio"
    assert release_notes.section(CHANGELOG, "1.3.0") == ""


def test_current_version_has_release_notes():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert release_notes.section(text, version.__version__), \
        "add a '## <version>' section to CHANGELOG.md"


def test_checksums_file(tmp_path):
    (tmp_path / "a.AppImage").write_bytes(b"abc")
    (tmp_path / "b.exe").write_bytes(b"")
    out = build.write_checksums(tmp_path)
    lines = out.read_text().splitlines()
    assert lines == [f"{hashlib.sha256(b'abc').hexdigest()}  a.AppImage",
                     f"{hashlib.sha256(b'').hexdigest()}  b.exe"]
    build.write_checksums(tmp_path)          # re-run: never lists itself
    assert "SHA256SUMS.txt" not in out.read_text()


def test_identities_never_change():
    """Changing these after a public release breaks updates (Windows sees
    a different app) or resets the macOS permissions."""
    iss = (ROOT / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    assert "AppId={{6F1C2B7E-4E0A-4C55-9D2B-0B8E5A7C3D41}" in iss
    assert "PrivilegesRequired=lowest" in iss       # silent self-update needs no UAC
    assert version.BUNDLE_ID == "eu.orizon.call"


def test_spec_is_valid_python():
    source = (ROOT / "packaging" / "orizon_call.spec").read_text(encoding="utf-8")
    compile(source, "orizon_call.spec", "exec")


def test_asset_names_match_updater():
    """build.py names files the way updater.asset_suffix looks for them."""
    import updater
    src = (ROOT / "packaging" / "build.py").read_text(encoding="utf-8")
    assert '-macos-{arch}' in src and 'f"{stem}.zip"' in src
    assert updater.asset_suffix("windows-installer") in (
        f"OrizonCall-{version.__version__}-windows-x64-setup" + ".exe")
    assert '-linux-{arch}.AppImage' in src


@pytest.mark.timeout(240)
def test_self_test_passes_from_source(tmp_path):
    import self_test
    report = tmp_path / "report.txt"
    assert self_test.run(str(report)) == 0, report.read_text()
    assert "RESULT: OK" in report.read_text()
