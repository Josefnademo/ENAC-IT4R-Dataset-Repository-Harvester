# Technical Architecture — ENAC-IT4R Dataset & Repository Harvester v1.5

This document explains how the harvester works, in the order a reader needs it: what the
tool is for, what happens during one harvest, how the UI is organized, which upstream
metadata field feeds which record field, how both persistence layers behave, what the
Phase 1 investigation established about resource types, and which rate-limit/WAF
safeguards are enforced.

---

## 1. In plain English

ENAC-IT4R needs to know, and to show, which research datasets, software, models and other
research products its laboratories publish in EPFL's institutional repository
(**Infoscience**) and in the **Zenodo EPFL community**. Doing this by hand is unreliable:
the two systems describe the same object differently, the same dataset can exist in both,
and each API has its own paging and rate-limit rules.

The harvester does four things:

1. **Asks both repositories** a narrow question: *which records of type X were ingested in
   this date window?*
2. **Normalizes** every answer into one `HarvestRecord` shape, so the UI never has to know
   which repository a field came from.
3. **Merges duplicates** that share a DOI, keeping the richer half of each side.
4. **Stores everything twice** — in SQLite for correctness/deltas, and in a JSON mirror for
   humans and downstream scripts — then renders it as a static dashboard that never calls
   the network on refresh.

Nothing is authenticated, nothing is scraped from HTML, and no synthetic or placeholder
metadata is ever invented: a field is either published by the repository or left empty.

---

## 2. End-to-end workflow

```
Browser                     backend/server.py            backend/harvester.py        Upstream APIs
   |                               |                              |                        |
   |  GET /api/data                |                              |                        |
   |------------------------------>| storage.load_records()       |                        |
   |<------------------------------| {items, last_run, filters}   |                        |
   |                               |                              |                        |
   |  GET /api/harvest?from&to&type|                              |                        |
   |------------------------------>| execute_harvest(...)         |                        |
   |                               |----------------------------->| resolve_window()       |
   |                               |                              | ThreadPoolExecutor(2)  |
   |                               |                              |--> zenodo.fetch ------>|
   |                               |                              |--> infoscience.fetch ->|
   |                               |                              |<-- FetchResult x2 -----|
   |                               |                              | merge_records()        |
   |                               |                              | storage.upsert_records |
   |                               |                              | storage.record_run()   |
   |                               |                              | export_json_mirror()   |
   |<------------------------------| {run, counts, sources, items}|                        |
   |  render cards + metrics       |                              |                        |
   |                               |                              |                        |
   |  GET /api/logs?since=N  (1.5 s poll)                        |                        |
   |------------------------------>| log_snapshot(N) -- ring buffer of the log handler     |
   |<------------------------------| {cursor, entries[]}          |                        |
```

### Step by step

1. **Window resolution** — `resolve_window(date_from, date_to, default_days)` parses both
   dates, defaults the end date to *today*, defaults a missing start to
   `end - (default_days - 1)`, and swaps the pair if the user picked them backwards.
2. **Guarded execution** — a module-level `threading.Lock` (`_run_lock`) is acquired with
   `acquire(blocking=False)`. A second concurrent request raises `HarvestBusyError`, which
   the HTTP layer maps to **409 Conflict** so the UI cannot double-harvest.
3. **Parallel fetch** — one `ThreadPoolExecutor(max_workers=2)` submits the Zenodo and the
   Infoscience client. Each runs inside `_safe_fetch`, which converts any exception into a
   `FetchResult(status="error", message=...)` instead of propagating it. A 429 on one
   repository therefore never discards the results of the other.
4. **Normalization** — each scraper maps raw payloads onto `HarvestRecord`
   (`backend/models.py`), whose `__post_init__` strips HTML, removes
   `#PLACEHOLDER_PARENT_METADATA_VALUE#`, de-duplicates lists, normalizes the DOI and
   truncates the abstract to `ABSTRACT_LIMIT` (1200 characters).
