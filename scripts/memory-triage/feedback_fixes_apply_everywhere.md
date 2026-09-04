---
name: Fixes and improvements apply to all subsystems — not just the one that broke
description: When fixing a pattern (verification, data quality, rendering), apply the same fix to Python API, SaaS product, TUI, and dashboard — not just the subsystem where the bug was found
type: feedback
originSessionId: 85514482-6eda-41bb-baf3-45fb37863d1a
tier: transferable
---
When a bug pattern is found in one subsystem, the fix should be applied across all subsystems that have the same pattern.

**Why:** The user pointed out that dashboard data quality fixes, content assertions, Puppeteer rendering checks, and human verification should apply to the SaaS product and TUI too — not just the Python backend dashboard. Fixing one subsystem while leaving the same problem in others is incomplete work.

**How to apply:** When creating Discussions or specs for fixes, explicitly scope them to ALL applicable subsystems: Python backend API + dashboard, SaaS Rust service, TUI, and the React dashboard. If a content assertion pattern works for the Python API, write the equivalent for the Rust API. If Puppeteer checks work for the backend dashboard, write them for the React dashboard too.
