"""CivicRoute AI MCP server.

Streamable HTTP transport, three tools, one process holding every city in ``cities/``.
The SDK negotiates the protocol version during ``initialize`` (latest spec, 2025-11-25,
when the installed ``mcp`` package supports it).

Run:  python -m civicroute.mcp_server
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .network import RoadNetwork, UnknownPlace, load_all_cities

HOST = os.environ.get("CIVICROUTE_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("CIVICROUTE_MCP_PORT", "8777"))
ROADS_FILE = os.environ.get("CIVICROUTE_ROADS", "").strip()

# Default city first, so an omitted `city` argument lands on the demo scenario.
if ROADS_FILE:
    _single = RoadNetwork.load(ROADS_FILE)
    networks = {_single.city_id: _single}
else:
    networks = load_all_cities()
default_city = next(iter(networks))


def _network(city: str = "") -> RoadNetwork:
    key = (city or "").strip().lower().replace(" ", "") or default_city
    if key not in networks:
        raise UnknownPlace(
            "Unknown city {!r}. Known cities: {}".format(city, ", ".join(sorted(networks)))
        )
    return networks[key]


_cities = ", ".join(n.city for n in networks.values())
mcp = FastMCP(
    "CivicRoute AI",
    instructions=(
        "Traffic planning tools for the simulated {} road networks ({}). ".format(len(networks), _cities)
        + "Plan in three steps: (1) lookup_incident with the driver's description of the "
        "disruption, (2) compare_routes with the origin, destination and every closed road "
        "id returned in step one, (3) estimate_congestion on each serious candidate route's "
        "edge_id list. Pass the same city argument to all three calls; edge and place ids are "
        "only unique inside one city. Only recommend a route that compare_routes reports as "
        "feasible=true."
    ),
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
)


def _failed(exc: Exception) -> dict[str, Any]:
    """Tool failures must come back as data, never as an aborted session."""
    hint = (
        "Known cities: {}.".format(", ".join(sorted(networks)))
        if isinstance(exc, UnknownPlace) and str(exc).startswith("Unknown city")
        else "Check the argument values."
    )
    return {"error": "{}: {}".format(type(exc).__name__, exc), "hint": hint}


@mcp.tool()
def lookup_incident(location: str, city: str = "") -> dict[str, Any]:
    """Find active incidents, closed roads and matched places for an area description.

    Args:
        location: Free text describing where to look, e.g. "Canal Road closed near Liberty
            Market", "Ring Road", "airport", or "hospital". Matches against node names,
            aliases, areas, road names, road types and incident titles. An empty or
            unmatched description widens to every live incident in the city.
        city: City id from "lahore", "karachi", "islamabad", "peshawar"; empty uses the
            default city.

    Returns:
        city and city_id, places (node ids usable as origin/destination with that city's
        default_origin/default_destination), matched_places, matched_roads, incidents
        (with the edges they close), resolved_incidents, closed_roads (edge ids to avoid)
        and a one-line summary.
    """
    try:
        return _network(city).lookup_incident(location)
    except Exception as exc:  # noqa: BLE001
        return _failed(exc)


@mcp.tool()
def compare_routes(
    origin: str,
    destination: str,
    closed_roads: list[str] | None = None,
    peak: bool = True,
    city: str = "",
) -> dict[str, Any]:
    """Compare candidate routes between two places, honouring the closures.

    Args:
        origin: Node id or name in that city, e.g. "liberty" or "Liberty Market, Gulberg".
        destination: Node id or name, e.g. "mayo" or "Mayo Hospital".
        closed_roads: Edge ids (or road names) to treat as impassable, e.g. ["E3"].
            These add to whatever is already closed in the city file and in active incidents.
        peak: True prices for rush hour, False for off-peak.
        city: City id from "lahore", "karachi", "islamabad", "peshawar"; empty uses the
            default city.

    Returns:
        Feasible adaptive and detour routes plus the pre-disruption plan (which may be marked
        feasible=false because it crosses a road that is now closed). Each route carries legs
        with edge ids, minutes and total live_travel_min, plus fastest_route_id.
    """
    try:
        return _network(city).compare_routes(origin, destination, closed_roads or [], peak)
    except Exception as exc:  # noqa: BLE001
        return _failed(exc)


@mcp.tool()
def estimate_congestion(
    route: list[str],
    closed_roads: list[str] | None = None,
    peak: bool = True,
    city: str = "",
) -> dict[str, Any]:
    """Price live congestion minute by minute for one route.

    Args:
        route: Ordered edge ids from compare_routes, e.g. ["E1", "E3", "E6"].
        closed_roads: Edge ids currently closed; nearby closures raise diversion load on the
            remaining roads.
        peak: True prices for rush hour, False for off-peak.
        city: City id from "lahore", "karachi", "islamabad", "peshawar"; empty uses the
            default city.

    Returns:
        Per-leg minutes and congestion factors, total_live_min, delay_min, worst_leg, a
        congestion level (clear/moderate/heavy), a reliability score, whether the legs are
        actually connected, and a plain-language summary.
    """
    try:
        return _network(city).estimate_congestion(route, closed_roads or [], peak)
    except Exception as exc:  # noqa: BLE001
        return _failed(exc)


if __name__ == "__main__":
    for net in networks.values():
        print("CivicRoute AI MCP server: {} | {} nodes, {} roads, {} incidents".format(
            net.city, len(net.nodes), len(net.edges), len(net.incidents)
        ))
    print("Streamable HTTP endpoint: http://{}:{}/mcp".format(HOST, PORT))
    mcp.run(transport="streamable-http")
