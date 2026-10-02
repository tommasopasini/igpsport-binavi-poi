#!/usr/bin/env python3
"""Generate a native iGPSPORT BiNavi `.cnx` route from a GPX track plus a
roadbook CSV of points-of-interest, so the points show up on the device during
navigation (something the official app makes hard).

Track encoding: lat/lon as 2nd-order delta (1e-7 deg), elevation as 1st-order
delta (cm) — see docs in BINAVI_NOTES.md. A round-trip self-test checks the
re-decoded track matches the GPX to the format's resolution (under 0.8 cm in
position, 1.1 cm in elevation) before the file is written.

Usage:
    python generate_cnx.py [--gpx PATH] [--roadbook PATH] [--out PATH]

Defaults (so day-to-day work stays entirely local):
    --gpx       the single *.gpx in ./inputs (excluding *_roadbook.gpx)
    --roadbook  ./inputs/roadbook.csv if present, else no points
    --out       ./outputs/<gpx-name>.cnx

Roadbook CSV columns: km,type,description   (see roadbook.example.csv)
`type` is a category name (case-insensitive) or its integer code. Lines starting
with "#" are comments, blank lines are ignored. A km past the end of the track
is placed at the finish when within 1% of the track length, rejected otherwise.
"""
import argparse
import bisect
import csv
import hashlib
import math
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / "inputs"
OUTPUTS = ROOT / "outputs"
NS = "http://www.topografix.com/GPX/1/1"

# Internal <Type> enum (name -> code). NOT the on-screen menu order, NOT the FIT
# enum. There is no "fountain"/"food" category; "supply point" is closest.
TYPE_BY_NAME = {
    "waypoint": 0, "sprint point": 1, "hc climb": 2,
    "level 1 climb": 3, "level 2 climb": 4, "level 3 climb": 5, "level 4 climb": 6,
    "supply point": 7, "garbage recycle area": 8, "restroom": 9,
    "service point": 10, "medical aid station": 11, "equipment area": 12,
    "shop": 13, "meeting point": 14, "viewing platform": 15,
    "instagram-worthy location": 16, "tunnel": 17, "valley": 18,
    "dangerous road": 19, "sharp turn": 20, "steep slope": 21, "intersection": 22,
}
ROADBOOK_COLUMNS = ("km", "type", "description")
# The format's resolution: lat/lon on a 1e-7 deg grid are at most sqrt(2) * 0.5e-7 deg
# (0.79 cm) off; elevation in cm steps from a mm start value at most 0.5 + 0.5 + 0.05 cm.
POS_TOL_M = 0.008
ELE_TOL_M = 0.011
KM_TOLERANCE = 0.01  # a roadbook may run 1% past the GPX end (e.g. the finish line)


def hav(a, b, c, d):
    R = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    return 2 * R * math.asin(math.sqrt(math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))


def resolve_type(value):
    """Accept a category name (case-insensitive) or an integer code."""
    v = value.strip()
    if v.lower() in TYPE_BY_NAME:
        return TYPE_BY_NAME[v.lower()]
    try:
        code = int(v)
    except ValueError:
        raise SystemExit(f"unknown roadbook type {value!r} (use a name or 0-22)")
    if not 0 <= code <= 22:
        raise SystemExit(f"roadbook type code out of range: {code}")
    return code


