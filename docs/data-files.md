# Data files

## Purpose

The parts of the tool (the interview, discovery, hard filters, evaluators, the ranked outputs, marks) meet only at the file shapes written down here, so each shape is defined once, frozen at `schema_version` 1 unless a section says otherwise, and drift-tested: `tests/test_data_files_doc.py` parses this file and fails when its vocabularies and field lists disagree with the code. `docs/contracts.md` holds the Posting, the Result envelope, the data-folder layout and the criteria top level; this file holds every other file the data folder gets.

Every path below is relative to the data folder and is named in `paths.LAYOUT` where a short name exists. Every file is UTF-8, written through `fsio`, and only the ranked CSV starts with a byte order mark. Dates are `YYYY-MM-DD`; timestamps are UTC ISO 8601 with `Z`.

## Logic

- The agent writes the files that record what the user said (answers, decisions, proposals). Code validates them and writes everything derived (scan files, ranked files, calibration, evidence, previews).
- Every config change goes through `criteria_store.save_new_version`, which bumps `criteria_version` and keeps the version it replaces in `config/criteria-history/`.
- Counts shown to the user come from code and are written into these files; the agent quotes them and never recounts.
- `Nullable` in the tables says whether the key is always present with a null value (yes) or must hold a real value (no). A key marked optional may be absent.

## Guardrails

- A field is never renamed in place. A shape change needs a `schema_version` bump and an ordered, idempotent migration.
- Unknown keys in a file this document defines are refused by the code that reads it, the same way `Posting.from_dict` refuses them.
- No file here holds a credential. Fixtures and examples are fictional.
- Listings are sorted before use or output; nothing relies on directory order.
- Where this document and the code the earlier plans shipped differ on a field the code already writes, the code's field is documented here and the later plan that owns the writer brings the rest in line.

## QA checklist

- [ ] `uv run --frozen pytest -q tests/test_data_files_doc.py tests/test_contracts.py` passes.
- [ ] The industry, size and stage lists equal `directory.INDUSTRIES`, `SIZES` and `STAGES`.
- [ ] The criterion ids, the evaluator rows and the hard filter types equal the code and the schema.
- [ ] The funnel identities below hold in a ranked file written from a real scan.
- [ ] This file contains no em dash character.

## answers.json

`profile/answers.json`. Written by the agent from the interview; `schema_version` 1.

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `schema_version` | int, always 1 | no | data shape |
| `answered_on` | date | no | the day the interview was finished or last changed |
| `target_titles` | list of str | no (may be empty) | job titles the user named, as said |
| `target_level` | `entry`, `mid`, `senior`, `lead`, `manager`, `director`, `executive` | yes | the level the user is aiming at |
| `years_experience` | int, 0 or more | yes | years in the field |
| `skills` | list of objects `{term, aliases, origin, priority}` | no (may be empty) | `term` str, each term once (compared ignoring case), `aliases` list of str, `origin` is `resume` or `stated`, `priority` an optional bool (absent means false) with at most three true |
| `pay_floor` | object `{amount, currency, interval}` | yes | `amount` number above 0 and at most 1,000,000,000,000, `currency` ISO 4217 code, `interval` is `year`, `month`, `week`, `day` or `hour` |
| `work_modes` | list drawn from `remote`, `hybrid`, `onsite` | no (may be empty) | arrangements the user accepts |
| `locations` | list of str | no (may be empty) | places the user would work, each written as City, ST (City, Country outside the US); a place the tool cannot read is refused |
| `regions` | list of ISO 3166 alpha-2 codes or `EU` | no (may be empty) | countries or the EU the user can work in; a code the tool does not know is refused |
| `needs_sponsorship` | bool | yes | true when the user needs visa sponsorship |
| `industries_wanted` | list drawn from the industry list below | no (may be empty) | industries the user wants |
| `industries_ruled_out` | list drawn from the industry list below | no (may be empty) | industries the user excludes |
| `company_sizes` | list drawn from the size list below | no (may be empty) | sizes the user prefers |
| `company_stages` | list drawn from the stage list below | no (may be empty) | stages the user prefers |
| `deal_breaker_terms` | list of str | no (may be empty) | words that rule a posting out |
| `companies_ruled_out` | list of str | no (may be empty) | companies the user will not apply to |
| `strict` | list drawn from `pay_floor`, `work_mode`, `region`, `sponsorship` | no (may be empty) | filters whose unknown policy is reject |
| `top_three` | list of up to three criterion ids | no (may be empty) | the criteria that matter most |
| `dont_care` | list of criterion ids | no (may be empty) | criteria the user does not care about |
| `skipped` | list of question ids | no (may be empty) | questions the user skipped |
| `first_choice_title` | str or null | yes; the key may be absent | the one target title the user named as their first choice, at most 80 characters. `families.normalize_title` of it must equal that of one of `target_titles`. Absent or null means the question was skipped. Every other key above is required; this one is optional, so a file written before 2026-11-18 stays valid |

## Criterion ids and the importance table

The criterion ids are fixed. Each id has one evaluator.

| Criterion id | Evaluator | Reads |
|--------------|-----------|-------|
| `title_fit` | `title_ladder` | the title, through the compiled title families |
| `skills` | `keyword_coverage` | the skills the user named or the resume showed |
| `level` | `level_match` | title and description seniority |
| `pay` | `banded_number` | the stated pay |
| `location` | `location_match` | work arrangement and places |
| `freshness` | `age_bands` | the posting date |
| `industry` | `term_scale` | the company industry |
| `company_size` | `enum_scale` | the company size |
| `company_stage` | `enum_scale` | the company stage |

Importance is asked on a 1 to 5 scale and turned into points by one fixed table: `IMPORTANCE_POINTS` is `{1: 1, 2: 2, 3: 4, 4: 6, 5: 9}`.

Mapping from the interview:

- A criterion in `top_three` has importance 5.
- A criterion in `dont_care` has importance 1.
- `skills` defaults to importance 5 unless the user places it in `dont_care`; `top_three` also gives 5. It is the default and not a fixed weight, so the user can still lower it.
- Any other answered criterion has importance 3.
- A skipped criterion that has data to evaluate has importance 3. A skipped criterion with nothing to evaluate gets no weight and is left out of the config.
- `freshness` defaults to importance 2 unless the user places it in `top_three` or `dont_care`.

Weights are the largest-remainder rounding of 100 times the criterion's points over the total points of all included criteria. Ties in the remainder go to the lower criterion id (sorted as text). The weights are integers and sum to 100.

### Title families and rungs

`title_fit` scores which title family a role matched, and every family carries a rung from 1 to 3 that code sets, never the agent and never a decisions file. The rule, in `criteria_build.family_rung`:

- Rung 3: the family whose terms include the user's `first_choice_title` (after `normalize_title`). When the question was skipped (absent or null), every family built from a title the user listed (origin `stated`) is rung 3.
- Rung 2: the other stated families when a first choice is named, and a family title discovery found that the user did not list (origin `discovered`) whose `evidence.asking_for_your_skills` is 5 or more.
- Rung 1: a discovered family with `evidence.asking_for_your_skills` below 5 (`THIN_EVIDENCE_POSTINGS`), or with no evidence block.

When every family has the same rung, `title_fit` could only score a constant, so `criteria-build` leaves it out of the criteria and its points are shared by the remaining criteria through the weights above. With two or more distinct rungs it is present. A role whose title is outside every family is still rejected by the title gate, whether or not `title_fit` is present. `title_fit` has two sets of reason wording: one when a first choice is named ("Title matches "{family}", your first choice.") and one when the question was skipped ("Title matches your target title "{family}"."). The matched family is chosen by `families.match_family`: when a title matches more than one family, the family whose matching term has the most words wins, then the higher rung, then the lower id. A regex entry counts as zero words, so a pattern never outranks a term. The title gate only needs some family to match. File order does not matter.

