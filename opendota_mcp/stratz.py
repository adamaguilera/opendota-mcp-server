"""STRATZ GraphQL client: true role/position (1-5) and rank-bracket-segmented benchmarks.

OpenDota can't do either well — its ``lane_role`` is parse-biased and can't split a pos1 carry
from a pos5 support, and its ``/benchmarks`` pool every skill bracket together. STRATZ exposes a
true per-match ``position`` and per-bracket hero stat averages, which is what grounds "is 240 last
hits low for a Guardian carry?". Surfaced through the ``get_role_summary`` / ``get_rank_benchmark``
MCP tools, which only register when ``STRATZ_API_TOKEN`` is set.

All values are inlined into the query from int/enum sources we control (account ids, hero ids, and
fixed position/bracket maps), so there is no injection surface.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Union

import httpx

from .config import STRATZ_API_TOKEN, STRATZ_GRAPHQL_URL, format_rank_tier

logger = logging.getLogger("opendota-server")

# STRATZ/Cloudflare can be slow on cold hero-stats aggregates; stay under feedbot's per-tool budget.
_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

# rank_tier tens digit (1=Herald .. 8=Immortal) -> STRATZ RankBracketBasicEnum bucket.
_TIER_TO_BRACKET = {1: "HERALD_GUARDIAN", 2: "HERALD_GUARDIAN",
                    3: "CRUSADER_ARCHON", 4: "CRUSADER_ARCHON",
                    5: "LEGEND_ANCIENT", 6: "LEGEND_ANCIENT",
                    7: "DIVINE_IMMORTAL", 8: "DIVINE_IMMORTAL"}

_BRACKET_ALIASES = {
    "herald": "HERALD_GUARDIAN", "guardian": "HERALD_GUARDIAN", "heraldguardian": "HERALD_GUARDIAN",
    "crusader": "CRUSADER_ARCHON", "archon": "CRUSADER_ARCHON", "crusaderarchon": "CRUSADER_ARCHON",
    "legend": "LEGEND_ANCIENT", "ancient": "LEGEND_ANCIENT", "legendancient": "LEGEND_ANCIENT",
    "divine": "DIVINE_IMMORTAL", "immortal": "DIVINE_IMMORTAL", "divineimmortal": "DIVINE_IMMORTAL",
}
_BRACKET_DISPLAY = {
    "HERALD_GUARDIAN": "Herald-Guardian", "CRUSADER_ARCHON": "Crusader-Archon",
    "LEGEND_ANCIENT": "Legend-Ancient", "DIVINE_IMMORTAL": "Divine-Immortal",
}

_HERO_MAP: Optional[Dict[str, int]] = None  # normalized hero name -> Valve hero id, fetched once


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _bracket_from_tier(rank_tier: Optional[int]) -> Optional[str]:
    if not rank_tier:
        return None
    return _TIER_TO_BRACKET.get(rank_tier // 10)


def _bracket_from_label(label: str) -> Optional[str]:
    return _BRACKET_ALIASES.get(_norm(label))


def _r(v: Any) -> Any:
    return round(v, 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


async def _gql(query: str) -> Dict[str, Any]:
    if not STRATZ_API_TOKEN:
        raise RuntimeError("STRATZ_API_TOKEN not configured")
    headers = {
        "Authorization": f"Bearer {STRATZ_API_TOKEN}",
        "User-Agent": "STRATZ_API",  # Cloudflare rejects requests with a default httpx UA
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(STRATZ_GRAPHQL_URL, json={"query": query}, headers=headers)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"STRATZ error: {payload['errors'][0].get('message')}")
    return payload.get("data") or {}


async def _ensure_heroes() -> Dict[str, int]:
    global _HERO_MAP
    if _HERO_MAP is None:
        data = await _gql("{ constants { heroes { id shortName displayName } } }")
        mapping: Dict[str, int] = {}
        for hero in (data.get("constants") or {}).get("heroes") or []:
            hid = hero.get("id")
            if hid is None:
                continue
            for name in (hero.get("displayName"), hero.get("shortName")):
                if name:
                    mapping[_norm(name)] = hid
        _HERO_MAP = mapping
    return _HERO_MAP


async def _hero_id(hero: Union[int, str]) -> Optional[int]:
    if isinstance(hero, int):
        return hero
    text = str(hero).strip()
    if text.isdigit():
        return int(text)
    return (await _ensure_heroes()).get(_norm(text))


async def role_summary(account_id: Union[int, str]) -> Dict[str, Any]:
    """Per-position (1-5) split for a player from STRATZ, with win rate, GPM/XPM, KDA and IMP."""
    try:
        aid = int(str(account_id).strip())
    except (TypeError, ValueError):
        return {"error": f"invalid account_id: {account_id!r}"}
    query = (
        f"{{ player(steamAccountId: {aid}) {{ matchCount "
        f"steamAccount {{ seasonRank isAnonymous }} "
        f"matchesGroupBy(request: {{ playerList: SINGLE, groupBy: POSITION }}) {{ "
        f"... on MatchGroupByPositionType {{ position matchCount winCount avgImp "
        f"avgGoldPerMinute avgExperiencePerMinute avgKDA avgKills avgDeaths avgAssists }} }} }} }}"
    )
    try:
        data = await _gql(query)
    except Exception as e:  # noqa: BLE001 — surface as a tool error, never crash the turn
        return {"error": f"STRATZ role_summary failed: {e}"}

    player = data.get("player") or {}
    account = player.get("steamAccount") or {}
    if account.get("isAnonymous"):
        return {"error": "player profile is anonymous on STRATZ — role/rank data is unavailable"}

    rank_tier = account.get("seasonRank")
    positions, sample = [], 0
    for row in player.get("matchesGroupBy") or []:
        pos = row.get("position") or ""
        if not pos.startswith("POSITION_"):
            continue
        games, wins = row.get("matchCount") or 0, row.get("winCount") or 0
        sample += games
        positions.append({
            "position": int(pos.split("_")[1]),
            "games": games,
            "winrate": round(wins / games * 100, 1) if games else None,
            "avg_imp": row.get("avgImp"),
            "gpm": row.get("avgGoldPerMinute"),
            "xpm": row.get("avgExperiencePerMinute"),
            "kda": _r(row.get("avgKDA")),
            "kills": _r(row.get("avgKills")),
            "deaths": _r(row.get("avgDeaths")),
            "assists": _r(row.get("avgAssists")),
        })
    positions.sort(key=lambda p: p["games"], reverse=True)
    return {
        "account_id": str(aid),
        "rank": format_rank_tier(rank_tier),
        "rank_tier": rank_tier,
        "total_matches": player.get("matchCount"),
        "positioned_sample": sample,
        "positions": positions,
        "note": ("position 1=safelane carry .. 5=hard support. STRATZ only sets position on parsed "
                 "matches, so positioned_sample is how many games this split covers — if it's small, "
                 "treat it as indicative and confirm the role from the hero pool. avg_imp is STRATZ's "
                 "performance score (~0 average, negative = below average for that role)."),
    }


async def rank_benchmark(
    hero: Union[int, str],
    position: int,
    account_id: Optional[Union[int, str]] = None,
    bracket: Optional[str] = None,
) -> Dict[str, Any]:
    """Average stats for a hero at a position WITHIN a rank bracket — the rank-grounded peer
    comparison OpenDota's all-bracket benchmarks can't give."""
    try:
        hid = await _hero_id(hero)  # resolving a hero NAME hits STRATZ; an int id does not
    except Exception as e:  # noqa: BLE001
        return {"error": f"STRATZ hero lookup failed: {e}"}
    if hid is None:
        return {"error": f"unknown hero: {hero!r}"}
    try:
        pos = int(position)
    except (TypeError, ValueError):
        return {"error": f"invalid position (use 1-5): {position!r}"}
    if not 1 <= pos <= 5:
        return {"error": f"invalid position (use 1-5): {position!r}"}

    br = _bracket_from_label(bracket) if bracket else None
    rank_tier_used = None
    if br is None and account_id is not None:
        try:
            aid = int(str(account_id).strip())
            data = await _gql(f"{{ player(steamAccountId: {aid}) {{ steamAccount {{ seasonRank }} }} }}")
            rank_tier_used = ((data.get("player") or {}).get("steamAccount") or {}).get("seasonRank")
            br = _bracket_from_tier(rank_tier_used)
        except Exception as e:  # noqa: BLE001
            return {"error": f"STRATZ rank lookup failed: {e}"}
    if br is None:
        return {"error": ("could not determine a rank bracket — pass bracket=herald/crusader/legend/"
                          "divine, or an account_id with a public rank")}

    query = (
        f"{{ heroStats {{ stats(heroIds: [{hid}], positionIds: [POSITION_{pos}], "
        f"bracketBasicIds: [{br}], groupByPosition: true, groupByBracket: true) {{ "
        f"heroId bracketBasicIds position matchCount winCount cs dn networth kills deaths assists "
        f"heroDamage towerDamage }} }} }}"
    )
    try:
        data = await _gql(query)
    except Exception as e:  # noqa: BLE001
        return {"error": f"STRATZ rank_benchmark failed: {e}"}

    stats = (data.get("heroStats") or {}).get("stats") or []
    if not stats:
        return {"error": (f"no STRATZ benchmark for hero {hid} at position {pos} in "
                          f"{_BRACKET_DISPLAY.get(br, br)} (hero may not be played in that role/bracket)")}
    row = stats[0]
    games, wins = row.get("matchCount") or 0, row.get("winCount") or 0
    return {
        "hero_id": hid,
        "position": pos,
        "bracket": _BRACKET_DISPLAY.get(br, br),
        "rank_tier_used": rank_tier_used,
        "sample_matches": games,
        "winrate": round(wins / games * 100, 1) if games else None,
        "avg_last_hits": _r(row.get("cs")),
        "avg_denies": _r(row.get("dn")),
        "avg_networth": _r(row.get("networth")),
        "avg_kills": _r(row.get("kills")),
        "avg_deaths": _r(row.get("deaths")),
        "avg_assists": _r(row.get("assists")),
        "avg_hero_damage": _r(row.get("heroDamage")),
        "avg_tower_damage": _r(row.get("towerDamage")),
        "note": ("this is the AVERAGE for that hero+position within the player's actual rank bracket "
                 "— the peer comparison OpenDota's all-bracket benchmarks can't give. It's a bracket "
                 "average, not a percentile curve, so phrase grounding as 'vs the bracket average' "
                 "(e.g. '242 last hits vs the ~237 Guardian-carry average'). STRATZ doesn't expose "
                 "GPM here; use avg_last_hits / avg_networth for farm."),
    }
