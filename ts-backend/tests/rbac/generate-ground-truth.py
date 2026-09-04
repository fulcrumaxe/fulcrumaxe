#!/usr/bin/env python3
"""
Generate ground-truth fnmatch results for the differential fuzz harness.

Outputs a JSON array of {pattern, path, expected} objects to stdout.
"""

import fnmatch
import json

# Build the test corpus
# Each entry: (pattern, path)
pairs: list[tuple[str, str]] = []

# ---- Spec-required edge cases ----

# * crossing /
star_crossing = [
    ("*", "/agents/x/y/z"),
    ("*", "/a/b/c/d/e"),
    ("*", ""),
    ("*", "/"),
    ("*", "GET /anything"),
    ("/agents/*", "/agents/x"),
    ("/agents/*", "/agents/x/y/z"),
    ("/agents/*", "/agents/"),
    ("/agents/*", "/agents"),
    ("/agents/*", "/agents/foo/bar/baz"),
    ("/a/b/*", "/a/b/c"),
    ("/a/b/*", "/a/b/c/d/e"),
    ("/a/b/*", "/a/b/"),
    ("/a/b/*", "/a/b"),
    ("GET *", "GET /agents/x"),
    ("GET *", "GET /"),
    ("GET *", "GET "),
    ("GET *", "POST /x"),
    ("GET /agents/*", "GET /agents/x"),
    ("GET /agents/*", "GET /agents/x/y/z"),
    ("GET /registry/*", "GET /registry/foo"),
    ("GET /registry/*", "GET /registry/foo/bar"),
]
pairs.extend(star_crossing)

# Dotfiles / leading dot
dotfile_pairs = [
    ("*", ".hidden"),
    ("*", "."),
    ("*.py", ".hidden.py"),
    (".*", ".hidden"),
    (".*", "not-hidden"),
    (".hidden", ".hidden"),
    (".hidden", "not-hidden"),
    ("*hidden*", ".hidden"),
]
pairs.extend(dotfile_pairs)

# ? wildcard
question_pairs = [
    ("/a/?/c", "/a/b/c"),
    ("/a/?/c", "/a/bb/c"),
    ("/a/?/c", "/a//c"),
    ("/a/?/c", "/a/b/c/d"),
    ("?", "a"),
    ("?", "ab"),
    ("?", ""),
    ("?", "/"),
    ("GET/?", "GET/x"),
    ("GET/?", "GET/xy"),
]
pairs.extend(question_pairs)

# Character classes [seq]
char_class_pairs = [
    ("[abc]", "a"),
    ("[abc]", "b"),
    ("[abc]", "c"),
    ("[abc]", "d"),
    ("[abc]/*", "a/x"),
    ("[abc]/*", "d/x"),
    ("[!abc]", "d"),
    ("[!abc]", "a"),
    ("[!abc]/*", "d/x"),
    ("[!abc]/*", "a/x"),
    ("[a-z]", "b"),
    ("[a-z]", "B"),
    ("[a-z]", "z"),
    ("[a-z]", "a"),
    ("[A-Z]", "B"),
    ("[A-Z]", "b"),
    ("[a-zA-Z]", "X"),
    ("[a-zA-Z]", "1"),
    ("[!a-z]", "B"),
    ("[!a-z]", "b"),
    ("[0-9]", "5"),
    ("[0-9]", "a"),
    ("[a-z0-9]", "a"),
    ("[a-z0-9]", "5"),
    ("[a-z0-9]", "A"),
    # Negated range
    ("[!0-9]", "a"),
    ("[!0-9]", "5"),
]
pairs.extend(char_class_pairs)

# Literal special chars
literal_pairs = [
    ("GET /health", "GET /health"),
    ("GET /health", "GET /health/"),
    ("GET /health", "get /health"),  # case sensitivity
    ("GET /metrics", "GET /metrics"),
    ("POST /budget/init", "POST /budget/init"),
    ("POST /budget/init", "GET /budget/init"),
    # Method prefix rules from rbac.py
    ("GET /health/*", "GET /health/loop"),
    ("GET /health/*", "GET /health/modules"),
    ("GET /health/*", "GET /health/"),
    ("GET /kpi/*", "GET /kpi/status"),
    ("GET /kpi/*", "GET /kpi/summary/v2"),
    ("GET /stream/*", "GET /stream/events"),
    ("GET /replays/*", "GET /replays/2024-01-01"),
    ("GET /budget/*", "GET /budget/status"),
    ("GET /budget/*", "GET /budget/history/month"),
]
pairs.extend(literal_pairs)

# Empty pattern / empty path
empty_pairs = [
    ("", ""),
    ("", "x"),
    ("*", ""),
    ("?", ""),
    ("/", "/"),
    ("/", ""),
]
pairs.extend(empty_pairs)

# Trailing slash edge cases
slash_pairs = [
    ("/agents/*", "/agents/"),
    ("/agents/*/", "/agents/x/"),
    ("/agents/*/", "/agents/x"),
    ("/a/*", "/a/"),
    ("/a/*", "/a"),
]
pairs.extend(slash_pairs)

# Consecutive / nested wildcards
nested_star = [
    ("*/*", "a/b"),
    ("*/*", "a/b/c"),
    ("*/*", "ab"),
    ("*/*", "/a/b"),
    ("**", "a/b/c"),  # ** in fnmatch is just two consecutive stars = same as *
    ("*/*/x", "a/b/x"),
    ("*/*/x", "a/b/c/x"),
]
pairs.extend(nested_star)

# Bracket edge cases
bracket_edge = [
    ("[", "["),    # unclosed bracket → literal [
    ("]", "]"),    # literal ]
    ("[]abc]", "]"),
    ("[]abc]", "a"),
    ("[!]abc]", "x"),
    ("[!]abc]", "]"),
]
pairs.extend(bracket_edge)

# Fuzz: cross product of common patterns and paths from rbac.py
rbac_patterns = [
    "*",
    "GET /health",
    "GET /health/*",
    "GET /metrics",
    "GET /budget/*",
    "GET /registry",
    "GET /registry/*",
    "GET /agents",
    "GET /agents/*",
    "GET /kpi",
    "GET /kpi/*",
    "GET /stream/*",
    "GET /replays",
    "GET /replays/*",
    "GET /rbac/whoami",
    "POST /budget/init",
    "GET *",
]
rbac_paths = [
    "GET /health",
    "GET /health/loop",
    "GET /health/modules",
    "GET /metrics",
    "GET /budget/status",
    "GET /budget/history",
    "GET /budget/history/month",
    "GET /registry",
    "GET /registry/agent1",
    "GET /agents",
    "GET /agents/abc",
    "GET /agents/abc/status",
    "GET /kpi",
    "GET /kpi/summary",
    "GET /kpi/summary/v2",
    "GET /stream/events",
    "GET /stream/updates/live",
    "GET /replays",
    "GET /replays/2024-01-01",
    "GET /rbac/whoami",
    "POST /budget/init",
    "POST /budget/status",
    "DELETE /agents/abc",
    "GET /unknown",
    "GET /",
    "POST /",
]
for pat in rbac_patterns:
    for path in rbac_paths:
        pairs.append((pat, path))

# Generate results
results = []
seen = set()
for pat, path in pairs:
    key = (pat, path)
    if key in seen:
        continue
    seen.add(key)
    expected = fnmatch.fnmatch(path, pat)
    results.append({"pattern": pat, "path": path, "expected": expected})

print(json.dumps(results, indent=2))
