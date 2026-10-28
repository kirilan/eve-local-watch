#!/usr/bin/env python3
"""Regression test: real captures (incl. space bleeding through the panel) and
synthetic hostile/neutral rows built from real pixels. Run: uv run selftest.py"""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import eve_local_watch as w

HERE = Path(__file__).resolve().parent
EXPECT = ["friendly", "blank", "friendly"]     # +10, you (no icon), blue
# Samples with a different Local
EXPECT_BY_FILE = {
    # dark portraits: name text once out-scored the portraits
    "dark_portraits.png": ["friendly", "blank", "friendly", "friendly", "friendly"],
    # real 1N-FJ8 Local: -10 / -5, grey neutral icons, corp alt (green), you (no icon);
    # row 7 is a blinking war target caught in its dark phase
    "real_20_pilots.png": ["friendly", "neutral", "neutral", "hostile", "neutral", "hostile", "blank",
                           "hostile", "neutral", "friendly", "friendly", "friendly", "hostile", "hostile",
                           "hostile", "hostile", "blank", "friendly", "hostile", "friendly"],
    # 259-pilot station Local, scrolled: busy name column once swallowed the portraits
    # (5 huge blank rows); the half-visible pilot at the bottom is not a row
    "long_local.png": ["friendly"] * 13,
    # live 2560x1600 client, smaller portraits; the same Local in compact mode
    # (no portraits, standing icon left of the name)
    "small_portraits.png": EXPECT[:1] + EXPECT[:2],
    "compact.png": ["friendly", "friendly", "blank"],
    # live false alarm: the 👤 header split at its 1 px neck (half of it read as a pilot), and
    # sizes taken from a short name made the green icon too big to count
    "compact_green.png": ["friendly", "blank"],
    # live false alarm: scenery past the list's right border read as 3 more pilots
    "compact_phantom.png": ["friendly", "friendly", "blank"],
}
SCROLLED = {"long_local.png"}
COMPACT = {"compact.png", "compact_green.png", "compact_phantom.png"}
fails = 0


def check(name, crop, expect, mode="default"):
    global fails
    rows, got_mode = w.analyze(crop)
    got = [r.status for r in rows]
    ok = got == expect and got_mode == mode
    if got_mode != mode:
        got = f"{got_mode} layout"
    fails += not ok
    print("PASS" if ok else "FAIL", name, got if ok else f"got {got}, want {expect}")


# 1. Real frames: locate Local, then classify.
for f in sorted((HERE / "samples").glob("*.png")):
    img = Image.open(f).convert("RGB")
    panel, reason = w.locate(img)
    if not panel:
        fails += 1
        print("FAIL", f.name, "locate:", reason)
        continue
    check(f.name, w.crop_panel(img, panel), EXPECT_BY_FILE.get(f.name, EXPECT),
          "compact" if f.name in COMPACT else "default")
    if w.has_scrollbar(w.crop_panel(img, panel)) != (f.name in SCROLLED):
        fails += 1
        print("FAIL", f.name, "scrollbar", "missed" if f.name in SCROLLED else "seen in a list that doesn't scroll")

# 2. Synthetic rows: recolour / remove icons of the real rows and stack them.
img = Image.open(HERE / "samples" / "clean.png").convert("RGB")
crop = w.crop_panel(img, w.locate(img)[0])
base = np.asarray(crop).copy()
rows, _ = w.analyze(crop)
hsv = np.asarray(crop.convert("HSV"))
icon = (hsv[:, :, 1] > 115) & (hsv[:, :, 2] > 70)
icon[:, : int(base.shape[1] * 0.7)] = False
bg = np.median(base[-60:].reshape(-1, 3), axis=0).astype(np.uint8)
top, bottom = rows[0].y0 - 2, rows[-1].y1 + 3


def row(i, rgb=None, strip=False):
    y0, y1 = rows[i].y0 - 2, rows[i].y1 + 3
    r, m = base[y0:y1].copy(), icon[y0:y1]
    if rgb:
        r[m] = rgb
    if strip and m.any():
        ys, xs = np.nonzero(m)
        r[ys.min():ys.max() + 1, xs.min():xs.max() + 1] = bg
    return r


def case(name, parts, expect):
    check("synthetic " + name, Image.fromarray(np.vstack([base[:top]] + parts + [base[bottom:]])), expect)


