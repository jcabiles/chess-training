// Stockfish Analysis Board — frontend.
//
// chessground = board rendering (no chess rules).
// chessops    = chess rules: legal-move generation (dests), FEN, UCI parsing.
// The Python server is the authority on move legality + analysis; chessops here
// gives instant legal-dest highlighting / snap-back without a round-trip.
//
// FSM states (state.mode):
//   'play'          — one legal move at a time, evaluator on (the default).
//   'setup'         — free piece placement / palette editor, evaluator paused. On
//                     "Begin Game" the position is validated + committed as the new
//                     starting position (reusing the /api/load path).
//   'study'         — read-only walkthrough of an opening line (view-only board).
//   'trap-watch'    — read-only step-through of a trap variation (view-only board).
//   'trap-practice' — interactive drill: the app auto-plays the victim's scripted
//                     replies; the user must find each trapper move. Legal-but-wrong
//                     moves snap back ("try again"); no quality label is ever shown.
//
// TODO(vendor): imported from a CDN (esm.sh resolves deps). To go offline,
// vendor chessground + chessops into static/vendor/ and update these imports.
import { Chessground } from 'https://esm.sh/chessground@9.1.1';
import { Chess } from 'https://esm.sh/chessops@0.14.2/chess';
import { parseFen, makeFen, INITIAL_FEN } from 'https://esm.sh/chessops@0.14.2/fen';
import { chessgroundDests } from 'https://esm.sh/chessops@0.14.2/compat';
import { parseSquare, parseUci } from 'https://esm.sh/chessops@0.14.2/util';

const EMPTY_PLACEMENT = '8/8/8/8/8/8/8/8';
const INITIAL_PLACEMENT = INITIAL_FEN.split(' ')[0];

// Palette piece codes → chessground piece objects.
const PIECE_CODES = {
  wK: { color: 'white', role: 'king' },   wQ: { color: 'white', role: 'queen' },
  wR: { color: 'white', role: 'rook' },    wB: { color: 'white', role: 'bishop' },
  wN: { color: 'white', role: 'knight' },  wP: { color: 'white', role: 'pawn' },
  bK: { color: 'black', role: 'king' },    bQ: { color: 'black', role: 'queen' },
  bR: { color: 'black', role: 'rook' },     bB: { color: 'black', role: 'bishop' },
  bN: { color: 'black', role: 'knight' },   bP: { color: 'black', role: 'pawn' },
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  mode: 'play',     // 'play' | 'setup' | 'study' | 'trap-watch' | 'trap-practice'
  baseFen: INITIAL_FEN,
  moves: [],        // UCI strings applied from baseFen
  cursor: 0,        // how many of `moves` are currently applied
  orientation: 'white',
  setupColor: 'white', // side-to-move while in setup mode
};

let ground = null;
let playSnapshot = null;   // saved play state captured when entering setup (for Cancel)
let brush = null;          // setup tool: null = move/drag, 'erase', or a piece object
let studySnapshot = null;  // saved play state captured when entering study OR trap (separate!)
let study = null;          // active study walkthrough { name, eco, san, uci, fens, step, max }
let openingFilter = '';    // current candidate filter text
let studyEvalToken = 0;    // guards out-of-order async eval/commentary while stepping (study + trap-watch share)
let trapsData = [];        // all trap summaries fetched on load
let trapsCheckToken = 0;   // guards stale /api/traps/check responses (mirrors studyEvalToken)
let trapChipDismissedFen = null; // sticky dismiss: EPD/FEN string for which the chip was dismissed
// trap-watch state
let trap = null;           // active trap: { id, name, mainLine, startFen, step, fens, lastUcis }

const byId = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Session persistence (localStorage). Mode-aware: in setup we save the working
// placement + the snapshot so a refresh keeps an in-progress setup AND its
// Cancel target. Legacy {baseFen,moves,...} entries (no `mode`) load as play.
// ---------------------------------------------------------------------------
const STORAGE_KEY = 'chess-training:session:v1';

function persist() {
  if (state.mode === 'study') return;        // study is transient — never persisted
  if (state.mode === 'trap-watch') return;   // trap modes are transient — never persisted
  if (state.mode === 'trap-practice') return;// (both watch + practice)
  try {
    let data;
    if (state.mode === 'setup') {
      data = {
        mode: 'setup',
        orientation: state.orientation,
        setupPlacement: ground ? ground.getFen() : INITIAL_PLACEMENT,
        setupColor: state.setupColor,
        snapshot: playSnapshot,
      };
    } else {
      data = {
        mode: 'play',
        baseFen: state.baseFen,
        moves: state.moves,
        cursor: state.cursor,
        orientation: state.orientation,
      };
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch (_) {
    // Storage full / disabled (e.g. private mode) — saving is best-effort.
  }
}

// Load saved session into `state`. Returns a descriptor ({mode, setupPlacement?})
// or null. Malformed data is ignored so a corrupt entry can't wedge the app.
function restore() {
  let data;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    data = JSON.parse(raw);
  } catch (_) {
    return null;
  }
  try {
    if (!data) return null;

    if (data.mode === 'setup') {
      if (typeof data.setupPlacement !== 'string') return null;
      state.mode = 'setup';
      state.orientation = data.orientation === 'black' ? 'black' : 'white';
      state.setupColor = data.setupColor === 'black' ? 'black' : 'white';
      playSnapshot =
        data.snapshot && typeof data.snapshot.baseFen === 'string' && Array.isArray(data.snapshot.moves)
          ? { baseFen: data.snapshot.baseFen, moves: data.snapshot.moves, cursor: data.snapshot.cursor | 0 }
          : { baseFen: INITIAL_FEN, moves: [], cursor: 0 };
      return { mode: 'setup', setupPlacement: data.setupPlacement };
    }

    // play (also handles legacy entries with no `mode`)
    if (typeof data.baseFen !== 'string' || !Array.isArray(data.moves)) return null;
    const pos = positionFromFen(data.baseFen);
    for (const uci of data.moves) {
      if (typeof uci !== 'string') return null;
      pos.play(parseUci(uci));
    }
    state.mode = 'play';
    state.baseFen = data.baseFen;
    state.moves = data.moves;
    state.cursor = Math.min(Math.max(0, data.cursor | 0), data.moves.length);
    state.orientation = data.orientation === 'black' ? 'black' : 'white';
    return { mode: 'play' };
  } catch (_) {
    return null; // unparseable FEN / illegal move → fall back to defaults
  }
}

// --- position helpers ------------------------------------------------------

function positionFromFen(fen) {
  const setup = parseFen(fen).unwrap();
  return Chess.fromSetup(setup).unwrap();
}

// Replay `count` moves from the base FEN; return { pos, lastMove }.
function positionAt(count) {
  const pos = positionFromFen(state.baseFen);
  let lastMove = null;
  for (let i = 0; i < count; i++) {
    const move = parseUci(state.moves[i]);
    pos.play(move);
    lastMove = state.moves[i];
  }
  return { pos, lastMove };
}

function fenOf(pos) {
  return makeFen(pos.toSetup());
}

function lastMoveSquares(uci) {
  if (!uci) return undefined;
  return [uci.slice(0, 2), uci.slice(2, 4)];
}

// --- board sync (play mode) ------------------------------------------------

function syncBoard() {
  const { pos, lastMove } = positionAt(state.cursor);
  const fen = fenOf(pos);
  ground.set({
    fen: fen.split(' ')[0],
    turnColor: pos.turn,
    orientation: state.orientation,
    lastMove: lastMoveSquares(lastMove),
    movable: { free: false, color: pos.turn, dests: chessgroundDests(pos) },
    draggable: { enabled: true, deleteOnDropOff: false },
  });
}

// --- promotion picker ------------------------------------------------------

function isPromotion(pos, from, to) {
  const piece = pos.board.get(parseSquare(from));
  if (!piece || piece.role !== 'pawn') return false;
  const rank = to[1];
  return (piece.color === 'white' && rank === '8') ||
         (piece.color === 'black' && rank === '1');
}

function askPromotion() {
  return new Promise((resolve) => {
    const overlay = byId('promo-overlay');
    overlay.hidden = false;
    const handler = (e) => {
      const btn = e.target.closest('button[data-piece]');
      if (!btn) return;
      overlay.hidden = true;
      overlay.removeEventListener('click', handler);
      resolve(btn.dataset.piece);
    };
    overlay.addEventListener('click', handler);
  });
}

// --- server calls ----------------------------------------------------------

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (res.status === 503) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || 'Engine unavailable.');
  }
  return res.json();
}

