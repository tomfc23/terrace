# Terrace

Opta player maps as printable terrain: collect a player's shots, touches and
passes for one match, build a smoothed frequency surface over the pitch, explore
it in the browser, and build watertight STL reliefs to 3D-print.

Requirements: Node.js 22 or newer (includes npm) and uv.
Install Node.js from https://nodejs.org/ and uv from https://docs.astral.sh/uv/.

After cloning the repository, open a terminal in its directory and run:

    ./run.sh

This installs the locked Python and JavaScript dependencies, builds the
frontend, and starts the backend serving both the website and API at:

    http://127.0.0.1:8001

Press Ctrl+C to stop everything. Internet access is required to install
dependencies and retrieve match data. No API key is required.

To use another port:

    PORT=8002 ./run.sh

Run ./run.sh again after pulling changes to install dependencies and rebuild.
On Windows, use WSL to run the command.

## Browsing a map

The website lists competitions, teams and matches, renders a player's map with
adjustable grid and smoothing, and lets you spin the surface in 3D. The FastAPI
backend behind it (api/main.py) caches everything slow or network-bound under
api/.cache, so repeat views are just file reads.

## Command line

    uv run python -m terrace.opta_collect matches --competition premier-league --team Liverpool
    uv run python -m terrace.opta_collect maps --player "Salah" --competition premier-league --team Liverpool
    uv run python -m terrace.pitch_heatmap3d --list
    uv run python -m terrace.pitch_heatmap3d --player "Wirtz" --map touch
    uv run python -m terrace.pitch_print styles
    uv run python -m terrace.pitch_print build --player Wirtz --map touch --style skyline

- `terrace/opta_collect.py` fetches shot, touch and pass maps from Opta's public
  endpoints and writes clean JSON to `data/`.
- `terrace/pitch_heatmap3d.py` renders frequency surfaces as 3D plots to `plots/`.
- `terrace/pitch_print.py` rebuilds a map as a closed solid and writes an STL to
  `models/`, with a preview in `plots/`. Named styles are just bundles of
  settings, so you can print each one and pick a favourite.

Each tool documents its flags in its module docstring.
