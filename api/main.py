"""FastAPI backend for the Opta heat-map browser.

Two jobs only: list what you can pick from, and render one heat map as a WebP.
Everything that is slow or network-bound is cached on disk under api/.cache, so
the second request for the same picture is a file read.

    uv run uvicorn api.main:app --reload --port 8001

Endpoints
    GET /api/health
    GET /api/competitions
    GET /api/teams?competition=premier-league
    GET /api/matches?competition=premier-league&team=Liverpool&limit=8
    GET /api/render?match=<id>&player=Wirtz&map=touch&grid=16x10&sigma=3.5
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Literal

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:          # opta_collect / pitch_heatmap3d live there
    sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from fastapi import FastAPI, HTTPException, Query, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

import opta_collect as oc  # noqa: E402
import pitch_heatmap3d as ph  # noqa: E402

CACHE = ROOT / "api" / ".cache"
DATA = ROOT / "data"
PITCH_LENGTH_M = ph.PITCH_LENGTH_M
PITCH_WIDTH_M = ph.PITCH_WIDTH_M

CACHE_CONTROL = "public, max-age=31536000, immutable"
# Bump when the drawing changes: it is part of the cache key, so old
# immutable URLs in browser caches stop being reused.
RENDER_VERSION = 5
FIXTURE_TTL = 12 * 3600          # fixtures move slowly
EVENT_TTL = 6 * 3600             # a live match keeps changing
_lock = threading.Lock()         # matplotlib is not thread-safe

COMPETITIONS = {
    "premier-league": "Premier League",
    "la-liga": "La Liga",
    "bundesliga": "Bundesliga",
    "serie-a": "Serie A",
    "ligue-1": "Ligue 1",
    "uefa-champions-league": "Champions League",
    "uefa-europa-league": "Europa League",
    "english-championship": "English Championship",
    "mls": "MLS",
    "saudi-pro-league": "Saudi Pro League",
}

MAPS = ("touch", "shot", "pass")

# Opta event types. Shots are the four attempt types; passes are pass/offside pass.
SHOT_TYPES = {13, 14, 15, 16}
PASS_TYPES = {1, 2}


# --------------------------------------------------------------------------- #
# disk cache
# --------------------------------------------------------------------------- #

def render_key(match: str, player: str, map_name: str, grid: str, sigma: float) -> str:
    """Cache key for a rendered map. Includes RENDER_VERSION so a change to the
    drawing produces a new key instead of serving yesterday's pixels."""
    return hashlib.sha1(
        f"v{RENDER_VERSION}|{match}|{player.lower()}|{map_name}|{grid}|{sigma}".encode()
    ).hexdigest()[:20]


def cached_json(name: str, producer: Callable[[], dict], ttl: int) -> dict:
    path = CACHE / f"{name}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    payload = producer()
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{int(time.time())}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return payload


def cached_bytes(name: str, producer: Callable[[], bytes]) -> bytes:
    path = CACHE / name
    if path.exists():
        return path.read_bytes()
    payload = producer()
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{int(time.time())}.tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)
    return payload


# --------------------------------------------------------------------------- #
# data access
# --------------------------------------------------------------------------- #

def fixtures(competition: str) -> list[dict]:
    if competition not in COMPETITIONS:
        raise HTTPException(404, f"unknown competition {competition!r}")
    tmcl = cached_json(
        f"tmcl-{competition}",
        lambda: {"tmcl": oc.resolve_tmcl(competition)},
        ttl=7 * 24 * 3600,
    )["tmcl"]
    schedule = cached_json(
        f"schedule-{tmcl}",
        lambda: oc.list_matches(tmcl),
        ttl=FIXTURE_TTL,
    )
    return schedule if isinstance(schedule, list) else []


def match_payloads(match_id: str) -> tuple[dict, dict]:
    """The event feed and the lineups for one match, both cached."""
    events = cached_json(f"events-{match_id}", lambda: oc.match_events(match_id), EVENT_TTL)
    lineups = cached_json(
        f"lineups-{match_id}", lambda: oc.match_lineups(match_id), EVENT_TTL
    )
    return events, lineups


