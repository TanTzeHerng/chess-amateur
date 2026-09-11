"use strict";

// Chess Amateur frontend. Talks to the FEAT-001 JSON API and renders an
// interactive board using Unicode chess glyphs (works fully offline).

const BOT_NAME = "Chess Amateur";

// Unicode glyphs keyed by python-chess/FEN piece letters.
const GLYPHS = {
  K: "\u2654", Q: "\u2655", R: "\u2656", B: "\u2657", N: "\u2658", P: "\u2659",
  k: "\u265A", q: "\u265B", r: "\u265C", b: "\u265D", n: "\u265E", p: "\u265F",
};
const PROMO_GLYPHS = { q: "\u2655", r: "\u2656", b: "\u2657", n: "\u2658" };

// --- DOM refs ---
const boardEl = document.getElementById("board");
const turnEl = document.getElementById("turnIndicator");
const messageEl = document.getElementById("message");
const moveLogEl = document.getElementById("moveLog");
const bannerEl = document.getElementById("banner");
const thinkingEl = document.getElementById("thinking");
const newGameBtn = document.getElementById("newGame");
const promoOverlay = document.getElementById("promoOverlay");
const promoChoices = document.getElementById("promoChoices");
const threadsInput = document.getElementById("threads");
const threadsBadge = document.getElementById("threadsBadge");

// --- game state (client) ---
let state = null;        // last API state snapshot
let humanColor = "white";
let selected = null;     // currently selected square (e.g. "e2")
let legalFrom = {};      // map: fromSquare -> [toSquare, ...] from legal_moves
let busy = false;        // guard against double-submits / mid-request clicks

// --------------------------------------------------------------------------
// FEN parsing
// --------------------------------------------------------------------------

// Returns an object mapping square name ("a1".."h8") -> piece letter.
function parseFen(fen) {
  const pieces = {};
  const placement = fen.split(" ")[0];
  const rows = placement.split("/"); // rows[0] = rank 8
  for (let r = 0; r < 8; r++) {
    const rank = 8 - r;
    let file = 0;
    for (const ch of rows[r]) {
      if (/\d/.test(ch)) {
        file += parseInt(ch, 10);
      } else {
        const fileChar = String.fromCharCode(97 + file); // 'a'
        pieces[fileChar + rank] = ch;
        file += 1;
      }
    }
  }
  return pieces;
}

// --------------------------------------------------------------------------
// Rendering
// --------------------------------------------------------------------------

function squareName(file, rank) {
  return String.fromCharCode(97 + file) + rank;
}

function isLightSquare(file, rank) {
  return (file + rank) % 2 === 0; // a1 (0,1) dark; standard coloring
}

function renderBoard() {
  boardEl.innerHTML = "";
  const pieces = state ? parseFen(state.fen) : {};

  // Orientation: white at bottom unless human plays black.
  const flipped = humanColor === "black";
  const files = [0, 1, 2, 3, 4, 5, 6, 7];
  const ranks = [8, 7, 6, 5, 4, 3, 2, 1];
  const fileOrder = flipped ? [...files].reverse() : files;
  const rankOrder = flipped ? [...ranks].reverse() : ranks;

  const lastMove = state && state.last_bot_move ? state.last_bot_move.uci : null;
  const lastFrom = lastMove ? lastMove.slice(0, 2) : null;
  const lastTo = lastMove ? lastMove.slice(2, 4) : null;

  for (const rank of rankOrder) {
    for (const file of fileOrder) {
      const name = squareName(file, rank);
      const sq = document.createElement("div");
      sq.className = "square " + (isLightSquare(file, rank) ? "light" : "dark");
      sq.dataset.square = name;

      if (name === selected) sq.classList.add("selected");
      if (name === lastFrom || name === lastTo) sq.classList.add("last-move");

      // legal destination highlight from currently selected piece
      if (selected && legalFrom[selected] && legalFrom[selected].includes(name)) {
        sq.classList.add("legal");
        if (pieces[name]) sq.classList.add("occupied");
      }

      // edge coordinates
      if (file === fileOrder[0]) {
        const c = document.createElement("span");
        c.className = "coord rank";
        c.textContent = rank;
        sq.appendChild(c);
      }
      if (rank === rankOrder[rankOrder.length - 1]) {
        const c = document.createElement("span");
        c.className = "coord file";
        c.textContent = String.fromCharCode(97 + file);
        sq.appendChild(c);
      }

      const p = pieces[name];
      if (p) {
        const span = document.createElement("span");
        span.className = "piece " + (p === p.toUpperCase() ? "white" : "black");
        span.textContent = GLYPHS[p];
        sq.appendChild(span);
      }

      sq.addEventListener("click", () => onSquareClick(name));
      boardEl.appendChild(sq);
    }
  }
}

