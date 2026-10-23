# CivicRoute AI

The hackathon write-up lives in [docs/CivicRoute-AI-Report.md](docs/CivicRoute-AI-Report.md)
(same content as [docs/CivicRoute-AI-Report.docx](docs/CivicRoute-AI-Report.docx)).

An agentic traffic-planning MVP for the Amazon Alexa+ hackathon track. Describe a
disruption in plain English - *"Canal Road closed near Liberty Market, route me to Mayo
Hospital"* - and the agent looks it up, compares routes, prices congestion, and tells you
which roads to take and why. The answer lands on a live map: OpenStreetMap tiles with the
road corridors coloured by congestion, the recommended route drawn thick with A/B pins, and
every tool call the agent made written out underneath.

The network is simulated on top of real Pakistani geography: **Lahore, Karachi, Islamabad and
Peshawar**, each its own file in `cities/` with 7 actual places at their approximate
coordinates, 12 named road corridors (Canal Road and the Lahore Ring Road, Shahrah-e-Faisal,
Jinnah Avenue and the Islamabad Expressway, GT Road and Charsadda Road), and 4 invented
incident reports per city. Nothing reads a live traffic feed, so the demo is deterministic.
Pick a city from the dropdown and the map re-centers on it.

```
 you type a disruption, pick a city
            |
            v
  FastAPI page  civicroute/web_app.py
            |
            v
  Agent  civicroute/agent.py  --- Amazon Bedrock (Claude) tool calling
            |                             |
            |  MCP over Streamable HTTP   |  no AWS credentials?
            v                             v
  MCP server  civicroute/mcp_server.py  --- rule-based planner (same 3 tools)
            |
            v
  cities/*.json  civicroute/network.py  (Dijkstra + congestion model, one graph per city)
```

## What the agent does

It is not a single-shot prompt. Each run is a visible multi-step tool conversation. Every tool
takes a `city` argument, and the agent stamps the selected city onto all of them, so `E3` in
Karachi never means `E3` in Lahore:

1. `lookup_incident(location, city)` - match your sentence against places, roads, road types
   and incident reports; returns the road ids that are actually closed.
2. `compare_routes(origin, destination, closed_roads, city)` - Dijkstra over live travel time,
   producing the adaptive route, up to two detours, and the pre-disruption plan (which is
   usually flagged `feasible: false` because it now crosses a closed road).
3. `estimate_congestion(route, city)` - per-leg congestion factors, total minutes, worst
   stretch, reliability.

Every step (arguments, latency, summary and a trimmed result) is rendered on the page as the
agent's reasoning timeline.

**Bedrock path:** if AWS credentials resolve, Claude receives the three MCP tool schemas and
chooses the calls itself (`toolMode: auto`, up to 8 steps). MCP JSON-Schema keywords that
Bedrock rejects (`$schema`, `additionalProperties`, `$defs`, ...) are stripped before the call.

**Fallback path:** if `boto3` is missing, no region/credentials are configured, the Bedrock
call raises, or Claude returns no answer, the deterministic planner runs the same three tools
in the same order and composes the recommendation from templates. The page always shows which
engine answered and why it switched.

## Requirements

- Python 3.10+ (3.12 recommended)
- Two terminals: one for the MCP server, one for the web app
- Internet access from the browser, for the OpenStreetMap tiles and Leaflet from cdnjs
- Optional: AWS credentials with Amazon Bedrock model access

Verified on Windows with Python 3.12.10, `mcp` 1.30.0, FastAPI 0.156.8 / Starlette 1.9.0 and
uvicorn 0.54.0.

## Setup

Install Python if `python --version` only prints the Microsoft Store hint:

```powershell
winget install -e --id Python.Python.3.12
```

Then, from the `civicroute-ai` folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On macOS/Linux (or Git Bash on Windows):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Run

Terminal 1 - the MCP server (Streamable HTTP on port 8777):

```bash
python -m civicroute.mcp_server
```

Terminal 2 - the web app:

```bash
python -m uvicorn civicroute.web_app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>, choose a city, type a disruption, and press **Plan my route**.
The example chips under the text box are all worth trying, and they change with the city.
`http://127.0.0.1:8000/?city=karachi` opens that city directly.

Without AWS credentials you will see `rule-based planner` in the engine badge - that is the
intended demo path, not an error.

