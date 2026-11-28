# Detailed maps for Portugal: a proposal

This is a proposal for discussion, not a decision. It answers, for Portugal, the questions [MAPS.md](MAPS.md) asks before
coverage can go beyond the United States: which open source replaces each federal list, under what licence, what a
"state" is there, and how addresses are written. It follows the shape of [REGIONS-ES.md](REGIONS-ES.md),
[REGIONS-DE.md](REGIONS-DE.md) and [REGIONS.md](REGIONS.md) and is meant to feed the same design discussion
(issues #23, #25 and #31). The machine-readable version is
[regions-pt-proposed.json](regions-pt-proposed.json).

Nothing here changes the app, the engine or a build. No code reads the JSON, and nothing here ships: builds package
`web/` and `content/` only.

**How things were checked.** Research was done on 20 November 2026, from Portugal, on a Fedora machine. Downloads were
run and numbers were computed from the data itself as described below. Each source carries one of three marks:

- **opened**: the dataset or licence page itself was opened and read;
- **search only**: the fact was seen in a search result or the page could not be parsed;
- **not checked**: the page could not be reached; the row says what is missing.

Several Portuguese public portals are JavaScript applications, so some rows are "not checked" the same way REGIONS-ES.md
found for the CNIG. The INE's INSPIRE portal was under maintenance on the day. These need a second look before anything
is built on them.

## Why Portugal is a useful case

- **Addresses are already open nationwide.** The INE (the national statistics institute) publishes a national address
  set through its INSPIRE ATOM service, distributed by OpenAddresses, under CC BY 4.0. Overture carries the same set
  (see "Addresses in Overture, measured"): 5,607,450 rows, every one with a postcode and coordinates.
- **Like Spain, postcode *areas* are not open.** The CTT postcode dictionary is a commercial product; its terms were not
  verified today (the page did not answer from our route), but the commercial shape is the same as the Correos case in
  REGIONS-ES.md. The postcode *attached to each address* in the CC BY 4.0 INE data is a different thing and is what
  address search needs.
- **The islands make the country's bounding box useless.** The Azores lie about 1,500 km west of the mainland and
  Madeira 1,000 km south-west, so Portugal's box (which the European country sets of PR #84 compute at about 25° of
  longitude) is mostly ocean. Any per-region design has to split the islands out. The INE address dataset already
  splits the Azores into two address groups (Corvo and Flores are about 240 km west of the other islands), and this proposal
  follows that split.
- **The NUTS 2021 reform changed the mainland's regions.** Portugal had five continental regions for decades; the
  2021 reform (in force for 2024 data) split out Grande Lisboa, Península de Setúbal and Oeste e Vale do Tejo. Nine
  NUTS II exist now. Boxes computed from the older five-region shape would be wrong today.
- **Language is simple.** Portugal is monolingual in Portuguese except for Mirandese, officially recognised for local
  use since Lei 7/99 of 1999 and co-official in the municipality of Miranda do Douro (page returned a JavaScript shell;
  law text verified by search only). The bilingual-name decisions of REGIONS-ES.md are much smaller here: one
  municipality.

## What a region is

The proposal is ten regions: the seven continental NUTS II, the Azores split into two regions that follow the two
address groups the INE dataset already uses, and Madeira. A continental NUTS II is the level closest to a US state in
what runs there: the CCDRs (comissões de coordenação e desenvolvimento regional) manage territory, regional planning and
civil-protection coordination, and the two autonomous regions have their own governments.

Each box was widened by 0.15° and rounded **outward** to 0.01° — the west and south edges floored, the east and north
edges ceiled — the same margin `map_sets.state` uses for US states. The first revision read the polygon bounds from
Eurostat GISCO's NUTS 2024 boundaries (1:3 million GeoJSON, `NUTS_RG_03M_2024_4326_LEVL_2.geojson`; the coordinates
are ETRS89, which differs from WGS 84 by far less than the 0.01° rounding). Every polygon box has since been
re-measured from the official CAOP 2025 polygons — Madeira on 22 November, the seven continental regions on
25 November — and the GISCO numbers stay in the cross-checks as the measure the first revision was verified against.

Eight of the ten boxes now carry CAOP polygon bounds. The two Azores boxes cannot come from a
polygon, because GISCO has no sub-regional NUTS for the islands, so they come from the INE address dataset's own two
groups of address points (marked in the table). The inputs are the CAOP 2025 geopackages for the polygon boxes and the
counted address rows described in "Addresses measured" for the island boxes, so every number here is recomputable from
those datasets with the rule stated.