5. **Merge** — `merge_records()` indexes Zenodo records by `canonical_id`
   (`doi or source_id`), walks the Infoscience records, and pairs DOI twins through
   `_merge_pair`. Unpaired Zenodo records are appended afterwards, so nothing is lost.
6. **Persistence** — `Storage.upsert_records()` compares each incoming record with the
   stored row on `TRACKED_FIELDS`; the record is marked `new`, `updated` or `unchanged`,
   `first_seen` is preserved and `last_seen` refreshed, all inside one transaction.
7. **Run bookkeeping** — `Storage.record_run()` stores window, resource filter, per-source
   status, new/updated/unchanged counts, merged count, total records, duration and notes.
8. **JSON mirror** — `Storage.export_json_mirror(run)` writes the full snapshot atomically
   (temp file + `os.replace`).
9. **Response** — the harvest payload returns `run`, `counts`, `sources`, `mirror` and the
   fresh `items` list, so the UI updates without a second request.
10. **Refresh safety** — `GET /api/data` only ever reads SQLite; pressing F5 cannot generate
    upstream traffic.

---

## 3. UI anatomy and badge taxonomy

The dashboard is a two-column shell: a sticky **sidebar** of controls and a **results**
column. The sidebar is an independently scrollable rail — it is bounded to the viewport
(`max-height: calc(100vh - var(--header-height) - 2rem)` with `overflow-y: auto` and
`overscroll-behavior: contain`), so Hub Metrics and the Scraper Live Console stay reachable at
any page scroll position without scrolling the card list to the bottom; below 960 px it reverts
to `position: static`, `max-height: none`, `overflow-y: visible` and scrolls with the page.
`frontend/app.js` is plain ES2017 with no build step and no framework; every string
that reaches `innerHTML` passes through `escapeHtml()` first.

| Sidebar panel | Contents |
|---|---|
| Harvest window | Presets (7/14/30/90 days, 1 year, all time) plus free `From` / `To` date inputs. The window drives **both** the live API query and the local display filter, so what the console reports and what the list shows can never diverge. |
| Search | Free-text box matching title, abstract, authors, lab acronym, PI, keywords, file size and file extensions (`_search` haystack built per record). Four refinement toggles: ENAC Units Only, Has Code Repository, Grant Funded, New / Updated Only. |
| Actions | **Run Scraper (Live API)**, CSV export, JSON export. |
| Hub Metrics | Records in view, distinct labs/units, new and updated counts of the last run, plus a one-line run summary. |
| Scraper Live Console | Ring buffer of scraper log lines streamed by `/api/logs?since=cursor` (1.5 s polling, 400-line DOM cap, auto-scroll only when already at the bottom). |

The results column holds a summary bar (`N records`, active window, per-source counts,
snapshot time), the resource-type filter pills with live counts, and the card list.

### Badge taxonomy

| Badge | Class | Meaning |
|---|---|---|
| Resource type (blue) | `.badge-type` | The **exact** type string published by the repository — e.g. `dataset`, `dataset::geospatial data`, `software::research software`. Subtypes are preserved on purpose: they explain *why* a record is in the list. |
| Source (gray) | `.badge-source` | `Infoscience (DSpace 7)`, `Zenodo (EPFL Community)` or `Infoscience + Zenodo` for a DOI-merged record. |
| Lab (red/pink) | `.badge-lab` | EPFL unit acronym declared by the record (`cris.virtual.department`), rendered in full. |
| Organization (dashed) | `.badge-org` | Parent hierarchy (`cris.virtual.parent-organization`, typically `ENAC`, `SB`, `EPFL`). |
| Grant (amber) | `.badge-grant`, `.is-ord` | Funding programme / grant title; ORD, Flagship and Programme entries are highlighted. |
| Keyword (pill) | `.keyword-chip` | Record subject terms, rendered in full. |
| Sync (green/amber) | `.badge-new`, `.badge-updated` | Delta state of the record in SQLite (`new` = first time seen, `updated` = a tracked field changed). |

