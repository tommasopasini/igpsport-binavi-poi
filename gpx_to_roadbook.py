#!/usr/bin/env python3
"""Turn the <wpt> waypoints already in a GPX into a roadbook CSV that
generate_cnx.py can place on the BiNavi.

This is the missing link for GPX files that *do* carry waypoints (e.g. a Komoot
export that preserved them): instead of hand-writing the km of each point, this
projects every <wpt> onto the track to find its progressive distance, maps its
GPX <sym> to the device's internal POI <Type>, and writes inputs-ready CSV.

    python gpx_to_roadbook.py [--gpx PATH] [--out PATH] [--max-offset M]

Then:  python generate_cnx.py --roadbook <that CSV>

The waypoint usually sits a few metres off the track (it marks a fountain at the
roadside, not a recorded trackpoint); it is snapped to the nearest point on the
track. A waypoint farther than --max-offset (default 80 m) is still written but
flagged, since that often means it doesn't belong to this track.
"""
import argparse
import csv
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from generate_cnx import INPUTS, OUTPUTS, NS, hav, TYPE_BY_NAME

NAME_BY_TYPE = {code: name for name, code in TYPE_BY_NAME.items()}
R = 6371000.0

# GPX <sym> (lower-cased) -> internal <Type> code. Inverse of build_roadbook_gpx's
# SYM_BY_TYPE, plus common Komoot/Garmin symbol names. Unknown syms fall back to
# "waypoint" (0) and are reported so you can refine the CSV by hand.
TYPE_BY_SYM = {
    "drinking water": 7, "water source": 7, "water": 7, "potable water": 7,
    "restaurant": 13, "fast food": 13, "shop": 13, "shopping center": 13,
    "convenience store": 13, "restroom": 9, "toilet": 9, "wc": 9,
    "danger area": 19, "danger": 19, "summit": 15, "scenic area": 15,
    "viewpoint": 15, "photo": 16, "campground": 14, "parking area": 14,
}


def read_track(path):
    pts = []  # (lat, lon)
    for tp in ET.parse(path).getroot().iter(f"{{{NS}}}trkpt"):
        pts.append((float(tp.get("lat")), float(tp.get("lon"))))
    if len(pts) < 2:
        raise SystemExit(f"{path}: need >=2 trackpoints, found {len(pts)}")
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + hav(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    return pts, cum


def read_waypoints(path):
    wpts = []  # (lat, lon, name, sym)
    for w in ET.parse(path).getroot().iter(f"{{{NS}}}wpt"):
        name = w.find(f"{{{NS}}}name")
        sym = w.find(f"{{{NS}}}sym")
        wpts.append((
            float(w.get("lat")), float(w.get("lon")),
            (name.text or "").strip() if name is not None else "",
            (sym.text or "").strip() if sym is not None else "",
        ))
    return wpts


def project_km(wlat, wlon, pts, cum):
    """Snap (wlat,wlon) to the nearest point on the track. Returns (km, offset_m).

    Uses a local planar frame centred on the waypoint, projecting onto each track
    segment and clamping to it, so the km is taken at the true closest point.
    """
    cosw = math.cos(math.radians(wlat))

    def xy(lat, lon):
        return (math.radians(lon - wlon) * cosw * R, math.radians(lat - wlat) * R)

    best = (float("inf"), 0.0)  # (offset_m, km)
    for i in range(1, len(pts)):
        ax, ay = xy(*pts[i - 1])
        bx, by = xy(*pts[i])
        abx, aby = bx - ax, by - ay
        denom = abx * abx + aby * aby
        t = 0.0 if denom == 0 else max(0.0, min(1.0, -(ax * abx + ay * aby) / denom))
        px, py = ax + t * abx, ay + t * aby
        off = math.hypot(px, py)
        if off < best[0]:
            km = (cum[i - 1] + t * (cum[i] - cum[i - 1])) / 1000.0
            best = (off, km)
    return best[1], best[0]


def main():
    ap = argparse.ArgumentParser(description="GPX <wpt> waypoints -> roadbook CSV")
    ap.add_argument("--gpx", type=Path, help="GPX with waypoints (default: the one in ./inputs)")
    ap.add_argument("--out", type=Path, help="output CSV (default: ./inputs/<gpx-name>_roadbook.csv)")
    ap.add_argument("--max-offset", type=float, default=80.0, help="flag waypoints farther than this many metres from the track")
    args = ap.parse_args()

    gpx = args.gpx
    if gpx is None:
        cands = sorted(p for p in INPUTS.glob("*.gpx") if not p.name.endswith("_roadbook.gpx"))
        if len(cands) != 1:
            raise SystemExit(f"--gpx not given and {len(cands)} candidate GPX in {INPUTS} (expected 1)")
        gpx = cands[0]
    if not gpx.exists():
        raise SystemExit(f"GPX not found: {gpx}")

    out = args.out or (INPUTS / (gpx.stem + "_roadbook.csv"))
    out.parent.mkdir(parents=True, exist_ok=True)

    pts, cum = read_track(gpx)
    wpts = read_waypoints(gpx)
    if not wpts:
        raise SystemExit(f"{gpx.name}: no <wpt> waypoints to convert")
    print(f"Track: {len(pts)} points, {cum[-1] / 1000:.2f} km   Waypoints: {len(wpts)}")

    rows = []  # (km, type_name, description, sym, offset, known)
    for wlat, wlon, name, sym in wpts:
        km, off = project_km(wlat, wlon, pts, cum)
        code = TYPE_BY_SYM.get(sym.lower())
        known = code is not None
        rows.append((km, NAME_BY_TYPE[code if known else 0], name or "(unnamed)", sym, off, known))
    rows.sort(key=lambda r: r[0])

    # Header MUST be the first line: generate_cnx's csv.DictReader treats line 1 as
    # the fieldnames, and skips later rows whose km column starts with "#".
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["km", "type", "description"])
        w.writerow([f"# generated by gpx_to_roadbook.py from {gpx.name}", "", ""])
        w.writerow(["# review the 'type' column (mapped from each waypoint's GPX <sym>)", "", ""])
        for km, type_name, descr, *_ in rows:
            w.writerow([f"{km:.2f}", type_name, descr])

    for km, type_name, descr, sym, off, known in rows:
        flag = "  <-- FAR FROM TRACK" if off > args.max_offset else ""
        symnote = f"sym={sym!r}" + ("" if known else " -> unmapped, defaulted to 'waypoint'")
        print(f"  {km:7.2f} km  {type_name:16s} {descr!r:28s} [{symnote}] offset {off:.0f} m{flag}")

    print(f"\nWrote {out}  ({len(rows)} points)")
    print(f"Next:  python generate_cnx.py --gpx {gpx} --roadbook {out}")


if __name__ == "__main__":
    main()