As a check, the outer edges match Portugal's known extreme points: Cabo da Roca in the west (-9.50°), the eastern border
with Spain (-6.21°), Melgaço and Castro Laboreiro in the north (42.15°), and Cabo de Santa Maria near Faro in the south
(36.96°). The address dataset's own per-group boxes agree from inside on every edge (see "Addresses measured").

| Code | NUTS | Name | Box (west, south, east, north) | Box with 0.15° margin | Box source |
|---|---|---|---|---|---|
| `pt-11` | PT11 | Norte | -8.8811, 40.7588, -6.1892, 42.1543 | -9.04, 40.60, -6.03, 42.31 | CAOP 2025 polygon |
| `pt-19` | PT19 | Centro | -9.0403, 39.4653, -6.7810, 41.0375 | -9.20, 39.31, -6.63, 41.19 | CAOP 2025 polygon |
| `pt-1a` | PT1A | Área Metropolitana de Lisboa (Grande Lisboa) | -9.5005, 38.6731, -8.8415, 39.0647 | -9.66, 38.52, -8.69, 39.22 | CAOP 2025 polygon |
| `pt-1b` | PT1B | Área Metropolitana de Lisboa (Península de Setúbal) | -9.2630, 38.4091, -8.4910, 38.8454 | -9.42, 38.25, -8.34, 39.00 | CAOP 2025 polygon |
| `pt-1c` | PT1C | Alentejo | -8.9129, 37.3189, -6.9315, 39.6637 | -9.07, 37.16, -6.78, 39.82 | CAOP 2025 polygon |
| `pt-1d` | PT1D | Oeste e Vale do Tejo | -9.5170, 38.7315, -7.8090, 39.8387 | -9.67, 38.58, -7.65, 39.99 | CAOP 2025 polygon |
| `pt-15` | PT15 | Algarve | -8.9975, 36.9617, -7.3933, 37.5291 | -9.15, 36.81, -7.24, 37.68 | CAOP 2025 polygon |
| `pt-20w` | PT20 (west) | Açores: Corvo e Flores | -31.2660, 39.3749, -31.1104, 39.6766 | -31.42, 39.22, -30.96, 39.83 | INE address points (group `AC25`) |
| `pt-20e` | PT20 (east) | Açores: restantes ilhas | -28.8250, 36.9295, -25.0138, 39.0954 | -28.98, 36.77, -24.86, 39.25 | INE address points (group `AC26`) |
| `pt-30` | PT30 | Região Autónoma da Madeira | -17.2659, 30.0288, -15.8537, 33.1282 | -17.42, 29.87, -15.70, 33.28 | CAOP 2025 polygon |

Cross-checks between the two datasets:

- The address dataset's mainland group (`PTCONT`) sits inside the continental boxes: the seven CAOP boxes contain all
  5,669,971 mainland rows. The first revision's GISCO boxes left 1,540 mainland rows outside them, all labelled
  geocoding outliers; the CAOP boxes contain every one of them.
- Against the GISCO 1:3M bounds, all but three of the 28 continental edges agree within 0.01°; the larger moves sit on
  the Atlantic coast — the Alentejo box reaches 0.038° further west, the Algarve 0.016° and the Setúbal peninsula
  0.013°. The Centro box's west edge matches GISCO's to 2×10⁻⁷° between the two independent datasets (GeoJSON degrees
  against a metre-based projection converted vertex by vertex), which also checks the conversion itself.
- Madeira's address group (`MAD`) sits inside the polygon box on every edge: its raw points reach
  -17.2578…-15.8637 and 30.0351…33.0886 against the official CAOP polygon's -17.2659…-15.8537 and 30.0288…33.1282
  (and inside the earlier GISCO row of -17.2660…-15.8554 and 30.0257…33.1040 as well).
- GISCO's own PT20 polygon box is -31.2679, 36.9305, -25.0142, 39.7262: its west edge matches the Corvo-and-Flores
  address points to 0.002°. Its south edge sits at 36.93° N, as does the eastern address group's lowest row — and that
  row **is now located**. It is id `pt.ine.add.AC26.76921`, postcode 9580-228, city SANTO ESPÍRITO: an address in the
  south-eastern parish of Santa Maria. Reverse-geocoding the coordinates (Nominatim, 21 November 2026) puts the point
  **15 m from OSM's node for the Farol de Gonçalo Velho**, the lighthouse on Santa Maria's south-eastern point — a real
  Santo Espírito address near the island's tip, not an islet in the sea and
  not a geocoding error. The GISCO 03M polygon's south edge lands about 0.001° north of the lighthouse, which is
  polygon simplification, not disagreement about where Santa Maria ends.
