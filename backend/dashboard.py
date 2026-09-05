"""
dashboard.py — HTML page generator for the autonomous-forever dashboard.

Returns a self-contained HTML/CSS/JS page served at GET /dashboard by api.py.
Zero external dependencies — works offline.
"""

from __future__ import annotations


def get_dashboard_html() -> str:
    """Return the full HTML dashboard page as a UTF-8 string."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>autonomous-forever dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: #1a1a2e;
      color: #e0e0e0;
      font-family: 'Segoe UI', system-ui, sans-serif;
      font-size: 14px;
      min-height: 100vh;
      padding: 24px;
    }

    header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 24px;
    }

    header h1 {
      font-size: 20px;
      font-weight: 600;
      letter-spacing: 0.02em;
      color: #c9d1d9;
    }

    #conn-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #444;
      flex-shrink: 0;
      transition: background 0.3s;
    }
    #conn-dot.ok  { background: #3fb950; }
    #conn-dot.err { background: #f85149; }

    #conn-label {
      font-size: 12px;
      color: #8b949e;
    }

    .grid {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
    }

    .card {
      background: #16213e;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 20px;
      flex: 1 1 260px;
      min-width: 220px;
    }

    .card h2 {
      font-size: 13px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #8b949e;
      margin-bottom: 16px;
    }

    /* Budget card */
    .bar-track {
      background: #0d1117;
      border-radius: 4px;
      height: 10px;
      overflow: hidden;
      margin-bottom: 8px;
    }
    .bar-fill {
      height: 100%;
      border-radius: 4px;
      transition: width 0.5s ease, background 0.3s;
      background: #3fb950;
    }
    .bar-fill.warn { background: #d29922; }
    .bar-fill.crit { background: #f85149; }

    .budget-row {
      display: flex;
      justify-content: space-between;
      font-size: 13px;
      color: #c9d1d9;
    }

    /* Queue card */
    .stat-row {
      display: flex;
      justify-content: space-between;
      padding: 5px 0;
      border-bottom: 1px solid #21262d;
      font-size: 13px;
    }
    .stat-row:last-child { border-bottom: none; }
    .stat-row .label { color: #8b949e; }
    .stat-row .value { font-weight: 600; color: #c9d1d9; }

    /* Dependencies card */
    .deps-hub-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      margin-top: 8px;
    }
    .deps-hub-table th {
      text-align: left;
      color: #8b949e;
      font-weight: 600;
      padding: 2px 4px;
      border-bottom: 1px solid #21262d;
    }
    .deps-hub-table td {
      padding: 3px 4px;
      color: #c9d1d9;
      font-family: 'Courier New', monospace;
      font-size: 11px;
    }
    .deps-hub-table tr:hover td { background: #21262d; }
    .deps-cycle-warn { color: #f85149; font-size: 12px; font-weight: 600; }

    /* Code Quality card */
    .quality-bar-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 4px;
      font-size: 12px;
    }
    .quality-bar-row .pr-label { width: 40px; color: #8b949e; flex-shrink: 0; }
    .quality-bar-track {
      flex: 1;
      background: #0d1117;
      border-radius: 3px;
      height: 8px;
      overflow: hidden;
    }
    .quality-bar-fill {
      height: 100%;
      border-radius: 3px;
      transition: width 0.4s ease;
    }
    .quality-bar-fill.grade-a  { background: #3fb950; }
    .quality-bar-fill.grade-b  { background: #d29922; }
    .quality-bar-fill.grade-c  { background: #f0883e; }
    .quality-bar-fill.grade-df { background: #f85149; }
    .quality-bar-row .score-label { width: 32px; text-align: right; color: #c9d1d9; flex-shrink: 0; }
    .quality-weakest { font-size: 11px; color: #8b949e; margin-top: 8px; }

    /* Loop Health card */
    .mono { font-family: 'Courier New', monospace; font-size: 12px; }

    /* Agents card */
    .agent-list {
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .agent-list li {
      background: #0d1117;
      border-radius: 4px;
      padding: 6px 10px;
      font-size: 12px;
      color: #c9d1d9;
    }

    /* Module Health card */
    .module-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .module-dot {
      padding: 3px 7px;
      border-radius: 3px;
      font-size: 11px;
      font-family: 'Courier New', monospace;
      cursor: default;
      color: #0d1117;
      background: #3fb950;
      position: relative;
    }
    .module-dot.failed {
      background: #f85149;
      color: #fff;
    }
    .module-tooltip {
      display: none;
      position: absolute;
      left: 0;
      top: 100%;
      z-index: 10;
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 4px;
      padding: 6px 8px;
      font-size: 11px;
      white-space: pre-wrap;
      max-width: 340px;
      color: #f85149;
      margin-top: 2px;
    }
    .module-dot:hover .module-tooltip { display: block; }
    .module-health-summary {
      font-size: 12px;
      color: #8b949e;
      margin-bottom: 10px;
    }
    .module-health-summary .ok  { color: #3fb950; font-weight: 600; }
    .module-health-summary .err { color: #f85149; font-weight: 600; }

    .placeholder { color: #484f58; font-size: 12px; font-style: italic; }

    footer {
      margin-top: 24px;
      font-size: 11px;
      color: #484f58;
      text-align: right;
    }
  </style>
</head>
<body>
  <header>
    <div id="conn-dot"></div>
    <h1>autonomous-forever dashboard</h1>
    <span id="conn-label">connecting…</span>
  </header>

  <div class="grid">

    <!-- Budget -->
    <div class="card" id="card-budget">
      <h2>Budget</h2>
      <div class="bar-track"><div class="bar-fill" id="budget-bar" style="width:0%"></div></div>
      <div class="budget-row">
        <span id="budget-spent">—</span>
        <span id="budget-ceiling">—</span>
      </div>
    </div>

    <!-- Queue -->
    <div class="card" id="card-queue">
      <h2>Queue</h2>
      <div class="stat-row"><span class="label">Total discussions</span><span class="value" id="q-total">—</span></div>
      <div class="stat-row"><span class="label">Done</span><span class="value" id="q-done">—</span></div>
      <div class="stat-row"><span class="label">In progress</span><span class="value" id="q-inprogress">—</span></div>
      <div class="stat-row"><span class="label">Spec ready</span><span class="value" id="q-specready">—</span></div>
    </div>

    <!-- KPI -->
    <div class="card" id="card-kpi">
      <h2>KPI</h2>
      <div class="stat-row"><span class="label">Done (24h)</span><span class="value" id="kpi-24h">--</span></div>
      <div class="stat-row"><span class="label">Tasks/day</span><span class="value" id="kpi-velocity">--</span></div>
      <div class="stat-row"><span class="label">Cycle time</span><span class="value" id="kpi-cycle">--</span></div>
      <div class="stat-row"><span class="label">Idle rate</span><span class="value" id="kpi-idle">--</span></div>
    </div>

    <!-- Loop Health -->
    <div class="card" id="card-loop">
      <h2>Loop Health</h2>
      <div class="stat-row">
        <span class="label">Last run</span>
        <span class="value mono" id="loop-last">—</span>
      </div>
      <div class="stat-row">
        <span class="label">Duration</span>
        <span class="value mono" id="loop-duration">—</span>
      </div>
      <div class="stat-row">
        <span class="label">Idle rate</span>
        <span class="value" id="loop-idle">—</span>
      </div>
    </div>

    <!-- Agents -->
    <div class="card" id="card-agents">
      <h2>Agents</h2>
      <ul class="agent-list" id="agent-list">
        <li class="placeholder">loading…</li>
      </ul>
    </div>

    <!-- Module Health -->
    <div class="card" id="card-module-health">
      <h2>Module Health</h2>
      <div class="module-health-summary" id="module-health-summary">loading…</div>
      <div class="module-grid" id="module-grid">
        <span class="placeholder">checking…</span>
      </div>
    </div>

    <!-- Dependencies -->
    <div class="card" id="card-deps">
      <h2>Dependencies</h2>
      <div class="stat-row"><span class="label">Modules</span><span class="value" id="deps-modules">—</span></div>
      <div class="stat-row"><span class="label">Edges</span><span class="value" id="deps-edges">—</span></div>
      <div class="stat-row"><span class="label">Cycles</span><span class="value" id="deps-cycles">—</span></div>
      <table class="deps-hub-table" id="deps-hub-table">
        <thead><tr><th>Hub module</th><th>In</th><th>Out</th></tr></thead>
        <tbody id="deps-hub-body"><tr><td colspan="3" class="placeholder">loading…</td></tr></tbody>
      </table>
    </div>

    <!-- Code Quality -->
    <div class="card" id="card-quality">
      <h2>Code Quality</h2>
      <div id="quality-bars"><span class="placeholder">loading…</span></div>
      <div class="quality-weakest" id="quality-weakest"></div>
    </div>

    <!-- Agent Memory -->
    <div class="card" id="card-memory">
      <h2>Agent Memory</h2>
      <div class="stat-row"><span class="label">Total lessons</span><span class="value" id="mem-total">—</span></div>
      <div class="stat-row"><span class="label">Failures</span><span class="value" id="mem-failure">—</span></div>
      <div class="stat-row"><span class="label">Successes</span><span class="value" id="mem-success">—</span></div>
      <div class="stat-row"><span class="label">Patterns</span><span class="value" id="mem-pattern">—</span></div>
      <div class="stat-row"><span class="label">Sessions</span><span class="value" id="mem-sessions">—</span></div>
    </div>

  </div>

  <footer id="last-updated">Last updated: —</footer>

  <script>
    const dot   = document.getElementById('conn-dot');
    const label = document.getElementById('conn-label');

    function setConn(ok) {
      dot.className   = ok ? 'ok' : 'err';
      label.textContent = ok ? 'connected' : 'disconnected';
    }

    async function safeFetch(url) {
      try {
        const r = await fetch(url);
        if (!r.ok) return null;
        return await r.json();
      } catch (_) {
        return null;
      }
    }

    function setText(id, val) {
      const el = document.getElementById(id);
      if (el) el.textContent = val ?? '—';
    }

    function updateBudget(data) {
      if (!data) return;
      const spent   = data.spent   ?? data.tokens_used   ?? 0;
      const ceiling = data.ceiling ?? data.token_ceiling ?? 0;
      const pct     = ceiling > 0 ? Math.min(100, (spent / ceiling) * 100) : 0;

      const bar = document.getElementById('budget-bar');
      bar.style.width = pct.toFixed(1) + '%';
      bar.className = 'bar-fill' + (pct >= 80 ? ' crit' : pct >= 60 ? ' warn' : '');

      setText('budget-spent',   'Spent: ' + spent.toLocaleString());
      setText('budget-ceiling', 'Ceiling: ' + (ceiling > 0 ? ceiling.toLocaleString() : 'unlimited'));
    }

    function updateQueue(data) {
      if (!data) return;
      // /registry/stats returns keys like total, done, in_progress, spec_ready
      setText('q-total',     data.total      ?? '—');
      setText('q-done',      data.done       ?? '—');
      setText('q-inprogress',data.in_progress ?? data.implementing ?? '—');
      setText('q-specready', data.spec_ready  ?? data.ready ?? '—');
    }

    function updateLoop(data) {
      if (!data) return;
      // /health may expose loop_* keys; fall back gracefully
      const last = data.loop_last_run ?? data.last_run ?? null;
      setText('loop-last', last ? new Date(last).toLocaleTimeString() : 'n/a');
      const dur = data.loop_duration_s ?? data.duration_s ?? null;
      setText('loop-duration', dur !== null ? dur + 's' : 'n/a');
      const idle = data.loop_idle_rate ?? data.idle_rate ?? null;
      setText('loop-idle', idle !== null ? (idle * 100).toFixed(0) + '%' : 'n/a');
    }

    function updateKPI(data) {
      if (!data) return;
      const v  = data.velocity      || {};
      const ct = data.pr_cycle_time || {};
      const ir = data.idle_rate     || {};
      setText('kpi-24h',      v.last_24h      != null ? v.last_24h      : '--');
      setText('kpi-velocity', v.all_time_per_day != null ? v.all_time_per_day + '/day' : '--');
      const cycle = ct.mean_hours;
      setText('kpi-cycle', cycle != null ? cycle.toFixed(1) + 'h' : '--');
      const idle = ir.all_time_pct;
      setText('kpi-idle', idle != null ? idle.toFixed(1) + '%' : '--');
    }

    function updateAgents(data) {
      const list = document.getElementById('agent-list');
      const agents = data && data.agents ? data.agents : null;
      if (!agents || agents.length === 0) {
        list.innerHTML = '<li class="placeholder">no agents registered</li>';
        return;
      }
      list.innerHTML = agents.map(a =>
        '<li>' + String(a).replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</li>'
      ).join('');
    }

    function esc(s) {
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    function updateModuleHealth(data) {
      const summary = document.getElementById('module-health-summary');
      const grid    = document.getElementById('module-grid');
      if (!data || !data.modules) {
        summary.innerHTML = '<span class="placeholder">unavailable</span>';
        grid.innerHTML = '';
        return;
      }
      const total  = data.total  || 0;
      const passed = data.passed || 0;
      const failed = data.failed || 0;
      const cls    = failed === 0 ? 'ok' : 'err';
      summary.innerHTML =
        '<span class="' + cls + '">' + passed + '/' + total + ' passed</span>' +
        (failed > 0 ? ' &mdash; <span class="err">' + failed + ' failed</span>' : '');

      grid.innerHTML = data.modules.map(function(m) {
        const ok  = m.import_ok && m.dep_ok;
        const tip = (!ok && m.errors && m.errors.length)
          ? '<span class="module-tooltip">' + esc(m.errors.slice(0,5).join('\\n')) + '</span>'
          : '';
        return '<span class="module-dot' + (ok ? '' : ' failed') + '">' +
               esc(m.name) + tip + '</span>';
      }).join('');
    }

    function gradeClass(grade) {
      if (!grade) return 'grade-df';
      const g = grade.charAt(0);
      if (g === 'A') return 'grade-a';
      if (g === 'B') return 'grade-b';
      if (g === 'C') return 'grade-c';
      return 'grade-df';
    }

    function updateQuality(data) {
      const barsEl = document.getElementById('quality-bars');
      const weakestEl = document.getElementById('quality-weakest');
      const scores = data && data.scores ? data.scores : null;
      if (!scores || scores.length === 0) {
        barsEl.innerHTML = '<span class="placeholder">no scored PRs yet</span>';
        weakestEl.textContent = '';
        return;
      }
      const recent = scores.slice(0, 10);
      barsEl.innerHTML = recent.map(s => {
        const pct = Math.min(100, s.total_score || 0);
        const gc = gradeClass(s.grade);
        const prLabel = s.pr ? '#' + s.pr : '—';
        return '<div class="quality-bar-row">' +
          '<span class="pr-label">' + prLabel + '</span>' +
          '<div class="quality-bar-track"><div class="quality-bar-fill ' + gc + '" style="width:' + pct + '%"></div></div>' +
          '<span class="score-label">' + (s.total_score ?? '?') + '</span>' +
          '</div>';
      }).join('');

      // Compute weakest dimension across recent scores
      const dims = ['complexity', 'test_coverage', 'review_rounds', 'size'];
      const dimMax = {complexity: 30, test_coverage: 25, review_rounds: 25, size: 20};
      const dimTotals = {complexity: 0, test_coverage: 0, review_rounds: 0, size: 0};
      recent.forEach(s => {
        const bd = s.breakdown || {};
        dims.forEach(d => { dimTotals[d] += (bd[d] && bd[d].score != null) ? bd[d].score : dimMax[d]; });
      });
      let weakest = null, weakestPct = 1.1;
      dims.forEach(d => {
        const pct = dimTotals[d] / (dimMax[d] * recent.length);
        if (pct < weakestPct) { weakestPct = pct; weakest = d; }
      });
      if (weakest) {
        const labels = {complexity: 'Complexity', test_coverage: 'Test Coverage', review_rounds: 'Review Rounds', size: 'PR Size'};
        weakestEl.textContent = 'Weakest: ' + labels[weakest] + ' (' + Math.round(weakestPct * 100) + '% of max)';
      }
    }

    function updateDeps(data) {
      if (!data || !data.stats) return;
      const s = data.stats;
      setText('deps-modules', s.total_modules ?? '—');
      setText('deps-edges',   s.total_edges   ?? '—');
      const cycs = data.cycles ? data.cycles.length : 0;
      const cycEl = document.getElementById('deps-cycles');
      if (cycEl) {
        cycEl.textContent = cycs;
        cycEl.className = cycs > 0 ? 'value deps-cycle-warn' : 'value';
      }
      const tbody = document.getElementById('deps-hub-body');
      if (tbody) {
        const hubs = (data.hubs || []).slice(0, 5);
        if (hubs.length === 0) {
          tbody.innerHTML = '<tr><td colspan="3" style="color:#484f58;font-size:11px">no hub modules</td></tr>';
        } else {
          tbody.innerHTML = hubs.map(h =>
            '<tr><td>' + esc(h.name) + '</td><td>' + h.in_degree + '</td><td>' + h.out_degree + '</td></tr>'
          ).join('');
        }
      }
    }

    function updateMemory(data) {
      if (!data) return;
      const byType = data.by_type || {};
      const bySess = data.by_session || {};
      setText('mem-total',    data.total    != null ? data.total    : '—');
      setText('mem-failure',  byType.failure   != null ? byType.failure   : '0');
      setText('mem-success',  byType.success   != null ? byType.success   : '0');
      setText('mem-pattern',  byType.pattern   != null ? byType.pattern   : '0');
      setText('mem-sessions', Object.keys(bySess).length);
    }

    async function fetchAll() {
      const [health, budget, stats, agents, kpi, modules, quality, memStats, depsData] =
        await Promise.all([
          safeFetch('/health'),
          safeFetch('/budget/status'),
          safeFetch('/registry/stats'),
          safeFetch('/agents'),
          safeFetch('/kpi'),
          safeFetch('/health/modules'),
          safeFetch('/quality'),
          safeFetch('/memory/stats'),
          safeFetch('/deps'),
        ]);

      const alive = health !== null && health.ok === true;
      setConn(alive);

      if (alive) {
        updateBudget(budget);
        updateQueue(stats);
        updateLoop(health);
        updateAgents(agents);
        updateKPI(kpi);
        updateModuleHealth(modules);
        updateDeps(depsData);
        updateQuality(quality);
        updateMemory(memStats);
        document.getElementById('last-updated').textContent =
          'Last updated: ' + new Date().toLocaleTimeString();
      }
    }

    fetchAll();
    setInterval(fetchAll, 15000);
  </script>
</body>
</html>"""
