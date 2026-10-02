#!/usr/bin/env python3
"""Repair a BiNavi/iGPSPORT .fit whose recording was appended to a stale,
never-saved activity fragment from an earlier day.

The device can keep an unsaved recording open across sessions: the new ride's
messages are appended to the old fragment, so file_id/session/lap carry the OLD
start time (and the old start position), and total_elapsed_time spans the whole
gap between the two days. Apps then date the ride wrongly and draw a straight
line from the old fragment's location to the new one.

This rewrites the file keeping only the ride after the stale fragment(s): every
short (at most --max-stale-minutes) stretch before a forward time jump of more
than --gap-hours is stale; the first longer stretch is the ride, and any later
jump (an overnight stop in a multi-day ride) is left alone.
  * drops the stale data messages and any lap that ended inside the stale head
    (definitions are kept, they are still used)
  * rebases record distances so the ride starts at 0
  * patches the session and the lap that straddles the jump: start_time,
    start_position, total_elapsed_time (the device's own end is kept, only the
    start moves), total_timer_time, total_moving_time, total_distance and avg
    speed lose exactly the stale share; the session bounding box is recomputed.
    Laps that start after the jump are left untouched.
  * the same summaries' statistics: heart rate, cadence, max speed/power and max
    temperature are recomputed from the kept records with the device's own rules
    (verified on BiNavi rides); altitude and average temperature, which the
    device smooths, are recomputed only where the stale fragment pushed them
    outside the kept records' range; calories and cycles are scaled by the
    remaining timer; ascent/descent lose the fragment's own climb (an estimate);
    session num_laps loses the dropped laps. Average power is left as is.
  * fixes stale file_id.time_created and device_info.timestamp, activity timer
  * recomputes header CRC and file CRC
  * names the output after the true start in device local time, read from
    activity.local_timestamp (override with --utc-offset)

Read-only on the input; writes a new file.
"""
import argparse
import math
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GARMIN_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
BASE_SIZE = {0: 1, 1: 1, 2: 1, 3: 2, 4: 2, 5: 4, 6: 4, 7: 1,
             8: 4, 9: 8, 10: 1, 11: 2, 12: 4, 13: 1, 14: 8, 15: 8, 16: 8}
FMT = {(1, False): "B", (2, False): "H", (4, False): "I", (8, False): "Q",
       (1, True): "b", (2, True): "h", (4, True): "i", (8, True): "q"}
SIGNED = {1, 3, 5, 14}
ZERO_INVALID = {10, 11, 12, 16}          # uint8z/16z/32z/64z use 0 as "no value"
FILE_ID, DEVICE_INFO, RECORD, EVENT, LAP, SESSION, ACTIVITY = 0, 23, 20, 21, 19, 18, 34
MSG_NAME = {RECORD: "record", EVENT: "event", LAP: "lap"}
DROPPABLE = {RECORD, EVENT, 142, 317}
TIMER_EVENT, START = 0, 0
STOP_TYPES = {1, 4, 8, 9}                # stop, stop_all, stop_disable, stop_disable_all
MOVING_TIME = {LAP: 52, SESSION: 59}
AVG_SPEED = {LAP: (13, 110), SESSION: (14, 124)}   # avg_speed, enhanced_avg_speed
ASCENT = {LAP: (21, 22), SESSION: (22, 23)}         # total_ascent, total_descent
CALORIES, CYCLES, NUM_LAPS = 11, 10, 26
SYSTEM_TIME_LIMIT = 0x10000000   # a FIT date_time below this counts seconds since power-on
CLIMB_STEP_M = 1.0               # altitude hysteresis for the stale fragment's climb
# Recomputed from the kept records with the device's rule, verified on real BiNavi
# rides: (lap field, session field, record fields in order of preference, rule).
EXACT_STATS = (
    (15, 16, (3,), "floor_mean"),            # avg_heart_rate (130.94 -> 130)
    (16, 17, (3,), "max"),                   # max_heart_rate
    (63, 64, (3,), "min"),                   # min_heart_rate
    (17, 18, (4,), "floor_mean_nonzero"),    # avg_cadence (zeros excluded, 67.50 -> 67)
    (18, 19, (4,), "max"),                   # max_cadence
    (14, 15, (73, 6), "max"),                # max_speed
    (111, 125, (73, 6), "max"),              # enhanced_max_speed
    (20, 21, (7,), "max"),                   # max_power
    (51, 58, (13,), "max"),                  # max_temperature
)
# Smoothed by the device, so kept unless outside what the kept records allow (the
# stale fragment's altitude drags them), then recomputed from those records.
BOUNDED_STATS = (
    (43, 50, (2, 78), "max"),                # max_altitude
    (62, 71, (2, 78), "min"),                # min_altitude
    (42, 49, (2, 78), "mean"),               # avg_altitude
    (50, 57, (13,), "mean"),                 # avg_temperature
)


