## ARCHIVE_PROTOCOL

Never use `git rm` on project files.
Use `git mv <path> archive/<name>-YYYY-MM-DD/` with a README in the archive folder
explaining: when removed, why removed, original path, how to restore,
and what consumer would justify restoring.

`git rm` is **NEVER** allowed. Files that become inactive get `git mv`'d to
`archive/<descriptive-name>-<YYYY-MM-DD>/` instead.