async function refreshAnalysis() {
  setStatus('Analyzing…');
  try {
    if (state.cursor === 0) {
      const data = await postJSON('/api/analyze', { fen: state.baseFen });
      renderAnalysis(data.analysis);
    } else {
      const before = positionAt(state.cursor - 1);
      const data = await postJSON('/api/move', {
        fen: fenOf(before.pos),
        move: state.moves[state.cursor - 1],
      });
      renderAnalysis(data.legal ? data.analysis : null);
    }
    setStatus('');
  } catch (err) {
    setStatus(err.message, true);
  }
}

// --- move handling (play mode) ---------------------------------------------

async function onUserMove(orig, dest) {
  // Study mode is a read-only walkthrough — ignore any board interaction.
  if (state.mode === 'study') return;
  // trap-watch is view-only — ignore any board interaction.
  if (state.mode === 'trap-watch') return;
  // trap-practice has its own input path (onTrapMove, wired separately) — the
  // play handler must never fire there.
  if (state.mode === 'trap-practice') return;
  // In setup mode, drags just rearrange pieces — no server call, no history.
  if (state.mode === 'setup') { persist(); return; }

  const before = positionAt(state.cursor);
  let promo = '';
  if (isPromotion(before.pos, orig, dest)) {
    promo = await askPromotion();
  }
  const uci = orig + dest + promo;
  const fenBefore = fenOf(before.pos);

  setStatus('Analyzing…');
  let data;
  try {
    data = await postJSON('/api/move', { fen: fenBefore, move: uci });
  } catch (err) {
    setStatus(err.message, true);
    syncBoard();
    return;
  }

  if (!data.legal) {
    setStatus('Illegal move.', true);
    syncBoard();
    return;
  }

  state.moves = state.moves.slice(0, state.cursor);
  state.moves.push(uci);
  state.cursor += 1;

  syncBoard();
  renderAnalysis(data.analysis);
  setStatus('');
  persist();
  refreshOpeningThenTraps(); // fire-and-forget: opening then traps check, sequential
}

// --- setup mode: brush stamping --------------------------------------------
//
// chessground's `select` event doesn't reliably fire on EMPTY squares, so we
// can't use it to place pieces. Instead we own the click: a capture-phase
// pointer listener on the board maps coordinates → square and stamps/erases,
// stopping chessground from also starting a drag/selection.

function eventSquare(e) {
  const boardEl = byId('board');
  const r = boardEl.getBoundingClientRect();
  const point = e.touches && e.touches[0] ? e.touches[0] : e;
  const col = Math.floor(((point.clientX - r.left) / r.width) * 8);
  const row = Math.floor(((point.clientY - r.top) / r.height) * 8);
  if (col < 0 || col > 7 || row < 0 || row > 7) return null;
  let fileIdx, rank;
  if (state.orientation === 'white') { fileIdx = col; rank = 8 - row; }
  else { fileIdx = 7 - col; rank = 1 + row; }
  return 'abcdefgh'[fileIdx] + rank;
}

function onBoardPointerDown(e) {
  if (state.mode !== 'setup' || !brush) return; // move tool → let chessground handle
  const sq = eventSquare(e);
  if (!sq) return;
  e.preventDefault();
  e.stopPropagation();
  ground.setPieces(new Map([[sq, brush === 'erase' ? undefined : brush]]));
  persist();
}

// --- rendering -------------------------------------------------------------

function formatEval(a) {
  if (a == null) return '—';
  if (a.mate != null) {
    const sign = a.mate > 0 ? '+' : '-';
    return `M${sign}${Math.abs(a.mate)}`;
  }
  if (a.evalCp != null) {
    const pawns = a.evalCp / 100;
    return (pawns >= 0 ? '+' : '') + pawns.toFixed(2);
  }
  return '—';
}

// `opts.suppressQuality` forces the quality label to '—' regardless of the
// engine's verdict. Trap practice passes it so a real "Blunder!" on a
// deliberately dubious trapper move never contradicts the lesson.
function renderAnalysis(a, opts = {}) {
  byId('eval').textContent = formatEval(a);

  const qEl = byId('quality');
  qEl.className = 'quality';
  if (a && a.quality && !opts.suppressQuality) {
    qEl.textContent = a.quality;
    qEl.classList.add(`q-${a.quality}`);
  } else {
    qEl.textContent = '—';
  }

  byId('best-move').textContent = (a && a.bestMoveSan) || '—';
  byId('pv').textContent = (a && a.pvSan && a.pvSan.length) ? a.pvSan.join(' ') : '—';
}

function setStatus(msg, isError = false) {
  const el = byId('status');
  el.textContent = msg || '';
  el.classList.toggle('error', !!isError);
}

