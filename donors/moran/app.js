/* ═══════════════════════════════════════════════════════════════
   HUD v2 — app.js
   ═══════════════════════════════════════════════════════════════ */

const FAST_POLL_MS = 3000;
const SLOW_POLL_MS = 10000;
const SCORECARD_POLL_MS = 30000;
const state = {
  hydrated: false,
  lastPayload: null,
  lastBackendTs: null,
  lastRenderAt: null,
  lastError: '',
  pollCount: 0,
  activePage: 'page-command',
  scorecard: null,
  preentry: null,
};

// ── Utilities ───────────────────────────────────────────────────
function nn(v, d = 0) { const n = Number(v); return Number.isFinite(n) ? n : d; }
function n(v, d = 0) { return (v === undefined || v === null || Number.isNaN(v)) ? d : v; }
function obj(v) { return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {}; }
function arr(v) { return Array.isArray(v) ? v : []; }
function text(v, d = '-') { return (v === undefined || v === null || v === '') ? d : String(v); }
function escapeHtml(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function $(id) { return document.getElementById(id); }
function setText(id, val) { const el = $(id); if (el) el.textContent = String(val); return el; }
function money(v) { return `$${nn(v, 0).toFixed(2)}`; }
function pct(v, d = 2) { return `${nn(v, 0).toFixed(d)}%`; }
function hhmmss(sec) {
  sec = Math.max(0, nn(sec, 0));
  const h = String(Math.floor(sec / 3600)).padStart(2, '0');
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0');
  const s = String(Math.floor(sec % 60)).padStart(2, '0');
  return `${h}:${m}:${s}`;
}
function dur(sec) {
  sec = nn(sec, 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  return `${h}h ${m}m ${s}s`;
}
function relAge(ts) {
  if (!ts) return '-';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
  return `${s}s`;
}
function formatTs(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '-'; }
function safeMint(m) {
  const mint = String(m || '');
  if (!mint || mint === '-') return '-';
  if (mint.length <= 12) return mint;
  return `${mint.slice(0, 6)}...${mint.slice(-4)}`;
}
function firstDefined(...values) {
  for (const v of values) {
    if (v !== undefined && v !== null && !(typeof v === 'number' && Number.isNaN(v))) return v;
  }
  return undefined;
}
function firstNonEmptyArray(...values) {
  for (const v of values) { if (Array.isArray(v) && v.length > 0) return v; }
  return [];
}
function dmPick(dm, ...keys) {
  for (const key of keys) {
    if (dm && dm[key] !== undefined && dm[key] !== null) return dm[key];
  }
  return undefined;
}
function pnlClass(v) { return v > 0.001 ? 'pnl-pos' : v < -0.001 ? 'pnl-neg' : 'pnl-zero'; }
function pnlSign(v) { return v > 0 ? `+$${v.toFixed(2)}` : v < 0 ? `-$${Math.abs(v).toFixed(2)}` : '$0.00'; }

// ── Tab Switching ───────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.page;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const page = $(target);
    if (page) page.classList.add('active');
    state.activePage = target;
    if (target === 'page-analytics') { fetchReplay(); fetchExitLab(); }
  });
});

// ── API Helpers ─────────────────────────────────────────────────
async function fetchJson(url, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url, { cache: 'no-store', headers: { 'Cache-Control': 'no-cache' }, signal: controller.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

async function postControl(payload) {
  const res = await fetch('/control', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}), cache: 'no-store'
  });
  if (!res.ok) { const t = await res.text(); throw new Error(t || `HTTP ${res.status}`); }
  return res.json();
}

async function postOperatorReset(mode) {
  const res = await fetch('/operator/reset_session', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }), cache: 'no-store'
  });
  if (!res.ok) { const t = await res.text(); throw new Error(t || `HTTP ${res.status}`); }
  return res.json();
}

// ── Confirm Dialog ──────────────────────────────────────────────
function confirmAction(title, message) {
  return new Promise(resolve => {
    const dialog = $('confirm-dialog');
    setText('confirm-title', title);
    setText('confirm-message', message);
    dialog.classList.remove('hidden');
    const ok = $('confirm-ok');
    const cancel = $('confirm-cancel');
    function cleanup(result) {
      dialog.classList.add('hidden');
      ok.removeEventListener('click', onOk);
      cancel.removeEventListener('click', onCancel);
      resolve(result);
    }
    function onOk() { cleanup(true); }
    function onCancel() { cleanup(false); }
    ok.addEventListener('click', onOk);
    cancel.addEventListener('click', onCancel);
  });
}

// ── Loading Overlay ─────────────────────────────────────────────
function showLoading(msg) {
  const ol = $('loading-overlay');
  if (ol) { ol.classList.remove('hidden'); setText('loading-text', msg || 'Loading...'); }
}
function hideLoading() {
  const ol = $('loading-overlay');
  if (ol) ol.classList.add('hidden');
}

// ── Profile Switching ───────────────────────────────────────────
async function switchProfile(profile) {
  if (!await confirmAction('Switch Profile', `Switch to ${profile}? This will restart the triad.`)) return;
  const statusEl = $('ctrl-profile-status');
  if (statusEl) statusEl.textContent = `Switching to ${profile}...`;
  showLoading(`Switching to ${profile}...`);
  try {
    const r = await fetch('/profile/switch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile }),
    });
    const j = await r.json();
    if (r.ok && j.ok) {
      if (statusEl) statusEl.textContent = `Switched to ${profile}. Reconnecting...`;
      setTimeout(hideLoading, 10000);
    } else {
      if (statusEl) statusEl.textContent = `Error: ${j.detail || JSON.stringify(j)}`;
      hideLoading();
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `Failed: ${e.message}`;
    hideLoading();
  }
}

