---
name: demo
description: A demo skill used only as a lint fixture.
allowed-tools: Bash(uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py" *) PowerShell(uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py" *)
---

# Demo

Run these lines one at a time. The first sets up the folder and the second checks it.

```text
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py" init --data-dir "{DATA_FOLDER}" --plugin-data "${CLAUDE_PLUGIN_DATA}"
```

```text
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py" status --data-dir "{DATA_FOLDER}"
```
