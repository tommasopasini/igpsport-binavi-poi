"""Regression tests for fit_fix_clock.py.

Synthetic rides cover the edge cases (multi-lap, a fragment cut off with the
timer running, missing values, winter time). The real stale-fragment rides in
device_backup/ are checked too when present (they are local, gitignored data):
invariants on both, and a field-level diff against the 2026-09-13 repair that
synced cleanly to iGPSPORT, Strava and Komoot.

Run: python3 -m pytest experiments/test_fit_fix_clock.py
"""
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fit_fix_clock import (ACTIVITY, EVENT, FILE_ID, GARMIN_EPOCH, LAP, RECORD,  # noqa: E402
                           SESSION, FitFixError, crc16, parse, repair, ts)

REPO = Path(__file__).resolve().parent.parent
UTC = timezone.utc
T0 = datetime(2026, 8, 20, 11, 0, tzinfo=UTC)     # stale fragment
T1 = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)      # the real ride
A = (548801377, 143165577)                         # stale fragment location (made up: 46N 12E)
B = (536870912, 119304647)                         # ride location (made up: 45N 10E)
TYPES = {"enum": ("B", 0x00, 0xFF), "u16": ("H", 0x84, 0xFFFF),
         "s32": ("i", 0x85, 0x7FFFFFFF), "u32": ("I", 0x86, 0xFFFFFFFF)}


# --- synthetic FIT writer ---------------------------------------------------

def raw(dt):
    return int((dt - GARMIN_EPOCH).total_seconds())


def ms(seconds):
    return None if seconds is None else int(seconds * 1000)


def build_fit(messages):
    """[(global_num, [(field_num, type, value or None)][, compressed_ts])] -> FIT bytes.
    A message with a third element gets a compressed-timestamp header for that time."""
    body, local_of = bytearray(), {}
    for gnum, fields, *compressed in messages:
        layout = (gnum, tuple((f, t) for f, t, _ in fields))
        if layout not in local_of:
            local_of[layout] = len(local_of)
            body += bytes([0x40 | local_of[layout], 0, 0]) + struct.pack("<H", gnum)
            body.append(len(fields))
            for f, t, _ in fields:
                fmt, base, _ = TYPES[t]
                body += bytes([f, struct.calcsize(fmt), base])
        if compressed:
            assert local_of[layout] < 4, "compressed headers only carry local types 0-3"
            body.append(0x80 | local_of[layout] << 5 | compressed[0] & 0x1F)
        else:
            body.append(local_of[layout])
        for _, t, v in fields:
            fmt, _, invalid = TYPES[t]
            body += struct.pack("<" + fmt, invalid if v is None else v)
    header = bytearray(struct.pack("<BBHI4s", 14, 0x10, 2132, len(body), b".FIT")) + b"\0\0"
    struct.pack_into("<H", header, 12, crc16(header[:12]))
    out = header + body
    return bytes(out + struct.pack("<H", crc16(out)))


def rec(t, dist, pos, compressed=False):
    lat, lon = pos if pos else (None, None)
    fields = [(0, "s32", lat), (1, "s32", lon), (5, "u32", dist)]
    if compressed:
        return (RECORD, fields, raw(t))
    return (RECORD, [(253, "u32", raw(t))] + fields)


def ev(t, ev_type):
    return (EVENT, [(253, "u32", raw(t)), (0, "enum", 0), (1, "enum", ev_type)])


def summary(gnum, end, start, timer_s, dist, invalid=False, standing_s=0):
    elapsed = (end - start).total_seconds()
    timer, dist, avg = (None, None, None) if invalid else (ms(timer_s), dist, 1000)
    moving = None if invalid else ms(timer_s - standing_s)
    moving_f, avg_f = (52, 13) if gnum == LAP else (59, 14)
    fields = [(253, "u32", raw(end)), (2, "u32", raw(start)), (3, "s32", A[0]), (4, "s32", A[1]),
              (7, "u32", ms(elapsed)), (8, "u32", timer), (9, "u32", dist),
              (avg_f, "u16", avg), (moving_f, "u32", moving)]
    if gnum == SESSION:
        fields += [(29, "s32", A[0]), (30, "s32", A[1]), (31, "s32", B[0]), (32, "s32", B[1])]
    return (gnum, fields)


