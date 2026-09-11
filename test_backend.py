#!/usr/bin/env python3
"""Self-contained smoke tests for the Chess Amateur backend.

Run: python3 chess_amateur/test_backend.py

Exercises:
  - engine.py returns a legal UCI move for the start position
  - POST /api/new (white and black)
  - POST /api/move with a legal move (engine replies)
  - POST /api/new with a specific "threads" value is honored (returned in
    state) and the engine still returns a legal reply; missing/out-of-range/
    invalid "threads" falls back to the default (128)
  - POST /api/move with an illegal move -> HTTP 400, state unchanged
  - GET /api/state
  - in-progress result fields are None
  - explicit game-over detection: Fool's-mate checkmate reported with the
    correct result ("0-1") and result_reason ("checkmate")
  - /api/move on a finished game -> HTTP 400
  - static index.html served at GET /

Uses only the standard library plus python-chess. Spins the real server on a
throwaway port in a background thread.
"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import chess  # noqa: E402
from engine import ChessAmateurEngine  # noqa: E402

PORT = int(os.environ.get("TEST_PORT", "8137"))
BASE = "http://127.0.0.1:%d" % PORT


def _post(path, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(path):
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def start_server():
    os.environ["PORT"] = str(PORT)
    import server  # noqa: E402
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, server


def test_engine():
    eng = ChessAmateurEngine()
    try:
        start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        mv = eng.best_move(start)
        assert mv is not None, "engine returned no move"
        board = chess.Board(start)
        assert chess.Move.from_uci(mv) in board.legal_moves, "engine move illegal: %s" % mv
        print("PASS engine.best_move ->", mv)
    finally:
        eng.close()


def main():
    test_engine()
    httpd, server = start_server()
    try:
        # new game as white
        status, data = _post("/api/new", {"human_color": "white"})
        assert status == 200, data
        assert data["bot_name"] == "Chess Amateur"
        assert data["human_color"] == "white"
        assert data["fen"].startswith("rnbqkbnr/pppppppp"), data["fen"]
        assert len(data["legal_moves"]) == 20, data["legal_moves"]
        gid = data["game_id"]
        print("PASS /api/new (white) game_id=%s" % gid)

        # legal move -> engine replies
        status, data = _post("/api/move", {"game_id": gid, "move": "e2e4"})
        assert status == 200, data
        assert data["san_history"][0] == "e4", data["san_history"]
        assert data["last_bot_move"] is not None, data
        assert len(data["san_history"]) == 2, data["san_history"]
        assert not data["game_over"]
        print("PASS /api/move legal, bot replied:", data["last_bot_move"])

        # illegal move -> 400, state unchanged
        status_before, state_before = _get("/api/state?game_id=%s" % gid)
        assert status_before == 200
        fen_before = json.loads(state_before)["fen"]
        status, data = _post("/api/move", {"game_id": gid, "move": "e2e4"})
        assert status == 400, (status, data)
        assert data.get("error") == "illegal move", data
        _, state_after = _get("/api/state?game_id=%s" % gid)
        assert json.loads(state_after)["fen"] == fen_before, "state changed on illegal move"
        print("PASS /api/move illegal -> 400, state unchanged")

        # threads: a specific in-range value is honored and returned in state,
        # and the engine still returns a legal reply.
        status, data = _post("/api/new", {"human_color": "white", "threads": 2})
        assert status == 200, data
        assert data["threads"] == 2, data
        tgid = data["game_id"]
        status, data = _post("/api/move", {"game_id": tgid, "move": "e2e4"})
        assert status == 200, data
        assert data["threads"] == 2, data
        assert data["last_bot_move"] is not None, data
        # The engine's reply must be legal in the position after 1.e4.
        rboard = chess.Board()
        rboard.push(chess.Move.from_uci("e2e4"))
        assert chess.Move.from_uci(data["last_bot_move"]["uci"]) in rboard.legal_moves, data
        print("PASS /api/new threads=2 honored, engine replied:", data["last_bot_move"])

        # threads: missing -> defaults to 128
        status, data = _post("/api/new", {"human_color": "white"})
        assert status == 200, data
        assert data["threads"] == 128, data
        print("PASS /api/new missing threads -> default 128")

        # threads: out-of-range and invalid values -> default 128
        for bad in (0, -5, 999, "abc", 3.5, True, None):
            status, data = _post("/api/new", {"human_color": "white", "threads": bad})
            assert status == 200, (bad, data)
            assert data["threads"] == 128, (bad, data)
        print("PASS /api/new out-of-range/invalid threads -> default 128")

        # new game as black -> engine moves first
        status, data = _post("/api/new", {"human_color": "black"})
        assert status == 200, data
        assert data["human_color"] == "black"
        assert len(data["san_history"]) == 1, data["san_history"]
        assert data["last_bot_move"] is not None, data
        print("PASS /api/new (black), bot first move:", data["last_bot_move"])

        # in-progress result fields are None while the game continues
        status, data = _post("/api/new", {"human_color": "white"})
        gid2 = data["game_id"]
        status, data = _post("/api/move", {"game_id": gid2, "move": "e2e4"})
        assert data["result"] is None and data["result_reason"] is None
        assert data["status"] in ("white_to_move", "black_to_move")
        assert not data["game_over"]
        print("PASS in-progress result fields are None")

        # EXPLICIT game-over / terminal-position test.
        # Build Fool's mate on a real python-chess board, register it in the
        # server's game store, then read it back through the real GET /api/state
        # serialization path and assert terminal fields.
        mate_board = chess.Board()
        for uci in ("f2f3", "e7e5", "g2g4", "d8h4"):
            mate_board.push(chess.Move.from_uci(uci))
        assert mate_board.is_checkmate(), "setup position is not checkmate"
        san_history = ["f3", "e5", "g4", "Qh4#"]
        mate_gid = "test-fools-mate"
        with server.GAMES_LOCK:
            server.GAMES[mate_gid] = {
                "game_id": mate_gid,
                "board": mate_board,
                "human_color": "white",
                "san_history": san_history,
            }
        status, body = _get("/api/state?game_id=%s" % mate_gid)
        assert status == 200, (status, body)
        mate_state = json.loads(body)
        assert mate_state["game_over"] is True, mate_state
        # White (human) is checkmated, so Black wins -> "0-1".
        assert mate_state["result"] == "0-1", mate_state["result"]
        assert mate_state["result_reason"] == "checkmate", mate_state["result_reason"]
        assert mate_state["status"] == "game_over", mate_state["status"]
        assert mate_state["legal_moves"] == [], mate_state["legal_moves"]
        assert mate_state["san_history"][-1] == "Qh4#", mate_state["san_history"]
        print("PASS game-over detection: checkmate reported (result 0-1)")

        # Also verify /api/move refuses to move once the game is over.
        status, data = _post("/api/move", {"game_id": mate_gid, "move": "e2e4"})
        assert status == 400, (status, data)
        assert data.get("error") == "game is over", data
        print("PASS /api/move on finished game -> 400 'game is over'")

        # static index served
        status, body = _get("/")
        assert status == 200, status
        assert "Chess Amateur" in body, body[:200]
        print("PASS GET / serves index.html")

        print("\nALL TESTS PASSED")
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.ENGINE.close()


if __name__ == "__main__":
    main()
