# Safe Routing (Flood-Aware, Transport-Aware Routing)

*Two city-specific routing implementations: Mumbai/Kurla V1 and
Delhi/Kushak V2. Chennai V3 remains disabled.*

Mumbai V1 and Delhi V2 share the user-facing idea of a transport-aware flood
screen, but they do not share graph data, hydraulic state, or thresholds. The
sections below document each implementation separately.

## Mumbai/Kurla V1

V1 keeps its existing OSM Mumbai graph, Copernicus DEM + Manning-D8 flood
pipeline, and affected-road street matcher. `GET /routing/safe-route` accepts
the existing `horizon`, `rainfall_scenario_mm`, and `event` + `step` state
selectors, plus:

| Param | Meaning |
| --- | --- |
| `transport_mode` | Optional `car` or `walk`; omitted requests retain the legacy V1 behavior |
| `horizon` | `NOW`, `+1h`, `+2h`, or `+3h` live/scenario horizon |
| `rainfall_scenario_mm` | Scenario rainfall override used with the V1 street model |
| `event`, `step` | Exact Mumbai 2017 replay hazard frame |

V1 transport policy is deliberately a screening layer over the existing
modelled street depths:

- Car: provisional hazard margin at 0.10 m and exclusion at 0.30 m.
- Walk: provisional hazard margin at 0.05 m and exclusion at 0.15 m.
- Walking uses the V1 road centerline as a pedestrian proxy; the extract has
  no surveyed sidewalks, crossings, closures, or water-velocity field.
- OSM highway/access tags screen obvious mode-ineligible edges. One-way
  vehicle restrictions do not automatically make walking one-way.
- Policy thresholds and penalty weights are configurable prototype values,
  not calibrated safety standards.
- A missing street assessment is returned as `UNKNOWN`; it is never treated as
  zero-depth pavement. A computed empty affected-road list remains the V1
  model's dry result.
- A non-empty affected-road response is a positive-match list, not a complete
  edge-depth map. Unlisted transport edges therefore remain `UNKNOWN` and
  receive the configured uncertainty penalty; they are not silently treated as
  dry. This is why a historical route can be `UNKNOWN` even when its selected
  geometry avoids the positively matched flooded roads.

V1 responses include `transport_mode`, `safety_classification`, `model_state`
with a deterministic `model_state_id`, policy metadata, route uncertainty
counts, route edge identities for both the distance-only comparison and the
selected route, and provenance. `SAFE`/`CAUTION` retain the existing V1 status
values; `UNKNOWN`, `UNAVAILABLE`, and `NO_PATH_FOUND` remain explicit
outcomes.

The Mumbai frontend keeps the existing Safe Route map workflow and adds a
compact Car/Walk selector. Switching mode, forecast horizon, scenario, or
historical timestep reruns the route against that active V1 model state.

## Delhi/Kushak V2 meaning of "safe" (read first)

The recommended route is the route with the **lowest supported MODELED
flood exposure under the currently available evidence**. It is **never**
a guarantee of safety:

- The Kushak hydraulic runtime has no stage and no depth (no
  storage–stage relation exists), so the system can never establish
  road passability or water depth.
