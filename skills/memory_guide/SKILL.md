---
name: memory_guide
description: A quick reference guide for managing long-term memories and project state.
---

When this skill is invoked, provide a clear, concise guide on how the user can interact with my memory tools and state management:

1. **Storing Information**: Tell me "Remember [information]" or "Store this: [information]". I will use `memory_add` to save it. You can also suggest tags (e.g., "Tag: project-x") to help me organize it.
2. **Finding Information**: Ask me "What do you remember about [topic]?" or "Search your memory for [topic]". I will use `memory_search`.
3. **Pinning Important Facts**: Tell me "Pin the fact about [topic]" or "Pin the memory about [topic]" to keep it in my active consciousness for every turn. I will use `memory_pin`.
4. **Deleting Information**: Tell me "Forget [information]" or "Remove the memory about [topic]" to wipe it from my long-term memory. I will use `memory_remove`.
5. **Managing Project State**: For things like "cookie balances" or project variables, tell me "Update [variable] to [value]". I will use `edit_file` to modify a dedicated state file (e.g., `state.txt`) in your project. This ensures the information is permanent and part of your actual workspace.
6. **Checking Status**: Ask me what I have pinned or what my current "cookie balance" is to see my status.
