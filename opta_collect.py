#!/usr/bin/env python3
"""Collect Opta player maps (shot / touch / pass) and save clean JSON to data/.

Where the data comes from
-------------------------
Two anonymous, public Opta endpoints power the Opta Analyst match-centre widgets
on theanalyst.com:

  * theanalyst.com WordPress "sdapi"  -> competitions, calendars (no key needed)
  * api.performfeeds.com "soccerdata" -> match events, with coordinates

The second one is the same feed the on-page Opta widgets (`<opta-widget ...>`
from secure.widget.cloud.opta.net) read. It needs no API key, but it *does*
reject requests that do not carry a `Referer: https://theanalyst.com/` header.

Coordinates
-----------
Everything is normalised onto Opta's 0-100 pitch: `x` runs from the team's own
goal line (0) to the opponent's goal line (100) and `y` runs across the pitch,
because Opta stores each event in the attacking team's frame of reference. Some
throw-in events sit slightly outside the pitch; those are kept and flagged with
`on_pitch: false` instead of being silently dropped. Metre values are derived
from the 105 x 68 m pitch and are rounded to 2 dp. Unknown qualifier blobs are
discarded; only the location-bearing / meaning-bearing ones are kept (pass the
`--raw-qualifiers` flag if you want every raw qualifier id and value as well).

Usage
-----
    # which matches are available for a competition (most recent first)
    python opta_collect.py matches --competition premier-league --team Liverpool
    python opta_collect.py matches --competition premier-league --limit 8

    # who played in a match (ids, positions, touches)
    python opta_collect.py players --match 4h8foczvol1zhy7usmy11maz8

    # the maps: shot map, touch map, pass map for one player
    python opta_collect.py maps --player "Salah" --match 4h8foczvol1zhy7usmy11maz8
    python opta_collect.py maps --player "Salah" --match 4h8f... --maps shot,pass
    python opta_collect.py maps --player "Salah" --competition premier-league \
        --team Liverpool            # picks the most recent finished match

Output lands in `data/` as `data/{player}_{match_id}_{map}.json`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

THEANALYST_API = "https://theanalyst.com/wp-json/sdapi/v1"
OPTA_API = "https://api.performfeeds.com/soccerdata"

# Outlet key the Opta Analyst widget build uses. It is public and anonymous; it
# lives in the widget subscription file at
# https://secure.widget.cloud.opta.net/subscriptions/v3_<hash>.json
OUTLET = "1mjq6w6ezkxe611ykkj8rgz7f1"

# api.performfeeds.com answers 403 (errorCode 10300) without this.
REFERER = "https://theanalyst.com/"
USER_AGENT = "Mozilla/5.0 (compatible; terrace/1.0)"

PITCH_LENGTH_M = 105.0
PITCH_WIDTH_M = 68.0
DATA_DIR = Path("data")

MAPS = ("shot", "touch", "pass")

# Standard Opta event type ids. Ids outside this table are reported as
# "type_<id>" rather than guessed at.
EVENT_TYPES = {
    1: "pass", 2: "offside_pass", 3: "take_on", 4: "foul", 5: "out",
    6: "corner_awarded", 7: "tackle", 8: "interception", 10: "save",
    11: "claim", 12: "clearance", 13: "miss", 14: "attempt_saved",
    15: "attempt_blocked", 16: "goal", 17: "card", 18: "player_off",
    19: "player_on", 20: "player_retired", 21: "player_returns",
    22: "player_becomes_goalkeeper", 23: "goalkeeper_becomes_player",
    24: "condition_change", 25: "official_change", 27: "start_delay",
    28: "end_delay", 30: "end_period", 32: "start_period", 33: "end_match",
    34: "team_set_up", 35: "player_changed_position",
    36: "player_changed_jersey_number", 37: "collection_end",
    38: "temp_not_active", 39: "temp_active", 40: "formation_change",
    41: "punch", 42: "good_skill", 43: "deleted_event", 44: "aerial",
    45: "challenge", 46: "recovery", 47: "dive", 48: "dive_save",
    49: "ball_recovery", 50: "dispossessed", 51: "error",
    52: "keeper_pick_up", 53: "cross_not_claimed", 54: "smother",
    55: "offside_provoked", 56: "shield_ball_opp", 57: "foul_throw_in",
    58: "penalty_faced", 59: "keeper_sweeper", 60: "chance_missed",
    61: "ball_touch", 63: "temp_save", 64: "resume",
    65: "contentious_referee_decision", 67: "50_50", 68: "referee_drop_ball",
    70: "injury_time", 74: "blocked_pass", 75: "delayed_start",
    76: "early_end", 79: "coverage_interruption", 80: "drop_of_ball",
    81: "obstacle",
}

# Events that carry no meaningful location for a player (periods, lineups,
# formation changes, official changes, deleted events ...). Dropped from maps.
NON_LOCATION_TYPES = {
    18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
    37, 38, 39, 40, 43, 65, 70, 75, 76, 79,
}

SHOT_TYPES = {13: "off_target", 14: "saved", 15: "blocked", 16: "goal"}

# Qualifier ids used below, with the meaning each carries in this feed.
Q_PASS_END_X, Q_PASS_END_Y = 140, 141
Q_PASS_LENGTH = 212
Q_PASS_DIRECTION = 56
Q_PASS_END_TYPE = 210
Q_GOAL_MOUTH_Y, Q_GOAL_MOUTH_Z = 102, 103
Q_SHOT_END_X, Q_SHOT_END_Y = 230, 231
Q_BODY_PART = {15: "head", 20: "right_foot", 21: "other", 72: "left_foot"}
Q_SITUATION = {
    22: "regular_play", 23: "fast_break", 24: "set_piece", 25: "from_corner",
    26: "free_kick", 27: "penalty", 28: "own_goal",
}
Q_ASSISTED = 29


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _get(url: str, referer: str | None = None, retries: int = 3) -> bytes:
    """GET a URL and return the body, retrying transient failures."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if referer:
        headers["Referer"] = referer
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()[:200].decode("utf-8", "replace")
            if exc.code == 403 and not referer:
                raise RuntimeError(
                    f"403 from {url} — the Opta feed needs "
                    f"'Referer: {REFERER}'. Response: {body}"
                ) from exc
            last_error = RuntimeError(f"HTTP {exc.code} from {url}: {body}")
            if exc.code in (400, 401, 403, 404):
                break
        except Exception as exc:  # noqa: BLE001 - network level, retry below
            last_error = exc
        time.sleep(1 + attempt)
    raise RuntimeError(f"request failed: {url}") from last_error


