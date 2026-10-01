"""Where browser windows go on screen.

`default`: the main window is maximized, workers cascade over it.
`split`: the left part of the screen is left free for the terminal; the main
window takes the right part and workers tile over that same area, so a screen
recording shows the terminal and every browser at once.
"""

from __future__ import annotations

import sys

TERMINAL_SHARE = 0.4
# Title bar, tabs and address bar: the page gets the window minus this.
BROWSER_CHROME_PX = 88

Rect = tuple[int, int, int, int]


def screen_size() -> tuple[int, int]:
    """Usable desktop size in the units Chrome's window flags use."""
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            # SM_CXFULLSCREEN / SM_CYFULLSCREEN: the area a maximized window gets,
            # i.e. without the taskbar.
            w, h = user32.GetSystemMetrics(16), user32.GetSystemMetrics(17)
            if w > 0 and h > 0:
                return w, h
        except Exception:  # noqa: BLE001
            pass
    return 1920, 1040


def main_window_rect(layout: str) -> Rect | None:
    if layout != "split":
        return None
    sw, sh = screen_size()
    left = int(sw * TERMINAL_SHARE)
    return left, 0, sw - left, sh


def worker_window_rect(layout: str, slot: int, total: int) -> Rect:
    if layout != "split":
        return 60 + slot * 360, 80 + slot * 70, 1100, 760
    left, top, area_w, area_h = main_window_rect(layout)  # type: ignore[misc]
    if total <= 1:
        return left, top, area_w, area_h
    rows = (total + 1) // 2
    w, h = area_w // 2, area_h // rows
    col, row = slot % 2, slot // 2
    if total % 2 and slot == total - 1:
        w = area_w
    return left + col * (area_w // 2), top + row * h, w, h


def window_args(rect: Rect | None) -> list[str]:
    if rect is None:
        return ["--start-maximized"]
    x, y, w, h = rect
    return [f"--window-position={x},{y}", f"--window-size={w},{h}"]


def viewport_for(rect: Rect | None, fallback: tuple[int, int]) -> dict[str, int]:
    """Page size that fits the window, so a recording matches what is on screen."""
    if rect is None:
        return {"width": fallback[0], "height": fallback[1]}
    _x, _y, w, h = rect
    return {"width": max(400, w - 16), "height": max(300, h - BROWSER_CHROME_PX)}
