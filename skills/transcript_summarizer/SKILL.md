---
name: transcript_summarizer
description: Summarizes a text file and saves the summary as '<filename>-summary.txt' in the 'files/' directory.
---

1. Identify the target filename from the user's request (e.g., 'document.txt').
2. Use `read_file` to read the contents of the file.
3. Generate a thorough, structured summary of the content. The summary should include:
    - A high-level overview.
    - Key points/themes.
    - Detailed breakdown of major sections or ideas.
    - A concluding summary.
4. Construct the output filename by appending '-summary.txt' to the original filename (e.g., 'document-summary.txt').
5. Use `edit_file` with the `create` action to write the summary to the `files/` directory (e.g., `files/document-summary.txt`).
6. Confirm to the user that the summary has been created and where it is located.