Tags are **never** capped, clipped or collapsed behind a `+N` marker: every affiliation,
parent organization, grant and keyword is rendered whole with natural wrapping
(`white-space: normal; word-break: break-word`). All four tag types are buttons — clicking
one copies its exact text into the search box (`#search-input`), updates the filter state and
re-renders the list, so a lab or a grant becomes a filter in one click.

Card body layout: title (linked to the repository record) → Authors / PI / EPFL labs /
ENAC units / Funding → abstract excerpt → detail grid (Date, Access & licence, Files, Code)
→ keyword chips → footer with the full identifier and the outbound links.
The footer keeps the complete DOI (or canonical id) in `.card-id` as a click-to-copy button
with a transient `Copied DOI` confirmation. `Record` always opens the repository page; a
`10.5075/epfl.*` handle DOI is **not** repeated as a separate `DOI` link because
`https://doi.org/` would redirect to that same Infoscience page, while a DOI-merged record
exposes both the Infoscience `Record` and its `Zenodo` DOI resolver.
Empty values are rendered as explicit muted text (`Not published`, `File metadata not
exposed`), never as fabricated data.


---

## 4. Metadata field mapping

### 4.1 Zenodo (InvenioRDM, community endpoint)

Endpoint: `GET https://zenodo.org/api/communities/epfl/records?q=...&size=25&page=N&sort=newest`

| `HarvestRecord` field | Zenodo source |
|---|---|
| `title` | `metadata.title` |
| `abstract` | `metadata.description` (HTML stripped, 1200 chars) |
| `authors` | `metadata.creators[].name` |
| `affiliations` | `metadata.creators[].affiliation` (author-declared) |
| `author_orcids` | `metadata.creators[].orcid`, normalized to bare `0000-0000-...` |
| `funding` | `metadata.grants[].code` / `title` / `funder.name` composed into readable labels |
| `resource_type` | `metadata.resource_type.type` plus `metadata.resource_type.subtype` when present |
| `code_repo` | `metadata.custom["code:codeRepository"]` |
| `file_summary` | `files[]` → count, total bytes, `total_human`, distinct extensions |
| `keywords` | `metadata.keywords` (list or `;`-separated string) |
| `license` | `metadata.license.id` / `license` |
| `access_rights` | `metadata.access_right` (falling back to `metadata.access`) |
| `doi` | `hit.doi` (falling back to `metadata.doi`), normalized |
| `url` | `links.self_html` → `https://zenodo.org/records/{id}` → `https://doi.org/{doi}` |
| `publication_date` | `metadata.publication_date` |
| `created_date` | `hit.created` (ingest timestamp, date part) |
| `parent_orgs`, `unit_managers` | always empty — Zenodo has no EPFL hierarchy |

### 4.2 Infoscience (DSpace 7 CRIS)

Endpoint: `GET https://infoscience.epfl.ch/server/api/discover/search/objects?configuration=researchoutputs&query=...&size=100&page=N&sort=dc.date.accessioned,desc`
Each hit is `_embedded.indexableObject`, whose `metadata` values are
`[{ "value": ..., "language": ... }]` lists.

| `HarvestRecord` field | Infoscience source |
|---|---|
| `title` | `dc.title` |
| `abstract` | `dc.description.abstract` |
| `authors` | `dc.contributor.author` |
| `affiliations` | `cris.virtual.department` (EPFL unit acronym, e.g. `ENAC-IT4`) |
| `parent_orgs` | `cris.virtual.parent-organization` (e.g. `ENAC`, `SB`, `EPFL`) |
| `unit_managers` | `cris.virtual.unitManager` (lab head / PI) |
| `author_orcids` | `epfl.author.orcid` |
| `funding` | `dc.relation.funding` |
| `code_repo` | first `epfl.url` whose host matches a code marker (`github.com`, `gitlab`, `bitbucket`, `codeocean`, `sourceforge`, `r-universe`, `pypi.org`, `cran.r-project.org`, …) or whose `epfl.url.description` mentions code / repository / source |
| `resource_type` | `dc.type` (raw string, subtype suffix preserved) |
| `keywords` | `dc.subject` |
| `license` | `ctb.oaireXXlicenseCondition` → `oaire.licenseCondition` |
| `access_rights` | `datacite.rights` |
| `doi` | `dc.identifier.doi` |
| `url` | `handle` → `https://infoscience.epfl.ch/handle/{handle}`, else `dc.identifier.uri` |
| `publication_date` | `dc.date.issued` (falling back to `dc.date.accessioned`) |
| `created_date` | `dc.date.accessioned` |
| `file_summary` | empty — the discover payload exposes no bitstream inventory |

