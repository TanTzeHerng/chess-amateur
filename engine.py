#!/usr/bin/env python3
"""Stockfish UCI wrapper for the "Chess Amateur" bot.

The bot is Stockfish restricted to a very shallow search (depth 1) but with a
large thread count (128), matching the proven invocation in
/projects/sandbox/sf_move.py.

CRITICAL HONESTY CONSTRAINT (inherited from sf_move.py):
  Only the bestmove (UCI) is ever returned. All 'info' lines carrying
  Stockfish's principal variation and evaluation score are intentionally
  ignored so the human opponent never sees the engine's analysis.

Configuration via environment:
  STOCKFISH_PATH  path to the Stockfish binary
                  (default: /projects/sandbox/stockfish/stockfish-linux-x86-64-universal)
"""
import os
import subprocess
import threading

DEFAULT_STOCKFISH_PATH = "/projects/sandbox/stockfish/stockfish-linux-x86-64-universal"
DEFAULT_DEPTH = 1
DEFAULT_THREADS = 128


class ChessAmateurEngine:
    """A thin, reusable wrapper around a long-lived Stockfish process.

    A single engine process is kept alive across moves for efficiency; between
    positions we send 'ucinewgame'/'isready'. The process is re-spawned
    automatically if it has died. Access is serialized with a lock so the
    wrapper is safe to share across HTTP handler threads.

    SINGLE-PROCESS INVARIANT (memory-critical):
      At most ONE Stockfish process may ever be alive at any instant. One
      Stockfish process at Threads=1 uses ~248 MB RSS (dominated by the NNUE
      net). On a 512 MB host (Render free tier) there is only room for a single
      engine plus the Python process. If a replacement engine were spawned
      while the previous one were still alive/shutting down, RSS would
      transiently double to ~500 MB and the OOM killer would terminate the
      service (observed as intermittent 502s with nothing in the app logs).
      Therefore EVERY code path that spawns a replacement engine MUST first
      fully terminate AND os-reap (wait() returned) the previous process before
      calling Popen() again. _kill() is the single choke point that guarantees
      this; _ensure_proc() and best_move()'s retry path both route replacement
      through it so two engines never coexist.
    """

    def __init__(self, path=None, depth=DEFAULT_DEPTH, threads=DEFAULT_THREADS):
        self.path = path or os.environ.get("STOCKFISH_PATH", DEFAULT_STOCKFISH_PATH)
        self.depth = int(depth)
        self.threads = int(threads)
        self._proc = None
        # Thread count currently applied to the live Stockfish process. We only
        # re-send "setoption name Threads" when the requested value changes.
        self._applied_threads = None
        self._lock = threading.Lock()

    # -- process lifecycle -------------------------------------------------

    def _spawn(self):
        proc = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._send(proc, "uci")
        self._read_until(proc, "uciok")
        self._send(proc, "setoption name Threads value %d" % self.threads)
        self._send(proc, "isready")
        self._read_until(proc, "readyok")
        self._applied_threads = self.threads
        return proc

    def _apply_threads(self, proc, threads):
        """Re-apply the Threads UCI option only when it changes."""
        if threads == self._applied_threads:
            return
        self._send(proc, "setoption name Threads value %d" % threads)
        self._send(proc, "isready")
        self._read_until(proc, "readyok")
        self._applied_threads = threads

    def _ensure_proc(self):
        # Reuse the live process if it is still running.
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        # Otherwise a replacement is needed. Enforce the single-process
        # invariant: fully terminate AND reap any existing process (even a
        # dead-but-unreaped one) BEFORE spawning, so two engines never coexist
        # and the OS reclaims the old process's memory first.
        self._kill()
        self._applied_threads = None
        self._proc = self._spawn()
        return self._proc

    @staticmethod
    def _send(proc, line):
        proc.stdin.write(line + "\n")
        proc.stdin.flush()

    @staticmethod
    def _read_until(proc, prefix):
        """Read stdout lines until one starts with `prefix`. Ignores others."""
        while True:
            line = proc.stdout.readline()
            if not line:
                return None
            if line.strip().startswith(prefix):
                return line.strip()

    # -- public API --------------------------------------------------------

    def best_move(self, fen, threads=None):
        """Return Chess Amateur's move (UCI string) for the given FEN.

        Returns None if there is no legal move ('bestmove (none)'/'0000').

        `threads` optionally overrides the Stockfish Threads option for this
        move (and stays applied until changed again). When omitted, the
        engine's default thread count is used. Depth is never affected.
        """
        with self._lock:
            try:
                return self._best_move_locked(fen, threads)
            except (BrokenPipeError, OSError, ValueError):
                # Process may have died mid-request. Fully terminate + reap the
                # old process (single-process invariant) BEFORE _best_move_locked
                # -> _ensure_proc spawns a replacement, so RSS never doubles.
                self._kill()
                return self._best_move_locked(fen, threads)

    def _best_move_locked(self, fen, threads=None):
        proc = self._ensure_proc()
        if threads is not None:
            self._apply_threads(proc, int(threads))
        self._send(proc, "ucinewgame")
        self._send(proc, "isready")
        self._read_until(proc, "readyok")
        self._send(proc, "position fen %s" % fen)
        self._send(proc, "go depth %d" % self.depth)

        bestmove = None
        while True:
            line = proc.stdout.readline()
            if not line:
                raise OSError("Stockfish closed stdout unexpectedly")
            line = line.strip()
            # ONLY the bestmove line matters. 'info' lines (PV + eval) are
            # intentionally ignored and never returned.
            if line.startswith("bestmove"):
                parts = line.split()
                if len(parts) >= 2:
                    bestmove = parts[1]
                break
        if bestmove in (None, "(none)", "0000"):
            return None
        return bestmove

    # -- shutdown ----------------------------------------------------------

    def _kill(self):
        """Synchronously terminate and OS-reap the current process.

        This is the single choke point that upholds the single-process
        invariant: it does not return until the old Stockfish process is dead
        and reaped (wait() has returned), so its ~248 MB of RSS is reclaimed
        before any replacement is spawned. Escalates terminate() -> kill() and
        always wait()s so no zombie/live process lingers.
        """
        proc = self._proc
        # Clear the reference up front so no other logic can observe a
        # half-dead process as "current".
        self._proc = None
        self._applied_threads = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                # Ask politely first, then wait a short while for it to exit.
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    # Still alive -> force kill and reap.
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
            else:
                # Already exited; reap it so the OS releases the process slot.
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        finally:
            # Close pipes to release fds regardless of exit path.
            for stream in (getattr(proc, "stdin", None),
                           getattr(proc, "stdout", None)):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass

    def close(self):
        """Cleanly stop the engine process (send 'quit', then wait/kill)."""
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    self._send(proc, "quit")
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


if __name__ == "__main__":
    eng = ChessAmateurEngine()
    try:
        start_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        print(eng.best_move(start_fen))
    finally:
        eng.close()