// --- play controls ---------------------------------------------------------

function undo() {
  if (state.mode !== 'play' || state.cursor === 0) return;
  state.cursor -= 1;
  syncBoard();
  refreshAnalysis();
  refreshOpeningThenTraps();
  persist();
}

function redo() {
  if (state.mode !== 'play' || state.cursor >= state.moves.length) return;
  state.cursor += 1;
  syncBoard();
  refreshAnalysis();
  refreshOpeningThenTraps();
  persist();
}

function flip() {
  state.orientation = state.orientation === 'white' ? 'black' : 'white';
  ground.set({ orientation: state.orientation });
  persist();
}

function reset() {
  if (state.mode !== 'play') return;
  state.baseFen = INITIAL_FEN;
  state.moves = [];
  state.cursor = 0;
  byId('fen-error').hidden = true;
  syncBoard();
  refreshAnalysis();
  refreshOpeningThenTraps();
  persist();
}

async function loadFen() {
  const input = byId('fen-input');
  const errEl = byId('fen-error');
  const fen = input.value.trim();
  if (!fen) return;

  try {
    positionFromFen(fen);
  } catch {
    errEl.textContent = 'Invalid FEN.';
    errEl.hidden = false;
    return;
  }

  let data;
  try {
    data = await postJSON('/api/load', { fen });
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
    return;
  }
  if (!data.valid) {
    errEl.textContent = data.error || 'Invalid FEN.';
    errEl.hidden = false;
    return;
  }

  errEl.hidden = true;
  state.baseFen = data.fen;
  state.moves = [];
  state.cursor = 0;
  syncBoard();
  renderAnalysis(data.analysis);
  persist();
  refreshOpeningThenTraps();
}

// --- setup mode: transitions + tools ---------------------------------------

function showSetupUI(on) {
  byId('setup-bar').hidden = !on;
  byId('setup-toggle').hidden = on;
  document.body.classList.toggle('setup-mode', on);
}

function updatePaletteActive(tool) {
  document.querySelectorAll('#palette [data-tool], #palette [data-piece]').forEach((b) => {
    const id = b.dataset.tool || b.dataset.piece;
    b.classList.toggle('active', id === tool);
  });
}

// Switch the active editing tool. `tool` is 'move', 'erase', or a piece code.
function setTool(tool) {
  if (tool === 'move') brush = null;
  else if (tool === 'erase') brush = 'erase';
  else brush = PIECE_CODES[tool] || null;

  if (brush) {
    // Stamp mode: clicks place/erase; dragging disabled so no accidental moves.
    ground.set({
      movable: { free: false, color: undefined, dests: undefined },
      draggable: { enabled: false },
      selectable: { enabled: true },
    });
  } else {
    // Move tool: free drag to rearrange; drag off-board to delete.
    ground.set({
      movable: { free: true, color: 'both', dests: undefined },
      draggable: { enabled: true, deleteOnDropOff: true },
      selectable: { enabled: true },
    });
  }
  updatePaletteActive(tool);
}

function setSide(color) {
  state.setupColor = color === 'black' ? 'black' : 'white';
  byId('side-white').classList.toggle('active', state.setupColor === 'white');
  byId('side-black').classList.toggle('active', state.setupColor === 'black');
  persist();
}

function showSetupError(msg) {
  const el = byId('setup-error');
  el.textContent = msg;
  el.hidden = false;
}
function clearSetupError() { byId('setup-error').hidden = true; }

function emptyBoard() { ground.set({ fen: EMPTY_PLACEMENT }); persist(); }
function startPosition() { ground.set({ fen: INITIAL_PLACEMENT }); persist(); }

function enterSetup() {
  // Non-destructive: snapshot the current game so Cancel can restore it.
  playSnapshot = { baseFen: state.baseFen, moves: state.moves.slice(), cursor: state.cursor };
  const { pos } = positionAt(state.cursor);
  state.mode = 'setup';
  state.setupColor = pos.turn;
  ground.set({ fen: fenOf(pos).split(' ')[0], lastMove: undefined, highlight: { lastMove: false, check: false } });
  enterSetupUI();
  persist();
}

function enterSetupUI() {
  showSetupUI(true);
  setSide(state.setupColor);
  setTool('move');
  clearSetupError();
  setStatus('Setup mode — arrange pieces, set side to move, then Begin Game.');
}

function exitSetupToPlay() {
  brush = null;
  showSetupUI(false);
  ground.set({ highlight: { lastMove: true, check: true } });
  syncBoard();        // restores play board config (legal dests, no free drag)
  refreshAnalysis();  // evaluator back on
  refreshOpeningThenTraps();
  persist();
}

// Expand a FEN rank ("4P3") to 8 chars ("....P...").
function expandRank(r) { return r.replace(/\d/g, (d) => '.'.repeat(+d)); }

// Infer castling rights from a placement: rights only where king+rook sit home.
function inferCastling(placement) {
  const ranks = placement.split('/');
  if (ranks.length !== 8) return '-';
  const r1 = expandRank(ranks[7]); // white back rank (rank 1)
  const r8 = expandRank(ranks[0]); // black back rank (rank 8)
  let c = '';
  if (r1[4] === 'K') { if (r1[7] === 'R') c += 'K'; if (r1[0] === 'R') c += 'Q'; }
  if (r8[4] === 'k') { if (r8[7] === 'r') c += 'k'; if (r8[0] === 'r') c += 'q'; }
  return c || '-';
}

function friendlyPosError(e) {
  const m = String((e && e.message) || e || '');
  if (/empty/i.test(m)) return 'The board is empty.';
  if (/king/i.test(m)) return 'Need exactly one king of each color.';
  if (/opposite|impossible.*check|other.*check/i.test(m)) return 'The side NOT to move is in check — illegal.';
  if (/pawn/i.test(m)) return 'Illegal pawn placement (e.g. a pawn on the back rank).';
  if (/check/i.test(m)) return 'Illegal position (check rule violated).';
  return 'Illegal position — a game can’t start from here.';
}

function beginGame() {
  const placement = ground.getFen();
  const fen = `${placement} ${state.setupColor === 'white' ? 'w' : 'b'} ${inferCastling(placement)} - 0 1`;

  // Validate fully client-side before committing (chessops enforces kings,
  // check rules, pawns-on-backrank, etc.). The server re-validates on analyze.
  try {
    positionFromFen(fen);
  } catch (e) {
    showSetupError(friendlyPosError(e));
    return;
  }

  state.baseFen = fen;
  state.moves = [];
  state.cursor = 0;
  state.mode = 'play';
  playSnapshot = null;
  exitSetupToPlay();
}

