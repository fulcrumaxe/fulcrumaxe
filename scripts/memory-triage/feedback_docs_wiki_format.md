---
name: Documentation as wiki-importable markdown
description: User wants docs in wiki/ directory structured for GitHub Wiki import, not docs/ directory
type: feedback
tier: transferable
---

Docs should live in a `wiki/` directory as flat markdown files that can be directly imported into GitHub Wiki (which is just a git repo of .md files).

**Why:** User wants docs that can be pushed to GitHub Wiki. GitHub Wikis use flat file structure with specific conventions.

**How to apply:**
- Use `wiki/` not `docs/` directory
- `Home.md` as entry point (becomes wiki homepage)
- `_Sidebar.md` for navigation
- Flat structure (no subdirectories — GitHub Wiki doesn't handle them well)
- Use `[[Page-Name]]` wiki-link syntax for internal links
- Hyphens in filenames (e.g., `Getting-Started.md`, `How-It-Works.md`)
- Still create a minimal `README.md` at project root pointing to wiki/docs
- The wiki/ contents should be directly pushable to `github.com/fulcrumaxe/fulcrumaxe.wiki.git`
