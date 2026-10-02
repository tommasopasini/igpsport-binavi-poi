"""Tests for gpx_to_roadbook.py.  Run: python3 -m pytest tests/"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import generate_cnx as g  # noqa: E402
import gpx_to_roadbook as r  # noqa: E402

STEP = 1e-3   # ~111 m between trackpoints along a meridian


def track(points):
    pts = [(la, lo) for la, lo in points]
    return pts, g.cumulative([(la, lo, 0.0) for la, lo in pts])


def out_and_back(lane_m=3.0):
    """10 segments north, then back south on the other lane (lane_m east):
    trackpoint i going out sits beside trackpoint 21 - i coming back."""
    east = lane_m / (111_195 * 0.7071)          # metres -> degrees of longitude at 45 N
    return track([(45 + i * STEP, 11.0) for i in range(11)] +
                 [(45 + i * STEP, 11.0 + east) for i in range(10, -1, -1)])


def test_straight_track_has_one_pass():
    pts, cum = track([(45 + i * STEP, 11.0) for i in range(11)])
    (km, off), = r.track_passes(45 + 3.5 * STEP, 11.00002, pts, cum)
    assert km == pytest.approx(cum[3] / 1000 + (cum[4] - cum[3]) / 2000, abs=1e-3)
    assert off == pytest.approx(1.6, abs=0.1)


def test_out_and_back_road_has_two_passes():
    pts, cum = out_and_back()
    passes = r.track_passes(45 + 2 * STEP, 11.0, pts, cum)
    assert [round(km, 2) for km, _ in passes] == [round(cum[2] / 1000, 2), round(cum[19] / 1000, 2)]


def test_neighbouring_stretch_is_not_a_pass():
    pts, cum = out_and_back(lane_m=17.0)       # like Giara's switchback, 17 m apart
    assert len(r.track_passes(45 + 2 * STEP, 11.0, pts, cum)) == 1


def test_waypoint_order_picks_the_pass():
    passes = [(1.0, 0.0), (9.0, 0.5)]
    assert r.choose_pass(passes, 0.0) == (1.0, 0.0)
    assert r.choose_pass(passes, 5.0) == (9.0, 0.5)
    assert r.choose_pass(passes, 12.0) == (9.0, 0.5)     # out of order: the last pass


def write_gpx(path, trkpts, wpts):
    body = "".join(f'<wpt lat="{la:.7f}" lon="{lo:.7f}"><name>{n}</name><sym>{s}</sym></wpt>'
                   for la, lo, n, s in wpts)
    body += "<trk><trkseg>" + "".join(f'<trkpt lat="{la:.7f}" lon="{lo:.7f}"/>' for la, lo in trkpts)
    path.write_text(f'<?xml version="1.0"?><gpx xmlns="{g.NS}" version="1.1">{body}</trkseg></trk></gpx>',
                    encoding="utf-8")


def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["gpx_to_roadbook.py", *map(str, args)])
    r.main()


def test_csv_follows_route_order_and_reads_back(tmp_path, monkeypatch):
    pts, cum = out_and_back()
    spot = (45 + 2 * STEP, 11.0)              # trackpoint 2 going out, 19 coming back
    write_gpx(tmp_path / "t.gpx", pts, [(*spot, "Fountain out", "Drinking Water"),
                                        (45 + 10 * STEP, 11.0, "Turnaround", "Summit"),
                                        (*spot, "Fountain back", "Drinking Water")])
    out = tmp_path / "rb.csv"
    run_main(monkeypatch, "--gpx", tmp_path / "t.gpx", "--out", out)
    rows = g.read_roadbook(out)                # the comment rows must not get in the way
    assert [(round(km, 2), descr) for km, _, descr in rows] == [
        (round(cum[2] / 1000, 2), "Fountain out"),
        (round(cum[10] / 1000, 2), "Turnaround"),
        (round(cum[19] / 1000, 2), "Fountain back"),
    ]


def test_giara_waypoints_land_on_their_km(tmp_path, monkeypatch):
    """Giara is a loop through Chievo; each waypoint name starts with its true km
    (local data, skipped when absent)."""
    gpx = REPO / "outputs" / "Giara_2026_N_TOCO_roadbook.gpx"
    if not gpx.exists():
        pytest.skip("local route data not present")
    out = tmp_path / "giara.csv"
    run_main(monkeypatch, "--gpx", gpx, "--out", out)
    rows = g.read_roadbook(out)
    assert len(rows) == 16
    for km, _, descr in rows:
        assert km == pytest.approx(float(re.match(r"[\d.]+", descr)[0]), abs=0.05), descr
