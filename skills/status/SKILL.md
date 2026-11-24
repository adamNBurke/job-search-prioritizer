---
name: status
description: Check that job-search-prioritizer runs on this computer, set up your data folder on first use, and show what to do next.
allowed-tools: Bash(uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py" *) PowerShell(uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py" *)
---

# Status

This skill checks that the tool runs on this computer and sets up the folder that holds your job-search data. It runs the engine through two commands. It never edits files itself.

The first run downloads Python and a few packages through uv and can take a minute. Tell the user this before running anything.

## Steps

1. If the `uv` command is not found, point the user to https://docs.astral.sh/uv/getting-started/installation/ and stop. Do not try to install it for them.
2. Ask the user which folder should hold their job-search data. It must be a folder they own, outside the plugin. A new folder in their home directory is a good choice.
3. Write the path with forward slashes and no trailing slash.
4. Refuse a path that contains a double quote, backtick, dollar sign, semicolon, ampersand or pipe, because those break the quoted command. Ask for a different folder.
5. Run these two lines one at a time, replacing {DATA_FOLDER} with that path. Each line sits alone inside a fenced text block.

```text
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py" init --data-dir "{DATA_FOLDER}" --plugin-data "${CLAUDE_PLUGIN_DATA}"
```

```text
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py" status --data-dir "{DATA_FOLDER}" --plugin-data "${CLAUDE_PLUGIN_DATA}"
```

6. Read the JSON each command prints. Tell the user in plain English whether it worked (`ok`), what to do next (`next`), and any `warnings`. If `ok` is false, show the `error` text and the `next` step, and stop.
