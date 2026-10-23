"""CivicRoute AI web front end.

A single page: pick a city, type a disruption, see it on the Leaflet map, get the route
comparison and the agent's steps. Each city is a JSON file in ``cities/``; the map is drawn
by the page from the JSON this module attaches to every response, so the router, the MCP
tools and the agent stay untouched.
Run:  python -m uvicorn civicroute.web_app:app --port 8000
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from . import agent
from .network import RoadNetwork, load_all_cities

ROADS_FILE = os.environ.get("CIVICROUTE_ROADS", "").strip()

if ROADS_FILE:
    _single = RoadNetwork.load(ROADS_FILE)
    networks = {_single.city_id: _single}
else:
    networks = load_all_cities()
default_city = next(iter(networks))

app = FastAPI(title="CivicRoute AI", version="0.1.0")
templates = Jinja2Templates(directory=str(os.path.join(os.path.dirname(__file__), "templates")))


def city_key(requested: str) -> str:
    return (requested or default_city).strip().lower().replace(" ", "")


def unknown_city(name: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "Unknown city {!r}.".format(name), "cities": sorted(networks)},
    )


def json_for_script(data: dict) -> str:
    """JSON safe to embed in a <script> element."""
    return json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")


def route_overlay(state: dict, comparison: dict) -> list:
    """Give each route the geographic coordinates of its legs, in travel order.

    ``shape`` is the node sequence a driver actually follows, while the base roads are
    drawn from the edge's own from/to pair, so the overlay must pick the endpoints that
    match the direction of travel.
    """
    geo = {e["edge_id"]: e for e in state["edges"]}
    nodes = {n["id"]: n for n in state["nodes"]}
    routes = []
    for route in comparison.get("routes", []) or []:
        order = [part.strip() for part in (route.get("shape") or "").split("->") if part.strip()]
        segments = []
        for index, leg in enumerate(route.get("legs", []) or []):
            edge = geo.get(leg.get("edge_id"))
            if not edge or index + 1 >= len(order):
                continue
            start, finish = nodes.get(order[index]), nodes.get(order[index + 1])
            if not start or not finish or {start["id"], finish["id"]} != {edge["from"], edge["to"]}:
                continue
            segments.append(
                {
                    "edge_id": edge["edge_id"],
                    "road": leg.get("road"),
                    "minutes": leg.get("minutes"),
                    "congestion_factor": leg.get("congestion_factor"),
                    "lat1": start["lat"], "lng1": start["lng"],
                    "lat2": finish["lat"], "lng2": finish["lng"],
                }
            )
        routes.append(
            {
                "route_id": route.get("route_id"),
                "label": route.get("label"),
                "live_travel_min": route.get("live_travel_min"),
                "base_travel_min": route.get("base_travel_min"),
                "total_distance_km": route.get("total_distance_km"),
                "avg_congestion_factor": route.get("avg_congestion_factor"),
                "delta_vs_fastest_min": route.get("delta_vs_fastest_min"),
                "feasible": route.get("feasible"),
                "crosses": route.get("closed_roads_used") or [],
                "segments": segments,
            }
        )
    return routes


def map_view(net: RoadNetwork, result: dict) -> dict:
    comparison = (result or {}).get("comparison") or {}
    state = net.map_state(comparison.get("closed_roads") or [])
    state["routes"] = route_overlay(state, comparison)
    state["recommended_route_id"] = comparison.get("fastest_route_id")
    state["origin"] = (comparison.get("origin") or {}).get("id")
    state["destination"] = (comparison.get("destination") or {}).get("id")
    return state


@app.get("/")
async def home(request: Request, city: str = ""):
    key = city_key(city)
    net = networks.get(key) or networks[default_city]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "city": net.city,
            "city_id": net.city_id,
            "updated_at": net.updated_at,
            "places": net.places(),
            "road_count": len(net.edges),
            "incident_count": len(net.active_incidents()),
            "examples": net.examples,
            "cities": json_for_script({"default": default_city, "list": [n.summary() for n in networks.values()]}),
            "mcp_url": agent.MCP_URL,
            "map_state": json_for_script(net.map_state()),
        },
    )


@app.get("/api/cities")
async def cities():
    return {"default": default_city, "cities": [n.summary() for n in networks.values()]}


@app.get("/api/map")
async def city_map(city: str = ""):
    net = networks.get(city_key(city))
    if net is None:
        return unknown_city(city)
    return net.map_state()


@app.post("/api/plan")
async def plan(request: Request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - tolerate an empty/non-JSON body
        payload = {}

    key = city_key(str(payload.get("city") or ""))
    if key not in networks:
        return unknown_city(str(payload.get("city")))
    net = networks[key]
    utterance = str(payload.get("utterance") or "").strip()
    origin = str(payload.get("origin") or "").strip() or None
    destination = str(payload.get("destination") or "").strip() or None

    try:
        result = await agent.plan(utterance, origin, destination, url=agent.MCP_URL, city=key)
    except Exception as exc:  # noqa: BLE001 - never leave the page hanging
        return JSONResponse(
            status_code=500,
            content={
                "engine": "crashed",
                "recommendation": "The planner raised {}: {}".format(type(exc).__name__, exc),
                "steps": [],
                "comparison": {},
                "pricing": {},
                "incidents": {},
                "map": net.map_state(),
                "note": "Check that the MCP server is running and that both processes share CIVICROUTE_MCP_URL.",
            },
        )
    result["city"] = key
    result["map"] = map_view(net, result)
    return result


@app.get("/api/health")
async def health():
    return {
        "default_city": default_city,
        "cities": {
            n.city_id: {"city": n.city, "places": len(n.nodes), "roads": len(n.edges),
                        "incidents": len(n.active_incidents())}
            for n in networks.values()
        },
    }
