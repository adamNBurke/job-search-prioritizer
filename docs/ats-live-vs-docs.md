# Live ATS responses versus the vendor docs

Purpose: this file is the written list of every place a live public job-board response differs from what the vendor docs or the earlier research say. It was produced from a fresh run of anonymous, read-only GET requests on 2026-11-12 (captured_at below), made through the project's single fetch layer. Where this run differs from earlier notes, the fresh value is stated and supersedes them. The Posting contract, the fetch hardening and the adapters normalize from these shapes.

Boards used: Greenhouse, a public company board with 260 postings; Lever `leverdemo` on the US host (10 postings) and on the EU host (324 postings), both vendor demo data; Ashby, the vendor's own board `Ashby` (75 postings). Hacker News was read through its public Firebase API. Nothing else was contacted, nothing was sent but GET, and no credentials or cookies were used.

The fixtures under `tests/fixtures/ats/` are sanitized from these responses: key sets, nulls and value types are kept exactly, and every company name, id, host, person and description text is replaced with fictional or synthetic data. `tests/test_ats_fixtures.py` compares every fixture to the keysets block at the end of this file, so this file cannot drift from the fixtures. The opt-in `tests/test_live_canary.py` (marker `live`) compares the live boards to the same block, so it also flags vendor drift.

Request count for this run: 12 GETs (10 job-board, 2 Hacker News), paced at 1 second or more per host.

## Findings that change what later plans build

- Greenhouse list items are not uniform. One of 260 postings carries an extra key `education`. An adapter must treat list-item keys as open-ended and read fields by name, never by position or fixed key set.
- Greenhouse `?content=true` is what makes a list item rich: without it the list drops `content`, `departments`, `offices` and the three `ai_*` keys, and keeps `first_published`, `metadata` and `data_compliance`. `board_url` already adds `content=true`.
- Greenhouse `pay_input_ranges` exists only on a single job fetched with `?pay_transparency=true`. The same single job without the parameter has no such key. The list never carries it.
- The `fetch` result keeps only the `content-type` header, live and from the cache. `etag`, `last-modified`, cache-control, content-encoding and `age` are visible on the wire but are dropped before the caller sees them, because the fetcher never sends a conditional request (it re-fetches after the cache TTL). `etag` and `last-modified` were stored until 2026-11-23 and removed as unread.
- Greenhouse sends `content-encoding: gzip`; the 4 MB list with content is about 4,047,606 bytes after decoding. The response size cap must be set against decoded size, and must leave room above 4.1 MB for the Greenhouse list and 4.6 MB for the Ashby list (4,530,476 bytes decoded), or a normal board will be refused.
- Only Ashby's 404 is not JSON. `decode_json` on that body raises, so a 404 must be handled by status before any JSON decode.
- No response carried a rate-limit header (`x-ratelimit*`, `retry-after`). That is an observation of 12 responses, not a statement that no limit exists. The 1 second per host pacing remains the control.

## Greenhouse

Docs: https://docs.greenhouse.io/job-board.html (board API at boards-api.greenhouse.io).

