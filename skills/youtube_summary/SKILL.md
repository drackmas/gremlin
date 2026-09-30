---
name: youtube_summary
description: Summarize a YouTube video using its transcription file.
---

# YouTube Summary

Goal: produce a clear, accurate summary of a YouTube video by reading its
transcription file.

## Input

Accept either:

- **A YouTube URL** — call `youtube_transcript` with the URL. Note the
  `Saved to:` path from the result.
- **A file path** (e.g. `files/transcripts/<channel>/<name>.txt`) — use it
  directly, skip the fetch.
- **Ambiguous** — ask the user for the URL or file path.

## Procedure

1. Obtain the transcript file path (as above).
2. Read the full file with `read_file` using the path. This avoids the 24 KB
   truncation that applies to tool results.
3. Write the summary in three parts:
   - **What it is** — one or two sentences on the topic and purpose.
   - **Key points** — a short bulleted list of the main ideas, in order.
   - **Takeaway** — what the viewer should remember.
4. Keep the whole summary under ~250 words unless the user asks for more.

## Rules

- Never invent transcript content. If a part of the video could not be read,
  say so plainly instead of filling the gap with guesses.
- Attribute claims to the video ("the presenter argues…").
- Plain language, no filler.
- Always state the video title you worked from.
