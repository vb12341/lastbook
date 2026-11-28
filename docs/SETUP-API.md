# Lastbook first-run setup: the shared contract

One download per computer: the Mac app (dmg) or the Windows installer (Setup.exe). On first open, Lastbook finds no library and
opens **setup** in its own window. Setup shows what this computer has, recommends a library (Small, Medium or All) and whether
the optional local AI fits, then downloads, checks, unpacks and switches to it. No separate library or model download, no
manual unzip. Setup also opens later from inside the app (Library → *Library and local AI*) to upgrade, add the AI or repair.

Several sessions build this at once. This file is the interface between them. Change it only together, and say so.

## Owner decisions this follows

- **The one way out.** Setup's downloader is the only code in Lastbook that may reach the internet, and only when the person
  starts or resumes a download. It fetches over HTTPS only from hosts in `ALLOWED_HOSTS` (below), with no proxy taken from
  the system, and follows a redirect only to another allowed host. Everything else stays as it is: the windows block all web
  loads, the assistant talks to loopback or pipes only, and nothing is sent anywhere (no telemetry; no update check at
  startup unless the person turned on "Notify me about app updates", see `updates` below). The owner authorized this route on 2026-11-12 for the first-run install. It replaces "downloads happen outside
  the app" for library packs and the model, and nothing more.
- **No blanket memory floor.** The local AI is offered when the measured fit says it fits (`llm_local.judge`, about 18 GB
  of memory for model and context, plus a 2.5 GiB reserve). It is not offered below that, and the screen says why in plain words.
- **A working library is never broken.** New data is downloaded and unpacked beside the old one, checked, then switched
  in one rename. A failure at any step leaves the old library as it was.

## Who owns what

| Part | Owner (session) | Files |
|---|---|---|
| Downloader, checks, unpacking, switching, hardware, manifest, `/api/setup/*` | **setup-backend** | `engine/setup/` (new package), `packaging/release_manifest.py`, setup mode in `app/main.py` (and the smallest shell change it needs) |
| Setup screens | **setup-ui** | `web/setup.html`, `web/setup.js`, `web/setup.css`; one link in `web/app.js` (Library page) |
| Packs: build, zip, upload, manifest entries | **publish-packs** | `derived/packs/`, `derived/release/`; writes `derived/release/manifest-v1.json` entries for each verified pack |
| Domain for downloads | **download-domain** | DNS, R2 custom domain, URLs in `website/` |
| Windows runner, Windows package and installer, Windows tests | **windows-ai** | `packaging/llm/*windows*`, `packaging/build_app_windows.sh`, `packaging/lastbook_setup.py`, `packaging/scan_app_package.py` |
| Local AI engine, assistant, Mac package, Mac tests, final report, website wording at release | **main session** | `llm_local.py`, `assistant.py`, `server.py`, `web/chat.js`, the rest of `web/app.js`, Mac packaging |

Re-read a file just before editing it. Commit nothing and switch no branch: the owner reviews the working tree.

## The manifest (`manifest-v1.json`)

The app carries a copy made at build time (`packaging/release_manifest.py` → bundled `setup/manifest-v1.json`). Setup may
also fetch `https://downloads.lastbook.org/manifest-v1.json` (one GET, only when the person opens setup online) to learn of
packs published after the app was built. A fetched manifest is used only if it parses, its `version` is 1, and every URL is
on an allowed host.

```json
{
  "version": 1,
  "published": "2026-11-12",
  "packs": [
    {"id": "small", "title": "Small", "summary": "…one plain sentence…", "records": 315,
     "installed_bytes": 8236931672, "available": true,
     "files": [{"name": "Lastbook-Small.zip", "url": "https://downloads.lastbook.org/1.0.0/Lastbook-Small.zip",
                "size": 8236931672, "sha256": "…", "kind": "zip"}]}
  ],
  "models": [
    {"id": "gemma-4-26b-a4b", "title": "Gemma 4 26B A4B", "file": "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf",
     "url": "https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF/resolve/c099eb48e663fd284577b04978a94ffccb261841/gemma-4-26B-A4B-it-UD-Q4_K_M.gguf?download=true",
     "size": 16947541728, "sha256": "f2c28b3dc4776931ac6f879e11f203dec637ea0f14267a86ec8f6165f63f293f",
     "memory_bytes": 19027384256, "available": true}
  ]
}
```

