"""STRATZ-backed MCP tools: true role/position (1-5) and rank-bracket-segmented benchmarks.

These cover the two things the OpenDota tools fundamentally cannot: separating a pos1 carry from a
pos5 support, and grounding a stat against the player's actual rank bracket. Registered ONLY when
STRATZ_API_TOKEN is set (feedbot passes it through the MCP subprocess env); otherwise the server
exposes the OpenDota tools alone, exactly as before.
"""
import logging
from typing import Any, Dict, Optional, Union

from fastmcp import FastMCP

from ..config import STRATZ_API_TOKEN
from .. import stratz

logger = logging.getLogger("opendota-server")


def register_stratz_tools(mcp: FastMCP):
    """Register the STRATZ tools, but only if a STRATZ token is configured."""
    if not STRATZ_API_TOKEN:
        logger.info("STRATZ_API_TOKEN not set — STRATZ tools (get_role_summary/get_rank_benchmark) "
                    "not registered; OpenDota-only behavior unchanged")
        return

    @mcp.tool()
    async def get_role_summary(player_name: str) -> Dict[str, Any]:
        """Get a player's TRUE role/position split (position 1-5) from STRATZ — with per-position
        win rate, GPM/XPM, KDA, and STRATZ's IMP performance score.

        Use this for role/position questions OpenDota can't answer well:
        - "What position/role does [player] actually play?"
        - "Is [player] a carry or a support?"
        - "How does [player] do as a core vs a support?"

        STRATZ derives position per match (1=safelane carry .. 5=hard support) and separates a pos1
        carry from a pos5 support, which OpenDota's lane_role cannot. Position is only set on matches
        STRATZ has parsed, so check `positioned_sample`; when it's small, treat the split as
        indicative and confirm the role from the hero pool. Pass the numeric account_id as player_name.

        Returns: rank, rank_tier, total_matches, positioned_sample, and positions[] (each with
        position, games, winrate, gpm, xpm, kda, kills/deaths/assists, avg_imp).
        """
        return await stratz.role_summary(player_name)

    @mcp.tool()
    async def get_rank_benchmark(
        hero: Union[int, str],
        position: int,
        player_name: Optional[str] = None,
        bracket: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the average stats for a hero at a position WITHIN a rank bracket, from STRATZ — the
        rank-grounded peer comparison OpenDota's all-bracket benchmarks can't provide.

        Use this to judge whether a player's farm/impact is good FOR THEIR RANK:
        - "Is [player]'s farm good for their rank?"
        - "Is 240 last hits low for a Guardian carry?"
        - grounding any per-hero stat against the player's actual bracket.

        hero: hero name or id (e.g. "Faceless Void" or 41). position: 1-5 (1=carry .. 5=hard support).
        Pass the player's numeric account_id as player_name to auto-pick their bracket from their
        medal; OR pass bracket explicitly (herald/crusader/legend/divine) — useful when the medal is
        stale/unranked and the recent match_rank_tier shows a different real bracket.

        Returns the bracket AVERAGE (not a full percentile curve): sample_matches, winrate,
        avg_last_hits, avg_denies, avg_networth, avg_kills/deaths/assists, avg_hero_damage,
        avg_tower_damage. STRATZ doesn't expose GPM here — use last hits / networth for farm.
        """
        return await stratz.rank_benchmark(hero, position, account_id=player_name, bracket=bracket)
