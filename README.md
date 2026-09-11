# Chess Amateur

A small, self-contained website that lets you play chess against **Chess Amateur**,
a bot powered by [Stockfish](https://stockfishchess.org/) running at **search depth 1**
with **128 threads**. Because the search is only one ply deep, Chess Amateur plays
at a beatable, amateur strength while still making legal, sensible-looking moves.

The whole app runs on the Python standard library (`http.server`) plus
[`python-chess`](https://python-chess.readthedocs.io/). There is no web framework,
no bundler, and no npm step. The frontend is plain HTML/CSS/JavaScript served
directly by the backend.

## What it does

- Play a full game of chess in the browser against Chess Amateur.
- You can play as White or Black; if you choose Black, Chess Amateur moves first.
- Every move is validated **server-side** by python-chess, which is the single
  source of truth for board state, move legality, SAN notation, FEN, and
  game-over/result detection. Illegal moves are rejected and never change the board.
- Checkmate, stalemate, and draw conditions (insufficient material, fifty-move
  rule, repetition) are detected and reported.

## Prerequisites

- **Python 3** (tested on 3.9).
- **python-chess** (`pip install chess`). Already installed in this environment
  (version 1.11.2).
- A **Stockfish** binary. By default the app looks for it at:

  ```
  /projects/sandbox/stockfish/stockfish-linux-x86-64-universal
  ```

  You can point it at a different binary by setting the `STOCKFISH_PATH`
  environment variable:

  ```
  export STOCKFISH_PATH=/path/to/your/stockfish
  ```

## Running the app

From inside the `chess_amateur` directory:

```
python3 server.py
```

or from the repository root:

```
python3 chess_amateur/server.py
```

On startup the server prints the URL it is listening on, for example:

```
Chess Amateur server running at http://localhost:8000
```

Then open **http://localhost:8000** in your browser and start playing.

### Environment overrides

| Variable         | Default                                                            | Purpose                                                   |
| ---------------- | ------------------------------------------------------------------ | --------------------------------------------------------- |
| `PORT`           | `8000`                                                             | Port the HTTP server listens on (binds `0.0.0.0`).        |
| `STOCKFISH_PATH` | `/projects/sandbox/stockfish/stockfish-linux-x86-64-universal`     | Path to the Stockfish binary.                             |
| `SF_THREADS`     | `128`                                                              | Server-wide default Stockfish thread count (1&ndash;128). |

Example, running on a different port:

```
PORT=9000 python3 chess_amateur/server.py
# then open http://localhost:9000
```

### Choosing the engine thread count

Chess Amateur always searches only **one ply deep** (depth 1), which is what
keeps it beatable. The **thread count does not change that** — it only affects
how much CPU Stockfish uses while picking its depth-1 move.

There are three ways to set it, in priority order:

1. **Per game (UI / request):** the "Engine threads" control on the page sends a
   `threads` value (1&ndash;128) with `POST /api/new`. This wins for that game.
2. **Server default:** the `SF_THREADS` environment variable (default `128`).
3. **Hard-coded fallback:** `128` when nothing else is set.

Missing, out-of-range, or invalid values fall back to the default rather than
erroring. The active thread count for a game is returned in the API state (the
`threads` field) and shown next to the opponent label in the UI.

On a small host (a tiny VPS, a free tier, a phone-tethered box) set a low count
like `SF_THREADS=2` — spawning 128 threads there is wasteful and slow to start.

> **Note:** The very first engine call can take a little while because Stockfish
> spawns 128 threads on startup. This is expected.

## HTTP API

The frontend talks to a tiny JSON API (also usable directly with `curl`):

- `POST /api/new` — body `{"human_color": "white" | "black", "threads": 1..128?}`.
  Starts a new game and returns the game state (including a `game_id` and the
  active `threads` count). `threads` is optional; omit it to use the server
  default (`SF_THREADS`, else 128).
- `POST /api/move` — body `{"game_id": "...", "move": "e2e4"}`. Applies your move
  (UCI notation), then returns Chess Amateur's reply and the updated state.
  Illegal moves return HTTP 400 with `{"error": "illegal move"}` and leave the
  board unchanged.
- `GET /api/state?game_id=...` — returns the current state of a game.
- `GET /` — serves the frontend (`static/index.html`).

## Running the tests

Self-contained backend smoke tests (no external test framework required):

```
python3 chess_amateur/test_backend.py
```

The tests exercise real code paths:

- Chess Amateur (`ChessAmateurEngine.best_move`) returns a legal UCI move.
- Starting a new game as White and as Black.
- A legal move followed by a real engine reply, with a consistent SAN history.
- An illegal move is rejected (HTTP 400) and the board state is unchanged.
- `GET /api/state` returns the current state.
- **Game-over detection**: a Fool's-mate checkmate position is reported as
  `game_over` with result `0-1` and `result_reason` `"checkmate"`.
- `GET /` serves the frontend.

They spin up the real server on a throwaway port within the test process, so no
separate server needs to be running.

## Deploying with Docker

The repo ships a `Dockerfile` that bundles a Stockfish binary (linux/amd64)
inside the image, so the container runs anywhere without an external engine
install. It uses a slim Python base and installs only `python-chess`.

Build and run locally:

```
docker build -t chess-amateur .
docker run -p 8000:8000 -e SF_THREADS=2 chess-amateur
# then open http://localhost:8000
```

- The container honors `PORT` (default `8000`) and binds `0.0.0.0`, which is
  what hosting platforms expect. Most platforms inject their own `PORT`; the
  server picks it up automatically.
- `SF_THREADS` sets the default engine thread count in the container. On small
  hosts, keep it low (`1`&ndash;`2`). Regardless of thread count, the engine
  stays at depth 1, so Chess Amateur remains beatable.
- `STOCKFISH_PATH` is preset to the bundled binary (`/usr/local/bin/stockfish`)
  inside the image; you do not need to set it.

### Picking a host

You just need a host that can run a Docker container and give you a public URL,
which you can then open on your phone. Any of the common container hosts work
(for example a small VPS running `docker run`, or a platform-as-a-service that
builds from a `Dockerfile`). Point it at this directory, let it build the image,
set `SF_THREADS` low if the host is small, and open the URL it gives you.

> **Note:** the image bundles the ~103&nbsp;MB Stockfish binary, so the build
> context and image are correspondingly large. `.dockerignore` trims caches and
> tests from the context.

## Notes

- All move legality is enforced **server-side** via python-chess; the browser is
  never trusted to decide what is legal.
- A single Stockfish process is reused across moves and is shut down cleanly when
  the server stops.