| Topic | Vendor docs say | Live result |
|-------|-----------------|-------------|
| List with `content=true`, job keys | `first_published` and `data_compliance` only on the single-job response | Present in the list too. Common keys: absolute_url, ai_disclaimer, ai_opt_out_request_url, application_deadline, company_name, content, data_compliance, departments, first_published, id, include_ai_disclaimer, internal_job_id, language, location, metadata, offices, requisition_id, title, updated_at. Top level is `jobs` plus `meta` (`meta.total` equals the list length, 260). |
| Optional list key | not mentioned | `education` (a string such as `education_required`) on 1 of 260 postings. Treat the key set as open. |
| List without `content=true` | content is optional | Same 260 postings, 183,135 bytes. Keys drop to absolute_url, application_deadline, company_name, data_compliance, first_published, id, internal_job_id, language, location, metadata, requisition_id, title, updated_at (plus `education` where present). `content`, `departments`, `offices` and the three `ai_*` keys are gone. `first_published` is still present. |
| Escaped HTML | `content` is HTML-escaped | Entity-escaped once: the raw string starts `&lt;div class=&quot;content-intro&quot;&gt;`. One `html.unescape` gives real HTML (tags seen: a, br, code, div, em, h1, h2, h3, h4, hr, li, ol, p, span, strong, u, ul). After that one unescape, all 260 still contain `&amp;` and `&nbsp;`, and 112 contain `&mdash;`. The HTML-to-text step must decode entities a second time. |
| Date fields | not specified | ISO 8601 with a local UTC offset. `updated_at` carried `-05:00` on all 260. `first_published` carried `-04:00` on 209 and `-05:00` on 51, so offsets vary within a board and must be converted to UTC per value. |
| Compensation in the list | `pay_input_ranges` documented on the single job | No `pay_input_ranges` key in any of 260 list items. Pay appears as text inside `content`: `<div class="title">United States Salary Range</div><div class="pay-range"><span>$139,200</span><span class="divider">&mdash;</span><span>$235,200 USD</span></div>`, inside a wrapper with the classes `content-pay-transparency` and `pay-input`. 112 of 260 postings have exactly one pay-range div; none had two or more in this run (a multi-range posting was not observed; the Greenhouse list and job fixtures add a synthetic second range to exercise it, and the description section below says so). |
| Compensation on a single job with `?pay_transparency=true` | `pay_input_ranges` with min_cents, max_cents, currency_type, title, blurb | Confirmed. The chosen job returned one range with `min_cents` and `max_cents` as integers in CENTS (13920000 means 139,200.00), `currency_type` `USD`, a `title` and a `blurb` of raw HTML containing `&nbsp;`. |
| Single job without the parameter | docs inconsistent (the Retrieve example shows the key without the parameter) | Settled: no `pay_input_ranges` key at all without the parameter. The other 19 keys are identical to the with-parameter call. |
| Single job versus list item | n/a | Same keys as a list item with `content=true`, plus `pay_input_ranges` when asked. `content` is byte-identical to the list's for the same id. |
| Metadata | n/a | `metadata` is always a list. Items are `{id: int, name: str, value: str, value_type: "single_select"}`. |
| Departments and offices | n/a | `departments` always had one entry with keys child_ids, id, name, parent_id. `offices` had 0, 1 or 2 entries (57, 174 and 25 postings) with keys child_ids, id, location, name, parent_id. `location` is null or a string, and `parent_id` is an integer or null on both departments and offices (child_ids is a list of integers). |
| Null fields | n/a | `application_deadline`, `ai_disclaimer`, `include_ai_disclaimer` and `ai_opt_out_request_url` were null on every posting. `data_compliance` was a non-empty list on every posting. |
| Apply URL host | `boards.greenhouse.io` in older docs | `absolute_url` host is `job-boards.greenhouse.io` on all 260. |
| 404 | undocumented | HTTP 404, `content-type: application/json`, 38 bytes, exact body `{"status":404,"error":"Job not found"}`. This is returned for a nonexistent BOARD on the list URL too, so the message says "Job" even though the board is what is missing. |
| Size, caching, encoding | n/a | List with content: 4,047,606 bytes decoded, `content-encoding: gzip`, `etag: W/"..."`, `cache-control: max-age=0, private, must-revalidate`, `vary: Origin, Accept-Encoding`, `via` a CDN. 404: `cache-control: no-cache`, no etag. No rate-limit headers. |
| Language | n/a | `language` was `en` on all 260. |

## Lever (US and EU)

Docs: https://github.com/lever/postings-api. The US host is `api.lever.co`; the EU host is `api.eu.lever.co`. A `leverdemo` site exists on both and holds different data on each.

