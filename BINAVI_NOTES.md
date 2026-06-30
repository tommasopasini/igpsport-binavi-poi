# iGPSPORT BiNavi Air — technical notes (reverse engineering)

Working document. Goal: show the 16 points of the **Giara 2026** roadbook (fountains,
food stops, hazards) on the BiNavi during navigation, automatically.

Updated: 2026-06-13

---

## 1. Connection and filesystem

- When **powered off** the BiNavi mounts as USB mass storage. When on = charge only.
- On Windows it is `D:`. On WSL it must be mounted manually:
  `sudo mount -t drvfs D: /mnt/d`  → contents under `/mnt/d/iGPSPORT/`.
- Device folders: `Activities, Courses, Maps, Roadbooks, Router, Schedule, Segments,
  Settings, System, Workouts`.
- Routes to navigate live in **`Courses/`**.

---

## 2. Route format `.cnx` (source, human-readable XML)

Every route uploaded from the app is a `.cnx` XML file. On first load the device
transcodes it into 4 binary caches (same name + extension):

| sidecar | magic | content |
|---|---|---|
| `.tcnx` | `TCNX` | track |
| `.tnav` | `TNAV` | turn-by-turn navigation |
| `.tclm` | `TCLM` | climb data (iClimb) |
| `.tsgn` | `TSGN` | "signs" (markers/turns) |

They all begin with `<magic>\x00\x0f Designed by Junjie Ding\x00…`. They are **regenerable**:
delete the caches and the device rebuilds them from the `.cnx`.

### XML structure

```xml
<Route>
  <Id>…</Id>
  <Distance>50062.17</Distance>      <!-- meters -->
  <Duration></Duration>
  <Ascent>882</Ascent>
  <Descent>-876</Descent>
  <Encode>2</Encode>                 <!-- 2 = 2nd-order delta for lat/lon -->
  <Lang>0</Lang>
  <TracksCount>1390</TracksCount>
  <Tracks>lat,lon,ele; Δ;Δ;…</Tracks>
  <Navs/>                            <!-- turn-by-turn; may be empty (auto-gen) -->
  <Points>…</Points>                 <!-- POI / Navigation Pro Point -->
  <PointsCount>N</PointsCount>
</Route>
```

### `<Tracks>` codec (validated on "Da Verona A Cerro": 50.1 km and ascent 903 vs 882)

First element = **absolute** point `lat,lon,ele_mm` (lat/lon 7 decimals, elevation in mm).
Then a list of **delta** triples:

- **lat / lon = SECOND-order delta**, unit 1e-7°.
  Decode: `vel += token; pos += vel/1e7` (vel starts at 0).
  Encode: `token[i] = d[i] − d[i−1]` where `d[i] = round((pos[i]−pos[i−1])·1e7)`,
  with `token[1] = d[1]`. (Quantize the positions onto the 1e-7 grid first to avoid drift.)
- **elevation = FIRST-order delta in cm**: `ele_mm += token_ele · 10`.

### `<Points>` — the POIs (this is where the roadbook points go!)

```xml
<Points>
  <Point><Lat>45.4460729</Lat><Lng>10.9774572</Lng><Type>8</Type><Descr>TESTUNO</Descr></Point>
  …
</Points>
<PointsCount>7</PointsCount>
```

- `<Lat>`/`<Lng>` = plain decimal degrees (no delta). The device snaps the point
  to the nearest position on the track. **No distance field.**
- `<Type>` = integer (category/icon). `<Descr>` = displayed name.
- The first auto-generated `<Point>` (route start) has **no `<Type>`** and its Descr is a
  geocoded address → ignore it.

---

## 3. POI categories (`<Type>`)

The codes are an **internal enum**: they do NOT follow the on-screen menu order, and are NOT
the FIT enum. Discover them by creating one Pro Point per category and reading the `.cnx`.

### Category list (on-screen order in the app, EN)
garbage recycle area · waypoint · sprint point · HC climb · level 1 climb · level 2 climb ·
level 3 climb · level 4 climb · service point · medical aid station · supply point · restroom ·
equipment area · meeting point · viewing platform · instagram-worthy location · shop · tunnel ·
valley · dangerous road · sharp turn · steep slope · intersection

⚠ There is **no** "fountain/water" or "food/restaurant" category. For water and food stops
the closest item is **supply point**.

### COMPLETE enum `Type → category` (23 entries, verified 2026-06-12)
| Type | category | Type | category |
|---|---|---|---|
| 0 | Waypoint | 12 | Equipment Area |
| 1 | Sprint Point | 13 | Shop |
| 2 | HC Climb | 14 | Meeting Point |
| 3 | Level 1 Climb | 15 | Viewing Platform |
| 4 | Level 2 Climb | 16 | Instagram-Worthy Location |
| 5 | Level 3 Climb | 17 | Tunnel |
| 6 | Level 4 Climb | 18 | Valley |
| 7 | Supply Point | 19 | Dangerous Road |
| 8 | Garbage recycle area | 20 | Sharp Turn |
| 9 | Restroom | 21 | Steep Slope |
| 10 | Service Point | 22 | Intersection |
| 11 | Medical Aid Station | | |

---

## 3-bis. VERIFIED POI pipeline (`.cnx` → `.tsgn`)

