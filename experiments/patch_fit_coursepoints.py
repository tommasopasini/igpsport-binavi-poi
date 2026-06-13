#!/usr/bin/env python3
"""DEAD END (kept for the record): FIT course points are ignored by the BiNavi
firmware — see BINAVI_NOTES.md §4. Use generate_cnx.py instead.

Fix the course_points in the FIT produced by gpsbabel:
- assign the correct type (water/food/danger)
- relocate each point to the exact distance/position by km (loop-snap bug fix)
- recompute the final FIT CRC
Length-preserving: patches the fields in-place (same size). The km->name/type maps
below are an example (the original Giara roadbook); adapt them to your own FIT."""
import math, struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GPX = str(ROOT / "inputs" / "track.gpx")     # the GPX the FIT was built from
FIT = str(ROOT / "outputs" / "course.fit")   # the FIT to patch in-place

# km -> course_point type (3=water, 4=food, 5=danger)
TYPE_BY_KM = {
    9.4: 3, 13.5: 5, 14.5: 5, 32.5: 3, 39.0: 3, 61.0: 4, 70.0: 3, 74.0: 5,
    79.8: 3, 82.8: 4, 89.0: 5, 91.0: 3, 93.0: 5, 100.0: 3, 106.0: 3, 109.0: 3,
}
# km token as it appears in <name> ("9.4", "109", ...) -> km float
KM_TOKEN = {f"{k:g}": k for k in TYPE_BY_KM}

# short name (<=15 ASCII chars) shown on the device; the device shows the distance itself
NAME_BY_KM = {
    9.4: "Fontana Chievo", 13.5: "! Segui traccia", 14.5: "! Alzaia",
    32.5: "Fontana Arce", 39.0: "Font.Palazzolo", 61.0: "Ristoro Calmas.",
    70.0: "Fontana Ponton", 74.0: "! Attr.Statale", 79.8: "Fontana Bure",
    82.8: "Fumane font+ris", 89.0: "! Deviaz.+font", 91.0: "Fontana Marano",
    93.0: "! Inizio disc.", 100.0: "Font.Pedemonte", 106.0: "Fontana Parona",
    109.0: "Font.Diga Chiev",
}

SEMI = 2**31 / 180.0
def to_semi(deg): return int(round(deg * SEMI))

def haversine(a, b, c, d):
    R = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c-a), math.radians(d-b)
    x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(x))

# ---- 1. GPX vertices + cumulative distance (to find bracket and fraction) ----
import xml.etree.ElementTree as ET
NS = "http://www.topografix.com/GPX/1/1"
verts = []
cum = 0.0; prev = None
for tp in ET.parse(GPX).getroot().iter(f"{{{NS}}}trkpt"):
    la, lo = float(tp.get("lat")), float(tp.get("lon"))
    if prev: cum += haversine(prev[0], prev[1], la, lo)
    verts.append((cum, la, lo)); prev = (la, lo)

def bracket(target_km):
    t = target_km * 1000.0
    if t <= verts[0][0]: return 0, 0, 0.0
    if t >= verts[-1][0]: return len(verts)-1, len(verts)-1, 0.0
    for i in range(1, len(verts)):
        if verts[i-1][0] <= t <= verts[i][0]:
            d0, d1 = verts[i-1][0], verts[i][0]
            f = 0.0 if d1 == d0 else (t-d0)/(d1-d0)
            return i-1, i, f
    return len(verts)-1, len(verts)-1, 0.0

# ---- 2. parse FIT: records (for distance in the gpsbabel frame) + course_point offsets ----
data = bytearray(open(FIT, "rb").read())
hsize = data[0]
dlen = struct.unpack('<I', data[4:8])[0]
end = hsize + dlen
pos = hsize
defs = {}
rec_dist = []          # distance (m) for each record, gpsbabel frame
rec_has_dist = True
cps = []               # (name, {fieldnum:(abs_off,size)})

def fstr(b): return b.split(b'\x00')[0].decode('utf-8', 'replace')

