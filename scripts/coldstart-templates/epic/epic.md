# Epic <N>: <Epic title>

> **Epic location:** `epics/epic-<N>-<slug>/epic.md`
> **Task files:** `epics/epic-<N>-<slug>/01.md` through `<NN>.md`
> **Source:** <where this epic came from — a roadmap line, a design doc, a
> conversation. Delete this line if there isn't one.>

## Context

<Prose, two or three paragraphs. What this epic covers and why it is worth
doing now.

Say plainly what already exists and what does not. An epic whose Context reads
"we will build X" when half of X already ships is the single most common way
this file goes wrong — a reader who has never seen the codebase should finish
this section knowing exactly where the work starts.

If parts of the epic only apply under some configuration (cloud mode, a paid
tier, one platform), say so here rather than leaving each task to rediscover
it.>

## Architecture

```
<A tree of the files and modules this epic touches, annotated existing or new.
Paths, not prose — this is the map a reader uses to orient before opening
anything.>

path/to/existing-module        — what it does today (existing)
path/to/another-existing       — what it does today (existing)
path/to/new-module             — what it will do (new)
```

## Tasks

<One row per task file. The `#` column is the task file's basename, so `01`
means `01.md`. Keep the two in sync: this table is what a human reads, the
task files are what the importer reads, and they drift the moment nobody is
checking.

Status is one of `not-started`, `in_progress`, `completed`, `superseded`.
Commit is the short SHA that finished the task, or `—` while it is open.>

| # | Title | Status | Commit |
|---|-------|--------|--------|
| 01 | <first task title> | not-started | — |
| 02 | <second task title> | not-started | — |

<!--
Notes on filling this in
========================

The directory name carries the epic number and a slug:
`epics/epic-7-billing/`. The number in the directory name, the number in the
H1 above, and the `epic:` field in every task file under it must all agree —
the importer builds Discussion titles and the `epic-<N>` label from the task
files' frontmatter, and nothing reconciles them against this file.

An epic with no task files yet is fine. It still gets an overview Discussion
when the importer is run with `--include-empty-epics`, taken from the H1 on
line 1 of this file.

Sections beyond these are fine — real epics grow phase tables, hour totals,
and cross-epic notes. The four above are the ones a reader expects to find.
-->
