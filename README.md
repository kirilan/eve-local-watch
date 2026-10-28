# eve-local-watch

Watches the **Local** chat member list in EVE Online and alerts you when a
hostile or neutral pilot enters system. Built for Hyprland (Omarchy) with the
game running through Steam/Proton; also runs on **Windows** (see
[Windows](#windows)). A **terminal UI** (see [Terminal UI](#terminal-ui)) gives you
a live dashboard on both.

It only *looks* at the screen: it captures the EVE window, reads the pixels and
plays a sound / shows a notification. It never reads game memory, sends input
or talks to the client. (Screen-reading tools are still a grey area in CCP's
third-party policy — use at your own discretion.)

- Works while EVE is on a **hidden workspace** / **covered by other windows** (captures the window itself, not the screen)
- Finds the Local panel **automatically on start**, and again if you move it
- Warns if it **loses Local** (closed, covered, moved off)
- About **1.8 checks per second**, ~5% of one CPU core
- Alerts typically fire **~1–2 s** after a pilot appears
- **Terminal UI**: a dashboard with a status banner, live Local strip, intel
  table, alert history and start/stop keys (`uv run python tui.py`, or
  `EveLocalWatch.exe` on Windows)
- **Intel**: finds your intel channel in the chat logs and warns when a
  hostile is reported **within 5 jumps** (see [Intel](#intel-channel))

## Quick start

Easiest: the [terminal UI](#terminal-ui) — `uv run python tui.py` (any OS), or
double-click/run `EveLocalWatch.exe` from the
[Releases](https://github.com/kirilan/eve-local-watch/releases) page on Windows,
then press `s`. Or use the plain command line:

```sh
eve-local-watch                # find Local and start watching (Ctrl+C to stop)
eve-local-watch test-alerts    # hear every alert sound + see its notification
eve-local-watch debug          # analyse one frame, print it, save an annotated image
eve-local-watch intel          # show your system and how recent intel is read
eve-local-watch -c "Pilot Name"  # watch only the "EVE - Pilot Name" client
eve-local-watch -j             # also a soft sound when anyone (incl. blues) joins
```

On start you should see:

```
12:44:07 Looking for Local in 'any EVE window'...
12:44:07 Local identified successfully: member list at x=407 y=809 size 186x494px (window pixels); 3 pilots: 2 friendly, 0 neutral, 0 hostile
12:44:08 pilots=3 hostile=0 neutral=0 friendly=2
```

After that it prints a line when the contents of Local change, plus a
**heartbeat every 2 minutes** so you can tell a quiet console means "all
clear", not "stuck":

```
12:46:08 HEARTBEAT watching: pilots=3 hostile=0 neutral=0 friendly=2 | 214 checks in the last 2 min, EVE last drew a new frame 0 s ago
```

If the watcher can't do its job, the heartbeat says `NOT WATCHING - …` with
the reason (EVE window not found, EVE picture frozen, Local not visible).
The check count should be ~210 per 2 min; far fewer means something is slow.

## Terminal UI

A full-screen dashboard in your terminal, on top of the same watcher. Run it:

```sh
uv run python tui.py        # Linux, Windows, anywhere with Python 3.12+ and uv
EveLocalWatch.exe           # Windows: download from the Releases page, no Python needed
```

(Any argument, e.g. `EveLocalWatch.exe -c "Pilot Name"`, `debug` or `intel`,
runs the plain command-line tool instead. The exe is unsigned, so Windows
SmartScreen may warn the first time.)

Press `s` to start watching. The screen shows:

- **Status banner**: STOPPED / NOT WATCHING / ALL CLEAR / THREAT IN LOCAL (the
  worst state across all watched clients), plus mute and neutral-alert state.
  It replaces the 2-minute heartbeat.
- **Clients table**: one row per watched EVE client with status, pilots,
  hostile / neutral / friendly counts and the pilot's current system.
- **Local strip**: one block per member-list row (red hostile, yellow flagged,
  white neutral, green friendly). Standings only; pilot names are not read. If
  Local can't be found it tells you what to do ("Open the Local chat window and
  click its Local tab", ...).
- **Intel panel and table**: which channel is followed, your system, how many
  systems are in range and how many messages were read, or what is wrong ("No
  chat log for X: open the channel in EVE with 'Log chat to file' on"). The table
  lists hostile/clear reports in range, including the startup recap.
- **Alerts and Log tabs**: history of alerts and Local changes, and the raw log.

| Key | Does |
|---|---|
| `s` | Start / stop watching |
| `w` | Pick which EVE client(s) to watch; several at once. Space ticks, Enter confirms; none ticked = any EVE window |
| `i` | Pick the intel channel(s) to read; none ticked = auto-detect (default). Saved to the config |
| `m` | Mute sounds (toasts still show) |
| `c` | Settings: alert on neutrals, join sound, repeat interval, intel range. Applied live and saved to the config |
| `d` | Snapshot: save what each watcher sees right now to `snapshots\` in the config folder (see *Troubleshooting*) |
| `t` | Play every alert sound and toast |
| `q` | Quit |

Build the exe yourself with:

```sh
pip install pyinstaller textual
pyinstaller --onefile --name EveLocalWatch --collect-submodules textual.widgets --add-data "templates;templates" --add-data "data;data" tui.py
```

Headless self-check of the UI (no EVE needed): `python selftest_tui.py`.

## Windows

Tested on Windows 11 (capture, Local detection, sounds and toasts verified in game). What differs:

| | Linux (Hyprland) | Windows |
|---|---|---|
| Capture | `grim -T` (per-window capture) | Win32 `PrintWindow` with full-content rendering (built into Python via `ctypes`, nothing to install) — works while EVE is covered, not while minimised |
| Sounds | `pw-play`, `.oga` from `/usr/share/sounds/freedesktop/stereo/` | `winsound`, `.wav` only; defaults from `C:\Windows\Media` (`Alarm01.wav`, `Windows Notify System Generic.wav`, `Windows Critical Stop.wav`) |
| Notifications | `notify-send` | Windows toast via the built-in Windows PowerShell; alarms use "reminder" toasts that stay until dismissed |
| Config | `~/.config/eve-local-watch/` | `%APPDATA%\eve-local-watch\` |
| Intel chat logs | Inside the Proton prefix: `<steam library>/steamapps/compatdata/8500/pfx/drive_c/users/steamuser/Documents/EVE/logs/Chatlogs` | `Documents\EVE\logs\Chatlogs`, wherever Documents really is (read from the registry, so OneDrive / moved folders work) |

Setup: install [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`),
get this folder, then in it:

```bat
uv run eve_local_watch.py test-alerts   :: hear/see the alerts
uv run eve_local_watch.py debug         :: check it finds Local
uv run eve_local_watch.py               :: watch
```

If `debug` says the picture is **all black**, `PrintWindow` can't see the
EVE client on that machine (some DirectX setups); that needs a different
capture method — report it. Check `debug`'s annotated image before trusting
it: `%APPDATA%\eve-local-watch\debug.png`.

## In-game requirements

- **Local in its own chat window** (not a background tab of another window).
- **Member list visible**, with portraits or in compact mode (detected
  automatically, also when you switch while watching).
- **Dark, opaque window background.** On a see-through background the "Local"
  label can't be found reliably. Even "opaque" is slightly see-through — that is
  handled (see *Noise handling*).
- **Don't minimise the EVE client.** A hidden workspace (or on Windows, being
  covered by other windows) is fine; a minimised client stops drawing.
- Only pilots **visible** in the list are checked. If the list gets a
  scrollbar you get a warning ("Local list scrolls", and the heartbeat says
  `PARTLY watching`) — make the member list taller.
- **One EVE window is watched.** With several clients open it takes the
  first `EVE - …` window it finds and **sticks with it** until that client
  closes (it prints `Watching window 'EVE - …'`). To choose, set `character`
  in the config or pass `-c "Pilot Name"` / `--character "Pilot Name"`.

## Alerts

Sounds and notifications below are the Linux ones; see [Windows](#windows) for its defaults.

| Event | Sound | Notification |
|---|---|---|
| Anyone joins (incl. blues) — only with `-j` / `alert_joins` | `message-new-instant.oga` (soft) | — |
| New hostile (red / orange) **or neutral / flagged** | `alarm-clock-elapsed.oga` | "HOSTILE in Local" / "NEUTRAL in Local" (critical) |
| Local not clear when watching starts | same alarm | "… (already there when watching started)" |
| Can't see Local for 5 s | `service-logout.oga` | "LOST LOCAL — not watching!" |
| Local found again | — | "Local found again" (normal) |
| EVE hasn't drawn a new frame for 10 s | — | "picture frozen" (normal) — the watcher may be looking at an old image |
| EVE drawing again | — | "EVE is drawing again" (normal) |
| Intel: hostile reported ≤ `intel_jumps` away | `dialog-warning.oga` (at most every 3 s) | "INTEL: hostile 2 jumps away" + the message (critical at 0–1 jumps, normal further out) |

Neutrals are treated exactly like hostiles — same alarm, same urgency, same
repeat; the notification only names which one it is. While hostiles/neutrals remain (or Local stays lost) the sound repeats every
30 s. Sounds are from `/usr/share/sounds/freedesktop/stereo/` and played with
`pw-play`; notifications use `notify-send`.

## Configuration

`~/.config/eve-local-watch/config.json` (Windows: `%APPDATA%\eve-local-watch\config.json`) — any key left out uses the default.

| Key | Default | Meaning |
|---|---|---|
| `character` | `""` | Watch `EVE - <character>`; empty = the first EVE window found, kept until it closes. `-c` / `--character` on the command line overrides it |
| `interval` | `0.5` | Pause between checks (seconds) |
| `vote_seconds` | `2.0` | How much recent history the vote covers |
| `vote_share` | `0.6` | Share of frames in that history that must agree before acting |
| `self_rows` | `1` | Rows with no standing icon at all that are you / your alts (not alerted) |
| `alert_neutrals` | `true` | Neutrals trigger the same alarm as hostiles (`false` = ignore neutrals) |
| `alert_joins` | `false` | Soft sound when anyone joins. `-j` / `--join-sound` on the command line turns it on |
| `repeat_seconds` | `30` | Repeat interval for ongoing alarms (0 = never) |
| `lost_seconds` | `5` | Grace period before "LOST LOCAL" (covers gate-jump reloads) |
| `stale_seconds` | `10` | Warn if EVE hasn't drawn a new frame for this long |
| `heartbeat_seconds` | `120` | Print a status line this often (0 = off) |
| `snapshot_alerts` | `true` | Save a snapshot of the frame behind every Local alarm (last 30 kept, a few MB each) |
| `intel_channels` | `[]` | Chat channels to read intel from, e.g. `["wc.Vale+Tr+Ge"]`; `[]` = auto-detect, `false` = intel off |
| `intel_jumps` | `5` | Alert on hostiles reported this many jumps away or closer |
| `chatlog_dir` | `""` | EVE's `Chatlogs` folder; empty = found automatically |
| `jump_bridges` | `[]` | Extra connections counted as one jump, e.g. `[["4-HWWF", "P3EN-E"]]` (Ansiblexes) |
| `sound_hostile`, `sound_join`, `sound_lost`, `sound_intel` | see above | Any `.oga` / `.ogg` / `.wav` path (Windows: `.wav` only) |

Longer `vote_seconds` or higher `vote_share` → fewer false alerts, slower warnings.

## Intel channel

The watcher also follows EVE's chat logs for intel. Needs
**Settings → Chat → "Log chat to file"** on in EVE (the files are in
`Documents/EVE/logs/Chatlogs`; under Proton that is inside
`steamapps/compatdata/8500/pfx/…`, found automatically in any Steam library).

- **Which channel:** with the default `"intel_channels": []` it is
  auto-detected: every channel logged in the last 3 days whose messages
  mostly (≥ 50 %, at least 20 messages) name a system. An intel channel
  scores ~80 %, corp / alliance / fleet chat ~10 %. If none qualifies yet it
  looks again every minute. Set `intel_channels` to pick the channels
  yourself, or `false` to turn intel off.

- **Where you are:** from the watched character's `Local` log
  (`EVE System > Channel changed to Local : 4-HWWF`), so it follows you
  through gates. The character is the one in the watched window's title.
- **Distances:** jumps through stargates (`data/eve_map.json`, plus your
  `jump_bridges`), counted fresh whenever you change system.
- **Reading a message:** systems are found by name; linked systems (they
  end in `*` in the log) win over plain words. If no full name is found, an
  ALL-CAPS short form like `IPAY` counts if it starts exactly one system in
  range. The nearest system named is the one reported.
  - contains `clr` / `clear` → printed as clear, no alarm
  - only `status` / `status?` besides the system → a question, ignored
  - anything else (pilots, ships, `nv`, a bare system name) → **hostile**
- Only new messages count: history from before the watcher started, and
  messages more than 5 minutes old, are skipped. A message logged by several of
  your characters alerts once.
- English client only (it reads the `Channel changed to Local` text).

On start (and whenever EVE opens a new log file) the console names each log
being followed, with the channel in bold, or says a configured channel has
no log yet (channel not open, or logging off). The heartbeat adds what was
read since the last one:

```
12:46:08 HEARTBEAT watching: … | intel: in 4-HWWF, 37 systems within 5 jumps | wc.Vale+Tr+Ge: 14 msgs (2 hostile in range, 1 clear, 9 out of range, 2 other), last 1 min ago
```

`other` covers questions, messages naming no system, and stale messages.

Once a channel is identified at startup, the console also sums up its last
30 minutes, once and with no sound or notification: the message count, plus
the latest report for each system in range, nearest first:

```
12:40:02 Intel: wc.Vale+Tr+Ge last 30 min: 41 msgs, 2 systems within 5 jumps reported (1 hostile at last report):
12:40:02   HOSTILE PM-DWE (1 jump away, 4 min ago) Scout > PM-DWE*  Anulos flycatcher
12:40:02   CLEAR YMJG-4 (2 jumps away, 12 min ago) Scout > YMJG-4 clr
```

Only the log currently being read is summed up, so right after logging in
(EVE starts a new log file) there is little or nothing to report.

`eve-local-watch intel` prints the log folder, the auto-detect score of each
channel (when `intel_channels` isn't set), each character's system, and
the last 25 intel messages as the watcher reads them (kind, system, jumps,
`ALERT`). Run it to check a channel name or a message that was misread.

The map is built from CCP's static data export; refresh it after an
expansion changes the map with `uv run tools/build_map.py`.

## Libraries and models

**No machine-learning models, no neural networks, no OCR, no training data.**
It is all classical image processing: fixed rules about brightness, colour,
edges and shapes, tuned on real captures from this client. It never reads
pilot names — only portraits, text presence and icon colour.

| Library | Used for |
|---|---|
| **grim** (system tool) | Linux: capturing the EVE window via Hyprland's per-window capture (`-T`) |
| **ctypes** (Python built-in) | Windows: listing windows and capturing EVE with `PrintWindow` |
| **NumPy** | All the pixel maths: brightness jumps (edges), masks, row/column projections, the divider-line check, hue averaging |
| **OpenCV** (`opencv-python-headless`) | Template matching to find the "Local" label (`matchTemplate`), connected components to find icon blobs (`connectedComponentsWithStats`), colour conversions and resizing while locating. Limited to 1 thread — the images are tiny |
| **Pillow** | Cropping the member list, RGB→HSV conversion, drawing the `debug` image |

The intel feature uses only the standard library (plain text matching).
The only "learned" asset is `templates/local_tab.png`, a 37×18 px picture of
the "Local" label cut from a real capture.

## How it works

### 1. Capturing (every ~0.55 s)

| Step | Time | What happens |
|---|---|---|
| Find window | every 10 s only | `hyprctl clients -j` → window whose title starts with `EVE - ` → its ID (Windows: `EnumWindows`). Surprisingly expensive on Hyprland (~40 ms CPU), so it's cached and refreshed every 10 s or when a capture fails; the same window is kept while it exists |
| Capture | ~11 ms CPU | `grim -T <id> -t ppm` → full EVE window (2018×2083 px at 1.6 scaling), raw, into `$XDG_RUNTIME_DIR/eve-local-watch.ppm` (tmpfs = RAM, overwritten every frame, never touches the disk). The bytes are wrapped as a NumPy array without decoding or copying the ~12 MB frame |
| Local still there? | <1 ms | Checks the 1 px divider line at the member list's left edge over its full height; if <80% of it is still a line, Local moved → re-locate |
| Read member list | ~14 ms CPU | Crops just the member list (≈186×494 px) and analyses it (below) |
| Vote | — | Adds the result to the last `vote_seconds` of results and acts only on a clear majority |

`grim -T` uses Hyprland's per-window capture, which renders the window even
when its workspace isn't shown. Tested: frames keep changing (drones fighting,
ship orbiting) while you're on another workspace.

### 2. Finding Local (on start, after a resize, or when lost — ~100 ms)

1. **Find the "Local" label.** `templates/local_tab.png` (the white "Local"
   tab text, cut from a real capture) is matched against the whole window with
   OpenCV template matching (`TM_CCOEFF_NORMED`, score ≥ 0.75), at 70–140%
   size in case the UI scale changes. Matches on coloured text are skipped, e.g.
   the red "Channel changed to Local" system message.
2. **Measure the panel from its border lines.** EVE draws 1 px lines slightly
   brighter than the background. A pixel is part of a line if it is brighter
   than both neighbours across the line (R+G+B difference > 25).
   - **top:** first horizontal line below the label (header separator)
   - **left:** first vertical line to the right that runs down ≥ 40 px (divider between messages and member list)
   - **bottom:** where that divider ends (line above the input box)
   - **right:** where the header separator ends
3. The member list is the box inside those four lines.

Lines are always compared with the pixels right next to them, so a changing
background behind the panel doesn't move them.

### 3. Reading the member list

1. **Portrait column.** Mark pixels where brightness jumps by more than 40
   (of 255) from the next pixel. Portraits are full of such edges. The
   **leftmost** busy column in the left 45% of the list is the portraits,
   widened out to the empty gap before the names. (Not the *busiest* column:
   with dark portraits, the name text can score just as high.)
2. **Pilot rows — from the names.** Every pilot has a name in bright white
   text (brightness > 150, low saturation) right of the portrait; space
   showing through never gets that bright. Each line of name text is one
   pilot; the row spans ±0.6 portrait heights around it (split halfway between
   neighbours). Text near the top of the list (the "👤 N" header) is skipped.
   Portraits aren't used for rows: dark portraits have too little detail, and
   a row cut short can push its icon outside the row → a blue read as neutral.
3. **Standing icon.** Only in the **right-most 30%** of the list, where the
   icon sits, never over portraits (so red hair can't count):
   - keep strongly coloured pixels (saturation > 59%, brightness > 16% — low,
     because a war target's blinking star is dim for part of each blink)
   - split them into colour families (red/orange, yellow, green, blue, purple)
   - join touching pixels of the same family into blobs (OpenCV connected components)
   - a blob is an icon only if it is ¼–¾ of a portrait wide and tall, roughly
     square (aspect 0.75–1.33) and ≥ 60% solid. A real icon is 15×15 px and
     84% solid.
   - separately, find **grey** squares (saturation < 20%, brightness 31–67%):
     EVE's neutral-standing icon. White name text is far brighter.
4. **Colour → standing.** Circular average of the icon's hue (so reds on both
   sides of 0° average correctly):

   | Hue | Colour | Result |
   |---|---|---|
   | 340–45° | red / orange | **hostile** (−10 / −5, war target, criminal) |
   | 45–70° | yellow | **flagged**, alerts like a neutral |
   | 70–170° | green | friendly (corp) |
   | 170–255° | blue | friendly (+5 / +10 / alliance) |
   | 255–340° | purple | friendly (fleet) |
   | grey icon | — | **neutral** |
   | no icon | — | **neutral** (EVE can hide neutral icons); the first `self_rows` of these are assumed to be you |

**Compact mode** (no portraits, the icon just left of the name): after step 1,
the busy column's height is compared to the "👤 N" header line, which is the
same size in both layouts. A portrait is ≥ 2.1 header lines tall; in compact
mode the column holds icons and names, ~1.2 lines tall (cut-off 1.7). Then each
line of name text is a pilot, and the icon is looked for left of the names
(within 2 line heights of the list's edge), with the same colour rules.

5. **Blinking icons.** A war target's red star blinks about once a second and
   is fully dark in over half the frames. A row that reads "no icon" but
   showed a hostile/flagged icon at the same spot in the last 2.5 s keeps that
   status, so the vote doesn't flip-flop and swallow the alert.
6. **Scrollbar.** A thin bright vertical bar near the list's right edge means
   pilots are out of view → warning.
7. **Result:** `{pilots, hostile, neutral, friendly}` — this is what gets voted on.

### 4. Noise handling

The Local window is slightly transparent even on the dark setting, so space
effects behind it — weapon beams, explosions, gas clouds — leak through. Early
versions read these as extra pilots and orange glows as hostile icons. What
fixed it:

| Show-through | Real content | Used by |
|---|---|---|
| Max brightness ≈ 79 | Name text > 150 | row detection |
| Blurry, few sharp edges (detail ≤ 9) | Portraits full of sharp edges (38–51) | portrait column |
| Glows ≈ 56×100 px, ~60% solid, saturation ≈ 130 | Icons 15×15 px, 84% solid, saturation ≈ 210 | icon detection |
| Glow touching an icon has a different colour | Icons are opaque and one colour | colour families |
| — | War target star blinks dark ~60% of frames | 2.5 s hold per row |
| Lasts a frame or two | Pilots stay | 2 s majority vote |

Measured live during combat: **120/120 frames correct** before voting,
no false alerts in a 90 s run of the full loop.

### 5. Frozen picture

A hidden EVE window keeps rendering: measured over 2 minutes on another
workspace, every capture (~8/s, the capture rate — EVE may draw faster) was a
new frame, never a gap. GPU use still drops, because a hidden window draws
far less often than your full frame rate. Just in case, every capture's
sparse sample (every 7th pixel, ~1 ms) is hashed; if it hasn't changed for
`stale_seconds` you get a *normal* notification, since a genuinely still
picture (e.g. docked in some stations) can also trigger it. Minimising EVE
does stop it drawing.

### 6. Lost Local

Local counts as lost when the divider check fails and it can't be found
again, or the list reads 0 pilots (you are always in Local). Re-locating is
tried at most once a second. After `lost_seconds` (5 s) you get the
"LOST LOCAL" alarm. The 5 s grace covers the list reloading during gate jumps.

## Troubleshooting

| Symptom | Try |
|---|---|
| `Local not found (no 'Local' tab found)` | Local open in its own window, dark background, not minimised. `eve-local-watch debug` shows what it sees. |
| `no member list divider` | The member list is hidden — show it in the Local window. |
| False alert / missed pilot | Every Local alarm saves a snapshot automatically, and `d` in the terminal UI saves one on demand: a folder under `snapshots/` in the config folder with `raw.png` (the whole EVE window: copy it into `samples/` for a regression test), `annotated.png` and `info.txt` (each row's status and hue, plus the readings of the last few seconds that were voted on). Or run `eve-local-watch debug` **while it's happening**; it prints each row's status and hue, and saves `~/.config/eve-local-watch/debug.png` (green = "Local" label, magenta = member list, row boxes: red hostile, yellow flagged, cyan friendly, white neutral, grey no icon). A blinking war target may show as "no icon" in a single `debug` frame; the watcher itself holds it. |
| Your alts in the same system trigger neutrals | Set `self_rows` to the number of rows that are you. |
| Watching the wrong character / several clients open | Set `character` to the name in the window title, e.g. `"Pilot Name"`. |

## Files

| Path | What |
|---|---|
| `eve_local_watch.py` | The program (commands: `run`, `test-alerts`, `debug [PNG]`, `intel`) |
| `templates/local_tab.png` | Picture of the "Local" tab label used to find the panel |
| `data/eve_map.json` | Stargate map for intel distances (systems + gate pairs) |
| `tools/build_map.py` | Rebuilds `data/eve_map.json` from CCP's static data |
| `selftest.py` | Regression test (`uv run selftest.py`) |
| `samples/` | Real window captures: `clean.png`, `bleed_*.png` (space showing through), `glow_*.png` (weapon glows), `dark_portraits.png` (5 pilots, dark portraits), `real_20_pilots.png` (real hostiles, grey neutrals, blinking war target) |
| `~/.local/bin/eve-local-watch` | Launcher: `uv run` inside this project |
| `~/.config/eve-local-watch/config.json` | Settings |
| `~/.config/eve-local-watch/debug.png` | Output of `debug` |

Dependencies (managed by `uv`, Python ≥ 3.12): `numpy`, `pillow`,
`opencv-python-headless`. System tools on Linux: `grim` (≥ 1.5, for `-T`), `hyprctl`,
`pw-play`, `notify-send`.

## Testing

```sh
uv run selftest.py
```

Checks that every capture in `samples/` is located and reads as
friendly / neutral (you) / friendly. It also builds fake rows from real pixels
(icons recoloured red, orange, green, purple, light blue, or removed) and
checks they classify correctly, including a 9-pilot mixed list. It also
writes fake chat logs the way EVE does (UTF-16, half-written lines, the same
message from two characters) and checks the intel alerts. Add new
problem captures to `samples/` so they stay fixed.

Verified on a real 20-pilot Local: red −10 (hue ≈ 358°), orange −5 (≈ 20°),
blinking war target (red star, V 0–110), grey neutral icons, blue and green
friendlies. `eve-local-watch debug` also saves the raw frame as
`~/.config/eve-local-watch/debug-raw.png` — copy it into `samples/` to keep a
problem case fixed.
