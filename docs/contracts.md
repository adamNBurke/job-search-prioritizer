# File contracts

## Purpose

The shapes below are frozen at schema version 1 and drift-tested: `tests/test_contracts.py` parses this file and fails when it disagrees with `src/jobsearch/models.py`, `src/jobsearch/paths.py` or `src/jobsearch/schemas/criteria.schema.json`.

The Posting field sources below come from the live responses recorded on 2026-11-12 in `docs/ats-live-vs-docs.md`. Where the vendor docs differ, the live shape wins.

## Data-folder resolution

- The folder is chosen by the user. There is no default location.
- A command takes `--data-dir`. Without it, the nearest ancestor of the current directory that holds `jobsearch.workspace.json` is used.
- `init` is the only command that creates a folder, and it refuses any folder inside the Claude plugin cache, the plugin data directory, or `~/.claude/plugins`, because an update or uninstall deletes those.
- `init` writes into a folder only if it does not exist, is empty, or already holds the marker.

## Posting

One normalized open role, one JSON object per line in `scans/<date>/postings.jsonl`. Field order is part of the contract. `Nullable` says whether the value may be null; the three source columns say where an adapter reads it (live key names).

| Field | Type | Nullable | Greenhouse | Lever | Ashby |
|-------|------|----------|------------|-------|-------|
| `schema_version` | int, always 1 | no | n/a | n/a | n/a |
| `role_id` | string | no | made by `make_role_id` | made by `make_role_id` | made by `make_role_id` |
| `source` | `ats` or `pasted` | no | `ats` | `ats` | `ats` |
| `ats` | `greenhouse`, `lever`, `lever_eu`, `ashby` | yes (null when pasted) | `greenhouse` | `lever` or `lever_eu` by host | `ashby` |
| `board_slug` | string | yes (null when pasted) | board token | site | board name |
| `posting_id` | string | no | `id` (integer, stored as a string) | `id` | `id` |
| `company` | string | no | `company_name`, else the directory entry | directory entry | directory entry |
| `title` | string | no | `title` | `text` | `title` |
| `team` | string | yes | `departments[0].name` | `categories.team` (optional key) | `team` |
| `locations` | list of strings | no (may be empty) | `location.name` | `categories.location` plus `categories.allLocations[]` | `location` plus `secondaryLocations[].location` |
| `work_mode_text` | string | yes | none (null) | `workplaceType` as sent | `workplaceType` as sent |
| `work_mode` | `remote`, `hybrid`, `onsite`, `unknown` | no | `unknown` unless the location text says remote | `workplaceType`: `onsite` to onsite, `remote`, `hybrid`, `unspecified` to unknown | `workplaceType` lowercased; `isRemote` alone is not enough because it is true for hybrid too |
| `posted` | UTC ISO 8601 with Z | yes | `first_published`, offset converted to UTC per value | `createdAt` (epoch milliseconds) to UTC | `publishedAt` to UTC |
| `url_apply` | string | no | `absolute_url` | `applyUrl` | `applyUrl` |
| `url_posting` | string | yes | `absolute_url` | `hostedUrl` | `jobUrl` |
| `description_text` | string | no (may be empty) | `content`, unescaped, tags stripped, entities decoded a second time | `descriptionPlain` plus `lists[]` plus `additionalPlain` | `descriptionPlain` |
| `comp_min` | float | yes | `pay_input_ranges[].min_cents` divided by 100, else parsed from the `pay-range` div | `salaryRange.min` (not cents) | Salary component `minValue` (not cents) |
| `comp_max` | float | yes | `pay_input_ranges[].max_cents` divided by 100, else parsed | `salaryRange.max` | Salary component `maxValue` |
| `comp_currency` | string | yes | `currency_type`, else parsed | `salaryRange.currency` | Salary component `currencyCode` |
| `comp_interval` | `year`, `month`, `week`, `day`, `hour`, `other` | yes | not stated by Greenhouse; null unless parsed | `salaryRange.interval` (`per-year-salary` to year) | component `interval` (`1 YEAR` to year; `NONE` to null) |
| `comp_source` | `structured` or `parsed_html` | yes (null when no pay) | `structured` for `pay_input_ranges`, `parsed_html` for the div | `structured` | `structured` |
| `fetched_at` | UTC ISO 8601 with Z | no | fetch time | fetch time | fetch time |

Rules that follow from the live canaries:

- Greenhouse `pay_input_ranges` exists only on a single job fetched with `?pay_transparency=true`. The list never has it, so list scans carry `parsed_html` pay. When a posting lists several ranges in the same currency, the adapter keeps the lowest minimum and the highest maximum and adds a warning. Ranges in a currency other than the first one stated are left out, with a warning that names how many and which currencies (Greenhouse and Ashby).
- Lever `salaryRange` and Ashby `compensation` are structured. Ashby `compensation` is per job, not at the top level of the response.
- `parsed_html` pay is lower trust than `structured` pay. The scorer may treat it differently, which is why the source is kept.
- Adapters read list-item keys by name with a default. Greenhouse items carry an occasional extra key, and Lever `categories` keys are optional.
- A 404 is handled by status before any JSON decode. Ashby's 404 body is plain text.
- A Posting never holds the raw response body. Unknown keys are refused by `Posting.from_dict`.

## role_id

- Format: `ats:board_slug:posting_id`, exactly three parts separated by `:`. Example: `lever_eu:acme-robotics:abc`.
- No part may be empty or contain `:`.
- Lever US and EU boards can share a slug with different data, which is why `lever_eu` is its own `ats` value and part of the id.
- A pasted posting gets `pasted:-:` followed by the first 12 lowercase hex characters of the SHA-256 of the case-folded, whitespace-normalized URL or text. Pasting the same thing twice gives the same id.
- The id is stable across runs, which makes every step that keys on it safe to repeat.

