"""Terminal UI for eve-local-watch: status banner, Local strip, intel table, alert history,
live settings, several EVE clients at once. Works the same on Windows and Linux."""
import io
import sys
import threading
import time
from datetime import datetime, timezone

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, RichLog, SelectionList,
                             Static, Switch, TabbedContent, TabPane)

import eve_local_watch as elw

LOOK = {"stopped": ("STOPPED", "#555555"), "off": ("NOT WATCHING", "#b8650a"),
        "ok": ("ALL CLEAR", "#1f7a1f"), "alert": ("THREAT IN LOCAL", "#c01818")}
SEVERITY = ["stopped", "ok", "off", "alert"]            # worst wins in the banner
BLOCK = {"hostile": "red", "flagged": "yellow", "friendly": "green", "neutral": "white", "blank": "grey37"}
HINTS = [("EVE window not found", "Start EVE and log in; the window title must be 'EVE - <pilot>'."),
         ("no 'Local' tab", "Open the Local chat window and click its Local tab."),
         ("member list", "Show Local's member list (the pilot list, not just the chat)."),
         ("too small", "Make the Local window bigger."),
         ("too close", "Move the Local window up, away from the bottom of the screen."),
         ("all black", "EVE is minimised or can't be captured; restore it."),
         ("frozen", "EVE stopped drawing; restore the window if it is minimised."),
         ("Local not visible", "Open Local with its member list showing, or restore the window.")]


def hint(text):
    return next((h for key, h in HINTS if key in text), "")


class LogOut(io.TextIOBase):
    """Stands in for sys.stdout: watcher threads print, the app shows lines (prefixed per client)."""

    def __init__(self, app):
        self.app, self.bufs = app, {}

    def write(self, s):
        name = threading.current_thread().name
        *lines, self.bufs[name] = (self.bufs.get(name, "") + s).split("\n")
        prefix = f"[{name}] " if name not in ("", "MainThread") and len(self.app.clients) > 1 else ""
        for line in lines:
            self.app.call_from_thread(self.app.query_one("#log", RichLog).write, prefix + line)
        return len(s)


class Client:
    def __init__(self, name, cfg):
        self.name = name
        self.cfg = dict(cfg, character=name)
        self.ctx = elw.Ctx()
        self.thread = threading.Thread(target=self.run, name=name or "any", daemon=True)

    def run(self):
        try:
            elw.cmd_run(self.cfg, self.ctx)
        except Exception as e:                  # show it instead of dying silently
            print(f"ERROR: {e!r}", flush=True)
            self.ctx.live["kind"], self.ctx.live["text"] = "off", f"crashed: {e!r}"

    @property
    def alive(self):
        return self.thread.is_alive()

    @property
    def kind(self):
        return self.ctx.live.get("kind", "off") if self.alive else "stopped"


class Pick(ModalScreen):
    """Tick the EVE clients to watch (none ticked = whichever window is found first)."""
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"), Binding("enter", "ok", "Watch selected", priority=True)]
    CSS = "Pick {align: center middle} #box {width: 54; height: auto; border: round white; padding: 1}"

    def __init__(self, chosen):
        super().__init__()
        self.chosen = chosen

    def compose(self):
        titles = sorted({w["title"][len("EVE - "):] for w in elw.list_eve_windows()
                         if w.get("title", "").startswith("EVE - ") and len(w["title"]) > 6})
        with Vertical(id="box"):
            yield Label("Space = tick a client, Enter = confirm. None ticked = any EVE window.")
            yield SelectionList(*[(t, t, t in self.chosen) for t in titles])

    def action_ok(self):
        self.dismiss(list(self.query_one(SelectionList).selected))


OFF = "— intel off —"


class PickChannels(ModalScreen):
    """Choose the intel channels to read (none ticked = auto-detect, the default)."""
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"), Binding("enter", "ok", "Use selected", priority=True)]
    CSS = "PickChannels {align: center middle} #box {width: 60; height: auto; border: round white; padding: 1}"

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def compose(self):
        cur = self.cfg["intel_channels"]
        chans = elw.list_channels(self.cfg)
        names = [c for c in (cur or []) if c not in chans] + chans
        with Vertical(id="box"):
            yield Label("Space = tick, Enter = confirm. None ticked = auto-detect (default).")
            if not names:
                yield Label("No chat logs found: open the intel channel in EVE with 'Log chat to file' on.")
            yield SelectionList((OFF, OFF, cur is False), *[(n, n, n in (cur or [])) for n in names])

    def action_ok(self):
        picked = list(self.query_one(SelectionList).selected)
        self.dismiss(False if OFF in picked else picked)


