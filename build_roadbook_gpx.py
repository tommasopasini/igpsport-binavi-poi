#!/usr/bin/env python3
"""Insert the roadbook points as GPX waypoints onto the track, placed by
progressive distance (km). Useful to preview the points in any map viewer
(Komoot, GPXSee, etc.) — it does NOT put them on the BiNavi (use generate_cnx.py
for that).

Usage:
    python build_roadbook_gpx.py [--gpx PATH] [--roadbook PATH] [--out PATH]

Defaults mirror generate_cnx.py (inputs/ in, outputs/ out). Same roadbook CSV.
"""
import argparse
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from generate_cnx import INPUTS, OUTPUTS, NS, read_roadbook

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


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def main():
    ap = argparse.ArgumentParser(description="GPX + roadbook CSV -> GPX with waypoints (for previewing)")
    ap.add_argument("--gpx", type=Path, help="input GPX track (default: the one in ./inputs)")
    ap.add_argument("--roadbook", type=Path, help="roadbook CSV (default: ./inputs/roadbook.csv)")
    ap.add_argument("--out", type=Path, help="output GPX (default: ./outputs/<gpx-name>_roadbook.gpx)")
    args = ap.parse_args()

    gpx = args.gpx
    if gpx is None:
        cands = sorted(p for p in INPUTS.glob("*.gpx") if not p.name.endswith("_roadbook.gpx"))
        if len(cands) != 1:
            raise SystemExit(f"--gpx not given and {len(cands)} candidate GPX in {INPUTS} (expected 1)")
        gpx = cands[0]
    roadbook = args.roadbook or (INPUTS / "roadbook.csv")
    if not roadbook.exists():
        raise SystemExit(f"roadbook not found: {roadbook}")
    out = args.out or (OUTPUTS / (gpx.stem + "_roadbook.gpx"))
    out.parent.mkdir(parents=True, exist_ok=True)

    tree = ET.parse(gpx)
    root = tree.getroot()
    ET.register_namespace("", NS)

    # collect trackpoints with cumulative distance
    pts, cum, prev = [], 0.0, None
    for tp in root.iter(f"{{{NS}}}trkpt"):
        lat, lon = float(tp.get("lat")), float(tp.get("lon"))
        if prev is not None:
            cum += haversine(prev[0], prev[1], lat, lon)
        pts.append((cum, lat, lon))
        prev = (lat, lon)
    print(f"Trackpoints: {len(pts)}  -  track length: {pts[-1][0] / 1000.0:.2f} km")

    def at_km(target_km):
        t = target_km * 1000.0
        if t <= pts[0][0]:
            return pts[0][1], pts[0][2]
        if t >= pts[-1][0]:
            return pts[-1][1], pts[-1][2]
        for i in range(1, len(pts)):
            d0, la0, lo0 = pts[i - 1]
            d1, la1, lo1 = pts[i]
            if d0 <= t <= d1:
                f = 0.0 if d1 == d0 else (t - d0) / (d1 - d0)
                return la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f
        return pts[-1][1], pts[-1][2]

    # drop existing waypoints, insert the roadbook ones before <trk> (valid GPX order)
    for old in root.findall(f"{{{NS}}}wpt"):
        root.remove(old)
    trk = root.find(f"{{{NS}}}trk")
    trk_index = list(root).index(trk)

    new_wpts = []
    for km, typ, desc in read_roadbook(roadbook):
        lat, lon = at_km(km)
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