## Result

Every command prints exactly one Result as JSON on stdout. The first column below is the key list, in order.

| Field | Type | Meaning |
|-------|------|---------|
| `ok` | bool | true when the command did what was asked |
| `command` | string | the subcommand name |
| `data` | object | command-specific values |
| `counts` | object of ints | numbers worth showing, such as postings |
| `wrote` | list of strings | data-folder-relative paths written, sorted |
| `warnings` | list of strings | plain-English soft problems |
| `next` | string or null | the one thing the user or skill should do next |
| `error` | string or null | plain-English reason when `ok` is false |

## Exit codes

- 0: the command succeeded.
- 1: a handled failure. A Result with `ok` false and an `error` was printed.
- 2: a usage error (missing or unknown argument).

## Stdout rules

- ASCII-only JSON (`ensure_ascii=True`), sorted keys and compact separators, printed on one line. Every key in the Result table is printed, even when it is empty or null.
- Bulk data goes into files, never stdout.
- Files in the data folder are UTF-8, written through `fsio` (temp file, fsync, `os.replace`). The ranked CSV is written with a leading byte order mark and LF line endings (see `docs/data-files.md`) and read with `utf-8-sig`; every other file has no byte order mark.

## Data-folder layout

Short name, then the path relative to the data folder (POSIX separators). `init` creates only the marker, `.gitignore` and `cache/http`; the commands that need the rest create it when first needed. The shape of every file named in this table is in `docs/data-files.md`.

| Name | Path |
|------|------|
| `marker` | `jobsearch.workspace.json` |
| `gitignore` | `.gitignore` |
| `profile` | `profile` |
| `criteria` | `config/criteria.json` |
| `criteria_history` | `config/criteria-history` |
| `cache_http` | `cache/http` |
| `scans` | `scans` |
| `roles` | `roles` |
| `ranked` | `ranked` |
| `applications` | `applications` |
| `feedback` | `feedback` |
| `answers` | `profile/answers.json` |
| `resume_md` | `profile/resume.md` |
| `profile_source` | `profile/source` |
| `scan_scope` | `config/scan-scope.json` |
| `targets` | `config/targets.json` |
| `boards_local` | `config/boards.local.json` |
| `calibration` | `config/calibration.json` |
| `roles_file` | `roles/roles.jsonl` |
| `pasted` | `roles/pasted.jsonl` |
| `titles` | `titles` |
| `marks` | `feedback/marks.jsonl` |
| `function_worklist` | `roles/function-worklist.json` |
| `function_verdicts` | `roles/function-verdicts.json` |
| `writing_samples` | `profile/samples` |
| `settings` | `config/settings.json` |
| `notices` | `config/notices.json` |
| `intake_summary` | `profile/summary.md` |

The marker `jobsearch.workspace.json` is a JSON object with `created_at` (UTC ISO 8601), `created_with_plugin_version`, `schema_version` and `workspace_schema` (both 1). The `.gitignore` holds the single line `*`, so an enclosing git repository never tracks the folder.

## Versioning

- `schema_version` (integer, in every JSON file and the marker): the data shape. A newer file than the code understands stops the command and tells the user to update the plugin. Older files are migrated by ordered, idempotent steps (built when the first shape change needs one).
- `criteria_version` (string such as `1.0`): the user-facing scoring version. It changes only when the user approves a change, and every ranked output records the one it used.
- The plugin version lives in `.claude-plugin/plugin.json` only.

## criteria.json

The scoring configuration at `config/criteria.json`. The full shape is `src/jobsearch/schemas/criteria.schema.json` (JSON Schema draft 2020-12, `schema_version` 1). A fictional example is `tests/fixtures/criteria/minimal.json`. `criteria-validate` checks a config in code; no schema-validation library is a dependency.

Required top-level keys:

- schema_version
- criteria_version
- created_from
- hard_filters
- title
- criteria
- bonuses
- tiers
- banding
- one_per_company

Rules the schema and the validator enforce together: the top level is closed (`reserved` is the only optional key and 1.0 ignores it); evaluators come from a closed registry in code (`title_ladder`, `keyword_coverage`, `age_bands`, `banded_number`, `enum_scale`, `term_scale`, `judged_scale`, `level_match`, `location_match`); criterion weights are integers that sum to 100 (bonuses use `max_points` instead); tier ids are A, B and C with strictly descending `min_score`.

## Guardrails

- These shapes change only with a `schema_version` bump and a migration. A field is never renamed in place.
- No file in the data folder holds a credential, and nothing here is ever sent to a job board; boards only receive anonymous GETs.
- The fixtures and examples contain fictional data only.
- Filenames and folder names may be non-ASCII. Compare names after NFC normalization, and compare file contents as exact UTF-8 bytes.
- Never rely on directory listing order. Sort every listing before use or output.

## QA checklist

- [ ] `uv run --frozen pytest -q tests/test_contracts.py` passes.
- [ ] The Posting table lists 22 fields in dataclass order.
- [ ] The layout table equals `paths.LAYOUT`.
- [ ] The required-keys list equals the `required` array in the schema.
- [ ] `tests/fixtures/criteria/minimal.json` has weights summing to 100 and tiers A, B, C.
- [ ] A live-versus-docs change in `docs/ats-live-vs-docs.md` is reflected in the source columns above in the same commit.
