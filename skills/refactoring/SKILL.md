---
name: refactoring
description: Improve code structure without changing behavior — extract, rename, simplify, and reorganize.
---

# Refactoring

When asked to refactor code, preserve behavior exactly while improving structure.

## Principles

- **Behavior-preserving**: every refactor must produce identical output for all inputs. Run tests before and after to confirm.
- **Small steps**: make one change at a time. Run tests after each step.
- **No feature creep**: do not add new functionality while refactoring.

## Common refactors

### Extract function
- Identify a block of code that does one thing.
- Give it a descriptive name based on what it does, not how.
- Move the block into a new function with appropriate parameters and return value.
- Replace the original block with a call to the new function.

### Rename
- Use `edit_file` with `action: "str_replace"` to rename a variable, function, or class.
- Search for all references with `grep_files` first to ensure you catch every usage.
- Rename consistently across all files.

### Simplify condition
- Replace nested `if/else` with early returns where appropriate.
- Combine redundant conditions.
- Replace complex boolean expressions with named helper functions.

### Remove duplication
- Find repeated code with `grep_files`.
- Extract the common logic into a shared function or module.
- Parameterize the differences.

### Reorganize imports
- Group imports: stdlib, third-party, local.
- Remove unused imports.
- Use absolute imports for cross-module references.

## Workflow

1. **Read the code**: Use `read_file` to understand the current structure.
2. **Identify the target**: What specific structural problem are you solving?
3. **Check for tests**: Run the existing test suite before starting.
4. **Make the change**: Use `edit_file` with `action: "str_replace"` for surgical edits.
5. **Verify**: Run tests to confirm behavior is unchanged.
6. **Repeat**: If the refactor touches multiple files, work file by file.

## Tips

- If there are no tests, write a quick characterization test first: capture the current behavior, then refactor.
- Prefer `str_replace` over `create` for refactoring: it keeps the diff reviewable.
- Do not reformat code you are not otherwise changing.
- If a refactor makes the code harder to understand, stop and reconsider.
