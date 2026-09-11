#!/usr/bin/env python3
"""HTTP backend for playing chess against "Chess Amateur" (Stockfish depth 1).

Uses only the Python standard library (http.server) plus python-chess. No
external web framework. python-chess is the single source of truth for board
state, move legality, SAN, FEN, and result detection.

Endpoints:
  POST /api/new    body {"human_color": "white"|"black", "threads": 1..128?}
  POST /api/move   body {"game_id": ..., "move": "e2e4"}
  GET  /api/state?game_id=...
  GET  /            -> static/index.html
  GET  /<path>      -> static/<path>

Environment:
  PORT            server port (default 8000)
  STOCKFISH_PATH  Stockfish binary path (see engine.py)
  SF_THREADS      server-wide default Stockfish thread count (default 128).
                  Per-request "threads" in POST /api/new overrides this.

Run: python3 chess_amateur/server.py
"""
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import chess

from engine import ChessAmateurEngine, DEFAULT_THREADS

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
BOT_NAME = "Chess Amateur"

# Allowed range for the Stockfish "Threads" option (per game).
MIN_THREADS = 1
MAX_THREADS = 128


def _clamp_threads(value):
    """Clamp a candidate thread count to [MIN_THREADS, MAX_THREADS]."""
    return max(MIN_THREADS, min(MAX_THREADS, int(value)))


def _server_default_threads():
    """Server-wide default thread count: SF_THREADS env, else 128.

    An unset, invalid, or out-of-range SF_THREADS falls back to the hard-coded
    DEFAULT_THREADS (128), clamped into the valid range.
    """
    raw = os.environ.get("SF_THREADS")
    if raw is None:
        return DEFAULT_THREADS
    try:
        return _clamp_threads(raw)
    except (TypeError, ValueError):
        return DEFAULT_THREADS


# Server-wide default applied when a game does not request a specific count.
DEFAULT_GAME_THREADS = _server_default_threads()


def _coerce_threads(value):
    """Resolve a per-request threads value.

    Priority: a valid integer in range from the request body wins; anything
    missing, non-integer, or out of range falls back to the server default
    (SF_THREADS env, else 128). Forgiving by design: never errors.
    """
    if value is None:
        return DEFAULT_GAME_THREADS
    if isinstance(value, bool):  # bool is an int subclass; reject it.
        return DEFAULT_GAME_THREADS
    if not isinstance(value, int):
        # Accept numeric strings like "2" but reject junk.
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            return DEFAULT_GAME_THREADS
    if value < MIN_THREADS or value > MAX_THREADS:
        return DEFAULT_GAME_THREADS
    return value


# Shared, thread-safe engine instance reused across all games/requests.
ENGINE = ChessAmateurEngine()

# In-memory game store: game_id -> {board, human_color, san_history, threads}
GAMES = {}
GAMES_LOCK = threading.Lock()

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


# -- game-state helpers ----------------------------------------------------

def _result_reason(board):
    """Human-readable reason the game ended, or None if still in progress."""
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material():
        return "insufficient material"
    if board.is_seventyfive_moves() or board.can_claim_fifty_moves():
        return "fifty-move rule"
    if board.is_fivefold_repetition() or board.can_claim_threefold_repetition():
        return "repetition"
    return None


def _status(board):
    if board.is_game_over(claim_draw=True):
        return "game_over"
    return "white_to_move" if board.turn == chess.WHITE else "black_to_move"


def _legal_moves(board):
    return [m.uci() for m in board.legal_moves]


def _turn_str(board):
    return "white" if board.turn == chess.WHITE else "black"


def _engine_move(board, threads=None):
    """Ask Chess Amateur for a move, validate it, push it. Returns (uci, san)
    or (None, None) if no move was made. `threads` sets the Stockfish Threads
    option for this move."""
    uci = ENGINE.best_move(board.fen(), threads=threads)
    if not uci:
        return None, None
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None, None
    if move not in board.legal_moves:
        return None, None
    san = board.san(move)
    board.push(move)
    return uci, san