A family may also carry an optional `exclude` list: plain words that mark another function, such as `Legal`, `Security` or `Technical`, which the user approved for that one family. At most 20 words, each 1 to 80 characters, no repeat in any case. A word is escaped and matched as a whole word on the plain lowercased title (so text after " - ", a comma or a parenthesis counts), never as a pattern, and only for the family `match_family` picks for the title: a word on one family never rejects a title another family wins. A word that matches one of the family's own terms is refused, because it would rule out every title in the family. The words are saved sorted, ignoring case. `titles-exclude` writes them (see "Title discovery files"), and `criteria-build` and `titles-apply` keep them across a rebuild.

Existing workspaces: an older config that still carries settings the scorer never read is refused when it is loaded. `criteria-build` then builds a fresh config from the answers and keeps the old file in `config/criteria-history`, so title families approved earlier and tier cutoffs are not carried over.

Existing workspaces: an older `answers.json` has no `first_choice_title` and builds as a skipped question. An older `titles/decisions.json` that carries `rung` keys still applies; the rung is ignored.

## Evaluator params

One row per evaluator. These rows are pinned so the validator, the config builder and the scorer build to the same keys; `tests/fixtures/criteria/minimal.json` stays valid under them.

Common rules:

- Every evaluator returns a `CriterionResult` with `criterion_id`, `label`, `weight`, `points`, `max_points`, `reason`, `gap`, `cap_reason` and `unstated_fields`. `points` is a whole number from 0 to 3, except for `keyword_coverage` with `share` true, where it is an exact fraction from 0 to 3; or the result is a gap (`gap` true, no points counted). The score uses the exact value and rounds once, half-up, to a whole number. `max_points` is 3. `cap_reason` is null except on a `level_match`, `keyword_coverage` or `age_bands` result that holds the role at tier B (see below); it never changes `points`. `unstated_fields` is a list of posting field names (empty by default) that the result's own `reason` sentence says are not stated; only `location_match` sets it today, to `work_mode` when the work mode is unknown but a place was read (the "does not say if it is remote" sentences). The scorer treats those fields as already covered, so a hard filter with the `gap` policy on `work_mode` is not listed a second and third time as "Work arrangement". It never changes `points`, `gap`, the score or the coverage.
- A criterion's optional `reasons` object maps a points value (`"0"` to `"3"`) or `"default"` to a template. When it is absent, the code default is used.
- All listed params keys are required unless marked optional. Any params key not listed for the evaluator is a validation error.
- `field` is the criterion's optional `field` key. Fields available to any `field` key: `title`, `description_text`, `text` (title plus description), `company`, `company_industry`, `company_size`, `company_stage`, `work_mode`, `locations`, `comp_min`, `comp_max`, `posted`.
- Bonuses use the same rows, with `max_points` in place of `weight`.