| Topic | Vendor docs say | Live result |
|-------|-----------------|-------------|
| Top level | a JSON array of postings | A bare top-level array: 10 postings on the US host, 324 on the EU host. |
| Posting keys | see README | additional, additionalPlain, applyUrl, categories, country, createdAt, description, descriptionBody, descriptionBodyPlain, descriptionPlain, hostedUrl, id, lists, opening, openingPlain, text, workplaceType, plus `salaryRange` only when set. |
| Optional keys | `salaryRange` and `salaryDescription` when set | `salaryRange` appeared on 4 of 10 US postings and on 0 of 324 EU postings. `salaryDescription` and `salaryDescriptionPlain` appeared on none, so do not rely on them. |
| `salaryRange` shape and vocabulary | `{currency, interval, min, max}` | Confirmed: min and max are integers (not cents), currency `USD`, interval `per-year-salary` on all 4. The interval vocabulary is Lever's own and differs from Ashby's `1 YEAR`. |
| `workplaceType` values | Earlier notes: `on-site`, `remote`, `hybrid`, `unspecified` | Live values: `onsite` (no hyphen), `remote`, `hybrid`, `unspecified`. US: 3 hybrid, 3 onsite, 3 remote, 1 unspecified. EU: all 324 `unspecified`. This supersedes the earlier notes. |
| Escaped HTML | `description` is HTML | Not escaped. `description`, `descriptionBody`, `opening` and `additional` are raw HTML. `lists[]` items are `{text, content}` where `content` is `<li>` fragments. The Plain variants are text. Two EU postings contain a literal `&lt;` inside real HTML (escaped angle brackets in running text), so a text step must still decode entities. |
| Date fields | not documented | `createdAt` is an integer in epoch MILLISECONDS (13 digits) on every posting. Posting dates range from 2013 to 2020 on the EU demo board and 2017 to 2026 on the US one. |
| `country` | documented field | Key always present. US values: US (8), GB (1), CA (1). EU: US 146, null 109, GB 42, CA 12, NL 8, FR 3, and others. Null is a real value, not a missing key. |
| `categories` | location, team, department, commitment, allLocations | `allLocations` is always a list. The `categories` keys other than `allLocations` are optional. Postings missing a key: EU (of 324) location 93, commitment 59, team 41, department 53; US (of 10) location 0, commitment 3, team 1, department 2. Read categories by name with a default. |
| Compensation placement | `salaryRange` top-level on the posting | Confirmed: `salaryRange` sits on the posting object itself, not under `categories`. |
| Apply URL hosts | `hostedUrl`, `applyUrl` | US: both on `jobs.lever.co`. EU: both on `jobs.eu.lever.co`. |
| 404 | undocumented | HTTP 404, `content-type: application/json; charset=utf-8`, 41 bytes, exact body `{"ok":false,"error":"Document not found"}`, with an `etag` header. |
| Size, caching | n/a | US list 83,523 bytes, EU list 1,539,759 bytes (both without `content-encoding` in this run). Headers: `content-type: application/json; charset=utf-8` and `etag: W/"..."` only. No cache-control and no rate-limit headers. |

## Ashby

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api (endpoint at api.ashbyhq.com).

| Topic | Vendor docs say | Live result |
|-------|-----------------|-------------|
| Top level | `jobs` array | Object with `jobs` (array, 75) and `apiVersion`, which is the string `"1"` (earlier notes had it as the number 1). |
| Job keys | see docs | address, applyUrl, compensation, department, descriptionHtml, descriptionPlain, employmentType, id, isListed, isRemote, jobUrl, location, publishedAt, secondaryLocations, shouldDisplayCompensationOnJobPostings, team, title, workplaceType. All 75 had every key. |
| Compensation placement | the docs table says per job, the docs example shows top level | Settled: PER JOB. All 75 jobs have a `compensation` object with keys compensationTierSummary, compensationTiers, scrapeableCompensationSalarySummary, summaryComponents (requested with `includeCompensation=true`). The top level never carries it. |
| Compensation tiers | `compensationTiers[]` | Each tier has additionalInformation (null), components, id, tierSummary, title. Each component has compensationType, currencyCode, id, interval, maxValue, minValue, summary. `compensationType` values seen: Salary, EquityPercentage, Commission, Bonus. `interval` strings are `"1 YEAR"` and `"NONE"`. `minValue` and `maxValue` are plain numbers (not cents) on Salary components and null on the other types. `currencyCode` is null on equity components. |
| `summaryComponents[]` | docs list it | Items have compensationType, interval, maxValue and minValue; `currencyCode` is present on Salary items and on some Bonus, Commission and equity items and absent on others (an equity item may also carry `interval` `"1 YEAR"` instead of `"NONE"`). There is no `id` or `summary` (unlike tier components). |
| Escaped HTML | `descriptionHtml` | Not escaped. Raw HTML with inline `style` attributes on all 75 (`<p style="min-height:1.5em">`). 28 contain a literal `&lt;` inside real HTML. `descriptionPlain` is text. |
| Date fields | `publishedAt` | ISO 8601 with milliseconds and `+00:00`, for example `2024-03-04T14:29:08.532+00:00`, same pattern on all 75. The oldest of the 75 dates from 2024-03-04 and is still listed, so age is not a filter for openness. |
| Remote and workplace | `isRemote` | `isRemote` true on all 75 and `workplaceType` the string `Remote` on all 75 (capitalized, unlike Lever). Also `isListed` (true on all), `shouldDisplayCompensationOnJobPostings` (true on all), `address.postalAddress` (an object with between 1 and 4 of postalCode, addressRegion, addressCountry and addressLocality), and `secondaryLocations[]` of `{location, address}`. |
| Apply URL hosts | `jobUrl`, `applyUrl` | Both on `jobs.ashbyhq.com`. |
| 404 | undocumented | HTTP 404, `content-type: text/plain; charset=utf-8`, 9 bytes, exact body `Not Found`. NOT JSON. |
| Size, caching | n/a | 4,530,476 bytes decoded, `content-encoding: gzip`, `cache-control: public, max-age=60, stale-while-revalidate=60`, `etag: W/"job-board:..."`, an `age` header, behind a CDN. The 404 has no cache-control and no etag. No rate-limit headers. |