- The risk translation therefore never emits `BLOCKED` or `SAFE` from
  model data. The best a segment can get from positive evidence is
  `LOW_RISK` ("no modeled runoff loading under the documented
  scenario") — and even that carries an explicit "not a guarantee of
  dry conditions" caveat in its reason.
- `UNKNOWN` is never `SAFE`: unknown segments carry a routing penalty,
  are listed explicitly, and force the route's evidence state down to
  LOW (weakest link).

## Delhi/Kushak V2 architecture

```
            ┌─────────────────────────────┐
            │     SAFE ROUTING CORE       │
            │  backend/app/domain/delhi/  │
            │           routing/          │
            │  network.py  road graph     │
            │  risk.py     risk mapping   │
            │  engine.py   cost + search  │
            │  state_source.py adapters   │
            └──────────────┬──────────────┘
                            │ same engine, three state sources
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
      LIVE NOWCAST    MODEL SCENARIO   HISTORICAL REPLAY
      (forecast +     (V1 reference    (Phase 15.5 +
       V1 surface)    depth product)   selected frame)
```

- `network.py` — graph over the committed OSM-derived pilot GeoJSON
  (`backend/app/data/roads/delhi_kushak_roads.geojson`, 9,048 ways,
  acquired build-time via Overpass with a provenance sidecar; never
  scraped at runtime). Directed edges honoring one-ways; UTM 43N metric
  math; node snapping with an 800 m threshold; `lru_cache` singleton.
- `risk.py` — the ONLY place hydraulic state becomes road risk:
  - Spatial mapping: each road edge maps to its nearest reach centerline
    (UG-01 from the documented subsurface segment; OC-01/CD-01/OC-02 by
    chainage-fraction split of the open trunk) within a **150 m ASSUMED
    threshold**; otherwise `UNKNOWN` ("outside modeled corridor").
  - Translation (loading-based, stage-less):
    `blocked/missing member state → UNKNOWN`;
    `positive loading (inflow > 0 or storage above initial) → ELEVATED_RISK`;
    `zero loading across computed members → LOW_RISK`.
  - Member aggregation is **weakest-link**: any blocked member of the
    six makes the segment UNKNOWN. Ensemble disagreement is reported.
  - Route evidence state (documented derivation): UNKNOWN if no route
    segment has coverage; LOW if any segment is UNKNOWN (a single
    unknown portion caps the route — coverage is never averaged away);
    MEDIUM if fully covered with member disagreement; HIGH only when
    fully covered with full agreement.
  - Freshness: FRESH ≤ 15 min, RECENT ≤ 60 min, STALE beyond, UNKNOWN
    when no timestamp. Historical mode reports EVENT evidence timestamps
    and never presents them as live data age.
- `engine.py` — deterministic cost model and search:
  `edge_cost = travel_time_s + RISK_PENALTY_S_PER_M[state] × length_m`
  with documented ASSUMED weights: LOW 0.0, UNKNOWN 0.09 s/m
  (~1.5 min/km), ELEVATED 0.18 s/m (~3 min/km), BLOCKED hard-excluded.
  Travel speeds by OSM highway class are ASSUMED engineering defaults.
  Dijkstra with deterministic tie-breaking; the alternative route is a
  re-run with recommended roads re-costed (×4 divergence factor) and is
   returned only if it is genuinely distinct (edge-set difference ≥ 30%),
   otherwise `no_alternative_reason` explains why not.
- `transport.py` — validated `transport_mode: "car" | "walk"` rules. Car
  excludes foot-only highway classes and uses a provisional 0.30 m depth
  exclusion screen. Walk excludes motorways, requires explicit pedestrian
  access for trunk roads, and uses a provisional 0.15 m depth exclusion
  screen. Both values are configurable in `TRANSPORT_RISK_CONFIG`; they are
  screening parameters informed by NOAA/NWS *Turn Around, Don't Drown*
  guidance; the lower hazard bands are conservative early-warning margins,
  not Delhi calibration or a safety guarantee. Walking applies a
  stronger provisional penalty to elevated/uncertain segments because the
  committed network has no surveyed sidewalk, crossing, or accessibility
  layer.
- Positive modeled depth without a velocity field remains uncertain: missing
  velocity is never converted to zero. A confirmed closure or a mode
  threshold breach hard-excludes the edge. Missing surface coverage remains
  UNKNOWN and is never presented as lower risk.
- `state_source.py` — mode adapters producing the SAME per-member
  per-reach structure: LIVE from the cached deterministic nowcast
  ensemble at the departure hour; HISTORICAL from the genuine Phase 15.5
  replay runtime at event + timestep (cached per event). Historical
  adapters never read live state and vice versa.

## API

### `GET /api/delhi/safe-route`

| Param | Meaning |
| --- | --- |
| `origin_lon/lat`, `dest_lon/lat` | WGS84 coordinates (validated; snapped ≤ 800 m) |
| `mode` | `live` (default) or `historical` |
| `departure_hour` | LIVE: forecast hour 0–3 |
| `event_id`, `timestep_index` | HISTORICAL: catalogue event + replay timestep |
| `transport_mode` | Optional backward-compatible mode selector: `car` or `walk`; required by the transport-aware UI |
| `model_state_id` | Exact active live/scenario/replay frame identity; stale transport requests return 409 |
| `complete_24h` | HISTORICAL transport flag for the selected completed replay frame |
| `client_rainfall_mm` | Optional client-assisted live four-bin forcing used for that exact active frame |

Transport-aware responses identify `transport_mode`, `model_state`, and
`safety_classification` (`LOWER_RISK`, `CAUTION`, `UNKNOWN`, or
`NO_SAFE_ROUTE`). `recommended_route` includes distance, supported ETA (or
null), maximum modeled depth/velocity when available, and counts for
elevated, hazardous, uncertain, unknown, and blocked segments.
`alternative_route` is mode-specific and may be absent when no genuinely
distinct path exists. The response also includes `route_comparison`,
`evidence_state` (with the
derivation text), `explanation` (structured facts), `data_freshness`
(per-source timestamps), `provenance` (mode, event, model timestamp,
forcing source, road-network source + acquisition date, mapping method +
threshold, cost weights, member count), `route_run_id` (deterministic
hash of all route inputs — reproducible), `generated_at`, and a
`claim_policy` that states the modeled-exposure boundary.

Errors: 400 invalid coordinates / bad timestep; 404 unknown event or
segment; 409 historical routing requested for a non-executable event
(`NOT_COMPUTABLE` — absent forcing is never simulated).

### `GET /api/delhi/safe-route/segments/{segment_id}`

Segment-level evidence for ANY segment (route-agnostic): geometry
source, spatial mapping (method/threshold/mapped reach/status), risk
state + reason, underlying model state (per-reach ensemble aggregation),
model/forcing timestamps, mode/event/timestep, and the segment evidence
state. Missing information is `UNKNOWN`, never inferred. This is the
traceability chain: **route → segment → risk → model state → forcing →
source**.

## Historical replay behavior

- The historical route uses ONLY the replayed state of the selected
  event + timestep. No live state leaks in (test-enforced), and a
  historical run never mutates live state.
- Timestep risk is per-timestep: UNKNOWN timesteps produce UNKNOWN risk
  on the mapped corridor — the previous timestep's state is never
  carried forward, and nothing is interpolated between timesteps.
- Historical routes are labeled **MODEL RECONSTRUCTIONS** in the claim
  policy; they do not assert that a route was actually passable during
  the real event.
- Non-executable events (control, missing forcing) return HTTP 409 with
  `NOT_COMPUTABLE` — the system does not fabricate a historical route.

## Transport safety criteria and limitations

The route engine distinguishes evidence classes:

1. `SIMULATED_MODEL_OUTPUT` depth is a model estimate on the derived 30 m
   DSM and is not an observed road measurement.
2. Verified closures, if supplied by a future closure feed, are hard
   exclusions; no such feed is currently present in the committed extract.
3. The depth limits and cost weights in `routing/transport.py` are explicitly
   provisional screening parameters. They must be replaced or recalibrated
   after a Delhi vehicle/pedestrian hazard study and local observations are
   acquired.
4. Missing depth, missing velocity, incomplete access tags, or rapidly
   changing inputs remain UNKNOWN/uncertain. The frontend exposes this in the
   selected route metrics and explanation rather than converting it to a
   safety score.

The walking network is an OSM road-centerline proxy where no sidewalk or
crossing tags exist. It is not a pedestrian accessibility survey. No route
classification guarantees that a person or vehicle can safely pass.

## Frontend

The Mumbai V1 Safe Route workflow follows: choose LIVE FORECAST, MODEL
SCENARIO, or HISTORICAL REPLAY → click SAFE ROUTE → choose **Car** or
**Walk** → pick origin and destination on the map. The existing V1 route
geometry and shortest-path comparison remain visible; the HUD identifies the
active V1 model-state fingerprint, selected mode, provisional threshold, and
any UNKNOWN result. It does not draw a route when no route is available.

The Delhi V2 Safe Route workflow follows: choose LIVE FORECAST, MODEL
SCENARIO, or HISTORICAL REPLAY → click SAFE ROUTE → choose **Car** or
**Walk** → pick origin and destination on the map. The map draws the
mode-specific recommended route (thick, white-cased), any alternative
(dashed), ELEVATED segments (amber), and UNKNOWN segments (dotted grey) —
color plus line-style plus legend, never color alone — with origin and
destination pins. The compact HUD shows the selected transport mode,
classification, depth/velocity availability, uncertainty counts, active
forecast/scenario/replay frame, and the explanation. A no-safe-route result
is shown explicitly rather than drawing an unsafe route.

## Limitations

- Risk is loading-based: the runtime has no stage/depth, so ELEVATED
  means "modeled conveyance loading", not observed flooding, and BLOCKED
  is never produced by the model.
- Only corridor-adjacent roads (150 m threshold) receive model-informed
  risk; everything else is UNKNOWN. Most urban routes will therefore be
  LOW evidence with many UNKNOWN segments — that is the honest state of
  coverage, not a defect.
- Travel speeds and cost weights are ASSUMED engineering constants,
  documented and exposed in the API payload, not calibrated values.
- The road network is a bounded OSM pilot extract; route directness
  depends on OSM completeness (a 1.5–1.7× straight-line ratio is typical
  here; some origin pockets route longer).
- `route_run_id` reproducibility holds for identical inputs; a new
  forecast acquisition (live) legitimately changes the risk state and
  the run.
