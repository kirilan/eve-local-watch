"""Headless check of the TUI with fake watcher data (no EVE needed). Run: python selftest_tui.py"""
import asyncio
import json
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from textual.widgets import DataTable, Input, Static

import eve_local_watch as elw
import tui

OUT = sys.__stdout__


class Fake(tui.Client):
    """A client whose 'watcher' just idles while the test fills its ctx."""

    def run(self):
        self.ctx.stop.wait(10)


async def main():
    elw.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"
    app = tui.Tui()
    async with app.run_test() as pilot:
        app.selected = ["Alice", "Bob"]
        tui.Client = Fake                    # start() builds its clients from this name
        app.start()
        a, b = app.clients
        a.ctx.live.update(kind="alert", text="watching", rows=["friendly", "hostile"], system="Jita")
        a.ctx.event("local", rows=2, hostile=1, neutral=0, friendly=1)
        a.ctx.event("alert", text="HOSTILE in Local: 1 hostile, 0 neutral")
        r = elw.Report(datetime.now(), "intel", "Scout", "Y-2 hostile", "hostile", "Y-2", 2)
        a.ctx.event("intel", report=r, where="2 jumps away")
        a.ctx.event("intel", report=r, where="2 jumps away", recap=True)
        a.ctx.live["intel"] = dict(dir="x", names=["wc.Test"], found=["wc.Test"], system="Jita", near=12,
                                   jumps=5, total=42, last=None)
        b.ctx.live["intel"] = dict(dir="x", names=["wc.Test"], found=[], system=None, near=0, jumps=5,
                                   total=0, last=None)
        b.ctx.live.update(kind="off", text="NOT WATCHING - Local not visible", reason="no 'Local' tab found")
        await pilot.pause(1)

        assert str(app.query_one("#banner", Static).render()).startswith("THREAT"), "worst kind wins"
        assert app.query_one("#clients", DataTable).row_count == 2
        assert app.query_one("#intel", DataTable).row_count == 2
        info = str(app.query_one("#intelinfo", Static).render())
        assert "Following wc.Test" in info and "42 msgs" in info, info
        assert "No chat log for wc.Test" in info, "tells what is wrong when the channel has no log"
        assert app.query_one("#alerts", DataTable).row_count == 2
        assert "Open the Local chat window" in str(app.query_one("#local", Static).render()), "setup hint"

        await pilot.press("m")
        assert elw.MUTED.is_set()
        await pilot.press("m")
        assert not elw.MUTED.is_set()

        await pilot.press("d")
        assert a.ctx.snap.is_set() and b.ctx.snap.is_set(), "snapshot request reaches every watcher"

        await pilot.press("c"); await pilot.pause(0.3)
        app.screen.query_one("#intel_jumps", Input).value = "3"
        await pilot.click("#save"); await pilot.pause(0.3)
        assert app.cfg["intel_jumps"] == 3 and a.cfg["intel_jumps"] == 3, "applied live"
        assert json.loads(elw.CONFIG_PATH.read_text())["intel_jumps"] == 3, "saved"

        elw.list_channels = lambda cfg: ["wc.Test"]
        await pilot.press("i"); await pilot.pause(0.3)
        await pilot.press("down", "space", "enter"); await pilot.pause(1.5)
        assert app.cfg["intel_channels"] == ["wc.Test"], app.cfg["intel_channels"]
        assert json.loads(elw.CONFIG_PATH.read_text())["intel_channels"] == ["wc.Test"], "channel saved"

        await pilot.press("s"); await pilot.pause(1)
        assert not app.running(), "stop reaches every client"
    print("tui selftest passed", file=OUT)


asyncio.run(main())
