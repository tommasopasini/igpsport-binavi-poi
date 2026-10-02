"""Tests for generate_cnx.py.  Run: python3 -m pytest tests/"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import generate_cnx as g  # noqa: E402


def write(tmp_path, name, text, encoding="utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return path


def gpx_file(tmp_path, eles, step=1e-3):
    """Trackpoints ~111 m apart along a meridian; eles are raw <trkpt> bodies."""
    body = "".join(f'<trkpt lat="{45 + i * step:.7f}" lon="11.0000000">{e}</trkpt>'
                   for i, e in enumerate(eles))
    return write(tmp_path, "track.gpx", f'<?xml version="1.0"?><gpx xmlns="{g.NS}" version="1.1">'
                                        f"<trk><trkseg>{body}</trkseg></trk></gpx>")


def ele(v):
    return f"<ele>{v}</ele>"


def line(n=11, step=1e-3):
    pts = [(45 + i * step, 11.0, 0.0) for i in range(n)]
    return pts, g.cumulative(pts)


# --- roadbook CSV ------------------------------------------------------------

def test_example_roadbook_yields_its_points():
    path = REPO / "roadbook.example.csv"
    data = [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")][1:]
    rows = g.read_roadbook(path)
    assert len(rows) == len(data) > 0
    assert rows[0] == (3.2, 7, "Water fountain")


def test_byte_order_mark_is_ignored(tmp_path):
    path = write(tmp_path, "rb.csv", "km,type,description\n1.5,shop,Bar\n", encoding="utf-8-sig")
    assert g.read_roadbook(path) == [(1.5, 13, "Bar")]


def test_comments_blank_rows_and_header_spelling(tmp_path):
    path = write(tmp_path, "rb.csv", '# intro\n\n KM , Type ,Description\n"# quoted, comment",,\n'
                                     "1,0,a\n,,\n  \n2,restroom,b\n")
    assert g.read_roadbook(path) == [(1.0, 0, "a"), (2.0, 9, "b")]


@pytest.mark.parametrize("text, message", [
    ("kilometre,type,description\n1,0,a\n", "lacks km"),
    ("# only comments\n", "no header row"),
    ("km,type,description\n3.2,supply point\n", ":2: expected km,type,description"),
    ("km,type,description\nabc,0,a\n", ":2: km 'abc' is not a number"),
    ("km,type,description\n\n1,fountain,a\n", ":3: unknown roadbook type"),
])
def test_bad_roadbook_names_the_line(tmp_path, text, message):
    with pytest.raises(SystemExit, match=message):
        g.read_roadbook(write(tmp_path, "rb.csv", text))


# --- GPX elevations -----------------------------------------------------------

def test_missing_elevations_are_interpolated_along_the_track(tmp_path):
    pts = g.read_gpx(gpx_file(tmp_path, [ele(100), "", ele(""), ele(130), "<ele> </ele>"]))
    assert [round(p[2], 6) for p in pts] == [100, 110, 120, 130, 130]


def test_leading_missing_elevation_takes_the_first_known(tmp_path):
    pts = g.read_gpx(gpx_file(tmp_path, ["", ele(50), ele(60)]))
    assert [p[2] for p in pts] == [50, 50, 60]


def test_gpx_without_elevations_is_flat(tmp_path, capsys):
    pts = g.read_gpx(gpx_file(tmp_path, ["", "", ""]))
    assert [p[2] for p in pts] == [0, 0, 0]
    assert "no elevations" in capsys.readouterr().out


def test_non_numeric_elevation_is_rejected(tmp_path):
    with pytest.raises(SystemExit, match="trackpoint 1: elevation 'n/a'"):
        g.read_gpx(gpx_file(tmp_path, [ele(1), ele("n/a")]))


# --- round-trip self-test ------------------------------------------------------

def test_round_trip_passes_at_the_format_resolution():
    # off the 1e-7 deg and cm grids on purpose: the worst rounding the format allows
    pts = [(45 + i * 1e-4 + 0.49e-7, 11 + i * 1e-4 + 0.49e-7, 100.0049 + i * 0.0149) for i in range(2000)]
    pos, ele_err = g.check_round_trip(pts, g.encode_tracks(pts))
    assert pos <= g.POS_TOL_M and ele_err <= g.ELE_TOL_M


@pytest.mark.parametrize("shift", ["lat", "ele"])
def test_round_trip_rejects_a_codec_error(shift):
    pts = [(45 + i * 1e-4, 11.0, 100.0) for i in range(100)]
    first, rest = g.encode_tracks(pts).split(";", 1)
    la, lo, el = first.split(",")
    if shift == "lat":   # 1e-6 deg = 11 cm, which the old 50 cm check let through
        first = f"{float(la) + 1e-6:.7f},{lo},{el}"
    else:                # 2 cm
        first = f"{la},{lo},{int(el) + 20}"
    with pytest.raises(SystemExit, match="self-test failed"):
        g.check_round_trip(pts, f"{first};{rest}")


# --- placing roadbook points -----------------------------------------------------

def test_point_at_km_interpolates_and_clamps():
    pts, cum = line()
    assert g.point_at_km(pts, cum, cum[1] / 2000) == pytest.approx((45.0005, 11.0))
    assert g.point_at_km(pts, cum, 0) == (45.0, 11.0)
    assert g.point_at_km(pts, cum, cum[-1] / 1000) == pytest.approx(pts[-1][:2])


def test_km_outside_the_track_is_rejected_with_every_offender():
    pts, cum = line()
    total_km = cum[-1] / 1000
    rows = [(0.5, 0, "fine-row"), (total_km * 1.02, 0, "typo-row"), (-1, 0, "negative-row")]
    with pytest.raises(SystemExit) as exc:
        g.place_points(rows, pts, cum)
    message = str(exc.value)
    assert "typo-row" in message and "negative-row" in message and "fine-row" not in message


def test_km_slightly_past_the_end_goes_to_the_finish(capsys):
    pts, cum = line()
    (placed,) = g.place_points([(cum[-1] / 1000 * 1.005, 0, "finish")], pts, cum)
    assert placed[:2] == pytest.approx(pts[-1][:2])
    assert "past the" in capsys.readouterr().out


def test_route_id_is_eight_digits_and_follows_the_content():
    a = g.route_id("1,2,3;", ["<Point>a</Point>"])
    assert a == g.route_id("1,2,3;", ["<Point>a</Point>"])
    assert 10_000_000 <= a <= 99_999_999
    assert a != g.route_id("1,2,3;", ["<Point>b</Point>"])
    assert a != g.route_id("1,2,4;", ["<Point>a</Point>"])


# --- end to end ----------------------------------------------------------------------

def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["generate_cnx.py", *map(str, args)])
    g.main()


def test_readme_quick_start_produces_the_example_points(tmp_path, monkeypatch):
    rows = g.read_roadbook(REPO / "roadbook.example.csv")
    # flat track at 300 m covering the example's last km, one point missing its elevation
    n = int(max(km for km, *_ in rows) * 1000 / 111) + 10
    gpx = gpx_file(tmp_path, [ele(300) if i != n // 2 else "" for i in range(n)])
    out = tmp_path / "out.cnx"
    run_main(monkeypatch, "--gpx", gpx, "--roadbook", REPO / "roadbook.example.csv", "--out", out)
    xml = out.read_text(encoding="utf-8")
    assert re.search(r"<PointsCount>(\d+)</PointsCount>", xml)[1] == str(len(rows))
    assert xml.count("<Point>") == len(rows)
    assert "<Ascent>0</Ascent>" in xml and "<Descent>0</Descent>" in xml
    assert re.search(r"<Id>(\d{8})</Id>", xml)[1] != "20260000"


@pytest.mark.parametrize("gpx, roadbook, reference", [
    ("Giara_2026_N_TOCO.gpx", "giara_roadbook.csv", "Giara.cnx"),
    ("TAV_TEST.gpx", "TAV_TEST_roadbook.csv", "TAV_TEST.cnx"),
])
def test_real_routes_unchanged_apart_from_the_id(tmp_path, monkeypatch, gpx, roadbook, reference):
    """The .cnx files in outputs/ came from the previous version and load on the
    device; only <Id> may differ (local data, skipped when absent)."""
    paths = [REPO / "inputs" / gpx, REPO / "inputs" / roadbook, REPO / "outputs" / reference]
    if not all(p.exists() for p in paths):
        pytest.skip("local route data not present")
    out = tmp_path / reference
    run_main(monkeypatch, "--gpx", paths[0], "--roadbook", paths[1], "--out", out)
    strip = lambda x: re.sub(r"<Id>\d+</Id>", "", x)  # noqa: E731
    assert strip(out.read_text(encoding="utf-8")) == strip(paths[2].read_text(encoding="utf-8"))
