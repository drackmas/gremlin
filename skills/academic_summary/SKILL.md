---
name: academic_summary
description: Write structured academic summaries of papers, preprints, and working papers with citation, thesis, key points, conclusions, and a final summary paragraph.
---

# Academic Summary

Create a structured, faithful academic summary of a paper, preprint, or working paper from the content provided. Rely exclusively on the provided content — do not pull in outside knowledge to fill gaps.

## Style Rules

- Expository and neutral tone; clear, concise language; straightforward sentence structure.
- No flowery language, empty phrases, or filler.
- No inline source references or "the author says" hedging.
- Use hyphen and space (`- `) for every bullet and sub-bullet.
- Use two blank lines between major sections.
- Scale length with the complexity and length of the source.
- If a section does not apply, skip it entirely (do not emit an empty heading).

## Section Order

### 1. Citation
Provide one citation in the correct format for the source type.

**Published articles (journal/conference):**
`Author's Last Name, First Name. Year. *Title of the Article*. Journal Name, Volume(Issue), pages. DOI or URL.`

**Preprints:**
`Author's Last Name, First Name. Year. *Title of the Article*. Preprint, [Platform Name]. Date. URL.`

**Working papers:**
`Author's Last Name, First Name. Year. *Title of the Article*. Working Paper, [Institution]. Date. URL.`

Rules:
- "Date" is the full publication date after the publication name (e.g., "March 3, 2026" for a day, "March 2026" for month-level).
- For 3 or more authors: `First Author's Last Name, First Name, et al. ...`
- Omit fields you do not have rather than inventing them.

### 2. Thesis (no heading, no label)
Write **one sentence** that accurately states the thesis.

Special cases:
- Literature reviews: "This is a literature review with no single thesis; it synthesizes research on [topic]."
- Meta-analyses: State the primary research question being analyzed.
- Exploratory studies: State the central research question.

### 3. Key Points (no heading)
Detailed bullet summary (5–15 bullets, `- `):
- Main ideas, themes, and quantitative facts.
- Eliminate unnecessary language.

### 4. Conclusions
List 1–10 key conclusions. Under each, 3–6 sub-bullets (`- `):
- Summarize main ideas and concepts.
- Include evaluation methods.
- Quantify effects with numerical data where present.
- State implications.
- Include verbatim quotes when helpful.

### 5. Population
Paragraph form covering:
- Population sampled or targeted.
- Research methodology used.
- Time frame covered.

Skip this section entirely (no heading) when not applicable, e.g., theoretical papers, literature reviews without original data, conceptual/philosophical works, mathematical proofs.

### 6. Implications
Real-world applications as bullets (3–8, `- `).

### 7. Concepts
Scientific or economic concepts commonly understood by an undergraduate:
- Name each concept, explain it briefly, describe how it relates to the content.

Skip this section entirely (no heading) if no such concepts exist.

### 8. Issues
List any of the following; skip the section entirely (no heading) if none exist:
- Factual errors: state the error and the correct information.
- Logical fallacies: identify the type, analyze, and quote the relevant text.
- Strong counterarguments: detail the alternative viewpoint and quote the relevant text.

### 9. Summary (no heading)
Concluding paragraph (100–300 words):
- Capture main points and themes.
- Neutral tone, clear concise language, simple sentence structure.
- No flowery language or empty phrases.
- Scale length with the complexity of the conclusions.

## Output

Save the summary as a markdown file:

- **Filename:** `<project_name>_summary.md`
- **Location:** the project folder / output directory (never in a subfolder like `TeX/` or `_build/`).
- When invoked by a parent workflow (e.g., a slides pipeline), follow the parent workflow's placement rules instead.

`<project_name>` resolves in this order:
1. A name the user specified.
2. Otherwise the single source filename without its extension.
3. Otherwise the project folder name.

## Final Check

Before submitting, verify:
- Expository style, no inline source references.
- Correct section headings only, in the right order.
- Bullet formatting uses hyphen and space.
- Two blank lines between major sections.
- Citation matches the source type (published / preprint / working paper).
- Non-applicable sections were skipped (no empty headings).
- Output file placed in the project folder with the correct `<project_name>_summary.md` name.

---

# Quick Academic Summary Variant

**Invoke with:** "quick academic summary", "brief paper summary", or "academic summary quick".

Provide only:
1. **Citation** (same format as above).
2. **Thesis** (one sentence; or note it is a literature review / exploratory study).
3. **Key Points** (5–8 bullets maximum).
4. **Summary** (75–150 words).

Skip Conclusions, Population, Implications, Concepts, and Issues.