// ── Wire Buttons ────────────────────────────────────────────────
function wireButtons() {
  const bind = (id, fn) => {
    const el = $(id);
    if (el && !el.dataset.bound) {
      el.dataset.bound = '1';
      el.addEventListener('click', async () => {
        try { await fn(); } catch (err) {
          console.error('[HUD control]', err);
          state.lastError = `control: ${err?.message || err}`;
        }
      });
    }
  };

  // Profile
  bind('btn-profile-safe', () => switchProfile('LIVE_CANARY_SAFE'));
  bind('btn-profile-research', () => switchProfile('LIVE_CANARY_RESEARCH'));
  bind('btn-profile-paper', () => switchProfile('PAPER'));

  // Live TX toggle
  bind('btn-toggle-live-tx', async () => {
    const safety = obj(state.lastPayload?.mode_safety);
    const armed = Boolean(safety.live_tx_submission_enabled);
    const action = armed ? 'disable_live_tx' : 'enable_live_tx';
    const title = armed ? 'Disarm Live TX' : 'Arm Live TX';
    const msg = armed ? 'Disarm live TX? Trading will also be disabled.' : 'Arm live transaction submission? Enable Trading separately as step 2.';
    if (!await confirmAction(title, msg)) return;
    const r = await postControl({ action });
    alert(r.note || `${action} queued`);
  });

  // Trading toggle
  bind('btn-toggle-trading', async () => {
    const safety = obj(state.lastPayload?.mode_safety);
    const armed = Boolean(safety.live_tx_submission_enabled);
    const trading = Boolean(safety.trading_enabled) && armed;
    const action = trading ? 'disable_trading' : 'enable_trading';
    const title = trading ? 'Disable Trading' : 'Enable Trading';
    const msg = trading ? 'Disable trading (buys)?' : 'Enable live buy entries? Requires Live TX armed.';
    if (!await confirmAction(title, msg)) return;
    try {
      const r = await postControl({ action });
      alert(r.note || `${action} queued`);
    } catch (err) { alert(`BLOCKED: ${err?.message || err}`); }
  });

  // Entries toggle
  bind('btn-toggle-entries', async () => {
    const controls = obj(state.lastPayload?.operator_controls);
    const currentlyDisabled = Boolean(controls.disable_new_entries);
    await postControl({ action: 'disable_new_entries', value: !currentlyDisabled });
  });
  bind('btn-flatten-all', async () => {
    if (await confirmAction('Flatten All', 'Sell ALL open positions immediately?'))
      await postControl({ action: 'flatten_all' });
  });

  // Session
  bind('btn-reset-session-metrics', async () => {
    if (await confirmAction('Reset Metrics', 'Reset session metrics?'))
      await postOperatorReset('metrics_only');
  });
  bind('btn-start-paper-session', async () => {
    if (await confirmAction('New Paper Session', 'Start new paper session with $100 budget?'))
      await postOperatorReset('baseline_100');
  });
  bind('btn-hard-reset', async () => {
    if (await confirmAction('Hard Reset', 'Hard reset the bot? This runs the full reset script.')) {
      showLoading('Hard resetting...');
      try {
        await postControl({ action: 'hard_reset' });
        setTimeout(hideLoading, 10000);
      } catch (e) { hideLoading(); alert(`Error: ${e.message}`); }
    }
  });
  bind('btn-restart', async () => {
    if (await confirmAction('Restart Engine', 'Restart the simulation engine process?')) {
      showLoading('Restarting...');
      try {
        await postControl({ action: 'restart' });
        setTimeout(hideLoading, 10000);
      } catch (e) { hideLoading(); alert(`Error: ${e.message}`); }
    }
  });

  // Mode
  bind('btn-set-mode-paper', async () => {
    if (await confirmAction('Paper Mode', 'Switch to PAPER mode? Requires restart.')) {
      const r = await postControl({ action: 'set_mode_paper' });
      if (r.requires_restart) alert(r.note || 'Restart required.');
    }
  });
  bind('btn-set-mode-live', async () => {
    if (await confirmAction('Live Mode', 'Switch to LIVE mode? Requires ALLOW_LIVE_TRADING in .env.')) {
      const r = await postControl({ action: 'set_mode_live' });
      if (r.requires_restart) alert(r.note || 'Restart required.');
    }
  });

  // Analytics: Replay & Exit Lab refresh
  bind('btn-refresh-replay', fetchReplay);
  bind('btn-refresh-exitlab', fetchExitLab);
}

// ── Render: Top Bar ─────────────────────────────────────────────
function renderTopBar(d) {
  const isLive = text(d.execution_mode, 'paper').includes('live');
  const pill = $('mode-pill');
  if (pill) {
    pill.textContent = isLive ? 'LIVE' : 'PAPER';
    pill.className = `mode-pill ${isLive ? 'live' : 'paper'}`;
  }
  const dot = $('live-dot');
  if (dot) dot.className = state.hydrated ? 'live-dot' : 'live-dot dead';

  const safety = obj(d.mode_safety);
  setText('top-profile', text(safety.current_profile, text(d.execution_mode, 'paper')).toUpperCase());

  const headline = obj(d.headline);
  const w = d.paper_mode
    ? 'PAPER'
    : (d.real_wallet_sol != null ? `${nn(d.real_wallet_sol, 0).toFixed(2)} SOL` : '-');
  setText('top-sol', w);

  const pnlVal = nn(firstDefined(headline.pnl_usd, d.pnl_usd), 0);
  const pnlEl = setText('top-pnl', pnlSign(pnlVal));
  if (pnlEl) pnlEl.style.color = pnlVal > 0 ? 'var(--green)' : pnlVal < 0 ? 'var(--red)' : '';

  const hs = obj(d.health_score);
  const score = nn(hs.score, 100);
  const tier = text(hs.tier, 'ELITE');
  const healthEl = setText('top-health', `${score.toFixed(0)} ${tier}`);
  if (healthEl) healthEl.className = score >= 85 ? 'elite' : score >= 70 ? 'degraded' : 'critical';

  const roll = obj(d.rolling_24h);
  const dm = obj(d.daily_metrics);
  setText('top-winrate', pct(firstDefined(roll.win_rate_pct, dm.win_rate_pct), 1));
  setText('top-data-age', relAge(firstDefined(d.state_updated_at, d.updated_at)));
}