## Description text and the leak gate

Description bodies in live responses contain real people's first-person text, benefits links and LinkedIn links (LinkedIn appeared in 12 of 260 Greenhouse postings, 45 of 75 Ashby postings and 4 of 324 EU Lever postings; none in the 10 US Lever postings). None of that is copied into the repo. Fixtures keep only the structural quirks: the Greenhouse single-escaped entity form with `&nbsp;` and `&mdash;` and the pay-range block, Lever `lists[].content` fragments, and Ashby inline `style` attributes with `"1 YEAR"` intervals. The Greenhouse multi-range pay block (two pay-range blocks in a posting, two `pay_input_ranges` entries on the job) is synthetic because no live posting had one. Greenhouse apostrophes in `content` arrive as `&#39;` after the one level of escaping, and the fixtures keep that form.

## Observed for 1.1 (not a v1 source)

Hacker News "Ask HN: Who is hiring?" is read through the official Firebase API (`https://hacker-news.firebaseio.com/v0/`), which needs no authentication. It is context only for 1.1 and no v1 code reads it.

- `user/whoishiring.json` returns an object with `about` (string), `created` (int), `id` (string), `karma` (int) and `submitted` (list of integer item ids, newest first; 553 ids in this run).
- The newest "Ask HN: Who is hiring? (Month YYYY)" item is a story with keys by, descendants, id, kids, score, text, time, title, type. `type` is `story`, `kids` is a list of integer comment ids (282 in this run), and `time` is epoch seconds.
- Both responses were `application/json; charset=utf-8` with `cache-control: no-cache`. No rate-limit headers.
- Only the story item was fetched, not its comments. Comment bodies are free text and a 1.1 source would need its own review before any fixture is made.

## Machine-readable keysets

`captured_at` is the UTC date of the run. Per fixture stem, `top` is the sorted top-level key list (null for a top-level array or a plain-text body), `item` is the sorted key list of the superset posting (null when the fixture is not a list of postings), and `optional` lists the item keys that are absent on some postings. For the three 404 fixtures `status`, `content_type` and `body` give the exact observed response. `observed_only` holds the two Greenhouse calls that have no committed fixture. `nested` holds key sets one level down that the Posting contract depends on.

