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

On a loop or out-and-back track the same spot is passed more than once. Every
pass is found, and the waypoint goes on the first one at or after the previous
waypoint's km, so waypoints listed in route order land on the right pass; such
waypoints are reported with all their candidate km so you can check.
"""
import argparse
import csv
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from generate_cnx import INPUTS, NS, TYPE_BY_NAME, cumulative, read_trkpts, resolve_gpx

NAME_BY_TYPE = {code: name for name, code in TYPE_BY_NAME.items()}
R = 6371000.0
# Repeat passes of the same road sit within a few metres of each other (GPS noise,
# opposite lanes); a neighbouring stretch, like the next leg of a switchback, is
# farther (17 m on Giara), and must not count as a pass.
PASS_SLACK_M = 10.0

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
    pts = [(lat, lon) for lat, lon, _ in read_trkpts(path)]
    return pts, cumulative(pts)


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


def track_passes(wlat, wlon, pts, cum):
    """Every pass of the track by (wlat, wlon), as [(km, offset_m)] in km order.

    Uses a local planar frame centred on the waypoint, projecting onto each track
    segment and clamping to it, so the km is taken at the true closest point. A
    pass is a run of consecutive segments within PASS_SLACK_M of the nearest
    approach, represented by its closest point; a loop's shared start/finish or
    an out-and-back road yields two.
    """
    cosw = math.cos(math.radians(wlat))

    def xy(lat, lon):
        return (math.radians(lon - wlon) * cosw * R, math.radians(lat - wlat) * R)

    segs = []  # (offset_m, km) of the closest point on each segment
    for i in range(1, len(pts)):
        ax, ay = xy(*pts[i - 1])
        bx, by = xy(*pts[i])
        abx, aby = bx - ax, by - ay
        denom = abx * abx + aby * aby
        t = 0.0 if denom == 0 else max(0.0, min(1.0, -(ax * abx + ay * aby) / denom))
        off = math.hypot(ax + t * abx, ay + t * aby)
        segs.append((off, (cum[i - 1] + t * (cum[i] - cum[i - 1])) / 1000.0))
    near = min(off for off, _ in segs) + PASS_SLACK_M
    passes, run = [], None
    for off, km in segs:
        if off <= near:
            if run is None or off < run[1]:
                run = (km, off)
        elif run is not None:
            passes.append(run)
            run = None
    if run is not None:
        passes.append(run)
    return passes


def choose_pass(passes, prev_km):
    """The first pass at or after prev_km, else the last one."""
    return next((p for p in passes if p[0] >= prev_km), passes[-1])


def main():
    ap = argparse.ArgumentParser(description="GPX <wpt> waypoints -> roadbook CSV")
    ap.add_argument("--gpx", type=Path, help="GPX with waypoints (default: the one in ./inputs)")
    ap.add_argument("--out", type=Path, help="output CSV (default: ./inputs/<gpx-name>_roadbook.csv)")
    ap.add_argument("--max-offset", type=float, default=80.0, help="flag waypoints farther than this many metres from the track")
    args = ap.parse_args()

    gpx = resolve_gpx(args.gpx)

    out = args.out or (INPUTS / (gpx.stem + "_roadbook.csv"))
    out.parent.mkdir(parents=True, exist_ok=True)

    pts, cum = read_track(gpx)
    wpts = read_waypoints(gpx)
    if not wpts:
        raise SystemExit(f"{gpx.name}: no <wpt> waypoints to convert")
    print(f"Track: {len(pts)} points, {cum[-1] / 1000:.2f} km   Waypoints: {len(wpts)}")

    rows = []  # (km, type_name, description, sym, offset, known, passes)
    prev_km = 0.0
    for wlat, wlon, name, sym in wpts:  # in file order: route order decides the pass
        passes = track_passes(wlat, wlon, pts, cum)
        km, off = choose_pass(passes, prev_km)
        prev_km = km
        code = TYPE_BY_SYM.get(sym.lower())
        known = code is not None
        rows.append((km, NAME_BY_TYPE[code if known else 0], name or "(unnamed)", sym, off, known, passes))
    rows.sort(key=lambda r: r[0])

    # generate_cnx.read_roadbook skips the "#" comment rows below the header.
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["km", "type", "description"])
        w.writerow([f"# generated by gpx_to_roadbook.py from {gpx.name}", "", ""])
        w.writerow(["# review the 'type' column (mapped from each waypoint's GPX <sym>)", "", ""])
        for km, type_name, descr, *_ in rows:
            w.writerow([f"{km:.2f}", type_name, descr])

    for km, type_name, descr, sym, off, known, passes in rows:
        flag = "  <-- FAR FROM TRACK" if off > args.max_offset else ""
        symnote = f"sym={sym!r}" + ("" if known else " -> unmapped, defaulted to 'waypoint'")
        print(f"  {km:7.2f} km  {type_name:16s} {descr!r:28s} [{symnote}] offset {off:.0f} m{flag}")
        if len(passes) > 1:
            kms = ", ".join(f"{p[0]:.2f}" for p in passes)
            print(f"             track passes here {len(passes)}x (km {kms}) - picked {km:.2f} by waypoint order")

    print(f"\nWrote {out}  ({len(rows)} points)")
    print(f"Next:  python generate_cnx.py --gpx {gpx} --roadbook {out}")


if __name__ == "__main__":
    main()
