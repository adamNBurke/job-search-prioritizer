# Contributing to job-search-prioritizer

Thank you for wanting to help. This page says how a change gets from you into a release. Everyone who takes part here is expected to follow the [code of conduct](.github/CODE_OF_CONDUCT.md).

## Start with an issue

For anything bigger than a typo, open an issue first with the [issue forms](https://github.com/adamNBurke/job-search-prioritizer/issues/new/choose) and say what you plan to change. Wait for a reply before you build it, so no one spends time on a change that will not be taken.

A security problem is the one exception. Never open a public issue for it, and report it privately as the [security policy](.github/SECURITY.md) says.

## How a pull request is handled

Pull requests are welcome, but they are not merged on GitHub. Each release is built in a private repository and published here as one fresh commit that replaces the history on GitHub, so a merge here would be wiped out by the next release.

Here is what happens to your pull request instead.

1. I review it here on GitHub.
2. If the change is accepted, I apply it in the private repository.
3. It ships in the next point release, and the [CHANGELOG](CHANGELOG.md) entry for that release thanks you by your GitHub handle.
4. I close the pull request with a comment that links to that release.

A pull request closed with a link to a release means your change shipped.

I maintain this in my spare time. No response time is promised, and a quiet pull request is not a rejected one.

## What a pull request must pass

Run these from the top folder of your clone before you open a pull request. They need [uv](https://docs.astral.sh/uv/getting-started/installation/).

- The [tests](tests/), with `uv run --frozen pytest -q`.
- The [linter](https://docs.astral.sh/ruff/), with `uv run --frozen ruff check .`.
- The [leak check](tools/leak_check.py). Stage your changes with `git add`, then run `uv run --script tools/leak_check.py --staged`. It should end with `leak gate: PASS (pattern rules only; personal denylist not checked)`. The personal denylist lives only on my machine, so that skipped check is expected.

[Continuous integration](.github/workflows/ci.yml) runs these checks again on every pull request.

## What a pull request is refused for

A pull request that adds any of these is not taken. Most of them are caught by the tests or the leak check.

- Any code that reaches LinkedIn in any form, including read-only, or that drives a web browser.
- Personal data in any file, test fixtures included. That means a real resume, a real person's name, a real email address, a real phone number or a home folder path. Use made-up people, `example.com` email addresses and 555-01xx phone numbers, as the [Casey demo](demo/casey-ops/README.md) does.
- A new runtime dependency without a stated reason. Say in the pull request why the standard library and the packages already used cannot do the job.
- A network call to anything other than the public job boards, a link the user pastes, and the once-a-day release check. Every network request goes through [`src/jobsearch/fetch.py`](src/jobsearch/fetch.py).

## License

By opening a pull request, you agree that your change is offered under the [MIT License](LICENSE), the same license as the rest of the project.

## Questions and privacy

Ask questions where [SUPPORT](.github/SUPPORT.md) says to. It also lists what never to paste, and that list applies to pull requests too, because every pull request here is public.