class FitFixError(Exception):
    pass


def ts(v):
    return None if v is None else GARMIN_EPOCH + timedelta(seconds=v)


def _invalid(base, size):
    if base in ZERO_INVALID:
        return 0
    bits = 8 * size
    return (1 << (bits - 1)) - 1 if base in SIGNED else (1 << bits) - 1


class Msg:
    """One data message: its raw bytes plus where each field sits inside them."""

    def __init__(self, gnum, raw, fields, endian):
        self.gnum = gnum
        self.raw = bytearray(raw)
        self.fields = fields      # fnum -> (offset_in_raw, size, base_type)
        self.endian = endian
        self.dropped = False
        self.implied_ts = None    # set for a compressed-timestamp header (no field 253)

    def _slot(self, fnum):
        """(offset, struct format, invalid value, base) for a scalar field, else None."""
        f = self.fields.get(fnum)
        if not f:
            return None
        off, size, base = f
        if size != BASE_SIZE.get(base, 1):
            return None
        fmt = FMT.get((size, base in SIGNED))
        return (off, fmt, _invalid(base, size), base) if fmt else None

    def get(self, fnum):
        """Field value, or None when the field is absent or holds FIT's invalid value."""
        slot = self._slot(fnum)
        if slot is None:
            return None
        off, fmt, invalid, _ = slot
        v = struct.unpack_from(self.endian + fmt, self.raw, off)[0]
        return None if v == invalid else v

    def fits(self, fnum, value):
        slot = self._slot(fnum)
        if slot is None or value is None:
            return False
        _, fmt, invalid, base = slot
        bits = 8 * struct.calcsize(fmt)
        if base in SIGNED:
            lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        else:
            lo, hi = 0, (1 << bits) - 1
        return lo <= value <= hi and value != invalid

    def set(self, fnum, value):
        """Write value; False if the field is absent or value is None."""
        slot = self._slot(fnum)
        if slot is None or value is None:
            return False
        if not self.fits(fnum, value):
            raise ValueError(f"msg {self.gnum} field {fnum}: {value} does not fit")
        off, fmt, _, _ = slot
        struct.pack_into(self.endian + fmt, self.raw, off, value)
        return True

    @property
    def time(self):
        """Raw date_time from field 253 or the compressed header, absolute or not."""
        v = self.get(253)
        return self.implied_ts if v is None else v

    @property
    def ts_raw(self):
        """Absolute date_time, or None (also for system time since power-on)."""
        v = self.time
        return v if v is not None and v >= SYSTEM_TIME_LIMIT else None

    @property
    def ts(self):
        return ts(self.ts_raw)


def parse(data):
    """Return (header, [items]) where an item is ('def', raw) or ('msg', Msg)."""
    hdr_size = data[0]
    data_size = struct.unpack("<I", data[4:8])[0]
    pos, end = hdr_size, hdr_size + data_size
    defs, items, last_ts = {}, [], None
    while pos < end:
        start = pos
        rh = data[pos]
        pos += 1
        if rh & 0x80:  # compressed-timestamp header: a data message, 2-bit local type
            local, offset = (rh >> 5) & 0x03, rh & 0x1F
        elif rh & 0x40:  # definition message
            local = rh & 0x0F
            endian = ">" if data[pos + 1] == 1 else "<"
            gnum = struct.unpack(endian + "H", data[pos + 2:pos + 4])[0]
            nfields = data[pos + 4]
            pos += 5
            fields = []
            for _ in range(nfields):
                fields.append((data[pos], data[pos + 1], data[pos + 2]))
                pos += 3
            dev = []
            if rh & 0x20:
                ndev = data[pos]
                pos += 1
                for _ in range(ndev):
                    dev.append((data[pos], data[pos + 1], data[pos + 2]))
                    pos += 3
            defs[local] = (gnum, fields, endian, dev)
            items.append(("def", bytes(data[start:pos])))
            continue
        else:  # data message
            local, offset = rh & 0x0F, None
        gnum, fields, endian, dev = defs[local]
        layout, off = {}, 1  # offset 1: past the record header byte
        for fnum, fsize, ftype in fields:
            layout[fnum] = (off, fsize, ftype & 0x1F)
            off += fsize
        for fnum, fsize, ftype in dev:
            off += fsize
        pos = start + off
        msg = Msg(gnum, data[start:pos], layout, endian)
        msg.offset = start
        if offset is not None and last_ts is not None:
            msg.implied_ts = last_ts + ((offset - last_ts) & 0x1F)
        if msg.time is not None:
            last_ts = msg.time
        items.append(("msg", msg))
    if pos != end:
        raise FitFixError(f"FIT records overrun the data section ({pos} != {end})")
    return data[:hdr_size], items


