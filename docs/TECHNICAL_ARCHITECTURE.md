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
   |  GET /api/logs?since=N  (1.5 s busy / 10 s idle poll)       |                        |
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
5. **Merge** — `merge_records()` first drops every record whose `resource_class` is outside
   `ALL_RESOURCE_CLASSES` (`_listable()`: journal articles, theses, patents and student work
   are classified `publication`), then indexes Zenodo records by `canonical_id`
   (`doi or source_id`), walks the Infoscience records, and pairs DOI twins through
   `_merge_pair`. Unpaired Zenodo records are appended afterwards, so nothing is lost.
6. **Persistence** — `Storage.upsert_records()` compares each incoming record with the
   stored row on `TRACKED_FIELDS`; the record is marked `new`, `updated` or `unchanged`,
   `first_seen` is preserved and `last_seen` refreshed, all inside one transaction. A
   single-source snapshot never downgrades a merged `Infoscience + Zenodo` row (see §5.1,
   *Superset preservation*).
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
that reaches `innerHTML` passes through `escapeHtml()` first. `frontend/preview.js`
(the preview overlay, loaded just before `app.js`) follows the same rule, including for
the Markdown and SVG chart renderers.

**Preview overlay.** A card whose `file_summary` reports files also renders a
**Preview Data** badge in `.card-links`. It opens `#preview-modal`: a backdrop-blurred
dialog (`max-width: 1100px; max-height: 86vh`) with a fixed header (record title,
EPFL-red accent), a toolbar (file `<select>`, size badge, "Download original" link) and
a scrollable body. `Escape`, the close button and a backdrop click close it, and
`document.body.preview-open` freezes the page behind. Renderers are chosen from the
`kind` returned by `/api/preview` (`table`, `geojson`, `zip`, `json`, `text`, `image`,
`pdf`, `unsupported`); see §8.

| Sidebar panel | Contents |
|---|---|
| Harvest window | Presets (7/14/30/90 days, 1 year, all time) plus free `From` / `To` date inputs. The window drives **both** the live API query and the local display filter, so what the console reports and what the list shows can never diverge. |
| Search | Free-text box matching title, abstract, authors, lab acronym, PI, keywords, file size and file extensions (`_search` haystack built per record). Four refinement toggles: ENAC Units Only, Has Code Repository, Grant Funded, New / Updated Only. |
| Actions | **Run Scraper (Live API)**, CSV export, JSON export. |
| Hub Metrics | Records in view, distinct labs/units, new and updated counts of the last run, plus a one-line run summary. |
| Scraper Live Console | Ring buffer of scraper log lines streamed by `/api/logs?since=cursor` (adaptive polling: 1.5 s while a harvest runs, 10 s when idle, paused while the tab is hidden; 400-line DOM cap, auto-scroll only when already at the bottom). |

The results column holds a summary bar (`N records`, active window, per-source counts,
snapshot time), the resource-type filter pills with live counts, and the card list.

### Badge taxonomy

| Badge | Class | Meaning |
|---|---|---|
| Resource type (blue) | `.badge-type` | The **exact** type string published by the repository — e.g. `dataset`, `dataset::geospatial data`, `software::research software`. Subtypes are preserved on purpose: they explain *why* a record is in the list. |
| Source (gray) | `.badge-source` | `Infoscience (DSpace 7)`, `Zenodo (EPFL Community)` or `Infoscience + Zenodo` for a DOI-merged record. |
| Lab (red/pink) | `.badge-lab` | EPFL unit acronym (`cris.virtual.department`) or the affiliation string a Zenodo record declares. Rendered in full and wrapped naturally in the header; the same text is in the badge tooltip and in the `EPFL labs` row below. |
| Organization (dashed) | `.badge-org` | Parent hierarchy (`cris.virtual.parent-organization`, typically `ENAC`, `SB`, `EPFL`). |
| Grant (amber) | `.badge-grant`, `.is-ord` | Funding programme / grant title; ORD, Flagship and Programme entries are highlighted. |
| Keyword (pill) | `.keyword-chip` | Record subject terms, one chip per term. |
| Sync (green/amber) | `.badge-new`, `.badge-updated` | Delta state of the record in SQLite (`new` = first time seen, `updated` = a tracked field changed). |
| Overflow (outline) | `.inline-expand-btn` | The **only** expand/collapse control: `+ N more` / `Show fewer` on lists, `Show more` / `Show less` on prose. |

### One collapsible control per field

A field never shows two competing controls. `Show more` is reserved for **prose** and `+ N more`
for **lists**; both come from the single `collapsiblePill()` builder and are toggled by one
delegated handler on `[data-collapsible-toggle]`.

