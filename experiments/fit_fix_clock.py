#!/usr/bin/env python3
"""Repair a BiNavi/iGPSPORT .fit whose recording was appended to a stale,
never-saved activity fragment from an earlier day.

The device can keep an unsaved recording open across sessions: the new ride's
messages are appended to the old fragment, so file_id/session/lap carry the OLD
start time (and the old start position), and total_elapsed_time spans the whole
gap between the two days. Apps then date the ride wrongly and draw a straight
line from the old fragment's location to the new one.

This rewrites the file keeping only the segment after the time discontinuity:
  * drops the stale data messages (definitions are kept, they are still used)
  * rebases record distances so the ride starts at 0
  * fixes file_id.time_created, device_info.timestamp, session/lap start_time,
    start_position, total_elapsed_time, total_timer_time, total_distance,
    avg speed, and the session bounding box
  * recomputes header CRC and file CRC

Read-only on the input; writes a new file.
"""
import argparse
import struct
import sys
from datetime import datetime, timedelta, timezone

GARMIN_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
BASE_SIZE = {0: 1, 1: 1, 2: 1, 3: 2, 4: 2, 5: 4, 6: 4, 7: 1,
             8: 4, 9: 8, 10: 1, 11: 2, 12: 4, 13: 1, 14: 8, 15: 8, 16: 8}
FMT = {(1, False): "B", (2, False): "H", (4, False): "I", (8, False): "Q",
       (1, True): "b", (2, True): "h", (4, True): "i", (8, True): "q"}
SIGNED = {1, 3, 5}
FILE_ID, DEVICE_INFO, RECORD, EVENT, LAP, SESSION, ACTIVITY = 0, 23, 20, 21, 19, 18, 34
INVALID32 = 0xFFFFFFFF


def ts(v):
    return None if v in (None, INVALID32) else GARMIN_EPOCH + timedelta(seconds=v)


class Msg:
    """One data message: its raw bytes plus where each field sits inside them."""

    def __init__(self, gnum, raw, fields, endian):
        self.gnum = gnum
        self.raw = bytearray(raw)
        self.fields = fields      # fnum -> (offset_in_raw, size, base_type)
        self.endian = endian
        self.dropped = False

    def get(self, fnum):
        f = self.fields.get(fnum)
        if not f:
            return None
        off, size, base = f
        fmt = FMT.get((BASE_SIZE.get(base, 1), base in SIGNED))
        if not fmt or size != BASE_SIZE.get(base, 1):
            return None
        return struct.unpack_from(self.endian + fmt, self.raw, off)[0]

    def set(self, fnum, value):
        f = self.fields.get(fnum)
        if not f:
            return False
        off, size, base = f
        fmt = FMT.get((BASE_SIZE.get(base, 1), base in SIGNED))
        if not fmt or size != BASE_SIZE.get(base, 1):
            return False
        struct.pack_into(self.endian + fmt, self.raw, off, value)
        return True

    @property
    def ts(self):
        return ts(self.get(253))


def parse(data):
    """Return (header, [items]) where an item is ('def', raw) or ('msg', Msg)."""
    hdr_size = data[0]
    data_size = struct.unpack("<I", data[4:8])[0]
    pos, end = hdr_size, hdr_size + data_size
    defs, items = {}, []
    while pos < end:
        start = pos
        rh = data[pos]
        pos += 1
        if rh & 0x40:  # definition message
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
        else:  # data message
            local = rh & 0x0F
            gnum, fields, endian, dev = defs[local]
            layout, off = {}, 1  # offset 1: past the record header byte
            for fnum, fsize, ftype in fields:
                layout[fnum] = (off, fsize, ftype & 0x1F)
                off += fsize
            for fnum, fsize, ftype in dev:
                off += fsize
            pos = start + off
            items.append(("msg", Msg(gnum, data[start:pos], layout, endian)))
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


