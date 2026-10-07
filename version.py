"""
Single source of truth for the app version and release coordinates.

Release flow (see RELEASING.md): bump ``__version__``, add the section to
CHANGELOG.md, push the tag ``v<version>``. The release workflow refuses a
tag that does not match this file, builds the three platforms and
publishes them on ``RELEASES_REPO``; installed apps compare their
``__version__`` with the latest release there.
"""

__version__ = "1.0.0"

APP_NAME = "Orizon Call"
# macOS bundle id / Windows AppUserModelID. Never change it after the first
# public release: macOS ties the Microphone and Screen Recording
# permissions to it, Windows the installed-app identity.
BUNDLE_ID = "eu.orizon.call"

# GitHub repository whose Releases carry the installers. It must be
# readable by the people who install the app: a public repository (it may
# contain only the releases) gives updates without any GitHub account.
RELEASES_REPO = "zano97/orizon-call"


def parse_version(text: str) -> tuple:
    """'v1.10.2' → (1, 10, 2). Pre-release suffixes ('1.2.0-rc1') sort
    before the final release. Unparseable → (0,)."""
    text = (text or "").strip().lstrip("vV")
    core, _, suffix = text.partition("-")
    parts = []
    for p in core.split("."):
        if not p.isdigit():
            return (0,)
        parts.append(int(p))
    while len(parts) < 3:
        parts.append(0)
    # Final release > any pre-release of the same version.
    parts.append(0 if suffix else 1)
    return tuple(parts)