`installed_bytes` is the unpacked size, measured. A pack with `"available": false` shows as *coming later* and can't be chosen.
Packs may be split into several files later; setup downloads all `files` of a pack, in order.

`ALLOWED_HOSTS`: `downloads.lastbook.org`, the bucket's current `*.r2.dev` host (until the domain is live), `huggingface.co`,
and the hosts Hugging Face redirects model downloads to (`cas-bridge.xethub.hf.co`, `*.hf.co`; on 2026-11-12 the model resolved to `us.aws.cdn.hf.co`). R2 ignores If-Range, so the downloader compares the ETag itself.
A new ETag on a resume does not by itself start the file over, because the same file uploaded again gets one: three 1 MiB
stretches of what is on disk (start, middle, end) are fetched again and compared. If they are the server's, the download
carries on under the new ETag, and the SHA-256 at the end still judges the whole file; if a byte differs, it starts over.
If a stretch cannot be fetched, that is a broken or busy connection like any other, and the partial file is kept (#208).
A file of `PARALLEL_MIN` (512 MiB) or more is fetched over `CONNECTIONS` (8) connections at once, each with a closed Range
for one `PIECE` (16 MiB). One connection to `downloads.lastbook.org` gave about 2 MB/s and 8 about 13 MB/s (2026-11-17).
Pieces are handed out in order, at most `AHEAD` (16) past the first unfinished one. The run of finished pieces from byte 0 is
hashed as it completes, read back from the disk, so the SHA-256 is ready when the last piece lands. Every 206 must carry the
first one's ETag and total size: another ETag is checked as above against the hashed run (the same bytes: the run is kept and
the pieces carry on; other bytes: the file starts over; a second new ETag: it goes on over one connection), another size
fails as `checksum`, and a 200 to a Range goes on over one connection from the hashed run. The exact list is a constant in
`engine/setup/download.py`, and the package scan checks that it is the only module that opens connections.

## Where things go

- The library folder stays **LastbookData** (layout unchanged: `library-data/`, `derived/`). Default place
  (`app/main.py` `default_choice`): `%LOCALAPPDATA%\Lastbook\LastbookData` on Windows, or
  `<system drive>:\Lastbook\LastbookData` when the user folder's path is too long for the library's longest file names;
  `~/Library/Application Support/Lastbook/LastbookData` on a Mac; `$XDG_DATA_HOME/Lastbook/LastbookData` on Linux
  (`~/.local/share/Lastbook/LastbookData` when that is not set). Or a folder the person chooses, on a disk with room.
  `settings.json` remembers it as before (`app/main.py`).
- Work in progress: `<parent>/LastbookData.setup/`, holding `downloads/` (partial files with a `.part` suffix and a
  `.json` beside each: url, size, sha256, bytes done, ETag, and `"parallel": true` while pieces may lie past bytes done; a resume then keeps only the hashed run, so at most `AHEAD` pieces are fetched again) and `unpack/` (the new LastbookData being built).
  A switch that fails leaves `kept-<time>/LastbookData/` there: the person's files (`models/`, `derived/geo/addresses/`)
  carried into the staged library before it failed, and named in the error. Setup never deletes it: not a later job, its
  cancel, nor a later successful setup. The person can delete it once the library in use has its AI and address search
  again (or they no longer want them); the work folder itself goes once no `kept-*` is left.
- The model goes to `LastbookData/models/<file>`. After its SHA-256 matches, setup calls `llm_local.mark_checked(path)`, so
  the AI does not read 17 GB a second time.
- Space needed = download size + unpacked size + 2 GB, on the disk that holds the target. A zip is deleted once it is
  unpacked and checked. Setup refuses to start, with the numbers, when there is not enough room.