// ── Render: P&L Cards ───────────────────────────────────────────
function renderPnlCards(d) {
  const headline = obj(d.headline);
  const a = obj(d.analytics_phase5c);
  const session = obj(a.session);
  const lifetime = obj(a.lifetime);

  const pnl = nn(firstDefined(headline.pnl_usd, d.pnl_usd), 0);
  const el = setText('pnl-session', pnlSign(pnl));
  if (el) el.className = `pnl-value ${pnlClass(pnl)}`;
  setText('pnl-session-roi', pct(firstDefined(headline.roi_pct, d.roi_pct), 1));
  setText('pnl-session-trades', n(session.closed_trades, 0));

  const unrealized = nn(firstDefined(session.unrealized_pnl_usd, d.unrealized_pnl_usd), 0);
  const uel = setText('pnl-unrealized', pnlSign(unrealized));
  if (uel) uel.className = `pnl-value ${pnlClass(unrealized)}`;
  const openPositions = firstNonEmptyArray(d.active_positions, d.paper_positions)
    .filter(p => ['open', 'partial_exit'].includes(text(obj(p).status, 'open')));
  setText('pnl-open-count', openPositions.length);

  const exp = nn(obj(lifetime.expectancy).per_trade_usd, 0);
  const expEl = setText('pnl-expectancy', pnlSign(exp));
  if (expEl) expEl.className = `pnl-value ${pnlClass(exp)}`;
  const tax = obj(d.lifetime_taxonomy);
  const at = obj(d.all_time_metrics);
  setText('pnl-lifetime-entries', n(firstDefined(tax.successful_entries, at.successful_entries), 0));
  setText('pnl-lifetime-wr', pct(firstDefined(lifetime.expectancy?.win_rate_pct, obj(lifetime.expectancy).win_rate_pct), 1));
}

// ── Render: Positions ───────────────────────────────────────────
function renderPositions(d) {
  const pb = $('positions');
  const emptyEl = $('positions-empty');
  if (!pb) return;

  const allPositions = firstNonEmptyArray(d.active_positions, d.paper_positions);
  const positions = allPositions.filter(p => ['open', 'partial_exit'].includes(text(obj(p).status, 'open')));

  if (emptyEl) emptyEl.style.display = positions.length ? 'none' : '';

  pb.innerHTML = '';
  positions.forEach(raw => {
    const p = obj(raw);
    const tr = document.createElement('tr');
    const roi = nn(p.unrealized_roi_pct, 0);
    const stale = Boolean(p.mark_stale);
    const entryPx = nn(firstDefined(p.entry_price, p.entry_px), 0);
    const markPx = firstDefined(p.last_price, p.current_px);
    const markCell = markPx != null ? nn(markPx, 0).toFixed(6) : '-';
    const source = text(firstDefined(p.mark_price_source, p.current_px_source), 'fallback');
    const stopPx = nn(p.stop_px, 0);
    const currentPx = nn(firstDefined(p.current_px, p.last_price), 0);
    const distPct = (currentPx > 0 && stopPx > 0) ? ((currentPx - stopPx) / currentPx * 100) : null;
    const distValid = distPct !== null && distPct >= -200 && distPct <= 50;
    const holdSec = Math.max(0, Date.now() / 1000 - nn(firstDefined(p.entry_ts, p.ts), Date.now() / 1000));
    const holdStr = holdSec < 60 ? `${Math.floor(holdSec)}s` : `${Math.floor(holdSec / 60)}m ${Math.floor(holdSec % 60)}s`;

    const roiColor = roi >= 0 ? 'c-green' : 'c-red';
    const roiLabel = stale ? `~${roi.toFixed(1)}%` : `${roi.toFixed(1)}%`;
    const stopLabel = stopPx > 0 ? `${stopPx.toFixed(6)}${distValid ? ` (${distPct.toFixed(0)}%)` : ''}` : '-';
    const statusBadge = p.position_truth_state === 'GHOST_UNRESOLVED' ? 'GHOST' : text(p.status, 'open');
    const badgeClass = statusBadge === 'GHOST' ? 'badge-red' : statusBadge === 'partial_exit' ? 'badge-amber' : 'badge-green';
    const staleBadge = stale ? ' <span class="badge badge-amber">STALE</span>' : '';
    const sourceTag = source === 'executable_notional_quote' ? 'EXEC' : source === 'mid_price' ? 'MID' : source === 'dexscreener' ? 'DEX' : 'FB';

    const mintAttr = escapeHtml(text(p.mint, ''));
    tr.innerHTML = `<td class="mono">${escapeHtml(safeMint(p.mint))}</td>` +
      `<td><span class="badge ${badgeClass}">${escapeHtml(statusBadge)}</span>${staleBadge}</td>` +
      `<td class="mono">${entryPx.toFixed(6)}</td>` +
      `<td class="mono">${markCell} <span class="c-dim" style="font-size:10px">${sourceTag}</span></td>` +
      `<td class="mono ${roiColor}">${roiLabel}</td>` +
      `<td class="mono">${escapeHtml(stopLabel)}</td>` +
      `<td class="mono c-dim">${holdStr}</td>` +
      `<td><button class="btn" data-action="sell25" data-mint="${mintAttr}">25%</button> ` +
      `<button class="btn" data-action="sell50" data-mint="${mintAttr}">50%</button> ` +
      `<button class="btn btn-red" data-action="sell100" data-mint="${mintAttr}">100%</button></td>`;
    pb.appendChild(tr);
  });

  pb.querySelectorAll('button[data-action]').forEach(btn => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', async () => {
      const mint = btn.dataset.mint;
      const action = btn.dataset.action;
      const fraction = action === 'sell25' ? 0.25 : action === 'sell50' ? 0.50 : 1.0;
      try { await postControl({ action: 'sell_fraction', mint, fraction }); }
      catch (err) { console.error('[sell]', err); }
    });
  });
}