RED, ORANGE, GREEN, PURPLE, LBLUE = (170, 25, 25), (215, 110, 25), (40, 140, 40), (120, 40, 160), (60, 120, 220)
DIM_RED = (58, 8, 13)          # war target star at its dimmest visible phase (V ~58)
GREY = (110, 110, 110)         # neutral standing icon
case("red", [row(0, RED), row(1), row(2)], ["hostile", "blank", "friendly"])
case("orange", [row(0), row(1), row(2, ORANGE)], ["friendly", "blank", "hostile"])
case("no icon", [row(0), row(1), row(2, strip=True)], ["friendly", "blank", "blank"])
case("dim red", [row(0), row(1), row(2, DIM_RED)], ["friendly", "blank", "hostile"])
case("grey neutral", [row(0, GREY), row(1), row(2, GREY)], ["neutral", "blank", "neutral"])
case("mixed", [row(0), row(1), row(2, RED), row(0, strip=True), row(2, GREEN), row(0, PURPLE),
               row(2, LBLUE), row(2, strip=True), row(0, ORANGE), row(2, GREY)],
     ["friendly", "blank", "hostile", "blank", "friendly", "friendly", "friendly", "blank", "hostile", "neutral"])

# 2b. The same in compact mode: icons left of the name, rows one text line tall.
cimg = Image.open(HERE / "samples" / "compact.png").convert("RGB")
ccrop = w.crop_panel(cimg, w.locate(cimg)[0])
cbase = np.asarray(ccrop).copy()
crows, _ = w.analyze(ccrop)
chsv = np.asarray(ccrop.convert("HSV"))
cicon = (chsv[:, :, 1] > 115) & (chsv[:, :, 2] > 70)
cicon[:, crows[0].y1 - crows[0].y0 + 10:] = False      # only the icon column, left of the names
cbg = np.median(cbase[-60:].reshape(-1, 3), axis=0).astype(np.uint8)


def crow(i, rgb=None, strip=False):
    r, m = cbase[crows[i].y0:crows[i].y1].copy(), cicon[crows[i].y0:crows[i].y1]
    if rgb:
        r[m] = rgb
    if strip and m.any():
        ys, xs = np.nonzero(m)
        r[ys.min():ys.max() + 1, xs.min():xs.max() + 1] = cbg
    return r


def ccase(name, parts, expect):
    stack = [cbase[:crows[0].y0]] + parts + [cbase[crows[-1].y1:]]
    check("synthetic compact " + name, Image.fromarray(np.vstack(stack)), expect, "compact")


ccase("red / grey", [crow(0, RED), crow(1, GREY), crow(2)], ["hostile", "neutral", "blank"])
ccase("long", [crow(0), crow(1, ORANGE), crow(2), crow(0, strip=True), crow(1, DIM_RED), crow(0, GREY),
               crow(1, GREEN), crow(0, PURPLE), crow(2), crow(1, RED), crow(0)] * 3,
      ["friendly", "hostile", "blank", "blank", "hostile", "neutral", "friendly", "friendly", "blank",
       "hostile", "friendly"] * 3)

# 3. Counting: grey icons are neutrals; blank rows are neutrals except self_rows.
R = w.Row
got = w.summarize([R(0, 1, "blank"), R(1, 2, "neutral"), R(2, 3, "neutral"), R(3, 4, "hostile")], 1)
ok = got == {"rows": 4, "hostile": 1, "neutral": 2, "friendly": 0}
fails += not ok
print("PASS" if ok else "FAIL", "summarize", got)

# 4. Blinking war target: lit, dark, dark... stays hostile for BLINK_HOLD seconds, then lets go.
seen = []
lit = [R(0, 30, "friendly"), R(30, 60, "hostile", 354.0), R(60, 90, "blank")]
dark = [R(0, 30, "friendly"), R(30, 60, "blank"), R(60, 90, "blank")]
got = [[r.status for r in w.hold_blinking(rs, seen, t, w.BLINK_HOLD)]
       for t, rs in [(0, lit), (0.5, dark), (w.BLINK_HOLD - 0.1, dark), (w.BLINK_HOLD + 0.1, dark)]]