function rebuildLegalMap() {
  legalFrom = {};
  if (!state || !state.legal_moves) return;
  for (const uci of state.legal_moves) {
    const from = uci.slice(0, 2);
    const to = uci.slice(2, 4);
    (legalFrom[from] = legalFrom[from] || []).push(to);
  }
}

function renderMoveLog() {
  moveLogEl.innerHTML = "";
  const hist = state ? state.san_history : [];
  for (let i = 0; i < hist.length; i += 2) {
    const li = document.createElement("li");
    li.value = i / 2 + 1;
    const white = document.createElement("span");
    white.className = "white-move";
    white.textContent = hist[i] || "";
    li.appendChild(white);
    if (hist[i + 1] !== undefined) {
      const black = document.createElement("span");
      black.className = "black-move";
      black.textContent = " " + hist[i + 1];
      li.appendChild(black);
    }
    moveLogEl.appendChild(li);
  }
  moveLogEl.scrollTop = moveLogEl.scrollHeight;
}

function renderTurn() {
  if (!state) { turnEl.textContent = ""; return; }
  if (state.game_over) {
    turnEl.textContent = "Game over";
    return;
  }
  const humansTurn = state.turn === humanColor;
  if (humansTurn) {
    turnEl.textContent = "Your move (" + humanColor + ")";
  } else {
    turnEl.textContent = BOT_NAME + " to move";
  }
}

function renderBanner() {
  if (!state || !state.game_over) {
    bannerEl.hidden = true;
    bannerEl.className = "banner";
    return;
  }
  bannerEl.hidden = false;
  const reason = state.result_reason || "game over";
  const result = state.result; // "1-0" / "0-1" / "1/2-1/2"
  let text;
  let cls = "banner";

  if (result === "1/2-1/2") {
    text = "Draw by " + reason + ".";
    cls += " draw";
  } else {
    const humanIsWhite = humanColor === "white";
    const whiteWon = result === "1-0";
    const humanWon = (whiteWon && humanIsWhite) || (!whiteWon && !humanIsWhite);
    if (humanWon) {
      text = "You beat " + BOT_NAME + " by " + reason + "!";
      cls += " win";
    } else {
      text = BOT_NAME + " wins by " + reason + ".";
      cls += " loss";
    }
  }
  bannerEl.textContent = text;
  bannerEl.className = cls;
}

function renderThreadsBadge() {
  if (!threadsBadge) return;
  if (state && typeof state.threads === "number") {
    threadsBadge.textContent = "\u00b7 " + state.threads +
      (state.threads === 1 ? " thread" : " threads");
  } else {
    threadsBadge.textContent = "";
  }
}

function renderAll() {
  rebuildLegalMap();
  renderBoard();
  renderMoveLog();
  renderTurn();
  renderBanner();
  renderThreadsBadge();
}

// --------------------------------------------------------------------------
// Interaction
// --------------------------------------------------------------------------

function clearMessage() { messageEl.textContent = ""; }
function showMessage(msg) { messageEl.textContent = msg; }

function humansTurnNow() {
  return state && !state.game_over && state.turn === humanColor;
}

