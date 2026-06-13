#!/usr/bin/env python3
"""DEAD END (kept for the record): FIT course points are ignored by the BiNavi
firmware — see BINAVI_NOTES.md §4. Use generate_cnx.py instead.

Build a FIT course from the same track + points as a source .cnx, for an on-foot
A/B comparison of FIT vs .cnx in the same spot.

Usage: python experiments/generate_test_fit_from_cnx.py [--src inputs/test.cnx] [--out outputs/Test_FIT.fit]
"""
import argparse, re, math, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser(description="(dead end) source .cnx -> FIT course")
ap.add_argument("--src", type=Path, default=ROOT / "inputs" / "test.cnx", help="source .cnx (copy one off the device into inputs/)")
ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "Test_FIT.fit")
args = ap.parse_args()
SRC, GPX, OUT = str(args.src), "/tmp/test_from_cnx.gpx", str(args.out)
if not args.src.exists():
    raise SystemExit(f"source .cnx not found: {SRC}")
args.out.parent.mkdir(parents=True, exist_ok=True)

x = open(SRC, encoding="utf-8").read()

# --- decode <Tracks> (lat/lon 2nd order 1e-7, ele 1st order cm) ---
toks = [t for t in re.search(r'<Tracks>(.*?)</Tracks>', x, re.S).group(1).split(';') if t.strip()]
la, lo, el = toks[0].split(',')
lat = float(la); lon = float(lo); ele_mm = float(el)
vla = vlo = 0.0
pts = [(lat, lon, ele_mm/1000)]
for tk in toks[1:]:
    dla, dlo, dele = [int(v) for v in tk.split(',')]
    vla += dla; vlo += dlo
    lat += vla/1e7; lon += vlo/1e7; ele_mm += dele*10
    pts.append((lat, lon, ele_mm/1000))

# --- points ---
points = re.findall(r'<Point><Lat>([\d.]+)</Lat><Lng>([\d.]+)</Lng>(?:<Type>(-?\d+)</Type>)?<Descr>(.*?)</Descr>', x)
points = [(float(a), float(b), d) for a, b, t, d in points if t is not None]  # drop the start point with no Type
print(f"track: {len(pts)} points   POI: {len(points)}")

# --- write GPX (track with walking-pace timestamps + wpt) ---
def ts(sec):
    s = 8*3600 + sec
    return f"2026-06-13T{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}Z"
def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
with open(GPX, "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">\n')
    for a, b, d in points:
        f.write(f'  <wpt lat="{a:.7f}" lon="{b:.7f}"><name>{esc(d)}</name></wpt>\n')
    f.write('  <trk><name>Test FIT from cnx</name><trkseg>\n')
    t = 0.0
    for i, (a, b, e) in enumerate(pts):
        if i > 0:
            R = 6371000; p1, p2 = math.radians(pts[i-1][0]), math.radians(a)
            dp, dl = math.radians(a-pts[i-1][0]), math.radians(b-pts[i-1][1])
            dist = 2*R*math.asin(math.sqrt(math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2))
            t += dist/1.39  # ~5 km/h
        f.write(f'    <trkpt lat="{a:.7f}" lon="{b:.7f}"><ele>{e:.1f}</ele><time>{ts(int(t))}</time></trkpt>\n')
    f.write('  </trkseg></trk>\n</gpx>\n')

# --- gpsbabel -> FIT ---
subprocess.run(["gpsbabel", "-i", "gpx", "-f", GPX, "-o", "garmin_fit", "-F", OUT], check=True)
print(f"Wrote {OUT}")