def local_points(match_id: str, player_name: str, map_name: str):
    """Fallback: a previously collected file for this match, if we have one."""
    for path in DATA.glob(f"*_{match_id}_{map_name}.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta = payload.get("meta") or {}
        name = str((meta.get("player") or {}).get("name") or "")
        if player_name.lower() not in name.lower():
            continue
        xs, ys = [], []
        for point in payload.get("points", []):
            x, y = point.get("x_m"), point.get("y_m")
            if x is None and point.get("x") is not None:
                x, y = point["x"] * PITCH_LENGTH_M / 100, point["y"] * PITCH_WIDTH_M / 100
            if x is None:
                continue
            if 0 <= x <= PITCH_LENGTH_M and 0 <= y <= PITCH_WIDTH_M:
                xs.append(x)
                ys.append(y)
        return np.array(xs), np.array(ys), len(payload.get("points", []))
    return None


def points_for(events_payload: dict, player: dict, map_name: str):
    """Player's located events for one map, in metres on the 105 x 68 pitch."""
    xs, ys, total = [], [], 0
    for event in (events_payload.get("liveData") or {}).get("event", []) or []:
        if event.get("playerId") != player["player_id"]:
            continue
        type_id = event.get("typeId")
        if map_name == "shot" and type_id not in SHOT_TYPES:
            continue
        if map_name == "pass" and type_id not in PASS_TYPES:
            continue
        if map_name == "touch" and type_id in oc.NON_LOCATION_TYPES:
            continue
        total += 1
        x, y = event.get("x"), event.get("y")
        if x is None or y is None:
            continue
        x, y = x * PITCH_LENGTH_M / 100.0, y * PITCH_WIDTH_M / 100.0
        if 0 <= x <= PITCH_LENGTH_M and 0 <= y <= PITCH_WIDTH_M:
            xs.append(x)
            ys.append(y)
    return np.array(xs), np.array(ys), total


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def render_webp(xs: np.ndarray, ys: np.ndarray, grid: str, sigma: float) -> bytes:
    """A 3D surface of the same field: colour *and* height are events per cell."""
    bins_x, _, bins_y = grid.lower().partition("x")
    nx, ny = int(bins_x), int(bins_y or 16)

    counts, x_edges, y_edges = np.histogram2d(
        xs, ys, bins=(nx, ny),
        range=((0.0, PITCH_LENGTH_M), (0.0, PITCH_WIDTH_M)),
    )
    cell_x, cell_y = PITCH_LENGTH_M / nx, PITCH_WIDTH_M / ny
    surface = ph.smooth_grid(counts, sigma / cell_y, sigma / cell_x)
    x_centres = (x_edges[:-1] + x_edges[1:]) / 2.0
    y_centres = (y_edges[:-1] + y_edges[1:]) / 2.0
    grid_x, grid_y = np.meshgrid(x_centres, y_centres, indexing="ij")
    z_max = float(surface.max()) or 1.0

    with _lock:
        # Fixed slots, no tight bbox: the output is exactly 1100 x 792 every time,
        # so the page can declare the intrinsic size and never shift.
        fig = plt.figure(figsize=(10, 5.9), dpi=110)
        ax = fig.add_axes([0.0, 0.0, 0.87, 1.0], projection="3d")
        cax = fig.add_axes([0.92, 0.32, 0.013, 0.36])

        artist = ax.plot_surface(
            grid_x, grid_y, surface, cmap="turbo", linewidth=0, antialiased=True,
            rstride=1, cstride=1, vmin=0.0, vmax=z_max,
        )
        ph.draw_pitch_markings(ax, colour="#ffffff", alpha=0.55)

        ax.set_xlim(0, PITCH_LENGTH_M)
        ax.set_ylim(0, PITCH_WIDTH_M)
        ax.set_zlim(0, z_max)
        ax.set_box_aspect((PITCH_LENGTH_M, PITCH_WIDTH_M, PITCH_LENGTH_M * 0.3), zoom=1.2)
        ax.view_init(elev=32, azim=-58)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_zticks([])
        for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
            axis.pane.set_alpha(0.05)   # faint floor box gives the pitch its extent
        bar = fig.colorbar(artist, cax=cax)
        bar.set_label("events per cell", fontsize=8.5, labelpad=4)
        bar.ax.tick_params(labelsize=8)

        buffer = io.BytesIO()
        fig.savefig(buffer, format="webp", pil_kwargs={"quality": 82, "method": 4})
        plt.close(fig)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #

app = FastAPI(title="Opta heat maps", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=500)   # html/css/json, not webp
app.add_middleware(
    CORSMiddleware,
    # Only needed for `astro dev`; the built site is served same-origin below.
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1|100\.73\.136\.110|192\.168\.\d+\.\d+)(:\d+)?",
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "cache": str(CACHE.relative_to(ROOT))}


