# Contributing

Use Python 3.11 or newer. Run `python3.11 -m unittest discover -s tests -v` before submitting changes. Tests must use mocks or disposable local repositories; the default suite must not need ClickUp credentials or call live APIs.

Runtime code lives in `src/clickup_gh_workflow/`. CLI commands preview writes by default. Preserve receipts and inspect uncertain remote outcomes before retrying. Project-specific IDs belong in ignored project config/state, not source or fixtures.

For a manual integration test, use a dedicated ClickUp test List and a disposable GitHub fork/branch, then verify read-back and idempotency. Do not use the original author's production List.

Keep README and docs/guide.md command examples in sync with `--help`. No automatic release or PyPI publishing is configured.