want = [["friendly", "hostile", "blank"]] * 3 + [["friendly", "blank", "blank"]]
ok = got == want
fails += not ok
print("PASS" if ok else "FAIL", "blink hold", got if ok else f"got {got}, want {want}")

# 5. Scrollbar: paint a 2 px thumb near the right edge of a real list.
sb = base.copy()
x = int(sb.shape[1] * 0.93)
sb[40:160, x:x + 2] = (140, 140, 140)
ok = w.has_scrollbar(Image.fromarray(sb)) and not w.has_scrollbar(crop)
fails += not ok
print("PASS" if ok else "FAIL", "scrollbar")

# 5b. Bright light behind the empty part of the list (explosion, structure lights),
# blurred by the panel: once read as a name line = a phantom no-icon pilot = a neutral.
cy = rows[-1].y1 + int(1.7 * (rows[-1].y1 - rows[-1].y0))     # ~2 portrait widths below
got = []
for sigma in (2, 4, 8):
    for peak in (120, 180, 240):
        m = np.zeros(base.shape[:2], np.float32)
        cv2.ellipse(m, (base.shape[1] // 2, cy), (base.shape[1] // 4, 8), 0, 0, 360, 1, -1)
        m = cv2.GaussianBlur(m, (0, 0), sigma)
        lit = np.clip(base + m[..., None] / m.max() * np.array([peak, peak, 0.9 * peak]), 0, 255)
        got.append([r.status for r in w.analyze(Image.fromarray(lit.astype(np.uint8)))[0]])
ok = cy + 20 < base.shape[0] and all(g == EXPECT for g in got)
fails += not ok
print("PASS" if ok else "FAIL", "light behind the list", "" if ok else got)

# 5c. Snapshots: files written, Local found if not given, oldest pruned past SNAPSHOT_KEEP.
with tempfile.TemporaryDirectory() as d:
    w.CONFIG_PATH, keep = Path(d) / "config.json", w.SNAPSHOT_KEEP
    w.SNAPSHOT_KEEP = 2
    for name in ("a", "b", "c"):
        snap = w.save_snapshot(np.asarray(cimg), None, w.DEFAULTS, name, ["extra line"])
    info = (snap / "info.txt").read_text(encoding="utf-8")
    left = sorted(p.name[-1] for p in snap.parent.iterdir())
    ok = ((snap / "raw.png").exists() and (snap / "annotated.png").exists() and "layout: compact" in info
          and "extra line" in info and left == ["b", "c"])
    w.SNAPSHOT_KEEP = keep
    fails += not ok
    print("PASS" if ok else "FAIL", "snapshot", "" if ok else (left, info))

# 6. Intel: fake chat logs written the way EVE writes them (UTF-16, a BOM on every line).
with tempfile.TemporaryDirectory() as d:
    def log(name, listener, channel):
        path = Path(d, name)
        head = f"\r\n\r\n  Channel Name:    {channel}\n  Listener:        {listener}\n  ----\n\n"
        path.write_bytes(("\ufeff" + head).encode("utf-16-le"))
        return path

    def say(path, sender, text, end="\r\n", ago=0):
        t = datetime.now(timezone.utc) - timedelta(seconds=ago)
        with path.open("ab") as f:
            f.write(f"\ufeff[ {t:%Y.%m.%d %H:%M:%S} ] {sender} > {text}{end}".encode("utf-16-le"))

    local = log("Local_20261002_161039_1.txt", "Me", "Local")
    say(local, "EVE System", "Channel changed to Local : 4-HWWF")
    intel_a = log("wc.Vale+Tr+Ge_20261002_161039_1.txt", "Me", "wc.Vale+Tr+Ge")
    intel_b = log("wc.Vale+Tr+Ge_20261002_161038_2.txt", "My Alt", "wc.Vale+Tr+Ge")
    say(intel_a, "Old", "YMJG-4  before we started")
    intel = w.Intel(dict(w.DEFAULTS, intel_channels=["wc.vale+tr+ge"], chatlog_dir=d))

    def poll():
        intel.last_scan = float("-inf")
        return sorted(((r.kind, r.system, r.jumps) for r in intel.poll("Me", 0)), key=str)

    steps = [("startup: history skipped", poll(), [])]
    say(intel_a, "Scout", "PM-DWE*  Anulos flycatcher")
    say(intel_b, "Scout", "PM-DWE*  Anulos flycatcher")                 # same message, other character
    say(intel_a, "Far", "H-5GUI  Someone")
    say(intel_a, "Late", "DAYP-G  stale", ago=w.INTEL_MAX_AGE + 60)
    say(intel_a, "Scout", "YMJG-4 cl", end="")                            # EVE mid-way through a line
    steps.append(("hostile 1 jump, once", poll(), [("hostile", "H-5GUI", None), ("hostile", "PM-DWE", 1)]))
    with intel_a.open("ab") as f:
        f.write("r\r\n".encode("utf-16-le"))                              # ...and the rest of it
    say(intel_a, "Ask", "YMJG-4 status?")
    steps.append(("clear / status", poll(), [("ask", "YMJG-4", 1), ("clear", "YMJG-4", 1)]))
    say(local, "EVE System", "Channel changed to Local : PM-DWE")
    say(intel_a, "Scout", "PM-DWE*  Anulos  in local")
    steps.append(("moved", poll(), [("hostile", "PM-DWE", 0)]))
    steps.append(("heartbeat summary", [intel.summary(), intel.summary()],
                  ["wc.Vale+Tr+Ge: 6 msgs (2 hostile in range, 1 clear, 1 out of range, 2 other), last 0 min ago",
                   "wc.Vale+Tr+Ge: 0 msgs (0 hostile in range, 0 clear, 0 out of range, 0 other), last 0 min ago"]))
    # Only Me's log is read; My Alt's takes over once Me's stops being written.
    steps.append(("one log per channel", sorted(l.who() for l in intel.logs.values()
                                                if intel.primary.get(l.channel.lower()) == l.path), ["Me"]))
    old = intel_a.stat().st_mtime - w.PRIMARY_STALE - 10
    os.utime(intel_a, (old, old))                                         # Me logged out...
    say(intel_b, "Scout", "PM-DWE*  Anulos  in local")                    # ...seen already, from Me's log
    say(intel_b, "Scout2", "YMJG-4  Someone else")
    steps.append(("standby takes over", poll(), [("hostile", "YMJG-4", 2)]))
    # Startup recap: the latest report per system in range from the last INTEL_RECAP seconds, once.
    say(intel_b, "Old", "H-5GUI*  ancient", ago=w.INTEL_RECAP + 60)
    say(intel_b, "Scout", "YMJG-4 clr", ago=60)
    rec, recapped = w.Intel(dict(w.DEFAULTS, intel_channels=["wc.vale+tr+ge"], chatlog_dir=d), recap=True), []
    rec.recap = lambda *a, f=rec.recap: (out := f(*a), recapped.extend((r.kind, r.system, r.jumps) for r in out))[0]
    for _ in range(2):
        rec.last_scan = float("-inf")
        rec.poll("Me", 0)
    steps.append(("startup recap", recapped, [("hostile", "PM-DWE", 0), ("clear", "YMJG-4", 2)]))
    near = intel.nearby("4-HWWF")
    steps.append(("shorthand", [intel.classify(None, "", "", t, near).system for t in
                                ["to IPAY", "ipay", "CLR", "Jita"]], ["IPAY-2", None, None, "Jita"]))
    # Auto-detect: a channel full of system names is intel; chat is not, nor is one too quiet to tell.
    feed = log("North.Feed_20261002_161039_1.txt", "Me", "North.Feed")
    corp = log("Corp_20261002_161039_1.txt", "Me", "Corp")
    for i in range(25):
        say(feed, "Scout", ["PM-DWE*  Anulos", "YMJG-4 clr", "anyone know a good fit?"][i % 3], ago=3600)
        say(corp, "Mate", ["o7", "undock for the roam in 4-HWWF", "brb"][i % 3], ago=3600)
    auto = w.Intel(dict(w.DEFAULTS, chatlog_dir=d))
    auto.poll("Me", 0)
    steps.append(("auto-detect", auto.names, ["North.Feed"]))
    for name, got, want in steps:
        fails += got != want
        print("PASS" if got == want else "FAIL", "intel", name, got if got == want else f"got {got}, want {want}")

print("all passed" if not fails else f"{fails} FAILED")
raise SystemExit(bool(fails))
