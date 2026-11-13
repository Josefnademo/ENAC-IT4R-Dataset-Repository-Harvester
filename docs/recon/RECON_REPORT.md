# EPFL Repository API Reconnaissance Report

Live diagnostic probe of the two EPFL open-data endpoints used by the ENAC-IT4R Dataset Harvester.

| Item | Value |
|---|---|
| Generated (UTC) | 2026-11-10T08:07:36+00:00 |
| Probe host | win32 / Python 3.14.10 |
| HTTP client, primary | requests 2.34.2 (OpenSSL TLS) |
| HTTP client, fallback | C:\windows\system32\curl.EXE |
| Zenodo endpoint | `https://zenodo.org/api/communities/epfl/records` |
| Infoscience endpoint | `https://infoscience.epfl.ch/server/api/discover/search/objects` |
| Zenodo probed at (UTC) | 2026-11-10T08:03:25+00:00 |
| Infoscience probed at (UTC) | 2026-11-09T16:17:01+00:00 |

## 0. Executive summary

| Service | API family | Reachability from this host | Live JSON | Verdict |
|---|---|---|---|---|
| Zenodo EPFL community | InvenioRDM | HTTP 200, open public API, no bot wall | Yes | PASS - endpoint reachable, 21 live probes, all documented syntaxes reproduced |
| EPFL Infoscience | DSpace 7 CRIS | HTTP 200 for all 16 probes through Python requests - the AWS WAF did **not** challenge this host in this run | Yes | PASS - live JSON retrieved; all 16 calls were accepted through plain Python requests (no WAF challenge in this run) |

| Measurement | Zenodo | Infoscience |
|---|---|---|
| Datasets, no date filter | 346 | 396 |
| Software, no date filter | 49 | 25 |
| Datasets inside the tested window | 11 | 47 |
| Page index base | 1-indexed - page=0 is rejected with HTTP 400 | 0-indexed (page=0 is the first page) |
| Max effective page size | 25 | 100 |
| Rate-limit signalling | X-RateLimit-Limit / Remaining / Reset + Retry-After | no X-RateLimit-*; HTTP 429 + Retry-After after a burst |

## 1. Deliverable - working Zenodo URL (datasets, date window)

Date window probed: **2026-08-12 to 2026-11-10** (rolling last 90 days).

**Recommended URL - system creation date (created):**

```text
https://zenodo.org/api/communities/epfl/records?q=resource_type.type%3Adataset+AND+created%3A%5B2026-08-12+TO+2026-11-10%5D&size=5&sort=newest&page=1
```

**Alternative URL - publication date (metadata.publication_date):**

```text
https://zenodo.org/api/communities/epfl/records?q=resource_type.type%3Adataset+AND+metadata.publication_date%3A%5B2026-08-12+TO+2026-11-10%5D&size=5&sort=newest&page=1
```

| Query variant | q expression | HTTP | Total hits | Result |
|---|---|---|---|---|
| created range | `resource_type.type:dataset AND created:[2026-08-12 TO 2026-11-10]` | 200 | 11 | PASS |
| metadata.publication_date range | `resource_type.type:dataset AND metadata.publication_date:[2026-08-12 TO 2026-11-10]` | 200 | 9 | PASS |
| bare publication_date (no metadata. prefix) | `resource_type.type:dataset AND publication_date:[2026-08-12 TO 2026-11-10]` | 200 | 9 | same count as the prefixed form - the alias resolves |
| created range, no type filter | `created:[2026-08-12 TO 2026-11-10]` | 200 | 17 | reference count |

Resource-type syntax (InvenioRDM Lucene field query):

| Query | HTTP | Total hits |
|---|---|---|
| no filter (whole community) | 200 | 512 |
| q=resource_type.type:dataset | 200 | 346 |
| q=resource_type.type:software | 200 | 49 |

## 2. Deliverable - working Infoscience URL (datasets, date window)

Date window probed: **2025-11-09 to 2026-11-09** (rolling last 365 days).

**Recommended URL - dc.date.issued range:**

```text
https://infoscience.epfl.ch/server/api/discover/search/objects?configuration=researchoutputs&query=dc.type%3Adataset+AND+dc.date.issued%3A%5B2025-11-09+TO+2026-11-09%5D&size=5&page=0&sort=dc.date.issued%2Cdesc
```

**Alternative URL - dc.date.accessioned range (ingest date):**

```text
https://infoscience.epfl.ch/server/api/discover/search/objects?configuration=researchoutputs&query=dc.type%3Adataset+AND+dc.date.accessioned%3A%5B2025-11-09+TO+2026-11-09%5D&size=3&page=0&sort=dc.date.accessioned%2Cdesc
```

Query-syntax results:

| Query / filter | HTTP | Total hits | Note |
|---|---|---|---|
| query=dc.type:dataset | 200 | 396 | - |
| query=dc.type:software | 200 | 25 | - |
| query=* (configuration baseline) | 200 | 197730 | - |
| dc.type:dataset AND dc.date.issued:[2025-11-09 TO 2026-11-09] | 200 | 47 | primary window |
| dc.type:dataset AND dc.date.accessioned:[2025-11-09 TO 2026-11-09] | 200 | 73 | ingest date variant |
| f.resourceType=dataset,equals | 400 | - | An exception has occurred |
| f.itemtype=dataset,equals | 400 | - | An exception has occurred |

> Facet filters only work for facet names declared in the `researchoutputs` discovery configuration. Both candidate names above answer with a generic HTTP 400 (`An exception has occurred`) instead of a hit count, so they cannot be used; the free-text `query=` syntax is the portable way to filter by type.

## 3. Deliverable - raw JSON snippet of one real dataset per service

### 3.1 Zenodo - record `20840785`

Fetched with: `https://zenodo.org/api/communities/epfl/records?q=resource_type.type%3Adataset+AND+created%3A%5B2026-08-12+TO+2026-11-10%5D&size=5&sort=newest&page=1`

