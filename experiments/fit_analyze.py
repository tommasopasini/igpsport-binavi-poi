#!/usr/bin/env python3
"""Deeper read-only look at a .fit: the event start/stop timeline, first/last
record, where the recording jumps (a stale fragment, see fit_fix_clock.py), and
the exact field layout (offsets) of session/lap/activity messages.

Usage: python experiments/fit_analyze.py FILE.fit [--gap-hours 6] [--max-stale-minutes 15]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fit_fix_clock import (ACTIVITY, EVENT, LAP, RECORD, SESSION, FitFixError,  # noqa: E402
                           find_jumps, parse, stale_cut, ts)

EVENT_TYPE = {0: "start", 1: "stop", 2: "consecutive_dp", 3: "marker",
              4: "stop_all", 8: "stop_disable", 9: "stop_disable_all"}
SUMMARY = {SESSION: "session", LAP: "lap", ACTIVITY: "activity"}


def main():
    ap = argparse.ArgumentParser(description="read-only FIT event/record/summary analysis")
    ap.add_argument("fit")
    ap.add_argument("--gap-hours", type=float, default=6.0,
                    help="forward time jump reported (default 6)")
    ap.add_argument("--max-stale-minutes", type=float, default=15.0,
                    help="longest stretch before a jump treated as a stale fragment (default 15)")
    args = ap.parse_args()
    msgs = [m for kind, m in parse(open(args.fit, "rb").read())[1] if kind == "msg"]

    print("=== EVENT timeline (start/stop) ===")
    for m in msgs:
        if m.gnum == EVENT:
            print(f"  {m.ts}  event={m.get(0)} type={EVENT_TYPE.get(m.get(1), m.get(1))}")

    records = [m for m in msgs if m.gnum == RECORD and m.ts]
    print(f"\n=== RECORD msgs: {len(records)} ===")
    if records:
        print(f"  first record: {records[0].ts}")
        print(f"  last  record: {records[-1].ts}")
    jumps = find_jumps(msgs, args.gap_hours)
    if not jumps:
        print(f"  no forward time jump over {args.gap_hours:g} h")
    for before, after in jumps:
        print(f"  time jump: {before} -> {after} ({(after - before).total_seconds() / 3600:.1f} h)")
    if jumps:
        try:
            before, after = stale_cut(msgs, args.gap_hours, args.max_stale_minutes)
        except FitFixError as e:
            print(f"  fit_fix_clock.py would refuse: {e}")
        else:
            kept = [m for m in records if m.ts >= after]
            print(f"  fit_fix_clock.py would cut at {after}: "
                  f"records before it: {len(records) - len(kept)}, after it: {len(kept)}")
            if kept:
                print(f"    first record kept: {kept[0].ts}")

    print("\n=== session/lap/activity field layout ===")
    for m in msgs:
        if m.gnum in SUMMARY:
            print(f"\n  [{SUMMARY[m.gnum]}] offset={m.offset} size={len(m.raw)} endian={m.endian}")
            for fnum, (off, size, base) in m.fields.items():
                val = m.get(fnum)
                when = ts(val) if fnum == 253 or (m.gnum != ACTIVITY and fnum == 2) else ""
                print(f"      field {fnum:3} size={size} base={base:2} @+{off} val={val} {when}")


if __name__ == "__main__":
    main()
