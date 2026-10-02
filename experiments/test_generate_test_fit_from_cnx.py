"""Tests for generate_test_fit_from_cnx.py.  Run: python3 -m pytest experiments/"""
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate_cnx as g  # noqa: E402
import generate_test_fit_from_cnx as t  # noqa: E402

GPX = "{http://www.topografix.com/GPX/1/1}"
TRACK = [(-33.9 + i * 1e-3, -70.6 + i * 1e-3, 500.0 + i) for i in range(5)]   # southern/western


def app_cnx(path):
    """An app-made route: start address without <Type>, then typed points."""
    points = ('<Point><Lat>-33.9000000</Lat><Lng>-70.6000000</Lng><Descr>Av. Siempre Viva 742</Descr></Point>'
              '<Point><Lat>-33.8990000</Lat><Lng>-70.5990000</Lng><Type>13</Type><Descr>Bar &amp; Caf&#233;</Descr></Point>')
    path.write_text("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n<Route><Id>1</Id>"
                    f"<Tracks>{g.encode_tracks(TRACK)}</Tracks><Points>{points}</Points></Route>\n",
                    encoding="utf-8")
    return path


def test_read_cnx_drops_the_untyped_start_and_keeps_negative_coordinates(tmp_path):
    track, points = t.read_cnx(app_cnx(tmp_path / "r.cnx"))
    assert points == [(-33.899, -70.599, "Bar & Café")]
    assert track == g.decode_tracks(g.encode_tracks(TRACK))


def test_gpx_time_rolls_over_midnight():
    assert t.gpx_time(20 * 3600 + 61) == "2026-06-14T04:01:01Z"


def test_gpx_is_valid_with_single_escaping_and_ordered_times(tmp_path):
    track = [(45 + i * 0.1, 11.0, 100.0) for i in range(10)]  # 100 km: ~20 h on foot from 08:00
    out = tmp_path / "t.gpx"
    t.write_gpx(out, track, [(45.0, 11.0, "Bar & Café <1>")])
    text = out.read_text(encoding="utf-8")
    assert "Bar &amp; Café &lt;1&gt;" in text and "&amp;amp;" not in text
    root = ET.parse(out).getroot()
    assert root.find(f"{GPX}wpt/{GPX}name").text == "Bar & Café <1>"
    times = [datetime.fromisoformat(e.text.replace("Z", "+00:00")) for e in root.iter(f"{GPX}time")]
    assert times == sorted(times) and times[-1].day == 14