function cancelSetup() {
  const snap = playSnapshot || { baseFen: INITIAL_FEN, moves: [], cursor: 0 };
  state.baseFen = snap.baseFen;
  state.moves = snap.moves;
  state.cursor = snap.cursor;
  state.mode = 'play';
  playSnapshot = null;
  exitSetupToPlay();
}

// --- opening trainer: detection + candidates -------------------------------

// Fire-and-forget after any play-line change. Fully isolated: a slow/failed
// opening call never blocks or breaks move handling or the eval render.
// Returns its promise so callers (e.g. refreshOpeningThenTraps) can sequence
// the traps check AFTER this resolves — but callers need NOT await it.
async function refreshOpening() {
  if (state.mode !== 'play') return;
  try {
    const body = { baseFen: state.baseFen, moves: state.moves.slice(0, state.cursor) };
    if (openingFilter) body.q = openingFilter;
    const data = await postJSON('/api/opening', body);
    renderOpening(data);
  } catch (_) {
    // Isolated by design — opening data is non-critical.
  }
}

// Sequentially fire refreshOpening then refreshTrapsAvailable so both network
// calls don't burst simultaneously. Always fire-and-forget at the call site.
async function refreshOpeningThenTraps() {
  await refreshOpening();
  refreshTrapsAvailable(); // fire-and-forget; owns its own try/catch + token
}

function renderOpening(data) {
  const nameEl = byId('opening-name');
  if (data && data.current) {
    nameEl.textContent = `${data.current.name} (${data.current.eco})`;
  } else {
    nameEl.textContent = '—';
  }

  const list = byId('opening-candidates');
  list.replaceChildren();
  const items = (data && data.candidates) || [];
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'cand-empty';
    empty.textContent = openingFilter ? 'No openings match the filter.' : 'No named continuations from here.';
    list.appendChild(empty);
  } else {
    for (const item of items) {
      const btn = document.createElement('button');
      const nm = document.createElement('span');
      nm.textContent = item.name;            // textContent → escaped, no injection
      const eco = document.createElement('span');
      eco.className = 'cand-eco';
      eco.textContent = item.eco;
      btn.append(nm, eco);
      btn.addEventListener('click', () => onCandidateClick(item));
      list.appendChild(btn);
    }
  }

  const trunc = byId('opening-truncated');
  if (data && data.truncated) {
    trunc.textContent = 'Showing the first 40 — filter or play a move to narrow.';
    trunc.hidden = false;
  } else {
    trunc.hidden = true;
  }
}

function onCandidateClick(item) {
  if (state.mode === 'setup') return;            // inert during setup
  if (state.mode === 'play') {
    enterStudy(item);                            // snapshots the game
  } else if (state.mode === 'study') {
    loadStudyLine(item);                         // switch lines WITHOUT re-snapshot
  }
}

// --- traps: browse section -------------------------------------------------

// Fetch the trap summary list once on startup and populate the browse section.
async function loadTraps() {
  try {
    const res = await fetch('/api/traps');
    const data = await res.json();
    trapsData = (data && Array.isArray(data.traps)) ? data.traps : [];
  } catch (_) {
    trapsData = []; // degraded — empty section, no crash
  }
  renderTraps();
}

// Re-render the #traps-list applying the current name + color filters.
// All text is set via textContent (no innerHTML injection).
function renderTraps() {
  const nameFilter = byId('traps-name-filter').value.trim().toLowerCase();
  const colorFilter = byId('traps-color-filter').value; // '' | 'white' | 'black'

  const filtered = trapsData.filter((t) => {
    if (nameFilter && !t.name.toLowerCase().includes(nameFilter)) return false;
    if (colorFilter && t.color !== colorFilter) return false;
    return true;
  });

  const list = byId('traps-list');
  list.replaceChildren();

  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'traps-empty';
    empty.textContent = trapsData.length
      ? 'No traps match the filter.'
      : 'No traps loaded.';
    list.appendChild(empty);
    return;
  }

  for (const trap of filtered) {
    const btn = document.createElement('button');

    const nameSpan = document.createElement('span');
    nameSpan.className = 'trap-item-name';
    nameSpan.textContent = trap.name; // textContent — no injection

    const metaSpan = document.createElement('span');
    metaSpan.className = 'trap-item-meta';

    const ecoSpan = document.createElement('span');
    ecoSpan.className = 'trap-item-eco';
    ecoSpan.textContent = trap.eco || '';

    const colorSpan = document.createElement('span');
    colorSpan.className = 'trap-item-color';
    colorSpan.textContent = trap.color || '';

    // Commonness as filled stars (1-5)
    const commSpan = document.createElement('span');
    commSpan.className = 'trap-item-commonness';
    const stars = Math.max(0, Math.min(5, trap.commonness | 0));
    commSpan.textContent = '★'.repeat(stars) + '☆'.repeat(5 - stars);

    metaSpan.append(ecoSpan, colorSpan, commSpan);
    btn.append(nameSpan, metaSpan);
    btn.addEventListener('click', () => enterTrap(trap.id));
    list.appendChild(btn);
  }
}

// --- traps: live "trap available" chip (play mode only) --------------------

// Fire-and-forget: POST /api/traps/check with the current position.
// Guarded by trapsCheckToken so stale responses never render.
// Only fires when state.mode === 'play'.
// Must be called AFTER refreshOpening() (use refreshOpeningThenTraps).
async function refreshTrapsAvailable() {
  if (state.mode !== 'play') return;
  const token = ++trapsCheckToken;
  try {
    const moves = state.moves.slice(0, state.cursor);
    const data = await postJSON('/api/traps/check', {
      baseFen: state.baseFen,
      moves,
    });
    if (token !== trapsCheckToken) return; // stale — superseded by a newer call
    if (state.mode !== 'play') return;      // mode changed while awaiting

    const available = (data && Array.isArray(data.available)) ? data.available : [];
    if (available.length) {
      const trap = available[0];
      // Build a position key for the sticky-dismiss check.
      const posKey = state.baseFen + '|' + moves.join(',');
      if (posKey === trapChipDismissedFen) return; // still dismissed for this position
      byId('trap-chip-text').textContent = `Trap available: ${trap.name} — drill it`;
      byId('trap-chip').dataset.trapId = trap.id;
      byId('trap-chip').hidden = false;
    } else {
      byId('trap-chip').hidden = true;
    }
  } catch (_) {
    // Isolated by design — a failed check never delays or breaks anything.
  }
}