def ride_pos(k):
    return (B[0] + 10 * k, B[1] + 5 * k)


def ride_dist(k):
    """Cumulative cm at ride second k (1..100): carries on from the 2000 cm stale head."""
    return 2000 + 500 * (k - 1)


def build_ride(*, lap_in_stale=False, stale_stop=True, first_kept_blank=False,
               local_offset_s=3600, summary_invalid=False, stale=True, standing_s=0,
               compressed=False):
    """21 s / 20 m stale fragment at T0, then a 100 s ride starting at T1.
    Session timer = 20 s stale + 100 s ride."""
    msgs = [(FILE_ID, [(0, "enum", 4), (1, "u16", 1), (4, "u32", raw(T0 if stale else T1))])]
    if stale:
        msgs.append(ev(T0, 0))
        for k in range(21):
            msgs.append(rec(T0 + timedelta(seconds=k), 100 * k, A, compressed))
            if lap_in_stale and k == 10:   # lap button pressed inside the fragment
                msgs.append(summary(LAP, T0 + timedelta(seconds=10), T0, 10, 1000))
        if stale_stop:
            msgs.append(ev(T0 + timedelta(seconds=20), 1))
    msgs.append(ev(T1, 0))
    for k in range(1, 101):
        t = T1 + timedelta(seconds=k)
        blank = first_kept_blank and k == 1
        msgs.append(rec(t, None if blank else ride_dist(k), None if blank else ride_pos(k), compressed))
        if lap_in_stale and k == 50:
            msgs.append(summary(LAP, t, T0 + timedelta(seconds=10), 60, ride_dist(50) - 1000))
    end = T1 + timedelta(seconds=100)
    msgs.append(ev(end, 1))
    start = T0 if stale else T1 + timedelta(seconds=1)
    timer = 120 if stale else 100
    if lap_in_stale:
        msgs.append(summary(LAP, end, T1 + timedelta(seconds=50), 50, ride_dist(100) - ride_dist(50)))
    else:
        msgs.append(summary(LAP, end, start, timer, ride_dist(100), summary_invalid))
    msgs.append(summary(SESSION, end, start, timer, ride_dist(100), summary_invalid, standing_s))
    local = None if local_offset_s is None else raw(end) + local_offset_s
    msgs.append((ACTIVITY, [(253, "u32", raw(end)), (0, "u32", ms(timer)), (5, "u32", local)]))
    return build_fit(msgs)


def messages(data):
    return [it for kind, it in parse(data)[1] if kind == "msg"]


def of(data, gnum):
    return [m for m in messages(data) if m.gnum == gnum]


def assert_crcs(data):
    assert struct.unpack("<H", data[12:14])[0] == crc16(data[:12])
    assert struct.unpack("<H", data[-2:])[0] == crc16(data[:-2])
    assert data[0] + struct.unpack("<I", data[4:8])[0] + 2 == len(data)


# --- synthetic edge cases ---------------------------------------------------

def test_single_lap_ride_is_repaired():
    r = repair(build_ride())
    assert_crcs(r.data)
    (lap,), (session,) = of(r.data, LAP), of(r.data, SESSION)
    for m in (lap, session):
        assert ts(m.get(2)) == T1 + timedelta(seconds=1)
        assert m.get(7) == 99_000                    # end kept at T1+100, start moved
        assert m.get(8) == 100_000                   # 120 s minus the 20 s stale timer
        assert m.get(9) == ride_dist(100) - 2000
        assert (m.get(3), m.get(4)) == ride_pos(1)
    records = of(r.data, RECORD)
    assert records[0].get(5) == 0 and records[-1].get(5) == session.get(9)
    assert all(m.ts >= T1 for m in messages(r.data) if m.ts)
    assert session.get(14) == round(session.get(9) * 10000 / session.get(8))


