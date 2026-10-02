#!/usr/bin/env python3
"""DEAD END (kept for the record): FIT course points are ignored by the BiNavi
firmware — see BINAVI_NOTES.md §4. Use generate_cnx.py instead.

Build a FIT course from the same track + points as a source .cnx, for an on-foot
A/B comparison of FIT vs .cnx in the same spot.

Usage: python experiments/generate_test_fit_from_cnx.py [--src inputs/test.cnx] [--out outputs/Test_FIT.fit]
"""
import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from generate_cnx import decode_tracks, hav  # noqa: E402

START = datetime(2026, 6, 13, 8, 0, tzinfo=timezone.utc)
WALKING_MS = 1.39  # ~5 km/h


def read_cnx(path):
    """(track [(lat, lon, ele)], points [(lat, lon, description)]) of a .cnx. The
    app adds the route's start address as a <Point> without <Type>: dropped."""
    root = ET.parse(path).getroot()
    track = decode_tracks(root.findtext("Tracks"))
    points = [(float(p.findtext("Lat")), float(p.findtext("Lng")), p.findtext("Descr") or "")
              for p in root.iter("Point") if (p.findtext("Type") or "").strip()]
    return track, points


def gpx_time(seconds):
    return (START + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_gpx(path, track, points):
    """Track with walking-pace timestamps (gpsbabel needs times) + the points as <wpt>."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">']
    lines += [f'  <wpt lat="{a:.7f}" lon="{b:.7f}"><name>{escape(d)}</name></wpt>' for a, b, d in points]
    lines.append("  <trk><name>Test FIT from cnx</name><trkseg>")
    t = 0.0
    for i, (a, b, e) in enumerate(track):
        if i:
            t += hav(track[i - 1][0], track[i - 1][1], a, b) / WALKING_MS
        lines.append(f'    <trkpt lat="{a:.7f}" lon="{b:.7f}"><ele>{e:.1f}</ele><time>{gpx_time(int(t))}</time></trkpt>')
    lines += ["  </trkseg></trk>", "</gpx>", ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="(dead end) source .cnx -> FIT course")
    ap.add_argument("--src", type=Path, default=ROOT / "inputs" / "test.cnx",
                    help="source .cnx (copy one off the device into inputs/)")
    ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "Test_FIT.fit")
    args = ap.parse_args()
    if not args.src.exists():
        raise SystemExit(f"source .cnx not found: {args.src}")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    track, points = read_cnx(args.src)
    print(f"track: {len(track)} points   POI: {len(points)}")
    gpx = args.out.with_suffix(".gpx")
    write_gpx(gpx, track, points)
    subprocess.run(["gpsbabel", "-i", "gpx", "-f", str(gpx), "-o", "garmin_fit", "-F", str(args.out)], check=True)
    print(f"Wrote {args.out}  (via {gpx})")


if __name__ == "__main__":
    main()
