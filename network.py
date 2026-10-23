"""Road network model for CivicRoute AI.

Loads one JSON file per city from `cities/` and provides the three capabilities the MCP
server exposes: incident lookup, closure-aware route comparison and congestion estimation.
Everything here is deterministic so demo runs are repeatable.
"""
from __future__ import annotations

import heapq
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

CITIES_DIR = Path(__file__).resolve().parents[1] / "cities"
DEFAULT_CITY = "lahore"
_ROADS_OVERRIDE = os.environ.get("CIVICROUTE_ROADS", "").strip()
# A single alternate network file, still honoured for demos that only want one city.
DATA_FILE = Path(_ROADS_OVERRIDE) if _ROADS_OVERRIDE else None

# Congestion load added per road type / destination kind.
ROAD_TYPE_LOAD = {"highway": 0.20, "bridge": 0.30, "arterial": 0.14, "street": 0.06}
KIND_LOAD = {"airport": 0.12, "health": 0.10, "commercial": 0.08, "work": 0.06, "retail": 0.05}
DIVERSION_LOAD = 0.16  # per nearby closure - rerouted traffic has to go somewhere
MAX_FACTOR = 2.0
SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}
# Congestion level bands, shared by estimate_congestion and the map colours.
MODERATE_FACTOR = 1.3
HEAVY_FACTOR = 1.6
# Suffixes carried by almost every street name. Matching on them alone would return the
# whole network, so they are ignored when road names are matched word by word.
ROAD_GENERIC_WORDS = {"road", "rd", "street", "st", "avenue", "ave", "boulevard", "blvd", "lane", "drive", "marg"}


class UnknownPlace(ValueError):
    """Raised when an origin or destination cannot be resolved."""


@dataclass
class Node:
    id: str
    name: str
    area: str
    kind: str
    aliases: tuple
    lat: float = 0.0
    lng: float = 0.0

    @classmethod
    def from_raw(cls, raw: dict) -> "Node":
        return cls(
            id=raw["id"],
            name=raw["name"],
            area=raw.get("area", ""),
            kind=raw.get("kind", ""),
            aliases=tuple(a.lower() for a in raw.get("aliases", [])),
            lat=float(raw.get("lat", 0) or 0),
            lng=float(raw.get("lng", 0) or 0),
        )

    def search_terms(self) -> list:
        return [self.id.lower(), self.name.lower(), self.area.lower(), *self.aliases]

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "area": self.area, "kind": self.kind}


@dataclass
class Edge:
    id: str
    u: str
    v: str
    name: str
    road_type: str
    distance_km: float
    base_travel_min: float
    closed: bool

    @classmethod
    def from_raw(cls, raw: dict) -> "Edge":
        return cls(
            id=raw["id"],
            u=raw["from"],
            v=raw["to"],
            name=raw.get("name", raw["id"]),
            road_type=raw.get("road_type", "street"),
            distance_km=float(raw["distance_km"]),
            base_travel_min=float(raw["base_travel_min"]),
            closed=bool(raw.get("closed", False)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "road_type": self.road_type,
            "from": self.u,
            "to": self.v,
            "distance_km": self.distance_km,
            "base_travel_min": self.base_travel_min,
            "closed": self.closed,
        }


@dataclass
class Incident:
    id: str
    title: str
    edges: tuple
    kind: str
    severity: str
    status: str
    reported: str
    note: str
    closes_edge: tuple
    extra_delay_min: float

    @classmethod
    def from_raw(cls, raw: dict) -> "Incident":
        return cls(
            id=raw["id"],
            title=raw.get("title", raw["id"]),
            edges=tuple(raw.get("edges", [])),
            kind=raw.get("type", "unknown"),
            severity=raw.get("severity", "low"),
            status=raw.get("status", "active"),
            reported=raw.get("reported", ""),
            note=raw.get("note", ""),
            closes_edge=tuple(raw.get("closes_edge", [])),
            extra_delay_min=float(raw.get("extra_delay_min", 0) or 0),
        )

    @property
    def active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.kind,
            "severity": self.severity,
            "status": self.status,
            "reported": self.reported,
            "note": self.note,
            "extra_delay_min": self.extra_delay_min,
            "closes_edge": list(self.closes_edge),
            "affects_edges": list(self.edges),
        }


