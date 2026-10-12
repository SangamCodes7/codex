"""REST API endpoint for Flood-Safe Routing."""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

try:
    from backend.app.domain.routing.models import SafeRouteResponse
    from backend.app.domain.routing.router import calculate_flood_safe_route
    from backend.routers.flood import get_street_flood_intelligence, get_historical_2017_replay
except ImportError:
    from app.domain.routing.models import SafeRouteResponse
    from app.domain.routing.router import calculate_flood_safe_route
    from routers.flood import get_street_flood_intelligence, get_historical_2017_replay

router = APIRouter(prefix="/routing", tags=["routing"])

# The only retrospective event with a per-timestep street hazard available for routing.
ROUTING_HAZARD_EVENTS = {"mumbai-2017-08-29-deluge": "29 August 2017 Mumbai Deluge"}


@router.get("/safe-route", response_model=SafeRouteResponse)
async def get_flood_safe_route(
    start_lon: float = Query(..., description="Origin longitude in WGS84 (EPSG:4326)"),
    start_lat: float = Query(..., description="Origin latitude in WGS84 (EPSG:4326)"),
    end_lon: float = Query(..., description="Destination longitude in WGS84 (EPSG:4326)"),
    end_lat: float = Query(..., description="Destination latitude in WGS84 (EPSG:4326)"),
    horizon: str = Query("+1h", description="Forecast horizon: NOW, +1h, +2h, +3h"),
    rainfall_scenario_mm: Optional[float] = Query(None, ge=0, description="Optional override/scenario rainfall depth [mm]"),
    event: Optional[str] = Query(None, description="Route against a retrospective event hazard instead of the forecast: pass event_id together with step"),
    step: Optional[int] = Query(None, ge=0, description="Timestep index of the retrospective event hazard to route against"),
):
    """
    Calculate an optimal flood-safe route between two coordinates in the Mumbai pilot area.
    
    Excludes impassable road segments (RiskLevel >= HIGH, depth >= 0.30m) and penalizes
    passable ponded roads (MEDIUM, LOW) using an operational depth-weighted cost function.

    The hazard source is either the forecast street assessment (default, optionally forced to a
    scenario rainfall depth) or, when event+step are supplied, the exact per-timestep street
    hazard of a retrospective replay — so a route drawn in Historical Replay avoids the same
    flooded streets the map is showing for that timestep.
    """
    # 1. Resolve which street hazard this route is evaluated against
    if (event is None) != (step is None):
        raise HTTPException(
            status_code=400,
            detail="Routing hazard selection incomplete: supply both 'event' and 'step', or neither.",
        )

    if event is not None:
        if event not in ROUTING_HAZARD_EVENTS:
            supported = ", ".join(sorted(ROUTING_HAZARD_EVENTS))
            raise HTTPException(
                status_code=404,
                detail=f"Unknown routing hazard event '{event}'. Supported events: {supported}.",
            )
        replay = get_historical_2017_replay(use_cache=True)
        if step >= len(replay.timesteps):
            raise HTTPException(
                status_code=400,
                detail=f"'step' {step} is outside this event's timestep range 0..{len(replay.timesteps) - 1}.",
            )
        timestep = replay.timesteps[step]
        if timestep.street_risk is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Replay timestep {step} ({timestep.time_display}) carries no street-level hazard, "
                    "so no flood-safe route can be evaluated for it."
                ),
            )
        affected_roads = timestep.street_risk.affected_roads
        hazard_description = (
            f"Retrospective replay hazard — {ROUTING_HAZARD_EVENTS[event]}, timestep {step} "
            f"({timestep.time_display}): rainfall {timestep.rainfall_mm} mm, peak modelled depth "
            f"{timestep.peak_flood_depth_m * 100:.0f} cm, {len(affected_roads)} affected road segments."
        )
    else:
        try:
            street_intel = await get_street_flood_intelligence(
                horizon=horizon,
                rainfall_mm=rainfall_scenario_mm,
                use_cache=True,
            )
            affected_roads = street_intel.affected_roads
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail=f"Street flood hazard assessment unavailable: {str(e)}"
            )
        scenario_note = (
            f"scenario rainfall {rainfall_scenario_mm} mm"
            if rainfall_scenario_mm is not None
            else "forecast rainfall"
        )
        hazard_description = (
            f"Forecast street hazard at horizon {horizon} from {scenario_note}: "
            f"{len(affected_roads)} affected road segments."
        )

    # 2. Run flood-safe routing engine
    try:
        route_result = calculate_flood_safe_route(
            start_lon=start_lon,
            start_lat=start_lat,
            end_lon=end_lon,
            end_lat=end_lat,
            horizon=horizon,
            affected_roads=affected_roads,
            hazard_description=hazard_description,
        )
        return route_result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Routing calculation failed: {str(e)}")