```json
{
  "created": "2026-11-05T15:09:57.965664+00:00",
  "modified": "2026-11-05T15:09:58.267191+00:00",
  "id": 20840785,
  "conceptrecid": "20840784",
  "doi": "10.5281/zenodo.20840785",
  "conceptdoi": "10.5281/zenodo.20840784",
  "doi_url": "https://doi.org/10.5281/zenodo.20840785",
  "metadata": {
    "title": "Atlas of Regenerative Materials",
    "doi": "10.5281/zenodo.20840785",
    "publication_date": "2026-06-25",
    "description": "<h2>The Atlas of Regenerative Materials</h2>\n<p>The Atlas of Regenerative Materials is collecting knowledge and know-how on availability and use of regenerative materials for construction. Data is structured in 5 entities: Resource, Buidling Material, Construction Technique, Professionals and Buildings. Taxonomies for ...",
    "access_right": "open",
    "creators": [
      {
        "name": "Habert, Guillaume",
        "affiliation": "ETH Zürich",
        "orcid": "0000-0003-3533-7896"
      },
      {
        "name": "Huber, Shoshana",
        "affiliation": "ETH Zürich",
        "orcid": "0009-0003-1354-5026"
      },
      {
        "name": "Bengana, Alia",
        "affiliation": "School of Engineering and  Architecture of Fribourg",
        "orcid": "0009-0004-4647-7178"
      },
      {
        "name": "Marcon, Yannick",
        "affiliation": "École Polytechnique Fédérale de Lausanne",
        "orcid": "0000-0003-0138-2023"
      }
    ],
    "contributors": [
      {
        "name": "Bocco, Andrea",
        "affiliation": "Politecnico di Torino",
        "type": "DataCurator",
        "orcid": "0000-0001-5639-5660"
      },
      {
        "name": "Fanelsa, Niklas",
        "affiliation": "Technical University of Munich",
        "type": "DataCurator",
        "orcid": "0009-0007-6216-4843"
      },
      {
        "name": "Calabria-Holley, Juliana",
        "affiliation": "University of Bath",
        "type": "DataCurator",
        "orcid": "0000-0002-0764-9473"
      },
      {
        "name": "Natarajan, Sukumar",
        "affiliation": "University of Bath",
        "type": "DataCurator",
        "orcid": "0000-0001-5831-1678"
      }
    ],
    "version": "1.0.0",
    "language": "eng",
    "custom": {
      "code:codeRepository": "https://github.com/EPFL-ENAC/ALICE-ETHZ-AREMA",
      "code:programmingLanguage": [
        {
          "id": "python",
          "title": {
            "en": "Python"
          }
        },
        {
          "id": "vue",
          "title": {
            "en": "Vue"
          }
        }
      ],
      "code:developmentStatus": {
        "id": "active",
        "title": {
          "en": "Active"
        }
      }
    },
    "resource_type": {
      "title": "Dataset",
      "type": "dataset"
    },
    "license": {
      "id": "cc-by-sa-4.0"
    },
    "communities": [
      {
        "id": "epfl"
      }
    ],
    "relations": {
      "version": [
        {
          "index": 0,
          "is_last": true,
          "parent": {
            "pid_type": "recid",
            "pid_value": "20840784"
          }
        }
      ]
    }
  },
  "title": "Atlas of Regenerative Materials",
  "links": {
    "self": "https://zenodo.org/api/records/20840785",
    "self_html": "https://zenodo.org/records/20840785",
    "preview_html": "https://zenodo.org/records/20840785?preview=1",
    "doi": "https://doi.org/10.5281/zenodo.20840785",
    "self_doi": "https://doi.org/10.5281/zenodo.20840785",
    "self_doi_html": "https://zenodo.org/doi/10.5281/zenodo.20840785",
    "reserve_doi": "https://zenodo.org/api/records/20840785/draft/pids/doi",
    "parent": "https://zenodo.org/api/records/20840784",
    "parent_html": "https://zenodo.org/records/20840784",
    "parent_doi": "https://doi.org/10.5281/zenodo.20840784",
    "parent_doi_html": "https://zenodo.org/doi/10.5281/zenodo.20840784",
    "self_iiif_manifest": "https://zenodo.org/api/iiif/record:20840785/manifest",
    "self_iiif_sequence": "https://zenodo.org/api/iiif/record:20840785/sequence/default",
    "files": "https://zenodo.org/api/records/20840785/files",
    "media_files": "https://zenodo.org/api/records/20840785/media-files",
    "latest": "https://zenodo.org/api/records/20840785/versions/latest",
    "latest_html": "https://zenodo.org/records/20840785/latest",
    "versions": "https://zenodo.org/api/records/20840785/versions",
    "draft": "https://zenodo.org/api/records/20840785/draft",
    "access_links": "https://zenodo.org/api/records/20840785/access/links",
    "access_grants": "https://zenodo.org/api/records/20840785/access/grants",
    "access_users": "https://zenodo.org/api/records/20840785/access/users",
    "access_request": "https://zenodo.org/api/records/20840785/access/request",
    "access": "https://zenodo.org/api/records/20840785/access",
    "communities": "https://zenodo.org/api/records/20840785/communities",
    "communities-suggestions": "https://zenodo.org/api/records/20840785/communities-suggestions",
    "request_deletion": "https://zenodo.org/api/records/20840785/request-deletion",
    "file_modification": "https://zenodo.org/api/records/20840785/file-modification",
    "quota_increase": "https://zenodo.org/api/records/20840785/quota-increase",
    "requests": "https://zenodo.org/api/records/20840785/requests"
  },
  "updated": "2026-11-05T15:09:58.267191+00:00",
  "recid": "20840785",
  "revision": 4,
  "files": [
    {
      "id": "1ed99036-4676-496a-ada2-2affafb65141",
      "key": "arema_snapshot_2026-10-08_10-10-55_no_authors.zip",
      "size": 330156115,
      "checksum": "md5:081b33a83d8709181e6f0767cb0d1354",
      "links": {
        "self": "https://zenodo.org/api/records/20840785/files/arema_snapshot_2026-10-08_10-10-55_no_authors.zip/content"
      }
    }
  ],
  "swh": null,
  "owners": [
    {
      "id": "1693361"
    }
  ],
  "status": "published",
  "stats": {
    "downloads": 5,
    "unique_downloads": 4,
    "views": 26,
    "unique_views": 22,
    "version_downloads": 5,
    "version_unique_downloads": 4,
    "version_unique_views": 22,
    "version_views": 26
  },
  "state": "done",
  "submitted": true
}
```