// --- trap-watch mode -------------------------------------------------------
//
// Mirrors the study walkthrough pattern: snapshot → fetch → set state →
// view-only board → lazy per-step eval (guarded by studyEvalToken) → restore.

// Build the trap step model from a full trap object (first variation).
// Returns { id, name, mainLine, startFen, step, fens, lastUcis }.
// fens[0] = startFen; fens[k] = FEN after replaying the first k UCIs from startFen.
// lastUcis[k] = UCI of the move that led into fens[k] (undefined for k=0).
function buildTrap(trapData) {
  const variation = trapData.variations[0];
  const mainLine = variation.mainLine;
  const startFen = trapData.startFen;

  const fens = [startFen];
  const lastUcis = [undefined];

  let pos = positionFromFen(startFen);
  for (const ply of mainLine) {
    pos.play(parseUci(ply.uci));
    fens.push(makeFen(pos.toSetup()));
    lastUcis.push(ply.uci);
  }

  return {
    id: trapData.id,
    name: trapData.name,
    mainLine,
    startFen,
    step: 0,
    max: mainLine.length,
    fens,
    lastUcis,
  };
}

// Entry point: called from trap list items and the live chip.
// From play mode: snapshot current game into studySnapshot (non-destructive).
// Re-entrant: if already in a trap mode, switch traps WITHOUT re-snapshotting.
async function enterTrap(trapId) {
  // Snapshot only if coming from play mode (not already in a trap).
  if (state.mode === 'play') {
    studySnapshot = { baseFen: state.baseFen, moves: state.moves.slice(), cursor: state.cursor };
  }

  // Fetch the full trap.
  let trapData;
  try {
    const res = await fetch(`/api/traps/${encodeURIComponent(trapId)}`);
    if (!res.ok) {
      setStatus(`Trap not found: ${trapId}`, true);
      return;
    }
    trapData = await res.json();
  } catch (err) {
    setStatus('Failed to load trap data.', true);
    return;
  }

  trap = buildTrap(trapData);
  // Carry trap-aware context for practice-mode eval/notes.
  trap.color = trapData.color;            // the TRAPPER (the side the user plays)
  trap.engineNote = trapData.engineNote || '';
  state.mode = 'trap-watch';

  // Show trap bar, hide play controls (same body-class pattern as study-mode).
  showTrapUI(true);

  byId('trap-title').textContent = trap.name;
  byId('trap-mode-toggle').hidden = false; // watch ⇄ practice toggle available
  applyTrapModeUI();                       // show watch controls, hide practice ones

  goToTrapStep(0);
}

// Toggle the visible body class + controls for the active trap sub-mode.
// Watch: stepper visible; practice: reveal + show-refutation + feedback visible.
function applyTrapModeUI() {
  const practice = state.mode === 'trap-practice';
  document.body.classList.toggle('trap-watch-mode', state.mode === 'trap-watch');
  document.body.classList.toggle('trap-practice-mode', practice);

  byId('trap-mode-toggle').textContent = practice
    ? 'Switch to Watch'
    : 'Switch to Practice';

  byId('trap-stepper').hidden = practice;       // stepper is watch-only
  byId('trap-reveal').hidden = !practice;       // reveal/show-refutation are practice-only
  byId('trap-feedback').hidden = !practice;
}

// Flip between watch and practice. ALWAYS restarts the drill at step 0
// (startFen) — never mid-line — so the played/unplayed boundary is unambiguous.
// Reuses the existing studySnapshot (does NOT re-snapshot the game).
function toggleTrapMode() {
  if (!trap) return;
  if (state.mode === 'trap-practice') {
    state.mode = 'trap-watch';
    applyTrapModeUI();
    byId('trap-feedback').textContent = '';
    goToTrapStep(0);
  } else if (state.mode === 'trap-watch') {
    state.mode = 'trap-practice';
    applyTrapModeUI();
    startPractice();
  }
}

// Navigate to step k in the trap variation.
// step 0 = startFen (no note); step k = after k plies.
function goToTrapStep(k) {
  if (!trap) return;
  trap.step = Math.max(0, Math.min(k, trap.max));

  const fen = trap.fens[trap.step];
  const pos = positionFromFen(fen);
  ground.set({
    fen: fen.split(' ')[0],
    turnColor: pos.turn,
    orientation: state.orientation,
    lastMove: trap.step > 0 ? lastMoveSquares(trap.lastUcis[trap.step]) : undefined,
    movable: { free: false, color: undefined, dests: undefined },
    draggable: { enabled: false },
  });

  renderTrapStep();
}

// Render move label, eval, and note for the current trap step.
// Uses studyEvalToken (shared with study) so switching between study and trap
// modes properly cancels any in-flight requests.
function renderTrapStep() {
  const step = trap.step;

  // Move label: step 0 = start position, step k = SAN of ply k-1.
  if (step === 0) {
    byId('trap-move').textContent = 'Start position';
  } else {
    const ply = trap.mainLine[step - 1];
    // Compute a rough move-number string (startFen gives us the turn).
    const startPos = positionFromFen(trap.startFen);
    const startFullMove = startPos.fullmoves;
    const startTurn = startPos.turn; // 'white' | 'black'
    // Move number at ply index i (0-based from startFen):
    //   i=0 is the first ply from startFen (startTurn's move).
    const plyIndex = step - 1; // 0-based ply index into mainLine
    let fullMove, colorLabel;
    if (startTurn === 'white') {
      fullMove = startFullMove + Math.floor(plyIndex / 2);
      colorLabel = plyIndex % 2 === 0 ? 'w' : 'b';
    } else {
      // Black moves first from startFen
      fullMove = startFullMove + Math.floor((plyIndex + 1) / 2);
      colorLabel = plyIndex % 2 === 0 ? 'b' : 'w';
    }
    byId('trap-move').textContent = `${fullMove}${colorLabel === 'w' ? '.' : '…'} ${ply.san}`;
  }

  // Lazy eval via /api/analyze — cancel stale requests with studyEvalToken.
  const token = ++studyEvalToken;
  const fen = trap.fens[step];

  postJSON('/api/analyze', { fen })
    .then((d) => {
      if (token !== studyEvalToken) return; // stale — superseded
      const evalStr = formatEval(d && d.analysis);
      // Append eval to the move label.
      const cur = byId('trap-move').textContent;
      byId('trap-move').textContent = cur ? `${cur}  ${evalStr}` : evalStr;
    })
    .catch(() => { /* eval is best-effort */ });

  // Note: step 0 has no ply note; ply k-1 (0-based) has the note for step k.
  const noteEl = byId('trap-note');
  if (step === 0) {
    noteEl.textContent = 'Trap starting position.';
    return;
  }

  const ply = trap.mainLine[step - 1];
  const parts = [];

  if (ply.note) {
    parts.push(ply.note);
  }

  // Bait ply: also surface refutation.note + assessment.
  if (ply.bait && ply.refutation) {
    const ref = ply.refutation;
    if (ref.note || ref.assessment) {
      parts.push(''); // blank line separator
      if (ref.note) parts.push(`If declined: ${ref.note}`);
      if (ref.assessment) parts.push(`Assessment: ${ref.assessment}`);
    }
  }

  noteEl.textContent = parts.join('\n') || '';
}