// ── Render: Scorecard ───────────────────────────────────────────
function renderScorecard() {
  const sc = state.scorecard;
  if (!sc) return;
  const wr = nn(sc.win_rate, 0);
  setText('sc-winrate', pct(wr, 1));
  const bar = $('sc-winrate-bar');
  if (bar) {
    bar.style.width = `${Math.min(100, wr)}%`;
    bar.style.background = wr >= 35 ? 'var(--green)' : 'var(--red)';
  }
  const pnlVal = nn(sc.total_pnl, 0);
  const pnlEl = setText('sc-pnl', pnlSign(pnlVal));
  if (pnlEl) pnlEl.style.color = pnlVal >= 0 ? 'var(--green)' : 'var(--red)';
  setText('sc-wins', n(sc.wins, 0));
  setText('sc-losses', n(sc.losses, 0));
  const hcPct = nn(sc.hard_cap_pct, 0);
  const hcEl = setText('sc-hardcap', `${n(sc.hard_cap_count, 0)} (${hcPct.toFixed(1)}%)`);
  if (hcEl) hcEl.style.color = hcPct > 15 ? 'var(--red)' : hcPct > 10 ? 'var(--amber)' : '';
  const verdict = $('sc-verdict');
  if (verdict) {
    verdict.textContent = text(sc.verdict, '—');
    verdict.className = `verdict ${sc.verdict === 'PASS' ? 'pass' : 'fail'}`;
  }
  setText('sc-window', `(last ${n(sc.trades, 0)})`);
}

// ── Render: Close Reasons Chart ─────────────────────────────────
function renderCloseReasonsChart(d) {
  const host = $('close-reasons-chart');
  const emptyEl = $('close-reasons-empty');
  if (!host) return;

  // Use session top_close_reasons from analytics or scorecard close_reasons
  const a = obj(d.analytics_phase5c);
  const session = obj(a.session);
  let reasons = arr(session.top_close_reasons);
  if (!reasons.length && state.scorecard) reasons = arr(state.scorecard.close_reasons);

  if (!reasons.length) {
    host.innerHTML = '';
    if (emptyEl) emptyEl.style.display = '';
    return;
  }
  if (emptyEl) emptyEl.style.display = 'none';

  const maxCount = Math.max(...reasons.map(r => n(obj(r).count, 0)), 1);
  host.innerHTML = reasons.slice(0, 8).map(raw => {
    const r = obj(raw);
    const cnt = n(r.count, 0);
    const pnlVal = nn(r.pnl_usd || r.pnl, 0);
    const widthPct = (cnt / maxCount * 100).toFixed(1);
    const barColor = pnlVal >= 0 ? 'var(--green)' : 'var(--red)';
    return `<div class="cr-row">
      <div class="cr-label">${escapeHtml(text(r.reason, '?'))}</div>
      <div class="cr-bar-wrap"><div class="cr-bar" style="width:${widthPct}%;background:${barColor}"></div></div>
      <div class="cr-count">${cnt}</div>
      <div class="cr-pnl ${pnlVal >= 0 ? 'c-green' : 'c-red'}">${pnlSign(pnlVal)}</div>
    </div>`;
  }).join('');
}

// ── Render: Market Stats ────────────────────────────────────────
function renderMarketStats(d) {
  const dm = obj(d.daily_metrics);
  const roll = obj(d.rolling_24h);
  setText('mk-uptime', hhmmss(firstDefined(d.process_uptime_seconds, dm.session_uptime_seconds)));
  setText('mk-temp', `${n(firstDefined(d.market_temp_mints_per_hour, dm.market_temp_mints_per_hour), 0)}/h`);
  setText('mk-unique', n(dmPick(dm, 'unique_mints'), 0));
  setText('mk-latency', `${nn(firstDefined(roll.avg_decision_latency_ms, dm.avg_decision_latency_ms), 0).toFixed(1)}ms`);
  setText('mk-parsefail', n(dmPick(dm, 'parse_failures'), 0));
  setText('mk-rpc429', n(dmPick(dm, 'rpc_429_count'), 0));
  const total = n(d.jito_bundle_total, 0);
  const rate = nn(d.jito_success_rate, 100);
  setText('mk-jito', `${rate.toFixed(0)}% (${n(d.jito_bundle_success, 0)}/${total})`);
  const minLiq = firstDefined(d.min_liquidity_usd_active, dm.min_liquidity_usd_active);
  setText('mk-minliq', minLiq != null ? `$${n(minLiq, 0)}` : '-');
}

