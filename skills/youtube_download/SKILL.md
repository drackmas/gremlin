---
name: youtube_download
description: Download YouTube videos or audio to the files folder as background jobs; Gremlin replies when the download is done.
---

# YouTube Download

Goal: download YouTube content (video or audio) to the `files/` folder when
the user asks to "download" something from a YouTube link. Downloads run as
background jobs so long downloads never block the conversation. Gremlin tells
the user automatically when a download finishes — you do not need to wait or
poll.

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

## How

1. Call `youtube_download_video` or `youtube_download_audio` with the URL.
2. It returns a `job_id` immediately. Tell the user the download is running and
   that you'll let them know when it's done. **Do not busy-poll.** The
   completion reply reaches the user on its own.
3. Only if the user explicitly asks "is it done yet?": call `download_status`
   with the `job_id` and relay the progress line.
4. To stop it: call `kill_download` with the `job_id`.
5. Once the completion notice arrives, name the saved file and offer natural
   next steps (e.g. transcribe the audio, ingest it into the knowledge
   library, summarize it).

## Rules

- Always use the matching tool — do not shell out to `yt-dlp` directly.
- Report the save path and file size from the completion notice.
- If a tool returns `ERROR:`, relay the error to the user and suggest a fix
  (e.g. check the URL, verify the video is not private/restricted).