// Show/hide the trap bar and toggle body class for play-control hiding.
// The specific watch/practice body class is owned by applyTrapModeUI(); here we
// only ensure both are cleared when the bar is hidden (on exit).
function showTrapUI(on) {
  byId('trap-bar').hidden = !on;
  if (!on) {
    document.body.classList.remove('trap-watch-mode', 'trap-practice-mode');
  }
}

// Return to my game: restore studySnapshot, mode → play.
// Reused by #trap-return for BOTH trap-watch and trap-practice.
function exitTrap() {
  const snap = studySnapshot || { baseFen: INITIAL_FEN, moves: [], cursor: 0 };
  state.baseFen = snap.baseFen;
  state.moves = snap.moves;
  state.cursor = snap.cursor;
  state.mode = 'play';
  studySnapshot = null;
  trap = null;
  showTrapUI(false);
  ground.set({ highlight: { lastMove: true, check: true } });
  syncBoard();
  refreshAnalysis();
  refreshOpeningThenTraps();
  persist();
}

// --- trap-practice mode ----------------------------------------------------
//
// Interactive drill. The trap's `color` is the TRAPPER (the side the user
// plays). In `mainLine`, victim moves are auto-played by the app; the user must
// find each trapper move. Validation is against the SCRIPT (mainLine[ply].uci) —
// any legal-but-wrong move snaps back with "try again".
//
// trap.step is the count of plies already applied (mirrors trap-watch). The
// board FEN at step k is trap.fens[k]; the move to find at step k (when it's a
// trapper ply) is trap.mainLine[k].uci.

// Begin (or restart) the drill from step 0.
function startPractice() {
  if (!trap) return;
  trap.step = 0;
  byId('trap-move').textContent = 'Practice — find the trapper’s moves.';
  byId('trap-note').textContent = '';
  byId('trap-feedback').textContent = '';
  byId('trap-feedback').classList.remove('feedback-good', 'feedback-bad');
  // Place the board at startFen, then run the loop (auto-plays any leading
  // victim move — e.g. Stafford's 4.Nxc6 — before handing control to the user).
  renderPracticeBoard();
  advancePractice();
}

// Drive the practice loop from the current step:
//   - line complete (step === max) → completion message, board frozen.
//   - victim ply → auto-play after a short beat, advance, recurse.
//   - trapper ply → make the board interactive and WAIT for onTrapMove.
function advancePractice() {
  if (!trap || state.mode !== 'trap-practice') return;

  if (trap.step >= trap.max) {
    setTrapFrozen();
    showPracticeComplete();
    return;
  }

  const ply = trap.mainLine[trap.step];
  if (ply.side === 'victim') {
    // Freeze the board, then auto-play the victim's scripted reply after a beat
    // so the user sees the trap spring (incl. the bait move).
    setTrapFrozen();
    const stepAtSchedule = trap.step;
    setTimeout(() => {
      // Guard: the user may have toggled/exited while we waited.
      if (!trap || state.mode !== 'trap-practice') return;
      if (trap.step !== stepAtSchedule) return;
      applyPracticeStep(ply.uci);
      trap.step += 1;
      advancePractice();
    }, 400);
  } else {
    // Trapper's turn — let the user move; onTrapMove validates against the script.
    setTrapInteractive();
    renderPracticeNote(); // show the note/eval for the position the user faces
  }
}

// Render the board at the current step's FEN (no interactivity changes here).
function renderPracticeBoard() {
  const fen = trap.fens[trap.step];
  const pos = positionFromFen(fen);
  ground.set({
    fen: fen.split(' ')[0],
    turnColor: pos.turn,
    orientation: state.orientation,
    lastMove: trap.step > 0 ? lastMoveSquares(trap.lastUcis[trap.step]) : undefined,
    movable: { free: false, color: undefined, dests: undefined },
    draggable: { enabled: false },
  });
}

// Apply a single scripted UCI on the board (used to auto-play victim moves and
// to commit a correct/revealed trapper move). Animates + highlights last move.
function applyPracticeStep(uci) {
  const fen = trap.fens[trap.step + 1]; // FEN after this ply
  const pos = positionFromFen(fen);
  ground.set({
    fen: fen.split(' ')[0],
    turnColor: pos.turn,
    orientation: state.orientation,
    lastMove: lastMoveSquares(uci),
    movable: { free: false, color: undefined, dests: undefined },
    draggable: { enabled: false },
  });
}

// Configure the board so ONLY the trapper can move, with ALL their legal moves
// as dests (same as play mode, restricted to trap.color). Validation of the
// CHOSEN move happens in onTrapMove against the script.
function setTrapInteractive() {
  const fen = trap.fens[trap.step];
  const pos = positionFromFen(fen);
  ground.set({
    fen: fen.split(' ')[0],
    turnColor: pos.turn,
    orientation: state.orientation,
    lastMove: trap.step > 0 ? lastMoveSquares(trap.lastUcis[trap.step]) : undefined,
    movable: { free: false, color: trap.color, dests: chessgroundDests(pos) },
    draggable: { enabled: true, deleteOnDropOff: false },
  });
}

// Freeze the board (no interaction) — used while a victim move is pending and
// when the line is complete.
function setTrapFrozen() {
  ground.set({ movable: { free: false, color: undefined, dests: undefined }, draggable: { enabled: false } });
}

function setTrapFeedback(msg, kind) {
  const el = byId('trap-feedback');
  el.textContent = msg;
  el.classList.remove('feedback-good', 'feedback-bad');
  if (kind === 'good') el.classList.add('feedback-good');
  else if (kind === 'bad') el.classList.add('feedback-bad');
}

function showPracticeComplete() {
  // The result (if any) sits on the final ply.
  const last = trap.mainLine[trap.max - 1];
  const result = last && last.result;
  const labels = {
    checkmate: 'Checkmate — the trap is complete!',
    'wins-queen': 'You win the queen — trap complete!',
    'wins-material': 'You win decisive material — trap complete!',
    'wins-piece': 'You win a piece — trap complete!',
  };
  const msg = (result && labels[result]) || 'Line complete — well done!';
  setTrapFeedback(msg, 'good');
  // Keep the final note + eval on screen.
  renderPracticeNote();
}

