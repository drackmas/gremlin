---
name: youtube_download
description: Download YouTube videos, audio, or transcriptions to the files folder.
---

# YouTube Download

Goal: download YouTube content (video, audio, or transcript) to the `files/`
folder when the user asks to "download" something from a YouTube link.

## Input

1. Ask the user for the YouTube URL if one was not provided.
2. Determine what they want to download from their phrasing:

| User says | Tool | Output |
|---|---|---|
| "download the video", "get the mp4" | `youtube_download_video` | `files/videos/<channel>/<name>.mp4` (~360p) |
| "download the audio", "get the mp3" | `youtube_download_audio` | `files/audio/<channel>/<name>.mp3` (128 kbps) |
| "download the transcription", "get the transcript" | `youtube_transcript` | `files/transcripts/<channel>/<name>.txt` |

If the request is ambiguous (e.g. just "download this"), ask which format they
want. If they ask for multiple formats, call each tool in turn.

## Rules

- Always use the matching tool — do not shell out to `yt-dlp` directly.
- Report the save path and file size from the tool result.
- If a tool returns `ERROR:`, relay the error to the user and suggest a fix
  (e.g. check the URL, verify the video is not private/restricted).