- **The proposal's boxes do not include the Formigas.** The `pt-20e` box's eastern edge is -24.86°, and the islets sit
  at about -24.71°, so they fall outside it (they fall outside GISCO's own PT20 polygon too). The islets carry no
  addressable settlement, so a street map loses nothing practical; if the maintainers want the reserve inside the
  map's coverage, `pt-20e`'s eastern edge would widen to about -24.70°.

Notes:

- **The two Lisbon boxes** are the two parts of the NUTS II split that the 2021 reform made of the old Área
  Metropolitana de Lisboa: Grande Lisboa (PT1A) north of the Tejo and Península de Setúbal (PT1B) south of it. Both are
  small (the whole old AML is 3,015 km²) but hold about three million people, so they are separate regions rather than
  one.
- **The Azores split.** The NUTS polygon of the Azores (PT20) spans 6.3° of longitude, almost all ocean, because Corvo
  and Flores sit about 240 km north-west of the central group (the published Flores and Horta airport coordinates,
  eAIP LPFL and LPHR, give 242 km in a straight line). The INE address dataset already splits the same two groups (its
  ids are `pt.ine.add.AC25…` for Corvo and Flores and `…AC26…` for the rest), so the proposal keeps that split and
  `pt-20w`'s box is the address group's own box, not a NUTS one. GISCO's PT20 box and the address groups' boxes agree
  at every edge. The eastern group's southern edge (36.93° N) is a real address at the south-eastern tip of Santa
  Maria — 15 m from OSM's Farol de Gonçalo Velho node, in the Santo Espírito parish — not the Formigas, whose
  published bounds lie north-east of Santa Maria.
- **Madeira includes the Selvagens Islands**, a nature reserve whose municipality is Funchal. One address row sits at
  30.035° N, south of the Selvagem Grande island at 30.14° N, and GISCO's polygon reaches 30.026° N there; both agree,
  and neither carries a settlement. The row is one outlier to re-check before any build.
- **Enclaves.** None: Portugal has no administrative enclave inside another region, unlike Spain's Treviño or Llívia.

### Codes

US state maps use two-letter codes. NUTS codes for Portugal contain digits (`PT11`, `PT1A`), so no two-letter US code
collides and a bare prefix is safe — but the app's own machinery still matters: `carry_over()` in
`engine/setup/install.py` keeps only address indexes named `[A-Z]{2}.sqlite` or `.json` on a library upgrade, so an
index named `PT11.sqlite` would be left behind without a message (read in this repository on 20 November 2026; the
same problem REGIONS-ES.md records for `ES-MD`). The proposal writes the codes lower-case with a hyphen (`pt-11`,
`pt-20w`), after the `eu-<iso>` style the European country sets use, and leaves the final shape to the maintainers.

## Sources, layer by layer

| Layer (file) | US source today | Proposed for Portugal | Publisher | Licence | Checked |
|---|---|---|---|---|---|
| Terrain (`terrain.sqlite`) | USGS 3DEP, 1 arc-second | **Copernicus DEM GLO-30** (surface model, EU-wide) | ESA / Airbus | Copernicus licence, as in REGIONS.md | collection page opened |
| Terrain, alternative | | DGT's MDT products (national bare-earth models, 2 m and 5 m grids) | DGT | not read | **not checked** (portal is a JavaScript application) |
| Water (`water.sqlite`) | NHD | OpenStreetMap waterways, already in the base map; APA hydrography as a dedicated source | OSM contributors; APA | ODbL; not read | OSM opened; APA **not checked** (did not respond) |
| Dams | NID | **OSM `waterway=dam`** (805 ways, 35 nodes) and 1,922 weirs; APA's dam inventory as a dedicated source | OSM contributors; APA | ODbL; not read | OSM counts from taginfo (opened); APA **not checked** |
| Crops (`crops.sqlite`) | USDA Cropland Data Layer | **COS 2018** (Carta de Uso e Ocupação do Solo; vineyards, orchards, olive groves classes) as the national source; CORINE Land Cover classes 221–223 as a coarse fallback; OSM `landuse=vineyard` (35,746 ways) and `landuse=orchard` (46,597 ways) in the meantime | DGT; Copernicus / EEA; OSM contributors | not read; CLMS data policy; ODbL | COS site **not checked** (did not resolve); OSM counts from taginfo (opened) |
| Buildings (`buildings.sqlite`) | Microsoft US footprints, OSM heights | **Overture buildings** (which already blend OSM and Microsoft); Microsoft global footprints cover Portugal (46 tiles in `dataset-links.csv`) | Overture; Microsoft | ODbL | Microsoft README and links CSV opened; Overture attribution as in REGIONS-ES.md |
| Places (`places.sqlite`) | GNIS, Census gazetteers, OSM | **GeoNames `PT.zip`** (main-level names, 1.2 MB); INE municipality register; OSM named places | GeoNames; INE; OSM contributors | CC BY 4.0; see notes; ODbL | GeoNames `PT.zip` opened (size read); INE page **not checked** (portal under maintenance) |
| Addresses (`addresses/<code>.sqlite`) | Overture addresses | **Overture addresses: 5,607,450 Portuguese rows** (source `pt/countrywide`: INE via OpenAddresses, CC BY 4.0); the INE CSV as the raw alternative | Overture Maps Foundation, from the INE through OpenAddresses | CC BY 4.0 | rows queried with DuckDB; source file opened; dataset downloaded and read |
| Points of interest (`poi` rows) | OSM | OSM, as today | OSM contributors | ODbL | counts from taginfo (opened) |

Notes:

- **Terrain.** Unlike Spain, Portugal's national bare-earth model (DGT's MDT, 2 m and 5 m) could not be verified today:
  the DGT portal is a JavaScript application and its INSPIRE mirror was under maintenance. Copernicus GLO-30 is a
  surface model, so bare-earth detail suffers the same canopy and roof caveat REGIONS-ES.md warns about. The MDT needs
  a check in a browser before any build; the proposal starts from GLO-30 meanwhile.
