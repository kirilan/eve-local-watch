#!/usr/bin/env python3
"""Is EVE drawing new frames right now? Compares two back-to-back captures
(faster than EVE draws) with one taken 5 s later.

Run: uv run tools/frame_check.py"""
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import eve_local_watch as w  # noqa: E402

win = w.find_window("")
if not win:
    sys.exit("No EVE window found.")
if w.WINDOWS:
    print(f"{win['title']}: minimised={bool(w._win32.u.IsIconic(win['id']))}")
else:
    active = json.loads(subprocess.run(["hyprctl", "activeworkspace", "-j"],
                                       capture_output=True, text=True).stdout)["id"]
    print(f"{win['title']}: workspace {win['workspace']['name']}, you are on {active}, "
          f"hidden={win.get('hidden')}, minimised/special={str(win['workspace']['name']).startswith('special')}")


def grab():
    img = w.capture(win)
    return None if img is None else img.copy()


def changed(x, y):
    return (np.abs(x.astype(int) - y.astype(int)).sum(axis=2) > 0).mean() * 100


a, b = grab(), grab()
time.sleep(5)
c = grab()
if a is None or c is None:
    sys.exit("Capture FAILED (could not copy the window).")
print(f"back-to-back: identical={a.tobytes() == b.tobytes()}, pixels changed {changed(a, b):.2f}%")
print(f"5 s apart:    identical={a.tobytes() == c.tobytes()}, pixels changed {changed(a, c):.2f}%")
print("=> EVE is DRAWING new frames" if a.tobytes() != c.tobytes() else "=> picture is FROZEN (EVE not drawing)")
