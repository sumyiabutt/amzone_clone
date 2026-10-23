"""Planning agent for CivicRoute AI.

Drives the MCP tools over Streamable HTTP in several steps. Uses Amazon Bedrock
(Claude) tool calling when AWS credentials resolve, otherwise a deterministic
rule-based planner. Both engines return the same shape: engine name, the recorded
tool calls, and a plain-language recommendation.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("CIVICROUTE_MCP_URL", "http://127.0.0.1:8777/mcp")
BEDROCK_MODEL_ID = os.environ.get("CIVICROUTE_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
MAX_AGENT_STEPS = int(os.environ.get("CIVICROUTE_MAX_STEPS", "8"))
DEFAULT_CITY = os.environ.get("CIVICROUTE_CITY", "lahore")

# Bedrock rejects several JSON-Schema keywords that MCP tool schemas carry.
_SCHEMA_DROP = {"$schema", "additionalProperties", "$defs", "definitions", "default", "examples", "title"}


class McpUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------- plumbing


def _decode(result: Any) -> Any:
    """Turn a CallToolResult into plain Python data."""
    if getattr(result, "isError", False):
        texts = [b.text for b in getattr(result, "content", []) or [] if getattr(b, "text", None)]
        return {"error": " ".join(texts) or "tool reported an error"}

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured["result"] if set(structured) == {"result"} else structured

    texts = [b.text for b in getattr(result, "content", []) or [] if getattr(b, "text", None)]
    joined = "\n".join(texts).strip()
    if not joined:
        return {"error": "tool returned no content"}
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined}


def _sanitize_schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _sanitize_schema(v) for k, v in node.items() if k not in _SCHEMA_DROP}
    if isinstance(node, list):
        return [_sanitize_schema(v) for v in node]
    return node


class ToolRouter:
    """One MCP session, shared by every step of a single plan."""

    def __init__(self, url: str = MCP_URL, city: str = DEFAULT_CITY) -> None:
        self.url = url
        self.city = (city or DEFAULT_CITY).strip().lower().replace(" ", "") or DEFAULT_CITY
        self._transport = None
        self._session = None
        self.session: Optional[ClientSession] = None
        self.tool_specs: list = []
        self.payloads: dict = {}

    async def __aenter__(self) -> "ToolRouter":
        try:
            self._transport = streamablehttp_client(self.url)
            read, write, _ = await self._transport.__aenter__()
            self._session = ClientSession(read, write)
            self.session = await self._session.__aenter__()
            await self.session.initialize()
            listed = await self.session.list_tools()
            self.tool_specs = list(listed.tools)
        except Exception as exc:  # noqa: BLE001
            raise McpUnavailable(
                "Could not reach the CivicRoute MCP server at {}. Start it with "
                "`python -m civicroute.mcp_server`.".format(self.url)
            ) from exc
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._session is not None:
            await self._session.__aexit__(*exc_info)
        if self._transport is not None:
            await self._transport.__aexit__(*exc_info)

    async def call(self, name: str, arguments: dict) -> tuple:
        arguments = dict(arguments)
        # Scope every call to this plan's city, even when the model omits the argument.
        arguments.setdefault("city", self.city)
        started = time.perf_counter()
        result = await self.session.call_tool(name, arguments)
        payload = _decode(result)
        self.payloads.setdefault(name, []).append(payload)
        step = {
            "tool": name,
            "arguments": arguments,
            "ms": round((time.perf_counter() - started) * 1000),
            "ok": not (isinstance(payload, dict) and "error" in payload),
            "summary": auto_summary(name, payload),
            "preview": _preview(payload),
        }
        return payload, step


def _preview(payload: Any) -> Any:
    """Trim a tool payload so the UI can show something readable next to each step."""
    if not isinstance(payload, dict):
        return payload
    keep = ("summary", "fastest_route_id", "closed_roads", "error")
    out = {k: payload[k] for k in keep if k in payload}
    if "incidents" in payload:
        out["incidents"] = [i.get("title") for i in payload["incidents"]]
    if "routes" in payload:
        out["routes"] = [
            {"route_id": r.get("route_id"), "label": r.get("label"), "live_travel_min": r.get("live_travel_min"),
             "feasible": r.get("feasible")}
            for r in payload["routes"]
        ]
    if "total_live_min" in payload:
        out["total_live_min"] = payload["total_live_min"]
        out["level"] = payload.get("level")
    return out or {k: payload[k] for k in list(payload)[:4]}


def auto_summary(name: str, payload: Any) -> str:
    if isinstance(payload, dict) and payload.get("error"):
        return "failed: {}".format(payload["error"])
    if not isinstance(payload, dict):
        return "returned a result"
    if name == "lookup_incident":
        return "{} active incident(s), {} closed road(s), {} place(s) matched".format(
            len(payload.get("incidents", [])), len(payload.get("closed_roads", [])), len(payload.get("matched_places", []))
        )
    if name == "compare_routes":
        return "{} route option(s), fastest {} min".format(
            payload.get("route_count", 0),
            _fastest_minutes(payload),
        )
    if name == "estimate_congestion":
        return payload.get("summary", "priced the route")
    return "ok"


def _fastest_minutes(compare: dict) -> Any:
    best = compare.get("fastest_route_id")
    for route in compare.get("routes", []):
        if route.get("route_id") == best:
            return route.get("live_travel_min")
    return "?"


# ------------------------------------------------------------------- Bedrock


def detect_bedrock() -> tuple:
    """Return (client bundle, None) when Bedrock is usable, else (None, reason)."""
    try:
        import boto3
    except ImportError:
        return None, "boto3 is not installed - run `pip install boto3`"

    session = boto3.session.Session()
    region = session.region_name or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        return None, "no AWS region set - export AWS_REGION=us-east-1"
    try:
        credentials = session.get_credentials()
        access_key = credentials.access_key if credentials else None
    except Exception as exc:  # noqa: BLE001
        return None, "AWS credentials did not resolve: {}".format(exc)
    if not access_key:
        return None, "no AWS credentials found - configure `aws sso login` or AWS_ACCESS_KEY_ID"
    try:
        client = session.client("bedrock-runtime", region_name=region)
    except Exception as exc:  # noqa: BLE001
        return None, "could not create a bedrock-runtime client: {}".format(exc)
    return {"client": client, "region": region}, None


SYSTEM_PROMPT = (
    "You are CivicRoute AI, a traffic planning assistant for simulated city road networks "
    "served by these MCP tools. "
    "Plan in several visible steps: first call lookup_incident on the disruption the driver described, "
    "then call compare_routes with the origin, the destination and every closed road id you just found, "
    "then call estimate_congestion on each serious candidate route using its edge_id list. "
    "Pass the city argument on every call so place and edge ids stay in the same network. "
    "Never recommend a route that compare_routes marks feasible=false. "
    "When the tools are exhausted, answer in plain language for a driver: which roads to take, how long "
    "it will really take, what was closed, why you rejected the obvious alternative, and any risk."
)


def _user_prompt(utterance: str, origin: Optional[str], destination: Optional[str], city: str) -> str:
    return (
        "City: {city}.\n"
        "Disruption reported: {utterance}\n"
        "Trip: from {origin} to {destination}.\n"
        "Use the tools to plan this trip and recommend one route."
    ).format(
        city=city,
        utterance=utterance or "no specific disruption reported, just current conditions",
        origin=origin or "the driver's usual starting point, work it out from the tools",
        destination=destination or "the driver's usual destination, work it out from the tools",
    )


async def plan_with_bedrock(router: ToolRouter, bedrock: dict, utterance: str,
                            origin: Optional[str], destination: Optional[str], steps: list) -> Optional[str]:
    client = bedrock["client"]
    tools = [
        {
            "toolSpec": {
                "name": spec.name,
                "description": spec.description or spec.name,
                "inputSchema": {"json": _sanitize_schema(spec.inputSchema)},
            }
        }
        for spec in router.tool_specs
    ]
    messages = [{"role": "user", "content": [{"text": _user_prompt(utterance, origin, destination, router.city)}]}]

    for _ in range(MAX_AGENT_STEPS):
        response = await asyncio.to_thread(
            client.converse,
            modelId=BEDROCK_MODEL_ID,
            messages=messages,
            system=[{"text": SYSTEM_PROMPT}],
            toolConfig={"tools": tools, "toolChoice": {"auto": {}}},
            inferenceConfig={"maxTokens": 2048, "temperature": 0.2},
        )
        message = response["output"]["message"]
        content = message["content"]
        messages.append({"role": message["role"], "content": content})

        if response.get("stopReason") == "tool_use":
            for block in content:
                use = block.get("toolUse")
                if not use:
                    continue
                payload, step = await router.call(use["name"], use["input"])
                step["source"] = "claude"
                step["reasoning"] = "".join(b.get("text", "") for b in content if "text" in b).strip()
                steps.append(step)
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "toolResult": {
                                    "toolUseId": use["toolUseId"],
                                    "content": [{"json": payload}],
                                    "status": "success" if step["ok"] else "error",
                                }
                            }
                        ],
                    }
                )
            continue

        text = "".join(block.get("text", "") for block in content).strip()
        if text:
            return text
        return None
    return None


# ----------------------------------------------------------------- rule based


def _pick_place(fragment: str, places: list) -> Optional[str]:
    """Return the place mentioned *first* in the fragment.

    ``places`` comes back in no particular order, so matching it list-first can hand back
    the destination when the caller wanted the departure: the "from ..." window in
    "from Johar Town to Liberty Market" holds both names. Area names are deliberately not
    matched - the Peshawar airport sits inside Hayatabad, so an area needle steals the
    wrong end of the trip.
    """
    text = (fragment or "").lower()
    hits = []
    for place in places:
        pid = place.get("id")
        if not pid:
            continue
        for needle in (pid.lower(), str(place.get("name", "")).lower()):
            position = text.find(needle) if needle else -1
            if position >= 0:
                hits.append((position, -len(needle), pid))
                break
    return min(hits)[2] if hits else None


def pick_endpoints(places: list, utterance: str, origin: Optional[str], destination: Optional[str],
                   default_origin: str, default_destination: str) -> dict:
    low = (utterance or "").lower()
    notes = []

    dest = destination
    if dest:
        notes.append("destination taken from the form")
    else:
        match = re.search(r"\b(?:to|towards|heading to|going to|route me back to|route me to)\s+([a-z ]{3,24})", low)
        dest = _pick_place(match.group(1), places) if match else None
        if not dest and any(word in low for word in ("home", "house", "my place")):
            dest = default_destination
            notes.append('"route me home" style phrasing, using the saved home area')
        if not dest:
            # Places named in a disruption report are where the incident is, not where the
            # driver wants to end up, so an unstated destination falls back to home.
            dest = default_destination
            notes.append("no destination named, using the saved default")

    orig = origin
    if orig:
        notes.append("origin taken from the form")
    else:
        match = re.search(r"\b(?:from|leaving|starting at|i am at|i am in|departing)\s+([a-z ]{3,24})", low)
        orig = _pick_place(match.group(1), places) if match else None
        if not orig:
            # A place named in the sentence is usually where the disruption is, not where the
            # driver is, so an unstated departure falls back to the saved default.
            orig = default_origin
            notes.append("no departure named, using the saved default")

    return {"origin": orig, "destination": dest, "notes": notes}


async def build_evidence(router: ToolRouter, utterance: str, origin: Optional[str],
                         destination: Optional[str], steps: list, source: str = "rules") -> dict:
    """The deterministic plan: incident lookup, endpoint choice, route comparison, pricing."""
    info, step = await router.call("lookup_incident", {"location": utterance or "current conditions across the network"})
    step["source"] = source
    steps.append(step)

    matched = info.get("matched_places", []) if isinstance(info, dict) else []
    known = info.get("places", []) if isinstance(info, dict) else []
    closed = [c["edge_id"] for c in info.get("closed_roads", [])] if isinstance(info, dict) else []
    # Matched places win, but the city's full place list is a candidate too: the "from ..."
    # window can hold a name lookup_incident did not score high enough to report.
    places = list(matched) + [p for p in known if p.get("id") not in {m.get("id") for m in matched}]
    endpoints = pick_endpoints(
        places,
        utterance,
        origin,
        destination,
        info.get("default_origin", "") if isinstance(info, dict) else "",
        info.get("default_destination", "") if isinstance(info, dict) else "",
    )
    steps.append(
        {
            "tool": "parse_request",
            "arguments": {"utterance": utterance, "city": router.city, "matched_places": [p["id"] for p in matched]},
            "ms": 0,
            "ok": True,
            "source": source,
            "summary": "planning {} -> {} ({})".format(
                endpoints["origin"], endpoints["destination"], "; ".join(endpoints["notes"])
            ),
            "preview": endpoints,
        }
    )

    comparison, step = await router.call(
        "compare_routes",
        {"origin": endpoints["origin"], "destination": endpoints["destination"], "closed_roads": closed},
    )
    step["source"] = source
    steps.append(step)

    pricing = {}
    if isinstance(comparison, dict) and not comparison.get("error"):
        candidates = sorted(
            [r for r in comparison.get("routes", []) if r.get("feasible")], key=lambda r: r["live_travel_min"]
        )
        for route in candidates[:3]:
            report, cstep = await router.call(
                "estimate_congestion",
                {"route": [leg["edge_id"] for leg in route["legs"]], "closed_roads": closed},
            )
            cstep["source"] = source
            cstep["route_id"] = route["route_id"]
            steps.append(cstep)
            if isinstance(report, dict):
                pricing[route["route_id"]] = report

    return {
        "comparison": comparison if isinstance(comparison, dict) else {},
        "pricing": pricing,
        "incidents": info if isinstance(info, dict) else {},
        "closed_roads": closed,
        "endpoints": endpoints,
    }


async def plan_with_rules(router: ToolRouter, utterance: str, origin: Optional[str],
                          destination: Optional[str], steps: list) -> dict:
    evidence = await build_evidence(router, utterance, origin, destination, steps)
    comparison = evidence["comparison"]
    if comparison.get("error"):
        recommendation = comparison["error"]
    elif not comparison:
        recommendation = "The routing tools did not return a comparison."
    else:
        recommendation = explain(comparison, evidence["pricing"], evidence["incidents"])
    return dict(evidence, recommendation=recommendation)


def explain(comparison: dict, pricing: dict, incident_info: dict) -> str:
    """Turn the tool results into the plain-language recommendation."""
    origin = comparison["origin"]["name"]
    destination = comparison["destination"]["name"]
    routes = comparison.get("routes", [])
    feasible = sorted([r for r in routes if r.get("feasible")], key=lambda r: r["live_travel_min"])
    if not feasible:
        return ("I could not find a passable route from {} to {} with the roads currently closed. "
                "Wait for a reopening or start from somewhere else.").format(origin, destination)

    best = feasible[0]
    price = pricing.get(best["route_id"], {})
    worst = price.get("worst_leg") or {}
    street_names = ", ".join(dict.fromkeys(leg["road"] for leg in best["legs"]))

    lines = [
        "Take {} to {}: about {:.0f} minutes in current traffic.".format(
            _lower_first(best["label"]), destination, best["live_travel_min"]
        ),
        "Roads: {}.".format(street_names),
    ]

    blocked = incident_info.get("closed_roads") or []
    if blocked:
        lines.append("This avoids the closed {}.".format(", ".join(b["name"] for b in blocked)))
    best_edges = {leg["edge_id"] for leg in best["legs"]}
    causes = [
        i["title"]
        for i in (incident_info.get("incidents") or [])
        if i.get("closes_edge") or best_edges & set(i.get("affects_edges", []))
    ][:2]
    if causes:
        lines.append("Reason: {}.".format("; ".join(causes)))

    rejected = next((r for r in routes if not r.get("feasible")), None)
    if rejected:
        crossed = ", ".join(_crossed_roads(rejected)) or "a road that is now closed"
        lines.append(
            "The pre-disruption plan ({:.0f} min on paper) is out: it runs onto {}.".format(
                rejected["live_travel_min"], crossed
            )
        )

    runners_up = feasible[1:3]
    if runners_up:
        lines.append(
            "Alternatives: {}.".format(
                ", ".join("{:.0f} min ({})".format(r["live_travel_min"], _lower_first(r["label"])) for r in runners_up)
            )
        )

    if worst.get("road"):
        lines.append(
            "Slowest stretch is {}, about {:.2f}x free-flow.".format(
                worst["road"], worst.get("congestion_factor", 1.0)
            )
        )
    if price.get("delay_min"):
        lines.append("That is {:.0f} min more than an empty road.".format(price["delay_min"]))
    if rejected:
        crossed_name = (_crossed_roads(rejected) or ["your usual route"])[0]
        lines.append(
            "The detour costs {:.0f} min more than {} did before it was shut.".format(
                best["live_travel_min"] - rejected["live_travel_min"], crossed_name
            )
        )

    return " ".join(lines)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _crossed_roads(route: dict) -> list:
    used = set(route.get("closed_roads_used", []))
    return [leg["road"] for leg in route.get("legs", []) if leg["edge_id"] in used]


# ---------------------------------------------------------------------- facade


def _evidence_from_claude(router: ToolRouter) -> dict:
    """Reuse what Claude already fetched, so the interface needs no extra tool calls."""
    comparison = next((p for p in reversed(router.payloads.get("compare_routes", [])) if isinstance(p, dict)), {})
    incidents = next((p for p in reversed(router.payloads.get("lookup_incident", [])) if isinstance(p, dict)), {})
    priced = [p for p in router.payloads.get("estimate_congestion", []) if isinstance(p, dict)]

    pricing = {}
    for route in comparison.get("routes", []):
        match = next((p for p in priced if p.get("shape") == route.get("shape")), None)
        if match:
            pricing[route["route_id"]] = match

    return {
        "comparison": comparison,
        "pricing": pricing,
        "incidents": incidents,
        "closed_roads": [c["edge_id"] for c in incidents.get("closed_roads", []) if isinstance(c, dict)],
    }


async def plan(utterance: str = "", origin: Optional[str] = None, destination: Optional[str] = None,
               url: str = MCP_URL, city: str = DEFAULT_CITY) -> dict:
    """Run the agent: Claude drives the MCP tools when Bedrock resolves, otherwise rules."""
    router = ToolRouter(url, city)
    try:
        await router.__aenter__()
    except McpUnavailable as exc:
        return {
            "engine": "unavailable",
            "city": router.city,
            "error": str(exc),
            "note": str(exc),
            "steps": [],
            "recommendation": str(exc),
            "comparison": {},
            "pricing": {},
            "incidents": {},
        }

    steps: list = []
    bedrock, why_not = detect_bedrock()
    engine = "rule-based planner"
    note = None
    result: Optional[dict] = None

    if bedrock:
        engine = "Amazon Bedrock Claude / {}".format(BEDROCK_MODEL_ID)
        try:
            recommendation = await plan_with_bedrock(router, bedrock, utterance, origin, destination, steps)
            if recommendation:
                result = dict(_evidence_from_claude(router), recommendation=recommendation)
            else:
                note = "Claude returned no final answer, so the rule-based planner took over."
        except Exception as exc:  # noqa: BLE001 - a Bedrock outage must still produce a route
            note = "Bedrock call failed ({}: {}), so the rule-based planner took over.".format(
                type(exc).__name__, exc
            )
            steps.append(
                {
                    "tool": "bedrock.converse",
                    "arguments": {"model": BEDROCK_MODEL_ID},
                    "ms": 0,
                    "ok": False,
                    "source": "claude",
                    "summary": note,
                    "preview": {},
                }
            )
    else:
        note = "Bedrock not usable ({}), so the rule-based planner ran instead.".format(why_not)

    if result is None:
        engine = "rule-based planner"
        steps = []  # keep the timeline consistent with the engine that actually answered
        result = await plan_with_rules(router, utterance, origin, destination, steps)

    try:
        await router.__aexit__(None, None, None)
    except Exception:  # noqa: BLE001
        pass

    return dict(result, engine=engine, note=note, city=router.city, steps=steps)
