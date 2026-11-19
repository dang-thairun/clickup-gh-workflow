# Changelog

## Unreleased

- Add read-only `board` command: one HTML page with a ClickUp tab (cards colored by status in lanes per main task, dependency lines, whose turn it is, days waiting on the Lead, a badge for new Lead comments, filters, hover details) and a GitHub tab (PR state, latest review, checks, unresolved review threads, card links). One `tasks` read per run; `--comments` re-reads comments, `--offline` uses the cache. Local time.
- `tasks` rows include `status_type` so callers can tell done/closed statuses from custom ones.
- Skill: add a read-only progress check across ClickUp and GitHub, and a single follow-up summary draft when the Lead has not answered.
- Add read-only `tasks` command: list open tasks in the configured List, `--mine` for the current user, `--assignee` by member ID/email/username, `--remember` to save the identity in `.team-workflow/me.json`.
- `--mine` refuses to guess when the ClickUp token owner is not a List member and warns when the identity comes from a possibly shared token.
- README: one-command install, GitHub web steps for the `CLICKUP_API_TOKEN` secret, workflow diagram; full reference moved to `docs/guide.md`.

## 1.0.4 — Standalone repository extraction

- Package the working CLI/worker/API/delivery modules without an Android repository or native AI plugin dependency.
- Add source installer, Python package entry point, offline init, printable provider-independent Skill and generic sample project.
- Make custom ClickUp ID prefix configurable; keep DEV as the compatibility default.
- Export automated tests with synthetic project fixtures. No original task history, tokens, bindings or screenshots are included.
- Preserve preview/apply, status/time/estimate/diff/sync, assignment/grouping and existing spec delivery gates.

The CLI had this version before extraction. This repository upload does not publish a PyPI package or change an installed global CLI.
