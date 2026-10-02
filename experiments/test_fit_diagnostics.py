"""Tests for the read-only diagnostics fit_inspect.py and fit_analyze.py.
Run: python3 -m pytest experiments/"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fit_analyze  # noqa: E402
import fit_inspect  # noqa: E402
from test_fit_fix_clock import build_ride  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def run(module, monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", [module.__name__, *map(str, args)])
    module.main()
    return capsys.readouterr().out


@pytest.mark.parametrize("compressed", [False, True])
def test_inspect_shows_the_jump_and_summaries(tmp_path, monkeypatch, capsys, compressed):
    fit = tmp_path / "ride.fit"
    fit.write_bytes(build_ride(compressed=compressed))
    out = run(fit_inspect, monkeypatch, capsys, fit)
    assert "first ts: 2026-08-20 11:00:00+00:00" in out
    assert "last  ts: 2026-09-13 06:01:40+00:00" in out
    assert "-> 2026-09-13 06:00:00+00:00 (event)" in out          # the jump, as a gap
    assert "'record': 121" in out
    assert "activity  ts=2026-09-13 06:01:40+00:00 timer=120000" in out


@pytest.mark.parametrize("compressed", [False, True])
def test_analyze_reports_the_stale_fragment(tmp_path, monkeypatch, capsys, compressed):
    fit = tmp_path / "ride.fit"
    fit.write_bytes(build_ride(compressed=compressed))
    out = run(fit_analyze, monkeypatch, capsys, fit)
    assert "time jump: 2026-08-20 11:00:20+00:00 -> 2026-09-13 06:00:00+00:00" in out
    assert "records before it: 21, after it: 100" in out
    assert "[session] offset=" in out and "[lap] offset=" in out


def test_analyze_without_a_jump(tmp_path, monkeypatch, capsys):
    fit = tmp_path / "ride.fit"
    fit.write_bytes(build_ride(stale=False))
    assert "no forward time jump over 6 h" in run(fit_analyze, monkeypatch, capsys, fit)


@pytest.mark.parametrize("module", [fit_inspect, fit_analyze])
def test_real_rides_run(monkeypatch, capsys, module):
    rides = sorted((REPO / "device_backup").glob("*.fit"))
    if not rides:
        pytest.skip("no local rides in device_backup/")
    for ride in rides:
        assert run(module, monkeypatch, capsys, ride)