def find_jump(items, gap_hours):
    """First forward jump larger than gap_hours between timestamped messages."""
    prev = None
    for i, (kind, it) in enumerate(items):
        if kind != "msg":
            continue
        t = it.ts
        if t is None:
            continue
        if prev is not None and (t - prev).total_seconds() > gap_hours * 3600:
            return i, prev, t
        prev = t
    return None, None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?", help="output .fit (default: derived from new start time)")
    ap.add_argument("--gap-hours", type=float, default=6.0,
                    help="minimum forward time jump treated as a stale head (default 6)")
    ap.add_argument("--utc-offset", type=float, default=2.0,
                    help="device local-time offset, used only for the derived filename")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = open(args.src, "rb").read()
    header, items = parse(data)

    jump_at, before, after = find_jump(items, args.gap_hours)
    if jump_at is None:
        sys.exit(f"no time jump > {args.gap_hours}h found - nothing to fix")
    print(f"stale head: ends {before}, ride resumes {after} "
          f"(gap {(after - before).total_seconds() / 3600:.1f}h)")

    # --- 1. mark stale data messages: the ride data recorded before the jump.
    # file_id / device_info also carry the stale time but are patched, not dropped.
    droppable = {RECORD, EVENT, 142, 317}
    keep_msgs = []
    stale_events = []
    dropped = 0
    idx = -1
    for kind, it in items:
        if kind != "msg":
            continue
        idx += 1
        t = it.ts
        if t is not None and t < after and it.gnum in droppable:
            it.dropped = True
            dropped += 1
            if it.gnum == EVENT:
                stale_events.append((t, it.get(0), it.get(1)))
        else:
            keep_msgs.append(it)
    print(f"dropping {dropped} stale data messages")

    # timer time the device accumulated inside the stale head (start->stop pairs)
    stale_timer, open_start = 0, None
    for t, ev, ev_type in sorted(stale_events):
        if ev != 0:
            continue
        if ev_type == 0:                      # start
            open_start = t
        elif open_start is not None:          # stop / stop_all
            stale_timer += (t - open_start).total_seconds()
            open_start = None
    print(f"stale head timer time: {stale_timer:.0f}s")

    records = [m for m in keep_msgs if m.gnum == RECORD and m.ts]
    if not records:
        sys.exit("no records left after the jump")
    new_start = records[0].ts
    new_start_raw = records[0].get(253)
    last_ts = records[-1].ts

    # --- 2. rebase distances so the kept ride starts at zero
    dist_offset = records[0].get(5) or 0
    if dist_offset:
        for m in keep_msgs:
            if m.gnum == RECORD:
                d = m.get(5)
                if d is not None and d != INVALID32:
                    m.set(5, max(0, d - dist_offset))
    print(f"distance rebased by -{dist_offset / 100:.2f}m")

    # --- 3. fix the summary messages
    lat0, lon0 = records[0].get(0), records[0].get(1)
    lats = [m.get(0) for m in records if m.get(0) not in (None, 0x7FFFFFFF)]
    lons = [m.get(1) for m in records if m.get(1) not in (None, 0x7FFFFFFF)]
    elapsed_ms = int((last_ts - new_start).total_seconds() * 1000)

    for m in keep_msgs:
        if m.gnum == FILE_ID:
            m.set(4, new_start_raw)                       # time_created
        elif m.gnum == DEVICE_INFO:
            m.set(253, new_start_raw)
        elif m.gnum in (LAP, SESSION):
            timer_ms = m.get(8)
            new_timer = max(0, timer_ms - int(stale_timer * 1000)) if timer_ms else timer_ms
            total_dist = m.get(9)
            new_dist = max(0, total_dist - dist_offset) if total_dist else total_dist
            m.set(2, new_start_raw)                       # start_time
            m.set(3, lat0)                                # start_position_lat
            m.set(4, lon0)                                # start_position_long
            m.set(7, elapsed_ms)                          # total_elapsed_time
            m.set(8, new_timer)                           # total_timer_time
            m.set(9, new_dist)                            # total_distance
            if m.gnum == LAP:
                m.set(52, new_timer)                      # lap total_moving_time
            else:
                m.set(59, new_timer)                      # session total_moving_time
                if lats and lons:
                    m.set(29, max(lats))                  # nec_lat
                    m.set(30, max(lons))                  # nec_long
                    m.set(31, min(lats))                  # swc_lat
                    m.set(32, min(lons))                  # swc_long
            if new_timer:
                avg = int(round(new_dist * 10000 / new_timer))  # cm/ms -> mm/s
                for f in ((13, 110) if m.gnum == LAP else (14, 124)):
                    if m.get(f) not in (None, 0xFFFF, INVALID32):
                        m.set(f, avg)
        elif m.gnum == ACTIVITY:
            timer_ms = m.get(0)
            if timer_ms:
                m.set(0, max(0, timer_ms - int(stale_timer * 1000)))

    local = new_start + timedelta(hours=args.utc_offset)
    print(f"new start: {new_start} (device local {local:%Y-%m-%d %H:%M:%S})")
    print(f"elapsed: {timedelta(milliseconds=elapsed_ms)}")

    dst = args.dst or f"{local:%Y-%m-%d-%H-%M-%S}.fit"
    if args.dry_run:
        print(f"dry run - would write {dst}")
        return

    # --- 4. re-emit: definitions all kept, stale data messages skipped
    body = bytearray()
    for kind, it in items:
        if kind == "def":
            body += it
        elif not it.dropped:
            body += it.raw

    out = bytearray(header)
    struct.pack_into("<I", out, 4, len(body))
    struct.pack_into("<H", out, 12, crc16(out[:12]))
    out += body
    out += struct.pack("<H", crc16(out))
    open(dst, "wb").write(out)
    print(f"wrote {dst} ({len(out)} bytes, was {len(data)})")


if __name__ == "__main__":
    main()
