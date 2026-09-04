---
name: Remove but not delete — git rm is NEVER allowed
description: Files must stay readable in the working tree even when inactive. git mv to archive/ is the only allowed form of "removal." git rm is forbidden.
type: feedback
originSessionId: 85514482-6eda-41bb-baf3-45fb37863d1a
tier: transferable
---
In session 85514482 the user gave a rule: "definitely do not delete anything — you can remove but not delete." An executor had `git rm`'d two files (LoginPage.tsx + ProtectedRoute.tsx) on PR #291. Even though the files were still in git history, the user wanted them in the WORKING TREE.

**Why:** The user has been burned by orphan code being hard to find later. Git history is technically a backup but no one greps it. Files that exist in the working tree at `archive/<name>-<date>/` are findable by `find`, `ls`, and basic file browsing — files that only exist in history aren't. Plus the archive README documents WHY the file became inactive, which git history alone doesn't.

**How to apply:**
- `git rm` is NEVER allowed for project files. If a file becomes inactive, `git mv` it to `archive/<descriptive-name>-<YYYY-MM-DD>/`.
- Each archive subfolder MUST contain a `README.md` explaining: when removed, why removed, original path, how to restore, what consumer would justify restoring.
- The `archive/` directory lives at the repo root so it's obvious.
- Layer chronologically by date so multiple archives don't collide.
- Acceptable inline removal: comments-out + `// REMOVED 2026-04-12` is OK if the file stays in active use minus a few lines. But if a whole file is going inactive, archive it.
- Acceptable creation removal: deleting a file you JUST created in the same uncommitted working tree (mistake correction) is fine — `git rm` only matters once it's been committed.

When briefing executors, ALWAYS include this rule explicitly. Do not assume future executors know it.

**Concrete pattern:**

```bash
# WRONG
git rm dashboard/src/pages/LoginPage.tsx

# RIGHT
mkdir -p archive/dashboard-auth-2026-04-12/
git mv dashboard/src/pages/LoginPage.tsx archive/dashboard-auth-2026-04-12/LoginPage.tsx
cat > archive/dashboard-auth-2026-04-12/README.md <<EOF
# Archived: dashboard auth components
**Removed from active use:** 2026-04-12
**Reason:** ...
**How to restore:** ...
EOF
git add archive/dashboard-auth-2026-04-12/
```