def test_multi_lap_drops_stale_lap_and_patches_only_the_straddling_one():
    orig = build_ride(lap_in_stale=True)
    r = repair(orig)
    assert_crcs(r.data)
    assert r.dropped["lap"] == 1
    l1, l2 = of(r.data, LAP)
    assert ts(l1.get(2)) == T1 + timedelta(seconds=1)
    assert l1.get(7) == 49_000                       # T1+1 .. T1+50
    assert l1.get(8) == 50_000                       # 60 s minus the 10 s after the stale lap
    assert l1.get(9) == ride_dist(50) - 2000         # minus the 10 m after the stale lap
    assert l1.get(52) == 50_000
    orig_l2 = next(m for m in of(orig, LAP) if m.ts == l2.ts)
    assert l2.raw == orig_l2.raw                     # lap after the jump: untouched
    (session,) = of(r.data, SESSION)
    for f in (7, 8, 9):
        assert l1.get(f) + l2.get(f) == session.get(f)


def test_timer_still_running_at_cut_is_subtracted():
    r = repair(build_ride(stale_stop=False))
    assert r.stale_timer_s == 20
    (session,), (activity,) = of(r.data, SESSION), of(r.data, ACTIVITY)
    assert session.get(8) == 100_000 and activity.get(0) == 100_000


def test_moving_time_loses_only_the_stale_share():
    # 15 s standing with the timer running: moving 105 s of a 120 s timer
    r = repair(build_ride(standing_s=15))
    (session,) = of(r.data, SESSION)
    assert session.get(8) == 100_000 and session.get(59) == 85_000


def test_first_kept_record_without_distance_or_gps():
    r = repair(build_ride(first_kept_blank=True))
    assert r.dist_offset_cm == 2000                  # from the stale head, not the blank record
    records = of(r.data, RECORD)
    assert records[0].get(5) is None and records[1].get(5) == ride_dist(2) - 2000
    (session,) = of(r.data, SESSION)
    assert (session.get(3), session.get(4)) == ride_pos(2)
    assert session.get(9) == ride_dist(100) - 2000


def test_invalid_summary_totals_stay_invalid():
    r = repair(build_ride(summary_invalid=True))
    assert_crcs(r.data)
    for m in of(r.data, LAP) + of(r.data, SESSION):
        assert m.get(8) is None and m.get(9) is None
        assert m.get(52 if m.gnum == LAP else 59) is None
        assert m.get(13 if m.gnum == LAP else 14) is None
        assert ts(m.get(2)) == T1 + timedelta(seconds=1) and m.get(7) == 99_000


@pytest.mark.parametrize("offset_s, name", [(3600, "2026-09-13-07-00-01.fit"),
                                            (7200, "2026-09-13-08-00-01.fit")])
def test_filename_uses_offset_recorded_in_file(offset_s, name):
    r = repair(build_ride(local_offset_s=offset_s))
    assert (r.utc_offset_s, r.utc_offset_src, r.filename) == (offset_s, "activity.local_timestamp", name)


def test_missing_local_timestamp_needs_explicit_offset():
    data = build_ride(local_offset_s=None)
    with pytest.raises(FitFixError, match="--utc-offset"):
        repair(data)
    assert repair(data, utc_offset_h=1).filename == "2026-09-13-07-00-01.fit"


def test_compressed_timestamp_records_are_parsed_and_kept():
    r = repair(build_ride(compressed=True))
    assert_crcs(r.data)
    records = of(r.data, RECORD)                     # re-parsed from the repaired file
    assert [m.ts for m in records] == [T1 + timedelta(seconds=k) for k in range(1, 101)]
    assert all(m.get(253) is None for m in records)  # still compressed, still anchored
    (session,) = of(r.data, SESSION)
    assert ts(session.get(2)) == T1 + timedelta(seconds=1) and session.get(7) == 99_000
    assert r.dropped["record"] == 21


def test_ride_without_stale_fragment_is_rejected():
    with pytest.raises(FitFixError, match="nothing to fix"):
        repair(build_ride(stale=False))


# --- real stale-fragment rides (local data, skipped when absent) -------------

FIXTURES = {
    "2026-08-20-13-31-59.fit": dict(filename="2026-09-13-07-48-34.fit", elapsed=16_374_000,
                                    timer=13_247_000, dist=7_467_734, stale_timer=44),
    "2026-06-13-10-17-40.fit": dict(filename="2026-06-14-08-04-24.fit", elapsed=34_726_000,
                                    timer=24_007_000, dist=13_200_760, stale_timer=50),
}


