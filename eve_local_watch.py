#!/usr/bin/env python3
"""Watch the EVE Online Local member list and alert on hostiles / neutrals.

Captures only the EVE window (Linux/Hyprland: grim -T, works on hidden
workspaces; Windows: PrintWindow, works while covered). On start it finds the
"Local" tab, measures the chat panel from its border lines and watches the
member list. Pilot rows are found by name text, and the standing icon in each
row is classified by hue. Read-only: never touches the game client.

Usage:
  eve_local_watch.py run            find Local, then watch and alert (default)
  eve_local_watch.py test-alerts    play every alert sound + notification
  eve_local_watch.py debug [PNG]    locate + analyse one frame (or a saved
                                    window PNG), write an annotated image
  eve_local_watch.py intel          show the chat log folder, your system and
                                    how recent intel messages are read

Options (any command):
  -c, --character NAME              watch the "EVE - NAME" window (overrides
                                    `character` in the config file)
  -j, --join-sound                  soft sound when anyone (incl. blues) joins
                                    Local (off by default; same as `alert_joins`)
"""
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import cv2
import numpy as np
from PIL import Image, ImageDraw

cv2.setNumThreads(1)          # images are tiny; worker threads only add overhead

WINDOWS = sys.platform == "win32"
HERE = Path(__file__).resolve().parent
TAB_TEMPLATE = HERE / "templates" / "local_tab.png"
MAP_PATH = HERE / "data" / "eve_map.json"
if WINDOWS:
    CONFIG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "eve-local-watch" / "config.json"
    SOUNDS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Media"
    SOUND_FILES = {"hostile": "Alarm01.wav", "join": "Windows Notify System Generic.wav",
                   "lost": "Windows Critical Stop.wav", "intel": "Windows Exclamation.wav"}
else:
    CONFIG_PATH = Path.home() / ".config" / "eve-local-watch" / "config.json"
    SOUNDS = Path("/usr/share/sounds/freedesktop/stereo")
    SOUND_FILES = {"hostile": "alarm-clock-elapsed.oga", "join": "message-new-instant.oga",
                   "lost": "service-logout.oga", "intel": "dialog-warning.oga"}
FRAME_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "eve-local-watch.ppm"

DEFAULTS = {
    "character": "",            # "" = first "EVE - *" window
    "interval": 0.5,            # seconds between captures
    "vote_seconds": 2.0,        # a reading must dominate this much recent history...
    "vote_share": 0.6,          # ...by at least this share of frames before we act on it
    "self_rows": 1,             # rows with no icon that are you / your alts
    "alert_neutrals": True,
    "alert_joins": False,       # soft sound when anyone (incl. blues) joins (-j turns it on)
    "repeat_seconds": 30,       # re-alarm while hostiles/neutrals remain / Local is lost
    "lost_seconds": 5,          # grace before "Local lost" (covers jump reloads)
    "stale_seconds": 10,        # warn if EVE hasn't drawn a new frame for this long
    "heartbeat_seconds": 120,   # print a "still watching" status line this often (0 = off)
    "sound_hostile": str(SOUNDS / SOUND_FILES["hostile"]),
    "sound_join": str(SOUNDS / SOUND_FILES["join"]),
    "sound_lost": str(SOUNDS / SOUND_FILES["lost"]),
    "intel_channels": [],       # chat channels to read intel from, e.g. ["wc.Vale+Tr+Ge"]
                                # ([] = auto-detect, false = off)
    "intel_jumps": 5,           # alert on hostiles reported this many jumps away or closer
    "chatlog_dir": "",          # "" = find EVE's Chatlogs folder automatically
    "jump_bridges": [],         # extra connections, e.g. [["4-HWWF", "P3EN-E"]] (Ansiblex)
    "sound_intel": str(SOUNDS / SOUND_FILES["intel"]),
    "snapshot_alerts": True,    # save the frame behind every Local alarm (to check false alarms)
}

# Standing icon hue ranges in degrees -> category.
HUE_CLASSES = [
    (0, 15, "hostile"),      # red   (-10, war target, criminal)
    (15, 45, "hostile"),     # orange (-5)
    (45, 70, "flagged"),     # yellow (suspect / bounty style flags)
    (70, 170, "friendly"),   # green (corp)
    (170, 255, "friendly"),  # blues (+5 / +10 / alliance)
    (255, 340, "friendly"),  # purple (fleet)
    (340, 361, "hostile"),   # red wrap-around
]

# Colour families for finding icon blobs (degrees; first one wraps through 0).
ICON_HUE_BINS = [(340, 45), (45, 70), (70, 170), (170, 255), (255, 340)]

LINE_DELTA = 25              # a 1px UI border is this much brighter (R+G+B) than its sides
BLINK_HOLD = 2.5             # seconds a blinking (war target) icon counts after it was last lit


MUTED = threading.Event()  # set by the TUI: no sounds (toasts still show)
_intel_sound_at = 0.0      # shared, so several watched clients make one sound per intel burst


class Ctx:
    """What a cmd_run publishes for a UI and takes orders from: one per watched client."""

    def __init__(self):
        self.stop = threading.Event()
        self.snap = threading.Event()               # set by a UI: save a snapshot of the next frame
        self.live = {}                              # kind (off/ok/alert), text, rows, system, reason
        self.events = collections.deque(maxlen=300)  # alert / local / intel dicts, newest last
        self.n = 0                                  # events ever added (UIs redraw when it changes)

    def event(self, kind, **data):
        self.events.append({"t": time.time(), "kind": kind, **data})
        self.n += 1
CONFIRM_THREAT_S = 3.0   # a new hostile/neutral must hold this long before alarming


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def save_config(**changes):
    """Merge `changes` into the config file, keeping its other keys."""
    cur = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    cur.update(changes)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cur, indent=2), encoding="utf-8")


def bold(text):
    """Bold on a terminal (not when piped to a file, or with NO_COLOR set)."""
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        return f"\033[1m{text}\033[0m"
    return text


def ts():
    return time.strftime("%H:%M:%S")


# --- capture -----------------------------------------------------------------
# A window is a dict with at least "title" and "id". find_window() returns the
# EVE window to watch; capture() returns its pixels as an RGB (h, w, 3) array.

def list_eve_windows():
    if WINDOWS:
        return _win32.list_windows()
    out = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout
    return [dict(c, id=c["stableId"]) for c in json.loads(out or "[]")]


def find_window(character="", current=None):
    """The EVE window to watch. Sticks with `current` while it still exists:
    Windows lists windows front to back, so "the first one" would otherwise
    change every time you click between clients."""
    wins = [c for c in list_eve_windows() if c.get("title", "").startswith("EVE - ")
            and (not character or c["title"] == f"EVE - {character}")]
    if current:
        same = [c for c in wins if c["id"] == current["id"]]
        if same:
            return dict(current, **same[0])      # keep per-window state (e.g. last frame)
    return wins[0] if wins else None