def intel_line(c):
    """(text, style) saying what intel is doing for client `c`, and what to fix if it is not working."""
    if c.cfg["intel_channels"] is False:
        return "Intel is off (press i to choose a channel)", "grey62"
    i = c.ctx.live.get("intel")
    if not c.alive or not i:
        return "Intel: not started", "grey62"
    if not i["dir"]:
        return "Chat log folder not found - set chatlog_dir in the config.", "yellow"
    if not i["names"]:
        return "No intel channel detected yet. Press i to pick one, or keep an intel channel open.", "yellow"
    lost = [n for n in i["names"] if n.lower() not in {f.lower() for f in i["found"]}]
    if lost:
        return (f"No chat log for {', '.join(lost)}: open the channel in EVE with 'Log chat to file' on "
                "(or press i to pick another).", "yellow")
    ago = ""
    if i["last"]:
        ago = f", last {(datetime.now(timezone.utc) - i['last']).total_seconds() / 60:.0f} min ago"
    where = (f"you are in {i['system']} ({i['near']} systems within {i['jumps']} jumps)" if i["system"]
             else "your system is unknown (distances not checked)")
    return f"Following {', '.join(i['found'])} | {where} | {i['total']} msgs read{ago}", "green"


class Settings(ModalScreen):
    """Live settings: applied to every watcher at once and saved to the config file."""
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    CSS = ("Settings {align: center middle} #box {width: 50; height: auto; border: round white; padding: 1}"
           " Horizontal {height: 3} Label {width: 30; padding-top: 1}")

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def compose(self):
        c = self.cfg
        with Vertical(id="box"):
            with Horizontal():
                yield Label("Alert on neutrals")
                yield Switch(c["alert_neutrals"], id="alert_neutrals")
            with Horizontal():
                yield Label("Join sound (anyone joins)")
                yield Switch(c["alert_joins"], id="alert_joins")
            with Horizontal():
                yield Label("Repeat alarm every (s, 0=off)")
                yield Input(str(c["repeat_seconds"]), id="repeat_seconds", type="integer")
            with Horizontal():
                yield Label("Intel range (jumps)")
                yield Input(str(c["intel_jumps"]), id="intel_jumps", type="integer")
            yield Button("Save", id="save", variant="primary")

    def on_button_pressed(self, event):
        out = {k: self.query_one(f"#{k}", Switch).value for k in ("alert_neutrals", "alert_joins")}
        for k in ("repeat_seconds", "intel_jumps"):
            v = self.query_one(f"#{k}", Input).value
            out[k] = int(v) if v.isdigit() else self.cfg[k]
        self.dismiss(out)


