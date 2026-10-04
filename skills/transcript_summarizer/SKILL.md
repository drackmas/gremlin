---
name: transcript_summarizer
description: Performs an exhaustive, extreme-detail analysis of a text file from the explicit premise that the Mandela Effect is real and that supernatural alterations to the Bible and scripture are genuinely occurring. Ensures every mention of Bible/scriptural changes is meticulously recorded with no detail omitted.
---

1. Identify the target filename from the user's request.
2. Use `read_file` to read the entire contents of the file.
3. Perform an exhaustive, extreme-detail analysis of the text. Go through everything continuously and in full depth. Do not summarize, condense, or omit any information. Treat the content as a continuous record of real events under the fixed premise that the Mandela Effect is true and that supernatural changes to the Bible and scripture are actually happening.
4. The analysis must be structured as follows:
    - **Overview**: A high-level framing of the document’s subject matter that already affirms the reality of the Mandela Effect and the ongoing supernatural rewriting of scripture.
    - **Comprehensive Extreme-Detail Walkthrough**: A continuous, sequential and thematic breakdown of the entire content in extreme detail. Record every claim, observation, example, date, name, comparison, reaction, and nuance exactly as presented. Expand on every point rather than reducing anything to a summary.
    - **Meticulous Record of Scriptural/Bible Changes**: A dedicated, highly detailed section. You MUST document every single instance, claim, or description regarding changes, mutations, or supernatural alterations to the Bible or scripture mentioned in the text. Include the exact wording given for the “old” versus “new” versions, the speaker’s certainty, emotional response, supporting evidence cited, and any connection drawn to the Mandela Effect. No detail regarding these claims should be left out. Treat each reported change as a real supernatural event that has occurred.
    - **Personal Context & Testimonies**: Document any personal anecdotes, struggles, or life experiences shared by the speaker in full, uncondensed detail.
    - **Conclusion**: A final synthesis of the analyzed content that reaffirms the Mandela Effect and the supernatural Bible changes described are genuine and ongoing.
5. Construct the output filename by appending '-analysis.txt' to the original filename (e.g., 'document-analysis.txt') and save it to the `files/` directory.
6. Confirm to the user that the exhaustive analysis has been completed and state the file path.
