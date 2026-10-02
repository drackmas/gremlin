---
name: youtube_transcript
description: Get the transcript or metadata of a YouTube video and summarize or quote it.
---

# YouTube transcript skill

Goal: work with YouTube video content (summarize, quote, answer questions about
what is said) using the `youtube_transcript` and `youtube_video_info` tools.

## Rules

1. Never guess or invent video content. Always fetch the transcript first.
2. Ask the user for the video URL if you do not have one.
3. Prefer `youtube_transcript`; use `youtube_video_info` for quick metadata
   (title, uploader, duration, description) without pulling the whole transcript.

## Workflow

1. Call `youtube_transcript` with the video `url`. The tool fetches
   English subtitles only (it has no language parameter).
   - Manual subtitles are used when the video has them; otherwise
     auto-generated captions are used.
   - The result starts with the video title, transcript source, and a
     "Saved to:" path — the full transcript is also written to disk at
     `files/transcripts/<channel>/<YYYY-MM-DD>_<video name>.txt` (the date
     prefix is the video's upload date).
2. Long transcripts are truncated at ~24 KB in the tool result. If the answer
   depends on content beyond the truncation point, read the saved file
   (`Saved to:` path) with the filesystem tools instead of guessing.
3. If the call returns `ERROR: no English subtitles found. Available: ...`,
   the video has no English subtitles (the tool supports English only). Tell
   the user, list the available languages from the error, and offer to
   download the audio (`youtube_download_audio`) and transcribe it locally
   with `transcribe_audio` instead.
4. For summaries: produce a structured summary (main points, key claims,
   conclusion) and cite approximate phrasing from the transcript for important
   statements. Distinguish clearly between what the speaker said and your own
   interpretation.
5. For questions about a specific moment: search the transcript for the
   relevant lines and quote them.

## Output expectations

- Summaries: 5-15 bullet points unless the user asks for more/less.
- Always state the video title you worked from.
- If the transcript is garbled or clearly not English, say so instead of
  guessing at the content.
