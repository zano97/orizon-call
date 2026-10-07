#!/usr/bin/env python3
"""
Print the CHANGELOG.md section of a version (the release description).

    python packaging/release_notes.py 1.2.0 > notes.md
    python packaging/release_notes.py --check v1.2.0   # tag vs version.py + section exists

Exits non-zero when the section is missing or empty, so a release can
never go out without notes for its users.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from version import __version__  # noqa: E402


def section(changelog: str, version: str) -> str:
    version = version.lstrip("vV")
    pattern = re.compile(rf"^##\s+v?{re.escape(version)}\s*(?:\(.*\))?\s*$", re.M)
    match = pattern.search(changelog)
    if not match:
        return ""
    rest = changelog[match.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    return (rest[: nxt.start()] if nxt else rest).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", nargs="?", default=__version__)
    parser.add_argument("--check", action="store_true",
                        help="Also require the version (or tag) to match version.py.")
    args = parser.parse_args()
    wanted = args.version.lstrip("vV")
    if args.check and wanted != __version__:
        print(f"Tag v{wanted} does not match version.py ({__version__}).", file=sys.stderr)
        return 1
    text = section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), wanted)
    if not text:
        print(f"CHANGELOG.md has no '## {wanted}' section with content.", file=sys.stderr)
        return 1
    if not args.check:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
