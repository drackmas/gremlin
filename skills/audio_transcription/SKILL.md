---
name: audio_transcription
description: Transcribe a local audio or video file to text with faster-whisper; runs as a background job and Gremlin replies when it's done.
---

# Audio Transcription

Turn a local audio or video file into a text transcript with the
`transcribe_audio` tool. It runs in the background (so even multi-hour files
work) and saves the result to `files/<name>-transcription.txt`. Gremlin tells
the user automatically when it finishes — you do not need to wait or poll.

## When to use

- The user shares or points at an audio/video file and wants its words.
- Podcasts, interviews, lectures, meetings, videos, voice notes, ringtones.
- They want to search, summarize, quote, translate, or reuse the spoken content.

## How

1. Call `transcribe_audio` with the file `path`.
   - `model`: `turbo` (default — fast) or `large-v3` (maximum accuracy).
     Pick `large-v3` only if the user explicitly wants top accuracy.
   - `language`: the spoken language code (e.g. `en`), or `auto` to detect.
   - `overwrite`: `true` to replace an existing `.txt`.
2. It returns a `job_id` immediately. Tell the user it's running and that you'll
   let them know when it's done. **Do not busy-poll.** The completion reply
   reaches the user on its own.
3. Only if the user explicitly asks "is it done yet?": call `transcribe_status`
   with the `job_id` and relay the progress line.
4. To stop it: call `kill_transcription` with the `job_id`.

## Rules

- Always use `transcribe_audio` — do not run `faster-whisper`, `whisper`, or
  `ffmpeg` via the shell for transcription.
- The transcript path is `files/<name>-transcription.txt`; name it clearly.
- If a tool returns `ERROR:`, relay the message and suggest a fix (check the
  file path, confirm the file is readable, pass `overwrite=true`, or try a
  different model).
- Once the transcript exists, the user's remaining request takes priority: if
  they also asked to summarize it, extract key points or quotes, translate it,
  or save a copy, do that now without asking. Only if the request is fully
  satisfied, offer one or two optional next steps.