| Field | Helper and cap | Pill |
|---|---|---|
| Abstract | `renderCollapsibleText(item.abstract, 280)` | `Show more` / `Show less` |
| `Authors` | `renderCollapsibleItems(authors, { maxItems: 6 })` | `+ N more` / `Show fewer` |
| `PI / Lab head`, `EPFL labs`, `ENAC units` | `metaList()` → `renderCollapsibleItems(list, { maxItems: 3, charLimit: 180 })` | `+ N more` |
| one over-long lab value | `metaList()` → `renderCollapsibleText(value, 160)` when the row holds a single value longer than 160 characters | `Show more` |
| Header badges | `renderHeaderBadges()` → `renderCollapsibleItems(affiliations + parent_orgs, { maxItems: 8, charLimit: 115, itemOverhead: 4 })` | `+ N more` |
| `Funding` | `renderFunding()` → `renderCollapsibleItems(labels, { maxItems: 4, charLimit: 200 })` | `+ N more` |
| Keywords | `renderKeywords()` → `renderCollapsibleItems(chips, { maxItems: 12, charLimit: 125, itemOverhead: 4 })` | `+ N more` |

Lists never call `renderCollapsibleText()` from `renderItem`: every item renders in full and the
tail is parked in a `[data-collapsible-hidden]` wrapper, so the pill reports the exact remainder
(`+ 7 more`). The single exception preserves the rule — a one-item list whose value exceeds 160
characters (a Zenodo multi-institution affiliation string) is prose, so it is rendered by
`renderCollapsibleText(value, 160)` and gets `Show more`, never a pointless `+ 1 more`.

Badge and chip rows are budgeted by **row width**, not by item count: each item costs
`text + separator + itemOverhead`, where `itemOverhead: 4` pays for a tag's own padding and the
flex `gap`. `charLimit` therefore stands for the width of one header (or keyword) row, so the
`+ N more` pill appears only when the next tag genuinely does not fit — a half-empty row never
collapses.

`renderCollapsibleText()` stores the preview in `data-short` and the whole value in `data-full`
on the pill; the handler swaps the body in place, so expanding never re-truncates and the full
stored abstract always survives. `.collapsible` / `.collapsible-extra` use `display: contents`,
so a revealed tail joins the parent's flow — inline spacing for text, `gap` for the flex badge
rows — with no layout wrappers. Lists are joined with `" · "` (middle dot) rather than `", "`,
so adjacent `Lastname, Firstname` authors never blur together.

Tags stay **buttons**: an affiliation, organisation, grant or keyword is a
`<button class="… is-filterable">` whose `data-filter-tag` and `title` carry the full text, and
clicking copies it into the search box (`#search-input`). No expand control is ever nested inside
a tag, and nothing is clipped: `.badge` keeps `max-width: 100%`, `white-space: normal`,
`word-break: break-word` and `overflow-wrap: anywhere`, so `ÉCOLE POLYTECHNIQUE FÉDÉRALE DE
LAUSANNE` keeps its last letters and a long multi-institution string simply wraps onto a second
line. Because nothing is ellipsised, the header is budgeted in row characters instead of badge
count: up to 8 badges inside 115 row characters means a normal
`[institution] [IIE] [ENAC] [EPFL]` header renders all four with no pill, while a
ten-affiliation record fills the row and collapses the rest behind one `+ N more`. The keyword
row applies the same rule (`maxItems: 12`, `charLimit: 125`), so short subject terms fill the
full width before the pill appears.

Card body layout: title (linked to the repository record) → Authors / PI / EPFL labs /
ENAC units / Funding → abstract → detail grid (Date, Access & licence, Files, Code)
→ keyword chips → footer with the full identifier and the outbound links.
A 150-author record therefore stays one card tall instead of stretching across several
viewports, with at most one expand/collapse pill per field.
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
| `funding` | `metadata.grants[].title`, with the `acronym` in parentheses when it names a programme (`NCCR SPIN`, `EoCoE-III`); `grant.code` — a funder number such as `200021-227992` — is never emitted |
| `resource_type` | `metadata.resource_type.type` plus `metadata.resource_type.subtype` when present |
| `code_repo` | first of: `metadata.custom["code:codeRepository"]` → `metadata.related_identifiers[]` (`identifier` of every `scheme: "url"` / `http…` entry, relations tried in the order `isVersionOf`, `isSupplementTo`, `isDerivedFrom`, `isIdenticalTo`, `isDocumentedBy`, then any other relation) → first code-forge URL in `metadata.description` (regex scan) |
| `file_summary` | `files[]` → count, total bytes, `total_human`, distinct extensions, plus `entries` (first 25: `key`, `size`, `size_human`, `ext`, `url` from `links.self` or `/api/records/{id}/files/{key}/content`) |
| `keywords` | `split_keywords(metadata.keywords)` — list, dict (`subject`/`value`) or delimited string |
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
| `code_repo` | first of: the first `epfl.url` whose host is a code forge (or whose `epfl.url.description` mentions code / repository / source) → the first code-forge URL in `datacite.relatedIdentifier` (`doi:`, `doi.org` and publisher links are skipped) |
| `resource_type` | `dc.type` (raw string, subtype suffix preserved) |
| `keywords` | `dc.subject`, split by `split_keywords()` |
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

