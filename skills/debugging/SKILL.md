---
name: debugging
description: Systematically locate and fix bugs using grep, read, edit_file, and run_command.
---

# Debugging

When a bug is reported, work through these steps in order.

## 1. Reproduce

- Run the failing command or test with `run_command` to confirm the error.
- Capture the full traceback and any error output.
- Note the exact input, environment, and steps that trigger the bug.

## 2. Locate

- Use `grep_files` to find the relevant code: search for error messages, function names, or variable names from the traceback.
- Use `read_file` (with `start_line`/`end_line`) to inspect the suspicious code in context.
- Check recent changes with `run_command` if a git repo is available: `git diff`, `git log --oneline -10`.

## 3. Diagnose

- Form a hypothesis: what should happen vs. what does happen, and why?
- Read the surrounding code to understand the data flow.
- Check edge cases: empty inputs, `None` values, off-by-one indices, type mismatches.

## 4. Fix

- Use `edit_file` with `action: "str_replace"` to make a surgical, minimal fix.
  - The `old_str` must be unique in the file — include enough surrounding context to disambiguate.
- Keep the fix as small as possible. Do not refactor unrelated code while fixing a bug.

## 5. Verify

- Re-run the command or test that originally failed.
- Run the broader test suite to check for regressions: `python -m pytest -x -q`.
- If the fix introduces new edge cases, add a targeted test.

## Tips

- Read the traceback from bottom to top: the last frame is where the error occurred.
- If a variable has the wrong value, trace backward from its assignment.
- If the bug is intermittent, add temporary logging before the suspicious code and re-run.
- Prefer `str_replace` over `create` for bug fixes: it keeps the diff minimal and reviewable.