- Manual downloads are recognized (`found()` in `engine/setup/api.py`) in these places only, and offered under *Found on
  this computer* as *Use the file you already have*:
  - a `Lastbook-*.zip`, or a file named like one in the manifest (pack, model, state map, address index), in the folder
    that holds LastbookData (the default place above, without `LastbookData`), or in the open library or its `models/`;
  - a file the person picks with *Choose a library zip or AI model file*;
  - an existing LastbookData at the remembered place or the default one (`app/main.py` `places`).

  Downloads, Documents and Desktop are not searched. A file is used only after its size and SHA-256 match the manifest.
- Chat's local AI looks for the model only in the open library's `models/` and the library folder itself, and in
  `LASTBOOK_MODEL_DIR` when that is set (`llm_local.py` `places`). *Look again* searches those again.
- If the local AI download stops as `unreachable` (also after an earlier try got part of it), or with a network error before
  gaining any bytes, the Stopped card also
  offers **Choose AI model file**. Choosing the published model file resumes that same job, keeping any finished library or
  map steps. Its size and SHA-256 are checked as usual; a file that does not match is left alone and is not used. An
  `unreachable` stop also shows the file's name, exact size, address and SHA-256, so it can be fetched on another network
  or computer (#254).

## Engine interface: `/api/setup/*`

JSON over the app's bridge, so no port. POSTs carry the launch key like every other POST. In setup mode (no library yet) these
are served by `engine/setup/api.py` alone. In normal mode `server.py` hands `/api/setup/` to the same module.

| Request | Answer |
|---|---|
| `GET /api/setup/state` | `{mode: "first-run" or "manage", hardware, manifest: {source: "bundled" or "fetched", packs, models}, current: {folder, pack, model, cloud_only, cloud_counting} or null (cloud_only: `{files, folders, bytes, examples}` of the library that are only in iCloud, counted from metadata in the background; null until counted and on other systems. cloud_counting: true only while a Mac counts, so the page knows when to ask again), found: [...], recommend: {pack, ai, why}, job}` |
| `POST /api/setup/start` `{pack, ai: true or false, folder?}` | the new `job`, or `{error, need_bytes, free_bytes}` |
| `GET /api/setup/job` | `job` |
| `POST /api/setup/pause`, `/resume`, `/cancel`, `/retry` | `job` |
| `POST /api/setup/use` `{path, library?, folder?}` | uses a library or file already on this computer: `job` (verify, then unpack or activate). A library zip is installed in `folder` as it is, with nothing downloaded. A model, map or address file in first-run is kept for the next `start`: `{job: null, noted: true, kind}`. With `library: true` (*Open another library folder* in manage mode) only an existing LastbookData, or the folder holding one, is accepted: a file or a folder of pack zips is refused (`code: not_library`) and nothing starts. The library that is open now is refused (`code: current`) |
| `POST /api/setup/finish` | `{ok: true}`; the app then opens the library (see below) |
| `POST /api/setup/pick` | `{path}` from the system's folder picker, or `{path: null}`: a place to install (`start {folder}`) or something to `use` (a LastbookData, a folder holding one, or a folder holding a pack zip) |
| `POST /api/setup/start` `{copy_to}` | manage mode: copies the open library to `<copy_to>/LastbookData` for someone with no internet (#114): the files in `pack-manifest.tsv` (or a state map's `map-set.json`) and the manifest, each written as `.partial`, read back from the drive, and renamed only when what the drive gives back has the right SHA-256; then `START-HERE.txt` beside it (`START-HERE (Lastbook).txt` if the drive already has someone else's). Refused with `code: "fat"` on a FAT drive when a file is 4 GB or more; room is counted in the drive's clusters; a file whose real path leads outside the library refuses the copy. Nothing is downloaded and nothing of the person's own is copied. Item `kind: "copy"`, state `copying`; `result: {files, bytes, folder, start_here}`; cancel keeps what is copied, and copying to the same place again carries on |
| `POST /api/setup/delete-old` `{path}` | deletes a `LastbookData.old-*` setup moved aside itself (listed in `current.old`): `{ok, deleting}` |
| `POST /api/setup/remove-ai` | manage mode: deletes `LastbookData/models/<file>` (the library stays): `{ok, freed_bytes}` |
| `POST /api/setup/server-models` `{address}` | the models of a model server on this computer (#78): `{base, address, models}` from its `/v1/models`, or `{error, code}`: `not_here` (not a loopback address; nothing is asked), `no_answer`, `needs_key` (401 or 403: no key is sent yet), `no_list`, `no_models`. The one request made before a choice, and only when the person presses *List models* |
| `POST /api/setup/use-server` `{address, model}` | lists the models again and saves the choice in `setup.json` (`model_server: {base, model}`): `{model_server: {address, model}}`, or `not_listed`. While a server is chosen, Chat uses it instead of the local AI (text only, no `view_page`); the local AI file stays. In manage mode the open library switches at once; a choice made in first-run setup counts when the library opens. `SURVIVAL_MODEL_BASE` set at start wins over the saved choice until the person chooses again |
| `POST /api/setup/stop-server` | forgets the choice: `{model_server: null}`; Chat goes back to the local AI |
| `GET /api/setup/updates` | "Notify me about app updates" (engine/update.py `Notices`): `{supported, notify, available, checking}`. `supported: false` outside the desktop app |
| `POST /api/setup/updates` `{notify: true\|false}` | sets it, from the person's own checkbox only (Setup's choose screen, Settings). Off clears any saved notice at once. Starts no check. Error `code`: `save`, `unsupported`, `bad_request` |
| `POST /api/setup/start` `{repair: true, ai}` | manage mode: re-checks every file against the library's `pack-manifest.tsv` and the model's SHA-256 (items `verifying`); fetches again only what is wrong (the pack: downloaded and switched to like an upgrade; the model: replaced in place). All well: `done`, nothing downloaded |

Added by setup-backend on 2026-11-12: `pick` (answers `{path, free_bytes, disk}` or `{path: null, cancelled: true}`), `delete-old`, `remove-ai` and repair above; `hardware.disks[].name` (Macintosh HD, "Data (D:)"); `current.old` (old libraries kept after an upgrade);
`recommend.folder` (where it would go) and `recommend.need_bytes` (`{pack id: bytes}` per pack); each `found` entry is
`{kind: "library" or "pack" or "model", path, name, id, size, size_ok}`. An error answer is `{error, code}` (with HTTP 400,
409 for `busy`/`not_done`, 507 for `no_space`). In normal mode the setup page is `/ui/setup.html`, which the bridge now allows as
the window's top page; in setup mode it is also served at `/`. An upgrade or a new library is switched to when the app restarts
after `finish` (the job ends `done` with the switch staged), so nothing is renamed while it is open.

`hardware` = `{os: "macos" or "windows", arch: "arm64" or "x64", ram_bytes, unified_memory: true or false, gpus: [{name,
vram_bytes}], disks: [{path, free_bytes, total_bytes}], ai: {fits, needs_bytes, why}}`. On a Mac `ai.fits` comes from
`llm_local.judge` of a real `lastbook-llm --measure` once the model file is present. Before that, it is an estimate from
`memory_bytes` and RAM, marked `"estimate": true`. On Windows, RAM plus dedicated VRAM, from DXGI.

`job` = `{id, state, items: [{id, kind: "pack" or "model", title, state, bytes_done, bytes_total, rate_bps, eta_s,
error}], error, started, folder}`, plus `repair: true` on a job started with `{repair: true}`, and `stopping: true` from `cancel` until the cancelled job's worker has stopped (`start`, `resume` and `retry` answer `busy` until then; `resume` or `retry` of a job that is already running return that job). Each item also has `source`: `"found"` when its file is already on this computer
(it is checked, not downloaded), else `"download"` (it changes to `"download"` when a found file is rejected). After a restart, an
item that stopped mid-run has `paused_from` (`downloading`, `verifying`, `unpacking` or `activating`), dropped on resume, and its
`bytes_done`/`bytes_total` describe that step: unpacked bytes of the installed size for `unpacking`, the whole file for a found item. States in order: `waiting`, `downloading`, `paused`, `verifying`, `unpacking`,
`activating`, `done`, or `failed` (with `error`: a plain sentence and a `code`: `no_space`, `network`, `server` (the download
server kept answering 5xx or 429 for 40 minutes, so the internet itself was up), `checksum`, `unpack`,
`unreachable` (the connection kept breaking on a host that never answered this download: a network that blocks or resets it, or no internet; the error carries `host`, and the file's published `name`, `size`, `url` and `sha256`), `activate`, `check` (a Check now, `start {check: true}`, could not read a file; it downloads and changes nothing), `cancelled`, and `found_mismatch`: a file already on this computer (`use`) did not match its published SHA-256; the error carries `file`, the file is left untouched and no longer offered, and `retry` downloads it instead). The job lives in the app's own settings folder (`<settings>/setup/job.json`, with a staged switch in `<settings>/setup/pending.json`; never in `LastbookData.setup`, whose contents are not obeyed: audit E3), so a restart picks it up as `paused` and resumes
with HTTP Range from the bytes already on disk. While downloading, a broken connection is retried six times over about 90 seconds.
A server that answers 5xx or 429 is waited on longer (5 s rising to 5 minutes, or its `Retry-After` up to 10 minutes, and
40 minutes in all without progress). While such a wait is ahead, the item has `retry_in_s`, the seconds until the next try.
A corrupt download (wrong SHA-256) is deleted and downloaded once more
automatically, then fails with `checksum`.

## Switching to the library

`finish` writes `settings.json` and tells the shell to load `lastbook://app/` (Mac) or `https://lastbook.invalid/` (Windows)
with the engine now started on the new folder. If an in-process switch proves fragile, the app may relaunch itself instead,
with the same result for the person. Either way, an upgrade (for example Small to Medium) renames the old LastbookData to
`LastbookData.old-<date>` only after the new one is complete and opens, and offers to delete the old one afterwards.

## State maps (added 2026-11-12)

The owner wants setup to offer one US state's detailed map with any library: local streets, buildings, terrain,
crops and the resource layers (water points, help points, atlas), built by `packaging/build_packs.py map <ST>`
(rules in `derived/packs/pack-spec.json` → `map_sets.state`). A state map set is self-contained: it is the base map
plus every row inside the state's box, so it **replaces** the library's map set and never merges with it.
One state per library for now; choosing another state replaces the first.

Who owns what: **map-packages** builds, zips, uploads and lists the state maps; **setup-backend** downloads and
installs them; **setup-ui** shows the choice. The app's Map, Ask and Videos screens are unchanged, and read the new
map set the same way as the base one.

Manifest: a new top-level list, alongside `packs` and `models`. A fetched manifest merges by `id`, as packs do.

```json
"maps": [
  {"id": "CA", "title": "California", "kind": "state", "installed_bytes": 5000000000, "available": true,
   "includes": "Local streets, buildings, terrain and crops, plus water, help and resource points",
   "files": [{"name": "Lastbook-Map-CA.zip", "url": "https://downloads.lastbook.org/maps/Lastbook-Map-CA.zip",
              "size": 0, "sha256": "…", "kind": "zip"}]}
]
```

The zip unpacks to the same layout as a pack, holding only the map set's files: `derived/geo/*.sqlite`,
`derived/geo/national-status.json`, `derived/geo/socal-cells.json` and `library-data/geography/resources/atlas.sqlite`,
plus `map-set.json` naming the state. Nothing else is allowed in it. Unpacking guards are the same as for packs.

Setup: `POST /api/setup/start {pack, ai, map: "CA" | null}`. A map is a third job item, `kind: "map"`. The space
needed counts the map's download plus installed bytes. Installing it builds the new map set in
`LastbookData.setup/unpack/` and switches the listed files in one step, keeping the old map set until the library
opens with the new one. `state.current.map` reports the installed set (`base`, `us` or a state code, from
`map-set.json`). The manage screen lets a person add or change the state later. A pack that already contains a
detailed map (Medium has California; All has the whole US) shows that, and choosing a state on Medium replaces
its California map. All needs no state map. The state is chosen by the person; Lastbook does not detect location.

Country maps work exactly the same way, for a European country instead of a US state: the entry's `kind` is
`"country"`, its `id` is `eu-<iso>` (`eu-at` for Austria — never a bare two-letter code, so a country can never take
a state's id, as Germany would take Delaware's), and the zip is `Lastbook-Map-eu-at.zip` with the same contents and
the same rules. `packaging/build_packs.py map eu-at` builds it with the state cutting rules
(`derived/packs/pack-spec.json` → `map_sets.country`); only the box differs: a state's box comes from its ZIP
centres, a country's is the bounding box of Geofabrik's OpenStreetMap extract polygon for that country
(`packaging/europe-countries.json`), with the same 0.15 degree margin. What a country set holds is bounded by the
source layers' own coverage, exactly as a state's is. Setup needs no change: a map is chosen by its manifest `id`
either way. Address indexes stay US-only — their source (Overture addresses) is split by US state, so there is no
country entry to publish until that source is keyed by country.

Publishing a country set is opt-in. `packaging/state_maps.py --order` and `--worker` only include countries listed in
`packaging/country-publication.json`; that file is empty because the detailed source layers are still US-only. Before
zipping or uploading a listed country, the publisher checks the built set and fails if it holds no detailed roads,
help or water points, terrain, buildings, crops or OpenStreetMap place names inside the country box. A base-only build
therefore never reaches the manifest as an available country map. A country is added to that file only after a source
snapshot for it passes the check and setup can show the set's attribution. This PR says nothing about the completeness
of any country's map: with today's sources, a country build is the low-zoom base only.

Built by setup-backend (2026-11-12):
- Job item `kind: "map"` comes after the pack and before the model: `waiting`, `downloading`, `unpacking` (bytes of
  `installed_bytes`), `done`. With a pack, the state's files replace the pack's own map set inside the staged library.
  On its own (manage mode, `start {map: "CA"}` with no `pack`), the job ends `done` with the swap staged. The swap happens
  when the app restarts after `finish`: the old map set's files, with their SQLite `-wal`/`-shm` files, move to
  `LastbookData.setup/map-old/`, the new ones move in, and if the result does not check out (every file `map-set.json`
  lists, at its size) the old set is put back. `map-old/` is deleted once the library has opened.
- A map zip may hold only the map-set files listed above (it fails with `unpack` otherwise), and `map-set.json`'s `name`
  must equal the entry's `id`. Its files may sit under a top `LastbookData/` folder or at the zip's top (as the published
  state zips do); a pack zip still needs the folder.
- `state.current.map` is `map-set.json`'s `name`, else `pack.json`'s `map`, else `"base"`. Asking for the state already
  there answers `{code: "nothing"}`. In first-run setup, a map needs a pack in the same `start`.
- `state.recommend.map_need_bytes` = `{map id: download + installed bytes}`; `start`'s space check adds the map.
  `state.manifest.maps` lists the entries (without URLs).
- `found[]` also lists a `Lastbook-Map-*.zip` already on this computer (`kind: "map"`); `use` on it starts the map job
  in manage mode, or is used by the next `start` in first-run setup.
- Repair checks the pack's own map set (All's `us`) with the pack's other files and fetches it with the pack. A state
  map installed apart (its name is not `pack.json`'s `map`, or `pack-manifest.tsv` lists no `map-set.json`) is checked against its
  `map-set.json` instead: one that checks out is kept when the pack is fetched again for other files; one that does
  not is repaired with its own `map` item, or Repair answers `{code: "unavailable"}` when that map is not offered.
