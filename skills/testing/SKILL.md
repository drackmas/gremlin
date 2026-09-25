---
name: testing
description: Write and run tests to verify code behavior using the project's test framework.
---

# Testing

When asked to test code or verify a change, follow this workflow.

## 1. Understand the target

- Read the function or module under test with `read_file`.
- Identify inputs, outputs, edge cases, and error conditions.
- Check for existing tests with `grep_files` (search for `def test_` or the function name).

## 2. Write tests

- Use `edit_file` with `action: "create"` for a new test file, or `action: "str_replace"` to add cases to an existing one.
- Follow the project's test conventions (fixture names, assertion style, file layout).
- Cover:
  - **Happy path**: the primary, expected behavior.
  - **Edge cases**: empty input, boundaries, single-element collections.
  - **Error paths**: invalid input, missing files, permission errors.
- Each test should be independent: no shared mutable state between tests.

## 3. Run tests

- Run the specific test: `python -m pytest path/to/test_file.py::test_name -x -q`.
- Run the full file: `python -m pytest path/to/test_file.py -x -q`.
- Run the whole suite: `python -m pytest -x -q`.

## 4. Iterate

- If a test fails, read the failure output carefully.
- Distinguish between a bug in the code and a bug in the test.
- Fix the code (not the test) unless the test itself is wrong.
- Re-run until green.

## Tips

- Name tests after what they verify: `test_edit_str_replace_unique`, not `test_edit_1`.
- Use `tmp_path` (pytest fixture) for filesystem tests — never write to the real project root.
- Keep test data minimal: just enough to exercise the code path.
- If the project has no test framework, recommend one and set up a minimal test file.
