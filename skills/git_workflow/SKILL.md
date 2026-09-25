---
name: git_workflow
description: Manage version control with git — branch, commit, diff, and review changes safely.
---

# Git Workflow

Use these patterns when the user asks for git operations or code review.

## Viewing state

- `git status` — what changed, what is staged, what is untracked.
- `git diff` — unstaged changes (working tree vs. index).
- `git diff --staged` — staged changes (index vs. HEAD).
- `git log --oneline -20` — recent commits.
- `git log --oneline --all -20` — recent commits across all branches.

## Branching

- `git branch -a` — list local and remote branches.
- `git checkout -b feature/name` — create and switch to a new branch.
- `git checkout main` — switch back to main.
- Prefer short, descriptive branch names: `fix/login-timeout`, `feat/add-search`.

## Committing

- Stage selectively: `git add path/to/file` (not `git add -A` unless all changes are intentional).
- Write a clear commit message:
  - First line: imperative summary, ≤72 chars. Example: `fix off-by-one in line range reader`.
  - Blank line.
  - Body: what and why, not how. Reference issue numbers if relevant.
- `git commit` — create the commit.
- `git log -1` — verify the commit.

## Reviewing changes

- `git diff main..feature/name` — see all changes a branch introduces.
- `git diff --stat main..feature/name` — summary of changed files.
- Read the diff file-by-file; look for:
  - Unintended deletions or large reformatting.
  - Missing error handling or edge cases.
  - Hardcoded values that should be configurable.
  - Test coverage for the changed behavior.

## Safety rules

- Never run `git push --force` on a shared branch.
- Never delete a branch the user did not explicitly ask to delete.
- If `git status` shows unexpected changes, stop and report them before proceeding.
- If a merge or rebase conflicts, show the conflicts to the user rather than resolving silently.