def fetch_json(url: str, referer: str | None = None) -> dict:
    raw = _get(url, referer=referer)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-JSON response from {url}") from exc


# --------------------------------------------------------------------------- #
# theanalyst.com (competitions and fixtures)
# --------------------------------------------------------------------------- #

def resolve_tmcl(competition: str) -> str:
    """Turn a competition slug (or a raw tmcl id) into a tournament calendar id."""
    if re.fullmatch(r"[a-z0-9]{20,}", competition):
        return competition
    page = _get(f"https://theanalyst.com/competition/{competition}").decode(
        "utf-8", "replace"
    )
    match = re.search(r'"tmcl"\s*:\s*"([a-z0-9]+)"', page)
    if not match:
        raise RuntimeError(
            f"could not find a tournament calendar id on the {competition} page; "
            "pass --tmcl explicitly"
        )
    return match.group(1)


def list_matches(tmcl: str) -> list[dict]:
    """Every fixture in a tournament calendar, cleaned and date-sorted."""
    url = (
        f"{THEANALYST_API}/soccerdata/tournamentschedule"
        f"?tmcl={urllib.parse.quote(tmcl)}"
    )
    payload = fetch_json(url)
    fixtures = []
    for day in payload.get("matchDate", []):
        for match in day.get("match", []):
            fixtures.append(
                {
                    "match_id": match.get("id"),
                    "date": (match.get("date") or "").rstrip("Z"),
                    "time": (match.get("time") or "").rstrip("Z"),
                    "status": match.get("matchStatus"),
                    "home": match.get("homeContestantName"),
                    "away": match.get("awayContestantName"),
                    "competition": (payload.get("competition") or {}).get("name"),
                    "season": (payload.get("tournamentCalendar") or {}).get("name"),
                }
            )
    fixtures.sort(key=lambda f: (f["date"], f["time"]))
    return fixtures


