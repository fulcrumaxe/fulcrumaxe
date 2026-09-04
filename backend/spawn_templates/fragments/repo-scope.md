## REPO_SCOPE

Repo scope: ONLY `{{REPO}}`.
Never post to or interact with any other repo.
Every `gh` CLI call must use `--repo {{REPO}}`.
Every GraphQL query must use `repository(owner:"{{REPO_OWNER}}", name:"{{REPO_NAME}}")`.

**Before every GitHub API call, every comment, every PR interaction:**
- Confirm the target is `{{REPO}}`
- If it is not — STOP. Never post to external repos.
