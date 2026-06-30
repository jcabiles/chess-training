// review.js — Game library, import, profile dashboard, and foresight cards.
//
// Injected-api module: all app dependencies arrive via `api` (no imports from app.js).
// Owns the #tab-review panel, #review-foresight, and reacts to the review:ply event.
//
// One-directional: review.js → api (never api.js → review.js directly).
// Contract: never modifies the localStorage session shape, never persists review state.

const byId = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------
let _api = null;
let _reviewData = null;  // ReviewResponse from GET /api/games/{id}/review (leaks + plies)
let _openedGameId = null;
let _analyzeAllInterval = null;  // polling interval for analyze-all progress

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'className') e.className = v;
    else if (k === 'textContent') e.textContent = v;
    else e.setAttribute(k, v);
  }
  for (const child of children) {
    if (typeof child === 'string') e.appendChild(document.createTextNode(child));
    else if (child) e.appendChild(child);
  }
  return e;
}

function fmt(val, fallback = '—') {
  if (val === null || val === undefined || val === '') return fallback;
  return String(val);
}

// ---------------------------------------------------------------------------
// API calls (all fetch — no postJSON from app.js to keep modules one-directional)
// ---------------------------------------------------------------------------

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${url}`);
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${url}`);
  return res.json();
}

async function patchJSON(url, body) {
  const res = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${url}`);
  return res.json();
}

async function deleteGame(id) {
  const res = await fetch(`/api/games/${id}`, { method: 'DELETE' });
  if (!res.ok) throw new Error(`HTTP ${res.status}: DELETE /api/games/${id}`);
}

// ---------------------------------------------------------------------------
// Game Library — list + import
// ---------------------------------------------------------------------------

function resultBadge(result, myColor) {
  if (!result) return '';
  // Determine if the user won/lost/drew based on my_color + PGN result.
  const r = result.trim();
  if (r === '1/2-1/2') return 'draw';
  if (!myColor) return r;
  if (myColor === 'white') return r === '1-0' ? 'win' : 'loss';
  if (myColor === 'black') return r === '0-1' ? 'win' : 'loss';
  return r;
}

function statusLabel(status) {
  const labels = { pending: 'Pending', analyzing: 'Analyzing…', done: 'Analyzed', failed: 'Failed' };
  return labels[status] || status;
}

function renderCoverageHint(games, profile) {
  const total = profile ? profile.games_total : (games ? games.length : 0);
  const tagged = profile ? profile.games_tagged : (games ? games.filter((g) => g.my_color).length : 0);
  const analyzed = profile ? profile.games_analyzed : 0;

  const wrap = el('div', { className: 'review-coverage' });

  const hint = el('div', { className: 'review-coverage-line' });
  hint.appendChild(el('span', {
    className: 'review-coverage-stat',
    textContent: `${tagged} of ${total} games tagged`,
  }));
  hint.appendChild(el('span', { className: 'review-coverage-sep', textContent: '·' }));
  hint.appendChild(el('span', {
    className: 'review-coverage-stat',
    textContent: `${analyzed} analyzed`,
  }));
  wrap.appendChild(hint);

  // Nudge when many games are untagged and total > 0.
  const untagged = total - tagged;
  if (total > 0 && untagged > 0) {
    const nudge = el('div', { className: 'review-coverage-nudge' });
    nudge.textContent =
      `${untagged} game${untagged !== 1 ? 's' : ''} have no color tag — ` +
      'the profile only counts tagged games. Use "Set my color from username" below to tag them in bulk.';
    wrap.appendChild(nudge);
  }

  return wrap;
}

function renderBulkControls() {
  const wrap = el('div', { className: 'review-bulk' });

  // --- "Set my color from username" ---
  const retagSection = el('div', { className: 'review-bulk-section' });
  retagSection.appendChild(el('div', { className: 'review-import-title', textContent: 'Set my color from username' }));

  const retagRow = el('div', { className: 'review-bulk-row' });
  const retagInput = el('input', {
    type: 'text',
    className: 'review-bulk-input',
    placeholder: 'username (or alias1,alias2)',
  });
  retagInput.setAttribute('aria-label', 'Chess username for bulk color tagging');
  const retagBtn = el('button', { className: 'review-btn review-btn-primary', textContent: 'Tag games' });
  retagRow.append(retagInput, retagBtn);

  const retagStatus = el('div', { className: 'review-import-status' });

  retagBtn.addEventListener('click', async () => {
    const username = retagInput.value.trim();
    if (!username) {
      retagStatus.textContent = 'Enter a username first.';
      retagStatus.className = 'review-import-status error';
      return;
    }
    retagBtn.disabled = true;
    retagStatus.textContent = 'Tagging…';
    retagStatus.className = 'review-import-status';
    try {
      const data = await postJSON('/api/games/retag-color', { username });
      retagStatus.textContent = `${data.updated} game${data.updated !== 1 ? 's' : ''} tagged.`;
      retagStatus.className = 'review-import-status success';
      showToast(`${data.updated} games tagged.`);
      await refreshLibraryAndProfile();
    } catch (err) {
      retagStatus.textContent = `Failed: ${err.message}`;
      retagStatus.className = 'review-import-status error';
    } finally {
      retagBtn.disabled = false;
    }
  });

  retagSection.append(retagRow, retagStatus);

  // --- "Analyze all" ---
  const analyzeSection = el('div', { className: 'review-bulk-section' });
  analyzeSection.appendChild(el('div', { className: 'review-import-title', textContent: 'Bulk analyze' }));

  const analyzeRow = el('div', { className: 'review-bulk-row' });
  const analyzeAllBtn = el('button', { className: 'review-btn review-btn-primary', textContent: 'Analyze all pending' });
  analyzeRow.appendChild(analyzeAllBtn);

  const analyzeProgress = el('span', { className: 'review-bulk-progress' });
  analyzeRow.appendChild(analyzeProgress);

  analyzeAllBtn.addEventListener('click', () => triggerAnalyzeAll(analyzeAllBtn, analyzeProgress));

  // If a poll is already running (re-render mid-flight), disable the button
  if (_analyzeAllInterval !== null) {
    analyzeAllBtn.disabled = true;
    analyzeProgress.textContent = 'Analyzing…';
  }

  analyzeSection.appendChild(analyzeRow);

  wrap.append(retagSection, analyzeSection);
  return wrap;
}

function renderLibrary(games, profile) {
  const host = byId('review-library');
  if (!host) return;
  host.replaceChildren();

  // Section header
  const hdr = el('div', { className: 'review-section-hdr' });
  hdr.appendChild(el('span', { className: 'review-section-title', textContent: 'Game Library' }));
  host.appendChild(hdr);

  // Coverage hint
  host.appendChild(renderCoverageHint(games, profile));

  // Bulk controls (retag + analyze-all)
  host.appendChild(renderBulkControls());

  // Import controls
  host.appendChild(renderImportControls());

  // Game list
  const listWrap = el('div', { className: 'review-game-list' });

  if (!games || games.length === 0) {
    listWrap.appendChild(el('div', { className: 'review-empty', textContent: 'No games imported yet. Paste a PGN below to get started.' }));
    host.appendChild(listWrap);
    return;
  }

  for (const game of games) {
    const badge = resultBadge(game.result, game.my_color);
    const row = el('div', { className: 'review-game-row' });

    // Players + result
    const players = el('div', { className: 'review-game-players' });
    const white = el('span', { className: 'review-player review-player-white', textContent: fmt(game.white) });
    const vs = el('span', { className: 'review-vs', textContent: 'vs' });
    const black = el('span', { className: 'review-player review-player-black', textContent: fmt(game.black) });
    players.append(white, vs, black);

    const meta = el('div', { className: 'review-game-meta' });

    if (badge) {
      const badgeEl = el('span', { className: `review-badge review-badge-${badge}`, textContent: badge });
      meta.appendChild(badgeEl);
    }
    if (game.opening) {
      meta.appendChild(el('span', { className: 'review-game-opening', textContent: game.opening }));
    }
    if (game.date) {
      meta.appendChild(el('span', { className: 'review-game-date', textContent: game.date }));
    }
    meta.appendChild(el('span', { className: `review-status review-status-${game.analysis_status}`, textContent: statusLabel(game.analysis_status) }));

    // Per-game color control
    const colorRow = el('div', { className: 'review-game-color-row' });
    const colorLabel = el('span', { className: 'review-game-color-label', textContent: 'My color:' });
    const colorSel = el('select', { className: 'review-color-select review-color-select-sm' });
    colorSel.setAttribute('aria-label', 'Set my color for this game');
    [
      { value: '', text: '— unknown' },
      { value: 'white', text: 'White' },
      { value: 'black', text: 'Black' },
    ].forEach(({ value, text }) => {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = text;
      if ((game.my_color || '') === value) opt.selected = true;
      colorSel.appendChild(opt);
    });

    const colorStatus = el('span', { className: 'review-game-color-status' });

    colorSel.addEventListener('change', async () => {
      const newColor = colorSel.value || null;
      colorStatus.textContent = 'Saving…';
      colorStatus.className = 'review-game-color-status';
      try {
        await patchJSON(`/api/games/${game.id}`, { my_color: newColor });
        colorStatus.textContent = 'Saved — pending re-analysis';
        colorStatus.className = 'review-game-color-status review-game-color-status-ok';
        await refreshLibraryAndProfile();
      } catch (err) {
        colorStatus.textContent = `Failed: ${err.message}`;
        colorStatus.className = 'review-game-color-status review-game-color-status-err';
      }
    });

    colorRow.append(colorLabel, colorSel, colorStatus);

    // Actions
    const actions = el('div', { className: 'review-game-actions' });

    const openBtn = el('button', { className: 'review-btn review-btn-primary', textContent: 'Open' });
    openBtn.addEventListener('click', () => openGame(game.id));

    const analyzeBtn = el('button', {
      className: 'review-btn',
      textContent: game.analysis_status === 'done' ? 'Re-analyze' : 'Analyze',
    });
    analyzeBtn.disabled = game.analysis_status === 'analyzing';
    analyzeBtn.addEventListener('click', () => triggerAnalysis(game.id, analyzeBtn, statusEl));

    const deleteBtn = el('button', { className: 'review-btn review-btn-danger', textContent: 'Delete' });
    deleteBtn.addEventListener('click', () => deleteAndRefresh(game.id));

    actions.append(openBtn, analyzeBtn, deleteBtn);

    // Status (live polling placeholder)
    const statusEl = el('span', { className: 'review-game-status-live' });

    row.append(players, meta, colorRow, actions, statusEl);
    listWrap.appendChild(row);
  }

  host.appendChild(listWrap);
}

function renderImportControls() {
  const wrap = el('div', { className: 'review-import' });

  const title = el('div', { className: 'review-import-title', textContent: 'Import PGN' });

  const textarea = el('textarea', {
    className: 'review-pgn-input',
    placeholder: 'Paste PGN text here (one or more games)…',
    rows: '5',
  });

  // File upload
  const fileRow = el('div', { className: 'review-import-file-row' });
  const fileInput = el('input', { type: 'file', accept: '.pgn,.txt', className: 'review-file-input', id: 'review-file-input' });
  const fileLabel = el('label', { 'for': 'review-file-input', className: 'review-btn review-btn-file', textContent: 'Choose file' });

  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const text = await file.text();
    textarea.value = text;
  });
  fileRow.append(fileLabel, fileInput);

  // Color override
  const colorRow = el('div', { className: 'review-import-options' });
  const colorLabel = el('label', { className: 'review-option-label', textContent: 'My color: ' });
  const colorSel = el('select', { className: 'review-color-select' });
  ['(auto-detect)', 'white', 'black'].forEach((v) => {
    const opt = document.createElement('option');
    opt.value = v === '(auto-detect)' ? '' : v;
    opt.textContent = v;
    colorSel.appendChild(opt);
  });
  colorRow.append(colorLabel, colorSel);

  const importBtn = el('button', { className: 'review-btn review-btn-primary', textContent: 'Import' });
  const importStatus = el('div', { className: 'review-import-status' });

  importBtn.addEventListener('click', async () => {
    const pgn = textarea.value.trim();
    if (!pgn) {
      importStatus.textContent = 'Please paste a PGN first.';
      importStatus.className = 'review-import-status error';
      return;
    }
    importBtn.disabled = true;
    importStatus.textContent = 'Importing…';
    importStatus.className = 'review-import-status';
    try {
      const body = { pgn };
      const myColor = colorSel.value;
      if (myColor) body.my_color = myColor;
      const data = await postJSON('/api/games/import', body);
      importStatus.textContent = `Imported ${data.imported} game(s). ${data.duplicates ? data.duplicates + ' duplicate(s) skipped.' : ''}`;
      importStatus.className = 'review-import-status success';
      textarea.value = '';
      await refreshLibraryAndProfile();
    } catch (err) {
      importStatus.textContent = `Import failed: ${err.message}`;
      importStatus.className = 'review-import-status error';
    } finally {
      importBtn.disabled = false;
    }
  });

  wrap.append(title, textarea, fileRow, colorRow, importBtn, importStatus);
  return wrap;
}

async function triggerAnalysis(gameId, btn, statusEl) {
  btn.disabled = true;
  if (statusEl) statusEl.textContent = 'Starting…';
  try {
    await postJSON(`/api/games/${gameId}/analyze`, {});
    if (statusEl) statusEl.textContent = 'Analysis started.';
    // Poll status briefly.
    pollStatus(gameId, btn, statusEl);
  } catch (err) {
    if (statusEl) statusEl.textContent = `Failed: ${err.message}`;
    btn.disabled = false;
  }
}

function pollStatus(gameId, btn, statusEl) {
  let attempts = 0;
  const max = 60; // poll up to ~60s
  const interval = setInterval(async () => {
    attempts++;
    try {
      const data = await fetchJSON(`/api/games/${gameId}/status`);
      if (statusEl) statusEl.textContent = statusLabel(data.analysis_status);
      if (data.analysis_status === 'done' || data.analysis_status === 'failed' || attempts >= max) {
        clearInterval(interval);
        if (btn) btn.disabled = false;
        await refreshLibraryAndProfile();
      }
    } catch (_) {
      clearInterval(interval);
      if (btn) btn.disabled = false;
    }
  }, 1000);
}

async function triggerAnalyzeAll(btn, progressEl) {
  if (_analyzeAllInterval !== null) return; // already running
  btn.disabled = true;
  if (progressEl) progressEl.textContent = 'Starting…';
  try {
    const data = await postJSON('/api/games/analyze-all', {});
    const initialPending = data.pending;
    if (initialPending === 0) {
      if (progressEl) progressEl.textContent = 'No pending games.';
      btn.disabled = false;
      return;
    }
    if (progressEl) progressEl.textContent = `Analyzing… ${initialPending} pending`;

    // Poll /api/profile every ~1.5s for updated pending count.
    _analyzeAllInterval = setInterval(async () => {
      try {
        const profile = await fetchJSON('/api/profile');
        const games = await fetchJSON('/api/games').catch(() => []);
        const pending = games.filter((g) => g.analysis_status === 'pending' || g.analysis_status === 'analyzing').length;
        if (progressEl) {
          progressEl.textContent = pending > 0 ? `Analyzing… ${pending} pending` : 'Done.';
        }
        renderLibrary(games, profile);
        renderProfile(profile);
        if (pending === 0) {
          clearInterval(_analyzeAllInterval);
          _analyzeAllInterval = null;
          btn.disabled = false;
          showToast('All games analyzed.');
          // Final full refresh to ensure consistent state.
          await refreshLibraryAndProfile();
        }
      } catch (_) {
        // Network blip — keep polling, do not stop.
      }
    }, 1500);
  } catch (err) {
    if (progressEl) progressEl.textContent = `Failed: ${err.message}`;
    btn.disabled = false;
  }
}

function showToast(message) {
  const container = byId('toasts');
  if (!container) return;
  const toast = el('div', { className: 'review-toast', textContent: message });
  container.appendChild(toast);
  // Auto-remove after 3.5s.
  setTimeout(() => {
    toast.classList.add('review-toast-fade');
    setTimeout(() => toast.remove(), 400);
  }, 3100);
}

async function deleteAndRefresh(gameId) {
  try {
    await deleteGame(gameId);
    await refreshLibraryAndProfile();
  } catch (err) {
    // Degraded — just refresh.
    await refreshLibraryAndProfile().catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// Open a game → enter review mode
// ---------------------------------------------------------------------------

async function openGame(gameId) {
  let gameDetail;
  try {
    gameDetail = await fetchJSON(`/api/games/${gameId}`);
  } catch (err) {
    return;
  }

  _openedGameId = gameId;
  _reviewData = null; // clear stale data

  // Enter review mode (sets board + state via api.actions).
  if (_api && _api.actions && _api.actions.enterReview) {
    _api.actions.enterReview(gameDetail);
  }

  // Activate the Analysis panel directly (the tab-click handler is gated to play
  // mode only, so clicking the button is a no-op in review mode).
  const tabsEl = byId('panel-tabs');
  if (tabsEl) {
    tabsEl.querySelectorAll('button[data-tab]').forEach((b) => {
      const on = b.dataset.tab === 'analysis';
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', String(on));
    });
  }
  ['analysis', 'opening', 'traps', 'repertoire', 'review'].forEach((name) => {
    const panel = byId(`tab-${name}`);
    if (panel) panel.classList.toggle('is-active', name === 'analysis');
  });

  // Load review data (leaks + foresight) in the background if analysis is done.
  if (gameDetail.analysis_status === 'done') {
    loadReviewData(gameId);
  }
}

async function loadReviewData(gameId) {
  try {
    _reviewData = await fetchJSON(`/api/games/${gameId}/review`);
    // Trigger foresight render for current ply (which is 0 at entry).
    const currentState = _api && _api.actions && _api.actions.getState();
    if (currentState) renderForesight(currentState.cursor);
    renderGameSummary(_reviewData && _reviewData.summary ? _reviewData.summary : null);
  } catch (_) {
    _reviewData = null;
  }
}

// ---------------------------------------------------------------------------
// Game summary — shown in #review-game-summary in the review-bar
// ---------------------------------------------------------------------------

function renderGameSummary(summary) {
  const el_ = document.getElementById('review-game-summary');
  if (!el_) return;

  if (!summary || (summary.white_accuracy == null && summary.black_accuracy == null)) {
    el_.hidden = true;
    el_.replaceChildren();
    return;
  }

  // Determine display order and labels based on my_color.
  let first, second;
  if (summary.my_color === 'white') {
    first  = { label: 'You',      acc: summary.white_accuracy, elo: summary.white_elo };
    second = { label: 'Opponent', acc: summary.black_accuracy, elo: summary.black_elo };
  } else if (summary.my_color === 'black') {
    first  = { label: 'You',      acc: summary.black_accuracy, elo: summary.black_elo };
    second = { label: 'Opponent', acc: summary.white_accuracy, elo: summary.white_elo };
  } else {
    first  = { label: 'White', acc: summary.white_accuracy, elo: summary.white_elo };
    second = { label: 'Black', acc: summary.black_accuracy, elo: summary.black_elo };
  }

  const fmtAcc = (acc) => (acc == null ? '—' : acc.toFixed(1) + '%');
  const fmtElo = (elo) => (elo == null ? '—' : '~' + elo);

  const buildSide = ({ label, acc, elo }) => {
    const side = el('div', { className: 'rgs-side' });
    side.appendChild(el('span', { className: 'rgs-label', textContent: label }));
    side.appendChild(el('span', { className: 'rgs-acc',   textContent: fmtAcc(acc) }));

    const eloSpan = el('span', {
      className: 'rgs-elo',
      title: 'Estimated from this single game — a rough heuristic, not an official rating.',
    });
    eloSpan.appendChild(document.createTextNode(fmtElo(elo) + ' '));
    eloSpan.appendChild(el('span', { className: 'rgs-est', textContent: 'est.' }));
    side.appendChild(eloSpan);

    return side;
  };

  el_.replaceChildren(buildSide(first), buildSide(second));
  el_.hidden = false;
}

// ---------------------------------------------------------------------------
// Foresight cards — shown in #review-foresight in the review-bar
// ---------------------------------------------------------------------------

function renderForesight(ply) {
  const host = byId('review-foresight');
  if (!host) return;
  host.replaceChildren();

  if (!_reviewData || !_reviewData.leaks || _reviewData.leaks.length === 0) return;
  const leaks = _reviewData.leaks;

  // Check if the current ply matches any lead_in_ply (show warning card BEFORE the blunder).
  const leadInLeaks = leaks.filter((l) => l.lead_in_ply === ply);
  // Check if the current ply matches any blunder ply (tie-back warning).
  const blunderLeaks = leaks.filter((l) => l.ply === ply);

  if (leadInLeaks.length === 0 && blunderLeaks.length === 0) return;

  // Lead-in warnings (highest priority — shown before the blunder occurs).
  for (const leak of leadInLeaks) {
    host.appendChild(renderForesightCard(leak, 'lead-in'));
  }

  // Blunder tie-back (gentler — the foresight already warned you).
  for (const leak of blunderLeaks) {
    // Only show tie-back if we haven't already shown a lead-in for this exact leak.
    const alreadyShown = leadInLeaks.some((l) => l.id === leak.id);
    if (!alreadyShown) {
      host.appendChild(renderForesightCard(leak, 'tie-back'));
    }
  }
}

function renderForesightCard(leak, kind) {
  const card = el('div', { className: `review-foresight-card review-foresight-${kind}` });

  // Header row: severity badge + phase.
  const hdr = el('div', { className: 'review-foresight-hdr' });
  const badge = el('span', {
    className: `review-severity review-severity-${leak.severity}`,
    textContent: kind === 'tie-back' ? 'Here it cost you' : `Watch out — ${leak.severity}`,
  });
  const phase = el('span', { className: 'review-foresight-phase', textContent: leak.phase });
  hdr.append(badge, phase);
  card.appendChild(hdr);

  if (kind === 'tie-back') {
    const tieBack = el('p', {
      className: 'review-foresight-tieback',
      textContent: `This is the ${leak.severity} (ply ${leak.ply}). The warning was shown at ply ${leak.lead_in_ply}.`,
    });
    if (leak.best_san) {
      const best = el('p', { className: 'review-foresight-best', textContent: `Best: ${leak.best_san}` });
      card.append(tieBack, best);
    } else {
      card.appendChild(tieBack);
    }
    return card;
  }

  // Lead-in card: narration buckets (Threat / Hanging / Plan / Summary).
  const narration = leak.narration || {};

  if (narration.threat) {
    const section = el('div', { className: 'review-narration-bucket' });
    section.appendChild(el('div', { className: 'review-bucket-label', textContent: 'Threat' }));
    section.appendChild(el('p', { className: 'review-bucket-text', textContent: narration.threat }));
    card.appendChild(section);
  }

  if (narration.hanging) {
    const section = el('div', { className: 'review-narration-bucket' });
    section.appendChild(el('div', { className: 'review-bucket-label', textContent: 'Hanging' }));
    section.appendChild(el('p', { className: 'review-bucket-text', textContent: narration.hanging }));
    card.appendChild(section);
  }

  if (narration.plan) {
    const section = el('div', { className: 'review-narration-bucket' });
    section.appendChild(el('div', { className: 'review-bucket-label', textContent: 'Plan' }));
    section.appendChild(el('p', { className: 'review-bucket-text', textContent: narration.plan }));
    card.appendChild(section);
  }

  if (narration.summary) {
    const section = el('div', { className: 'review-narration-bucket review-narration-summary' });
    section.appendChild(el('p', { className: 'review-bucket-text', textContent: narration.summary }));
    card.appendChild(section);
  }

  // Category + win-prob drop (context, not a scolding).
  const ctx = el('div', { className: 'review-foresight-ctx' });
  if (leak.category) ctx.appendChild(el('span', { className: 'review-category', textContent: leak.category }));
  if (typeof leak.win_prob_drop === 'number') {
    const pct = Math.round(leak.win_prob_drop * 100);
    ctx.appendChild(el('span', { className: 'review-win-drop', textContent: `Win% −${pct}%` }));
  }
  if (ctx.children.length) card.appendChild(ctx);

  return card;
}

// ---------------------------------------------------------------------------
// Profile dashboard
// ---------------------------------------------------------------------------

async function refreshLibraryAndProfile() {
  try {
    const [games, profile] = await Promise.all([
      fetchJSON('/api/games').catch(() => []),
      fetchJSON('/api/profile').catch(() => null),
    ]);
    renderLibrary(games, profile);
    renderProfile(profile);
  } catch (_) {
    // Degraded — render empty states.
    renderLibrary([], null);
    renderProfile(null);
  }
}

function renderProfile(profile) {
  const host = byId('review-profile');
  if (!host) return;
  host.replaceChildren();

  if (!profile || profile.games_analyzed === 0) {
    const emptyWrap = el('div', { className: 'review-profile-wrap' });
    // Show coverage header even when no analysis yet (so user knows what to do).
    emptyWrap.appendChild(el('div', { className: 'review-section-hdr' }, [
      el('span', { className: 'review-section-title', textContent: 'Your Coaching Profile' }),
    ]));
    if (profile && profile.games_total > 0) {
      const statsRow = el('div', { className: 'review-stats-row' });
      statsRow.appendChild(reviewStat('Total Games', profile.games_total));
      statsRow.appendChild(reviewStat('Tagged', profile.games_tagged));
      statsRow.appendChild(reviewStat('Analyzed', profile.games_analyzed));
      emptyWrap.appendChild(statsRow);
    }
    emptyWrap.appendChild(el('div', { className: 'review-profile-empty', textContent: 'No analyzed games yet. Import and analyze some games to see your coaching profile.' }));
    host.appendChild(emptyWrap);
    return;
  }

  const wrap = el('div', { className: 'review-profile-wrap' });

  // Header
  wrap.appendChild(el('div', { className: 'review-section-hdr' }, [
    el('span', { className: 'review-section-title', textContent: 'Your Coaching Profile' }),
  ]));

  // Stats row: total / tagged / analyzed + hope-chess rate.
  const statsRow = el('div', { className: 'review-stats-row' });
  if (profile.games_total > 0) {
    statsRow.appendChild(reviewStat('Total Games', profile.games_total));
    statsRow.appendChild(reviewStat('Tagged', profile.games_tagged));
  }
  statsRow.appendChild(reviewStat('Games Analyzed', profile.games_analyzed));
  const hopeRate = typeof profile.hope_chess_rate === 'number'
    ? Math.round(profile.hope_chess_rate * 100) + '%'
    : '—';
  statsRow.appendChild(reviewStat('Hope Chess Rate', hopeRate, 'Games where you missed a developing threat'));
  wrap.appendChild(statsRow);

  // Top leaks.
  if (profile.top_leaks && profile.top_leaks.length > 0) {
    wrap.appendChild(el('div', { className: 'review-subsection-title', textContent: 'Top Recurring Mistakes' }));
    const leakList = el('div', { className: 'review-leak-list' });
    for (const leak of profile.top_leaks) {
      const row = el('div', { className: 'review-leak-row' });
      const name = el('span', { className: 'review-leak-category', textContent: leak.category || leak.coach || '—' });
      const count = el('span', { className: 'review-leak-count', textContent: `${leak.count}×` });
      if (leak.coach && leak.category && leak.coach !== leak.category) {
        const coach = el('span', { className: 'review-leak-coach', textContent: leak.coach });
        row.append(name, coach, count);
      } else {
        row.append(name, count);
      }
      leakList.appendChild(row);
    }
    wrap.appendChild(leakList);
  }

  // By phase.
  if (profile.by_phase && Object.keys(profile.by_phase).length > 0) {
    wrap.appendChild(el('div', { className: 'review-subsection-title', textContent: 'By Game Phase' }));
    const phaseWrap = el('div', { className: 'review-phase-list' });
    for (const [phase, count] of Object.entries(profile.by_phase)) {
      phaseWrap.appendChild(el('div', { className: 'review-phase-row' }, [
        el('span', { className: 'review-phase-name', textContent: phase }),
        el('span', { className: 'review-phase-count', textContent: String(count) }),
      ]));
    }
    wrap.appendChild(phaseWrap);
  }

  // By color.
  if (profile.by_color && Object.keys(profile.by_color).length > 0) {
    wrap.appendChild(el('div', { className: 'review-subsection-title', textContent: 'By Color' }));
    const colorWrap = el('div', { className: 'review-phase-list' });
    for (const [color, count] of Object.entries(profile.by_color)) {
      colorWrap.appendChild(el('div', { className: 'review-phase-row' }, [
        el('span', { className: 'review-phase-name', textContent: color }),
        el('span', { className: 'review-phase-count', textContent: String(count) }),
      ]));
    }
    wrap.appendChild(colorWrap);
  }

  // By opening.
  if (profile.by_opening && profile.by_opening.length > 0) {
    wrap.appendChild(el('div', { className: 'review-subsection-title', textContent: 'By Opening' }));
    const opList = el('div', { className: 'review-opening-list' });
    for (const op of profile.by_opening.slice(0, 5)) {
      opList.appendChild(el('div', { className: 'review-opening-row' }, [
        el('span', { className: 'review-opening-name', textContent: op.opening || op.eco || '—' }),
        el('span', { className: 'review-opening-count', textContent: `${op.count}×` }),
      ]));
    }
    wrap.appendChild(opList);
  }

  // Trend.
  if (profile.trend && profile.trend.length > 0) {
    wrap.appendChild(el('div', { className: 'review-subsection-title', textContent: 'Trend' }));
    const trendWrap = el('div', { className: 'review-trend' });
    for (const bucket of profile.trend) {
      const bucketEl = el('div', { className: 'review-trend-bucket' });
      bucketEl.appendChild(el('span', { className: 'review-trend-date', textContent: bucket.bucket || '' }));
      bucketEl.appendChild(el('span', { className: 'review-trend-count', textContent: String(bucket.count || 0) }));
      trendWrap.appendChild(bucketEl);
    }
    wrap.appendChild(trendWrap);
  }

  host.appendChild(wrap);
}

function reviewStat(label, value, subtitle) {
  const stat = el('div', { className: 'review-stat' });
  stat.appendChild(el('div', { className: 'review-stat-value', textContent: String(value) }));
  stat.appendChild(el('div', { className: 'review-stat-label', textContent: label }));
  if (subtitle) stat.appendChild(el('div', { className: 'review-stat-sub', textContent: subtitle }));
  return stat;
}

// ---------------------------------------------------------------------------
// Keyboard navigation in review mode (← →)
// ---------------------------------------------------------------------------

function onKeyDown(e) {
  const state = _api && _api.actions && _api.actions.getState();
  if (!state || state.mode !== 'review') return;
  if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT')) return;

  if (e.key === 'ArrowLeft') {
    e.preventDefault();
    _api.actions.goto(Math.max(0, state.cursor - 1));
  } else if (e.key === 'ArrowRight') {
    e.preventDefault();
    _api.actions.goto(Math.min(state.moves.length, state.cursor + 1));
  }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export function initReview(api) {
  _api = api;

  // Subscribe to review:ply to drive foresight cards.
  if (api && api.on) {
    api.on('review:ply', (ply) => {
      renderForesight(ply);
    });
  }

  // Keyboard nav.
  document.addEventListener('keydown', onKeyDown);

  // Initial load of library + profile.
  refreshLibraryAndProfile();
}