The `.tsgn` is the cache that holds the POIs. Size = **376 + 56·N** bytes (N = point count):
0 points → 376; Giara 16 points → 1272; app test route 23 points → 1664. Identical between my
`Giara.cnx` and the app's native Pro Points. Decoded `Giara.cnx.tsgn`: 16 records of 56 bytes,
lat (double LE at offset record+0) and **correct names** ("Fontana", "! Alzaia", "Ristoro"…).
→ The device **ingests my 16 points correctly**. The `.cnx` file is right.

**RESOLVED (2026-06-13, on-bike test):** the POIs **work**. They are not map icons:
they appear **only during active navigation** as a **proximity list with a countdown distance**
as you get closer (no audio alert — they are visual reminders). Confirmed on the test route
created with the igpsport app. The **FIT version of the same track shows nothing**
→ FIT abandoned for good. The native `.cnx` `<Points>` is the only channel that works,
and my `Giara.cnx` uses it identically to the app test route (same `.tsgn`).

## 4. Dead end: FIT course points

Tried `Giara_2026.fit` (FIT course with typed `course_point` water/food/danger):
the device reads it (generates `.tcnx/.tnav/.tclm`) **but ignores the course points** — no icons,
no alerts. So POIs come **only** through the `.cnx` `<Points>`. FIT abandoned.

---

## 5. Final plan for the Giara — STATUS

1. ✅ Complete `Type` enum (§3).
2. ✅ `Giara.cnx` generated (`generate_giara_cnx.py`): track from `Giara_2026_N_TOCO.gpx`
   (codec §2, round-trip self-test 0.00 cm) + 16 typed `<Point>` interpolated by km.
3. ✅ Copied to `Courses/`, removed the old `Giara_2026.fit`. Device regenerated the caches,
   `.tsgn` included (16 points absorbed — see §3-bis).
4. ✅ **DONE (on-bike A/B test, 2026-06-13):** the app's `.cnx` test route shows the POIs
   in active navigation as a **proximity list with countdown distance** (no sound); the
   FIT version shows nothing. So the mechanism is clear and `Giara.cnx` is ready:
   during the ride the 16 roadbook points will appear with the distance counting down
   as you approach fountains/food stops/hazards. Final confirmation = riding the Giara.

### Roadbook → category mapping (FINAL, full enum)
| km | point | category | Type |
|---|---|---|---|
| 9.4, 32.5, 39, 70, 79.8, 91, 100, 106, 109 | fountains (9) | Supply Point | 7 |
| 61 Calmasino (bakery), 82.8 Fumane (food stop) | food stops (2) | Shop | 13 |
| 89 | scenic detour + fountain | Viewing Platform | 15 |
| 74 | main-road crossing | Intersection | 22 |
| 14.5 | Alzaia (narrow) | Dangerous Road | 19 |
| 93 | start of descent | Steep Slope | 21 |
| 13.5 | stay on the track | Waypoint | 0 |

---

## 6. Repository layout

Code and docs are committed; all real work stays in two **gitignored** folders so
tracks/roadbooks/outputs never leave the local machine.

Committed:
- `generate_cnx.py` — main tool: GPX + roadbook CSV → native `.cnx` (track codec §2 + POIs, with self-test)
- `gpx_to_roadbook.py` — convert a GPX's `<wpt>` waypoints into a roadbook CSV: snaps each to the nearest km on the track and maps its `<sym>` to the §3 `<Type>` enum (unknown `<sym>` → `waypoint`)
- `build_roadbook_gpx.py` — preview helper: same points as GPX waypoints (for map viewers)
- `roadbook.example.csv` — roadbook CSV template
- `README.md`, `LICENSE`, `BINAVI_NOTES.md` (this file)
- `experiments/` — FIT dead end (course points ignored by the device):
  `generate_test_fit_from_cnx.py`, `patch_fit_coursepoints.py`, `generate_test_course.py`

Local-only (gitignored):
- `inputs/` — your GPX tracks, roadbook CSVs, PDFs (e.g. the Giara track + `giara_roadbook.csv`)
- `outputs/` — generated `.cnx` / `.gpx` / `.fit`

### Roadbook input
The POIs are no longer hardcoded: they live in a CSV (`km,type,description`), where
`type` is a category name or its 0–22 code (enum §3). The original Giara roadbook is
`inputs/giara_roadbook.csv`. Run: `python generate_cnx.py --roadbook inputs/giara_roadbook.csv`.

### Note on POI text
The `description` strings written to the device (e.g. "Fontana Chievo", "! Resta sulla traccia")
can be any language — they show verbatim on the bike computer. For the Giara they are kept in
Italian on purpose. Code, docstrings, prints and this document are in English.

### Giara 2026 roadbook (km → point, from the PDF)
9.4 Chievo fountain · 13.5 ⚠ stay on track · 14.5 ⚠ Alzaia · 32.5 Arcè fountain ·
39 Palazzolo fountain · 61 Calmasino food stop+fountain · 70 Ponton fountain · 74 ⚠ main-road crossing ·
79.8 Bure fountain · 82.8 Fumane fountain+food stop · 89 ⚠ scenic detour+fountain ·
91 Marano fountain · 93 ⚠ start of descent · 100 Pedemonte fountain · 106 Parona fountain ·
109 Ponte Diga Chievo fountain
