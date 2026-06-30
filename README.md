# igpsport-binavi-poi

Put your own points of interest — water, food, hazards, notes — onto an
**iGPSPORT BiNavi** bike computer so they show up during navigation.

The official iGPSPORT app makes this awkward (no "fountain" or "food" category,
points are hard to place by distance, and exported FIT course points are ignored
by the device). This repo carries a small generator that writes the device's
**native `.cnx` route format** directly, with your POIs placed by kilometre along
the track. During navigation the BiNavi then shows each point as a
**proximity reminder with a countdown distance** as you approach it.

The reverse-engineered format (track codec + the POI `<Type>` enum) is documented
in **[BINAVI_NOTES.md](BINAVI_NOTES.md)**.

> ⚠️ Unofficial. Not affiliated with or endorsed by iGPSPORT. Built by reverse
> engineering; a firmware update could change the format. Use at your own risk.
>
> **Tested only on the BiNavi Air.** The `.cnx` format is written by the iGPSPORT app
> (shared across the product line), so it may work on other map-capable iGPSPORT devices —
> but whether the firmware actually renders the points is device-specific and unverified
> elsewhere. Reports from other devices welcome.

## How it works

A BiNavi route is a `.cnx` XML file in the device's `Courses/` folder. It holds the
track (delta-encoded) and a `<Points>` list of POIs. `generate_cnx.py` takes a normal
**GPX track** plus a **roadbook CSV** (your points, by km) and writes a `.cnx` you copy
to the device. A round-trip self-test checks the encoded track decodes back to the GPX
within 0.5 cm before anything is written.

### Why not GPX or FIT?

Both were tried first; neither gets POIs onto the device:

- **GPX** is only a *track* source. The BiNavi's own route format is `.cnx`; when a GPX is
  imported (via the app), the track is converted to `.cnx` and its `<wpt>` **waypoints are
  dropped** — they never become on-device POIs. (You *can* still reuse those `<wpt>`s as a
  roadbook source with `gpx_to_roadbook.py` below — that routes them into the `.cnx`
  `<Points>` list instead, the channel the device does render.)
- **FIT** course files *load* (the track works), but the firmware **ignores the
  `course_point` messages** — no icons, no alerts. Verified on the device: a FIT with typed
  water/food/danger course points showed nothing. Details in
  [BINAVI_NOTES.md §4](BINAVI_NOTES.md); the scripts are kept in `experiments/` as a record.

The native `.cnx` `<Points>` list is the only channel the device actually renders — which is
what `generate_cnx.py` writes.

## Requirements

- Python 3 (standard library only — no `pip install` needed)
- `gpsbabel` — only for the `experiments/` FIT scripts, not for the main tool

## Usage

Everything you actually work on stays **local**: the `inputs/` and `outputs/` folders
are gitignored, so your tracks and generated routes are never committed.

```bash
# 1. put your track and roadbook in inputs/
cp my_ride.gpx            inputs/
cp roadbook.example.csv   inputs/roadbook.csv     # then edit it

# 2. generate the native route
python generate_cnx.py
#   -> outputs/my_ride.cnx   (+ prints a self-test result)

# 3. copy it to the device (BiNavi mounted as USB mass storage, powered off)
cp outputs/my_ride.cnx /mnt/d/iGPSPORT/Courses/
```

Options: `--gpx`, `--roadbook`, `--out` override the defaults. With one `.gpx` in
`inputs/` and an `inputs/roadbook.csv`, no arguments are needed.

### From GPX waypoints (skip writing the roadbook by hand)