| Evaluator | `field` | Params keys | Gap when | Placeholders |
|-----------|---------|-------------|----------|--------------|
| `title_ladder` | none | none | never; a title outside every family fails the title gate first | `{family}` |
| `keyword_coverage` | optional: `text` (default), `title`, `description_text` | `must_have` and `nice` (lists of `{term: str, aliases: list of str, weight: int 1 to 5, priority: optional bool}`; `nice` may be empty), `bands` (optional, list of `[share, points]`: share a number 0 to 1, points an int 0 to 3, shares strictly descending, last share 0), `share` (optional bool; exactly one of `bands` or `share` true must be present), `rarity` (optional bool, default false), `cap_no_skill` (optional bool, default false) | the field text is empty, or `must_have` plus `nice` is empty | `{hit}`, `{total}`, `{hits_list}` |
| `age_bands` | none | `bands` (list of `[max_days, points]`: max_days an int at least 0 and strictly ascending, points an int 0 to 3), `else` (int 0 to 3), `cap_after_days` (optional int at least 1), `reject_after_days` (optional int greater than `cap_after_days` when both are set) | `posted` is null | `{days}` |
| `banded_number` | one of `comp_min`, `comp_max` | `bands` (list of `[threshold, points]`: threshold a number, strictly descending, points an int 0 to 3), `currency` (optional ISO 4217 code) | the value is null, or `currency` is set and `comp_currency` differs, or `comp_interval` is not `year` (an interval of null counts as `year` only when the value is at least 10000, the same rule the `min_number` filter uses) | `{value}`, `{currency}` |
| `enum_scale` | one of `company_size`, `company_stage`, `work_mode` | `values` (object mapping a vocabulary value to points, an int 0 to 3) | the field is null or `unknown` | `{value}` |
| `term_scale` | one of `company_industry`, `company`, `title`, `description_text`, `text` | `terms` (object mapping a term to points, an int 0 to 3) | the field is null or empty | `{term}` |
| `level_match` | none | `target` (one of `entry`, `mid`, `senior`, `lead`, `manager`, `director`, `executive`, or null), `years` (int at least 0, or null), `cap_off_level` (optional bool, default false) | `target` and `years` are both null | `{level}`, `{target}` |
| `location_match` | none | `modes` (list drawn from `remote`, `hybrid`, `onsite`), `places` (list of str: the user's locations and region codes, read through `locations.user_place`) | `work_mode` is unknown and the posting has no locations, or a place on either side cannot be read and no readable place matches (a remote role, or a mode the user does not want, is not a gap) | `{mode}`, `{place}` |
| `judged_scale` | none | none | not available | none |

How each evaluator reads its facts:

- `title_ladder` reads the title through `title.families` (compiled by `families.compile_families`). The points are the matched family's `rung`, capped at 3; the matched family is the one `families.match_family` picks (most words in the matching term, then the higher rung, then the lower id).
- `keyword_coverage` sums the weights of the matched terms (a term or any alias, case-insensitive, on word boundaries) over the sum of all weights. With `bands` it takes the points of the first band whose share is at or below that matched share. With `share` true (what criteria-build writes) points = 3 x matched weight / total weight, after rarity, held as an exact fraction, so one real hit earns partial credit (one weight-2 skill out of a total of 13 earns 6/13) and a role naming no skill earns exactly 0. A skill with `priority` true never has a rarity factor below 2, so a common priority skill counts 2 x 2 = 4 and not 2; a priority skill at a share of exactly 1/5 keeps factor 2 and one at 1/20 or less keeps factor 3. criteria-build writes `share` true, `rarity` true, and `priority` true on the skills the user marked. A config with `bands` scores as before. Accepted risk: a higher skills weight makes a false match cost more, so the matching guards below matter more. `{hit}` is the number of terms matched, `{total}` the number of terms, `{hits_list}` the matched terms joined by commas. criteria-build writes weight 2 for a skill the user marked as mattering most and 1 otherwise. With `rarity` true, `score_all` multiplies each term's weight by 3, 2 or 1 by the share of the ranked postings (those with readable text for the field) that name it: 1/20 or less gives 3, 1/5 or less gives 2, more gives 1, and a term no posting names counts once. That is measured before any hard filter, on an internal copy of the config, so the file is never changed and the same scan and config always give the same scores; a role's `weight` in the details stays the criterion weight. criteria-build turns `rarity` on. A priority skill's weight 2 and the factor multiply, so a rare priority skill counts 6 and a common unchosen one counts 1. Matching reads `src/jobsearch/data/skill_words.json`: `variants` extend a term (the term `budgeting` also matches `budget` and `budgets`) and `not_skill` phrases are blanked out of the text before that term is matched, either `any_case` or `exact_case` (the verb in `we excel at logistics` is not the Excel skill, `export to Excel` still is, and `a culture of continuous improvement` is not process improvement). A term with no entry in the file matches as whole words, case-insensitive, as before. For this evaluator a `0` reason template is read only when no term matched; a role with some matches that still earns 0 points reads the `default` template. When `cap_no_skill` is true and the field text is readable but names none of the skills, the result also sets `cap_reason`, which holds the role at tier B, with the clause "none of your 5 key skills appear in the description" ("your key skill does not appear in the description" for one skill). The cap is read from the matched skills, never from the points, so a real hit that a band scores at 0 is never read as no hit. A gap (no text to read, or no skills listed) carries no cap. criteria-build writes `cap_no_skill` false when skills is in `dont_care` and true otherwise; a config with no `cap_no_skill` is never capped for skills.
- `age_bands` measures the age in days from `posted` to the scan date. The first band whose `max_days` is at or above the age gives its points, else `else` gives them. Two optional params turn age into a gate: past `cap_after_days` (age strictly greater, so 60 days is not capped and 61 is) the result sets `cap_reason` "it was posted N days ago", which holds the role at tier B and never changes the points; past `reject_after_days` (strictly greater, so 180 is kept and 181 is rejected) `score_role` rejects the role with `filter_id` `posting_age`, label "Posted more than N days ago" and reason "Posted N days ago, more than N days, so it is likely filled or no longer being filled.", counted in `rejected_by_filters` like any filter failure. Both are fixed words and integers, never posting text. A role with no posted date is never held or rejected for age, and its freshness detail stays a gap. criteria-build writes 60 and 180, also when freshness is in `dont_care`; a config with neither key has no age rule.
- `banded_number` takes the first band whose threshold is at or below the value, else 0.
- `enum_scale` gives 0 for a value the `values` object does not list.
- `term_scale` gives the highest points among the terms found (case-insensitive, on word boundaries), or 0 when none is found.
- `level_match` reads the title and `description_text`, with seniority tokens from `src/jobsearch/data/seniority.json`. When `cap_off_level` is true it also sets `cap_reason`, which holds the role at tier B: a title that names a level other than `target` ("the title reads as lead level and you are aiming for manager"), or a description asking for more than `years` plus 2 ("the description asks for 12 years and you have 9"); both clauses are joined with `; `. A title that names no level word is never capped for level, and the points are unchanged. criteria-build writes `cap_off_level` false when level is in `dont_care` and true otherwise; a config with no `cap_off_level` is never capped for level.
- `seniority.json` has `levels` (title words per level) and `not_level`, a list of lowercase phrases such as `product manager` and `account manager` where "manager" names an individual-contributor job. `title_level_named` blanks those phrases out before reading levels, so "Senior Product Manager" reads as senior and "Product Manager" names no level (mid), while "Engineering Manager" and "Head of Product" still read as manager.
- `skill_words.json` has `schema_version` (1), `variants` (a casefolded term to extra words that also count as the term) and `not_skill` (a casefolded term to `any_case` and `exact_case` phrase lists, and `sentence_start` phrases blanked only at the start of the text or of a sentence or bullet, all blanked before matching). The verb "excel at" is blanked in lowercase and at a sentence start only, so the product in "Advanced Excel in a finance setting" still counts. It holds literal phrases only: each is escaped and matched as text, never read as a pattern, and each starts and ends on a letter or digit.
- `location_match` reads `work_mode` and `locations`.
- `judged_scale` is rejected by `criteria validate` in 1.0, because researched values arrive in 1.1. The row exists so the evaluator list in code and in this file stay equal.

## Hard filter types

The schema's closed list of `hard_filters[].type` values:

| Type | What it checks |
|------|----------------|
| `enum_allow` | the posting's `field` value is in `allow` |
| `min_number` | the posting's number (`field`) is at or above `value`, annualized with year 1, month 12, week 52, day 260, hour 2080; a currency mismatch is unknown |
| `region_allow` | the posting's places (through `locations.parse_locations`) fall in the `allow` list of country codes or `EU`; no parseable place is unknown |
| `deny_terms` | none of `terms` appears in `field` (`title`, `description_text`, `text` or `company_industry`), matched as whole phrases, case-insensitive; a null field is unknown |
| `deny_list` | the posting's company, normalized, is not one of `names` |

Rules:

- Every type returns a `FilterVerdict` with state `pass`, `fail` or `unknown`.
- The unknown policy is `gap`, `pass` or `reject`. It is applied once, in `filters.apply_filters`. A strict filter sets `reject`.
- A `gap`-policy unknown is listed among the role's gaps through `filters.gap_filters`.
- A `reject_reason` template may use `{company}`, `{title}`, `{value}`, `{comp_max}`, `{comp_currency}`, `{work_mode_text}` and `{term}`.
- Before the hard filters, `filters.apply_filters` also rejects a title that carries one of the words its own family rules out (the `exclude` list under "Title families and rungs"). Its `filter_id` is `title_function`, its label is "Function you ruled out", and the reason reads `Title contains "legal", which you ruled out for Operations Manager.` It runs after the title gate and before the hard filters, and it is counted in `rejected_by_filters`. The scan's title gate does not read these words, so the scan keeps the role and a changed word list applies at the next rank with no re-scan.
- After that check, and also before the hard filters, `filters.apply_filters` rejects a role the agent hid after reading its description (a `reject` entry in `roles/function-verdicts.json`). Its `filter_id` is `function_review`, its label is "Other function, read from the description", and the reason reads `Read from the description: Hardware engineering role, not operations.` It is counted in `rejected_by_filters` and in the top rejection reasons.
- Unknown-fact defaults: pay floor `gap`; work mode `gap`; region `gap`; sponsorship `pass` (the filter exists only when `needs_sponsorship` is true); industries ruled out `pass`; deal-breaker terms `pass`.

## scan-scope.json

`config/scan-scope.json`. Written by the code from the interview answers; read by the scan to narrow the starter directory. `schema_version` 1.

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `schema_version` | int, always 1 | no | data shape |
| `criteria_version` | str | no | the config version the scope was written for |
| `industries` | list drawn from the industry list below | no (may be empty) | industries to scan; empty means all |
| `exclude_industries` | list drawn from the industry list below | no (may be empty) | industries never scanned |
| `sizes` | list drawn from the size list below | no (may be empty) | company sizes to scan; empty means all |

## Starter directory (src/jobsearch/data/boards.json)

Entries carry `company`, `ats`, `slug`, `industry`, `size`, `stage`, `last_verified`, `identity` and `postings_at_verify`. `ats` is one of `greenhouse`, `lever`, `lever_eu`, `ashby`. `identity` is `company_name` or `description_mentions`. `last_verified` is a date. `postings_at_verify` is a whole number. Every slug is verified against the live endpoint before it is added.

### Industries

- `software`
- `fintech`
- `healthcare`
- `biotech`
- `education`
- `ecommerce-retail`
- `consumer`
- `media-entertainment`
- `marketing-adtech`
- `logistics-transportation`
- `manufacturing-hardware`
- `energy-climate`
- `security`
- `hr-workplace`
- `real-estate-construction`
- `travel-hospitality`
- `professional-services`
- `nonprofit-public`

### Sizes

- `1-50`
- `51-200`
- `201-1000`
- `1001-5000`
- `5001+`

### Stages

- `early`
- `growth`
- `public`
- `nonprofit`

## scans/<date>/postings.jsonl and scan-meta.json

`scans/<date>/postings.jsonl` holds the title-gate-kept, deduplicated Postings (the Posting shape in `docs/contracts.md`), one per line, sorted by `role_id`.

`scans/<date>/scan-meta.json` is one object:

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `schema_version` | int, always 1 | no | data shape |
| `scan_date` | date | no | the day of the scan |
| `criteria_version` | str | no | the config version the scan used |
| `boards` | list of board objects | no | one per board tried, in directory order |
| `totals` | object | no | the counts below |
| `new_roles` | list of `{role_id, company, title}` | no (may be empty) | roles whose `first_seen` equals the scan date, sorted by company, title, role_id |
| `reopened_roles` | list of `{role_id, company, title}` | no (may be empty) | roles seen before, closed, and open again, sorted the same way |
| `closed_roles` | list of `{role_id, company, title}` | no (may be empty) | roles that were open on the last scan and are gone, sorted the same way |

A board object holds `key` (`ats:slug`), `company`, `ats`, `slug`, `industry`, `size`, `stage`, `status` (`ok`, `no_board` or `failed`), `http_status` (int or null), `from_cache` (bool), `fetched` (int, postings parsed from the board), `kept` (int, postings it contributed to `postings.jsonl`) and `error` (str or null).

`totals` holds:

- `fetched`: every posting parsed from boards with status `ok`, counted before the title gate and the dedupe.
- `title_gate_dropped`: postings whose title failed the family or exclude gate.
- `duplicates_merged`: title-gate-kept postings folded into a lower `role_id` by the dedupe.
- `kept`: lines in `postings.jsonl`.

The identity `fetched = title_gate_dropped + duplicates_merged + kept` always holds. A board with status `failed` never closes its roles.

## roles.jsonl

`roles/roles.jsonl`: one line per role ever seen, sorted by `role_id`. These are the `roles_store.RoleRecord` fields; a line carries no `schema_version` of its own, and a new field always has a default so older lines still load.

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `role_id` | str | no | the Posting `role_id` |
| `board_key` | str | no | `ats:slug` of the board it was seen on |
| `company` | str | no | company name |
| `title` | str | no | title as posted |
| `dedupe_key` | str | yes | company plus normalized title plus location key used by the dedupe |
| `first_seen` | date | no | the first scan that saw it |
| `last_seen` | date | no | the latest scan that saw it open |
| `status` | `open` or `closed` | no | whether the latest scan saw it |
| `closed_on` | date | yes | the scan date it was found gone |
| `duplicates` | list of `role_id` | no (may be empty) | roles merged into this one |

## pasted.jsonl

`roles/pasted.jsonl`: one line per role the user pasted, sorted by the posting's `role_id`. Pasting the same role again replaces the posting and keeps the first `pasted_on`.

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `pasted_on` | date | no | the day it was pasted |
| `posting` | Posting object | no | the normalized posting (`source` is `pasted`) |

## function-worklist.json

`roles/function-worklist.json`: written by `function-review`. It lists every passing role whose shown tier is A or B at the config's current cutoffs and that has no verdict yet, so the agent can read each one and decide whether its work is the kind the user is looking for. The agent reads it and never writes it. It holds posting text, so it stays in the user's data folder. `schema_version` 1. The file holds no timestamp, so the same scan, config and verdict file give the same bytes.

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `schema_version` | int, always 1 | no | data shape |
| `scan_date` | date | no | the scan the roles come from |
| `criteria_version` | str | no | the config version the tiers were read at |
| `roles` | list | no (may be empty) | one item per role, sorted by tier, then score descending, then `role_id` |

Each item in `roles`:

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `role_id` | str | no | the Posting `role_id`, to be copied exactly into a verdict |
| `company` | str | no | company name, cleaned |
| `title` | str | no | title as posted, cleaned |
| `team` | str | yes | the posting's team or department, cleaned |
| `family` | str | yes | the label of the title family the role matched |
| `tier` | `A` or `B` | no | the tier the role is shown in |
| `score` | int | no | the role's score |
| `description` | str | yes | the posting text, cleaned, whitespace collapsed to single spaces, at most 3000 characters; null when the posting has no text |

C roles are not listed: the ranked list collapses them to a count and the top five.

## function-verdicts.json

`roles/function-verdicts.json`: the agent's reading of each listed role. The agent writes it, `function-review` checks it, and `roles_store.load_rankable` applies it, so `rank`, `criteria-test` and `function-review` all see the same verdicts. `schema_version` 1. The shape is `{"schema_version": 1, "verdicts": [{"role_id", "verdict", "reason"}]}`.

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `role_id` | str, 1 to 200 characters | no | the role, exactly as `function-worklist.json` listed it; no two entries share one |
| `verdict` | `reject` or `keep` | no | `reject` hides the role; `keep` records that it was reviewed |
| `reason` | str, 1 to 200 characters after trimming | no | one plain sentence on one line, with no control, format, private-use or surrogate character |

- At most 5000 entries. Any other key, in the file or in an entry, is refused; a `tier`, `score`, `rank`, `points` or `cap` key is refused with the message that a verdict can only hide a role.
- Only `reject` has an effect, and only by rejecting: `filters.apply_filters` rejects the role with `filter_id` `function_review`, label "Other function, read from the description", and the reason `Read from the description: <reason>`. It runs after the `title_function` check and before the hard filters, and it is counted in `rejected_by_filters` and the top rejection reasons. A `keep` changes nothing; it only stops the role from being listed again.
- A verdict can hide a role but never raise one. With any verdict file, every role that stays in the ranked list has exactly the score, tier, tier cap, cap reasons and reason it has with no verdict file.
- A role with a verdict is never listed again. A role with no verdict that is A or B is listed until the agent writes one. Deleting an entry brings the role back.
- A verdict for a role that is not in the latest scan is kept and ignored, because the role may reopen.
- A file with a problem is refused with path-prefixed errors (such as `verdicts[2].reason`) by `function-review`, and `rank` and `criteria-test` stop with a message that names `function-review` as the fix. A refused file hides no role.
- Ranking is a pure function of the scan, the config and this file: the same three give byte-identical ranked files.

## targets.json

`config/targets.json`: companies the user wants scanned. `schema_version` 1. `companies` is a list of objects:

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `name` | str | no | company name as the user said it |
| `ats` | one of the `ats` values | yes | set once the board is known |
| `slug` | str | yes | set once the board is known |
| `industry` | industry value | yes | the user's or the directory's tag |
| `size` | size value | yes | the user's or the directory's tag |
| `confirmed` | bool | yes | true when the user confirmed the board, false when the user rejected it, null when not asked yet |

## boards.local.json

`config/boards.local.json`: boards the user added or confirmed. It is rebuilt from `config/targets.json` on every run and removed when that file is gone. `schema_version` 1. `boards` is an object keyed by the normalized company name; each value is:

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `company` | str | no | company name |
| `status` | `resolved`, `needs_confirmation`, `unresolved` or `rejected` | no | only `resolved` boards are scanned |
| `ats` | one of the `ats` values | yes | the board's system |
| `slug` | str | yes | the board's slug |
| `identity` | `company_name` or `description_mentions` | yes | how the board was matched to the company |
| `checked_on` | date | yes | when the board was last verified |
| `reason` | str | yes | why a board is `unresolved` or `rejected` |
| `industry` | industry value | yes | optional tag the scan reads when present |
| `size` | size value | yes | optional tag the scan reads when present |

## Title discovery files

All under `titles/`.

`titles/sample-<date>.jsonl`: one line per sampled posting.

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `role_id` | str | no | the posting |
| `company` | str | no | company name |
| `industry` | industry value | yes | the company's industry |
| `size` | size value | yes | the company's size |
| `title` | str | no | title as posted |
| `normalized_title` | str | no | `families.normalize_title` of the title |
| `skills_hit` | list of str | no (may be empty) | the user's skill terms found in the description, matched with scoring's `skill_mentioned` on NFKC text (whole words, any case, `skill_words.json` variants, not-a-skill phrases such as "excel at" blanked) |

`titles/evidence-<date>.json`: written by code. Fields: `sample_date`, `boards_sampled`, `postings_sampled`, `industries_covered` (list of industry values), `coverage_note` (str, the stated sampling bias), `skills_used` (list of str) and `titles` (list). Each title entry holds `normalized`, `n_postings`, `n_with_skills`, `examples` (up to 3 raw titles) and `top_skills` (list of str). The first line holds every field but `titles`, then each title entry sits on its own line as compact JSON, and `]}` closes the file, so a long file can be read in parts.

`titles/proposal.json`: written by the agent. `families` is a list of at most 8 objects, each with `id` (`tf-` followed by a slug), `label`, `titles` (list of normalized titles it groups) and `terms` (list of str).

`titles/proposal-view.md`: rendered by code from the evidence and the proposal. Each family line carries its count as text such as "31 of 412 sampled postings".

`titles/decisions.json`: written by the agent from the user's answers. `decisions` is a list of objects with `id` (a proposal family id), `decision` (`approve`, `reject` or `edit`), `label` (str) and `terms` (list of str). The tool sets each family's rung itself by the rule under "Title families and rungs"; a `rung` key in an existing file is ignored, and `titles-apply` says so in its warnings.

`titles/exclusions.json`: written by the agent after the user approves, edits or rejects each family's words. Shape: `{"families": [{"id": "<family id>", "exclude": ["word", ...]}]}`. A family the file does not name keeps its words; an entry with an empty list clears them. The `titles-exclude` command reads it, validates it, and saves the words sorted into `title.families[].exclude` as a new config version (none when nothing changed). It refuses and writes nothing for an unknown family id (the message lists the valid ids with their labels), a stray key, a word that is not text or is empty, a word over 80 characters, more than 20 words, a repeated word, or a word that matches one of the family's own terms. It changes only the `exclude` lists.

Rules code enforces on the two agent-written files (`titles-check` and `titles-apply` refuse the file and name the JSON path):

- The proposal holds only `families`, and each family only `id`, `label`, `titles` and `terms`. Any count, share, `n_postings` or `examples` key is refused, because every number the user sees is computed by code. At most 8 families; ids match `tf-[a-z0-9-]+`, are unique, and never start with `tf-s-` (that prefix belongs to the families built from the titles the user listed). Every title must appear in the evidence and belong to one family. Terms are at most 80 characters.
- `decisions` needs exactly one decision for every proposed family. An `approve` keeps the proposal as written; an `edit` needs a new `label` and a non-empty `terms` list. A `reject` drops the family, including an earlier approved family with the same id.
- Two kept discovered or newly approved families may not share a term (compared by words, ignoring case, hyphens and slashes).
- `titles-apply` writes each approved or edited family into `config/criteria.json` as `{id, label, rung, terms, regex: [], origin, evidence, approved_at}`. `origin` is `stated` when the family covers a title the user listed and `discovered` otherwise. `evidence` is `{sample_date, postings_sampled, asking_for_your_skills, share (3 places), top_shared_skills}`. Each rung comes from the rule under "Title families and rungs". All families are written in rung-descending then id order. `titles-apply` rebuilds the whole config through the same builder as `criteria-build` (`build_criteria`, with the approved families carried in), so `title_fit` and the weights follow the full family set and a `criteria-build` run right after it makes no new version. A family set that ends up empty is refused, and a decision set that changes nothing makes no new config version.
- `criteria-build` regenerates only the `tf-s-<slug>` families from the answers. Every family `titles-apply` wrote (origin `discovered`, or `stated` for a listed title the user edited) has an id outside that prefix and is carried across a later `criteria-build`, unless a regenerated family now uses the same id. Both also keep each family's `exclude` words across a rebuild; a regenerated `tf-s-` family takes its words from the previous family with the same id, and a word that an edit makes match the family's new terms is dropped with a warning.

## calibration.json

`config/calibration.json`: the tier cutoffs proposed from the first scan and what the user accepted. `schema_version` 1.

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `schema_version` | int, always 1 | no | data shape |
| `criteria_version` | str | no | the config version the accepted cutoffs were saved into (the new version `calibrate` wrote), so `rank` does not warn right after `calibrate` |
| `scan_date` | date | no | the scan it was computed from |
| `passing_roles` | int | no | roles that passed the hard filters |
| `proposed` | object `{a_min, b_min, counts}` | no | `a_min` and `b_min` are integers; `counts` is `{A, B, C}` at those cutoffs |
| `accepted` | object `{a_min, b_min}` | yes | the cutoffs written into the config |

## Ranked outputs

`rank` writes five files for one scan: `ranked/ranked-<date>.md`, `.json` and `.csv`, `ranked/funnel-<date>.md` (see "Summary files") and `ranked/rejected-<date>.json`. One writer, `output.write_ranked`, produces all five from one scored list, so they cannot disagree.

### Funnel

The funnel explains where every role went. Each count has one source:

| Funnel key | Source |
|------------|--------|
| `scanned` | `RankInput.fetched_total`, which is `totals.fetched` in `scan-meta.json`, counted before the title gate and the dedupe |
| `pasted` | `RankInput.pasted_total`, every record in `roles/pasted.jsonl` |
| `duplicates_merged` | `RankInput.duplicates_merged`: `totals.duplicates_merged` from `scan-meta.json` plus every pasted record dropped because it duplicates a scanned role, or an earlier pasted record, by `role_id` or dedupe key |
| `title_gate_dropped` | `RankInput.title_gate_dropped` |
| `rejected_by_filters` | counted from the scored list; a pasted title outside every family counts here with `filter_id` `title_family`, a role posted more than `reject_after_days` before the scan counts here with `filter_id` `posting_age`, a title carrying a word its family rules out counts here with `filter_id` `title_function`, and a role the agent hid after reading its description counts here with `filter_id` `function_review` |
| `passed` | counted from the scored list |
| `tiers` | object `{A, B, C}`, counted from the passed roles |

Two identities hold in every ranked file:

- `scanned + pasted = duplicates_merged + title_gate_dropped + rejected_by_filters + passed`
- `A + B + C = passed`

### ranked-<date>.json

`ranked/ranked-<date>.json`. `schema_version` 1.

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | int, always 1 | data shape |
| `criteria_version` | str | the config version that produced the file |
| `scan_date` | date | the scan it ranks |
| `funnel` | object | `scanned`, `pasted`, `duplicates_merged`, `title_gate_dropped`, `rejected_by_filters`, `passed`, `tiers` |
| `rejections` | list of `{filter_id, label, count}` | hard-filter rejections by filter, sorted by count descending then `filter_id` |
| `closed_since_last` | list of `{role_id, company, title}` | roles closed since the last scan, sorted by company, title, `role_id` |
| `roles` | list of role objects | every passing role, sorted by score descending then `role_id` |

A role object holds `role_id`, `tier`, `score`, `coverage`, `provisional`, `tier_cap`, `cap_reasons`, `company`, `title`, `locations`, `url_apply`, `first_seen`, `last_seen`, `source`, `reason_text`, `gaps`, `shown` and `more_at_company`. There is no per-role `details` list: its reasons are already in `reason_text`, so the file the skills read stays small. `tier` is the shown tier: the tier of `score`, lowered to `tier_cap` when the cap ranks below it. `tier_cap` is `"B"` or null and `cap_reasons` is the list of plain-English reasons (empty when there is no cap); a cap never changes `score`. `shown` is false for a role the Markdown file does not print in full: a C role beyond the top 5, or a lower-ranked role at a company where `one_per_company` keeps the best (best means the highest shown tier first, then the highest score, then the newer posting, a role with a posted date before one without, then the higher `coverage`, then `title` and `role_id`, so a role held at B never hides an open A role and a tie never goes to the alphabetically first title). `more_at_company` is the number of other passing roles at the company of a shown role, in any tier, so the Markdown says "Best of `more_at_company` + 1 roles", and 0 otherwise. A role named on an "Also tier A here" line in the Markdown has `shown` false and `more_at_company` 0, and is still counted in `tiers`.

These are the `scoring.ScoredRole` fields and where each goes:

| `ScoredRole` field | In the ranked file |
|--------------------|--------------------|
| `role_id`, `company`, `title`, `locations`, `url_apply`, `first_seen`, `last_seen` | the same key on the role object |
| `source` | the same key on the role object |
| `score`, `coverage`, `provisional`, `gaps`, `reason_text` | the same key on the role object. `reason_text` is the `reason_lines` joined by single spaces in their fixed order, each as `<label>: <text>` ending in a period (`Why it fits: ... Where and how: ... Pay: ... Skills: ... Level: ... Posted: ... Company: ...`); the CSV `reason` column is the same string. `gaps` keeps its meaning: the labels of what could not be checked |
| `reason_lines` | the labeled lines of the `.md` block, in order, as `(label, text)` pairs; not written as its own JSON key, because `reason_text` is the same lines joined (see "The fixed reason lines" below) |
| `tier_cap`, `cap_reasons` | the same key on the role object; `tier` is the capped tier. A role is capped at `B` when it is provisional (clause "it was scored on only N percent of your criteria", first) or when its `level_match`, `keyword_coverage` or `age_bands` result has a `cap_reason`, with those clauses after the provisional clause in criterion id order |
| `details` | a list of `CriterionResult`; stays in memory for the Markdown reason lines and is not written to the JSON, because its reasons are already in `reason_text` |
| `family_id`, `family_label` | used for the title reason and the preview; not written to the file |
| `posted` | the posting's posted date (or null), copied for passing and rejected roles; used only to break a tie between two roles at one company, the newer posting winning; not written as its own key |
| `rejected` | a `FilterFailure` (`filter_id`, `label`, `reason`); the role goes to `ranked/rejected-<date>.json` instead of `roles` |
| `bonus_points` | included in `score`; not written separately |

### rejected-<date>.json

`ranked/rejected-<date>.json`. `schema_version` 1. Keys: `schema_version`, `scan_date` and `rejected`, a list of `{role_id, company, title, filter_id, reason}` with one entry for every role a hard filter removed, sorted by `role_id`, text cleaned as in the ranked JSON. Every `rank` writes it, with an empty list when nothing was rejected, so a re-rank never leaves an old list behind. No skill reads it. It answers "why isn't job X listed?". Its name does not start with `ranked-` on purpose: the code that finds the newest ranked file matches `ranked-*.json`.

### ranked-<date>.csv

`ranked/ranked-<date>.csv`: one row per passing role, in the same order as `roles`, written as UTF-8 with a leading byte order mark (so Excel on Windows reads accented names correctly; open it with `utf-8-sig`) and LF line endings. The `.md` and `.json` files have no byte order mark. Columns, in order: `tier`, `score`, `coverage`, `provisional`, `company`, `title`, `locations`, `last_seen`, `url_apply`, `reason`, `gaps`, `role_id`, `criteria_version`. `locations` and `gaps` are joined with `; `.

### ranked-<date>.md

The Markdown file is for reading. Rules:

- Every A and B block prints the seven fixed lines of "The fixed reason lines" below right after its heading (Why it fits first, then Where and how, Pay, Skills, Level, Posted and Company, with skills and level each on their own line), then the Tier line, so the reason comes before the Apply link. A missing fact reads `not stated` in its line and is said once; there is no `Gaps:` line and no "Could not check" sentence.
- A and B roles are shown in full. C roles are collapsed to a count line and the top 5. The count is every role in the tier, as the funnel counts it, and the number after `Top` is the number of roles listed under the line, so `2 roles. Top 2:` when there are only two. With `one_per_company` on, when some roles are hidden because their company already has a role in the list, the line says so: `3 roles, 2 not shown because their company is already listed. Top 1:`. With nothing hidden that way the line is `8 roles. Top 5:`. When every role in a tier is hidden that way, the line ends after the reason and nothing is listed under it: `3 roles, 3 not shown because their company is already listed.` (`1 role, 1 not shown because its company is already listed.` for one). That is the only time an A or B section prints a count line, and `None this time.` appears only under an A or B heading with no roles at all.
- A provisional role carries a visible provisional marker, with its missing facts shown as `not stated` in their lines, and is held at tier B at most. A role whose tier a cap lowered shows one `Held at tier B: ...` line after its Tier line, saying why: it was provisional, it was off the user's level, it named none of the user's skills, or it was posted more than `cap_after_days` before the scan ("it was posted N days ago"). A role posted more than `reject_after_days` before the scan is not shown at all: it is in `rejected-<date>.json` with `filter_id` `posting_age` and in the top rejection reasons when it is among the top three. `provisional_below_coverage` is 60 when criteria-build writes it.
- With `one_per_company` on, one role per company is shown, with the line "Best of N roles at <company> for you" when the company has more than one passing role (N counts every passing role there, in any tier), followed by "Also tier A here: <title>, score <S>. Apply: <link>" for each other tier A role there, so no tier A role is hidden. The role shown is the one in the highest shown tier, then the highest score, then the newer posting (a dated role before an undated one), then coverage, title and `role_id`. A company with one passing role has neither line, and with `one_per_company` off every role is shown in full with neither line.
- A one-line funnel follows the tiers in the form `412 scanned, 120 passed filters, 9 A, 22 B, 89 C`. When pasted roles exist, `, 2 pasted` is inserted after the scanned count: `412 scanned, 2 pasted, 120 passed filters, 9 A, 22 B, 89 C`.
- The top three hard-filter rejection reasons follow, each with its count.
- When roles closed since the last scan, the line `N roles closed since your last scan` is shown, and it lists them. It is shown only when N is above zero.
- A `## Send to career-ops` block follows the closed roles. It lists every tier A role, each shown role and then its "Also tier A here" roles, in the order the A section prints them, as one career-ops pipeline line per role inside a fenced `text` block: `- [ ] <apply link> | <company> | <title>`. That is the shape of a Pending line in career-ops' `modes/pipeline.md` ("Format of pipeline.md": `- [ ] {url} | {company} | {title}`, with optional location and compensation columns that this file leaves out), checked against the live file on 2026-11-22. The block is text the user copies; nothing in this tool reads, writes or contacts career-ops. Sanitizing: company and title go through the same hidden-character clean as every other text, backticks are removed, whitespace collapses to one line, and a pipe becomes a slash, so a posting cannot add a column, forge a second line or close the fence. A role whose apply link is not a plain https URL (the rule that makes an Apply link), or whose link holds a pipe or a backtick, is left out, and the block says "N tier A roles have no usable apply link and are left out." (singular: "1 tier A role has no usable apply link and is left out."). With no tier A role the block says "No tier A roles this time." and prints no fence. B and C roles are never listed.
- A `## Next steps` block closes the file with two lines: write a cover letter with `/job-search-prioritizer:letter`, and mark whether a ranking was right with `/job-search-prioritizer:review`.
- The two blocks only restate the ranking: no score, tier, cap, count or order changes because of them.
- The file holds no wall-clock timestamp, so the same inputs give the same bytes.

### The fixed reason lines

Every A and B entry in `ranked-<date>.md` prints these seven lines after its heading and before its Tier line, always, in this order. The slot of a criterion comes from its evaluator, and its field where it matters, never from its id or its points, so a hand-edited config lands in the same slots.

| Order | Label | Criteria that feed it |
|-------|-------|-----------------------|
| 1 | `Why it fits` | `title_ladder`, then `term_scale` on `title`, `description_text` or `text`, then any evaluator not listed below (criterion id order); then the reasons of bonuses that earned points |
| 2 | `Where and how` | `location_match`; `enum_scale` on `work_mode` |
| 3 | `Pay` | `banded_number` |
| 4 | `Skills` | `keyword_coverage` |
| 5 | `Level` | `level_match` (target level and years of experience) |
| 6 | `Posted` | `age_bands` |
| 7 | `Company` | `term_scale` on `company_industry` or `company`; `enum_scale` on `company_size` or `company_stage` (criterion id order) |

Text rules:

- An evaluable criterion contributes its `reason`, unchanged. Skills and level evidence each have their own line and never sit inside `Why it fits`.
- A gapped criterion reads `not stated` when it is the only entry of its slot, and `<criterion label>: not stated.` when the slot has other entries.
- A slot that no criterion of the config feeds reads `not one of your criteria`. The exception is `Why it fits`: when no criterion feeds it (`criteria-build` leaves `title_fit` out when every family has the same rung), it reads `Title matches your "<family label>" family.` from the title family the role matched, followed by any bonus reasons that earned points. Only a role with no matched family and no bonus reads `not one of your criteria` there.
- A missing fact is said once. A hard filter whose `unknown` policy is `gap` and whose posting field no criterion already covers is printed in the first slot (in the order above) whose criteria read one of its fields, as `<filter label>: not stated.`. A filter gap that no slot reads goes on one trailing line `Also not stated: <labels joined with ", ">.`, printed only when there is one. A filter gap on a field a gapped criterion covers, or that a result lists in `unstated_fields`, is not listed again.
- `reason_text` and the CSV `reason` join these lines (the `Also not stated` line last, when present) by single spaces as `<label>: <text>`, adding a period where the text does not end in one. The one-line C entries carry that joined string.
- The Markdown labels are fixed words and every text part is escaped to one line, so posting text cannot change a label or start a new line. Scores, coverage, tiers, caps, `gaps` and the order of roles do not depend on any of this.

### ranked/preview-<date>.md

Written by `criteria-test` before a change is saved. It shows the funnel, the top three rejection reasons, the tier mix at the current cutoffs and at the proposed cutoffs, and up to three sample titles per family.

## Summary files

Three files restate, in plain words, what the tool decided, so a skill quotes a file instead of paraphrasing and the user can read it again later. Code renders them from saved state (`src/jobsearch/summaries.py`); no skill writes them. None holds a wall-clock time, so the same inputs give the same bytes, and every value that came from the user, a resume or a posting is escaped as literal text. Each is written atomically as UTF-8 with LF line endings, `criteria-build` and `titles-apply` write theirs only when the rendered bytes differ from the file on disk and list it in `wrote` only then, and `rank` rewrites the funnel file every time, with the same bytes for the same inputs. They restate decisions already made: no score, tier, cap, count or order depends on them.

### profile/summary.md

Layout name `intake_summary`, file `profile/summary.md`. Written by `criteria-build` after every successful build, never after a refused one (invalid answers write nothing). It starts with `# What the tool understood` and the day the answers were given (`answered_on`), then one labeled line per topic in the `answers.json` field order: job titles with the first choice marked, level, years of experience, skills (each marked `from your resume` or `you added`, and `matters most` for a priority skill), pay floor with currency and period, work arrangements, places, regions, visa sponsorship, industries wanted, industries ruled out, company sizes, company stages, deal-breaker words, companies the user will not apply to, which filters are treated as strict, what matters most and what does not (the criterion ids in plain words). A topic the user skipped reads `skipped` in its own line, an empty answer reads `none given`, and a missing value reads `not stated`; no skipped topic is listed twice. It ends with `Scoring config version <v>.` and the line "If anything here is wrong, tell the start skill what to change and it rebuilds your config; the previous version is kept."

### titles/summary-<date>.md

Written by `titles-apply`, named for the `sample_date` of the evidence it used. It starts with `# Job titles chosen` and one line giving the sample (date, postings, boards) and the evidence's own `coverage_note`. Then `## Kept`: every title family of the saved config, in config order, as `- <label> (<why>)`, where `<why>` is `you listed this title` or `found in the sample`, plus `your first choice` for the family at rung 3 when the user named a first choice. A family with an evidence block states its count as `N of M sampled postings ask for your skills` (the block's `asking_for_your_skills` and `postings_sampled`, never recounted), then `Skills they share:` from `top_shared_skills`; a family with `exclude` words adds `Words that rule a title out:`. Then `## Dropped`: the label of every family the user rejected (the proposal's label, else the decision's), or `None.`. It ends by saying the start skill can rework the choice and the previous config version is kept. The file is written when the apply saved a new config version and its bytes differ from the file on disk. An apply that changes nothing only refreshes a summary that already exists and never creates one, so `wrote` stays empty then.

