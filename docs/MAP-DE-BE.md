# Berlin (DE-BE): a working offline map

Streets, named places, help points and street addresses for Berlin, built from redistributable sources by one command
and kept in its own folder; its places are searchable offline, and its addresses form an index ready for a German
address search. It follows the plan in [REGIONS-DE.md](REGIONS-DE.md) and the file formats in [MAPS.md](MAPS.md). The
region keeps its full ID, `DE-BE`: a bare `DE` means Delaware.

The builder is the data half (#433): it runs `tools/build_map.py` unchanged and needs nothing from the app. The app
reads the address index the German way and draws the street pumps (#79): `engine/address_search.py` searches an index
whose `meta` says `norm = de-1` with the German rule and every other index with the US rule, and the map has a hand-pump
layer. Both halves share the query cases in `app/tests/fixtures/de_be_queries.json`. Installing the index from Setup
waits for the shared address-index IDs coordinated with the other regions in #31 and #25.

## Build it

```sh
python3 tools/build_region_de_be.py fetch    # downloads the sources, about 270 MB, into derived/regions/DE-BE/source/
python3 tools/build_region_de_be.py build    # builds beside the last build, replaces data/ once it passes check (about a minute)
python3 tools/build_region_de_be.py check    # validates it; exits non-zero on any failure
python3 tools/build_region_de_be.py demo     # adds an empty library beside it for trying the map alone
python3 server.py --data derived/regions/DE-BE/demo --no-index
```

Python 3.9 or newer and its standard library; no GDAL, no other packages. `fetch` is the only step that uses the
network. Everything is written under `derived/regions/DE-BE/`, so no existing map set changes. `data/build.json`
records every source (URL, dated file name, data date, licence, size, SHA-256) and every other output (size, SHA-256).

`build` writes into `data.new/` and runs `check` on it; only if every check passes does it take the place of `data/`.
A rebuild that fails, or fails its checks, leaves the previous `data/` and its `build.json` as they were (a failed build
stays in `data.new/` to look at, and the next build clears it).

## Sources

| What | Source | Licence | Attribution |
|---|---|---|---|
| Streets, water, land use, buildings, named places and points | OpenStreetMap via Geofabrik, `berlin-YYMMDD-free.gpkg.zip` (the dated name is recorded) | ODbL 1.0 | © OpenStreetMap contributors (already on the map) |
| Street addresses (house number, suffix, postcode, locality) | Geoportal Berlin WFS `adressen_rbs` | Datenlizenz Deutschland Zero 2.0 | none required |
| Hospitals | Geoportal Berlin WFS `krankenhaeuser` (plan hospitals and other hospitals) | dl-de/zero-2.0 | none required |
| Pharmacies | Geoportal Berlin WFS `apotheken` | dl-de/zero-2.0 | none required |
| Fire stations | Geoportal Berlin WFS `feuerwehr` | dl-de/zero-2.0 | none required (one catalogue entry says dl-de/by-2.0; the service says Zero) |
| Police stations | Geoportal Berlin WFS `polizei` | dl-de/zero-2.0 | none required |
| Districts (Bezirke) and localities (Ortsteile) | Geoportal Berlin WFS `alkis_bezirke`, `alkis_ortsteile` | dl-de/zero-2.0 | none required |

Postcode areas are placed at the middle of their own addresses rather than taken from the postcode polygons, which are
CC BY 3.0 DE; that keeps the map to ODbL plus sources without an attribution duty.

Inside Berlin, the Geoportal records replace OpenStreetMap's points of the four official kinds, so each facility has
one pin from the public register. Everything else, including street hand pumps, comes from OpenStreetMap.

The OpenStreetMap extract reaches a few kilometres past the city boundary into Brandenburg, which surrounds Berlin. Its
points there stay on the map, so the nearest help from the city edge can be across it: from the edge of Rahnsdorf, the
nearest hospital is in Woltersdorf, 4.3 km away. The place finder names where each row is: Berlin inside the boundary
(the twelve districts from `alkis_bezirke`), Brandenburg beyond it, so `Teltow` reads `Brandenburg (OpenStreetMap)` and
`Lichterfelde` reads `Berlin`.

## What the build holds

Built on 24 November 2026 from the sources fetched on 17 November 2026: OpenStreetMap data of 16 November 2026
(`berlin-261116-free.gpkg.zip`) and the Geoportal services as served that morning. On 17 November a full `fetch`,
`build`, `check` took under two minutes.

| File | Size | SHA-256 (this build) | Holds |
|---|---|---|---|
| `derived/geo/national.sqlite` | 188.1 MB | `14adbd46…3fd0c` | 467,739 road features plus water, land, rail, buildings and places; 21,096 help and supply points |
| `derived/geo/map.sqlite` | 32 KB | `bd820351…da6e0` | empty, as in a US state map; carries the map's source and date |
| `derived/geo/places.sqlite` | 3.3 MB | `f3c7a77d…59f26` | 12 districts, 97 localities, 193 postcode areas, 11,321 streets, 13,508 named points, 196 named places |
| `derived/geo/addresses/DE-BE.sqlite` | 51.6 MB | `8f73892e…57f34` | 404,303 addresses; a sidecar, as the US address indexes are |
| `map-set.json` | 191 B | | name `DE-BE` and the sizes of the map files |

Help points: 111 hospitals, 702 pharmacies, 102 fire stations and 37 police stations from the Geoportal, replacing
57, 674, 99 and 65 OpenStreetMap points inside Berlin; OpenStreetMap's 2 hospitals, 11 pharmacies, 4 fire stations and
3 police stations just beyond the boundary stay. 2,080 street hand pumps come from OpenStreetMap.

Full checksums are in `data/build.json`. They identify one build: the map and address files record their build time, so two
builds of the same sources differ in those bytes, not in content.

## Checks

`check` verifies, and fails on any miss:

- every output file has the size and SHA-256 recorded at build time; `map-set.json` is named `DE-BE` and its sizes match;
- every SQLite file passes `PRAGMA integrity_check`;
- streets are present, and each help kind has a plausible count (hospitals, pharmacies, fire stations, police, pumps);
- every help point and address lies inside Berlin's box from `regions-de-proposed.json`, and every address has a
  five-digit postcode;
- the address index says `region = DE-BE` and `norm = de-1` in its `meta`, its row count matches, and every stored
  street key is `normalize_de(street)`;
- three public buildings are in the index exactly once with their postcode and locality (Unter den Linden 77, Platz der
  Republik 1, Straße des 17. Juni 135);
- every Geoportal help point lies inside the city boundary;
- the place finder has a locality, a district, a postcode area and a street, labels every official help point inside
  Berlin with its source, Geoportal Berlin, and names Berlin in no row beyond the boundary.

`check` reads the files with SQL and the district boundaries from `source/`, and imports nothing from the app, so it
says nothing about how the app searches them; `app/tests/test_address_search.py` runs every query in the fixture against
the app's search. `app/tests/test_build_region_de_be.py` runs offline: the `de-1` spellings, the stored house-number
form, the fixtures' consistency, that each pump lands exactly where `tools/build_map.py` puts a point, that each place
names the source it came from and, for a place on each side of the boundary, where it is, that the Geoportal replaces
OpenStreetMap's points only inside Berlin, and that a rebuild which fails, or fails its checks, leaves the previous
output in place.

## Try it

With the demo server running, the map opens over the US; go to Berlin with
`http://127.0.0.1:8890/#/map?lat=52.5163&lon=13.39&z=15&layers=hospital,pharmacy,fire,police`.

With only this map set installed:

| Search | Result |
|---|---|
| `Unter den Linden 77`, `Unter den Linden 77, 10117 Berlin`, `Straße des 17. Juni 135` | the house, named the German way (`Unter den Linden 77`, `10117 Berlin-Mitte`) and credited to Geoportal Berlin |
| `Müllerstraße 12`, `Muellerstr. 12` | the three houses of that number, in Wedding, Mahlsdorf and Lichterfelde: Berlin has more than one Müllerstraße |
| `Unter den Linden 9999` | the street, never a nearby house |
| `Kreuzberg`, `10117`, `10117 Berlin`, `Oranienstraße`, `Alexanderplatz` | the locality, the postcode area, the street |
| `Route 66` | `Straße 66` in Karow, a real numbered Berlin street: with no such house the place search answers |
| `77 Unter den Linden`, `25 Oranienstraße, 10999 Berlin` | the same house: with no US house of that address, the German index is asked for the whole street |
| `77 Linden`, `100 Main St` | nothing: written number first, a street must be one Berlin street in full, so no US query finds Lindenstraße or Mainzer Straße |

The expectations for German queries, and the US answers that must stay the same with Berlin installed, are in
`app/tests/fixtures/de_be_queries.json` and `app/tests/test_address_search.py`.

`/api/map/near?lat=52.5163&lon=13.3777&cls=hospital,pharmacy,fire_station,police,water_well` lists the nearest help and
street pumps around the Brandenburg Gate. The pumps have their own layer, "Hand pumps & water wells (untested water)",
in the Water preset.

## Still open

- **Address-index IDs.** `engine/setup/manifest.py` and the upgrade carry-over in `engine/setup/install.py` accept only
  two-letter IDs, so Setup does not install `DE-BE.sqlite` yet; it opens with `--data`. The shared change is coordinated
  in #31.

## Limits

- **The app's words are still American.** The map page says it shows what was mapped by 25 Oct 2026 (a fixed line in
  `web/map.js`; this map's date is in its `meta`), the search box asks for a ZIP, a button says "Whole United States",
  and the map opens over the US. The source list in `map_info` names US datasets. These belong to the shared region
  work, not to one region's builder.
- **Not in this map:** terrain, the water and dam layers, crops and the resource atlas (US datasets); Berlin's
  disaster-relief points (Katastrophenschutz-Leuchttürme), which are published only as a map and a PDF without a licence.
- **Pumps are not checked.** OpenStreetMap does not say whether a pump works, and the water is untested.
- **The iPad app** marks place results as on the map only inside Southern California or in a pack (`onMap` in
  `server.py`); the web app does not use that flag.

### German addresses

- Eleven houses in Schmöckwitz and Rahnsdorf officially carry a Brandenburg postcode (155xx); they are kept as recorded.
- The source has no postal place names beyond Berlin, so each row holds `city = Berlin` and its locality in `district`;
  a German result reads `10117 Berlin-Mitte` (postcode, city, locality).
- A query may be written `Straße 12`, `Straße 12a` or `12 a`, with `Str.`/`Strasse`/`Straße`,
  umlauts typed or spelled out (`Muellerstrasse`), an optional postcode and an optional `Berlin` or locality, and with
  numbers inside street names. The number may also come first (`77 Unter den Linden`, `12a Müllerstraße`) when the
  street is written in full. Not covered: ranges (`12-14`), a street name split in two (`Oranien Str. 25`) and
  building parts beyond one letter.