- **Water and dams.** Portugal's dams are mapped in OSM as `waterway=dam` — the wall, not the reservoir: 805 ways and
  35 nodes, plus 1,922 weirs. Notably **nothing** carries `man_made=dam` or `barrier=dam` (both 0), so any dam layer
  built from OSM must read the `waterway` tag, which the US builder does not (it reads NID records). APA's own
  inventories (barragens, hidrografia) did not answer from our route; they are the strong source when a person can open
  them in a browser.
- **Crops.** `tools/build_crops.py` keeps orchards, vineyards and berries. OSM alone holds 46,597 `landuse=orchard`
  ways and 35,746 `landuse=vineyard` ways in Portugal (the Douro alone is one of Europe's densest vineyard
  landscapes); of the orchard ways, **4,975 carry `trees=olive_trees`** — the olive groves are a subset of the orchard
  count, not a separate figure — which is enough for a first pass. COS 2018 is the national land-use map and the right
  permanent source, but its site did not resolve on the day; CORINE classes 221–223 (vineyards; fruit and berry
  plantations; olive groves) are the fallback the Spain proposal also names.
- **Places.** GeoNames' Portugal extract is small (1.2 MB) but holds the main gazetteer; INE's own municipality codes
  and the DGT's toponymy would be the official sources once the portals are reachable. OSM named places come with the
  base map regardless.

## Addresses in Overture, measured

The Portuguese rows of Overture release `2026-10-21.0` (the release `tools/build_addresses.py` defaults to) were read on
20 November 2026, with the same DuckDB query the builder uses but `country = 'PT'` — in full:

```sql
SELECT count(*), count_if(number IS NOT NULL), count_if(street IS NOT NULL), count_if(postcode IS NOT NULL),
       count_if(postal_city IS NOT NULL AND postal_city <> '')
FROM read_parquet('s3://overturemaps-us-west-2/release/2026-10-21.0/theme=addresses/type=address/*',
                  hive_partitioning = 1)
WHERE country = 'PT';
```

