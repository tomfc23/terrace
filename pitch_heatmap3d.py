#!/usr/bin/env python3
"""3D frequency heat maps of Opta player maps.

Reads the JSON written by `opta_collect.py` and turns event locations into a
surface over the pitch: colour *and* height both encode how often the player was
in that spot (events per grid cell). Anything from a single map (one player, one
match) up to a whole season's worth of files can be aggregated into one surface.

How the surface is built
------------------------
1. Every selected event location is binned onto a regular grid over a 105 x 68 m
   pitch (metres, taken from the `x_m` / `y_m` fields).
2. The count grid is smoothed with a separable Gaussian kernel, so the surface
   reads as a kernel density estimate while the volume under it still equals the
   number of events: height = events per cell.
3. The result is drawn as a `plot_surface` coloured by height, with the same
   density projected onto the floor plus pitch markings for orientation.

Usage
-----
    python pitch_heatmap3d.py --list                        # what data is available
    python pitch_heatmap3d.py --player "Wirtz" --map touch   # one player, one map
    python pitch_heatmap3d.py --player "Wirtz"               # all of that player's maps
    python pitch_heatmap3d.py --map shot --all               # every shot file in data/
    python pitch_heatmap3d.py --player "Wirtz" --map pass \
        --include-pass-ends --sigma 4 --output plots/wirtz_pass.png
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import matplotlib

matplotlib.use("Agg")  # headless-safe; --show re-targets the interactive backend
import matplotlib.pyplot as plt  # noqa: E402

PITCH_LENGTH_M = 105.0
PITCH_WIDTH_M = 68.0
DATA_DIR = Path("data")
PLOT_DIR = Path("plots")

# Metres, for the marking geometry.
PENALTY_AREA_DEPTH = 16.5
PENALTY_AREA_WIDTH = 40.32
GOAL_AREA_DEPTH = 5.5
GOAL_AREA_WIDTH = 18.32
CENTRE_CIRCLE_RADIUS = 9.15
PENALTY_SPOT_DISTANCE = 11.0


# --------------------------------------------------------------------------- #
# Loading the collected JSON
# --------------------------------------------------------------------------- #

@dataclass
class EventRow:
    x_m: float
    y_m: float
    player: str
    map_name: str
    match_id: str
    source: Path
    role: str = "start"  # "start" or "end" (pass end points)


def slug(text: str) -> str:
    """File-name-safe version of a player name."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "player"


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_points(path: Path, include_pass_ends: bool = False) -> tuple[list[EventRow], int]:
    """Pull located events out of one JSON file; also report off-pitch drops."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    meta = payload.get("meta") or {}
    map_name = meta.get("map") or path.stem.split("_")[-1]
    player = ((meta.get("player") or {}).get("name")) or path.stem
    match_id = ((meta.get("match") or {}).get("match_id")) or path.stem
    rows: list[EventRow] = []
    dropped = 0

    for point in payload.get("points", []):
        x, y = _number(point.get("x_m")), _number(point.get("y_m"))
        if x is None or y is None:
            x, y = _number(point.get("x")), _number(point.get("y"))
            if x is not None:
                x, y = x * PITCH_LENGTH_M / 100.0, y * PITCH_WIDTH_M / 100.0
        if x is None or y is None:
            continue
        if not (0.0 <= x <= PITCH_LENGTH_M and 0.0 <= y <= PITCH_WIDTH_M):
            dropped += 1  # throw-ins and other off-pitch events
            continue
        rows.append(EventRow(x, y, player, map_name, match_id, path))
        if include_pass_ends and map_name == "pass":
            end_x, end_y = _number(point.get("end_x")), _number(point.get("end_y"))
            if end_x is not None and end_y is not None:
                end_x = end_x * PITCH_LENGTH_M / 100.0
                end_y = end_y * PITCH_WIDTH_M / 100.0
                if 0.0 <= end_x <= PITCH_LENGTH_M and 0.0 <= end_y <= PITCH_WIDTH_M:
                    rows.append(EventRow(end_x, end_y, player, map_name, match_id, path, "end"))
                else:
                    dropped += 1
    return rows, dropped


def read_meta(path: Path) -> dict:
    """The `meta` block of one collected file."""
    try:
        return (json.loads(path.read_text(encoding="utf-8")) or {}).get("meta") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def discover_files(
    data_dir: Path,
    players: list[str] | None,
    maps: list[str] | None,
    match_ids: list[str] | None,
    everything: bool,
) -> list[Path]:
    """Pick the data files matching the filters, matched against their meta block."""
    files = sorted(data_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"no JSON files in {data_dir}/ — run opta_collect.py first")

    def matches(meta: dict, needles: list[str] | None, field: str) -> bool:
        if not needles:
            return True
        haystack = slug(str(meta.get(field) or ""))
        return any(slug(n) in haystack for n in needles)

    def matches_match(meta: dict, needles: list[str] | None) -> bool:
        if not needles:
            return True
        haystack = slug(str(meta.get("match_id") or ""))
        return any(slug(n) in haystack for n in needles)

    selected = []
    for path in files:
        meta = read_meta(path)
        meta["player_name"] = (meta.get("player") or {}).get("name")
        meta["match_id"] = (meta.get("match") or {}).get("match_id")
        if (
            matches(meta, players, "player_name")
            and matches(meta, maps, "map")
            and matches_match(meta, match_ids)
        ):
            selected.append(path)
    if not selected and everything:
        selected = files
    if not selected:
        available: dict[str, list[str]] = {}
        for path in files:
            meta = read_meta(path)
            name = str((meta.get("player") or {}).get("name") or path.stem)
            available.setdefault(name, []).append(str(meta.get("map") or "?"))
        listing = "; ".join(f"{n} ({', '.join(sorted(m))})" for n, m in available.items())
        raise SystemExit(f"no files matched; available: {listing}")
    return selected


# --------------------------------------------------------------------------- #
# The model: histogram -> Gaussian smoothed density
# --------------------------------------------------------------------------- #

def gaussian_kernel_1d(sigma_cells: float, truncate: float = 4.0) -> np.ndarray:
    radius = max(1, int(round(truncate * sigma_cells)))
    offsets = np.arange(-radius, radius + 1, dtype=float)
    kernel = np.exp(-0.5 * (offsets / sigma_cells) ** 2)
    return kernel / kernel.sum()


def _blur_along(grid: np.ndarray, sigma_cells: float, axis: int) -> np.ndarray:
    """Blur one axis with a normalised Gaussian, padding only that axis."""
    kernel = gaussian_kernel_1d(sigma_cells)
    pad = len(kernel) // 2
    pads = [(0, 0), (0, 0)]
    pads[axis] = (pad, pad)
    padded = np.pad(grid, pads, mode="edge")
    return np.apply_along_axis(
        lambda line: np.convolve(line, kernel, mode="valid"), axis, padded
    )


def smooth_grid(grid: np.ndarray, sigma_y_cells: float, sigma_x_cells: float) -> np.ndarray:
    """Separable Gaussian blur; total mass is preserved (so heights stay counts)."""
    out = grid.astype(float)
    if sigma_x_cells > 0:
        out = _blur_along(out, sigma_x_cells, axis=0)
    if sigma_y_cells > 0:
        out = _blur_along(out, sigma_y_cells, axis=1)
    return out


def build_surface(rows: list[EventRow], bins_x: int, bins_y: int, sigma_m: float):
    """Bin event locations and smooth them into a frequency surface."""
    xs = np.array([r.x_m for r in rows])
    ys = np.array([r.y_m for r in rows])
    counts, x_edges, y_edges = np.histogram2d(
        xs,
        ys,
        bins=(bins_x, bins_y),
        range=((0.0, PITCH_LENGTH_M), (0.0, PITCH_WIDTH_M)),
    )
    x_centres = (x_edges[:-1] + x_edges[1:]) / 2.0
    y_centres = (y_edges[:-1] + y_edges[1:]) / 2.0
    cell_x = PITCH_LENGTH_M / bins_x
    cell_y = PITCH_WIDTH_M / bins_y
    surface = smooth_grid(counts, sigma_m / cell_y, sigma_m / cell_x)
    grid_x, grid_y = np.meshgrid(x_centres, y_centres, indexing="ij")
    return grid_x, grid_y, surface, counts


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #

def draw_pitch_markings(ax, colour: str = "#9aa0a6", alpha: float = 0.55, three_d: bool = True) -> None:
    def line(xs, ys):
        if three_d:
            ax.plot(xs, ys, zs=0, zdir="z", color=colour, lw=0.8, alpha=alpha)
        else:
            ax.plot(xs, ys, color=colour, lw=0.8, alpha=alpha)

    half = PITCH_WIDTH_M / 2.0
    line([0, PITCH_LENGTH_M, PITCH_LENGTH_M, 0, 0], [0, 0, PITCH_WIDTH_M, PITCH_WIDTH_M, 0])
    line([PITCH_LENGTH_M / 2, PITCH_LENGTH_M / 2], [0, PITCH_WIDTH_M])

    theta = np.linspace(0, 2 * math.pi, 120)
    line(
        PITCH_LENGTH_M / 2 + CENTRE_CIRCLE_RADIUS * np.cos(theta),
        half + CENTRE_CIRCLE_RADIUS * np.sin(theta),
    )
    line([PITCH_LENGTH_M / 2, PITCH_LENGTH_M / 2], [half - 0.9, half + 0.9])

    for side, start in ((1, 0.0), (-1, PITCH_LENGTH_M)):
        depth = PENALTY_AREA_DEPTH * side
        line(
            [start, start + depth, start + depth, start],
            [half - PENALTY_AREA_WIDTH / 2, half - PENALTY_AREA_WIDTH / 2,
             half + PENALTY_AREA_WIDTH / 2, half + PENALTY_AREA_WIDTH / 2],
        )
        small = GOAL_AREA_DEPTH * side
        line(
            [start, start + small, start + small, start],
            [half - GOAL_AREA_WIDTH / 2, half - GOAL_AREA_WIDTH / 2,
             half + GOAL_AREA_WIDTH / 2, half + GOAL_AREA_WIDTH / 2],
        )
        spot = start + PENALTY_SPOT_DISTANCE * side
        line([spot, spot], [half, half])


def style_axes(ax, title: str, z_max: float, elev: float, azim: float) -> None:
    ax.set_xlim(0, PITCH_LENGTH_M)
    ax.set_ylim(0, PITCH_WIDTH_M)
    ax.set_zlim(0, max(z_max, 1e-6))
    ax.set_xlabel("pitch length (m)", labelpad=8)
    ax.set_ylabel("pitch width (m)", labelpad=8)
    # No z-axis label: the colour bar already names the quantity that height shows.
    ax.set_zlabel("")
    ax.view_init(elev=elev, azim=azim)
    ax.set_title(title, pad=18, fontsize=11)
    ax.set_box_aspect((PITCH_LENGTH_M, PITCH_WIDTH_M, PITCH_LENGTH_M * 0.28), zoom=1.05)
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.pane.set_alpha(0.06)


def render_3d(
    grid_x, grid_y, surface, title: str, cmap: str, elev: float, azim: float,
    floor: bool, output: Path, dpi: int, show: bool,
) -> Path:
    fig = plt.figure(figsize=(11, 8))
    ax = fig.add_subplot(projection="3d")
    z_max = float(surface.max())

    surface_artist = ax.plot_surface(
        grid_x,
        grid_y,
        surface,
        cmap=cmap,
        linewidth=0,
        antialiased=True,
        rstride=1,
        cstride=1,
        vmin=0.0,
        vmax=z_max,
    )
    if floor:
        levels = np.linspace(0.0, z_max, 12)[1:]
        ax.contour(
            grid_x, grid_y, surface, levels=levels, zdir="z", offset=0.0,
            cmap=cmap, linewidths=0.6, alpha=0.9,
        )
    draw_pitch_markings(ax)
    style_axes(ax, title, z_max, elev, azim)
    fig.colorbar(
        surface_artist, ax=ax, shrink=0.6, pad=0.14, fraction=0.028,
        label="events per cell",
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=dpi, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)
    return output


def render_2d(grid_x, grid_y, surface, title: str, cmap: str, output: Path, dpi: int) -> Path:
    """Flat companion view — much easier to read numbers off than the surface."""
    fig, ax = plt.subplots(figsize=(11, 7))
    mesh = ax.pcolormesh(
        grid_x, grid_y, surface, cmap=cmap, shading="nearest", vmin=0.0
    )
    draw_pitch_markings(ax, three_d=False)
    ax.set_aspect("equal")
    ax.set_xlim(0, PITCH_LENGTH_M)
    ax.set_ylim(0, PITCH_WIDTH_M)
    ax.set_xlabel("pitch length (m)")
    ax.set_ylabel("pitch width (m)")
    ax.set_title(title, fontsize=11)
    fig.colorbar(mesh, ax=ax, shrink=0.8, pad=0.02, label="events per cell")
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return output


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_list(args: argparse.Namespace) -> int:
    files = sorted(Path(args.data).glob("*.json"))
    if not files:
        print(f"no JSON files in {args.data}/")
        return 1
    print(f"{'file':52s} {'map':6s} {'points':>7s}  player")
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        meta = payload.get("meta") or {}
        print(
            f"{path.name:52.52s} {str(meta.get('map')):6.6s} "
            f"{len(payload.get('points', [])):7d}  "
            f"{(meta.get('player') or {}).get('name')}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="3D frequency heat map of collected Opta player maps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", action="store_true", help="list the available data files")
    parser.add_argument("--data", default=str(DATA_DIR), help="input directory (data/)")
    parser.add_argument("--player", help="substring of the player name (repeatable)",
                        action="append")
    parser.add_argument("--map", dest="maps", action="append", choices=["shot", "touch", "pass"],
                        help="which map type(s) to include (repeatable)")
    parser.add_argument("--match", dest="matches", action="append", help="match id filter")
    parser.add_argument("--all", action="store_true", help="use every file in the data dir")
    parser.add_argument("--include-pass-ends", action="store_true",
                        help="also count where passes finished, not just where they started")
    parser.add_argument("--grid", default="24x16", help="cells across x width, e.g. 24x16")
    parser.add_argument("--sigma", type=float, default=3.0,
                        help="Gaussian smoothing bandwidth in metres (0 = raw bins)")
    parser.add_argument("--cmap", default="turbo", help="matplotlib colormap")
    parser.add_argument("--elev", type=float, default=32.0, help="camera elevation")
    parser.add_argument("--azim", type=float, default=-58.0, help="camera azimuth")
    parser.add_argument("--no-floor", action="store_true", help="skip the floor projection")
    parser.add_argument("--also-2d", action="store_true", help="also write a flat heat map")
    parser.add_argument("--output", help="output PNG (default plots/<something>.png)")
    parser.add_argument("--dpi", type=int, default=160)
    parser.add_argument("--show", action="store_true", help="open an interactive window")
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list(args)

    if args.show:
        # Agg is the default so this file runs headless; only --show needs a window.
        try:
            plt.switch_backend("MacOSX" if sys.platform == "darwin" else "TkAgg")
        except Exception as error:  # no display / no GUI toolkit installed
            print(f"note: no interactive window ({error}); writing PNGs only", file=sys.stderr)

    bins_x, _, bins_y = args.grid.lower().partition("x")
    bins_x, bins_y = int(bins_x), int(bins_y or 16)

    files = discover_files(
        Path(args.data), args.player, args.maps, args.matches, args.all
    )
    rows, dropped = [], 0
    for path in files:
        file_rows, file_dropped = read_points(path, args.include_pass_ends)
        rows.extend(file_rows)
        dropped += file_dropped
    if not rows:
        raise SystemExit("the selected files contain no on-pitch locations")

    grid_x, grid_y, surface, counts = build_surface(rows, bins_x, bins_y, args.sigma)

    players = sorted({r.player for r in rows})
    map_names = sorted({r.map_name for r in rows})
    matches = sorted({r.match_id for r in rows})
    label = players[0] if len(players) == 1 else f"{len(players)} players"
    title = (
        f"{label} — {' + '.join(map_names)} map\n"
        f"{len(rows)} events | {len(matches)} match{'es' if len(matches) > 1 else ''} | "
        f"{bins_x}x{bins_y} grid, sigma {args.sigma:g} m"
    )

    stem = args.output
    if not stem:
        name = "_".join(
            [
                *[slug(p) for p in players[:2]],
                *map_names,
                f"{len(matches)}match{'es' if len(matches) > 1 else ''}",
            ]
        )
        stem = str(PLOT_DIR / f"{name}_heatmap3d.png")
    output = Path(stem)
    rendered = render_3d(
        grid_x, grid_y, surface, title, args.cmap, args.elev, args.azim,
        not args.no_floor, output, args.dpi, args.show,
    )
    print(
        f"{len(files)} file(s) -> {len(rows)} plotted events"
        + (f" ({dropped} off-pitch skipped)" if dropped else "")
    )
    print(f"peak cell {surface.max():.2f} events, total volume {surface.sum():.1f}")
    print(f"saved {rendered}")

    if args.also_2d:
        flat = render_2d(
            grid_x, grid_y, surface, title, args.cmap,
            output.with_name(f"{output.stem}_flat.png"), args.dpi,
        )
        print(f"saved {flat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