### 3.2 Infoscience - handle `20.500.14299/269054`

Fetched with: `https://infoscience.epfl.ch/server/api/discover/search/objects?configuration=researchoutputs&query=dc.type%3Adataset+AND+dc.date.issued%3A%5B2025-11-09+TO+2026-11-09%5D&size=25&page=0&sort=dc.date.accessioned%2Cdesc`

Note: field names and values are exactly as returned by the API, with these display-only reductions:

- long free-text values (abstract, citations) are cut to 220 characters;
- value lists are capped at 4 entries;
- dropped, because they carry no record content:
  `indexableObject._links` hypermedia, the CRIS internal UUID mirrors (`cris.virtualsource.*`) and the auto-generated citation strings (`epfl.citation.*`). Section 6.2 still lists every key the API returns.

```json
{
  "hitHighlights": null,
  "type": "discover",
  "uniqueType": "discover.discover",
  "_links": {
    "indexableObject": {
      "href": "https://infoscience.epfl.ch/server/api/core/items/a21bc5df-4b44-434e-a81a-36eb4548d879"
    }
  },
  "_embedded": {
    "indexableObject": {
      "id": "a21bc5df-4b44-434e-a81a-36eb4548d879",
      "uuid": "a21bc5df-4b44-434e-a81a-36eb4548d879",
      "name": "Atlas of Regenerative Materials",
      "handle": "20.500.14299/269054",
      "metadata": {
        "cris.virtual.author-scopus": [
          {"value": "#PLACEHOLDER_PARENT_METADATA_VALUE#", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "cris.virtual.department": [
          {"value": "ENAC-IT", "language": null, "authority": "4602c074-dbba-4f2e-a40d-1c64abf027f2", "confidence": 600, "place": 0}
        ],
        "cris.virtual.orcid": [
          {"value": "#PLACEHOLDER_PARENT_METADATA_VALUE#", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "cris.virtual.parent-organization": [
          {"value": "IA", "language": null, "authority": "f5a2327d-ca6f-4f51-89c1-0d5cfc8242fa", "confidence": 600, "place": 0},
          {"value": "ENAC", "language": null, "authority": "95372c6b-7d45-432e-a84e-660c9fa54e05", "confidence": 600, "place": 1},
          {"value": "EPFL", "language": null, "authority": "41674f42-ba15-4612-9817-2a6f60985c01", "confidence": 600, "place": 2},
          {"value": "#PLACEHOLDER_PARENT_METADATA_VALUE#", "language": null, "authority": null, "confidence": -1, "place": 3}
        ],
        "cris.virtual.rid": [
          {"value": "#PLACEHOLDER_PARENT_METADATA_VALUE#", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "cris.virtual.sciperId": [
          {"value": "375133", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "cris.virtual.unitId": [
          {"value": "10241", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "cris.virtual.unitManager": [
          {"value": "Kaufmann, Vincent", "language": null, "authority": "04a63bfb-922b-4e43-90b0-946e31cb8ecc", "confidence": 600, "place": 0},
          {"value": "Desimone, Laurent", "language": null, "authority": "244a1e74-4ed1-4ec8-8878-e71def5dec03", "confidence": 600, "place": 1}
        ],
        "ctb.oaireXXlicenseCondition": [
          {"value": "CC BY-SA", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "datacite.relatedIdentifier": [
          {
            "value": "https://doi.org/10.5281/zenodo.20840784",
            "language": null,
            "authority": null,
            "confidence": -1,
            "place": 0,
            "securityLevel": 0
          }
        ],
        "datacite.relationType": [
          {
            "value": "IsVersionOf",
            "language": null,
            "authority": null,
            "confidence": -1,
            "place": 0,
            "securityLevel": 0
          }
        ],
        "datacite.rights": [
          {"value": "metadata-only", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.contributor": [
          {"value": "Bocco, Andrea", "language": null, "authority": null, "confidence": -1, "place": 0},
          {"value": "Fanelsa, Niklas", "language": null, "authority": null, "confidence": -1, "place": 1},
          {"value": "Calabria-Holley, Juliana", "language": null, "authority": null, "confidence": -1, "place": 2},
          {"value": "Natarajan, Sukumar", "language": null, "authority": null, "confidence": -1, "place": 3}
        ],
        "dc.contributor.author": [
          {"value": "Habert, Guillaume", "language": null, "authority": null, "confidence": -1, "place": 0},
          {"value": "Huber, Shoshana", "language": null, "authority": null, "confidence": -1, "place": 1},
          {"value": "Bengana, Alia", "language": null, "authority": null, "confidence": -1, "place": 2},
          {"value": "Marcon, Yannick", "language": null, "authority": "4df335e1-aac1-4797-912c-817b55177c7a", "confidence": 600, "place": 3}
        ],
        "dc.date.accessioned": [
          {"value": "2026-11-06T06:30:59Z", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.date.available": [
          {"value": "2026-11-06T06:30:59Z", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.date.created": [
          {"value": "2026-11-05", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.date.issued": [
          {"value": "2026", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.date.modified": [
          {"value": "2026-11-06T12:35:50.645180Z", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.description.abstract": [
          {"value": "The Atlas of Regenerative Materials\n\nThe Atlas of Regenerative Materials is collecting knowledge and know-how on availability and use of regenerative materials for construction. Data is structured in 5 entities: Resource...", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.description.sponsorship": [
          {"value": "LASUR", "language": null, "authority": "4997bbce-76d4-4f27-be42-926d32669650", "confidence": 600, "place": 0}
        ],
        "dc.description.version": [
          {"value": "1.0.0", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.identifier.doi": [
          {"value": "10.5281/zenodo.20840785", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.identifier.uri": [
          {"value": "https://infoscience.epfl.ch/handle/20.500.14299/269054", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.language.iso": [
          {"value": "en", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.publisher": [
          {"value": "Zenodo", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.relation.funding": [
          {"value": "ORD Explore Grant", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.relation.grantno": [
          {"value": "#PLACEHOLDER_PARENT_METADATA_VALUE#", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.relation.title": [
          {
            "value": "Atlas of Regenerative Materials",
            "language": null,
            "authority": null,
            "confidence": -1,
            "place": 0,
            "securityLevel": 0
          }
        ],
        "dc.title": [
          {"value": "Atlas of Regenerative Materials", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "dc.type": [
          {"value": "dataset", "language": "en", "authority": "dataset-coar-types:c_ddb1", "confidence": 600, "place": 0}
        ],
        "dspace.entity.type": [
          {"value": "Product", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "epfl.author.orcid": [
          {"value": "0000-0003-3533-7896", "language": null, "authority": null, "confidence": -1, "place": 0},
          {"value": "0009-0003-1354-5026", "language": null, "authority": null, "confidence": -1, "place": 1},
          {"value": "0009-0004-4647-7178", "language": null, "authority": null, "confidence": -1, "place": 2},
          {"value": "0000-0003-0138-2023", "language": null, "authority": null, "confidence": -1, "place": 3}
        ],
        "epfl.contributor.orcid": [
          {"value": "0000-0001-5639-5660", "language": null, "authority": null, "confidence": -1, "place": 0},
          {"value": "0009-0007-6216-4843", "language": null, "authority": null, "confidence": -1, "place": 1},
          {"value": "0000-0002-0764-9473", "language": null, "authority": null, "confidence": -1, "place": 2},
          {"value": "0000-0001-5831-1678", "language": null, "authority": null, "confidence": -1, "place": 3}
        ],
        "epfl.contributor.role": [
          {"value": "#PLACEHOLDER_PARENT_METADATA_VALUE#", "language": null, "authority": null, "confidence": -1, "place": 0},
          {"value": "#PLACEHOLDER_PARENT_METADATA_VALUE#", "language": null, "authority": null, "confidence": -1, "place": 1},
          {"value": "#PLACEHOLDER_PARENT_METADATA_VALUE#", "language": null, "authority": null, "confidence": -1, "place": 2},
          {"value": "#PLACEHOLDER_PARENT_METADATA_VALUE#", "language": null, "authority": null, "confidence": -1, "place": 3}
        ],
        "epfl.url": [
          {
            "value": "https://github.com/EPFL-ENAC/ALICE-ETHZ-AREMA",
            "language": null,
            "authority": null,
            "confidence": -1,
            "place": 0,
            "securityLevel": 0
          }
        ],
        "epfl.url.description": [
          {
            "value": "Code Repository URL",
            "language": null,
            "authority": null,
            "confidence": -1,
            "place": 0,
            "securityLevel": 0
          }
        ],
        "epfl.workflow.startDateTime": [
          {"value": "2026-11-05T21:09:30.596Z", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "epfl.writtenAt": [
          {"value": "EPFL", "language": null, "authority": null, "confidence": -1, "place": 0}
        ],
        "oairecerif.author.affiliation": [
          {"value": "ETH Zürich", "language": null, "authority": null, "confidence": -1, "place": 0},
          {"value": "ETH Zürich", "language": null, "authority": null, "confidence": -1, "place": 1},
          {"value": "School of Engineering and  Architecture of Fribourg", "language": null, "authority": null, "confidence": -1, "place": 2},
          {"value": "École Polytechnique Fédérale de Lausanne", "language": null, "authority": "will be referenced::ROR-ID::https://ror.org/02s376052", "confidence": -1, "place": 3}
        ],
        "oairecerif.contributor.affiliation": [
          {"value": "Politecnico di Torino", "language": null, "authority": null, "confidence": -1, "place": 0},
          {"value": "Technical University of Munich", "language": null, "authority": null, "confidence": -1, "place": 1},
          {"value": "University of Bath", "language": null, "authority": null, "confidence": -1, "place": 2},
          {"value": "University of Bath", "language": null, "authority": null, "confidence": -1, "place": 3}
        ],
        "oairecerif.funder": [
          {"value": "ETH Zurich", "language": null, "authority": "401c2295-63c4-45f1-af34-af8cab6d50d6", "confidence": 400, "place": 0}
        ]
      },
      "inArchive": true,
      "discoverable": true,
      "withdrawn": false,
      "lastModified": "2026-11-06T12:35:50.645+00:00",
      "entityType": "Product",
      "type": "item",
      "uniqueType": "core.item"
    }
  },
  "_recon_snippet_note": {
    "dropped_fields": [
      "indexableObject._links",
      "cris.virtualsource.author-scopus",
      "cris.virtualsource.department",
      "cris.virtualsource.orcid",
      "cris.virtualsource.parent-organization",
      "cris.virtualsource.rid",
      "cris.virtualsource.sciperId",
      "cris.virtualsource.unitId",
      "cris.virtualsource.unitManager",
      "epfl.citation.apa",
      "epfl.citation.chicago",
      "epfl.citation.cslitem",
      "epfl.citation.date",
      "epfl.citation.harvard",
      "epfl.citation.ieee",
      "epfl.citation.iso690",
      "epfl.citation.mla",
      "epfl.citation.vancouver"
    ],
    "value_lists_capped_to": 4,
    "capped_counts": {},
    "string_limit": 220
  }
}
```