def match_finished(payload: dict) -> bool:
    return ((payload.get("liveData") or {}).get("matchDetails") or {}).get("matchStatus") == "Played"


def fixture_started(fixture: dict) -> bool:
    if fixture.get("status") in ("Postponed", "Cancelled", "Canceled"):
        return False
    if fixture.get("status") == "Played":
        return True
    try:
        kickoff = datetime.fromisoformat(f"{fixture['date']}T{fixture.get('time') or '23:59:59'}".replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return False
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return kickoff <= datetime.now(timezone.utc)


def pick_match(tmcl: str, team: str | None, on_or_before: str | None) -> dict:
    """Most recent finished fixture involving `team`."""
    limit = on_or_before or datetime.now(timezone.utc).date().isoformat()
    fixtures = [
        f for f in list_matches(tmcl) if f["date"] and f["date"] <= limit and fixture_started(f)
    ]
    if team:
        needle = team.strip().lower()
        fixtures = [
            f
            for f in fixtures
            if needle in (f["home"] or "").lower() or needle in (f["away"] or "").lower()
        ]
    if not fixtures:
        raise RuntimeError(
            f"no match found for team={team!r} on or before {limit}; "
            "run the `matches` command to see what is available"
        )
    for fixture in reversed(fixtures):
        if fixture.get("status") == "Played" or match_finished(match_events(fixture["match_id"])):
            return fixture
    raise RuntimeError("no finished match found for those filters")


# --------------------------------------------------------------------------- #
# api.performfeeds.com (the Opta feeds)
# --------------------------------------------------------------------------- #

def opta_feed(feed: str, path: str, **params) -> dict:
    """Fetch one soccerdata feed, e.g. opta_feed('matchevent', f'{OUTLET}/{match_id}')."""
    query = {"_rt": "c", "_lcl": "en", "_fmt": "json", "sps": "widgets"}
    query.update({k: v for k, v in params.items() if v is not None})
    url = f"{OPTA_API}/{feed}/{path}?{urllib.parse.urlencode(query)}"
    return fetch_json(url, referer=REFERER)


def match_events(match_id: str) -> dict:
    return opta_feed("matchevent", f"{OUTLET}/{match_id}")


def match_lineups(match_id: str) -> dict:
    return opta_feed("match", f"{OUTLET}/{match_id}", live="yes", lineups="yes")


def pass_matrix(match_id: str) -> dict:
    return opta_feed("passmatrix", f"{OUTLET}/{match_id}")


# --------------------------------------------------------------------------- #
# Cleaning helpers
# --------------------------------------------------------------------------- #

def _round(value: float) -> float:
    return round(float(value), 2)


def location(x, y) -> dict:
    """Opta 0-100 coordinates plus metres, and whether they sit on the pitch."""
    x, y = float(x), float(y)
    on_pitch = 0.0 <= x <= 100.0 and 0.0 <= y <= 100.0
    return {
        "x": _round(x),
        "y": _round(y),
        "x_m": _round(x * PITCH_LENGTH_M / 100.0),
        "y_m": _round(y * PITCH_WIDTH_M / 100.0),
        "on_pitch": on_pitch,
    }


def qualifiers(event: dict) -> dict[int, str]:
    return {q["qualifierId"]: q.get("value") for q in event.get("qualifier", []) or []}


def body_part(quals: dict[int, str]) -> str | None:
    for qid, name in Q_BODY_PART.items():
        if qid in quals:
            return name
    return None


def situation(quals: dict[int, str]) -> str | None:
    for qid, name in Q_SITUATION.items():
        if qid in quals:
            return name
    return None


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _raw_qualifiers(quals: dict[int, str]) -> dict[str, str] | None:
    return {str(k): v for k, v in sorted(quals.items())} if quals else None


def match_context(match_info: dict, events_payload: dict) -> dict:
    contestants = {
        c["id"]: c.get("name") for c in match_info.get("contestant", []) or []
    }
    home = next(
        (c["name"] for c in match_info.get("contestant", []) or []
         if c.get("position") == "home"),
        None,
    )
    away = next(
        (c["name"] for c in match_info.get("contestant", []) or []
         if c.get("position") == "away"),
        None,
    )
    details = (events_payload.get("liveData") or {}).get("matchDetails") or {}
    return {
        "match_id": match_info.get("id"),
        "description": match_info.get("description"),
        "date": (match_info.get("date") or "").rstrip("Z"),
        "time": (match_info.get("time") or "").rstrip("Z"),
        "competition": (match_info.get("competition") or {}).get("name"),
        "season": (match_info.get("tournamentCalendar") or {}).get("name"),
        "home_team": home,
        "away_team": away,
        "teams": contestants,
        "score": details.get("scores"),
        "status": details.get("matchStatus"),
    }


# --------------------------------------------------------------------------- #
# Player resolution
# --------------------------------------------------------------------------- #

def lineup_players(lineups_payload: dict) -> list[dict]:
    """Flatten the `match` feed lineups into one row per player."""
    match_info = lineups_payload.get("matchInfo") or {}
    teams = {c["id"]: c.get("name") for c in match_info.get("contestant", []) or []}
    rows = []
    for side in (lineups_payload.get("liveData") or {}).get("lineUp", []) or []:
        for player in side.get("player", []) or []:
            rows.append(
                {
                    "player_id": player.get("playerId"),
                    "name": player.get("matchName")
                    or f"{player.get('firstName', '')} {player.get('lastName', '')}".strip(),
                    "full_name": " ".join(
                        p for p in (player.get("firstName"), player.get("lastName")) if p
                    ),
                    "team_id": side.get("contestantId"),
                    "team": teams.get(side.get("contestantId")),
                    "position": player.get("position"),
                    "position_side": player.get("positionSide"),
                    "shirt_number": player.get("shirtNumber"),
                }
            )
    return rows


def match_players(events_payload: dict, lineups_payload: dict | None) -> list[dict]:
    rows = lineup_players(lineups_payload or {})
    seen = {row["player_id"] for row in rows}
    teams = {team["id"]: team.get("name") for team in (events_payload.get("matchInfo") or {}).get("contestant", []) or []}
    for event in (events_payload.get("liveData") or {}).get("event", []) or []:
        player_id = event.get("playerId")
        if not player_id or player_id in seen:
            continue
        seen.add(player_id)
        name = event.get("playerName") or player_id
        rows.append({
            "player_id": player_id, "name": name, "full_name": name,
            "team_id": event.get("contestantId"), "team": teams.get(event.get("contestantId")),
            "position": None, "position_side": None, "shirt_number": None,
        })
    return rows


def resolve_player(events_payload: dict, lineups_payload: dict | None, query: str) -> dict:
    """Find one player by id, or by name/id fragment from the match lineups."""
    rows = match_players(events_payload, lineups_payload)
    if re.fullmatch(r"[a-z0-9]{20,}", query):
        for row in rows:
            if row["player_id"] == query:
                return row
        raise RuntimeError(f"no player with id {query} in this match")

    needle = query.strip().lower()
    matches = [
        row
        for row in rows
        if needle in (row["name"] or "").lower()
        or needle in (row["full_name"] or "").lower()
    ]
    if not matches:
        raise RuntimeError(f"no player matching {query!r} in this match")
    if len(matches) > 1:
        names = ", ".join(sorted({f"{m['name']} ({m['team']})" for m in matches}))
        raise RuntimeError(
            f"{query!r} is ambiguous — {len(matches)} players match: {names}"
        )
    return matches[0]


# --------------------------------------------------------------------------- #
# Maps
# --------------------------------------------------------------------------- #

def _base_point(event: dict) -> dict:
    point = {
        "event_id": event.get("eventId"),
        "minute": event.get("timeMin"),
        "second": event.get("timeSec"),
        "team_id": event.get("contestantId"),
        "type_id": event.get("typeId"),
    }
    point.update(location(event["x"], event["y"]))
    return point


def shot_map(events: list[dict], player: dict, raw: bool = False) -> dict:
    """Shots only: one point per attempt, in Opta coordinates."""
    points = []
    for event in events:
        type_id = event.get("typeId")
        if type_id not in SHOT_TYPES:
            continue
        if event.get("playerId") != player["player_id"]:
            continue
        if event.get("x") is None or event.get("y") is None:
            continue
        quals = qualifiers(event)
        point = _base_point(event)
        point["type"] = EVENT_TYPES.get(type_id, f"type_{type_id}")
        point["outcome"] = SHOT_TYPES[type_id]
        point["body_part"] = body_part(quals)
        point["situation"] = situation(quals)
        if Q_ASSISTED in quals:
            point["assisted"] = True
        goal_mouth_y = _as_float(quals.get(Q_GOAL_MOUTH_Y))
        goal_mouth_z = _as_float(quals.get(Q_GOAL_MOUTH_Z))
        if goal_mouth_y is not None:
            point["goal_mouth_y"] = _round(goal_mouth_y)
        if goal_mouth_z is not None:
            point["goal_mouth_z"] = _round(goal_mouth_z)
        end_x = _as_float(quals.get(Q_SHOT_END_X))
        end_y = _as_float(quals.get(Q_SHOT_END_Y))
        if end_x is not None and end_y is not None:
            point["end_x"], point["end_y"] = _round(end_x), _round(end_y)
        if raw:
            point["raw_qualifiers"] = _raw_qualifiers(quals)
        points.append(point)

    summary = {
        "shots": len(points),
        "goals": sum(1 for p in points if p["outcome"] == "goal"),
        "on_target": sum(1 for p in points if p["outcome"] in ("goal", "saved")),
        "average_x": _round(sum(p["x"] for p in points) / len(points)) if points else None,
        "average_y": _round(sum(p["y"] for p in points) / len(points)) if points else None,
    }
    return {"points": points, "summary": summary}


def touch_map(events: list[dict], player: dict, raw: bool = False) -> dict:
    """Every located on-ball action by the player — the touch map."""
    points = []
    for event in events:
        type_id = event.get("typeId")
        if type_id in NON_LOCATION_TYPES or event.get("playerId") != player["player_id"]:
            continue
        if event.get("x") is None or event.get("y") is None:
            continue
        quals = qualifiers(event)
        point = _base_point(event)
        point["type"] = EVENT_TYPES.get(type_id, f"type_{type_id}")
        point["outcome"] = event.get("outcome")
        if type_id in (1, 2):
            end_x = _as_float(quals.get(Q_PASS_END_X))
            end_y = _as_float(quals.get(Q_PASS_END_Y))
            if end_x is not None and end_y is not None:
                point["end_x"], point["end_y"] = _round(end_x), _round(end_y)
        if raw:
            point["raw_qualifiers"] = _raw_qualifiers(quals)
        points.append(point)

    by_type: dict[str, int] = {}
    for point in points:
        by_type[point["type"]] = by_type.get(point["type"], 0) + 1
    summary = {
        "touches": len(points),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "off_pitch": sum(1 for p in points if not p["on_pitch"]),
        "average_x": _round(sum(p["x"] for p in points) / len(points)) if points else None,
        "average_y": _round(sum(p["y"] for p in points) / len(points)) if points else None,
    }
    return {"points": points, "summary": summary}


def pass_map(
    events: list[dict], player: dict, matrix: dict | None = None, raw: bool = False
) -> dict:
    """Passes with start *and* end coordinates, plus the team pass network."""
    points = []
    for event in events:
        type_id = event.get("typeId")
        if type_id not in (1, 2) or event.get("playerId") != player["player_id"]:
            continue
        if event.get("x") is None or event.get("y") is None:
            continue
        quals = qualifiers(event)
        end_x = _as_float(quals.get(Q_PASS_END_X))
        end_y = _as_float(quals.get(Q_PASS_END_Y))
        if end_x is None or end_y is None:
            continue
        point = _base_point(event)
        point["type"] = EVENT_TYPES.get(type_id, f"type_{type_id}")
        point["end_x"], point["end_y"] = _round(end_x), _round(end_y)
        point["end_x_m"] = _round(end_x * PITCH_LENGTH_M / 100.0)
        point["end_y_m"] = _round(end_y * PITCH_WIDTH_M / 100.0)
        point["complete"] = event.get("outcome") == 1
        point["direction"] = quals.get(Q_PASS_DIRECTION)
        length = _as_float(quals.get(Q_PASS_LENGTH))
        if length is None:
            dx = (end_x - float(event.get("x", 0.0))) * PITCH_LENGTH_M / 100.0
            dy = (end_y - float(event.get("y", 0.0))) * PITCH_WIDTH_M / 100.0
            length = (dx * dx + dy * dy) ** 0.5
        point["length_m"] = _round(length)
        end_type = quals.get(Q_PASS_END_TYPE)
        if end_type is not None:
            point["pass_end_type_id"] = int(end_type)
        if raw:
            point["raw_qualifiers"] = _raw_qualifiers(quals)
        points.append(point)

    complete = sum(1 for p in points if p["complete"])
    summary = {
        "passes": len(points),
        "complete": complete,
        "incomplete": len(points) - complete,
        "accuracy_pct": _round(100.0 * complete / len(points)) if points else None,
        "average_length_m": (
            _round(sum(p["length_m"] for p in points) / len(points)) if points else None
        ),
        "average_x": _round(sum(p["x"] for p in points) / len(points)) if points else None,
        "average_y": _round(sum(p["y"] for p in points) / len(points)) if points else None,
    }
    return {"points": points, "summary": summary, "network": team_pass_network(matrix, player)}


def team_pass_network(matrix: dict | None, player: dict) -> dict | None:
    """Average positions and pass volumes for the player's team (pass matrix feed)."""
    if not matrix:
        return None
    side = next(
        (
            s
            for s in (matrix.get("liveData") or {}).get("lineUp", []) or []
            if s.get("contestantId") == player.get("team_id")
        ),
        None,
    )
    if not side:
        return None

    nodes = []
    for entry in side.get("player", []) or []:
        x = _as_float(entry.get("x"))
        y = _as_float(entry.get("y"))
        node = {
            "player_id": entry.get("playerId"),
            "name": entry.get("matchName"),
            "shirt_number": entry.get("shirtNumber"),
        }
        if x is not None and y is not None:
            node.update(location(x, y))
        nodes.append(node)

    edges = []
    for entry in side.get("player", []) or []:
        for target in entry.get("playerPass", []) or []:
            count = int(target.get("value") or 0)
            if count <= 0:
                continue
            edges.append(
                {
                    "from": entry.get("playerId"),
                    "from_name": entry.get("matchName"),
                    "to": target.get("playerId"),
                    "to_name": target.get("knownName")
                    or target.get("shortLastName")
                    or target.get("lastName"),
                    "passes": count,
                }
            )
    return {"team_id": player.get("team_id"), "nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def player_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "player").lower()).strip("_")
    return slug or "player"


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def build_document(
    map_name: str,
    payload: dict,
    context: dict,
    player: dict,
    source_feeds: list[str],
) -> dict:
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "map": map_name,
            "player": player,
            "match": context,
            "source": "api.performfeeds.com/soccerdata (Opta Analyst widgets)",
            "source_feeds": source_feeds,
            "coordinate_system": {
                "units": "opta_0_100",
                "x": "0 = own goal line, 100 = opponent goal line (attacking team's frame)",
                "y": "0-100 across the pitch",
                "pitch_m": [PITCH_LENGTH_M, PITCH_WIDTH_M],
                "metres": "derived: x_m = x * 1.05, y_m = y * 0.68",
                "off_pitch_events": "kept, flagged with on_pitch: false",
            },
            "count": len(payload["points"]),
        },
        **payload,
    }


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_matches(args: argparse.Namespace) -> int:
    tmcl = args.tmcl or resolve_tmcl(args.competition)
    fixtures = list_matches(tmcl)
    if args.team:
        needle = args.team.lower()
        fixtures = [
            f
            for f in fixtures
            if needle in (f["home"] or "").lower() or needle in (f["away"] or "").lower()
        ]
    if args.before:
        fixtures = [f for f in fixtures if f["date"] <= args.before]
    for fixture in reversed(fixtures[-args.limit :]):
        print(
            f"{fixture['date']} {fixture['time']}  {fixture['match_id']}  "
            f"{fixture['home']} vs {fixture['away']}"
        )
    print(f"\n{len(fixtures)} match(es) in {tmcl} (newest first)")
    return 0