def crc16(data, crc=0):
    table = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)
    for byte in data:
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[byte & 0xF]
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[(byte >> 4) & 0xF]
    return crc


def find_jumps(msgs, gap_hours):
    """[(timestamp before, timestamp after)] for every forward jump larger than
    gap_hours between messages with an absolute timestamp, in file order."""
    jumps, prev = [], None
    for m in msgs:
        t = m.ts
        if t is None:
            continue
        if prev is not None and (t - prev).total_seconds() > gap_hours * 3600:
            jumps.append((prev, t))
        prev = t
    return jumps


def stale_cut(msgs, gap_hours, max_stale_minutes):
    """(last stale timestamp, first ride timestamp): the end of the run of short
    stretches - stale fragments - that precede the ride, each followed by a jump
    of more than gap_hours."""
    jumps = find_jumps(msgs, gap_hours)
    if not jumps:
        raise FitFixError(f"no time jump > {gap_hours}h found - nothing to fix")
    start = next(m.ts for m in msgs if m.ts)
    cut = None
    for before, after in jumps:
        span_min = (before - start).total_seconds() / 60
        if span_min > max_stale_minutes:
            break
        cut, start = (before, after), after
    if cut is None:
        raise FitFixError(f"the part before the jump at {jumps[0][1]} spans {span_min:.0f} min, "
                          f"longer than a stale fragment (--max-stale-minutes {max_stale_minutes:g}); "
                          "raise the limit if it really is one")
    return cut


def timer_spans(msgs, before, after):
    """(start, stop) spans the timer ran inside the stale head. A span still open
    at the cut (fragment lost to a power-off) is closed at the last stale timestamp."""
    events = [(m.ts, m.get(1)) for m in msgs
              if m.gnum == EVENT and m.get(0) == TIMER_EVENT and m.ts and m.ts < after]
    spans, open_start = [], None
    for t, ev_type in sorted(events, key=lambda e: e[0]):
        if ev_type == START:
            if open_start is None:
                open_start = t
        elif ev_type in STOP_TYPES and open_start is not None:
            spans.append((open_start, t))
            open_start = None
    if open_start is not None:
        spans.append((open_start, before))
    return spans


def seconds_after(spans, lo):
    """Seconds of spans falling after lo (all of them when lo is None)."""
    total = 0.0
    for a, b in spans:
        a = a if lo is None else max(a, lo)
        if b > a:
            total += (b - a).total_seconds()
    return total


def samples(records, fields):
    """Each record's value of the first of `fields` it has."""
    out = []
    for r in records:
        v = next((r.get(f) for f in fields if r.get(f) is not None), None)
        if v is not None:
            out.append(v)
    return out


def summarize(values, rule):
    if rule == "max":
        return max(values)
    if rule == "min":
        return min(values)
    if rule == "floor_mean":
        return math.floor(sum(values) / len(values))
    if rule == "floor_mean_nonzero":
        nonzero = [v for v in values if v]
        return math.floor(sum(nonzero) / len(nonzero)) if nonzero else None
    return round(sum(values) / len(values))


def climb(altitudes, step):
    """(ascent, descent) in the altitudes' own units, counting moves of at least `step`."""
    up = down = 0
    ref = altitudes[0] if altitudes else None
    for a in altitudes[1:]:
        if a - ref >= step:
            up, ref = up + a - ref, a
        elif ref - a >= step:
            down, ref = down + ref - a, a
    return up, down


@dataclass
class Result:
    data: bytes
    before: datetime
    after: datetime
    dropped: Counter
    stale_timer_s: float
    dist_offset_cm: int
    new_start: datetime
    utc_offset_s: int
    utc_offset_src: str
    session_elapsed_ms: int

    @property
    def local_start(self):
        return self.new_start + timedelta(seconds=self.utc_offset_s)

    @property
    def filename(self):
        return f"{self.local_start:%Y-%m-%d-%H-%M-%S}.fit"


