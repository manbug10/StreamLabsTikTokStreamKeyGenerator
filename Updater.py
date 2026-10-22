from __future__ import annotations

import traceback

import requests
from packaging import version

try:
    from _version import __version__
except ImportError:
    # Running from source: _version.py is generated only during CI builds.
    # Fall back to 0.0.0 so the app does not crash with ModuleNotFoundError.
    __version__ = "0.0.0"


class VersionChecker:
    REPO = "manbug10/StreamLabsTikTokStreamKeyGenerator"

    @classmethod
    def check_update(cls):
        try:
            response = requests.get(
                f"https://api.github.com/repos/{cls.REPO}/releases/latest",
                timeout=10
            )
            release = response.json()
            latest = str(release.get("tag_name", "")).removeprefix("v")

            if not latest:
                return None

            if version.parse(latest) > version.parse(__version__):
                return {
                    "current": __version__,
                    "latest": latest,
                    "url": release.get("html_url", f"https://github.com/{cls.REPO}/releases/latest"),
                    "notes": release.get("body", "")
                }
        except Exception:
            traceback.print_exc()
            return None
