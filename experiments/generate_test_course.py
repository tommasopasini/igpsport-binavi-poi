#!/usr/bin/env python3
"""DEAD END (kept for the record): FIT course points are ignored by the BiNavi
firmware — see BINAVI_NOTES.md §4. Use generate_cnx.py instead.

Mini test FIT course: a straight 400 m track from a start point you pass in,
with 3 closely-spaced course points (water/danger/food). Uses gpsbabel + in-place
patch + CRC.

Usage: python experiments/generate_test_course.py --lat LAT --lon LON
"""
import argparse, math, struct, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser(description="(dead end) build a tiny test FIT course at a given start point")
ap.add_argument("--lat", type=float, required=True, help="start latitude")
ap.add_argument("--lon", type=float, required=True, help="start longitude")
ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "Test_Course.fit")
args = ap.parse_args()

LAT0, LON0 = args.lat, args.lon       # start point (passed in, never hardcoded)
TMP_GPX = "/tmp/test_course.gpx"
OUT = str(args.out)
args.out.parent.mkdir(parents=True, exist_ok=True)

# course point: (meters_from_start, type, name<=15)  type: 3=water 4=food 5=danger
CPS = [
    (0,   3, "Test fontana"),    # at your feet
    (50,  5, "! Test 50m"),      # after a few steps
    (150, 4, "Test ristoro"),    # after ~2 min walking
]

# --- geometry: head east, 1 point every 10 m, 400 m total ---
m_per_deg_lon = 111320.0 * math.cos(math.radians(LAT0))
def east(m): return LON0 + m / m_per_deg_lon

NPTS = 41                      # 0..400 m
verts = [(i*10, LAT0, east(i*10)) for i in range(NPTS)]   # (dist_m, lat, lon)

# --- write a temp GPX: track with timestamps (~5 km/h) + 3 wpt tagged CPxxx ---
def ts(sec):
    s = 8*3600 + sec
    return f"2026-06-12T{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}Z"

with open(TMP_GPX, "w") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    f.write('<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">\n')
    for m, t, name in CPS:                       # wpt BEFORE the trk
        lat, lon = LAT0, east(m)
        f.write(f'  <wpt lat="{lat:.7f}" lon="{lon:.7f}"><name>CP{m}</name></wpt>\n')
    f.write('  <trk><name>Test Course iGPSPORT</name><trkseg>\n')
    for i, (m, lat, lon) in enumerate(verts):
        f.write(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"><ele>50</ele><time>{ts(int(m/1.39))}</time></trkpt>\n')
    f.write('  </trkseg></trk>\n</gpx>\n')

# --- gpsbabel -> FIT ---
subprocess.run(["gpsbabel", "-i", "gpx", "-f", TMP_GPX, "-o", "garmin_fit", "-F", OUT], check=True)

# --- patch: exact type + name + distance/position, then CRC ---
SEMI = 2**31 / 180.0
data = bytearray(open(OUT, "rb").read())
hs = data[0]; end = hs + struct.unpack('<I', data[4:8])[0]; pos = hs
defs = {}; rec_dist = []; cps = []
def fstr(b): return b.split(b'\x00')[0].decode('utf-8','replace')
while pos < end:
    h = data[pos]; pos += 1
    if h & 0x80: pos += defs[(h>>5)&0x3][1]; continue
    lmt = h & 0x0f
    if h & 0x40:
        arch = data[pos+1]; gmn = struct.unpack('<H' if arch==0 else '>H', data[pos+2:pos+4])[0]
        nf = data[pos+4]; p = pos+5; fields = []
        for _ in range(nf): fields.append((data[p],data[p+1],data[p+2])); p += 3
        sz = sum(f[1] for f in fields); pos = p
        if h & 0x20:
            ndf = data[pos]; pos += 1; sz += sum(data[pos+i*3+1] for i in range(ndf)); pos += ndf*3
        defs[lmt] = (gmn, sz, fields)
    else:
        gmn, sz, fields = defs[lmt]; off = pos; lay = {}
        for fdn, fsz, bt in fields: lay[fdn] = (off, fsz); off += fsz
        if gmn == 20 and 5 in lay:
            rec_dist.append(struct.unpack('<I', data[lay[5][0]:lay[5][0]+4])[0]/100.0)
        elif gmn == 32:
            cps.append((fstr(data[lay[6][0]:lay[6][0]+lay[6][1]]), lay))
        pos += sz

BY_TOK = {f"CP{m}": (m, t, n) for m, t, n in CPS}
for name, lay in cps:
    m, t, nm = BY_TOK[name.split()[0]]
    idx = m // 10                                   # matching vertex
    o, s = lay[2]; data[o:o+s] = struct.pack('<i', int(round(LAT0*SEMI)))
    o, s = lay[3]; data[o:o+s] = struct.pack('<i', int(round(east(m)*SEMI)))
    o, s = lay[4]; data[o:o+s] = struct.pack('<I', int(round(rec_dist[idx]*100)))
    o, s = lay[5]; data[o] = t
    o, s = lay[6]; data[o:o+s] = nm.encode('ascii')[:s-1].ljust(s, b'\x00')

CRC_T = [0x0000,0xCC01,0xD801,0x1400,0xF001,0x3C00,0x2800,0xE401,
         0xA001,0x6C00,0x7800,0xB401,0x5000,0x9C01,0x8801,0x4400]
def crc(buf):
    c = 0
    for b in buf:
        t = CRC_T[c&0xF]; c = (c>>4)&0xFFF; c ^= t ^ CRC_T[b&0xF]
        t = CRC_T[c&0xF]; c = (c>>4)&0xFFF; c ^= t ^ CRC_T[(b>>4)&0xF]
    return c & 0xFFFF
data[end:end+2] = struct.pack('<H', crc(bytes(data[0:end])))
open(OUT, "wb").write(data)
print(f"Wrote {OUT}: {len(verts)} track points (400 m), {len(cps)} course points")
