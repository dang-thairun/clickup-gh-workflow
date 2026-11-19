---
name: clickup-gh-workflow
description: Manage configured ClickUp cards, subtasks, assignments, estimates, tracked time and GitHub delivery through a shell CLI. Use for projects with clickup-gh-workflow configuration, not unrelated project tracking.
---

Run `clickup-gh-workflow --help`; flags --root and --via go before the command. If it is not on PATH, use the installed absolute executable path. Read the target project's .clickup-workflow.toml, Implementation specs and local agent rules. Settings belong to that project: never reuse another team's workspace/List/repository IDs.

The tool works from any shell-capable AI. It does not need a provider SDK or browser. `clickup-gh-workflow skill` prints these instructions. Quick start is in the repository README; full usage is in docs/guide.md.

GitHub mode keeps CLICKUP_API_TOKEN in the configured fork's Actions secret. Even reads/previews create scoped automation branches and runs; ClickUp writes require --apply. Local mode reads the token from process environment. Do not ask for credentials in chat or put tokens in arguments, specs or Git.

Spec IDs, custom ClickUp IDs (prefix set by custom_id_prefix) and internal API IDs are different. Use read to confirm real custom IDs; do not invent them. Create imports one spec at a time and preserves existing content/status. Native custom fields require verified live schema; descriptions alone do not populate fields or dependency relations.

To find work, use tasks (read-only): tasks lists open List tasks, tasks --mine filters to the user. If tasks --mine cannot resolve the user, or its result carries a note about a shared token owner, ask the user which ClickUp member they are (ID, email or username) and rerun with --assignee; add --remember only when they agree to save it locally. Never infer their ClickUp identity from git config, local email or chat names. Combine the listing with the repository, specs and gh issue/PR lists when the user asks what to work on.

Estimate the lowest step subtasks and leave parent own estimates zero; points belong on delivery units. Rollup visibility depends on the ClickUp ClickApp. Actual time is not an estimate. start/stop is a local stopwatch that posts a completed entry when stopped, not a server-side running timer.

Diff/sync manages the title and the marked spec region. Preserve manual notes and runtime status, priority, assignment, schedule and actual time. Sync apply requires a fresh preview hash. --adopt preserves legacy text and adds a managed region. On uncertain or partial writes, inspect remote state and the receipt; resume observes the existing run without replaying the mutation.

Read delivery.pr_unit. Spec mode supports READY gating, one branch/PR per spec, captured RED evidence and explicit human verification before GREEN. Do not fabricate RED or approval for migrated work. Group mode organizes tasks under main cards and links already-created group PRs; automated start currently refuses child branches in that mode. Use the recorded group PR/branch and the project's chosen review process.

PR bodies begin with ClickUp-Task and commits end with the matching trailer. Ready requires current test/build/lint evidence, clean pushed commits and required successful CI. Missing native PR fields remain pending. A merge alone is not QA deployment or DONE. Never merge a PR or send review requests/comments without user authorization.

Progress check (when the user asks what moved): read-only. For every card in ready, in progress or in review, and every open PR, read the card status and comments since the last check (read), the PR state, reviews and comments (pr read), and new merges on the configured upstream base. Report per card: newly READY, change requests to fix, RED approved, PR approved or merged, and backlog cards a merge just unblocked. Say plainly when nothing changed. Never change status or reply during a check.

Comments and attachments: write the comment to a file, show the preview, and run comment --apply only after the user approves that exact text; tag people with --mention <member ID> from members. Questions and blockers for the Lead belong on the card as a tagged comment. attach uploads one PNG from Implementation/mockups/ and skips a file already attached under the same name. sprints is read-only. link-deps mirrors spec Dependency lines as ClickUp waiting-on links; preview first, it never removes links.

Board (when the user wants the whole picture): `board` writes `.team-workflow/board.html` from one `tasks` read, cached card comments, the specs' dependencies and `gh`. Add `--comments` for fresh comments (one read per card, slow), `--offline` to skip ClickUp. It is read-only and shows local time. Cards are colored by status with lines to the cards they wait on, whose turn it is, days waiting on the Lead, and a red badge when the Lead answered after the user's last comment. Open the file for the user; never publish it, since it holds team card text.

Follow-up (when the Lead or reviewer was tagged and has not answered): draft one summary for today, not a re-tag on each card. For each waiting item give the card and spec ID, the PR link, what is done, the exact question or approval needed, and what it unblocks. Order by what unblocks the most work. Write it in the language the user asks for; it may be a DM the user sends themself. Preview only; post or tag anything only after explicit OK.

New cards/steps default to Normal priority. Assignment batches preserve other assignees and restrict scope to bound plan tasks and direct steps. group-backlog validates all configured groups, preserves task IDs and step relationships, and verifies the resulting hierarchy. Inspect a partial failure before retrying.