// onTrapMove(orig, dest) — the ONLY board-move handler in trap-practice.
// Builds the attempted UCI (with a promotion suffix via askPromotion when the
// move is a promotion), validates it against the scripted trapper move, then:
//   correct → apply + positive feedback + advance + run the loop (auto-victim).
//   wrong   → snap back via ground.set({fen: currentFen}) + "try again".
async function onTrapMove(orig, dest) {
  if (state.mode !== 'trap-practice' || !trap) return;
  const ply = trap.mainLine[trap.step];
  // Defensive: only trapper plies are interactive, but guard anyway.
  if (!ply || ply.side !== 'trapper') { renderPracticeBoard(); return; }

  const fenBefore = trap.fens[trap.step];
  const posBefore = positionFromFen(fenBefore);

  let promo = '';
  if (isPromotion(posBefore, orig, dest)) {
    promo = await askPromotion();
  }
  const attempted = orig + dest + promo;

  if (attempted === ply.uci) {
    // Correct — commit the scripted move, advance, continue (auto-victim next).
    applyPracticeStep(ply.uci);
    trap.step += 1;
    setTrapFeedback(ply.note ? `Yes! ${ply.note}` : 'Yes — that’s the move!', 'good');
    renderPracticeNote();
    advancePractice();
  } else {
    // Legal but not the scripted move (incl. wrong promotion piece) → snap the
    // piece back. setTrapInteractive() re-sets the board to fenBefore and
    // re-arms the trapper's dests (same takeback effect as play mode's
    // ground.set({fen: currentFen}) on a rejected move).
    setTrapInteractive();
    setTrapFeedback('Not quite — try again.', 'bad');
  }
}

// Reveal: play the next expected trapper move for a stuck user, then continue
// the loop (which auto-plays the following victim move).
function revealTrapMove() {
  if (state.mode !== 'trap-practice' || !trap) return;
  if (trap.step >= trap.max) return;
  const ply = trap.mainLine[trap.step];
  if (ply.side !== 'trapper') return; // only meaningful on the user's turn
  applyPracticeStep(ply.uci);
  trap.step += 1;
  setTrapFeedback(ply.note ? `${ply.san} — ${ply.note}` : `The move was ${ply.san}.`, 'good');
  renderPracticeNote();
  advancePractice();
}

// Show-refutation: read-only preview of what the victim SHOULD have played
// (from the bait ply's refutation: san + declineSan line + assessment).
// Does NOT change the drill position or step — purely fills the note/feedback.
function showTrapRefutation() {
  if (!trap) return;
  const baitPly = trap.mainLine.find((p) => p.bait && p.refutation);
  if (!baitPly) {
    setTrapFeedback('No refutation recorded for this trap.', null);
    return;
  }
  const ref = baitPly.refutation;
  const parts = [];
  parts.push(`Refutation: ${ref.san}`);
  if (ref.note) parts.push(ref.note);
  if (Array.isArray(ref.declineSan) && ref.declineSan.length) {
    parts.push(`Line: ${ref.san} ${ref.declineSan.join(' ')}`);
  }
  if (ref.assessment) parts.push(`Assessment: ${ref.assessment}`);
  byId('trap-note').textContent = parts.join('\n');
  setTrapFeedback(`If the opponent declines: ${ref.san}`, null);
}

// Render the move label (with trap-aware eval) + note for the CURRENT practice
// position. Eval is fetched lazily and the quality label is ALWAYS suppressed.
function renderPracticeNote() {
  const step = trap.step;

  // The note/eval describe the position AS IT NOW STANDS. If the last applied
  // ply (step-1) carried a note, show it; on the user's turn (no ply applied
  // since the prompt) we still show the prior ply's note for context.
  const lastPly = step > 0 ? trap.mainLine[step - 1] : null;
  const noteParts = [];
  if (lastPly && lastPly.note) noteParts.push(lastPly.note);
  // On a dubious trapper ply, also surface the trap's engineNote (why the
  // engine-poor move scores in practice).
  if (lastPly && lastPly.dubious && trap.engineNote) {
    noteParts.push(`Engine: ${trap.engineNote}`);
  }
  byId('trap-note').textContent = noteParts.join('\n');

  // Move label.
  byId('trap-move').textContent = step > 0
    ? `${lastPly.san}`
    : 'Your move — find the trapper’s idea.';

  // Lazy, trap-aware eval. Use /api/move when we have a prior ply (gives the
  // move-relative eval), else /api/analyze on the position. Either way the
  // quality label is suppressed in the panel.
  const token = ++studyEvalToken;
  const fen = trap.fens[step];

  if (step > 0) {
    const before = trap.fens[step - 1];
    postJSON('/api/move', { fen: before, move: trap.lastUcis[step] })
      .then((d) => {
        if (token !== studyEvalToken) return;
        if (state.mode !== 'trap-practice') return;
        renderAnalysis(d && d.legal ? d.analysis : null, { suppressQuality: true });
      })
      .catch(() => { /* eval is best-effort */ });
  } else {
    postJSON('/api/analyze', { fen })
      .then((d) => {
        if (token !== studyEvalToken) return;
        if (state.mode !== 'trap-practice') return;
        renderAnalysis(d && d.analysis, { suppressQuality: true });
      })
      .catch(() => { /* eval is best-effort */ });
  }
}

// --- study walkthrough -----------------------------------------------------

function buildStudy(item) {
  const uci = item.uci.split(/\s+/).filter(Boolean);
  const pos = positionFromFen(INITIAL_FEN);
  const fens = [fenOf(pos)];                      // step 0 = initial position
  for (const u of uci) { pos.play(parseUci(u)); fens.push(fenOf(pos)); }
  return { name: item.name, eco: item.eco, san: item.san, uci, fens, step: 0, max: uci.length };
}

function enterStudy(item) {
  if (state.mode !== 'play') return;
  studySnapshot = { baseFen: state.baseFen, moves: state.moves.slice(), cursor: state.cursor };
  state.mode = 'study';
  showStudyUI(true);
  loadStudyLine(item);
}

function loadStudyLine(item) {
  study = buildStudy(item);
  byId('study-title').textContent = `Studying: ${item.name} (${item.eco})`;
  goToStep(0);
}