def fixture(name):
    path = REPO / "device_backup" / name
    if not path.exists():
        pytest.skip(f"{path} not present (local ride data)")
    return path.read_bytes()


def stale_spans_s(msgs, after):
    """Independent recount of timer seconds in the stale head."""
    total, start = 0, None
    for m in msgs:
        if m.gnum != EVENT or m.get(0) != 0 or not m.ts or m.ts >= after:
            continue
        if m.get(1) == 0 and start is None:
            start = m.ts
        elif m.get(1) in (1, 4, 8, 9) and start is not None:
            total += (m.ts - start).total_seconds()
            start = None
    return total


@pytest.mark.parametrize("name", FIXTURES)
def test_real_ride_invariants(name):
    orig = fixture(name)
    want = FIXTURES[name]
    r = repair(orig)
    assert_crcs(r.data)
    (o_session,), (session,) = of(orig, SESSION), of(r.data, SESSION)
    records = of(r.data, RECORD)

    assert r.filename == want["filename"]
    assert all(m.ts >= r.after for m in messages(r.data) if m.ts)
    assert ts(session.get(2)) == records[0].ts == r.new_start
    # the device's own end is preserved, only the start moved
    assert session.get(2) * 1000 + session.get(7) == o_session.get(2) * 1000 + o_session.get(7)
    assert r.stale_timer_s == stale_spans_s(messages(orig), r.after) == want["stale_timer"]
    assert session.get(8) == o_session.get(8) - want["stale_timer"] * 1000
    assert session.get(8) <= session.get(7) and session.get(59) <= session.get(8)
    assert records[0].get(5) == 0 and records[-1].get(5) == session.get(9)
    positions = [(m.get(0), m.get(1)) for m in records if m.get(0) is not None]
    assert (session.get(3), session.get(4)) == positions[0]
    assert (session.get(29), session.get(30)) == (max(p[0] for p in positions), max(p[1] for p in positions))
    assert (session.get(31), session.get(32)) == (min(p[0] for p in positions), min(p[1] for p in positions))
    assert (session.get(7), session.get(8), session.get(9)) == (want["elapsed"], want["timer"], want["dist"])
    (lap,) = of(r.data, LAP)
    assert [lap.get(f) for f in (2, 7, 8, 9)] == [session.get(f) for f in (2, 7, 8, 9)]


def test_matches_the_repair_that_synced():
    """Only total_elapsed_time may differ from the 2026-09-13 file the device
    re-synced to iGPSPORT/Strava/Komoot: it now ends at the device's stop time
    (10:21:28) instead of the last record (10:21:25)."""
    good_path = REPO / "outputs" / "2026-09-13-07-48-34.fit"
    if not good_path.exists():
        pytest.skip(f"{good_path} not present (local ride data)")
    new = messages(repair(fixture("2026-08-20-13-31-59.fit")).data)
    good = messages(good_path.read_bytes())
    assert len(new) == len(good)
    changed = {}
    for a, b in zip(new, good):
        assert (a.gnum, list(a.fields)) == (b.gnum, list(b.fields))
        if a.raw != b.raw:
            for f in a.fields:
                if a.get(f) != b.get(f):
                    changed[(a.gnum, f)] = (b.get(f), a.get(f))
    assert changed == {(LAP, 7): (16_371_000, 16_374_000), (SESSION, 7): (16_371_000, 16_374_000)}


def test_june_ride_keeps_the_same_track_as_fit_split_day():
    """The June repair was made by fit_split_day.py (since deleted); the track
    must be the same."""
    old_path = REPO / "outputs" / "2026-06-13-10-17-40.fixed.fit"
    if not old_path.exists():
        pytest.skip(f"{old_path} not present (local ride data)")
    new = repair(fixture("2026-06-13-10-17-40.fit")).data
    track = lambda data: [(m.get(253), m.get(0), m.get(1)) for m in of(data, RECORD)]  # noqa: E731
    assert track(new) == track(old_path.read_bytes())
