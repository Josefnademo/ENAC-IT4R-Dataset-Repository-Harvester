# ENAC-IT4R Dataset & Repository Harvester (v1.5)

A local, dependency-light web application for **EPFL ENAC-IT4R** that harvests research
outputs published in **Infoscience (DSpace 7 REST API)** and the **Zenodo EPFL community**,
normalizes them into one record model, persists them in SQLite plus a JSON mirror, and
explores them through an EPFL-styled dashboard.

Nothing is scraped from HTML and no credentials are used: both repositories are queried
through their public APIs, anonymously, within their published rate limits.

---

## 1. What it does

| Capability | Detail |
|---|---|
| Resource typing | Datasets (incl. every COAR dataset subtype), Software, Models & Workflows, Other research products, or All Works |
| Date windows | Free `From` / `To` date pickers plus 7 / 14 / 30 / 90 days, 1 year and All-time presets |
| Cross-repository merge | Records sharing a normalized DOI are merged; Infoscience wins on labs/PIs/hierarchy, Zenodo wins on file inventory and code links |
| Delta sync | Every upsert is classified `new`, `updated` or `unchanged` (tracked-field comparison in SQLite) |
| Metadata richness | Authors, ORCIDs, EPFL lab acronyms, parent organizations (ENAC / SB / EPFL), unit managers (PIs), funding, keywords, licence, access rights, DOI, file count/size/extensions, linked code repository |
| Local-first UI | The browser only reads SQLite; upstream calls happen exclusively on **Run Scraper (Live API)** and on an explicit **Preview Data** request |
| In-browser data preview | **Preview Data** on a card opens an overlay that renders CSV/TSV tables, JSON, GeoJSON maps, text/YAML/Markdown, images and the file list of remote ZIP archives — never downloading a multi-GB archive |
| Live console | Every scraper log line is streamed into the dashboard by cursor-based polling |
| Exports | CSV and JSON of the currently filtered result set |

---

## 2. Directory layout

```
.
├─ run.py                          CLI entry point (dashboard or headless harvest)
├─ requirements.txt                requests only
├─ backend/
│  ├─ config.py                    verified API constraints, resource taxonomy, paths
│  ├─ models.py                    HarvestRecord + cleaning / typing / file-summary helpers
│  ├─ preview.py                   bounded, SSRF-guarded file preview (incl. remote ZIP)
│  ├─ scrapers/zenodo.py           Zenodo (InvenioRDM) client
│  ├─ scrapers/infoscience.py      Infoscience (DSpace 7) client + WAF handling
│  ├─ harvester.py                 parallel fetch, DOI merge, persistence
│  ├─ storage.py                   SQLite + atomic JSON mirror
│  ├─ logger.py                    ring-buffer logger feeding the console
│  └─ server.py                    threading HTTP server, static + JSON API
├─ frontend/                       index.html, styles.css, preview.js, app.js (no build step)
├─ docs/
│  ├─ TECHNICAL_ARCHITECTURE.md    end-to-end design and verified API behaviour
│  └─ recon/                       Phase 1 probe artefacts (evidence for the taxonomy)
└─ data/                           harvester.db + dataset_cache.json (generated)
```

---

## 3. Prerequisites

- Python 3.9 or newer (`python --version`)
- Outbound HTTPS access to `zenodo.org` and `infoscience.epfl.ch`
- `requests` (the only third-party dependency)

```bash
pip install -r requirements.txt
```

---

## 4. Run it

### Dashboard

```bash
python run.py                # http://127.0.0.1:8080/
python run.py --port 9000 --open
```

The first launch shows an empty state until a harvest is executed from the UI
(**Run Scraper (Live API)**). Reloading the page never calls the upstream APIs.

### Headless harvest (CI / cron / quick check)

```bash
python run.py --harvest --days 7
python run.py --harvest --days 365 --type dataset
python run.py --harvest --from 2026-01-01 --to 2026-03-31 --type software
```

`--type` accepts `dataset`, `software`, `model_workflow`, `other`, `all`
(default `dataset`). The command prints a summary and exits; the dashboard picks the new
records up on the next refresh.

---

## 5. HTTP API

| Endpoint | Purpose |
|---|---|
| `GET /api/data` | Snapshot from SQLite: `items`, `last_run`, `runs`, `filters`, `default_filter` |
| `GET /api/preview?url=<encoded_file_url>&name=<filename>[&inner=<path_inside_zip>]` | Bounded preview of one record file, or of one file stored inside a remote ZIP (400 for any host outside `zenodo.org` / `infoscience.epfl.ch`, 404 for an unknown archive member, 413 when a member exceeds the in-memory budget, 502 when the repository cannot be reached) |
| `GET /api/record-files?id=<canonical_id>` | Resolves the file inventory of a record stored without preview `entries`, caches it in SQLite + JSON mirror, and returns `{"canonical_id", "file_summary"}` (404 for a non-Zenodo record, 502 when Zenodo cannot be reached) |
| `GET /api/harvest?from=YYYY-MM-DD&to=YYYY-MM-DD&type=dataset` | Runs one live harvest (409 when a harvest is already running, 400 on bad input) |
| `GET /api/logs?since=<cursor>` | Incremental console entries plus the new cursor |
| `GET /api/status` | Version, record count, last run, database and mirror paths |
| `GET /api/filters` | Resource taxonomy for the UI |

