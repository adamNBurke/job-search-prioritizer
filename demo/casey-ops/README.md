# A full run for Casey, a made-up operations manager

This folder is one complete run of [job-search-prioritizer](../../README.md), so you can judge what it does without
installing it.

Casey is fictional. So are the three job boards and every posting in this run (Northwind Example,
Harbor Example and Meridian Example) and the two employers on Casey's resume (Brightwater Example and
Kestrel Example). No real person's or company's data is in this folder.

Each step below says what the file shows and who wrote it:

- **Casey** answered the interview questions.
- **The agent** is Claude, following the [start skill](../../skills/start/SKILL.md). It groups titles and reads descriptions.
- **The code** is the tool's own Python. Every count, score, tier and reason comes from the code.

## 1. The interview

Folder: [1-intake](1-intake)

- [resume.md](1-intake/resume.md) is Casey's resume, read in by the code.
- [answers.json](1-intake/answers.json) holds Casey's answers: target titles, skills, pay floor and work
  arrangement. Casey wrote these in the interview.
- [summary.md](1-intake/summary.md) is the code's plain-English readback of those answers, as written
  by the first build (version 1.0). Casey skipped the first-choice title question, so it reads
  "first choice: skipped".
- [criteria.json](1-intake/criteria.json) is the scoring setup the code built from the answers, in its
  final form after the title families and tier cutoffs below were saved into it.

## 2. Which titles your skills really fit

Folder: [2-titles](2-titles)

The tool read 23 postings from three boards and counted which job titles ask for Casey's skills.

- [proposal-view.md](2-titles/proposal-view.md) shows each family of titles with its count, such as
  "6 of 23 sampled postings ask for your skills". The agent grouped the titles
  ([titles-proposal.json](inputs/titles-proposal.json)); the code computed every number.
- [titles-decisions.json](inputs/titles-decisions.json) is what Casey decided: keep Operations leadership
  and Program delivery (plus the three titles Casey listed: Operations Manager, Business Operations
  Manager and Program Manager), and drop Data and analytics, Product and UX design and Recruiting and
  writing.
- [summary.md](2-titles/summary.md) is the code's record of what was kept.

## 3. The ranked list

Folder: [3-ranked](3-ranked)

- [ranked.md](3-ranked/ranked.md) is the list. Every open role that passed Casey's filters is in tier A,
  B or C, with the reason first and a link to apply. The tiers come from the code, and the cutoffs are the
  ones the code proposed from this scan, which Casey accepted.
- [funnel.md](3-ranked/funnel.md) shows where every posting went: scanned, merged as a duplicate,
  dropped for the wrong title, rejected by a filter, or ranked.
- In a real run the agent also reads the description of each questionable role before ranking. In this
  demo every listed role is kept with the reason "Fits the operations work Casey is looking for.", so
  nothing here depends on the agent's judgment of a description.

## 4. The cover letter

Folder: [4-letter](4-letter)

The [letter tool](../../skills/letter/SKILL.md) asks a few questions, keeps your answers word for word, and then helps you cut them
into a letter. At least 80% of the letter body must be your own words, and the code checks it.

- [letter-answers.md](inputs/letter-answers.md) holds the questions the tool asked and Casey's raw
  answers. I dictated Casey's answers in character, as spoken.
- [letter-draft.md](inputs/letter-draft.md) is the cut: Casey's sentences kept, ordered and trimmed.
- [check.md](4-letter/check.md) is the code's check of that draft. It counts a word as changed when it
  is not in a run of three or more words in a row from Casey's answers. This letter changed 7 of its 348
  body words (3%, which the check rounds up), against the 20% most allowed. It also looks for lines that
  sound machine-written and lines copied from the posting, and found none.
- [letter.md](4-letter/letter.md) is the saved letter. The code saves it only when the check passes.

[`tests/test_demo.py`](../../tests/test_demo.py) rebuilds the letter and checks that the share of changed words is at most 20%,
using the same count the tool uses, and that [check.md](4-letter/check.md) reports the same numbers.

## Check it yourself

Every generated file in this folder is rebuilt offline by one command, from the same fixtures and the
same commands the skills run:

```text
uv run --frozen python tools/build_demo.py
```

The test in `tests/test_demo.py` builds the demo again on every change and fails if any file differs by
one byte, so what you read here is what the code writes today.