// ── Render: Controls State ──────────────────────────────────────
function renderControlsState(d) {
  const safety = obj(d.mode_safety);
  const controls = obj(d.operator_controls);
  const isLive = text(d.execution_mode, 'paper').includes('live');
  const liveTxArmed = Boolean(safety.live_tx_submission_enabled);
  const tradingActive = Boolean(safety.trading_enabled) && liveTxArmed;
  const entriesDisabled = Boolean(controls.disable_new_entries);

  setText('ctrl-profile', text(safety.current_profile, '-').toUpperCase());

  // Live TX toggle
  const txBtn = $('btn-toggle-live-tx');
  if (txBtn) {
    if (liveTxArmed) {
      txBtn.textContent = 'TX Armed';
      txBtn.className = 'btn btn-green btn-active';
    } else {
      txBtn.textContent = 'TX Disarmed';
      txBtn.className = 'btn btn-red';
    }
  }

  // Trading toggle
  const tradBtn = $('btn-toggle-trading');
  if (tradBtn) {
    if (tradingActive) {
      tradBtn.textContent = 'Trading Enabled';
      tradBtn.className = 'btn btn-green btn-active';
    } else {
      tradBtn.textContent = 'Trading Disabled';
      tradBtn.className = 'btn btn-red';
    }
  }

  // Entries toggle
  const toggleBtn = $('btn-toggle-entries');
  if (toggleBtn) {
    if (entriesDisabled) {
      toggleBtn.textContent = 'Entries Disabled';
      toggleBtn.className = 'btn btn-red';
    } else {
      toggleBtn.textContent = 'Entries Enabled';
      toggleBtn.className = 'btn btn-green btn-active';
    }
  }

  // Pending mode
  const pendingMode = firstDefined(d.pending_execution_mode, obj(d.hud_derived).pending_execution_mode);
  const pendEl = $('pending-mode');
  if (pendEl) {
    if (pendingMode && pendingMode !== text(d.execution_mode, 'paper')) {
      pendEl.textContent = `Pending: ${pendingMode} (restart required)`;
      pendEl.style.display = '';
    } else {
      pendEl.style.display = 'none';
    }
  }
}

// ── Render: Analytics ───────────────────────────────────────────
function renderAnalytics(d) {
  const a = obj(d.analytics_phase5c);
  const session = obj(a.session);
  const lifetime = obj(a.lifetime);
  const today = obj(a.today_utc);

  // MFE capture stats
  setText('an-avg-mfe', pct(lifetime.avg_mfe_pct));
  setText('an-avg-mae', pct(lifetime.avg_mae_pct));
  setText('an-avg-win', money(obj(session.expectancy).avg_win_usd));
  setText('an-avg-loss', money(obj(session.expectancy).avg_loss_usd));
  setText('an-avg-hold', dur(lifetime.avg_hold_time_seconds));
  setText('an-quote-exits', n(session.quoteability_exit_count, 0));

  // Close reasons lifetime
  const reasonsTb = $('analytics-reasons');
  if (reasonsTb) {
    reasonsTb.innerHTML = '';
    const merged = obj(lifetime.win_loss_by_close_reason);
    Object.entries(merged)
      .sort((a, b) => nn(b[1]?.realized_pnl_usd, 0) - nn(a[1]?.realized_pnl_usd, 0))
      .forEach(([reason, row]) => {
        row = obj(row);
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${escapeHtml(text(reason, '-'))}</td><td>${n(row.count, 0)}</td><td>${n(row.wins, 0)}</td><td>${n(row.losses, 0)}</td><td class="${nn(row.realized_pnl_usd, 0) >= 0 ? 'c-green' : 'c-red'}">${money(row.realized_pnl_usd)}</td><td>${pct(row.avg_realized_roi_pct)}</td>`;
        reasonsTb.appendChild(tr);
      });
  }

  // Summary by window
  const sumTb = $('analytics-summary');
  if (sumTb) {
    sumTb.innerHTML = '';
    [['Today UTC', today], ['Session', session], ['Lifetime', lifetime]].forEach(([label, row]) => {
      const exp = obj(row.expectancy);
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${label}</td><td>${money(row.realized_pnl_usd)}</td><td>${money(row.unrealized_pnl_usd)}</td><td>${money(row.total_pnl_usd)}</td><td>${n(row.closed_trades, 0)}</td><td>${n(row.open_positions, 0)}</td><td>${pct(exp.win_rate_pct ?? row.win_rate_pct)}</td><td>${money(exp.per_trade_usd)}</td>`;
      sumTb.appendChild(tr);
    });
  }

  // Graveyard
  const grave = arr(d.graveyard);
  const gb = $('grave');
  const graveEmpty = $('grave-empty');
  if (gb) {
    gb.innerHTML = '';
    if (graveEmpty) graveEmpty.style.display = grave.length ? 'none' : '';
    grave.forEach(raw => {
      const g = obj(raw);
      const tr = document.createElement('tr');
      const missed = nn(g.missed_roi_pct, 0);
      const moon = missed > 100;
      const threshCtx = g.historical_threshold_usd
        ? `hist ${money(g.historical_threshold_usd)} vs ${money(g.current_policy_threshold_usd)}`
        : `${money(g.current_policy_threshold_usd)}`;
      tr.innerHTML = `<td class="mono">${escapeHtml(safeMint(g.mint))}</td><td>${escapeHtml(text(g.reason, ''))}</td><td>${escapeHtml(threshCtx)}</td><td class="mono">${escapeHtml(text(g.current_price, '-'))}</td><td class="${moon ? 'c-amber' : 'c-green'}">${missed.toFixed(1)}%${moon ? ' MOON' : ''}</td>`;
      gb.appendChild(tr);
    });
  }

  // Wallet reconciliation
  renderWalletRecon(d);

  // Closed positions
  const allPositions = firstNonEmptyArray(d.active_positions, d.paper_positions);
  const closed = arr(firstDefined(d.closed_positions, allPositions.filter(p => text(obj(p).status, '') === 'closed'))).slice(-12).reverse();
  const cb = $('closed-positions');
  if (cb) {
    cb.innerHTML = '';
    closed.forEach(raw => {
      const p = obj(raw);
      const tr = document.createElement('tr');
      const exitPx = p.exit_price != null ? nn(p.exit_price, 0).toFixed(6) : '-';
      const roiVal = nn(p.realized_roi_pct, 0);
      tr.innerHTML = `<td class="mono">${escapeHtml(safeMint(p.mint))}</td><td>${escapeHtml(text(p.close_reason, ''))}</td><td>${formatTs(p.exit_ts)}</td><td class="mono">${exitPx}</td><td class="${roiVal >= 0 ? 'c-green' : 'c-red'}">${roiVal.toFixed(1)}%</td>`;
      cb.appendChild(tr);
    });
  }
}