### ranked/funnel-<date>.md

Written by `write_ranked`, so `rank` lists `ranked/funnel-<scan date>.md` beside the other ranked files. It starts with `# What the scan found` and the scan date and config version. `## Where the roles came from`: `Boards read` (status `ok` in `scan-meta.json`), `Boards that failed` by company name with `could not be reached` (status `failed`) or `was not found` (status `no_board`), or `none`; a scan with no boards says `No boards were read in this scan.`; then `Postings fetched`, `Postings you pasted` and `Duplicates merged`. `## What was filtered out`: `Title outside your approved families` (the title gate count), every hard-filter rejection by its label with its count (all of them, sorted by count then label), and `Hidden after reading the description` (the `function_review` rejections, counted there and not in the list above). `## What is left`: `Passed your filters` and the A, B and C counts. It ends with the one-line funnel, `output.funnel_line` verbatim, the same text the ranked file shows. The counts are the ones in "Funnel" above, so the identity `scanned + pasted = duplicates_merged + title_gate_dropped + rejected_by_filters + passed` holds across the file's lines.

## marks.jsonl

`feedback/marks.jsonl`: the user's right or wrong marks. Append-only; the latest line per `role_id` is the effective mark.

| Field | Type | Nullable | Meaning |
|-------|------|----------|---------|
| `marked_at` | UTC timestamp | no | when the mark was made |
| `role_id` | str | no | the role |
| `verdict` | `right` or `wrong` | no | the user's call on the ranking |
| `note` | str, at most 1000 characters | yes | the user's own words |
| `tier_at_time` | `A`, `B` or `C` | no | the tier the role had |
| `score_at_time` | int | no | the score the role had |
| `criteria_version` | str | no | the config version in use |
| `ranked_file` | str | no | the ranked file the mark was made against |

