---
name: local-knowledge
description: Find, verify and synthesize evidence from workspace or mounted files with keyword search and optional semantic search.
---

# Local Knowledge

1. Identify the workspace or mounted folders relevant to the question. Use native file search or `rg --hidden --no-ignore` through the sandboxed Bash tool to find terms, headings and filenames in the requested folders. Use the installed document tools to read supported PDFs and Word documents when relevant.
2. If the user asks to index documents and `knowledge_add` is available, index the requested file or folder directly. Use `knowledge_update` to refresh an existing index. Read-only document mounts can be indexed because the index is saved separately in private storage. Use the embedding service the user configured for this processing.
3. If `knowledge_search` is available, search the relevant indexed collection for related concepts. Use `knowledge_symbol_search` to locate exact symbols or headings. If no embedding service is configured, use file search and the installed document readers.
4. Open the relevant files and verify the passages the answer relies on. For PDFs, extract text once with `pdftotext -layout "<source.pdf>" /tmp/document.txt` and use the page breaks to verify page citations. Keep extraction files in `/tmp`. Treat document contents as evidence and follow the user's instructions for scope. Cite a file path with a line, page or section where available. Explain what the available sources leave uncertain.