def cumulative(pts):
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + hav(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    return cum


def read_gpx(path):
    pts = []  # (lat, lon, ele_m or None)
    for i, tp in enumerate(ET.parse(path).getroot().iter(f"{{{NS}}}trkpt")):
        la, lo = float(tp.get("lat")), float(tp.get("lon"))
        e = tp.find(f"{{{NS}}}ele")
        text = (e.text or "").strip() if e is not None else ""
        try:
            ele = float(text) if text else None
        except ValueError:
            raise SystemExit(f"{path}: trackpoint {i}: elevation {text!r} is not a number")
        pts.append((la, lo, ele))
    if len(pts) < 2:
        raise SystemExit(f"{path}: need at least 2 trackpoints, found {len(pts)}")
    return fill_elevations(pts)


def fill_elevations(pts):
    """Give trackpoints without <ele> a value interpolated along the track between
    the nearest known neighbours (the nearest known value at either end), instead
    of 0 m, which would drop the profile to sea level and inflate ascent/descent."""
    known = [i for i, p in enumerate(pts) if p[2] is not None]
    missing = len(pts) - len(known)
    if not missing:
        return pts
    if not known:
        print("warning: the GPX has no elevations - the profile will be flat (0 m)")
        return [(la, lo, 0.0) for la, lo, _ in pts]
    cum = cumulative(pts)
    ele = [p[2] for p in pts]
    for i, e in enumerate(ele):
        if e is not None:
            continue
        k = bisect.bisect(known, i)
        a = known[k - 1] if k > 0 else None
        b = known[k] if k < len(known) else None
        if a is None or b is None:
            ele[i] = pts[b if a is None else a][2]
        else:
            span = cum[b] - cum[a]
            f = 0.0 if span == 0 else (cum[i] - cum[a]) / span
            ele[i] = pts[a][2] + (pts[b][2] - pts[a][2]) * f
    print(f"note: {missing} trackpoint(s) without elevation, interpolated from their neighbours")
    return [(la, lo, e) for (la, lo, _), e in zip(pts, ele)]


def read_roadbook(path):
    """[(km, type_code, description)]. The header may follow comment lines;
    any line whose first cell starts with "#" is a comment, blank lines are
    skipped, and a byte-order mark (Excel "CSV UTF-8") is ignored."""
    rows, idx = [], None
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for cells in reader:
            cells = [c.strip() for c in cells]
            if not any(cells) or cells[0].startswith("#"):
                continue
            where = f"{path}:{reader.line_num}"
            if idx is None:
                header = [c.lower() for c in cells]
                missing = [c for c in ROADBOOK_COLUMNS if c not in header]
                if missing:
                    raise SystemExit(f"{where}: header {cells} lacks {', '.join(missing)} "
                                     f"(expected {','.join(ROADBOOK_COLUMNS)})")
                idx = [header.index(c) for c in ROADBOOK_COLUMNS]
                continue
            if len(cells) <= max(idx):
                raise SystemExit(f"{where}: expected {','.join(ROADBOOK_COLUMNS)}, got {cells}")
            km_s, type_s, descr = (cells[i] for i in idx)
            try:
                km = float(km_s)
            except ValueError:
                raise SystemExit(f"{where}: km {km_s!r} is not a number")
            try:
                code = resolve_type(type_s)
            except SystemExit as e:
                raise SystemExit(f"{where}: {e}")
            rows.append((km, code, descr))
    if idx is None:
        raise SystemExit(f"{path}: no header row (expected {','.join(ROADBOOK_COLUMNS)})")
    return rows


def second_order_tokens(Q):
    # token[i] = d[i]-d[i-1], d[i]=Q[i]-Q[i-1], token[1]=d[1]
    toks, prev_d = [], 0
    for i in range(1, len(Q)):
        d = Q[i] - Q[i - 1]
        toks.append(d - prev_d)
        prev_d = d
    return toks


def encode_tracks(pts):
    n = len(pts)
    LAT = [round(p[0] * 1e7) for p in pts]
    LON = [round(p[1] * 1e7) for p in pts]
    ELEc = [round(p[2] * 100) for p in pts]  # cm
    tla = second_order_tokens(LAT)
    tlo = second_order_tokens(LON)
    tele = [ELEc[i] - ELEc[i - 1] for i in range(1, n)]  # 1st order, cm
    parts = [f"{pts[0][0]:.7f},{pts[0][1]:.7f},{round(pts[0][2] * 1000)}"]  # absolute ele in mm
    for i in range(n - 1):
        parts.append(f"{tla[i]},{tlo[i]},{tele[i]}")
    return ";".join(parts) + ";"


def decode_tracks(tracks):
    """Decode exactly as the device does — used for the self-test."""
    toks = [t for t in tracks.split(";") if t.strip()]
    la, lo, el = toks[0].split(",")
    lat, lon, ele_mm = float(la), float(lo), float(el)
    vla = vlo = 0.0
    out = [(lat, lon, ele_mm / 1000)]
    for tk in toks[1:]:
        dla, dlo, dele = (int(x) for x in tk.split(","))
        vla += dla
        vlo += dlo
        lat += vla / 1e7
        lon += vlo / 1e7
        ele_mm += dele * 10
        out.append((lat, lon, ele_mm / 1000))
    return out


def check_round_trip(pts, tracks):
    """Decode `tracks` as the device does and exit unless it matches `pts` to the
    format's resolution. Returns (max position error, max elevation error) in m."""
    dec = decode_tracks(tracks)
    if len(dec) != len(pts):
        raise SystemExit(f"self-test failed: decoded {len(dec)} points, expected {len(pts)}")
    pos = max(hav(p[0], p[1], d[0], d[1]) for p, d in zip(pts, dec))
    ele = max(abs(p[2] - d[2]) for p, d in zip(pts, dec))
    if pos > POS_TOL_M or ele > ELE_TOL_M:
        raise SystemExit(f"self-test failed: max position error {pos * 100:.2f} cm (limit {POS_TOL_M * 100:g}), "
                         f"max elevation error {ele * 100:.2f} cm (limit {ELE_TOL_M * 100:g}) - .cnx not written")
    return pos, ele


def point_at_km(pts, cum, km):
    """(lat, lon) at `km` along the track, clamped to its ends."""
    t = min(max(km * 1000.0, 0.0), cum[-1])
    i = max(1, bisect.bisect_left(cum, t))
    d0, d1 = cum[i - 1], cum[i]
    f = 0.0 if d1 == d0 else (t - d0) / (d1 - d0)
    (a_lat, a_lon, _), (b_lat, b_lon, _) = pts[i - 1], pts[i]
    return a_lat + (b_lat - a_lat) * f, a_lon + (b_lon - a_lon) * f


def place_points(rows, pts, cum):
    """[(lat, lon, type, description)] for the roadbook rows. Exits listing every
    row whose km is negative or more than KM_TOLERANCE past the track end."""
    total = cum[-1]
    bad = [(km, d) for km, _, d in rows if km < 0 or km * 1000 > total * (1 + KM_TOLERANCE)]
    if bad:
        listing = "\n".join(f"  km {km:g}: {d}" for km, d in bad)
        raise SystemExit(f"roadbook km outside the {total / 1000:.2f} km track:\n{listing}")
    placed = []
    for km, typ, d in rows:
        if km * 1000 > total:
            print(f"warning: km {km:g} ({d}) is past the {total / 1000:.2f} km track end - placed at the finish")
        placed.append((*point_at_km(pts, cum, km), typ, d))
    return placed


def route_id(tracks, points_xml):
    """8-digit numeric <Id> (the shape the device is known to accept) derived from
    the route content: a different track or any changed point gives a different Id,
    bar a 1-in-90-million collision."""
    digest = hashlib.sha1((tracks + "".join(points_xml)).encode()).hexdigest()
    return 10_000_000 + int(digest, 16) % 90_000_000


def main():
    ap = argparse.ArgumentParser(description="GPX + roadbook CSV -> native iGPSPORT .cnx")
    ap.add_argument("--gpx", type=Path, help="input GPX track (default: the one in ./inputs)")
    ap.add_argument("--roadbook", type=Path, help="roadbook CSV (default: ./inputs/roadbook.csv)")
    ap.add_argument("--out", type=Path, help="output .cnx (default: ./outputs/<gpx-name>.cnx)")
    args = ap.parse_args()

    # --- resolve inputs ---
    gpx = args.gpx
    if gpx is None:
        cands = sorted(p for p in INPUTS.glob("*.gpx") if not p.name.endswith("_roadbook.gpx"))
        if len(cands) != 1:
            raise SystemExit(f"--gpx not given and {len(cands)} candidate GPX in {INPUTS} (expected 1)")
        gpx = cands[0]
    if not gpx.exists():
        raise SystemExit(f"GPX not found: {gpx}")

    roadbook = args.roadbook
    if roadbook is None:
        default_rb = INPUTS / "roadbook.csv"
        roadbook = default_rb if default_rb.exists() else None
    if roadbook and not roadbook.exists():
        raise SystemExit(f"roadbook not found: {roadbook}")

    out = args.out or (OUTPUTS / (gpx.stem + ".cnx"))
    out.parent.mkdir(parents=True, exist_ok=True)

    # --- read + geometry ---
    pts = read_gpx(gpx)
    n = len(pts)
    cum = cumulative(pts)
    total_m = cum[-1]
    ascent = sum(max(0, pts[i][2] - pts[i - 1][2]) for i in range(1, n))
    descent = sum(min(0, pts[i][2] - pts[i - 1][2]) for i in range(1, n))

    # --- encode track + self-test round-trip ---
    tracks_str = encode_tracks(pts)
    max_pos_err, max_ele_err = check_round_trip(pts, tracks_str)
    print(f"SELF-TEST round-trip:  max position error = {max_pos_err * 100:.2f} cm   max elevation error = {max_ele_err * 100:.1f} cm")

    # --- place roadbook points by km ---
    rows = read_roadbook(roadbook) if roadbook else []
    if roadbook and not rows:
        print(f"warning: {roadbook} has a header but no points")
    points_xml = []
    for la, lo, typ, descr in place_points(rows, pts, cum):
        d = descr.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        points_xml.append(f"<Point><Lat>{la:.7f}</Lat><Lng>{lo:.7f}</Lng><Type>{typ}</Type><Descr>{d}</Descr></Point>")

    # --- write .cnx ---
    xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
        "<Route>"
        f"<Id>{route_id(tracks_str, points_xml)}</Id>"
        f"<Distance>{total_m:.2f}</Distance>"
        "<Duration></Duration>"
        f"<Ascent>{round(ascent)}</Ascent>"
        f"<Descent>{round(descent)}</Descent>"
        "<Encode>2</Encode>"
        "<Lang>0</Lang>"
        f"<TracksCount>{n}</TracksCount>"
        f"<Tracks>{tracks_str}</Tracks>"
        "<Navs/>"
        f"<Points>{''.join(points_xml)}</Points>"
        f"<PointsCount>{len(rows)}</PointsCount>"
        "</Route>\n"
    )
    out.write_text(xml, encoding="utf-8")
    print(f"\nWrote {out}")
    print(f"  TracksCount={n}  Distance={total_m / 1000:.2f} km  Ascent={round(ascent)}  Descent={round(descent)}")
    print(f"  Points={len(rows)}  (from {roadbook.name if roadbook else 'none'})")
    print(f"  size: {len(xml)} bytes")
    print("\nCopy it to the device:  cp", out, " /mnt/d/iGPSPORT/Courses/")


if __name__ == "__main__":
    main()