def cmd_players(args: argparse.Namespace) -> int:
    events_payload = match_events(args.match)
    lineups_payload = match_lineups(args.match)
    events = (events_payload.get("liveData") or {}).get("event", []) or []
    touches: dict[str, int] = {}
    for event in events:
        if event.get("playerId") and event.get("typeId") not in NON_LOCATION_TYPES:
            touches[event["playerId"]] = touches.get(event["playerId"], 0) + 1

    print(f"{(events_payload.get('matchInfo') or {}).get('description')}\n")
    for row in sorted(lineup_players(lineups_payload), key=lambda r: (r["team"] or "", r["shirt_number"] or 0)):
        print(
            f"{row['player_id']}  {str(row['shirt_number'] or ''):>2}  "
            f"{(row['name'] or ''):24.24s} {(row['position'] or ''):12.12s} "
            f"{row['team'] or '':14.14s} events={touches.get(row['player_id'], 0)}"
        )
    return 0


def cmd_maps(args: argparse.Namespace) -> int:
    match_id = args.match
    context_hint: dict = {}
    if not match_id:
        if not args.competition:
            raise RuntimeError("pass --match, or --competition (with --team) to pick one")
        tmcl = args.tmcl or resolve_tmcl(args.competition)
        fixture = pick_match(tmcl, args.team, args.before)
        match_id = fixture["match_id"]
        context_hint = fixture
        print(f"using match {fixture['date']} {fixture['home']} vs {fixture['away']} ({match_id})")

    events_payload = match_events(match_id)
    match_info = events_payload.get("matchInfo") or {}
    events = (events_payload.get("liveData") or {}).get("event", []) or []
    context = match_context(match_info, events_payload)
    if context_hint.get("competition") and not context.get("competition"):
        context["competition"] = context_hint["competition"]

    lineups_payload = match_lineups(match_id)
    player = resolve_player(events_payload, lineups_payload, args.player)
    print(f"player: {player['name']} ({player['player_id']}) — {player['team']}")

    requested = [m.strip() for m in args.maps.split(",") if m.strip()]
    unknown = [m for m in requested if m not in MAPS]
    if unknown:
        raise RuntimeError(f"unknown map(s): {', '.join(unknown)} (choose from {', '.join(MAPS)})")

    matrix = pass_matrix(match_id) if "pass" in requested else None
    built = {
        "shot": (shot_map, [f"matchevent/{match_id}"]),
        "touch": (touch_map, [f"matchevent/{match_id}"]),
        "pass": (pass_map, [f"matchevent/{match_id}", f"passmatrix/{match_id}"]),
    }
    out_dir = Path(args.out)
    for map_name in requested:
        builder, feeds = built[map_name]
        payload = (
            builder(events, player, matrix=matrix, raw=args.raw_qualifiers)
            if map_name == "pass"
            else builder(events, player, raw=args.raw_qualifiers)
        )
        document = build_document(map_name, payload, context, player, feeds)
        path = out_dir / f"{player_slug(player['name'])}_{match_id}_{map_name}.json"
        write_json(path, document)
        print(f"saved {path}  ({len(payload['points'])} points)")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect Opta shot / touch / pass maps as clean JSON in data/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    matches = sub.add_parser("matches", help="list fixtures for a competition")
    matches.add_argument("--competition", required=True, help="e.g. premier-league")
    matches.add_argument("--tmcl", help="tournament calendar id (skips the slug lookup)")
    matches.add_argument("--team", help="filter to fixtures involving this team")
    matches.add_argument("--before", help="only matches on or before YYYY-MM-DD")
    matches.add_argument("--limit", type=int, default=10, help="how many to print")
    matches.set_defaults(func=cmd_matches)

    players = sub.add_parser("players", help="list the players in a match")
    players.add_argument("--match", required=True, help="Opta fixture id")
    players.set_defaults(func=cmd_players)

    maps = sub.add_parser("maps", help="collect shot / touch / pass maps for a player")
    maps.add_argument("--player", required=True, help="player name or Opta player id")
    maps.add_argument("--match", help="Opta fixture id")
    maps.add_argument("--competition", help="competition slug, to pick the match for you")
    maps.add_argument("--tmcl", help="tournament calendar id")
    maps.add_argument("--team", help="team used when picking a match")
    maps.add_argument("--before", help="only matches on or before YYYY-MM-DD")
    maps.add_argument("--maps", default="shot,touch,pass", help="comma list of maps")
    maps.add_argument("--out", default=str(DATA_DIR), help="output directory (default data/)")
    maps.add_argument("--raw-qualifiers", action="store_true", help="keep every raw qualifier")
    maps.set_defaults(func=cmd_maps)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
