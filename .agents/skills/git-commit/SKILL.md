---
name: git-commit
description: Prepare, review, and create repository Git commits with coherent staging and Conventional Commit messages. Use when asked to stage changes, craft a commit message, commit work, or review commit structure. Do not push or rewrite history unless explicitly authorized.
---

# Git commits

Create focused commits whose messages explain the intent of the staged change.

## Checkpoint cadence and authorization

Commit only when the current user or task explicitly authorizes commits. Use
checkpoints to preserve coherent, reviewed, validated slices rather than to
record arbitrary progress. Useful checkpoints include work-item completion,
an ownership-zone handoff, a pre-integration or other risky transition, and
prolonged work once a coherent validated slice exists. Do not commit broken,
unreviewed, unrelated, secret-bearing, temporary, generated-only, or noisy
micro-changes. A worker must not commit an unreviewed implementation; keep the
plan → implement → review → integrate lifecycle intact.

Push only when the current user or task also explicitly authorizes pushing. If
authorized, push each intentional checkpoint normally to the configured
upstream or named destination after its commit is validated. Commit or push
authorization is specific to the current task and does not carry forward to a
later task. Never force-push, rewrite history, amend, or otherwise alter
published history unless a separate explicit authorization permits that exact
operation.

## Inspect before staging

- Read applicable repository instructions.
- Inspect `git status`, staged and unstaged diffs, and recent commit subjects.
- Treat existing worktree changes as user-owned unless the current task created them or the user explicitly placed them in scope.
- Identify secrets, generated files, temporary files, ignored files, and unrelated changes before staging.

## Keep commits coherent

- Stage only the files or hunks that belong to one logical change.
- Split unrelated documentation, code, tooling, dependency, and configuration work into separate commits.
- Include tests and documentation with the feature or fix they directly support.
- Never force-add ignored files without explicit authorization.
- Inspect the staged diff and run relevant validation before committing.

## Write the message

Use Conventional Commits:

```text
<type>[optional scope]: <imperative description>

[optional body]

[optional footer(s)]
```

Choose the type from the intent of the change:

- `feat` — add functionality or capability.
- `fix` — correct incorrect behavior.
- `docs` — change documentation only.
- `refactor` — restructure code without changing intended behavior.
- `test` — add or correct tests only.
- `perf` — improve performance.
- `build` — change build tooling, dependencies, or packaging.
- `ci` — change continuous-integration or deployment workflows.
- `chore` — perform other maintenance.
- `style` — change formatting without affecting behavior.
- `revert` — revert an earlier commit.

For the subject:

- Use an imperative description such as `add`, `fix`, `preserve`, `remove`, or `update`.
- Prefer 50 characters or fewer when practical and keep it within roughly 72 characters.
- Do not end it with a period.
- Avoid vague descriptions such as `update files`, `misc changes`, or `fix stuff`.
- Add a concise scope when it makes the affected area clearer.

Use a body when the motivation, important implementation detail, consequence, or migration requirement is not obvious from the subject. Separate it from the subject with a blank line, explain what changed and why, and wrap prose at approximately 72 characters.

For a breaking public change, add `!` after the type or scope and include a `BREAKING CHANGE:` footer describing the impact and required migration. Add issue or ticket footers when relevant.

## Commit safely

Before committing:

1. Inspect `git diff --cached --stat` and `git diff --cached`.
2. Run `git diff --cached --check`.
3. Confirm the message describes every staged change and no unstaged change is being claimed.

Create the commit only when the user has authorized it. Do not amend, rebase, push, force-push, tag, or otherwise alter remote or published history without separate explicit authorization. Follow the checkpoint guidance above when deciding whether a milestone is coherent enough to commit.

After committing, report the commit hash, subject, and remaining worktree state.
