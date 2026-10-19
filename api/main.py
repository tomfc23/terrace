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
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Literal

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:          # the terrace package lives there
    sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from fastapi import FastAPI, HTTPException, Query, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

from terrace import opta_collect as oc  # noqa: E402
from terrace import pitch_heatmap3d as ph  # noqa: E402

CACHE = ROOT / "api" / ".cache"
DATA = ROOT / "data"
PITCH_LENGTH_M = ph.PITCH_LENGTH_M
PITCH_WIDTH_M = ph.PITCH_WIDTH_M

CACHE_CONTROL = "public, max-age=31536000, immutable"
# Bump when the drawing changes: it is part of the cache key, so old
# immutable URLs in browser caches stop being reused.
RENDER_VERSION = 7
FIXTURE_TTL = 12 * 3600          # fixtures move slowly
EVENT_TTL = 60                  # a live match keeps changing
FINISHED_TTL = 10 * 365 * 24 * 3600
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

# Opta event types. Shots are the four attempt types; passes are pass/offside pass.
SHOT_TYPES = {13, 14, 15, 16}
PASS_TYPES = {1, 2}


# --------------------------------------------------------------------------- #
# disk cache
# --------------------------------------------------------------------------- #

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def safe_key(value: str) -> str:
    """Cache- and glob-safe id: real match ids pass through unchanged, anything
    else is hashed so a request value can never name a path segment."""
    return value if _SAFE_ID.fullmatch(value) else "h" + hashlib.sha1(value.encode()).hexdigest()[:20]


def render_key(match: str, player: str, map_name: str, grid: str, sigma: float) -> str:
    """Cache key for a rendered map. Includes RENDER_VERSION so a change to the
    drawing produces a new key instead of serving yesterday's pixels."""
    return hashlib.sha1(
        f"v{RENDER_VERSION}|{match}|{player.lower()}|{map_name}|{grid}|{sigma}".encode()
    ).hexdigest()[:20]


def write_cache(path: Path, payload: bytes) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=CACHE, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(payload)
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def cached_json(name: str, producer: Callable, ttl: int | Callable) -> dict | list:
    path = CACHE / f"{name}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        lifetime = ttl(payload) if callable(ttl) else ttl
        if time.time() - path.stat().st_mtime < lifetime:
            return payload
    except (OSError, json.JSONDecodeError):
        pass
    payload = producer()
    write_cache(path, json.dumps(payload).encode("utf-8"))
    return payload


def cached_bytes(name: str, producer: Callable[[], bytes], ttl: int = FINISHED_TTL) -> bytes:
    path = CACHE / name
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return path.read_bytes()
    payload = producer()
    write_cache(path, payload)
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
    key = safe_key(match_id)
    lifetime = lambda payload: FINISHED_TTL if oc.match_finished(payload) else EVENT_TTL
    events = cached_json(f"events-{key}", lambda: oc.match_events(match_id), lifetime)
    try:
        lineups = cached_json(f"lineups-{key}", lambda: oc.match_lineups(match_id), lifetime)
    except RuntimeError:
        lineups = {}  # Event names still allow maps when optional lineups are unavailable.
    return events, lineups


def local_points(match_id: str, player_name: str, map_name: str):
    """Fallback: a previously collected file for this match, if we have one."""
    for path in DATA.glob(f"*_{safe_key(match_id)}_{map_name}.json"):
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
            if x is None and point.get("x") is not None and point.get("y") is not None:
                x, y = point["x"] * PITCH_LENGTH_M / 100, point["y"] * PITCH_WIDTH_M / 100
            if x is None or y is None:
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

def validate_grid(grid: str) -> tuple[int, int]:
    try:
        return ph.parse_grid(grid)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def render_webp(xs: np.ndarray, ys: np.ndarray, grid: str, sigma: float) -> bytes:
    """A 3D surface of the same field: colour *and* height are events per cell."""
    nx, ny = ph.parse_grid(grid)

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

# Extra browser origin to accept for CORS, e.g. a Tailscale address when testing
# from another device: TERRACE_ORIGIN=100.x.y.z ./run.sh
EXTRA_ORIGIN = os.environ.get("TERRACE_ORIGIN", "").strip()
ORIGIN_HOSTS = r"localhost|127\.0\.0\.1|192\.168\.\d+\.\d+"
if EXTRA_ORIGIN:
    ORIGIN_HOSTS += "|" + re.escape(EXTRA_ORIGIN)

app = FastAPI(title="Terrace", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=500)   # html/css/json, not webp
app.add_middleware(
    CORSMiddleware,
    # Only needed for `astro dev`; the built site is served same-origin below.
    allow_origin_regex=rf"http://({ORIGIN_HOSTS})(:\d+)?",
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
    body = json.dumps([{"slug": slug, "label": label} for slug, label in COMPETITIONS.items()])
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
    search: str = "",
) -> list[dict]:
    played = [f for f in fixtures(competition) if oc.fixture_started(f)]
    if team:
        needle = team.lower()
        played = [
            f for f in played
            if needle in (f["home"] or "").lower() or needle in (f["away"] or "").lower()
        ]
    if search.strip():
        needle = search.strip().lower()
        played = [f for f in played if needle in f"{f['home']} {f['away']} {f['date']}".lower()]
    return list(reversed(played[-limit:]))