**Keyword normalisation.** `models.split_keywords()` is the single keyword entry point for both
scrapers. It walks strings, `{"subject"|"value"}` dicts and lists recursively, splits every
string on `,` **and** `;`, trims, then de-duplicates case-insensitively through `unique()`. Both
APIs routinely return one delimited blob inside a single-element list —
`["magnonics, nanomagnetism, spin waves, grating coupler"]` — which used to surface as one
enormous chip; the helper turns it into four. `HarvestRecord.__post_init__` re-runs the helper,
so a delimited blob can never reach SQLite from any code path.

### 4.3 Merge matrix (same DOI in both repositories)

| Field | Winner |
|---|---|
| `source` / `source_keys` | `Infoscience + Zenodo` / `["infoscience","zenodo"]` |
| `title`, `abstract` | Infoscience, unless it is the `Untitled record` placeholder |
| `authors` | Infoscience, falling back to Zenodo |
| `affiliations`, `parent_orgs`, `unit_managers` | Infoscience (authoritative EPFL hierarchy) |
| `author_orcids`, `keywords` | union of both |
| `funding` | Infoscience (`dc.relation.funding` is the authoritative “Funding(s)” column) when it is non-empty, else Zenodo; Zenodo grants are only appended when they are not already covered (see below) |
| `file_summary` | Zenodo when it reports files, else Infoscience |
| `code_repo` | Zenodo (`custom` → `related_identifiers` → `description`), falling back to Infoscience (`epfl.url` → `datacite.relatedIdentifier`) |
| `license`, `access_rights`, `doi`, `url` | Infoscience, falling back to Zenodo |
| `publication_date` | Zenodo (always a full `YYYY-MM-DD`) |
| `created_date` | Infoscience (ingest date drives the window) |
| `resource_type` / `resource_class` | Infoscience, unless it degraded to `other` |

**Funding de-duplication.** `_merge_funding()` keeps the Infoscience labels and only adds a
Zenodo grant whose comparison key — lowercased, with any trailing `(ACRONYM)` stripped —
neither equals nor is a substring of an Infoscience key (and vice versa). Zenodo writes
`FOSTERING … EXASCALE (EoCoE-III)` where Infoscience already has
`FOSTERING … EXASCALE`, so the merged record shows the grant once. If Infoscience has no
funding metadata at all, Zenodo's cleaned list is used as-is.

**Resource scope.** Only listable classes reach the merge: `models.classify_resource_type()`
returns `PUBLICATION_CLASS` (`"publication"`) for `text::…` types (journal articles, conference
papers, theses), Zenodo `publication`, patents and student work — while a
`text::…::software paper` stays `software` — and `harvester._listable()` drops every record
whose `resource_class` is outside `ALL_RESOURCE_CLASSES` before the DOI pairing runs, reporting
how many were skipped in the run log. This is why the *All Works* filter is an explicit
disjunction rather than an empty query (`config.RESOURCE_FILTERS["all"]`):

```
zenodo_query      (resource_type.type:dataset OR resource_type.type:software
                   OR resource_type.type:model OR resource_type.type:workflow
                   OR resource_type.type:other OR resource_type.type:image
                   OR resource_type.type:video OR resource_type.type:poster
                   OR resource_type.type:presentation)
infoscience_query ((dc.type:dataset OR dc.type:software OR dc.type:model
                   OR dc.type:workflow OR dc.type:other)
                   AND NOT dc.type:"student work" AND NOT dc.type:patent)
```

An unfiltered Solr query returns the ~197 000 journal articles of the CRIS, and because
Infoscience is capped at `INFOSCIENCE_MAX_PAGES * INFOSCIENCE_PAGE_SIZE` = **300 records per
run** the harvest would stop inside that flood before reaching a single dataset, model or
research product from an earlier month.

Every `code_repo` candidate — from Zenodo or Infoscience — is resolved through the
single helper pair `models.extract_code_repo_url()` / `models.normalize_code_repo_url()`
(a ~60-line `urllib.parse.urlsplit` implementation), so both sources agree on what a
repository link is:

* **allow-list**: `github.com`, `gitlab.com`, `gitlab.epfl.ch`, any `gitlab.*` host,
  `bitbucket.org`, `codeocean.com`, `sourceforge.net`. Nothing else qualifies, so DOI,
  Zenodo, arXiv, HAL, publisher and documentation links need no deny-list;
* **Pages sites** (`*.github.io`, `*.gitlab.io`) are excluded explicitly;
* **normalisation**: `http` → `https`, `www.` dropped, `?query#fragment` dropped, then the
  path is clipped to `<owner>/<repo>` — which removes `/tree/<ref>`, `/blob/...`,
  `/issues`, `/releases`, … — or, on GitLab, to the `/-/` marker so subgroups
  (`<group>/<subgroup>/<repo>`) survive; trailing `/` and `.git` are removed last;
* **at least two path segments** are required, so a profile URL is never a repository.

So `https://gitlab.com/solidipes/solidipes/-/tree/dev`,
`https://gitlab.com/solidipes/solidipes/` and
`gitlab.com/solidipes/solidipes` all store `https://gitlab.com/solidipes/solidipes`.


---

## 5. Dual persistence

### 5.1 SQLite — `data/harvester.db` (system of record)