- **5,607,450 addresses**, from one source: `pt/countrywide`
  (source file: [sources/pt/countrywide.json](https://github.com/openaddresses/openaddresses/blob/master/sources/pt/countrywide.json),
  "Portugal, distributed by OpenAddresses", from the INE's INSPIRE ATOM service, upload dated 2026-02-23 in the source
  file). The raw INE CSV as published on OpenAddresses holds **5,911,139 rows** — 303,689 more than the Overture
  release carries. The difference was not investigated; the source file was uploaded after the release's ingest may
  have run.
- **Postcodes.** All 5,607,450 rows have a postcode, and every one is 7 characters or more: the Portuguese format
  `3050-020` (four digits, hyphen, three) survives into Overture unchanged. **This needs one builder change**: the
  builder's `coalesce(left(postcode, 5), '')` keeps the hyphen ("3050-") where Spain's five-digit codes fit exactly.
  Portugal wants the four digits before the hyphen (`left(postcode, 4)`), or the hyphen stripped and the postcode kept
  whole.
- **House numbers.** 4,367,129 rows have a number (77.9%). There is no `S-N` convention in the Portuguese data
  (0 rows, against Spain's 308,341). With the builder's `number IS NOT NULL AND street IS NOT NULL` filter,
  **4,365,268 rows would build**.
- **Levels.** `postal_city` is always empty, and `address_levels[1]` holds the municipality in capitals (LISBOA 324,265,
  PORTO 136,764, VILA NOVA DE GAIA 90,860, AMADORA 87,840 …). This is the same shape Spain found, with the same
  collision risk: the builder's `state = address_levels[1].value.upper()[:2]` turns MAIA into `MA` (Massachusetts),
  ALMADA into `AL` (Alabama) and COIMBRA into `CO` (Colorado). A region
  cut must come from the postcode or the box, never from this field.
- **Coverage by region.** The INE CSV's rows were counted against the ten region boxes at **full precision** — the
  CAOP polygon bounds and the address groups' own exact extremes, not the four-decimal figures printed in the table
  (counting against those printed figures drops three rows in each island group, whose true extremes carry more
  decimals; an earlier revision of this proposal made exactly that mistake, and the counts below supersede it). The
  totals double-count at borders: **1,842,324 of 5,911,139 rows fall inside two or more boxes**, because neighbouring
  regions' bounding boxes overlap over large areas (the Norte and Centro boxes alone share a 2°-by-0.3° band).
  Per-box counts, for scale only, with double counting: Norte 1,957,747; Centro 1,414,055; Oeste e Vale do Tejo
  1,422,122; Grande Lisboa 1,163,567; Península de Setúbal 1,067,495; Alentejo 692,635; Algarve 393,246; Madeira
  130,271; Azores east 108,523; Corvo and Flores 2,374. The three island counts equal their source groups exactly,
  and the seven CAOP continental boxes contain every mainland row — the first revision's GISCO boxes left 1,540
  mainland rows outside them, all labelled geocoding outliers. The 36.93° N row above is not one of those — it
  supplies the southern edge of the `pt-20e` box itself. Unlike
  Spain — whose province-coded postcodes cut exactly — **a per-region address index for Portugal cannot be cut by
  box**; it needs a municipality→region table from the INE municipality register, or per-municipality cuts keyed on
  the address rows' own city field.
- **Postcode prefixes.** The first two digits do not carry province codes the way Spain's do (01–52). The exact
  prefix→locality table is the CTT dictionary, which is not open. A per-region index cut would come from a
  municipality→region table built from INE's municipality register.

## Addresses measured, by group

The raw INE CSV (5,911,139 rows, download date 2026-02-23, CC BY 4.0) was read row by row on 20 November 2026:

- **5,911,139 addresses**, every one with coordinates and a postcode (100%), 96.0% with a street, 99.9% with a city,
  74.7% with a house number. Ids are `pt.ine.add.<group>.<n>` with four groups: `PTCONT` (mainland, 5,669,971),
  `MAD` (Madeira, 130,271), `AC26` (the Azores' central and eastern islands, 108,523) and `AC25` (Corvo and Flores,
  2,374).
- The per-group bounding boxes, computed from the rows: mainland -9.5060…-6.2118, 36.9724…42.1531; Madeira
  -17.2578…-15.8637, 30.0351…33.0886; Azores east -28.8250…-25.0138, 36.9295…39.0954; Corvo and Flores
  -31.2660…-31.1104, 39.3749…39.6766. Every mainland edge falls inside the CAOP-derived region boxes (Cabo da Roca,
  the eastern border, Melgaço in the north and the Faro islands in the south), which cross-checks both datasets.

## Help points

OSM is the practical source for help points across Portugal, as in Spain: no national open list with coordinates was
confirmed for hospitals, pharmacies, fire stations or police on the day (the INE and SNS portals did not answer or
rendered as JavaScript shells). Counts from the taginfo database for Geofabrik's Portugal extract, data until
19 November 2026 21:19 UTC (opened):

| Tag | Nodes | Ways | Notes |
|---|---|---|---|
| `amenity=pharmacy` | 2,614 | 147 | |
| `amenity=fire_station` | 91 | 491 | Portugal's ~577 bombeiros are mapped as building outlines, not points |
| `amenity=police` | 513 | 484 | PSP and GNR posts |
| `amenity=hospital` | 66 | 244 | |
| `amenity=clinic` | 1,697 | 800 | includes the *centros de saúde* of primary care |
| `amenity=drinking_water` | 5,968 | 14 | public fountains |
| `man_made=water_tap` | 1,176 | 103 | |
| `man_made=water_well` | 1,443 | 8,463 | wells mapped as areas in the countryside |
| `natural=spring` | 1,169 | 63 | not drinkable by default |
| `emergency=water_tank` | 40 | 246 | firefighting ponds, common in the south |
| `emergency=drinking_water` | — | — | the German emergency-well tag is not used in Portugal |
| `emergency=defibrillator` | 394 | 1 | thin: see below |
| `tourism=wilderness_hut` | 12 | 24 | Portugal has almost none of Spain's hut network |
| `tourism=alpine_hut` | 12 | 6 | |
| `amenity=fountain` | 2,392 | 1,364 | decorative; drinking only with `drinking_water=yes` |
| `amenity=shelter` | 952 | 2,630 | bus stops included; needs the `shelter_type` filter |

Two Portuguese kinds of point are worth a decision:

- **Defibrillators.** OSM's 395 are thin. Portugal's DAE (desfibrilhador automático externo) registry exists inside the
  SNS/DGS systems, but its open-data state was not confirmed today. As in Spain, a defibrillator layer would be OSM
  with a plain "incomplete" note, or a national registry if one is published.
- **Dams as help points.** 840 `waterway=dam` walls and their reservoirs are the water layer the map already knows how
  to show; whether a dam carries help-point semantics (a water source in an outage) is a maintainers' decision, the
  same one REGIONS-ES.md leaves open for their inventory.

## Licence notes and attribution

All proposed licences allow redistribution in a free offline library, including commercial reuse, except where noted.

- **CC BY 4.0, INE addresses.** Attribution "Instituto Nacional de Estatística, I.P.", as the source file requires;
  derived works add the standard CC BY 4.0 notice.
- **Eurostat GISCO — research only as proposed.** GISCO's statistical geometry terms (the
  [statistical-units page](https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units), opened 20 November 2026)
  grant free use "on condition that the data will not be used for commercial purposes", with the source acknowledged
  and the copyright notice "© EuroGeographics" visible in any publication using the data. This proposal — research
  documentation in a free repository — fits that. **Boxes derived from GISCO boundaries for redistribution inside a
  shipped map pack would not**, because the library's own distribution involves commercial links (the buying guide's
  affiliate). That case no longer arises for the polygons: every polygon box in this proposal is now measured from the
  CAOP (DGT's open administrative cartography, CC BY 4.0 — see the 22 and 25 November entries in "What was checked,
  and what was not"), and the GISCO numbers remain as the cross-check the first revision was verified against.
- **Copernicus DEM.** The Copernicus licence, as in REGIONS.md.
- **ODbL** (OSM, Overture buildings, Microsoft footprints). As for the US map.
- **CC BY 4.0, GeoNames.** "Give credit to GeoNames."
- **DGT cartography** (CAOP, COS, MDT). The CAOP's terms are confirmed — CC BY 4.0, attribution to the
  Direção-Geral do Território, high-value dataset under Regulation (EU) 2023/138 with no INSPIRE use constraints
  (see the 22 November entry in "What was checked, and what was not"). COS and MDT remain not checked; to verify in
  a browser before any build that uses them.

These notices would belong wherever the map already credits its sources, and in `THIRD_PARTY_NOTICES.md` if that is
where the maintainers keep data licences.

## How addresses are written

- **Order.** Street type first, then street, number, floor and door, then postcode and town: "Rua das Flores, 12,
  1.º Dto., 1200-195 Lisboa". Common types: *Rua*, *Avenida*, *Travessa*, *Praça*, *Largo*, *Calçada*, abbreviated
  *R.*, *Av.*, *Tv.*, *Pça.*. The number often carries a floor and door: "45, 3.º Esq.".
- **Postcode.** Four digits, hyphen, three (`1200-195`); the -3 suffix is the locality extension. No leading zeros:
  postcodes run 1000–9999, and the smallest two-digit prefix, 10 (Lisbon), holds 32,775 rows in the INE CSV. It must
  stay text because of the hyphen.
- **Names.** Portuguese street names carry accents and cedillas, and Overture's source keeps them: the INE CSV holds
  cities such as `BARCOUÇO`. **The current code breaks these words, worse than Spain's case**: `normalize()` and
  `norm()` keep only `a-z0-9`, so `BARCOUÇO` normalises to `barcou o`, `São Pedro` to `s o pedro`, `Póvoa` to
  `p voa` and `Espírito Santo` to `esp rito santo` (tried on 20 November 2026 with the repository's own code). Accents
  and the cedilla need folding (á to a, ç to c, õ to o) on both sides, or every *São*, *Água* and *Condomínio* street
  would be unreachable.
- **What the current code does with that** (read in this repository and tried on examples on 20 November 2026):
  - `is_address_query()` needs a number first, so "Rua das Flores 12" and "R. Flores, 12, 1200-195 Lisboa" are not
    treated as addresses. Only "12 Rua das Flores" style input is.
  - `_parts()` takes a postcode only as the last word, while Portuguese addresses put the town after the postcode.
  - The postcode-prefix and state problems are described in "Addresses in Overture, measured".
  - `tools/build_addresses.py` keeps only `country = 'US'`.
  - The hyphen in the postcode needs the builder change noted above.

## Still undecided

Each of these needs the maintainers' decision before any code changes. The first two are shared with REGIONS.md and the
other proposals.

1. **Codes in setup.** Whether `pt-11` and the others are acceptable as a manifest `id`, in `map-set.json` and in
   `Lastbook-Map-<id>.zip`, and how setup groups ten Portuguese regions; `carry_over()`'s `[A-Z]{2}` pattern must
   learn the new shape (see "Codes").
2. **The box rule**, as in REGIONS.md — the boxes now come from the CAOP (the official administrative cartography,
   CC BY 4.0); the 0.15° outward margin and the outward rounding stay as proposed.
3. **NUTS II or districts.** ISO 3166-2:PT lists the 18 districts and the two autonomous regions, and districts are the
   division Portuguese readers know by name; but districts have had no administration for decades, while NUTS II is
   what the CCDRs and the INE data actually run on. This proposal uses NUTS II. If the maintainers prefer ISO codes,
   the boxes would be recomputed from the CAOP's district boundaries.
4. **Postcodes.** The CTT dictionary's terms were not verified today (expected to mirror the Correos case in
   REGIONS-ES.md). Postcode *areas* are not proposed; the per-address postcode in the CC BY 4.0 INE data is what
   address search needs.
5. **The Azores split.** Whether `pt-20w` / `pt-20e` are acceptable codes, and whether Corvo and Flores (2,374
   addresses, no large settlements) deserve their own map set or should ride with `pt-20e`'s set and a wider box. The
   boxes as proposed leave out the Formigas reserve (see the cross-checks above); widening `pt-20e` eastward to about
   -24.70° would include it, at no practical gain for a street map.
6. **New crop kinds.** Vineyards and olive groves are large land uses in Portugal (35,746 vineyard ways and 46,597
   orchard ways, of which 4,975 carry `trees=olive_trees`); whether
   the crops layer grows kinds beyond orchards, vineyards and berries is the maintainers' decision, the same one
   REGIONS-ES.md leaves open for olives and citrus.
7. **Mirandese names.** Whether the place finder indexes both forms for Miranda do Douro (one municipality; small
   change).
8. **Terrain before a build.** Whether the DGT's MDT (bare earth, 2 m / 5 m) is reachable and open — it should replace
   GLO-30's surface model if the licence and download are confirmed.

## What was checked, and what was not

- **Opened on 20 November 2026:** OpenAddresses' source file for Portugal (`sources/pt/countrywide.json`) and the INE
  dataset itself (downloaded and read row by row: 5,911,139 rows); Overture release 2026-10-21.0's Portuguese rows
  (queried with DuckDB, counts above); GISCO's NUTS 2024 1:3M GeoJSON (boxes computed); Microsoft
  GlobalMLBuildingFootprints' README and `dataset-links.csv` (46 Portugal tiles); GeoNames' `PT.zip` (existence and
  size); Copernicus' COP-DEM collection page; the taginfo database for Geofabrik's Portugal extract (API, data until
  19 November 2026 21:19 UTC); this repository's address builder and engine code.
- **Computed on 20 November 2026:** the ten region boxes (seven from GISCO NUTS 2024 1:3M polygon bounds, two from the
  INE address groups' own points, Madeira from its GISCO polygon), widened 0.15° and rounded outward — floor for the
  west and south edges, ceil for the east and north; the address dataset's per-group bounding boxes; the address
  counts and checks, from the INE CSV and from Overture.
- **Also opened on 20 November 2026:** Eurostat's statistical geometry terms page (the GISCO licence note above).
- **Seen only in search results:** the Mirandese law (Lei 7/99; the DRE page rendered as a JavaScript shell); the CTT
  postcode dictionary's commercial terms; the Formigas reserve's published bounds (the regional government document
  cited in review, placed at about 37.25–37.27° N, 24.71–24.75° W).
- **Also opened on 21 November 2026:** Portuguese Wikipedia's coordinates for the Formigas islets (37.2697° N,
  -24.7137° W — which corrected this proposal's earlier identification of the 36.93° N edge) and Nominatim's search for
  the name (two street-level matches, no islet match, consistent with the reserve being outside OSM's addressable
  data); taginfo for Geofabrik's Portugal extract for `trees=olive_trees` (4,975 ways) and `produce=olives` (2);
  Nominatim's reverse geocode of the 36.93° N address row itself (nearest feature: the Farol de Gonçalo Velho node,
  15 m away), and the row's own fields, which settle where that row is — see the cross-checks.
- **Updated on 22 November 2026 (the Madeira pilot, [MAP-PT-30.md](MAP-PT-30.md)):** the CAOP download and licence,
  which the "Not checked" list was waiting on. DGT's portal turns out to be reachable without a browser: the page
  renders server-side and the downloads are direct (`CAOP_RAM_2025-gpkg.zip`, 15 MB, SHA-256 recorded in the pilot's
  `build.json`). The licence is confirmed from DGT's own open-data page — CC BY 4.0, attribution to the Direção-Geral
  do Território — and the SNIG register records the CAOP as a high-value dataset under Regulation (EU) 2023/138 with
  no INSPIRE use constraints. Madeira's box was re-measured from the official polygon (EPSG:5016, converted vertex by
  vertex): the north edge moves 0.024° north — Porto Santo's tip, which the GISCO 1:3M generalisation does not carry —
  and west, south and east agree with GISCO by at most 0.0031°. All 130,271 address rows sit inside the official box, and
  the address count reproduces exactly. The pilot's builder, `tools/build_region_pt_30.py`, cuts the Geofabrik
  country extract to this box and passes it to the repository's own `tools/build_map.py` unchanged.