@app.get("/api/players")
def players(match: str = Query(...), map: Literal["touch", "shot", "pass"] = "touch") -> dict:
    """Everyone in the match, sorted by their total for the selected stat."""
    events, lineups = match_payloads(match)
    rows = oc.match_players(events, lineups)

    counts: dict[str, int] = {}
    for event in (events.get("liveData") or {}).get("event", []) or []:
        player_id = event.get("playerId")
        type_id = event.get("typeId")
        if map == "shot" and type_id not in SHOT_TYPES:
            continue
        if map == "pass" and type_id not in PASS_TYPES:
            continue
        if map == "touch" and type_id in oc.NON_LOCATION_TYPES:
            continue
        if player_id:
            counts[player_id] = counts.get(player_id, 0) + 1

    for row in rows:
        row["events"] = counts.get(row["player_id"], 0)
    rows.sort(key=lambda row: -row["events"])
    return {"match": match, "players": rows}


@app.get("/api/geometry")
def geometry(
    match: str = Query(...),
    player: str = Query(...),
    map: Literal["touch", "shot", "pass"] = "touch",
    grid: str = "16x10",
    sigma: float = Query(3.5, ge=0, le=20),
) -> Response:
    """The surface as numbers, for the page to draw and spin itself.

    Same pipeline as /api/render, but it returns the binned field instead of an
    image: a few hundred floats is smaller than a picture and lets the browser
    rotate it. Cached on disk like everything else.
    """
    nx, ny = validate_grid(grid)
    key = render_key(match, player, map, grid, sigma)

    def build() -> dict:
        events, lineups = match_payloads(match)
        resolved = oc.resolve_player(events, lineups, player)
        xs, ys, total = points_for(events, resolved, map)
        if xs.size == 0:
            raise HTTPException(404, f"no {map} locations for {player} in that match")

        counts, x_edges, y_edges = np.histogram2d(
            xs, ys, bins=(nx, ny),
            range=((0.0, PITCH_LENGTH_M), (0.0, PITCH_WIDTH_M)),
        )
        cell_x, cell_y = PITCH_LENGTH_M / nx, PITCH_WIDTH_M / ny
        surface = ph.smooth_grid(counts, sigma / cell_y, sigma / cell_x)
        return {
            "finished": oc.match_finished(events),
            "status": ((events.get("liveData") or {}).get("matchDetails") or {}).get("matchStatus"),
            "shape": [nx, ny],
            "x": [round(float(v), 2) for v in (x_edges[:-1] + x_edges[1:]) / 2],
            "y": [round(float(v), 2) for v in (y_edges[:-1] + y_edges[1:]) / 2],
            "z": [[round(float(v), 4) for v in row] for row in surface],
            "z_max": round(float(surface.max()), 4),
            "pitch": [PITCH_LENGTH_M, PITCH_WIDTH_M],
            "meta": {
                "player": resolved.get("name", player), "map": map,
                "events": int(xs.size), "total": int(total),
                "grid": grid, "sigma": sigma, "match_id": match,
            },
        }

    try:
        payload = cached_json(f"geo-{key}", build,
                              ttl=lambda value: FINISHED_TTL if value.get("finished") else EVENT_TTL)
    except HTTPException:
        raise
    except RuntimeError as error:
        raise HTTPException(404, str(error)) from error
    body = json.dumps(payload)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Cache-Control": CACHE_CONTROL if payload.get("finished") else f"public, max-age={EVENT_TTL}",
            "ETag": f'"{hashlib.sha1(body.encode()).hexdigest()}"',
        },
    )


@app.get("/api/render")
def render(
    request: Request,
    match: str = Query(..., description="Opta match id"),
    player: str = Query(..., description="player name or Opta player id"),
    map: Literal["touch", "shot", "pass"] = "touch",
    grid: str = "16x10",
    sigma: float = Query(3.5, ge=0, le=20),
) -> Response:
    validate_grid(grid)
    key = render_key(match, player, map, grid, sigma)

    def build() -> bytes:
        finished = False
        try:
            events, lineups = match_payloads(match)
            resolved = oc.resolve_player(events, lineups, player)
            xs, ys, total = points_for(events, resolved, map)
            finished = oc.match_finished(events)
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
            "finished": finished,
            "match_id": match, "player": resolved.get("name", player), "map": map,
            "events": int(xs.size), "total": int(total), "grid": grid, "sigma": sigma,
        }
        image = render_webp(xs, ys, grid, sigma)
        write_cache(CACHE / f"meta-{key}.json", json.dumps(meta).encode())
        return image

    def lifetime() -> int:
        try:
            meta = json.loads((CACHE / f"meta-{key}.json").read_text())
            return FINISHED_TTL if meta.get("finished") else EVENT_TTL
        except (OSError, json.JSONDecodeError):
            return EVENT_TTL

    path = CACHE / f"render-{key}.webp"
    ttl = lifetime()
    was_cached = path.exists() and time.time() - path.stat().st_mtime < ttl
    image = cached_bytes(f"render-{key}.webp", build, ttl=ttl)
    etag = f'"{hashlib.sha1(image).hexdigest()}"'
    cache_control = CACHE_CONTROL if lifetime() == FINISHED_TTL else f"public, max-age={EVENT_TTL}"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache_control})

    headers = {
        "Cache-Control": cache_control,
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
