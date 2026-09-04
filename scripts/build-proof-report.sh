#!/usr/bin/env bash
# build-proof-report.sh — Assembles the HTML proof report
#
# Extends or creates verification-report/index.html with:
#   - Embedded annotated screenshots (base64 inline)
#   - Checklist results as interactive table
#   - Bug matrix as filterable table
#   - Asciinema player embeds for terminal recordings
#
# The report is self-contained (no external deps except asciinema CDN).
# Also writes verification-report/index.html for permanent reference.
#
# Usage:
#   ./scripts/build-proof-report.sh [--proof-dir DIR] [--checklist PATH]
#                                    [--bug-matrix PATH] [--timestamp TS]
#                                    [--output PATH]
#
# Run from the repository root.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
PROOF_DIR=""
CHECKLIST=""
BUG_MATRIX=""
OUTPUT_PATH=""

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proof-dir)   PROOF_DIR="$2"; shift 2 ;;
    --checklist)   CHECKLIST="$2"; shift 2 ;;
    --bug-matrix)  BUG_MATRIX="$2"; shift 2 ;;
    --timestamp)   TIMESTAMP="$2"; shift 2 ;;
    --output)      OUTPUT_PATH="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Resolve proof dir — default to latest timestamped subdirectory
if [[ -z "$PROOF_DIR" ]]; then
  PROOF_DIR=$(ls -td "$REPO_ROOT/verification-report/proof"/*/ 2>/dev/null | head -1 | sed 's|/$||' || true)
fi

# Resolve checklist and bug matrix — fall back to canonical locations
if [[ -z "$CHECKLIST" ]]; then
  if [[ -f "$PROOF_DIR/checklist-results.json" ]]; then
    CHECKLIST="$PROOF_DIR/checklist-results.json"
  else
    CHECKLIST="$REPO_ROOT/verification-report/checklist.json"
  fi
fi

if [[ -z "$BUG_MATRIX" ]]; then
  if [[ -f "$PROOF_DIR/bug-matrix-results.json" ]]; then
    BUG_MATRIX="$PROOF_DIR/bug-matrix-results.json"
  else
    BUG_MATRIX="$REPO_ROOT/verification-report/bug-matrix.json"
  fi
fi

# Primary output: timestamped proof dir; also written to index.html for permalink
if [[ -n "$PROOF_DIR" ]]; then
  mkdir -p "$PROOF_DIR"
  REPORT_OUT="${OUTPUT_PATH:-$PROOF_DIR/proof-report.html}"
else
  REPORT_OUT="${OUTPUT_PATH:-$REPO_ROOT/verification-report/index.html}"
fi
INDEX_HTML="$REPO_ROOT/verification-report/index.html"

echo "Building proof report..."
echo "  Proof dir:  ${PROOF_DIR:-none}"
echo "  Checklist:  $CHECKLIST"
echo "  Bug matrix: $BUG_MATRIX"
echo "  Output:     $REPORT_OUT"

# ---------------------------------------------------------------------------
# Generate report via Python — all data passed as argv, no env var expansion
# ---------------------------------------------------------------------------
export PROOF_DIR CHECKLIST BUG_MATRIX TIMESTAMP REPO_ROOT REPORT_OUT INDEX_HTML

python3 - <<'PYEOF'
import json, os, sys, base64, glob, html as html_mod

proof_dir    = os.environ["PROOF_DIR"]
checklist_p  = os.environ["CHECKLIST"]
bug_matrix_p = os.environ["BUG_MATRIX"]
timestamp    = os.environ["TIMESTAMP"]
repo_root    = os.environ["REPO_ROOT"]
report_out   = os.environ["REPORT_OUT"]
index_html   = os.environ["INDEX_HTML"]

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
checklist = {}
if os.path.exists(checklist_p):
    try:
        with open(checklist_p) as f:
            checklist = json.load(f)
    except Exception as e:
        print(f"  WARNING: could not load checklist: {e}")

bug_matrix = {}
if os.path.exists(bug_matrix_p):
    try:
        with open(bug_matrix_p) as f:
            bug_matrix = json.load(f)
    except Exception as e:
        print(f"  WARNING: could not load bug matrix: {e}")

# Collect screenshots and recordings
# PNGs may be directly in proof_dir, in timestamped subdirs, or in annotated/screenshots subdirs
screenshots = []
if proof_dir:
    # First: try annotated/ and screenshots/ subdirs (legacy layout)
    screenshots = sorted(glob.glob(os.path.join(proof_dir, "annotated", "*.png")))
    if not screenshots:
        screenshots = sorted(glob.glob(os.path.join(proof_dir, "screenshots", "*.png")))
    if not screenshots:
        # Current layout: PNGs live directly in proof_dir and/or timestamped subdirs
        screenshots = sorted(glob.glob(os.path.join(proof_dir, "**", "*.png"), recursive=True))
    if not screenshots:
        # Also check parent proof dir for PNGs not in subdirectories
        parent = os.path.dirname(proof_dir) if proof_dir else ""
        if parent:
            screenshots = sorted(glob.glob(os.path.join(parent, "*.png")))

recordings = []
if proof_dir:
    recordings = sorted(
        glob.glob(os.path.join(proof_dir, "recordings", "*.cast")) +
        glob.glob(os.path.join(proof_dir, "recordings", "*.webm"))
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def img_b64(path):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return ""

def esc(s):
    return html_mod.escape(str(s))

def checklist_table_html(data):
    rows = []
    for sub in data.get("subsystems", []):
        sub_name = esc(sub.get("name", ""))
        for item in sub.get("items", []):
            st = item.get("status", "pending")
            color = {"pass": "#3fb950", "fail": "#f85149", "skip": "#8b949e"}.get(st, "#d29922")
            badge = f'<span style="color:{color};font-weight:bold">{esc(st.upper())}</span>'
            rows.append(
                f"<tr>"
                f"<td>{sub_name}</td>"
                f"<td><code>{esc(item.get('id',''))}</code></td>"
                f"<td>{esc(item.get('description',''))}</td>"
                f"<td>{badge}</td>"
                f"<td>{esc(str(item.get('actual',''))[:80])}</td>"
                f"</tr>"
            )
    return "\n".join(rows) if rows else "<tr><td colspan='5' style='color:#8b949e'>No checklist data</td></tr>"

def bug_table_html(data):
    rows = []
    for bug in data.get("bugs", []):
        sev = bug.get("severity", "medium")
        sev_color = {
            "critical": "#f85149",
            "high":     "#f0883e",
            "medium":   "#d29922",
            "low":      "#8b949e",
        }.get(sev, "#8b949e")
        st = bug.get("status", "open")
        st_color = {
            "fixed":    "#3fb950",
            "verified": "#3fb950",
            "open":     "#f85149",
            "fixing":   "#d29922",
        }.get(st, "#8b949e")
        rows.append(
            f"<tr>"
            f"<td><code>{esc(bug.get('id',''))}</code></td>"
            f"<td>{esc(bug.get('component',''))}</td>"
            f"<td><span style='color:{sev_color}'>{esc(sev.upper())}</span></td>"
            f"<td>{esc(str(bug.get('title',''))[:80])}</td>"
            f"<td><span style='color:{st_color}'>{esc(st.upper())}</span></td>"
            f"</tr>"
        )
    return "\n".join(rows) if rows else "<tr><td colspan='5' style='color:#8b949e'>No bugs found</td></tr>"

def screenshot_gallery_html(paths):
    if not paths:
        return "<p style='color:#8b949e'>No screenshots available</p>"
    items = []
    for p in paths:
        name = esc(os.path.basename(p))
        b64 = img_b64(p)
        if b64:
            items.append(
                f'<div style="margin:8px;display:inline-block;vertical-align:top;max-width:420px">'
                f'<p style="color:#8b949e;font-size:12px;margin-bottom:4px">{name}</p>'
                f'<img src="data:image/png;base64,{b64}" '
                f'style="max-width:420px;border:1px solid #30363d;border-radius:6px" '
                f'alt="{name}"/></div>'
            )
        else:
            items.append(
                f'<div style="margin:8px;color:#8b949e">'
                f'<p>{name} (file not accessible)</p></div>'
            )
    return "\n".join(items)

def recording_embeds_html(paths):
    if not paths:
        return "<p style='color:#8b949e'>No recordings available</p>"
    items = []
    for p in paths:
        name = esc(os.path.basename(p))
        rel  = os.path.relpath(p, os.path.dirname(report_out)) if proof_dir else p
        safe_id = "player_" + os.path.basename(p).replace(".", "_").replace("-", "_")
        if p.endswith(".cast"):
            items.append(
                f'<div style="margin:16px 0">'
                f'<p style="color:#8b949e;font-size:14px;margin-bottom:8px">{name}</p>'
                f'<div id="{safe_id}"></div>'
                f'<script>'
                f"AsciinemaPlayer.create('{rel}',document.getElementById('{safe_id}'),"
                f"{{cols:120,rows:30,autoPlay:false}});"
                f'</script></div>'
            )
        elif p.endswith(".webm"):
            items.append(
                f'<div style="margin:16px 0">'
                f'<p style="color:#8b949e;font-size:14px;margin-bottom:8px">{name}</p>'
                f'<video controls style="max-width:100%;border:1px solid #30363d;border-radius:6px">'
                f'<source src="{rel}" type="video/webm"/></video></div>'
            )
    return "\n".join(items)

# ---------------------------------------------------------------------------
# Summary stats for header
# ---------------------------------------------------------------------------
# Compute pass/fail/total by iterating actual checklist items —
# run-checklist.sh doesn't write a "summary" key so we can't rely on it
all_items = [item for sub in checklist.get("subsystems", []) for item in sub.get("items", [])]
cl_pass  = sum(1 for i in all_items if i.get("status") == "pass")
cl_fail  = sum(1 for i in all_items if i.get("status") == "fail")
cl_total = sum(1 for i in all_items if i.get("type") == "programmatic")
# If no items are tagged as programmatic, fall back to all items
if cl_total == 0:
    cl_total = len(all_items)

bm_summary = bug_matrix.get("summary", {})
open_bugs     = [b for b in bug_matrix.get("bugs", []) if b.get("status") == "open"]
critical_high = sum(1 for b in open_bugs if b.get("severity") in ("critical", "high"))
# Fall back to summary key if present and no explicit open bugs computed
if not open_bugs and bm_summary:
    critical_high = bm_summary.get("critical", 0) + bm_summary.get("high", 0)

gate_ok    = cl_fail == 0 and critical_high == 0
gate_label = "PASS" if gate_ok else "FAIL"
gate_color = "#3fb950" if gate_ok else "#f85149"
ch_color   = "#f85149" if critical_high > 0 else "#3fb950"

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>fulcrumaxe — E2E Proof Report ({timestamp})</title>
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/asciinema-player@3.6.3/dist/bundle/asciinema-player.min.css"/>
  <style>
    *, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0; }}
    :root {{
      --bg:#0d1117; --surface:#161b22; --surface2:#21262d;
      --border:#30363d; --accent:#58a6ff; --text:#e6edf3; --muted:#8b949e;
    }}
    body {{
      background:var(--bg); color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      padding:24px; max-width:1200px; margin:0 auto;
    }}
    h1 {{ font-size:24px; margin-bottom:8px; color:var(--accent); }}
    h2 {{
      font-size:18px; margin:32px 0 12px;
      border-bottom:1px solid var(--border); padding-bottom:6px;
    }}
    .meta {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
    .gate {{
      font-size:28px; font-weight:bold; color:{gate_color};
      margin:16px 0; padding:12px 20px;
      background:var(--surface); border:1px solid var(--border);
      border-radius:8px; display:inline-block;
    }}
    .stats {{ display:flex; gap:16px; margin:16px 0 32px; flex-wrap:wrap; }}
    .stat {{
      background:var(--surface); border:1px solid var(--border);
      border-radius:8px; padding:12px 20px; min-width:140px;
    }}
    .stat-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; }}
    .stat-value {{ font-size:24px; font-weight:bold; margin-top:4px; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
    th, td {{ padding:8px 12px; border:1px solid var(--border); text-align:left; }}
    th {{ background:var(--surface2); color:var(--muted); font-weight:600; }}
    tbody tr:nth-child(even) {{ background:var(--surface); }}
    tbody tr:hover {{ background:var(--surface2); }}
    .section {{ margin-bottom:48px; }}
    .screenshots {{ display:flex; flex-wrap:wrap; gap:12px; margin-top:8px; }}
    code {{ background:var(--surface2); padding:2px 6px; border-radius:4px; font-size:12px; }}
    a {{ color:var(--accent); text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    .filter-input {{
      background:var(--surface2); border:1px solid var(--border); color:var(--text);
      padding:6px 12px; border-radius:6px; font-size:13px; width:300px; margin-bottom:8px;
    }}
    .filter-input:focus {{ outline:none; border-color:var(--accent); }}
  </style>
</head>
<body>
  <h1>fulcrumaxe — E2E Proof Report</h1>
  <p class="meta">Generated: {timestamp}
    {f'| Proof directory: <code>{esc(proof_dir)}</code>' if proof_dir else ''}
  </p>

  <div class="gate">Gate verdict: {gate_label}</div>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">Checklist pass</div>
      <div class="stat-value" style="color:#3fb950">{cl_pass}/{cl_total}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Checklist fail</div>
      <div class="stat-value" style="color:{('#f85149' if cl_fail > 0 else '#8b949e')}">{cl_fail}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Critical/High bugs</div>
      <div class="stat-value" style="color:{ch_color}">{critical_high}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Total open bugs</div>
      <div class="stat-value">{len(open_bugs) if open_bugs else bm_summary.get('total_open', 0)}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Screenshots</div>
      <div class="stat-value">{len(screenshots)}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Recordings</div>
      <div class="stat-value">{len(recordings)}</div>
    </div>
  </div>

  <div class="section">
    <h2>Checklist Results</h2>
    <input class="filter-input" type="text" id="cl-filter"
           placeholder="Filter by subsystem, ID, or status..."
           oninput="filterTable('cl-table', this.value)"/>
    <table id="cl-table">
      <thead>
        <tr>
          <th>Subsystem</th><th>ID</th><th>Description</th><th>Status</th><th>Actual</th>
        </tr>
      </thead>
      <tbody>
        {checklist_table_html(checklist)}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>Bug Matrix ({len(open_bugs) if open_bugs else bm_summary.get('total_open', len(bug_matrix.get('bugs', [])))} open)</h2>
    <input class="filter-input" type="text" id="bug-filter"
           placeholder="Filter by component, severity, or status..."
           oninput="filterTable('bug-table', this.value)"/>
    <table id="bug-table">
      <thead>
        <tr>
          <th>ID</th><th>Component</th><th>Severity</th><th>Title</th><th>Status</th>
        </tr>
      </thead>
      <tbody>
        {bug_table_html(bug_matrix)}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>Annotated Screenshots ({len(screenshots)})</h2>
    <div class="screenshots">
      {screenshot_gallery_html(screenshots)}
    </div>
  </div>

  <div class="section">
    <h2>Terminal Recordings ({len(recordings)})</h2>
    {recording_embeds_html(recordings)}
  </div>

  <script>
    function filterTable(tableId, query) {{
      const q = query.toLowerCase();
      const rows = document.getElementById(tableId).querySelectorAll('tbody tr');
      rows.forEach(function(row) {{
        row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
      }});
    }}
  </script>
  <script src="https://cdn.jsdelivr.net/npm/asciinema-player@3.6.3/dist/bundle/asciinema-player.min.js"></script>
</body>
</html>"""

# Write primary output
os.makedirs(os.path.dirname(os.path.abspath(report_out)), exist_ok=True)
with open(report_out, "w") as f:
    f.write(html)
print(f"Report written: {report_out}")

# Also write/update index.html as the permanent link (unless already the target)
if os.path.abspath(report_out) != os.path.abspath(index_html):
    os.makedirs(os.path.dirname(os.path.abspath(index_html)), exist_ok=True)
    with open(index_html, "w") as f:
        f.write(html)
    print(f"Permalink updated: {index_html}")
PYEOF
