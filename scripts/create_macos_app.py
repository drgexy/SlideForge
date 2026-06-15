#!/usr/bin/env python3
from __future__ import annotations

import os
import plistlib
import stat
import textwrap
from pathlib import Path


APP_NAME = "SlideForge Mac"
BUNDLE_ID = "com.xinyuge.slideforge-mac"


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    app_root = repo_root / "dist" / f"{APP_NAME}.app"
    macos_dir = app_root / "Contents" / "MacOS"
    resources_dir = app_root / "Contents" / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    launcher = macos_dir / APP_NAME
    launcher.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            cd "{repo_root}"
            if ! python3 -c "import slideforge" >/dev/null 2>&1; then
              python3 -m pip install -e .
            fi
            exec python3 -m slideforge.gui
            """
        ),
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": APP_NAME,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    }
    with (app_root / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)

    print(app_root)


if __name__ == "__main__":
    main()
