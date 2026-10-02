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
from fit_fix_clock import (ACTIVITY, DEVICE_INFO, EVENT, FILE_ID, GARMIN_EPOCH, LAP,  # noqa: E402
                           RECORD, SESSION, FitFixError, crc16, parse, repair, ts)

REPO = Path(__file__).resolve().parent.parent
UTC = timezone.utc
T0 = datetime(2026, 8, 20, 11, 0, tzinfo=UTC)     # stale fragment
T1 = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)      # the real ride
A = (548801377, 143165577)                         # stale fragment location (made up: 46N 12E)
B = (536870912, 119304647)                         # ride location (made up: 45N 10E)
TYPES = {"enum": ("B", 0x00, 0xFF), "u8": ("B", 0x02, 0xFF), "s8": ("b", 0x01, 0x7F),
         "u16": ("H", 0x84, 0xFFFF), "s32": ("i", 0x85, 0x7FFFFFFF), "u32": ("I", 0x86, 0xFFFFFFFF),
         "s64": ("q", 0x8E, 0x7FFFFFFFFFFFFFFF)}


# --- synthetic FIT writer ---------------------------------------------------

def raw(dt):
    return int((dt - GARMIN_EPOCH).total_seconds())


def ms(seconds):
    return None if seconds is None else int(seconds * 1000)


def build_fit(messages, header_size=14):
    """[(global_num, [(field_num, type, value or None)][, compressed_ts])] -> FIT bytes.
    A message with a third element gets a compressed-timestamp header for that time;
    header_size 12 writes the legacy header without a CRC."""
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
    header = bytearray(struct.pack("<BBHI4s", header_size, 0x10, 2132, len(body), b".FIT"))
    if header_size == 14:
        header += struct.pack("<H", crc16(header))
    out = header + body
    return bytes(out + struct.pack("<H", crc16(out)))


def rec(t, dist, pos, compressed=False, extra=()):
    lat, lon = pos if pos else (None, None)
    fields = [(0, "s32", lat), (1, "s32", lon), (5, "u32", dist), *extra]
    if compressed:
        return (RECORD, fields, raw(t))
    return (RECORD, [(253, "u32", raw(t))] + fields)


def ev(t, ev_type):
    return (EVENT, [(253, "u32", raw(t)), (0, "enum", 0), (1, "enum", ev_type)])


def summary(gnum, end, start, timer_s, dist, invalid=False, standing_s=0, laps=1, extra=()):
    elapsed = (end - start).total_seconds()
    timer, dist, avg = (None, None, None) if invalid else (ms(timer_s), dist, 1000)
    moving = None if invalid else ms(timer_s - standing_s)
    moving_f, avg_f = (52, 13) if gnum == LAP else (59, 14)
    fields = [(253, "u32", raw(end)), (2, "u32", raw(start)), (3, "s32", A[0]), (4, "s32", A[1]),
              (7, "u32", ms(elapsed)), (8, "u32", timer), (9, "u32", dist),
              (avg_f, "u16", avg), (moving_f, "u32", moving)]
    if gnum == SESSION:
        fields += [(29, "s32", A[0]), (30, "s32", A[1]), (31, "s32", B[0]), (32, "s32", B[1]),
                   (26, "u16", laps)]
    return (gnum, fields + list(extra))


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
    msgs.append(summary(SESSION, end, start, timer, ride_dist(100), summary_invalid, standing_s,
                        laps=3 if lap_in_stale else 1))
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
    assert session.get(26) == 2                      # num_laps: the stale lap is gone


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


def ride_from(segments, head=(), header_size=14, stats=None):
    """Segments [(start, seconds, position)] recorded at 1 Hz and 5 m/s, each
    between a timer start and stop, with one lap and a session spanning them all.
    `head` messages go right after file_id; `stats(k, segment)` gives extra record
    fields and is used to fill the summaries' statistics as the device would."""
    msgs = [(FILE_ID, [(0, "enum", 4), (1, "u16", 1), (4, "u32", raw(segments[0][0]))]), *head]
    dist = timer = 0
    for n, (start, secs, pos) in enumerate(segments):
        msgs.append(ev(start, 0))
        for k in range(secs + 1):
            extra = stats(k, n) if stats else ()
            msgs.append(rec(start + timedelta(seconds=k), dist + 500 * k, pos, extra=extra))
        dist, timer = dist + 500 * secs, timer + secs
        msgs.append(ev(start + timedelta(seconds=secs), 1))
    end = segments[-1][0] + timedelta(seconds=segments[-1][1])
    lap_extra = session_extra = ()
    if stats:
        values = [stats(k, n) for n, (_, secs, _) in enumerate(segments) for k in range(secs + 1)]
        lap_extra, session_extra = device_stats(values)
    msgs.append(summary(LAP, end, segments[0][0], timer, dist, extra=lap_extra))
    msgs.append(summary(SESSION, end, segments[0][0], timer, dist, extra=session_extra))
    msgs.append((ACTIVITY, [(253, "u32", raw(end)), (0, "u32", ms(timer)), (5, "u32", raw(end) + 3600)]))
    return build_fit(msgs, header_size)