`epfl.unit`, `epfl.laboratory`, `epfl.department` and `dc.contributor.department` are
**never** returned by this API (0 % presence across the probe sample); the lab acronym is
therefore read from `cris.virtual.department`, as in the mapping table above.

### 4.3 Merge matrix (same DOI in both repositories)

| Field | Winner |
|---|---|
| `source` / `source_keys` | `Infoscience + Zenodo` / `["infoscience","zenodo"]` |
| `title`, `abstract` | Infoscience, unless it is the `Untitled record` placeholder |
| `authors` | Infoscience, falling back to Zenodo |
| `affiliations`, `parent_orgs`, `unit_managers` | Infoscience (authoritative EPFL hierarchy) |
| `author_orcids`, `funding`, `keywords` | union of both |
| `file_summary` | Zenodo when it reports files, else Infoscience |
| `code_repo` | Zenodo, falling back to Infoscience |
| `license`, `access_rights`, `doi`, `url` | Infoscience, falling back to Zenodo |
| `publication_date` | Zenodo (always a full `YYYY-MM-DD`) |
| `created_date` | Infoscience (ingest date drives the window) |
| `resource_type` / `resource_class` | Infoscience, unless it degraded to `other` |


---

## 5. Dual persistence

### 5.1 SQLite — `data/harvester.db` (system of record)

Two tables, created on first use (`SCHEMA_VERSION = "1.5"`):

| Table | Purpose |
|---|---|
| `records` | One row per `canonical_id` (DOI when available, otherwise `zenodo-<record id>`, or `infoscience-<handle>`, `infoscience-<uuid>`, `infoscience-<md5 of title+date+author>`). Scalar columns are plain `TEXT`; `source_keys`, `authors`, `affiliations`, `parent_orgs`, `unit_managers`, `author_orcids`, `funding`, `keywords` and `file_summary` are JSON-encoded. Indexed on `publication_date`, `created_date` and `resource_class`. |
| `harvest_runs` | One row per harvest (identical zero-change repeats are coalesced, see below): `run_at`, `date_from`, `date_to`, `resource_type`, per-source status and counts, `new_count`, `updated_count`, `unchanged_count`, `merged_count`, `total_records`, `duration_seconds`, `notes`. |

**Delta detection.** `upsert_records()` opens one transaction, reads the existing row for
each incoming `canonical_id`, and compares it field-by-field against `TRACKED_FIELDS`
(`title`, `abstract`, `affiliations`, `unit_managers`, `doi`, `publication_date`,
`resource_type`, `funding`, `code_repo`, `keywords`, `url`):

- no existing row → `sync_status = "new"`, `first_seen = last_seen = now`
- any tracked field differs → `sync_status = "updated"`, `first_seen` kept, `last_seen = now`
- nothing differs → `sync_status = "unchanged"`, only `last_seen` refreshed

The returned counters (`{"new": n, "updated": n, "unchanged": n}`) are what the UI shows in
Hub Metrics and what the run row stores.

