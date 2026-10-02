#!/usr/bin/env python3
"""Insert the roadbook points as GPX waypoints onto the track, placed by
progressive distance (km). Useful to preview the points in any map viewer
(Komoot, GPXSee, etc.) — it does NOT put them on the BiNavi (use generate_cnx.py
for that).

Usage:
    python build_roadbook_gpx.py [--gpx PATH] [--roadbook PATH] [--out PATH]

Defaults mirror generate_cnx.py (inputs/ in, outputs/ out). Same roadbook CSV,
placed and checked exactly as generate_cnx.py places it on the device.
"""
import argparse
from pathlib import Path
import xml.etree.ElementTree as ET

from generate_cnx import (INPUTS, OUTPUTS, NS, cumulative, place_points, read_roadbook,
                          read_trkpts, resolve_gpx)

# map the internal <Type> code -> a common GPX symbol name
SYM_BY_TYPE = {
    7: "Drinking Water",      # supply point
    9: "Restroom",            # restroom
    13: "Restaurant",         # shop
    19: "Danger Area",        # dangerous road
    20: "Danger Area",        # sharp turn
    21: "Danger Area",        # steep slope
    22: "Danger Area",        # intersection
}
DEFAULT_SYM = "Flag, Blue"


def main():
    ap = argparse.ArgumentParser(description="GPX + roadbook CSV -> GPX with waypoints (for previewing)")
    ap.add_argument("--gpx", type=Path, help="input GPX track (default: the one in ./inputs)")
    ap.add_argument("--roadbook", type=Path, help="roadbook CSV (default: ./inputs/roadbook.csv)")
    ap.add_argument("--out", type=Path, help="output GPX (default: ./outputs/<gpx-name>_roadbook.gpx)")
    args = ap.parse_args()

    gpx = resolve_gpx(args.gpx)
    roadbook = args.roadbook or (INPUTS / "roadbook.csv")
    if not roadbook.exists():
        raise SystemExit(f"roadbook not found: {roadbook}")
    out = args.out or (OUTPUTS / (gpx.stem + "_roadbook.gpx"))
    out.parent.mkdir(parents=True, exist_ok=True)

    tree = ET.parse(gpx)
    root = tree.getroot()
    ET.register_namespace("", NS)

    pts = read_trkpts(gpx)
    cum = cumulative(pts)
    print(f"Trackpoints: {len(pts)}  -  track length: {cum[-1] / 1000.0:.2f} km")

    # drop existing waypoints, insert the roadbook ones before <trk> (valid GPX order)
    for old in root.findall(f"{{{NS}}}wpt"):
        root.remove(old)
    trk = root.find(f"{{{NS}}}trk")
    trk_index = list(root).index(trk)

    new_wpts = []
    rows = read_roadbook(roadbook)
    for (km, _, _), (lat, lon, typ, desc) in zip(rows, place_points(rows, pts, cum)):
        w = ET.Element(f"{{{NS}}}wpt", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"})
        ET.SubElement(w, f"{{{NS}}}name").text = f"{km:g} {desc}"
        ET.SubElement(w, f"{{{NS}}}cmt").text = desc
        ET.SubElement(w, f"{{{NS}}}desc").text = desc
        ET.SubElement(w, f"{{{NS}}}sym").text = SYM_BY_TYPE.get(typ, DEFAULT_SYM)
        new_wpts.append(w)
        print(f"  {km:6.1f} km -> {lat:.6f}, {lon:.6f}  [type {typ}] {desc}")

    for off, w in enumerate(new_wpts):
        root.insert(trk_index + off, w)

    ET.indent(tree, space="  ")
    tree.write(out, encoding="UTF-8", xml_declaration=True)
    print(f"\nWrote: {out}  ({len(new_wpts)} waypoints)")


if __name__ == "__main__":
    main()