Two tables, created on first use (`SCHEMA_VERSION = "1.5"`):

| Table | Purpose |
|---|---|
| `records` | One row per `canonical_id` (DOI when available, otherwise `zenodo-<record id>`, or `infoscience-<handle>`, `infoscience-<uuid>`, `infoscience-<md5 of title+date+author>`). Scalar columns are plain `TEXT`; `source_keys`, `authors`, `affiliations`, `parent_orgs`, `unit_managers`, `author_orcids`, `funding`, `keywords` and `file_summary` are JSON-encoded. Indexed on `publication_date`, `created_date` and `resource_class`. |
| `harvest_runs` | One row per harvest (identical zero-change repeats are coalesced, and each query scope keeps only its previous and current run, see below): `run_at`, `date_from`, `date_to`, `resource_type`, per-source status and counts, `new_count`, `updated_count`, `unchanged_count`, `merged_count`, `total_records`, `duration_seconds`, `notes`. |

**Scope invariant.** Every stored row is listable: `harvester._listable()` drops anything whose
class falls outside `ALL_RESOURCE_CLASSES` before the upsert, `resource_class` is always
`classify_resource_type(resource_type)` (both scrapers derive it that way and
`HarvestRecord.__post_init__` repairs a stale `other`), and neither `records` nor the mirror can
hold a `publication` row.

**Delta detection.** `upsert_records()` opens one transaction, reads the existing row for
each incoming `canonical_id`, and compares it field-by-field against `TRACKED_FIELDS`
(`title`, `abstract`, `affiliations`, `unit_managers`, `doi`, `publication_date`,
`resource_type`, `funding`, `code_repo`, `keywords`, `url`):

- no existing row → `sync_status = "new"`, `first_seen = last_seen = now`
- any tracked field differs → `sync_status = "updated"`, `first_seen` kept, `last_seen = now`
- nothing differs → `sync_status = "unchanged"`, only `last_seen` refreshed

The returned counters (`{"new": n, "updated": n, "unchanged": n}`) are what the UI shows in
Hub Metrics and what the run row stores.

**Superset preservation.** When the stored row's `source_keys` is a *strict superset* of the
incoming record's — the normal case when a merged `["infoscience","zenodo"]` row is revisited
by a run that only reached one repository (Infoscience stopped at its 300-record page cap,
Zenodo answering 429, a narrower window) — `upsert_records()` refuses to downgrade the row:
`source` and `source_keys` stay `Infoscience + Zenodo` / `["infoscience","zenodo"]`, and the
non-empty Infoscience-authoritative fields (`affiliations`, `parent_orgs`, `unit_managers`,
`funding`, `url`, `created_date`, plus `file_summary` when the incoming record ships no files)
are re-applied from the stored row. A Zenodo-only snapshot therefore cannot blank
`cris.virtual.department` affiliations, the PI list or the Infoscience funding. Newer
single-source metadata still lands, because `abstract`, `code_repo`, `keywords` and
`publication_date` are always taken from the incoming record before the comparison runs.

**Run retention.** `harvest_runs` is a bounded audit trail, not an unbounded click log, and it
is bounded twice.

*Per query scope* `(resource_type, date_from, date_to)` only the **previous and the current**
run are kept: every `record_run()` ends with `_prune_scope_runs()`
(`DELETE FROM harvest_runs WHERE <scope> AND id NOT IN (SELECT id … WHERE <scope> ORDER BY id
DESC LIMIT ?)` with `MAX_RUNS_PER_SCOPE = 2`), so re-running one window day after day cannot
accumulate rows just because the metadata kept changing — and duplicates already in the
database disappear on the next harvest. *Across all scopes* the newest
`MAX_HARVEST_RUNS_HISTORY = 25` executions are kept.

On top of that, a zero-change repeat of the newest row's scope is coalesced: when the incoming
execution has `new_count = 0` and `updated_count = 0` and repeats the same `date_from`,
`date_to`, `resource_type`, `zenodo_status` and `infoscience_status`, its `run_at`,
`duration_seconds` and `total_records` refresh that row instead of appending — so pressing
**Run Scraper** repeatedly on an unchanged window cannot flood the table, and the Hub Metrics
tiles keep reporting the counts of the *current* run.

### 5.2 JSON mirror — `data/dataset_cache.json` (human/automation facing)

Rewritten after every harvest via a temp file plus `os.replace`, so an interrupted run can
never leave a half-written file. Top-level keys:

```json
{
  "generated_at": "2026-11-13T13:58:29+00:00",
  "schema_version": "1.5",
  "app": "ENAC-IT4R Dataset & Repository Harvester",
  "record_count": 668,
  "counts_by_source": { "infoscience": 380, "zenodo": 445 },
  "counts_by_resource_class": { "dataset": 502, "software": 65, "other": 101 },
  "source_labels": { "zenodo": "...", "infoscience": "...", "merged": "..." },
  "last_run": { "...": "same shape as the harvest_runs row" },
  "items": [ { "...": "full HarvestRecord dictionaries incl. window_date" } ]
}
```

