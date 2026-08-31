---
name: project_explorer
description: Explore this project's structure and read its source files using the filesystem tools.
---

# Project Explorer

Use the filesystem tools to explore the project the user is working in.

## How to explore

1. Start with `list_directory` on `.` to see the top-level layout.
2. Descend into directories that look relevant (`frontend/`, `backend/`, `src/`, ...).
3. Read entry points first: `app.py`, `main.py`, `package.json`, `README.md`.
4. Use `read_file` on individual files to understand their contents.

## Tips

- Prefer listing a directory before reading a file so you know what exists.
- Keep an eye on the overall shape: configuration, entry points, and module
  boundaries tell the most about how a project is organized.
- If a file is truncated, mention that it was cut off rather than guessing
  about the missing part.