def repair(data, gap_hours=6.0, utc_offset_h=None, max_stale_minutes=15.0):
    header, items = parse(data)
    msgs = [it for kind, it in items if kind == "msg"]
    before, after = stale_cut(msgs, gap_hours, max_stale_minutes)

    def stale(m):
        return m.ts is not None and m.ts < after

    records = [m for m in msgs if m.gnum == RECORD and m.ts and not stale(m)]
    if not records:
        raise FitFixError("no records left after the jump")
    stale_records = [m for m in msgs if m.gnum == RECORD and stale(m)]
    spans = timer_spans(msgs, before, after)
    stale_timer_s = seconds_after(spans, None)

    # distance the device accumulated in the stale head
    stale_dists = [d for d in (m.get(5) for m in stale_records) if d is not None]
    dist_offset = stale_dists[-1] if stale_dists else 0
    first_kept = next((d for d in (m.get(5) for m in records) if d is not None), None)
    if first_kept is not None and first_kept < dist_offset:
        dist_offset = 0          # odometer restarted at the jump: nothing to rebase

    def dist_at(when):
        """Cumulative stale-head distance at `when` (0 when None or before any record)."""
        d = 0
        if when is not None:
            for m in stale_records:
                if m.ts > when:
                    break
                d = m.get(5) if m.get(5) is not None else d
        return d

    positions = [(m.get(0), m.get(1)) for m in records
                 if m.get(0) is not None and m.get(1) is not None]
    pos0 = positions[0] if positions else None
    new_start_raw = records[0].ts_raw
    new_start = records[0].ts

    if utc_offset_h is not None:
        utc_offset_s, utc_offset_src = round(utc_offset_h * 3600), "--utc-offset"
    else:
        act = next((m for m in msgs if m.gnum == ACTIVITY), None)
        if act is None or act.get(5) is None or act.ts_raw is None:
            raise FitFixError("file has no activity.local_timestamp - pass --utc-offset")
        utc_offset_s, utc_offset_src = act.get(5) - act.ts_raw, "activity.local_timestamp"

    lap_ends = sorted(m.ts for m in msgs if m.gnum == LAP and m.ts)
    stale_laps = sum(1 for m in msgs if m.gnum == LAP and stale(m))

    def patch_summary(m):
        # the stale share of this message: everything after the previous lap's end
        # (a lap button press inside the stale head), or the whole head
        window = None
        if m.gnum == LAP:
            window = max((t for t in lap_ends if t < m.ts), default=None)
        stale_ms = round(seconds_after(spans, window) * 1000)
        stale_cm = max(0, dist_offset - dist_at(window))

        old_start, old_elapsed = m.get(2), m.get(7)
        if old_start is not None and old_elapsed is not None:
            elapsed = old_start * 1000 + old_elapsed - new_start_raw * 1000
        elif m.ts_raw is not None:
            elapsed = (m.ts_raw - new_start_raw) * 1000
        else:
            elapsed = None
        if elapsed is not None:
            m.set(7, max(0, elapsed))
        m.set(2, new_start_raw)
        if pos0:
            m.set(3, pos0[0])
            m.set(4, pos0[1])

        old_timer = timer = m.get(8)
        if timer is not None:
            timer = max(0, timer - stale_ms)
            m.set(8, timer)
        if old_timer and timer is not None:           # calories, cycles: by timer share
            for f in (CALORIES, CYCLES):
                if m.get(f) is not None:
                    m.set(f, round(m.get(f) * timer / old_timer))
        dist = m.get(9)
        if dist is not None:
            dist = max(0, dist - stale_cm)
            m.set(9, dist)
        moving = m.get(MOVING_TIME[m.gnum])
        if moving is not None:
            moving = max(0, moving - stale_ms)
            m.set(MOVING_TIME[m.gnum], min(moving, timer) if timer is not None else moving)
        if m.gnum == SESSION and positions:
            lats, lons = [p[0] for p in positions], [p[1] for p in positions]
            m.set(29, max(lats))                          # nec_lat
            m.set(30, max(lons))                          # nec_long
            m.set(31, min(lats))                          # swc_lat
            m.set(32, min(lons))                          # swc_long
        if timer and dist is not None:
            avg = round(dist * 10000 / timer)             # cm / ms -> mm/s
            for f in AVG_SPEED[m.gnum]:
                if m.get(f) is not None and m.fits(f, avg):
                    m.set(f, avg)

        # statistics over the records this summary keeps (a lap: up to its end)
        kept = [r for r in records if m.gnum == SESSION or r.ts <= m.ts]
        which = 0 if m.gnum == LAP else 1
        for stat in EXACT_STATS:
            f, values = stat[which], samples(kept, stat[2])
            if m.get(f) is not None and values:
                v = summarize(values, stat[3])
                if m.fits(f, v):
                    m.set(f, v)
        for stat in BOUNDED_STATS:
            f, values = stat[which], samples(kept, stat[2])
            v = m.get(f)
            if v is not None and values and not min(values) <= v <= max(values):
                v = summarize(values, stat[3])
                if m.fits(f, v):
                    m.set(f, v)
        # altitude raw units are 1/5 m: the fragment's own climb, from where this summary's
        # stale share starts
        dropped_alts = samples([r for r in stale_records if window is None or r.ts > window], (2, 78))
        for f, gain in zip(ASCENT[m.gnum], climb(dropped_alts, CLIMB_STEP_M * 5)):
            if m.get(f) is not None:
                m.set(f, max(0, m.get(f) - round(gain / 5)))
        if m.gnum == SESSION and m.get(NUM_LAPS) is not None:
            m.set(NUM_LAPS, max(0, m.get(NUM_LAPS) - stale_laps))
        return elapsed

    dropped = Counter()
    session_elapsed = None
    for m in msgs:
        if m.gnum in DROPPABLE and stale(m):
            m.dropped = True
            dropped[MSG_NAME.get(m.gnum, str(m.gnum))] += 1
        elif m.gnum == LAP and stale(m):                  # lap ended inside the stale head
            m.dropped = True
            dropped["lap"] += 1
        elif m.gnum == LAP:
            start = ts(m.get(2))
            if m.ts is not None and (start is None or start < after):
                patch_summary(m)
        elif m.gnum == SESSION:
            session_elapsed = patch_summary(m)
        elif m.gnum == FILE_ID:
            created = ts(m.get(4))
            if created is not None and m.get(4) >= SYSTEM_TIME_LIMIT and created < after:
                m.set(4, new_start_raw)                   # time_created
        elif m.gnum == DEVICE_INFO:
            if stale(m):
                m.set(253, new_start_raw)
        elif m.gnum == ACTIVITY:
            timer = m.get(0)
            if timer is not None:
                m.set(0, max(0, timer - round(stale_timer_s * 1000)))

    # rebase distances last: dist_at() above reads the original stale values
    if dist_offset:
        for m in records:
            d = m.get(5)
            if d is not None:
                m.set(5, max(0, d - dist_offset))

    body = bytearray()
    for kind, it in items:
        if kind == "def":
            body += it
        elif not it.dropped:
            body += it.raw
    out = bytearray(header)
    struct.pack_into("<I", out, 4, len(body))
    if len(header) >= 14:                                 # a 12-byte header has no CRC
        struct.pack_into("<H", out, 12, crc16(out[:12]))
    out += body
    out += struct.pack("<H", crc16(out))

    return Result(bytes(out), before, after, dropped, stale_timer_s, dist_offset,
                  new_start, utc_offset_s, utc_offset_src, session_elapsed)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?", help="output .fit (default: derived from new start time)")
    ap.add_argument("--gap-hours", type=float, default=6.0,
                    help="minimum forward time jump treated as a stale head (default 6)")
    ap.add_argument("--utc-offset", type=float, default=None,
                    help="device local-time offset in hours, used only for the derived "
                         "filename (default: read from the file's activity.local_timestamp)")
    ap.add_argument("--max-stale-minutes", type=float, default=15.0,
                    help="longest stretch before a jump still treated as a stale fragment "
                         "(default 15); a longer one is the ride itself")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = open(args.src, "rb").read()
    try:
        r = repair(data, args.gap_hours, args.utc_offset, args.max_stale_minutes)
    except FitFixError as e:
        sys.exit(str(e))

    print(f"stale head: ends {r.before}, ride resumes {r.after} "
          f"(gap {(r.after - r.before).total_seconds() / 3600:.1f}h)")
    print("dropping " + ", ".join(f"{n} {k}" for k, n in sorted(r.dropped.items())))
    print(f"stale head timer time: {r.stale_timer_s:.0f}s")
    print(f"distance rebased by -{r.dist_offset_cm / 100:.2f}m")
    print(f"new start: {r.new_start} (device local {r.local_start:%Y-%m-%d %H:%M:%S}, "
          f"UTC{r.utc_offset_s / 3600:+g}h from {r.utc_offset_src})")
    if r.session_elapsed_ms is not None:
        print(f"elapsed: {timedelta(milliseconds=r.session_elapsed_ms)}")

    dst = args.dst or r.filename
    if args.dry_run:
        print(f"dry run - would write {dst}")
        return
    open(dst, "wb").write(r.data)
    print(f"wrote {dst} ({len(r.data)} bytes, was {len(data)})")


if __name__ == "__main__":
    main()
