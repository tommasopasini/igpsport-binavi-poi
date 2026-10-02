"""Tests for build_roadbook_gpx.py.  Run: python3 -m pytest tests/"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import build_roadbook_gpx as b  # noqa: E402
import generate_cnx as g  # noqa: E402


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def track_gpx(path, n):
    body = "".join(f'<trkpt lat="{45 + i * 1e-3:.7f}" lon="11.0000000"><ele>100</ele></trkpt>' for i in range(n))
    return write(path, f'<?xml version="1.0"?><gpx xmlns="{g.NS}" version="1.1">'
                       f"<trk><trkseg>{body}</trkseg></trk></gpx>")


def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["build_roadbook_gpx.py", *map(str, args)])
    b.main()


def test_waypoints_sit_where_generate_cnx_places_the_points(tmp_path, monkeypatch):
    gpx = track_gpx(tmp_path / "t.gpx", 30)                  # ~3.2 km
    roadbook = write(tmp_path / "rb.csv", "km,type,description\n0.5,supply point,Fountain\n2.25,19,! Gravel\n")
    out = tmp_path / "out.gpx"
    run_main(monkeypatch, "--gpx", gpx, "--roadbook", roadbook, "--out", out)
    wpts = ET.parse(out).getroot().findall(f"{{{g.NS}}}wpt")
    pts = g.read_trkpts(gpx)
    cum = g.cumulative(pts)
    assert [w.findtext(f"{{{g.NS}}}name") for w in wpts] == ["0.5 Fountain", "2.25 ! Gravel"]
    assert [w.findtext(f"{{{g.NS}}}sym") for w in wpts] == ["Drinking Water", "Danger Area"]
    for w, km in zip(wpts, (0.5, 2.25)):
        lat, lon = g.point_at_km(pts, cum, km)
        assert (w.get("lat"), w.get("lon")) == (f"{lat:.6f}", f"{lon:.6f}")


def test_km_outside_the_track_is_rejected_like_in_generate_cnx(tmp_path, monkeypatch):
    gpx = track_gpx(tmp_path / "t.gpx", 30)
    roadbook = write(tmp_path / "rb.csv", "km,type,description\n9.0,0,Typo\n")
    with pytest.raises(SystemExit, match="outside the"):
        run_main(monkeypatch, "--gpx", gpx, "--roadbook", roadbook, "--out", tmp_path / "out.gpx")


@pytest.mark.parametrize("n", [0, 1])
def test_track_without_two_points_is_reported_not_crashed_on(tmp_path, monkeypatch, n):
    gpx = track_gpx(tmp_path / "t.gpx", n)
    roadbook = write(tmp_path / "rb.csv", "km,type,description\n0.1,0,a\n")
    with pytest.raises(SystemExit, match="need at least 2"):
        run_main(monkeypatch, "--gpx", gpx, "--roadbook", roadbook, "--out", tmp_path / "out.gpx")
