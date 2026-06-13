#!/usr/bin/env python3
"""Generate a native iGPSPORT BiNavi `.cnx` route from a GPX track plus a
roadbook CSV of points-of-interest, so the points show up on the device during
navigation (something the official app makes hard).

Track encoding: lat/lon as 2nd-order delta (1e-7 deg), elevation as 1st-order
delta (cm) — see docs in BINAVI_NOTES.md. A round-trip self-test asserts the
re-decoded track matches the GPX to < 0.5 cm before the file is written.

Usage:
    python generate_cnx.py [--gpx PATH] [--roadbook PATH] [--out PATH]

Defaults (so day-to-day work stays entirely local):
    --gpx       the single *.gpx in ./inputs (excluding *_roadbook.gpx)
    --roadbook  ./inputs/roadbook.csv if present, else no points
    --out       ./outputs/<gpx-name>.cnx

Roadbook CSV columns: km,type,description   (see roadbook.example.csv)
`type` is a category name (case-insensitive) or its integer code.
"""
import argparse
import csv
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


def read_gpx(path):
    pts = []  # (lat, lon, ele_m)
    for tp in ET.parse(path).getroot().iter(f"{{{NS}}}trkpt"):
        la, lo = float(tp.get("lat")), float(tp.get("lon"))
        e = tp.find(f"{{{NS}}}ele")
        pts.append((la, lo, float(e.text) if e is not None else 0.0))
    if len(pts) < 2:
        raise SystemExit(f"{path}: need at least 2 trackpoints, found {len(pts)}")
    return pts


def read_roadbook(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("km") is None or row["km"].strip().startswith("#"):
                continue
            rows.append((float(row["km"]), resolve_type(row["type"]), row["description"].strip()))
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
    cum = [0.0]
    for i in range(1, n):
        cum.append(cum[-1] + hav(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    total_m = cum[-1]
    ascent = sum(max(0, pts[i][2] - pts[i - 1][2]) for i in range(1, n))
    descent = sum(min(0, pts[i][2] - pts[i - 1][2]) for i in range(1, n))

    # --- encode track + self-test round-trip ---
    tracks_str = encode_tracks(pts)
    dec = decode_tracks(tracks_str)
    assert len(dec) == n, f"count mismatch {len(dec)} != {n}"
    max_pos_err = max(hav(pts[i][0], pts[i][1], dec[i][0], dec[i][1]) for i in range(n))
    max_ele_err = max(abs(pts[i][2] - dec[i][2]) for i in range(n))
    print(f"SELF-TEST round-trip:  max position error = {max_pos_err * 100:.2f} cm   max elevation error = {max_ele_err * 100:.1f} cm")
    assert max_pos_err < 0.5, "position error too large!"
    assert max_ele_err < 0.5, "elevation error too large!"

    # --- interpolate roadbook points by km ---
    def at_km(km):
        t = km * 1000.0
        if t <= 0:
            return pts[0][0], pts[0][1]
        if t >= total_m:
            return pts[-1][0], pts[-1][1]
        for i in range(1, n):
            if cum[i - 1] <= t <= cum[i]:
                d0, d1 = cum[i - 1], cum[i]
                f = 0.0 if d1 == d0 else (t - d0) / (d1 - d0)
                return pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * f, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * f
        return pts[-1][0], pts[-1][1]

    rows = read_roadbook(roadbook) if roadbook else []
    points_xml = []
    for km, typ, descr in rows:
        la, lo = at_km(km)
        d = descr.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        points_xml.append(f"<Point><Lat>{la:.7f}</Lat><Lng>{lo:.7f}</Lng><Type>{typ}</Type><Descr>{d}</Descr></Point>")

    # --- write .cnx ---
    xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
        "<Route>"
        "<Id>20260000</Id>"
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