The **From** and **To** selects default to *Auto - read from my sentence*, so the agent parses
endpoints out of your words ("route me to Mayo Hospital", "I am leaving Model Town ... to the
airport"). Pin them with the selects if you want an exact origin/destination for the demo.

## Worked example

Type `Canal Road closed near Liberty Market, route me to Mayo Hospital` with the city on Lahore
and both selects on Auto. The agent plans the Liberty Market (Gulberg) to Mayo Hospital trip
and the timeline fills in like this:

1. `lookup_incident("Canal Road closed near Liberty Market, route me to Mayo Hospital", "lahore")` -
   matches Liberty Market and Mayo Hospital, finds the multi-vehicle collision on Canal Road,
   and reports edge `E3` closed (Multan Road `E9` is already shut in the file).
2. `parse_request` - destination `mayo` from "route me to Mayo Hospital"; no departure named,
   so Lahore's saved default `liberty` is used.
3. `compare_routes("liberty", "mayo", ["E3"], "lahore")` - four options. The adaptive route
   (Jail Road -> Mall Road via Data Darbar, 36.8 min) is fastest; the two detours come in at
   46.2 and 48.3 min; and the pre-disruption plan straight up Canal Road, 19.8 min, is marked
   `crosses a closure` because `E3` is shut.
4. `estimate_congestion(...)` for each serious candidate - Jail Road runs at 1.38x free-flow
   because it is absorbing the diverted Canal Road traffic, which is why the answer is
   "about 37 minutes, not 27".

Switch the city to Karachi and the same three steps run against `cities/karachi.json`: the
flagship example there closes `E2` (Shahrah-e-Faisal) and the recommendation drops to
Khayaban-e-Iqbal and Shahrah-e-Liaquat.

Because closures push diversion load onto the junctions around them, editing a city file and
restarting the server changes every number in the comparison consistently.

## Verify the pieces separately

Ask the MCP server directly what it negotiated and which tools it exposes:

```bash
python -c "
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client('http://127.0.0.1:8777/mcp') as (r, w, _):
        async with ClientSession(r, w) as s:
            info = await s.initialize()
            print('server:', info.serverInfo.name, '| protocol:', info.protocolVersion)
            for t in (await s.list_tools()).tools:
                print(t.name, list(t.inputSchema['properties']))

asyncio.run(main())
"
```

Expected output on `mcp` 1.30.0:

```
server: CivicRoute AI | protocol: 2025-11-25
lookup_incident ['location', 'city']
compare_routes ['origin', 'destination', 'closed_roads', 'peak', 'city']
estimate_congestion ['route', 'closed_roads', 'peak', 'city']
```

Check the web layer is up:

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/cities
curl "http://127.0.0.1:8000/api/map?city=islamabad"
curl -X POST http://127.0.0.1:8000/api/plan -H "Content-Type: application/json" \
  -d '{"city":"karachi","utterance":"Shahrah-e-Faisal closed near DHA, route me to Saddar","origin":"","destination":""}'
```

Empty `origin`/`destination` means "read them from my sentence"; set them to pin an endpoint.
Omit `city` to get the default (Lahore); an unknown city comes back as HTTP 400 with the list
of valid ids.

## Enabling Amazon Bedrock

1. Install `boto3` (already in `requirements.txt`).
2. Give the account Bedrock access to an Anthropic Claude model in your region
   (Console -> Bedrock -> Model access).
3. Provide credentials the standard way, e.g. `aws sso login --profile myprofile`, an
   `AWS_PROFILE`, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
4. Set the region and optionally the model id:

```powershell
$env:AWS_REGION = "us-east-1"
$env:CIVICROUTE_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
```

Restart the web app. The engine badge should read `Amazon Bedrock Claude / <model id>`.
If a call fails mid-run, the app prints the reason in the amber note and completes with the
rule-based plan instead of failing.

## Configuration

All optional - see `.env.example`. Export them in your shell (this project does not load
`.env` files automatically).

| Variable | Default | Purpose |
| --- | --- | --- |
| `CIVICROUTE_MCP_URL` | `http://127.0.0.1:8777/mcp` | Where the agent connects |
| `CIVICROUTE_MCP_HOST` / `CIVICROUTE_MCP_PORT` | `127.0.0.1` / `8777` | Server bind address |
| `CIVICROUTE_CITY` | `lahore` | City used when nothing else names one |
| `CIVICROUTE_ROADS` | empty | One network file to use instead of everything in `cities/` |
| `CIVICROUTE_MODEL_ID` | Claude Sonnet 4 in `us.` region | Bedrock model id |
| `CIVICROUTE_MAX_STEPS` | `8` | Cap on the Claude tool loop |

## Editing the scenario

Each city is one JSON file, `cities/lahore.json`, `cities/karachi.json`,
`cities/islamabad.json`, `cities/peshawar.json`. Add a file and it appears in the dropdown -
the filename stem is the `city` id:

- `city`, `city_id`, `updated_at` - header text; the MCP server instructions reuse the city list.
- `default_origin`, `default_destination` - where a trip starts and ends when the sentence does
  not say (this is what "take me home" resolves to).
- `examples` - the chips shown under the text box once that city is selected.
- `nodes` - `id`, `name`, `area`, `kind`, `aliases` (aliases are how "take me to the hospital"
  resolves) and `lat` / `lng`, the real coordinates the map draws between. Routing ignores
  the coordinates.
- `edges` - `from`, `to`, `name`, `road_type`, `distance_km`, `base_travel_min`, `closed`.
  `distance_km` is the straight-line distance between the two places inflated by 1.3 to look
  like a driving route. Lahore's Multan Road (`E9`) ships with `closed: true` as a long-running
  closure that no incident report owns; Canal Road (`E3`) is open in the file and is shut only
  by the live collision, which is what makes the "pre-disruption plan" row appear.
- `incidents` - `edges`, `closes_edge`, `extra_delay_min`, `status` (`active` counts, `resolved`
  does not), `severity`, `note`. All four per city are invented for the demo.

Congestion is deterministic, not random: each road type carries a base load (bridge and highway
worst), hotspot destinations add load, and **every closure pushes extra diversion load onto the
roads touching the same junctions** - capped at 2x free-flow. Close a road and watch the
alternatives reprice themselves. The map colours every road from that same model: under 1.3x
green, 1.3-1.6x amber, 1.6x and above red, closed dashed red with an X.

Because a dozen Pakistani street names end in "Road", that word alone never selects a road:
`ROAD_GENERIC_WORDS` in `network.py` drops `road`, `street`, `avenue` and friends when road
names are matched word by word, so "Jail Road" finds Jail Road and not the whole city.

`lookup_incident` answers `scope: "specific"` when your sentence named a road or place it
recognised, and `scope: "city-wide"` when it did not - in that case it returns every active
incident and every closed road rather than an empty list, so "any closures right now?" is a
useful question.

Peak vs off-peak: pass `peak: false` to `compare_routes` / `estimate_congestion`.

## Files

| Path | Role |
| --- | --- |
| `cities/lahore.json` | The default scenario: simulated network over real Lahore geography |
| `cities/karachi.json`, `cities/islamabad.json`, `cities/peshawar.json` | The same shape for the other three cities: nodes with lat/lng, edges with `closed` flags, incidents, examples, default endpoints |
| `civicroute/network.py` | Graph loading (one `RoadNetwork` per city), Dijkstra, congestion model, the three tool implementations, `map_state()` |
| `civicroute/mcp_server.py` | FastMCP server, Streamable HTTP, 3 tools, all cities in one process |
| `civicroute/agent.py` | MCP client, Bedrock tool loop, rule-based planner, recommendation text |
| `civicroute/web_app.py` | FastAPI routes, the city registry, and the map payload attached to every plan |
| `civicroute/templates/index.html` | The page: city dropdown, Leaflet + OpenStreetMap traffic layer, route cards, reasoning timeline |
| `docs/CivicRoute-AI-Report.md`, `.docx` | The hackathon write-up: architecture, worked example with real numbers, verification log |
| `requirements.txt`, `LICENSE` | Setup and MIT licence |

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Page says it could not reach the MCP server | Terminal 1 is not running, or ports differ. `CIVICROUTE_MCP_URL` must match the server's host/port. |
| Map area is a flat light-blue box with no streets | Leaflet or the tiles did not load - both come from a CDN (`cdnjs.cloudflare.com`, `tile.openstreetmap.org`). The route cards, steps and recommendation still work offline; only the basemap is missing. |
| `Python was not found` on Windows | Install with `winget install -e --id Python.Python.3.12`, or disable the Store app execution aliases in Settings -> Apps -> Advanced app settings. |
| Engine badge says `rule-based planner` even though you have AWS | The amber note explains why: missing `boto3`, no `AWS_REGION`, or credentials that do not resolve. `aws sts get-caller-identity` is the quickest check. |
| `ValidationException` from Bedrock about the tool schema | The model id is wrong for your region, or the account lacks Claude model access. Set `CIVICROUTE_MODEL_ID` to a model enabled in your region. |
| `ImportError: attempted relative import` | Run with `-m` (`python -m civicroute.mcp_server`) from the project root, not the file path. |
| Both terminals agree but 8777 is busy | `CIVICROUTE_MCP_PORT=8778 python -m civicroute.mcp_server` and point `CIVICROUTE_MCP_URL` at it. |

## Notes on the MCP protocol version

The server uses the official Python `mcp` SDK with the Streamable HTTP transport
(`mcp.run(transport="streamable-http")`). The SDK negotiates the protocol version on
`initialize`; install the newest `mcp` release to speak the 2025-11-25 revision - older SDK
falls back to the newest version it knows, which this server also accepts.

## Alexa+ angle

The three MCP tools are the natural voice surface: `lookup_incident` answers "what happened on
my commute", `compare_routes` answers "how do I get around it", `estimate_congestion` answers
"will I still make my flight". Because the agent returns a plain-language recommendation plus
structured steps, the same payload can drive an Alexa+ skill response with no changes to the
planning code.

## Licence

MIT - see [LICENSE](LICENSE).