// ── Render: Pre-Entry Stats ─────────────────────────────────────
function renderPreEntryStats() {
  const pe = state.preentry;
  if (!pe) return;
  setText('pe-ok', n(pe.ok_count, 0));
  setText('pe-surge', n(pe.surge_count, 0));
  setText('pe-dump', n(pe.dump_count, 0));
  setText('pe-ratio', `${nn(pe.ok_ratio_pct, 0).toFixed(1)}%`);
  setText('pe-logfile', text(pe.log_file, 'none'));
  const bar = $('pe-ok-bar');
  if (bar) bar.style.width = `${Math.min(100, nn(pe.ok_ratio_pct, 0))}%`;
}

// ── Render: Wallet Reconciliation ───────────────────────────────
function renderWalletRecon(d) {
  const wr = obj(d.wallet_reconciliation);
  const summaryEl = $('wallet-recon-summary');
  const bodyEl = $('wallet-recon-body');
  if (!summaryEl || !bodyEl) return;

  if (!wr.enabled) {
    summaryEl.textContent = 'Paper mode — wallet reconciliation not active.';
    bodyEl.innerHTML = '';
    return;
  }

  const clean = wr.reconciliation_clean;
  summaryEl.textContent = clean
    ? `Clean — ${n(wr.wallet_spl_token_count, 0)} SPL tokens, ${n(wr.all_open_position_mint_count, 0)} managed`
    : `GAP — ${n(wr.unmanaged_wallet_mint_count, 0)} unmanaged, ${n(wr.managed_missing_from_wallet_count, 0)} missing`;
  summaryEl.style.color = clean ? 'var(--green)' : 'var(--red)';

  const age = wr.last_wallet_scan_age_seconds != null ? `${wr.last_wallet_scan_age_seconds}s ago` : 'never';
  const unmanagedDetail = arr(wr.unmanaged_wallet_detail).map(t => `${escapeHtml(safeMint(t.mint))} ${escapeHtml(t.ui_amount)} [${escapeHtml(t.classification)}]`).join('; ') || 'none';
  const ghostDetail = arr(wr.ghost_position_detail).map(t => `${escapeHtml(safeMint(t.mint))} $${escapeHtml(t.size_usd || '?')} [${escapeHtml(t.classification)}]`).join('; ') || 'none';

  const rows = [
    ['Scan Age', age],
    ['SPL Tokens', n(wr.wallet_spl_token_count, 0)],
    ['Managed Open', n(wr.all_open_position_mint_count, 0)],
    ['Unmanaged', unmanagedDetail],
    ['Ghost Positions', ghostDetail],
    ['Status', clean ? 'CLEAN' : 'GAP DETECTED'],
  ];
  bodyEl.innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');
}

// ── Render: Debug Page ──────────────────────────────────────────
function renderDebug(d) {
  const hs = obj(d.health_score);
  const hsf = obj(hs.factors);
  const score = nn(hs.score, 100);
  setText('dbg-health-score', `${score.toFixed(1)} ${text(hs.tier, 'ELITE')}`);
  const hbar = $('dbg-health-bar');
  if (hbar) {
    hbar.style.width = `${score}%`;
    hbar.style.background = score >= 85 ? 'var(--green)' : score >= 70 ? 'var(--amber)' : 'var(--red)';
  }
  setText('dbg-parse-fail', `${nn(hsf.parse_fail_rate, 0).toFixed(2)}%`);
  setText('dbg-rpc429-rate', `${nn(hsf.rpc429_rate, 0).toFixed(2)}%`);
  setText('dbg-cov', `${nn(hsf.decision_coverage, 0).toFixed(2)}%`);
  const provider = obj(d.provider_health);
  setText('dbg-ws-disc', n(provider.ws?.disconnects_1h, 0));
  setText('dbg-ws-status', text(provider.ws?.derived_status || provider.ws?.uptime_note, 'unknown'));
  setText('dbg-rpc-timeouts', n(provider.rpc?.timeouts_1h, 0));

  // Latency
  const lat = obj(d.latency_breakdown);
  setText('lat-headline', `${nn(lat.headline_avg_latency_ms, 0).toFixed(1)}ms`);
  setText('lat-decision', `${nn(lat.decision_avg_latency_ms, 0).toFixed(1)}ms`);
  setText('lat-parse', `${nn(lat.mint_parse_avg_latency_ms, 0).toFixed(1)}ms`);
  setText('lat-gettx', `${nn(lat.get_transaction_avg_wait_ms, 0).toFixed(1)}ms`);
  setText('lat-dependency', `${nn(lat.dependency_delay_avg_ms, 0).toFixed(1)}ms`);
  setText('lat-exec', `${nn(lat.execution_avg_latency_ms, 0).toFixed(1)}ms`);
  setText('lat-dominant', text(lat.dominant_component, '-'));

  // Decision breakdown
  const b = obj(d.decision_breakdown);
  const dm = obj(d.daily_metrics);
  const breakdownEl = $('breakdown');
  if (breakdownEl) {
    const bd = {
      scans: n(firstDefined(b.total_scans, dm.total_scans), 0),
      unique: n(firstDefined(b.unique_mints, dm.unique_mints), 0),
      dupes: n(firstDefined(b.duplicates_skipped, dm.duplicates_skipped), 0),
      vetoL: n(firstDefined(b.vetoed_liquidity, dm.vetoed_liquidity), 0),
      vetoP: n(firstDefined(b.vetoed_price, dm.vetoed_price), 0),
      parseFail: n(firstDefined(b.parse_failures, dm.parse_failures), 0),
      entries: n(firstDefined(b.successful_entries, dm.successful_entries), 0),
      other: n(firstDefined(b.pending_other), 0),
    };
    breakdownEl.innerHTML = `<tr><td>${bd.scans}</td><td>${bd.unique}</td><td>${bd.dupes}</td><td>${bd.vetoL}</td><td>${bd.vetoP}</td><td>${bd.parseFail}</td><td>${bd.entries}</td><td>${bd.other}</td></tr>`;
  }

  // KV Tables
  renderKVTable('rpc-health-body', obj(d.rpc_health));
  renderKVTable('terminal-blockers-body', obj(d.terminal_blockers_summary));
  renderKVTable('execution-audit-body', obj(d.execution_audit));
  renderKVTable('candidate-timing-body', obj(d.candidate_timing));
  renderKVTable('entry-funnel-body', obj(d.entry_funnel));
  renderModeSafety(d);
  renderJupiter(d);

  // Anomalies
  const anomaly = obj(d.operator_anomalies);
  setText('anom-restored-tp', n(anomaly.restored_tp_replay_blocked_count, 0));
  setText('anom-zero-qty', n(anomaly.zero_qty_without_canonical_close_count, 0));
  setText('anom-reentry', n(anomaly.reentry_blocked_missing_canonical_close_count, 0));
  setText('anom-contaminated-count', n(anomaly.session_contaminated_mint_count, 0));

  // Raw telemetry
  const rawEl = $('debug-raw');
  if (rawEl) {
    const summary = {
      execution_mode: d.execution_mode,
      process_uptime_seconds: d.process_uptime_seconds,
      state_updated_at: d.state_updated_at,
      health_score: d.health_score,
      daily_metrics: d.daily_metrics,
      operator_controls: d.operator_controls,
      mode_safety: d.mode_safety,
    };
    rawEl.textContent = JSON.stringify(summary, null, 2);
  }
}