If your GPX already carries `<wpt>` waypoints — e.g. a Komoot export that *preserved*
them (the standard Komoot export usually doesn't; check first) — `gpx_to_roadbook.py`
turns them straight into a roadbook CSV, so you don't place each point by km yourself:

```bash
# GPX waypoints -> inputs/<gpx-name>_roadbook.csv
python gpx_to_roadbook.py --gpx inputs/my_ride.gpx
python generate_cnx.py    --gpx inputs/my_ride.gpx --roadbook inputs/my_ride_roadbook.csv
```

What it does per waypoint:

- **km** — snaps the waypoint to the nearest point on the track (point-to-segment
  projection) and reads off its progressive distance. Waypoints sit a few metres off
  the line (they mark a roadside fountain, not a recorded trackpoint); one farther than
  `--max-offset` (default 80 m) is still written but flagged, since that usually means
  it doesn't belong to this track.
- **type** — maps the waypoint's GPX `<sym>` to the device's `<Type>` enum (see the
  table below). An unrecognised `<sym>` defaults to `waypoint` (0) and is reported, so
  you can fix that one row before generating.

The script prints a per-point report; review the `type` column, edit the CSV if needed,
then run `generate_cnx.py`. (This is the same `.cnx`-only channel as the hand-written
path — it just authors the roadbook for you.)

#### `<sym>` → POI type mapping

These GPX symbol names are recognised (case-insensitive); anything else falls back to
`waypoint` (0):

| recognised `<sym>` names | mapped POI type | code |
|---|---|---|
| `Drinking Water`, `Water Source`, `Water`, `Potable Water` | supply point | 7 |
| `Restaurant`, `Fast Food`, `Shop`, `Shopping Center`, `Convenience Store` | shop | 13 |
| `Restroom`, `Toilet`, `WC` | restroom | 9 |
| `Danger Area`, `Danger` | dangerous road | 19 |
| `Summit`, `Scenic Area`, `Viewpoint` | viewing platform | 15 |
| `Photo` | instagram-worthy location | 16 |
| `Campground`, `Parking Area` | meeting point | 14 |
| *(anything else)* | waypoint | 0 |

Add more rows to `TYPE_BY_SYM` in `gpx_to_roadbook.py` if your source uses other names.

### Roadbook CSV

See **[roadbook.example.csv](roadbook.example.csv)**. Columns:

| column | meaning |
|---|---|
| `km` | distance along the GPX track where the point sits (decimal km) |
| `type` | category — a name (e.g. `supply point`) or its integer code `0`–`22` |
| `description` | short text shown on the device (a leading `!` reads as a warning) |

There is no "fountain" or "food" category on the device; `supply point` is the
closest for water/feed, `shop` for a food stop.

**POI type legend** (the internal `<Type>` enum — *not* the app's on-screen menu order):

| code | name | code | name |
|---|---|---|---|
| 0 | waypoint | 12 | equipment area |
| 1 | sprint point | 13 | shop |
| 2 | hc climb | 14 | meeting point |
| 3 | level 1 climb | 15 | viewing platform |
| 4 | level 2 climb | 16 | instagram-worthy location |
| 5 | level 3 climb | 17 | tunnel |
| 6 | level 4 climb | 18 | valley |
| 7 | supply point | 19 | dangerous road |
| 8 | garbage recycle area | 20 | sharp turn |
| 9 | restroom | 21 | steep slope |
| 10 | service point | 22 | intersection |
| 11 | medical aid station | | |

You can write either the name or the number in the `type` column. (How each renders as an
icon is the device's business; see [BINAVI_NOTES.md §3](BINAVI_NOTES.md) for how the enum was
recovered.)

### Preview in a map app (optional)

`build_roadbook_gpx.py` writes the same points as GPX waypoints (to `outputs/`) so you
can eyeball them in Komoot/GPXSee. This is preview only — it does **not** put anything
on the BiNavi.

## Testing on the device (without riding)

POIs are **not** map icons — on the BiNavi they appear only **during active navigation**, as
a proximity reminder whose distance counts down as you approach (no sound). To confirm that
before a real ride, make a short test route near you with 2–3 points a few hundred metres
apart, navigate it, and walk toward the first point until the reminder appears.

Two ways to get that test route — useful as an A/B check:

- **With the iGPSPORT app (reference / known-good):** create a short route in the app, add a
  couple of *Navigation Pro Points* on it, and sync to the device. This is the official path;
  if your generated file behaves the same, you know the file is right.
- **Without the app (this tool):** make a tiny GPX near you and a 2–3 line roadbook CSV, run
  `python generate_cnx.py --gpx inputs/test.gpx --roadbook inputs/test_roadbook.csv`, and copy
  the `.cnx` to `Courses/`.

Both should show the points the same way. (A FIT-based test route, for contrast, shows
nothing — that's the dead end documented above; `experiments/generate_test_course.py` builds
one if you want to see it fail.)

## Layout

```
generate_cnx.py        main tool: GPX + roadbook CSV -> native .cnx
gpx_to_roadbook.py     convert GPX <wpt> waypoints -> roadbook CSV (by km + type)
build_roadbook_gpx.py  preview helper: -> GPX waypoints
roadbook.example.csv   template for your roadbook
BINAVI_NOTES.md        reverse-engineered .cnx format + POI enum
experiments/           FIT attempts (dead end: device ignores FIT course points)
inputs/                YOUR tracks / roadbooks / PDFs        (gitignored)
outputs/               generated .cnx / .gpx / .fit          (gitignored)
```

## License

MIT — see [LICENSE](LICENSE).