## 4. Deliverable - exact JSON field paths

Every path below was verified against the live sample payload in section 3. `NOT PRESENT` means the field does not exist in that response.

### 4.1 Zenodo (InvenioRDM)

| Logical field | Verified path | Example value | Other candidates tested |
|---|---|---|---|
| Title | `metadata.title` | Atlas of Regenerative Materials | - |
| Author (single) | `metadata.creators[0].name` | Habert, Guillaume | `metadata.creators[0].name`<br>`metadata.creators[0].person_or_org.name`<br>`metadata.creators[0].person_or_org.family_name` |
| All authors | `metadata.creators` | 4 entries, first = Habert, Guillaume | - |
| Affiliation / unit | `metadata.creators[0].affiliation` | ETH Zürich | `metadata.creators[0].affiliation`<br>`metadata.creators[0].affiliations[0].name` |
| Publication date | `metadata.publication_date` | 2026-06-25 | `metadata.publication_date`<br>`created` |
| DOI | `doi` | 10.5281/zenodo.20840785 | `doi`<br>`metadata.doi`<br>`doi_url`<br>`conceptdoi`<br>`pids.doi.identifier` |
| Direct record URL | `links.self_html` | https://zenodo.org/records/20840785 | `links.self_html`<br>`links.html`<br>`links.self` |
| Resource type | `metadata.resource_type.type` | dataset | `metadata.resource_type.type`<br>`metadata.resource_type.id` |
| File entry | `files[0].key` | arema_snapshot_2026-10-08_10-10-55_no_authors.zip | `files[0].key`<br>`files[0].size`<br>`files[0].checksum`<br>`files[0].links.self`<br>`files.entries[0].key`<br>`files.entries[0].links.content` |

