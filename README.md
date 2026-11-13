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
| Local-first UI | The browser only reads SQLite; upstream calls happen exclusively on **Run Scraper (Live API)** |
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
│  ├─ models.py                    HarvestRecord + cleaning / typing helpers
│  ├─ scrapers/zenodo.py           Zenodo (InvenioRDM) client
│  ├─ scrapers/infoscience.py      Infoscience (DSpace 7) client + WAF handling
│  ├─ harvester.py                 parallel fetch, DOI merge, persistence
│  ├─ storage.py                   SQLite + atomic JSON mirror
│  ├─ logger.py                    ring-buffer logger feeding the console
│  └─ server.py                    threading HTTP server, static + JSON API
├─ frontend/                       index.html, styles.css, app.js (no build step)
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
| `GET /api/harvest?from=YYYY-MM-DD&to=YYYY-MM-DD&type=dataset` | Runs one live harvest (409 when a harvest is already running, 400 on bad input) |
| `GET /api/logs?since=<cursor>` | Incremental console entries plus the new cursor |
| `GET /api/status` | Version, record count, last run, database and mirror paths |
| `GET /api/filters` | Resource taxonomy for the UI |

---

## 6. Persistence

| Store | Path | Role |
|---|---|---|
| SQLite | `data/harvester.db` | System of record: one row per canonical id, `first_seen` / `last_seen` / `sync_status`. `harvest_runs` keeps the newest 25 executions and coalesces repeated zero-change runs |
| JSON mirror | `data/dataset_cache.json` | Human-readable snapshot of the last harvest (atomic replace) |

Both paths can be overridden with `HARVESTER_DB` and `HARVESTER_JSON_MIRROR`.
Deleting `data/` resets the application to an empty state without touching configuration;
`data/` is git-ignored, so a checkout never ships a local database or cache file.

---

## 7. Verified upstream constraints

The numbers below were measured against the live APIs during Phase 1 and are enforced in
`backend/config.py`; section 4 of `docs/TECHNICAL_ARCHITECTURE.md` explains each one.

| | Zenodo (InvenioRDM) | Infoscience (DSpace 7) |
|---|---|---|
| Page index | 1-based (`page=0` → HTTP 400) | 0-based |
| Page size | **25** anonymous hard cap | **100** (`size>100` silently skips records) |
| Pacing | 2 s between pages; pause 15 s when `X-RateLimit-Remaining <= 3` | 6.5 s between calls; fail fast on HTTP 429 (`Retry-After: 900`) |
| Safety valve | max 60 pages per run | max 3 pages per run, AWS WAF challenge replayed through `curl.exe` |
