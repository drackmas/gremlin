---
name: code_review
description: Review code changes for correctness, clarity, and maintainability before they are merged.
---

# Code Review

When asked to review code, read the changes and provide structured feedback.

## Process

1. **Read the diff**: Use `run_command` with `git diff` (or `git diff --staged`) to see what changed. If there is no git history, read the files directly with `read_file`.
2. **Understand intent**: Infer what the change is trying to accomplish from the diff, commit messages, and surrounding code.
3. **Read context**: Use `read_file` on the files being changed to understand the surrounding architecture.

## What to check

### Correctness
- Does the code do what it claims? Trace the logic step by step.
- Are edge cases handled: empty inputs, `None`, zero, negative numbers, very large values?
- Are error paths handled: missing files, network failures, permission errors?
- Are there off-by-one errors, race conditions, or resource leaks?

### Clarity
- Are names descriptive? A variable called `x` in a 5-line function is fine; in a 200-line function it is not.
- Are functions small and single-purpose? A function doing three things should be split.
- Is the control flow easy to follow? Deep nesting (>3 levels) usually signals a refactor.
- Are comments explaining *why*, not *what*?

### Maintainability
- Is there duplication that should be extracted?
- Are magic numbers or strings extracted to constants?
- Does the change follow the existing patterns in the codebase?
- Are dependencies minimal and justified?

### Testing
- Does the change include tests?
- Do the tests cover the happy path, edge cases, and error paths?
- Are the tests independent and deterministic?

## Output format

Structure the review as:

1. **Summary**: One or two sentences on what the change does.
2. **Blocking issues**: Bugs, security problems, or breaking changes that must be fixed before merge.
3. **Suggestions**: Non-blocking improvements to clarity, performance, or maintainability.
4. **Nits**: Minor style issues, naming preferences, or optional refinements.
5. **Verdict**: Approve, request changes, or comment.

## Tips

- Be specific: point to the exact line and explain the problem.
- Distinguish between "this is wrong" and "I would prefer this".
- Acknowledge good patterns you see — reviews should not only be criticism.
- If you are unsure about something, say so and suggest how to verify.
