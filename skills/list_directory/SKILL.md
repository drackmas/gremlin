---
name: list_directory
description: Show the files and subdirectories inside a directory using the list_directory tool.
---

# List directory

Goal: when the user asks to show or list the files in a directory (the project
root, `files/transcripts`, a subfolder, ...), use the `list_directory` tool to
show them exactly what is there.

## Rules

1. Use the `list_directory` tool — do not guess or invent the contents of a
   directory from memory.
2. Pass a relative path from the project root:
   - The root directory is `.` (or omit `path`; it defaults to `.`).
   - A subdirectory is its relative path, e.g. `files/transcripts`.
3. Show only the immediate contents of the requested directory — one level
   deep. Do not automatically list the contents of subfolders. A folder shown
   with a trailing `/` is listed by name only; its contents are not expanded.
4. Only enter a subfolder when the user explicitly asks to see that specific
   folder (e.g. "show me `files/transcripts`").
5. Never invent entries. Only report what the tool returns.

## Workflow

1. Call `list_directory` with the target directory's relative path.
2. Present the result to the user. The tool returns one entry per line;
   subdirectories end with `/`, plain files do not.
3. Stop there. Do not call `list_directory` on the subfolders that came back.
   If the user then asks to look inside one of them, call `list_directory` on
   that specific folder only.

## Handling failures

- If the tool returns an `ERROR:` (e.g. the path is not a directory, does not
  exist, or would escape the sandbox), show the error to the user and ask for
  a corrected path. Do not fall back to guessing the contents.
- If the user names an absolute path or one that walks outside the project
  (`..`), explain that only paths inside the project root are allowed and ask
  for the relative path.
