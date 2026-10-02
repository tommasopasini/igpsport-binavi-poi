#!/usr/bin/env python3
"""Read-only overview of a .fit: message counts, first/last timestamp, gaps over
5 minutes, and the session/lap/activity summary — enough to see where a
recording jumps from one day to another (the stale fragment fit_fix_clock.py
repairs).

Usage: python experiments/fit_inspect.py FILE.fit
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fit_fix_clock import ACTIVITY, LAP, SESSION, parse, ts  # noqa: E402

NAMES = {0: "file_id", 18: "session", 19: "lap", 20: "record", 21: "event",
         23: "device_info", 34: "activity", 49: "file_creator"}


def name(m):
    return NAMES.get(m.gnum, m.gnum)


def main():
    path = sys.argv[1]
    msgs = [m for kind, m in parse(open(path, "rb").read())[1] if kind == "msg"]
    stamped = [m for m in msgs if m.ts]
    print(f"file: {path}")
    print(f"total messages: {len(msgs)}, with timestamp: {len(stamped)}")
    if stamped:
        print(f"first ts: {stamped[0].ts}  ({name(stamped[0])})")
        print(f"last  ts: {stamped[-1].ts}  ({name(stamped[-1])})")
        print(f"span: {stamped[-1].ts - stamped[0].ts}")
    print("msg counts:", dict(Counter(name(m) for m in msgs)))

    print("\n--- gaps > 5 min between consecutive timestamped messages ---")
    for i in range(1, len(stamped)):
        a, b = stamped[i - 1], stamped[i]
        gap = (b.ts - a.ts).total_seconds()
        if gap > 300:
            print(f"  gap {gap / 60:.1f} min: {a.ts} ({name(a)}) -> {b.ts} ({name(b)})  [idx {i - 1}->{i}]")

    print("\n--- session / lap / activity messages ---")
    for m in msgs:
        if m.gnum in (SESSION, LAP):
            print(f"  {name(m):9} ts={m.ts} start={ts(m.get(2))} elapsed={m.get(7)} timer={m.get(8)}")
        elif m.gnum == ACTIVITY:
            print(f"  {name(m):9} ts={m.ts} timer={m.get(0)} local={ts(m.get(5))}")


if __name__ == "__main__":
    main()