def device_stats(records):
    """Lap and session statistic fields computed over `records` (lists of extra
    record fields) the way the BiNavi does."""
    col = lambda f: [v for fields in records for fn, _, v in fields if fn == f]  # noqa: E731
    hr, cad, alt, temp, power, speed = col(3), col(4), col(2), col(13), col(7), col(73)
    nonzero = [c for c in cad if c]
    v = {"avg_hr": sum(hr) // len(hr), "max_hr": max(hr), "min_hr": min(hr),
         "avg_cad": sum(nonzero) // len(nonzero), "max_cad": max(cad), "max_speed": max(speed),
         "max_power": max(power), "max_temp": max(temp), "avg_temp": round(sum(temp) / len(temp)),
         "max_alt": max(alt), "min_alt": min(alt), "avg_alt": round(sum(alt) / len(alt))}
    common = [(11, "u16", 1000), (10, "u32", 5000)]                 # calories, cycles
    lap = common + [(15, "u8", v["avg_hr"]), (16, "u8", v["max_hr"]), (63, "u8", v["min_hr"]),
                    (17, "u8", v["avg_cad"]), (18, "u8", v["max_cad"]), (14, "u16", v["max_speed"]),
                    (111, "u32", v["max_speed"]), (19, "u16", 333), (20, "u16", v["max_power"]),
                    (51, "s8", v["max_temp"]), (50, "s8", v["avg_temp"]), (43, "u16", v["max_alt"]),
                    (62, "u16", v["min_alt"]), (42, "u16", v["avg_alt"]), (21, "u16", 100), (22, "u16", 40)]
    session = common + [(16, "u8", v["avg_hr"]), (17, "u8", v["max_hr"]), (64, "u8", v["min_hr"]),
                        (18, "u8", v["avg_cad"]), (19, "u8", v["max_cad"]), (15, "u16", v["max_speed"]),
                        (125, "u32", v["max_speed"]), (20, "u16", 333), (21, "u16", v["max_power"]),
                        (58, "s8", v["max_temp"]), (57, "s8", v["avg_temp"]), (50, "u16", v["max_alt"]),
                        (71, "u16", v["min_alt"]), (49, "u16", v["avg_alt"]), (22, "u16", 100), (23, "u16", 40)]
    return lap, session


TB = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)          # a second stale fragment


def test_every_stale_fragment_before_the_ride_is_dropped():
    r = repair(ride_from([(T0, 20, A), (TB, 15, A), (T1, 100, B)]))
    assert r.after == T1 and r.dropped["record"] == 21 + 16
    records = of(r.data, RECORD)
    assert records[0].ts == T1 and records[0].get(5) == 0 and len(records) == 101
    (session,) = of(r.data, SESSION)
    assert session.get(8) == 100_000 and session.get(9) == 500 * 100


def test_overnight_stop_in_a_multi_day_ride_is_kept():
    day2 = T1 + timedelta(hours=14)
    r = repair(ride_from([(T0, 20, A), (T1, 1200, B), (day2, 100, B)]))
    assert r.after == T1
    assert [m.ts for m in of(r.data, RECORD)][-1] == day2 + timedelta(seconds=100)


def test_long_stretch_before_the_jump_is_not_taken_for_a_fragment():
    data = ride_from([(T0, 1200, A), (T1, 100, B)])          # 20 min of real riding
    with pytest.raises(FitFixError, match="--max-stale-minutes 15"):
        repair(data)
    assert repair(data, max_stale_minutes=30).after == T1