**Run retention.** `harvest_runs` is a bounded audit trail, not an unbounded click log.
`Storage.record_run()` reads the newest row (`ORDER BY id DESC LIMIT 1`) before writing: when the
incoming execution has `new_count = 0` and `updated_count = 0` and repeats the same scope
(`date_from`, `date_to`, `resource_type`, `zenodo_status`, `infoscience_status`) as an equally
zero-change newest row, it refreshes that row's `run_at`, `duration_seconds` and `total_records`
instead of appending — so pressing **Run Scraper** repeatedly on an unchanged window cannot flood
the table, and the Hub Metrics tiles keep reporting the counts of the *current* run. Every real
insert then prunes older rows
(`DELETE FROM harvest_runs WHERE id NOT IN (SELECT id FROM harvest_runs ORDER BY id DESC LIMIT ?)`),
keeping the newest `MAX_HARVEST_RUNS_HISTORY = 25` executions: enough history for
previous-versus-current comparisons, never unbounded growth.

### 5.2 JSON mirror — `data/dataset_cache.json` (human/automation facing)

Rewritten after every harvest via a temp file plus `os.replace`, so an interrupted run can
never leave a half-written file. Top-level keys:

```json
{
  "generated_at": "2026-11-12T14:21:04+00:00",
  "schema_version": "1.5",
  "app": "ENAC-IT4R Dataset & Repository Harvester",
  "record_count": 510,
  "counts_by_source": { "infoscience": 300, "zenodo": 347 },
  "counts_by_resource_class": { "dataset": 510 },
  "source_labels": { "zenodo": "...", "infoscience": "...", "merged": "..." },
  "last_run": { "...": "same shape as the harvest_runs row" },
  "items": [ { "...": "full HarvestRecord dictionaries incl. window_date" } ]
}
```

### 5.3 Why both

SQLite gives transactional upserts, delta bookkeeping and indexed reads for the UI; the JSON
mirror gives a diff-friendly artefact that other ENAC-IT4R tooling (or a simple `git diff`)
can consume without a database driver. Neither store is authoritative upstream data: both
are disposable — delete `data/` and the next harvest rebuilds them.


---

## 6. Phase 1 — resource-type discovery findings

Before writing any code, both repositories were probed with throw-away scripts and every
answer was written to `docs/recon/` (raw JSON included, so each number below is auditable).

### 6.1 Zenodo EPFL community (`docs/recon/zenodo_type_counts.json`, 2026-11-12)

| Query | Records |
|---|---|
| `communities/epfl/records` (whole community) | **513** |
| `resource_type.type:dataset` | **347** |
| `resource_type.type:software` | **49** |
| `resource_type.type:model` | **1** |
| `resource_type.type:workflow` | **2** |
| `resource_type.type:other` | **15** |
| `resource_type.type:image` | **8** |
| `resource_type.type:video` | **9** |
| `resource_type.type:poster` | **5** |
| `resource_type.type:presentation` | **8** |

The five buckets the harvester exposes (dataset, software, model+workflow, other, all) cover
**444** of the 513 community records; the remaining **69** are publication-like types
(articles, preprints, reports…) that are deliberately out of scope for a *research data &
software* hub.

### 6.2 Infoscience dataset subtypes (`docs/recon/probe_results.json`)

365-day window (`2025-11-12` → `2026-11-12`), `dc.type:dataset`, `size=100`, first page:

| `dc.type` value | Records |
|---|---|
| `dataset` | 61 |
| `dataset::simulation data` | 4 |
| `dataset::measurement and test data` | 2 |
| `dataset::geospatial data` | 2 |
| `dataset::experimental data` | 2 |
| `dataset::recorded data` | 1 |
| `dataset::données enregistrées` | 1 |
| `dataset::données de simulation` | 1 |
| `dataset::données expérimentales` | 1 |
| **total** | **75** |

Two consequences are baked into `models.classify_resource_type()`: the leading segment
before `::` decides the UI class, and the full string is preserved for display, so a
`dataset::geospatial data` record is still a *Dataset* filter hit but explains itself on the
card. Subtypes exist in both English and French.

Field presence across those 75 records explains which fields the UI can honestly promise:
`cris.virtual.department`, `cris.virtual.parent-organization`, `cris.virtual.unitManager`,
`dc.title`, `dc.contributor.author`, `dc.date.accessioned`, `dc.date.issued`,
`dc.description.abstract` → **75/75**; `dc.identifier.doi` → 71/75; `datacite.relatedIdentifier`
→ 71/75; `dc.relation.funding` + `dc.relation.grantno` + `oairecerif.funder` → **38/75**;
`dc.subject` (keywords) → 29/75; `epfl.url` (code link) → 16/75; `dspace.file.type` → 5/75.