def capture(win):
    if WINDOWS:
        return _win32.capture(win)
    # grim writes to a file in RAM ($XDG_RUNTIME_DIR is tmpfs): cheaper than a pipe.
    r = subprocess.run(["grim", "-T", str(win["id"]), "-t", "ppm", str(FRAME_PATH)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return None
    data = FRAME_PATH.read_bytes()
    # Wrap the raw PPM pixels as an array without decoding/copying the whole
    # ~12 MB frame; only the small slices we look at get converted later.
    magic, w, h, maxval = data[:32].split(maxsplit=4)[:4]
    offset = len(b" ".join([magic, w, h, maxval])) + 1   # header fields + one whitespace each
    w, h = int(w), int(h)
    return np.frombuffer(data, np.uint8, count=w * h * 3, offset=offset).reshape(h, w, 3)


class _Win32:
    """Windows window list + capture through the Win32 API (ctypes, no extra deps).

    PrintWindow with PW_RENDERFULLCONTENT asks the window to draw itself into
    our bitmap, so it works while EVE is covered by other windows (not while
    it's minimised: then nothing is drawn, and we hand back the last frame so
    the frozen-picture warning fires, as on Linux)."""

    PW_CLIENTONLY, PW_RENDERFULLCONTENT = 1, 2

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt
        self.ct = ctypes
        u, g = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32")
        # Physical pixels, not DPI-scaled ones, or a scaled desktop gets a cropped capture.
        try:
            u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))     # per-monitor v2
        except (AttributeError, OSError):
            try:
                ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                pass
        # Handles are pointer-sized: without explicit types ctypes truncates them to 32 bits.
        H = wt.HANDLE
        self.EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        for name, res, args in [
            ("EnumWindows", wt.BOOL, [self.EnumProc, wt.LPARAM]),
            ("IsWindowVisible", wt.BOOL, [wt.HWND]),
            ("IsWindow", wt.BOOL, [wt.HWND]),
            ("IsIconic", wt.BOOL, [wt.HWND]),
            ("GetWindowTextLengthW", ctypes.c_int, [wt.HWND]),
            ("GetWindowTextW", ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
            ("GetClientRect", wt.BOOL, [wt.HWND, ctypes.POINTER(wt.RECT)]),
            ("GetDC", H, [wt.HWND]),
            ("ReleaseDC", ctypes.c_int, [wt.HWND, H]),
            ("PrintWindow", wt.BOOL, [wt.HWND, H, wt.UINT]),
        ]:
            f = getattr(u, name)
            f.restype, f.argtypes = res, args
        for name, res, args in [
            ("CreateCompatibleDC", H, [H]),
            ("CreateCompatibleBitmap", H, [H, ctypes.c_int, ctypes.c_int]),
            ("SelectObject", H, [H, H]),
            ("DeleteObject", wt.BOOL, [H]),
            ("DeleteDC", wt.BOOL, [H]),
            ("GetDIBits", ctypes.c_int, [H, H, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]),
        ]:
            f = getattr(g, name)
            f.restype, f.argtypes = res, args
        self.u, self.g, self.wt = u, g, wt

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]
        self.BITMAPINFOHEADER = BITMAPINFOHEADER

    def list_windows(self):
        out = []

        def cb(hwnd, _):
            if self.u.IsWindowVisible(hwnd):
                n = self.u.GetWindowTextLengthW(hwnd)
                buf = self.ct.create_unicode_buffer(n + 1)
                self.u.GetWindowTextW(hwnd, buf, n + 1)
                out.append({"title": buf.value, "id": hwnd})
            return True
        self.u.EnumWindows(self.EnumProc(cb), 0)
        return out

    def capture(self, win):
        u, g, hwnd = self.u, self.g, win["id"]
        if not u.IsWindow(hwnd):
            return None
        if u.IsIconic(hwnd):
            # Nothing to capture; a black frame makes locate() say "minimised".
            return win.get("last", np.zeros((64, 64, 3), np.uint8))
        rect = self.wt.RECT()
        u.GetClientRect(hwnd, self.ct.byref(rect))
        w, h = rect.right, rect.bottom
        if w <= 0 or h <= 0:
            return None
        screen_dc = u.GetDC(hwnd)
        dc = g.CreateCompatibleDC(screen_dc)
        bmp = g.CreateCompatibleBitmap(screen_dc, w, h)
        old = g.SelectObject(dc, bmp)
        try:
            ok = u.PrintWindow(hwnd, dc, self.PW_CLIENTONLY | self.PW_RENDERFULLCONTENT)
            # Top-down (negative height) 32-bit BGRA; the extra bytes cover bmiColors.
            info = (self.ct.c_byte * 64)()
            hdr = self.BITMAPINFOHEADER.from_buffer(info)
            hdr.biSize, hdr.biWidth, hdr.biHeight, hdr.biPlanes, hdr.biBitCount = 40, w, -h, 1, 32
            buf = self.ct.create_string_buffer(w * h * 4)
            got = g.GetDIBits(dc, bmp, 0, h, buf, info, 0)
        finally:
            g.SelectObject(dc, old)
            g.DeleteObject(bmp)
            g.DeleteDC(dc)
            u.ReleaseDC(hwnd, screen_dc)
        if not ok or got != h:
            return None
        img = np.ascontiguousarray(np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, 2::-1])
        win["last"] = img
        return img


_win32 = _Win32() if WINDOWS else None


# --- locating Local ----------------------------------------------------------

@dataclass
class Panel:
    tab: tuple           # (x, y, w, h) of the "Local" tab text
    top: int             # header separator line (y)
    divider: int         # line between messages and member list (x)
    right: int           # panel right border (x)
    bottom: int          # line above the input box, where the divider ends (y)

    @property
    def region(self):
        """Member list interior as (x, y, w, h)."""
        return (self.divider + 1, self.top + 1, self.right - self.divider - 1, self.bottom - self.top - 1)


def _vline(a, x, y):
    return a[y, x] - a[y, x - 1] > LINE_DELTA and a[y, x] - a[y, x + 1] > LINE_DELTA


def _hline(a, x, y):
    return a[y, x] - a[y - 1, x] > LINE_DELTA and a[y, x] - a[y + 1, x] > LINE_DELTA


def measure_panel(a, tab):
    """From the tab position, follow the panel's border lines. a = R+G+B int array."""
    h, w = a.shape
    tx, ty, tw, th = tab

    # Header separator: first horizontal line below the tab text.
    cx = tx + tw // 2
    top = next((y for y in range(ty + th, min(h - 1, ty + th + 4 * th)) if _hline(a, cx, y)), None)
    if top is None:
        return None, "no header line under the Local tab"

    # Divider: first vertical line to the right of the tab that runs down >= 40 rows.
    yr = top + 3
    if yr + 40 >= h:
        return None, "panel too close to the bottom of the window"
    divider = None
    for x in range(tx + tw, w - 1):
        if _vline(a, x, yr) and all(_vline(a, x, y) for y in range(yr, yr + 40, 4)):
            divider = x
            break
    if divider is None:
        return None, "no member list divider (is the member list hidden?)"

    # Bottom: follow the divider down until it stops.
    bottom = yr
    gap = 0
    for y in range(yr, h - 1):
        if _vline(a, divider, y):
            bottom, gap = y, 0
        else:
            gap += 1
            if gap > 40:     # bright scenery can break the divider for a long stretch
                break
    bottom += 1

    # Right border: follow the header separator right until it ends; bright scenery
    # behind the panel can break the line for ~10 px. (Line tests
    # compare to neighbours, so space bleeding through the panel doesn't matter.)
    right, gap = divider + 1, 0
    for x in range(divider + 1, w - 1):
        if _hline(a, x, top):
            right, gap = x + 1, 0
        else:
            gap += 1
            if gap > 25:
                break

    p = Panel(tab, top, divider, right, bottom)
    _, _, rw, rh = p.region
    if rw < 40 or rh < 40 or rw < 9 * th:    # a real list is >= ~10 tab-heights wide; narrower = right border cut short by scenery
        return None, f"member list too small ({rw}x{rh})"
    return p, "ok"