def _state_dict(game, last_bot_move=None):
    board = game["board"]
    game_over = board.is_game_over(claim_draw=True)
    result = board.result(claim_draw=True) if game_over else None
    return {
        "game_id": game["game_id"],
        "fen": board.fen(),
        "human_color": game["human_color"],
        "threads": game.get("threads", DEFAULT_GAME_THREADS),
        "turn": _turn_str(board),
        "legal_moves": _legal_moves(board),
        "status": _status(board),
        "san_history": list(game["san_history"]),
        "bot_name": BOT_NAME,
        "last_bot_move": last_bot_move,
        "game_over": game_over,
        "result": result,
        "result_reason": _result_reason(board) if game_over else None,
    }


# -- request handler -------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ChessAmateur/1.0"

    def log_message(self, fmt, *args):
        # Keep logs concise but present.
        print("%s - %s" % (self.address_string(), fmt % args))

    # -- helpers --
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({"error": message}, status=status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # -- routing --
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            return self._handle_state(parse_qs(parsed.query))
        if path.startswith("/api/"):
            return self._send_error_json("not found", status=404)
        return self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/new":
                return self._handle_new()
            if path == "/api/move":
                return self._handle_move()
        except json.JSONDecodeError:
            return self._send_error_json("invalid JSON body", status=400)
        return self._send_error_json("not found", status=404)

    # -- API handlers --
    def _handle_new(self):
        body = self._read_json_body()
        human_color = str(body.get("human_color", "white")).lower()
        if human_color not in ("white", "black"):
            return self._send_error_json("human_color must be 'white' or 'black'", status=400)

        threads = _coerce_threads(body.get("threads"))

        game_id = str(uuid.uuid4())
        board = chess.Board()
        game = {
            "game_id": game_id,
            "board": board,
            "human_color": human_color,
            "san_history": [],
            "threads": threads,
        }

        last_bot_move = None
        # If the human is Black, Chess Amateur (White) moves first.
        if human_color == "black" and not board.is_game_over(claim_draw=True):
            uci, san = _engine_move(board, threads=threads)
            if uci:
                game["san_history"].append(san)
                last_bot_move = {"uci": uci, "san": san}

        with GAMES_LOCK:
            GAMES[game_id] = game
        return self._send_json(_state_dict(game, last_bot_move=last_bot_move))

    def _handle_move(self):
        body = self._read_json_body()
        game_id = body.get("game_id")
        move_uci = body.get("move")

        with GAMES_LOCK:
            game = GAMES.get(game_id)
        if game is None:
            return self._send_error_json("unknown game_id", status=404)

        board = game["board"]
        if board.is_game_over(claim_draw=True):
            return self._send_error_json("game is over", status=400)

        # Parse + validate the human move. Illegal => 400, no state change.
        try:
            move = chess.Move.from_uci(str(move_uci))
        except (ValueError, TypeError):
            return self._send_error_json("illegal move", status=400)
        if move not in board.legal_moves:
            return self._send_error_json("illegal move", status=400)

        # Apply human move (record SAN before pushing).
        human_san = board.san(move)
        board.push(move)
        game["san_history"].append(human_san)

        # Chess Amateur replies if the game continues.
        last_bot_move = None
        if not board.is_game_over(claim_draw=True):
            uci, san = _engine_move(board, threads=game.get("threads"))
            if uci:
                game["san_history"].append(san)
                last_bot_move = {"uci": uci, "san": san}

        return self._send_json(_state_dict(game, last_bot_move=last_bot_move))

    def _handle_state(self, query):
        game_id_list = query.get("game_id")
        game_id = game_id_list[0] if game_id_list else None
        with GAMES_LOCK:
            game = GAMES.get(game_id)
        if game is None:
            return self._send_error_json("unknown game_id", status=404)
        return self._send_json(_state_dict(game))

    # -- static files --
    def _serve_static(self, path):
        if path == "/" or path == "":
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        # Prevent path traversal: resolve and confirm the target stays inside
        # the static directory.
        static_root = os.path.realpath(STATIC_DIR)
        full = os.path.realpath(os.path.join(static_root, rel))
        if full != static_root and not full.startswith(static_root + os.sep):
            return self._send_error_json("forbidden", status=403)
        if not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("%s server running at http://localhost:%d (default threads: %d)"
          % (BOT_NAME, port, DEFAULT_GAME_THREADS))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ENGINE.close()


if __name__ == "__main__":
    main()
