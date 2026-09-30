---
name: youtube_analysis
description: Thorough analysis of a YouTube video using its full transcription file.
---

# YouTube Analysis

Goal: produce a complete, thorough analysis of a YouTube video that covers
**all** information in the transcription — no sections skipped.

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
   truncation that applies to tool results. If the file is very large, read it
   in multiple passes to cover every section.
3. Produce a complete analysis with these sections:
   - **Overview** — topic, purpose, speaker, context, and overall structure
     of the video.
   - **Detailed breakdown** — go through the content section by section, in
     order, covering every major point, argument, example, and claim the
     speaker makes. Do not skip or merge sections.
   - **Key arguments & evidence** — what the speaker argues and what they use
     to support each claim (data, examples, anecdotes, references).
   - **Notable details** — specific data points, quotes, examples, names, and
     references worth flagging.
   - **Gaps & weaknesses** — anything the speaker skips, contradicts,
     oversimplifies, or fails to address.
   - **Conclusion** — overall assessment of the video's argument and
     credibility.

## Rules

- Must be thorough. If the transcript is long, the analysis is long. Cover
  every section — do not summarize away parts of the content.
- Quote the transcript for important claims. Distinguish clearly between the
  speaker's words and your own analysis.
- Never invent content that is not in the transcript.
- Always state the video title you worked from.
- Plain, precise language. No filler.