## Letter files (applications/<role key>/)

One folder per ranked role holds everything for that role's cover letter. `letter-start` creates it; `letter-check` fills it in. The folder name is built in code, never by a skill, from the role id: lowercase it, turn every run of characters outside a-z, 0-9, period, underscore and hyphen into one hyphen, strip leading and trailing hyphens and periods, cut to 60 characters, then add a hyphen and the first 8 hex characters of the sha256 of the role id (UTF-8). For `greenhouse:acme-robotics:7000001` that is `greenhouse-acme-robotics-7000001-<8 hex>`. The result has no colon, so it is valid on Windows, and two role ids that read alike never share a folder.

| File | Written by | Shape |
|------|------------|-------|
| `role.json` | `letter-start` | JSON object: `schema_version` (1), `role_id`, `title`, `company`, `url_apply`, `tier`, `score`, `scan_date`, `criteria_version`, all copied from the newest `ranked/ranked-<date>.json`. Rewritten on every `letter-start`. |
| `posting.txt` | `letter-start` | The posting's description text, cleaned of hidden characters, at most 20000 characters. Empty when the posting is not in the latest scan or the pasted roles, unless an earlier `letter-start` saved its text: that text is kept, because a closed posting cannot be fetched again. |
| `answers.md` | the letter skill | The user's answers in their own words. Each question is a `## ` heading line followed by the answer. The `## Greeting` and `## Sign-off` sections hold the opening and closing the user picked, such as a sign-off line and the name, phone and email from `profile/resume.md`. |
| `draft.md` | the letter skill | The draft: the user's answers and samples cut, reordered and tightened, with at most 20% of the body's words changed. |
| `fixes.json` | the letter skill, only when the user approved a typo fix | `{"schema_version": 1, "fixes": [{"from": "teh", "to": "the"}]}`. Optional. |
| `check.md` | `letter-check` | The report, rewritten on every check: how much of the letter body is not the user's words and every sentence with a changed word, lines that may sound machine-written, lines that match the posting, and whether `letter.md` was saved. No timestamps, so the same inputs give the same bytes. |
| `letter.md` | `letter-check` | The saved letter: the draft with LF line endings, trailing spaces trimmed, and one final newline. Written only when at most 20% of the body's words are not from the user's answers or samples. |
| `letter-v<N>.md` | `letter-check` | An earlier `letter.md` that a changed draft replaced. N starts at 1 and rises by one each time. |