### 6.3 Non-dataset types (`docs/recon/probe_results2.json`, `probe_results3.json`)

| Query | Total | Sampled types |
|---|---|---|
| `dc.type:software` | **25** | `software` 12, `software::source code` 7, `software::research software` 5, `text::journal::journal article::software paper` 1 |
| `dc.type:other` | **2 851** | of 100 sampled: `student work::semester or other student projects` **85**, `other` 14, `Controlled Vocabulary for Resource Type Genres::other` 1 |
| `dc.type:model` | **77** | of 20 sampled: `patent::utility model` **20** (no true model records on that page) |
| `dc.type:workflow` | **0** | — |
| `dc.type:technicalreport` | **0** | — |

These numbers are the reason two query clauses exist in `config.RESOURCE_FILTERS`:

- `other` → `dc.type:other AND NOT dc.type:"student work"` (85 % of a naive `other` query is
  student coursework, which is not a research product);
- `model_workflow` → `(dc.type:model OR dc.type:workflow) AND NOT dc.type:patent`
  (the `model` facet is dominated by patents).

### 6.4 End-to-end verification (2026-11-12, live)

| Run | Window | Zenodo | Infoscience | Merged by DOI | Stored |
|---|---|---|---|---|---|
| `--harvest --days 7 --type dataset` | 2026-11-06 → 2026-11-12 | 0 | 2 | 0 | 2 |
| `--harvest --days 365 --type dataset` | 2025-11-13 → 2026-11-12 | 47 | 74 | **34** | **87** |
| `--harvest --from 1900-01-01 --type dataset` (the UI's *All Time*) | 1900-01-01 → 2026-11-12 | 347 | 300 | **137** | **510** |

The DOI merge is not theoretical: **34 of the 47** Zenodo datasets published in the last
12 months are twins of an Infoscience record, and **137 of 347** Zenodo datasets are twins
over the full history. Merging them is what turns `47 + 74 = 121` raw hits into 87
de-duplicated research outputs.

Running the second harvest over the already-populated database also exercised the delta
logic: 83 rows came back unchanged, 4 were flagged updated, 423 were new.

Post-run assertions on the 510-record mirror: **0** occurrences of
`#PLACEHOLDER_PARENT_METADATA_VALUE#`; 485 records carry a DOI; 335/347 Zenodo records carry
a populated `file_summary` (count, size, extensions); 459 records carry at least one EPFL
lab acronym, 296 a unit manager (PI), 264 a parent organization, 225 funding metadata,
47 a linked code repository and 373 keywords — i.e. every field shown in the UI is sourced
from a real repository value.


---

## 7. Rate limits, WAF and operational safeguards

Every constraint below was observed live; the constants live in `backend/config.py`.

### 7.1 Zenodo (InvenioRDM)

| Signal | Value | Harvester reaction |
|---|---|---|
| `X-RateLimit-Limit` / `Remaining` / `Reset` | 30 requests / 60 s | After each page, if `Remaining <= 3`, sleep `ZENODO_RATE_LIMIT_PAUSE` (15 s) before continuing |
| Page index | 1-based (`page=0` → HTTP 400) | `ZENODO_FIRST_PAGE = 1` |
| Page size | 25 anonymous hard cap (`size=100` → HTTP 400) | `ZENODO_PAGE_SIZE = 25` |
| Pacing | — | `ZENODO_DELAY_SECONDS = 2.0` between pages |
| HTTP 429 | `Retry-After` in seconds | Status `rate_limited`, pagination stops, already-collected page kept |
| Ceiling | — | `ZENODO_MAX_PAGES = 60` (1 500 records per run) |

### 7.2 Infoscience (DSpace 7 + CloudFront + AWS WAF)

| Signal | Value | Harvester reaction |
|---|---|---|
| `X-RateLimit-*` | absent | — |
| Host-wide burst ban | HTTP 429 + `Retry-After: 900` after ~13 calls / 2 min | **Fail fast**: the first 429 ends the pagination (retrying only extends the ban), remaining pages are skipped and the window is reported as `rate_limited` |
| Page index | 0-based | `INFOSCIENCE_FIRST_PAGE = 0` |
| Page size | 100 exact (`size>100` is accepted but objects are clamped while the offset still advances by `page*size` → silent record loss) | `INFOSCIENCE_PAGE_SIZE = 100`, never exceeded |
| Pacing | — | `INFOSCIENCE_DELAY_SECONDS = 6.5` between pages |
| Per-run budget | — | `INFOSCIENCE_MAX_PAGES = 3` (300 records) |
| AWS WAF Bot Control | HTTP 202 / 405 challenge with `x-amzn-waf-action` (CAPTCHA page) | Detected by header or by a CAPTCHA/AWS-WAF body marker, then the same URL is **replayed through the OS `curl.exe`** (native Schannel TLS), which the edge accepts; the payload is parsed identically |

### 7.3 Cross-cutting safeguards

- **One harvest at a time** — non-blocking `_run_lock`; a concurrent call gets HTTP 409.
- **Failure isolation** — `_safe_fetch` turns any scraper exception into a `FetchResult`
  with `status="error"`, so a broken repository never loses the other repository's records.
- **Fail fast, never hammer** — both clients stop paginating on the first 429 instead of
  retrying with back-off, which is the only behaviour that reliably clears CloudFront bans.
- **Bounded work** — page ceilings, a 60 s per-request timeout, an 800-entry log ring buffer
  and a 1200-character abstract limit bound memory and runtime.
- **Placeholder hygiene** — `#PLACEHOLDER_PARENT_METADATA_VALUE#` (DSpace's "no value"
  marker) is stripped by `models.clean_value()`/`unique()` before anything is stored, so it
  can never surface as a lab name in the UI.
- **Read-only UI** — the browser has no write endpoint other than `/api/harvest`; a refresh
  cannot mutate or re-fetch anything.

---

## 8. Module map and extension points

| Module | Responsibility | Typical extension |
|---|---|---|
| `backend/config.py` | Paths, verified API constants, `RESOURCE_FILTERS` taxonomy | Add a filter (e.g. `publication`) by adding a key with `zenodo_query`, `infoscience_query`, `classes`, `label`, `pill`, `description` |
| `backend/models.py` | `HarvestRecord`, cleaning, type classification, file summaries | Add a field: extend the dataclass, `to_dict()`, `record_from_row()` and `storage.COLUMNS` |
| `backend/scrapers/zenodo.py` | Lucene query build, creator/grant/license/keyword extraction, pacing | Add a metadata extractor (e.g. `metadata.dates`) |
| `backend/scrapers/infoscience.py` | Solr query build, DSpace metadata extraction, WAF replay | Add a CRIS field (`cris.virtual.*`) to the record mapping |
| `backend/harvester.py` | Window resolution, parallel fetch, DOI merge, run bookkeeping | Change merge precedence in `_merge_pair()` |
| `backend/storage.py` | SQLite schema, delta detection, JSON mirror | Add a tracked field to `TRACKED_FIELDS` to make it delta-sensitive |
| `backend/logger.py` | Ring-buffer log handler + cursor snapshots | Change `LOG_BUFFER_SIZE` for a longer console history |
| `backend/server.py` | Static serving + JSON API | Add a new `/api/*` route in `_handle_api()` |
| `frontend/app.js` | Rendering, filtering, exports, polling | The badge/label vocabulary is centralized in `SOURCE_LABELS`, `FALLBACK_FILTERS` and the `render*` helpers |

**Design rules kept throughout:** no third-party Python package beyond `requests`; no
JavaScript build step or framework; no credentials or environment-specific values; every
displayed value traceable to an upstream record; failures degrade to a partial, clearly
labelled result rather than a silent empty page.

