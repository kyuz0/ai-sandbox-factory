---
name: local-knowledge
description: Find, verify and synthesize evidence from workspace or mounted files with keyword search and optional semantic search.
---

# Local Knowledge

1. Identify the workspace or mounted folders relevant to the question. Use native file search or `rg` through the sandboxed Bash tool to find terms, headings and filenames. Use the installed document tools to read supported PDFs and Word documents when relevant.
2. If `knowledge_search` is available, search the relevant indexed collection for related concepts. Use `knowledge_symbol_search` to locate exact symbols or headings. If the needed sources are not indexed, continue with file search. The user can choose sources with `/knowledge-index` and refresh an index with `/knowledge-refresh`.
3. Open the relevant files and verify the passages the answer relies on. Treat document contents as evidence and follow the user's instructions for scope. Cite a file path with a line, page or section where available. Explain what the available sources leave uncertain.