function renderKVTable(tbodyId, data) {
  const el = $(tbodyId);
  if (!el) return;
  const d2 = obj(data);
  const keys = Object.keys(d2);
  if (!keys.length) { el.innerHTML = '<tr><td colspan="2" class="c-dim">No data</td></tr>'; return; }
  el.innerHTML = keys.map(k => {
    let v = d2[k];
    let display;
    if (v === null || v === undefined) display = '<span class="c-dim">—</span>';
    else if (typeof v === 'boolean') display = v ? '<span class="c-green">YES</span>' : '<span class="c-red">NO</span>';
    else if (typeof v === 'object') display = `<span class="c-dim">${escapeHtml(JSON.stringify(v))}</span>`;
    else display = escapeHtml(v);
    return `<tr><td>${escapeHtml(k.replace(/_/g, ' '))}</td><td>${display}</td></tr>`;
  }).join('');
}

function renderModeSafety(d) {
  const el = $('mode-safety-body');
  if (!el) return;
  const ms = obj(d.mode_safety);
  const keys = Object.keys(ms);
  if (!keys.length) { el.innerHTML = '<tr><td colspan="2" class="c-dim">No data</td></tr>'; return; }
  const rows = keys.filter(k => k !== 'jupiter' && k !== 'sell_execution_status').map(k => {
    let v = ms[k];
    let display;
    if (v === null || v === undefined) display = '<span class="c-dim">—</span>';
    else if (typeof v === 'boolean') display = v ? '<span class="c-green">YES</span>' : '<span class="c-red">NO</span>';
    else if (typeof v === 'object') display = `<span class="c-dim">${escapeHtml(JSON.stringify(v))}</span>`;
    else display = escapeHtml(v);
    return `<tr><td>${escapeHtml(k.replace(/_/g, ' '))}</td><td>${display}</td></tr>`;
  });
  el.innerHTML = rows.join('');
}

function renderJupiter(d) {
  const el = $('jupiter-body');
  if (!el) return;
  const ms = obj(d.mode_safety);
  const jup = obj(ms.jupiter);
  const keys = Object.keys(jup);
  if (!keys.length) { el.innerHTML = '<tr><td colspan="2" class="c-dim">No data</td></tr>'; return; }
  el.innerHTML = keys.map(k => {
    let v = jup[k];
    let display;
    if (v === null || v === undefined) display = '<span class="c-dim">—</span>';
    else if (typeof v === 'boolean') display = v ? '<span class="c-green">YES</span>' : '<span class="c-red">NO</span>';
    else display = escapeHtml(v);
    return `<tr><td>${escapeHtml(k.replace(/_/g, ' '))}</td><td>${display}</td></tr>`;
  }).join('');
}

// ── Main Render ─────────────────────────────────────────────────
function render(d) {
  state.lastPayload = d;
  renderTopBar(d);
  renderPnlCards(d);
  renderPositions(d);
  renderCloseReasonsChart(d);
  renderMarketStats(d);
  renderControlsState(d);
  renderScorecard();
  renderPreEntryStats();

  // Only render heavy pages if visible
  if (state.activePage === 'page-analytics') renderAnalytics(d);
  if (state.activePage === 'page-debug') renderDebug(d);

  state.lastRenderAt = new Date();
}

// ── Poll Loop ───────────────────────────────────────────────────
async function poll() {
  state.pollCount++;
  try {
    const data = await fetchJson(`/hud?_=${Date.now()}`, 12000);
    state.hydrated = true;
    state.lastError = '';
    state.lastBackendTs = data?.state_updated_at ?? data?.updated_at;
    render(data);
    hideLoading();
  } catch (err) {
    console.error('[HUD poll]', err);
    state.lastError = err?.name === 'AbortError' ? 'timeout' : (err?.message || String(err));
    const dot = $('live-dot');
    if (dot) dot.className = 'live-dot dead';
  }
}

// ── Replay & Exit Lab (on-demand only) ─────────────────────────
let _replayFetching = false;
let _exitlabFetching = false;