<!-- keysets:begin -->
```json
{
  "ashby_404": {
    "body": "Not Found",
    "content_type": "text/plain; charset=utf-8",
    "item": null,
    "optional": [],
    "status": 404,
    "top": null
  },
  "ashby_list": {
    "item": [
      "address",
      "applyUrl",
      "compensation",
      "department",
      "descriptionHtml",
      "descriptionPlain",
      "employmentType",
      "id",
      "isListed",
      "isRemote",
      "jobUrl",
      "location",
      "publishedAt",
      "secondaryLocations",
      "shouldDisplayCompensationOnJobPostings",
      "team",
      "title",
      "workplaceType"
    ],
    "optional": [],
    "top": [
      "apiVersion",
      "jobs"
    ]
  },
  "captured_at": "2026-11-12",
  "greenhouse_404": {
    "body": "{\"status\":404,\"error\":\"Job not found\"}",
    "content_type": "application/json",
    "item": null,
    "optional": [],
    "status": 404,
    "top": [
      "error",
      "status"
    ]
  },
  "greenhouse_job": {
    "item": null,
    "optional": [],
    "top": [
      "absolute_url",
      "ai_disclaimer",
      "ai_opt_out_request_url",
      "application_deadline",
      "company_name",
      "content",
      "data_compliance",
      "departments",
      "first_published",
      "id",
      "include_ai_disclaimer",
      "internal_job_id",
      "language",
      "location",
      "metadata",
      "offices",
      "pay_input_ranges",
      "requisition_id",
      "title",
      "updated_at"
    ]
  },
  "greenhouse_list": {
    "item": [
      "absolute_url",
      "ai_disclaimer",
      "ai_opt_out_request_url",
      "application_deadline",
      "company_name",
      "content",
      "data_compliance",
      "departments",
      "education",
      "first_published",
      "id",
      "include_ai_disclaimer",
      "internal_job_id",
      "language",
      "location",
      "metadata",
      "offices",
      "requisition_id",
      "title",
      "updated_at"
    ],
    "optional": [
      "education"
    ],
    "top": [
      "jobs",
      "meta"
    ]
  },
  "lever_404": {
    "body": "{\"ok\":false,\"error\":\"Document not found\"}",
    "content_type": "application/json; charset=utf-8",
    "item": null,
    "optional": [],
    "status": 404,
    "top": [
      "error",
      "ok"
    ]
  },
  "lever_eu_list": {
    "item": [
      "additional",
      "additionalPlain",
      "applyUrl",
      "categories",
      "country",
      "createdAt",
      "description",
      "descriptionBody",
      "descriptionBodyPlain",
      "descriptionPlain",
      "hostedUrl",
      "id",
      "lists",
      "opening",
      "openingPlain",
      "text",
      "workplaceType"
    ],
    "optional": [],
    "top": null
  },
  "lever_us_list": {
    "item": [
      "additional",
      "additionalPlain",
      "applyUrl",
      "categories",
      "country",
      "createdAt",
      "description",
      "descriptionBody",
      "descriptionBodyPlain",
      "descriptionPlain",
      "hostedUrl",
      "id",
      "lists",
      "opening",
      "openingPlain",
      "salaryRange",
      "text",
      "workplaceType"
    ],
    "optional": [
      "salaryRange"
    ],
    "top": null
  },
  "nested": {
    "ashby_list.compensation": [
      "compensationTierSummary",
      "compensationTiers",
      "scrapeableCompensationSalarySummary",
      "summaryComponents"
    ],
    "ashby_list.compensation.compensationTiers[]": [
      "additionalInformation",
      "components",
      "id",
      "tierSummary",
      "title"
    ],
    "ashby_list.compensation.compensationTiers[].components[]": [
      "compensationType",
      "currencyCode",
      "id",
      "interval",
      "maxValue",
      "minValue",
      "summary"
    ],
    "greenhouse_job.pay_input_ranges[]": [
      "blurb",
      "currency_type",
      "max_cents",
      "min_cents",
      "title"
    ],
    "greenhouse_list.location": [
      "name"
    ],
    "lever_us_list.salaryRange": [
      "currency",
      "interval",
      "max",
      "min"
    ]
  },
  "observed_only": {
    "greenhouse_job_without_pay_transparency": [
      "absolute_url",
      "ai_disclaimer",
      "ai_opt_out_request_url",
      "application_deadline",
      "company_name",
      "content",
      "data_compliance",
      "departments",
      "first_published",
      "id",
      "include_ai_disclaimer",
      "internal_job_id",
      "language",
      "location",
      "metadata",
      "offices",
      "requisition_id",
      "title",
      "updated_at"
    ],
    "greenhouse_list_without_content_param": [
      "absolute_url",
      "application_deadline",
      "company_name",
      "data_compliance",
      "education",
      "first_published",
      "id",
      "internal_job_id",
      "language",
      "location",
      "metadata",
      "requisition_id",
      "title",
      "updated_at"
    ]
  }
}
```
<!-- keysets:end -->
