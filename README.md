# binavi-poi

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

## How it works

A BiNavi route is a `.cnx` XML file in the device's `Courses/` folder. It holds the
track (delta-encoded) and a `<Points>` list of POIs. `generate_cnx.py` takes a normal
**GPX track** plus a **roadbook CSV** (your points, by km) and writes a `.cnx` you copy
to the device. A round-trip self-test checks the encoded track decodes back to the GPX
within 0.5 cm before anything is written.

FIT course points were tried and **do not display** on the BiNavi — see
[BINAVI_NOTES.md §4](BINAVI_NOTES.md). Those scripts live in `experiments/` for reference.

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

### Roadbook CSV

See **[roadbook.example.csv](roadbook.example.csv)**. Columns:

| column | meaning |
|---|---|
| `km` | distance along the GPX track where the point sits (decimal km) |
| `type` | category — a name (e.g. `supply point`) or its integer code `0`–`22` |
| `description` | short text shown on the device (a leading `!` reads as a warning) |

There is no "fountain" or "food" category on the device; `supply point` is the
closest for water/feed, `shop` for a food stop. Full enum in the example file and in
[BINAVI_NOTES.md §3](BINAVI_NOTES.md).

### Preview in a map app (optional)

`build_roadbook_gpx.py` writes the same points as GPX waypoints (to `outputs/`) so you
can eyeball them in Komoot/GPXSee. This is preview only — it does **not** put anything
on the BiNavi.

## Layout

```
generate_cnx.py        main tool: GPX + roadbook CSV -> native .cnx
build_roadbook_gpx.py  preview helper: -> GPX waypoints
roadbook.example.csv   template for your roadbook
BINAVI_NOTES.md        reverse-engineered .cnx format + POI enum
experiments/           FIT attempts (dead end: device ignores FIT course points)
inputs/                YOUR tracks / roadbooks / PDFs        (gitignored)
outputs/               generated .cnx / .gpx / .fit          (gitignored)
```

## License

MIT — see [LICENSE](LICENSE).
