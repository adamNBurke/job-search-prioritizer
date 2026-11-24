---
name: feedback
description: Draft a bug report, a feature idea, a win to share, or a note to Adam from what you tell me. You review it and send it yourself.
---

# Feedback

This skill helps the user tell the maker of this tool what happened: a bug, an idea, a win, or a plain note. It turns what the user says into a short draft written for the matching GitHub form, removes personal details, shows the draft, and gives the link. The user reads it, changes it, and sends it from their own account.

This skill runs no command and makes no network call. It only talks with the user and drafts text.

## Steps

1. Ask what happened or what they want to say, in their own words. Let them finish before you ask anything else.
2. Ask with the AskUserQuestion picker which kind of feedback it is: bug, feature idea, win, or note to Adam.
3. For a bug, ask for the operating system and the shell in one AskUserQuestion picker call, then ask for the plugin version and the JSON line the tool printed when it went wrong in one message. The version is in the plugin's `.claude-plugin/plugin.json` file and on its page in the `/plugin` screen. If they cannot find one of these, leave it as "not sure".
4. Draft the text. For a bug, use the fields of the bug form: what happened, what you expected, steps to reproduce, operating system, shell, plugin version, the JSON line. For a feature idea, use the fields of the feature form: what problem would this solve, what would you like the tool to do, what do you do today instead. For a win or a note, write a short discussion post of a few plain sentences. Keep the user's meaning and, where you can, their own words.
5. Before you show the draft, remove these: names of people, email addresses, phone numbers, employer names the user did not ask to include, any text from their resume or answers, and any folder path that shows their user name. Write ~ for the home folder in a path. If a printed JSON line holds any of these, remove them from the line and say what you removed.
6. Show the draft and ask with the AskUserQuestion picker whether they want to change it. Change it as they ask, and repeat the check in step 5 on the new text.
7. Give the matching link and stop. Tell the user to open it, paste the draft in, read it once more, and send it:
   - bug: https://github.com/adamNBurke/job-search-prioritizer/issues/new?template=bug.yml
   - feature idea: https://github.com/adamNBurke/job-search-prioritizer/issues/new?template=feature.yml
   - win: https://github.com/adamNBurke/job-search-prioritizer/discussions/categories/wins
   - note to Adam: https://github.com/adamNBurke/job-search-prioritizer/discussions

For anything else, the project lives at https://github.com/adamNBurke/job-search-prioritizer.

## Rules

- You never send, post or open anything yourself; the user reviews the draft and sends it.
- Never read files in the data folder unless the user asks you to quote an error line from one.
- Never put the user's resume, answers, contact details or data-folder paths in a draft.
- This skill has no command line. Do not run any command to write or send feedback.
