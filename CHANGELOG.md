# Changelog

This file lists what changed in each release of job-search-prioritizer, newest first.

## 1.0.0 - 2026-11-24

The first release.

### Added

- A start skill that sets up your data folder, takes your resume as a PDF, a Word document or a Markdown file, and walks you through a short interview about the work you want: level, years of experience, pay floor, remote and location preferences, work authorization, industries wanted and ruled out, company size, deal-breakers, and how much each of those matters to you. Run it again at any time to retune your answers, and your earlier settings are kept.
- Title discovery. The tool pulls the skills out of your resume, samples real postings from public job boards, and shows you which job titles ask for those skills as counts, including titles you did not list. You approve, reject or edit each one.
- Scanning of the public job boards that companies host on Greenhouse, Lever and Ashby, starting from a shipped directory of companies filtered by your industries and company size. Requests are paced and cached, and the same inputs give the same results within the cache window.
- Your own target companies, which the tool resolves to their job boards or tells you it could not, and pasted postings, either as text or as a link, scored like any scanned role.
- Hard filters and weighted scores read from settings you can open and edit. A role that fails a filter says which one. A criterion with no data in a posting is shown as a gap and left out of the score instead of counting as zero. Scoring is plain code, so the same inputs always give the same tiers.
- A ranked list in A, B and C tiers, with the plain-English reason first, the gaps, and a link to apply for each role. Roles that are no longer listed on a re-scan are marked closed and drop out. Duplicate postings appear once.
- A review skill that walks through your top roles and lets you mark each recommendation right or wrong with a note of your own. Each mark records which version of your settings produced the ranking.
- A scan skill for later sessions: re-scan the boards, add companies, paste a posting the tool missed, and see the re-ranked list.
- A letter skill that builds a cover letter for a ranked role mostly from your own words. You answer a few pointed questions or share your own writing, the tool tightens your answers, and the saved letter must be at least 80% your own words, counted in runs of three or more words. A tone check names any line that sounds machine-written, such as an em dash, a stock phrase or a setup sentence ending in a colon. The letter is saved as Markdown beside the role it was written for.
- A Send to career-ops block at the end of every ranked list, with your tier A roles in the shape the career-ops plugin reads for its pipeline, so the two tools can work side by side.
- Saved summaries of your interview, your title choices and each scan, as plain files you can read and quote.
- A once-a-day check for a newer release. It sends nothing about you, shows one line when a newer version exists, and can be turned off.
- A feedback skill that drafts a bug report, a feature idea, a win or a note from what you tell it, with personal details stripped. You review it and send it yourself. After your first ranked list the tool also shows a one-time invite to share a win in the Wins category of GitHub Discussions.
- A full demo run for a fictional job seeker named Casey in `demo/casey-ops/`, with the interview answers, the title counts, a ranked list, a cover letter and screenshots. A test rebuilds the demo from fixtures, so it cannot drift from the code.
- Support on macOS, Windows and Linux with only Claude Code, git and uv installed. Continuous integration runs the tests and the skills in real shells on all three systems, and plugin validation on Linux, on every push.
- A leak check that runs before every commit and in continuous integration, so personal data and any LinkedIn access code cannot reach the repository.
- Bug and feature issue forms, a Wins discussion form, a support page, a contributing guide, a pull request template, a security policy, a code of conduct and a guide for agents.

### Privacy and limits

- Your data lives in a folder you choose, outside the plugin, and the plugin never writes your data into its own folders.
- The tool never submits an application, sends no telemetry, and makes no requests to LinkedIn. The only network requests go to public job boards, to a posting link you paste, and to the once-a-day release check.