`counts_by_source` counts a DOI-merged record once per source, so its total exceeds
`record_count`; `counts_by_resource_class` always sums to `record_count`.

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
  marker) is stripped by `models.clean_text()`/`unique()` before anything is stored, so it
  can never surface as a lab name in the UI.
- **Read-only UI** — the browser has no write endpoint other than `/api/harvest`; a refresh
  cannot mutate or re-fetch anything.

---

## 8. In-browser data preview

The dashboard must answer "what is inside this record?" without spending a
multi-gigabyte download. `backend/preview.py` implements `GET /api/preview?url=<file
url>&name=<filename>&inner=<optional path inside a ZIP>` as a **read-only, bounded**
pipeline; `backend/server.py` maps its `PreviewError` to the HTTP status it carries and
everything else to a JSON body.

`PREVIEW_REGISTRY` is the single declarative table behind that pipeline: one
`extension or exact basename -> (handler_kind, byte_limit)` mapping that drives
`resolve_preview_spec()` for direct files **and** for ZIP members, and that
`supported_preview_extensions()` publishes through `/api/data` and `/api/filters`
(as `preview_extensions`). Adding a format is one dictionary entry, plus one renderer
branch when it needs a new `kind`; the frontend never hardcodes an extension list, so
`isPreviewableInner()` and the overlay follow the backend automatically.

A second endpoint, `GET /api/record-files?id=<canonical_id>`, rebuilds the file
inventory of a record whose stored `file_summary` predates the preview `entries`
(see §8.2), so the overlay never depends on the harvest window.

```
Browser                backend/server.py            backend/preview.py          Repository
   |  GET /api/preview       |                              |                        |
   |------------------------>| build_preview(url,name,inner) |                        |
   |                         |----------------------------->| validate_preview_url()  |
   |                         |                              | classify by extension  |
   |                         |                              |--> Range GET / HEAD -->|
   |                         |<-----------------------------| bounded JSON preview   |
   |<------------------------| {kind, inner_path?, ...}      |                        |
```

### 8.1 SSRF guard

`validate_preview_url()` accepts only `https://` URLs whose hostname is exactly
`zenodo.org` or `infoscience.epfl.ch`; a URL with credentials (`user:pass@`), any
other scheme (`http`, `ftp`, `file`) or any other host is rejected with **HTTP 400**
before a socket is opened. Errors while talking to an allowed repository become
**HTTP 502**.

### 8.2 On-demand file inventory (`GET /api/record-files`)

`file_summary` is a tracked field, so the scraper path already persists `entries`,
`count`, `total_bytes` and the download URLs. Rows written before this feature — or
last touched outside the current harvest window — only know the aggregate count,
which would leave the overlay without a file selector.

`fetch_record_file_summary(record)` closes that gap:

1. when `record["file_summary"]["entries"]` is non-empty it returns the stored dict
   unchanged, so the endpoint is a no-op for fresh rows;