async function fetchReplay() {
  if (_replayFetching) return;
  _replayFetching = true;
  const body = $('replay-body');
  if (body) body.textContent = 'Loading...';
  try {
    const d = await fetchJson('/api/replay_summary', 15000);
    if (d.error) { if (body) body.textContent = d.error; return; }
    const cf = obj(d.trail_counterfactual);
    const reasons = Object.entries(obj(d.close_reason_distribution)).slice(0, 6);
    let html = `<div class="sc-stat-row"><span class="sc-label">Post-fix trades</span><span class="sc-value">${n(d.post_fix_trades, 0)}</span></div>`;
    html += `<div class="sc-stat-row"><span class="sc-label">Win rate</span><span class="sc-value ${nn(d.win_rate) >= 35 ? 'c-green' : 'c-red'}">${pct(d.win_rate, 1)}</span></div>`;
    html += `<div class="sc-stat-row"><span class="sc-label">Total P&L</span><span class="sc-value ${nn(d.total_pnl) >= 0 ? 'c-green' : 'c-red'}">${pnlSign(nn(d.total_pnl))}</span></div>`;
    html += `<div class="sc-stat-row"><span class="sc-label">Avg ROI</span><span class="sc-value">${pct(d.avg_roi, 1)}</span></div>`;
    html += `<div class="sc-stat-row"><span class="sc-label">Real-price trades</span><span class="sc-value">${n(d.real_price_trades, 0)} (${pct(d.real_price_win_rate, 1)} WR)</span></div>`;
    html += `<div style="margin:6px 0 3px;font-size:11px;color:var(--text-muted)">Trail Counterfactual</div>`;
    html += `<div class="sc-stat-row"><span class="sc-label">Actual P&L</span><span class="sc-value">${pnlSign(nn(cf.actual_pnl))}</span></div>`;
    html += `<div class="sc-stat-row"><span class="sc-label">-6% trail P&L</span><span class="sc-value ${nn(cf.trail_6pct_pnl) > nn(cf.actual_pnl) ? 'c-green' : ''}">${pnlSign(nn(cf.trail_6pct_pnl))}</span></div>`;
    html += `<div class="sc-stat-row"><span class="sc-label">-8% trail P&L</span><span class="sc-value ${nn(cf.trail_8pct_pnl) > nn(cf.actual_pnl) ? 'c-green' : ''}">${pnlSign(nn(cf.trail_8pct_pnl))}</span></div>`;
    if (reasons.length) {
      html += `<div style="margin:6px 0 3px;font-size:11px;color:var(--text-muted)">Top Close Reasons</div>`;
      reasons.forEach(([r, cnt]) => {
        html += `<div class="sc-stat-row"><span class="sc-label">${r}</span><span class="sc-value">${cnt}</span></div>`;
      });
    }
    if (body) body.innerHTML = html;
    const ts = d.last_updated_ts;
    setText('replay-updated', ts ? new Date(ts * 1000).toLocaleTimeString() : '—');
  } catch (err) {
    if (body) body.textContent = `Error: ${err.message}`;
  } finally {
    _replayFetching = false;
  }
}

async function fetchExitLab() {
  if (_exitlabFetching) return;
  _exitlabFetching = true;
  const body = $('exitlab-body');
  if (body) body.textContent = 'Loading...';
  try {
    const d = await fetchJson('/api/exit_lab_summary?n=30', 15000);
    if (d.error) { if (body) body.textContent = d.error; return; }
    const strats = arr(d.strategies);
    if (!strats.length) { if (body) body.textContent = 'No trades with MFE > 0'; return; }
    let html = `<div style="margin-bottom:4px;font-size:11px;color:var(--text-muted)">${n(d.trade_count, 0)} trades analysed</div>`;
    html += `<table style="font-size:11px;width:100%"><thead><tr><th style="text-align:left">Strategy</th><th>Win%</th><th>Avg ROI</th><th>~P&L</th></tr></thead><tbody>`;
    strats.forEach(s => {
      const isBest = s.name === d.best_strategy;
      const cls = isBest ? 'style="background:rgba(0,208,132,0.12)"' : '';
      const pnlCls = nn(s.total_pnl) >= 0 ? 'c-green' : 'c-red';
      html += `<tr ${cls}><td>${s.name}${isBest ? ' *' : ''}</td><td style="text-align:right">${pct(s.win_rate, 1)}</td><td style="text-align:right">${pct(s.avg_roi, 1)}</td><td style="text-align:right" class="${pnlCls}">${pnlSign(nn(s.total_pnl))}</td></tr>`;
    });
    html += `</tbody></table>`;
    if (d.best_strategy && d.best_strategy !== strats[0]?.name) {
      html += `<div style="margin-top:6px;font-size:11px;color:var(--green)">Best: ${d.best_strategy}</div>`;
    }
    if (body) body.innerHTML = html;
    const ts = d.last_updated_ts;
    setText('exitlab-updated', ts ? new Date(ts * 1000).toLocaleTimeString() : '—');
  } catch (err) {
    if (body) body.textContent = `Error: ${err.message}`;
  } finally {
    _exitlabFetching = false;
  }
}

async function pollScorecard() {
  try {
    state.scorecard = await fetchJson('/api/scorecard?n=20', 8000);
    renderScorecard();
  } catch (err) {
    console.error('[scorecard poll]', err);
  }
}

async function pollPreEntry() {
  try {
    state.preentry = await fetchJson('/api/preentry_stats', 8000);
    renderPreEntryStats();
  } catch (err) {
    console.error('[preentry poll]', err);
  }
}

// ── Boot ────────────────────────────────────────────────────────
wireButtons();
showLoading('Connecting to telemetry...');
poll();
pollScorecard();
pollPreEntry();

// Fast poll: positions + command page (3s)
setInterval(poll, FAST_POLL_MS);
// Slow poll: scorecard + preentry (30s)
setInterval(pollScorecard, SCORECARD_POLL_MS);
setInterval(pollPreEntry, SCORECARD_POLL_MS);

// Update data age counter every second
setInterval(() => {
  if (state.lastBackendTs) setText('top-data-age', relAge(state.lastBackendTs));
}, 1000);