function goToStep(k) {
  if (!study) return;
  study.step = Math.max(0, Math.min(k, study.max));
  const fen = study.fens[study.step];
  const pos = positionFromFen(fen);
  ground.set({
    fen: fen.split(' ')[0],
    turnColor: pos.turn,
    orientation: state.orientation,
    lastMove: study.step > 0 ? lastMoveSquares(study.uci[study.step - 1]) : undefined,
    movable: { free: false, color: undefined, dests: undefined },
    draggable: { enabled: false },
  });
  renderStudyStep();
}

function renderStudyStep() {
  byId('study-step').textContent = `${study.step} / ${study.max}`;
  byId('study-move-san').textContent = study.step > 0 ? study.san[study.step - 1] : 'Start position';
  byId('study-eval').textContent = '';
  const commEl = byId('study-commentary');

  const token = ++studyEvalToken;
  const fen = study.fens[study.step];

  // Lazy eval for the displayed step only.
  postJSON('/api/analyze', { fen })
    .then((d) => { if (token === studyEvalToken) byId('study-eval').textContent = formatEval(d.analysis); })
    .catch(() => { /* eval is best-effort */ });

  // Commentary: step 0 has none (commentary is keyed by position-after-a-move).
  if (study.step === 0) {
    commEl.textContent = 'The starting position of this line.';
    return;
  }
  commEl.textContent = '…';
  postJSON('/api/opening/commentary', { fen })
    .then((c) => {
      if (token !== studyEvalToken) return;
      commEl.textContent = (c && c.text) ? c.text : 'No commentary yet for this move.';
    })
    .catch(() => { if (token === studyEvalToken) commEl.textContent = 'No commentary yet for this move.'; });
}

function exitStudy() {
  const snap = studySnapshot || { baseFen: INITIAL_FEN, moves: [], cursor: 0 };
  state.baseFen = snap.baseFen;
  state.moves = snap.moves;
  state.cursor = snap.cursor;
  state.mode = 'play';
  studySnapshot = null;
  study = null;
  showStudyUI(false);
  ground.set({ highlight: { lastMove: true, check: true } });
  syncBoard();
  refreshAnalysis();
  refreshOpeningThenTraps();
  persist();
}

function showStudyUI(on) {
  byId('study-bar').hidden = !on;
  document.body.classList.toggle('study-mode', on);
}

// --- init ------------------------------------------------------------------

function init() {
  const restored = restore();

  const initialFen = state.mode === 'setup'
    ? ((restored && restored.setupPlacement) || EMPTY_PLACEMENT)
    : fenOf(positionAt(state.cursor).pos).split(' ')[0];

  ground = Chessground(byId('board'), {
    fen: initialFen,
    orientation: state.orientation,
    movable: {
      free: false,
      color: 'white',
      dests: undefined,
      showDests: true,
      events: {
        after: (orig, dest) => {
          // trap-practice has its own validated move path; everything else
          // (play/setup) goes through onUserMove (study/trap-watch early-return).
          if (state.mode === 'trap-practice') onTrapMove(orig, dest);
          else onUserMove(orig, dest);
        },
      },
    },
    draggable: { deleteOnDropOff: true },
    highlight: { lastMove: true, check: true },
    animation: { enabled: true, duration: 150 },
    events: {
      change: () => { if (state.mode === 'setup') persist(); },
    },
  });

  // Own board clicks for setup stamping (capture phase, before chessground).
  const boardEl = byId('board');
  boardEl.addEventListener('mousedown', onBoardPointerDown, true);
  boardEl.addEventListener('touchstart', onBoardPointerDown, { capture: true, passive: false });

  // Play controls
  byId('undo').addEventListener('click', undo);
  byId('redo').addEventListener('click', redo);
  byId('flip').addEventListener('click', flip);
  byId('reset').addEventListener('click', reset);
  byId('load-fen').addEventListener('click', loadFen);
  byId('fen-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadFen(); });

  // Setup controls
  byId('setup-toggle').addEventListener('click', enterSetup);
  byId('begin-game').addEventListener('click', beginGame);
  byId('cancel-setup').addEventListener('click', cancelSetup);
  byId('empty-board').addEventListener('click', emptyBoard);
  byId('start-pos').addEventListener('click', startPosition);
  byId('side-white').addEventListener('click', () => setSide('white'));
  byId('side-black').addEventListener('click', () => setSide('black'));
  document.querySelectorAll('#palette [data-tool], #palette [data-piece]').forEach((b) => {
    b.addEventListener('click', () => setTool(b.dataset.tool || b.dataset.piece));
  });

  // Opening filter + study controls
  byId('opening-filter').addEventListener('input', (e) => {
    openingFilter = e.target.value.trim();
    refreshOpening();
  });
  byId('study-return').addEventListener('click', exitStudy);
  byId('study-first').addEventListener('click', () => goToStep(0));
  byId('study-prev').addEventListener('click', () => goToStep(study ? study.step - 1 : 0));
  byId('study-next').addEventListener('click', () => goToStep(study ? study.step + 1 : 0));
  byId('study-last').addEventListener('click', () => goToStep(study ? study.max : 0));

  // Traps browse filters
  byId('traps-name-filter').addEventListener('input', () => renderTraps());
  byId('traps-color-filter').addEventListener('change', () => renderTraps());

  // Trap chip: dismiss (sticky for this position) + drill
  byId('trap-chip-dismiss').addEventListener('click', () => {
    const moves = state.moves.slice(0, state.cursor);
    trapChipDismissedFen = state.baseFen + '|' + moves.join(',');
    byId('trap-chip').hidden = true;
  });
  byId('trap-chip-drill').addEventListener('click', () => {
    const trapId = byId('trap-chip').dataset.trapId;
    if (trapId) enterTrap(trapId);
  });

  // Trap bar: return + stepper (trap-watch mode).
  byId('trap-return').addEventListener('click', exitTrap);
  byId('trap-first').addEventListener('click', () => goToTrapStep(0));
  byId('trap-prev').addEventListener('click', () => goToTrapStep(trap ? trap.step - 1 : 0));
  byId('trap-next').addEventListener('click', () => goToTrapStep(trap ? trap.step + 1 : 0));
  byId('trap-last').addEventListener('click', () => goToTrapStep(trap ? trap.max : 0));

  // Trap bar: practice-mode controls (TT6).
  byId('trap-mode-toggle').addEventListener('click', toggleTrapMode);
  byId('trap-reveal-move').addEventListener('click', revealTrapMove);
  byId('trap-show-refutation').addEventListener('click', showTrapRefutation);

  if (state.mode === 'setup') {
    enterSetupUI();           // resume an in-progress setup session
  } else {
    syncBoard();
    refreshAnalysis();
    refreshOpeningThenTraps();
  }

  // Load traps browse data (non-blocking — section degrades gracefully if absent).
  loadTraps();
}

init();
