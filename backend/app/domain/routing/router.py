"""Flood-safe routing calculation engine using weighted Dijkstra on OSM road graph."""
from __future__ import annotations

import heapq
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from backend.app.domain.roads.models import AffectedRoad, RiskLevel
    from backend.app.domain.routing.graph import RoadGraph, EdgeData
    from backend.app.domain.routing.transport import TRANSPORT_POLICIES, edge_access, TransportMode
    from backend.app.domain.routing.models import (
        RouteSafetyStatus,
        RouteSegment,
        ShortestPathComparison,
        SafeRouteResponse,
        SnappedPoint,
    )
except ImportError:
    from app.domain.roads.models import AffectedRoad, RiskLevel
    from app.domain.routing.graph import RoadGraph, EdgeData
    from app.domain.routing.transport import TRANSPORT_POLICIES, edge_access, TransportMode
    from app.domain.routing.models import (
        RouteSafetyStatus,
        RouteSegment,
        ShortestPathComparison,
        SafeRouteResponse,
        SnappedPoint,
    )


PROVENANCE_DISCLOSURES = {
    "road_network": "OpenStreetMap road network (Mumbai pilot area)",
    "flood_hazard": "Modelled flood depth forecast (Copernicus DEM + Manning-D8 routing)",
    "routing_engine": "Hydraulically-weighted Dijkstra pathfinder (EPSG:32643)",
    "disclaimer": (
        "Experimental flood-safe routing based on modelled flood forecasts and OSM geometry. "
        "Does NOT reflect real-time traffic, municipal road closures, BMC official diversions, "
        "or live flood observations."
    ),
}


def _edge_identity(edge: EdgeData) -> str:
    """Stable identity for one directed graph edge, not merely its road name."""
    return f"{edge.road_id}:{edge.u}->{edge.v}"


def _run_dijkstra(
    graph: RoadGraph,
    start_node: int,
    target_node: int,
    flood_lookup: Dict[str, Tuple[Optional[float], RiskLevel]],
    exclude_high_critical: bool,
    transport_mode: Optional[TransportMode] = None,
) -> Tuple[Optional[List[EdgeData]], float, float]:
    """
    Execute Dijkstra pathfinding.
    
    Args:
        graph: Topological road graph
        start_node: Origin vertex index
        target_node: Destination vertex index
        flood_lookup: Dict mapping road_id -> (flood_depth_m, risk_level)
        exclude_high_critical: If True, exclude edges with RiskLevel >= HIGH
        
    Returns:
        (path_edges, total_cost, total_distance_m) or (None, inf, 0.0) if no path found.
    """
    if start_node == target_node:
        return [], 0.0, 0.0

    dist: Dict[int, float] = {start_node: 0.0}
    prev: Dict[int, Tuple[int, EdgeData, float]] = {}
    pq: List[Tuple[float, int]] = [(0.0, start_node)]

    while pq:
        curr_cost, u = heapq.heappop(pq)
        if u == target_node:
            break
        if curr_cost > dist.get(u, float("inf")):
            continue

        adjacency = graph.walk_adj if transport_mode == "walk" else graph.adj
        for edge in adjacency.get(u, []):
            v = edge.v
            road_id = edge.road_id
            if transport_mode in TRANSPORT_POLICIES and flood_lookup:
                flood_info = flood_lookup.get(road_id, (None, RiskLevel.NONE))
            else:
                # An empty lookup is the explicit distance-only/dry baseline.
                flood_info = flood_lookup.get(road_id, (0.0, RiskLevel.NONE))
            depth_m, risk = flood_info

            if transport_mode in TRANSPORT_POLICIES:
                policy = TRANSPORT_POLICIES[transport_mode]
                if not edge_access(edge, transport_mode)[0]:
                    continue
                if depth_m is not None and depth_m >= policy.exclusion_depth_m:
                    continue
                if depth_m is None:
                    weight = edge.length_m * (1.0 + policy.uncertainty_penalty)
                elif depth_m >= policy.hazard_depth_m:
                    weight = edge.length_m * (1.0 + policy.medium_depth_penalty * depth_m)
                elif depth_m > 0.001:
                    weight = edge.length_m * (1.0 + policy.low_depth_penalty * depth_m)
                else:
                    weight = edge.length_m
                new_cost = curr_cost + weight
                if new_cost < dist.get(v, float("inf")):
                    dist[v] = new_cost
                    prev[v] = (u, edge, weight)
                    heapq.heappush(pq, (new_cost, v))
                continue

            # Impassability rule: HIGH and CRITICAL roads are unavailable for safe routing
            if exclude_high_critical and risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
                continue

            # Weight calculation
            length = edge.length_m
            if risk == RiskLevel.MEDIUM:
                # Heavy penalty for medium flood (0.15m to <0.30m)
                weight = length * (1.0 + 8.0 * depth_m)
            elif risk == RiskLevel.LOW:
                # Moderate penalty for low flood (>0m to <0.15m)
                weight = length * (1.0 + 2.0 * depth_m)
            else:
                # Dry / unflooded road
                weight = length

            new_cost = curr_cost + weight
            if new_cost < dist.get(v, float("inf")):
                dist[v] = new_cost
                prev[v] = (u, edge, weight)
                heapq.heappush(pq, (new_cost, v))

    if target_node not in prev:
        return None, float("inf"), 0.0

    # Reconstruct path
    curr = target_node
    path_edges: List[EdgeData] = []
    while curr in prev:
        p_node, edge, _ = prev[curr]
        path_edges.append(edge)
        curr = p_node
    path_edges.reverse()

    total_dist = sum(e.length_m for e in path_edges)
    return path_edges, dist[target_node], total_dist