| Derived value | Template / example |
|---|---|
| Record URL (fallback) | `https://zenodo.org/records/{id}` -> https://zenodo.org/records/20840785 |

Zenodo hit layout: this deployment returns the **legacy flat serializer** - author name and unit are siblings on the same object (`metadata.creators[i].name`, `metadata.creators[i].affiliation`), and ORCID is a plain `metadata.creators[i].orcid` string.

### 4.2 EPFL Infoscience (DSpace 7 CRIS)

| Logical field | Verified path | Example value | Other candidates tested |
|---|---|---|---|
| Title | `_embedded.indexableObject.metadata.dc.title[0].value` | Atlas of Regenerative Materials | - |
| Author (single) | `_embedded.indexableObject.metadata.dc.contributor.author[0].value` | Habert, Guillaume | `_embedded.indexableObject.metadata.dc.contributor.author[0].value`<br>`_embedded.indexableObject.metadata.dc.creator[0].value` |
| All authors | `_embedded.indexableObject.metadata.dc.contributor.author` | 4 entries, first = Habert, Guillaume | - |
| Laboratory / unit affiliation | `_embedded.indexableObject.metadata.cris.virtual.department[0].value` | ENAC-IT | `_embedded.indexableObject.metadata.cris.virtual.department[0].value`<br>`_embedded.indexableObject.metadata.cris.virtual.unitId[0].value`<br>`_embedded.indexableObject.metadata.oairecerif.author.affiliation[0].value`<br>`_embedded.indexableObject.metadata.epfl.unit[0].value`<br>`_embedded.indexableObject.metadata.epfl.laboratory[0].value`<br>`_embedded.indexableObject.metadata.dc.contributor.department[0].value`<br>`_embedded.indexableObject.metadata.epfl.department[0].value` |
| Publication date | `_embedded.indexableObject.metadata.dc.date.issued[0].value` | 2026 | - |
| Accession date | `_embedded.indexableObject.metadata.dc.date.accessioned[0].value` | 2026-11-06T06:30:59Z | - |
| DOI | `_embedded.indexableObject.metadata.dc.identifier.doi[0].value` | 10.5281/zenodo.20840785 | `_embedded.indexableObject.metadata.dc.identifier.doi[0].value`<br>`_embedded.indexableObject.metadata.dc.identifier.uri[0].value` |
| Direct record URL | `_embedded.indexableObject.handle` | 20.500.14299/269054 | `handle`<br>`_embedded.indexableObject.handle`<br>`_embedded.indexableObject._links.self.href` |
| Resource type | `_embedded.indexableObject.metadata.dc.type[0].value` | dataset | - |
| Item UUID | `_embedded.indexableObject.uuid` | a21bc5df-4b44-434e-a81a-36eb4548d879 | - |

| Derived value | Template / example |
|---|---|
| Record URL (fallback) | `https://infoscience.epfl.ch/handle/{handle}` -> https://infoscience.epfl.ch/handle/20.500.14299/269054 |

## 5. Deliverable - pagination parameters

| Service | Page parameter | Index base | Size parameter | Max size observed |
|---|---|---|---|---|
| Zenodo (InvenioRDM) | `page` | 1-indexed - page=0 is rejected with HTTP 400 | `size` | 25 |
| Infoscience (DSpace 7) | `page` | 0-indexed (page=0 is the first page) | `size` | 100 |

### 5.1 Zenodo page-size matrix

| Requested size | HTTP | Records returned | Accepted | Note |
|---|---|---|---|---|
| 10 | 200 | 10 | yes | size accepted |
| 25 | 200 | 25 | yes | size accepted |
| 50 | 400 | 0 | no | HTTP 400: size: Page size cannot be greater than 25. Please use authenticated requests to increase the limit to 100. |
| 100 | 400 | 0 | no | HTTP 400: size: Page size cannot be greater than 25. Please use authenticated requests to increase the limit to 100. |
| 200 | 400 | 0 | no | HTTP 400: size: Page size cannot be greater than 25. Please use authenticated requests to increase the limit to 100. |
| 1000 | 400 | 0 | no | HTTP 400: size: Page size cannot be greater than 25. Please use authenticated requests to increase the limit to 100. |