---

## 6. In-browser data preview ("surcouche")

Every card whose record reports files carries a **Preview Data** badge in its footer.
It opens a modal overlay that inspects one file at a time without downloading the
record: the overlay has a sticky header with the record title, a file selector (when
the record lists several files), the file-size badge, a direct download link, and it
closes with the button, `Escape` or a click on the backdrop. The whole overlay lives in
`frontend/preview.js` (loaded before `app.js`, still plain ES2017 with no build step),
so `app.js` only keeps the dashboard: cards, filters, exports and polling.

`GET /api/preview` only reads a hard byte budget per format:

| File | Preview |
|---|---|
| `.csv`, `.tsv` | First **256 KB**, decoded UTF-8 (latin-1 fallback), delimiter auto-detected, header row + at most **100 rows**. The overlay renders a sticky-header table with row numbers, a client-side row filter and a truncation banner, plus a **Quick Chart** view (inline SVG, no library) with X/Y column pickers over the first 100 rows |
| `.geojson`, `.json` | First **1 MB**. GeoJSON (`FeatureCollection`, `Feature`, `GeometryCollection`, standard geometries) is rendered on a Leaflet map (loaded lazily from unpkg only when needed, with a formatted-JSON fallback when offline); other JSON is pretty-printed |
| `.txt`, `.md`, `.yml`, `.yaml` | First **256 KB**. `.md` files are formatted by default (headings, bold, inline/fenced code, bullet lists, clickable links — every character escaped before formatting) with a **Rendered \| Raw** toggle; other text is a monospace viewer |
| `.zip` | The **central directory only**, read through HTTP `Range` requests (HEAD/Content-Range probe + seekable remote file wrapper), so a 17 GB archive lists its entries with one 1 MB range request. Up to 200 entries: path, extension badge, uncompressed size, and a **Preview** pill on CSV/TSV/JSON/GeoJSON/text/Markdown/YAML/image members. Clicking one reads only that member (≤ 10 MB uncompressed, ≤ 12 MB compressed, ≤ 4 MB for inlined images) and shows it under a sticky **← Back to `<archive>` contents** breadcrumb; archives whose directory would exceed a 12 MB metadata budget are refused with HTTP 413 |
| `.png`, `.jpg`, `.jpeg`, `.webp`, `.svg` | Hot-linked full-resolution image (images extracted from a ZIP are inlined as a base64 data URI instead) |
| `.nc`, `.zarr`, `.gpkg`, `.parquet`, `.h5`, … | Explicit "binary scientific format" notice with the direct download link |

**Session cache.** `frontend/preview.js` keeps a bounded `Map` of the last 40 preview
payloads keyed by `url + "::" + inner_path`. Opening a record, switching files,
leaving a ZIP member with the breadcrumb or closing and reopening the modal therefore
re-renders instantly with **zero network requests**; the cache lives only in the page.

**Security.** The endpoint is a strict SSRF allow-list: only `https://` URLs whose
hostname is `zenodo.org` or `infoscience.epfl.ch` are fetched; anything else is
rejected with **HTTP 400** before a socket is opened.

**File inventory.** `file_summary` keeps its original `count`, `total_bytes`,
`total_human` and `extensions` fields and now also stores `entries` (first **25**
files per record) with `key`, `size`, `size_human`, `ext` and `url`, where `url` is
`files[].links.self` (or the rebuilt
`https://zenodo.org/api/records/<record_id>/files/<key>/content` fallback).
`file_summary` is a tracked field, so a changed upload list marks the row `updated`.
Records harvested before this feature only know the file count: the overlay calls
`GET /api/record-files?id=<canonical_id>`, which fetches
`https://zenodo.org/api/records/<record_id>` once, caches the rebuilt inventory in
SQLite and `data/dataset_cache.json`, and returns it — the file selector is then
populated without re-running a harvest, and later clicks read it straight from
SQLite.

---

## 7. Persistence

| Store | Path | Role |
|---|---|---|
| SQLite | `data/harvester.db` | System of record: one row per canonical id, `first_seen` / `last_seen` / `sync_status`. `harvest_runs` keeps the newest 25 executions and coalesces repeated zero-change runs |
| JSON mirror | `data/dataset_cache.json` | Human-readable snapshot of the last harvest (atomic replace) |

Both paths can be overridden with `HARVESTER_DB` and `HARVESTER_JSON_MIRROR`.
Deleting `data/` resets the application to an empty state without touching configuration;
`data/` is git-ignored, so a checkout never ships a local database or cache file.

---

## 8. Verified upstream constraints

The numbers below were measured against the live APIs during Phase 1 and are enforced in
`backend/config.py`; section 4 of `docs/TECHNICAL_ARCHITECTURE.md` explains each one.

| | Zenodo (InvenioRDM) | Infoscience (DSpace 7) |
|---|---|---|
| Page index | 1-based (`page=0` → HTTP 400) | 0-based |
| Page size | **25** anonymous hard cap | **100** (`size>100` silently skips records) |
| Pacing | 2 s between pages; pause 15 s when `X-RateLimit-Remaining <= 3` | 6.5 s between calls; fail fast on HTTP 429 (`Retry-After: 900`) |
| Safety valve | max 60 pages per run | max 3 pages per run, AWS WAF challenge replayed through `curl.exe` |
