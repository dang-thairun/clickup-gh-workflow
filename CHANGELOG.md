# Changelog

## Unreleased

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