def tokens(text: str) -> list:
    """Lowercased word tokens of length >= 3, so stop words like "at"/"on" never match."""
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) >= 3]


class RoadNetwork:
    def __init__(self, raw: dict, city_id: Optional[str] = None) -> None:
        self.city = raw.get("city", "Unknown City")
        self.city_id = (city_id or raw.get("city_id") or self.city).strip().lower().replace(" ", "")
        self.updated_at = raw.get("updated_at", "")
        self.examples = list(raw.get("examples", []))
        self.default_origin = raw.get("default_origin", "")
        self.default_destination = raw.get("default_destination", "")
        self.nodes = {n["id"]: Node.from_raw(n) for n in raw["nodes"]}
        self.edges = {e["id"]: Edge.from_raw(e) for e in raw["edges"]}
        self.incidents = [Incident.from_raw(i) for i in raw.get("incidents", [])]

        self._adj = {nid: [] for nid in self.nodes}
        for edge in self.edges.values():
            if edge.u in self._adj and edge.v in self._adj:
                self._adj[edge.u].append(edge.id)
                self._adj[edge.v].append(edge.id)

        self._edge_by_upper = {e.id.upper(): e for e in self.edges.values()}
        self._incidents_by_edge = {}
        self._delay_by_edge = {}
        self._closure_reason = {}
        for inc in self.incidents:
            if not inc.active:
                continue
            for eid in inc.edges:
                self._incidents_by_edge.setdefault(eid, []).append(inc)
                if inc.extra_delay_min:
                    self._delay_by_edge[eid] = self._delay_by_edge.get(eid, 0.0) + inc.extra_delay_min
            for eid in inc.closes_edge:
                self._closure_reason.setdefault(eid, []).append(inc)

    @classmethod
    def load(cls, path: Optional[str] = None, city_id: Optional[str] = None) -> "RoadNetwork":
        target = Path(path) if path else (DATA_FILE or city_files()[DEFAULT_CITY])
        return cls(json.loads(target.read_text(encoding="utf-8")), city_id or target.stem)

    # ---------------------------------------------------------------- lookups

    def other_end(self, edge_id: str, node_id: str) -> str:
        edge = self.edges[edge_id]
        return edge.v if edge.u == node_id else edge.u

    def node(self, node_id: str) -> Node:
        if node_id not in self.nodes:
            raise UnknownPlace(
                "Unknown place {!r}. Known places: {}".format(node_id, ", ".join(sorted(self.nodes)))
            )
        return self.nodes[node_id]

    def find_node(self, ref: str) -> Optional[Node]:
        ref_lower = (ref or "").strip().lower()
        if not ref_lower:
            return None
        for node in self.nodes.values():
            if node.id.lower() == ref_lower or node.name.lower() == ref_lower:
                return node
        for node in self.nodes.values():
            if ref_lower in node.search_terms():
                return node
        return None

    def match_nodes(self, text: str) -> list:
        hay = (text or "").lower()
        toks = set(tokens(text))
        matches = []
        for node in self.nodes.values():
            hit = None
            for term in node.search_terms():
                if term in toks:
                    hit = term
                    break
                if " " in term and term in hay:
                    hit = term
                    break
            if hit:
                matches.append(node)
        return matches

    def match_edges(self, text: str, by_type: bool = False) -> list:
        hay = (text or "").lower()
        toks = set(tokens(text))
        matched = []
        for edge in self.edges.values():
            if edge.id.lower() in toks or edge.name.lower() in hay:
                matched.append(edge)
                continue
            if any(word in toks for word in edge.name.lower().split() if word not in ROAD_GENERIC_WORDS):
                matched.append(edge)
                continue
            if by_type and (edge.road_type in toks or edge.road_type.replace("_", " ") in hay):
                matched.append(edge)
                continue
            if any(node.id in (edge.u, edge.v) for node in self.match_nodes(text)):
                matched.append(edge)
        return matched

    def active_incidents(self) -> list:
        return [i for i in self.incidents if i.active]

    def file_closures(self) -> set:
        """Roads marked closed in the city file."""
        return {eid for eid, edge in self.edges.items() if edge.closed}

    def incident_closures(self) -> set:
        """Roads an active incident has shut."""
        return set(self._closure_reason)

    def current_closures(self) -> set:
        """Everything impassable right now."""
        return self.file_closures() | self.incident_closures()

    def resolve_edges(self, refs) -> dict:
        """Map caller-supplied road references to edge ids.

        Accepts a list, a comma separated string or dicts carrying an ``edge_id``.
        Returns {edge_id: reason}. Unknown references are reported under ``_unmatched``.
        """
        if refs is None:
            refs = []
        if isinstance(refs, str):
            refs = [part for part in re.split(r"[,;|]", refs) if part.strip()]
        if isinstance(refs, dict):
            refs = [refs]
        resolved = {}
        unmatched = []
        for ref in refs:
            if isinstance(ref, dict):
                ref = ref.get("edge_id") or ref.get("id") or ref.get("road") or ref.get("name") or ""
            text = str(ref).strip()
            if not text:
                continue
            exact = self._edge_by_upper.get(text.upper())
            if exact is not None:
                resolved[exact.id] = text
                continue
            hits = [e for e in self.match_edges(text)
                    if e.name.lower() in text.lower() or text.lower() in e.name.lower()]
            if hits:
                for edge in hits:
                    resolved[edge.id] = text
            else:
                unmatched.append(text)
        resolved["_unmatched"] = unmatched  # type: ignore[assignment]
        return resolved

    # ------------------------------------------------------------ congestion

    def congestion_factor(self, edge_id: str, closed_ids=(), peak: bool = True) -> float:
        edge = self.edges[edge_id]
        load = ROAD_TYPE_LOAD.get(edge.road_type, 0.08)
        ends = {edge.u, edge.v}
        diversions = sum(
            1
            for cid in closed_ids
            if cid in self.edges and cid != edge_id and ends & {self.edges[cid].u, self.edges[cid].v}
        )
        load += DIVERSION_LOAD * min(diversions, 3)
        load += max(
            KIND_LOAD.get(self.nodes[edge.u].kind, 0.0) if edge.u in self.nodes else 0.0,
            KIND_LOAD.get(self.nodes[edge.v].kind, 0.0) if edge.v in self.nodes else 0.0,
        )
        if not peak:
            load *= 0.6
        return round(min(1.0 + load, MAX_FACTOR), 2)

    def leg_minutes(self, edge_id: str, closed_ids=(), peak: bool = True) -> float:
        edge = self.edges[edge_id]
        return round(edge.base_travel_min * self.congestion_factor(edge_id, closed_ids, peak)
                     + self._delay_by_edge.get(edge_id, 0.0), 1)

    def congestion_level(self, factor: float) -> str:
        if factor >= HEAVY_FACTOR:
            return "heavy"
        if factor >= MODERATE_FACTOR:
            return "moderate"
        return "clear"

    # ---------------------------------------------------------------- search

    def shortest_path(self, origin: str, destination: str, blocked=frozenset(), peak: bool = True):
        """Dijkstra on live travel time. Returns (edge_ids, cost) or (None, inf)."""
        self.node(origin)
        self.node(destination)
        if origin == destination:
            return [], 0.0
        blocked = set(blocked)
        best = {origin: 0.0}
        prev = {}
        visited = set()
        heap = [(0.0, origin)]
        while heap:
            cost, current = heapq.heappop(heap)
            if current in visited:
                continue
            visited.add(current)
            if current == destination:
                break
            for edge_id in self._adj[current]:
                if edge_id in blocked:
                    continue
                nxt = self.other_end(edge_id, current)
                candidate = cost + self.leg_minutes(edge_id, blocked, peak)
                if candidate < best.get(nxt, float("inf")) - 1e-9:
                    best[nxt] = candidate
                    prev[nxt] = (current, edge_id)
                    heapq.heappush(heap, (candidate, nxt))
        if destination not in prev:
            return None, float("inf")
        path = []
        cursor = destination
        while cursor != origin:
            cursor, edge_id = prev[cursor]
            path.append(edge_id)
        path.reverse()
        return path, best[destination]

    def ordered_nodes(self, edge_ids, origin=None, destination=None):
        """Rebuild the node sequence a driver actually follows.

        Edges are undirected, so a city file's from/to order is not the travel direction.
        With two or more legs only one orientation stays connected; a single leg is
        connected both ways, so the trip's endpoints have to break that tie.
        """
        if not edge_ids:
            return []
        first = self.edges[edge_ids[0]]
        candidates = []
        for start in (first.u, first.v):
            nodes = [start]
            for eid in edge_ids:
                edge = self.edges[eid]
                if nodes[-1] == edge.u:
                    nodes.append(edge.v)
                elif nodes[-1] == edge.v:
                    nodes.append(edge.u)
                else:
                    break
            else:
                candidates.append(nodes)
        if not candidates:
            return None
        for nodes in candidates:
            if (origin is None or nodes[0] == origin) and (destination is None or nodes[-1] == destination):
                return nodes
        return candidates[0]

    def route_shape(self, edge_ids, origin=None, destination=None) -> str:
        nodes = self.ordered_nodes(edge_ids, origin, destination)
        if nodes is None:
            edge = self.edges[edge_ids[0]] if edge_ids else None
            nodes = [edge.u, edge.v] if edge else []
            for eid in edge_ids[1:]:
                nodes.append(self.edges[eid].v)
        return " -> ".join(nodes)

    def describe_route(
        self, route_id: str, label: str, edge_ids, closed_ids=(), peak: bool = True,
        origin: Optional[str] = None, destination: Optional[str] = None,
    ) -> dict:
        nodes = self.ordered_nodes(edge_ids, origin, destination) or []
        legs = []
        total_km = 0.0
        total_base = 0.0
        total_live = 0.0
        worst_factor = 1.0
        worst_leg = None
        blocked_used = []
        for index, eid in enumerate(edge_ids):
            edge = self.edges[eid]
            factor = self.congestion_factor(eid, closed_ids, peak)
            delay = round(self._delay_by_edge.get(eid, 0.0), 1)
            minutes = round(edge.base_travel_min * factor + delay, 1)
            incidents = [i.to_dict() for i in self._incidents_by_edge.get(eid, [])]
            from_id = nodes[index] if nodes else edge.u
            to_id = nodes[index + 1] if nodes else edge.v
            legs.append(
                {
                    "edge_id": eid,
                    "road": edge.name,
                    "road_type": edge.road_type,
                    "from": self.nodes[from_id].name,
                    "to": self.nodes[to_id].name,
                    "distance_km": edge.distance_km,
                    "base_travel_min": edge.base_travel_min,
                    "congestion_factor": factor,
                    "incident_delay_min": delay,
                    "minutes": minutes,
                    "incidents": incidents,
                }
            )
            total_km += edge.distance_km
            total_base += edge.base_travel_min
            total_live += minutes
            if factor > worst_factor:
                worst_factor = factor
                worst_leg = edge.name
            if eid in closed_ids:
                blocked_used.append(eid)
        shape = self.route_shape(edge_ids, origin, destination)
        return {
            "route_id": route_id,
            "label": label,
            "shape": shape,
            "legs": legs,
            "total_distance_km": round(total_km, 1),
            "base_travel_min": round(total_base, 1),
            "live_travel_min": round(total_live, 1),
            "avg_congestion_factor": round(total_live / total_base, 2) if total_base else 1.0,
            "worst_leg": {"road": worst_leg or "n/a", "factor": round(worst_factor, 2)},
            "uses_closed_roads": bool(blocked_used),
            "closed_roads_used": blocked_used,
            "feasible": not blocked_used,
        }

    # ------------------------------------------------------------- the tools

    def _incident_payload(self, inc: Incident) -> dict:
        payload = inc.to_dict()
        payload["affects"] = [
            {"edge_id": eid, "name": self.edges[eid].name, "road_type": self.edges[eid].road_type}
            for eid in inc.edges
            if eid in self.edges
        ]
        return payload

    def lookup_incident(self, location: str) -> dict:
        places = self.match_nodes(location)
        edges = self.match_edges(location, by_type=True)
        place_ids = {n.id for n in places}
        edge_ids = {e.id for e in edges}

        matched, resolved = [], []
        for inc in self.incidents:
            touched = [eid for eid in inc.edges if eid in edge_ids]
            ends = {self.edges[eid].u for eid in inc.edges if eid in self.edges} | {
                self.edges[eid].v for eid in inc.edges if eid in self.edges
            }
            if touched or (place_ids & ends) or tokens(inc.title) and any(
                t in set(tokens(location)) for t in tokens(inc.title)
            ):
                (matched if inc.active else resolved).append(self._incident_payload(inc))

        current = self.current_closures()
        closed_map = {}
        for inc in matched:
            for eid in inc["closes_edge"]:
                if eid in self.edges:
                    closed_map.setdefault(eid, []).append(inc["title"])
        for edge in edges:
            if edge.id in current:
                closed_map.setdefault(edge.id, [])

        # A driver who asks "any closures right now?" names no road we can match, but still
        # wants to know what is shut - widen to the whole city instead of reporting nothing.
        scope = "specific"
        if not matched and not closed_map:
            scope = "city-wide"
            matched = [self._incident_payload(i) for i in self.active_incidents()]
            for eid in sorted(current):
                closed_map.setdefault(eid, [])

        closed_roads = [
            {
                "edge_id": eid,
                "name": self.edges[eid].name,
                "road_type": self.edges[eid].road_type,
                "closed_in_network_file": self.edges[eid].closed,
                "caused_by": causes
                or [i.title for i in self._closure_reason.get(eid, [])]
                or ["closed in the city file"],
                "live_minutes_if_open": self.leg_minutes(eid, current),
            }
            for eid, causes in sorted(closed_map.items())
        ]

        region = "city-wide" if scope == "city-wide" else "in the matched area"
        parts = [
            "{} active incident(s) matched".format(len(matched)),
            "{} closed road(s) {}".format(len(closed_roads), region),
        ]
        if places:
            parts.append("matched places: " + ", ".join(p.name for p in places))
        if edges:
            parts.append("matched roads: " + ", ".join(sorted({e.name for e in edges})))
        if scope == "city-wide":
            parts.append("no road or place in that description could be matched, so every live incident is shown")
        if not matched and not closed_roads:
            parts.append("the network is clear - no active incident and no closed road anywhere")

        return {
            "query": location,
            "city": self.city,
            "city_id": self.city_id,
            "updated_at": self.updated_at,
            "default_origin": self.default_origin,
            "default_destination": self.default_destination,
            "scope": scope,
            "matched_places": [n.to_dict() for n in places],
            "places": self.places(),
            "matched_roads": [e.to_dict() for e in edges],
            "incidents": matched,
            "resolved_incidents": resolved,
            "closed_roads": closed_roads,
            "summary": "; ".join(parts),
        }

    def compare_routes(self, origin: str, destination: str, closed_roads=None, peak: bool = True) -> dict:
        origin_node = self.find_node(origin) or self.node(origin)
        dest_node = self.find_node(destination) or self.node(destination)

        requested = self.resolve_edges(closed_roads)
        unmatched = requested.pop("_unmatched", [])
        before = self.file_closures()
        effective = self.current_closures() | set(requested)

        routes = []
        adaptive, _ = self.shortest_path(origin_node.id, dest_node.id, frozenset(effective), peak)
        if adaptive is None:
            return {
                "error": "No route exists from {} to {} with the current closures.".format(
                    origin_node.name, dest_node.name
                ),
                "origin": origin_node.to_dict(),
                "destination": dest_node.to_dict(),
                "closed_roads": sorted(effective),
                "routes": [],
            }

        routes.append(
            self.describe_route(
                "adaptive", "Adaptive route (avoids every closure)", adaptive, effective, peak,
                origin_node.id, dest_node.id,
            )
        )

        seen_shapes = {routes[0]["shape"]}
        alternates = []
        for pinned in adaptive:
            blocked = set(effective) | {pinned}
            candidate, cost = self.shortest_path(origin_node.id, dest_node.id, frozenset(blocked), peak)
            if candidate is None:
                continue
            route = self.describe_route(
                "alt_{}".format(pinned),
                "Detour avoiding {}".format(self.edges[pinned].name),
                candidate,
                effective,
                peak,
                origin_node.id,
                dest_node.id,
            )
            if route["shape"] in seen_shapes:
                continue
            seen_shapes.add(route["shape"])
            alternates.append((cost, route))
        alternates.sort(key=lambda item: item[0])
        routes.extend(route for _, route in alternates[:2])

        # The plan a driver would have made from the city file alone, before any incident report:
        # priced with today's traffic, but allowed to cross roads that have since been shut.
        baseline, _ = self.shortest_path(origin_node.id, dest_node.id, frozenset(before), peak)
        if baseline is not None and self.route_shape(baseline, origin_node.id, dest_node.id) not in seen_shapes:
            routes.append(
                self.describe_route(
                    "pre_disruption", "Pre-disruption plan (before today's reports)", baseline, effective, peak,
                    origin_node.id, dest_node.id,
                )
            )

        feasible = [r for r in routes if r["feasible"]]
        fastest = min(feasible, key=lambda r: r["live_travel_min"]) if feasible else routes[0]
        for route in routes:
            route["delta_vs_fastest_min"] = round(route["live_travel_min"] - fastest["live_travel_min"], 1)

        return {
            "origin": origin_node.to_dict(),
            "destination": dest_node.to_dict(),
            "peak_hours": peak,
            "closed_roads": sorted(effective),
            "requested_closures": sorted(requested),
            "unmatched_closures": unmatched,
            "route_count": len(routes),
            "fastest_route_id": fastest["route_id"],
            "routes": routes,
        }

    def estimate_congestion(self, route, closed_roads=None, peak: bool = True) -> dict:
        if isinstance(route, dict):
            route = [leg.get("edge_id") for leg in route.get("legs", [])]
        elif isinstance(route, str):
            route = [part.strip() for part in re.split(r"[,;|>\s]+", route) if part.strip()]
        edge_ids = [str(e).strip() for e in (route or []) if str(e).strip()]

        if not edge_ids:
            return {"error": "No route supplied.", "hint": "Pass the edge_id list from compare_routes."}

        unknown = [e for e in edge_ids if e not in self.edges]
        if unknown:
            return {
                "error": "Unknown road segment id(s): {}".format(", ".join(unknown)),
                "hint": "Use the edge_id values returned by compare_routes.",
                "known_edges": [e.to_dict() for e in self.edges.values()],
            }

        requested = self.resolve_edges(closed_roads)
        requested.pop("_unmatched", None)
        blocked = self.current_closures() | set(requested)

        # Undirected legs connect only if consecutive edges share a node.
        gaps = [
            "{} -> {}".format(self.edges[first].name, self.edges[second].name)
            for first, second in zip(edge_ids, edge_ids[1:])
            if not {self.edges[first].u, self.edges[first].v} & {self.edges[second].u, self.edges[second].v}
        ]

        legs = [
            {
                "edge_id": eid,
                "road": self.edges[eid].name,
                "minutes": self.leg_minutes(eid, blocked, peak),
                "congestion_factor": self.congestion_factor(eid, blocked, peak),
                "incidents": [i.to_dict() for i in self._incidents_by_edge.get(eid, [])],
            }
            for eid in edge_ids
        ]
        total = round(sum(leg["minutes"] for leg in legs), 1)
        base = round(sum(self.edges[eid].base_travel_min for eid in edge_ids), 1)
        worst = max(legs, key=lambda leg: leg["congestion_factor"]) if legs else None
        severity = sum(SEVERITY_WEIGHT.get(i["severity"], 1) for leg in legs for i in leg["incidents"])
        reliability = 1.0 if not gaps else 0.6
        reliability -= min(0.35, 0.04 * severity)
        level = self.congestion_level(worst["congestion_factor"]) if worst else "clear"

        summary = (
            "{} min across {} segment(s) ({} vs {} min free-flow), congestion level: {}.".format(
                total, len(legs), total, base, level
            )
        )
        if gaps:
            summary += " Warning: the legs are not connected at " + ", ".join(gaps) + "."

        return {
            "shape": self.route_shape(edge_ids),
            "legs": legs,
            "total_live_min": total,
            "total_base_min": base,
            "delay_min": round(total - base, 1),
            "worst_leg": worst,
            "level": level,
            "reliability": round(max(0.2, reliability), 2),
            "connected": not gaps,
            "summary": summary,
        }

    # -------------------------------------------------------------- UI extras

    def places(self) -> list:
        return [n.to_dict() for n in self.nodes.values()]

    def map_state(self, closed_roads=None) -> dict:
        """Geometry plus live congestion for every road, so the page can draw the whole city."""
        requested = self.resolve_edges(closed_roads)
        requested.pop("_unmatched", None)
        blocked = self.current_closures() | set(requested)

        edges = []
        for edge in self.edges.values():
            a, b = self.nodes[edge.u], self.nodes[edge.v]
            factor = self.congestion_factor(edge.id, blocked)
            closed = edge.id in blocked
            edges.append(
                {
                    "edge_id": edge.id,
                    "name": edge.name,
                    "road_type": edge.road_type,
                    "from": edge.u,
                    "to": edge.v,
                    "lat1": a.lat, "lng1": a.lng, "lat2": b.lat, "lng2": b.lng,
                    "distance_km": edge.distance_km,
                    "base_travel_min": edge.base_travel_min,
                    "live_minutes": self.leg_minutes(edge.id, blocked),
                    "congestion_factor": factor,
                    "level": "closed" if closed else self.congestion_level(factor),
                    "closed": closed,
                    "caused_by": [i.title for i in self._closure_reason.get(edge.id, [])]
                    or (["Long-running closure in the city file"] if edge.closed else []),
                    "incidents": [i.to_dict() for i in self._incidents_by_edge.get(edge.id, [])],
                }
            )
        return {
            "city": self.city,
            "city_id": self.city_id,
            "updated_at": self.updated_at,
            "nodes": [
                {"id": n.id, "name": n.name, "area": n.area, "kind": n.kind, "lat": n.lat, "lng": n.lng}
                for n in self.nodes.values()
            ],
            "edges": edges,
            "closures": sorted(blocked),
        }

    def summary(self) -> dict:
        """What the page needs to draw a city selector without loading the map."""
        return {
            "city_id": self.city_id,
            "city": self.city,
            "updated_at": self.updated_at,
            "place_count": len(self.nodes),
            "road_count": len(self.edges),
            "incident_count": len(self.active_incidents()),
            "is_default": self.city_id == DEFAULT_CITY,
            "examples": self.examples,
            "default_origin": self.default_origin,
            "default_destination": self.default_destination,
        }


# ------------------------------------------------------------------- registry


def city_files() -> dict:
    """city_id -> JSON path, with the default city first so selectors lead with it."""
    if DATA_FILE is not None:
        return {DATA_FILE.stem.lower(): DATA_FILE}
    files = {p.stem.lower(): p for p in sorted(CITIES_DIR.glob("*.json"))}
    if not files:
        raise FileNotFoundError("No city files found in {}".format(CITIES_DIR))
    ordered = {}
    if DEFAULT_CITY in files:
        ordered[DEFAULT_CITY] = files.pop(DEFAULT_CITY)
    ordered.update(files)
    return ordered


def load_city(name: str = "") -> RoadNetwork:
    files = city_files()
    key = (name or DEFAULT_CITY).strip().lower().replace(" ", "")
    if key not in files:
        raise UnknownPlace("Unknown city {!r}. Known cities: {}".format(name, ", ".join(sorted(files))))
    return RoadNetwork.load(files[key], key)


def load_all_cities() -> dict:
    return {key: RoadNetwork.load(path, key) for key, path in city_files().items()}
