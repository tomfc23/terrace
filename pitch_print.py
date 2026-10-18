#!/usr/bin/env python3
"""Turn a collected touch / shot / pass heat map into a printable 3D structure.

A plotted surface is an open shell — zero thickness, no bottom — so it cannot be
printed. This module rebuilds the same frequency field as a closed solid and
exposes the settings that actually change how the print looks and how it slices:

  how tall the hills are       --height-mm, --compression, --cutoff
  how thick the base plate is  --base-mm
  how chunky the surface is    --style (smooth vs stepped), --grid, --sigma
  how big the finished part is --size-mm

Named styles are just bundles of those settings, so you can print each one and
pick a favourite. Every mesh is checked before it is written: it must be closed
(every edge shared by exactly two triangles) and its signed volume must match the
volume worked out from the height field, which is what tells you the faces are
wound the right way round for a slicer.

Usage
-----
    python pitch_print.py styles                       # list the presets
    python pitch_print.py sheet  --player Wirtz --map touch
    python pitch_print.py build  --player Wirtz --map touch --style skyline
    python pitch_print.py build  --player Wirtz --map touch \
        --height-mm 18 --cutoff 0.25 --compression sqrt --levels 6 --stl models/x.stl

Outputs go to models/ (STL) and plots/ (previews). Import with any slicer at
millimetres; the parts are modelled at final size, so do not rescale.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np

import pitch_heatmap3d as ph  # reuse the loader, binning and smoothing

MODEL_DIR = Path("models")
PLOT_DIR = Path("plots")
PLA_DENSITY_G_CM3 = 1.24


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #

@dataclass
class Style:
    """One printable look; every field is overridable from the command line."""

    height_mm: float = 12.0        # tallest point above the base plate
    base_mm: float = 2.5           # solid base plate thickness
    compression: str = "linear"    # how frequency becomes height
    cutoff: float = 0.0            # ignore cells below this share of the peak
    levels: int | None = None      # discrete terrace count; None = continuous slopes
    grid: str = "24x16"            # cells across the 105 x 68 m pitch
    sigma: float = 3.0             # Gaussian smoothing bandwidth, metres
    blurb: str = ""                # what the style is for


STYLES: dict[str, Style] = {
    "terrain": Style(
        height_mm=12, base_mm=2.5, compression="linear", cutoff=0.0,
        grid="24x16", sigma=3.0,
        blurb="Smooth landscape, every touch counts. The honest default.",
    ),
    "skyline": Style(
        height_mm=15, base_mm=2.5, compression="sqrt", cutoff=0.05,
        levels=6, grid="30x19", sigma=2.2,
        blurb="Terraced steps under a sqrt curve, spikes tamed. Reads as a skyline.",
    ),
    "peaks": Style(
        height_mm=17, base_mm=2.5, compression="sqrt", cutoff=0.35,
        grid="28x18", sigma=3.0,
        blurb="Only the hot zones rise; quiet areas stay flat base.",
    ),
    "plateau": Style(
        height_mm=9, base_mm=3.0, compression="linear", cutoff=0.28,
        levels=3, grid="20x13", sigma=4.0,
        blurb="A few chunky mesas. Bold, prints fastest, easiest to read from across a room.",
    ),
    "relief": Style(
        height_mm=5, base_mm=3.0, compression="log", cutoff=0.0,
        grid="36x23", sigma=2.5,
        blurb="Low bas-relief for a coaster or wall tile; detail over drama.",
    ),
}

COMPRESSIONS = ("linear", "sqrt", "log")


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

def cell_edges(count: int, length_mm: float) -> np.ndarray:
    return np.linspace(0.0, length_mm, count + 1)


class Mesh:
    """Triangle soup with the two checks that matter for printing."""

    def __init__(self) -> None:
        self.triangles: list[tuple[tuple[float, float, float], ...]] = []

    def add_quad(self, a, b, c, d) -> None:
        self.triangles.append((a, b, c))
        self.triangles.append((a, c, d))

    def array(self) -> np.ndarray:
        return np.asarray(self.triangles, dtype=float)

    def signed_volume_mm3(self) -> float:
        """Divergence-theorem volume; positive means outward-facing normals."""
        tris = self.array()
        a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
        return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)

    def is_closed(self) -> tuple[bool, int]:
        """True when every edge is shared by exactly two triangles."""
        tris = np.round(self.array(), 6)
        edges: dict[tuple, int] = {}
        for tri in tris:
            for i in range(3):
                p, q = tri[i], tri[(i + 1) % 3]
                key = (tuple(p), tuple(q)) if tuple(p) < tuple(q) else (tuple(q), tuple(p))
                edges[key] = edges.get(key, 0) + 1
        bad = sum(1 for count in edges.values() if count != 2)
        return bad == 0, bad


def corner_heights(heights: np.ndarray) -> np.ndarray:
    """Cell heights averaged onto grid corners, replicating at the edges.

    The corner grid is what the smooth mesh and the trapezoid volume share, so
    the surface and the volume estimate always describe the same geometry.
    """
    padded = np.pad(heights, 1, mode="edge")
    return (padded[:-1, :-1] + padded[1:, :-1] + padded[:-1, 1:] + padded[1:, 1:]) / 4.0


def build_smooth_mesh(heights: np.ndarray, x_edges, y_edges, base_mm: float) -> Mesh:
    """Continuous surface, full rectangular base, walls on the outline."""
    corners = corner_heights(heights)
    nx, ny = heights.shape
    mesh = Mesh()
    top = lambda i, j: (float(x_edges[i]), float(y_edges[j]), float(base_mm + corners[i, j]))
    bottom = lambda i, j: (float(x_edges[i]), float(y_edges[j]), 0.0)

    for i in range(nx):
        for j in range(ny):
            mesh.add_quad(top(i, j), top(i + 1, j), top(i + 1, j + 1), top(i, j + 1))
            mesh.add_quad(bottom(i, j), bottom(i, j + 1), bottom(i + 1, j + 1), bottom(i + 1, j))

    # Walls are built by walking the outline anticlockwise seen from above, so
    # the outward normal is always on the right of the direction of travel.
    for i in range(nx):  # south, then north
        mesh.add_quad(top(i, 0), bottom(i, 0), bottom(i + 1, 0), top(i + 1, 0))
        mesh.add_quad(top(i + 1, ny), bottom(i + 1, ny), bottom(i, ny), top(i, ny))
    for j in range(ny):  # west, then east
        mesh.add_quad(top(0, j + 1), bottom(0, j + 1), bottom(0, j), top(0, j))
        mesh.add_quad(top(nx, j), bottom(nx, j), bottom(nx, j + 1), top(nx, j + 1))
    return mesh





def analytic_volume_mm3(heights: np.ndarray, x_edges, y_edges, base_mm: float) -> float:
    """Volume the mesh should enclose, computed the same way the mesh is built.

    Any mismatch against the mesh volume means a face is wound wrongly or missing,
    which is exactly the failure a slicer cannot repair silently.
    """
    area = float((x_edges[1] - x_edges[0]) * (y_edges[1] - y_edges[0]))
    # Smooth tops are quads a,b,c,d split as (a,b,c)+(a,c,d), so a cell
    # contributes area/6 x (2a + b + 2c + d) — not the plain average of its
    # four corners.
    corners = corner_heights(heights)
    a = corners[:-1, :-1]
    b = corners[1:, :-1]
    c = corners[1:, 1:]
    d = corners[:-1, 1:]
    hills = float(area / 6.0 * (2 * a + b + 2 * c + d).sum())
    return hills + (x_edges[-1] * y_edges[-1]) * base_mm


def write_stl(path: Path, mesh: Mesh) -> Path:
    """Binary STL, normals computed per facet."""
    tris = mesh.array()
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 0)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(b"soccer-modeling pitch print".ljust(80, b"\0"))
        handle.write(np.uint32(len(tris)).tobytes())
        for normal, tri in zip(normals, tris):
            handle.write(np.asarray(normal, dtype=np.float32).tobytes())
            handle.write(np.asarray(tri, dtype=np.float32).tobytes())
            handle.write(np.uint16(0).tobytes())
    return path


# --------------------------------------------------------------------------- #
# Height field
# --------------------------------------------------------------------------- #

def height_field(surface: np.ndarray, style: Style) -> tuple[np.ndarray, list[str]]:
    """Frequency -> millimetres above the base plate, with printability notes."""
    peak = float(surface.max())
    notes: list[str] = []
    if peak <= 0:
        raise SystemExit("the smoothed surface is empty; nothing to print")

    raw = surface / peak
    if style.cutoff > 0:
        keep = raw >= style.cutoff
        span = max(1.0 - style.cutoff, 1e-9)
        norm = np.where(keep, (raw - style.cutoff) / span, 0.0)
    else:
        norm = raw

    if style.compression == "sqrt":
        norm = np.sqrt(norm)
    elif style.compression == "log":
        norm = np.log1p(9.0 * norm) / np.log(10.0)
    elif style.compression != "linear":
        raise SystemExit(f"unknown compression {style.compression!r}")

    heights = np.clip(norm, 0.0, 1.0) * style.height_mm
    if style.levels and style.levels > 1:
        heights = quantize_heights(heights, style.levels, style.height_mm)

    shallow = int(((heights > 0) & (heights < 0.8)).sum())
    if shallow:
        notes.append(
            f"{shallow} cells ({100 * shallow / heights.size:.0f}% of the pitch) are "
            "under 0.8 mm tall — an FDM print shows those as a faint wash; "
            "--cutoff trims them flat"
        )
    if style.base_mm < 1.2:
        notes.append(f"base plate {style.base_mm} mm is thin enough to curl; 2-3 mm is safer")
    return heights, notes


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #



def quantize_heights(heights: np.ndarray, levels: int, height_mm: float) -> np.ndarray:
    """Snap hills onto N even terraces so the surface steps instead of sloping.

    Stepped heights stay a heightfield, so the mesh keeps sharing its grid
    vertices and stays closed — vertical-walled blocks would leave T-junctions
    at every corner where three different heights meet, which slicers cannot
    repair reliably. The risers here are short ramps inside one cell."""
    step = height_mm / (levels - 1)
    snapped = np.round(heights / step) * step
    snapped[snapped < step / 2.0] = 0.0  # anything under half a step stays base
    return snapped


def render_model(
    ax, heights: np.ndarray, x_edges, y_edges, base_mm: float,
    cmap: str, mono: bool, z_max: float,
) -> None:
    xs, ys = np.meshgrid(x_edges, y_edges, indexing="ij")
    zs = base_mm + corner_heights(heights)
    surf = ax.plot_surface(
        xs, ys, zs, cmap=cmap, linewidth=0, antialiased=True,
        vmin=base_mm, vmax=z_max,
    )
    if mono:
        surf.set_array(np.zeros_like(zs))
        surf.set_cmap(colormaps["Greys"])
    ax.set_zlim(0, z_max)


def preview_figure(
    panels: list[dict], title: str, cmap: str, mono: bool, output: Path, dpi: int,
) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    columns = min(len(panels), 3)
    rows = int(np.ceil(len(panels) / columns))
    fig = plt.figure(figsize=(5.2 * columns, 4.6 * rows))
    for index, panel in enumerate(panels, start=1):
        ax = fig.add_subplot(rows, columns, index, projection="3d")
        z_max = float(panel["base_mm"] + corner_heights(panel["heights"]).max())
        render_model(
            ax, panel["heights"], panel["x_edges"], panel["y_edges"],
            panel["base_mm"], cmap, mono, z_max,
        )
        ax.set_xlim(0, panel["x_edges"][-1])
        ax.set_ylim(0, panel["y_edges"][-1])
        ax.set_box_aspect((panel["x_edges"][-1], panel["y_edges"][-1],
                           (panel["x_edges"][-1]) * 0.32))
        ax.view_init(elev=34, azim=-58)
        ax.set_title(panel["title"], fontsize=9.5, pad=6)
        ax.set_xlabel("mm", fontsize=7, labelpad=2)
        ax.set_ylabel("mm", fontsize=7, labelpad=2)
        ax.tick_params(labelsize=6)
        ax.set_zlabel("")
        ax.set_xticks([0, panel["x_edges"][-1]])
        ax.set_yticks([0, panel["y_edges"][-1]])
        ax.zaxis.set_tick_params(labelsize=6)
    fig.suptitle(title, fontsize=12, y=0.995)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return output


# --------------------------------------------------------------------------- #
# Shared build path
# --------------------------------------------------------------------------- #

@dataclass
class Build:
    style_name: str
    style: Style
    heights: np.ndarray
    x_edges: np.ndarray
    y_edges: np.ndarray
    mesh: Mesh
    mesh_volume: float
    expected_volume: float
    closed: bool
    open_edges: int
    notes: list[str] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)

    def size_mm(self) -> tuple[float, float, float]:
        """Bounding box of the part that was actually meshed, from the mesh itself."""
        vertices = self.mesh.array().reshape(-1, 3)
        return (
            float(vertices[:, 0].max()),
            float(vertices[:, 1].max()),
            float(vertices[:, 2].max()),
        )

    def grams(self) -> float:
        return self.mesh_volume / 1000.0 * PLA_DENSITY_G_CM3


def make_build(surface: np.ndarray, style_name: str, style: Style,
               size_mm: float) -> Build:
    heights, notes = height_field(surface, style)
    nx, ny = surface.shape
    width_mm = size_mm * ph.PITCH_WIDTH_M / ph.PITCH_LENGTH_M
    x_edges = cell_edges(nx, size_mm)
    y_edges = cell_edges(ny, width_mm)

    mesh = build_smooth_mesh(heights, x_edges, y_edges, style.base_mm)
    closed, bad = mesh.is_closed()
    return Build(
        style_name=style_name, style=style, heights=heights,
        x_edges=x_edges, y_edges=y_edges, mesh=mesh,
        mesh_volume=mesh.signed_volume_mm3(),
        expected_volume=analytic_volume_mm3(heights, x_edges, y_edges, style.base_mm),
        closed=closed, open_edges=bad, notes=notes,
    )


def report(build: Build) -> str:
    length, width, tall = build.size_mm()
    drift = abs(build.mesh_volume - build.expected_volume) / max(build.expected_volume, 1e-9)
    lines = [
        f"{build.style_name}: {length:.1f} × {width:.1f} × {tall:.1f} mm  "
        f"(base {build.style.base_mm:g} mm + hills {build.style.height_mm:g} mm)",
        f"  mesh: {len(build.mesh.triangles)} triangles, "
        f"closed={'yes' if build.closed else f'NO ({build.open_edges} bad edges)'}, "
        f"volume {build.mesh_volume / 1000:.2f} cm³ "
        f"(expected {build.expected_volume / 1000:.2f}, drift {drift * 100:.2f}%)",
        f"  solid PLA mass upper bound: {build.grams():.0f} g",
    ]
    lines.extend(f"  note: {note}" for note in build.notes)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def gather_surface(args: argparse.Namespace):
    files = ph.discover_files(
        Path(args.data), args.player, args.maps, args.matches, args.all
    )
    rows = []
    for path in files:
        file_rows, _ = ph.read_points(path, args.include_pass_ends)
        rows.extend(file_rows)
    if not rows:
        raise SystemExit("the selected files contain no on-pitch locations")
    bins_x, _, bins_y = args.grid.lower().partition("x") if args.grid else ("24", "x", "16")
    bins_x, bins_y = int(bins_x), int(bins_y)
    grid_x, grid_y, surface, _counts = ph.build_surface(rows, bins_x, bins_y, args.sigma)
    del grid_x, grid_y
    players = sorted({r.player for r in rows})
    maps = sorted({r.map_name for r in rows})
    label = players[0] if len(players) == 1 else f"{len(players)} players"
    return surface, label, maps, len(files)


def style_from_args(args: argparse.Namespace, base: Style) -> Style:
    style = Style(**asdict(base))
    if args.height_mm is not None:
        style.height_mm = args.height_mm
    if args.base_mm is not None:
        style.base_mm = args.base_mm
    if args.cutoff is not None:
        style.cutoff = args.cutoff
    if args.compression:
        style.compression = args.compression
    if args.levels is not None:
        style.levels = args.levels if args.levels > 0 else None
    if args.grid:
        style.grid = args.grid
    if args.sigma is not None:
        style.sigma = args.sigma
    return style


def grid_dims(style: Style) -> tuple[int, int]:
    bins_x, _, bins_y = style.grid.lower().partition("x")
    return int(bins_x), int(bins_y or bins_x)


def cmd_styles(args: argparse.Namespace) -> int:
    print("style      height  base  compression  cutoff  terraces  grid    sigma  what it is")
    for name, style in STYLES.items():
        print(
            f"{name:10s} {style.height_mm:5.1f}mm {style.base_mm:4.1f}mm "
            f"{style.compression:12s} {style.cutoff:6.2f}  "
            f"{('terrace ' + str(style.levels)) if style.levels else 'smooth':8s} "
            f"{style.grid:7s} "
            f"{style.sigma:5.1f}m  {style.blurb}"
        )
    print("\nAny of these can be overridden, e.g. --style skyline --height-mm 20 --cutoff 0.2")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    base = STYLES[args.style]
    style = style_from_args(args, base)
    width_mm = args.size_mm * ph.PITCH_WIDTH_M / ph.PITCH_LENGTH_M
    surface, label, maps, file_count = gather_surface_for(style, args)
    build = make_build(surface, args.style, style, args.size_mm)
    print(f"{label} — {' + '.join(maps)} map from {file_count} file(s)")
    print(report(build))

    stem = args.name or f"{ph.slug(label)}_{'_'.join(maps)}_{args.style}"
    stl = Path(args.stl) if args.stl else MODEL_DIR / f"{stem}.stl"
    write_stl(stl, build.mesh)
    print(f"  wrote {stl}")

    if not args.no_preview:
        png = Path(args.preview) if args.preview else PLOT_DIR / f"{stem}_{args.style}_print.png"
        preview_figure(
            [{"title": f"{args.style} — {build.size_mm()[2]:.0f} mm tall",
              "heights": build.heights, "x_edges": build.x_edges,
              "y_edges": build.y_edges, "base_mm": style.base_mm,
              "height_mm": style.height_mm}],
            f"{label} — {' + '.join(maps)} map, printable {args.style}",
            args.cmap, args.mono, png, args.dpi,
        )
        print(f"  wrote {png}")
    return 0


def cmd_sheet(args: argparse.Namespace) -> int:
    """Mesh every preset, export each STL, and lay them out in one image."""
    panels, builds, cache = [], [], {}
    label, maps, file_count = "", [], 0
    for name, base in STYLES.items():
        style = style_from_args(args, base)
        key = (style.grid, style.sigma)
        if key not in cache:  # several styles can share a grid
            cache[key] = gather_surface_for(style, args)
        surface, label, maps, file_count = cache[key]
        build = make_build(surface, name, style, args.size_mm)
        builds.append(build)
        panels.append({
            "title": f"{name} — {build.size_mm()[2]:.0f} mm tall, {build.grams():.0f} g",
            "heights": build.heights, "x_edges": build.x_edges,
            "y_edges": build.y_edges, "base_mm": style.base_mm,
            "height_mm": style.height_mm,
        })

    print(f"{label} — {' + '.join(maps)} map from {file_count} file(s)\n")
    for build in builds:
        print(report(build))
        stl = MODEL_DIR / f"{ph.slug(label)}_{'_'.join(maps)}_{build.style_name}.stl"
        write_stl(stl, build.mesh)
        print(f"  wrote {stl}")

    sheet = Path(args.output) if args.output else (
        PLOT_DIR / f"{ph.slug(label)}_{'_'.join(maps)}_print_styles.png"
    )
    preview_figure(
        panels, f"{label} — {' + '.join(maps)} map: print styles to choose from",
        args.cmap, args.mono, sheet, args.dpi,
    )
    print(f"\nwrote {sheet}")
    return 0


def gather_surface_for(style: Style, args: argparse.Namespace):
    """Rebin the data at this style's own grid and smoothing."""
    scope = argparse.Namespace(**vars(args))
    scope.grid = style.grid
    scope.sigma = style.sigma
    return gather_surface(scope)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build printable STL models from the Opta heat maps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target):
        target.add_argument("--data", default="data", help="input directory")
        target.add_argument("--player", action="append", help="player name filter")
        target.add_argument("--map", dest="maps", action="append",
                            choices=["shot", "touch", "pass"])
        target.add_argument("--match", dest="matches", action="append")
        target.add_argument("--all", action="store_true")
        target.add_argument("--include-pass-ends", action="store_true")
        target.add_argument("--size-mm", type=float, default=180.0,
                            help="length of the finished part in mm (default 180)")
        target.add_argument("--height-mm", type=float, help="tallest hill, above the base")
        target.add_argument("--base-mm", type=float, help="base plate thickness")
        target.add_argument("--cutoff", type=float, help="drop cells below this share of peak")
        target.add_argument("--compression", choices=COMPRESSIONS)
        target.add_argument("--levels", type=int, default=None,
                            help="terrace steps, e.g. 6; 0 or omitted keeps a smooth surface")
        target.add_argument("--grid", help="cells across the pitch length, e.g. 30x19")
        target.add_argument("--sigma", type=float, help="smoothing bandwidth in metres")
        target.add_argument("--cmap", default="turbo")
        target.add_argument("--mono", action="store_true",
                            help="single-colour preview, closer to the printed part")
        target.add_argument("--dpi", type=int, default=150)

    listing = sub.add_parser("styles", help="list the style presets")
    listing.set_defaults(func=cmd_styles)

    build = sub.add_parser("build", help="mesh and export one style")
    add_common(build)
    build.add_argument("--style", default="terrain", choices=list(STYLES))
    build.add_argument("--stl", help="STL path")
    build.add_argument("--preview", help="PNG path")
    build.add_argument("--name", help="file-name stem")
    build.add_argument("--no-preview", action="store_true")
    build.set_defaults(func=cmd_build)

    sheet = sub.add_parser("sheet", help="mesh every style and lay them out to compare")
    add_common(sheet)
    sheet.add_argument("--style", default="terrain", help="unused; kept for symmetry")
    sheet.add_argument("--output", help="contact-sheet PNG path")
    sheet.set_defaults(func=cmd_sheet)

    args = parser.parse_args(argv)
    if getattr(args, "style", None) is None:
        args.style = "terrain"
    try:
        return args.func(args)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