function onSquareClick(name) {
  if (busy || !state || state.game_over) return;
  if (!humansTurnNow()) return;

  const pieces = parseFen(state.fen);

  // If clicking a legal destination for the selected piece, attempt the move.
  if (selected && legalFrom[selected] && legalFrom[selected].includes(name)) {
    attemptMove(selected, name, pieces[selected]);
    return;
  }

  // Otherwise treat as (re)selection: only allow selecting squares that have
  // at least one legal move originating from them.
  if (legalFrom[name] && legalFrom[name].length > 0) {
    selected = name;
    clearMessage();
    renderBoard();
    return;
  }

  // Clicked an empty/illegal-origin square: clear selection.
  selected = null;
  renderBoard();
}

function needsPromotion(fromPiece, toSquare) {
  if (!fromPiece || fromPiece.toLowerCase() !== "p") return false;
  const toRank = parseInt(toSquare[1], 10);
  return toRank === 8 || toRank === 1;
}

function attemptMove(from, to, fromPiece) {
  if (needsPromotion(fromPiece, to)) {
    askPromotion((promo) => {
      if (!promo) { selected = null; renderBoard(); return; }
      sendMove(from + to + promo);
    });
  } else {
    sendMove(from + to);
  }
}

function askPromotion(cb) {
  promoChoices.innerHTML = "";
  const options = ["q", "r", "b", "n"];
  // Show glyphs in the human's color.
  const whiteSide = humanColor === "white";
  for (const opt of options) {
    const btn = document.createElement("button");
    btn.type = "button";
    // upper-case glyph = white styling
    const letter = whiteSide ? opt.toUpperCase() : opt;
    btn.textContent = GLYPHS[letter];
    btn.addEventListener("click", () => {
      promoOverlay.hidden = true;
      cb(opt);
    });
    promoChoices.appendChild(btn);
  }
  promoOverlay.hidden = false;
}

// --------------------------------------------------------------------------
// API calls
// --------------------------------------------------------------------------

function setBusy(on) {
  busy = on;
  thinkingEl.hidden = !on;
  newGameBtn.disabled = on;
}

async function sendMove(uci) {
  if (busy || !state) return;
  setBusy(true);
  selected = null;
  renderBoard();
  clearMessage();
  try {
    const res = await fetch("/api/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ game_id: state.game_id, move: uci }),
    });
    if (res.status === 400) {
      // Illegal move (server is authority). Keep board as-is, no desync.
      showMessage("Illegal move. Try a different move.");
      return;
    }
    if (!res.ok) {
      showMessage("Server error (" + res.status + ").");
      return;
    }
    state = await res.json();
    renderAll();
  } catch (e) {
    showMessage("Network error. Please try again.");
  } finally {
    setBusy(false);
  }
}

function chosenThreads() {
  // Read the threads control and clamp to 1..128. Fall back to 128 on junk;
  // the server clamps/defaults too, so this is just a friendly hint.
  let n = parseInt(threadsInput && threadsInput.value, 10);
  if (!Number.isFinite(n)) n = 128;
  n = Math.max(1, Math.min(128, n));
  if (threadsInput) threadsInput.value = String(n);
  return n;
}

async function newGame() {
  const chosen = document.querySelector('input[name="color"]:checked');
  humanColor = chosen ? chosen.value : "white";
  const threads = chosenThreads();
  setBusy(true);
  selected = null;
  clearMessage();
  bannerEl.hidden = true;
  try {
    const res = await fetch("/api/new", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ human_color: humanColor, threads: threads }),
    });
    if (!res.ok) {
      showMessage("Could not start a new game (" + res.status + ").");
      return;
    }
    state = await res.json();
    renderAll();
  } catch (e) {
    showMessage("Network error starting game.");
  } finally {
    setBusy(false);
  }
}

newGameBtn.addEventListener("click", newGame);

// Start an initial game on load (White by default).
newGame();
