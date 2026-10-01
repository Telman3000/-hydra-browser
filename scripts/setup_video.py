#!/usr/bin/env python3
"""Give Playwright an ffmpeg for --record-video without its CDN.

`playwright install ffmpeg` downloads a custom build that is often unreachable.
Any ffmpeg with libvpx works: this copies the one on PATH to where Playwright
looks for it (`.pw-browsers/`, which hydra.config points Playwright at).

    python scripts/setup_video.py
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path

import playwright

ROOT = Path(__file__).resolve().parents[1]
TARGET_NAME = {
    "Windows": "ffmpeg-win64.exe",
    "Darwin": "ffmpeg-mac",
    "Linux": "ffmpeg-linux",
}


def main() -> int:
    source = shutil.which("ffmpeg")
    if not source:
        print("ffmpeg is not on PATH - install it first (winget install Gyan.FFmpeg / brew / apt)")
        return 2
    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
    revision = next(b["revision"] for b in browsers if b["name"] == "ffmpeg")
    name = TARGET_NAME.get(platform.system())
    if not name:
        print(f"unsupported platform {platform.system()}")
        return 2
    target = ROOT / ".pw-browsers" / f"ffmpeg-{revision}" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    print(f"copied {source} -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