@app.get("/api/competitions")
def competitions() -> Response:
    """The competition list, plus the render version in a header.

    The client appends that version to image URLs: the responses are immutable,
    so the only way to retire an old picture is a new URL.
    """
    import json as _json

    body = _json.dumps([{"slug": slug, "label": label} for slug, label in COMPETITIONS.items()])
    return Response(
        content=body,
        media_type="application/json",
        headers={"X-Render-Version": str(RENDER_VERSION)},
    )


@app.get("/api/teams")
def teams(competition: str = Query(...)) -> dict:
    names: set[str] = set()
    for fixture in fixtures(competition):
        names.update(n for n in (fixture["home"], fixture["away"]) if n)
    return {"competition": competition, "teams": sorted(names)}


@app.get("/api/matches")
def matches(
    competition: str = Query(...),
    team: str | None = None,
    limit: int = Query(8, ge=1, le=50),
) -> list[dict]:
    today = time.strftime("%Y-%m-%d")
    played = [f for f in fixtures(competition) if f["date"] and f["date"] <= today]
    if team:
        needle = team.lower()
        played = [
            f for f in played
            if needle in (f["home"] or "").lower() or needle in (f["away"] or "").lower()
        ]
    return list(reversed(played[-limit:]))


@app.get("/api/players")
def players(match: str = Query(...)) -> dict:
    """Everyone in the match, busiest first — the search bar's suggestions."""
    events, lineups = match_payloads(match)
    rows = oc.lineup_players(lineups)

    touches: dict[str, int] = {}
    for event in (events.get("liveData") or {}).get("event", []) or []:
        player_id = event.get("playerId")
        if player_id and event.get("typeId") not in oc.NON_LOCATION_TYPES:
            touches[player_id] = touches.get(player_id, 0) + 1

    for row in rows:
        row["events"] = touches.get(row["player_id"], 0)
    rows.sort(key=lambda row: -row["events"])
    return {"match": match, "players": rows}


@app.get("/api/render")
def render(
    request: Request,
    match: str = Query(..., description="Opta match id"),
    player: str = Query(..., description="player name or Opta player id"),
    map: Literal["touch", "shot", "pass"] = "touch",
    grid: str = "16x10",
    sigma: float = Query(3.5, ge=0, le=20),
) -> Response:
    key = render_key(match, player, map, grid, sigma)
    etag = f'"{key}"'

    def build() -> bytes:
        try:
            events, lineups = match_payloads(match)
            resolved = oc.resolve_player(events, lineups, player)
            xs, ys, total = points_for(events, resolved, map)
        except HTTPException:
            raise
        except RuntimeError as error:          # unknown/ambiguous player, bad match
            raise HTTPException(404, str(error)) from error
        except Exception as error:             # network trouble: fall back to disk
            fallback = local_points(match, player, map)
            if not fallback:
                raise HTTPException(503, f"could not fetch that match: {error}") from error
            xs, ys, total = fallback
            resolved = {"name": player}
        if xs.size == 0:
            raise HTTPException(404, f"no {map} locations for {player} in that match")

        meta = {
            "match_id": match, "player": resolved.get("name", player), "map": map,
            "events": int(xs.size), "total": int(total), "grid": grid, "sigma": sigma,
        }
        image = render_webp(xs, ys, grid, sigma)
        cached_bytes(f"meta-{key}.json", lambda: json.dumps(meta).encode())
        return image

    was_cached = (CACHE / f"render-{key}.webp").exists()
    if was_cached and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": CACHE_CONTROL})

    image = cached_bytes(f"render-{key}.webp", build)
    headers = {
        "Cache-Control": CACHE_CONTROL,
        "ETag": etag,
        "X-Render": "disk" if was_cached else "fresh",
        "Access-Control-Expose-Headers": "ETag, X-Render",
    }
    return Response(content=image, media_type="image/webp", headers=headers)


@app.get("/api/render/meta")
def render_meta(
    match: str = Query(...), player: str = Query(...), map: str = "touch",
    grid: str = "16x10", sigma: float = 3.5,
) -> dict:
    """The facts behind a render, for the caption. Cheap: reads the cached meta."""
    key = hashlib.sha1(
        f"v{RENDER_VERSION}|{match}|{player.lower()}|{map}|{grid}|{sigma}".encode()
    ).hexdigest()[:20]
    path = CACHE / f"meta-{key}.json"
    if not path.exists():
        raise HTTPException(404, "render it first")
    return json.loads(path.read_text())


# The Astro build, served from the same origin as the API: one process, one port,
# no CORS, and the page can fetch relatively. Registered last so /api wins.
DIST = ROOT / "web" / "dist"
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="web")
