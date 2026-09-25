---
name: youtube_summary
description: Summarize a YouTube video for the user using the tools available in this environment.
---

# YouTube Summary

Goal: produce a clear, accurate summary of a YouTube video for the user.

## Procedure

1. Confirm the exact video URL or video ID with the user if it is ambiguous.
2. Use the available tools to obtain the video's metadata and transcript:
   - If a transcript-capable tool (e.g. a yt-dlp based tool) is registered,
     call it with the video URL and ask for the transcript/subtitles.
   - Otherwise, fetch at least the title and description so the summary is
     grounded in real data, and say explicitly what you could not access.
3. Write the summary in three parts:
   - **What it is** — one or two sentences on the topic and purpose.
   - **Key points** — a short bulleted list of the main ideas, in order.
   - **Takeaway** — what the viewer should remember.
4. Never invent transcript content. If a part of the video could not be
   read, say so plainly instead of filling the gap with guesses.

## Style

- Plain language, no filler.
- Attribute claims to the video ("the presenter argues...").
- Keep the whole summary under ~250 words unless the user asks for more.