- **Updated on 25 November 2026:** the seven continental boxes, re-measured from the official CAOP 2025 polygons the
  same way Madeira was. `CAOP_Continente_2025-gpkg.zip` (111,647,845 bytes, SHA-256
  `87cd67f4b1fbadf23d9324e6fb231ff05531e4db347af36ccc7c6cbabe3ecd1d`) holds one geopackage,
  `Continente_CAOP2025.gpkg`, in ETRS89 / Portugal TM06 (EPSG:3763; projection parameters read from the file's own
  WKT), with the seven NUTS II polygons carrying about 319,000 vertices in total. Every vertex was converted to
  lon/lat. Against the GISCO 1:3M bounds, all but three of the 28 edges agree within 0.01° (the exceptions are the
  Atlantic-coast edges listed in the cross-checks above). The CAOP boxes contain all 5,669,971 mainland address rows,
  and the per-box counts and the overlap total in "Addresses in Overture, measured" were recomputed against them.
- **Not checked:** the INE INSPIRE portal (under
  maintenance on the day); COS 2018 (site did not resolve); APA/SNIAmb hydrography and dam inventories (did not
  respond); the MDT-DGT products and licence; the INE municipality register; the ISO 3166-2:PT codes against the ISO
  site; the reason for Overture's row count differing from the raw INE CSV. The CAOP download and licence, which this
  list held, moved to the checked list on 22 November 2026 (see above).