def locate(img):
    """Find the Local panel in a full window capture. Returns (Panel|None, reason)."""
    # imdecode rather than imread: imread can't open non-ASCII paths on Windows.
    tpl0 = cv2.imdecode(np.fromfile(TAB_TEMPLATE, np.uint8), cv2.IMREAD_GRAYSCALE)
    rgb = np.asarray(img)
    if rgb[::7, ::7].max() < 10:
        return None, "the captured picture is all black (EVE minimised, or the capture can't see it)"
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    sat = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[:, :, 1]
    a = rgb.astype(np.int32).sum(axis=2)
    reason = "no 'Local' tab found"
    for scale in (1.0, 0.9, 1.1, 0.8, 1.25, 0.7, 1.4):
        tpl = tpl0 if scale == 1.0 else cv2.resize(tpl0, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        th, tw = tpl.shape
        res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        for _ in range(8):                      # best few candidates at this scale
            _, score, _, (x, y) = cv2.minMaxLoc(res)
            if score < 0.75:
                break
            res[max(0, y - th):y + th, max(0, x - tw):x + tw] = -1
            patch_g, patch_s = gray[y:y + th, x:x + tw], sat[y:y + th, x:x + tw]
            text = patch_g > np.percentile(patch_g, 80)
            if np.median(patch_s[text]) > 80:   # coloured text, e.g. "Channel changed to Local"
                continue
            panel, reason = measure_panel(a, (x, y, tw, th))
            if panel:
                return panel, "ok"
    return None, reason


def panel_intact(img, panel):
    """Cheap per-frame check that the member list divider is still where we left it."""
    x = panel.divider
    arr = np.asarray(img)
    if panel.bottom >= arr.shape[0] or x + 1 >= arr.shape[1]:
        return False
    strip = arr[panel.top + 1:panel.bottom, x - 1:x + 2].astype(np.int32).sum(axis=2)
    ok = (strip[:, 1] - strip[:, 0] > LINE_DELTA) & (strip[:, 1] - strip[:, 2] > LINE_DELTA)
    return ok.mean() > 0.5


# --- analysis ----------------------------------------------------------------

@dataclass
class Row:
    y0: int
    y1: int
    status: str          # hostile / flagged / friendly / neutral (grey icon) / blank (no icon)
    hue: float | None = None
    icon_px: int = 0


def classify_hue(deg):
    for lo, hi, name in HUE_CLASSES:
        if lo <= deg < hi:
            return name
    return "friendly"


def runs(mask, min_len=1, max_gap=0):
    """Return [start, end) runs of True in a 1-D mask, bridging gaps <= max_gap."""
    out = []
    start = None
    gap = 0
    for i, v in enumerate(mask):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > max_gap:
                end = i - gap + 1
                if end - start >= min_len:
                    out.append((start, end))
                start, gap = None, 0
    if start is not None:
        end = len(mask) - gap
        if end - start >= min_len:
            out.append((start, end))
    return out


def analyze(crop):
    """Find pilot rows and each row's standing icon. Returns (rows, mode): mode is
    "default" (a portrait per pilot, icon at the right end of the row) or
    "compact" (EVE's compact member list: no portraits, icon left of the name)."""
    rgb = np.asarray(crop, dtype=np.int16)
    hsv = np.asarray(crop.convert("HSV"), dtype=np.int16)
    h, w, _ = rgb.shape

    # The panel is slightly see-through: space (explosions, clouds) bleeds through
    # as dim, blurry light. Only trust sharp detail (portrait edges, name text).
    lum = rgb.mean(axis=2)
    edge = np.zeros((h, w), bool)
    edge[:, 1:] |= np.abs(np.diff(lum, axis=1)) > 40
    edge[1:, :] |= np.abs(np.diff(lum, axis=0)) > 40
    text = (lum > 150) & ((rgb.max(axis=2) - rgb.min(axis=2)) < 60)
    # Name text is crisp: every stroke pixel touches a sharp edge. Light behind
    # the panel is blurred by it, so a bright patch there (an explosion, a
    # structure's lights) has no edges inside it and must not read as a name.
    text &= cv2.dilate(edge.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)

    # Portrait column: the dense block of edges in the left part of the list.
    col_score = edge[:, : max(1, int(w * 0.45))].sum(axis=0)
    if col_score.max() < 10:
        return [], "default"
    cols = runs(col_score >= 0.3 * col_score.max(), min_len=8, max_gap=3)
    if not cols:
        return [], "default"
    # Portraits are the LEFTMOST busy column (name text can score just as high
    # when portraits are dark); widen it out to the empty gaps on either side.
    # The gap is only nearly empty: with a long list, stray edges from space
    # bleeding through add up to a few per column, so scale the floor.
    px0, px1 = cols[0]
    floor = max(3, 0.12 * col_score.max())
    while px0 > 0 and col_score[px0 - 1] >= floor:
        px0 -= 1
    while px1 < len(col_score) and col_score[px1] >= floor:
        px1 += 1
    pw = px1 - px0

    # Layout: measured against the "👤 N" header (same size in both layouts), a
    # portrait is >= 2.1 lines tall; in compact mode the busy column holds icons
    # and/or names, ~1.2 lines tall. max_gap=1: stacked icons are only ~4 px apart.
    # (Text lines bridge 3 px: the 👤 glyph's neck is a 1 px row; lines are >= 7 px apart.)
    lines = runs(text.sum(axis=1) >= 2, min_len=5, max_gap=3)
    if lines:
        hh = lines[0][1] - lines[0][0]
        below = edge[lines[0][1] + 2:, px0:px1].sum(axis=1) >= 2
        if max((b - a for a, b in runs(below, max_gap=1)), default=0) < 1.7 * hh:
            # Names are left-aligned just past the icon column (~2.3 lines in): only
            # text there makes a pilot. Scenery at the list's right edge (its border
            # is measured a few px off at times) once made 3 phantom "neutrals".
            names = runs(text[:, : int(3.5 * hh)].sum(axis=1) >= 2, min_len=5, max_gap=3)
            return analyze_compact(hsv, [n for n in names if n[0] > lines[0][1]], hh, h, w), "compact"

    # Rows: every pilot has a name in bright white text right of the portrait
    # (dark portraits can have too little detail to rely on). One text line =
    # one pilot; the row spans a portrait's height around it.
    name_lines = runs(text[:, px1 + 4:].sum(axis=1) >= 2, min_len=max(4, pw // 6), max_gap=2)
    centers = []
    for a, b in name_lines:
        c = (a + b) / 2
        if c < 0.9 * pw:                 # the "👤 N" member-count header at the top
            continue
        if centers and c - centers[-1] < 0.5 * pw:
            centers[-1] = (centers[-1] + c) / 2
        else:
            centers.append(c)
    bands = []
    for k, c in enumerate(centers):
        y0 = max(0, c - 0.6 * pw, (centers[k - 1] + c) / 2 if k else 0)
        y1 = min(h, c + 0.6 * pw, (c + centers[k + 1]) / 2 if k + 1 < len(centers) else h)
        bands.append((int(y0), int(y1)))

    # Standing icons sit at the right end of the row.
    icon_zone = np.ones((h, w), bool)
    icon_zone[:, : max(px1 + pw // 2, int(w * 0.7))] = False  # never look at portraits
    find = icon_finder(hsv, icon_zone, pw)

    rows = []
    for c, (y0, y1) in zip(centers, bands):
        row = find(y0, y1)
        if row:
            rows.append(row)
        elif c < h - 0.3 * pw and edge[y0:y1, px0:px1].mean() >= 0.01:
            rows.append(Row(y0, y1, "blank", None, 0))
        # else: a pilot half scrolled out at the bottom, icon cut off - not a
        # neutral; or no portrait beside the "name" (real ones measured >= 2%
        # edge pixels): a bright spot behind the panel, not a pilot. Rows with
        # a standing icon are kept regardless: a portrait may not have loaded yet.
    return rows, "default"


def analyze_compact(hsv, lines, lh, h, w):
    """Compact member list: one name per text line (`lines`, header excluded),
    the standing icon a square about one line tall just left of the name.
    lh: the header line's height, the size reference (names vary: a short one
    measures 10 px, a long one 16)."""
    if not lines:
        return []
    centers = [(a + b) / 2 for a, b in lines]
    icon_zone = np.zeros((h, w), bool)
    icon_zone[:, : int(2 * lh)] = True                    # names start ~2.3 line heights in
    find = icon_finder(hsv, icon_zone, 2 * lh)
    rows = []
    for k, ((a, b), c) in enumerate(zip(lines, centers)):
        y0 = int(max(0, c - 0.8 * lh, (centers[k - 1] + c) / 2 if k else 0))
        y1 = int(min(h, c + 0.8 * lh, (c + centers[k + 1]) / 2 if k + 1 < len(centers) else h))
        row = find(y0, y1)
        if row:
            rows.append(row)
        elif b < h - 1:              # not a name half scrolled out at the bottom
            rows.append(Row(y0, y1, "blank", None, 0))
    return rows


def icon_finder(hsv, zone, pw):
    """Returns find(y0, y1): the Row for the standing icon centred in that band
    of `zone`, or None. Icons are squares 0.25-0.75 pw across.

    Icons are small, solid, strongly coloured squares; weapon glows bleeding
    through the panel are big, soft and paler. Some icons are drawn dimmed (a
    dark red star measured V ~58), so the brightness floor sits just above the
    background (V ~22), not higher."""
    colored = (hsv[:, :, 1] > 150) & (hsv[:, :, 2] > 40) & zone    # S > 0.59, V > 0.16
    # Neutral standing icon: a mid-grey square (V ~110); name text is far brighter.
    grey = (hsv[:, :, 1] < 50) & (hsv[:, :, 2] > 80) & (hsv[:, :, 2] < 170) & zone

    def blobs(mask):
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if (0.25 * pw <= bw <= 0.75 * pw and 0.25 * pw <= bh <= 0.75 * pw
                    and 0.75 <= bw / bh <= 1.33 and area / (bw * bh) >= 0.6):
                yield y + bh / 2, labels == i, int(area)

    # Split by colour family first, so an orange glow touching a blue icon
    # doesn't merge with it into one big blob.
    hue_deg = hsv[:, :, 0].astype(float) * 360 / 256
    icons = []
    for lo, hi in ICON_HUE_BINS:
        in_bin = (hue_deg >= lo) & (hue_deg < hi) if lo < hi else (hue_deg >= lo) | (hue_deg < hi)
        icons += blobs(colored & in_bin)
    grey_icons = list(blobs(grey))

    def find(y0, y1):
        mine = [(mask, area) for cy, mask, area in icons if y0 <= cy < y1]
        greys = [area for cy, _, area in grey_icons if y0 <= cy < y1]
        if mine:
            mask, area = max(mine, key=lambda t: t[1])
            # circular mean so red around 0/360 averages correctly
            ang = np.deg2rad(hue_deg[mask])
            deg = float(np.rad2deg(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())) % 360)
            return Row(y0, y1, classify_hue(deg), deg, area)
        if greys:
            return Row(y0, y1, "neutral", None, max(greys))
        return None
    return find


def hold_blinking(rows, seen, now, hold):
    """Keep blinking icons lit: war targets have a red star that blinks about
    once a second and is fully dark in over half the frames. A row that reads
    blank but showed a hostile/flagged icon at the same spot within `hold`
    seconds keeps that status. `seen` is the caller's memory of recent icons."""
    seen[:] = [s for s in seen if now - s[0] <= hold]
    seen += [(now, (r.y0 + r.y1) / 2, r.status, r.hue) for r in rows if r.status in ("hostile", "flagged")]
    out = []
    for r in rows:
        hits = [s for s in seen if r.y0 <= s[1] < r.y1] if r.status == "blank" else []
        out.append(Row(r.y0, r.y1, hits[-1][2], hits[-1][3], 0) if hits else r)
    return out


def has_scrollbar(crop):
    """True if the member list shows a scrollbar, i.e. some pilots are out of view.

    The thumb is a thin (1-3 px) light vertical bar near the right edge with
    dark columns on both sides; nothing else in the list makes a line that long.
    In a long Local (hundreds of pilots) it is short (~19 px) and dim (V ~87).
    """
    v = np.asarray(crop.convert("HSV"))[:, :, 2]
    h, w = v.shape
    bright = v > 60
    x_from = int(w * 0.85)
    for x in range(max(1, x_from), w - 3):
        for bw in (1, 2, 3):
            bar = bright[:, x:x + bw].all(axis=1) & ~bright[:, x - 1] & ~bright[:, x + bw]
            min_len = max(12, h // 40)
            if bar.sum() >= min_len and runs(bar, min_len=min_len, max_gap=1):
                return True
    return False


def summarize(rows, self_rows):
    hostile = sum(r.status == "hostile" for r in rows)
    flagged = sum(r.status == "flagged" for r in rows)
    friendly = sum(r.status == "friendly" for r in rows)
    # Grey-icon rows are neutrals for certain. Rows with no icon at all are
    # neutrals too (when EVE hides neutral icons), except you / your alts.
    blank = max(0, sum(r.status == "blank" for r in rows) - self_rows)
    neutral = sum(r.status == "neutral" for r in rows) + blank
    return {"rows": len(rows), "hostile": hostile, "neutral": neutral + flagged, "friendly": friendly}


def crop_panel(img, panel):
    x, y, w, h = panel.region
    return Image.fromarray(np.ascontiguousarray(np.asarray(img)[y:y + h, x:x + w]))


def describe(panel, state):
    x, y, w, h = panel.region
    return (f"member list at x={x} y={y} size {w}x{h}px (window pixels); "
            f"{state['rows']} pilots: {state['friendly']} friendly, "
            f"{state['neutral']} neutral, {state['hostile']} hostile")


def annotate(img, panel, rows, path):
    out = Image.fromarray(np.asarray(img)).convert("RGB")
    d = ImageDraw.Draw(out)
    tx, ty, tw, th = panel.tab
    d.rectangle([tx, ty, tx + tw, ty + th], outline="lime")
    rx, ry, rw, rh = panel.region
    d.rectangle([rx, ry, rx + rw - 1, ry + rh - 1], outline="magenta", width=2)
    colors = {"hostile": "red", "flagged": "yellow", "friendly": "cyan", "neutral": "white", "blank": "gray"}
    for r in rows:
        d.rectangle([rx, ry + r.y0, rx + rw - 1, ry + r.y1 - 1], outline=colors[r.status])
    pad = 40
    out = out.crop((max(0, tx - pad), max(0, ty - pad), min(out.width, rx + rw + pad), min(out.height, ry + rh + pad)))
    out.save(path)


# --- alerts ------------------------------------------------------------------

# Windows toast through the built-in Windows PowerShell (WinRT isn't available
# in PowerShell 7), shown under PowerShell's app id so nothing needs installing.
# The toast XML comes in through an environment variable: no quoting problems.
TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml($env:EVE_LOCAL_TOAST)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
    [Windows.UI.Notifications.ToastNotification]::new($x))
"""


def play(path):
    if MUTED.is_set() or not path or not Path(path).exists():
        return
    if WINDOWS:
        import winsound       # plays .wav only
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    else:
        subprocess.Popen(["pw-play", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def notify(title, body, urgency="critical"):
    if WINDOWS:
        # "reminder" toasts stay on screen until dismissed, like critical ones on Linux.
        scenario = ' scenario="reminder"' if urgency == "critical" else ""
        actions = ('<actions><action content="Dismiss" arguments="dismiss" activationType="system"/></actions>'
                   if urgency == "critical" else "")
        xml = (f'<toast{scenario}><visual><binding template="ToastGeneric"><text>{escape(title)}</text>'
               f'<text>{escape(body)}</text></binding></visual>{actions}</toast>')
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", TOAST_PS],
                         env=dict(os.environ, EVE_LOCAL_TOAST=xml), creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["notify-send", "-u", urgency, "-a", "EVE Local", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- intel (chat logs) -------------------------------------------------------
# With "Log chat to file" on, EVE writes every channel to Documents/EVE/logs/
# Chatlogs as UTF-16 text, one file per channel per session, named
# "<channel>_<YYYYMMDD>_<HHMMSS>_<characterID>.txt". The Local log says which
# system you're in ("Channel changed to Local : 4-HWWF"); intel messages name
# systems, linked ones with a "*" ("P3EN-E*  Some Pilot  nv").

LOG_NAME = re.compile(r"^(.+)_(\d{8})_(\d{6})(?:_(\d+))?\.txt$")
LOG_LINE = re.compile(r"^\[ (\d{4}\.\d\d\.\d\d \d\d:\d\d:\d\d) \] (.+?) > (.*)$")
CLEAR_WORDS = {"clr", "clear", "cleared"}
ASK_WORDS = {"status", "stat", "status?", "?"}
INTEL_MAX_AGE = 300           # seconds; older messages (e.g. a log read late) are stale
INTEL_RECAP = 1800            # seconds of a channel's history summed up once when the watcher starts
# Auto-detect: a channel is intel if most of its recent messages name a system
# (an intel channel ~80%, corp / alliance / fleet chat ~10%).
AUTO_DAYS = 3                 # logs written to this recently are looked at
AUTO_MIN_MSGS = 20
AUTO_MIN_SHARE = 0.5
AUTO_RETRY = 60               # seconds between attempts while none is found
# Several of your characters in one intel channel each log it; only one log is
# read. A standby takes over when the read one is this many seconds behind it
# (that character logged out or left the channel).
PRIMARY_STALE = 60
STEAM_EVE = Path("steamapps", "compatdata", "8500", "pfx", "drive_c", "users", "steamuser")


def away(jumps):
    return "in your system" if jumps == 0 else f"{jumps} jump{'s' if jumps > 1 else ''} away"


def find_chatlog_dir(cfg):
    if cfg["chatlog_dir"]:
        return Path(cfg["chatlog_dir"]).expanduser()
    tail = Path("Documents", "EVE", "logs", "Chatlogs")
    if WINDOWS:
        # Documents can be moved (OneDrive backup, another drive); the registry knows where.
        import winreg
        dirs = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                docs = os.path.expandvars(winreg.QueryValueEx(k, "Personal")[0])
                dirs.append(Path(docs) / tail.relative_to("Documents"))
        except OSError:
            pass
        dirs += [Path.home() / tail, Path.home() / "OneDrive" / tail]
    else:
        # EVE under Proton keeps its Documents inside the Steam library it's installed in.
        libs = [Path.home() / ".local" / "share" / "Steam", Path.home() / ".steam" / "steam"]
        for root in list(libs):
            vdf = root / "steamapps" / "libraryfolders.vdf"
            if vdf.exists():
                libs += [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace"))]
        dirs = [lib / STEAM_EVE / tail for lib in libs]
    return next((d for d in dirs if d.is_dir()), None)


class StarMap:
    """Stargate connections between systems (data/eve_map.json, from CCP's static data)."""

    def __init__(self, bridges=()):
        data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        self.names = data["systems"]
        self.by_lower = {n.lower(): n for n in self.names}
        self.links = {n: set() for n in self.names}
        pairs = [(self.names[a], self.names[b]) for a, b in data["gates"]]
        for a, b in bridges:
            if self.name(a) and self.name(b):
                pairs.append((self.name(a), self.name(b)))
            else:
                print(ts(), f"jump_bridges: unknown system in {a} - {b}, ignored", flush=True)
        for a, b in pairs:
            self.links[a].add(b)
            self.links[b].add(a)

    def name(self, text):
        return self.by_lower.get(text.lower())

    def within(self, start, jumps):
        """{system: jumps from start} for every system at most `jumps` away."""
        dist = {start: 0}
        edge = [start]
        for d in range(1, jumps + 1):
            edge = [n for s in edge for n in self.links[s] if n not in dist]
            for n in edge:
                dist[n] = d
        return dist


class ChatLog:
    """One chat log file, read incrementally as EVE appends to it."""

    def __init__(self, path, channel, from_start):
        self.path, self.channel, self.listener = path, channel, None
        self.offset = 0 if from_start else path.stat().st_size // 2 * 2
        self.catch_up = False     # just took over from another log: skip its stale lines

    def read_listener(self, f):
        if self.listener is None:                    # header may not be written yet
            m = re.search(r"Listener:\s*(.+?)\s*$", f.read(4096).decode("utf-16-le", "replace"), re.M)
            self.listener = m and m.group(1)
        return self.listener

    def who(self):
        """The character logging this channel, or None if not known yet."""
        if self.listener is None:
            try:
                with self.path.open("rb") as f:
                    self.read_listener(f)
            except OSError:
                pass
        return self.listener

    def read(self):
        """New complete lines since the last call, as (utc time, sender, text)."""
        try:
            with self.path.open("rb") as f:
                self.read_listener(f)
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        # Only up to the last whole line ("\n" on a 2-byte UTF-16 boundary).
        end = data.rfind(b"\n\x00")
        while end > 0 and end % 2:
            end = data.rfind(b"\n\x00", 0, end + 1)
        if end < 0:
            return []
        self.offset += end + 2
        out = []
        for line in data[:end + 2].decode("utf-16-le", "replace").splitlines():
            m = LOG_LINE.match(line.strip("\ufeff\r "))
            if m:
                t = datetime.strptime(m.group(1), "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
                out.append((t, m.group(2), m.group(3).strip()))
        return out


@dataclass
class Report:
    time: datetime
    channel: str
    sender: str
    text: str
    kind: str            # hostile / clear / ask / none (no system named)
    system: str | None = None
    jumps: int | None = None   # None = not within range (or your system is unknown)


class Intel:
    """Follows your Local log (where you are) and the intel channels' logs."""

    def __init__(self, cfg, recap=False):
        self.cfg = cfg
        self.recap_due = [] if recap else None   # logs whose recent history is still to be summed up
        self.recapped = set()     # channels (lower case) already summed up
        self.names = list(cfg["intel_channels"] or [])   # [] = not known yet: auto-detect
        self.channels = {c.lower() for c in self.names}
        self.last_detect = float("-inf")
        self.told_none = False
        self.dir = find_chatlog_dir(cfg)
        self.map = StarMap(cfg["jump_bridges"])
        self.logs = {}            # path -> ChatLog (the newest file per channel and character)
        self.where = {}           # listener -> (system, time they arrived)
        self.seen = {}            # (sender, text) -> its time: one message logged by several characters
        self.first_scan = True
        self.last_scan = float("-inf")
        self.near_cache = (None, {})
        self.primary = {}         # intel channel (lower case) -> path of the one log read
        self.missing = set()      # configured channels already reported as having no log
        self.counts = collections.Counter()   # (channel, nearby/clear/far/other) since the last summary()
        self.last_msg = {}        # channel -> time of its newest message
        self.total = collections.Counter()    # channel -> messages read since start (for the UI)
        self.backlog = []         # startup recap reports, for the UI to show (they never alarm)

    def scan(self):
        """Pick up new log files (EVE starts one per channel when you log in / rejoin)."""
        newest = {}
        try:
            entries = list(os.scandir(self.dir))
        except OSError:
            return
        for e in entries:
            m = LOG_NAME.match(e.name)
            if m and (m.group(1).lower() in self.channels or m.group(1) == "Local"):
                key = (m.group(1).lower(), m.group(4))
                if key not in newest or m.group(2, 3) > newest[key][1]:
                    newest[key] = (Path(e.path), m.group(2, 3), m.group(1))
        keep = {path for path, _, _ in newest.values()}
        self.logs = {p: log for p, log in self.logs.items() if p in keep}
        for path, _, channel in newest.values():
            if path not in self.logs:
                # Local is read whole (to learn where you are now); intel only from
                # now on, except files that appear later, which are all new.
                self.logs[path] = ChatLog(path, channel, channel == "Local" or not self.first_scan)
        self.first_scan = False

    def here(self, character):
        """The system `character` is in, or None. Only with no character known
        (no EVE window yet, no `character` set) use whoever moved last: another
        pilot's system would give wrong distances."""
        if character:
            return self.where.get(character, (None,))[0]
        return max(self.where.values(), key=lambda v: v[1])[0] if self.where else None

    def nearby(self, system):
        key = (system, self.cfg["intel_jumps"])        # jumps can change live (TUI settings)
        if self.near_cache[0] != key:
            start = self.map.name(system) if system else None
            self.near_cache = (key, self.map.within(start, self.cfg["intel_jumps"]) if start else {})
        return self.near_cache[1]

    def classify(self, t, channel, sender, text, near):
        words = [w.strip(".,;:!()[]<>\"'") for w in text.split()]
        # Linked systems end in "*"; if any are, they're what the message is about.
        linked = [w[:-1] for w in words if w.endswith("*")]
        systems = {self.map.name(w) for w in (linked or [w.rstrip("*") for w in words])} - {None}
        if not systems:
            # Shorthand like "IPAY" for IPAY-2: only if it fits exactly one system near you.
            for w in words:
                w = w.rstrip("*")
                if len(w) >= 3 and w.isupper() and w.lower() not in CLEAR_WORDS | ASK_WORDS:
                    hits = [s for s in near if s.lower().startswith(w.lower())]
                    if len(hits) == 1:
                        systems.add(hits[0])
        if not systems:
            return Report(t, channel, sender, text, "none")
        system = min(systems, key=lambda s: near.get(s, 999))
        rest = {w.lower().rstrip("*") for w in words} - {s.lower() for s in systems} - {""}
        kind = "clear" if rest & CLEAR_WORDS else "ask" if rest and rest <= ASK_WORDS else "hostile"
        return Report(t, channel, sender, text, kind, system, near.get(system))

    def duplicate(self, t, sender, text):
        """Seen already? Each character in the channel logs it, timestamps up to ~1 s apart."""
        prev = self.seen.get((sender, text))
        self.seen[(sender, text)] = t
        if len(self.seen) > 5000:
            self.seen = {k: v for k, v in self.seen.items() if (t - v).total_seconds() < INTEL_MAX_AGE}
        return prev is not None and abs((t - prev).total_seconds()) <= 5

    def detect(self):
        """[(channel, messages, messages naming a system)] for the recently written logs."""
        cutoff = time.time() - AUTO_DAYS * 86400
        counts = collections.defaultdict(lambda: [0, 0])
        try:
            entries = list(os.scandir(self.dir))
        except OSError:
            return []
        for e in entries:
            m = LOG_NAME.match(e.name)
            if not m or m.group(1) == "Local" or e.stat().st_mtime < cutoff:
                continue
            for _, sender, text in ChatLog(Path(e.path), m.group(1), True).read():
                if sender != "EVE System":
                    c = counts[m.group(1)]
                    c[0] += 1
                    c[1] += any(self.map.name(w.strip(".,;:!()[]<>\"'").rstrip("*")) for w in text.split())
        return sorted(((ch, n, hits) for ch, (n, hits) in counts.items()), key=lambda c: -c[1])

    @staticmethod
    def looks_like_intel(n, hits):
        return n >= AUTO_MIN_MSGS and hits >= n * AUTO_MIN_SHARE

    def auto_detect(self):
        found = [c for c in self.detect() if self.looks_like_intel(*c[1:])]
        if found:
            self.names = [ch for ch, _, _ in found]
            self.channels = {c.lower() for c in self.names}
            self.first_scan = True                     # skip their history, as at startup
            for ch, n, hits in found:
                print(ts(), f"Intel: auto-detected {bold(ch)} ({hits * 100 // n}% of {n} recent messages "
                      "name a system) - set intel_channels in the config to choose", flush=True)
        elif not self.told_none:
            self.told_none = True
            print(ts(), "Intel: no intel channel found in the chat logs yet - will keep looking "
                  "(or set intel_channels in the config)", flush=True)

    def poll(self, character, now):
        """New intel reports since the last call (at most one scan per second)."""
        if not self.dir or now - self.last_scan < 1.0:
            return []
        self.last_scan = now
        if not self.names and now - self.last_detect >= AUTO_RETRY:
            self.last_detect = now
            self.auto_detect()
        self.scan()
        self.pick_primaries(character)
        before = self.here(character)
        fresh = []
        utc = datetime.now(timezone.utc)
        for log in self.logs.values():
            if log.channel == "Local":
                for t, sender, text in log.read():
                    if sender == "EVE System" and text.startswith("Channel changed to Local"):
                        self.where[log.listener] = (text.split(":", 1)[-1].strip(" *"), t)
            elif self.primary.get(log.channel.lower()) == log.path:
                for t, sender, text in log.read():
                    # A log taking over is read from where it was left: what the
                    # old one already gave is a duplicate, and old lines are dropped.
                    if log.catch_up and (utc - t).total_seconds() > INTEL_MAX_AGE:
                        continue
                    if not self.duplicate(t, sender, text):
                        fresh.append((t, log.channel, sender, text))
                log.catch_up = False
        self.announce()
        system = self.here(character)
        if system != before:
            print(ts(), f"Intel: you are in {system}"
                  + ("" if self.map.name(system) else " (no stargates - intel distances unknown)"), flush=True)
        near = self.nearby(system)
        while self.recap_due:
            self.backlog += self.recap(self.recap_due.pop(0), near, utc, {(m[0], m[2], m[3]) for m in fresh}, system)
        reports = []
        for m in sorted(fresh):
            self.last_msg[m[1]] = m[0]
            self.total[m[1]] += 1
            if (utc - m[0]).total_seconds() > INTEL_MAX_AGE:
                self.counts[m[1], "other"] += 1
                continue
            r = self.classify(*m, near)
            self.counts[r.channel, "other" if r.kind not in ("hostile", "clear") else
                        "far" if r.jumps is None else "nearby" if r.kind == "hostile" else "clear"] += 1
            reports.append(r)
        return reports

    def pick_primaries(self, character):
        """Choose the one log read per intel channel: `character`'s (the watched
        client's pilot), else the one already read, else the newest - skipping
        any that has fallen PRIMARY_STALE seconds behind the newest."""
        by_channel = collections.defaultdict(list)
        for log in self.logs.values():
            if log.channel != "Local":
                by_channel[log.channel.lower()].append(log)
        primary = {}
        for ch, logs in by_channel.items():
            mtime = {}
            for log in logs:
                try:
                    mtime[log.path] = log.path.stat().st_mtime
                except OSError:
                    mtime[log.path] = 0.0
            newest = max(logs, key=lambda log: mtime[log.path])
            live = [log for log in logs if mtime[log.path] >= mtime[newest.path] - PRIMARY_STALE]
            cur = self.primary.get(ch)
            pick = (next((log for log in live if character and log.who() == character), None)
                    or next((log for log in live if log.path == cur), None) or newest)
            primary[ch] = pick.path
            if pick.path != cur:
                pick.catch_up = cur is not None
                if self.recap_due is not None and ch not in self.recapped:
                    self.recapped.add(ch)
                    self.recap_due.append(pick)
                others = len(logs) - 1
                print(ts(), f"Intel: following {bold(pick.channel)} as {pick.who() or '?'} ({pick.path.name})"
                      + (f", {others} other character{'s' if others > 1 else ''} on standby" if others else ""),
                      flush=True)
        self.primary = primary

    def recap(self, log, near, utc, skip, system):
        """Once per channel at startup: the last INTEL_RECAP seconds of it, as
        the latest report for each system in range. Returns those reports."""
        msgs = [m for m in ChatLog(log.path, log.channel, True).read()
                if (utc - m[0]).total_seconds() <= INTEL_RECAP and m[1] != "EVE System" and m not in skip]
        latest = {}
        for m in msgs:
            r = self.classify(m[0], log.channel, *m[1:], near)
            if r.jumps is not None and r.kind in ("hostile", "clear"):
                latest[r.system] = r
        mins = INTEL_RECAP // 60
        if not system:
            print(ts(), f"Intel: {bold(log.channel)} last {mins} min: {len(msgs)} msgs "
                  "(your system unknown - distances not checked)", flush=True)
            return []
        hostile = sum(r.kind == "hostile" for r in latest.values())
        print(ts(), f"Intel: {bold(log.channel)} last {mins} min: {len(msgs)} msgs, "
              + (f"{len(latest)} system{'s' if len(latest) > 1 else ''} within {self.cfg['intel_jumps']} jumps "
                 f"reported ({hostile} hostile at last report):" if latest else
                 f"nothing reported within {self.cfg['intel_jumps']} jumps of {system}"), flush=True)
        out = sorted(latest.values(), key=lambda r: (r.jumps, r.time))
        for r in out:
            ago = (utc - r.time).total_seconds() / 60
            print(ts(), f"  {r.kind.upper()} {r.system} ({away(r.jumps)}, {ago:.0f} min ago) "
                  f"{r.sender} > {r.text}", flush=True)
        return out

    def announce(self):
        """Say which configured channels have no log."""
        found = {log.channel.lower() for log in self.logs.values() if log.channel != "Local"}
        self.missing &= self.channels - found         # a channel that comes back is reported again
        for c in self.names:
            if c.lower() not in found and c.lower() not in self.missing:
                self.missing.add(c.lower())
                print(ts(), f"Intel: no chat log for {bold(c)} - is the channel open, with "
                      "'Log chat to file' on?", flush=True)

    def summary(self):
        """Messages read per intel channel since the last call."""
        parts = []
        for log in {log.channel: log for log in self.logs.values() if log.channel != "Local"}.values():
            c = log.channel
            n = {k: self.counts[c, k] for k in ("nearby", "clear", "far", "other")}
            last = self.last_msg.get(c)
            ago = f", last {(datetime.now(timezone.utc) - last).total_seconds() / 60:.0f} min ago" if last else ""
            parts.append(f"{c}: {sum(n.values())} msgs ({n['nearby']} hostile in range, {n['clear']} clear, "
                         f"{n['far']} out of range, {n['other']} other){ago}")
        self.counts.clear()
        return "; ".join(parts)

    def info(self, character):
        """A side-effect free snapshot of what intel is doing, for a UI."""
        system = self.here(character)
        return {"dir": self.dir, "names": list(self.names), "system": system,
                "found": sorted({log.channel for log in self.logs.values() if log.channel != "Local"}),
                "near": len(self.nearby(system)) if system else 0, "jumps": self.cfg["intel_jumps"],
                "total": sum(self.total.values()), "last": max(self.last_msg.values(), default=None)}

    def status(self, character):
        system = self.here(character)
        if not self.dir:
            return "intel: chat log folder not found"
        if not self.names:
            return "intel: no intel channel found yet (auto-detect)"
        if not any(log.channel.lower() in self.channels for log in self.logs.values()):
            return "intel: no log for " + ", ".join(self.names)
        where = (f"in {system}, {len(self.nearby(system))} systems within {self.cfg['intel_jumps']} jumps"
                 if system else "your system unknown")
        return f"intel: {where} | {self.summary()}"


def list_channels(cfg, days=30):
    """Chat channels with a recent log file, newest first (Local excluded): the intel choices."""
    newest = {}
    try:
        for e in os.scandir(find_chatlog_dir(cfg)):
            m = LOG_NAME.match(e.name)
            if m and m.group(1) != "Local" and time.time() - e.stat().st_mtime < days * 86400:
                newest[m.group(1)] = max(newest.get(m.group(1), 0), e.stat().st_mtime)
    except OSError:
        pass
    return sorted(newest, key=lambda c: -newest[c])


# --- commands ----------------------------------------------------------------

def debug_lines(img, panel, cfg):
    """What the watcher reads from `img`, row by row, as text lines. Returns (lines, rows)."""
    crop = crop_panel(img, panel)
    rows, mode = analyze(crop)
    out = [f"tab {panel.tab}, top={panel.top} divider={panel.divider} right={panel.right} bottom={panel.bottom}",
           f"member list layout: {mode}"]
    for i, r in enumerate(rows, 1):
        hue = f"hue={r.hue:5.1f}" if r.hue is not None else ("grey icon" if r.status == "neutral" else "no icon  ")
        out.append(f"  row {i:2d} y={r.y0:4d}-{r.y1:<4d} {r.status:8s} {hue} px={r.icon_px}")
    out.append(describe(panel, summarize(rows, cfg["self_rows"])))
    if has_scrollbar(crop):
        out.append("WARNING: the member list has a scrollbar - pilots scrolled out of view are NOT checked")
    return out, rows


SNAPSHOT_KEEP = 30       # ponytail: oldest snapshots deleted past this; raw frames are a few MB each


def save_snapshot(img, panel, cfg, name, extra=()):
    """Save a frame for later study: raw.png (a window capture, as `debug PNG` and
    samples/ take it), annotated.png and info.txt (rows read + `extra` lines).
    Returns the folder."""
    root = CONFIG_PATH.parent / "snapshots"
    d = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
    d.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.asarray(img)).save(d / "raw.png", compress_level=1)
    lines = []
    if panel is None:
        panel, reason = locate(img)
        lines.append(f"locate: {reason}")
    if panel:
        more, rows = debug_lines(img, panel, cfg)
        lines += more
        annotate(img, panel, rows, d / "annotated.png")
    (d / "info.txt").write_text("\n".join([*lines, "", *extra, ""]), encoding="utf-8")
    for old in sorted(p for p in root.iterdir() if p.is_dir())[:-SNAPSHOT_KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    return d


def cmd_debug(cfg, image_path):
    if image_path:
        img = Image.open(image_path).convert("RGB")
    else:
        win = find_window(cfg["character"])
        img = capture(win) if win else None
        if img is None:
            sys.exit("Could not capture the EVE window.")
        print(f"window: '{win['title']}' ({img.shape[1]}x{img.shape[0]})")
    t = time.time()
    panel, reason = locate(img)
    print(f"locate: {reason} ({(time.time() - t) * 1000:.0f} ms)")
    if not panel:
        sys.exit(1)
    lines, rows = debug_lines(img, panel, cfg)
    print("\n".join(lines))
    out = CONFIG_PATH.parent / "debug.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    annotate(img, panel, rows, out)
    print(f"annotated image: {out}")
    if not image_path:
        raw = CONFIG_PATH.parent / "debug-raw.png"
        Image.fromarray(np.asarray(img)).save(raw)
        print(f"raw capture (for samples/): {raw}")


def cmd_intel(cfg):
    intel = Intel(cfg)
    print(f"chat logs: {intel.dir or 'NOT FOUND - set chatlog_dir in the config'}")
    if not intel.dir:
        sys.exit(1)
    if not intel.names:
        print(f"no intel_channels in the config; auto-detect (logs of the last {AUTO_DAYS} days):")
        for ch, n, hits in intel.detect():
            print(f"  {ch:30s} {n:6d} msgs, {hits * 100 // n:3d}% name a system"
                  + ("  <- intel" if intel.looks_like_intel(n, hits) else ""))
    intel.first_scan = False                   # read the intel logs from the start
    intel.poll(cfg["character"], time.time())
    for log in intel.logs.values():
        standby = log.channel != "Local" and intel.primary.get(log.channel.lower()) != log.path
        print(f"  {log.channel:20s} {log.listener or '?':24s} {log.path.name}" + ("  (standby)" if standby else ""))
    for listener, (system, t) in intel.where.items():
        print(f"{listener} is in {system} (since {t:%H:%M} EVE time)")
    system = intel.here(cfg["character"])
    near = intel.nearby(system)
    print(f"distances from {system}: {len(near)} systems within {cfg['intel_jumps']} jumps")
    # Every message of the current logs, as the watcher would read it (ignoring age).
    intel.seen.clear()
    msgs = sorted(m for log in intel.logs.values() if intel.primary.get(log.channel.lower()) == log.path
                  for m in ChatLog(log.path, log.channel, True).read())
    msgs = [m for m in msgs if not intel.duplicate(*m)]
    for t, sender, text in msgs[-25:]:
        r = intel.classify(t, "", sender, text, near)
        where = "-" if r.system is None else f"{r.system} ({'far' if r.jumps is None else r.jumps})"
        alert = "ALERT" if r.kind == "hostile" and r.jumps is not None else ""
        print(f"  {t:%H:%M:%S} {r.kind:7s} {where:16s} {alert:5s} {sender} > {text}")


def threats(state, cfg):
    return state["hostile"] + (state["neutral"] if cfg["alert_neutrals"] else 0)


def cmd_run(cfg, ctx=None):
    ctx = ctx or Ctx()
    who = cfg["character"] or "any EVE window"
    print(f"{ts()} Looking for Local in '{who}'...", flush=True)

    panel = None
    size = None
    lost_since = None         # when the panel/rows first went missing
    lost_alerted = False
    last_locate = float("-inf")
    ever_found = False
    confirmed = None          # last state we acted on
    history = collections.deque()   # (time, state) of recent frames for voting
    last_alarm = 0.0
    settle_until = 0.0
    candidate, candidate_since = None, 0.0   # a worse reading waiting out CONFIRM_THREAT_S
    waiting_msg = None
    win, win_checked = None, float("-inf")
    watched = None            # the last window we watched; kept even across failed captures
    frame_hash, last_new_frame, stale_alerted = None, time.time(), False
    last_heartbeat, checks = time.time(), 0
    seen_icons = []           # recent hostile/flagged icons, to hold blinking ones
    scrolled = False          # member list has a scrollbar: some pilots not visible
    sb_flips = 0              # consecutive frames disagreeing with `scrolled`
    mode, mode_flips = None, 0   # member list layout (default / compact), same debounce
    intel = Intel(cfg, recap=True) if cfg["intel_channels"] is not False else None
    if intel:
        print(ts(), f"Intel: reading {', '.join(map(bold, intel.names)) or 'an auto-detected channel'} from "
              f"{intel.dir}, alerting within {cfg['intel_jumps']} jumps" if intel.dir else
              "Intel: EVE's Chatlogs folder not found - set chatlog_dir in the config", flush=True)
    else:
        print(ts(), f'Intel: off ("intel_channels": false in {CONFIG_PATH})', flush=True)

    def alarm(state, where):
        kind = "HOSTILE" if state["hostile"] else "NEUTRAL"
        play(cfg["sound_hostile"])
        notify(f"{kind} {where}", f"{state['hostile']} hostile, {state['neutral']} neutral")
        ctx.event("alert", text=f"{kind} {where}: {state['hostile']} hostile, {state['neutral']} neutral")
        if cfg["snapshot_alerts"]:
            snapshot("alarm")

    def snapshot(why):
        """Save the current frame + what was read, and the readings that led up to it."""
        extra = [f"{why} at {ts()}, window '{(win or watched or {}).get('title', '?')}'",
                 f"confirmed state: {confirmed}", f"frames in the last {cfg['vote_seconds']} s (voted on):",
                 *(f"  {time.strftime('%H:%M:%S', time.localtime(t))} {s}" for t, s in history)]
        try:
            d = save_snapshot(img, panel, cfg, f"{character() or 'any'}-{why}", extra)
            print(ts(), f"Snapshot saved: {d}", flush=True)
        except Exception as e:          # never let a debugging aid stop the watcher
            print(ts(), f"Snapshot failed: {e!r}", flush=True)

    def current_status():
        if win is None:
            return "off", "NOT WATCHING - EVE window not found"
        if stale_alerted:
            return "off", "NOT WATCHING - EVE picture frozen"
        if panel is None or lost_alerted:
            return "off", "NOT WATCHING - Local not visible"
        if not confirmed:
            return "ok", "watching (settling)"
        text = (f"{'PARTLY watching (list scrolls, pilots out of view)' if scrolled else 'watching'}: "
                f"pilots={confirmed['rows']} hostile={confirmed['hostile']} "
                f"neutral={confirmed['neutral']} friendly={confirmed['friendly']}")
        return ("alert" if threats(confirmed, cfg) else "ok"), text

    def heartbeat(now):
        """Periodic status line, so a quiet console visibly means 'all clear'."""
        status = current_status()[1]
        minutes = (now - last_heartbeat) / 60
        print(ts(), f"HEARTBEAT {status} | {checks} checks in the last {minutes:.0f} min, "
              f"EVE last drew a new frame {now - last_new_frame:.0f} s ago"
              + (f" | {intel.status(character())}" if intel else ""), flush=True)

    def character():
        """Whose system intel distances are measured from: the watched client's pilot."""
        title = (win or watched or {}).get("title", "")
        return title[len("EVE - "):] if title.startswith("EVE - ") else cfg["character"]

    def check_intel(now):
        global _intel_sound_at
        for r in intel.backlog:                       # startup recap: shown, never alarmed
            ctx.event("intel", report=r, where=away(r.jumps), recap=True)
        intel.backlog.clear()
        ctx.live["intel"] = intel.info(character())
        for r in intel.poll(character(), now):
            if r.jumps is None or r.kind not in ("hostile", "clear"):
                continue
            where = away(r.jumps)
            print(ts(), f"INTEL {r.kind.upper()} {r.system} ({where}) [{r.channel}] {r.sender} > {r.text}",
                  flush=True)
            ctx.event("intel", report=r, where=where)
            if r.kind == "hostile":
                ctx.event("alert", text=f"INTEL hostile {where}: {r.system}")
                notify(f"INTEL: hostile {where}", f"{r.system}: {r.text}", "critical" if r.jumps <= 1 else "normal")
                if now - _intel_sound_at >= 3:          # one sound for a burst of reports
                    play(cfg["sound_intel"])
                    _intel_sound_at = now

    while not ctx.stop.is_set():
        now = time.time()
        ctx.live["kind"], ctx.live["text"] = current_status()
        ctx.live["reason"] = waiting_msg
        ctx.live["system"] = intel.here(character()) if intel else None
        if panel is None:
            ctx.live["rows"] = []
        if cfg["heartbeat_seconds"] and now - last_heartbeat >= cfg["heartbeat_seconds"]:
            heartbeat(now)
            last_heartbeat, checks = now, 0

        # Listing windows is the most expensive step on Hyprland (~40 ms), so
        # only do it every 10 s or when a capture fails.
        if win is None or time.time() - win_checked > 10:
            win, win_checked = find_window(cfg["character"], win or watched), time.time()
            if win and (watched is None or win["id"] != watched["id"]):
                print(ts(), f"Watching window '{win['title']}'", flush=True)
                panel = None          # another client: its Local is somewhere else
            watched = win or watched
        if intel:                 # after the window lookup: its pilot's intel log is the one read
            check_intel(now)
        img = capture(win) if win else None
        if img is None:
            win = None
            if waiting_msg != "nowin":
                print(ts(), "EVE window not found, waiting...", flush=True)
                waiting_msg = "nowin"
            time.sleep(2)
            continue
        now = time.time()
        checks += 1

        # Is EVE still drawing? A hidden window keeps rendering (measured ~8+
        # new frames/s), but if the picture freezes we'd be watching an old image.
        h = hashlib.md5(img[::7, ::7].tobytes()).digest()     # sparse sample: ~1 ms
        if h != frame_hash:
            frame_hash, last_new_frame = h, now
            if stale_alerted:
                print(ts(), "EVE is drawing new frames again.", flush=True)
                notify("EVE Local watch", "EVE is drawing again, watching.", "normal")
                stale_alerted = False
        elif not stale_alerted and now - last_new_frame >= cfg["stale_seconds"]:
            print(ts(), f"EVE hasn't drawn a new frame for {cfg['stale_seconds']} s - the picture may be "
                  "frozen (minimised?). Alerts can't be trusted until it updates.", flush=True)
            ctx.event("alert", text="EVE picture frozen - alerts can't be trusted")
            notify("EVE Local watch: picture frozen",
                   f"No new EVE frame for {cfg['stale_seconds']} s. Is EVE minimised?", "normal")
            stale_alerted = True

        # (Re)locate Local: at start, after a resize, or when the panel went away.
        if panel and img.shape != size:
            print(ts(), f"EVE window resized to {img.shape[1]}x{img.shape[0]}, re-finding Local...", flush=True)
            panel = None
        if panel and not panel_intact(img, panel):
            panel = None
            lost_since = lost_since or now
        if panel is None and now - last_locate >= 1.0:
            last_locate = now
            found, reason = locate(img)
            if found:
                panel, size = found, img.shape
                seen_icons.clear()
                history.clear()
                settle_until = now + 15   # ponytail: just re-laid-out; icons glitch for a few s, don't vote on them
                rows, mode = analyze(crop_panel(img, panel))
                state = summarize(rows, cfg["self_rows"])
                verb = "re-identified" if ever_found else "identified successfully"
                ever_found = True
                print(ts(), f"Local {verb} ({mode} layout): {describe(panel, state)}", flush=True)
                if lost_alerted:
                    notify("EVE Local watch", "Local found again, watching.", "normal")
                lost_since, lost_alerted, waiting_msg = None, False, None
            elif waiting_msg != reason:
                print(ts(), f"Local not found ({reason}). Keep the Local window open with its "
                      "member list showing; retrying...", flush=True)
                waiting_msg = reason

        if panel is None:
            lost_since = lost_since or now
        else:
            crop = crop_panel(img, panel)
            rows, m = analyze(crop)
            # ponytail: debounce, like the scrollbar - one odd frame mustn't flip the layout
            mode_flips = mode_flips + 1 if m != mode else 0
            if mode_flips >= 4:
                mode, mode_flips = m, 0
                seen_icons.clear()            # rows moved: old icon positions mean nothing
                print(ts(), f"Local member list switched to the {mode} layout.", flush=True)
            rows = hold_blinking(rows, seen_icons, now, BLINK_HOLD)
            state = summarize(rows, cfg["self_rows"])
            ctx.live["rows"] = [r.status for r in rows]
            # ponytail: debounce - scenery behind the panel flickers false bars for a frame or two
            sb_flips = sb_flips + 1 if has_scrollbar(crop) != scrolled else 0
            if sb_flips >= 4:
                sb_flips = 0
                scrolled = not scrolled
                if scrolled:
                    print(ts(), "WARNING: Local's member list scrolls - pilots out of view are NOT checked. "
                          "Make the member list taller.", flush=True)
                    notify("EVE Local watch: Local list scrolls",
                           "Some pilots are out of view and NOT checked. Make the member list taller.")
                else:
                    print(ts(), "Local's member list fits again - all pilots checked.", flush=True)
            if state["rows"] == 0:
                lost_since = lost_since or now      # you are always in Local; empty = wrong spot
            elif lost_since:
                lost_since = None
                if lost_alerted:
                    print(ts(), "Local member list is back.", flush=True)
                    notify("EVE Local watch", "Local member list is back, watching.", "normal")
                    lost_alerted = False

        if ctx.snap.is_set():
            ctx.snap.clear()
            snapshot("manual")

        # Lost Local for too long: warn, then repeat every repeat_seconds.
        if lost_since and now - lost_since >= cfg["lost_seconds"]:
            if not lost_alerted or now - last_alarm >= cfg["repeat_seconds"]:
                if not lost_alerted:
                    print(ts(), "LOST LOCAL - not watching! Is the Local window closed, moved or covered?",
                          flush=True)
                    notify("EVE Local watch: LOST LOCAL", "Can't see the Local member list. Not watching!")
                    ctx.event("alert", text="LOST LOCAL - not watching")
                play(cfg["sound_lost"])
                lost_alerted, last_alarm = True, now
            confirmed = None
            history.clear()
            time.sleep(cfg["interval"])
            continue
        if panel is None or lost_since:
            time.sleep(cfg["interval"])
            continue

        # Vote over the last vote_seconds: single noisy frames (weapon glows,
        # explosions behind the panel) are outvoted by the frames around them.
        if now < settle_until:
            time.sleep(cfg["interval"])
            continue
        history.append((now, state))
        while history and now - history[0][0] > cfg["vote_seconds"]:
            history.popleft()
        votes = collections.Counter(tuple(sorted(s.items())) for _, s in history)
        top, count = votes.most_common(1)[0]
        state = dict(top)
        if len(history) >= 3 and count >= cfg["vote_share"] * len(history):
            if state != confirmed and confirmed is not None and threats(state, cfg) > threats(confirmed, cfg):
                # ponytail: panel redraws can briefly blank every icon (all pilots read neutral); a real arrival persists
                if state != candidate:
                    candidate, candidate_since = state, now
                if now - candidate_since < CONFIRM_THREAT_S:
                    time.sleep(cfg["interval"])
                    continue
            if state != confirmed:
                candidate = None
                print(ts(), f"pilots={state['rows']} hostile={state['hostile']} "
                      f"neutral={state['neutral']} friendly={state['friendly']}", flush=True)
                ctx.event("local", **state)
                prev = confirmed or state
                # Neutrals are treated exactly like hostiles: same alarm, same repeat.
                grew = state["hostile"] > prev["hostile"] or threats(state, cfg) > threats(prev, cfg)
                if confirmed is not None and grew:
                    alarm(state, "in Local")
                    last_alarm = now
                elif confirmed is not None and cfg["alert_joins"] and state["rows"] > prev["rows"]:
                    play(cfg["sound_join"])
                elif confirmed is None and threats(state, cfg):
                    alarm(state, "in Local (already there when watching started)")
                    last_alarm = now
                confirmed = state
            elif cfg["repeat_seconds"] and now - last_alarm >= cfg["repeat_seconds"] and threats(state, cfg):
                play(cfg["sound_hostile"])
                last_alarm = now

        time.sleep(cfg["interval"])


def cmd_test_alerts(cfg):
    for label, sound, title, body, urgency in [
        ("join (anyone joins)", "sound_join", None, None, None),
        ("hostile or neutral", "sound_hostile", "HOSTILE in Local", "TEST: 1 hostile, 1 neutral", "critical"),
        ("lost Local", "sound_lost", "EVE Local watch: LOST LOCAL", "TEST: can't see Local", "critical"),
        ("intel hostile nearby", "sound_intel", "INTEL: hostile 2 jumps away", "TEST: P3EN-E: Some Pilot nv",
         "normal"),
    ]:
        print(f"{label}: {Path(cfg[sound]).name}", flush=True)
        play(cfg[sound])
        if title:
            notify(title, body, urgency)
        time.sleep(4)


def main():
    # A Windows console/pipe may not be UTF-8; a pilot's window title must not crash us.
    sys.stdout.reconfigure(errors="replace")
    cfg = load_config()
    args = []
    argv = iter(sys.argv[1:])
    for a in argv:
        if a in ("-c", "--character"):
            cfg["character"] = next(argv, None) or sys.exit(f"{a} needs a character name")
        elif a.startswith("--character="):
            cfg["character"] = a.split("=", 1)[1]
        elif a in ("-j", "--join-sound"):
            cfg["alert_joins"] = True
        else:
            args.append(a)
    args = args or ["run"]
    if args[0] == "debug":
        cmd_debug(cfg, args[1] if len(args) > 1 else None)
    elif args[0] == "test-alerts":
        cmd_test_alerts(cfg)
    elif args[0] == "intel":
        cmd_intel(cfg)
    elif args[0] == "run":
        try:
            cmd_run(cfg)
        except KeyboardInterrupt:
            pass
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
