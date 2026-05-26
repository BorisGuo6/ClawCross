# Issue Tracker

Issues and implementation follow-ups for this repo live in GitHub Issues unless the user explicitly asks for local markdown tracking.

Use the `gh` CLI from inside this clone so it infers the repository from `git remote -v`.

## Common Operations

- Create: `gh issue create --title "..." --body "..."`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels,comments`
- Comment: `gh issue comment <number> --body "..."`
- Label: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

When a skill says to publish work to the issue tracker, create or update a GitHub Issue.