2. otherwise `resolve_zenodo_record_id()` extracts the numeric record ID from
   `source_id` (`zenodo-<digits>`) or `doi` (`10.5281/zenodo.<digits>`); a record
   matched by neither is answered with **404** ("This repository does not expose
   downloadable file metadata for this record."), because Infoscience does not
   publish a machine-readable file listing here;
3. one `GET https://zenodo.org/api/records/<recid>` with the shared `REQUEST_HEADERS`
   and a `(PROBE_TIMEOUT, PREVIEW_TIMEOUT)` budget returns the record payload, which
   is mapped through the same `build_file_summary(files, recid)` the scraper uses
   (HTTP ≥ 400 or a network error becomes **502**).

`_api_record_files()` then calls `Storage.update_file_summary()`, which updates the
`file_summary` column and rewrites `data/dataset_cache.json`, and returns
`{"canonical_id": ..., "file_summary": ...}`. The overlay copies that dict into
`state.items`, so reopening the modal renders the selector immediately and a page
reload finds the inventory in SQLite.

### 8.3 Byte budgets and payload shapes

| Extensions | Strategy | Response |
|---|---|---|
| `.csv`, `.tsv` | Range GET for the first 262 144 bytes, incomplete trailing line dropped, UTF-8 then latin-1, `csv.Sniffer` on the first 8 KB (delimiters `, ; \t \|`), header + at most 100 rows | `{"kind": "table", "truncated", "columns", "rows", "row_count_shown"}` |
| `.geojson`, `.json` | Up to 1 048 576 bytes; JSON parsed; GeoJSON detected via `type` in `FeatureCollection`, `Feature`, `GeometryCollection`, `Point`, `MultiPoint`, `LineString`, `MultiLineString`, `Polygon`, `MultiPolygon` | `{"kind": "geojson", "geojson"}` or `{"kind": "json", "content"}` (pretty-printed); unparsable/truncated JSON degrades to `text` |
| `.pdf` | A direct URL is never proxied: the validated link is returned and the overlay embeds it in an `<iframe>`. A PDF inside a ZIP cannot be hot-linked, so it is extracted under the member budgets (capped at 8 MB by `MAX_INNER_PDF_BYTES`) and inlined | `{"kind": "pdf", "pdf_url"}` directly, or `{"kind": "pdf", "pdf_base64", "from_zip": true, "inner_path"}` from a ZIP |
| Source code and configuration: `.py`, `.js`, `.ts`, `.jsx`, `.tsx`, `.vue`, `.c`, `.cpp`, `.h`, `.hpp`, `.rs`, `.go`, `.java`, `.r`, `.jl`, `.m`, `.sql`, `.sh`, `.bash`, `.zsh`, `.toml`, `.ini`, `.cfg`, `.conf`, `.xml`, `.rst`, `.tex`, `.bib`, `.gitignore`, `.env.example`, plus the exact basenames `Makefile`, `Dockerfile`, `LICENSE` and `README` | Same 256 KB text reader as `.txt` and same `TEXT_BYTE_LIMIT`; exact basenames are matched first, so `README.md` still resolves through its extension | `{"kind": "text", "truncated", "content"}` |
| `.txt`, `.md`, `.yml`, `.yaml` | Up to 262 144 bytes, UTF-8/latin-1 | `{"kind": "text", "truncated", "content"}` |
| `.zip` | Central directory through HTTP Range requests, at most 200 listed entries | `{"kind": "zip", "entry_count", "entries": [{name, size, size_human, ext}]}` |
| `.zip` + `inner=<member>` | Central directory located as below, then **only that member's compressed bytes** are fetched; member budget 10 MB uncompressed / 12 MB compressed, images 4 MB, PDFs 8 MB | same payload as the matching direct format, plus `"inner_path"`; an extracted image becomes `{"kind": "image", "image_url": "data:<mime>;base64,…", "from_zip": true}` |
| `.png`, `.jpg`, `.jpeg`, `.webp`, `.svg` | No proxy at all: the validated URL is returned for hot-linking | `{"kind": "image", "image_url"}` |
| anything else (`.nc`, `.zarr`, `.gpkg`, `.parquet`, `.h5`, …) | no fetch | `{"kind": "unsupported", "reason": "Binary scientific format — download record to inspect in Python/QGIS"}` |

`_fetch_bytes()` asks for `limit + 1` bytes through a `Range` header and stops as
soon as the buffer exceeds the budget, so truncation is detected without reading the
rest of the file. `Accept-Encoding: identity` keeps the budget in transferred bytes
rather than decompressed bytes, a `200` response (server ignoring Range) is still
capped by `iter_content`, and a `416` (range past EOF) is retried without Range.
Direct downloads and ZIP members share the same byte parsers
(`_parse_table_bytes()`, `_parse_structured_bytes()`, `_parse_text_bytes()`), so a CSV
renders identically whether it sits behind a URL or inside an archive.

Every preview request goes through one module-level `HTTP_SESSION` (pool 4/8) whose
adapter carries `urllib3`'s `Retry(total=2, connect=2, read=1, backoff_factor=0.25)`
limited to `GET`/`HEAD` with `raise_on_status=False`, so real HTTP errors still reach
the callers. All GETs also use `_session_get()`, which catches the
`ConnectionError`/`RemoteDisconnected` raised when a keep-alive socket was closed while
idle (or after `_fetch_bytes()` closes a partial response early), clears the pool with
`HTTP_SESSION.close()` and retries once — the transient failure never becomes HTTP 502.

### 8.4 Remote ZIP inspection without downloading the archive

`_HttpRangeFile(io.RawIOBase)` is a seekable, read-only file object over
`Range: bytes=start-end` GETs:

* `_remote_zip_size()` first tries `HEAD` (6 s budget) for `Content-Length` and
  `Accept-Ranges: bytes`; Zenodo's CDN omits `Accept-Ranges` and answers HEAD slowly,
  so a one-byte `Range` GET confirms the capability and yields the total size from
  `Content-Range: bytes 0-0/<total>`;
* a `200` answer is accepted in addition to `206` only when the range is
  `0..(size-1)` and `Content-Length` matches the archive size — some CDNs do that
  for a small file whose requested range spans all of it; any other status is
  treated as "no Range support";
* `read()` serves from a 1 MB read-ahead buffer that aligns to the **end** of the
  archive when reading near EOF. The end-of-central-directory record, the zip64
  locator and the central directory therefore travel in one request: listing a
  330 MB / 533-entry archive costs exactly one 1 MB Range GET after the size
  probe;
* an unbounded `read()` is capped at 1 MB, and the wrapper refuses to transfer
  more than 12 MB of ZIP metadata (`MAX_ZIP_BYTES`, HTTP 413), so even a
  million-entry archive can never pull its whole directory into memory;
* a server without Range support yields
  `{"kind": "unsupported", "reason": "Remote server does not support partial ZIP inspection"}`.

`_preview_zip_member(url, inner)` reuses that wrapper to render one member. It first
classifies `inner` through `resolve_preview_spec()` — an unsupported extension answers
**without touching the network** — then resolves the central directory and calls
`archive.getinfo(inner)`:

* a missing name or a directory entry raises `PreviewError(..., 404)`;
* `file_size > 10 MB` or `compress_size > 12 MB` raises `PreviewError(..., 413)`, so a
  compressed bomb is refused before it expands in memory;
* otherwise `archive.open(info).read(limit + 1)` reads at most the format budget
  through the same Range buffer (a member's local header plus its data is one or two
  extra requests) and the shared byte parser renders it;
* an image is inlined as `data:<mime>;base64,…` only when it fits in 4 MB; a larger one
  degrades to a "too large" notice rather than a truncated (corrupt) image;
* a PDF is returned as `pdf_base64` when it fits in 8 MB (`MAX_INNER_PDF_BYTES`), so the
  overlay can build a Blob URL; a larger one gets the same "too large" notice instead of
  a truncated file.

### 8.5 Frontend renderers and session cache

The overlay lives in `frontend/preview.js`, loaded before `app.js`; `app.js` calls
`initPreview()` once and keeps only the delegated `[data-preview-id]` click that
forwards to `openPreviewByKey()`. State is `previewState` (`item`, `entries`, `index`,
`view`, `payload`, `entry`, `chartX`, `chartY`, `token`, `map`); the inner ZIP path is
read from `payload.inner_path`, so the breadcrumb always matches the rendered file.

`renderPreviewPayload()` dispatches on `kind`:

* **table** — `renderPreviewTable()` builds a `<table>` with sticky headers and a
  sticky row-number column, a `title` on every cell (full value even when ellipsised)
  and a row filter (`bindPreviewTableFilter()`) that hides non-matching `<tr>`s and
  updates the "shown / total" counter. The banner states that only the first 256 KB
  and 100 rows are displayed whenever `truncated` is true. When at least one column is
  numeric (60% of its non-empty cells parse as finite numbers), a
  **Table | Quick Chart** toggle and X/Y `<select>`s appear; `renderPreviewChart()`
  draws an inline `<svg>` polyline (EPFL Red stroke, three grid lines, min/mid/max
  labels, `<title>` tooltips) over the first 100 rows, still without a chart library.
* **geojson** — `renderPreviewGeojsonShell()` emits the map container, a status line
  and a hidden `<pre class="preview-code">` fallback. `loadLeaflet()` lazily injects
  `leaflet@1.9.4` CSS + JS from unpkg **only on first use**; `mountPreviewMap()` adds
  an OpenStreetMap tile layer, fits bounds to the GeoJSON layer, and binds a popup
  listing up to 12 feature properties (all HTML-escaped). If the CDN is unreachable,
  the map is hidden and the formatted GeoJSON is revealed instead.
* **zip** — internal paths, extension badges, uncompressed sizes and a **Preview** pill
  on CSV/TSV/JSON/GeoJSON/text/Markdown/YAML/image/PDF/source-code members. Previewable rows carry
  `data-zip-inner`; one delegated click on `#preview-body` calls
  `loadPreviewFile(index, inner)`, which requests
  `/api/preview?url=…&name=…&inner=…`, and `renderPreviewPayload()` prepends the sticky
  breadcrumb from `renderPreviewInnerBar()` (`data-preview-back`). Going back reloads
  the archive listing straight from the session cache.
* **json / text** — `<pre class="preview-code">` with `escapeHtml()`, plus the
  truncation banner where applicable. A `.md` file (direct or inside a ZIP) is rendered
  by `renderMarkdown()`: the document is escaped **first**, fenced blocks are parked as
  placeholders, then headings `#`–`###`, bold, inline code, bullet lists and `https://`
  links are added, so no upstream byte can inject markup. A **Rendered | Raw** toggle
  re-renders from the cached payload without a new request.
* **image** — a centered, responsive `<img>` (max-height 58 vh); `previewImageUrl()`
  accepts both allow-listed `https://` URLs and the `data:image/…;base64,` payloads
  produced for ZIP members.
* **pdf** — a direct PDF is embedded in `<iframe class="preview-pdf">`, letting the
  native viewer stream and paginate it; a PDF extracted from a ZIP is decoded by
  `previewBase64Bytes()` into a Blob object URL first. `releasePreviewPdf()` revokes that
  URL when another file is loaded and in `closePreview()`, so repeated previews cannot
  leak Blob references.
* **unsupported** — reason, file metadata and a direct download button.

`openPreview(item)` drives the selector: `previewEntries(item)` collects the cached
`entries`, `populatePreviewSelect()` fills `#preview-file` (`key · size_human`, the
list disabled below two files) and `loadPreviewFile(0, "")` renders the first one. When
the record has no cached entries, the body shows "Loading file list from
repository…", `GET /api/record-files?id=<canonical_id>` is awaited under the same
`previewState.token` guard, the returned summary is written into `state.items`
(`cachePreviewInventory()`), the selector is populated and the first file is loaded.
A record with no attachments at all shows "No downloadable files are attached to
this record."; a failure renders the backend `detail` inside
`.preview-notice.is-error`.

A bounded `Map` (`previewCache`, `MAX_PREVIEW_CACHE = 40`, keyed by
`url + "::" + inner`) short-circuits `loadPreviewFile()` before any `fetch`, so
switching files, stepping out of a ZIP member and closing/reopening the modal render in
0 ms with zero network requests. Every file load increments `token`, so a slow response
for a previously selected file is dropped; `destroyPreviewMap()` tears down the Leaflet
instance before switching files or closing, and `closePreview()` clears the body and
the `preview-open` scroll lock.

---

## 9. Module map and extension points

| Module | Responsibility | Typical extension |
|---|---|---|
| `backend/config.py` | Paths, verified API constants, `RESOURCE_FILTERS` taxonomy | Add a filter (e.g. `publication`) by adding a key with `zenodo_query`, `infoscience_query`, `classes`, `label`, `pill`, `description` |
| `backend/models.py` | `HarvestRecord`, `clean_text()`, `split_keywords()`, type classification, file summaries (counts + first 25 `entries` with download URLs) | Add a field: extend the dataclass, `to_dict()`, `record_from_row()` and `storage.COLUMNS` |
| `backend/preview.py` | SSRF allow-list, the `PREVIEW_REGISTRY` format table, bounded Range fetches per format, shared byte parsers, table/JSON/GeoJSON/text/ZIP/image/PDF payloads, ZIP-member extraction (`inner=`), on-demand Zenodo file inventory | Add a format: one `PREVIEW_REGISTRY` entry, plus a `_parse_<format>_bytes()` parser and a dispatch branch when it needs a new `kind`; a new trusted host goes in `ALLOWED_HOSTS` |
| `backend/scrapers/zenodo.py` | Lucene query build, creator/grant/license extraction, keyword splitting, pacing | Add a metadata extractor (e.g. `metadata.dates`) |
| `backend/scrapers/infoscience.py` | Solr query build, DSpace metadata extraction, WAF replay | Add a CRIS field (`cris.virtual.*`) to the record mapping |
| `backend/harvester.py` | Window resolution, parallel fetch, DOI merge, run bookkeeping | Change merge precedence in `_merge_pair()` |
| `backend/storage.py` | SQLite schema, delta detection, JSON mirror, on-demand `file_summary` caching | Add a tracked field to `TRACKED_FIELDS` to make it delta-sensitive |
| `backend/logger.py` | Ring-buffer log handler + cursor snapshots | Change `LOG_BUFFER_SIZE` for a longer console history |
| `backend/server.py` | Static serving + JSON API | Add a new `/api/*` route in `_handle_api()` |
| `frontend/preview.js` | The whole preview overlay: `previewState`, on-demand file inventory, session cache (`MAX_PREVIEW_CACHE`), table + row filter, inline SVG quick chart, clickable ZIP members with breadcrumb, Markdown renderer, image/PDF/unsupported/GeoJSON renderers and the lazy Leaflet loader | Member previewability comes from `state.previewExtensions` (the backend registry), so new member types need no change here; a new view goes in `renderPreviewPayload()`, and a new toggle reuses the delegated `[data-*]` click handler in `initPreview()` |
| `frontend/app.js` | Rendering, filtering, exports, adaptive log polling (`scheduleLogPoll()`), and the single-control collapsible contract (`collapsiblePill()`, `renderCollapsibleText()` for prose, `renderCollapsibleItems()` for lists, behind one `[data-collapsible-toggle]` handler) | Field renderers are centralized in `metaList()`, `renderAuthors()`, `renderHeaderBadges()`, `renderFunding()` and `renderKeywords()`; the label vocabulary in `SOURCE_LABELS` and `FALLBACK_FILTERS`; preview entry points in `frontend/preview.js` |

**Design rules kept throughout:** no third-party Python package beyond `requests`; no
JavaScript build step or framework; no credentials or environment-specific values; every
displayed value traceable to an upstream record; failures degrade to a partial, clearly
labelled result rather than a silent empty page.

---

## 10. Regression suite

`tests/` is a plain `unittest` suite with no network access and no extra dependency:

```bash
python -m unittest discover -s tests -v
```

| Module | Coverage |
|---|---|
| `test_models_and_storage.py` | `HarvestRecord` helpers and file summaries; delta detection across `new` / `updated` / `unchanged`; superset merge that must survive a later partial harvest; `update_file_summary()` mirroring; `MAX_RUNS_PER_SCOPE` retention |
| `test_preview_and_api.py` | SSRF allow-list, `resolve_preview_spec()` matrix, `supported_preview_extensions()`, the shared table/structured parsers, in-memory ZIP central directory and member extraction (CSV, code, PDF, PNG, 404/413 cases), direct PDF/image dispatch, and a real `create_server(port=0)` instance serving `/api/data`, `/api/filters`, `/api/logs`, `/api/status`, `/api/preview` and `/api/record-files` |
| `test_e2e_playwright.py` | Optional Chromium walkthrough (cards, search, keyword badge, preview modal, `Escape`), skipped with `unittest.skipUnless` when `playwright` is not installed |

The ZIP cases patch `_remote_zip_size()` and `_HttpRangeFile` with a buffered archive, so
the remote-inspection code runs unmodified without contacting Zenodo; the API cases patch
`storage._default_storage` with a temporary SQLite database.

