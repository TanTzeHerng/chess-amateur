#!/usr/bin/env python3
"""Controlled depth-1 Stockfish 17.1 MPV1 vs MPV4 screening; chess results only."""
import datetime as dt
import json
import os
import pathlib
import platform
import traceback
import chess
import chess.engine
import chess.pgn
import chess.syzygy

ROOT = pathlib.Path("results")
ROOT.mkdir(exist_ok=True)
SF = "/tmp/Stockfish/src/stockfish"
TB = "/tmp/tbs/3-4-5-wdl:/tmp/tbs/3-4-5-dtz"
OPENINGS = [
    "e4 e5 Nf3 Nc6 Bb5 a6",
    "d4 d5 c4 e6 Nc3 Nf6",
    "e4 c5 Nf3 d6 d4 cxd4",
    "d4 Nf6 c4 g6 Nc3 Bg7",
    "e4 e6 d4 d5 Nc3 Bb4",
    "e4 c6 d4 d5 Nc3 dxe4",
    "c4 e5 Nc3 Nf6",
    "Nf3 d5 g3 Nf6 Bg2 g6",
    "e4 e5 Nf3 Nc6 Bc4 Bc5",
    "d4 Nf6 c4 e6 Nf3 b6",
]

def log(*parts):
    print("MULTIPV_MATCH", *parts, flush=True)

def save(data):
    temp = ROOT / "score.json.tmp"
    temp.write_text(json.dumps(data, indent=2, default=str), encoding="utf8")
    temp.replace(ROOT / "score.json")

def check_tb():
    a, b = TB.split(":")
    wdl = list(pathlib.Path(a).glob("*.rtbw"))
    dtz = list(pathlib.Path(b).glob("*.rtbz"))
    if len(wdl) != 145 or len(dtz) != 145:
        raise RuntimeError(f"Syzygy 3-5 pieces incomplete: {len(wdl)} WDL, {len(dtz)} DTZ")
    with chess.syzygy.open_tablebase(a) as table:
        table.add_directory(b)
        assert table.probe_wdl(chess.Board("7k/8/8/8/8/8/2Q5/K7 w - - 0 1")) == 2
    log("SYZYGY_VERIFIED", len(wdl), len(dtz))