while pos < end:
    h = data[pos]; pos += 1
    if h & 0x80:  # compressed timestamp
        lmt = (h >> 5) & 0x3; g, sz, fl = defs[lmt]; pos += sz; continue
    lmt = h & 0x0f
    if h & 0x40:  # definition
        arch = data[pos+1]
        gmn = struct.unpack('<H' if arch == 0 else '>H', data[pos+2:pos+4])[0]
        nf = data[pos+4]; p = pos+5; fields = []
        for _ in range(nf):
            fields.append((data[p], data[p+1], data[p+2])); p += 3
        size = sum(f[1] for f in fields); pos = p
        if h & 0x20:
            ndf = data[pos]; pos += 1; size += sum(data[pos+i*3+1] for i in range(ndf)); pos += ndf*3
        defs[lmt] = (gmn, size, fields)
    else:
        g, sz, fields = defs[lmt]
        off = pos; layout = {}
        for fdn, fsz, bt in fields:
            layout[fdn] = (off, fsz); off += fsz
        if g == 20:  # record
            if 5 in layout:
                o, s = layout[5]
                rec_dist.append(struct.unpack('<I', data[o:o+s])[0] / 100.0)
            else:
                rec_has_dist = False; rec_dist.append(None)
        elif g == 32:  # course_point
            name = fstr(data[layout[6][0]:layout[6][0]+layout[6][1]]) if 6 in layout else ""
            cps.append((name, layout))
        pos += sz

print(f"record={len(rec_dist)} gpx_vertices={len(verts)} course_point={len(cps)} rec_has_dist={rec_has_dist}")
assert len(rec_dist) == len(verts), "record != vertices: invalid index mapping"

def gps_dist_at(target_km):
    i0, i1, f = bracket(target_km)
    if rec_has_dist:
        d0, d1 = rec_dist[i0], rec_dist[i1]
        return d0 + f*(d1-d0)
    return target_km*1000.0  # fallback: own cumulative

# ---- 3. in-place patch of each course_point ----
patched = 0
for name, layout in cps:
    tok = name.split()[0] if name else ""
    if tok not in KM_TOKEN:
        print(f"  ! skipping unrecognized course_point: {name!r}"); continue
    km = KM_TOKEN[tok]
    i0, i1, f = bracket(km)
    la = verts[i0][1] + (verts[i1][1]-verts[i0][1])*f
    lo = verts[i0][2] + (verts[i1][2]-verts[i0][2])*f
    dm = gps_dist_at(km)
    # field 2=lat, 3=long, 4=distance, 5=type
    o, s = layout[2]; data[o:o+s] = struct.pack('<i', to_semi(la))
    o, s = layout[3]; data[o:o+s] = struct.pack('<i', to_semi(lo))
    o, s = layout[4]; data[o:o+s] = struct.pack('<I', int(round(dm*100)))
    o, s = layout[5]; data[o] = TYPE_BY_KM[km]
    o, s = layout[6]                                   # name (16 bytes, null-padded)
    nb = NAME_BY_KM[km].encode('ascii')[:s-1].ljust(s, b'\x00')
    data[o:o+s] = nb
    patched += 1

print(f"course_points patched: {patched}")

# ---- 4. recompute the final FIT CRC ----
CRC_T = [0x0000,0xCC01,0xD801,0x1400,0xF001,0x3C00,0x2800,0xE401,
         0xA001,0x6C00,0x7800,0xB401,0x5000,0x9C01,0x8801,0x4400]
def fit_crc(buf):
    crc = 0
    for byte in buf:
        t = CRC_T[crc & 0xF]; crc = (crc >> 4) & 0x0FFF; crc ^= t ^ CRC_T[byte & 0xF]
        t = CRC_T[crc & 0xF]; crc = (crc >> 4) & 0x0FFF; crc ^= t ^ CRC_T[(byte >> 4) & 0xF]
    return crc & 0xFFFF

stored = struct.unpack('<H', data[end:end+2])[0]
# detect which range gpsbabel uses for the CRC (whole file vs data only)
rng_full = bytes(data[0:end]); rng_data = bytes(data[hsize:end])
# (note: the patched bytes are identical in both hypotheses as far as effect goes)
orig = open(FIT, 'rb').read()
if fit_crc(orig[0:end]) == stored:
    new_crc = fit_crc(rng_full); mode = "full"
elif fit_crc(orig[hsize:end]) == stored:
    new_crc = fit_crc(rng_data); mode = "data-only"
else:
    new_crc = fit_crc(rng_full); mode = "full(default)"
data[end:end+2] = struct.pack('<H', new_crc)
print(f"CRC range={mode}  stored={stored:#06x} new={new_crc:#06x}")

open(FIT, "wb").write(data)
print(f"Wrote {FIT} ({len(data)} bytes)")