> The rejection message names the authenticated ceiling - `size=100` with a personal access token. Anonymous harvesting is therefore capped at 25 records per page; with the 1 request / 2 s budget that is ~750 records per minute.

### 5.2 Zenodo page index probe

| page value | HTTP | First record ids returned |
|---|---|---|
| 0 | 400 | - |
| 1 | 200 | 20840785, 23345145, 23190655 |
| 2 | 200 | 23209677, 23015391, 22768812 |

### 5.3 Infoscience page-size and page-index probes

| Requested size | HTTP | Objects returned | Server page object |
|---|---|---|---|
| 100 | 200 | 100 | {"number": 0, "size": 100, "totalPages": 4, "totalElements": 396} |
| 200 | 200 | 100 | {"number": 0, "size": 200, "totalPages": 2, "totalElements": 396} |

| page value | HTTP | Handles returned |
|---|---|---|
| 0 | 200 | 20.500.14299/269054, 20.500.14299/268377 |
| 1 | 200 | 20.500.14299/268122, 20.500.14299/267349 |

### 5.4 Infoscience offset hazard when size > 100

| Request | First handle returned | Server page object |
|---|---|---|
| size=200&page=1 | 20.500.14299/206126 | {"number": 1, "size": 200, "totalPages": 2, "totalElements": 300} |
| size=100&page=2 | 20.500.14299/206126 | {"number": 2, "size": 100, "totalPages": 4, "totalElements": 396} |

**Gap detected: YES** - size>100 is clamped to 100 objects per response, but the offset is still computed as page*size - so 100 records are silently skipped on every page transition. Never request size>100.

## 6. Probe detail - hit object structures

### 6.1 Zenodo hit object

| Element | Observed |
|---|---|
| Serializer flavour | legacy flat serializer (creators[i].name / creators[i].affiliation) |
| Top-level keys | `conceptdoi`, `conceptrecid`, `created`, `doi`, `doi_url`, `files`, `id`, `links`, `metadata`, `modified`, `owners`, `recid`, `revision`, `state`, `stats`, `status`, `submitted`, `swh`, `title`, `updated` |
| metadata keys | `access_right`, `communities`, `contributors`, `creators`, `custom`, `description`, `doi`, `language`, `license`, `publication_date`, `relations`, `resource_type`, `title`, `version` |
| links keys | `access`, `access_grants`, `access_links`, `access_request`, `access_users`, `communities`, `communities-suggestions`, `doi`, `draft`, `file_modification`, `files`, `latest`, `latest_html`, `media_files`, `parent`, `parent_doi`, `parent_doi_html`, `parent_html`, `preview_html`, `quota_increase`, `request_deletion`, `requests`, `reserve_doi`, `self`, `self_doi`, `self_doi_html`, `self_html`, `self_iiif_manifest`, `self_iiif_sequence`, `versions` |
| files container | list |
| files.enabled | absent - the legacy serializer returns `files` as a plain list, not the `{enabled, entries}` object |
| files count | 1 |
| files[0] keys | `checksum`, `id`, `key`, `links`, `size` |

First creator object exactly as returned:

```json
{
  "name": "Habert, Guillaume",
  "affiliation": "ETH Zürich",
  "orcid": "0000-0003-3533-7896"
}
```

### 6.2 Infoscience metadata field presence audit

Presence measured over the 25 datasets returned by the audit probe (configuration `researchoutputs`, `query=dc.type:dataset`).