class Tui(App):
    TITLE = "EVE Local Watch"
    BINDINGS = [Binding("s", "toggle", "Start/Stop"), Binding("w", "window", "Windows"),
                Binding("i", "intel", "Intel channel"), Binding("m", "mute", "Mute"),
                Binding("c", "settings", "Settings"), Binding("d", "snapshot", "Snapshot"),
                Binding("t", "test", "Test alerts"), Binding("q", "quit", "Quit")]
    CSS = """
    #banner {height: 5; content-align: center middle; text-style: bold; color: white}
    #clients {height: auto; max-height: 8}
    #mid {height: 1fr}
    #local {width: 45%; border: round grey; padding: 0 1}
    #intelbox {width: 55%}
    #intelinfo {height: auto; padding: 0 1}
    #tabs {height: 12}
    """

    def __init__(self):
        super().__init__()
        self.cfg = elw.load_config()
        self.selected = [self.cfg["character"]] if self.cfg["character"] else []
        self.clients = []
        self.restart = False
        self.drawn = None                       # event counts the tables were last drawn for

    def compose(self):
        yield Header()
        yield Static("", id="banner")
        yield DataTable(id="clients", show_cursor=False)
        with Horizontal(id="mid"):
            yield Static("", id="local")
            with Vertical(id="intelbox"):
                yield Static("", id="intelinfo")
                yield DataTable(id="intel", show_cursor=False)
        with TabbedContent(id="tabs"):
            with TabPane("Alerts"):
                yield DataTable(id="alerts", show_cursor=False)
            with TabPane("Log", id="logtab"):
                yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self):
        sys.stdout = LogOut(self)               # ponytail: global redirect, one app per process
        self.query_one("#clients", DataTable).add_columns("Client", "Status", "Pilots", "Host", "Neut", "Frnd", "System")
        self.query_one("#intel", DataTable).add_columns("Seen", "Client", "System", "Away", "Kind", "Reporter")
        self.query_one("#alerts", DataTable).add_columns("Time", "Event")
        self.set_interval(0.5, self.refresh_view)
        self.query_one("#log", RichLog).write("Press s to start watching.")
        self.refresh_view()

    # --- view ---------------------------------------------------------------
    def refresh_view(self):
        if self.restart and not any(c.alive for c in self.clients):    # window set changed: start fresh
            self.restart = False
            self.start()
        kinds = [c.kind for c in self.clients] or ["stopped"]
        label, color = LOOK[max(kinds, key=SEVERITY.index)]
        banner = self.query_one("#banner", Static)
        banner.styles.background = color
        if len(self.clients) == 1:
            c = self.clients[0]
            detail = c.ctx.live.get("text", "starting...") if c.alive else "press s to start"
        else:
            detail = f"{len(self.clients)} clients" if self.clients else "press s to start"
        mute = "MUTED" if elw.MUTED.is_set() else "sound on"
        banner.update(f"{label}\n{detail}\n{mute} | alert neutrals: {'on' if self.cfg['alert_neutrals'] else 'off'}")

        table = self.query_one("#clients", DataTable)
        table.clear()
        for c in self.clients:
            live = c.ctx.live
            last = next((e for e in reversed(c.ctx.events) if e["kind"] == "local"), {})
            table.add_row(c.name or "(any)", LOOK[c.kind][0], last.get("rows", "-"), last.get("hostile", "-"),
                          last.get("neutral", "-"), last.get("friendly", "-"), live.get("system") or "-")

        lines = Text()
        for c in self.clients:
            lines.append(f"{c.name or '(any window)'}\n", style="bold")
            rows = c.ctx.live.get("rows") or []
            if rows:
                for r in rows:
                    lines.append("■ ", style=BLOCK.get(r, "white"))
                lines.append("\n")
            else:
                lines.append("no member list in view\n", style="grey62")
            h = hint(c.ctx.live.get("reason") or "") or hint(c.ctx.live.get("text", ""))
            if c.alive and c.kind == "off" and h:
                lines.append(f"-> {h}\n", style="yellow")
            lines.append("\n")
        if not self.clients:
            lines.append("Local strip: red hostile, yellow flagged, white neutral, green friendly", style="grey62")
        self.query_one("#local", Static).update(lines)

        info = Text()
        for c in self.clients:
            line, style = intel_line(c)
            info.append(f"{c.name + ': ' if len(self.clients) > 1 else ''}{line}\n", style=style)
        if not self.clients:
            info.append("Intel: reports within range appear below once watching starts. Press i to choose "
                        "the channel.", style="grey62")
        self.query_one("#intelinfo", Static).update(info)

        counts = tuple(c.ctx.n for c in self.clients)
        if counts != self.drawn:
            self.drawn = counts
            self.redraw_events()

    def redraw_events(self):
        many = len(self.clients) > 1
        evs = sorted(((e, c.name or "(any)") for c in self.clients for e in c.ctx.events), key=lambda x: x[0]["t"])
        intel, alerts = self.query_one("#intel", DataTable), self.query_one("#alerts", DataTable)
        intel.clear()
        alerts.clear()
        for e, name in reversed(evs):
            t = time.strftime("%H:%M:%S", time.localtime(e["t"]))
            tag = f"[{name}] " if many else ""
            if e["kind"] == "intel":
                r = e["report"]
                intel.add_row(t, name, r.system, e["where"], r.kind.upper() + (" (recap)" if e.get("recap") else ""),
                              r.sender)
            elif e["kind"] == "alert":
                alerts.add_row(t, tag + e["text"])
            elif e["kind"] == "local":
                alerts.add_row(t, f"{tag}Local: {e['rows']} pilots, {e['hostile']} hostile, {e['neutral']} neutral")

    # --- control -------------------------------------------------------------
    def start(self):
        self.drawn = None
        self.clients = [Client(n, self.cfg) for n in (self.selected or [""])]
        for c in self.clients:
            c.thread.start()

    def stop(self):
        for c in self.clients:
            c.ctx.stop.set()

    def running(self):
        return any(c.alive for c in self.clients)

    def action_toggle(self):
        if self.running():
            self.stop()
        else:
            self.start()

    def action_test(self):
        if not self.running():
            threading.Thread(target=elw.cmd_test_alerts, args=(self.cfg,), name="test", daemon=True).start()

    def action_snapshot(self):
        """Each watcher saves its next frame + what it read (the path shows in the Log tab)."""
        live = [c for c in self.clients if c.alive]
        for c in live:
            c.ctx.snap.set()
        self.query_one("#log", RichLog).write(
            f"Snapshot requested ({len(live)} client{'s' if len(live) != 1 else ''})..." if live
            else "Start watching (s) first: a snapshot saves what the watcher sees.")
        self.query_one("#tabs", TabbedContent).active = "logtab"

    def action_mute(self):
        elw.MUTED.clear() if elw.MUTED.is_set() else elw.MUTED.set()

    def action_window(self):
        def chosen(names):
            if names is None:
                return
            self.selected = names
            if self.running():
                self.restart = True
                self.stop()
        self.push_screen(Pick(self.selected), chosen)

    def action_intel(self):
        def chosen(channels):
            if channels is None:
                return
            for target in [self.cfg, *(c.cfg for c in self.clients)]:
                target["intel_channels"] = channels
            elw.save_config(intel_channels=channels)
            if self.running():                  # Intel is built at start: restart to apply
                self.restart = True
                self.stop()
        self.push_screen(PickChannels(self.cfg), chosen)

    def action_settings(self):
        def saved(out):
            if out is None:
                return
            for target in [self.cfg, *(c.cfg for c in self.clients)]:
                target.update(out)               # watchers read cfg every pass: applies live
            elw.save_config(**out)
        self.push_screen(Settings(self.cfg), saved)

    def action_quit(self):
        self.stop()
        self.exit()


def main():
    if len(sys.argv) > 1:            # any argument: behave as the plain CLI (debug, intel, -c ...)
        return elw.main()
    Tui().run()


if __name__ == "__main__":
    main()
