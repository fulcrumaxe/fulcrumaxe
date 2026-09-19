---
# Every field below is read by backend/task_file.py's schema-v1 validator.
# Delete the comments once you have filled the values in — they are here for
# the first read, not for the file's whole life. Validate with:
#   python3 backend/task_file.py validate <this file>
#
# schema_version   always 1 for a new task file.
schema_version: 1
# epic / task    the epic number and this task's ID within it. `epic` must
#                match the number in the directory name and in epic.md's H1.
#                `task` must match this file's basename (rename this file
#                to your task's ID, e.g. H08.md, and set task: H08 here) and
#                contain only letters, digits and dashes. Together they
#                become the Discussion title and the key that other tasks'
#                `depends_on` resolves against.
epic: 1
task: NN
# title          one line, no trailing period. Quote it — titles with a colon
#                or a leading bracket are not valid unquoted YAML.
title: "<one line — what this task delivers>"
# type           feature | bug | doc | infra | process | security
type: feature
# status         draft | ready | superseded | completed — authoring states,
#                not run states. `ready` is the point a task is considered
#                import-ready.
status: draft
# estimated_hours  a number > 0 and <= 40, including tests. If it is over
#                about 12, it is probably two tasks.
estimated_hours: 4
# complexity_points  1, 2, 3, 5 or 8.
complexity_points: 3
# planned_prs    how many PRs this task expects. 0 means operational work
#                with no PR — set planned_prs_reason instead of
#                acceptance_files below in that case.
planned_prs: 1
# planned_prs_reason  required only when planned_prs is 0.
# planned_prs_reason: "<why this task has no PR>"
# milestone      stage-1 | stage-2 | launch | post-launch
milestone: stage-1
# security_review  true or false.
security_review: false
# depends_on     each item is a same-epic task ID (H08), a cross-epic
#                <epic>.<task> id (2.H13), a Discussion (D#123), or an Issue
#                (#123). [] if none.
depends_on: []
# acceptance_files  required when status is ready and planned_prs is >= 1 —
#                a non-empty list of repo-relative paths that show the work
#                is done.
# acceptance_files: []
# tags           free-form. Keep epic-<N> first so a tag search finds the
#                whole epic, then the areas this task touches.
tags: [epic-1, area]
---

# Task: <same title as the frontmatter>

## Overview

**Type:** <Feature | Enhancement | Bugfix | Refactoring | Testing | Spec> Task
**Target:** <one or two sentences: the outcome, stated as something a user or
operator can observe. Not "add a module" — "the operator can see which sessions
are active and end one of them".>
**Location:** `<primary file>`, `<second file>`

---

## 1. Assessment

<Both lists are checkboxes on purpose. "What exists now" is checked for things
that are already true and unchecked for gaps — so the same list shows the
starting position and what is missing, and a reader can tell at a glance
whether the task has been re-scoped since it was written.>

**What exists now:**
- [x] <something that already works, and is load-bearing for this task>
- [ ] <a gap — something the task description assumes but that is absent>
- [ ] <another gap>

**What needs to be done:**
- [ ] <a concrete deliverable — a table, an endpoint, a screen, a check>
- [ ] <another, with enough detail that someone else could build it>
- [ ] <keep going until the list covers the whole task>

---

## 2. Implementation Plan

**Approach:**
1. <first step>
2. <second step>
3. <...>

**Files to create:**
- [ ] `<path>` — <what goes in it>

**Files to modify:**
- [ ] `<path>` — <what changes and why>

---

## 3. Acceptance Criteria

<Numbered, checkable, and phrased as observable outcomes. Each one should be
something a reviewer can verify by running the thing, not by reading the diff.
If a criterion can only be confirmed by inspecting code, rewrite it.>

**Done when:**
1. [ ] <observable outcome>
2. [ ] <observable outcome>
3. [ ] <observable outcome>

---

## 4. Implementation Notes

<Advisory, not binding. The place for the things that are true about this task
but do not belong in the criteria: a decision already made and why, an
approach that looks obvious and is wrong, a constraint from elsewhere in the
system, a gotcha found while writing the task.

Delete this section rather than leaving it empty.>

---

**Status:** <Not Started | In Progress | Complete>