`letter-start` never changes `answers.md`, `draft.md` or `fixes.json`. A refused check writes `check.md` only, and the saved `letter.md` stays as it was.

Rules `letter-check` applies:

- The body is every draft line except a line that matches, compared without case and with white space collapsed, a line of at most 8 words under `## Greeting` or `## Sign-off` in `answers.md`. Those are the picked opening and closing, so they are left out of the count and are never a source. A longer line under those headings is neither, so copying it into the draft counts as changed words.
- Each body line is split into sentences at `.`, `!` or `?` followed by a space, and each sentence into words. A word is a run of letters and digits; an apostrophe or hyphen between two of them is part of the word, so `follow-up` is one word. Words are compared without case, and curly and straight quotes match each other.
- A word is the user's when it sits in a run of 3 or more words in a row that appears, in the same order, inside one sentence of a source. Any other word is changed. So reordering sentences, joining two of the user's sentences and cutting words cost nothing, and an added or swapped word costs one. A user's word that is not part of any such run, such as one beside a change at the start or end of a sentence, counts as changed too.
- A sentence of fewer than 3 words is the user's only when it is a whole sentence of a source. Otherwise all its words are changed.
- A run that drops a negation does not count. When the source sentence holds `not`, `no`, `never`, `without`, `cannot` or a word ending in `n't` before the run, that match is skipped, so "Lead the team." counts 3 changed words against "I did not lead the team."
- The share is the changed words divided by the body's words, rounded up to a whole percent. `letter.md` is saved when the share is 20% or less. `check.md` gives the counts and the share, then lists every sentence with a changed word, most changed first, as `Line N` with its count.
- The sources are `answers.md` with every Markdown heading line removed (one to six `#` and then a space, indent allowed; the questions are never the user's words) and without its `## Greeting` and `## Sign-off` sections, and every file in `profile/samples`. Answers and samples are equal members of one list.
- A typo fix in `fixes.json` makes one corrected word count as the user's. Each fix is one word to one word, an edit distance of 1 or 2 apart, the `from` word must appear as a whole word in a source, there are at most 50 fixes, and no other keys are allowed. A file that breaks any rule is refused by name and nothing is written. The draft is never changed; the fix is applied to the sources when matching.
- Caps, checked before the file is read: `draft.md` 40000 bytes, `answers.md` 200000 bytes, `posting.txt` 100000 bytes, `fixes.json` 20000 bytes. A file that resolves outside the data folder is refused. An empty draft is refused with a plain sentence and nothing is written.
- The tone gate advises and never blocks the save. It names each hit by line and kind: em dash (U+2014, U+2015, a spaced double hyphen, or U+2013 with a space on both sides), stock phrase (from `src/jobsearch/data/tone_phrases.json`; the longest wins when two overlap), colon-preface setup, and contrast framing. Hits are sorted by line, then column, then kind.
- The posting check advises and never blocks the save. A draft line that shares a run of 8 or more words with `posting.txt` (compared without case or punctuation) is listed as matching the posting, so posting sentences cannot pass as the user's words unseen. When the posting text is empty, `check.md` says it was not checked.

## Writing samples (profile/samples/)

`profile/samples/` holds the user's earlier writing: a cover letter they wrote, a note, an email. Every `.md` and `.txt` file directly in the folder counts as the user's own words for every letter, the same as `answers.md`. Other suffixes and subfolders are ignored.

| Rule | Value |
|------|-------|
| Suffixes read | `.md` and `.txt` |
| Files at most | 20 (more is refused with a plain sentence) |
| Each file at most | 100000 bytes |
| Encoding | UTF-8, a leading byte order mark allowed |
| Read order | sorted by file name |

The folder is created by the user or the letter skill, never by `init`. A sample is read, never changed.

## settings.json

`config/settings.json` is the user's own switch for the once-a-day release check. The user changes it by asking Claude to turn update checks off or on, which runs `update-check --turn-off` or `update-check --turn-on`. They can also edit it by hand.

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | integer | Always 1. |
| `update_check` | boolean | `true` checks once a day; `false` makes no request and shows no note. |

The file does not exist until the user first uses a switch, and an absent file means checks are on. A damaged file (not JSON, not an object, or `update_check` not true or false) also means checks are on, with a warning naming the file. Running either switch command rewrites it cleanly. Nothing in it describes the user.

## notices.json

`config/notices.json` is code-owned state for the two short notes. The user never needs to open it.

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | integer | Always 1. |
| `update_checked_on` | string or null | The UTC date (`YYYY-MM-DD`) of the last release check, or null. |
| `update_off_hint_shown` | boolean | True once a notice has told the user how to turn the notes off. Later notices leave that sentence out. |
| `feedback_invite_shown_on` | string or null | The UTC date the win invite was shown, or null. |

Rules:

- The release check makes at most one request a day. The day is written to `update_checked_on` BEFORE the request, so an offline, refused, rate-limited or failed check is silent and still counts as that day's check. A stamp that cannot be saved means no request is made.
- The only address contacted is `https://api.github.com/repos/adamNBurke/job-search-prioritizer/releases/latest`. It has no query string, and only the fixed User-Agent and Accept headers go with it. Nothing about the user is sent.
- A notice appears only when the latest release is strictly newer than the installed version. Versions compare as three whole numbers (1.10.0 is newer than 1.9.0). A tag that is not three whole numbers, with an optional leading `v`, gives no notice.
- The first notice ever adds one sentence on how to turn the notes off and sets `update_off_hint_shown`.
- The win invite is shown once ever. After `rank` writes the ranked files it returns the invite in `data.notice` only when `feedback_invite_shown_on` is null; it writes the date first (so `config/notices.json` is in that run's `wrote`), then returns the one line (the Discussions Wins link and the feedback skill). Every later rank returns an empty notice. It makes no network call.
- A damaged file (not JSON, not an object, a different `schema_version`, or a field of the wrong type) is replaced by a fresh file whose `feedback_invite_shown_on` is today, with one warning naming the file. The win invite therefore never repeats after damage.
- Both files are written atomically through `fsio`, as UTF-8 with LF line endings.
