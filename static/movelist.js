// movelist.js — clickable notation history with per-move quality colors.
// Additive module: everything arrives via the injected `api` (no imports from app.js).
// SAN is computed CLIENT-SIDE for DISPLAY ONLY (not identity) — the server stays the
// authority on legality + analysis. Mirrors the panel/feedback/shortcuts module pattern.

import { Chess } from 'https://esm.sh/chessops@0.14.2/chess';
import { parseFen } from 'https://esm.sh/chessops@0.14.2/fen';
import { parseUci } from 'https://esm.sh/chessops@0.14.2/util';
import { makeSanAndPlay } from 'https://esm.sh/chessops@0.14.2/san';

const byId = (id) => document.getElementById(id);

const UI_PREFS_KEY = 'chess-training:ui:v1';

function readUiPrefs() {
  try { return JSON.parse(localStorage.getItem(UI_PREFS_KEY) || '{}') || {}; } catch (_) { return {}; }
}

function writeUiPref(key, val) {
  try {
    const prefs = readUiPrefs();
    prefs[key] = val;
    localStorage.setItem(UI_PREFS_KEY, JSON.stringify(prefs));
  } catch (_) { /* best-effort */ }
}

let _api = null;

// Replay baseFen + moves to produce a SAN per ply (display only).
// Returns { sans, startFull, startWhite } or null on bad data.
function computeSans(baseFen, moves) {
  let setup, pos;
  try { setup = parseFen(baseFen).unwrap(); } catch (_) { return null; }
  try { pos = Chess.fromSetup(setup).unwrap(); } catch (_) { return null; }
  const startWhite = pos.turn === 'white';
  const startFull = setup.fullmoves;
  const sans = [];
  for (const uci of moves) {
    const move = parseUci(uci);
    if (!move) break;
    const san = makeSanAndPlay(pos, move); // generates SAN + applies the move
    if (!san || san === '--') break;       // illegal/null (shouldn't happen for legal history)
    sans.push(san);
  }
  return { sans, startFull, startWhite };
}

// A clickable move cell. Ply index `i` → clicking jumps to cursor = i+1 (position AFTER
// that move); current when cursor === i+1. Quality tints the text via the shared .q-* class.
function moveCell(san, i, quality, cursor) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'movelist-move';
  if (quality) btn.classList.add('q-' + quality);
  if (cursor === i + 1) {
    btn.classList.add('is-current');
    btn.setAttribute('aria-current', 'true');
  }
  btn.textContent = san;
  btn.addEventListener('click', () => { if (_api) _api.actions.goto(i + 1); });
  return btn;
}

function spanCell(cls, text) {
  const s = document.createElement('span');
  s.className = 'movelist-move ' + cls;
  if (text) s.textContent = text;
  return s;
}

function render() {
  const mount = byId('move-list');
  if (!mount || !_api) return;
  const state = _api.actions.getState();
  // Render in play mode and review mode. In all other modes (setup/trap/rep)
  // the movelist is hidden or irrelevant, so early-return.
  // Refuter resolution #3: broadened from play-only to play|review.
  if (state.mode && state.mode !== 'play' && state.mode !== 'review') return;
  const moves = state.moves || [];
  const quality = state.moveQuality || [];
  const cursor = state.cursor || 0;

  if (moves.length === 0) {
    mount.innerHTML = '<div class="movelist-empty">No moves yet.</div>';
    return;
  }

  const computed = computeSans(state.baseFen, moves);
  if (!computed || computed.sans.length === 0) { mount.innerHTML = ''; return; }
  const { sans, startFull, startWhite } = computed;

  const table = document.createElement('div');
  table.className = 'movelist-table';

  let i = 0;
  let full = startFull;
  let whiteToMove = startWhite;
  while (i < sans.length) {
    const row = document.createElement('div');
    row.className = 'movelist-row';

    const num = document.createElement('span');
    num.className = 'movelist-num';
    num.textContent = full + '.';
    row.appendChild(num);

    if (whiteToMove) {
      row.appendChild(moveCell(sans[i], i, quality[i], cursor));
      if (i + 1 < sans.length) row.appendChild(moveCell(sans[i + 1], i + 1, quality[i + 1], cursor));
      else row.appendChild(spanCell('movelist-empty-cell'));
      i += 2;
    } else {
      // Line begins with Black to move: "N. … <black>", then rows are normal.
      row.appendChild(spanCell('movelist-ellipsis', '…'));
      row.appendChild(moveCell(sans[i], i, quality[i], cursor));
      i += 1;
      whiteToMove = true;
    }
    full += 1;
    table.appendChild(row);
  }

  mount.innerHTML = '';
  mount.appendChild(table);

  const active = mount.querySelector('.movelist-move.is-current');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

export function initMovelist(api) {
  _api = api;

  const toggle = byId('movelist-toggle');
  const block = toggle ? toggle.closest('.movelist-block') : null;

  // Apply saved collapsed state before first paint.
  if (toggle && block && readUiPrefs().moveListCollapsed) {
    block.classList.add('collapsed');
    toggle.setAttribute('aria-expanded', 'false');
  }

  // Wire click handler — native <button> handles Enter/Space automatically.
  if (toggle && block) {
    toggle.addEventListener('click', () => {
      const isNowCollapsed = block.classList.toggle('collapsed');
      toggle.setAttribute('aria-expanded', String(!isNowCollapsed));
      writeUiPref('moveListCollapsed', isNowCollapsed);
    });
  }

  if (api && api.on) api.on('position:change', render);
  render(); // initial paint
}