def test_system_time_is_not_a_date_and_only_stale_device_info_is_restamped():
    head = [(DEVICE_INFO, [(253, "u32", 1000), (0, "u8", 0)]),             # seconds since power-on
            (DEVICE_INFO, [(253, "u32", raw(T0)), (0, "u8", 1)])]          # stale
    data = ride_from([(T0, 20, A), (T1, 100, B)], head=head)
    r = repair(data)
    assert r.after == T1
    assert [m.get(253) for m in of(r.data, DEVICE_INFO)] == [1000, raw(T1)]


def test_legacy_12_byte_header():
    r = repair(ride_from([(T0, 20, A), (T1, 100, B)], header_size=12))
    assert r.data[0] == 12 and r.data[0] + struct.unpack("<I", r.data[4:8])[0] + 2 == len(r.data)
    assert struct.unpack("<H", r.data[-2:])[0] == crc16(r.data[:-2])
    assert of(r.data, RECORD)[0].ts == T1


def ride_stats(k, segment):
    """The stale fragment (segment 0) climbs from 1000 m with a racing heart, hot,
    spinning and powerful; the ride is at 100 m."""
    def alt(m):
        return round((m + 500) * 5)
    if segment == 0:
        return ((2, "u16", alt(1000 + k)), (3, "u8", 190), (4, "u8", 120), (13, "s8", 40),
                (7, "u16", 900), (73, "u32", 9000))
    return ((2, "u16", alt(100 + k % 10)), (3, "u8", 100 + k % 50), (4, "u8", 80 * (k % 2)),
            (13, "s8", 20 + k % 5), (7, "u16", 200 + k % 100), (73, "u32", 5000))


def test_statistics_are_recomputed_without_the_fragment():
    r = repair(ride_from([(T0, 20, A), (T1, 100, B)], stats=ride_stats))
    kept = [ride_stats(k, 1) for k in range(101)]
    want_lap, want_session = device_stats(kept)
    (lap,), (session,) = of(r.data, LAP), of(r.data, SESSION)
    # calories, cycles, avg power and ascent/descent follow other rules: checked below
    for m, want, other_rules in ((lap, want_lap, {10, 11, 19, 21, 22}),
                                 (session, want_session, {10, 11, 20, 22, 23})):
        for f, _, v in want:
            if f not in other_rules:
                assert m.get(f) == v, (m.gnum, f)
    assert lap.get(20) == session.get(21) == 299         # max power without the fragment
    assert lap.get(19) == session.get(20) == 333         # avg power: left as the device had it
    assert session.get(11) == round(1000 * 100 / 120)   # calories by remaining timer share
    assert session.get(10) == round(5000 * 100 / 120)   # cycles likewise
    assert lap.get(21) == session.get(22) == 100 - 20   # ascent minus the fragment's 20 m climb
    assert lap.get(22) == session.get(23) == 40         # it never descended


def test_signed_64_bit_fields_read_as_signed():
    (m,) = messages(build_fit([(0, [(4, "s64", -5)])]))
    assert m.get(4) == -5


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
    """What differs from the 2026-09-13 file the device re-synced to iGPSPORT,
    Strava and Komoot, field by field, and why: elapsed now ends at the device's
    stop time (10:21:28, not the last record); calories lose the fragment's 44 s
    share; ascent its 3 m climb; and altitude no longer reports the fragment's
    ~1077 m (max 1045 m, min -21.8 m, avg 52 m from the device's smoothing) but
    the ride's own 59-218 m."""
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
    want = {7: (16_371_000, 16_374_000), 11: (1303, 1299)}
    want.update({(LAP, 21): (705, 702), (SESSION, 22): (705, 702)})
    for lap_f, session_f, old, new in ((42, 49, 2760, 3148), (43, 50, 7727, 3592), (62, 71, 2391, 2795)):
        want.update({(LAP, lap_f): (old, new), (SESSION, session_f): (old, new)})
    for f in (7, 11):
        want.update({(LAP, f): want[f], (SESSION, f): want.pop(f)})
    assert changed == want


def test_june_ride_keeps_the_same_track_as_fit_split_day():
    """The June repair was made by fit_split_day.py (since deleted); the track
    must be the same."""
    old_path = REPO / "outputs" / "2026-06-13-10-17-40.fixed.fit"
    if not old_path.exists():
        pytest.skip(f"{old_path} not present (local ride data)")
    new = repair(fixture("2026-06-13-10-17-40.fit")).data
    track = lambda data: [(m.get(253), m.get(0), m.get(1)) for m in of(data, RECORD)]  # noqa: E731
    assert track(new) == track(old_path.read_bytes())
