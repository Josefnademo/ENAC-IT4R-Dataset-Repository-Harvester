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
| `file_summary` | `files[]` → count, total bytes, `total_human`, distinct extensions |
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

## 8. Module map and extension points

| Module | Responsibility | Typical extension |
|---|---|---|
| `backend/config.py` | Paths, verified API constants, `RESOURCE_FILTERS` taxonomy | Add a filter (e.g. `publication`) by adding a key with `zenodo_query`, `infoscience_query`, `classes`, `label`, `pill`, `description` |
| `backend/models.py` | `HarvestRecord`, `clean_text()`, `split_keywords()`, type classification, file summaries | Add a field: extend the dataclass, `to_dict()`, `record_from_row()` and `storage.COLUMNS` |
| `backend/scrapers/zenodo.py` | Lucene query build, creator/grant/license extraction, keyword splitting, pacing | Add a metadata extractor (e.g. `metadata.dates`) |
| `backend/scrapers/infoscience.py` | Solr query build, DSpace metadata extraction, WAF replay | Add a CRIS field (`cris.virtual.*`) to the record mapping |
| `backend/harvester.py` | Window resolution, parallel fetch, DOI merge, run bookkeeping | Change merge precedence in `_merge_pair()` |
| `backend/storage.py` | SQLite schema, delta detection, JSON mirror | Add a tracked field to `TRACKED_FIELDS` to make it delta-sensitive |
| `backend/logger.py` | Ring-buffer log handler + cursor snapshots | Change `LOG_BUFFER_SIZE` for a longer console history |
| `backend/server.py` | Static serving + JSON API | Add a new `/api/*` route in `_handle_api()` |
| `frontend/app.js` | Rendering, filtering, exports, polling, and the single-control collapsible contract (`collapsiblePill()`, `renderCollapsibleText()` for prose, `renderCollapsibleItems()` for lists, behind one `[data-collapsible-toggle]` handler) | Field renderers are centralized in `metaList()`, `renderAuthors()`, `renderHeaderBadges()`, `renderFunding()` and `renderKeywords()`; the label vocabulary in `SOURCE_LABELS` and `FALLBACK_FILTERS` |

**Design rules kept throughout:** no third-party Python package beyond `requests`; no
JavaScript build step or framework; no credentials or environment-specific values; every
displayed value traceable to an upstream record; failures degrade to a partial, clearly
labelled result rather than a silent empty page.