def _build_merged_linestring_coords(edges: List[EdgeData]) -> List[List[float]]:
    """Merge segment coordinates into a single continuous WGS84 LineString coordinate array."""
    if not edges:
        return []
    merged: List[List[float]] = []
    for edge in edges:
        for c in edge.coords_4326:
            if not merged or merged[-1] != c:
                merged.append(c)
    return merged


def calculate_flood_safe_route(
    start_lon: float,
    start_lat: float,
    end_lon: float,
    end_lat: float,
    horizon: str = "+1h",
    affected_roads: Optional[List[AffectedRoad]] = None,
    hazard_description: Optional[str] = None,
    transport_mode: Optional[str] = None,
    model_state: Optional[Dict[str, Any]] = None,
) -> SafeRouteResponse:
    """
    Calculate an optimal flood-safe route between origin and destination coordinates.
    
    1. Snaps origin and destination to the nearest nodes in the Mumbai pilot road network.
    2. Builds the flood penalty lookup from Phase 1.8 affected roads.
    3. Computes the baseline (unconstrained) direct shortest path.
    4. Computes the flood-safe path, excluding roads >= HIGH risk and penalizing LOW/MEDIUM roads.
    5. Identifies flooded roads avoided by taking the detour.
    6. Returns structured response with safety status, distance, travel cost, and GeoJSON geometry.

    The hazard the route was evaluated against is supplied by the caller as `affected_roads`;
    `hazard_description` states which hazard that was so the response can disclose it.
    """
    if transport_mode is not None:
        transport_mode = transport_mode.strip().lower()
        if transport_mode not in TRANSPORT_POLICIES:
            raise ValueError("transport_mode must be one of: car, walk")
    route_mode = transport_mode if transport_mode in TRANSPORT_POLICIES else None
    graph = RoadGraph.get_instance()
    hazard_unknown = route_mode is not None and affected_roads is None

    provenance = dict(PROVENANCE_DISCLOSURES)
    if hazard_description:
        provenance["hazard_assessment"] = hazard_description
    if route_mode:
        policy = TRANSPORT_POLICIES[route_mode]
        provenance.update({
            "transport_mode": route_mode,
            "transport_policy": (
                f"Provisional V1 screening: hazard >= {policy.hazard_depth_m:.2f} m; "
                f"exclude >= {policy.exclusion_depth_m:.2f} m; "
                "weights are configurable and not calibrated safety standards."
            ),
            "transport_access": (
                "OSM highway-class/access screening; Mumbai V1 extract has no surveyed "
                "sidewalk, crossing, closure, or water-velocity layer."
            ),
        })
        provenance["hazard_coverage"] = (
            "Complete computed dry state"
            if affected_roads == []
            else "Positive affected-road list; unlisted transport edges remain UNKNOWN"
            if affected_roads is not None
            else "No street hazard assessment available; all transport edges UNKNOWN"
        )

    # 1. Snap coordinates to network
    snap_mode = route_mode or "legacy"
    start_node, start_snap = graph.snap_coordinate(start_lon, start_lat, transport_mode=snap_mode)
    end_node, end_snap = graph.snap_coordinate(end_lon, end_lat, transport_mode=snap_mode)

    # 2. Build flood depth & risk lookup
    flood_lookup: Dict[str, Tuple[Optional[float], RiskLevel]] = {}
    flooded_road_names_all: Dict[str, str] = {}  # road_id -> name
    if affected_roads:
        for aff in affected_roads:
            flood_lookup[aff.road_id] = (aff.max_depth_m, aff.risk_level)
            flooded_road_names_all[aff.road_id] = aff.name
        if route_mode is not None:
            # V1's street response is a positive affected-road list, not a
            # complete edge-depth raster.  Once it contains observations,
            # every unlisted edge is UNKNOWN rather than silently dry.
            for road_id in graph.edge_by_road:
                flood_lookup.setdefault(road_id, (None, RiskLevel.NONE))
    elif hazard_unknown:
        # A missing assessment is different from a computed dry assessment.
        flood_lookup = {
            road_id: (None, RiskLevel.NONE)
            for road_id in graph.edge_by_road
        }

    # 3. Compute baseline unconstrained shortest path (no flood penalties)
    direct_edges, _, direct_dist = _run_dijkstra(
        graph=graph,
        start_node=start_node,
        target_node=end_node,
        # The comparison path is always the shortest eligible graph path; it
        # does not receive flood or UNKNOWN penalties.
        flood_lookup={},
        exclude_high_critical=False,
        transport_mode=route_mode,
    )

    if direct_edges is None:
        return SafeRouteResponse(
            status=RouteSafetyStatus.NO_PATH_FOUND,
            total_distance_m=0.0,
            estimated_travel_cost=None,
            max_predicted_flood_depth_m=0.0,
            roads_avoided=[],
            route_geometry=None,
            segments=[],
            shortest_path_comparison=None,
            horizon=horizon,
            start_snapped_to=start_snap,
            end_snapped_to=end_snap,
            provenance=provenance,
            transport_mode=route_mode or "car",
            safety_classification="NO_SAFE_ROUTE",
            explanation="No connected route exists for the selected Mumbai V1 network and transport mode.",
            model_state=model_state or {},
            transport_policy=TRANSPORT_POLICIES[route_mode].as_metadata() if route_mode else {},
            route_edge_ids=[],
        )

    # Analyze direct path flood impact
    direct_max_depth = 0.0
    direct_unknown_count = 0
    direct_known_count = 0
    direct_flooded_edge_ids: Set[str] = set()
    direct_flooded_road_ids: Set[str] = set()
    for e in direct_edges:
        depth, _ = flood_lookup.get(e.road_id, (0.0, RiskLevel.NONE))
        if depth is None:
            direct_unknown_count += 1
        else:
            direct_known_count += 1
            if depth > direct_max_depth:
                direct_max_depth = depth
        if depth is not None and depth > 0.001:
            direct_flooded_edge_ids.add(_edge_identity(e))
            direct_flooded_road_ids.add(e.road_id)

    shortest_comparison = ShortestPathComparison(
        distance_m=round(direct_dist, 3),
        edge_ids=[_edge_identity(e) for e in direct_edges],
        max_flood_depth_m=round(direct_max_depth, 3) if direct_known_count else None,
        unknown_segment_count=direct_unknown_count,
        has_flooded_roads=len(direct_flooded_road_ids) > 0,
        flooded_road_count=len(direct_flooded_edge_ids),
        geometry={
            "type": "LineString",
            "coordinates": _build_merged_linestring_coords(direct_edges),
        },
    )

    # 4. Compute flood-safe route (HIGH & CRITICAL roads excluded; MEDIUM & LOW penalized)
    safe_edges, safe_cost, safe_dist = _run_dijkstra(
        graph=graph,
        start_node=start_node,
        target_node=end_node,
        flood_lookup=flood_lookup,
        exclude_high_critical=route_mode is None,
        transport_mode=route_mode,
    )

    # If no safe route exists without traversing >= HIGH risk roads
    if safe_edges is None:
        # Find which high/critical roads blocked the direct route
        blocked_names = set()
        for e in direct_edges:
            depth, risk = flood_lookup.get(e.road_id, (0.0, RiskLevel.NONE))
            if (
                risk in (RiskLevel.HIGH, RiskLevel.CRITICAL)
                or (
                    route_mode is not None
                    and depth is not None
                    and depth >= TRANSPORT_POLICIES[route_mode].exclusion_depth_m
                )
                or (
                    route_mode is not None
                    and not edge_access(e, route_mode)[0]
                )
            ):
                blocked_names.add(e.name)

        return SafeRouteResponse(
            status=RouteSafetyStatus.UNAVAILABLE,
            total_distance_m=0.0,
            estimated_travel_cost=None,
            max_predicted_flood_depth_m=round(direct_max_depth, 3) if direct_known_count else None,
            roads_avoided=sorted(list(blocked_names)),
            route_geometry=None,
            segments=[],
            shortest_path_comparison=shortest_comparison,
            horizon=horizon,
            start_snapped_to=start_snap,
            end_snapped_to=end_snap,
            provenance=provenance,
            transport_mode=route_mode or "car",
            safety_classification="NO_SAFE_ROUTE",
            explanation=(
                f"No {route_mode or 'legacy'} route remains after the configured Mumbai V1 "
                "flood and access screening rules."
            ),
            model_state=model_state or {},
            transport_policy=TRANSPORT_POLICIES[route_mode].as_metadata() if route_mode else {},
            route_edge_ids=[],
        )

    # 5. Build route segments and calculate metrics
    max_route_depth = 0.0
    route_unknown_count = 0
    route_known_count = 0
    safe_road_ids: Set[str] = set()
    segments: List[RouteSegment] = []

    for e in safe_edges:
        safe_road_ids.add(e.road_id)
        depth, risk = flood_lookup.get(e.road_id, (0.0, RiskLevel.NONE))
        if depth is None:
            route_unknown_count += 1
        else:
            route_known_count += 1
            if depth > max_route_depth:
                max_route_depth = depth

        segments.append(
            RouteSegment(
                road_id=e.road_id,
                edge_id=_edge_identity(e),
                osm_id=e.osm_id,
                name=e.name,
                highway=e.highway,
                length_m=round(e.length_m, 1),
                flood_depth_m=round(depth, 3) if depth is not None else None,
                risk_level=(risk.value if isinstance(risk, RiskLevel) else str(risk)) if depth is not None else "UNKNOWN",
                geometry={"type": "LineString", "coordinates": e.coords_4326},
                uncertain=depth is None,
            )
        )

    # Identify roads avoided: flooded roads on direct path that were bypassed
    avoided_names: Set[str] = set()
    for rid in direct_flooded_road_ids:
        if rid not in safe_road_ids:
            name = flooded_road_names_all.get(rid)
            if name:
                avoided_names.add(name)

    # Determine safety status
    if route_unknown_count:
        safety_status = RouteSafetyStatus.UNKNOWN
    elif max_route_depth <= 0.001:
        safety_status = RouteSafetyStatus.SAFE
    else:
        safety_status = RouteSafetyStatus.CAUTION

    # Complete route geometry
    merged_coords = _build_merged_linestring_coords(safe_edges)
    route_geom = {"type": "LineString", "coordinates": merged_coords}

    classification = (
        "UNKNOWN" if safety_status == RouteSafetyStatus.UNKNOWN
        else "LOWER_RISK" if safety_status == RouteSafetyStatus.SAFE
        else "CAUTION"
    )
    explanation = (
        f"{(route_mode or 'car').title()} route uses Mumbai V1 modelled street depths "
        f"with a provisional {(route_mode or 'car')} screening policy."
        + (" Flood depth coverage is UNKNOWN for this request." if route_unknown_count else "")
    )
    return SafeRouteResponse(
        status=safety_status,
        total_distance_m=round(safe_dist, 3),
        estimated_travel_cost=round(safe_cost, 3),
        max_predicted_flood_depth_m=round(max_route_depth, 3) if route_known_count else None,
        roads_avoided=sorted(list(avoided_names)),
        route_geometry=route_geom,
        segments=segments,
        shortest_path_comparison=shortest_comparison,
        horizon=horizon,
        start_snapped_to=start_snap,
        end_snapped_to=end_snap,
        provenance=provenance,
        transport_mode=route_mode or "car",
        safety_classification=classification,
        uncertainty_count=route_unknown_count,
        explanation=explanation,
        model_state=model_state or {},
        transport_policy=TRANSPORT_POLICIES[route_mode].as_metadata() if route_mode else {},
        route_edge_ids=[_edge_identity(e) for e in safe_edges],
    )