def main():
    check_tb()
    data = {
        "settings": {"Stockfish": "17.1", "Threads": 1024, "depth": 1,
                     "MultiPV": [1, 4], "Hash": "UCI default, untouched",
                     "SyzygyProbeLimit": 5, "screen_at": [20, 40],
                     "maximum_games": 100},
        "hardware": {"platform": platform.platform(), "logical_cpus": os.cpu_count()},
        "score": {"MPV1_wins": 0, "draws": 0, "MPV4_wins": 0, "completed": 0},
        "games": [], "errors": [], "status": "setup"
    }
    save(data)
    engines = {}
    try:
        for pv in (1, 4):
            log("STARTING_STOCKFISH", pv)
            eng = chess.engine.SimpleEngine.popen_uci(SF, timeout=600)
            if "17.1" not in eng.id.get("name", ""):
                raise RuntimeError("Wrong Stockfish binary: " + repr(eng.id))
            default_hash = eng.options["Hash"].default
            if default_hash != 16:
                raise RuntimeError(f"Unexpected default hash {default_hash}")
            # Keep the idle opponent at 1 thread to avoid >2048 simultaneous
            # threads on a 4-core hosted runner. Each actual depth-1 search
            # still runs at the requested 1024 Stockfish threads.
            eng.configure({"Threads": 1, "SyzygyPath": TB,
                           "SyzygyProbeLimit": 5, "Syzygy50MoveRule": True,
                           "Skill Level": 20})
            if "SyzygyProbeDepth" in eng.options:
                eng.configure({"SyzygyProbeDepth": 1})
            engines[pv] = eng
            log("ENGINE_VERIFIED", pv, "default_hash_MB", default_hash)
        data["status"] = "playing"
        save(data)
        pairings = [(opening, white) for opening in OPENINGS for white in (True, False)] * 5
        active_thread_owner = None
        with (ROOT / "games.pgn").open("a", encoding="utf8", buffering=1) as fp:
            for num, (opening, pv4_white) in enumerate(pairings, 1):
                played = data["score"]["completed"]
                if played in (20, 40):
                    mpv4_points = data["score"]["MPV4_wins"] + data["score"]["draws"] / 2
                    if mpv4_points <= played / 2:
                        data["status"] = "stopped_no_early_advantage"
                        save(data)
                        log("SCREEN_STOP", played, "MPV4_points", mpv4_points)
                        break
                    log("SCREEN_CONTINUE", played, "MPV4_points", mpv4_points)
                board = chess.Board()
                game = chess.pgn.Game()
                node = game
                game.headers.update({
                    "Event": "Stockfish 17.1 d1 t1024 MPV1 vs MPV4 with Syzygy5",
                    "Site": "GitHub-hosted Linux",
                    "Date": dt.datetime.now(dt.timezone.utc).strftime("%Y.%m.%d"),
                    "Round": str(num),
                    "White": "Stockfish 17.1 MPV4" if pv4_white else "Stockfish 17.1 MPV1",
                    "Black": "Stockfish 17.1 MPV1" if pv4_white else "Stockfish 17.1 MPV4",
                    "WhiteMultiPV": "4" if pv4_white else "1",
                    "BlackMultiPV": "1" if pv4_white else "4",
                    "Threads": "1024", "Depth": "1", "Hash": "16 default",
                    "SyzygyProbeLimit": "5",
                    "Opening": opening
                })
                for san in opening.split():
                    move = board.parse_san(san)
                    node = node.add_variation(move)
                    board.push(move)
                try:
                    for ply in range(650):
                        if board.is_game_over(claim_draw=True):
                            break
                        pv = 4 if (board.turn == chess.WHITE) == pv4_white else 1
                        if active_thread_owner != pv:
                            if active_thread_owner is not None:
                                engines[active_thread_owner].configure({"Threads": 1})
                            engines[pv].configure({"Threads": 1024})
                            active_thread_owner = pv
                        info = engines[pv].analyse(
                            board, chess.engine.Limit(depth=1),
                            multipv=pv, game=num)
                        if not info or not info[0].get("pv"):
                            raise RuntimeError("Engine failed to provide PV")
                        move = info[0]["pv"][0]
                        if move not in board.legal_moves:
                            raise RuntimeError("Illegal move: " + move.uci())
                        node = node.add_variation(move)
                        board.push(move)
                        if ply % 25 == 0:
                            log("MOVE", num, "ply", board.ply(), "MPV", pv, move.uci())
                    if not board.is_game_over(claim_draw=True):
                        raise RuntimeError("No chess result in 650 plies; must replay")
                    result = board.result(claim_draw=True)
                    game.headers["Result"] = result
                    game.headers["Termination"] = "Normal"
                    if result == "1/2-1/2":
                        data["score"]["draws"] += 1
                    elif (result == "1-0") == pv4_white:
                        data["score"]["MPV4_wins"] += 1
                    else:
                        data["score"]["MPV1_wins"] += 1
                    data["score"]["completed"] += 1
                    data["games"].append({
                        "number": num, "opening": opening, "mpv4_white": pv4_white,
                        "result": result, "ply": board.ply()})
                    fp.write(str(game) + "\n\n")
                    fp.flush()
                    save(data)
                    log("GAME", num, "result", result, "SCORE",
                        json.dumps(data["score"]))
                except Exception as exc:
                    data["status"] = "technical_error_unscored"
                    data["errors"].append({
                        "game": num, "error": repr(exc), "fen": board.fen()})
                    save(data)
                    log("TECHNICAL_ERROR_UNSCORED", repr(exc))
                    traceback.print_exc()
                    raise
            else:
                data["status"] = "completed_100"
                save(data)
        log("FINAL", data["status"], json.dumps(data["score"]))
    finally:
        for pv, eng in engines.items():
            try:
                eng.quit()
            except Exception as exc:
                log("ENGINE_QUIT_ISSUE", pv, repr(exc))

if __name__ == "__main__":
    main()