| Metadata field | Present in | Presence % | Example values |
|---|---|---|---|
| `dc.title` | 25/25 | 100.0 % | Atlas of Regenerative Materials; DCSM Documentation; ArmEpiC – Armenian Epigraphic Corpus (ArtsakhEpiC Sub-Corpus). |
| `dc.contributor.author` | 25/25 | 100.0 % | Habert, Guillaume; Huber, Shoshana; Bengana, Alia |
| `dc.contributor.department` | 0/25 | 0.0 % | - |
| `epfl.unit` | 0/25 | 0.0 % | - |
| `epfl.laboratory` | 0/25 | 0.0 % | - |
| `epfl.department` | 0/25 | 0.0 % | - |
| `cris.virtual.department` | 25/25 | 100.0 % | ENAC-IT; LSMS; #PLACEHOLDER_PARENT_METADATA_VALUE# |
| `cris.virtualsource.department` | 25/25 | 100.0 % | 4df335e1-aac1-4797-912c-817b55177c7a; b2bde37b-b5b3-4e0c-b167-9adc6a9622ec; c82fa577-db9f-4ed3-8084-cd6b6af2d027 |
| `cris.virtual.unitId` | 25/25 | 100.0 % | 10241; 10232; 12632 |
| `cris.virtualsource.unitId` | 25/25 | 100.0 % | 4997bbce-76d4-4f27-be42-926d32669650; eaa245bc-3fce-4c2b-8d67-2e0c6ee10031; 1bb732d2-397c-436a-a982-26d43348ce79 |
| `oairecerif.author.affiliation` | 25/25 | 100.0 % | ETH Zürich; School of Engineering and  Architecture of Fribourg; École Polytechnique Fédérale de Lausanne |
| `dc.date.issued` | 25/25 | 100.0 % | 2026; 2026-08-05; 2026-03-20 |
| `dc.date.accessioned` | 25/25 | 100.0 % | 2026-11-06T06:30:59Z; 2026-10-21T06:15:58Z; 2026-10-14T06:54:17Z |
| `dc.date.available` | 25/25 | 100.0 % | 2026-11-06T06:30:59Z; 2026-10-21T06:15:58Z; 2026-10-14T06:54:17Z |
| `dc.date.created` | 25/25 | 100.0 % | 2026-11-05; 2026-10-20; 2026-10-14 |
| `dc.identifier.doi` | 25/25 | 100.0 % | 10.5281/zenodo.20840785; 10.5281/zenodo.23209677; 10.5281/zenodo.23208654 |
| `dc.identifier.uri` | 25/25 | 100.0 % | https://infoscience.epfl.ch/handle/20.500.14299/269054; https://infoscience.epfl.ch/handle/20.500.14299/268377; https://infoscience.epfl.ch/handle/20.500.14299/268122 |
| `dc.type` | 25/25 | 100.0 % | dataset; dataset::simulation data; dataset::measurement and test data |
| `dc.publisher` | 25/25 | 100.0 % | Zenodo; European Organization for Nuclear Research |
| `dc.description.abstract` | 25/25 | 100.0 % | The Atlas of Regenerative Materials  The Atlas of Regenerative Materials is collecting kno...; Documentation for the DCSM project (Dissemination of Computational Solid Mechanics). It in...; ArmEpiC: Methodology and Data Description   ArmEpiC, Armenian Epigraphic Corpus (ArtsakhEp... |
| `dc.subject` | 6/25 | 24.0 % | Armenian epigraphy; Armenian inscriptions; Epigraphy |
| `dc.language.iso` | 20/25 | 80.0 % | en; xcl |
| `dspace.entity.type` | 25/25 | 100.0 % | Product |
| `epfl.author.orcid` | 25/25 | 100.0 % | 0000-0003-3533-7896; 0009-0003-1354-5026; 0009-0004-4647-7178 |
| `epfl.author.role` | 18/25 | 72.0 % | #PLACEHOLDER_PARENT_METADATA_VALUE#; data collector; researcher |
| `dc.relation.funding` | 16/25 | 64.0 % | ORD Explore Grant; Open Research Data (ORD) Explore grant; Open Educational Resources (OER) grant |

**Key finding - fields that do not exist in this API:** `dc.contributor.department`, `epfl.unit`, `epfl.laboratory`, `epfl.department`. 

The EPFL unit/laboratory of a dataset is exposed as `cris.virtual.department` (label, e.g. `ENAC-IT`) together with `cris.virtual.unitId`; `oairecerif.author.affiliation` carries the author's own affiliation string (often an external institution such as `ETH Zürich`), so it is not a substitute for the EPFL unit.

### 6.3 Infoscience search-result object

| Element | Observed |
|---|---|
| Search-results entry keys | `_embedded`, `_links`, `hitHighlights`, `type`, `uniqueType` |
| `_embedded.indexableObject` keys | `_links`, `discoverable`, `entityType`, `handle`, `id`, `inArchive`, `lastModified`, `metadata`, `name`, `type`, `uniqueType`, `uuid`, `withdrawn` |
| Metadata keys returned | 59 |
| Metadata value shape | every key maps to a list of entry objects `{value, language, authority, confidence, place}` |
| Sample `dc.title` entry | {"value": "Atlas of Regenerative Materials", "language": null, "authority": null, "confidence": -1, "place": 0} |
| Sample `dc.contributor.author` entry | {"value": "Habert, Guillaume", "language": null, "authority": null, "confidence": -1, "place": 0} |
| Sample `dc.date.issued` entry | {"value": "2026", "language": null, "authority": null, "confidence": -1, "place": 0} |
| Sample `cris.virtual.department` entry | {"value": "ENAC-IT", "language": null, "authority": "4602c074-dbba-4f2e-a40d-1c64abf027f2", "confidence": 600, "place": 0} |

The metadata keys are the vocabulary of the CRIS; only the ~15 listed in section 4.2 are populated on every dataset. A field that exists in the vocabulary but not on a record is simply absent from `metadata`, so every read must be defensive.

## 7. Rate limiting, transport & anti-bot behaviour

### 7.1 Zenodo

| Signal | Observed value |
|---|---|
| `X-RateLimit-Limit` | 30 |
| `X-RateLimit-Remaining` | 15 |
| `X-RateLimit-Reset` | unix epoch of window reset (present on every response) |
| `Retry-After` | 60 (advertised on normal 200 responses) |
| Calls made by this probe run | 21 |
| Pacing used | 2200 ms |

**Recommended delay:** 2000 ms between pages for anonymous access (30 requests / 60 s window = 1 request every 2 s). Watch `X-RateLimit-Remaining` and pause when it drops below 5.

### 7.2 Infoscience (the critical finding)

| Layer | Behaviour observed |
|---|---|
| AWS WAF Bot Control | when it fires: HTTP 405 + `x-amzn-waf-action: captcha` for Python/OpenSSL clients on `/server/api/*` and `/search`, while the same URL through `curl.exe` returns HTTP 200 JSON. The block is traffic-scored, not deterministic - see the run counters below |
| CloudFront rate rule | HTTP 429 + `Retry-After: 900` after a burst of ~13 API calls in ~2 minutes |
| `/server/api/*` headers | no `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`; quota is enforced invisibly and only `Retry-After` is exposed after a block |
| Paths that stay open | `/`, `/robots.txt`, `/sitemap_index.xml`, `/sitemap<n>.xml`, `/handle/<handle>` |
| This run, transport used | 16 of 16 probes answered to plain Python `requests`; 0 required the `curl.exe` replay after a WAF challenge |

**Recommended delay:** 6000-7000 ms between paginated requests, and stop immediately on the first 429 (honour `Retry-After`, which is 900 s - one burst costs a 15-minute ban on the whole `/server/api` tree). With `size=100` this still yields ~14 pages per 90 s.

Additional operational notes:

1. `requests` (OpenSSL) can be fingerprinted and challenged - a plain `requests.get()` harvester may receive HTML, not JSON. Keep the `curl.exe` replay (or a browser-like TLS stack) as a fallback.
2. The WAF challenge arrives with HTTP 405/202 plus `x-amzn-waf-action`; check that header before calling `response.json()`.
3. `Retry-After: 900` applies to the whole CloudFront distribution, not to a single URL.

## Appendix A - full probe log

| ID | Service | Purpose | HTTP | Transport | Total hits | Returned | ms | Verdict |
|---|---|---|---|---|---|---|---|---|
| Z-01 | zenodo | Baseline - no query filter (whole EPFL community) | 200 | requests | 512 | 5 | 336 | OK - community scope applied server-side |
| Z-02 | zenodo | Type filter - q=resource_type.type:dataset | 200 | requests | 346 | 5 | 379 | OK - datasets only |
| Z-03 | zenodo | Type filter - q=resource_type.type:software | 200 | requests | 49 | 5 | 939 | OK - software only |
| Z-04/last_90d | zenodo | Window discovery (rolling last 90 days) - resource_type.type:dataset AND created:[...] | 200 | requests | 11 | 5 | 215 | window probe |
| Z-05 | zenodo | Date filter - created:[from TO to] (record creation date) | 200 | requests | 11 | 5 | 204 | Lucene range on system field |
| Z-06 | zenodo | Date filter - metadata.publication_date:[from TO to] | 200 | requests | 9 | 5 | 640 | Lucene range on metadata field |
| Z-07 | zenodo | Date filter - publication_date:[from TO to] (no metadata. prefix) | 200 | requests | 9 | 5 | 254 | syntax comparison |
| Z-08 | zenodo | Date filter alone - created:[from TO to] with no type filter | 200 | requests | 17 | 3 | 257 | syntax comparison |
| Z-09/10 | zenodo | Page size probe - size=10 | 200 | requests | 346 | 10 | 282 | size accepted |
| Z-09/25 | zenodo | Page size probe - size=25 | 200 | requests | 346 | 25 | 1440 | size accepted |
| Z-09/50 | zenodo | Page size probe - size=50 | 400 | requests | - | 0 | 106 | HTTP 400 - rejected |
| Z-09/100 | zenodo | Page size probe - size=100 | 400 | requests | - | 0 | 101 | HTTP 400 - rejected |
| Z-09/200 | zenodo | Page size probe - size=200 | 400 | requests | - | 0 | 107 | HTTP 400 - rejected |
| Z-09/1000 | zenodo | Page size probe - size=1000 | 400 | requests | - | 0 | 111 | HTTP 400 - rejected |
| Z-10/page=0 | zenodo | Page index probe - page=0 | 400 | requests | - | 0 | 306 | HTTP 400 - rejected |
| Z-10/page=1 | zenodo | Page index probe - page=1 | 200 | requests | 346 | 3 | 1017 | page numbering check |
| Z-10/page=2 | zenodo | Page index probe - page=2 | 200 | requests | 346 | 3 | 286 | page numbering check |
| Z-11/bestmatch | zenodo | Sort syntax probe - sort=bestmatch | 200 | requests | 346 | 3 | 286 | sort accepted |
| Z-11/mostrecent | zenodo | Sort syntax probe - sort=mostrecent | 200 | requests | 346 | 3 | 190 | sort accepted |
| Z-11/newest | zenodo | Sort syntax probe - sort=newest | 200 | requests | 346 | 3 | 888 | sort accepted |
| Z-11/oldest | zenodo | Sort syntax probe - sort=oldest | 200 | requests | 346 | 3 | 1438 | sort accepted |
| I-01 | infoscience | Datasets - query=dc.type:dataset (25 objects, also used for the audit) | 200 | requests | 396 | 25 | 1017 | Solr field query accepted |
| I-02 | infoscience | Type filter - query=dc.type:software | 200 | requests | 25 | 1 | 983 | Solr field query accepted |
| I-03 | infoscience | Baseline - configuration=researchoutputs with no query | 200 | requests | 197730 | 1 | 1180 | Baseline count for the researchoutputs configuration |
| I-05/100 | infoscience | Page size probe - size=100 | 200 | requests | 396 | 100 | 1623 | page size cap probe |
| I-05/200 | infoscience | Page size probe - size=200 | 200 | requests | 396 | 100 | 1346 | page size cap probe |
| I-06/page=0 | infoscience | Page index probe - page=0 | 200 | requests | 396 | 2 | 1084 | page numbering check |
| I-06/page=1 | infoscience | Page index probe - page=1 | 200 | requests | 396 | 2 | 1046 | page numbering check |
| I-06b/size=100&page=2 | infoscience | Offset check - size=100&page=2 | 200 | requests | 396 | 100 | 1965 | offset semantics check |
| I-06b/size=200&page=1 | infoscience | Offset check - size=200&page=1 | 200 | requests | 300 | 100 | 1424 | offset semantics check |
| I-07/last_90d | infoscience | Date window probe (rolling last 90 days) - dc.type:dataset AND dc.date.issued:[...] | 200 | requests | 0 | 0 | 1318 | Solr range query on dc.date.issued |
| I-07/last_365d | infoscience | Date window probe (rolling last 365 days) - dc.type:dataset AND dc.date.issued:[...] | 200 | requests | 47 | 2 | 1199 | Solr range query on dc.date.issued |
| I-08 | infoscience | Date window on issued date - dc.type:dataset AND dc.date.issued:[from TO to] | 200 | requests | 47 | 5 | 468 | Solr range query |
| I-09 | infoscience | Date window on accession date - dc.date.accessioned:[from TO to] | 200 | requests | 73 | 3 | 1115 | Solr range query |
| I-10 | infoscience | Field audit - 25 datasets inspected for unit/lab/date/DOI fields | 200 | requests | 47 | 25 | 1457 | Metadata field audit |
| I-11/itemtype | infoscience | Facet filter - f.itemtype=dataset,equals | 400 | requests | - | 0 | 103 | HTTP 400 - rejected |
| I-11/resourceType | infoscience | Facet filter - f.resourceType=dataset,equals | 400 | requests | - | 0 | 130 | HTTP 400 - rejected |

## Appendix B - provenance

The one-off reconnaissance harness that produced this report belonged to the v1 prototype
and has since been removed together with it. Its measurements are superseded by the
constraints enforced in `backend/config.py`; the raw probe output is preserved next to
this file in `recon_raw.json` and `recon_report.json`.
