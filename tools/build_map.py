#!/usr/bin/env python3
"""Build data/eve_map.json (system names + stargate connections) from CCP's
static data export. Re-run after an expansion adds or moves systems.

Usage: uv run tools/build_map.py [SDE-JSONL-ZIP]
       (downloads the latest SDE from developers.eveonline.com if no zip is given)
"""
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

SDE_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
OUT = Path(__file__).resolve().parent.parent / "data" / "eve_map.json"


def main():
    if len(sys.argv) > 1:
        zf = zipfile.ZipFile(sys.argv[1])
    else:
        print(f"downloading {SDE_URL} (~100 MB)...", flush=True)
        with urllib.request.urlopen(SDE_URL) as r:
            zf = zipfile.ZipFile(io.BytesIO(r.read()))
    names = {}
    for line in zf.open("mapSolarSystems.jsonl"):
        s = json.loads(line)
        names[s["_key"]] = s["name"]["en"]
    links = set()
    for line in zf.open("mapStargates.jsonl"):
        g = json.loads(line)
        a, b = g["solarSystemID"], g["destination"]["solarSystemID"]
        links.add((min(a, b), max(a, b)))
    # Only systems with gates: wormhole space can't be reached by jumping.
    ids = sorted({i for link in links for i in link})
    index = {sid: k for k, sid in enumerate(ids)}
    data = {"systems": [names[i] for i in ids],
            "gates": sorted([index[a], index[b]] for a, b in links)}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"{OUT}: {len(ids)} systems, {len(links)} gate connections")


if __name__ == "__main__":
    main()
