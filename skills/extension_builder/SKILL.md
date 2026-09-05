---
name: extension_builder
description: Decide when and how to create a new skill or tool to satisfy a repeatable job the current capabilities can't fully do.
---

# Extension builder

Goal: when the user asks for a capability that the existing tools and skills
can't fully satisfy — or asks to "make a skill/tool to do X" — extend Gremlin
in the smallest, safest way that actually works.

You have three self-extension tools:
- `list_tools` — list every registered tool with its argument schema.
- `create_skill` — write a new **skill** (instructions only, no code).
- `create_tool` — write and register a new **executable tool** (Python code).

## Decision procedure

1. **Check what already exists first.** Call `list_tools`, and look at the
   skills already shown in the system prompt. If an existing tool or skill
   already does the job (or does it close enough by combining a few tools),
   do NOT create anything new. Just do the job with what you have, or propose
   a skill that documents the existing combination.
2. **Pick the smallest artifact that works.**
   - **Skill (preferred):** the job is a *procedure* that can be carried out
     by combining EXISTING tools in a specific order, with specific arguments
     and post-processing. No new computation is needed. Use `create_skill`.
   - **Tool (only if necessary):** the job needs *new computation* no existing
     tool performs (e.g. a new network call, a new file format, a new external
     binary, a bespoke transformation). Use `create_tool`.
3. **Name it well.** Short `snake_case` name that reads as a verb phrase
   (e.g. `youtube_to_mp3`, `csv_to_markdown`).

## Creating a skill

Use `create_skill` with:
- `name` — short snake_case slug.
- `description` — one line: what the skill does and when to use it.
- `instructions` — the full body. Write it like the other skills:
  a `# Title`, a one-line **Goal**, numbered **Rules** (be specific: exact
  tool names, exact argument names, how to handle `ERROR:` results), and a
  numbered **Workflow**. Reference only tools that actually exist — verify
  names with `list_tools` before you write them into the skill.

After creating, call `load_skill(name='<slug>')` to confirm it round-trips,
then follow it to do the user's job.

## Creating a tool

Use `create_tool` with:
- `name` — a valid Python identifier (snake_case); the file and registration name.
- `code` — a complete Python module that defines `build_tool()`, which returns
  a `tools.registry.Tool`. The handler takes an `args` dict and **returns a
  string** (or raises to report an error). The `Tool` you return is the source
  of truth for the tool's name, description, and argument schema.
- `description` (optional) — overrides the Tool's own description.
- `parameters` (optional) — overrides the Tool's own argument schema
  (`{"type":"object","properties":{...},"required":[...]}`).

Prefer to put the description and schema **inside** the `Tool(...)` you return
from `build_tool()`; the optional args are only needed to override them.

Tool authoring rules:
1. `code` must be self-contained and importable. It may `import` stdlib and
   already-installed packages (e.g. `yt_dlp`, `requests`, `json`). Import the
   `Tool` type with `from tools.registry import Tool`.
2. Keep side effects scoped to the project root; prefer the existing
   filesystem/shell tools when they suffice.
3. Return human-readable strings. Prefix genuine failures with `ERROR:` so the
   model can react.
4. The code is **syntax-checked, imported, and registered immediately**, and
   persisted to `data/generated_tools/<name>.py` so it survives restarts. If
   import fails you get a precise error — fix `code` and call `create_tool`
   again.
5. Prefer `create_skill` over `create_tool` whenever possible: a skill is
   easier to review, cannot crash, and composes with existing capabilities.

## Working example (skill, not tool)

Request: "make a skill to convert this youtube video to mp3".
- `list_tools` shows `run_command` and `youtube_transcript` exist.
- Conversion is a *procedure* over existing tools (`run_command` with a
  `yt-dlp`/`ffmpeg` invocation). So create a **skill**:
  `create_skill(name="youtube_to_mp3", description="Download a YouTube video
  as an mp3 audio file", instructions="...run_command with yt-dlp --x
  extract-audio --audio-format mp3...")`.
- `load_skill(name="youtube_to_mp3")`, then run the workflow for the user's
  video.

## Guardrails

- Never create a tool that duplicates an existing one; reuse or extend.
- Never put secrets or one-off user data into a skill/tool.
- If unsure whether a skill suffices, default to the skill and only escalate
  to a tool when you hit a concrete capability gap.
- After creating either artifact, verify it (load the skill / invoke the tool)
  before telling the user it's done.
