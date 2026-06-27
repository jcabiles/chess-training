// Stockfish Analysis Board — frontend.
//
// chessground = board rendering (no chess rules).
// chessops    = chess rules: legal-move generation (dests), FEN, UCI parsing.
// The Python server is the authority on move legality + analysis; chessops here
// gives instant legal-dest highlighting / snap-back without a round-trip.
//
// Two modes (finite state machine — state.mode):
//   'play'  — one legal move at a time, evaluator on (the default).
//   'setup' — free piece placement / palette editor, evaluator paused. On
//             "Begin Game" the position is validated + committed as the new
//             starting position (reusing the /api/load path).
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
  mode: 'play',     // 'play' | 'setup'
  baseFen: INITIAL_FEN,
  moves: [],        // UCI strings applied from baseFen
  cursor: 0,        // how many of `moves` are currently applied
  orientation: 'white',
  setupColor: 'white', // side-to-move while in setup mode
};

let ground = null;
let playSnapshot = null;   // saved play state captured when entering setup (for Cancel)
let brush = null;          // setup tool: null = move/drag, 'erase', or a piece object
let studySnapshot = null;  // saved play state captured when entering study (separate!)
let study = null;          // active study walkthrough { name, eco, san, uci, fens, step, max }
let openingFilter = '';    // current candidate filter text
let studyEvalToken = 0;    // guards out-of-order async eval/commentary while stepping

const byId = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Session persistence (localStorage). Mode-aware: in setup we save the working
// placement + the snapshot so a refresh keeps an in-progress setup AND its
// Cancel target. Legacy {baseFen,moves,...} entries (no `mode`) load as play.
// ---------------------------------------------------------------------------
const STORAGE_KEY = 'chess-training:session:v1';

function persist() {
  if (state.mode === 'study') return; // study is transient — never persisted
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
  refreshOpening();
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

function renderAnalysis(a) {
  byId('eval').textContent = formatEval(a);

  const qEl = byId('quality');
  qEl.className = 'quality';
  if (a && a.quality) {
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
  refreshOpening();
  persist();
}

function redo() {
  if (state.mode !== 'play' || state.cursor >= state.moves.length) return;
  state.cursor += 1;
  syncBoard();
  refreshAnalysis();
  refreshOpening();
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
  refreshOpening();
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
  refreshOpening();
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
  refreshOpening();
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
  refreshOpening();
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
      events: { after: (orig, dest) => { onUserMove(orig, dest); } },
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

  if (state.mode === 'setup') {
    enterSetupUI();           // resume an in-progress setup session
  } else {
    syncBoard();
    refreshAnalysis();
    refreshOpening();
  }
}

init();
